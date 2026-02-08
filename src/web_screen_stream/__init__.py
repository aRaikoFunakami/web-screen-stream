"""web-screen-stream: Browser screen streaming via Xvfb + FFmpeg H.264 + WebSocket."""

from web_screen_stream.config import StreamConfig
from web_screen_stream.ffmpeg_source import FFmpegSource
from web_screen_stream.h264_extractor import H264UnitExtractor
from web_screen_stream.session import BrowserStreamSession, SessionManager
from web_screen_stream.xvfb import XvfbManager

__all__ = [
    "BrowserStreamSession",
    "FFmpegSource",
    "H264UnitExtractor",
    "SessionManager",
    "StreamConfig",
    "XvfbManager",
]
