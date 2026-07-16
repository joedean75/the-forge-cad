import { useEffect, useState } from 'react'
import { getAuthToken } from '../api/client'
import { useDocumentStore } from '../store/useDocumentStore'

export interface Peer {
  id: string
  email: string
}

/**
 * Live collaboration channel for the open document: presence (who else is
 * here) and silent refresh when a collaborator changes the part. Reconnects
 * with a small backoff; cleans up on unmount / document switch.
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

    const connect = () => {
      const token = getAuthToken() ?? ''
      const proto = location.protocol === 'https:' ? 'wss' : 'ws'
      ws = new WebSocket(`${proto}://${location.host}/api/documents/${docId}/ws?token=${encodeURIComponent(token)}`)
      ws.onmessage = (e) => {
        try {
          const msg = JSON.parse(e.data as string)
          if (msg.type === 'presence') {
            setPeers(msg.users as Peer[])
          } else if (msg.type === 'document_updated') {
            const st = useDocumentStore.getState()
            // Our own edits already updated local state (same updated_at) —
            // only a *different* timestamp means someone else changed the part.
            if (!st.busy && st.doc?.id === docId && st.doc.updated_at !== msg.updated_at) {
              st.refreshDoc()
            }
          }
        } catch {
          /* ignore malformed frames */
        }
      }
      ws.onclose = () => {
        setPeers([])
        if (!closed) retry = window.setTimeout(connect, 3000)
      }
    }

    connect()
    ping = window.setInterval(() => {
      if (ws?.readyState === WebSocket.OPEN) ws.send('ping')
    }, 30000)

    return () => {
      closed = true
      window.clearInterval(ping)
      window.clearTimeout(retry)
      ws?.close()
      setPeers([])
    }
  }, [docId])

  return peers
}
