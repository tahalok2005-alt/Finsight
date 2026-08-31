"""PDF structural validation and metadata probing.

Runs *before* any expensive work so that a corrupt/encrypted/empty file fails
fast with an honest error rather than producing an empty "successful" run.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pymupdf

from app.config.settings import settings
from app.core.errors import (
    CorruptDocument, EmptyDocument, EncryptedDocument, ValidationFailure,
)
from app.core.logging import get_logger
from app.core.modes import NativeTextScope, guard_native_text

log = get_logger("ingestion.validator")

PDF_MAGIC = b"%PDF-"
# A page with fewer extractable characters than this is treated as having no
# usable text layer (i.e. it must go through OCR to be readable at all).
TEXT_LAYER_MIN_CHARS_PER_PAGE = 60


@dataclass(slots=True)
class PdfProbe:
    page_count: int
    pdf_version: str
    is_encrypted: bool
    metadata: dict[str, Any]
    text_layer_chars: int
    pages_with_text: int
    is_scanned: bool
    page_sizes: list[tuple[float, float]] = field(default_factory=list)
    rotations: list[int] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def text_coverage(self) -> float:
        return self.pages_with_text / self.page_count if self.page_count else 0.0


def check_magic(path: str | Path) -> None:
    """Content-based type check -- the declared MIME type is not trusted."""
    with open(path, "rb") as fh:
        head = fh.read(1024)
    if PDF_MAGIC not in head[:512]:
        raise CorruptDocument(
            "File does not begin with a PDF header; it is not a valid PDF.",
            detail={"first_bytes": head[:8].hex()})


def probe_pdf(path: str | Path) -> PdfProbe:
    """Open the PDF and collect structural facts.

    Raises the appropriate domain error for unusable documents instead of
    letting a broken file flow into the pipeline.
    """
    path = Path(path)
    check_magic(path)

    try:
        doc = pymupdf.open(path)
    except Exception as exc:  # pymupdf raises a variety of types
        raise CorruptDocument(f"PDF could not be opened: {exc}",
                              detail={"error": str(exc)}) from exc

    warnings: list[str] = []
    try:
        if doc.needs_pass:
            raise EncryptedDocument(
                "PDF is password protected and cannot be processed.")

        page_count = doc.page_count
        if page_count <= 0:
            raise EmptyDocument("PDF contains no pages.")
        if page_count > settings.max_pages:
            raise ValidationFailure(
                f"PDF has {page_count} pages, exceeding the "
                f"{settings.max_pages}-page limit.",
                detail={"page_count": page_count, "limit": settings.max_pages})

        meta = {k: v for k, v in (doc.metadata or {}).items() if v}
        version = str(meta.get("format", "unknown"))

        # INGESTION_VALIDATION scope: this counts characters to decide
        # whether a text layer exists at all.  It is pure metadata --
        # nothing downstream of ingestion branches on it and it never
        # reaches extraction -- so it is permitted in every mode, but it
        # is still recorded in the ledger so the audit is complete.
        guard_native_text("ingestion.validator.probe_pdf",
                          NativeTextScope.INGESTION_VALIDATION,
                          detail=f"pages={page_count}")
        total_chars = 0
        pages_with_text = 0
        sizes: list[tuple[float, float]] = []
        rotations: list[int] = []
        for i in range(page_count):
            try:
                page = doc[i]
            except Exception as exc:  # noqa: BLE001 - individual page corruption
                warnings.append(f"page {i + 1}: unreadable ({exc})")
                sizes.append((0.0, 0.0))
                rotations.append(0)
                continue
            rect = page.rect
            sizes.append((float(rect.width), float(rect.height)))
            rotations.append(int(page.rotation or 0))
            try:
                txt = page.get_text("text") or ""
            except Exception as exc:  # noqa: BLE001
                warnings.append(f"page {i + 1}: text layer unreadable ({exc})")
                txt = ""
            n = len(txt.strip())
            total_chars += n
            if n >= TEXT_LAYER_MIN_CHARS_PER_PAGE:
                pages_with_text += 1

        readable = sum(1 for w, h in sizes if w > 0 and h > 0)
        if readable == 0:
            raise CorruptDocument("No page in the PDF could be read.",
                                  detail={"warnings": warnings})

        coverage = pages_with_text / page_count
        # "Scanned" here means: the document cannot be understood from its text
        # layer alone.  OCR is mandatory for these; for the rest it is still the
        # canonical channel, but the text layer is available for corroboration.
        is_scanned = coverage < 0.5
        if is_scanned:
            warnings.append(
                f"Only {pages_with_text}/{page_count} pages carry a usable text "
                f"layer; treating the document as image-based.")

        return PdfProbe(
            page_count=page_count, pdf_version=version, is_encrypted=False,
            metadata=meta, text_layer_chars=total_chars,
            pages_with_text=pages_with_text, is_scanned=is_scanned,
            page_sizes=sizes, rotations=rotations, warnings=warnings,
        )
    finally:
        doc.close()
