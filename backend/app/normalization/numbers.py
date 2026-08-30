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
    (re.compile(r"\bthousands?\b|\b000s?\b|\bk\b", re.I), MagnitudeUnit.THOUSAND),
]

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
]

# 1,23,456.78 (Indian grouping) and 123,456.78 (Western) both parse.
_NUMBER_CORE = r"\d{1,3}(?:[, \s]\d{2,3})*(?:\.\d+)?|\d+(?:\.\d+)?"
NUMBER_RE = re.compile(_NUMBER_CORE)
# A numeric cell may carry a unit suffix *inside* the parentheses that mark it
# negative -- "(210 bps)" is how a margin decline is printed -- so the closing
# bracket has to be allowed both before and after the suffix.
_STRICT_NUMBER_RE = re.compile(rf"^[\s]*[-+(]?\s*[₹$€£¥]?\s*({_NUMBER_CORE})\s*\)?"
                               r"\s*(%|bps|bp|pp)?\s*\)?\s*[-+]?\s*$", re.I)
_NIL_RE = re.compile(r"^\s*[-–—]\s*$")
_PERCENT_RE = re.compile(r"%|\bper\s*cent\b|\bpercent(age)?\b", re.I)
_BPS_RE = re.compile(r"\bbps\b|\bbasis points?\b|\bbp\b", re.I)


def looks_numeric(text: str) -> bool:
    """True if the token reads as a numeric table cell.

    Deliberately permissive about surrounding currency/percent decoration but
    strict about the token being *predominantly* a number, so that a sentence
    containing a figure does not register as a numeric cell.
    """
    t = (text or "").strip()
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


def parse_number(text: str) -> tuple[float, bool, bool] | None:
    """Parse a numeric token.

    Returns ``(value, is_negative, negative_from_parentheses)`` or ``None``.
    """
    if text is None:
        return None
    t = str(text).strip()
    if not t:
        return None
    if _NIL_RE.match(t):
        return 0.0, False, False

    paren = ("(" in t and ")" in t)
    t_clean = t.replace("(", " ").replace(")", " ")
    minus = bool(re.search(r"(?<![\w])[-–—]\s*(?=[\d₹$€£¥])", t_clean)) or \
        bool(re.search(r"\d\s*[-–—]\s*$", t_clean))

    m = NUMBER_RE.search(t_clean.replace(" ", " "))
    if not m:
        return None
    raw = m.group(0)
    raw = re.sub(r"[,\s ]", "", raw)
    try:
        value = float(raw)
    except ValueError:
        return None
    negative = paren or minus
    return (-value if negative else value), negative, paren


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
                     expected_type: ValueType | None = None) -> NormalizedNumber | None:
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
    if vtype == ValueType.UNKNOWN and expected_type is not None:
        vtype = expected_type

    magnitude = detect_magnitude(token) or detect_magnitude(context)
    currency, symbol = detect_currency(token)
    if currency is None and currency_glyph:
        currency, symbol = CURRENCY_SYMBOLS.get(currency_glyph), currency_glyph
    if currency is None:
        currency, symbol2 = detect_currency(context)
        symbol = symbol or symbol2

    if vtype in (ValueType.PERCENTAGE, ValueType.BPS):
        # a percentage has no currency and no magnitude multiplier
        magnitude = (MagnitudeUnit.PERCENT if vtype == ValueType.PERCENTAGE
                     else MagnitudeUnit.BPS)
        currency = Currency.UNKNOWN
        symbol = None
    else:
        if magnitude is None and default_magnitude is not None:
            magnitude = default_magnitude
        if currency is None and default_currency is not None:
            currency = default_currency
        if magnitude is not None and vtype == ValueType.UNKNOWN:
            vtype = ValueType.MONETARY

    magnitude = magnitude or MagnitudeUnit.NONE
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

    return NormalizedNumber(
        value=value, raw_text=token.strip(), is_negative=is_neg,
        negative_from_parentheses=from_paren, value_type=vtype,
        magnitude=magnitude, currency=currency, currency_symbol=symbol,
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


def infer_document_units(texts: Iterable[str]) -> tuple[Currency | None, MagnitudeUnit | None]:
    """Infer the document's dominant currency/magnitude from unit declarations.

    Looks for the parenthetical unit statements that financial reports put above
    tables, e.g. "(in Rs crore)".  Returns the most frequent finding, or None.
    """
    cur_counts: dict[Currency, int] = {}
    mag_counts: dict[MagnitudeUnit, int] = {}
    # A currency *symbol* is not a word character, so it cannot be followed by
    # \b -- "(₹ in crore)" would never match. Symbols are matched separately.
    decl = re.compile(
        r"\(?\s*(?:(?:in|figures?\s+in|amounts?\s+in|rs\.?|inr|usd)\b|[₹$€£¥])"
        r"[^)\n]{0,40}\)?", re.I)
    for text in texts:
        if not text:
            continue
        for m in decl.finditer(text):
            frag = m.group(0)
            c, _ = detect_currency(frag)
            u = detect_magnitude(frag)
            if c:
                cur_counts[c] = cur_counts.get(c, 0) + 1
            if u:
                mag_counts[u] = mag_counts.get(u, 0) + 1
    cur = max(cur_counts, key=lambda k: cur_counts[k]) if cur_counts else None
    mag = max(mag_counts, key=lambda k: mag_counts[k]) if mag_counts else None
    return cur, mag
