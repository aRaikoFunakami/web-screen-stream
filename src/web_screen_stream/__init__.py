"""web-screen-stream: Browser screen streaming via Xvfb + FFmpeg H.264 + WebSocket."""

from web_screen_stream.config import StreamConfig
from web_screen_stream.ffmpeg_source import FFmpegSource
from web_screen_stream.h264_extractor import H264UnitExtractor
from web_screen_stream.input import ControlChannel, ControlError, XTestInjector, parse_control_message
from web_screen_stream.session import BrowserStreamSession, ControlBusyError, SessionManager
from web_screen_stream.xvfb import DisplaysHeldError, XvfbManager

__all__ = [
    "DisplaysHeldError",
    "BrowserStreamSession",
    "ControlBusyError",
    "ControlChannel",
    "ControlError",
    "FFmpegSource",
    "H264UnitExtractor",
    "SessionManager",
    "StreamConfig",
    "XTestInjector",
    "XvfbManager",
    "parse_control_message",
]
