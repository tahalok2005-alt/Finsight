"""Shared test fixtures.

Every test runs against a throwaway SQLite database and a throwaway data
directory, so the suite never touches the developer's real documents.
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parents[1]
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

os.environ["FINSIGHT_TESTING"] = "1"

BENCHMARK_PDF = (BACKEND.parent / "data" / "benchmark"
                 / "Media_Release_RIL_Q1_FY2026-27_Financial_and_Operational_"
                   "Performance.pdf")


@pytest.fixture(scope="session", autouse=True)
def _isolated_storage(tmp_path_factory):
    """Point every filesystem-backed setting at a temporary tree."""
    from app.config.settings import settings

    root = tmp_path_factory.mktemp("finsight-data")
    settings.data_dir = root
    settings.upload_dir = root / "uploads"
    settings.render_dir = root / "renders"
    settings.cache_dir = root / "cache"
    settings.export_dir = root / "exports"
    settings.ensure_dirs()
    yield root


@pytest.fixture(autouse=True)
def db(_isolated_storage):
    """A fresh in-memory database per test."""
    from app.database.session import init_db, reset_engine

    reset_engine("sqlite:///:memory:")
    init_db()
    yield
    reset_engine("sqlite:///:memory:")


@pytest.fixture
def session():
    from app.database.session import get_session_factory
    s = get_session_factory()()
    try:
        yield s
    finally:
        s.close()


@pytest.fixture
def inert_queue(monkeypatch):
    """Record job submissions without executing them.

    The API's contract is that an upload is *queued*; running the real pipeline
    on a background thread during a test would outlive the test and race the
    teardown of the in-memory database, which crashes the SQLite driver.
    Pipeline execution is covered directly in ``test_pipeline.py``.
    """
    from app.database.session import session_scope
    from app.database import repository as repo
    from app.models.enums import DocumentStatus
    from app.workers.queue import queue as real_queue

    submitted: list[dict] = []

    def fake_submit(document_id, *, job_type="process", force_ocr=False):
        with session_scope() as s:
            job = repo.enqueue_job(s, document_id, job_type=job_type,
                                   payload={"force_ocr": force_ocr})
            repo.set_status(s, document_id, DocumentStatus.QUEUED)
            job_id = job.id
        submitted.append({"job_id": job_id, "document_id": document_id,
                          "job_type": job_type, "force_ocr": force_ocr})
        return job_id

    monkeypatch.setattr(real_queue, "submit", fake_submit)
    return submitted


@pytest.fixture
def client(inert_queue):
    """FastAPI test client bound to the per-test database."""
    from fastapi.testclient import TestClient
    from app.main import app
    with TestClient(app) as c:
        yield c


@pytest.fixture
def benchmark_pdf() -> Path:
    if not BENCHMARK_PDF.is_file():
        pytest.skip(f"benchmark PDF not present at {BENCHMARK_PDF}")
    return BENCHMARK_PDF


@pytest.fixture
def synthetic_pdf(tmp_path) -> Path:
    """A small generated financial PDF with a known table.

    Built with PyMuPDF so the suite does not depend on any external document.
    """
    import pymupdf

    path = tmp_path / "synthetic_results.pdf"
    doc = pymupdf.open()
    page = doc.new_page(width=595, height=842)
    y = 60

    def line(text: str, size: int = 10, bold: bool = False, x: float = 50):
        nonlocal y
        page.insert_text((x, y), text, fontsize=size,
                         fontname="hebo" if bold else "helv")
        y += size + 6

    line("ACME INDUSTRIES LIMITED", 14, True)
    line("CONSOLIDATED RESULTS FOR QUARTER ENDED 30TH JUNE, 2026", 11, True)
    line("(Rs in crore)", 9)
    y += 6

    rows = [
        ("Particulars", "1Q FY27", "1Q FY26", "% chg. Y-o-Y"),
        ("Gross Revenue", "12,450", "10,910", "14.1"),
        ("EBITDA", "2,490", "2,180", "14.2"),
        ("EBITDA margin (%)", "20.0", "20.0", "-"),
        ("Depreciation", "600", "550", "9.1"),
        ("Finance Costs", "300", "280", "7.1"),
        ("Profit Before Tax", "1,590", "1,350", "17.8"),
        ("Tax Expenses", "390", "330", "18.2"),
        ("Profit After Tax", "1,200", "1,020", "17.6"),
    ]
    cols = [50, 300, 390, 480]
    for r_i, row in enumerate(rows):
        for c_i, cell in enumerate(row):
            page.insert_text((cols[c_i], y), cell, fontsize=9,
                             fontname="hebo" if r_i == 0 else "helv")
        y += 16
    y += 10
    page.insert_text((50, y), "Revenue increased 14.1% Y-o-Y to Rs 12,450 crore.",
                     fontsize=9)
    y += 16
    page.insert_text((50, y),
                     "The company expects capital expenditure of Rs 3,000 crore "
                     "in FY28.", fontsize=9)

    doc.save(path)
    doc.close()
    return path


@pytest.fixture
def corrupt_pdf(tmp_path) -> Path:
    p = tmp_path / "corrupt.pdf"
    p.write_bytes(b"%PDF-1.4\nthis is not actually a pdf body\n%%EOF")
    return p


@pytest.fixture
def not_a_pdf(tmp_path) -> Path:
    p = tmp_path / "notes.pdf"
    p.write_bytes(b"just some plain text, definitely not a PDF")
    return p


@pytest.fixture
def empty_file(tmp_path) -> Path:
    p = tmp_path / "empty.pdf"
    p.write_bytes(b"")
    return p
