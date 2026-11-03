"""Document lifecycle: queue, pause, resume, cancel, delete.

These exercise the real control path -- the same :mod:`app.core.control`
objects the API drives -- rather than mocking it, because the thing worth
testing is precisely that a cooperative stop actually stops something.

Where a test needs a running pipeline it uses the synthetic PDF fixture, which
is small enough to keep the suite quick; where it only needs the state machine
it drives the control object directly, which keeps those tests instant.
"""
from __future__ import annotations

import threading
import time

import pytest
from sqlalchemy import select

from app.core.control import Control, ProcessingCancelled, registry
from app.database import repository as repo
from app.database.models import (
    Document, FinancialFactRow, Page, ProcessingRun, StageRun,
)
from app.database.session import session_scope
from app.models.enums import DocumentStatus, StageStatus


@pytest.fixture
def processed(synthetic_pdf):
    """One fully processed document, fresh for each test.

    Function-scoped on purpose: the deletion tests destroy it, and a shared
    fixture would make them order-dependent.
    """
    from app.core.ids import sha256_file
    from app.ingestion import storage
    from app.ocr.registry import get_engine
    from app.services.processor import process_document

    get_engine().warmup()
    doc_id = "lifecycle001"
    # copy into the document's own directory so deletion has files to remove,
    # exactly as an upload would leave them
    target_dir = storage.document_dir(doc_id)
    target_dir.mkdir(parents=True, exist_ok=True)
    stored = target_dir / synthetic_pdf.name
    stored.write_bytes(synthetic_pdf.read_bytes())

    with session_scope() as s:
        repo.create_document(
            s, document_id=doc_id, filename=stored.name,
            original_filename=synthetic_pdf.name, stored_path=str(stored),
            content_hash=sha256_file(stored),
            size_bytes=stored.stat().st_size)
    process_document(doc_id, trigger="test")
    yield doc_id
    registry.release(doc_id)


# --------------------------------------------------------------- the switch
class TestControlSignal:
    """The cooperative stop, on its own."""

    def test_a_checkpoint_is_free_when_nothing_is_asked_of_it(self):
        ctl = Control(document_id="d1")
        for page in range(50):
            ctl.checkpoint(page=page, total=50)      # must not block or raise
        assert not ctl.is_paused and not ctl.is_cancelled

    def test_cancel_raises_at_the_next_checkpoint(self):
        ctl = Control(document_id="d1")
        ctl.checkpoint(page=1, total=10)
        ctl.request_cancel()
        with pytest.raises(ProcessingCancelled) as err:
            ctl.checkpoint(page=2, total=10)
        assert err.value.page == 2 and err.value.total == 10

    def test_pause_blocks_until_resumed(self):
        """The worker really waits -- this is what makes pause a pause."""
        ctl = Control(document_id="d1")
        ctl.request_pause()
        released = threading.Event()

        def worker():
            ctl.checkpoint(page=7, total=20)         # blocks here
            released.set()

        t = threading.Thread(target=worker, daemon=True)
        t.start()
        assert not released.wait(0.4), "checkpoint returned while paused"
        assert ctl.is_paused
        assert ctl.paused_at == (7, 20, "")

        ctl.request_resume()
        assert released.wait(2), "checkpoint did not return after resume"
        assert not ctl.is_paused

    def test_cancelling_while_paused_wakes_the_worker_and_stops_it(self):
        """A paused run must not become unstoppable."""
        ctl = Control(document_id="d1")
        ctl.request_pause()
        outcome: list[str] = []

        def worker():
            try:
                ctl.checkpoint(page=3, total=9)
                outcome.append("returned")
            except ProcessingCancelled:
                outcome.append("cancelled")

        t = threading.Thread(target=worker, daemon=True)
        t.start()
        time.sleep(0.2)
        ctl.request_cancel()
        t.join(timeout=2)
        assert outcome == ["cancelled"]

    def test_pause_after_cancel_is_ignored(self):
        ctl = Control(document_id="d1")
        ctl.request_cancel()
        ctl.request_pause()
        with pytest.raises(ProcessingCancelled):
            ctl.checkpoint(page=1, total=1)

    def test_the_registry_hands_out_one_control_per_document(self):
        a = registry.acquire("doc-a")
        b = registry.acquire("doc-b")
        assert registry.get("doc-a") is a
        assert registry.get("doc-b") is b
        registry.release("doc-a")
        assert registry.get("doc-a") is None
        registry.release("doc-b")


# ------------------------------------------------------------- the API rules
class TestLifecycleRules:
    """Which actions the server offers, and which it refuses."""

    def test_a_finished_document_cannot_be_paused_or_cancelled(self, client, processed):
        ctrl = client.get(f"/api/v1/documents/{processed}/control").json()
        assert ctrl["can_pause"] is False
        assert ctrl["can_resume"] is False
        assert ctrl["can_cancel"] is False
        assert ctrl["can_open"] is True
        assert ctrl["can_delete"] is True

        # a state-machine violation is a conflict with the current state,
        # which a client can resolve by refreshing it
        r = client.post(f"/api/v1/documents/{processed}/pause")
        assert r.status_code == 409
        assert "processing" in r.json()["error"]["message"].lower()

    def test_resume_is_refused_unless_paused(self, client, processed):
        r = client.post(f"/api/v1/documents/{processed}/resume")
        assert r.status_code == 409
        assert "paused" in r.json()["error"]["message"].lower()

    def test_cancelling_a_finished_document_is_a_no_op(self, client, processed):
        before = client.get(f"/api/v1/documents/{processed}").json()["status"]
        r = client.post(f"/api/v1/documents/{processed}/cancel")
        assert r.status_code == 202
        after = client.get(f"/api/v1/documents/{processed}").json()["status"]
        assert after == before, "a settled document must not be disturbed"

    def test_a_document_with_no_live_run_is_marked_cancelled_honestly(
            self, client, processed):
        """A document left mid-flight by a restart can still be closed out."""
        with session_scope() as s:
            repo.set_status(s, processed, DocumentStatus.OCR_PROCESSING)
        r = client.post(f"/api/v1/documents/{processed}/cancel")
        assert r.status_code == 202
        doc = client.get(f"/api/v1/documents/{processed}").json()
        assert doc["status"] == "CANCELLED"
        assert doc["progress_note"]

    def test_control_flags_travel_with_every_list_row(self, client, processed):
        """The list renders actions from these, so they must always be there."""
        rows = client.get("/api/v1/documents?limit=50").json()["items"]
        row = next(r for r in rows if r["id"] == processed)
        for flag in ("can_pause", "can_resume", "can_cancel", "can_requeue",
                     "can_open", "can_delete"):
            assert flag in row, f"{flag} missing from the listing"


# ---------------------------------------------------------------- reconcile
class TestInterruptedRuns:
    def test_a_run_left_active_by_a_restart_becomes_interrupted(self, client, processed):
        from app.services.reconcile import reconcile_interrupted_runs

        with session_scope() as s:
            repo.set_status(s, processed, DocumentStatus.EXTRACTION)
        assert reconcile_interrupted_runs() >= 1

        doc = client.get(f"/api/v1/documents/{processed}").json()
        assert doc["status"] == "INTERRUPTED"
        assert "re-queue" in (doc["progress_note"] or "").lower()
        # and it offers the only sensible way forward
        assert doc["can_requeue"] is True
        assert doc["can_pause"] is False

    def test_orphaned_upload_directories_are_swept(self, processed):
        """A directory that outlived its document row is reclaimed.

        Deletion removes the directory inline, but a synced folder can hold a
        handle and refuse; the leftover is picked up at the next startup rather
        than accumulating unnoticed.
        """
        from app.config.settings import settings
        from app.services.reconcile import sweep_orphaned_files

        orphan = settings.upload_dir / "no-such-document-0001"
        orphan.mkdir(parents=True, exist_ok=True)
        (orphan / "leftover.pdf").write_bytes(b"%PDF-1.4 leftover")
        assert orphan.exists()

        assert sweep_orphaned_files() >= 1
        assert not orphan.exists()

    def test_the_sweep_never_touches_a_live_document(self, processed):
        from app.config.settings import settings
        from app.ingestion import storage
        from app.services.reconcile import sweep_orphaned_files

        owned = storage.document_dir(processed)
        assert owned.exists()
        sweep_orphaned_files()
        assert owned.exists(), "the sweep removed a directory a document owns"

    def test_reconciling_leaves_settled_documents_alone(self, client, processed):
        from app.services.reconcile import reconcile_interrupted_runs

        before = client.get(f"/api/v1/documents/{processed}").json()["status"]
        reconcile_interrupted_runs()
        after = client.get(f"/api/v1/documents/{processed}").json()["status"]
        assert after == before


# ----------------------------------------------------------------- deletion
class TestDeletion:
    """Deleting must leave nothing behind, in the database or on disk."""

    def _child_counts(self, document_id: str) -> dict[str, int]:
        from app.database.models import (
            OCRElementRow, RegionRow, TableCellRow, TableRow,
            ValidationResultRow,
        )
        from app.database.models_intelligence import (
            AnomalyRow, DerivedMetricRow, EntityRow, InsightRow,
        )
        models = {
            "pages": Page, "runs": ProcessingRun, "facts": FinancialFactRow,
            "tables": TableRow, "cells": TableCellRow, "regions": RegionRow,
            "ocr_elements": OCRElementRow, "validations": ValidationResultRow,
            "entities": EntityRow, "derived": DerivedMetricRow,
            "insights": InsightRow, "anomalies": AnomalyRow,
        }
        out = {}
        with session_scope() as s:
            for name, model in models.items():
                out[name] = len(list(s.scalars(
                    select(model).where(model.document_id == document_id))))
        return out

    def test_deleting_removes_every_child_row_and_the_files(
            self, client, processed):
        from app.ingestion import storage

        before = self._child_counts(processed)
        assert before["facts"] > 0 and before["pages"] > 0, \
            "fixture should have produced data to delete"
        doc_dir = storage.document_dir(processed)
        assert doc_dir.exists()

        assert client.delete(f"/api/v1/documents/{processed}").status_code == 204

        after = self._child_counts(processed)
        assert all(v == 0 for v in after.values()), f"orphans left: {after}"
        with session_scope() as s:
            assert s.get(Document, processed) is None
        assert not doc_dir.exists(), "uploaded file and renders left on disk"
        assert client.get(f"/api/v1/documents/{processed}").status_code == 404

    def test_stage_rows_go_with_their_run(self, client, processed):
        with session_scope() as s:
            runs = list(s.scalars(select(ProcessingRun).where(
                ProcessingRun.document_id == processed)))
            assert runs, "fixture should have a processing run"
            stage_count = len(list(s.scalars(select(StageRun).where(
                StageRun.document_id == processed))))
        assert stage_count > 0

        client.delete(f"/api/v1/documents/{processed}")
        with session_scope() as s:
            assert not list(s.scalars(select(StageRun).where(
                StageRun.document_id == processed)))

    def test_deletion_leaves_no_empty_directory_behind(self, client, processed):
        """Not even an empty one.

        ``shutil.rmtree(ignore_errors=True)`` removes the contents and can
        still fail to remove the directory when a handle has just been
        released, which on Windows left a directory per deleted document while
        reporting success.
        """
        from app.ingestion import storage

        doc_dir = storage.document_dir(processed)
        assert doc_dir.exists()
        assert client.delete(f"/api/v1/documents/{processed}").status_code == 204
        assert not doc_dir.exists(), "an empty directory was left behind"

    def test_removing_files_reports_whether_it_worked(self):
        """The helper answers the question rather than assuming the answer."""
        from app.ingestion import storage
        assert storage.delete_document_files("never-existed") is True

    def test_deleting_a_cancelled_document_works(self, client, processed):
        with session_scope() as s:
            repo.set_status(s, processed, DocumentStatus.CANCELLED)
        assert client.delete(f"/api/v1/documents/{processed}").status_code == 204
        with session_scope() as s:
            assert s.get(Document, processed) is None

    def test_deleting_a_paused_document_cancels_it_first(self, client, processed):
        """A paused run holds a worker; delete must stop it, not orphan it."""
        ctl = registry.acquire(processed)
        ctl.request_pause()
        with session_scope() as s:
            repo.set_status(s, processed, DocumentStatus.PAUSED)
        try:
            stopped = threading.Event()

            def worker():
                try:
                    ctl.checkpoint(page=5, total=10)
                except ProcessingCancelled:
                    pass
                finally:
                    registry.release(processed)
                    stopped.set()

            threading.Thread(target=worker, daemon=True).start()
            time.sleep(0.2)

            assert client.delete(f"/api/v1/documents/{processed}").status_code == 204
            assert stopped.wait(5), "the paused worker was never released"
            with session_scope() as s:
                assert s.get(Document, processed) is None
        finally:
            registry.release(processed)


# -------------------------------------------------------- progress reporting
class TestProgressReporting:
    def test_status_reports_stages_pages_and_a_terminal_state(
            self, client, processed):
        st = client.get(f"/api/v1/documents/{processed}/status").json()
        assert st["status"] in ("COMPLETED", "PARTIALLY_COMPLETED",
                                "REQUIRES_REVIEW")
        assert st["pages_total"] >= 1
        assert st["pages_done"] >= 1
        names = {s["stage"] for s in st["stages"]}
        assert {"validating", "rendering", "ocr", "extraction"} <= names
        assert all(s["status"] != "RUNNING" for s in st["stages"]), \
            "a finished run must not leave a stage claiming to be running"

    def test_page_counts_are_recorded_on_the_document(self, client, processed):
        doc = client.get(f"/api/v1/documents/{processed}").json()
        assert doc["pages_processed"] >= 1
        assert doc["page_count"] >= doc["pages_processed"]

    def test_timestamps_carry_a_timezone(self, client, processed):
        """Naive timestamps are read as local time by a browser, which turned a
        two-minute-old run into a five-and-a-half-hour one."""
        doc = client.get(f"/api/v1/documents/{processed}").json()
        for field in ("created_at", "processing_started_at"):
            value = doc.get(field)
            if value:
                assert value.endswith("+00:00") or value.endswith("Z"), \
                    f"{field} has no timezone: {value}"


# ------------------------------------------------------------- listing speed
class TestListingCost:
    def test_the_listing_does_not_issue_a_query_per_document(self, client):
        """The listing used to run nine COUNT(*) per row, two of them over the
        OCR element table; thirty-four documents took twenty-three seconds."""
        from sqlalchemy import event
        from app.database.session import get_engine

        seen: list[str] = []
        engine = get_engine()

        def before(conn, cursor, statement, params, context, many):
            seen.append(statement)

        event.listen(engine, "before_cursor_execute", before)
        try:
            r = client.get("/api/v1/documents?limit=50")
            assert r.status_code == 200
        finally:
            event.remove(engine, "before_cursor_execute", before)

        rows = len(r.json()["items"])
        counts = [s for s in seen if "count(" in s.lower()]
        # a handful of grouped aggregates, not one per document
        assert len(counts) <= 12, (
            f"{len(counts)} COUNT queries for {rows} documents -- "
            "the per-row aggregation is back")
