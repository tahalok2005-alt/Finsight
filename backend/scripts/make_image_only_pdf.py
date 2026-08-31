"""Build a genuinely image-only PDF from selected pages of a source PDF.

Each chosen page is rasterised and the *image alone* is placed on a new page.
No text, no fonts, no glyph operators survive — the result is what a scanner
produces, and it is the only fair way to test whether the pipeline can work
without a text layer.

The script verifies its own output: it re-opens the result and asserts that
every page yields zero extractable characters and zero text blocks, and that no
font resources are embedded.  If any of those checks fail the file is rejected
rather than shipped, because a "scanned" fixture that still carries glyphs would
silently invalidate the whole visual-only experiment.

    python scripts/make_image_only_pdf.py SOURCE.pdf OUT.pdf --pages 1,4,5,8,11,21
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pymupdf  # noqa: E402


def build(source: Path, out: Path, pages: list[int], dpi: int = 200) -> dict:
    src = pymupdf.open(source)
    dst = pymupdf.open()
    rendered = []
    try:
        for page_no in pages:
            if page_no < 1 or page_no > src.page_count:
                raise SystemExit(f"page {page_no} out of range (1..{src.page_count})")
            page = src[page_no - 1]
            rect = page.rect
            # rasterise at the given DPI; alpha off so the result is opaque
            pix = page.get_pixmap(dpi=dpi, alpha=False)
            new_page = dst.new_page(width=rect.width, height=rect.height)
            # place the bitmap over the whole page: image content only
            new_page.insert_image(new_page.rect, pixmap=pix)
            rendered.append({"source_page": page_no,
                             "pixels": f"{pix.width}x{pix.height}"})
            del pix
        dst.save(out, garbage=4, deflate=True)
    finally:
        dst.close()
        src.close()

    return {"output": str(out), "pages": rendered, "dpi": dpi,
            "size_bytes": out.stat().st_size}


def verify_no_text(path: Path) -> dict:
    """Prove the output really has no text layer."""
    findings = {"pages": 0, "total_chars": 0, "text_blocks": 0,
                "words": 0, "fonts": [], "images": 0}
    with pymupdf.open(path) as doc:
        findings["pages"] = doc.page_count
        for i in range(doc.page_count):
            page = doc[i]
            findings["total_chars"] += len((page.get_text("text") or "").strip())
            findings["words"] += len(page.get_text("words") or [])
            blocks = page.get_text("dict").get("blocks", [])
            findings["text_blocks"] += sum(1 for b in blocks if b.get("type") == 0)
            findings["images"] += len(page.get_images(full=True))
            for font in page.get_fonts(full=True):
                name = font[3] if len(font) > 3 else str(font)
                if name not in findings["fonts"]:
                    findings["fonts"].append(name)
    findings["is_image_only"] = (
        findings["total_chars"] == 0
        and findings["text_blocks"] == 0
        and findings["words"] == 0
        and not findings["fonts"]
        and findings["images"] >= findings["pages"]
    )
    return findings


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("source")
    ap.add_argument("output")
    ap.add_argument("--pages", default="1,4,5,8,11,21",
                    help="1-based source pages to rasterise, comma separated")
    ap.add_argument("--dpi", type=int, default=200)
    args = ap.parse_args()

    pages = [int(p) for p in args.pages.split(",") if p.strip()]
    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)

    info = build(Path(args.source), out, pages, dpi=args.dpi)
    check = verify_no_text(out)

    print(f"wrote {info['output']}  ({info['size_bytes']:,} bytes, "
          f"{len(info['pages'])} pages @ {info['dpi']} dpi)")
    for r in info["pages"]:
        print(f"   source page {r['source_page']:>3} -> {r['pixels']} px")
    print()
    print("verification that no text layer survived:")
    print(f"   pages               {check['pages']}")
    print(f"   extractable chars   {check['total_chars']}")
    print(f"   text blocks         {check['text_blocks']}")
    print(f"   words               {check['words']}")
    print(f"   embedded fonts      {len(check['fonts'])} {check['fonts']}")
    print(f"   embedded images     {check['images']}")
    print(f"   IMAGE-ONLY          {check['is_image_only']}")

    if not check["is_image_only"]:
        print("\nREJECTED: the output still carries text; not a valid scan fixture.",
              file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
