"""Final data-quality report, in both machine- and human-readable form.

Reads the cold and warm evaluation artefacts produced by
``scripts/final_evaluation.py`` and emits:

* ``data/reports/data_quality.json`` -- one record per document/mode
* ``data/reports/DATA_QUALITY.md``   -- the same, as a table

The two kinds of number are kept apart throughout and labelled as such:

* **benchmark ground-truth accuracy** -- precision / recall / F1 against a
  hand-built answer key covering a fixed number of high-value figures;
* **total extracted facts** -- how much the pipeline produced and how much of
  it is fully attributed.

Conflating them would overstate the system, so the report never adds them
together and always prints the size of the key beside the accuracy.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRATCH = ROOT / "scratch"
OUT_DIR = ROOT / "data" / "reports"

FIELDS = [
    ("document", "Document"),
    ("mode", "Mode"),
    ("pages", "Pages"),
    ("facts", "Facts"),
    ("tables", "Tables"),
    ("cells", "Cells"),
    ("charts", "Charts"),
    ("entities", "Entities"),
    ("distinct_periods", "Periods"),
    ("validation_checks", "Checks"),
    ("validation_warnings", "Warnings"),
    ("review_items", "Review"),
    ("precision", "Precision"),
    ("recall", "Recall"),
    ("f1", "F1"),
    ("key_size", "Key size"),
    ("provenance_coverage", "Provenance"),
    ("cold_seconds", "Cold s"),
    ("warm_seconds", "Warm s"),
]


def build() -> list[dict]:
    cold_path = SCRATCH / "evaluation_cold.json"
    warm_path = SCRATCH / "evaluation_warm.json"
    if not cold_path.is_file():
        print(f"missing {cold_path}; run scripts/final_evaluation.py --cold")
        sys.exit(1)

    cold = json.loads(cold_path.read_text(encoding="utf-8"))
    warm = (json.loads(warm_path.read_text(encoding="utf-8"))
            if warm_path.is_file() else {"documents": []})
    warm_modes = {
        (d["label"], mode): m
        for d in warm["documents"] for mode, m in d["modes"].items()
    }

    rows: list[dict] = []
    for d in cold["documents"]:
        for mode, m in d["modes"].items():
            accuracy = m.get("accuracy") or {}
            has_key = accuracy.get("available") is not False
            w = warm_modes.get((d["label"], mode), {})
            rows.append({
                "document": d["label"],
                "file": d["file"],
                "sha256": d["sha256"],
                "size_bytes": d["size_bytes"],
                "mode": mode,
                "pages": m["pages"],
                "facts": m["facts"],
                "numeric_facts": m["numeric_facts"],
                "tables": m["tables"],
                "cells": m["cells"],
                "charts": m["charts"],
                "entities": m["entities"],
                "entity_names": m["entity_names"],
                "distinct_periods": m["distinct_periods"],
                "facts_with_period": m["with_period"],
                "validation_checks": m["validation_checks"],
                "validation_warnings": m["validation_warnings"],
                "review_items": m["review_items"],
                "derived_metrics": m["derived_metrics"],
                "insights": m["insights"],
                "anomalies": m["anomalies"],
                "risks": m["risks"],
                # --- benchmark ground-truth accuracy (a spot check) ---------
                "precision": accuracy.get("precision") if has_key else None,
                "recall": accuracy.get("recall") if has_key else None,
                "f1": accuracy.get("f1") if has_key else None,
                "matched": accuracy.get("matched") if has_key else None,
                "key_size": accuracy.get("truth_facts") if has_key else None,
                "accuracy_note": (
                    "precision/recall/F1 measure a hand-built key of "
                    f"{accuracy.get('truth_facts')} figures, not all "
                    f"{m['facts']} extracted facts"
                    if has_key else
                    "no answer key for this document; no accuracy is claimed"),
                # --- coverage ------------------------------------------------
                "provenance_coverage": m["provenance_coverage"],
                "bounding_boxes": m["bounding_boxes"],
                # --- performance ---------------------------------------------
                "cold_seconds": m["wall_seconds"],
                "warm_seconds": w.get("wall_seconds"),
                "cold_ms_per_page": m["ms_per_page"],
                "page_phase_seconds": m.get("page_phase_seconds") or {},
                "stage_ms": m.get("stage_ms") or {},
                "persistence_seconds": m.get("persistence_seconds"),
                # --- boundary -------------------------------------------------
                "native_text_audit": m["native_text_audit"],
                "failure_breakdown": m["failure_breakdown"],
            })
    return rows


def render_markdown(rows: list[dict]) -> str:
    out: list[str] = [
        "# Final data-quality report",
        "",
        "Generated by `backend/scripts/data_quality_report.py` from the cold",
        "and warm evaluation runs.",
        "",
        "**Accuracy is a spot check.** Precision, recall and F1 are measured",
        "against a hand-built answer key of a fixed size, shown in the",
        "*Key size* column. They are not a claim about every extracted fact.",
        "A document with no key reports no accuracy.",
        "",
        "| " + " | ".join(label for _, label in FIELDS) + " |",
        "|" + "|".join("---" for _ in FIELDS) + "|",
    ]
    for r in rows:
        cells = []
        for key, _ in FIELDS:
            value = r.get(key)
            if value is None:
                cells.append("—")
            elif key == "provenance_coverage":
                cells.append(f"{value:.0%}")
            elif key == "document":
                cells.append(str(value))
            else:
                cells.append(str(value))
        out.append("| " + " | ".join(cells) + " |")

    out += ["", "## Native-text boundary", "",
            "| Document | Mode | Accesses | Blocked | Feeds extraction |",
            "|---|---|---|---|---|"]
    for r in rows:
        audit = r["native_text_audit"]
        out.append(f"| {r['document']} | {r['mode']} | {audit['allowed_total']} "
                   f"| {audit['blocked_total']} "
                   f"| {audit['extraction_fed_by_native_text']} |")

    out += ["", "## Per-document notes", ""]
    for r in rows:
        if r["mode"] != "VISUAL_ONLY":
            continue
        out.append(f"### {r['document']} ({r['mode']})")
        out.append("")
        out.append(f"- {r['accuracy_note']}")
        out.append(f"- entities discovered: {', '.join(r['entity_names']) or 'none'}")
        out.append(f"- {r['facts_with_period']} of {r['facts']} facts carry a period")
        out.append(f"- provenance coverage {r['provenance_coverage']:.0%} "
                   f"({r['bounding_boxes']} bounding boxes)")
        out.append("")
    return "\n".join(out) + "\n"


def main() -> None:
    rows = build()
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    (OUT_DIR / "data_quality.json").write_text(
        json.dumps(rows, indent=2, default=str), encoding="utf-8")
    (OUT_DIR / "DATA_QUALITY.md").write_text(render_markdown(rows),
                                             encoding="utf-8")
    print(f"wrote {OUT_DIR / 'data_quality.json'}")
    print(f"wrote {OUT_DIR / 'DATA_QUALITY.md'}")
    print(f"{len(rows)} document/mode records")


if __name__ == "__main__":
    main()
