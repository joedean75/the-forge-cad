"""A real ISO 68-1 thread — the helix, not a callout.

S3 built the groove tool correctly and could not cut it: the body came back
still measuring the full major cylinder. The answer is the other construction —
fuse a helical rib *outward* onto a minor-diameter core rather than cut a groove
inward from a major-diameter one — and two details that decide whether it works
at all:

* **the rib is sunk a twentieth of its depth into the core, along its own
  flanks**, because a rib whose inner face sits exactly on the core surface is a
  coincident-face boolean and OCC returns an empty result for an M4 built that
  way; and
* **it is built in millimetres and scaled down**, because OCC's tolerances are
  absolute and an M8's crest flat is 78 microns — the identical construction
  returns a solid of *zero volume* at metre scale.

Which is why every test here checks a **volume against the closed form**. A
thread that looks right and measures wrong is the failure mode this feature had
for a whole sprint, and only the number catches it.
"""

import math

import pytest

from app.cad_kernel.core.feature_tree import FeatureTree
from app.cad_kernel.evaluator import brep
from app.services import cad_operations as ops
from app.services import decompiler
from app.services import document_service as docs

pytestmark = pytest.mark.skipif(not brep.AVAILABLE,
                                reason="a thread exists only on the exact kernel")

V = lambda x, y, z: {"x": x, "y": y, "z": z}  # noqa: E731
ROOT3 = math.sqrt(3.0)


def _thread_tree(D_mm, P_mm, L_mm, right=True, axis=(0, 0, 1), centre=(0, 0, 0)):
    return FeatureTree.from_dict({"version": "brep-v1", "root": {
        "type": "ThreadFeature", "name": "Thread", "params": {
            "diameter": D_mm / 1000, "pitch": P_mm / 1000, "length": L_mm / 1000,
            "center": V(*[c / 1000 for c in centre]), "axis": V(*axis),
            "right_handed": right}}}).root


def closed_form(D, P, L):
    """The volume of a basic ISO 68-1 external thread, in mm³.

    The minor cylinder plus the material the thread stands above it: a trapezoid
    `0.75P` wide at the root and `P/8` at the crest over a radial depth of
    `5H/8`, carried round by Pappus. Independent of anything the kernel does,
    which is the point of comparing against it.
    """
    minor_r = D / 2 - 5 * (P * ROOT3 / 2) / 8
    depth = D / 2 - minor_r
    cross = (0.75 * P + 0.125 * P) / 2 * depth
    centroid = minor_r + depth * (0.75 + 0.25) / (3 * 0.875)
    return math.pi * minor_r ** 2 * L + cross * 2 * math.pi * centroid * (L / P)


def _volume_mm3(node):
    return brep.volume(brep.build_strict(node)) * 1e9


# ================================================== the geometry is the standard ==

@pytest.mark.parametrize("D,P,L", [(8.0, 1.25, 20.0), (4.0, 0.7, 12.0),
                                   (12.0, 1.75, 40.0), (3.0, 0.5, 6.0),
                                   (2.0, 0.4, 5.0), (6.0, 1.0, 15.0)])
def test_a_thread_matches_the_closed_form(D, P, L):
    """Exact, not approximate: the fused body *is* the ISO trapezoid swept, and
    the sink that makes the boolean work is buried inside the core where it
    cannot change the answer."""
    got = _volume_mm3(_thread_tree(D, P, L))
    assert got == pytest.approx(closed_form(D, P, L), rel=2e-4), (D, P, L, got)


def test_the_thread_lies_between_its_own_two_cylinders():
    """The reading that would have caught S3's failure immediately: a body that
    still measures the full major cylinder has no thread on it."""
    D, P, L = 8.0, 1.25, 20.0
    minor_r = D / 2 - 5 * (P * ROOT3 / 2) / 8
    got = _volume_mm3(_thread_tree(D, P, L))
    assert math.pi * minor_r ** 2 * L < got < math.pi * (D / 2) ** 2 * L
    # And it is nowhere near either end of that range — a thread is about 82% of
    # its major cylinder, not 99% and not 70%.
    assert 0.78 < got / (math.pi * (D / 2) ** 2 * L) < 0.86


def _extent_mm(node):
    """The tessellation's extent, in millimetres.

    Measured off the triangles and never off `Bnd_Box`: that box bounds a swept
    BSpline's *control points*, not its surface, and reported a 10 mm coil as
    60 mm across. S3 lost two hours to it.
    """
    verts, _ = brep.tessellate(brep.build_strict(node))
    return (verts.max(axis=0) - verts.min(axis=0)) * 1000, verts.min(axis=0) * 1000


def test_it_is_the_right_size_where_it_is_asked_for():
    size, _ = _extent_mm(_thread_tree(8.0, 1.25, 20.0))
    assert size == pytest.approx([8.0, 8.0, 20.0], abs=0.05)


def test_a_left_hand_thread_is_a_different_body_of_the_same_size():
    right = brep.build_strict(_thread_tree(8.0, 1.25, 20.0, right=True))
    left = brep.build_strict(_thread_tree(8.0, 1.25, 20.0, right=False))
    assert brep.volume(right) == pytest.approx(brep.volume(left), rel=1e-5)
    # Same volume, opposite winding: a left-hand thread will not take a
    # right-hand fastener, so it has to actually be a different solid.
    a, _ = brep.tessellate(right)
    b, _ = brep.tessellate(left)
    assert not (a.shape == b.shape and bool((a == b).all()))


def test_it_winds_on_the_axis_it_is_given():
    size, low = _extent_mm(_thread_tree(8.0, 1.25, 20.0, axis=(1, 0, 0),
                                        centre=(10, 0, 0)))
    assert size == pytest.approx([20.0, 8.0, 8.0], abs=0.05)
    assert low[0] == pytest.approx(10.0, abs=0.05)


# ============================================== a stud one way, a tap the other ==

def _part(name="part"):
    return docs.create_document(name=name, owner_id="dev", resolution=64)


def test_union_gives_a_bolt():
    doc = _part("bolt")
    ops.dispatch("add_primitive", doc.id, {
        "shape": "cylinder",
        "params": {"radius": 0.0065, "height": 0.008, "center": V(0, 0, -0.004)}})
    result = ops.dispatch("add_thread", doc.id, {"thread": "M8", "length": 0.02})
    assert result["turns"] == pytest.approx(16.0)
    body = docs.get_document(doc.id)
    assert body.derived.error is None
    head = math.pi * 6.5 ** 2 * 8
    assert body.derived.volume * 1e9 == pytest.approx(
        head + closed_form(8.0, 1.25, 20.0), rel=0.01)


def test_subtract_taps_a_hole():
    """An internal thread is the negative of an external one — a tap is a
    screw — which is why one feature answers both and neither needs a flag."""
    doc = _part("tapped plate")
    ops.dispatch("add_primitive", doc.id, {
        "shape": "box", "params": {"size": V(0.03, 0.03, 0.012), "center": V(0, 0, 0)}})
    ops.dispatch("add_thread", doc.id, {
        "thread": "M6", "length": 0.014, "center": V(0, 0, -0.007),
        "operation": "subtract"})
    body = docs.get_document(doc.id)
    assert body.derived.error is None
    assert body.derived.volume * 1e9 == pytest.approx(
        30 * 30 * 12 - closed_form(6.0, 1.0, 12.0), rel=0.005)


def test_the_designation_carries_the_iso_261_coarse_pitch():
    doc = _part()
    ops.dispatch("add_primitive", doc.id, {
        "shape": "cylinder", "params": {"radius": 0.01, "height": 0.02}})
    result = ops.dispatch("add_thread", doc.id, {"thread": "M8", "length": 0.01})
    assert result["pitch_mm"] == pytest.approx(1.25)
    # d1 and d2 from the ISO 68-1 triangle, which are what a fastener is
    # actually checked against.
    assert result["minor_diameter_mm"] == pytest.approx(6.647, abs=0.001)
    assert result["pitch_diameter_mm"] == pytest.approx(7.188, abs=0.001)


def test_an_explicit_pitch_overrides_the_designation():
    doc = _part()
    ops.dispatch("add_primitive", doc.id, {
        "shape": "cylinder", "params": {"radius": 0.01, "height": 0.02}})
    fine = ops.dispatch("add_thread", doc.id,
                        {"thread": "M8", "pitch": 0.001, "length": 0.01})
    assert fine["pitch_mm"] == pytest.approx(1.0)


# ================================================================ the refusals ==

def test_more_turns_than_occ_builds_is_refused_and_says_why():
    """**The one that matters.** Past about 32 swept turns the rib comes back
    quietly short of material rather than failing — 4% at 36 turns, 43% at 60 —
    and a body that is silently 43% light is exactly what this action space must
    not return as `ok`."""
    doc = _part()
    ops.dispatch("add_primitive", doc.id, {
        "shape": "cylinder", "params": {"radius": 0.01, "height": 0.04}})
    result = ops.execute("add_thread", doc.id, {"thread": "M3", "length": 0.030})
    assert not result["ok"]
    assert result["error"]["code"] == "PRECONDITION"
    # The real reason, not "boolean child has no exact form": a caller can act
    # on a turn count and cannot act on that.
    assert "turns" in result["error"]["message"]
    assert "coarser pitch" in result["error"]["message"]
    # And the document is unchanged — a rolled-back edit, not a broken body.
    assert docs.get_document(doc.id).derived.error is None


def test_a_diameter_without_a_pitch_is_refused():
    """There is no coarse pitch to assume for a diameter that is not a
    designation, and inventing one is inventing a different thread."""
    doc = _part()
    ops.dispatch("add_primitive", doc.id, {
        "shape": "cylinder", "params": {"radius": 0.01, "height": 0.02}})
    result = ops.execute("add_thread", doc.id, {"diameter": 0.008, "length": 0.01})
    assert not result["ok"] and result["error"]["code"] == "MISSING_ARG"


def test_naming_no_thread_at_all_is_refused():
    doc = _part()
    ops.dispatch("add_primitive", doc.id, {
        "shape": "cylinder", "params": {"radius": 0.01, "height": 0.02}})
    result = ops.execute("add_thread", doc.id, {"length": 0.01})
    assert not result["ok"] and result["error"]["code"] == "MISSING_ARG"


def test_a_pitch_that_cuts_past_the_axis_is_refused():
    doc = _part()
    ops.dispatch("add_primitive", doc.id, {
        "shape": "cylinder", "params": {"radius": 0.01, "height": 0.02}})
    result = ops.execute("add_thread", doc.id,
                         {"diameter": 0.002, "pitch": 0.004, "length": 0.004})
    assert not result["ok"]
    assert "axis" in result["error"]["message"]


# ================================================================ the surface ==

def test_the_decompiler_round_trips_a_thread():
    """A feature the decompiler cannot emit drags every document containing it
    to 0% and takes its neighbours with it."""
    doc = _part("threaded stud")
    ops.dispatch("add_primitive", doc.id, {
        "shape": "cylinder", "params": {"radius": 0.0065, "height": 0.008,
                                        "center": V(0, 0, -0.004)}})
    ops.dispatch("add_thread", doc.id, {"thread": "M8", "length": 0.02})
    report = decompiler.decompile_document(doc.id, resolution=48)
    assert report["ok"], report
    assert any(step["op"] == "add_thread" for step in report["steps"]), report["steps"]


# ======================================= the catalog's third fidelity ==========

def test_a_catalog_screw_is_plain_by_default_and_that_is_load_bearing():
    """Every screw already saved in this datastore is a plain shank. A document
    that changes shape because the catalog learned something new is a document
    that stopped meaning what it said."""
    from app.services import parts_catalog as pc

    entry = pc.by_key("iso4762-m4-16")
    assert pc.resolve_params(entry)["threaded"] == 0.0
    assert pc.make_ref("iso4762-m4-16") == "iso4762-m4-16"
    assert pc.fidelity_of(entry) == "envelope"
    # And the listing still shows the row it always showed.
    assert pc.describe(entry)["fidelity"] == "envelope"
    assert pc.describe(entry)["size_mm"] == {"x": 7.0, "y": 7.0, "z": 20.0}


def test_a_threaded_screw_is_a_different_configuration_with_its_own_fidelity():
    from app.services import parts_catalog as pc

    entry = pc.by_key("iso4762-m4-16")
    assert pc.make_ref("iso4762-m4-16", {"threaded": 1}) == "iso4762-m4-16?threaded=1"
    assert pc.designation_of(entry, {"threaded": 1}) == "ISO 4762 M4×16 (threaded)"
    assert pc.fidelity_of(entry, {"threaded": 1}) == "as-standard"


def test_the_thread_is_cut_over_the_standards_own_thread_length():
    """ISO 4762 threads `b = 2d + 6` from the tip. It is also what keeps every
    row in the catalog inside the sweep's turn limit."""
    from app.services import parts_catalog as pc

    entry = pc.by_key("iso4762-m4-16")
    plain = _volume_mm3(FeatureTree.from_dict(pc.build_tree(entry)).root)
    threaded = _volume_mm3(
        FeatureTree.from_dict(pc.build_tree(entry, {"threaded": 1})).root)
    run = 2 * 4.0 + 6.0                       # b for an M4
    removed = math.pi * 2.0 ** 2 * run - closed_form(4.0, 0.7, run)
    assert plain - threaded == pytest.approx(removed, rel=0.01)


def test_a_choice_takes_its_two_answers_and_nothing_else():
    """A zero here is a real answer — "not threaded" — where every other
    parameter in this catalog is a length and a zero is a mistake."""
    from app.services import parts_catalog as pc

    entry = pc.by_key("iso4762-m4-16")
    assert pc.resolve_params(entry, {"threaded": 0})["threaded"] == 0.0
    with pytest.raises(ValueError, match="one of"):
        pc.resolve_params(entry, {"threaded": 2})


def test_every_catalog_screw_threads_within_the_sweep_limit():
    """The limit is real and the catalog has to live inside it, or a row exists
    that cannot be placed in the configuration it advertises."""
    from app.cad_kernel.evaluator.brep import _MAX_THREAD_TURNS
    from app.services import parts_catalog as pc

    worst = 0.0
    for entry in pc.catalog():
        if not entry.key.startswith("iso4762-"):
            continue
        size, length = entry.designation.split()[2].split("×")
        shank, length = float(size[1:]), float(length)
        run = min(2 * shank + 6.0, length)
        worst = max(worst, run / pc.METRIC[size].pitch + 2.0)
    assert 0 < worst <= _MAX_THREAD_TURNS, worst


def test_placing_a_threaded_screw_reports_which_body_it_placed():
    from app.services import assembly_service

    asm = assembly_service.create_assembly(name="threaded", owner_id="dev")
    plain = ops.dispatch("insert_standard_part", asm.id,
                         {"key": "iso4762-m4-16"})
    assert plain["fidelity"] == "envelope"
    threaded = ops.dispatch("insert_standard_part", asm.id,
                            {"key": "iso4762-m4-16", "params": {"threaded": 1}})
    assert threaded["fidelity"] == "as-standard"
    assert "threaded" in threaded["designation"]
    # Two configurations are two parts, so the BOM has two lines to order from.
    assert threaded["part_id"] != plain["part_id"]


def test_a_thread_needs_the_exact_kernel_and_says_so():
    """No field form: the distance to a helical surface has no closed form and
    the usual approximation drops the tangential term exactly where the pitch is
    the interesting part."""
    from app.cad_kernel.evaluator.sdf import _SDF_BUILDERS

    assert "ThreadFeature" in _SDF_BUILDERS
    with pytest.raises(ValueError, match="exact B-rep kernel"):
        _SDF_BUILDERS["ThreadFeature"](_thread_tree(8.0, 1.25, 20.0))
