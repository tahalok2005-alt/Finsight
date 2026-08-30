"""Group OCR elements into visual lines.

PP-OCR emits one element per detected text *fragment*.  On a financial page a
single visual row of a table arrives as many separate fragments (the row label
plus one fragment per numeric column).  Grouping them into lines -- while
keeping every fragment's own box -- is the foundation for reading order, table
row detection and narrative reconstruction.

Nothing here merges the fragments' text destructively: a line holds references
to its members, and each member keeps its own coordinates and confidence.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from statistics import median

from app.core.geometry import BBox
from app.core.ids import stable_id
from app.models.schemas import OCRElement


@dataclass(slots=True)
class TextLine:
    id: str
    page_number: int
    elements: list[OCRElement] = field(default_factory=list)
    bbox: BBox = field(default_factory=lambda: BBox(0, 0, 0, 0))

    @property
    def text(self) -> str:
        return " ".join(e.text for e in self.elements).strip()

    @property
    def confidence(self) -> float:
        if not self.elements:
            return 0.0
        return sum(e.confidence for e in self.elements) / len(self.elements)

    @property
    def height(self) -> float:
        return self.bbox.height

    @property
    def font_size(self) -> float:
        sizes = [e.font_size_px for e in self.elements if e.font_size_px]
        return median(sizes) if sizes else self.bbox.height

    @property
    def is_bold(self) -> bool:
        flags = [e.is_bold for e in self.elements if e.is_bold is not None]
        return bool(flags) and sum(flags) / len(flags) >= 0.5

    def element_ids(self) -> list[str]:
        return [e.id for e in self.elements]


def _recompute(line: TextLine) -> None:
    line.elements.sort(key=lambda e: e.bbox.x)
    box = BBox.union_all(e.bbox.geom for e in line.elements)
    if box is not None:
        line.bbox = box


def group_lines(elements: list[OCRElement], *, document_id: str,
                page_number: int, overlap_ratio: float = 0.45) -> list[TextLine]:
    """Cluster elements into visual lines by vertical overlap.

    Uses overlap of the *vertical extent* rather than a fixed y-tolerance, so it
    adapts to mixed font sizes on the same page (headline vs 7pt footnote).
    """
    if not elements:
        return []

    items = sorted(elements, key=lambda e: (e.bbox.y, e.bbox.x))
    lines: list[TextLine] = []

    for el in items:
        ebox = el.bbox.geom
        placed = False
        # only the most recent few lines can plausibly match a y-sorted stream
        for line in reversed(lines[-6:]):
            if line.bbox.v_overlap_ratio(ebox) >= overlap_ratio:
                line.elements.append(el)
                _recompute(line)
                placed = True
                break
        if not placed:
            line = TextLine(
                id=stable_id("line", document_id, page_number, len(lines),
                             round(ebox.y, 1)),
                page_number=page_number, elements=[el], bbox=ebox)
            lines.append(line)

    lines.sort(key=lambda ln: (round(ln.bbox.y, 1), ln.bbox.x))
    for idx, line in enumerate(lines):
        line.id = stable_id("line", document_id, page_number, idx,
                            round(line.bbox.y, 1))
        for el in line.elements:
            el.line_id = line.id
    return lines


def median_line_height(lines: list[TextLine]) -> float:
    heights = [ln.height for ln in lines if ln.height > 0]
    return median(heights) if heights else 12.0


def median_gap(lines: list[TextLine]) -> float:
    """Median vertical gap between consecutive lines (paragraph-break signal)."""
    gaps = []
    for prev, cur in zip(lines, lines[1:]):
        g = cur.bbox.y - prev.bbox.y1
        if g > 0:
            gaps.append(g)
    return median(gaps) if gaps else 4.0
