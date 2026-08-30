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

from app.core.ids import stable_id
from app.core.logging import get_logger
from app.extraction import confidence as conf_mod
from app.extraction.taxonomy import match_metric
from app.models.enums import (
    CommentaryTopic, Currency, FactKind, GrowthType, MagnitudeUnit,
    StatementNature, ValueType,
)
from app.models.schemas import (
    BBoxModel, Commentary, FinancialFact, Guidance, Provenance, Region,
)
from app.normalization.numbers import normalize_number
from app.normalization.periods import FiscalCalendar, find_all_periods, parse_period

log = get_logger("extraction.narrative")

# ------------------------------------------------------------------ patterns
SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+(?=[A-Z(])|\n+")

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
    CommentaryTopic.SEGMENTS: ("segment", "business", "vertical", "division",
                               "platforms", "retail", "o2c"),
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


def extract_narrative_facts(region: Region, ctx: NarrativeContext,
                            ) -> list[FinancialFact]:
    """Pull metric/value/growth triples out of a prose region."""
    out: list[FinancialFact] = []
    text = (region.text or "").strip()
    if not text or len(text) < 12:
        return out
    if FORWARD_DISCLAIMER.search(text):
        return out

    for s_idx, sentence in enumerate(split_sentences(text)):
        if not any(ch.isdigit() for ch in sentence):
            continue
        found = _leading_metric(sentence)
        if found is None:
            continue
        label, match = found

        periods = find_all_periods(sentence, calendar=ctx.calendar)
        period = periods[0] if periods else (
            parse_period(ctx.default_period, calendar=ctx.calendar)
            if ctx.default_period else None)

        expected = match.definition.value_type
        value_num = None
        raw_value = ""
        # Prefer a figure carrying an explicit unit or currency; a bare number in
        # prose is usually a growth rate or a footnote marker.
        for m in VALUE_PHRASE.finditer(sentence):
            tok = m.group(0).strip()
            if not any(c.isdigit() for c in tok):
                continue
            if "%" in tok or re.search(r"\bbps\b", tok, re.I):
                continue
            has_unit = bool(m.group("unit")) or any(
                sym in tok for sym in "₹$€£¥")
            window = sentence[max(0, m.start() - 24): m.end() + 24]
            if not has_unit and not re.search(
                    r"crore|lakh|million|billion|₹|\$|rs\.?", window, re.I):
                continue
            n = normalize_number(
                tok, context=window, default_currency=ctx.default_currency,
                default_magnitude=ctx.default_unit,
                expected_type=expected if expected != ValueType.UNKNOWN else None)
            if n is not None:
                value_num, raw_value = n, tok
                break

        growth_val: float | None = None
        growth_type: GrowthType | None = None
        gm = GROWTH_PHRASE.search(sentence)
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

        entity = _entity_named_in(sentence, ctx.known_entities, ctx.entity)
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
            entity=entity, kind=FactKind.NARRATIVE,
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
            meta={"sentence_index": s_idx, "region_id": region.id})
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
