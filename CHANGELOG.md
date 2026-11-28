# Changelog

## Unreleased

### Added

- `StreamConfig.user_agent`: 自前起動する Chromium のページとポップアップの User-Agent を指定する（既定は Chromium のまま）

### Fixed

- 自前起動する Chromium が、サイトから自動操作に見えないようにした。Playwright の `launch` は `--enable-automation` などを付け、`navigator.webdriver` が true になるので、人が操作しても reCAPTCHA などでログインが拒まれていた（TVer で実測）。Chromium を普通に起動して CDP で接続する。CDP のポートは番号で渡す（0 にすると `navigator.webdriver` が true になる）。起動ごとのプロファイルは刈り取りで消す。認証付きの `proxy` は `--proxy-server` に書けないので、そのときだけ従来の `launch` を使う。`user_agent` は起動の引数で渡すので、Client Hints の `Sec-CH-UA-Platform` は実際の OS のままになる
- `SessionManager` が Chromium を刈り取るとき、起動の前後で増えた `chrome*` の PID の差分ではなく、起動ごとに環境変数で渡した一意な目印（`WSS_BROWSER_TAG`）を持つプロセスだけを対象にした。同時に起動した別の Chromium（別の `SessionManager`・別のプロセス）や別ユーザーのプロセスを止めない。親が PID 1 に付け替わった `chrome_crashpad` も、環境変数が引き継がれるので引き続き刈り取る（#12）

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
