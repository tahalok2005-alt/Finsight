"""Export extracted data as JSON, CSV or Excel-compatible XLSX.

Every export carries the full evidence chain -- raw value, unit, currency,
period, growth basis, confidence, validation status and the page/region/cell the
figure came from -- so a downstream consumer can audit any number without
returning to this system.
"""
from __future__ import annotations

import csv
import io
import json
from datetime import datetime, timezone
from typing import Any, Iterable, Sequence

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config.settings import settings
from app.database.models import (
    ChartRow, CommentaryRow, Document, FinancialFactRow, GuidanceRow,
    ProvenanceRow, TableCellRow, TableRow, ValidationResultRow,
)
from app.core.logging import get_logger

log = get_logger("export")

FACT_COLUMNS: list[tuple[str, str]] = [
    ("fact_id", "id"),
    ("metric", "metric"),
    ("canonical_metric", "canonical_metric"),
    ("metric_display", "metric_display"),
    ("entity", "entity"),
    ("kind", "kind"),
    ("nature", "nature"),
    ("value", "value"),
    ("raw_value", "raw_value"),
    ("value_type", "value_type"),
    ("unit", "unit"),
    ("currency", "currency"),
    ("base_value", "base_value"),
    ("period", "period"),
    ("period_type", "period_type"),
    ("fiscal_year", "fiscal_year"),
    ("quarter", "quarter"),
    ("period_raw", "period_raw"),
    ("comparison_period", "comparison_period"),
    ("comparison_value", "comparison_value"),
    ("growth", "growth"),
    ("growth_type", "growth_type"),
    ("growth_reported", "growth_reported"),
    ("confidence", "confidence"),
    ("confidence_band", "confidence_band"),
    ("validation_status", "validation_status"),
    ("requires_review", "requires_review"),
    ("review_state", "review_state"),
    ("corrected_value", "corrected_value"),
    ("source_page", "source_page"),
    ("region_id", "region_id"),
    ("table_id", "table_id"),
    ("cell_id", "cell_id"),
    ("original_text", "original_text"),
]


def _fact_rows(session: Session, document_id: str) -> list[dict[str, Any]]:
    facts = list(session.scalars(
        select(FinancialFactRow)
        .where(FinancialFactRow.document_id == document_id)
        .order_by(FinancialFactRow.source_page, FinancialFactRow.canonical_metric)))
    prov = {p.fact_id: p for p in session.scalars(
        select(ProvenanceRow).where(ProvenanceRow.document_id == document_id))}
    out: list[dict[str, Any]] = []
    for f in facts:
        row = {name: getattr(f, attr) for name, attr in FACT_COLUMNS}
        p = prov.get(f.id)
        row["bbox_x"] = p.x if p else None
        row["bbox_y"] = p.y if p else None
        row["bbox_width"] = p.width if p else None
        row["bbox_height"] = p.height if p else None
        row["extraction_method"] = p.extraction_method if p else None
        row["source_url"] = (
            f"{settings.api_prefix}/documents/{document_id}"
            f"/pages/{f.source_page}/image")
        out.append(row)
    return out


def export_json(session: Session, document_id: str, *,
                include: Sequence[str] | None = None) -> bytes:
    """Full structured export, mirroring the API's object model."""
    doc = session.get(Document, document_id)
    if doc is None:
        raise ValueError(f"Unknown document {document_id}")
    want = set(include or ["facts", "tables", "charts", "commentary",
                           "guidance", "validation"])

    payload: dict[str, Any] = {
        "export_version": "1.0",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "generator": f"{settings.app_name} {settings.app_version}",
        "document": {
            "id": doc.id, "filename": doc.original_filename,
            "content_hash": doc.content_hash, "page_count": doc.page_count,
            "status": doc.status, "title": doc.title,
            "company_name": doc.company_name, "report_period": doc.report_period,
            "report_type": doc.report_type, "currency": doc.document_currency,
            "unit": doc.document_unit, "is_scanned": doc.is_scanned,
            "processing_ms": doc.processing_ms,
            "pages_processed": doc.pages_processed,
            "pages_failed": doc.pages_failed,
            "settings": doc.settings_snapshot,
        },
    }

    if "facts" in want:
        payload["facts"] = _fact_rows(session, document_id)

    if "tables" in want:
        tables = list(session.scalars(
            select(TableRow).where(TableRow.document_id == document_id)
            .order_by(TableRow.page_number)))
        cells_by_table: dict[str, list[TableCellRow]] = {}
        for c in session.scalars(select(TableCellRow).where(
                TableCellRow.document_id == document_id)):
            cells_by_table.setdefault(c.table_id, []).append(c)
        payload["tables"] = [{
            "id": t.id, "page": t.page_number, "title": t.title,
            "n_rows": t.n_rows, "n_cols": t.n_cols, "confidence": t.confidence,
            "unit_hint": t.unit_hint, "currency_hint": t.currency_hint,
            "entity": t.entity_hint, "detector": t.detector,
            "continued_from": t.continued_from_table_id,
            "columns": t.columns, "warnings": t.warnings,
            "bbox": {"x": t.x, "y": t.y, "width": t.width, "height": t.height},
            "cells": [{
                "id": c.id, "row": c.row, "col": c.col, "text": c.text,
                "role": c.role, "numeric_value": c.numeric_value,
                "is_numeric": c.is_numeric, "confidence": c.confidence,
                "bbox": {"x": c.x, "y": c.y, "width": c.width, "height": c.height},
            } for c in sorted(cells_by_table.get(t.id, []),
                              key=lambda z: (z.row, z.col))],
        } for t in tables]

    if "charts" in want:
        payload["charts"] = [{
            "id": c.id, "page": c.page_number, "chart_type": c.chart_type,
            "type_confidence": c.type_confidence, "title": c.title,
            "axis_labels": c.axis_labels, "legend_labels": c.legend_labels,
            "categories": c.categories, "data_labels": c.data_labels,
            "values_extracted": c.values_extracted,
            "requires_review": c.requires_review, "notes": c.notes,
            "bbox": {"x": c.x, "y": c.y, "width": c.width, "height": c.height},
        } for c in session.scalars(select(ChartRow).where(
            ChartRow.document_id == document_id))]

    if "commentary" in want:
        payload["commentary"] = [{
            "id": c.id, "page": c.page_number, "text": c.text,
            "primary_topic": c.primary_topic, "topics": c.topics,
            "sentiment": c.sentiment, "speaker": c.speaker,
            "is_quote": c.is_quote, "confidence": c.confidence,
            "mentioned_metrics": c.mentioned_metrics, "summary": c.summary,
        } for c in session.scalars(select(CommentaryRow).where(
            CommentaryRow.document_id == document_id))]

    if "guidance" in want:
        payload["guidance"] = [{
            "id": g.id, "page": g.page_number, "text": g.text,
            "nature": g.nature, "canonical_metric": g.canonical_metric,
            "metric": g.metric, "target_value": g.target_value,
            "target_unit": g.target_unit, "target_currency": g.target_currency,
            "target_period": g.target_period, "direction": g.direction,
            "trigger_phrase": g.trigger_phrase, "confidence": g.confidence,
            "requires_review": g.requires_review,
        } for g in session.scalars(select(GuidanceRow).where(
            GuidanceRow.document_id == document_id))]

    if "validation" in want:
        payload["validation"] = [{
            "id": v.id, "rule": v.rule, "rule_family": v.rule_family,
            "status": v.status, "severity": v.severity, "message": v.message,
            "reported_value": v.reported_value,
            "calculated_value": v.calculated_value,
            "difference": v.difference, "tolerance": v.tolerance,
            "fact_ids": v.fact_ids, "pages": v.page_numbers, "detail": v.detail,
        } for v in session.scalars(select(ValidationResultRow).where(
            ValidationResultRow.document_id == document_id))]

    return json.dumps(payload, indent=2, default=str).encode("utf-8")


def export_csv(session: Session, document_id: str) -> bytes:
    rows = _fact_rows(session, document_id)
    buf = io.StringIO(newline="")
    fieldnames = ([name for name, _ in FACT_COLUMNS]
                  + ["bbox_x", "bbox_y", "bbox_width", "bbox_height",
                     "extraction_method", "source_url"])
    writer = csv.DictWriter(buf, fieldnames=fieldnames, extrasaction="ignore")
    writer.writeheader()
    for r in rows:
        writer.writerow(r)
    # BOM so Excel opens UTF-8 correctly on Windows
    return b"\xef\xbb\xbf" + buf.getvalue().encode("utf-8")


def export_xlsx(session: Session, document_id: str) -> bytes:
    """Multi-sheet workbook: facts, tables, validation, commentary, guidance."""
    from openpyxl import Workbook  # noqa: PLC0415 - optional-ish, heavy import
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import get_column_letter

    wb = Workbook()
    header_font = Font(bold=True, color="FFFFFF")
    header_fill = PatternFill("solid", fgColor="1F3A5F")

    def write_sheet(title: str, headers: list[str],
                    rows: Iterable[Sequence[Any]], first: bool = False):
        ws = wb.active if first else wb.create_sheet()
        ws.title = title[:31]
        ws.append(headers)
        for cell in ws[1]:
            cell.font = header_font
            cell.fill = header_fill
            cell.alignment = Alignment(vertical="center")
        for row in rows:
            ws.append(list(row))
        ws.freeze_panes = "A2"
        for i, h in enumerate(headers, start=1):
            width = max(10, min(46, len(str(h)) + 4))
            ws.column_dimensions[get_column_letter(i)].width = width
        if ws.max_row > 1:
            ws.auto_filter.ref = f"A1:{get_column_letter(len(headers))}{ws.max_row}"
        return ws

    facts = _fact_rows(session, document_id)
    fact_headers = ([name for name, _ in FACT_COLUMNS]
                    + ["bbox_x", "bbox_y", "bbox_width", "bbox_height",
                       "extraction_method", "source_url"])
    write_sheet("Financial Facts", fact_headers,
                ([r.get(h) for h in fact_headers] for r in facts), first=True)

    validations = list(session.scalars(select(ValidationResultRow).where(
        ValidationResultRow.document_id == document_id)))
    write_sheet("Validation", ["rule_family", "rule", "status", "severity",
                               "reported_value", "calculated_value",
                               "difference", "tolerance", "pages", "message"],
                ((v.rule_family, v.rule, v.status, v.severity, v.reported_value,
                  v.calculated_value, v.difference, v.tolerance,
                  ", ".join(str(p) for p in (v.page_numbers or [])), v.message)
                 for v in validations))

    cells = list(session.scalars(select(TableCellRow).where(
        TableCellRow.document_id == document_id)
        .order_by(TableCellRow.page_number, TableCellRow.table_id,
                  TableCellRow.row, TableCellRow.col)))
    write_sheet("Table Cells", ["table_id", "page", "row", "col", "role",
                                "text", "numeric_value", "confidence"],
                ((c.table_id, c.page_number, c.row, c.col, c.role, c.text,
                  c.numeric_value, c.confidence) for c in cells))

    commentary = list(session.scalars(select(CommentaryRow).where(
        CommentaryRow.document_id == document_id)))
    write_sheet("Commentary", ["page", "primary_topic", "sentiment", "speaker",
                               "is_quote", "confidence", "text"],
                ((c.page_number, c.primary_topic, c.sentiment, c.speaker,
                  c.is_quote, c.confidence, c.text[:2000]) for c in commentary))

    guidance = list(session.scalars(select(GuidanceRow).where(
        GuidanceRow.document_id == document_id)))
    write_sheet("Guidance", ["page", "nature", "metric", "canonical_metric",
                             "target_value", "target_unit", "target_period",
                             "direction", "confidence", "requires_review",
                             "text"],
                ((g.page_number, g.nature, g.metric, g.canonical_metric,
                  g.target_value, g.target_unit, g.target_period, g.direction,
                  g.confidence, g.requires_review, g.text[:2000])
                 for g in guidance))

    out = io.BytesIO()
    wb.save(out)
    return out.getvalue()


EXPORTERS = {"json": export_json, "csv": export_csv, "xlsx": export_xlsx}
MEDIA_TYPES = {
    "json": "application/json",
    "csv": "text/csv; charset=utf-8",
    "xlsx": ("application/vnd.openxmlformats-officedocument"
             ".spreadsheetml.sheet"),
}
