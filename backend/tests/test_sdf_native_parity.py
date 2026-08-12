"""The native sampler must not move the isosurface. At all.

`forge_sdf` is a Rust reimplementation of the numpy SDF evaluator, chosen at
runtime when it is installed. Two evaluators for one field is a correctness
hazard before it is a speedup: a document that meshes one way on a machine with
the wheel and another way without it breaks the one property the whole caching
layer rests on — that `mesh_hash` identifies the triangles. `derived.sampler`
records which ran precisely because they must otherwise be indistinguishable.

So these compare the two directly, on the same trees, and require **identical
mesh hashes** rather than an approximate tolerance. `test_evaluation_cache`
already applies that standard to the numpy fast paths; this extends it across
the language boundary.

Skipped when the wheel is not built, which is the normal state of a dev
checkout — see `native/forge_sdf/README.md`.
"""

import pytest

from app.cad_kernel.core.feature_tree import Feature, FeatureTree
from app.cad_kernel.evaluator import brep, sdf_native
from app.cad_kernel.geom.meshing import mesh_hash, mesh_volume
from app.cad_kernel.kernel import Kernel

pytestmark = pytest.mark.skipif(
    not sdf_native.AVAILABLE,
    reason="the native SDF wheel is not built (see native/forge_sdf/README.md)")

_C = {"x": 0.0, "y": 0.0, "z": 0.0}


def _box(sx=0.1, sy=0.06, sz=0.02, c=_C):
    return {"type": "BoxFeature",
            "params": {"size": {"x": sx, "y": sy, "z": sz}, "center": dict(c)}}


def _sphere(r=0.04, c=_C):
    return {"type": "SphereFeature", "params": {"radius": r, "center": dict(c)}}


def _cyl(r=0.01, h=0.05, c=_C):
    return {"type": "CylinderFeature",
            "params": {"radius": r, "height": h, "center": dict(c),
                       "axis": {"x": 0, "y": 0, "z": 1}}}


def _bool(op, *children):
    return {"type": "BooleanFeature",
            "params": {"operation": op, "children": list(children)}}


#: One per shape the native compiler claims, plus the combinations that decide
#: whether the *tree walk* agrees rather than just the leaf primitives.
TREES = {
    "box": _box(),
    "sphere": _sphere(),
    "cylinder": _cyl(0.03, 0.08),
    "cone": {"type": "ConeFeature",
             "params": {"base_radius": 0.03, "height": 0.05, "center": dict(_C),
                        "axis": {"x": 0, "y": 0, "z": 1}}},
    "union": _bool("union", _box(), _cyl()),
    "subtract": _bool("subtract", _box(), _cyl(0.012, 0.06)),
    "intersect": _bool("intersect", _box(), _sphere(0.03)),
    "nested boolean": _bool("union", _bool("subtract", _box(), _cyl(0.012, 0.06)),
                            _sphere(0.012, {"x": 0.03, "y": 0.0, "z": 0.02})),
    # The smooth blends and the shell are the ones that matter most: the B-rep
    # kernel declines all three, so the SDF is the only path they ever take.
    "smooth fillet": {"type": "FilletFeature",
                      "params": {"operation": "union", "radius": 0.004,
                                 "children": [_box(), _cyl()]}},
    "smooth chamfer": {"type": "ChamferFeature",
                       "params": {"operation": "union", "radius": 0.004,
                                  "children": [_box(), _cyl()]}},
    "shell": {"type": "ShellFeature",
              "params": {"thickness": 0.002, "children": [_box()]}},
    "hole": {"type": "HoleFeature",
             "params": {"radius": 0.005, "depth": 0.05, "center": dict(_C),
                        "axis": {"x": 0, "y": 0, "z": 1}, "children": [_box()]}},
    "pattern": {"type": "PatternFeature",
                "params": {"kind": "linear", "count": 3, "spacing": 0.02,
                           "axis": {"x": 1, "y": 0, "z": 0}, "center": dict(_C),
                           "children": [_cyl()]}},
    "transform": {"type": "TransformFeature",
                  "params": {"translate": {"x": 0.01, "y": 0.0, "z": 0.0},
                             "rotate": {"x": 0.0, "y": 0.0, "z": 30.0},
                             "scale": 1.0, "children": [_box()]}},
    "extrude": {"type": "ExtrudeFeature",
                "params": {"profile": {"type": "rect", "width": 0.05,
                                       "height": 0.03, "cx": 0.0, "cy": 0.0},
                           "plane": "xy", "offset": 0.0, "depth": 0.02}},
}


def _evaluate(node: dict, *, native: bool, resolution: int):
    """Mesh `node` through the SDF path, with the native sampler on or off.

    The B-rep kernel is forced off for the duration: with OpenCascade installed
    it builds every one of these exactly and neither SDF sampler would run, so
    the comparison would silently be brep-against-brep — which passes while
    testing nothing. That is what a first pass at this measured.
    """
    was_brep, was_native = brep.AVAILABLE, sdf_native.AVAILABLE
    brep.AVAILABLE = False
    sdf_native.AVAILABLE = was_native and native
    try:
        tree = FeatureTree.from_dict({"version": "brep-v1", "root": node})
        mesh = Kernel.tessellate(Kernel.evaluate_feature(tree.root, resolution=resolution))
        return mesh_hash(mesh), abs(float(mesh_volume(mesh.vertices, mesh.faces)))
    finally:
        brep.AVAILABLE, sdf_native.AVAILABLE = was_brep, was_native


@pytest.mark.parametrize("name", sorted(TREES))
def test_the_two_samplers_produce_the_same_mesh(name):
    native_hash, native_vol = _evaluate(TREES[name], native=True, resolution=64)
    numpy_hash, numpy_vol = _evaluate(TREES[name], native=False, resolution=64)
    assert native_hash == numpy_hash, (
        f"{name}: the native sampler moved the isosurface "
        f"({native_vol * 1e9:.3f} mm³ against {numpy_vol * 1e9:.3f} mm³)")


def test_every_declared_shape_actually_compiles():
    """A silent fallback is the failure mode here: `compile_feature` returning
    None means the tree quietly takes the numpy path, and nothing observable
    changes except the time it takes. This asserts the coverage the module
    claims, so a shape that stops compiling is caught rather than absorbed."""
    uncompiled = []
    for name, node in TREES.items():
        if sdf_native.compile_feature(Feature.from_dict(node)) is None:
            uncompiled.append(name)
    assert not uncompiled, f"these no longer compile to native: {uncompiled}"


def test_the_kernel_reports_which_sampler_ran():
    """`derived.sampler` is what makes two meshes comparable across machines —
    one with the wheel, one without. It has to be the truth."""
    was_brep = brep.AVAILABLE
    brep.AVAILABLE = False
    try:
        tree = FeatureTree.from_dict({"version": "brep-v1", "root": TREES["union"]})
        assert Kernel.sampler_for(tree.root) == "native"
    finally:
        brep.AVAILABLE = was_brep


def test_a_voxel_body_still_falls_back_rather_than_compiling_wrong():
    """`OptimizeFeature`/`ImportFeature` carry a voxel grid the Rust side has no
    node for. The right answer is None — numpy handles it — and the wrong one is
    a compile that drops the grid and returns a different body."""
    from app.cad_kernel.ops.generative import OptimizeFeature

    feature = OptimizeFeature(
        name="opt",
        cache={"density": [[[0.0] * 4] * 4] * 4,
               "origin": {"x": 0.0, "y": 0.0, "z": 0.0},
               "size": {"x": 0.1, "y": 0.1, "z": 0.1},
               "threshold": 0.5},
        children=[Feature.from_dict(_box())])
    assert sdf_native.compile_feature(feature) is None
