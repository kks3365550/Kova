# -*- coding: utf-8 -*-
"""
tests/benchmarks/prototypes/pack_consistency/pack_format.py
Track 2-10: Pack Container Writer & Indexer with Atomic Commit and Rebuilding
"""

import os
import struct
import zlib
import hashlib
from typing import Dict, Any, Optional, Tuple, List
from pathlib import Path


PACK_MAGIC = b"PCK1"
INDEX_MAGIC = b"PIDX"
HEADER_FORMAT = "<4sII32s"  # magic(4), chunk_id(int), payload_len(int), sha256(32 bytes)
HEADER_SIZE = struct.calcsize(HEADER_FORMAT)
FOOTER_FORMAT = "<4sI"      # 'PEND'(4), crc32(4 bytes)
FOOTER_SIZE = struct.calcsize(FOOTER_FORMAT)


class PackFormatError(Exception):
    pass


class PackConsistencyError(Exception):
    pass


class PackContainerWriter:
    """
    Append-Only Pack Container Writer with Two-Phase Index Atomic Commit.
    """
    def __init__(self, pack_path: Path, idx_path: Path):
        self.pack_path = Path(pack_path)
        self.idx_path = Path(idx_path)
        self.pack_file = None
        self.current_offset = 0
        self.index_entries: Dict[str, Tuple[int, int, int]] = {}  # sha256 -> (offset, length, crc32)
        self._open_pack()

    def _open_pack(self):
        if not self.pack_path.exists():
            self.pack_path.parent.mkdir(parents=True, exist_ok=True)
            self.pack_file = open(self.pack_path, "wb")
            self.pack_file.write(PACK_MAGIC)
            self.pack_file.flush()
            self.current_offset = len(PACK_MAGIC)
        else:
            self.pack_file = open(self.pack_path, "r+b")
            self.pack_file.seek(0, os.SEEK_END)
            self.current_offset = self.pack_file.tell()

    def write_chunk(self, chunk_hash: str, payload: bytes) -> int:
        """
        Writes a single chunk frame:
        [HEADER (44B): MAGIC + ID + LEN + SHA256] + [PAYLOAD] + [FOOTER (8B): PEND + CRC32]
        """
        if self.pack_file is None or self.pack_file.closed:
            raise RuntimeError("Pack file is closed")

        chunk_len = len(payload)
        sha_bytes = bytes.fromhex(chunk_hash) if len(chunk_hash) == 64 else hashlib.sha256(payload).digest()
        crc = zlib.crc32(payload) & 0xFFFFFFFF

        header = struct.pack(HEADER_FORMAT, b"CHNK", len(self.index_entries), chunk_len, sha_bytes)
        footer = struct.pack(FOOTER_FORMAT, b"PEND", crc)

        start_offset = self.current_offset
        self.pack_file.seek(start_offset)
        self.pack_file.write(header)
        self.pack_file.write(payload)
        self.pack_file.write(footer)
        self.pack_file.flush()

        # Update in-memory index
        payload_offset = start_offset + HEADER_SIZE
        self.index_entries[chunk_hash.lower()] = (payload_offset, chunk_len, crc)
        self.current_offset += HEADER_SIZE + chunk_len + FOOTER_SIZE
        return start_offset

    def commit_index(self):
        """
        Atomically commits the index using write-to-temp + os.replace.
        """
        if self.pack_file and not self.pack_file.closed:
            self.pack_file.flush()
            os.fsync(self.pack_file.fileno())

        tmp_idx = self.idx_path.with_suffix(".idx.tmp")
        with open(tmp_idx, "wb") as f:
            # Index Header: PIDX (4), Total Chunks (4), Pack File Size (8)
            f.write(INDEX_MAGIC)
            f.write(struct.pack("<IQ", len(self.index_entries), self.current_offset))

            # Entries: SHA256 (32), Offset (8), Length (4), CRC32 (4)
            for h_str, (offset, length, crc) in sorted(self.index_entries.items()):
                h_bytes = bytes.fromhex(h_str)
                f.write(struct.pack("<32sQII", h_bytes, offset, length, crc))

            f.flush()
            os.fsync(f.fileno())

        # Atomic replace on Windows
        os.replace(tmp_idx, self.idx_path)

    def close(self):
        if self.pack_file and not self.pack_file.closed:
            self.pack_file.close()


class PackContainerReader:
    """
    Random-access reader for Pack Container with CRC & SHA256 verification.
    """
    def __init__(self, pack_path: Path, idx_path: Path):
        self.pack_path = Path(pack_path)
        self.idx_path = Path(idx_path)
        self.index: Dict[str, Tuple[int, int, int]] = {}
        self.load_index()

    def load_index(self):
        if not self.idx_path.exists():
            self.index = {}
            return

        with open(self.idx_path, "rb") as f:
            magic = f.read(4)
            if magic != INDEX_MAGIC:
                raise PackFormatError(f"Invalid Index Magic: {magic}")

            total_chunks, pack_size = struct.unpack("<IQ", f.read(12))
            actual_pack_size = self.pack_path.stat().st_size if self.pack_path.exists() else 0

            # If index expects larger pack file than physical, index is out of sync
            if actual_pack_size < pack_size:
                raise PackConsistencyError(
                    f"Pack file truncated: actual={actual_pack_size}, index expects={pack_size}"
                )

            self.index = {}
            entry_format = "<32sQII"
            entry_size = struct.calcsize(entry_format)
            for _ in range(total_chunks):
                data = f.read(entry_size)
                if len(data) < entry_size:
                    raise PackConsistencyError("Index file truncated prematurely")
                h_bytes, offset, length, crc = struct.unpack(entry_format, data)
                self.index[h_bytes.hex().lower()] = (offset, length, crc)

    def has_chunk(self, chunk_hash: str) -> bool:
        return chunk_hash.lower() in self.index

    def read_chunk(self, chunk_hash: str, verify: bool = True) -> bytes:
        chunk_hash = chunk_hash.lower()
        if chunk_hash not in self.index:
            raise KeyError(f"Chunk not found in index: {chunk_hash}")

        offset, length, expected_crc = self.index[chunk_hash]

        with open(self.pack_path, "rb") as f:
            # Check frame header
            f.seek(offset - HEADER_SIZE)
            header_bytes = f.read(HEADER_SIZE)
            if len(header_bytes) < HEADER_SIZE:
                raise PackConsistencyError(f"Header truncated for chunk {chunk_hash}")
            magic, cid, clen, sha_bytes = struct.unpack(HEADER_FORMAT, header_bytes)
            if magic != b"CHNK" or clen != length:
                raise PackConsistencyError(f"Frame header corrupted for chunk {chunk_hash}")

            # Read payload
            f.seek(offset)
            payload = f.read(length)
            if len(payload) < length:
                raise PackConsistencyError(f"Payload truncated for chunk {chunk_hash}")

            # Check footer
            footer_bytes = f.read(FOOTER_SIZE)
            if len(footer_bytes) < FOOTER_SIZE:
                raise PackConsistencyError(f"Footer truncated for chunk {chunk_hash}")
            f_magic, f_crc = struct.unpack(FOOTER_FORMAT, footer_bytes)
            if f_magic != b"PEND":
                raise PackConsistencyError(f"Footer magic invalid for chunk {chunk_hash}")

            if verify:
                calc_crc = zlib.crc32(payload) & 0xFFFFFFFF
                if calc_crc != expected_crc or calc_crc != f_crc:
                    raise PackConsistencyError(
                        f"CRC mismatch for {chunk_hash}: expected={expected_crc}, actual={calc_crc}"
                    )
                calc_sha = hashlib.sha256(payload).hexdigest()
                if calc_sha.lower() != chunk_hash:
                    raise PackConsistencyError(
                        f"SHA256 mismatch for {chunk_hash}: expected={chunk_hash}, actual={calc_sha}"
                    )

            return payload


class PackRecoveryEngine:
    """
    Rebuilds index and repairs torn/corrupted pack files.
    """
    @staticmethod
    def rebuild_index_from_pack(pack_path: Path, idx_path: Path) -> Tuple[int, int]:
        """
        Scans pack file sequentially, validates each chunk frame,
        and rebuilds a valid .idx index. If a torn/incomplete chunk is found at EOF,
        it reports the valid boundary.
        Returns: (valid_chunks_count, valid_bytes_offset)
        """
        pack_path = Path(pack_path)
        idx_path = Path(idx_path)

        if not pack_path.exists():
            return 0, 0

        valid_entries: Dict[str, Tuple[int, int, int]] = {}
        valid_offset = 0

        with open(pack_path, "rb") as f:
            magic = f.read(len(PACK_MAGIC))
            if magic != PACK_MAGIC:
                raise PackFormatError(f"Invalid Pack Magic: {magic}")
            valid_offset = len(PACK_MAGIC)

            while True:
                header_pos = f.tell()
                header_bytes = f.read(HEADER_SIZE)
                if not header_bytes:
                    break  # Clean EOF
                if len(header_bytes) < HEADER_SIZE:
                    # Torn header at EOF
                    break

                magic, cid, chunk_len, sha_bytes = struct.unpack(HEADER_FORMAT, header_bytes)
                if magic != b"CHNK":
                    # Corrupted frame magic
                    break

                payload = f.read(chunk_len)
                if len(payload) < chunk_len:
                    # Torn payload at EOF
                    break

                footer_bytes = f.read(FOOTER_SIZE)
                if len(footer_bytes) < FOOTER_SIZE:
                    # Torn footer at EOF
                    break

                f_magic, f_crc = struct.unpack(FOOTER_FORMAT, footer_bytes)
                if f_magic != b"PEND":
                    break

                # Verify payload CRC
                calc_crc = zlib.crc32(payload) & 0xFFFFFFFF
                if calc_crc != f_crc:
                    break

                sha_str = sha_bytes.hex().lower()
                payload_offset = header_pos + HEADER_SIZE
                valid_entries[sha_str] = (payload_offset, chunk_len, f_crc)
                valid_offset = f.tell()

        # Write clean rebuilt index
        tmp_idx = idx_path.with_suffix(".idx.rebuilt")
        with open(tmp_idx, "wb") as f:
            f.write(INDEX_MAGIC)
            f.write(struct.pack("<IQ", len(valid_entries), valid_offset))
            for h_str, (offset, length, crc) in sorted(valid_entries.items()):
                f.write(struct.pack("<32sQII", bytes.fromhex(h_str), offset, length, crc))
            f.flush()
            os.fsync(f.fileno())

        os.replace(tmp_idx, idx_path)
        return len(valid_entries), valid_offset

    @staticmethod
    def truncate_to_valid_offset(pack_path: Path, valid_offset: int):
        """Truncates incomplete trailing bytes from torn writes."""
        with open(pack_path, "r+b") as f:
            f.truncate(valid_offset)
            f.flush()
            os.fsync(f.fileno())
