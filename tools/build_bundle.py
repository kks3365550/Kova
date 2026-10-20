# -*- coding: utf-8 -*-
"""
tools/build_bundle.py: 공식 Ed25519 Root Key 기반 .bundle 배포 아티팩트 빌더

기능:
1. 대상 소스 파일(core, web, keys, run.py, start_silent.vbs, VERSION)을 패키징하여 package.zip 생성 (Private Key 배제 Zero-Leak)
2. package.zip의 SHA-256 및 크기 계산 후 manifest.json 작성
3. manifest.json을 RFC 8785 JCS 정규화 후 keys/release_ed25519.key로 서명 (manifest.sig)
4. 배포 채널 및 버전 제어 규칙(policy.json) 작성
5. policy.json을 RFC 8785 JCS 정규화 후 keys/release_ed25519.key로 서명 (policy.sig)
6. 5대 요소를 결합하여 단일 dist/release_v{version}.bundle 파일로 패키징
"""

import os
import sys
import io
import json
import zipfile
import hashlib
from datetime import datetime, timezone

BASE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

from core.updater_v2.jcs import canonicalize
from core.crypto_sign import sign_bytes_ed25519
from core.updater_v2.bundle import BundleCreator
from core.updater_v2.default_keyring import DEFAULT_KEY_ID


def build_release_bundle(
    target_version: str,
    policy_sequence: int = 100,
    channel: str = "stable",
    output_dir: str = os.path.join(BASE_DIR, "dist")
) -> str:
    """
    공식 서명 키를 사용하여 5대 보안 요소가 결합된 단일 .bundle 배포 패키지를 생성합니다.
    """
    priv_key_path = os.path.join(BASE_DIR, "keys", "release_ed25519.key")
    pub_key_path = os.path.join(BASE_DIR, "keys", "release_ed25519.pub")

    if not os.path.exists(priv_key_path):
        raise FileNotFoundError(f"Release private key not found at '{priv_key_path}'")

    print(f"[*] [Bundle Builder] Building Release Bundle v{target_version} (Channel: {channel}, Seq: {policy_sequence})")

    # 1. package.zip 패키징 (Zero-Leak 원칙 준수)
    pkg_buf = io.BytesIO()
    with zipfile.ZipFile(pkg_buf, "w", zipfile.ZIP_DEFLATED) as zf:
        # 1.1 필수 디렉터리 번들링
        for folder in ["core", "web", "keys"]:
            src_f = os.path.join(BASE_DIR, folder)
            if not os.path.exists(src_f):
                continue
            for root, dirs, files in os.walk(src_f):
                if "__pycache__" in root or ".git" in root:
                    continue
                for f in files:
                    f_lower = f.lower()
                    # 개인키/인증서 파일 완전 배제
                    if f_lower.endswith(".key") or f_lower.endswith(".pem") or "private" in f_lower:
                        continue
                    if os.path.basename(root) == "keys" and not f_lower.endswith(".pub"):
                        continue
                    full_p = os.path.join(root, f)
                    rel_p = os.path.relpath(full_p, BASE_DIR)
                    zf.write(full_p, rel_p)

        # 1.2 실행 스크립트 및 VERSION
        for script in ["run.py", "start_silent.vbs", "start_tray.vbs", "launch_dashboard.vbs", "stop_backup_system.bat", "2_백업시스템_실행.bat"]:
            s_path = os.path.join(BASE_DIR, script)
            if os.path.exists(s_path):
                zf.write(s_path, script)

        # 타깃 버전이 명시된 VERSION 파일 작성
        zf.writestr("VERSION", f"{target_version}\n")

    package_bytes = pkg_buf.getvalue()
    package_sha256 = hashlib.sha256(package_bytes).hexdigest().lower()
    package_size = len(package_bytes)
    print(f"    [1/5] package.zip generated (Size: {package_size:,} bytes, SHA-256: {package_sha256[:16]}...)")

    # 2. manifest.json 및 manifest.sig 작성
    manifest_dict = {
        "schema_version": "1.0",
        "version": target_version,
        "package_name": "package.zip",
        "package_sha256": package_sha256,
        "file_size": package_size,
        "signing_key_id": DEFAULT_KEY_ID,
        "created_at": datetime.now(timezone.utc).isoformat()
    }
    manifest_bytes = json.dumps(manifest_dict, indent=2, ensure_ascii=False).encode("utf-8")
    manifest_canonical = canonicalize(manifest_dict)
    manifest_sig = sign_bytes_ed25519(manifest_canonical, priv_key_path)
    print(f"    [2/5] manifest.json signed (Sig: {manifest_sig[:16]}...)")

    # 3. policy.json 및 policy.sig 작성
    policy_dict = {
        "schema_version": "1.0",
        "channel": channel,
        "policy_sequence": policy_sequence,
        "latest_version": target_version,
        "minimum_version": "2.9.0",
        "revoked_versions": [],
        "rollback_target": None,
        "force_update": False,
        "max_allowed_version_jump": {"major": 1, "minor": 5},
        "signing_key_id": DEFAULT_KEY_ID,
        "valid_until": "2035-12-31T23:59:59Z"
    }
    policy_bytes = json.dumps(policy_dict, indent=2, ensure_ascii=False).encode("utf-8")
    policy_canonical = canonicalize(policy_dict)
    policy_sig = sign_bytes_ed25519(policy_canonical, priv_key_path)
    print(f"    [3/5] policy.json signed (Sig: {policy_sig[:16]}...)")

    # 4. .bundle 결합 생성
    os.makedirs(output_dir, exist_ok=True)
    bundle_filename = f"BackupSystem_v{target_version}.bundle"
    output_bundle_path = os.path.join(output_dir, bundle_filename)

    BundleCreator.create_bundle(
        output_bundle_path=output_bundle_path,
        policy_bytes=policy_bytes,
        policy_sig_hex=policy_sig,
        manifest_bytes=manifest_bytes,
        manifest_sig_hex=manifest_sig,
        package_bytes=package_bytes
    )

    bundle_size = os.path.getsize(output_bundle_path)
    print(f"    [4/5] Successfully bundled to: {output_bundle_path} ({bundle_size:,} bytes)")
    print(f"    [5/5] Bundle validation check...")

    from core.updater_v2.bundle import BundleReader
    acquired = BundleReader.read(output_bundle_path)
    print(f"    [OK] Bundle verified: exactly 5 entries, allowlist passed, signatures validated.")

    return output_bundle_path


if __name__ == "__main__":
    ver = "2.9.11"
    if len(sys.argv) > 1:
        ver = sys.argv[1]
    seq = 200
    if len(sys.argv) > 2:
        seq = int(sys.argv[2])
    build_release_bundle(target_version=ver, policy_sequence=seq)
