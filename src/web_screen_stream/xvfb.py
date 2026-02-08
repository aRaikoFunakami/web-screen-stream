"""Xvfb ディスプレイ管理ユーティリティ."""

import logging
import os

logger = logging.getLogger(__name__)


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
