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
    # Features the solve ate through (see simp_optimizer._audit_removed_features).
    # Advisory: removing unloaded material is the point, but a part that comes
    # back missing a mounting boss should say so rather than look finished.
    removed_features: List["RemovedFeature"] = []


class RemovedFeature(BaseModel):
    feature_id: Optional[str] = None
    name: str
    type: str
    remaining_fraction: float   # 0..1 of the feature's own volume still solid
    kept: bool = False          # was it flagged must-keep and removed anyway?


class SimulationResult(BaseModel):
    max_stress: float
    max_displacement: float
    safety_factor: float
    compliance: float
    mass: float
    stress_field_url: Optional[str] = None


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
    # Evaluation conditions. Geometry is only comparable across runs when these
    # match: `resolution` is a total voxel budget and `sampler` selects between
    # the Rust and numpy field evaluators, either of which moves the isosurface
    # slightly. Recorded so a mesh, a drawing or a logged trajectory always
    # states what produced it.
    resolution: Optional[int] = None
    sampler: Optional[Literal["native", "numpy"]] = None
    mesh_hash: Optional[str] = None


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


class DocumentRevision(BaseModel):
    """A formal, released revision (feature 09): a letter (A/B/C), an immutable
    feature-tree snapshot, who released it and when, and change notes. Sits above
    working version snapshots — this is what a shop references."""

    id: str
    letter: str
    label: str = ""
    feature_tree: dict
    state: Literal["released", "obsolete"] = "released"
    released_by: Optional[str] = None
    released_at: str
    notes: str = ""


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
    provenance: Literal["text_to_cad", "manual", "generative", "mixed", "imported"] = "manual"
    created_at: str
    updated_at: str
    version_history: List[DocumentVersion] = []
    dimensions: List[Dimension] = []
    # Engineering material bound to the part (see services/materials.py); drives
    # mass, mass properties, simulation, DFM and quotes. None => catalog default.
    material_id: Optional[str] = None
    # Named driving parameters/equations (feature 03); resolved into feature
    # params at evaluation. Ordered so later entries may reference earlier ones.
    parameters: List["DocumentParameter"] = []
    # Named variants (feature 07): each overrides parameter expressions, so one
    # model yields a size family. active_configuration selects the live variant.
    configurations: List["DocumentConfiguration"] = []
    active_configuration: Optional[str] = None
    # GD&T / tolerances (feature 02): a general-tolerance class (ISO 2768),
    # per-feature fit/± callouts, and feature-control frames — rendered on the drawing.
    general_tolerance: Optional[str] = None  # ISO 2768 class: f|m|c|v
    tolerances: List["ToleranceCallout"] = []
    gdt: List["FeatureControlFrame"] = []
    # Revision & release management (feature 09): lifecycle state + released
    # revisions (immutable letter snapshots) above working version history.
    lifecycle_state: Literal["draft", "in_review", "released", "obsolete"] = "draft"
    revisions: List["DocumentRevision"] = []


class DocumentParameter(BaseModel):
    """A named driving parameter or equation on a document (feature 03).

    ``expr`` is a numeric literal or an expression over earlier parameters
    (``"width / 10"``); ``value`` caches the last resolved number.
    """

    name: str
    expr: str
    value: Optional[float] = None
    unit: Literal["mm", "deg", "count", "ratio"] = "mm"


class DocumentConfiguration(BaseModel):
    """A named part-family variant (feature 07): overrides driving-parameter
    expressions by name, e.g. {"width": "120", "bore": "8"}."""

    name: str
    overrides: dict = {}


class ToleranceCallout(BaseModel):
    """A dimensional tolerance on a labelled feature (feature 02).

    ``kind='fit'`` uses ``spec`` as an ISO 286 fit ('H7/g6'); ``kind='pm'`` uses
    ``plus``/``minus`` (mm) about ``nominal_mm``.
    """

    label: str
    nominal_mm: float
    kind: Literal["fit", "pm"] = "fit"
    spec: Optional[str] = None
    plus: float = 0.0
    minus: float = 0.0


class FeatureControlFrame(BaseModel):
    """A GD&T feature-control frame (feature 02)."""

    feature: str
    symbol: str            # flatness | position | perpendicularity | ...
    tolerance_mm: float
    datums: List[str] = []


class DocumentSummary(BaseModel):
    """Lightweight document listing entry (no geometry payload)."""

    id: str
    name: str
    provenance: str
    updated_at: str
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


# --------------------------------------------------------------------------
# Assemblies — a document that composes part documents into a placed whole
# (standard CAD assembly workflow: insert parts, position each instance).
# --------------------------------------------------------------------------


class ComponentTransform(BaseModel):
    """Placement of a component instance in assembly space (metres, degrees)."""

    translate: Vector3 = Field(default_factory=lambda: Vector3(x=0.0, y=0.0, z=0.0))
    rotate_deg: Vector3 = Field(default_factory=lambda: Vector3(x=0.0, y=0.0, z=0.0))
    scale: float = 1.0


class AssemblyComponent(BaseModel):
    """One placed instance of a part document inside an assembly."""

    id: str
    part_id: str  # references a DesignDocument
    name: str  # cached part name for display
    transform: ComponentTransform = Field(default_factory=ComponentTransform)
    # Resolved from the referenced part at read time (not authoritative on disk):
    mesh_url: Optional[str] = None
    bbox: Optional[BBox] = None
    missing: bool = False  # referenced part was deleted


class AssemblyMate(BaseModel):
    """A positional constraint between two components (feature 04).

    Solved into the moved component's ComponentTransform, replacing hand-typed
    placements: ``fasten`` rigidly attaches at an offset; ``coincident`` mates a
    face of each part along an axis (stacking); ``concentric`` makes two parts
    coaxial; ``distance`` holds a gap along an axis. Mates assume axis-aligned
    parts (no relative rotation), covering the common assembly cases.
    """

    id: str
    type: Literal["fasten", "coincident", "concentric", "distance"]
    base: str    # component id held fixed
    moved: str   # component id positioned by the mate
    axis: Literal["x", "y", "z"] = "z"
    base_face: Literal["min", "max", "center"] = "max"
    moved_face: Literal["min", "max", "center"] = "min"
    offset_mm: float = 0.0


class AssemblyDoc(BaseModel):
    id: str
    name: str
    owner_id: Optional[str] = None
    components: List[AssemblyComponent] = []
    mates: List[AssemblyMate] = []
    bbox: Optional[BBox] = None  # resolved envelope over placed components
    created_at: str
    updated_at: str


class AssemblySummary(BaseModel):
    id: str
    name: str
    component_count: int = 0
    updated_at: str
    role: str = "owner"
    shared: bool = False


class CreateAssemblyRequest(BaseModel):
    name: Optional[str] = None


class UpdateAssemblyRequest(BaseModel):
    name: Optional[str] = None


class AddComponentRequest(BaseModel):
    part_id: str


class UpdateComponentRequest(BaseModel):
    transform: ComponentTransform
