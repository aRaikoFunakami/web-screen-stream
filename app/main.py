"""Minimal FastAPI application for Step 1 standalone server."""

from fastapi import FastAPI
from fastapi.responses import JSONResponse

from web_screen_stream.xvfb import check_display

app = FastAPI(
    title="web-screen-stream",
    description="Browser screen streaming via Xvfb + FFmpeg H.264 + WebSocket",
    version="0.1.0",
)


@app.get("/api/healthz")
async def healthz() -> dict:
    """ヘルスチェック."""
    display_ok = check_display()
    status = "healthy" if display_ok else "degraded"
    return {
        "status": status,
        "display": display_ok,
    }


@app.get("/api/sessions")
async def list_sessions() -> list:
    """アクティブセッション一覧 (stub)."""
    return []
