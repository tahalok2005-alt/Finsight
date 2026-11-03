"""Whether two table fragments are pieces of one logical table.

Phase 19 rejoined statements that layout had split, using geometry alone:
adjacent bands of the same width, nothing between them. That worked on the
corpus and carried a known risk, recorded at the time -- two genuinely separate
tables of the same width with nothing between them would be merged, and the
merge is invisible once made.

This module replaces "looks adjacent" with an evidence model. A merge now has
to be *argued for*, from the same kinds of evidence a person would use, and the
argument is recorded on the merged table so a reader can see why the two halves
were joined.

The asymmetry is deliberate and is the whole design: **positive evidence is
required, and any single piece of contrary evidence is decisive.** Two tables
that a filer meant to be separate almost always say so somewhere -- a title, a
basis declaration, a different unit, a fresh set of column dates -- and one
such statement outweighs any amount of geometric similarity. Geometry alone is
never enough, because geometry is exactly what two consecutive tables of the
same statement share.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from app.core.geometry import BBox
from app.core.logging import get_logger
from app.models.enums import RegionType

log = get_logger("tables.fragment_identity")

#: Vertical gap, as a fraction of page height, still readable as one table.
MAX_GAP_RATIO = 0.035
#: How closely the horizontal extents must agree, as a fraction of the wider.
MIN_EXTENT_AGREEMENT = 0.90
#: How closely two column boundaries must line up to count as the same column,
#: as a fraction of page width.
COLUMN_TOLERANCE_RATIO = 0.012
#: Positive evidence needed before a merge is allowed at all.
MIN_SUPPORTING_EVIDENCE = 2

#: A line that starts a new statement. Any of these between two fragments, or
#: at the top of the lower one, means the lower one is a new table.
_STATEMENT_TITLE = re.compile(
    r"\b(statement\s+of|balance\s+sheet|cash\s*flow|profit\s+and\s+loss|"
    r"segment\s+information|notes?\s+to|financial\s+results|"
    r"assets\s+and\s+liabilities)\b", re.I)
#: A declaration of reporting basis.
_BASIS = re.compile(r"\b(consolidated|standalone)\b", re.I)
#: A unit declaration.
_UNIT_DECL = re.compile(
    r"\b(crore|cr\.?|lakh|million|mn|billion|bn|thousand)\b", re.I)


@dataclass(frozen=True, slots=True)
class Evidence:
    """One reason for or against a merge, in words a reader can check."""
    supports: bool
    code: str
    detail: str

    def as_dict(self) -> dict:
        return {"supports": self.supports, "code": self.code,
                "detail": self.detail}


@dataclass
class MergeVerdict:
    merged: bool
    evidence: list[Evidence] = field(default_factory=list)

    @property
    def supporting(self) -> list[Evidence]:
        return [e for e in self.evidence if e.supports]

    @property
    def contrary(self) -> list[Evidence]:
        return [e for e in self.evidence if not e.supports]

    def explain(self) -> str:
        """The verdict as a sentence, for the table's own record."""
        head = "Merged because" if self.merged else "Not merged because"
        reasons = self.contrary if not self.merged else self.supporting
        if not reasons:
            return f"{head} there was insufficient evidence either way."
        return head + " " + "; ".join(e.detail for e in reasons) + "."

    def as_dict(self) -> dict:
        return {"merged": self.merged,
                "explanation": self.explain(),
                "evidence": [e.as_dict() for e in self.evidence]}


def _extent_agreement(a: BBox, b: BBox) -> float:
    inner = min(a.x1, b.x1) - max(a.x, b.x)
    outer = max(a.x1, b.x1) - min(a.x, b.x)
    return max(0.0, inner / outer) if outer > 0 else 0.0


def _numeric_edges(region, lines_by_id) -> list[float]:
    """Right edges of the numeric fragments in a region.

    A value column is right-aligned, so its figures share a right edge. Two
    fragments of one table share those edges; two different tables that merely
    happen to be the same width do not.
    """
    from app.normalization.numbers import looks_numeric
    edges: list[float] = []
    for line_id in region.meta.get("line_ids", []):
        line = lines_by_id.get(line_id)
        if line is None:
            continue
        for element in line.elements:
            if looks_numeric(element.text):
                edges.append(round(element.bbox.x1, 1))
    return sorted(edges)


#: How many rows of figures a band needs before its column edges are worth
#: comparing. Below this the "columns" are whatever few numbers happen to be
#: there -- a year row, a note reference -- and they say nothing about layout.
MIN_NUMERIC_ROWS_TO_COMPARE_COLUMNS = 3


def _numeric_rows(region, lines_by_id) -> int:
    """How many of a band's lines carry two or more figures."""
    from app.normalization.numbers import looks_numeric
    count = 0
    for line_id in region.meta.get("line_ids", []):
        line = lines_by_id.get(line_id)
        if line is None:
            continue
        figures = sum(1 for e in line.elements if looks_numeric(e.text))
        if figures >= 2:
            count += 1
    return count


def _cluster(edges: list[float], tolerance: float) -> list[float]:
    """Collapse right edges into the columns they imply."""
    out: list[float] = []
    for edge in edges:
        if out and edge - out[-1] <= tolerance:
            continue
        out.append(edge)
    return out


def _columns_agree(upper: list[float], lower: list[float],
                   tolerance: float) -> tuple[bool, int, int]:
    """How many of the lower fragment's columns line up with the upper's."""
    if not upper or not lower:
        return False, 0, max(len(upper), len(lower))
    matched = 0
    for edge in lower:
        if any(abs(edge - other) <= tolerance for other in upper):
            matched += 1
    return matched >= max(2, int(0.75 * len(lower))), matched, len(lower)


def _text_of(region) -> str:
    return (region.text or "").strip()


def _reads_as_column_heads(region, lines_by_id) -> bool:
    """Whether a band reads as column heads: short pieces, most of them pieces
    of a period ("Three months ended June 30,", "2025", "(Unaudited)"), and no
    amount among them. A head may carry other words -- "Convenience
    translation into U.S. Dollar in millions" over a translation column --
    but a band holding a figure is not a head."""
    from app.normalization.numbers import looks_numeric
    from app.normalization.periods import looks_like_period_fragment
    pieces = []
    for line_id in region.meta.get("line_ids", []):
        line = lines_by_id.get(line_id)
        if line is None:
            continue
        pieces += [e.text.strip() for e in line.elements if (e.text or "").strip()]
    if not pieces or any(len(t) > 60 for t in pieces):
        return False
    if any(looks_numeric(t) and not _YEAR.fullmatch(t) for t in pieces):
        return False
    periodic = sum(1 for t in pieces
                   if looks_like_period_fragment(t) or _AUDIT.fullmatch(t))
    return periodic * 2 >= len(pieces)


_YEAR = re.compile(r"\s*(?:19|20)\d{2}\s*")


_AUDIT = re.compile(r"\(?\s*(?:un\s*-?\s*audited|audited|reviewed)\s*\)?", re.I)


def assess(upper, lower, *, between: list, page_height: int, page_width: int,
           lines_by_id: dict) -> MergeVerdict:
    """Decide whether ``lower`` continues ``upper``, and say why.

    ``between`` is every non-table region lying vertically between them.
    """
    evidence: list[Evidence] = []
    upper_box, lower_box = upper.bbox.geom, lower.bbox.geom
    gap = lower_box.y - upper_box.y1
    max_gap = MAX_GAP_RATIO * max(1, page_height)
    tolerance = COLUMN_TOLERANCE_RATIO * max(1, page_width)

    # ---------------------------------------------------- contrary evidence
    # Checked first and completely: one of these settles the question, and a
    # verdict that lists the reason it refused is worth more than an early exit.
    for region in between:
        text = _text_of(region)
        if not text:
            continue
        if region.type in (RegionType.SECTION_HEADING, RegionType.TITLE,
                           RegionType.CAPTION):
            if _STATEMENT_TITLE.search(text):
                evidence.append(Evidence(
                    False, "new_statement_title",
                    f"a new statement begins between them ({text[:60]!r})"))
            elif len(text) > 8:
                evidence.append(Evidence(
                    False, "heading_between",
                    f"a heading separates them ({text[:60]!r})"))
        elif region.type == RegionType.PARAGRAPH and len(text) > 60:
            width = region.bbox.geom.width
            if width > 0.25 * min(upper_box.width, lower_box.width):
                evidence.append(Evidence(
                    False, "prose_between",
                    "body text separates them"))

    lower_text = _text_of(lower)
    upper_text = _text_of(upper)
    if _STATEMENT_TITLE.search(lower_text[:160]):
        evidence.append(Evidence(
            False, "lower_opens_a_statement",
            "the lower band opens with a statement title of its own"))

    upper_basis = {m.group(0).lower() for m in _BASIS.finditer(upper_text[:400])}
    lower_basis = {m.group(0).lower() for m in _BASIS.finditer(lower_text[:200])}
    if lower_basis and upper_basis and lower_basis != upper_basis:
        evidence.append(Evidence(
            False, "different_reporting_basis",
            f"the reporting basis changes ({'/'.join(sorted(upper_basis))} "
            f"then {'/'.join(sorted(lower_basis))})"))

    upper_units = {m.group(0).lower() for m in _UNIT_DECL.finditer(upper_text[:400])}
    lower_units = {m.group(0).lower() for m in _UNIT_DECL.finditer(lower_text[:200])}
    if lower_units and upper_units and not (lower_units & upper_units):
        evidence.append(Evidence(
            False, "different_unit_declaration",
            f"the amounts are declared in different units "
            f"({'/'.join(sorted(upper_units))} then "
            f"{'/'.join(sorted(lower_units))})"))

    if gap > max_gap:
        evidence.append(Evidence(
            False, "vertical_gap",
            f"they are {gap:.0f}px apart, more than the {max_gap:.0f}px a "
            f"single table's internal rules leave"))

    agreement = _extent_agreement(upper_box, lower_box)
    # A band of column heads standing over the lower table's value columns --
    # narrower than the table because it has no label column -- is the
    # table's head. Layout cut one statement's head ("Three months ended June
    # 30," over its years) away from its figures, and the extents test,
    # comparing a head that spans the value columns with a table that also
    # spans its labels, refused to put them back: every figure of the
    # statement lost its period.
    head_over_values = (
        _numeric_rows(upper, lines_by_id) <= 1
        and upper_box.x >= lower_box.x + 0.15 * lower_box.width
        and upper_box.x1 <= lower_box.x1 + tolerance
        and _reads_as_column_heads(upper, lines_by_id))
    if agreement < MIN_EXTENT_AGREEMENT and not head_over_values:
        evidence.append(Evidence(
            False, "extents_disagree",
            f"their horizontal extents agree only {agreement:.0%}"))
    if head_over_values:
        evidence.append(Evidence(
            True, "head_over_value_columns",
            "the upper band is column heads alone, standing over the lower "
            "band's value columns"))

    # ---------------------------------------------------- supporting evidence
    if gap <= max_gap:
        evidence.append(Evidence(
            True, "adjacent",
            f"they are adjacent ({gap:.0f}px apart)"))
    if agreement >= MIN_EXTENT_AGREEMENT:
        evidence.append(Evidence(
            True, "same_extent",
            f"their horizontal extents agree {agreement:.0%}"))

    # Column alignment is only evidence when both bands actually have value
    # columns to compare. A header block's numbers are its year row -- bare,
    # centred over the column -- while a data band's are right-aligned figures,
    # so their edges differ by tens of pixels for a reason that has nothing to
    # do with whether the two belong together. Treating that as contrary
    # evidence refused the very join this module exists to make: a statement's
    # head was left orphaned from its own figures, which is the Phase 19 defect
    # exactly.
    #
    # So the test speaks only when both sides are data-bearing, and stays
    # silent otherwise rather than guessing in either direction.
    upper_rows = _numeric_rows(upper, lines_by_id)
    lower_rows = _numeric_rows(lower, lines_by_id)
    comparable = (upper_rows >= MIN_NUMERIC_ROWS_TO_COMPARE_COLUMNS
                  and lower_rows >= MIN_NUMERIC_ROWS_TO_COMPARE_COLUMNS)
    if comparable:
        upper_cols = _cluster(_numeric_edges(upper, lines_by_id), tolerance)
        lower_cols = _cluster(_numeric_edges(lower, lines_by_id), tolerance)
        aligned, matched, total = _columns_agree(upper_cols, lower_cols,
                                                 tolerance)
        if aligned:
            evidence.append(Evidence(
                True, "columns_align",
                f"{matched} of {total} value columns stand at the same edges"))
        elif upper_cols and lower_cols:
            evidence.append(Evidence(
                False, "columns_do_not_align",
                f"only {matched} of {total} value columns line up"))
    elif upper_rows == 0 or lower_rows == 0:
        # One band carries no figures at all: it is a head, a note or a title
        # block. That is a reason to expect it to belong with its neighbour,
        # not a reason to doubt it.
        evidence.append(Evidence(
            True, "one_band_carries_no_figures",
            "one band holds no figures of its own, so it cannot stand as a "
            "table by itself"))

    if not lower_basis and not _STATEMENT_TITLE.search(lower_text[:160]):
        evidence.append(Evidence(
            True, "lower_has_no_identity_of_its_own",
            "the lower band declares no title, basis or period of its own"))

    # ---------------------------------------------------------------- verdict
    contrary = [e for e in evidence if not e.supports]
    supporting = [e for e in evidence if e.supports]
    merged = not contrary and len(supporting) >= MIN_SUPPORTING_EVIDENCE

    verdict = MergeVerdict(merged=merged, evidence=evidence)
    log.debug("tables.fragment_identity", merged=merged,
              supporting=[e.code for e in supporting],
              contrary=[e.code for e in contrary])
    return verdict
