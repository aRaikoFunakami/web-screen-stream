"""刈り取りの対象を、その起動の Chromium だけに絞る（#12）のテスト.

起動ごとの目印（環境変数）で自分のプロセスだけを見分ける。偽の /proc と実プロセスの両方で、
同時に起動した別の Chromium・別ユーザーのプロセスに触れないこと、親が PID 1 に付け替わった
crashpad のような子を引き続き刈り取れることを確認する。
"""

from __future__ import annotations

import asyncio
import os
import time
import sys
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


async def test_each_launch_passes_a_unique_tag(monkeypatch):
    tags: list[str] = []

    class _Page:
        async def goto(self, *a, **k):
            return None

        async def set_viewport_size(self, size):
            return None

    class _Context:
        pages = [_Page()]

    class _Browser:
        contexts = [_Context()]

    class _PW:
        async def stop(self):
            return None

    class _Starter:
        async def start(self):
            return _PW()

    async def fake_cdp(pw, config, tag, proxy):
        tags.append(tag)
        return _Browser()

    monkeypatch.setattr(pa, "async_playwright", lambda: _Starter())
    monkeypatch.setattr(sess, "_launch_over_cdp", fake_cdp)
    mgr = SessionManager()
    returned = [(await mgr._launch_browser("s", StreamConfig(), "about:blank"))[3] for _ in range(2)]

    assert returned == tags and tags[0] != tags[1] and all(tags)


async def test_cdp_launch_passes_the_tag_and_keeps_the_environment(monkeypatch, tmp_path):
    """普通に起動する Chromium にも目印を環境変数で渡す（環境は置き換えない）。起動直後に終了したら失敗にする。"""
    seen: list[dict] = []

    class _Proc:
        returncode = 1

    async def fake_exec(*args, env=None, **kw):
        seen.append(env)
        return _Proc()

    class _Chromium:
        executable_path = "chrome"

    class _PW:
        chromium = _Chromium()

    monkeypatch.setattr(sess.asyncio, "create_subprocess_exec", fake_exec)
    monkeypatch.setattr(sess.tempfile, "gettempdir", lambda: str(tmp_path))
    monkeypatch.setenv("WSS_TEST_MARKER", "kept")
    with pytest.raises(RuntimeError, match="開かずに終了"):
        await sess._launch_over_cdp(_PW(), StreamConfig(), "tag-1", None)
    sess._LAUNCHED.pop("tag-1")

    assert seen[0][TAG_ENV] == "tag-1" and seen[0]["WSS_TEST_MARKER"] == "kept"
    assert TAG_ENV not in os.environ  # 自分の環境には付けない
    assert (tmp_path / "wss-chromium-tag-1").is_dir()  # プロファイルは目印から決まり、刈り取りで消す


# ---------- 自力で先に終了して zombie になった子 ----------


def test_exited_chrome_children_of_ours_are_collected(tmp_path, monkeypatch):
    me = os.getpid()
    for pid, comm, state, ppid in [
        (11, "chrome_crashpad", "Z", me),  # 回収する
        (12, "chrome_crashpad", "R", me),  # 生きている
        (13, "chrome_crashpad", "Z", 1),  # 他人の子
        (14, "ffmpeg", "Z", me),  # chrome ではない
        (15, "chrome (x) y", "Z", me),  # comm に空白と ')' があっても読める
    ]:
        d = tmp_path / str(pid)
        d.mkdir()
        (d / "stat").write_text(f"{pid} ({comm}) {state} {ppid} 1 1 0\n")
    waited: list[int] = []
    monkeypatch.setattr(sess.os, "waitpid", lambda pid, o: waited.append(pid) or (pid, 0))
    sess._collect_chrome_zombies(str(tmp_path))
    assert sorted(waited) == [11, 15]

    # 普通に起動した本体（別のセッションの分も）は asyncio が回収するので、ここでは取らない
    waited.clear()
    sess._collect_chrome_zombies(str(tmp_path), frozenset({15}))
    assert waited == [11]


def test_collect_zombies_ignores_missing_proc_and_non_children(tmp_path, monkeypatch):
    sess._collect_chrome_zombies(str(tmp_path / "none"))
    d = tmp_path / "11"
    d.mkdir()
    (d / "stat").write_text(f"11 (chrome) Z {os.getpid()} 1\n")

    def gone(pid, o):
        raise ChildProcessError

    monkeypatch.setattr(sess.os, "waitpid", gone)
    sess._collect_chrome_zombies(str(tmp_path))  # 例外を出さない


async def test_reap_removes_the_profile_of_the_tag(monkeypatch, tmp_path):
    """普通に起動した分のプロファイル（Cookie を含む）は、刈り取りで消す。"""
    monkeypatch.setattr(sess.tempfile, "gettempdir", lambda: str(tmp_path))
    monkeypatch.setattr(sess, "_tagged_pids", lambda tag: set())
    profile = tmp_path / "wss-chromium-t"
    (profile / "Default").mkdir(parents=True)
    await sess._reap_tagged("t")
    assert not profile.exists()


async def test_connects_to_the_url_its_own_chromium_printed(monkeypatch, tmp_path):
    """ポートの番号ではなく、自分が起動した Chromium が標準エラーに出した接続先に繋ぐ（他人のブラウザに繋がらない）。"""
    class _Proc:
        returncode = None

    async def fake_exec(*args, stderr=None, **kw):  # Chromium は標準エラー（ログのファイル）に接続先を書く
        stderr.write(b"[123:ERROR] something\nDevTools listening on ws://127.0.0.1:9555/devtools/browser/abc-123\n")
        stderr.flush()
        return _Proc()

    connected = []

    class _Chromium:
        executable_path = "chrome"

        async def connect_over_cdp(self, url):
            connected.append(url)
            return "browser"

    class _PW:
        chromium = _Chromium()

    monkeypatch.setattr(sess.asyncio, "create_subprocess_exec", fake_exec)
    monkeypatch.setattr(sess.tempfile, "gettempdir", lambda: str(tmp_path))
    assert await sess._launch_over_cdp(_PW(), StreamConfig(), "tag-2", None) == "browser"
    assert connected == ["ws://127.0.0.1:9555/devtools/browser/abc-123"]
    sess._LAUNCHED.pop("tag-2")


async def test_reap_kills_and_collects_its_own_chromium(monkeypatch, tmp_path):
    """/proc の無い環境でも、普通に起動した本体は刈り取りで止めて回収する。"""
    monkeypatch.setattr(sess.tempfile, "gettempdir", lambda: str(tmp_path))
    monkeypatch.setattr(sess, "_CHROME_REAP_SIGTERM_WAIT", 0.05)
    proc = await asyncio.create_subprocess_exec("sleep", "60")
    sess._LAUNCHED["t-kill"] = proc
    await sess._reap_tagged("t-kill")
    assert proc.returncode is not None and "t-kill" not in sess._LAUNCHED


async def test_profile_is_removed_even_if_reaping_fails(monkeypatch, tmp_path):
    """刈り取りの途中で失敗しても、Cookie を含むプロファイルは消す。"""
    monkeypatch.setattr(sess.tempfile, "gettempdir", lambda: str(tmp_path))

    def boom(tag):
        raise RuntimeError("scan failed")

    monkeypatch.setattr(sess, "_tagged_pids", boom)
    profile = tmp_path / "wss-chromium-f"
    profile.mkdir()
    with pytest.raises(RuntimeError):
        await sess._reap_tagged("f")
    assert not profile.exists()


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="PR_SET_PDEATHSIG は Linux だけ")
def test_launched_chromium_dies_with_its_parent(tmp_path):
    """本プロセスが落ちたら、普通に起動した Chromium も止まる（親の終了で SIGKILL を受ける）。"""
    pidfile = tmp_path / "pid"
    code = (
        "import subprocess, sys, web_screen_stream.session as s\n"
        f"p = subprocess.Popen(['sleep', '60'], preexec_fn=s._die_with_parent)\n"
        f"open({str(pidfile)!r}, 'w').write(str(p.pid))\n"
    )
    subprocess.run([sys.executable, "-c", code], check=True)  # 親は子を残して終了する
    pid = int(pidfile.read_text())
    for _ in range(50):
        if not sess._pid_alive(pid) or _is_zombie(pid):
            break
        time.sleep(0.1)
    assert not sess._pid_alive(pid) or _is_zombie(pid)
