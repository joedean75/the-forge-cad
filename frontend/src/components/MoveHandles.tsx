import { useEffect, useMemo, useRef, useState } from 'react'
import { Html } from '@react-three/drei'
import { useThree } from '@react-three/fiber'
import * as THREE from 'three'
import type { ThreeEvent } from '@react-three/fiber'
import type { FeatureNode, Vector3 } from '../../../shared/types'

type Vec3 = [number, number, number]
type Fit = { s: number; toDisplay: (p: Vector3) => Vec3 }

/**
 * Drag something along an axis, and commit **one** operation.
 *
 * Used by both viewports, and deliberately knows about neither. What it is
 * handed is a point in the subject's own metres and a callback taking the point
 * it ends at; a *feature's* centre and a *component's* placement are the same
 * question asked of two different things, and writing the gizmo twice would
 * mean two answers to "how far did that go" that agree until one of them is
 * touched. The caller supplies the preview, because that is where the two
 * genuinely differ — see `DocumentViewport` and `AssemblyViewport`.
 *
 * Three decisions carry the rest of the design:
 *
 * - **One operation on release, never one per mouse-move.** A drag is hundreds
 *   of pointer events; committing each would be hundreds of server round trips
 *   and, worse, hundreds of rows in the trajectory recording — a corpus in
 *   which moving a boss takes 300 calls is a corpus that teaches that.
 * - **The drag runs on the window, not on the handle.** A pointer that leaves
 *   a 60-pixel arrow is the normal case, not the exception, and scene-graph
 *   pointer events stop arriving the moment it does. Only the *grab* comes from
 *   the scene; after that the ray is rebuilt from the raw client coordinates.
 * - **Nothing is committed for a drag that did not move.** A click on a handle
 *   that never travels is a click, and an operation whose delta is zero is a
 *   row in the history saying nothing happened.
 */

/** Which param holds this feature's position, and what it currently is. */
export function movableCenter(node: FeatureNode | null): { key: string; at: Vector3 } | null {
  const params = (node?.params ?? {}) as Record<string, unknown>
  for (const key of ['center', 'translate']) {
    const value = params[key] as Vector3 | undefined
    if (value && typeof value.x === 'number' && typeof value.y === 'number'
        && typeof value.z === 'number') {
      return { key, at: value }
    }
  }
  return null
}

const AXES: { axis: 'x' | 'y' | 'z'; dir: Vec3; colour: string }[] = [
  { axis: 'x', dir: [1, 0, 0], colour: '#e0574f' },
  { axis: 'y', dir: [0, 1, 0], colour: '#63b463' },
  { axis: 'z', dir: [0, 0, 1], colour: '#4f8fe0' },
]

/** Where a camera ray comes closest to an infinite line, as a distance along it. */
function alongAxis(ray: THREE.Ray, origin: THREE.Vector3, dir: THREE.Vector3): number | null {
  const w0 = origin.clone().sub(ray.origin)
  const b = dir.dot(ray.direction)
  const denominator = 1 - b * b
  // Looking straight down the axis there is no meaningful answer, and the
  // formula would return an enormous one. Better to ignore the frame.
  if (Math.abs(denominator) < 1e-4) return null
  return (b * ray.direction.dot(w0) - dir.dot(w0)) / denominator
}

type Drag = { axis: 'x' | 'y' | 'z'; dir: Vec3; from: number; delta: number }

export function MoveHandles({
  at,
  fit,
  onPreview,
  onCommit,
  setControlsEnabled,
}: {
  /** Where the thing being moved currently is, in the subject's own metres. */
  at: Vector3
  fit: Fit
  /** Live offset in those metres while dragging, or null when it ends. */
  onPreview: (offset: Vector3 | null) => void
  /** Where it ended up. Called once, on release, and never for a zero drag. */
  onCommit: (next: Vector3) => void
  setControlsEnabled: (on: boolean) => void
}) {
  const { camera, gl } = useThree()
  const [drag, setDrag] = useState<Drag | null>(null)

  const origin = useMemo<Vec3>(
    () => fit.toDisplay(at),
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [at.x, at.y, at.z, fit],
  )

  // Held in refs because the window listeners below are installed once per drag
  // and must see the current values without being torn down and rebuilt on
  // every pointer move.
  const state = useRef({ drag, origin, at, fit, onPreview, onCommit, setControlsEnabled })
  state.current = { drag, origin, at, fit, onPreview, onCommit, setControlsEnabled }

  useEffect(() => {
    if (!drag) return
    const raycaster = new THREE.Raycaster()

    const rayAt = (event: PointerEvent) => {
      const rect = gl.domElement.getBoundingClientRect()
      raycaster.setFromCamera(new THREE.Vector2(
        ((event.clientX - rect.left) / rect.width) * 2 - 1,
        -((event.clientY - rect.top) / rect.height) * 2 + 1,
      ), camera)
      return raycaster.ray
    }

    const onMove = (event: PointerEvent) => {
      const now = state.current
      if (!now.drag) return
      const along = alongAxis(rayAt(event), new THREE.Vector3(...now.origin),
                              new THREE.Vector3(...now.drag.dir))
      if (along === null) return
      const delta = along - now.drag.from
      setDrag({ ...now.drag, delta })
      const shift = delta / now.fit.s
      now.onPreview({
        x: now.drag.axis === 'x' ? shift : 0,
        y: now.drag.axis === 'y' ? shift : 0,
        z: now.drag.axis === 'z' ? shift : 0,
      })
    }

    const onUp = () => {
      const now = state.current
      setDrag(null)
      now.onPreview(null)
      now.setControlsEnabled(true)
      document.body.style.cursor = ''
      if (!now.drag || Math.abs(now.drag.delta) < 1e-9) return
      const shift = now.drag.delta / now.fit.s
      now.onCommit({
        x: now.at.x + (now.drag.axis === 'x' ? shift : 0),
        y: now.at.y + (now.drag.axis === 'y' ? shift : 0),
        z: now.at.z + (now.drag.axis === 'z' ? shift : 0),
      })
    }

    window.addEventListener('pointermove', onMove)
    window.addEventListener('pointerup', onUp)
    window.addEventListener('pointercancel', onUp)
    return () => {
      window.removeEventListener('pointermove', onMove)
      window.removeEventListener('pointerup', onUp)
      window.removeEventListener('pointercancel', onUp)
    }
    // Installed once when a drag starts; `state` carries everything that moves.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [!!drag, camera, gl])

  // Sized in display units so the handles stay the same size on screen whatever
  // the part measures — the same reason the sketch canvas scales its strokes by
  // the view span.
  const length = 0.42
  const shaft = 0.008

  // The camera controls listen on the canvas element itself, so stopping
  // propagation on the react-three-fiber event does not stop them — by the time
  // a pointerdown reaches a handler the view has already begun to orbit, and a
  // missed grab does not merely do nothing, it spins the model off screen.
  // Disabling on *hover* is what makes the button-down land on the handle.
  const hover = (over: boolean) => () => {
    if (!over && state.current.drag) return
    setControlsEnabled(!over)
    document.body.style.cursor = over ? 'grab' : ''
  }

  const begin = (axis: 'x' | 'y' | 'z', dir: Vec3) => (event: ThreeEvent<PointerEvent>) => {
    event.stopPropagation()
    const along = alongAxis(event.ray, new THREE.Vector3(...origin), new THREE.Vector3(...dir))
    if (along === null) return
    setControlsEnabled(false)
    document.body.style.cursor = 'grabbing'
    setDrag({ axis, dir, from: along, delta: 0 })
  }

  return (
    <group position={origin}>
      {AXES.map(({ axis, dir, colour }) => {
        const active = drag?.axis === axis
        const offset = active ? drag.delta : 0
        const mid = dir.map((d) => d * (length / 2 + offset)) as Vec3
        const tip = dir.map((d) => d * (length + offset)) as Vec3
        const quat = new THREE.Quaternion().setFromUnitVectors(
          new THREE.Vector3(0, 1, 0), new THREE.Vector3(...dir))
        return (
          <group key={axis}>
            {/* The pointer target, invisible and far fatter than the arrow it
                covers: a 3 px line is not something anyone can reliably grab. */}
            <mesh
              position={mid}
              quaternion={quat}
              onPointerOver={hover(true)}
              onPointerOut={hover(false)}
              onPointerDown={begin(axis, dir)}
            >
              <cylinderGeometry args={[shaft * 7, shaft * 7, length + 0.09, 10]} />
              <meshBasicMaterial visible={false} depthTest={false} />
            </mesh>
            <mesh position={mid} quaternion={quat} raycast={() => null}>
              <cylinderGeometry args={[shaft, shaft, length, 12]} />
              <meshBasicMaterial color={colour} opacity={active ? 1 : 0.85} transparent
                                 depthTest={false} />
            </mesh>
            <mesh position={tip} quaternion={quat} raycast={() => null}>
              <coneGeometry args={[shaft * 3.4, 0.07, 14]} />
              <meshBasicMaterial color={colour} depthTest={false} />
            </mesh>
          </group>
        )
      })}
      {drag && Math.abs(drag.delta) > 1e-9 && (
        <Html center position={[0, 0.16, 0]} zIndexRange={[30, 0]}>
          <div data-testid="drag-readout"
               className="tabular text-[10px] px-1.5 py-0.5 rounded bg-cad-panel/95
                          border border-cad-border text-cad-text whitespace-nowrap
                          pointer-events-none">
            {drag.axis.toUpperCase()} {(drag.delta / fit.s) * 1000 >= 0 ? '+' : ''}
            {((drag.delta / fit.s) * 1000).toFixed(2)} mm
          </div>
        </Html>
      )}
    </group>
  )
}
