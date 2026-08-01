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
from typing import Any, Dict, List, Tuple

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


def _residual_rows(x: np.ndarray, spec: dict, vi: Dict[str, int],
                   radii_index: Dict[str, int]) -> List[Tuple[Any, List[float]]]:
    """``(owner, rows)`` for every constraint, then the implicit per-arc rows.

    Grouped by owner rather than flattened, because the diagnosis has to be able
    to say *which constraint* carries a residual or spans a redundancy, and a
    bare row index cannot.
    """
    def px(pid):
        try:
            return x[vi[pid]]
        except KeyError:
            raise SketchError(f"constraint references unknown point: {pid}")

    def py(pid):
        return x[vi[pid] + 1]

    def line_dir(lid):
        ln = lines.get(lid)
        if ln is None:
            raise SketchError(f"constraint references unknown line: {lid}")
        return px(ln["b"]) - px(ln["a"]), py(ln["b"]) - py(ln["a"])

    def centre_pid(eid):
        """The centre point of a circle or an arc, whichever ``eid`` names."""
        entity = circles.get(eid) or arcs.get(eid)
        if entity is None:
            raise SketchError(f"constraint references unknown circle or arc: {eid}")
        return entity["center"]

    def radius_of(eid):
        """A circle's radius variable, or an arc's radius measured from its ends."""
        if eid in radii_index:
            return x[radii_index[eid]]
        arc = arcs.get(eid)
        if arc is None:
            raise SketchError(f"constraint references unknown circle or arc: {eid}")
        cx, cy = px(arc["center"]), py(arc["center"])
        return (np.hypot(px(arc["a"]) - cx, py(arc["a"]) - cy)
                + np.hypot(px(arc["b"]) - cx, py(arc["b"]) - cy)) / 2.0

    def point_line_distance(pid, lid):
        """Signed perpendicular distance from a point to a line's infinite extent."""
        ln = lines[lid]
        ex, ey = px(ln["b"]) - px(ln["a"]), py(ln["b"]) - py(ln["a"])
        wx, wy = px(pid) - px(ln["a"]), py(pid) - py(ln["a"])
        return (ex * wy - ey * wx) / (np.hypot(ex, ey) + 1e-12)

    def authored_point(pid):
        point = authored.get(pid)
        if point is None:
            raise SketchError(f"constraint references unknown point: {pid}")
        return float(point.get("x", 0.0)), float(point.get("y", 0.0))

    lines = {l["id"]: l for l in spec.get("lines", [])}
    arcs = {a["id"]: a for a in spec.get("arcs", [])}
    circles = {c["id"]: c for c in spec.get("circles", [])}
    authored = {p["id"]: p for p in spec.get("points", [])}
    out: List[Tuple[Any, List[float]]] = []

    for index, c in enumerate(spec.get("constraints", [])):
        t = c.get("type")
        if t == "fixed":
            # One row per axis actually given. `x` used to default to the point's
            # *live* coordinate, which makes a residual that is identically zero
            # with a zero derivative: it pinned nothing, yet counted as an
            # equation, so pinning only y reported a spurious redundancy. With
            # neither axis given it falls back to where the point was authored,
            # which is what "fixed" plainly means.
            p = c["point"]
            home_x, home_y = authored_point(p)
            has_axis = ("x" in c) or ("y" in c)
            res = []
            if "x" in c or not has_axis:
                res.append(px(p) - float(c.get("x", home_x)))
            if "y" in c or not has_axis:
                res.append(py(p) - float(c.get("y", home_y)))
        elif t == "coincident":
            res = [px(c["a"]) - px(c["b"]), py(c["a"]) - py(c["b"])]
        elif t == "horizontal":
            res = [line_dir(c["line"])[1]]
        elif t == "vertical":
            res = [line_dir(c["line"])[0]]
        elif t == "distance":
            d = np.hypot(px(c["a"]) - px(c["b"]), py(c["a"]) - py(c["b"]))
            res = [d - float(c["value"])]
        elif t == "parallel":
            d1x, d1y = line_dir(c["line1"])
            d2x, d2y = line_dir(c["line2"])
            res = [d1x * d2y - d1y * d2x]
        elif t == "perpendicular":
            d1x, d1y = line_dir(c["line1"])
            d2x, d2y = line_dir(c["line2"])
            res = [d1x * d2x + d1y * d2y]
        elif t == "equal_length":
            res = [np.hypot(*line_dir(c["line1"])) - np.hypot(*line_dir(c["line2"]))]
        elif t == "radius":
            key = c.get("circle") or c.get("arc")
            res = [radius_of(key) - float(c["value"])]
        elif t == "point_on_line":
            res = [point_line_distance(c["point"], c["line"])]
        elif t == "arc_radius":
            arc = arcs.get(c["arc"])
            if arc is None:
                raise SketchError(f"arc_radius references unknown arc: {c['arc']}")
            res = [np.hypot(px(arc["a"]) - px(arc["center"]),
                            py(arc["a"]) - py(arc["center"])) - float(c["value"])]
        elif t == "tangent":
            res = [_tangent_residual(c, line_dir, px, py, centre_pid, radius_of,
                                     point_line_distance, lines)]
        elif t == "concentric":
            ca, cb = centre_pid(c["a"]), centre_pid(c["b"])
            res = [px(ca) - px(cb), py(ca) - py(cb)]
        elif t == "symmetric":
            # Two halves: the pair's midpoint sits on the mirror line, and the
            # line joining them crosses it square. Either alone is not symmetry.
            lid = c["line"]
            ln = lines.get(lid)
            if ln is None:
                raise SketchError(f"constraint references unknown line: {lid}")
            ex, ey = line_dir(lid)
            length = np.hypot(ex, ey) + 1e-12
            mx = (px(c["a"]) + px(c["b"])) / 2.0
            my = (py(c["a"]) + py(c["b"])) / 2.0
            wx, wy = mx - px(ln["a"]), my - py(ln["a"])
            join_x, join_y = px(c["b"]) - px(c["a"]), py(c["b"]) - py(c["a"])
            res = [(ex * wy - ey * wx) / length,
                   (ex * join_x + ey * join_y) / length]
        elif t == "angle":
            d1x, d1y = line_dir(c["line1"])
            d2x, d2y = line_dir(c["line2"])
            actual = math.atan2(d1x * d2y - d1y * d2x, d1x * d2x + d1y * d2y)
            wanted = math.radians(float(c["value"]))
            # Wrapped into (-pi, pi] so 179 deg and -181 deg are the same ask.
            res = [(actual - wanted + math.pi) % (2 * math.pi) - math.pi]
        elif t == "midpoint":
            ln = lines.get(c["line"])
            if ln is None:
                raise SketchError(f"constraint references unknown line: {c['line']}")
            res = [px(c["point"]) - (px(ln["a"]) + px(ln["b"])) / 2.0,
                   py(c["point"]) - (py(ln["a"]) + py(ln["b"])) / 2.0]
        elif t == "equal_radius":
            res = [radius_of(c["a"]) - radius_of(c["b"])]
        elif t == "point_on_circle":
            key = c.get("circle") or c.get("arc")
            centre = centre_pid(key)
            res = [np.hypot(px(c["point"]) - px(centre), py(c["point"]) - py(centre))
                   - radius_of(key)]
        else:
            raise SketchError(f"unknown constraint: {t}")
        out.append((("constraint", index), [float(v) for v in res]))

    # Implicit per-arc residual: keep the arc circular by forcing both endpoints
    # equidistant from the center.
    for arc_id, arc in arcs.items():
        ra = np.hypot(px(arc["a"]) - px(arc["center"]), py(arc["a"]) - py(arc["center"]))
        rb = np.hypot(px(arc["b"]) - px(arc["center"]), py(arc["b"]) - py(arc["center"]))
        out.append((("arc", arc_id), [float(ra - rb)]))

    return out


def _tangent_residual(c, line_dir, px, py, centre_pid, radius_of,
                      point_line_distance, lines) -> float:
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
        # |perpendicular distance| - r: the line grazes the circle from either
        # side, and which side is not something tangency has an opinion about.
        return float(abs(point_line_distance(centre_pid(curves[0]), line_id))
                     - radius_of(curves[0]))
    if len(curves) < 2:
        raise SketchError("tangent needs a line and a circle/arc, or two of them")
    ca, cb = centre_pid(curves[0]), centre_pid(curves[1])
    span = np.hypot(px(ca) - px(cb), py(ca) - py(cb))
    ra, rb = radius_of(curves[0]), radius_of(curves[1])
    if bool(c.get("internal", False)):
        return float(span - abs(ra - rb))
    return float(span - (ra + rb))


def _residuals(x: np.ndarray, spec: dict, vi: Dict[str, int],
               radii_index: Dict[str, int]) -> np.ndarray:
    rows = _residual_rows(x, spec, vi, radii_index)
    return np.array([v for _, values in rows for v in values], dtype=float)


# ------------------------------------------------------------- diagnosis --

_TOLERANCE = 1e-6          # a residual this small is a satisfied constraint
_RANK_TOLERANCE = 1e-7     # relative to the largest singular value


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


def _jacobian(x: np.ndarray, spec: dict, vi, radii_index, step: float) -> np.ndarray:
    """Central differences at the solution.

    `least_squares` hands back a forward-difference Jacobian computed with its
    own step, which is accurate to about 1e-8 relative — close enough to the rank
    tolerance below to make the diagnosis a coin toss. Two extra evaluations per
    variable buys three more digits and a stable answer.
    """
    n = x.size
    base = _residuals(x, spec, vi, radii_index)
    jac = np.zeros((base.size, n), dtype=float)
    for j in range(n):
        up, down = x.copy(), x.copy()
        up[j] += step
        down[j] -= step
        jac[:, j] = (_residuals(up, spec, vi, radii_index)
                     - _residuals(down, spec, vi, radii_index)) / (2.0 * step)
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


def diagnose(spec: dict, x: np.ndarray, vi, radii_index) -> Dict[str, Any]:
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
    rows = _residual_rows(x, spec, vi, radii_index)
    scale = _sketch_scale(x)
    tolerance = _TOLERANCE * scale

    owners = [owner for owner, _ in rows]
    counts = [len(values) for _, values in rows]
    flat = np.array([v for _, values in rows for v in values], dtype=float)

    per_owner: List[Tuple[Any, float]] = []
    cursor = 0
    for owner, count in zip(owners, counts):
        chunk = flat[cursor:cursor + count]
        per_owner.append((owner, float(np.linalg.norm(chunk)) if count else 0.0))
        cursor += count

    n_vars = int(x.size)
    if flat.size == 0:
        jac = np.zeros((0, n_vars))
        rank = 0
    else:
        jac = _row_normalised(_jacobian(x, spec, vi, radii_index, 1e-6 * scale))
        singular = np.linalg.svd(jac, compute_uv=False)
        rank = int(np.sum(singular > (singular[0] if singular.size else 0.0) * _RANK_TOLERANCE))

    dof = max(n_vars - rank, 0)
    redundant = max(flat.size - rank, 0)

    conflicts = [_reference(spec, owner) | {"residual": round(value, 12)}
                 for owner, value in sorted(per_owner, key=lambda kv: -kv[1])
                 if value > tolerance]

    redundant_groups: List[List[Dict[str, Any]]] = []
    if redundant and flat.size:
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
        "equations": int(flat.size),
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

    vi, x0 = _index_variables(points)
    circles = spec.get("circles", [])
    radii_index: Dict[str, int] = {}
    for cir in circles:
        radii_index[cir["id"]] = len(x0)
        x0.append(float(cir.get("radius", 0.01)))

    x0_arr = np.array(x0, dtype=float)
    n_res = _residuals(x0_arr, spec, vi, radii_index).size
    if n_res == 0:
        sol_x = x0_arr
        residual = 0.0
    else:
        # 'lm' is fast/exact but requires residuals >= variables; under-constrained
        # sketches (fewer constraints than DOF) need 'trf', which handles both.
        method = "lm" if n_res >= x0_arr.size else "trf"
        # Tighter than the 1e-8 default: these systems are a few dozen unknowns,
        # so the extra iterations cost nothing, and at the default an
        # under-constrained solve stopped around 5e-9 — an order of magnitude
        # from the 1e-6 that `converged` claims and well short of what a tangency
        # needs to be believable.
        result = least_squares(_residuals, x0_arr, args=(spec, vi, radii_index),
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
    solved.update(diagnose(spec, sol_x, vi, radii_index))
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
