"""操作チャネル: ブラウザ(クライアント)からのマウス・キー・文字入力を、配信中の Xvfb へ XTEST で注入する.

配信(H.264)は受信専用のまま、操作は別の WebSocket(制御チャネル)で受ける。閲覧だけの人と操作する人を
分けて許可でき、既存の配信クライアントには影響しない。

制御メッセージ(JSON, client → server)。Android 版(android-screen-stream)と同じ形:
  {"type": "pointer", "action": "down"|"move"|"up", "x": 0..1, "y": 0..1}   # 表示中の映像に対する比率
  {"type": "wheel", "x": 0..1, "y": 0..1, "dy": -10..10}                      # dy>0 で下へ
  {"type": "key", "key": "Enter"|"Backspace"|"Tab"|"Escape"|"ArrowLeft"|... }
  {"type": "text", "text": "..."}                                           # 確定済みの文字列(IME の確定後)
応答(server → client): {"type": "ack", "seq": <受け取った seq>, "ok": true} / {"type": "error", "seq": ..., "error": "..."}

Playwright には依存しない(ブラウザを誰が起動したかに関係なく、X サーバへの入力として届く)。
キーボード配列に無い文字(日本語など)は、空きキーコードへ一時的に Unicode keysym を割り当てて押す
(xdotool と同じ方式。加えて XKB の配列を読み直させる)。入力した文字の内容はログに出さない(資格情報を打つ用途があるため)。
"""

from __future__ import annotations

import asyncio
import logging
import re
import subprocess
import threading
import time

from Xlib import X, XK
from Xlib.display import Display
from Xlib.ext import xtest

logger = logging.getLogger(__name__)

MAX_TEXT_LEN = 256  # 1 メッセージで受ける文字数の上限(貼り付けの暴走を防ぐ)
# 空きキーコードへ割り当てて配列を読み直させた後・戻す前の待ち(クライアントが配列の変更を取り込む前に押さないため)
REMAP_SETTLE_SEC = 0.05

_KEYS = {
    "Enter": "Return",
    "Backspace": "BackSpace",
    "Tab": "Tab",
    "Escape": "Escape",
    "Delete": "Delete",
    "ArrowLeft": "Left",
    "ArrowRight": "Right",
    "ArrowUp": "Up",
    "ArrowDown": "Down",
    "Home": "Home",
    "End": "End",
}


_CONTROL_KEYSYMS = {"\n": XK.XK_Return, "\t": XK.XK_Tab}  # 貼り付けた改行・タブはキーとして押す


class ControlError(ValueError):
    """制御メッセージが不正(型・範囲・未知の種別)."""


def _normalize_text(text: str) -> str:
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    if any((ord(c) < 0x20 and c not in "\n\t") or 0x7F <= ord(c) <= 0x9F for c in text):
        raise ControlError("text must not contain control characters other than newline and tab")
    return text


def parse_control_message(msg: object) -> dict:
    """制御メッセージを検証して正規化する(X には触れない。Android 版と同じ規則)."""
    if not isinstance(msg, dict):
        raise ControlError("message must be an object")
    kind = msg.get("type")
    if kind in ("pointer", "wheel"):
        x, y = msg.get("x"), msg.get("y")
        if not all(isinstance(v, (int, float)) and not isinstance(v, bool) and 0.0 <= v <= 1.0 for v in (x, y)):
            raise ControlError("x/y must be numbers in 0..1")
        if kind == "pointer":
            if msg.get("action") not in ("down", "move", "up"):
                raise ControlError("action must be down/move/up")
            return {"type": kind, "action": msg["action"], "x": float(x), "y": float(y)}
        dy = msg.get("dy")
        if not isinstance(dy, int) or isinstance(dy, bool) or not -10 <= dy <= 10:
            raise ControlError("dy must be an int in -10..10")
        return {"type": kind, "x": float(x), "y": float(y), "dy": dy}
    if kind == "key":
        if msg.get("key") not in _KEYS:
            raise ControlError(f"unsupported key: {msg.get('key')!r}")
        return {"type": kind, "key": msg["key"]}
    if kind == "text":
        text = msg.get("text")
        if not isinstance(text, str) or not text or len(text) > MAX_TEXT_LEN:
            raise ControlError(f"text must be a non-empty string up to {MAX_TEXT_LEN} chars")
        return {"type": kind, "text": _normalize_text(text)}
    raise ControlError(f"unknown type: {kind!r}")


class XTestInjector:
    """1 つの X ディスプレイへ XTEST で入力する. スレッド安全(内部ロックで直列化)."""

    def __init__(self, display: str, width: int, height: int):
        try:
            self._d = Display(display)
        except Exception as e:  # Xlib の接続エラー類を呼び出し側が扱える RuntimeError にそろえる
            raise RuntimeError(f"cannot connect to X display {display}: {e}") from e
        if not self._d.has_extension("XTEST"):
            self._d.close()
            raise RuntimeError(f"XTEST extension is not available on {display}")
        self._display = display
        self._button_down = False
        self._named: set[int] | None = None  # XKB で名前のあるキーコード(初回の日本語入力時に取る)
        self._w, self._h = width, height
        self._lock = threading.Lock()
        self._shift = self._d.keysym_to_keycode(XK.XK_Shift_L)

    def close(self) -> None:
        with self._lock:
            try:
                if self._button_down:  # 押したまま切断されたら離す(次の操作がドラッグにならないように)
                    xtest.fake_input(self._d, X.ButtonRelease, 1)
                    self._sync()
            finally:
                self._d.close()

    def _sync(self) -> None:
        """要求を流し、届いたイベント(配列の付け替えで来る MappingNotify 等)を捨てる. 読まないとキューが溜まり続ける."""
        self._d.sync()
        while self._d.pending_events():
            self._d.next_event()

    def _xy(self, nx: float, ny: float) -> tuple[int, int]:
        return min(int(nx * self._w), self._w - 1), min(int(ny * self._h), self._h - 1)

    def pointer(self, action: str, nx: float, ny: float) -> None:
        x, y = self._xy(nx, ny)
        with self._lock:
            xtest.fake_input(self._d, X.MotionNotify, x=x, y=y)
            if action == "down":
                xtest.fake_input(self._d, X.ButtonPress, 1)
            elif action == "up":
                xtest.fake_input(self._d, X.ButtonRelease, 1)
            self._button_down = (action == "down") or (self._button_down and action == "move")
            self._sync()

    def wheel(self, nx: float, ny: float, dy: int) -> None:
        x, y = self._xy(nx, ny)
        button = 5 if dy > 0 else 4
        with self._lock:
            xtest.fake_input(self._d, X.MotionNotify, x=x, y=y)
            for _ in range(abs(dy)):
                xtest.fake_input(self._d, X.ButtonPress, button)
                xtest.fake_input(self._d, X.ButtonRelease, button)
            self._sync()

    def key(self, name: str) -> None:
        code = self._d.keysym_to_keycode(XK.string_to_keysym(_KEYS[name]))
        with self._lock:
            self._tap(code, False)

    def text(self, text: str) -> None:
        with self._lock:
            first = self._d.display.info.min_keycode
            mapping = self._d.get_keyboard_mapping(first, self._d.display.info.max_keycode - first + 1)
            empty = {first + i for i, syms in enumerate(mapping) if not any(syms)}
            needs_spare = any(not self._base_key(_keysym(ch), empty) for ch in text)
            named = self._named_keycodes() if needs_spare else set()
            # xkbcomp は XKB で名前の無いキーコードを書き戻せないので、名前のある空きキーコードだけを使う
            spares = sorted(empty & named)
            if needs_spare and not spares:  # 途中まで打ってから失敗しない(再送で二重に入るのを防ぐ)
                raise RuntimeError("no spare keycode to type this character")
            width = len(mapping[0])
            loaded: dict[int, int] = {}  # keysym → 一時割り当てした空きキーコード
            touched: set[int] = set()  # この呼び出しで割り当てた空きキーコード(全部を戻す)
            try:
                for i, ch in enumerate(text):
                    keysym = _keysym(ch)
                    if keysym not in loaded:
                        base = self._base_key(keysym, empty)
                        if base:
                            self._tap(*base)
                            continue
                        if loaded:  # 前の割り当てで押したキーを取り込ませてから付け替える
                            time.sleep(REMAP_SETTLE_SEC)
                        loaded = self._load_spares(spares, empty, width, text[i:], touched)
                    self._tap(loaded[keysym], False)
            finally:
                if touched:
                    time.sleep(REMAP_SETTLE_SEC)
                    for code in touched:
                        self._d.change_keyboard_mapping(code, [(X.NoSymbol,) * width])
                    self._sync()

    def _base_key(self, keysym: int, empty: set[int]) -> tuple[int, bool] | None:
        """元の配列で Shift なし/ありで打てるキー (keycode, shift). AltGr 等の 3 段目以降は使わない(一時割り当てに回す)."""
        for code, index in self._d.keysym_to_keycodes(keysym):
            if index in (0, 1) and code not in empty:
                return code, index == 1
        return None

    def _load_spares(self, spares: list[int], empty: set[int], width: int, rest: str, touched: set[int]) -> dict[int, int]:
        """配列に無い文字を、これから打つ順に空きキーコードへまとめて割り当て、XKB の配列を読み込み直す.

        Chromium は XKB の配列を一度読むと、コアの ChangeKeyboardMapping(MappingNotify)では読み直さず、
        XkbNewKeyboardNotify でしか更新しない(実測: Chromium 159 で「日本語」が「日日日」になる)。
        そこで xkbcomp で今の配列を取り出して書き戻し、配列の読み直しを起こす(xkbcomp は Xvfb の依存で入っている)。
        割り当てたキーコードは先に touched へ入れる(xkbcomp が失敗しても呼び出し側が戻せるように)。
        """
        if not spares:
            raise RuntimeError("no spare keycode to type this character")
        loaded: dict[int, int] = {}
        for ch in rest:
            keysym = _keysym(ch)
            if len(loaded) == len(spares):
                break
            if keysym in loaded or self._base_key(keysym, empty):
                continue
            code = loaded[keysym] = spares[len(loaded)]
            touched.add(code)
            self._d.change_keyboard_mapping(code, [(keysym,) * width])
        self._sync()
        subprocess.run(["xkbcomp", "-w", "0", "-", self._display], input=self._dump_keymap(), capture_output=True, check=True, timeout=5)
        time.sleep(REMAP_SETTLE_SEC)
        return loaded

    def _dump_keymap(self) -> bytes:
        return subprocess.run(["xkbcomp", "-xkb", self._display, "-"], capture_output=True, check=True, timeout=5).stdout

    def _named_keycodes(self) -> set[int]:
        if self._named is None:
            self._named = {int(n) for n in re.findall(rb"<[^>]+>\s*=\s*(\d+)\s*;", self._dump_keymap())}
        return self._named

    def _tap(self, code: int, shift: bool) -> None:
        if shift:
            xtest.fake_input(self._d, X.KeyPress, self._shift)
        xtest.fake_input(self._d, X.KeyPress, code)
        xtest.fake_input(self._d, X.KeyRelease, code)
        if shift:
            xtest.fake_input(self._d, X.KeyRelease, self._shift)
        self._sync()


def _keysym(ch: str) -> int:
    if ch in _CONTROL_KEYSYMS:
        return _CONTROL_KEYSYMS[ch]
    cp = ord(ch)
    return cp if 0x20 <= cp <= 0xFF else 0x01000000 | cp  # X の Unicode keysym 規則


class ControlChannel:
    """1 セッションの操作チャネル. 同時に操作できるのは 1 つだけ(SessionManager.open_control が保証)."""

    def __init__(self, injector: XTestInjector, on_close):
        self._inj = injector
        self._on_close = on_close
        self._closed = False
        self._close_lock = threading.Lock()

    @property
    def closed(self) -> bool:
        return self._closed

    async def handle(self, raw: object) -> dict:
        """制御メッセージを 1 件処理し、応答を返す. 不正なメッセージは error 応答(接続は切らない)."""
        seq = raw.get("seq") if isinstance(raw, dict) else None
        try:
            m = parse_control_message(raw)
        except ControlError as e:
            return {"type": "error", "seq": seq, "error": str(e)}
        if self._closed:
            return {"type": "error", "seq": seq, "error": "control channel closed"}
        try:
            if m["type"] == "pointer":
                await asyncio.to_thread(self._inj.pointer, m["action"], m["x"], m["y"])
            elif m["type"] == "wheel":
                await asyncio.to_thread(self._inj.wheel, m["x"], m["y"], m["dy"])
            elif m["type"] == "key":
                await asyncio.to_thread(self._inj.key, m["key"])
            else:
                await asyncio.to_thread(self._inj.text, m["text"])
        except Exception as e:  # X の切断・空きキーコード無しなど。接続は切らずに error 応答で知らせる
            logger.warning("control %s injection failed: %s", m["type"], type(e).__name__)  # 内容(text)は出さない
            return {"type": "error", "seq": seq, "error": f"injection failed: {type(e).__name__}"}
        logger.debug("control %s", m["type"])  # 内容(text)は出さない
        return {"type": "ack", "seq": seq, "ok": True}

    def close(self) -> None:
        with self._close_lock:  # stop(別スレッド)と WS の後始末が同時に来ても 1 回だけ閉じる
            if self._closed:
                return
            self._closed = True
        try:
            self._inj.close()
        finally:
            self._on_close()
