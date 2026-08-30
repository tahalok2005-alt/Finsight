"""Chart and figure understanding.

What this module does and does not claim
---------------------------------------
It detects graphical regions from the page image, classifies the chart family
from visual evidence (bar geometry, radial ink, line continuity, colour
palette), and harvests the text that sits inside and around the chart -- title,
axis labels, legend entries, categories and any printed data labels.

It does **not** measure bar heights against an axis to invent values.  Deriving
a number from pixel geometry is unreliable at this resolution and would produce
confident-looking financial figures with no basis in the document.  So:

* a value is recorded only when it is *printed* on the chart as a data label and
  read by OCR;
* charts whose values were not printed are stored with their labels, marked
  ``values_extracted = False`` and ``requires_review = True``;
* ``notes`` states plainly why no values were extracted.

An uncertain result that says so is worth more than a fabricated one.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

import cv2
import numpy as np

from app.core.geometry import BBox
from app.core.ids import stable_id
from app.core.logging import get_logger
from app.layout.cv_signals import binarize, to_gray
from app.models.enums import ChartType, MagnitudeUnit, RegionType
from app.models.schemas import (
    BBoxModel, ChartSeriesPoint, ExtractedChart, OCRElement, Region,
)
from app.normalization.numbers import detect_magnitude, looks_numeric, parse_number
from app.ocr.lines import TextLine

log = get_logger("charts.detector")

AXIS_LABEL_RE = re.compile(r"^[A-Za-z][\w %/().-]{0,40}$")
LEGEND_HINT_RE = re.compile(r"\b(FY\d{2}|Q[1-4]|\d{4}|actual|target|total)\b", re.I)


@dataclass(slots=True)
class ChartEvidence:
    n_vertical_bars: int = 0
    n_horizontal_bars: int = 0
    radial_score: float = 0.0
    line_score: float = 0.0
    color_ratio: float = 0.0
    distinct_colors: int = 0
    gridline_count: int = 0


def _rect_candidates(roi_bin: np.ndarray, *, vertical: bool) -> int:
    """Count filled rectangles consistent with bars in the given orientation."""
    h, w = roi_bin.shape[:2]
    if h < 20 or w < 20:
        return 0
    contours, _ = cv2.findContours(roi_bin, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    count = 0
    for c in contours:
        x, y, cw, ch = cv2.boundingRect(c)
        area = cw * ch
        if area < (h * w) * 0.002:
            continue
        fill = cv2.contourArea(c) / max(1.0, float(area))
        if fill < 0.65:  # bars are solid rectangles
            continue
        if vertical and ch > cw * 1.4 and ch > h * 0.08:
            count += 1
        elif not vertical and cw > ch * 1.4 and cw > w * 0.08:
            count += 1
    return count


def analyse_region(image: np.ndarray, box: BBox) -> ChartEvidence:
    x0, y0 = max(0, int(box.x)), max(0, int(box.y))
    x1 = min(image.shape[1], int(np.ceil(box.x1)))
    y1 = min(image.shape[0], int(np.ceil(box.y1)))
    if x1 <= x0 or y1 <= y0:
        return ChartEvidence()
    roi = image[y0:y1, x0:x1]
    roi_bin = binarize(roi)
    hsv = cv2.cvtColor(roi, cv2.COLOR_RGB2HSV)
    sat = hsv[:, :, 1]
    color_mask = (sat > 60).astype(np.uint8) * 255
    combined = cv2.bitwise_or(roi_bin, color_mask)

    ev = ChartEvidence()
    ev.n_vertical_bars = _rect_candidates(combined, vertical=True)
    ev.n_horizontal_bars = _rect_candidates(combined, vertical=False)
    ev.color_ratio = float(np.count_nonzero(color_mask)) / max(1, color_mask.size)
    q = (roi // 64).reshape(-1, 3)
    ev.distinct_colors = int(len(np.unique(q, axis=0))) if q.size else 0

    # radial evidence: circles indicate a pie or donut
    gray = to_gray(roi)
    min_dim = min(roi.shape[0], roi.shape[1])
    if min_dim >= 60:
        circles = cv2.HoughCircles(
            cv2.medianBlur(gray, 5), cv2.HOUGH_GRADIENT, dp=1.2,
            minDist=min_dim // 2, param1=110, param2=55,
            minRadius=int(min_dim * 0.18), maxRadius=int(min_dim * 0.52))
        ev.radial_score = 0.0 if circles is None else min(1.0, len(circles[0]) / 2.0)

    # line evidence: long non-axis-aligned segments
    edges = cv2.Canny(gray, 60, 160)
    segs = cv2.HoughLinesP(edges, 1, np.pi / 180, threshold=45,
                           minLineLength=max(20, min_dim // 4), maxLineGap=8)
    diagonal = 0
    grid = 0
    if segs is not None and segs.size:
        # HoughLinesP returns (N,1,4); reshape rather than index so a single
        # detected segment does not collapse to a 1-D array.
        for sx, sy, ex, ey in segs.reshape(-1, 4):
            dx, dy = abs(ex - sx), abs(ey - sy)
            if dx == 0 and dy == 0:
                continue
            if dy <= 2 or dx <= 2:
                grid += 1
            elif dy / max(dx, 1) > 0.12:
                diagonal += 1
    ev.line_score = min(1.0, diagonal / 12.0)
    ev.gridline_count = grid
    return ev


def classify(ev: ChartEvidence) -> tuple[ChartType, float]:
    """Pick the most likely chart family with an honest confidence."""
    scores: dict[ChartType, float] = {}
    if ev.radial_score > 0.4:
        scores[ChartType.PIE] = 0.5 + 0.4 * ev.radial_score
    if ev.n_vertical_bars >= 3:
        scores[ChartType.COLUMN] = min(0.85, 0.4 + 0.08 * ev.n_vertical_bars)
    if ev.n_horizontal_bars >= 3:
        scores[ChartType.BAR] = min(0.85, 0.4 + 0.08 * ev.n_horizontal_bars)
    if ev.line_score > 0.35 and ev.n_vertical_bars < 3:
        scores[ChartType.LINE] = min(0.8, 0.35 + 0.45 * ev.line_score)
    if not scores:
        return ChartType.UNKNOWN, 0.25
    best = max(scores, key=lambda k: scores[k])
    conf = scores[best]
    # several families scoring similarly means we are not actually sure
    ranked = sorted(scores.values(), reverse=True)
    if len(ranked) > 1 and ranked[0] - ranked[1] < 0.12:
        conf *= 0.75
    return best, round(min(0.9, conf), 3)


def extract_chart(*, document_id: str, page_number: int, region: Region,
                  image: np.ndarray, lines: list[TextLine],
                  chart_index: int, nearby_title: str = "",
                  ) -> ExtractedChart:
    """Build a chart record from a detected graphical region."""
    box = region.bbox.geom
    ev = analyse_region(image, box)
    ctype, type_conf = classify(ev)

    inside: list[TextLine] = []
    around: list[TextLine] = []
    probe = box.padded(max(18.0, box.height * 0.12))
    for ln in lines:
        if box.contains_ratio(ln.bbox) > 0.6:
            inside.append(ln)
        elif probe.contains_ratio(ln.bbox) > 0.5:
            around.append(ln)

    title = nearby_title.strip()
    if not title and around:
        above = [ln for ln in around if ln.bbox.y1 <= box.y + 4]
        if above:
            title = max(above, key=lambda ln: (ln.bbox.y1, ln.font_size)).text.strip()

    data_labels: list[ChartSeriesPoint] = []
    categories: list[str] = []
    legend: list[str] = []
    axis_labels: list[str] = []
    unit_hint: MagnitudeUnit | None = detect_magnitude(f"{title} " + " ".join(
        ln.text for ln in around))

    for ln in inside:
        text = ln.text.strip()
        if not text:
            continue
        if looks_numeric(text):
            parsed = parse_number(text)
            if parsed is not None:
                data_labels.append(ChartSeriesPoint(
                    label=None, value=parsed[0], value_source="data_label",
                    confidence=round(ln.confidence, 4),
                    bbox=BBoxModel(**ln.bbox.as_dict())))
            continue
        if LEGEND_HINT_RE.search(text) and len(text) <= 24:
            legend.append(text)
        elif AXIS_LABEL_RE.match(text):
            categories.append(text)

    for ln in around:
        t = ln.text.strip()
        if t and t != title and len(t) <= 60 and not looks_numeric(t):
            axis_labels.append(t)

    values_extracted = bool(data_labels)
    if values_extracted:
        notes = (f"{len(data_labels)} value(s) read from data labels printed on "
                 f"the chart. Series-to-value assignment is positional and "
                 f"unverified.")
        confidence = round(min(0.7, 0.4 + 0.05 * len(data_labels)), 3)
    else:
        notes = ("No printed data labels were found. Values are not derived from "
                 "bar or point geometry because that cannot be done reliably at "
                 "this resolution; only the chart's text has been captured.")
        confidence = round(min(0.5, type_conf), 3)

    return ExtractedChart(
        id=stable_id("chart", document_id, page_number, chart_index),
        document_id=document_id, page_number=page_number, region_id=region.id,
        chart_type=ctype, type_confidence=type_conf, title=title[:300],
        axis_labels=axis_labels[:8], legend_labels=legend[:12],
        categories=categories[:24], data_labels=data_labels[:60],
        unit_hint=unit_hint, bbox=region.bbox, confidence=confidence,
        values_extracted=values_extracted,
        # Always true: even a well-read chart's series mapping is unverified.
        requires_review=True, notes=notes)


def detect_charts(*, document_id: str, page_number: int, regions: list[Region],
                  image: np.ndarray, lines: list[TextLine]) -> list[ExtractedChart]:
    out: list[ExtractedChart] = []
    idx = 0
    for region in regions:
        if region.type not in (RegionType.CHART, RegionType.FIGURE):
            continue
        title = ""
        for other in regions:
            if other.type in (RegionType.CAPTION, RegionType.SECTION_HEADING):
                if abs(other.bbox.y1 - region.bbox.y) < 60 and \
                        other.bbox.h_overlap_ratio(region.bbox) > 0.3:
                    title = other.text
                    break
        try:
            out.append(extract_chart(
                document_id=document_id, page_number=page_number, region=region,
                image=image, lines=lines, chart_index=idx, nearby_title=title))
            idx += 1
        except Exception as exc:  # noqa: BLE001 - one bad region must not fail the page
            log.exception("charts.extract_failed", page=page_number,
                          region_id=region.id, error=str(exc))
    return out
