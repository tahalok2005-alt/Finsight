"""Cross-page document reasoning.

A results release is not a bag of independent pages.  Section headings on one
page govern the tables that follow, an entity named in a heading owns every
figure under it until the next heading, and the letterhead that repeats on
every page is chrome rather than content.  This module builds those
document-level relationships:

* **page chrome detection** - text that recurs at the same position across many
  pages is reclassified as header/footer, whatever it looked like in isolation;
* **section tree** - headings become sections that span pages and own the
  regions, tables and charts beneath them;
* **entity attribution** - the reporting entity (consolidated, a subsidiary, a
  business segment) is carried down from the governing heading onto every fact,
  which is what makes segment-vs-total validation possible;
* **document metadata** - title, company, report period, dominant currency and
  magnitude, all inferred from content rather than the filename.
"""
from __future__ import annotations

import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field

from app.core.ids import stable_id
from app.core.logging import get_logger
from app.document.entities import _CORPORATE_SUFFIX, EntityRegistry
from app.models.enums import (
    PeriodBasis,
    Currency, EntityType, MagnitudeUnit, RegionType,
)
from app.models.schemas import ExtractedTable, Region
from app.normalization.numbers import detect_magnitude, infer_document_units
from app.normalization.periods import FiscalCalendar, find_all_periods, infer_calendar

log = get_logger("document.assembly")

# Headings that describe a *section* of the report rather than naming a
# reporting entity.  These must never become an entity, or every figure under
# "A. FINANCIAL RESULTS" would be attributed to an entity of that name.
_SECTION_HEADING_WORDS = re.compile(
    r"\b(financial results?|financial highlights?|operational update|"
    r"operational highlights?|business performance|quarterly performance|"
    r"key highlights?|highlights?|summary|overview|outlook|guidance|"
    r"strategic progress|leadership quote|management commentary|commentary|"
    r"results? for|performance|update|notes?|annexure|disclosure|"
    r"unaudited|audited|extract of|"
    r"segment information|statement of|balance sheet|cash flow|"
    r"earnings per share|other income|revenue|profit|ebitda|margin|capex|"
    r"consumption basket|value added|about |media release)\b", re.I)

# Corporate/segment naming signals: a real business unit usually carries one.
_ENTITY_SIGNAL = re.compile(
    r"\b(limited|ltd\.?|inc\.?|plc|llp|corp\.?|corporation|company|"
    r"platforms?|retail|digital|ventures?|holdings?|industries|"
    r"solutions?|services|infocomm|telecom|energy|petrochemicals?|"
    r"refining|division|segment|business|brands?)\b", re.I)

_SECTION_LETTER = re.compile(r"^\s*[A-Z]\.\s*", re.M)
_CONSOLIDATED = re.compile(r"\bconsolidated\b", re.I)
_STANDALONE = re.compile(r"\bstandalone\b", re.I)

REPORT_TYPE_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("media_release", re.compile(r"\bmedia release\b|\bpress release\b", re.I)),
    ("earnings_release", re.compile(r"\bearnings (release|report)\b", re.I)),
    ("quarterly_results", re.compile(r"\bquarterly results\b|\bquarter ended\b", re.I)),
    ("annual_report", re.compile(r"\bannual report\b", re.I)),
    ("investor_presentation", re.compile(r"\binvestor presentation\b", re.I)),
    ("financial_statements", re.compile(
        r"\b(balance sheet|statement of profit and loss|cash flow statement)\b", re.I)),
]


@dataclass(slots=True)
class Section:
    id: str
    title: str
    entity: str | None
    start_page: int
    end_page: int
    start_order: int
    entity_id: str | None = None
    region_ids: list[str] = field(default_factory=list)
    level: int = 1


@dataclass(slots=True)
class DocumentModel:
    document_id: str
    calendar: FiscalCalendar
    sections: list[Section]
    entity_by_region: dict[str, str]
    section_by_region: dict[str, str]
    chrome_region_ids: set[str]
    entities: EntityRegistry
    title: str | None
    company_name: str | None
    report_period: str | None
    report_type: str | None
    currency: Currency | None
    unit: MagnitudeUnit | None
    primary_period: str | None


# ------------------------------------------------------------------- chrome
def _norm_chrome(text: str) -> str:
    t = re.sub(r"\d+", "#", (text or "").lower())
    return re.sub(r"\s+", " ", t).strip()[:120]


def detect_page_chrome(regions_by_page: dict[int, list[Region]],
                       page_heights: dict[int, int]) -> set[str]:
    """Identify repeated letterhead/footer text by cross-page recurrence.

    A single page cannot tell a letterhead from a title.  Across the document,
    text that appears at nearly the same vertical position on at least half the
    pages is chrome.  This is why the check lives at document level.
    """
    n_pages = len(regions_by_page)
    if n_pages < 3:
        return set()
    occurrences: dict[tuple[str, int], list[Region]] = defaultdict(list)
    for page, regions in regions_by_page.items():
        h = max(1, page_heights.get(page, 1))
        for r in regions:
            if r.type in (RegionType.TABLE, RegionType.CHART, RegionType.FIGURE):
                continue
            key = (_norm_chrome(r.text), int((r.bbox.cy / h) * 20))
            if key[0]:
                occurrences[key].append(r)

    threshold = max(2, int(n_pages * 0.5))
    chrome: set[str] = set()
    for (text, _band), group in occurrences.items():
        pages = {r.page_number for r in group}
        if len(pages) >= threshold and len(text) > 3:
            for r in group:
                chrome.add(r.id)
    return chrome


def apply_chrome(regions: list[Region], chrome_ids: set[str],
                 page_height: int) -> None:
    """Retype chrome regions in place, preserving their boxes and text."""
    for r in regions:
        if r.id not in chrome_ids:
            continue
        if r.type in (RegionType.HEADER, RegionType.FOOTER,
                      RegionType.PAGE_NUMBER):
            continue
        r.meta["reclassified_from"] = str(r.type)
        r.meta["reclassified_reason"] = "repeats at the same position across pages"
        r.type = (RegionType.HEADER if r.bbox.cy < page_height * 0.5
                  else RegionType.FOOTER)
        r.confidence = round(min(r.confidence, 0.9), 3)


# ------------------------------------------------------------------ entities
_ABBREV_RE = re.compile(r"[(\[]\s*[\"“']?\s*(?P<abbr>[A-Z][A-Za-z0-9&./-]{1,12})"
                        r"\s*[\"”']?\s*[)\]]")
_CORP_NAME_RE = re.compile(
    r"^(?:for\s+)?(?P<name>[A-Z][\w&.\- ]{2,48}?\s+"
    r"(?:Limited|Ltd\.?|Inc\.?|PLC|LLP|Corporation|Corp\.?))\s*$", re.I)
_TRAILING_QUALIFIER = re.compile(
    r"\s*(segment|business|division|results?|performance|highlights?)\s*$", re.I)
# date / period vocabulary that can trail a heading
_PERIOD_PHRASE = re.compile(
    r"\b(for\s+)?(the\s+)?(quarter|year|half[- ]year|nine\s+months?|"
    r"three\s+months?|period)?\s*(ended|ending)?\s*"
    r"\d{0,2}\s*(st|nd|rd|th)?\s*"
    r"(january|february|march|april|may|june|july|august|september|october|"
    r"november|december|jan|feb|mar|apr|jun|jul|aug|sep|sept|oct|nov|dec)"
    r"[a-z]*\s*,?\s*\d{0,4}|\bQ[1-4]\s*FY\s*\d{2,4}\b|\bFY\s*\d{2,4}\b",
    re.I)
# an explicit segment banner ("JIOSTAR BUSINESS") is an entity even as one word
_SEGMENT_MARKER = re.compile(r"\b(segment|business|division)\b", re.I)

#: A corporate suffix names a legal entity wherever it appears in the heading:
#: "Consolidated Jio Platforms Limited" is a subsidiary's result whether or not
#: anything follows.
_CORPORATE_SUFFIX = re.compile(
    r"\b(limited|ltd\.?|inc\.?|plc|llp|corp\.?|corporation)\b", re.I)
#: These name a reporting unit only when the heading *ends* on one. Position is
#: what separates the two uses: "JIOSTAR BUSINESS" is a reporting unit and
#: "Paytm for Business App" is a product, and the word is the same in both.
_STRUCTURAL_HEAD = re.compile(
    r"\b(segments?|divisions?|business(?:es)?|holdings?|operations?)\s*$", re.I)
#: A trailing abbreviation is part of the name, not the head: strip it before
#: asking what the heading ends on.
_TRAILING_ABBREV = re.compile(r"[\(\[\"']\s*[A-Z0-9&/.-]{2,12}\s*[\)\]\"']\s*$")


def _declares_a_reporting_unit(heading: str) -> bool:
    """Whether this heading names a unit that reports its own figures.

    The previous test asked only whether an entity-ish word appeared anywhere,
    and in a results deck almost every heading mentions retail, digital,
    services or business. Three separate documents showed what that costs: a
    marketing line ("Quality Financial Services") governed 23 of one release's
    25 figures, a product name ("Paytm for Business App") became a segment, and
    a wheel-diagram label ("RETAIL BANKING") owned two dozen figures of a bank
    whose deck never reported that unit separately.

    What real ones have in common is structural rather than lexical: either a
    legal suffix somewhere, or a head noun that says the heading *is* a unit.
    """
    t = (heading or "").strip()
    if _CORPORATE_SUFFIX.search(t):
        return True
    without_abbrev = _TRAILING_ABBREV.sub("", t).strip(" -\u2013\u2014:,.\"'")
    return bool(_STRUCTURAL_HEAD.search(without_abbrev))


def _tokens(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9]+", (text or "").lower())
            if w not in {"limited", "ltd", "inc", "plc", "the", "of", "and", "for"}}


#: Connectives that a name does not begin with.  A heading starting with one
#: is a fragment of a sentence that OCR or layout cut in the wrong place.
_LEADING_CONNECTIVE = re.compile(
    r"^(?:of|and|for|the|to|in|on|with|from|by|our|its|a|an)\b", re.I)


def _reads_as_a_name(candidate: str) -> bool:
    """Whether a heading fragment reads as the name of a reporting unit.

    The signal-word gate above asks whether the *heading* mentions anything
    entity-like. It cannot tell a business unit from a chart legend, and in a
    slide deck almost every heading mentions "retail", "digital" or "business"
    somewhere. That let legends through as segments, so figures were attributed
    to units named "Retail Deposits Retail Deposits 9%" and "Mortgages Vehicles
    MSME Consumer Loans Credit Cards Microfin" -- keyword soup presented to the
    reader as though the document had said it.

    Rather than filter for particular wrong answers, this asks the positive
    question: does the string have the shape of a name? Four things say it does
    not, and each of them is a property of the text rather than of any filer:

    * **It carries a number.** Names do not contain "9%" or "5%". A legend
      pairing a category with its share does.
    * **It repeats a word.** "Retail Deposits Retail Deposits" is two legend
      entries concatenated, not a name said twice.
    * **It begins with a connective.** "of Ethical, Digital & Social Good
      Banking" is the tail of a sentence, cut mid-phrase.
    * **It is a run of unrelated nouns.** Four or more capitalised words with
      no connective, preposition or corporate suffix anywhere is a list --
      "Mortgages Vehicles MSME Consumer Loans Credit Cards" -- because a real
      multi-word unit name binds its words together ("Retail Banking Division",
      "Jio Platforms Limited", "Oil to Chemicals").
    """
    name = (candidate or "").strip()
    if not name:
        return False
    if any(ch.isdigit() for ch in name) or "%" in name:
        return False

    #: Words that join a name together rather than being part of what it
    #: names.  A real name may repeat one -- "Oil and Gas (Exploration and
    #: Production)" says "and" twice -- so they are excluded from the
    #: repeated-word test, which is looking for a repeated *subject*.
    binds = {"of", "and", "for", "the", "to", "in", "on", "&", "-"}
    words = name.split()
    lowered = [w.strip(",&/()").lower() for w in words]
    content = [w for w in lowered if w and w not in binds]
    if len(content) != len(set(content)):
        return False
    if _LEADING_CONNECTIVE.match(name):
        return False

    if len(words) >= 4:
        has_binding = any(w in binds for w in lowered)
        if not (has_binding or _CORPORATE_SUFFIX.search(name)
                or _SEGMENT_MARKER.search(name)):
            return False
    return True


def _entity_from_heading(text: str, *, company_name: str | None = None
                         ) -> str | None:
    """Return the reporting entity a heading introduces, or None.

    Returning None is the common and correct outcome: most headings label a
    section, not an entity, and must leave the current entity untouched.  Only a
    heading that actually names a business unit switches attribution.

    The subtle case that matters most: "CONSOLIDATED JIO PLATFORMS LIMITED" is
    *not* the group's consolidated result -- it is the consolidated result of a
    subsidiary.  Treating it as the group would mix a subsidiary's EBITDA with
    the group's revenue and manufacture false margin failures, so the word
    "consolidated" only designates the group when nothing else is named.
    """
    t = (text or "").strip().strip(":-–— ")
    if not t:
        return None
    # The length cut below keeps prose out, but a *basis* declaration is a
    # statement about what follows and stays true however much description
    # trails it: "Standalone Segment information in accordance with the RBI
    # guidelines and Accounting Standard 17 - Segment Reporting" is 108
    # characters and declares the basis for the whole section beneath it.
    if len(t) > 90:
        if (_CONSOLIDATED.search(t) or _STANDALONE.search(t))                 and not _CORPORATE_SUFFIX.search(t):
            return ("Consolidated" if _CONSOLIDATED.search(t)
                    else "Standalone")
        return None
    t = _SECTION_LETTER.sub("", t).strip()
    if not t:
        return None

    abbr_m = _ABBREV_RE.search(t)
    abbrev = abbr_m.group("abbr") if abbr_m else None
    # work on the name with any parenthetical abbreviation removed
    core = _ABBREV_RE.sub(" ", t).strip(" -–—:,.")
    # A dash or colon ends the name; a hyphen between two letters is part of a
    # word. "Risk-weighted assets by business" named a segment "Risk".
    core = re.split(r"\s*(?:[–—:]|(?<!\w)-|-(?!\w))\s*", core)[0].strip()
    if not core:
        return None

    is_consolidated = bool(_CONSOLIDATED.search(core))
    is_standalone = bool(_STANDALONE.search(core))
    stripped = _CONSOLIDATED.sub(" ", core)
    stripped = _STANDALONE.sub(" ", stripped)
    stripped = _SECTION_HEADING_WORDS.sub(" ", stripped)
    stripped = _TRAILING_QUALIFIER.sub(" ", stripped)
    # A trailing period phrase ("for quarter ended 30th June, 2026") describes
    # when, not who, and must not be mistaken for an entity name.
    stripped = _PERIOD_PHRASE.sub(" ", stripped)
    stripped = re.sub(r"\s+", " ", stripped).strip(" -–—:,.\"'")

    company_tokens = _tokens(company_name) if company_name else set()
    name_tokens = _tokens(stripped)

    # "Consolidated"/"Standalone" with no other name -> the filing group itself
    if (is_consolidated or is_standalone) and (
            not name_tokens or name_tokens <= company_tokens):
        return "Consolidated" if is_consolidated else "Standalone"

    # The same declaration, followed by description rather than by a name.
    # "Standalone Segment information in accordance with the RBI guidelines and
    # Accounting Standard 17" declares the basis for everything under it; what
    # follows says what the section contains, not who it belongs to. Without
    # this a bank's filing put its standalone and consolidated results under one
    # entity and reported twelve contradictions between them.
    if (is_consolidated or is_standalone) and not _CORPORATE_SUFFIX.search(t):
        return "Consolidated" if is_consolidated else "Standalone"

    # a heading that just names the filer is the group, not a segment
    if company_tokens and name_tokens and name_tokens <= company_tokens:
        return "Consolidated"

    if not stripped or len(stripped) < 3:
        return None
    # An unbalanced bracket means the heading is a piece of something longer.
    # One filing's legal name -- "Billionbrains Garage Ventures Limited
    # (Formerly known as Billionbrains Garage Ventures Private Limited)" -- was
    # cut by the line grouper, and the tail "Garage Ventures Private Limited)"
    # became a subsidiary governing half the document's figures.
    if stripped.count(")") != stripped.count("(") or \
            stripped.count("]") != stripped.count("["):
        return None

    words = stripped.split()
    min_words = 1 if _SEGMENT_MARKER.search(t) else 2
    if not (min_words <= len(words) <= 8):
        # a bare single word is a sub-heading ("Digital", "Sports"), not an
        # entity -- unless the heading explicitly says "segment"/"business"
        return None
    if stripped.endswith((".", ",", "?")) or stripped.islower():
        return None
    capitalised = sum(1 for w in words if w[:1].isupper())
    if capitalised / len(words) < 0.7:
        return None
    # Require the heading to declare a reporting unit, not merely to mention an
    # entity-ish word. See `_declares_a_reporting_unit`.
    if not _declares_a_reporting_unit(t):
        return None
    if not _reads_as_a_name(stripped):
        return None

    # Prefer the document's own abbreviation ("JPL", "RRVL", "O2C"): it is what
    # the tables under this heading use to refer back to the entity.
    if abbrev and len(abbrev) <= 12:
        return abbrev
    return re.sub(r"\s+", " ", stripped)[:80]


# Words that name a *kind of document*, never part of a company's legal name.
# A letterhead split by OCR yields fragments like "Media Release Industries
# Limited" that match the corporate-name shape but name nobody; excluding
# chrome catches most, and this catches the rest when the fragment is
# classified as a heading instead.  Entirely generic -- no filer is named.
_DOCUMENT_WORDS = re.compile(
    r"(?<![A-Za-z])(?:media|press|news)[ ]+release|presentation|"
    r"earnings[ ]+call|investor[ ]+(?:update|relations)|fact[ ]+sheet|"
    r"(?:annual|quarterly|interim)[ ]+report|financial[ ]+results",
    re.I)


def _is_document_kind(name: str) -> bool:
    return bool(_DOCUMENT_WORDS.search(name or ""))


def infer_filer_name(regions_by_page: dict[int, list[Region]],
                     chrome_ids: set[str] | None = None) -> str | None:
    """The filing entity's legal name, taken from headings that state it.

    Statement pages and the signature block spell the filer out in full
    ("<Name> Limited"), which is more reliable than a letterhead that OCR may
    have split across logo fragments.  Chrome regions are excluded for exactly
    that reason: a split letterhead yields fragments like "Media Release
    Industries Limited" that match the corporate-name shape but name nobody.
    """
    chrome = chrome_ids or set()
    counter: Counter[str] = Counter()
    for regions in regions_by_page.values():
        for r in regions:
            if r.id in chrome:
                continue
            if r.type not in (RegionType.SECTION_HEADING, RegionType.TITLE,
                              RegionType.PARAGRAPH):
                continue
            m = _CORP_NAME_RE.match(re.sub(r"\s+", " ", r.text).strip())
            if m:
                name = m.group("name").strip()
                if _is_document_kind(name):
                    continue
                counter[name] += 1
    return counter.most_common(1)[0][0] if counter else None


def infer_company_name(regions_by_page: dict[int, list[Region]],
                       chrome_ids: set[str]) -> str | None:
    """The letterhead text repeated near the top of every page."""
    counter: Counter[str] = Counter()
    for regions in regions_by_page.values():
        for r in regions:
            if r.id in chrome_ids and r.bbox.y < 400:
                t = re.sub(r"\s+", " ", r.text).strip()
                if 3 < len(t) < 70 and not any(c.isdigit() for c in t):
                    counter[t] += 1
    return counter.most_common(1)[0][0] if counter else None


# A statement block announces its reporting basis, and in a long filing that
# announcement repeats at the top of every page of the block -- which makes it
# look exactly like a running header.  Chrome detection therefore removes it
# from the content stream, and with it the only statement of whether these
# pages are consolidated or standalone.  Those are different reporting
# entities with different totals, so losing the distinction makes the two sets
# of figures appear to contradict each other.
#: Both word orders: "Consolidated financial results" and, as slide decks
#: title a page, "Financial summary – Consolidated". A summary slide headed
#: that way was otherwise read as continuing the section of a subsidiary
#: named two slides earlier, and the group's own figures went to the
#: subsidiary.
_BASIS_IN_CHROME = re.compile(
    r"(?<![A-Za-z])(?:unaudited[ ]+|audited[ ]+)?"
    r"(?P<basis>consolidated|standalone)(?![A-Za-z])"
    # "Resuits": the recogniser's damage to "results" must not hide a
    # standalone statement's title (Phase 22).
    r".{0,60}?(?:financial[ ]+)?(?:re[a-z0-9]{2,4}ts?|statements?|information)"
    r"(?![A-Za-z])"
    r"|(?<![A-Za-z])(?:financial[ ]+)?(?:results?|statements?|information|summary|"
    r"highlights|snapshot|performance)[ ]*[-–—:(|][ ]*"
    r"(?P<basis2>consolidated|standalone)(?![A-Za-z])",
    re.I)


#: How far down a page a basis declaration may sit and still be read as a
#: heading over the figures rather than a mention inside them.
#:
#: Position, not region count. Counting regions made the limit depend on how
#: much letterhead a filer prints: one company opens each results page with a
#: logo, its registered name, a former name, a postal address, a CIN and a
#: contact line before reaching "Statement of the unaudited standalone
#: financial results". That is six regions of preamble, none of it repeated
#: closely enough for chrome detection to match it -- OCR renders the former
#: name differently on each page -- so a rank limit either missed the
#: declaration or had to be raised until it let prose through. The declaration
#: itself sits at 16% and 20% of page height on the two pages concerned, while
#: the sentences that mention "consolidated" in passing are below 60%.
_BASIS_DECLARATION_DEPTH = 0.33


#: "Financial summary - X": a title band's fixed words, a dash, a qualifier.
_TITLE_PREFIX = re.compile(
    r"^(?P<prefix>(?:\d{1,3}\s*[-–]?\s*(?:years?|yrs?|quarters?|months?)\s+)?"
    r"[A-Za-z][A-Za-z &]{3,40}?)\s*[-–—:|]\s*\S", re.I)


def _prefix_key(prefix: str) -> str:
    """A title band's fixed words, with its numbers taken out: "19-years
    financial snapshot" and "9-years financial snapshot" are one band."""
    return re.sub(r"[\d\s\-–]+", " ", prefix.lower()).strip()


def _basis_by_page(regions_by_page: dict[int, list[Region]],
                   chrome_ids: set[str],
                   page_heights: dict[int, int] | None = None,
                   ) -> dict[int, str]:
    """Reporting basis declared by each page's header/footer chrome.

    Only the two group-level bases are recognised here.  A running header
    naming a specific business unit is not treated as an entity declaration,
    because a page header is too weak a signal to move attribution to a
    subsidiary -- a real section heading does that.
    """
    out: dict[int, str] = {}
    #: The words a basis-declaring title puts before its dash.
    prefixes: set[str] = set()
    for page, regions in regions_by_page.items():
        ordered = sorted(regions, key=lambda r: (r.bbox.geom.y, r.reading_order))
        height = max(1, (page_heights or {}).get(page, 0)
                     or int(max((x.bbox.geom.y1 for x in regions), default=1)))
        for r in ordered:
            if not r.text:
                continue
            is_chrome = (r.id in chrome_ids or r.type in (
                RegionType.HEADER, RegionType.FOOTER))
            # A declaration in the running header is the usual case. But a
            # statutory filing often opens the page with it in ordinary body
            # text instead -- "Standalone Segment information in accordance
            # with the RBI guidelines and Accounting Standard 17" -- and where
            # the header band is OCR noise ("HHDFC BAN", "40C 063") that is the
            # only place the page says which basis it is on. Reading only the
            # chrome put one bank's standalone and consolidated results under a
            # single entity, and the evidence layer then reported twelve
            # contradictions between figures that were never comparable.
            #
            # Only the top of the page counts. Further down, "consolidated"
            # is a word in a sentence rather than a heading over the figures.
            if (not is_chrome
                    and r.bbox.geom.y > height * _BASIS_DECLARATION_DEPTH):
                continue
            m = _BASIS_IN_CHROME.search(r.text)
            if m:
                out[page] = (m.group("basis") or m.group("basis2")).lower()
                prefix = _TITLE_PREFIX.match(r.text.strip())
                if prefix and m.group("basis2"):
                    prefixes.add(_prefix_key(prefix.group("prefix")))
                break

    # A title band that declares a basis on some pages -- "Financial summary
    # - Consolidated" -- and names something else in the same place on
    # another -- "Financial summary - Bajaj Finance" -- is telling us that
    # page is NOT on that basis. Left to the default, the parent company's
    # own figures were filed under the consolidated group, 57 of them, with
    # nothing to show they had been. The page's basis is recorded as unstated.
    if prefixes:
        for page, regions in regions_by_page.items():
            if page in out:
                continue
            height = max(1, (page_heights or {}).get(page, 0)
                         or int(max((x.bbox.geom.y1 for x in regions), default=1)))
            for r in regions:
                if not r.text or r.bbox.geom.y > height * _BASIS_DECLARATION_DEPTH:
                    continue
                prefix = _TITLE_PREFIX.match(r.text.strip())
                if prefix and _prefix_key(prefix.group("prefix")) in prefixes:
                    out[page] = "unstated"
                    break
    return out


#: A running header must hold still for this many consecutive pages before it
#: is believed to be the document's own section structure. Below it, the text
#: in the header band is page content that the layout detector mislabelled,
#: and it says nothing about where one section ends.
MIN_PAGES_IN_A_SECTION_BAND = 3


def _section_bands(regions_by_page: dict[int, list[Region]],
                   chrome_ids: set[str]) -> dict[int, str]:
    """The running section title each page carries, where the document has one.

    A slide deck prints its own structure in the corner of every page --
    "Section 3: Assets", then "Section 4: Asset Quality" -- and that is the
    strongest available statement of where one part of the document stops. A
    filing does not: what lands in its header band is a continuation of the
    table above, different on every page.

    Stability is what separates the two, and it is measurable. A header value
    that repeats across at least ``MIN_PAGES_IN_A_SECTION_BAND`` consecutive
    pages is section structure; one that changes every page is not, and pages
    carrying it are left with no band at all.

    Measured on the two documents this rule was built from: the deck yields
    five bands covering pages 6-63, the filing yields none.
    """
    raw: dict[int, str] = {}
    for page, regions in regions_by_page.items():
        for r in sorted(regions, key=lambda z: z.reading_order):
            is_chrome = r.id in chrome_ids or r.type == RegionType.HEADER
            text = " ".join((r.text or "").split()).strip()
            if is_chrome and text:
                raw[page] = text
                break

    bands: dict[int, str] = {}
    pages = sorted(raw)
    start = 0
    while start < len(pages):
        end = start
        while (end + 1 < len(pages)
               and raw[pages[end + 1]] == raw[pages[start]]
               and pages[end + 1] == pages[end] + 1):
            end += 1
        if end - start + 1 >= MIN_PAGES_IN_A_SECTION_BAND:
            for i in range(start, end + 1):
                bands[pages[i]] = raw[pages[start]]
        start = end + 1
    return bands


def build_sections(regions_by_page: dict[int, list[Region]],
                   chrome_ids: set[str], company_name: str | None = None,
                   registry: EntityRegistry | None = None,
                   page_heights: dict[int, int] | None = None,
                   ) -> tuple[list[Section], dict[str, str], dict[str, str],
                              EntityRegistry]:
    """Walk the document in reading order and cut a new section at each heading.

    Each heading that names a reporting unit registers it and becomes the
    governing entity for everything beneath, until the next such heading.  The
    mapping returned is region id -> *entity id*, so downstream stages carry an
    identity rather than a free-text name.
    """
    document_id = next(
        (r.document_id for regions in regions_by_page.values()
         for r in regions), "")
    registry = registry or EntityRegistry(document_id, company_name)

    sections: list[Section] = []
    section_by_region: dict[str, str] = {}
    entity_by_region: dict[str, str] = {}

    basis_by_page = _basis_by_page(regions_by_page, chrome_ids, page_heights)
    bands = _section_bands(regions_by_page, chrome_ids)
    standalone_id: str | None = None

    current: Section | None = None
    # Until something on the page states a basis, none is known. This used to
    # start at "Consolidated", the registry's default -- so a bank's press
    # release that states no basis anywhere had every figure filed under its
    # consolidated group, because a footnote mentioned "consolidated financial
    # statements of the Group" (Phase 22). A basis is what a document states;
    # where it states none, the figures say so.
    unstated_id: str | None = None

    def _unstated() -> str:
        nonlocal unstated_id
        if unstated_id is None:
            unstated_id = registry.register_unstated_basis(
                evidence="no heading, page header or column head before this "
                         "region states a reporting basis")
        return unstated_id

    current_entity_id: str | None = None
    basis_entity_id: str | None = None
    current_band: str | None = None

    for page in sorted(regions_by_page):
        # A heading names the entity for *its section*, not for the rest of the
        # document. Where the document declares its own sections in a running
        # header, a change of section ends the attribution.
        #
        # Without this, one heading reaches arbitrarily far: a deck naming its
        # "Retail, Agri and MSME (RAM) Book" on page 23 went on to own pages 41
        # to 73 -- the bank's own profit and loss, its capital adequacy and its
        # shareholding pattern, all filed under one of its lending segments.
        # That was 67 of the document's 104 figures.
        band = bands.get(page)
        if band is not None and band != current_band:
            if current_band is not None:
                current_entity_id = basis_entity_id
                current = None
            current_band = band
        # A page whose chrome declares a reporting basis switches attribution
        # for that page, unless a section heading on the page names something
        # more specific.
        basis = basis_by_page.get(page)
        if basis == "standalone":
            if standalone_id is None:
                standalone_id = registry.register_basis(
                    EntityType.STANDALONE, page=page,
                    evidence="declared by the page header of this statement "
                             "block")
            basis_entity_id = standalone_id
            current_entity_id = standalone_id
            current = None
        elif basis == "consolidated":
            basis_entity_id = registry.consolidated_id
            current_entity_id = registry.consolidated_id
            current = None
        elif basis == "unstated":
            basis_entity_id = registry.register_unstated_basis(
                page=page, evidence="the page's title names neither basis where "
                                    "other pages of this document name one")
            current_entity_id = basis_entity_id
            current = None
        elif basis_entity_id is not None and page not in basis_by_page:
            basis_entity_id = None

        for r in sorted(regions_by_page[page], key=lambda z: z.reading_order):
            if r.id in chrome_ids or r.type in (
                    RegionType.HEADER, RegionType.FOOTER, RegionType.PAGE_NUMBER):
                continue
            if r.type in (RegionType.SECTION_HEADING, RegionType.TITLE):
                name = _entity_from_heading(r.text, company_name=company_name)
                if name:
                    abbr_m = _ABBREV_RE.search(r.text)
                    heading_entity = registry.register_heading(
                        name, raw_heading=r.text, page=page,
                        abbreviation=abbr_m.group("abbr") if abbr_m else None)
                    # A heading naming the filer itself says *who* is
                    # reporting; a basis declaration on the same page says *on
                    # what basis*. They are not competing claims -- the second
                    # refines the first -- so the basis must survive.
                    #
                    # Without this, a filer that prints its own registered name
                    # in the letterhead of every page overrode the declaration
                    # below it, and an entire standalone statement was
                    # attributed to the consolidated group. The reconciler then
                    # compared the two statements line by line and reported 89
                    # contradictions between figures that were never meant to
                    # agree.
                    if (heading_entity == registry.consolidated_id
                            and basis_entity_id is not None):
                        current_entity_id = basis_entity_id
                    else:
                        current_entity_id = heading_entity
                section_entity_id = current_entity_id or _unstated()
                current = Section(
                    id=stable_id("section", r.document_id, page, r.reading_order),
                    title=r.text.strip()[:300],
                    entity=(registry.get(section_entity_id).name
                            if registry.get(section_entity_id) else None),
                    entity_id=section_entity_id,
                    start_page=page, end_page=page,
                    start_order=r.reading_order,
                    level=1 if r.type == RegionType.TITLE else 2)
                sections.append(current)
            if current is not None:
                current.end_page = max(current.end_page, page)
                current.region_ids.append(r.id)
                section_by_region[r.id] = current.id
            entity_by_region[r.id] = current_entity_id or _unstated()
    return sections, entity_by_region, section_by_region, registry


# ----------------------------------------------------------------- metadata
def infer_metadata(regions_by_page: dict[int, list[Region]],
                   chrome_ids: set[str], calendar: FiscalCalendar,
                   company: str | None = None,
                   ) -> tuple[str | None, str | None, str | None, str | None,
                              Currency | None, MagnitudeUnit | None, str | None]:
    """Infer document-level metadata strictly from page content."""
    first_page = min(regions_by_page) if regions_by_page else 1
    front = sorted(regions_by_page.get(first_page, []),
                   key=lambda r: r.reading_order)

    title = None
    for r in front:
        if r.id in chrome_ids:
            continue
        if r.type in (RegionType.TITLE, RegionType.SECTION_HEADING) and \
                len(r.text.strip()) > 12:
            title = r.text.strip()[:300]
            break

    all_text: list[str] = []
    for regions in regions_by_page.values():
        for r in regions:
            if r.text:
                all_text.append(r.text)
    joined = "\n".join(all_text)

    report_type = None
    for name, pattern in REPORT_TYPE_PATTERNS:
        if pattern.search(joined[:6000]):
            report_type = name
            break

    periods = find_all_periods(joined[:8000], calendar=calendar)
    quarterly = [p for p in periods if p.quarter]
    primary = None
    if quarterly:
        primary = max(quarterly, key=lambda p: (p.fiscal_year or 0, p.quarter or 0))
    elif periods:
        primary = max(periods, key=lambda p: (p.fiscal_year or 0))
    report_period = primary.label if primary else None

    # The document's currency is what its own unit declarations say, or
    # nothing. There used to be a fallback to the first currency symbol on the
    # opening pages, and it read whatever was there: a footnote marked "$" made
    # a telecom's rupee statements dollars, and a figure printed "$12,840.08"
    # (an amount, not a declaration) would have done the same to a bank's.
    # Without a declaration a table's currency comes from its own evidence;
    # crore and lakh figures still say rupees, and say it was inferred.
    currency, unit = infer_document_units(all_text)
    if unit is None:
        unit = detect_magnitude(joined[:6000])

    return (title, company, report_period, report_type, currency, unit,
            report_period)


# ------------------------------------------------------------------ assemble
def assemble(document_id: str, regions_by_page: dict[int, list[Region]],
             page_heights: dict[int, int]) -> DocumentModel:
    texts = [r.text for regions in regions_by_page.values()
             for r in regions if r.text]
    calendar = infer_calendar(texts)

    chrome = detect_page_chrome(regions_by_page, page_heights)
    for page, regions in regions_by_page.items():
        apply_chrome(regions, chrome, page_heights.get(page, 1))

    # The company name is needed before sections are cut: a heading that merely
    # repeats the filer's own name means "consolidated", not a new segment.
    # Prefer the filer's stated legal name; fall back to the letterhead.
    company = infer_filer_name(regions_by_page, chrome) or infer_company_name(
        regions_by_page, chrome)
    sections, entity_by_region, section_by_region, registry = build_sections(
        regions_by_page, chrome, company_name=company,
        page_heights=page_heights)
    title, company, period, rtype, currency, unit, primary = infer_metadata(
        regions_by_page, chrome, calendar, company=company)

    log.info("document.assembled", document_id=document_id,
             sections=len(sections), chrome_regions=len(chrome),
             fiscal_year_end_month=calendar.year_end_month,
             entities=len(registry),
             entity_names=[e.name for e in registry.all()][:12])

    return DocumentModel(
        document_id=document_id, calendar=calendar, sections=sections,
        entity_by_region=entity_by_region, section_by_region=section_by_region,
        chrome_region_ids=chrome, entities=registry,
        title=title, company_name=company,
        report_period=period, report_type=rtype, currency=currency, unit=unit,
        primary_period=primary)


def entity_for(model: DocumentModel, region_id: str | None) -> str:
    """Entity *id* governing a region; a region no statement reached has a
    basis the document does not state -- never a default one."""
    if region_id and region_id in model.entity_by_region:
        return model.entity_by_region[region_id]
    return model.entities.register_unstated_basis(
        evidence="the region is outside every section that states a basis")


def entity_ref_for(model: DocumentModel, region_id: str | None):
    """The full entity record governing a region."""
    return model.entities.get(entity_for(model, region_id))


def link_tables(model: DocumentModel, tables: list[ExtractedTable]) -> None:
    """Attach section titles to tables that have no caption of their own."""
    by_id = {s.id: s for s in model.sections}
    for t in tables:
        sid = model.section_by_region.get(t.region_id or "")
        if sid and sid in by_id and not t.title:
            t.title = by_id[sid].title


#: Words that make a stub head a statement's title rather than a line label.
_STATEMENT_TITLE = re.compile(
    r"\b(?:statements?|results|balance\s+sheet|cash\s+flows?|financial\s+position)\b", re.I)


def _stub_stem(text: str) -> str:
    """A stub head reduced to its words: a repeated "(continued)" and the unit
    declaration are the same head."""
    t = re.sub(r"[^a-z]+", " ", (text or "").lower())
    return " ".join(w for w in t.split() if w not in (
        "continued", "contd", "cont", "millions", "million", "unless", "otherwise", "stated",
        "in", "crore", "crores", "lakhs", "e", "rs", "inr"))


def link_continuations(tables: list[ExtractedTable]) -> None:
    """Link a table that continues its predecessor, across or within a page.

    Two cases matter for financial statements:

    * a table broken across a page boundary, recognised by an identical column
      structure on consecutive pages;
    * a long statement split *within* one page because a wide gap interrupted
      the row run, recognised by the same column count directly below with no
      header of its own.

    In both cases the continuation inherits the predecessor's column semantics,
    so its rows still resolve to the right periods instead of being dropped.
    """
    ordered = sorted(tables, key=lambda t: (t.page_number, t.bbox.y))
    for prev, cur in zip(ordered, ordered[1:]):
        same_page = cur.page_number == prev.page_number
        next_page = cur.page_number == prev.page_number + 1
        if not (same_page or next_page):
            continue
        # on the same page the fragment must start below its parent; boxes may
        # overlap slightly where a wrapped label straddles the split
        if same_page and cur.bbox.y <= prev.bbox.y:
            continue
        if cur.n_cols != prev.n_cols:
            continue
        prev_headers = [c.header.strip().lower() for c in prev.columns]
        cur_headers = [c.header.strip().lower() for c in cur.columns]
        same = sum(1 for a, b in zip(prev_headers, cur_headers) if a and a == b)
        headerless = sum(1 for h in cur_headers if h) <= 1
        # a fragment whose columns carry no period at all, following a table
        # that does, is a continuation rather than an independent table
        inherits = (headerless
                    or (not any(c.normalized_period for c in cur.columns)
                        and any(c.normalized_period for c in prev.columns)))
        # Two statements, one after the other, each titled in its stub --
        # "(A.1) ... Quarter" and "(A.2) ... Year-to-Date" -- are two tables,
        # whatever their column counts. The year-to-date statement inherited
        # the quarter's heads and 41 half-year figures were dated to the
        # quarter (Phase 22).
        stub_prev = (prev.columns[0].header if prev.columns else "") or ""
        stub_cur = (cur.columns[0].header if cur.columns else "") or ""
        if (_STATEMENT_TITLE.search(stub_prev) and _STATEMENT_TITLE.search(stub_cur)
                and _stub_stem(stub_prev) != _stub_stem(stub_cur)):
            continue
        if same >= max(2, len(prev_headers) // 2) or inherits:
            cur.continued_from_table_id = prev.id
            # A fragment either carries no title of its own or repeats its
            # parent's, because both were cut from one titled statement.  A
            # repeated title argues *for* continuation, so it must not block the
            # inheritance -- only a genuinely different title does.
            def _title_stem(t: str) -> str:
                return (t or "").strip().lower().removesuffix("(continued)").strip()
            same_title = _title_stem(cur.title) == _title_stem(prev.title)
            if inherits and (not cur.title or same_title):
                if not cur.title:
                    cur.title = f"{prev.title} (continued)".strip()
                # inherit the predecessor's column semantics so the continuation
                # rows still resolve to periods
                for c_prev, c_cur in zip(prev.columns, cur.columns):
                    if not c_cur.normalized_period:
                        c_cur.header = c_prev.header
                        c_cur.header_path = list(c_prev.header_path)
                        c_cur.normalized_period = c_prev.normalized_period
                        # The fragment's period is its parent's, dates and all;
                        # marked inherited because the fragment itself said
                        # nothing.
                        if c_prev.period_resolved is not None:
                            inherited = c_prev.period_resolved.model_copy()
                            inherited.basis = PeriodBasis.INHERITED
                            c_cur.period_resolved = inherited
                        c_cur.period_label = c_prev.period_label
                        c_cur.is_growth_column = c_prev.is_growth_column
                        c_cur.growth_type = c_prev.growth_type
