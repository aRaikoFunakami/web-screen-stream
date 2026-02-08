"""FastAPI REST API / WebSocket エンドポイントのテスト.

TestClient を使用してセッション管理 API をテスト。
WebSocket / FFmpeg の実行は行わず、API の正常系・異常系をカバー。
"""

from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from app.main import app, session_manager


@pytest.fixture(autouse=True)
def _reset_session_manager():
    """テストごとに SessionManager をリセット."""
    session_manager._sessions.clear()
    session_manager._browsers.clear()
    yield
    session_manager._sessions.clear()
    session_manager._browsers.clear()


class TestHealthz:
    """GET /api/healthz のテスト."""

    def test_healthz(self):
        with TestClient(app) as client:
            resp = client.get("/api/healthz")
            assert resp.status_code == 200
            data = resp.json()
            assert data["status"] in ("healthy", "degraded")
            assert "display" in data
            assert "active_sessions" in data


class TestSessionsAPI:
    """セッション管理 REST API のテスト."""

    def test_list_sessions_empty(self):
        with TestClient(app) as client:
            resp = client.get("/api/sessions")
            assert resp.status_code == 200
            assert resp.json() == []

    def test_get_session_not_found(self):
        with TestClient(app) as client:
            resp = client.get("/api/sessions/nonexistent")
            assert resp.status_code == 404

    def test_delete_session_not_found(self):
        with TestClient(app) as client:
            resp = client.delete("/api/sessions/nonexistent")
            assert resp.status_code == 404

    @patch("app.main.session_manager.create", new_callable=AsyncMock)
    def test_create_session(self, mock_create):
        """セッション作成（FFmpeg/Playwright はモック）."""
        from web_screen_stream.session import BrowserStreamSession

        fake_session = BrowserStreamSession("test-1", None)
        fake_session._status = "streaming"
        mock_create.return_value = fake_session

        with TestClient(app) as client:
            resp = client.post(
                "/api/sessions",
                json={"session_id": "test-1", "url": "https://example.com"},
            )
            assert resp.status_code == 201
            data = resp.json()
            assert data["session_id"] == "test-1"
            assert data["status"] == "streaming"
            assert data["ws_url"] == "/api/ws/stream/test-1"

    @patch("app.main.session_manager.create", new_callable=AsyncMock)
    def test_create_duplicate_session(self, mock_create):
        """重複セッション作成は 409 を返す."""
        mock_create.side_effect = ValueError("Session test-1 already exists")

        with TestClient(app) as client:
            resp = client.post(
                "/api/sessions",
                json={"session_id": "test-1"},
            )
            assert resp.status_code == 409

    def test_create_session_minimal(self):
        """最小パラメータでのリクエスト（session_id のみ）."""
        with TestClient(app) as client:
            # session_id がない場合はバリデーションエラー
            resp = client.post("/api/sessions", json={})
            assert resp.status_code == 422


class TestWebSocket:
    """WebSocket エンドポイントのテスト."""

    def test_ws_session_not_found(self):
        """存在しないセッションの WebSocket は 4004 で閉じる."""
        with TestClient(app) as client:
            with pytest.raises(Exception):
                # TestClient の WebSocket は close code を Exception で返す
                with client.websocket_connect("/api/ws/stream/nonexistent"):
                    pass
