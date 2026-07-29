"""Exact B-rep evaluation, and the STEP export that depends on it.

The SDF kernel's answer to "how big is this hole" is only exact to within a
sample spacing, and its answer to "which face is that" is that there are no
faces. This is the exact kernel alongside it.

The tests that matter most here are *differential*: the same feature tree built
both ways must agree. A B-rep translation that quietly differs from what the
rest of the system computes would be far worse than not having one, so every
primitive and modifier is checked against the SDF volume rather than against a
number someone typed in.
"""

import math

import pytest

from app.cad_kernel.core.feature_tree import FeatureTree
from app.cad_kernel.evaluator import brep
from app.cad_kernel.geom.meshing import mesh_volume, solid_to_mesh
from app.cad_kernel.kernel import Kernel
from app.services import cad_operations as ops
from app.services import document_analysis
from app.services import document_service as docs

pytestmark = pytest.mark.skipif(not brep.AVAILABLE, reason="OCP (OpenCascade) not installed")

V = lambda x, y, z: {"x": x, "y": y, "z": z}  # noqa: E731


def _tree(root: dict) -> FeatureTree:
    return FeatureTree.from_dict({"version": "brep-v1", "root": root})


def _node(kind: str, **params) -> dict:
    return {"type": kind, "id": "f1", "params": params}


def _sdf_volume(tree: FeatureTree, resolution: int = 160) -> float:
    mesh = solid_to_mesh(Kernel.evaluate_tree(tree, resolution=resolution))
    return abs(mesh_volume(mesh.vertices, mesh.faces))


def _exact_volume(tree: FeatureTree) -> float:
    shape = brep.brep_from_feature(tree.root)
    assert shape is not None, "expected an exact form"
    assert brep.is_valid(shape), "OCC produced an invalid shape"
    return brep.volume(shape)


# ------------------------------------------------------- exact vs sampled --

@pytest.mark.parametrize("root", [
    pytest.param(_node("BoxFeature", size=V(.08, .05, .02), center=V(0, 0, 0)), id="box"),
    pytest.param(_node("SphereFeature", radius=.03, center=V(0, 0, 0)), id="sphere"),
    pytest.param(_node("CylinderFeature", radius=.02, height=.06,
                       center=V(0, 0, 0), axis=V(0, 0, 1)), id="cylinder-z"),
    pytest.param(_node("CylinderFeature", radius=.015, height=.08,
                       center=V(.01, .02, 0), axis=V(1, 0, 0)), id="cylinder-x-offset"),
    pytest.param(_node("CylinderFeature", radius=.012, height=.07,
                       center=V(0, 0, 0), axis=V(1, 1, 1)), id="cylinder-diagonal"),
    pytest.param(_node("ConeFeature", base_radius=.03, height=.06,
                       center=V(0, 0, 0), axis=V(0, 0, 1)), id="cone"),
    pytest.param({"type": "BooleanFeature", "id": "r", "params": {
        "operation": "subtract", "children": [
            _node("BoxFeature", size=V(.08, .05, .02), center=V(0, 0, 0)),
            {"type": "CylinderFeature", "id": "c", "params": {
                "radius": .008, "height": .2, "center": V(0, 0, 0), "axis": V(0, 0, 1)}}]}},
        id="plate-with-bore"),
    pytest.param({"type": "HoleFeature", "id": "h", "params": {
        "radius": .006, "depth": .2, "center": V(.01, 0, 0), "axis": V(0, 0, 1),
        "children": [_node("BoxFeature", size=V(.08, .05, .02), center=V(0, 0, 0))]}},
        id="hole-feature"),
    pytest.param({"type": "TransformFeature", "id": "t", "params": {
        "translate": V(.02, .01, 0), "rotate": V(0, 0, 30), "scale": 1.0,
        "children": [_node("BoxFeature", size=V(.06, .03, .02), center=V(0, 0, 0))]}},
        id="transform-rotate"),
    pytest.param({"type": "TransformFeature", "id": "t", "params": {
        "translate": V(0, 0, 0), "rotate": V(0, 0, 0), "scale": 1.5,
        "children": [_node("BoxFeature", size=V(.04, .04, .04), center=V(0, 0, 0))]}},
        id="transform-scale"),
    pytest.param({"type": "MirrorFeature", "id": "m", "params": {
        "plane": "yz", "offset": 0.0,
        "children": [_node("BoxFeature", size=V(.03, .04, .02), center=V(.03, 0, 0))]}},
        id="mirror"),
    pytest.param({"type": "PatternFeature", "id": "p", "params": {
        "kind": "linear", "count": 3, "spacing": .05, "axis": V(1, 0, 0), "center": V(0, 0, 0),
        "children": [_node("BoxFeature", size=V(.02, .02, .02), center=V(0, 0, 0))]}},
        id="pattern-linear"),
    pytest.param({"type": "PatternFeature", "id": "p", "params": {
        "kind": "circular", "count": 4, "spacing": .05, "axis": V(0, 0, 1), "center": V(0, 0, 0),
        "children": [{"type": "CylinderFeature", "id": "c", "params": {
            "radius": .008, "height": .02, "center": V(.03, 0, 0), "axis": V(0, 0, 1)}}]}},
        id="pattern-circular"),
    pytest.param(_node("ExtrudeFeature", profile={"type": "rect", "width": .06, "height": .04,
                                                  "cx": 0, "cy": 0},
                       plane="xy", offset=0.0, depth=.02), id="extrude-rect"),
    pytest.param(_node("ExtrudeFeature", profile={"type": "circle", "radius": .02, "cx": 0, "cy": 0},
                       plane="xz", offset=.01, depth=.03), id="extrude-circle"),
    pytest.param(_node("ExtrudeFeature",
                       profile={"type": "polygon",
                                "points": [[-.03, -.02], [.03, -.02], [.03, .02],
                                           [0, .035], [-.03, .02]]},
                       plane="xy", offset=0.0, depth=.015), id="extrude-polygon"),
])
def test_the_exact_body_agrees_with_the_sampled_one(root):
    """Within marching-cubes discretisation error — the SDF is the approximation."""
    tree = _tree(root)
    exact, sampled = _exact_volume(tree), _sdf_volume(tree)
    assert abs(exact - sampled) / exact < 0.02, f"exact={exact} sampled={sampled}"


def test_exact_means_exact_not_merely_close():
    """A box is its own volume to floating point, with no resolution to pick."""
    tree = _tree(_node("BoxFeature", size=V(.08, .05, .02), center=V(0, 0, 0)))
    assert _exact_volume(tree) == pytest.approx(.08 * .05 * .02, rel=1e-12)

    sphere = _tree(_node("SphereFeature", radius=.03, center=V(0, 0, 0)))
    assert _exact_volume(sphere) == pytest.approx(4 / 3 * math.pi * .03 ** 3, rel=1e-9)


def test_a_thin_feature_is_still_exact_at_metre_scale():
    """OCC tolerances are pitched at mm; this kernel works in metres."""
    tree = _tree({"type": "BooleanFeature", "id": "r", "params": {
        "operation": "subtract", "children": [
            _node("BoxFeature", size=V(.002, .002, .0002), center=V(0, 0, 0)),
            {"type": "CylinderFeature", "id": "c", "params": {
                "radius": .00005, "height": .001, "center": V(0, 0, 0), "axis": V(0, 0, 1)}}]}})
    want = .002 * .002 * .0002 - math.pi * .00005 ** 2 * .0002
    assert _exact_volume(tree) == pytest.approx(want, rel=1e-9)


# ---------------------------------------------------- honest unsupported --

@pytest.mark.parametrize("kind", ["FilletFeature", "ChamferFeature", "ShellFeature",
                                  "SheetMetalFeature", "OptimizeFeature", "ImportFeature",
                                  "SweepFeature", "LoftFeature", "RevolveFeature"])
def test_features_with_no_exact_form_are_declined_not_approximated(kind):
    assert kind in brep.UNSUPPORTED


def test_a_smooth_boolean_fillet_has_no_exact_form():
    """It blends two bodies; it is not an edge treatment, so there is nothing to map."""
    tree = _tree({"type": "FilletFeature", "id": "f", "params": {
        "radius": .005, "operation": "union", "children": [
            _node("BoxFeature", size=V(.05, .05, .05), center=V(0, 0, 0)),
            {"type": "SphereFeature", "id": "s", "params": {"radius": .03, "center": V(.02, 0, 0)}}]}})
    assert brep.brep_from_feature(tree.root) is None


def test_one_unsupported_feature_disqualifies_the_whole_tree():
    """Half a body exactly and half approximated would be worse than neither."""
    tree = _tree({"type": "BooleanFeature", "id": "r", "params": {
        "operation": "union", "children": [
            _node("BoxFeature", size=V(.05, .05, .05), center=V(0, 0, 0)),
            {"type": "ImportFeature", "id": "i", "params": {"cache": {}, "source": {}}}]}})
    assert brep.brep_from_feature(tree.root) is None
    assert brep.supports(tree.root) is False


def test_supports_reports_what_brep_from_feature_will_do():
    box = _tree(_node("BoxFeature", size=V(.05, .05, .05), center=V(0, 0, 0)))
    assert brep.supports(box.root) is True
    shell = _tree({"type": "ShellFeature", "id": "s", "params": {
        "thickness": .004, "children": [box.root.to_dict() if hasattr(box.root, "to_dict")
                                        else _node("BoxFeature", size=V(.05, .05, .05),
                                                   center=V(0, 0, 0))]}})
    assert brep.supports(shell.root) is False


# -------------------------------------------------------------- topology --

def test_an_exact_body_has_addressable_faces_and_edges():
    """The thing the SDF kernel destroys at meshing: a plate with a bore is
    six planes plus one cylinder, not one nameless triangle soup."""
    from OCP.TopAbs import TopAbs_EDGE, TopAbs_FACE
    from OCP.TopExp import TopExp_Explorer

    tree = _tree({"type": "BooleanFeature", "id": "r", "params": {
        "operation": "subtract", "children": [
            _node("BoxFeature", size=V(.08, .05, .02), center=V(0, 0, 0)),
            {"type": "CylinderFeature", "id": "c", "params": {
                "radius": .008, "height": .2, "center": V(0, 0, 0), "axis": V(0, 0, 1)}}]}})
    shape = brep.brep_from_feature(tree.root)

    def count(kind):
        exp, n = TopExp_Explorer(shape, kind), 0
        while exp.More():
            n += 1
            exp.Next()
        return n

    assert count(TopAbs_FACE) == 7      # six box faces + the bore wall
    assert count(TopAbs_EDGE) > 10

    mesh = solid_to_mesh(Kernel.evaluate_tree(tree, resolution=48))
    assert len(mesh.faces) > 100        # the SDF answer: triangles, no identity


# ------------------------------------------------------------ STEP export --

def _doc_with_bore():
    doc = docs.create_document(name="Plate", resolution=48, owner_id=None)
    ops.dispatch("add_primitive", doc.id, {"shape": "box",
                                           "params": {"size": V(.08, .05, .02)}})
    ops.dispatch("add_primitive", doc.id, {"shape": "cylinder", "operation": "subtract",
                                           "params": {"radius": .008, "height": .2}})
    return doc.id


def test_step_export_writes_analytic_surfaces_not_triangles():
    """The interop wall: a facetted cylinder cannot be measured or edited downstream."""
    from app.cad_kernel.io.step_brep import surface_kinds

    text = document_analysis.exact_step_document(_doc_with_bore())
    assert text is not None
    kinds = surface_kinds(text)
    assert kinds["CYLINDRICAL_SURFACE"] == 1
    assert kinds["ADVANCED_FACE"] == 7


def test_the_exported_file_declares_millimetres():
    """Without a unit context an importer guesses, and an 80 mm plate arrived 0.08 mm."""
    text = document_analysis.exact_step_document(_doc_with_bore())
    assert "SI_UNIT(.MILLI.,.METRE.)" in text


def test_the_export_round_trips_at_the_right_size():
    """The end-to-end check that units and scale actually agree."""
    import tempfile

    from OCP.Bnd import Bnd_Box
    from OCP.BRepBndLib import BRepBndLib
    from OCP.IFSelect import IFSelect_ReturnStatus
    from OCP.STEPControl import STEPControl_Reader

    text = document_analysis.exact_step_document(_doc_with_bore())
    path = tempfile.mktemp(suffix=".step")
    with open(path, "w") as fh:
        fh.write(text)

    reader = STEPControl_Reader()
    assert reader.ReadFile(path) == IFSelect_ReturnStatus.IFSelect_RetDone
    reader.TransferRoots()
    shape = reader.OneShape()

    bounds = Bnd_Box()
    BRepBndLib.Add_s(shape, bounds)
    xmin, ymin, zmin, xmax, ymax, zmax = bounds.Get()
    assert (xmax - xmin, ymax - ymin, zmax - zmin) == pytest.approx((80.0, 50.0, 20.0), abs=1e-6)
    assert brep.volume(shape) == pytest.approx(80 * 50 * 20 - math.pi * 64 * 20, rel=1e-6)


def test_a_body_with_no_exact_form_still_exports_as_facets():
    """Falling back is the contract; an unexportable part would be worse."""
    doc = docs.create_document(name="Shelled", resolution=48, owner_id=None)
    ops.dispatch("add_primitive", doc.id, {"shape": "box"})
    ops.dispatch("add_modifier", doc.id, {"modifier": "shell", "params": {"thickness": .004}})

    assert document_analysis.exact_step_document(doc.id) is None
    text = document_analysis.export_step_document(doc.id, resolution=32)
    assert "MANIFOLD_SOLID_BREP" in text
    assert "SI_UNIT(.MILLI.,.METRE.)" in text  # the faceted writer declares units too


def test_the_faceted_writer_also_exports_at_the_right_size():
    """It emitted raw metre values with no unit context, so parts arrived 1000x small."""
    import re

    doc = docs.create_document(name="Shelled", resolution=32, owner_id=None)
    ops.dispatch("add_primitive", doc.id, {"shape": "box",
                                           "params": {"size": V(.08, .05, .02)}})
    ops.dispatch("add_modifier", doc.id, {"modifier": "shell", "params": {"thickness": .004}})
    text = document_analysis.export_step_document(doc.id, resolution=32)

    xs = [abs(float(m.split(",")[0]))
          for m in re.findall(r"CARTESIAN_POINT\('NONE',\(([^)]+)\)\)", text)]
    # 80 mm wide, so ±40 — plus 2 mm, because ShellFeature is `abs(d) - t/2` and
    # therefore grows the body outward by half its thickness. The point is the
    # magnitude: tens of millimetres, not hundredths.
    assert max(xs) == pytest.approx(42.0, abs=1.0)
