"""FocusRelay のテスト.

python-xlib は MagicMock でスタブし、実 X サーバー無しでロジックを検証する。
(Issue: https://github.com/aRaikoFunakami/web-screen-stream/issues/2)
"""

import asyncio
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from Xlib import X
from Xlib.error import BadWindow, ConnectionClosedError

from web_screen_stream import focus_relay as focus_relay_module
from web_screen_stream.focus_relay import FocusRelay

ATOMS = {
    "_NET_CLIENT_LIST": 10,
    "_NET_WM_STATE": 11,
    "_NET_WM_STATE_DEMANDS_ATTENTION": 12,
    "_NET_ACTIVE_WINDOW": 13,
}


def make_relay() -> FocusRelay:
    """start() を経由せず、接続済み状態を直接組み立てる."""
    relay = FocusRelay(":100")
    relay._display = MagicMock()
    relay._root = MagicMock()
    relay._root.id = 1
    relay._atoms = dict(ATOMS)
    relay._loop = MagicMock()
    relay._fd = 42
    return relay


def make_window(wid: int, wm_class=None, states: list[int] | None = None) -> MagicMock:
    win = MagicMock()
    win.id = wid
    win.get_wm_class.return_value = wm_class
    win.get_full_property.return_value = (
        SimpleNamespace(value=states) if states is not None else None
    )
    return win


def property_notify(window, atom: int) -> SimpleNamespace:
    return SimpleNamespace(type=X.PropertyNotify, window=window, atom=atom)


def _bad_window() -> BadWindow:
    """__init__ が生バイナリの parse を要求するため、__new__ で素通しする."""
    err = BadWindow.__new__(BadWindow)
    err._data = {
        "code": 3,
        "resource_id": 0,
        "sequence_number": 0,
        "major_opcode": 0,
        "minor_opcode": 0,
    }
    return err


# ============================================================
# WM_CLASS 判定
# ============================================================


class TestIsChromium:
    def test_chromium_class_matches(self):
        relay = make_relay()
        win = make_window(1, wm_class=("chromium", "Chromium"))
        assert relay._is_chromium(win) is True

    def test_chromium_derivative_class_matches(self):
        relay = make_relay()
        win = make_window(1, wm_class=("chromium-browser", "Chromium-browser"))
        assert relay._is_chromium(win) is True

    def test_non_chromium_class_rejected(self):
        relay = make_relay()
        win = make_window(1, wm_class=("xmessage", "Xmessage"))
        assert relay._is_chromium(win) is False

    def test_matches_class_not_instance(self):
        """WM_CLASS の instance 側が Chromium でも class 側で判定する."""
        relay = make_relay()
        win = make_window(1, wm_class=("Chromium", "Xmessage"))
        assert relay._is_chromium(win) is False

    def test_no_wm_class_rejected(self):
        relay = make_relay()
        win = make_window(1, wm_class=None)
        assert relay._is_chromium(win) is False

    def test_bad_window_rejected(self):
        relay = make_relay()
        win = make_window(1)
        win.get_wm_class.side_effect = _bad_window()
        assert relay._is_chromium(win) is False

    def test_non_xerror_from_get_wm_class_does_not_propagate(self):
        """WM_CLASS が UTF8_STRING型かつ不正バイト列だと python-xlib は
        UnicodeDecodeError を送出する（XError のサブクラスではない）。
        ここで拾い損ねると、無関係な1ウィンドウのせいで
        _on_readable まで例外が伝播し reader が外れ、リレー機能全体が
        セッションの残り全期間停止してしまう（コードレビューで確認済み）。"""
        relay = make_relay()
        win = make_window(1)
        win.get_wm_class.side_effect = UnicodeDecodeError(
            "utf-8", b"\xff", 0, 1, "invalid start byte"
        )
        assert relay._is_chromium(win) is False


# ============================================================
# DEMANDS_ATTENTION 検知 → activate
# ============================================================


class TestMaybeActivate:
    def test_chromium_with_demands_attention_activates(self):
        relay = make_relay()
        win = make_window(
            5,
            wm_class=("chromium", "Chromium"),
            states=[ATOMS["_NET_WM_STATE_DEMANDS_ATTENTION"]],
        )
        relay._maybe_activate(win)
        relay._root.send_event.assert_called_once()
        relay._display.flush.assert_called_once()

    def test_non_chromium_with_demands_attention_not_activated(self):
        """issue の受け入れ条件: xmessage 等の他プロセスに誤反応しない."""
        relay = make_relay()
        win = make_window(
            5,
            wm_class=("xmessage", "Xmessage"),
            states=[ATOMS["_NET_WM_STATE_DEMANDS_ATTENTION"]],
        )
        relay._maybe_activate(win)
        relay._root.send_event.assert_not_called()

    def test_chromium_without_demands_attention_not_activated(self):
        relay = make_relay()
        win = make_window(5, wm_class=("chromium", "Chromium"), states=[])
        relay._maybe_activate(win)
        relay._root.send_event.assert_not_called()

    def test_no_wm_state_property_not_activated(self):
        relay = make_relay()
        win = make_window(5, wm_class=("chromium", "Chromium"), states=None)
        relay._maybe_activate(win)
        relay._root.send_event.assert_not_called()

    def test_get_full_property_bad_window_swallowed(self):
        relay = make_relay()
        win = make_window(5, wm_class=("chromium", "Chromium"))
        win.get_full_property.side_effect = _bad_window()
        relay._maybe_activate(win)  # 例外を送出しないこと
        relay._root.send_event.assert_not_called()


# ============================================================
# _activate: pager ソース (source indication = 2) の検証
# ============================================================


class TestActivate:
    def test_sends_client_message_with_pager_source(self):
        relay = make_relay()
        win = make_window(0xABCD, wm_class=("chromium", "Chromium"))

        relay._activate(win)

        relay._root.send_event.assert_called_once()
        sent_event, kwargs = relay._root.send_event.call_args
        event = sent_event[0]
        assert event.client_type == ATOMS["_NET_ACTIVE_WINDOW"]
        source_indication = event.data[1][0]
        assert source_indication == 2, (
            "Fluxbox の focus-stealing 防止を無条件で回避できるのは "
            "pager ソース(=2) のみ（issue #2 の実測事実）"
        )
        assert kwargs["event_mask"] == (
            X.SubstructureRedirectMask | X.SubstructureNotifyMask
        )
        relay._display.flush.assert_called_once()

    def test_send_event_error_does_not_raise(self):
        relay = make_relay()
        win = make_window(1, wm_class=("chromium", "Chromium"))
        relay._root.send_event.side_effect = _bad_window()
        relay._activate(win)  # 例外を送出しないこと


# ============================================================
# _NET_CLIENT_LIST 監視・新規ウィンドウの取り込み
# ============================================================


class TestRefreshClientList:
    def test_new_window_gets_watched_and_subscribed(self):
        relay = make_relay()
        win = make_window(7, wm_class=("chromium", "Chromium"), states=[])
        relay._display.create_resource_object.return_value = win
        relay._root.get_full_property.return_value = SimpleNamespace(value=[7])

        relay._refresh_client_list()

        assert 7 in relay._watched
        win.change_attributes.assert_called_once_with(
            event_mask=X.PropertyChangeMask
        )

    def test_already_demanding_attention_activated_on_first_scan(self):
        """監視開始が遅れて既に DEMANDS_ATTENTION が立っていた場合の救済."""
        relay = make_relay()
        win = make_window(
            7,
            wm_class=("chromium", "Chromium"),
            states=[ATOMS["_NET_WM_STATE_DEMANDS_ATTENTION"]],
        )
        relay._display.create_resource_object.return_value = win
        relay._root.get_full_property.return_value = SimpleNamespace(value=[7])

        relay._refresh_client_list()

        relay._root.send_event.assert_called_once()

    def test_destroyed_window_pruned_from_watched(self):
        relay = make_relay()
        relay._watched = {7, 8}
        relay._root.get_full_property.return_value = SimpleNamespace(value=[7])

        relay._refresh_client_list()

        assert relay._watched == {7}

    def test_no_client_list_property_treated_as_empty(self):
        relay = make_relay()
        relay._root.get_full_property.return_value = None
        relay._refresh_client_list()  # 例外を送出しないこと
        assert relay._watched == set()

    def test_window_destroyed_before_subscribe_skipped(self):
        relay = make_relay()
        win = make_window(7)
        win.change_attributes.side_effect = _bad_window()
        relay._display.create_resource_object.return_value = win
        relay._root.get_full_property.return_value = SimpleNamespace(value=[7])

        relay._refresh_client_list()  # 例外を送出しないこと

        assert 7 not in relay._watched


# ============================================================
# PropertyNotify ディスパッチ
# ============================================================


class TestHandlePropertyNotify:
    """event.atom は整数 ID として直接比較する（get_atom_name() は使わない:
    毎回 X サーバーへの同期往復が発生する上、BadAtom 発生時に
    _on_readable のドレインループ全体を中断させてしまうため）。"""

    def test_root_client_list_change_triggers_refresh(self):
        relay = make_relay()
        relay._refresh_client_list = MagicMock()

        relay._handle_property_notify(
            property_notify(relay._root, ATOMS["_NET_CLIENT_LIST"])
        )

        relay._refresh_client_list.assert_called_once()

    def test_window_wm_state_change_triggers_maybe_activate(self):
        relay = make_relay()
        relay._maybe_activate = MagicMock()
        win = make_window(5)

        relay._handle_property_notify(
            property_notify(win, ATOMS["_NET_WM_STATE"])
        )

        relay._maybe_activate.assert_called_once_with(win)

    def test_unrelated_atom_ignored(self):
        relay = make_relay()
        relay._maybe_activate = MagicMock()
        relay._refresh_client_list = MagicMock()
        win = make_window(5)

        relay._handle_property_notify(property_notify(win, 999))  # 未知の atom

        relay._maybe_activate.assert_not_called()
        relay._refresh_client_list.assert_not_called()

    def test_no_get_atom_name_round_trip(self):
        """get_atom_name() への同期往復が発生しないことを確認."""
        relay = make_relay()
        relay._refresh_client_list = MagicMock()

        relay._handle_property_notify(
            property_notify(relay._root, ATOMS["_NET_CLIENT_LIST"])
        )

        relay._display.get_atom_name.assert_not_called()


# ============================================================
# asyncio reader コールバック（イベントドレイン・ビジーループ防止）
# ============================================================


class TestOnReadable:
    def test_drains_all_pending_events(self):
        """1回の readable で複数イベントが溜まっていても全て処理する."""
        relay = make_relay()
        relay._handle_property_notify = MagicMock()
        win = make_window(5)
        events = [
            property_notify(win, 1),
            property_notify(win, 2),
            property_notify(win, 3),
        ]
        relay._display.pending_events.side_effect = [3, 2, 1, 0]
        relay._display.next_event.side_effect = events

        relay._on_readable()

        assert relay._handle_property_notify.call_count == 3

    def test_non_property_notify_events_ignored(self):
        relay = make_relay()
        relay._handle_property_notify = MagicMock()
        other_event = SimpleNamespace(type=X.CreateNotify)
        relay._display.pending_events.side_effect = [1, 0]
        relay._display.next_event.side_effect = [other_event]

        relay._on_readable()

        relay._handle_property_notify.assert_not_called()

    def test_connection_closed_detaches_reader(self):
        relay = make_relay()
        relay._display.pending_events.side_effect = ConnectionClosedError(":100")

        relay._on_readable()

        relay._loop.remove_reader.assert_called_once_with(42)
        assert relay._fd is None

    def test_unexpected_exception_detaches_reader_no_busy_loop(self):
        """想定外の例外でも reader を外す（無限コールバック＝ビジーループの防止）."""
        relay = make_relay()
        relay._display.pending_events.side_effect = RuntimeError("boom")

        relay._on_readable()

        relay._loop.remove_reader.assert_called_once_with(42)
        assert relay._fd is None


# ============================================================
# start / stop ライフサイクル
# ============================================================


class TestLifecycle:
    @pytest.mark.asyncio
    async def test_stop_removes_reader_and_closes_display(self):
        relay = make_relay()

        await relay.stop()

        relay._loop.remove_reader.assert_called_once_with(42)
        assert relay._display is None
        assert relay._root is None
        assert relay._fd is None

    @pytest.mark.asyncio
    async def test_stop_is_idempotent(self):
        relay = make_relay()
        await relay.stop()
        await relay.stop()  # 例外を送出しないこと

    @pytest.mark.asyncio
    async def test_stop_without_start_is_noop(self):
        relay = FocusRelay(":100")
        await relay.stop()  # 例外を送出しないこと

    @pytest.mark.asyncio
    async def test_stop_close_error_is_logged_not_raised(self):
        relay = make_relay()
        relay._display.close.side_effect = RuntimeError("close failed")

        await relay.stop()  # 例外を送出しないこと

        assert relay._display is None

    @pytest.mark.asyncio
    async def test_start_failure_after_partial_connect_closes_display(self):
        """_connect() が Display() 確立後に例外を送出した場合でも、
        start() は確立済みの接続を必ず閉じてから例外を再送出する
        （fd リーク防止。コードレビューで確認済み）。"""
        relay = FocusRelay(":100")
        opened_display = MagicMock()

        def fake_connect():
            # Display() 確立には成功し、その後の atom intern 等で失敗する
            # という実際の部分失敗パターンを再現する。
            relay._display = opened_display
            raise RuntimeError("intern_atom failed")

        relay._connect = fake_connect

        with pytest.raises(RuntimeError):
            await relay.start()

        opened_display.close.assert_called_once()
        assert relay._display is None

    @pytest.mark.asyncio
    async def test_start_timeout_closes_partial_connection(self):
        """X サーバー不応答で接続確立が固まった場合、無期限に待たず
        タイムアウトし、呼び出し元のロックを握り続けない
        （コードレビューで確認済み）。"""
        relay = FocusRelay(":100")

        def fake_connect():
            import time

            time.sleep(0.5)

        relay._connect = fake_connect

        original_timeout = focus_relay_module._CONNECT_TIMEOUT
        focus_relay_module._CONNECT_TIMEOUT = 0.05
        try:
            with pytest.raises(asyncio.TimeoutError):
                await relay.start()
        finally:
            focus_relay_module._CONNECT_TIMEOUT = original_timeout
