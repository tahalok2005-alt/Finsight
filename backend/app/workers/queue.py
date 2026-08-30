"""Background job execution.

Processing a 34-page document takes minutes, so it must never run inside an
HTTP request.  Jobs are recorded in the database (so their state survives a
restart and is queryable) and executed by a small in-process thread pool.

Why not Celery/RQ: both need a broker this deployment does not have, and the
workload is a handful of long CPU-bound jobs rather than high-throughput
messaging.  The queue is deliberately a thin, replaceable seam --
``submit``/``get_state`` is all the API depends on, so swapping in a distributed
broker later touches only this module.
"""
from __future__ import annotations

import threading
from concurrent.futures import Future, ThreadPoolExecutor
from typing import Callable

from app.config.settings import settings
from app.core.logging import get_logger
from app.database import repository as repo
from app.database.session import session_scope
from app.models.enums import DocumentStatus, JobState

log = get_logger("workers.queue")


class JobQueue:
    def __init__(self, max_workers: int | None = None):
        self.max_workers = max_workers or settings.worker_threads
        self._pool: ThreadPoolExecutor | None = None
        self._futures: dict[str, Future] = {}
        self._lock = threading.Lock()

    # ------------------------------------------------------------- lifecycle
    def start(self) -> None:
        with self._lock:
            if self._pool is None:
                self._pool = ThreadPoolExecutor(
                    max_workers=self.max_workers, thread_name_prefix="finsight-job")
                log.info("queue.started", workers=self.max_workers)

    def shutdown(self, wait: bool = False) -> None:
        with self._lock:
            if self._pool is not None:
                self._pool.shutdown(wait=wait, cancel_futures=not wait)
                self._pool = None
                log.info("queue.stopped")

    # ---------------------------------------------------------------- submit
    def submit(self, document_id: str, *, job_type: str = "process",
               force_ocr: bool = False) -> str:
        """Queue a processing job and return its id."""
        self.start()
        with session_scope() as session:
            job = repo.enqueue_job(session, document_id, job_type=job_type,
                                   payload={"force_ocr": force_ocr})
            job_id = job.id
            repo.set_status(session, document_id, DocumentStatus.QUEUED)

        assert self._pool is not None
        future = self._pool.submit(self._run, job_id, document_id, job_type,
                                   force_ocr)
        with self._lock:
            self._futures[job_id] = future
        log.info("queue.submitted", job_id=job_id, document_id=document_id,
                 job_type=job_type)
        return job_id

    # ------------------------------------------------------------- execution
    def _run(self, job_id: str, document_id: str, job_type: str,
             force_ocr: bool) -> None:
        from app.services.processor import process_document  # local: avoids cycle

        with session_scope() as session:
            repo.claim_job(session, job_id)
        try:
            if job_type in ("process", "reprocess"):
                process_document(document_id, trigger=job_type,
                                 force_ocr=force_ocr)
            else:
                raise ValueError(f"Unknown job type {job_type!r}")
            with session_scope() as session:
                repo.finish_job(session, job_id, state=JobState.SUCCEEDED.value)
        except Exception as exc:  # noqa: BLE001 - recorded, never swallowed
            log.exception("queue.job_failed", job_id=job_id,
                          document_id=document_id, error=str(exc))
            message = f"{type(exc).__name__}: {exc}"
            with session_scope() as session:
                repo.finish_job(session, job_id, state=JobState.FAILED.value,
                                error=message)
                # the document must reflect the failure, not stay mid-pipeline
                repo.set_status(session, document_id, DocumentStatus.FAILED,
                                error=message)
        finally:
            with self._lock:
                self._futures.pop(job_id, None)

    # ----------------------------------------------------------------- state
    def is_running(self, job_id: str) -> bool:
        with self._lock:
            fut = self._futures.get(job_id)
        return fut is not None and not fut.done()

    @property
    def active_count(self) -> int:
        with self._lock:
            return sum(1 for f in self._futures.values() if not f.done())

    def wait(self, job_id: str, timeout: float | None = None) -> bool:
        """Block until a job finishes.  Used by tests and the CLI, not the API."""
        with self._lock:
            fut = self._futures.get(job_id)
        if fut is None:
            return True
        try:
            fut.result(timeout=timeout)
            return True
        except Exception:  # noqa: BLE001 - failure state is on the job row
            return True


queue = JobQueue()
