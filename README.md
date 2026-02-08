# web-screen-stream

Docker コンテナ内で Chromium ブラウザ画面を Xvfb + FFmpeg で H.264 エンコードし、WebSocket 経由でリアルタイム配信するライブラリ。

## 概要

```
Xvfb → Chromium (Playwright) → FFmpeg x11grab → H.264 → WebSocket → H264Player
```

フロントエンド（example）は JMuxer ベースの最小 `H264Player` を同梱し、単体リポジトリとして動作する。

## クイックスタート

```bash
# 1) 取得
git clone https://github.com/aRaikoFunakami/web-screen-stream.git
cd web-screen-stream

# 2) セットアップ & 起動（backend + frontend）
make setup
```

ブラウザで http://localhost:3001/ にアクセスすると、セッション作成・切り替え・停止とストリーム再生まで一通り試せます。

停止する場合:

```bash
make down
```

## サンプル Backend API

ベースURL: `http://localhost:8200`

### Health

- `GET /api/healthz`

レスポンス例（dynamic Xvfb）:

```json
{
	"status": "healthy",
	"active_sessions": 1,
	"max_sessions": 5,
	"available_displays": 4
}
```

### セッション作成

- `POST /api/sessions`

リクエスト:

```json
{
	"session_id": "demo",
	"url": "https://example.com",
	"width": 1280,
	"height": 720,
	"framerate": 15,
	"bitrate": "500k"
}
```

パラメータ:

- `session_id` (string, required): セッションID（重複すると 409）
- `url` (string, optional): Chromium で開くURL（省略時は空ページ）
- `width`/`height` (int, default `1280`/`720`): キャプチャ解像度
- `framerate` (int, default `15`): FPS
- `bitrate` (string, default `500k`): H.264 の平均ビットレート（例: `800k`, `2M`）

レスポンス（201）:

```json
{
	"session_id": "demo",
	"status": "running",
	"ws_url": "/api/ws/stream/demo"
}
```

主なエラー:

- `409`: `session_id` が既に存在
- `502`: FFmpeg/Playwright 起動失敗など上流起因
- `500`: 予期せぬエラー

### セッション一覧

- `GET /api/sessions`

レスポンス: アクティブセッションの配列（`SessionManager.list_sessions()` 互換）

### セッション取得

- `GET /api/sessions/{session_id}`

レスポンス（200）:

```json
{
	"session_id": "demo",
	"status": "running",
	"subscribers": 1,
	"url": "https://example.com",
	"display": ":10",
	"resolution": "1280x720",
	"created_at": "2026-02-08T00:00:00Z"
}
```

エラー:

- `404`: セッションが存在しない

### セッション停止

- `DELETE /api/sessions/{session_id}`

レスポンス（200）:

```json
{
	"session_id": "demo",
	"status": "stopped"
}
```

エラー:

- `404`: セッションが存在しない

### ストリーム購読（WebSocket）

- `GET ws://localhost:8200/api/ws/stream/{session_id}`

仕様:

- バイナリフレームで H.264 Annex-B の NAL unit を送出
- late-join でもデコード開始できるよう、接続直後に GOP キャッシュ（SPS/PPS/IDR 等）を先頭に送出

## ドキュメント

| ドキュメント | 内容 |
|-------------|------|
| [AGENTS.md](AGENTS.md) | AI/人間の作業契約 |
| [instructions/architecture.md](instructions/architecture.md) | アーキテクチャ・ディレクトリ構成 |
| [instructions/protocol.md](instructions/protocol.md) | WebSocket 仕様・ライブラリ API |
| [instructions/development.md](instructions/development.md) | 開発手順・トラブルシューティング |

## 技術スタック

- Python 3.13+ / uv
- FastAPI + uvicorn
- Xvfb + Fluxbox
- FFmpeg (x11grab → libx264)
- Playwright Chromium
- WebSocket (H.264 Annex-B binary)

## ライブラリ構造

```
src/web_screen_stream/    ← ライブラリ本体（Backend に統合可能）
app/                      ← Step 1 専用サーバー
example/                  ← サンプルフロントエンド
```

## ライセンス

Private
