# Changelog

## 1.1.0 (2026-11-25)

### Added

- 操作チャネル: 配信中の Xvfb へマウス・キー・文字（日本語を含む）を XTEST で注入する `web_screen_stream.input`（`parse_control_message` / `XTestInjector` / `ControlChannel`）と `SessionManager.open_control`（同時に 1 接続だけ。2 つ目は `ControlBusyError`）
- `SessionManager.export_storage_state`: 自前起動した Chromium のログイン状態（HttpOnly Cookie・localStorage・IndexedDB）を取り出す。`path` を渡すと 0600 で排他作成する
- `StreamConfig.proxy`: 自前起動する Chromium を Playwright の `launch(proxy=...)` でプロキシに通す（ブラウザの通信を絞り込むプロキシに強制的に通す用途。既定は無し）。設定したときは WebRTC の UDP もプロキシの外へ出さない。`server` 以外は `username`・`password` だけを受け付け、`bypass` は拒む（誤りは `ProxyConfigError`）。あわせて `SessionManager.create` は Xvfb の display を `dataclasses.replace` で差し替え、今後足す項目も落ちないようにした
- `StreamConfig.draw_mouse`: True で FFmpeg x11grab にマウスカーソルを描かせる（既定は従来どおり描かない）
- 単体サーバ: セッション作成時に `control_token` を発行し、`WS /api/ws/control/{id}`（最初のメッセージで認証）と `POST /api/sessions/{id}/storage-state`（Bearer）を追加

### Fixed

- `tests/test_api.py` が lifespan 前の `session_manager` を import して収集エラーになっていたのを直した
- `tests/test_config.py` の framerate 既定値の期待を現行（15）に合わせた
