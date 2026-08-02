"""What a sketch solve establishes, beyond "the residual is small".

`converged` was `residual < 1e-6`, which an unconstrained sketch satisfies
trivially: the solver reported success for the most common failure. That is the
same class of bug as the hardcoded `ok: True` the operation envelope was built
to kill, and the one a model driving this action space would learn to exploit
fastest — so the tests here are mostly about sketches whose answer is *known* by
counting, not by measuring.
"""

import math

import pytest

from app.services import cad_operations as ops
from app.services import document_service as docs
from app.services import sketch_solver

# A rectangle drawn slightly wonky, so the solver has something to do.
POINTS = [{"id": "p1", "x": 0.0, "y": 0.0}, {"id": "p2", "x": 0.06, "y": 0.001},
          {"id": "p3", "x": 0.061, "y": 0.04}, {"id": "p4", "x": -0.001, "y": 0.039}]
LINES = [{"id": "l1", "a": "p1", "b": "p2"}, {"id": "l2", "a": "p2", "b": "p3"},
         {"id": "l3", "a": "p3", "b": "p4"}, {"id": "l4", "a": "p4", "b": "p1"}]
SHAPED = [{"type": "horizontal", "line": "l1"}, {"type": "horizontal", "line": "l3"},
          {"type": "vertical", "line": "l2"}, {"type": "vertical", "line": "l4"},
          {"type": "distance", "a": "p1", "b": "p2", "value": 0.06},
          {"type": "distance", "a": "p2", "b": "p3", "value": 0.04}]
ANCHOR = {"type": "fixed", "point": "p1", "x": 0.0, "y": 0.0}
DIAGONAL = math.hypot(0.06, 0.04)


def _rect(constraints, scale: float = 1.0) -> dict:
    def scaled(value):
        return value * scale

    points = [{"id": p["id"], "x": scaled(p["x"]), "y": scaled(p["y"])} for p in POINTS]
    out = []
    for c in constraints:
        c = dict(c)
        for key in ("value", "x", "y"):
            if key in c:
                c[key] = scaled(c[key])
        out.append(c)
    return {"points": points, "lines": LINES, "constraints": out}


def _solve(constraints, scale: float = 1.0) -> dict:
    return sketch_solver.solve_sketch(_rect(constraints, scale))


# ------------------------------------------------------------------ status --

def test_a_fully_dimensioned_rectangle_is_well_constrained():
    result = _solve(SHAPED + [ANCHOR])
    assert result["status"] == "well_constrained"
    assert result["dof"] == 0
    assert result["redundant"] == 0
    assert result["conflicts"] == []


def test_the_same_rectangle_unanchored_has_two_degrees_of_freedom():
    """It can still slide; the four h/v constraints already pinned its rotation."""
    result = _solve(SHAPED)
    assert result["status"] == "under_constrained"
    assert result["dof"] == 2


def test_an_unconstrained_sketch_is_not_reported_as_a_success():
    """The bug this sprint exists for: converged was True and said nothing."""
    result = _solve([])
    assert result["converged"] is True          # still true, still meaningless
    assert result["status"] == "under_constrained"
    assert result["dof"] == 8                   # four points, free


def test_dimensioning_both_diagonals_is_over_constrained_and_names_them():
    result = _solve(SHAPED + [ANCHOR,
                              {"type": "distance", "id": "diag1", "a": "p1", "b": "p3",
                               "value": DIAGONAL},
                              {"type": "distance", "id": "diag2", "a": "p2", "b": "p4",
                               "value": DIAGONAL}])
    assert result["status"] == "over_constrained"
    assert result["dof"] == 0
    assert result["redundant"] == 2
    assert result["conflicts"] == [], "consistent redundancy is not a conflict"
    named = {member["id"] for group in result["redundant_groups"] for member in group}
    assert {"diag1", "diag2"} <= named
    # Redundancy is a property of a set, so the group names the whole cycle —
    # but the constraint that made it redundant leads it.
    assert result["redundant_groups"][0][0]["id"] == "diag1"


def test_two_conflicting_dimensions_are_named():
    result = _solve(SHAPED + [ANCHOR,
                              {"type": "distance", "id": "clash", "a": "p1", "b": "p2",
                               "value": 0.08}])
    assert result["status"] == "conflicting"
    types = [c["id"] or c["index"] for c in result["conflicts"]]
    assert "clash" in types
    assert len(result["conflicts"]) == 2, "both halves of the disagreement"
    assert "cannot all hold" in result["message"]


@pytest.mark.parametrize("scale", [1.0, 1000.0, 0.001], ids=["m", "mm", "um"])
def test_the_diagnosis_does_not_depend_on_the_units(scale):
    """The kernel works in metres and the canvas in millimetres."""
    result = _solve(SHAPED + [ANCHOR, {"type": "distance", "a": "p1", "b": "p3",
                                       "value": DIAGONAL}], scale=scale)
    assert (result["status"], result["dof"], result["redundant"]) == ("over_constrained", 0, 1)


def test_fixing_one_axis_pins_one_axis_and_counts_as_one_equation():
    """`fixed` used to default the missing axis to the point's *live* coordinate,
    which is a residual that is always zero with a zero derivative: it pinned
    nothing and still counted as an equation."""
    one_axis = _solve(SHAPED + [{"type": "fixed", "point": "p1", "y": 0.0}])
    assert one_axis["dof"] == 1
    assert one_axis["redundant"] == 0

    both = _solve(SHAPED + [{"type": "fixed", "point": "p1"}])
    assert both["dof"] == 0 and both["redundant"] == 0


# ------------------------------------------------------------- constraints --

def test_tangency_holds_a_rounded_slot_together():
    """Two lines and two arcs, tangent at all four joints — the shape tangency
    exists for, and the test that it works rather than merely existing."""
    length, radius = 0.05, 0.01
    spec = {
        "points": [
            {"id": "cl", "x": -length / 2 + 0.001, "y": 0.0004},
            {"id": "cr", "x": length / 2 - 0.0007, "y": -0.0003},
            {"id": "tl", "x": -length / 2, "y": radius + 0.0008},
            {"id": "tr", "x": length / 2, "y": radius - 0.0005},
            {"id": "br", "x": length / 2, "y": -radius + 0.0006},
            {"id": "bl", "x": -length / 2, "y": -radius - 0.0009},
        ],
        "lines": [{"id": "top", "a": "tl", "b": "tr"}, {"id": "bot", "a": "br", "b": "bl"}],
        "arcs": [{"id": "right", "a": "tr", "b": "br", "center": "cr", "ccw": False},
                 {"id": "left", "a": "bl", "b": "tl", "center": "cl", "ccw": False}],
        "constraints": [
            {"type": "fixed", "point": "cl", "x": -length / 2, "y": 0.0},
            {"type": "fixed", "point": "cr", "y": 0.0},
            {"type": "horizontal", "line": "top"}, {"type": "horizontal", "line": "bot"},
            {"type": "distance", "a": "cl", "b": "cr", "value": length},
            {"type": "radius", "arc": "left", "value": radius},
            {"type": "equal_radius", "a": "left", "b": "right"},
            {"type": "tangent", "line": "top", "arc": "left"},
            {"type": "tangent", "line": "top", "arc": "right"},
            {"type": "tangent", "line": "bot", "arc": "left"},
            {"type": "tangent", "line": "bot", "arc": "right"},
        ],
    }
    result = sketch_solver.solve_sketch(spec)
    assert result["residual"] < 1e-9, result["message"]
    assert result["dof"] == 0
    for pid, (_, y) in result["points"].items():
        if pid in ("tl", "tr"):
            assert y == pytest.approx(radius, abs=1e-9)
        if pid in ("bl", "br"):
            assert y == pytest.approx(-radius, abs=1e-9)


def test_arc_to_arc_tangency_defaults_to_external_and_internal_is_asked_for():
    def two_circles(internal):
        return {
            "points": [{"id": "ca", "x": 0.0, "y": 0.0}, {"id": "cb", "x": 0.02, "y": 0.0}],
            "circles": [{"id": "a", "center": "ca", "radius": 0.01},
                        {"id": "b", "center": "cb", "radius": 0.004}],
            "constraints": [
                {"type": "fixed", "point": "ca", "x": 0.0, "y": 0.0},
                {"type": "fixed", "point": "cb", "y": 0.0},
                {"type": "radius", "circle": "a", "value": 0.01},
                {"type": "radius", "circle": "b", "value": 0.004},
                dict({"type": "tangent", "a": "a", "b": "b"},
                     **({"internal": True} if internal else {})),
            ],
        }

    external = sketch_solver.solve_sketch(two_circles(False))
    assert external["residual"] < 1e-9
    assert external["points"]["cb"][0] == pytest.approx(0.014, abs=1e-9)   # 10 + 4

    internal = sketch_solver.solve_sketch(two_circles(True))
    assert internal["residual"] < 1e-9
    assert internal["points"]["cb"][0] == pytest.approx(0.006, abs=1e-9)   # 10 - 4


@pytest.mark.parametrize("constraint,check", [
    ({"type": "concentric", "a": "k1", "b": "k2"},
     lambda r: r["points"]["c2"] == pytest.approx(r["points"]["c1"], abs=1e-9)),
    ({"type": "equal_radius", "a": "k1", "b": "k2"},
     lambda r: r["radii"]["k2"] == pytest.approx(r["radii"]["k1"], rel=1e-9)),
    ({"type": "point_on_circle", "point": "c2", "circle": "k1"},
     lambda r: math.dist(r["points"]["c2"], r["points"]["c1"]) == pytest.approx(0.01, abs=1e-9)),
], ids=["concentric", "equal_radius", "point_on_circle"])
def test_the_circle_constraints(constraint, check):
    spec = {
        "points": [{"id": "c1", "x": 0.0, "y": 0.0}, {"id": "c2", "x": 0.003, "y": 0.002}],
        "circles": [{"id": "k1", "center": "c1", "radius": 0.01},
                    {"id": "k2", "center": "c2", "radius": 0.004}],
        "constraints": [{"type": "fixed", "point": "c1", "x": 0.0, "y": 0.0},
                        {"type": "radius", "circle": "k1", "value": 0.01},
                        constraint],
    }
    result = sketch_solver.solve_sketch(spec)
    assert result["residual"] < 1e-9, result["message"]
    assert check(result)


def test_angle_between_two_lines():
    spec = {
        "points": [{"id": "o", "x": 0.0, "y": 0.0}, {"id": "a", "x": 0.05, "y": 0.0},
                   {"id": "b", "x": 0.04, "y": 0.01}],
        "lines": [{"id": "la", "a": "o", "b": "a"}, {"id": "lb", "a": "o", "b": "b"}],
        "constraints": [{"type": "fixed", "point": "o", "x": 0.0, "y": 0.0},
                        {"type": "horizontal", "line": "la"},
                        {"type": "distance", "a": "o", "b": "a", "value": 0.05},
                        {"type": "distance", "a": "o", "b": "b", "value": 0.05},
                        {"type": "angle", "line1": "la", "line2": "lb", "value": 30.0}],
    }
    result = sketch_solver.solve_sketch(spec)
    assert result["residual"] < 1e-9, result["message"]
    bx, by = result["points"]["b"]
    assert math.degrees(math.atan2(by, bx)) == pytest.approx(30.0, abs=1e-6)


def test_midpoint_and_symmetric():
    spec = {
        "points": [{"id": "p1", "x": 0.0, "y": 0.0}, {"id": "p2", "x": 0.06, "y": 0.0},
                   {"id": "m", "x": 0.02, "y": 0.001},
                   {"id": "s1", "x": 0.01, "y": 0.02}, {"id": "s2", "x": 0.048, "y": -0.017}],
        "lines": [{"id": "base", "a": "p1", "b": "p2"},
                  {"id": "axis", "a": "m", "b": "top"}],
        "constraints": [],
    }
    spec["points"].append({"id": "top", "x": 0.03, "y": 0.05})
    spec["constraints"] = [
        {"type": "fixed", "point": "p1", "x": 0.0, "y": 0.0},
        {"type": "fixed", "point": "p2", "x": 0.06, "y": 0.0},
        {"type": "midpoint", "point": "m", "line": "base"},
        {"type": "vertical", "line": "axis"},
        {"type": "fixed", "point": "top", "y": 0.05},
        {"type": "symmetric", "a": "s1", "b": "s2", "line": "axis"},
        {"type": "fixed", "point": "s1", "x": 0.01, "y": 0.02},
    ]
    result = sketch_solver.solve_sketch(spec)
    assert result["residual"] < 1e-9, result["message"]
    assert result["points"]["m"] == pytest.approx([0.03, 0.0], abs=1e-9)
    # Mirrored about x = 30 mm, at the same height.
    assert result["points"]["s2"] == pytest.approx([0.05, 0.02], abs=1e-9)


def test_a_constraint_naming_something_that_is_not_there_says_so():
    with pytest.raises(sketch_solver.SketchError, match="unknown line"):
        sketch_solver.solve_sketch({"points": [{"id": "p", "x": 0.0, "y": 0.0}],
                                    "constraints": [{"type": "horizontal", "line": "nope"}]})
    with pytest.raises(sketch_solver.SketchError, match="unknown constraint"):
        sketch_solver.solve_sketch({"points": [{"id": "p", "x": 0.0, "y": 0.0}],
                                    "constraints": [{"type": "looks_right"}]})


# ------------------------------------------------------- the operation surface --

def _doc():
    return docs.create_document(name="diagnosis").id


def test_add_sketch_reports_the_status_and_keeps_the_old_fields():
    result = ops.execute("add_sketch", _doc(), {"sketch": _rect(SHAPED + [ANCHOR]),
                                                "depth": 0.01})
    assert result["ok"], result["error"]
    payload = result["result"]
    assert payload["status"] == "well_constrained" and payload["dof"] == 0
    assert payload["converged"] is True and payload["residual"] == 0.0


def test_an_under_constrained_sketch_is_still_built():
    """Refusing one would make the canvas unusable — it is a reported state."""
    result = ops.execute("add_sketch", _doc(), {"sketch": _rect(SHAPED), "depth": 0.01})
    assert result["ok"], result["error"]
    assert result["result"]["status"] == "under_constrained"


def test_a_conflicting_sketch_is_refused_by_name_rather_than_built_wrong():
    result = ops.execute("add_sketch", _doc(), {
        "sketch": _rect(SHAPED + [ANCHOR, {"type": "distance", "id": "clash",
                                           "a": "p1", "b": "p2", "value": 0.08}]),
        "depth": 0.01})
    assert not result["ok"]
    assert result["error"]["code"] == "BAD_ARG"
    assert "clash" in result["error"]["message"]


def test_check_sketch_reads_a_saved_sketch_without_changing_it():
    doc_id = _doc()
    added = ops.execute("add_sketch", doc_id, {"sketch": _rect(SHAPED), "depth": 0.01})
    checked = ops.execute("check_sketch", doc_id, {"feature_id": added["result"]["feature_id"]})
    assert checked["ok"], checked["error"]
    assert checked["result"]["status"] == "under_constrained"
    assert checked["delta"]["changed_geometry"] is False


def test_check_sketch_on_a_feature_with_no_sketch_is_a_precondition():
    doc_id = _doc()
    box = ops.execute("add_primitive", doc_id, {"shape": "box"})
    result = ops.execute("check_sketch", doc_id, {"feature_id": box["result"]["feature_id"]})
    assert not result["ok"]
    assert result["error"]["code"] == "PRECONDITION"


# --------------------------------------------------- the compiled model --

def _dense_jacobian(model, x, step):
    """Every column re-evaluated in full, ignoring the sparsity map."""
    import numpy as np

    xs = list(x)
    out = np.zeros((model.n_res, model.n_vars))
    for column in range(model.n_vars):
        original = xs[column]
        xs[column] = original + step
        up = sketch_solver._residuals(xs, model).copy()
        xs[column] = original - step
        down = sketch_solver._residuals(xs, model).copy()
        xs[column] = original
        out[:, column] = (up - down) / (2 * step)
    return out


EVERY_CONSTRAINT = {
    "points": [{"id": "o", "x": 0.0, "y": 0.0}, {"id": "a", "x": 0.05, "y": 0.002},
               {"id": "b", "x": 0.048, "y": 0.031}, {"id": "m", "x": 0.026, "y": 0.001},
               {"id": "s1", "x": 0.01, "y": 0.02}, {"id": "s2", "x": 0.041, "y": -0.018},
               {"id": "c1", "x": 0.02, "y": -0.03}, {"id": "c2", "x": 0.033, "y": -0.028},
               {"id": "ap", "x": 0.06, "y": 0.01}, {"id": "bp", "x": 0.06, "y": 0.03},
               {"id": "ac", "x": 0.052, "y": 0.02}, {"id": "free", "x": 0.07, "y": 0.05}],
    "lines": [{"id": "l1", "a": "o", "b": "a"}, {"id": "l2", "a": "a", "b": "b"},
              {"id": "l3", "a": "o", "b": "b"}],
    "arcs": [{"id": "arc1", "a": "ap", "b": "bp", "center": "ac", "ccw": True}],
    "circles": [{"id": "k1", "center": "c1", "radius": 0.006},
                {"id": "k2", "center": "c2", "radius": 0.004}],
    "constraints": [
        {"type": "fixed", "point": "o", "x": 0.0, "y": 0.0},
        {"type": "fixed", "point": "free", "y": 0.05},
        {"type": "coincident", "a": "s1", "b": "s2"},
        {"type": "horizontal", "line": "l1"},
        {"type": "vertical", "line": "l2"},
        {"type": "distance", "a": "o", "b": "a", "value": 0.05},
        {"type": "parallel", "line1": "l1", "line2": "l3"},
        {"type": "perpendicular", "line1": "l1", "line2": "l2"},
        {"type": "equal_length", "line1": "l1", "line2": "l3"},
        {"type": "radius", "circle": "k1", "value": 0.006},
        {"type": "point_on_line", "point": "m", "line": "l1"},
        {"type": "arc_radius", "arc": "arc1", "value": 0.01},
        {"type": "tangent", "line": "l1", "circle": "k1"},
        {"type": "tangent", "a": "k1", "b": "k2"},
        {"type": "tangent", "a": "k1", "b": "k2", "internal": True},
        {"type": "concentric", "a": "k1", "b": "k2"},
        {"type": "symmetric", "a": "s1", "b": "s2", "line": "l3"},
        {"type": "angle", "line1": "l1", "line2": "l3", "value": 30.0},
        {"type": "midpoint", "point": "m", "line": "l1"},
        {"type": "equal_radius", "a": "k1", "b": "arc1"},
        {"type": "point_on_circle", "point": "m", "circle": "k2"},
    ],
}


def _model_of(spec):
    import numpy as np

    vi, x0 = sketch_solver._index_variables(spec["points"])
    radii = {}
    for circle in spec.get("circles", []):
        radii[circle["id"]] = len(x0)
        x0.append(float(circle["radius"]))
    x = np.array(x0, dtype=float)
    return sketch_solver._compile(spec, vi, radii, x.size), x


def test_every_constraint_type_declares_the_columns_it_reads():
    """The sparse Jacobian trusts each row's declared `columns`. A missing entry
    is a silently wrong rank — and therefore a silently wrong dof — so the
    sparse matrix is compared against one built the slow, unconditional way."""
    import numpy as np

    model, x = _model_of(EVERY_CONSTRAINT)
    # Every constraint type is exercised, or this test is not saying much.
    used = {c["type"] for c in EVERY_CONSTRAINT["constraints"]}
    assert len(used) == 18, sorted(used)

    step = 1e-6 * float(np.max(np.abs(x)))
    sparse = sketch_solver._jacobian(model, x, step)
    dense = _dense_jacobian(model, x, step)
    assert np.allclose(sparse, dense, atol=1e-9), np.abs(sparse - dense).max()


def test_the_model_is_built_once_not_per_evaluation():
    """The tables used to be rebuilt inside every residual evaluation, which was
    91% of a solve. This pins the structure that fixed it."""
    model, x = _model_of(EVERY_CONSTRAINT)
    assert len(model.rows) == len(EVERY_CONSTRAINT["constraints"]) + 1  # + implicit arc
    assert model.n_res == sum(row.width for row in model.rows)
    # Nothing reads a column that does not exist.
    for row in model.rows:
        assert all(0 <= column < model.n_vars for column in row.columns)


# --------------------------------------------------------- the size bound --

def _ring(n):
    points = [{"id": f"p{i}", "x": 0.05 * math.cos(2 * math.pi * i / n),
               "y": 0.05 * math.sin(2 * math.pi * i / n)} for i in range(n)]
    lines = [{"id": f"l{i}", "a": f"p{i}", "b": f"p{(i + 1) % n}"} for i in range(n)]
    return {"points": points, "lines": lines, "constraints": []}


def test_an_oversized_sketch_is_refused_by_name_and_number():
    """Unbounded, the rank SVD costs O(rows^2 x variables) and allocates
    rows x rows — one request could spend minutes and hundreds of megabytes on
    a surface anyone who can open a document can reach."""
    spec = _ring(sketch_solver.MAX_ENTITIES)      # 2N entities: over the line
    with pytest.raises(sketch_solver.SketchError, match=r"more than the \d+"):
        sketch_solver.solve_sketch(spec)

    entities, constraints = sketch_solver.sketch_size(spec)
    assert entities == 2 * sketch_solver.MAX_ENTITIES and constraints == 0


def test_too_many_constraints_is_refused_separately():
    spec = _ring(4)
    spec["constraints"] = [{"type": "horizontal", "line": "l0"}] * (
        sketch_solver.MAX_CONSTRAINTS + 1)
    with pytest.raises(sketch_solver.SketchError, match="constraints"):
        sketch_solver.solve_sketch(spec)


def test_a_sketch_at_the_limit_still_solves():
    """The bound has to leave room for a real generated sketch."""
    spec = _ring(sketch_solver.MAX_ENTITIES // 2)
    spec["constraints"] = [{"type": "fixed", "point": "p0", "x": 0.05, "y": 0.0}]
    result = sketch_solver.solve_sketch(spec)
    assert result["status"] == "under_constrained"


def test_the_operation_surface_reports_the_bound_as_a_typed_failure():
    result = ops.execute("add_sketch", _doc(), {"sketch": _ring(500), "depth": 0.01})
    assert not result["ok"]
    assert result["error"]["code"] == "BAD_ARG"
    assert "entities" in result["error"]["message"]
