from fastapi import APIRouter, HTTPException
from fastapi.responses import PlainTextResponse

from app.cad_kernel.core.feature_tree import FeatureTree
from app.cad_kernel.kernel import Kernel
from app.cad_kernel.ops.booleans import BooleanFeature
from app.services.cad_service import solid_to_step_string
from app.services.mesh_generator import get_candidate_entry
from app.services.assembly_generator import get_assembly

router = APIRouter(prefix="/api/export", tags=["export"])


def _ensure_feature_tree(value) -> FeatureTree:
    if isinstance(value, FeatureTree):
        return value
    if isinstance(value, dict):
        return FeatureTree.from_dict(value)
    raise ValueError(f"Cannot convert {type(value).__name__} to FeatureTree")


def _get_solid(data: dict, resolution: int = 128):
    """Return a kernel solid for a candidate, evaluating lazily if needed."""
    solid = data.get("kernel_solid")
    if solid is not None:
        return solid

    feature_tree = data.get("feature_tree")
    if feature_tree is not None:
        tree = _ensure_feature_tree(feature_tree)
        solid = Kernel.evaluate_tree(tree, resolution=resolution)
        data["kernel_solid"] = solid
        return solid

    candidate_mesh = data.get("candidate_mesh")
    if candidate_mesh is not None:
        mesh = candidate_mesh
        solid = Kernel.from_mesh(mesh.vertices, mesh.faces)
        data["kernel_solid"] = solid
        return solid

    return None


@router.get("/{candidate_id}/step", response_class=PlainTextResponse)
def export_step(candidate_id: str):
    data = get_candidate_entry(candidate_id)
    if data is None:
        raise HTTPException(status_code=404, detail="Candidate not found")

    solid = _get_solid(data)
    if solid is None:
        raise HTTPException(status_code=404, detail="Candidate geometry not available")

    step_text = solid_to_step_string(solid, name=f"candidate_{candidate_id}")
    return PlainTextResponse(
        step_text,
        media_type="application/step",
        headers={"Content-Disposition": f"attachment; filename={candidate_id}.step"},
    )


@router.get("/assembly/{assembly_id}/step", response_class=PlainTextResponse)
def export_assembly_step(assembly_id: str):
    assembly = get_assembly(assembly_id)
    if assembly is None:
        raise HTTPException(status_code=404, detail="Assembly not found")

    features = []
    for node in assembly.nodes:
        node_data = get_candidate_entry(node.candidate_id or "")
        if node_data is None:
            continue
        feature_tree = node_data.get("feature_tree")
        if feature_tree is None:
            continue
        tree = _ensure_feature_tree(feature_tree)
        features.append(tree.root)

    if not features:
        raise HTTPException(status_code=404, detail="Assembly geometry not available")

    root = BooleanFeature(operation="union", children=features) if len(features) > 1 else features[0]
    solid = Kernel.evaluate_tree(FeatureTree(root), resolution=128)
    step_text = solid_to_step_string(solid, name=f"assembly_{assembly_id}")
    return PlainTextResponse(
        step_text,
        media_type="application/step",
        headers={"Content-Disposition": f"attachment; filename={assembly_id}.step"},
    )
