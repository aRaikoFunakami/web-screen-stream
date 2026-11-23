"""ストリーミング設定."""

from __future__ import annotations

import os
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
        draw_mouse: マウスカーソルを映像に描くか（操作チャネルで人が操作するときに True。既定は描かない）
        proxy: 自前起動する Chromium に渡すプロキシ（Playwright の launch(proxy=...) と同じ形の dict。例:
            {"server": "http://127.0.0.1:8899"}）。既定は無し。ブラウザの通信を絞り込むプロキシに強制的に通すときに使う
            （Playwright は --proxy-server と --proxy-bypass-list=<-loopback> を付け、localhost もプロキシを通す）。
            "server" は必須、"bypass" は受け付けない（強制のプロキシを抜ける穴になるため。check_proxy）。設定したときは WebRTC の UDP も
            プロキシの外へ出さない（--webrtc-ip-handling-policy=disable_non_proxied_udp）
    """

    display: str = ":99"
    width: int = 1280
    height: int = 720
    framerate: int = 15
    bitrate: str = "500k"
    maxrate: str = "800k"
    bufsize: str = "500k"
    gop_size: int = 10
    draw_mouse: bool = False
    proxy: dict[str, str] | None = None

    def __post_init__(self) -> None:
        if self.proxy is not None:
            self.proxy = check_proxy(self.proxy)  # 呼び出し元の dict を後から変えても、起動した設定と食い違わないように写す


def check_proxy(proxy: object) -> dict[str, str]:
    """強制のプロキシとして使える形か確かめて写しを返す。server（空でない文字列）は必須、bypass は拒む。

    Playwright は環境変数 PLAYWRIGHT_DISABLE_FORCED_CHROMIUM_PROXIED_LOOPBACK があると <-loopback> を付けず、localhost が
    プロキシの外へ出るので、そのときも拒む。起動の直前にも呼ぶ（StreamConfig は変更できるため）。"""
    if not isinstance(proxy, dict) or not isinstance(proxy.get("server"), str) or not proxy["server"]:
        raise ValueError("StreamConfig.proxy は server（空でない文字列）を持つ dict にしてください")
    if "bypass" in proxy:
        raise ValueError("StreamConfig.proxy の bypass は使えません（強制のプロキシを抜ける穴になるため）")
    if os.environ.get("PLAYWRIGHT_DISABLE_FORCED_CHROMIUM_PROXIED_LOOPBACK"):
        raise ValueError("PLAYWRIGHT_DISABLE_FORCED_CHROMIUM_PROXIED_LOOPBACK があると localhost がプロキシを通らないので、"
                         "proxy は使えません")
    return dict(proxy)
