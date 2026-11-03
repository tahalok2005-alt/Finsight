"""Checking extracted facts against golden facts read from the source.

The golden files under ``data/golden`` are transcriptions of real statements,
made by looking at the rendered page. They are the ground truth; FinSight's own
output is never used to produce or update them. When the two disagree, the
golden file is right until the source proves otherwise.

What this module measures is deliberately *not* how many facts were extracted.
It measures how many of the facts that were extracted are correctly attributed,
and -- separately -- how many golden facts were missed. A run that produces
fewer facts but attributes them all correctly is a better run, and the report
these numbers feed has to be able to say so.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from app.core.logging import get_logger
from app.extraction.taxonomy import METRICS, match_metric

log = get_logger("evaluation.golden")

#: Values agree within this fraction. Tolerant enough for a printed rounding,
#: tight enough that a scale error can never pass. Used where no printed form
#: is known; a golden fact that knows how it was printed is compared at that
#: precision instead (see ``same_printed``).
_VALUE_TOLERANCE = 0.005

#: Every canonical key the taxonomy defines. A golden row's expected metric
#: must be one of these (or null: the taxonomy has no key for the line).
METRIC_KEYS = frozenset(m.key for m in METRICS)


@dataclass(frozen=True, slots=True)
class GoldenColumn:
    id: str
    period_end: str
    period_type: str
    group: str | None = None
    audit_status: str | None = None
    #: The first day of the span, where the column is a span. None for a
    #: point in time -- and None is the truth there, not a gap.
    period_start: str | None = None
    fiscal_label: str | None = None
    #: A column can carry its own basis (SBI prints standalone and
    #: consolidated side by side in one table) and its own unit and currency
    #: (Wipro translates one column into dollars). Each overrides the table.
    basis: str | None = None
    unit: str | None = None
    currency: str | None = None


@dataclass(frozen=True, slots=True)
class GoldenFact:
    """One cell of a source statement, as a person read it."""
    row: str
    column: GoldenColumn
    value: float
    unit: str | None = None
    currency: str | None = None
    entity: str | None = None
    value_type: str | None = None
    #: A balance printed inside a flow-headed table (share capital, NPAs,
    #: opening cash). Its column's span does not describe it, so duration is
    #: not held against it.
    stock: bool = False
    #: A subtotal the page prints on its own ruled line with no row name.
    unlabelled: bool = False
    row_index: int = 0
    #: The canonical key the printed label names, decided by a person reading
    #: the label in its statement. None: the taxonomy has no key for the line.
    metric: str | None = None
    #: Other keys that name the same measure (an NBFC's "Interest expenses"
    #: is finance costs or interest expended).
    metric_alt: tuple[str, ...] = ()
    #: Whether the golden file states the metric at all (older files may not).
    metric_stated: bool = False
    #: The row's vertical position as a fraction of the page height, where
    #: the golden file records one.
    y_rel: float | None = None
    #: Decimal places the figure is printed with, where known.
    decimals: int | None = None

    @property
    def describe(self) -> str:
        return f"{self.row} [{self.column.id}] = {self.value}"

    @property
    def metric_keys(self) -> frozenset[str]:
        return frozenset(k for k in (self.metric, *self.metric_alt) if k)


@dataclass
class GoldenTable:
    doc_key: str
    document: str
    page: int
    title: str
    basis: str | None
    nature: str
    unit: str | None
    currency: str | None
    columns: dict[str, GoldenColumn]
    facts: list[GoldenFact]
    must_never: list[str] = field(default_factory=list)
    source_path: Path | None = None
    #: The table's extent on the page as fractions of the page, [x0, y0, x1,
    #: y1]. Facts outside it are not scored against this table.
    bbox_rel: tuple[float, float, float, float] | None = None
    statement: str | None = None
    excluded_columns: list[dict] = field(default_factory=list)
    excluded_rows: list[dict] = field(default_factory=list)


def _column(raw: dict) -> GoldenColumn:
    return GoldenColumn(
        id=raw["id"], period_end=raw["period_end"],
        period_type=raw.get("period_type", ""), group=raw.get("group"),
        audit_status=raw.get("audit_status"),
        period_start=raw.get("period_start"),
        fiscal_label=raw.get("fiscal_label"), basis=raw.get("basis"),
        unit=raw.get("unit"), currency=raw.get("currency"))


def load_golden(path: Path) -> GoldenTable:
    """Read one golden file into the shape the checker works in."""
    raw = json.loads(path.read_text(encoding="utf-8"))
    table = raw["table"]
    columns = {c["id"]: _column(c) for c in table["columns"]}
    declaration = table.get("unit_declaration") or {}

    facts: list[GoldenFact] = []
    ordered = list(columns.values())
    height = table.get("page_height_pt")
    scale = {"pt": 1.0, "px200": 200.0 / 72.0}.get(table.get("y_units", ""), None)
    for row_index, row in enumerate(table.get("rows", [])):
        label = row["label"]
        y = row.get("y")
        y_rel = (float(y) / (height * scale)
                 if y is not None and height and scale else None)
        printed = row.get("printed") if isinstance(row.get("printed"), list) else None
        for index, value in enumerate(row.get("values", [])):
            # "nil" is a printed dash and null is a blank cell. Neither is a
            # fact, and a system that produced 0.0 for them would be inventing
            # a figure the statement does not make.
            if value is None or isinstance(value, str):
                continue
            if index >= len(ordered):
                continue
            column = ordered[index]
            # Row beats column beats table: a per-share row inside a dollar
            # column is still per-share, and still in dollars.
            unit = (row["unit"] if "unit" in row
                    else column.unit or declaration.get("unit"))
            currency = (row["currency"] if "currency" in row
                        else column.currency or declaration.get("currency"))
            text = (printed[index] if printed and index < len(printed)
                    and isinstance(printed[index], str) else None)
            facts.append(GoldenFact(
                row=label, column=column, value=float(value),
                unit=unit, currency=currency,
                entity=column.basis or table.get("basis"),
                value_type=row.get("value_type"),
                stock=bool(row.get("stock")),
                unlabelled=bool(row.get("unlabelled")),
                row_index=row_index,
                metric=row.get("metric"),
                metric_alt=tuple(row.get("metric_alt") or ()),
                metric_stated="metric" in row,
                y_rel=y_rel,
                decimals=_decimals(text, float(value))))

    return GoldenTable(
        doc_key=raw["doc_key"], document=raw["document"], page=raw["page"],
        title=table.get("title", ""), basis=table.get("basis"),
        nature=table.get("nature", ""), unit=declaration.get("unit"),
        currency=declaration.get("currency"), columns=columns, facts=facts,
        must_never=list((raw.get("acceptance") or {}).get("must_never", [])),
        source_path=path,
        bbox_rel=tuple(table["bbox_rel"]) if table.get("bbox_rel") else None,
        statement=table.get("statement"),
        excluded_columns=list(table.get("excluded_columns", [])),
        excluded_rows=list(table.get("excluded_rows", [])))


def load_all(directory: Path) -> list[GoldenTable]:
    return [load_golden(p) for p in sorted(directory.glob("*.json"))]


@dataclass
class Mismatch:
    kind: str
    golden: str
    detail: str


@dataclass
class GoldenReport:
    """What a run got right, and what it got wrong, against one golden table."""
    doc_key: str
    page: int
    golden_total: int = 0
    matched: int = 0
    period_correct: int = 0
    unit_correct: int = 0
    entity_correct: int = 0
    missing: list[str] = field(default_factory=list)
    mismatches: list[Mismatch] = field(default_factory=list)

    @property
    def wrong_period(self) -> int:
        return sum(1 for m in self.mismatches if m.kind == "period")

    @property
    def wrong_unit(self) -> int:
        return sum(1 for m in self.mismatches if m.kind == "unit")

    @property
    def wrong_entity(self) -> int:
        return sum(1 for m in self.mismatches if m.kind == "entity")

    def rate(self, correct: int) -> float | None:
        return round(correct / self.matched, 4) if self.matched else None

    def as_dict(self) -> dict:
        return {
            "doc_key": self.doc_key, "page": self.page,
            "golden_facts": self.golden_total,
            "matched": self.matched,
            "missing": len(self.missing),
            "period_correct": self.period_correct,
            "unit_correct": self.unit_correct,
            "entity_correct": self.entity_correct,
            "period_accuracy": self.rate(self.period_correct),
            "unit_accuracy": self.rate(self.unit_correct),
            "entity_accuracy": self.rate(self.entity_correct),
            "wrong_period": self.wrong_period,
            "wrong_unit": self.wrong_unit,
            "wrong_entity": self.wrong_entity,
            "mismatch_detail": [
                {"kind": m.kind, "golden": m.golden, "detail": m.detail}
                for m in self.mismatches[:40]],
            "missing_detail": self.missing[:40],
        }


def _normalise(label: str) -> str:
    keep = [c for c in label.lower() if c.isalnum() or c.isspace()]
    return " ".join("".join(keep).split())


def _same_value(a: float, b: float) -> bool:
    scale = max(abs(a), abs(b), 1.0)
    return abs(a - b) <= _VALUE_TOLERANCE * scale


def _decimals(printed: str | None, value: float) -> int:
    """Decimal places the figure was printed with: from the printed text where
    the golden file keeps it ("0.20" -> 2), else from the value itself."""
    if printed:
        digits = printed.strip().rstrip(")%").split(".")
        if len(digits) == 2 and digits[1][:1].isdigit():
            return len("".join(ch for ch in digits[1] if ch.isdigit()))
        return 0
    text = repr(float(value))
    if "e" in text:
        return 6
    frac = text.split(".")[1] if "." in text else ""
    return 0 if frac in ("", "0") else len(frac)


def same_printed(golden: "GoldenFact", value: float) -> bool:
    """Is ``value`` the figure the golden fact records, at the precision it was
    printed? 3.54 is not 3.55 -- a relative tolerance paired SAP's diluted EPS
    with its basic EPS -- and 21.29 is not 21.54."""
    d = golden.decimals if golden.decimals is not None else 2
    return abs(float(value) - golden.value) <= 0.5 * 10 ** (-d) + 1e-9


def check_against_golden(golden: GoldenTable, facts) -> GoldenReport:
    """Compare one run's facts with one golden table.

    Matching is by row label and value together. Value alone would pair a
    figure with whichever column happened to repeat it -- Groww's statement
    prints 0.20 in two different columns -- and label alone cannot tell four
    columns of one row apart.
    """
    report = GoldenReport(doc_key=golden.doc_key, page=golden.page,
                          golden_total=len(golden.facts))
    on_page = [f for f in facts
               if f.provenance and f.provenance.page_number == golden.page]

    used: set[int] = set()
    for want in golden.facts:
        wanted_label = _normalise(want.row)
        # The golden row is the label as the *page prints it*; the fact carries
        # the label as OCR read it. Comparing the two as strings scores a
        # perfectly extracted fact as missing whenever recognition mangled a
        # letter -- "Interest eamed" for "Interest earned" -- and that is a
        # recognition defect already counted elsewhere, not a coverage one.
        #
        # Both sides are therefore also mapped through the taxonomy, which is
        # what actually decides whether two labels name the same measure. The
        # value and the column still have to agree, so this cannot make a wrong
        # fact count as right.
        wanted_key = match_metric(want.row)
        wanted_key = wanted_key.key if wanted_key else None

        def _names_the_same(fact) -> bool:
            fact_label = _normalise(fact.metric or "")
            if (wanted_label.startswith(fact_label)
                    or fact_label.startswith(wanted_label[:18])
                    or _normalise(fact.original_text or "") == wanted_label):
                return True
            if wanted_key is None:
                return False
            canonical = getattr(fact, "canonical_metric", None)
            if canonical and canonical == wanted_key:
                return True
            hit = match_metric(fact.metric or "")
            return hit is not None and hit.key == wanted_key

        candidates = [
            (i, f) for i, f in enumerate(on_page)
            if i not in used
            and f.value is not None and _same_value(f.value, want.value)
            and _names_the_same(f)]
        if not candidates:
            report.missing.append(want.describe)
            continue

        # Prefer the candidate whose period already agrees, so a row appearing
        # in several columns pairs each value with its own column rather than
        # being scored against an arbitrary one.
        index, fact = next(
            ((i, f) for i, f in candidates
             if getattr(f.period_normalized, "end_date", None) == want.column.period_end),
            candidates[0])
        used.add(index)
        report.matched += 1

        end = getattr(fact.period_normalized, "end_date", None)
        if end == want.column.period_end:
            report.period_correct += 1
        else:
            report.mismatches.append(Mismatch(
                "period", want.describe,
                f"expected {want.column.period_end}, got {end or 'none'}"))

        if want.unit is None or str(fact.unit) == want.unit:
            report.unit_correct += 1
        elif want.unit == "per_share" and str(fact.value_type) == "per_share":
            # A per-share figure has no magnitude; the value type carries it.
            report.unit_correct += 1
        else:
            report.mismatches.append(Mismatch(
                "unit", want.describe,
                f"expected {want.unit}, got {fact.unit}"))

        if want.entity is None or (fact.entity or "") == want.entity:
            report.entity_correct += 1
        else:
            report.mismatches.append(Mismatch(
                "entity", want.describe,
                f"expected {want.entity}, got {fact.entity or 'none'}"))

    log.info("evaluation.golden", doc_key=golden.doc_key, page=golden.page,
             golden=report.golden_total, matched=report.matched,
             period_correct=report.period_correct)
    return report
