from pydantic import BaseModel, Field
from typing import Any, Dict, List, Literal, Optional
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
    # Everything that changes what an evaluation would produce — tree, resolution,
    # parameters, configuration. `evaluate_document` skips the work when it still
    # matches, so an edit that cannot move the body does not rebuild one.
    eval_key: Optional[str] = None
    # A URL now, not a payload: `/api/documents/{id}/mesh/{mesh_hash}`. It used
    # to be a base64 data URI, which made the triangles 99% of the document body
    # and re-sent them on every edit.
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
    # "brep" is exact OpenCascade geometry; "native"/"numpy" are the two
    # signed-distance-field samplers.
    sampler: Optional[Literal["brep", "native", "numpy"]] = None
    # Source of bbox, volume, area and geometric mass; distinct from display quality.
    measurement_basis: Optional[Literal["exact", "mesh"]] = None
    # What kind of body this is. A `solid` bounds a volume; a `sheet` is surface
    # with no inside, which is the point of it rather than a defect. Defaulted
    # rather than optional, deliberately: every document stored before surfaces
    # existed holds a solid, and a reader that takes `None` for "unknown" would
    # have to decide what to do about it on every path.
    body_kind: Literal["solid", "sheet"] = "solid"
    # Surface area in m², from the declared measurement basis. Stands in for volume
    # on a sheet, and worth having on a solid too — it is what a coating, a
    # finish or a heat-transfer estimate is quoted against.
    area: float = 0.0
    mesh_hash: Optional[str] = None
    # The feature tree with its parameter *expressions* substituted — the same
    # nodes and the same ids, but every `"=length/2"` replaced by the number it
    # resolved to. Present only when the document has parameters at all.
    #
    # `feature_tree` deliberately keeps the expression: that is what a parameter
    # is, and the inspector has to show it. But everything that reads the tree as
    # *geometry* — snap points, selection outlines, drag handles — wants numbers,
    # and a client that does its own arithmetic here would be a second, drifting
    # copy of the evaluator. This is derived, so it never enters `eval_key`.
    numeric_tree: Optional[Dict[str, Any]] = None


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


class AnalysisProduct(BaseModel):
    """One derived reading of the body, with the inputs it was a reading *of*.

    The panels used to hold these client-side and clear them on every edit,
    which is right for the tab that made the edit and useless after a reload or
    for a second viewer. Stored against the document with a fingerprint, the
    question "is this report about the part I am looking at?" has one answer
    that everybody gets — see `services/derived_cache`.
    """

    kind: str                       # analysis | dfm | cam | spec | drawing
    #: `derived_cache.analysis_fingerprint` of the inputs that produced it.
    fingerprint: str
    computed_at: str
    result: Dict[str, Any] = {}
    #: True when the result was too large to carry inline. A drawing sheet or a
    #: G-code program is an artifact with a URL, not a field on the document.
    truncated: bool = False


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
    # Which standard part this document *is*, when it came from the catalog
    # (`services/parts_catalog`). One document per (key, owner), reused, so the
    # BOM says qty 8 rather than printing eight lines — see
    # `services/standard_parts`.
    catalog_ref: Optional[str] = None
    # The natural-language request this document came from. Previously the prompt
    # was used to derive a name, logged at INFO, and then dropped — so the one
    # piece of text that says what the part is *for* was unrecoverable, and no
    # generated document could ever be paired with its own instruction.
    source_prompt: Optional[str] = None
    # The recorded operation sequence that produced this document, when there is
    # one (a copilot run, a replayed trajectory).
    trajectory_id: Optional[str] = None
    # What the part is supposed to be: measurable requirements the body is scored
    # against (see services/design_spec.py). Left free-form here and validated
    # there, because the requirement kinds are a property of what the kernel can
    # measure and grow with it.
    spec: Optional[dict] = None
    created_at: str
    updated_at: str
    version_history: List[DocumentVersion] = []
    dimensions: List[Dimension] = []
    # Engineering material bound to the part (see services/materials.py); drives
    # mass, mass properties, simulation, DFM and quotes. None => catalog default.
    material_id: Optional[str] = None
    # A mass someone measured or read off a datasheet, in grams. Beats both the
    # geometry and the catalog, because it is a statement about the actual
    # object rather than about a model of it — see `services/part_mass`.
    #
    # Only meaningful for a part whose geometry is a deliberate simplification:
    # a purchased motor, an ESC, a battery. Setting it on a bracket you modelled
    # is how a printed part comes to disagree with its own volume.
    mass_override_g: Optional[float] = None
    # Electrical metadata for a document that *is* a battery pack: cells,
    # chemistry, nominal volts and capacity. Recorded rather than derived —
    # pack dimensions are not standardised, so nothing here can be inferred
    # from anything else, and a mass budget that also knows the pack's energy
    # is what a flight-time or run-time estimate needs. See `add_battery`.
    battery: Optional[Dict[str, Any]] = None
    # How the part moves: which shaft turns about what, which way a gear runs,
    # what bearing sits in which pocket, what screw goes in which hole.
    #
    # Recorded on the document rather than drawn on the sheet, so one statement
    # reaches the drawing, the BOM note and anything else that asks. A rotation
    # arrow retyped onto a sheet is one that disagrees with the model the first
    # time somebody mirrors the part. See `services/mechanism`.
    mechanism: List[Dict[str, Any]] = []
    # How the part is meant to be made: the process, the machine's assumptions
    # (bed, nozzle, layer), the hardware it is designed around and any notes for
    # whoever makes it.
    #
    # An *intention*, not a measurement — which is exactly why it has to be
    # written down. A release package that does not state the nozzle it was
    # designed for is a package whose wall-thickness numbers cannot be checked
    # by the person receiving it. See `set_manufacturing` and
    # `services/printability`.
    manufacturing: Optional[Dict[str, Any]] = None
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
    # How many times this document has been written. Bumped by every persisted
    # mutation, including ones that cannot move the body (a rename, a material).
    #
    # Distinct from `derived.eval_key`, and the distinction is load-bearing: a
    # revision counts *writes*, so two clients can tell they saw the same
    # document and a write can refuse to land on a version it did not read; a
    # fingerprint hashes *inputs*, so a rename does not recompute geometry.
    # Conflating them makes either a rename rebuild the body or a lost update
    # invisible.
    revision: int = 0
    # Derived readings of the body (FEA, DFM, CAM, spec), each carrying the
    # fingerprint of the inputs that produced it. See `services/derived_cache`.
    analysis: Dict[str, AnalysisProduct] = {}


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
    created_at: str = ""
    # What the document was asked for, when it was asked for in words. Often the
    # only thing the user remembers about a generated part, so the library
    # searches it alongside the name.
    source_prompt: Optional[str] = None
    # The caller's access level: "owner" | "editor" | "viewer".
    role: str = "owner"
    # True when the document is shared with the caller (they are not the owner).
    shared: bool = False
    # What the library card shows about the part itself. Two documents were
    # indistinguishable in a list until one of them was opened; all of this is
    # extracted in the same SQL pass that already ran.
    lifecycle_state: str = "draft"
    material_id: Optional[str] = None
    mass_kg: Optional[float] = None
    revision: Optional[str] = None
    # The geometry's content hash, which is what a thumbnail is a picture of.
    # None when the document has never been evaluated.
    mesh_hash: Optional[str] = None


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
    # The revision the caller read before making this edit. When given, the write
    # is refused with 409 if the document has moved on — a whole-tree PUT is the
    # write most likely to clobber somebody, because it replaces everything.
    expected_revision: Optional[int] = None


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
    """A positional constraint between two components.

    Two schemes, and the mate says which by whether it names connectors.

    **Bounding-box mates** read a component's bbox *extremes* along one world
    axis: ``fasten`` rigidly attaches at an offset; ``coincident`` mates a face
    of each part (stacking); ``concentric`` makes two parts coaxial;
    ``distance`` holds a gap. They keep parts axis-aligned, because there is
    nothing in an extreme that could express a rotation. Unchanged, and stored
    assemblies depend on them staying that way.

    **Connector mates** name a `MateConnectorFeature` on each part — a frame
    that lives in the part rather than on its silhouette — and align the two.
    ``revolute`` (a hinge), ``slider`` (a rail), ``cylindrical`` (a shaft in a
    bearing), ``planar``, ``ball`` and ``parallel`` are all that same alignment
    with a different set of degrees of freedom left free, which is also the only
    way a part gets *rotated* into place here.
    """

    id: str
    type: Literal["fasten", "coincident", "concentric", "distance",
                  "revolute", "slider", "cylindrical", "planar", "ball", "parallel"]
    base: str    # component id held fixed
    moved: str   # component id positioned by the mate
    axis: Literal["x", "y", "z"] = "z"
    base_face: Literal["min", "max", "center"] = "max"
    moved_face: Literal["min", "max", "center"] = "min"
    offset_mm: float = 0.0
    #: Where the mate sits inside the rotation it leaves free, in degrees about
    #: the connectors' shared axis. A revolute mate is a hinge, and a hinge has
    #: a position; without this it always landed at the connectors' own
    #: alignment, so a door could be mated but never opened. Only meaningful
    #: for a connector mate whose type leaves a rotation about that axis free
    #: (`assembly_mates.DRIVEN_ROTATION`) — anywhere else it would be a
    #: violation of the constraint rather than a pose within it, and `add_mate`
    #: refuses it. Driving it does **not** consume the degree of freedom: it
    #: picks a value inside one.
    angle_deg: float = 0.0
    #: Feature ids of a `MateConnectorFeature` in each part. Both or neither:
    #: one alone has nothing to align against, so the mate falls back to the
    #: bounding-box path and the caller is told rather than left guessing.
    base_connector: Optional[str] = None
    moved_connector: Optional[str] = None


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
    created_at: str = ""
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
