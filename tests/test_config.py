"""StreamConfig のテスト."""

from web_screen_stream.config import StreamConfig


def test_stream_config_defaults():
    """デフォルト設定が正しいことを確認."""
    config = StreamConfig()
    assert config.display == ":99"
    assert config.width == 1280
    assert config.height == 720
    assert config.framerate == 15
    assert config.bitrate == "500k"
    assert config.gop_size == 10


def test_stream_config_custom():
    """カスタム設定が反映されることを確認."""
    config = StreamConfig(
        display=":100",
        width=1920,
        height=1080,
        framerate=10,
        bitrate="1000k",
    )
    assert config.display == ":100"
    assert config.width == 1920
    assert config.height == 1080
    assert config.framerate == 10
    assert config.bitrate == "1000k"


async def test_chromium_args_are_appended_to_launch(monkeypatch):
    """chromium_args は既定の引数の後ろに付けて Chromium に渡す（既定は何も足さない）。"""
    import playwright.async_api as pa

    from web_screen_stream.session import SessionManager

    launched: list[list[str]] = []

    class _Page:
        async def goto(self, *a, **k):
            return None

    class _Browser:
        async def new_page(self, **k):
            return _Page()

    class _Chromium:
        async def launch(self, headless, args):
            launched.append(args)
            return _Browser()

    class _PW:
        chromium = _Chromium()

    class _Starter:
        async def start(self):
            return _PW()

    monkeypatch.setattr(pa, "async_playwright", lambda: _Starter())
    mgr = SessionManager()
    await mgr._launch_browser("s", StreamConfig(), "about:blank")
    await mgr._launch_browser("s", StreamConfig(chromium_args=("--disable-features=X",)), "about:blank")
    assert "--disable-features=X" not in launched[0]
    assert launched[1][-1] == "--disable-features=X"
    assert launched[1][:-1] == launched[0]
