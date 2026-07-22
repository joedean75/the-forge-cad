import { Suspense, useEffect, useMemo, useRef, useState } from 'react'
import { Canvas } from '@react-three/fiber'
import { CameraControls, Grid, GizmoHelper, GizmoViewport, Html, Line } from '@react-three/drei'
import * as THREE from 'three'
import { Boxes, Ruler, Crosshair, X, ScanEye, PencilRuler, Maximize } from 'lucide-react'
import { useDocumentStore } from '../store/useDocumentStore'
import { MeshLoader } from './MeshLoader'
import { collectSnapPoints, collectProxies, findNode, type FeatureProxy } from '../util/featureTree'
import type { BBox, Dimension, Vector3, FeatureNode } from '../../../shared/types'

type Vec3 = [number, number, number]

/** Part-space (metres) <-> display-space transform.

 * Frozen per document: recomputed only on doc switch, first geometry arrival,
 * or an explicit refit (`nonce`). If it tracked every bbox change, each edit
 * that grows/shrinks the part would re-normalize the model and visibly jump
 * the view at working zoom — real CAD keeps the model still while you edit. */
function useFit(docId: string | undefined, bbox: BBox | undefined, nonce: number) {
  // Snapshot the bbox only when a freeze key changes (doc switch, first
  // geometry, manual refit) — later bbox edits keep the frozen snapshot.
  const frozen = useRef<{ key: string; bbox: BBox } | null>(null)
  const key = `${docId ?? ''}|${!!bbox}|${nonce}`
  if (bbox && frozen.current?.key !== key) frozen.current = { key, bbox }
  const fb = docId && frozen.current ? frozen.current.bbox : undefined

  return useMemo(() => {
    if (!fb) return null
    const sx = fb.max.x - fb.min.x
    const sy = fb.max.y - fb.min.y
    const sz = fb.max.z - fb.min.z
    const s = 1.5 / (Math.max(sx, sy, sz) || 1)
    const cx = (fb.min.x + fb.max.x) / 2
    const cy = (fb.min.y + fb.max.y) / 2
    const cz = (fb.min.z + fb.max.z) / 2
    return {
      s,
      c: [cx, cy, cz] as Vec3,
      toDisplay: (p: Vector3): Vec3 => [(p.x - cx) * s, (p.y - cy) * s, (p.z - cz) * s],
      toPart: (d: Vec3): Vector3 => ({ x: d[0] / s + cx, y: d[1] / s + cy, z: d[2] / s + cz }),
    }
  }, [fb])
}

function dist(a: Vector3, b: Vector3): number {
  return Math.hypot(a.x - b.x, a.y - b.y, a.z - b.z)
}

function DimensionLines({
  dims,
  fit,
  onRemove,
}: {
  dims: Dimension[]
  fit: NonNullable<ReturnType<typeof useFit>>
  onRemove: (id: string) => void
}) {
  return (
    <>
      {dims.map((d) => {
        const a = fit.toDisplay(d.a)
        const b = fit.toDisplay(d.b)
        const mid: Vec3 = [(a[0] + b[0]) / 2, (a[1] + b[1]) / 2, (a[2] + b[2]) / 2]
        const isUser = d.source === 'user'
        const color = isUser ? '#e8683c' : '#7d8ba0'
        const text = d.label ?? `${(dist(d.a, d.b) * 1000).toFixed(1)} mm`
        return (
          <group key={d.id}>
            <Line points={[a, b]} color={color} lineWidth={1.5} />
            <mesh position={a}><sphereGeometry args={[0.012, 12, 12]} /><meshBasicMaterial color={color} /></mesh>
            <mesh position={b}><sphereGeometry args={[0.012, 12, 12]} /><meshBasicMaterial color={color} /></mesh>
            <Html position={mid} center zIndexRange={[20, 0]}>
              <button
                onClick={() => onRemove(d.id)}
                title="Remove dimension"
                className={`tabular group flex items-center gap-1 text-[9px] px-1.5 py-0.5 rounded-sm border whitespace-nowrap transition-colors ${
                  isUser
                    ? 'text-cad-accent-hot border-cad-accent/50 bg-cad-panel/90'
                    : 'text-cad-text-heading border-cad-border bg-cad-panel/90'
                }`}
              >
                {text}
                <X size={9} className="opacity-0 group-hover:opacity-100 text-cad-danger" />
              </button>
            </Html>
          </group>
        )
      })}
    </>
  )
}

/** Dimension annotations mirroring MeshLoader's auto-fit (centered, scaled to 1.5 units). */
function DimLabel({ position, text }: { position: [number, number, number]; text: string }) {
  return (
    <Html position={position} center zIndexRange={[10, 0]}>
      <div className="tabular text-[9px] text-cad-accent-hot bg-cad-panel/85 border border-cad-border px-1.5 py-0.5 rounded-sm whitespace-nowrap pointer-events-none">
        {text}
      </div>
    </Html>
  )
}

function DimensionOverlay({ bbox, fit }: { bbox: BBox; fit: NonNullable<ReturnType<typeof useFit>> }) {
  const s = fit.s
  const sx = bbox.max.x - bbox.min.x
  const sy = bbox.max.y - bbox.min.y
  const sz = bbox.max.z - bbox.min.z
  const hx = (sx * s) / 2
  const hy = (sy * s) / 2
  const hz = (sz * s) / 2
  // Placed through the frozen fit so the envelope tracks the *current* bbox
  // even when the model transform is frozen mid-edit.
  const center = fit.toDisplay({
    x: (bbox.min.x + bbox.max.x) / 2,
    y: (bbox.min.y + bbox.max.y) / 2,
    z: (bbox.min.z + bbox.max.z) / 2,
  })

  const edges = useMemo(
    () => new THREE.EdgesGeometry(new THREE.BoxGeometry(sx * s, sy * s, sz * s)),
    [sx, sy, sz, s],
  )
  const mm = (m: number) => `${(m * 1000).toFixed(1)} mm`

  return (
    <group position={center}>
      <lineSegments geometry={edges}>
        <lineBasicMaterial color="#6b7f99" transparent opacity={0.6} />
      </lineSegments>
      <DimLabel position={[0, -hy - 0.12, hz]} text={mm(sx)} />
      <DimLabel position={[hx + 0.14, 0, hz]} text={mm(sy)} />
      <DimLabel position={[hx + 0.14, -hy, 0]} text={mm(sz)} />
    </group>
  )
}

const HIGHLIGHT = '#e8683c'

function ProxyMesh({ px, fit }: { px: FeatureProxy; fit: NonNullable<ReturnType<typeof useFit>> }) {
  const pos = fit.toDisplay(px.center)
  const s = fit.s
  const boxGeo = useMemo(
    () => (px.kind === 'box' && px.size ? new THREE.EdgesGeometry(new THREE.BoxGeometry(px.size.x * s, px.size.y * s, px.size.z * s)) : null),
    [px, s],
  )
  const cylGeo = useMemo(
    () => (px.kind === 'cyl' ? new THREE.EdgesGeometry(new THREE.CylinderGeometry((px.radius ?? 0.01) * s, (px.radius ?? 0.01) * s, (px.height ?? 0.01) * s, 24)) : null),
    [px, s],
  )
  const quat = useMemo(() => {
    if (px.kind !== 'cyl' || !px.axis) return [0, 0, 0, 1] as [number, number, number, number]
    const q = new THREE.Quaternion().setFromUnitVectors(
      new THREE.Vector3(0, 1, 0),
      new THREE.Vector3(px.axis.x, px.axis.y, px.axis.z).normalize(),
    )
    return [q.x, q.y, q.z, q.w] as [number, number, number, number]
  }, [px])

  if (px.kind === 'sphere') {
    return (
      <mesh position={pos}>
        <sphereGeometry args={[(px.radius ?? 0.01) * s, 16, 12]} />
        <meshBasicMaterial color={HIGHLIGHT} wireframe transparent opacity={0.5} />
      </mesh>
    )
  }
  if (px.kind === 'box' && boxGeo) {
    return (
      <lineSegments geometry={boxGeo} position={pos}>
        <lineBasicMaterial color={HIGHLIGHT} />
      </lineSegments>
    )
  }
  if (cylGeo) {
    return (
      <lineSegments geometry={cylGeo} position={pos} quaternion={quat}>
        <lineBasicMaterial color={HIGHLIGHT} />
      </lineSegments>
    )
  }
  return null
}

function SelectionHighlight({ node, fit }: { node: FeatureNode; fit: NonNullable<ReturnType<typeof useFit>> }) {
  const proxies = useMemo(() => collectProxies(node), [node])
  return (
    <>
      {proxies.map((px, i) => (
        <ProxyMesh key={i} px={px} fit={fit} />
      ))}
    </>
  )
}

const EXAMPLE_PROMPTS = [
  '100×60×20 mm mounting bracket with 4 corner holes',
  'a flanged pipe, 40 mm bore, 8 mm wall',
  'hex standoff, M3 thread, 25 mm tall',
  'L-bracket 80×80 mm, 5 mm thick, 3 holes per leg',
]

export function DocumentViewport() {
  const { doc, busy, selectedFeatureId, addDimension, removeDimension, createFromPrompt, newBlank,
    sketchPick, pickSketchOnFace, pickSketchPlane, cancelSketchPick } = useDocumentStore()
  const [isolate, setIsolate] = useState(false)
  const [showDims, setShowDims] = useState(false)
  const [measuring, setMeasuring] = useState(false)
  const [pending, setPending] = useState<Vector3 | null>(null)
  const meshUrl = doc?.derived.mesh_url
  const evalError = doc?.derived.error
  const bbox = doc?.derived.bbox
  const [fitNonce, setFitNonce] = useState(0)
  const fit = useFit(doc?.id, bbox, fitNonce)
  const dims = doc?.dimensions ?? []

  // F re-fits the part to the standard display size (after big edits).
  useEffect(() => {
    const h = (e: KeyboardEvent) => {
      if (e.key.toLowerCase() !== 'f') return
      const t = e.target as HTMLElement | null
      if (t && (t.tagName === 'INPUT' || t.tagName === 'TEXTAREA' || t.tagName === 'SELECT' || t.isContentEditable)) return
      setFitNonce((n) => n + 1)
    }
    window.addEventListener('keydown', h)
    return () => window.removeEventListener('keydown', h)
  }, [])

  // Highlight only non-root selections (root would outline every primitive).
  const rootId = doc?.feature_tree.root?.id
  const selectedNode = useMemo<FeatureNode | null>(() => {
    if (!doc || !selectedFeatureId || selectedFeatureId === rootId) return null
    return findNode(doc.feature_tree, selectedFeatureId)
  }, [doc, selectedFeatureId, rootId])

  // Snap targets (part-space) with cached display positions.
  const snaps = useMemo(() => {
    if (!fit || !doc) return []
    const pts = collectSnapPoints(doc.feature_tree)
    if (bbox) {
      for (const x of [bbox.min.x, bbox.max.x])
        for (const y of [bbox.min.y, bbox.max.y])
          for (const z of [bbox.min.z, bbox.max.z]) pts.push({ x, y, z })
    }
    return pts.map((part) => ({ part, disp: fit.toDisplay(part) }))
  }, [doc, bbox, fit])

  const SNAP_R = 0.11 // display units

  const handlePick = (hit: Vec3) => {
    if (!measuring || !fit) return
    // Snap the raycast hit to the nearest feature point within threshold.
    let best: Vector3 | null = null
    let bestD = SNAP_R
    for (const s of snaps) {
      const d = Math.hypot(hit[0] - s.disp[0], hit[1] - s.disp[1], hit[2] - s.disp[2])
      if (d < bestD) {
        bestD = d
        best = s.part
      }
    }
    const part = best ?? fit.toPart(hit)
    if (!pending) {
      setPending(part)
    } else {
      addDimension(pending, part)
      setPending(null)
      setShowDims(false)
    }
  }

  const toggleMeasure = () => {
    setMeasuring((v) => !v)
    setPending(null)
  }

  return (
    <div
      className="relative flex-1 min-w-0"
      style={{
        background:
          'radial-gradient(120% 120% at 50% 20%, #1a1f27 0%, var(--color-cad-bg) 55%, #0e1116 100%)',
      }}
    >
      {/* near=0.01 so close zoom never clips the part; far generous for wide views */}
      {/* logarithmicDepthBuffer: the model is normalized to ~1.5 units but is
          viewed from 0.08–50 units with thin/coplanar CAD faces; a linear depth
          buffer z-fights and "glitches" the model at certain distances. Log depth
          + a tighter near/far ratio give stable precision across the whole range. */}
      <Canvas camera={{ position: [3, 2, 4], fov: 45, near: 0.02, far: 100 }} shadows
        gl={{ logarithmicDepthBuffer: true }}>
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

          {meshUrl && (
            <MeshLoader
              key={meshUrl}
              url={meshUrl}
              fit={fit ? { c: fit.c, s: fit.s } : undefined}
              onPick={measuring && !sketchPick ? handlePick : undefined}
              onPickFace={sketchPick && rootId ? (face) => pickSketchOnFace(rootId, face) : undefined}
              dim={isolate && !!selectedNode}
            />
          )}
          {fit && selectedNode && <SelectionHighlight node={selectedNode} fit={fit} />}
          {showDims && bbox && fit && <DimensionOverlay bbox={bbox} fit={fit} />}
      {fit && dims.length > 0 && <DimensionLines dims={dims} fit={fit} onRemove={removeDimension} />}

          {/* snap targets while measuring */}
          {measuring &&
            snaps.map((s, i) => (
              <mesh key={i} position={s.disp}>
                <sphereGeometry args={[0.007, 8, 8]} />
                <meshBasicMaterial color="#7d8ba0" transparent opacity={0.7} />
              </mesh>
            ))}

          {pending && fit && (
            <mesh position={fit.toDisplay(pending)}>
              <sphereGeometry args={[0.02, 16, 16]} />
              <meshBasicMaterial color="#ffb86b" />
            </mesh>
          )}

          {/* camera-controls: proper dolly-to-cursor with smoothing — unlike
              OrbitControls' zoomToCursor it doesn't drift or jump when the
              dolly clamps at close working distance */}
          <CameraControls
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

      {/* instrument datum */}
      <div className="absolute top-3 left-3 flex items-center gap-2 pointer-events-none">
        <span aria-hidden className="w-0.5 h-3 bg-cad-accent rounded-full" />
        <span className="eyebrow text-cad-text/80">Perspective · body</span>
      </div>

      {/* viewport tools */}
      {bbox && (
        <div className="absolute top-9 left-3 flex items-center gap-1.5">
          <button
            onClick={() => setFitNonce((n) => n + 1)}
            title="Fit the part to the view (F)"
            className="flex items-center gap-1.5 text-[10px] px-2 py-1 rounded-sm border border-cad-border text-cad-text bg-cad-panel/70 hover:text-cad-text-heading backdrop-blur-sm transition-colors"
          >
            <Maximize size={11} /> Fit
          </button>
          <button
            onClick={() => setShowDims((v) => !v)}
            title="Toggle bounding-box dimensions"
            className={`flex items-center gap-1.5 text-[10px] px-2 py-1 rounded-sm border backdrop-blur-sm transition-colors ${
              showDims
                ? 'border-cad-accent/50 text-cad-accent-hot bg-cad-accent/10'
                : 'border-cad-border text-cad-text bg-cad-panel/70 hover:text-cad-text-heading'
            }`}
          >
            <Ruler size={11} /> Envelope
          </button>
          <button
            onClick={toggleMeasure}
            title="Measure: click two points on the part"
            className={`flex items-center gap-1.5 text-[10px] px-2 py-1 rounded-sm border backdrop-blur-sm transition-colors ${
              measuring
                ? 'border-cad-accent/50 text-cad-accent-hot bg-cad-accent/10'
                : 'border-cad-border text-cad-text bg-cad-panel/70 hover:text-cad-text-heading'
            }`}
          >
            <Crosshair size={11} /> Measure
          </button>
          <button
            onClick={() => setIsolate((v) => !v)}
            disabled={!selectedNode}
            title="Isolate the selected feature (dim the rest)"
            className={`flex items-center gap-1.5 text-[10px] px-2 py-1 rounded-sm border backdrop-blur-sm transition-colors disabled:opacity-40 ${
              isolate && selectedNode
                ? 'border-cad-accent/50 text-cad-accent-hot bg-cad-accent/10'
                : 'border-cad-border text-cad-text bg-cad-panel/70 hover:text-cad-text-heading'
            }`}
          >
            <ScanEye size={11} /> Isolate
          </button>
        </div>
      )}

      {measuring && !sketchPick && (
        <div className="tabular absolute top-[68px] left-3 text-[10px] text-cad-accent-hot bg-cad-panel/85 border border-cad-accent/40 rounded-sm px-2 py-1">
          {pending ? 'Click the second point — snaps to ●' : 'Click the first point — snaps to feature ●'}
        </div>
      )}

      {/* sketch-plane picker: click a face on the body, or choose a base plane */}
      {sketchPick && (
        <div className="absolute top-3 left-1/2 -translate-x-1/2 flex items-center gap-3 bg-cad-panel/95 backdrop-blur-sm border border-cad-accent/50 rounded-md px-3 py-2 shadow-lg">
          <div className="flex items-center gap-2">
            <PencilRuler size={14} className="text-cad-accent" />
            <div className="leading-tight">
              <p className="text-[11px] font-medium text-cad-text-heading">
                {meshUrl ? 'Click a face to sketch on' : 'Choose a base plane to sketch on'}
              </p>
              <p className="text-[9px] text-cad-text/70">
                {meshUrl ? 'or pick the part in the tree, or a base plane →' : 'the body is empty — start on a datum plane'}
              </p>
            </div>
          </div>
          <div className="flex items-center gap-1 pl-2 border-l border-cad-border">
            {(['xy', 'xz', 'yz'] as const).map((p) => (
              <button
                key={p}
                onClick={() => pickSketchPlane(p)}
                title={`Sketch on the ${p.toUpperCase()} datum plane`}
                className="tabular px-2 py-1 rounded-sm border border-cad-border text-[10px] text-cad-text hover:text-cad-accent-hot hover:border-cad-accent/50 transition-colors"
              >
                {p.toUpperCase()}
              </button>
            ))}
          </div>
          <button
            onClick={cancelSketchPick}
            title="Cancel"
            className="p-1 rounded-sm text-cad-text hover:text-cad-danger"
          >
            <X size={13} />
          </button>
        </div>
      )}

      {/* empty state — with one-click example prompts to make text-to-CAD discoverable */}
      {!doc && (
        <div className="absolute inset-0 flex flex-col items-center justify-center gap-4 px-6">
          <Boxes size={40} className="text-cad-border" />
          <div className="text-center">
            <p className="text-sm text-cad-text-heading">Describe a part to begin</p>
            <p className="text-[11px] text-cad-text/60 mt-1">Type in the bar above, or try one of these:</p>
          </div>
          <div className="flex flex-wrap items-center justify-center gap-2 max-w-lg">
            {EXAMPLE_PROMPTS.map((p) => (
              <button
                key={p}
                onClick={() => !busy && createFromPrompt(p)}
                disabled={busy}
                className="text-[11px] px-2.5 py-1.5 rounded-full border border-cad-border text-cad-text hover:text-cad-text-heading hover:border-cad-accent/50 hover:bg-cad-accent/5 transition-colors disabled:opacity-40"
              >
                {p}
              </button>
            ))}
          </div>
          <button
            onClick={() => !busy && newBlank()}
            disabled={busy}
            className="text-[11px] text-cad-text/70 hover:text-cad-accent-hot underline underline-offset-2 disabled:opacity-40"
          >
            or start from a blank body
          </button>
        </div>
      )}

      {/* evaluation error */}
      {doc && evalError && (
        <div className="absolute bottom-3 right-3 max-w-xs text-[11px] text-cad-danger bg-cad-panel/90 border border-cad-danger/40 rounded-sm px-3 py-2">
          Evaluation failed: {evalError}
        </div>
      )}

      {busy && (
        <div className="tabular absolute top-3 right-3 flex items-center gap-2 text-[10px] text-cad-accent-hot bg-cad-panel/70 backdrop-blur-sm px-2.5 py-1 rounded-sm border border-cad-border">
          <span className="w-1.5 h-1.5 rounded-full bg-cad-accent-hot animate-filament" />
          evaluating
        </div>
      )}

      <div className="tabular absolute bottom-3 left-3 text-[10px] text-cad-text bg-cad-panel/70 backdrop-blur-sm px-2.5 py-1 rounded-sm border border-cad-border">
        L-drag rotate · R-drag pan · scroll zoom
      </div>
    </div>
  )
}
