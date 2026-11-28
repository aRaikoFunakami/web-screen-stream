"""起動の失敗・取り消し時の片付け（#13）のテスト.

片付けの段が返らなくても期限内に戻ること、取り消しが握りつぶされないこと、
起動の各段（launch / new_page / goto / FFmpeg 起動）で失敗・取り消しても
ドライバ・Chromium・FFmpeg・display が残らないことを確認する。
実際の Playwright・FFmpeg・Xvfb は使わず、差し替えて資源の解放を記録する。
"""

from __future__ import annotations

import asyncio

import pytest

import playwright.async_api as pa

import web_screen_stream.session as sess
from web_screen_stream.config import StreamConfig
from web_screen_stream.session import BrowserStreamSession, SessionManager


@pytest.fixture(autouse=True)
def _fast_deadlines(monkeypatch):
    """期限を縮めて、期限切れのテストを速くする."""
    monkeypatch.setattr(sess, "_TIMEOUT_SCALE", 0.01)


class Log(list):
    pass


class FakeXvfb:
    def __init__(self, log: Log, hang_release: bool = False):
        self.log, self.hang_release = log, hang_release

    async def allocate(self, w, h):
        return ":101"

    async def release(self, display):
        self.log.append("display.release")


def fake_playwright(monkeypatch, log: Log, *, fail_at: str | None = None, hang_at: str | None = None):
    """fail_at / hang_at: launch | new_page | goto。解放は log に記録する."""

    async def step(name):
        if name == fail_at:
            raise RuntimeError(f"{name} failed")
        if name == hang_at:
            await asyncio.Event().wait()

    class _Page:
        async def goto(self, *a, **k):
            await step("goto")

        async def set_viewport_size(self, size):
            return None

    class _Context:
        pages: list = []

        async def new_page(self):
            await step("new_page")
            return _Page()

    class _Browser:
        contexts = [_Context()]

        async def new_page(self, **k):
            await step("new_page")
            return _Page()

        async def close(self):
            log.append("browser.close")

    class _Chromium:
        async def launch(self, **kw):
            await step("launch")
            return _Browser()

    class _PW:
        chromium = _Chromium()

        async def stop(self):
            log.append("pw.stop")

    class _Starter:
        async def start(self):
            return _PW()

    async def launch_over_cdp(pw, config, tag, proxy):  # 普通に起動して CDP で接続する経路（proxy 無し）
        await step("launch")
        return _Browser()

    monkeypatch.setattr(pa, "async_playwright", lambda: _Starter())
    monkeypatch.setattr(sess, "_launch_over_cdp", launch_over_cdp)


def fake_ffmpeg(monkeypatch, log: Log, *, fail=False, hang=False):
    async def start(self):
        self._status = "starting"
        if fail:
            raise RuntimeError("ffmpeg failed")
        if hang:
            await asyncio.Event().wait()

    async def stop(self):
        log.append("ffmpeg.stop")

    monkeypatch.setattr("web_screen_stream.ffmpeg_source.FFmpegSource.start", start)
    monkeypatch.setattr("web_screen_stream.ffmpeg_source.FFmpegSource.stop", stop)


ALL_RELEASED = {"browser.close", "pw.stop", "ffmpeg.stop", "display.release"}


# ---------- _cleanup ----------


async def test_hung_step_returns_within_deadline_and_next_steps_run():
    ran = []

    async def hang():
        await asyncio.Event().wait()

    async def after():
        ran.append("after")

    await asyncio.wait_for(sess._cleanup([("hang", hang, 1.0), ("after", after, 1.0)]), timeout=2)
    assert ran == ["after"]


async def test_step_failure_is_logged_and_next_step_runs():
    ran = []

    async def boom():
        raise PermissionError("nope")

    async def after():
        ran.append("after")

    await sess._cleanup([("boom", boom, 1.0), ("after", after, 1.0)])
    assert ran == ["after"]


async def test_cancel_during_cleanup_is_delivered_after_all_steps_finish():
    ran = []
    gate = asyncio.Event()

    async def slow():
        await gate.wait()
        ran.append("slow")

    async def after():
        ran.append("after")

    task = asyncio.ensure_future(sess._cleanup([("slow", slow, 5.0), ("after", after, 5.0)]))
    await asyncio.sleep(0.01)
    task.cancel()
    await asyncio.sleep(0.01)
    task.cancel()  # 2 回取り消されても片付けは止まらない
    gate.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert ran == ["slow", "after"]


# ---------- create ----------


@pytest.mark.parametrize("point", ["launch", "new_page", "goto"])
async def test_create_failure_releases_everything(monkeypatch, point):
    log = Log()
    fake_playwright(monkeypatch, log, fail_at=point)
    fake_ffmpeg(monkeypatch, log)
    mgr = SessionManager(xvfb_manager=FakeXvfb(log))
    with pytest.raises(RuntimeError):
        await mgr.create("s", StreamConfig(), url="about:blank")
    released = set(log)
    assert "display.release" in released
    if point != "launch":
        assert {"browser.close", "pw.stop"} <= released
    else:
        assert "pw.stop" in released  # ドライバだけは起動済み
    assert mgr.get("s") is None and not mgr._browsers


@pytest.mark.parametrize("point", ["launch", "new_page", "goto"])
async def test_create_cancel_releases_everything_and_cancel_propagates(monkeypatch, point):
    log = Log()
    fake_playwright(monkeypatch, log, hang_at=point)
    fake_ffmpeg(monkeypatch, log)
    mgr = SessionManager(xvfb_manager=FakeXvfb(log))
    task = asyncio.ensure_future(mgr.create("s", StreamConfig(), url="about:blank"))
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert "pw.stop" in log and "display.release" in log
    if point != "launch":
        assert "browser.close" in log


async def test_goto_failure_cleans_up_once(monkeypatch):
    log = Log()
    fake_playwright(monkeypatch, log, fail_at="goto")
    fake_ffmpeg(monkeypatch, log)
    with pytest.raises(RuntimeError, match="開けませんでした"):
        await SessionManager(xvfb_manager=FakeXvfb(log)).create("s", StreamConfig(), url="about:blank")
    assert log.count("browser.close") == 1 and log.count("pw.stop") == 1


@pytest.mark.parametrize("mode", ["fail", "hang"])
async def test_create_ffmpeg_start_failure_or_cancel_releases_everything(monkeypatch, mode):
    log = Log()
    fake_playwright(monkeypatch, log)
    fake_ffmpeg(monkeypatch, log, fail=mode == "fail", hang=mode == "hang")
    mgr = SessionManager(xvfb_manager=FakeXvfb(log))
    task = asyncio.ensure_future(mgr.create("s", StreamConfig(), url="about:blank"))
    await asyncio.sleep(0.05)
    if mode == "hang":
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    else:
        with pytest.raises(RuntimeError):
            await task
    assert ALL_RELEASED <= set(log)
    assert mgr.get("s") is None


async def test_cancel_during_cleanup_after_exception_still_cancels(monkeypatch):
    """例外から始まった片付けの途中で取り消されても、CancelledError が届き、後続の段も完走する."""
    import types

    log = Log()
    gate = asyncio.Event()
    fake_playwright(monkeypatch, log)
    fake_ffmpeg(monkeypatch, log, fail=True)
    monkeypatch.setattr(sess, "_TIMEOUT_SCALE", 1.0)

    async def slow_close(self):  # browser.close を遅くして、片付けの途中で取り消す
        await gate.wait()
        log.append("browser.close")

    real_launch = SessionManager._launch_browser

    async def launch(self, sid, cfg, url):
        pw, browser, page, pids = await real_launch(self, sid, cfg, url)
        browser.close = types.MethodType(slow_close, browser)
        return pw, browser, page, pids

    monkeypatch.setattr(SessionManager, "_launch_browser", launch)
    mgr = SessionManager(xvfb_manager=FakeXvfb(log))
    task = asyncio.ensure_future(mgr.create("s", StreamConfig(), url="about:blank"))
    await asyncio.sleep(0.1)  # FFmpeg 失敗 → 片付け中（browser.close 待ち）
    task.cancel()
    gate.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert ALL_RELEASED <= set(log)


# ---------- stop / stop_all ----------


async def _running_manager(monkeypatch, log, sid="s"):
    fake_playwright(monkeypatch, log)

    async def start(self):
        self._status = "streaming"

    async def stop(self):
        log.append("ffmpeg.stop")

    monkeypatch.setattr(BrowserStreamSession, "start", start)
    monkeypatch.setattr(BrowserStreamSession, "stop", stop)
    mgr = SessionManager(xvfb_manager=FakeXvfb(log))
    await mgr.create(sid, StreamConfig(), url="about:blank")
    return mgr


async def test_stop_cancel_still_releases_remaining_steps(monkeypatch):
    log = Log()
    mgr = await _running_manager(monkeypatch, log)
    gate = asyncio.Event()
    monkeypatch.setattr(sess, "_TIMEOUT_SCALE", 1.0)

    async def slow_stop(self):
        await gate.wait()
        log.append("ffmpeg.stop")

    monkeypatch.setattr(BrowserStreamSession, "stop", slow_stop)
    task = asyncio.ensure_future(mgr.stop("s"))
    await asyncio.sleep(0.05)
    task.cancel()
    gate.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert ALL_RELEASED <= set(log)


async def test_stop_all_cancel_stops_every_session(monkeypatch):
    log = Log()
    mgr = await _running_manager(monkeypatch, log, "a")
    mgr._xvfb = FakeXvfb(log)
    await mgr.create("b", StreamConfig(), url="about:blank")
    gate = asyncio.Event()
    monkeypatch.setattr(sess, "_TIMEOUT_SCALE", 1.0)

    async def slow_stop(self):
        await gate.wait()
        log.append("ffmpeg.stop")

    monkeypatch.setattr(BrowserStreamSession, "stop", slow_stop)
    task = asyncio.ensure_future(mgr.stop_all())
    await asyncio.sleep(0.05)
    task.cancel()
    gate.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert mgr.get("a") is None and mgr.get("b") is None
    assert log.count("display.release") == 2


# ---------- reap ----------


async def test_reap_permission_error_does_not_propagate(monkeypatch):
    def fake_kill(pid, sig):
        if sig == 0:
            return
        raise PermissionError

    async def fake_sleep(_):
        return None

    monkeypatch.setattr(sess.os, "kill", fake_kill)
    monkeypatch.setattr(sess.os, "waitpid", lambda pid, o: (pid, 0))
    monkeypatch.setattr(sess.asyncio, "sleep", fake_sleep)
    await sess._reap_chrome_pids({1234})
