"""H264UnitExtractor のテスト."""

from web_screen_stream.h264_extractor import H264UnitExtractor


SC4 = b"\x00\x00\x00\x01"
SC3 = b"\x00\x00\x01"


def _make_nal(nal_type: int, payload_size: int = 10) -> bytes:
    """テスト用 NAL unit を生成 (4-byte start code + nal_header + payload)."""
    nal_header = bytes([0x60 | nal_type])  # forbidden=0, nri=3, type
    payload = bytes(range(payload_size))
    return SC4 + nal_header + payload


def test_extract_two_nals():
    """2つの NAL unit を連続して push すると、1つ目が取り出せる."""
    ext = H264UnitExtractor()
    sps = _make_nal(H264UnitExtractor.NAL_TYPE_SPS, 5)
    pps = _make_nal(H264UnitExtractor.NAL_TYPE_PPS, 3)
    data = sps + pps

    result = ext.push(data)
    assert len(result) == 1
    assert result[0] == sps

    # flush で残りを取り出す
    remaining = ext.flush()
    assert len(remaining) == 1
    assert remaining[0] == pps


def test_extract_three_nals():
    """3つの NAL unit を push すると、2つが取り出せる."""
    ext = H264UnitExtractor()
    sps = _make_nal(H264UnitExtractor.NAL_TYPE_SPS, 5)
    pps = _make_nal(H264UnitExtractor.NAL_TYPE_PPS, 3)
    idr = _make_nal(H264UnitExtractor.NAL_TYPE_IDR, 20)
    data = sps + pps + idr

    result = ext.push(data)
    assert len(result) == 2
    assert result[0] == sps
    assert result[1] == pps

    remaining = ext.flush()
    assert len(remaining) == 1
    assert remaining[0] == idr


def test_incremental_push():
    """データを小分けに push しても正しく抽出できる."""
    ext = H264UnitExtractor()
    sps = _make_nal(H264UnitExtractor.NAL_TYPE_SPS, 8)
    pps = _make_nal(H264UnitExtractor.NAL_TYPE_PPS, 4)
    data = sps + pps

    # 5 バイトずつ分割して push
    all_nals = []
    for i in range(0, len(data), 5):
        chunk = data[i : i + 5]
        nals = ext.push(chunk)
        all_nals.extend(nals)

    # flush で残り取得
    all_nals.extend(ext.flush())

    assert len(all_nals) == 2
    assert all_nals[0] == sps
    assert all_nals[1] == pps


def test_3byte_start_code_normalized():
    """3-byte start code は 4-byte に正規化される."""
    ext = H264UnitExtractor()
    # 3-byte start code + SPS
    nal1 = SC3 + bytes([0x67, 0x01, 0x02, 0x03])
    # 4-byte start code + PPS (区切り用)
    nal2 = SC4 + bytes([0x68, 0x04, 0x05])

    result = ext.push(nal1 + nal2)
    assert len(result) == 1
    # 4-byte start code に正規化されている
    assert result[0][:4] == SC4


def test_nal_type():
    """nal_type() が正しく型を返す."""
    sps = _make_nal(H264UnitExtractor.NAL_TYPE_SPS)
    pps = _make_nal(H264UnitExtractor.NAL_TYPE_PPS)
    idr = _make_nal(H264UnitExtractor.NAL_TYPE_IDR)
    non_idr = _make_nal(H264UnitExtractor.NAL_TYPE_NON_IDR)

    assert H264UnitExtractor.nal_type(sps) == 7
    assert H264UnitExtractor.nal_type(pps) == 8
    assert H264UnitExtractor.nal_type(idr) == 5
    assert H264UnitExtractor.nal_type(non_idr) == 1


def test_is_keyframe():
    """is_keyframe() と is_sps/is_pps のヘルパー."""
    sps = _make_nal(H264UnitExtractor.NAL_TYPE_SPS)
    pps = _make_nal(H264UnitExtractor.NAL_TYPE_PPS)
    idr = _make_nal(H264UnitExtractor.NAL_TYPE_IDR)
    non_idr = _make_nal(H264UnitExtractor.NAL_TYPE_NON_IDR)

    assert H264UnitExtractor.is_sps(sps)
    assert not H264UnitExtractor.is_sps(pps)
    assert H264UnitExtractor.is_pps(pps)
    assert H264UnitExtractor.is_keyframe(idr)
    assert not H264UnitExtractor.is_keyframe(non_idr)


def test_empty_push():
    """空データの push は空リストを返す."""
    ext = H264UnitExtractor()
    assert ext.push(b"") == []


def test_flush_empty():
    """空バッファの flush は空リストを返す."""
    ext = H264UnitExtractor()
    assert ext.flush() == []


def test_buffer_overflow():
    """バッファ上限を超えても動作する."""
    ext = H264UnitExtractor(max_buffer_bytes=64)
    sps = _make_nal(H264UnitExtractor.NAL_TYPE_SPS, 30)
    pps = _make_nal(H264UnitExtractor.NAL_TYPE_PPS, 30)

    # バッファを溢れさせるが、パースは続く
    result = ext.push(sps + pps)
    # 結果は実装の切り捨て挙動に依存するが、クラッシュしないことが重要
    assert isinstance(result, list)
