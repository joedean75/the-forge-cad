import { useEffect, useState } from 'react'
import { api } from '../api/client'
import { useDocumentStore } from '../store/useDocumentStore'

export interface Peer {
  id: string
  email: string
}

/**
 * Live collaboration channel for the open document: presence (who else is
 * here) and silent refresh when a collaborator changes the part. Connects with
 * a single-use server ticket (so the session JWT never rides the URL),
 * reconnects with jitter; cleans up on unmount / document switch.
 */
export function useCollab(docId: string | undefined): Peer[] {
  const [peers, setPeers] = useState<Peer[]>([])

  useEffect(() => {
    if (!docId) {
      setPeers([])
      return
    }
    let ws: WebSocket | null = null
    let ping: number | undefined
    let retry: number | undefined
    let closed = false
    // A remote update that arrives while we're mid-operation is deferred, not
    // dropped: once busy clears, refresh if the timestamp still differs.
    let pendingUpdate: string | null = null

    const maybeRefresh = (updatedAt: string) => {
      const st = useDocumentStore.getState()
      if (st.doc?.id !== docId) return
      if (st.busy) {
        pendingUpdate = updatedAt
        return
      }
      // Our own edits already updated local state (same updated_at) —
      // only a *different* timestamp means someone else changed the part.
      if (st.doc.updated_at !== updatedAt) st.refreshDoc()
    }

    const unsubscribe = useDocumentStore.subscribe((st) => {
      if (!st.busy && pendingUpdate) {
        const ts = pendingUpdate
        pendingUpdate = null
        maybeRefresh(ts)
      }
    })

    const connect = async () => {
      let token = ''
      try {
        token = (await api.wsTicket(docId)).ticket
      } catch {
        /* ticket endpoint unavailable — connect unauthenticated (dev mode) */
      }
      if (closed) return
      const proto = location.protocol === 'https:' ? 'wss' : 'ws'
      ws = new WebSocket(`${proto}://${location.host}/api/documents/${docId}/ws?token=${encodeURIComponent(token)}`)
      ws.onmessage = (e) => {
        try {
          const msg = JSON.parse(e.data as string)
          if (msg.type === 'presence') {
            setPeers(msg.users as Peer[])
          } else if (msg.type === 'document_updated') {
            maybeRefresh(msg.updated_at as string)
          }
        } catch {
          /* ignore malformed frames */
        }
      }
      ws.onclose = () => {
        setPeers([])
        // Jittered backoff so many clients don't stampede a restarting server.
        if (!closed) retry = window.setTimeout(connect, 2000 + Math.random() * 2000)
      }
    }

    connect()
    ping = window.setInterval(() => {
      if (ws?.readyState === WebSocket.OPEN) ws.send('ping')
    }, 30000)

    return () => {
      closed = true
      unsubscribe()
      window.clearInterval(ping)
      window.clearTimeout(retry)
      ws?.close()
      setPeers([])
    }
  }, [docId])

  return peers
}
