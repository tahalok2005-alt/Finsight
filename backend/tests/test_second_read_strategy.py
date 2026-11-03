"""Phase 22 performance: the second read renders each page once, not each cell.

The per-cell clip render decoded a scanned page's whole image once per cell
(610 ms a cell); rendering the page once and cropping every cell from it cut
a 29-page scanned filing's second read from 247 s to 69 s. The two methods'
readings were compared cell by cell over the corpus before the change: they
agreed on 5,758 of 5,763 cells, and the fast method raised every dispute the
slow one did plus three on correct figures. A dispute from the fast read now
stands only if the cell's own clip read disagrees too.
"""
from __future__ import annotations

from types import SimpleNamespace

import numpy as np
import pymupdf
import pytest

from app.config.settings import settings
from app.models.enums import FactKind
from app.models.schemas import BBoxModel, FinancialFact, Provenance
from app.ocr import second_read


@pytest.fixture()
def pdf(tmp_path):
    doc = pymupdf.open()
    page = doc.new_page(width=612, height=792)
    page.insert_text((400, 300), "24,545", fontsize=10)
    page.insert_text((100, 300), "Other comprehensive income", fontsize=10)
    path = tmp_path / "t.pdf"
    doc.save(path)
    return path


def _fact(raw="24,545"):
    # the cell's box in 200 dpi pixels: "24,545" sits at about (400, 292) pt
    k = settings.render_dpi / 72.0
    return FinancialFact(
        fact_id="f", document_id="d", metric="Other comprehensive income",
        canonical_metric="other_comprehensive_income", value=24545.0, raw_value=raw,
        kind=FactKind.TABLE_CELL,
        provenance=Provenance(document_id="d", page_number=1,
                              bbox=BBoxModel(x=398 * k, y=291 * k, width=36 * k, height=11 * k),
                              original_text=raw, extraction_method="table_cell"))


class _Engine:
    """Answers the page crop with one reading and the clip with another."""

    info = SimpleNamespace(version="test")

    def __init__(self, crop_text, clip_text):
        self.calls = []
        self.crop_text, self.clip_text = crop_text, clip_text

    def engine(self, img):
        h, w = img.shape[:2]
        self.calls.append((h, w))
        text = self.clip_text if len(self.calls) > 1 else self.crop_text
        poly = [[0, 0], [w, 0], [w, h], [0, h]]
        return [[poly, text, 0.9]], None


@pytest.fixture()
def page_strategy(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "second_read_strategy", "page")
    monkeypatch.setattr(settings, "second_read_workers", 2)
    monkeypatch.setattr(settings, "cache_dir", tmp_path / "cache")


class TestOneRenderPerPage:
    def test_a_crop_is_the_clips_size(self, pdf):
        page = pymupdf.open(pdf)[0]
        f = _fact()
        crop = second_read._page_crops(page, [f.provenance.bbox])[0]
        rect = second_read._cell_rect(page, f.provenance.bbox)
        clip = page.get_pixmap(dpi=settings.second_read_dpi, clip=rect)
        assert crop.shape[:2] == (clip.height, clip.width)
        a = np.frombuffer(clip.samples, np.uint8).reshape(clip.height, clip.width, clip.n)[:, :, :3]
        # the same pixels but at the clip's edge
        inner = (slice(2, -2), slice(2, -2))
        assert np.abs(a[inner].astype(int) - crop[inner].astype(int)).max() <= 16


class TestADisputeIsConfirmedByTheCellsOwnClip:
    def test_the_fast_read_alone_does_not_dispute(self, pdf, page_strategy):
        f = _fact()
        engine = _Engine(crop_text="2455", clip_text="24,545")
        stats = second_read.verify([f], pdf, cache_scope="t", engine=engine)
        sr = f.meta["second_read"]
        assert sr["agrees"] is True and sr["text"] == "24,545"
        assert sr["page_crop_text"] == "2455"
        assert stats.confirmed == 1 and stats.disputed == 0

    def test_both_reads_disagreeing_is_a_dispute(self, pdf, page_strategy):
        f = _fact()
        engine = _Engine(crop_text="2455", clip_text="2,455")
        stats = second_read.verify([f], pdf, cache_scope="t2", engine=engine)
        assert f.meta["second_read"]["agrees"] is False
        assert stats.disputed == 1

    def test_an_agreeing_fast_read_needs_no_clip(self, pdf, page_strategy):
        f = _fact()
        engine = _Engine(crop_text="24,545", clip_text="should not be asked")
        second_read.verify([f], pdf, cache_scope="t3", engine=engine)
        assert len(engine.calls) == 1
        assert f.meta["second_read"]["agrees"] is True

    def test_a_warm_run_reads_nothing(self, pdf, page_strategy):
        engine = _Engine(crop_text="2455", clip_text="24,545")
        second_read.verify([_fact()], pdf, cache_scope="t4", engine=engine)
        again = _Engine(crop_text="x", clip_text="x")
        f = _fact()
        stats = second_read.verify([f], pdf, cache_scope="t4", engine=again)
        assert again.calls == [] and stats.cached == 1
        assert f.meta["second_read"]["agrees"] is True

    def test_the_two_strategies_never_share_a_cache(self, monkeypatch, tmp_path):
        monkeypatch.setattr(settings, "cache_dir", tmp_path)
        monkeypatch.setattr(settings, "second_read_strategy", "page")
        page_path = second_read._cache_path("s", 1, "v")
        monkeypatch.setattr(settings, "second_read_strategy", "clip")
        assert second_read._cache_path("s", 1, "v") != page_path
