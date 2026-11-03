"""Visual-only mode: proving the pipeline can work from pixels alone.

The central architectural claim of this system is that it understands financial
PDFs as *images*, not as glyph streams.  These tests exist to make that claim
falsifiable:

* the native-text boundary is enforced, including inside worker threads;
* a genuinely image-only PDF (no text, no fonts) is processed end to end;
* OCR produces bounding boxes, tables are rebuilt from them, facts are
  extracted, and provenance resolves to rendered-image coordinates;
* nothing silently falls back to the PDF text layer;
* hybrid mode still uses corroboration, so the two modes remain distinct.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pymupdf
import pytest

from app.core.modes import (
    AccessLedger, NativeTextAccessViolation, NativeTextScope, ProcessingMode,
    guard_native_text, is_visual_only, native_text_allowed, processing_mode,
)

BENCHMARK_DIR = Path(__file__).resolve().parents[2] / "data" / "benchmark"


# --------------------------------------------------------------------- fixture
@pytest.fixture(scope="module")
def image_only_pdf(tmp_path_factory) -> Path:
    """A PDF containing nothing but rasterised page images.

    Built here rather than committed so the test proves the property it relies
    on: the fixture is verified to carry zero text, zero words and zero fonts
    before any test uses it.
    """
    source = next(
        (p for p in [
            BENCHMARK_DIR / "Media_Release_RIL_Q1_FY2026-27_Financial_and_"
                            "Operational_Performance.pdf",
        ] if p.is_file()), None)
    if source is None:
        pytest.skip("benchmark PDF not present")

    out = tmp_path_factory.mktemp("imageonly") / "scan.pdf"
    src = pymupdf.open(source)
    dst = pymupdf.open()
    try:
        for page_no in (1, 4):          # a results table and a segment table
            page = src[page_no - 1]
            pix = page.get_pixmap(dpi=200, alpha=False)
            new = dst.new_page(width=page.rect.width, height=page.rect.height)
            new.insert_image(new.rect, pixmap=pix)
            del pix
        dst.save(out, garbage=4, deflate=True)
    finally:
        dst.close()
        src.close()
    return out


def test_fixture_really_has_no_text_layer(image_only_pdf):
    """Guard the guard: a fixture that still carried glyphs would prove nothing."""
    with pymupdf.open(image_only_pdf) as doc:
        assert doc.page_count == 2
        for i in range(doc.page_count):
            page = doc[i]
            assert (page.get_text("text") or "").strip() == ""
            assert page.get_text("words") == []
            assert [b for b in page.get_text("dict")["blocks"]
                    if b.get("type") == 0] == []
            assert page.get_fonts(full=True) == []
            assert len(page.get_images(full=True)) >= 1


# ------------------------------------------------------- the access boundary
class TestNativeTextBoundary:
    def test_corroboration_is_blocked_in_visual_only(self):
        with processing_mode(ProcessingMode.VISUAL_ONLY):
            with pytest.raises(NativeTextAccessViolation):
                guard_native_text("t", NativeTextScope.CORROBORATION)

    def test_corroboration_is_allowed_in_hybrid(self):
        with processing_mode(ProcessingMode.HYBRID) as ledger:
            guard_native_text("t", NativeTextScope.CORROBORATION)
        assert ledger.corroboration_accesses == 1
        assert ledger.summary()["extraction_fed_by_native_text"] is True

    @pytest.mark.parametrize("scope", [
        NativeTextScope.INGESTION_VALIDATION,
        NativeTextScope.EVALUATION_BASELINE,
    ])
    def test_non_extraction_scopes_stay_permitted(self, scope):
        """Metadata and measurement reads never feed extraction."""
        with processing_mode(ProcessingMode.VISUAL_ONLY) as ledger:
            guard_native_text("t", scope)          # must not raise
        assert ledger.corroboration_accesses == 0
        assert ledger.summary()["extraction_fed_by_native_text"] is False

    def test_textlayer_module_refuses_in_visual_only(self, benchmark_pdf):
        from app.ocr import textlayer
        with processing_mode(ProcessingMode.VISUAL_ONLY):
            with pytest.raises(NativeTextAccessViolation):
                textlayer.extract_words(benchmark_pdf, 1, 2.0)

    def test_textlayer_module_works_in_hybrid(self, benchmark_pdf):
        from app.ocr import textlayer
        with processing_mode(ProcessingMode.HYBRID):
            words = textlayer.extract_words(benchmark_pdf, 1, 200 / 72)
        assert words, "hybrid mode must still read the text layer"

    def test_boundary_holds_inside_worker_threads(self):
        """ContextVars do not cross into a thread pool by default.

        The pipeline analyses pages on worker threads, so the mode must be
        copied across explicitly.  Without that the guard would be inactive in
        exactly the threads that do the work -- this test pins the behaviour.
        """
        import contextvars

        def probe(_):
            return (is_visual_only(),
                    native_text_allowed(NativeTextScope.CORROBORATION))

        with processing_mode(ProcessingMode.VISUAL_ONLY):
            ctx = contextvars.copy_context()
            with ThreadPoolExecutor(max_workers=2) as pool:
                results = list(pool.map(lambda i: ctx.copy().run(probe, i),
                                        range(4)))
        assert all(visual and not allowed for visual, allowed in results)

    def test_mode_is_restored_after_the_block(self):
        assert not is_visual_only()
        with processing_mode(ProcessingMode.VISUAL_ONLY):
            assert is_visual_only()
        assert not is_visual_only()


# ------------------------------------------------------ end-to-end, no glyphs
@pytest.mark.slow
class TestImageOnlyProcessing:
    @pytest.fixture(scope="class")
    def result(self, image_only_pdf):
        from app.ocr.registry import get_engine
        from app.services.pipeline import Pipeline
        get_engine().warmup()
        return Pipeline("visualonly01", image_only_pdf, page_workers=1,
                        mode=ProcessingMode.VISUAL_ONLY).run()

    def test_pdf_with_no_text_layer_is_processed(self, result):
        """The headline question: can it work with no glyphs at all?"""
        assert result.status != "FAILED", result.error
        assert result.pages_done == 2
        assert result.pages_failed == 0

    def test_extraction_never_touched_native_text(self, result):
        audit = result.native_text_audit
        assert result.mode == "VISUAL_ONLY"
        assert audit["corroboration_accesses"] == 0
        assert audit["extraction_fed_by_native_text"] is False

    def test_ocr_produced_bounding_boxes(self, result):
        assert len(result.elements) > 50
        for el in result.elements:
            assert el.bbox.width > 0 and el.bbox.height > 0
            assert el.source == "ocr"
            # nothing came from the glyph stream
            assert el.corroborated is False
            assert el.aux_text is None
            assert el.currency_glyph is None

    def test_layout_regions_were_derived(self, result):
        assert result.regions
        assert any(str(r.type) == "table" for r in result.regions)

    def test_tables_reconstructed_from_ocr_alone(self, result):
        assert result.tables, "expected the results tables to be rebuilt"
        table = max(result.tables, key=lambda t: len(t.cells))
        assert table.n_rows >= 5 and table.n_cols >= 4
        assert table.cells
        # column periods came from OCR-read header text
        assert any(c.normalized_period for c in table.columns)
        for cell in table.cells:
            assert cell.bbox.width > 0 and cell.bbox.height > 0

    def test_financial_facts_extracted(self, result):
        """Both fixture pages carry a Q1 FY27 gross revenue -- the group's and
        a subsidiary's -- so the key includes the entity, which also checks
        that attribution survived with no text layer to lean on."""
        assert len(result.facts) > 20
        by = {(f.canonical_metric, f.entity, f.period): f for f in result.facts}

        consolidated = by.get(("gross_revenue", "Consolidated", "Q1 FY27"))
        assert consolidated is not None, sorted(
            k for k in by if k[0] == "gross_revenue")
        assert consolidated.value == pytest.approx(340257.0)
        assert str(consolidated.unit) == "crore"

        # the subsidiary's own figure is kept separate, not merged into it
        subsidiary = by.get(("gross_revenue", "JPL", "Q1 FY27"))
        assert subsidiary is not None
        assert subsidiary.value == pytest.approx(45961.0)

    def test_currency_inferred_from_visible_text_not_glyphs(self, result):
        """PP-OCR cannot emit the rupee sign, so INR must come from context.

        "crore" is read from the pixels like any other word and is India-only,
        so the currency is deduced from it -- and flagged as inferred rather
        than presented as a symbol that was read.
        """
        monetary = [f for f in result.facts
                    if str(f.value_type) == "monetary" and str(f.unit) == "crore"]
        assert monetary
        assert all(str(f.currency) == "INR" for f in monetary)

    def test_provenance_points_at_rendered_image_coordinates(self, result):
        page_dims = {p.page_number: (p.render_width, p.render_height)
                     for p in result.pages}
        checked = 0
        for fact in result.facts:
            prov = fact.provenance
            assert prov is not None
            assert prov.bbox is not None
            assert prov.bbox_relative is not None
            width, height = page_dims[prov.page_number]
            # the box lies inside the rendered page, in pixel space
            assert 0 <= prov.bbox.x <= width
            assert 0 <= prov.bbox.y <= height
            assert 0.0 <= prov.bbox_relative.x <= 1.0
            assert 0.0 <= prov.bbox_relative.y <= 1.0
            assert prov.page_image_url and "/pages/" in prov.page_image_url
            checked += 1
        assert checked > 20

    def test_provenance_resolves_to_ocr_elements(self, result):
        by_id = {e.id: e for e in result.elements}
        table_facts = [f for f in result.facts if str(f.kind) == "table_cell"]
        assert table_facts
        linked = 0
        for fact in table_facts:
            for element_id in fact.provenance.ocr_element_ids:
                assert element_id in by_id, "provenance must name real OCR elements"
                assert by_id[element_id].source == "ocr"
                linked += 1
        assert linked > 0

    def test_validation_ran_on_visually_extracted_facts(self, result):
        assert result.validations
        growth = [v for v in result.validations if v.rule_family == "growth"]
        assert growth and any(str(v.status) == "PASSED" for v in growth)


# ------------------------------------------------------------ mode comparison
@pytest.mark.slow
class TestModesRemainDistinct:
    def test_hybrid_uses_corroboration_visual_only_does_not(self, synthetic_pdf):
        from app.ocr.registry import get_engine
        from app.services.pipeline import Pipeline
        get_engine().warmup()

        hybrid = Pipeline("modecmp_h", synthetic_pdf, page_workers=1,
                          mode=ProcessingMode.HYBRID).run()
        visual = Pipeline("modecmp_v", synthetic_pdf, page_workers=1,
                          mode=ProcessingMode.VISUAL_ONLY).run()

        assert hybrid.native_text_audit["extraction_fed_by_native_text"] is True
        assert visual.native_text_audit["extraction_fed_by_native_text"] is False
        # hybrid corroborates against the glyph stream; visual-only cannot
        assert any(e.corroborated for e in hybrid.elements)
        assert not any(e.corroborated for e in visual.elements)
        # both still extract the same headline figure from the same pixels
        for result in (hybrid, visual):
            values = {(f.canonical_metric, f.period): f.value for f in result.facts}
            assert values.get(("gross_revenue", "Q1 FY27")) == pytest.approx(12450.0)

        # ...and describe it identically.  Unit and value type are decided from
        # the pixels in both modes, so a difference here would mean the text
        # layer had reached a decision it must not reach.
        def described(result):
            return {(f.canonical_metric, f.period):
                    (str(f.unit), str(f.currency), str(f.value_type))
                    for f in result.facts}

        shared = set(described(hybrid)) & set(described(visual))
        assert shared, "the two modes produced no comparable facts"
        for key in shared:
            assert described(hybrid)[key] == described(visual)[key], key

    def test_visual_only_does_not_claim_a_text_layer(self, synthetic_pdf):
        from app.ocr.registry import get_engine
        from app.services.pipeline import Pipeline
        get_engine().warmup()
        visual = Pipeline("modecmp_v2", synthetic_pdf, page_workers=1,
                          mode=ProcessingMode.VISUAL_ONLY).run()
        # it deliberately does not look, so it must not report having found one
        assert all(p.has_text_layer is False for p in visual.pages)
        assert all(p.text_layer_chars == 0 for p in visual.pages)

# --------------------------------------------------------- §8 capture quality
class TestImageQuality:
    """What the page image says about how it was captured.

    Every signal is a statistic over the pixels, so each test builds the defect
    it is about and checks that the measurement finds it -- and, just as
    importantly, that a clean page is left alone.  No model file is involved
    anywhere, which is what makes these assertions stable.
    """

    @staticmethod
    def _page(text_rows: int = 24, height: int = 22) -> "np.ndarray":
        """A synthetic page of real glyphs on white paper.

        Drawn with actual text rather than solid bars: a bar has no edges in
        its interior, so it measures as blurred however crisp it is, and a
        fixture that misrepresents the thing being measured proves nothing.
        """
        import cv2
        import numpy as np
        img = np.full((1400, 1000, 3), 255, dtype=np.uint8)
        scale = height / 22.0
        for r in range(text_rows):
            y = 60 + int(r * height * 2.2) + height
            if y > 1360:
                break
            cv2.putText(img, "Revenue 12,450 EBITDA 2,490 Margin 20.0",
                        (80, y), cv2.FONT_HERSHEY_SIMPLEX, 0.62 * scale,
                        (20, 20, 20), max(1, round(scale)), cv2.LINE_AA)
        return img

    def test_a_clean_page_has_no_defects(self):
        from app.rendering.quality import measure
        q = measure(self._page(height=34))
        assert q.defects == []
        assert q.is_degraded is False
        assert q.contrast > 0.9 and abs(q.skew_degrees) < 0.25

    def test_skew_is_measured_and_signed(self):
        import cv2
        from app.rendering.quality import measure
        page = self._page(height=34)
        h, w = page.shape[:2]
        m = cv2.getRotationMatrix2D((w / 2, h / 2), 1.5, 1.0)
        turned = cv2.warpAffine(page, m, (w, h), borderValue=(255, 255, 255))
        q = measure(turned)
        assert "skew" in q.defects
        # the value is the correction: applying it levels the rows again
        assert q.skew_degrees == pytest.approx(-1.5, abs=0.4)

    def test_a_blurred_page_is_recognised_as_blurred(self):
        import cv2
        from app.rendering.quality import measure
        blurred = cv2.GaussianBlur(self._page(height=34), (0, 0), 2.5)
        assert "blur" in measure(blurred).defects

    def test_small_text_is_measured_in_pixels_not_dpi(self):
        """Nominal dpi says nothing: what matters is how many pixels a row of
        type actually occupies, which is what the recogniser sees."""
        from app.rendering.quality import measure
        big = measure(self._page(height=34))
        small = measure(self._page(text_rows=60, height=9))
        assert big.text_height_px > small.text_height_px
        assert "small_text" in small.defects
        assert "small_text" not in big.defects

    def test_a_washed_out_page_is_low_contrast(self):
        import numpy as np
        from app.rendering.quality import measure
        faint = self._page(height=34).astype(np.float32)
        faint = (faint * 0.35 + 150).clip(0, 255).astype(np.uint8)
        assert "low_contrast" in measure(faint).defects

    def test_uneven_illumination_is_observed_but_not_acted_on(self):
        """It is measured honestly and deliberately not made actionable: on
        the benchmark it cannot separate a lighting gradient from a printed
        colour block, and a signal that cannot tell a defect from a design
        must not drive a transformation."""
        import numpy as np
        from app.rendering.quality import measure
        page = self._page(height=34).astype(np.float32)
        ramp = np.linspace(1.0, 0.55, page.shape[1])[None, :, None]
        lit = (page * ramp).clip(0, 255).astype(np.uint8)
        q = measure(lit)
        assert "uneven_illumination" in q.observations
        assert "uneven_illumination" not in q.defects

    def test_the_measurement_is_reported_per_page(self):
        """A reader who sees a bad result should be able to see why."""
        from app.models.schemas import PageInfo
        assert "image_quality" in PageInfo.model_fields
        assert PageInfo.model_fields["ocr_image"].default == "original"

    def test_extraction_only_ever_reads_the_rendered_page(self):
        """No enhanced or intermediate image feeds extraction, so every
        bounding box addresses pixels the reader can actually be shown."""
        import app.services.pipeline as pipeline_module
        source = Path(pipeline_module.__file__).read_text(encoding="utf-8")
        assert "build_candidate" not in source
        assert "info.ocr_image" not in source
