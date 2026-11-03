"""Risk and cautionary statement extraction.

Financial documents hedge.  They say "subject to", "may be adversely affected",
"volatility in crude prices", "regulatory developments could impact".  Those
sentences are the document's own statement of what could go wrong, and they are
worth surfacing verbatim with their location.

What this module does **not** do is score them.  There is no "risk score of
7.2/10" here: a score would be a number this system invented, and a reader
cannot check an invented number against the page.  What is reported instead is:
the sentence as printed, the category its own vocabulary places it in, the
phrase that triggered the match, and whether it is written as forward-looking.
Severity reflects the strength of the language used, and nothing more.

The boilerplate forward-looking-statements disclaimer is deliberately excluded:
it is a legal notice present in every filing, not a statement about this
business.
"""
from __future__ import annotations

import re

from app.core.ids import stable_id
from app.core.logging import get_logger
from app.models.enums import RiskCategory, Severity
from app.models.schemas import Region, RiskStatement

log = get_logger("analysis.risks")

# Language that marks a sentence as cautionary, with how strong it is.
_STRONG = re.compile(
    r"\badversely (affect|impact)\w*\b|\bmaterial(ly)? adverse\b|"
    r"\bsignificant(ly)? (risk|impact|decline|reduction)\b|"
    r"\bcannot (be )?assur\w+\b|\bno assurance\b|\bsubstantial (risk|doubt)\b|"
    r"\bimpairment\b|\bdefault\b|\blitigation\b", re.I)
_MODERATE = re.compile(
    r"\brisks?\b|\bexposure to\b|\bvolatil\w+\b|\buncertain\w*\b|"
    r"\bheadwinds?\b|\bpressure on\b|\bchalleng\w+\b|\bdisrupt\w+\b|"
    r"\bconstrain\w+\b|\bweakness\b|\bslowdown\b|\bdownturn\b", re.I)
_HEDGE = re.compile(
    r"\bmay (be|have|result|affect|impact|decline)\b|\bcould (be|have|result|"
    r"affect|impact|adversely)\b|\bsubject to\b|\bdepend(s|ent)? (up)?on\b|"
    r"\bif .{0,40}\bwere to\b", re.I)

_FORWARD = re.compile(
    r"\bexpect\w*\b|\banticipat\w*\b|\bwill\b|\bgoing forward\b|\boutlook\b|"
    r"\bfuture\b|\bmay\b|\bcould\b|\bproject\w*\b|\bforecast\w*\b", re.I)

# The standard safe-harbour notice: legal boilerplate, not a business risk.
_DISCLAIMER = re.compile(
    r"forward[- ]looking statements?.{0,200}(within the meaning|safe harbou?r|"
    r"securities litigation|actual results may differ)|"
    r"(this|these) (document|presentation|release).{0,80}contains? "
    r"forward[- ]looking", re.I | re.S)

# Category vocabulary.  Generic industry-neutral language; a document that talks
# about none of these yields no categorised risk rather than a guessed one.
_CATEGORIES: tuple[tuple[RiskCategory, re.Pattern], ...] = (
    (RiskCategory.COMMODITY_PRICE, re.compile(
        r"\bcrude\b|\bcommodit\w+\b|\bfeedstock\b|\braw material price\w*\b|"
        r"\bcrack\w*\b|\bprice volatility\b|\bspreads?\b|\bbrent\b", re.I)),
    (RiskCategory.CURRENCY, re.compile(
        r"\b(foreign )?exchange rate\w*\b|\bcurrency (risk|fluctuat\w+|"
        r"volatil\w+)\b|\bforex\b|\bdepreciation of the (rupee|dollar|euro)\b",
        re.I)),
    (RiskCategory.REGULATORY, re.compile(
        r"\bregulat\w+\b|\bcomplian\w+\b|\bpolicy chang\w+\b|\btariff\w*\b|"
        r"\bduty\b|\blegislat\w+\b|\bstatutory\b|\btax(ation)? (law|change)\w*\b",
        re.I)),
    (RiskCategory.DEMAND, re.compile(
        r"\bdemand\b|\bconsumption\b|\bvolume\w* (decline|weak\w*|fell)\b|"
        r"\bcustomer (churn|attrition)\b|\bsubscriber loss\w*\b", re.I)),
    (RiskCategory.SUPPLY_CHAIN, re.compile(
        r"\bsupply chain\b|\blogistics\b|\bshipping\b|\bfreight\b|"
        r"\bshortage\w*\b|\bsourcing\b|\bavailability of\b", re.I)),
    (RiskCategory.MACROECONOMIC, re.compile(
        r"\bmacroeconomic\b|\bglobal econom\w+\b|\brecession\b|\binflation\w*\b|"
        r"\binterest rate\w*\b|\bgdp\b|\beconomic (slowdown|uncertain\w+)\b",
        re.I)),
    (RiskCategory.GEOPOLITICAL, re.compile(
        r"\bgeopolitic\w+\b|\bconflict\b|\bsanction\w*\b|\bwar\b|"
        r"\btrade (tension|restriction)\w*\b|\bembargo\b", re.I)),
    (RiskCategory.COMPETITIVE, re.compile(
        r"\bcompetiti\w+\b|\bmarket share\b|\bpricing pressure\b|\brival\w*\b|"
        r"\bnew entrant\w*\b", re.I)),
    (RiskCategory.CREDIT, re.compile(
        r"\bcredit risk\b|\breceivable\w* (risk|overdue)\b|\bcounterparty\b|"
        r"\bdefault\w*\b|\bbad debt\w*\b|\bprovisioning\b", re.I)),
    (RiskCategory.OPERATIONAL, re.compile(
        r"\bshutdown\w*\b|\bturnaround\w*\b|\bmaintenance\b|\boutage\w*\b|"
        r"\bplant (availability|reliability)\b|\bsafety\b|\bcyber\w*\b|"
        r"\bexecution risk\b", re.I)),
)

_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+(?=[A-Z(])")
MIN_LENGTH = 40
MAX_LENGTH = 600


def _split(text: str) -> list[str]:
    return [s.strip() for s in _SENTENCE_SPLIT.split(text or "") if s.strip()]


def _categorise(sentence: str) -> tuple[RiskCategory, list[RiskCategory]]:
    hits = [category for category, pattern in _CATEGORIES
            if pattern.search(sentence)]
    if not hits:
        return RiskCategory.OTHER, []
    return hits[0], hits


def extract_risks(document_id: str, regions: list[Region], *,
                  entity_id: str | None = None, entity_name: str | None = None,
                  period: str | None = None) -> list[RiskStatement]:
    """Find cautionary statements in narrative regions.

    Returns one record per qualifying sentence, quoting it verbatim.  No
    aggregate score is produced -- the document did not state one, so neither
    does FinSight.
    """
    out: list[RiskStatement] = []
    seen: set[str] = set()

    for region in regions:
        text = region.text or ""
        if not text or _DISCLAIMER.search(text):
            continue
        for sentence in _split(text):
            if not (MIN_LENGTH <= len(sentence) <= MAX_LENGTH):
                continue
            if _DISCLAIMER.search(sentence):
                continue

            strong = _STRONG.search(sentence)
            moderate = _MODERATE.search(sentence)
            hedge = _HEDGE.search(sentence)
            if not (strong or moderate or hedge):
                continue
            category, categories = _categorise(sentence)
            # A hedge on its own ("subject to", "depends on") is ordinary
            # accounting language.  It becomes a cautionary statement only when
            # it hedges something the risk vocabulary recognises -- either
            # explicit risk language, or a named risk domain such as regulation
            # or currency.
            if hedge and not (strong or moderate or categories):
                continue

            trigger = (strong or moderate or hedge).group(0)
            severity = (Severity.HIGH if strong else
                        Severity.MEDIUM if moderate and hedge else
                        Severity.MEDIUM if hedge and categories else
                        Severity.LOW)
            normalised = " ".join(sentence.lower().split())
            if normalised in seen:
                continue
            seen.add(normalised)

            out.append(RiskStatement(
                id=stable_id("risk", document_id, region.id, normalised[:120]),
                document_id=document_id, page_number=region.page_number,
                region_id=region.id, text=sentence,
                category=category, categories=categories,
                entity_id=entity_id, entity_name=entity_name, period=period,
                trigger_phrase=trigger, severity=severity,
                is_forward_looking=bool(_FORWARD.search(sentence)),
                confidence=round(min(0.95, 0.55 + 0.2 * bool(strong)
                                     + 0.1 * bool(categories)
                                     + 0.15 * region.confidence), 3),
                bbox=region.bbox,
            ))

    rank = {str(Severity.HIGH): 0, str(Severity.MEDIUM): 1,
            str(Severity.LOW): 2, str(Severity.INFO): 3}
    out.sort(key=lambda r: (rank.get(str(r.severity), 9), r.page_number))
    log.info("risks.extracted", document_id=document_id, count=len(out))
    return out


def summarise(risks: list[RiskStatement]) -> dict:
    """Counts by category and severity.

    A count is a fact about the document.  A score would not be, so none is
    produced.
    """
    by_category: dict[str, int] = {}
    by_severity: dict[str, int] = {}
    for r in risks:
        by_category[str(r.category)] = by_category.get(str(r.category), 0) + 1
        by_severity[str(r.severity)] = by_severity.get(str(r.severity), 0) + 1
    return {
        "total": len(risks),
        "by_category": dict(sorted(by_category.items(),
                                   key=lambda kv: -kv[1])),
        "by_severity": by_severity,
        "forward_looking": sum(1 for r in risks if r.is_forward_looking),
        "note": ("Counts of cautionary statements the document itself makes. "
                 "FinSight does not compute an overall risk score, because the "
                 "document does not state one and an invented score could not "
                 "be checked against the source."),
    }
