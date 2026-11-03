"""Defects found by putting a real results presentation through the system.

The document was IDFC's Q1 FY27 investor deck: 75 pages, mostly slides, with
its tables laid out by business segment and its columns headed with month-year
abbreviations. Every test here pins a specific thing that went wrong on it.

None of these are about that filer. A slide deck is simply a harsher test of
the same general questions -- is this string a name, is this column a period,
is this figure actually doubtful -- than a statutory filing is, because a deck
answers all three badly.
"""
from __future__ import annotations

import pytest

from app.document.assembly import _entity_from_heading, _reads_as_a_name
from app.extraction.confidence import review_reason_for
from app.models.enums import (
    FactKind, MagnitudeUnit, PeriodBasis, ReviewReason, StatementNature,
    ValidationStatus, ValueType,
)
from app.models.schemas import ConfidenceBreakdown, FinancialFact
from app.normalization.periods import (
    FiscalCalendar, looks_like_period_header, parse_period_levels,
)

MARCH = FiscalCalendar(year_end_month=3)


# ------------------------------------------------------- entity attribution
class TestEntityNamesMustReadAsNames:
    """Chart legends were being registered as business units.

    The gate was "does the heading mention anything entity-like", and its word
    list includes `retail`, `digital` and `business` -- which appear on nearly
    every slide of a bank's presentation. Legends passed it, and figures were
    then attributed to units the document never named.
    """

    @pytest.mark.parametrize("heading", [
        # a legend paired with its share of the total
        "Retail Deposits Retail Deposits 9%",
        "Digital Consumer Digital Personal Loan 5%",
        # a legend listing every category in a chart
        "Mortgages Vehicles MSME Consumer Loans Credit Cards Microfin",
        # the tail of a sentence, cut mid-phrase by layout
        "of Ethical, Digital & Social Good Banking",
        # two legend entries concatenated
        "Retail Deposits Wholesale Deposits",
    ])
    def test_keyword_soup_is_not_an_entity(self, heading):
        assert _reads_as_a_name(heading) is False, heading
        assert _entity_from_heading(heading) is None, heading

    @pytest.mark.parametrize("heading,expected", [
        ("Jio Platforms Limited", "Jio Platforms Limited"),
        ("Reliance Retail Ventures Limited", "Reliance Retail Ventures Limited"),
        ("Retail Banking Division", "Retail Banking"),
        ("Wholesale Banking Segment", "Wholesale Banking"),
        ("Consolidated", "Consolidated"),
        ("Standalone", "Standalone"),
    ])
    def test_real_units_still_resolve(self, heading, expected):
        assert _entity_from_heading(heading) == expected

    @pytest.mark.parametrize("heading", [
        "Digital Services",
        "Quality Financial Services",
        "DISTRIBUTION OF FINANCIAL SERVICES",
    ])
    def test_a_bare_descriptor_no_longer_resolves(self, heading):
        """Tightened in Phase 18, and this is a deliberate narrowing.

        "Digital Services" used to resolve, on the grounds that it names a real
        segment of at least one filer. The trouble is that it is the same shape
        as every other two-word service phrase, and three documents the system
        had not seen showed what accepting that shape costs: a marketing line
        ("Quality Financial Services") governed 23 of one earnings release's 25
        figures, and a slide heading took most of another's.

        A heading now has to *declare* a unit -- a legal suffix somewhere, or a
        head noun saying so ("... Division", "... Segment", "... Business").
        A real segment heading in a filing almost always carries one; the
        benchmark documents' own headings all do, and none of their entity
        attribution moved when this was tightened.
        """
        assert _entity_from_heading(heading) is None

    def test_a_name_may_repeat_a_word_only_across_a_binding(self):
        """"Reliance Retail" binds; "Retail Deposits Retail Deposits" repeats."""
        assert _reads_as_a_name("Reliance Retail Ventures Limited") is True
        assert _reads_as_a_name("Retail Deposits Retail Deposits") is False

    def test_a_repeated_connective_is_not_a_repeated_subject(self):
        """"Oil and Gas (Exploration and Production)" says "and" twice.

        The repeated-word test is looking for a repeated *subject*, which is
        what a concatenated legend has. Counting connectives caught a real
        segment name and dropped its figures onto the consolidated group,
        where they contradicted the consolidated reading of the same filing.
        """
        heading = "OIL AND GAS (EXPLORATION AND PRODUCTION) SEGMENT"
        assert _reads_as_a_name("OIL AND GAS (EXPLORATION AND PRODUCTION)")
        assert _entity_from_heading(heading) == \
            "OIL AND GAS (EXPLORATION AND PRODUCTION)"

    @pytest.mark.parametrize("heading,expected", [
        ('OILTO CHEMICALS("O2C")SEGMENT', "O2C"),
        ('JIO PLATFORMS LIMITED ("JPL")', "JPL"),
        ('RELIANCE RETAIL VENTURES LIMITED ("RRVL")', "RRVL"),
        ("JIOSTAR BUSINESS", "JIOSTAR"),
        ("DIGITAL SERVICES SEGMENT", "DIGITAL SERVICES"),
    ])
    def test_the_benchmark_filing_s_segments_still_resolve(self, heading, expected):
        """The name rule must not cost the documents it was not written for."""
        assert _entity_from_heading(heading) == expected


# ------------------------------------------------------------ month-year heads
class TestMonthYearColumnHeads:
    """"Jun-26" over a balance-sheet column is a period and was read as none.

    This is the ordinary column head of an Asian or European presentation. Not
    reading it left a balance sheet whose every figure was undated.
    """

    @pytest.mark.parametrize("head,end", [
        ("Jun-26", "2026-06-30"),
        ("Mar-26", "2026-03-31"),
        ("Jun-25", "2025-06-30"),
        ("Dec-18 (Merger)", "2018-12-31"),
        ("Sep-2026", "2026-09-30"),
        ("Jun-'26", "2026-06-30"),
    ])
    def test_a_month_year_head_dates_the_column(self, head, end):
        p = parse_period_levels([head], calendar=MARCH)
        assert p is not None, head
        assert p.end_date == end
        assert p.start_date is None, "a balance-sheet column is an instant"

    def test_a_span_phrase_turns_it_into_a_period(self):
        p = parse_period_levels(["Quarter ended", "Jun-26"], calendar=MARCH)
        assert p.label == "Q1 FY27"
        assert p.start_date == "2026-04-01"
        assert p.end_date == "2026-06-30"

    def test_a_day_is_never_mistaken_for_a_year(self):
        """"June 28," with its year on the next row is not June 2028.

        Both are a month followed by two digits. Only the separator tells them
        apart, which is why a bare space is not accepted.
        """
        assert parse_period_levels(["Three Months Ended", "June 28,"],
                                   calendar=MARCH) is None
        assert parse_period_levels(["Jun 26"], calendar=MARCH) is None
        p = parse_period_levels(["Three Months Ended", "June 28,", "2025"],
                                calendar=MARCH)
        assert p.end_date == "2025-06-28"

    @pytest.mark.parametrize("head", [
        "Particulars (In Uss Mn.)", "Balance Sheet", "YoY Growth",
        "Total Assets", "1,234", "May increase", "March of progress",
    ])
    def test_ordinary_text_is_not_a_period_head(self, head):
        assert looks_like_period_header(head) is False, head
        assert parse_period_levels([head], calendar=MARCH) is None, head


# -------------------------------------------- period absent, and why it is
def _fact(**kw) -> FinancialFact:
    base = dict(
        fact_id="f1", document_id="d1", metric="Total Assets",
        canonical_metric="total_assets", metric_display="Total Assets",
        kind=FactKind.TABLE_CELL, nature=StatementNature.ACTUAL,
        value=42211.0, raw_value="42,211", value_type=ValueType.MONETARY,
        unit=MagnitudeUnit.MILLION, source_page=44, confidence=0.86,
    )
    base.update(kw)
    return FinancialFact(**base)


class TestPeriodAbsenceHasTwoMeanings:
    """"Not applicable" and "could not be identified" are different states.

    A segment column never offered a period, so there is nothing to find. A
    column that read like a date and would not resolve is a genuine gap. Before
    these were separated, an ordinary segment table rendered as a screen of
    warnings.
    """

    def test_a_dimensional_column_is_not_a_gap(self):
        f = _fact(meta={"period_absence": str(PeriodBasis.NOT_APPLICABLE)})
        assert review_reason_for(f, ValidationStatus.NOT_CHECKED) is None

    def test_an_unresolved_period_column_is_a_gap(self):
        f = _fact(meta={"period_absence": str(PeriodBasis.UNKNOWN)})
        assert review_reason_for(f, ValidationStatus.NOT_CHECKED) == \
            ReviewReason.AMBIGUOUS_PERIOD


class TestReviewReasonsDescribeTheValue:
    """A missing period must not make a well-read number look doubtful.

    The coverage rule used to raise one REQUIRES_REVIEW validation and attach
    every undated fact to it. Because any validation touching a fact raises its
    review flag, a document with 35 undated figures marked all 35 as suspect --
    which says the values are wrong when what is missing is a column header.
    """

    def test_a_clean_figure_with_no_period_is_not_flagged(self):
        f = _fact(period=None,
                  meta={"period_absence": str(PeriodBasis.NOT_APPLICABLE)})
        assert review_reason_for(f, ValidationStatus.NOT_CHECKED) is None
        assert review_reason_for(f, ValidationStatus.PASSED) is None

    def test_an_unreadable_value_is_flagged_first(self):
        f = _fact(value=None)
        assert review_reason_for(f, ValidationStatus.FAILED) == \
            ReviewReason.VALUE_UNPARSED

    def test_a_failed_check_is_flagged(self):
        assert review_reason_for(_fact(), ValidationStatus.FAILED) == \
            ReviewReason.VALIDATION_FAILED

    def test_a_monetary_figure_with_no_declared_scale_is_flagged(self):
        f = _fact(unit=MagnitudeUnit.NONE)
        assert review_reason_for(f, ValidationStatus.NOT_CHECKED) == \
            ReviewReason.UNIT_UNDETERMINED

    def test_a_faint_reading_is_flagged_last(self):
        f = _fact(confidence=0.2,
                  meta={"period_absence": str(PeriodBasis.NOT_APPLICABLE)})
        assert review_reason_for(f, ValidationStatus.NOT_CHECKED) == \
            ReviewReason.LOW_CONFIDENCE

    def test_every_reason_has_a_sentence_for_the_reviewer(self):
        from app.models.enums import REVIEW_REASON_TEXT
        for reason in ReviewReason:
            assert REVIEW_REASON_TEXT.get(reason), reason
            assert len(REVIEW_REASON_TEXT[reason]) > 30, reason


# ------------------------------------------------- header/data alignment
class TestMisalignedHeadersGiveNoPeriod:
    """A heading that sits above nothing must not date the column beside it.

    On one results slide the detector reconstructed the headings ``Jun-25``,
    ``Mar-26`` and ``Jun-26`` at column positions holding no cells, while the
    figures sat three positions to the right. Pairing by index handed a
    balance-sheet total the year-on-year percentage column's date -- and it
    arrived dated, at high confidence, and wrong.
    """

    def _table(self, header_cols: list[int], data_cols: list[int]):
        from app.models.enums import CellRole
        from app.models.schemas import BBoxModel, ExtractedTable, TableCell, TableColumn
        box = BBoxModel(x=0, y=0, width=10, height=10)
        cells = [TableCell(id="c-label", table_id="t1", document_id="d1",
                           row=1, col=0, text="Total Assets", role=CellRole.ROW_LABEL,
                           bbox=box, page_number=44)]
        columns = [TableColumn(index=0, header="Balance Sheet")]
        for i in header_cols:
            columns.append(TableColumn(index=i, header="Jun-26",
                                       normalized_period="As at 2026-06-30"))
        for i in data_cols:
            if not any(c.index == i for c in columns):
                columns.append(TableColumn(index=i, header=""))
            cells.append(TableCell(id=f"c{i}", table_id="t1", document_id="d1",
                                   row=1, col=i, text="42,211",
                                   role=CellRole.DATA, bbox=box, page_number=44,
                                   is_numeric=True, numeric_value=42211.0))
        cells.append(TableCell(id="h0", table_id="t1", document_id="d1",
                               row=0, col=0, text="Balance Sheet",
                               role=CellRole.COLUMN_HEADER, bbox=box,
                               page_number=44))
        return ExtractedTable(
            id="t1", document_id="d1", page_number=44, bbox=box,
            n_rows=2, n_cols=max(data_cols + header_cols) + 1,
            columns=columns, cells=cells)

    def test_headings_over_empty_columns_are_not_used(self):
        from app.extraction.facts import _header_row_is_misaligned
        table = self._table(header_cols=[9, 10, 11, 12], data_cols=[13, 18, 23])
        period_cols = [(c, object()) for c in table.columns
                       if c.index in (9, 10, 11, 12)]
        assert _header_row_is_misaligned(table, period_cols, n_header=1) is True

    def test_headings_over_their_own_data_are_used(self):
        from app.extraction.facts import _header_row_is_misaligned
        table = self._table(header_cols=[1, 2, 3], data_cols=[1, 2, 3])
        period_cols = [(c, object()) for c in table.columns
                       if c.index in (1, 2, 3)]
        assert _header_row_is_misaligned(table, period_cols, n_header=1) is False

    def test_a_two_column_table_is_never_judged(self):
        """Two columns is too small a sample to conclude anything from."""
        from app.extraction.facts import _header_row_is_misaligned
        table = self._table(header_cols=[9, 10], data_cols=[13])
        period_cols = [(c, object()) for c in table.columns
                       if c.index in (9, 10)]
        assert _header_row_is_misaligned(table, period_cols, n_header=1) is False


# --------------------------------------------------------- worker sizing
class TestWorkerCountRespectsMemory:
    """Eight workers on a machine with 2 GB free is how a run appears to hang.

    The core-count rule says what the machine can compute and nothing about
    what it can hold. On a 16 GB machine with 2.2 GB free it still asked for
    eight, and one batch of eight pages then took 35 minutes against a 59
    second median -- a 75-page document that had taken 14 minutes took 48.
    Nothing errored; it thrashed, which is the worst way for this to fail
    because the pipeline simply looks frozen.
    """

    def _settings(self, **kw):
        from app.config.settings import Settings
        return Settings(**kw)

    def test_a_starved_machine_gets_fewer_workers(self, monkeypatch):
        import app.config.settings as mod
        monkeypatch.setattr(mod, "_available_memory_mb", lambda: 2161.0)
        monkeypatch.setattr(mod.os, "cpu_count", lambda: 12)
        s = self._settings(page_workers=0)
        assert s.resolved_page_workers == 4, "2.2 GB free does not hold eight"

    def test_a_roomy_machine_keeps_the_measured_default(self, monkeypatch):
        import app.config.settings as mod
        monkeypatch.setattr(mod, "_available_memory_mb", lambda: 12_000.0)
        monkeypatch.setattr(mod.os, "cpu_count", lambda: 12)
        s = self._settings(page_workers=0)
        assert s.resolved_page_workers == 8, "memory must not cost throughput"

    def test_it_never_falls_below_the_floor(self, monkeypatch):
        import app.config.settings as mod
        monkeypatch.setattr(mod, "_available_memory_mb", lambda: 120.0)
        monkeypatch.setattr(mod.os, "cpu_count", lambda: 12)
        assert self._settings(page_workers=0).resolved_page_workers == 2

    def test_an_explicit_setting_is_always_obeyed(self, monkeypatch):
        import app.config.settings as mod
        monkeypatch.setattr(mod, "_available_memory_mb", lambda: 120.0)
        assert self._settings(page_workers=6).resolved_page_workers == 6

    def test_an_unknowable_machine_keeps_the_core_count_answer(self, monkeypatch):
        """No probe must never mean no workers."""
        import app.config.settings as mod
        monkeypatch.setattr(mod, "_available_memory_mb", lambda: None)
        monkeypatch.setattr(mod.os, "cpu_count", lambda: 12)
        s = self._settings(page_workers=0)
        assert s.resolved_page_workers == 8


# ---------------------------------------------------- statement coherence
class TestStatementsRefuseToLieWithAGrid:
    """A grid of dashes tells a reader nothing about why it is empty."""

    def _statement(self, rows: int, periods: list[str], dated: int) -> list[dict]:
        values, made = [], 0
        metrics = []
        for i in range(rows):
            vals = []
            for p in periods:
                if made < dated:
                    vals.append({"fact_id": f"f{made}", "period": p, "value": 1.0})
                    made += 1
                else:
                    vals.append({"fact_id": f"f{i}x{p}", "period": None,
                                 "value": 1.0})
            metrics.append({"entity_id": "e1", "canonical_metric": f"m{i}",
                            "metric_display": f"M{i}", "values": vals})
            values.extend(vals)
        return [{"category": "other", "metrics": metrics}]

    def test_a_mostly_empty_grid_is_refused(self):
        """The shape the IDFC deck produced: 13 rows, 2 periods, 3 filled."""
        from app.api.routes_intelligence import _comparative_verdict
        v = _comparative_verdict(self._statement(13, ["Q1 FY27", "Q4 FY26"], 3))
        assert v["renderable"] is False
        assert v["reason"] and "could not be attributed" in v["reason"]

    def test_a_well_populated_statement_renders(self):
        from app.api.routes_intelligence import _comparative_verdict
        v = _comparative_verdict(self._statement(4, ["Q1 FY27", "Q1 FY26"], 8))
        assert v["renderable"] is True
        assert v["periods"] == ["Q1 FY26", "Q1 FY27"]

    def test_no_periods_at_all_is_refused_and_says_so(self):
        from app.api.routes_intelligence import _comparative_verdict
        v = _comparative_verdict(self._statement(6, [], 0))
        assert v["renderable"] is False
        assert "no columns to compare" in v["reason"]
