import hashlib
import os
import stat
from typing import Optional, Dict, Any

def calculate_sha256(filepath: str, block_size: int = 1048576) -> str:
    """Calculate SHA-256 hash of a file efficiently with 1MB buffer."""
    sha256 = hashlib.sha256()
    with open(filepath, 'rb') as f:
        for block in iter(lambda: f.read(block_size), b''):
            sha256.update(block)
    return sha256.hexdigest()

def calculate_bytes_sha256(data: bytes) -> str:
    """Calculate SHA-256 of byte array."""
    return hashlib.sha256(data).hexdigest()

def get_file_stat(filepath: str) -> Optional[Dict[str, Any]]:
    """Get file size and modified timestamp safely without redundant syscalls."""
    try:
        # lstat 1회로 심볼릭 링크 및 파일 기본 속성 확인
        st = os.lstat(filepath)
        is_symlink = stat.S_ISLNK(st.st_mode)

        if is_symlink:
            # 심볼릭 링크인 경우 원본 대상(target)의 stat 조회 (os.stat)
            try:
                target_st = os.stat(filepath)
                size = target_st.st_size
                mtime = target_st.st_mtime
                ctime = target_st.st_ctime
                is_dir = stat.S_ISDIR(target_st.st_mode)
                is_file = stat.S_ISREG(target_st.st_mode)
            except (PermissionError, FileNotFoundError, OSError):
                # 끊어진 링크(Broken symlink)의 경우 lstat 정보 유지
                size = st.st_size
                mtime = st.st_mtime
                ctime = st.st_ctime
                is_dir = False
                is_file = False
        else:
            # 일반 파일/디렉토리 (99.9%): 단 1회의 lstat 결과의 st_mode로 판정
            size = st.st_size
            mtime = st.st_mtime
            ctime = st.st_ctime
            is_dir = stat.S_ISDIR(st.st_mode)
            is_file = stat.S_ISREG(st.st_mode)

        return {
            'size': size,
            'mtime': mtime,
            'created': ctime,
            'is_dir': is_dir,
            'is_file': is_file,
            'is_symlink': is_symlink
        }
    except (PermissionError, FileNotFoundError, OSError):
        return None

