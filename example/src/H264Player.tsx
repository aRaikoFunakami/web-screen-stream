import type { CSSProperties } from 'react'
import { useEffect, useMemo, useRef } from 'react'
import JMuxer from 'jmuxer'

type Fit = 'contain' | 'cover'

export interface H264PlayerProps {
  wsUrl: string
  fps?: number
  debug?: boolean
  fit?: Fit
  maxHeight?: string
  onConnected?: () => void
  onDisconnected?: () => void
  onError?: (err: string) => void
}

function toAbsoluteWsUrl(wsUrl: string): string {
  if (wsUrl.startsWith('ws://') || wsUrl.startsWith('wss://')) return wsUrl
  const proto = window.location.protocol === 'https:' ? 'wss:' : 'ws:'
  const path = wsUrl.startsWith('/') ? wsUrl : `/${wsUrl}`
  return `${proto}//${window.location.host}${path}`
}

export function H264Player(props: H264PlayerProps) {
  const {
    wsUrl,
    fps = 15,
    debug = false,
    fit = 'contain',
    maxHeight,
    onConnected,
    onDisconnected,
    onError,
  } = props

  const canvasRef = useRef<HTMLCanvasElement | null>(null)
  const wsRef = useRef<WebSocket | null>(null)
  const jmuxerRef = useRef<JMuxer | null>(null)

  const absoluteWsUrl = useMemo(() => toAbsoluteWsUrl(wsUrl), [wsUrl])

  useEffect(() => {
    const canvas = canvasRef.current
    if (!canvas) return

    const jmuxer = new JMuxer({
      node: canvas,
      mode: 'video',
      fps,
      debug,
      flushingTime: 0,
      clearBuffer: true,
    })
    jmuxerRef.current = jmuxer

    const ws = new WebSocket(absoluteWsUrl)
    ws.binaryType = 'arraybuffer'
    wsRef.current = ws

    ws.onopen = () => {
      onConnected?.()
    }

    ws.onmessage = (ev) => {
      try {
        if (!(ev.data instanceof ArrayBuffer)) return
        jmuxer.feed({ video: new Uint8Array(ev.data) })
      } catch (e) {
        onError?.(e instanceof Error ? e.message : String(e))
      }
    }

    ws.onerror = () => {
      onError?.('WebSocket error')
    }

    ws.onclose = () => {
      onDisconnected?.()
    }

    return () => {
      try {
        ws.onopen = null
        ws.onmessage = null
        ws.onerror = null
        ws.onclose = null
        ws.close()
      } catch {
        // ignore
      }
      wsRef.current = null

      try {
        jmuxer.destroy()
      } catch {
        // ignore
      }
      jmuxerRef.current = null
    }
  }, [absoluteWsUrl, fps, debug, onConnected, onDisconnected, onError])

  const objectFit = fit === 'cover' ? 'cover' : 'contain'

  return (
    <canvas
      ref={canvasRef}
      style={{
        width: '100%',
        height: '100%',
        maxHeight,
        display: 'block',
        background: '#000',
        objectFit,
      } as CSSProperties}
    />
  )
}
