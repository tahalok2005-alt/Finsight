"""Upload limits for a public instance.

A document occupies a CPU core for seconds to minutes, and a small host has
two. Without limits, one visitor queueing ten long filings would leave every
other visitor watching a spinner. These apply only in public mode
(``FINSIGHT_WORKSPACES_ENABLED``); a laptop or a team instance is never
throttled.

Three limits, each answering a different abuse:

* **per workspace, documents in progress** -- one browser cannot fill the queue;
* **per network address, uploads per hour** -- clearing the cookie to get a
  fresh workspace does not get around the first;
* **instance-wide, documents in progress** -- past this the service says it is
  busy, honestly, rather than accepting work it will not reach for an hour.

The per-address counter lives in memory: it resets on restart, and a single
process is the only deployment shape this application supports.
"""
from __future__ import annotations

import threading
import time
from collections import defaultdict, deque

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config.settings import settings
from app.core import workspace
from app.core.errors import ServiceBusy, TooManyRequests
from app.database.models import Document
from app.models.enums import DocumentStatus

ACTIVE_STATUSES = tuple(s.value for s in DocumentStatus if s.is_active)
_HOUR = 3600.0


class SlidingWindow:
    """Events per key within the last ``window`` seconds."""

    def __init__(self, window: float = _HOUR):
        self.window = window
        self._events: dict[str, deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    def count(self, key: str, now: float | None = None) -> int:
        now = time.monotonic() if now is None else now
        with self._lock:
            q = self._events[key]
            while q and q[0] <= now - self.window:
                q.popleft()
            if not q:
                self._events.pop(key, None)
                return 0
            return len(q)

    def record(self, key: str, now: float | None = None) -> None:
        now = time.monotonic() if now is None else now
        with self._lock:
            self._events[key].append(now)

    def clear(self) -> None:
        with self._lock:
            self._events.clear()


uploads_by_address = SlidingWindow()


def _active(session: Session, *conds) -> int:
    return int(session.scalar(
        select(func.count()).select_from(Document)
        .where(Document.status.in_(ACTIVE_STATUSES), *conds)) or 0)


def check_upload_allowed(session: Session, address: str | None) -> None:
    """Refuse an upload that would break a limit; record it if not.

    Called before the file is written, so a refused upload costs no disk.
    """
    if not workspace.enabled():
        return
    key = address or "unknown"
    per_hour = settings.uploads_per_ip_per_hour
    if uploads_by_address.count(key) >= per_hour:
        raise TooManyRequests(
            f"Upload limit reached: {per_hour} documents an hour. "
            "Please try again later.", detail={"limit_per_hour": per_hour})

    mine = settings.max_active_per_workspace
    if _active(session, workspace.owns(Document)) >= mine:
        raise TooManyRequests(
            f"You already have {mine} documents processing. Wait for one to "
            "finish, then upload the next.", detail={"limit": mine})

    everyone = settings.max_active_documents
    if _active(session) >= everyone:
        raise ServiceBusy(
            "FinSight is busy reading other documents right now. "
            "Please try again in a few minutes.", detail={"limit": everyone})

    uploads_by_address.record(key)
