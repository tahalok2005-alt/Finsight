"""Two columns in one cell are two figures, never their concatenation.

Found in the Phase 21 corpus. When a column boundary is missed, two figures
land in one cell, and the number pattern -- which accepts mixed separators and
groups of two or three digits in any order -- read them as ONE figure:

    Eternal  "20,211 17,292"   -> revenue from operations 2,021,117,292  (unflagged)
    Eternal  "228 88"          -> profit before tax 22,888               (unflagged)
    Eternal  "151 132"         -> finance costs 151,132                  (unflagged)
    Airtel   "25,409 12.203"   -> current tax 2,540,912.203              (unflagged)
    Tata     "(22.25) (1.62)"  -> -22,251.62
    ICICI    "0.22% 0.22%"     -> 220.22

The existing guard (`holds_more_than_one_number`) returned False for every one
of them. These tests hold the rules a printed figure obeys -- one kind of
separator, a real grouping, one decimal part, one sign -- and the one decision
that needs the table: whether a space before three digits is a thousands
separator ("18 090.16" in a table that groups with spaces) or a column gap
("166 632" in a table that groups with commas).
"""
from __future__ import annotations

import pytest

from app.normalization.numbers import holds_more_than_one_number, table_grouping


@pytest.mark.parametrize("text,grouping", [
    ("20,211 17,292", None), ("20,211 17,292", "comma"),
    ("5,088 28,173", None), ("25,409 12.203", None),
    ("(22.25) (1.62)", None), ("0.22% 0.22%", None), ("1.38% 1.40%", None),
    ("0.03 0.40", None), ("228 88", None), ("228 88", "space"),
    ("(0.04) 10.32", None), ("166 632", "comma"), ("151 132", "comma"),
])
def test_two_figures_are_two_figures(text, grouping):
    assert holds_more_than_one_number(text, grouping)


@pytest.mark.parametrize("text,grouping", [
    ("1,501.42", None), ("1,27,896.46", None), ("14272.21", None),
    ("18 090.16", None), ("18 090.16", "space"), ("4 222.47", None),
    ("1 27 896.46", "space"), ("166 632", "space"),
    ("1.053.69", None), ("62005,98", None), ("123，162", None),
    ("(1,082)", None), ("₹ 1,200", None), ("0.00", None), ("-", None),
    ("17.93%", None), ("(125)", None),
])
def test_one_figure_is_one_figure(text, grouping):
    assert not holds_more_than_one_number(text, grouping)


def test_an_ambiguous_space_in_a_table_that_shows_no_convention_is_refused():
    # One figure or two? Nothing on the page says, so the cell is not read.
    assert holds_more_than_one_number("166 632", None)


class TestTheTableSaysHowItGroups:
    def test_commas_elsewhere_make_a_space_a_column_gap(self):
        assert table_grouping(["20,211", "17,292", "151", "166 632"]) == "comma"

    def test_spaces_with_decimals_make_a_space_a_thousands_separator(self):
        assert table_grouping(["18 090.16", "399.86", "4 222.47"]) == "space"

    def test_a_table_of_small_figures_shows_no_convention(self):
        assert table_grouping(["399.86", "234.75", "12"]) is None
