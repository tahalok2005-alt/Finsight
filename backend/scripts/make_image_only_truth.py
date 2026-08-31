"""Derive the ground-truth key for the image-only fixture.

The image-only PDF is a subset of the source document's pages, rasterised.  Its
answer key is therefore the subset of the *already hand-built* source key that
falls on those pages, with page numbers remapped to their new positions.

Nothing here reads pipeline output: the values still come from the key that was
read off the original PDF by eye.  Only the page numbers change.

    python scripts/make_image_only_truth.py SOURCE.truth.json OUT.truth.json \
        --pages 1,4,5,8,11,21
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("source_truth")
    ap.add_argument("output")
    ap.add_argument("--pages", default="1,4,5,8,11,21")
    ap.add_argument("--document", default="RIL_Q1_FY27_IMAGE_ONLY.pdf")
    args = ap.parse_args()

    pages = [int(p) for p in args.pages.split(",") if p.strip()]
    remap = {src: idx + 1 for idx, src in enumerate(pages)}

    data = json.loads(Path(args.source_truth).read_text(encoding="utf-8"))
    kept = []
    for fact in data.get("facts", []):
        page = fact.get("page")
        if page in remap:
            out = dict(fact)
            out["page"] = remap[page]
            out["source_page"] = page
            kept.append(out)

    payload = {
        "document": args.document,
        "derived_from": Path(args.source_truth).name,
        "page_mapping": {str(k): v for k, v in remap.items()},
        "how_this_was_built": [
            "Subset of the hand-built key for the source PDF, restricted to the",
            "pages that were rasterised into the image-only fixture, with page",
            "numbers remapped to their new positions.  The values themselves",
            "were read off the original document by eye and are unchanged; no",
            "pipeline output was consulted.",
        ],
        "facts": kept,
    }
    out_path = Path(args.output)
    out_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    print(f"wrote {out_path} with {len(kept)} facts "
          f"(from {len(data.get('facts', []))} in the source key)")
    by_page: dict[int, int] = {}
    for f in kept:
        by_page[f["page"]] = by_page.get(f["page"], 0) + 1
    for page in sorted(by_page):
        src = next(k for k, v in remap.items() if v == page)
        print(f"   fixture page {page} (source page {src}): {by_page[page]} facts")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
