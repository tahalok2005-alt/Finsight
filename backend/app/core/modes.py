"""Processing modes and the native-text access boundary.

The central architectural claim of this system is that it understands financial
PDFs *as images*, not as glyph streams.  That claim is only meaningful if it can
be enforced and audited, so this module makes the boundary explicit in code
rather than in a comment.

Modes
-----
``VISUAL_ONLY``
    Downstream extraction may see **only** what came out of the rendered pixels:
    the page image, OCR text, OCR bounding boxes, layout regions, tables and
    charts derived from them.  Any attempt to read the PDF's native text layer
    for extraction purposes raises ``NativeTextAccessViolation``.  There is no
    silent fallback: if the visual pipeline cannot recover something, the result
    is absent or marked uncertain.

``HYBRID`` (production default)
    The same visual pipeline, plus the PDF text layer as an *independent
    corroborating channel* — it can confirm an OCR reading, supply typography,
    and recover glyphs outside the recogniser's charset.  It never replaces the
    visual reading.

Access scopes
-------------
Every native-text call site is classified, and the classification is enforced:

``INGESTION_VALIDATION``
    Class A.  Counting characters to decide whether a document has a usable text
    layer at all.  Pure metadata: nothing downstream of it branches on the
    result, and it never reaches extraction.  Permitted in every mode.

``CORROBORATION``
    Class B/C.  The auxiliary channel that confirms OCR, supplies font size and
    weight to layout classification, and recovers currency glyphs.  Because it
    genuinely does feed extraction, it is **blocked in VISUAL_ONLY**.

``EVALUATION_BASELINE``
    Class A.  The text-only baseline that exists specifically to be measured
    against the visual pipeline.  Permitted in every mode; it is measurement,
    not extraction.

Every access is recorded in a per-run ledger, so a test — or the final report —
can assert that a visual-only run performed zero corroboration accesses rather
than merely trusting that it did.
"""
from __future__ import annotations

import threading
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from enum import Enum
from typing import Iterator


class ProcessingMode(str, Enum):
    VISUAL_ONLY = "VISUAL_ONLY"
    HYBRID = "HYBRID"

    def __str__(self) -> str:  # pragma: no cover - trivial
        return self.value


class NativeTextScope(str, Enum):
    INGESTION_VALIDATION = "ingestion_validation"
    CORROBORATION = "corroboration"
    EVALUATION_BASELINE = "evaluation_baseline"

    def __str__(self) -> str:  # pragma: no cover - trivial
        return self.value


#: Scopes whose output can reach the extraction pipeline, and which must
#: therefore be refused when the run claims to be visual-only.
EXTRACTION_FEEDING_SCOPES = frozenset({NativeTextScope.CORROBORATION})


class NativeTextAccessViolation(RuntimeError):
    """Raised when extraction-feeding native text is touched in VISUAL_ONLY.

    This is deliberately a hard failure rather than a warning.  A silent
    fallback to PDF glyphs would invalidate every visual-only measurement in
    this project, so the run stops instead.
    """

    def __init__(self, caller: str, scope: NativeTextScope):
        super().__init__(
            f"VISUAL_ONLY mode forbids native PDF text access from {caller!r} "
            f"(scope={scope}). The visual pipeline must work from rendered "
            f"pixels alone; falling back to the PDF text layer here would make "
            f"the visual-only result meaningless."
        )
        self.caller = caller
        self.scope = scope


@dataclass(slots=True)
class AccessRecord:
    caller: str
    scope: NativeTextScope
    detail: str = ""


@dataclass(slots=True)
class AccessLedger:
    """Records every native-text access attempted during a run."""
    mode: ProcessingMode
    allowed: list[AccessRecord] = field(default_factory=list)
    blocked: list[AccessRecord] = field(default_factory=list)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def record(self, record: AccessRecord, *, permitted: bool) -> None:
        with self._lock:
            (self.allowed if permitted else self.blocked).append(record)

    @property
    def corroboration_accesses(self) -> int:
        return sum(1 for r in self.allowed
                   if r.scope == NativeTextScope.CORROBORATION)

    def summary(self) -> dict:
        by_scope: dict[str, int] = {}
        for r in self.allowed:
            by_scope[str(r.scope)] = by_scope.get(str(r.scope), 0) + 1
        return {
            "mode": str(self.mode),
            "allowed_total": len(self.allowed),
            "allowed_by_scope": by_scope,
            "blocked_total": len(self.blocked),
            "corroboration_accesses": self.corroboration_accesses,
            "extraction_fed_by_native_text": self.corroboration_accesses > 0,
        }


_MODE: ContextVar[ProcessingMode] = ContextVar(
    "finsight_processing_mode", default=ProcessingMode.HYBRID)
_LEDGER: ContextVar[AccessLedger | None] = ContextVar(
    "finsight_access_ledger", default=None)


def current_mode() -> ProcessingMode:
    return _MODE.get()


def is_visual_only() -> bool:
    return _MODE.get() == ProcessingMode.VISUAL_ONLY


def current_ledger() -> AccessLedger | None:
    return _LEDGER.get()


@contextmanager
def processing_mode(mode: ProcessingMode | str,
                    ledger: AccessLedger | None = None) -> Iterator[AccessLedger]:
    """Run a block under an explicit processing mode.

    The mode is stored in a context variable, so it propagates into the worker
    threads the pipeline uses for per-page analysis without being threaded
    through every function signature.
    """
    mode = ProcessingMode(mode)
    book = ledger if ledger is not None else AccessLedger(mode=mode)
    mode_token = _MODE.set(mode)
    ledger_token = _LEDGER.set(book)
    try:
        yield book
    finally:
        _MODE.reset(mode_token)
        _LEDGER.reset(ledger_token)


def guard_native_text(caller: str, scope: NativeTextScope,
                      detail: str = "") -> None:
    """Gate every native-PDF-text read.  Call this *before* touching glyphs.

    Raises ``NativeTextAccessViolation`` when an extraction-feeding scope is
    used inside a visual-only run.  Permitted accesses are still recorded, so
    the ledger proves what a run actually did.
    """
    record = AccessRecord(caller=caller, scope=scope, detail=detail)
    permitted = not (is_visual_only() and scope in EXTRACTION_FEEDING_SCOPES)
    book = _LEDGER.get()
    if book is not None:
        book.record(record, permitted=permitted)
    if not permitted:
        raise NativeTextAccessViolation(caller, scope)


def native_text_allowed(scope: NativeTextScope) -> bool:
    """Non-raising check, for callers that want to skip work rather than fail."""
    return not (is_visual_only() and scope in EXTRACTION_FEEDING_SCOPES)
