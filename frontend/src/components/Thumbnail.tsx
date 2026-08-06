import { useEffect, useRef, useState } from 'react'
import { authHeaders } from '../api/client'

/**
 * The picture on a library card, fetched only once the card is on screen.
 *
 * Three decisions worth writing down:
 *
 * **It cannot be a plain `<img src>`.** The endpoint is access-checked and the
 * session is a bearer token, which an `<img>` cannot carry. Putting the token in
 * the query string would authenticate it — and put a credential in every
 * referrer, proxy log and browser history entry. So it is an authed `fetch` to a
 * blob URL. The HTTP cache still does its job: the response is immutable, so a
 * second visit is a cache hit with no network.
 *
 * **It is lazy at the card, not at the image.** `loading="lazy"` would still
 * have issued the request, and the render behind it is the expensive half. An
 * IntersectionObserver means a library of fifty parts renders the dozen the user
 * can see.
 *
 * **A missing picture is not an error.** A document that has never been
 * evaluated has no `mesh_hash` and nothing to draw; a hash the server has
 * forgotten 404s by design. Both fall back to the icon, silently — the library
 * must never look broken because a derived artefact was not there.
 */
export function Thumbnail({
  documentId, meshHash, fallback,
}: {
  documentId: string
  meshHash?: string | null
  fallback: React.ReactNode
}) {
  const [url, setUrl] = useState<string | null>(null)
  const [visible, setVisible] = useState(false)
  const holder = useRef<HTMLDivElement>(null)

  useEffect(() => {
    const node = holder.current
    if (!node || visible) return
    if (typeof IntersectionObserver === 'undefined') { setVisible(true); return }
    const observer = new IntersectionObserver((entries) => {
      if (entries.some((e) => e.isIntersecting)) { setVisible(true); observer.disconnect() }
    }, { rootMargin: '200px' })
    observer.observe(node)
    return () => observer.disconnect()
  }, [visible])

  useEffect(() => {
    if (!visible || !meshHash) return
    let objectUrl: string | null = null
    let cancelled = false
    fetch(`/api/documents/${documentId}/thumbnail/${meshHash}`, { headers: authHeaders() })
      .then((res) => (res.ok ? res.blob() : null))
      .then((blob) => {
        if (cancelled || !blob) return
        objectUrl = URL.createObjectURL(blob)
        setUrl(objectUrl)
      })
      .catch(() => {})
    return () => {
      cancelled = true
      if (objectUrl) URL.revokeObjectURL(objectUrl)
    }
  }, [visible, documentId, meshHash])

  return (
    <div
      ref={holder}
      className="aspect-[4/3] rounded-sm bg-cad-bg/60 border border-cad-border/60 overflow-hidden flex items-center justify-center"
    >
      {url ? (
        <img src={url} alt="" className="w-full h-full object-contain" draggable={false} />
      ) : (
        <span className="opacity-60">{fallback}</span>
      )}
    </div>
  )
}
