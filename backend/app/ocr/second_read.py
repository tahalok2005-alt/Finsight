"""A second, independent read of every table figure's cell.

The page read -- one OCR pass over a whole page at 200 dpi -- is right almost
all the time, and when it is wrong it is plausibly wrong: on an image cash
flow statement it read "(711)" as "(71)", "(1,168)" as "(168)" and "(161)" as
"(167)", in a font whose thin "1" merges with its bracket. Nothing about those
figures looks wrong, and one of them carried no flag.

So each cell is read again, alone: its box, with a margin, rendered at twice
the resolution and recognised on its own. The two reads are independent in
the ways that matter -- different pixels, a different detection context, no
neighbouring text competing for the box. Where they agree about the digits
and the sign, that corroborates the value. Where they disagree, the figure is
sent to review with both readings shown. The value is never changed: which
read is right is for a person to settle, and the brief is explicit that a
suspicious number is REVIEW_REQUIRED, not repaired.

Measured on the 1,687 golden figures the corpus finds (Phase 22): the second
read disagreed with 1 correct value in 1,681 and with every one of the page
read's digit misreads.

Pixels only: the clip is rendered from the page image, never read from a PDF
text layer, so this holds under VISUAL_ONLY. Reads are cached by content
hash, page, box, resolution, strategy and engine version, so a warm run pays
nothing.

Two ways to get the cell's pixels (``settings.second_read_strategy``):

* ``clip`` renders each cell's clip from the PDF on its own. On a scanned page
  that decodes the whole page image once per cell -- 610 ms a cell on a 29-page
  scanned filing, twice the cost of recognising it.
* ``page`` renders the page once at the second-read resolution and crops every
  cell from that image, then recognises the cells on a thread pool (ONNX
  Runtime releases the GIL). The crops are the clip renders' pixels except at
  the clip edge, where anti-aliasing can differ by a few grey levels. Over the
  5,763 corpus cells the two readings agreed on 5,758; the page read raised
  every dispute the clip read did, and three more on figures the clip read
  confirmed. So a page read that DISAGREES with the page's figure is
  confirmed by the clip read of that one cell before the figure is disputed:
  the dispute stands only if both second reads disagree. On the corpus that
  gives exactly the clip method's verdicts (and reads two cells it could not),
  at a quarter of the time (docs/PERFORMANCE.md).
"""
from __future__ import annotations

import json
import re
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from time import perf_counter

import numpy as np

from app.config.settings import settings
from app.core.logging import get_logger
from app.models.enums import FactKind
from app.models.schemas import FinancialFact
from app.normalization.numbers import looks_numeric, normalise_separators

log = get_logger("ocr.second_read")

_VERSION = "sr1"
#: Cache versions by strategy, so readings made two ways are never mixed.
_STRATEGY_VERSION = {"clip": "sr1", "page": "sr2p"}


def signature(text: str | None) -> tuple[str, bool] | None:
    """The digits and the sign of a printed figure, however its separators
    were read: "8.785" and "8,785" are one figure; "(71)" and "(711)" are
    not; "(844)" and "（844)" are one."""
    if not text:
        return None
    t = normalise_separators(text)
    digits = re.sub(r"\D", "", t).lstrip("0") or "0"
    negative = "(" in t or bool(re.search(r"(^|\s)-\s*\d", t))
    return digits, negative


@dataclass
class SecondReadStats:
    read: int = 0
    agreed: int = 0
    disputed: int = 0
    unread: int = 0
    cached: int = 0
    #: page-crop disagreements re-read from the cell's own clip
    confirmed: int = 0
    seconds: float = 0.0

    def as_dict(self) -> dict:
        return {"read": self.read, "agreed": self.agreed, "disputed": self.disputed,
                "unread": self.unread, "cached": self.cached,
                "confirmed_by_clip": self.confirmed,
                "seconds": round(self.seconds, 2)}


def _strategy() -> str:
    chosen = settings.second_read_strategy
    return chosen if chosen in _STRATEGY_VERSION else "clip"


def _cache_path(scope: str, page: int, engine_version: str) -> Path:
    d = settings.cache_dir.resolve() / "second_read" / scope
    d.mkdir(parents=True, exist_ok=True)
    tag = (f"{_STRATEGY_VERSION[_strategy()]}-p{page:04d}-dpi{settings.second_read_dpi}-pad"
           f"{settings.second_read_pad_x}x{settings.second_read_pad_y}-{engine_version}")
    return d / f"{re.sub(r'[^A-Za-z0-9_.-]', '_', tag)}.json"


def _box_key(bbox) -> str:
    return f"{round(bbox.x)}_{round(bbox.y)}_{round(bbox.width)}_{round(bbox.height)}"


def _cell_rect(page, bbox):
    """The cell's box with its margin, in PDF points, clipped to the page."""
    import pymupdf  # noqa: PLC0415 - rendering is local to this stage
    k = 72.0 / settings.render_dpi            # 200 dpi pixels -> PDF points
    px, py = settings.second_read_pad_x, settings.second_read_pad_y
    rect = pymupdf.Rect((bbox.x - px) * k, (bbox.y - py) * k,
                        (bbox.x + bbox.width + px) * k, (bbox.y + bbox.height + py) * k)
    rect = rect & page.rect
    return None if rect.is_empty else rect


def _nearest_numeric(raw, bbox) -> str | None:
    """The numeric token nearest the cell's centre in a recognised crop."""
    px, py = settings.second_read_pad_x, settings.second_read_pad_y
    scale = settings.second_read_dpi / settings.render_dpi
    cx, cy = (px + bbox.width / 2) * scale, (py + bbox.height / 2) * scale
    best = None
    for item in raw or []:
        poly, text = item[0], item[1]
        if not looks_numeric(text or ""):
            continue
        mx = sum(p[0] for p in poly) / len(poly)
        my = sum(p[1] for p in poly) / len(poly)
        d = (mx - cx) ** 2 + (my - cy) ** 2
        if best is None or d < best[0]:
            best = (d, text)
    return best[1] if best else None


def _read_cell(page, bbox, engine) -> str | None:
    """The numeric token nearest the cell's centre, in the cell alone
    (the ``clip`` strategy: the cell's clip rendered on its own)."""
    from PIL import Image  # noqa: PLC0415

    rect = _cell_rect(page, bbox)
    if rect is None:
        return None
    pix = page.get_pixmap(dpi=settings.second_read_dpi, clip=rect)
    img = np.asarray(Image.frombytes("RGB", (pix.width, pix.height), pix.samples))
    raw, _ = engine.engine(img)
    return _nearest_numeric(raw, bbox)


def _page_crops(page, boxes) -> list:
    """Every cell's crop from ONE render of the page at the second-read
    resolution (the ``page`` strategy)."""
    import pymupdf  # noqa: PLC0415
    mat = pymupdf.Matrix(settings.second_read_dpi / 72.0, settings.second_read_dpi / 72.0)
    full = page.get_pixmap(matrix=mat, alpha=False)
    arr = np.frombuffer(full.samples, np.uint8).reshape(full.height, full.width, full.n)[:, :, :3]
    crops = []
    for bbox in boxes:
        rect = _cell_rect(page, bbox)
        if rect is None:
            crops.append(None)
            continue
        ir = (rect * mat).irect
        x0, y0 = max(0, ir.x0 - full.x), max(0, ir.y0 - full.y)
        crop = arr[y0:y0 + ir.height, x0:x0 + ir.width]
        crops.append(np.ascontiguousarray(crop) if crop.size else None)
    return crops


def verify(facts: list[FinancialFact], pdf_path: Path, *, cache_scope: str,
           engine) -> SecondReadStats:
    """Read every table figure's cell again and record the outcome on the fact."""
    stats = SecondReadStats()
    if not settings.second_read_enabled:
        return stats
    t0 = perf_counter()
    by_page: dict[int, list[FinancialFact]] = defaultdict(list)
    for f in facts:
        prov = f.provenance
        if (f.kind == FactKind.TABLE_CELL and f.value is not None and prov is not None
                and prov.bbox is not None and prov.page_number):
            by_page[prov.page_number].append(f)
    if not by_page:
        return stats

    import pymupdf  # noqa: PLC0415
    version = re.sub(r"[^0-9A-Za-z.]", "", getattr(engine.info, "version", "") or "x")
    strategy = _strategy()
    workers = settings.second_read_workers or settings.resolved_page_workers
    pool = (ThreadPoolExecutor(max_workers=max(1, workers), thread_name_prefix="second-read")
            if strategy == "page" else None)
    doc = None
    try:
        for page_number, page_facts in sorted(by_page.items()):
            path = _cache_path(cache_scope, page_number, version)
            cache = (json.loads(path.read_text(encoding="utf-8"))
                     if path.exists() else {})
            dirty = False
            todo = {}
            for f in page_facts:
                key = _box_key(f.provenance.bbox)
                if key not in cache and key not in todo:
                    todo[key] = f.provenance.bbox
            if todo:
                if doc is None:
                    doc = pymupdf.open(pdf_path)
                page = doc[page_number - 1]
                keys = list(todo)
                if pool is not None and not page.rotation:
                    # One render, every cell cropped from it, recognised in
                    # parallel; results come back in the order of the cells.
                    crops = _page_crops(page, [todo[k] for k in keys])
                    raws = pool.map(
                        lambda c: engine.engine(c)[0] if c is not None else None, crops)
                    for key, raw in zip(keys, raws):
                        cache[key] = _nearest_numeric(raw, todo[key])
                else:
                    for key in keys:
                        cache[key] = _read_cell(page, todo[key], engine)
                dirty = True
            for f in page_facts:
                key = _box_key(f.provenance.bbox)
                text = cache.get(key)
                if key not in todo:
                    stats.cached += 1
                stats.read += 1
                first, second = signature(f.raw_value), signature(text)
                page_text = None
                if strategy == "page" and second is not None and second != first:
                    # A dispute by the page crop is confirmed by the cell's own
                    # clip before it stands (see the module docstring).
                    ckey = f"clip:{key}"
                    if ckey not in cache:
                        if doc is None:
                            doc = pymupdf.open(pdf_path)
                        cache[ckey] = _read_cell(doc[page_number - 1],
                                                 f.provenance.bbox, engine)
                        dirty = True
                    page_text, text = text, cache[ckey]
                    second = signature(text)
                    stats.confirmed += 1
                if second is None:
                    agrees = None
                    stats.unread += 1
                else:
                    agrees = second == first
                    if agrees:
                        stats.agreed += 1
                    else:
                        stats.disputed += 1
                f.meta = {**(f.meta or {}), "second_read": {
                    "text": text, "agrees": agrees,
                    "dpi": settings.second_read_dpi, "strategy": strategy,
                    **({"page_crop_text": page_text} if page_text is not None else {})}}
            if dirty:
                path.write_text(json.dumps(cache), encoding="utf-8")
    finally:
        if pool is not None:
            pool.shutdown()
        if doc is not None:
            doc.close()
    stats.seconds = perf_counter() - t0
    log.info("second_read.done", **stats.as_dict())
    return stats
