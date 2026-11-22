"""FastAPI REST API / WebSocket エンドポイントのテスト.

TestClient を使用してセッション管理 API をテスト。
WebSocket / FFmpeg の実行は行わず、API の正常系・異常系をカバー。
session_manager は lifespan で作られるので、TestClient を開いた後に main.session_manager を参照する。
"""

from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

import app.main as main


@pytest.fixture
def client():
    with TestClient(main.app) as c:
        yield c
    main.control_tokens.clear()


class TestHealthz:
    def test_healthz(self, client):
        resp = client.get("/api/healthz")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] in ("healthy", "degraded")
        assert "active_sessions" in data


class TestSessionsAPI:
    def test_list_sessions_empty(self, client):
        resp = client.get("/api/sessions")
        assert resp.status_code == 200
        assert resp.json() == []

    def test_get_session_not_found(self, client):
        assert client.get("/api/sessions/nonexistent").status_code == 404

    def test_delete_session_not_found(self, client):
        assert client.delete("/api/sessions/nonexistent").status_code == 404

    def test_create_session_issues_control_token(self, client):
        """セッション作成（FFmpeg/Playwright はモック）で操作用トークンが発行され、URL には載らない."""
        from web_screen_stream.session import BrowserStreamSession

        fake_session = BrowserStreamSession("test-1", None)
        fake_session._status = "streaming"
        with patch.object(main.session_manager, "create", new=AsyncMock(return_value=fake_session)):
            resp = client.post("/api/sessions", json={"session_id": "test-1", "url": "https://example.com"})
        assert resp.status_code == 201
        data = resp.json()
        assert data["session_id"] == "test-1"
        assert data["status"] == "streaming"
        assert data["ws_url"] == "/api/ws/stream/test-1"
        assert data["control_ws_url"] == "/api/ws/control/test-1"
        assert data["control_token"] == main.control_tokens["test-1"]
        assert data["control_token"] not in data["control_ws_url"]

    def test_create_duplicate_session(self, client):
        """重複セッション作成は 409 を返す."""
        with patch.object(main.session_manager, "create", new=AsyncMock(side_effect=ValueError("dup"))):
            assert client.post("/api/sessions", json={"session_id": "test-1"}).status_code == 409

    def test_create_session_minimal(self, client):
        """session_id がない場合はバリデーションエラー."""
        assert client.post("/api/sessions", json={}).status_code == 422


class TestWebSocket:
    def test_ws_session_not_found(self, client):
        """存在しないセッションの WebSocket は閉じられる."""
        with pytest.raises(Exception):
            with client.websocket_connect("/api/ws/stream/nonexistent"):
                pass
