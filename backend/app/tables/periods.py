"""Document-level repair of table column periods.

A statement column's header is often only a date -- ``30th Jun'26``.  What that
date *means* comes from a group header above it, "Quarter Ended" or "Year
Ended", and that qualifier is routinely lost:

* the statement continues onto the next page, which repeats the date row but
  not the span banner, so every column degrades to a point in time;
* one span banner covers three columns and a second covers the fourth, but the
  banner cell binds to only one column, so the annual column inherits
  "quarter" and collides with the actual quarter.

Both are repaired here, at document level, using only what the document itself
shows:

* **Harmonisation** -- if the same header text resolved to a real duration on
  another table of the same document, that meaning is adopted.  The document
  has already demonstrated what the header means.
* **Collision** -- two value columns of one table cannot denote the same
  period.  When they do, a span qualifier was lost; if the table names a year
  span, the colliding column that says so (or the rightmost) becomes that
  fiscal year.

Nothing here invents a period.  A column that cannot be resolved by either rule
keeps whatever it had, including nothing.
"""
from __future__ import annotations

import re
from collections import Counter

from app.core.logging import get_logger
from app.models.enums import PeriodBasis, PeriodType
from app.models.schemas import ExtractedTable, NormalizedPeriod
from app.normalization.periods import (
    FiscalCalendar, parse_period, parse_period_levels,
)

log = get_logger("tables.periods")

_YEAR_MARKER = re.compile(r"year\s+ended|twelve\s+months?|full\s+year|audited",
                          re.I)
_DURATIONS = (PeriodType.QUARTER, PeriodType.HALF_YEAR, PeriodType.NINE_MONTH,
              PeriodType.FISCAL_YEAR, PeriodType.CALENDAR_YEAR, PeriodType.TTM)


def _normalise(header: str) -> str:
    """Reduce a header to its comparable core, dropping span words.

    "Quarter Ended 31st Mar'26" and "31st Mar'26" must compare equal, because
    the point of harmonisation is to carry the first one's meaning to the
    second.
    """
    text = (header or "").lower()
    text = re.sub(r"quarter\s+ended|year\s+ended|three\s+months?\s+ended|"
                  r"twelve\s+months?\s+ended|full\s+year|\(audited\)|"
                  r"\(unaudited\)", " ", text)
    text = re.sub(r"[^a-z0-9]+", "", text)
    return text


def _is_duration(label: str | None, calendar: FiscalCalendar) -> bool:
    if not label:
        return False
    period = parse_period(label, calendar=calendar)
    return period is not None and PeriodType(period.period_type) in _DURATIONS


def harmonise_periods(tables: list[ExtractedTable], *,
                      calendar: FiscalCalendar) -> dict:
    """Repair column periods across a document's tables, in place."""
    stats = {"harmonised": 0, "collisions_repaired": 0,
             "spans_inherited": 0}

    # ---- 1. a lone point-in-time column among durations -------------------
    # Evidence from inside the table is settled before evidence borrowed
    # from other tables: a sibling column under the same banner says more
    # about this column than a same-dated column on another page does.
    # A column left as a bare date in a table whose other value columns are
    # durations lost the same span qualifier they kept -- typically because the
    # date was typeset with a different apostrophe and so matched nothing.  It
    # takes the periodicity its siblings demonstrate, but never one that would
    # collide with a column already resolved: a collision means the span is
    # genuinely unknown, and a wrong period is worse than none.
    for table in tables:
        durations = [c for c in table.columns
                     if not c.is_growth_column
                     and _is_duration(c.normalized_period, calendar)]
        if not durations:
            continue
        spans = Counter()
        for column in durations:
            period = parse_period(column.normalized_period, calendar=calendar)
            if period is not None:
                spans[PeriodType(period.period_type)] += 1
        if not spans:
            continue
        dominant = spans.most_common(1)[0][0]
        span_word = {PeriodType.QUARTER: "quarter",
                     PeriodType.HALF_YEAR: "half year",
                     PeriodType.NINE_MONTH: "nine months",
                     PeriodType.FISCAL_YEAR: "year",
                     PeriodType.CALENDAR_YEAR: "year"}.get(dominant)
        if span_word is None:
            continue
        taken = {c.normalized_period for c in durations}
        for column in table.columns:
            if (column.is_growth_column or not column.header
                    or _is_duration(column.normalized_period, calendar)):
                continue
            if not column.normalized_period:
                continue          # never invent a period for an unlabelled column
            period = parse_period_levels(list(column.header_path or []),
                                         calendar=calendar,
                                         default_span=span_word)
            if period is None:
                period = parse_period(column.header, calendar=calendar,
                                      default_span=span_word)
            if period is None or period.label in taken:
                continue
            log.info("tables.period_span_inherited", table_id=table.id,
                     column=column.index, header=column.header,
                     was=column.normalized_period, now=period.label,
                     span=span_word)
            # borrowed from a sibling column under the same banner
            period.basis = PeriodBasis.INHERITED
            column.normalized_period = period.label
            column.period_resolved = period
            taken.add(period.label)
            stats["spans_inherited"] = stats.get("spans_inherited", 0) + 1

    # ---- 2. what each header text means, where the document made it plain --
    meaning: dict[str, Counter] = {}
    #: the resolved period behind each agreed label, so a harmonised column can
    #: borrow the source's dates rather than only its wording
    examples: dict[tuple[str, str], NormalizedPeriod] = {}
    for table in tables:
        for column in table.columns:
            if not column.header or not column.normalized_period:
                continue
            if not _is_duration(column.normalized_period, calendar):
                continue
            key = _normalise(column.header)
            if key:
                meaning.setdefault(key, Counter())[column.normalized_period] += 1
                if column.period_resolved is not None:
                    examples.setdefault((key, column.normalized_period),
                                        column.period_resolved)

    resolved = {key: counts.most_common(1)[0][0]
                for key, counts in meaning.items()
                if len(counts) == 1}

    for table in tables:
        # Comparing headers with their span words stripped is what lets a
        # mangled "Quarter Ended" be repaired -- but it also makes a balance
        # sheet's "June 28, 2025" look identical to an income statement's
        # "Three Months Ended June 28, 2025", which is a different period
        # entirely.  What tells the two apart is whether the *whole* header row
        # is recognised: a statement continued repeats all of its heads, while
        # a different statement merely happens to share one date.  A partial
        # match is a coincidence, and acting on it invents a period.
        heads = [c for c in table.columns
                 if c.header and not c.is_growth_column
                 and c.normalized_period]
        if not heads or not all(_normalise(c.header) in resolved
                                or _is_duration(c.normalized_period, calendar)
                                for c in heads):
            continue
        for column in table.columns:
            if not column.header:
                continue
            if _is_duration(column.normalized_period, calendar):
                continue
            key = _normalise(column.header)
            better = resolved.get(key)
            if better and better != column.normalized_period:
                log.info("tables.period_harmonised", table_id=table.id,
                         column=column.index, header=column.header,
                         was=column.normalized_period, now=better)
                column.normalized_period = better
                # The label came from a matching header elsewhere in the
                # document; the dates travel with it where they exist.
                source = examples.get((key, better))
                if source is not None:
                    borrowed = source.model_copy()
                    borrowed.basis = PeriodBasis.INHERITED
                    column.period_resolved = borrowed
                else:
                    column.period_resolved = None
                stats["harmonised"] += 1

    # ---- 3. two value columns cannot be the same period -------------------
    for table in tables:
        seen: dict[str, int] = {}
        header_blob = " ".join(c.header or "" for c in table.columns)
        table_names_year = bool(_YEAR_MARKER.search(
            f"{header_blob} {table.title or ''} {table.caption or ''}"))
        for column in table.columns:
            label = column.normalized_period
            if not label or column.is_growth_column:
                continue
            if label not in seen:
                seen[label] = column.index
                continue

            # A collision: decide which of the two is the annual column.
            first_index = seen[label]
            first = next(c for c in table.columns if c.index == first_index)
            annual = None
            if _YEAR_MARKER.search(column.header or ""):
                annual = column
            elif _YEAR_MARKER.search(first.header or ""):
                annual = first
            elif table_names_year:
                annual = column          # the rightmost, by reading order
            if annual is None:
                continue

            period = parse_period(annual.header or "", calendar=calendar,
                                  default_span="year")
            if period is None or period.label == label:
                continue
            log.info("tables.period_collision_repaired", table_id=table.id,
                     column=annual.index, header=annual.header,
                     was=label, now=period.label)
            period.basis = PeriodBasis.INHERITED
            annual.normalized_period = period.label
            annual.period_resolved = period
            stats["collisions_repaired"] += 1
            if annual is not column:
                seen[label] = column.index

    if any(stats.values()):
        log.info("tables.periods_repaired", **stats)
    return stats


# --------------------------------------------------- calendar from headers
def _span_length(period) -> int | None:
    """How many months a resolved period covers, when it states a duration."""
    return {PeriodType.QUARTER: 3, PeriodType.HALF_YEAR: 6,
            PeriodType.NINE_MONTH: 9, PeriodType.FISCAL_YEAR: 12,
            PeriodType.CALENDAR_YEAR: 12,
            }.get(PeriodType(period.period_type))


#: A span phrase covering a full year, as a column head states it.
_YEAR_SPAN = re.compile(
    r"\b(?:year|twelve\s+months?|full\s+year)\s+end(?:ed|ing)\b", re.I)


def infer_calendar_from_headers(tables: list[ExtractedTable],
                                ) -> FiscalCalendar | None:
    """Establish the fiscal year end from reconstructed column heads.

    Inferring the year end by pattern-matching flowing text is unreliable,
    because a statement's header block stacks banners above dates and the two
    only pair up correctly once the table has been reconstructed.  A column
    whose *hierarchy* reads "Year Ended" over "31st Mar'26" states the year end
    outright: the annual period closes in March.  No cross-line guessing is
    involved, because the hierarchy already settled which banner governs which
    date.

    An interim filing that never prints "Year Ended" states its year end a
    second way.  When a statement shows a cumulative column -- "Nine Months
    Ended June 28, 2025" -- beside a shorter one closing on the *same* date --
    "Three Months Ended June 28, 2025" -- the longer column is the year to
    date, because that is what an interim statement's cumulative column is: the
    quarter is the last stub of it.  Nine months of the year having closed on
    28 June puts the year end three months later, in September.  The shorter
    column ending on the same date is what makes this an observation rather
    than an assumption; without it the span could be a rolling window.

    Returns None unless every such column agrees.  Contradictory evidence is
    not evidence, and a wrong calendar mislabels every period in the document.
    """
    votes: Counter = Counter()
    #: every (span in months, end date) pair the document actually printed
    spans: set[tuple[int, str]] = set()
    for table in tables:
        for column in table.columns:
            levels = list(column.header_path or [])
            if not levels:
                continue
            period = parse_period_levels(levels,
                                         calendar=FiscalCalendar(3, False))
            if period is None or not period.end_date:
                continue
            months = _span_length(period)
            if months:
                spans.add((months, period.end_date))
            if not _YEAR_SPAN.search(" ".join(levels)):
                continue
            try:
                month = int(period.end_date.split("-")[1])
            except (IndexError, ValueError):
                continue
            votes[month] += 1

    # the cumulative-column reading, as described above
    for months, end in spans:
        if not 3 < months < 12:
            continue
        if not any(m < months and e == end for m, e in spans):
            continue          # nothing proves this column is cumulative
        try:
            end_month = int(end.split("-")[1])
        except (IndexError, ValueError):
            continue
        votes[((end_month + (12 - months) - 1) % 12) + 1] += 1

    if len(votes) != 1:
        if votes:
            log.info("tables.calendar_evidence_contradictory",
                     votes=dict(votes))
        return None
    month = next(iter(votes))
    log.info("tables.calendar_from_headers", year_end_month=month,
             columns=votes[month])
    return FiscalCalendar(year_end_month=month, is_known=True)


def reresolve_column_periods(tables: list[ExtractedTable], *,
                             calendar: FiscalCalendar) -> int:
    """Re-read every column head under a newly established calendar."""
    changed = 0
    for table in tables:
        for column in table.columns:
            levels = list(column.header_path or [])
            if not levels:
                continue
            period = parse_period_levels(levels, calendar=calendar)
            if period is not None and period.label != column.normalized_period:
                column.normalized_period = period.label
                column.period_label = period.raw_text
                column.period_resolved = period
                changed += 1
    if changed:
        log.info("tables.periods_reresolved", columns=changed,
                 year_end_month=calendar.year_end_month)
    return changed
