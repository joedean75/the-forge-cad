from pydantic import BaseModel, Field
from typing import List, Literal, Optional
from enum import Enum


class Vector3(BaseModel):
    x: float
    y: float
    z: float


class Material(BaseModel):
    id: str
    name: str
    density: float = Field(..., description="kg/m^3")
    yield_strength: float = Field(..., description="MPa")
    youngs_modulus: float = Field(..., description="GPa")
    poissons_ratio: float
    color: str


class DesignSpace(BaseModel):
    size: Vector3
    center: Vector3 = Vector3(x=0, y=0, z=0)


class PreserveRegion(BaseModel):
    id: str
    name: str
    type: Literal["sphere", "cylinder"]
    center: Vector3
    radius: float
    height: Optional[float] = None


class ObstacleRegion(BaseModel):
    id: str
    name: str
    type: Literal["box", "sphere"]
    center: Vector3
    size: Optional[Vector3] = None
    radius: Optional[float] = None


class LoadCase(BaseModel):
    id: str
    name: str
    point: Vector3
    force: Vector3
    load_type: Literal["static", "dynamic", "thermal"] = "static"


class Constraint(BaseModel):
    id: str
    name: str
    type: Literal["fixed", "symmetry", "displacement"]
    point: Vector3
    normal: Optional[Vector3] = None


class Objective(BaseModel):
    target: Literal["minimize_mass", "maximize_stiffness", "minimize_displacement"]
    weight: float = 1.0


class DesignSpec(BaseModel):
    name: str
    design_space: DesignSpace
    preserve_regions: List[PreserveRegion] = []
    obstacle_regions: List[ObstacleRegion] = []
    load_cases: List[LoadCase] = []
    constraints: List[Constraint] = []
    material_id: str
    objective: Objective


class JobStatus(str, Enum):
    PENDING = "pending"
    MESHING = "meshing"
    OPTIMIZING = "optimizing"
    POSTPROCESSING = "postprocessing"
    DONE = "done"
    FAILED = "failed"


class OptimizeJob(BaseModel):
    id: str
    status: JobStatus
    progress: float = Field(0.0, ge=0.0, le=1.0)
    message: str


class CadRepresentation(BaseModel):
    format: str = "brep-v1"
    feature_tree: dict
    step_url: Optional[str] = None


class Candidate(BaseModel):
    id: str
    name: str
    score: float
    mass: float
    stiffness: float
    max_stress: float
    mesh_url: str
    cad: Optional[CadRepresentation] = None


class OptimizeResult(BaseModel):
    job_id: str
    candidates: List[Candidate]


class SimulationResult(BaseModel):
    max_stress: float
    max_displacement: float
    safety_factor: float
    compliance: float
    mass: float
    stress_field_url: Optional[str] = None


class ManufacturingResult(BaseModel):
    build_orientation: str
    build_orientation_vector: Optional[Vector3] = None
    overhang_angle: float
    overhang_area: float
    support_volume: float
    printability_score: int
    print_time_minutes: int
    feature_size_ok: bool
    enclosed_cavities: bool


class NodePart(BaseModel):
    id: str
    name: str
    material: str
    quantity: int
    print_time_minutes: int
    position: Optional[Vector3] = None
    candidate_id: Optional[str] = None
    mesh_url: Optional[str] = None


class Strut(BaseModel):
    id: str
    start: Vector3
    end: Vector3
    diameter: float


class AssemblyResult(BaseModel):
    id: str
    kind: str = "truss"
    nodes: List[NodePart]
    struts: List[Strut]


class AssemblyGenerateRequest(BaseModel):
    prompt: str
    resolution: int = 128


# ---------------------------------------------------------------------------
# Unified design-document model (rearchitecture)
#
# A DesignDocument is the single source of truth: its geometry is always a
# serialized FeatureTree. Manual edits and generative optimization both operate
# on that tree. Everything downstream (simulate / DFM / CAM / machine) consumes
# the document's evaluated body via the DerivedCache.
# ---------------------------------------------------------------------------


class BBox(BaseModel):
    min: Vector3
    max: Vector3


class Quote(BaseModel):
    process: str
    material_cost: float = 0.0
    machine_cost: float = 0.0
    setup_cost: float = 0.0
    total: float = 0.0
    currency: str = "USD"
    lead_time_days: Optional[int] = None


class DfmCheck(BaseModel):
    id: str
    label: str
    status: Literal["pass", "warn", "fail"]
    detail: str = ""
    value: Optional[float] = None


class DfmReport(BaseModel):
    process: str
    checks: List[DfmCheck] = []
    printability_score: int = 0
    quote: Optional[Quote] = None
    # Legacy manufacturing fields, kept optional so ManufacturingResult can be
    # adapted into a DfmReport without loss.
    build_orientation: Optional[str] = None
    build_orientation_vector: Optional[Vector3] = None
    overhang_angle: Optional[float] = None
    overhang_area: Optional[float] = None
    support_volume: Optional[float] = None
    print_time_minutes: Optional[int] = None


class CamResult(BaseModel):
    gcode_url: Optional[str] = None
    operations: List[str] = []
    estimated_minutes: float = 0.0
    stock_size: Optional[Vector3] = None


class DerivedCache(BaseModel):
    """Cached evaluation products for a document body."""

    solid_hash: Optional[str] = None
    mesh_url: Optional[str] = None
    step_url: Optional[str] = None
    bbox: Optional[BBox] = None
    mass: float = 0.0
    volume: float = 0.0
    sim: Optional[SimulationResult] = None
    dfm: Optional[DfmReport] = None
    cam: Optional[CamResult] = None
    error: Optional[str] = None


class OptimizationSetup(BaseModel):
    """Generative-optimization overlay on a document body."""

    design_domain: Literal["from_body", "box"] = "from_body"
    box_size: Optional[Vector3] = None
    box_center: Optional[Vector3] = None
    keep_in: List[PreserveRegion] = []
    keep_out: List[ObstacleRegion] = []
    load_cases: List[LoadCase] = []
    constraints: List[Constraint] = []
    objective: Objective = Field(default_factory=lambda: Objective(target="minimize_mass"))
    volume_fraction: float = Field(0.4, ge=0.05, le=1.0)
    resolution: int = Field(128, ge=32, le=512)
    material_id: str = "al-7075"
    result_cache_id: Optional[str] = None
    inputs_hash: Optional[str] = None


class Dimension(BaseModel):
    """A point-to-point linear dimension anchored in part space (metres)."""

    id: str
    a: Vector3
    b: Vector3
    label: Optional[str] = None  # None -> auto (measured distance in mm)
    source: Literal["user", "llm"] = "user"


class AddDimensionRequest(BaseModel):
    a: Vector3
    b: Vector3
    label: Optional[str] = None
    source: Literal["user", "llm"] = "user"


class DocumentVersion(BaseModel):
    id: str
    label: str
    feature_tree: dict
    parent_id: Optional[str] = None
    author_id: Optional[str] = None
    created_at: str


class VersionSummary(BaseModel):
    """A version-history entry without the (heavy) feature-tree payload."""

    id: str
    label: str
    author_id: Optional[str] = None
    created_at: str


class Collaborator(BaseModel):
    account_id: str
    email: str
    role: Literal["viewer", "editor"] = "viewer"
    added_at: str


class DesignDocument(BaseModel):
    id: str
    name: str
    # Tenant that owns this document. None => legacy/unowned (pre-multi-tenancy),
    # accessible to any authenticated account; every document created after
    # multi-tenancy landed carries an owner and is strictly isolated.
    owner_id: Optional[str] = None
    # Accounts the owner has shared this document with (team collaboration).
    collaborators: List[Collaborator] = []
    units: Literal["mm", "m"] = "m"
    feature_tree: dict
    optimization: Optional[OptimizationSetup] = None
    derived: DerivedCache = Field(default_factory=DerivedCache)
    provenance: Literal["text_to_cad", "manual", "generative", "mixed"] = "manual"
    created_at: str
    updated_at: str
    version_history: List[DocumentVersion] = []
    dimensions: List[Dimension] = []


class DocumentSummary(BaseModel):
    """Lightweight document listing entry (no geometry payload)."""

    id: str
    name: str
    provenance: str
    updated_at: str
    mesh_url: Optional[str] = None
    # The caller's access level: "owner" | "editor" | "viewer".
    role: str = "owner"
    # True when the document is shared with the caller (they are not the owner).
    shared: bool = False


class CreateDocumentRequest(BaseModel):
    name: Optional[str] = None
    prompt: Optional[str] = None
    units: Literal["mm", "m"] = "m"
    resolution: int = Field(128, ge=32, le=512)


class UpdateDocumentRequest(BaseModel):
    name: Optional[str] = None
    feature_tree: Optional[dict] = None
    optimization: Optional[OptimizationSetup] = None
    resolution: int = Field(128, ge=32, le=512)
