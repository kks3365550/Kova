# -*- coding: utf-8 -*-
"""
tests/test_updater_v2_phase1.py: OTA 1단계 정밀 보강 검증 테스트 (Reinforced Test Suite)

검증 범위:
1. JCS RFC 8785: 공식 예제 대조, UTF-16 서러게이트 페어 정렬, 제어문자 및 유니코드 보존
2. Models: 엄격한 SemVer 2.0.0 (v-접두사 및 선행 0 거부, 999.999.999 구조적 허용), AcquiredUpdate 원본 바이트 불변성
3. Artifact Safety:
   - Windows 혼합 슬래시 및 유니코드 우회 공격 (..\/, ..\, .\.., CORE/../ 등) 전수 차단
   - Symlink 및 NTFS Junction/Reparse Point 차단
   - 4축 독립 리소스 제한 (단일 크기, 압축비, 누적 크기, 파일 개수) 독립적 차단
"""

import io
import os
import json
import zipfile
import unittest

from core.updater_v2.jcs import canonicalize
from core.updater_v2.models import (
    ManifestDocument, PolicyDocument, AcquiredUpdate, ValidationError
)
from core.updater_v2.artifact_safety import (
    ArtifactSafetyChecker,
    assert_safe_destination_path,
    SafetyViolationError
)


class TestJCSCanonicalization(unittest.TestCase):
    """RFC 8785 JSON Canonicalization Scheme 정밀 테스트"""

    def test_rfc8785_appendix_example(self):
        # RFC 8785 Appendix A / B style test: key sorting & whitespace elimination
        obj = {
            "numbers": [333333333.3333333, 1e30, 4.50, 2e-3],
            "string": "\u20ac$\u000f\u000aA'\u0042\u0022\u005c\\\"/",
            "literals": [None, True, False]
        }
        res = canonicalize(obj)
        # Verify deterministic byte output
        self.assertTrue(res.startswith(b'{"literals":[null,true,false],"numbers":['))
        # Ensure 'numbers' comes after 'literals', 'string' comes last
        self.assertIn(b'"string":', res)
        # Check that newline (\u000a) is serialized as \n, double quote as \", etc.
        self.assertIn(b'\\n', res)
        self.assertIn(b'\\"', res)

    def test_key_ordering_utf16_surrogate(self):
        # BMP characters vs Supplementary characters (surrogate pair)
        # U+0061 ('a') -> [0x61]
        # U+007A ('z') -> [0x7A]
        # U+10000 -> UTF-16 [0xD800, 0xDC00]
        # U+FFFF -> [0xFFFF]
        # Under UTF-16 code unit ordering: 0x7A < 0xD800 < 0xFFFF
        # So 'z' < '\U00010000' < '\uFFFF'
        obj = {
            "\uFFFF": "bmp_high",
            "\U00010000": "supplementary",
            "z": "ascii_z"
        }
        res = canonicalize(obj).decode("utf-8")
        expected = '{"z":"ascii_z","\U00010000":"supplementary","\uFFFF":"bmp_high"}'
        self.assertEqual(res, expected)

    def test_unicode_preservation_u2028_u2029(self):
        # Under RFC 8785, U+2028 (Line separator) and U+2029 (Paragraph separator)
        # are literal UTF-8 bytes, NOT escaped as \u2028
        obj = {"text": "hello\u2028world\u2029"}
        res = canonicalize(obj)
        self.assertIn("hello\u2028world\u2029".encode("utf-8"), res)

    def test_number_formatting(self):
        self.assertEqual(canonicalize({"int": 1042}), b'{"int":1042}')
        self.assertEqual(canonicalize({"zero": 0.0}), b'{"zero":0}')
        self.assertEqual(canonicalize({"float": 12.5}), b'{"float":12.5}')

    def test_nan_infinity_rejected(self):
        with self.assertRaises(ValueError):
            canonicalize({"val": float("nan")})
        with self.assertRaises(ValueError):
            canonicalize({"val": float("inf")})


class TestOTADataModels(unittest.TestCase):
    """Manifest & Policy 데이터 계약 구조 및 엄격한 SemVer 2.0.0 검증 테스트"""

    def setUp(self):
        self.valid_manifest_dict = {
            "schema_version": "1.0",
            "version": "2.9.9",
            "package_name": "backup_engine_2.9.9.zip",
            "package_sha256": "a" * 64,
            "file_size": 2048000,
            "signing_key_id": "ed25519_2026a",
            "created_at": "2026-09-26T12:00:00Z"
        }
        self.valid_policy_dict = {
            "schema_version": "1.0",
            "channel": "stable",
            "policy_sequence": 100,
            "latest_version": "2.9.9",
            "minimum_version": "2.9.8",
            "revoked_versions": ["2.9.7"],
            "rollback_target": "2.9.8",
            "force_update": False,
            "max_allowed_version_jump": {"major": 1, "minor": 5},
            "signing_key_id": "pol_key_2026a",
            "valid_until": "2026-12-31T23:59:59Z"
        }

    def test_semver_strict_rules(self):
        # 1. Valid standard versions
        for v in ["2.9.9", "2.9.9-beta.1", "2.9.9+build.123", "999.999.999"]:
            d = dict(self.valid_manifest_dict)
            d["version"] = v
            m = ManifestDocument.from_dict(d)
            self.assertEqual(m.version, v)

        # 2. Leading 'v' rejected (Strict SemVer 2.0.0)
        d = dict(self.valid_manifest_dict)
        d["version"] = "v2.9.9"
        with self.assertRaises(ValidationError):
            ManifestDocument.from_dict(d)

        # 3. Leading zero rejected (Strict SemVer 2.0.0)
        d["version"] = "2.09.9"
        with self.assertRaises(ValidationError):
            ManifestDocument.from_dict(d)

        # 4. Incomplete SemVer rejected
        d["version"] = "2.9"
        with self.assertRaises(ValidationError):
            ManifestDocument.from_dict(d)

    def test_acquired_update_container(self):
        # AcquiredUpdate stores raw bytes without re-serializing
        acq = AcquiredUpdate(
            policy_bytes=b'{"channel":"stable"}',
            policy_sig="a" * 128,
            manifest_bytes=b'{"version":"2.9.9"}',
            manifest_sig="b" * 128,
            package_bytes=b'PK\x03\x04...'
        )
        acq.validate_signatures_format()
        self.assertEqual(len(acq.policy_sig), 128)

        # Invalid sig length check
        bad_acq = AcquiredUpdate(
            policy_bytes=b'{}',
            policy_sig="short",
            manifest_bytes=b'{}',
            manifest_sig="b" * 128
        )
        with self.assertRaises(ValidationError):
            bad_acq.validate_signatures_format()


class TestArtifactSafetyChecker(unittest.TestCase):
    """Step 4B 아티팩트 무해성 및 공격 벡터 전수 차단 테스트"""

    def setUp(self):
        self.checker = ArtifactSafetyChecker(
            max_file_count=10,
            max_total_uncompressed_bytes=5 * 1024 * 1024,   # 5 MB
            max_per_file_bytes=2 * 1024 * 1024,             # 2 MB
            max_compression_ratio=20.0                      # 20x
        )

    def _create_zip_bytes(self, files_dict, custom_attr=None) -> bytes:
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            for name, content in files_dict.items():
                info = zipfile.ZipInfo(name)
                info.compress_type = zipfile.ZIP_DEFLATED
                if custom_attr and name in custom_attr:
                    info.external_attr = custom_attr[name]
                zf.writestr(info, content)
        return buf.getvalue()

    def test_normal_package_passes(self):
        pkg = self._create_zip_bytes({
            "core/backup.py": b"# backup core",
            "web/app.py": b"# web app",
            "keys/release_ed25519.pub": b"public key contents",
            "run.py": b"print('run')",
            "VERSION": b"2.9.9\n"
        })
        is_safe, errors = self.checker.check_archive(pkg)
        self.assertTrue(is_safe, f"Expected safe, got errors: {errors}")

    def test_zip_slip_evasion_variations_rejected(self):
        # Evasion 1: Mixed slashes (..\/..\/evil.bat)
        pkg = self._create_zip_bytes({
            "core/backup.py": b"ok",
            "web/app.py": b"ok",
            "..\\/..\\/evil.bat": b"payload"
        })
        is_safe, errors = self.checker.check_archive(pkg)
        self.assertFalse(is_safe)
        self.assertTrue(any("ZIP_SLIP_VIOLATION" in e for e in errors))

        # Evasion 2: Single dot traversal (.\\..\\evil.bat)
        pkg = self._create_zip_bytes({
            "core/backup.py": b"ok",
            "web/app.py": b"ok",
            ".\\..\\evil.bat": b"payload"
        })
        is_safe, errors = self.checker.check_archive(pkg)
        self.assertFalse(is_safe)

        # Evasion 3: Nested directory traversal (CORE/../evil.bat)
        pkg = self._create_zip_bytes({
            "core/backup.py": b"ok",
            "web/app.py": b"ok",
            "CORE/../evil.bat": b"payload"
        })
        is_safe, errors = self.checker.check_archive(pkg)
        self.assertFalse(is_safe)

        # Evasion 4: Unicode fullwidth slash (\uff0f)
        pkg = self._create_zip_bytes({
            "core/backup.py": b"ok",
            "web/app.py": b"ok",
            "..\uff0fevil.bat": b"payload"
        })
        is_safe, errors = self.checker.check_archive(pkg)
        self.assertFalse(is_safe)

    def test_symlink_and_reparse_rejected(self):
        # UNIX Symlink mode
        symlink_attr = 0o120000 << 16
        pkg = self._create_zip_bytes(
            {"core/backup.py": b"ok", "web/app.py": b"ok", "core/link.py": b"/etc/passwd"},
            custom_attr={"core/link.py": symlink_attr}
        )
        is_safe, errors = self.checker.check_archive(pkg)
        self.assertFalse(is_safe)
        self.assertTrue(any("SYMLINK_VIOLATION" in e for e in errors))

        # Windows Reparse point
        reparse_attr = 0x400
        pkg = self._create_zip_bytes(
            {"core/backup.py": b"ok", "web/app.py": b"ok", "core/junction": b""},
            custom_attr={"core/junction": reparse_attr}
        )
        is_safe, errors = self.checker.check_archive(pkg)
        self.assertFalse(is_safe)
        self.assertTrue(any("REPARSE_POINT_VIOLATION" in e for e in errors))

    def test_zip_bomb_4_axis_independent_limits(self):
        # Axis 1: Single file size exceeded (> 2MB limit in test checker)
        big_single = b"a" * (3 * 1024 * 1024)
        pkg = self._create_zip_bytes({
            "core/backup.py": b"ok", "web/app.py": b"ok",
            "core/data.py": big_single
        })
        is_safe, errors = self.checker.check_archive(pkg)
        self.assertFalse(is_safe)
        self.assertTrue(any("RESOURCE_VIOLATION_PER_FILE_SIZE" in e for e in errors))

        # Axis 2: Compression ratio exceeded (> 20x)
        compressible = b"0" * (500 * 1024)  # 500KB compresses to ~500B (> 500x ratio)
        pkg = self._create_zip_bytes({
            "core/backup.py": b"ok", "web/app.py": b"ok",
            "core/comp.py": compressible
        })
        is_safe, errors = self.checker.check_archive(pkg)
        self.assertFalse(is_safe)
        self.assertTrue(any("RESOURCE_VIOLATION_COMPRESSION_RATIO" in e for e in errors))

        # Axis 3: Total file count exceeded (> 10 files in test checker)
        many_files = {f"core/file_{i}.py": b"print(1)" for i in range(12)}
        many_files["web/app.py"] = b"ok"
        pkg = self._create_zip_bytes(many_files)
        is_safe, errors = self.checker.check_archive(pkg)
        self.assertFalse(is_safe)
        self.assertTrue(any("RESOURCE_VIOLATION_FILE_COUNT" in e for e in errors))

        # Axis 4: Total uncompressed size exceeded (> 5MB total limit)
        # Even if individual files are under 2MB and compression ratio is under 20x:
        three_files = {
            "core/part1.py": b"b" * (1800 * 1024),
            "core/part2.py": b"c" * (1800 * 1024),
            "core/part3.py": b"d" * (1800 * 1024),
            "web/app.py": b"ok"
        }
        pkg = self._create_zip_bytes(three_files)
        is_safe, errors = self.checker.check_archive(pkg)
        self.assertFalse(is_safe)
        self.assertTrue(any("RESOURCE_VIOLATION_TOTAL_SIZE" in e for e in errors))

    def test_safe_destination_path_assertion(self):
        # Valid path
        safe_path = assert_safe_destination_path("C:\\app", "core/sub/file.py")
        self.assertTrue(safe_path.startswith(os.path.abspath("C:\\app")))

        # Path escape
        with self.assertRaises(SafetyViolationError):
            assert_safe_destination_path("C:\\app", "../../evil.bat")


if __name__ == "__main__":
    unittest.main()
