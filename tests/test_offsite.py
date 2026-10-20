# -*- coding: utf-8 -*-
"""
tests/test_offsite.py - 오프사이트 복제 엔진 회귀 테스트

검증 대상 (실측에서 발견한 함정 포함):
  1. robocopy 요약 출력 파싱 - 로케일 한글 라벨("파일") 대응
  2. 인코딩 함정: encoding="utf-8" 지정 시 cp949 출력이 깨져 파싱이 전부 0이 됨
  3. 사전 점검 - 원격 미도달 시 스킵(백업 결과에 영향 없음)
  4. 증분 복제 - 이미 복제된 파일은 건너뜀
"""

import os
import sys
import shutil
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.offsite import OffsiteReplicator


class TestOffsiteParsing(unittest.TestCase):
    """robocopy 출력 파싱 회귀 테스트"""

    def test_parse_korean_locale_summary(self):
        """한글 로케일 요약 블록 파싱 (실측 형식)"""
        out = (
            "                  전체       복사됨      건너뜀     불일치       실패        추가\n"
            "    디렉터리 :         2         2         0         0         0         0\n"
            "      파일 :         3         3         0         0         0         0\n"
            "     바이트 :       300       300         0         0         0         0\n"
        )
        r = OffsiteReplicator._parse_robocopy_output(out)
        self.assertEqual(r["files_total"], 3)
        self.assertEqual(r["files_copied"], 3)
        self.assertEqual(r["files_failed"], 0)

    def test_parse_english_locale_summary(self):
        """영문 로케일 요약 블록 파싱"""
        out = (
            "               Total    Copied   Skipped  Mismatch    FAILED    Extras\n"
            "    Dirs :         2         2         0         0         0         0\n"
            "   Files :     84192     47642     36550         0         0         0\n"
            "   Bytes :  9.939 g   5.343 g   4.596 g         0         0         0\n"
        )
        r = OffsiteReplicator._parse_robocopy_output(out)
        self.assertEqual(r["files_total"], 84192)
        self.assertEqual(r["files_copied"], 47642)
        self.assertEqual(r["files_failed"], 0)

    def test_parse_ignores_filter_line(self):
        """'파일 : *.*' 필터 설정 줄을 Files 행으로 오인하지 않아야 한다"""
        out = (
            "        파일 : *.*\n"
            "      파일 :         3         3         0         0         0         0\n"
        )
        r = OffsiteReplicator._parse_robocopy_output(out)
        self.assertEqual(r["files_total"], 3, "필터 줄(*.*)을 오인했다")

    def test_parse_empty_output(self):
        """빈 출력이면 예외 없이 0 반환"""
        r = OffsiteReplicator._parse_robocopy_output("")
        self.assertEqual(r["files_total"], 0)
        self.assertEqual(r["files_copied"], 0)
        self.assertEqual(r["files_failed"], 0)

    def test_parse_with_thousands_separator(self):
        """쉼표 구분 숫자 파싱"""
        out = (
            "   Files : 84,192 47,642 36,550 0 0 0\n"
        )
        r = OffsiteReplicator._parse_robocopy_output(out)
        self.assertEqual(r["files_total"], 84192)
        self.assertEqual(r["files_copied"], 47642)


class TestOffsiteReplication(unittest.TestCase):
    """실제 robocopy 실행 회귀 테스트"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="offsite_test_")
        self.src = os.path.join(self.tmp, "src")
        self.dst = os.path.join(self.tmp, "dst")
        os.makedirs(os.path.join(self.src, "blobs", "aa"), exist_ok=True)
        for i in range(3):
            with open(os.path.join(self.src, "blobs", "aa", "b%d.blob" % i), "wb") as f:
                f.write(b"x" * 100)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_first_run_copies_all(self):
        """1회차: 신규 파일 모두 복제"""
        res = OffsiteReplicator(self.src, self.dst, timeout_sec=60).replicate()
        self.assertTrue(res["success"], res["reason"])
        self.assertEqual(res["files_total"], 3)
        self.assertEqual(res["files_copied"], 3)
        self.assertEqual(res["files_failed"], 0)

    def test_incremental_skips_existing(self):
        """2회차: 변경 없으면 복제 0건 (인코딩 버그 시 3으로 회귀)"""
        OffsiteReplicator(self.src, self.dst, timeout_sec=60).replicate()
        res = OffsiteReplicator(self.src, self.dst, timeout_sec=60).replicate()
        self.assertTrue(res["success"])
        self.assertEqual(res["files_copied"], 0,
                         "files_copied=0이어야 한다 (인코딩/파싱 회귀 suspicion)")

    def test_incremental_copies_only_new(self):
        """3회차: 신규 1개만 복제"""
        OffsiteReplicator(self.src, self.dst, timeout_sec=60).replicate()
        with open(os.path.join(self.src, "blobs", "aa", "new.blob"), "wb") as f:
            f.write(b"y" * 100)
        res = OffsiteReplicator(self.src, self.dst, timeout_sec=60).replicate()
        self.assertTrue(res["success"])
        self.assertEqual(res["files_total"], 4)
        self.assertEqual(res["files_copied"], 1)

    def test_preflight_local_missing(self):
        """로컬 저장소 없으면 스킵(실패 아님)"""
        rep = OffsiteReplicator(os.path.join(self.tmp, "nope"), self.dst)
        pf = rep.preflight()
        self.assertFalse(pf["ok"])
        self.assertIn("로컬 저장소", pf["reason"])

    def test_preflight_unc_detection(self):
        """UNC 경로 인식 여부"""
        rep = OffsiteReplicator(self.src, r"\\server\share\repo")
        pf = rep.preflight()
        self.assertTrue(pf["is_unc"], "UNC 경로로 인식되어야 한다")
        # 도달 불가하므로 ok는 False
        self.assertFalse(pf["ok"])


class TestGoogleDriveOffsite(unittest.TestCase):
    """Google Drive 오프사이트 복제 엔진 테스트"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="gdrive_offsite_test_")
        self.src = os.path.join(self.tmp, "src")
        os.makedirs(os.path.join(self.src, "blobs", "bb"), exist_ok=True)
        with open(os.path.join(self.src, "blobs", "bb", "sample.blob"), "wb") as f:
            f.write(b"data" * 50)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_gdrive_missing_credentials(self):
        from core.offsite import GoogleDriveOffsiteReplicator
        rep = GoogleDriveOffsiteReplicator(self.src, credentials_file="C:\\nonexistent\\key.json")
        pf = rep.preflight()
        self.assertFalse(pf["ok"])
        self.assertTrue("Service Account" in pf["reason"] or "JSON" in pf["reason"])

    def test_gdrive_preflight_and_replication_mocked(self):
        from unittest.mock import patch, MagicMock
        from core.offsite import GoogleDriveOffsiteReplicator

        # Dummy credentials file
        cred_path = os.path.join(self.tmp, "creds.json")
        with open(cred_path, "w", encoding="utf-8") as f:
            f.write('{"client_email": "backup-bot@gdrive.iam.gserviceaccount.com"}')

        with patch("core.offsite.g_service_account") as mock_sa, \
             patch("core.offsite.g_discovery") as mock_disc, \
             patch("subprocess.Popen") as mock_popen, \
             patch.object(GoogleDriveOffsiteReplicator, "_get_rclone_exe", return_value="mock_rclone.exe"), \
             patch.object(GoogleDriveOffsiteReplicator, "_ensure_rclone_config", return_value="mock.conf"):
            
            mock_creds = MagicMock()
            mock_sa.Credentials.from_service_account_file.return_value = mock_creds
            
            mock_service = MagicMock()
            mock_disc.build.return_value = mock_service

            # Mock about().get()
            mock_about = MagicMock()
            mock_about.execute.return_value = {
                "storageQuota": {"limit": "10737418240", "usage": "1073741824"},
                "user": {"emailAddress": "backup-bot@gdrive.iam.gserviceaccount.com"}
            }
            mock_service.about().get.return_value = mock_about

            # Mock files().list()
            mock_files_list = MagicMock()
            mock_files_list.execute.return_value = {
                "files": [{"id": "folder_123", "name": "BackupSystem_Offsite"}]
            }
            mock_service.files().list.return_value = mock_files_list

            # Mock files().create()
            mock_create = MagicMock()
            mock_create.next_chunk.return_value = (None, {"id": "file_new_999"})
            mock_create.execute.return_value = {"id": "file_new_999"}
            mock_service.files().create.return_value = mock_create

            # Mock Rclone subprocess execution
            mock_proc = MagicMock()
            mock_proc.wait.return_value = 0
            mock_proc.stdout = []
            mock_popen.return_value = mock_proc

            rep = GoogleDriveOffsiteReplicator(self.src, credentials_file=cred_path, folder_name_or_id="BackupSystem_Offsite")
            pf = rep.preflight()
            self.assertTrue(pf["ok"])
            self.assertEqual(pf["user_email"], "backup-bot@gdrive.iam.gserviceaccount.com")
            self.assertEqual(pf["free_gb"], 9.0)

            res = rep.replicate()
            self.assertTrue(res["success"])
            self.assertEqual(res["provider"], "gdrive")
            self.assertIn("files_copied", res)

    def test_gdrive_quoted_credentials_path(self):
        from core.offsite import GoogleDriveOffsiteReplicator
        cred_path = os.path.join(self.src, "creds.json")
        with open(cred_path, "w", encoding="utf-8") as f:
            f.write('{"type": "service_account"}')

        # Test with surrounding double quotes (as copied from Windows Explorer)
        quoted_path = f'"{cred_path}"'
        rep = GoogleDriveOffsiteReplicator(self.src, credentials_file=quoted_path)
        self.assertEqual(rep.credentials_file, cred_path)
        self.assertTrue(os.path.exists(rep.credentials_file))

        # Test with surrounding single quotes
        single_quoted = f"'{cred_path}'"
        rep2 = GoogleDriveOffsiteReplicator(self.src, credentials_file=single_quoted)
        self.assertEqual(rep2.credentials_file, cred_path)
        self.assertTrue(os.path.exists(rep2.credentials_file))


if __name__ == "__main__":
    unittest.main(verbosity=2)


