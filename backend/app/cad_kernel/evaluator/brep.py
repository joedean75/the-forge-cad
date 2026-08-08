"""Exact B-rep evaluation via OpenCascade, alongside the SDF kernel.

The SDF kernel's strengths are real: booleans never fail, a voxel density field
from the optimizer or an imported mesh drops straight in, and arbitrary bodies
blend smoothly. What it cannot do is give geometry an *identity*. Marching cubes
returns one face with `surface=None`, so no edge or face is addressable, a
dimension is only exact to within a sample spacing, and STEP export is triangles
wearing AP214 clothing.

This module builds the same feature tree as exact topology instead. It is
deliberately partial: `brep_from_feature` returns ``None`` for anything it cannot
represent faithfully, exactly as `sdf_native.compile_feature` already does for
the Rust fast path, and the caller falls back. Approximating a feature here would
be worse than declining it — a body that silently differs from what the SDF
kernel shows is a far harder problem than one that is honestly unavailable.

What it does not build, and why:

* ``FilletFeature`` / ``ChamferFeature`` — these are smooth booleans *between
  bodies*, not edge treatments. There is no B-rep equivalent; the B-rep answer
  is a genuine edge fillet, which is a different operation on different inputs.
* ``ShellFeature`` — ``abs(d) - t/2`` hollows everything with no face removal.
  A real shell removes named faces, which again is a different operation.
* ``SheetMetalFeature`` — its whole value is the analytic bend model that makes
  `flat_pattern` computable.
* ``OptimizeFeature`` / ``ImportFeature`` — cached voxel grids with no analytic
  form to recover.
* ``LoftFeature`` — `LoftSDF` linearly blends the two profiles' *distance
  values*, which its own docstring calls an approximation rather than a ruled
  surface. OCC's `ThruSections` builds a true loft, which is a genuinely
  different shape — so mapping it here would silently change what an existing
  document means depending on whether OCC happens to be installed. The true
  loft is ``RuledLoftFeature`` instead, sitting beside it the way
  `EdgeFilletFeature` sits beside `FilletFeature`.

Every build also records a **generative history** (`core/history`): which feature
created each face and edge, and what each one became as the tree was assembled.
That is what lets a stored selection be resolved by following the topology rather
than by re-measuring it — see `_resolved` and `build_with_history`. It costs a
topology traversal per stage and no geometry evaluation, and it is always on,
because the builders that resolve references need it mid-build whether or not the
caller wants it afterwards.

Units are metres throughout, matching the rest of the kernel. OCC's default
tolerances are pitched at millimetres, but `Precision::Confusion` is 1e-7, which
at metre scale is 0.1 µm — four orders of magnitude below the thinnest feature
this kernel deals with (0.15 mm sheet). Verified in `tests/test_brep_kernel.py`
down to a 0.2 mm plate.
"""

from __future__ import annotations

import copy
import hashlib
import json
import math
from collections import OrderedDict
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any, Dict, List, Optional, Tuple

import numpy as np

from app.cad_kernel.core import history

try:  # OCP (OpenCascade 7.x bindings) is an optional dependency.
    from OCP.BRep import BRep_Builder
    from OCP.BRepAlgoAPI import BRepAlgoAPI_Common, BRepAlgoAPI_Cut, BRepAlgoAPI_Fuse
    from OCP.BRepBuilderAPI import (
        BRepBuilderAPI_MakeEdge,
        BRepBuilderAPI_MakeFace,
        BRepBuilderAPI_MakePolygon,
        BRepBuilderAPI_MakeWire,
        BRepBuilderAPI_Transform,
    )
    from OCP.BRepCheck import BRepCheck_Analyzer
    from OCP.BRepGProp import BRepGProp
    from OCP.BRepPrimAPI import (
        BRepPrimAPI_MakeBox,
        BRepPrimAPI_MakeCone,
        BRepPrimAPI_MakeCylinder,
        BRepPrimAPI_MakePrism,
        BRepPrimAPI_MakeSphere,
    )
    from OCP.gp import (
        gp_Ax1,
        gp_Ax2,
        gp_Circ,
        gp_Dir,
        gp_Pnt,
        gp_Trsf,
        gp_Vec,
    )
    from OCP.GProp import GProp_GProps
    from OCP.TopoDS import TopoDS_Shape

    AVAILABLE = True
except Exception:  # pragma: no cover - exercised only where OCP is absent
    AVAILABLE = False
    TopoDS_Shape = Any  # type: ignore[assignment,misc]


class BrepUnsupported(Exception):
    """This feature has no faithful exact representation (yet)."""


# Feature types the SDF kernel owns outright. Listed rather than inferred so the
# reason for each is written down next to it (see the module docstring).
UNSUPPORTED = {
    "FilletFeature", "ChamferFeature", "ShellFeature",
    "OptimizeFeature", "ImportFeature", "LoftFeature",
}


# ---------------------------------------------------- generative history --

class _Recorder:
    """Tag maps for every shape built during one evaluation.

    Keyed on `id(shape)` — the Python handle a builder returned — because that is
    the only thing that identifies a *whole body* between the moment it is built
    and the moment its parent consumes it. Sub-shape identity is a different
    question and `history.ShapeKeys` answers it. The built shapes are kept alive
    in `_alive` for exactly that reason: a freed handle's `id` could be reused by
    a later one and silently hand a body the wrong map.
    """

    def __init__(self):
        self.keys = history.ShapeKeys()
        self._maps: Dict[int, history.EntityMap] = {}
        self._alive: List[Any] = []
        # What each topology-addressing feature's stored references resolved to
        # this build. Collected here rather than recomputed on demand because the
        # answer is only knowable while the intermediate bodies still exist.
        self.reports: List[Dict[str, Any]] = []
        # Subtree content keys, memoised per feature object for this build. Also
        # keyed on `id`, so the features are pinned for the same reason the
        # shapes are.
        self.subtree_keys: Dict[int, Optional[str]] = {}
        self._pinned: List[Any] = []

    def set(self, shape, entities: history.EntityMap) -> history.EntityMap:
        self._maps[id(shape)] = entities
        self._alive.append(shape)
        return entities

    def get(self, shape) -> Optional[history.EntityMap]:
        return self._maps.get(id(shape)) if shape is not None else None


_RECORDER: ContextVar[Optional[_Recorder]] = ContextVar("brep_recorder", default=None)


@contextmanager
def _recording():
    """Run a build with a recorder, reusing an outer one if a build is already up."""
    existing = _RECORDER.get()
    if existing is not None:
        yield existing
        return
    recorder = _Recorder()
    token = _RECORDER.set(recorder)
    try:
        yield recorder
    finally:
        _RECORDER.reset(token)


# ----------------------------------------------------------- build cache --
#
# Editing one feature of a twelve-feature part used to rebuild all twelve, and
# then rebuild them *again* the first time any topology operation asked for the
# exact body. Both are avoidable: a subtree's shape is a pure function of that
# subtree, so it can be keyed by content and kept.
#
# Three things make this safe rather than merely fast, and none of them is
# optional:
#
# * **The key covers everything the build reads.** It is the node's own
#   parameters plus its children's keys, recursively, so a parameter substituted
#   into a leaf ten levels down changes the root's key. Any feature that will not
#   serialise yields ``None`` and simply is not cached — a miss is slow, a stale
#   hit is a document that quietly stops meaning what it says.
# * **The entity tags come back with the shape.** A cached shape handed to a
#   parent boolean has to answer `_tags`, or `history.advance` sees an untagged
#   input and the whole Sprint-8 naming scheme silently degrades to descriptor
#   matching. The `EntityMap` is cached alongside, still bound to the
#   `ShapeKeys` it was built under; that map indexes by `IsSame`, and the cached
#   shape's sub-shapes are the same ones, so the tags still resolve.
# * **So do the reference verdicts.** `_resolved` files "these four edges
#   matched, that one is gone" on the recorder while the child body exists. A
#   cached subtree never runs it, so the verdicts are replayed from the entry —
#   otherwise `check_references` would report fewer selections the second time a
#   document is built than the first.

# Bounded because the entries are OCC bodies, not numbers: each pins a shape,
# its tag map, and the `ShapeKeys` map that map indexes into. A twelve-feature
# part occupies about two dozen entries per distinct state, so this holds a few
# states of a few documents — which is what an edit-undo-redo loop actually
# revisits — rather than a session's whole history.
_NODE_CACHE_SIZE = 96
_NODE_CACHE: "OrderedDict[str, Tuple[Any, Any, List[Dict[str, Any]]]]" = OrderedDict()

_MISSING = object()


def clear_cache() -> None:
    """Drop every cached subtree build. For tests and for memory pressure."""
    _NODE_CACHE.clear()


def cache_size() -> int:
    return len(_NODE_CACHE)


def _assign_keys(feature, node: Dict[str, Any], recorder: "_Recorder") -> str:
    """Key this feature and every feature under it, from one serialized tree.

    Walked against the already-serialized dict rather than calling `to_dict` per
    node: `to_dict` recurses into children, so keying each node from its own
    call would serialise the tree once per node.
    """
    children = getattr(feature, "children", None) or []
    serialized = ((node.get("params") or {}).get("children") or [])
    child_keys = [_assign_keys(child, child_node, recorder)
                  for child, child_node in zip(children, serialized)]
    params = {k: v for k, v in (node.get("params") or {}).items() if k != "children"}
    payload = json.dumps(
        [node.get("type"), node.get("id"), node.get("name"), params, child_keys],
        sort_keys=True)
    key = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    recorder.subtree_keys[id(feature)] = key
    recorder._pinned.append(feature)
    return key


def _subtree_key(feature) -> Optional[str]:
    """A content key for this subtree, or ``None`` if it cannot be keyed."""
    recorder = _RECORDER.get()
    if recorder is None:
        return None
    hit = recorder.subtree_keys.get(id(feature), _MISSING)
    if hit is not _MISSING:
        return hit
    try:
        _assign_keys(feature, feature.to_dict(), recorder)
    except Exception:  # noqa: BLE001 — a feature that will not serialise is uncached
        recorder.subtree_keys[id(feature)] = None
        recorder._pinned.append(feature)
        return None
    return recorder.subtree_keys.get(id(feature))


def _reuse(key: Optional[str]):
    """A cached shape, re-registered on the running build, or ``None``."""
    if key is None:
        return None
    entry = _NODE_CACHE.get(key)
    if entry is None:
        return None
    _NODE_CACHE.move_to_end(key)
    shape, entities, reports = entry
    recorder = _RECORDER.get()
    if recorder is not None:
        recorder.set(shape, entities)
        # Copied, because these outlive the build that made them and a caller
        # that edited one in place would be editing every later build's answer.
        recorder.reports.extend(copy.deepcopy(reports))
    return shape


def _remember(key: Optional[str], shape, mark: int) -> None:
    """Cache a freshly built shape with its tags and the verdicts it filed."""
    recorder = _RECORDER.get()
    if key is None or recorder is None or shape is None:
        return
    entities = recorder.get(shape)
    if entities is None:
        return
    _NODE_CACHE[key] = (shape, entities, copy.deepcopy(recorder.reports[mark:]))
    _NODE_CACHE.move_to_end(key)
    while len(_NODE_CACHE) > _NODE_CACHE_SIZE:
        _NODE_CACHE.popitem(last=False)


def _owner(feature) -> str:
    """The tag namespace for entities this feature creates.

    Feature ids are deterministic (`feature_ops.IdAllocator`), so tags are too.
    The type-name fallback only fires for a hand-built tree that never went
    through the document layer, where every node of a type shares a namespace —
    weaker, but no worse than having no tags at all.
    """
    return str(getattr(feature, "id", None) or type(feature).__name__)


def _tags(shape) -> Optional[history.EntityMap]:
    recorder = _RECORDER.get()
    return recorder.get(shape) if recorder is not None else None


def _record(owner, result, builder, sources, *, trust_deleted: bool = False):
    """Carry the sources' tags across one OCC builder onto ``result``.

    ``owner`` is a feature or a namespace string; whatever the stage creates from
    nothing is tagged under it.
    """
    recorder = _RECORDER.get()
    if recorder is None:
        return result
    name = owner if isinstance(owner, str) else _owner(owner)
    pairs = [(s, _tags(s)) for s in sources]
    recorder.set(result, history.advance(pairs, builder, result, name,
                                         recorder.keys, trust_deleted=trust_deleted))
    return result


def _record_map(result, entities):
    recorder = _RECORDER.get()
    if recorder is not None and entities is not None:
        recorder.set(result, entities)
    return result


def _seeded(shape, owner: str):
    """First tags for a shape built from nothing, or None outside a build."""
    recorder = _RECORDER.get()
    return history.seed(shape, owner, recorder.keys) if recorder is not None else None


def _suffix(shape, mark: str):
    """The shape's tags, marked as one copy — see `EntityMap.suffixed`."""
    entities = _tags(shape)
    return entities.suffixed(mark) if entities is not None else None


def _pnt(v) -> "gp_Pnt":
    return gp_Pnt(float(v.x), float(v.y), float(v.z))


def _dir(v) -> "gp_Dir":
    n = math.sqrt(v.x ** 2 + v.y ** 2 + v.z ** 2)
    if n < 1e-12:
        raise BrepUnsupported("zero-length axis")
    return gp_Dir(float(v.x) / n, float(v.y) / n, float(v.z) / n)


def _rotation_trsf(matrix: np.ndarray) -> "gp_Trsf":
    """A pure rotation as a gp_Trsf, from the same 3×3 the SDF path uses."""
    trsf = gp_Trsf()
    m = np.asarray(matrix, dtype=float)
    trsf.SetValues(m[0, 0], m[0, 1], m[0, 2], 0.0,
                   m[1, 0], m[1, 1], m[1, 2], 0.0,
                   m[2, 0], m[2, 1], m[2, 2], 0.0)
    return trsf


def _transformed(shape, trsf: "gp_Trsf", owner: Optional[str] = None):
    """Move a shape, carrying its entity tags with it when a build is recording.

    A rigid transform is the one stage where history is exactly 1:1 — nothing is
    split, merged or consumed — so a reference to a face survives a translate,
    rotate or mirror unconditionally.
    """
    builder = BRepBuilderAPI_Transform(shape, trsf, True)
    result = builder.Shape()
    return _record(owner, result, builder, [shape]) if owner is not None else result


# ------------------------------------------------------------- primitives --

def _box(f):
    size, center = f.size, f.center
    return BRepPrimAPI_MakeBox(
        gp_Pnt(center.x - size.x / 2.0, center.y - size.y / 2.0, center.z - size.z / 2.0),
        float(size.x), float(size.y), float(size.z)).Shape()


def _sphere(f):
    return BRepPrimAPI_MakeSphere(_pnt(f.center), float(f.radius)).Shape()


def _cylinder_shape(radius: float, height: float, center, axis):
    """A cylinder centred on `center`, matching CylinderSDF.

    OCC builds from the *base*, so the origin is stepped back half the height
    along the axis. Getting this wrong shifts the body by h/2 and every
    differential test against the SDF catches it, which is the point of having
    them.
    """
    direction = _dir(axis)
    half = gp_Vec(direction).Multiplied(float(height) / 2.0)
    base = gp_Pnt(center.x - half.X(), center.y - half.Y(), center.z - half.Z())
    return BRepPrimAPI_MakeCylinder(gp_Ax2(base, direction),
                                    float(radius), float(height)).Shape()


def _cylinder(f):
    return _cylinder_shape(f.radius, f.height, f.center, f.axis)


def _cone(f):
    """Apex at +height/2 along the axis, base radius at -height/2 (ConeSDF)."""
    direction = _dir(f.axis)
    half = gp_Vec(direction).Multiplied(float(f.height) / 2.0)
    base = gp_Pnt(f.center.x - half.X(), f.center.y - half.Y(), f.center.z - half.Z())
    return BRepPrimAPI_MakeCone(gp_Ax2(base, direction),
                                float(f.base_radius), 0.0, float(f.height)).Shape()


# --------------------------------------------------------------- booleans --

def _boolean(f):
    children = [brep_from_feature(c) for c in f.children]
    if not children or any(c is None for c in children):
        raise BrepUnsupported("boolean child has no exact form")

    operation = getattr(f, "operation", "union")

    # Fusing twelve bodies is eleven booleans folded left to right, and editing
    # the twelfth leaves the first ten results identical. The node cache alone
    # cannot see that — the boolean node's own key changed — so each partial
    # result is keyed on the children consumed so far. Adding or tuning the last
    # feature of a part, which is what people spend their time doing, then costs
    # one boolean rather than all of them.
    child_keys = [_subtree_key(c) for c in f.children]
    stem = f"{_owner(f)}|{operation}"

    def prefix(upto: int) -> Optional[str]:
        keys = child_keys[:upto + 1]
        if any(k is None for k in keys):
            return None
        return hashlib.sha256("|".join([stem, *keys]).encode("utf-8")).hexdigest()

    result = children[0]
    for index, other in enumerate(children[1:], start=1):
        key = prefix(index)
        cached = _reuse(key)
        if cached is not None:
            result = cached
            continue
        if operation == "subtract":
            builder = BRepAlgoAPI_Cut(result, other)
        elif operation == "intersect":
            builder = BRepAlgoAPI_Common(result, other)
        else:
            builder = BRepAlgoAPI_Fuse(result, other)
        # OCC booleans genuinely fail on tangencies and near-degenerate input.
        # An unchecked failure returns a null or invalid shape that only shows up
        # much later, so it is turned into a fallback signal here.
        if not builder.IsDone():
            raise BrepUnsupported(f"OCC {operation} failed")
        # A boolean is the one builder that both splits entities and genuinely
        # consumes them, and its history is filled properly — so this is the one
        # place `IsDeleted` is believed.
        result = _record(f, builder.Shape(), builder, [result, other], trust_deleted=True)
        # A boolean files no reference verdicts of its own, so the mark is taken
        # *after* the fold step and the remembered slice is empty. Taking it as
        # zero instead would cache whatever an earlier sibling filed and replay
        # it every time this prefix is reused.
        recorder = _RECORDER.get()
        _remember(key, result, len(recorder.reports) if recorder is not None else 0)
    return result


def _hole(f):
    body = brep_from_feature(f.children[0])
    if body is None:
        raise BrepUnsupported("hole target has no exact form")
    tool = _cylinder_shape(f.radius, f.depth, f.center, f.axis)
    # The tool is a body in its own right as far as naming is concerned: the bore
    # wall a hole leaves is the cylinder's own lateral face, carried through the
    # cut, so a reference to it survives the plate being resized around it.
    _record_map(tool, _seeded(tool, f"{_owner(f)}~tool"))
    builder = BRepAlgoAPI_Cut(body, tool)
    if not builder.IsDone():
        raise BrepUnsupported("OCC hole cut failed")
    return _record(f, builder.Shape(), builder, [body, tool], trust_deleted=True)


# ------------------------------------------------------------- modifiers --

def _transform(f):
    from app.cad_kernel.evaluator.sdf import _euler_matrix

    child = brep_from_feature(f.children[0])
    if child is None:
        raise BrepUnsupported("transform target has no exact form")

    # TransformSDF is world = R @ (local * scale) + t, so the same order here.
    scale = gp_Trsf()
    scale.SetScale(gp_Pnt(0.0, 0.0, 0.0), max(float(f.scale), 1e-6))
    rotate = _rotation_trsf(_euler_matrix(f.rotate.x, f.rotate.y, f.rotate.z))
    translate = gp_Trsf()
    translate.SetTranslation(gp_Vec(float(f.translate.x), float(f.translate.y),
                                    float(f.translate.z)))
    return _transformed(child, translate.Multiplied(rotate.Multiplied(scale)),
                        _owner(f))


_MIRROR_NORMALS = {"xy": (0.0, 0.0, 1.0), "xz": (0.0, 1.0, 0.0), "yz": (1.0, 0.0, 0.0)}


def _mirror(f):
    """Union of the body with its reflection, matching MirrorSDF."""
    child = brep_from_feature(f.children[0])
    if child is None:
        raise BrepUnsupported("mirror target has no exact form")

    nx, ny, nz = _MIRROR_NORMALS.get(getattr(f, "plane", "yz"), (1.0, 0.0, 0.0))
    offset = float(getattr(f, "offset", 0.0))
    origin = gp_Pnt(nx * offset, ny * offset, nz * offset)
    trsf = gp_Trsf()
    trsf.SetMirror(gp_Ax2(origin, gp_Dir(nx, ny, nz)))

    # The reflection's entities are their originals' twins, not the originals, so
    # their tags are suffixed — otherwise a fillet on one corner would silently
    # round the mirrored one too.
    reflected = _transformed(child, trsf, _owner(f))
    _record_map(reflected, _suffix(reflected, "#m"))

    builder = BRepAlgoAPI_Fuse(child, reflected)
    if not builder.IsDone():
        raise BrepUnsupported("OCC mirror fuse failed")
    return _record(f, builder.Shape(), builder, [child, reflected], trust_deleted=True)


def _pattern(f):
    """Linear or circular copies fused together, matching _pattern_transforms."""
    child = brep_from_feature(f.children[0])
    if child is None:
        raise BrepUnsupported("pattern target has no exact form")

    count = max(1, int(f.count))
    copies = []
    if getattr(f, "kind", "linear") == "circular":
        axis = f.axis
        # The SDF picks a single dominant world axis rather than using the vector
        # as given; matching that is what keeps the two kernels in agreement.
        if abs(axis.z) >= abs(axis.x) and abs(axis.z) >= abs(axis.y):
            direction = gp_Dir(0.0, 0.0, 1.0)
        elif abs(axis.y) >= abs(axis.x):
            direction = gp_Dir(0.0, 1.0, 0.0)
        else:
            direction = gp_Dir(1.0, 0.0, 0.0)
        pivot = gp_Ax1(_pnt(f.center), direction)
        for i in range(count):
            trsf = gp_Trsf()
            trsf.SetRotation(pivot, math.radians(360.0 * i / count))
            copies.append(_instance(child, trsf, f, i))
    else:
        for i in range(count):
            trsf = gp_Trsf()
            trsf.SetTranslation(gp_Vec(float(f.axis.x) * float(f.spacing) * i,
                                       float(f.axis.y) * float(f.spacing) * i,
                                       float(f.axis.z) * float(f.spacing) * i))
            copies.append(_instance(child, trsf, f, i))

    result = copies[0]
    for other in copies[1:]:
        builder = BRepAlgoAPI_Fuse(result, other)
        if not builder.IsDone():
            raise BrepUnsupported("OCC pattern fuse failed")
        result = _record(f, builder.Shape(), builder, [result, other], trust_deleted=True)
    return result


def _instance(child, trsf: "gp_Trsf", f, index: int):
    """One copy of a patterned body, its entities marked with the copy number.

    Copy 0 keeps the child's own tags, so a reference taken before the pattern
    was added still names the original. Every other copy is a distinct entity and
    is tagged as one — an agent filleting "the top edge" of instance 2 has to be
    able to say which.
    """
    copy = _transformed(child, trsf, _owner(f))
    if index:
        _record_map(copy, _suffix(copy, f"#{index}"))
    return copy


# --------------------------------------------------------------- extrude --

# Sketch normal -> (u world axis, v world axis, w world axis), matching
# ExtrudeSDF._local.
_EXTRUDE_AXES = {"z": (0, 1, 2), "y": (0, 2, 1), "x": (1, 2, 0)}


def _world_point(u: float, v: float, w: float, normal: str) -> "gp_Pnt":
    iu, iv, iw = _EXTRUDE_AXES[normal]
    coords = [0.0, 0.0, 0.0]
    coords[iu], coords[iv], coords[iw] = u, v, w
    return gp_Pnt(*coords)


def _plane_frame(place, axis_dir=None):
    """``(place, axis_dir)`` for a planar mapping, deriving the normal if absent.

    `place(u, v)` is any affine map from profile coordinates into a plane in
    space, so the plane's normal is the cross product of where it sends the two
    unit steps. Circles and arcs need that normal (a `gp_Circ` is a circle *in a
    frame*); lines do not, which is why the polygon paths never had to care.
    """
    if axis_dir is not None:
        return axis_dir
    origin = place((0.0, 0.0))
    pu = place((1.0, 0.0))
    pv = place((0.0, 1.0))
    du = (pu.X() - origin.X(), pu.Y() - origin.Y(), pu.Z() - origin.Z())
    dv = (pv.X() - origin.X(), pv.Y() - origin.Y(), pv.Z() - origin.Z())
    return gp_Dir(du[1] * dv[2] - du[2] * dv[1],
                  du[2] * dv[0] - du[0] * dv[2],
                  du[0] * dv[1] - du[1] * dv[0])


def _loop_wire(loop: dict, place, axis_dir=None):
    """One closed wire from a loop of typed curves — arcs stay arcs.

    This is what makes a sketched bore a *cylinder* rather than 24 planes, and
    with it the thing `design_spec`'s `hole` requirement, DFM's smallest bore and
    `naming`'s face ids can all see.

    `place` maps a profile ``[u, v]`` to a `gp_Pnt`, so the same builder serves
    an extrude's plane and a revolve's half-plane.
    """
    from OCP.GC import GC_MakeArcOfCircle

    from app.cad_kernel.geom import profile2d

    curves = loop.get("curves") or []
    if not curves:
        raise BrepUnsupported("an empty sketch loop bounds nothing")

    if len(curves) == 1 and curves[0]["kind"] == "circle":
        curve = curves[0]
        circle = gp_Circ(gp_Ax2(place(curve["center"]), _plane_frame(place, axis_dir)),
                         float(curve["r"]))
        return BRepBuilderAPI_MakeWire(BRepBuilderAPI_MakeEdge(circle).Edge()).Wire()

    builder = BRepBuilderAPI_MakeWire()
    for curve in curves:
        if curve["kind"] == "line":
            builder.Add(BRepBuilderAPI_MakeEdge(place(curve["a"]), place(curve["b"])).Edge())
        elif curve["kind"] == "arc":
            # Three-point construction: OCC needs a point *on* the arc to know
            # which way round it goes, and the midpoint of the swept angle is
            # the one that agrees with how the SDF and the tessellator read it.
            centre = curve["center"]
            radius = profile2d.arc_radius(curve)
            a0, da = profile2d.sweep(centre, curve["a"], curve["b"],
                                     bool(curve.get("ccw", True)))
            mid = [centre[0] + radius * math.cos(a0 + da / 2.0),
                   centre[1] + radius * math.sin(a0 + da / 2.0)]
            builder.Add(BRepBuilderAPI_MakeEdge(GC_MakeArcOfCircle(
                place(curve["a"]), place(mid), place(curve["b"])).Value()).Edge())
        elif curve["kind"] == "circle":
            raise BrepUnsupported("a circle is a loop on its own, not one curve of several")
        else:
            raise BrepUnsupported(f"no wire for curve kind {curve['kind']!r}")
    if not builder.IsDone():
        raise BrepUnsupported("the sketch loop does not close")
    return builder.Wire()


def _loops_faces(profile: dict, place, axis_dir=None):
    """Faces for a loops profile — outer wires with their holes cut out.

    Which loop is a hole is decided by `geom.profile2d.faces_of`, the same
    function the distance field's even-odd parity comes from. Deciding it twice
    is how a document ends up meaning one thing with OCC installed and another
    without.
    """
    from OCP.TopoDS import TopoDS

    from app.cad_kernel.geom import profile2d

    faces = []
    for group in profile2d.faces_of(profile):
        maker = BRepBuilderAPI_MakeFace(_loop_wire(group["outer"], place, axis_dir))
        for hole in group["holes"]:
            # Reversed: OCC reads a hole as a wire wound against its outer.
            # `Reversed` hands back a plain TopoDS_Shape, which `Add` will not
            # take — the downcast is not cosmetic.
            maker.Add(TopoDS.Wire_s(_loop_wire(hole, place, axis_dir).Reversed()))
        if not maker.IsDone():
            raise BrepUnsupported("sketch loops do not bound a face")
        faces.append(maker.Face())
    if not faces:
        raise BrepUnsupported("the sketch has no closed loop to build a face from")
    return faces


def _fused(parts, message: str):
    """Fuse a list of shapes into one, or say which builder gave up."""
    if not parts:
        raise BrepUnsupported(message)
    result = parts[0]
    for other in parts[1:]:
        builder = BRepAlgoAPI_Fuse(result, other)
        if not builder.IsDone():
            raise BrepUnsupported(message)
        result = builder.Shape()
    return result


def _extrude_place(normal: str, w: float, u0: float, v0: float):
    def place(point):
        return _world_point(float(point[0]) + u0, float(point[1]) + v0, w, normal)
    iu, iv, iw = _EXTRUDE_AXES[normal]
    axis = [0.0, 0.0, 0.0]
    axis[iw] = 1.0
    return place, gp_Dir(*axis)


def _profile_wire(profile: dict, normal: str, w: float, u0: float, v0: float):
    """A closed wire for a rect/circle/polygon profile, in world coordinates."""
    kind = (profile or {}).get("type", "rect")
    cx = float(profile.get("cx", 0.0)) + u0
    cy = float(profile.get("cy", 0.0)) + v0

    if kind == "circle":
        radius = float(profile.get("radius", 0.02))
        iu, iv, iw = _EXTRUDE_AXES[normal]
        axis = [0.0, 0.0, 0.0]
        axis[iw] = 1.0
        circle = gp_Circ(gp_Ax2(_world_point(cx, cy, w, normal), gp_Dir(*axis)), radius)
        edge = BRepBuilderAPI_MakeEdge(circle).Edge()
        return BRepBuilderAPI_MakeWire(edge).Wire()

    if kind == "polygon":
        points = [(float(p[0]) + u0, float(p[1]) + v0)
                  for p in (profile.get("points") or [])]
        if len(points) < 3:
            raise BrepUnsupported("polygon profile needs at least three points")
        if points[0] == points[-1]:
            points = points[:-1]
    else:
        half_w = float(profile.get("width", 0.05)) / 2.0
        half_h = float(profile.get("height", 0.03)) / 2.0
        points = [(cx - half_w, cy - half_h), (cx + half_w, cy - half_h),
                  (cx + half_w, cy + half_h), (cx - half_w, cy + half_h)]

    polygon = BRepBuilderAPI_MakePolygon()
    for u, v in points:
        polygon.Add(_world_point(u, v, w, normal))
    polygon.Close()
    if not polygon.IsDone():
        raise BrepUnsupported("could not close the profile wire")
    return polygon.Wire()


def _extrude(f):
    """A profile extruded symmetrically about `offset`, matching ExtrudeSDF."""
    from app.cad_kernel.core.planes import plane_normal

    normal = plane_normal(getattr(f, "plane", "xy"))
    depth = float(f.depth)
    if abs(depth) < 1e-12:
        raise BrepUnsupported("zero-depth extrude")

    from app.cad_kernel.geom import profile2d

    origin = getattr(f, "plane_origin", None) or (0.0, 0.0)
    base_w = float(f.offset) - depth / 2.0
    u0, v0 = float(origin[0]), float(origin[1])

    iu, iv, iw = _EXTRUDE_AXES[normal]
    direction = [0.0, 0.0, 0.0]
    direction[iw] = depth

    if profile2d.is_loops(f.profile or {}):
        # A sketch can bound several disjoint faces — two separate outlines, or
        # an island sitting inside a hole. Each is prismed and the results fused,
        # which is the same body the even-odd distance field describes.
        place, axis_dir = _extrude_place(normal, base_w, u0, v0)
        parts = [BRepPrimAPI_MakePrism(face, gp_Vec(*direction)).Shape()
                 for face in _loops_faces(f.profile, place, axis_dir)]
        return _fused(parts, "OCC could not fuse the sketch's regions")

    wire = _profile_wire(f.profile or {}, normal, base_w, u0, v0)
    face = BRepBuilderAPI_MakeFace(wire)
    if not face.IsDone():
        raise BrepUnsupported("profile does not bound a face")
    return BRepPrimAPI_MakePrism(face.Face(), gp_Vec(*direction)).Shape()


# ------------------------------------------------- topology-addressing ops --

def _resolved(shape, references, kind: str, what: str, feature=None):
    """Turn stored references into live sub-shapes, or say which were lost.

    Resolution follows the generative history first and falls back to descriptor
    matching, so a reference to an edge a later cut split names *both* segments
    rather than one of them arbitrarily. The per-reference outcome is filed on the
    recorder: it is only knowable here, while the child body still exists, and
    `check_references` reads it back so an agent can see which of its selections
    an upstream edit destroyed.
    """
    from app.cad_kernel.core import naming

    if not references:
        raise BrepUnsupported(f"no {kind}s selected for {what}")

    outcomes = naming.resolve_references(shape, list(references), kind=kind,
                                         entities=_tags(shape))
    recorder = _RECORDER.get()
    if recorder is not None and feature is not None:
        recorder.reports.append({
            "feature_id": getattr(feature, "id", None),
            "feature": type(feature).__name__,
            "operation": what,
            "kind": kind,
            "references": [{"id": o["id"], "status": o["status"],
                            "resolves_to": len(o["shapes"])} for o in outcomes],
        })

    matched = [sub for outcome in outcomes for sub in outcome["shapes"]]
    if not matched:
        lost = sum(1 for o in outcomes if o["status"] == "deleted")
        detail = (f"{lost} of them destroyed by an upstream edit" if lost
                  else "none of them can be matched on the current body")
        raise BrepUnsupported(
            f"the {len(references)} selected {kind}s no longer exist: {detail}")
    return matched, [o["reference"] for o in outcomes if not o["shapes"]]


def serialize(shape) -> str:
    """An exact shape as a portable string: OCC BRep, deflated, base64'd."""
    import base64
    import io
    import zlib

    from OCP.BRepTools import BRepTools

    buffer = io.BytesIO()
    BRepTools.Write_s(shape, buffer)
    return base64.b64encode(zlib.compress(buffer.getvalue(), 6)).decode("ascii")


def deserialize(payload: str):
    """Rebuild a shape from :func:`serialize`."""
    import base64
    import io
    import zlib

    from OCP.BRep import BRep_Builder
    from OCP.BRepTools import BRepTools
    from OCP.TopoDS import TopoDS_Shape

    shape = TopoDS_Shape()
    raw = zlib.decompress(base64.b64decode(payload))
    BRepTools.Read_s(shape, io.BytesIO(raw), BRep_Builder())
    if shape.IsNull():
        raise BrepUnsupported("stored B-rep payload is empty or unreadable")
    return shape


def _brep_body(f):
    if not getattr(f, "payload", ""):
        raise BrepUnsupported("BrepBodyFeature has no stored geometry")
    return deserialize(f.payload)


def _edge_fillet(f):
    from OCP.BRepFilletAPI import BRepFilletAPI_MakeFillet

    body = brep_from_feature(f.children[0])
    if body is None:
        raise BrepUnsupported("fillet target has no exact form")

    edges, _ = _resolved(body, f.edges, "edge", "fillet", f)
    builder = BRepFilletAPI_MakeFillet(body)
    for edge in edges:
        builder.Add(float(f.radius), edge)
    builder.Build()
    if not builder.IsDone():
        # Overwhelmingly this is a radius larger than the geometry can carry.
        raise BrepUnsupported(
            f"OCC could not build a {f.radius * 1000:g} mm fillet on those edges")
    # `trust_deleted` stays off: BRepFilletAPI reports untouched box edges as
    # deleted, so believing it here would declare live geometry destroyed.
    return _record(f, builder.Shape(), builder, [body])


def _edge_chamfer(f):
    from OCP.BRepFilletAPI import BRepFilletAPI_MakeChamfer

    body = brep_from_feature(f.children[0])
    if body is None:
        raise BrepUnsupported("chamfer target has no exact form")

    edges, _ = _resolved(body, f.edges, "edge", "chamfer", f)
    builder = BRepFilletAPI_MakeChamfer(body)
    for edge in edges:
        builder.Add(float(f.distance), edge)
    builder.Build()
    if not builder.IsDone():
        raise BrepUnsupported(
            f"OCC could not build a {f.distance * 1000:g} mm chamfer on those edges")
    return _record(f, builder.Shape(), builder, [body])


def _face_shell(f):
    from OCP.BRepOffsetAPI import BRepOffsetAPI_MakeThickSolid
    from OCP.TopTools import TopTools_ListOfShape

    body = brep_from_feature(f.children[0])
    if body is None:
        raise BrepUnsupported("shell target has no exact form")

    faces, _ = _resolved(body, f.faces, "face", "shell", f)
    removed = TopTools_ListOfShape()
    for face in faces:
        removed.Append(face)

    # Negative offset hollows inward, so the outer surface stays where the
    # designer put it and the wall grows into the body.
    builder = BRepOffsetAPI_MakeThickSolid()
    builder.MakeThickSolidByJoin(body, removed, -abs(float(f.thickness)), 1e-6)
    builder.Build()
    if not builder.IsDone():
        raise BrepUnsupported(
            f"OCC could not shell to {f.thickness * 1000:g} mm through those faces")
    return _record(f, builder.Shape(), builder, [body])


# Revolve axis name -> (world index along the axis, the two in-plane indices),
# matching `_REV_AXES` in the SDF evaluator.
_REVOLVE_AXES = {"x": (0, 1, 2), "y": (1, 0, 2), "z": (2, 0, 1)}


def _revolve(f):
    """A profile in (radius, height) turned about an axis, matching RevolveSDF.

    The profile is authored in the half-plane r >= 0 and swept symmetrically
    about the radial axis — a 90° revolve runs -45°..+45°, not 0°..90° — so the
    profile is pre-rotated by half the angle before OCC sweeps it forward.
    """
    from OCP.BRepPrimAPI import BRepPrimAPI_MakeRevol

    axis_i, radial_i, third_i = _REVOLVE_AXES.get(getattr(f, "axis", "z"),
                                                  _REVOLVE_AXES["z"])
    profile = f.profile or {}
    min_radius = _profile_min_u(profile)
    if min_radius < 0:
        raise BrepUnsupported("a revolve profile cannot cross its own axis")

    centre = [f.center.x, f.center.y, f.center.z]

    def to_world(u: float, v: float) -> "gp_Pnt":
        coords = list(centre)
        coords[radial_i] += u
        coords[axis_i] += v
        return gp_Pnt(*coords)

    from app.cad_kernel.geom import profile2d

    if profile2d.is_loops(profile):
        # A sketch profile can bound several regions; each is revolved and the
        # results fused, matching the even-odd field `RevolveSDF` evaluates.
        faces = _loops_faces(profile, lambda p: to_world(float(p[0]), float(p[1])))
    else:
        maker = BRepBuilderAPI_MakeFace(_profile_wire_2d(profile, to_world))
        if not maker.IsDone():
            raise BrepUnsupported("revolve profile does not bound a face")
        faces = [maker.Face()]

    direction = [0.0, 0.0, 0.0]
    direction[axis_i] = 1.0
    pivot = gp_Ax1(gp_Pnt(*centre), gp_Dir(*direction))

    angle = float(getattr(f, "angle", 360.0))
    parts = []
    for shape in faces:
        if angle < 359.999:
            start = gp_Trsf()
            start.SetRotation(pivot, math.radians(-angle / 2.0))
            shape = _transformed(shape, start)
            parts.append(BRepPrimAPI_MakeRevol(shape, pivot, math.radians(angle)).Shape())
        else:
            parts.append(BRepPrimAPI_MakeRevol(shape, pivot).Shape())
    return _fused(parts, "OCC could not fuse the revolved regions")


def _sweep(f):
    """A round tube along a polyline, matching SweepSDF.

    SweepSDF is a union of *capsules* — each segment is a cylinder with a
    hemisphere at both ends — which is why this builds cylinders and spheres and
    fuses them rather than reaching for `BRepOffsetAPI_MakePipe`. A pipe has flat
    ends and mitred joints, so it would be a different body at every corner.
    """
    path = [list(map(float, p)) for p in (getattr(f, "path", None) or [])]
    if len(path) < 2:
        raise BrepUnsupported("a sweep needs at least two path points")
    radius = float(f.radius)
    if radius <= 0:
        raise BrepUnsupported("a sweep needs a positive radius")

    parts = []
    for start, end in zip(path, path[1:]):
        delta = [end[i] - start[i] for i in range(3)]
        length = math.sqrt(sum(d * d for d in delta))
        if length < 1e-12:
            continue
        parts.append(BRepPrimAPI_MakeCylinder(
            gp_Ax2(gp_Pnt(*start), gp_Dir(*delta)), radius, length).Shape())
    for point in path:
        parts.append(BRepPrimAPI_MakeSphere(gp_Pnt(*point), radius).Shape())
    if not parts:
        raise BrepUnsupported("sweep path has no length")

    result = parts[0]
    for other in parts[1:]:
        builder = BRepAlgoAPI_Fuse(result, other)
        if not builder.IsDone():
            raise BrepUnsupported("OCC sweep fuse failed")
        result = builder.Shape()
    return result


def _profile_min_u(profile: dict) -> float:
    """Smallest first-axis coordinate of a profile — for a revolve, its radius."""
    from app.cad_kernel.geom import profile2d

    kind = (profile or {}).get("type", "rect")
    if kind == "loops":
        return profile2d.loops_bounds(profile)[0]
    cx = float(profile.get("cx", 0.0))
    if kind == "circle":
        return cx - float(profile.get("radius", 0.02))
    if kind == "polygon":
        points = profile.get("points") or [[0.0, 0.0]]
        return min(float(p[0]) for p in points)
    return cx - float(profile.get("width", 0.04)) / 2.0


def _profile_wire_2d(profile: dict, to_world):
    """A closed wire for a rect/circle/polygon profile, placed by `to_world(u, v)`."""
    kind = (profile or {}).get("type", "rect")
    cx = float(profile.get("cx", 0.0))
    cy = float(profile.get("cy", 0.0))

    if kind == "circle":
        radius = float(profile.get("radius", 0.02))
        # Approximated as a polygon: a circle in a revolve plane needs an axis
        # frame this helper deliberately does not know about, and 64 segments is
        # under a micron of chord error at these sizes.
        points = [(cx + radius * math.cos(2 * math.pi * i / 64),
                   cy + radius * math.sin(2 * math.pi * i / 64)) for i in range(64)]
    elif kind == "polygon":
        points = [(float(p[0]), float(p[1])) for p in (profile.get("points") or [])]
        if len(points) < 3:
            raise BrepUnsupported("polygon profile needs at least three points")
        if points[0] == points[-1]:
            points = points[:-1]
    else:
        half_w = float(profile.get("width", 0.04)) / 2.0
        half_h = float(profile.get("height", 0.03)) / 2.0
        points = [(cx - half_w, cy - half_h), (cx + half_w, cy - half_h),
                  (cx + half_w, cy + half_h), (cx - half_w, cy + half_h)]

    polygon = BRepBuilderAPI_MakePolygon()
    for u, v in points:
        polygon.Add(to_world(u, v))
    polygon.Close()
    if not polygon.IsDone():
        raise BrepUnsupported("could not close the profile wire")
    return polygon.Wire()


def _ruled_loft(f):
    """A genuine ruled surface between two profiles, via `ThruSections`.

    Deliberately *not* wired to `LoftFeature`: `LoftSDF` interpolates distance
    values, which is a different body. See `ops/exact.RuledLoftFeature`.
    """
    from OCP.BRepOffsetAPI import BRepOffsetAPI_ThruSections

    from app.cad_kernel.core.planes import plane_normal

    normal = plane_normal(getattr(f, "plane", "xy"))
    offset_a, offset_b = float(f.offset_a), float(f.offset_b)
    if abs(offset_b - offset_a) < 1e-12:
        raise BrepUnsupported("the two loft sections are at the same height")

    from app.cad_kernel.geom import profile2d

    builder = BRepOffsetAPI_ThruSections(True, bool(getattr(f, "ruled", True)))
    for profile, offset in ((f.profile_a, offset_a), (f.profile_b, offset_b)):
        profile = profile or {}
        if profile2d.is_loops(profile):
            # One wire per section is all `ThruSections` can pair up: a section
            # with a hole or two islands has no single correspondence with the
            # other end, and guessing one would loft to an invented shape.
            loops = profile2d.loops_of(profile)
            if len(loops) != 1:
                raise BrepUnsupported(
                    "a ruled loft section must be a single closed loop, "
                    f"but this one has {len(loops)}")
            place, axis_dir = _extrude_place(normal, offset, 0.0, 0.0)
            builder.AddWire(_loop_wire(loops[0], place, axis_dir))
            continue
        builder.AddWire(_profile_wire(profile, normal, offset, 0.0, 0.0))
    builder.Build()
    if not builder.IsDone():
        raise BrepUnsupported("OCC could not loft between those two profiles")
    return builder.Shape()


def _sheet_metal(f):
    """A base flange plus bent edge flanges, built the way the field defines them.

    `SheetMetalSDF` is not a generic solid and this is not a generic translation
    of one: it is the same construction in exact geometry. Each flange is an
    annular sector welded to the plate edge plus a straight leg tangent to the
    end of that sector, solved in the plane normal to the bend line and extruded
    across the part. Mapping it any other way — a fillet on a corner, say —
    would give a body that differs from the one `flat_pattern` unfolds, and the
    developed blank is the whole reason this feature exists.

    In the local frame ``qa = (a - ca) * out_sign``, ``qz = (z - cz) * up_sign``
    the sector runs from straight down, ``(0, -1)``, rotating toward ``+qa``.
    """
    from OCP.BRepPrimAPI import BRepPrimAPI_MakePrism
    from OCP.gp import gp_Vec

    thickness = float(f.thickness)
    hx, hy = float(f.length) / 2.0, float(f.width) / 2.0
    body = BRepPrimAPI_MakeBox(
        gp_Pnt(-hx, -hy, 0.0), float(f.length), float(f.width), thickness).Shape()

    for flange in f.normalized_flanges():
        axis, sign = flange["edge"][0], flange["edge"][1]
        out_sign = 1.0 if sign == "+" else -1.0
        up_sign = 1.0 if flange["angle"] > 0 else -1.0
        sweep = math.radians(abs(flange["angle"]))
        r_in = float(flange["radius"])
        leg = float(flange["length"])

        edge, half_across = (hx, hy) if axis == "x" else (hy, hx)
        ca = edge * out_sign
        cz = thickness + r_in if up_sign > 0 else -r_in

        def world(qa: float, qz: float, across: float):
            a = ca + qa * out_sign
            z = cz + qz * up_sign
            return gp_Pnt(a, across, z) if axis == "x" else gp_Pnt(across, a, z)

        wire = _flange_profile(r_in, r_in + thickness, sweep, leg,
                               lambda qa, qz: world(qa, qz, -half_across))
        face = BRepBuilderAPI_MakeFace(wire, True).Face()
        vector = (gp_Vec(0.0, 2.0 * half_across, 0.0) if axis == "x"
                  else gp_Vec(2.0 * half_across, 0.0, 0.0))
        body = BRepAlgoAPI_Fuse(body, BRepPrimAPI_MakePrism(face, vector).Shape()).Shape()

    return body


def _flange_profile(r_in: float, r_out: float, sweep: float, leg: float, place):
    """The bend-plus-leg section as a closed wire, via `place(qa, qz)`.

    A zero inside radius is legal — a sharp bend — and collapses the inner arc to
    a point. Building the arc anyway would hand OCC two coincident endpoints.
    """
    from OCP.GC import GC_MakeArcOfCircle

    sin_s, cos_s = math.sin(sweep), math.cos(sweep)
    half = sweep / 2.0
    tan_a, tan_z = cos_s, sin_s

    inner_start = (0.0, -r_in)
    inner_end = (r_in * sin_s, -r_in * cos_s)
    outer_start = (0.0, -r_out)
    outer_end = (r_out * sin_s, -r_out * cos_s)
    leg_inner = (inner_end[0] + leg * tan_a, inner_end[1] + leg * tan_z)
    leg_outer = (outer_end[0] + leg * tan_a, outer_end[1] + leg * tan_z)

    builder = BRepBuilderAPI_MakeWire()

    def line(p, q):
        builder.Add(BRepBuilderAPI_MakeEdge(place(*p), place(*q)).Edge())

    def arc(radius, start, end):
        mid = (radius * math.sin(half), -radius * math.cos(half))
        builder.Add(BRepBuilderAPI_MakeEdge(
            GC_MakeArcOfCircle(place(*start), place(*mid), place(*end)).Value()).Edge())

    if r_in > 1e-12:
        arc(r_in, inner_start, inner_end)
    line(inner_end, leg_inner)
    line(leg_inner, leg_outer)
    line(leg_outer, outer_end)
    arc(r_out, outer_end, outer_start)
    line(outer_start, inner_start)
    return builder.Wire()


_BUILDERS = {
    "RuledLoftFeature": _ruled_loft,
    "SheetMetalFeature": _sheet_metal,
    "BrepBodyFeature": _brep_body,
    "RevolveFeature": _revolve,
    "SweepFeature": _sweep,
    "EdgeFilletFeature": _edge_fillet,
    "EdgeChamferFeature": _edge_chamfer,
    "FaceShellFeature": _face_shell,
    "BoxFeature": _box,
    "SphereFeature": _sphere,
    "CylinderFeature": _cylinder,
    "ConeFeature": _cone,
    "BooleanFeature": _boolean,
    "HoleFeature": _hole,
    "TransformFeature": _transform,
    "MirrorFeature": _mirror,
    "PatternFeature": _pattern,
    "ExtrudeFeature": _extrude,
}


# Features that exist only on this kernel. Falling back to the SDF for one of
# these is pointless — it cannot build them at all — and the fallback destroys
# the real reason, which is usually specific and actionable ("radius too large
# for that edge"), replacing it with a generic "needs the exact kernel".
EXACT_ONLY = {"EdgeFilletFeature", "EdgeChamferFeature", "FaceShellFeature",
              "BrepBodyFeature", "RuledLoftFeature"}


def requires_exact(feature) -> bool:
    """Whether any feature in the tree can only be built by this kernel."""
    if type(feature).__name__ in EXACT_ONLY:
        return True
    return any(requires_exact(child) for child in getattr(feature, "children", []) or [])


def build_strict(feature):
    """Build a tree, raising the real reason on failure instead of returning None."""
    if not AVAILABLE:
        raise BrepUnsupported(
            "this deployment has no exact kernel installed (pip install cadquery-ocp)")
    with _recording():
        return _build(feature)


def supports(feature) -> bool:
    """Whether this whole subtree has an exact representation."""
    name = type(feature).__name__
    if name in UNSUPPORTED or name not in _BUILDERS:
        return False
    return all(supports(child) for child in getattr(feature, "children", []) or [])


def brep_from_feature(feature) -> Optional["TopoDS_Shape"]:
    """Exact geometry for a feature tree, or ``None`` if any part of it is not.

    Never approximates: a tree containing one unsupported feature yields None so
    the caller falls back to the SDF kernel for the whole body, rather than
    showing something subtly different from what the rest of the system computes.

    The public signature is unchanged by generative history: a build always
    records one, because the builders that resolve stored references need it
    mid-build whether or not the caller wants it afterwards. `build_with_history`
    is the same build with the map handed back.
    """
    return build_with_history(feature)[0]


def build_with_history(feature) -> Tuple[Optional["TopoDS_Shape"], Optional[Dict[str, Any]]]:
    """``(shape, history)`` — the exact body and how its topology is named.

    ``history`` is ``{"entities", "references", "error"}``: the tag map for the
    returned shape, what each topology-addressing feature's stored references
    resolved to on the way up, and why the build stopped if it did.

    The reference verdicts are returned **even when the build fails**, which is
    the case that matters most: a fillet whose edges an upstream edit destroyed is
    a build failure whose only useful diagnosis — "the four edges you picked are
    gone" — is collected on the way to that failure and would otherwise be thrown
    away with it.
    """
    if not AVAILABLE:
        return None, None
    with _recording() as recorder:
        shape, error = None, None
        try:
            shape = _build(feature)
        except BrepUnsupported as exc:
            error = str(exc)
        except Exception as exc:  # noqa: BLE001 — OCC raises many exception types
            # Treated exactly like "unsupported": the SDF kernel takes over. A
            # failed boolean must not take a document down with it.
            error = str(exc)
        return shape, {"entities": recorder.get(shape),
                       "references": list(recorder.reports),
                       "error": error}


def _build(feature):
    name = type(feature).__name__
    if name in UNSUPPORTED:
        raise BrepUnsupported(f"{name} has no exact B-rep form")
    builder = _BUILDERS.get(name)
    if builder is None:
        raise BrepUnsupported(f"no B-rep builder for {name}")

    key = _subtree_key(feature)
    cached = _reuse(key)
    if cached is not None:
        return cached
    recorder = _RECORDER.get()
    mark = len(recorder.reports) if recorder is not None else 0

    shape = builder(feature)
    if shape is None or shape.IsNull():
        raise BrepUnsupported(f"{name} produced no shape")
    if recorder is not None and recorder.get(shape) is None:
        # A leaf — a primitive, a sheet-metal body, an imported solid. It has no
        # input to inherit from, so this is where its entities are first named.
        recorder.set(shape, history.seed(shape, _owner(feature), recorder.keys))
    _remember(key, shape, mark)
    return shape


# ------------------------------------------------------------ properties --

def tessellate(shape, resolution: int = 128):
    """A display mesh for an exact shape, as (vertices, faces) numpy arrays.

    Exact geometry has no resolution — but a *picture* of it does, so
    `resolution` keeps its meaning here: how finely to approximate curves for
    display and STL. Deflection is scaled from the body's own diagonal so a
    50 mm part and a 500 mm part come back equally smooth, rather than the
    latter looking faceted at the same setting.
    """
    vertices, faces, _ = _tessellate(shape, resolution)
    return vertices, faces


def tessellate_owned(shape, entities, resolution: int = 128):
    """``(vertices, faces, owners, face_ids)`` — the mesh, and what each triangle is.

    Two labels per triangle, both read from the OCC face it was meshed from and
    both exact — the triangle either came from that face or it did not, with no
    nearest-thing guess anywhere in it:

    * ``owners`` is the **feature** that made it, from the tag `core/history`
      minted at build time. It answers "what did I click on" in the tree.
    * ``face_ids`` is the **face**, the same descriptor id `list_faces` hands
      out and `fillet_edges` / `shell_faces` / `on_face` take. It is what turns
      the sidebar list of coordinates into a click on the part.

    The second one is nearly free: the faces are already being walked to read
    the first, so this is one more label per face carried through the same
    mesh pass rather than a second tessellation.

    Contrast the marching-cubes path, which gets neither: `Kernel.from_mesh`
    produces one `Face` with `surface=None`, so no triangle there can name a
    face and the list stays the only way.
    """
    owners = _face_owners(shape, entities)
    ids = _face_ids(shape)
    per_triangle = _tessellate(shape, resolution, _columns(owners, ids))
    vertices, faces, labels = per_triangle
    if labels is None:
        return vertices, faces, None, None
    return vertices, faces, labels[0], labels[1]


def _columns(*label_lists):
    """Zip several per-face label lists into one list of tuples, or None.

    `_tessellate` maps *one* per-face array onto triangles; carrying two means
    carrying a pair. Returns None only when every column is absent, so a body
    with tags but no nameable faces still gets its feature owners.
    """
    present = [l for l in label_lists if l is not None]
    if not present:
        return None
    length = len(present[0])
    columns = [l if l is not None else [None] * length for l in label_lists]
    return [tuple(values) for values in zip(*columns)]


def _face_ids(shape) -> Optional[List[Optional[str]]]:
    """The naming descriptor id of each face, in the same order as `_face_owners`.

    Deliberately the **id from the descriptor**, not an index into what
    `list_faces` returns: that listing is sorted for stability, so an index into
    it would silently point at a different face the first time two faces swapped
    places. The id is a hash of what the face *is*, which is the thing
    `fillet_edges` resolves and the thing that survives a rebuild.
    """
    from app.cad_kernel.core import naming

    try:
        bounds = naming.body_bounds(shape)
    except Exception as exc:  # noqa: BLE001 — a nameless body is not a failure
        _LOGGER.debug("face ids unavailable for picking: %s", exc)
        return None

    out: List[Optional[str]] = []
    # `naming.faces` rather than `history._explore`: same traversal and so the
    # same order as `_face_owners`, but downcast to `TopoDS_Face`, which is what
    # the surface adaptor takes. The explorer yields the base `TopoDS_Shape`.
    for face in naming.faces(shape):
        try:
            out.append(naming.face_descriptor(face, bounds)["id"])
        except Exception:  # noqa: BLE001 — one unnameable face, not the body
            out.append(None)
    return out


def _face_owners(shape, entities) -> Optional[List[Optional[str]]]:
    """The owning feature id for each face of ``shape``, in `MapShapes` order."""
    if entities is None:
        return None
    out: List[Optional[str]] = []
    for face in history._explore(shape, "face"):
        tag = entities.tag_of(face)
        if not tag:
            out.append(None)
            continue
        # A tag is `{feature_id}:{kind}:{index}`, with a pattern instance's
        # suffix on the end. `~` marks a namespace a feature made for a body of
        # its own — a hole's cutting cylinder — and the bore wall it leaves
        # belongs to the hole, which is the feature someone clicking it means.
        out.append(tag.split(":", 1)[0].split("~", 1)[0])
    return out


def _tessellate(shape, resolution: int = 128,
                labels: Optional[List[tuple]] = None):
    """``(vertices, faces, per_triangle_labels)``. See `tessellate`.

    ``labels`` is one tuple per OCC face — whatever the caller wants to know
    about the triangles that face produces. The return transposes it: a tuple of
    lists, one list per column, each as long as the triangle array.
    """
    from OCP.Bnd import Bnd_Box
    from OCP.BRep import BRep_Tool
    from OCP.BRepBndLib import BRepBndLib
    from OCP.BRepBuilderAPI import BRepBuilderAPI_Copy
    from OCP.BRepMesh import BRepMesh_IncrementalMesh
    from OCP.BRepTools import BRepTools
    from OCP.TopAbs import TopAbs_Orientation, TopAbs_ShapeEnum
    from OCP.TopExp import TopExp
    from OCP.TopLoc import TopLoc_Location
    from OCP.TopoDS import TopoDS
    from OCP.TopTools import TopTools_IndexedMapOfShape

    # Meshed on a copy, because meshing is not a read. `BRepMesh` widens edge and
    # vertex tolerances to cover the deflection it just achieved, and that
    # survives `BRepTools.Clean_s` — so tessellating a body twice at the same
    # resolution returns two different meshes, the second one built against a
    # slightly slackened shape. It never showed while every build was thrown away
    # after one mesh; the moment a shape is kept and reused (see the build cache
    # above, and `document_service._BREP_CACHE`, which has handed out shared
    # bodies all along) it turns into a `mesh_hash` that changes for a document
    # nobody edited. One `BRepBuilderAPI_Copy` costs 1 ms against a 280 ms mesh.
    copier = BRepBuilderAPI_Copy(shape)
    original, shape = shape, copier.Shape()

    # Ownership is known about the *original* faces, and the copy's faces are
    # different objects. `ModifiedShape` is the copier's own answer to which is
    # which — matching by exploration order would be a guess that happens to
    # hold, which is the kind of assumption this file exists to avoid.
    owner_by_face: Optional[List[Optional[tuple]]] = None
    if labels is not None:
        lookup = TopTools_IndexedMapOfShape()
        TopExp.MapShapes_s(shape, TopAbs_ShapeEnum.TopAbs_FACE, lookup)
        owner_by_face = [None] * lookup.Extent()
        for source, owner in zip(history._explore(original, "face"), labels):
            try:
                twin = copier.ModifiedShape(source)
            except Exception:  # noqa: BLE001 — a face the copier does not know
                continue
            if twin is None or twin.IsNull():
                continue
            index = lookup.FindIndex(twin)
            if 1 <= index <= len(owner_by_face):
                owner_by_face[index - 1] = owner

    bounds = Bnd_Box()
    BRepBndLib.Add_s(shape, bounds)
    xmin, ymin, zmin, xmax, ymax, zmax = bounds.Get()
    diagonal = math.sqrt((xmax - xmin) ** 2 + (ymax - ymin) ** 2 + (zmax - zmin) ** 2)
    deflection = max(diagonal / max(int(resolution), 8), 1e-9)
    # Angular deflection has to scale too. Left at OCC's default a cylinder gets
    # the same dozen segments however fine the linear tolerance, so `resolution`
    # would visibly do nothing on exactly the curved surfaces it matters for.
    angle = min(0.5, max(0.03, 8.0 / max(int(resolution), 8)))

    # A shape caches its triangulation, so re-meshing at a different resolution
    # silently returns the old one unless it is cleared first.
    BRepTools.Clean_s(shape)
    BRepMesh_IncrementalMesh(shape, deflection, False, angle, True)

    mapping = TopTools_IndexedMapOfShape()
    TopExp.MapShapes_s(shape, TopAbs_ShapeEnum.TopAbs_FACE, mapping)

    # Read out through `MapNodeArray` / `MapTriangleArray` and `np.fromiter`
    # rather than indexing `Node(n)` / `Triangle(n)` into Python lists. Same
    # numbers — the test compares them — but a mesh of this size crosses into
    # OCC tens of thousands of times, and the indexed form built two throwaway
    # objects per node where iteration builds one.
    vertex_blocks: List[np.ndarray] = []
    face_blocks: List[np.ndarray] = []
    owner_blocks: List[Optional[tuple]] = []
    offset = 0
    for i in range(1, mapping.Extent() + 1):
        face = TopoDS.Face_s(mapping.FindKey(i))
        location = TopLoc_Location()
        triangulation = BRep_Tool.Triangulation_s(face, location)
        if triangulation is None:
            continue
        node_count = triangulation.NbNodes()
        triangle_count = triangulation.NbTriangles()
        if not node_count or not triangle_count:
            continue

        vertices = np.fromiter(
            (c for p in triangulation.MapNodeArray() for c in (p.X(), p.Y(), p.Z())),
            dtype=np.float64, count=node_count * 3).reshape(node_count, 3)
        # Most faces of a built body sit at the identity, and applying it costs
        # a matrix read and a multiply over every node to change nothing.
        if not location.IsIdentity():
            transform = location.Transformation()
            matrix = np.array([[transform.Value(r, c) for c in (1, 2, 3, 4)]
                               for r in (1, 2, 3)], dtype=np.float64)
            vertices = vertices @ matrix[:, :3].T + matrix[:, 3]

        faces = np.fromiter(
            (n for tri in triangulation.MapTriangleArray() for n in tri.Get()),
            dtype=np.int64, count=triangle_count * 3).reshape(triangle_count, 3)
        faces = faces - 1 + offset
        # A face whose orientation is REVERSED has its triangles wound the other
        # way; flipping them keeps every normal pointing out of the solid, which
        # the mesh-validity checks and STL both rely on.
        if face.Orientation() == TopAbs_Orientation.TopAbs_REVERSED:
            faces = faces[:, [0, 2, 1]]

        vertex_blocks.append(vertices)
        face_blocks.append(faces)
        if owner_by_face is not None:
            owner_blocks.extend([owner_by_face[i - 1]] * triangle_count)
        offset += node_count

    if not face_blocks:
        empty = ([[] for _ in range(len(labels[0]))] if labels else [])
        return (np.zeros((0, 3), dtype=float), np.zeros((0, 3), dtype=int),
                empty if owner_by_face is not None else None)
    merged, welded, keep = _weld(np.vstack(vertex_blocks), np.vstack(face_blocks))
    if owner_by_face is None:
        return merged, welded, None
    # `_weld` drops triangles that collapsed to a line; their labels go with
    # them, or the arrays stop lining up with the triangles they describe.
    kept = [o for o, alive in zip(owner_blocks, keep) if alive]
    width = len(labels[0]) if labels else 0
    return merged, welded, [[(row or (None,) * width)[i] for row in kept]
                            for i in range(width)]


def _weld(vertices: np.ndarray, faces: np.ndarray, decimals: int = 9):
    """Merge the duplicate vertices OCC emits along shared edges.

    Each face carries its own triangulation, so every edge vertex appears once
    per adjoining face. Left unmerged the mesh is a pile of disconnected patches:
    watertightness and connected-body checks both fail on geometry that is
    perfectly closed.
    """
    keys = np.round(vertices, decimals) + 0.0
    _, index, inverse = np.unique(keys, axis=0, return_index=True, return_inverse=True)
    merged = vertices[np.sort(index)]

    order = np.argsort(index)
    remap = np.empty(len(index), dtype=np.int64)
    remap[order] = np.arange(len(index))
    faces = remap[inverse.reshape(-1)][faces]

    # Triangles that collapsed to a line or point carry no area and upset
    # winding checks downstream. The mask is returned as well so a caller
    # carrying anything per-triangle can drop the same rows.
    keep = (faces[:, 0] != faces[:, 1]) & (faces[:, 1] != faces[:, 2]) & (faces[:, 0] != faces[:, 2])
    return merged, faces[keep], keep


def volume(shape) -> float:
    props = GProp_GProps()
    BRepGProp.VolumeProperties_s(shape, props)
    return abs(float(props.Mass()))


def surface_area(shape) -> float:
    props = GProp_GProps()
    BRepGProp.SurfaceProperties_s(shape, props)
    return float(props.Mass())


def center_of_mass(shape) -> List[float]:
    props = GProp_GProps()
    BRepGProp.VolumeProperties_s(shape, props)
    p = props.CentreOfMass()
    return [p.X(), p.Y(), p.Z()]


def is_valid(shape) -> bool:
    return bool(BRepCheck_Analyzer(shape).IsValid())
