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

from app.models.enums import PeriodBasis, PeriodType
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
#
# The space before "ended" is optional because OCR routinely loses it in the
# tight kerning of a column head: Groww's statement is headed "Quarterended"
# and "Yearended", and requiring the space left every column of that filing
# with a date but no span -- so four quarterly figures were typed as instants.
_ENDED = re.compile(
    r"\b(?P<span>quarter|three\s*months?|six\s*months?|nine\s*months?|half[- ]?year|"
    r"year|twelve\s*months?|full\s*year|\d{1,2}\s*months?)\s*ended\s*"
    r"(?:\((?:un)?audited\)\s*)?"
    r"(?P<d1>\d{1,2})?\s*(?:st|nd|rd|th)?\s*,?\s*"
    r"(?P<month>[A-Za-z]{3,9})\s*,?\s*"
    r"(?P<d2>\d{1,2})?\s*,?\s*[''’`]?(?P<year>\d{4}|\d{2})\b", re.I)


def _ended_year(m: re.Match) -> int | None:
    """The four-digit year of an ``_ENDED`` match, or None if it has none.

    The regex is deliberately permissive about separators, because filings
    elide the year behind an apostrophe ("31st Mar'26") and the apostrophe
    itself is often mangled by text extraction.  That permissiveness creates
    one genuine ambiguity: in "Three Months Ended June 28," -- where the year
    sits on the next line of a statement header -- the day "28" lands in the
    year group and would resolve to fiscal year 2028.

    The discriminator is whether a day was already captured *before* the month.
    "30th Jun'26" has one, so the trailing 26 is a year.  "June 28," has none,
    so a bare two-digit trailing number is the day and the year is simply
    absent.  A four-digit number is always a year.

    Returning None is the correct outcome for the ambiguous case: no period is
    far better than an invented one.
    """
    raw = m.group("year")
    if not raw:
        return None
    if len(raw) == 4:
        return int(raw)
    return _four_digit_year(raw) if m.group("d1") else None


def _bare_date_year(m: re.Match) -> int | None:
    """The year of a :data:`_BARE_DATE` match, or None when it has none.

    Separate from :func:`_ended_year` because the two patterns settle the
    two-digit case differently. ``_BARE_DATE`` only matches a two-digit year
    behind an apostrophe, so reaching ``year2`` is already evidence; the older
    shape accepted a bare pair of digits and read "30 June 31" as 2031.
    """
    four = m.group("year")
    if four:
        return int(four)
    two = m.group("year2")
    if not two:
        return None
    return _four_digit_year(two) if m.group("d1") else None

# A bare date used as a column header: "30th Jun'26", "31st Mar 2026",
# "June 30, 2026".  Carries no span of its own -- the caller supplies it.
#: Two rules here are load-bearing, and both were added after the parser
#: produced dates the source did not contain.
#:
#: **The day may not be taken from the year.** ``(?![\d])`` after the day stops
#: "Mar 2026" being read as the 20th of March in '26 -- the day group ate the
#: first half of the year and the year group took what was left. That misdated
#: a month-headed column by eleven days while looking entirely reasonable.
#:
#: **A two-digit year must be introduced by an apostrophe.** Without that,
#: "30 June 31" resolved to 30 June *2031*, a year appearing nowhere in the
#: document -- which is how three concatenated column heads produced a period
#: five years into the future. A bare trailing pair of digits after a complete
#: day-and-month really is ambiguous, and the rest of this module already holds
#: that line: see :data:`_MONTH_YEAR`, which requires the same evidence.
#: Every dated head in the corpus prints either a four-digit year or an
#: apostrophe, so nothing real is lost.
_BARE_DATE = re.compile(
    r"(?:(?P<d1>\d{1,2})\s*(?:st|nd|rd|th)?\s*[- ]?\s*"
    r"(?P<mon1>Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec)[a-z]*"
    r"|(?P<mon2>Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec)[a-z]*\s+"
    r"(?P<d2>\d{1,2})\s*(?:st|nd|rd|th)?(?!\d))"
    r"\s*,?\s*(?:(?P<year>\d{4})|['’`]\s*(?P<year2>\d{2}))\b", re.I)
#: An all-numeric date: "30.06.26", "31/12/2025", "30-06-26".  UK, European
#: and Indian filings head their columns this way as a matter of course.
#: A comma is accepted as a separator alongside the stop and the slash, because
#: OCR confuses the two constantly in small type: one bank's column head reads
#: "31,03.2026" where the page prints "31.03.2026". It cannot be a number --
#: three groups with a two-or-four digit tail is not a figure in any
#: convention -- and the day/month validity check below rejects anything that
#: is not a real date, so an Indian-grouped figure cannot slip through.
_NUMERIC_DATE = re.compile(
    r"(?<![\d.,\-/])(?P<a>\d{1,2})\s*[.,/-]\s*(?P<b>\d{1,2})\s*[.,/-]\s*"
    r"(?P<year>\d{4}|\d{2})(?![\d.,\-/])")


def _numeric_date(m: re.Match) -> date | None:
    """Resolve a numeric date, or None where the order cannot be established.

    ``30.06.26`` can only be day-first: there is no thirtieth month.  ``06.30.26``
    can only be month-first.  ``06.07.26`` is genuinely both, and no amount of
    reasoning about this string alone will settle it — 6 July under one
    convention, 7 June under the other.

    Guessing there would put a figure in the wrong month and leave no trace, so
    the ambiguous case returns None.  It costs little in practice: reporting
    periods close at month ends, and every month end past the twelfth is
    self-disambiguating.
    """
    a, b = int(m.group("a")), int(m.group("b"))
    if a > 12 and b <= 12:
        day, month = a, b
    elif b > 12 and a <= 12:
        month, day = a, b
    else:
        return None
    if not 1 <= month <= 12 or day < 1:
        return None
    year = _four_digit_year(m.group("year"))
    if day > _last_day(year, month):
        return None
    return date(year, month, day)


#: A month and a year with no day: "Jun-26", "Mar-2026", "Sep '25", "Jun 26".
#:
#: This is the standard column head of an Asian or European results
#: presentation -- a balance sheet set out as "Jun-25 | Mar-26 | Jun-26" -- and
#: it names a month rather than a day.
#:
#: A reporting column headed with a month closes at the end of that month:
#: there is no other day it could mean, and the day itself comes from the
#: calendar rather than from a guess. The end date is therefore recorded, at a
#: confidence below that of a head which printed the day outright, and the
#: label keeps the resolved date so a reader sees which day was used.
#: A two-digit number after a month is ambiguous on its own: "Jun-26" is the
#: year, "June 28," is the day with its year on the next header row. Only two
#: shapes settle it, and both are required to be explicit:
#:
#:   * a joining separator -- ``Jun-26``, ``Jun/26``, ``Jun'26`` -- which a day
#:     never takes; or
#:   * a four-digit year -- ``Jun 2026`` -- which a day never is.
#:
#: A bare ``Jun 26`` is left unresolved. It really is both, and inventing the
#: month end there would misdate a column by up to four days.
_MONTH_YEAR = re.compile(
    r"(?<![A-Za-z0-9])"
    r"(?P<mon>Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec)[a-z]*"
    r"(?:\s*[-–—/]\s*['’`]?(?P<y2>\d{2}|\d{4})"
    r"|\s*['’`]\s*(?P<y3>\d{2}|\d{4})"
    r"|\s+(?P<y4>\d{4}))"
    r"(?![\d%,])", re.I)


def _month_year_end(text: str) -> date | None:
    """The last day of a bare month-year, or None if there is no such head.

    A head that also prints a day is not this: "30 Jun 26" is a date and is
    read by :data:`_BARE_DATE`, which the callers try first.
    """
    m = _MONTH_YEAR.search(text or "")
    if m is None:
        return None
    month = MONTHS.get(m.group("mon").lower())
    raw_year = m.group("y2") or m.group("y3") or m.group("y4")
    if not month or not raw_year:
        return None
    year = _four_digit_year(raw_year)
    return date(year, month, _last_day(year, month))


_CY = re.compile(r"\b(?:CY|calendar\s+year)\s*[- ]?\s*(?P<year>\d{4}|\d{2})\b", re.I)
_TTM = re.compile(r"\b(TTM|trailing\s+twelve\s+months?|LTM)\b", re.I)
#: "H1 FY26", and also the bare "H1 2026" that half-yearly reporters use in
#: their own narrative.  The four-digit form is required without "FY" so that
#: "H1 26" -- which could as easily be a footnote marker -- is not swept up.
_HALF = re.compile(
    r"\bH([12])\s*[- ]?\s*(?:FY\s*[- ]?\s*(?P<fy>\d{2,4})"
    r"|(?P<cy>(?:19|20)\d{2}))\b", re.I)
_NINEM = re.compile(r"\b9M\s*[- ]?\s*FY\s*[- ]?\s*(\d{2,4})\b", re.I)
#: "Q1-Q2 2026", "Q1–Q3 2026": the quarters of a year to date.
_Q_RANGE = re.compile(
    r"\bQ\s*1\s*[-–—]\s*Q\s*(?P<last>[2-4])\s*[,/]?\s*(?P<year>(?:19|20)\d{2})\b", re.I)
#: "Q2 2026", "2Q 2026": a quarter of a year written in four digits, without
#: "FY". The year is the reporting year the filer names.
_Q_YEAR = re.compile(
    r"\b(?:Q\s*(?P<q1>[1-4])|(?P<q2>[1-4])\s*Q)\s*[,/]?\s*(?P<year>(?:19|20)\d{2})\b",
    re.I)
_MONTH_NAME = (r"(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?"
               r"|aug(?:ust)?|sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)")
#: "Jan-Jun 2026", "April - June 2026": a span written as its first and last
#: months. Its dates are in the text; no calendar is needed to read them.
_MONTH_RANGE = re.compile(
    r"\b(?P<m1>" + _MONTH_NAME + r")\.?\s*(?:[-–—]|to)\s*(?P<m2>" + _MONTH_NAME +
    r")\.?\s*,?\s*['’]?(?P<year>(?:19|20)\d{2}|\d{2})\b", re.I)
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

    ``is_known`` records whether the year end was actually established from the
    document or is merely the fallback.  When it is False the calendar can
    still date a period exactly -- an end date is an end date -- but it must
    not label one with a fiscal year, because that label would be a guess.  A
    period dated ``3M ended 2025-06-28`` is truthful; calling it ``Q1 FY26``
    when the filer closes its year in September is not.
    """
    year_end_month: int = 3
    is_known: bool = True

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


#: Used when the document never states its year end.  Marked *not* known
#: so periods derived under it are dated but not fiscally labelled.
DEFAULT_CALENDAR = FiscalCalendar(year_end_month=3, is_known=False)


def looks_like_period_header(text: str) -> bool:
    """Whether a fragment reads like a column head naming a period.

    Deliberately laxer than :func:`parse_period`.  Resolving a period demands
    an unambiguous year, and rightly refuses "Three Months Ended June 28,"
    where the year sits on the next line.  But that fragment is still plainly a
    *column head*, and table reconstruction needs to know that so it does not
    mistake the header block for data and shift every column by a row.

    Recognising the shape and resolving the value are two different questions.
    """
    t = text or ""
    if not t.strip():
        return False
    return bool(_ENDED.search(t) or _BARE_DATE.search(t) or _QFY.search(t)
                or _FY.search(t) or _CY.search(t) or _ASAT.search(t)
                or _HALF.search(t) or _NINEM.search(t) or _TTM.search(t)
                # "Q2 2026", "Q1-Q2 2025", "January-June 2026": a quarter or
                # span of a calendar year. Unrecognised, the row that carries
                # them beside "€ millions" was taken for data and a statement's
                # column heads were lost (Phase 22).
                or _Q_YEAR.search(t) or _Q_RANGE.search(t) or _MONTH_RANGE.search(t)
                or _resolvable_numeric_date(t)
                or _MONTH_YEAR.search(t)
                or re.search(r"\b(quarter|year|month)s?\s+ended\b", t,
                              re.I))


def _resolvable_numeric_date(text: str) -> bool:
    """Whether the text holds a numeric date that resolves without guessing.

    The regex shape alone is not enough. "10.11.12" has it and means nothing
    definite, and treating an ambiguous string as a column head would let
    table reconstruction cut the header band in the wrong place. Only a date
    that settles its own day-month order counts as evidence of a head.
    """
    return any(_numeric_date(m) is not None
               for m in _NUMERIC_DATE.finditer(text or ""))


def looks_like_period_fragment(text: str) -> bool:
    """Whether a fragment is *part of* a column head, complete or not.

    Laxer again than :func:`looks_like_period_header`, because a head printed
    over three lines has no complete line in it::

        Nine Months Ended        <- a span with no date
        June 28,                 <- a date with no year
        2025                     <- a year on its own

    Each of those is a header fragment and none of them parses on its own.
    Recognising them is what lets table reconstruction find a head that sits
    above the band it cut, and stop at the title or prose above that.
    """
    t = (text or "").strip()
    if not t:
        return False
    return bool(_YEAR_ONLY.match(t) or _DATE_NO_YEAR.search(t)
                or _SPAN_ONLY.search(t) or _AS_AT_ONLY.search(t)
                or looks_like_period_header(t))


_DATE_WORDS = (r"(?:\d{1,2}[./-]\d{1,2}[./-]\d{2,4}"
               r"|\d{1,2}(?:st|nd|rd|th)?\s+" + _MONTH_NAME + r",?\s+\d{4}"
               r"|" + _MONTH_NAME + r"\s+\d{1,2},?\s+\d{4})")
#: "as at 6/30/2026 and 12/31/2025", "as of June 28, 2025 and September 28, 2024".
_AS_AT_PAIR = re.compile(
    r"\bas\s+(?:at|of|on)\s+(?P<a>" + _DATE_WORDS + r")\s+and\s+(?P<b>" +
    _DATE_WORDS + r")", re.I)


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
            if not month or _ended_year(em) is None:
                continue
            # year-end month = quarter-end month shifted back by (q-1) quarters
            ye = ((month - 3 * q) % 12) or 12
            votes[ye] = votes.get(ye, 0) + 1
    if votes:
        return FiscalCalendar(
            year_end_month=max(votes, key=lambda k: votes[k]), is_known=True)

    # Fallback, used only when the document never writes "Q<n> FY<yy>" -- a
    # convention many filers outside India do not use.  A cumulative span pins
    # the year end by arithmetic alone: if nine months end in June, the year
    # began in October and so ends in September.  No regional assumption is
    # involved.
    #
    # This runs only as a fallback because a statement's header block
    # interleaves banners and dates across lines, so a regex can pair one
    # column's "Year Ended" with another column's date.  Where the quarter
    # markers exist they are the stronger evidence and settle it first.
    #
    # Three-month spans are ignored deliberately: a quarter ending in June is
    # Q1 under a March year end and Q3 under a September one, so it settles
    # nothing.
    span_months = {"six": 6, "nine": 9, "twelve": 12, "half": 6, "full": 12}
    for text in texts:
        if not text:
            continue
        for em in _ENDED.finditer(text):
            span = (em.group("span") or "").lower()
            months = next((n for word, n in span_months.items()
                           if word in span), None)
            if months is None and "year" in span:
                months = 12
            if not months:
                continue
            # The banner and the date must sit on the same line.  A statement
            # header block stacks "Quarter Ended" and "Year Ended" above four
            # dated columns, and in extracted reading order the regex will
            # happily pair one column's banner with another column's date
            # across a line break -- which votes for a year end the filer does
            # not use and shifts every period by a quarter.  Only a phrase
            # written as one line is real evidence.
            if "\n" in em.group(0):
                continue
            month = MONTHS.get((em.group("month") or "").lower())
            if not month or _ended_year(em) is None:
                continue
            ye = ((month - months) % 12) or 12
            votes[ye] = votes.get(ye, 0) + 1

    # An interim balance sheet compares its date with the close of the
    # preceding financial year -- "as at 6/30/2026 and 12/31/2025", "as of
    # June 28, 2025 and September 28, 2024" -- so the earlier date's month is
    # the year end. A filer that writes neither "Q<n> FY<yy>" nor a
    # cumulative span still says this much in the head of its balance sheet.
    for text in texts:
        for am in _AS_AT_PAIR.finditer(text or ""):
            first = parse_period(am.group("a"))
            second = parse_period(am.group("b"))
            if not (first and second and first.end_date and second.end_date):
                continue
            d1 = date.fromisoformat(first.end_date)
            d2 = date.fromisoformat(second.end_date)
            if d2 >= d1 or (d1 - d2).days > 400 or d2.day != _last_day(d2.year, d2.month):
                continue
            votes[d2.month] = votes.get(d2.month, 0) + 1

    # Only a *unanimous* span reading is trusted.  A statement's header block
    # interleaves banners and dates across lines, so the regex can pair one
    # column's "Year Ended" with a neighbouring column's quarter-end date and
    # vote for a year end the filer does not use.  When the readings disagree
    # the evidence is self-contradictory, which is not evidence: the default
    # calendar stands rather than a majority of possibly-mispaired matches.
    if len(votes) == 1:
        return FiscalCalendar(year_end_month=next(iter(votes)), is_known=True)
    return DEFAULT_CALENDAR


# --------------------------------------------------- hierarchical headers
#: The span phrase of a column head, wherever it sits in the hierarchy.
#: "Three Months Ended" over "June 28," over "2025" is one column head spread
#: across three header rows, and the span belongs to the whole stack.
_SPAN_ONLY = re.compile(
    r"\b(?P<span>quarter|three\s+months?|six\s+months?|nine\s+months?|"
    r"twelve\s+months?|half[- ]?year|full\s+year|year|period|\d{1,2}\s*months?)"
    r"\s+end(?:ed|ing)\b", re.I)
_AS_AT_ONLY = re.compile(r"\bas\s+(?:at|on|of)\b", re.I)
#: A date whose year is missing because it wrapped onto the next header row.
_DATE_NO_YEAR = re.compile(
    r"(?:(?P<d1>\d{1,2})\s*(?:st|nd|rd|th)?\s*[- ]?\s*"
    r"(?P<mon1>Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec)[a-z]*"
    r"|(?P<mon2>Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec)[a-z]*\s*"
    r"(?P<d2>\d{1,2})\s*(?:st|nd|rd|th)?)\s*,?\s*$", re.I)
_YEAR_ONLY = re.compile(r"^\s*(?P<year>(?:19|20)\d{2})\s*$")
#: The date-based labels emitted when the fiscal calendar is unknown.
#: They must parse back, because later passes re-read column labels.
_SPAN_ISO = re.compile(
    r"^\s*(?P<months>\d{1,2})M\s+ended\s+(?P<date>\d{4}-\d{2}-\d{2})\s*$", re.I)
_ASAT_ISO = re.compile(
    r"^\s*as\s+at\s+(?P<date>\d{4}-\d{2}-\d{2})\s*$", re.I)

#: How many months each span word covers.  "quarter" and "three months" are the
#: same thing said two ways; the distinction that matters is the *length*.
_SPAN_MONTH_COUNT = {
    "quarter": 3, "three months": 3, "three month": 3,
    "six months": 6, "six month": 6, "half year": 6, "half-year": 6,
    "nine months": 9, "nine month": 9,
    "twelve months": 12, "twelve month": 12, "year": 12, "full year": 12,
    "period": 0,          # length unstated; the date alone must carry it
}


def _span_months(phrase: str) -> int | None:
    key = re.sub(r"\s+", " ", (phrase or "").strip().lower())
    hit = _SPAN_MONTH_COUNT.get(key)
    if hit is not None:
        return hit
    # "6 months ended" says the same thing as "six months ended"; UK and
    # European filings prefer the digit.  Anything longer than a year is not a
    # reporting span and is left unrecognised rather than trusted.
    digits = re.fullmatch(r"(\d{1,2})\s*months?", key)
    if digits:
        n = int(digits.group(1))
        return n if 1 <= n <= 12 else None
    return None


def _period_type_for(months: int | None) -> PeriodType:
    return {3: PeriodType.QUARTER, 6: PeriodType.HALF_YEAR,
            9: PeriodType.NINE_MONTH,
            12: PeriodType.FISCAL_YEAR}.get(months or 0, PeriodType.UNKNOWN)


def _start_of_span(end: date, months: int) -> date:
    """First day of a span of ``months`` ending on ``end``."""
    month = end.month - months + 1
    year = end.year + (month - 1) // 12
    month = ((month - 1) % 12) + 1
    return date(year, month, 1)


#: A head that names its period rather than implying it from a date.  These
#: are the document's own words -- "1Q FY27", "H1 FY26", "CY2025" -- and no
#: banner above them overrides what they say.
_STATES_OWN_PERIOD = re.compile(
    r"(?<![A-Za-z0-9])(?:[1-4]\s*Q|Q\s*[1-4]|H\s*[12]|9\s*M)\s*(?:FY|F\.Y\.)\s*\d{2,4}(?![A-Za-z0-9])"
    r"|(?<![A-Za-z0-9])FY\s*[- ]?\s*\d{2,4}(?![A-Za-z0-9])"
    r"|(?<![A-Za-z0-9])(?:CY|calendar\s+year)\s*[- ]?\s*\d{2,4}(?![A-Za-z0-9])", re.I)


def _span_rank(period: NormalizedPeriod) -> int:
    """How specific a period is, as the number of months it covers."""
    return {PeriodType.QUARTER: 3, PeriodType.HALF_YEAR: 6,
            PeriodType.NINE_MONTH: 9, PeriodType.FISCAL_YEAR: 12,
            PeriodType.CALENDAR_YEAR: 12,
            }.get(PeriodType(period.period_type), 99)


def _resolve_from_levels(levels: list[str], *,
                         calendar: FiscalCalendar | None = None,
                         default_span: str | None = None,
                         basis: PeriodBasis = PeriodBasis.DERIVED,
                         ) -> NormalizedPeriod | None:
    """Resolve a column's period from its **header hierarchy**.

    A statement column head is a stack, not a line.  The span phrase sits on one
    header row, spanning several columns; the date sits on the row beneath it;
    and the year frequently wraps onto a third row::

            Three Months Ended                Nine Months Ended
          June 28,      June 29,            June 28,      June 29,
            2025          2024                2025          2024

    Flattening that to one string and pattern-matching it works only when the
    fragments happen to land in the right order.  This reads the stack as a
    stack: find the span at whatever level states it, find the date at whatever
    level carries it, and rejoin a date whose year wrapped.

    Falls back to :func:`parse_period` over the joined text, so every form that
    already worked keeps working.
    """
    cal = calendar or DEFAULT_CALENDAR
    parts = [p.strip() for p in (levels or []) if p and p.strip()]
    if not parts:
        return None

    # ---- 0. a level that names its own period outright -------------------
    # A banner states what a group of columns has in common; a leaf states what
    # *this* column is.  Where a column head reads "1Q FY26" under a banner
    # reading "results for the quarter ended 30th June, 2026", the leaf is the
    # more specific statement and must win -- otherwise every column under the
    # banner collapses onto the banner's own period.  Deepest level first,
    # because specificity increases downwards.
    # Levels are read from the deepest outwards, because a head may be stacked
    # ("1Q" over "FY27") and only means what it says once joined.  Among the
    # readings that parse, the shortest span wins: "1Q FY27" is a more specific
    # statement than the "FY27" inside it, and specificity is what a leaf has
    # that a banner does not.
    stated: NormalizedPeriod | None = None
    for k in range(1, len(parts) + 1):
        tail = " ".join(parts[len(parts) - k:])
        if not _STATES_OWN_PERIOD.search(tail):
            continue
        candidate = parse_period(tail, calendar=cal)
        if candidate is None:
            continue
        if stated is None or _span_rank(candidate) < _span_rank(stated):
            stated = candidate
    if stated is not None:
        return stated

    # ---- 1. the span, from whichever level names it ----------------------
    span_phrase: str | None = None
    instant = False
    for part in parts:
        m = _SPAN_ONLY.search(part)
        if m:
            span_phrase = m.group("span")
            break
    if span_phrase is None:
        # A span phrase printed over two lines -- "Financial year" over
        # "ended on" -- is a phrase only when the lines are read together.
        for first, second in zip(parts, parts[1:]):
            m = _SPAN_ONLY.search(f"{first} {second}")
            if m:
                span_phrase = m.group("span")
                break
    if span_phrase is None and any(_AS_AT_ONLY.search(p) for p in parts):
        instant = True

    # ---- 2. the date, rejoining a year that wrapped ----------------------
    joined = " ".join(parts)
    date_text: str | None = None
    if _BARE_DATE.search(joined):
        date_text = _BARE_DATE.search(joined).group(0)
    else:
        # "June 28," on one level and "2025" on the next
        for i, part in enumerate(parts):
            head = _DATE_NO_YEAR.search(part)
            if not head:
                continue
            for follower in parts[i + 1:]:
                year = _YEAR_ONLY.match(follower)
                if year:
                    date_text = f"{head.group(0).rstrip(', ')} {year.group('year')}"
                    break
            if date_text:
                break

    end: date | None = None
    if date_text is None:
        # No month name anywhere.  A numeric head — "6 months ended / 30.06.26",
        # or a bare "31.12.25" over a balance-sheet column — is a date the
        # document printed just as plainly.
        nm = _NUMERIC_DATE.search(joined)
        if nm is not None:
            end = _numeric_date(nm)
        if end is None:
            # A month with no day -- "Jun-26" over a balance-sheet column.
            end = _month_year_end(joined)
        if end is None:
            return None
    else:
        dm = _BARE_DATE.search(date_text)
        if dm is None:
            return None
        month = MONTHS.get((dm.group("mon1") or dm.group("mon2") or "").lower())
        day = int(dm.group("d1") or dm.group("d2") or 0)
        year = _bare_date_year(dm)
        if not month or not day or not year:
            return None
        day = min(day, _last_day(year, month))
        end = date(year, month, day)

    # ---- 3. combine span + date -----------------------------------------
    months = _span_months(span_phrase) if span_phrase else None
    if months == 0:                       # "period ended", length unstated
        months = None
    if months is None and not instant and default_span:
        # Any span phrase may be inherited, not just "quarter" or "year": a
        # sibling column reading "Nine Months Ended" is evidence about the
        # column beside it whose banner was typeset too narrow to reach it.
        months = _span_months(default_span) or None

    if instant:
        return NormalizedPeriod(
            label=f"As at {end.isoformat()}", raw_text=" / ".join(parts),
            period_type=PeriodType.POINT_IN_TIME, fiscal_year=None,
            end_date=end.isoformat(), is_fiscal=False, confidence=0.9)

    if months is None:
        # A bare date with no span anywhere in the stack is an instant.  Saying
        # so is honest; inventing a quarter around it would not be.
        return NormalizedPeriod(
            label=f"As at {end.isoformat()}", raw_text=" / ".join(parts),
            period_type=PeriodType.POINT_IN_TIME, fiscal_year=None,
            end_date=end.isoformat(), is_fiscal=False, confidence=0.75)

    start = _start_of_span(end, months)
    period_type = _period_type_for(months)

    if not cal.is_known:
        # The exact dates are known; the fiscal year is not.  Label by the span
        # and the end date rather than guessing a fiscal year -- an incomplete
        # truth beats a confident error.
        return NormalizedPeriod(
            label=f"{months}M ended {end.isoformat()}",
            raw_text=" / ".join(parts), period_type=period_type,
            fiscal_year=None, quarter=None,
            start_date=start.isoformat(), end_date=end.isoformat(),
            is_fiscal=False, confidence=0.7)

    fy = cal.fiscal_year_of(end)
    if months == 3:
        quarter = cal.quarter_of_month(end.month)
        return NormalizedPeriod(
            label=f"Q{quarter} FY{fy % 100:02d}", raw_text=" / ".join(parts),
            period_type=PeriodType.QUARTER, fiscal_year=fy, quarter=quarter,
            start_date=start.isoformat(), end_date=end.isoformat(),
            is_fiscal=True, confidence=0.92)
    if months == 12:
        return NormalizedPeriod(
            label=f"FY{fy % 100:02d}", raw_text=" / ".join(parts),
            period_type=PeriodType.FISCAL_YEAR, fiscal_year=fy,
            start_date=start.isoformat(), end_date=end.isoformat(),
            is_fiscal=True, confidence=0.92)
    prefix = {6: "H", 9: "9M"}.get(months, f"{months}M")
    label = (f"H{1 if cal.quarter_of_month(end.month) <= 2 else 2} FY{fy % 100:02d}"
             if months == 6 else f"9M FY{fy % 100:02d}")
    return NormalizedPeriod(
        label=label, raw_text=" / ".join(parts), period_type=period_type,
        fiscal_year=fy, start_date=start.isoformat(), end_date=end.isoformat(),
        is_fiscal=True, confidence=0.9)


#: A span phrase anywhere in a head: "Quarter ended", "Nine months ended",
#: "Year ended" -- with or without the space OCR loses ("Quarterended").
_SPAN_PHRASE = re.compile(
    r"\b(?P<span>quarter|three\s*months?|six\s*months?|nine\s*months?|"
    r"half[- ]?year|year|twelve\s*months?|full\s*year)\s*ended\b", re.I)


def _span_from_text(raw: str) -> str | None:
    """The span a head's own words declare, in the vocabulary `default_span`
    uses: quarter, half_year, nine_month or year."""
    m = _SPAN_PHRASE.search(raw or "")
    if not m:
        return None
    w = m.group("span").lower()
    if "quarter" in w or "three" in w:
        return "quarter"
    if "six" in w or "half" in w:
        return "half_year"
    if "nine" in w:
        return "nine_month"
    return "year"


def _spanned(end: date, span: str | None, cal: FiscalCalendar, raw_text: str,
             confidence: float) -> NormalizedPeriod | None:
    """The period of the given span that ends on ``end``, or None if the span
    is not one this module can date."""
    fy = cal.fiscal_year_of(end)
    if span == "quarter":
        q = cal.quarter_of_month(end.month)
        start, _ = cal.quarter_bounds(fy, q)
        return NormalizedPeriod(
            label=f"Q{q} FY{fy % 100:02d}", raw_text=raw_text,
            period_type=PeriodType.QUARTER, fiscal_year=fy, quarter=q,
            start_date=start.isoformat(), end_date=end.isoformat(),
            is_fiscal=True, confidence=confidence)
    if span in ("half_year", "nine_month", "year", "fiscal_year"):
        start, _ = cal.year_bounds(fy)
        ptype, label = {
            "half_year": (PeriodType.HALF_YEAR, f"H1 FY{fy % 100:02d}"),
            "nine_month": (PeriodType.NINE_MONTH, f"9M FY{fy % 100:02d}"),
        }.get(span, (PeriodType.FISCAL_YEAR, f"FY{fy % 100:02d}"))
        return NormalizedPeriod(
            label=label, raw_text=raw_text, period_type=ptype, fiscal_year=fy,
            start_date=start.isoformat(), end_date=end.isoformat(),
            is_fiscal=True, confidence=confidence)
    return None


#: "31-Dec-25", "30-Jun-23", "31/Mar/26": a day, a month name and a two-digit
#: year joined by separators. The leading day and the joins settle what a
#: bare "Jun 26" cannot, so this is a full date.
_DAY_MON_YY = re.compile(
    r"(?<![\d])(?P<d>\d{1,2})\s*[-/]\s*"
    r"(?P<mon>Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec)[a-z]*"
    r"\s*[-/]\s*(?P<yy>\d{2})(?!\d)", re.I)


def _own_date_beside(raw: str, label: re.Match, kind: PeriodType,
                     fiscal_year: int) -> NormalizedPeriod | None:
    """The dated period a head states beside its fiscal label, when the two
    agree.

    "Year ended March 31, 2026 (FY2026)" names the year and prints the day
    it closed. Under an unknown calendar the label alone gives no dates, and
    reading only the label threw away the date printed beside it -- a bank's
    year column came out with no end date at all. The date is taken only if
    it is the same kind of period and ends in the labelled year."""
    rest = (raw[:label.start()] + " " + raw[label.end():]).strip()
    if not rest:
        return None
    dated = _resolve_from_levels([rest], calendar=FiscalCalendar(3, False),
                                 basis=PeriodBasis.STATED)
    if dated is None or not dated.end_date or dated.period_type != kind:
        return None
    if kind == PeriodType.FISCAL_YEAR and int(dated.end_date[:4]) != fiscal_year:
        return None
    return dated


def _resolve_from_text(text: str, *, calendar: FiscalCalendar | None = None,
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
    raw = _part_first(text.strip())

    # A label this module emitted under an unknown calendar, read back.
    m = _SPAN_ISO.match(raw)
    if m:
        months = int(m.group("months"))
        end = date.fromisoformat(m.group("date"))
        return NormalizedPeriod(
            label=raw, raw_text=raw, period_type=_period_type_for(months),
            fiscal_year=None, start_date=_start_of_span(end, months).isoformat(),
            end_date=end.isoformat(), is_fiscal=False, confidence=0.7)
    m = _ASAT_ISO.match(raw)
    if m:
        return NormalizedPeriod(
            label=raw, raw_text=raw, period_type=PeriodType.POINT_IN_TIME,
            fiscal_year=None, end_date=m.group("date"), is_fiscal=False,
            confidence=0.75)

    m = _QFY.search(raw)
    if m:
        q = int(m.group("qn1") or m.group("qn2"))
        fy = _four_digit_year(m.group("year"))
        if not cal.is_known:
            dated = _own_date_beside(raw, m, PeriodType.QUARTER, fy)
            if dated is not None:
                return NormalizedPeriod(
                    label=f"Q{q} FY{fy % 100:02d}", raw_text=raw[:80],
                    period_type=PeriodType.QUARTER, fiscal_year=fy, quarter=q,
                    start_date=dated.start_date, end_date=dated.end_date,
                    is_fiscal=True, confidence=0.9)
            # The quarter and the fiscal year are stated; the dates they cover
            # depend on a year end this document never gave, so they stay unset.
            return NormalizedPeriod(
                label=f"Q{q} FY{fy % 100:02d}", raw_text=m.group(0),
                period_type=PeriodType.QUARTER, fiscal_year=fy, quarter=q,
                is_fiscal=True, confidence=0.85)
        start, end = cal.quarter_bounds(fy, q)
        return NormalizedPeriod(
            label=f"Q{q} FY{fy % 100:02d}", raw_text=m.group(0),
            period_type=PeriodType.QUARTER, fiscal_year=fy, quarter=q,
            start_date=start.isoformat(), end_date=end.isoformat(),
            is_fiscal=True, confidence=0.95)

    m = _HALF.search(raw)
    if m:
        half = int(m.group(1))
        fy = _four_digit_year(m.group("fy") or m.group("cy"))
        if not cal.is_known:
            return NormalizedPeriod(
                label=f"H{half} FY{fy % 100:02d}", raw_text=m.group(0),
                period_type=PeriodType.HALF_YEAR, fiscal_year=fy, quarter=None,
                is_fiscal=True, confidence=0.8)
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
        if not cal.is_known:
            return NormalizedPeriod(
                label=f"9M FY{fy % 100:02d}", raw_text=m.group(0),
                period_type=PeriodType.NINE_MONTH, fiscal_year=fy,
                is_fiscal=True, confidence=0.8)
        s1, _ = cal.quarter_bounds(fy, 1)
        _, e3 = cal.quarter_bounds(fy, 3)
        return NormalizedPeriod(
            label=f"9M FY{fy % 100:02d}", raw_text=m.group(0),
            period_type=PeriodType.NINE_MONTH, fiscal_year=fy,
            start_date=s1.isoformat(), end_date=e3.isoformat(),
            is_fiscal=True, confidence=0.9)

    # "Q1-Q2 2026" and "Q2 2026" name the filer's own reporting year. Where
    # that year is the calendar year -- a December year end -- the dates
    # follow. Under any other year end, or none known, "2026" could be either
    # the fiscal year or the calendar year the quarter falls in, and the
    # dates are left unset rather than chosen between.
    m = _Q_RANGE.search(raw)
    if m:
        last = int(m.group("last"))
        year = int(m.group("year"))
        ptype = {2: PeriodType.HALF_YEAR, 3: PeriodType.NINE_MONTH,
                 4: PeriodType.FISCAL_YEAR}[last]
        label = {2: f"H1 {year}", 3: f"9M {year}", 4: f"FY {year}"}[last]
        if cal.is_known and cal.year_end_month == 12:
            s1, _ = cal.quarter_bounds(year, 1)
            _, e2 = cal.quarter_bounds(year, last)
            return NormalizedPeriod(
                label=label, raw_text=m.group(0), period_type=ptype,
                fiscal_year=year, start_date=s1.isoformat(),
                end_date=e2.isoformat(), is_fiscal=True, confidence=0.9)
        return NormalizedPeriod(label=label, raw_text=m.group(0),
                                period_type=ptype, confidence=0.7)
    m = _Q_YEAR.search(raw)
    if m:
        q = int(m.group("q1") or m.group("q2"))
        year = int(m.group("year"))
        if cal.is_known and cal.year_end_month == 12:
            start, end = cal.quarter_bounds(year, q)
            return NormalizedPeriod(
                label=f"Q{q} {year}", raw_text=m.group(0),
                period_type=PeriodType.QUARTER, fiscal_year=year, quarter=q,
                start_date=start.isoformat(), end_date=end.isoformat(),
                is_fiscal=True, confidence=0.9)
        return NormalizedPeriod(label=f"Q{q} {year}", raw_text=m.group(0),
                                period_type=PeriodType.QUARTER, quarter=q,
                                confidence=0.7)
    m = _MONTH_RANGE.search(raw)
    if m:
        m1 = MONTHS.get(m.group("m1").lower()[:3])
        m2 = MONTHS.get(m.group("m2").lower()[:3])
        if m1 and m2 and m1 != m2:
            year = _four_digit_year(m.group("year"))
            months = (m2 - m1) % 12 + 1
            start = date(year - 1 if m1 > m2 else year, m1, 1)
            end = date(year, m2, _last_day(year, m2))
            ptype = (PeriodType.CALENDAR_YEAR if (m1, m2) == (1, 12)
                     else _period_type_for(months))
            return NormalizedPeriod(
                label=m.group(0).strip(), raw_text=m.group(0),
                period_type=ptype,
                fiscal_year=cal.fiscal_year_of(end) if cal.is_known else None,
                start_date=start.isoformat(), end_date=end.isoformat(),
                is_fiscal=False, confidence=0.85)

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
        if not cal.is_known:
            dated = _own_date_beside(raw, m, PeriodType.FISCAL_YEAR, fy)
            if dated is not None:
                return NormalizedPeriod(
                    label=f"FY{fy % 100:02d}", raw_text=raw[:80],
                    period_type=PeriodType.FISCAL_YEAR, fiscal_year=fy,
                    start_date=dated.start_date, end_date=dated.end_date,
                    is_fiscal=True, confidence=0.9)
            # The document names its own fiscal year, so the label is a fact.
            # The calendar dates it spans are not: without a year end there is
            # no way to say when FY26 began or closed, so they stay unset.
            return NormalizedPeriod(
                label=f"FY{fy % 100:02d}", raw_text=m.group(0),
                period_type=PeriodType.FISCAL_YEAR, fiscal_year=fy,
                is_fiscal=True, confidence=0.8)
        start, end = cal.year_bounds(fy)
        return NormalizedPeriod(
            label=f"FY{fy % 100:02d}", raw_text=m.group(0),
            period_type=PeriodType.FISCAL_YEAR, fiscal_year=fy,
            start_date=start.isoformat(), end_date=end.isoformat(),
            is_fiscal=True, confidence=0.9)

    if _TTM.search(raw):
        return NormalizedPeriod(label="TTM", raw_text=_TTM.search(raw).group(0),
                                period_type=PeriodType.TTM, confidence=0.7)

    if not cal.is_known:
        # Everything above this point is the document stating a fiscal label
        # outright ("Q1 FY27", "FY26"), which is fact, not inference.  What
        # follows derives a period from a *date*, and deriving a fiscal year
        # from a date needs a year end this document never gave us.  Hand it to
        # the hierarchy-aware parser, which dates the period exactly and leaves
        # the fiscal year unset rather than guessing it.
        # Flat source text, handed to the hierarchy parser only because it
        # dates a period without inventing a fiscal year.  The document
        # still *stated* this, so the basis stays STATED.
        return _resolve_from_levels([raw], calendar=cal,
                                    default_span=default_span,
                                    basis=PeriodBasis.STATED)

    m = _ENDED.search(raw)
    if m:
        month = MONTHS.get((m.group("month") or "").lower())
        year = _ended_year(m)
        if month and year:
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


    # The head's own span words govern every date form below. "Quarter ended
    # 31.12.2025" and "Nine months ended 31-Dec-24" were read as balances "as
    # at" those days: the span attached only to a date whose month was written
    # out, and a numeric or hyphenated date lost it -- which is how a bank's
    # nine months to 31-Dec-24 came to be reported as a balance.
    # The head's own words come first; a span supplied from elsewhere in the
    # table only speaks for a date that says nothing itself. A steel maker's
    # "Financial year ended on 31.03.2026" sat in a head block whose only
    # other span was "Quarter ended", and with the borrowed span first the
    # year's figures became the fourth quarter's.
    default_span = _span_from_text(raw) or default_span

    m = _DAY_MON_YY.search(raw)
    if m:
        month = MONTHS.get(m.group("mon").lower())
        if month:
            year = _four_digit_year(m.group("yy"))
            day = int(m.group("d"))
            if 1 <= day <= _last_day(year, month):
                end = date(year, month, day)
                spanned = _spanned(end, default_span, cal, m.group(0), 0.82)
                if spanned is not None:
                    return spanned
                fy = cal.fiscal_year_of(end)
                return NormalizedPeriod(
                    label=f"As at {end.isoformat()}", raw_text=m.group(0),
                    period_type=PeriodType.POINT_IN_TIME, fiscal_year=fy,
                    quarter=cal.quarter_of_month(month),
                    start_date=end.isoformat(), end_date=end.isoformat(),
                    is_fiscal=True, confidence=0.7)

    # A bare date, e.g. a statement column headed "30th Jun'26".  Without a span
    # word the periodicity is genuinely unknown, so ``default_span`` must be
    # supplied by the caller from the table's spanning header; otherwise the
    # date is recorded as a point in time rather than guessed to be a quarter.
    m = _BARE_DATE.search(raw)
    if m:
        mon = (m.group("mon1") or m.group("mon2") or "").lower()
        month = MONTHS.get(mon)
        # `_bare_date_year` carries the apostrophe discipline; going straight to
        # the raw group would accept the bare two-digit form this pattern now
        # deliberately refuses.
        year = _bare_date_year(m)
        if month and year:
            day = int(m.group("d1") or m.group("d2") or _last_day(year, month))
            day = min(day, _last_day(year, month))
            end = date(year, month, day)
            fy = cal.fiscal_year_of(end)
            spanned = _spanned(end, default_span, cal, m.group(0), 0.8)
            if spanned is not None:
                return spanned
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

    # An all-numeric date: "30.06.2026", "31/12/25". Reachable through
    # `parse_period_levels` but not, until now, through here -- so the same
    # column head meant a period when it arrived as a list of header lines and
    # nothing when it arrived as one string. A bank's results page went that
    # way: every dated column fell back to the period in the document title,
    # and four different quarters were all reported as the first.
    numeric = _NUMERIC_DATE.search(raw)
    if numeric is not None:
        end = _numeric_date(numeric)
        if end is not None:
            fy = cal.fiscal_year_of(end)
            spanned = _spanned(end, default_span, cal, numeric.group(0), 0.8)
            if spanned is not None:
                return spanned
            return NormalizedPeriod(
                label=f"As at {end.isoformat()}", raw_text=numeric.group(0),
                period_type=PeriodType.POINT_IN_TIME, fiscal_year=fy,
                quarter=cal.quarter_of_month(end.month),
                start_date=end.isoformat(), end_date=end.isoformat(),
                is_fiscal=True, confidence=0.75)

    # A month-and-year head with no day: "Jun-26", "Mar-2026", "Sep '25". This
    # is the standard column head of a KPI table, and it resolved through
    # `parse_period_levels` but not through here -- so the same text meant a
    # period in a table and nothing at all in a sentence. The two entry points
    # now agree. Confidence stays below a head that printed its day, and the
    # discipline of :data:`_MONTH_YEAR` still applies: a bare "Jun 26" is
    # refused, because that really is both a day and a year.
    end = _month_year_end(raw)
    if end is not None:
        fy = cal.fiscal_year_of(end)
        spanned = _spanned(end, default_span, cal, raw.strip()[:60], 0.7)
        if spanned is not None:
            return spanned
        return NormalizedPeriod(
            label=f"As at {end.isoformat()}", raw_text=raw.strip()[:60],
            period_type=PeriodType.POINT_IN_TIME, fiscal_year=fy,
            quarter=cal.quarter_of_month(end.month),
            start_date=end.isoformat(), end_date=end.isoformat(),
            is_fiscal=True, confidence=0.6)
    return None


#: "FY 26 Q4" -- a year's group head over its quarter -- is Q4 of FY26, not the
#: year: a deck's fourth-quarter column was typed as the fiscal year (Phase 22).
_FY_THEN_PART = re.compile(
    r"\bFY\s*[- ]?\s*(?P<year>\d{4}|\d{2})\s*[-,/:]?\s*(?P<part>Q\s*[1-4]|[1-4]\s*Q|H\s*[12]|9\s*M)\b",
    re.I)


def _part_first(text: str) -> str:
    return _FY_THEN_PART.sub(lambda m: f"{m.group('part')} FY{m.group('year')}", text)


def find_all_periods(text: str, *, calendar: FiscalCalendar | None = None
                     ) -> list[NormalizedPeriod]:
    """All distinct periods mentioned, in order of appearance."""
    out: list[NormalizedPeriod] = []
    seen: set[str] = set()
    text = _part_first(text or "")
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


def _stamp(period: NormalizedPeriod | None,
           basis: PeriodBasis) -> NormalizedPeriod | None:
    """Record how this period was arrived at, without overwriting a stronger
    answer a nested resolver already gave."""
    if period is not None and period.basis == PeriodBasis.UNKNOWN:
        period.basis = basis
    return period


def parse_period(text: str, *, calendar: FiscalCalendar | None = None,
                 default_span: str | None = None) -> NormalizedPeriod | None:
    """Parse a period the document wrote out, in one piece of text.

    Anything this returns was matched against words on the page, so the basis
    is ``STATED``: the source said it, whatever else had to be worked out to
    turn it into dates.
    """
    return _stamp(_resolve_from_text(text, calendar=calendar,
                                     default_span=default_span),
                  PeriodBasis.STATED)


def parse_period_levels(levels: list[str], *,
                        calendar: FiscalCalendar | None = None,
                        default_span: str | None = None,
                        basis: PeriodBasis = PeriodBasis.DERIVED,
                        ) -> NormalizedPeriod | None:
    """Resolve a period from a column head's stack of levels.

    Assembling a span from one row with a date from another is *derivation* --
    the document never wrote the period as such -- so that is the default
    basis.  A level that states its own period outright comes back already
    marked ``STATED`` and keeps it.
    """
    return _stamp(_resolve_from_levels(levels, calendar=calendar,
                                       default_span=default_span, basis=basis),
                  basis)


# --------------------------------------------------------------- date tokens
_MONTH_WORD = (r"(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|"
               r"aug(?:ust)?|sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)\b\.?")
_DATE_LIKE = re.compile(
    rf"\b\d{{1,2}}(?:st|nd|rd|th)?[\s\-]+{_MONTH_WORD},?[\s\-'\u2019]+\d{{2,4}}\b"
    rf"|\b{_MONTH_WORD}[\s\-]*\d{{1,2}}(?:st|nd|rd|th)?,?\s+\d{{4}}\b"
    rf"|\b{_MONTH_WORD}[\s\-'\u2019]+\d{{2,4}}\b"
    r"|\b\d{1,2}[./-]\d{1,2}[./-]\d{2,4}\b"
    r"|\b(?:q[1-4]|[1-4]q|h[12]|[12]h|9m|fy|cy|[jfmasond]q)\s?['\u2019]?\s?(?:fy\s?['\u2019]?)?\d{2,4}\b",
    re.I)


def date_like_spans(text: str) -> list[tuple[int, int]]:
    """Character spans of ``text`` that are dates or period labels.

    "December 31, 2025", "30 Jun 26", "31.03.2026", "Q3FY26", "JQ 25": every
    digit inside one of these belongs to a date, never to a figure. A prose
    or tile reader that takes "31" from "as at December 31, 2025" as the
    amount beside it has read the calendar, not the statement.
    """
    return [(m.start(), m.end()) for m in _DATE_LIKE.finditer(text or "")]


def is_date_like(text: str) -> bool:
    """True when the whole of ``text`` (ignoring edge punctuation) is a date."""
    t = (text or "").strip(" .,;:()[]")
    return any(a == 0 and b == len(t) for a, b in date_like_spans(t))
