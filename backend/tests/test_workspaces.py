"""Public mode: private workspaces, upload limits and automatic deletion.

The property that matters is that one visitor can never reach another's
documents by any route. ``TestEveryRoute`` checks it for every endpoint that
takes a document id, read from the application's own OpenAPI schema -- so an
endpoint added later that forgets the check fails here without anyone having
to remember to write a test for it.
"""
from __future__ import annotations

import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.config.settings import settings
from app.core import limits, workspace
from app.core.workspace import COOKIE, CSRF_HEADER
from app.database import repository as repo
from app.database.models import Document
from app.database.session import session_scope
from app.main import app

API = settings.api_prefix


@pytest.fixture
def public(monkeypatch):
    monkeypatch.setattr(settings, "workspaces_enabled", True)
    monkeypatch.setattr(settings, "max_active_per_workspace", 50)
    monkeypatch.setattr(settings, "max_active_documents", 500)
    monkeypatch.setattr(settings, "uploads_per_ip_per_hour", 500)
    limits.uploads_by_address.clear()
    yield
    limits.uploads_by_address.clear()


def _visitor() -> TestClient:
    """A browser of its own: its own cookie jar, so its own workspace."""
    return TestClient(app, headers={CSRF_HEADER: "1"})


def _upload(client: TestClient, pdf: Path, name: str = "report.pdf"):
    return client.post(f"{API}/documents",
                       files={"file": (name, pdf.read_bytes(), "application/pdf")})


@pytest.fixture
def two_visitors(public, inert_queue, synthetic_pdf):
    alice, bob = _visitor(), _visitor()
    r = _upload(alice, synthetic_pdf, "alice-q1-results.pdf")
    assert r.status_code == 201, r.text
    return alice, bob, r.json()["id"]


def _code(r) -> str | None:
    try:
        body = r.json()
    except ValueError:
        return None
    if not isinstance(body, dict) or not isinstance(body.get("error"), dict):
        return None  # a JSON null, a list, or a document whose "error" is null
    return body["error"].get("code")


# ------------------------------------------------------------- every route
def _fill(param: dict):
    schema = param.get("schema", {})
    if "enum" in schema:
        return schema["enum"][0]
    return {"integer": 1, "number": 1.0, "boolean": False}.get(schema.get("type"), "x")


def _document_operations():
    for path, ops in app.openapi()["paths"].items():
        if "{document_id}" not in path:
            continue
        for method, op in ops.items():
            yield method.upper(), path, op


_REVIEW = {"target_type": "fact", "target_id": "nofact", "action": "comment",
           "note": "probe"}


class TestEveryRoute:
    def test_the_schema_lists_the_routes_this_test_relies_on(self):
        # if the schema stopped listing document routes, the test below would
        # pass by checking nothing
        assert len(list(_document_operations())) >= 30

    def test_no_route_reaches_another_workspace_s_document(self, two_visitors):
        alice, bob, doc = two_visitors
        checked = []
        for method, path, op in _document_operations():
            url = path.format(document_id=doc, fact_id="nofact", page_number=1)
            params = {p["name"]: _fill(p) for p in op.get("parameters", [])
                      if p["in"] == "query" and p.get("required")}
            body = _REVIEW if "requestBody" in op else None

            r = bob.request(method, url, params=params, json=body)
            assert r.status_code == 404 and _code(r) == "document_not_found", (
                f"{method} {path} answered another workspace with "
                f"{r.status_code} {r.text[:160]}")

            if method not in ("DELETE",):
                # the control: the owner is not told the document is missing,
                # so the 404 above came from the workspace check
                mine = alice.request(method, url, params=params, json=body)
                assert _code(mine) != "document_not_found", (
                    f"{method} {path} refused the owner: {mine.text[:160]}")
            checked.append(f"{method} {path}")
        assert len(checked) >= 30
        # bob's DELETE above was refused: the document is still alice's
        assert alice.get(f"{API}/documents/{doc}").status_code == 200


# -------------------------------------------------- routes spanning documents
class TestCrossDocumentRoutes:
    def test_the_list_shows_only_your_own(self, two_visitors):
        alice, bob, doc = two_visitors
        assert [d["id"] for d in alice.get(f"{API}/documents").json()["items"]] == [doc]
        listing = bob.get(f"{API}/documents").json()
        assert listing["items"] == [] and listing["total"] == 0

    def test_the_overview_counts_only_your_own(self, two_visitors):
        alice, bob, _doc = two_visitors
        assert alice.get(f"{API}/overview").json()["documents"]["total"] == 1
        theirs = bob.get(f"{API}/overview").json()
        assert theirs["documents"]["total"] == 0
        assert theirs["recent_documents"] == []
        assert theirs["jobs"]["queued"] == 0

    def test_search_finds_nothing_of_anyone_else(self, two_visitors):
        alice, bob, doc = two_visitors
        assert [d["id"] for d in alice.get(
            f"{API}/search", params={"q": "alice"}).json()["results"]["documents"]] == [doc]
        found = bob.get(f"{API}/search", params={"q": "alice"}).json()["results"]
        assert all(v == [] for v in found.values()), found
        # nor by naming the document outright
        named = bob.get(f"{API}/search", params={"q": "alice", "document_id": doc})
        assert all(v == [] for v in named.json()["results"].values())

    def test_compare_will_not_open_someone_else_s_document(self, two_visitors):
        _alice, bob, doc = two_visitors
        r = bob.get(f"{API}/compare", params={"left": doc, "right": doc})
        assert r.status_code == 404

    def test_bulk_delete_reports_their_document_missing_and_leaves_it(self, two_visitors):
        alice, bob, doc = two_visitors
        r = bob.post(f"{API}/documents/bulk-delete", json={"document_ids": [doc]})
        assert r.json()["missing"] == [doc] and r.json()["deleted"] == []
        assert alice.get(f"{API}/documents/{doc}").status_code == 200

    def test_the_same_file_is_not_a_duplicate_of_someone_else_s(
            self, two_visitors, synthetic_pdf):
        alice, bob, doc = two_visitors
        # Searched instance-wide, this was a 409 carrying alice's document id
        # and file name -- telling bob she had uploaded this exact filing.
        r = _upload(bob, synthetic_pdf, "bobs-copy.pdf")
        assert r.status_code == 201
        assert doc not in r.text and "alice" not in r.text
        # within one workspace a duplicate is still caught
        assert _upload(alice, synthetic_pdf).status_code == 409


# ------------------------------------------------------------------- cookie
class TestWorkspaceCookie:
    def test_a_first_visit_is_given_a_workspace(self, public):
        r = _visitor().get(f"{API}/documents")
        cookie = r.headers["set-cookie"]
        assert cookie.startswith(f"{COOKIE}=") and "HttpOnly" in cookie
        assert "SameSite=Lax" in cookie and "Secure" not in cookie

    def test_over_https_it_survives_an_iframe_and_stays_partitioned(self, public):
        https = TestClient(app, base_url="https://testserver",
                           headers={CSRF_HEADER: "1"})
        cookie = https.get(f"{API}/documents").headers["set-cookie"]
        for part in ("Secure", "SameSite=None", "Partitioned", "HttpOnly"):
            assert part in cookie

    def test_a_returning_visitor_keeps_their_workspace(self, public):
        v = _visitor()
        v.get(f"{API}/documents")
        assert "set-cookie" not in v.get(f"{API}/documents").headers

    def test_a_forged_or_malformed_cookie_is_replaced(self, public):
        v = _visitor()
        v.cookies.set(COOKIE, "../../etc/passwd")
        assert v.get(f"{API}/documents").headers["set-cookie"].startswith(f"{COOKIE}=")

    def test_the_database_holds_a_hash_never_the_token(
            self, public, inert_queue, synthetic_pdf):
        v = _visitor()
        doc = _upload(v, synthetic_pdf).json()["id"]
        token = v.cookies.get(COOKIE)
        with session_scope() as s:
            stored = repo.get_document(s, doc).workspace_id
        assert stored == workspace.workspace_id_for(token) and token not in stored

    def test_with_workspaces_off_nothing_changes(self, inert_queue):
        r = TestClient(app).get(f"{API}/documents")
        assert r.status_code == 200 and "set-cookie" not in r.headers


class TestCrossSiteRequests:
    def test_a_change_without_the_dashboard_header_is_refused(self, public, synthetic_pdf):
        bare = TestClient(app)
        r = bare.post(f"{API}/documents",
                      files={"file": ("x.pdf", synthetic_pdf.read_bytes(), "application/pdf")})
        assert r.status_code == 403 and _code(r) == "missing_request_header"

    def test_reading_needs_no_header(self, public):
        assert TestClient(app).get(f"{API}/documents").status_code == 200


class TestFailsClosed:
    def test_with_no_workspace_in_context_nothing_is_visible(self, public):
        doc = Document(id="x" * 8, workspace_id="a" * 64)
        assert workspace.visible(doc) is False
        assert str(workspace.owns(Document)) == "false"


# ------------------------------------------------------------------- limits
class TestUploadLimits:
    def test_one_browser_cannot_fill_the_queue(
            self, public, inert_queue, synthetic_pdf, tmp_path, monkeypatch):
        monkeypatch.setattr(settings, "max_active_per_workspace", 1)
        v = _visitor()
        assert _upload(v, synthetic_pdf).status_code == 201
        other = tmp_path / "other.pdf"
        other.write_bytes(synthetic_pdf.read_bytes() + b"\n%second copy")
        r = _upload(v, other, "other.pdf")
        assert r.status_code == 429 and _code(r) == "too_many_requests"

    def test_a_fresh_cookie_does_not_reset_the_hourly_limit(
            self, public, inert_queue, synthetic_pdf, tmp_path, monkeypatch):
        monkeypatch.setattr(settings, "uploads_per_ip_per_hour", 2)
        for i in range(2):
            pdf = tmp_path / f"r{i}.pdf"
            pdf.write_bytes(synthetic_pdf.read_bytes() + f"\n%{i}".encode())
            assert _upload(_visitor(), pdf).status_code == 201
        r = _upload(_visitor(), synthetic_pdf)  # a brand-new workspace, same address
        assert r.status_code == 429

    def test_the_instance_says_it_is_busy_rather_than_queueing_forever(
            self, public, inert_queue, synthetic_pdf, tmp_path, monkeypatch):
        monkeypatch.setattr(settings, "max_active_documents", 1)
        assert _upload(_visitor(), synthetic_pdf).status_code == 201
        other = tmp_path / "other.pdf"
        other.write_bytes(synthetic_pdf.read_bytes() + b"\n%other")
        r = _upload(_visitor(), other)
        assert r.status_code == 503 and _code(r) == "service_busy"

    def test_a_refused_upload_writes_nothing_to_disk(
            self, public, inert_queue, synthetic_pdf, monkeypatch):
        monkeypatch.setattr(settings, "max_active_documents", 0)
        before = sorted(p.name for p in settings.upload_dir.iterdir())
        assert _upload(_visitor(), synthetic_pdf).status_code == 503
        assert sorted(p.name for p in settings.upload_dir.iterdir()) == before

    def test_a_private_instance_is_never_throttled(self, inert_queue, synthetic_pdf,
                                                   tmp_path, monkeypatch):
        monkeypatch.setattr(settings, "uploads_per_ip_per_hour", 0)
        monkeypatch.setattr(settings, "max_active_documents", 0)
        assert _upload(TestClient(app), synthetic_pdf).status_code == 201

    def test_the_window_forgets_old_uploads(self):
        w = limits.SlidingWindow(window=60)
        w.record("ip", now=0)
        w.record("ip", now=30)
        assert w.count("ip", now=59) == 2
        assert w.count("ip", now=61) == 1
        assert w.count("ip", now=200) == 0


# ---------------------------------------------------------------- retention
def _document(doc_id: str, *, hours_old: float, status: str = "COMPLETED") -> None:
    with session_scope() as s:
        repo.create_document(s, document_id=doc_id, filename="f.pdf",
                             original_filename="f.pdf", stored_path="/nowhere/f.pdf",
                             content_hash=doc_id.ljust(64, "0"), size_bytes=1)
        d = repo.get_document(s, doc_id)
        d.status = status
        d.created_at = datetime.now(timezone.utc) - timedelta(hours=hours_old)


def _aged_file(path: Path, hours_old: float) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"cached")
    t = time.time() - hours_old * 3600
    os.utime(path, (t, t))
    return path


class TestRetention:
    @pytest.fixture(autouse=True)
    def _one_day(self, monkeypatch):
        monkeypatch.setattr(settings, "retention_hours", 24)

    def test_off_unless_configured(self, monkeypatch):
        from app.services.retention import sweep_once
        monkeypatch.setattr(settings, "retention_hours", None)
        assert sweep_once() == {"enabled": False}

    def test_documents_past_the_window_are_deleted_and_younger_ones_kept(self):
        from app.services.retention import sweep_once
        _document("old00001", hours_old=30)
        _document("new00001", hours_old=2)
        result = sweep_once()
        assert result["documents_deleted"] == ["old00001"]
        with session_scope() as s:
            assert repo.get_document(s, "old00001") is None
            assert repo.get_document(s, "new00001") is not None

    def test_a_document_still_being_read_is_left_for_a_later_sweep(self, monkeypatch):
        from app.services.retention import sweep_once
        from app.workers.queue import queue
        _document("busy0001", hours_old=30, status="OCR_PROCESSING")
        monkeypatch.setattr(queue, "is_controllable", lambda _id: True)
        assert sweep_once()["deferred"] == ["busy0001"]
        with session_scope() as s:
            assert repo.get_document(s, "busy0001") is not None

    def test_cached_ocr_and_page_images_go_with_the_window(self):
        from app.services.retention import sweep_once
        stale = _aged_file(settings.cache_dir / "ocr" / ("ab" * 32) / "p0001.json", 30)
        render = _aged_file(settings.render_dir / "_by_content" / ("cd" * 32) / "p1.png", 30)
        fresh = _aged_file(settings.cache_dir / "ocr" / ("ef" * 32) / "p0001.json", 1)
        assert sweep_once()["cache_files_deleted"] >= 2
        assert not stale.exists() and not render.exists() and fresh.exists()

    def test_the_cache_is_not_swept_from_under_a_running_job(self, monkeypatch):
        from app.services.retention import sweep_once
        from app.workers.queue import JobQueue
        stale = _aged_file(settings.cache_dir / "ocr" / ("12" * 32) / "p.json", 30)
        monkeypatch.setattr(JobQueue, "active_count", property(lambda self: 1))
        assert sweep_once()["cache_files_deleted"] == 0
        assert stale.exists()

    def test_the_sweep_interval_is_bounded(self, monkeypatch):
        from app.services.retention import RetentionSweeper
        for hours, expected in ((0.01, 60.0), (1, 600.0 if 3600 / 4 > 600 else 900.0),
                                (1000, 600.0)):
            monkeypatch.setattr(settings, "retention_hours", hours)
            assert 60.0 <= RetentionSweeper.interval_seconds() <= 600.0


class TestDeleteRemovesEverything:
    def test_renders_and_crops_go_with_the_document(self, inert_queue, synthetic_pdf):
        from app.services.retention import purge_document_artifacts
        doc = "gone0001"
        render = _aged_file(settings.render_dir / doc / "p1.png", 0)
        crop = _aged_file(settings.cache_dir / "crops" / doc / "c.png", 0)
        purge_document_artifacts(doc)
        assert not render.exists() and not crop.exists()

    @pytest.mark.parametrize("bad", ["../escape", "..", "a/b", ""])
    def test_an_unsafe_id_never_names_a_path(self, bad):
        from app.services.retention import purge_document_artifacts
        keep = _aged_file(settings.render_dir / "keep" / "p.png", 0)
        purge_document_artifacts(bad)
        assert keep.exists()

    def test_the_delete_route_purges_them(self, client, synthetic_pdf):
        doc = client.post(f"{API}/documents", files={
            "file": ("d.pdf", synthetic_pdf.read_bytes(), "application/pdf")}).json()["id"]
        render = _aged_file(settings.render_dir / doc / "p1.png", 0)
        assert client.delete(f"{API}/documents/{doc}").status_code == 204
        assert not render.exists()
