"""API contract tests."""
from __future__ import annotations

import pytest

from app.config.settings import settings
from app.core.ids import sha256_file
from app.database import repository as repo
from app.database.session import session_scope

API = settings.api_prefix


@pytest.fixture
def processed(synthetic_pdf):
    """A fully processed document available to every read endpoint."""
    from app.ocr.registry import get_engine
    from app.services.processor import process_document

    get_engine().warmup()
    with session_scope() as s:
        repo.create_document(
            s, document_id="apidoc0001", filename=synthetic_pdf.name,
            original_filename=synthetic_pdf.name,
            stored_path=str(synthetic_pdf),
            content_hash=sha256_file(synthetic_pdf),
            size_bytes=synthetic_pdf.stat().st_size)
    process_document("apidoc0001", trigger="test")
    return "apidoc0001"


class TestSystem:
    def test_health(self, client):
        r = client.get(f"{API}/health")
        assert r.status_code == 200
        body = r.json()
        assert body["status"] == "ok"
        assert body["database"]["ok"] is True

    def test_system_reports_engines_honestly(self, client):
        body = client.get(f"{API}/system").json()
        assert "rapidocr" in body["ocr"]["engines"]
        # an engine that cannot run must not claim it can
        for name, info in body["ocr"]["engines"].items():
            if not info["available"]:
                assert info.get("error")

    def test_llm_is_optional_and_off_by_default(self, client):
        body = client.get(f"{API}/system").json()
        assert body["llm"]["enabled"] is False

    def test_openapi_and_docs(self, client):
        assert client.get("/openapi.json").status_code == 200
        assert client.get("/docs").status_code == 200

    def test_metrics_taxonomy(self, client):
        body = client.get(f"{API}/metrics").json()
        keys = {m["key"] for m in body["metrics"]}
        assert {"revenue", "ebitda", "pat", "net_debt"} <= keys
        assert body["families"]


class TestUploadValidation:
    def test_rejects_non_pdf_content(self, client):
        r = client.post(f"{API}/documents",
                        files={"file": ("evil.pdf", b"not a pdf at all",
                                        "application/pdf")})
        assert r.status_code == 422
        assert r.json()["error"]["code"] in ("corrupt_document",
                                             "validation_failed")

    def test_rejects_wrong_extension(self, client):
        r = client.post(f"{API}/documents",
                        files={"file": ("payload.exe", b"%PDF-1.4 xx",
                                        "application/pdf")})
        assert r.status_code == 415

    def test_rejects_empty_file(self, client):
        r = client.post(f"{API}/documents",
                        files={"file": ("empty.pdf", b"", "application/pdf")})
        assert r.status_code == 422

    def test_accepts_valid_pdf_and_queues_it(self, client, synthetic_pdf):
        with open(synthetic_pdf, "rb") as fh:
            r = client.post(f"{API}/documents",
                            files={"file": (synthetic_pdf.name, fh,
                                            "application/pdf")})
        assert r.status_code == 201
        body = r.json()
        assert body["page_count"] == 1
        assert body["job_id"]
        # status must be honest: queued, not "completed"
        assert body["status"] in ("UPLOADED", "QUEUED")

    def test_duplicate_is_detected_by_content_hash(self, client, synthetic_pdf):
        for _ in range(2):
            with open(synthetic_pdf, "rb") as fh:
                r = client.post(f"{API}/documents",
                                files={"file": ("a.pdf", fh, "application/pdf")})
        assert r.status_code == 409
        assert "document_id" in r.json()["error"]["detail"]

    def test_filename_traversal_is_neutralised(self, client, synthetic_pdf):
        with open(synthetic_pdf, "rb") as fh:
            r = client.post(
                f"{API}/documents",
                files={"file": ("../../../etc/evil.pdf", fh, "application/pdf")})
        assert r.status_code == 201
        assert ".." not in r.json()["filename"]


class TestNotFound:
    def test_unknown_document(self, client):
        r = client.get(f"{API}/documents/doesnotexist")
        assert r.status_code == 404
        assert r.json()["error"]["code"] == "document_not_found"

    def test_unknown_page(self, client, processed):
        r = client.get(f"{API}/documents/{processed}/pages/99")
        assert r.status_code == 404


class TestReadEndpoints:
    def test_list_and_get(self, client, processed):
        listing = client.get(f"{API}/documents").json()
        assert listing["total"] >= 1
        doc = client.get(f"{API}/documents/{processed}").json()
        assert doc["id"] == processed
        assert doc["fact_count"] > 0

    def test_status_reports_real_stages(self, client, processed):
        body = client.get(f"{API}/documents/{processed}/status").json()
        assert body["progress"] == 1.0
        names = {s["stage"] for s in body["stages"]}
        assert {"rendering", "ocr", "layout", "extraction"} <= names
        assert body["pages_total"] == 1

    def test_pages_and_regions(self, client, processed):
        pages = client.get(f"{API}/documents/{processed}/pages").json()
        assert pages["total"] == 1
        page = client.get(
            f"{API}/documents/{processed}/pages/1?include_ocr=true").json()
        assert page["ocr_elements"]
        assert page["regions"]
        for r in page["regions"][:5]:
            rel = r["bbox_relative"]
            assert 0.0 <= rel["x"] <= 1.0 and 0.0 <= rel["y"] <= 1.0

    def test_page_image_and_crop(self, client, processed):
        img = client.get(f"{API}/documents/{processed}/pages/1/image")
        assert img.status_code == 200
        assert img.headers["content-type"] == "image/png"
        crop = client.get(
            f"{API}/documents/{processed}/pages/1/crop?x=50&y=50&w=120&h=40")
        assert crop.status_code == 200
        assert crop.headers["content-type"] == "image/png"
        assert len(crop.content) > 100

    def test_financials_with_filters_and_facets(self, client, processed):
        body = client.get(
            f"{API}/documents/{processed}/financials?metric=gross_revenue").json()
        assert body["total"] >= 1
        assert body["facets"]["periods"]
        f = body["items"][0]
        assert f["provenance"]["bbox"] is not None
        assert f["provenance"]["page_image_url"]

    def test_tables_expose_grid_and_cells(self, client, processed):
        body = client.get(f"{API}/documents/{processed}/tables").json()
        assert body["total"] >= 1
        t = body["items"][0]
        assert t["grid"] and t["cells"]
        assert t["columns"]

    def test_validation_summary(self, client, processed):
        body = client.get(f"{API}/documents/{processed}/validation").json()
        assert "summary" in body and "by_family" in body

    def test_commentary_guidance_charts(self, client, processed):
        for ep in ("commentary", "guidance", "charts"):
            r = client.get(f"{API}/documents/{processed}/{ep}")
            assert r.status_code == 200
            assert "items" in r.json()

    def test_provenance_resolves_to_a_source_region(self, client, processed):
        facts = client.get(f"{API}/documents/{processed}/financials").json()
        fid = facts["items"][0]["fact_id"]
        prov = client.get(f"{API}/documents/{processed}/provenance/{fid}").json()
        assert prov["page_number"] >= 1
        assert prov["bbox"] and prov["bbox_relative"]
        assert prov["original_text"]
        assert prov["page_image_url"]


class TestSearchAndCompare:
    def test_search(self, client, processed):
        body = client.get(f"{API}/search?q=EBITDA").json()
        assert body["total"] >= 1
        assert "facts" in body["results"]

    def test_compare_two_periods_of_one_document(self, client, processed):
        body = client.get(
            f"{API}/compare?left={processed}&right={processed}"
            f"&left_period=Q1%20FY26&right_period=Q1%20FY27").json()
        comps = {c["canonical_metric"]: c for c in body["comparisons"]}
        rev = comps.get("gross_revenue")
        assert rev is not None
        assert rev["left"]["value"] == 10910.0
        assert rev["right"]["value"] == 12450.0
        assert rev["change_basis"] in ("reported", "calculated")


class TestExport:
    @pytest.mark.parametrize("fmt,ctype", [
        ("json", "application/json"),
        ("csv", "text/csv"),
        ("xlsx", "spreadsheetml"),
    ])
    def test_formats(self, client, processed, fmt, ctype):
        r = client.get(f"{API}/documents/{processed}/export?format={fmt}")
        assert r.status_code == 200
        assert ctype in r.headers["content-type"]
        assert "attachment" in r.headers["content-disposition"]
        assert len(r.content) > 100

    def test_json_export_preserves_the_evidence_chain(self, client, processed):
        body = client.get(
            f"{API}/documents/{processed}/export?format=json").json()
        assert body["document"]["id"] == processed
        f = body["facts"][0]
        for key in ("metric", "value", "raw_value", "unit", "currency",
                    "period", "confidence", "validation_status", "source_page",
                    "bbox_x", "extraction_method"):
            assert key in f

    def test_csv_has_a_header_row(self, client, processed):
        text = client.get(
            f"{API}/documents/{processed}/export?format=csv").content.decode(
            "utf-8-sig")
        header = text.splitlines()[0]
        assert "canonical_metric" in header and "confidence" in header


class TestReview:
    def test_queue_then_approve_then_history(self, client, processed):
        queue = client.get(f"{API}/documents/{processed}/reviews").json()
        if queue["total"] == 0:
            pytest.skip("nothing needed review on this document")
        fid = queue["items"][0]["fact_id"]
        r = client.post(f"{API}/documents/{processed}/reviews", json={
            "target_type": "fact", "target_id": fid, "action": "approve",
            "reviewer": "tester", "note": "checked against source"})
        assert r.status_code == 201
        assert r.json()["result"]["review_state"] == "approved"

        history = client.get(
            f"{API}/documents/{processed}/reviews/history?target_id={fid}").json()
        assert history["total"] >= 1
        assert history["items"][0]["reviewer"] == "tester"

    def test_edit_preserves_the_extracted_value(self, client, processed):
        facts = client.get(f"{API}/documents/{processed}/financials").json()
        fact = facts["items"][0]
        original = fact["value"]
        r = client.post(f"{API}/documents/{processed}/reviews", json={
            "target_type": "fact", "target_id": fact["fact_id"],
            "action": "edit", "reviewer": "tester",
            "new_value": {"value": 424242.0}})
        assert r.status_code == 201
        result = r.json()["result"]
        assert result["corrected_value"] == 424242.0
        assert result["value"] == original      # the extraction is not destroyed

    def test_edit_without_a_value_is_rejected(self, client, processed):
        facts = client.get(f"{API}/documents/{processed}/financials").json()
        r = client.post(f"{API}/documents/{processed}/reviews", json={
            "target_type": "fact", "target_id": facts["items"][0]["fact_id"],
            "action": "edit", "reviewer": "tester"})
        assert r.status_code == 422

    def test_unknown_target_is_404(self, client, processed):
        r = client.post(f"{API}/documents/{processed}/reviews", json={
            "target_type": "fact", "target_id": "nope", "action": "approve"})
        assert r.status_code == 404


class TestOverview:
    def test_dashboard_overview(self, client, processed):
        body = client.get(f"{API}/overview").json()
        assert body["documents"]["total"] >= 1
        assert body["extraction"]["facts"] >= 1
        assert "validation" in body and "review_queue" in body
        assert body["recent_documents"]


class TestLifecycle:
    def test_delete_removes_everything(self, client, processed):
        assert client.delete(f"{API}/documents/{processed}").status_code == 204
        assert client.get(f"{API}/documents/{processed}").status_code == 404
        with session_scope() as s:
            assert repo.document_counts(s, processed)["fact_count"] == 0

    def test_reprocess_is_accepted(self, client, processed):
        r = client.post(f"{API}/documents/{processed}/reprocess")
        assert r.status_code == 202
        assert r.json()["status"] == "QUEUED"
