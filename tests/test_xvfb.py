"""XvfbManager と FocusRelay の統合部分のテスト.

Xvfb / Fluxbox / FocusRelay の生成はすべてモックし、実プロセス・実 X 接続
なしで `allocate()`/`release()` の配線とプロセスリーク耐性を検証する。
(Issue: https://github.com/aRaikoFunakami/web-screen-stream/issues/2)
"""

import asyncio
import os
from pathlib import Path
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
def manager(monkeypatch, tmp_path):
    """実プロセス操作を行わず、ロック/ソケット/proc は tmp_path に向けた XvfbManager."""
    monkeypatch.setattr(xvfb_module, "check_display", lambda display=None: True)
    monkeypatch.setattr(xvfb_module.os, "makedirs", lambda *a, **k: None)
    # 万一 fake pid が実プロセスと衝突しても実シグナルを送らないようにする
    monkeypatch.setattr(xvfb_module.os, "killpg", MagicMock())

    mgr = XvfbManager(base_display=100, max_displays=2)
    mgr._POLL_INTERVAL = 0
    mgr._FLUXBOX_WAIT = 0
    mgr._LOCK_DIR = str(tmp_path / "lock")
    mgr._SOCKET_DIR = str(tmp_path / "sock")
    mgr._PROC_DIR = str(tmp_path / "proc")
    for d in (mgr._LOCK_DIR, mgr._SOCKET_DIR, mgr._PROC_DIR):
        Path(d).mkdir(parents=True)
    return mgr


def put_lock(mgr: XvfbManager, display_num: int, pid: int, comm: str | None = "Xvfb"):
    """ロックを置く. comm を渡すと /proc/<pid>/comm も置く（None なら存在しないプロセス）."""
    with open(f"{mgr._LOCK_DIR}/.X{display_num}-lock", "w") as f:
        f.write(f"{pid:>10}\n")
    open(f"{mgr._SOCKET_DIR}/X{display_num}", "w").close()
    if comm is not None:
        Path(f"{mgr._PROC_DIR}/{pid}").mkdir(parents=True, exist_ok=True)
        with open(f"{mgr._PROC_DIR}/{pid}/comm", "w") as f:
            f.write(comm + "\n")


def fluxbox_open(mgr: XvfbManager):
    """Fluxbox 設定の書き込み(/root)だけを握りつぶし、ロック等の実ファイルは通す open."""
    real_open = open
    fake = mock_open()

    def _open(path, *args, **kwargs):
        if str(path).startswith(str(mgr._LOCK_DIR)) or str(path).startswith(
            str(mgr._PROC_DIR)
        ):
            return real_open(path, *args, **kwargs)
        return fake(path, *args, **kwargs)

    return patch("builtins.open", _open)


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
        # start() が部分的に成功していた場合の X11 接続 fd リーク防止
        # （コードレビューで確認済みの修正）: 失敗時も必ず stop() で
        # 後始末してから参照を破棄すること。
        relay_instance.stop.assert_awaited_once()

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

    @pytest.mark.asyncio
    async def test_fluxbox_start_failure_logs_traceback(
        self, manager, mock_focus_relay, caplog
    ):
        """logger.exception を使い、失敗原因のトレースバックが
        ログに残ること（コードレビューで確認済みの修正）。"""
        xvfb_proc = make_fake_proc(90012)
        create_mock = AsyncMock(
            side_effect=[xvfb_proc, FileNotFoundError("fluxbox: not found")]
        )

        with patch.object(asyncio, "create_subprocess_exec", create_mock), patch(
            "builtins.open", mock_open()
        ), caplog.at_level("ERROR"):
            with pytest.raises(FileNotFoundError):
                await manager.allocate(1280, 720)

        records = [r for r in caplog.records if "cleaning up" in r.message]
        assert records, "expected an error log for the cleanup path"
        assert records[0].exc_info is not None, (
            "logger.exception (not logger.error) should be used so the "
            "traceback is preserved"
        )


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




class TestAllocateWhenDisplayHeldByAnotherXvfb:
    """同じ display に別の Xvfb が居る場合 (Issue #6)."""

    @pytest.mark.asyncio
    async def test_early_exit_of_own_xvfb_is_a_failure(self, manager, mock_focus_relay):
        """自分の Xvfb が即死したら、check_display が相手に繋がって成功しても失敗にする."""
        dead_xvfb = make_fake_proc(90010)
        dead_xvfb.returncode = 1  # "Server already active"
        create_mock = AsyncMock(side_effect=[dead_xvfb])

        with patch.object(asyncio, "create_subprocess_exec", create_mock):
            with pytest.raises(RuntimeError, match="exited early on :100"):
                await manager.allocate(1280, 720)

        assert manager._displays == {}
        assert create_mock.await_count == 1  # Fluxbox まで進まない
        xvfb_module.os.killpg.assert_not_called()  # 死んだ pid へ kill を送らない

    @pytest.mark.asyncio
    async def test_foreign_lock_holder_after_start_is_a_failure(
        self, manager, mock_focus_relay
    ):
        """選んだ後に他者が番号を取った場合、ロックの持ち主が自分でなければ失敗にし自分を止める."""
        xvfb_proc = make_fake_proc(90011)  # returncode=None（終了をまだ観測していない）

        async def spawn(*args, **kwargs):
            put_lock(manager, 100, 777)  # 起動の合間に他の Xvfb が :100 を握った
            return xvfb_proc

        with patch.object(asyncio, "create_subprocess_exec", AsyncMock(side_effect=spawn)):
            with pytest.raises(RuntimeError, match="held by another process"):
                await manager.allocate(1280, 720)

        assert manager._displays == {}
        xvfb_module.os.killpg.assert_called_once()  # 自分の Xvfb は止める
        assert os.path.exists(f"{manager._LOCK_DIR}/.X100-lock")  # 他者のロックは消さない

    @pytest.mark.asyncio
    async def test_held_display_is_skipped_and_next_one_is_used(
        self, manager, mock_focus_relay
    ):
        """孤児が握る :100 は飛ばし :101 で確保する（以降の確保が永久に失敗しない）."""
        put_lock(manager, 100, 777)
        create_mock = AsyncMock(side_effect=[make_fake_proc(90012), make_fake_proc(90112)])

        with patch.object(asyncio, "create_subprocess_exec", create_mock), fluxbox_open(
            manager
        ):
            display = await manager.allocate(1280, 720)

        assert display == ":101"
        assert list(manager._displays) == [":101"]
        assert os.path.exists(f"{manager._LOCK_DIR}/.X100-lock")  # 孤児のロックは残す

    @pytest.mark.asyncio
    async def test_stale_lock_with_reused_pid_is_cleaned_and_display_used(
        self, manager, mock_focus_relay
    ):
        """ロックの PID が別プロセス(Xvfb でない)に再利用されていれば stale として消し、:100 を使う."""
        put_lock(manager, 100, 777, comm="python")
        create_mock = AsyncMock(side_effect=[make_fake_proc(90013), make_fake_proc(90113)])

        with patch.object(asyncio, "create_subprocess_exec", create_mock), fluxbox_open(
            manager
        ):
            display = await manager.allocate(1280, 720)

        assert display == ":100"
        assert not os.path.exists(f"{manager._SOCKET_DIR}/X100")

    @pytest.mark.asyncio
    async def test_all_candidates_held_raises_capacity_error_without_launching(
        self, manager
    ):
        """全候補が握られていたら、起動せず容量不足と同じ文言("Maximum displays reached")で失敗する."""
        put_lock(manager, 100, 777)
        put_lock(manager, 101, 778)
        create_mock = AsyncMock()

        with patch.object(asyncio, "create_subprocess_exec", create_mock):
            with pytest.raises(RuntimeError, match="Maximum displays reached") as exc:
                await manager.allocate(1280, 720)

        assert ":100" in str(exc.value) and ":101" in str(exc.value)
        create_mock.assert_not_awaited()

    def test_available_count_excludes_held_displays(self, manager):
        assert manager.available_count == 2
        put_lock(manager, 100, 777)
        assert manager.available_count == 1

    def test_held_warning_is_logged_once_per_display(self, manager, caplog):
        put_lock(manager, 100, 777)
        with caplog.at_level("WARNING", logger=xvfb_module.logger.name):
            manager._next_display_num()
            manager._next_display_num()
        held = [r for r in caplog.records if "held by another Xvfb" in r.getMessage()]
        assert len(held) == 1


class TestLockHelpers:
    def test_read_lock_pid_parses_padded_pid(self, manager):
        put_lock(manager, 100, 27)
        assert manager._read_lock_pid(100) == 27

    def test_read_lock_pid_returns_none_for_missing_or_garbage(self, manager):
        assert manager._read_lock_pid(100) is None
        with open(f"{manager._LOCK_DIR}/.X100-lock", "w") as f:
            f.write("not-a-pid")
        assert manager._read_lock_pid(100) is None

    def test_garbage_lock_is_cleaned_as_stale(self, manager):
        with open(f"{manager._LOCK_DIR}/.X100-lock", "w") as f:
            f.write("not-a-pid")
        manager._cleanup_stale_lock(100)
        assert not os.path.exists(f"{manager._LOCK_DIR}/.X100-lock")

    def test_is_xvfb_alive(self, manager):
        put_lock(manager, 100, 5, comm="Xvfb")
        put_lock(manager, 101, 6, comm="python")
        put_lock(manager, 102, 7, comm=None)  # 存在しないプロセス
        assert manager._is_xvfb_alive(5) is True
        assert manager._is_xvfb_alive(6) is False
        assert manager._is_xvfb_alive(7) is False


@pytest.mark.asyncio
async def test_startup_timeout_kills_the_still_running_xvfb(manager, monkeypatch):
    """起動確認がタイムアウトしたら、まだ動いている Xvfb を止めて失敗にする."""
    monkeypatch.setattr(xvfb_module, "check_display", lambda display=None: False)
    xvfb_proc = make_fake_proc(90014)  # returncode=None のまま
    create_mock = AsyncMock(side_effect=[xvfb_proc])

    with patch.object(asyncio, "create_subprocess_exec", create_mock):
        with pytest.raises(RuntimeError, match="Xvfb failed to start on :100"):
            await manager.allocate(1280, 720)

    assert manager._displays == {}
    xvfb_module.os.killpg.assert_called_once()
