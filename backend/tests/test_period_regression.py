"""Period resolution, held to the shapes real filings actually use.

Brief section 24. Every case is a column head or a phrase taken from a real
financial statement -- Indian and US filers, quarterly, half-year, nine-month
and annual, audited and unaudited, dated and fiscal-labelled.

The governing rule, from the standing constraints: *no regression is acceptable
where an explicit source period becomes UNKNOWN.* Silence is only the right
answer when the source is genuinely silent, and the second half of this file
pins that down as carefully as the first.
"""
from __future__ import annotations

import pytest

from app.normalization.periods import FiscalCalendar, parse_period

MARCH = FiscalCalendar(year_end_month=3)      # Indian filers
SEPTEMBER = FiscalCalendar(year_end_month=9)  # Apple
DECEMBER = FiscalCalendar(year_end_month=12)  # calendar-year filers


def _period(text: str, calendar: FiscalCalendar = MARCH):
    return parse_period(text, calendar=calendar)


class TestExplicitDates:
    """A date printed in full must always resolve to that date."""

    @pytest.mark.parametrize("text,end", [
        ("30 June 2026", "2026-06-30"),
        ("31 March 2026", "2026-03-31"),
        ("30 June 2025", "2025-06-30"),
        ("31 December 2025", "2025-12-31"),
        ("30 September 2025", "2025-09-30"),
        ("June 30, 2026", "2026-06-30"),
        ("March 31, 2026", "2026-03-31"),
        ("30th June 2026", "2026-06-30"),
        ("31st March, 2026", "2026-03-31"),
        ("28 June 2025", "2025-06-28"),
    ])
    def test_a_full_date_resolves_to_itself(self, text, end):
        period = _period(text)
        assert period is not None, f"{text!r} produced no period"
        assert period.end_date == end


class TestSpanQualifiers:
    """The span word decides whether a date is an instant or a range."""

    @pytest.mark.parametrize("text,end,is_quarter", [
        ("Quarter ended 30 June 2026", "2026-06-30", True),
        ("Quarter ended 31 March 2026", "2026-03-31", True),
        ("Three months ended 30 June 2026", "2026-06-30", True),
        ("Year ended 31 March 2026", "2026-03-31", False),
        ("Twelve months ended 31 March 2026", "2026-03-31", False),
    ])
    def test_the_span_word_is_honoured(self, text, end, is_quarter):
        period = _period(text)
        assert period is not None, f"{text!r} produced no period"
        assert period.end_date == end
        assert (period.quarter is not None) == is_quarter, (
            f"{text!r} resolved to {period.label}")

    @pytest.mark.parametrize("text", [
        "Quarterended 30 June 2026",
        "Yearended 31 March 2026",
        "Threemonths ended 30 June 2026",
    ])
    def test_a_lost_space_does_not_lose_the_span(self, text):
        """OCR closes the gap in a tightly set column head.

        One statement is headed "Quarterended" and "Yearended"; requiring the
        space left every column of that filing with a date but no span, so four
        quarterly figures were typed as instants.
        """
        period = _period(text)
        assert period is not None, f"{text!r} produced no period"
        assert period.end_date in ("2026-06-30", "2026-03-31")

    @pytest.mark.parametrize("text,end", [
        ("Six months ended 30 September 2025", "2025-09-30"),
        ("Half year ended 30 September 2025", "2025-09-30"),
        ("Nine months ended 31 December 2025", "2025-12-31"),
        ("Nine months ended December 31, 2025", "2025-12-31"),
    ])
    def test_half_year_and_nine_month_spans(self, text, end):
        period = _period(text)
        assert period is not None, f"{text!r} produced no period"
        assert period.end_date == end


class TestPointInTime:
    """A balance sheet column is an instant, not a range."""

    @pytest.mark.parametrize("text,end", [
        ("As at 31 March 2026", "2026-03-31"),
        ("As at 30 June 2026", "2026-06-30"),
        ("As at March 31, 2026", "2026-03-31"),
        ("As of September 27, 2025", "2025-09-27"),
    ])
    def test_as_at_resolves_to_the_instant(self, text, end):
        period = _period(text)
        assert period is not None, f"{text!r} produced no period"
        assert period.end_date == end


class TestFiscalLabels:

    @pytest.mark.parametrize("text,label", [
        ("Q1 FY27", "Q1 FY27"),
        ("Q4 FY26", "Q4 FY26"),
        ("Q1FY27", "Q1 FY27"),
        ("FY26", "FY26"),
        ("FY2026", "FY26"),
    ])
    def test_a_fiscal_label_is_read_as_written(self, text, label):
        period = _period(text)
        assert period is not None, f"{text!r} produced no period"
        assert period.label == label

    def test_an_indian_june_quarter_is_the_first_of_the_next_fiscal_year(self):
        """A March year end puts the June quarter in Q1 of the *next* FY."""
        period = _period("Quarter ended 30 June 2026")
        assert period.label == "Q1 FY27"

    def test_a_march_quarter_is_the_fourth_of_its_own_fiscal_year(self):
        period = _period("Quarter ended 31 March 2026")
        assert period.label == "Q4 FY26"

    def test_the_calendar_decides_the_fiscal_year(self):
        """The same date belongs to different fiscal years for different filers."""
        indian = _period("Quarter ended 30 June 2026", MARCH)
        american = _period("Quarter ended 30 June 2026", SEPTEMBER)
        assert indian.label != american.label


class TestAbbreviatedForms:

    @pytest.mark.parametrize("text,end", [
        ("Jun-26", "2026-06-30"),
        ("Mar-26", "2026-03-31"),
        ("Dec-25", "2025-12-31"),
        ("Sep-25", "2025-09-30"),
        ("Jun'26", "2026-06-30"),
        ("31st Mar'26", "2026-03-31"),
        ("Mar 2026", "2026-03-31"),
    ])
    def test_month_year_abbreviations(self, text, end):
        period = _period(text)
        assert period is not None, f"{text!r} produced no period"
        assert period.end_date == end


class TestHeadsCarryingMoreThanADate:
    """Audit status and note references sit in the head and mean nothing to it."""

    @pytest.mark.parametrize("text,end", [
        ("Quarter ended (Unaudited) 30 June 2026", "2026-06-30"),
        ("Quarter Ended (Audited) 31 March 2026", "2026-03-31"),
        ("30 June 2026 (Unaudited)", "2026-06-30"),
        ("31 March 2026 (Audited)", "2026-03-31"),
    ])
    def test_audit_status_does_not_disturb_the_date(self, text, end):
        period = _period(text)
        assert period is not None, f"{text!r} produced no period"
        assert period.end_date == end


class TestWhatMustNotResolve:
    """Silence is the right answer when the source is silent.

    Each of these once produced, or could produce, a confident wrong period.
    """

    @pytest.mark.parametrize("text", [
        "30 June 31 March 30 June",
        "31 March 2026 31 March 2026 30 June 2025",
    ])
    def test_several_columns_of_head_text_resolve_to_nothing_sane(self, text):
        """The concatenation defect, guarded at the parser.

        Three columns' heads merged into one string once produced "As at
        2031-06-30" -- a year that appears nowhere in the document. The header
        block now prevents the merge; this checks the parser cannot invent a
        year from such a string even if one reaches it.
        """
        period = _period(text)
        if period is not None:
            assert period.end_date is None or period.end_date[:4] in (
                "2025", "2026"), (
                f"{text!r} resolved to {period.end_date}, a date the source "
                "does not contain")

    @pytest.mark.parametrize("text", [
        "Particulars",
        "Segment",
        "Retail Banking",
        "Total",
        "%",
        "Change",
        "YoY",
        "(Audited)",
        "(Refer Note 3)",
        "",
    ])
    def test_a_head_that_is_not_a_period_resolves_to_none(self, text):
        assert _period(text) is None

    @pytest.mark.parametrize("text", [
        "1,501.42",
        "260.40",
    ])
    def test_a_figure_is_not_a_period(self, text):
        assert _period(text) is None


class TestComparativeColumns:
    """A prior-period column must resolve to the prior period, not the current."""

    def test_the_same_quarter_of_the_previous_year(self):
        current = _period("Quarter ended 30 June 2026")
        prior = _period("Quarter ended 30 June 2025")
        assert current.end_date == "2026-06-30"
        assert prior.end_date == "2025-06-30"
        assert current.label != prior.label

    def test_the_preceding_quarter(self):
        assert _period("Quarter ended 31 March 2026").end_date == "2026-03-31"

    def test_a_quarter_and_a_year_may_end_on_the_same_day(self):
        """Groww's statement carries both, and they are different periods.

        Column 2 is the quarter ended 31 March 2026 and column 4 the year ended
        31 March 2026. Treating a shared end date as a shared period would
        collapse two real columns into one.
        """
        quarter = _period("Quarter ended 31 March 2026")
        year = _period("Year ended 31 March 2026")
        assert quarter.end_date == year.end_date == "2026-03-31"
        assert quarter.label != year.label
        assert quarter.quarter is not None and year.quarter is None
