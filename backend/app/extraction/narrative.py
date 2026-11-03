"""Extraction from prose: narrative facts, commentary and forward-looking guidance.

Financial releases state many figures only in sentences ("Revenue increased
24.5% Y-o-Y to Rs 340,257 crore"), and the surrounding commentary carries the
management view that the tables cannot express.  This module handles three
distinct jobs and keeps their outputs separate:

* **narrative facts** - a metric, a value and (where stated) a growth figure,
  recovered from a sentence with the same provenance discipline as a table cell;
* **commentary** - topic-classified narrative passages, with the metrics they
  mention linked;
* **guidance** - forward-looking statements, which are *never* mixed into the
  historical fact set.  Separating them is a correctness requirement: a target
  is not a result.

Everything is rule-based and deterministic.  An optional local LLM can add a
summary to a commentary passage, but it can never introduce a fact.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from typing import TYPE_CHECKING

from app.core.ids import stable_id
from app.core.logging import get_logger
from app.extraction import confidence as conf_mod
from app.extraction.taxonomy import match_metric
from app.models.enums import (
    CommentaryTopic, Currency, DerivationStatus, EntityType, FactKind, GrowthType, MagnitudeUnit, StatementNature, ValueType,
)
from app.models.schemas import (
    BBoxModel, Commentary, FinancialFact, Guidance, Provenance, Region,
)
from app.normalization.numbers import normalize_number
from app.normalization.periods import (
    FiscalCalendar, date_like_spans, find_all_periods, parse_period,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from app.document.entities import EntityRegistry

log = get_logger("extraction.narrative")

# ------------------------------------------------------------------ patterns
#: A sentence ends at a stop before a capital -- but not the stop of "Rs.",
#: "No." or "Ltd.": "total revenues of Rs. Nil" is one sentence (Phase 22).
SENTENCE_SPLIT = re.compile(
    r"(?<!\bRs\.)(?<!\bNo\.)(?<!\bMr\.)(?<!\bMs\.)(?<!\bDr\.)(?<!\bLtd\.)(?<!\bCo\.)"
    r"(?<!\bInc\.)(?<!\bvs\.)(?<=[.!?])\s+(?=[A-Z(])|\n+")

_NUM = r"\(?-?[₹$€£¥]?\s?\d[\d,\. ]*\)?"
_UNIT = r"(?:crores?|cr\.?|lakhs?|millions?|mn|billions?|bn|thousand|%|bps)?"

# "Revenue increased 24.5% Y-o-Y to Rs 340,257 crore"
# "EBITDA at Rs 54,067 crore, up 10.1% Y-o-Y"
VALUE_PHRASE = re.compile(
    rf"(?P<value>{_NUM})\s*(?P<unit>{_UNIT})", re.I)

GROWTH_PHRASE = re.compile(
    r"(?P<dir>up|down|higher|lower|increased?|decreased?|declined?|grew|rose|fell|"
    r"improved?|reduced?)\s+(?:by\s+)?(?P<pct>\d+(?:\.\d+)?)\s*(?P<kind>%|per\s*cent|bps)"
    r"(?:[^.]{0,40}?(?P<basis>y-?o-?y|yoy|q-?o-?q|qoq|year[- ]on[- ]year|"
    r"sequentially|year[- ]over[- ]year))?", re.I)

_DECREASE_WORDS = re.compile(
    r"^(down|lower|decreased?|declined?|fell|reduced?)$", re.I)

GUIDANCE_TRIGGERS = re.compile(
    r"\b(expects?|expecting|anticipates?|targets?|targeting|guidance|guide[sd]?|"
    r"outlook|forecasts?|projects?|plans? to|aims? to|intends? to|"
    r"on track to|will (?:be|reach|deliver|invest|commission)|"
    r"is likely to|estimated to|going forward|in the coming (?:quarters?|years?)|"
    r"over the next|by (?:FY|20)\d{2})\b", re.I)

FORWARD_DISCLAIMER = re.compile(
    r"forward[- ]looking statements?|safe harbou?r|may differ materially", re.I)

QUOTE_RE = re.compile(r"[\"“”']{1}(?P<quote>[^\"“”]{40,})[\"“”']{1}")
SPEAKER_RE = re.compile(
    r"(?:said|commenting|stated|according to|remarked)[,:]?\s+"
    r"(?:Mr\.?|Ms\.?|Mrs\.?|Dr\.?)?\s*(?P<name>[A-Z][A-Za-z.\-]+(?:\s+[A-Z][A-Za-z.\-]+){0,3})"
    r"(?:\s*,\s*(?P<role>[^.\n]{4,80}))?", re.I)

TOPIC_KEYWORDS: dict[CommentaryTopic, tuple[str, ...]] = {
    CommentaryTopic.REVENUE: ("revenue", "sales", "turnover", "topline", "top line"),
    CommentaryTopic.PROFITABILITY: ("profit", "pat", "ebitda", "earnings", "bottom line",
                                    "pbt", "net income"),
    CommentaryTopic.MARGINS: ("margin", "spread", "realisation", "realization", "bps"),
    CommentaryTopic.DEMAND: ("demand", "volume", "consumption", "footfall", "traffic",
                             "subscriber", "customer"),
    CommentaryTopic.COSTS: ("cost", "expense", "opex", "input price", "raw material"),
    CommentaryTopic.CAPEX: ("capex", "capital expenditure", "investment", "commissioned",
                            "expansion", "capacity"),
    CommentaryTopic.DEBT: ("debt", "borrowing", "leverage", "net debt", "deleverag",
                           "finance cost", "interest"),
    # Generic organisational vocabulary only.  A particular filer's coinage for
    # its own segment (an abbreviation such as "O2C") must never appear here:
    # it would classify one company's commentary correctly and every other
    # company's not at all.  Segment *names* are discovered per document by the
    # entity registry, which is where company-specific vocabulary belongs.
    CommentaryTopic.SEGMENTS: ("segment", "business", "vertical", "division",
                               "platforms", "retail", "unit", "arm",
                               "portfolio", "subsidiary"),
    CommentaryTopic.OPERATIONS: ("operation", "utilisation", "utilization", "throughput",
                                 "production", "store", "network", "plant"),
    CommentaryTopic.RISKS: ("risk", "headwind", "volatility", "uncertain", "pressure",
                            "challenge", "adverse"),
    CommentaryTopic.OUTLOOK: ("outlook", "going forward", "expect", "anticipate",
                              "coming quarter", "coming year"),
    CommentaryTopic.GUIDANCE: ("guidance", "target", "plan to", "on track"),
    CommentaryTopic.STRATEGY: ("strategy", "strategic", "initiative", "partnership",
                               "acquisition", "launch", "transformation"),
    CommentaryTopic.SUSTAINABILITY: ("sustainab", "renewable", "carbon", "green",
                                     "esg", "net zero"),
}

_POSITIVE = re.compile(
    r"\b(record|strong|robust|healthy|growth|grew|improved?|higher|increased?|"
    r"expand|resilient|momentum|best|highest|outperform)\w*\b", re.I)
_NEGATIVE = re.compile(
    r"\b(decline[d]?|lower|weak|softness|pressure|headwind|challeng\w+|"
    r"contraction|fell|deteriorat\w+|impacted|adverse|loss)\b", re.I)


def split_sentences(text: str) -> list[str]:
    parts = [p.strip() for p in SENTENCE_SPLIT.split(text or "") if p and p.strip()]
    return [p for p in parts if len(p) > 2]


def classify_topics(text: str) -> tuple[CommentaryTopic, list[CommentaryTopic]]:
    low = (text or "").lower()
    scores: dict[CommentaryTopic, int] = {}
    for topic, kws in TOPIC_KEYWORDS.items():
        n = sum(low.count(k) for k in kws)
        if n:
            scores[topic] = n
    if not scores:
        return CommentaryTopic.OTHER, []
    ordered = sorted(scores, key=lambda t: scores[t], reverse=True)
    return ordered[0], ordered[:4]


def classify_sentiment(text: str) -> str:
    pos = len(_POSITIVE.findall(text or ""))
    neg = len(_NEGATIVE.findall(text or ""))
    if pos and neg:
        return "mixed" if abs(pos - neg) <= 1 else ("positive" if pos > neg else "negative")
    if pos:
        return "positive"
    if neg:
        return "negative"
    return "neutral"


def is_forward_looking(text: str) -> bool:
    if FORWARD_DISCLAIMER.search(text or ""):
        return False  # the boilerplate disclaimer is not itself guidance
    return bool(GUIDANCE_TRIGGERS.search(text or ""))


# ------------------------------------------------------------ narrative facts
@dataclass(slots=True)
class NarrativeContext:
    document_id: str
    entity: str | None
    calendar: FiscalCalendar
    default_currency: Currency | None
    default_unit: MagnitudeUnit | None
    default_period: str | None = None
    page_image_url: str | None = None
    known_entities: tuple[str, ...] = ()
    """Entity names seen elsewhere in this document, so a sentence that names
    one ("Robust O2C performance with EBITDA at ...") is attributed to it rather
    than to whichever section happens to enclose the sentence."""
    entity_id: str | None = None
    entity_type: EntityType = EntityType.UNKNOWN
    parent_entity_id: str | None = None
    registry: "EntityRegistry | None" = None
    """The document's entity registry.  When present it resolves a named
    mention to a full entity record -- id, type and parent -- so a narrative
    override changes the whole attribution, not just the display name."""


@dataclass(slots=True)
class _Attribution:
    name: str | None
    entity_id: str | None
    entity_type: EntityType
    parent_entity_id: str | None
    overridden: bool = False


def _attribute(sentence: str, ctx: "NarrativeContext",
               anchor: int | None = None) -> _Attribution:
    """Resolve which entity a sentence is about.

    A sentence that explicitly names an entity the document introduced wins
    over the section default.  ``anchor`` is the offset of the metric label, so
    that when several units are named the nearest one to the figure wins rather
    than the alphabetically or lexically luckiest.  Nothing is invented: only
    registered entities can be matched.
    """
    if ctx.registry is not None:
        ref = ctx.registry.find_in_text(sentence, anchor=anchor)
        if ref is not None and ref.entity_id != ctx.entity_id:
            return _Attribution(ref.name, ref.entity_id, ref.entity_type,
                                ref.parent_id, overridden=True)
        return _Attribution(ctx.entity, ctx.entity_id, ctx.entity_type,
                            ctx.parent_entity_id)
    named = _entity_named_in(sentence, ctx.known_entities, ctx.entity)
    return _Attribution(named, ctx.entity_id if named == ctx.entity else None,
                        ctx.entity_type if named == ctx.entity
                        else EntityType.UNKNOWN,
                        ctx.parent_entity_id if named == ctx.entity else None,
                        overridden=named != ctx.entity)


def _entity_named_in(sentence: str, known: tuple[str, ...],
                     fallback: str | None) -> str | None:
    """Attribute a sentence to an entity it explicitly names, if any.

    Only whole-word matches count, and only for entities the document itself
    introduced -- this never invents an entity, it just prefers the one the
    sentence is actually about.
    """
    best: tuple[int, str] | None = None
    for name in known:
        token = name.strip()
        if len(token) < 2:
            continue
        pattern = re.compile(rf"(?<![\w]){re.escape(token)}(?![\w])",
                             re.I if len(token) > 4 else 0)
        if pattern.search(sentence):
            # prefer the most specific (longest) name mentioned
            if best is None or len(token) > best[0]:
                best = (len(token), name)
    return best[1] if best else fallback


def _leading_metric(sentence: str) -> tuple[str, object] | None:
    """Find the metric a sentence is about, preferring the earliest mention.

    Scans phrases from the start of the sentence rather than the whole string so
    that "Revenue ... driven by EBITDA growth" is attributed to revenue.
    """
    words = sentence.split()
    best = None
    for start in range(min(len(words), 8)):
        for span in (5, 4, 3, 2, 1):
            if start + span > len(words):
                continue
            phrase = " ".join(words[start:start + span]).strip(" ,;:()")
            if len(phrase) < 3:
                continue
            m = match_metric(phrase, threshold=0.86)
            if m is not None and (best is None or m.score > best[1].score):
                best = (phrase, m)
        if best is not None:
            break
    return best


#: A scale or currency word standing near a bare number is what makes the
#: number a financial figure rather than a count.
#:
#: The word boundaries are the whole point. Without a leading one, "rs" matches
#: inside "quarters", "customers" and "numbers", and the phrase
#: "GNPA trend analysis (5 quarters)" then supplied both a figure and its
#: licence: the fragment "(5" was recorded as a gross NPA ratio of 5%, at
#: medium confidence, with nothing flagged. Currency symbols are matched
#: separately because a symbol is not a word character and can carry no
#: boundary at all.
_SCALE_NEARBY = re.compile(
    r"\b(?:crore|lakh|million|billion|rs\b\.?)|[₹$€£¥]", re.I)

# ---- which figure in a sentence is the metric's own (Phase 22) -------------
# A human audit of every prose figure in the corpus found about half of them
# wrong, none flagged: the 31 of "December 31, 2025" filed as net worth, 8.8
# basis points as 8.8 crore of revenue, the prior year's figure after "from",
# the change after "grew by", a dollar conversion's "$" giving a rupee figure
# a dollar currency. Each rule below refuses one of those readings; none of
# them names a company or a document.

#: A currency marker standing immediately before a figure.
_CURRENCY_TAIL = re.compile(r"(?:\brs\.?|\binr|\busd|\beur|\bgbp|\bchf|\bjpy|[₹$€£¥])\s*$", re.I)
#: The same, plus the "R" OCR makes of a rupee glyph -- used only to see
#: past it to the word before, never to give a figure a currency.
_MARKER_TAIL = re.compile(r"(?:\brs\.?|\binr|\busd|\beur|\bgbp|\bchf|\bjpy|[₹$€£¥]|\bR)\s*$", re.I)
_CURRENCY_WORD = re.compile(r"(?:rs\.?|inr|usd|eur|gbp|chf|jpy|[₹$€£¥]|R)", re.I)
#: The figure is the earlier side of a comparison: "from X", "as against X".
_COMPARATIVE_TAIL = re.compile(
    r"(?:\bfrom|\bagainst|\bcompared\s+(?:to|with)|\bvs\.?|\bversus|\bover|"
    r"\bprevious(?:ly)?|\blast\s+year'?s?|\bearlier)\s*$", re.I)
#: The figure is a change, not a level: "grew by X", "an increase of X".
_CHANGE_TAIL = re.compile(
    r"(?:\b(?:grew|grown|increased?|decreased?|declined?|rose|risen|fell|fallen|up|down|"
    r"higher|lower|improved?|reduced?|added|expanded?|contracted?|jumped|dropped)\b"
    r"(?:\s+\S+){0,2}?\s+by|\b(?:increase|decrease|decline|rise|growth|drop|reduction|"
    r"addition|improvement|change)\s+of)\s*$", re.I)
#: A rate follows the figure: "8.8bps", "12 per cent", "2.5x".
_RATE_AFTER = re.compile(
    r"^\s*(?:%|per\s*cent\b|percent\b|pc\b|bps\b|basis\s+points?\b|x\b|times\b|pp\b|"
    r"ppts?\b|percentage\s+points?\b)", re.I)
#: A second scale word after the unit -- "1.21 Lakh Crore" -- a compound
#: scale the number parser does not carry.
_COMPOUND_SCALE = re.compile(r"^\s*(?:crores?\b|cr\b|lakhs?\b|millions?\b|mn\b|billions?\b|bn\b|thousand\b)", re.I)
#: What follows is a part of the measure, not the measure: "profit includes a
#: one-time gain of Rs 722 crore", "revenue of which exports were X". A press
#: release's one-time gain was filed as the quarter's profit after tax.
_COMPONENT_OF = re.compile(
    r"\b(?:includes?|including|included|of\s+which|out\s+of\s+which|comprising|"
    r"consisting\s+of|on\s+account\s+of|towards|net\s+of|after\s+charging)\b", re.I)
#: "Capex per store ... INR 2.5 crore": a rate per something, not the total.
_PER_UNIT = re.compile(r"\bper\s+(?!cent\b)[a-z]+", re.I)
#: The standard auditor's sentence about components' own figures "before
#: consolidation adjustments": those are a subsidiary's numbers, not the
#: group's, and the sentence never says which subsidiary.
_COMPONENT_REPORT = re.compile(
    r"before\s+consolidation\s+adjustments|whose\s+(?:[a-z]+\s+){0,3}financial\s+"
    r"(?:results?|information|statements?)\b", re.I)
#: At most this many words between the metric's name and its figure.
_MAX_GAP_WORDS = 8
#: "Earnings before interest, tax, depreciation and amortization (EBITDA)":
#: the parenthesis defines what the phrase names.
_ABBREVIATION = re.compile(r"\(\s*([A-Za-z][A-Za-z&/.\- ]{1,14}?)\s*\)")
_NO_SCALE_TYPES = (ValueType.PER_SHARE, ValueType.PERCENTAGE, ValueType.RATIO,
                   ValueType.BPS, ValueType.COUNT)


#: At most this many measures are read from one sentence. A highlights block
#: lists four or five; anything beyond that is a paragraph being mined.
_MAX_MENTIONS = 8


def _metric_mentions(sentence: str) -> list[tuple[int, int, str, object]]:
    """Every measure named in a sentence, in the order they are printed.

    A press release prints its results as a list -- "Revenue from operations
    Rs 9,579 Crore  EBITDA ... Rs 1,382 Crore  PBT ... Rs 2,126 Crore" -- and
    a region of such a list has no full stops in it, so it arrives here as one
    sentence. Reading only the first measure threw away every figure after it:
    one filing stated twelve and the pipeline kept two.

    Longest match wins at each position, and a match may not overlap one
    already taken, so "Profit Before Tax" is read once rather than as "Profit"
    and then "Tax".
    """
    words: list[tuple[int, int, str]] = []
    for m in re.finditer(r"\S+", sentence):
        words.append((m.start(), m.end(), m.group(0)))
    out: list[tuple[int, int, str, object]] = []
    i = 0
    while i < len(words) and len(out) < _MAX_MENTIONS:
        best = None
        for span in (5, 4, 3, 2, 1):
            if i + span > len(words):
                continue
            start, end = words[i][0], words[i + span - 1][1]
            phrase = sentence[start:end].strip(" ,;:()-–—")
            if len(phrase) < 3:
                continue
            m = match_metric(phrase, threshold=0.9)
            if m is not None and (best is None or m.score > best[3].score):
                best = (start, end, phrase, m, span)
        if best is None:
            i += 1
            continue
        out.append(best[:4])
        i += best[4]
    return out


def _candidates(sentence: str, label: str, match, label_start: int,
                label_end: int, ctx: "NarrativeContext"):
    """The measures a sentence states, each with the text it owns.

    A sentence usually states one. A results list has no full stop in it, so
    "Revenue from operations Rs 9,579 Crore EBITDA Rs 1,382 Crore PBT Rs 2,126
    Crore" arrives here as a single sentence, and reading only the first
    measure threw the rest of the filing's own highlights away.

    A sentence counts as a list only when two or more of the measures named in
    it each have a figure of their own before the next measure is named.
    Otherwise it is prose that mentions other measures in passing -- "revenue
    grew, driven by EBITDA" -- and only the measure the sentence is about is
    read, exactly as before.
    """
    single = [(label, match, label_start, label_end, None)]
    mentions = _metric_mentions(sentence)
    if len(mentions) < 2:
        return single
    rows: list[tuple] = []
    for i, (start, end, phrase, m) in enumerate(mentions):
        stop = mentions[i + 1][0] if i + 1 < len(mentions) else None
        lab, mm = _defined_abbreviation(sentence, phrase, m)
        if _pick_figure(sentence, end, mm.definition.value_type, ctx, stop) is None:
            continue
        rows.append((lab, mm, start, end, stop))
    if len(rows) < 2:
        return single
    out, seen = [], set()
    for row in rows:                      # a measure named twice is read once
        if row[1].key in seen:
            continue
        seen.add(row[1].key)
        out.append(row)
    return out


def _find_label(sentence: str, label: str) -> tuple[int, int] | None:
    """Where the matched label sits in the sentence, whatever its spacing."""
    words = label.split()
    if not words:
        return None
    m = re.search(r"\s+".join(re.escape(w) for w in words), sentence)
    return (m.start(), m.end()) if m else None


def _defined_abbreviation(sentence: str, label: str, match):
    """Prefer the measure a parenthesised abbreviation right after the label
    defines, when it names a different one than the words matched so far."""
    span = _find_label(sentence, label)
    if span is None:
        return label, match
    tail = sentence[span[1]: span[1] + 80]
    m = _ABBREVIATION.search(tail)
    if m is None:
        return label, match
    between = tail[:m.start()]
    if any(ch.isdigit() for ch in between) or len(between.split()) > 5:
        return label, match
    abbr = match_metric(m.group(1), threshold=0.9)
    if abbr is None or abbr.key == match.key:
        return label, match
    return sentence[span[0]: span[1] + m.end()], abbr


def _pick_figure(sentence: str, label_end: int, expected: ValueType,
                 ctx: "NarrativeContext", stop: int | None = None):
    """The figure a sentence states for its metric, or None.

    Only a figure AFTER the metric's name, within a few words of it, that is
    not part of a date, not a rate, not in a parenthesis (a comparative, a
    conversion, a breakdown), not the earlier side of a comparison and not a
    change. The first such figure is the metric's; if the first is too far
    away, later ones are further still.
    """
    dates = date_like_spans(sentence)
    for m in VALUE_PHRASE.finditer(sentence):
        tok = m.group(0).strip()
        if not any(c.isdigit() for c in tok) or m.start() < label_end:
            continue
        # a figure printed after the next measure's name belongs to that one
        if stop is not None and m.start() >= stop:
            return None
        if any(a < m.end() and m.start() < b for a, b in dates):
            continue
        unit = (m.group("unit") or "").lower()
        after = sentence[m.end(): m.end() + 24]
        if unit in ("%", "bps") or "%" in tok or _RATE_AFTER.match(after):
            continue
        before = sentence[:m.start()]
        gap = sentence[label_end:m.start()]
        words = [w for w in gap.split() if not _CURRENCY_WORD.fullmatch(w.strip(".,:;"))]
        if len(words) > _MAX_GAP_WORDS:
            return None
        if unit and _COMPOUND_SCALE.match(after):
            continue
        if before.count("(") > before.count(")") or tok.startswith("("):
            continue
        head = _MARKER_TAIL.sub("", before)
        if _COMPARATIVE_TAIL.search(head) or _CHANGE_TAIL.search(head):
            continue
        if _PER_UNIT.search(gap) and expected != ValueType.PER_SHARE:
            continue
        if _COMPONENT_OF.search(gap):
            # the figure is a part of the measure, not the measure
            continue
        has_unit = bool(unit) or any(sym in tok for sym in "₹$€£¥")
        if not has_unit and not _CURRENCY_TAIL.search(before):
            continue
        # The currency and scale are read from the figure's own neighbourhood:
        # a marker a few characters away, up to the next parenthesis -- never
        # from the conversion printed beside it ("R244.8 billion ($2,585.9
        # million)" is not a dollar figure).
        tight = before[-8:] + tok + after.split("(")[0][:12]
        rate_like = expected in (ValueType.PERCENTAGE, ValueType.RATIO, ValueType.BPS,
                                 ValueType.COUNT)
        n = normalize_number(
            tok, context=tight,
            default_currency=None if rate_like else ctx.default_currency,
            default_currency_inferred=True,
            default_magnitude=(None if expected in _NO_SCALE_TYPES else ctx.default_unit),
            expected_type=expected if expected != ValueType.UNKNOWN else None)
        if n is None:
            continue
        if (expected in (ValueType.PERCENTAGE, ValueType.RATIO, ValueType.BPS)
                and (n.currency not in (None, Currency.UNKNOWN)
                     or n.magnitude not in (None, MagnitudeUnit.NONE))):
            # An amount can never be a margin or a ratio: the figure beside
            # the name belongs to something else.
            return None
        return n, tok
    return None


def extract_narrative_facts(region: Region, ctx: NarrativeContext,
                            ) -> list[FinancialFact]:
    """Pull metric/value/growth triples out of a prose region."""
    out: list[FinancialFact] = []
    text = (region.text or "").strip()
    if not text or len(text) < 12:
        return out
    if FORWARD_DISCLAIMER.search(text):
        return out
    # The auditor's paragraph about components' own figures: every figure in
    # it is a subsidiary's, and the paragraph never says which one.
    if _COMPONENT_REPORT.search(text):
        return out

    for s_idx, sentence in enumerate(split_sentences(text)):
        if not any(ch.isdigit() for ch in sentence):
            continue
        if _COMPONENT_REPORT.search(sentence):
            continue
        found = _leading_metric(sentence)
        if found is None:
            continue
        label, match = found
        label, match = _defined_abbreviation(sentence, label, match)
        span = _find_label(sentence, label)
        candidates = _candidates(sentence, label, match,
                                 span[0] if span else sentence.find(label),
                                 span[1] if span else 0, ctx)

        periods = find_all_periods(sentence, calendar=ctx.calendar)
        period = periods[0] if periods else (
            parse_period(ctx.default_period, calendar=ctx.calendar)
            if ctx.default_period else None)

        for label, match, label_start, label_end, stop in candidates:
            expected = match.definition.value_type
            # Prefer a figure carrying an explicit unit or currency; a bare number in
            # prose is usually a growth rate or a footnote marker.
            picked = _pick_figure(sentence, label_end, expected, ctx, stop)
            value_num, raw_value = picked if picked else (None, "")

            growth_val: float | None = None
            growth_type: GrowthType | None = None
            gm = (GROWTH_PHRASE.search(sentence, label_end,
                                       stop if stop is not None else len(sentence))
                  if len(candidates) > 1 else GROWTH_PHRASE.search(sentence))
            if gm:
                try:
                    growth_val = float(gm.group("pct"))
                except (TypeError, ValueError):
                    growth_val = None
                if growth_val is not None and _DECREASE_WORDS.match(gm.group("dir") or ""):
                    growth_val = -growth_val
                basis = (gm.group("basis") or "").lower().replace("-", "")
                if "yoy" in basis or "yearon" in basis or "yearover" in basis:
                    growth_type = GrowthType.YOY
                elif "qoq" in basis or "sequential" in basis:
                    growth_type = GrowthType.QOQ
                else:
                    growth_type = GrowthType.UNKNOWN
                if (gm.group("kind") or "").lower() == "bps":
                    growth_val = None  # a bps move is recorded separately, not as growth

            if value_num is None and growth_val is None:
                continue

            attribution = _attribute(sentence, ctx,
                                     anchor=label_start if label_start >= 0 else None)
            fact_id = stable_id("fact", ctx.document_id, region.id, s_idx, match.key)
            prov = Provenance(
                document_id=ctx.document_id, page_number=region.page_number,
                region_id=region.id, bbox=region.bbox,
                original_text=sentence[:500],
                ocr_element_ids=list(region.element_ids),
                page_image_url=ctx.page_image_url,
                extraction_method="narrative_sentence")

            sig = conf_mod.Signals(
                ocr=region.confidence, layout=region.confidence,
                # prose has no grid to validate against, so structural evidence is
                # inherently weaker than a table cell's
                structure=0.55, semantic=match.score,
                notes=["extracted from narrative text"])
            bd = conf_mod.score(sig)

            f = FinancialFact(
                fact_id=fact_id, document_id=ctx.document_id, metric=label,
                canonical_metric=match.key, metric_display=match.definition.display,
                entity=attribution.name, entity_id=attribution.entity_id,
                entity_type=attribution.entity_type,
                parent_entity_id=attribution.parent_entity_id,
                statement=match.definition.statement,
                category=match.definition.category,
                derivation=DerivationStatus.REPORTED,
                kind=FactKind.NARRATIVE,
                nature=StatementNature.ACTUAL,
                value=value_num.value if value_num else None,
                raw_value=raw_value,
                value_type=(value_num.value_type if value_num
                            else ValueType.PERCENTAGE),
                currency=value_num.currency if value_num else Currency.UNKNOWN,
                unit=value_num.magnitude if value_num else MagnitudeUnit.NONE,
                base_value=value_num.base_value if value_num else None,
                period=period.label if period else None, period_normalized=period,
                growth=growth_val, growth_type=growth_type,
                growth_reported=growth_val is not None,
                confidence=bd.overall, confidence_band=bd.band,
                confidence_breakdown=bd, source_page=region.page_number,
                provenance=prov, original_text=sentence[:500],
                context_text=text[:300],
                meta={"sentence_index": s_idx, "region_id": region.id,
                      "entity_overridden": attribution.overridden})
            out.append(f)
    return out


# ------------------------------------------------------------------ commentary
def extract_commentary(region: Region, ctx: NarrativeContext) -> Commentary | None:
    text = (region.text or "").strip()
    if len(text) < 40:
        return None
    if FORWARD_DISCLAIMER.search(text):
        return None

    primary, topics = classify_topics(text)
    quote_m = QUOTE_RE.search(text)
    speaker_m = SPEAKER_RE.search(text)

    mentioned: list[str] = []
    for chunk in re.split(r"[,;.]", text):
        m = match_metric(chunk.strip(), threshold=0.88)
        if m and m.key not in mentioned:
            mentioned.append(m.key)

    return Commentary(
        id=stable_id("comm", ctx.document_id, region.id),
        document_id=ctx.document_id, page_number=region.page_number,
        region_id=region.id, text=text[:4000], topics=topics,
        primary_topic=primary, sentiment=classify_sentiment(text),
        speaker=(speaker_m.group("name").strip() if speaker_m else None),
        is_quote=bool(quote_m),
        confidence=round(min(0.95, region.confidence), 3),
        bbox=region.bbox, mentioned_metrics=mentioned[:12])


# -------------------------------------------------------------------- guidance
_TARGET_PERIOD = re.compile(
    r"\b(?:by|in|for|through|during)\s+(FY\s?\d{2,4}|\d{4}|"
    r"the next \w+ (?:quarters?|years?)|coming (?:quarters?|years?))", re.I)
_DIRECTION = re.compile(
    r"\b(increase|grow|expand|improve|rise|double|higher|up)\b|"
    r"\b(reduce|lower|decline|decrease|down)\b|\b(maintain|sustain|stable|steady)\b",
    re.I)


def extract_guidance(region: Region, ctx: NarrativeContext) -> list[Guidance]:
    """Isolate forward-looking statements from a prose region."""
    out: list[Guidance] = []
    text = (region.text or "").strip()
    if not text or FORWARD_DISCLAIMER.search(text):
        return out

    for idx, sentence in enumerate(split_sentences(text)):
        trigger = GUIDANCE_TRIGGERS.search(sentence)
        if not trigger:
            continue

        found = _leading_metric(sentence)
        metric_key = found[1].key if found else None
        metric_label = found[0] if found else None

        target_value = None
        target_unit = None
        target_currency = None
        for m in VALUE_PHRASE.finditer(sentence):
            tok = m.group(0).strip()
            if not any(c.isdigit() for c in tok):
                continue
            window = sentence[max(0, m.start() - 20): m.end() + 20]
            n = normalize_number(tok, context=window,
                                 default_currency=ctx.default_currency,
                                 default_currency_inferred=True,
                                 default_magnitude=ctx.default_unit)
            if n is not None and (n.magnitude != MagnitudeUnit.NONE
                                  or n.value_type != ValueType.UNKNOWN):
                target_value = n.value
                target_unit = n.magnitude
                target_currency = n.currency
                break

        pm = _TARGET_PERIOD.search(sentence)
        target_period = None
        if pm:
            p = parse_period(pm.group(1), calendar=ctx.calendar)
            target_period = p.label if p else pm.group(1).strip()

        dm = _DIRECTION.search(sentence)
        direction = "unspecified"
        if dm:
            if dm.group(1):
                direction = "increase"
            elif dm.group(2):
                direction = "decrease"
            elif dm.group(3):
                direction = "maintain"

        nature = StatementNature.GUIDANCE
        low = sentence.lower()
        if "target" in low or "aim" in low:
            nature = StatementNature.TARGET
        elif "outlook" in low or "going forward" in low or "expect" in low:
            nature = StatementNature.OUTLOOK

        # Guidance rarely comes with a firm number; that is normal, not a
        # failure -- but a numberless statement is flagged for a human.
        needs_review = target_value is None or metric_key is None

        out.append(Guidance(
            id=stable_id("guid", ctx.document_id, region.id, idx),
            document_id=ctx.document_id, page_number=region.page_number,
            region_id=region.id, text=sentence[:1500], nature=nature,
            canonical_metric=metric_key, metric=metric_label,
            target_value=target_value, target_unit=target_unit,
            target_currency=target_currency, target_period=target_period,
            direction=direction, trigger_phrase=trigger.group(0),
            confidence=round(min(0.9, 0.45 + 0.2 * bool(metric_key)
                                 + 0.2 * (target_value is not None)
                                 + 0.1 * bool(target_period)), 3),
            bbox=region.bbox, requires_review=needs_review))
    return out
