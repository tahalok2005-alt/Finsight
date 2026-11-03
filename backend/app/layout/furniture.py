"""Telling a document's furniture apart from its content.

A slide deck is built on a master: a logo in one corner, a coloured ribbon
across the top, a footer strip, a rule under the title. Every one of those is
non-text ink in a coloured block, which is exactly what the graphic detector
looks for, so every one of them arrives downstream as a ``chart`` region.

On a 75-page investor presentation that was 123 of 205 chart detections --
60 % of them -- and each was then measured, classified and stored as though it
might hold a financial figure. The footer strip alone was reported as a pie
chart forty-eight times.

What identifies furniture is not what it looks like. It is that **it recurs**:
the same box, at the same place, on many pages. Nothing in a document's actual
content does that.

Recurrence on its own is not enough, though, and this is the part that matters.
A deck will run the same chart template across several consecutive slides with
different data in it -- three pages of the same 2372x857 bar chart, in the
document that prompted this module. Suppressing those would delete real
content. So a second test is required, and it is about substance rather than
shape: **furniture carries no data**. The footer holds a page number; the
repeated chart held thirty-eight recognised elements, twenty of them numeric.

Both tests must pass, and the second is asked twice: once of the template, as
what it *usually* holds, and once of the individual region. A footer strip is
decoration on all forty-eight pages it appears on, but on three of them a
chart's bottom row overlaps the band. Judging the template by its worst
instance would keep all forty-eight; judging every instance by the template
would discard those three rows. Each region is therefore checked against its
own contents as well.

Neither test names a document, a page or a coordinate.
"""
from __future__ import annotations

import statistics
from dataclasses import dataclass, field

from app.core.geometry import BBox
from app.core.logging import get_logger
from app.models.enums import RegionType
from app.models.schemas import OCRElement, Region
from app.normalization.numbers import looks_numeric

log = get_logger("layout.furniture")

#: Below this many pages a repetition is coincidence. Three separate pages
#: placing ink in the same rectangle is a template.
MIN_PAGES_FOR_A_TEMPLATE = 3
#: Positions are compared in units of this share of the page, so the same
#: tolerance means the same thing at any render resolution.
POSITION_TOLERANCE = 0.01
#: A region holding more recognised text than this is carrying content,
#: however often its outline repeats.
MAX_ELEMENTS_IN_FURNITURE = 6
#: A page number is one numeric fragment. Two is generous. Twenty is a chart.
MAX_NUMERIC_IN_FURNITURE = 2


@dataclass(slots=True)
class Template:
    """One recurring position, and what was found inside it."""
    key: tuple[int, int, int, int]
    region_ids: list[str] = field(default_factory=list)
    pages: set[int] = field(default_factory=set)
    #: One instance's size in page pixels. The key is in tolerance units,
    #: which are meaningless to anybody reading the diagnostics.
    width_px: int = 0
    height_px: int = 0
    #: Recognised elements, and numeric ones, per instance of this template.
    elements: list[int] = field(default_factory=list)
    numerics: list[int] = field(default_factory=list)

    @property
    def typical_elements(self) -> float:
        return statistics.median(self.elements) if self.elements else 0.0

    @property
    def typical_numeric(self) -> float:
        return statistics.median(self.numerics) if self.numerics else 0.0

    @property
    def is_furniture(self) -> bool:
        """Whether this recurring position is decoration.

        The median rather than the maximum, because a single page whose content
        overlaps the band should not rescue a footer strip from being a footer
        strip. What that page's own instance holds is a separate question, and
        ``find_templates`` asks it separately.
        """
        return (len(self.pages) >= MIN_PAGES_FOR_A_TEMPLATE
                and self.typical_elements <= MAX_ELEMENTS_IN_FURNITURE
                and self.typical_numeric <= MAX_NUMERIC_IN_FURNITURE)

    def describe(self) -> str:
        return (f"the same {self.width_px}x{self.height_px} px block appears "
                f"at this position on {len(self.pages)} pages and usually "
                f"holds {self.typical_elements:.0f} recognised element(s), so "
                f"it is part of the page template rather than content")


def _bucket(box: BBox, width: int, height: int) -> tuple[int, int, int, int]:
    """Quantise a box so near-identical placements land in one bucket."""
    step_x = max(1.0, width * POSITION_TOLERANCE)
    step_y = max(1.0, height * POSITION_TOLERANCE)
    return (int(round(box.x / step_x)), int(round(box.y / step_y)),
            int(round(box.width / step_x)), int(round(box.height / step_y)))


def find_templates(pages: list[tuple[int, int, int, list[Region],
                                     list[OCRElement]]],
                   *, types: tuple[RegionType, ...]) -> dict[str, Template]:
    """Map each region of ``types`` that is page furniture to its template.

    ``pages`` is one tuple per page: page number, render width, render height,
    the page's regions and its recognised elements. Only regions whose id
    appears in the result are furniture; everything else is left alone.
    """
    templates: dict[tuple[int, int, int, int], Template] = {}
    #: What each individual region turned out to hold.
    content: dict[str, tuple[int, int]] = {}
    for page_number, width, height, regions, elements in pages:
        if width <= 0 or height <= 0:
            continue
        boxes = [(e, BBox.from_dict(e.bbox.model_dump())) for e in elements]
        for region in regions:
            if region.type not in types:
                continue
            box = region.bbox.geom
            key = _bucket(box, width, height)
            t = templates.setdefault(key, Template(key=key))
            t.width_px = int(round(box.width))
            t.height_px = int(round(box.height))
            t.region_ids.append(region.id)
            t.pages.add(page_number)
            inside = [e for e, b in boxes if box.contains_ratio(b) > 0.6]
            t.elements.append(len(inside))
            t.numerics.append(sum(1 for e in inside if looks_numeric(e.text)))
            content[region.id] = (t.elements[-1], t.numerics[-1])

    furniture: dict[str, Template] = {}
    for t in templates.values():
        if not t.is_furniture:
            continue
        for region_id in t.region_ids:
            # This instance may be the one where real content overlaps the
            # band. Decoration is a claim about a rectangle on a page, not
            # only about the rectangle.
            n_elements, n_numeric = content.get(region_id, (0, 0))
            if (n_elements > MAX_ELEMENTS_IN_FURNITURE
                    or n_numeric > MAX_NUMERIC_IN_FURNITURE):
                continue
            furniture[region_id] = t
    if furniture:
        log.info("furniture.detected", regions=len(furniture),
                 templates=len({id(t) for t in furniture.values()}))
    return furniture


def group_by_template(furniture: dict[str, Template]) -> list[Template]:
    """The distinct templates behind a furniture map, for diagnostics."""
    seen: dict[tuple[int, int, int, int], Template] = {}
    for t in furniture.values():
        seen[t.key] = t
    return sorted(seen.values(), key=lambda t: -len(t.pages))
