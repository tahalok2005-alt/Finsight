"""The golden scorer, as Phase 22 rebuilt it.

The Phase 21 scorer paired a golden row with a fact by printed label and
value and then checked period, entity, unit and currency. It never checked the
canonical metric, so a figure read correctly and filed under the wrong measure
("Share-based payment reserve" as the period's expense) scored as correct; it
compared values within 0.5%, so SAP's diluted EPS 3.54 paired with basic EPS
3.55; and it paired by label only, so "16Minority Interest" (a row number run
into the label) was reported as a figure attached to the wrong row. And one
tag held records from three code versions.

These tests hold the rebuilt behaviour.
"""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from app.evaluation.golden import GoldenColumn, GoldenFact, GoldenTable, same_printed
from app.evaluation.provenance import code_fingerprint, same_state
from app.evaluation.scoring import score_table

Q1 = GoldenColumn(id="c1", period_end="2026-06-30", period_type="quarter",
                  period_start="2026-04-01")


def _g(row, value, *, metric, y=None, decimals=None, alt=()):
    return GoldenFact(row=row, column=Q1, value=value, unit="crore", currency="INR",
                      entity="Consolidated", metric=metric, metric_alt=tuple(alt),
                      metric_stated=True, y_rel=y, decimals=decimals)


def _table(*facts):
    return GoldenTable(doc_key="T", document="t.pdf", page=1, title="t", basis=None,
                       nature="", unit="crore", currency="INR", columns={"c1": Q1},
                       facts=list(facts))


def _f(fid, label, key, value, *, y=0.5, end="2026-06-30", review=False,
       ptype="quarter"):
    period = (SimpleNamespace(end_date=end, start_date="2026-04-01", period_type=ptype)
              if end else None)
    return SimpleNamespace(
        fact_id=fid, metric=label, canonical_metric=key, value=value,
        period_normalized=period, entity="Consolidated", entity_type="consolidated",
        unit="crore", currency="INR", value_type="monetary",
        requires_review=review, raw_value=str(value), original_text=label,
        provenance=SimpleNamespace(page_number=1, original_text=label,
                                   bbox_relative=SimpleNamespace(x=0.6, y=y - 0.005,
                                                                 width=0.05, height=0.01)))


class TestTheMetricIsScored:
    def test_a_correct_label_under_the_wrong_key_is_wrong(self):
        g = _g("Share-based payment reserve", 7920.0, metric=None)
        t = score_table(_table(g), [_f("f1", "Share-based payment reserve",
                                        "share_based_payment_expense", 7920.0)])
        v = t.verdicts[0]
        assert v.status == "wrong" and v.status_detail == "WRONG_METRIC"
        assert "metric" in v.wrong_dims

    def test_the_right_key_is_correct(self):
        g = _g("Profit before tax", 30630.0, metric="pbt")
        t = score_table(_table(g), [_f("f1", "Profit Before Tax", "pbt", 30630.0)])
        assert t.verdicts[0].status_detail == "CORRECT"

    def test_an_alternative_key_naming_the_same_measure_is_correct(self):
        g = _g("Interest expenses", 5170.0, metric="finance_costs",
               alt=["interest_expended"])
        t = score_table(_table(g), [_f("f1", "Interest expenses", "interest_expended",
                                        5170.0)])
        assert t.verdicts[0].status_detail == "CORRECT"

    def test_an_unflagged_wrong_metric_fails_the_gate_and_a_flagged_one_does_not(self):
        g = _g("Operating Profit", 30544.0, metric="pre_provision_operating_profit")
        silent = score_table(_table(g), [_f("f1", "Operating Profit", "ebit", 30544.0)])
        flagged = score_table(_table(g), [_f("f1", "Operating Profit", "ebit", 30544.0,
                                             review=True)])
        assert silent.unflagged_wrong()["metric"] == 1
        assert flagged.unflagged_wrong()["metric"] == 0
        assert flagged.wrong_facts() == [(["metric"], True)]


class TestValuesAreComparedAtTheirPrintedPrecision:
    def test_diluted_is_not_basic(self):
        g = _g("Earnings per share, basic", 3.55, metric="eps", decimals=2)
        assert not same_printed(g, 3.54)
        assert same_printed(g, 3.55)

    def test_a_whole_number_is_compared_whole(self):
        g = _g("Revenue", 30544.0, metric="revenue", decimals=0)
        assert same_printed(g, 30544.0)
        assert not same_printed(g, 30545.0)

    def test_the_nearest_figure_is_not_paired(self):
        basic = _g("Earnings per share, basic", 3.55, metric="eps", decimals=2, y=0.60)
        diluted = _g("Earnings per share, diluted", 3.54, metric="diluted_eps",
                     decimals=2, y=0.62)
        t = score_table(_table(basic, diluted),
                        [_f("f1", "Earnings per share, diluted", "diluted_eps", 3.54,
                            y=0.62)])
        by_row = {v.golden.row: v.status_detail for v in t.verdicts}
        assert by_row["Earnings per share, diluted"] == "CORRECT"
        assert by_row["Earnings per share, basic"] == "MISSING"


class TestAMultipleIsNeitherAnAmountNorAPercent:
    """NTPC's debt-equity ratio 1.09 is a multiple. Before this check a golden
    row in "ratio" fell into the magnitude branch and a correct ratio fact
    was scored as a ratio read in the wrong unit."""

    def _g(self):
        return GoldenFact(row="Debt equity ratio", column=Q1, value=1.09, unit="ratio",
                          currency=None, entity="Standalone", metric="debt_to_equity",
                          metric_stated=True, decimals=2)

    def test_a_ratio_fact_is_right(self):
        from app.evaluation.scoring import check_unit
        f = SimpleNamespace(value_type="ratio", unit="none")
        assert check_unit(self._g(), f)[0] == "ok"

    def test_a_ratio_read_as_an_amount_or_a_percent_is_wrong(self):
        from app.evaluation.scoring import check_unit
        assert check_unit(self._g(), SimpleNamespace(value_type="monetary", unit="crore"))[0] == "wrong"
        assert check_unit(self._g(), SimpleNamespace(value_type="percentage", unit="percent"))[0] == "wrong"

    def test_no_value_type_is_unknown_not_wrong(self):
        from app.evaluation.scoring import check_unit
        assert check_unit(self._g(), SimpleNamespace(value_type="unknown", unit="none"))[0] == "unknown"


class TestAFigureOnItsRowBelongsToIt:
    def _rows(self):
        # five rows 0.02 apart; enough label pairs to calibrate positions
        labels = ["Interest earned", "Other income", "Total income", "Interest expended",
                  "Operating expenses", "Minority Interest"]
        keys = ["interest_earned", "other_income", "total_income", "interest_expended",
                "operating_expenses", "nci_share_of_profit"]
        return [_g(l, 100.0 + i, metric=k, y=0.30 + 0.02 * i, decimals=2)
                for i, (l, k) in enumerate(zip(labels, keys))]

    def test_a_row_number_run_into_the_label_does_not_orphan_the_figure(self):
        golden = self._rows()
        facts = [_f(f"f{i}", g.row, g.metric, g.value, y=g.y_rel) for i, g in
                 enumerate(golden[:5])]
        facts.append(_f("f5", "16Minority Interest", "nci_share_of_profit", 105.0,
                        y=golden[5].y_rel))
        t = score_table(_table(*golden), facts)
        assert t.positional == "used"
        v = next(v for v in t.verdicts if v.golden.row == "Minority Interest")
        assert v.status_detail == "CORRECT" and v.paired_by.startswith("position")
        assert not [e for e in t.extras if e.kind == "wrong_metric"]

    def test_a_growth_rate_on_a_row_is_a_wrong_value(self):
        """SBI's slide: the Q1FY27-over-Q4FY26 growth (3.90%) filed as the
        row's Q4 FY26 interest income."""
        golden = self._rows()
        facts = [_f(f"f{i}", g.row, g.metric, g.value, y=g.y_rel) for i, g in
                 enumerate(golden)]
        facts.append(_f("fx", "Interest earned", "interest_earned", 3.90,
                        y=golden[0].y_rel, end="2026-03-31"))
        t = score_table(_table(*golden), facts)
        wrong = [e for e in t.extras if e.kind == "wrong_value"]
        assert len(wrong) == 1 and t.unflagged_wrong()["value"] == 1


class TestAnUndatedCoincidenceIsNotAnAccusation:
    def test_an_equal_value_from_an_unrelated_label_is_unscored(self):
        g = _g("Proceeds from sale / maturity of government securities", 210.0,
               metric="proceeds_from_sale_of_investments")
        t = score_table(_table(g), [
            _f("f1", "Proceeds from sale / maturity of government securities",
               "proceeds_from_sale_of_investments", 210.0, end=None),
            _f("f2", "-Share-based payment expense", "share_based_payment_expense",
               210.0, end=None)])
        assert [e.kind for e in t.extras] == ["unscored"]


class TestAnEvaluationIsOneState:
    def test_the_same_code_and_config_are_the_same_state(self):
        state = {"code": {"fingerprint": "a"}, "config": {"hash": "b"},
                 "manifest_sha256": "c"}
        assert same_state(state, dict(state)) == []

    @pytest.mark.parametrize("field,value,named", [
        ("code", {"fingerprint": "z"}, "code"),
        ("config", {"hash": "z"}, "config"),
        ("manifest_sha256", "z", "corpus manifest")])
    def test_any_difference_is_named(self, field, value, named):
        a = {"code": {"fingerprint": "a"}, "config": {"hash": "b"}, "manifest_sha256": "c"}
        b = {**a, field: value}
        assert same_state(a, b) == [named]

    def test_the_code_fingerprint_is_stable(self):
        assert code_fingerprint()["fingerprint"] == code_fingerprint()["fingerprint"]

    def test_a_tag_with_records_is_refused(self, tmp_path, monkeypatch):
        import scripts.corpus_run as cr
        runs = tmp_path / "runs"
        runs.mkdir()
        (runs / "GROWW.tagx.json").write_text(json.dumps({"provenance": {}}))
        monkeypatch.setattr(cr, "RUNS_DIR", runs)
        monkeypatch.setattr(cr, "EVALUATIONS_DIR", tmp_path / "evaluations")
        monkeypatch.setattr("sys.argv", ["corpus_run.py", "--tag", "tagx", "GROWW"])
        assert cr.main() == 3

    def test_a_tag_from_other_code_cannot_be_resumed(self, tmp_path, monkeypatch):
        import scripts.corpus_run as cr
        runs, evals = tmp_path / "runs", tmp_path / "evaluations"
        runs.mkdir()
        evals.mkdir()
        (runs / "GROWW.tagy.json").write_text(json.dumps({"provenance": {"run_id": "r"}}))
        (evals / "tagy.json").write_text(json.dumps({
            "run_id": "r", "code": {"fingerprint": "someone-else"},
            "config": {"hash": "x"}, "manifest_sha256": "y"}))
        monkeypatch.setattr(cr, "RUNS_DIR", runs)
        monkeypatch.setattr(cr, "EVALUATIONS_DIR", evals)
        monkeypatch.setattr("sys.argv", ["corpus_run.py", "--tag", "tagy", "--resume",
                                         "GROWW"])
        assert cr.main() == 3
