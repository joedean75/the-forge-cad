"""Assembly mate solver (roadmap feature 04).

Resolves positional constraints — fasten, coincident, concentric, distance —
into each moved component's ``ComponentTransform``, so an assembly is built by
*relationships* ("this face sits on that face", "these two are coaxial") rather
than hand-typed coordinates. This is what text-to-assembly and the UI emit.

Mates assume axis-aligned parts (no relative rotation), which covers the common
stacking/coaxial/attach cases; the moved part inherits the base part's rotation.
Solved in dependency order so a chain (A→B→C) places correctly.
"""

from __future__ import annotations

from typing import Dict, List

from app.models.schemas import AssemblyComponent, AssemblyMate, ComponentTransform, Vector3

_AX = {"x": 0, "y": 1, "z": 2}
MM = 1000.0


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


def solve(components: List[AssemblyComponent], mates: List[AssemblyMate]) -> None:
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
            _apply(mate, base, moved)
            solved.add(mate.moved)
            remaining.remove(mate)
            progressed = True
        if not progressed:
            break


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
