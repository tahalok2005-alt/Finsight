"""A bank's results, scanned: two defects of the scanned page, held here.

Found in the Phase 21 corpus (the scanned consolidated results of a private
bank, whose typeset twin is also in the corpus -- the same figures, which is
how both defects were confirmed):

  * the scanner read decimal points as colons -- "12,351:26", "156:43" --
    and each such cell was refused as two figures run together;
  * the serial numbers fell into two columns ("1" a few pixels left of "2"),
    neither followed directly by the words, and an empty margin column was
    taken for the labels: a page of 153 figures produced none.
"""
from __future__ import annotations

from app.extraction.facts import TableFactContext, _label_columns, extract_from_table
from app.models.enums import CellRole, Currency, MagnitudeUnit
from app.models.schemas import BBoxModel, ExtractedTable, TableCell, TableColumn
from app.normalization.numbers import repair_by_decimals, table_decimals
from app.normalization.periods import FiscalCalendar, parse_period

MARCH = FiscalCalendar(year_end_month=3, is_known=True)


class TestAPointReadAsAColon:
    def test_in_a_table_of_two_place_amounts_it_is_a_point(self):
        convention = table_decimals(["17,506.80", "17,198.74", "16,633.14",
                                     "(633.02)", "1,427.87"])
        assert convention == "2dp"
        assert repair_by_decimals("12,351:26", convention) == "12,351.26"
        assert repair_by_decimals("(1,078:93)", convention) == "(1,078.93)"
        assert repair_by_decimals("17,506.80", convention) is None

    def test_in_a_table_of_whole_numbers_it_is_left_alone(self):
        assert repair_by_decimals("12,351:26", "integer") is None
        assert repair_by_decimals("12,351:26", None) is None


def _table():
    cells = []

    def cell(r, c, text, num=None, role=None, numeric=None):
        cells.append(TableCell(
            id=f"c{r}_{c}", table_id="t", document_id="d", page_number=6,
            row=r, col=c, text=text,
            role=role or (CellRole.COLUMN_HEADER if r == 0 else
                          CellRole.ROW_LABEL if c <= 2 else CellRole.DATA),
            bbox=BBoxModel(x=100 * c, y=20 * r, width=90, height=12),
            confidence=0.8, numeric_value=num,
            is_numeric=num is not None if numeric is None else numeric))

    cell(0, 3, "Quarter ended 31-Dec-25")
    cell(1, 0, "1", 1.0); cell(1, 2, "Interestearned (a+b+c+d)")
    cell(1, 3, "17,506.80", 17506.80)
    cell(2, 2, "(a) Interest/discountonadvances/bills"); cell(2, 3, "12,717.93", 12717.93)
    cell(3, 1, "2", 2.0); cell(3, 2, "Other income (a+b+c)"); cell(3, 3, "10,343.99", 10343.99)
    # read as a figure by the table builder, left unparsed before the repair
    cell(4, 2, "(c) Other income (Refer Note 4)"); cell(4, 3, "12,351:26", numeric=True)
    q = parse_period("Quarter ended 31-Dec-25", calendar=MARCH)
    return ExtractedTable(
        id="t", document_id="d", page_number=6, region_id="r", title="",
        bbox=BBoxModel(x=0, y=0, width=400, height=100), n_rows=5, n_cols=4,
        columns=[TableColumn(index=0, header=""), TableColumn(index=1, header="No"),
                 TableColumn(index=2, header="Particulars"),
                 TableColumn(index=3, header="Quarter ended 31-Dec-25",
                             normalized_period=q.label, period_resolved=q,
                             numeric_ratio=1.0)],
        cells=cells, confidence=0.8, unit_hint=MagnitudeUnit.CRORE,
        currency_hint=Currency.INR)


class TestSerialNumbersSplitOverTwoColumns:
    def test_both_are_serial_and_the_words_are_the_label(self):
        assert _label_columns(_table(), 1) == [0, 1, 2]

    def test_and_the_rows_produce_facts(self):
        ctx = TableFactContext(document_id="d", entity="Consolidated", calendar=MARCH,
                               default_currency=Currency.INR,
                               default_unit=MagnitudeUnit.CRORE)
        by_value = {f.value: f.canonical_metric
                    for f in extract_from_table(_table(), ctx)}
        assert by_value[17506.80] == "interest_earned"
        assert by_value[12717.93] == "interest_on_advances"
        assert by_value[10343.99] == "other_income"
        assert by_value[12351.26] == "other_income"
