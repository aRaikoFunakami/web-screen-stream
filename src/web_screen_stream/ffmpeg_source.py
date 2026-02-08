"""FFmpeg x11grab → H.264 パイプラインソース.

Xvfb 仮想ディスプレイを FFmpeg でキャプチャし、
H.264 Annex-B raw ストリームを stdout パイプで読み取る。
"""

import asyncio
import logging
import signal
from collections.abc import AsyncIterator

from web_screen_stream.config import StreamConfig
from web_screen_stream.h264_extractor import H264UnitExtractor

logger = logging.getLogger(__name__)

# FFmpeg stdout 読み取りチャンクサイズ
READ_CHUNK_SIZE = 32 * 1024  # 32KB


class FFmpegSource:
    """FFmpeg x11grab プロセスを管理し、H.264 NAL unit を生成する.

    Usage:
        source = FFmpegSource(config)
        await source.start()
        async for nal_unit in source.stream():
            # nal_unit は Annex-B 形式の bytes
            ...
        await source.stop()
    """

    def __init__(self, config: StreamConfig):
        self._config = config
        self._process: asyncio.subprocess.Process | None = None
        self._extractor = H264UnitExtractor()
        self._running = False

    @property
    def is_running(self) -> bool:
        return self._running

    def _build_command(self) -> list[str]:
        """FFmpeg コマンドを構築する."""
        c = self._config
        return [
            "ffmpeg",
            "-nostdin",
            "-f", "x11grab",
            "-video_size", f"{c.width}x{c.height}",
            "-framerate", str(c.framerate),
            "-draw_mouse", "0",
            "-i", c.display,
            "-c:v", "libx264",
            "-preset", "ultrafast",
            "-tune", "zerolatency",
            "-profile:v", "baseline",
            "-level", "3.1",
            "-pix_fmt", "yuv420p",
            "-g", str(c.gop_size),
            "-keyint_min", str(c.gop_size),
            "-sc_threshold", "0",
            "-b:v", c.bitrate,
            "-maxrate", c.maxrate,
            "-bufsize", c.bufsize,
            "-f", "h264",
            "-",
        ]

    async def start(self) -> None:
        """FFmpeg プロセスを起動する.

        Raises:
            RuntimeError: 既に起動中の場合
        """
        if self._running:
            raise RuntimeError("FFmpegSource is already running")

        cmd = self._build_command()
        logger.info("Starting FFmpeg: %s", " ".join(cmd))

        self._process = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        self._running = True
        logger.info("FFmpeg started (PID=%d)", self._process.pid)

        # stderr を非同期でログ出力（FFmpeg の進捗/エラー）
        asyncio.create_task(self._log_stderr())

    async def _log_stderr(self) -> None:
        """FFmpeg stderr をログに出力する."""
        if not self._process or not self._process.stderr:
            return
        try:
            async for line in self._process.stderr:
                text = line.decode("utf-8", errors="replace").rstrip()
                if text:
                    logger.debug("FFmpeg: %s", text)
        except Exception:
            pass

    async def stream(self) -> AsyncIterator[bytes]:
        """H.264 NAL unit を非同期で生成する.

        Yields:
            Annex-B 形式の NAL unit (bytes)
        """
        if not self._process or not self._process.stdout:
            raise RuntimeError("FFmpegSource is not started")

        try:
            while self._running:
                chunk = await self._process.stdout.read(READ_CHUNK_SIZE)
                if not chunk:
                    logger.info("FFmpeg stdout closed")
                    break

                nal_units = self._extractor.push(chunk)
                for nal in nal_units:
                    yield nal
        finally:
            # ストリーム終了時にバッファをフラッシュ
            remaining = self._extractor.flush()
            for nal in remaining:
                yield nal

    async def stop(self) -> None:
        """FFmpeg プロセスを停止する."""
        self._running = False
        if not self._process:
            return

        pid = self._process.pid
        logger.info("Stopping FFmpeg (PID=%d)", pid)

        try:
            # SIGTERM で graceful shutdown を試みる
            self._process.send_signal(signal.SIGTERM)
            try:
                await asyncio.wait_for(self._process.wait(), timeout=5.0)
                logger.info("FFmpeg exited gracefully (PID=%d)", pid)
            except asyncio.TimeoutError:
                # タイムアウトしたら SIGKILL
                logger.warning("FFmpeg did not exit in 5s, sending SIGKILL (PID=%d)", pid)
                self._process.kill()
                await self._process.wait()
        except ProcessLookupError:
            logger.debug("FFmpeg already exited (PID=%d)", pid)
        finally:
            self._process = None
