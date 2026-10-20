# -*- coding: utf-8 -*-
"""
tests/test_block_stream.py: 초고속 LBA 블록 스트리밍 엔진 단위 및 무결성 테스트
"""

import os
import zlib
import struct
import tempfile
import pytest

from core.block_stream import (
    pack_header,
    unpack_header,
    pack_chunk_header,
    unpack_chunk_header,
    get_allocated_cluster_runs,
    HEADER_SIZE,
    CHUNK_HEADER_SIZE,
    MAGIC,
    VERSION,
    ZSTD_AVAILABLE
)

if ZSTD_AVAILABLE:
    import zstandard as zstd


class TestBlockStreamHeader:
    def test_header_pack_unpack_success(self):
        hdr = pack_header(
            cluster_size=4096,
            total_clusters=244140625,
            used_clusters=75000000,
            vsn="5AEF-9960",
            source_label="SamsungTestOS"
        )
        assert len(hdr) == HEADER_SIZE

        meta = unpack_header(hdr)
        assert meta["version"] == VERSION
        assert meta["cluster_size"] == 4096
        assert meta["total_clusters"] == 244140625
        assert meta["used_clusters"] == 75000000
        assert meta["vsn"] == "5AEF-9960"
        assert meta["source_label"] == "SamsungTestOS"

    def test_header_crc_corruption_detection(self):
        hdr = bytearray(pack_header(4096, 1000, 500, "1234-5678", "CorruptTest"))
        # 10번째 바이트 변조
        hdr[10] ^= 0xFF
        with pytest.raises(ValueError, match="CRC32"):
            unpack_header(bytes(hdr))

    def test_header_magic_mismatch(self):
        hdr = bytearray(pack_header(4096, 1000, 500, "1234-5678", "MagicTest"))
        hdr[0:4] = b"FAKE"
        with pytest.raises(ValueError, match="매직"):
            unpack_header(bytes(hdr))


class TestBlockStreamChunk:
    def test_chunk_header_round_trip(self):
        lcn = 1048576
        cluster_cnt = 2048
        uncomp_sz = 2048 * 4096
        comp_sz = 123456
        crc = 0xA1B2C3D4

        packed = pack_chunk_header(lcn, cluster_cnt, uncomp_sz, comp_sz, crc)
        assert len(packed) == CHUNK_HEADER_SIZE

        r_lcn, r_cnt, r_uncomp, r_comp, r_crc = unpack_chunk_header(packed)
        assert r_lcn == lcn
        assert r_cnt == cluster_cnt
        assert r_uncomp == uncomp_sz
        assert r_comp == comp_sz
        assert r_crc == crc


class TestAllocatedClusterRuns:
    def test_empty_bitmap(self):
        runs = get_allocated_cluster_runs(b"\x00\x00\x00\x00", cluster_size=4096)
        assert runs == []

    def test_sparse_bitmap_parsing(self):
        # 0b00000111 (LCN 0, 1, 2 할당)
        # 0b00000000 (LCN 8~15 미할당)
        # 0b00000010 (LCN 17 할당)
        bitmap = bytes([0b00000111, 0b00000000, 0b00000010])
        runs = get_allocated_cluster_runs(bitmap, cluster_size=4096, max_chunk_bytes=1024 * 1024)

        assert len(runs) == 2
        assert runs[0] == (0, 3)
        assert runs[1] == (17, 1)

    def test_large_chunk_splitting(self):
        # 32개 클러스터 연속 할당 (4바이트 모두 0xFF)
        bitmap = b"\xFF" * 4
        # max_chunk_bytes = 16KB (4 클러스터당 1청크)
        runs = get_allocated_cluster_runs(bitmap, cluster_size=4096, max_chunk_bytes=16 * 1024)

        assert len(runs) == 8  # 32 / 4 = 8 청크
        for i, (lcn, count) in enumerate(runs):
            assert lcn == i * 4
            assert count == 4


class TestZstdStreamRoundTrip:
    @pytest.mark.skipif(not ZSTD_AVAILABLE, reason="zstandard 미설치 환경 건너뜀")
    def test_full_stream_file_round_trip(self):
        """임시 파일 기반 블록 스트림 생성 및 복원 무결성 100% 검증"""
        sample_clusters = 5
        cluster_size = 4096
        total_size = sample_clusters * cluster_size
        test_payload = b"DIRECT_NVME_RAW_BLOCK_STREAM_DATA_" * (total_size // 34 + 1)
        test_payload = test_payload[:total_size]

        with tempfile.NamedTemporaryFile(suffix=".qblk", delete=False) as f_img:
            img_path = f_img.name

        try:
            # 1. 파일에 헤더 및 압축 청크 쓰기
            compressor = zstd.ZstdCompressor(level=1)
            comp_data = compressor.compress(test_payload)
            crc = zlib.crc32(test_payload) & 0xFFFFFFFF

            with open(img_path, "wb") as f_out:
                f_out.write(pack_header(cluster_size, 1000, sample_clusters, "9999-AAAA", "UnitTest"))
                f_out.write(pack_chunk_header(100, sample_clusters, len(test_payload), len(comp_data), crc))
                f_out.write(comp_data)

            # 2. 파일에서 읽고 역압축 검증
            with open(img_path, "rb") as f_in:
                meta = unpack_header(f_in.read(HEADER_SIZE))
                assert meta["cluster_size"] == cluster_size
                assert meta["used_clusters"] == sample_clusters
                assert meta["vsn"] == "9999-AAAA"

                chk_hdr = f_in.read(CHUNK_HEADER_SIZE)
                lcn, count, uncomp_sz, comp_sz, st_crc = unpack_chunk_header(chk_hdr)
                assert lcn == 100
                assert count == sample_clusters
                assert uncomp_sz == total_size
                assert st_crc == crc

                c_bytes = f_in.read(comp_sz)
                decompressor = zstd.ZstdDecompressor()
                restored = decompressor.decompress(c_bytes, max_output_size=uncomp_sz)
                assert restored == test_payload
                assert zlib.crc32(restored) & 0xFFFFFFFF == st_crc

        finally:
            if os.path.exists(img_path):
                os.remove(img_path)
