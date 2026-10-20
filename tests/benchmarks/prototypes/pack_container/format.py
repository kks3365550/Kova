"""
Track 2-9: Pack Container Format & Storage Abstract Interfaces.

원칙:
Chunking Strategy != Storage Layout
- StorageAdapter 추상 인터페이스 정의
- Pack Container 바이너리 레코드 포맷 (Header, Record, Footer)
- ChunkIndex 데이터베이스 / 인메모리 인덱스 규격
"""

import os
import struct
import hashlib
from abc import ABC, abstractmethod
from typing import Dict, Any, List, Optional, Tuple, Iterator
from dataclasses import dataclass


PACK_MAGIC = b"BKPK"  # BackPack Magic Header
PACK_VERSION = 1
MAX_CONTAINER_SIZE = 1024 * 1024 * 1024  # 1GB 단위 컨테이너 분할


class StorageError(Exception):
    """저장소 계층 기본 예외"""
    pass


class ChunkNotFoundError(StorageError):
    """청크를 찾을 수 없을 때 발생하는 예외"""
    pass


class StorageCorruptionError(StorageError):
    """컨테이너 손상, 체크섬 불일치, 불완전 레코드 감지 시 발생하는 예외 (Fail-Closed)"""
    pass


@dataclass(frozen=True)
class IndexEntry:
    """인덱스에 보관되는 청크 물리적 위치 정보"""
    chunk_id: str        # SHA-256 (64 hex characters)
    container_id: str    # e.g., "pack_0000.bin"
    offset: int          # 컨테이너 파일 내 시작 바이트 오프셋
    length: int          # 페이로드 바이트 길이
    content_hash: str    # 무결성 검증용 SHA-256


class StorageAdapter(ABC):
    """물리 저장소 레이아웃 추상 인터페이스"""

    @property
    @abstractmethod
    def layout_name(self) -> str:
        pass

    @abstractmethod
    def put_chunk(self, chunk_id: str, data: bytes) -> Tuple[bool, int]:
        """
        청크 저장. 이미 존재하면 (False, 0) 반환하여 중복 저장 방지.
        신규 저장 시 (True, stored_bytes) 반환.
        """
        pass

    @abstractmethod
    def get_chunk(self, chunk_id: str) -> bytes:
        """청크 페이로드 조회 및 SHA-256 검증 후 반환. 손상 시 StorageCorruptionError."""
        pass

    @abstractmethod
    def has_chunk(self, chunk_id: str) -> bool:
        """청크 존재 여부 O(1) 확인"""
        pass

    @abstractmethod
    def flush(self) -> None:
        """버퍼 및 인덱스 영구 동기화"""
        pass

    @abstractmethod
    def get_stats(self) -> Dict[str, Any]:
        """저장소 물리 통계 (총 청크 수, 파일/컨테이너 개수, 물리 디스크 바이트, 인덱스 바이트)"""
        pass
