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

import asyncio
import os
import shutil
import time

import pytest

from web_screen_stream.xvfb import XvfbManager

pytestmark = pytest.mark.skipif(
    not (shutil.which("Xvfb") and shutil.which("fluxbox")),
    reason="Xvfb/fluxbox が無い環境では実環境統合テストを skip する",
)

# 本番 (app/main.py) は :100-:104 しか使わないため、このテスト専用の番号にして
# 本番コードと衝突しないようにする。
_TEST_DISPLAY_NUM = 250

# fbsetbg 失敗時の xmessage 出現を待つ猶予（実測: Fluxbox 起動後 1〜2s 以内）。
# 固定 sleep だと CI 負荷時に間に合わず false negative になりうるため、
# 出現をポーリングで検知し、間に合わなければ最大 _XMESSAGE_WAIT まで待つ。
_XMESSAGE_WAIT = 5.0
_POLL_INTERVAL = 0.1


def _pids_by_comm(name: str) -> set[int]:
    """/proc を走査し、comm が name に一致する PID 集合を返す（ゾンビも含む）."""
    pids = set()
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        try:
            with open(f"/proc/{entry}/comm") as f:
                comm = f.read().strip()
        except (FileNotFoundError, PermissionError, ProcessLookupError):
            continue
        if comm == name:
            pids.add(int(entry))
    return pids


def _force_clear_stale_display_lock(display_num: int) -> None:
    """このテスト専用の display 番号のロック残骸を無条件に消す.

    本番は :100-:104 しか使わないため、:250 のロックは前回のこのテスト実行分
    以外あり得ない。XvfbManager._cleanup_stale_lock() の PID 生存確認は
    PID 再利用時に誤検知しうる（前回このテストが異常終了し、そのロックの
    PID がその後たまたま無関係なプロセスに再利用されたケース）ため、
    テスト専用の番号であることを利用してここで確実に片付ける。
    """
    for path in (
        f"/tmp/.X{display_num}-lock",
        f"/tmp/.X11-unix/X{display_num}",
    ):
        try:
            os.unlink(path)
        except FileNotFoundError:
            pass


@pytest.mark.asyncio
async def test_release_does_not_leak_xmessage_process():
    """allocate()/release() サイクル後、fbsetbg 由来の xmessage が残らないこと.

    Fail→Pass: 壁紙設定バイナリが無い状態では fbsetbg が失敗して xmessage を
    出し、別 pgid のため killpg で倒せず release() 後も居残る（Fail）。
    壁紙設定バイナリ導入後は fbsetbg が成功し xmessage 自体が発生しない
    ため Pass する。
    """
    _force_clear_stale_display_lock(_TEST_DISPLAY_NUM)

    mgr = XvfbManager(base_display=_TEST_DISPLAY_NUM, max_displays=1)
    display = await mgr.allocate(320, 240)

    # fbsetbg が失敗判定に至り xmessage を出すまでポーリングで待つ
    # （固定 sleep と違い、早期に出現すれば即座に次へ進む）。
    deadline = time.monotonic() + _XMESSAGE_WAIT
    while time.monotonic() < deadline:
        if _pids_by_comm("xmessage"):
            break
        await asyncio.sleep(_POLL_INTERVAL)

    await mgr.release(display)

    # before/after 差分ではなく現在の残留有無そのものを見る: 差分方式だと
    # 過去の失敗実行で残ったゾンビが「既知」として除外され、リークが
    # 蓄積し続けても検知できなくなるため。
    leaked = _pids_by_comm("xmessage")
    assert not leaked, f"release() 後に xmessage プロセスが残留した(leak): {leaked}"
