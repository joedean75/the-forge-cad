import { create } from 'zustand'
import type {
  WorkflowStep,
  DesignSpec,
  OptimizeJob,
  Candidate,
  SimulationResult,
  AssemblyResult,
} from '../../../shared/types'

export const DEFAULT_SPEC: DesignSpec = {
  name: 'Forge Chassis Node',
  design_space: { size: { x: 3, y: 2, z: 1.5 }, center: { x: 0, y: 0, z: 0 } },
  preserve_regions: [
    { id: 'p1', name: 'Mount A', type: 'sphere', center: { x: -1, y: 0, z: 0 }, radius: 0.35 },
    { id: 'p2', name: 'Mount B', type: 'sphere', center: { x: 1, y: 0, z: 0 }, radius: 0.35 },
    { id: 'p3', name: 'Mount C', type: 'sphere', center: { x: 0, y: 0.8, z: 0 }, radius: 0.25 },
  ],
  obstacle_regions: [],
  load_cases: [
    { id: 'l1', name: 'Vertical Load', point: { x: 0, y: 0.8, z: 0 }, force: { x: 0, y: -5000, z: 0 }, load_type: 'static' },
  ],
  constraints: [
    { id: 'c1', name: 'Fixed A', type: 'fixed', point: { x: -1, y: 0, z: 0 } },
    { id: 'c2', name: 'Fixed B', type: 'fixed', point: { x: 1, y: 0, z: 0 } },
  ],
  material_id: 'al-7075',
  objective: { target: 'minimize_mass', weight: 1 },
}

interface AppState {
  step: WorkflowStep
  setStep: (step: WorkflowStep) => void
  spec: DesignSpec
  setSpec: (spec: DesignSpec) => void
  activeJob: OptimizeJob | null
  setActiveJob: (job: OptimizeJob | null) => void
  candidates: Candidate[]
  setCandidates: (candidates: Candidate[]) => void
  selectedCandidate: Candidate | null
  setSelectedCandidate: (candidate: Candidate | null) => void
  simulation: SimulationResult | null
  setSimulation: (result: SimulationResult | null) => void
  assembly: AssemblyResult | null
  setAssembly: (result: AssemblyResult | null) => void
  showDesignSpace: boolean
  setShowDesignSpace: (show: boolean) => void
  showPreserve: boolean
  setShowPreserve: (show: boolean) => void
  showObstacles: boolean
  setShowObstacles: (show: boolean) => void
  showLoadPaths: boolean
  setShowLoadPaths: (show: boolean) => void
}

export const useAppStore = create<AppState>((set) => ({
  step: 'requirements',
  setStep: (step) => set({ step }),
  spec: DEFAULT_SPEC,
  setSpec: (spec) => set({ spec }),
  activeJob: null,
  setActiveJob: (activeJob) => set({ activeJob }),
  candidates: [],
  setCandidates: (candidates) => set({ candidates }),
  selectedCandidate: null,
  setSelectedCandidate: (selectedCandidate) => set({ selectedCandidate }),
  simulation: null,
  setSimulation: (simulation) => set({ simulation }),
  assembly: null,
  setAssembly: (assembly) => set({ assembly }),
  showDesignSpace: true,
  setShowDesignSpace: (showDesignSpace) => set({ showDesignSpace }),
  showPreserve: true,
  setShowPreserve: (showPreserve) => set({ showPreserve }),
  showObstacles: true,
  setShowObstacles: (showObstacles) => set({ showObstacles }),
  showLoadPaths: false,
  setShowLoadPaths: (showLoadPaths) => set({ showLoadPaths }),
}))
