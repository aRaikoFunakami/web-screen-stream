"""ブラウザストリーミングセッション管理.

FFmpegSource からの H.264 NAL units を複数の WebSocket クライアントに
マルチキャスト配信する。Late-join 対策として SPS/PPS + GOP キャッシュを保持。

android-screen-stream の StreamSession/StreamManager と同設計。
"""

from __future__ import annotations

import asyncio
import ctypes
import dataclasses
import json
import logging
import os
import re
import shutil
import signal
import sys
import socket
import tempfile
import threading
import time
import uuid
from urllib.parse import urlsplit
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Any

from web_screen_stream.config import ProxyConfigError, StreamConfig, check_proxy
from web_screen_stream.input import ControlChannel, XTestInjector
from web_screen_stream.ffmpeg_source import FFmpegSource
from web_screen_stream.h264_extractor import H264UnitExtractor

if TYPE_CHECKING:
    from web_screen_stream.xvfb import XvfbManager

logger = logging.getLogger(__name__)


class ControlBusyError(RuntimeError):
    """同じセッションの操作チャネルが既に開いている(呼び出し側は 4009 で返す)."""


# GOP キャッシュの上限サイズ（4MB 超で自動クリア）
MAX_GOP_BYTES = 4 * 1024 * 1024

# subscriber queue が満杯の場合はドロップ（遅いクライアントを待たない）
DEFAULT_QUEUE_SIZE = 200

# sentinel: ストリーム終了を通知
_SENTINEL = b""


class BrowserStreamSession:
    """1つのブラウザストリーミングセッション.

    FFmpegSource → H264UnitExtractor → マルチキャスト配信。
    GOP キャッシュにより Late-join クライアントも即座にデコード開始可能。
    """

    def __init__(
        self,
        session_id: str,
        config: StreamConfig,
        *,
        url: str | None = None,
    ):
        self._session_id = session_id
        self._config = config
        self._url = url
        self._created_at = time.time()
        self._ffmpeg = FFmpegSource(config)
        self._subscribers: list[asyncio.Queue[bytes]] = []
        self._lock = asyncio.Lock()
        self._broadcast_task: asyncio.Task | None = None
        self._status = "created"

        # GOP キャッシュ（Late-join 用）
        self._last_sps: bytes = b""
        self._last_pps: bytes = b""
        self._gop_nals: list[bytes] = []
        self._gop_bytes: int = 0
        self._gop_has_idr: bool = False

        # 最初の IDR 到着を待つためのイベント
        self._idr_ready = asyncio.Event()

    @property
    def session_id(self) -> str:
        return self._session_id

    @property
    def status(self) -> str:
        return self._status

    @property
    def subscriber_count(self) -> int:
        return len(self._subscribers)

    @property
    def url(self) -> str | None:
        """表示中の URL."""
        return self._url

    @property
    def display(self) -> str:
        """このセッションの Xvfb ディスプレイ."""
        return self._config.display

    @property
    def created_at(self) -> float:
        """作成時刻 (Unix timestamp)."""
        return self._created_at

    @property
    def resolution(self) -> str:
        """解像度 (例: '1280x720')."""
        return f"{self._config.width}x{self._config.height}"

    async def start(self) -> None:
        """セッション開始: FFmpeg 起動 + ブロードキャストループ開始."""
        if self._status not in ("created", "stopped"):
            raise RuntimeError(f"Cannot start session in {self._status} state")

        self._status = "starting"
        logger.info("Starting session %s", self._session_id)

        await self._ffmpeg.start()
        self._broadcast_task = asyncio.create_task(
            self._run_broadcast(), name=f"broadcast-{self._session_id}"
        )
        self._status = "streaming"
        logger.info("Session %s is now streaming", self._session_id)

    async def stop(self) -> None:
        """セッション停止: FFmpeg 停止 + 全 subscriber に終了通知."""
        if self._status in ("stopped", "stopping"):
            return

        self._status = "stopping"
        logger.info("Stopping session %s", self._session_id)

        # FFmpeg 停止
        await self._ffmpeg.stop()

        # ブロードキャストタスク終了待ち
        if self._broadcast_task and not self._broadcast_task.done():
            self._broadcast_task.cancel()
            try:
                await self._broadcast_task
            except asyncio.CancelledError:
                pass

        # 全 subscriber に終了通知
        async with self._lock:
            for queue in self._subscribers:
                try:
                    queue.put_nowait(_SENTINEL)
                except asyncio.QueueFull:
                    pass
            self._subscribers.clear()

        self._status = "stopped"
        logger.info("Session %s stopped", self._session_id)

    async def subscribe(self) -> AsyncIterator[bytes]:
        """WebSocket クライアントの購読を開始する.

        Late-join: GOP キャッシュを Queue に先詰めしてから subscribers に登録。
        これにより SPS→PPS→IDR→non-IDR の順序が保証される。
        GOP キャッシュに IDR がない場合は、最初の IDR が到着するまで
        non-IDR フレームをスキップする（ブロッキングなし）。

        Yields:
            H.264 NAL units (Annex-B 形式)
        """
        async with self._lock:
            # GOP スナップショットを取得
            has_idr = self._gop_has_idr
            gop_snapshot = (
                list(self._gop_nals) if has_idr else []
            )
            qsize = max(DEFAULT_QUEUE_SIZE, len(gop_snapshot) + DEFAULT_QUEUE_SIZE)
            queue: asyncio.Queue[bytes] = asyncio.Queue(maxsize=qsize)

            # GOP をQueue に先詰め（ロック中 → 順序保証）
            for nal in gop_snapshot:
                queue.put_nowait(nal)

            self._subscribers.append(queue)
            logger.info(
                "Session %s: subscriber added (total=%d, late_join_nals=%d, has_idr=%s)",
                self._session_id,
                len(self._subscribers),
                len(gop_snapshot),
                has_idr,
            )

        # IDR が GOP キャッシュになかった場合は、最初の IDR が来るまでスキップ
        # ただし SPS/PPS は先に送ってデコーダ初期化を助ける。
        got_idr = has_idr
        sent_sps = has_idr
        sent_pps = has_idr

        try:
            while True:
                nal = await queue.get()
                if nal is _SENTINEL:
                    break
                if not got_idr:
                    nal_type = H264UnitExtractor.nal_type(nal)
                    if nal_type == H264UnitExtractor.NAL_TYPE_SPS:
                        if not sent_sps:
                            sent_sps = True
                            yield nal
                        continue
                    if nal_type == H264UnitExtractor.NAL_TYPE_PPS:
                        if not sent_pps:
                            sent_pps = True
                            yield nal
                        continue
                    if nal_type == H264UnitExtractor.NAL_TYPE_IDR:
                        got_idr = True
                        # IDR の前に SPS/PPS を送る
                        if not sent_sps and self._last_sps:
                            sent_sps = True
                            yield self._last_sps
                        if not sent_pps and self._last_pps:
                            sent_pps = True
                            yield self._last_pps
                    else:
                        # non-IDR フレームはスキップ
                        continue
                yield nal
        finally:
            async with self._lock:
                if queue in self._subscribers:
                    self._subscribers.remove(queue)
                    logger.info(
                        "Session %s: subscriber removed (total=%d)",
                        self._session_id,
                        len(self._subscribers),
                    )

    def _update_gop_cache(self, nal: bytes) -> None:
        """GOP キャッシュを更新する.

        NAL type に応じてキャッシュを更新:
        - SPS/PPS: 最新値を保存
        - IDR: 新 GOP 開始（SPS + PPS + IDR）
        - non-IDR: 現在の GOP に追記
        """
        nal_type = H264UnitExtractor.nal_type(nal)

        if nal_type == H264UnitExtractor.NAL_TYPE_SPS:
            self._last_sps = nal
            return

        if nal_type == H264UnitExtractor.NAL_TYPE_PPS:
            self._last_pps = nal
            return

        if nal_type == H264UnitExtractor.NAL_TYPE_IDR:
            # 新 GOP 開始
            self._gop_nals = []
            self._gop_bytes = 0

            # SPS + PPS を先頭に
            if self._last_sps:
                self._gop_nals.append(self._last_sps)
                self._gop_bytes += len(self._last_sps)
            if self._last_pps:
                self._gop_nals.append(self._last_pps)
                self._gop_bytes += len(self._last_pps)

            self._gop_nals.append(nal)
            self._gop_bytes += len(nal)
            self._gop_has_idr = True
            self._idr_ready.set()
            return

        if nal_type == H264UnitExtractor.NAL_TYPE_NON_IDR:
            if self._gop_has_idr:
                self._gop_nals.append(nal)
                self._gop_bytes += len(nal)

                # 上限超えたらキャッシュクリア
                if self._gop_bytes > MAX_GOP_BYTES:
                    logger.warning(
                        "GOP cache exceeded %d bytes, clearing",
                        MAX_GOP_BYTES,
                    )
                    self._gop_nals.clear()
                    self._gop_bytes = 0
                    self._gop_has_idr = False

    async def _run_broadcast(self) -> None:
        """FFmpeg → NAL 抽出 → 全 subscriber に配信するループ."""
        try:
            async for nal in self._ffmpeg.stream():
                self._update_gop_cache(nal)

                async with self._lock:
                    subscribers = list(self._subscribers)

                for queue in subscribers:
                    try:
                        queue.put_nowait(nal)
                    except asyncio.QueueFull:
                        pass  # 遅いクライアントはドロップ

        except asyncio.CancelledError:
            logger.info("Broadcast cancelled for session %s", self._session_id)
        except Exception:
            logger.exception("Broadcast error for session %s", self._session_id)
        finally:
            logger.info("Broadcast loop ended for session %s", self._session_id)


# chrome_crashpad 等の生存確認後、SIGTERM の猶予として待つ秒数
_CHROME_REAP_SIGTERM_WAIT = 2.0


# 起動した Chromium に渡す、起動ごとに一意な目印の環境変数名。子（renderer・crashpad 等）は環境を引き継ぐ
_BROWSER_TAG_ENV = "WSS_BROWSER_TAG"


def _tagged_pids(tag: str, proc: str = "/proc") -> set[int]:
    """環境変数に目印 `tag` を持つ PID（= その起動で生まれた Chromium の全プロセス）を返す.

    chrome_crashpad は起動直後から親が PID 1 で、ppid の木ではブラウザの子孫に入らない。
    環境変数は fork・exec で引き継がれ、reparent されても残るので、これで自分のものだけを見分ける。
    同時に起動した別の Chromium（別の SessionManager・別のプロセス）は目印が違うので入らない。
    別ユーザーの PID は含めない（kill で PermissionError を出さない）。
    Linux 専用。/proc が無い環境（macOS 開発機など）では空集合を返す。
    `proc` はテストが偽の /proc に差し替えるための引数。
    """
    needle = f"\0{_BROWSER_TAG_ENV}={tag}\0".encode()  # 先頭の \0 で、末尾が同じ別名の変数を拾わない
    try:
        entries = os.listdir(proc)
    except FileNotFoundError:
        return set()
    pids: set[int] = set()
    for entry in entries:
        if not entry.isdigit():
            continue
        try:
            with open(f"{proc}/{entry}/environ", "rb") as f:
                environ = b"\0" + f.read() + b"\0"
        except OSError:  # 読む前に終了した・権限が無い（別ユーザー）
            continue
        if needle in environ and _owned_by_me(entry, proc):
            pids.add(int(entry))
    return pids


def _owned_by_me(pid: str, proc: str) -> bool:
    try:
        with open(f"{proc}/{pid}/status") as f:
            for line in f:
                if line.startswith("Uid:"):
                    return int(line.split()[1]) == os.getuid()  # 実 UID
    except (OSError, ValueError, IndexError):
        pass
    return False


def _pid_alive(pid: int) -> bool:
    """PID が生存しているか確認する（シグナルは送らない）."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


async def _reap_tagged(tag: str) -> None:
    """目印 `tag` を持つプロセスを、刈り取りの時点で探して刈り取る（起動時に PID を覚えない = PID の再利用に強い）."""
    try:
        await _stop_launched(tag)  # 先に自分の子を回収する（waitpid を _reap_chrome_pids と取り合わない）
        await _reap_chrome_pids(await asyncio.to_thread(_tagged_pids, tag))  # /proc の走査でループを止めない
        # 普通に起動した本体は asyncio が回収する（先に取ると終了コードが壊れる）。集合はループ側で作る
        # （_LAUNCHED は別のセッションの起動・停止で変わるので、別スレッドで回さない）
        launched = frozenset(p.pid for p in _LAUNCHED.values())
        await asyncio.to_thread(_collect_chrome_zombies, "/proc", launched)
    finally:  # 途中で失敗・期限切れになっても、普通に起動した分のプロファイル（Cookie を含む）は消す
        await asyncio.to_thread(shutil.rmtree, _profile_dir(tag), True)


def _collect_chrome_zombies(proc: str = "/proc", launched: frozenset[int] = frozenset()) -> None:
    """自分の子になった終了済みの chrome 系プロセス（zombie）を回収する.

    PID 1 が本プロセスのとき、reparent された crashpad 等が自力で先に終了すると zombie で残る。
    zombie は environ が空で目印では見つからないので、状態 Z・親が自分・名前が chrome* で探す。
    どの Manager の分でも、終了済みの chrome を回収するだけなので害はない。
    """
    try:
        entries = os.listdir(proc)
    except FileNotFoundError:
        return
    me = os.getpid()
    for entry in entries:
        if not entry.isdigit():
            continue
        try:
            with open(f"{proc}/{entry}/stat") as f:
                stat = f.read()
            state, ppid = stat.rpartition(")")[2].split()[:2]
            comm = stat[stat.index("(") + 1 : stat.rindex(")")]
        except (OSError, ValueError):
            continue
        if state == "Z" and int(ppid) == me and comm.startswith("chrome") and int(entry) not in launched:
            try:
                os.waitpid(int(entry), os.WNOHANG)
            except ChildProcessError:
                pass


async def _reap_chrome_pids(pids: set[int]) -> None:
    """browser.close() で終了しなかった chrome 系プロセスを個別に刈り取る.

    crashpad handler はブラウザ本体への CDP close に反応せず、コンテナの
    PID 1（本プロセス）に reparent されたまま生き残る設計のため、
    `_tagged_pids` が見つけた、その起動の Chromium の PID を対象に
    SIGTERM → 猶予 → SIGKILL する。Playwright は Node driver 経由で
    Chromium を起動しており、Python 側はプロセスグループを所有していない
    ため killpg ではなく PID 単位で kill する。
    reparent 後は本プロセスが実の親になるため、kill しただけでは zombie
    （PID 1 が回収しない限り消えない）として残る。自分の子として
    os.waitpid で回収する。
    """
    survivors = {pid for pid in pids if _pid_alive(pid)}
    if not survivors:
        return

    logger.warning("Reaping leftover chrome processes: %s", sorted(survivors))
    for pid in survivors:
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        except OSError:  # PermissionError 等。他の PID と後続の段を止めない
            logger.exception("Cannot SIGTERM leftover chrome process PID=%d", pid)

    await asyncio.sleep(_CHROME_REAP_SIGTERM_WAIT)

    for pid in survivors:
        if _pid_alive(pid):
            try:
                os.kill(pid, signal.SIGKILL)
                logger.warning("Force-killed leftover chrome process PID=%d", pid)
            except ProcessLookupError:
                pass
            except OSError:
                logger.exception("Cannot SIGKILL leftover chrome process PID=%d", pid)

    # reparent の完了を少し待って回収（自分の子でなければ ChildProcessError → 諦める）
    for _ in range(10):
        for pid in list(survivors):
            try:
                waited_pid, _status = os.waitpid(pid, os.WNOHANG)
            except ChildProcessError:
                survivors.discard(pid)
                continue
            if waited_pid == pid:
                survivors.discard(pid)
        if not survivors:
            break
        await asyncio.sleep(0.1)


_CDP_READY_TIMEOUT = 15.0  # 普通に起動した Chromium が CDP のポートを開くまでの上限（秒）


def _profile_dir(browser_tag: str) -> str:
    """普通に起動する Chromium の、起動ごとのプロファイル（目印から決まるので、片付けで消せる）."""
    return os.path.join(tempfile.gettempdir(), f"wss-chromium-{browser_tag}")


def _chromium_args(config: StreamConfig, proxy: dict[str, str] | None) -> list[str]:
    return [
        f"--display={config.display}",
        "--no-sandbox",
        "--disable-gpu",
        "--disable-dev-shm-usage",  # Playwright の launch と同じ。/dev/shm が小さいコンテナ（既定 64MB）でタブが落ちない
        f"--window-size={config.width},{config.height}",
        "--window-position=0,0",
        # proxy を通すときは、WebRTC の UDP（STUN・ICE）もプロキシの外へ直接出さない。
        # --force-webrtc-ip-handling-policy は効かない（実 Chromium で ICE の候補を集めて確かめた）
        *(["--webrtc-ip-handling-policy=disable_non_proxied_udp"] if proxy else []),
    ]


def _proxy_origin(server: str) -> str:
    """check_proxy が通した server を「スキーム://ホスト:ポート」に組み直す（Playwright の launch と同じ。末尾の / を残さない）."""
    u = urlsplit(server)
    return f"{u.scheme}://{u.netloc}"


def _cdp_launch_args(executable: str, config: StreamConfig, proxy: dict[str, str] | None, port: int, profile: str) -> list[str]:
    """普通に起動する Chromium の引数. Playwright の launch と違い、--enable-automation などの自動操作の印を付けない."""
    return [
        executable,
        # 番号で渡す。0（Chromium に選ばせる）と --remote-debugging-pipe は navigator.webdriver が true になる（実測）。
        # このポートは 127.0.0.1 で認証なしに開く。同じホスト（コンテナ）のプロセスは CDP でブラウザを操作し Cookie を読める。
        # Xvfb も -ac（認証なし）で開いているので、同じホストのプロセスを信頼する前提は変わらない
        f"--remote-debugging-port={port}",
        f"--user-data-dir={profile}",
        "--no-first-run",
        "--no-default-browser-check",
        # Playwright の launch と同じく、ポップアップを止めない（SSO のボタンは非同期の処理の後に window.open することがある）
        "--disable-popup-blocking",
        *_chromium_args(config, proxy),
        # Playwright の launch(proxy=...) と同じく、localhost もプロキシを通す（<-loopback>）
        *([f"--proxy-server={_proxy_origin(proxy['server'])}", "--proxy-bypass-list=<-loopback>"] if proxy else []),
        *([f"--user-agent={config.user_agent}"] if config.user_agent else []),
        "about:blank",
    ]


async def _launch_over_cdp(pw: Any, config: StreamConfig, browser_tag: str, proxy: dict[str, str] | None) -> Any:
    """Playwright の Chromium を普通に起動し、CDP で接続した Browser を返す.

    proxy は認証なし（server だけ）に限る。起動から接続までに失敗・取り消しされたら、起動したプロセスを止める。
    接続した後のプロセスの終了は、片付けの _close_browser と目印の刈り取り（_reap_tagged）が行う。
    """
    profile = _profile_dir(browser_tag)
    os.makedirs(profile, mode=0o700)
    # ponytail: 選んでから Chromium が listen するまでに他が取ると、Chromium は待ち受けられず期限切れで失敗する。
    # 起き始めたら再試行を足す（他人のブラウザに繋がることは無い。下の DevTools の行で繋ぐ）
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    # 標準エラーはパイプにしない。crashpad などの子が引き継いで残ると、asyncio の proc.wait() が戻らなくなる
    log_path = os.path.join(profile, "chromium.log")
    with open(log_path, "wb") as log:
        proc = await asyncio.create_subprocess_exec(
            *_cdp_launch_args(pw.chromium.executable_path, config, proxy, port, profile),
            env={**os.environ, _BROWSER_TAG_ENV: browser_tag},  # 指定すると既定の環境を置き換えるので、足して渡す
            stdout=asyncio.subprocess.DEVNULL,
            stderr=log,
            preexec_fn=_die_with_parent,
        )
    _LAUNCHED[browser_tag] = proc  # 刈り取りで止めて回収する（/proc の無い環境でも残さない）
    try:
        ws_url = await _devtools_ws_url(proc, log_path)
        # ポートの番号ではなく、自分が起動した Chromium が出した URL（ブラウザ固有の ID 入り）に繋ぐ
        return await pw.chromium.connect_over_cdp(ws_url)
    except BaseException:
        if proc.returncode is None:
            proc.kill()
        raise


def _die_with_parent() -> None:
    """（fork した子で呼ばれる）本プロセスが落ちたら Chromium も止める. Linux だけ.

    Playwright の launch では driver が Chromium を止めていた。直接の子にしたので、親が SIGKILL などで落ちると
    認証なしの CDP ポートとログイン済みのプロファイルが残る。PR_SET_PDEATHSIG で親の終了時に SIGKILL を受ける。
    """
    if _PRCTL is not None:
        _PRCTL(1, signal.SIGKILL)  # 1 = PR_SET_PDEATHSIG


# fork した子では共有ライブラリを開かない（他のスレッドが握っていたロックで止まりうる）ので、先に解決しておく
_PRCTL = ctypes.CDLL(None, use_errno=True).prctl if sys.platform.startswith("linux") else None

_DEVTOOLS_LINE = re.compile(rb"DevTools listening on (ws://\S+)")
_LAUNCHED: dict[str, asyncio.subprocess.Process] = {}  # 目印 → 普通に起動した Chromium の本体


async def _devtools_ws_url(proc: asyncio.subprocess.Process, log_path: str) -> str:
    """Chromium が待ち受けを始めたときに標準エラー（ログのファイル）へ出す接続先（ws://…/devtools/browser/<id>）を返す."""
    deadline = time.monotonic() + _CDP_READY_TIMEOUT
    while True:
        with open(log_path, "rb") as f:
            if m := _DEVTOOLS_LINE.search(f.read()):
                return m.group(1).decode()
        if proc.returncode is not None:
            raise RuntimeError(f"Chromium が CDP のポートを開かずに終了しました（終了コード {proc.returncode}）")
        if time.monotonic() > deadline:
            raise RuntimeError(f"Chromium が {_CDP_READY_TIMEOUT} 秒以内に CDP のポートを開きませんでした")
        await asyncio.sleep(0.1)


async def _stop_launched(tag: str) -> None:
    """普通に起動した Chromium の本体を止めて回収する（Browser.close で終了していれば回収だけ）."""
    proc = _LAUNCHED.get(tag)
    if proc is None:
        return
    try:
        await asyncio.wait_for(proc.wait(), _CHROME_REAP_SIGTERM_WAIT)  # Browser.close の後の自然な終了を待つ
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
    _LAUNCHED.pop(tag, None)  # 回収が済むまでは、他のセッションの zombie の回収から外しておく


async def _close_browser(browser: Any) -> None:
    """ブラウザを終了する. CDP で接続しただけのブラウザは browser.close() では終了しないので、先に Browser.close を送る."""
    try:
        cdp = await browser.new_browser_cdp_session()
        await cdp.send("Browser.close")
    except Exception:  # 既に終了したブラウザ。下の close と刈り取りに任せる（launch したブラウザでも送るが、Playwright が後始末する）
        logger.debug("Browser.close failed", exc_info=True)
    await browser.close()


# 片付けの各段の期限（秒）。各段の内部の期限より長くする
# （FFmpegSource.stop は SIGTERM 5 秒→SIGKILL、_reap_chrome_pids は約 3 秒かかる。
#  短くすると、内部の SIGKILL へ進む前に取り消してしまう）。
_T_SESSION = 10.0
_T_CLOSE = 5.0
_T_REAP = 10.0
_T_RELEASE = 20.0
_TIMEOUT_SCALE = 1.0  # テストが期限を縮めるための倍率

_CleanupStep = tuple[str, Callable[[], Awaitable[Any]], float]


def _consume_exception(task: asyncio.Future) -> None:
    if not task.cancelled():
        task.exception()  # 期限切れで見捨てた段の例外を回収し、未取得の警告を出さない


async def _run_steps(steps: list[_CleanupStep]) -> None:
    """各段を期限付きで順に実行する. 失敗・期限切れは記録して次の段へ進む."""
    for name, fn, timeout in steps:
        task = asyncio.ensure_future(fn())
        done, _ = await asyncio.wait({task}, timeout=timeout * _TIMEOUT_SCALE)
        if not done:
            # 返らない段は待たずに次へ進む（強制終了の段が後ろに控えている）
            task.cancel()
            task.add_done_callback(_consume_exception)
            logger.error("Cleanup step %s timed out after %.1fs", name, timeout * _TIMEOUT_SCALE)
            continue
        try:
            task.result()
        except asyncio.CancelledError:
            logger.warning("Cleanup step %s was cancelled", name)
        except Exception:
            logger.exception("Cleanup step %s failed", name)


async def _cleanup(steps: list[_CleanupStep]) -> None:
    """片付けを、呼び出し側の取り消しから守りつつ、期限付きで行う.

    片付けは別タスクで走らせる。待っている間に取り消されても止めず（各段が有界なので有限）、
    終わってから CancelledError を投げ直す（取り消しを握りつぶさない）。
    """
    task = asyncio.ensure_future(_run_steps(steps))
    cancelled: asyncio.CancelledError | None = None
    while not task.done():
        try:
            await asyncio.wait({task})
        except asyncio.CancelledError as e:
            cancelled = cancelled or e
    task.result()
    if cancelled is not None:
        raise cancelled


class SessionManager:
    """ブラウザストリーミングセッションの管理.

    セッションの作成・停止・取得・一覧を提供する。
    XvfbManager を注入するとセッションごとに独立した Xvfb を動的起動する。
    XvfbManager 未設定の場合は従来通り config.display を使用する。
    """

    def __init__(self, xvfb_manager: XvfbManager | None = None):
        self._sessions: dict[str, BrowserStreamSession] = {}
        self._browsers: dict[str, tuple[Any, Any, Any]] = {}
        # (playwright_instance, browser, page)
        self._browser_tags: dict[str, str] = {}  # session_id → 起動した Chromium の目印
        self._displays: dict[str, str] = {}  # session_id → display
        self._xvfb = xvfb_manager
        self._lock = asyncio.Lock()
        self._control_lock = threading.Lock()  # open_control はスレッドからも呼ばれる
        self._controllers: dict[str, ControlChannel | object] = {}  # session_id → 開いている操作チャネル(同時に 1 つだけ)

    async def create(
        self,
        session_id: str,
        config: StreamConfig | None = None,
        url: str | None = None,
    ) -> BrowserStreamSession:
        """セッション作成: FFmpeg 起動 + ブロードキャスト開始.

        XvfbManager が設定されている場合:
          1. xvfb_manager.allocate(width, height) でディスプレイ確保
          2. config.display をそのディスプレイに設定
          3. 途中で失敗した場合は確保済みリソースを逆順解放

        Args:
            session_id: セッション識別子
            config: ストリーム設定（省略時はデフォルト）
            url: 開くURL（Playwright でブラウザ起動）

        Returns:
            作成した BrowserStreamSession

        Raises:
            ValueError: 既に同じ session_id が存在する場合
            ProxyConfigError: config.proxy が強制のプロキシとして使えない（url 無しで proxy を渡したときも）
            RuntimeError: Xvfb 起動失敗、ブラウザ起動失敗
        """
        async with self._lock:
            if session_id in self._sessions:
                raise ValueError(f"Session {session_id} already exists")

            if config is None:
                config = StreamConfig()
            if config.proxy is not None and not url:
                # ブラウザを起動しない（url 無し）と proxy は効かない。黙って無視せず、表示の確保の前に拒む
                raise ProxyConfigError("StreamConfig.proxy は、ブラウザを起動する（url を渡す）create() でだけ使えます")

            display = None
            session = None
            browser_info = None
            browser_tag = ""

            try:
                # Phase 1: ディスプレイ確保（XvfbManager がある場合）
                if self._xvfb:
                    display = await self._xvfb.allocate(config.width, config.height)
                    # 項目を並べ直すと、足した項目（proxy など）が黙って落ちるので replace で display だけ変える
                    config = dataclasses.replace(config, display=display)

                # Phase 2: ブラウザ起動（url が指定された場合）。失敗・取り消しの片付けは _launch_browser が行う
                if url:
                    pw, browser, page, browser_tag = await self._launch_browser(
                        session_id, config, url
                    )
                    if pw is not None:
                        browser_info = (pw, browser, page)
                        self._browsers[session_id] = browser_info
                        self._browser_tags[session_id] = browser_tag

                # Phase 3: FFmpeg + ストリーミング開始
                session = BrowserStreamSession(session_id, config, url=url)
                await session.start()
                self._sessions[session_id] = session
                if display:
                    self._displays[session_id] = display

                logger.info("Session %s created (url=%s)", session_id, url)
                return session

            except BaseException:  # 取り消し（CancelledError）でも資源を残さない
                self._browsers.pop(session_id, None)
                self._browser_tags.pop(session_id, None)
                steps = self._teardown_steps(session, browser_info, browser_tag, display)
                await _cleanup(steps)  # 期限付き。取り消された場合は片付けの後に CancelledError が届く
                raise

    def _teardown_steps(
        self,
        session: BrowserStreamSession | None,
        browser_info: tuple[Any, Any, Any] | None,
        browser_tag: str | None,
        display: str | None,
        controller: object | None = None,
    ) -> list[_CleanupStep]:
        """create の失敗と stop が共有する、逆順の片付けの段."""
        steps: list[_CleanupStep] = []
        if isinstance(controller, ControlChannel):  # 接続中の予約(object)なら open_control 側が後始末する
            # 注入中なら終わるのを待つ(イベントループは止めない)
            steps.append(("control.close", lambda: asyncio.to_thread(controller.close), _T_SESSION))
        if session is not None:
            steps.append(("session.stop", session.stop, _T_SESSION))
        if browser_info:
            pw, browser, _page = browser_info
            if browser is not None:
                steps.append(("browser.close", lambda: _close_browser(browser), _T_CLOSE))
            steps.append(("pw.stop", pw.stop, _T_CLOSE))
        if browser_tag:  # crashpad handler 等、CDP close に反応せず生き残るプロセスを刈り取る
            steps.append(("reap", lambda: _reap_tagged(browser_tag), _T_REAP))
        if display and self._xvfb:
            xvfb = self._xvfb
            steps.append(("display.release", lambda: xvfb.release(display), _T_RELEASE))
        return steps

    async def stop(self, session_id: str) -> None:
        """セッション停止: FFmpeg 停止 + ブラウザ終了 + リソース解放.

        逆順解放:
          1. session.stop() → FFmpeg 停止 + subscriber 通知
          2. browser.close() + pw.stop() → Playwright 完全解放
          3. xvfb_manager.release() → Xvfb + Fluxbox 停止

        各ステップは独立 try/except（1つの失敗で他が止まらない）。

        Args:
            session_id: 停止するセッションID

        Raises:
            KeyError: セッションが存在しない場合
        """
        async with self._lock:
            session = self._sessions.pop(session_id, None)
            if session is None:
                raise KeyError(f"Session {session_id} not found")

            browser_info = self._browsers.pop(session_id, None)
            browser_tag = self._browser_tags.pop(session_id, None)
            with self._control_lock:  # open_control(別スレッド)の予約・確定と排他にする
                controller = self._controllers.pop(session_id, None)
            display = self._displays.pop(session_id, None)

        # ロック外で停止（I/O 待ちが長い可能性）。取り消されても全段を期限付きで完走させる
        # 0. 操作チャネルを閉じる(同じディスプレイを次のセッションが使っても、古い操作が届かないように)
        # 1. FFmpeg + subscriber 停止  2. Playwright 解放  3. Xvfb + Fluxbox 解放
        await _cleanup(self._teardown_steps(session, browser_info, browser_tag, display, controller))

        logger.info("Session %s stopped and removed", session_id)

    def get(self, session_id: str) -> BrowserStreamSession | None:
        """セッション取得."""
        return self._sessions.get(session_id)

    def open_control(self, session_id: str) -> ControlChannel:
        """操作チャネルを開く. 同じセッションを同時に操作できるのは 1 つだけ.

        X への接続を待つので、イベントループからは asyncio.to_thread で呼ぶとよい。

        Raises:
            KeyError: セッションが存在しない(接続中に停止された場合も)
            ControlBusyError: 既に他の操作チャネルが開いている
            RuntimeError: X に繋がらない / XTEST が使えない
        """
        session = self._sessions.get(session_id)
        if session is None:
            raise KeyError(f"Session {session_id} not found")
        reservation = object()  # X への接続中も枠を押さえる(スレッドから呼ばれても二重に通さない)
        with self._control_lock:
            if self._sessions.get(session_id) is not session:  # 予約の直前に停止された
                raise KeyError(f"Session {session_id} not found")
            # 静的 Xvfb では複数のセッションが同じディスプレイを使うので、ディスプレイ単位でも 1 つにする
            same_display = any(
                (s := self._sessions.get(sid)) is not None and s.display == session.display for sid in self._controllers
            )
            if session_id in self._controllers or same_display:
                raise ControlBusyError(f"Session {session_id} is already being controlled")
            self._controllers[session_id] = reservation
        try:
            w, h = (int(v) for v in session.resolution.split("x"))
            injector = XTestInjector(session.display, w, h)
        except BaseException:
            with self._control_lock:
                if self._controllers.get(session_id) is reservation:
                    del self._controllers[session_id]
            raise

        def release() -> None:  # 自分が持っている枠だけを解放する(停止・再作成後の古いチャネルが新しい枠を外さない)
            with self._control_lock:
                if self._controllers.get(session_id) is channel:
                    del self._controllers[session_id]

        channel = ControlChannel(injector, release)
        with self._control_lock:
            stopped = self._controllers.get(session_id) is not reservation or self._sessions.get(session_id) is not session
            if not stopped:
                self._controllers[session_id] = channel
        if stopped:  # 接続中にセッションが停止された(close はロックの外で)
            injector.close()
            raise KeyError(f"Session {session_id} was stopped")
        return channel

    async def export_storage_state(self, session_id: str, path: str | None = None) -> dict:
        """このライブラリが起動したブラウザ(create(url=...))の認証状態を Playwright の storageState で返す.

        人がブラウザを操作してログインした後に呼び、Cookie(HttpOnly 含む)・localStorage・IndexedDB を取り出す。
        path を渡すと所有者だけが読める権限(0600)で排他作成する(既存ファイル・symlink があれば FileExistsError)。
        返り値・ファイルは秘密情報として扱うこと。

        Raises:
            KeyError: セッションが無い、またはこのライブラリがブラウザを起動していない
            FileExistsError: path が既に存在する
        """
        info = self._browsers.get(session_id)
        if info is None:
            raise KeyError(f"Session {session_id} has no browser launched by this manager")
        _pw, _browser, page = info
        state = await page.context.storage_state(indexed_db=True)
        if path is not None:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            try:
                with os.fdopen(fd, "w") as f:
                    json.dump(state, f)
            except BaseException:  # 書きかけの秘密ファイルを残さない
                os.unlink(path)
                raise
        return state

    def list_sessions(self) -> list[dict]:
        """アクティブセッション一覧（メタデータ付き）."""
        return [
            {
                "session_id": s.session_id,
                "status": s.status,
                "subscribers": s.subscriber_count,
                "url": s.url,
                "display": s.display,
                "resolution": s.resolution,
                "created_at": s.created_at,
            }
            for s in self._sessions.values()
        ]

    async def stop_all(self) -> None:
        """全セッション停止."""
        async with self._lock:
            session_ids = list(self._sessions.keys())

        cancelled: asyncio.CancelledError | None = None
        for sid in session_ids:
            try:
                await self.stop(sid)
            except asyncio.CancelledError as e:  # 取り消されても残りのセッションを止めてから投げ直す
                cancelled = cancelled or e
            except Exception:
                logger.exception("Error stopping session %s", sid)
        if cancelled is not None:
            raise cancelled

    async def _launch_browser(
        self, session_id: str, config: StreamConfig, url: str
    ) -> tuple[Any, Any, Any, str]:
        """Playwright の Chromium を起動してページを開く.

        人が操作するブラウザなので、普通に起動して CDP で接続する（`_launch_over_cdp`）。
        認証付きの proxy のときだけ Playwright の launch を使う（自動操作に見える）。
        起動ごとに一意な目印を環境変数で渡し、browser.close() では終了しない
        crashpad handler 等を、後で自分の分だけ刈り取れるようにする
        （`_tagged_pids` / `_reap_chrome_pids` 参照）。

        Returns:
            (playwright_instance, browser, page, browser_tag) のタプル
            Playwright が利用不可の場合は (None, None, None, "")
        """
        try:
            from playwright.async_api import async_playwright
        except ImportError:
            if config.proxy is not None:  # proxy を求められたのにブラウザを起動できない。黙って続けない（起動の失敗）
                raise RuntimeError("Playwright が無いので、proxy を通したブラウザを起動できません") from None
            logger.warning("Playwright not available, skipping browser launch")
            return (None, None, None, "")

        proxy = check_proxy(config.proxy) if config.proxy is not None else None  # 作った後に変えられていても確かめる
        browser_tag = uuid.uuid4().hex
        pw = browser = None
        try:
            pw = await async_playwright().start()
            if proxy is None or not (proxy.get("username") or proxy.get("password")):
                # 人が操作するブラウザなので、Playwright の launch は使わない。launch は --enable-automation などを付け、
                # サイトから自動操作に見える（navigator.webdriver が true。TVer のログインが reCAPTCHA で拒まれた）。
                # 普通に起動して CDP で接続する。
                browser = await _launch_over_cdp(pw, config, browser_tag, proxy)
                context = browser.contexts[0]
                page = context.pages[0] if context.pages else await context.new_page()
                await page.set_viewport_size({"width": config.width, "height": config.height})
            else:
                # ponytail: 認証付きの proxy は Playwright でしか渡せないので launch のまま（自動操作に見える）
                browser = await pw.chromium.launch(
                    headless=False,
                    env={**os.environ, _BROWSER_TAG_ENV: browser_tag},  # 指定すると既定の環境を置き換えるので、足して渡す
                    proxy=proxy,
                    args=_chromium_args(config, proxy),
                )
                page = await browser.new_page(
                    viewport={"width": config.width, "height": config.height},
                    **({"user_agent": config.user_agent} if config.user_agent else {}),
                )
            try:
                await page.goto(url, wait_until="domcontentloaded")
            except Exception as e:
                logger.error("Failed to navigate to %s: %s", url, e)
                raise RuntimeError(f"URL '{url}' を開けませんでした: {e}") from e
        except BaseException:
            # 起動・new_page・移動のどこで失敗・取り消されても、持っている資源を一度だけ片付ける
            steps: list[_CleanupStep] = []
            if browser is not None:
                steps.append(("browser.close", lambda: _close_browser(browser), _T_CLOSE))
            if pw is not None:
                steps.append(("pw.stop", pw.stop, _T_CLOSE))
                steps.append(("reap", lambda: _reap_tagged(browser_tag), _T_REAP))
            await _cleanup(steps)
            raise

        logger.info(
            "Browser launched for session %s: %s (%dx%d)",
            session_id,
            url,
            config.width,
            config.height,
        )
        return (pw, browser, page, browser_tag)
