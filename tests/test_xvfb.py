"""XvfbManager と FocusRelay の統合部分のテスト.

Xvfb / Fluxbox / FocusRelay の生成はすべてモックし、実プロセス・実 X 接続
なしで `allocate()`/`release()` の配線とプロセスリーク耐性を検証する。
(Issue: https://github.com/aRaikoFunakami/web-screen-stream/issues/2)
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock, mock_open, patch

import pytest

from web_screen_stream import xvfb as xvfb_module
from web_screen_stream.xvfb import XvfbManager


def make_fake_proc(pid: int) -> MagicMock:
    proc = MagicMock()
    proc.pid = pid
    proc.returncode = None
    proc.wait = AsyncMock(return_value=0)
    return proc


@pytest.fixture
def manager(monkeypatch):
    """実ファイル I/O・実プロセス操作を行わない XvfbManager."""
    monkeypatch.setattr(xvfb_module, "check_display", lambda display=None: True)
    monkeypatch.setattr(xvfb_module.os, "makedirs", lambda *a, **k: None)
    # 万一 fake pid が実プロセスと衝突しても実シグナルを送らないようにする
    monkeypatch.setattr(xvfb_module.os, "killpg", MagicMock())

    mgr = XvfbManager(base_display=100, max_displays=2)
    mgr._POLL_INTERVAL = 0
    mgr._FLUXBOX_WAIT = 0
    return mgr


@pytest.fixture
def mock_focus_relay(monkeypatch):
    instance = MagicMock()
    instance.start = AsyncMock()
    instance.stop = AsyncMock()
    cls = MagicMock(return_value=instance)
    monkeypatch.setattr(xvfb_module, "FocusRelay", cls)
    return cls, instance


class TestAllocateFocusRelayWiring:
    @pytest.mark.asyncio
    async def test_allocate_starts_focus_relay_for_the_allocated_display(
        self, manager, mock_focus_relay
    ):
        relay_cls, relay_instance = mock_focus_relay
        xvfb_proc = make_fake_proc(90001)
        fluxbox_proc = make_fake_proc(90002)
        create_mock = AsyncMock(side_effect=[xvfb_proc, fluxbox_proc])

        with patch.object(asyncio, "create_subprocess_exec", create_mock), patch(
            "builtins.open", mock_open()
        ):
            display = await manager.allocate(1280, 720)

        assert display == ":100"
        relay_cls.assert_called_once_with(":100")
        relay_instance.start.assert_awaited_once()

        info = manager._displays[display]
        assert info.focus_relay is relay_instance

    @pytest.mark.asyncio
    async def test_relay_start_failure_is_non_fatal(self, manager, mock_focus_relay):
        """relay 起動失敗は allocate() 全体を失敗させない（単一ウィンドウ回帰なし）."""
        relay_cls, relay_instance = mock_focus_relay
        relay_instance.start.side_effect = RuntimeError("X connection refused")
        xvfb_proc = make_fake_proc(90003)
        fluxbox_proc = make_fake_proc(90004)
        create_mock = AsyncMock(side_effect=[xvfb_proc, fluxbox_proc])

        with patch.object(asyncio, "create_subprocess_exec", create_mock), patch(
            "builtins.open", mock_open()
        ):
            display = await manager.allocate(1280, 720)

        info = manager._displays[display]
        assert info.focus_relay is None
        # Xvfb/Fluxbox 自体は通常通り起動している
        assert info.xvfb_proc is xvfb_proc
        assert info.fluxbox_proc is fluxbox_proc

    @pytest.mark.asyncio
    async def test_fluxbox_start_failure_cleans_up_xvfb(
        self, manager, mock_focus_relay
    ):
        """Fluxbox 起動失敗時、既に起動済みの Xvfb がリークしないこと."""
        relay_cls, _relay_instance = mock_focus_relay
        xvfb_proc = make_fake_proc(90005)
        create_mock = AsyncMock(
            side_effect=[xvfb_proc, FileNotFoundError("fluxbox: not found")]
        )

        with patch.object(asyncio, "create_subprocess_exec", create_mock), patch(
            "builtins.open", mock_open()
        ):
            with pytest.raises(FileNotFoundError):
                await manager.allocate(1280, 720)

        # allocate() が例外送出前に Xvfb を kill していること
        xvfb_proc.wait.assert_awaited()
        xvfb_module.os.killpg.assert_any_call(90005, xvfb_module.signal.SIGKILL)
        # 管理テーブルに残っていないこと（release() できない孤児にしない）
        assert manager._displays == {}
        # relay は Fluxbox 失敗より後段のため、生成すらされない
        relay_cls.assert_not_called()


class TestReleaseFocusRelayWiring:
    @pytest.mark.asyncio
    async def test_release_stops_focus_relay_before_fluxbox(
        self, manager, mock_focus_relay
    ):
        relay_cls, relay_instance = mock_focus_relay
        xvfb_proc = make_fake_proc(90006)
        fluxbox_proc = make_fake_proc(90007)
        create_mock = AsyncMock(side_effect=[xvfb_proc, fluxbox_proc])

        with patch.object(asyncio, "create_subprocess_exec", create_mock), patch(
            "builtins.open", mock_open()
        ):
            display = await manager.allocate(1280, 720)

        await manager.release(display)

        relay_instance.stop.assert_awaited_once()
        assert display not in manager._displays

    @pytest.mark.asyncio
    async def test_release_without_relay_does_not_crash(self, manager):
        """relay 起動失敗済みのディスプレイでも release() が正常終了すること."""
        xvfb_proc = make_fake_proc(90008)
        info = xvfb_module.DisplayInfo(
            display=":100",
            xvfb_proc=xvfb_proc,
            fluxbox_proc=make_fake_proc(90009),
            width=1280,
            height=720,
            focus_relay=None,
        )
        manager._displays[":100"] = info

        await manager.release(":100")  # 例外を送出しないこと

        assert ":100" not in manager._displays

    @pytest.mark.asyncio
    async def test_relay_stop_failure_does_not_block_fluxbox_xvfb_stop(
        self, manager, mock_focus_relay
    ):
        """relay.stop() が失敗しても Fluxbox/Xvfb の停止は続行される."""
        relay_cls, relay_instance = mock_focus_relay
        relay_instance.stop.side_effect = RuntimeError("boom")
        xvfb_proc = make_fake_proc(90010)
        fluxbox_proc = make_fake_proc(90011)
        create_mock = AsyncMock(side_effect=[xvfb_proc, fluxbox_proc])

        with patch.object(asyncio, "create_subprocess_exec", create_mock), patch(
            "builtins.open", mock_open()
        ):
            display = await manager.allocate(1280, 720)

        await manager.release(display)  # 例外を送出しないこと

        fluxbox_proc.wait.assert_awaited()
        xvfb_proc.wait.assert_awaited()
