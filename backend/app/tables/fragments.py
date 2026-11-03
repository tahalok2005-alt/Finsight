"""Reuniting one statement that layout reported as several table regions.

A financial statement carries horizontal rules between its sections, and the
layout detector reads a rule as a boundary. Groww's consolidated P&L came back
as four TABLE regions:

    region_c4507e8   y  385- 484   x 183-1383   the header block, almost no data
    region_8c095f0   y  450-1042   x 181-1392   data, no header
    region_afd1a77   y 1009-1521   x 181-1391   data, no header
    region_96a4558   y 1485-1625   x 181-1395   data, no header

Splitting there is not unreasonable -- the bands are real. The damage is done
downstream: the first fragment keeps the column heads and the rest keep the
figures, so every figure loses its period, and the orphaned head, with no
numeric columns left to align against, collapsed three columns' text into one
string and produced a date in 2031.

Rejoining them here is a smaller and safer change than re-tuning the layout
detector, which would move every document in the corpus. The test is purely
geometric and carries no notion of what any particular filer's tables look
like:

* the fragments sit within a small vertical gap of each other, or overlap
* their horizontal extents agree closely
* neither is separated from the other by intervening non-table content

Nothing here decides that a merged band *is* a table. It only offers the whole
band to the table builder, which applies its usual structural tests and is free
to reject the result.
"""
from __future__ import annotations

from dataclasses import dataclass

from app.core.geometry import BBox
from app.core.logging import get_logger
from app.models.enums import RegionType
from app.tables.fragment_identity import assess

log = get_logger("tables.fragments")

#: Vertical gap, as a fraction of page height, still readable as one table.
#: A statement's internal section rules leave a few pixels; a genuinely new
#: table further down the page is separated by a caption or a heading, which
#: `_blocked_between` catches independently of this.
MAX_GAP_RATIO = 0.035
#: How closely two fragments' horizontal extents must agree, as a fraction of
#: the wider one. The Groww fragments agree to within 4px of ~1210.
MIN_EXTENT_AGREEMENT = 0.90
#: Region types that mean the fragments below belong to something else.
_SEPARATORS = frozenset({
    RegionType.SECTION_HEADING, RegionType.TITLE, RegionType.CAPTION,
})


@dataclass(frozen=True, slots=True)
class MergeGroup:
    """A set of regions to offer the table builder as one band."""
    region_ids: tuple[str, ...]
    reason: str
    #: The verdict for each join, in order, as `fragment_identity` recorded it.
    #: Kept so a merged table can say why it was merged and a refused one can
    #: say why it was not.
    verdicts: tuple[dict, ...] = ()

    @property
    def is_merge(self) -> bool:
        return len(self.region_ids) > 1


def _extent_agreement(a: BBox, b: BBox) -> float:
    inner = min(a.x1, b.x1) - max(a.x, b.x)
    outer = max(a.x1, b.x1) - min(a.x, b.x)
    if outer <= 0:
        return 0.0
    return max(0.0, inner / outer)


def _blocked_between(a: BBox, b: BBox, others: list) -> bool:
    """Whether prose or a heading sits between two fragments.

    A heading between two bands means the second is a new table with a name of
    its own. Body text between them means the same. Either way they are not one
    statement, whatever their geometry says.
    """
    top, bottom = (a, b) if a.y1 <= b.y1 else (b, a)
    lo, hi = top.y1, bottom.y
    if hi <= lo:
        return False
    for region in others:
        if region.type not in _SEPARATORS and region.type != RegionType.PARAGRAPH:
            continue
        box = region.bbox.geom
        if box.y >= lo - 2 and box.y1 <= hi + 2:
            # Ignore a stray fragment narrower than a quarter of the band: a
            # footnote marker between two halves of a statement is not a
            # separator.
            if box.width > 0.25 * min(top.width, bottom.width):
                return True
    return False


def group_table_regions(regions: list, page_height: int,
                        page_width: int = 0,
                        lines_by_id: dict | None = None) -> list[MergeGroup]:
    """Group a page's table regions into the tables they actually are.

    ``regions`` is every region on the page, in reading order. Returns one
    group per table, preserving order; a table that was never split comes back
    as a group of one.

    Each join is decided by `fragment_identity.assess`, which requires positive
    structural evidence and refuses on any single piece of contrary evidence.
    The verdict is carried on the group so the merged table can record it.
    """
    tables = [r for r in regions if r.type == RegionType.TABLE]
    if len(tables) < 2:
        return [MergeGroup((r.id,), "single") for r in tables]

    others = [r for r in regions if r.type != RegionType.TABLE]
    ordered = sorted(tables, key=lambda r: r.bbox.geom.y)
    lines_by_id = lines_by_id or {}
    if not page_width:
        page_width = int(max(r.bbox.geom.x1 for r in ordered) or 1)

    groups: list[list] = [[ordered[0]]]
    reasons: list[str] = ["single"]
    verdicts: list[list[dict]] = [[]]

    for region in ordered[1:]:
        previous = groups[-1][-1]
        pbox, rbox = previous.bbox.geom, region.bbox.geom
        lo, hi = pbox.y1, rbox.y
        between = [o for o in others
                   if o.bbox.geom.y >= lo - 2 and o.bbox.geom.y1 <= hi + 2]

        verdict = assess(previous, region, between=between,
                         page_height=page_height, page_width=page_width,
                         lines_by_id=lines_by_id)
        if verdict.merged:
            groups[-1].append(region)
            reasons[-1] = verdict.explain()
            verdicts[-1].append(verdict.as_dict())
        else:
            groups.append([region])
            reasons.append(verdict.explain())
            verdicts.append([])

    out = [MergeGroup(tuple(r.id for r in g), reason, tuple(v))
           for g, reason, v in zip(groups, reasons, verdicts)]
    merged = [g for g in out if g.is_merge]
    if merged:
        log.info("tables.fragments_merged",
                 groups=len(merged),
                 regions=sum(len(g.region_ids) for g in merged),
                 reasons=[g.reason[:90] for g in merged])
    return out
