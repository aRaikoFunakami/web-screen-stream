"""単体サーバの操作チャネルと認証状態保存 API が、control_token の無い呼び出しを拒否することのテスト."""

import os
import stat
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

import app.main as main


class FakeInjector:
    def __init__(self, *a):
        self.calls = []

    def pointer(self, *a):
        self.calls.append(("pointer", a))

    def close(self):
        pass


@pytest.fixture
def client(monkeypatch, tmp_path):
    monkeypatch.setattr(main, "STORAGE_STATE_DIR", str(tmp_path / "states"))
    monkeypatch.setattr("web_screen_stream.session.XTestInjector", FakeInjector)
    with TestClient(main.app) as c:
        main.control_tokens["s1"] = "right-token"
        yield c
    main.control_tokens.clear()


def _add_fake_session(sid="s1"):
    main.session_manager._sessions[sid] = SimpleNamespace(display=":0", resolution="640x480")


def _auth(ws, token="right-token"):
    ws.send_json({"type": "auth", "token": token})
    return ws.receive_json()


@pytest.mark.parametrize("first", [{"type": "pointer"}, {"type": "auth", "token": "wrong"}, {"type": "auth"}, "not json"])
def test_control_ws_rejects_missing_or_wrong_token(client, first):
    _add_fake_session()
    with pytest.raises(WebSocketDisconnect) as e:
        with client.websocket_connect("/api/ws/control/s1") as ws:
            ws.send_json(first) if isinstance(first, dict) else ws.send_text(first)
            ws.receive_text()
    assert e.value.code == 4403


def test_control_ws_rejects_token_of_other_session(client):
    main.control_tokens["s2"] = "other-token"
    _add_fake_session()
    with pytest.raises(WebSocketDisconnect) as e:
        with client.websocket_connect("/api/ws/control/s1") as ws:
            _auth(ws, "other-token")
    assert e.value.code == 4403


def test_control_ws_accepts_right_token_then_reports_missing_session(client):
    # トークンは正しいがセッション実体が無い → 認証は通り、4004 で閉じる(認証より先に存在を漏らさない)
    with pytest.raises(WebSocketDisconnect) as e:
        with client.websocket_connect("/api/ws/control/s1") as ws:
            _auth(ws)
    assert e.value.code == 4004


def test_control_ws_second_connection_gets_4009_and_first_release_frees_it(client):
    _add_fake_session()
    with client.websocket_connect("/api/ws/control/s1") as ws1:
        assert _auth(ws1) == {"type": "ready"}
        ws1.send_json({"type": "pointer", "action": "move", "x": 0.5, "y": 0.5, "seq": 1})
        assert ws1.receive_json() == {"type": "ack", "seq": 1, "ok": True}
        ws1.send_json({"type": "key", "key": "F13", "seq": 2})
        assert ws1.receive_json()["type"] == "error"
        with pytest.raises(WebSocketDisconnect) as e:
            with client.websocket_connect("/api/ws/control/s1") as ws2:
                _auth(ws2)
        assert e.value.code == 4009
    with client.websocket_connect("/api/ws/control/s1") as ws3:
        assert _auth(ws3) == {"type": "ready"}


def test_storage_state_requires_bearer_token(client):
    assert client.post("/api/sessions/s1/storage-state").status_code == 403
    assert client.post("/api/sessions/s1/storage-state?token=right-token").status_code == 403  # クエリでは受けない
    assert client.post("/api/sessions/s1/storage-state", headers={"Authorization": "Bearer wrong"}).status_code == 403
    r = client.post("/api/sessions/s1/storage-state", headers={"Authorization": "Bearer right-token"})
    assert r.status_code == 404  # 認証は通る(ブラウザの無いセッションなので 404)


def test_storage_state_is_written_owner_only_and_summary_hides_values(client):
    state = {
        "cookies": [{"name": "sid", "value": "SECRET", "domain": "example.test", "httpOnly": True}],
        "origins": [{"origin": "https://example.test", "localStorage": [{"name": "k", "value": "SECRET"}]}],
    }

    async def fake_export(session_id, path=None):
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        os.write(fd, b"{}")
        os.close(fd)
        return state

    with patch.object(main.session_manager, "export_storage_state", new=AsyncMock(side_effect=fake_export)):
        for _ in range(2):  # 2 回目は既存ファイルを消して作り直す
            r = client.post("/api/sessions/s1/storage-state", headers={"Authorization": "Bearer right-token"})
            assert r.status_code == 200
    body = r.json()
    assert "SECRET" not in r.text
    assert body["cookies"] == [{"name": "sid", "domain": "example.test", "httpOnly": True}]
    assert stat.S_IMODE(os.stat(main.STORAGE_STATE_DIR).st_mode) == 0o700
    assert stat.S_IMODE(os.stat(body["path"]).st_mode) == 0o600


def test_private_dir_rejects_symlink(tmp_path):
    target = tmp_path / "real"
    target.mkdir()
    link = tmp_path / "link"
    link.symlink_to(target)
    with pytest.raises(PermissionError):
        main._private_dir(str(link))
