#!/bin/bash
set -e

PORT=${PORT:-8200}

echo "=== Web Screen Stream ==="
echo "Port: ${PORT}"

if [ "${XVFB_STATIC:-0}" = "1" ]; then
    # ==============================
    # 静的モード: 従来通りグローバル Xvfb を起動
    # ==============================
    DISPLAY_NUM=${DISPLAY_NUM:-99}
    SCREEN_WIDTH=${SCREEN_WIDTH:-1280}
    SCREEN_HEIGHT=${SCREEN_HEIGHT:-720}
    SCREEN_DEPTH=${SCREEN_DEPTH:-24}
    export DISPLAY=:${DISPLAY_NUM}

    echo "Mode: Static Xvfb (XVFB_STATIC=1)"
    echo "Display: ${DISPLAY}"
    echo "Resolution: ${SCREEN_WIDTH}x${SCREEN_HEIGHT}x${SCREEN_DEPTH}"

    # Xvfb 起動
    echo "Starting Xvfb..."
    Xvfb :${DISPLAY_NUM} \
      -screen 0 ${SCREEN_WIDTH}x${SCREEN_HEIGHT}x${SCREEN_DEPTH} \
      -ac +extension GLX +render -noreset &
    XVFB_PID=$!
    sleep 1

    if ! kill -0 $XVFB_PID 2>/dev/null; then
      echo "ERROR: Xvfb failed to start"
      exit 1
    fi

    # Fluxbox 起動
    echo "Starting Fluxbox..."
    fluxbox -display :${DISPLAY_NUM} &>/dev/null &
    sleep 1

    # ディスプレイ確認
    if xdpyinfo -display :${DISPLAY_NUM} >/dev/null 2>&1; then
      echo "Virtual display :${DISPLAY_NUM} ready."
    else
      echo "ERROR: Display :${DISPLAY_NUM} not available"
      exit 1
    fi
else
    # ==============================
    # 動的モード: SessionManager が Xvfb/Fluxbox を管理
    # ==============================
    echo "Mode: Dynamic Xvfb (managed by SessionManager)"
    echo "Max sessions: ${MAX_SESSIONS:-5}"
    # DISPLAY は設定しない（各セッションが独自 display を使用）
fi

# FastAPI サーバー起動
# `uv run` 経由だと uv 自身が PID 1 のまま残り、Python は子プロセスになる。
# reparent された孤児プロセス（chrome_crashpad 等）は PID 1 でないと
# os.waitpid で回収できず zombie 化するため、venv の uvicorn を直接 exec して
# Python プロセス自体を PID 1 にする（SessionManager._reap_chrome_pids 参照）。
echo "Starting server on port ${PORT}..."
exec .venv/bin/uvicorn app.main:app --host 0.0.0.0 --port ${PORT}
