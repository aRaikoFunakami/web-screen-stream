"""FocusRelay の実環境（実 Xvfb + 実 Fluxbox）統合テスト.

Issue #2 の核心（Fluxbox の focus-stealing 防止 → pager ソースでの
activate 救済）は WM の実挙動に依存するため、モックだけでは検証できない。
Chromium/Playwright は使わず、WM_CLASS="Chromium" を名乗る素の X ウィンドウ
で代用する（対象は WM 挙動であり、ブラウザ本体は無関係なため）。

`Xvfb` / `fluxbox` が無い環境（このリポジトリの通常の開発マシン等）では
自動的に skip する。`docker compose exec server uv run pytest` 等、両方が
揃った環境（本番と同じ Dockerfile 由来）でのみ実行される。
"""

import asyncio
import shutil
import subprocess
import time

import pytest
from Xlib import X
from Xlib.display import Display
from Xlib.protocol import event as xevent

from web_screen_stream.focus_relay import FocusRelay, render_fluxbox_apps_config

pytestmark = pytest.mark.skipif(
    not (shutil.which("Xvfb") and shutil.which("fluxbox")),
    reason="Xvfb/fluxbox が無い環境では実環境統合テストを skip する",
)

TEST_DISPLAY = ":97"


def _wait_display_ready(display: str) -> None:
    for _ in range(50):
        time.sleep(0.1)
        if (
            subprocess.run(
                ["xdpyinfo", "-display", display], capture_output=True
            ).returncode
            == 0
        ):
            return
    raise RuntimeError(f"Xvfb did not become ready on {display}")


def _make_window(d: Display, wm_class: tuple[str, str], x: int):
    root = d.screen().root
    win = root.create_window(
        x, 0, 200, 200, 0, d.screen().root_depth, X.InputOutput, X.CopyFromParent
    )
    win.set_wm_class(*wm_class)
    win.set_wm_name(f"test-{wm_class[1]}")
    win.map()
    d.sync()
    return win


def _get_active_window(root, active_atom: int) -> int | None:
    prop = root.get_full_property(active_atom, X.AnyPropertyType)
    if not prop or not prop.value:
        return None
    return prop.value[0]


@pytest.fixture
def xvfb_fluxbox(tmp_path):
    """本番と同じ apps ルールで実 Xvfb + 実 Fluxbox を起動する."""
    xvfb = subprocess.Popen(
        ["Xvfb", TEST_DISPLAY, "-screen", "0", "320x240x24", "-ac"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    _wait_display_ready(TEST_DISPLAY)

    fluxbox_home = tmp_path / "fluxbox_home"
    (fluxbox_home / ".fluxbox").mkdir(parents=True)
    (fluxbox_home / ".fluxbox" / "init").write_text(
        "session.screen0.defaultDeco: NONE\n"
        "session.screen0.focusModel: ClickFocus\n"
    )
    # xvfb.py の allocate() と同じ生成元を使い、本番の apps ルールと
    # このテストのスコープがドリフトしないようにする。
    (fluxbox_home / ".fluxbox" / "apps").write_text(render_fluxbox_apps_config())
    fluxbox = subprocess.Popen(
        ["fluxbox", "-display", TEST_DISPLAY],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        env={"DISPLAY": TEST_DISPLAY, "HOME": str(fluxbox_home)},
    )
    time.sleep(1.0)

    yield TEST_DISPLAY

    fluxbox.terminate()
    xvfb.terminate()
    try:
        fluxbox.wait(timeout=5)
        xvfb.wait(timeout=5)
    except subprocess.TimeoutExpired:
        fluxbox.kill()
        xvfb.kill()


@pytest.mark.asyncio
async def test_demands_attention_activates_chromium_window_via_pager_source(
    xvfb_fluxbox,
):
    """issue #2 の受け入れ条件の核心: DEMANDS_ATTENTION → 実際に前面化される."""
    display = xvfb_fluxbox
    d = Display(display)
    root = d.screen().root
    net_wm_state = d.intern_atom("_NET_WM_STATE")
    net_demands_attention = d.intern_atom("_NET_WM_STATE_DEMANDS_ATTENTION")
    net_active_window = d.intern_atom("_NET_ACTIVE_WINDOW")
    atom_type = d.get_atom("ATOM")

    # Fluxbox が各 map を処理し終わる猶予を挟む（本番では popup 出現に
    # 秒単位の間隔があるため、詰めて作ること自体に意味がない）
    win_a = _make_window(d, ("chromium", "Chromium"), 0)
    time.sleep(0.5)
    win_b = _make_window(d, ("chromium", "Chromium"), 200)
    time.sleep(0.5)
    win_c = _make_window(d, ("xmessage", "Xmessage"), 400)  # 非 Chromium 対照
    time.sleep(0.5)

    # A を最初にアクティブにしておく
    ev = xevent.ClientMessage(
        window=win_a,
        client_type=net_active_window,
        data=(32, [2, X.CurrentTime, 0, 0, 0]),
    )
    root.send_event(
        ev, event_mask=X.SubstructureRedirectMask | X.SubstructureNotifyMask
    )
    d.flush()
    time.sleep(0.3)
    assert _get_active_window(root, net_active_window) == win_a.id

    # B・C に DEMANDS_ATTENTION を立てる（Fluxbox が拒否した際の実測状態を模擬）
    for w in (win_b, win_c):
        w.change_property(net_wm_state, atom_type, 32, [net_demands_attention])
    d.flush()
    d.sync()
    # 注意: d を close しない。X のデフォルト close-down モードでは
    # クライアント切断時にそのクライアントが作った全リソースが破棄される。

    relay = FocusRelay(display)
    await relay.start()
    try:
        activated_to_b = False
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            await asyncio.sleep(0.1)
            if _get_active_window(root, net_active_window) == win_b.id:
                activated_to_b = True
                break

        assert activated_to_b, (
            "Chromium ウィンドウ B が DEMANDS_ATTENTION 検知後に "
            "pager ソースで前面化されなかった"
        )
        assert _get_active_window(root, net_active_window) != win_c.id, (
            "非 Chromium (xmessage) ウィンドウ C が誤って前面化された"
        )
    finally:
        await relay.stop()
        d.close()
