"""Xvfb ディスプレイ管理ユーティリティ.

既存の静的ユーティリティ (get_display, check_display) に加え、
XvfbManager でセッションごとに独立した Xvfb + Fluxbox を動的管理する。
"""

import asyncio
import logging
import os
import signal
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


# ============================================================
# 静的ユーティリティ（既存互換）
# ============================================================


def get_display() -> str:
    """現在の DISPLAY 環境変数を取得する.

    Returns:
        DISPLAY 文字列 (例: ":99")

    Raises:
        RuntimeError: DISPLAY が設定されていない場合
    """
    display = os.environ.get("DISPLAY")
    if not display:
        raise RuntimeError(
            "DISPLAY environment variable is not set. "
            "Ensure Xvfb is running (see entrypoint.sh)."
        )
    return display


def check_display(display: str | None = None) -> bool:
    """X11 ディスプレイが利用可能か確認する.

    Args:
        display: チェックするディスプレイ (None の場合は環境変数から取得)

    Returns:
        ディスプレイが利用可能なら True
    """
    import subprocess

    display = display or os.environ.get("DISPLAY", ":99")
    try:
        result = subprocess.run(
            ["xdpyinfo", "-display", display],
            capture_output=True,
            timeout=5,
        )
        return result.returncode == 0
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return False


# ============================================================
# XvfbManager: セッションごとの動的 Xvfb + Fluxbox 管理
# ============================================================


@dataclass
class DisplayInfo:
    """割り当て済みディスプレイの管理情報."""

    display: str  # ":100"
    xvfb_proc: asyncio.subprocess.Process
    fluxbox_proc: asyncio.subprocess.Process
    width: int
    height: int


class XvfbManager:
    """セッションごとに独立した Xvfb + Fluxbox を管理する.

    ディスプレイ番号を動的に割り当て、プロセスの起動・停止を行う。
    コンテナ内で使用する前提（Xvfb は root 権限不要）。

    Usage:
        mgr = XvfbManager(max_displays=5)
        display = await mgr.allocate(1280, 720)  # ":100"
        ...
        await mgr.release(display)
    """

    # Xvfb 起動ポーリング: 0.2s 間隔 × 15 回 = 最大 3s
    _POLL_INTERVAL = 0.2
    _POLL_MAX_ATTEMPTS = 15

    # Fluxbox 起動待ち
    _FLUXBOX_WAIT = 0.5

    # プロセス停止タイムアウト
    _STOP_TIMEOUT = 3.0

    def __init__(
        self,
        base_display: int = 100,
        max_displays: int = 5,
        screen_depth: int = 24,
    ):
        """XvfbManager を初期化する.

        Args:
            base_display: 割り当て開始番号（:100 から）
            max_displays: 同時最大ディスプレイ数
            screen_depth: X11 色深度
        """
        self._base = base_display
        self._max = max_displays
        self._depth = screen_depth
        self._displays: dict[str, DisplayInfo] = {}
        self._lock = asyncio.Lock()

    async def allocate(self, width: int = 1280, height: int = 720) -> str:
        """新しいディスプレイを割り当て、Xvfb + Fluxbox を起動する.

        Args:
            width: 画面幅 (px)
            height: 画面高さ (px)

        Returns:
            display 文字列 (例: ":100")

        Raises:
            RuntimeError: 最大数に達した場合、または Xvfb 起動失敗
        """
        async with self._lock:
            if len(self._displays) >= self._max:
                raise RuntimeError(
                    f"Maximum displays reached ({self._max}). "
                    f"Stop an existing session first."
                )

            display_num = self._next_display_num()
            display = f":{display_num}"

            # 1. stale ロックファイルの清掃
            self._cleanup_stale_lock(display_num)

            # 2. Xvfb 起動（プロセスグループリーダーとして）
            xvfb_proc = await asyncio.create_subprocess_exec(
                "Xvfb",
                display,
                "-screen",
                "0",
                f"{width}x{height}x{self._depth}",
                "-ac",
                "+extension",
                "GLX",
                "+render",
                "-noreset",
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
                start_new_session=True,
            )

            # 3. Xvfb 起動確認（ポーリング）
            started = False
            for _ in range(self._POLL_MAX_ATTEMPTS):
                await asyncio.sleep(self._POLL_INTERVAL)
                if check_display(display):
                    started = True
                    break

            if not started:
                # タイムアウト → Xvfb 起動失敗、クリーンアップ
                logger.error("Xvfb failed to start on %s", display)
                await self._kill_process(xvfb_proc)
                self._cleanup_stale_lock(display_num)
                raise RuntimeError(f"Xvfb failed to start on {display}")

            logger.info(
                "Xvfb started on %s (%dx%d, PID=%d)",
                display,
                width,
                height,
                xvfb_proc.pid,
            )

            # 4. Fluxbox 起動（Xvfb の display を指定）
            fluxbox_proc = await asyncio.create_subprocess_exec(
                "fluxbox",
                "-display",
                display,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
                env={**os.environ, "DISPLAY": display, "HOME": "/root"},
                start_new_session=True,
            )

            # 5. Fluxbox 起動待ち
            await asyncio.sleep(self._FLUXBOX_WAIT)
            logger.info(
                "Fluxbox started on %s (PID=%d)", display, fluxbox_proc.pid
            )

            # 6. 管理テーブルに登録
            info = DisplayInfo(
                display=display,
                xvfb_proc=xvfb_proc,
                fluxbox_proc=fluxbox_proc,
                width=width,
                height=height,
            )
            self._displays[display] = info
            return display

    async def release(self, display: str) -> None:
        """ディスプレイを解放し、Xvfb + Fluxbox プロセスを停止する.

        Args:
            display: 解放するディスプレイ (例: ":100")
        """
        async with self._lock:
            info = self._displays.pop(display, None)
            if info is None:
                logger.warning("Display %s not found in managed displays", display)
                return

        # ロック外で停止（I/O 待ちが長い可能性）
        # 停止順序: Fluxbox → Xvfb
        logger.info("Releasing display %s", display)

        await self._stop_process(info.fluxbox_proc, "Fluxbox", display)
        await self._stop_process(info.xvfb_proc, "Xvfb", display)

        # SIGKILL 後のロックファイル清掃
        display_num = int(display.lstrip(":"))
        self._cleanup_stale_lock(display_num)

        logger.info("Display %s released", display)

    async def release_all(self) -> None:
        """全ディスプレイを解放する."""
        async with self._lock:
            displays = list(self._displays.keys())

        for display in displays:
            try:
                await self.release(display)
            except Exception:
                logger.exception("Error releasing display %s", display)

    @property
    def active_count(self) -> int:
        """使用中のディスプレイ数."""
        return len(self._displays)

    @property
    def available_count(self) -> int:
        """残り利用可能なディスプレイ数."""
        return self._max - len(self._displays)

    @property
    def max_displays(self) -> int:
        """最大同時ディスプレイ数."""
        return self._max

    def _next_display_num(self) -> int:
        """未使用のディスプレイ番号を返す.

        Returns:
            ディスプレイ番号

        Raises:
            RuntimeError: 空きがない場合
        """
        used = {int(d.lstrip(":")) for d in self._displays}
        for num in range(self._base, self._base + self._max):
            if num not in used:
                return num
        raise RuntimeError("No available display numbers")

    def _cleanup_stale_lock(self, display_num: int) -> None:
        """stale なロックファイルを検出・削除する.

        SIGKILL で Xvfb が終了した場合、ロックファイルが残留する。
        PID を確認し、プロセスが存在しなければ削除する。

        Args:
            display_num: ディスプレイ番号
        """
        lock_file = f"/tmp/.X{display_num}-lock"
        socket_file = f"/tmp/.X11-unix/X{display_num}"

        if not os.path.exists(lock_file):
            return

        try:
            with open(lock_file) as f:
                pid = int(f.read().strip())
            os.kill(pid, 0)  # プロセス存在確認（シグナルは送らない）
            # プロセスが生きている → 実際に使用中
            logger.warning(
                "Display :%d lock exists and PID %d is alive, skipping cleanup",
                display_num,
                pid,
            )
        except (ProcessLookupError, ValueError, PermissionError):
            # プロセスが存在しない or PID 読み取り失敗 → stale
            logger.warning(
                "Cleaning stale lock for display :%d", display_num
            )
            for f in [lock_file, socket_file]:
                try:
                    os.unlink(f)
                except FileNotFoundError:
                    pass

    async def _stop_process(
        self,
        proc: asyncio.subprocess.Process,
        name: str,
        display: str,
    ) -> None:
        """プロセスを停止する (SIGTERM → タイムアウト → SIGKILL).

        プロセスグループ kill で子プロセスも確実に停止する。

        Args:
            proc: 停止するプロセス
            name: ログ用のプロセス名
            display: ログ用のディスプレイ名
        """
        if proc.returncode is not None:
            logger.debug("%s already exited on %s", name, display)
            return

        pid = proc.pid
        try:
            # プロセスグループに SIGTERM
            os.killpg(pid, signal.SIGTERM)
            try:
                await asyncio.wait_for(proc.wait(), timeout=self._STOP_TIMEOUT)
                logger.info(
                    "%s exited gracefully on %s (PID=%d)", name, display, pid
                )
            except asyncio.TimeoutError:
                logger.warning(
                    "%s did not exit in %.1fs on %s, sending SIGKILL (PID=%d)",
                    name,
                    self._STOP_TIMEOUT,
                    display,
                    pid,
                )
                await self._kill_process(proc)
        except ProcessLookupError:
            logger.debug("%s already exited on %s (PID=%d)", name, display, pid)

    async def _kill_process(self, proc: asyncio.subprocess.Process) -> None:
        """プロセスを SIGKILL で強制終了する."""
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        try:
            await proc.wait()
        except Exception:
            pass
