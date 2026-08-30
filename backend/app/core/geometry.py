"""Bounding-box geometry used across OCR, layout, tables and provenance.

Coordinate convention
---------------------
All persisted boxes are in **rendered-image pixel space** of the page at the
render DPI recorded on the page row.  ``BBox.to_relative`` converts to
resolution-independent 0..1 fractions for the frontend overlay, which is the
only representation the UI needs, so the UI never depends on the render DPI.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Sequence


@dataclass(frozen=True, slots=True)
class BBox:
    x: float
    y: float
    width: float
    height: float

    # ----------------------------------------------------------- constructors
    @classmethod
    def from_xyxy(cls, x0: float, y0: float, x1: float, y1: float) -> "BBox":
        return cls(min(x0, x1), min(y0, y1), abs(x1 - x0), abs(y1 - y0))

    @classmethod
    def from_polygon(cls, poly: Sequence[Sequence[float]]) -> "BBox":
        xs = [float(p[0]) for p in poly]
        ys = [float(p[1]) for p in poly]
        return cls.from_xyxy(min(xs), min(ys), max(xs), max(ys))

    @classmethod
    def union_all(cls, boxes: Iterable["BBox"]) -> "BBox | None":
        items = list(boxes)
        if not items:
            return None
        return cls.from_xyxy(
            min(b.x for b in items), min(b.y for b in items),
            max(b.x1 for b in items), max(b.y1 for b in items),
        )

    # ------------------------------------------------------------- properties
    @property
    def x1(self) -> float:
        return self.x + self.width

    @property
    def y1(self) -> float:
        return self.y + self.height

    @property
    def cx(self) -> float:
        return self.x + self.width / 2.0

    @property
    def cy(self) -> float:
        return self.y + self.height / 2.0

    @property
    def area(self) -> float:
        return max(0.0, self.width) * max(0.0, self.height)

    # ---------------------------------------------------------------- methods
    def as_dict(self) -> dict[str, float]:
        return {"x": round(self.x, 2), "y": round(self.y, 2),
                "width": round(self.width, 2), "height": round(self.height, 2)}

    @classmethod
    def from_dict(cls, d: dict) -> "BBox":
        return cls(float(d["x"]), float(d["y"]), float(d["width"]), float(d["height"]))

    def to_relative(self, page_w: float, page_h: float) -> dict[str, float]:
        if page_w <= 0 or page_h <= 0:
            return {"x": 0.0, "y": 0.0, "width": 0.0, "height": 0.0}
        return {"x": round(self.x / page_w, 6), "y": round(self.y / page_h, 6),
                "width": round(self.width / page_w, 6),
                "height": round(self.height / page_h, 6)}

    def scaled(self, factor: float) -> "BBox":
        return BBox(self.x * factor, self.y * factor,
                    self.width * factor, self.height * factor)

    def padded(self, pad: float) -> "BBox":
        return BBox(self.x - pad, self.y - pad,
                    self.width + 2 * pad, self.height + 2 * pad)

    def clipped(self, w: float, h: float) -> "BBox":
        x0, y0 = max(0.0, self.x), max(0.0, self.y)
        x1, y1 = min(w, self.x1), min(h, self.y1)
        return BBox.from_xyxy(x0, y0, max(x0, x1), max(y0, y1))

    def intersection(self, other: "BBox") -> "BBox | None":
        x0, y0 = max(self.x, other.x), max(self.y, other.y)
        x1, y1 = min(self.x1, other.x1), min(self.y1, other.y1)
        if x1 <= x0 or y1 <= y0:
            return None
        return BBox.from_xyxy(x0, y0, x1, y1)

    def iou(self, other: "BBox") -> float:
        inter = self.intersection(other)
        if inter is None:
            return 0.0
        union = self.area + other.area - inter.area
        return inter.area / union if union > 0 else 0.0

    def contains_ratio(self, other: "BBox") -> float:
        """Fraction of *other* that lies inside self."""
        inter = self.intersection(other)
        if inter is None or other.area <= 0:
            return 0.0
        return inter.area / other.area

    def h_overlap(self, other: "BBox") -> float:
        lo, hi = max(self.x, other.x), min(self.x1, other.x1)
        return max(0.0, hi - lo)

    def v_overlap(self, other: "BBox") -> float:
        lo, hi = max(self.y, other.y), min(self.y1, other.y1)
        return max(0.0, hi - lo)

    def v_overlap_ratio(self, other: "BBox") -> float:
        m = min(self.height, other.height)
        return self.v_overlap(other) / m if m > 0 else 0.0

    def h_overlap_ratio(self, other: "BBox") -> float:
        m = min(self.width, other.width)
        return self.h_overlap(other) / m if m > 0 else 0.0
