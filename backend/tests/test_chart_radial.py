"""The radial test, and why it is safe to run it at a bounded size.

The circle search was 96.8% of the chart stage, measured across the corpus. It
asks a question about shape, and every parameter is a fraction of the region,
so the size of the working image should not change the answer. These tests hold
the implementation to that claim.

What they deliberately do NOT do is assert the corpus result from a synthetic
fixture. The claim that the cap removes a false positive comes from one real
page -- Standard Chartered's back cover -- and is recorded in
`_radial_score` and the phase report, where the evidence for it actually is.
Regular synthetic "text" rings the Hough accumulator in a way real text does
not, so a fixture built to prove that point would be proving something else.
"""
from __future__ import annotations

import cv2
import numpy as np
import pytest

from app.charts.detector import RADIAL_WORKING_SIZE, _radial_score


def pie(size: int, *, slices: int = 4) -> np.ndarray:
    """A filled circle with slice boundaries, on white, as grayscale."""
    img = np.full((size, size), 255, np.uint8)
    c, r = size // 2, int(size * 0.35)
    cv2.circle(img, (c, c), r, 90, -1)
    cv2.circle(img, (c, c), r, 0, max(1, size // 200))
    for i in range(slices):
        a = 2 * np.pi * i / slices
        cv2.line(img, (c, c),
                 (int(c + r * np.cos(a)), int(c + r * np.sin(a))), 0,
                 max(1, size // 300))
    return img


class TestTheAnswerDoesNotDependOnSize:
    """The one property that makes the cap safe."""

    @pytest.mark.parametrize("size", [500, 800, 1600, 2300, 3000])
    def test_a_pie_reads_as_radial_at_every_size(self, size):
        assert _radial_score(pie(size)) > 0.4

    @pytest.mark.parametrize("multiple", [2, 3, 4])
    def test_the_same_drawing_scores_the_same_large_or_small(self, multiple):
        """Above the cap the score must stop moving with the pixel count.

        This is the whole justification for downscaling, stated as a test: a
        region drawn at four times the size is the same region.
        """
        assert (_radial_score(pie(RADIAL_WORKING_SIZE * multiple))
                == _radial_score(pie(RADIAL_WORKING_SIZE)))


class TestWhatItRefuses:

    def test_a_region_too_small_to_judge_scores_nothing(self):
        assert _radial_score(np.full((40, 40), 255, np.uint8)) == 0.0

    def test_a_blank_region_is_not_radial(self):
        assert _radial_score(np.full((900, 900), 255, np.uint8)) == 0.0

    def test_a_plain_rectangle_is_not_radial(self):
        img = np.full((1200, 1600), 255, np.uint8)
        cv2.rectangle(img, (200, 200), (1400, 1000), 40, -1)
        assert _radial_score(img) <= 0.4


class TestTheOptimisationItself:

    def test_a_large_region_is_actually_downscaled(self, monkeypatch):
        """Guard the change: it must not quietly stop applying."""
        seen: list[tuple[int, int]] = []
        real = cv2.HoughCircles

        def spy(image, *a, **k):
            seen.append(image.shape[:2])
            return real(image, *a, **k)

        monkeypatch.setattr(cv2, "HoughCircles", spy)
        _radial_score(pie(RADIAL_WORKING_SIZE * 3))
        assert seen and min(seen[0]) == RADIAL_WORKING_SIZE

    def test_a_small_region_is_left_alone(self, monkeypatch):
        """Below the cap nothing changes, so nothing may be resized."""
        seen: list[tuple[int, int]] = []
        real = cv2.HoughCircles

        def spy(image, *a, **k):
            seen.append(image.shape[:2])
            return real(image, *a, **k)

        monkeypatch.setattr(cv2, "HoughCircles", spy)
        size = RADIAL_WORKING_SIZE // 2
        _radial_score(pie(size))
        assert seen and seen[0] == (size, size)
