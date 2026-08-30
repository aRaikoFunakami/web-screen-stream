"""ブラウザストリーミングセッション管理.

FFmpegSource からの H.264 NAL units を複数の WebSocket クライアントに
マルチキャスト配信する。Late-join 対策として SPS/PPS + GOP キャッシュを保持。

android-screen-stream の StreamSession/StreamManager と同設計。
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Any

from web_screen_stream.config import StreamConfig
from web_screen_stream.ffmpeg_source import FFmpegSource
from web_screen_stream.h264_extractor import H264UnitExtractor

if TYPE_CHECKING:
    from web_screen_stream.xvfb import XvfbManager

logger = logging.getLogger(__name__)

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


def _snapshot_chrome_pids() -> set[int]:
    """/proc から chrome 系プロセス（chrome, chrome_crashpad 等）の PID 集合を取得する.

    Linux 専用。/proc が無い環境（macOS 開発機など）では空集合を返す。
    """
    pids: set[int] = set()
    try:
        entries = os.listdir("/proc")
    except FileNotFoundError:
        return pids
    for entry in entries:
        if not entry.isdigit():
            continue
        try:
            with open(f"/proc/{entry}/comm") as f:
                comm = f.read().strip()
        except OSError:
            continue
        if comm.startswith("chrome"):
            pids.add(int(entry))
    return pids


def _pid_alive(pid: int) -> bool:
    """PID が生存しているか確認する（シグナルは送らない）."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


async def _reap_chrome_pids(pids: set[int]) -> None:
    """browser.close() で終了しなかった chrome 系プロセスを個別に刈り取る.

    crashpad handler はブラウザ本体への CDP close に反応せず、コンテナの
    PID 1（本プロセス）に reparent されたまま生き残る設計のため、
    `_launch_browser` が launch 前後の /proc 差分で記録した PID を対象に
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

    await asyncio.sleep(_CHROME_REAP_SIGTERM_WAIT)

    for pid in survivors:
        if _pid_alive(pid):
            try:
                os.kill(pid, signal.SIGKILL)
                logger.warning("Force-killed leftover chrome process PID=%d", pid)
            except ProcessLookupError:
                pass

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
        self._browser_pids: dict[str, set[int]] = {}  # session_id → chrome系PID集合
        self._displays: dict[str, str] = {}  # session_id → display
        self._xvfb = xvfb_manager
        self._lock = asyncio.Lock()

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
            RuntimeError: Xvfb 起動失敗、ブラウザ起動失敗
        """
        async with self._lock:
            if session_id in self._sessions:
                raise ValueError(f"Session {session_id} already exists")

            if config is None:
                config = StreamConfig()

            display = None
            pw = None
            browser = None
            chrome_pids: set[int] = set()

            try:
                # Phase 1: ディスプレイ確保（XvfbManager がある場合）
                if self._xvfb:
                    display = await self._xvfb.allocate(config.width, config.height)
                    config = StreamConfig(
                        display=display,
                        width=config.width,
                        height=config.height,
                        framerate=config.framerate,
                        bitrate=config.bitrate,
                        maxrate=config.maxrate,
                        bufsize=config.bufsize,
                        gop_size=config.gop_size,
                    )

                # Phase 2: ブラウザ起動（url が指定された場合）
                if url:
                    pw, browser, page, chrome_pids = await self._launch_browser(
                        session_id, config, url
                    )
                    if pw is not None:
                        self._browsers[session_id] = (pw, browser, page)
                        self._browser_pids[session_id] = chrome_pids

                # Phase 3: FFmpeg + ストリーミング開始
                session = BrowserStreamSession(session_id, config, url=url)
                await session.start()
                self._sessions[session_id] = session
                if display:
                    self._displays[session_id] = display

                logger.info("Session %s created (url=%s)", session_id, url)
                return session

            except Exception:
                # 逆順クリーンアップ
                if browser:
                    try:
                        await browser.close()
                    except Exception:
                        logger.exception(
                            "Cleanup: error closing browser for %s", session_id
                        )
                if pw:
                    try:
                        await pw.stop()
                    except Exception:
                        logger.exception(
                            "Cleanup: error stopping playwright for %s", session_id
                        )
                if chrome_pids:
                    await _reap_chrome_pids(chrome_pids)
                self._browsers.pop(session_id, None)
                self._browser_pids.pop(session_id, None)
                if display and self._xvfb:
                    try:
                        await self._xvfb.release(display)
                    except Exception:
                        logger.exception(
                            "Cleanup: error releasing display %s", display
                        )
                raise

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
            chrome_pids = self._browser_pids.pop(session_id, None)
            display = self._displays.pop(session_id, None)

        # ロック外で停止（I/O 待ちが長い可能性）

        # 1. FFmpeg + subscriber 停止
        try:
            await session.stop()
        except Exception:
            logger.exception("Error stopping session %s", session_id)

        # 2. Playwright 解放
        if browser_info:
            pw, browser, _page = browser_info
            try:
                await browser.close()
                logger.info("Browser closed for session %s", session_id)
            except Exception:
                logger.exception("Error closing browser for session %s", session_id)
            try:
                await pw.stop()
                logger.info("Playwright stopped for session %s", session_id)
            except Exception:
                logger.exception(
                    "Error stopping playwright for session %s", session_id
                )
            # crashpad handler 等、CDP close に反応せず生き残るプロセスを刈り取る
            if chrome_pids:
                await _reap_chrome_pids(chrome_pids)

        # 3. Xvfb + Fluxbox 解放
        if display and self._xvfb:
            try:
                await self._xvfb.release(display)
            except Exception:
                logger.exception("Error releasing display %s", display)

        logger.info("Session %s stopped and removed", session_id)

    def get(self, session_id: str) -> BrowserStreamSession | None:
        """セッション取得."""
        return self._sessions.get(session_id)

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

        for sid in session_ids:
            try:
                await self.stop(sid)
            except Exception:
                logger.exception("Error stopping session %s", sid)

    async def _launch_browser(
        self, session_id: str, config: StreamConfig, url: str
    ) -> tuple[Any, Any, Any, set[int]]:
        """Playwright Chromium を起動してページを開く.

        launch 前後で /proc の chrome 系 PID を差分取得し、browser.close()
        では終了しない crashpad handler 等を後で個別に刈り取れるようにする
        （`_reap_chrome_pids` 参照）。

        Returns:
            (playwright_instance, browser, page, chrome_pids) のタプル
            Playwright が利用不可の場合は (None, None, None, set())
        """
        try:
            from playwright.async_api import async_playwright
        except ImportError:
            logger.warning("Playwright not available, skipping browser launch")
            return (None, None, None, set())

        pids_before = _snapshot_chrome_pids()
        pw = await async_playwright().start()
        browser = await pw.chromium.launch(
            headless=False,
            args=[
                f"--display={config.display}",
                "--no-sandbox",
                "--disable-gpu",
                f"--window-size={config.width},{config.height}",
                "--window-position=0,0",
            ],
        )
        page = await browser.new_page(
            viewport={"width": config.width, "height": config.height}
        )
        try:
            await page.goto(url, wait_until="domcontentloaded")
        except Exception as e:
            logger.error("Failed to navigate to %s: %s", url, e)
            try:
                await browser.close()
            except Exception:
                pass
            try:
                await pw.stop()
            except Exception:
                pass
            await _reap_chrome_pids(_snapshot_chrome_pids() - pids_before)
            raise RuntimeError(f"URL '{url}' を開けませんでした: {e}") from e

        chrome_pids = _snapshot_chrome_pids() - pids_before
        logger.info(
            "Browser launched for session %s: %s (%dx%d, chrome_pids=%d)",
            session_id,
            url,
            config.width,
            config.height,
            len(chrome_pids),
        )
        return (pw, browser, page, chrome_pids)
