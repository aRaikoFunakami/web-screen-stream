"""ストリーミング設定."""

from dataclasses import dataclass


@dataclass
class StreamConfig:
    """Xvfb + FFmpeg ストリーミング設定.

    Attributes:
        display: X11 ディスプレイ番号 (例: ":99")
        width: 画面幅 (px)
        height: 画面高さ (px)
        framerate: キャプチャフレームレート (fps)
        bitrate: H.264 ビットレート (例: "500k")
        maxrate: H.264 最大ビットレート (例: "800k")
        bufsize: H.264 バッファサイズ (例: "500k")
        gop_size: GOP サイズ (フレーム数, Late-join 間隔)
    """

    display: str = ":99"
    width: int = 1280
    height: int = 720
    framerate: int = 15
    bitrate: str = "500k"
    maxrate: str = "800k"
    bufsize: str = "500k"
    gop_size: int = 10
