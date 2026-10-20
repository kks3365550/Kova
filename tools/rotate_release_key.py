# -*- coding: utf-8 -*-
"""
tools/rotate_release_key.py - 배포 서명 키 회전 도구

왜 이 도구가 있는가
------------------
배포 서명 Ed25519 개인키가 공개 저장소 이력에 유출됐다
(docs/SECURITY_20261008.md). 키를 교체하려면 전환 기간에 구 공개키와
신 공개키를 동시에 신뢰해야 하며, core/updater.py 는 이제 keys/*.pub 를
자동 탐색한다 (get_trusted_public_key_paths).

회전 절차 (2단계):
  1단계: 새 키 쌍을 만들고 공개키를 keys/ 에 추가한다.
         이 시점의 클라이언트는 구 키와 신 키를 모두 신뢰한다.
         새 키로 서명한 릴리즈를 배포한다.
  2단계: 모든 클라이언트가 새 키를 가진 릴리즈로 올라간 뒤,
         구 공개키를 제거한다. 그때부터 구 키 서명은 거부된다.

사용법
------
    python tools/rotate_release_key.py --dry-run     # 기본값. 파일에 쓰지 않음
    python tools/rotate_release_key.py --apply       # 새 키를 keys/ 에 쓴다
    python tools/rotate_release_key.py --apply --key-id v2
        # keys/release_ed25519_v2.key / .pub 로 생성

주의
----
- --apply 는 기존 keys/release_ed25519.* 를 절대 덮어쓰지 않는다.
  항상 버전 suffix 가 붙은 새 파일로만 만든다.
- 개인키는 .gitignore 대상이다 (keys/*.key). 공개키만 커밋한다.
- 구 개인키는 이 도구가 삭제하지 않는다. 사용자가 안전한 곳에 보관 후
  폐기해야 한다. git 이력에 이미 있으므로 filter-repo 정리도 별도 절차다.
  (docs/SECURITY_20261008.md 참고)
"""

import argparse
import os
import sys

BASE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, BASE_DIR)

# 표준출력 인코딩을 UTF-8 로 고정한다.
#
# windows-latest 러너의 표준출력은 cp1252 라서 한국어 print 가
# UnicodeEncodeError 로 죽는다 (tools/scan_secrets.py 와 동일한 실패).
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")


def generate_keypair():
    try:
        from cryptography.hazmat.primitives.asymmetric.ed25519 import (
            Ed25519PrivateKey,
        )
        from cryptography.hazmat.primitives import serialization
    except ImportError as e:
        raise RuntimeError("cryptography 패키지가 필요하다: %s" % e)
    priv = Ed25519PrivateKey.generate()
    priv_pem = priv.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    pub_pem = priv.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    return priv_pem, pub_pem


def main():
    ap = argparse.ArgumentParser(description="배포 서명 키 회전")
    ap.add_argument("--apply", action="store_true",
                    help="새 키를 keys/ 에 실제로 쓴다 (기본은 dry-run)")
    ap.add_argument("--key-id", default="v2",
                    help="새 키 파일 suffix (기본 v2)")
    ap.add_argument("--keys-dir", default=os.path.join(BASE_DIR, "keys"))
    args = ap.parse_args()

    priv_pem, pub_pem = generate_keypair()

    # 파일명 결정. 기존 파일을 절대 덮어쓰지 않는다.
    safe_id = "".join(c for c in args.key_id if c.isalnum() or c in ("-", "_", "v")) or "v2"
    priv_name = "release_ed25519_%s.key" % safe_id
    pub_name = "release_ed25519_%s.pub" % safe_id
    priv_path = os.path.join(args.keys_dir, priv_name)
    pub_path = os.path.join(args.keys_dir, pub_name)

    print("새 키 쌍 생성 완료 (메모리에만 있음)")
    print("  개인키 파일: %s" % priv_path)
    print("  공개키 파일: %s" % pub_path)
    print("  공개키 크기: %d bytes" % len(pub_pem))

    if os.path.exists(priv_path) or os.path.exists(pub_path):
        print("중단: 같은 이름의 파일이 이미 있다. --key-id 를 바꿔라.")
        return 2

    if not args.apply:
        print()
        print("dry-run: 파일에 쓰지 않았다.")
        print("적용하려면: python tools/rotate_release_key.py --apply --key-id %s" % safe_id)
        print("적용 후:")
        print("  1. %s 를 커밋하고 릴리즈한다 (클라이언트가 새 키를 배운다)." % pub_name)
        print("  2. 새 키로 서명한 릴리즈를 배포한다.")
        print("  3. 전 클라이언트 갱신 후 구 공개키를 제거한다.")
        return 0

    os.makedirs(args.keys_dir, exist_ok=True)
    # 개인키는 0o600. Windows 에서는 ACL 효과가 제한적이지만 명시한다.
    with open(priv_path, "wb") as f:
        f.write(priv_pem)
    try:
        os.chmod(priv_path, 0o600)
    except OSError:
        pass
    with open(pub_path, "wb") as f:
        f.write(pub_pem)

    print()
    print("적용 완료.")
    print("  개인키: %s (커밋 금지, .gitignore 대상)" % priv_path)
    print("  공개키: %s (커밋하고 릴리즈에 포함)" % pub_path)
    return 0


if __name__ == "__main__":
    sys.exit(main())