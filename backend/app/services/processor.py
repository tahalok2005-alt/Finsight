"""Bridge between the pipeline and the database.

Owns the transactional story for one processing run: create the run, stream
stage records as they complete so ``GET /status`` reflects live progress, then
persist the results atomically and set an honest final status.
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from app.config.settings import settings
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
        except Exception as exc:  # noqa: BLE001 - progress is best-effort
            log.warning("processor.progress_write_failed", stage=stage,
                        document_id=document_id, error=str(exc))

    pipeline = Pipeline(document_id, pdf_path, progress=on_progress,
                        force_ocr=force_ocr, content_hash=content_hash,
                        mode=mode)
    result = pipeline.run()

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
        repo.save_tables(session, result.tables, entity_by_region)
        repo.save_charts(session, result.charts)
        repo.save_facts(session, result.facts)
        repo.save_commentary(session, result.commentary)
        repo.save_guidance(session, result.guidance)
        repo.save_validations(session, result.validations)

        for record in result.stages:
            repo.upsert_stage(session, run_id, document_id, record)

        repo.update_document_metadata(
            session, document_id, probe=result.probe, metadata=result.metadata,
            counts={"pages_processed": result.pages_done,
                    "pages_failed": result.pages_failed},
            processing_ms=result.duration_ms)
        repo.set_status(session, document_id, result.status, error=result.error)
        repo.finish_run(session, run_id,
                        status=("SUCCEEDED" if result.status != DocumentStatus.FAILED
                                else "FAILED"),
                        duration_ms=result.duration_ms, error=result.error)

        # record how this run was produced, so a stored result can always be
        # traced to the mode that produced it
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

    log.info("processor.persisted", document_id=document_id,
             status=str(result.status), facts=len(result.facts),
             tables=len(result.tables), pages=len(result.pages),
             mode=result.mode,
             native_text_for_extraction=result.native_text_audit.get(
                 "extraction_fed_by_native_text"))
    return result
