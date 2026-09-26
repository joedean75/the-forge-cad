"""The CadQuery interpreter, which exists because the chain reader saw 9.4%.

`external.convert_cadquery` read a program as one linear run of method calls. The
calls it stopped at were almost all ones this catalog has — `union` blocked 58.6%
of Zero-To-CAD's test programs, `center` 56.2% — because a program that assigns
intermediate solids and unions them is not a chain. Measured on the same 10,000:
interpreter 10.6%, chain reader 9.4%, both together 18.7%, because they fail on
different programs.

What is pinned here is the state the chain reader lacked: variables, arithmetic,
placement, and a running extent for `faces` to resolve against.
"""

import pytest

from datasets import cadquery_interp as ci

MM = 1000.0


def _ops(source: str):
    return ci.interpret(source)


def test_a_variable_holding_a_solid_is_a_solid():
    """`solid = …` then `solid.union(…)` — not a chain, and 58.6% of programs."""
    ops = _ops("""
import cadquery as cq
plate = cq.Workplane("XY").box(40, 30, 6)
boss = cq.Workplane("XY").cylinder(10, 5)
result = plate.union(boss)
""")
    assert [o["op"] for o in ops] == ["add_primitive", "add_primitive"]
    assert ops[0]["args"]["params"]["size"] == {"x": 0.04, "y": 0.03, "z": 0.006}, \
        "millimetres in the program, metres in the operations"


def test_a_cut_re_aims_the_tool_rather_than_un_unioning_it():
    ops = _ops("""
import cadquery as cq
block = cq.Workplane("XY").box(40, 30, 10)
pocket = cq.Workplane("XY").box(10, 10, 20)
result = block.cut(pocket)
""")
    assert ops[1]["args"]["operation"] == "subtract"


def test_a_composed_tool_is_refused_rather_than_half_subtracted():
    """Subtracting an extrude-with-a-hole needs the tool built in a group first,
    which `target` supports and this does not do yet. Refused and counted."""
    with pytest.raises(ci.Unsupported) as caught:
        _ops("""
import cadquery as cq
block = cq.Workplane("XY").box(40, 30, 10)
tool = cq.Workplane("XY").box(10, 10, 20).faces(">Z").hole(2)
result = block.cut(tool)
""")
    assert caught.value.cause == "cut:composed_tool"


def test_a_face_selector_resolves_against_the_running_extent():
    """`.faces('>Z')` is the commonest idiom in the dataset and the reason the
    chain reader could place nothing. The extent has to be tracked through an
    *extrude* too — growing a throwaway clone's box instead of the real one made
    this report `faces:no_body_yet` for 15% of programs."""
    ops = _ops("""
import cadquery as cq
result = cq.Workplane("XY").rect(40, 30).extrude(8).faces(">Z").hole(5)
""")
    assert [o["op"] for o in ops] == ["add_extrude", "add_hole"]
    # The extrusion runs 0..8 mm, so its top face is at 8 mm and the hole's
    # centre sits there rather than at the origin.
    assert ops[1]["args"]["center"]["z"] == pytest.approx(0.008)


@pytest.mark.parametrize("selector", [">Z[1]", "|Z", ">>Z", "%Plane"])
def test_a_selector_this_cannot_resolve_is_refused(selector):
    with pytest.raises(ci.Unsupported) as caught:
        _ops(f'''
import cadquery as cq
result = cq.Workplane("XY").box(10, 10, 10).faces("{selector}").hole(2)
''')
    assert caught.value.cause.startswith("faces:selector")


def test_placement_is_baked_into_the_profile_rather_than_lost():
    """`rect` and `circle` profiles have no centre argument, so an in-plane offset
    would have nowhere to go — and `center`/`moveTo` are exactly that offset. The
    outline is emitted as `loops` in absolute coordinates instead."""
    ops = _ops("""
import cadquery as cq
result = cq.Workplane("XY").center(12, 0).rect(10, 10).extrude(4)
""")
    profile = ops[0]["args"]["profile"]
    assert profile["type"] == "loops"
    xs = [point[0] for curve in profile["loops"][0]["curves"] for point in
          (curve["a"], curve["b"])]
    assert min(xs) == pytest.approx(0.007) and max(xs) == pytest.approx(0.017), \
        "the outline sits where center() put it"


def test_the_programs_own_arithmetic_is_evaluated():
    """653 of the 10,000 were dropped as `unresolved_argument` for want of this,
    and `radians`/`sin` appear in 7.9%."""
    ops = _ops("""
import cadquery as cq
import math
w = 40.0
h = w / 2
lift = 10 * math.sin(math.radians(90))
result = cq.Workplane("XY").box(w, h, lift)
""")
    size = ops[0]["args"]["params"]["size"]
    assert (size["x"], size["y"], size["z"]) == pytest.approx((0.04, 0.02, 0.01))


def test_a_loop_places_repeated_features():
    """The largest single cause at 11.8%: holes on a circle, ribs along a plate."""
    ops = _ops("""
import cadquery as cq
result = cq.Workplane("XY").box(60, 60, 6)
for x in [-20, 0, 20]:
    result = result.faces(">Z").center(x, 0).hole(4)
""")
    holes = [o for o in ops if o["op"] == "add_hole"]
    assert len(holes) == 3
    assert [round(h["args"]["center"]["x"], 4) for h in holes] == [-0.02, 0.0, 0.02]


def test_an_unbounded_loop_is_refused_rather_than_run():
    """A converter reading untrusted programs must not be turned into a
    long-running one by a large `range`."""
    with pytest.raises(ci.Unsupported) as caught:
        _ops("""
import cadquery as cq
result = cq.Workplane("XY").box(10, 10, 10)
for i in range(5000):
    result = result.faces(">Z").hole(1)
""")
    assert caught.value.cause == "for:too_many_iterations"


def test_translate_moves_a_tool_that_is_already_built():
    """`boss = …cylinder(…).translate((10, 0, 0))` then `union` is the ordinary
    shape, and refusing it was 8.8% of the dataset."""
    ops = _ops("""
import cadquery as cq
plate = cq.Workplane("XY").box(40, 30, 6)
boss = cq.Workplane("XY").cylinder(10, 4).translate((12, 0, 3))
result = plate.union(boss)
""")
    centre = ops[1]["args"]["params"]["center"]
    assert (centre["x"], centre["z"]) == pytest.approx((0.012, 0.003))


def test_a_translated_extrusion_moves_its_profile_and_its_offset():
    ops = _ops("""
import cadquery as cq
result = cq.Workplane("XY").rect(10, 10).extrude(4).translate((5, 0, 2))
""")
    assert ops[0]["args"]["offset"] == pytest.approx(0.002)
    xs = [c["a"][0] for c in ops[0]["args"]["profile"]["loops"][0]["curves"]]
    assert min(xs) == pytest.approx(0.0)      # -5 mm + 5 mm


def test_an_arc_through_three_points_becomes_the_centre_form():
    ops = _ops("""
import cadquery as cq
result = (cq.Workplane("XY").moveTo(-10, 0).lineTo(10, 0)
          .threePointArc((0, 10), (-10, 0)).close().extrude(3))
""")
    curves = ops[0]["args"]["profile"]["loops"][0]["curves"]
    arc = next(c for c in curves if c["kind"] == "arc")
    # Through (10,0), (0,10), (-10,0) the centre is the origin and the radius 10.
    assert arc["center"] == pytest.approx([0.0, 0.0])


def test_collinear_points_are_not_quietly_turned_into_a_line():
    with pytest.raises(ci.Unsupported) as caught:
        _ops("""
import cadquery as cq
result = (cq.Workplane("XY").moveTo(0, 0).lineTo(5, 0)
          .threePointArc((10, 0), (15, 0)).close().extrude(2))
""")
    assert caught.value.cause == "threePointArc:collinear"


@pytest.mark.parametrize("source, cause", [
    ("class Measures:\n    a = 1\n", "statement:ClassDef"),
    ("def f():\n    return 1\n", "statement:FunctionDef"),
    ("import cadquery as cq\nr = cq.Workplane('XY').box(9,9,9).shell(1)\n",
     "shell:no_face_selected"),
])
def test_what_it_cannot_run_is_named_rather_than_approximated(source, cause):
    """The cause is the measurement `yield_report` ranks, so it has to be specific:
    `shell:no_face_selected` is a decision (a closed hollow is a body nothing
    here builds exactly), while `statement:ClassDef` is work not done."""
    with pytest.raises(ci.Unsupported) as caught:
        _ops(source)
    assert caught.value.cause == cause


def test_an_array_of_holes_becomes_one_operation_each():
    """`pushPoints` 11.9%, `rarray` 5.6% and `polarArray` 3.6% are one idea, and
    together they were the largest remaining block after the first two passes."""
    ops = _ops("""
import cadquery as cq
result = (cq.Workplane("XY").box(80, 80, 6).faces(">Z")
          .rarray(40, 40, 2, 2).hole(5))
""")
    holes = [o for o in ops if o["op"] == "add_hole"]
    assert len(holes) == 4
    corners = sorted((round(h["args"]["center"]["x"], 4),
                      round(h["args"]["center"]["y"], 4)) for h in holes)
    assert corners == [(-0.02, -0.02), (-0.02, 0.02), (0.02, -0.02), (0.02, 0.02)]


def test_a_drawn_shape_at_several_points_is_one_profile_with_a_loop_each():
    """Even-odd parity reads several circles as several holes, so a
    pushPoints-and-circle is one operation rather than four."""
    ops = _ops("""
import cadquery as cq
result = (cq.Workplane("XY").rect(60, 60).extrude(5).faces(">Z")
          .pushPoints([(-20, 0), (20, 0)]).circle(4).cutBlind(-5))
""")
    cut = [o for o in ops if o["args"].get("operation") == "subtract"]
    assert len(cut) == 1
    circles = [c for c in cut[0]["args"]["profile"]["loops"][0]["curves"]
               if c["kind"] == "circle"]
    assert len(circles) == 2
    assert sorted(round(c["center"][0], 4) for c in circles) == [-0.02, 0.02]


def test_a_full_polar_array_does_not_double_its_first_instance():
    """A 360-degree span divides by `count`; a partial span by `count - 1`.
    Backwards, that doubles one hole and loses another — which no geometry check
    would call an error."""
    ops = _ops("""
import cadquery as cq
result = (cq.Workplane("XY").box(80, 80, 6).faces(">Z")
          .polarArray(30, 0, 360, 4).hole(4))
""")
    holes = [o for o in ops if o["op"] == "add_hole"]
    assert len(holes) == 4
    places = sorted((round(h["args"]["center"]["x"], 4),
                     round(h["args"]["center"]["y"], 4)) for h in holes)
    assert places == [(-0.03, 0.0), (0.0, -0.03), (0.0, 0.03), (0.03, 0.0)]


def test_a_comprehension_is_how_a_bolt_circle_is_usually_written():
    ops = _ops("""
import cadquery as cq
import math
holes = [(25 * math.cos(math.radians(a)), 25 * math.sin(math.radians(a)))
         for a in [0, 90, 180, 270]]
result = cq.Workplane("XY").box(70, 70, 5).faces(">Z").pushPoints(holes).hole(3)
""")
    assert len([o for o in ops if o["op"] == "add_hole"]) == 4


def test_a_selected_face_shell_becomes_shell_faces_by_position():
    """`shell_faces` could always open a face; it could not be *told* which one
    without an evaluated body to measure ids on."""
    ops = _ops("""
import cadquery as cq
result = cq.Workplane("XY").box(60, 40, 30).faces(">Z").shell(-3)
""")
    assert ops[-1]["op"] == "shell_faces"
    assert ops[-1]["args"]["faces"] == "top"
    assert ops[-1]["args"]["thickness"] == pytest.approx(0.003)


def test_a_closed_hollow_is_declined_rather_than_approximated():
    """`.shell()` with no face selected asks for a closed hollow, and nothing here
    builds one exactly: `add_modifier("shell")` puts its outer surface half a wall
    outside the body, and `MakeThickSolid` with no removed faces returns the inner
    solid rather than the wall."""
    with pytest.raises(ci.Unsupported) as caught:
        _ops("""
import cadquery as cq
result = cq.Workplane("XY").circle(20).extrude(10).shell(-2)
""")
    assert caught.value.cause == "shell:no_face_selected"


def test_a_whole_body_edge_treatment_on_a_curved_body_is_refused():
    """One program held the ingest for over an hour at 283% CPU: a 360-degree
    revolve with three holes, then `add_modifier("chamfer")` — a whole-body
    chamfer over a seam edge and several circular ones, which is where OCC's
    fillet solver stops converging. Nothing times out inside a C++ call, so the
    only reliable bound is not to emit the case."""
    with pytest.raises(ci.Unsupported) as caught:
        _ops("""
import cadquery as cq
ring = cq.Workplane("XZ").moveTo(20, 0).lineTo(40, 0).lineTo(40, 5).lineTo(20, 5).close().revolve()
result = ring.chamfer(1)
""")
    assert caught.value.cause == "chamfer:curved_body"


def test_a_whole_body_fillet_on_a_prismatic_body_is_still_emitted():
    """The guard is about curved edges, not about fillets: refusing every
    whole-body treatment took the yield from 28.8% to 2.3% when tried."""
    ops = _ops("""
import cadquery as cq
result = cq.Workplane("XY").box(40, 30, 6).edges(">Z").fillet(2)
""")
    assert ops[-1]["op"] == "add_modifier"
    assert ops[-1]["args"]["modifier"] == "fillet"


def test_a_program_emitting_hundreds_of_operations_is_refused():
    """Each operation rebuilds the whole tree, so the ingest cost is quadratic in
    the count — and a corpus where a part takes 255 calls teaches that. Measured
    on this dataset the median program emits 7 and the worst 255."""
    with pytest.raises(ci.Unsupported) as caught:
        _ops("""
import cadquery as cq
result = cq.Workplane("XY").box(200, 200, 6)
for i in range(40):
    result = result.faces(">Z").center(i * 2 - 40, 0).hole(1)
""")
    assert caught.value.cause.startswith("too_many_operations")
