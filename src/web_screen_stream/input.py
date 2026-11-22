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
(xdotool と同じ方式)。入力した文字の内容はログに出さない(資格情報を打つ用途があるため)。
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time

from Xlib import X, XK
from Xlib.display import Display
from Xlib.ext import xtest

logger = logging.getLogger(__name__)

MAX_TEXT_LEN = 256  # 1 メッセージで受ける文字数の上限(貼り付けの暴走を防ぐ)
# 空きキーコードへ割り当てた後・戻す前の待ち。待たないと Chromium が配列の変更を取り込む前に押され、最初に割り当てた
# 文字を使い続ける(実測: 0 秒で「日日日」、20ms 以上で「日本語」。余裕を見て 50ms)
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


class ControlError(ValueError):
    """制御メッセージが不正(型・範囲・未知の種別)."""


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
        return {"type": kind, "text": text}
    raise ControlError(f"unknown type: {kind!r}")


class XTestInjector:
    """1 つの X ディスプレイへ XTEST で入力する. スレッド安全(内部ロックで直列化)."""

    def __init__(self, display: str, width: int, height: int):
        self._d = Display(display)
        if not self._d.has_extension("XTEST"):
            self._d.close()
            raise RuntimeError(f"XTEST extension is not available on {display}")
        self._w, self._h = width, height
        self._lock = threading.Lock()
        self._shift = self._d.keysym_to_keycode(XK.XK_Shift_L)

    def close(self) -> None:
        with self._lock:
            self._d.close()

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
            self._d.sync()

    def wheel(self, nx: float, ny: float, dy: int) -> None:
        x, y = self._xy(nx, ny)
        button = 5 if dy > 0 else 4
        with self._lock:
            xtest.fake_input(self._d, X.MotionNotify, x=x, y=y)
            for _ in range(abs(dy)):
                xtest.fake_input(self._d, X.ButtonPress, button)
                xtest.fake_input(self._d, X.ButtonRelease, button)
            self._d.sync()

    def key(self, name: str) -> None:
        code = self._d.keysym_to_keycode(XK.string_to_keysym(_KEYS[name]))
        with self._lock:
            self._tap(code, shift=False)

    def text(self, text: str) -> None:
        with self._lock:
            for ch in text:
                cp = ord(ch)
                keysym = cp if 0x20 <= cp <= 0xFF else 0x01000000 | cp  # X の Unicode keysym 規則
                code = self._d.keysym_to_keycode(keysym)
                if code:
                    self._tap(code, shift=self._d.keycode_to_keysym(code, 0) != keysym)
                else:
                    self._tap_remapped(keysym)

    def _tap(self, code: int, *, shift: bool) -> None:
        if shift:
            xtest.fake_input(self._d, X.KeyPress, self._shift)
        xtest.fake_input(self._d, X.KeyPress, code)
        xtest.fake_input(self._d, X.KeyRelease, code)
        if shift:
            xtest.fake_input(self._d, X.KeyRelease, self._shift)
        self._d.sync()

    def _tap_remapped(self, keysym: int) -> None:
        """配列に無い文字: 空きキーコードへ一時的に keysym を割り当てて押し、元に戻す(xdotool と同じ方式)."""
        first = self._d.display.info.min_keycode
        count = self._d.display.info.max_keycode - first + 1
        mapping = self._d.get_keyboard_mapping(first, count)
        spare = next((first + i for i, syms in enumerate(mapping) if not any(syms)), None)
        if spare is None:
            raise RuntimeError("no spare keycode to type this character")
        width = len(mapping[0])
        self._d.change_keyboard_mapping(spare, [(keysym,) * width])
        self._d.sync()
        try:
            time.sleep(REMAP_SETTLE_SEC)
            self._tap(spare, shift=False)
            time.sleep(REMAP_SETTLE_SEC)
        finally:
            self._d.change_keyboard_mapping(spare, [(X.NoSymbol,) * width])
            self._d.sync()


class ControlChannel:
    """1 セッションの操作チャネル. 同時に操作できるのは 1 つだけ(SessionManager.open_control が保証)."""

    def __init__(self, injector: XTestInjector, on_close):
        self._inj = injector
        self._on_close = on_close
        self._closed = False

    async def handle(self, raw: object) -> dict:
        """制御メッセージを 1 件処理し、応答を返す. 不正なメッセージは error 応答(接続は切らない)."""
        seq = raw.get("seq") if isinstance(raw, dict) else None
        try:
            m = parse_control_message(raw)
        except ControlError as e:
            return {"type": "error", "seq": seq, "error": str(e)}
        if m["type"] == "pointer":
            await asyncio.to_thread(self._inj.pointer, m["action"], m["x"], m["y"])
        elif m["type"] == "wheel":
            await asyncio.to_thread(self._inj.wheel, m["x"], m["y"], m["dy"])
        elif m["type"] == "key":
            await asyncio.to_thread(self._inj.key, m["key"])
        else:
            await asyncio.to_thread(self._inj.text, m["text"])
        logger.debug("control %s", m["type"])  # 内容(text)は出さない
        return {"type": "ack", "seq": seq, "ok": True}

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self._inj.close()
        finally:
            self._on_close()
