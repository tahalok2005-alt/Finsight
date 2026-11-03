"""Build a scanner-degraded copy of a real filing for the OCR stress test.

The financial content is untouched: these are the *same real pages* as the
image-only benchmark, so the existing answer key still applies and no value is
invented.  What changes is only the image quality, reproducing what a desk
scanner actually does to a page:

* **lower capture resolution** -- 150 dpi rather than 200;
* **skew** -- a fraction of a degree of rotation, because paper never sits
  square on the glass;
* **sensor noise** -- mild Gaussian noise;
* **JPEG artefacts** -- lossy compression, which is what most scanners emit;
* **uneven illumination** -- a soft brightness gradient across the page.

That is a genuine stress test of the visual path: small text, numeric columns,
percentages and units all have to survive it, and because the ground truth is
unchanged the degradation can be measured rather than guessed at.
"""
from __future__ import annotations

import io
import sys
from pathlib import Path

import numpy as np
import pymupdf
from PIL import Image

SOURCE = Path(__file__).resolve().parents[2] / "data" / "benchmark" / \
    "RIL_Q1_FY27_IMAGE_ONLY.pdf"
DEST = Path(__file__).resolve().parents[2] / "data" / "benchmark" / \
    "RIL_Q1_FY27_SCANNED_DEGRADED.pdf"

CAPTURE_DPI = 150
SKEW_DEGREES = 0.6
NOISE_SIGMA = 6.0
JPEG_QUALITY = 55


def degrade(image: Image.Image, rng: np.random.Generator) -> Image.Image:
    """Apply the degradations a flatbed scan actually introduces."""
    # skew: paper is never square on the glass
    angle = float(rng.uniform(-SKEW_DEGREES, SKEW_DEGREES))
    image = image.rotate(angle, resample=Image.BICUBIC, expand=False,
                         fillcolor=(255, 255, 255))

    arr = np.asarray(image).astype(np.float32)

    # uneven illumination: a soft gradient, brighter on one edge
    h, w = arr.shape[:2]
    gradient = np.linspace(0.94, 1.06, w, dtype=np.float32)[None, :, None]
    arr *= gradient

    # sensor noise
    arr += rng.normal(0.0, NOISE_SIGMA, arr.shape).astype(np.float32)
    arr = np.clip(arr, 0, 255).astype(np.uint8)

    # lossy compression, as most scanners emit
    buf = io.BytesIO()
    Image.fromarray(arr).save(buf, format="JPEG", quality=JPEG_QUALITY)
    buf.seek(0)
    return Image.open(buf).convert("RGB")


def main() -> None:
    if not SOURCE.is_file():
        print(f"source not found: {SOURCE}")
        sys.exit(1)

    rng = np.random.default_rng(20260906)   # reproducible fixture
    src = pymupdf.open(SOURCE)
    out = pymupdf.open()

    for index in range(src.page_count):
        page = src[index]
        pix = page.get_pixmap(dpi=CAPTURE_DPI)
        image = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
        scanned = degrade(image, rng)

        buf = io.BytesIO()
        scanned.save(buf, format="JPEG", quality=JPEG_QUALITY)
        buf.seek(0)

        new_page = out.new_page(width=page.rect.width, height=page.rect.height)
        new_page.insert_image(new_page.rect, stream=buf.getvalue())

    out.save(DEST, deflate=True)
    out.close()
    src.close()

    check = pymupdf.open(DEST)
    chars = sum(len(p.get_text().strip()) for p in check)
    print(f"wrote {DEST.name}: {check.page_count} pages, "
          f"{DEST.stat().st_size:,} bytes, text characters = {chars}")
    check.close()


if __name__ == "__main__":
    main()
