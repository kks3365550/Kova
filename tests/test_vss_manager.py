# -*- coding: utf-8 -*-
"""
tests/test_vss_manager.py: P0-2 VSS(Volume Shadow Copy) 관리자 단위 테스트
- 드라이브 문자 파싱 (C:\, c:/, UNC, 상대경로 등)
- 섀도 복사본 경로 변환 (get_shadow_path)
- 비관리자 모드 Fallback 및 warnings 기록 검증
- 컨텍스트 매니저 예외 상황 시 Cleanup 안전성 검증
"""

import os
import unittest
from unittest.mock import patch, MagicMock

from core.vss_manager import (
    extract_drive_letter, ShadowCopyRecord, VSSContext, is_admin
)


class TestVSSManager(unittest.TestCase):
    def test_extract_drive_letter(self):
        self.assertEqual(extract_drive_letter("C:\\Users\\admin"), "C:")
        self.assertEqual(extract_drive_letter("c:/users/admin/data"), "C:")
        self.assertEqual(extract_drive_letter("D:\\Repository"), "D:")
        self.assertEqual(extract_drive_letter("z:\\share"), "Z:")
        # 유효하지 않은 드라이브
        self.assertIsNone(extract_drive_letter("\\\\server\\share\\data"))
        self.assertIsNone(extract_drive_letter("relative/path/to/file"))
        self.assertIsNone(extract_drive_letter(""))
        self.assertIsNone(extract_drive_letter(None))

    def test_get_shadow_path_mapping(self):
        """VSS 활성화 시 원본 경로가 Shadow Copy 장치 경로로 정확히 변환되는지 검증"""
        ctx = VSSContext(["C:\\Users\\admin"], enabled=True)
        ctx.vss_active = True
        ctx.shadows["C:"] = ShadowCopyRecord(
            drive="C:",
            shadow_id="{12345678-1234-1234-1234-1234567890AB}",
            device_path="\\\\?\\GLOBALROOT\\Device\\HarddiskVolumeShadowCopy4"
        )

        orig = "C:\\Users\\admin\\file.txt"
        expected = "\\\\?\\GLOBALROOT\\Device\\HarddiskVolumeShadowCopy4\\Users\\admin\\file.txt"
        self.assertEqual(ctx.get_shadow_path(orig), expected)

        # 슬래시 경로인 경우도 확인
        orig_slash = "c:/Users/admin/docs/report.pdf"
        expected_slash = "\\\\?\\GLOBALROOT\\Device\\HarddiskVolumeShadowCopy4\\Users\\admin\\docs\\report.pdf"
        self.assertEqual(ctx.get_shadow_path(orig_slash), expected_slash)

        # VSS에 등록되지 않은 드라이브는 원본 반환
        self.assertEqual(ctx.get_shadow_path("D:\\Other\\file.txt"), "D:\\Other\\file.txt")

    def test_fallback_when_not_admin(self):
        """관리자 권한이 없을 때 strict=False인 경우 예외 없이 직접 읽기 모드로 graceful fallback하는지 검증"""
        with patch("core.vss_manager.is_admin", return_value=False):
            with VSSContext(["C:\\Users\\admin"], strict=False) as ctx:
                self.assertFalse(ctx.vss_active)
                self.assertTrue(any("관리자 권한" in w for w in ctx.warnings))
                # 섀도 경로 변환 시 원본 경로 반환
                self.assertEqual(ctx.get_shadow_path("C:\\Users\\file.txt"), "C:\\Users\\file.txt")

    def test_cleanup_on_exception(self):
        """컨텍스트 블록 내에서 예외가 발생하더라도 _delete_shadow가 반드시 호출되어 리소스가 정리되는지 검증"""
        ctx = VSSContext(["C:\\Users\\admin"], enabled=True, strict=False)
        ctx.vss_active = True
        ctx.shadows["C:"] = ShadowCopyRecord(
            drive="C:",
            shadow_id="{TEST-GUID-1234}",
            device_path="\\\\?\\GLOBALROOT\\Device\\HarddiskVolumeShadowCopy1"
        )

        with patch.object(VSSContext, "_delete_shadow", return_value=True) as mock_del:
            try:
                with ctx:
                    raise RuntimeError("Simulated unexpected crash during backup")
            except RuntimeError:
                pass

            # Cleanup이 정상 호출되었는지 확인
            mock_del.assert_called_once_with("{TEST-GUID-1234}")
            self.assertEqual(len(ctx.shadows), 0)
            self.assertFalse(ctx.vss_active)


if __name__ == "__main__":
    unittest.main()
