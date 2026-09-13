import { Suspense, useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { Canvas, useLoader } from '@react-three/fiber'
import { CameraControls, Grid, GizmoHelper, GizmoViewport } from '@react-three/drei'
import { ForgeMeshLoader } from '../util/forgeMesh'
import { DoubleSide } from 'three'
import { Boxes, Link2, Maximize } from 'lucide-react'
import { CadLighting, CadMaterial } from './CadAppearance'
import { MoveHandles } from './MoveHandles'
import type { AssemblyComponent, BBox, ComponentTransform, Vector3 } from '../../../shared/types'

type Vec3 = [number, number, number]
const DEG = Math.PI / 180

/** Shared part-space (metres) → display transform over the whole assembly envelope. */
function useFit(assemblyId: string, bbox: BBox | null | undefined, nonce: number) {
  const frozen = useRef<{ key: string; bbox: BBox } | null>(null)
  const key = `${assemblyId}|${!!bbox}|${nonce}`
  if (!bbox) frozen.current = null
  if (bbox && frozen.current?.key !== key) frozen.current = { key, bbox }
  const envelope = bbox ? frozen.current?.bbox : undefined
  return useMemo(() => {
    const bbox = envelope
    if (!bbox) return null
    const sx = bbox.max.x - bbox.min.x
    const sy = bbox.max.y - bbox.min.y
    const sz = bbox.max.z - bbox.min.z
    const s = 1.8 / (Math.max(sx, sy, sz) || 1)
    const cx = (bbox.min.x + bbox.max.x) / 2
    const cy = (bbox.min.y + bbox.max.y) / 2
    const cz = (bbox.min.z + bbox.max.z) / 2
    return { s, toDisplay: (p: Vector3): Vec3 => [(p.x - cx) * s, (p.y - cy) * s, (p.z - cz) * s] }
  }, [envelope])
}

function ComponentMesh({
  url,
  position,
  scale,
  rotation,
  selected,
  dim,
  onSelect,
}: {
  url: string
  position: Vec3
  scale: number
  rotation: Vec3
  selected: boolean
  dim: boolean
  onSelect: () => void
}) {
  const loaded = useLoader(ForgeMeshLoader, url)
  // Each component owns a clone and preserves the loader's face-aware normals.
  // Never recenter: components must share one coordinate frame.
  const geometry = useMemo(() => loaded.clone(), [loaded])
  useEffect(() => () => geometry.dispose(), [geometry])

  return (
    <mesh
      position={position}
      scale={scale}
      // 'ZYX' because the server composes a placement as Rz . Ry . Rx
      // (`sdf._euler_matrix`), and three.js's default 'XYZ' is Rx . Ry . Rz. The
      // two agree about a turn on one axis and disagree about any compound one,
      // so an imported part turned on two axes drew somewhere the interference
      // check and the mass roll-up did not think it was.
      rotation={[rotation[0], rotation[1], rotation[2], 'ZYX']}
      onClick={(e) => {
        e.stopPropagation()
        onSelect()
      }}
    >
      <primitive object={geometry} attach="geometry" />
      <CadMaterial selected={selected} dim={dim} />
    </mesh>
  )
}

export function AssemblyViewport({
  assemblyId,
  components,
  bbox,
  selectedId,
  isolate,
  driven,
  onSelect,
  onMove,
}: {
  assemblyId: string
  components: AssemblyComponent[]
  bbox: BBox | null | undefined
  selectedId: string | null
  isolate: boolean
  /** Components a mate positions. Their placement is an output, not an input. */
  driven: Set<string>
  onSelect: (id: string | null) => void
  onMove: (componentId: string, transform: ComponentTransform) => void
}) {
  const [fitNonce, setFitNonce] = useState(0)
  const fit = useFit(assemblyId, bbox, fitNonce)
  const [orbit, setOrbit] = useState(true)
  // Where the dragged component is *right now*, in assembly metres. Held here
  // rather than pushed at the store because nothing outside this viewport can
  // act on a placement that is still under the pointer — and the store write is
  // a server round trip, which is what the one-commit-on-release rule is about.
  const [dragOffset, setDragOffset] = useState<Vector3 | null>(null)
  const placed = components.filter((c) => c.mesh_url && !c.missing)
  const selected = placed.find((c) => c.id === selectedId) ?? null
  // A mate writes this component's translation, so a drag is a number the next
  // solve overwrites — the user drags, watches it snap back, and concludes the
  // tool is broken. The same reason the placement fields are disabled, and the
  // handles are simply not drawn rather than being drawn and ignored.
  const drivenSelection = !!selected && driven.has(selected.id)
  const draggable = selected && !drivenSelection ? selected : null
  // CameraControls doesn't auto-frame — explicitly frame the origin-normalized
  // assembly once components are placed, so it isn't an empty viewport on open.
  const controlsRef = useRef<{ updateCameraUp: () => void; setLookAt: (px: number, py: number, pz: number, tx: number, ty: number, tz: number, t?: boolean) => void } | null>(null)
  const framedFor = useRef<string | null>(null)
  const frameModel = useCallback(() => {
    const controls = controlsRef.current
    if (!controls) return
    controls.updateCameraUp()
    const key = `${assemblyId}|${fitNonce}`
    if (!fit) {
      framedFor.current = null
      return
    }
    if (framedFor.current === key) return
    controls.setLookAt(2.6, -3.4, 1.9, 0, 0, 0, false)
    framedFor.current = key
  }, [assemblyId, fitNonce, fit])
  useEffect(frameModel, [frameModel])

  return (
    <div
      className="relative flex-1 min-w-0"
      style={{ background: 'radial-gradient(120% 120% at 50% 20%, #1a1f27 0%, var(--color-cad-bg) 55%, #0e1116 100%)' }}
    >
      <Canvas camera={{ position: [2.6, -3.4, 1.9], up: [0, 0, 1], fov: 45, near: 0.02, far: 100 }} gl={{ logarithmicDepthBuffer: true }} onPointerMissed={() => onSelect(null)}>
        <Suspense fallback={null}>
          <CadLighting />
          <Grid
            position={[0, 0, -1]}
            rotation={[Math.PI / 2, 0, 0]}
            side={DoubleSide}
            args={[20, 20]}
            cellSize={0.5}
            cellThickness={0.5}
            cellColor="#20252e"
            sectionSize={2}
            sectionThickness={1}
            sectionColor="#2c333e"
            fadeDistance={15}
            fadeStrength={1}
            infiniteGrid
          />
          {fit &&
            placed.map((c) => {
              // **The preview moves the component itself**, which is the one
              // place this differs from the part editor. There a feature is
              // moving and the union of it with everything else is not that
              // mesh translated, so the outline moves instead; here a component
              // is a rigid body and its mesh translating *is* what happens.
              const live = dragOffset && c.id === draggable?.id
                ? { x: c.transform.translate.x + dragOffset.x,
                    y: c.transform.translate.y + dragOffset.y,
                    z: c.transform.translate.z + dragOffset.z }
                : c.transform.translate
              return (
                <ComponentMesh
                  key={c.id}
                  url={c.mesh_url as string}
                  position={fit.toDisplay(live)}
                  scale={fit.s * (c.transform.scale || 1)}
                  rotation={[c.transform.rotate_deg.x * DEG, c.transform.rotate_deg.y * DEG, c.transform.rotate_deg.z * DEG]}
                  selected={c.id === selectedId}
                  dim={isolate && selectedId != null && c.id !== selectedId}
                  onSelect={() => onSelect(c.id)}
                />
              )
            })}
          {fit && draggable && (
            <MoveHandles
              at={draggable.transform.translate}
              fit={fit}
              onPreview={setDragOffset}
              onCommit={(next) => onMove(draggable.id, {
                ...draggable.transform, translate: next,
              })}
              setControlsEnabled={setOrbit}
            />
          )}
          <CameraControls
            ref={(controls) => {
              controlsRef.current = controls
              // Suspense may delay controls until after the framing effect ran.
              if (controls) frameModel()
            }}
            enabled={orbit}
            makeDefault
            minDistance={0.08}
            maxDistance={50}
            dollyToCursor
            smoothTime={0.08}
            draggingSmoothTime={0.05}
          />
          <GizmoHelper alignment="bottom-right" margin={[80, 80]}>
            <GizmoViewport axisColors={['#e8553c', '#5cc9a7', '#7d8ba0']} labelColor="white" />
          </GizmoHelper>
        </Suspense>
      </Canvas>

      <button type="button" onClick={() => setFitNonce((n) => n + 1)}
        disabled={!fit} title="Fit assembly to view" aria-label="Fit assembly to view"
        className="absolute top-3 right-3 flex items-center gap-1.5 rounded-sm border border-cad-border bg-cad-panel/90 px-2.5 py-1.5 text-[11px] text-cad-text-heading disabled:opacity-40">
        <Maximize size={13} /> Fit
      </button>

      {placed.length === 0 && (
        <div className="absolute inset-0 flex flex-col items-center justify-center gap-3 pointer-events-none">
          <Boxes size={40} className="text-cad-border" />
          <div className="text-center">
            <p className="text-sm text-cad-text-heading">No parts placed yet</p>
            <p className="text-[11px] text-cad-text/60 mt-1">Insert a part from the panel to start the assembly.</p>
          </div>
        </div>
      )}

      {/* Said where the handles would have been, rather than left as an
          absence: a selected part with no gizmo reads as a broken gizmo. */}
      {drivenSelection && (
        <div className="absolute top-3 left-1/2 -translate-x-1/2 flex items-center gap-1.5
                        text-[10px] text-cad-accent-hot bg-cad-panel/85 backdrop-blur-sm
                        px-2.5 py-1 rounded-sm border border-cad-accent/30 pointer-events-none">
          <Link2 size={11} /> A mate positions this part — remove the mate to drag it
        </div>
      )}

      <div className="tabular absolute bottom-3 left-3 text-[10px] text-cad-text bg-cad-panel/70 backdrop-blur-sm px-2.5 py-1 rounded-sm border border-cad-border">
        L-drag rotate · R-drag pan · scroll zoom · click a part to select
        {selected && !drivenSelection && ' · drag an arrow to move it'}
      </div>
    </div>
  )
}
