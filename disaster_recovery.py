#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
disaster_recovery.py - 독립형 무설치 재해 복구 CLI 도구 (Standalone Disaster Recovery CLI)
외부 프레임워크 의존 없이 파이썬 표준 라이브러리(zlib, hashlib, json 등)만으로 동작합니다.
- zstandard 및 cryptography가 설치된 경우 초고속 zstd 압축 해제 및 Ed25519 디지털 서명 검증 지원
- 저장소 Bit-Rot 전수 감사(--audit), 무결성 검증(--verify), 스냅샷 복원(--restore) 제공
"""

import os
import sys
import json
import zlib
import hashlib
import time
import glob
import argparse
import concurrent.futures
from typing import Dict, List, Any, Optional, Tuple

# Windows 콘솔 UTF-8 안전 처리 (cp949 에러 방지)
try:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

# 선택적 라이브러리 감지
try:
    import zstandard as zstd
    HAS_ZSTD = True
except ImportError:
    zstd = None
    HAS_ZSTD = False

try:
    from cryptography.hazmat.primitives.serialization import load_pem_public_key
    HAS_CRYPTO = True
except ImportError:
    HAS_CRYPTO = False


def timed_input(prompt: str, timeout: float = 5.0, default: str = "") -> str:
    """Windows 콘솔 타임아웃 입력기 (키 입력 없을 시 기본값 자동 적용)"""
    print(prompt, end="", flush=True)
    try:
        import msvcrt
        start = time.time()
        chars = []
        while time.time() - start < timeout:
            if msvcrt.kbhit():
                while True:
                    ch = msvcrt.getwche()
                    if ch in ('\r', '\n'):
                        print()
                        return "".join(chars).strip()
                    elif ch == '\x08':  # Backspace
                        if chars:
                            chars.pop()
                    else:
                        chars.append(ch)
            time.sleep(0.05)
        print(f"\n[*] {int(timeout)}초 자동 진행 타임아웃 -> 기본값 적용: '{default}'")
        return default
    except Exception:
        print()
        return default


def find_candidate_target_drives(repo_dir: Optional[str] = None) -> List[str]:
    """WinPE 및 복구 환경에서 복원 대상이 될 수 있는 유효 쓰기 가능 드라이브 탐색"""
    import string
    script_drive = os.path.splitdrive(os.path.abspath(__file__))[0].upper()
    repo_drive = os.path.splitdrive(os.path.abspath(repo_dir))[0].upper() if repo_dir else ""
    sys_drive = os.environ.get("SYSTEMDRIVE", "X:").upper()

    candidates = []
    for letter in string.ascii_uppercase:
        drv = f"{letter}:"
        if drv in (script_drive, repo_drive, sys_drive):
            continue
        root = f"{drv}\\"
        if os.path.exists(root):
            try:
                test_file = os.path.join(root, ".dr_target_probe.tmp")
                with open(test_file, "w") as f_probe:
                    f_probe.write("probe")
                os.remove(test_file)
                candidates.append(drv)
            except Exception:
                pass
    return candidates


def format_bytes(size: int) -> str:
    power = 1024
    n = 0
    units = ['B', 'KB', 'MB', 'GB', 'TB']
    val = float(size)
    while val > power and n < len(units) - 1:
        val /= power
        n += 1
    return f"{val:.2f} {units[n]}"


def get_candidate_repositories() -> List[str]:
    """시스템 내 백업 저장소 후보 경로 자동 탐색"""
    script_dir = os.path.dirname(os.path.abspath(__file__))
    candidates = [
        os.path.join(script_dir, "MyBackup_Repository"),
        os.path.join(script_dir, "backup_repository"),
        r"D:\MyBackup_Repository",
        r"C:\MyBackup_Repository",
        os.path.join(os.path.expanduser("~"), "MyBackup_Repository"),
    ]
    try:
        import psutil
        for part in psutil.disk_partitions(all=False):
            if part.mountpoint:
                candidates.append(os.path.join(part.mountpoint, "MyBackup_Repository"))
                candidates.append(os.path.join(part.mountpoint, "backup_repository"))
    except Exception:
        pass

    import string
    for letter in string.ascii_uppercase:
        if letter == "X":
            continue
        candidates.append(f"{letter}:\\MyBackup_Repository")
        candidates.append(f"{letter}:\\backup_repository")

    valid = []
    seen = set()
    for c in candidates:
        norm = os.path.normpath(c).lower()
        if norm not in seen and os.path.isdir(os.path.join(c, "snapshots")) and os.path.isdir(os.path.join(c, "blobs")):
            seen.add(norm)
            valid.append(c)

    def _latest_snap_time(r: str) -> float:
        s_dir = os.path.join(r, "snapshots")
        try:
            files = [os.path.join(s_dir, f) for f in os.listdir(s_dir) if f.endswith(".json")]
            return max([os.path.getmtime(f) for f in files]) if files else 0.0
        except Exception:
            return 0.0

    valid.sort(key=_latest_snap_time, reverse=True)
    return valid


def get_blob_path(repo_dir: str, sha256_hash: str) -> str:
    """해시값에 해당하는 블롭 파일 경로 반환"""
    if not sha256_hash:
        return ""
    prefix = sha256_hash[:2]
    return os.path.join(repo_dir, "blobs", prefix, f"{sha256_hash}.blob")


def verify_ed25519_manifest(manifest: Dict[str, Any], pubkey_path: str) -> Tuple[bool, str]:
    """Manifest의 Ed25519 전자서명 검증"""
    if not HAS_CRYPTO:
        return False, "cryptography 미설치 (Ed25519 검증 생략)"
    if not os.path.exists(pubkey_path):
        return False, f"공개키 없음 ({pubkey_path})"

    sig_hex = manifest.get("ed25519_signature")
    if not sig_hex or not isinstance(sig_hex, str):
        return False, "서명 누락 (ed25519_signature 필드 없음)"

    try:
        with open(pubkey_path, "rb") as f:
            pub_key = load_pem_public_key(f.read())

        sigable = {k: v for k, v in manifest.items() if k not in ("manifest_signature", "ed25519_signature")}
        payload = json.dumps(sigable, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode('utf-8')
        pub_key.verify(bytes.fromhex(sig_hex), payload)
        return True, "Ed25519 전자서명 검증 통과 (신뢰성 보증)"
    except Exception as e:
        return False, f"Ed25519 서명 불일치 (위조 탐지: {str(e)})"


def verify_manifest_fingerprint(manifest: Dict[str, Any]) -> Tuple[bool, str]:
    """Manifest 내부 파일 엔트리 기반 SHA-256 결합 지문(Fingerprint) 검증"""
    stored_sig = manifest.get("manifest_signature")
    if not stored_sig:
        return True, "지문 없음 (SHA-256 검증 건너뜀)"

    entries = manifest.get("entries", [])
    if not entries:
        calc_sig = hashlib.sha256(b"").hexdigest()
    else:
        hashes = []
        for entry in entries:
            h = entry.get("sha256") or entry.get("blob_id") or entry.get("blob_hash") or entry.get("hash")
            if h:
                hashes.append(h.lower())
        hashes.sort()
        calc_sig = hashlib.sha256("\n".join(hashes).encode('utf-8')).hexdigest()

    if calc_sig == stored_sig:
        return True, "Manifest 지문(Fingerprint) 일치"
    return False, f"Manifest 지문 불일치 (예상: {stored_sig[:12]}..., 실제: {calc_sig[:12]}...)"


def list_snapshots(repo_dir: str) -> List[Dict[str, Any]]:
    """저장소 내 스냅샷 목록 로드 및 정렬"""
    snaps_dir = os.path.join(repo_dir, "snapshots")
    if not os.path.exists(snaps_dir):
        return []

    pubkey_path = os.path.join(repo_dir, "keys", "backup_ed25519.pub")
    pattern = os.path.join(snaps_dir, "*.json")
    files = glob.glob(pattern)

    snapshots = []
    for f in files:
        try:
            with open(f, "r", encoding="utf-8") as fp:
                data = json.load(fp)
            snap_id = data.get("id") or os.path.splitext(os.path.basename(f))[0]
            entries = data.get("entries", [])
            total_bytes = sum(e.get("size", 0) for e in entries)

            # 서명 상태 판별
            sig_status = "미서명"
            if "ed25519_signature" in data:
                if HAS_CRYPTO and os.path.exists(pubkey_path):
                    ok, _ = verify_ed25519_manifest(data, pubkey_path)
                    sig_status = "[SECURE] Ed25519 정상" if ok else "[FAIL] 서명 위조"
                else:
                    sig_status = "[SIGNED] Ed25519 보유"
            elif "manifest_signature" in data:
                sig_status = "[FINGERPRINT] 지문 보유"

            snapshots.append({
                "id": snap_id,
                "path": f,
                "created_at": data.get("created_at", 0),
                "iso_time": data.get("iso_time") or time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(data.get("created_at", 0))),
                "profile_name": data.get("profile_name", "Unknown"),
                "file_count": len(entries),
                "total_bytes": total_bytes,
                "sig_status": sig_status,
                "data": data
            })
        except Exception:
            continue

    snapshots.sort(key=lambda s: s["created_at"], reverse=True)
    return snapshots


def extract_blob(blob_path: str, dest_path: str, expected_sha256: str, verify_hash: bool = True, crypto_engine: Optional[Any] = None) -> bool:
    """단일 블롭을 읽어 대상 파일로 복원 (v1 암호화 및 v0 평문 자동 판별)"""
    if not os.path.exists(blob_path):
        raise FileNotFoundError(f"블롭 누락: {blob_path}")

    os.makedirs(os.path.dirname(dest_path), exist_ok=True)
    if os.path.exists(dest_path):
        try:
            import stat
            os.chmod(dest_path, stat.S_IWRITE)
        except Exception:
            pass

    hasher = hashlib.sha256() if verify_hash else None

    with open(blob_path, "rb") as fin:
        header = fin.read(4)

    is_enc = (header == b"ENC\x01")
    is_zstd = (header == b"\x28\xb5\x2f\xfd")
    buf_size = 262144

    try:
        if is_enc:
            if not crypto_engine:
                raise RuntimeError(f"암호화된 블롭입니다 ({expected_sha256[:12]}). 복호화를 위해 마스터 키/패스프레이즈가 필요합니다.")
            with open(blob_path, "rb") as fin:
                blob_bytes = fin.read()
            decompressed = crypto_engine.decrypt_blob_data(blob_bytes, expected_sha256)
            with open(dest_path, "wb") as fout:
                fout.write(decompressed)
            if hasher:
                hasher.update(decompressed)
        else:
            with open(blob_path, "rb") as fin, open(dest_path, "wb") as fout:
                if is_zstd:
                    if not HAS_ZSTD:
                        raise RuntimeError("Zstd 압축 블롭입니다. 'pip install zstandard'가 필요합니다.")
                    dctx = zstd.ZstdDecompressor()
                    with dctx.stream_reader(fin) as reader:
                        while True:
                            chunk = reader.read(buf_size)
                            if not chunk:
                                break
                            fout.write(chunk)
                            if hasher:
                                hasher.update(chunk)
                else:
                    decompressor = zlib.decompressobj()
                    while True:
                        chunk = fin.read(buf_size)
                        if not chunk:
                            break
                        decompressed = decompressor.decompress(chunk)
                        if decompressed:
                            fout.write(decompressed)
                            if hasher:
                                hasher.update(decompressed)
                    tail = decompressor.flush()
                    if tail:
                        fout.write(tail)
                        if hasher:
                            hasher.update(tail)
    except Exception as e:
        if os.path.exists(dest_path):
            try:
                os.remove(dest_path)
            except OSError:
                pass
        raise ValueError(f"블롭 데이터 손상 (Bit-Rot 탐지): {str(e)}") from e

    if verify_hash and hasher:
        actual_hash = hasher.hexdigest()
        if actual_hash.lower() != expected_sha256.lower():
            try:
                os.remove(dest_path)
            except OSError:
                pass
            raise ValueError(f"해시 불일치 (Bit-Rot 탐지): 예상={expected_sha256[:16]}..., 실제={actual_hash[:16]}...")

    return True


def audit_repository(repo_dir: str) -> Dict[str, Any]:
    """저장소 내 모든 블롭(.blob)의 Bit-Rot 전수 감사"""
    print(f"[*] 저장소 블롭 무결성 전수 감사(Audit) 시작: {repo_dir}")
    blobs_dir = os.path.join(repo_dir, "blobs")
    if not os.path.exists(blobs_dir):
        print(f"[!] 블롭 디렉터리가 존재하지 않습니다: {blobs_dir}")
        return {"total": 0, "corrupted": 0, "healthy": 0}

    blob_files = glob.glob(os.path.join(blobs_dir, "*", "*.blob"))
    total = len(blob_files)
    print(f"[*] 총 감시대상 블롭: {total}개")

    healthy = 0
    corrupted = []
    start_time = time.time()

    def check_blob(path: str) -> Tuple[str, bool, str]:
        expected_hash = os.path.splitext(os.path.basename(path))[0]
        hasher = hashlib.sha256()
        try:
            with open(path, "rb") as fin:
                header = fin.read(4)

            is_zstd = (header == b"\x28\xb5\x2f\xfd")
            is_enc = (header == b"ENC\x01")

            # 암호화 블롭(ENC\x01)은 zlib/zstd decompress 대상이 아니므로 오분류 방지
            if is_enc:
                file_size = os.path.getsize(path)
                if file_size < 32:
                    return path, False, "암호화 블롭 구조 손상 (최소 크기 미달)"
                return path, True, ""

            with open(path, "rb") as fin:
                if is_zstd:
                    if not HAS_ZSTD:
                        return path, False, "zstandard 미설치"
                    dctx = zstd.ZstdDecompressor()
                    with dctx.stream_reader(fin) as reader:
                        while True:
                            c = reader.read(262144)
                            if not c:
                                break
                            hasher.update(c)
                else:
                    decomp = zlib.decompressobj()
                    while True:
                        c = fin.read(262144)
                        if not c:
                            break
                        dc = decomp.decompress(c)
                        if dc:
                            hasher.update(dc)
                    t = decomp.flush()
                    if t:
                        hasher.update(t)

            actual_hash = hasher.hexdigest()
            if actual_hash.lower() == expected_hash.lower():
                return path, True, ""
            return path, False, f"해시 불일치 (기대: {expected_hash[:12]}, 계산: {actual_hash[:12]})"
        except Exception as e:
            return path, False, f"압축 해제 또는 판독 실패: {str(e)}"

    with concurrent.futures.ThreadPoolExecutor(max_workers=min(16, (os.cpu_count() or 4) * 2)) as executor:
        futures = {executor.submit(check_blob, b): b for b in blob_files}
        done_count = 0
        for fut in concurrent.futures.as_completed(futures):
            path, ok, err = fut.result()
            done_count += 1
            if ok:
                healthy += 1
            else:
                corrupted.append((path, err))

            if done_count % 500 == 0 or done_count == total:
                elapsed = time.time() - start_time
                pct = (done_count / total * 100) if total > 0 else 100
                speed = done_count / elapsed if elapsed > 0 else 0
                print(f"  진행률: {done_count}/{total} ({pct:.1f}%) | {speed:.1f} blob/s | 손상: {len(corrupted)}개")

    print("\n" + "=" * 60)
    print("[AUDIT] 저장소 Bit-Rot 감사 결과 요약")
    print(f" - 전체 검사 블롭: {total}개")
    print(f" - 정상 무결성 블롭: {healthy}개")
    print(f" - 손상(Bit-Rot) 블롭: {len(corrupted)}개")
    if corrupted:
        print("\n[WARNING] 손상된 블롭 목록:")
        for cpath, cerr in corrupted[:10]:
            print(f"  * {os.path.basename(cpath)}: {cerr}")
        if len(corrupted) > 10:
            print(f"  ... 외 {len(corrupted)-10}개 생략")
    print("=" * 60)

    return {"total": total, "healthy": healthy, "corrupted": len(corrupted), "errors": corrupted}


def restore_snapshot(
    repo_dir: str,
    snapshot_id: str,
    dest_dir: Optional[str] = None,
    verify_hash: bool = True,
    filter_keyword: Optional[str] = None,
    non_interactive: bool = True,
    crypto_engine: Optional[Any] = None,
    passphrase: Optional[str] = None,
    key_file: Optional[str] = None
) -> Dict[str, Any]:
    """지정된 스냅샷 복원 실행 (v1 암호화 및 v0 평문 자동 지원)"""
    snapshots = list_snapshots(repo_dir)
    target_snap = None
    if snapshot_id.lower() == "latest":
        if snapshots:
            target_snap = snapshots[0]
    else:
        for s in snapshots:
            if s["id"] == snapshot_id or os.path.splitext(os.path.basename(s["path"]))[0] == snapshot_id:
                target_snap = s
                break

    if not target_snap:
        print(f"[!] 선택한 백업 시점을 찾지 못했어요: {snapshot_id}\n    목록에서 번호나 이름을 다시 확인해 주세요.")
        return {"success": False, "error": "스냅샷 미존재"}

    manifest = target_snap["data"]
    print(f"\n[*] 백업 되돌리기를 준비하고 있어요: {target_snap['id']} ({target_snap['iso_time']})")
    print(f"[*] 프로필: {target_snap['profile_name']} | 총 {target_snap['file_count']}개 파일 ({format_bytes(target_snap['total_bytes'])})")

    # 암호화 엔진 초기화
    if not crypto_engine:
        if key_file and os.path.exists(key_file):
            try:
                from core.crypto_at_rest import CryptoAtRestEngine
                crypto_engine = CryptoAtRestEngine.from_key_file(key_file, repo_dir)
                print(f"[*] 오프라인 키 파일 로드 완료: {key_file}")
            except Exception as e:
                print(f"[!] 암호 키 파일을 읽지 못했어요. 키 파일 위치를 확인한 뒤 다시 시도해 주세요. (자세한 원인: {e})")
        elif passphrase:
            try:
                from core.crypto_at_rest import CryptoAtRestEngine
                crypto_engine = CryptoAtRestEngine.from_passphrase(passphrase, repo_dir, create_if_missing=False)
                print("[*] 패스프레이즈 기반 복호화 키 초기화 완료")
            except Exception as e:
                print(f"[!] 입력한 암호로 백업을 열지 못했어요. 암호를 다시 확인해 주세요. (자세한 원인: {e})")

    # 무결성 검증 (Ed25519 & Manifest 지문)
    pubkey = os.path.join(repo_dir, "keys", "backup_ed25519.pub")
    if "ed25519_signature" in manifest:
        ok, msg = verify_ed25519_manifest(manifest, pubkey)
        print(f"[*] {msg}")
        if not ok and "위조" in msg:
            if non_interactive:
                return {"success": False, "error": "서명 위조 탐지로 인한 자동 복원 중단"}
            choice = input("[주의] 이 백업이 중간에 바뀐 것 같아요. 그래도 되돌릴까요? 모르겠으면 N을 누르세요. (y/N): ").strip().lower()
            if choice != "y":
                return {"success": False, "error": "서명 위조로 인한 사용자 복원 중단"}

    ok_fp, msg_fp = verify_manifest_fingerprint(manifest)
    print(f"[*] {msg_fp}")

    entries = manifest.get("entries", [])
    if filter_keyword:
        kw = filter_keyword.lower()
        entries = [e for e in entries if kw in (e.get("path") or e.get("rel_path", "")).lower()]
        print(f"[*] 필터 '{filter_keyword}' 적용: {len(entries)}개 항목 대상")

    if not entries:
        print("[!] 복원할 대상 파일이 없습니다.")
        return {"success": True, "restored": 0, "failed": 0}

    # 복원 대상 기준점 산출
    if dest_dir:
        dest_base = os.path.abspath(dest_dir)
        print(f"[*] 복원 대상 디렉터리: {dest_base}")
    else:
        dest_base = None
        print("[*] 백업 당시 원본 경로로 복원합니다.")

    # 공통 prefix 산출 (dest_dir 지정 시 상대경로 보존용)
    # NOTE: 아래 common_prefix는 계산만 하고 어디에서도 사용되지 않는 죽은 코드였다(dead code).
    # 경로 매핑은 process_entry()가 '드라이브 문자 제거' 방식으로 직접 처리한다.
    # 죽은 변수 계산을 제거해 오해를 유발하는 코드를 정리한다.

    success_count = 0
    fail_count = 0
    failed_entries = []
    start_time = time.time()

    def process_entry(entry: Dict[str, Any]) -> Tuple[bool, str]:
        src_path = entry.get("path") or os.path.join(entry.get("source_root", ""), entry.get("rel_path", ""))
        h = entry.get("sha256") or entry.get("blob_id") or entry.get("blob_hash") or entry.get("hash", "")
        if not h or not src_path:
            return False, f"잘못된 엔트리: {src_path}"

        blob_p = get_blob_path(repo_dir, h)
        if not os.path.exists(blob_p):
            return False, f"블롭 누락: {h[:12]} ({src_path})"

        if dest_base:
            # src_path에서 드라이브 문자(예: C:)를 제거하여 원본 디렉터리 트리 보존
            drive, path_part = os.path.splitdrive(src_path)
            rel = path_part.lstrip("\\/")
            target_out = os.path.join(dest_base, rel)
        else:
            target_out = src_path

        try:
            extract_blob(blob_p, target_out, expected_sha256=h, verify_hash=verify_hash, crypto_engine=crypto_engine)
            # mtime 복원
            mtime = entry.get("mtime")
            if mtime:
                try:
                    os.utime(target_out, (mtime, mtime))
                except Exception:
                    pass
            return True, ""
        except Exception as e:
            return False, f"{src_path}: {str(e)}"

    workers = min(16, (os.cpu_count() or 4) * 2)
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as executor:
        future_map = {executor.submit(process_entry, e): e for e in entries}
        total_items = len(entries)
        done_items = 0
        for fut in concurrent.futures.as_completed(future_map):
            done_items += 1
            ok, err = fut.result()
            if ok:
                success_count += 1
            else:
                fail_count += 1
                failed_entries.append(err)

            if done_items % 200 == 0 or done_items == total_items:
                pct = (done_items / total_items * 100) if total_items > 0 else 100
                sys.stdout.write(f"\r[*] 복원 진행률: {done_items}/{total_items} ({pct:.1f}%) | 성공: {success_count}, 실패: {fail_count}")
                sys.stdout.flush()

    elapsed = max(0.001, time.time() - start_time)
    print(f"\n\n{'='*60}")
    print(f"[완료] 되돌리기가 끝났어요! (걸린 시간: {elapsed:.0f}초)")
    print(f" - 되돌린 파일: {success_count}개")
    print(f" - 되돌리지 못한 파일: {fail_count}개")
    if failed_entries:
        print("\n[주의] 되돌리지 못한 파일 목록 (처음 10개):")
        for ferr in failed_entries[:10]:
            print(f"  * {ferr}")
        if len(failed_entries) > 10:
            print(f"  ... 외 {len(failed_entries)-10}개 생략")
    print(f"{'='*60}\n")

    return {"success": fail_count == 0, "restored": success_count, "failed": fail_count, "errors": failed_entries}


def main():
    parser = argparse.ArgumentParser(
        description="독립형 무설치 재해 복구 CLI 도구 (Standalone Disaster Recovery Engine)"
    )
    parser.add_argument("--repo", "-r", type=str, default=None, help="백업 저장소 경로 (미지정 시 자동 감지)")
    parser.add_argument("--list", "-l", action="store_true", help="저장소 내 사용 가능한 스냅샷 목록 조회")
    parser.add_argument("--verify", "-v", action="store_true", help="스냅샷 무결성(Ed25519 서명/지문/블롭) 검증")
    parser.add_argument("--restore", type=str, default=None, help="복원할 스냅샷 ID 지정 (또는 'latest')")
    parser.add_argument("--dest", "-d", type=str, default=None, help="복원 대상 디렉터리 (미지정 시 원본 위치)")
    parser.add_argument("--audit", action="store_true", help="저장소 내 전체 블롭 Bit-Rot 전수 감사")
    parser.add_argument("--filter", "-f", type=str, default=None, help="복원 파일명/경로 필터링")
    parser.add_argument("--passphrase", "-p", type=str, default=None, help="암호화 저장소 복호화 패스프레이즈")
    parser.add_argument("--key-file", "-k", type=str, default=None, help="오프라인 키 백업 파일 경로")
    args = parser.parse_args()

    repo = args.repo
    if not repo:
        candidates = get_candidate_repositories()
        if candidates:
            repo = candidates[0]
            print(f"[*] 백업 보관함을 자동으로 찾았어요: {repo}")
        else:
            repo = input("백업 보관함이 있는 폴더를 입력하세요 (예: D:\\MyBackup_Repository): ").strip('"\' ')

    if not repo or not os.path.isdir(repo):
        print(f"[!] 백업 보관함을 찾지 못했어요: {repo}\n    백업용 외장하드가 연결되어 있는지 확인해 주세요.")
        sys.exit(1)

    if args.audit:
        audit_repository(repo)
        return

    snapshots = list_snapshots(repo)
    if not snapshots:
        print(f"[!] 이 보관함에는 아직 백업이 없어요: {repo}")
        return

    if args.list:
        print(f"\n{'='*75}")
        print(f"[목록] 되돌릴 수 있는 백업: {repo}")
        print(f"{'='*75}")
        for idx, s in enumerate(snapshots, 1):
            print(f"[{idx:2d}] ID: {s['id']}")
            print(f"     일시: {s['iso_time']} | 프로필: {s['profile_name']}")
            print(f"     파일: {s['file_count']}개 | 용량: {format_bytes(s['total_bytes'])} | 서명: {s['sig_status']}")
        print(f"{'='*75}\n")
        return

    if args.verify:
        snap_id = args.restore or "latest"
        print(f"[*] 백업이 온전한지 꼼꼼히 확인하고 있어요: {snap_id}")
        target = snapshots[0] if snap_id == "latest" else next((s for s in snapshots if s["id"] == snap_id), None)
        if not target:
            print(f"[!] 백업 {snap_id}을(를) 찾지 못했어요. 목록에서 다시 확인해 주세요.")
            return

        manifest = target["data"]
        pubkey = os.path.join(repo, "keys", "backup_ed25519.pub")
        ok_sig, msg_sig = verify_ed25519_manifest(manifest, pubkey)
        ok_fp, msg_fp = verify_manifest_fingerprint(manifest)
        print(f" - 변조 방지 확인: {msg_sig}")
        print(f" - 백업 목록표 확인: {msg_fp}")

        missing_blobs = []
        for e in manifest.get("entries", []):
            bh = e.get("blob_hash") or e.get("hash") or e.get("sha256", "")
            if bh and not os.path.exists(get_blob_path(repo, bh)):
                missing_blobs.append(bh)

        if missing_blobs:
            print(f" [주의] 빠진 저장 조각이 {len(missing_blobs)}개 있어요. 일부만 되돌려질 수 있어요.")
        else:
            print(f" [OK] 전체 {len(manifest.get('entries', []))}개 파일이 모두 온전해요")
        return

    target_drives = find_candidate_target_drives(repo)
    if args.restore:
        dest = args.dest
        if dest == "auto":
            dest = target_drives[0] + "\\" if target_drives else None
            if dest:
                print(f"[*] 되돌릴 드라이브를 자동으로 골랐어요: {dest}")
        restore_snapshot(
            repo_dir=repo,
            snapshot_id=args.restore,
            dest_dir=dest,
            verify_hash=True,
            filter_keyword=args.filter,
            passphrase=args.passphrase,
            key_file=args.key_file
        )
        return

    # 대화형 모드 (옵션 미지정 시)
    print(f"\n{'='*75}")
    print(f"[목록] 되돌릴 수 있는 백업: {repo}")
    print(f"{'='*75}")
    for idx, s in enumerate(snapshots[:10], 1):
        print(f"[{idx:2d}] {s['id']} | {s['iso_time'][:19]} | {s['profile_name']} | {s['file_count']}개 | {s['sig_status']}")
    print(f"{'='*75}")

    choice = timed_input("\n되돌릴 백업 번호를 입력하세요. (안 누르면 5초 뒤 1번=가장 최근 백업, 그만두려면 Q): ", timeout=5.0, default="1").strip()
    if choice.lower() == 'q':
        return

    if choice.isdigit() and 1 <= int(choice) <= len(snapshots):
        selected_snap = snapshots[int(choice) - 1]
    elif choice:
        match = next((s for s in snapshots if s["id"].lower() == choice.lower() or s["id"].lower().startswith(choice.lower())), None)
        if match:
            selected_snap = match
        else:
            print(f"[!] '{choice}'에 맞는 백업이 없어서 가장 최근 백업(1번)을 골랐어요.")
            selected_snap = snapshots[0]
    else:
        selected_snap = snapshots[0]

    dest = args.dest
    if dest == "auto":
        dest = target_drives[0] + "\\" if target_drives else None
        if dest:
            print(f"[*] 되돌릴 드라이브를 자동으로 골랐어요: {dest}")
    elif not dest:
        default_target = (target_drives[0] + "\\") if target_drives else ""
        rec_hint = f" (추천: {target_drives[0]}\\)" if target_drives else " (엔터 시 원본 위치)"
        dest_in = timed_input(f"되돌릴 위치(폴더)를 입력하세요{rec_hint} (안 누르면 5초 뒤 자동 선택): ", timeout=5.0, default=default_target).strip('"\' ')
        if not dest_in and target_drives:
            dest = target_drives[0] + "\\"
            print(f"[*] 선택된 대상 드라이브: {dest}")
        else:
            dest = dest_in if dest_in else None

    restore_snapshot(
        repo_dir=repo,
        snapshot_id=selected_snap["id"],
        dest_dir=dest,
        verify_hash=True
    )


if __name__ == "__main__":
    main()
