"""Canonical financial metric taxonomy.

Structure
---------
Each metric is placed on three orthogonal axes rather than in one flat list:

* ``statement`` — which formal financial statement it belongs to
  (income statement, balance sheet, cash flow, ratio, segment, operational)
* ``category``  — its analytical grouping (revenue, earnings, margins, …)
* ``family``    — the roll-up group used for comparison views

Terminology is never destroyed.  A fact keeps the label exactly as the document
wrote it (``metric``), the canonical key (``canonical_metric``) and a display
name (``metric_display``).  Canonicalisation maps *spellings* onto one measure;
it must never merge two genuinely different measures.

That last point is enforced, not merely intended.  "Revenue", "Gross Revenue",
"Revenue from Operations" and "Net Revenue" are related but different, so each
keeps its own key and they are linked through ``family``.  The same applies to
"Profit After Tax" versus "Profit After Tax and Share of Profit of Associates &
JVs": a results table lists both, and collapsing them would make a
period-over-period comparison compare different things.  ``distinct_from``
records such near-neighbours explicitly so the separation is testable.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from functools import lru_cache

from app.models.enums import MetricCategory, StatementType, ValueType

_PUNCT = re.compile(r"[^a-z0-9%&/ ]+")
_WS = re.compile(r"\s+")
_NOISE = re.compile(
    r"\b(total|consolidated|standalone|reported|for the (period|quarter|year)|"
    r"as per|ind as|gaap|the|and|of|in|on|at|rs|inr|crore|cr|million|mn|"
    r"billion|bn|lakh|figures?)\b")
_TRAILING_UNIT = re.compile(r"\((?:%|rs|inr|₹|\$|in [^)]*)\)\s*$", re.I)
# any short trailing parenthetical, used only for a fallback match attempt
_TRAILING_PAREN = re.compile(r"\s*\([^)]{1,30}\)\s*$")
_FOOTNOTE_MARK = re.compile(r"[*#†‡^]+|\(\d\)|\[\d\]")

#: Characters a text recogniser confuses, collapsed to one representative.
#:
#: These are properties of the recogniser, not of any filer: an "m" set tightly
#: reads as "rn" and back, "l" and "i" and "1" differ by a dot or a serif, and
#: "a", "o" and "0" close up at low resolution. A scanned bank statement in the
#: corpus produces "Interest eamed", "i)Empioyees cost" and "Operating Prafit
#: befare provisians" for labels whose correct spellings the taxonomy knows.
#:
#: The key is applied to BOTH sides of a comparison, so it is a normalisation
#: in the same sense as lowercasing -- not a loosened threshold and not a wider
#: neighbourhood. Two labels that differ only by a confusion the recogniser is
#: known to make become identical; two labels that differ by anything else stay
#: as far apart as they were.
_RECOGNITION_CLASSES = (
    ("rn", "m"),            # contract first: "earned" and "eamed" meet at "eamed"
    ("cl", "d"),
    ("il", "u"), ("ii", "u"),
)
_RECOGNITION_CHARS = str.maketrans({
    "l": "i", "1": "i", "|": "i",
    "a": "o", "0": "o",
    "5": "s", "8": "b",
})


@lru_cache(maxsize=65536)
def recognition_key(text: str) -> str:
    """A spelling reduced to what the recogniser could not have distinguished."""
    t = (text or "").lower()
    for wrong, right in _RECOGNITION_CLASSES:
        t = t.replace(wrong, right)
    t = t.translate(_RECOGNITION_CHARS)
    return re.sub(r"[^a-z0-9]", "", t)


#: Words that flip or fence off what a label refers to.
#:
#: A fuzzy score measures how alike two strings look, and these words are the
#: case where looking alike means nothing: "Total non-current assets" and
#: "Total current assets" differ by one short token and score 0.91, yet they
#: are opposite halves of a balance sheet.  So a fuzzy match is refused when
#: one side carries one of these and the other does not.
#:
#: Only fuzzy matching is affected.  A metric that deliberately declares such a
#: label as an alias still matches it exactly — the rule governs guesses, not
#: the taxonomy's own statements about itself.
_CONTRAST_WORDS = frozenset({
    "non", "excluding", "before", "after", "discontinued", "continuing",
    "attributable", "minority", "diluted", "basic",
    # "Total" and "Other" name a different line of the same statement, not a
    # different wording of the same one. Without them here, fuzzy matching
    # scored "total comprehensive income" against "other comprehensive income"
    # at 0.96 and merged two lines that differ by the whole profit for the
    # period; and "total expenditure" against "capital expenditure" at 0.83,
    # which is a bank's entire cost base reported as capex. A near-neighbour
    # that a reader would never confuse is exactly what a fuzzy score cannot
    # see.
    "total", "other",
    # Cash is what a cash-flow line measures and an income-statement line does
    # not: "Results from operating activities" -- operating profit, in IFRS
    # wording -- scored against "net cash from operating activities" and was
    # reported as operating cash flow.
    "cash",
    # "Items that will not be reclassified to profit or loss" and "Items that
    # will be reclassified ..." are the two halves of other comprehensive
    # income; one word apart, they are opposites. Looked for as the phrase,
    # squashed, so that "not" inside "note" or "another" does not count.
    "willnotbe",
    # A reserve is a balance in equity; the thing it is named after is a
    # charge. "Share-based payment reserve" scored against "share-based
    # payment expense" -- "expense" is a neutral word -- and a balance-sheet
    # line was reported as the period's cost. ("Reserve Bank" is a name.)
    "reserve",
})


@lru_cache(maxsize=65536)
def _contrast_words_in(text: str) -> frozenset[str]:
    """The qualifiers a label carries, found without relying on its spaces.

    OCR runs words together — a degraded scan of "Profit Before Tax" reads
    ``ProfitBeforeTax`` — and splitting on whitespace would then find no
    qualifier at all and call the label unqualified. That turns a rule meant to
    catch opposites into one that rejects a correct match, which is how this
    was first noticed: it cost 32 facts on the scanner-degraded benchmark.
    """
    squashed = text.replace(" ", "").replace("reservebank", "")
    # Also looked for in the recognition-reduced spelling, so a damaged
    # qualifier is still found: a scan reading "befare" for "before" otherwise
    # leaves the label unqualified while its alias is qualified, and the rule
    # meant to keep opposites apart rejects a correct match instead.
    reduced = recognition_key(text).replace("reservebonk", "")
    return frozenset(w for w in _CONTRAST_WORDS
                     if w in squashed or recognition_key(w) in reduced)


def _contrast_mismatch(a: str, b: str) -> bool:
    """True when one normalised label is qualified and the other is not."""
    return (_contrast_words_in(a) != _contrast_words_in(b)
            or _subjects_differ(a, b))


#: What a measure is OF, in classes a similarity score cannot see across.
#: "Equity attributable to owners of the Company" scored 0.91 against "profit
#: attributable to owners of the company" and a balance-sheet total was filed
#: as the period's profit; "Profit on sale of property, plant and equipment"
#: scored against "proceeds from sale of property, plant and equipment" and a
#: gain was filed as the cash received (Phase 22). Two labels whose subjects
#: fall in different classes, with none in common, name different measures.
_SUBJECT_CLASSES = {
    "profit": "result", "loss": "result", "earnings": "result", "gain": "result",
    "equity": "equity", "networth": "equity", "reserves": "equity",
    "proceeds": "cash", "receipts": "cash", "payment": "cash", "payments": "cash",
    "repayment": "cash", "repayments": "cash", "purchase": "cash", "purchases": "cash",
    "assets": "position", "liabilities": "position",
}


@lru_cache(maxsize=65536)
def _subject_classes(text: str) -> frozenset[str]:
    words = text.replace("net worth", "networth").split()
    return frozenset(_SUBJECT_CLASSES[w] for w in words if w in _SUBJECT_CLASSES)


def _subjects_differ(a: str, b: str) -> bool:
    ca, cb = _subject_classes(a), _subject_classes(b)
    return bool(ca and cb and not (ca & cb))


#: Words that appear in the name of almost every financial measure. Two labels
#: sharing only these have not been shown to be about the same thing.
_GENERIC_TOKENS = frozenset({
    "expense", "expenses", "income", "cost", "costs", "profit", "loss",
    "revenue", "ratio", "ratios", "margin", "margins", "total", "net",
    "gross", "value", "amount", "other", "and", "of", "the", "for", "from",
    "per", "share", "average", "adjusted",
})


def _share_a_distinctive_word(a: str, b: str) -> bool:
    """Whether two labels have any word in common that means something.

    A sequence-similarity score measures how alike two strings *look*, and on
    short financial labels that is frequently a coincidence: "IT Expenses" and
    "Tax Expenses" score 0.87, share only the word every expense line contains,
    and are entirely different things. Reporting the bank's technology spend as
    its tax charge is the kind of error nothing downstream can catch.

    So a fuzzy match must also be able to point at one substantive word both
    labels use. Exact and prefix matches are unaffected -- they are the
    taxonomy recognising its own declared wording, not a guess.
    """
    ta = {w for w in a.split() if len(w) > 2 and w not in _GENERIC_TOKENS}
    tb = {w for w in b.split() if len(w) > 2 and w not in _GENERIC_TOKENS}
    if not ta and not tb:
        # Both sides are made entirely of common words -- "total income" and
        # "net income". There is nothing distinctive on either side to agree
        # on, so the ordinary similarity threshold is left to decide.
        return True
    if ta & tb:
        return True
    # OCR loses spaces, so "ProfitBeforeTax" arrives as a single token and
    # would share nothing with "profit before tax" word-for-word. Looking for
    # the distinctive word inside the other side's text finds it, and does not
    # find "tax" inside "itexpenses".
    squashed_a, squashed_b = a.replace(" ", ""), b.replace(" ", "")
    if any(w in squashed_b for w in ta) or any(w in squashed_a for w in tb):
        return True
    # The same containment test on the recognition-reduced spellings, so a word
    # the recogniser damaged can still be recognised as shared. Applied to both
    # sides, so it cannot bring together words that were never alike.
    reduced_a, reduced_b = recognition_key(a), recognition_key(b)
    if any(recognition_key(w) in reduced_b for w in ta) or             any(recognition_key(w) in reduced_a for w in tb):
        return True
    # One side names something specific and the other does not. They cannot be
    # about the same measure, however alike they read: "IT Expenses" carries no
    # distinctive word at all, while "Tax Expenses" carries "tax".
    return False


#: The temporal phrase a statement appends to almost every row of its profit
#: and loss: "for the period/year", "for the quarter", "for the year ended".
#:
#: It says *when*, never *what*, so it is removed before a label is compared
#: with anything -- unlike the noise list below, which only feeds a fallback
#: lookup. Leaving it in was not a small inaccuracy: the slash survives
#: punctuation stripping, so "Profit before tax for the period/year" normalised
#: to "profit before tax for the period year" and scored below threshold
#: against its own alias "profit before tax". Sixty-eight golden facts across
#: two statements were lost to that one trailing phrase.
#:
#: Matched anywhere in the label, not only at the end. A statement that groups
#: rows under a heading puts the phrase in the middle -- "Profit for the
#: period/year attributable to: Shareholders of the Company" -- and an
#: end-anchored pattern leaves it there, where it defeats the alias it should
#: have reached.
_SPAN_WORD = (r"(?:period|quarter|year|half[\s-]*year|nine\s+months?|"
              r"six\s+months?|three\s+months?|month)")
_PERIOD_QUALIFIER = re.compile(
    r"\s*(?:,\s*)?\bfor\s+the\s+" + _SPAN_WORD +
    r"(?:\s*[/&]\s*" + _SPAN_WORD + r")*"
    r"(?:\s+ended\b[^,;]*)?", re.I)


# ---------------------------------------------------------------------------
# What a row label carries besides the name of what it measures.
#
# A statement numbers its rows, points them at notes, shows how a subtotal is
# built and says what the share's face value is -- all inside the label. None
# of it says what the row measures, and all of it defeats an exact lookup:
# "(d) Others", "Paid-up equity share capital [Face value ₹ 1 per share]",
# "PROFIT/(LOSS) FROM ORDINARY ACTIVITIES BEFORE TAX (7)-(8)+(9)",
# "(c) Other income (Refer Note 4)". Found across the Phase 21 corpus, where it
# accounted for a large share of the rows the taxonomy did not recognise.
#
# Each pattern removes a construction that is annotation by its FORM, and is
# applied to both sides of every comparison -- the taxonomy's aliases and the
# document's labels -- so it is a normalisation in the same sense as
# lowercasing, never a loosening of what counts as a match.
# ---------------------------------------------------------------------------
_ENUM = r"(?:[a-z]|[ivx]{1,4}|\d{1,2})"
#: "(a)", "( iv )", "(22)" anywhere in the label.
_ENUM_IN_PARENS = re.compile(r"\(\s*" + _ENUM + r"\s*\)")
#: "a)", "iv)" after a space or at the start -- the closing bracket alone.
_ENUM_HALF_PAREN = re.compile(r"(?:(?<=\s)|^)" + _ENUM + r"\)\s*")
#: "(a+b+c+d)", "(3-6)", "(7- 8+9)", "(A+B+C)": how a subtotal is built.
_FORMULA = re.compile(r"\(\s*[a-z0-9]{1,3}(?:\s*[-+]\s*[a-z0-9]{1,3})+\s*\)")
#: "[7 - 8]", "[1(a) + 1(b)]", "[net (charge) / credit]", "[Face value ₹ 1]".
_BRACKETED = re.compile(r"\[[^\]]{0,60}\]")
#: "(Refer Note 4)", "(Refer Nate9)" as OCR reads it, "(Note 3)".
_NOTE_REF_IN_LABEL = re.compile(
    r"\(\s*(?:refer|see)?\s*n[a-z]?tes?\s*(?:no\.?)?\s*[0-9a-z.,&\s]{0,12}\)"
    r"|\b(?:refer|see)\s+n[a-z]?tes?\s*(?:no\.?)?\s*[0-9a-z.]{1,4}\b")
#: "(Face value: ₹ 5 each)", "(Face yalue of 1 pershare)".
_FACE_VALUE = re.compile(r"\(\s*face\s*[vy]alue[^)]*\)")
#: A currency or an empty bracket where OCR dropped the glyph: "(₹)", "(INR)",
#: "(in Rs.)", "()"; and "- in Rupees".
_CURRENCY_PAREN = re.compile(
    r"\(\s*(?:in\s+)?(?:rs\.?|inr|₹|usd|us\$|\$|rupees?|eur|€)?\s*\.?\s*\)"
    r"|\s-?\s*\bin\s+rupees\b")
#: "(not annualised)", "(annualised)".
_ANNUALISED = re.compile(r"\(?\s*(?:not\s+)?annuali[sz]ed\s*\)?")
#: "(net of write back)", "(net of tax)", ", net of tax expense".
_NET_OF = re.compile(r"\(\s*net\s*of\s*[^)]{1,30}\)|,?\s*\bnet\s+of\s+tax(?:\s+expense)?\b")
#: "(including capital work-in-progress and capital advances)": what a line
#: includes, not what it is.
_INCLUDING = re.compile(r"\(\s*(?:including|includes|incl\.?)[^)]*\)")
#: Leading dashes and bullets: "- Trade payables", "---of which ...".
_LEADING_BULLET = re.compile(r"^[\s\-–—•·*~]+")
#: Leading "Less:", "Add:", "of which", "out of which".
_LEADING_OPERATOR = re.compile(r"^(?:less|add)\s*:\s*|^(?:out\s+)?of\s*which\s+")
#: A leading row number, roman numeral or letter: "1.", "iv)", "XV ", "A ",
#: "16Minority" as OCR joins it.
_LEADING_ENUM = re.compile(
    r"^(?:" + _ENUM + r"\s*[.)]\s*|(?:[ivx]{1,4}|[a-h])\s+(?=[a-z])"
    r"|\d{1,2}\s+(?=[a-z])|\d{1,2}(?=[a-z]{3}))")
#: A trailing enumerator that OCR moved behind the words: "Others (d)".
_TRAILING_ENUM = re.compile(r"\s*\(\s*" + _ENUM + r"?\s*\)\s*$")
#: A trailing row number that OCR read into the label ("... after tax 12").
#: Not after a word that takes a numeral ("Tier 1", "Level 2").
_TRAILING_NUMBER = re.compile(
    r"(?<!\btier)(?<!\blevel)(?<!\bstage)(?<!\bphase)(?<!\bcet)(?<!\bbasel)"
    r"(?<!\bgrade)(?<!\bclass)(?<!\bseries)(?<!\bpillar)(?<!\btranche)"
    r"\s+\d{1,2}\s*$")


_SLASH = re.compile(r"\s*/\s*")


_DANGLING_ATTRIBUTION = re.compile(r"\s*\battributable\s+to\s*:?\s*$")


@lru_cache(maxsize=65536)
def normalize_label(text: str) -> str:
    """Canonical form used for alias lookup and fuzzy comparison."""
    t = (text or "").lower().strip()
    t = _FOOTNOTE_MARK.sub(" ", t)
    t = _FACE_VALUE.sub(" ", t)
    t = _NOTE_REF_IN_LABEL.sub(" ", t)
    t = _BRACKETED.sub(" ", t)
    t = _FORMULA.sub(" ", t)
    t = _ENUM_IN_PARENS.sub(" ", t)
    t = _CURRENCY_PAREN.sub(" ", t)
    t = _ANNUALISED.sub(" ", t)
    t = _NET_OF.sub(" ", t)
    t = _INCLUDING.sub(" ", t)
    t = _TRAILING_UNIT.sub(" ", t)
    t = _PERIOD_QUALIFIER.sub("", t)
    t = _WS.sub(" ", t).strip()
    for _ in range(3):
        before = t
        t = _LEADING_BULLET.sub("", t)
        t = _LEADING_OPERATOR.sub("", t)
        t = _LEADING_ENUM.sub("", t)
        t = _ENUM_HALF_PAREN.sub(" ", t).strip()
        if t == before:
            break
    t = _TRAILING_ENUM.sub("", t)
    if len(t.split()) >= 3:
        t = _TRAILING_NUMBER.sub("", t)
    # "attributable to:" with no one after it introduces the lines below and
    # says nothing of this figure: "Profit for the quarter / year attributable
    # to :" is the total, and was taken for the owners' share.
    t = _DANGLING_ATTRIBUTION.sub("", t)
    t = t.replace("&", " and ")
    t = _PUNCT.sub(" ", t)
    # "Profit / (Loss)" and "Profit/(Loss)" are one spelling.
    t = _SLASH.sub("/", t)
    t = _WS.sub(" ", t).strip()
    return t


def _strip_noise(t: str) -> str:
    return _WS.sub(" ", _NOISE.sub(" ", t)).strip()


@dataclass(frozen=True, slots=True)
class MetricDef:
    key: str
    display: str
    family: str
    aliases: tuple[str, ...]
    statement: StatementType = StatementType.UNKNOWN
    category: MetricCategory = MetricCategory.OTHER
    value_type: ValueType = ValueType.MONETARY
    sign: str = "any"            # "positive" | "any"
    is_flow: bool = True         # flow (period) vs stock (point-in-time)
    is_operational: bool = False
    definition: str = ""
    #: Metrics this one is routinely confused with but must stay separate from.
    distinct_from: tuple[str, ...] = ()
    notes: str = ""
    #: Labels that name this metric only in a statement of balances -- one
    #: whose columns are headed "As at". "Non-controlling interests" in a
    #: balance sheet is the equity line; the same words in a statement of
    #: profit are the minority's share of the profit.
    stock_aliases: tuple[str, ...] = ()
    #: Labels that name this metric only in a statement of flows -- one whose
    #: columns are headed "... ended".
    flow_aliases: tuple[str, ...] = ()
    #: The metric the same words name when the figure is of the other kind --
    #: an amount rather than its rate, or the reverse. "Gross NPA" is printed
    #: both as crore and as a percentage of advances.
    twin: str = ""

    @property
    def is_rate(self) -> bool:
        """True for measures where a *relative* change is meaningless."""
        return self.value_type in (ValueType.PERCENTAGE, ValueType.RATIO,
                                   ValueType.BPS)


def _m(key: str, display: str, family: str, aliases: list[str], **kw) -> MetricDef:
    for k in ("stock_aliases", "flow_aliases", "distinct_from"):
        if k in kw:
            kw[k] = tuple(kw[k])
    return MetricDef(key=key, display=display, family=family,
                     aliases=tuple(aliases), **kw)


PCT = ValueType.PERCENTAGE
MON = ValueType.MONETARY
CNT = ValueType.COUNT
RAT = ValueType.RATIO
PS = ValueType.PER_SHARE

IS = StatementType.INCOME_STATEMENT
BS = StatementType.BALANCE_SHEET
CF = StatementType.CASH_FLOW
RATIO_ST = StatementType.RATIO
SEG = StatementType.SEGMENT
OPS = StatementType.OPERATIONAL

C = MetricCategory

METRICS: tuple[MetricDef, ...] = (
    # ======================================================= INCOME STATEMENT
    _m("revenue", "Revenue", "revenue",
       ["revenue", "total revenue", "value of sales and services",
        "value of sales & services (revenue)", "revenues", "total revenues"],
       statement=IS, category=C.REVENUE, sign="positive",
       definition="Top-line revenue as labelled by the document.",
       distinct_from=("gross_revenue", "revenue_from_operations", "net_revenue")),
    _m("gross_revenue", "Gross Revenue", "revenue",
       ["gross revenue", "gross turnover", "gross sales"],
       statement=IS, category=C.REVENUE, sign="positive",
       definition="Revenue before deducting indirect taxes such as GST.",
       distinct_from=("revenue", "revenue_from_operations", "net_revenue")),
    _m("revenue_from_operations", "Revenue from Operations", "revenue",
       ["revenue from operations", "operating revenue", "operational revenue",
        "revenue from operation", "income from operations",
        "total revenue from operations"],
       statement=IS, category=C.REVENUE, sign="positive",
       definition="Revenue net of indirect taxes, from operating activity only.",
       distinct_from=("gross_revenue", "revenue", "total_income")),
    _m("net_revenue", "Net Revenue", "revenue",
       ["net revenue", "net sales", "net turnover"],
       statement=IS, category=C.REVENUE, sign="positive",
       distinct_from=("gross_revenue", "revenue")),
    # "Sales / income from operations" is line 1(a) of the SEBI results
    # format -- a component, beside "Other operating revenues" (1(b)), of the
    # total revenue from operations. Filed as the total, a steel maker's two
    # lines carried two different figures under one metric.
    _m("sales", "Sales", "revenue", ["sales", "turnover",
                                    "sales / income from operations"],
       statement=IS, category=C.REVENUE, sign="positive"),
    _m("total_income", "Total Income", "revenue", ["total income"],
       statement=IS, category=C.REVENUE, sign="positive",
       definition="Revenue from operations plus other income.",
       distinct_from=("revenue_from_operations", "revenue")),
    _m("other_income", "Other Income", "revenue", ["other income"],
       statement=IS, category=C.REVENUE),
    _m("investment_income", "Investment Income", "revenue",
       ["investment income", "income from investments", "income on investments"],
       statement=IS, category=C.REVENUE),
    _m("segment_revenue", "Segment Revenue", "revenue",
       ["segment revenue", "segment sales", "revenue by segment"],
       statement=SEG, category=C.SEGMENT, sign="positive"),

    _m("ebitda", "EBITDA", "earnings",
       ["ebitda", "earnings before interest tax depreciation and amortisation",
        "operating profit before depreciation"],
       statement=IS, category=C.EARNINGS,
       definition="Earnings before interest, tax, depreciation and amortisation."),
    _m("segment_ebitda", "Segment EBITDA", "earnings",
       ["segment ebitda", "ebitda by segment"],
       statement=SEG, category=C.SEGMENT),
    _m("ebit", "EBIT", "earnings",
       ["ebit", "earnings before interest and tax", "operating profit",
        "results from operating activities"],
       statement=IS, category=C.EARNINGS,
       definition="Earnings before interest and tax.",
       distinct_from=("ebitda",)),
    _m("segment_ebit", "Segment EBIT", "earnings",
       ["segment ebit", "segment operating profit"],
       statement=SEG, category=C.SEGMENT),
    _m("pbt", "Profit Before Tax", "earnings",
       ["profit before tax", "pbt", "profit before taxes",
        "profit/(loss) before tax", "profit (loss) before tax",
        "profit from ordinary activities before tax",
        "profit/(loss) from ordinary activities before tax",
        "profit from ordinary activities before tax and minority interest",
        "profit/(loss) from ordinary activities before tax and minority interest"],
       statement=IS, category=C.EARNINGS,
       definition="Profit before the tax charge, after the share of "
                  "associates where the statement presents it that way.",
       distinct_from=("pbt_before_associates", "pbt_before_exceptional"),
       notes="'Profit before exceptional items and tax' was an alias until "
             "Phase 21. A statement with exceptional items prints both lines "
             "with different figures, and they were reported as one metric."),
    _m("pbt_before_exceptional", "Profit Before Exceptional Items and Tax",
       "earnings",
       ["profit before exceptional items and tax",
        "profit/(loss) before exceptional items and tax",
        "profit before tax and exceptional items",
        "profit from ordinary activities before exceptional items and tax",
        "profit/(loss) from ordinary activities before exceptional items and tax"],
       statement=IS, category=C.EARNINGS,
       definition="Profit before exceptional items and tax.",
       distinct_from=("pbt",)),
    _m("pbt_before_associates",
       "Profit Before Share of Associates and Tax", "earnings",
       ["profit before share of net loss of associate and tax",
        "profit before share of profit loss of associate and tax",
        "profit before share of net profit of associate and tax",
        "profit before share of associates and tax",
        "profit before exceptional items share of associates and tax",
        # Schedule III's wording, and the order an FMCG statement prints it:
        # the line comes before the associate's share AND before exceptional
        # items, ahead of "profit before exceptional items and tax".
        "profit before exceptional items, share of net profits of investments "
        "accounted for using equity method and tax",
        "profit before exceptional items and tax and before share of "
        "equity-accounted investee",
        "profit before exceptional items and tax and before share of "
        "equity-accounted investees"],
       statement=IS, category=C.EARNINGS,
       definition="Profit before tax and before the share of associates. An "
                  "Ind-AS statement lists this, then the associate share, then "
                  "profit before tax -- three consecutive rows with three "
                  "different values.",
       distinct_from=("pbt", "share_of_associates")),
    _m("share_of_associates", "Share of Profit/(Loss) of Associates",
       "earnings",
       ["share of net loss of associate accounted for using equity method",
        "share of profit loss of associate accounted for using equity method",
        "share of net profit of associate", "share of profit of associates",
        "share of loss of associates", "share of profit loss of associates",
        "share of profit loss of associates and joint ventures"],
       statement=IS, category=C.EARNINGS,
       definition="The reporting entity's share of an associate's result, "
                  "brought in under the equity method.",
       distinct_from=("pbt", "pbt_before_associates")),
    _m("pat", "Profit After Tax", "earnings",
       ["profit after tax", "pat", "profit for the period", "profit for the year",
        "net profit", "profit/(loss) for the period", "net profit/(loss) for the period",
        "net profit for the period", "net profit after tax",
        "net profit/(loss) after tax", "profit/(loss) after tax",
        "net profit after tax before minority interest"],
       statement=IS, category=C.EARNINGS,
       definition="Profit after tax, before the share of associates and JVs.",
       distinct_from=("pat_after_associates", "net_income")),
    _m("pat_after_associates",
       "Profit After Tax incl. Share of Associates & JVs", "earnings",
       ["profit after tax and share of profit loss of associates and jvs",
        "profit after tax and share of profit loss of associates and jv",
        "profit after tax and share of profit loss of associates and joint ventures",
        "profit after tax and share of profit of associates and jvs",
        "profit after tax and share of"],
       statement=IS, category=C.EARNINGS,
       definition="Profit after tax including the share of associates and JVs. "
                  "A distinct measure from PAT; results tables list both.",
       distinct_from=("pat",)),
    _m("net_income", "Net Income", "earnings",
       ["net income", "net earnings"],
       statement=IS, category=C.EARNINGS, distinct_from=("pat",)),
    _m("segment_profit", "Segment Profit", "earnings",
       ["segment profit", "segment results", "segment result"],
       statement=SEG, category=C.SEGMENT),
    _m("share_of_associates", "Share of Profit of Associates & JV", "earnings",
       ["share of profit loss of associates and jv", "share of profit of associates",
        "share of profit loss of associates and joint ventures",
        "share of associates"],
       statement=IS, category=C.EARNINGS),
    _m("exceptional_items", "Exceptional Items", "earnings",
       ["exceptional items", "exceptional item", "total exceptional items"],
       statement=IS, category=C.EARNINGS),
    _m("other_comprehensive_income", "Other Comprehensive Income", "earnings",
       ["other comprehensive income", "oci",
        "other comprehensive income net of tax",
        "other comprehensive income/(loss)",
        "total other comprehensive income",
        "total other comprehensive income/(loss)",
        "other comprehensive income/(loss) net of tax"],
       statement=IS, category=C.EARNINGS,
       definition="Gains and losses recognised outside profit or loss -- "
                  "remeasurements, translation differences, fair-value moves.",
       distinct_from=("total_comprehensive_income", "pat"),
       notes="Held apart from total comprehensive income, which is this plus "
             "the profit for the period. They were the same key until Phase "
             "20, so a statement listing both -- and Ind-AS statements list "
             "both -- reported two different figures under one metric."),
    _m("pat_attributable_to_owners",
       "Profit Attributable to Owners", "earnings",
       ["profit attributable to shareholders of the company",
        "profit attributable to owners of the company",
        "profit attributable to equity holders of the parent",
        "profit attributable to owners", "profit attributable to shareholders",
        "profit attributable to: shareholders of the company",
        "profit attributable to: total",
        "profit attributable to equity shareholders",
        "profit attributable to owners of the parent",
        "profit after tax attributable to owners of parent",
        "profit after tax attributable to owners of the parent",
        # IAS 1.81B, as printed under "Profit (loss) after tax"
        "profit (loss) after tax attributable to owners of parent",
        "profit (loss) attributable to owners of the parent",
        "net profit attributable to owners of the company",
        "profit attributable to equity holders of the company",
        "net profit/(loss) after minority interest",
        "net profit after minority interest"],
       statement=IS, category=C.EARNINGS,
       definition="The parent's share of profit for the period, after any "
                  "non-controlling interest.",
       distinct_from=("pat", "nci_share_of_profit"),
       notes="A statement prints this as a heading -- 'Profit for the period "
             "attributable to:' -- with 'Shareholders of the Company' and "
             "'Total' beneath it. Neither child names a measure alone."),
    _m("oci_attributable_to_owners",
       "Other Comprehensive Income Attributable to Owners", "earnings",
       ["other comprehensive income attributable to shareholders of the company",
        "other comprehensive income/(loss) attributable to shareholders of the company",
        "other comprehensive income attributable to owners of the company",
        "other comprehensive income attributable to: shareholders of the company",
        "other comprehensive income/(loss) attributable to: shareholders of the company",
        "other comprehensive income attributable to: total",
        "other comprehensive income/(loss) attributable to: total"],
       statement=IS, category=C.EARNINGS,
       definition="The parent's share of other comprehensive income.",
       distinct_from=("other_comprehensive_income",
                      "pat_attributable_to_owners")),
    _m("total_comprehensive_income", "Total Comprehensive Income", "earnings",
       ["total comprehensive income",
        "total comprehensive income for the period",
        "total comprehensive income net of tax",
        "total comprehensive income/(loss)"],
       statement=IS, category=C.EARNINGS,
       definition="Profit for the period plus other comprehensive income.",
       distinct_from=("other_comprehensive_income", "pat")),

    _m("oci_remeasurement_benefit_plans",
       "Remeasurement of Defined Benefit Plans", "earnings",
       ["remeasurement gains losses on defined employee benefit plans",
        "remeasurement gains/(losses) on defined employee benefit plans",
        "remeasurement of defined benefit plans",
        "remeasurement of the net defined benefit liability"],
       statement=IS, category=C.EARNINGS,
       definition="An other-comprehensive-income component: actuarial "
                  "remeasurement of employee benefit obligations."),
    _m("oci_fair_value_investments",
       "Remeasurement of Investments at Fair Value", "earnings",
       ["remeasurement gains on investment carried at fair value",
        "remeasurement gains losses on investment carried at fair value",
        "fair value gains on investments",
        "net gain loss on fair value of investments"],
       statement=IS, category=C.EARNINGS,
       definition="An other-comprehensive-income component: fair-value "
                  "movement on investments not routed through profit."),
    _m("foreign_currency_translation_reserve",
       "Foreign Currency Translation Reserve", "earnings",
       ["foreign currency translation reserve",
        "exchange differences on translation of foreign operations",
        "foreign currency translation differences"],
       statement=IS, category=C.EARNINGS,
       definition="An other-comprehensive-income component: translation "
                  "differences on foreign operations."),

    _m("depreciation", "Depreciation", "costs",
       ["depreciation", "depreciation and amortisation", "depreciation amortisation",
        "d&a", "depreciation, amortisation and depletion",
        "depreciation and amortisation expense",
        "depreciation and amortization expense",
        "depreciation and amortisation expenses",
        "depreciation, amortisation and impairment",
        "depreciation, amortisation and impairment expense",
        "depreciation and amortization"],
       statement=IS, category=C.COSTS, sign="positive"),
    _m("finance_costs", "Finance Costs", "costs",
       ["finance costs", "finance cost", "interest costs", "interest expense",
        "finance charges", "finance expenses", "finance expense"],
       statement=IS, category=C.COSTS, sign="positive"),
    _m("tax_expense", "Tax Expense", "costs",
       ["tax expenses", "tax expense", "total tax expense", "income tax expense",
        "current and deferred tax", "provision for taxes", "provision for tax",
        "provision for taxes (tax expenses/(reversal))", "income taxes",
        "tax expense/(credit)", "total tax expenses"],
       statement=IS, category=C.COSTS),
    _m("current_tax", "Current Tax", "costs",
       ["current tax", "current tax charge", "tax expenses current tax",
        "tax expense current tax"],
       statement=IS, category=C.COSTS),
    _m("deferred_tax", "Deferred Tax", "costs",
       ["deferred tax", "deferred tax (charge)/credit", "deferred tax charge",
        "deferred tax credit", "tax expenses deferred tax",
        "tax expense deferred tax"],
       statement=IS, category=C.COSTS),
    _m("total_expenses", "Total Expenses", "costs",
       ["total expenses", "total expense"],
       statement=IS, category=C.COSTS, sign="positive",
       distinct_from=("total_expenditure",)),
    _m("total_expenditure", "Total Expenditure", "costs",
       ["total expenditure",
        "total expenditure excluding provisions and contingencies"],
       statement=IS, category=C.COSTS, sign="positive",
       definition="A bank's operating cost base: interest expended plus "
                  "operating expenses, before provisions and contingencies.",
       distinct_from=("total_expenses", "capex"),
       notes="Kept apart from capital expenditure, which fuzzy matching "
             "scored at 0.83 against it -- reporting a bank's entire cost "
             "base as capex."),
    _m("operating_expenses", "Operating Expenses", "costs",
       ["operating expenses", "operating expense", "opex",
        "total operating expenses"],
       statement=IS, category=C.COSTS, sign="positive",
       definition="Staff and other operating costs, excluding interest and "
                  "provisions.",
       distinct_from=("other_operating_expenses", "other_expenses")),
    _m("other_operating_expenses", "Other Operating Expenses", "costs",
       ["other operating expenses", "other operating expense"],
       statement=IS, category=C.COSTS, sign="positive",
       distinct_from=("operating_expenses", "other_expenses")),
    _m("other_expenses", "Other Expenses", "costs",
       ["other expenses", "other expense"],
       statement=IS, category=C.COSTS, sign="positive"),
    _m("employee_cost", "Employee Cost", "costs",
       ["employee benefit expense", "employee costs", "staff costs",
        "employee benefits expense", "employee benefits expenses",
        "employee benefit expenses", "employees cost", "employee cost",
        "payments to and provisions for employees", "staff expenses"],
       statement=IS, category=C.COSTS, sign="positive"),
    _m("raw_material_cost", "Raw Material Cost", "costs",
       ["cost of materials consumed", "raw material cost", "cost of goods sold",
        "cogs"],
       statement=IS, category=C.COSTS, sign="positive"),

    _m("eps", "EPS", "per_share",
       ["eps", "earnings per share", "basic eps", "earning per share",
        "basic earnings per share", "earnings per share basic",
        "earning per share basic", "earnings per equity share basic",
        "earnings per share - basic", "basic earnings per share after exceptional items",
        "basic and diluted eps before extraordinary items",
        "basic and diluted eps after extraordinary items",
        "basic eps before and after extraordinary items",
        "earnings per equity share attributable to equity holders of the company basic",
        "earnings per equity share attributable to equity holders of the company: basic"],
       statement=IS, category=C.PER_SHARE, value_type=PS),
    _m("diluted_eps", "Diluted EPS", "per_share",
       ["diluted eps", "diluted earnings per share", "earnings per share diluted",
        "earning per share diluted", "earnings per equity share diluted",
        "earnings per share - diluted",
        "diluted earnings per share after exceptional items",
        "diluted eps before and after extraordinary items",
        "earnings per equity share attributable to equity holders of the company diluted",
        "earnings per equity share attributable to equity holders of the company: diluted"],
       statement=IS, category=C.PER_SHARE, value_type=PS,
       distinct_from=("eps",)),
    _m("book_value_per_share", "Book Value per Share", "per_share",
       ["book value per share", "bvps", "net asset value per share",
        "navps"],
       statement=BS, category=C.PER_SHARE, value_type=PS, is_flow=False,
       definition="Shareholders' equity divided by the number of shares "
                  "outstanding.",
       distinct_from=("eps",)),
    _m("shares_outstanding", "Shares Outstanding", "per_share",
       ["total no. of shares", "total number of shares", "shares outstanding",
        "number of shares outstanding", "total shares outstanding",
        "no. of equity shares"],
       statement=BS, category=C.PER_SHARE, value_type=CNT, is_flow=False,
       definition="Count of equity shares in issue.", sign="positive"),

    # =========================================================== BALANCE SHEET
    _m("cash", "Cash", "liquidity", ["cash"],
       statement=BS, category=C.LIQUIDITY, is_flow=False, sign="positive"),
    _m("cash_and_equivalents", "Cash & Cash Equivalents", "liquidity",
       ["cash and cash equivalents", "cash and bank balances",
        "cash and cash equivalent"],
       statement=BS, category=C.LIQUIDITY, is_flow=False, sign="positive"),
    _m("debt", "Debt", "leverage",
       ["debt", "borrowings", "total borrowings", "loans and borrowings",
        "loans, borrowings and bank overdrafts"],
       statement=BS, category=C.LEVERAGE, is_flow=False, sign="positive"),
    _m("outstanding_debt", "Outstanding Debt", "leverage",
       ["outstanding debt", "gross debt", "total debt"],
       statement=BS, category=C.LEVERAGE, is_flow=False, sign="positive",
       definition="Gross borrowings before netting cash.",
       distinct_from=("net_debt",)),
    _m("net_debt", "Net Debt", "leverage", ["net debt"],
       statement=BS, category=C.LEVERAGE, is_flow=False,
       definition="Gross debt less cash and cash equivalents.",
       distinct_from=("outstanding_debt", "debt")),
    _m("receivables", "Receivables", "balance_sheet",
       ["trade receivables", "receivables", "accounts receivable", "debtors",
        "trade and other receivables"],
       statement=BS, category=C.BALANCE_SHEET, is_flow=False, sign="positive"),
    _m("inventory", "Inventory", "balance_sheet",
       ["inventories", "inventory", "stock in trade"],
       statement=BS, category=C.BALANCE_SHEET, is_flow=False, sign="positive"),
    _m("payables", "Payables", "balance_sheet",
       ["trade payables", "payables", "accounts payable", "creditors",
        "trade and other payables"],
       statement=BS, category=C.BALANCE_SHEET, is_flow=False, sign="positive"),
    _m("current_assets", "Current Assets", "balance_sheet",
       ["total current assets", "current assets"],
       statement=BS, category=C.BALANCE_SHEET, is_flow=False, sign="positive"),
    _m("current_liabilities", "Current Liabilities", "balance_sheet",
       ["total current liabilities", "current liabilities"],
       statement=BS, category=C.BALANCE_SHEET, is_flow=False, sign="positive"),
    _m("working_capital", "Working Capital", "liquidity",
       ["working capital", "net working capital"],
       statement=BS, category=C.LIQUIDITY, is_flow=False,
       definition="Current assets less current liabilities."),
    _m("net_worth", "Net Worth", "leverage",
       ["net worth", "shareholders funds", "total equity", "equity"],
       statement=BS, category=C.BALANCE_SHEET, is_flow=False, sign="positive",
       distinct_from=("share_capital", "other_equity")),
    _m("share_capital", "Paid-up Equity Share Capital", "balance_sheet",
       ["paid up equity share capital", "equity share capital",
        "paid-up equity share capital", "share capital",
        "issued subscribed and paid up share capital", "issued capital",
        "paid up equity capital", "paid-up share capital",
        "paid up share capital"],
       stock_aliases=["capital"],
       statement=BS, category=C.BALANCE_SHEET, is_flow=False, sign="positive",
       definition="The nominal value of issued equity, which a results "
                  "statement prints beside its per-share figures.",
       distinct_from=("net_worth", "other_equity"),
       notes="Carries a face-value parenthetical in every filing that prints "
             "it -- '(Face Value : INR 2/- per share)' -- which describes the "
             "share, not the unit of this figure."),
    _m("other_equity", "Other Equity", "balance_sheet",
       ["other equity", "reserves and surplus", "reserves excluding "
        "revaluation reserve", "reserves excluding revaluation reserves",
        "group reserves excluding minority interest and revaluation reserves"],
       statement=BS, category=C.BALANCE_SHEET, is_flow=False,
       definition="Retained earnings and reserves, excluding share capital.",
       distinct_from=("net_worth", "share_capital")),
    _m("total_assets", "Total Assets", "balance_sheet", ["total assets"],
       statement=BS, category=C.BALANCE_SHEET, is_flow=False, sign="positive"),
    _m("total_liabilities", "Total Liabilities", "balance_sheet",
       ["total liabilities"],
       statement=BS, category=C.BALANCE_SHEET, is_flow=False, sign="positive"),

    # =============================================================== CASH FLOW
    _m("operating_cash_flow", "Operating Cash Flow", "cashflow",
       ["operating cash flow", "cash flow from operations", "cfo",
        "net cash from operating activities",
        "net cash generated from operating activities",
        "net cash generated from/(used in) operating activities",
        "net cash (used in)/generated from operating activities",
        "net cash flows from operating activities",
        "net cash flows from/(used in) operating activities",
        "net cash from/(used in) operating activities",
        "net cash used in operating activities",
        "net cash provided by operating activities"],
       statement=CF, category=C.CASHFLOW),
    _m("investing_cash_flow", "Investing Cash Flow", "cashflow",
       ["net cash used in investing activities", "cash flow from investing",
        "net cash from investing activities",
        "net cash generated from/(used in) investing activities",
        "net cash (used in)/generated from investing activities",
        "net cash flows from investing activities",
        "net cash flows used in investing activities",
        "net cash from/(used in) investing activities"],
       statement=CF, category=C.CASHFLOW),
    _m("financing_cash_flow", "Financing Cash Flow", "cashflow",
       ["net cash from financing activities", "cash flow from financing",
        "net cash used in financing activities",
        "net cash generated from/(used in) financing activities",
        "net cash (used in)/generated from financing activities",
        "net cash flows from financing activities",
        "net cash flows used in financing activities",
        "net cash from/(used in) financing activities"],
       statement=CF, category=C.CASHFLOW),
    _m("free_cash_flow", "Free Cash Flow", "cashflow", ["free cash flow", "fcf"],
       statement=CF, category=C.CASHFLOW,
       definition="Operating cash flow less capital expenditure."),
    _m("capex", "Capital Expenditure", "investment",
       ["capital expenditure", "capex", "capital expenditure incurred",
        "capital investment", "purchase of property, plant and equipment",
        "purchases of property, plant and equipment",
        "purchase of property, plant and equipment and other intangible assets",
        "purchase of intangible assets and property, plant, and equipment",
        "purchase of intangible assets and property, plant and equipment",
        "acquisition of property, plant and equipment"],
       statement=CF, category=C.INVESTMENT, sign="positive"),

    # ============================================================== MARGINS
    _m("ebitda_margin", "EBITDA Margin", "margins",
       ["ebitda margin", "ebitda margins", "ebitda %", "ebitda margin %"],
       statement=RATIO_ST, category=C.MARGINS, value_type=PCT,
       definition="EBITDA as a percentage of revenue."),
    _m("ebit_margin", "EBIT Margin", "margins",
       ["ebit margin", "operating margin", "operating profit margin"],
       statement=RATIO_ST, category=C.MARGINS, value_type=PCT),
    _m("gross_margin", "Gross Margin", "margins",
       ["gross margin", "gross profit margin"],
       statement=RATIO_ST, category=C.MARGINS, value_type=PCT),
    _m("net_margin", "Net Margin", "margins",
       ["net margin", "net profit margin", "pat margin"],
       statement=RATIO_ST, category=C.MARGINS, value_type=PCT,
       definition="Profit after tax as a percentage of revenue."),
    _m("effective_tax_rate", "Effective Tax Rate", "margins",
       ["effective tax rate", "etr", "tax rate"],
       statement=RATIO_ST, category=C.MARGINS, value_type=PCT,
       definition="Tax charge as a percentage of profit before tax."),
    _m("segment_margin", "Segment Margin", "margins",
       ["segment margin", "segment ebitda margin"],
       statement=SEG, category=C.SEGMENT, value_type=PCT),

    # =================================================== LEVERAGE / LIQUIDITY
    # Not "leverage ratio": a bank's is Tier 1 capital over exposure (Basel
    # III, printed in %), an NBFC's is assets or debt over equity (5-7x); an
    # NBFC deck's and a UK bank's were both reported as net debt to EBITDA.
    _m("net_debt_to_ebitda", "Net Debt to EBITDA", "leverage",
       ["net debt to ebitda", "net debt ebitda", "net debt / ebitda"],
       statement=RATIO_ST, category=C.LEVERAGE, value_type=RAT, is_flow=False),
    _m("debt_to_equity", "Debt to Equity", "leverage",
       ["debt to equity", "debt equity ratio", "gearing ratio",
        "debt / equity"],
       statement=RATIO_ST, category=C.LEVERAGE, value_type=RAT, is_flow=False),
    _m("current_ratio", "Current Ratio", "liquidity", ["current ratio"],
       statement=RATIO_ST, category=C.LIQUIDITY, value_type=RAT, is_flow=False),
    _m("interest_coverage", "Interest Coverage", "leverage",
       ["interest coverage", "interest coverage ratio",
        "interest service coverage ratio"],
       statement=RATIO_ST, category=C.LEVERAGE, value_type=RAT, is_flow=False),

    # ================================================================ RETURNS
    _m("roe", "ROE", "returns", ["roe", "return on equity"],
       statement=RATIO_ST, category=C.RETURNS, value_type=PCT),
    _m("roce", "ROCE", "returns", ["roce", "return on capital employed"],
       statement=RATIO_ST, category=C.RETURNS, value_type=PCT),
    _m("roa", "ROA", "returns", ["roa", "return on assets"],
       statement=RATIO_ST, category=C.RETURNS, value_type=PCT),

    # =============================================================== BANKING
    # A bank's income statement and balance sheet are not the corporate ones
    # above: there is no revenue line, no EBITDA and no inventory, and the
    # figures a reader looks for first -- net interest margin, asset quality,
    # the deposit mix -- have no equivalent among them.
    #
    # These are the standard vocabulary of bank reporting, not one filer's
    # words. Without them a bank's results release parses as a document with
    # almost no recognisable metrics, which is what it did.
    _m("net_interest_income", "Net Interest Income", "revenue",
       ["net interest income", "nii"],
       statement=IS, category=C.REVENUE,
       definition="Interest earned less interest expended."),
    _m("net_interest_margin", "Net Interest Margin", "margins",
       ["net interest margin", "nim"],
       statement=RATIO_ST, category=C.MARGINS, value_type=PCT,
       definition="Net interest income as a percentage of earning assets."),
    _m("cost_of_funds", "Cost of Funds", "margins",
       ["cost of funds", "cost of deposits"],
       statement=RATIO_ST, category=C.MARGINS, value_type=PCT),
    _m("gross_npa_ratio", "Gross NPA Ratio", "asset_quality",
       ["gnpa ratio", "gross npa ratio", "gross npa", "gnpa", "% of gross npas",
        "% of gross npa"],
       statement=RATIO_ST, category=C.LEVERAGE, value_type=PCT,
       twin="gross_npa_amount",
       definition="Gross non-performing advances as a percentage of gross "
                  "advances."),
    _m("net_npa_ratio", "Net NPA Ratio", "asset_quality",
       ["nnpa ratio", "net npa ratio", "net npa", "nnpa", "% of net npas",
        "% of net npa"],
       statement=RATIO_ST, category=C.LEVERAGE, value_type=PCT,
       twin="net_npa_amount"),
    _m("provision_coverage_ratio", "Provision Coverage Ratio", "asset_quality",
       ["provision coverage ratio", "pcr"],
       statement=RATIO_ST, category=C.LEVERAGE, value_type=PCT),
    _m("casa_ratio", "CASA Ratio", "funding",
       ["casa ratio", "casa %"],
       statement=RATIO_ST, category=C.LEVERAGE, value_type=PCT,
       definition="Current and savings account deposits as a percentage of "
                  "total deposits."),
    _m("casa_deposits", "CASA Deposits", "funding",
       ["casa deposits", "casa"],
       statement=BS, category=C.BALANCE_SHEET, is_flow=False, sign="positive"),
    _m("total_deposits", "Total Deposits", "funding",
       ["total deposits", "deposits", "customer deposits",
        "total customer deposits"],
       statement=BS, category=C.BALANCE_SHEET, is_flow=False, sign="positive"),
    _m("interest_earned", "Interest Earned", "revenue",
       ["interest earned", "interest income", "interest earned a b c d",
        "total interest income", "interest and discount income"],
       statement=IS, category=C.REVENUE, sign="positive",
       definition="A bank's gross interest income: the sum of interest on "
                  "advances, income on investments, interest on balances with "
                  "the central bank, and other interest.",
       distinct_from=("interest_on_advances", "investment_income",
                      "interest_on_rbi_balances"),
       notes="A bank's results table lists the total and its four components "
             "as consecutive rows, so they must stay separate keys."),
    _m("interest_on_advances", "Interest / Discount on Advances and Bills",
       "revenue",
       ["interest discount on advances bills",
        "interest/discount on advances/bills",
        "interest and discount on advances and bills",
        "interest on advances"],
       statement=IS, category=C.REVENUE, sign="positive",
       definition="The largest component of a bank's interest income.",
       distinct_from=("interest_earned", "investment_income")),
    _m("interest_on_rbi_balances",
       "Interest on Balances with the Central Bank", "revenue",
       ["interest on balances with reserve bank of india and other inter bank funds",
        "interest on balances with reserve bank of india",
        "interest on balances with central bank",
        "interest on inter bank funds"],
       statement=IS, category=C.REVENUE, sign="positive",
       definition="Interest earned on balances held with the central bank and "
                  "on inter-bank funds.",
       distinct_from=("interest_earned", "interest_on_advances")),

    _m("advances", "Advances", "funding",
       ["advances", "loans and advances", "gross advances", "net advances",
        "loan book", "total advances"],
       statement=BS, category=C.BALANCE_SHEET, is_flow=False, sign="positive"),
    _m("capital_adequacy_ratio", "Capital Adequacy Ratio", "capital",
       ["capital adequacy ratio", "car", "crar", "total capital ratio"],
       statement=RATIO_ST, category=C.LEVERAGE, value_type=PCT),
    _m("tier_1_ratio", "Tier 1 Capital Ratio", "capital",
       ["tier 1 ratio", "tier i ratio", "tier 1 capital ratio",
        "tier i capital ratio"],
       statement=RATIO_ST, category=C.LEVERAGE, value_type=PCT,
       distinct_from=("cet1_ratio",),
       notes="CET1 was an alias until Phase 21. Tier 1 is CET1 plus "
             "Additional Tier 1; a bank prints all three."),
    _m("cet1_ratio", "CET1 Ratio", "capital",
       ["cet1 ratio", "cet 1 ratio", "common equity tier 1",
        "common equity tier 1 ratio", "common equity tier (cet) 1 ratio",
        "cet i ratio", "common equity tier i ratio"],
       statement=RATIO_ST, category=C.LEVERAGE, value_type=PCT,
       distinct_from=("tier_1_ratio",)),
    _m("at1_ratio", "Additional Tier 1 Ratio", "capital",
       ["additional tier 1 ratio", "additional tier i ratio", "at1 ratio",
        "at 1 ratio"],
       statement=RATIO_ST, category=C.LEVERAGE, value_type=PCT),
    _m("tier_2_ratio", "Tier 2 Ratio", "capital",
       ["tier 2 ratio", "tier ii ratio", "tier 2 capital ratio"],
       statement=RATIO_ST, category=C.LEVERAGE, value_type=PCT),
    _m("credit_cost", "Credit Cost", "asset_quality",
       ["credit cost", "cost of credit", "credit costs"],
       statement=IS, category=C.COSTS, value_type=PCT),
    _m("credit_deposit_ratio", "Credit to Deposit Ratio", "funding",
       ["credit deposit ratio", "credit to deposit ratio", "cd ratio"],
       statement=RATIO_ST, category=C.LEVERAGE, value_type=PCT),
    # A bank's operating profit before credit provisions. Distinct from EBIT:
    # for a lender, provisions are an operating cost of the core business, so
    # the two measure different things and must not merge.
    _m("pre_provision_operating_profit",
       "Pre-Provision Operating Profit", "profitability",
       ["pre provisioning operating profit", "pre-provisioning operating "
        "profit", "pre provision operating profit", "ppop",
        "core operating profit", "operating profit before provisions",
        "operating profit before provisions and contingencies",
        "operating profit before provision and contingencies",
        "operating profit (profit before provisions and contingencies)",
        "operating profit/(loss) before provisions and contingencies"],
       statement=IS, category=C.EARNINGS,
       definition="Operating profit before credit provisions and "
                  "contingencies.",
       distinct_from=("ebit", "ebitda", "operating_profit")),
    _m("risk_adjusted_nii", "Risk Adjusted Net Interest Income", "margins",
       ["risk adjusted nii", "risk adjusted net interest income",
        "risk adjusted nim"],
       statement=RATIO_ST, category=C.MARGINS, value_type=PCT,
       definition="Net interest income less credit provisions, as a "
                  "percentage of average total assets.",
       distinct_from=("net_interest_margin", "net_interest_income")),

    # ================================================= STANDARD FORMAT LINES
    # The lines a statement prescribed by its framework prints: Ind AS
    # Schedule III (statement of profit and loss, balance sheet), the SEBI
    # results format for banks and the bank balance sheet (Banking Regulation
    # Act, Third Schedule), and IAS 1 / IAS 7 statements. They are the
    # framework's words, used by every filer under it -- not any one
    # company's. Added in Phase 21, when a fifteen-company corpus showed most
    # of these rows producing no fact at all.
    #
    # ---- statement of profit and loss (Schedule III, IAS 1) ---------------
    _m("sale_of_products", "Sale of Products", "revenue",
       ["sale of products", "sales of products"],
       statement=IS, category=C.REVENUE, sign="positive",
       distinct_from=("revenue_from_operations",)),
    _m("sale_of_services", "Sale of Services", "revenue",
       ["sale of services", "sales of services"],
       statement=IS, category=C.REVENUE, sign="positive",
       distinct_from=("revenue_from_operations",)),
    _m("other_operating_revenue", "Other Operating Revenue", "revenue",
       ["other operating revenue", "other operating revenues"],
       statement=IS, category=C.REVENUE),
    _m("purchases_of_stock_in_trade", "Purchases of Stock-in-Trade", "costs",
       ["purchases of stock-in-trade", "purchase of stock-in-trade",
        "purchases of stock in trade", "purchases of traded goods"],
       statement=IS, category=C.COSTS),
    _m("changes_in_inventories", "Changes in Inventories", "costs",
       ["changes in inventories of finished goods, work-in-progress and stock-in-trade",
        "changes in inventories of finished goods, stock-in-trade and work-in-progress",
        "changes in inventories of finished and semi-finished goods, stock-in-trade and work-in-progress",
        "changes in inventories of finished goods and work-in-progress",
        "changes in inventories"],
       statement=IS, category=C.COSTS),
    _m("cost_of_revenue", "Cost of Revenue", "costs",
       ["cost of revenue", "cost of revenues", "total cost of revenue",
        "total cost of revenues"],
       statement=IS, category=C.COSTS, sign="positive"),
    _m("gross_profit", "Gross Profit", "earnings", ["gross profit"],
       statement=IS, category=C.EARNINGS,
       definition="Revenue less cost of revenue, as an amount.",
       distinct_from=("gross_margin",)),
    _m("research_and_development", "Research and Development", "costs",
       ["research and development", "research and development expenses",
        "research and development expense"],
       statement=IS, category=C.COSTS, sign="positive"),
    _m("sales_and_marketing", "Sales and Marketing", "costs",
       ["sales and marketing", "sales and marketing expenses",
        "selling and marketing expenses", "selling and marketing",
        "selling and distribution expenses"],
       statement=IS, category=C.COSTS, sign="positive"),
    _m("general_and_administrative", "General and Administrative", "costs",
       ["general and administration", "general and administrative",
        "general and administrative expenses",
        "general and administration expenses"],
       statement=IS, category=C.COSTS, sign="positive"),
    _m("restructuring_expense", "Restructuring", "costs",
       ["restructuring", "restructuring costs", "restructuring expenses"],
       statement=IS, category=C.COSTS),
    _m("other_operating_income_expense", "Other Operating Income/Expense, Net",
       "earnings",
       ["other operating income/expense, net",
        "other operating income/(expense), net",
        "other operating income/(loss), net"],
       statement=IS, category=C.EARNINGS),
    _m("finance_income", "Finance Income", "revenue",
       ["finance income", "financial income"],
       statement=IS, category=C.REVENUE, distinct_from=("financial_income_net",)),
    _m("financial_income_net", "Financial Income, Net", "revenue",
       ["financial income, net", "finance income, net", "net finance income",
        "net financial income"],
       statement=IS, category=C.REVENUE, distinct_from=("finance_income",)),
    _m("finance_and_other_income", "Finance and Other Income", "revenue",
       ["finance and other income"],
       statement=IS, category=C.REVENUE, distinct_from=("other_income",)),
    _m("fx_gains_losses", "Foreign Exchange Gains/(Losses), Net", "earnings",
       ["foreign exchange gains/(losses), net", "foreign exchange gain/(loss), net",
        "net foreign exchange gains/(losses)"],
       statement=IS, category=C.EARNINGS),
    _m("other_non_operating_income", "Other Non-Operating Income/Expense, Net",
       "earnings",
       ["other non-operating income/expense, net",
        "other non-operating income/(expense), net"],
       statement=IS, category=C.EARNINGS),
    _m("pat_continuing", "Profit from Continuing Operations", "earnings",
       ["profit for the period from continuing operations",
        "profit from continuing operations",
        "profit/(loss) from continuing operations",
        "profit/(loss) for the period from continuing operations"],
       statement=IS, category=C.EARNINGS, distinct_from=("pat",)),
    _m("pat_discontinued", "Profit from Discontinued Operations", "earnings",
       ["profit from discontinued operations",
        "profit/(loss) from discontinued operations",
        "profit/(loss) for the period from discontinued operations",
        "profit/(loss) from discontinued operations after tax",
        "profit/(loss) from discontinued operations (after tax)"],
       statement=IS, category=C.EARNINGS, distinct_from=("pat",)),
    _m("nci_share_of_profit", "Profit Attributable to Non-Controlling Interests",
       "earnings",
       ["profit attributable to non-controlling interests",
        "profit attributable to non-controlling interest",
        "profit attributable to: non-controlling interests",
        "net profit attributable to non-controlling interest",
        "net profit attributable to non-controlling interests",
        "profit after tax attributable to non-controlling interests",
        "profit (loss) after tax attributable to non-controlling interests",
        "profit (loss) attributable to non-controlling interests",
        "share of profit/(loss) of minority shareholders",
        "share of profit of minority shareholders"],
       flow_aliases=["non-controlling interests", "non-controlling interest",
                     "minority interest", "minority interests"],
       statement=IS, category=C.EARNINGS,
       distinct_from=("pat_attributable_to_owners", "non_controlling_interests")),
    _m("oci_attributable_to_nci",
       "Other Comprehensive Income Attributable to Non-Controlling Interests",
       "earnings",
       ["other comprehensive income attributable to non-controlling interests",
        "other comprehensive income attributable to non-controlling interest",
        "oci attributable to non-controlling interests"],
       statement=IS, category=C.EARNINGS),
    _m("tci_attributable_to_owners",
       "Total Comprehensive Income Attributable to Owners", "earnings",
       ["total comprehensive income attributable to owners of the parent",
        "total comprehensive income attributable to owners of the company",
        "total comprehensive income attributable to equity holders of the company",
        "total comprehensive income attributable to shareholders of the company"],
       statement=IS, category=C.EARNINGS,
       distinct_from=("total_comprehensive_income",)),
    _m("tci_attributable_to_nci",
       "Total Comprehensive Income Attributable to Non-Controlling Interests",
       "earnings",
       ["total comprehensive income attributable to non-controlling interests",
        "total comprehensive income attributable to non-controlling interest"],
       statement=IS, category=C.EARNINGS),
    _m("oci_items_not_reclassified",
       "OCI: Items That Will Not Be Reclassified to Profit or Loss", "earnings",
       ["items that will not be reclassified to profit or loss",
        "items that will not be reclassified to profit and loss"],
       statement=IS, category=C.EARNINGS,
       distinct_from=("oci_items_reclassified",)),
    _m("oci_tax_not_reclassified",
       "OCI: Income Tax on Items That Will Not Be Reclassified", "earnings",
       ["income tax relating to items that will not be reclassified to profit or loss",
        "income tax relating to items that will not be reclassified to profit and loss",
        "income tax on items that will not be reclassified to profit or loss"],
       statement=IS, category=C.EARNINGS,
       distinct_from=("oci_tax_reclassified", "tax_expense")),
    _m("oci_items_reclassified",
       "OCI: Items That Will Be Reclassified to Profit or Loss", "earnings",
       ["items that will be reclassified to profit or loss",
        "items that will be reclassified to profit and loss"],
       statement=IS, category=C.EARNINGS,
       distinct_from=("oci_items_not_reclassified",)),
    _m("oci_tax_reclassified",
       "OCI: Income Tax on Items That Will Be Reclassified", "earnings",
       ["income tax relating to items that will be reclassified to profit or loss",
        "income tax relating to items that will be reclassified to profit and loss",
        "income tax on items that will be reclassified to profit or loss"],
       statement=IS, category=C.EARNINGS,
       distinct_from=("oci_tax_not_reclassified", "tax_expense")),
    _m("weighted_average_shares_basic", "Weighted Average Shares (Basic)",
       "per_share",
       ["weighted average number of equity shares used in computing earnings per equity share basic",
        "weighted average number of equity shares used in computing earnings per equity share: basic",
        "weighted average number of equity shares basic",
        "weighted average number of shares basic"],
       statement=IS, category=C.PER_SHARE, value_type=CNT,
       distinct_from=("weighted_average_shares_diluted", "shares_outstanding")),
    _m("weighted_average_shares_diluted", "Weighted Average Shares (Diluted)",
       "per_share",
       ["weighted average number of equity shares used in computing earnings per equity share diluted",
        "weighted average number of equity shares used in computing earnings per equity share: diluted",
        "weighted average number of equity shares diluted",
        "weighted average number of shares diluted"],
       statement=IS, category=C.PER_SHARE, value_type=CNT,
       distinct_from=("weighted_average_shares_basic", "shares_outstanding")),

    # ---- the SEBI results format for banks -------------------------------
    _m("interest_expended", "Interest Expended", "costs",
       ["interest expended"],
       statement=IS, category=C.COSTS, sign="positive",
       definition="A bank's interest cost on deposits and borrowings.",
       distinct_from=("finance_costs",)),
    _m("other_interest_income", "Interest Earned: Others", "revenue",
       ["interest earned others", "other interest income"],
       statement=IS, category=C.REVENUE,
       definition="The fourth component of a bank's interest earned, after "
                  "advances, investments and central-bank balances.",
       distinct_from=("interest_earned",)),
    _m("insurance_premium_income", "Premium Income from Insurance Business",
       "revenue",
       ["premium on insurance business",
        "premium and other operating income from insurance business",
        "premium income from insurance business"],
       statement=IS, category=C.REVENUE),
    _m("insurance_claims_expense", "Insurance Claims and Policyholder Reserves",
       "costs",
       ["claims and benefits paid and other expenses pertaining to insurance business",
        "policy holders' reserves, surrender expense and claims",
        "policy holders' reserves, surrender expense and claims (insurance business)",
        "policyholders' reserves, surrender expense and claims"],
       statement=IS, category=C.COSTS),
    _m("insurance_operating_expenses", "Operating Expenses of Insurance Business",
       "costs",
       ["operating expenses relating to insurance business",
        "operating expenses of insurance business"],
       statement=IS, category=C.COSTS,
       distinct_from=("operating_expenses",)),
    _m("provisions_and_contingencies", "Provisions (Other than Tax) and Contingencies",
       "costs",
       ["provisions (other than tax) and contingencies",
        "provisions and contingencies",
        "provisions (other than tax) and contingencies (net)"],
       flow_aliases=["total provisions"],
       statement=IS, category=C.COSTS,
       definition="A bank's charge for credit and other provisions.",
       distinct_from=("tax_expense", "provisions")),
    _m("provisions_for_npa", "Provisions for Non-Performing Assets", "costs",
       ["provisions for non-performing assets", "provision for non-performing assets",
        "provisions for npas", "provision for npas", "provisions for npa"],
       statement=IS, category=C.COSTS,
       distinct_from=("provisions_and_contingencies",)),
    _m("extraordinary_items", "Extraordinary Items", "earnings",
       ["extraordinary items", "extraordinary item"],
       statement=IS, category=C.EARNINGS),
    _m("pat_ordinary_activities", "Net Profit from Ordinary Activities after Tax",
       "earnings",
       ["net profit from ordinary activities after tax",
        "net profit/(loss) from ordinary activities after tax",
        "profit from ordinary activities after tax",
        "net profit from ordinary activities after tax before minority interest"],
       statement=IS, category=C.EARNINGS,
       definition="A bank's profit after tax before extraordinary items.",
       distinct_from=("pat",)),
    _m("govt_shareholding_pct", "Shares Held by Government of India", "capital",
       ["percentage of shares held by government of india",
        "% of shares held by government of india"],
       statement=RATIO_ST, category=C.OTHER, value_type=PCT, is_flow=False),
    _m("gross_npa_amount", "Gross NPA (Amount)", "asset_quality",
       ["amount of gross npas", "gross npas", "gross npa amount",
        "gross non-performing assets", "amount of gross non-performing assets"],
       statement=BS, category=C.LEVERAGE, is_flow=False,
       distinct_from=("gross_npa_ratio",), twin="gross_npa_ratio"),
    _m("net_npa_amount", "Net NPA (Amount)", "asset_quality",
       ["amount of net npas", "net npas", "net npa amount",
        "net non-performing assets", "amount of net non-performing assets"],
       statement=BS, category=C.LEVERAGE, is_flow=False,
       distinct_from=("net_npa_ratio",), twin="net_npa_ratio"),
    _m("debt_to_assets", "Total Debts to Total Assets", "leverage",
       ["total debts to total assets", "total debt to total assets"],
       statement=RATIO_ST, category=C.LEVERAGE, value_type=RAT, is_flow=False),
    _m("cost_to_income_ratio", "Cost to Income Ratio", "margins",
       ["cost to income ratio", "cost income ratio", "cost to income"],
       statement=RATIO_ST, category=C.MARGINS, value_type=PCT),
    _m("cost_to_assets", "Cost to Assets", "margins",
       ["cost to assets", "cost to average assets"],
       statement=RATIO_ST, category=C.MARGINS, value_type=PCT),
    _m("non_interest_income", "Non-Interest Income", "revenue",
       ["non-interest income", "non interest income"],
       statement=IS, category=C.REVENUE,
       distinct_from=("other_income", "net_interest_income")),
    _m("net_total_income", "Net Total Income", "revenue",
       ["net total income", "nti", "net total income (nti)"],
       statement=IS, category=C.REVENUE),
    _m("impairment_on_financial_instruments",
       "Impairment on Financial Instruments", "costs",
       ["impairment on financial instruments", "impairment of financial instruments",
        "loan losses and provisions"],
       statement=IS, category=C.COSTS),
    _m("aum", "Assets Under Management", "funding",
       ["assets under management", "aum", "total aum"],
       statement=BS, category=C.BALANCE_SHEET, is_flow=False, sign="positive"),

    # ---- balance sheet (Schedule III, bank Form A, IAS 1) ----------------
    _m("ppe", "Property, Plant and Equipment", "balance_sheet",
       ["property, plant and equipment", "property, plant, and equipment",
        "property plant and equipments"],
       statement=BS, category=C.BALANCE_SHEET, is_flow=False, sign="positive"),
    _m("capital_work_in_progress", "Capital Work-in-Progress", "balance_sheet",
       ["capital work-in-progress", "capital work in progress"],
       statement=BS, category=C.BALANCE_SHEET, is_flow=False, sign="positive"),
    _m("right_of_use_assets", "Right-of-Use Assets", "balance_sheet",
       ["right-of-use assets", "right of use assets"],
       statement=BS, category=C.BALANCE_SHEET, is_flow=False, sign="positive"),
    _m("goodwill", "Goodwill", "balance_sheet",
       ["goodwill", "goodwill on consolidation"],
       statement=BS, category=C.BALANCE_SHEET, is_flow=False, sign="positive"),
    _m("intangible_assets", "Intangible Assets", "balance_sheet",
       ["intangible assets", "other intangible assets"],
       statement=BS, category=C.BALANCE_SHEET, is_flow=False, sign="positive"),
    _m("investments_equity_method", "Investments Accounted for Using the Equity Method",
       "balance_sheet",
       ["investments accounted for using the equity method",
        "equity accounted investees", "investment in associates and joint ventures"],
       statement=BS, category=C.BALANCE_SHEET, is_flow=False),
    _m("investments", "Investments", "balance_sheet",
       ["total investments"],
       stock_aliases=["investments"],
       statement=BS, category=C.BALANCE_SHEET, is_flow=False),
    _m("deferred_tax_assets", "Deferred Tax Assets", "balance_sheet",
       ["deferred tax assets", "deferred tax assets (net)", "deferred tax asset"],
       statement=BS, category=C.BALANCE_SHEET, is_flow=False,
       distinct_from=("deferred_tax",)),
    _m("deferred_tax_liabilities", "Deferred Tax Liabilities", "balance_sheet",
       ["deferred tax liabilities", "deferred tax liabilities (net)",
        "deferred tax liability"],
       statement=BS, category=C.BALANCE_SHEET, is_flow=False,
       distinct_from=("deferred_tax",)),
    _m("current_tax_assets", "Current Tax Assets", "balance_sheet",
       ["current tax assets", "current tax assets (net)"],
       statement=BS, category=C.BALANCE_SHEET, is_flow=False,
       distinct_from=("current_tax",)),
    _m("non_current_tax_assets", "Non-Current Tax Assets", "balance_sheet",
       ["non-current tax assets", "non-current tax assets (net)"],
       statement=BS, category=C.BALANCE_SHEET, is_flow=False),
    _m("current_tax_liabilities", "Current Tax Liabilities", "balance_sheet",
       ["current tax liabilities", "current tax liabilities (net)"],
       statement=BS, category=C.BALANCE_SHEET, is_flow=False,
       distinct_from=("current_tax",)),
    _m("non_current_tax_liabilities", "Non-Current Tax Liabilities", "balance_sheet",
       ["non-current tax liabilities", "non-current tax liabilities (net)"],
       statement=BS, category=C.BALANCE_SHEET, is_flow=False),
    _m("tax_assets", "Tax Assets", "balance_sheet", ["tax assets"],
       statement=BS, category=C.BALANCE_SHEET, is_flow=False),
    _m("tax_liabilities", "Tax Liabilities", "balance_sheet", ["tax liabilities"],
       statement=BS, category=C.BALANCE_SHEET, is_flow=False),
    _m("other_financial_assets", "Other Financial Assets", "balance_sheet",
       ["other financial assets"],
       statement=BS, category=C.BALANCE_SHEET, is_flow=False),
    _m("other_non_financial_assets", "Other Non-Financial Assets", "balance_sheet",
       ["other non-financial assets"],
       statement=BS, category=C.BALANCE_SHEET, is_flow=False),
    _m("contract_assets", "Contract Assets", "balance_sheet", ["contract assets"],
       statement=BS, category=C.BALANCE_SHEET, is_flow=False),
    _m("unbilled_receivables", "Unbilled Receivables", "balance_sheet",
       ["unbilled receivables", "unbilled revenue"],
       statement=BS, category=C.BALANCE_SHEET, is_flow=False,
       distinct_from=("receivables",)),
    _m("derivative_assets", "Derivative Assets", "balance_sheet",
       ["derivative assets", "derivative financial assets"],
       statement=BS, category=C.BALANCE_SHEET, is_flow=False),
    _m("other_bank_balances", "Bank Balances Other than Cash and Cash Equivalents",
       "liquidity",
       ["bank balances other than cash and cash equivalents", "other bank balances",
        "bank balances other than cash and cash equivalents above"],
       statement=BS, category=C.LIQUIDITY, is_flow=False),
    _m("other_current_assets", "Other Current Assets", "balance_sheet",
       ["other current assets"],
       statement=BS, category=C.BALANCE_SHEET, is_flow=False),
    _m("other_non_current_assets", "Other Non-Current Assets", "balance_sheet",
       ["other non-current assets"],
       statement=BS, category=C.BALANCE_SHEET, is_flow=False),
    _m("other_assets", "Other Assets", "balance_sheet", [],
       stock_aliases=["other assets"],
       statement=BS, category=C.BALANCE_SHEET, is_flow=False),
    _m("non_current_assets", "Non-Current Assets", "balance_sheet",
       ["total non-current assets", "non-current assets"],
       statement=BS, category=C.BALANCE_SHEET, is_flow=False, sign="positive",
       distinct_from=("current_assets",)),
    _m("cash_and_rbi_balances", "Cash and Balances with the Central Bank",
       "liquidity",
       ["cash and balances with reserve bank of india", "cash and balances with rbi",
        "cash and balances with central banks", "cash and balances with central bank"],
       statement=BS, category=C.LIQUIDITY, is_flow=False, sign="positive"),
    _m("balances_with_banks", "Balances with Banks and Money at Call",
       "liquidity",
       ["balances with banks and money at call and short notice"],
       statement=BS, category=C.LIQUIDITY, is_flow=False, sign="positive"),
    _m("fixed_assets", "Fixed Assets", "balance_sheet", ["fixed assets"],
       statement=BS, category=C.BALANCE_SHEET, is_flow=False, sign="positive"),
    _m("lease_liabilities", "Lease Liabilities", "leverage", ["lease liabilities"],
       statement=BS, category=C.LEVERAGE, is_flow=False),
    _m("other_financial_liabilities", "Other Financial Liabilities",
       "balance_sheet", ["other financial liabilities"],
       statement=BS, category=C.BALANCE_SHEET, is_flow=False),
    _m("other_non_financial_liabilities", "Other Non-Financial Liabilities",
       "balance_sheet", ["other non-financial liabilities"],
       statement=BS, category=C.BALANCE_SHEET, is_flow=False),
    _m("financial_liabilities", "Financial Liabilities", "balance_sheet",
       ["financial liabilities"],
       statement=BS, category=C.BALANCE_SHEET, is_flow=False),
    _m("contract_liabilities", "Contract Liabilities", "balance_sheet",
       ["contract liabilities"],
       statement=BS, category=C.BALANCE_SHEET, is_flow=False),
    _m("derivative_liabilities", "Derivative Liabilities", "balance_sheet",
       ["derivative liabilities", "derivative financial liabilities"],
       statement=BS, category=C.BALANCE_SHEET, is_flow=False),
    _m("accrued_expenses", "Accrued Expenses", "balance_sheet",
       ["accrued expenses"],
       statement=BS, category=C.BALANCE_SHEET, is_flow=False),
    # "Total provisions" is the balance sheet's line where the columns are
    # balances and a bank's charge for the period where they are periods; a
    # results deck's P&L slide was reported as the balance.
    _m("provisions", "Provisions", "balance_sheet", [],
       stock_aliases=["provisions", "total provisions"],
       statement=BS, category=C.BALANCE_SHEET, is_flow=False,
       distinct_from=("provisions_and_contingencies",)),
    _m("other_current_liabilities", "Other Current Liabilities", "balance_sheet",
       ["other current liabilities"],
       statement=BS, category=C.BALANCE_SHEET, is_flow=False),
    _m("other_non_current_liabilities", "Other Non-Current Liabilities",
       "balance_sheet", ["other non-current liabilities"],
       statement=BS, category=C.BALANCE_SHEET, is_flow=False),
    _m("other_liabilities_and_provisions", "Other Liabilities and Provisions",
       "balance_sheet", ["other liabilities and provisions"],
       statement=BS, category=C.BALANCE_SHEET, is_flow=False),
    _m("non_current_liabilities", "Non-Current Liabilities", "balance_sheet",
       ["total non-current liabilities", "non-current liabilities"],
       statement=BS, category=C.BALANCE_SHEET, is_flow=False, sign="positive",
       distinct_from=("current_liabilities",)),
    _m("total_equity_and_liabilities", "Total Equity and Liabilities",
       "balance_sheet",
       ["total equity and liabilities", "total capital and liabilities",
        "total liabilities and equity", "total liabilities and shareholders' equity"],
       statement=BS, category=C.BALANCE_SHEET, is_flow=False, sign="positive"),
    _m("retained_earnings", "Retained Earnings", "balance_sheet",
       ["retained earnings"],
       statement=BS, category=C.BALANCE_SHEET, is_flow=False),
    _m("share_premium", "Share Premium", "balance_sheet",
       ["share premium", "securities premium"],
       statement=BS, category=C.BALANCE_SHEET, is_flow=False),
    _m("other_components_of_equity", "Other Components of Equity",
       "balance_sheet", ["other components of equity"],
       statement=BS, category=C.BALANCE_SHEET, is_flow=False),
    _m("treasury_shares", "Treasury Shares", "balance_sheet", ["treasury shares"],
       statement=BS, category=C.BALANCE_SHEET, is_flow=False),
    _m("equity_attributable_to_owners", "Equity Attributable to Owners",
       "balance_sheet",
       ["equity attributable to owners of parent",
        "equity attributable to owners of the parent",
        "equity attributable to equity holders of the company",
        "equity attributable to shareholders of the company",
        "total equity attributable to equity holders of the company",
        # Ind AS and IFRS wordings of the same line (Phase 22)
        "equity attributable to owners of the company",
        "total equity attributable to owners of the parent",
        "total equity attributable to shareholders of the parent",
        "equity attributable to shareholders of the parent",
        "total equity attributable to the owners of the parent"],
       statement=BS, category=C.BALANCE_SHEET, is_flow=False,
       distinct_from=("net_worth",)),
    _m("non_controlling_interests", "Non-Controlling Interests", "balance_sheet",
       ["equity attributable to non-controlling interests"],
       stock_aliases=["non-controlling interests", "non-controlling interest",
                      "minority interest", "minority interests"],
       statement=BS, category=C.BALANCE_SHEET, is_flow=False,
       distinct_from=("nci_share_of_profit",)),
    _m("esop_outstanding", "Employee Stock Options Outstanding", "balance_sheet",
       ["employees stock options outstanding", "employee stock options outstanding",
        "employee stock options/units outstanding",
        "employees stock options/units outstanding"],
       statement=BS, category=C.BALANCE_SHEET, is_flow=False),
    _m("policyholders_funds", "Policyholders' Funds", "balance_sheet",
       ["policyholders' funds", "policy holders' funds"],
       statement=BS, category=C.BALANCE_SHEET, is_flow=False),
    _m("paid_up_debt_capital", "Paid-up Debt Capital", "leverage",
       ["paid-up debt capital", "paid up debt capital"],
       statement=BS, category=C.LEVERAGE, is_flow=False,
       distinct_from=("share_capital",)),

    # ---- statement of cash flows (IAS 7) ----------------------------------
    _m("net_change_in_cash", "Net Increase/(Decrease) in Cash and Cash Equivalents",
       "cashflow",
       ["net increase/(decrease) in cash and cash equivalents",
        "net increase/decrease in cash and cash equivalents",
        "net (decrease)/increase in cash and cash equivalents",
        "net increase in cash and cash equivalents",
        "net decrease in cash and cash equivalents"],
       statement=CF, category=C.CASHFLOW),
    _m("cash_at_beginning", "Cash and Cash Equivalents at the Beginning of the Period",
       "cashflow",
       ["cash and cash equivalents at the beginning of the period",
        "cash and cash equivalents as at the beginning of the period",
        "cash and cash equivalents at the beginning of the period/year",
        "cash and cash equivalents as at the beginning of the period/year",
        "cash and cash equivalents at the beginning of the year",
        "cash and cash equivalents at beginning of the period"],
       statement=CF, category=C.CASHFLOW, is_flow=False,
       distinct_from=("cash_at_end", "cash_and_equivalents")),
    _m("cash_at_end", "Cash and Cash Equivalents at the End of the Period",
       "cashflow",
       ["cash and cash equivalents at the end of the period",
        "cash and cash equivalents as at the end of the period",
        "cash and cash equivalents at the end of the period/year",
        "cash and cash equivalents as at the end of the period/year",
        "cash and cash equivalents at the end of the year",
        "cash and cash equivalents at end of the period"],
       statement=CF, category=C.CASHFLOW, is_flow=False,
       distinct_from=("cash_at_beginning", "cash_and_equivalents")),
    _m("fx_effect_on_cash", "Effect of Exchange Rates on Cash", "cashflow",
       ["effect of exchange rate changes on cash and cash equivalents",
        "effect of foreign currency rates on cash and cash equivalents",
        "effect of exchange differences on cash and cash equivalents",
        "net foreign exchange difference",
        "effect of exchange rate changes on cash and cash equivalents held in foreign currency"],
       statement=CF, category=C.CASHFLOW),
    _m("operating_profit_before_wc_changes",
       "Operating Profit before Working Capital Changes", "cashflow",
       ["operating profit before working capital changes",
        "operating profit/(loss) before working capital changes"],
       statement=CF, category=C.CASHFLOW),
    _m("cash_generated_from_operations", "Cash Generated from Operations",
       "cashflow",
       ["cash generated from operations", "cash generated from/(used in) operations",
        "cash (used in)/generated from operations"],
       statement=CF, category=C.CASHFLOW,
       distinct_from=("operating_cash_flow",)),
    _m("income_taxes_paid", "Income Taxes Paid", "cashflow",
       ["income taxes paid", "income taxes paid, net of refunds",
        "income taxes refund/(paid) (net)", "income taxes (paid)/refund (net)",
        "income taxes paid (net of refunds)", "direct taxes paid (net of refunds)",
        "income tax paid (net of refunds)", "taxes paid"],
       statement=CF, category=C.CASHFLOW, distinct_from=("tax_expense",)),
    _m("interest_received", "Interest Received", "cashflow", ["interest received"],
       statement=CF, category=C.CASHFLOW),
    _m("interest_paid", "Interest Paid", "cashflow",
       ["interest paid", "finance costs paid"],
       statement=CF, category=C.CASHFLOW, distinct_from=("finance_costs",)),
    _m("dividends_received", "Dividends Received", "cashflow",
       ["dividends received", "dividend received"],
       statement=CF, category=C.CASHFLOW),
    _m("dividends_paid", "Dividends Paid", "cashflow",
       ["dividends paid", "dividend paid", "dividends paid to equity shareholders"],
       statement=CF, category=C.CASHFLOW),
    _m("dividends_paid_nci", "Dividends Paid to Non-Controlling Interests",
       "cashflow",
       ["dividends paid on non-controlling interests",
        "dividends paid to non-controlling interests"],
       statement=CF, category=C.CASHFLOW),
    _m("proceeds_from_sale_of_ppe", "Proceeds from Sale of Property, Plant and Equipment",
       "cashflow",
       ["proceeds from sale of property, plant and equipment",
        "proceeds from sales of intangible assets and property, plant, and equipment",
        "proceeds from sales of intangible assets and property, plant and equipment",
        "proceeds from disposal of property, plant and equipment"],
       statement=CF, category=C.CASHFLOW),
    _m("purchase_of_investments", "Purchase of Investments", "cashflow",
       ["purchase of investments",
        "purchase of equity or debt instruments of other entities"],
       statement=CF, category=C.CASHFLOW),
    _m("proceeds_from_sale_of_investments", "Proceeds from Sale of Investments",
       "cashflow",
       ["proceeds from sale of investments",
        "proceeds from sales of equity or debt instruments of other entities"],
       statement=CF, category=C.CASHFLOW),
    _m("business_acquisitions", "Business Acquisitions, Net of Cash Acquired",
       "cashflow",
       ["business combinations, net of cash and cash equivalents acquired",
        "acquisition of subsidiaries, net of cash acquired",
        "payment for business acquisitions, net of cash acquired"],
       statement=CF, category=C.CASHFLOW),
    _m("proceeds_from_borrowings", "Proceeds from Borrowings", "cashflow",
       ["proceeds from borrowings", "proceeds from long-term borrowings",
        "proceeds from non-current borrowings"],
       statement=CF, category=C.CASHFLOW),
    _m("repayment_of_borrowings", "Repayment of Borrowings", "cashflow",
       ["repayment of borrowings", "repayments of borrowings",
        "repayment of long-term borrowings"],
       statement=CF, category=C.CASHFLOW),
    _m("lease_payments", "Payments of Lease Liabilities", "cashflow",
       ["payments of lease liabilities", "payment of lease liabilities",
        "repayment of lease liabilities"],
       statement=CF, category=C.CASHFLOW,
       distinct_from=("lease_principal_payments", "lease_interest_payments")),
    _m("lease_principal_payments", "Payment of Principal Portion of Lease Liabilities",
       "cashflow", ["payment of principal portion of lease liabilities"],
       statement=CF, category=C.CASHFLOW),
    _m("lease_interest_payments", "Payment of Interest Portion of Lease Liabilities",
       "cashflow", ["payment of interest portion of lease liabilities"],
       statement=CF, category=C.CASHFLOW),
    _m("purchase_of_treasury_shares", "Purchase of Treasury Shares", "cashflow",
       ["purchase of treasury shares"],
       statement=CF, category=C.CASHFLOW),
    _m("proceeds_from_issue_of_shares", "Proceeds from Issue of Shares", "cashflow",
       ["proceeds from issue of equity shares", "proceeds from issue of shares",
        "proceeds from issuance of equity shares"],
       statement=CF, category=C.CASHFLOW),
    _m("share_based_payment_expense", "Share-Based Payment Expense", "costs",
       ["share-based payment expense", "share-based payments",
        "share based compensation", "employee share-based payment expense"],
       statement=IS, category=C.COSTS),

    # ---- industry lines -----------------------------------------------------
    # Lines every filer in an industry prints in its statutory statement of
    # profit and loss; the industry's vocabulary, not one company's.
    _m("access_charges", "Access Charges", "costs", ["access charges"],
       statement=IS, category=C.COSTS, sign="positive",
       definition="Telecom interconnect and access costs."),
    _m("licence_fee_spectrum_charges", "Licence Fee / Spectrum Charges", "costs",
       ["license fee / spectrum charges", "licence fee / spectrum charges",
        "license fee and spectrum charges", "licence fee and spectrum charges"],
       statement=IS, category=C.COSTS, sign="positive"),
    _m("network_operating_expenses", "Network Operating Expenses", "costs",
       ["network operating expenses", "network operating expense"],
       statement=IS, category=C.COSTS, sign="positive",
       distinct_from=("operating_expenses",),
       notes="Fuzzy matching reported this as operating expenses until Phase "
             "21 -- a telecom's network costs as its whole operating cost base."),
    _m("advertising_and_promotion", "Advertising and Promotion", "costs",
       ["advertising and promotion", "advertisement and sales promotion",
        "advertising and sales promotion", "advertisement and publicity",
        "advertising and publicity"],
       statement=IS, category=C.COSTS, sign="positive"),

    # =========================================================== OPERATIONAL
    # Company-specific KPIs.  These are deliberately NOT treated as universal
    # financial metrics: they carry their own units, are never fed into margin
    # or leverage arithmetic, and are grouped separately in every view.
    _m("subscribers", "Subscribers", "operational",
       ["subscriber base", "total subscribers", "subscribers",
        "customer base", "total customer base"],
       statement=OPS, category=C.OPERATIONAL, value_type=CNT,
       is_operational=True, sign="positive"),
    _m("net_subscriber_addition", "Net Subscriber Addition", "operational",
       ["net subscriber addition", "net adds", "net additions"],
       statement=OPS, category=C.OPERATIONAL, value_type=CNT,
       is_operational=True),
    _m("arpu", "ARPU", "operational",
       ["arpu", "average revenue per user", "arpu per month"],
       statement=OPS, category=C.OPERATIONAL, value_type=MON,
       is_operational=True, sign="positive",
       definition="Average revenue per user, per period."),
    _m("data_traffic", "Data Traffic", "operational",
       ["data traffic", "total data traffic"],
       statement=OPS, category=C.OPERATIONAL, value_type=CNT,
       is_operational=True, sign="positive"),
    _m("voice_traffic", "Voice Traffic", "operational",
       ["voice traffic", "total voice traffic"],
       statement=OPS, category=C.OPERATIONAL, value_type=CNT,
       is_operational=True, sign="positive"),
    _m("store_count", "Store Count", "operational",
       ["number of stores", "store count", "total stores", "stores"],
       statement=OPS, category=C.OPERATIONAL, value_type=CNT,
       is_operational=True, sign="positive"),
    _m("area_operated", "Area Operated", "operational",
       ["area operated", "retail area", "warehousing and retail area"],
       statement=OPS, category=C.OPERATIONAL, value_type=CNT,
       is_operational=True, sign="positive"),
    _m("footfalls", "Footfalls", "operational",
       ["footfalls", "customer footfalls", "footfall"],
       statement=OPS, category=C.OPERATIONAL, value_type=CNT,
       is_operational=True, sign="positive"),
    _m("registered_customers", "Registered Customers", "operational",
       ["registered customer base", "registered customers"],
       statement=OPS, category=C.OPERATIONAL, value_type=CNT,
       is_operational=True, sign="positive"),
    _m("throughput", "Throughput", "operational",
       ["throughput", "production meant for sale", "crude processed"],
       statement=OPS, category=C.OPERATIONAL, value_type=CNT,
       is_operational=True, sign="positive"),
    _m("production", "Production", "operational",
       ["production", "gross production", "output"],
       statement=OPS, category=C.OPERATIONAL, value_type=CNT,
       is_operational=True, sign="positive"),
    _m("volumes", "Volumes", "operational",
       ["volumes", "sales volume", "volume"],
       statement=OPS, category=C.OPERATIONAL, value_type=CNT,
       is_operational=True, sign="positive"),
    _m("utilisation", "Utilisation", "operational",
       ["utilisation", "utilization", "capacity utilisation",
        "capacity utilization", "utilisation rate"],
       statement=OPS, category=C.OPERATIONAL, value_type=PCT,
       is_operational=True),
    _m("occupancy", "Occupancy", "operational",
       ["occupancy", "occupancy rate", "occupancy level"],
       statement=OPS, category=C.OPERATIONAL, value_type=PCT,
       is_operational=True),
)

METRICS_BY_KEY: dict[str, MetricDef] = {m.key: m for m in METRICS}

def _general_aliases(d: MetricDef) -> tuple[str, ...]:
    """The wordings that name ``d`` wherever they appear: its aliases, and its
    display name unless that name is one it may carry only in context -- the
    balance-sheet line "Non-Controlling Interests" is not, by those words
    alone, the minority's share of a period's profit."""
    context = {normalize_label(a) for a in (*d.stock_aliases, *d.flow_aliases)}
    if normalize_label(d.display) in context:
        return d.aliases
    return (d.display, *d.aliases)


_ALIAS_INDEX: dict[str, str] = {}
for _m_def in METRICS:
    for _a in _general_aliases(_m_def):
        _ALIAS_INDEX.setdefault(normalize_label(_a), _m_def.key)


def _unambiguous_index(keyed) -> dict[str, str]:
    """An index in which a key naming two metrics names neither.

    Squashing spaces and collapsing recognition confusions both make keys
    coarser, so two aliases of different metrics can meet on one key. Such a
    key is dropped rather than resolved: a label reaching it cannot be told
    apart, and guessing between two measures is exactly what the taxonomy
    must not do.
    """
    out: dict[str, str | None] = {}
    for key, metric in keyed:
        if not key:
            continue
        if key in out and out[key] != metric:
            out[key] = None
        else:
            out.setdefault(key, metric)
    return {k: v for k, v in out.items() if v is not None}


def _squash(norm: str) -> str:
    return norm.replace(" ", "")


#: Exact lookup that survives lost spaces: "NETPROFIT/(LOSS)FROMORDINARY-
#: ACTIVITIESAFTERTAX" is "net profit (loss) from ordinary activities after
#: tax" with the spaces OCR did not see.
_SQUASHED_INDEX = _unambiguous_index(
    (_squash(normalize_label(a)), d.key)
    for d in METRICS for a in _general_aliases(d))

#: Exact lookup that survives the recogniser's known confusions ("Interest
#: eamed", "Prafit").
_RECOGNITION_INDEX = _unambiguous_index(
    (recognition_key(normalize_label(a)), d.key)
    for d in METRICS for a in _general_aliases(d))

_STOCK_INDEX = _unambiguous_index(
    (_squash(normalize_label(a)), d.key) for d in METRICS for a in d.stock_aliases)
_FLOW_INDEX = _unambiguous_index(
    (_squash(normalize_label(a)), d.key) for d in METRICS for a in d.flow_aliases)

#: Words that do not change what a label measures when added to an alias.
_NEUTRAL_WORDS = frozenset({
    "total", "net", "consolidated", "standalone", "reported", "the", "and",
    "of", "in", "on", "at", "for", "from", "to", "as", "per", "rs", "inr",
    "crore", "crores", "cr", "million", "millions", "mn", "billion", "bn",
    "lakh", "lakhs", "figures", "ind", "gaap", "period", "quarter", "year",
    "ended", "an", "during", "expense", "expenses",
})


@lru_cache(maxsize=65536)
def _substantive_words(text: str) -> frozenset[str]:
    return frozenset(w for w in text.split()
                     if len(w) > 2 and w not in _GENERIC_TOKENS
                     and w not in _NEUTRAL_WORDS and not _PERIOD_TOKEN.match(w))


def _each_names_something_else(a: str, b: str) -> bool:
    """Whether each label carries a substantive word the other lacks.

    "Customer assets" -- the second line of a bank's wrapped "% of net
    non-performing customer assets to net customer assets" -- scored 0.86
    against "customer base" and a percentage was reported as a subscriber
    count. They share "customer"; one speaks of assets, the other of a base.
    Two labels that each name something the other does not are about
    different things, however alike they read. A word counts as shared when
    the other side has a near-spelling of it (a misread) or contains it (lost
    spaces), so damage to a label is not mistaken for a difference.
    """
    wa, wb = _substantive_words(a), _substantive_words(b)
    ua, ub = wa - wb, wb - wa
    if not ua or not ub:
        return False

    def near(w: str, pool: set[str]) -> bool:
        for v in pool:
            if w in v or v in w:
                return True
            if recognition_key(w) == recognition_key(v):
                return True
            if SequenceMatcher(None, w, v).ratio() >= 0.75:
                return True
        return False

    return (any(not near(w, wb) for w in ua)
            and any(not near(w, wa) for w in ub))


#: A token that dates a label rather than qualifying what it measures.
_PERIOD_TOKEN = re.compile(r"^(?:q[1-4]|h[12]|9m|fy\d{2,4}|(?:19|20)\d{2})$")


def _nets_the_label(label: str, alias: str) -> bool:
    """Whether the alias is a NET measure the label does not say it is.

    "Other operating income" -- a gross line, printed above "Other operating
    expenses" -- scored 0.83 against "other operating income/(loss), net" and a
    292 of income was reported as the net line, which on that statement is
    -1,228 (Phase 22). A gross line is not its net; interest income is not net
    interest income. Only an alias that nets a PAIR ("income/(loss), net") is
    meant: in "net cash generated by operating activities" the word is a
    convention, and Apple's "Cash generated by operating activities" is the
    same line. The label is looked at without its spaces, so a scan's
    "Netprofit" still says net."""
    return ("net" in alias.split() and "/" in alias
            and "net" not in label.split() and "/" not in label
            and "net" not in label.replace(" ", "")[:4])


def _adds_a_qualifier(label: str, alias: str) -> bool:
    """Whether the label is the alias plus a word that narrows what it means.

    "Network operating expenses" contains "operating expenses" and scored
    0.82 against it -- a telecom's network costs reported as its whole
    operating cost base. "Deferred tax assets" begins with "deferred tax" and
    was reported as the tax charge. A label that names a known measure and
    adds a substantive word names a different measure; only the taxonomy
    declaring the longer label as an alias may say otherwise.
    """
    lt, at = set(label.split()), set(alias.split())
    if not at or not at < lt:
        return False
    return any(len(w) > 2 and w not in _GENERIC_TOKENS and w not in _NEUTRAL_WORDS
               and not _PERIOD_TOKEN.match(w)
               for w in lt - at)


@lru_cache(maxsize=4096)
def _vocabulary(key: str) -> frozenset[str]:
    """Every word the measure's own declared names use."""
    d = METRICS_BY_KEY.get(key)
    if d is None:
        return frozenset()
    words: set[str] = set()
    for alias in (*_general_aliases(d), d.display or ""):
        words |= set(normalize_label(alias).split())
    return frozenset(words)


def _qualifies_the_core(label: str, alias: str, vocabulary: frozenset[str]) -> bool:
    """Whether the label names the alias's core more narrowly.

    "Construction materials consumed" scored 0.84 against "cost of materials
    consumed" and a contractor's construction materials were filed as its cost
    of materials, beside the raw-materials line that is (Phase 22). The label
    ends with the alias's core -- its words after the generic head "cost of"
    -- and puts a word of its own where the head was. A word the measure's own
    names use ("raw" in "raw material cost") does not narrow it.
    """
    lw, aw = label.split(), alias.split()
    i = 0
    while i < len(aw) and (aw[i] in _GENERIC_TOKENS or aw[i] in _JOINING_WORDS):
        i += 1
    core = aw[i:]
    if i == 0 or len(core) < 2 or len(lw) <= len(core) or lw[-len(core):] != core:
        return False
    return any(len(w) > 2 and w not in _GENERIC_TOKENS and w not in _NEUTRAL_WORDS
               and not _PERIOD_TOKEN.match(w) and w not in vocabulary
               for w in lw[:-len(core)])


#: Words a repaired label may lack without naming less than its alias.
_JOINING_WORDS = frozenset({"of", "and", "the", "on", "in", "by", "for", "a",
                            "an", "to", "from", "with", "at", "or"})
#: Bracketed text: notes and qualifiers, not the name of the line.
_NOTE_IN_BRACKETS = re.compile(r"[(\[（][^()\[\]（）]*[)\]）]")
#: Letters left over after a repair that can still be damage, not a word.
_MAX_REPAIR_RESIDUE = 3


def _within_one_edit(a: str, b: str) -> bool:
    if abs(len(a) - len(b)) > 1:
        return False
    if len(a) == len(b):
        return sum(x != y for x, y in zip(a, b)) <= 1
    short, long_ = (a, b) if len(a) < len(b) else (b, a)
    return any(long_[:i] + long_[i + 1:] == short for i in range(len(long_)))


def _find_word(word: str, text: str) -> tuple[int, int] | None:
    """Where a recognition-reduced word sits in a reduced label, allowing a
    plural and, in a word of five letters or more, one misread letter."""
    forms = [word]
    if len(word) > 3 and word.endswith("s"):
        forms.append(word[:-1])
    if len(word) > 4 and word.endswith("es"):
        forms.append(word[:-2])
    for form in forms:
        at = text.find(form)
        if at >= 0:
            # a plural the label adds belongs to the word
            end = at + len(form)
            if text[end:end + 1] == "s" and form == word:
                end += 1
            return at, end
    if len(word) >= 5:
        # a misread letter before a lost or doubled one: the shorter window
        # would leave a letter of the word behind as residue
        for size in (len(word), len(word) + 1, len(word) - 1):
            for at in range(0, len(text) - size + 1):
                if _within_one_edit(word, text[at:at + size]):
                    return at, at + size
    return None


@lru_cache(maxsize=8192)
def _repair_accounts_for(label: str, alias: str) -> bool:
    """Whether a recognition repair explains the whole difference between a
    label and an alias -- every word of the alias found in the label, and
    nothing substantive left over.

    Collapsing confusable letters and lost spaces makes damaged labels meet
    their aliases, and it also makes undamaged ones meet aliases they do not
    name: "Credit Cards" reduces to "creditcords" and scored 0.90 against
    "creditors"; "As a % of Revenue" reduced to "osoofrevenue" and became cost
    of revenue; a bank's "Asset Management" segment became assets under
    management. A repair corrects misread letters; it does not supply a
    missing word, remove an extra one, or turn a ratio ("Operating expenses to
    NTI") into its numerator.
    """
    norm_alias = normalize_label(alias)
    words = re.findall(r"[a-z0-9]+", norm_alias)
    if re.search(r"\bto\b", normalize_label(label)) and "to" not in words:
        return False
    # "Total capital (%)" says "ratio" with its percent sign.
    if "%" in label:
        words = [w for w in words if w not in ("ratio", "percentage", "percent")]
    # Notes in brackets, figures run into the label, and alternatives joined
    # by a slash ("Sales/income from operations") are not part of the name.
    text = normalize_label(_NOTE_IN_BRACKETS.sub(" ", label))
    segments = [recognition_key(re.sub(r"\d", "", s)) for s in text.split("/")]
    found_in: set[int] = set()
    residue_segments = list(segments)
    for word in words:
        key = recognition_key(word)
        if not key:
            continue
        hit = None
        for i, seg in enumerate(residue_segments):
            span = _find_word(key, seg)
            if span is not None:
                hit = (i, span)
                break
        if hit is None:
            if word in _JOINING_WORDS:
                continue
            return False
        i, (start, end) = hit
        found_in.add(i)
        residue_segments[i] = residue_segments[i][:start] + residue_segments[i][end:]
    residue = sum(len(residue_segments[i]) for i in found_in)
    return residue <= _MAX_REPAIR_RESIDUE


#: Match methods that are the taxonomy recognising its own declared wording,
#: as opposed to a similarity judgement.
EXACT_METHODS = frozenset({"exact", "squashed", "recognition-exact", "context"})


@dataclass(slots=True)
class MetricMatch:
    key: str
    definition: MetricDef
    score: float
    matched_alias: str
    method: str  # "exact" | "squashed" | "recognition-exact" | "context" | "prefix" | "fuzzy" | "recognition-repair"

    @property
    def is_exact(self) -> bool:
        return self.method in EXACT_METHODS


def _similarity(a: str, b: str) -> float:
    """Blend sequence similarity with token overlap.

    Token overlap alone rewards long labels that merely share common words;
    sequence similarity alone punishes reordering.  Together they behave well on
    financial labels such as "Profit After Tax and Share of Profit of Associates".
    """
    seq = SequenceMatcher(None, a, b).ratio()
    ta, tb = set(a.split()), set(b.split())
    if not ta or not tb:
        return seq
    jac = len(ta & tb) / len(ta | tb)
    cover = len(ta & tb) / min(len(ta), len(tb))
    return max(seq, 0.45 * seq + 0.30 * jac + 0.25 * cover)


#: A trailing parenthetical that states a unit or annotates the figure, and so
#: may be dropped for a retry: "(Rs/Sub/month)", "(Mn sq ft)", "(%)", "(net)".
#: One that names a scope -- "(Domestic)", "(Whole Bank)", "(Foreign
#: Offices)" -- is part of what the row measures and stays: a bank's domestic
#: margin and its whole-bank margin are two figures, not one.
_UNIT_OR_NOTE_PAREN = re.compile(
    r"\(\s*[^)]*(?:%|₹|\$|\brs\b|\binr\b|\busd\b|\bmn\b|\bmillions?\b|"
    r"\bbillions?\b|\bbn\b|\bcrores?\b|\bcr\b|\blakhs?\b|\bthousands?\b|"
    r"'000|\bsq\.?\s*ft\b|\bper\b|/|\btimes\b|\bdays\b|\bbps\b|\bno\.|"
    r"\bnos\b|\bnumber\b|\bunits?\b|\btonnes?\b|\bmt\b|\bkt\b|\bnote\b|"
    r"\bnet\b|\bbasel\b|\baudited\b|\bunaudited\b|\bx\b"
    # a period, which says when and not what: "(Annualized Q1 FY27)", "(FY26)"
    r"|\bq[1-4]\b|\bh[12]\b|\bfy\s*'?\d{2,4}\b|\b9m\b|annuali[sz]ed"
    r"|\b(?:19|20)\d{2}\b)[^)]*\)\s*$", re.I)


#: A label, a subtotal formula -- "(e)+(f)", "(1+2)", "(9)-(10)" -- and then
#: two or more words outside any bracket.
_FORMULA_THEN_TEXT = re.compile(
    r"^(.*?\((?:\s*\w{1,3}\s*\)?\s*[+\-]\s*\(?\s*\w{1,3})+\s*\))"
    r"\s+(?![(\[])[A-Za-z][\w'/-]*\s+[A-Za-z].*$")
#: An "I" read as "/", "1", "l" or "|" between capitals, with two more after.
_ACRONYM_I = re.compile(r"(?<=[A-Z])[/1l|](?=[A-Z]{2})")
#: A unit or annualisation bracket -- "(in ₹)", "(not annualised)" -- ends a
#: label line as a formula does. "... expense (not annualised) (in ₹) Diluted
#: EPS before and after ..." is the tail of the basic-EPS line and the head of
#: the diluted one, on the row holding the BASIC figures; it was filed as
#: diluted EPS.
_UNIT_THEN_TEXT = re.compile(
    r"\(\s*(?:in\s+[^)]{0,14}|not\s+annuali[sz]ed|annuali[sz]ed|[₹$€£]|rs\.?|inr|usd)\s*\)"
    r"\s+(?![(\[])[A-Za-z][\w'/-]*\s+[A-Za-z]", re.I)
#: "... relating to (the) above" and then more words: the back-reference ends
#: the line, so what follows is another one.
_ABOVE_THEN_TEXT = re.compile(
    r"\brelating\s+to\s+(?:the\s+)?above\s+(?!items?\s*(?:$|[(\[:;,.]))[A-Za-z]", re.I)


def _exact(norm: str, stock: bool | None) -> tuple[str, str] | None:
    """Every exact route to a metric, most literal first: (key, method)."""
    hit = _ALIAS_INDEX.get(norm)
    if hit:
        return hit, "exact"
    sq = _squash(norm)
    hit = _SQUASHED_INDEX.get(sq)
    if hit:
        return hit, "squashed"
    hit = _RECOGNITION_INDEX.get(recognition_key(norm))
    if hit:
        return hit, "recognition-exact"
    if stock is True:
        hit = _STOCK_INDEX.get(sq)
    elif stock is False:
        hit = _FLOW_INDEX.get(sq)
    if hit:
        return hit, "context"
    return None


_EXACT_SCORES = {"exact": 1.0, "squashed": 0.99, "recognition-exact": 0.96,
                 "context": 0.98}


@lru_cache(maxsize=8192)
def match_metric(label: str, *, threshold: float | None = None,
                 operational_ok: bool = True,
                 stock: bool | None = None) -> MetricMatch | None:
    """Map a source label onto a canonical metric, or None if nothing fits.

    Returning None is a valid, meaningful outcome: a row the taxonomy does not
    recognise must not be forced onto the nearest known metric.

    ``stock`` says what the row's columns are: True for balances ("As at"),
    False for flows ("... ended"), None when the columns do not say. It
    admits only the aliases that are unambiguous in that kind of statement.
    """
    from app.config.settings import settings  # local: avoids import cycle at import time
    thr = settings.metric_match_threshold if threshold is None else threshold

    norm = normalize_label(label)
    if not norm or len(norm) < 2:
        return None
    # Two label lines run together by the row grouper name two rows, and
    # nothing in the text says which of them the figures belong to. A
    # subtotal's formula, or "relating to above", ends a line; words after it
    # that are not in brackets are the next one. "Other income (e)+(f)
    # Premium and other operating income from insurance" was matched as the
    # premium, and an image statement's "Profit for the period (VII-VIII)
    # Other comprehensive income ... Income tax relating to above items" --
    # the tax line's figures -- as the profit. Neither is guessed.
    if (_FORMULA_THEN_TEXT.match(label) or _ABOVE_THEN_TEXT.search(label)
            or _UNIT_THEN_TEXT.search(label)):
        return None

    found = _exact(norm, stock)
    if found:
        key, method = found
        d = METRICS_BY_KEY[key]
        if operational_ok or not d.is_operational:
            return MetricMatch(key, d, _EXACT_SCORES[method], norm, method)

    # Retry without a trailing qualifier: financial tables append the unit or a
    # clarifier to the row label -- "ARPU (Rs/Sub/month)", "Area (Mn sq ft)".
    bare = (_TRAILING_PAREN.sub("", label or "").strip()
            if _UNIT_OR_NOTE_PAREN.search(label or "") else label)
    if bare and bare != label:
        norm_bare = normalize_label(bare)
        hit = _ALIAS_INDEX.get(norm_bare)
        if hit:
            d = METRICS_BY_KEY[hit]
            if operational_ok or not d.is_operational:
                return MetricMatch(hit, d, 0.97, norm_bare, "exact")

    stripped = _strip_noise(norm)
    if stripped and stripped != norm:
        hit = _ALIAS_INDEX.get(stripped)
        if hit:
            d = METRICS_BY_KEY[hit]
            if operational_ok or not d.is_operational:
                return MetricMatch(hit, d, 0.95, stripped, "exact")

    # An acronym whose "I" the recogniser read as "/", "1" or "l" -- "EB/TDA"
    # and "EB1TDA" for EBITDA, in a results release's footnote -- is the
    # acronym, where the corrected spelling is a label the taxonomy declares
    # word for word. Nothing is taken on similarity.
    fixed = _ACRONYM_I.sub("I", label)
    if fixed != label:
        found = _exact(normalize_label(fixed), stock)
        if found:
            key, _method = found
            d = METRICS_BY_KEY[key]
            if operational_ok or not d.is_operational:
                return MetricMatch(key, d, 0.96, normalize_label(fixed),
                                   "recognition-exact")

    # "Attributable to owners of parent" says to whom, not what: the profit,
    # the comprehensive income or the equity is the row above it. Guessing
    # the subject filed an income statement's profit split as balance-sheet
    # equity and its minority share as other comprehensive income.
    subjectless = norm.startswith("attributable to")

    best: MetricMatch | None = None
    for d in METRICS:
        if not operational_ok and d.is_operational:
            continue
        for alias in _general_aliases(d):
            na = normalize_label(alias)
            if not na:
                continue
            if subjectless and not na.startswith("attributable to"):
                continue
            if _contrast_mismatch(norm, na):
                continue
            if _adds_a_qualifier(norm, na):
                continue
            if _qualifies_the_core(norm, na, _vocabulary(d.key)):
                continue
            # A prefix match must account for most of the label.  Without the
            # coverage test, the 6-character alias "income" would claim
            # "income tax relating to items that will not be reclassified..."
            # with a near-perfect score.
            coverage = len(na) / max(len(norm), 1)
            if norm.startswith(na) and len(na) >= 4 and coverage >= 0.55 and (
                    len(norm) == len(na) or not norm[len(na)].isalnum()):
                score = 0.9 + 0.1 * coverage
                method = "prefix"
            else:
                if not _share_a_distinctive_word(norm, na):
                    continue
                if _each_names_something_else(norm, na):
                    continue
                if _nets_the_label(norm, na):
                    continue
                score = _similarity(norm, na)
                method = "fuzzy"
                # A label the recogniser damaged scores below threshold against
                # its own alias. Comparing the two with confusable characters
                # collapsed removes that handicap symmetrically; the threshold
                # is unchanged, and a pair that was never close stays never
                # close.
                # A rate's alias ("ebitda %") and its measure's ("ebitda")
                # reduce to the same recognition key; a label with no rate
                # sign cannot repair onto the rate ("EB1TDA" became the
                # EBITDA margin).
                if score < thr and not ("%" in alias and "%" not in label):
                    ra, rb = recognition_key(norm), recognition_key(na)
                    if ra and rb and ra != norm.replace(" ", ""):
                        repaired = _similarity(ra, rb)
                        if repaired > score and _repair_accounts_for(label, alias):
                            score = repaired
                            method = "recognition-repair"
            if best is None or score > best.score:
                best = MetricMatch(d.key, d, round(score, 4), na, method)
    if best and best.score >= thr:
        return best
    return None


def get(key: str) -> MetricDef | None:
    return METRICS_BY_KEY.get(key)


def families() -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for m in METRICS:
        out.setdefault(m.family, []).append(m.key)
    return out


def by_statement() -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for m in METRICS:
        out.setdefault(str(m.statement), []).append(m.key)
    return out


def by_category() -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for m in METRICS:
        out.setdefault(str(m.category), []).append(m.key)
    return out


def revenue_preference() -> tuple[str, ...]:
    """Revenue denominators, most-specific first.

    Margin arithmetic needs *a* revenue line; documents differ on which they
    publish, so this states the preference order explicitly instead of leaving
    it implicit in each rule.
    """
    return ("revenue_from_operations", "gross_revenue", "revenue",
            "net_revenue", "sales", "total_income")


def all_metrics() -> list[dict]:
    return [{"key": m.key, "display": m.display, "family": m.family,
             "statement": str(m.statement), "category": str(m.category),
             "value_type": str(m.value_type), "is_flow": m.is_flow,
             "is_operational": m.is_operational, "sign": m.sign,
             "definition": m.definition, "distinct_from": list(m.distinct_from),
             "aliases": list(m.aliases)} for m in METRICS]
