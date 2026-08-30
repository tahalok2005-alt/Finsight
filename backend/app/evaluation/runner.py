"""Evaluation framework.

Two kinds of measurement, kept separate because they answer different questions:

1. **Intrinsic metrics** need no ground truth and can run on any document: OCR
   confidence, text-layer agreement, table structural soundness, unit/period
   normalisation coverage, provenance completeness, validation pass rate.
   These measure whether the system is internally coherent.

2. **Accuracy against ground truth** requires a hand-checked answer key
   (``data/benchmark/*.truth.json``).  Precision, recall and F1 are computed by
   matching on (metric, entity, period) and comparing values within tolerance.
   A key is checked in for the acceptance document; the *values in it were read
   off the PDF by eye*, and the pipeline has no access to it at runtime.

Nothing here inspects the answer key during extraction -- it is loaded only to
score a finished run.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from sqlalchemy import func, select

from app.config.settings import settings
from app.core.logging import get_logger
from app.database.models import (
    Document, FinancialFactRow, OCRElementRow, Page, ProvenanceRow, TableCellRow,
    TableRow, ValidationResultRow,
)
from app.database.session import session_scope

log = get_logger("evaluation")

VALUE_TOLERANCE_REL = 0.005  # 0.5% -- absorbs rounding, not a misread digit


@dataclass(slots=True)
class TruthFact:
    canonical_metric: str
    period: str
    value: float
    entity: str = "Consolidated"
    unit: str | None = None
    currency: str | None = None
    page: int | None = None

    @property
    def key(self) -> tuple[str, str, str]:
        return (self.canonical_metric, normalize_entity(self.entity), self.period)


_ENTITY_NOISE = re.compile(
    r"\b(limited|ltd|inc|plc|the|and|of|segment|business|division|"
    r"exploration|production)\b", re.I)


def normalize_entity(name: str | None) -> str:
    """Compare entities on their distinctive tokens.

    The answer key names entities the way a reader would ("Oil and Gas"); the
    pipeline uses whatever the document's own banner said ("OIL AND GAS
    (EXPLORATION AND PRODUCTION)").  Scoring should reward correct *attribution*,
    not identical spelling, so both sides are reduced to a token signature.
    """
    if not name:
        return "consolidated"
    t = re.sub(r"[^a-z0-9 ]+", " ", name.lower())
    t = _ENTITY_NOISE.sub(" ", t)
    tokens = sorted(w for w in t.split() if len(w) > 1)
    return " ".join(tokens) or "consolidated"


def load_truth(path: str | Path) -> list[TruthFact]:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    return [TruthFact(
        canonical_metric=f["canonical_metric"], period=f["period"],
        value=float(f["value"]), entity=f.get("entity", "Consolidated"),
        unit=f.get("unit"), currency=f.get("currency"), page=f.get("page"))
        for f in data.get("facts", [])]


def default_truth_path(document: Document) -> Path | None:
    stem = Path(document.original_filename).stem
    candidate = settings.data_dir / "benchmark" / f"{stem}.truth.json"
    return candidate if candidate.is_file() else None


# ------------------------------------------------------------------ intrinsic
def intrinsic_metrics(session, document_id: str) -> dict[str, Any]:
    pages = list(session.scalars(
        select(Page).where(Page.document_id == document_id)))
    facts = list(session.scalars(
        select(FinancialFactRow).where(
            FinancialFactRow.document_id == document_id)))
    tables = list(session.scalars(
        select(TableRow).where(TableRow.document_id == document_id)))

    n_elements = int(session.scalar(select(func.count()).select_from(
        OCRElementRow).where(OCRElementRow.document_id == document_id)) or 0)
    n_corroborated = int(session.scalar(select(func.count()).select_from(
        OCRElementRow).where(OCRElementRow.document_id == document_id,
                             OCRElementRow.corroborated.is_(True))) or 0)
    n_prov = int(session.scalar(select(func.count()).select_from(
        ProvenanceRow).where(ProvenanceRow.document_id == document_id)) or 0)
    n_prov_boxed = int(session.scalar(select(func.count()).select_from(
        ProvenanceRow).where(ProvenanceRow.document_id == document_id,
                             ProvenanceRow.x.is_not(None))) or 0)

    cells = list(session.scalars(select(TableCellRow).where(
        TableCellRow.document_id == document_id)))
    numeric_cells = sum(1 for c in cells if c.is_numeric)
    parsed_cells = sum(1 for c in cells if c.is_numeric
                       and c.numeric_value is not None)

    ok_pages = [p for p in pages if p.status == "SUCCEEDED"]
    confs = [p.ocr_mean_confidence for p in ok_pages
             if p.ocr_mean_confidence is not None]

    validations = list(session.scalars(select(ValidationResultRow).where(
        ValidationResultRow.document_id == document_id)))
    checked = [v for v in validations if v.status != "NOT_CHECKED"]

    def ratio(a: int, b: int) -> float:
        return round(a / b, 4) if b else 0.0

    return {
        "pages": {
            "total": len(pages), "succeeded": len(ok_pages),
            "failed": sum(1 for p in pages if p.status == "FAILED"),
            "coverage": ratio(len(ok_pages), len(pages)),
        },
        "ocr": {
            "elements": n_elements,
            "mean_confidence": round(sum(confs) / len(confs), 4) if confs else 0.0,
            "min_page_confidence": round(min(confs), 4) if confs else 0.0,
            "text_layer_agreement": ratio(n_corroborated, n_elements),
            "note": ("text_layer_agreement is the share of OCR tokens the PDF "
                     "text layer independently confirms; it is 0 for scans, "
                     "which is expected, not a failure."),
        },
        "tables": {
            "count": len(tables),
            "mean_confidence": round(
                sum(t.confidence for t in tables) / len(tables), 4) if tables else 0.0,
            "cells": len(cells),
            "numeric_cells": numeric_cells,
            "numeric_parse_rate": ratio(parsed_cells, numeric_cells),
            "with_period_columns": sum(
                1 for t in tables
                if any((c or {}).get("normalized_period") for c in (t.columns or []))),
            "continuations_linked": sum(
                1 for t in tables if t.continued_from_table_id),
            "with_warnings": sum(1 for t in tables if t.warnings),
        },
        "normalization": {
            "facts": len(facts),
            "unit_resolved": ratio(sum(1 for f in facts
                                       if f.unit not in (None, "none")), len(facts)),
            "currency_resolved": ratio(sum(1 for f in facts
                                           if f.currency != "UNKNOWN"), len(facts)),
            "period_resolved": ratio(sum(1 for f in facts if f.period), len(facts)),
            "entity_resolved": ratio(sum(1 for f in facts if f.entity), len(facts)),
            "growth_linked": ratio(sum(1 for f in facts if f.growth is not None),
                                   len(facts)),
        },
        "provenance": {
            "facts_with_record": ratio(n_prov, len(facts)),
            "records_with_bbox": ratio(n_prov_boxed, n_prov),
            "complete": ratio(n_prov_boxed, len(facts)),
        },
        "confidence": {
            band: int(session.scalar(select(func.count()).select_from(
                FinancialFactRow).where(
                FinancialFactRow.document_id == document_id,
                FinancialFactRow.confidence_band == band)) or 0)
            for band in ("HIGH", "MEDIUM", "LOW", "REQUIRES_REVIEW")
        },
        "validation": {
            "checks": len(validations),
            "passed": sum(1 for v in validations if v.status == "PASSED"),
            "warnings": sum(1 for v in validations if v.status == "WARNING"),
            "failures": sum(1 for v in validations if v.status == "FAILED"),
            "requires_review": sum(1 for v in validations
                                   if v.status == "REQUIRES_REVIEW"),
            "pass_rate": ratio(sum(1 for v in checked if v.status == "PASSED"),
                               len(checked)),
            "by_family": _family_breakdown(validations),
        },
        "review": {
            "queue": sum(1 for f in facts
                         if f.requires_review and f.review_state == "pending"),
            "rate": ratio(sum(1 for f in facts if f.requires_review), len(facts)),
        },
    }


def _family_breakdown(validations) -> dict[str, dict[str, int]]:
    out: dict[str, dict[str, int]] = {}
    for v in validations:
        out.setdefault(v.rule_family, {}).setdefault(v.status, 0)
        out[v.rule_family][v.status] += 1
    return out


# ------------------------------------------------------------------- accuracy
def _values_match(expected: float, actual: float) -> bool:
    if expected == 0:
        return abs(actual) < 1e-9
    return abs(actual - expected) / abs(expected) <= VALUE_TOLERANCE_REL


def accuracy_metrics(session, document_id: str,
                     truth: list[TruthFact]) -> dict[str, Any]:
    """Precision / recall / F1 against a hand-checked answer key."""
    facts = list(session.scalars(select(FinancialFactRow).where(
        FinancialFactRow.document_id == document_id)))

    by_key: dict[tuple, list[FinancialFactRow]] = {}
    for f in facts:
        by_key.setdefault(
            (f.canonical_metric, normalize_entity(f.entity), f.period or ""),
            []).append(f)

    matched: list[dict] = []
    wrong_value: list[dict] = []
    missing: list[dict] = []

    for t in truth:
        candidates = by_key.get(t.key, [])
        if not candidates:
            missing.append({"metric": t.canonical_metric, "entity": t.entity,
                            "period": t.period, "expected": t.value})
            continue
        best = max(candidates, key=lambda f: f.confidence)
        actual = (best.corrected_value if best.corrected_value is not None
                  else best.value)
        record = {"metric": t.canonical_metric, "entity": t.entity,
                  "period": t.period, "expected": t.value, "actual": actual,
                  "unit": best.unit, "expected_unit": t.unit,
                  "confidence": best.confidence, "page": best.source_page,
                  "expected_page": t.page, "fact_id": best.id}
        if actual is not None and _values_match(t.value, actual):
            matched.append(record)
        else:
            wrong_value.append(record)

    truth_keys = {t.key for t in truth}
    # Only metrics the key covers count as false positives; the pipeline
    # legitimately extracts far more than a spot-check key enumerates.
    covered_metrics = {t.canonical_metric for t in truth}
    spurious = [
        {"metric": f.canonical_metric, "entity": f.entity, "period": f.period,
         "value": f.value, "page": f.source_page, "confidence": f.confidence}
        for f in facts
        if f.canonical_metric in covered_metrics
        and (f.canonical_metric, normalize_entity(f.entity), f.period or "")
        not in truth_keys]

    tp, fn = len(matched), len(missing) + len(wrong_value)
    fp = len(wrong_value)
    precision = tp / (tp + fp) if (tp + fp) else 0.0
    recall = tp / (tp + len(missing) + len(wrong_value)) if truth else 0.0
    f1 = (2 * precision * recall / (precision + recall)
          if (precision + recall) else 0.0)

    unit_ok = sum(1 for m in matched
                  if not m["expected_unit"] or m["unit"] == m["expected_unit"])
    page_ok = sum(1 for m in matched
                  if not m["expected_page"] or m["page"] == m["expected_page"])

    return {
        "truth_facts": len(truth),
        "matched": tp, "wrong_value": len(wrong_value), "missing": len(missing),
        "precision": round(precision, 4), "recall": round(recall, 4),
        "f1": round(f1, 4),
        "value_accuracy": round(tp / len(truth), 4) if truth else 0.0,
        "unit_accuracy": round(unit_ok / tp, 4) if tp else 0.0,
        "provenance_page_accuracy": round(page_ok / tp, 4) if tp else 0.0,
        "tolerance_rel": VALUE_TOLERANCE_REL,
        "errors": {"wrong_value": wrong_value[:40], "missing": missing[:40]},
        "unexpected_period_combinations": spurious[:20],
    }


# --------------------------------------------------------------------- runner
def run_evaluation(document_id: str, truth_path: str | Path | None = None,
                   ) -> dict[str, Any]:
    with session_scope() as session:
        doc = session.get(Document, document_id)
        if doc is None:
            raise ValueError(f"Unknown document {document_id}")

        report: dict[str, Any] = {
            "document": {
                "id": doc.id, "filename": doc.original_filename,
                "status": doc.status, "pages": doc.page_count,
                "company": doc.company_name, "period": doc.report_period,
                "is_scanned": doc.is_scanned,
                "processing_ms": doc.processing_ms,
                "ms_per_page": (round(doc.processing_ms / doc.page_count, 1)
                                if doc.processing_ms and doc.page_count else None),
            },
            "intrinsic": intrinsic_metrics(session, document_id),
        }

        path = Path(truth_path) if truth_path else default_truth_path(doc)
        if path and Path(path).is_file():
            truth = load_truth(path)
            report["accuracy"] = accuracy_metrics(session, document_id, truth)
            report["accuracy"]["truth_file"] = str(path)
        else:
            report["accuracy"] = {
                "available": False,
                "reason": ("No ground-truth file for this document. Add "
                           f"data/benchmark/{Path(doc.original_filename).stem}"
                           ".truth.json to measure accuracy."),
            }

        try:
            from app.evaluation.baseline import compare_baseline_vs_visual
            report["baseline_comparison"] = compare_baseline_vs_visual(
                doc.stored_path, document_id=document_id)
        except Exception as exc:  # noqa: BLE001 - evaluation must not crash
            log.warning("evaluation.baseline_failed", error=str(exc))
            report["baseline_comparison"] = {"error": str(exc)}

    return report
