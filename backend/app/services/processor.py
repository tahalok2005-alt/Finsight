"""Bridge between the pipeline and the database.

Owns the transactional story for one processing run: create the run, stream
stage records as they complete so ``GET /status`` reflects live progress, then
persist the results atomically and set an honest final status.
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from app.config.settings import settings
from app.core.control import ProcessingCancelled, registry
from app.core.logging import get_logger
from app.database import repository as repo
from app.database.session import session_scope
from app.core.modes import ProcessingMode
from app.models.enums import DocumentStatus, StageStatus
from app.models.schemas import StageRecord
from app.ocr.registry import get_engine, probe_engines
from app.services.pipeline import Pipeline, PipelineResult

log = get_logger("services.processor")


def _engine_versions() -> dict:
    try:
        info = get_engine().info
        return {"ocr": {"name": info.name, "version": info.version},
                "layout": {"name": "heuristic-v1"},
                "available": probe_engines()}
    except Exception as exc:  # noqa: BLE001 - diagnostics must not break a run
        return {"error": str(exc)}


def _settings_snapshot() -> dict:
    return {
        "render_dpi": settings.render_dpi,
        "render_format": settings.render_format,
        "ocr_engine": settings.ocr_engine,
        "ocr_signature": settings.ocr_signature,
        "layout_engine": settings.layout_engine,
        "metric_match_threshold": settings.metric_match_threshold,
        "growth_tolerance_pct": settings.growth_tolerance_pct,
        "margin_tolerance_pct": settings.margin_tolerance_pct,
        "total_tolerance_rel": settings.total_tolerance_rel,
        "review_threshold": settings.review_threshold,
        "app_version": settings.app_version,
    }


STAGE_TO_STATUS = {
    "validating": DocumentStatus.VALIDATING,
    "rendering": DocumentStatus.RENDERING,
    "ocr": DocumentStatus.OCR_PROCESSING,
    "layout": DocumentStatus.LAYOUT_ANALYSIS,
    "document_assembly": DocumentStatus.LAYOUT_ANALYSIS,
    "tables": DocumentStatus.TABLE_ANALYSIS,
    "charts": DocumentStatus.TABLE_ANALYSIS,
    "extraction": DocumentStatus.EXTRACTION,
    "validating_facts": DocumentStatus.VALIDATING_FACTS,
    "analysis": DocumentStatus.ANALYSING,
}


def process_document(document_id: str, *, trigger: str = "upload",
                     force_ocr: bool = False,
                     mode: ProcessingMode | str = ProcessingMode.HYBRID,
                     ) -> PipelineResult:
    """Run the pipeline for one document and persist everything it produced."""
    with session_scope() as session:
        doc = repo.get_document(session, document_id)
        if doc is None:
            raise ValueError(f"Unknown document {document_id}")
        pdf_path = Path(doc.stored_path)
        content_hash = doc.content_hash
        run = repo.start_run(session, document_id, trigger=trigger,
                             settings_snapshot=_settings_snapshot(),
                             engine_versions=_engine_versions())
        run_id = run.id
        doc.processing_started_at = datetime.now(timezone.utc)
        doc.status = DocumentStatus.QUEUED.value
        doc.error = None
        # a note from the previous run ("Interrupted when the server stopped")
        # describes history, not this run, and must not follow it forward
        doc.progress_note = None

    def on_progress(stage: str, status: StageStatus, done: int, total: int,
                    detail: dict) -> None:
        """Persist stage progress immediately so status polling is truthful."""
        try:
            with session_scope() as s:
                repo.upsert_stage(s, run_id, document_id, StageRecord(
                    stage=stage, status=status, items_total=total,
                    items_done=done, detail=detail))
                mapped = STAGE_TO_STATUS.get(stage)
                if mapped is not None and status == StageStatus.RUNNING:
                    repo.set_status(s, document_id, mapped)
                # Keep the document's own page counter current while the run is
                # in flight.  The listing reads this column, and without it a
                # document that is 40% through shows 0% until it finishes.
                if stage == "ocr" and total:
                    repo.set_page_progress(s, document_id, done, total)
        except Exception as exc:  # noqa: BLE001 - progress is best-effort
            log.warning("processor.progress_write_failed", stage=stage,
                        document_id=document_id, error=str(exc))

    control = registry.acquire(document_id)

    def on_control(event: str, page: int, total: int, stage: str) -> None:
        """Reflect a pause or resume in the database as it happens.

        The user is watching a progress view; a pause that is not visible
        within a poll interval reads as a hung application.
        """
        try:
            with session_scope() as s:
                if event == "PAUSED":
                    repo.set_status(s, document_id, DocumentStatus.PAUSED)
                    repo.set_progress_note(
                        s, document_id,
                        f"Paused at page {page} of {total}" if total
                        else f"Paused before {stage or 'the next stage'}")
                elif event == "RESUMED":
                    repo.set_progress_note(s, document_id, None)
                    # Put the document back into an active state at once.
                    # Waiting for the next stage write left the list and the
                    # buttons claiming PAUSED for seconds after the run had
                    # already carried on, which reads as a control that did
                    # not work.
                    repo.set_status(s, document_id,
                                    DocumentStatus.OCR_PROCESSING)
        except Exception as exc:  # noqa: BLE001 - never break the run over a note
            log.warning("processor.control_write_failed", event=event,
                        document_id=document_id, error=str(exc))

    control.on_change = on_control

    pipeline = Pipeline(document_id, pdf_path, progress=on_progress,
                        force_ocr=force_ocr, content_hash=content_hash,
                        control=control, mode=mode)
    try:
        result = pipeline.run()
    except ProcessingCancelled as stop:
        # Policy: a cancelled run persists no extraction.  Nothing has been
        # written yet at this point -- persistence happens once, after the
        # pipeline returns -- so the document is left exactly as it was before
        # the run, with no half-built result masquerading as a whole one.  The
        # rendered pages and OCR cache on disk are content-hash keyed and are
        # deliberately kept, so re-queueing the document is fast rather than
        # starting from nothing.
        _finish_cancelled(document_id, run_id, stop)
        raise
    finally:
        registry.release(document_id)

    persist_result(document_id, result, run_id=run_id)
    _log_outcome(document_id, result)
    return result


def _finish_cancelled(document_id: str, run_id: str | None,
                      stop: ProcessingCancelled) -> None:
    """Bring a cancelled document to a consistent, honest resting state."""
    where = (f"Cancelled at page {stop.page} of {stop.total}"
             if stop.total else "Cancelled before processing completed")
    with session_scope() as session:
        repo.set_status(session, document_id, DocumentStatus.CANCELLED,
                        error=None)
        repo.set_progress_note(session, document_id, where)
        if run_id is not None:
            repo.finish_run(session, run_id, status="CANCELLED", error=where)
            # stages left mid-flight would otherwise keep claiming RUNNING
            repo.mark_running_stages(session, run_id, StageStatus.CANCELLED)
    log.info("processor.cancelled", document_id=document_id, page=stop.page,
             total=stop.total, stage=stop.stage)


def persist_result(document_id: str, result: PipelineResult, *,
                   run_id: str | None = None) -> None:
    """Write everything one pipeline run produced.

    Split out from :func:`process_document` so the write path can be exercised
    on its own -- a test can run the pipeline once against a real document and
    then replay the persistence into a fresh database, instead of reprocessing
    a 34-page PDF for every assertion.
    """
    with session_scope() as session:
        # A re-run replaces derived rows wholesale; review history is untouched.
        repo.clear_derived(session, document_id)

        repo.save_pages(session, result.pages)
        repo.save_ocr_elements(session, result.elements)
        repo.save_regions(session, result.regions)

        # Tables record the entity of the section that governs them, taken from
        # the facts already attributed to each table.
        entity_by_table = {f.provenance.table_id: f.entity
                           for f in result.facts
                           if f.provenance and f.provenance.table_id and f.entity}
        entity_by_region = {t.region_id: entity_by_table[t.id]
                            for t in result.tables
                            if t.region_id and t.id in entity_by_table}
        # Entities are saved before facts so a fact's entity_id always
        # resolves to a row that exists.
        repo.save_entities(session, document_id, result.entities)
        repo.save_tables(session, result.tables, entity_by_region)
        repo.save_table_semantics(session, result.table_semantics)
        repo.save_charts(session, result.charts)
        repo.save_facts(session, result.facts)
        repo.save_commentary(session, result.commentary)
        repo.save_guidance(session, result.guidance)
        repo.save_validations(session, result.validations)

        # Intelligence layer.  Relations are filtered to endpoints that
        # survived, so a link can never point at a fact that was not stored.
        fact_ids = {f.fact_id for f in result.facts}
        derived_ids = {m.id for m in result.derived_metrics}
        known = fact_ids | derived_ids
        repo.save_derived_metrics(session, result.derived_metrics)
        repo.save_relations(session, [
            r for r in result.relations
            if r.from_fact_id in known and r.to_fact_id in known])
        repo.save_risks(session, result.risks)
        repo.save_insights(session, result.insights)
        repo.save_anomalies(session, result.anomalies)

        if run_id is not None:
            for record in result.stages:
                repo.upsert_stage(session, run_id, document_id, record)

        repo.update_document_metadata(
            session, document_id, probe=result.probe, metadata=result.metadata,
            counts={"pages_processed": result.pages_done,
                    "pages_failed": result.pages_failed},
            processing_ms=result.duration_ms)
        repo.set_status(session, document_id, result.status, error=result.error)
        if run_id is not None:
            repo.finish_run(
                session, run_id,
                status=("SUCCEEDED" if result.status != DocumentStatus.FAILED
                        else "FAILED"),
                duration_ms=result.duration_ms, error=result.error)

            # record how this run was produced, so a stored result can always
            # be traced to the mode that produced it
            from app.database.models import ProcessingRun  # noqa: PLC0415
            run_row = session.get(ProcessingRun, run_id)
            if run_row is not None:
                run_row.settings_snapshot = {
                    **(run_row.settings_snapshot or {}),
                    "processing_mode": result.mode,
                    "native_text_audit": result.native_text_audit,
                }

        doc = repo.get_document(session, document_id)
        if doc is not None and result.metadata.get("sections"):
            doc.pdf_metadata = {**(doc.pdf_metadata or {}),
                                "sections": result.metadata["sections"][:200]}

    _log_outcome(document_id, result)


def _log_outcome(document_id: str, result: PipelineResult) -> None:
    log.info("processor.persisted", document_id=document_id,
             status=str(result.status), facts=len(result.facts),
             tables=len(result.tables), pages=len(result.pages),
             mode=result.mode,
             native_text_for_extraction=result.native_text_audit.get(
                 "extraction_fed_by_native_text"))
