//! 작업 큐 (서비스용). 같은 프로파일은 동시에 한 개만 실행하고, 대기 중복은 합친다.
//! 시간·스레드와 무관한 상태 기계라 서비스 틱과 시험이 같은 코드를 쓴다.
use std::collections::VecDeque;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::Arc;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Kind {
    Backup,
    Verify,
    Gc,
    /// 볼륨 이미지(.qblk) 만들기. 값은 드라이브 문자(대문자).
    Image(char),
    /// 스냅샷 하나 지우기. 대상 번호는 `Job::arg`. 보호(ACL)가 걸린 파일은 SYSTEM 권한인 서비스만 지울 수 있다.
    DeleteSnapshot,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Job {
    pub id: u64,
    pub profile_id: String,
    pub kind: Kind,
    /// 작업 인자(스냅샷 삭제의 스냅샷 번호). 없으면 빈 문자열.
    pub arg: String,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Enqueue {
    Queued(u64),
    /// 같은 프로파일·종류가 이미 대기 중이라 합쳤다
    Merged(u64),
}

#[derive(Default)]
pub struct JobQueue {
    next_id: u64,
    waiting: VecDeque<Job>,
    running: Vec<(Job, Arc<AtomicBool>)>,
}

impl JobQueue {
    pub fn new() -> Self {
        Self::default()
    }

    pub fn enqueue(&mut self, profile_id: &str, kind: Kind) -> Enqueue {
        self.enqueue_with(profile_id, kind, "")
    }

    pub fn enqueue_with(&mut self, profile_id: &str, kind: Kind, arg: &str) -> Enqueue {
        if let Some(j) = self
            .waiting
            .iter()
            .find(|j| j.profile_id == profile_id && j.kind == kind && j.arg == arg)
        {
            return Enqueue::Merged(j.id);
        }
        // 이미 실행 중인 같은 작업은 정상적으로 뒤에 한 번 더 대기시킨다(실행 중 바뀐 파일을 다음 회차가 잡는다)
        self.next_id += 1;
        let id = self.next_id;
        self.waiting.push_back(Job {
            id,
            profile_id: profile_id.into(),
            kind,
            arg: arg.into(),
        });
        Enqueue::Queued(id)
    }

    /// 지금 시작할 수 있는 다음 작업을 꺼낸다 (같은 프로파일이 실행 중이면 건너뛰고 뒤의 것을 본다).
    /// 반환되는 플래그는 취소 요청용이며 작업 쪽에서 주기적으로 확인한다.
    pub fn start_next(&mut self, max_concurrent: usize) -> Option<(Job, Arc<AtomicBool>)> {
        if self.running.len() >= max_concurrent {
            return None;
        }
        let pos = self.waiting.iter().position(|w| {
            !self
                .running
                .iter()
                .any(|(r, _)| r.profile_id == w.profile_id)
        })?;
        let job = self.waiting.remove(pos)?;
        let flag = Arc::new(AtomicBool::new(false));
        self.running.push((job.clone(), flag.clone()));
        Some((job, flag))
    }

    pub fn finish(&mut self, id: u64) -> bool {
        let n = self.running.len();
        self.running.retain(|(j, _)| j.id != id);
        self.running.len() != n
    }

    /// 대기 중이면 제거, 실행 중이면 취소 플래그를 세운다. 처리했으면 true.
    pub fn cancel(&mut self, id: u64) -> bool {
        if let Some(i) = self.waiting.iter().position(|j| j.id == id) {
            self.waiting.remove(i);
            return true;
        }
        if let Some((_, f)) = self.running.iter().find(|(j, _)| j.id == id) {
            f.store(true, Ordering::SeqCst);
            return true;
        }
        false
    }

    pub fn is_profile_busy(&self, profile_id: &str) -> bool {
        self.running.iter().any(|(j, _)| j.profile_id == profile_id)
            || self.waiting.iter().any(|j| j.profile_id == profile_id)
    }

    pub fn counts(&self) -> (usize, usize) {
        (self.waiting.len(), self.running.len())
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn same_kind_with_different_args_does_not_merge() {
        let mut q = JobQueue::new();
        let a = q.enqueue_with("p1", Kind::DeleteSnapshot, "s1");
        let b = q.enqueue_with("p1", Kind::DeleteSnapshot, "s2");
        let c = q.enqueue_with("p1", Kind::DeleteSnapshot, "s1");
        assert!(matches!(a, Enqueue::Queued(_)));
        assert!(matches!(b, Enqueue::Queued(_)));
        assert!(matches!(c, Enqueue::Merged(_)));
        let (j, _) = q.start_next(4).unwrap();
        assert_eq!((j.kind, j.arg.as_str()), (Kind::DeleteSnapshot, "s1"));
    }

    #[test]
    fn duplicate_waiting_jobs_merge() {
        let mut q = JobQueue::new();
        let a = q.enqueue("p1", Kind::Backup);
        assert_eq!(
            q.enqueue("p1", Kind::Backup),
            Enqueue::Merged(match a {
                Enqueue::Queued(i) => i,
                _ => 0,
            })
        );
        assert!(matches!(q.enqueue("p1", Kind::Verify), Enqueue::Queued(_)));
        assert_eq!(q.counts(), (2, 0));
    }

    #[test]
    fn same_profile_never_runs_in_parallel_but_others_can() {
        let mut q = JobQueue::new();
        q.enqueue("p1", Kind::Backup);
        q.enqueue("p1", Kind::Verify);
        q.enqueue("p2", Kind::Backup);
        let (j1, _) = q.start_next(4).unwrap();
        assert_eq!(j1.profile_id, "p1");
        // p1 Verify 는 건너뛰고 p2 가 시작
        let (j2, _) = q.start_next(4).unwrap();
        assert_eq!(j2.profile_id, "p2");
        assert!(q.start_next(4).is_none(), "p1 이 아직 실행 중");
        assert!(q.finish(j1.id));
        let (j3, _) = q.start_next(4).unwrap();
        assert_eq!((j3.profile_id.as_str(), j3.kind), ("p1", Kind::Verify));
    }

    #[test]
    fn concurrency_limit_and_busy_flag() {
        let mut q = JobQueue::new();
        q.enqueue("a", Kind::Backup);
        q.enqueue("b", Kind::Backup);
        assert!(q.start_next(1).is_some());
        assert!(q.start_next(1).is_none());
        assert!(q.is_profile_busy("a") && q.is_profile_busy("b") && !q.is_profile_busy("c"));
    }

    #[test]
    fn cancel_waiting_removes_and_cancel_running_sets_flag() {
        let mut q = JobQueue::new();
        let Enqueue::Queued(w) = q.enqueue("a", Kind::Backup) else {
            panic!()
        };
        let Enqueue::Queued(w2) = q.enqueue("b", Kind::Backup) else {
            panic!()
        };
        let (j, flag) = q.start_next(1).unwrap();
        assert_eq!(j.id, w);
        assert!(q.cancel(w2) && q.counts() == (0, 1));
        assert!(!flag.load(Ordering::SeqCst));
        assert!(q.cancel(j.id) && flag.load(Ordering::SeqCst));
        assert!(!q.cancel(999));
    }

    #[test]
    fn same_job_can_requeue_while_running() {
        let mut q = JobQueue::new();
        q.enqueue("a", Kind::Backup);
        let (j, _) = q.start_next(2).unwrap();
        assert!(matches!(q.enqueue("a", Kind::Backup), Enqueue::Queued(_)));
        assert!(q.start_next(2).is_none());
        q.finish(j.id);
        assert!(q.start_next(2).is_some());
    }
}
