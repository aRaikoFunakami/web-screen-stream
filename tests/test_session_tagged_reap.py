"""刈り取りの対象を、その起動の Chromium だけに絞る（#12）のテスト.

起動ごとの目印（環境変数）で自分のプロセスだけを見分ける。偽の /proc と実プロセスの両方で、
同時に起動した別の Chromium・別ユーザーのプロセスに触れないこと、親が PID 1 に付け替わった
crashpad のような子を引き続き刈り取れることを確認する。
"""

from __future__ import annotations

import asyncio
import os
import signal
import subprocess

import pytest

import playwright.async_api as pa

import web_screen_stream.session as sess
from web_screen_stream.config import StreamConfig
from web_screen_stream.session import SessionManager

ME = os.getuid()
TAG_ENV = sess._BROWSER_TAG_ENV


def fake_proc(tmp_path, procs):
    """procs: {pid: (環境変数の dict, uid)} から偽の /proc を作る."""
    for pid, (env, uid) in procs.items():
        d = tmp_path / str(pid)
        d.mkdir()
        (d / "environ").write_bytes(b"".join(f"{k}={v}".encode() + b"\0" for k, v in env.items()))
        (d / "status").write_text(f"Name:\tchrome\nUid:\t{uid}\t{uid}\t{uid}\t{uid}\n")
    (tmp_path / "self").mkdir()  # 数字でない項目は無視される
    return str(tmp_path)


def test_only_processes_with_the_tag_are_returned(tmp_path):
    proc = fake_proc(
        tmp_path,
        {
            11: ({TAG_ENV: "mine", "A": "1"}, ME),  # ブラウザ本体
            12: ({"A": "1", TAG_ENV: "mine"}, ME),  # crashpad（変数の位置は問わない）
            20: ({TAG_ENV: "other"}, ME),  # 同時に起動した別の Chromium
            21: ({TAG_ENV: "other"}, ME),
            30: ({"A": "1"}, ME),  # 無関係
            31: ({f"X{TAG_ENV}": "mine"}, ME),  # 名前の末尾だけ同じ別の変数
            32: ({TAG_ENV: "mine2"}, ME),  # 値の先頭だけ同じ
        },
    )
    assert sess._tagged_pids("mine", proc) == {11, 12}


def test_other_users_process_is_skipped(tmp_path):
    proc = fake_proc(tmp_path, {11: ({TAG_ENV: "t"}, ME), 12: ({TAG_ENV: "t"}, ME + 1)})
    assert sess._tagged_pids("t", proc) == {11}


def test_missing_proc_returns_empty(tmp_path):
    assert sess._tagged_pids("t", str(tmp_path / "none")) == set()


def test_process_that_vanishes_while_scanning_is_ignored(tmp_path):
    proc = fake_proc(tmp_path, {11: ({TAG_ENV: "t"}, ME), 12: ({TAG_ENV: "t"}, ME)})
    (tmp_path / "12" / "environ").unlink()  # 読む前に終了した
    assert sess._tagged_pids("t", proc) == {11}


async def test_reap_kills_only_tagged_processes(tmp_path, monkeypatch):
    proc = fake_proc(tmp_path, {11: ({TAG_ENV: "t"}, ME), 12: ({TAG_ENV: "t"}, ME), 20: ({TAG_ENV: "other"}, ME)})
    real = sess._tagged_pids
    monkeypatch.setattr(sess, "_tagged_pids", lambda tag: real(tag, proc))
    killed: list[int] = []
    monkeypatch.setattr(sess.os, "kill", lambda pid, sig: killed.append(pid) if sig else None)
    monkeypatch.setattr(sess.os, "waitpid", lambda pid, o: (pid, 0))

    async def no_sleep(_):
        return None

    monkeypatch.setattr(sess.asyncio, "sleep", no_sleep)
    await sess._reap_tagged("t")
    assert set(killed) == {11, 12}


@pytest.mark.skipif(not os.path.isdir("/proc"), reason="Linux の /proc が要る")
async def test_real_orphan_is_reaped_and_other_tag_is_untouched():
    """親が PID 1 へ付け替わった子（crashpad 相当）も、目印で見つけて刈り取る。別の目印の子は残る."""

    def spawn(tag):  # sh が sleep を残して先に終わる = sleep は孤児になる
        env = {**os.environ, TAG_ENV: tag}
        out = subprocess.run(
            ["sh", "-c", "sleep 60 >/dev/null 2>&1 & echo $!"], env=env, capture_output=True, text=True, check=True
        )
        return int(out.stdout)

    mine, other = spawn("real-mine"), spawn("real-other")
    try:
        assert sess._tagged_pids("real-mine") == {mine}
        await sess._reap_tagged("real-mine")
        assert not sess._pid_alive(mine) or _is_zombie(mine)
        assert sess._pid_alive(other) and not _is_zombie(other)
    finally:
        for pid in (mine, other):
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass


def _is_zombie(pid: int) -> bool:
    try:
        with open(f"/proc/{pid}/stat") as f:
            return f.read().rpartition(")")[2].split()[0] == "Z"
    except OSError:
        return True


# ---------- 起動が目印を渡す ----------


async def test_each_launch_passes_a_unique_tag_and_keeps_the_environment(monkeypatch):
    seen: list[dict] = []

    class _Page:
        async def goto(self, *a, **k):
            return None

    class _Browser:
        async def new_page(self, **k):
            return _Page()

        async def close(self):
            return None

    class _Chromium:
        async def launch(self, **kw):
            seen.append(kw["env"])
            return _Browser()

    class _PW:
        chromium = _Chromium()

        async def stop(self):
            return None

    class _Starter:
        async def start(self):
            return _PW()

    monkeypatch.setattr(pa, "async_playwright", lambda: _Starter())
    monkeypatch.setenv("WSS_TEST_MARKER", "kept")
    mgr = SessionManager()
    tags = []
    for _ in range(2):
        *_, tag = await mgr._launch_browser("s", StreamConfig(), "about:blank")
        tags.append(tag)

    assert tags[0] != tags[1] and all(tags)
    assert [e[TAG_ENV] for e in seen] == tags
    assert all(e["WSS_TEST_MARKER"] == "kept" for e in seen)  # 環境を置き換えない
    assert TAG_ENV not in os.environ  # 自分の環境には付けない

