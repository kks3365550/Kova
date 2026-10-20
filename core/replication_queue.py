# -*- coding: utf-8 -*-
"""
core/replication_queue.py: 내결함성(Durable) 오프사이트 복제 큐 관리 엔진 (Sprint 1)
- SQLite WAL 모드 기반 작업 영속화: PC 급작 종료/재부팅 시에도 미완료 복제 작업 무손실 보존
- 상태 전이 머신: PENDING -> TRANSFERRING -> VERIFIED -> COMMITTED (실패 시 FAILED)
- CAS 불변성 활용 멱등성 보장: 비정상 종료 후 재개(Auto-Resume) 시 이미 전송된 블롭 스킵, 누락 블롭만 증분 전송
- 백그라운드 워커 스레드 및 자동 재시도 지원
"""

import os
import sqlite3
import threading
import time
from typing import Optional, Dict, List, Any


class ReplicationQueueManager:
    """
    SQLite WAL 모드를 활용한 Durable 오프사이트 복제 큐 관리자.
    멀티스레드 안전 커넥션 관리 및 크래시 후 자동 재개(Auto-Resume) 지원.
    """

    VALID_STATES = ('PENDING', 'TRANSFERRING', 'VERIFIED', 'COMMITTED', 'FAILED')

    def __init__(self, db_path_or_repo_dir: str):
        path = os.path.abspath(db_path_or_repo_dir)
        if os.path.isdir(path) or not path.lower().endswith(".db"):
            self.db_path = os.path.join(path, "replication_queue.db")
        else:
            self.db_path = path

        db_dir = os.path.dirname(self.db_path)
        if db_dir and not os.path.exists(db_dir):
            os.makedirs(db_dir, exist_ok=True)

        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self._worker_thread: Optional[threading.Thread] = None
        self._conn: Optional[sqlite3.Connection] = None

        self._init_db()

    def _get_connection(self) -> sqlite3.Connection:
        if self._conn is None:
            conn = sqlite3.connect(self.db_path, check_same_thread=False, timeout=30.0)
            conn.execute("PRAGMA journal_mode=WAL;")
            conn.execute("PRAGMA synchronous=NORMAL;")
            self._conn = conn
        return self._conn

    def _init_db(self):
        with self._lock:
            conn = self._get_connection()
            cursor = conn.cursor()
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS replication_queue (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    snapshot_id TEXT NOT NULL UNIQUE,
                    repo_dir TEXT NOT NULL,
                    remote_repo_dir TEXT NOT NULL,
                    state TEXT CHECK(state IN ('PENDING', 'TRANSFERRING', 'VERIFIED', 'COMMITTED', 'FAILED')) DEFAULT 'PENDING',
                    attempts INTEGER DEFAULT 0,
                    error_msg TEXT,
                    created_at REAL,
                    updated_at REAL
                )
            """)
            cursor.execute("""
                CREATE INDEX IF NOT EXISTS idx_rq_state ON replication_queue(state)
            """)
            cursor.execute("""
                CREATE INDEX IF NOT EXISTS idx_rq_snapshot_id ON replication_queue(snapshot_id)
            """)
            conn.commit()

    def enqueue(self, snapshot_id: str, repo_dir: str, remote_repo_dir: str) -> int:
        """
        PENDING 상태로 큐에 등록. 이미 존재하는 경우 id를 반환하고 미완료면 PENDING 유지.
        """
        now = time.time()
        with self._lock:
            conn = self._get_connection()
            cursor = conn.cursor()
            cursor.execute(
                "SELECT id, state FROM replication_queue WHERE snapshot_id = ?",
                (snapshot_id,)
            )
            row = cursor.fetchone()

            if row:
                existing_id, state = row
                if state == 'FAILED':
                    cursor.execute(
                        "UPDATE replication_queue SET state='PENDING', error_msg=NULL, updated_at=? WHERE id=?",
                        (now, existing_id)
                    )
                    conn.commit()
                return existing_id

            try:
                cursor.execute(
                    """
                    INSERT INTO replication_queue 
                    (snapshot_id, repo_dir, remote_repo_dir, state, attempts, error_msg, created_at, updated_at)
                    VALUES (?, ?, ?, 'PENDING', 0, NULL, ?, ?)
                    """,
                    (snapshot_id, os.path.abspath(repo_dir), os.path.abspath(remote_repo_dir), now, now)
                )
                conn.commit()
                return cursor.lastrowid
            except sqlite3.IntegrityError:
                cursor.execute("SELECT id FROM replication_queue WHERE snapshot_id = ?", (snapshot_id,))
                return cursor.fetchone()[0]

    def get_status(self, snapshot_id: str) -> Optional[Dict[str, Any]]:
        """
        스냅샷의 현재 복제 상태를 조회합니다.
        """
        with self._lock:
            conn = self._get_connection()
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT id, snapshot_id, repo_dir, remote_repo_dir, state, 
                       attempts, error_msg, created_at, updated_at
                FROM replication_queue
                WHERE snapshot_id = ?
                """,
                (snapshot_id,)
            )
            row = cursor.fetchone()
            if not row:
                return None

            return {
                "id": row[0],
                "snapshot_id": row[1],
                "repo_dir": row[2],
                "remote_repo_dir": row[3],
                "state": row[4],
                "attempts": row[5],
                "error_msg": row[6],
                "created_at": row[7],
                "updated_at": row[8],
                "is_offsite_protected": (row[4] == 'COMMITTED')
            }

    def _update_state(self, task_id: int, state: str, error_msg: Optional[str] = None):
        now = time.time()
        with self._lock:
            conn = self._get_connection()
            cursor = conn.cursor()
            if error_msg is not None:
                cursor.execute(
                    "UPDATE replication_queue SET state=?, error_msg=?, updated_at=? WHERE id=?",
                    (state, error_msg, now, task_id)
                )
            else:
                cursor.execute(
                    "UPDATE replication_queue SET state=?, error_msg=NULL, updated_at=? WHERE id=?",
                    (state, now, task_id)
                )
            conn.commit()

    def _increment_attempts(self, task_id: int):
        with self._lock:
            conn = self._get_connection()
            cursor = conn.cursor()
            cursor.execute(
                "UPDATE replication_queue SET attempts = attempts + 1 WHERE id=?",
                (task_id,)
            )
            conn.commit()

    def process_next_task(self) -> bool:
        """
        대기(PENDING) 또는 중단 복구(TRANSFERRING) 작업을 1건 처리합니다.
        반환값: 작업이 실행되었으면 True, 대기열이 비었으면 False.
        """
        task = self._fetch_next_task()
        if not task:
            return False

        task_id = task['id']
        snapshot_id = task['snapshot_id']
        repo_dir = task['repo_dir']
        remote_repo_dir = task['remote_repo_dir']

        # 1. 상태 전이: TRANSFERRING
        self._update_state(task_id, 'TRANSFERRING')

        try:
            from core.replication import ReplicationManager

            rm = ReplicationManager(repo_dir, remote_repo_dir)
            result = rm.replicate_snapshot(snapshot_id)

            if result.get("status") == "success":
                # 2. 검증 완료 -> 커밋 완료
                self._update_state(task_id, 'VERIFIED')
                self._update_state(task_id, 'COMMITTED')
                return True
            else:
                errors = "; ".join(result.get("errors", ["Replication verification failed"]))
                self._handle_failure(task_id, errors)
                return False

        except Exception as e:
            self._handle_failure(task_id, str(e))
            return False

    def _handle_failure(self, task_id: int, error_msg: str, max_retries: int = 3):
        """
        실패 처리: 지수 백오프를 고려하여 재시도 가능 여부를 원자적으로 업데이트합니다.
        """
        now = time.time()
        with self._lock:
            conn = self._get_connection()
            cursor = conn.cursor()
            cursor.execute("SELECT attempts FROM replication_queue WHERE id=?", (task_id,))
            row = cursor.fetchone()
            current_attempts = (row[0] + 1) if row else 1

            if current_attempts >= max_retries:
                cursor.execute(
                    "UPDATE replication_queue SET state='FAILED', attempts=?, error_msg=?, updated_at=? WHERE id=?",
                    (current_attempts, error_msg, now, task_id)
                )
            else:
                cursor.execute(
                    "UPDATE replication_queue SET state='PENDING', attempts=?, error_msg=?, updated_at=? WHERE id=?",
                    (current_attempts, error_msg, now, task_id)
                )
            conn.commit()

    def _fetch_next_task(self) -> Optional[Dict[str, Any]]:
        now = time.time()
        with self._lock:
            conn = self._get_connection()
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT id, snapshot_id, repo_dir, remote_repo_dir, state, attempts, updated_at
                FROM replication_queue
                WHERE state IN ('PENDING', 'TRANSFERRING')
                ORDER BY 
                    CASE WHEN state = 'PENDING' THEN 0 ELSE 1 END,
                    created_at ASC
                LIMIT 20
                """
            )
            rows = cursor.fetchall()
            if not rows:
                return None

            for row in rows:
                attempts = row[5] or 0
                updated_at = row[6] or 0
                # 재시도 이력이 있는 작업은 2^attempts 초 지수 백오프 경과 후에만 재시도
                backoff_seconds = (2 ** attempts) if attempts > 0 else 0
                if attempts > 0 and (now - updated_at) < backoff_seconds:
                    continue  # 아직 백오프 쿨다운 중인 태스크 건너뜀

                return {
                    "id": row[0],
                    "snapshot_id": row[1],
                    "repo_dir": row[2],
                    "remote_repo_dir": row[3],
                    "state": row[4]
                }
            return None

    def resume_all_interrupted(self) -> int:
        """
        PC 비정상 종료(Power-off/Crash)로 중단된 모든 TRANSFERRING 작업을 PENDING으로 리셋하여 재개합니다.
        """
        now = time.time()
        with self._lock:
            conn = self._get_connection()
            cursor = conn.cursor()
            cursor.execute(
                "UPDATE replication_queue SET state='PENDING', updated_at=? WHERE state='TRANSFERRING'",
                (now,)
            )
            conn.commit()
            return cursor.rowcount

    def start_background_worker(self, poll_interval: float = 0.5):
        """
        백그라운드 스레드로 큐를 감시하며 대기 작업을 순차 처리합니다.
        """
        if self._worker_thread and self._worker_thread.is_alive():
            return

        self._stop_event.clear()
        self._worker_thread = threading.Thread(
            target=self._worker_loop,
            args=(poll_interval,),
            daemon=True,
            name="ReplicationQueueWorker"
        )
        self._worker_thread.start()

    def stop_background_worker(self, timeout: float = 3.0):
        if self._worker_thread and self._worker_thread.is_alive():
            self._stop_event.set()
            self._worker_thread.join(timeout=timeout)

    def _worker_loop(self, poll_interval: float):
        while not self._stop_event.is_set():
            try:
                processed = self.process_next_task()
                if not processed:
                    self._stop_event.wait(timeout=poll_interval)
                else:
                    time.sleep(0.05)
            except Exception:
                self._stop_event.wait(timeout=poll_interval)

    def list_tasks(self, limit: int = 50) -> List[Dict[str, Any]]:
        with self._lock:
            conn = self._get_connection()
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT id, snapshot_id, repo_dir, remote_repo_dir, state, 
                       attempts, error_msg, created_at, updated_at
                FROM replication_queue
                ORDER BY created_at DESC
                LIMIT ?
                """,
                (limit,)
            )
            rows = cursor.fetchall()
            return [
                {
                    "id": r[0],
                    "snapshot_id": r[1],
                    "repo_dir": r[2],
                    "remote_repo_dir": r[3],
                    "state": r[4],
                    "attempts": r[5],
                    "error_msg": r[6],
                    "created_at": r[7],
                    "updated_at": r[8],
                    "is_offsite_protected": (r[4] == 'COMMITTED')
                }
                for r in rows
            ]

    def get_status_batch(self, snapshot_ids: List[str], chunk_size: int = 500) -> Dict[str, Dict[str, Any]]:
        """
        여러 스냅샷 ID의 상태를 배치로 조회합니다.
        SQLite의 바인딩 변수 한계(999개)를 방어하기 위해 chunk_size 단위로 분할 쿼리합니다.
        """
        if not snapshot_ids:
            return {}

        result = {}
        # 중복 제거 및 입력 순서 유지
        unique_ids = list(dict.fromkeys(snapshot_ids))

        for i in range(0, len(unique_ids), chunk_size):
            chunk = unique_ids[i:i + chunk_size]
            placeholders = ','.join(['?' for _ in chunk])
            query = f"""
                SELECT id, snapshot_id, repo_dir, remote_repo_dir, state, 
                       attempts, error_msg, created_at, updated_at
                FROM replication_queue
                WHERE snapshot_id IN ({placeholders})
            """
            with self._lock:
                conn = self._get_connection()
                cursor = conn.cursor()
                cursor.execute(query, chunk)
                rows = cursor.fetchall()

            for r in rows:
                sid = r[1]
                result[sid] = {
                    "id": r[0],
                    "snapshot_id": r[1],
                    "repo_dir": r[2],
                    "remote_repo_dir": r[3],
                    "state": r[4],
                    "attempts": r[5],
                    "error_msg": r[6],
                    "created_at": r[7],
                    "updated_at": r[8],
                    "is_offsite_protected": (r[4] == 'COMMITTED')
                }

        return result

    def close(self):
        self.stop_background_worker()
        with self._lock:
            if self._conn:
                try:
                    try:
                        self._conn.execute("PRAGMA wal_checkpoint(TRUNCATE);")
                    except Exception:
                        pass
                finally:
                    try:
                        self._conn.close()
                    except Exception:
                        pass
                self._conn = None

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()
