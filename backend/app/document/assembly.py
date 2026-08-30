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
from app.models.enums import Currency, MagnitudeUnit, RegionType
from app.models.schemas import ExtractedChart, ExtractedTable, Region
from app.normalization.numbers import detect_currency, detect_magnitude, infer_document_units
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


def _tokens(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9]+", (text or "").lower())
            if w not in {"limited", "ltd", "inc", "plc", "the", "of", "and", "for"}}


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
    if not t or len(t) > 90:
        return None
    t = _SECTION_LETTER.sub("", t).strip()
    if not t:
        return None

    abbr_m = _ABBREV_RE.search(t)
    abbrev = abbr_m.group("abbr") if abbr_m else None
    # work on the name with any parenthetical abbreviation removed
    core = _ABBREV_RE.sub(" ", t).strip(" -–—:,.")
    core = re.split(r"\s*[-–—:]\s*", core)[0].strip()
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

    # a heading that just names the filer is the group, not a segment
    if company_tokens and name_tokens and name_tokens <= company_tokens:
        return "Consolidated"

    if not stripped or len(stripped) < 3:
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
    # Require an explicit corporate or segment marker somewhere in the original
    # heading; without one this is prose, not an entity banner.
    if not _ENTITY_SIGNAL.search(t):
        return None

    # Prefer the document's own abbreviation ("JPL", "RRVL", "O2C"): it is what
    # the tables under this heading use to refer back to the entity.
    if abbrev and len(abbrev) <= 12:
        return abbrev
    return re.sub(r"\s+", " ", stripped)[:80]


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
                counter[m.group("name").strip()] += 1
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


def build_sections(regions_by_page: dict[int, list[Region]],
                   chrome_ids: set[str], company_name: str | None = None,
                   ) -> tuple[list[Section], dict[str, str], dict[str, str]]:
    """Walk the document in reading order and cut a new section at each heading."""
    sections: list[Section] = []
    section_by_region: dict[str, str] = {}
    entity_by_region: dict[str, str] = {}

    current: Section | None = None
    current_entity = "Consolidated"

    for page in sorted(regions_by_page):
        for r in sorted(regions_by_page[page], key=lambda z: z.reading_order):
            if r.id in chrome_ids or r.type in (
                    RegionType.HEADER, RegionType.FOOTER, RegionType.PAGE_NUMBER):
                continue
            if r.type in (RegionType.SECTION_HEADING, RegionType.TITLE):
                entity = _entity_from_heading(r.text, company_name=company_name)
                if entity:
                    current_entity = entity
                current = Section(
                    id=stable_id("section", r.document_id, page, r.reading_order),
                    title=r.text.strip()[:300], entity=current_entity,
                    start_page=page, end_page=page,
                    start_order=r.reading_order,
                    level=1 if r.type == RegionType.TITLE else 2)
                sections.append(current)
            if current is not None:
                current.end_page = max(current.end_page, page)
                current.region_ids.append(r.id)
                section_by_region[r.id] = current.id
            entity_by_region[r.id] = current_entity
    return sections, entity_by_region, section_by_region


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

    currency, unit = infer_document_units(all_text)
    if currency is None:
        currency, _ = detect_currency(joined[:6000])
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
    sections, entity_by_region, section_by_region = build_sections(
        regions_by_page, chrome, company_name=company)
    title, company, period, rtype, currency, unit, primary = infer_metadata(
        regions_by_page, chrome, calendar, company=company)

    log.info("document.assembled", document_id=document_id,
             sections=len(sections), chrome_regions=len(chrome),
             fiscal_year_end_month=calendar.year_end_month,
             entities=len(set(entity_by_region.values())))

    return DocumentModel(
        document_id=document_id, calendar=calendar, sections=sections,
        entity_by_region=entity_by_region, section_by_region=section_by_region,
        chrome_region_ids=chrome, title=title, company_name=company,
        report_period=period, report_type=rtype, currency=currency, unit=unit,
        primary_period=primary)


def entity_for(model: DocumentModel, region_id: str | None) -> str:
    if region_id and region_id in model.entity_by_region:
        return model.entity_by_region[region_id]
    return "Consolidated"


def link_tables(model: DocumentModel, tables: list[ExtractedTable]) -> None:
    """Attach section titles to tables that have no caption of their own."""
    by_id = {s.id: s for s in model.sections}
    for t in tables:
        sid = model.section_by_region.get(t.region_id or "")
        if sid and sid in by_id and not t.title:
            t.title = by_id[sid].title


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
        if same >= max(2, len(prev_headers) // 2) or inherits:
            cur.continued_from_table_id = prev.id
            if inherits and not cur.title:
                cur.title = f"{prev.title} (continued)".strip()
                # inherit the predecessor's column semantics so the continuation
                # rows still resolve to periods
                for c_prev, c_cur in zip(prev.columns, cur.columns):
                    if not c_cur.normalized_period:
                        c_cur.header = c_prev.header
                        c_cur.header_path = list(c_prev.header_path)
                        c_cur.normalized_period = c_prev.normalized_period
                        c_cur.period_label = c_prev.period_label
                        c_cur.is_growth_column = c_prev.is_growth_column
                        c_cur.growth_type = c_prev.growth_type
