"""A row whose label means nothing alone is read with the row it belongs to.

Found in the Phase 21 corpus. Statements print components under their total
("(d) Others" under "Interest earned (a)+(b)+(c)+(d)"), children under a
heading with no colon ("Earnings per share (Face value: ₹ 5 each)" over
"Basic" and "Diluted"), and labels wrapped onto a second line ("Operating
profit (3-6)" over "(Profit before provisions and contingencies)"). Read
alone, the first two produce nothing and the third is misread as EBIT.

The rule every composition here obeys: it is taken only when the taxonomy
recognises the composed label word for word. A similarity judgement on a
label the extractor assembled would be a guess about a guess.

And the statement's kind -- balances ("As at") or flows ("... ended") --
decides the few bare names whose words alone do not: "Minority interest" is
an equity line in one and the minority's share of profit in the other.
"""
from __future__ import annotations

from app.extraction.facts import TableFactContext, extract_from_table
from app.models.enums import CellRole, Currency, MagnitudeUnit
from app.models.schemas import BBoxModel, ExtractedTable, TableCell, TableColumn
from app.normalization.periods import FiscalCalendar, parse_period

MARCH = FiscalCalendar(year_end_month=3)


def _table(rows, head="Quarter ended 30.06.2026") -> ExtractedTable:
    cells = [TableCell(id="h0", table_id="t", document_id="d", page_number=1,
                       row=0, col=0, text="Particulars", role=CellRole.CORNER,
                       bbox=BBoxModel(x=0, y=0, width=300, height=12),
                       confidence=0.9),
             TableCell(id="h1", table_id="t", document_id="d", page_number=1,
                       row=0, col=1, text=head, role=CellRole.COLUMN_HEADER,
                       bbox=BBoxModel(x=320, y=0, width=100, height=12),
                       confidence=0.9)]
    for r, (label, value) in enumerate(rows, start=1):
        cells.append(TableCell(
            id=f"l{r}", table_id="t", document_id="d", page_number=1, row=r,
            col=0, text=label, role=CellRole.ROW_LABEL,
            bbox=BBoxModel(x=0, y=20 * r, width=300, height=12), confidence=0.9))
        if value is not None:
            cells.append(TableCell(
                id=f"v{r}", table_id="t", document_id="d", page_number=1, row=r,
                col=1, text=value, role=CellRole.DATA,
                bbox=BBoxModel(x=320, y=20 * r, width=100, height=12),
                confidence=0.9, is_numeric=True,
                numeric_value=float(value.rstrip("%").replace(",", ""))))
    period = parse_period(head, calendar=MARCH)
    return ExtractedTable(
        id="t", document_id="d", page_number=1, region_id="r", title="",
        bbox=BBoxModel(x=0, y=0, width=420, height=20 * (len(rows) + 1)),
        n_rows=len(rows) + 1, n_cols=2,
        columns=[TableColumn(index=0, header="Particulars", numeric_ratio=0.0),
                 TableColumn(index=1, header=head, normalized_period=period.label,
                             period_resolved=period, numeric_ratio=1.0)],
        cells=cells, confidence=0.9, unit_hint=MagnitudeUnit.CRORE,
        currency_hint=Currency.INR)


def _ctx():
    return TableFactContext(document_id="d", entity="Standalone", calendar=MARCH,
                            default_currency=Currency.INR,
                            default_unit=MagnitudeUnit.CRORE)


def _by_value(facts):
    return {f.value: f.canonical_metric for f in facts}


class TestAComponentUnderItsTotal:
    def test_others_under_interest_earned(self):
        facts = extract_from_table(_table([
            ("Interest earned (a)+(b)+(c)+(d)", "17,506.80"),
            ("(a) Interest/discount on advances/bills", "12,717.93"),
            ("(b) Income on investments", "4,027.17"),
            ("(c) Interest on balances with Reserve Bank of India and other "
             "inter-bank funds", "213.71"),
            ("(d)others", "547.99"),
            ("Other income", "10,343.99"),
        ]), _ctx())
        got = _by_value(facts)
        assert got[547.99] == "other_interest_income"
        assert got[17506.80] == "interest_earned"
        assert got[10343.99] == "other_income"

    def test_a_component_the_taxonomy_cannot_name_produces_nothing(self):
        facts = extract_from_table(_table([
            ("Total income", "1,000.00"),
            ("(a) Something else entirely", "10.00"),
        ]), _ctx())
        assert 10.0 not in _by_value(facts)


class TestChildrenUnderAHeadingWithoutAColon:
    def test_basic_and_diluted_under_earnings_per_share(self):
        facts = extract_from_table(_table([
            ("Profit for the period", "5,000.00"),
            ("Earnings per share (Face value : ₹ 5 each)", None),
            ("Basic", "8.12"),
            ("Diluted", "8.05"),
        ]), _ctx())
        got = _by_value(facts)
        assert got[8.12] == "eps"
        assert got[8.05] == "diluted_eps"

    def test_a_wrapped_label_whose_figures_are_on_its_second_line(self):
        facts = extract_from_table(_table([
            ("PROFIT FROM ORDINARY ACTIVITIES BEFORE EXCEPTIONAL", None),
            ("ITEMS AND TAX (7)-(8)", "16,221.46"),
        ]), _ctx())
        assert _by_value(facts)[16221.46] == "pbt_before_exceptional"


class TestAWrappedContinuationBelowTheFigures:
    def test_operating_profit_of_a_bank(self):
        facts = extract_from_table(_table([
            ("Operating profit (3-6)", "7,003.26"),
            ("(Profit before provisions and contingencies)", None),
            ("Provisions (other than tax) and contingencies", "947.83"),
        ]), _ctx())
        got = _by_value(facts)
        assert got[7003.26] == "pre_provision_operating_profit"
        assert got[947.83] == "provisions_and_contingencies"


class TestTheStatementsKindDecidesABareName:
    def test_in_a_statement_of_profit(self):
        facts = extract_from_table(_table([
            ("Net profit for the period", "4,000.00"),
            ("Minority Interest", "120.00"),
        ], head="Quarter ended 30.06.2026"), _ctx())
        assert _by_value(facts)[120.0] == "nci_share_of_profit"

    def test_in_a_balance_sheet(self):
        facts = extract_from_table(_table([
            ("Capital", "1,592.00"),
            ("Minority interest", "8,500.00"),
        ], head="As at 30.06.2026"), _ctx())
        got = _by_value(facts)
        assert got[8500.0] == "non_controlling_interests"
        assert got[1592.0] == "share_capital"

    def test_a_bare_quarter_label_decides_nothing(self):
        facts = extract_from_table(_table([
            ("Minority interest", "8,500.00"),
        ], head="Q1 FY27"), _ctx())
        assert 8500.0 not in _by_value(facts)


def _two_column_table(rows) -> ExtractedTable:
    """A quarter column and a second column whose head was not read."""
    base = _table([(label, first) for label, first, _ in rows])
    cells = list(base.cells)
    for r, (_, _, second) in enumerate(rows, start=1):
        cells.append(TableCell(
            id=f"w{r}", table_id="t", document_id="d", page_number=1, row=r,
            col=2, text=second, role=CellRole.DATA,
            bbox=BBoxModel(x=440, y=20 * r, width=80, height=12),
            confidence=0.9, is_numeric=True,
            numeric_value=float(second.rstrip("%").replace(",", ""))))
    return base.model_copy(update={
        "cells": cells, "n_cols": 3,
        "columns": base.columns + [TableColumn(index=2, header="",
                                               numeric_ratio=1.0)]})


class TestARateInARowOfAmounts:
    """A slide printed "Profit after tax | 6,081 | 28%": the second figure is a
    year-on-year change whose column head was not read, and it came out as
    profit after tax. It is not the row's value and is not extracted."""

    def test_the_rate_is_not_the_rows_value(self):
        facts = extract_from_table(_two_column_table([
            ("Profit after tax", "6,081", "28%"),
            ("Interest income", "20,513", "20%"),
        ]), _ctx())
        got = _by_value(facts)
        assert got[6081.0] == "pat"
        assert got[20513.0] == "interest_earned"
        assert 28.0 not in got and 20.0 not in got

    def test_a_row_of_rates_is_still_read_as_rates(self):
        facts = extract_from_table(_two_column_table([
            ("CASA ratio", "50.8%", "48.2%"),
        ]), _ctx())
        assert {f.value for f in facts} == {50.8, 48.2}


class TestALabelWrappedOverTwoLinesIsReadWhole:
    """The second line of a wrapped label names something by itself, and the
    wrong thing: "MINORITY INTEREST (9)-(10)+(11)" under "PROFIT FROM ORDINARY
    ACTIVITIES BEFORE TAX AND" was read as the minority's share of profit, and
    "of tax expense (not annualised) (in ₹)" under "a) Basic EPS before and
    after extraordinary items, net" as the tax charge."""

    def test_the_first_line_completes_the_second(self):
        facts = extract_from_table(_table([
            ("PROFIT FROM ORDINARY ACTIVITIES BEFORE TAX AND", None),
            ("MINORITY INTEREST (9)-(10)+(11)", "21,373.61"),
            ("a) Basic EPS before and after extraordinary items, net", None),
            ("of tax expense (not annualised) (in ₹)", "20.65"),
        ]), _ctx())
        got = _by_value(facts)
        assert got[21373.61] == "pbt"
        assert got[20.65] == "eps"

    def test_a_heading_does_not_rename_a_row_that_names_itself(self):
        facts = extract_from_table(_table([
            ("Revenue from operations", None),
            ("Sale of products", "17,149"),
        ]), _ctx())
        assert _by_value(facts)[17149.0] == "sale_of_products"


class TestAColumnHeadedAsARate:
    def test_its_bare_figures_are_not_the_rows_value(self):
        base = _two_column_table([("Total revenue", "18,012", "8"),
                                  ("Operating profit", "4,519", "12")])
        cols = [c if c.index != 2 else c.model_copy(update={"header": "A in %"})
                for c in base.columns]
        facts = extract_from_table(base.model_copy(update={"columns": cols}), _ctx())
        got = _by_value(facts)
        assert got[18012.0] == "revenue" and got[4519.0] == "ebit"
        assert 8.0 not in got and 12.0 not in got


class TestANameForAnAmountAndItsRate:
    """ "Gross NPA" is printed in crore and as a percentage of advances. A
    bank's typeset results filed its gross NPAs of 6,266.25 crore under the
    NPA ratio; its scanned twin, read separately, is how that showed."""

    def test_the_figures_own_marks_decide(self):
        facts = extract_from_table(_table([
            ("(a) Gross NPA", "6,266.25"),
            ("(b) Net NPA", "1,523.79"),
            ("(c) % of Gross NPA", "1.44"),
            ("(d) % of Net NPA", "0.35"),
        ]), _ctx())
        got = _by_value(facts)
        assert got[6266.25] == "gross_npa_amount"
        assert got[1523.79] == "net_npa_amount"
        assert got[1.44] == "gross_npa_ratio"
        assert got[0.35] == "net_npa_ratio"

    def test_a_rate_marked_on_the_cell_is_the_ratio(self):
        facts = extract_from_table(_table([("Gross NPA", "1.83%")]), _ctx())
        assert _by_value(facts)[1.83] == "gross_npa_ratio"


class TestAComponentOfASameNamedTotal:
    """ "(c) Other income" under "Other income (a+b+c)" is part of that total.
    A bank's (a) line wraps, and its figures sit on the second line, which is
    not enumerated -- so the row just read is not the total. The component is
    still recorded as one, so no identity takes it for the whole."""

    def test_the_component_is_marked(self):
        facts = extract_from_table(_table([
            ("Other income (a+b+c)", "10,343.99"),
            ("(a) Profit/(Loss) on sale of", None),
            ("Investments including revaluation (insurance business)", "969.17"),
            ("(b) Premium on Insurance Business", "4,852.45"),
            ("(c) Other income (Refer Note 4)", "4,522.37"),
        ]), _ctx())
        by_value = {f.value: f for f in facts}
        assert by_value[4522.37].meta.get("component") is True
        assert not by_value[10343.99].meta.get("component")
