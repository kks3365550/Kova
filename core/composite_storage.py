# -*- coding: utf-8 -*-
"""
core/composite_storage.py
Production Composite Storage Facade providing unified logical interface over:
1. Pack Containers (`packs/*.pack` + `packs/*.idx`)
2. Individual Blobs (`blobs/xx/{sha256}.blob`)
Dual-Read: Pack in-memory index first -> Individual Blob fallback
"""

import zlib
import hashlib
from typing import Dict, Any, Optional, Tuple
from pathlib import Path

from core.pack_format import (
    PackContainerReader,
    PackConsistencyError,
    PackFormatError,
    PackRecoveryEngine
)

try:
    import zstandard as zstd
    HAS_ZSTD = True
except ImportError:
    HAS_ZSTD = False

ZSTD_MAGIC = b"\x28\xb5\x2f\xfd"


class CompositeStorageFacade:
    """
    Composite Storage Facade providing unified logical interface over Pack Containers and Individual Blobs.
    """
    def __init__(self, repo_dir: Path, crypto_engine: Optional[Any] = None, blob_storage: Optional[Any] = None):
        self.repo_dir = Path(repo_dir)
        self.blobs_dir = self.repo_dir / "blobs"
        self.packs_dir = self.repo_dir / "packs"
        self.crypto_engine = crypto_engine

        # Underlying traditional Individual BlobStorage
        if blob_storage is not None:
            self.blob_storage = blob_storage
        else:
            from core.storage import BlobStorage
            self.blob_storage = BlobStorage(str(self.repo_dir), crypto_engine=crypto_engine)

        # Pack Readers: pack_id -> PackContainerReader
        self.pack_readers: Dict[str, PackContainerReader] = {}
        # Global Pack Index Cache: sha256 -> pack_id
        self.pack_index_map: Dict[str, str] = {}

        self.reload_packs()

    def reload_packs(self):
        """Scans packs_dir and builds unified in-memory lookup map."""
        # 1. Build new maps in temporary dictionaries to avoid race conditions
        new_pack_readers: Dict[str, PackContainerReader] = {}
        new_pack_index_map: Dict[str, str] = {}

        if not self.packs_dir.exists():
            # Atomic swap even if empty to ensure consistency
            self.pack_readers = new_pack_readers
            self.pack_index_map = new_pack_index_map
            return

        for idx_file in self.packs_dir.glob("*.idx"):
            pack_file = idx_file.with_suffix(".pack")
            if not pack_file.exists():
                continue

            pack_id = idx_file.stem
            try:
                reader = PackContainerReader(pack_file, idx_file)
                new_pack_readers[pack_id] = reader
                for chunk_hash in reader.index.keys():
                    new_pack_index_map[chunk_hash.lower()] = pack_id
            except (PackConsistencyError, PackFormatError):
                # Auto recovery attempt
                try:
                    PackRecoveryEngine.rebuild_index_from_pack(pack_file, idx_file)
                    reader = PackContainerReader(pack_file, idx_file)
                    new_pack_readers[pack_id] = reader
                    for chunk_hash in reader.index.keys():
                        new_pack_index_map[chunk_hash.lower()] = pack_id
                except Exception:
                    continue

        # 2. Atomic Swap: Replace references only after full scan is complete
        self.pack_readers = new_pack_readers
        self.pack_index_map = new_pack_index_map

    def has_blob(self, blob_id: str) -> bool:
        """
        Dual-Read exists check:
        1. Fast in-memory Pack index lookup O(1)
        2. Fallback to Individual BlobStorage
        """
        blob_id = blob_id.lower()
        if blob_id in self.pack_index_map:
            return True
        return self.blob_storage.has_blob(blob_id)

    def extract_blob_to_file(
        self,
        blob_id: str,
        dest_filepath: Path,
        verify_hash: bool = True
    ) -> bool:
        """
        Dual-Read restore check:
        1. If in Pack Container: Read slice, decompress, write dest
        2. If not in Pack: Fallback to BlobStorage.extract_blob_to_file
        """
        dest_filepath = Path(dest_filepath)
        dest_filepath.parent.mkdir(parents=True, exist_ok=True)
        blob_id = blob_id.lower()

        # 1. Pack Container branch
        if blob_id in self.pack_index_map:
            pack_id = self.pack_index_map[blob_id]
            reader = self.pack_readers.get(pack_id)
            if reader:
                payload = reader.read_chunk(blob_id, verify=verify_hash)
                decompressed_data = self._unpack_payload(payload, blob_id)
                with open(dest_filepath, "wb") as f:
                    f.write(decompressed_data)
                return True

        # 2. Individual Blob branch fallback
        if self.blob_storage.has_blob(blob_id):
            return self.blob_storage.extract_blob_to_file(
                blob_id, str(dest_filepath), verify_hash=verify_hash
            )

        raise FileNotFoundError(f"Blob not found in Composite Storage: {blob_id}")

    def read_blob_bytes(self, blob_id: str) -> bytes:
        """
        Dual-Read raw decompressed bytes fetch:
        1. If in Pack: read and unpack
        2. If not in Pack: delegate to BlobStorage.read_blob_bytes
        """
        blob_id = blob_id.lower()
        if blob_id in self.pack_index_map:
            pack_id = self.pack_index_map[blob_id]
            reader = self.pack_readers.get(pack_id)
            if reader:
                payload = reader.read_chunk(blob_id, verify=False)
                return self._unpack_payload(payload, blob_id)

        return self.blob_storage.read_blob_bytes(blob_id)

    def verify_blob(self, blob_id: str) -> Tuple[bool, Optional[str], int]:
        """
        Dual-Read verification check:
        1. If in Pack: verify CRC32/SHA256 and unpack size
        2. If in Individual Blob: delegate to BlobStorage.verify_blob
        """
        blob_id = blob_id.lower()

        # 1. Pack Container branch
        if blob_id in self.pack_index_map:
            pack_id = self.pack_index_map[blob_id]
            reader = self.pack_readers.get(pack_id)
            if reader:
                try:
                    payload = reader.read_chunk(blob_id, verify=True)
                    unpacked = self._unpack_payload(payload, blob_id)
                    calc_sha = hashlib.sha256(unpacked).hexdigest()
                    if calc_sha.lower() != blob_id:
                        return False, f"Hash mismatch in Pack {pack_id}: {calc_sha}", 0
                    return True, None, len(unpacked)
                except Exception as e:
                    return False, f"Pack verification error ({pack_id}): {str(e)}", 0

        # 2. Individual Blob branch fallback
        if self.blob_storage.has_blob(blob_id):
            return self.blob_storage.verify_blob(blob_id)

        return False, f"블롭이 저장소에 존재하지 않음: {blob_id}", 0

    def _unpack_payload(self, raw_bytes: bytes, blob_id: str) -> bytes:
        """Handles decompression / encryption if chunk was packed in compressed form."""
        if not raw_bytes:
            return b""

        # Check encryption header
        if raw_bytes.startswith(b"ENC\x01"):
            if not self.crypto_engine:
                raise RuntimeError("복호화 키 필요")
            return self.crypto_engine.decrypt_blob_data(raw_bytes, blob_id)

        # Check zstd
        if raw_bytes.startswith(ZSTD_MAGIC) and HAS_ZSTD:
            dctx = zstd.ZstdDecompressor()
            return dctx.decompress(raw_bytes)

        # Check zlib
        try:
            return zlib.decompress(raw_bytes)
        except Exception:
            return raw_bytes
