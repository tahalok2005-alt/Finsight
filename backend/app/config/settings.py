"""Application configuration.

All tunable behaviour of the pipeline is centralised here so that runs are
reproducible: the effective settings are hashed into the render/OCR cache keys
and recorded on every processing run.
"""
from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# repo root = .../backend/app/config/settings.py -> up 4
REPO_ROOT = Path(__file__).resolve().parents[3]


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
    ocr_intra_threads: int = 4
    ocr_min_confidence: float = 0.30

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

    # ---------------------------------------------------------------- workers
    worker_threads: int = 2
    page_workers: int = 2

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
