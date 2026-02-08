/**
 * web-screen-stream サンプルアプリ
 *
 * 1. セッション作成（URL 入力 + Create ボタン）
 * 2. H264Player でストリーミング表示
 * 3. セッション停止
 */

import { useState, useCallback } from 'react'
import { H264Player } from 'react-android-screen'

export function App() {
  const [sessionId, setSessionId] = useState('')
  const [url, setUrl] = useState('https://example.com')
  const [activeSession, setActiveSession] = useState<string | null>(null)
  const [wsUrl, setWsUrl] = useState<string | null>(null)
  const [status, setStatus] = useState<string>('idle')
  const [error, setError] = useState<string | null>(null)

  const createSession = useCallback(async () => {
    if (!sessionId.trim()) return
    setError(null)
    setStatus('creating...')

    try {
      const resp = await fetch('/api/sessions', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          session_id: sessionId,
          url: url || undefined,
        }),
      })

      if (!resp.ok) {
        const data = await resp.json()
        throw new Error(data.error || `HTTP ${resp.status}`)
      }

      const data = await resp.json()
      setActiveSession(data.session_id)
      setWsUrl(data.ws_url)
      setStatus('streaming')
    } catch (err) {
      setError(String(err))
      setStatus('error')
    }
  }, [sessionId, url])

  const stopSession = useCallback(async () => {
    if (!activeSession) return
    setStatus('stopping...')

    try {
      await fetch(`/api/sessions/${activeSession}`, { method: 'DELETE' })
      setActiveSession(null)
      setWsUrl(null)
      setStatus('idle')
    } catch (err) {
      setError(String(err))
    }
  }, [activeSession])

  return (
    <div style={{ maxWidth: 900, margin: '0 auto', padding: 20 }}>
      <h1 style={{ fontSize: 24, marginBottom: 20 }}>
        🖥️ web-screen-stream
      </h1>

      {/* コントロール */}
      <div style={{
        display: 'flex', gap: 10, marginBottom: 20, flexWrap: 'wrap',
        alignItems: 'center',
      }}>
        <input
          type="text"
          placeholder="Session ID"
          value={sessionId}
          onChange={(e) => setSessionId(e.target.value)}
          disabled={!!activeSession}
          style={inputStyle}
        />
        <input
          type="text"
          placeholder="URL (optional)"
          value={url}
          onChange={(e) => setUrl(e.target.value)}
          disabled={!!activeSession}
          style={{ ...inputStyle, flex: 2 }}
        />
        {!activeSession ? (
          <button onClick={createSession} style={btnStyle}>
            Create Session
          </button>
        ) : (
          <button onClick={stopSession} style={{ ...btnStyle, background: '#e74c3c' }}>
            Stop Session
          </button>
        )}
      </div>

      {/* ステータス */}
      <div style={{ marginBottom: 10, fontSize: 14, color: '#aaa' }}>
        Status: <strong>{status}</strong>
        {activeSession && <span> | Session: {activeSession}</span>}
      </div>

      {error && (
        <div style={{ color: '#e74c3c', marginBottom: 10 }}>{error}</div>
      )}

      {/* プレイヤー */}
      {wsUrl && (
        <div style={{
          border: '1px solid #333',
          borderRadius: 8,
          overflow: 'hidden',
          background: '#000',
        }}>
          <H264Player
            wsUrl={wsUrl}
            fit="contain"
            maxHeight="70vh"
            fps={5}
            debug={true}
            onConnected={() => setStatus('streaming (connected)')}
            onDisconnected={() => setStatus('disconnected')}
            onError={(err) => setError(err)}
          />
        </div>
      )}
    </div>
  )
}

const inputStyle: React.CSSProperties = {
  flex: 1,
  padding: '8px 12px',
  borderRadius: 4,
  border: '1px solid #444',
  background: '#2a2a3e',
  color: '#eee',
  fontSize: 14,
}

const btnStyle: React.CSSProperties = {
  padding: '8px 20px',
  borderRadius: 4,
  border: 'none',
  background: '#3498db',
  color: '#fff',
  cursor: 'pointer',
  fontSize: 14,
  fontWeight: 'bold',
}
