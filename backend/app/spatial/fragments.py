"""What a recognised fragment reads as.

Both spatial readers -- tiles stacked vertically, rows laid out side by side --
have to answer the same four questions of a piece of recognised text before
they can say anything about its arrangement: is this a figure, is it the name
of a measure, is it a period, is it a note about a movement. The answers must
be the same in both, or the two readers would disagree about what they are
looking at and the routing between them would mean nothing.

Nothing here considers position. These are questions about text alone; where
it sits on the page is the readers' business.
"""
from __future__ import annotations

import re

from app.models.schemas import OCRElement
from app.normalization.numbers import looks_numeric, normalize_number
from app.normalization.periods import looks_like_period_header


#: Above this share of letters, the text is a phrase that happens to contain a
#: number rather than a number that happens to carry a unit. "US$ 114 Mn." is
#: 29% letters; "Section 5" is 78%.
_MAX_LETTER_RATIO_FOR_A_VALUE = 0.45


def is_value(element: OCRElement) -> bool:
    """Whether this element reads as the figure a card is about.

    ``looks_numeric`` is not the test here. It is built for table cells, where
    the cell holds the bare number and the unit lives in the column head. A
    tile or a row carries everything inline -- "US$ 114 Mn." -- and fails that check
    while being exactly the figure wanted.

    The test used instead is whether the pipeline's own number normaliser can
    read it, which is the question that actually matters and avoids inventing a
    second opinion about what a number looks like. The letter ratio then keeps
    out phrases that merely contain a digit.

    Periods and movement notes are excluded first. "Q1 FY27" parses as a
    number if asked, and a tile whose figure was its own period label would
    be nonsense.
    """
    text = element.text.strip()
    if not text or is_period(element) or is_change(element):
        return False
    if not any(c.isdigit() for c in text):
        return False
    letters = sum(1 for c in text if c.isalpha())
    if letters / len(text) > _MAX_LETTER_RATIO_FOR_A_VALUE:
        return False
    parsed = normalize_number(text)
    return parsed is not None and parsed.value is not None


def is_change(element: OCRElement) -> bool:
    """A parenthesised movement note, not the figure itself."""
    text = element.text.strip().lower()
    if not text.startswith("("):
        return False
    return any(marker in text for marker in ("yoy", "qoq", "y-o-y", "q-o-q",
                                             "bps", "growth"))


#: A label carrying its own figure is a sentence about a number, not the name
#: of one: "Credit Cost is 1.13%" names nothing that a neighbouring value could
#: be. Prose like this belongs to the narrative extractor.
_LABEL_WITH_ITS_OWN_FIGURE = re.compile(r"\d\s*%|\bis\b\s+[\d(]|\bat\b\s+\d")


def is_label(element: OCRElement) -> bool:
    """Words that could name a metric: mostly alphabetic, not a sentence."""
    text = element.text.strip()
    if len(text) < 3 or looks_numeric(text):
        return False
    letters = sum(1 for c in text if c.isalpha())
    if letters / max(1, len(text)) < 0.5:
        return False
    if _LABEL_WITH_ITS_OWN_FIGURE.search(text):
        return False
    if text[0] in "-–—•*+>":
        # A chart annotation or bullet, not the name of a measure.
        return False
    # A sentence is narrative, and the narrative extractor already reads it.
    return len(text.split()) <= 6 and not text.endswith(".")


def is_period(element: OCRElement) -> bool:
    return looks_like_period_header(element.text.strip())


#: Prose markers. A line opening with one of these is a note about the page,
#: not a heading over it.
_FOOTNOTE_OPENER = re.compile(r"^\s*(\*+|#+|\d{1,2}[.)]|\[\d{1,2}\]|Note\b)", re.I)
#: Beyond this many words a line is a sentence however it is set.
_MAX_WORDS_IN_A_HEADING = 12


def is_page_heading(element: OCRElement) -> bool:
    """Whether page-spanning text is a heading over the page or prose on it.

    The distinction is not cosmetic. A heading's period governs what is set out
    beneath it; a sentence's period belongs to the sentence and to nothing
    else. Treating every wide line as a banner attached a footnote's
    ``H1 FY19`` -- from a remark about a one-time provision -- to a figure that
    was a different year's, on a different metric, in a different panel.

    Three tests, each of which the offending footnote failed:

    * it opened with a footnote marker;
    * it ran to twenty-six words;
    * it contained sentence-ending punctuation.
    """
    text = element.text.strip()
    if not text or _FOOTNOTE_OPENER.match(text):
        return False
    if len(text.split()) > _MAX_WORDS_IN_A_HEADING:
        return False
    # A full stop inside the line means at least one sentence has ended in it.
    # A trailing one is the same evidence. Abbreviations ("Mn.", "Q1.") are why
    # the test is on a stop followed by a space or the end of the line, not on
    # the character alone.
    return not re.search(r"[.!?](\s|$)", text[:-1] + " ")
