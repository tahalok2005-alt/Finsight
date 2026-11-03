"""Scoring a run against golden facts, one dimension at a time.

The Phase 20 checker answered one question -- was the figure found, and does
its end date agree? That is not enough to say a financial fact is right. A
figure can carry the right end date and the wrong duration (the quarter to 31
March and the year to 31 March), the right value and the wrong currency (a
rupee figure labelled dollars), or the right label and the wrong value (a
misread digit). Each of those is a wrong fact, and each must be counted.

So every golden fact ends in exactly one state:

    correct     -- found, and every dimension the source states agrees
    incomplete  -- found, nothing asserted wrongly, but some dimension was not
                   established (no period, no entity). Honest, not wrong.
    wrong       -- found, and at least one dimension is asserted wrongly
    missing     -- not found

reported in detail as CORRECT, AMBIGUOUS, MISSING or WRONG_<dimension> --
value, metric, period, duration, basis, entity, unit, currency -- never
collapsed into one "wrong" number. The metric is checked against the
canonical key the golden row states (Phase 22): the Phase 21 scorer compared
printed labels only, and so scored a correct number under the wrong measure
as correct.

Every extracted fact inside a golden table's region that does not pair with a
golden fact is examined too: it is a figure of one row filed under another
measure (wrong metric), a figure on a row that the row does not print (wrong
value), a repeat of a scored figure, or a figure the golden table does not
cover (unscored). Wrong extras are wrong facts and count in every total and
in the gate.

"Unknown" is never scored as wrong. The brief is explicit that UNKNOWN is
better than a plausible wrong answer, and a scorer that punished honesty
would push the system toward guessing.
"""
from __future__ import annotations

import json
import re
from collections import Counter
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path

from app.config.settings import settings
from app.evaluation.golden import (
    GoldenFact, GoldenTable, _normalise, _same_value, same_printed,
)
from app.extraction.taxonomy import match_metric

#: Facts whose provenance centre falls this far outside a golden table's box
#: still count as inside it. Boxes are drawn by hand around the printed table.
_REGION_MARGIN = 0.02


class Category(StrEnum):
    """The Phase 21 failure taxonomy, lettered as the brief lists it."""
    OCR = "A. OCR"
    TABLE_DETECTION = "B. table detection"
    FRAGMENTATION = "C. table fragmentation"
    COLUMN_GEOMETRY = "D. column geometry"
    HEADER = "E. header recognition"
    PERIOD = "F. period parsing"
    DURATION = "G. duration parsing"
    ENTITY = "H. entity/basis"
    UNIT = "I. unit/currency"
    NUMBER = "J. number parsing"
    TAXONOMY = "K. metric taxonomy"
    ROW = "L. row segmentation"
    STATEMENT = "M. statement classification"
    RECONCILIATION = "N. reconciliation"
    SAFETY_GATE = "O. safety gate"
    UI = "P. UI representation"
    AMBIGUOUS = "Q. genuinely ambiguous source"


# --------------------------------------------------------------------------- #
# dimension checks
# --------------------------------------------------------------------------- #
_FLOW_TYPES = {"quarter", "half_year", "nine_month", "fiscal_year",
               "calendar_year", "ttm"}


def _ptype(fact) -> str | None:
    p = getattr(fact, "period_normalized", None)
    if p is None:
        return None
    t = str(getattr(p, "period_type", "") or "")
    return None if t in ("", "unknown") else t


def _pend(fact) -> str | None:
    p = getattr(fact, "period_normalized", None)
    return getattr(p, "end_date", None) if p is not None else None


def _pstart(fact) -> str | None:
    p = getattr(fact, "period_normalized", None)
    return getattr(p, "start_date", None) if p is not None else None


def check_period(g: GoldenFact, fact) -> tuple[str, str]:
    """('ok'|'unknown'|'wrong', detail) for the period END."""
    end = _pend(fact)
    if not end:
        return "unknown", "no period end"
    if end == g.column.period_end:
        return "ok", end
    return "wrong", f"expected end {g.column.period_end}, got {end}"


def check_duration(g: GoldenFact, fact) -> tuple[str, str]:
    """('ok'|'unknown'|'wrong'|'n/a', detail) for what KIND of period it is.

    Only judged when the end date is right; a wrong end date is already a
    period error and double-counting it would hide which stage failed.
    """
    want = g.column.period_type or ""
    got = _ptype(fact)
    if g.stock:
        # A balance inside a flow-headed table: its column's span does not
        # describe it, and either reading is defensible.
        return "n/a", "stock row"
    if got is None:
        return "unknown", "no period type"
    if want == "point_in_time":
        if got == "point_in_time":
            return "ok", got
        return "wrong", f"a balance typed as a {got} flow"
    if got == "point_in_time":
        return "wrong", f"a {want} flow typed as a balance"
    equivalent = {want} | ({"calendar_year", "fiscal_year"}
                           if want in ("calendar_year", "fiscal_year") else set())
    start = _pstart(fact)
    if got in equivalent:
        if start and g.column.period_start and start != g.column.period_start:
            return "wrong", (f"{got} but starting {start}, "
                             f"expected {g.column.period_start}")
        return "ok", got
    return "wrong", f"expected {want}, got {got}"


def _entity_class(text: str | None) -> str | None:
    if not text:
        return None
    t = text.lower()
    if "standalone" in t or "stand-alone" in t:
        return "standalone"
    if "consolidated" in t or "group" in t:
        return "consolidated"
    return t.strip()


def check_entity(g: GoldenFact, fact) -> tuple[str, str]:
    if not g.entity:
        return "n/a", "basis not stated by the source"
    got = _entity_class(getattr(fact, "entity", None))
    # The filer named without a basis, or a basis the document did not state,
    # asserts nothing about standalone or consolidated.
    if got is None or str(getattr(fact, "entity_type", "")) in ("unknown", "company"):
        return "unknown", "no basis stated"
    want = _entity_class(g.entity)
    if got == want:
        return "ok", fact.entity
    if got in ("standalone", "consolidated"):
        return "wrong", f"basis: expected {g.entity}, got {fact.entity}"
    return "wrong", f"entity: expected {g.entity}, got {fact.entity}"


def check_metric(g: GoldenFact, fact) -> tuple[str, str]:
    """Is the figure filed under the measure its printed row names?

    A correct number under the wrong metric is a wrong fact: "Share-based
    payment reserve" filed as the period's share-based payment expense reads
    correctly and means something else. Only golden files that state the
    row's metric can be judged; a row the taxonomy has no key for (metric
    null) is named by no key, so any key a fact claims for it is wrong.
    """
    if not g.metric_stated:
        return "n/a", "golden row states no metric"
    got = getattr(fact, "canonical_metric", None)
    if got and got in g.metric_keys:
        return "ok", got
    want = g.metric or "no canonical key"
    return "wrong", f"expected {want}, got {got}"


def check_unit(g: GoldenFact, fact) -> tuple[str, str]:
    want = (g.unit or "").lower()
    vt = str(getattr(fact, "value_type", "") or "")
    unit = str(getattr(fact, "unit", "") or "")
    if not want:
        return "n/a", "no unit stated"
    if want == "per_share":
        if vt == "per_share":
            return "ok", "per_share"
        if unit in ("none", "unit", "") and vt in ("unknown", ""):
            return "unknown", "no value type"
        return "wrong", f"a per-share figure read as {vt}/{unit}"
    if want == "percent":
        if vt in ("percentage",) or unit == "percent":
            return "ok", "percent"
        if vt in ("unknown", "") and unit in ("none", ""):
            return "unknown", "no value type"
        return "wrong", f"a percentage read as {vt}/{unit}"
    if want == "count":
        if vt == "count":
            return "ok", "count"
        if vt in ("unknown", "") and unit in ("none", "", "unit"):
            return "unknown", "no value type"
        return "wrong", f"a count read as {vt}/{unit}"
    if want == "ratio":
        # A multiple ("1.09" times): no magnitude, no percent sign.
        if vt == "ratio" and unit in ("none", "unit", ""):
            return "ok", "ratio"
        if vt in ("unknown", "") and unit in ("none", "", "unit"):
            return "unknown", "no value type"
        return "wrong", f"a ratio read as {vt}/{unit}"
    # a magnitude: crore / million / lakh / billion
    if vt in ("per_share", "percentage", "count", "ratio", "bps"):
        return "wrong", f"a {want} amount read as {vt}"
    if unit in ("none", "unit", ""):
        return "unknown", "no magnitude"
    if unit == want:
        return "ok", unit
    return "wrong", f"expected {want}, got {unit}"


def check_currency(g: GoldenFact, fact) -> tuple[str, str]:
    want = g.currency
    if not want:
        return "n/a", "no currency stated"
    got = str(getattr(fact, "currency", "") or "")
    if got in ("", "UNKNOWN"):
        return "unknown", "no currency"
    if got == want:
        return "ok", got
    return "wrong", f"expected {want}, got {got}"


# --------------------------------------------------------------------------- #
# matching
# --------------------------------------------------------------------------- #
_SECTION_QUALIFIER = re.compile(
    r"\s*\((?:non-current|current)[^)]*\)\s*$|\s*\((?:total of the attribution)\)",
    re.I)


def _golden_key(label: str) -> str | None:
    hit = match_metric(_SECTION_QUALIFIER.sub("", label))
    return hit.key if hit else None


def names_match(g_label: str, fact, *, strict: bool = False) -> bool:
    """Does this fact name the golden row? Loose: prefix or taxonomy. Strict
    (used only to call a DIFFERENT value wrong): same taxonomy key or the same
    normalised label."""
    wanted = _normalise(_SECTION_QUALIFIER.sub("", g_label))
    fact_label = _normalise(getattr(fact, "metric", "") or "")
    wanted_key = _golden_key(g_label)
    canonical = getattr(fact, "canonical_metric", None)
    if strict:
        if fact_label and fact_label == wanted:
            return True
        return bool(wanted_key and canonical and canonical == wanted_key)
    if fact_label and (wanted.startswith(fact_label)
                       or fact_label.startswith(wanted[:18])):
        return True
    prov = getattr(fact, "provenance", None)
    if prov is not None and _normalise(prov.original_text or "") == wanted:
        return True
    if wanted_key is None:
        return False
    if canonical and canonical == wanted_key:
        return True
    hit = match_metric(getattr(fact, "metric", "") or "")
    return hit is not None and hit.key == wanted_key


def in_region(fact, table: GoldenTable) -> bool:
    prov = getattr(fact, "provenance", None)
    if prov is None or prov.page_number != table.page:
        return False
    if table.bbox_rel is None:
        return True
    rel = prov.bbox_relative
    if rel is None:
        return True
    cx = rel.x + rel.width / 2
    cy = rel.y + rel.height / 2
    x0, y0, x1, y1 = table.bbox_rel
    m = _REGION_MARGIN
    return (x0 - m) <= cx <= (x1 + m) and (y0 - m) <= cy <= (y1 + m)


# --------------------------------------------------------------------------- #
# results
# --------------------------------------------------------------------------- #
DIMENSIONS = ("metric", "period", "duration", "entity", "unit", "currency")

#: The order a wrong fact is named by when several dimensions are wrong: a
#: misread figure first (every other dimension is then about the wrong
#: number), then the measure, then when, then whose, then how much.
_PRIORITY = ("value", "metric", "period", "duration", "basis", "entity", "unit",
             "currency")
#: Statuses reported separately (section 2 of the Phase 22 brief).
STATUSES = ("CORRECT", "AMBIGUOUS", "MISSING", *(f"WRONG_{d.upper()}" for d in _PRIORITY))
#: Dimensions whose unflagged errors fail the build.
GATED = ("metric", "value", "period", "duration", "basis", "entity", "unit", "currency")


@dataclass
class Verdict:
    golden: GoldenFact
    status: str                     # correct | incomplete | wrong | missing
    fact_id: str | None = None
    dims: dict[str, str] = field(default_factory=dict)       # dim -> ok/unknown/wrong/n/a
    details: dict[str, str] = field(default_factory=dict)
    wrong_value: float | None = None
    review: bool = False
    category: Category | None = None
    cause: str = ""
    #: How the fact was paired: label, position, key, or position+label.
    paired_by: str = ""

    @property
    def wrong_dims(self) -> list[str]:
        """Every dimension asserted wrongly, entity split into basis/entity."""
        out = []
        if self.wrong_value is not None:
            out.append("value")
        for d in ("metric", "period", "duration"):
            if self.dims.get(d) == "wrong":
                out.append(d)
        if self.dims.get("entity") == "wrong":
            out.append("basis" if self.details.get("entity", "").startswith("basis")
                       else "entity")
        for d in ("unit", "currency"):
            if self.dims.get(d) == "wrong":
                out.append(d)
        return out

    @property
    def status_detail(self) -> str:
        if self.status == "missing":
            return "MISSING"
        wrong = self.wrong_dims
        if wrong:
            return "WRONG_" + min(wrong, key=_PRIORITY.index).upper()
        if self.status == "incomplete":
            return "AMBIGUOUS"
        return "CORRECT"

    def as_dict(self) -> dict:
        g = self.golden
        return {
            "row": g.row, "column": g.column.id, "value": g.value,
            "metric": g.metric, "period_end": g.column.period_end,
            "period_type": g.column.period_type,
            "entity": g.entity, "unit": g.unit, "currency": g.currency,
            "status": self.status, "status_detail": self.status_detail,
            "wrong_dims": self.wrong_dims, "fact_id": self.fact_id,
            "dims": self.dims, "details": self.details,
            "wrong_value": self.wrong_value, "review": self.review,
            "paired_by": self.paired_by,
            "category": str(self.category) if self.category else None,
            "cause": self.cause}


@dataclass
class Extra:
    fact_id: str
    metric: str
    value: float | None
    period_end: str | None
    kind: str                      # wrong_metric | wrong_value | duplicate | unscored
    detail: str
    category: Category | None = None
    review: bool = False
    canonical: str | None = None

    @property
    def wrong_dims(self) -> list[str]:
        return {"wrong_value": ["value"], "wrong_metric": ["metric"]}.get(self.kind, [])

    def as_dict(self) -> dict:
        return {"fact_id": self.fact_id, "metric": self.metric,
                "canonical": self.canonical, "value": self.value,
                "period_end": self.period_end, "kind": self.kind,
                "detail": self.detail, "review": self.review,
                "category": str(self.category) if self.category else None}


@dataclass
class TableScore:
    doc_key: str
    page: int
    file: str
    title: str
    statement: str | None
    verdicts: list[Verdict] = field(default_factory=list)
    extras: list[Extra] = field(default_factory=list)
    #: Whether golden row positions could be used to pair facts, and why not.
    positional: str = ""

    # ---- counts -------------------------------------------------------------
    def count(self, status: str) -> int:
        return sum(1 for v in self.verdicts if v.status == status)

    @property
    def golden_total(self) -> int:
        return len(self.verdicts)

    @property
    def found(self) -> int:
        return sum(1 for v in self.verdicts if v.status in ("correct", "incomplete", "wrong"))

    @property
    def wrong_metric(self) -> int:
        return sum(1 for e in self.extras if e.kind == "wrong_metric")

    @property
    def wrong_value_extra(self) -> int:
        return sum(1 for e in self.extras if e.kind == "wrong_value")

    def status_counts(self) -> Counter:
        return Counter(v.status_detail for v in self.verdicts)

    def wrong_facts(self) -> list[tuple[list[str], bool]]:
        """Every wrong fact -- golden verdicts and wrong extras alike -- as
        (wrong dimensions, flagged for review)."""
        out = [(v.wrong_dims, v.review) for v in self.verdicts if v.wrong_dims]
        out += [(e.wrong_dims, e.review) for e in self.extras if e.wrong_dims]
        return out

    def unflagged_wrong(self) -> Counter:
        c = Counter()
        for dims, review in self.wrong_facts():
            if not review:
                c.update(dims)
        return c

    def dim_counts(self) -> dict[str, Counter]:
        out = {d: Counter() for d in DIMENSIONS}
        out["value"] = Counter()
        for v in self.verdicts:
            if v.status == "missing":
                continue
            for d in DIMENSIONS:
                out[d][v.dims.get(d, "n/a")] += 1
            out["value"]["wrong" if v.wrong_value is not None else "ok"] += 1
        return out

    def as_dict(self) -> dict:
        dims = {d: dict(c) for d, c in self.dim_counts().items()}
        wrong = self.wrong_facts()
        return {
            "doc_key": self.doc_key, "page": self.page, "file": self.file,
            "title": self.title, "statement": self.statement,
            "golden": self.golden_total, "found": self.found,
            "correct": self.count("correct"), "incomplete": self.count("incomplete"),
            "wrong": self.count("wrong"), "missing": self.count("missing"),
            "wrong_metric": self.wrong_metric,
            "wrong_value_extra": self.wrong_value_extra,
            "wrong_unflagged": sum(1 for v in self.verdicts
                                   if v.status == "wrong" and not v.review),
            "wrong_facts": len(wrong),
            "wrong_facts_unflagged": sum(1 for _, r in wrong if not r),
            "unflagged_by_dim": dict(self.unflagged_wrong()),
            "status_detail": dict(self.status_counts()),
            "review": sum(1 for v in self.verdicts if v.review),
            "positional": self.positional,
            "dims": dims,
            "categories": dict(Counter(str(v.category) for v in self.verdicts
                                       if v.category is not None)),
            "verdicts": [v.as_dict() for v in self.verdicts],
            "extras": [e.as_dict() for e in self.extras],
        }


# --------------------------------------------------------------------------- #
# scoring
# --------------------------------------------------------------------------- #
def _judge(g: GoldenFact, fact) -> Verdict:
    v = Verdict(golden=g, status="correct", fact_id=getattr(fact, "fact_id", None),
                review=bool(getattr(fact, "requires_review", False)))
    for name, check in (("metric", check_metric), ("period", check_period),
                        ("entity", check_entity), ("unit", check_unit),
                        ("currency", check_currency)):
        state, detail = check(g, fact)
        v.dims[name], v.details[name] = state, detail
    if v.dims["period"] == "ok":
        state, detail = check_duration(g, fact)
    else:
        state, detail = "n/a", "period end not established"
    v.dims["duration"], v.details["duration"] = state, detail
    if any(s == "wrong" for s in v.dims.values()):
        v.status = "wrong"
    elif any(s == "unknown" for s in v.dims.values()):
        v.status = "incomplete"
    return v


def _fact_y(fact) -> float | None:
    prov = getattr(fact, "provenance", None)
    rel = getattr(prov, "bbox_relative", None) if prov is not None else None
    if rel is None:
        return None
    return rel.y + rel.height / 2


class _Rows:
    """Where each golden row sits on the page, when that can be trusted.

    Golden files transcribed from a native text layer record each row's
    vertical position, and a fact whose box sits on that line belongs to
    that row whatever its label reads -- "16Minority Interest" is the
    minority-interest row. Rows transcribed by eye from an image carry
    approximate positions, so a table's positions are used only after they
    are checked: the offset between golden rows and the facts paired with
    them by label and value must be consistent to within a fraction of the
    row pitch. Otherwise pairing falls back to labels alone.
    """

    def __init__(self, golden: GoldenTable, region):
        self.offset: float | None = None
        self.reason = "no row positions"
        ys = sorted({f.y_rel for f in golden.facts if f.y_rel is not None})
        if len(ys) < 3:
            return
        gaps = [b - a for a, b in zip(ys, ys[1:]) if b - a > 1e-4]
        self.pitch = sorted(gaps)[len(gaps) // 2] if gaps else None
        if not self.pitch:
            return
        self.ys = ys
        offsets = []
        for g in golden.facts:
            if g.y_rel is None:
                continue
            for f in region:
                fy = _fact_y(f)
                if (fy is not None and f.value is not None and same_printed(g, f.value)
                        and names_match(g.row, f, strict=True)):
                    offsets.append(fy - g.y_rel)
        if len(offsets) < 5:
            self.reason = f"only {len(offsets)} label pairs to calibrate positions"
            return
        offsets.sort()
        lo, hi = offsets[len(offsets) // 10], offsets[(9 * len(offsets)) // 10]
        if hi - lo > 0.35 * self.pitch:
            self.reason = (f"row positions inconsistent (spread {hi - lo:.4f} "
                           f"against pitch {self.pitch:.4f})")
            return
        self.offset = offsets[len(offsets) // 2]
        self.reason = "used"

    def on_row(self, g: GoldenFact, fact) -> bool:
        if self.offset is None or g.y_rel is None:
            return False
        fy = _fact_y(fact)
        if fy is None:
            return False
        # Half the distance to the nearest other row, so neighbouring rows
        # never share a fact.
        i = self.ys.index(g.y_rel)
        near = [abs(self.ys[j] - g.y_rel) for j in (i - 1, i + 1) if 0 <= j < len(self.ys)]
        tol = 0.45 * min(near) if near else 0.45 * self.pitch
        return abs(fy - self.offset - g.y_rel) <= tol

    def row_of(self, fact, golden: GoldenTable) -> str | None:
        hits = {g.row for g in golden.facts if self.on_row(g, fact)}
        return next(iter(hits)) if len(hits) == 1 else None


def score_table(golden: GoldenTable, facts) -> TableScore:
    score = TableScore(doc_key=golden.doc_key, page=golden.page,
                       file=golden.source_path.name if golden.source_path else "",
                       title=golden.title, statement=golden.statement)
    region = [f for f in facts if in_region(f, golden)]
    rows = _Rows(golden, region)
    score.positional = rows.reason
    used: set[int] = set()
    pending: list[GoldenFact] = []

    def row_evidence(g, f) -> str:
        """Why this fact can be said to sit on this golden row, or ''."""
        by_pos = rows.on_row(g, f)
        by_label = names_match(g.row, f)
        if by_pos and by_label:
            return "position+label"
        if by_pos:
            return "position"
        if by_label:
            return "label"
        if getattr(f, "canonical_metric", None) in g.metric_keys:
            return "key"
        return ""

    # 1. value + row, as an ASSIGNMENT ----------------------------------------
    # The same figure often prints in several columns (share capital in six of
    # SBI's eight). Pairing greedily lets one column's golden fact take the
    # fact that belongs to another, and then reports a period error that is
    # the scorer's, not the system's. So pairs are made in passes, strictest
    # first: same period and basis, then same period, then anything.
    def _tier(g, f) -> int:
        same_end = _pend(f) == g.column.period_end
        same_type = _ptype(f) == g.column.period_type
        same_basis = (not g.entity or
                      _entity_class(getattr(f, "entity", None)) == _entity_class(g.entity))
        if same_end and same_type and same_basis:
            return 0
        if same_end and same_basis:
            return 1
        if same_end:
            return 2
        return 3

    pairs: dict[int, tuple[int, object, str]] = {}
    for tier in (0, 1, 2, 3):
        for gi, g in enumerate(golden.facts):
            if gi in pairs:
                continue
            for i, f in enumerate(region):
                if (i in used or f.value is None or not same_printed(g, f.value)
                        or _tier(g, f) != tier):
                    continue
                how = row_evidence(g, f)
                if not how:
                    continue
                used.add(i)
                pairs[gi] = (i, f, how)
                break
    for gi, g in enumerate(golden.facts):
        if gi in pairs:
            v = _judge(g, pairs[gi][1])
            v.paired_by = pairs[gi][2]
            score.verdicts.append(v)
        else:
            pending.append(g)

    # 2. a figure on this row whose value is none of the row's printed
    #    figures: misread, misparsed, or two columns run together. It is a
    #    wrong fact whatever period it carries -- the concatenation of two
    #    columns usually carries none, and that is exactly the case that must
    #    not slip past as an unscored extra.
    row_values: dict[str, list[GoldenFact]] = {}
    for o in golden.facts:
        row_values.setdefault(o.row, []).append(o)
    for g in pending:
        cands = [(i, f) for i, f in enumerate(region)
                 if i not in used and f.value is not None
                 and (rows.on_row(g, f) or names_match(g.row, f, strict=True))
                 and not any(same_printed(o, f.value) for o in row_values[g.row])
                 and _pend(f) in (g.column.period_end, None)]
        cands.sort(key=lambda item: 0 if _pend(item[1]) == g.column.period_end else 1)
        if cands:
            i, f = cands[0]
            used.add(i)
            v = _judge(g, f)
            v.status = "wrong"
            v.wrong_value = f.value
            v.details["value"] = f"expected {g.value}, got {f.value}"
            v.paired_by = "position" if rows.on_row(g, f) else "label"
            score.verdicts.append(v)
        else:
            score.verdicts.append(Verdict(golden=g, status="missing"))

    # 3. what is left inside the region -------------------------------------
    for i, f in enumerate(region):
        if i in used or f.value is None:
            continue
        review = bool(getattr(f, "requires_review", False))
        canonical = getattr(f, "canonical_metric", None)
        on = [g for g in golden.facts if rows.on_row(g, f)]
        named = on or [g for g in golden.facts if names_match(g.row, f, strict=True)]
        same_value = [g for g in golden.facts if same_printed(g, f.value)]
        same_dated = [g for g in same_value if g.column.period_end == _pend(f)]
        base = dict(fact_id=f.fact_id, metric=f.metric, value=f.value,
                    period_end=_pend(f), review=review, canonical=canonical)
        if named and not any(same_printed(o, f.value) for o in named):
            score.extras.append(Extra(
                **base, kind="wrong_value",
                detail=(f"on golden row {named[0].row!r} but {f.value} is none of "
                        f"that row's printed figures"),
                category=_concatenation_or_misread(f, named)))
            continue
        if named:
            # A second reading of a figure the row does print: a repeat of the
            # same cell, or the same figure printed twice. Wrong only if it
            # names another measure.
            keys = set().union(*(g.metric_keys for g in named))
            if named[0].metric_stated and canonical not in keys:
                score.extras.append(Extra(
                    **base, kind="wrong_metric",
                    detail=(f"a figure of golden row {named[0].row!r} filed under "
                            f"{canonical}"), category=Category.TAXONOMY))
            else:
                score.extras.append(Extra(**base, kind="duplicate",
                                          detail="a repeat of a scored figure"))
            continue
        # An undated figure that happens to equal another row's figure says
        # nothing about which row it came from (Eternal's share-based payment
        # add-back, 210, equals a securities line's 210); it is that row's only
        # if its own label is part of the row's printed label.
        own = _normalise(getattr(f, "metric", "") or "")
        other = same_dated or ([g for g in same_value
                                if own and own in _normalise(g.row)]
                               if _pend(f) is None else [])
        if other and not any(canonical in g.metric_keys for g in other):
            score.extras.append(Extra(
                **base, kind="wrong_metric",
                detail=(f"value belongs to golden row {other[0].row!r} "
                        f"but the fact names {f.metric!r} ({canonical})"),
                category=Category.ROW))
        else:
            score.extras.append(Extra(**base, kind="unscored",
                                      detail="no golden fact covers this figure"))
    return score


# --------------------------------------------------------------------------- #
# stored OCR evidence, for telling "never read" from "read but not tabulated"
# --------------------------------------------------------------------------- #
def ocr_texts(content_hash: str, page: int) -> list[str]:
    d = settings.cache_dir.resolve() / "ocr" / content_hash
    hits = sorted(d.glob(f"v1-p{page:04d}-*.json")) if d.exists() else []
    if not hits:
        return []
    raw = json.loads(hits[0].read_text(encoding="utf-8"))
    return [e.get("text", "") for e in raw.get("elements", [])]


def _digits(value: float) -> str:
    """The significant digits a printed figure must contain, however it was
    grouped: 137963.03 -> '13796303', 594457 -> '594457'."""
    s = f"{abs(value):.2f}"
    if s.endswith(".00"):
        s = s[:-3]
    return s.replace(".", "")


def _text_digits(text: str) -> str:
    return re.sub(r"[^0-9]", "", text or "")


# --------------------------------------------------------------------------- #
# classification: which stage lost or broke each fact
# --------------------------------------------------------------------------- #
def _bbox_rel_of(table) -> tuple[float, float, float, float] | None:
    meta = getattr(table, "meta", None) or {}
    rel = meta.get("bbox_rel")
    if rel:
        return tuple(rel)
    return None


def _cells_with_value(tables, page: int, value: float):
    for t in tables:
        if t.page_number != page:
            continue
        for c in t.cells:
            nv = getattr(c, "numeric_value", None)
            if nv is not None and _same_value(nv, value):
                yield t, c


def _row_label(table, cell) -> str:
    parts = [c.text for c in table.cells
             if c.row == cell.row and c.col < cell.col and c.text.strip()
             and not getattr(c, "is_numeric", False)]
    return " ".join(parts).strip()


def classify(verdict: Verdict, golden: GoldenTable, *, tables, facts,
             ocr: list[str]) -> None:
    """Set verdict.category and verdict.cause. Reads stored evidence only."""
    g = verdict.golden
    page = golden.page

    if verdict.status == "wrong":
        d = verdict.dims
        if verdict.wrong_value is not None:
            fact = next((f for f in facts if f.fact_id == verdict.fact_id), None)
            raw = _text_digits(getattr(fact, "raw_value", "") or "")
            if raw and raw == _digits(g.value):
                verdict.category, verdict.cause = Category.NUMBER, (
                    f"printed digits read correctly but parsed as {verdict.wrong_value}")
            else:
                verdict.category, verdict.cause = Category.OCR, (
                    f"digits misread: printed {g.value}, read "
                    f"{getattr(fact, 'raw_value', '')!r}")
            return
        if d.get("metric") == "wrong":
            fact = next((f for f in facts if f.fact_id == verdict.fact_id), None)
            label = getattr(fact, "metric", "") or ""
            verdict.category = (Category.TAXONOMY if names_match(g.row, fact)
                                else Category.ROW) if fact else Category.TAXONOMY
            verdict.cause = (f"filed under {getattr(fact, 'canonical_metric', None)} "
                             f"as {label!r}: {verdict.details['metric']}")
            return
        if d.get("period") == "wrong":
            fact_end = verdict.details["period"].rsplit(" ", 1)[-1]
            other = [c for c in golden.columns.values()
                     if c.period_end == fact_end and c.id != g.column.id]
            if other:
                verdict.category, verdict.cause = Category.COLUMN_GEOMETRY, (
                    f"figure placed in column {other[0].id}'s period ({fact_end})")
            else:
                verdict.category, verdict.cause = Category.PERIOD, verdict.details["period"]
            return
        if d.get("duration") == "wrong":
            verdict.category, verdict.cause = Category.DURATION, verdict.details["duration"]
            return
        if d.get("entity") == "wrong":
            verdict.category, verdict.cause = Category.ENTITY, verdict.details["entity"]
            return
        if d.get("unit") == "wrong" or d.get("currency") == "wrong":
            which = "unit" if d.get("unit") == "wrong" else "currency"
            verdict.category, verdict.cause = Category.UNIT, verdict.details[which]
            return
        return

    if verdict.status == "incomplete":
        d = verdict.dims
        fact = next((f for f in facts if f.fact_id == verdict.fact_id), None)
        if d.get("period") == "unknown":
            gate = (getattr(fact, "meta", None) or {}).get("safety_gate") or {}
            verdict.category = Category.HEADER
            verdict.cause = ("no period reached the figure: the column head was not "
                             "read as a period" + (" (safety gate held it)"
                                                    if gate and not gate.get("passed", True)
                                                    else ""))
        elif d.get("duration") == "unknown":
            verdict.category, verdict.cause = Category.DURATION, "end date known, span not"
        elif d.get("entity") == "unknown":
            verdict.category, verdict.cause = Category.ENTITY, "no entity attributed"
        else:
            verdict.category, verdict.cause = Category.UNIT, "unit or currency not established"
        return

    if verdict.status != "missing":
        return

    # ---- missing: walk the pipeline in order ------------------------------
    digits = _digits(g.value)
    if len(digits) >= 3 and ocr and not any(digits in _text_digits(t) for t in ocr):
        verdict.category, verdict.cause = Category.OCR, (
            f"the digits {digits} do not occur in any OCR element on page {page}")
        return

    page_tables = [t for t in tables if t.page_number == page]
    if not page_tables:
        verdict.category, verdict.cause = Category.TABLE_DETECTION, (
            f"no table was reconstructed on page {page}")
        return

    hits = list(_cells_with_value(page_tables, page, g.value))
    if not hits:
        raw_hits = [(t, c) for t in page_tables for c in t.cells
                    if c.text and len(digits) >= 3 and digits in _text_digits(c.text)]
        if raw_hits:
            t, c = raw_hits[0]
            from app.normalization.numbers import holds_more_than_one_number
            if holds_more_than_one_number(c.text):
                verdict.category, verdict.cause = Category.COLUMN_GEOMETRY, (
                    f"cell {c.text!r} holds more than one figure: two columns "
                    f"merged into one cell")
            else:
                verdict.category, verdict.cause = Category.NUMBER, (
                    f"cell {c.text!r} holds the digits but parsed to "
                    f"{getattr(c, 'numeric_value', None)}")
            return
        verdict.category, verdict.cause = Category.ROW, (
            "OCR read the figure but it never became a table cell")
        return

    table, cell = hits[0]
    in_golden = [t for t in page_tables if t is table]
    label = _row_label(table, cell)
    if not label:
        verdict.category, verdict.cause = Category.ROW, (
            f"row {cell.row} holds the figure but no label was assembled for it")
        return
    if match_metric(label) is None:
        if _golden_key(g.row) is not None:
            verdict.category, verdict.cause = Category.OCR, (
                f"label read as {label!r}; the printed label is known to the "
                f"taxonomy -- recognition damaged it")
        else:
            verdict.category, verdict.cause = Category.TAXONOMY, (
                f"no taxonomy entry for {label!r}")
        return

    produced = [f for f in facts
                if f.provenance and f.provenance.page_number == page
                and f.value is not None and _same_value(f.value, g.value)]
    if not produced:
        verdict.category, verdict.cause = Category.SAFETY_GATE, (
            f"row {label!r} is known and the cell holds the figure, but no fact "
            f"was emitted from it")
        return
    fact = produced[0]
    if not in_region(fact, golden):
        verdict.category, verdict.cause = Category.FRAGMENTATION, (
            f"the fact exists but its provenance lies outside the table's region")
        return
    other_rows = [o for o in golden.facts if o.row != g.row and names_match(o.row, fact)]
    if other_rows:
        verdict.category, verdict.cause = Category.ROW, (
            f"the figure was emitted under {fact.metric!r}, another row's name")
        return
    verdict.category, verdict.cause = Category.TAXONOMY, (
        f"the figure was emitted as {fact.metric!r} ({fact.canonical_metric}), "
        f"which does not name {g.row!r}")


def _concatenation_or_misread(fact, named: list[GoldenFact]) -> Category:
    """A wrong value built from two of the row's printed figures is two columns
    read as one (D); anything else is a misread or misparsed figure."""
    digits = _text_digits(getattr(fact, "raw_value", "") or "")
    vals = [_digits(g.value) for g in named]
    for a in vals:
        for b in vals:
            if a != b and digits in (a + b, b + a):
                return Category.COLUMN_GEOMETRY
    return Category.NUMBER
