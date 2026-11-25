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


async def test_stop_closes_open_channel_and_stale_close_does_not_free_new_slot(monkeypatch):
    monkeypatch.setattr("web_screen_stream.session.XTestInjector", FakeInjector)
    mgr = SessionManager()

    class FakeSession:
        display, resolution = ":0", "640x480"

        async def stop(self):
            pass

    mgr._sessions["s1"] = FakeSession()
    old = mgr.open_control("s1")
    await mgr.stop("s1")
    assert old.closed and old._inj.closed  # 停止で古い操作チャネルも閉じる
    assert (await old.handle({"type": "key", "key": "Enter"}))["type"] == "error"

    mgr._sessions["s1"] = FakeSession()  # 同じ id で作り直す
    new = mgr.open_control("s1")
    old.close()  # 古いチャネルの後始末が新しい枠を外さない
    with pytest.raises(ControlBusyError):
        mgr.open_control("s1")
    new.close()
    mgr.open_control("s1").close()


async def test_stop_still_cleans_up_when_channel_close_fails(monkeypatch):
    class BrokenInjector(FakeInjector):
        def close(self):
            raise OSError("X connection lost")

    monkeypatch.setattr("web_screen_stream.session.XTestInjector", BrokenInjector)
    stopped = []

    class FakeSession:
        display, resolution = ":0", "640x480"

        async def stop(self):
            stopped.append(True)

    mgr = SessionManager()
    mgr._sessions["s1"] = FakeSession()
    mgr.open_control("s1")
    await mgr.stop("s1")
    assert stopped == [True]


def test_slot_is_reserved_while_connecting_and_stop_during_connect_wins(monkeypatch):
    mgr = SessionManager()
    mgr._sessions["s1"] = SimpleNamespace(display=":0", resolution="640x480")
    seen = []

    class SlowInjector(FakeInjector):
        def __init__(self, *a):
            super().__init__()
            with pytest.raises(ControlBusyError):  # X へ接続中の 2 つ目は通さない
                mgr.open_control("s1")
            mgr._controllers.pop("s1")  # 接続中に stop された
            seen.append(self)

    monkeypatch.setattr("web_screen_stream.session.XTestInjector", SlowInjector)
    with pytest.raises(KeyError):
        mgr.open_control("s1")
    assert seen[0].closed and "s1" not in mgr._controllers


def test_sessions_sharing_a_display_cannot_be_controlled_at_once(monkeypatch):
    monkeypatch.setattr("web_screen_stream.session.XTestInjector", FakeInjector)
    mgr = SessionManager()
    mgr._sessions["a"] = SimpleNamespace(display=":99", resolution="640x480")
    mgr._sessions["b"] = SimpleNamespace(display=":99", resolution="640x480")  # 静的 Xvfb
    first = mgr.open_control("a")
    with pytest.raises(ControlBusyError):
        mgr.open_control("b")
    first.close()
    mgr.open_control("b").close()


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
