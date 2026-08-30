"""End-to-end pipeline: PDF in, validated facts with provenance out.

These run the real pipeline including OCR, so they are slower than the unit
tests.  They use the generated synthetic PDF, so they do not depend on any
particular external document.
"""
from __future__ import annotations

import pytest

from app.database import repository as repo
from app.database.models import (
    FinancialFactRow, OCRElementRow, Page, ProvenanceRow, RegionRow,
    TableCellRow, TableRow,
)
from app.database.session import session_scope
from app.models.enums import DocumentStatus, MagnitudeUnit, StageStatus
from app.services.pipeline import Pipeline

pytestmark = pytest.mark.slow


@pytest.fixture(scope="module")
def _engine_warm():
    """Load the OCR model once for the whole module."""
    from app.ocr.registry import get_engine
    get_engine().warmup()


@pytest.fixture
def result(synthetic_pdf, _engine_warm):
    return Pipeline("pipetest01", synthetic_pdf, page_workers=1).run()


class TestPipelineHappyPath:
    def test_completes_with_an_honest_status(self, result):
        assert result.error is None
        assert result.status in (DocumentStatus.COMPLETED,
                                 DocumentStatus.REQUIRES_REVIEW)
        assert result.pages_done == 1
        assert result.pages_failed == 0

    def test_page_is_rendered_and_ocred(self, result):
        page = result.pages[0]
        assert page.status == StageStatus.SUCCEEDED
        assert page.render_width > 0 and page.render_height > 0
        assert page.ocr_element_count > 0
        assert 0.0 < page.ocr_mean_confidence <= 1.0

    def test_ocr_elements_keep_bounding_boxes(self, result):
        assert result.elements
        for el in result.elements[:20]:
            assert el.text.strip()
            assert el.bbox.width > 0 and el.bbox.height > 0
            assert 0.0 <= el.confidence <= 1.0
        # OCR is not reduced to one string
        assert len(result.elements) > 5

    def test_text_layer_corroborates_ocr(self, result):
        assert any(e.corroborated for e in result.elements)

    def test_layout_regions_are_typed_and_ordered(self, result):
        assert result.regions
        orders = [r.reading_order for r in result.regions]
        assert len(set(orders)) == len(orders)
        assert {str(r.type) for r in result.regions} - {"misc"}

    def test_table_is_detected_and_reconstructed(self, result):
        assert result.tables, "expected the synthetic results table"
        t = result.tables[0]
        assert t.n_rows >= 5 and t.n_cols >= 3
        assert t.cells
        grid_text = " ".join(c.text for c in t.cells)
        assert "12,450" in grid_text
        assert any(c.normalized_period == "Q1 FY27" for c in t.columns)

    def test_facts_carry_units_periods_and_provenance(self, result):
        assert result.facts
        by = {(f.canonical_metric, f.period): f for f in result.facts}
        rev = by.get(("gross_revenue", "Q1 FY27"))
        assert rev is not None and rev.value == 12450.0
        assert rev.unit == MagnitudeUnit.CRORE
        assert rev.provenance is not None
        assert rev.provenance.bbox is not None
        assert rev.provenance.bbox_relative is not None
        assert rev.provenance.page_image_url

    def test_reported_growth_is_validated(self, result):
        assert result.validations
        growth = [v for v in result.validations if v.rule_family == "growth"]
        assert growth
        assert any(v.status == "PASSED" for v in growth)

    def test_guidance_is_separated_from_actuals(self, result):
        assert result.guidance, "expected the capex guidance sentence"
        g = result.guidance[0]
        assert g.canonical_metric == "capex"
        assert all(str(f.nature) == "actual" for f in result.facts)

    def test_every_fact_has_a_confidence_band(self, result):
        for f in result.facts:
            assert 0.0 <= f.confidence <= 1.0
            assert f.confidence_band in ("HIGH", "MEDIUM", "LOW",
                                         "REQUIRES_REVIEW")
            assert f.confidence_breakdown is not None

    def test_stages_are_all_recorded(self, result):
        names = {s.stage for s in result.stages}
        assert {"validating", "rendering", "ocr", "layout", "tables",
                "extraction", "validating_facts"} <= names


class TestFailureHandling:
    def test_corrupt_pdf_fails_honestly(self, corrupt_pdf):
        r = Pipeline("badpipe01", corrupt_pdf, page_workers=1).run()
        assert r.status == DocumentStatus.FAILED
        assert r.error
        assert not r.facts

    def test_non_pdf_fails_honestly(self, not_a_pdf):
        r = Pipeline("badpipe02", not_a_pdf, page_workers=1).run()
        assert r.status == DocumentStatus.FAILED
        assert not r.facts

    def test_missing_file_fails_honestly(self, tmp_path):
        r = Pipeline("badpipe03", tmp_path / "nope.pdf", page_workers=1).run()
        assert r.status == DocumentStatus.FAILED
        assert r.error

    def test_ocr_engine_failure_is_reported_not_hidden(self, synthetic_pdf,
                                                       monkeypatch):
        from app.core.errors import EngineUnavailable
        import app.services.pipeline as pipeline_mod

        class BrokenEngine:
            info = type("I", (), {"name": "broken", "version": "0"})()

            def warmup(self):
                pass

            def recognize(self, *a, **k):
                raise EngineUnavailable("synthetic OCR failure")

        monkeypatch.setattr(pipeline_mod, "get_engine", lambda *a, **k: BrokenEngine())
        r = Pipeline("badpipe04", synthetic_pdf, page_workers=1).run()
        assert r.status == DocumentStatus.FAILED
        assert r.pages_failed == 1
        assert not r.facts


class TestPersistence:
    def test_full_round_trip_through_the_database(self, synthetic_pdf,
                                                  _engine_warm):
        from app.core.ids import sha256_file
        from app.services.processor import process_document

        with session_scope() as s:
            repo.create_document(
                s, document_id="persist01", filename=synthetic_pdf.name,
                original_filename=synthetic_pdf.name,
                stored_path=str(synthetic_pdf),
                content_hash=sha256_file(synthetic_pdf),
                size_bytes=synthetic_pdf.stat().st_size)

        result = process_document("persist01", trigger="test")
        assert result.status != DocumentStatus.FAILED

        with session_scope() as s:
            doc = repo.get_document(s, "persist01")
            assert doc.status == result.status
            assert doc.page_count == 1
            counts = repo.document_counts(s, "persist01")
            assert counts["fact_count"] == len(result.facts)
            assert counts["table_count"] == len(result.tables)

            assert s.query(Page).count() == 1
            assert s.query(OCRElementRow).count() > 0
            assert s.query(RegionRow).count() > 0
            assert s.query(TableCellRow).count() > 0
            # every fact keeps a provenance row
            assert (s.query(ProvenanceRow).count()
                    == s.query(FinancialFactRow).count())

    def test_reprocess_replaces_rather_than_duplicates(self, synthetic_pdf,
                                                       _engine_warm):
        from app.core.ids import sha256_file
        from app.services.processor import process_document

        with session_scope() as s:
            repo.create_document(
                s, document_id="persist02", filename=synthetic_pdf.name,
                original_filename=synthetic_pdf.name,
                stored_path=str(synthetic_pdf),
                content_hash=sha256_file(synthetic_pdf),
                size_bytes=synthetic_pdf.stat().st_size)

        process_document("persist02", trigger="test")
        with session_scope() as s:
            first = repo.document_counts(s, "persist02")
        process_document("persist02", trigger="reprocess")
        with session_scope() as s:
            second = repo.document_counts(s, "persist02")
            assert second["fact_count"] == first["fact_count"]
            assert s.query(Page).count() == 1
