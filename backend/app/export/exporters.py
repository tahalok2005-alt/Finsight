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
from app.database.models_intelligence import (
    AnomalyRow, DerivedMetricRow, EntityRow, FactRelationRow, InsightRow,
    RiskRow, TableSemanticsRow,
)
from app.core.logging import get_logger
from app.models.enums import REVIEW_REASON_TEXT

log = get_logger("export")

FACT_COLUMNS: list[tuple[str, str]] = [
    ("fact_id", "id"),
    ("metric", "metric"),
    ("canonical_metric", "canonical_metric"),
    ("metric_display", "metric_display"),
    ("entity", "entity"),
    ("entity_id", "entity_id"),
    ("entity_type", "entity_type"),
    ("parent_entity_id", "parent_entity_id"),
    ("statement", "statement"),
    ("category", "category"),
    ("derivation", "derivation"),
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
    ("period_start", "period_start"),
    ("period_end", "period_end"),
    ("period_basis", "period_basis"),
    ("period_confidence", "period_confidence"),
    ("period_raw", "period_raw"),
    ("comparison_period", "comparison_period"),
    ("comparison_value", "comparison_value"),
    ("growth", "growth"),
    ("growth_type", "growth_type"),
    ("growth_reported", "growth_reported"),
    ("growth_calculated", "growth_calculated"),
    ("change_absolute", "change_absolute"),
    ("change_bps", "change_bps"),
    ("duplicate_resolution", "duplicate_resolution"),
    ("unit_inferred", "unit_inferred"),
    ("currency_inferred", "currency_inferred"),
    ("confidence", "confidence"),
    ("confidence_band", "confidence_band"),
    ("validation_status", "validation_status"),
    ("requires_review", "requires_review"),
    ("review_reason", "review_reason"),
    ("review_state", "review_state"),
    ("corrected_value", "corrected_value"),
    ("source_page", "source_page"),
    ("region_id", "region_id"),
    ("table_id", "table_id"),
    ("cell_id", "cell_id"),
    ("original_text", "original_text"),
]

#: What a spreadsheet export carries, in the order an analyst reads it.
#:
#: The full column set above is the record of everything the system knows,
#: which is the right thing for the JSON export and the wrong thing for a
#: spreadsheet: region ids, cell ids and bounding boxes are addresses inside
#: this system, and a column of them is noise to anyone opening the file to
#: look at figures. The document, the metric, the value, the period, the
#: source and the confidence are what answer a question about a number.
SPREADSHEET_COLUMNS: list[str] = [
    "fact_id", "document", "metric", "canonical_metric", "source_line_item",
    "entity", "entity_id", "entity_type", "statement", "category",
    "value", "raw_value", "unit", "currency", "value_type", "base_value",
    "period", "period_type", "period_start", "period_end", "period_basis",
    "fiscal_year", "quarter", "comparison_period", "comparison_value",
    "growth", "growth_type", "growth_reported", "growth_calculated",
    "derivation",
    "confidence", "confidence_band", "validation_status", "requires_review",
    "review_reason",
    # What the evidence layer made of this figure. An analyst deciding whether
    # to quote a number wants to know that two independent readings of the page
    # agreed on it -- or that something else in the document did not.
    "extraction_strategy", "reconciliation", "corroborating_sources",
    "corroboration_count", "conflicting_value", "conflicting_page",
    "source_page", "source_table", "source_url",
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
        # The evidence layer records corroboration and conflicts on the fact's
        # meta; the exporter needs it to fill the reconciliation columns.
        row["meta"] = f.meta
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
                           "guidance", "validation", "entities",
                           "derived_metrics", "relations", "risks",
                           "insights", "anomalies", "table_semantics"])

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

    # ---- financial intelligence -------------------------------------------
    # Derived metrics are exported in their own section, never merged into
    # "facts": a calculated figure must stay distinguishable from a reported
    # one in every artefact the system produces, exports included.
    if "entities" in want:
        payload["entities"] = [{
            "entity_id": e.id, "entity_name": e.name,
            "entity_type": e.entity_type,
            "parent_entity_id": e.parent_entity_id, "aliases": e.aliases,
            "confidence": e.confidence, "source": e.source,
            "first_page": e.first_page, "evidence": e.evidence,
        } for e in session.scalars(select(EntityRow).where(
            EntityRow.document_id == document_id))]

    if "derived_metrics" in want:
        payload["derived_metrics"] = [{
            "id": m.id, "canonical_metric": m.canonical_metric,
            "metric_display": m.metric_display, "entity_id": m.entity_id,
            "entity_name": m.entity_name, "period": m.period, "value": m.value,
            "value_type": m.value_type, "unit": m.unit, "currency": m.currency,
            "formula": m.formula, "inputs": m.inputs,
            "input_values": m.input_values,
            "reported_fact_id": m.reported_fact_id,
            "reported_value": m.reported_value,
            "agrees_with_reported": m.agrees_with_reported,
            "difference": m.difference, "confidence": m.confidence,
            "derivation": m.derivation, "source_pages": m.source_pages,
            "notes": m.notes,
        } for m in session.scalars(select(DerivedMetricRow).where(
            DerivedMetricRow.document_id == document_id))]

    if "relations" in want:
        payload["relations"] = [{
            "id": r.id, "from_fact_id": r.from_fact_id,
            "to_fact_id": r.to_fact_id, "kind": r.kind, "detail": r.detail,
            "confidence": r.confidence,
        } for r in session.scalars(select(FactRelationRow).where(
            FactRelationRow.document_id == document_id))]

    if "risks" in want:
        payload["risks"] = [{
            "id": r.id, "page_number": r.page_number, "text": r.text,
            "category": r.category, "categories": r.categories,
            "entity_name": r.entity_name, "period": r.period,
            "trigger_phrase": r.trigger_phrase, "severity": r.severity,
            "is_forward_looking": r.is_forward_looking,
            "confidence": r.confidence,
        } for r in session.scalars(select(RiskRow).where(
            RiskRow.document_id == document_id))]

    if "insights" in want:
        payload["insights"] = [{
            "id": i.id, "insight_type": i.insight_type,
            "headline": i.headline, "detail": i.detail,
            "entity_name": i.entity_name, "metric": i.metric,
            "periods": i.periods,
            "supporting_fact_ids": i.supporting_fact_ids,
            "value": i.value, "comparison_value": i.comparison_value,
            "change": i.change, "change_unit": i.change_unit,
            "derivation": i.derivation, "confidence": i.confidence,
            "source_pages": i.source_pages,
        } for i in session.scalars(select(InsightRow).where(
            InsightRow.document_id == document_id))]

    if "anomalies" in want:
        payload["anomalies"] = [{
            "id": a.id, "anomaly_type": a.anomaly_type,
            "severity": a.severity, "message": a.message, "reason": a.reason,
            "entity_name": a.entity_name, "metric": a.metric,
            "period": a.period, "observed_value": a.observed_value,
            "expected_value": a.expected_value, "deviation": a.deviation,
            "threshold": a.threshold, "fact_ids": a.fact_ids,
            "source_pages": a.source_pages,
        } for a in session.scalars(select(AnomalyRow).where(
            AnomalyRow.document_id == document_id))]

    if "table_semantics" in want:
        payload["table_semantics"] = [{
            "id": t.id, "table_id": t.table_id, "page_number": t.page_number,
            "table_type": t.table_type, "statement": t.statement,
            "title": t.title, "entity_name": t.entity_name,
            "periods": t.periods, "current_period": t.current_period,
            "comparison_period": t.comparison_period,
            "sequential_period": t.sequential_period,
            "unit": t.unit, "currency": t.currency, "columns": t.columns,
            "row_metrics": t.row_metrics, "confidence": t.confidence,
            "notes": t.notes,
        } for t in session.scalars(select(TableSemanticsRow).where(
            TableSemanticsRow.document_id == document_id))]

    payload["notice"] = (
        "Figures under 'facts' are as the document reported them. Figures "
        "under 'derived_metrics' were calculated by FinSight and each carries "
        "its formula and the fact ids it was computed from. The two are never "
        "merged."
    )
    return json.dumps(payload, indent=2, default=str).encode("utf-8")


def _reconciliation_columns(row: dict[str, Any]) -> dict[str, Any]:
    """How this figure stood up against the rest of the document.

    Exported as plain columns rather than as a nested blob: a spreadsheet is
    filtered and sorted, and "show me every figure another source disputes" has
    to be one column away.
    """
    meta = row.get("meta") or {}
    if isinstance(meta, str):
        try:
            meta = json.loads(meta)
        except ValueError:
            meta = {}
    sources = meta.get("corroborating_sources") or []
    conflicts = meta.get("conflicts") or []
    first = conflicts[0] if conflicts else {}
    return {
        "extraction_strategy": (meta.get("extraction_strategy")
                                or row.get("extraction_method")),
        "reconciliation": row.get("duplicate_resolution"),
        "corroborating_sources": ", ".join(sources),
        "corroboration_count": (len(sources) if sources else None),
        "conflicting_value": first.get("value"),
        "conflicting_page": first.get("page"),
    }


def _spreadsheet_rows(session: Session, document_id: str) -> list[dict[str, Any]]:
    """The fact rows, named and ordered for someone reading a spreadsheet."""
    doc = session.get(Document, document_id)
    name = doc.original_filename if doc else document_id
    out: list[dict[str, Any]] = []
    for r in _fact_rows(session, document_id):
        reason = r.get("review_reason")
        out.append({
            "fact_id": r.get("fact_id"),
            "document": name,
            # All three names are wanted and none substitutes for the others:
            # the display name reads, the canonical key groups and joins across
            # documents, and the printed line item is what the reader will
            # actually find on the page.
            "metric": r.get("metric_display") or r.get("canonical_metric"),
            "canonical_metric": r.get("canonical_metric"),
            "source_line_item": r.get("metric"),
            "entity": r.get("entity"),
            "entity_id": r.get("entity_id"),
            "entity_type": r.get("entity_type"),
            "statement": r.get("statement"),
            "category": r.get("category"),
            "base_value": r.get("base_value"),
            "comparison_value": r.get("comparison_value"),
            "growth_reported": r.get("growth_reported"),
            # Whether the document said this or FinSight worked it out. A
            # spreadsheet that loses that distinction lets a derived figure be
            # quoted as though the filer had published it.
            "growth_calculated": r.get("growth_calculated"),
            "derivation": r.get("derivation"),
            **_reconciliation_columns(r),
            "value": (r.get("corrected_value")
                      if r.get("corrected_value") is not None else r.get("value")),
            "raw_value": r.get("raw_value"),
            "unit": r.get("unit"),
            "currency": r.get("currency"),
            "value_type": r.get("value_type"),
            "period": r.get("period"),
            "period_type": r.get("period_type"),
            "period_start": r.get("period_start"),
            "period_end": r.get("period_end"),
            "period_basis": r.get("period_basis"),
            "fiscal_year": r.get("fiscal_year"),
            "quarter": r.get("quarter"),
            "comparison_period": r.get("comparison_period"),
            "growth": r.get("growth"),
            "growth_type": r.get("growth_type"),
            "confidence": r.get("confidence"),
            "confidence_band": r.get("confidence_band"),
            "validation_status": r.get("validation_status"),
            "requires_review": r.get("requires_review"),
            "review_reason": REVIEW_REASON_TEXT.get(reason, reason or ""),
            "source_page": r.get("source_page"),
            "source_table": r.get("table_id"),
            "source_url": r.get("source_url"),
        })
    return out


def export_csv(session: Session, document_id: str) -> bytes:
    rows = _spreadsheet_rows(session, document_id)
    buf = io.StringIO(newline="")
    writer = csv.DictWriter(buf, fieldnames=SPREADSHEET_COLUMNS,
                            extrasaction="ignore")
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
