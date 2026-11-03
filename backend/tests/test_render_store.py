"""Phase 22 performance: a page is rendered once per file, not once per document id.

OCR was cached by the file's content hash, but renders by document id, so the
same bytes under a new id -- a re-upload, or a filing evaluated under a new
tag -- rendered every page again. Renders are now shared by content hash (and
MuPDF version); the per-document render is a link to, or a copy of, the same
bytes.
"""
from __future__ import annotations

import pymupdf
import pytest

from app.config.settings import settings
from app.rendering import renderer as renderer_mod
from app.rendering.renderer import PageRenderer


@pytest.fixture()
def pdf(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "render_dir", tmp_path / "renders")
    doc = pymupdf.open()
    page = doc.new_page(width=300, height=200)
    page.insert_text((40, 100), "Revenue 1,234", fontsize=12)
    path = tmp_path / "t.pdf"
    doc.save(path)
    return path


def test_the_same_bytes_under_a_new_id_are_not_rendered_again(pdf, monkeypatch):
    first = PageRenderer("doc_a", pdf, content_hash="h1").render_page(1)
    assert not first.from_cache
    calls = []
    real = pymupdf.Page.get_pixmap

    def counting(self, *a, **k):
        calls.append(1)
        return real(self, *a, **k)
    monkeypatch.setattr(pymupdf.Page, "get_pixmap", counting)
    second = PageRenderer("doc_b", pdf, content_hash="h1").render_page(1)
    assert second.from_cache and not calls
    assert second.image_path.read_bytes() == first.image_path.read_bytes()
    assert second.image_path != first.image_path


def test_without_a_hash_nothing_is_shared(pdf):
    PageRenderer("doc_a", pdf).render_page(1)
    assert not PageRenderer("doc_b", pdf).render_page(1).from_cache


def test_another_hash_is_another_render(pdf):
    PageRenderer("doc_a", pdf, content_hash="h1").render_page(1)
    assert not PageRenderer("doc_b", pdf, content_hash="h2").render_page(1).from_cache


def test_the_renderer_version_is_part_of_the_key(pdf):
    img, _ = renderer_mod._shared_paths("h1", 1, "dpi200-png")
    assert f"mupdf{pymupdf.VersionBind}" in str(img)
