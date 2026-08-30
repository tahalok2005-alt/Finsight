"""Background job execution and the evaluation framework."""
from __future__ import annotations

import pytest

from app.core.ids import sha256_file
from app.database import repository as repo
from app.database.models import JobRow
from app.database.session import session_scope
from app.models.enums import DocumentStatus


@pytest.fixture
def registered(synthetic_pdf):
    with session_scope() as s:
        repo.create_document(
            s, document_id="jobdoc0001", filename=synthetic_pdf.name,
            original_filename=synthetic_pdf.name,
            stored_path=str(synthetic_pdf),
            content_hash=sha256_file(synthetic_pdf),
            size_bytes=synthetic_pdf.stat().st_size)
    return "jobdoc0001"


@pytest.mark.slow
class TestJobQueue:
    def test_job_runs_and_is_recorded(self, registered):
        from app.ocr.registry import get_engine
        from app.workers.queue import JobQueue

        get_engine().warmup()
        q = JobQueue(max_workers=1)
        job_id = q.submit(registered)
        q.wait(job_id, timeout=300)
        q.shutdown(wait=True)

        with session_scope() as s:
            job = s.get(JobRow, job_id)
            assert job.state == "succeeded"
            assert job.attempts == 1
            assert job.finished_at is not None
            doc = repo.get_document(s, registered)
            assert doc.status != DocumentStatus.QUEUED.value
            assert repo.document_counts(s, registered)["fact_count"] > 0

    def test_failure_marks_both_job_and_document(self, tmp_path):
        from app.workers.queue import JobQueue

        missing = tmp_path / "gone.pdf"
        with session_scope() as s:
            repo.create_document(
                s, document_id="jobdoc0002", filename="gone.pdf",
                original_filename="gone.pdf", stored_path=str(missing),
                content_hash="0" * 64, size_bytes=1)

        q = JobQueue(max_workers=1)
        job_id = q.submit("jobdoc0002")
        q.wait(job_id, timeout=120)
        q.shutdown(wait=True)

        with session_scope() as s:
            doc = repo.get_document(s, "jobdoc0002")
            # the pipeline reports failure honestly rather than "completed"
            assert doc.status == DocumentStatus.FAILED.value
            assert doc.error


@pytest.mark.slow
class TestEvaluation:
    @pytest.fixture
    def processed(self, registered):
        from app.ocr.registry import get_engine
        from app.services.processor import process_document
        get_engine().warmup()
        process_document(registered, trigger="test")
        return registered

    def test_intrinsic_metrics(self, processed):
        from app.evaluation.runner import run_evaluation
        report = run_evaluation(processed)
        i = report["intrinsic"]
        assert i["pages"]["coverage"] == 1.0
        assert 0.0 < i["ocr"]["mean_confidence"] <= 1.0
        assert i["provenance"]["complete"] == 1.0
        assert i["normalization"]["period_resolved"] > 0.5
        assert i["tables"]["numeric_parse_rate"] == 1.0

    def test_accuracy_is_reported_as_unavailable_without_truth(self, processed):
        from app.evaluation.runner import run_evaluation
        report = run_evaluation(processed)
        assert report["accuracy"]["available"] is False
        assert "reason" in report["accuracy"]

    def test_accuracy_against_a_supplied_key(self, processed, tmp_path):
        """A key written from the synthetic PDF's known contents."""
        import json
        from app.evaluation.runner import run_evaluation

        truth = tmp_path / "key.json"
        truth.write_text(json.dumps({"facts": [
            {"canonical_metric": "gross_revenue", "entity": "Consolidated",
             "period": "Q1 FY27", "value": 12450, "unit": "crore", "page": 1},
            {"canonical_metric": "gross_revenue", "entity": "Consolidated",
             "period": "Q1 FY26", "value": 10910, "unit": "crore", "page": 1},
            {"canonical_metric": "ebitda", "entity": "Consolidated",
             "period": "Q1 FY27", "value": 2490, "unit": "crore", "page": 1},
            {"canonical_metric": "pat", "entity": "Consolidated",
             "period": "Q1 FY27", "value": 1200, "unit": "crore", "page": 1},
        ]}), encoding="utf-8")

        report = run_evaluation(processed, truth_path=truth)
        acc = report["accuracy"]
        assert acc["truth_facts"] == 4
        assert acc["matched"] == 4, acc["errors"]
        assert acc["precision"] == 1.0 and acc["recall"] == 1.0
        assert acc["unit_accuracy"] == 1.0

    def test_wrong_values_are_counted_not_hidden(self, processed, tmp_path):
        import json
        from app.evaluation.runner import run_evaluation

        truth = tmp_path / "bad.json"
        truth.write_text(json.dumps({"facts": [
            {"canonical_metric": "gross_revenue", "entity": "Consolidated",
             "period": "Q1 FY27", "value": 999999, "unit": "crore"},
        ]}), encoding="utf-8")
        acc = run_evaluation(processed, truth_path=truth)["accuracy"]
        assert acc["matched"] == 0
        assert acc["wrong_value"] == 1
        assert acc["errors"]["wrong_value"][0]["expected"] == 999999


class TestEntityNormalisationForScoring:
    def test_spelling_differences_do_not_penalise_attribution(self):
        from app.evaluation.runner import normalize_entity
        assert normalize_entity("Oil and Gas") == normalize_entity(
            "OIL AND GAS (EXPLORATION AND PRODUCTION)")
        assert normalize_entity(None) == "consolidated"
        assert normalize_entity("JPL") != normalize_entity("RRVL")


class TestBaseline:
    def test_baseline_extracts_less_than_the_visual_pipeline(self, synthetic_pdf):
        from app.evaluation.baseline import run_baseline
        naive = run_baseline(synthetic_pdf, mode="naive")
        strong = run_baseline(synthetic_pdf, mode="line_reconstructed")
        # the strong baseline pairs labels with figures; the naive one cannot
        assert len(strong.facts) >= len(naive.facts)
        # neither can attribute the comparative column, so figures are dropped
        assert strong.numbers_discarded > 0

    def test_baseline_finds_nothing_in_an_image_only_pdf(self, tmp_path):
        import pymupdf
        from app.evaluation.baseline import run_baseline

        path = tmp_path / "scan.pdf"
        doc = pymupdf.open()
        doc.new_page(width=595, height=842)
        doc.save(path)
        doc.close()
        result = run_baseline(path)
        assert result.facts == []
        assert result.pages_without_text == 1
