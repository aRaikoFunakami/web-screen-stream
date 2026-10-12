"""Xvfb ディスプレイ管理ユーティリティ.

既存の静的ユーティリティ (get_display, check_display) に加え、
XvfbManager でセッションごとに独立した Xvfb + Fluxbox を動的管理する。
"""

import asyncio
import logging
import os
import signal
from dataclasses import dataclass, field

from web_screen_stream.focus_relay import FocusRelay, render_fluxbox_apps_config

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


class DisplaysHeldError(RuntimeError):
    """空き番号が、他のプロセス（前の backend の孤児 Xvfb 等）に握られた分だけ足りない.

    メッセージは容量不足と同じ "Maximum displays reached" で始まる。待っても解放
    されないので、呼び出し側は isinstance で通常の容量不足と区別できる。
    `held` は握られている display（例: [":100"]）。
    """

    def __init__(self, max_displays: int, held: list[str]):
        self.max_displays = max_displays
        self.held = held
        super().__init__(
            f"Maximum displays reached ({max_displays}); "
            f"held by other processes: {', '.join(held)}. "
            f"Stop an existing session first."
        )

    def __reduce__(self):
        # 既定の復元は args（メッセージだけ）で __init__ を呼び、TypeError になる
        return (type(self), (self.max_displays, self.held))


@dataclass
class DisplayInfo:
    """割り当て済みディスプレイの管理情報."""

    display: str  # ":100"
    xvfb_proc: asyncio.subprocess.Process
    fluxbox_proc: asyncio.subprocess.Process
    width: int
    height: int
    focus_relay: FocusRelay | None = None


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

    # X のロック/ソケットと /proc の場所（テストで差し替える）
    _LOCK_DIR = "/tmp"
    _SOCKET_DIR = "/tmp/.X11-unix"
    _PROC_DIR = "/proc"

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
        # 他プロセスに握られていると警告済みの番号 → 持ち主 PID
        # （確保のたびに同じ警告を出さない。持ち主が変われば再度警告する）
        self._warned_held: dict[int, int] = {}
        # 確保の途中（Xvfb 起動済み・未登録）の番号。他者が握る番号と区別して使用中扱いにする
        self._pending: set[int] = set()
        # 実行中の解放タスク（呼び出し側がキャンセルされても完走させるため参照を保持する）
        self._release_tasks: set[asyncio.Future[None]] = set()

    async def allocate(self, width: int = 1280, height: int = 720) -> str:
        """新しいディスプレイを割り当て、Xvfb + Fluxbox を起動する.

        Args:
            width: 画面幅 (px)
            height: 画面高さ (px)

        Returns:
            display 文字列 (例: ":100")

        Raises:
            DisplaysHeldError: 空きが、他のプロセス（孤児 Xvfb 等）に握られた番号だけに
                なった場合。待っても解放されない。
            RuntimeError: 最大数に達した場合、または Xvfb 起動失敗
        """
        async with self._lock:
            if len(self._displays) >= self._max:
                raise RuntimeError(
                    f"Maximum displays reached ({self._max}). "
                    f"Stop an existing session first."
                )

            # 1. 未使用かつ他の Xvfb に握られていない番号を選ぶ
            #    （stale ロックの清掃も _next_display_num が行う）
            display_num = self._next_display_num()
            self._pending.add(display_num)
            try:
                return await self._start_display(display_num, width, height)
            finally:
                self._pending.discard(display_num)

    async def _start_display(self, display_num: int, width: int, height: int) -> str:
        """選んだ番号で Xvfb + Fluxbox を起動して登録する（self._lock を保持して呼ぶ）."""
        display = f":{display_num}"

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
        failure = f"Xvfb failed to start on {display}"
        for _ in range(self._POLL_MAX_ATTEMPTS):
            await asyncio.sleep(self._POLL_INTERVAL)
            if xvfb_proc.returncode is not None:
                # 自分の Xvfb が即死している（同じ display に別の Xvfb が居る等）。
                # check_display は相手に繋がって成功してしまうので、成功扱いにしない。
                failure = (
                    f"Xvfb exited early on {display} "
                    f"(returncode={xvfb_proc.returncode}); "
                    f"display may be held by another Xvfb"
                )
                break
            if check_display(display):
                # 応答したのが自分の Xvfb とは限らない（起動確認が早すぎると
                # 即死前の自分ではなく、先に居た Xvfb に繋がる）。ロックの
                # 持ち主が自分であることで確かめる。
                holder = self._read_lock_pid(display_num)
                if holder is not None and not self._is_own_process(
                    holder, xvfb_proc.pid
                ):
                    failure = (
                        f"Xvfb on {display} is held by another process "
                        f"(PID={holder}, ours={xvfb_proc.pid})"
                    )
                    break
                started = True
                break

        if not started:
            # 起動失敗（タイムアウト・自分の Xvfb の早期終了・他者がロックを保持）、
            # クリーンアップ
            logger.error(failure)
            if xvfb_proc.returncode is None:
                await self._kill_process(xvfb_proc)
            self._cleanup_stale_lock(display_num)
            raise RuntimeError(failure)

        logger.info(
            "Xvfb started on %s (%dx%d, PID=%d)",
            display,
            width,
            height,
            xvfb_proc.pid,
        )

        # 4-8: Fluxbox 設定 + 起動 + FocusRelay 起動。
        # 失敗時は起動済みリソースを逆順で後始末してから再送出する
        # （途中失敗で Xvfb だけがリークするのを防ぐ）。
        fluxbox_proc: asyncio.subprocess.Process | None = None
        try:
            # 4. Fluxbox 設定: init + apps を書き込み
            #    init: グローバル設定（デコレーション無効、ツールバー非表示）
            #    apps: Chromium クラスのウィンドウにのみ EWMH fullscreen を強制
            #    （smartestiroid-ui#335）。[Maximized] は装飾除去とサイズ一致のみで
            #    Chromium 自身の chrome UI（タブバー・URL バー）は消えない。
            #    [Fullscreen] は _NET_WM_STATE_FULLSCREEN を立てる:
            #    - window.open() ポップアップ等の追加ウィンドウは、これを検知した
            #      Chromium が chrome UI を自ら非表示にする（F11 相当、実測確認済み）
            #    - Playwright launch() 経由の初回ウィンドウはこの通知に反応しない
            #      ため、呼び出し側で CDP Browser.setWindowBounds
            #      {windowState: fullscreen} を併用すること（実測確認済み）
            #    ルールを (name=.*) の全窓一致にしないのは、Fluxbox 自身が出す
            #    xmessage 等のダイアログまで fullscreen 化され録画を覆うため（実測）。
            #    apps ファイルの内容は focus_relay.render_fluxbox_apps_config()
            #    が唯一の生成元（統合テストもここを経由し、対象スコープの
            #    ドリフトを防ぐ）。
            fluxbox_home = "/root"
            fluxbox_dir = os.path.join(fluxbox_home, ".fluxbox")
            os.makedirs(fluxbox_dir, exist_ok=True)

            init_path = os.path.join(fluxbox_dir, "init")
            with open(init_path, "w") as f:
                f.write(
                    "session.screen0.defaultDeco: NONE\n"
                    "session.screen0.toolbar.visible: false\n"
                    "session.screen0.focusModel: ClickFocus\n"
                    "session.screen0.maxDisableMove: true\n"
                    "session.screen0.maxDisableResize: true\n"
                )

            apps_path = os.path.join(fluxbox_dir, "apps")
            with open(apps_path, "w") as f:
                f.write(render_fluxbox_apps_config())

            logger.info(
                "Fluxbox config written: init=%s, apps=%s",
                init_path,
                apps_path,
            )

            # 5. Fluxbox 起動（Xvfb の display を指定）
            fluxbox_proc = await asyncio.create_subprocess_exec(
                "fluxbox",
                "-display",
                display,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
                env={**os.environ, "DISPLAY": display, "HOME": fluxbox_home},
                start_new_session=True,
            )

            # 6. Fluxbox 起動待ち
            await asyncio.sleep(self._FLUXBOX_WAIT)
            logger.info(
                "Fluxbox started on %s (PID=%d)", display, fluxbox_proc.pid
            )

            # 7. FocusRelay 起動（smartestiroid-ui/web-screen-stream#2）
            #    Fluxbox の focus-stealing 防止から、複数ウィンドウ切替時の
            #    前面化を救済する。接続失敗は non-fatal（ログのみ）:
            #    単一ウィンドウ・多数タブのケースには影響させない。
            focus_relay: FocusRelay | None = FocusRelay(display)
            try:
                await focus_relay.start()
            except Exception:
                logger.exception(
                    "FocusRelay failed to start on %s, continuing without it",
                    display,
                )
                # start() が部分的に成功した場合（X 接続は確立済み）に
                # 備え、確実に閉じてから参照を破棄する（fd リーク防止）。
                await focus_relay.stop()
                focus_relay = None
        except Exception:
            logger.exception(
                "Fluxbox/FocusRelay setup failed on %s, cleaning up", display
            )
            if fluxbox_proc is not None:
                await self._kill_process(fluxbox_proc)
            await self._kill_process(xvfb_proc)
            self._cleanup_stale_lock(display_num)
            raise

        # 8. 管理テーブルに登録
        info = DisplayInfo(
            display=display,
            xvfb_proc=xvfb_proc,
            fluxbox_proc=fluxbox_proc,
            width=width,
            height=height,
            focus_relay=focus_relay,
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
            display_num = int(display.lstrip(":"))  # 登録済みの display だけを解析する
            # 停止が終わるまでは、自分の Xvfb がまだ生きている。確保途中と同様に
            # 使用中として数え、他者（孤児）が握る番号と取り違えない。
            self._pending.add(display_num)

        # 呼び出し側がキャンセルされても、Xvfb の停止・ロック清掃・pending の解除は
        # 最後まで行う（途中で止まると、生きた自分の Xvfb が孤児扱いで残る）。
        task = asyncio.ensure_future(self._finish_release(info, display, display_num))
        self._release_tasks.add(task)
        task.add_done_callback(self._release_tasks.discard)
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            # 呼び出し側は結果を受け取れない。失敗しても誰にも伝わらないので、ここでだけログに残す
            # （呼び出し側が待っている場合は例外がそのまま届くので、二重にログしない）。
            task.add_done_callback(self._log_release_failure)
            raise

    async def _finish_release(
        self, info: DisplayInfo, display: str, display_num: int
    ) -> None:
        """停止を行い、成否に関わらず pending を解除する."""
        try:
            await self._stop_display(info, display, display_num)
        finally:
            self._pending.discard(display_num)

    @staticmethod
    def _log_release_failure(task: "asyncio.Future[None]") -> None:
        if not task.cancelled() and task.exception() is not None:
            logger.error("Display release failed", exc_info=task.exception())

    async def _stop_display(
        self, info: DisplayInfo, display: str, display_num: int
    ) -> None:
        """ロック外で FocusRelay → Fluxbox → Xvfb の順に止め、ロックを清掃する."""
        # ロック外で停止（I/O 待ちが長い可能性）
        # 停止順序: FocusRelay → Fluxbox → Xvfb
        # （root ウィンドウが消える前に X 接続を畳んでおく）
        logger.info("Releasing display %s", display)

        if info.focus_relay is not None:
            try:
                await info.focus_relay.stop()
            except Exception:
                logger.exception("Error stopping FocusRelay on %s", display)

        await self._stop_process(info.fluxbox_proc, "Fluxbox", display)
        await self._stop_process(info.xvfb_proc, "Xvfb", display)

        # SIGKILL 後のロックファイル清掃
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
        """残り利用可能なディスプレイ数（他プロセスが握る番号は数えない）.

        ロックファイルと /proc を読むので、頻繁な呼び出しには向かない。
        """
        return sum(1 for num in self._unused_numbers() if self._holder_pid(num) is None)

    def held_displays(self) -> list[str]:
        """この manager の管理外の生きた Xvfb が握っている display（例: [":100"]）.

        ロックファイルと /proc を読む。
        """
        return [f":{n}" for n in self._unused_numbers() if self._holder_pid(n) is not None]

    @property
    def max_displays(self) -> int:
        """最大同時ディスプレイ数."""
        return self._max

    def _unused_numbers(self) -> list[int]:
        """範囲内で、この manager が登録も確保中にもしていない番号."""
        used = {int(d.lstrip(":")) for d in self._displays} | self._pending
        return [n for n in range(self._base, self._base + self._max) if n not in used]

    def _next_display_num(self) -> int:
        """未使用で、他のプロセスにも握られていないディスプレイ番号を返す.

        stale なロックはここで清掃する。生きた Xvfb（前の backend の孤児等）が
        握る番号は飛ばす。

        Returns:
            ディスプレイ番号

        Raises:
            DisplaysHeldError: 空きが、他プロセスに握られた番号だけになった場合
                （"Maximum displays reached" で始まる RuntimeError のサブクラス）
            RuntimeError: 空きがない場合
        """
        held: list[str] = []
        for num in self._unused_numbers():
            pid = self._cleanup_stale_lock(num)  # stale を清掃し、生きた持ち主の PID を得る
            if pid is not None:
                if self._warned_held.get(num) != pid:
                    self._warned_held[num] = pid
                    logger.warning(
                        "Display :%d is held by another Xvfb (PID=%d), skipping", num, pid
                    )
                held.append(f":{num}")
                continue
            self._warned_held.pop(num, None)
            return num
        if held:
            raise DisplaysHeldError(self._max, held)
        raise RuntimeError("No available display numbers")

    def _read_lock_pid(self, display_num: int) -> int | None:
        """ロックファイルの持ち主 PID を返す. ロックが無い・読めない場合は None."""
        try:
            with open(f"{self._LOCK_DIR}/.X{display_num}-lock") as f:
                return int(f.read().strip())
        except (ValueError, OSError):
            return None

    def _holder_pid(self, display_num: int) -> int | None:
        """その番号のロックを握る生きた Xvfb の PID. 握られていなければ None."""
        pid = self._read_lock_pid(display_num)
        return pid if pid is not None and self._is_xvfb_alive(pid) else None

    def _is_xvfb_alive(self, pid: int) -> bool:
        """PID が生きた Xvfb か. PID が別のプロセスに再利用されていれば False.

        /proc が読めない（macOS 開発機等、または権限不足）場合は、名前は確かめられない
        ので生存確認だけで判断する。読めないことを「プロセスが居ない」とは扱わない
        （生きた孤児のロックを消さないため）。
        """
        try:
            with open(f"{self._PROC_DIR}/{pid}/comm") as f:
                return f.read().strip().startswith("Xvfb")
        except FileNotFoundError:
            if os.path.isdir(self._PROC_DIR):
                return False  # /proc はあり、そのプロセスだけが居ない
        except OSError:
            pass  # PermissionError 等: 下の生存確認に任せる
        try:
            os.kill(pid, 0)  # シグナルは送らない
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        return True

    def _parent_pid(self, pid: int) -> int | None:
        """/proc/<pid>/stat から親 PID を返す. 読めなければ None."""
        try:
            with open(f"{self._PROC_DIR}/{pid}/stat") as f:
                # "pid (comm) S ppid ..." — comm は括弧を含み得るので最後の ')' 以降を読む
                return int(f.read().rsplit(")", 1)[1].split()[1])
        except (OSError, ValueError, IndexError):
            return None

    def _is_own_process(self, holder: int, spawned_pid: int) -> bool:
        """ロックの持ち主が、自分が起動したプロセスか（またはその子か）.

        Xvfb がラッパー経由で fork する環境でも、子がロックを書いたものとして扱う。
        """
        return holder == spawned_pid or self._parent_pid(holder) == spawned_pid

    def _cleanup_stale_lock(self, display_num: int) -> int | None:
        """stale なロックファイルを検出・削除する.

        SIGKILL で Xvfb が終了した場合、ロックファイルが残留する。
        持ち主の PID が生きた Xvfb でなければ（終了済み・別プロセスに再利用済み・
        読み取り不能）削除する。

        Args:
            display_num: ディスプレイ番号

        Returns:
            ロックを握る生きた Xvfb の PID。握られていなければ None
        """
        lock_file = f"{self._LOCK_DIR}/.X{display_num}-lock"
        socket_file = f"{self._SOCKET_DIR}/X{display_num}"

        if not os.path.exists(lock_file):
            return None

        holder = self._holder_pid(display_num)
        if holder is not None:
            # 生きている → 実際に使用中（警告は _next_display_num が持ち主ごとに一度出す）
            logger.debug("Display :%d is in use, skipping lock cleanup", display_num)
            return holder

        logger.warning("Cleaning stale lock for display :%d", display_num)
        for f in [lock_file, socket_file]:
            try:
                os.unlink(f)
            except FileNotFoundError:
                pass
        return None

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
