"""System, health and dashboard-overview endpoints."""
from __future__ import annotations

import platform
import sys
from typing import Any

from fastapi import APIRouter
from sqlalchemy import func, select

from app.api.deps import DbSession
from app.config.settings import settings
from app.database.models import (
    ChartRow, CommentaryRow, Document, FinancialFactRow, GuidanceRow, JobRow,
    TableRow, ValidationResultRow,
)
from app.models.enums import DocumentStatus
from app.ocr.registry import probe_engines
from app.workers.queue import queue

router = APIRouter(tags=["system"])


@router.get("/health")
def health(session: DbSession) -> dict:
    """Liveness plus a real dependency check."""
    db_ok, db_error = True, None
    try:
        session.execute(select(1))
    except Exception as exc:  # noqa: BLE001
        db_ok, db_error = False, str(exc)
    return {
        "status": "ok" if db_ok else "degraded",
        "app": settings.app_name, "version": settings.app_version,
        "environment": settings.environment,
        "database": {"ok": db_ok, "error": db_error},
        "workers": {"active": queue.active_count, "max": queue.max_workers},
    }


@router.get("/system")
def system_info() -> dict[str, Any]:
    """Engines and configuration actually in force -- useful for reproducibility."""
    return {
        "app": {"name": settings.app_name, "version": settings.app_version,
                "environment": settings.environment},
        "runtime": {"python": sys.version.split()[0],
                    "platform": platform.platform(),
                    "processor": platform.processor()},
        "ocr": {"configured": settings.ocr_engine,
                "engines": probe_engines(),
                "signature": settings.ocr_signature},
        "rendering": {"dpi": settings.render_dpi,
                      "format": settings.render_format,
                      "signature": settings.render_signature},
        "layout": {"engine": settings.layout_engine},
        "llm": {"enabled": settings.llm_enabled, "provider": settings.llm_provider,
                "model": settings.llm_model if settings.llm_enabled else None,
                "note": ("Optional. The pipeline extracts, normalises and "
                         "validates every figure without any LLM; a local model "
                         "is only ever used to summarise narrative text.")},
        "tolerances": {"growth_pct": settings.growth_tolerance_pct,
                       "margin_pct": settings.margin_tolerance_pct,
                       "total_rel": settings.total_tolerance_rel,
                       "ratio_rel": settings.ratio_tolerance_rel},
        "confidence": {"high": settings.confidence_high,
                       "medium": settings.confidence_medium,
                       "review_threshold": settings.review_threshold},
        "limits": {"max_upload_mb": settings.max_upload_mb,
                   "max_pages": settings.max_pages},
    }


@router.get("/overview")
def overview(session: DbSession) -> dict:
    """Dashboard summary across every document."""
    def count(model, *conds) -> int:
        return int(session.scalar(
            select(func.count()).select_from(model).where(*conds)) or 0)

    by_status = {r[0]: r[1] for r in session.execute(
        select(Document.status, func.count()).group_by(Document.status)).all()}

    # Both spellings of the file name. The client types this list as the same
    # document shape it gets everywhere else, which calls the field
    # original_filename; sending only "filename" left every unnamed document on
    # the overview with a blank row where its name should be.
    recent = [{
        "id": d.id, "filename": d.original_filename,
        "original_filename": d.original_filename, "title": d.title,
        "company_name": d.company_name, "report_period": d.report_period,
        "status": d.status, "page_count": d.page_count,
        "created_at": d.created_at, "processing_ms": d.processing_ms,
        "error": d.error,
    } for d in session.scalars(
        select(Document).order_by(Document.created_at.desc()).limit(10))]

    validation_summary = {r[0]: r[1] for r in session.execute(
        select(ValidationResultRow.status, func.count())
        .group_by(ValidationResultRow.status)).all()}

    band_summary = {r[0]: r[1] for r in session.execute(
        select(FinancialFactRow.confidence_band, func.count())
        .group_by(FinancialFactRow.confidence_band)).all()}

    top_metrics = [{"metric": r[0], "count": r[1]} for r in session.execute(
        select(FinancialFactRow.canonical_metric, func.count())
        .group_by(FinancialFactRow.canonical_metric)
        .order_by(func.count().desc()).limit(12)).all()]

    review_queue = count(FinancialFactRow,
                         FinancialFactRow.requires_review.is_(True),
                         FinancialFactRow.review_state == "pending")

    terminal = {DocumentStatus.COMPLETED.value,
                DocumentStatus.PARTIALLY_COMPLETED.value,
                DocumentStatus.REQUIRES_REVIEW.value,
                DocumentStatus.FAILED.value}
    processing = sum(n for s, n in by_status.items() if s not in terminal)

    return {
        "documents": {
            "total": count(Document), "by_status": by_status,
            "processing": processing,
            "failed": by_status.get(DocumentStatus.FAILED.value, 0),
        },
        "extraction": {
            "facts": count(FinancialFactRow), "tables": count(TableRow),
            "charts": count(ChartRow), "commentary": count(CommentaryRow),
            "guidance": count(GuidanceRow),
            "confidence_bands": band_summary, "top_metrics": top_metrics,
        },
        "validation": {
            "summary": validation_summary,
            "warnings": (validation_summary.get("WARNING", 0)
                         + validation_summary.get("REQUIRES_REVIEW", 0)),
            "failures": validation_summary.get("FAILED", 0),
            "passed": validation_summary.get("PASSED", 0),
        },
        "review_queue": review_queue,
        "recent_documents": recent,
        "jobs": {
            "queued": count(JobRow, JobRow.state == "queued"),
            "running": count(JobRow, JobRow.state == "running"),
            "failed": count(JobRow, JobRow.state == "failed"),
            "active_workers": queue.active_count,
        },
    }
