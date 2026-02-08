"""StreamConfig のテスト."""

from web_screen_stream.config import StreamConfig


def test_stream_config_defaults():
    """デフォルト設定が正しいことを確認."""
    config = StreamConfig()
    assert config.display == ":99"
    assert config.width == 1280
    assert config.height == 720
    assert config.framerate == 5
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
