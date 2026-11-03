"""Whose statement about a figure's kind wins.

Every extraction strategy faces the same question and used to answer it
separately: this cell says "50.8 %", the table heading says "US$ million", and
the taxonomy says CASA deposits are an amount of money. Which is right?

The precedence is the same everywhere and it only runs one way:

    the cell itself  >  its row  >  its table or page  >  the taxonomy

A declaration attached to the figure beats one attached to the block the figure
sits in, and any declaration in the document beats a prior about what a metric
name usually means. The taxonomy knows that "revenue" is normally money; it
does not know that this particular column is a year-on-year change.

Why this had to be shared
-------------------------
The spatial reader already refused a percentage against a monetary metric --
a share-of-mix figure beside "Retail Deposits" in a chart legend is not the
deposits. The table reader did not, and produced ``casa_deposits = 50.80 %``
from a cell that says so in plain sight. Two paths, one question, two answers.

Nothing here parses a number or reads a taxonomy. It takes what each level of
the document declared and returns which declaration to believe, or says that
the evidence conflicts and cannot be resolved -- in which case the caller is
expected to record UNKNOWN and ask for review rather than pick the convenient
reading.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import IntEnum

from app.models.enums import ValueType


class Level(IntEnum):
    """Where a declaration was attached, weakest first."""
    TAXONOMY = 0
    DOCUMENT = 1
    TABLE = 2
    ROW = 3
    CELL = 4


#: Kinds that measure a proportion. A figure declared as one of these by the
#: source is never re-read as an amount of money.
RATE_KINDS = frozenset({ValueType.PERCENTAGE, ValueType.RATIO, ValueType.BPS})
#: Kinds that carry no scale of their own from a table or document heading.
SCALE_FREE_KINDS = frozenset({ValueType.PERCENTAGE, ValueType.RATIO,
                              ValueType.BPS, ValueType.COUNT,
                              ValueType.PER_SHARE})


@dataclass(frozen=True, slots=True)
class Claim:
    """One statement about what kind of figure this is, and who made it."""
    value_type: ValueType
    level: Level
    detail: str = ""


@dataclass(frozen=True, slots=True)
class Verdict:
    """The kind to use, the reason, and whether a person should look."""
    value_type: ValueType
    #: The strongest level that actually asserted something.
    level: Level
    reason: str
    conflicted: bool = False

    @property
    def takes_a_document_scale(self) -> bool:
        """Whether a table- or document-level magnitude may reach this figure.

        A percentage does not become "15.9 crore" because the statement was
        headed in crore, and a per-share figure does not become "56.47 million"
        because the deck reports in millions.
        """
        return self.value_type not in SCALE_FREE_KINDS


def resolve_value_type(claims: list[Claim]) -> Verdict:
    """Decide what kind of figure this is from everything that said so.

    The strongest level wins. Where two claims sit at the same level and
    disagree, nothing in the document settles it, and the verdict says so --
    ``conflicted`` is the signal to record UNKNOWN and ask for review rather
    than to take the first one.
    """
    stated = [c for c in claims if c.value_type != ValueType.UNKNOWN]
    if not stated:
        return Verdict(ValueType.UNKNOWN, Level.TAXONOMY,
                       "nothing in the document or the taxonomy said what kind "
                       "of figure this is")

    top = max(c.level for c in stated)
    at_top = [c for c in stated if c.level == top]
    kinds = {c.value_type for c in at_top}
    if len(kinds) > 1:
        names = ", ".join(sorted(str(k) for k in kinds))
        return Verdict(
            ValueType.UNKNOWN, top,
            f"the document describes this figure as both {names} at the same "
            f"level, and nothing available settles which is meant",
            conflicted=True)

    winner = at_top[0]
    overruled = [c for c in stated
                 if c.level < top and c.value_type != winner.value_type]
    if overruled:
        beaten = overruled[-1]
        return Verdict(
            winner.value_type, top,
            f"{winner.detail or _describe(winner.level)} overrules "
            f"{beaten.detail or _describe(beaten.level)}")
    return Verdict(winner.value_type, top,
                   winner.detail or _describe(winner.level))


def _describe(level: Level) -> str:
    return {
        Level.CELL: "the figure itself",
        Level.ROW: "its row",
        Level.TABLE: "its table heading",
        Level.DOCUMENT: "the document's declaration",
        Level.TAXONOMY: "what this metric name usually means",
    }[level]
