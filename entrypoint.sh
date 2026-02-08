#!/bin/bash
set -e

DISPLAY_NUM=${DISPLAY_NUM:-99}
SCREEN_WIDTH=${SCREEN_WIDTH:-1280}
SCREEN_HEIGHT=${SCREEN_HEIGHT:-720}
SCREEN_DEPTH=${SCREEN_DEPTH:-24}
PORT=${PORT:-8200}

export DISPLAY=:${DISPLAY_NUM}

echo "=== Web Screen Stream ==="
echo "Display: ${DISPLAY}"
echo "Resolution: ${SCREEN_WIDTH}x${SCREEN_HEIGHT}x${SCREEN_DEPTH}"
echo "Port: ${PORT}"

# 1. Xvfb 起動
echo "Starting Xvfb..."
Xvfb :${DISPLAY_NUM} \
  -screen 0 ${SCREEN_WIDTH}x${SCREEN_HEIGHT}x${SCREEN_DEPTH} \
  -ac +extension GLX +render -noreset &
XVFB_PID=$!
sleep 1

# Xvfb 起動確認
if ! kill -0 $XVFB_PID 2>/dev/null; then
  echo "ERROR: Xvfb failed to start"
  exit 1
fi

# 2. Fluxbox 起動（ウィンドウマネージャ）
echo "Starting Fluxbox..."
fluxbox -display :${DISPLAY_NUM} &>/dev/null &
sleep 1

# 3. ディスプレイ確認
echo "Verifying display..."
if xdpyinfo -display :${DISPLAY_NUM} >/dev/null 2>&1; then
  echo "Virtual display :${DISPLAY_NUM} ready."
else
  echo "ERROR: Display :${DISPLAY_NUM} not available"
  exit 1
fi

# 4. FastAPI サーバー起動
echo "Starting WebSocket server on port ${PORT}..."
exec uv run uvicorn app.main:app --host 0.0.0.0 --port ${PORT}
