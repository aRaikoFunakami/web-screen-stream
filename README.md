# web-screen-stream

Docker コンテナ内で Chromium ブラウザ画面を Xvfb + FFmpeg で H.264 エンコードし、WebSocket 経由でリアルタイム配信するライブラリ。

## 概要

```
Xvfb → Chromium (Playwright) → FFmpeg x11grab → H.264 → WebSocket → H264Player
```

フロントエンドでは [react-android-screen](../screen-stream-capture/packages/react-android-screen/) の `H264Player`（JMuxer ベース）をそのまま利用する。

## クイックスタート

```bash
# Docker コンテナ起動
docker compose up -d --build

# サンプルフロントエンド起動（別ターミナル）
cd example
npm install
npm run dev
# → http://localhost:5173
```

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
