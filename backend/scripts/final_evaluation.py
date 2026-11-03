"""Final validation harness.

Runs every benchmark document through every processing mode plus the native
PDF-text baseline, and emits one machine-readable report (``evaluation.json``)
and one human-readable one (``EVALUATION.md``).

What each system is allowed to read
-----------------------------------
A. **Baseline** -- the PDF's own text layer, and nothing else.  It gets the
   *reconstructed* line mode, which regroups words by y-coordinate rather than
   trusting content-stream order, so it is the strongest form of the baseline
   rather than a straw man.  It never sees a rendered image.
B. **Visual-only** -- rendered page images only.  Native text access is barred
   by ``app.core.modes`` except for the single ingestion probe that decides
   whether the PDF is scanned; that scope cannot feed extraction.
C. **Hybrid** -- images as the canonical representation, with the text layer
   allowed as a *corroboration* signal only.

Usage
-----
    python scripts/final_evaluation.py            # warm (uses the OCR cache)
    python scripts/final_evaluation.py --cold     # clears the OCR cache first
"""
from __future__ import annotations

import json
import shutil
import sys
import time
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config.settings import settings                       # noqa: E402
from app.core.ids import sha256_file                           # noqa: E402
from app.core.modes import ProcessingMode, processing_mode     # noqa: E402
from app.database import repository as repo                    # noqa: E402
from app.database.session import init_db, session_scope        # noqa: E402
from app.evaluation.baseline import run_baseline               # noqa: E402
from app.evaluation.modes import compare_modes                 # noqa: E402
from app.evaluation.runner import run_evaluation               # noqa: E402
from app.ocr.registry import get_engine                        # noqa: E402
from app.services.pipeline import Pipeline                     # noqa: E402
from app.services.processor import persist_result              # noqa: E402

BENCH = Path(__file__).resolve().parents[2] / "data" / "benchmark"
OUT = Path(__file__).resolve().parents[2] / "scratch"

DOCUMENTS = [
    ("RIL Q1 FY27 media release", "Media_Release_RIL_Q1_FY2026-27_"
                                  "Financial_and_Operational_Performance.pdf"),
    ("RIL Q1 FY27 image-only", "RIL_Q1_FY27_IMAGE_ONLY.pdf"),
    ("Apple FY25 Q3 statements", "AAPL_FY25_Q3_Consolidated_Financial_"
                                 "Statements.pdf"),
    ("RIL Q1 FY27 scanner-degraded", "RIL_Q1_FY27_SCANNED_DEGRADED.pdf"),
]


def _register(doc_id: str, pdf: Path) -> None:
    with session_scope() as s:
        if repo.get_document(s, doc_id) is None:
            repo.create_document(
                s, document_id=doc_id, filename=pdf.name,
                original_filename=pdf.name, stored_path=str(pdf),
                content_hash=sha256_file(pdf), size_bytes=pdf.stat().st_size)


def _provenance_coverage(result) -> float:
    if not result.facts:
        return 0.0
    with_box = sum(1 for f in result.facts
                   if f.provenance and f.provenance.bbox is not None)
    return round(with_box / len(result.facts), 4)


def _failure_breakdown(result) -> dict:
    """Classify what stopped each extracted fact from being fully usable.

    These are *attribution* gaps measured on the facts the system produced,
    not a comparison against a key.  A fact can appear under more than one
    heading; the counts are therefore not a partition.
    """
    facts = result.facts
    n = len(facts) or 1
    ocr_weak = sum(1 for f in facts
                   if f.confidence_breakdown
                   and f.confidence_breakdown.ocr < 0.75)
    return {
        "facts": len(facts),
        "ocr_low_confidence": ocr_weak,
        "layout_no_region": sum(1 for f in facts
                                if not (f.provenance and f.provenance.region_id)),
        "table_structure_no_table": sum(
            1 for f in facts
            if f.kind == "table_cell"
            and not (f.provenance and f.provenance.table_id)),
        "metric_semantics_unmatched": sum(1 for f in facts
                                          if not f.canonical_metric),
        "entity_unattributed": sum(1 for f in facts if not f.entity_id),
        "period_unattributed": sum(1 for f in facts if not f.period),
        "unit_missing": sum(1 for f in facts if str(f.unit) == "none"),
        "currency_unknown": sum(
            1 for f in facts
            if str(f.value_type) == "monetary" and str(f.currency) == "UNKNOWN"),
        "unit_inferred_not_declared": sum(1 for f in facts if f.unit_inferred),
        "currency_inferred_not_read": sum(1 for f in facts
                                          if f.currency_inferred),
        "validation_failed_or_warned": sum(
            1 for f in facts
            if str(f.validation_status) in ("FAILED", "WARNING")),
        "validation_not_applicable": sum(
            1 for f in facts if str(f.validation_status) == "NOT_CHECKED"),
        "provenance_missing_bbox": sum(
            1 for f in facts
            if not (f.provenance and f.provenance.bbox is not None)),
        "requires_review": sum(1 for f in facts if f.requires_review),
        "share_requiring_review": round(
            sum(1 for f in facts if f.requires_review) / n, 4),
    }


def evaluate(cold: bool) -> dict:
    init_db()
    get_engine().warmup()
    report: dict = {"cold_run": cold, "documents": []}

    for label, filename in DOCUMENTS:
        pdf = BENCH / filename
        if not pdf.is_file():
            print(f"SKIP {label}: {pdf} not present", flush=True)
            continue

        entry: dict = {
            "label": label, "file": filename,
            "size_bytes": pdf.stat().st_size,
            "sha256": sha256_file(pdf),
            "modes": {},
        }

        # ---- A. native PDF text baseline ---------------------------------
        t0 = time.perf_counter()
        base = run_baseline(pdf, mode="line_reconstructed")
        entry["baseline"] = {
            "allowed_to_read": "PDF text layer only; never a rendered image",
            "pages": base.pages, "chars": base.chars, "lines": base.lines,
            "facts": len(base.facts),
            "numeric_tokens": base.numeric_tokens,
            "numbers_discarded": base.numbers_discarded,
            "with_period": base.with_period, "with_unit": base.with_unit,
            "pages_without_text": base.pages_without_text,
            "bounding_boxes": 0,
            "provenance_coverage": 0.0,
            "wall_seconds": round(time.perf_counter() - t0, 2),
        }

        # ---- B / C. the pipeline, in each mode ---------------------------
        for mode in (ProcessingMode.HYBRID, ProcessingMode.VISUAL_ONLY):
            doc_id = f"final_{filename[:14].replace('.', '_')}_{mode}"
            _register(doc_id, pdf)
            if cold:
                cache = Path(settings.cache_dir)
                shutil.rmtree(cache, ignore_errors=True)
                cache.mkdir(parents=True, exist_ok=True)

            t0 = time.perf_counter()
            with processing_mode(mode):
                result = Pipeline(doc_id, pdf, mode=mode).run()
            wall = time.perf_counter() - t0
            t_persist = time.perf_counter()
            persist_result(doc_id, result)
            persist_seconds = round(time.perf_counter() - t_persist, 2)
            evaluation = run_evaluation(doc_id)

            stages = {s.stage: round(s.duration_ms or 0.0, 1)
                      for s in result.stages}
            # The three per-page phases run interleaved in one parallel loop,
            # so their individual cost is reported as time actually spent in
            # each, summed across workers -- not as a slice of wall clock.
            phases = {s.stage: s.detail.get("phase_seconds")
                      for s in result.stages
                      if isinstance(s.detail, dict)
                      and s.detail.get("phase_seconds") is not None}
            numeric = sum(1 for f in result.facts if f.value is not None)
            entry["modes"][str(mode)] = {
                "allowed_to_read": (
                    "rendered page images only (native text barred except the "
                    "ingestion scanned-or-not probe)"
                    if mode == ProcessingMode.VISUAL_ONLY else
                    "rendered page images, with the text layer as a "
                    "corroboration signal only"),
                "document_id": doc_id,
                "status": str(result.status),
                "pages": len(result.pages),
                "tables": len(result.tables),
                "cells": sum(len(t.cells) for t in result.tables),
                "charts": len(result.charts),
                "facts": len(result.facts),
                "numeric_facts": numeric,
                "bounding_boxes": sum(1 for f in result.facts
                                      if f.provenance
                                      and f.provenance.bbox is not None),
                "provenance_coverage": _provenance_coverage(result),
                "entities": len(result.entities),
                "entity_names": [e.entity_name for e in result.entities],
                "periods": sorted({f.period for f in result.facts if f.period}),
                "distinct_periods": len({f.period for f in result.facts
                                         if f.period}),
                "with_period": sum(1 for f in result.facts if f.period),
                "validation_checks": len(result.validations),
                "validation_warnings": sum(
                    1 for v in result.validations
                    if str(v.status) in ("WARNING", "FAILED")),
                "review_items": sum(1 for f in result.facts if f.requires_review),
                "derived_metrics": len(result.derived_metrics),
                "insights": len(result.insights),
                "anomalies": len(result.anomalies),
                "risks": len(result.risks),
                "accuracy": evaluation.get("accuracy", {}),
                "failure_breakdown": _failure_breakdown(result),
                "native_text_audit": result.native_text_audit,
                "wall_seconds": round(wall, 2),
                "ms_per_page": round(wall * 1000 / max(1, len(result.pages)), 1),
                "stage_ms": stages,
                "page_phase_seconds": phases,
                "persistence_seconds": None,   # filled in below
                "currencies": dict(Counter(str(f.currency)
                                           for f in result.facts)),
                "units": dict(Counter(str(f.unit) for f in result.facts)),
            }
            entry["modes"][str(mode)]["persistence_seconds"] = persist_seconds
            print(f"  {label} :: {mode}: {len(result.facts)} facts, "
                  f"{wall:.1f}s", flush=True)

        # ---- the formal three-mode comparison ---------------------------
        # Uses the purpose-built harness so the baseline is profiled exactly
        # the same way in both of its forms (content-stream order and
        # y-coordinate reconstruction) against the same ground truth.
        visual_id = entry["modes"].get(str(ProcessingMode.VISUAL_ONLY), {}).get(
            "document_id")
        hybrid_id = entry["modes"].get(str(ProcessingMode.HYBRID), {}).get(
            "document_id")
        if visual_id and hybrid_id:
            try:
                entry["three_mode_comparison"] = compare_modes(
                    pdf, visual_id, hybrid_id)
            except Exception as exc:  # noqa: BLE001 - reported, never hidden
                entry["three_mode_comparison"] = {"error": f"{type(exc).__name__}: {exc}"}

        report["documents"].append(entry)

    return report


def main() -> None:
    cold = "--cold" in sys.argv
    report = evaluate(cold)
    OUT.mkdir(parents=True, exist_ok=True)
    name = "evaluation_cold.json" if cold else "evaluation_warm.json"
    (OUT / name).write_text(json.dumps(report, indent=2, default=str),
                            encoding="utf-8")
    print(f"\nwrote {OUT / name}")


if __name__ == "__main__":
    main()
