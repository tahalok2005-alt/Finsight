"""Qualified facts: one measure, entity and period, stated on a named basis.

A metric and a period do not always name one value. A results release prints
last year's quarter "excluding" a one-off gain on its first page and the
statutory figure, including it, in its financial statements; a UK bank prints
"Reported" and "Adjusted" columns side by side; a restated comparative sits
beside the figure first published. Both figures are true, and each is a
different fact. Treated as one metric they were a conflict with no answer,
flagged for review on both sides, and a benchmark picked between them on a tie
of confidence scores.

So a figure carries a qualification where -- and only where -- the source
states one that makes it a different figure:

* a column head that names a basis ("Adjusted", "Reported", "Restated",
  "Continuing operations");
* a footnote that names a period and a measure and says how it is stated
  ("1Q FY26 EBITDA excludes 8,924 crore ... profit from sale of listed
  investments");
* a footnote marker on a row label and its note ("Capital Expenditure#" /
  "# Excluding amount incurred towards spectrum").

A figure the page COMPUTES from a qualified one carries it too, but only when
the page's own arithmetic shows it in that column: profit before tax is
EBITDA less depreciation and finance costs to the rupee, so it is on the
same basis. Nothing is qualified on a similarity of words, and prose is not
modelled.
"""
from __future__ import annotations

import re
from collections import defaultdict
from typing import Iterable

from app.extraction.taxonomy import match_metric
from app.models.enums import QualificationType as Q
from app.models.schemas import FinancialFact, Qualification
from app.normalization.periods import FiscalCalendar, parse_period

#: Words a column head uses to state a basis, strongest first.
_COLUMN_BASES: list[tuple[re.Pattern[str], Q]] = [
    (re.compile(r"\brestated\b", re.I), Q.RESTATED),
    (re.compile(r"\bpro[\s-]?forma\b", re.I), Q.PRO_FORMA),
    (re.compile(r"\blike[\s-]for[\s-]like\b|\bconstant\s+currency\b", re.I), Q.LIKE_FOR_LIKE),
    (re.compile(r"\bdiscontinued\s+operations?\b", re.I), Q.DISCONTINUED_OPERATIONS),
    (re.compile(r"\bcontinuing\s+operations?\b", re.I), Q.CONTINUING_OPERATIONS),
    (re.compile(r"\bbefore\s+exceptional", re.I), Q.BEFORE_EXCEPTIONAL),
    (re.compile(r"\bafter\s+exceptional", re.I), Q.AFTER_EXCEPTIONAL),
    (re.compile(r"\badjusted[1-9il¹²³]?\b", re.I), Q.ADJUSTED),
    (re.compile(r"\bunderlying\b", re.I), Q.UNDERLYING),
    (re.compile(r"\breported[1-9il¹²³]?\b|\bstatutory\b", re.I), Q.REPORTED),
]


def from_column_head(header: str | None) -> Qualification | None:
    """The basis a column head states, if it states one."""
    text = (header or "").strip()
    for pattern, kind in _COLUMN_BASES:
        m = pattern.search(text)
        if m:
            return Qualification(
                qualification_type=kind, qualification_label=m.group(0).strip(),
                qualification_source="column_head", source_text=text[:160])
    return None


#: A footnote stating how one period's measure is stated: "1Q FY26 EBITDA
#: excludes 8,924 crore being proceeds of profit from sale of listed
#: investments for comparison".
_PERIOD_NOTE = re.compile(
    r"^\s*(?P<marker>[*#^†‡§]|\d{1,2}(?=\s*[A-Z]))?\s*"
    r"(?P<period>(?:[1-4]\s*Q|Q\s*[1-4]|H\s*[12]|9\s*M)\s*'?\s*FY\s*'?\s*\d{2}(?:\d{2})?"
    r"|FY\s*'?\s*\d{2}(?:\d{2})?|(?:Q[1-4]|H[12])\s*(?:19|20)\d{2})\s*"
    r"(?P<measure>[A-Za-z][A-Za-z &/(),.-]{1,60}?)\s*"
    # No word boundary after the verb: a degraded scan loses every space --
    # "1QFY26EBITDAexcludes8.924crore...".
    r"(?P<verb>excludes|excluding|includes|including|is\s*restated|restated|"
    r"is\s*adjusted\s*for|adjusted\s*for)\s*(?P<rest>.*)$", re.I)
#: A footnote that begins with a marker a row label carries: "# Excluding
#: amount incurred towards spectrum".
_MARKER_NOTE = re.compile(
    r"^\s*(?P<marker>[*#^†‡§])\s*(?P<text>(?:excluding|excludes|including|includes|"
    r"adjusted|restated|before\s*exceptional|after\s*exceptional).*)$", re.I)
_VERB_TYPE = {"exclud": Q.EXCLUDING, "includ": Q.INCLUDING, "restat": Q.RESTATED,
              "adjust": Q.ADJUSTED, "before": Q.BEFORE_EXCEPTIONAL,
              "after": Q.AFTER_EXCEPTIONAL}


def _type_of(verb: str) -> Q:
    v = verb.lower().replace("is ", "").strip()
    return next((t for stem, t in _VERB_TYPE.items() if v.startswith(stem)), Q.OTHER)


#: What a page computes from what, in one column: (result, terms with signs).
_DERIVATIONS: list[tuple[str, list[tuple[str, int]]]] = [
    ("pbt", [("ebitda", 1), ("depreciation", -1), ("finance_costs", -1)]),
    ("pbt", [("ebitda", 1), ("depreciation", -1), ("finance_costs", -1),
             ("share_of_associates", 1)]),
    ("ebit", [("ebitda", 1), ("depreciation", -1)]),
    ("pbt", [("ebit", 1), ("finance_costs", -1)]),
    ("pat", [("pbt", 1), ("tax_expense", -1)]),
    ("pat_after_associates", [("pat", 1), ("share_of_associates", 1)]),
]
_REVENUES = ("gross_revenue", "revenue", "revenue_from_operations", "net_revenue")


def _column_of(f: FinancialFact) -> tuple | None:
    m = f.meta or {}
    if m.get("table_id") is None or m.get("col") is None:
        return None
    return (m["table_id"], m["col"])


def _carried(q: Qualification, how: str) -> Qualification:
    return Qualification(
        qualification_type=q.qualification_type,
        qualification_label=q.qualification_label,
        qualification_source=q.qualification_source,
        footnote_reference=q.footnote_reference,
        source_text=f"{q.source_text} [carried: {how}]"[:300])


def _close(a: float, b: float, rel: float = 0.005) -> bool:
    return abs(a - b) <= rel * max(abs(a), abs(b), 1.0)


def _propagate(column: list[FinancialFact]) -> int:
    """Carry a qualification to the figures the column's arithmetic derives
    from a qualified figure. Returns how many were qualified."""
    carried = 0
    changed = True
    while changed:
        changed = False
        by_key: dict[str, list[FinancialFact]] = defaultdict(list)
        for f in column:
            if f.value is not None:
                by_key[f.canonical_metric].append(f)
        for result, terms in _DERIVATIONS:
            for target in by_key.get(result, []):
                if target.qualification is not None:
                    continue
                if any(len(by_key.get(k, [])) != 1 for k, _ in terms):
                    continue
                inputs = [(by_key[k][0], sign) for k, sign in terms]
                seed = next((f for f, _ in inputs if f.qualification is not None), None)
                if seed is None:
                    continue
                total = sum(sign * f.value for f, sign in inputs)
                if _close(total, target.value):
                    formula = " ".join(
                        ("+ " if sign > 0 else "- ") + f.canonical_metric
                        for f, sign in inputs).lstrip("+ ")
                    target.qualification = _carried(
                        seed.qualification, f"{result} = {formula} in this column")
                    carried += 1
                    changed = True
        # A leverage ratio on a qualified EBITDA -- as printed, or on the
        # quarter annualised -- is on the same basis.
        for ratio in by_key.get("net_debt_to_ebitda", []):
            seed = next((f for f in by_key.get("ebitda", []) if f.qualification), None)
            debt = next((f for f in by_key.get("net_debt", []) if f.value), None)
            if ratio.qualification is not None or seed is None or debt is None or not seed.value:
                continue
            if ratio.value and any(_close(debt.value / (seed.value * k), ratio.value, 0.02)
                                   for k in (1.0, 4.0, 2.0)):
                ratio.qualification = _carried(
                    seed.qualification, "net_debt_to_ebitda = net_debt / ebitda")
                carried += 1
                changed = True
        # A margin struck on a qualified figure is on the same basis.
        for margin, base in (("ebitda_margin", "ebitda"), ("ebit_margin", "ebit"),
                             ("net_margin", "pat")):
            for m in by_key.get(margin, []):
                seed = next((f for f in by_key.get(base, []) if f.qualification), None)
                if m.qualification is not None or seed is None or m.value is None:
                    continue
                for rk in _REVENUES:
                    rev = next((f for f in by_key.get(rk, []) if f.value), None)
                    if rev and abs(seed.value / rev.value * 100.0 - m.value) <= 0.15:
                        m.qualification = _carried(
                            seed.qualification, f"{margin} = {base} / {rk}")
                        carried += 1
                        changed = True
                        break
    return carried


def qualify_from_footnotes(facts: list[FinancialFact], page_texts: dict[int, list[str]],
                           calendar: FiscalCalendar | None) -> int:
    """Apply footnote qualifications, page by page. Returns facts qualified."""
    by_page: dict[int, list[FinancialFact]] = defaultdict(list)
    for f in facts:
        if f.source_page:
            by_page[f.source_page].append(f)
    qualified = 0
    for page, lines in page_texts.items():
        on_page = by_page.get(page, [])
        if not on_page:
            continue
        seeded_columns: set[tuple] = set()
        for line in lines:
            m = _PERIOD_NOTE.match(line or "")
            if m:
                period = parse_period(m.group("period"), calendar=calendar)
                measure = match_metric(m.group("measure"), stock=False)
                if period is None or measure is None:
                    continue
                q = Qualification(
                    qualification_type=_type_of(m.group("verb")),
                    qualification_label=f"{m.group('verb')} {m.group('rest')}".strip()[:160],
                    qualification_source="footnote",
                    footnote_reference=(m.group("marker") or m.group("period")).strip(),
                    source_text=line.strip()[:300])
                for f in on_page:
                    if (f.qualification is None and f.canonical_metric == measure.key
                            and f.period == period.label):
                        f.qualification = q
                        qualified += 1
                        col = _column_of(f)
                        if col:
                            seeded_columns.add(col)
                continue
            m = _MARKER_NOTE.match(line or "")
            if m:
                marker = m.group("marker")
                q = Qualification(
                    qualification_type=_type_of(m.group("text").split()[0]),
                    qualification_label=m.group("text").strip()[:160],
                    qualification_source="footnote", footnote_reference=marker,
                    source_text=line.strip()[:300])
                for f in on_page:
                    label = (f.metric or "").rstrip()
                    if f.qualification is None and label.endswith(marker) and len(label) > 1:
                        f.qualification = q
                        qualified += 1
        for col in seeded_columns:
            qualified += _propagate([f for f in on_page if _column_of(f) == col])
    return qualified


def same_basis(a: FinancialFact, b: FinancialFact) -> bool:
    """Whether two figures are stated on the same basis."""
    qa, qb = a.qualification, b.qualification
    if qa is None and qb is None:
        return True
    if qa is None or qb is None:
        return False
    return (qa.qualification_type == qb.qualification_type
            and qa.qualification_label.lower() == qb.qualification_label.lower())


def describe(facts: Iterable[FinancialFact]) -> dict[str, int]:
    out: dict[str, int] = defaultdict(int)
    for f in facts:
        if f.qualification is not None:
            out[str(f.qualification.qualification_type)] += 1
    return dict(out)
