"""操作チャネルの制御メッセージ検証(X 不要)と、実 Xvfb への XTEST 注入の統合テスト.

統合テストは Xvfb が無い環境では skip する(docker の server コンテナ等で実行)。
"""

import shutil
import subprocess
import time

import pytest

from web_screen_stream.input import ControlError, XTestInjector, parse_control_message


@pytest.mark.parametrize("msg", [
    {"type": "pointer", "action": "down", "x": 0.5, "y": 0},
    {"type": "pointer", "action": "up", "x": 1, "y": 1.0},
    {"type": "wheel", "x": 0.1, "y": 0.2, "dy": -3},
    {"type": "key", "key": "Enter"},
    {"type": "text", "text": "テスト abc"},
])
def test_valid_messages_are_normalized(msg):
    assert parse_control_message(msg)["type"] == msg["type"]


@pytest.mark.parametrize("msg", [
    None, [], {"type": "nope"},
    {"type": "pointer", "action": "down", "x": 1.5, "y": 0},
    {"type": "pointer", "action": "drag", "x": 0, "y": 0},
    {"type": "pointer", "action": "down", "x": True, "y": 0},
    {"type": "wheel", "x": 0, "y": 0, "dy": 11},
    {"type": "key", "key": "F13"},
    {"type": "text", "text": ""},
    {"type": "text", "text": "a" * 257},
])
def test_invalid_messages_are_rejected(msg):
    with pytest.raises(ControlError):
        parse_control_message(msg)


@pytest.fixture
def xvfb():
    if not shutil.which("Xvfb"):
        pytest.skip("Xvfb not installed")
    proc = subprocess.Popen(["Xvfb", ":91", "-screen", "0", "640x480x24"], stderr=subprocess.DEVNULL)
    time.sleep(1)
    yield ":91"
    proc.terminate()
    proc.wait(timeout=5)


def test_pointer_moves_to_scaled_position(xvfb):
    from Xlib.display import Display
    inj = XTestInjector(xvfb, 640, 480)
    inj.pointer("move", 0.5, 0.25)
    pos = Display(xvfb).screen().root.query_pointer()
    inj.close()
    assert (pos.root_x, pos.root_y) == (320, 120)


def test_text_delivers_key_events_and_restores_spare_keycode(xvfb):
    from Xlib import X
    from Xlib.display import Display
    d = Display(xvfb)
    win = d.screen().root.create_window(0, 0, 640, 480, 0, d.screen().root_depth, event_mask=X.KeyPressMask)
    win.map()
    d.sync()
    time.sleep(0.2)
    win.set_input_focus(X.RevertToParent, X.CurrentTime)
    d.sync()
    first = d.display.info.min_keycode
    before = d.get_keyboard_mapping(first, d.display.info.max_keycode - first + 1)

    inj = XTestInjector(xvfb, 640, 480)
    inj.text("aB日")  # a=通常, B=Shift, 日=空きキーコードへの一時割り当て
    inj.close()

    presses = []
    deadline = time.time() + 2
    while time.time() < deadline and len(presses) < 4:
        if d.pending_events():
            ev = d.next_event()
            if ev.type == X.KeyPress:
                presses.append(ev.detail)
        else:
            time.sleep(0.02)
    after = d.get_keyboard_mapping(first, d.display.info.max_keycode - first + 1)
    assert len(presses) >= 4  # a, Shift, b, 日(空きキーコード)
    assert [list(r) for r in after] == [list(r) for r in before]  # 一時割り当ては元に戻っている


@pytest.mark.parametrize("draw, expected", [(False, "0"), (True, "1")])
def test_draw_mouse_switches_ffmpeg_cursor_capture(draw, expected):
    from web_screen_stream.config import StreamConfig
    from web_screen_stream.ffmpeg_source import FFmpegSource
    cmd = FFmpegSource(StreamConfig(draw_mouse=draw))._build_command()
    assert cmd[cmd.index("-draw_mouse") + 1] == expected


def test_japanese_reaches_chromium_via_remapped_keycodes(xvfb):
    """空きキーコードへの一時割り当てで打った日本語が、実際の Chromium の入力欄に入る(待ちが無いと最初の文字が繰り返される)."""
    pytest.importorskip("playwright")
    from playwright.sync_api import sync_playwright
    with sync_playwright() as pw:
        b = pw.chromium.launch(headless=False, env={"DISPLAY": xvfb}, args=["--window-position=0,0", "--window-size=640,480"])
        page = b.new_page()
        page.set_content("<input id=i autofocus>")
        page.click("#i")
        inj = XTestInjector(xvfb, 640, 480)
        inj.text("日本語ab")
        inj.close()
        time.sleep(0.5)
        value = page.input_value("#i")
        b.close()
    assert value == "日本語ab"
