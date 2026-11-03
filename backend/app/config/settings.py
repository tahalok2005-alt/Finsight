"""Application configuration.

All tunable behaviour of the pipeline is centralised here so that runs are
reproducible: the effective settings are hashed into the render/OCR cache keys
and recorded on every processing run.
"""
from __future__ import annotations

import os
import sys
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# repo root = .../backend/app/config/settings.py -> up 4
REPO_ROOT = Path(__file__).resolve().parents[3]


def _available_memory_mb() -> float | None:
    """Free physical memory, without taking a dependency to find out.

    Tries psutil where the environment happens to have it, then the platform's
    own answer -- Windows' GlobalMemoryStatusEx, Linux's /proc/meminfo -- and
    finally nothing, in which case the caller keeps the core-count answer
    rather than guessing.

    One function rather than a probe at each call site, so there is a single
    place to reason about and a single seam to substitute in a test.
    """
    try:  # not a dependency; used when present because it is the best answer
        import psutil  # noqa: PLC0415

        return psutil.virtual_memory().available / (1024 * 1024)
    except Exception:  # noqa: BLE001
        pass
    if sys.platform.startswith("win"):
        try:
            import ctypes  # noqa: PLC0415

            class _Status(ctypes.Structure):
                _fields_ = [("dwLength", ctypes.c_ulong),
                            ("dwMemoryLoad", ctypes.c_ulong),
                            ("ullTotalPhys", ctypes.c_ulonglong),
                            ("ullAvailPhys", ctypes.c_ulonglong),
                            ("ullTotalPageFile", ctypes.c_ulonglong),
                            ("ullAvailPageFile", ctypes.c_ulonglong),
                            ("ullTotalVirtual", ctypes.c_ulonglong),
                            ("ullAvailVirtual", ctypes.c_ulonglong),
                            ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]

            st = _Status()
            st.dwLength = ctypes.sizeof(_Status)
            if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(st)):
                return st.ullAvailPhys / (1024 * 1024)
        except Exception:  # noqa: BLE001
            return None
        return None
    try:
        with open("/proc/meminfo", encoding="utf-8") as fh:
            for line in fh:
                if line.startswith("MemAvailable:"):
                    return float(line.split()[1]) / 1024.0
    except Exception:  # noqa: BLE001
        return None
    return None


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="FINSIGHT_",
        env_file=str(REPO_ROOT / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # ---------------------------------------------------------------- general
    app_name: str = "FinSight"
    app_version: str = "1.0.0"
    environment: Literal["dev", "test", "prod"] = "dev"
    log_level: str = "INFO"
    log_json: bool = False

    # ------------------------------------------------------------- filesystem
    data_dir: Path = REPO_ROOT / "data"
    upload_dir: Path = REPO_ROOT / "data" / "uploads"
    render_dir: Path = REPO_ROOT / "data" / "renders"
    cache_dir: Path = REPO_ROOT / "data" / "cache"
    export_dir: Path = REPO_ROOT / "data" / "exports"

    # --------------------------------------------------------------- database
    database_url: str = f"sqlite:///{(REPO_ROOT / 'data' / 'finsight.db').as_posix()}"
    db_echo: bool = False

    # --------------------------------------------------------------- ingestion
    max_upload_mb: int = 200
    allowed_extensions: tuple[str, ...] = (".pdf",)
    max_pages: int = 500

    # --------------------------------------------------------------- rendering
    render_dpi: int = 200
    render_format: Literal["png", "webp"] = "png"
    thumbnail_width: int = 320
    render_max_pixels: int = 40_000_000  # guard against absurd page sizes

    # --------------------------------------------------------------------- ocr
    ocr_engine: str = "rapidocr"
    ocr_det_limit_side_len: int = 1600
    ocr_text_score: float = 0.35
    ocr_use_angle_cls: bool = False
    ocr_rec_batch_num: int = 16
    #: ONNX intra-op threads per OCR session.  Measured on a 12-core machine
    #: over 24 pages of a real 165-page report, with the OCR cache bypassed:
    #:
    #:   workers=2 intra=4  363.3 s   (the previous default)
    #:   workers=4 intra=3  291.9 s   -19.7%
    #:   workers=6 intra=2  276.0 s   -24.0%
    #:   workers=4 intra=4  281.0 s   -22.7%
    #:   workers=8 intra=2  255.1 s   -29.8%
    #:
    #: Extraction was byte-identical across all five (119 facts, 38 tables), as
    #: it must be -- pages are independent, so concurrency cannot change what is
    #: read.  Fewer intra-op threads and more concurrent pages wins because
    #: ONNX scales poorly within one session but the pages parallelise cleanly.
    ocr_intra_threads: int = 2
    ocr_min_confidence: float = 0.30
    #: Phase 22: every table figure's cell is read a second time, alone, from a
    #: 400 dpi render of just that cell. Measured on the 1,687 golden figures
    #: the corpus finds: it disagreed with 1 correct value in 1,681 and with
    #: every digit misread the page read made (Eternal's image cash flow,
    #: "(71)" for "(711)", "(168)" for "(1,168)", "(167)" for "(161)"). A
    #: disagreement sends the figure to review; it never changes the value.
    second_read_enabled: bool = True
    second_read_dpi: int = 400
    #: Margin around the cell box, in 200 dpi pixels: a tight crop cut digits
    #: off right-aligned figures ("7,52" for "7,521").
    second_read_pad_x: int = 24
    second_read_pad_y: int = 8
    #: How the cells are rendered for the second read. "clip" renders each
    #: cell's clip from the PDF on its own -- on a scanned page that decodes
    #: the page image once PER CELL (610 ms a cell, measured). "page" renders
    #: the page once at the second-read resolution and crops every cell from
    #: it, then recognises the cells in parallel (Phase 22; see
    #: docs/PERFORMANCE.md for the controlled comparison of the two).
    second_read_strategy: str = "page"
    #: Threads recognising cells in parallel (0: the page-worker count).
    second_read_workers: int = 0

    # ------------------------------------------------------------------ layout
    layout_engine: str = "heuristic"  # pluggable: "heuristic" | "docling" | ...
    layout_column_gap_ratio: float = 0.035
    layout_line_merge_ratio: float = 0.6

    # ------------------------------------------------------------------ tables
    table_min_rows: int = 2
    table_min_cols: int = 2
    table_ruling_min_len_ratio: float = 0.10
    table_column_tolerance_ratio: float = 0.012

    # ------------------------------------------------------------- extraction
    metric_match_threshold: float = 0.82
    numeric_cell_ratio_for_data_row: float = 0.30

    # ------------------------------------------------------------- validation
    growth_tolerance_pct: float = 0.6      # abs pct-points on a reported growth
    margin_tolerance_pct: float = 0.35     # abs pct-points on a reported margin
    total_tolerance_rel: float = 0.02      # 2% relative on subtotal checks
    ratio_tolerance_rel: float = 0.06

    # ------------------------------------------------------------- confidence
    confidence_high: float = 0.85
    confidence_medium: float = 0.65
    review_threshold: float = 0.65
    #: A page slower than this is logged with its OCR time and element count.
    #: Set from the observed spread rather than a round number: a dense page at
    #: 200 dpi runs 10-15 s, so 45 s is well clear of normal and still catches a
    #: page that is genuinely stuck.
    slow_page_ms: float = 45_000.0

    # ---------------------------------------------------------------- workers
    #: How long a delete waits for an in-flight run to reach a checkpoint and
    #: unwind.  One page of OCR is the bound in practice.
    delete_cancel_timeout_s: float = 30.0
    #: How many documents may process at once.  A paused run holds its worker
    #: while it waits, so this is also the number of documents that can sit
    #: paused before the queue stalls.
    worker_threads: int = 3
    #: Pages processed concurrently within one document.  0 means "choose from
    #: the machine" -- see ``resolved_page_workers``.
    page_workers: int = 0

    # ------------------------------------------------------- optional local LLM
    llm_enabled: bool = False
    llm_provider: Literal["ollama", "none"] = "none"
    llm_base_url: str = "http://127.0.0.1:11434"
    llm_model: str = "qwen3:4b"
    llm_timeout_s: int = 120

    # -------------------------------------------------------------------- api
    api_prefix: str = "/api/v1"
    cors_origins: tuple[str, ...] = (
        "http://localhost:5173",
        "http://127.0.0.1:5173",
        "http://localhost:4173",
    )

    @field_validator("data_dir", "upload_dir", "render_dir", "cache_dir", "export_dir")
    @classmethod
    def _abs(cls, v: Path) -> Path:
        return Path(v).resolve()

    def ensure_dirs(self) -> None:
        for d in (self.data_dir, self.upload_dir, self.render_dir,
                  self.cache_dir, self.export_dir):
            d.mkdir(parents=True, exist_ok=True)

    @property
    def render_signature(self) -> str:
        """Cache key component: changing any of these invalidates page renders."""
        return f"dpi{self.render_dpi}-{self.render_format}"

    #: Working memory one page worker needs: the rendered bitmap at 200 dpi,
    #: the float arrays OCR preprocessing derives from it, and that worker's
    #: share of the ONNX runtime arena.  Measured at roughly 350 MB peak; 400
    #: is that with room to be wrong in the safe direction.
    page_worker_memory_mb: int = 400
    #: Never taken below this, however little memory is free.  Two workers on a
    #: starved machine is slow; zero workers is broken.
    min_page_workers: int = 2

    @property
    def resolved_page_workers(self) -> int:
        """Concurrent pages, chosen from the machine unless pinned.

        Two thirds of the logical cores, capped at eight -- and then capped
        again by the memory actually free.

        The core-count rule alone is what a machine can compute; it says
        nothing about what it can hold.  On a 16 GB machine with 2.2 GB free it
        still asked for eight workers, and eight page bitmaps plus eight OCR
        arenas do not fit in 2.2 GB.  The run did not fail -- it thrashed. One
        batch of eight pages took 35 minutes where the median page took 59
        seconds, and a 75-page document that had taken 14 minutes took 48.

        That is the worst way for this to go wrong, because nothing reports an
        error: the pipeline simply appears to hang, which is exactly what was
        reported from the field. Sizing against free memory turns a stall into
        a slower but honest run.
        """
        if self.page_workers > 0:
            return self.page_workers
        cores = os.cpu_count() or 4
        by_cores = max(2, min(8, round(cores * 0.66)))
        by_memory = self._workers_that_fit_in_memory()
        if by_memory is None:
            return by_cores
        return max(self.min_page_workers, min(by_cores, by_memory))

    def _workers_that_fit_in_memory(self) -> int | None:
        """How many workers the free memory supports, or None if unknowable.

        Deliberately reads *available* rather than total memory: the question
        is what this run can have now, not what the machine came with.
        """
        available_mb = _available_memory_mb()
        if not available_mb:
            return None
        # Leave a quarter of what is free for everything that is not a page
        # worker: the request thread, SQLite, the OS file cache.
        usable = available_mb * 0.75
        return max(1, int(usable // max(1, self.page_worker_memory_mb)))

    @property
    def ocr_signature(self) -> str:
        return (f"{self.ocr_engine}-side{self.ocr_det_limit_side_len}"
                f"-ts{self.ocr_text_score}-cls{int(self.ocr_use_angle_cls)}")


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    s = Settings()
    if os.environ.get("FINSIGHT_TESTING") == "1":
        s.environment = "test"
    s.ensure_dirs()
    return s


settings = get_settings()
