# プロトコル仕様・ライブラリ API・FFmpeg

---

## 1. WebSocket プロトコル

Android 版と **完全に同一のプロトコル** を使用する。

```
エンドポイント:
  Step 1: WS /api/ws/stream/{session_id}      （独立サーバー）
  Step 2: WS /api/ws/web-stream/{job_id}       （Backend 統合）

方向:
  client → server: なし（受信専用）
  server → client: binary フレーム

バイナリメッセージ形式:
  - 各メッセージ = 1つの H.264 NAL unit（Annex-B 形式）
  - 先頭は必ず 0x00 0x00 0x00 0x01（4-byte start code）

Late-join:
  - 新規接続時に SPS + PPS + 最新 GOP キャッシュをキューに先詰め

エラーコード:
  - 4004: Session not found
  - 1011: Server not ready
  - 1000: 正常終了
```

→ フロントエンドの `H264Player` は `wsUrl` を変えるだけで動作する。

---

## 2. ライブラリ API（src/web_screen_stream/）

### SessionManager

```python
class SessionManager:
    """ブラウザストリーミングセッションの管理"""

    async def create(
        self,
        session_id: str,
        config: StreamConfig,
        url: str | None = None,    # Step 1 用（ブラウザ起動含む）
    ) -> BrowserStreamSession:
        """セッション作成: FFmpeg 起動 + ブロードキャスト開始"""

    async def stop(self, session_id: str) -> None:
        """セッション停止: FFmpeg 停止 + リソース解放"""

    def get(self, session_id: str) -> BrowserStreamSession | None:
        """セッション取得"""

    def list_sessions(self) -> list[dict]:
        """アクティブセッション一覧"""
```

### BrowserStreamSession

```python
class BrowserStreamSession:
    """1つのストリーミングセッション"""

    async def subscribe(self) -> AsyncIterator[bytes]:
        """WebSocket クライアントの購読（H.264 NAL units のストリーム）"""

    @property
    def status(self) -> str:
        """"starting" | "streaming" | "stopping" | "stopped" """

    @property
    def subscriber_count(self) -> int:
        """現在の視聴者数"""
```

### StreamConfig

```python
@dataclass
class StreamConfig:
    display: str = ":99"
    width: int = 1280
    height: int = 720
    framerate: int = 5
    bitrate: str = "500k"
    maxrate: str = "800k"
```

### Step 1 と Step 2 の違い

| 項目 | Step 1（独立サーバー） | Step 2（Backend 統合） |
|------|----------------------|----------------------|
| ブラウザ起動 | `SessionManager.create()` 内で Playwright 起動 | Backend の `test_runner.py` が起動（既存） |
| FFmpeg 起動 | `SessionManager.create()` 内 | `job_execution_manager.py` から呼び出し |
| WebSocket | `app/api/endpoints/stream.py` | `backend/main.py` に直接追加 |
| Xvfb | `entrypoint.sh` で起動 | `start_backend.sh` で起動 |
| URL 指定 | REST API で受け取る | テスト実行コマンドから渡される |

---

## 3. H.264 パイプライン

```
FFmpeg stdout (raw bytes)
    │
    │ asyncio subprocess PIPE
    ▼
FFmpegSource.read_stream()
    │
    │ chunk ごとに H264UnitExtractor.push(chunk) → list[NAL]
    ▼
BrowserStreamSession._broadcast(nal_units)
    │
    │ GOP キャッシュ更新 (SPS/PPS/IDR/non-IDR)
    │ 全 subscriber queue に put_nowait(nal)
    ▼
WebSocket endpoint
    │
    │ async for chunk in session.subscribe():
    │     await websocket.send_bytes(chunk)
    ▼
Frontend H264Player (JMuxer → MSE → <video>)
```

**`_H264UnitExtractor` の再利用**: `android-screen-stream` の `_H264UnitExtractor` をそのまま移植する。FFmpeg の Annex-B 出力を NAL 単位に分解するロジックは同一。

---

## 4. FFmpeg コマンド仕様

```bash
ffmpeg \
  -f x11grab \
  -video_size 1280x720 \
  -framerate 5 \
  -draw_mouse 0 \
  -i :99 \
  -c:v libx264 \
  -preset ultrafast \
  -tune zerolatency \
  -profile:v baseline \
  -level 3.1 \
  -pix_fmt yuv420p \
  -g 10 \
  -keyint_min 10 \
  -sc_threshold 0 \
  -b:v 500k \
  -maxrate 800k \
  -bufsize 500k \
  -f h264 \
  -
```

| パラメータ | 値 | 理由 |
|-----------|-----|------|
| `-framerate 5` | 5fps | 低負荷。テスト画面は高 FPS 不要 |
| `-preset ultrafast` | - | 最小エンコード遅延 |
| `-tune zerolatency` | - | ゼロレイテンシモード（バッファなし出力） |
| `-profile:v baseline` | - | JMuxer / MSE 互換性最大化 |
| `-level 3.1` | - | 720p@30fps 以下をカバー |
| `-g 10` | 10フレーム = 2秒 | Late-join 対策。2秒に1回 I フレーム |
| `-keyint_min 10` | 10 | GOP サイズ固定 |
| `-sc_threshold 0` | 0 | シーンチェンジ検出無効化（安定 GOP） |
| `-b:v 500k` | 500kbps | テスト画面として十分 |
| `-f h264` | - | raw H.264 Annex-B 出力（MP4 コンテナなし） |
| `-` | stdout | パイプ出力 → asyncio subprocess で読み取り |

**注意**: FFmpeg の `-f h264` 出力は Annex-B 形式のため、`-bsf:v h264_mp4toannexb` は **不要**。

---

## 5. Xvfb 仮想ディスプレイ

```bash
Xvfb :99 -screen 0 1280x720x24 -ac +extension GLX +render -noreset &
export DISPLAY=:99

# 軽量ウィンドウマネージャ（Chromium のウィンドウ管理に必要）
fluxbox -display :99 &
```

| パラメータ | 値 | 理由 |
|-----------|-----|------|
| ディスプレイ番号 | `:99` | 他のディスプレイと衝突しない |
| 解像度 | `1280x720x24` | テスト対象として十分、CPU 負荷を抑制 |
| `-ac` | - | アクセス制御を無効化（コンテナ内なので不要） |
| `fluxbox` | - | Chromium がウィンドウリサイズ・フォーカスに必要 |

---

## 6. 複数セッション対応（将来拡張）

### ディスプレイ管理

初期実装は **単一セッション**（1コンテナ = 1ブラウザ = 1ストリーム）。
マルチユーザー対応時に方式 B に拡張する。

**方式 B: セッションごとに独立ディスプレイ（推奨）**
```bash
# セッション 1: :100 (SessionManager が動的に割り当て)
Xvfb :100 -screen 0 1280x720x24 &
DISPLAY=:100 chromium ...
ffmpeg -f x11grab -i :100 ...

# セッション 2: :101
Xvfb :101 -screen 0 1280x720x24 &
DISPLAY=:101 chromium ...
ffmpeg -f x11grab -i :101 ...
```

---

## 7. Step 2: Backend 統合時の変更

### Backend の変更一覧

| ファイル | 変更内容 |
|---------|---------|
| `backend/Dockerfile` | `apt: xvfb, fluxbox, ffmpeg` + Chromium 依存ライブラリ追加 |
| `backend/start_backend.sh` | Xvfb + Fluxbox の起動処理を追加 |
| `backend/pyproject.toml` | `uv add --editable ./external/web-screen-stream` |
| `backend/main.py` | WebSocket エンドポイント `/api/ws/web-stream/{job_id}` 追加 |
| `backend/job_execution_manager.py` | Web テスト (headless=false) 時にストリーミングセッション起動/停止 |

### Frontend の変更一覧

| ファイル | 変更内容 |
|---------|---------|
| `frontend/vite.config.ts` | プロキシ追加: `/ws/web-stream` → `ws://backend:8123/api` |
| `frontend/src/components/v2/execution/WebScreenPanel.tsx` | プレースホルダー → `H264Player` 実装 |

### WebSocket エンドポイント（Backend 統合後）

```python
# main.py に追加
@app.websocket("/api/ws/web-stream/{job_id}")
async def web_stream(websocket: WebSocket, job_id: str):
    await websocket.accept()
    session = stream_manager.get(job_id)
    if not session:
        await websocket.close(code=4004, reason="Session not found")
        return
    async for chunk in session.subscribe():
        await websocket.send_bytes(chunk)
```

### Vite プロキシ（Frontend 統合後）

```typescript
'/ws/web-stream': {
  target: mode === 'docker'
    ? 'ws://backend:8123/api'
    : 'ws://localhost:8123/api',
  ws: true,
  changeOrigin: true,
},
```

### WebScreenPanel.tsx（Frontend 統合後）

```tsx
import { H264Player } from 'react-android-screen';

export const WebScreenPanel: React.FC<{
  jobId: string | null;
  isHeadless: boolean;
  isRunning: boolean;
}> = ({ jobId, isHeadless, isRunning }) => {
  if (isHeadless || !isRunning || !jobId) {
    return <Placeholder />;
  }
  return (
    <H264Player
      wsUrl={`/ws/web-stream/${jobId}`}
      className="w-full h-full"
      fit="contain"
    />
  );
};
```
