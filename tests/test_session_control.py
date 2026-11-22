"""SessionManager.open_control の排他と、export_storage_state の中身・書き出し権限のテスト."""

import json
import os
import stat
from types import SimpleNamespace

import pytest

from web_screen_stream.session import ControlBusyError, SessionManager


class FakeInjector:
    def __init__(self, *a):
        self.closed = False

    def close(self):
        self.closed = True


def test_second_open_control_is_rejected_until_first_closes(monkeypatch):
    monkeypatch.setattr("web_screen_stream.session.XTestInjector", FakeInjector)
    mgr = SessionManager()
    mgr._sessions["s1"] = SimpleNamespace(display=":0", resolution="640x480")
    first = mgr.open_control("s1")
    with pytest.raises(ControlBusyError):
        mgr.open_control("s1")
    first.close()
    first.close()  # 二重 close でも壊れない
    mgr.open_control("s1").close()
    with pytest.raises(KeyError):
        mgr.open_control("missing")


def test_open_control_failure_does_not_leave_session_locked(monkeypatch):
    def broken(*a):
        raise RuntimeError("no X")

    monkeypatch.setattr("web_screen_stream.session.XTestInjector", broken)
    mgr = SessionManager()
    mgr._sessions["s1"] = SimpleNamespace(display=":0", resolution="640x480")
    with pytest.raises(RuntimeError):
        mgr.open_control("s1")
    assert "s1" not in mgr._controllers


async def test_export_storage_state_contains_httponly_cookie_and_local_storage(tmp_path):
    pw_api = pytest.importorskip("playwright.async_api")
    async with pw_api.async_playwright() as pw:
        try:
            browser = await pw.chromium.launch()
        except Exception as e:  # ブラウザ未インストールの環境
            pytest.skip(f"chromium not available: {e}")
        page = await browser.new_page()

        async def serve(route):
            await route.fulfill(
                body="<script>localStorage.setItem('token','LS-VALUE')</script>",
                content_type="text/html",
                headers={"Set-Cookie": "sid=COOKIE-VALUE; HttpOnly; Path=/; Secure"},
            )

        await page.route("https://login.example.test/**", serve)
        await page.goto("https://login.example.test/")
        mgr = SessionManager()
        mgr._browsers["s1"] = (pw, browser, page)

        path = tmp_path / "state.json"
        state = await mgr.export_storage_state("s1", str(path))
        with pytest.raises(FileExistsError):  # 排他作成: 既存ファイルを上書きしない
            await mgr.export_storage_state("s1", str(path))
        link = tmp_path / "link.json"
        link.symlink_to(tmp_path / "elsewhere.json")
        with pytest.raises(FileExistsError):  # symlink を辿らない
            await mgr.export_storage_state("s1", str(link))
        await browser.close()

    cookie = next(c for c in state["cookies"] if c["name"] == "sid")
    assert cookie["httpOnly"] is True and cookie["value"] == "COOKIE-VALUE"
    origin = next(o for o in state["origins"] if o["origin"] == "https://login.example.test")
    assert {"name": "token", "value": "LS-VALUE"} in origin["localStorage"]
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    assert json.loads(path.read_text()) == state
    assert not (tmp_path / "elsewhere.json").exists()

    with pytest.raises(KeyError):
        await SessionManager().export_storage_state("none")
