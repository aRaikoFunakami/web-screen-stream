/**
 * web-screen-stream サンプルアプリ（マルチセッション対応）
 *
 * URL を入力して「▶ 開始」で新しいセッションを作成。
 * 複数セッションを同時管理し、一覧から選択してストリームを切り替え。
 * 解像度プリセットからブラウザの画面サイズを指定可能。
 */

import { useState, useCallback, useRef, useEffect } from 'react'
import { H264Player } from 'react-android-screen'

// ============================================================
// 定数
// ============================================================

const URL_PRESETS = [
  { label: 'Example Domain', url: 'https://example.com' },
  { label: 'The Internet', url: 'https://the-internet.herokuapp.com/' },
  { label: 'Wikipedia', url: 'https://ja.wikipedia.org/' },
  { label: 'GitHub', url: 'https://github.com/' },
]

const RESOLUTION_PRESETS = [
  { label: 'HD (720p)', width: 1280, height: 720 },
  { label: 'Full HD (1080p)', width: 1920, height: 1080 },
  { label: 'タブレット', width: 1024, height: 768 },
  { label: 'モバイル (横)', width: 896, height: 414 },
  { label: 'モバイル (縦)', width: 414, height: 896 },
]

const POLL_INTERVAL = 5000

let sessionCounter = 0

// ============================================================
// 型定義
// ============================================================

interface SessionInfo {
  session_id: string
  status: string
  subscribers: number
  url: string | null
  resolution: string
  display: string
  created_at: number
}

interface HealthInfo {
  status: string
  active_sessions: number
  max_sessions?: number
  available_displays?: number
}

// ============================================================
// ユーティリティ
// ============================================================

function elapsed(createdAt: number): string {
  const sec = Math.floor(Date.now() / 1000 - createdAt)
  if (sec < 60) return `${sec}秒前`
  if (sec < 3600) return `${Math.floor(sec / 60)}分前`
  return `${Math.floor(sec / 3600)}時間前`
}

function truncate(s: string, max: number): string {
  return s.length > max ? s.slice(0, max) + '…' : s
}

// ============================================================
// App
// ============================================================

export function App() {
  // 入力状態
  const [url, setUrl] = useState(URL_PRESETS[0].url)
  const [resolution, setResolution] = useState(RESOLUTION_PRESETS[0])
  const [creating, setCreating] = useState(false)
  const [error, setError] = useState<string | null>(null)

  // 視聴状態
  const [viewingSession, setViewingSession] = useState<string | null>(null)
  const [wsUrl, setWsUrl] = useState<string | null>(null)

  // セッション一覧 + ヘルス
  const [sessions, setSessions] = useState<SessionInfo[]>([])
  const [health, setHealth] = useState<HealthInfo | null>(null)
  const pollRef = useRef<ReturnType<typeof setInterval> | null>(null)

  // ──────────────────────────────────────────────
  // ポーリング
  // ──────────────────────────────────────────────
  const fetchSessions = useCallback(async () => {
    try {
      const [sessResp, healthResp] = await Promise.all([
        fetch('/api/sessions'),
        fetch('/api/healthz'),
      ])
      if (sessResp.ok) setSessions(await sessResp.json())
      if (healthResp.ok) setHealth(await healthResp.json())
    } catch {
      // ネットワークエラーは無視
    }
  }, [])

  useEffect(() => {
    fetchSessions()
    pollRef.current = setInterval(fetchSessions, POLL_INTERVAL)
    return () => { if (pollRef.current) clearInterval(pollRef.current) }
  }, [fetchSessions])

  // ──────────────────────────────────────────────
  // セッション作成
  // ──────────────────────────────────────────────
  const startStreaming = useCallback(async () => {
    if (!url.trim() || creating) return
    setError(null)
    setCreating(true)

    const sid = `session-${Date.now()}-${++sessionCounter}`

    try {
      const resp = await fetch('/api/sessions', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          session_id: sid,
          url,
          width: resolution.width,
          height: resolution.height,
        }),
      })

      if (!resp.ok) {
        const text = await resp.text()
        let msg = `HTTP ${resp.status}`
        try {
          const data = JSON.parse(text)
          msg = data.error || data.detail || msg
        } catch { msg = text || msg }
        throw new Error(msg)
      }

      const data = await resp.json()
      // 作成したセッションを視聴
      setViewingSession(data.session_id)
      setWsUrl(data.ws_url)
      await fetchSessions()
    } catch (err) {
      setError(String(err))
    } finally {
      setCreating(false)
    }
  }, [url, resolution, creating, fetchSessions])

  // ──────────────────────────────────────────────
  // セッション停止
  // ──────────────────────────────────────────────
  const stopSession = useCallback(async (sid: string) => {
    try {
      await fetch(`/api/sessions/${sid}`, { method: 'DELETE' })
    } catch { /* ignore */ }
    // 視聴中のセッションだったらプレイヤーを閉じる
    if (viewingSession === sid) {
      setViewingSession(null)
      setWsUrl(null)
    }
    await fetchSessions()
  }, [viewingSession, fetchSessions])

  // ──────────────────────────────────────────────
  // ストリーム切り替え
  // ──────────────────────────────────────────────
  const viewSession = useCallback((sid: string) => {
    setViewingSession(sid)
    setWsUrl(`/api/ws/stream/${sid}`)
    setError(null)
  }, [])

  // ──────────────────────────────────────────────
  // アスペクト比の計算
  // ──────────────────────────────────────────────
  const viewedSession = sessions.find(s => s.session_id === viewingSession)
  const aspectRatio = viewedSession
    ? (() => {
        const [w, h] = viewedSession.resolution.split('x').map(Number)
        return `${w} / ${h}`
      })()
    : `${resolution.width} / ${resolution.height}`

  // ──────────────────────────────────────────────
  // 容量表示
  // ──────────────────────────────────────────────
  const capacityText = health?.max_sessions != null
    ? `${health.active_sessions}/${health.max_sessions}`
    : `${health?.active_sessions ?? 0}`

  return (
    <div style={{ maxWidth: 1000, margin: '0 auto', padding: '20px 24px' }}>
      {/* ヘッダー */}
      <h1 style={{ fontSize: 22, marginBottom: 4 }}>
        🖥️ web-screen-stream
      </h1>
      <p style={{ color: '#888', fontSize: 13, marginBottom: 20 }}>
        マルチセッション対応 — 複数の URL を同時にブラウザ画面配信
      </p>

      {/* URL 入力 + 解像度 + 開始 */}
      <div style={{ marginBottom: 16 }}>
        <div style={{ display: 'flex', gap: 8, marginBottom: 8 }}>
          <input
            type="url"
            placeholder="https://... 表示したい URL を入力"
            value={url}
            onChange={(e) => setUrl(e.target.value)}
            onKeyDown={(e) => e.key === 'Enter' && startStreaming()}
            style={{ ...inputStyle, flex: 1 }}
          />
          <select
            value={`${resolution.width}x${resolution.height}`}
            onChange={(e) => {
              const [w, h] = e.target.value.split('x').map(Number)
              const preset = RESOLUTION_PRESETS.find(p => p.width === w && p.height === h)
              if (preset) setResolution(preset)
            }}
            style={{ ...inputStyle, width: 180 }}
          >
            {RESOLUTION_PRESETS.map((r) => (
              <option key={`${r.width}x${r.height}`} value={`${r.width}x${r.height}`}>
                {r.label}
              </option>
            ))}
          </select>
          <button
            onClick={startStreaming}
            disabled={!url.trim() || creating}
            style={{
              ...btnStyle,
              background: creating ? '#666' : '#27ae60',
              cursor: creating ? 'wait' : 'pointer',
            }}
          >
            {creating ? '⏳ 起動中...' : '▶ 開始'}
          </button>
        </div>

        {/* URL プリセット */}
        <div style={{ display: 'flex', gap: 6, flexWrap: 'wrap', alignItems: 'center' }}>
          <span style={{ color: '#666', fontSize: 12, lineHeight: '28px' }}>サンプルURL:</span>
          {URL_PRESETS.map((p) => (
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
        </div>
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
      {wsUrl && viewingSession ? (
        <div style={{
          border: '1px solid #333', borderRadius: 8,
          overflow: 'hidden', background: '#000',
          aspectRatio,
          width: '100%',
          position: 'relative',
        }}>
          <H264Player
            key={viewingSession}
            wsUrl={wsUrl}
            fit="contain"
            maxHeight="70vh"
            fps={5}
            debug={true}
            onDisconnected={() => setError('接続が切断されました')}
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
          {creating ? (
            <span>⏳ Chromium を起動中...</span>
          ) : sessions.length > 0 ? (
            <>
              <span style={{ fontSize: 32 }}>👆</span>
              <span>下のセッション一覧から選択して視聴</span>
            </>
          ) : (
            <>
              <span style={{ fontSize: 40 }}>🌐</span>
              <span>URL を入力して「▶ 開始」を押すと</span>
              <span>ブラウザ画面がここにリアルタイム表示されます</span>
            </>
          )}
        </div>
      )}

      {/* セッション一覧 */}
      <div style={{ marginTop: 16 }}>
        <h2 style={{ fontSize: 16, marginBottom: 8, color: '#ccc' }}>
          アクティブセッション ({capacityText})
        </h2>
        {sessions.length === 0 ? (
          <p style={{ color: '#666', fontSize: 13 }}>セッションなし</p>
        ) : (
          <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: 13 }}>
            <thead>
              <tr style={{ borderBottom: '1px solid #444', color: '#999' }}>
                <th style={thStyle}>ID</th>
                <th style={thStyle}>URL</th>
                <th style={thStyle}>解像度</th>
                <th style={{ ...thStyle, textAlign: 'center' }}>接続</th>
                <th style={thStyle}>稼働</th>
                <th style={{ ...thStyle, textAlign: 'center' }}>操作</th>
              </tr>
            </thead>
            <tbody>
              {sessions.map((s) => {
                const isViewing = viewingSession === s.session_id
                return (
                  <tr
                    key={s.session_id}
                    onClick={() => viewSession(s.session_id)}
                    style={{
                      borderBottom: '1px solid #333',
                      background: isViewing ? '#1e3a5f' : 'transparent',
                      cursor: 'pointer',
                    }}
                  >
                    <td style={tdStyle}>
                      {isViewing && <span style={{ marginRight: 4 }}>▶</span>}
                      {s.session_id.slice(-8)}
                    </td>
                    <td style={{ ...tdStyle, maxWidth: 200, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                      {s.url ? truncate(s.url, 40) : '—'}
                    </td>
                    <td style={tdStyle}>{s.resolution}</td>
                    <td style={{ ...tdStyle, textAlign: 'center' }}>{s.subscribers}</td>
                    <td style={tdStyle}>{elapsed(s.created_at)}</td>
                    <td style={{ ...tdStyle, textAlign: 'center' }}>
                      <button
                        onClick={(e) => { e.stopPropagation(); stopSession(s.session_id) }}
                        style={{ ...stopBtnStyle }}
                      >
                        停止
                      </button>
                    </td>
                  </tr>
                )
              })}
            </tbody>
          </table>
        )}
      </div>
    </div>
  )
}

// ============================================================
// スタイル定数
// ============================================================

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

const thStyle: React.CSSProperties = {
  textAlign: 'left',
  padding: '6px 8px',
  fontSize: 12,
  fontWeight: 'normal',
}

const tdStyle: React.CSSProperties = {
  padding: '8px 8px',
  color: '#ddd',
}

const stopBtnStyle: React.CSSProperties = {
  padding: '3px 10px',
  borderRadius: 4,
  border: '1px solid #e74c3c',
  background: 'transparent',
  color: '#e74c3c',
  cursor: 'pointer',
  fontSize: 11,
}
