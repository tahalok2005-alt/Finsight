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
    #: The basis the key states for this figure (Phase 22): a qualification
    #: type such as "excluding", or None for the plain measure. A release that
    #: prints last year's quarter excluding a one-off gain, and the statutory
    #: statement that includes it, carry two figures for one measure and
    #: period; the key says which one it means.
    qualification: str | None = None

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
        unit=f.get("unit"), currency=f.get("currency"), page=f.get("page"),
        qualification=f.get("qualification"))
        for f in data.get("facts", [])]


def _qualification_of(f) -> str | None:
    q = (getattr(f, "meta", None) or {}).get("qualification") or {}
    return q.get("qualification_type")


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
        # The figure the key means: on the basis it states, or the plain one.
        candidates = [f for f in by_key.get(t.key, [])
                      if _qualification_of(f) == t.qualification]
        if not candidates:
            missing.append({"metric": t.canonical_metric, "entity": t.entity,
                            "period": t.period, "expected": t.value})
            continue
        # The key records the page each figure is printed on. Where the
        # document prints one slot twice, correctly both times -- finance
        # costs 8,337 on the highlights page and "(8,337)" as a deduction in
        # the statement -- the key means its page's printing; before Phase 22
        # a near-tie of confidence scores decided, and a false alarm on one
        # of them tipped it. ``page_blind_selection`` below reports the score
        # the old rule gives, so the two can be compared.
        on_page = [f for f in candidates if t.page and f.source_page == t.page]
        best = max(on_page or candidates, key=lambda f: f.confidence)
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

    # The same key scored under the earlier selection rules, reported beside
    # the headline so a change of evaluator can never pass for a change of
    # extraction: "page_blind" drops the page preference; "phase20" drops the
    # qualification as well, as the benchmark did before Phase 22.
    def _alternative(use_qualification: bool) -> float:
        hits = wrong = 0
        for t in truth:
            cands = by_key.get(t.key, [])
            if use_qualification:
                cands = [f for f in cands if _qualification_of(f) == t.qualification]
            if not cands:
                continue
            b = max(cands, key=lambda f: f.confidence)
            val = b.corrected_value if b.corrected_value is not None else b.value
            if val is not None and _values_match(t.value, val):
                hits += 1
            else:
                wrong += 1
        p = hits / (hits + wrong) if (hits + wrong) else 0.0
        r = hits / len(truth) if truth else 0.0
        return round(2 * p * r / (p + r), 4) if (p + r) else 0.0

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
        "f1_page_blind_selection": _alternative(True),
        "f1_phase20_selection": _alternative(False),
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

        report["semantics"] = semantic_metrics(session, document_id)

    return report


def semantic_metrics(session, document_id: str) -> dict[str, Any]:
    """How much of the *semantic* layer the document actually supported.

    These are coverage counts over what was extracted, not accuracy scores:
    they say how many facts carry a statement, an entity, a period and a unit,
    and how much intelligence the pipeline was able to build on top.  They are
    reported separately from the ground-truth spot check so the two are never
    confused -- a document can be fully attributed and still have figures
    wrong, and the spot check is what measures that.
    """
    from app.database.models_intelligence import (
        AnomalyRow, DerivedMetricRow, EntityRow, FactRelationRow, InsightRow,
        RiskRow,
    )

    facts = list(session.scalars(
        select(FinancialFactRow).where(
            FinancialFactRow.document_id == document_id)))
    total = len(facts) or 1

    def share(predicate) -> float:
        return round(sum(1 for f in facts if predicate(f)) / total, 4)

    by_statement: dict[str, int] = {}
    by_category: dict[str, int] = {}
    by_entity: dict[str, int] = {}
    for f in facts:
        by_statement[f.statement or "unknown"] = (
            by_statement.get(f.statement or "unknown", 0) + 1)
        by_category[f.category or "other"] = (
            by_category.get(f.category or "other", 0) + 1)
        key = f.entity or "unattributed"
        by_entity[key] = by_entity.get(key, 0) + 1

    def count(model) -> int:
        return int(session.scalar(
            select(func.count()).select_from(model)
            .where(model.document_id == document_id)) or 0)

    derived = list(session.scalars(
        select(DerivedMetricRow)
        .where(DerivedMetricRow.document_id == document_id)))
    checked = [m for m in derived if m.agrees_with_reported is not None]

    return {
        "facts_total": len(facts),
        "attribution": {
            "with_period": share(lambda f: bool(f.period)),
            "with_entity": share(lambda f: bool(f.entity_id)),
            "with_statement": share(
                lambda f: bool(f.statement) and f.statement != "unknown"),
            "with_currency": share(lambda f: f.currency not in (None, "UNKNOWN")),
            "with_unit": share(lambda f: f.unit not in (None, "none")),
            "unit_inferred": share(lambda f: bool(f.unit_inferred)),
            "currency_inferred": share(lambda f: bool(f.currency_inferred)),
        },
        "facts_by_statement": dict(sorted(by_statement.items(),
                                          key=lambda kv: -kv[1])),
        "facts_by_category": dict(sorted(by_category.items(),
                                         key=lambda kv: -kv[1])),
        "facts_by_entity": dict(sorted(by_entity.items(), key=lambda kv: -kv[1])),
        "entities": count(EntityRow),
        "derived_metrics": {
            "total": len(derived),
            "with_reported_counterpart": len(checked),
            "agreeing": sum(1 for m in checked if m.agrees_with_reported),
            "disagreeing": sum(1 for m in checked
                               if m.agrees_with_reported is False),
            "note": ("Agreement is measured against the document's own "
                     "published ratio, where it published one. It is not a "
                     "ground-truth accuracy figure."),
        },
        "relations": count(FactRelationRow),
        "risks": count(RiskRow),
        "insights": count(InsightRow),
        "anomalies": count(AnomalyRow),
    }
