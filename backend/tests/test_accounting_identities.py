"""The accounting checks, and the keys they name.

Two kinds of test here, and the first matters more than it looks.

**A rule that names a metric the taxonomy does not define never runs.** It
matches no fact, so it emits no result, so it never fails -- and a check that
silently does nothing is indistinguishable from a document that does not
support it. Four of the nine identity rules were in that state: "finance_cost"
for "finance_costs", "total_equity" for "net_worth", and two cash-flow names
that were never keys at all. Nothing in the suite could see it, because the
absence of a result is exactly what a passing document looks like.

**The identities themselves must hold on real figures**, and must not fire
where the accounting presentation makes them inapplicable.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from app.analysis.relations import COMPONENT_OF
from app.extraction.taxonomy import METRICS_BY_KEY

ENGINE = Path(__file__).resolve().parents[1] / "app" / "validation" / "engine.py"


def _identity_rules() -> list[tuple[str, str, list[str], list[str]]]:
    """The identity table, read from the source it is declared in."""
    src = ENGINE.read_text(encoding="utf-8")
    start = src.index("identities:")
    stop = src.index("out: list[ValidationResult] = []", start)
    block = src[start:stop]
    out = []
    for match in re.finditer(
            r'\(\s*"(?P<rule>[a-z_]+)"\s*,\s*"(?P<target>[a-z_]+)"\s*,\s*'
            r'\[(?P<plus>[^\]]*)\]\s*,\s*\[(?P<minus>[^\]]*)\]', block):
        def keys(text: str) -> list[str]:
            return re.findall(r'"([a-z_]+)"', text)
        out.append((match.group("rule"), match.group("target"),
                    keys(match.group("plus")), keys(match.group("minus"))))
    return out


class TestEveryRuleCanActuallyFire:

    def test_the_table_was_parsed(self):
        rules = _identity_rules()
        assert len(rules) >= 12, f"only found {len(rules)} identity rules"

    @pytest.mark.parametrize("rule,target,plus,minus", _identity_rules())
    def test_every_key_a_rule_names_exists(self, rule, target, plus, minus):
        for key in [target, *plus, *minus]:
            assert key in METRICS_BY_KEY, (
                f"rule {rule!r} names metric {key!r}, which the taxonomy does "
                f"not define -- the rule can never match a fact and so can "
                f"never run")

    def test_every_component_relationship_names_real_metrics(self):
        for component, whole in COMPONENT_OF.items():
            assert component in METRICS_BY_KEY, (
                f"{component!r} is not a metric")
            assert whole in METRICS_BY_KEY, f"{whole!r} is not a metric"

    def test_no_rule_checks_a_metric_against_itself(self):
        for rule, target, plus, minus in _identity_rules():
            assert target not in plus and target not in minus, (
                f"rule {rule!r} has {target!r} on both sides")


class TestTheIdentitiesAreAccountingTruths:
    """Each identity, checked as arithmetic on the figures a real statement
    prints. The numbers are Groww's consolidated statement, page 6."""

    def test_revenue_plus_other_income_is_total_income(self):
        assert 1501.42 + 47.25 == pytest.approx(1548.67)

    def test_current_plus_deferred_tax_is_total_tax(self):
        assert 260.40 + (-3.17) == pytest.approx(257.23)

    def test_profit_plus_oci_is_comprehensive_income(self):
        assert 735.04 + (-0.04) == pytest.approx(735.00)

    def test_profit_before_associates_less_share_is_pbt(self):
        assert 992.99 + (-0.72) == pytest.approx(992.27)

    def test_pbt_less_tax_is_pat(self):
        assert 992.27 - 257.23 == pytest.approx(735.04)

    def test_income_less_expenses_is_profit_before_associates(self):
        assert 1548.67 - 555.68 == pytest.approx(992.99)


class TestBankIdentities:
    """HDFC Bank's standalone results, page 1. A bank's income statement is
    shaped differently and needs its own identities."""

    def test_interest_plus_other_income_is_total_income(self):
        assert 79362.78 + 12821.60 == pytest.approx(92184.38)

    def test_interest_expended_plus_operating_is_total_expenditure(self):
        assert 45828.83 + 18187.49 == pytest.approx(64016.32)

    def test_employee_plus_other_operating_is_operating_expenses(self):
        assert 6414.83 + 11772.66 == pytest.approx(18187.49)

    def test_income_less_expenditure_is_operating_profit(self):
        assert 92184.38 - 64016.32 == pytest.approx(28168.06)
