"""
Track 2-8: Three Chunking Adapters implementing the Common Contract.
1. WholeFileAdapter
2. FixedBlockAdapter
3. FastCDCAdapter
"""

import os
import hashlib
from typing import Optional, Callable, Dict, Any, List
from .contract import (
    ChunkingAdapter, ChunkItem, ManifestEntry,
    FileMutatedError, RestoreError, ChunkMissingError,
    ChunkCorruptedError, ManifestInvalidError
)

try:
    import fastcdc
except ImportError:
    fastcdc = None


class BaseAdapter(ChunkingAdapter):
    """공통 복원 및 파일 변이 감지 로직을 공유하는 기저 클래스"""

    def _verify_file_stability(self, filepath: str, initial_stat: os.stat_result):
        """청킹 도중 파일 크기나 mtime이 변경되었는지 검증"""
        try:
            curr_stat = os.stat(filepath)
            if curr_stat.st_size != initial_stat.st_size or curr_stat.st_mtime != initial_stat.st_mtime:
                raise FileMutatedError(
                    f"파일 '{filepath}'이 청킹 도중 변경되었습니다 (Size: {initial_stat.st_size} -> {curr_stat.st_size}, mtime 변동)"
                )
        except FileNotFoundError:
            raise FileMutatedError(f"파일 '{filepath}'이 청킹 도중 삭제되었습니다.")

    def restore_file(
        self,
        manifest: ManifestEntry,
        chunk_fetch_cb: Callable[[str], bytes],
        output_filepath: str
    ) -> bool:
        """
        계약에 따른 결정론적 복원:
        1. Manifest 계약 무결성 검증 (오프셋 순서, 길이 합)
        2. 임시 파일에 청크 순차 인출 및 조립
        3. 복원 도중 SHA-256 누적 계산
        4. 누락/변조 감지 시 즉시 실패 및 부분 파일 삭제
        5. 최종 SHA-256 검증 성공 시 원자적 승격
        """
        manifest.validate_contract()

        temp_output = output_filepath + ".tmp_restore"
        sha = hashlib.sha256()
        total_restored = 0

        try:
            with open(temp_output, "wb") as out_f:
                for idx, c in enumerate(manifest.chunks):
                    # 1. 청크 인출
                    try:
                        raw_data = chunk_fetch_cb(c.chunk_id)
                    except Exception as e:
                        raise ChunkMissingError(f"청크 {c.chunk_id} 인출 실패: {e}")

                    if raw_data is None:
                        raise ChunkMissingError(f"청크 {c.chunk_id}를 찾을 수 없습니다.")

                    # 2. 청크 무결성 및 길이 검증
                    if len(raw_data) != c.length:
                        raise ChunkCorruptedError(
                            f"청크 {c.chunk_id} 길이 불일치 (기대: {c.length}, 실제: {len(raw_data)}) - 잘린 청크 감지"
                        )

                    actual_hash = hashlib.sha256(raw_data).hexdigest()
                    if actual_hash != c.content_hash:
                        raise ChunkCorruptedError(
                            f"청크 {c.chunk_id} 해시 불일치 (기대: {c.content_hash}, 실제: {actual_hash}) - 변조 청크 감지"
                        )

                    out_f.write(raw_data)
                    sha.update(raw_data)
                    total_restored += len(raw_data)

            # 3. 전체 파일 SHA-256 무결성 검증
            final_sha = sha.hexdigest()
            if final_sha != manifest.file_sha256:
                raise RestoreError(
                    f"복원된 파일의 최종 SHA-256 불일치 (기대: {manifest.file_sha256}, 실제: {final_sha})"
                )

            if total_restored != manifest.original_size:
                raise RestoreError(
                    f"복원된 파일의 크기 불일치 (기대: {manifest.original_size}, 실제: {total_restored})"
                )

            # 원자적 이동
            os.replace(temp_output, output_filepath)
            return True

        except Exception:
            # 실패 시 부분 복원 파일 즉시 격리/삭제 (오인 방지)
            if os.path.exists(temp_output):
                try:
                    os.remove(temp_output)
                except OSError:
                    pass
            raise


class WholeFileAdapter(BaseAdapter):
    """
    Whole-file CAS 전략 어댑터.
    - 0바이트 파일: 0개 청크
    - 1바이트 이상: 정확히 1개의 단일 청크 (offset=0, length=filesize)
    """

    @property
    def strategy_id(self) -> str:
        return "whole_file"

    def chunk_file(
        self,
        filepath: str,
        chunk_sink_cb: Optional[Callable[[ChunkItem, bytes], None]] = None
    ) -> ManifestEntry:
        init_st = os.stat(filepath)
        file_size = init_st.st_size

        if file_size == 0:
            return ManifestEntry(
                strategy_id=self.strategy_id,
                original_size=0,
                file_sha256=hashlib.sha256(b"").hexdigest(),
                chunks=[]
            )

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

        manifest = ManifestEntry(
            strategy_id=self.strategy_id,
            original_size=file_size,
            file_sha256=chunk_hash,
            chunks=[item]
        )
        manifest.validate_contract()
        return manifest


class FixedBlockAdapter(BaseAdapter):
    """
    Fixed Block 전략 어댑터 (기본 4MB, 임의 블록 크기 지정 가능).
    - 마지막 청크는 블록 크기 미만의 나머지 바이트를 온전히 보존.
    """

    def __init__(self, block_size: int = 4 * 1024 * 1024):
        self._block_size = block_size

    @property
    def strategy_id(self) -> str:
        return f"fixed_{self._block_size}"

    def chunk_file(
        self,
        filepath: str,
        chunk_sink_cb: Optional[Callable[[ChunkItem, bytes], None]] = None
    ) -> ManifestEntry:
        init_st = os.stat(filepath)
        file_size = init_st.st_size

        if file_size == 0:
            return ManifestEntry(
                strategy_id=self.strategy_id,
                original_size=0,
                file_sha256=hashlib.sha256(b"").hexdigest(),
                chunks=[]
            )

        chunks: List[ChunkItem] = []
        file_sha = hashlib.sha256()
        curr_offset = 0

        with open(filepath, "rb") as f:
            while True:
                data = f.read(self._block_size)
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

        manifest = ManifestEntry(
            strategy_id=self.strategy_id,
            original_size=file_size,
            file_sha256=file_sha.hexdigest(),
            chunks=chunks
        )
        manifest.validate_contract()
        return manifest


class FastCDCAdapter(BaseAdapter):
    """
    FastCDC 전략 어댑터 (min, avg, max 크기 파라미터화).
    - Gear Hashing 기반 가변 청킹 수행 및 Contract 준수 보장.
    """

    def __init__(
        self,
        min_size: int = 1 * 1024 * 1024,
        avg_size: int = 4 * 1024 * 1024,
        max_size: int = 8 * 1024 * 1024
    ):
        if fastcdc is None:
            raise RuntimeError("fastcdc 패키지가 설치되어 있지 않습니다.")
        self._min_size = min_size
        self._avg_size = avg_size
        self._max_size = max_size

    @property
    def strategy_id(self) -> str:
        return f"fastcdc_min{self._min_size}_avg{self._avg_size}_max{self._max_size}"

    def chunk_file(
        self,
        filepath: str,
        chunk_sink_cb: Optional[Callable[[ChunkItem, bytes], None]] = None
    ) -> ManifestEntry:
        init_st = os.stat(filepath)
        file_size = init_st.st_size

        if file_size == 0:
            return ManifestEntry(
                strategy_id=self.strategy_id,
                original_size=0,
                file_sha256=hashlib.sha256(b"").hexdigest(),
                chunks=[]
            )

        chunks: List[ChunkItem] = []
        file_sha = hashlib.sha256()

        with open(filepath, "rb") as f:
            for c in fastcdc.fastcdc(f, min_size=self._min_size, avg_size=self._avg_size, max_size=self._max_size):
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

        manifest = ManifestEntry(
            strategy_id=self.strategy_id,
            original_size=file_size,
            file_sha256=file_sha.hexdigest(),
            chunks=chunks
        )
        manifest.validate_contract()
        return manifest
