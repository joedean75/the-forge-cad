import { Suspense, useEffect, useMemo, useRef, useState } from 'react'
import { Canvas, useLoader } from '@react-three/fiber'
import { CameraControls, Grid, GizmoHelper, GizmoViewport } from '@react-three/drei'
import { ForgeMeshLoader } from '../util/forgeMesh'
import { Boxes, Link2 } from 'lucide-react'
import { MoveHandles } from './MoveHandles'
import type { AssemblyComponent, BBox, ComponentTransform, Vector3 } from '../../../shared/types'

type Vec3 = [number, number, number]
const DEG = Math.PI / 180

/** Shared part-space (metres) → display transform over the whole assembly envelope. */
function useFit(bbox: BBox | null | undefined) {
  return useMemo(() => {
    if (!bbox) return null
    const sx = bbox.max.x - bbox.min.x
    const sy = bbox.max.y - bbox.min.y
    const sz = bbox.max.z - bbox.min.z
    const s = 1.8 / (Math.max(sx, sy, sz) || 1)
    const cx = (bbox.min.x + bbox.max.x) / 2
    const cy = (bbox.min.y + bbox.max.y) / 2
    const cz = (bbox.min.z + bbox.max.z) / 2
    return { s, toDisplay: (p: Vector3): Vec3 => [(p.x - cx) * s, (p.y - cy) * s, (p.z - cz) * s] }
  }, [bbox])
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
  // Clone so we can compute normals without mutating the loader's cached geometry
  // (the same part may be instanced by several components). Crucially we do NOT
  // recenter — components must share one coordinate frame.
  const geometry = useMemo(() => {
    const g = loaded.clone()
    g.computeVertexNormals()
    return g
  }, [loaded])

  return (
    <mesh
      position={position}
      scale={scale}
      rotation={rotation}
      castShadow
      receiveShadow
      onClick={(e) => {
        e.stopPropagation()
        onSelect()
      }}
    >
      <primitive object={geometry} attach="geometry" />
      <meshStandardMaterial
        color={selected ? '#ffb86b' : '#c2ccd8'}
        metalness={0.25}
        roughness={0.55}
        emissive="#e8683c"
        emissiveIntensity={selected ? 0.18 : 0.04}
        transparent={dim}
        opacity={dim ? 0.18 : 1}
      />
    </mesh>
  )
}

export function AssemblyViewport({
  components,
  bbox,
  selectedId,
  isolate,
  driven,
  onSelect,
  onMove,
}: {
  components: AssemblyComponent[]
  bbox: BBox | null | undefined
  selectedId: string | null
  isolate: boolean
  /** Components a mate positions. Their placement is an output, not an input. */
  driven: Set<string>
  onSelect: (id: string | null) => void
  onMove: (componentId: string, transform: ComponentTransform) => void
}) {
  const fit = useFit(bbox)
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
  const controlsRef = useRef<{ setLookAt: (px: number, py: number, pz: number, tx: number, ty: number, tz: number, t?: boolean) => void } | null>(null)
  useEffect(() => {
    if (fit && placed.length) controlsRef.current?.setLookAt(2.6, 1.9, 3.4, 0, 0, 0, false)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [fit, placed.length])

  return (
    <div
      className="relative flex-1 min-w-0"
      style={{ background: 'radial-gradient(120% 120% at 50% 20%, #1a1f27 0%, var(--color-cad-bg) 55%, #0e1116 100%)' }}
    >
      <Canvas camera={{ position: [3, 2, 4], fov: 45, near: 0.02, far: 100 }} shadows gl={{ logarithmicDepthBuffer: true }} onPointerMissed={() => onSelect(null)}>
        <Suspense fallback={null}>
          <ambientLight intensity={0.6} />
          <directionalLight position={[5, 10, 7]} intensity={1.2} castShadow />
          <directionalLight position={[-5, -5, -5]} intensity={0.4} />
          <Grid
            position={[0, -1, 0]}
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
            ref={controlsRef as never}
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
