import { useEffect } from 'react'
import { useThree } from '@react-three/fiber'
import { Vector3 } from 'three'
import { registerViewport } from '../util/viewportCapture'
import { fitWithin } from '../util/imageAttach'

/** Render and copy synchronously on request, without preserving every frame. */
export function ViewportCapture({ subject, id }: { subject: 'document' | 'assembly'; id?: string }) {
  const { gl, scene, camera } = useThree()
  useEffect(() => {
    if (!id) return
    return registerViewport(subject, id, () => {
      gl.render(scene, camera)
      const canvas = document.createElement('canvas')
      const size = fitWithin(gl.domElement.width, gl.domElement.height)
      if (!size.width || !size.height) throw new Error('The viewport is not visible yet.')
      canvas.width = size.width
      canvas.height = size.height
      const ctx = canvas.getContext('2d')
      if (!ctx) throw new Error('Could not capture the viewport.')
      ctx.drawImage(gl.domElement, 0, 0, size.width, size.height)
      const direction = camera.getWorldDirection(new Vector3())
      return { uri: canvas.toDataURL('image/jpeg', 0.85), viewport: {
        camera_direction: direction.toArray() as [number, number, number],
        camera_up: camera.up.toArray() as [number, number, number],
        projection: camera.type === 'OrthographicCamera' ? 'orthographic' : 'perspective',
      } }
    })
  }, [subject, id, gl, scene, camera])
  return null
}
