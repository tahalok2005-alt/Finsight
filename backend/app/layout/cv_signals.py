"""Computer-vision signals extracted from the rendered page image.

Everything here works on pixels, so it behaves identically for a born-digital
PDF and a scan.  These signals are what make the pipeline *visual* rather than
text-based: ruling lines delimit tables, ink density separates figures from
prose, colour saturation flags charts, and whitespace valleys reveal columns.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import cv2
import numpy as np

from app.core.geometry import BBox


@dataclass(slots=True)
class LineSegment:
    bbox: BBox
    orientation: str  # "h" | "v"
    length: float
    thickness: float


@dataclass(slots=True)
class GraphicBlob:
    bbox: BBox
    area: float
    fill_ratio: float
    color_ratio: float
    edge_density: float
    n_components: int = 0
    distinct_colors: int = 0


@dataclass(slots=True)
class PageSignals:
    width: int
    height: int
    h_lines: list[LineSegment] = field(default_factory=list)
    v_lines: list[LineSegment] = field(default_factory=list)
    graphics: list[GraphicBlob] = field(default_factory=list)
    ink_ratio: float = 0.0
    mean_color_saturation: float = 0.0


#: Colours are quantised to 2 bits per channel, so a region can hold at most
#: 64 distinct values. That bound is what makes the count cheap.
_COLOUR_LEVELS = 64


def distinct_quantised_colours(rgb: np.ndarray) -> int:
    """How many of the 64 quantised colours appear in this image.

    A chart has a small palette of several strong colours, a photograph has
    many, plain text has one. The measure is unchanged from the obvious
    implementation -- len(np.unique(rgb // 64, axis=0)) -- and returns the
    identical number; only the route differs.

    That route matters: the obvious form lexicographically sorts several
    million RGB triples and, on this document, accounted for 85.6% of all
    chart-processing time. Packing three 2-bit channels into one 6-bit index
    and counting occupied bins is the same question asked of an array that
    already knows it has 64 possible answers.
    """
    if rgb.size == 0:
        return 0
    q = (rgb // 64).reshape(-1, 3).astype(np.uint16)
    packed = (q[:, 0] << 4) | (q[:, 1] << 2) | q[:, 2]
    return int(np.count_nonzero(np.bincount(packed, minlength=_COLOUR_LEVELS)))


def to_gray(image: np.ndarray) -> np.ndarray:
    if image.ndim == 2:
        return image
    return cv2.cvtColor(image, cv2.COLOR_RGB2GRAY)


def binarize(image: np.ndarray) -> np.ndarray:
    """Ink mask: 255 where there is ink, 0 for background.

    Otsu on the inverted image handles both crisp digital renders and greyer
    scans without a hand-tuned threshold.
    """
    gray = to_gray(image)
    _, binary = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)
    return binary


def detect_ruling_lines(image: np.ndarray, *, min_len_ratio: float = 0.10
                        ) -> tuple[list[LineSegment], list[LineSegment]]:
    """Find horizontal and vertical ruling lines by directional morphology.

    A long thin run of ink that survives erosion with a 1-D kernel is a rule,
    not text.  Table borders, underlines and column separators all appear here.
    """
    h, w = image.shape[:2]
    binary = binarize(image)

    h_min = max(20, int(w * min_len_ratio))
    v_min = max(20, int(h * min_len_ratio * 0.6))

    h_kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (h_min, 1))
    v_kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (1, v_min))

    h_img = cv2.morphologyEx(binary, cv2.MORPH_OPEN, h_kernel, iterations=1)
    v_img = cv2.morphologyEx(binary, cv2.MORPH_OPEN, v_kernel, iterations=1)

    def _extract(mask: np.ndarray, orientation: str) -> list[LineSegment]:
        out: list[LineSegment] = []
        n, _, stats, _ = cv2.connectedComponentsWithStats(mask, connectivity=8)
        for i in range(1, n):
            x, y, ww, hh, area = stats[i]
            if area <= 0:
                continue
            if orientation == "h":
                if ww < h_min or hh > max(6, h * 0.01):
                    continue
                length, thickness = float(ww), float(hh)
            else:
                if hh < v_min or ww > max(6, w * 0.01):
                    continue
                length, thickness = float(hh), float(ww)
            out.append(LineSegment(
                bbox=BBox(float(x), float(y), float(ww), float(hh)),
                orientation=orientation, length=length, thickness=thickness))
        return out

    return _extract(h_img, "h"), _extract(v_img, "v")


def detect_graphics(image: np.ndarray, *, text_mask: np.ndarray | None = None,
                    min_area_ratio: float = 0.004) -> list[GraphicBlob]:
    """Find non-text graphical blocks (charts, logos, figures, coloured panels).

    Text pixels are removed first (using the OCR boxes when supplied) so prose
    cannot masquerade as a figure.  Remaining ink is dilated into blocks; each
    block is scored by colour content and edge density, which is what separates
    a chart from an incidental rule or a stray mark.
    """
    h, w = image.shape[:2]
    page_area = float(h * w)
    binary = binarize(image)

    if text_mask is not None:
        binary = cv2.bitwise_and(binary, cv2.bitwise_not(text_mask))

    # colour/saturation channel: charts are usually the only coloured content
    hsv = cv2.cvtColor(image, cv2.COLOR_RGB2HSV)
    sat = hsv[:, :, 1]
    color_mask = (sat > 60).astype(np.uint8) * 255

    combined = cv2.bitwise_or(binary, color_mask)
    kernel = cv2.getStructuringElement(
        cv2.MORPH_RECT, (max(6, w // 90), max(6, h // 130)))
    blocks = cv2.morphologyEx(combined, cv2.MORPH_CLOSE, kernel, iterations=2)

    edges = cv2.Canny(to_gray(image), 60, 160)

    out: list[GraphicBlob] = []
    n, labels, stats, _ = cv2.connectedComponentsWithStats(blocks, connectivity=8)
    for i in range(1, n):
        x, y, ww, hh, area = stats[i]
        if area / page_area < min_area_ratio:
            continue
        if ww < w * 0.05 or hh < h * 0.02:
            continue
        box = BBox(float(x), float(y), float(ww), float(hh))
        roi_ink = binary[y:y + hh, x:x + ww]
        roi_color = color_mask[y:y + hh, x:x + ww]
        roi_edges = edges[y:y + hh, x:x + ww]
        roi_rgb = image[y:y + hh, x:x + ww]
        denom = float(ww * hh) or 1.0
        distinct = distinct_quantised_colours(roi_rgb)
        out.append(GraphicBlob(
            bbox=box, area=float(area),
            fill_ratio=float(np.count_nonzero(roi_ink)) / denom,
            color_ratio=float(np.count_nonzero(roi_color)) / denom,
            edge_density=float(np.count_nonzero(roi_edges)) / denom,
            n_components=int(np.count_nonzero(labels[y:y + hh, x:x + ww] == i)),
            distinct_colors=distinct,
        ))
    return out


def build_text_mask(image_shape: tuple[int, int], boxes: list[BBox],
                    pad: float = 2.0) -> np.ndarray:
    """A binary mask covering every OCR box, used to subtract text from ink."""
    h, w = image_shape[:2]
    mask = np.zeros((h, w), dtype=np.uint8)
    for b in boxes:
        bb = b.padded(pad).clipped(w, h)
        x0, y0 = int(bb.x), int(bb.y)
        x1, y1 = int(np.ceil(bb.x1)), int(np.ceil(bb.y1))
        if x1 > x0 and y1 > y0:
            mask[y0:y1, x0:x1] = 255
    return mask


def vertical_whitespace_columns(boxes: list[BBox], page_width: int,
                                *, min_gap_ratio: float = 0.035,
                                margin_ratio: float = 0.04) -> list[tuple[float, float]]:
    """Detect column bands from gaps in the horizontal projection of text boxes.

    Returns ``[(x_start, x_end), ...]``.  A single-column page yields one band.
    """
    if not boxes:
        return [(0.0, float(page_width))]
    profile = np.zeros(page_width + 1, dtype=np.int32)
    for b in boxes:
        x0 = max(0, int(b.x))
        x1 = min(page_width, int(np.ceil(b.x1)))
        if x1 > x0:
            profile[x0:x1] += 1

    margin = int(page_width * margin_ratio)
    min_gap = max(8, int(page_width * min_gap_ratio))

    bands: list[tuple[float, float]] = []
    in_band = False
    start = 0
    gap_run = 0
    for x in range(margin, page_width - margin + 1):
        occupied = profile[x] > 0
        if occupied:
            if not in_band:
                in_band, start = True, x
            gap_run = 0
        else:
            gap_run += 1
            if in_band and gap_run >= min_gap:
                bands.append((float(start), float(x - gap_run)))
                in_band = False
    if in_band:
        bands.append((float(start), float(page_width - margin)))

    if not bands:
        return [(0.0, float(page_width))]
    # merge bands narrower than 12% of the page into their neighbour: those are
    # indentation artefacts, not real columns
    merged: list[list[float]] = [list(bands[0])]
    for b in bands[1:]:
        prev = merged[-1]
        if (b[0] - prev[1]) < page_width * 0.06 or (b[1] - b[0]) < page_width * 0.12:
            prev[1] = b[1]
        else:
            merged.append(list(b))
    return [(m[0], m[1]) for m in merged]


def page_signals(image: np.ndarray, text_boxes: list[BBox] | None = None,
                 *, min_len_ratio: float = 0.10) -> PageSignals:
    h, w = image.shape[:2]
    h_lines, v_lines = detect_ruling_lines(image, min_len_ratio=min_len_ratio)
    mask = build_text_mask((h, w), text_boxes) if text_boxes else None
    graphics = detect_graphics(image, text_mask=mask)
    binary = binarize(image)
    hsv = cv2.cvtColor(image, cv2.COLOR_RGB2HSV)
    return PageSignals(
        width=w, height=h, h_lines=h_lines, v_lines=v_lines, graphics=graphics,
        ink_ratio=float(np.count_nonzero(binary)) / float(h * w),
        mean_color_saturation=float(hsv[:, :, 1].mean()) / 255.0,
    )
