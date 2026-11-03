"""Hierarchical entity resolution.

A financial release reports on several reporting units at once:

    Filing company
      ├── Consolidated        (reporting basis)
      ├── Standalone          (reporting basis)
      ├── Subsidiary          e.g. a platforms or retail holding company
      ├── Segment             e.g. an oil-to-chemicals or media segment
      └── Joint venture / associate

Attributing a figure to the wrong unit is worse than not extracting it: a
subsidiary's EBITDA divided by the group's revenue produces a margin that looks
plausible and is nonsense.  This module therefore resolves entities explicitly,
gives each one an identity, a type and a parent, and records how it was found.

Nothing here is company-specific.  Entity names are inferred from the document's
own headings, its abbreviations and its narrative; the classifier keys off
generic corporate and segment vocabulary, never off a list of known companies.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from app.core.ids import stable_id
from app.core.logging import get_logger
from app.models.enums import EntityType

log = get_logger("document.entities")

CONSOLIDATED = "Consolidated"
UNSTATED_BASIS = "Basis not stated"
STANDALONE = "Standalone"

#: Words that mark a heading as naming a *segment* rather than a legal entity.
_SEGMENT_MARKER = re.compile(r"\b(segment|division|business|vertical)\b", re.I)
#: Corporate-form suffixes that mark a legal entity.
_CORPORATE_SUFFIX = re.compile(
    r"\b(limited|ltd\.?|inc\.?|plc|llp|corp\.?|corporation|company|"
    r"gmbh|s\.a\.|n\.v\.|pte|pvt)\b", re.I)
_JV_MARKER = re.compile(r"\b(joint venture|jv|associate)\b", re.I)
_CONSOLIDATED_RE = re.compile(r"\bconsolidated\b", re.I)
_STANDALONE_RE = re.compile(r"\bstandalone\b", re.I)


@dataclass(slots=True)
class EntityRef:
    """One reporting unit named by the document."""
    entity_id: str
    name: str
    entity_type: EntityType
    parent_id: str | None = None
    #: Every surface form seen for this unit -- the full legal name, the
    #: abbreviation the document introduced, headings that referred to it.
    aliases: set[str] = field(default_factory=set)
    confidence: float = 0.6
    #: "heading" | "abbreviation" | "filing_company" | "default"
    source: str = "heading"
    first_page: int | None = None
    evidence: str = ""

    def as_dict(self) -> dict:
        return {
            "entity_id": self.entity_id,
            "entity_name": self.name,
            "entity_type": str(self.entity_type),
            "parent_entity_id": self.parent_id,
            "aliases": sorted(self.aliases),
            "confidence": round(self.confidence, 4),
            "source": self.source,
            "first_page": self.first_page,
            "evidence": self.evidence[:300],
        }


def make_entity_id(document_id: str, name: str) -> str:
    """Stable, document-scoped id for an entity name."""
    return stable_id("ent", document_id, name.strip().lower())


def classify(name: str, *, raw_heading: str = "",
             filing_company: str | None = None) -> EntityType:
    """Decide what kind of reporting unit a name denotes.

    Order matters: a heading may carry several signals, and the most specific
    wins.  "Consolidated Jio Platforms Limited" is a *subsidiary* reported on a
    consolidated basis, not the group's consolidated column.
    """
    text = f"{name} {raw_heading}".strip()
    lowered = name.strip().lower()

    if lowered == CONSOLIDATED.lower():
        return EntityType.CONSOLIDATED
    if lowered == STANDALONE.lower():
        return EntityType.STANDALONE
    if filing_company and lowered == filing_company.strip().lower():
        return EntityType.COMPANY
    if _JV_MARKER.search(text):
        return EntityType.JOINT_VENTURE
    if _CORPORATE_SUFFIX.search(name):
        return EntityType.SUBSIDIARY
    if _SEGMENT_MARKER.search(text):
        return EntityType.SEGMENT
    # an abbreviation introduced in a heading, or an all-caps banner
    if name.isupper() or (2 <= len(name) <= 12 and name.isalnum()):
        return EntityType.SEGMENT
    return EntityType.BUSINESS_UNIT


class EntityRegistry:
    """Collects the reporting units a document names, and relates them.

    The registry is built once per document during assembly and then queried by
    the extraction stages, so every fact receives the same entity identity.
    """

    def __init__(self, document_id: str, filing_company: str | None = None):
        self.document_id = document_id
        self.filing_company = (filing_company or "").strip() or None
        self._by_id: dict[str, EntityRef] = {}
        self._by_alias: dict[str, str] = {}

        # The filing company is the root of the hierarchy.  Both reporting
        # bases hang off it, and every other unit hangs off Consolidated.
        self.root_id: str | None = None
        if self.filing_company:
            self.root_id = self._add(self.filing_company, EntityType.COMPANY,
                                     parent_id=None, source="filing_company",
                                     confidence=0.9)
        self.consolidated_id = self._add(
            CONSOLIDATED, EntityType.CONSOLIDATED, parent_id=self.root_id,
            source="default", confidence=0.75,
            evidence="Default reporting basis when no other unit is named.")

    # ------------------------------------------------------------- building
    def _add(self, name: str, entity_type: EntityType, *,
             parent_id: str | None, source: str, confidence: float,
             page: int | None = None, evidence: str = "") -> str:
        name = re.sub(r"\s+", " ", name).strip()
        key = name.lower()
        if key in self._by_alias:
            existing = self._by_id[self._by_alias[key]]
            if page is not None and existing.first_page is None:
                existing.first_page = page
            existing.confidence = max(existing.confidence, confidence)
            return existing.entity_id

        entity_id = make_entity_id(self.document_id, name)
        ref = EntityRef(entity_id=entity_id, name=name, entity_type=entity_type,
                        parent_id=parent_id, confidence=confidence,
                        source=source, first_page=page, evidence=evidence)
        ref.aliases.add(name)
        self._by_id[entity_id] = ref
        self._by_alias[key] = entity_id
        return entity_id

    def register_heading(self, name: str, *, raw_heading: str = "",
                         page: int | None = None,
                         abbreviation: str | None = None) -> str:
        """Register a unit named by a section heading, returning its id."""
        entity_type = classify(name, raw_heading=raw_heading,
                               filing_company=self.filing_company)

        if entity_type == EntityType.CONSOLIDATED:
            return self.consolidated_id
        if entity_type == EntityType.STANDALONE:
            return self._add(STANDALONE, EntityType.STANDALONE,
                             parent_id=self.root_id, source="heading",
                             confidence=0.85, page=page, evidence=raw_heading)
        if entity_type == EntityType.COMPANY:
            # The filer named, with no basis: who is reporting, not on what
            # basis. Never the consolidated group by default (Phase 22).
            return self.root_id or self.register_unstated_basis(
                page=page, evidence=raw_heading or name)

        # Everything else is a unit beneath the consolidated group.
        entity_id = self._add(name, entity_type,
                              parent_id=self.consolidated_id, source="heading",
                              confidence=0.85, page=page, evidence=raw_heading)
        if abbreviation:
            self.add_alias(entity_id, abbreviation)
        return entity_id

    def register_basis(self, basis: EntityType, *, page: int | None = None,
                       evidence: str = "") -> str:
        """Register one of the two group-level reporting bases.

        Used when the basis is declared somewhere other than a section heading
        -- typically the running header of a statement block, which chrome
        detection strips from the content stream.  Consolidated and standalone
        report different totals for the same metric and period, so keeping them
        apart is what stops the two sets of figures from looking like a
        contradiction.
        """
        if basis == EntityType.STANDALONE:
            return self._add(STANDALONE, EntityType.STANDALONE,
                             parent_id=self.root_id, source="page_header",
                             confidence=0.8, page=page, evidence=evidence)
        return self.consolidated_id

    def register_unstated_basis(self, *, page: int | None = None,
                                evidence: str = "") -> str:
        """The filer's figures on a basis the document does not state.

        Distinct from the default: "Consolidated" is where figures go when a
        document names nothing, and a page that names something else in the
        place its siblings name a basis has said it is not that."""
        return self._add(UNSTATED_BASIS, EntityType.UNKNOWN,
                         parent_id=self.root_id, source="page_header",
                         confidence=0.5, page=page, evidence=evidence)

    def add_alias(self, entity_id: str, alias: str) -> None:
        alias = alias.strip()
        if not alias or entity_id not in self._by_id:
            return
        self._by_id[entity_id].aliases.add(alias)
        self._by_alias.setdefault(alias.lower(), entity_id)

    # -------------------------------------------------------------- lookup
    def get(self, entity_id: str | None) -> EntityRef | None:
        return self._by_id.get(entity_id or "")

    def resolve_name(self, name: str | None) -> EntityRef | None:
        """Find a unit by any of its surface forms."""
        if not name:
            return None
        hit = self._by_alias.get(name.strip().lower())
        return self._by_id.get(hit) if hit else None

    def find_in_text(self, text: str,
                     anchor: int | None = None) -> EntityRef | None:
        """Return the unit a sentence explicitly names, if any.

        Only whole-word matches count, and short aliases are matched
        case-sensitively so a three-letter abbreviation cannot hit a common
        word.

        When ``anchor`` is given -- the offset of the metric the sentence is
        about -- the entity mentioned *closest to that metric* wins.  A run of
        bullets frequently reaches this method as one block:

            "Consolidated Revenue at 340,257 crore ... JPL EBITDA up 23.9%"

        Ranking by alias length alone would hand the consolidated revenue
        figure to JPL, or, if the group basis were excluded from matching
        altogether, would do so every time.  Proximity is what actually
        distinguishes them.  Ties break towards the longer, more specific
        alias.
        """
        if not text:
            return None
        best: tuple[int, int, EntityRef] | None = None   # (distance, -len, ref)
        for ref in self._by_id.values():
            for surface in ref.aliases:
                token = surface.strip()
                if len(token) < 2:
                    continue
                flags = re.I if len(token) > 4 else 0
                pattern = re.compile(rf"(?<![\w]){re.escape(token)}(?![\w])",
                                     flags)
                positions = [m.start() for m in pattern.finditer(text)]
                if not positions:
                    continue
                distance = (min(abs(pos - anchor) for pos in positions)
                            if anchor is not None else positions[0])
                candidate = (distance, -len(token), ref)
                if best is None or candidate[:2] < best[:2]:
                    best = candidate
        return best[2] if best else None

    # --------------------------------------------------------------- output
    def all(self) -> list[EntityRef]:
        """Every unit, roots first, then by first appearance."""
        def sort_key(r: EntityRef) -> tuple:
            depth = 0 if r.parent_id is None else (1 if r.parent_id == self.root_id else 2)
            return (depth, r.first_page if r.first_page is not None else 999, r.name)
        return sorted(self._by_id.values(), key=sort_key)

    def hierarchy(self) -> list[dict]:
        """Nested representation for the API and the dashboard."""
        children: dict[str | None, list[EntityRef]] = {}
        for ref in self._by_id.values():
            children.setdefault(ref.parent_id, []).append(ref)

        def build(parent_id: str | None) -> list[dict]:
            out = []
            for ref in sorted(children.get(parent_id, []),
                              key=lambda r: (r.first_page or 999, r.name)):
                node = ref.as_dict()
                node["children"] = build(ref.entity_id)
                out.append(node)
            return out

        return build(None)

    def is_group_level(self, entity_id: str | None) -> bool:
        """True for the consolidated/standalone/company level.

        Segment-versus-total roll-ups compare units *against* this level, so it
        must be identifiable without string matching on names.
        """
        ref = self.get(entity_id)
        return ref is not None and ref.entity_type in (
            EntityType.CONSOLIDATED, EntityType.STANDALONE, EntityType.COMPANY)

    def __len__(self) -> int:
        return len(self._by_id)
