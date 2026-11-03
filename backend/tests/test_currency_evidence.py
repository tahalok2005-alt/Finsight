"""A figure's currency comes from evidence about that figure, never from a vote.

Found in the Phase 21 corpus: three filings whose statements are in rupees or
euros came through in US dollars -- 140 figures, most of them unflagged.

    Airtel  "(₹ in Millions ...)"  read as "(Kin Milions ...)"
    SAP     "€ millions ..."      read as "E millions ..."
    Wipro   "(₹ in millions ...)"  read as "( in millions ...)"

The visual OCR cannot emit the rupee or euro glyph, so each table's own
declaration named no currency, and each fell back to a document-wide vote. The
vote counted AMOUNTS in prose -- "$480 million", "$ 43 million" -- as though
they declared the document's currency, and a phrase naming two currencies
("USD per EUR") as though it named one. Dollars won every time.

The rules these tests hold:
  * a declaration states a unit and never an amount; a fragment with a figure
    in it does not vote;
  * a fragment naming two currencies declares neither;
  * a document has a currency only if its declarations agree;
  * a table that declares its own unit keeps an illegible currency UNKNOWN --
    it may not borrow the document's;
  * a figure that does borrow the document's currency says it was inferred;
  * a table's own figures, when they carry exactly one currency symbol, are
    table-scoped evidence (the US convention of "$" on the first and total
    rows).
"""
from __future__ import annotations

from app.core.geometry import BBox
from app.extraction.facts import TableFactContext, extract_from_table
from app.models.enums import CellRole, Currency, MagnitudeUnit
from app.models.schemas import (
    BBoxModel, ExtractedTable, OCRElement, TableCell, TableColumn,
)
from app.normalization.numbers import infer_document_units, normalize_number
from app.normalization.periods import FiscalCalendar
from app.ocr.lines import TextLine
from app.tables.structure import build_table

MARCH = FiscalCalendar(year_end_month=3)


# ---------------------------------------------------------------- the vote
class TestTheDocumentVoteCountsOnlyDeclarations:
    def test_amounts_in_prose_do_not_vote(self):
        texts = ["( in millions, except share and per share data)",
                 "Revenue grew to $480 million in the quarter",
                 "a charge of $ 43 million was recognised",
                 "net profit of Rs. 11,402 million"]
        currency, magnitude = infer_document_units(texts)
        assert currency is None
        assert magnitude == MagnitudeUnit.MILLION

    def test_a_phrase_naming_two_currencies_declares_neither(self):
        currency, _ = infer_document_units(["(rates are in USD per EUR)"])
        assert currency is None

    def test_agreeing_declarations_give_the_document_its_currency(self):
        currency, magnitude = infer_document_units(
            ["(₹ in crore)", "(Rs. in crore unless otherwise stated)"])
        assert currency == Currency.INR
        assert magnitude == MagnitudeUnit.CRORE

    def test_disagreeing_declarations_give_it_none(self):
        currency, _ = infer_document_units(
            ["(₹ in millions)", "(USD in millions)"])
        assert currency is None

    def test_a_footnote_marker_is_not_a_declaration(self):
        # A telecom filing marks footnotes with "$"; each marker voted dollars
        # against its rupee statements, whose own glyph OCR had lost.
        currency, _ = infer_document_units(
            ["$ Passive infrastructure services represent", "$",
             "$ Under liquidation", " Rs.", "(Kin Milions;except persharedata)"])
        assert currency is None

    def test_a_symbol_with_its_magnitude_is_one(self):
        currency, magnitude = infer_document_units(["(US$ million)", "US$ million"])
        assert currency == Currency.USD
        assert magnitude == MagnitudeUnit.MILLION

    def test_a_worded_declaration_needs_no_magnitude(self):
        currency, _ = infer_document_units(["(Amount in ₹)"])
        assert currency == Currency.INR


class TestTheFrontPagesDoNotDecideTheCurrency:
    """The document's currency is what it declares, or nothing. A fallback to
    the first currency symbol on the opening pages picked a footnote's "$"
    for a rupee filing, and would have picked an amount printed "$12,840.08"
    for a bank's."""

    def _metadata(self, texts):
        from app.document.assembly import infer_metadata
        from app.models.enums import RegionType
        from app.models.schemas import Region
        regions = [Region(id=f"r{i}", document_id="d", page_number=1,
                          type=RegionType.PARAGRAPH,
                          bbox=BBoxModel(x=0, y=20 * i, width=100, height=10),
                          confidence=0.9, reading_order=i, text=t)
                   for i, t in enumerate(texts)]
        return infer_metadata({1: regions}, set(), MARCH)

    def test_an_amount_on_page_one_is_not_the_document_currency(self):
        meta = self._metadata(["Consolidated results", "$12,840.08",
                               "(in crore)"])
        assert meta[4] is None

    def test_a_footnote_marker_is_not_either(self):
        meta = self._metadata(["$ Passive infrastructure services represent",
                               "( in millions)"])
        assert meta[4] is None

    def test_a_declaration_still_is(self):
        meta = self._metadata(["(₹ in crore)", "$12,840.08"])
        assert meta[4] == Currency.INR


class TestABorrowedCurrencySaysSo:
    def test_the_document_default_is_marked_inferred(self):
        n = normalize_number("1,234", default_currency=Currency.USD,
                             default_magnitude=MagnitudeUnit.MILLION,
                             default_currency_inferred=True)
        assert n.currency == Currency.USD
        assert n.currency_inferred is True

    def test_a_table_declared_currency_is_not(self):
        n = normalize_number("1,234", default_currency=Currency.USD,
                             default_magnitude=MagnitudeUnit.MILLION)
        assert n.currency_inferred is False

    def test_a_symbol_on_the_figure_is_read_not_inferred(self):
        n = normalize_number("$ 1,234", default_currency=Currency.INR,
                             default_magnitude=MagnitudeUnit.MILLION,
                             default_currency_inferred=True)
        assert n.currency == Currency.USD
        assert n.currency_inferred is False


# ---------------------------------------------------------------- the table
def _table(*, currency_hint, unit_hint, declaration: str | None) -> ExtractedTable:
    rows = [("Revenue from operations", "585,391", "553,832"),
            ("Profit before tax", "137,728", "100,447")]
    cells = [
        TableCell(id="h0", table_id="t", document_id="d", page_number=1, row=0,
                  col=0, text="Particulars", role=CellRole.CORNER,
                  bbox=BBoxModel(x=0, y=0, width=90, height=12), confidence=0.9),
        TableCell(id="h1", table_id="t", document_id="d", page_number=1, row=0,
                  col=1, text="June 30, 2026", role=CellRole.COLUMN_HEADER,
                  bbox=BBoxModel(x=100, y=0, width=90, height=12), confidence=0.9),
        TableCell(id="h2", table_id="t", document_id="d", page_number=1, row=0,
                  col=2, text="March 31, 2026", role=CellRole.COLUMN_HEADER,
                  bbox=BBoxModel(x=200, y=0, width=90, height=12), confidence=0.9),
    ]
    for r, row in enumerate(rows, start=1):
        for c, text in enumerate(row):
            cells.append(TableCell(
                id=f"c{r}{c}", table_id="t", document_id="d", page_number=1,
                row=r, col=c, text=text,
                role=CellRole.ROW_LABEL if c == 0 else CellRole.DATA,
                bbox=BBoxModel(x=100 * c, y=20 * r, width=90, height=12),
                confidence=0.9,
                numeric_value=float(text.replace(",", "")) if c else None,
                is_numeric=c > 0))
    return ExtractedTable(
        id="t", document_id="d", page_number=1, region_id="r", title="",
        bbox=BBoxModel(x=0, y=0, width=300, height=60), n_rows=3, n_cols=3,
        columns=[TableColumn(index=0, header="Particulars", numeric_ratio=0.0),
                 TableColumn(index=1, header="June 30, 2026",
                             normalized_period="Q1 FY27", numeric_ratio=1.0),
                 TableColumn(index=2, header="March 31, 2026",
                             normalized_period="Q4 FY26", numeric_ratio=1.0)],
        cells=cells, confidence=0.9, unit_hint=unit_hint,
        currency_hint=currency_hint,
        meta={"unit_declaration": declaration} if declaration else {})


def _ctx(default_currency):
    return TableFactContext(document_id="d", entity="Consolidated",
                            calendar=MARCH, default_currency=default_currency,
                            default_unit=MagnitudeUnit.MILLION)


class TestATableDeclarationGovernsItsTable:
    def test_a_declared_unit_with_an_illegible_currency_stays_unknown(self):
        # "€ millions" read as "E millions": the table declared its unit, and
        # the document's dollars must not be put in the euro's place.
        table = _table(currency_hint=None, unit_hint=MagnitudeUnit.MILLION,
                       declaration="E millions, unless otherwise stated")
        facts = extract_from_table(table, _ctx(Currency.USD))
        assert facts
        assert {f.currency for f in facts} == {Currency.UNKNOWN}

    def test_an_undeclared_table_may_borrow_but_says_it_did(self):
        table = _table(currency_hint=None, unit_hint=None, declaration=None)
        facts = extract_from_table(table, _ctx(Currency.USD))
        assert facts
        assert all(f.currency == Currency.USD for f in facts)
        assert all(f.currency_inferred for f in facts)

    def test_the_table_own_currency_is_used_and_not_called_inferred(self):
        table = _table(currency_hint=Currency.INR,
                       unit_hint=MagnitudeUnit.MILLION,
                       declaration="(Rs. in Millions)")
        facts = extract_from_table(table, _ctx(Currency.USD))
        assert {f.currency for f in facts} == {Currency.INR}
        assert not any(f.currency_inferred for f in facts)

    def test_crore_still_implies_rupees_when_the_glyph_is_lost(self):
        table = _table(currency_hint=None, unit_hint=MagnitudeUnit.CRORE,
                       declaration="( in crore)")
        facts = extract_from_table(table, _ctx(Currency.USD))
        assert {f.currency for f in facts} == {Currency.INR}
        assert all(f.currency_inferred for f in facts)


# ------------------------------------------------- the table's own figures
def _line(i: int, y: float, items: list[tuple[str, float]]) -> TextLine:
    els = [OCRElement(id=f"e{i}_{j}", document_id="d", page_number=1, text=t,
                      confidence=0.95,
                      bbox=BBoxModel(x=x, y=y, width=max(30.0, len(t) * 8.0),
                                     height=14.0))
           for j, (t, x) in enumerate(items)]
    box = BBox.union_all(BBox(x=e.bbox.x, y=e.bbox.y, width=e.bbox.width,
                              height=e.bbox.height) for e in els)
    return TextLine(id=f"l{i}", page_number=1, bbox=box, elements=els)


class TestATablesOwnFiguresAreEvidence:
    def test_dollar_signs_on_the_figures_make_it_a_dollar_table(self):
        lines = [
            _line(0, 10, [("(In millions, except per share amounts)", 20)]),
            _line(1, 40, [("Three Months Ended", 300)]),
            _line(2, 60, [("June 28, 2025", 280), ("June 29, 2024", 420)]),
            _line(3, 90, [("Total net sales", 20), ("$", 270), ("94,036", 300),
                          ("$", 410), ("85,777", 440)]),
            _line(4, 110, [("Cost of sales", 20), ("50,318", 300), ("46,099", 440)]),
            _line(5, 130, [("Gross margin", 20), ("43,718", 300), ("39,678", 440)]),
            _line(6, 150, [("Net income", 20), ("$", 270), ("23,434", 300),
                           ("$", 410), ("21,448", 440)]),
        ]
        table = build_table(document_id="d", page_number=1, region_id="r",
                            lines=lines, v_lines=[], page_width=600,
                            page_height=400, table_index=0, calendar=MARCH,
                            ruled=False, base_confidence=0.7)
        assert table is not None
        assert table.currency_hint == Currency.USD

    def test_a_rupee_table_with_a_dollar_equivalent_names_two_and_decides_nothing(self):
        lines = [
            _line(0, 10, [("Segment revenue", 20)]),
            _line(1, 40, [("Q1 FY27", 300), ("Q1 FY26", 440)]),
            _line(2, 70, [("Retail", 20), ("₹ 1,200", 300), ("₹ 1,100", 440)]),
            _line(3, 90, [("Digital", 20), ("$ 14.3", 300), ("$ 13.2", 440)]),
            _line(4, 110, [("Other", 20), ("300", 300), ("280", 440)]),
        ]
        table = build_table(document_id="d", page_number=1, region_id="r",
                            lines=lines, v_lines=[], page_width=600,
                            page_height=400, table_index=0, calendar=MARCH,
                            ruled=False, base_confidence=0.7)
        if table is not None:
            assert table.currency_hint is None
