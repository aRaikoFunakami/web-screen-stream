"""単体サーバの操作チャネルと認証状態保存 API が、control_token の無い呼び出しを拒否することのテスト."""

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

import app.main as main


@pytest.fixture
def client():
    with TestClient(main.app) as c:
        main.control_tokens["s1"] = "right-token"
        yield c
        main.control_tokens.clear()


@pytest.mark.parametrize("query", ["", "?token=wrong"])
def test_control_ws_rejects_missing_or_wrong_token(client, query):
    with pytest.raises(WebSocketDisconnect) as e:
        with client.websocket_connect(f"/api/ws/control/s1{query}") as ws:
            ws.receive_text()
    assert e.value.code == 4403


def test_storage_state_requires_token(client):
    assert client.post("/api/sessions/s1/storage-state").status_code == 403
    assert client.post("/api/sessions/s1/storage-state?token=wrong").status_code == 403


def test_private_dir_rejects_symlink(tmp_path):
    target = tmp_path / "real"
    target.mkdir()
    link = tmp_path / "link"
    link.symlink_to(target)
    with pytest.raises(PermissionError):
        main._private_dir(str(link))
