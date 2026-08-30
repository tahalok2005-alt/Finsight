"""Ingestion, PDF validation, storage security and rendering."""
from __future__ import annotations

import io
from pathlib import Path

import pytest

from app.core.errors import (
    CorruptDocument, FileTooLarge, UnsupportedFileType, ValidationFailure,
)
from app.core.geometry import BBox
from app.ingestion import storage
from app.ingestion.validator import probe_pdf
from app.rendering.renderer import PageRenderer, image_dimensions, load_rgb


class TestFilenameSanitisation:
    @pytest.mark.parametrize("raw,expect_not_in", [
        ("../../etc/passwd.pdf", ".."),
        ("..\\..\\windows\\system32\\evil.pdf", ".."),
        ("/absolute/path/report.pdf", "/"),
        ("C:\\Users\\x\\report.pdf", "\\"),
    ])
    def test_path_components_are_stripped(self, raw, expect_not_in):
        safe = storage.sanitize_filename(raw)
        assert expect_not_in not in safe
        assert safe.endswith(".pdf")

    def test_windows_reserved_names_are_defused(self):
        assert storage.sanitize_filename("CON.pdf") != "CON.pdf"
        assert storage.sanitize_filename("LPT1.pdf").startswith("file_")

    def test_unicode_and_spaces_are_normalised(self):
        safe = storage.sanitize_filename("résultats financiers Q1.pdf")
        assert " " not in safe and safe.endswith(".pdf")

    def test_empty_falls_back(self):
        assert storage.sanitize_filename("") == "document.pdf"

    def test_extension_is_enforced(self):
        with pytest.raises(UnsupportedFileType):
            storage.validate_extension("payload.exe")
        with pytest.raises(UnsupportedFileType):
            storage.validate_extension("archive.zip")


class TestStorageIsolation:
    def test_document_dir_stays_under_upload_root(self):
        from app.config.settings import settings
        d = storage.document_dir("abcd1234")
        assert str(d).startswith(str(settings.upload_dir.resolve()))

    @pytest.mark.parametrize("bad", ["../escape", "a/b", "..", "x" * 100, "!!"])
    def test_traversal_ids_are_rejected(self, bad):
        with pytest.raises(ValidationFailure):
            storage.document_dir(bad)

    def test_size_limit_is_enforced_and_partial_file_removed(self, monkeypatch):
        from app.config.settings import settings
        monkeypatch.setattr(settings, "max_upload_mb", 0.001)
        payload = io.BytesIO(b"%PDF-1.4" + b"x" * 20000)
        with pytest.raises(FileTooLarge):
            storage.store_stream("sizetest01", "big.pdf", payload)
        assert not any(storage.document_dir("sizetest01").glob("*.pdf"))

    def test_empty_upload_is_rejected(self):
        with pytest.raises(ValidationFailure):
            storage.store_stream("emptytest1", "empty.pdf", io.BytesIO(b""))


class TestPdfValidation:
    def test_valid_pdf_probe(self, synthetic_pdf):
        probe = probe_pdf(synthetic_pdf)
        assert probe.page_count == 1
        assert probe.text_layer_chars > 100
        assert probe.is_encrypted is False

    def test_content_is_checked_not_extension(self, not_a_pdf):
        with pytest.raises(CorruptDocument):
            probe_pdf(not_a_pdf)

    def test_corrupt_body_is_rejected(self, corrupt_pdf):
        with pytest.raises((CorruptDocument, ValidationFailure)):
            probe_pdf(corrupt_pdf)

    def test_page_limit(self, synthetic_pdf, monkeypatch):
        from app.config.settings import settings
        monkeypatch.setattr(settings, "max_pages", 0)
        with pytest.raises(ValidationFailure):
            probe_pdf(synthetic_pdf)

    def test_scanned_detection_on_text_pdf(self, synthetic_pdf):
        assert probe_pdf(synthetic_pdf).is_scanned is False

    def test_image_only_pdf_is_flagged_as_scanned(self, tmp_path):
        """A page with no text layer must be recognised as needing OCR."""
        import pymupdf
        path = tmp_path / "scan.pdf"
        doc = pymupdf.open()
        for _ in range(3):
            doc.new_page(width=595, height=842)   # blank: no text layer
        doc.save(path)
        doc.close()
        probe = probe_pdf(path)
        assert probe.is_scanned is True
        assert probe.pages_with_text == 0
        assert probe.warnings


class TestRendering:
    def test_renders_and_caches(self, synthetic_pdf):
        with PageRenderer("rendertest1", synthetic_pdf) as r:
            first = r.render_page(1)
            assert first.from_cache is False
            assert first.image_path.is_file()
            assert first.width > 500 and first.height > 700
            second = r.render_page(1)
            assert second.from_cache is True
            assert (second.width, second.height) == (first.width, first.height)

    def test_image_loads_at_native_resolution(self, synthetic_pdf):
        """The array must match the stored file, not a 72-dpi re-render."""
        with PageRenderer("rendertest2", synthetic_pdf) as r:
            rendered = r.render_page(1)
            arr = r.load_image_array(1)
        assert arr.shape[:2] == (rendered.height, rendered.width)
        assert arr.shape[2] == 3
        assert image_dimensions(rendered.image_path) == (rendered.width,
                                                         rendered.height)

    def test_dpi_scale_relates_pdf_to_pixels(self, synthetic_pdf):
        with PageRenderer("rendertest3", synthetic_pdf) as r:
            rendered = r.render_page(1)
        assert rendered.scale == pytest.approx(rendered.dpi / 72.0)
        assert rendered.width == pytest.approx(
            rendered.pdf_width * rendered.scale, rel=0.02)

    def test_out_of_range_page(self, synthetic_pdf):
        from app.core.errors import StageFailure
        with PageRenderer("rendertest4", synthetic_pdf) as r:
            with pytest.raises(StageFailure):
                r.render_page(99)


class TestGeometry:
    def test_relative_coordinates_are_resolution_independent(self):
        box = BBox(100, 200, 50, 20)
        rel = box.to_relative(1000, 2000)
        assert rel == {"x": 0.1, "y": 0.1, "width": 0.05, "height": 0.01}
        assert box.scaled(2).to_relative(2000, 4000) == rel

    def test_intersection_and_iou(self):
        a, b = BBox(0, 0, 10, 10), BBox(5, 0, 10, 10)
        assert a.intersection(b).width == 5
        assert a.iou(b) == pytest.approx(5 * 10 / (10 * 10 + 10 * 10 - 50))
        assert a.intersection(BBox(100, 100, 5, 5)) is None

    def test_clipping_and_union(self):
        assert BBox(-5, -5, 20, 20).clipped(10, 10) == BBox(0, 0, 10, 10)
        u = BBox.union_all([BBox(0, 0, 5, 5), BBox(10, 10, 5, 5)])
        assert (u.x, u.y, u.x1, u.y1) == (0, 0, 15, 15)
        assert BBox.union_all([]) is None
