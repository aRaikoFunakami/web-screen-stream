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


@pytest.mark.parametrize("first", [{"type": "pointer"}, {"type": "auth", "token": "wrong"}, {"type": "auth"}])
def test_control_ws_rejects_missing_or_wrong_token(client, first):
    with pytest.raises(WebSocketDisconnect) as e:
        with client.websocket_connect("/api/ws/control/s1") as ws:
            ws.send_json(first)
            ws.receive_text()
    assert e.value.code == 4403


def test_control_ws_accepts_right_token_then_reports_missing_session(client):
    # トークンは正しいがセッション実体が無い → 認証は通り、4004 で閉じる(認証より先に存在を漏らさない)
    with pytest.raises(WebSocketDisconnect) as e:
        with client.websocket_connect("/api/ws/control/s1") as ws:
            ws.send_json({"type": "auth", "token": "right-token"})
            ws.receive_text()
    assert e.value.code == 4004


def test_storage_state_requires_bearer_token(client):
    assert client.post("/api/sessions/s1/storage-state").status_code == 403
    assert client.post("/api/sessions/s1/storage-state?token=right-token").status_code == 403  # クエリでは受けない
    assert client.post("/api/sessions/s1/storage-state", headers={"Authorization": "Bearer wrong"}).status_code == 403
    r = client.post("/api/sessions/s1/storage-state", headers={"Authorization": "Bearer right-token"})
    assert r.status_code == 404  # 認証は通る(ブラウザの無いセッションなので 404)


def test_private_dir_rejects_symlink(tmp_path):
    target = tmp_path / "real"
    target.mkdir()
    link = tmp_path / "link"
    link.symlink_to(target)
    with pytest.raises(PermissionError):
        main._private_dir(str(link))
