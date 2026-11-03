"""Number, unit, currency and period normalisation."""
from __future__ import annotations

import pytest

from app.models.enums import Currency, MagnitudeUnit, PeriodType, ValueType
from app.normalization.numbers import (
    convert, detect_currency, detect_magnitude, infer_document_units,
    looks_numeric, normalize_number, parse_number, to_base,
)
from app.normalization.periods import (
    DEFAULT_CALENDAR, FiscalCalendar, find_all_periods, infer_calendar,
    is_quarter_over_quarter, is_year_over_year, parse_period,
    parse_period_levels, period_distance,
)


class TestParseNumber:
    @pytest.mark.parametrize("text,expected", [
        ("340,257", 340257.0),
        ("1,175,919", 1175919.0),
        ("1,23,456", 123456.0),        # Indian digit grouping
        ("24.5", 24.5),
        ("0.57", 0.57),
        ("12 450", 12450.0),
    ])
    def test_positive_forms(self, text, expected):
        parsed = parse_number(text)
        assert parsed is not None
        assert parsed[0] == pytest.approx(expected)

    def test_parentheses_mean_negative(self):
        value, negative, from_paren = parse_number("(1,234)")
        assert value == -1234.0
        assert negative and from_paren

    def test_leading_minus(self):
        value, negative, from_paren = parse_number("-450")
        assert value == -450.0
        assert negative and not from_paren

    def test_dash_is_nil_not_missing(self):
        assert parse_number("-")[0] == 0.0
        assert parse_number("–")[0] == 0.0

    def test_non_numeric_returns_none(self):
        assert parse_number("Gross Revenue") is None
        assert parse_number("") is None
        assert parse_number(None) is None


class TestLooksNumeric:
    @pytest.mark.parametrize("text", ["340,257", "24.5%", "(210 bps)", "-",
                                      "0.57", "1,175,919"])
    def test_numeric_cells(self, text):
        assert looks_numeric(text)

    @pytest.mark.parametrize("text", [
        "Gross Revenue", "Profit Before Tax",
        "Revenue increased 24.5% to Rs 340,257 crore", ""])
    def test_non_numeric_cells(self, text):
        assert not looks_numeric(text)


class TestUnitsAndCurrency:
    @pytest.mark.parametrize("text,unit", [
        ("in Rs crore", MagnitudeUnit.CRORE),
        ("(in lakh)", MagnitudeUnit.LAKH),
        ("US$ million", MagnitudeUnit.MILLION),
        ("$ bn", MagnitudeUnit.BILLION),
        ("figures in thousand", MagnitudeUnit.THOUSAND),
    ])
    def test_magnitude_detection(self, text, unit):
        assert detect_magnitude(text) == unit

    @pytest.mark.parametrize("text,cur", [
        ("₹ 340,257", Currency.INR),
        ("Rs. 500", Currency.INR),
        ("$12.5", Currency.USD),
        ("EUR 40", Currency.EUR),
    ])
    def test_currency_detection(self, text, cur):
        assert detect_currency(text)[0] == cur

    def test_unit_is_never_invented(self):
        """A bare number with no unit context keeps magnitude NONE."""
        n = normalize_number("12,345")
        assert n.magnitude == MagnitudeUnit.NONE
        assert n.base_value is None

    def test_context_supplies_unit_but_token_wins(self):
        n = normalize_number("500 million", context="(in Rs crore)")
        assert n.magnitude == MagnitudeUnit.MILLION  # token beats context

    def test_raw_text_is_preserved(self):
        n = normalize_number("(1,234)", context="(in Rs crore)")
        assert n.raw_text == "(1,234)"
        assert n.value == -1234.0
        assert n.magnitude == MagnitudeUnit.CRORE
        assert n.base_value == pytest.approx(-1234.0 * 1e7)

    def test_percentage_gets_no_currency_or_magnitude(self):
        n = normalize_number("24.5%", context="(in Rs crore)")
        assert n.value_type == ValueType.PERCENTAGE
        assert n.currency == Currency.UNKNOWN
        assert n.magnitude == MagnitudeUnit.PERCENT
        assert n.base_value is None

    def test_context_cannot_retype_a_plain_number(self):
        """A nearby mention of bps must not turn a revenue figure into bps."""
        n = normalize_number("340,257",
                             context="EBITDA margin up 150 bps Y-o-Y")
        assert n.value_type != ValueType.BPS

    def test_expected_type_applies_only_when_token_is_silent(self):
        assert normalize_number("15.9", expected_type=ValueType.PERCENTAGE
                                ).value_type == ValueType.PERCENTAGE
        assert normalize_number("15.9%", expected_type=ValueType.MONETARY
                                ).value_type == ValueType.PERCENTAGE

    def test_conversion(self):
        assert to_base(1.0, MagnitudeUnit.CRORE) == 1e7
        assert convert(1.0, MagnitudeUnit.CRORE, MagnitudeUnit.MILLION) == 10.0
        assert convert(1.0, MagnitudeUnit.PERCENT, MagnitudeUnit.CRORE) is None

    def test_infer_document_units(self):
        cur, unit = infer_document_units(
            ["Consolidated Financial Highlights", "(₹ in crore)",
             "Some other text"])
        assert cur == Currency.INR
        assert unit == MagnitudeUnit.CRORE


#: A filer whose year ends in March, stated rather than assumed.  Tests that
#: are about fiscal labelling must supply one: the parser will not invent a
#: fiscal year for a document that never gave its year end.
MARCH = FiscalCalendar(year_end_month=3)


class TestPeriods:
    @pytest.mark.parametrize("text,label,ptype", [
        ("1Q FY27", "Q1 FY27", PeriodType.QUARTER),
        ("Q1 FY2027", "Q1 FY27", PeriodType.QUARTER),
        ("4Q FY26", "Q4 FY26", PeriodType.QUARTER),
        ("FY26", "FY26", PeriodType.FISCAL_YEAR),
        ("FY2026", "FY26", PeriodType.FISCAL_YEAR),
        ("H1 FY26", "H1 FY26", PeriodType.HALF_YEAR),
        ("9M FY26", "9M FY26", PeriodType.NINE_MONTH),
        ("CY2025", "CY2025", PeriodType.CALENDAR_YEAR),
    ])
    def test_labels(self, text, label, ptype):
        p = parse_period(text)
        assert p is not None and p.label == label and p.period_type == ptype

    def test_quarter_ended_prose(self):
        p = parse_period("quarter ended 30th June, 2026", calendar=MARCH)
        assert p.label == "Q1 FY27"
        assert p.start_date == "2026-04-01" and p.end_date == "2026-06-30"

    def test_apostrophe_year_in_statement_header(self):
        p = parse_period("Quarter Ended 31st Mar'26", calendar=MARCH)
        assert p.label == "Q4 FY26"
        assert p.end_date == "2026-03-31"

    def test_audited_parenthetical_is_tolerated(self):
        p = parse_period("Year Ended (Audited) 31st Mar'26", calendar=MARCH)
        assert p.label == "FY26" and p.period_type == PeriodType.FISCAL_YEAR

    def test_bare_date_needs_a_span_to_become_a_quarter(self):
        """Without a span word the periodicity is unknown, not guessed."""
        assert parse_period("30th Jun'26").period_type == PeriodType.POINT_IN_TIME
        assert parse_period("30th Jun'26", calendar=MARCH,
                            default_span="quarter").label == "Q1 FY27"
        assert parse_period("31st Mar'26", calendar=MARCH,
                            default_span="year").label == "FY26"

    def test_fiscal_year_is_not_calendar_year(self):
        fy = parse_period("FY26", calendar=MARCH)
        cy = parse_period("CY2026", calendar=MARCH)
        assert fy.is_fiscal and not cy.is_fiscal
        assert fy.end_date == "2026-03-31"
        assert cy.end_date == "2026-12-31"

    def test_a_date_is_not_given_a_fiscal_year_the_document_never_stated(self):
        """The year end is what turns a date into a fiscal label.

        Without it the date is still known exactly, and saying so is honest;
        calling 30 June 2026 "Q1 FY27" would be a guess about a filer whose
        year might close in any month.
        """
        p = parse_period("quarter ended 30th June, 2026")   # no calendar given
        assert p.end_date == "2026-06-30"
        assert p.fiscal_year is None and not p.is_fiscal
        assert p.label == "3M ended 2026-06-30"

    def test_an_explicit_fiscal_label_survives_an_unknown_calendar(self):
        """"FY26" written on the page is the document's own statement.

        It is a fact about the filing, not something derived from a date, so an
        unknown year end must not suppress it -- only the calendar dates it
        would span, which genuinely are unknown.
        """
        p = parse_period("FY26")                            # no calendar given
        assert p.label == "FY26" and p.fiscal_year == 2026 and p.is_fiscal
        assert p.start_date is None and p.end_date is None
        assert parse_period("Q1 FY27").label == "Q1 FY27"
        assert parse_period("9M FY26").label == "9M FY26"
        assert parse_period("CY2025").end_date == "2025-12-31"

    def test_no_period_returns_none(self):
        assert parse_period("Gross Revenue") is None
        assert parse_period("") is None

    def test_calendar_inference(self):
        cal = infer_calendar(
            ["1Q FY27 results for the quarter ended 30th June, 2026"])
        assert cal.year_end_month == 3

    def test_december_year_end_calendar(self):
        cal = FiscalCalendar(year_end_month=12)
        p = parse_period("1Q FY26", calendar=cal)
        assert p.start_date == "2026-01-01" and p.end_date == "2026-03-31"

    def test_distance_and_comparison(self):
        q1_27 = parse_period("1Q FY27")
        q1_26 = parse_period("1Q FY26")
        q4_26 = parse_period("4Q FY26")
        assert period_distance(q1_27, q1_26) == 4
        assert is_year_over_year(q1_27, q1_26)
        assert is_quarter_over_quarter(q1_27, q4_26)
        assert not is_year_over_year(q1_27, q4_26)

    def test_find_all_periods(self):
        found = find_all_periods("Comparing 1Q FY27 with 1Q FY26 and FY26")
        labels = {p.label for p in found}
        assert {"Q1 FY27", "Q1 FY26", "FY26"} <= labels


class TestHierarchicalPeriodHeads:
    """The column head is a *stack*, not a line.

    A statement writes "Three Months Ended" across two columns, the day and
    month under it, and the year on a third row.  No single line of that is a
    period, and flattening the three to one string only works when the
    fragments happen to land in a parseable order.  Each test here pins one
    shape that a real filing prints.
    """

    SEPT = FiscalCalendar(year_end_month=9)

    def test_a_head_split_over_three_rows_resolves(self):
        p = parse_period_levels(["Three Months Ended", "June 28,", "2025"],
                                calendar=self.SEPT)
        assert p.label == "Q3 FY25"
        assert p.start_date == "2025-04-01" and p.end_date == "2025-06-28"

    def test_a_span_banner_is_read_from_whichever_row_states_it(self):
        """The banner sits two levels above the year, never beside it."""
        nine = parse_period_levels(["Nine Months Ended", "June 28,", "2025"],
                                   calendar=self.SEPT)
        assert nine.label == "9M FY25" and nine.end_date == "2025-06-28"
        assert nine.period_type == PeriodType.NINE_MONTH

    def test_a_year_wrapped_onto_its_own_row_rejoins_its_date(self):
        """"June 28," and "2025" are separate OCR lines and separate levels."""
        p = parse_period_levels(["Year Ended", "March 31,", "2026"],
                                calendar=MARCH)
        assert p.label == "FY26" and p.end_date == "2026-03-31"

    def test_a_bare_year_row_alone_is_not_a_period(self):
        assert parse_period_levels(["2025"], calendar=self.SEPT) is None
        assert parse_period_levels([], calendar=self.SEPT) is None

    def test_a_date_with_no_span_anywhere_is_an_instant(self):
        """A balance sheet heads its columns with a date and nothing else.

        Reading that as a quarter would date a stock as though it were a flow,
        which is wrong in a way no later stage can detect.
        """
        p = parse_period_levels(["June 28,", "2025"], calendar=self.SEPT)
        assert p.period_type == PeriodType.POINT_IN_TIME
        assert p.label == "As at 2025-06-28" and p.end_date == "2025-06-28"

    def test_as_at_is_an_instant_even_beneath_a_span_word(self):
        p = parse_period_levels(["As at", "March 31, 2026"], calendar=MARCH)
        assert p.period_type == PeriodType.POINT_IN_TIME
        assert p.end_date == "2026-03-31"

    def test_a_stacked_label_means_what_the_join_says(self):
        """"1Q" over "FY27" is one label printed on two rows."""
        assert parse_period_levels(["1Q", "FY27"], calendar=MARCH).label == "Q1 FY27"

    def test_a_leaf_label_beats_the_banner_above_it(self):
        """A banner says what a group of columns shares; a leaf says what this
        column is.  Where a page is titled "results for the quarter ended 30th
        June, 2026" and the columns are headed "1Q FY27" and "1Q FY26", both
        columns must not collapse onto the banner's own period."""
        banner = "CONSOLIDATED RESULTS FOR QUARTER ENDED 30TH JUNE, 2026"
        current = parse_period_levels([banner, "1Q FY27"], calendar=MARCH)
        prior = parse_period_levels([banner, "1QFY26"], calendar=MARCH)
        assert current.label == "Q1 FY27" and prior.label == "Q1 FY26"

    def test_a_day_of_month_is_never_read_as_a_fiscal_year(self):
        """"June 28," with the year on the next level must not yield FY28."""
        p = parse_period_levels(["Three Months Ended", "June 28,", "2025"],
                                calendar=self.SEPT)
        assert p.fiscal_year == 2025
        assert parse_period_levels(["Three Months Ended", "June 28,"],
                                   calendar=self.SEPT) is None

    def test_an_inherited_span_may_be_any_length(self):
        """A banner typeset too narrow to reach its last column leaves that
        column a bare date; the span its siblings demonstrate is what it
        inherits, and that span is not always a quarter."""
        p = parse_period_levels(["June 29,", "2024"], calendar=self.SEPT,
                                default_span="nine months")
        assert p.label == "9M FY24" and p.end_date == "2024-06-29"


class TestUnknownFiscalCalendar:
    """What a document never states must not be invented.

    A filing that prints exact dates but never its year end can be dated
    exactly and cannot be labelled fiscally.  Reporting the date and leaving
    the fiscal year unset is incomplete; reporting a guessed fiscal year is
    wrong, and wrong is worse.
    """

    UNKNOWN = FiscalCalendar(year_end_month=3, is_known=False)

    def test_the_default_calendar_is_marked_unknown(self):
        assert DEFAULT_CALENDAR.is_known is False
        assert FiscalCalendar(year_end_month=3).is_known is True

    def test_a_derived_period_keeps_its_dates_and_drops_the_fiscal_year(self):
        p = parse_period_levels(["Three Months Ended", "June 28,", "2025"],
                                calendar=self.UNKNOWN)
        assert p.end_date == "2025-06-28" and p.start_date == "2025-04-01"
        assert p.fiscal_year is None and p.is_fiscal is False
        assert p.label == "3M ended 2025-06-28"
        assert p.confidence < 0.9

    def test_a_label_this_module_emitted_reads_back_unchanged(self):
        """The document-level repair pass re-parses labels it was given, so a
        truthful label must survive the round trip rather than decay."""
        for label in ("3M ended 2025-06-28", "9M ended 2024-06-29",
                      "As at 2025-06-28"):
            back = parse_period(label, calendar=self.UNKNOWN)
            assert back is not None and back.label == label

    def test_an_instant_stays_an_instant(self):
        p = parse_period_levels(["September 28,", "2024"], calendar=self.UNKNOWN)
        assert p.period_type == PeriodType.POINT_IN_TIME
        assert p.end_date == "2024-09-28" and p.fiscal_year is None

    def test_an_explicitly_written_fiscal_label_still_stands(self):
        """The document naming its own year is a fact about the filing, not an
        inference from a date, so an unknown year end must not suppress it."""
        for text, label in [("FY26", "FY26"), ("Q1 FY27", "Q1 FY27"),
                            ("H1 FY26", "H1 FY26"), ("9M FY26", "9M FY26")]:
            p = parse_period(text, calendar=self.UNKNOWN)
            assert p is not None and p.label == label
            # the dates it spans, however, are genuinely unknown
            assert p.start_date is None and p.end_date is None

    def test_a_calendar_year_needs_no_fiscal_calendar(self):
        p = parse_period("CY2025", calendar=self.UNKNOWN)
        assert p.start_date == "2025-01-01" and p.end_date == "2025-12-31"


class TestPeriodGeneralization:
    """Pins two defects found when validating against a non-Indian filer.

    Both are generic bugs, not company-specific ones: the first invented a
    fiscal year out of a day-of-month, the second assumed a March year end for
    a filer that never writes "Q<n> FY<yy>".
    """

    def test_a_day_of_month_is_never_read_as_a_fiscal_year(self):
        """"Three Months Ended June 28," -- year on the next line of the
        header -- must resolve to nothing, not to fiscal year 2028."""
        from app.normalization.periods import FiscalCalendar, parse_period
        cal = FiscalCalendar(year_end_month=9)
        assert parse_period("Three Months Ended June 28,", calendar=cal) is None
        assert parse_period("Nine Months Ended June 28,", calendar=cal) is None

    def test_an_elided_two_digit_year_still_parses(self):
        """The guard must not break "31st Mar'26", where a day is captured
        before the month so the trailing 26 is unambiguously a year."""
        from app.normalization.periods import FiscalCalendar, parse_period
        cal = FiscalCalendar(year_end_month=3)
        assert parse_period("Quarter Ended 31st Mar'26", calendar=cal).label == "Q4 FY26"
        assert parse_period("Year Ended (Audited) 31st Mar'26",
                            calendar=cal).label == "FY26"

    def test_full_dates_parse_under_either_convention(self):
        from app.normalization.periods import FiscalCalendar, parse_period
        sept = FiscalCalendar(year_end_month=9)
        march = FiscalCalendar(year_end_month=3)
        assert parse_period("Three Months Ended June 28, 2025",
                            calendar=sept).label == "Q3 FY25"
        assert parse_period("Three Months Ended June 28, 2025",
                            calendar=march).label == "Q1 FY26"

    def test_a_cumulative_span_pins_the_fiscal_year_end(self):
        """Nine months ending in June means the year began in October, so it
        ends in September -- arithmetic, with no regional assumption."""
        from app.normalization.periods import infer_calendar
        assert infer_calendar(
            ["Nine Months Ended June 28, 2025"]).year_end_month == 9
        assert infer_calendar(
            ["Twelve months ended March 31, 2026"]).year_end_month == 3
        assert infer_calendar(
            ["Six months ended September 30, 2025"]).year_end_month == 3

    def test_a_three_month_span_settles_nothing(self):
        """A quarter ending in June is Q1 under a March year end and Q3 under
        a September one, so it must not vote."""
        from app.normalization.periods import DEFAULT_CALENDAR, infer_calendar
        assert (infer_calendar(["Three Months Ended June 28, 2025"]).year_end_month
                == DEFAULT_CALENDAR.year_end_month)

    def test_a_banner_and_date_on_different_lines_do_not_vote(self):
        """A statement header stacks "Quarter Ended" and "Year Ended" above
        four dated columns.  In extracted reading order a regex will pair one
        column's banner with another column's date; that mispairing once made
        a March-year-end filer infer a June year end, shifting every period by
        a quarter.  Only a phrase written on one line is evidence."""
        from app.normalization.periods import DEFAULT_CALENDAR, infer_calendar
        mispaired = ["Year Ended \n(Audited) \n30th Jun26"]
        assert (infer_calendar(mispaired).year_end_month
                == DEFAULT_CALENDAR.year_end_month)
        # the same phrase on one line is trusted
        assert infer_calendar(
            ["Nine Months Ended June 28, 2025"]).year_end_month == 9

    def test_contradictory_span_evidence_is_ignored(self):
        """A statement header block can pair one column's banner with another
        column's date.  Disagreeing readings are not evidence."""
        from app.normalization.periods import DEFAULT_CALENDAR, infer_calendar
        contradictory = ["Year Ended 30th Jun'26 ... Year Ended March 31, 2026"]
        assert (infer_calendar(contradictory).year_end_month
                == DEFAULT_CALENDAR.year_end_month)

    def test_explicit_quarter_markers_win_over_the_span_fallback(self):
        """Where a filing writes "Q1 FY27 ... quarter ended 30th June, 2026",
        that pairing is the stronger signal and settles the calendar first."""
        from app.normalization.periods import infer_calendar
        assert infer_calendar(
            ["Q1 FY27 results for the quarter ended 30th June, 2026"]
        ).year_end_month == 3
