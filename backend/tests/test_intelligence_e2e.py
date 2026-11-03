"""End-to-end Milestone 2 checks against real documents.

These are the tests that need an actual PDF, so they are marked ``slow``.  Two
real documents are used, both already in ``data/benchmark``:

* the RIL Q1 FY2026-27 media release (has a text layer);
* the same filing rendered to a genuinely image-only PDF for Milestone 1.5
  (no text layer at all).

The second is a real document with real financial contents -- nothing was
fabricated to create it, it is the first one's own pages as images.  Using it
for the cross-document comparison has a useful property: because the underlying
figures are identical, every matched metric *should* compare to no change, so
any difference the comparison reports is a genuine extraction inconsistency
between the two representations rather than a business movement.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from app.core.ids import sha256_file
from app.database import repository as repo
from app.database.session import session_scope

BENCH = Path(__file__).resolve().parents[2] / "data" / "benchmark"
NATIVE = BENCH / "Media_Release_RIL_Q1_FY2026-27_Financial_and_Operational_Performance.pdf"
IMAGE_ONLY = BENCH / "RIL_Q1_FY27_IMAGE_ONLY.pdf"

pytestmark = pytest.mark.slow


# The pipeline is run once per PDF for the whole module and the result cached in
# memory.  Each test then gets a fresh database (conftest resets it per test)
# into which the cached result is replayed, so the assertions are made against
# real stored rows without reprocessing a 34-page PDF ten times over.
_RESULTS: dict[str, object] = {}


def _process_once(pdf: Path, document_id: str):
    if document_id not in _RESULTS:
        from app.ocr.registry import get_engine
        from app.services.pipeline import Pipeline
        get_engine().warmup()
        _RESULTS[document_id] = Pipeline(document_id, pdf).run()
    return _RESULTS[document_id]


def _materialise(pdf: Path, document_id: str) -> str:
    from app.services.processor import persist_result
    result = _process_once(pdf, document_id)
    with session_scope() as s:
        repo.create_document(
            s, document_id=document_id, filename=pdf.name,
            original_filename=pdf.name, stored_path=str(pdf),
            content_hash=sha256_file(pdf), size_bytes=pdf.stat().st_size)
    persist_result(document_id, result)
    return document_id


@pytest.fixture
def native_doc() -> str:
    if not NATIVE.is_file():
        pytest.skip("benchmark PDF not present")
    return _materialise(NATIVE, "m2_native")


@pytest.fixture
def image_only_doc() -> str:
    if not IMAGE_ONLY.is_file():
        pytest.skip("image-only benchmark PDF not present")
    return _materialise(IMAGE_ONLY, "m2_image_only")


# ------------------------------------------------------------------- §33 RIL
class TestRealDocumentIntelligence:
    def test_entities_are_discovered_and_nested(self, native_doc, session):
        from app.database.models_intelligence import EntityRow
        from sqlalchemy import select
        rows = list(session.scalars(select(EntityRow)
                                    .where(EntityRow.document_id == native_doc)))
        assert len(rows) >= 4, "the filing names several reporting units"
        types = {r.entity_type for r in rows}
        assert "consolidated" in types
        assert "segment" in types
        # every non-root entity points at a parent that exists
        ids = {r.id for r in rows}
        for r in rows:
            if r.parent_entity_id:
                assert r.parent_entity_id in ids

    def test_every_fact_carries_its_entity_and_statement(self, native_doc, session):
        from app.database.models import FinancialFactRow
        from sqlalchemy import select
        facts = list(session.scalars(
            select(FinancialFactRow)
            .where(FinancialFactRow.document_id == native_doc)))
        assert facts
        assert all(f.entity_id for f in facts)
        placed = [f for f in facts if f.statement and f.statement != "unknown"]
        assert len(placed) / len(facts) > 0.9

    def test_no_derived_metric_mixes_two_entities(self, native_doc, session):
        """Section 33: figures from different reporting units must never be
        combined into one calculation."""
        from app.database.models import FinancialFactRow
        from app.database.models_intelligence import DerivedMetricRow
        from sqlalchemy import select

        facts = {f.id: f for f in session.scalars(
            select(FinancialFactRow)
            .where(FinancialFactRow.document_id == native_doc))}
        derived = list(session.scalars(
            select(DerivedMetricRow)
            .where(DerivedMetricRow.document_id == native_doc)))
        assert derived, "the document supports at least some ratios"

        for metric in derived:
            inputs = [facts[fid] for fid in (metric.inputs or {}).values()
                      if fid in facts]
            entities = {f.entity_id for f in inputs}
            periods = {f.period for f in inputs}
            assert len(entities) <= 1, (
                f"{metric.canonical_metric} mixed entities {entities}")
            assert len(periods) <= 1, (
                f"{metric.canonical_metric} mixed periods {periods}")

    def test_reported_ratios_reconcile_or_are_flagged(self, native_doc, session):
        """Either the recomputation agrees with the document, or the
        disagreement is surfaced as an anomaly -- never silently dropped."""
        from app.database.models_intelligence import AnomalyRow, DerivedMetricRow
        from sqlalchemy import select

        disagreeing = [m for m in session.scalars(
            select(DerivedMetricRow)
            .where(DerivedMetricRow.document_id == native_doc))
            if m.agrees_with_reported is False]
        anomaly_metrics = {a.metric for a in session.scalars(
            select(AnomalyRow).where(
                AnomalyRow.document_id == native_doc,
                AnomalyRow.anomaly_type == "reported_vs_calculated"))}
        for m in disagreeing:
            assert m.canonical_metric in anomaly_metrics

    def test_no_fact_claims_a_currency_the_magnitude_contradicts(
            self, native_doc, session):
        """'Crore' is used only for rupees; a crore figure must not be dollars."""
        from app.database.models import FinancialFactRow
        from sqlalchemy import select
        offenders = [
            f for f in session.scalars(
                select(FinancialFactRow)
                .where(FinancialFactRow.document_id == native_doc))
            if f.unit in ("crore", "lakh") and f.currency not in ("INR", "UNKNOWN")
        ]
        assert not offenders, [
            (f.canonical_metric, f.unit, f.currency, f.source_page)
            for f in offenders]

    def test_insights_and_anomalies_reference_real_facts(self, native_doc, session):
        from app.database.models import FinancialFactRow
        from app.database.models_intelligence import AnomalyRow, InsightRow
        from sqlalchemy import select
        ids = {f.id for f in session.scalars(
            select(FinancialFactRow)
            .where(FinancialFactRow.document_id == native_doc))}
        for i in session.scalars(select(InsightRow)
                                 .where(InsightRow.document_id == native_doc)):
            assert i.supporting_fact_ids
            assert set(i.supporting_fact_ids) <= ids
        for a in session.scalars(select(AnomalyRow)
                                 .where(AnomalyRow.document_id == native_doc)):
            assert set(filter(None, a.fact_ids)) <= ids
            assert a.reason


# -------------------------------------------------- §34 two-document comparison
class TestCrossDocumentComparison:
    def test_two_real_documents_compare_on_matched_entities(
            self, native_doc, image_only_doc, session):
        from app.comparison import service as comparison
        result = comparison.compare(
            session, left_document_id=native_doc,
            right_document_id=image_only_doc)
        assert result["comparisons"], "some metric matched across both documents"

        matched = [c for c in result["comparisons"]
                   if c["left"]["value"] is not None
                   and c["right"]["value"] is not None]
        assert matched, "at least one metric present in both documents"

        # Both documents carry the same figures, so a like-for-like pair -- the
        # same metric, entity and period -- must agree.  Rows the service
        # labels period-over-period or not-comparable are excluded, because
        # those are genuinely different measurements.
        like_for_like = [c for c in matched
                         if c["change_basis"] == "like_for_like"]
        assert like_for_like, "the two readings share at least one exact period"

        disagreeing = []
        for c in like_for_like:
            left, right = c["left"]["value"], c["right"]["value"]
            scale = max(abs(left), abs(right)) or 1.0
            if abs(left - right) / scale >= 0.02:
                disagreeing.append(
                    f"{c['canonical_metric']} ({c['entity']}): "
                    f"{left} vs {right}")
        assert not disagreeing, (
            "the text-layer and image-only readings of the same filing "
            "disagree on: " + "; ".join(disagreeing))

    def test_comparison_never_pairs_different_entities(
            self, native_doc, image_only_doc, session):
        from app.comparison import service as comparison
        result = comparison.compare(
            session, left_document_id=native_doc,
            right_document_id=image_only_doc)
        for c in result["comparisons"]:
            # one entity per row, by construction
            assert isinstance(c["entity"], str) and c["entity"]

    def test_comparison_refuses_incomparable_pairs(self, native_doc,
                                                   image_only_doc, session):
        from app.comparison import service as comparison
        result = comparison.compare(
            session, left_document_id=native_doc,
            right_document_id=image_only_doc)
        for c in result["comparisons"]:
            if c["change_basis"] == "not_comparable":
                assert c["note"], "a refusal must say why"


# --------------------------------------------------------------- §27 the API
class TestIntelligenceApi:
    def test_every_intelligence_endpoint_answers(self, native_doc, client):
        for path in ("entities", "financial-summary", "derived-metrics",
                     "relations", "risks", "insights", "anomalies",
                     "table-semantics", "taxonomy-coverage"):
            r = client.get(f"/api/v1/documents/{native_doc}/{path}")
            assert r.status_code == 200, f"{path} -> {r.status_code}"
            assert isinstance(r.json(), dict)

    def test_explanation_endpoint_is_grounded(self, native_doc, client, session):
        from app.database.models import FinancialFactRow
        from sqlalchemy import select
        fact = session.scalars(
            select(FinancialFactRow)
            .where(FinancialFactRow.document_id == native_doc,
                   FinancialFactRow.period.is_not(None))).first()
        r = client.get(
            f"/api/v1/documents/{native_doc}/financials/{fact.id}/explanation")
        assert r.status_code == 200
        body = r.json()
        assert body["why_this_metric"] and body["why_this_period"]
        assert body["confidence_components"]
        assert "no language model" in body["generated_by"]

    def test_export_separates_reported_from_calculated(self, native_doc, client):
        import json
        r = client.get(f"/api/v1/documents/{native_doc}/export?format=json")
        assert r.status_code == 200
        payload = json.loads(r.content)
        assert "facts" in payload and "derived_metrics" in payload
        fact_ids = {f["fact_id"] if "fact_id" in f else f.get("id")
                    for f in payload["facts"]}
        derived_ids = {m["id"] for m in payload["derived_metrics"]}
        assert not (fact_ids & derived_ids), (
            "a calculated figure must never appear among reported facts")
        assert "never merged" in payload["notice"]

    def test_csv_export_carries_the_semantic_columns(self, native_doc, client):
        r = client.get(f"/api/v1/documents/{native_doc}/export?format=csv")
        assert r.status_code == 200
        header = r.content.decode("utf-8-sig").splitlines()[0]
        for column in ("entity_id", "entity_type", "statement", "category",
                       "derivation", "growth_calculated"):
            assert column in header
