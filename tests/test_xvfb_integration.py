"""XvfbManager の実環境（実 Xvfb + 実 Fluxbox）統合テスト.

Fluxbox は起動直後にバックグラウンドで `fbsetbg`（壁紙復元）を自動実行する。
壁紙設定用バイナリ（feh/hsetroot/Esetroot 等）が1つも無いコンテナでは
fbsetbg が失敗し、`xmessage` エラーダイアログを出す（実測確認済み）。
この xmessage は Fluxbox とは別プロセスグループで起動されるため
`XvfbManager._stop_process`/`_kill_process` の `os.killpg(fluxbox_pid, ...)`
では倒せず、`release()` 後も孤児プロセス（→ゾンビ）としてコンテナに残り続ける
プロセステーブルリークになる（AGENTS.md の「プロセスリーク禁止」に抵触）。

`Xvfb` / `fluxbox` が無い環境では自動的に skip する。
`docker compose exec server uv run pytest tests/test_xvfb_integration.py -v`
等、両方が揃った環境（本番と同じ Dockerfile 由来）でのみ実行される。
"""

import os
import shutil
import time

import pytest

from web_screen_stream.xvfb import XvfbManager

pytestmark = pytest.mark.skipif(
    not (shutil.which("Xvfb") and shutil.which("fluxbox")),
    reason="Xvfb/fluxbox が無い環境では実環境統合テストを skip する",
)

# fbsetbg 失敗時の xmessage 出現を待つ猶予（実測: Fluxbox 起動後 1〜2s 以内）
_XMESSAGE_WAIT = 3.0


def _pids_by_comm(name: str) -> set[int]:
    """/proc を走査し、comm が name に一致する PID 集合を返す（ゾンビも含む）."""
    pids = set()
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        try:
            with open(f"/proc/{entry}/comm") as f:
                comm = f.read().strip()
        except (FileNotFoundError, ProcessLookupError):
            continue
        if comm == name:
            pids.add(int(entry))
    return pids


@pytest.mark.asyncio
async def test_release_does_not_leak_xmessage_process():
    """allocate()/release() サイクル後、fbsetbg 由来の xmessage が残らないこと.

    Fail→Pass: 壁紙設定バイナリが無い状態では fbsetbg が失敗して xmessage を
    出し、別 pgid のため killpg で倒せず release() 後も居残る（Fail）。
    壁紙設定バイナリ導入後は fbsetbg が成功し xmessage 自体が発生しない
    ため Pass する。
    """
    before = _pids_by_comm("xmessage")

    mgr = XvfbManager(base_display=250, max_displays=1)
    display = await mgr.allocate(320, 240)
    time.sleep(_XMESSAGE_WAIT)  # fbsetbg が失敗判定に至るまでの猶予
    await mgr.release(display)

    leaked = [pid for pid in _pids_by_comm("xmessage") - before if os.path.exists(f"/proc/{pid}")]
    assert not leaked, f"release() 後に xmessage プロセスが残留した(leak): {leaked}"
