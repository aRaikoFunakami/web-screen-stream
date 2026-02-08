# 開発ガイド

---

## 1. Docker 開発サイクル

### 基本コマンド

```bash
# ビルド + 起動
docker compose up -d --build

# ログ確認
docker compose logs -f

# コンテナ内で確認
docker compose exec server bash

# 停止
docker compose down

# ボリューム含めて完全削除
docker compose down --volumes
```

### docker-compose.yml

```yaml
services:
  server:
    build:
      context: .
      dockerfile: Dockerfile
    ports:
      - "8200:8200"
    environment:
      - DISPLAY_NUM=99
      - SCREEN_WIDTH=1280
      - SCREEN_HEIGHT=720
      - PORT=8200
    deploy:
      resources:
        limits:
          memory: 2G
          cpus: '2.0'
```

---

## 2. サンプルフロントエンド開発

### 起動手順

```bash
# 1. react-android-screen をビルド（初回のみ）
cd ../../screen-stream-capture/packages/react-android-screen
npm install && npm run build

# 2. Docker コンテナ起動（WebSocket サーバー）
cd ../../web-screen-stream
docker compose up -d

# 3. サンプルフロントエンド起動（ホスト PC）
cd example
npm install
npm run dev
# → http://localhost:5173 でアクセス
```

### react-android-screen のローカル参照

`example/package.json` で `file:` プロトコルを使用：
```json
"react-android-screen": "file:../../screen-stream-capture/packages/react-android-screen"
```

- npm/yarn link は不要。`npm install` だけでローカルパッケージがリンクされる
- smartestiroid-ui の `frontend/package.json` と同じパターン

---

## 3. テスト実行

```bash
# ユニットテスト（Docker 不要）
uv run pytest tests/ -v

# 統合テスト（Docker コンテナ内で実行）
docker compose exec server uv run pytest tests/ -v

# 特定のテストのみ
uv run pytest tests/test_session.py -v
```

---

## 4. 依存関係管理

### Python (uv)

```bash
# ライブラリの追加
uv add <package-name>

# 開発用
uv add --dev <package-name>

# 依存関係の同期
uv sync
```

**禁止**: `pip install` を直接使用しない。必ず `uv add` を使う。

### サンプルフロントエンド (npm)

```bash
cd example
npm install <package-name>
```

---

## 5. Dockerfile 概要

```dockerfile
FROM python:3.13-slim

# システム依存: xvfb, fluxbox, ffmpeg, Chromium依存ライブラリ, 日本語フォント
# Python 環境: uv + pyproject.toml
# Playwright: chromium のみインストール
# ソースコード: src/ + app/
# エントリポイント: entrypoint.sh
```

### entrypoint.sh の起動順序

1. Xvfb 起動（仮想ディスプレイ :99）
2. Xvfb 起動確認（`kill -0` でプロセス生存チェック）
3. Fluxbox 起動（ウィンドウマネージャ）
4. FastAPI サーバー起動（`exec uv run uvicorn`）

---

## 6. リソース見積もり

| プロセス | CPU (1コア基準) | メモリ |
|---------|----------------|--------|
| Xvfb | ~5% | ~50MB |
| Fluxbox | ~1% | ~10MB |
| Chromium | ~20-40% | ~300-500MB |
| FFmpeg (5fps) | ~10-20% | ~50MB |
| FastAPI server | ~5% | ~100MB |
| **合計** | **~40-70%** | **~500-700MB** |

### 帯域幅

| FPS | ビットレート | 1分あたりデータ量 |
|-----|------------|-----------------|
| 2 fps | ~200 kbps | ~1.5 MB |
| 5 fps | ~500 kbps | ~3.75 MB |

### End-to-End 遅延

| 区間 | 遅延 |
|------|------|
| Xvfb → FFmpeg x11grab | ~0ms |
| FFmpeg エンコード | ~50-100ms |
| WebSocket 送信 | ~1-5ms (LAN) |
| JMuxer デコード + MSE | ~100-200ms |
| **合計** | **~200-400ms** |

### Apple Silicon 対応

| ケース | 動作 | 注意点 |
|--------|------|--------|
| arm64 ネイティブ | ✅ 最良 | Playwright arm64 Chromium が利用可能 |
| Rosetta 2 (amd64) | ✅ 動作 | 性能 ~30% 低下 |
| Docker Desktop `--platform linux/amd64` | ✅ 動作 | QEMU 経由、FFmpeg は遅い可能性 |

**推奨**: arm64 ネイティブビルド。

---

## 7. セキュリティ考慮

### Step 1（開発用）

| 項目 | 対応 |
|------|------|
| WebSocket 認証 | なし（開発用） |
| HTTPS/WSS | HTTP のみ |
| コンテナ分離 | Chromium は `--no-sandbox` で起動 |

Step 2 で Backend に統合する際に、Keycloak 認証やアクセス制御を適用する。

---

## 8. トラブルシューティング

### Xvfb が起動しない

```bash
# コンテナ内で確認
docker compose exec server bash
Xvfb :99 -screen 0 1280x720x24 -ac &
xdpyinfo -display :99
```

### FFmpeg が H.264 を出力しない

```bash
# コンテナ内で手動実行
export DISPLAY=:99
ffmpeg -f x11grab -video_size 1280x720 -framerate 5 -i :99 \
  -c:v libx264 -preset ultrafast -f h264 - | hexdump -C | head -50
# → 0x00 0x00 0x00 0x01 が出力されるか確認
```

### Chromium が起動しない

```bash
# 依存ライブラリの確認
docker compose exec server bash
DISPLAY=:99 chromium --no-sandbox --disable-gpu about:blank
# エラーメッセージを確認
```

### H264Player に映像が表示されない

1. WebSocket 接続確認: ブラウザ DevTools → Network → WS
2. バイナリデータ受信確認: メッセージが binary か text か
3. NAL unit 形式確認: 先頭が `0x00000001` か
4. JMuxer コンソールエラー確認

### Docker ビルドエラー

```bash
# キャッシュクリアして再ビルド
docker compose build --no-cache

# Playwright ブラウザのインストール確認
docker compose exec server uv run playwright install --with-deps chromium
```
