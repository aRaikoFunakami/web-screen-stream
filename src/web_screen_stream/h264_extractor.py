"""H.264 NAL unit extractor.

FFmpeg の `-f h264` Annex-B 出力からNAL unit を抽出する。
android-screen-stream の _H264UnitExtractor を参考に、
FFmpeg (Annex-B 出力のみ) に特化して実装。
"""

import logging

logger = logging.getLogger(__name__)


class H264UnitExtractor:
    """H.264 Annex-B ストリームから NAL unit を抽出する.

    FFmpeg の `-f h264` 出力は常に Annex-B 形式のため、
    AVCC 形式のサポートは不要。

    各 NAL unit は 4-byte start code (0x00 0x00 0x00 0x01) 付きで返す。
    末尾の未確定データは内部バッファに保持され、次回 push() で確定する。
    """

    # NAL type constants
    NAL_TYPE_SPS = 7
    NAL_TYPE_PPS = 8
    NAL_TYPE_IDR = 5
    NAL_TYPE_NON_IDR = 1

    START_CODE_3 = b"\x00\x00\x01"
    START_CODE_4 = b"\x00\x00\x00\x01"

    def __init__(
        self,
        *,
        max_buffer_bytes: int = 512 * 1024,
        max_nal_bytes: int = 4 * 1024 * 1024,
    ):
        self._buf = bytearray()
        self._max = max_buffer_bytes
        self._max_nal = max_nal_bytes

    @staticmethod
    def nal_type(nal: bytes) -> int:
        """NAL unit のタイプを返す.

        Args:
            nal: Annex-B 形式の NAL unit (start code 付き)

        Returns:
            NAL type (0-31)
        """
        # Skip start code (3 or 4 bytes)
        if nal[2] == 1:
            return nal[3] & 0x1F
        return nal[4] & 0x1F

    @staticmethod
    def is_keyframe(nal: bytes) -> bool:
        """NAL unit が IDR (キーフレーム) かどうか."""
        return H264UnitExtractor.nal_type(nal) == H264UnitExtractor.NAL_TYPE_IDR

    @staticmethod
    def is_sps(nal: bytes) -> bool:
        """NAL unit が SPS かどうか."""
        return H264UnitExtractor.nal_type(nal) == H264UnitExtractor.NAL_TYPE_SPS

    @staticmethod
    def is_pps(nal: bytes) -> bool:
        """NAL unit が PPS かどうか."""
        return H264UnitExtractor.nal_type(nal) == H264UnitExtractor.NAL_TYPE_PPS

    def _find_start_code(self, buf: bytearray, start: int = 0) -> int:
        """バッファ内の次の start code 位置を返す.

        Returns:
            start code の開始位置。見つからなければ -1。
        """
        n = len(buf)
        i = start
        while i < n - 3:
            if buf[i] == 0 and buf[i + 1] == 0:
                if buf[i + 2] == 1:
                    return i
                if i < n - 4 and buf[i + 2] == 0 and buf[i + 3] == 1:
                    return i
            i += 1
        return -1

    def push(self, data: bytes) -> list[bytes]:
        """データを入力し、完成した NAL unit のリストを返す.

        Args:
            data: FFmpeg stdout からの raw バイトチャンク

        Returns:
            完成した NAL unit のリスト (Annex-B 形式、4-byte start code 付き)
        """
        if data:
            self._buf.extend(data)
            # バッファが上限を超えたら先頭を切り捨て
            if len(self._buf) > self._max:
                cut = len(self._buf) - self._max
                del self._buf[:cut]

        buf = self._buf
        n = len(buf)
        if n < 4:
            return []

        # start code の位置をすべて収集
        starts: list[int] = []
        i = 0
        while i < n - 3:
            if buf[i] == 0 and buf[i + 1] == 0:
                if buf[i + 2] == 1:
                    starts.append(i)
                    i += 3
                    continue
                if i < n - 4 and buf[i + 2] == 0 and buf[i + 3] == 1:
                    starts.append(i)
                    i += 4
                    continue
            i += 1

        if len(starts) < 2:
            return []

        # 先頭の start code 前のゴミを捨てる
        if starts[0] != 0:
            del buf[: starts[0]]
            return self.push(b"")

        # 隣接する start code 間が完全な NAL unit
        out: list[bytes] = []
        for a, b in zip(starts, starts[1:]):
            nal = bytes(buf[a:b])
            # 3-byte start code → 4-byte に正規化
            if nal[:3] == self.START_CODE_3 and nal[3:4] != b"\x00":
                nal = self.START_CODE_4 + nal[3:]
            if len(nal) <= self._max_nal:
                out.append(nal)
            else:
                logger.warning("NAL unit too large (%d bytes), skipping", len(nal))

        # 末尾（最後の start code から）は未確定として保持
        self._buf = buf[starts[-1] :]
        return out

    def flush(self) -> list[bytes]:
        """バッファに残っている最後の NAL unit を強制出力する.

        ストリーム終了時に呼び出す。
        """
        if len(self._buf) < 5:
            self._buf.clear()
            return []

        nal = bytes(self._buf)
        self._buf.clear()

        # 3-byte start code → 4-byte に正規化
        if nal[:3] == self.START_CODE_3 and nal[3:4] != b"\x00":
            nal = self.START_CODE_4 + nal[3:]

        if nal[:4] == self.START_CODE_4:
            return [nal]
        return []
