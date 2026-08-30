"""Provenance: every fact traceable back to the pixels it came from.

A financial number without a source is an assertion, not evidence.  Each fact
carries a ``Provenance`` record naming the document, page, region, table cell
and bounding box, plus the original text as it was read.  This module turns
that record into something the UI and the API can use:

* resolution to a stable page-image URL;
* relative (0..1) coordinates so an overlay works at any zoom without knowing
  the render DPI;
* on-demand generation of a cropped evidence image for the exact source area.
"""
from __future__ import annotations

from pathlib import Path


from app.config.settings import settings
from app.core.errors import ResourceNotFound
from app.core.geometry import BBox
from app.core.logging import get_logger
from app.models.schemas import BBoxModel, Provenance

log = get_logger("provenance")

CROP_PADDING = 8.0


def page_image_url(document_id: str, page_number: int) -> str:
    """Opaque API URL for a page image -- never a filesystem path."""
    return f"{settings.api_prefix}/documents/{document_id}/pages/{page_number}/image"


def crop_url(document_id: str, page_number: int, bbox: BBoxModel | None) -> str | None:
    if bbox is None:
        return None
    return (f"{settings.api_prefix}/documents/{document_id}/pages/{page_number}"
            f"/crop?x={bbox.x:.1f}&y={bbox.y:.1f}"
            f"&w={bbox.width:.1f}&h={bbox.height:.1f}")


def enrich(prov: Provenance, *, render_width: int, render_height: int) -> Provenance:
    """Fill in URLs and relative coordinates on a provenance record."""
    prov.page_image_url = page_image_url(prov.document_id, prov.page_number)
    if prov.bbox is not None and render_width > 0 and render_height > 0:
        rel = prov.bbox.geom.to_relative(render_width, render_height)
        prov.bbox_relative = BBoxModel(**rel)
        prov.crop_url = crop_url(prov.document_id, prov.page_number, prov.bbox)
    return prov


def crop_path(document_id: str, page_number: int) -> Path:
    d = (settings.cache_dir / "crops" / document_id).resolve()
    root = settings.cache_dir.resolve()
    if not str(d).startswith(str(root)):
        raise ResourceNotFound("Invalid crop path")
    d.mkdir(parents=True, exist_ok=True)
    return d


def render_crop(page_image: str | Path, bbox: BBox, out_path: Path,
                *, padding: float = CROP_PADDING) -> Path:
    """Write a cropped region of a page image to ``out_path``.

    Pillow is used for the crop rather than PyMuPDF: this build's
    ``Pixmap(pixmap, clip)`` and ``Pixmap(colorspace, irect, samples, alpha)``
    overloads both fail argument dispatch, and Pillow is already a dependency.
    """
    from PIL import Image  # noqa: PLC0415 - only needed on this path

    src = Path(page_image)
    if not src.is_file():
        raise ResourceNotFound(f"Page image is not available: {src.name}")

    with Image.open(src) as im:
        im = im.convert("RGB")
        box = bbox.padded(padding).clipped(im.width, im.height)
        x0, y0 = int(box.x), int(box.y)
        x1, y1 = max(x0 + 1, int(box.x1)), max(y0 + 1, int(box.y1))
        if x1 <= x0 or y1 <= y0:
            raise ResourceNotFound("Requested crop area is empty")
        im.crop((x0, y0, x1, y1)).save(out_path, format="PNG")
    return out_path


def cached_crop(document_id: str, page_number: int, page_image: str | Path,
                bbox: BBox) -> Path:
    d = crop_path(document_id, page_number)
    name = (f"p{page_number:04d}-{int(bbox.x)}_{int(bbox.y)}_"
            f"{int(bbox.width)}_{int(bbox.height)}.png")
    out = d / name
    if out.exists() and out.stat().st_size > 0:
        return out
    return render_crop(page_image, bbox, out)
