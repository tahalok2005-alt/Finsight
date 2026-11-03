"""Numeric, currency and unit normalisation.

Principles
----------
1. **Never destroy the source.**  Every parsed number keeps ``raw_text`` exactly
   as it was read, together with the magnitude word and currency that were in
   the document.  ``base_value`` is an *additional* convenience field, not a
   replacement -- so a value reported in crore is still a crore value.
2. **Never invent a unit.**  If the document does not say whether a figure is in
   crore or million, ``magnitude`` stays ``NONE`` and ``base_value`` stays
   ``None`` rather than guessing.
3. **Accounting conventions are honoured**: parentheses mean negative, a bare
   dash means "nil", and a trailing minus is a minus.
"""
from __future__ import annotations

import re
from typing import Iterable

from app.models.enums import Currency, MagnitudeUnit, ValueType
from app.models.schemas import NormalizedNumber

# --------------------------------------------------------------------- units
MAGNITUDE_FACTORS: dict[MagnitudeUnit, float] = {
    MagnitudeUnit.UNIT: 1.0,
    MagnitudeUnit.THOUSAND: 1e3,
    MagnitudeUnit.LAKH: 1e5,
    MagnitudeUnit.MILLION: 1e6,
    MagnitudeUnit.CRORE: 1e7,
    MagnitudeUnit.BILLION: 1e9,
    MagnitudeUnit.TRILLION: 1e12,
}

_MAGNITUDE_WORDS: list[tuple[re.Pattern[str], MagnitudeUnit]] = [
    (re.compile(r"\bcrores?\b|\bcr\.?\b", re.I), MagnitudeUnit.CRORE),
    (re.compile(r"\blakhs?\b|\blacs?\b", re.I), MagnitudeUnit.LAKH),
    (re.compile(r"\bbillions?\b|\bbn\b", re.I), MagnitudeUnit.BILLION),
    (re.compile(r"\btrillions?\b|\btn\b", re.I), MagnitudeUnit.TRILLION),
    (re.compile(r"\bmillions?\b|\bmn\b|\bmm\b", re.I), MagnitudeUnit.MILLION),
    # Both short forms of "thousand" have to be pinned to their context or they
    # match ordinary text.  "000" is a declaration only standing alone -- "Rs
    # '000", "in 000s" -- because the comma inside a figure is a word boundary,
    # so a bare ``\b000s?\b`` reads "79,000" as "79 thousand" and lets a number
    # overrule the table that declared it in millions.  "k" is a suffix on a
    # figure ("50k"), never a word: as a bare ``\bk\b`` it matches the list
    # marker in a row labelled "k) Operating margin".
    (re.compile(r"\bthousands?\b|(?<![\d.,])000s?\b|\d\s*k\b", re.I),
     MagnitudeUnit.THOUSAND),
]

#: Magnitude words used only in the Indian numbering system.  Seeing one is
#: strong evidence the amount is in rupees even when no symbol was legible.
INDIAN_MAGNITUDES = frozenset({MagnitudeUnit.CRORE, MagnitudeUnit.LAKH})

CURRENCY_SYMBOLS: dict[str, Currency] = {
    "₹": Currency.INR, "$": Currency.USD, "€": Currency.EUR,
    "£": Currency.GBP, "¥": Currency.JPY,
}

_CURRENCY_WORDS: list[tuple[re.Pattern[str], Currency]] = [
    (re.compile(r"\b(inr|rs\.?|rupees?)\b|₹", re.I), Currency.INR),
    (re.compile(r"\b(usd|us\$|dollars?)\b|\$", re.I), Currency.USD),
    (re.compile(r"\b(eur|euros?)\b|€", re.I), Currency.EUR),
    (re.compile(r"\b(gbp|pounds? sterling)\b|£", re.I), Currency.GBP),
    (re.compile(r"\b(jpy|yen)\b|¥", re.I), Currency.JPY),
    (re.compile(r"\b(aed|dirhams?)\b", re.I), Currency.AED),
    (re.compile(r"\b(chf|swiss\s+francs?)\b", re.I), Currency.CHF),
]

# 1,23,456.78 (Indian grouping) and 123,456.78 (Western) both parse.
#
# The separated form requires *at least one* group separator. With a star
# it also matched the leading three digits of an unseparated figure, and
# alternation is ordered, so it won: "14272.21" matched "142" and the rest
# was discarded -- silently, and a hundredfold wrong. Only filings that
# print their figures without separators were affected, which is why it
# survived: the benchmark documents print "1,501.42" throughout, and the
# first statement to print "14272.21" entered the corpus in Phase 19.
_NUMBER_CORE = r"\d{1,3}(?:[, \s]\d{2,3})+(?:\.\d+)?|\d+(?:\.\d+)?"
NUMBER_RE = re.compile(_NUMBER_CORE)
# A numeric cell may carry a unit suffix *inside* the parentheses that mark it
# negative -- "(210 bps)" is how a margin decline is printed -- so the closing
# bracket has to be allowed both before and after the suffix.
_STRICT_NUMBER_RE = re.compile(rf"^[\s]*[-+(]?\s*[₹$€£¥]?\s*({_NUMBER_CORE})\s*\)?"
                               r"\s*(%|bps|bp|pp)?\s*\)?\s*[-+]?\s*$", re.I)
_NIL_RE = re.compile(r"^\s*[-–—]\s*$")
#: The widest a printed rate plausibly gets.  Margins sit inside a hundred and
#: growth rates occasionally reach the hundreds; four figures and beyond is not
#: a rate any more.  Used only to recognise a figure that *cannot* be the rate
#: its metric name suggests -- never to reject one that can.
RATE_CEILING = 1000.0

_PERCENT_RE = re.compile(r"%|\bper\s*cent\b|\bpercent(age)?\b", re.I)
_BPS_RE = re.compile(r"\bbps\b|\bbasis points?\b|\bbp\b", re.I)


def looks_numeric(text: str) -> bool:
    """True if the token reads as a numeric table cell.

    Deliberately permissive about surrounding currency/percent decoration but
    strict about the token being *predominantly* a number, so that a sentence
    containing a figure does not register as a numeric cell.
    """
    t = normalise_separators(text or "").strip()
    if not t or len(t) > 32:
        return False
    if _NIL_RE.match(t):
        return True
    if _STRICT_NUMBER_RE.match(t):
        return True
    digits = sum(c.isdigit() for c in t)
    if digits == 0:
        return False
    letters = sum(c.isalpha() for c in t)
    return digits >= 1 and letters <= 3 and digits / len(t) >= 0.4


#: A figure whose thousands separators were recognised as full stops:
#: "1.053.69" for 1,053.69, "8.403.55" for 8,403.55. Two or more stops in one
#: token is not a number in any convention, so this is unambiguous evidence of
#: a misread separator rather than an interpretation.
#:
#: Two stops are the minimum, and the distinction is the whole safety of this
#: repair: "1.053" is an ordinary decimal and must be left alone, while
#: "1.053.69" cannot be a number at all. Requiring a single group would rewrite
#: every genuine three-decimal figure -- a ratio of 1.053, a per-share value of
#: 12.345 -- by a factor of a thousand, which is the same error this repair
#: exists to prevent, in the opposite direction.
_MISREAD_SEPARATORS = re.compile(
    r"^(?P<lead>\d{1,3})"
    r"(?:(?P<mid>(?:\.\d{3})+)\.(?P<tail>\d{1,2})"   # 1.053.69 -> 1053.69
    r"|(?P<mid2>(?:\.\d{3}){2,}))$")                 # 1.234.567 -> 1234567


def repair_misread_separators(token: str) -> str | None:
    """Rewrite a figure whose commas were recognised as full stops.

    Returns the corrected token, or None when there is nothing to repair.

    This is a recognition repair, not a guess. A token like "1.053.69" cannot
    be read as a number under any convention: the decimal separator can appear
    once. What makes the correction determinate is the shape -- every group
    after the first is exactly three digits, which is what a thousands
    separator produces and what a decimal point never does.

    It matters because the failure was silent and looked plausible. The number
    pattern stopped at the second stop, so "1.053.69" parsed to **1.053** -- a
    value a thousand times too small, in the same units, with no warning. On
    one statement that turned total income of 1,053.69 crore into 1.053 crore,
    and the only reason it came to light is that the cross-footing check found
    revenue plus other income no longer matched the total.
    """
    m = _MISREAD_SEPARATORS.match(token.strip())
    if m is None:
        return None
    groups = m.group("mid") or m.group("mid2") or ""
    whole = m.group("lead") + groups.replace(".", "")
    tail = m.group("tail")
    return f"{whole}.{tail}" if tail else whole


#: A comma standing where the decimal point belongs: "62005,98". A thousands
#: separator is always followed by exactly three digits, so a comma with two
#: digits and nothing after it cannot be one. Both the European convention and
#: an OCR misreading of a full stop produce this, and neither is ambiguous.
_DECIMAL_COMMA = re.compile(r"^(?P<whole>\d+),(?P<frac>\d{1,2})$")


#: Separator characters a recogniser emits in place of an ordinary comma or
#: full stop. A degraded scan produced "123，162" -- a fullwidth comma --
#: for 123,162, and an apostrophe is the Swiss thousands separator besides.
#: Left alone they turn one figure into two, and the ambiguous-cell guard then
#: discards a perfectly good value.
#: The recogniser also emits fullwidth brackets: a telecom's "(844)" -- a
#: loss from associates -- arrived as "（844)" and was reported as +844, with no
#: flag. The brackets, signs and digits are mapped one for one. Unicode NFKC
#: is deliberately NOT used: it also turns a superscript footnote marker into
#: a digit, and "12,351¹" would become 123,511.
_SEPARATOR_VARIANTS = str.maketrans({
    "，": ",",   # fullwidth comma
    "、": ",",   # ideographic comma
    "ʼ": ",",   # modifier letter apostrophe
    "’": ",",   # right single quote, used as a group separator
    "．": ".",   # fullwidth full stop
    "·": ".",   # middle dot
    "（": "(",   # fullwidth left parenthesis
    "）": ")",   # fullwidth right parenthesis
    "－": "-",   # fullwidth hyphen-minus
    "−": "-",   # minus sign
    "％": "%",   # fullwidth percent
    "＋": "+",   # fullwidth plus
    **{chr(0xFF10 + i): str(i) for i in range(10)},   # fullwidth digits
})


def normalise_separators(text: str) -> str:
    """Map recogniser variants of the group and decimal separators onto ASCII."""
    return (text or "").translate(_SEPARATOR_VARIANTS)


#: A figure grouped with stops or spaces and ending in a decimal comma --
#: "1.234,56", "1.234.567,89", "12 345,67". A comma after grouped digits can
#: only be the decimal separator, so the form is determinate. Parsed by the
#: Anglo pattern it came out as 1.234, silently a thousand times too small.
_EUROPEAN = re.compile(
    r"^(?P<int>\d{1,3}(?:(?:\.\d{3})+|(?:[ \xa0]\d{3})+)),(?P<frac>\d{1,2})$")


def european_figure(token: str) -> str | None:
    """"1.234,56" -> "1234.56", or None when the token is not that form."""
    m = _EUROPEAN.match(token.strip())
    if m is None:
        return None
    whole = re.sub(r"[.\s\xa0]", "", m.group("int"))
    return f"{whole}.{m.group('frac')}"


def repair_decimal_comma(token: str) -> str | None:
    """Rewrite a figure whose decimal point was recognised as a comma."""
    m = _DECIMAL_COMMA.match(token.strip())
    if m is None:
        return None
    return f"{m.group('whole')}.{m.group('frac')}"


#: Digit groups and what separates them, inside one token.
_GROUPED = re.compile(r"\d+(?:[.,\xa0 ]\d+)*")


_THREE_DECIMALS = re.compile(r"^\s*(?P<neg>[(\-–])?\s*(?P<lead>[1-9]\d{0,2})\.(?P<grp>\d{3})\s*\)?\s*$")


_TWO_PLACES = re.compile(r"^\(?-?\d{1,3}(?:[,\xa0 ]?\d{2,3})*\.\d{2}\)?$")
_COLON_POINT = re.compile(
    r"^(?P<head>\(?-?\d{1,3}(?:,\d{2,3})*|\(?-?\d+):(?P<tail>\d{2})(?P<close>\))?$")


def table_decimals(texts) -> str | None:
    """Whether a table prints its amounts as whole numbers.

    Returns "integer" when the table shows at least two comma-grouped whole
    amounts ("585,391") and no amount of four or more whole digits carrying a
    decimal part. Per-share figures and ratios ("13.38") are small and do not
    count against it. None when the table does not show its convention.
    """
    whole = decimal_large = two_places = 0
    for t in texts:
        t = normalise_separators(t or "").strip()
        if re.search(r"\d,\d{3}(?![\d.])", t) and "." not in t:
            whole += 1
        if re.search(r"\d(?:[,\xa0 ]?\d){3,}\.\d{1,2}(?!\d)", t):
            decimal_large += 1
        if _TWO_PLACES.match(t):
            two_places += 1
    if whole >= 2 and not decimal_large:
        return "integer"
    # "2dp": the table prints its amounts to two places -- "17,506.80".
    if two_places >= 3 and two_places >= 2 * whole:
        return "2dp"
    return None


def repair_by_decimals(text: str, convention: str | None) -> str | None:
    """A thousands comma the recogniser read as a decimal point.

    In a table printed in whole numbers, "9.066" beside 585,391 and 8,785 is
    9,066 with its comma misread: a statement does not print one line to
    three decimal places among whole numbers. Only that exact shape is
    repaired -- one to three leading digits, not starting with zero (a
    thousands group never does; "0.123" is a ratio), a point, and exactly
    three digits. Returns the corrected text, or None.
    """
    if convention == "2dp":
        # A decimal point read as a colon -- "12,351:26" -- is a scanner's
        # confusion, not two figures. In a table of two-place amounts, a
        # colon followed by exactly two digits at the end of a figure is the
        # point it was printed as.
        m = _COLON_POINT.match(normalise_separators(text or ""))
        if m:
            return f"{m.group('head')}.{m.group('tail')}{m.group('close') or ''}"
        return None
    if convention != "integer":
        return None
    m = _THREE_DECIMALS.match(normalise_separators(text or ""))
    if not m:
        return None
    body = f"{m.group('lead')},{m.group('grp')}"
    return f"({body})" if m.group("neg") else body


def table_grouping(texts) -> str | None:
    """How a table separates thousands, read from its unambiguous figures.

    "20,211" can only be comma grouping. "18 090.16" can only be space
    grouping -- a decimal part leaves no other reading. A bare "166 632" could
    be either one figure or two columns' figures run together, and is decided
    by what the rest of the table does. Returns "comma", "space" or None.
    """
    comma = space = spaced = figures = 0
    for t in texts:
        t = normalise_separators(t or "")
        if re.search(r"\d", t):
            figures += 1
        if re.search(r"\d,\d{3}(?!\d)", t):
            comma += 1
        if re.search(r"\d[\xa0 ]\d{3}\.\d", t):
            space += 1
        if _SPACE_GROUPED.match(t.strip()):
            spaced += 1
    if comma and not space:
        return "comma"
    if space and not comma:
        return "space"
    # A Swiss or French statement prints "43 109" and "(23 118)" and never a
    # decimal in its thousands (Phase 22). With no comma-grouped figure in the
    # table at all, a third or more of its figures space-grouped in threes is
    # the table's convention: two columns run into one cell happen to a few
    # cells of a table, never to a third of them.
    if not comma and spaced >= 3 and spaced >= 0.3 * figures:
        return "space"
    return None


#: A figure grouped in threes by spaces and nothing else: "43 109", "(23 118)",
#: "1 234 567".
_SPACE_GROUPED = re.compile(r"^[(\-−]?\d{1,3}(?:[\xa0 ]\d{3})+\)?$")


def _one_grouped_figure(core: str, grouping: str | None) -> bool:
    """Whether a run of digits and separators is ONE printed figure.

    A real figure uses one kind of separator throughout and a real grouping:
    western (groups of three) or Indian (groups of two before a final three).
    "20,211 17,292" mixes a comma and a space; "228 88" ends on a group of two
    after a group of three; "0.03 0.40" has a decimal part followed by more
    digits. None of those is one number in any convention.
    """
    whole, _, frac = core.partition(".")
    if "." in frac:                  # two decimal parts
        return False
    seps = set(re.findall(r"[,\xa0 ]", whole))
    if not seps:
        return True
    if len(seps) > 1 and not seps <= {"\xa0", " "}:
        return False                 # commas and spaces in one figure
    groups = re.split(r"[,\xa0 ]", whole)
    if not (1 <= len(groups[0]) <= 3) or any(not g.isdigit() for g in groups):
        return False
    tail = groups[1:]
    western = all(len(g) == 3 for g in tail)
    indian = (len(tail) >= 2 and all(len(g) == 2 for g in tail[:-1])
              and len(tail[-1]) == 3)
    if not (western or indian):
        return False
    uses_space = bool(seps & {"\xa0", " "})
    if uses_space and grouping == "comma":
        # The table groups thousands with commas, so a space between digits
        # here is the gap between two columns that landed in one cell.
        return False
    if uses_space and grouping is None and not frac:
        # "166 632" in a table that never shows its convention: one figure or
        # two, and nothing on the page says which. Refused, not guessed.
        return False
    return True


def holds_more_than_one_number(text: str, grouping: str | None = None) -> bool:
    """Whether this cell text contains two separate figures.

    A table cell holds one value. When the column boundary is missed, two
    columns' figures land in one cell -- "(0.04) 10.32", "20,211 17,292" --
    and the parser then either returns the first figure (right value, wrong
    column) or, worse, reads the two as one: "20,211 17,292" became a revenue
    of 2,021,117,292 and "228 88" a profit of 22,888, both emitted as clean,
    unflagged facts. A cell is one figure only if it is ONE figure by the
    rules every printed number obeys (see `_one_grouped_figure`).

    ``grouping`` is the table's thousands convention (`table_grouping`); it
    decides the one genuinely ambiguous shape, a space before three digits.

    Two forms are still one figure:
    * a figure whose separators OCR misread as stops -- "1.053.69" -- when
      there is no space in it for a column gap to hide in;
    * a decimal comma -- "62005,98".
    """
    t = normalise_separators(text).strip()
    if not t:
        return False
    # One sign of negation, one percent sign, one currency per figure.
    if t.count("%") > 1 or t.count("(") > 1 or len(re.findall(r"[₹$€£¥]", t)) > 1:
        return True
    if european_figure(t.strip(" ()-")) is not None:
        return False
    has_gap = bool(re.search(r"\d[\xa0 ]+\S", t))
    digits = re.sub(r"[^\d.]", "", t)
    if not has_gap and repair_misread_separators(digits) is not None:
        return False
    if repair_decimal_comma(t.strip(" ()")) is not None:
        return False
    runs = _GROUPED.findall(t)
    if len(runs) > 1:
        return True
    if not runs:
        return False
    return not _one_grouped_figure(runs[0], grouping)


def parse_number(text: str) -> tuple[float, bool, bool] | None:
    """Parse a numeric token.

    Returns ``(value, is_negative, negative_from_parentheses)`` or ``None``.
    """
    if text is None:
        return None
    t = normalise_separators(str(text)).strip()
    if not t:
        return None
    if _NIL_RE.match(t):
        return 0.0, False, False

    paren = ("(" in t and ")" in t)
    t_clean = t.replace("(", " ").replace(")", " ")
    minus = bool(re.search(r"(?<![\w])[-–—]\s*(?=[\d₹$€£¥])", t_clean)) or \
        bool(re.search(r"\d\s*[-–—]\s*$", t_clean))

    european = european_figure(t_clean.strip(" -–—+"))
    if european is not None:
        value = float(european)
        negative = paren or minus
        return (-value if negative else value), negative, paren

    m = NUMBER_RE.search(t_clean.replace(" ", " "))
    if not m:
        return None
    raw = m.group(0)
    # Repair a misread thousands separator before the pattern's truncation can
    # turn it into a plausible wrong answer. Tried against the whole token,
    # because NUMBER_RE stops at the second stop and would hide the evidence.
    repaired = repair_misread_separators(re.sub(r"[^\d.]", "", t_clean.strip()))
    if repaired is None:
        repaired = repair_decimal_comma(t_clean.strip(" ()"))
    if repaired is not None:
        raw = repaired
    raw = re.sub(r"[,\s ]", "", raw)
    try:
        value = float(raw)
    except ValueError:
        return None
    negative = paren or minus
    return (-value if negative else value), negative, paren


def describe_number(text: str) -> dict:
    """How a printed figure was read, as evidence a reviewer can check.

    grouping   western "1,234,567" | indian "12,34,567" | stop "1.234.567" |
               space "1 234 567" | none
    decimal    point | comma | none
    sign       parentheses | leading minus | trailing minus | none
    repairs    what was mapped or corrected before parsing: fullwidth
               characters, stops read for commas ("1.053.69"), a decimal
               comma ("62005,98"), a European figure ("1.234,56")
    """
    raw = text or ""
    t = normalise_separators(raw).strip()
    repairs = []
    if t != raw.strip():
        repairs.append("recogniser variants mapped (fullwidth, apostrophe)")
    body = t.strip("()-–— ").rstrip("%").strip()
    body = re.sub(r"^[₹$€£¥]\s*", "", body)
    body = re.sub(r"\s*(?:bps|bp|pp|%)$", "", body, flags=re.I)
    decimal = "none"
    grouping = "none"
    if european_figure(body) is not None:
        decimal = "comma"
        grouping = "stop" if "." in body else "space"
        repairs.append("european figure (grouped, decimal comma)")
    elif repair_misread_separators(re.sub(r"[^\d.]", "", body)) is not None:
        decimal = "point"
        grouping = "western"
        repairs.append("stops read as thousands separators")
    elif repair_decimal_comma(body) is not None:
        decimal = "comma"
        repairs.append("decimal comma")
    else:
        whole, _, frac = body.partition(".")
        if frac:
            decimal = "point"
        groups = re.split(r"[,\xa0 ]", whole)
        if len(groups) > 1:
            tail = groups[1:]
            if all(len(g) == 3 for g in tail):
                grouping = "space" if re.search(r"[\xa0 ]", whole) else "western"
            elif len(tail) >= 2 and all(len(g) == 2 for g in tail[:-1]) and len(tail[-1]) == 3:
                grouping = "indian"
    sign = "none"
    if "(" in t and ")" in t:
        sign = "parentheses"
    elif re.match(r"^[-–—]\s*[\d₹$€£¥]", t):
        sign = "leading minus"
    elif re.search(r"\d\s*[-–—]$", t):
        sign = "trailing minus"
    return {"grouping": grouping, "decimal": decimal, "sign": sign,
            "repairs": repairs}


def detect_magnitude(text: str) -> MagnitudeUnit | None:
    for pattern, unit in _MAGNITUDE_WORDS:
        if pattern.search(text or ""):
            return unit
    return None


def detect_currency(text: str) -> tuple[Currency | None, str | None]:
    """Return ``(currency, symbol_seen)``.  Symbols win over words."""
    t = text or ""
    for sym, cur in CURRENCY_SYMBOLS.items():
        if sym in t:
            return cur, sym
    for pattern, cur in _CURRENCY_WORDS:
        if pattern.search(t):
            return cur, None
    return None, None


def currencies_in(text: str) -> set[Currency]:
    """Every distinct currency the text mentions, by symbol or by word."""
    t = text or ""
    found: set[Currency] = set()
    for sym, cur in CURRENCY_SYMBOLS.items():
        if sym in t:
            found.add(cur)
    for pattern, cur in _CURRENCY_WORDS:
        if pattern.search(t):
            found.add(cur)
    return found


def unambiguous_currency(text: str) -> Currency | None:
    """The currency of a text, only when the text names exactly one.

    A caption such as "Revenue at 201,803 crore ($ 21.3 billion)" names two:
    a rupee figure and its dollar equivalent.  Choosing either would be a
    guess, so this returns None and lets a more specific source decide.
    """
    found = currencies_in(text)
    return next(iter(found)) if len(found) == 1 else None


def states_rate(text: str) -> bool:
    """Whether the text itself says the figure is a rate.

    A percent sign, the word "percent", or a basis-point marker -- written in
    the cell, in the row label, in a unit-of-measure cell or in the column
    head.  This is the document speaking, as opposed to the taxonomy assuming,
    and the two must not be weighed equally.
    """
    t = text or ""
    return bool(_PERCENT_RE.search(t) or _BPS_RE.search(t))


#: Fractions of a currency unit, which a statement names when a per-share
#: figure is too small to print in the table's declared magnitude.
_SUB_UNIT_RE = re.compile(r"\bcents?\b|\bpence\b|\bpaise?\b|\bpaisa\b", re.I)


def states_sub_unit(text: str) -> bool:
    """Whether the text says the figure is in fractions of a currency unit.

    A statement headed "$million" still prints earnings per share in cents,
    and says so in the row label. Reading 151.6 there as 151.6 million dollars
    is wrong by eight orders of magnitude, and it is wrong *confidently* --
    nothing downstream can tell that the table's declaration was never meant
    to reach that row.

    There is no magnitude for a cent, so the honest outcome is to drop the
    table's declaration for the row and report the figure exactly as printed,
    with the label's own "(cents)" left to say what it is.
    """
    return bool(_SUB_UNIT_RE.search(text or ""))


def detect_value_type(text: str) -> ValueType:
    t = text or ""
    if _BPS_RE.search(t):
        return ValueType.BPS
    if _PERCENT_RE.search(t):
        return ValueType.PERCENTAGE
    if re.search(r"\bper\s+share\b|\beps\b", t, re.I):
        return ValueType.PER_SHARE
    cur, sym = detect_currency(t)
    if cur or sym or detect_magnitude(t):
        return ValueType.MONETARY
    return ValueType.UNKNOWN


def normalize_number(text: str, *, context: str = "",
                     default_currency: Currency | None = None,
                     default_magnitude: MagnitudeUnit | None = None,
                     currency_glyph: str | None = None,
                     expected_type: ValueType | None = None,
                     default_currency_inferred: bool = False,
                     ) -> NormalizedNumber | None:
    """Parse a token into a fully-described number.

    ``context`` is the surrounding text (table title, unit declaration, column
    header) and is used **only** to inherit a currency or magnitude the token
    itself omits.  It is deliberately *not* consulted for the value *type*: a
    nearby sentence mentioning "bps" or "%" must never re-type an unrelated
    figure.  When the caller knows the expected type from the taxonomy it passes
    ``expected_type``, which applies only if the token itself is silent.
    """
    parsed = parse_number(text)
    if parsed is None:
        return None
    value, is_neg, from_paren = parsed
    token = str(text)

    vtype = detect_value_type(token)
    #: whether the *token* said "rate", as opposed to the caller expecting one
    observed_rate = vtype in (ValueType.PERCENTAGE, ValueType.BPS)
    if vtype == ValueType.UNKNOWN and expected_type is not None:
        vtype = expected_type

    magnitude = detect_magnitude(token) or detect_magnitude(context)
    currency, symbol = detect_currency(token)
    if currency is None and currency_glyph:
        currency, symbol = CURRENCY_SYMBOLS.get(currency_glyph), currency_glyph
    if currency is None:
        currency, symbol2 = detect_currency(context)
        symbol = symbol or symbol2

    # An *expected* rate is the caller's prior about a metric name; an observed
    # one is the document's own mark.  Only the latter may erase a magnitude the
    # caller supplied from an explicit declaration -- and then only for a figure
    # that could actually be a rate.  Without this, a metric the taxonomy calls
    # a margin turns a declared-in-millions 43,718 into "43,718 percent".
    if (not observed_rate and vtype in (ValueType.PERCENTAGE, ValueType.BPS)
            and default_magnitude is not None and abs(value) > RATE_CEILING):
        vtype = ValueType.UNKNOWN

    # A magnitude scales a figure; it does not make it money -- only a currency
    # does.  "396 million" is read as monetary purely because "million" is
    # there, so where the caller knows the metric counts things (subscribers,
    # stores, shares) the document's currency would otherwise be attached and a
    # customer base reported in rupees.  A currency written on the token itself
    # still wins, because then the document really did say money.
    if (expected_type == ValueType.COUNT and vtype == ValueType.MONETARY
            and detect_currency(token)[0] is None):
        vtype = ValueType.COUNT
        currency, symbol = None, None

    took_default_currency = False
    if vtype in (ValueType.PERCENTAGE, ValueType.BPS):
        # a percentage has no currency and no magnitude multiplier
        magnitude = (MagnitudeUnit.PERCENT if vtype == ValueType.PERCENTAGE
                     else MagnitudeUnit.BPS)
        currency = Currency.UNKNOWN
        symbol = None
    else:
        if magnitude is None and default_magnitude is not None:
            magnitude = default_magnitude
        if (currency is None and default_currency is not None
                and vtype != ValueType.COUNT):
            currency = default_currency
            took_default_currency = True
        if magnitude is not None and vtype == ValueType.UNKNOWN:
            vtype = ValueType.MONETARY

    magnitude = magnitude or MagnitudeUnit.NONE

    # Currency inferred from an India-specific magnitude word.
    #
    # This matters for visual-only processing: the PP-OCR charset cannot emit
    # the rupee sign, so the symbol is simply absent from a pixel-only reading.
    # But "crore" and "lakh" are read from the pixels like any other word, and
    # they are only ever used for rupee amounts.  The inference therefore comes
    # from the document's own visible text -- not from the PDF text layer -- and
    # it is flagged as *inferred* rather than presented as a symbol that was
    # actually read.
    # A currency supplied by the caller from the DOCUMENT rather than from
    # this figure, its row or its own table is a reading of the document, not
    # of the figure, and says so.
    inferred = default_currency_inferred and took_default_currency
    if currency is None and magnitude in INDIAN_MAGNITUDES:
        currency = Currency.INR
        inferred = True
    elif (magnitude in INDIAN_MAGNITUDES and currency not in (None, Currency.INR)
          and not (symbol and symbol in token)):
        # The magnitude says rupees but a different currency was picked up from
        # the surrounding text rather than from this number itself.  Indian
        # filings quote a rupee figure with its dollar equivalent alongside --
        # "201,803 crore ($ 21.3 billion)" -- and with the rupee glyph often
        # missing from extraction the stray "$" is the only symbol present.
        # "Crore" and "lakh" are used only for rupees, so the magnitude is the
        # stronger evidence and the borrowed symbol is discarded.
        currency = Currency.INR
        symbol = None
        inferred = True

    if vtype == ValueType.COUNT:
        # A count of things has no currency, whatever scale it is quoted at.
        # "861.47 Cr" against "Total No. of shares" is eight and a half billion
        # shares; the crore-implies-rupees inference above is right for money
        # and nonsense here, and it rendered the share count as a rupee amount.
        currency = Currency.UNKNOWN
        symbol = None
        inferred = False

    currency = currency or Currency.UNKNOWN

    base = None
    factor = MAGNITUDE_FACTORS.get(magnitude)
    if factor is not None:
        base = value * factor

    confidence = 1.0
    if magnitude == MagnitudeUnit.NONE and vtype == ValueType.MONETARY:
        confidence -= 0.15
    if currency == Currency.UNKNOWN and vtype == ValueType.MONETARY:
        confidence -= 0.10
    if inferred:
        confidence -= 0.05

    return NormalizedNumber(
        value=value, raw_text=token.strip(), is_negative=is_neg,
        negative_from_parentheses=from_paren, value_type=vtype,
        magnitude=magnitude, currency=currency, currency_symbol=symbol,
        currency_inferred=inferred,
        base_value=base, confidence=round(max(0.3, confidence), 3))


def to_base(value: float, magnitude: MagnitudeUnit) -> float | None:
    factor = MAGNITUDE_FACTORS.get(magnitude)
    return None if factor is None else value * factor


def convert(value: float, frm: MagnitudeUnit, to: MagnitudeUnit) -> float | None:
    """Convert between magnitudes.  Returns None if either side is not scalar."""
    f_from = MAGNITUDE_FACTORS.get(frm)
    f_to = MAGNITUDE_FACTORS.get(to)
    if f_from is None or f_to is None or f_to == 0:
        return None
    return value * f_from / f_to


def comparable(a: NormalizedNumber | None, b: NormalizedNumber | None) -> bool:
    """True if two numbers can be arithmetically compared without assumptions."""
    if a is None or b is None:
        return False
    if a.value_type != b.value_type:
        return False
    if a.value_type in (ValueType.PERCENTAGE, ValueType.BPS):
        return True
    if a.currency != b.currency and Currency.UNKNOWN not in (a.currency, b.currency):
        return False
    return a.base_value is not None and b.base_value is not None


#: Words that introduce an exception to a unit declaration.  What follows one
#: describes the *excluded* item, so its magnitude is not the statement's.
_CARVE_OUT_RE = re.compile(r"\b(?:except|excepting|excluding|other\s+than)\b",
                           re.I)


def _after_carve_out(text: str, position: int) -> bool:
    """Whether ``position`` falls inside an exception clause.

    Scope is the enclosing parenthetical where there is one, because that is
    how these declarations are written; failing that, the current line.  Only
    what precedes the position within that scope is examined -- a carve-out
    later in the sentence says nothing about a declaration that came first.
    """
    start = max(text.rfind("(", 0, position),
                text.rfind("\n", 0, position)) + 1
    return bool(_CARVE_OUT_RE.search(text[start:position]))


#: A declaration introduced by words -- "in", "figures in", "amounts in" --
#: rather than by a bare symbol or currency code.
_WORDED_DECLARATION = re.compile(r"^\(?\s*(?:figures?\s+|amounts?\s+)?in\b", re.I)


def _document_declarations(texts: Iterable[str]) -> tuple[dict, dict]:
    """Count the currency and magnitude declarations a document makes."""
    cur_counts: dict[Currency, int] = {}
    mag_counts: dict[MagnitudeUnit, int] = {}
    # A currency *symbol* is not a word character, so it can carry neither a
    # leading nor a trailing \b -- "(₹ in crore)" would never match.
    # Symbols are matched separately from the words for that reason.
    #
    # The words need a boundary on *both* sides. Without a leading one, "rs"
    # matches inside "customers", "Numbers" and "quarters", and a deck
    # reporting in US dollars that happens to mention its customers is read
    # as reporting in rupees: on the presentation this was found in, ordinary
    # prose cast 80 phantom INR votes against 38 genuine USD ones, and every
    # figure that fell back to the document's currency was then wrong by an
    # exchange rate.
    decl = re.compile(
        r"\(?\s*(?:\b(?:in|figures?\s+in|amounts?\s+in|rs\.?|inr|usd)\b|[₹$€£¥])"
        r"[^)\n]{0,40}\)?", re.I)
    for text in texts:
        if not text:
            continue
        for m in decl.finditer(text):
            if _after_carve_out(text, m.start()):
                continue
            frag = m.group(0)
            u = detect_magnitude(frag)
            if u:
                mag_counts[u] = mag_counts.get(u, 0) + 1
            # A declaration states a unit; it never states an amount. "$480
            # million" in a sentence about a bond issue, "Rs. 11,402 million"
            # in a note, "$ 43 million" in a subsidiary's disclosure -- each is
            # a figure, and each was counted as though the document had
            # declared its currency. On a rupee filing whose own declaration
            # had lost its glyph to OCR, three such figures made every
            # statement dollars.
            if any(ch.isdigit() for ch in frag):
                continue
            # A symbol or a currency code declares a currency only together
            # with the magnitude it qualifies -- "(₹ crore)", "US$ million".
            # Standing alone, "$" is as often a footnote marker ("$ Passive
            # infrastructure services represent...") and "Rs." a stray column
            # head; a telecom filing's footnote markers voted dollars against
            # its rupee statements. A worded declaration ("Amount in ₹") needs
            # no magnitude: the words say what it is.
            if not u and not _WORDED_DECLARATION.match(frag):
                continue
            # A fragment naming two currencies ("USD per EUR") declares neither.
            c = unambiguous_currency(frag)
            if c:
                cur_counts[c] = cur_counts.get(c, 0) + 1
    return cur_counts, mag_counts


def infer_document_units(texts: Iterable[str]) -> tuple[Currency | None, MagnitudeUnit | None]:
    """Infer the document's dominant currency/magnitude from unit declarations.

    Looks for the parenthetical unit statements that financial reports put above
    tables, e.g. "(in Rs crore)".  Returns the most frequent finding, or None.

    A declaration standing after a carve-out -- "(In millions, **except**
    number of shares, which are reflected in thousands)" -- states the unit of
    the thing being excluded, not the unit of the statement.  Counting it as
    though it were the document's own unit is how a filing reported in millions
    comes to be read in thousands, and OCR makes that likelier rather than
    less: lose the leading "In" to a misread glyph and the carve-out is the
    only declaration left standing.
    """
    cur_counts, mag_counts = _document_declarations(texts)
    # The document has a currency only if everything it declares agrees. A
    # filing that declares rupees for its statements and dollars for a
    # translation column has two, and a table that lost its own glyph cannot
    # be told which of them applies by counting.
    cur = next(iter(cur_counts)) if len(cur_counts) == 1 else None
    mag = max(mag_counts, key=lambda k: mag_counts[k]) if mag_counts else None
    return cur, mag
