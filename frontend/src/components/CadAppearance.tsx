/** Shared modeling appearance. Self-shadowing is deliberately off: it can
 * produce shadow-map stripes on thin CAD faces. Selection stays explicit. */
export function CadLighting() {
  return (
    <>
      <ambientLight intensity={0.6} />
      <directionalLight position={[5, 10, 7]} intensity={1.2} />
      <directionalLight position={[-5, -5, -5]} intensity={0.4} />
    </>
  )
}

export function CadMaterial({ selected = false, dim = false }: {
  selected?: boolean
  dim?: boolean
}) {
  return (
    <meshStandardMaterial
      color={selected ? '#ffb86b' : '#c2ccd8'}
      metalness={0.25}
      roughness={0.55}
      emissive="#e8683c"
      emissiveIntensity={dim ? 0 : selected ? 0.18 : 0.05}
      transparent={dim}
      opacity={dim ? 0.16 : 1}
      flatShading={false}
    />
  )
}
