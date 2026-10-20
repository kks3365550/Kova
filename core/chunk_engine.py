# -*- coding: utf-8 -*-
"""
core/chunk_engine.py: 청킹 전략 어댑터 및 정책 선택 엔진 (v2.10.0)
Track 2-12: Chunking Strategy Pipeline & Multi-chunk Metadata Contract
- WholeFileAdapter: 16MB 미만 소형 파일 또는 단일 CAS 대상
- FixedBlockAdapter: 4MB 고정 블록 슬라이싱 (VM/DB/고정 크기 파일)
- FastCDCAdapter: Gear Hashing 기반 가변 청킹 (가변 삽입/삭제 파일)
- ChunkPolicySelector: 크기 및 확장자/변이 신호 기반 전략 자동 선택
"""

import os
import hashlib
from abc import ABC, abstractmethod
from typing import Optional, Callable, Dict, Any, List, Tuple
from dataclasses import dataclass

try:
    import fastcdc
except ImportError:
    fastcdc = None


CHUNK_THRESHOLD_BYTES = 16 * 1024 * 1024  # 16MB 기본 임계치


@dataclass(frozen=True)
class ChunkItem:
    chunk_id: str        # Content Hash (SHA-256 hexdigest)
    offset: int          # 원본 파일 내 시작 오프셋 (0부터 시작)
    length: int          # 청크의 바이트 길이
    content_hash: str    # 페이로드 검증 해시


class FileMutatedError(Exception):
    """청킹 작업 도중 소스 파일의 크기나 mtime이 변경되었을 때 발생하는 예외."""
    pass


class BaseChunkAdapter(ABC):
    @property
    @abstractmethod
    def strategy_id(self) -> str:
        pass

    def _verify_file_stability(self, filepath: str, initial_stat: os.stat_result):
        try:
            curr_stat = os.stat(filepath)
            if curr_stat.st_size != initial_stat.st_size or curr_stat.st_mtime != initial_stat.st_mtime:
                raise FileMutatedError(
                    f"파일 '{filepath}'이 청킹 도중 변경되었습니다 (Size: {initial_stat.st_size} -> {curr_stat.st_size})"
                )
        except FileNotFoundError:
            raise FileMutatedError(f"파일 '{filepath}'이 청킹 도중 삭제되었습니다.")

    @abstractmethod
    def chunk_file(
        self,
        filepath: str,
        chunk_sink_cb: Optional[Callable[[ChunkItem, bytes], None]] = None
    ) -> Tuple[str, List[ChunkItem]]:
        """
        파일을 청킹하고 전체 파일 SHA-256 및 ChunkItem 목록을 반환.
        반환: (file_sha256, chunk_items)
        """
        pass


class WholeFileAdapter(BaseChunkAdapter):
    """16MB 미만 소형 파일 및 레거시 Whole-file CAS 전략"""
    @property
    def strategy_id(self) -> str:
        return "whole_file"

    def chunk_file(
        self,
        filepath: str,
        chunk_sink_cb: Optional[Callable[[ChunkItem, bytes], None]] = None
    ) -> Tuple[str, List[ChunkItem]]:
        init_st = os.stat(filepath)
        file_size = init_st.st_size

        if file_size == 0:
            return hashlib.sha256(b"").hexdigest(), []

        sha = hashlib.sha256()
        with open(filepath, "rb") as f:
            data = f.read()
            sha.update(data)

        self._verify_file_stability(filepath, init_st)
        chunk_hash = sha.hexdigest()
        item = ChunkItem(
            chunk_id=chunk_hash,
            offset=0,
            length=file_size,
            content_hash=chunk_hash
        )
        if chunk_sink_cb:
            chunk_sink_cb(item, data)
        return chunk_hash, [item]


class FixedBlockAdapter(BaseChunkAdapter):
    """4MB Fixed Block 전략 (VM/DB/고정 슬라이싱)"""
    def __init__(self, block_size: int = 4 * 1024 * 1024):
        self.block_size = block_size

    @property
    def strategy_id(self) -> str:
        return f"fixed_{self.block_size}"

    def chunk_file(
        self,
        filepath: str,
        chunk_sink_cb: Optional[Callable[[ChunkItem, bytes], None]] = None
    ) -> Tuple[str, List[ChunkItem]]:
        init_st = os.stat(filepath)
        file_size = init_st.st_size

        if file_size == 0:
            return hashlib.sha256(b"").hexdigest(), []

        chunks: List[ChunkItem] = []
        file_sha = hashlib.sha256()
        curr_offset = 0

        with open(filepath, "rb") as f:
            while True:
                data = f.read(self.block_size)
                if not data:
                    break
                file_sha.update(data)
                c_len = len(data)
                c_hash = hashlib.sha256(data).hexdigest()
                item = ChunkItem(
                    chunk_id=c_hash,
                    offset=curr_offset,
                    length=c_len,
                    content_hash=c_hash
                )
                chunks.append(item)
                if chunk_sink_cb:
                    chunk_sink_cb(item, data)
                curr_offset += c_len

        self._verify_file_stability(filepath, init_st)
        return file_sha.hexdigest(), chunks


class FastCDCAdapter(BaseChunkAdapter):
    """FastCDC 가변 청킹 전략 (삽입/삭제 빈번 파일)"""
    def __init__(
        self,
        min_size: int = 1 * 1024 * 1024,
        avg_size: int = 4 * 1024 * 1024,
        max_size: int = 8 * 1024 * 1024
    ):
        if fastcdc is None:
            raise RuntimeError("fastcdc 패키지가 설치되어 있지 않습니다.")
        if not (0 < min_size <= avg_size <= max_size):
            raise ValueError(f"유효하지 않은 FastCDC 청크 크기 규격: min={min_size}, avg={avg_size}, max={max_size}")
        self.min_size = min_size
        self.avg_size = avg_size
        self.max_size = max_size

    @property
    def strategy_id(self) -> str:
        return f"fastcdc_min{self.min_size}_avg{self.avg_size}_max{self.max_size}"

    def chunk_file(
        self,
        filepath: str,
        chunk_sink_cb: Optional[Callable[[ChunkItem, bytes], None]] = None
    ) -> Tuple[str, List[ChunkItem]]:
        init_st = os.stat(filepath)
        file_size = init_st.st_size

        if file_size == 0:
            return hashlib.sha256(b"").hexdigest(), []

        chunks: List[ChunkItem] = []
        file_sha = hashlib.sha256()

        with open(filepath, "rb") as f:
            for c in fastcdc.fastcdc(f, min_size=self.min_size, avg_size=self.avg_size, max_size=self.max_size):
                f.seek(c.offset)
                data = f.read(c.length)
                file_sha.update(data)
                c_hash = hashlib.sha256(data).hexdigest()
                item = ChunkItem(
                    chunk_id=c_hash,
                    offset=c.offset,
                    length=c.length,
                    content_hash=c_hash
                )
                chunks.append(item)
                if chunk_sink_cb:
                    chunk_sink_cb(item, data)

        self._verify_file_stability(filepath, init_st)
        return file_sha.hexdigest(), chunks


class ChunkPolicySelector:
    """
    Track 2-7에 기반한 파일별 최적 청킹 전략 동적 선택기:
    1. 파일 크기 < CHUNK_THRESHOLD (16MB): WholeFileAdapter
    2. 파일 크기 >= 16MB & 고정 블록 확장자(.vhdx, .vmdk, .mdf, .db, .iso): FixedBlockAdapter (4MB)
    3. 파일 크기 >= 16MB & 일반 텍스트/로그/아카이브: FastCDCAdapter (사용 가능 시) 또는 FixedBlockAdapter
    """
    FIXED_EXTENSIONS = {".db", ".sqlite", ".mdf", ".raw", ".bin", ".dat", ".vhdx", ".vmdk", ".iso"}

    def __init__(self, threshold_bytes: int = CHUNK_THRESHOLD_BYTES):

        self.threshold_bytes = threshold_bytes
        self.whole_adapter = WholeFileAdapter()
        self.fixed_adapter = FixedBlockAdapter(4 * 1024 * 1024)
        self.fastcdc_adapter = FastCDCAdapter() if fastcdc is not None else None

    def select_adapter(self, filepath: str, file_size: int, prev_entry: Optional[Dict[str, Any]] = None) -> BaseChunkAdapter:
        if file_size < self.threshold_bytes:
            return self.whole_adapter

        ext = os.path.splitext(filepath)[1].lower()
        if ext in self.FIXED_EXTENSIONS:
            return self.fixed_adapter

        # FastCDC 사용 가능 시 가변 워크로드에 우선 적용
        if self.fastcdc_adapter is not None:
            return self.fastcdc_adapter

        return self.fixed_adapter
