"""
Track 2-8: Chunking Adapter Contract & Abstract Base Definitions.

독립 검증용 모듈 (core/* 무수정 원칙 준수)
- ChunkItem, ManifestEntry 정의
- 공통 ChunkingAdapter 추상 클래스
- 청킹 도중 파일 변경 감지 및 복원 무결성 예외 정의
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Iterator, Dict, Any, List, Optional, Callable
import hashlib
import os


class ChunkingError(Exception):
    """청킹 과정에서 발생하는 기본 예외"""
    pass


class FileMutatedError(ChunkingError):
    """청킹 도중 원본 파일 크기나 mtime이 변경되었을 때 발생하는 예외"""
    pass


class RestoreError(Exception):
    """복원 과정에서 발생하는 기본 예외 (부분 복원 파일 오인 방지)"""
    pass


class ChunkMissingError(RestoreError):
    """필요한 청크가 저장소에 누락되었을 때 발생하는 예외"""
    pass


class ChunkCorruptedError(RestoreError):
    """청크 데이터 해시 불일치 또는 잘림이 감지되었을 때 발생하는 예외"""
    pass


class ManifestInvalidError(RestoreError):
    """매니페스트 구조 또는 필드가 유효하지 않을 때 발생하는 예외"""
    pass


@dataclass(frozen=True)
class ChunkItem:
    """
    모든 청킹 어댑터가 생성해야 하는 청크 단위의 최소 불변 규격.
    """
    chunk_id: str        # 청크 고유 식별자 (content_hash와 동일)
    offset: int          # 원본 파일 내의 시작 바이트 오프셋 (0-indexed)
    length: int          # 청크의 바이트 길이
    content_hash: str    # 청크 페이로드의 SHA-256 해시 (hexdigest)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "chunk_id": self.chunk_id,
            "offset": self.offset,
            "length": self.length,
            "content_hash": self.content_hash,
        }

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "ChunkItem":
        return cls(
            chunk_id=d["chunk_id"],
            offset=d["offset"],
            length=d["length"],
            content_hash=d["content_hash"],
        )


@dataclass
class ManifestEntry:
    """
    파일 1개에 대한 스냅샷 메타데이터 매니페스트 규격 (v1).
    """
    schema_version: str = "v1"
    strategy_id: str = ""                # 'whole_file', 'fixed_4mb', 'fastcdc_4mb'
    original_size: int = 0               # 원본 파일 전체 크기 (bytes)
    file_sha256: str = ""                # 원본 파일 전체의 SHA-256 해시
    chunks: List[ChunkItem] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "strategy_id": self.strategy_id,
            "original_size": self.original_size,
            "file_sha256": self.file_sha256,
            "chunks": [c.to_dict() for c in self.chunks],
        }

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "ManifestEntry":
        if "schema_version" not in d or "strategy_id" not in d or "original_size" not in d or "file_sha256" not in d:
            raise ManifestInvalidError("Manifest 필수 헤더 필드가 누락되었습니다.")
        chunks = [ChunkItem.from_dict(c) for c in d.get("chunks", [])]
        return cls(
            schema_version=d["schema_version"],
            strategy_id=d["strategy_id"],
            original_size=d["original_size"],
            file_sha256=d["file_sha256"],
            chunks=chunks,
        )

    def validate_contract(self):
        """
        계약의 수학적 무결성 검증:
        1. 빈 파일인 경우 chunks는 빈 리스트여야 함
        2. chunks의 length 합은 original_size와 일치해야 함
        3. 각 chunk의 offset은 이전 chunk의 (offset + length)와 빈틈/중첩 없이 일치해야 함
        """
        if self.original_size == 0:
            if len(self.chunks) != 0:
                raise ManifestInvalidError("0바이트 빈 파일의 chunks 리스트는 비어 있어야 합니다.")
            return

        expected_offset = 0
        total_len = 0
        for i, c in enumerate(self.chunks):
            if c.offset != expected_offset:
                raise ManifestInvalidError(f"청크 {i}의 오프셋({c.offset})이 예상 오프셋({expected_offset})과 불일치합니다.")
            if c.length <= 0:
                raise ManifestInvalidError(f"청크 {i}의 길이({c.length})는 0보다 커야 합니다.")
            expected_offset += c.length
            total_len += c.length

        if total_len != self.original_size:
            raise ManifestInvalidError(f"청크 길이 합({total_len})이 원본 크기({self.original_size})와 불일치합니다.")


class ChunkingAdapter(ABC):
    """모든 청킹 전략이 준수해야 하는 최소 공통 Contract"""

    @property
    @abstractmethod
    def strategy_id(self) -> str:
        """전략 식별자 (예: 'whole_file', 'fixed_4mb', 'fastcdc_4mb')"""
        pass

    @abstractmethod
    def chunk_file(
        self,
        filepath: str,
        chunk_sink_cb: Optional[Callable[[ChunkItem, bytes], None]] = None
    ) -> ManifestEntry:
        """
        파일을 청킹하고 ManifestEntry를 생성.
        chunk_sink_cb가 주어지면 (ChunkItem, raw_bytes)를 저장소로 실시간 콜백.
        청킹 도중 파일 변이가 감지되면 FileMutatedError 발생.
        """
        pass

    @abstractmethod
    def restore_file(
        self,
        manifest: ManifestEntry,
        chunk_fetch_cb: Callable[[str], bytes],
        output_filepath: str
    ) -> bool:
        """
        ManifestEntry와 청크 인출 콜백을 통해 원본 파일을 복원.
        복원 완료 후 원본 file_sha256과 복원 파일 sha256을 전수 대조하여 100% 일치 확인.
        실패 시 부분 파일은 안전하게 삭제(격리)하고 예외 발생.
        """
        pass
