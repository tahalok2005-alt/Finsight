"""Visual page rendering -- the canonical representation of every page.

Why rendering is canonical
--------------------------
A financial PDF encodes meaning in *layout*: column alignment tells you which
period a number belongs to, ruling lines delimit tables, indentation marks
sub-totals, and a KPI card groups a label with its value.  The PDF content
stream carries none of that structure -- it is an unordered list of glyph
placements.  Rendering to pixels and analysing the image keeps every visual cue
available to the downstream stages, and it is the only representation that also
works for scanned documents.

Memory behaviour
----------------
Pages are rendered one at a time and written straight to disk.  Callers get a
path, never a retained image, so peak RSS stays at roughly one page bitmap.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

import numpy as np
import pymupdf

from app.config.settings import settings
from app.core.errors import StageFailure
from app.core.logging import get_logger

log = get_logger("rendering")


@dataclass(slots=True)
class RenderedPage:
    page_number: int
    image_path: Path
    thumbnail_path: Path | None
    width: int
    height: int
    dpi: int
    pdf_width: float
    pdf_height: float
    rotation: int
    scale: float
    from_cache: bool = False

    @property
    def pdf_to_image_scale(self) -> float:
        """Multiply a PDF-space coordinate by this to get image pixels."""
        return self.scale


def page_render_dir(document_id: str) -> Path:
    d = (settings.render_dir / document_id).resolve()
    root = settings.render_dir.resolve()
    if not str(d).startswith(str(root)):
        raise StageFailure("rendering", "Invalid render path")
    return d


def _paths(document_id: str, page_number: int, signature: str) -> tuple[Path, Path]:
    d = page_render_dir(document_id)
    ext = settings.render_format
    base = f"page-{page_number:04d}-{signature}"
    return d / f"{base}.{ext}", d / f"{base}-thumb.{ext}"


def _shared_paths(content_hash: str, page_number: int, signature: str) -> tuple[Path, Path]:
    """Where a page's render is kept for EVERY document with these bytes.

    Renders were kept per document id, so a second document with the same
    bytes -- a re-upload, or the same filing evaluated under a new tag --
    rendered every page again although its OCR was cached by content hash
    (Phase 22). The MuPDF version is part of the key: a different renderer may
    draw different pixels, and a cached render must be the render this build
    would have made."""
    ext = settings.render_format
    base = f"page-{page_number:04d}-{signature}"
    d = settings.render_dir.resolve() / "_by_content" / content_hash / f"mupdf{pymupdf.VersionBind}"
    return d / f"{base}.{ext}", d / f"{base}-thumb.{ext}"


def _link_or_copy(src: Path, dst: Path) -> bool:
    """Make ``dst`` the same bytes as ``src``: a hard link where the volume
    allows one, a copy otherwise. Never raises; False when neither worked."""
    import os
    import shutil
    try:
        dst.parent.mkdir(parents=True, exist_ok=True)
        if dst.exists():
            dst.unlink()
        try:
            os.link(src, dst)
        except OSError:
            shutil.copyfile(src, dst)
        return True
    except OSError:
        return False


def _effective_dpi(rect: pymupdf.Rect, requested_dpi: int) -> int:
    """Clamp DPI so an unusually large page cannot blow up memory."""
    w_in, h_in = rect.width / 72.0, rect.height / 72.0
    if w_in <= 0 or h_in <= 0:
        return requested_dpi
    px = (w_in * requested_dpi) * (h_in * requested_dpi)
    if px <= settings.render_max_pixels:
        return requested_dpi
    scale = math.sqrt(settings.render_max_pixels / px)
    clamped = max(72, int(requested_dpi * scale))
    log.warning("render.dpi_clamped", requested=requested_dpi, effective=clamped,
                page_in=(round(w_in, 2), round(h_in, 2)))
    return clamped


class PageRenderer:
    """Renders PDF pages to PNG with a content-addressed on-disk cache.

    The cache key includes the render signature (dpi + format), so changing the
    render settings transparently invalidates previous renders instead of
    silently reusing them.
    """

    def __init__(self, document_id: str, pdf_path: str | Path,
                 dpi: int | None = None, content_hash: str | None = None):
        self.document_id = document_id
        self.pdf_path = Path(pdf_path)
        self.dpi = dpi or settings.render_dpi
        self.signature = f"dpi{self.dpi}-{settings.render_format}"
        #: The PDF's sha256, when known: renders are then shared by every
        #: document with these bytes (see `_shared_paths`).
        self.content_hash = content_hash
        self._doc: pymupdf.Document | None = None

    # ------------------------------------------------------------- lifecycle
    def __enter__(self) -> "PageRenderer":
        self.open()
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def open(self) -> None:
        if self._doc is None:
            try:
                self._doc = pymupdf.open(self.pdf_path)
            except Exception as exc:
                raise StageFailure("rendering",
                                   f"Cannot open PDF for rendering: {exc}") from exc

    def close(self) -> None:
        if self._doc is not None:
            self._doc.close()
            self._doc = None

    @property
    def page_count(self) -> int:
        self.open()
        assert self._doc is not None
        return self._doc.page_count

    # --------------------------------------------------------------- render
    def render_page(self, page_number: int, *, force: bool = False) -> RenderedPage:
        """Render one 1-based page number, using the cache when possible."""
        self.open()
        assert self._doc is not None
        if page_number < 1 or page_number > self._doc.page_count:
            raise StageFailure("rendering", f"Page {page_number} out of range")

        page = self._doc[page_number - 1]
        rect = page.rect
        dpi = _effective_dpi(rect, self.dpi)
        signature = f"dpi{dpi}-{settings.render_format}"
        img_path, thumb_path = _paths(self.document_id, page_number, signature)

        if not force and img_path.exists() and img_path.stat().st_size > 0:
            try:
                w, h = image_dimensions(img_path)
                return RenderedPage(
                    page_number=page_number, image_path=img_path,
                    thumbnail_path=thumb_path if thumb_path.exists() else None,
                    width=int(w), height=int(h), dpi=dpi,
                    pdf_width=float(rect.width), pdf_height=float(rect.height),
                    rotation=int(page.rotation or 0), scale=dpi / 72.0,
                    from_cache=True)
            except Exception:  # noqa: BLE001 - corrupt cache entry, just re-render
                log.warning("render.cache_unreadable", page=page_number,
                            path=str(img_path))

        shared_img = shared_thumb = None
        if self.content_hash and not force:
            shared_img, shared_thumb = _shared_paths(self.content_hash, page_number, signature)
            if (shared_img.exists() and shared_img.stat().st_size > 0
                    and _link_or_copy(shared_img, img_path)):
                thumb = (thumb_path if shared_thumb.exists()
                         and _link_or_copy(shared_thumb, thumb_path) else None)
                try:
                    w, h = image_dimensions(img_path)
                    return RenderedPage(
                        page_number=page_number, image_path=img_path,
                        thumbnail_path=thumb, width=int(w), height=int(h), dpi=dpi,
                        pdf_width=float(rect.width), pdf_height=float(rect.height),
                        rotation=int(page.rotation or 0), scale=dpi / 72.0,
                        from_cache=True)
                except Exception:  # noqa: BLE001 - unreadable copy: render afresh
                    log.warning("render.shared_cache_unreadable", page=page_number)

        img_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            pix = page.get_pixmap(dpi=dpi, alpha=False)
            pix.save(img_path)
            width, height = pix.width, pix.height
            thumb = None
            if settings.thumbnail_width > 0 and width > settings.thumbnail_width:
                factor = settings.thumbnail_width / width
                tpix = page.get_pixmap(matrix=pymupdf.Matrix(
                    (dpi / 72.0) * factor, (dpi / 72.0) * factor), alpha=False)
                tpix.save(thumb_path)
                thumb = thumb_path
                del tpix
            del pix
        except Exception as exc:
            raise StageFailure(
                "rendering", f"Failed to render page {page_number}: {exc}",
                detail={"page": page_number}) from exc

        if self.content_hash:
            shared_img, shared_thumb = _shared_paths(self.content_hash, page_number, signature)
            if not shared_img.exists():
                _link_or_copy(img_path, shared_img)
            if thumb is not None and not shared_thumb.exists():
                _link_or_copy(thumb, shared_thumb)
        return RenderedPage(
            page_number=page_number, image_path=img_path, thumbnail_path=thumb,
            width=width, height=height, dpi=dpi,
            pdf_width=float(rect.width), pdf_height=float(rect.height),
            rotation=int(page.rotation or 0), scale=dpi / 72.0, from_cache=False)

    def iter_pages(self, *, force: bool = False) -> Iterator[RenderedPage]:
        for n in range(1, self.page_count + 1):
            yield self.render_page(n, force=force)

    # ------------------------------------------------------------ image i/o
    def load_image_array(self, page_number: int, *, force: bool = False) -> np.ndarray:
        """Return the rendered page as an HxWx3 uint8 RGB array.

        The array is produced from the *rendered file* so image analysis always
        sees exactly what is persisted and shown in the UI.
        """
        rp = self.render_page(page_number, force=force)
        return load_rgb(rp.image_path)


def load_rgb(path: str | Path) -> np.ndarray:
    """Decode an image file into an HxWx3 uint8 RGB array at native resolution.

    ``pymupdf.Pixmap(path)`` decodes the stored bitmap directly.  Re-opening the
    file as a *document* would rasterise it at 72 dpi and silently downscale it,
    which would break every pixel coordinate downstream.
    """
    pix = pymupdf.Pixmap(str(path))
    if pix.alpha:
        pix = pymupdf.Pixmap(pix, 0)  # drop alpha
    arr = np.frombuffer(pix.samples, dtype=np.uint8)
    arr = arr.reshape(pix.height, pix.width, pix.n)
    if pix.n == 1:
        arr = np.repeat(arr, 3, axis=2)
    elif pix.n >= 3:
        arr = arr[:, :, :3]
    out = np.ascontiguousarray(arr)
    del pix
    return out


def image_dimensions(path: str | Path) -> tuple[int, int]:
    """(width, height) of a stored raster image, without decoding pixels twice."""
    pix = pymupdf.Pixmap(str(path))
    dims = (pix.width, pix.height)
    del pix
    return dims
