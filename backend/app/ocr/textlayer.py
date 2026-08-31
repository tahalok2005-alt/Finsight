"""Auxiliary evidence channel: the PDF text layer.

This is deliberately **not** the primary extraction path.  It is used to:

1. *Corroborate* OCR readings.  When the glyph stream and the pixel recogniser
   independently agree on a token, confidence in that token rises; when they
   disagree, the disagreement is recorded and the item becomes review-worthy.
2. *Recover glyphs the recogniser cannot emit* -- most importantly currency
   symbols such as the rupee sign, which are outside the PP-OCR charset.
3. Supply typography (font size, bold) that pixels alone would only approximate,
   which improves heading/section detection on digital PDFs.

For scanned pages this channel is simply empty and the pipeline proceeds on
pixels alone -- nothing downstream requires it.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

import pymupdf

from app.core.geometry import BBox
from app.core.logging import get_logger
from app.core.modes import NativeTextScope, guard_native_text

log = get_logger("ocr.textlayer")

_NORM_STRIP = re.compile(r"[^0-9a-z%.,()/-]+")


@dataclass(slots=True)
class TextLayerWord:
    text: str
    bbox: BBox          # already scaled into rendered-image pixel space
    font_size: float
    is_bold: bool
    block_no: int
    line_no: int
    word_no: int


def normalize_token(text: str) -> str:
    """Aggressive normalisation used only for OCR/text-layer agreement tests."""
    t = text.lower().strip()
    t = t.replace("₹", "").replace(" ", " ")
    t = _NORM_STRIP.sub("", t)
    return t


def extract_words(pdf_path: str | Path, page_number: int,
                  scale: float) -> list[TextLayerWord]:
    """Extract words with boxes scaled by ``scale`` into image pixel space.

    ``scale`` must be ``dpi / 72`` for the render that produced the page image,
    so text-layer boxes and OCR boxes share one coordinate frame.

    This is the CORROBORATION scope: its output feeds layout typography and
    currency recovery, so it genuinely reaches extraction and is refused in
    VISUAL_ONLY mode rather than being silently skipped.
    """
    guard_native_text("ocr.textlayer.extract_words",
                      NativeTextScope.CORROBORATION,
                      detail=f"page={page_number}")
    words: list[TextLayerWord] = []
    try:
        with pymupdf.open(pdf_path) as doc:
            if page_number < 1 or page_number > doc.page_count:
                return words
            page = doc[page_number - 1]
            data = page.get_text("dict")
            for b_no, block in enumerate(data.get("blocks", [])):
                if block.get("type") != 0:  # 0 == text block
                    continue
                for l_no, line in enumerate(block.get("lines", [])):
                    for w_no, span in enumerate(line.get("spans", [])):
                        text = (span.get("text") or "").strip()
                        if not text:
                            continue
                        x0, y0, x1, y1 = span.get("bbox", (0, 0, 0, 0))
                        flags = int(span.get("flags", 0))
                        words.append(TextLayerWord(
                            text=text,
                            bbox=BBox.from_xyxy(x0 * scale, y0 * scale,
                                                x1 * scale, y1 * scale),
                            font_size=float(span.get("size", 0.0)) * scale,
                            is_bold=bool(flags & 2 ** 4),
                            block_no=b_no, line_no=l_no, word_no=w_no,
                        ))
    except Exception as exc:  # noqa: BLE001 - auxiliary channel, never fatal
        log.warning("textlayer.extract_failed", page=page_number, error=str(exc))
    return words


# --------------------------------------------------------------------- merge
CURRENCY_GLYPHS = "₹$€£¥"


def corroborate(elements: list, words: list[TextLayerWord]) -> dict:
    """Cross-check OCR elements against text-layer spans, in place.

    Sets ``corroborated`` on agreeing elements, fills in typography, and
    restores currency glyphs the recogniser dropped by prefixing the symbol
    found at the same position in the text layer.

    Returns a small statistics dict for the run record.
    """
    stats = {"ocr_elements": len(elements), "textlayer_spans": len(words),
             "agreed": 0, "disagreed": 0, "unmatched": 0,
             "currency_recovered": 0}
    if not words or not elements:
        stats["unmatched"] = len(elements)
        return stats

    # bucket text-layer words by vertical band for a cheap spatial lookup
    band_h = 24.0
    buckets: dict[int, list[TextLayerWord]] = {}
    for w in words:
        for b in range(int(w.bbox.y // band_h), int(w.bbox.y1 // band_h) + 1):
            buckets.setdefault(b, []).append(w)

    for el in elements:
        ebox = el.bbox.geom
        cands: list[TextLayerWord] = []
        seen: set[int] = set()
        for b in range(int(ebox.y // band_h), int(ebox.y1 // band_h) + 1):
            for w in buckets.get(b, []):
                if id(w) in seen:
                    continue
                seen.add(id(w))
                if ebox.contains_ratio(w.bbox) > 0.45 or w.bbox.iou(ebox) > 0.25:
                    cands.append(w)
        if not cands:
            stats["unmatched"] += 1
            continue

        cands.sort(key=lambda w: (w.bbox.y, w.bbox.x))
        aux_text = " ".join(w.text for w in cands).strip()
        el.aux_text = aux_text
        sizes = [w.font_size for w in cands if w.font_size > 0]
        if sizes:
            el.font_size_px = round(sum(sizes) / len(sizes), 2)
        el.is_bold = any(w.is_bold for w in cands)

        if normalize_token(aux_text) == normalize_token(el.text):
            el.corroborated = True
            el.confidence = min(1.0, el.confidence + (1.0 - el.confidence) * 0.5)
            stats["agreed"] += 1
        else:
            stats["disagreed"] += 1

        # Recover a leading currency glyph the recogniser cannot emit.
        sym = next((c for c in aux_text if c in CURRENCY_GLYPHS), None)
        if sym and sym not in el.text:
            el.currency_glyph = sym
            stats["currency_recovered"] += 1

    return stats
