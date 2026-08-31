"""Three-way comparison: native text vs visual-only vs hybrid.

This is the experiment the whole visual-first argument rests on, so it is a
first-class, repeatable command rather than a one-off script.

A. **NATIVE TEXT BASELINE** — the PDF's own glyph stream, no rendering, no OCR.
   Two variants are measured: the raw content-stream order, and a deliberately
   strong version that rebuilds lines from word coordinates.

B. **VISUAL_ONLY** — rendered pixels, OCR, layout and table geometry, with the
   native text layer hard-blocked from reaching extraction.

C. **HYBRID** — the same visual pipeline, plus the text layer as a corroborating
   channel.

Each mode is scored against the same hand-built ground-truth key, and the audit
ledger for each run is reported so a reader can verify that the visual-only
column really did work from pixels alone.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from sqlalchemy import select

from app.core.logging import get_logger
from app.database.models import (
    Document, FinancialFactRow, OCRElementRow, ProcessingRun, ProvenanceRow,
    RegionRow, TableCellRow, TableRow,
)
from app.database.session import session_scope
from app.evaluation.baseline import run_baseline
from app.evaluation.runner import accuracy_metrics, default_truth_path, load_truth

log = get_logger("evaluation.modes")


def _mode_of(session, document_id: str) -> str:
    run = session.scalars(
        select(ProcessingRun).where(ProcessingRun.document_id == document_id)
        .order_by(ProcessingRun.started_at.desc()).limit(1)).first()
    if run is None:
        return "unknown"
    return (run.settings_snapshot or {}).get("processing_mode", "unknown")


def _audit_of(session, document_id: str) -> dict:
    run = session.scalars(
        select(ProcessingRun).where(ProcessingRun.document_id == document_id)
        .order_by(ProcessingRun.started_at.desc()).limit(1)).first()
    if run is None:
        return {}
    return (run.settings_snapshot or {}).get("native_text_audit", {})


def _count(session, model, document_id: str) -> int:
    return len(list(session.scalars(
        select(model.id).where(model.document_id == document_id))))


def profile_run(document_id: str, truth_path: str | Path | None = None) -> dict:
    """Everything the comparison needs about one processed document."""
    with session_scope() as session:
        doc = session.get(Document, document_id)
        if doc is None:
            raise ValueError(f"Unknown document {document_id}")

        facts = list(session.scalars(select(FinancialFactRow).where(
            FinancialFactRow.document_id == document_id)))
        tables = list(session.scalars(select(TableRow).where(
            TableRow.document_id == document_id)))
        prov_boxed = len([p for p in session.scalars(select(ProvenanceRow).where(
            ProvenanceRow.document_id == document_id)) if p.x is not None])

        path = Path(truth_path) if truth_path else default_truth_path(doc)
        accuracy: dict[str, Any]
        if path and Path(path).is_file():
            accuracy = accuracy_metrics(session, document_id, load_truth(path))
        else:
            accuracy = {"available": False}

        return {
            "document_id": document_id,
            "filename": doc.original_filename,
            "mode": _mode_of(session, document_id),
            "native_text_audit": _audit_of(session, document_id),
            "pages": doc.page_count,
            "pages_processed": doc.pages_processed,
            "ocr_elements": _count(session, OCRElementRow, document_id),
            "layout_regions": _count(session, RegionRow, document_id),
            "tables": len(tables),
            "table_cells": _count(session, TableCellRow, document_id),
            "facts": len(facts),
            "numeric_facts": sum(1 for f in facts if f.value is not None),
            "facts_with_bbox": prov_boxed,
            "facts_with_provenance": _count(session, ProvenanceRow, document_id),
            "facts_with_period": sum(1 for f in facts if f.period),
            "facts_with_unit": sum(1 for f in facts
                                   if f.unit not in (None, "none")),
            "facts_with_currency": sum(1 for f in facts
                                       if f.currency != "UNKNOWN"),
            "facts_with_growth": sum(1 for f in facts if f.growth is not None),
            "validations": _count(session, __import__(
                "app.database.models", fromlist=["ValidationResultRow"]
            ).ValidationResultRow, document_id),
            "review_items": sum(1 for f in facts
                                if f.requires_review
                                and f.review_state == "pending"),
            "processing_ms": doc.processing_ms,
            "precision": accuracy.get("precision"),
            "recall": accuracy.get("recall"),
            "f1": accuracy.get("f1"),
            "matched": accuracy.get("matched"),
            "truth_facts": accuracy.get("truth_facts"),
            "unit_accuracy": accuracy.get("unit_accuracy"),
        }


def profile_baseline(pdf_path: str | Path, mode: str,
                     truth_path: str | Path | None = None) -> dict:
    """Score a text-only baseline against the same key.

    The baseline produces no tables, no bounding boxes and no provenance, so
    those columns are zero by construction rather than by omission.
    """
    result = run_baseline(pdf_path, mode=mode)

    truth = load_truth(truth_path) if truth_path and Path(truth_path).is_file() else []
    matched = wrong = 0
    if truth:
        # match on (metric, period) only: the baseline has no entity concept at
        # all, so requiring one would understate it
        produced: dict[tuple, list[float]] = {}
        for f in result.facts:
            produced.setdefault((f["canonical_metric"], f["period"] or ""),
                                []).append(f["value"])
        for t in truth:
            values = produced.get((t.canonical_metric, t.period))
            if not values:
                continue
            if any(abs(v - t.value) <= abs(t.value) * 0.005 or
                   (t.value == 0 and abs(v) < 1e-9) for v in values):
                matched += 1
            else:
                wrong += 1

    total_produced = len(result.facts)
    precision = matched / (matched + wrong) if (matched + wrong) else 0.0
    recall = matched / len(truth) if truth else 0.0
    f1 = (2 * precision * recall / (precision + recall)
          if (precision + recall) else 0.0)

    return {
        "document_id": None,
        "filename": Path(pdf_path).name,
        "mode": f"NATIVE_TEXT ({mode})",
        "native_text_audit": {"extraction_fed_by_native_text": True,
                              "note": "this mode is native text by definition"},
        "pages": result.pages,
        "pages_processed": result.pages - result.pages_without_text,
        "ocr_elements": 0,
        "layout_regions": 0,
        "tables": 0,
        "table_cells": 0,
        "facts": total_produced,
        "numeric_facts": total_produced,
        "facts_with_bbox": 0,
        "facts_with_provenance": 0,
        "facts_with_period": result.with_period,
        "facts_with_unit": result.with_unit,
        "facts_with_currency": sum(1 for f in result.facts
                                   if f["currency"] != "UNKNOWN"),
        "facts_with_growth": 0,
        "validations": 0,
        "review_items": 0,
        "processing_ms": result.duration_ms,
        "precision": round(precision, 4),
        "recall": round(recall, 4),
        "f1": round(f1, 4),
        "matched": matched,
        "truth_facts": len(truth),
        "unit_accuracy": None,
        "numbers_discarded": result.numbers_discarded,
    }


ROWS = [
    ("pages_processed", "Pages processed"),
    ("ocr_elements", "OCR elements"),
    ("layout_regions", "Layout regions"),
    ("tables", "Tables"),
    ("table_cells", "Table cells"),
    ("facts", "Financial facts"),
    ("numeric_facts", "Numeric facts"),
    ("facts_with_period", "Facts with period"),
    ("facts_with_unit", "Facts with unit"),
    ("facts_with_currency", "Facts with currency"),
    ("facts_with_growth", "Facts with growth"),
    ("facts_with_bbox", "Facts with bounding box"),
    ("facts_with_provenance", "Facts with provenance"),
    ("validations", "Validation checks"),
    ("review_items", "Review items"),
    ("matched", "Ground-truth matched"),
    ("precision", "Precision"),
    ("recall", "Recall"),
    ("f1", "F1"),
    ("processing_ms", "Processing time (ms)"),
]


def render_table(profiles: list[dict]) -> str:
    """A plain-text side-by-side comparison."""
    headers = [p["mode"] for p in profiles]
    width = max(24, max(len(h) for h in headers) + 2)
    lines = []
    lines.append(f"{'':28}" + "".join(f"{h:>{width}}" for h in headers))
    lines.append("-" * (28 + width * len(headers)))
    for key, label in ROWS:
        cells = []
        for p in profiles:
            v = p.get(key)
            if v is None:
                cells.append(f"{'-':>{width}}")
            elif isinstance(v, float):
                cells.append(f"{v:>{width}.4g}")
            else:
                cells.append(f"{v:>{width},}")
        lines.append(f"{label:28}" + "".join(cells))
    lines.append("-" * (28 + width * len(headers)))
    audit_cells = []
    for p in profiles:
        used = p["native_text_audit"].get("extraction_fed_by_native_text")
        audit_cells.append(f"{str(used):>{width}}")
    lines.append(f"{'Extraction used PDF text':28}" + "".join(audit_cells))
    return "\n".join(lines)


def compare_modes(pdf_path: str | Path, visual_document_id: str,
                  hybrid_document_id: str,
                  truth_path: str | Path | None = None) -> dict:
    """Run all three approaches over one document and contrast them."""
    if truth_path is None:
        with session_scope() as session:
            doc = session.get(Document, visual_document_id)
            truth_path = default_truth_path(doc) if doc else None

    profiles = [
        profile_baseline(pdf_path, "naive", truth_path),
        profile_baseline(pdf_path, "line_reconstructed", truth_path),
        profile_run(visual_document_id, truth_path),
        profile_run(hybrid_document_id, truth_path),
    ]
    return {"pdf": str(pdf_path),
            "truth_file": str(truth_path) if truth_path else None,
            "profiles": profiles,
            "table": render_table(profiles)}
