import { Suspense, useMemo } from 'react'
import { Canvas } from '@react-three/fiber'
import {
  OrbitControls,
  Grid,
  GizmoHelper,
  GizmoViewport,
  Html,
  useProgress,
} from '@react-three/drei'
import * as THREE from 'three'
import { useAppStore } from '../store/useAppStore'
import { MeshLoader } from './MeshLoader'
import type {
  Vector3,
  PreserveRegion,
  ObstacleRegion,
  LoadCase,
  Constraint,
  NodePart,
  Strut,
} from '../../../shared/types'

function Loader() {
  const { progress } = useProgress()
  return (
    <Html center>
      <div className="tabular text-cad-accent-hot text-xs whitespace-nowrap">
        {Math.round(progress)}% · evaluating field
      </div>
    </Html>
  )
}

function DesignSpaceBox({ size, center }: { size: Vector3; center: Vector3 }) {
  const geometry = useMemo(() => {
    const g = new THREE.BoxGeometry(size.x, size.y, size.z)
    g.translate(center.x, center.y, center.z)
    return g
  }, [size, center])

  return (
    <lineSegments geometry={new THREE.WireframeGeometry(geometry)}>
      <lineBasicMaterial color="#3a4658" transparent opacity={0.55} />
    </lineSegments>
  )
}

function PreserveRegionMarker({ region }: { region: PreserveRegion }) {
  const { type, center, radius, height } = region
  if (type === 'sphere') {
    return (
      <mesh position={[center.x, center.y, center.z]}>
        <sphereGeometry args={[radius, 24, 24]} />
        <meshBasicMaterial color="#22c55e" transparent opacity={0.35} depthWrite={false} />
      </mesh>
    )
  }
  return (
    <mesh position={[center.x, center.y, center.z]}>
      <cylinderGeometry args={[radius, radius, height ?? radius * 2, 24]} />
      <meshBasicMaterial color="#22c55e" transparent opacity={0.35} depthWrite={false} />
    </mesh>
  )
}

function ObstacleMarker({ region }: { region: ObstacleRegion }) {
  const { type, center, size, radius } = region
  if (type === 'sphere') {
    return (
      <mesh position={[center.x, center.y, center.z]}>
        <sphereGeometry args={[radius ?? 0.2, 24, 24]} />
        <meshBasicMaterial color="#ef4444" transparent opacity={0.25} depthWrite={false} />
      </mesh>
    )
  }
  return (
    <mesh position={[center.x, center.y, center.z]}>
      <boxGeometry args={[size?.x ?? 0.2, size?.y ?? 0.2, size?.z ?? 0.2]} />
      <meshBasicMaterial color="#ef4444" transparent opacity={0.25} depthWrite={false} />
    </mesh>
  )
}

function LoadArrow({ load }: { load: LoadCase }) {
  const origin = new THREE.Vector3(load.point.x, load.point.y, load.point.z)
  const force = new THREE.Vector3(load.force.x, load.force.y, load.force.z)
  const len = Math.max(0.1, force.length() / 8000)
  const dir = force.clone().normalize()
  const end = origin.clone().add(dir.clone().multiplyScalar(len))

  const lineGeo = new THREE.BufferGeometry().setFromPoints([origin, end])
  const line = new THREE.Line(lineGeo, new THREE.LineBasicMaterial({ color: '#f59e0b', linewidth: 2 }))

  const cone = new THREE.Mesh(
    new THREE.ConeGeometry(0.04, 0.12, 12),
    new THREE.MeshBasicMaterial({ color: '#f59e0b' })
  )
  cone.position.copy(end)
  cone.quaternion.setFromUnitVectors(new THREE.Vector3(0, 1, 0), dir)

  return (
    <group>
      <primitive object={line} />
      <primitive object={cone} />
      <Html position={end.toArray()}>
        <div className="text-[10px] text-amber-400 whitespace-nowrap">{load.name}</div>
      </Html>
    </group>
  )
}

function ConstraintMarker({ constraint }: { constraint: Constraint }) {
  return (
    <mesh position={[constraint.point.x, constraint.point.y, constraint.point.z]}>
      <boxGeometry args={[0.1, 0.1, 0.1]} />
      <meshBasicMaterial color="#7d8ba0" />
    </mesh>
  )
}

function AssemblyNode({ node }: { node: NodePart }) {
  if (!node.position) return null
  return (
    <mesh position={[node.position.x, node.position.y, node.position.z]}>
      <sphereGeometry args={[0.05, 16, 16]} />
      <meshStandardMaterial color="#22c55e" metalness={0.5} roughness={0.4} />
    </mesh>
  )
}

function StrutMarker({ strut }: { strut: Strut }) {
  const origin = new THREE.Vector3(strut.start.x, strut.start.y, strut.start.z)
  const end = new THREE.Vector3(strut.end.x, strut.end.y, strut.end.z)
  const direction = new THREE.Vector3().subVectors(end, origin)
  const length = direction.length()
  if (length === 0) return null

  const mid = origin.clone().add(end).multiplyScalar(0.5)
  const cylinder = new THREE.Mesh(
    new THREE.CylinderGeometry(strut.diameter / 2, strut.diameter / 2, length, 12),
    new THREE.MeshStandardMaterial({ color: '#94a3b8', metalness: 0.6, roughness: 0.35 })
  )
  cylinder.position.copy(mid)
  cylinder.quaternion.setFromUnitVectors(new THREE.Vector3(0, 1, 0), direction.clone().normalize())

  return <primitive object={cylinder} />
}

function LoadPathLines() {
  const { spec } = useAppStore()
  const lines = useMemo(() => {
    const pts: THREE.Vector3[] = []
    spec.preserve_regions.forEach((p) => {
      spec.load_cases.forEach((l) => {
        pts.push(new THREE.Vector3(p.center.x, p.center.y, p.center.z))
        pts.push(new THREE.Vector3(l.point.x, l.point.y, l.point.z))
      })
    })
    return new THREE.BufferGeometry().setFromPoints(pts)
  }, [spec])

  return (
    <lineSegments geometry={lines}>
      <lineBasicMaterial color="#e86a3c" transparent opacity={0.4} />
    </lineSegments>
  )
}

export function Viewport() {
  const {
    spec,
    selectedCandidate,
    assembly,
    showDesignSpace,
    showPreserve,
    showObstacles,
    showLoadPaths,
  } = useAppStore()

  return (
    <div
      className="relative flex-1 min-w-0"
      style={{
        background:
          'radial-gradient(120% 120% at 50% 20%, #1a1f27 0%, var(--color-cad-bg) 55%, #0e1116 100%)',
      }}
    >
      <Canvas camera={{ position: [3, 2, 4], fov: 45 }} shadows>
        <Suspense fallback={<Loader />}>
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

          {showDesignSpace && <DesignSpaceBox size={spec.design_space.size} center={spec.design_space.center} />}
          {showPreserve && spec.preserve_regions.map((r) => <PreserveRegionMarker key={r.id} region={r} />)}
          {showObstacles && spec.obstacle_regions.map((r) => <ObstacleMarker key={r.id} region={r} />)}
          {spec.load_cases.map((l) => <LoadArrow key={l.id} load={l} />)}
          {spec.constraints.map((c) => <ConstraintMarker key={c.id} constraint={c} />)}

          {showLoadPaths && <LoadPathLines />}

          {selectedCandidate?.mesh_url && <MeshLoader url={selectedCandidate.mesh_url} />}

          {assembly?.nodes.map((n) => <AssemblyNode key={n.id} node={n} />)}
          {assembly?.struts.map((s) => <StrutMarker key={s.id} strut={s} />)}

          <OrbitControls makeDefault enableDamping dampingFactor={0.05} />
          <GizmoHelper alignment="bottom-right" margin={[80, 80]}>
            <GizmoViewport axisColors={['#ef4444', '#22c55e', '#3b82f6']} labelColor="white" />
          </GizmoHelper>
        </Suspense>
      </Canvas>

      {/* instrument datum — top-left corner label */}
      <div className="absolute top-3 left-3 flex items-center gap-2 pointer-events-none">
        <span aria-hidden className="w-0.5 h-3 bg-cad-accent rounded-full" />
        <span className="eyebrow text-cad-text/80">Perspective · design domain</span>
      </div>

      <div className="tabular absolute bottom-3 left-3 text-[10px] text-cad-text bg-cad-panel/70 backdrop-blur-sm px-2.5 py-1 rounded-sm border border-cad-border">
        L-drag rotate · R-drag pan · scroll zoom
      </div>
    </div>
  )
}
