"""FastAPI application for Step 1 standalone server.

WebSocket エンドポイント + REST API でブラウザ画面をストリーミング配信する。
操作チャネル(WS /api/ws/control/{session_id})でクライアントのマウス・キー・文字を Xvfb へ注入し、
POST /api/sessions/{session_id}/storage-state で人がログインした後の認証状態を保存できる。
マルチセッション対応: セッションごとに独立した Xvfb を動的起動する。
"""

import json
import logging
import os
import secrets
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
control_tokens: dict[str, str] = {}  # session_id → 操作チャネルのトークン(create で発行、delete で破棄)


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
    framerate: int = 15
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
        control_tokens[session.session_id] = secrets.token_urlsafe(24)
        return {
            "session_id": session.session_id,
            "status": session.status,
            "ws_url": f"/api/ws/stream/{session.session_id}",
            # 操作チャネルはこのトークンを知っている呼び出し元だけが開ける(?token=)
            "control_ws_url": f"/api/ws/control/{session.session_id}",
            "control_token": control_tokens[session.session_id],
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
        control_tokens.pop(session_id, None)
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



# ============================================================
# WebSocket: 操作チャネル(client → server の入力)
# ============================================================


@app.websocket("/api/ws/control/{session_id}")
async def ws_control(websocket: WebSocket, session_id: str, token: str = ""):
    """クライアントの入力(pointer/wheel/key/text の JSON)を XTEST で注入する. 同時に 1 接続だけ.

    セッション作成時に返した control_token(?token=)が一致しない接続は accept 前に拒否する。
    配信(閲覧)より強い権限なので、閲覧用 URL を知っているだけでは操作できない。
    """
    expected = control_tokens.get(session_id)
    if not expected or not secrets.compare_digest(token, expected):
        await websocket.close(code=4403, reason="Forbidden")
        return
    try:
        channel = session_manager.open_control(session_id)
    except KeyError:
        await websocket.close(code=4004, reason="Session not found")
        return
    except RuntimeError as e:
        await websocket.close(code=4009, reason=str(e)[:120])
        return
    await websocket.accept()
    try:
        while True:
            try:
                raw = json.loads(await websocket.receive_text())
            except json.JSONDecodeError:
                await websocket.send_json({"type": "error", "seq": None, "error": "invalid JSON"})
                continue
            await websocket.send_json(await channel.handle(raw))
    except WebSocketDisconnect:
        pass
    finally:
        channel.close()


# 認証状態の保存先。共有の /tmp は使わない(他ユーザーの先回り作成・symlink を避ける)
STORAGE_STATE_DIR = os.environ.get("STORAGE_STATE_DIR", os.path.join(os.path.expanduser("~"), ".web-screen-stream", "states"))


def _private_dir(path: str) -> str:
    """所有者だけが使えるディレクトリを用意する. symlink・他人所有なら拒否する."""
    os.makedirs(path, mode=0o700, exist_ok=True)
    st = os.lstat(path)
    if os.path.islink(path) or st.st_uid != os.getuid():
        raise PermissionError(f"unsafe storage state dir: {path}")
    os.chmod(path, 0o700)
    return path


@app.post("/api/sessions/{session_id}/storage-state")
async def save_storage_state(session_id: str, token: str = "") -> dict:
    """人がログインした後の認証状態をファイルに保存し、中身の値を含まない要約だけを返す(control_token 必須)."""
    expected = control_tokens.get(session_id)
    if not expected or not secrets.compare_digest(token, expected):
        return JSONResponse(status_code=403, content={"error": "Forbidden"})
    try:
        state = await session_manager.export_storage_state(session_id)
    except KeyError as e:
        return JSONResponse(status_code=404, content={"error": str(e)})
    path = os.path.join(_private_dir(STORAGE_STATE_DIR), f"{session_id}.json")
    if os.path.lexists(path):
        os.unlink(path)  # 上書きは消してから排他作成(既存の symlink を辿らない)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(state, f)
    return {
        "path": path,
        "cookies": [{"name": c["name"], "domain": c["domain"], "httpOnly": c["httpOnly"]} for c in state["cookies"]],
        "origins": [{"origin": o["origin"], "localStorage": [i["name"] for i in o["localStorage"]]} for o in state["origins"]],
    }
