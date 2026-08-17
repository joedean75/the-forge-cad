"""Lightweight 2D sketch constraint solver.

A sketch is points + lines + arcs + circles with geometric/dimensional
constraints. We map the free coordinates (and circle radii) to a variable
vector, express each constraint as a residual, and solve the nonlinear
least-squares system with scipy. Solved loops of lines, arcs and circles become
a `loops` profile (see `cad_kernel.geom.profile2d`) that both kernels build from
directly.

Constraints: fixed, coincident, horizontal, vertical, distance, parallel,
perpendicular, equal_length, radius, point_on_line, arc_radius, tangent,
concentric, symmetric, angle, midpoint, equal_radius, point_on_circle. Arcs
carry an implicit equal-radius residual (both endpoints equidistant from the
center) so they stay circular without an explicit constraint.

A line, arc or circle marked ``construction: true`` is solved and constrained
like any other, and left out of the profile: a centreline or a bolt circle is
there to measure from, not to extrude.

**The solver says what is wrong, not just whether it converged.** `converged`
was `residual < 1e-6`, which an *unconstrained* sketch satisfies trivially — the
same class of bug as the hardcoded `ok: True` the operation envelope was built
to kill, and the one a model would learn to exploit fastest. Every solve now
also reports a `status` (`well_constrained` / `under_constrained` /
`over_constrained` / `conflicting`), how many degrees of freedom are left, and
*which constraints* are fighting or redundant. See `diagnose`.
"""

from __future__ import annotations

import math
from math import hypot
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
from scipy.optimize import least_squares


class SketchError(ValueError):
    pass


def _index_variables(points: List[dict]) -> Tuple[Dict[str, int], List[float]]:
    """Assign each point's (x, y) a slot in the variable vector."""
    var_index: Dict[str, int] = {}
    x0: List[float] = []
    for p in points:
        var_index[p["id"]] = len(x0)
        x0.append(float(p.get("x", 0.0)))
        x0.append(float(p.get("y", 0.0)))
    return var_index, x0


class _Row:
    """One residual owner: what it is, how to evaluate it, what it depends on.

    ``columns`` is the set of variable indices the row actually reads. It is the
    difference between a Jacobian that costs ``2 · variables`` full evaluations
    and one that costs two per *nonzero*, which for a sketch — where a constraint
    touches four or six coordinates out of hundreds — is the whole cost.
    """

    __slots__ = ("owner", "evaluate", "columns", "width")

    def __init__(self, owner, evaluate, columns, width: int):
        self.owner = owner
        self.evaluate = evaluate
        self.columns = tuple(sorted(columns))
        self.width = width


class _Model:
    """A sketch compiled for evaluation.

    The entity tables and the per-constraint closures are built **once**. They
    used to be rebuilt inside every residual evaluation — four dicts and six
    closures, several hundred times a solve — which was 91% of the time spent
    solving even a modest sketch.
    """

    __slots__ = ("spec", "vi", "radii", "rows", "n_vars", "n_res", "spans", "by_column")

    def __init__(self, spec, vi, radii, rows, n_vars):
        self.spec = spec
        self.vi = vi
        self.radii = radii
        self.rows = rows
        self.n_vars = n_vars
        self.spans = []
        cursor = 0
        for row in rows:
            self.spans.append((cursor, cursor + row.width))
            cursor += row.width
        self.n_res = cursor
        # column -> the rows that read it, for the sparse Jacobian.
        self.by_column: List[List[int]] = [[] for _ in range(n_vars)]
        for i, row in enumerate(rows):
            for column in row.columns:
                self.by_column[column].append(i)


def _compile(spec: dict, vi: Dict[str, int], radii: Dict[str, int], n_vars: int) -> _Model:
    """Resolve every constraint into a closure over variable indices."""
    lines = {l["id"]: l for l in spec.get("lines", [])}
    arcs = {a["id"]: a for a in spec.get("arcs", [])}
    circles = {c["id"]: c for c in spec.get("circles", [])}
    authored = {p["id"]: p for p in spec.get("points", [])}

    def point(pid):
        """``(x column, y column)`` for a point id."""
        base = vi.get(pid)
        if base is None:
            raise SketchError(f"constraint references unknown point: {pid}")
        return base, base + 1

    def segment(lid):
        ln = lines.get(lid)
        if ln is None:
            raise SketchError(f"constraint references unknown line: {lid}")
        return point(ln["a"]) + point(ln["b"])

    def centre(eid):
        entity = circles.get(eid) or arcs.get(eid)
        if entity is None:
            raise SketchError(f"constraint references unknown circle or arc: {eid}")
        return point(entity["center"])

    def radius(eid):
        """``(fn, columns)`` — a circle's variable, or an arc's measured radius."""
        if eid in radii:
            column = radii[eid]
            return (lambda x, i=column: x[i]), (column,)
        arc = arcs.get(eid)
        if arc is None:
            raise SketchError(f"constraint references unknown circle or arc: {eid}")
        cx, cy = point(arc["center"])
        ax, ay = point(arc["a"])
        bx, by = point(arc["b"])

        def measured(x, cx=cx, cy=cy, ax=ax, ay=ay, bx=bx, by=by):
            ox, oy = x[cx], x[cy]
            return (hypot(x[ax] - ox, x[ay] - oy) + hypot(x[bx] - ox, x[by] - oy)) / 2.0

        return measured, (cx, cy, ax, ay, bx, by)

    def offset_from_line(px_i, py_i, lid):
        """``fn`` for the signed perpendicular distance from a point to a line."""
        ax, ay, bx, by = segment(lid)

        def distance(x, ax=ax, ay=ay, bx=bx, by=by, px_i=px_i, py_i=py_i):
            ex, ey = x[bx] - x[ax], x[by] - x[ay]
            wx, wy = x[px_i] - x[ax], x[py_i] - x[ay]
            return (ex * wy - ey * wx) / (hypot(ex, ey) + 1e-12)

        return distance, (ax, ay, bx, by, px_i, py_i)

    rows: List[_Row] = []
    for index, c in enumerate(spec.get("constraints", [])):
        owner = ("constraint", index)
        rows.append(_constraint_row(owner, c, point, segment, centre, radius,
                                    offset_from_line, lines, arcs, authored))

    # Implicit per-arc residual: keep the arc circular by forcing both endpoints
    # equidistant from the center.
    for arc_id, arc in arcs.items():
        cx, cy = point(arc["center"])
        ax, ay = point(arc["a"])
        bx, by = point(arc["b"])

        def circular(x, cx=cx, cy=cy, ax=ax, ay=ay, bx=bx, by=by):
            ox, oy = x[cx], x[cy]
            return [hypot(x[ax] - ox, x[ay] - oy) - hypot(x[bx] - ox, x[by] - oy)]

        rows.append(_Row(("arc", arc_id), circular, (cx, cy, ax, ay, bx, by), 1))

    return _Model(spec, vi, radii, rows, n_vars)


def _constraint_row(owner, c, point, segment, centre, radius, offset_from_line,
                    lines, arcs, authored) -> _Row:
    """One constraint, as a closure over variable indices plus the columns it reads."""
    t = c.get("type")

    if t == "fixed":
        # One row per axis actually given. `x` used to default to the point's
        # *live* coordinate, which makes a residual that is identically zero
        # with a zero derivative: it pinned nothing, yet counted as an equation,
        # so pinning only y reported a spurious redundancy. With neither axis
        # given it falls back to where the point was authored, which is what
        # "fixed" plainly means.
        pid = c["point"]
        px_i, py_i = point(pid)
        home = authored.get(pid) or {}
        wants_x = "x" in c or ("y" not in c)
        wants_y = "y" in c or ("x" not in c)
        at_x = float(c.get("x", home.get("x", 0.0)))
        at_y = float(c.get("y", home.get("y", 0.0)))
        columns = ([px_i] if wants_x else []) + ([py_i] if wants_y else [])

        def pinned(x, px_i=px_i, py_i=py_i, at_x=at_x, at_y=at_y,
                   wants_x=wants_x, wants_y=wants_y):
            out = []
            if wants_x:
                out.append(x[px_i] - at_x)
            if wants_y:
                out.append(x[py_i] - at_y)
            return out

        return _Row(owner, pinned, columns, len(columns))

    if t == "coincident":
        ax, ay = point(c["a"])
        bx, by = point(c["b"])
        return _Row(owner, lambda x: [x[ax] - x[bx], x[ay] - x[by]],
                    (ax, ay, bx, by), 2)

    if t in ("horizontal", "vertical"):
        ax, ay, bx, by = segment(c["line"])
        if t == "horizontal":
            return _Row(owner, lambda x: [x[by] - x[ay]], (ay, by), 1)
        return _Row(owner, lambda x: [x[bx] - x[ax]], (ax, bx), 1)

    if t == "distance":
        ax, ay = point(c["a"])
        bx, by = point(c["b"])
        value = float(c["value"])
        return _Row(owner, lambda x: [hypot(x[ax] - x[bx], x[ay] - x[by]) - value],
                    (ax, ay, bx, by), 1)

    if t in ("parallel", "perpendicular"):
        a1x, a1y, b1x, b1y = segment(c["line1"])
        a2x, a2y, b2x, b2y = segment(c["line2"])
        columns = (a1x, a1y, b1x, b1y, a2x, a2y, b2x, b2y)

        if t == "parallel":
            def crossed(x):
                return [(x[b1x] - x[a1x]) * (x[b2y] - x[a2y])
                        - (x[b1y] - x[a1y]) * (x[b2x] - x[a2x])]
            return _Row(owner, crossed, columns, 1)

        def dotted(x):
            return [(x[b1x] - x[a1x]) * (x[b2x] - x[a2x])
                    + (x[b1y] - x[a1y]) * (x[b2y] - x[a2y])]
        return _Row(owner, dotted, columns, 1)

    if t == "equal_length":
        a1x, a1y, b1x, b1y = segment(c["line1"])
        a2x, a2y, b2x, b2y = segment(c["line2"])

        def same_length(x):
            return [hypot(x[b1x] - x[a1x], x[b1y] - x[a1y])
                    - hypot(x[b2x] - x[a2x], x[b2y] - x[a2y])]
        return _Row(owner, same_length, (a1x, a1y, b1x, b1y, a2x, a2y, b2x, b2y), 1)

    if t == "radius":
        measure, columns = radius(c.get("circle") or c.get("arc"))
        value = float(c["value"])
        return _Row(owner, lambda x: [measure(x) - value], columns, 1)

    if t == "point_on_line":
        px_i, py_i = point(c["point"])
        distance, columns = offset_from_line(px_i, py_i, c["line"])
        return _Row(owner, lambda x: [distance(x)], columns, 1)

    if t == "arc_radius":
        arc = arcs.get(c["arc"])
        if arc is None:
            raise SketchError(f"arc_radius references unknown arc: {c['arc']}")
        cx, cy = point(arc["center"])
        ax, ay = point(arc["a"])
        value = float(c["value"])
        return _Row(owner, lambda x: [hypot(x[ax] - x[cx], x[ay] - x[cy]) - value],
                    (cx, cy, ax, ay), 1)

    if t == "tangent":
        return _tangent_row(owner, c, point, segment, centre, radius,
                            offset_from_line, lines)

    if t == "concentric":
        ax, ay = centre(c["a"])
        bx, by = centre(c["b"])
        return _Row(owner, lambda x: [x[ax] - x[bx], x[ay] - x[by]], (ax, ay, bx, by), 2)

    if t == "symmetric":
        lax, lay, lbx, lby = segment(c["line"])
        ax, ay = point(c["a"])
        bx, by = point(c["b"])

        # Two halves: the pair's midpoint sits on the mirror line, and the line
        # joining them crosses it square. Either alone is not symmetry.
        def mirrored(x):
            ex, ey = x[lbx] - x[lax], x[lby] - x[lay]
            length = hypot(ex, ey) + 1e-12
            wx = (x[ax] + x[bx]) / 2.0 - x[lax]
            wy = (x[ay] + x[by]) / 2.0 - x[lay]
            return [(ex * wy - ey * wx) / length,
                    (ex * (x[bx] - x[ax]) + ey * (x[by] - x[ay])) / length]

        return _Row(owner, mirrored, (lax, lay, lbx, lby, ax, ay, bx, by), 2)

    if t == "angle":
        a1x, a1y, b1x, b1y = segment(c["line1"])
        a2x, a2y, b2x, b2y = segment(c["line2"])
        wanted = math.radians(float(c["value"]))

        def between(x):
            d1x, d1y = x[b1x] - x[a1x], x[b1y] - x[a1y]
            d2x, d2y = x[b2x] - x[a2x], x[b2y] - x[a2y]
            actual = math.atan2(d1x * d2y - d1y * d2x, d1x * d2x + d1y * d2y)
            # Wrapped into (-pi, pi] so 179 deg and -181 deg are the same ask.
            return [(actual - wanted + math.pi) % (2 * math.pi) - math.pi]

        return _Row(owner, between, (a1x, a1y, b1x, b1y, a2x, a2y, b2x, b2y), 1)

    if t == "midpoint":
        lax, lay, lbx, lby = segment(c["line"])
        px_i, py_i = point(c["point"])
        return _Row(owner, lambda x: [x[px_i] - (x[lax] + x[lbx]) / 2.0,
                                      x[py_i] - (x[lay] + x[lby]) / 2.0],
                    (lax, lay, lbx, lby, px_i, py_i), 2)

    if t == "equal_radius":
        first, cols_a = radius(c["a"])
        second, cols_b = radius(c["b"])
        return _Row(owner, lambda x: [first(x) - second(x)], (*cols_a, *cols_b), 1)

    if t == "point_on_circle":
        key = c.get("circle") or c.get("arc")
        cx, cy = centre(key)
        measure, cols_r = radius(key)
        px_i, py_i = point(c["point"])
        return _Row(owner,
                    lambda x: [hypot(x[px_i] - x[cx], x[py_i] - x[cy]) - measure(x)],
                    (cx, cy, px_i, py_i, *cols_r), 1)

    raise SketchError(f"unknown constraint: {t}")


def _tangent_row(owner, c, point, segment, centre, radius, offset_from_line, lines) -> _Row:
    """Tangency, line-to-curve or curve-to-curve.

    Curve-to-curve has **two** answers — the circles can touch outside each other
    or one can sit inside the other — and no residual can mean both. External is
    the default because it is what a rounded slot, a dogbone and a belt path all
    want; pass ``"internal": true`` for the other. The sign of the residual then
    says which side of the chosen tangency the current configuration is on.
    """
    line_id = c.get("line")
    curves = [k for k in (c.get("arc"), c.get("circle"), c.get("a"), c.get("b"),
                          c.get("arc1"), c.get("arc2"), c.get("curve1"), c.get("curve2"))
              if k is not None]

    if line_id is not None:
        if line_id not in lines:
            raise SketchError(f"tangent references unknown line: {line_id}")
        if not curves:
            raise SketchError("tangent needs a line and a circle/arc, or two of them")
        cx, cy = centre(curves[0])
        measure, cols_r = radius(curves[0])
        distance, cols_d = offset_from_line(cx, cy, line_id)
        # |perpendicular distance| - r: the line grazes the circle from either
        # side, and which side is not something tangency has an opinion about.
        return _Row(owner, lambda x: [abs(distance(x)) - measure(x)],
                    (*cols_d, *cols_r), 1)

    if len(curves) < 2:
        raise SketchError("tangent needs a line and a circle/arc, or two of them")
    ax, ay = centre(curves[0])
    bx, by = centre(curves[1])
    first, cols_a = radius(curves[0])
    second, cols_b = radius(curves[1])
    internal = bool(c.get("internal", False))

    def touching(x):
        span = hypot(x[ax] - x[bx], x[ay] - x[by])
        ra, rb = first(x), second(x)
        return [span - (abs(ra - rb) if internal else ra + rb)]

    return _Row(owner, touching, (ax, ay, bx, by, *cols_a, *cols_b), 1)


def _rows_at(model: _Model, x) -> List[Tuple[Any, List[float]]]:
    """``(owner, values)`` for every row, which is what the diagnosis reads."""
    return [(row.owner, row.evaluate(x)) for row in model.rows]


def _residuals(x, model: _Model) -> np.ndarray:
    values = np.empty(model.n_res, dtype=float)
    xs = x.tolist() if isinstance(x, np.ndarray) else x
    for row, (lo, hi) in zip(model.rows, model.spans):
        values[lo:hi] = row.evaluate(xs)
    return values


# ------------------------------------------------------------- diagnosis --

_TOLERANCE = 1e-6          # a residual this small is a satisfied constraint
_RANK_TOLERANCE = 1e-7     # relative to the largest singular value

# How large a sketch this will accept.
#
# The residual evaluation and the Jacobian are both linear in the sketch, but
# the least-squares solve and the rank are not: the rank comes from an SVD
# costing O(rows^2 x variables), and `_redundancy_groups` asks for the full left
# basis, which allocates rows x rows. The operation surface is reachable by
# anyone who can open a document and `routers/operations` allows 300 calls a
# minute, so this is a denial-of-service bound and not a style preference.
#
# **Measured, on the machine this was written on.** A polyline chain, one
# `distance` and one `horizontal` per segment (the ordinary shape), against the
# same chain with an `equal_length` added per segment (the expensive shape — it
# reads four points, so its Jacobian rows are denser):
#
#     entities  constraints   ordinary   expensive
#          601          601      0.41 s      2.24 s   (900 constraints)
#         1201         1201      2.04 s          --
#         1001         1500          --      7.06 s
#         1801         1801      5.66 s          --
#         1501         2250          --     22.65 s
#
# Two things follow, and the second is why this is not simply a bigger number
# than it was. **Cost tracks entities x constraints, not either alone**: a
# 1500-entity traced outline carrying 300 constraints is cheap, and refusing it
# because of an entity count tuned for a heavily-dimensioned sketch was the
# actual limitation. And **the old 600/600 pair was about right for the
# expensive shape** — 2.24 s at that corner — so raising both flatly would have
# multiplied the worst case rather than moved the ceiling.
#
# So the bound is on the product, with generous per-axis caps to stop either
# running away on its own.
MAX_ENTITIES = 2000
MAX_CONSTRAINTS = 2000

#: `entities x constraints`. 600 x 900 measured 2.24 s and 47 MB on the
#: expensive shape, which is the most one request may cost when three hundred a
#: minute are allowed.
MAX_SOLVE_WORK = 600_000

#: Past half the budget a sketch is in territory where a solve is seconds rather
#: than milliseconds. Warned about rather than refused: it is legitimate work,
#: and the caller is the one who can decide to split it.
WARN_SOLVE_WORK = MAX_SOLVE_WORK // 2


def sketch_size(spec: dict) -> Tuple[int, int]:
    """``(entities, constraints)`` — what the limits below are counted against."""
    entities = sum(len(spec.get(kind) or ())
                   for kind in ("points", "lines", "arcs", "circles"))
    return entities, len(spec.get("constraints") or ())


def solve_work(spec: dict) -> int:
    """`entities x constraints` — what the solve actually costs. See the caps."""
    entities, constraints = sketch_size(spec)
    return entities * constraints


def size_warning(spec: dict) -> Optional[str]:
    """A note for a sketch heading towards the ceiling, or None.

    Separate from `check_size` because it is not a refusal: a big sketch is
    legitimate work, and the caller is the one who can decide to split it.
    """
    work = solve_work(spec)
    if work <= WARN_SOLVE_WORK:
        return None
    entities, constraints = sketch_size(spec)
    return (f"this sketch is {entities} entities x {constraints} constraints, "
            f"{work * 100 // MAX_SOLVE_WORK}% of what the solver will take — "
            f"expect the solve to cost seconds rather than milliseconds, and "
            f"expect a refusal past {MAX_SOLVE_WORK // max(constraints, 1)} "
            f"entities at this constraint count")


def check_size(spec: dict) -> None:
    """Refuse a sketch too expensive to solve, by name and with the number.

    The bound is on `entities x constraints` rather than on either alone,
    because that is what the cost tracks: a 1500-entity traced outline carrying
    300 constraints is cheap, and the old per-axis cap refused it on the
    strength of a number tuned for a heavily-dimensioned sketch.
    """
    entities, constraints = sketch_size(spec)
    if entities > MAX_ENTITIES:
        raise SketchError(
            f"sketch has {entities} entities, more than the {MAX_ENTITIES} this "
            "solver will diagnose — split it across several sketches")
    if constraints > MAX_CONSTRAINTS:
        raise SketchError(
            f"sketch has {constraints} constraints, more than the "
            f"{MAX_CONSTRAINTS} this solver will diagnose")
    work = entities * constraints
    if work > MAX_SOLVE_WORK:
        allowed = MAX_SOLVE_WORK // max(constraints, 1)
        raise SketchError(
            f"sketch is {entities} entities x {constraints} constraints, which "
            f"is {work // 1000}k units of solver work against a {MAX_SOLVE_WORK // 1000}k "
            f"budget — the cost grows with the product, not with either alone. "
            f"At {constraints} constraints this solver will take {allowed} "
            f"entities; drop constraints or split it across several sketches")


def _sketch_scale(x: np.ndarray) -> float:
    """A length that means something for this sketch, for scaling the diagnosis.

    The kernel works in metres and the canvas works in millimetres, and the same
    sketch drawn in either must diagnose the same — so nothing here may compare
    against an absolute length.
    """
    if x.size == 0:
        return 1.0
    magnitude = float(np.max(np.abs(x)))
    return magnitude if magnitude > 1e-12 else 1.0


def _jacobian(model: _Model, x, step: float) -> np.ndarray:
    """Central differences at the solution, evaluated only where they can be nonzero.

    Two things are load-bearing. **Central** rather than forward differences:
    `least_squares` hands back a forward-difference Jacobian computed with its
    own step, accurate to about 1e-8 relative, which is close enough to the rank
    tolerance below to make the diagnosis a coin toss; two evaluations per
    variable buys three more digits and a stable answer. And **sparse**: a
    constraint reads four or six coordinates, so perturbing one variable can only
    move the handful of rows that read it. Re-evaluating every row per column
    made the cost quadratic in the sketch size, which is how a few hundred
    entities turned into fifteen seconds.
    """
    xs = list(x)
    jac = np.zeros((model.n_res, model.n_vars), dtype=float)
    inverse = 1.0 / (2.0 * step)
    for column, affected in enumerate(model.by_column):
        if not affected:
            continue
        original = xs[column]
        xs[column] = original + step
        up = [model.rows[i].evaluate(xs) for i in affected]
        xs[column] = original - step
        down = [model.rows[i].evaluate(xs) for i in affected]
        xs[column] = original
        for i, high, low in zip(affected, up, down):
            lo, hi = model.spans[i]
            for k in range(hi - lo):
                jac[lo + k, column] = (high[k] - low[k]) * inverse
    return jac


def _row_normalised(jac: np.ndarray) -> np.ndarray:
    """Scale every row to unit length, leaving all-zero rows alone.

    Residual rows are not commensurate — `parallel` is a cross product (length
    squared), `distance` is a length, `angle` is radians — so a metre-to-
    millimetre rescale moves them by different factors and a rank tolerance read
    off the raw matrix would change its mind. Per-row normalisation removes
    exactly that, and cannot change the row space.
    """
    norms = np.linalg.norm(jac, axis=1)
    safe = np.where(norms > 0, norms, 1.0)
    return jac / safe[:, None]


def _label(spec: dict, owner) -> str:
    kind, key = owner
    if kind == "arc":
        return f"arc {key} (implicit: stays circular)"
    constraint = spec.get("constraints", [])[key]
    named = constraint.get("id")
    parts = [str(constraint.get("type", "?"))]
    for field in ("point", "a", "b", "line", "line1", "line2", "circle", "arc",
                  "arc1", "arc2"):
        if field in constraint:
            parts.append(f"{field}={constraint[field]}")
    if "value" in constraint:
        parts.append(f"value={constraint['value']}")
    body = " ".join(parts)
    return f"{named}: {body}" if named else f"#{key} {body}"


def _reference(spec: dict, owner) -> Dict[str, Any]:
    kind, key = owner
    if kind == "arc":
        return {"kind": "arc", "arc": key, "label": _label(spec, owner)}
    constraint = spec.get("constraints", [])[key]
    return {"kind": "constraint", "index": key,
            "id": constraint.get("id"), "type": constraint.get("type"),
            "label": _label(spec, owner)}


def diagnose(model: "_Model", x) -> Dict[str, Any]:
    """What the solve actually established, beyond "the residual is small".

    Four states, read off the Jacobian at the solution:

    ``well_constrained``  every variable pinned, every constraint satisfied.
    ``under_constrained`` the sketch can still move — the *normal* state while
                          someone is drawing, reported rather than refused.
    ``over_constrained``  more constraints than independent information, but
                          they agree. Harmless, and worth saying.
    ``conflicting``       constraints that cannot all hold. The only state that
                          should stop an operation.

    ``dof`` is variables minus the Jacobian's rank; ``redundant`` is rows minus
    that rank. A conflict is named by residual, which is exact. A redundancy is
    named by the sketch's left null space, and names a *group*: "these five
    constraints carry four constraints' worth of information" is the true
    statement, and picking one of them as the culprit would be an invention.
    """
    spec = model.spec
    xs = x.tolist() if isinstance(x, np.ndarray) else list(x)
    rows = _rows_at(model, xs)
    scale = _sketch_scale(np.asarray(xs, dtype=float))
    tolerance = _TOLERANCE * scale

    owners = [owner for owner, _ in rows]
    counts = [len(values) for _, values in rows]
    per_owner = [(owner, math.sqrt(sum(v * v for v in values)) if values else 0.0)
                 for owner, values in rows]

    n_vars = model.n_vars
    n_res = model.n_res
    if n_res == 0:
        jac = np.zeros((0, n_vars))
        rank = 0
    else:
        jac = _row_normalised(_jacobian(model, xs, 1e-6 * scale))
        singular = np.linalg.svd(jac, compute_uv=False)
        rank = int(np.sum(singular > (singular[0] if singular.size else 0.0) * _RANK_TOLERANCE))

    dof = max(n_vars - rank, 0)
    redundant = max(n_res - rank, 0)

    conflicts = [_reference(spec, owner) | {"residual": round(value, 12)}
                 for owner, value in sorted(per_owner, key=lambda kv: -kv[1])
                 if value > tolerance]

    redundant_groups: List[List[Dict[str, Any]]] = []
    if redundant and n_res:
        redundant_groups = _redundancy_groups(spec, jac, owners, counts, rank)

    if conflicts:
        status = "conflicting"
    elif dof > 0:
        status = "under_constrained"
    elif redundant:
        status = "over_constrained"
    else:
        status = "well_constrained"

    return {
        "status": status,
        "dof": dof,
        "redundant": redundant,
        "rank": rank,
        "variables": n_vars,
        "equations": n_res,
        "conflicts": conflicts,
        "redundant_groups": redundant_groups,
        "message": _message(status, dof, conflicts, redundant_groups),
    }


def _redundancy_groups(spec: dict, jac: np.ndarray, owners, counts,
                       rank: int) -> List[List[Dict[str, Any]]]:
    """Constraints spanning the Jacobian's left null space, one group per direction."""
    left, singular, _ = np.linalg.svd(jac, full_matrices=True)
    groups: List[List[Dict[str, Any]]] = []
    for k in range(rank, left.shape[1]):
        direction = np.abs(left[:, k])
        peak = float(direction.max()) if direction.size else 0.0
        if peak <= 0:
            continue
        weights: Dict[int, float] = {}
        cursor = 0
        for index, count in enumerate(counts):
            share = float(np.max(direction[cursor:cursor + count])) if count else 0.0
            cursor += count
            if share > peak * 0.1:      # below a tenth of the peak is noise
                weights[index] = share
        members = [_reference(spec, owners[i]) | {"weight": round(weights[i], 6)}
                   for i in sorted(weights, key=lambda i: -weights[i])]
        if len(members) > 1:
            groups.append(members)
    return groups


def _message(status: str, dof: int, conflicts, groups) -> str:
    if status == "conflicting":
        names = ", ".join(c["label"] for c in conflicts[:4])
        return f"these constraints cannot all hold: {names}"
    if status == "under_constrained":
        return f"{dof} degree(s) of freedom left — the sketch can still move"
    if status == "over_constrained":
        if groups:
            names = ", ".join(m["label"] for m in groups[0][:4])
            return f"consistent, but these constraints repeat each other: {names}"
        return "consistent, but some constraints carry no new information"
    return "fully constrained"


def solve_sketch(spec: dict) -> dict:
    """Solve a sketch: solved coordinates, radii, residual, and a diagnosis.

    `converged` and `residual` are kept exactly as they were — things read them —
    and `status`/`dof`/`redundant` sit alongside. `converged` on its own is not
    a success signal: an unconstrained sketch converges trivially.
    """
    points = spec.get("points", [])
    if not points:
        raise SketchError("sketch has no points")
    check_size(spec)

    vi, x0 = _index_variables(points)
    circles = spec.get("circles", [])
    radii_index: Dict[str, int] = {}
    for cir in circles:
        radii_index[cir["id"]] = len(x0)
        x0.append(float(cir.get("radius", 0.01)))

    x0_arr = np.array(x0, dtype=float)
    model = _compile(spec, vi, radii_index, x0_arr.size)
    if model.n_res == 0:
        sol_x = x0_arr
        residual = 0.0
    else:
        # 'lm' is fast/exact but requires residuals >= variables; under-constrained
        # sketches (fewer constraints than DOF) need 'trf', which handles both.
        method = "lm" if model.n_res >= x0_arr.size else "trf"
        # Tighter than the 1e-8 default: these systems are a few dozen unknowns,
        # so the extra iterations cost nothing, and at the default an
        # under-constrained solve stopped around 5e-9 — an order of magnitude
        # from the 1e-6 that `converged` claims and well short of what a tangency
        # needs to be believable.
        result = least_squares(_residuals, x0_arr, args=(model,),
                               method=method, max_nfev=2000,
                               ftol=1e-14, xtol=1e-14, gtol=1e-14)
        sol_x = result.x
        residual = float(np.sqrt(np.mean(result.fun ** 2))) if result.fun.size else 0.0

    solved_points = {p["id"]: [float(sol_x[vi[p["id"]]]), float(sol_x[vi[p["id"]] + 1])] for p in points}
    solved_radii = {cid: float(sol_x[idx]) for cid, idx in radii_index.items()}
    solved = {
        "points": solved_points,
        "radii": solved_radii,
        "residual": round(residual, 9),
        "converged": residual < 1e-6,
    }
    solved.update(diagnose(model, sol_x))
    note = size_warning(spec)
    if note:
        solved["size_note"] = note
    return solved


def _arc_points(
    center: List[float],
    p_from: List[float],
    p_to: List[float],
    ccw: bool,
    segments: int = 24,
) -> List[List[float]]:
    """Tessellate the arc from ``p_from`` to ``p_to`` about ``center``.

    Returns the intermediate points and the endpoint (``p_from`` is assumed to
    already be in the polygon). Radius is interpolated between the two endpoint
    radii so a not-quite-circular solve still produces a smooth curve.
    """
    cx, cy = center
    a0 = math.atan2(p_from[1] - cy, p_from[0] - cx)
    a1 = math.atan2(p_to[1] - cy, p_to[0] - cx)
    r0 = math.hypot(p_from[0] - cx, p_from[1] - cy)
    r1 = math.hypot(p_to[0] - cx, p_to[1] - cy)
    da = a1 - a0
    if ccw:
        while da <= 1e-9:
            da += 2 * math.pi
    else:
        while da >= -1e-9:
            da -= 2 * math.pi
    out: List[List[float]] = []
    for i in range(1, segments + 1):
        t = i / segments
        ang = a0 + da * t
        r = r0 + (r1 - r0) * t
        out.append([cx + r * math.cos(ang), cy + r * math.sin(ang)])
    return out


def _edges_of(spec: dict) -> List[dict]:
    """Lines and arcs as a uniform edge list, in spec order.

    Construction geometry is left out. A centreline, a bolt-circle or a mirror
    axis is there to be constrained *against* — it drives the solve like any
    other entity — but it is not part of the outline, and a profile that
    included it would not close.
    """
    edges: List[dict] = []
    for line in spec.get("lines", []):
        if line.get("construction"):
            continue
        edges.append({"a": line["a"], "b": line["b"], "kind": "line"})
    for arc in spec.get("arcs", []):
        if arc.get("construction"):
            continue
        edges.append({
            "a": arc["a"],
            "b": arc["b"],
            "kind": "arc",
            "center": arc["center"],
            "ccw": bool(arc.get("ccw", True)),
        })
    return edges


def _walk_loop(edges: List[dict], used: List[bool], start_index: int):
    """Chain edges from ``edges[start_index]`` until they close or run out.

    Yields ``(edge, from_pid, to_pid)`` — the traversal direction matters,
    because an arc stored ``b→a`` sweeps the other way round.
    """
    start = edges[start_index]["a"]
    cur = start
    ordered: List[Tuple[dict, str, str]] = []
    for _ in range(len(edges)):
        found = None
        for i, edge in enumerate(edges):
            if used[i]:
                continue
            if edge["a"] == cur:
                found = (i, edge["a"], edge["b"])
                break
            if edge["b"] == cur:
                found = (i, edge["b"], edge["a"])
                break
        if found is None:
            break
        i, frm, to = found
        used[i] = True
        ordered.append((edges[i], frm, to))
        cur = to
        if cur == start:
            break
    return start, ordered


def _loop_points(pts, start: str, ordered, arc_segments: int) -> List[List[float]]:
    poly: List[List[float]] = [list(pts[start])]
    for edge, frm, to in ordered:
        if edge["kind"] == "line":
            poly.append(list(pts[to]))
        else:
            traverse_ccw = edge["ccw"] if frm == edge["a"] else (not edge["ccw"])
            poly.extend(_arc_points(pts[edge["center"]], pts[frm], pts[to],
                                    traverse_ccw, arc_segments))
    if len(poly) > 1 and poly[-1] == poly[0]:
        poly.pop()
    return poly


def sketch_to_polygon(spec: dict, solved: dict, arc_segments: int = 24) -> List[List[float]]:
    """The sketch's first closed loop as a ring of 2D points, arcs tessellated.

    Superseded by :func:`sketch_to_loops`, which keeps curves as curves and
    returns *every* loop. Kept because documents persisted before that carry
    ``{"type": "polygon"}`` profiles and must go on meaning exactly what they
    meant when they were saved — tessellation included.
    """
    edges = _edges_of(spec)
    if not edges:
        raise SketchError("sketch has no lines or arcs to form a profile")
    start, ordered = _walk_loop(edges, [False] * len(edges), 0)
    return _loop_points(solved["points"], start, ordered, arc_segments)


def sketch_to_loops(spec: dict, solved: dict) -> dict:
    """Every closed loop in the sketch, as typed curves rather than points.

    This is the profile the kernels actually want. `sketch_to_polygon` flattened
    a sketch to one ring of points before either kernel saw it, which cost three
    things that matter here: a circle-only sketch had no lines or arcs and so
    could not be extruded at all; an arc became 24 straight segments, so a
    sketched bore arrived as 24 planar faces and was invisible to everything in
    this codebase that counts cylindrical ones (`design_spec._c_hole`,
    `dfm_topology`'s smallest bore, `naming`'s face ids, STEP); and only the
    first loop was walked, so a profile with a hole in it was not expressible.

    Loops are unordered and unnested here — containment is worked out where the
    profile is evaluated, by `cad_kernel.geom.profile2d`, so the SDF and the
    B-rep cannot disagree about which loop is a hole.
    """
    pts = solved["points"]
    radii = solved.get("radii", {})
    loops: List[dict] = []

    edges = _edges_of(spec)
    used = [False] * len(edges)
    for index in range(len(edges)):
        if used[index]:
            continue
        start, ordered = _walk_loop(edges, used, index)
        if not ordered:
            continue
        curves = []
        for edge, frm, to in ordered:
            if edge["kind"] == "line":
                curves.append({"kind": "line", "a": list(pts[frm]), "b": list(pts[to])})
            else:
                curves.append({
                    "kind": "arc", "a": list(pts[frm]), "b": list(pts[to]),
                    "center": list(pts[edge["center"]]),
                    "ccw": edge["ccw"] if frm == edge["a"] else (not edge["ccw"]),
                })
        loops.append({"curves": curves})

    for circle in spec.get("circles", []):
        radius = float(radii.get(circle["id"], circle.get("radius", 0.01)))
        if radius <= 0 or circle.get("construction"):
            continue
        loops.append({"curves": [{"kind": "circle",
                                  "center": list(pts[circle["center"]]),
                                  "r": radius}]})

    if not loops:
        raise SketchError(
            "sketch has nothing closed to extrude — draw a closed chain of "
            "lines/arcs, or a circle")
    return {"type": "loops", "loops": loops}
