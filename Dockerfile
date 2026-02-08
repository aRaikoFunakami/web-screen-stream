# ============================================================
# web-screen-stream: Xvfb + Chromium + FFmpeg + WebSocket Server
# Step 1 の独立開発用コンテナ
# ============================================================
FROM python:3.13-slim

# === システム依存パッケージ ===
RUN apt-get update && apt-get install -y --no-install-recommends \
    # ディスプレイ
    xvfb \
    fluxbox \
    x11-utils \
    # FFmpeg
    ffmpeg \
    # Chromium (Playwright) 依存
    libnss3 \
    libnspr4 \
    libatk1.0-0 \
    libatk-bridge2.0-0 \
    libcups2 \
    libdrm2 \
    libxkbcommon0 \
    libxcomposite1 \
    libxdamage1 \
    libxfixes3 \
    libxrandr2 \
    libgbm1 \
    libpango-1.0-0 \
    libcairo2 \
    libasound2 \
    libatspi2.0-0 \
    libwayland-client0 \
    # 日本語フォント
    fonts-noto-cjk \
    && rm -rf /var/lib/apt/lists/*

# === Python 環境 ===
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/
WORKDIR /app
COPY pyproject.toml README.md ./
RUN uv sync --no-install-project

# === Playwright ブラウザインストール ===
RUN uv run playwright install chromium

# === ソースコード ===
COPY src/ ./src/
COPY app/ ./app/
COPY entrypoint.sh ./

# ソースコード込みで再 sync（パッケージ自体をインストール）
RUN uv sync

RUN chmod +x entrypoint.sh

EXPOSE 8200

ENTRYPOINT ["./entrypoint.sh"]
