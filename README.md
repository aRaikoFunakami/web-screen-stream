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

注意:

- 初回は Docker イメージのビルド（Playwright/Chromium 含む）で数分かかることがあります
- 使用ポート: backend `8200` / frontend `3001`（他プロセスが使用中だと起動に失敗します）

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
	"ws_url": "/api/ws/stream/demo",
	"control_ws_url": "/api/ws/control/demo",
	"control_token": "<操作用トークン>"
}
```

`control_token` は操作チャネルと認証状態の保存にだけ使う秘密の値。URL には載せない。

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

### 操作チャネル（WebSocket）

- `GET ws://localhost:8200/api/ws/control/{session_id}`

配信中の Xvfb へ、クライアントのマウス・キー・文字を XTEST で注入する。映像の WebSocket とは別で、同じセッションを同時に操作できるのは 1 接続だけ。

1. 接続後 5 秒以内に最初のメッセージで `{"type":"auth","token":"<control_token>"}` を送る。一致しなければ `4403` で切られる。通ると `{"type":"ready"}` が返る
2. 以降は次の JSON を送る（任意で `seq` を付けると応答に入る）
   - `{"type":"pointer","action":"down"|"move"|"up","x":0..1,"y":0..1}`（表示中の映像に対する比率）
   - `{"type":"wheel","x":0..1,"y":0..1,"dy":-10..10}`（`dy>0` で下）
   - `{"type":"key","key":"Enter"|"Backspace"|"Tab"|"Escape"|"Delete"|"ArrowLeft"|"ArrowRight"|"ArrowUp"|"ArrowDown"|"Home"|"End"}`
   - `{"type":"text","text":"..."}`（IME 確定後の文字列。1 メッセージ 256 文字まで。日本語も可）
3. 応答は `{"type":"ack","seq":n,"ok":true}` または `{"type":"error","seq":n,"error":"..."}`

`XVFB_STATIC=1`（全セッションが 1 つのディスプレイを共有）では、操作チャネルはディスプレイごとに 1 つだけ開け、フォーカスのある別セッションのウィンドウにも入力が届く。人に操作させるときは動的 Xvfb（既定）で使う。

切断コード: `4403` 認証失敗（存在しないセッションもトークンが無いのでこれになる） / `4004` 認証後にセッションが停止された / `4009` 他の接続が操作中 / `1011` X に繋がらない。

入力した文字の内容はログに出さない。キー配列に無い文字は空きキーコードへ一時的に割り当て、`xkbcomp`（Xvfb の依存で入る）で配列を読み直させてから押す。

### 認証状態の保存

- `POST /api/sessions/{session_id}/storage-state`（ヘッダ `Authorization: Bearer <control_token>` 必須。無い・違うと `403`）

人が操作チャネルでログインした後、このサーバが起動した Chromium の Playwright storageState（HttpOnly Cookie・localStorage・IndexedDB）を `~/.web-screen-stream/states/{session_id}.json` に保存する（環境変数 `STORAGE_STATE_DIR` で変更可）。ディレクトリは `0700`。ファイルは `0600` の一時ファイルを排他作成してから置き換える（同じセッションの前回の保存は上書きされ、失敗したときは残る）。レスポンスは値を含まない要約（Cookie の名前・ドメイン・HttpOnly、localStorage のキー名）と保存先のパス。

ライブラリとして使うときは `SessionManager.open_control(session_id)`（2 つ目は `ControlBusyError`）と `SessionManager.export_storage_state(session_id, path=None)` を使う。`StreamConfig(draw_mouse=True)` で作ったセッションは、マウスカーソルを映像に描く（既定は描かない。操作させるセッションだけ True にする。単体サーバの REST からは指定できない）。`StreamConfig(proxy={"server": "http://127.0.0.1:8899"})` は、自前起動する Chromium を Playwright の `launch(proxy=...)` でそのプロキシに通す（WebRTC の UDP もプロキシの外へ出さない）。`server` は http(s) の URL（スキーム・ホスト・ポートだけ）で、ほかに使える項目は `username`・`password`。`bypass` は使えない。ブラウザを起動する（`url` を渡す）`create()` でだけ使え、誤りは `ProxyConfigError`。単体サーバの REST からは指定できない。

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
