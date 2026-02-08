# アーキテクチャ

## プロジェクト概要

**web-screen-stream** は、Docker コンテナ内で headless: false の Chromium を Xvfb 仮想ディスプレイに描画させ、FFmpeg の x11grab で H.264 にエンコードし、WebSocket 経由でリアルタイム配信するライブラリ。

フロントエンドでは既存の `react-android-screen` パッケージの `H264Player`（JMuxer ベース）をそのまま利用する。

---

## 全体構成

```
┌──────────────── Docker Container ──────────────────────────────────┐
│                                                                     │
│  Xvfb :99 ◄── Chromium (Playwright, headless:false, DISPLAY=:99)   │
│    │                                                                │
│    │ x11grab                                                        │
│    ▼                                                                │
│  FFmpeg (libx264 ultrafast zerolatency)                             │
│    │                                                                │
│    │ raw H.264 Annex-B (stdout pipe)                                │
│    ▼                                                                │
│  H264UnitExtractor → BrowserStreamSession → WebSocket Server :8200 │
│                                                                     │
└─────────────────────────────┬───────────────────────────────────────┘
                              │ WebSocket binary (NAL units)
                              ▼
                     H264Player (JMuxer → MSE → <video>)
```

---

## 2 段階開発アプローチ

### なぜ 2 段階か

最終的にブラウザは **Backend コンテナ内** で Playwright が起動する。Xvfb も FFmpeg も同じコンテナ内で動く必要がある（X11 ディスプレイはプロセスローカルなため）。

しかし、いきなり Backend コンテナに統合すると：
- Backend の起動・テストが重くなり、開発サイクルが遅い
- 「Xvfb + FFmpeg + H.264 パイプライン」の問題と「Backend 統合」の問題が混在

そこで `screen-stream-capture` と同じパターンを採用する。

### フェーズ

| フェーズ | 内容 | 状態 |
|---------|------|------|
| **Step 1** | 独立 Docker コンテナ + サンプルフロントエンドで動作確認 | 開発中 |
| **Step 2** | ライブラリを Backend コンテナに editable install で統合 | 未着手 |

| 観点 | 説明 |
|------|------|
| **前例** | `screen-stream-capture` がまったく同じパターン（独立 Docker → editable install 統合） |
| **問題分離** | FFmpeg/Xvfb の問題と Backend 統合の問題を切り分けられる |
| **再利用性** | ライブラリとして独立しているため、他プロジェクトでも使える |
| **高速な開発サイクル** | 独立コンテナなら変更→確認が数秒（Backend 全体リビルドは不要） |

---

## ディレクトリ構成

```
web-screen-stream/
├── AGENTS.md                    ← 契約（AI向けルール）
├── README.md
├── Makefile                     ← docker compose up/down/build
├── docker-compose.yml           ← 開発用
├── pyproject.toml               ← uv プロジェクト
├── Dockerfile                   ← Xvfb + FFmpeg + Chromium + FastAPI
├── entrypoint.sh                ← Xvfb + Fluxbox + uvicorn 起動
│
├── instructions/                ← プロジェクト固有の技術ドキュメント
│   ├── architecture.md          ← 本ファイル
│   ├── protocol.md              ← WebSocket 仕様・ライブラリ API・FFmpeg
│   └── development.md           ← 開発手順・トラブルシューティング
│
├── src/
│   └── web_screen_stream/       ← ★ ライブラリ本体（Step 2 で Backend に取り込む）
│       ├── __init__.py
│       ├── config.py            ← StreamConfig (解像度/FPS/品質)
│       ├── session.py           ← BrowserStreamSession + SessionManager
│       ├── ffmpeg_source.py     ← FFmpeg x11grab → H.264 stdout reader
│       ├── h264_extractor.py    ← _H264UnitExtractor (android版から流用)
│       └── xvfb.py              ← Xvfb ディスプレイ管理
│
├── app/                         ← Step 1 専用サーバー（Step 2 では不使用）
│   ├── main.py                  ← FastAPI アプリケーション
│   └── api/
│       ├── router.py
│       └── endpoints/
│           ├── stream.py        ← WS /api/ws/stream/{session_id}
│           ├── sessions.py      ← REST セッション管理
│           └── healthz.py
│
├── example/                     ← サンプルフロントエンド（開発確認用）
│   ├── index.html
│   ├── main.tsx
│   ├── package.json             ← react-android-screen をローカル参照
│   └── vite.config.ts
│
└── tests/
    └── test_session.py
```

### ディレクトリの役割分担（重要）

| ディレクトリ | 役割 | Step 2 での扱い |
|-------------|------|----------------|
| `src/web_screen_stream/` | ライブラリ本体 | Backend に editable install で取り込む |
| `app/` | Step 1 専用サーバー | **使わない**（Backend の main.py に統合） |
| `example/` | サンプルフロントエンド | **使わない**（Frontend の WebScreenPanel.tsx に統合） |

---

## 技術スタック

| 項目 | 技術 |
|------|------|
| 言語 | Python 3.13+ |
| パッケージ管理 | uv |
| Web フレームワーク | FastAPI + uvicorn |
| ブラウザ | Playwright Chromium |
| 仮想ディスプレイ | Xvfb + Fluxbox |
| ビデオエンコード | FFmpeg x11grab → libx264 |
| ストリーミング | WebSocket binary (H.264 Annex-B NAL units) |
| フロントエンド（example） | React 19 + Vite 7 + react-android-screen (H264Player) |
| テスト | pytest |

---

## Android 版との比較

| 項目 | Android (screen-stream-capture) | Web (web-screen-stream) |
|------|--------------------------------|------------------------|
| **ソースデバイス** | Android 端末 (scrcpy) | Xvfb 仮想ディスプレイ (FFmpeg) |
| **エンコーダ** | scrcpy-server (端末上) | FFmpeg libx264 (コンテナ内) |
| **H.264 入力** | TCP (adb forward) | subprocess stdout (pipe) |
| **NAL 抽出** | `_H264UnitExtractor` | `_H264UnitExtractor`（同一） |
| **マルチキャスト** | `StreamSession` | `BrowserStreamSession`（同設計） |
| **Late-join** | SPS/PPS + GOP キャッシュ | SPS/PPS + GOP キャッシュ（同設計） |
| **WebSocket** | `WS /api/ws/stream/{serial}` | `WS /api/ws/stream/{session_id}` |
| **プロトコル** | H.264 Annex-B binary | H.264 Annex-B binary（同一） |
| **Frontend** | `H264Player` (JMuxer) | `H264Player` (JMuxer)（同一） |
| **統合パターン** | 別サービスのまま | ライブラリとして Backend に取り込み |

**統合パターンが異なる理由**:
- Android: scrcpy は Android デバイスに接続するため、Backend とは独立で動作可能
- Web: Playwright が起動する Chromium は Backend コンテナ内で動くため、Xvfb + FFmpeg も同居が必要

---

## 関連リポジトリ・ドキュメント

| リソース | パス | 用途 |
|---------|------|------|
| **設計書** | `../../work/web_screen_stream/design.md` | 全体設計（必読） |
| **開発計画** | `../../work/web_screen_stream/plan.md` | フェーズ管理・TODO |
| **親 AGENTS.md** | `../../AGENTS.md` | smartestiroid-ui 全体ルール |
| **screen-stream-capture** | `../screen-stream-capture/` | Android 版ストリーミング（参考実装） |
| `_H264UnitExtractor` | `../screen-stream-capture/packages/android-screen-stream/src/android_screen_stream/session.py` | NAL unit パーサー（移植元） |
| `react-android-screen` | `../screen-stream-capture/packages/react-android-screen/` | H264Player（フロントエンドで再利用） |
| **API 仕様書** | `../../backend/docs/api_spec.md` | Backend API（Step 2 で参照） |
| **SSE ガイド** | `../../docs/SSE_EVENT_HANDLING_GUIDE.md` | SSE 契約（Step 2 で参照） |
