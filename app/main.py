"""FastAPI application for Step 1 standalone server.

WebSocket エンドポイント + REST API でブラウザ画面をストリーミング配信する。
マルチセッション対応: セッションごとに独立した Xvfb を動的起動する。
"""

import logging
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from web_screen_stream.config import StreamConfig
from web_screen_stream.session import SessionManager
from web_screen_stream.xvfb import XvfbManager, check_display

logger = logging.getLogger(__name__)

# XvfbManager / SessionManager のシングルトン
# XVFB_STATIC=1 の場合は XvfbManager を使わず従来のグローバル Xvfb を使用
xvfb_manager: XvfbManager | None = None
session_manager: SessionManager  # lifespan で初期化


@asynccontextmanager
async def lifespan(app: FastAPI):
    """アプリケーションのライフサイクル管理."""
    global xvfb_manager, session_manager

    static_mode = os.environ.get("XVFB_STATIC", "0") == "1"

    if static_mode:
        logger.info("Starting in static Xvfb mode (XVFB_STATIC=1)")
        xvfb_manager = None
    else:
        max_displays = int(os.environ.get("MAX_SESSIONS", "5"))
        logger.info(
            "Starting in dynamic Xvfb mode (max_displays=%d)", max_displays
        )
        xvfb_manager = XvfbManager(max_displays=max_displays)

    session_manager = SessionManager(xvfb_manager=xvfb_manager)
    logger.info("web-screen-stream server starting")
    yield
    logger.info("web-screen-stream server shutting down")
    await session_manager.stop_all()
    if xvfb_manager:
        await xvfb_manager.release_all()


app = FastAPI(
    title="web-screen-stream",
    description="Browser screen streaming via Xvfb + FFmpeg H.264 + WebSocket",
    version="0.1.0",
    lifespan=lifespan,
)


# ============================================================
# ヘルスチェック
# ============================================================


@app.get("/api/healthz")
async def healthz() -> dict:
    """ヘルスチェック."""
    if xvfb_manager:
        # 動的 Xvfb モード
        return {
            "status": "healthy",
            "active_sessions": xvfb_manager.active_count,
            "max_sessions": xvfb_manager.max_displays,
            "available_displays": xvfb_manager.available_count,
        }
    else:
        # 静的 Xvfb モード（XVFB_STATIC=1）
        display_ok = check_display()
        sessions = session_manager.list_sessions()
        return {
            "status": "healthy" if display_ok else "degraded",
            "display": display_ok,
            "active_sessions": len(sessions),
        }


# ============================================================
# REST API: セッション管理
# ============================================================


class CreateSessionRequest(BaseModel):
    """セッション作成リクエスト."""

    session_id: str
    url: str | None = None
    width: int = 1280
    height: int = 720
    framerate: int = 5
    bitrate: str = "500k"


@app.post("/api/sessions", status_code=201)
async def create_session(req: CreateSessionRequest) -> dict:
    """セッション作成: FFmpeg + (オプション) ブラウザ起動."""
    config = StreamConfig(
        width=req.width,
        height=req.height,
        framerate=req.framerate,
        bitrate=req.bitrate,
    )
    try:
        session = await session_manager.create(
            session_id=req.session_id,
            config=config,
            url=req.url,
        )
        return {
            "session_id": session.session_id,
            "status": session.status,
            "ws_url": f"/api/ws/stream/{session.session_id}",
        }
    except ValueError as e:
        return JSONResponse(status_code=409, content={"error": str(e)})
    except RuntimeError as e:
        return JSONResponse(status_code=502, content={"error": str(e)})
    except Exception as e:
        logger.exception("Unexpected error creating session %s", req.session_id)
        return JSONResponse(status_code=500, content={"error": str(e)})


@app.get("/api/sessions")
async def list_sessions() -> list[dict]:
    """アクティブセッション一覧."""
    return session_manager.list_sessions()


@app.get("/api/sessions/{session_id}")
async def get_session(session_id: str) -> dict:
    """セッション情報取得."""
    session = session_manager.get(session_id)
    if session is None:
        return JSONResponse(status_code=404, content={"error": "Session not found"})
    return {
        "session_id": session.session_id,
        "status": session.status,
        "subscribers": session.subscriber_count,
        "url": session.url,
        "display": session.display,
        "resolution": session.resolution,
        "created_at": session.created_at,
    }


@app.delete("/api/sessions/{session_id}")
async def delete_session(session_id: str) -> dict:
    """セッション停止."""
    try:
        await session_manager.stop(session_id)
        return {"session_id": session_id, "status": "stopped"}
    except KeyError:
        return JSONResponse(status_code=404, content={"error": "Session not found"})


# ============================================================
# WebSocket: H.264 ストリーミング
# ============================================================


@app.websocket("/api/ws/stream/{session_id}")
async def ws_stream(websocket: WebSocket, session_id: str):
    """H.264 NAL units を WebSocket binary フレームで配信.

    Late-join 対応: 接続時に GOP キャッシュ (SPS/PPS/IDR/non-IDR) を先送り。
    """
    session = session_manager.get(session_id)
    if session is None:
        await websocket.close(code=4004, reason="Session not found")
        return

    await websocket.accept()
    logger.info("WebSocket client connected for session %s", session_id)

    try:
        async for nal in session.subscribe():
            await websocket.send_bytes(nal)
    except WebSocketDisconnect:
        logger.info("WebSocket client disconnected for session %s", session_id)
    except Exception:
        logger.exception("WebSocket error for session %s", session_id)
    finally:
        logger.info("WebSocket client ended for session %s", session_id)

