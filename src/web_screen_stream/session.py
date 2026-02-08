"""ブラウザストリーミングセッション管理.

FFmpegSource からの H.264 NAL units を複数の WebSocket クライアントに
マルチキャスト配信する。Late-join 対策として SPS/PPS + GOP キャッシュを保持。

android-screen-stream の StreamSession/StreamManager と同設計。
"""

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from web_screen_stream.config import StreamConfig
from web_screen_stream.ffmpeg_source import FFmpegSource
from web_screen_stream.h264_extractor import H264UnitExtractor

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

    def __init__(self, session_id: str, config: StreamConfig):
        self._session_id = session_id
        self._config = config
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

    @property
    def session_id(self) -> str:
        return self._session_id

    @property
    def status(self) -> str:
        return self._status

    @property
    def subscriber_count(self) -> int:
        return len(self._subscribers)

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

        Yields:
            H.264 NAL units (Annex-B 形式)
        """
        async with self._lock:
            # GOP スナップショットを取得
            gop_snapshot = (
                list(self._gop_nals) if self._gop_has_idr else []
            )
            qsize = max(DEFAULT_QUEUE_SIZE, len(gop_snapshot) + DEFAULT_QUEUE_SIZE)
            queue: asyncio.Queue[bytes] = asyncio.Queue(maxsize=qsize)

            # GOP をQueue に先詰め（ロック中 → 順序保証）
            for nal in gop_snapshot:
                queue.put_nowait(nal)

            self._subscribers.append(queue)
            logger.info(
                "Session %s: subscriber added (total=%d, late_join_nals=%d)",
                self._session_id,
                len(self._subscribers),
                len(gop_snapshot),
            )

        try:
            while True:
                nal = await queue.get()
                if nal is _SENTINEL:
                    break
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


class SessionManager:
    """ブラウザストリーミングセッションの管理.

    セッションの作成・停止・取得・一覧を提供する。
    Step 1 では Playwright ブラウザの起動も行う。
    """

    def __init__(self):
        self._sessions: dict[str, BrowserStreamSession] = {}
        self._browsers: dict[str, tuple] = {}  # session_id → (browser, page)

    async def create(
        self,
        session_id: str,
        config: StreamConfig | None = None,
        url: str | None = None,
    ) -> BrowserStreamSession:
        """セッション作成: FFmpeg 起動 + ブロードキャスト開始.

        Args:
            session_id: セッション識別子
            config: ストリーム設定（省略時はデフォルト）
            url: 開くURL（Step 1 用、Playwright でブラウザ起動）

        Returns:
            作成した BrowserStreamSession

        Raises:
            ValueError: 既に同じ session_id が存在する場合
        """
        if session_id in self._sessions:
            raise ValueError(f"Session {session_id} already exists")

        if config is None:
            config = StreamConfig()

        session = BrowserStreamSession(session_id, config)

        # Step 1: Playwright でブラウザ起動（url が指定された場合）
        if url:
            await self._launch_browser(session_id, config, url)

        await session.start()
        self._sessions[session_id] = session
        logger.info("Session %s created (url=%s)", session_id, url)
        return session

    async def stop(self, session_id: str) -> None:
        """セッション停止: FFmpeg 停止 + ブラウザ終了 + リソース解放.

        Args:
            session_id: 停止するセッションID

        Raises:
            KeyError: セッションが存在しない場合
        """
        session = self._sessions.pop(session_id, None)
        if session is None:
            raise KeyError(f"Session {session_id} not found")

        await session.stop()

        # ブラウザ終了
        browser_info = self._browsers.pop(session_id, None)
        if browser_info:
            browser, _ = browser_info
            try:
                await browser.close()
                logger.info("Browser closed for session %s", session_id)
            except Exception:
                logger.exception("Error closing browser for session %s", session_id)

        logger.info("Session %s stopped and removed", session_id)

    def get(self, session_id: str) -> BrowserStreamSession | None:
        """セッション取得."""
        return self._sessions.get(session_id)

    def list_sessions(self) -> list[dict]:
        """アクティブセッション一覧."""
        return [
            {
                "session_id": s.session_id,
                "status": s.status,
                "subscribers": s.subscriber_count,
            }
            for s in self._sessions.values()
        ]

    async def stop_all(self) -> None:
        """全セッション停止."""
        session_ids = list(self._sessions.keys())
        for sid in session_ids:
            try:
                await self.stop(sid)
            except Exception:
                logger.exception("Error stopping session %s", sid)

    async def _launch_browser(
        self, session_id: str, config: StreamConfig, url: str
    ) -> None:
        """Playwright Chromium を起動してページを開く."""
        try:
            from playwright.async_api import async_playwright
        except ImportError:
            logger.warning("Playwright not available, skipping browser launch")
            return

        pw = await async_playwright().__aenter__()
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
                await pw.__aexit__(None, None, None)
            except Exception:
                pass
            raise RuntimeError(f"URL '{url}' を開けませんでした: {e}") from e

        self._browsers[session_id] = (browser, page)
        logger.info(
            "Browser launched for session %s: %s (%dx%d)",
            session_id,
            url,
            config.width,
            config.height,
        )
