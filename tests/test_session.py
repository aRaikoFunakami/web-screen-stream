"""BrowserStreamSession / SessionManager のテスト.

FFmpegSource をモックして、マルチキャスト・Late-join・ライフサイクルをテスト。
"""

import asyncio
from collections.abc import AsyncIterator

import pytest

from web_screen_stream.h264_extractor import H264UnitExtractor
from web_screen_stream.session import BrowserStreamSession, SessionManager, _SENTINEL

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
