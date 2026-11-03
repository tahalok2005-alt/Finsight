"""What a page image tells you about how it was captured.

A scanner leaves fingerprints: the page goes in crooked, the sensor adds
speckle, the lamp lights one side more than the other, and JPEG smears the
strokes.  None of that changes the *content*, but all of it changes what OCR
can read, and the same document scanned badly loses accuracy the pipeline can
otherwise reach.

Everything here is measured, not learned.  There is no model file, no training
set and no network: each signal is a statistic over the pixels with a stated
meaning, so a page that scores badly can be explained rather than merely
flagged.  The point is to know *which* defect a page has, because the repair
for skew is not the repair for noise, and applying all of them to a clean page
makes it worse.

The thresholds below are the ones at which a defect starts to cost OCR
accuracy on real pages.  They are deliberately conservative: a page that is
merely slightly imperfect is left alone.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import cv2
import numpy as np

from app.core.logging import get_logger

log = get_logger(__name__)

#: Beyond this many degrees, rotating the page back is worth the resample.
#: Below it the rows are straight enough that OCR's own tolerance covers them.
SKEW_DEGREES = 0.25
#: Residual after a median filter, as a fraction of the dynamic range.  Speckle
#: from a scanner sensor sits well above this; clean rasterisation sits at zero.
NOISE_RESIDUAL = 0.020
#: Gap between the paper and the ink.  A faint or washed-out scan narrows it;
#: a crisp page separates them almost completely.  Measured at 0.97-1.00 on
#: the benchmark's clean rasterisations and 0.60-0.65 on the scanner-degraded
#: copy of those same pages, so the line sits between the two bands.
LOW_CONTRAST = 0.80
#: Variation of the *paper* across the page -- a lamp brighter on one edge, or
#: a book spine's shadow.  Measured after the text is closed out of the image,
#: because otherwise this reads the density of the type instead of the light.
#:
#: This one is *reported but not acted on*.  On the benchmark it cannot tell a
#: lighting gradient from a printed design: the media release's coloured
#: masthead measures 0.15-0.16, higher than the deliberately vignetted scan of
#: those same pages at 0.12-0.13, and fitting a plane to the paper level
#: reverses the ordering rather than separating them.  A signal that cannot
#: distinguish the defect from the design must not drive a transformation, so
#: it is recorded as an observation and left there.
UNEVEN_LIGHT = 0.10
#: Variance of the Laplacian over ink.  Falls as strokes blur: the benchmark's
#: clean pages measure 0.17-0.26 and the JPEG-compressed scan of those same
#: pages 0.042-0.051, so the boundary is set between the two bands.
BLUR_VARIANCE = 0.10
#: Median height of a text row, in pixels -- the effective resolution, which
#: matters more than the nominal dpi.  Set from measurement rather than from
#: the recogniser's nominal 48px training height: the benchmark filing's dense
#: statement pages measure 19-21px and read at F1 0.988, so type that size is
#: demonstrably fine and calling it a defect would be crying wolf on a third
#: of a clean document.  This fires below that observed working range.
SMALL_TEXT_PX = 16.0


@dataclass(slots=True)
class ImageQuality:
    """Measured capture defects of one page image."""

    skew_degrees: float = 0.0
    noise: float = 0.0
    contrast: float = 1.0
    illumination_range: float = 0.0
    sharpness: float = 1.0
    text_height_px: float = 0.0

    #: defects a transformation can repair, each above its threshold
    defects: list[str] = field(default_factory=list)
    #: measured, above threshold, but not something to act on -- see the note
    #: on :data:`UNEVEN_LIGHT`
    observations: list[str] = field(default_factory=list)

    @property
    def is_degraded(self) -> bool:
        """Whether this page has a defect worth trying to repair."""
        return bool(self.defects)

    def as_dict(self) -> dict:
        return {"skew_degrees": round(self.skew_degrees, 3),
                "noise": round(self.noise, 4),
                "contrast": round(self.contrast, 3),
                "illumination_range": round(self.illumination_range, 4),
                "sharpness": round(self.sharpness, 5),
                "text_height_px": round(self.text_height_px, 1),
                "defects": list(self.defects),
                "observations": list(self.observations)}


def _grey(image: np.ndarray) -> np.ndarray:
    if image.ndim == 2:
        return image
    return cv2.cvtColor(image, cv2.COLOR_RGB2GRAY)


def _ink_mask(grey: np.ndarray) -> np.ndarray:
    """Pixels that are ink rather than paper, by Otsu's split."""
    _, mask = cv2.threshold(grey, 0, 255,
                            cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)
    return mask


def estimate_skew(grey: np.ndarray, *, max_degrees: float = 8.0) -> float:
    """How far the text rows are off level, in degrees.

    The value is the *correction*: rotating the page by this many degrees with
    :func:`cv2.getRotationMatrix2D` puts the rows back level.

    Text rows are found by smearing the ink horizontally until each row becomes
    one long blob, then taking the median angle of those blobs' minimum-area
    rectangles.  The median is what makes this robust: a figure or a ruling
    line contributes one wrong angle, not a wrong answer.
    """
    mask = _ink_mask(grey)
    # smear along the row so words merge into lines, not into the line below
    width = max(15, grey.shape[1] // 60)
    kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (width, 3))
    smeared = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, kernel)
    contours, _ = cv2.findContours(smeared, cv2.RETR_EXTERNAL,
                                   cv2.CHAIN_APPROX_SIMPLE)
    angles: list[float] = []
    for contour in contours:
        (_, _), (w, h), angle = cv2.minAreaRect(contour)
        # only a long, thin blob is a text row; a logo or a figure is neither,
        # and one wrong angle among few is what makes a median lie
        if w < grey.shape[1] * 0.15 or min(w, h) < 3 or w < h * 6:
            continue
        if h > w:                      # rectangle reported on its side
            angle += 90.0
        if abs(angle) <= max_degrees:
            angles.append(angle)
    if len(angles) < 8:
        return 0.0
    return float(np.median(angles))


def estimate_text_height(grey: np.ndarray) -> float:
    """Median height of a text row in pixels -- the effective resolution.

    A page scanned at 150 dpi and one rendered at 300 dpi hold the same words
    at very different sizes, and it is the size in pixels, not the nominal dpi,
    that decides whether the recogniser can separate the strokes.
    """
    mask = _ink_mask(grey)
    width = max(15, grey.shape[1] // 60)
    kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (width, 1))
    rows = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, kernel)
    contours, _ = cv2.findContours(rows, cv2.RETR_EXTERNAL,
                                   cv2.CHAIN_APPROX_SIMPLE)
    heights = [cv2.boundingRect(c)[3] for c in contours
               if cv2.boundingRect(c)[2] > grey.shape[1] * 0.04]
    heights = [h for h in heights if 4 <= h <= grey.shape[0] * 0.05]
    return float(np.median(heights)) if len(heights) >= 5 else 0.0


def measure(image: np.ndarray) -> ImageQuality:
    """Measure every capture defect of one rendered page."""
    grey = _grey(image)
    grey_f = grey.astype(np.float32) / 255.0
    q = ImageQuality()

    q.skew_degrees = estimate_skew(grey)
    q.text_height_px = estimate_text_height(grey)

    # Noise: what a median filter removes.  Real strokes survive a 3x3 median;
    # sensor speckle does not, so the residual is close to a noise estimate.
    residual = cv2.absdiff(grey, cv2.medianBlur(grey, 3)).astype(np.float32)
    q.noise = float(residual.mean()) / 255.0

    # Contrast: the gap between the paper and the ink, as a fraction of full
    # range.  Measured over the two populations Otsu separates rather than
    # over percentiles of the whole page, because a percentile answers a
    # question about *coverage*: a sparse page is mostly paper, so its 2nd
    # percentile is still paper and the page reads as low contrast however
    # black its type is.
    mask = _ink_mask(grey) > 0
    if mask.any() and not mask.all():
        q.contrast = float(np.median(grey_f[~mask]) - np.median(grey_f[mask]))
    else:
        q.contrast = 1.0

    # Illumination: how much the *paper* brightness drifts across the page.
    # Blurring alone would measure the density of the type instead -- a page of
    # dense tables averages darker than one with wide margins, and that is not
    # a lighting defect.  A morphological closing with a kernel wider than any
    # stroke removes the text and leaves the paper it was printed on.
    kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (25, 25))
    paper = cv2.morphologyEx(grey, cv2.MORPH_CLOSE, kernel)
    paper = cv2.resize(paper, (32, 32), interpolation=cv2.INTER_AREA)
    paper_f = cv2.GaussianBlur(paper.astype(np.float32) / 255.0, (0, 0), 2.0)
    q.illumination_range = float(paper_f.max() - paper_f.min())

    # Sharpness: variance of the Laplacian, measured over ink only so that a
    # page with wide margins is not called blurry for being mostly blank.
    lap = cv2.Laplacian(grey_f, cv2.CV_32F)
    q.sharpness = float(lap[mask].var()) if mask.any() else 0.0

    if abs(q.skew_degrees) > SKEW_DEGREES:
        q.defects.append("skew")
    if q.noise > NOISE_RESIDUAL:
        q.defects.append("noise")
    if q.contrast < LOW_CONTRAST:
        q.defects.append("low_contrast")
    if q.illumination_range > UNEVEN_LIGHT:
        q.observations.append("uneven_illumination")
    if q.sharpness < BLUR_VARIANCE:
        q.defects.append("blur")
    if 0.0 < q.text_height_px < SMALL_TEXT_PX:
        q.defects.append("small_text")
    return q
