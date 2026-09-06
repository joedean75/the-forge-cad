export interface Vector3 {
  x: number
  y: number
  z: number
}

export interface Material {
  id: string
  name: string
  density: number
  yield_strength: number
  youngs_modulus: number
  poissons_ratio: number
  color: string
}

export interface DesignSpace {
  size: Vector3
  center: Vector3
}

export interface PreserveRegion {
  id: string
  name: string
  type: 'sphere' | 'cylinder'
  center: Vector3
  radius: number
  height?: number
}

export interface ObstacleRegion {
  id: string
  name: string
  type: 'box' | 'sphere'
  center: Vector3
  size?: Vector3
  radius?: number
}

export interface LoadCase {
  id: string
  name: string
  point: Vector3
  force: Vector3
  load_type: 'static' | 'dynamic' | 'thermal'
}

export interface Constraint {
  id: string
  name: string
  type: 'fixed' | 'symmetry' | 'displacement'
  point: Vector3
  normal?: Vector3
}

export interface Objective {
  target: 'minimize_mass' | 'maximize_stiffness' | 'minimize_displacement'
  weight: number
}

export interface DesignSpec {
  name: string
  design_space: DesignSpace
  preserve_regions: PreserveRegion[]
  obstacle_regions: ObstacleRegion[]
  load_cases: LoadCase[]
  constraints: Constraint[]
  material_id: string
  objective: Objective
}

export type JobStatus = 'pending' | 'meshing' | 'optimizing' | 'postprocessing' | 'done' | 'failed'

export interface OptimizeJob {
  id: string
  status: JobStatus
  progress: number
  message: string
  /** Features the solve ate through — advisory, see RemovedFeature. */
  removed_features?: RemovedFeature[]
}

/** A feature that lost most of its material during optimization. */
export interface RemovedFeature {
  feature_id?: string
  name: string
  type: string
  /** 0..1 of the feature's own volume still solid after the solve. */
  remaining_fraction: number
  /** True if it was flagged must-keep and removed anyway. */
  kept: boolean
}

export interface SimulationResult {
  max_stress: number
  max_displacement: number
  safety_factor: number
  compliance: number
  mass: number
  stress_field_url?: string
}

/**
 * Everything the `simulate` operation returns — a superset of `SimulationResult`,
 * which is what the older `POST /simulate` endpoint narrows it down to.
 *
 * `solver` and `estimate_only` are the fields that matter and the reason the UI
 * calls the operation instead: the closed-form fallback is `total_force /
 * volume^(2/3)` clamped to a range, which cannot tell a cube from a cantilever.
 * Presented as an analysis it is worse than no analysis at all.
 */
export interface AnalysisReport extends SimulationResult {
  solver: string
  estimate_only?: boolean
  /** Why the real solve did not run, when it did not. */
  reason?: string
  material?: string
  /** The stable percentile figure — context, not the number to size a part from. */
  stress_p99?: number
  mesh?: { elements: number; grid: [number, number, number]; element_size_mm: number }
  grounded_on?: string
  notes?: string[]
}

export interface LlmConfig {
  configured: boolean
  provider: string
  base_url: string
  model: string
  available_models: string[]
}

// ---------------------------------------------------------------------------
// Unified design-document model (mirrors backend/app/models/schemas.py)
// ---------------------------------------------------------------------------

export interface BBox {
  min: Vector3
  max: Vector3
}

export interface Quote {
  process: string
  material_cost: number
  machine_cost: number
  setup_cost: number
  total: number
  currency: string
  lead_time_days?: number
}

export interface DfmCheck {
  id: string
  label: string
  status: 'pass' | 'warn' | 'fail'
  detail: string
  value?: number
}

/** What the part will be made by. 'additive' reports overhang/support/quote;
 *  the rest report topology-measured manufacturability and carry no quote. */
export type DfmProcess = 'additive' | 'milling' | 'molding' | 'casting'

export interface DfmReport {
  process: string
  checks: DfmCheck[]
  printability_score: number
  quote?: Quote
  build_orientation?: string
  build_orientation_vector?: Vector3
  overhang_angle?: number
  overhang_area?: number
  support_volume?: number
  print_time_minutes?: number
}

export interface CamResult {
  gcode_url?: string
  operations: string[]
  estimated_minutes: number
  stock_size?: Vector3
}

export interface DerivedCache {
  solid_hash?: string
  /** Everything that changes what an evaluation would produce. An edit whose key
   *  still matches does not rebuild the body — see `document_service`. */
  eval_key?: string
  /** `/api/documents/{id}/mesh/{mesh_hash}` — a URL, not a payload. Immutable
   *  and cached forever, because the hash *is* the identity of the triangles. */
  mesh_url?: string
  mesh_hash?: string
  step_url?: string
  bbox?: BBox
  mass: number
  volume: number
  /** The conditions the geometry was produced under. Two meshes are only
   *  comparable when these match. */
  resolution?: number
  sampler?: 'brep' | 'native' | 'numpy'
  sim?: SimulationResult
  dfm?: DfmReport
  cam?: CamResult
  error?: string
  /** The feature tree with parameter *expressions* substituted — same nodes,
   *  same ids, `"=length/2"` replaced by the number it resolved to. Present
   *  only when the document has parameters. `feature_tree` keeps the
   *  expression, because that is the design intent and the inspector shows it;
   *  anything reading the tree as geometry wants this. See `geometryTree`. */
  numeric_tree?: FeatureTreeDict | null
}

export interface OptimizationSetup {
  design_domain: 'from_body' | 'box'
  box_size?: Vector3
  box_center?: Vector3
  keep_in: PreserveRegion[]
  keep_out: ObstacleRegion[]
  load_cases: LoadCase[]
  constraints: Constraint[]
  objective: Objective
  volume_fraction: number
  resolution: number
  material_id: string
  result_cache_id?: string
  inputs_hash?: string
}

export interface DocumentVersion {
  id: string
  label: string
  feature_tree: object
  parent_id?: string
  author_id?: string | null
  created_at: string
}

export interface VersionSummary {
  id: string
  label: string
  author_id?: string | null
  created_at: string
}

export interface Collaborator {
  account_id: string
  email: string
  role: 'viewer' | 'editor'
  added_at: string
}

export interface Dimension {
  id: string
  a: Vector3
  b: Vector3
  label?: string
  source: 'user' | 'llm'
}

export type DocumentProvenance = 'text_to_cad' | 'manual' | 'generative' | 'mixed' | 'imported'

export interface DesignDocument {
  id: string
  name: string
  /** Owning tenant; null for legacy documents created before multi-tenancy. */
  owner_id?: string | null
  collaborators: Collaborator[]
  units: 'mm' | 'm'
  feature_tree: FeatureTreeDict
  optimization?: OptimizationSetup
  derived: DerivedCache
  provenance: DocumentProvenance
  /** The natural-language request this document came from, when there was one. */
  source_prompt?: string | null
  /** The recorded operation sequence that produced it (copilot run, replay). */
  trajectory_id?: string | null
  /** Measurable requirements the body is scored against (services/design_spec). */
  spec?: PartSpec | null
  created_at: string
  updated_at: string
  version_history: DocumentVersion[]
  dimensions: Dimension[]
  /** Engineering material bound to the part (drives mass/sim/DFM/quote). */
  material_id?: string | null
  /** Named driving parameters/equations (design intent). */
  parameters?: DocumentParameter[]
  /** Part-family variants overriding parameters. */
  configurations?: DocumentConfiguration[]
  active_configuration?: string | null
  /** GD&T / tolerances (feature 02). */
  general_tolerance?: string | null
  tolerances?: ToleranceCallout[]
  gdt?: FeatureControlFrame[]
  /** Revision & release (feature 09). */
  lifecycle_state?: 'draft' | 'in_review' | 'released' | 'obsolete'
  revisions?: DocumentRevisionMeta[]
  /**
   * How many times this document has been written — bumped by every persisted
   * mutation, including ones that cannot move the body (a rename, a material).
   *
   * Pass it back as `expected_revision` on a write to get a 409 instead of
   * silently overwriting a change you never saw. Distinct from
   * `derived.eval_key`, which hashes the *inputs an evaluation reads*: a rename
   * moves the revision and not the fingerprint, which is what stops renaming a
   * part from rebuilding its body. See docs/FINGERPRINTS.md.
   */
  revision?: number
  /** Derived readings of the body, each carrying the fingerprint of the inputs
   *  that produced it. Keyed by kind: analysis | dfm | cam | spec | drawing. */
  analysis?: Record<string, AnalysisProduct>
}

/**
 * One stored reading of the body, and what it was a reading *of*.
 *
 * The panels used to hold these client-side and clear them on every edit, which
 * is right for the tab that made the edit and useless after a reload or for a
 * second viewer. Stored against the document with a fingerprint, "is this report
 * about the part I am looking at?" has one answer that everybody gets.
 */
export interface AnalysisProduct {
  kind: string
  /** `derived_cache.analysis_fingerprint` of the inputs that produced it. */
  fingerprint: string
  computed_at: string
  result: Record<string, unknown>
  /** True when the result was too large to carry inline — a drawing sheet or a
   *  G-code program is an artifact with a URL, not a field on the document. */
  truncated?: boolean
}

/** What the part is supposed to be. Every kind is something the kernel can
 *  measure, so `verify_against_spec` scores it mechanically. */
export interface SpecRequirement {
  id?: string
  kind: 'bounding_box' | 'mass' | 'volume' | 'material' | 'watertight' | 'bodies' | 'hole' | 'feature'
  label?: string
  /** bounding_box: which extent. */
  axis?: 'x' | 'y' | 'z'
  /** Nominal with optional ± tolerance, or a min/max range. */
  value?: number | string
  tolerance?: number
  min?: number
  max?: number
  /** hole: bore diameter in mm, how many, and how close counts. */
  diameter?: number
  diameter_tolerance?: number
  count?: number
}

/**
 * What the part is supposed to be, stored on `DesignDocument.spec`.
 *
 * Named `PartSpec` rather than `DesignSpec` because that name is already taken
 * by the generative-optimization spec above, and TypeScript *merges* two
 * interfaces of the same name rather than complaining — so declaring both left a
 * single type demanding `name`, `design_space` and `requirements` at once, which
 * nothing on either side of the wire ever sends.
 */
export interface PartSpec {
  goal?: string
  requirements: SpecRequirement[]
}

// ---------------------------------------------------------------------------
// Addressable topology (the exact kernel). `list_faces` / `list_edges` rows.
// ---------------------------------------------------------------------------

/** A selectable face. `id` is stable across rebuilds — see cad_kernel/core/naming. */
export interface FaceRef {
  id: string
  kind: 'plane' | 'cylinder' | 'cone' | 'sphere' | 'torus' | 'bspline' | 'bezier' | 'other'
  area_mm2: number
  center_mm: [number, number, number]
  radius_mm?: number
  normal?: [number, number, number]
}

export interface EdgeRef {
  id: string
  kind: 'line' | 'circle' | 'ellipse' | 'bspline' | 'bezier' | 'other'
  length_mm: number
  midpoint_mm: [number, number, number]
  radius_mm?: number
}

/**
 * What became of a stored selection after an upstream edit.
 *
 * `split` is not a failure — a cut that divides an edge leaves the reference
 * naming both halves, and the fillet is applied to both. `deleted` and
 * `unresolved` are: the body still builds, and is simply no longer the part that
 * was asked for.
 */
export type ReferenceStatus = 'matched' | 'split' | 'deleted' | 'unresolved'

export interface ReferenceRow {
  id: string
  status: ReferenceStatus
  resolves_to: number
  meaning: string
}

export interface ReferenceReport {
  features: Array<{
    feature_id: string | null
    feature: string
    operation: string
    kind: 'face' | 'edge'
    references: ReferenceRow[]
  }>
  lost: number
  split: number
  ok: boolean
  error?: string
  message: string
}

// ── Sketching ────────────────────────────────────────────────────────────────

/** Mirrors `services/sketch_solver`'s residual table. */
export type ConstraintType =
  | 'fixed' | 'coincident' | 'horizontal' | 'vertical' | 'distance'
  | 'parallel' | 'perpendicular' | 'equal_length' | 'radius' | 'point_on_line'
  | 'arc_radius' | 'tangent' | 'concentric' | 'symmetric' | 'angle'
  | 'midpoint' | 'equal_radius' | 'point_on_circle'

export interface SketchPoint { id: string; x: number; y: number }
export interface SketchLine { id: string; a: string; b: string; construction?: boolean }
export interface SketchArc {
  id: string; a: string; b: string; center: string; ccw: boolean; construction?: boolean
}
export interface SketchCircle {
  id: string; center: string; radius: number; construction?: boolean
}
export interface SketchConstraint { id?: string; type: ConstraintType; [k: string]: unknown }

export interface SketchSpec {
  points: SketchPoint[]
  lines?: SketchLine[]
  arcs?: SketchArc[]
  circles?: SketchCircle[]
  constraints?: SketchConstraint[]
}

/**
 * What a solve established, not merely whether it converged.
 *
 * `converged` is `residual < 1e-6`, which an *unconstrained* sketch satisfies
 * trivially — so it is kept for callers that read it but says nothing on its
 * own. `status` and `dof` are the answer.
 */
export type SketchStatus =
  | 'well_constrained' | 'under_constrained' | 'over_constrained' | 'conflicting'

export interface ConstraintRef {
  kind: 'constraint' | 'arc'
  index?: number
  id?: string | null
  type?: string
  label: string
  residual?: number
  weight?: number
}

export interface SketchSolution {
  points: Record<string, [number, number]>
  radii: Record<string, number>
  residual: number
  converged: boolean
  status: SketchStatus
  dof: number
  redundant: number
  rank: number
  variables: number
  equations: number
  conflicts: ConstraintRef[]
  redundant_groups: ConstraintRef[][]
  message: string
  loops: { type: 'loops'; loops: unknown[] } | null
  polygon: [number, number][] | null
}

/** What `add_sketch` / `create_sketch` / `update_sketch` report back. */
export interface SketchOpResult {
  message: string
  feature_id?: string
  converged: boolean
  residual: number
  status: SketchStatus
  dof: number
  redundant: number
  redundant_groups: ConstraintRef[][]
  solver_message: string
  consumers?: string[]
}

export interface SpecReport {
  goal?: string | null
  checks: DfmCheck[]
  passed: number
  failed: number
  /** Fraction of requirements met — the scalar an RL loop optimises. */
  score: number
  met: boolean
}

export interface ToleranceCallout {
  label: string
  nominal_mm: number
  kind: 'fit' | 'pm'
  spec?: string | null
  plus: number
  minus: number
}

export interface FeatureControlFrame {
  feature: string
  symbol: string
  tolerance_mm: number
  datums: string[]
}

export interface DocumentRevisionMeta {
  id?: string
  letter: string
  label: string
  state: 'released' | 'obsolete'
  released_by?: string | null
  released_at: string
  notes: string
}

/** Which views to draw, where to cut, what to dimension — all choices, no geometry. */
export interface DrawingSpec {
  views?: Array<'front' | 'back' | 'top' | 'bottom' | 'left' | 'right'>
  sections?: Array<{ plane: 'xy' | 'xz' | 'yz'; offset_mm?: number; label?: string }>
  /** Points are model mm on the view's own two axes (FRONT is X,Z; TOP is X,Y; RIGHT is Y,Z). */
  dimensions?: Array<{
    view: string
    type?: 'horizontal' | 'vertical' | 'aligned'
    from: [number, number]
    to: [number, number]
    offset?: number
    /** Omit to print the true model distance between the endpoints. */
    label?: string
  }>
  annotations?: Array<{
    view: string
    at: [number, number]
    text?: string
    /** Pull the text from a GD&T frame / tolerance callout already on the document. */
    gdt_feature?: string
    tolerance_label?: string
    dx?: number
    dy?: number
    frame?: boolean
  }>
  projection?: 'first' | 'third'
  sheet?: 'auto' | 'a4' | 'a3' | 'a2' | 'a1' | 'a0'
  scale?: number | 'auto'
  show_hidden?: boolean
}

export interface DrawingResult {
  svg: string
  /** Contour counts per view. front/top/right are always present (0 when not drawn). */
  views: Record<string, number> & { front: number; top: number; right: number; section: number }
  /** The views actually requested — `views` pads the legacy keys with zeros. */
  drawn_views: string[]
  /** Dashed-line contour counts per view. */
  hidden: Record<string, number>
  sections: Array<{ label: string; plane: string; cut_at_mm: number; contours: number }>
  /** From the evaluated body, so it agrees with `bounding_box`. */
  bbox_mm: { width: number; depth: number; height: number }
  /** What the views traced; a divergence from bbox_mm means the silhouette is wrong. */
  traced_mm: { width: number; depth: number; height: number }
  scale: number
  scale_text: string
  projection: string
  sheet_mm: { width: number; height: number }
  dimensions: number
  annotations: number
  material: string
}

/** Structural or drafting checks, shared shape with DfmReport's checks. */
export interface ValidationReport {
  valid: boolean
  checks: DfmCheck[]
  failed: string[]
  warnings: string[]
}

export interface DocumentConfiguration {
  name: string
  overrides: Record<string, string>
}

export interface DocumentParameter {
  name: string
  expr: string
  value?: number | null
  unit: 'mm' | 'deg' | 'count' | 'ratio'
}

/** Engineering material from the assignable catalog (services/materials.py). */
export interface CatalogMaterial {
  id: string
  name: string
  category: string
  /** 'maker' (shelf-stock for hobbyists) | 'professional' (specified engineering stock) */
  tier: string
  density: number
  youngs_modulus: number
  poisson_ratio: number
  yield_strength: number
  cost_per_kg: number
  process: string
  notes: string
}

/** Display descriptor for a material category, in catalog-defined order. */
export interface MaterialCategory {
  id: string
  label: string
  blurb: string
  count: number
}

export interface MassProperties {
  material: { id: string; name: string; density: number }
  volume_cm3: number
  mass_kg: number
  surface_area_cm2: number
  center_of_mass_mm: number[]
  inertia_kg_mm2: number[][]
  bbox_mm: { min: number[]; max: number[]; size: number[] }
}

export interface DocumentSummary {
  id: string
  name: string
  provenance: string
  updated_at: string
  created_at: string
  /** What the document was asked for, when it was asked for in words — often the
   *  only thing the user remembers about a generated part. */
  source_prompt?: string | null
  /** The caller's access level on this document. */
  role: 'owner' | 'editor' | 'viewer'
  /** True when the document is shared with the caller (they are not the owner). */
  shared: boolean
  /** What the card shows about the part itself, so two documents are not
   *  indistinguishable until one of them is opened. */
  lifecycle_state: 'draft' | 'in_review' | 'released' | 'obsolete'
  material_id?: string | null
  mass_kg?: number | null
  /** Latest released revision letter, if any. */
  revision?: string | null
  /** The geometry's content hash — what the thumbnail is a picture of.
   *  Null when the document has never been evaluated. */
  mesh_hash?: string | null
}

// --- Instant quotes + orders ("Get it made") ---
export interface QuoteBundle {
  document_id: string
  quantity: number
  volume_cm3: number
  bbox_mm: number[]
  provider: string
  quotes: Quote[]
}

export interface ManufacturingOrder {
  id: string
  document_id: string
  process: string
  quantity: number
  quote: Quote
  notes: string
  status: string
  provider: string
  created_at: string
  /** The released-revision package sent with the order — STEP, drawing, BOM and
   *  spec score. Null when the part has no released revision, with the reason
   *  in `package_note`: a prototype is a real thing to order. */
  package?: OrderPackage | null
  package_note?: string
}

export interface OrderPackage {
  revision: string
  url: string
  filename: string
  bytes: number
  spec_score?: number | null
  contents: string[]
}

/** A file exported from a document, addressed by the hash of its own bytes. */
export interface ExportArtifact {
  format: string
  description: string
  url: string
  hash: string
  bytes: number
  filename: string
  media_type: string
}

export interface ReleasePackageJob {
  id: string
  status: 'running' | 'done' | 'error'
  message: string
  result?: {
    revision: string
    url: string
    filename: string
    bytes: number
    hash: string
    manifest: Record<string, unknown>
  } | null
}

// --- Forge Copilot: agentic operation loop ---
export interface CopilotStep {
  kind: 'op' | 'error' | 'done'
  op?: string
  args?: Record<string, unknown>
  rationale?: string | null
  ok?: boolean
  error?: string | null
  result?: Record<string, unknown>
}

export interface CopilotResult {
  ok: boolean
  /** Job id — copilot runs as a background job; poll GET /copilot/{job_id}. */
  job_id: string
  status: 'running' | 'done' | 'error'
  summary: string | null
  steps: CopilotStep[]
  document?: DesignDocument | null
}

// --- Machines: the shop's real equipment ---
export interface MachineSpec {
  id: string
  name: string
  vendor: string
  /** fdm | laser | press_brake | cnc */
  process: string
  /** working envelope in mm (x, y, z); sheet machines use z = 0 */
  bed: number[]
  dialect: string
  /** moonraker (drive live) | dispatch (export job) | quote (send out) */
  connection: string
  notes: string
}

export interface NestResult {
  machine: { id: string; name: string; sheet_mm: number[] }
  requested: number
  per_sheet: number
  placed: number
  sheets_needed: number
  grid: { cols: number; rows: number }
  part_mm: number[]
  utilisation_pct: number
  placements: { index: number; x: number; y: number }[]
  paths: number[][][]
  cut_length_mm: number
}

// --- FDM slicing (3D printing) ---
export interface SliceSettings {
  layer_height?: number
  nozzle_diameter?: number
  wall_count?: number
  /** 0..1 density */
  infill?: number
  /** Solid top/bottom shell count (0 disables). */
  skin_layers?: number
  speed_print?: number
  speed_first_layer?: number
  speed_travel?: number
  temp_nozzle?: number
  temp_bed?: number
}

export interface SliceStats {
  layers: number
  estimated_minutes: number
  filament_mm: number
  filament_g: number
  size_mm: number[]
  gcode_bytes: number
}

export interface SliceLayerPreview {
  layer: number
  z: number
  walls: number[][][]
  /** Dense top/bottom solid surfaces. */
  skin: number[][][]
  infill: number[][][]
}

export interface SliceResponse {
  stats: SliceStats
  preview: SliceLayerPreview[]
}

// --- Assemblies: compose part documents into a placed whole ---
export interface AssemblyPromptJob {
  job_id: string
  status: 'running' | 'done' | 'error'
  progress?: string | null
  assembly_id?: string | null
  error?: string | null
  /** What to do about the error, when the failure carries one — chiefly
   *  "no language model is configured", which is useless without it. */
  remedy?: string | null
}

export interface ComponentTransform {
  translate: Vector3
  rotate_deg: Vector3
  scale: number
}

export interface AssemblyComponent {
  id: string
  part_id: string
  name: string
  transform: ComponentTransform
  /** Resolved from the referenced part at read time. */
  mesh_url?: string | null
  bbox?: BBox | null
  /** True when the referenced part was deleted. */
  missing: boolean
}

/**
 * Two schemes, and the mate says which by whether it names connectors.
 *
 * Without them it reads the parts' bounding-box extremes along one world axis
 * (`fasten` | `coincident` | `concentric` | `distance`) and the parts stay
 * axis-aligned, because an extreme cannot express a rotation. With
 * `base_connector`/`moved_connector` it aligns two frames that live in the
 * parts — the only way a part gets rotated into place here — and the six
 * kinematic types below are that same alignment with different degrees of
 * freedom left free.
 */
export interface AssemblyMate {
  id: string
  type:
    | 'fasten' | 'coincident' | 'concentric' | 'distance'
    | 'revolute' | 'slider' | 'cylindrical' | 'planar' | 'ball' | 'parallel'
  base: string
  moved: string
  axis: 'x' | 'y' | 'z'
  base_face: 'min' | 'max' | 'center'
  moved_face: 'min' | 'max' | 'center'
  offset_mm: number
  /** Feature ids of a MateConnectorFeature in each part. Both or neither. */
  base_connector?: string | null
  moved_connector?: string | null
}

/** One row of the `dof` operation: what a component can still move. */
export interface ComponentDof {
  component_id: string
  name?: string | null
  dof: number
  constrained_by: string[]
  over_constrained: boolean
}

export interface DofReport {
  components: ComponentDof[]
  dof: number
  over_constrained: string[]
  grounded: string[]
}

/** A named coordinate frame on a part, from `list_mate_connectors`. */
export interface MateConnector {
  connector_id: string
  name?: string | null
  origin_mm: [number, number, number]
  x_axis: [number, number, number]
  z_axis: [number, number, number]
}

export interface AssemblyDoc {
  id: string
  name: string
  owner_id?: string | null
  components: AssemblyComponent[]
  mates: AssemblyMate[]
  bbox?: BBox | null
  created_at: string
  updated_at: string
}

export interface InterferenceReport {
  assembly_id: string
  components_checked: number
  interferences: number
  clearance_warnings: number
  clearance_threshold_mm: number
  pairs: { a: string; b: string; status: 'interference' | 'clearance'; gap_mm: number; penetration_mm: number }[]
  clear: boolean
}

export interface BomLine {
  item: number
  part_number: string
  name: string
  part_id: string
  quantity: number
  material: string
  unit_mass_g: number
  ext_mass_g: number
  ext_material_cost_usd: number
  /** The referenced part document was deleted; the line is a placeholder. */
  missing: boolean
}

export interface BomReport {
  assembly_id: string
  assembly_name: string
  line_count: number
  component_count: number
  total_mass_g: number
  total_material_cost_usd: number
  lines: BomLine[]
}

/** A purchased part you can place, from either half of the catalog.
 *
 *  Two axes, and both are on every row so neither has to be inferred:
 *
 *  - `fidelity` — how the body relates to the real part. `as-standard`: every
 *    dimension the published standard gives. `envelope`: the outside only.
 *    `supplier`: the manufacturer's own geometry, imported from a file.
 *  - `geometry` — `exact` means faces and edges are addressable (it can be
 *    filleted, measured exactly, re-exported as a solid); `mesh` means it came
 *    in as triangles and cannot.
 */
export interface StandardPart {
  key: string
  category: PartCategory
  designation: string
  /** The standard's number for a generated part; the maker and part number for
   *  an imported one. */
  standard: string
  material_id: string
  fidelity: 'as-standard' | 'envelope' | 'supplier'
  geometry: 'exact' | 'mesh'
  notes: string
  size_mm: Vector3
  /** Published mass, where the solid envelope is known to over-state it. */
  typical_mass_g?: number
  /** A search — deliberately not a claimed part number. */
  supplier_search: string
  // ── imported parts only ──
  /** Which portal the file came from, as the importer stated it. */
  source?: string
  manufacturer?: string
  part_number?: string
  document_id?: string
  created_at?: string
}

export type PartCategory =
  | 'fastener' | 'bearing' | 'motor' | 'extrusion'
  | 'electronics' | 'pneumatic' | 'hardware' | 'other'

/** A portal an imported part can have come from. `search` is a query URL — a
 *  hyperlink out, never a fetch: no path in this system reaches into a portal
 *  on the user's behalf, because none of them permit it. Empty for the sources
 *  that are not one site ("Manufacturer", "Other"). */
export interface PartSource {
  id: string
  label: string
  search: string
  note: string
}

/** What the purchased-parts listing returns. */
export interface StandardPartPage {
  parts: StandardPart[]
  count: number
  returned: number
  categories: Record<string, number>
  /** How many of `parts` this account imported itself. */
  imported: number
  sources: PartSource[]
  source: string
}

/** A starter part: an ordinary feature tree whose driving dimensions are
 *  already named parameters, so the first edit is changing a number. Built by
 *  the `apply_template` operation, which runs a sequence of ordinary
 *  operations — never an imported body. */
export interface PartTemplate {
  key: string
  name: string
  summary: string
  tags: string[]
  steps: number
  parameters: { name: string; value: number; unit: string }[]
}

export interface AssemblySummary {
  id: string
  name: string
  component_count: number
  updated_at: string
  created_at: string
  role: 'owner' | 'editor' | 'viewer'
  shared: boolean
}

// Serialized FeatureTree ({ version, root }) — the parametric recipe.
export interface FeatureNode {
  type: string
  id?: string | null
  name: string
  params: Record<string, unknown>
  suppressed?: boolean
  /** Must-keep: generative optimization pins this feature's material solid. */
  keep?: boolean
}

export interface FeatureTreeDict {
  version: string
  root: FeatureNode
}

/**
 * The workflow pipeline, in order: design the part, optimize it, analyse it
 * structurally, prepare the job (G-code + nest + post), validate that job by
 * simulating the build, then send it to a real machine.
 */
export type EditorMode = 'model' | 'optimize' | 'analysis' | 'make' | 'validate' | 'machine'

export interface MachinePrinter {
  name: string
  url: string
}

export interface MachineStatus {
  connected: boolean
  error?: string
  state?: string
  filename?: string
  progress?: number
  print_duration?: number
  extruder_temp?: number
  extruder_target?: number
  bed_temp?: number
  bed_target?: number
  position?: number[]
}

// ---------------------------------------------------------------------------
// Operation catalog (GET /api/operations)
//
// The backend publishes every operation it can run, with typed arguments. The
// UI reads the same catalog an agent does, so a newly shipped operation is
// reachable from the console without anyone updating a list here.
// ---------------------------------------------------------------------------

export interface OperationParam {
  type?: string
  enum?: string[]
  default?: unknown
  /** Display unit, e.g. "mm" — the value itself is whatever `type` says. */
  unit?: string
  required?: boolean
  description?: string
  properties?: Record<string, unknown>
}

export interface OperationSpec {
  name: string
  /** `mutate` changes the body; `query` only answers. */
  kind: 'mutate' | 'query'
  description: string
  params: Record<string, OperationParam>
  /** Which endpoint runs it: a document or an assembly. */
  subject?: 'document' | 'assembly'
}
