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
}

export interface CadRepresentation {
  format: string
  feature_tree: object
  step_url?: string
}

export interface Candidate {
  id: string
  name: string
  score: number
  mass: number
  stiffness: number
  max_stress: number
  mesh_url: string
  cad?: CadRepresentation
}

export interface OptimizeResult {
  job_id: string
  candidates: Candidate[]
}

export interface SimulationResult {
  max_stress: number
  max_displacement: number
  safety_factor: number
  compliance: number
  mass: number
  stress_field_url?: string
}

export interface ManufacturingResult {
  build_orientation: string
  build_orientation_vector?: Vector3
  overhang_angle: number
  overhang_area: number
  support_volume: number
  printability_score: number
  print_time_minutes: number
  feature_size_ok: boolean
  enclosed_cavities: boolean
}

export interface NodePart {
  id: string
  name: string
  material: string
  quantity: number
  print_time_minutes: number
  position?: Vector3
  candidate_id?: string
  mesh_url?: string
}

export interface Strut {
  id: string
  start: Vector3
  end: Vector3
  diameter: number
}

export interface AssemblyResult {
  id: string
  kind: string
  nodes: NodePart[]
  struts: Strut[]
}

export type WorkflowStep =
  | 'requirements'
  | 'generative'
  | 'topology'
  | 'simulation'
  | 'manufacturing'
  | 'assembly'
  | 'export'

export interface DesignGenerationResponse {
  kind: 'part' | 'assembly'
  part?: Candidate
  assembly?: AssemblyResult
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
  mesh_url?: string
  step_url?: string
  bbox?: BBox
  mass: number
  volume: number
  sim?: SimulationResult
  dfm?: DfmReport
  cam?: CamResult
  error?: string
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

export interface DrawingResult {
  svg: string
  views: { front: number; top: number; right: number; section: number }
  bbox_mm: { width: number; depth: number; height: number }
  material: string
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
  density: number
  youngs_modulus: number
  poisson_ratio: number
  yield_strength: number
  cost_per_kg: number
  process: string
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
  /** The caller's access level on this document. */
  role: 'owner' | 'editor' | 'viewer'
  /** True when the document is shared with the caller (they are not the owner). */
  shared: boolean
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

export interface AssemblyMate {
  id: string
  type: 'fasten' | 'coincident' | 'concentric' | 'distance'
  base: string
  moved: string
  axis: 'x' | 'y' | 'z'
  base_face: 'min' | 'max' | 'center'
  moved_face: 'min' | 'max' | 'center'
  offset_mm: number
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

export interface AssemblySummary {
  id: string
  name: string
  component_count: number
  updated_at: string
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
}

export interface FeatureTreeDict {
  version: string
  root: FeatureNode
}

export type EditorMode = 'model' | 'optimize' | 'validate' | 'make' | 'machine'

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
