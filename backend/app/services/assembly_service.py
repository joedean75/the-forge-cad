"""Assembly documents — compose part documents into a placed whole.

An ``AssemblyDoc`` holds a list of component instances, each referencing a
``DesignDocument`` (a part) plus a placement transform (translate/rotate/scale)
in assembly space. This is the standard CAD assembly workflow: create an empty
assembly, insert parts, position each instance.

Derived fields (each component's ``mesh_url``/``bbox`` and the assembly envelope)
are resolved from the referenced parts at read time — never trusted from disk, so
they stay correct when a part is edited or deleted.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import List, Optional

from app.models.schemas import (
    AssemblyComponent,
    AssemblyDoc,
    AssemblySummary,
    BBox,
    ComponentTransform,
    Vector3,
)
from app.services import document_service, persistence


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _role(asm: AssemblyDoc, account_id: Optional[str]) -> Optional[str]:
    """Access role: owner or None. Legacy (owner_id=None) assemblies are shared."""
    if asm.owner_id is None or asm.owner_id == account_id:
        return "owner"
    return None


def can_access(asm: AssemblyDoc, account_id: Optional[str]) -> bool:
    return _role(asm, account_id) is not None


def _resolve(asm: AssemblyDoc) -> AssemblyDoc:
    """Populate each component's mesh_url/bbox/missing, solve mates, and the envelope."""
    # 1) Resolve each component's part geometry (mesh + part-space bbox).
    for c in asm.components:
        part = document_service.get_document(c.part_id)
        if part is None:
            c.missing = True
            c.mesh_url = None
            c.bbox = None
            continue
        document_service.ensure_derived(part)  # meshes are not persisted
        c.missing = False
        c.name = part.name
        c.mesh_url = part.derived.mesh_url
        c.bbox = part.derived.bbox

    # 2) Solve mates into component transforms (needs bboxes from step 1).
    if asm.mates:
        from app.services import assembly_mates

        assembly_mates.solve(asm.components, asm.mates)

    # 3) Axis-aligned envelope of the placed parts (rotation ignored for the
    #    envelope — good enough for camera-fit; exact placement is rendered).
    lo = [float("inf")] * 3
    hi = [float("-inf")] * 3
    for c in asm.components:
        b = c.bbox
        if b is None:
            continue
        s = c.transform.scale
        t = c.transform.translate
        mins = [b.min.x, b.min.y, b.min.z]
        maxs = [b.max.x, b.max.y, b.max.z]
        offs = [t.x, t.y, t.z]
        for i in range(3):
            lo[i] = min(lo[i], mins[i] * s + offs[i])
            hi[i] = max(hi[i], maxs[i] * s + offs[i])
    asm.bbox = (
        BBox(min=Vector3(x=lo[0], y=lo[1], z=lo[2]), max=Vector3(x=hi[0], y=hi[1], z=hi[2]))
        if hi[0] > lo[0]
        else None
    )
    return asm


def _persist(asm: AssemblyDoc) -> None:
    persistence.save_assembly_doc(asm.id, asm.model_dump())


def _load(asm_id: str) -> Optional[AssemblyDoc]:
    data = persistence.load_assembly_doc(asm_id)
    return AssemblyDoc(**data) if data else None


def create_assembly(name: Optional[str] = None, owner_id: Optional[str] = None) -> AssemblyDoc:
    now = _now()
    asm = AssemblyDoc(
        id=str(uuid.uuid4()),
        name=name or "Untitled Assembly",
        owner_id=owner_id,
        components=[],
        created_at=now,
        updated_at=now,
    )
    _persist(asm)
    return _resolve(asm)


def get_assembly(asm_id: str) -> Optional[AssemblyDoc]:
    asm = _load(asm_id)
    return _resolve(asm) if asm else None


def list_assemblies(owner_id: Optional[str] = None) -> List[AssemblySummary]:
    out: List[AssemblySummary] = []
    for aid in persistence.list_assembly_docs():
        asm = _load(aid)
        if asm is None:
            continue
        role = _role(asm, owner_id) if owner_id is not None else "owner"
        if owner_id is not None and role is None:
            continue
        out.append(
            AssemblySummary(
                id=asm.id,
                name=asm.name,
                component_count=len(asm.components),
                updated_at=asm.updated_at,
                created_at=asm.created_at or asm.updated_at,
                role=role or "owner",
                shared=bool(owner_id is not None and asm.owner_id not in (None, owner_id)),
            )
        )
    out.sort(key=lambda s: s.updated_at, reverse=True)
    return out


def rename_assembly(asm_id: str, name: str) -> Optional[AssemblyDoc]:
    asm = _load(asm_id)
    if asm is None:
        return None
    asm.name = name
    asm.updated_at = _now()
    _persist(asm)
    return _resolve(asm)


def delete_assembly(asm_id: str) -> bool:
    return persistence.delete_assembly_doc(asm_id)


def add_component(asm_id: str, part_id: str) -> Optional[AssemblyDoc]:
    asm = _load(asm_id)
    if asm is None:
        return None
    part = document_service.get_document(part_id)
    # A part the assembly's owner cannot open is not a part they can place.
    # Checked here rather than at each caller because the consequence is not
    # cosmetic: a component is a *reference*, so an assembly holding someone
    # else's document reads its geometry straight back out through the BOM and
    # the interference check. Legacy assemblies carry no owner and are left
    # alone, exactly as `can_access` treats their documents.
    if part is not None and asm.owner_id and not document_service.can_access(part, asm.owner_id):
        part = None
    if part is None:
        raise ValueError("part not found")
    asm.components.append(
        AssemblyComponent(
            id=str(uuid.uuid4()),
            part_id=part_id,
            name=part.name,
            transform=ComponentTransform(),
        )
    )
    asm.updated_at = _now()
    _persist(asm)
    return _resolve(asm)


def update_component(asm_id: str, comp_id: str, transform: ComponentTransform) -> Optional[AssemblyDoc]:
    asm = _load(asm_id)
    if asm is None:
        return None
    found = False
    for c in asm.components:
        if c.id == comp_id:
            c.transform = transform
            found = True
            break
    if not found:
        return None
    asm.updated_at = _now()
    _persist(asm)
    return _resolve(asm)


def add_mate(asm_id: str, mate) -> Optional[AssemblyDoc]:
    """Add a mate constraint and re-solve placements."""
    from app.models.schemas import AssemblyMate

    asm = _load(asm_id)
    if asm is None:
        return None
    m = mate if isinstance(mate, AssemblyMate) else AssemblyMate(**mate)
    m.id = str(uuid.uuid4())
    ids = {c.id for c in asm.components}
    if m.base not in ids or m.moved not in ids:
        raise ValueError("mate references an unknown component")
    if m.base == m.moved:
        raise ValueError("a component cannot mate to itself")
    asm.mates.append(m)
    asm.updated_at = _now()
    _persist(asm)
    return _resolve(asm)


def remove_mate(asm_id: str, mate_id: str) -> Optional[AssemblyDoc]:
    asm = _load(asm_id)
    if asm is None:
        return None
    before = len(asm.mates)
    asm.mates = [m for m in asm.mates if m.id != mate_id]
    if len(asm.mates) == before:
        return None
    asm.updated_at = _now()
    _persist(asm)
    return _resolve(asm)


def remove_component(asm_id: str, comp_id: str) -> Optional[AssemblyDoc]:
    asm = _load(asm_id)
    if asm is None:
        return None
    before = len(asm.components)
    asm.components = [c for c in asm.components if c.id != comp_id]
    if len(asm.components) == before:
        return None
    # Drop any mates that referenced the removed component.
    asm.mates = [m for m in asm.mates if m.base != comp_id and m.moved != comp_id]
    asm.updated_at = _now()
    _persist(asm)
    return _resolve(asm)
