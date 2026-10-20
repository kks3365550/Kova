# -*- coding: utf-8 -*-
"""
core/updater_v2/bundle.py: 오프라인 배포 번들(.bundle) 안전 파서 및 패키징 유틸리티 (Phase 5-A Hardened)

보안 강화 수칙:
1. 엔트리 개수 및 Allowlist 엄격 검증:
   - Outer ZIP의 엔트리는 정확히 5개여야 함: {"policy.json", "policy.sig", "manifest.json", "manifest.sig", "package.zip"}
   - 미승인 파일, 디렉터리 엔트리, 숨김 파일 등이 포함되면 즉시 REJECT.
2. 중복 엔트리(Duplicate ZIP Entry) 탐지:
   - 동일한 파일명이 2개 이상 존재하는 해석 차이 공격(Interpretation Divergence Attack) 즉시 REJECT.
3. 메타데이터 파일별 크기 상한:
   - policy.json: 최대 64 KB (65,536 bytes)
   - policy.sig: 최대 1 KB (1,024 bytes)
   - manifest.json: 최대 64 KB (65,536 bytes)
   - manifest.sig: 최대 1 KB (1,024 bytes)
4. Payload (package.zip) 크기 및 압축비 검증:
   - package.zip 압축 해제 용량 최대 150 MB
   - Outer compression ratio 검증 (압축비 > 50.0x 거부)
5. 파일명 이상치 탐지:
   - 경로 구분자(/, \\), 상위 경로(..), 제어 문자, 비ASCII 문자 포함 시 즉시 REJECT.
6. 디스크 미추출(In-Memory Only):
   - 디스크 extractall() 호출 금지, 메모리 상에서만 바이트 추출.
"""

import io
import os
import re
import zipfile
from typing import Union, Set

from .models import AcquiredUpdate, ValidationError

ALLOWED_EXACT_ENTRIES: Set[str] = {
    "policy.json",
    "policy.sig",
    "manifest.json",
    "manifest.sig",
    "package.zip"
}

MAX_POLICY_JSON_SIZE = 64 * 1024        # 64 KB
MAX_POLICY_SIG_SIZE = 1024              # 1 KB
MAX_MANIFEST_JSON_SIZE = 64 * 1024      # 64 KB
MAX_MANIFEST_SIG_SIZE = 1024            # 1 KB
MAX_PACKAGE_ZIP_SIZE = 150 * 1024 * 1024  # 150 MB
MAX_OUTER_COMPRESSION_RATIO = 50.0

SAFE_FILENAME_PATTERN = re.compile(r"^[a-zA-Z0-9._-]+$")


class BundleError(Exception):
    """번들 처리 기본 예외"""
    pass


class BundleFormatError(BundleError):
    """번들 구조 또는 보안 제약 위반 예외"""
    pass


class BundleReader:
    """오프라인 .bundle 파일을 인메모리에서 전수 검증 및 파싱하는 하든드 리더"""

    @classmethod
    def read(cls, bundle_source: Union[str, bytes]) -> AcquiredUpdate:
        """
        .bundle 파일 경로 또는 바이너리 바이트로부터 AcquiredUpdate를 안전하게 구성합니다.
        """
        try:
            if isinstance(bundle_source, bytes):
                zf = zipfile.ZipFile(io.BytesIO(bundle_source), "r")
            else:
                if not os.path.exists(bundle_source):
                    raise BundleError(f"Bundle file not found: {bundle_source}")
                zf = zipfile.ZipFile(bundle_source, "r")
        except zipfile.BadZipFile as e:
            raise BundleFormatError(f"Invalid .bundle zip archive: {e}")

        with zf:
            infolist = zf.infolist()

            # 1. 엔트리 개수 정확히 5개 검증
            if len(infolist) != 5:
                raise BundleFormatError(
                    f"Bundle must contain exactly 5 entries, got {len(infolist)}"
                )

            # 2. 중복 엔트리 및 Allowlist 검증
            seen_names: Set[str] = set()
            for info in infolist:
                fname = info.filename

                # 2.1 파일명 안전 문자 및 경로 구분자 검사
                if not SAFE_FILENAME_PATTERN.match(fname):
                    raise BundleFormatError(
                        f"Disallowed or malicious filename pattern in bundle: '{fname}'"
                    )

                if "/" in fname or "\\" in fname or ".." in fname:
                    raise BundleFormatError(
                        f"Path traversal or directory separator detected in bundle entry: '{fname}'"
                    )

                # 2.2 디렉터리 엔트리 거부
                if info.is_dir() or fname.endswith("/"):
                    raise BundleFormatError(
                        f"Directory entry not allowed in bundle: '{fname}'"
                    )

                # 2.3 중복 엔트리 거부 (해석 차이 공격 방어)
                if fname in seen_names:
                    raise BundleFormatError(
                        f"Duplicate entry detected in bundle: '{fname}'"
                    )
                seen_names.add(fname)

                # 2.4 Allowlist 검증
                if fname not in ALLOWED_EXACT_ENTRIES:
                    raise BundleFormatError(
                        f"Unapproved entry in bundle: '{fname}'"
                    )

                # 3. 메타데이터 및 페이로드 크기/압축비 검증
                uncompressed = info.file_size
                compressed = info.compress_size

                if fname == "policy.json" and uncompressed > MAX_POLICY_JSON_SIZE:
                    raise BundleFormatError(
                        f"policy.json size ({uncompressed} bytes) exceeds limit ({MAX_POLICY_JSON_SIZE} bytes)"
                    )
                elif fname == "policy.sig" and uncompressed > MAX_POLICY_SIG_SIZE:
                    raise BundleFormatError(
                        f"policy.sig size ({uncompressed} bytes) exceeds limit ({MAX_POLICY_SIG_SIZE} bytes)"
                    )
                elif fname == "manifest.json" and uncompressed > MAX_MANIFEST_JSON_SIZE:
                    raise BundleFormatError(
                        f"manifest.json size ({uncompressed} bytes) exceeds limit ({MAX_MANIFEST_JSON_SIZE} bytes)"
                    )
                elif fname == "manifest.sig" and uncompressed > MAX_MANIFEST_SIG_SIZE:
                    raise BundleFormatError(
                        f"manifest.sig size ({uncompressed} bytes) exceeds limit ({MAX_MANIFEST_SIG_SIZE} bytes)"
                    )
                elif fname == "package.zip":
                    if uncompressed > MAX_PACKAGE_ZIP_SIZE:
                        raise BundleFormatError(
                            f"package.zip uncompressed size ({uncompressed} bytes) exceeds limit ({MAX_PACKAGE_ZIP_SIZE} bytes)"
                        )
                    if uncompressed > 1024 and compressed > 0:
                        ratio = uncompressed / compressed
                        if ratio > MAX_OUTER_COMPRESSION_RATIO:
                            raise BundleFormatError(
                                f"package.zip excessive compression ratio ({ratio:.1f}x) in outer bundle"
                            )

            # 4. 필수 파일 5개 존재 여부 최종 확인
            if seen_names != ALLOWED_EXACT_ENTRIES:
                missing = ALLOWED_EXACT_ENTRIES - seen_names
                raise BundleFormatError(f"Bundle is missing required entries: {missing}")

            # 5. 메모리 바이트 안전 추출
            policy_bytes = zf.read("policy.json")
            try:
                policy_sig_raw = zf.read("policy.sig").decode("utf-8").strip()
            except UnicodeDecodeError as e:
                raise BundleFormatError(f"policy.sig must be UTF-8 hex string: {e}")

            manifest_bytes = zf.read("manifest.json")
            try:
                manifest_sig_raw = zf.read("manifest.sig").decode("utf-8").strip()
            except UnicodeDecodeError as e:
                raise BundleFormatError(f"manifest.sig must be UTF-8 hex string: {e}")

            package_bytes = zf.read("package.zip")

        acquired = AcquiredUpdate(
            policy_bytes=policy_bytes,
            policy_sig=policy_sig_raw,
            manifest_bytes=manifest_bytes,
            manifest_sig=manifest_sig_raw,
            package_bytes=package_bytes
        )

        try:
            acquired.validate_signatures_format()
        except ValidationError as e:
            raise BundleFormatError(f"Bundle contains invalid signature format: {e}")

        return acquired


class BundleCreator:
    """배포용 .bundle 아카이브를 빌드하는 유틸리티"""

    @classmethod
    def create_bundle(
        cls,
        output_bundle_path: str,
        policy_bytes: bytes,
        policy_sig_hex: str,
        manifest_bytes: bytes,
        manifest_sig_hex: str,
        package_bytes: bytes
    ) -> str:
        """
        5대 구성 요소를 패키징하여 단일 .bundle 파일로 안전하게 저장합니다.
        """
        os.makedirs(os.path.dirname(os.path.abspath(output_bundle_path)), exist_ok=True)
        with zipfile.ZipFile(output_bundle_path, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("policy.json", policy_bytes)
            zf.writestr("policy.sig", policy_sig_hex.strip().encode("utf-8"))
            zf.writestr("manifest.json", manifest_bytes)
            zf.writestr("manifest.sig", manifest_sig_hex.strip().encode("utf-8"))
            zf.writestr("package.zip", package_bytes)

        return output_bundle_path
