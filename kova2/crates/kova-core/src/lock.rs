//! 저장소 잠금: 한 저장소에 쓰기 작업(백업·정리·복구)이 동시에 둘 이상 돌지 않게 한다.
//! OS 파일 잠금을 쓰므로 프로세스가 죽으면 잠금이 자동으로 풀린다(낡은 잠금 파일이 남지 않는다).
use std::fs::{File, OpenOptions, TryLockError};
use std::io;
use std::path::{Path, PathBuf};

pub const LOCK_FILE_NAME: &str = ".kova.lock";

#[derive(Debug)]
pub enum LockError {
    /// 다른 작업이 이미 저장소를 쓰고 있다
    Busy,
    Io(io::Error),
}

impl std::fmt::Display for LockError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            LockError::Busy => write!(f, "repository is locked by another operation"),
            LockError::Io(e) => write!(f, "{}", e),
        }
    }
}

impl std::error::Error for LockError {}

pub struct RepoLock {
    file: File,
    path: PathBuf,
}

impl RepoLock {
    /// 잠금을 즉시 시도한다. 이미 잠겨 있으면 `LockError::Busy`.
    pub fn try_acquire(repo: &Path) -> Result<RepoLock, LockError> {
        std::fs::create_dir_all(repo).map_err(LockError::Io)?;
        let path = repo.join(LOCK_FILE_NAME);
        let file = OpenOptions::new().create(true).read(true).write(true).open(&path).map_err(LockError::Io)?;
        match file.try_lock() {
            Ok(()) => {}
            Err(TryLockError::WouldBlock) => return Err(LockError::Busy),
            Err(TryLockError::Error(e)) => return Err(LockError::Io(e)),
        }
        Ok(RepoLock { file, path })
    }

    pub fn path(&self) -> &Path {
        &self.path
    }
}

impl Drop for RepoLock {
    fn drop(&mut self) {
        let _ = self.file.unlock();
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn second_lock_is_busy_until_first_is_dropped() {
        let d = tempfile::tempdir().unwrap();
        let a = RepoLock::try_acquire(d.path()).unwrap();
        assert!(matches!(RepoLock::try_acquire(d.path()), Err(LockError::Busy)));
        drop(a);
        assert!(RepoLock::try_acquire(d.path()).is_ok());
    }

    #[test]
    fn different_repos_do_not_conflict() {
        let d1 = tempfile::tempdir().unwrap();
        let d2 = tempfile::tempdir().unwrap();
        let _a = RepoLock::try_acquire(d1.path()).unwrap();
        assert!(RepoLock::try_acquire(d2.path()).is_ok());
    }
}
