"""Baseline (raw PDF text extraction) versus the visual pipeline.

This exists to test the project's central claim rather than assert it: that
treating a financial PDF as a *visual* document recovers structure that a text
extractor cannot.

To keep the comparison honest, *two* baselines are measured, both using the same
taxonomy, number parser and period parser as the real pipeline -- only the
visual stages differ:

``naive``
    The PDF text layer in its native content-stream order.  This is what a plain
    text extractor gives you, and it is worth measuring because the result is
    surprising: in this document the content stream emits each span on its own
    line, so a row label and its figures are not even adjacent.

``line_reconstructed``
    A deliberately *strong* baseline.  Words are pulled with their coordinates
    and grouped into visual lines by y-position, so labels and their figures are
    correctly paired.  This already borrows one geometric idea from the visual
    pipeline, and it is included so the comparison cannot be accused of beating
    a strawman.  What it still cannot do is decide *which column* -- and
    therefore which reporting period -- each figure belongs to, because that
    needs column geometry, ruling lines and header alignment.

Any remaining difference is attributable to visual document understanding rather
than to a weaker parser.  And note what neither baseline can do at all: on a
scanned page both return nothing, because there is no text layer to read.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from time import perf_counter

import pymupdf

from app.core.logging import get_logger
from app.extraction.taxonomy import match_metric
from app.normalization.numbers import looks_numeric, normalize_number, parse_number
from app.normalization.periods import DEFAULT_CALENDAR, find_all_periods, parse_period

log = get_logger("evaluation.baseline")

_WS = re.compile(r"\s{2,}")


@dataclass(slots=True)
class BaselineResult:
    pages: int = 0
    chars: int = 0
    lines: int = 0
    facts: list[dict] = field(default_factory=list)
    numeric_tokens: int = 0
    duration_ms: float = 0.0
    pages_without_text: int = 0
    numbers_discarded: int = 0
    mode: str = "line_reconstructed"

    @property
    def with_period(self) -> int:
        return sum(1 for f in self.facts if f.get("period"))

    @property
    def with_unit(self) -> int:
        return sum(1 for f in self.facts if f.get("unit") not in (None, "none"))


def _page_lines_naive(page) -> list[str]:
    """Lines exactly as the PDF content stream orders them."""
    return [ln.strip() for ln in (page.get_text("text") or "").splitlines()
            if ln.strip()]


def _page_lines_reconstructed(page, y_tol: float = 3.0) -> list[str]:
    """Lines rebuilt by grouping words on their y-coordinate.

    This is the strong baseline: it repairs the label/figure pairing that the
    raw content stream loses, without any rendering, OCR or column analysis.
    """
    words = page.get_text("words")  # (x0, y0, x1, y1, text, block, line, word)
    if not words:
        return []
    buckets: dict[int, list] = {}
    for w in words:
        buckets.setdefault(int(w[1] / y_tol), []).append(w)
    lines: list[str] = []
    for key in sorted(buckets):
        row = sorted(buckets[key], key=lambda w: w[0])
        lines.append(" ".join(w[4] for w in row).strip())
    return [ln for ln in lines if ln]


def run_baseline(pdf_path: str | Path, *, mode: str = "line_reconstructed",
                 ) -> BaselineResult:
    """Extract facts from the PDF text layer only.

    ``mode`` selects ``naive`` (content-stream order) or ``line_reconstructed``
    (words grouped by y-position).  Neither performs column analysis.
    """
    t0 = perf_counter()
    result = BaselineResult()
    result.mode = mode
    seen: set[tuple] = set()

    with pymupdf.open(pdf_path) as doc:
        result.pages = doc.page_count
        for i in range(doc.page_count):
            page = doc[i]
            try:
                text = page.get_text("text") or ""
                lines = (_page_lines_naive(page) if mode == "naive"
                         else _page_lines_reconstructed(page))
            except Exception as exc:  # noqa: BLE001
                log.warning("baseline.page_failed", page=i + 1, error=str(exc))
                continue
            if len(text.strip()) < 40:
                result.pages_without_text += 1
                continue
            result.chars += len(text)

            page_periods = find_all_periods(text, calendar=DEFAULT_CALENDAR)
            default_period = page_periods[0].label if page_periods else None

            for line in lines:
                result.lines += 1
                parts = [p.strip() for p in _WS.split(line) if p.strip()]
                if len(parts) < 2:
                    parts = line.split()
                numbers = [p for p in parts if looks_numeric(p)]
                result.numeric_tokens += len(numbers)
                if not numbers:
                    continue

                label = " ".join(p for p in parts if not looks_numeric(p)).strip()
                if not label:
                    continue
                m = match_metric(label)
                if m is None:
                    continue

                period = parse_period(line, calendar=DEFAULT_CALENDAR)
                period_label = period.label if period else default_period

                # Without column geometry there is no way to know which period
                # each number belongs to.  The best a text pipeline can do is
                # take the first figure and attribute it to the page's period.
                num = normalize_number(numbers[0], context=line)
                if num is None:
                    continue
                result.numbers_discarded += max(0, len(numbers) - 1)
                key = (m.key, period_label, round(num.value, 4))
                if key in seen:
                    continue
                seen.add(key)
                result.facts.append({
                    "canonical_metric": m.key, "metric": label[:120],
                    "value": num.value, "raw_value": numbers[0],
                    "unit": str(num.magnitude), "currency": str(num.currency),
                    "period": period_label, "page": i + 1,
                    "n_numbers_on_line": len(numbers),
                })

    result.duration_ms = round((perf_counter() - t0) * 1000, 1)
    return result


def compare_baseline_vs_visual(pdf_path: str | Path,
                               document_id: str | None = None) -> dict:
    """Run both approaches on one PDF and contrast what each recovers."""
    from sqlalchemy import select  # noqa: PLC0415
    from app.database.models import FinancialFactRow, TableRow  # noqa: PLC0415
    from app.database.session import session_scope  # noqa: PLC0415

    naive = run_baseline(pdf_path, mode="naive")
    baseline = run_baseline(pdf_path, mode="line_reconstructed")

    visual: dict = {"available": False}
    if document_id:
        with session_scope() as session:
            facts = list(session.scalars(
                select(FinancialFactRow).where(
                    FinancialFactRow.document_id == document_id)))
            tables = list(session.scalars(
                select(TableRow).where(TableRow.document_id == document_id)))
            if facts:
                visual = {
                    "available": True,
                    "facts": len(facts),
                    "distinct_metrics": len({f.canonical_metric for f in facts}),
                    "with_period": sum(1 for f in facts if f.period),
                    "with_unit": sum(1 for f in facts
                                     if f.unit not in (None, "none")),
                    "with_currency": sum(1 for f in facts
                                         if f.currency != "UNKNOWN"),
                    "with_growth": sum(1 for f in facts if f.growth is not None),
                    "with_entity": sum(1 for f in facts if f.entity),
                    "with_provenance_bbox": sum(1 for f in facts if f.cell_id
                                                or f.region_id),
                    "tables_reconstructed": len(tables),
                    "table_cells": sum(t.n_rows * t.n_cols for t in tables),
                    "validated": sum(1 for f in facts
                                     if f.validation_status == "PASSED"),
                }

    def summarise(r: BaselineResult) -> dict:
        return {
            "mode": r.mode,
            "facts": len(r.facts),
            "distinct_metrics": len({f["canonical_metric"] for f in r.facts}),
            "with_period": r.with_period,
            "with_unit": r.with_unit,
            "with_currency": sum(1 for f in r.facts if f["currency"] != "UNKNOWN"),
            "with_growth": 0,
            "with_entity": 0,
            "with_provenance_bbox": 0,
            "tables_reconstructed": 0,
            "table_cells": 0,
            "validated": 0,
            "pages": r.pages,
            "pages_without_text": r.pages_without_text,
            "numeric_tokens_seen": r.numeric_tokens,
            "numbers_discarded": r.numbers_discarded,
            "duration_ms": r.duration_ms,
        }

    naive_summary = summarise(naive)
    base_summary = {
        "facts": len(baseline.facts),
        "distinct_metrics": len({f["canonical_metric"] for f in baseline.facts}),
        "with_period": baseline.with_period,
        "with_unit": baseline.with_unit,
        "with_currency": sum(1 for f in baseline.facts
                             if f["currency"] != "UNKNOWN"),
        "with_growth": 0,
        "with_entity": 0,
        "with_provenance_bbox": 0,
        "tables_reconstructed": 0,
        "table_cells": 0,
        "validated": 0,
        "pages": baseline.pages,
        "pages_without_text": baseline.pages_without_text,
        "numeric_tokens_seen": baseline.numeric_tokens,
        "numbers_discarded": baseline.numbers_discarded,
        "duration_ms": baseline.duration_ms,
        "mode": baseline.mode,
    }

    findings: list[str] = []
    if visual.get("available"):
        v, b = visual, base_summary
        findings.append(
            f"Reading the PDF's text stream in its native order yields only "
            f"{naive_summary['facts']} usable fact(s): the stream emits spans "
            f"separately, so a row label and its figures are not adjacent. "
            f"Rebuilding lines from word coordinates -- already a geometric "
            f"step -- lifts that to {b['facts']}, which is the fair baseline "
            f"used below.")
        findings.append(
            f"The visual pipeline reconstructed {v['tables_reconstructed']} tables "
            f"({v['table_cells']} cells); the text baseline reconstructs none, "
            f"because a text stream carries no column geometry.")
        findings.append(
            f"Period attribution is where the two approaches diverge most. The "
            f"baseline labels every fact with whatever period the *page* "
            f"mentions, because it cannot tell which column a figure sits "
            f"under; on a table with four period columns that is right at best "
            f"one time in four. The visual pipeline resolves the period from "
            f"the column header above each figure, giving "
            f"{v['with_period']}/{v['facts']} facts a period that is tied to "
            f"the column the number actually came from.")
        findings.append(
            f"The baseline keeps only the first figure on each line, so it "
            f"discards {b['numbers_discarded']} of the "
            f"{b['numeric_tokens_seen']} numeric tokens it saw. Those discarded "
            f"numbers are the comparative periods -- exactly the data an "
            f"analyst wants.")
        findings.append(
            f"Units and currency: {v['with_unit']}/{v['facts']} visual facts "
            f"carry a magnitude and {v['with_currency']} a currency, against "
            f"{b['with_unit']} and {b['with_currency']} for the baseline, "
            f"because those are declared in a caption or a header cell that "
            f"only the table structure connects to the figure.")
        findings.append(
            f"Growth linkage: {v['with_growth']} visual facts carry a growth "
            f"figure tied to a comparison period; the baseline produces none, "
            f"as that requires knowing which columns are comparable.")
        findings.append(
            f"Provenance: {v['with_provenance_bbox']}/{v['facts']} visual facts "
            f"resolve to a bounding box on a rendered page; the text baseline "
            f"has no coordinates to offer.")
        findings.append(
            f"Validation: {v['validated']} visual facts passed an independent "
            f"arithmetic check, which is only possible once values are "
            f"correctly attributed to metric, entity and period.")

    findings.append(
        "On a scanned document the baseline returns nothing at all, while the "
        "visual pipeline is unaffected because it starts from pixels.")

    return {"pdf": str(pdf_path), "document_id": document_id,
            "baseline": base_summary,
            "baseline_naive": naive_summary,
            "visual": visual, "findings": findings}
