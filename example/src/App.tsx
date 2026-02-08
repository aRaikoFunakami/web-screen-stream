/**
 * web-screen-stream サンプルアプリ
 *
 * URL を入力（またはプリセットから選択）して「▶ 開始」を押すだけ。
 * Docker 内の Chromium がそのページを開き、画面を H.264 でリアルタイム配信する。
 */

import { useState, useCallback, useRef } from 'react'
import { H264Player } from 'react-android-screen'

const PRESETS = [
  { label: 'Example Domain', url: 'https://example.com' },
  { label: 'The Internet', url: 'https://the-internet.herokuapp.com/' },
  { label: 'Wikipedia', url: 'https://ja.wikipedia.org/' },
  { label: 'GitHub', url: 'https://github.com/' },
]

let sessionCounter = 0

export function App() {
  const [url, setUrl] = useState(PRESETS[0].url)
  const [activeSession, setActiveSession] = useState<string | null>(null)
  const [wsUrl, setWsUrl] = useState<string | null>(null)
  const [status, setStatus] = useState<'idle' | 'creating' | 'streaming' | 'stopping' | 'error'>('idle')
  const [error, setError] = useState<string | null>(null)
  const sessionIdRef = useRef<string | null>(null)

  const startStreaming = useCallback(async () => {
    if (!url.trim()) return
    setError(null)
    setStatus('creating')

    const sid = `session-${Date.now()}-${++sessionCounter}`
    sessionIdRef.current = sid

    try {
      const resp = await fetch('/api/sessions', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ session_id: sid, url }),
      })

      if (!resp.ok) {
        const text = await resp.text()
        let msg = `HTTP ${resp.status}`
        try {
          const data = JSON.parse(text)
          msg = data.error || data.detail || msg
        } catch {
          msg = text || msg
        }
        throw new Error(msg)
      }

      const data = await resp.json()
      setActiveSession(data.session_id)
      setWsUrl(data.ws_url)
      setStatus('streaming')
    } catch (err) {
      setError(String(err))
      setStatus('error')
    }
  }, [url])

  const stopStreaming = useCallback(async () => {
    if (!activeSession) return
    setStatus('stopping')

    try {
      await fetch(`/api/sessions/${activeSession}`, { method: 'DELETE' })
    } catch {
      // ignore cleanup errors
    }
    setActiveSession(null)
    setWsUrl(null)
    setStatus('idle')
  }, [activeSession])

  const isActive = !!activeSession

  return (
    <div style={{ maxWidth: 960, margin: '0 auto', padding: '20px 24px' }}>
      {/* ヘッダー */}
      <h1 style={{ fontSize: 22, marginBottom: 4 }}>
        🖥️ web-screen-stream
      </h1>
      <p style={{ color: '#888', fontSize: 13, marginBottom: 20 }}>
        Docker 内の Chromium ブラウザ画面を H.264 でリアルタイム配信するデモ
      </p>

      {/* URL 入力 + プリセット */}
      <div style={{ marginBottom: 16 }}>
        <div style={{ display: 'flex', gap: 8, marginBottom: 8 }}>
          <input
            type="url"
            placeholder="https://... 表示したい URL を入力"
            value={url}
            onChange={(e) => setUrl(e.target.value)}
            disabled={isActive}
            onKeyDown={(e) => e.key === 'Enter' && !isActive && startStreaming()}
            style={{ ...inputStyle, flex: 1 }}
          />
          {!isActive ? (
            <button
              onClick={startStreaming}
              disabled={!url.trim() || status === 'creating'}
              style={{
                ...btnStyle,
                background: status === 'creating' ? '#666' : '#27ae60',
                cursor: status === 'creating' ? 'wait' : 'pointer',
              }}
            >
              {status === 'creating' ? '⏳ 起動中...' : '▶ 開始'}
            </button>
          ) : (
            <button onClick={stopStreaming} style={{ ...btnStyle, background: '#e74c3c' }}>
              ⏹ 停止
            </button>
          )}
        </div>

        {/* プリセットボタン */}
        {!isActive && (
          <div style={{ display: 'flex', gap: 6, flexWrap: 'wrap', alignItems: 'center' }}>
            <span style={{ color: '#666', fontSize: 12, lineHeight: '28px' }}>サンプルURL:</span>
            {PRESETS.map((p) => (
              <button
                key={p.url}
                onClick={() => setUrl(p.url)}
                style={{
                  ...presetStyle,
                  background: url === p.url ? '#3498db' : '#2a2a3e',
                  borderColor: url === p.url ? '#3498db' : '#444',
                }}
              >
                {p.label}
              </button>
            ))}
            <span style={{ color: '#555', fontSize: 11, marginLeft: 4 }}>※ 任意の URL も直接入力可</span>
          </div>
        )}
      </div>

      {/* エラー表示 */}
      {error && (
        <div style={{
          color: '#e74c3c', background: '#2d1a1a', padding: '8px 12px',
          borderRadius: 4, marginBottom: 12, fontSize: 13,
        }}>
          ❌ {error}
        </div>
      )}

      {/* プレイヤー or プレースホルダー */}
      {wsUrl ? (
        <div style={{
          border: '1px solid #333', borderRadius: 8,
          overflow: 'hidden', background: '#000',
          /* 16:9 のアスペクト比を維持しつつ画面全体を表示 */
          aspectRatio: '16 / 9',
          width: '100%',
          position: 'relative',
        }}>
          <H264Player
            wsUrl={wsUrl}
            fit="contain"
            maxHeight="80vh"
            fps={5}
            debug={true}
            onConnected={() => setStatus('streaming')}
            onDisconnected={() => {
              if (activeSession) setError('接続が切断されました')
            }}
            onError={(err) => setError(err)}
          />
        </div>
      ) : (
        <div style={{
          border: '1px dashed #444', borderRadius: 8, background: '#1a1a2e',
          display: 'flex', flexDirection: 'column', alignItems: 'center',
          justifyContent: 'center', aspectRatio: '16 / 9', width: '100%',
          color: '#666', fontSize: 15, gap: 8,
        }}>
          {status === 'creating' ? (
            <span>⏳ Chromium を起動中...</span>
          ) : (
            <>
              <span style={{ fontSize: 40 }}>🌐</span>
              <span>URL を入力して「▶ 開始」を押すと</span>
              <span>ブラウザ画面がここにリアルタイム表示されます</span>
            </>
          )}
        </div>
      )}

      {/* ステータスバー */}
      {isActive && (
        <div style={{
          marginTop: 8, fontSize: 12, color: '#666',
          display: 'flex', justifyContent: 'space-between',
        }}>
          <span>Session: {activeSession}</span>
          <span>URL: {url}</span>
        </div>
      )}
    </div>
  )
}

const inputStyle: React.CSSProperties = {
  padding: '10px 14px',
  borderRadius: 6,
  border: '1px solid #444',
  background: '#2a2a3e',
  color: '#eee',
  fontSize: 15,
}

const btnStyle: React.CSSProperties = {
  padding: '10px 24px',
  borderRadius: 6,
  border: 'none',
  color: '#fff',
  cursor: 'pointer',
  fontSize: 15,
  fontWeight: 'bold',
  whiteSpace: 'nowrap',
}

const presetStyle: React.CSSProperties = {
  padding: '4px 12px',
  borderRadius: 4,
  border: '1px solid #444',
  color: '#ccc',
  cursor: 'pointer',
  fontSize: 12,
}
