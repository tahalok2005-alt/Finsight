"""Cooperative pause and cancel for a running document pipeline.

A 165-page filing occupies a worker for the better part of an hour, and a user
who started it by mistake -- or who needs the machine back -- must be able to
stop it. Killing the thread is not an option: OCR runs inside native ONNX code
and a half-written database row helps nobody. So the pipeline *cooperates*, and
this module is the channel it cooperates through.

The contract is deliberately small. Processing calls :meth:`Control.checkpoint`
at points where stopping is safe -- between pages, and between pipeline stages
-- and the checkpoint either returns immediately, blocks while paused, or
raises :class:`ProcessingCancelled`. Nothing else in the pipeline needs to know
this module exists.

Two consequences worth stating plainly, because they shape what the product can
promise:

* **Granularity is a page.** A cancel issued mid-page finishes that page's OCR
  first. On a dense page that is a few seconds, not instant.
* **A paused run keeps its worker.** The pipeline's state stays in memory
  rather than being serialised to disk, so resuming is exact and cannot
  duplicate work -- but a paused document holds a worker slot until it is
  resumed or cancelled. That is a real cost, and the queue is sized for it.

Control state lives in this process. A server restart therefore ends any run in
flight, which the startup reconciliation turns into an honest ``INTERRUPTED``
rather than a document that claims to still be processing.
"""
from __future__ import annotations

import threading
from dataclasses import dataclass, field

from app.core.logging import get_logger

log = get_logger("core.control")


class ProcessingCancelled(Exception):
    """Raised inside the pipeline when the user cancelled this document.

    Carries how far the run had got, so the cancellation can be reported as
    "stopped at page 41 of 165" rather than merely "stopped".
    """

    def __init__(self, document_id: str, *, page: int = 0, total: int = 0,
                 stage: str = ""):
        self.document_id = document_id
        self.page = page
        self.total = total
        self.stage = stage
        where = f" at page {page} of {total}" if total else ""
        super().__init__(f"processing cancelled{where}")


@dataclass
class Control:
    """The pause/cancel switch for one document's run."""

    document_id: str
    #: cleared while paused, set while running -- a waiter blocks on it
    _resume: threading.Event = field(default_factory=threading.Event)
    _cancelled: bool = False
    _pause_requested: bool = False
    #: set once the run has actually come to rest inside a checkpoint
    _paused_at: tuple[int, int, str] | None = None
    _lock: threading.Lock = field(default_factory=threading.Lock)
    #: called with ("PAUSED" | "RESUMED" | "CANCELLED", page, total, stage)
    on_change: object = None

    def __post_init__(self) -> None:
        self._resume.set()

    # ------------------------------------------------------------- requests
    def request_pause(self) -> None:
        with self._lock:
            if self._cancelled:
                return
            self._pause_requested = True
            self._resume.clear()
        log.info("control.pause_requested", document_id=self.document_id)

    def request_resume(self) -> None:
        with self._lock:
            self._pause_requested = False
            self._paused_at = None
            self._resume.set()
        log.info("control.resume_requested", document_id=self.document_id)

    def request_cancel(self) -> None:
        with self._lock:
            self._cancelled = True
            self._pause_requested = False
            # a paused run must wake up to notice it has been cancelled
            self._resume.set()
        log.info("control.cancel_requested", document_id=self.document_id)

    # --------------------------------------------------------------- state
    @property
    def is_cancelled(self) -> bool:
        with self._lock:
            return self._cancelled

    @property
    def is_paused(self) -> bool:
        with self._lock:
            return self._paused_at is not None

    @property
    def pause_pending(self) -> bool:
        with self._lock:
            return self._pause_requested and self._paused_at is None

    @property
    def paused_at(self) -> tuple[int, int, str] | None:
        with self._lock:
            return self._paused_at

    # ---------------------------------------------------------- the contract
    def checkpoint(self, *, page: int = 0, total: int = 0,
                   stage: str = "") -> None:
        """A point at which the run may safely stop or wait.

        Returns immediately in the normal case, so the cost on the hot path is
        one uncontended lock acquisition per page.
        """
        with self._lock:
            if self._cancelled:
                raise ProcessingCancelled(self.document_id, page=page,
                                          total=total, stage=stage)
            if not self._pause_requested:
                return
            self._paused_at = (page, total, stage)
            notify = self.on_change

        log.info("control.paused", document_id=self.document_id, page=page,
                 total=total, stage=stage)
        if callable(notify):
            notify("PAUSED", page, total, stage)

        # Block here until resumed or cancelled.  The worker thread is held on
        # purpose: the pipeline's state stays in memory, so resuming continues
        # exactly where it stopped instead of replaying work.
        self._resume.wait()

        with self._lock:
            self._paused_at = None
            cancelled = self._cancelled
            notify = self.on_change
        if cancelled:
            raise ProcessingCancelled(self.document_id, page=page,
                                      total=total, stage=stage)
        log.info("control.resumed", document_id=self.document_id, page=page)
        if callable(notify):
            notify("RESUMED", page, total, stage)


class _Registry:
    """Every control currently in play, keyed by document."""

    def __init__(self) -> None:
        self._items: dict[str, Control] = {}
        self._lock = threading.Lock()

    def acquire(self, document_id: str) -> Control:
        """The control for a run that is starting, replacing any stale one."""
        with self._lock:
            ctl = Control(document_id=document_id)
            self._items[document_id] = ctl
            return ctl

    def get(self, document_id: str) -> Control | None:
        with self._lock:
            return self._items.get(document_id)

    def release(self, document_id: str) -> None:
        with self._lock:
            self._items.pop(document_id, None)

    def active_documents(self) -> list[str]:
        with self._lock:
            return list(self._items)


registry = _Registry()
