"""Assembly-document HTTP surface.

Thin router over `services.assembly_service`. An assembly composes part documents
(insert parts, place each instance). Owner-scoped like documents: a caller with no
access gets a 404 (existence not leaked).
"""

import logging

from fastapi import APIRouter, Depends, HTTPException

from app.models.schemas import (
    AddComponentRequest,
    AssemblyDoc,
    AssemblySummary,
    CreateAssemblyRequest,
    UpdateAssemblyRequest,
    UpdateComponentRequest,
)
from app.security import current_account
from app.services import accounts, assembly_service as asm

router = APIRouter(prefix="/api/assemblies", tags=["assemblies"])


def owned_assembly(assembly_id: str, acc: accounts.Account = Depends(current_account)) -> AssemblyDoc:
    a = asm.get_assembly(assembly_id)
    if a is None or not asm.can_access(a, acc.id):
        raise HTTPException(status_code=404, detail="Assembly not found")
    return a


@router.post("", response_model=AssemblyDoc)
def create_assembly(body: CreateAssemblyRequest, acc: accounts.Account = Depends(current_account)):
    return asm.create_assembly(name=body.name, owner_id=acc.id)


@router.get("", response_model=list[AssemblySummary])
def list_assemblies(acc: accounts.Account = Depends(current_account)):
    return asm.list_assemblies(owner_id=acc.id)


@router.get("/{assembly_id}", response_model=AssemblyDoc)
def get_assembly(a: AssemblyDoc = Depends(owned_assembly)):
    return a


@router.put("/{assembly_id}", response_model=AssemblyDoc)
def update_assembly(body: UpdateAssemblyRequest, a: AssemblyDoc = Depends(owned_assembly)):
    if body.name is not None:
        return asm.rename_assembly(a.id, body.name)
    return a


@router.delete("/{assembly_id}")
def delete_assembly(a: AssemblyDoc = Depends(owned_assembly)):
    asm.delete_assembly(a.id)
    return {"ok": True}


@router.post("/{assembly_id}/components", response_model=AssemblyDoc)
def add_component(body: AddComponentRequest, a: AssemblyDoc = Depends(owned_assembly)):
    try:
        updated = asm.add_component(a.id, body.part_id)
    except ValueError:
        raise HTTPException(status_code=404, detail="Part not found")
    if updated is None:
        raise HTTPException(status_code=404, detail="Assembly not found")
    return updated


@router.put("/{assembly_id}/components/{component_id}", response_model=AssemblyDoc)
def update_component(
    component_id: str, body: UpdateComponentRequest, a: AssemblyDoc = Depends(owned_assembly)
):
    updated = asm.update_component(a.id, component_id, body.transform)
    if updated is None:
        raise HTTPException(status_code=404, detail="Component not found")
    return updated


@router.delete("/{assembly_id}/components/{component_id}", response_model=AssemblyDoc)
def remove_component(component_id: str, a: AssemblyDoc = Depends(owned_assembly)):
    updated = asm.remove_component(a.id, component_id)
    if updated is None:
        raise HTTPException(status_code=404, detail="Component not found")
    return updated
