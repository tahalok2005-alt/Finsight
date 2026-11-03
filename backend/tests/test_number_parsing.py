"""Reading a printed figure as the number it is.

The cases here are all real cell text from the corpus. Two of them are
recognition errors the parser repairs, and the rest are ordinary figures that
must survive the repair untouched -- which is the harder half, because a repair
that fires too eagerly is the same magnitude of error in the other direction
and lands on far more figures.
"""
from __future__ import annotations

import pytest

from app.normalization.numbers import parse_number, repair_misread_separators


def value(text: str) -> float | None:
    out = parse_number(text)
    return out[0] if out else None


class TestFiguresPrintedWithoutSeparators:
    """One bank prints its statement unseparated. Every figure was truncated.

    The number pattern's separated form used a star, so it also matched the
    leading three digits of "14272.21" -- and alternation is ordered, so it
    won. The value became 142.0: a hundredfold error, silent, and plausible
    enough to read as a real figure.
    """

    @pytest.mark.parametrize("text,expected", [
        ("14272.21", 14272.21),
        ("13923.99", 13923.99),
        ("79362.78", 79362.78),
        ("92184.38", 92184.38),
        ("307522.08", 307522.08),
        ("1501.42", 1501.42),
        ("239762.46", 239762.46),
    ])
    def test_an_unseparated_figure_keeps_all_its_digits(self, text, expected):
        assert value(text) == pytest.approx(expected)


class TestFiguresPrintedWithSeparators:

    @pytest.mark.parametrize("text,expected", [
        ("1,501.42", 1501.42),
        ("123,456.78", 123456.78),
        ("1,23,456.78", 123456.78),      # Indian grouping
        ("2,083.01", 2083.01),
        ("8,403.55", 8403.55),
        ("1 234.56", 1234.56),           # thin space
    ])
    def test_a_separated_figure_still_parses(self, text, expected):
        assert value(text) == pytest.approx(expected)


class TestMisreadSeparators:
    """OCR reads a comma as a full stop in small type."""

    @pytest.mark.parametrize("text,expected", [
        ("1.053.69", 1053.69),
        ("1.050.91", 1050.91),
        ("3.332.76", 3332.76),
        ("8.403.55", 8403.55),
        ("1.234.567", 1234567.0),
    ])
    def test_a_doubled_stop_is_repaired(self, text, expected):
        assert value(text) == pytest.approx(expected)

    @pytest.mark.parametrize("text,expected", [
        ("1.053", 1.053),
        ("12.345", 12.345),
        ("1.5", 1.5),
        ("0.66", 0.66),
        ("3.47", 3.47),
        ("260.40", 260.40),
    ])
    def test_a_single_stop_is_a_decimal_point(self, text, expected):
        assert repair_misread_separators(text) is None
        assert value(text) == pytest.approx(expected)


class TestAccountingConventions:

    @pytest.mark.parametrize("text,expected", [
        ("(19.21)", -19.21),
        ("(0.72)", -0.72),
        ("(31.83)", -31.83),
    ])
    def test_parentheses_mean_negative(self, text, expected):
        assert value(text) == pytest.approx(expected)

    def test_a_bare_dash_is_nil(self):
        assert value("-") == 0.0


class TestDecimalComma:
    """A comma standing where the decimal point belongs.

    A thousands separator is always followed by exactly three digits, so a
    comma with one or two digits and nothing after it cannot be one. Both the
    European convention and an OCR misreading of a full stop produce this.

    It matters twice over: the value is wrong without the repair, and the
    ambiguous-cell guard would otherwise see two numbers where there is one and
    discard the figure entirely -- which is how a bank's total expenditure of
    62,005.98 crore came to be thrown away.
    """

    @pytest.mark.parametrize("text,expected", [
        ("62005,98", 62005.98),
        ("249,34", 249.34),
        ("1234,5", 1234.5),
    ])
    def test_a_decimal_comma_is_read_as_a_decimal_point(self, text, expected):
        assert value(text) == pytest.approx(expected)

    @pytest.mark.parametrize("text,expected", [
        ("1,234", 1234.0),
        ("12,34,567", 1234567.0),
        ("1,501.42", 1501.42),
    ])
    def test_a_thousands_comma_is_still_a_thousands_comma(self, text, expected):
        assert value(text) == pytest.approx(expected)

    def test_a_decimal_comma_is_not_two_numbers(self):
        from app.normalization.numbers import holds_more_than_one_number
        assert not holds_more_than_one_number("62005,98")

    def test_two_figures_in_one_cell_still_are(self):
        from app.normalization.numbers import holds_more_than_one_number
        assert holds_more_than_one_number("(0.04) 10.32")


class TestSeparatorVariants:
    """Characters a recogniser emits in place of an ordinary comma.

    A degraded scan produced a fullwidth comma for 123,162. Left alone it turns
    one figure into two, and the ambiguous-cell guard then discards a value
    that was read perfectly well -- which is a worse outcome than the odd
    character.
    """

    @pytest.mark.parametrize("text,expected", [
        ("123\uff0c162", 123162.0),      # fullwidth comma
        ("1\u2019234\u2019567", 1234567.0),  # apostrophe as group separator
        ("12\u3001345", 12345.0),        # ideographic comma
    ])
    def test_a_variant_separator_is_read_as_a_separator(self, text, expected):
        assert value(text) == pytest.approx(expected)

    def test_a_variant_separator_is_not_two_numbers(self):
        from app.normalization.numbers import holds_more_than_one_number
        assert not holds_more_than_one_number("123\uff0c162")
