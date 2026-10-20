"""
Track 2-9: Two Concrete Storage Adapters.
1. IndividualFileStore (청크당 개별 파일 저장: Baseline)
2. PackContainerStore (Pack Container + Index 분리 저장)
"""

import os
import struct
import hashlib
import zlib
import sqlite3
from typing import Dict, Any, List, Optional, Tuple
from .format import (
    StorageAdapter, StorageError, ChunkNotFoundError, StorageCorruptionError,
    IndexEntry, PACK_MAGIC, PACK_VERSION, MAX_CONTAINER_SIZE
)


class IndividualFileStore(StorageAdapter):
    """
    Individual Files Layout (현행 기준선):
    - 각 청크를 'chunks/xx/{sha256}.chk' 파일로 직접 저장.
    - 2,560개 청크 시 정확히 2,560개의 파일 및 256개 디렉토리 생성.
    """

    def __init__(self, repo_dir: str):
        self.repo_dir = repo_dir
        self.chunks_dir = os.path.join(repo_dir, "individual_chunks")
        os.makedirs(self.chunks_dir, exist_ok=True)
        for i in range(256):
            os.makedirs(os.path.join(self.chunks_dir, f"{i:02x}"), exist_ok=True)
        self._in_memory_cache = set()
        # 기존 청크 스캔
        self._scan_existing()

    @property
    def layout_name(self) -> str:
        return "individual_files"

    def _get_path(self, chunk_id: str) -> str:
        return os.path.join(self.chunks_dir, chunk_id[:2], f"{chunk_id}.chk")

    def _scan_existing(self):
        for root, _, files in os.walk(self.chunks_dir):
            for f in files:
                if f.endswith(".chk"):
                    self._in_memory_cache.add(f[:-4])

    def has_chunk(self, chunk_id: str) -> bool:
        return chunk_id in self._in_memory_cache

    def put_chunk(self, chunk_id: str, data: bytes) -> Tuple[bool, int]:
        if self.has_chunk(chunk_id):
            return False, 0

        p = self._get_path(chunk_id)
        with open(p, "wb") as f:
            f.write(data)
        self._in_memory_cache.add(chunk_id)
        return True, len(data)

    def get_chunk(self, chunk_id: str) -> bytes:
        if not self.has_chunk(chunk_id):
            raise ChunkNotFoundError(f"청크 '{chunk_id}'를 찾을 수 없습니다.")

        p = self._get_path(chunk_id)
        try:
            with open(p, "rb") as f:
                data = f.read()
        except FileNotFoundError:
            raise ChunkNotFoundError(f"청크 파일 '{p}' 부재")

        actual_hash = hashlib.sha256(data).hexdigest()
        if actual_hash != chunk_id:
            raise StorageCorruptionError(f"개별 파일 청크 손상 감지: {chunk_id}")
        return data

    def flush(self) -> None:
        pass

    def get_stats(self) -> Dict[str, Any]:
        total_files = 0
        total_bytes = 0
        for root, _, files in os.walk(self.chunks_dir):
            for f in files:
                if f.endswith(".chk"):
                    total_files += 1
                    total_bytes += os.path.getsize(os.path.join(root, f))
        return {
            "total_chunks": len(self._in_memory_cache),
            "physical_files_count": total_files,
            "physical_bytes": total_bytes,
            "index_bytes": 0,
            "total_disk_bytes": total_bytes
        }


# =========================================================================
# Pack Container Layout (Pack Payload + Index DB)
# =========================================================================

# Record Format:
# [Magic: 4B ("BKPK")]
# [Version: 1B]
# [ChunkID: 32B (Binary SHA-256)]
# [Length: 4B (uint32)]
# [Payload: Length bytes]
# [CRC32: 4B (Header + Payload CRC)]
RECORD_HEADER_FORMAT = ">4sB32sI"
RECORD_HEADER_LEN = struct.calcsize(RECORD_HEADER_FORMAT)  # 4 + 1 + 32 + 4 = 41 bytes


class PackContainerStore(StorageAdapter):
    """
    Pack Container Layout:
    - 여러 청크를 1개 또는 소수의 대형 컨테이너 파일(`pack_xxxx.bin`)에 순차 append.
    - 청크 위치 정보(container_id, offset, length, hash)는 SQLite 인덱스 DB 및 인메모리 캐시에서 O(1) 관리.
    """

    def __init__(self, repo_dir: str, max_pack_size: int = MAX_CONTAINER_SIZE):
        self.repo_dir = repo_dir
        self.packs_dir = os.path.join(repo_dir, "packs")
        os.makedirs(self.packs_dir, exist_ok=True)
        self.max_pack_size = max_pack_size

        self.db_path = os.path.join(repo_dir, "pack_index.db")
        self._init_index_db()

        self._in_memory_index: Dict[str, IndexEntry] = {}
        self._load_index_to_memory()

        self._current_pack_idx = 0
        self._current_pack_file: Optional[Any] = None
        self._current_pack_size = 0
        self._init_active_pack()

    @property
    def layout_name(self) -> str:
        return "pack_container"

    def _init_index_db(self):
        conn = sqlite3.connect(self.db_path)
        cur = conn.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS chunk_index (
                chunk_id TEXT PRIMARY KEY,
                container_id TEXT NOT NULL,
                offset INTEGER NOT NULL,
                length INTEGER NOT NULL,
                content_hash TEXT NOT NULL
            )
        """)
        conn.commit()
        conn.close()

    def _load_index_to_memory(self):
        conn = sqlite3.connect(self.db_path)
        cur = conn.cursor()
        cur.execute("SELECT chunk_id, container_id, offset, length, content_hash FROM chunk_index")
        for row in cur.fetchall():
            self._in_memory_index[row[0]] = IndexEntry(
                chunk_id=row[0],
                container_id=row[1],
                offset=row[2],
                length=row[3],
                content_hash=row[4]
            )
        conn.close()

    def _get_pack_name(self, idx: int) -> str:
        return f"pack_{idx:04d}.bin"

    def _init_active_pack(self):
        # 가장 최근 pack 찾기
        existing_packs = sorted([f for f in os.listdir(self.packs_dir) if f.startswith("pack_") and f.endswith(".bin")])
        if existing_packs:
            last_pack = existing_packs[-1]
            idx = int(last_pack.split("_")[1].split(".")[0])
            last_p = os.path.join(self.packs_dir, last_pack)
            size = os.path.getsize(last_p)
            if size < self.max_pack_size:
                self._current_pack_idx = idx
                self._current_pack_size = size
                return
            else:
                self._current_pack_idx = idx + 1
        else:
            self._current_pack_idx = 0
        self._current_pack_size = 0

    def has_chunk(self, chunk_id: str) -> bool:
        return chunk_id in self._in_memory_index

    def put_chunk(self, chunk_id: str, data: bytes) -> Tuple[bool, int]:
        # Deduplication check
        if self.has_chunk(chunk_id):
            return False, 0

        # 컨테이너 크기 확인 및 롤링
        record_len = RECORD_HEADER_LEN + len(data) + 4  # Header + Payload + CRC32
        if self._current_pack_size + record_len > self.max_pack_size:
            if self._current_pack_file:
                self._current_pack_file.close()
                self._current_pack_file = None
            self._current_pack_idx += 1
            self._current_pack_size = 0

        pack_name = self._get_pack_name(self._current_pack_idx)
        pack_path = os.path.join(self.packs_dir, pack_name)

        if self._current_pack_file is None:
            self._current_pack_file = open(pack_path, "a+b")
            self._current_pack_file.seek(0, os.SEEK_END)
            self._current_pack_size = self._current_pack_file.tell()

        record_offset = self._current_pack_size

        # Binary Header 구성
        bin_hash = bytes.fromhex(chunk_id)
        header = struct.pack(RECORD_HEADER_FORMAT, PACK_MAGIC, PACK_VERSION, bin_hash, len(data))
        crc = struct.pack(">I", zlib.crc32(header + data) & 0xFFFFFFFF)

        # 컨테이너에 기록
        self._current_pack_file.write(header)
        self._current_pack_file.write(data)
        self._current_pack_file.write(crc)
        self._current_pack_file.flush()
        self._current_pack_size += record_len

        # 인덱스 등록
        entry = IndexEntry(
            chunk_id=chunk_id,
            container_id=pack_name,
            offset=record_offset,
            length=record_len,
            content_hash=chunk_id
        )
        self._in_memory_index[chunk_id] = entry

        # DB 트랜잭션 기록
        conn = sqlite3.connect(self.db_path)
        cur = conn.cursor()
        cur.execute("INSERT OR REPLACE INTO chunk_index VALUES (?, ?, ?, ?, ?)",
                    (entry.chunk_id, entry.container_id, entry.offset, entry.length, entry.content_hash))
        conn.commit()
        conn.close()

        return True, record_len

    def get_chunk(self, chunk_id: str) -> bytes:
        entry = self._in_memory_index.get(chunk_id)
        if not entry:
            raise ChunkNotFoundError(f"청크 '{chunk_id}'가 인덱스에 없습니다.")

        pack_path = os.path.join(self.packs_dir, entry.container_id)
        if not os.path.exists(pack_path):
            raise StorageCorruptionError(f"컨테이너 파일 부재: {entry.container_id}")

        with open(pack_path, "rb") as f:
            f.seek(entry.offset)
            record_bytes = f.read(entry.length)

        if len(record_bytes) < entry.length:
            raise StorageCorruptionError(f"청크 {chunk_id} 잘림 감지 (불완전한 레코드)")

        header = record_bytes[:RECORD_HEADER_LEN]
        payload = record_bytes[RECORD_HEADER_LEN:-4]
        crc_bytes = record_bytes[-4:]

        magic, version, bin_hash, p_len = struct.unpack(RECORD_HEADER_FORMAT, header)
        if magic != PACK_MAGIC or version != PACK_VERSION:
            raise StorageCorruptionError(f"청크 {chunk_id} 매직/버전 손상")

        # CRC32 검증
        expected_crc = struct.pack(">I", zlib.crc32(header + payload) & 0xFFFFFFFF)
        if crc_bytes != expected_crc:
            raise StorageCorruptionError(f"청크 {chunk_id} CRC32 체크섬 불일치")

        # SHA-256 검증
        actual_hash = hashlib.sha256(payload).hexdigest()
        if actual_hash != chunk_id:
            raise StorageCorruptionError(f"청크 {chunk_id} SHA-256 해시 불일치 (변조 감지)")

        return payload

    def flush(self) -> None:
        if self._current_pack_file:
            self._current_pack_file.flush()
            os.fsync(self._current_pack_file.fileno())

    def get_stats(self) -> Dict[str, Any]:
        self.flush()
        pack_files = [f for f in os.listdir(self.packs_dir) if f.startswith("pack_") and f.endswith(".bin")]
        total_pack_bytes = sum(os.path.getsize(os.path.join(self.packs_dir, f)) for f in pack_files)
        index_db_bytes = os.path.getsize(self.db_path) if os.path.exists(self.db_path) else 0

        return {
            "total_chunks": len(self._in_memory_index),
            "physical_files_count": len(pack_files) + 1,  # Packs + 1 Index DB
            "pack_files_count": len(pack_files),
            "physical_bytes": total_pack_bytes,
            "index_bytes": index_db_bytes,
            "total_disk_bytes": total_pack_bytes + index_db_bytes
        }
