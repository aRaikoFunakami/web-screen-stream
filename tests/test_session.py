"""BrowserStreamSession / SessionManager のテスト.

FFmpegSource をモックして、マルチキャスト・Late-join・ライフサイクルをテスト。
"""

import asyncio
import signal
from collections.abc import AsyncIterator

import pytest

from web_screen_stream.h264_extractor import H264UnitExtractor
import web_screen_stream.session as sess
from web_screen_stream.session import (
    BrowserStreamSession,
    SessionManager,
    _SENTINEL,
    _reap_chrome_pids,
)

SC4 = b"\x00\x00\x00\x01"


def _make_nal(nal_type: int, size: int = 10) -> bytes:
    """テスト用 NAL unit を生成."""
    return SC4 + bytes([0x60 | nal_type]) + bytes(size)


SPS = _make_nal(H264UnitExtractor.NAL_TYPE_SPS, 5)
PPS = _make_nal(H264UnitExtractor.NAL_TYPE_PPS, 3)
IDR = _make_nal(H264UnitExtractor.NAL_TYPE_IDR, 20)
NON_IDR1 = _make_nal(H264UnitExtractor.NAL_TYPE_NON_IDR, 8)
NON_IDR2 = _make_nal(H264UnitExtractor.NAL_TYPE_NON_IDR, 12)


# ============================================================
# GOP キャッシュのテスト
# ============================================================


class TestGopCache:
    """_update_gop_cache のテスト."""

    def test_sps_pps_stored(self):
        session = BrowserStreamSession("test", None)
        session._update_gop_cache(SPS)
        session._update_gop_cache(PPS)
        assert session._last_sps == SPS
        assert session._last_pps == PPS
        assert not session._gop_has_idr

    def test_idr_starts_new_gop(self):
        session = BrowserStreamSession("test", None)
        session._update_gop_cache(SPS)
        session._update_gop_cache(PPS)
        session._update_gop_cache(IDR)

        assert session._gop_has_idr
        # GOP = [SPS, PPS, IDR]
        assert len(session._gop_nals) == 3
        assert session._gop_nals[0] == SPS
        assert session._gop_nals[1] == PPS
        assert session._gop_nals[2] == IDR

    def test_non_idr_appended(self):
        session = BrowserStreamSession("test", None)
        session._update_gop_cache(SPS)
        session._update_gop_cache(PPS)
        session._update_gop_cache(IDR)
        session._update_gop_cache(NON_IDR1)
        session._update_gop_cache(NON_IDR2)

        # GOP = [SPS, PPS, IDR, NON_IDR1, NON_IDR2]
        assert len(session._gop_nals) == 5

    def test_second_idr_resets_gop(self):
        session = BrowserStreamSession("test", None)
        session._update_gop_cache(SPS)
        session._update_gop_cache(PPS)
        session._update_gop_cache(IDR)
        session._update_gop_cache(NON_IDR1)
        session._update_gop_cache(NON_IDR2)

        # 新しい GOP
        idr2 = _make_nal(H264UnitExtractor.NAL_TYPE_IDR, 30)
        session._update_gop_cache(idr2)

        # GOP = [SPS, PPS, IDR2]
        assert len(session._gop_nals) == 3
        assert session._gop_nals[2] == idr2

    def test_non_idr_before_idr_ignored(self):
        session = BrowserStreamSession("test", None)
        session._update_gop_cache(NON_IDR1)
        assert len(session._gop_nals) == 0
        assert not session._gop_has_idr


# ============================================================
# subscribe / Late-join のテスト
# ============================================================


class TestSubscribe:
    """subscribe() と Late-join のテスト."""

    @pytest.mark.asyncio
    async def test_late_join_with_gop_cache(self):
        """GOP キャッシュがある場合、Late-join で先に受信できる."""
        session = BrowserStreamSession("test", None)
        # GOP キャッシュを事前構築
        session._update_gop_cache(SPS)
        session._update_gop_cache(PPS)
        session._update_gop_cache(IDR)
        session._update_gop_cache(NON_IDR1)
        session._status = "streaming"

        # subscribe: GOP snapshot が Queue に先詰めされるはず
        received = []
        async for nal in _subscribe_with_timeout(session, timeout=0.1):
            received.append(nal)

        # Late-join で 4 NAL (SPS, PPS, IDR, NON_IDR1)
        assert len(received) == 4
        assert received[0] == SPS
        assert received[1] == PPS
        assert received[2] == IDR
        assert received[3] == NON_IDR1

    @pytest.mark.asyncio
    async def test_no_late_join_without_idr(self):
        """IDR がない場合、Late-join は空."""
        session = BrowserStreamSession("test", None)
        session._update_gop_cache(SPS)
        session._update_gop_cache(PPS)
        session._status = "streaming"

        received = []
        async for nal in _subscribe_with_timeout(session, timeout=0.1):
            received.append(nal)

        assert len(received) == 0

    @pytest.mark.asyncio
    async def test_subscriber_cleanup_on_exit(self):
        """subscribe を抜けた後、subscriber リストから除去される."""
        session = BrowserStreamSession("test", None)
        session._status = "streaming"

        assert session.subscriber_count == 0

        async for _ in _subscribe_with_timeout(session, timeout=0.05):
            pass

        assert session.subscriber_count == 0


# ============================================================
# ブロードキャスト・マルチキャストのテスト
# ============================================================


class TestBroadcast:
    """_run_broadcast / マルチキャスト配信のテスト."""

    @pytest.mark.asyncio
    async def test_broadcast_to_multiple_subscribers(self):
        """複数 subscriber に同じ NAL が配信される."""
        session = BrowserStreamSession("test", None)
        session._status = "streaming"

        # 手動で subscriber を追加
        q1: asyncio.Queue = asyncio.Queue(maxsize=100)
        q2: asyncio.Queue = asyncio.Queue(maxsize=100)
        async with session._lock:
            session._subscribers.append(q1)
            session._subscribers.append(q2)

        # _update_gop_cache + 配信をシミュレート
        nals = [SPS, PPS, IDR, NON_IDR1]
        for nal in nals:
            session._update_gop_cache(nal)
            async with session._lock:
                subscribers = list(session._subscribers)
            for queue in subscribers:
                try:
                    queue.put_nowait(nal)
                except asyncio.QueueFull:
                    pass

        # 両方とも同じ 4 NAL を受信
        for q in [q1, q2]:
            assert q.qsize() == 4
            assert q.get_nowait() == SPS
            assert q.get_nowait() == PPS
            assert q.get_nowait() == IDR
            assert q.get_nowait() == NON_IDR1


# ============================================================
# SessionManager のテスト
# ============================================================


class TestSessionManager:
    """SessionManager の CRUD テスト."""

    def test_list_sessions_empty(self):
        mgr = SessionManager()
        assert mgr.list_sessions() == []

    def test_get_nonexistent(self):
        mgr = SessionManager()
        assert mgr.get("nonexistent") is None

    @pytest.mark.asyncio
    async def test_duplicate_session_raises(self):
        mgr = SessionManager()
        # 直接セッションを登録（FFmpeg を実際に起動しない）
        session = BrowserStreamSession("test", None)
        mgr._sessions["test"] = session

        with pytest.raises(ValueError, match="already exists"):
            await mgr.create("test")

    @pytest.mark.asyncio
    async def test_stop_nonexistent_raises(self):
        mgr = SessionManager()
        with pytest.raises(KeyError, match="not found"):
            await mgr.stop("nonexistent")


# ============================================================
# chrome プロセスリークのテスト（crashpad handler 対策）
# ============================================================


class TestReapChromePids:
    """_reap_chrome_pids のテスト.

    browser.close() では終了しない chrome_crashpad 等を、launch 前後の
    PID 差分に基づき SIGTERM→SIGKILL で個別に刈り取れることを確認する。
    実際の Chromium は起動せず、os.kill を差し替えてプロセス生死を模擬する。
    """

    @pytest.mark.asyncio
    async def test_survivor_gets_sigterm_then_sigkill(self, monkeypatch):
        """SIGTERM で死なない PID は SIGKILL で刈り取られる."""
        alive = {4242}  # SIGTERM を無視して生き残る PID（= chrome_crashpad 想定）
        sent_signals: list[tuple[int, int]] = []

        def fake_kill(pid, sig):
            sent_signals.append((pid, sig))
            if sig == 0:
                if pid not in alive:
                    raise ProcessLookupError
                return
            if sig == signal.SIGKILL:
                alive.discard(pid)
            # SIGTERM は無視（alive のまま）

        async def fake_sleep(_seconds):
            return None

        monkeypatch.setattr("web_screen_stream.session.os.kill", fake_kill)
        monkeypatch.setattr("web_screen_stream.session.asyncio.sleep", fake_sleep)

        await _reap_chrome_pids({4242})

        assert (4242, signal.SIGTERM) in sent_signals
        assert (4242, signal.SIGKILL) in sent_signals
        assert 4242 not in alive

    @pytest.mark.asyncio
    async def test_reparented_zombie_is_waitpid_reaped(self, monkeypatch):
        """SIGKILL 後、reparent 済みの自分の子は os.waitpid で回収される.

        コンテナの PID 1 に reparent された chrome_crashpad は kill しただけ
        では zombie として残るため、os.waitpid(WNOHANG) で回収されることを
        確認する（reparent 完了までの遅延を模したリトライも検証）。
        """
        waitpid_calls: list[int] = []
        attempts = {"n": 0}

        # _pid_alive: 最初の生存チェック(SIGTERM前)のみ True、以降(SIGKILL後の
        # 再チェック含む)は False（= SIGKILL で死んだ）を返す。
        alive_checks = {"n": 0}

        def fake_kill(pid, sig):
            if sig == 0:
                alive_checks["n"] += 1
                if alive_checks["n"] > 1:
                    raise ProcessLookupError
                return
            # SIGTERM/SIGKILL の送信自体は成功扱い

        def fake_waitpid(pid, options):
            waitpid_calls.append(pid)
            attempts["n"] += 1
            if attempts["n"] < 3:
                return (0, 0)  # まだ reparent/回収できていない
            return (pid, 0)  # 回収成功

        async def fake_sleep(_seconds):
            return None

        monkeypatch.setattr("web_screen_stream.session.os.kill", fake_kill)
        monkeypatch.setattr("web_screen_stream.session.os.waitpid", fake_waitpid)
        monkeypatch.setattr("web_screen_stream.session.asyncio.sleep", fake_sleep)

        await _reap_chrome_pids({7777})

        assert waitpid_calls.count(7777) == 3

    @pytest.mark.asyncio
    async def test_non_child_pid_gives_up_without_hanging(self, monkeypatch):
        """自分の子でない PID は ChildProcessError で諦め、無限リトライしない."""
        waitpid_calls: list[int] = []
        alive_checks = {"n": 0}

        def fake_kill(pid, sig):
            if sig == 0:
                alive_checks["n"] += 1
                if alive_checks["n"] > 1:
                    raise ProcessLookupError  # SIGKILL 後は死亡確認済み扱い
                return  # 最初の _pid_alive チェックだけ生きている扱い

        def fake_waitpid(pid, options):
            waitpid_calls.append(pid)
            raise ChildProcessError  # 自分の子ではない（reparent されていない等）

        async def fake_sleep(_seconds):
            return None

        monkeypatch.setattr("web_screen_stream.session.os.kill", fake_kill)
        monkeypatch.setattr("web_screen_stream.session.os.waitpid", fake_waitpid)
        monkeypatch.setattr("web_screen_stream.session.asyncio.sleep", fake_sleep)

        await _reap_chrome_pids({8888})  # ChildProcessError ですぐ諦め、ハングしない

        assert waitpid_calls == [8888]  # リトライせず1回で諦めている

    @pytest.mark.asyncio
    async def test_already_dead_pid_is_noop(self, monkeypatch):
        """既に終了している PID には何もシグナルを送らない."""
        sent_signals: list[tuple[int, int]] = []

        def fake_kill(pid, sig):
            sent_signals.append((pid, sig))
            if sig == 0:
                raise ProcessLookupError  # 既に死んでいる
            raise AssertionError("dead PID に SIGTERM/SIGKILL を送るべきではない")

        monkeypatch.setattr("web_screen_stream.session.os.kill", fake_kill)

        await _reap_chrome_pids({999})

        assert sent_signals == [(999, 0)]

    @pytest.mark.asyncio
    async def test_empty_pids_is_noop(self, monkeypatch):
        """PID 集合が空なら os.kill を一切呼ばない."""
        def fake_kill(pid, sig):
            raise AssertionError("空集合で kill が呼ばれるべきではない")

        monkeypatch.setattr("web_screen_stream.session.os.kill", fake_kill)

        await _reap_chrome_pids(set())


# ============================================================
# ヘルパー
# ============================================================


async def _subscribe_with_timeout(
    session: BrowserStreamSession, timeout: float
) -> AsyncIterator:
    """subscribe を timeout 秒後に打ち切る（テスト用）."""
    async def _gen():
        async for nal in session.subscribe():
            yield nal

    gen = _gen()
    try:
        async with asyncio.timeout(timeout):
            async for nal in gen:
                yield nal
    except TimeoutError:
        pass
    finally:
        await gen.aclose()


# ============================================================
# 起動の設定（proxy・Xvfb で割り当てた display）
# ============================================================


@pytest.fixture(autouse=True)
def _no_forced_loopback_bypass(monkeypatch):
    """CI の環境に PLAYWRIGHT_DISABLE_FORCED_CHROMIUM_PROXIED_LOOPBACK があっても、proxy のテストを揺らさない。"""
    monkeypatch.delenv("PLAYWRIGHT_DISABLE_FORCED_CHROMIUM_PROXIED_LOOPBACK", raising=False)


async def test_create_with_xvfb_keeps_config_fields(monkeypatch):
    """Xvfb の割り当てで display を差し替えても、draw_mouse・proxy などの項目を落とさない。"""
    from web_screen_stream.config import StreamConfig

    class FakeXvfb:
        async def allocate(self, w, h):
            return ":101"

        async def release(self, display):
            pass

    launched = []

    async def fake_launch(self, session_id, config, url):
        launched.append(config)
        return (None, None, None, set())

    async def fake_start(self):
        return None

    monkeypatch.setattr(SessionManager, "_launch_browser", fake_launch)
    monkeypatch.setattr(BrowserStreamSession, "start", fake_start)
    mgr = SessionManager(xvfb_manager=FakeXvfb())
    proxy = {"server": "http://127.0.0.1:8899"}
    await mgr.create("s", StreamConfig(draw_mouse=True, proxy=proxy), url="about:blank")
    assert launched[0].display == ":101"
    assert launched[0].draw_mouse is True and launched[0].proxy == proxy


def test_cdp_launch_args_have_no_automation_marks():
    """普通に起動する引数: ポートは番号で渡し（0 だと navigator.webdriver が true）、--enable-automation を付けない。
    proxy・WebRTC の制限・User-Agent は指定したときだけ。"""
    from web_screen_stream.config import StreamConfig

    plain = sess._cdp_launch_args("chrome", StreamConfig(), None, 9555, "/p")
    assert "--remote-debugging-port=9555" in plain and "--user-data-dir=/p" in plain
    assert not any(a.startswith(("--enable-automation", "--proxy", "--user-agent")) for a in plain)
    assert "--webrtc-ip-handling-policy=disable_non_proxied_udp" not in plain

    c = StreamConfig(user_agent="Mozilla/5.0 (test)")
    full = sess._cdp_launch_args("chrome", c, {"server": "http://127.0.0.1:1"}, 9555, "/p")
    assert "--proxy-server=http://127.0.0.1:1" in full and "--proxy-bypass-list=<-loopback>" in full
    assert "--webrtc-ip-handling-policy=disable_non_proxied_udp" in full
    assert "--user-agent=Mozilla/5.0 (test)" in full
    assert "--disable-popup-blocking" in plain
    # server は「スキーム://ホスト:ポート」に組み直す（末尾の / を Chromium に渡さない）
    slash = sess._cdp_launch_args("chrome", StreamConfig(), {"server": "http://127.0.0.1:1/"}, 9555, "/p")
    assert "--proxy-server=http://127.0.0.1:1" in slash


async def test_launch_uses_cdp_unless_proxy_has_credentials(monkeypatch):
    """認証なし（無しも含む）は普通に起動して CDP で接続する。認証付きの proxy だけは Playwright の launch に渡す
    （--proxy-server に認証は書けない）。launch のときの User-Agent は new_page に渡す。"""
    import playwright.async_api as pa

    from web_screen_stream.config import StreamConfig

    cdp_calls, launches, pages = [], [], []

    class _Page:
        async def goto(self, *a, **k):
            return None

        async def set_viewport_size(self, size):
            pages.append({"viewport": size})

    class _Context:
        pages = [_Page()]

    class _Browser:
        contexts = [_Context()]

        async def new_page(self, **k):
            pages.append(k)
            return _Page()

    class _Chromium:
        async def launch(self, **kw):
            launches.append(kw)
            return _Browser()

    class _PW:
        chromium = _Chromium()

    class _Starter:
        async def start(self):
            return _PW()

    async def fake_cdp(pw, config, tag, proxy):
        cdp_calls.append(proxy)
        return _Browser()

    monkeypatch.setattr(pa, "async_playwright", lambda: _Starter())
    monkeypatch.setattr(sess, "_launch_over_cdp", fake_cdp)
    mgr = SessionManager()
    await mgr._launch_browser("a", StreamConfig(), "about:blank")
    await mgr._launch_browser("b", StreamConfig(proxy={"server": "http://127.0.0.1:1"}), "about:blank")
    empty = {"server": "http://127.0.0.1:1", "username": "", "password": ""}  # 空の認証は認証なしと同じ
    await mgr._launch_browser("d", StreamConfig(proxy=empty), "about:blank")
    auth = {"server": "http://127.0.0.1:1", "username": "u", "password": "p"}
    await mgr._launch_browser("c", StreamConfig(proxy=auth, user_agent="Mozilla/5.0 (test)"), "about:blank")

    assert cdp_calls == [None, {"server": "http://127.0.0.1:1"}, empty]
    assert pages[:3] == [{"viewport": {"width": 1280, "height": 720}}] * 3
    assert len(launches) == 1 and launches[0]["proxy"] == auth
    assert "--webrtc-ip-handling-policy=disable_non_proxied_udp" in launches[0]["args"]
    assert pages[3]["user_agent"] == "Mozilla/5.0 (test)"


@pytest.mark.parametrize("proxy", [{}, {"server": ""}, {"server": 123}, "http://127.0.0.1:1",
                                   {"server": "http://127.0.0.1:1", "bypass": "*.corp"},
                                   {"server": "http://127.0.0.1:1", "pasword": "x"},
                                   {"server": "http://127.0.0.1:1", "username": 1},
                                   {"server": "socks5://127.0.0.1:1080", "username": "u", "password": "p"},
                                   {"server": "127.0.0.1:8899"}, {"server": "http://127.0.0.1:99999"},
                                   {"server": "http://[::1"}, {"server": "http://u:p@127.0.0.1:1"},
                                   {"server": "http://127.0.0.1:1/x"}, {"server": "http://127.0.0.1:0"},
                                   {"server": "http://127.0.0.1:1", 1: "a", "b": "c"},
                                   {"server": "http://filter-proxy,other-host:"}, {"server": "http://a;b:1"}])
def test_proxy_must_have_server_and_no_bypass(proxy):
    """空の proxy で黙ってプロキシ無しに起動したり、bypass で強制のプロキシを抜けたりしない。誤りは必ず ProxyConfigError。"""
    from web_screen_stream import ProxyConfigError, StreamConfig

    with pytest.raises(ProxyConfigError):
        StreamConfig(proxy=proxy)


def test_proxy_dict_is_copied():
    from web_screen_stream.config import StreamConfig

    p = {"server": "http://127.0.0.1:1"}
    c = StreamConfig(proxy=p)
    p["server"] = "http://127.0.0.1:2"
    assert c.proxy == {"server": "http://127.0.0.1:1"}


def test_proxy_refused_when_playwright_would_skip_loopback(monkeypatch):
    from web_screen_stream.config import ProxyConfigError, StreamConfig

    monkeypatch.setenv("PLAYWRIGHT_DISABLE_FORCED_CHROMIUM_PROXIED_LOOPBACK", "1")
    with pytest.raises(ProxyConfigError):
        StreamConfig(proxy={"server": "http://127.0.0.1:1"})


def test_proxy_is_not_in_repr():
    from web_screen_stream.config import StreamConfig

    c = StreamConfig(proxy={"server": "http://127.0.0.1:1", "username": "u", "password": "s3cret"})
    assert "s3cret" not in repr(c)


async def test_launch_rechecks_proxy_before_starting_playwright(monkeypatch):
    """作った後に bypass を足されても、Playwright を起動する前に拒む。"""
    import playwright.async_api as pa

    from web_screen_stream.config import ProxyConfigError, StreamConfig

    started = []
    monkeypatch.setattr(pa, "async_playwright", lambda: started.append(True))
    c = StreamConfig(proxy={"server": "http://127.0.0.1:1"})
    c.proxy["bypass"] = "*.corp"
    with pytest.raises(ProxyConfigError):
        await SessionManager()._launch_browser("a", c, "about:blank")
    assert started == []


def test_proxy_host_may_contain_underscore():
    from web_screen_stream import StreamConfig

    assert StreamConfig(proxy={"server": "http://egress_proxy:3128"}).proxy == {"server": "http://egress_proxy:3128"}


async def test_proxy_without_url_is_rejected_before_allocating_display():
    """ブラウザを起動しない create() で proxy を渡しても、黙って無視せず拒む（表示を確保する前に）。"""
    from web_screen_stream import ProxyConfigError, StreamConfig

    class FakeXvfb:
        async def allocate(self, w, h):
            raise AssertionError("表示を確保しない")

    with pytest.raises(ProxyConfigError):
        await SessionManager(xvfb_manager=FakeXvfb()).create("p", StreamConfig(proxy={"server": "http://127.0.0.1:1"}))
