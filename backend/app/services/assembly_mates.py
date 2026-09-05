"""Assembly mate solver.

Two schemes live here, and which one runs is decided by whether the mate names
mate connectors.

**Bounding-box mates** came first: `fasten`, `coincident`, `concentric`,
`distance`, each reading a component's bbox *extremes* along one world axis.
That places a bolted stack and nothing else, and it keeps parts axis-aligned
because there is nothing in it that could express a rotation. Every one of them
still behaves exactly as it did — assemblies are persisted, and a stack that
moves because the solver got cleverer is a regression however much better the
new scheme is.

**Connector mates** are the table those extremes were standing in for. A mate
connector (`cad_kernel/ops/mate_connector.py`) is a named frame *in the part*;
a mate aligns two of them, and every mate type worth having is that same
alignment with a different set of degrees of freedom left free. This is what
lets a part be rotated into place at all, and what makes `revolute` mean
something more than "positioned and then never moved again".

**Degrees of freedom are a property of the constraint, not of the placement.**
A revolute mate leaves one rotation free; the solver still has to *pick* a
value for it. Reporting DOF from the chosen placement would report zero for
everything, which is true of the numbers and false about the assembly.

**`angle_deg` is which value it picks.** Until it existed the answer was always
"the connectors' own alignment", so a hinge could be mated and never opened, a
gear could not be indexed and a servo horn always pointed the same way — the
mate said what could move and then held it at zero. It is the rotational twin
of `offset_mm`, which has always positioned a slider along its rail, and it is
composed in the same place: between the two frames, as
`T_base . F_base . Rz(angle) . offset . F_moved⁻¹`. Driving it **does not**
consume the degree of freedom — a door held open 30° is still a door — so
`degrees_of_freedom` is deliberately unchanged by it.
"""

from __future__ import annotations

import math
from typing import Any, Dict, List, Optional, Tuple

import numpy as np

from app.models.schemas import AssemblyComponent, AssemblyMate, ComponentTransform, Vector3

_AX = {"x": 0, "y": 1, "z": 2}
MM = 1000.0

#: What each mate type leaves free, as `(rotational, translational)`.
#:
#: The three legacy face mates are listed at their *constraint's* freedom, not
#: at what the bbox solver happens to pin down: `coincident` states that a face
#: sits on a face, which removes one translation and two rotations, and the
#: solver's extra decision to centre the other two axes is a resting position
#: rather than part of what was asked for.
FREEDOMS: Dict[str, Tuple[int, int]] = {
    "fasten": (0, 0),
    "revolute": (1, 0),        # a hinge
    "slider": (0, 1),          # a linear rail
    "cylindrical": (1, 1),     # a shaft in a plain bearing
    "planar": (1, 2),          # a face sliding on a face
    "ball": (3, 0),            # a rod end
    "parallel": (1, 3),        # two axes held parallel, nothing else
    # legacy, bbox-based
    "concentric": (1, 1),
    "coincident": (1, 2),
    "distance": (1, 2),
}

#: Mate types that need a connector on each side. The legacy four do not, and
#: may not have one either — their whole definition is the bounding box.
CONNECTOR_TYPES = frozenset({"revolute", "slider", "cylindrical", "planar",
                             "ball", "parallel"})

#: Mate types whose free rotation is *about the mate axis*, and which can
#: therefore be driven to an angle by `AssemblyMate.angle_deg`.
#:
#: This is not simply "leaves a rotation free". `ball` leaves three rotations
#: free and one of them is about the shared Z, so it is here and the other two
#: are still free afterwards. The legacy four are absent for a harder reason
#: than their DOF count: a bounding-box extreme cannot express a rotation at
#: all, so there is no frame for an angle to be measured against and driving one
#: would place the part somewhere invented.
DRIVEN_ROTATION = frozenset({"revolute", "cylindrical", "planar", "ball",
                             "parallel"})

MATE_TYPES = tuple(FREEDOMS)

#: The frames a solve was given: `{part_id: {connector_id: (origin, rotation)}}`
#: in part space. Passed in rather than read here, so this module stays a pure
#: function of its arguments and the document lookup stays in the service that
#: already owns it.
Frames = Dict[str, Dict[str, Tuple[np.ndarray, np.ndarray]]]


def removed(mate_type: str) -> int:
    """How many of the six degrees of freedom a mate of this type removes."""
    rotational, translational = FREEDOMS.get(mate_type, (0, 0))
    return 6 - rotational - translational


def _euler_matrix(rx: float, ry: float, rz: float) -> np.ndarray:
    from app.cad_kernel.evaluator.sdf import _euler_matrix as build

    return build(rx, ry, rz)


def _euler_degrees(rotation: np.ndarray) -> Tuple[float, float, float]:
    """The inverse of `_euler_matrix`, whose convention is ``Rz @ Ry @ Rx``.

    Written out rather than pulled from a library because getting the
    convention wrong is silent: the part lands somewhere plausible and only a
    non-axis-aligned mate shows it.
    """
    sy = float(np.clip(-rotation[2, 0], -1.0, 1.0))
    beta = math.asin(sy)
    if abs(sy) > 1.0 - 1e-9:
        # Gimbal lock: pitch is +-90 and roll/yaw are no longer separable, so
        # fold the whole remaining rotation into yaw and leave roll at zero.
        return (0.0, math.degrees(beta),
                math.degrees(math.atan2(-rotation[0, 1], rotation[1, 1])))
    return (math.degrees(math.atan2(rotation[2, 1], rotation[2, 2])),
            math.degrees(beta),
            math.degrees(math.atan2(rotation[1, 0], rotation[0, 0])))


def _spin(angle_deg: float) -> np.ndarray:
    """Rotation about the connectors' shared Z — the mate's own driven angle.

    Written here rather than reached for from `_euler_matrix(0, 0, angle)`
    because this is a rotation in the *joint's* frame, not a component's world
    orientation, and the two happening to share a formula today is not a reason
    for one to change when the other does.
    """
    if not angle_deg:
        return np.eye(3)
    c, s = math.cos(math.radians(angle_deg)), math.sin(math.radians(angle_deg))
    return np.array([[c, -s, 0.0], [s, c, 0.0], [0.0, 0.0, 1.0]])


def _face_coord(comp: AssemblyComponent, axis: str, face: str) -> float:
    """World coordinate of a component's bbox face along an axis (metres).

    Uses the part-space bbox scaled by the component's uniform scale plus its
    translation; rotation is assumed axis-aligned.
    """
    i = _AX[axis]
    t = comp.transform
    off = (t.translate.x, t.translate.y, t.translate.z)[i]
    if comp.bbox is None:
        return off
    lo = (comp.bbox.min.x, comp.bbox.min.y, comp.bbox.min.z)[i] * t.scale
    hi = (comp.bbox.max.x, comp.bbox.max.y, comp.bbox.max.z)[i] * t.scale
    if face == "min":
        return off + lo
    if face == "max":
        return off + hi
    return off + (lo + hi) / 2.0


def _center_world(comp: AssemblyComponent, axis: str) -> float:
    return _face_coord(comp, axis, "center")


def solve(components: List[AssemblyComponent], mates: List[AssemblyMate],
          frames: Optional[Frames] = None) -> None:
    """Update moved components' transforms in place to satisfy the mates.

    Runs in dependency order: a mate is applied once its base is placed (base
    has no unsolved mate targeting it). Cyclic/unresolvable mates are skipped.
    """
    by_id: Dict[str, AssemblyComponent] = {c.id: c for c in components}
    # A component is "placed" if nothing moves it (no mate names it as moved) or
    # its mate has already been applied.
    solved = {c.id for c in components if c.id not in {m.moved for m in mates}}
    remaining = list(mates)
    for _ in range(len(mates) + 1):
        progressed = False
        for mate in list(remaining):
            base = by_id.get(mate.base)
            moved = by_id.get(mate.moved)
            if base is None or moved is None:
                remaining.remove(mate)
                continue
            if mate.base not in solved:
                continue  # base not placed yet
            pair = _frame_pair(mate, base, moved, frames)
            if pair is not None:
                _apply_connectors(mate, base, moved, *pair)
            else:
                _apply(mate, base, moved)
            solved.add(mate.moved)
            remaining.remove(mate)
            progressed = True
        if not progressed:
            break


def degrees_of_freedom(components: List[AssemblyComponent],
                       mates: List[AssemblyMate]) -> Dict[str, Any]:
    """What each component can still move, and whether anything is over-mated.

    A free body has six degrees of freedom. Each mate naming a component as
    `moved` removes what its type removes; the total is floored at zero, and a
    component whose mates remove more than six is reported as over-constrained
    rather than being silently clamped — two mates can be individually valid and
    jointly impossible, and the count is the cheapest signal that they are.
    """
    rows = []
    for component in components:
        applied = [m for m in mates if m.moved == component.id]
        taken = sum(removed(m.type) for m in applied)
        rows.append({
            "component_id": component.id,
            "name": component.name,
            "dof": max(0, 6 - taken),
            "constrained_by": [m.id for m in applied],
            "over_constrained": taken > 6,
        })
    return {
        "components": rows,
        "dof": sum(r["dof"] for r in rows),
        "over_constrained": [r["component_id"] for r in rows if r["over_constrained"]],
        "grounded": [r["component_id"] for r in rows if r["dof"] == 0],
    }


def _frame_pair(mate: AssemblyMate, base: AssemblyComponent,
                moved: AssemblyComponent, frames: Optional[Frames]):
    """The two part-space frames a connector mate aligns, or None.

    Returns None when either side is unnamed or unresolvable, which sends the
    mate down the bounding-box path — right for the legacy four, and for the
    connector-only types the caller has already refused the mate before it got
    here, so a missing frame at this point means the part was edited and the
    connector deleted. Placing it by bbox is then a visible wrong answer rather
    than an assembly that fails to load.
    """
    base_id = getattr(mate, "base_connector", None)
    moved_id = getattr(mate, "moved_connector", None)
    if not base_id or not moved_id or not frames:
        return None
    base_frame = (frames.get(base.part_id) or {}).get(base_id)
    moved_frame = (frames.get(moved.part_id) or {}).get(moved_id)
    if base_frame is None or moved_frame is None:
        return None
    return base_frame, moved_frame


def _world(component: AssemblyComponent) -> Tuple[np.ndarray, np.ndarray]:
    t = component.transform
    return (np.array([t.translate.x, t.translate.y, t.translate.z]),
            _euler_matrix(t.rotate_deg.x, t.rotate_deg.y, t.rotate_deg.z))


def _apply_connectors(mate: AssemblyMate, base: AssemblyComponent,
                      moved: AssemblyComponent,
                      base_frame, moved_frame) -> None:
    """Place `moved` so its connector lands on the base's.

    The two frames are aligned **directly** — Z onto Z, X onto X — and the mate's
    own joint transform then sits between them: `angle_deg` about that shared Z
    and `offset_mm` along it. Direct alignment rather than the face-to-face
    convention some packages use, because either can be had by defining the
    connector the other way round, and a rule with no hidden flip in it is one a
    caller can predict from the schema alone.

        T_moved . F_moved  =  T_base . F_base . Rz(angle) . offset

    so `T_moved = T_base . F_base . Rz(angle) . offset . F_moved⁻¹`, which is the
    whole solver. Scale is left alone: a mate positions a part, it does not
    resize it.

    The angle turns the *moved* part about the base connector's axis, which is
    the one reading that matches the physical joint: a door swings, its hinge
    does not. Rotating about Z leaves the offset direction untouched, so the two
    commute here — but they are still written in constraint order, because the
    first mate type that offsets along anything but its own axis would otherwise
    inherit a coincidence as if it were a rule.
    """
    base_origin, base_rotation = _world(base)
    fb_origin, fb_rotation = base_frame
    fm_origin, fm_rotation = moved_frame

    # The base connector, in world.
    target_rotation = base_rotation @ fb_rotation
    target_origin = base_origin + base_rotation @ (fb_origin * base.transform.scale)
    # Where the joint sits inside the travel its type leaves free.
    target_rotation = target_rotation @ _spin(getattr(mate, "angle_deg", 0.0))
    # Offset along the shared Z, which is the connector's own primary axis.
    target_origin = target_origin + target_rotation @ np.array(
        [0.0, 0.0, mate.offset_mm / MM])

    rotation = target_rotation @ fm_rotation.T
    translate = target_origin - rotation @ (fm_origin * moved.transform.scale)
    rx, ry, rz = _euler_degrees(rotation)
    moved.transform = ComponentTransform(
        translate=Vector3(x=float(translate[0]), y=float(translate[1]),
                          z=float(translate[2])),
        rotate_deg=Vector3(x=rx, y=ry, z=rz),
        scale=moved.transform.scale,
    )


def _apply(mate: AssemblyMate, base: AssemblyComponent, moved: AssemblyComponent) -> None:
    axis = mate.axis
    i = _AX[axis]
    others = [a for a in ("x", "y", "z") if a != axis]
    offset_m = mate.offset_mm / MM

    t = ComponentTransform(
        translate=Vector3(x=moved.transform.translate.x, y=moved.transform.translate.y, z=moved.transform.translate.z),
        rotate_deg=Vector3(x=base.transform.rotate_deg.x, y=base.transform.rotate_deg.y, z=base.transform.rotate_deg.z),
        scale=moved.transform.scale,
    )
    tr = [t.translate.x, t.translate.y, t.translate.z]

    if mate.type == "fasten":
        # Rigid: moved origin = base origin + offset along axis.
        tr = [base.transform.translate.x, base.transform.translate.y, base.transform.translate.z]
        tr[i] += offset_m
    elif mate.type in ("coincident", "distance"):
        # Align moved_face to base_face (+offset) along the axis; center the rest.
        base_face_w = _face_coord(base, axis, mate.base_face)
        # moved_face world coord as currently placed, minus current translate =
        # the face's local contribution; solve translate so face lands on target.
        moved_local_face = _face_coord(moved, axis, mate.moved_face) - tr[i]
        tr[i] = base_face_w + offset_m - moved_local_face
        for a in others:
            k = _AX[a]
            tr[k] = _center_world(base, a) - (_center_world(moved, a) - tr[k])
    elif mate.type in CONNECTOR_TYPES:
        # A connector mate that reached the bounding-box path: the part was
        # edited and the connector it named is gone. `add_mate` refuses these
        # without connectors, so this is not a call that can be made — it is a
        # part that moved out from under one. The component is left where it
        # was rather than being placed by an invented equivalent, because a
        # revolute is not "concentric, roughly" and a plausible wrong placement
        # is harder to notice than a part that visibly did not move.
        return
    elif mate.type == "concentric":
        # Coaxial: share the axis line (center the two non-axis coords); offset
        # positions along the axis (center-to-center).
        for a in others:
            k = _AX[a]
            tr[k] = _center_world(base, a) - (_center_world(moved, a) - tr[k])
        base_c = _center_world(base, axis)
        moved_local_c = _center_world(moved, axis) - tr[i]
        tr[i] = base_c + offset_m - moved_local_c

    moved.transform = ComponentTransform(
        translate=Vector3(x=tr[0], y=tr[1], z=tr[2]),
        rotate_deg=t.rotate_deg,
        scale=t.scale,
    )
