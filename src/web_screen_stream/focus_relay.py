"""Fluxbox focus-stealing 防止からの救済リレー.

Issue: Fluxbox の `focusRequestFromClient` はフォーカス中のウィンドウが
fullscreen の場合、他ウィンドウの通常の activate 要求を無条件に拒否する
（apps ルールの [FocusProtection]{gain} でも回避不可、実測確認済み）。

回避策（実測確認済み）: `_NET_ACTIVE_WINDOW` を **pager ソース**
(source indication = 2) で送ると、Fluxbox は無条件に focus() + raise()
する。拒否された切替要求の対象ウィンドウには `_NET_WM_STATE_DEMANDS_ATTENTION`
が立つため、これを監視して pager ソースで activate し直せばよい。

参照: https://github.com/aRaikoFunakami/web-screen-stream/issues/2
"""

from __future__ import annotations

import asyncio
import logging
import re

from Xlib import X
from Xlib.display import Display
from Xlib.error import ConnectionClosedError, XError
from Xlib.protocol import event as xevent
from Xlib.xobject.drawable import Window

logger = logging.getLogger(__name__)

# xvfb.py の Fluxbox apps ルール (class=Chromium.*) と同じスコープを
# 共有する。xmessage 等の他プロセスに誤反応しないよう、Chromium
# クライアントのみに限定する。両ファイルはこの定数を通じて同期させる。
CHROMIUM_WM_CLASS_PATTERN = "Chromium.*"
_CHROMIUM_CLASS_RE = re.compile(CHROMIUM_WM_CLASS_PATTERN)


def render_fluxbox_apps_config() -> str:
    """Fluxbox `apps` ファイルの内容を返す（唯一の生成元）.

    XvfbManager.allocate() と統合テストの両方がここを経由することで、
    実際に書き込まれる apps ルールと `_is_chromium()` のスコープが
    ドリフトしないようにする。
    """
    return (
        f"[app] (class={CHROMIUM_WM_CLASS_PATTERN})\n"
        "  [Fullscreen] {yes}\n"
        "  [Deco] {NONE}\n"
        "[end]\n"
    )


# _NET_ACTIVE_WINDOW の source indication: 2 = pager（実測: Fluxbox の
# focus-stealing 防止を無条件で回避できる唯一のソース種別。0/1 は拒否される）
_SOURCE_INDICATION_PAGER = 2

# X 接続確立のタイムアウト。X サーバーが不応答のまま無期限に
# ブロックすると、呼び出し元 (XvfbManager.allocate()) の排他ロックを
# 握ったまま戻らなくなり、他の全セッションの allocate()/release() が
# 巻き込まれて停止する（コードレビューで確認済み）。
_CONNECT_TIMEOUT = 5.0


class FocusRelay:
    """1つの X ディスプレイに対して DEMANDS_ATTENTION → activate を中継する.

    Usage:
        relay = FocusRelay(":100")
        await relay.start()
        ...
        await relay.stop()
    """

    def __init__(self, display: str):
        self._display_name = display
        self._display: Display | None = None
        self._root: Window | None = None
        self._atoms: dict[str, int] = {}
        self._watched: set[int] = set()  # 監視済みウィンドウ id
        self._loop: asyncio.AbstractEventLoop | None = None
        self._fd: int | None = None

    async def start(self) -> None:
        """X 接続を張り、既存クライアントの監視を開始する.

        接続失敗時は例外を送出する（呼び出し側で non-fatal に扱う）。
        """
        self._loop = asyncio.get_running_loop()
        # 接続確立（ソケット open + ハンドシェイク + atom intern の
        # 往復）は同期 I/O のため、イベントループを塞がないよう
        # スレッドに逃がす。以降のイベント処理は add_reader 経由の
        # 非ブロッキング呼び出しのみ（next_event は pending_events()
        # で readable を確認済みの場合だけ呼ぶ）。
        # タイムアウトも設ける: X サーバー不応答時に呼び出し元の
        # ロックを無期限に握り続けないため。
        try:
            await asyncio.wait_for(
                asyncio.to_thread(self._connect), timeout=_CONNECT_TIMEOUT
            )
        except (Exception, asyncio.CancelledError):
            # 部分的に確立済みの接続が残っていれば必ず閉じる
            # （X11 fd リークの防止）。
            await self._close()
            raise

        self._fd = self._display.fileno()
        self._loop.add_reader(self._fd, self._on_readable)
        logger.info("FocusRelay started on %s", self._display_name)

    def _connect(self) -> None:
        """接続確立 + 初回スキャン（別スレッドで実行）."""
        self._display = Display(self._display_name)
        self._root = self._display.screen().root

        for name in (
            "_NET_CLIENT_LIST",
            "_NET_WM_STATE",
            "_NET_WM_STATE_DEMANDS_ATTENTION",
            "_NET_ACTIVE_WINDOW",
        ):
            self._atoms[name] = self._display.intern_atom(name)

        self._root.change_attributes(event_mask=X.PropertyChangeMask)
        self._refresh_client_list()

    async def stop(self) -> None:
        """監視を止めて X 接続を閉じる."""
        await self._close()
        logger.info("FocusRelay stopped on %s", self._display_name)

    async def _close(self) -> None:
        """reader を外し X 接続を閉じる（start() の失敗経路からも共用）."""
        self._detach_reader()
        if self._display is not None:
            try:
                # close() は内部でソケットへの flush を伴いうる同期
                # I/O（相手が応答不能だと事実上ブロックしうる）ため、
                # _connect() と対称に to_thread に逃がす。
                await asyncio.to_thread(self._display.close)
            except Exception:
                logger.exception(
                    "Error closing X display %s", self._display_name
                )
        self._display = None
        self._root = None
        self._watched.clear()

    # ------------------------------------------------------------
    # 内部実装
    # ------------------------------------------------------------

    def _on_readable(self) -> None:
        """asyncio reader コールバック: 溜まっているイベントを処理する."""
        assert self._display is not None
        try:
            while self._display.pending_events():
                event = self._display.next_event()
                if event.type != X.PropertyNotify:
                    continue
                self._handle_property_notify(event)
        except ConnectionClosedError:
            logger.warning(
                "X connection closed on %s, stopping relay", self._display_name
            )
            self._detach_reader()
        except XError:
            logger.exception(
                "Xlib error while processing events on %s", self._display_name
            )
        except Exception:
            # 想定外の例外を reader コールバックから漏らすと、fd が
            # readable のまま reader だけが残り、ビジーループ
            # （無限に呼ばれ続ける）になり得る。AGENTS.md の「終了性」
            # 要件のため、想定外の例外でも reader は必ず外して止める。
            logger.exception(
                "Unexpected error in FocusRelay reader on %s, detaching",
                self._display_name,
            )
            self._detach_reader()

    def _detach_reader(self) -> None:
        if self._loop is not None and self._fd is not None:
            self._loop.remove_reader(self._fd)
            self._fd = None

    def _handle_property_notify(self, event) -> None:
        assert self._root is not None
        # event.atom は既に intern 済みの整数 atom ID なので、
        # self._atoms と直接比較する（get_atom_name() は毎回 X サーバー
        # への同期往復が発生する上、BadAtom を送出しうる = 1回の
        # readable でまとめて届いた後続イベントのドレインが中断される
        # リスクがあった。整数比較ならその往復も例外経路も無い）。
        if event.window.id == self._root.id:
            if event.atom == self._atoms["_NET_CLIENT_LIST"]:
                self._refresh_client_list()
            return

        if event.atom == self._atoms["_NET_WM_STATE"]:
            self._maybe_activate(event.window)

    def _refresh_client_list(self) -> None:
        """_NET_CLIENT_LIST を読み直し、新規ウィンドウを監視対象に加える."""
        assert self._display is not None and self._root is not None
        try:
            prop = self._root.get_full_property(
                self._atoms["_NET_CLIENT_LIST"], X.AnyPropertyType
            )
        except XError:
            logger.exception(
                "Failed to read _NET_CLIENT_LIST on %s", self._display_name
            )
            return

        window_ids = list(prop.value) if prop else []

        # 閉じられたウィンドウを追跡集合から外す（無制限増加の防止）
        self._watched.intersection_update(window_ids)

        for wid in window_ids:
            if wid in self._watched:
                continue
            win = self._display.create_resource_object("window", wid)
            try:
                win.change_attributes(event_mask=X.PropertyChangeMask)
            except XError:
                # マップ直後に破棄されたウィンドウ等、無害なので継続
                continue
            self._watched.add(wid)
            # 監視開始が遅れて既に DEMANDS_ATTENTION が立っている場合を救済
            self._maybe_activate(win)

    def _maybe_activate(self, window: Window) -> None:
        try:
            prop = window.get_full_property(
                self._atoms["_NET_WM_STATE"], X.AnyPropertyType
            )
        except XError:
            return

        states = list(prop.value) if prop else []
        if self._atoms["_NET_WM_STATE_DEMANDS_ATTENTION"] not in states:
            return

        if not self._is_chromium(window):
            return

        self._activate(window)

    def _is_chromium(self, window: Window) -> bool:
        try:
            wm_class = window.get_wm_class()
        except Exception:
            # XError（BadWindow等）に加え、WM_CLASS が UTF8_STRING型
            # かつ不正バイト列だった場合 python-xlib は UnicodeDecodeError
            # を送出する（XError のサブクラスではない）。ここで拾い損ねると
            # 呼び出し元の _on_readable まで伝播し、無関係な1ウィンドウの
            # せいでリレー機能全体が停止してしまう（コードレビューで確認済
            # み）。「判定できない = Chromium ではない」として扱えば十分。
            return False
        if not wm_class:
            return False
        _, cls = wm_class
        return bool(cls and _CHROMIUM_CLASS_RE.match(cls))

    def _activate(self, window: Window) -> None:
        """`_NET_ACTIVE_WINDOW` を pager ソース(=2)で送出し前面化させる."""
        assert self._display is not None and self._root is not None
        try:
            event = xevent.ClientMessage(
                window=window,
                client_type=self._atoms["_NET_ACTIVE_WINDOW"],
                data=(32, [_SOURCE_INDICATION_PAGER, X.CurrentTime, 0, 0, 0]),
            )
            mask = X.SubstructureRedirectMask | X.SubstructureNotifyMask
            self._root.send_event(event, event_mask=mask)
            self._display.flush()
            logger.info(
                "FocusRelay: activated window 0x%x on %s (pager source)",
                window.id,
                self._display_name,
            )
        except XError:
            logger.exception(
                "Failed to activate window on %s", self._display_name
            )
