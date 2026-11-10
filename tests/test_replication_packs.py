# -*- coding: utf-8 -*-
"""청크(pack) 전략 파일이 포함된 스냅샷의 로컬 복제 회귀 테스트."""
import json
import os

from core.replication import ReplicationManager

CHUNK_A = "ab" + "0" * 62
CHUNK_B = "cd" + "1" * 62
FILE_SHA = "ef" + "2" * 62


def _make_repo(tmp_path, with_pack=True):
    local = tmp_path / "local"
    remote = tmp_path / "remote"
    (local / "blobs").mkdir(parents=True)
    (local / "snapshots").mkdir()
    remote.mkdir()
    if with_pack:
        (local / "packs").mkdir()
        (local / "packs" / "pack_1.pack").write_bytes(b"PACKDATA")
        (local / "packs" / "pack_1.idx").write_bytes(b"IDX")
    manifest = {
        "id": "snap_t",
        "entries": [
            {"rel_path": "big.bin", "blob_id": FILE_SHA, "sha256": FILE_SHA, "chunk_ids": [CHUNK_A, CHUNK_B]},
        ],
    }
    (local / "snapshots" / "snap_t.json").write_text(json.dumps(manifest), encoding="utf-8")
    return str(local), str(remote), manifest


def test_extract_uses_chunk_ids_not_whole_file_hash(tmp_path):
    local, remote, manifest = _make_repo(tmp_path)
    rm = ReplicationManager(local, remote)
    ids = rm._extract_blob_ids_from_manifest(manifest)
    assert ids == {CHUNK_A, CHUNK_B}


def test_pack_resident_chunks_replicated_via_pack_files(tmp_path):
    local, remote, _ = _make_repo(tmp_path)
    rm = ReplicationManager(local, remote)
    res = rm.replicate_snapshot("snap_t")
    assert res["status"] == "success", res
    assert os.path.exists(os.path.join(remote, "packs", "pack_1.pack"))
    assert os.path.exists(os.path.join(remote, "packs", "pack_1.idx"))
    assert os.path.exists(os.path.join(remote, "snapshots", "snap_t.json"))


def test_missing_everywhere_still_reports_error(tmp_path):
    local, remote, _ = _make_repo(tmp_path, with_pack=False)
    rm = ReplicationManager(local, remote)
    res = rm.replicate_snapshot("snap_t")
    assert res["status"] == "error"
