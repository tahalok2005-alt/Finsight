"""What makes two candidate figures the same financial fact.

A document states the same number more than once on purpose: a highlights tile
on page 2, a statement row on page 18, a sentence in the commentary. Three
strategies read those three places and produce three candidates. Deciding
whether they describe one fact -- and if so whether they agree -- is a separate
question from how any of them was read, and it is answered here.

The identity deliberately excludes four things
----------------------------------------------
**The value.** Two candidates with the same identity and different values are
the interesting case; putting the value in the identity would hide it.

**The printed label.** "Depreciation" and "Depreciation / Amortisation" are the
same line of the same statement, printed twice in one document, carrying the
same number. Keying on the label put them in different groups and lost the
corroboration -- 36 such groups on one filing. The label still matters, but as
an *explanation* of a difference, not as a barrier to noticing one.

**The unit and the currency.** Ten thousand million and one thousand crore are
the same amount. Keeping scale in the identity made the comparison that knows
this unreachable. Scale belongs in *comparability*, which is asked after the
group is formed and can answer "these cannot be compared" without pretending
they are unrelated.

**The source.** Which strategy read a figure says nothing about which figure it
is. That is the whole point of reconciling across strategies.

What it includes, and why each one is a real divider
----------------------------------------------------
Metric, entity, period, period type and reporting basis. Two figures differing
in any of these are different facts, not disagreeing ones -- the brief for this
layer says so and the arithmetic agrees: this quarter's revenue does not
contradict last quarter's.
"""
from __future__ import annotations

from dataclasses import dataclass

from app.models.enums import Currency, MagnitudeUnit, ValueType
from app.models.schemas import FinancialFact

#: How far two values may differ and still be the same number. Documents round
#: the same figure differently between a highlights box and a statement.
REL_TOLERANCE = 0.005
ABS_TOLERANCE = 0.01

#: Value types that measure an amount of money and can therefore be compared
#: once scale is normalised. Everything else compares only against its own kind.
_MONETARY_KINDS = frozenset({ValueType.MONETARY})


@dataclass(frozen=True, slots=True)
class FactIdentity:
    """The slot a figure occupies in a document's financial picture."""
    metric: str
    entity: str
    period: str
    period_type: str
    basis: str

    @property
    def is_undated(self) -> bool:
        return not self.period


def identity_of(fact: FinancialFact) -> FactIdentity:
    """The identity of one candidate. Deterministic and value-free."""
    metric = (fact.canonical_metric
              or " ".join((fact.metric or "").lower().split()).strip(" :*-"))
    period = fact.period or ""
    period_type = ""
    if fact.period_normalized is not None:
        period_type = str(fact.period_normalized.period_type or "")
    return FactIdentity(
        metric=metric,
        entity=fact.entity_id or fact.entity or "",
        period=period,
        period_type=period_type,
        basis=str(getattr(fact, "nature", "") or ""),
    )


def printed_label(fact: FinancialFact) -> str:
    """The row label as the document printed it, reduced for comparison."""
    return " ".join((fact.metric or "").lower().split()).strip(" :*-")


def comparable(a: FinancialFact, b: FinancialFact) -> bool:
    """Whether these two figures can be held against each other at all.

    Comparability is not agreement. Two figures may be perfectly comparable and
    disagree, which is a contradiction; or be incomparable, which is not a
    contradiction and must never be reported as one.
    """
    if a.value is None or b.value is None:
        return False
    if a.value_type != b.value_type:
        return False
    if a.value_type in _MONETARY_KINDS:
        # Different currencies are not comparable, whatever the numbers say.
        # A rupee figure beside a dollar one is two statements about the same
        # quantity in different money, and this layer has no exchange rate.
        known = {a.currency, b.currency} - {Currency.UNKNOWN, None}
        if len(known) > 1:
            return False
        # Different scales are comparable through the base value, which is what
        # `base_value` exists for.
        if a.unit != b.unit:
            return a.base_value is not None and b.base_value is not None
    return True


def agrees(a: FinancialFact, b: FinancialFact) -> bool:
    """Whether two comparable figures are the same number.

    Compared on the base value where the scales differ, so a figure in crore
    and the same figure in million agree instead of contradicting.
    """
    if not comparable(a, b):
        return False
    left, right = a.value, b.value
    if (a.unit != b.unit and a.base_value is not None
            and b.base_value is not None):
        left, right = a.base_value, b.base_value
    if left is None or right is None:
        return False
    if abs(left - right) <= ABS_TOLERANCE:
        return True
    scale = max(abs(left), abs(right)) or 1.0
    return abs(left - right) / scale <= REL_TOLERANCE


def scale_note(a: FinancialFact, b: FinancialFact) -> str | None:
    """A phrase describing a scale difference the comparison saw through."""
    if a.unit == b.unit:
        return None
    left = a.unit if a.unit not in (None, MagnitudeUnit.NONE) else "no scale"
    right = b.unit if b.unit not in (None, MagnitudeUnit.NONE) else "no scale"
    return f"the same amount stated in {left} and in {right}"
