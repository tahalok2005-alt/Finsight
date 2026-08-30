"""Fiscal period parsing and normalisation.

Financial documents label the same period a dozen ways.  This module maps them
onto one canonical label while keeping the source text, and -- importantly --
keeps *fiscal* and *calendar* years distinct.  ``FY26`` is not the calendar year
2026; for an April-March fiscal calendar it ends 31 March 2026.

The fiscal year-end month is configurable per document rather than assumed, and
is inferred from statements like "quarter ended 30th June, 2026" when present.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date

from app.models.enums import PeriodType
from app.models.schemas import NormalizedPeriod

MONTHS = {
    "january": 1, "jan": 1, "february": 2, "feb": 2, "march": 3, "mar": 3,
    "april": 4, "apr": 4, "may": 5, "june": 6, "jun": 6, "july": 7, "jul": 7,
    "august": 8, "aug": 8, "september": 9, "sep": 9, "sept": 9,
    "october": 10, "oct": 10, "november": 11, "nov": 11, "december": 12, "dec": 12,
}

# 1Q FY27 / Q1 FY27 / Q1FY2027 / 1QFY27
_QFY = re.compile(
    r"\b(?:(?P<qn1>[1-4])\s*Q|Q\s*(?P<qn2>[1-4]))\s*[- ]?\s*"
    r"FY\s*[- ]?\s*(?P<year>\d{2,4})\b", re.I)
# FY26 / FY2026 / FY 25-26
_FY = re.compile(r"\bFY\s*[- ]?\s*(?P<year>\d{4}|\d{2})(?:\s*[-/]\s*(?P<year2>\d{2,4}))?\b", re.I)
# quarter ended 30th June, 2026 / three months ended June 30, 2026 /
# Quarter Ended (Unaudited) 31st Mar'26  -- audit-status parentheticals and
# apostrophe-elided two-digit years are both common in statement column heads.
_ENDED = re.compile(
    r"\b(?P<span>quarter|three\s+months?|six\s+months?|nine\s+months?|half[- ]year|"
    r"year|twelve\s+months?|full\s+year)\s+ended\s*"
    r"(?:\((?:un)?audited\)\s*)?"
    r"(?P<d1>\d{1,2})?\s*(?:st|nd|rd|th)?\s*,?\s*"
    r"(?P<month>[A-Za-z]{3,9})\s*,?\s*"
    r"(?P<d2>\d{1,2})?\s*,?\s*[''’`]?(?P<year>\d{4}|\d{2})\b", re.I)

# A bare date used as a column header: "30th Jun'26", "31st Mar 2026",
# "June 30, 2026".  Carries no span of its own -- the caller supplies it.
_BARE_DATE = re.compile(
    r"(?:(?P<d1>\d{1,2})\s*(?:st|nd|rd|th)?\s*[- ]?\s*"
    r"(?P<mon1>Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec)[a-z]*"
    r"|(?P<mon2>Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec)[a-z]*\s+"
    r"(?P<d2>\d{1,2})\s*(?:st|nd|rd|th)?)"
    r"\s*,?\s*[''’`]?\s*(?P<year>\d{4}|\d{2})\b", re.I)
_CY = re.compile(r"\b(?:CY|calendar\s+year)\s*[- ]?\s*(?P<year>\d{4}|\d{2})\b", re.I)
_TTM = re.compile(r"\b(TTM|trailing\s+twelve\s+months?|LTM)\b", re.I)
_HALF = re.compile(r"\bH([12])\s*[- ]?\s*FY\s*[- ]?\s*(\d{2,4})\b", re.I)
_NINEM = re.compile(r"\b9M\s*[- ]?\s*FY\s*[- ]?\s*(\d{2,4})\b", re.I)
_ASAT = re.compile(r"\bas\s+(?:at|on|of)\s+(?P<d1>\d{1,2})?\s*(?:st|nd|rd|th)?\s*,?\s*"
                   r"(?P<month>[A-Za-z]{3,9})\s*,?\s*(?P<d2>\d{1,2})?\s*,?\s*"
                   r"(?P<year>\d{4})", re.I)


def _four_digit_year(y: str | int) -> int:
    y = int(y)
    if y < 100:
        return 2000 + y if y < 80 else 1900 + y
    return y


@dataclass(slots=True)
class FiscalCalendar:
    """The fiscal year-end month for the reporting entity.

    Default is March (India/UK convention).  ``infer`` adjusts it from explicit
    "quarter ended <month> <year>" statements paired with a quarter label, so a
    December- or June-year-end filer is handled without configuration.
    """
    year_end_month: int = 3

    def quarter_end_month(self, q: int) -> int:
        return ((self.year_end_month + 3 * q - 1) % 12) + 1

    def quarter_of_month(self, month: int) -> int:
        start = (self.year_end_month % 12) + 1
        return ((month - start) % 12) // 3 + 1

    def fiscal_year_of(self, d: date) -> int:
        """Fiscal year *label* containing date d (labelled by its end year)."""
        return d.year + 1 if d.month > self.year_end_month else d.year

    def quarter_bounds(self, fy: int, q: int) -> tuple[date, date]:
        end_month = self.quarter_end_month(q)
        end_year = fy if end_month <= self.year_end_month else fy - 1
        start_month = ((end_month - 3) % 12) + 1
        start_year = end_year if start_month <= end_month else end_year - 1
        last_day = _last_day(end_year, end_month)
        return date(start_year, start_month, 1), date(end_year, end_month, last_day)

    def year_bounds(self, fy: int) -> tuple[date, date]:
        end = date(fy, self.year_end_month, _last_day(fy, self.year_end_month))
        start_month = (self.year_end_month % 12) + 1
        start_year = fy - 1 if start_month > self.year_end_month else fy
        return date(start_year, start_month, 1), end


def _last_day(year: int, month: int) -> int:
    if month == 12:
        return 31
    nxt = date(year + (month // 12), (month % 12) + 1, 1)
    return (nxt - date(year, month, 1)).days


DEFAULT_CALENDAR = FiscalCalendar(year_end_month=3)


def infer_calendar(texts: list[str]) -> FiscalCalendar:
    """Infer the fiscal year-end from '<Nth> quarter ended <month>' statements."""
    votes: dict[int, int] = {}
    for text in texts:
        if not text:
            continue
        for qm in _QFY.finditer(text):
            q = int(qm.group("qn1") or qm.group("qn2"))
            window = text[max(0, qm.start() - 160): qm.end() + 160]
            em = _ENDED.search(window)
            if not em:
                continue
            month = MONTHS.get((em.group("month") or "").lower())
            if not month:
                continue
            # year-end month = quarter-end month shifted back by (q-1) quarters
            ye = ((month - 3 * q) % 12) or 12
            votes[ye] = votes.get(ye, 0) + 1
    if votes:
        return FiscalCalendar(year_end_month=max(votes, key=lambda k: votes[k]))
    return DEFAULT_CALENDAR


def parse_period(text: str, *, calendar: FiscalCalendar | None = None,
                 default_span: str | None = None) -> NormalizedPeriod | None:
    """Parse any period expression found in ``text``.

    ``default_span`` supplies the periodicity ("quarter" or "year") for a bare
    date that carries none of its own -- typically taken from a spanning table
    header such as "Quarter Ended" sitting above several dated columns.

    Returns None when nothing period-like is present -- callers must not
    fabricate a period for a value that has none.
    """
    if not text:
        return None
    cal = calendar or DEFAULT_CALENDAR
    raw = text.strip()

    m = _QFY.search(raw)
    if m:
        q = int(m.group("qn1") or m.group("qn2"))
        fy = _four_digit_year(m.group("year"))
        start, end = cal.quarter_bounds(fy, q)
        return NormalizedPeriod(
            label=f"Q{q} FY{fy % 100:02d}", raw_text=m.group(0),
            period_type=PeriodType.QUARTER, fiscal_year=fy, quarter=q,
            start_date=start.isoformat(), end_date=end.isoformat(),
            is_fiscal=True, confidence=0.95)

    m = _HALF.search(raw)
    if m:
        half, fy = int(m.group(1)), _four_digit_year(m.group(2))
        s1, _ = cal.quarter_bounds(fy, 1 if half == 1 else 3)
        _, e2 = cal.quarter_bounds(fy, 2 if half == 1 else 4)
        return NormalizedPeriod(
            label=f"H{half} FY{fy % 100:02d}", raw_text=m.group(0),
            period_type=PeriodType.HALF_YEAR, fiscal_year=fy, quarter=None,
            start_date=s1.isoformat(), end_date=e2.isoformat(),
            is_fiscal=True, confidence=0.9)

    m = _NINEM.search(raw)
    if m:
        fy = _four_digit_year(m.group(1))
        s1, _ = cal.quarter_bounds(fy, 1)
        _, e3 = cal.quarter_bounds(fy, 3)
        return NormalizedPeriod(
            label=f"9M FY{fy % 100:02d}", raw_text=m.group(0),
            period_type=PeriodType.NINE_MONTH, fiscal_year=fy,
            start_date=s1.isoformat(), end_date=e3.isoformat(),
            is_fiscal=True, confidence=0.9)

    m = _ENDED.search(raw)
    if m:
        month = MONTHS.get((m.group("month") or "").lower())
        # the year may be elided to two digits ("31st Mar'26")
        year = _four_digit_year(m.group("year"))
        if month:
            day = int(m.group("d1") or m.group("d2") or _last_day(year, month))
            day = min(day, _last_day(year, month))
            end = date(year, month, day)
            span = (m.group("span") or "").lower()
            fy = cal.fiscal_year_of(end)
            if "quarter" in span or "three" in span:
                q = cal.quarter_of_month(month)
                start, _ = cal.quarter_bounds(fy, q)
                return NormalizedPeriod(
                    label=f"Q{q} FY{fy % 100:02d}", raw_text=m.group(0),
                    period_type=PeriodType.QUARTER, fiscal_year=fy, quarter=q,
                    start_date=start.isoformat(), end_date=end.isoformat(),
                    is_fiscal=True, confidence=0.85)
            if "six" in span or "half" in span:
                ptype, label = PeriodType.HALF_YEAR, f"H1 FY{fy % 100:02d}"
            elif "nine" in span:
                ptype, label = PeriodType.NINE_MONTH, f"9M FY{fy % 100:02d}"
            else:
                ptype, label = PeriodType.FISCAL_YEAR, f"FY{fy % 100:02d}"
            start, _ = cal.year_bounds(fy)
            return NormalizedPeriod(
                label=label, raw_text=m.group(0), period_type=ptype,
                fiscal_year=fy, start_date=start.isoformat(),
                end_date=end.isoformat(), is_fiscal=True, confidence=0.82)

    m = _CY.search(raw)
    if m:
        y = _four_digit_year(m.group("year"))
        return NormalizedPeriod(
            label=f"CY{y}", raw_text=m.group(0),
            period_type=PeriodType.CALENDAR_YEAR, fiscal_year=y,
            start_date=date(y, 1, 1).isoformat(),
            end_date=date(y, 12, 31).isoformat(),
            is_fiscal=False, confidence=0.9)

    m = _FY.search(raw)
    if m:
        y2 = m.group("year2")
        fy = _four_digit_year(y2) if y2 else _four_digit_year(m.group("year"))
        start, end = cal.year_bounds(fy)
        return NormalizedPeriod(
            label=f"FY{fy % 100:02d}", raw_text=m.group(0),
            period_type=PeriodType.FISCAL_YEAR, fiscal_year=fy,
            start_date=start.isoformat(), end_date=end.isoformat(),
            is_fiscal=True, confidence=0.9)

    if _TTM.search(raw):
        return NormalizedPeriod(label="TTM", raw_text=_TTM.search(raw).group(0),
                                period_type=PeriodType.TTM, confidence=0.7)

    # A bare date, e.g. a statement column headed "30th Jun'26".  Without a span
    # word the periodicity is genuinely unknown, so ``default_span`` must be
    # supplied by the caller from the table's spanning header; otherwise the
    # date is recorded as a point in time rather than guessed to be a quarter.
    m = _BARE_DATE.search(raw)
    if m:
        mon = (m.group("mon1") or m.group("mon2") or "").lower()
        month = MONTHS.get(mon)
        year = _four_digit_year(m.group("year"))
        if month:
            day = int(m.group("d1") or m.group("d2") or _last_day(year, month))
            day = min(day, _last_day(year, month))
            end = date(year, month, day)
            fy = cal.fiscal_year_of(end)
            if default_span == "quarter":
                q = cal.quarter_of_month(month)
                start, _ = cal.quarter_bounds(fy, q)
                return NormalizedPeriod(
                    label=f"Q{q} FY{fy % 100:02d}", raw_text=m.group(0),
                    period_type=PeriodType.QUARTER, fiscal_year=fy, quarter=q,
                    start_date=start.isoformat(), end_date=end.isoformat(),
                    is_fiscal=True, confidence=0.8)
            if default_span in ("year", "fiscal_year"):
                start, _ = cal.year_bounds(fy)
                return NormalizedPeriod(
                    label=f"FY{fy % 100:02d}", raw_text=m.group(0),
                    period_type=PeriodType.FISCAL_YEAR, fiscal_year=fy,
                    start_date=start.isoformat(), end_date=end.isoformat(),
                    is_fiscal=True, confidence=0.8)
            return NormalizedPeriod(
                label=f"As at {end.isoformat()}", raw_text=m.group(0),
                period_type=PeriodType.POINT_IN_TIME, fiscal_year=fy,
                quarter=cal.quarter_of_month(month),
                start_date=end.isoformat(), end_date=end.isoformat(),
                is_fiscal=True, confidence=0.6)

    m = _ASAT.search(raw)
    if m:
        month = MONTHS.get((m.group("month") or "").lower())
        year = int(m.group("year"))
        if month:
            day = int(m.group("d1") or m.group("d2") or _last_day(year, month))
            day = min(day, _last_day(year, month))
            d = date(year, month, day)
            fy = cal.fiscal_year_of(d)
            return NormalizedPeriod(
                label=f"As at {d.isoformat()}", raw_text=m.group(0),
                period_type=PeriodType.POINT_IN_TIME, fiscal_year=fy,
                quarter=cal.quarter_of_month(month),
                start_date=d.isoformat(), end_date=d.isoformat(),
                is_fiscal=True, confidence=0.75)
    return None


def find_all_periods(text: str, *, calendar: FiscalCalendar | None = None
                     ) -> list[NormalizedPeriod]:
    """All distinct periods mentioned, in order of appearance."""
    out: list[NormalizedPeriod] = []
    seen: set[str] = set()
    for pattern in (_QFY, _HALF, _NINEM, _ENDED, _CY, _FY):
        for m in pattern.finditer(text or ""):
            p = parse_period(m.group(0), calendar=calendar)
            if p and p.label not in seen:
                seen.add(p.label)
                out.append(p)
    return out


def period_distance(a: NormalizedPeriod, b: NormalizedPeriod) -> int | None:
    """Distance in quarters between two periods; None if incomparable."""
    if a.fiscal_year is None or b.fiscal_year is None:
        return None
    if a.quarter and b.quarter:
        return (a.fiscal_year * 4 + a.quarter) - (b.fiscal_year * 4 + b.quarter)
    if not a.quarter and not b.quarter:
        return (a.fiscal_year - b.fiscal_year) * 4
    return None


def is_year_over_year(current: NormalizedPeriod, prior: NormalizedPeriod) -> bool:
    d = period_distance(current, prior)
    return d == 4


def is_quarter_over_quarter(current: NormalizedPeriod, prior: NormalizedPeriod) -> bool:
    d = period_distance(current, prior)
    return d == 1
