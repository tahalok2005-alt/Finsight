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
    is_quarter_over_quarter, is_year_over_year, parse_period, period_distance,
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
        p = parse_period("quarter ended 30th June, 2026")
        assert p.label == "Q1 FY27"
        assert p.start_date == "2026-04-01" and p.end_date == "2026-06-30"

    def test_apostrophe_year_in_statement_header(self):
        p = parse_period("Quarter Ended 31st Mar'26")
        assert p.label == "Q4 FY26"
        assert p.end_date == "2026-03-31"

    def test_audited_parenthetical_is_tolerated(self):
        p = parse_period("Year Ended (Audited) 31st Mar'26")
        assert p.label == "FY26" and p.period_type == PeriodType.FISCAL_YEAR

    def test_bare_date_needs_a_span_to_become_a_quarter(self):
        """Without a span word the periodicity is unknown, not guessed."""
        assert parse_period("30th Jun'26").period_type == PeriodType.POINT_IN_TIME
        assert parse_period("30th Jun'26", default_span="quarter").label == "Q1 FY27"
        assert parse_period("31st Mar'26", default_span="year").label == "FY26"

    def test_fiscal_year_is_not_calendar_year(self):
        fy = parse_period("FY26")
        cy = parse_period("CY2026")
        assert fy.is_fiscal and not cy.is_fiscal
        assert fy.end_date == "2026-03-31"
        assert cy.end_date == "2026-12-31"

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
