"""SQLAlchemy ORM models.

Design notes
------------
* Core entities are **relational and queryable**; JSON columns are used only for
  genuinely open-ended metadata (engine params, detector notes, breakdowns).
* Types are chosen to be PostgreSQL-compatible: no SQLite-only constructs.
  ``JSONVariant`` maps to ``JSONB`` on PostgreSQL and ``JSON`` on SQLite.
* Every child row carries ``document_id`` so that per-document queries never
  need multi-level joins, and cascade deletes are cheap.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import (
    Boolean, CheckConstraint, DateTime, Float, ForeignKey, Index, Integer,
    String, Text, UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship
from sqlalchemy.types import JSON

JSONVariant = JSON().with_variant(JSONB(), "postgresql")


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False)


# ---------------------------------------------------------------- documents
class Document(Base, TimestampMixin):
    __tablename__ = "documents"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    filename: Mapped[str] = mapped_column(String(512), nullable=False)
    original_filename: Mapped[str] = mapped_column(String(512), nullable=False)
    stored_path: Mapped[str] = mapped_column(String(1024), nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    mime_type: Mapped[str] = mapped_column(String(128), default="application/pdf")
    #: The private workspace that uploaded this document: a hash of the
    #: visitor's browser cookie, never the cookie itself. NULL when workspaces
    #: are off, which is every local and single-team installation.
    workspace_id: Mapped[str | None] = mapped_column(String(64), index=True)

    status: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    error: Mapped[str | None] = mapped_column(Text)
    #: A short, human-readable note about where a run stopped -- "Paused at
    #: page 41 of 165".  Shown beside the status so a stopped document
    #: explains itself without the reader opening it.
    progress_note: Mapped[str | None] = mapped_column(String(256))

    page_count: Mapped[int] = mapped_column(Integer, default=0)
    pdf_version: Mapped[str | None] = mapped_column(String(32))
    is_encrypted: Mapped[bool] = mapped_column(Boolean, default=False)
    is_scanned: Mapped[bool] = mapped_column(Boolean, default=False)
    text_layer_chars: Mapped[int] = mapped_column(Integer, default=0)

    # document-level semantics inferred from content (never from filename)
    title: Mapped[str | None] = mapped_column(String(1024))
    company_name: Mapped[str | None] = mapped_column(String(512))
    report_period: Mapped[str | None] = mapped_column(String(64))
    report_type: Mapped[str | None] = mapped_column(String(64))
    document_currency: Mapped[str | None] = mapped_column(String(16))
    document_unit: Mapped[str | None] = mapped_column(String(32))
    pdf_metadata: Mapped[dict[str, Any]] = mapped_column(JSONVariant, default=dict)

    processing_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    processing_finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    processing_ms: Mapped[float | None] = mapped_column(Float)

    pages_processed: Mapped[int] = mapped_column(Integer, default=0)
    pages_failed: Mapped[int] = mapped_column(Integer, default=0)

    settings_snapshot: Mapped[dict[str, Any]] = mapped_column(JSONVariant, default=dict)

    pages: Mapped[list["Page"]] = relationship(
        back_populates="document", cascade="all, delete-orphan", lazy="selectin")
    runs: Mapped[list["ProcessingRun"]] = relationship(
        back_populates="document", cascade="all, delete-orphan")

    __table_args__ = (
        Index("ix_documents_status_created", "status", "created_at"),
    )


class Page(Base, TimestampMixin):
    __tablename__ = "pages"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    document_id: Mapped[str] = mapped_column(
        ForeignKey("documents.id", ondelete="CASCADE"), nullable=False, index=True)
    page_number: Mapped[int] = mapped_column(Integer, nullable=False)

    pdf_width: Mapped[float] = mapped_column(Float, nullable=False)
    pdf_height: Mapped[float] = mapped_column(Float, nullable=False)
    render_width: Mapped[int] = mapped_column(Integer, default=0)
    render_height: Mapped[int] = mapped_column(Integer, default=0)
    dpi: Mapped[int] = mapped_column(Integer, default=0)
    rotation: Mapped[int] = mapped_column(Integer, default=0)

    image_path: Mapped[str | None] = mapped_column(String(1024))
    thumbnail_path: Mapped[str | None] = mapped_column(String(1024))
    render_signature: Mapped[str | None] = mapped_column(String(64))

    has_text_layer: Mapped[bool] = mapped_column(Boolean, default=False)
    text_layer_chars: Mapped[int] = mapped_column(Integer, default=0)
    is_scanned: Mapped[bool] = mapped_column(Boolean, default=False)

    ocr_engine: Mapped[str | None] = mapped_column(String(64))
    ocr_signature: Mapped[str | None] = mapped_column(String(128))
    ocr_element_count: Mapped[int] = mapped_column(Integer, default=0)
    ocr_mean_confidence: Mapped[float | None] = mapped_column(Float)
    ocr_duration_ms: Mapped[float | None] = mapped_column(Float)
    #: Wall time this page spent in the page loop, end to end.  Without it a
    #: run that slows down cannot be told from one that is simply long, and the
    #: only honest answer to "which page is it stuck on" is a guess.
    duration_ms: Mapped[float | None] = mapped_column(Float)
    #: measured capture defects of this page, and which image OCR read
    image_quality: Mapped[dict] = mapped_column(JSONVariant, default=dict)
    ocr_image: Mapped[str] = mapped_column(String(64), default="original")

    region_count: Mapped[int] = mapped_column(Integer, default=0)
    table_count: Mapped[int] = mapped_column(Integer, default=0)
    chart_count: Mapped[int] = mapped_column(Integer, default=0)

    section_title: Mapped[str | None] = mapped_column(String(512))
    entity_hint: Mapped[str | None] = mapped_column(String(256))

    status: Mapped[str] = mapped_column(String(32), default="PENDING", index=True)
    error: Mapped[str | None] = mapped_column(Text)

    document: Mapped[Document] = relationship(back_populates="pages")

    __table_args__ = (
        UniqueConstraint("document_id", "page_number", name="uq_page_doc_number"),
        Index("ix_pages_doc_num", "document_id", "page_number"),
    )


class ProcessingRun(Base, TimestampMixin):
    __tablename__ = "processing_runs"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    document_id: Mapped[str] = mapped_column(
        ForeignKey("documents.id", ondelete="CASCADE"), nullable=False, index=True)
    trigger: Mapped[str] = mapped_column(String(32), default="upload")
    status: Mapped[str] = mapped_column(String(32), default="RUNNING", index=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    duration_ms: Mapped[float | None] = mapped_column(Float)
    error: Mapped[str | None] = mapped_column(Text)
    settings_snapshot: Mapped[dict[str, Any]] = mapped_column(JSONVariant, default=dict)
    engine_versions: Mapped[dict[str, Any]] = mapped_column(JSONVariant, default=dict)

    document: Mapped[Document] = relationship(back_populates="runs")
    stages: Mapped[list["StageRun"]] = relationship(
        back_populates="run", cascade="all, delete-orphan", lazy="selectin")


class StageRun(Base):
    __tablename__ = "stage_runs"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    run_id: Mapped[str] = mapped_column(
        ForeignKey("processing_runs.id", ondelete="CASCADE"), nullable=False, index=True)
    document_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    stage: Mapped[str] = mapped_column(String(48), nullable=False)
    status: Mapped[str] = mapped_column(String(24), default="PENDING")
    engine: Mapped[str | None] = mapped_column(String(64))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    duration_ms: Mapped[float | None] = mapped_column(Float)
    items_total: Mapped[int] = mapped_column(Integer, default=0)
    items_done: Mapped[int] = mapped_column(Integer, default=0)
    items_failed: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str | None] = mapped_column(Text)
    detail: Mapped[dict[str, Any]] = mapped_column(JSONVariant, default=dict)

    run: Mapped[ProcessingRun] = relationship(back_populates="stages")

    __table_args__ = (
        UniqueConstraint("run_id", "stage", name="uq_stage_run"),
    )


# ------------------------------------------------------------------- visual
class OCRElementRow(Base):
    __tablename__ = "ocr_elements"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    document_id: Mapped[str] = mapped_column(
        ForeignKey("documents.id", ondelete="CASCADE"), nullable=False, index=True)
    page_number: Mapped[int] = mapped_column(Integer, nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    confidence: Mapped[float] = mapped_column(Float, default=0.0)
    x: Mapped[float] = mapped_column(Float, nullable=False)
    y: Mapped[float] = mapped_column(Float, nullable=False)
    width: Mapped[float] = mapped_column(Float, nullable=False)
    height: Mapped[float] = mapped_column(Float, nullable=False)
    polygon: Mapped[list | None] = mapped_column(JSONVariant)
    block_id: Mapped[str | None] = mapped_column(String(64))
    line_id: Mapped[str | None] = mapped_column(String(64), index=True)
    word_id: Mapped[str | None] = mapped_column(String(64))
    source: Mapped[str] = mapped_column(String(16), default="ocr")
    reading_index: Mapped[int | None] = mapped_column(Integer)
    font_size_px: Mapped[float | None] = mapped_column(Float)
    is_bold: Mapped[bool | None] = mapped_column(Boolean)
    corroborated: Mapped[bool] = mapped_column(Boolean, default=False)
    aux_text: Mapped[str | None] = mapped_column(Text)
    currency_glyph: Mapped[str | None] = mapped_column(String(8))
    region_id: Mapped[str | None] = mapped_column(String(64), index=True)

    __table_args__ = (
        Index("ix_ocr_doc_page", "document_id", "page_number"),
    )


class RegionRow(Base):
    __tablename__ = "regions"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    document_id: Mapped[str] = mapped_column(
        ForeignKey("documents.id", ondelete="CASCADE"), nullable=False, index=True)
    page_number: Mapped[int] = mapped_column(Integer, nullable=False)
    type: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    x: Mapped[float] = mapped_column(Float, nullable=False)
    y: Mapped[float] = mapped_column(Float, nullable=False)
    width: Mapped[float] = mapped_column(Float, nullable=False)
    height: Mapped[float] = mapped_column(Float, nullable=False)
    confidence: Mapped[float] = mapped_column(Float, default=0.0)
    reading_order: Mapped[int] = mapped_column(Integer, default=0)
    column_index: Mapped[int] = mapped_column(Integer, default=0)
    text: Mapped[str] = mapped_column(Text, default="")
    parent_region_id: Mapped[str | None] = mapped_column(String(64))
    detector: Mapped[str] = mapped_column(String(48), default="heuristic")
    meta: Mapped[dict[str, Any]] = mapped_column(JSONVariant, default=dict)

    __table_args__ = (
        Index("ix_regions_doc_page", "document_id", "page_number"),
    )


class TableRow(Base):
    __tablename__ = "tables"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    document_id: Mapped[str] = mapped_column(
        ForeignKey("documents.id", ondelete="CASCADE"), nullable=False, index=True)
    page_number: Mapped[int] = mapped_column(Integer, nullable=False)
    region_id: Mapped[str | None] = mapped_column(String(64))
    title: Mapped[str] = mapped_column(Text, default="")
    caption: Mapped[str] = mapped_column(Text, default="")
    x: Mapped[float] = mapped_column(Float, default=0.0)
    y: Mapped[float] = mapped_column(Float, default=0.0)
    width: Mapped[float] = mapped_column(Float, default=0.0)
    height: Mapped[float] = mapped_column(Float, default=0.0)
    n_rows: Mapped[int] = mapped_column(Integer, default=0)
    n_cols: Mapped[int] = mapped_column(Integer, default=0)
    detector: Mapped[str] = mapped_column(String(48), default="")
    confidence: Mapped[float] = mapped_column(Float, default=0.0)
    has_ruling_lines: Mapped[bool] = mapped_column(Boolean, default=False)
    unit_hint: Mapped[str | None] = mapped_column(String(32))
    currency_hint: Mapped[str | None] = mapped_column(String(16))
    entity_hint: Mapped[str | None] = mapped_column(String(256))
    columns: Mapped[list] = mapped_column(JSONVariant, default=list)
    footnotes: Mapped[list] = mapped_column(JSONVariant, default=list)
    warnings: Mapped[list] = mapped_column(JSONVariant, default=list)
    continued_from_table_id: Mapped[str | None] = mapped_column(String(64))

    cells: Mapped[list["TableCellRow"]] = relationship(
        back_populates="table", cascade="all, delete-orphan", lazy="selectin")

    __table_args__ = (
        Index("ix_tables_doc_page", "document_id", "page_number"),
    )


class TableCellRow(Base):
    __tablename__ = "table_cells"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    table_id: Mapped[str] = mapped_column(
        ForeignKey("tables.id", ondelete="CASCADE"), nullable=False, index=True)
    document_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    page_number: Mapped[int] = mapped_column(Integer, nullable=False)
    row: Mapped[int] = mapped_column(Integer, nullable=False)
    col: Mapped[int] = mapped_column(Integer, nullable=False)
    row_span: Mapped[int] = mapped_column(Integer, default=1)
    col_span: Mapped[int] = mapped_column(Integer, default=1)
    text: Mapped[str] = mapped_column(Text, default="")
    role: Mapped[str] = mapped_column(String(24), default="data")
    x: Mapped[float] = mapped_column(Float, default=0.0)
    y: Mapped[float] = mapped_column(Float, default=0.0)
    width: Mapped[float] = mapped_column(Float, default=0.0)
    height: Mapped[float] = mapped_column(Float, default=0.0)
    confidence: Mapped[float] = mapped_column(Float, default=0.0)
    numeric_value: Mapped[float | None] = mapped_column(Float)
    raw_numeric_text: Mapped[str | None] = mapped_column(String(128))
    is_numeric: Mapped[bool] = mapped_column(Boolean, default=False)
    element_ids: Mapped[list] = mapped_column(JSONVariant, default=list)

    table: Mapped[TableRow] = relationship(back_populates="cells")

    __table_args__ = (
        UniqueConstraint("table_id", "row", "col", name="uq_cell_pos"),
        Index("ix_cells_table_pos", "table_id", "row", "col"),
    )


class ChartRow(Base):
    __tablename__ = "charts"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    document_id: Mapped[str] = mapped_column(
        ForeignKey("documents.id", ondelete="CASCADE"), nullable=False, index=True)
    page_number: Mapped[int] = mapped_column(Integer, nullable=False)
    region_id: Mapped[str | None] = mapped_column(String(64))
    chart_type: Mapped[str] = mapped_column(String(32), default="unknown")
    type_confidence: Mapped[float] = mapped_column(Float, default=0.0)
    title: Mapped[str] = mapped_column(Text, default="")
    x: Mapped[float] = mapped_column(Float, default=0.0)
    y: Mapped[float] = mapped_column(Float, default=0.0)
    width: Mapped[float] = mapped_column(Float, default=0.0)
    height: Mapped[float] = mapped_column(Float, default=0.0)
    axis_labels: Mapped[list] = mapped_column(JSONVariant, default=list)
    legend_labels: Mapped[list] = mapped_column(JSONVariant, default=list)
    categories: Mapped[list] = mapped_column(JSONVariant, default=list)
    data_labels: Mapped[list] = mapped_column(JSONVariant, default=list)
    unit_hint: Mapped[str | None] = mapped_column(String(32))
    confidence: Mapped[float] = mapped_column(Float, default=0.0)
    values_extracted: Mapped[bool] = mapped_column(Boolean, default=False)
    requires_review: Mapped[bool] = mapped_column(Boolean, default=True)
    notes: Mapped[str] = mapped_column(Text, default="")

    __table_args__ = (
        Index("ix_charts_doc_page", "document_id", "page_number"),
    )


# ------------------------------------------------------------------- facts
class FinancialFactRow(Base, TimestampMixin):
    __tablename__ = "financial_facts"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    document_id: Mapped[str] = mapped_column(
        ForeignKey("documents.id", ondelete="CASCADE"), nullable=False, index=True)

    metric: Mapped[str] = mapped_column(String(256), nullable=False)
    canonical_metric: Mapped[str] = mapped_column(String(96), nullable=False, index=True)
    metric_display: Mapped[str] = mapped_column(String(256), default="")
    entity: Mapped[str | None] = mapped_column(String(256), index=True)
    # Entity identity, so facts group and roll up without matching on names.
    entity_id: Mapped[str | None] = mapped_column(String(64), index=True)
    entity_type: Mapped[str] = mapped_column(String(24), default="unknown")
    parent_entity_id: Mapped[str | None] = mapped_column(String(64))
    # Where the metric sits in the taxonomy.
    statement: Mapped[str] = mapped_column(String(24), default="unknown", index=True)
    category: Mapped[str] = mapped_column(String(24), default="other", index=True)
    kind: Mapped[str] = mapped_column(String(24), default="table_cell", index=True)
    nature: Mapped[str] = mapped_column(String(16), default="actual", index=True)
    # Reported by the document, or computed here -- never conflated.
    derivation: Mapped[str] = mapped_column(String(16), default="reported", index=True)
    formula: Mapped[str | None] = mapped_column(String(256))
    input_fact_ids: Mapped[list] = mapped_column(JSONVariant, default=list)

    value: Mapped[float | None] = mapped_column(Float)
    raw_value: Mapped[str] = mapped_column(String(128), default="")
    value_type: Mapped[str] = mapped_column(String(24), default="unknown")
    currency: Mapped[str] = mapped_column(String(16), default="UNKNOWN")
    unit: Mapped[str] = mapped_column(String(24), default="none")
    base_value: Mapped[float | None] = mapped_column(Float)

    period: Mapped[str | None] = mapped_column(String(64), index=True)
    period_type: Mapped[str | None] = mapped_column(String(24))
    fiscal_year: Mapped[int | None] = mapped_column(Integer, index=True)
    quarter: Mapped[int | None] = mapped_column(Integer)
    period_raw: Mapped[str | None] = mapped_column(String(128))
    #: The exact dates the resolver established from the source, ISO-8601.
    #: These are persisted rather than recomputed because a fiscal label cannot
    #: reproduce them: a 52/53-week filer's "Q3 FY25" ends on 28 June 2025, and
    #: re-deriving it from the label yields 30 June -- a fabricated date two
    #: days out.  NULL means the source never established one.
    period_start: Mapped[str | None] = mapped_column(String(10))
    period_end: Mapped[str | None] = mapped_column(String(10), index=True)
    #: The resolver's own confidence in the period, distinct from the
    #: confidence in the figure's value.
    period_confidence: Mapped[float | None] = mapped_column(Float)
    #: stated | derived | inherited | unknown -- see :class:`PeriodBasis`.
    period_basis: Mapped[str | None] = mapped_column(String(16))
    comparison_period: Mapped[str | None] = mapped_column(String(64))
    comparison_value: Mapped[float | None] = mapped_column(Float)

    growth: Mapped[float | None] = mapped_column(Float)
    growth_type: Mapped[str | None] = mapped_column(String(16))
    growth_reported: Mapped[bool] = mapped_column(Boolean, default=False)
    # The independently recomputed change, kept alongside whatever the document
    # reported so the two are always comparable.
    growth_calculated: Mapped[float | None] = mapped_column(Float)
    change_absolute: Mapped[float | None] = mapped_column(Float)
    change_bps: Mapped[float | None] = mapped_column(Float)
    duplicate_resolution: Mapped[str | None] = mapped_column(String(32))
    unit_inferred: Mapped[bool] = mapped_column(Boolean, default=False)
    currency_inferred: Mapped[bool] = mapped_column(Boolean, default=False)

    confidence: Mapped[float] = mapped_column(Float, default=0.0, index=True)
    confidence_band: Mapped[str] = mapped_column(String(24), default="LOW", index=True)
    confidence_breakdown: Mapped[dict[str, Any]] = mapped_column(JSONVariant, default=dict)

    validation_status: Mapped[str] = mapped_column(String(24), default="NOT_CHECKED", index=True)
    requires_review: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    #: Which of the review reasons applies, so the flag can say what to look at
    #: rather than only that something is wrong.
    review_reason: Mapped[str | None] = mapped_column(String(32))
    review_state: Mapped[str] = mapped_column(String(16), default="pending", index=True)

    source_page: Mapped[int] = mapped_column(Integer, default=0, index=True)
    region_id: Mapped[str | None] = mapped_column(String(64))
    table_id: Mapped[str | None] = mapped_column(String(64), index=True)
    cell_id: Mapped[str | None] = mapped_column(String(64))
    chart_id: Mapped[str | None] = mapped_column(String(64))

    original_text: Mapped[str] = mapped_column(Text, default="")
    context_text: Mapped[str] = mapped_column(Text, default="")
    meta: Mapped[dict[str, Any]] = mapped_column(JSONVariant, default=dict)

    # values recorded by a human reviewer; the extracted value is never mutated
    corrected_value: Mapped[float | None] = mapped_column(Float)
    corrected_by: Mapped[str | None] = mapped_column(String(128))
    corrected_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        Index("ix_facts_doc_metric", "document_id", "canonical_metric"),
        Index("ix_facts_doc_period", "document_id", "period"),
        Index("ix_facts_review", "document_id", "requires_review", "review_state"),
        CheckConstraint("confidence >= 0.0 AND confidence <= 1.0",
                        name="ck_fact_confidence_range"),
    )


class ProvenanceRow(Base):
    __tablename__ = "provenance"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    document_id: Mapped[str] = mapped_column(
        ForeignKey("documents.id", ondelete="CASCADE"), nullable=False, index=True)
    fact_id: Mapped[str | None] = mapped_column(String(64), index=True)
    target_type: Mapped[str] = mapped_column(String(24), default="fact")
    target_id: Mapped[str] = mapped_column(String(64), index=True)
    page_number: Mapped[int] = mapped_column(Integer, nullable=False)
    region_id: Mapped[str | None] = mapped_column(String(64))
    table_id: Mapped[str | None] = mapped_column(String(64))
    cell_id: Mapped[str | None] = mapped_column(String(64))
    chart_id: Mapped[str | None] = mapped_column(String(64))
    x: Mapped[float | None] = mapped_column(Float)
    y: Mapped[float | None] = mapped_column(Float)
    width: Mapped[float | None] = mapped_column(Float)
    height: Mapped[float | None] = mapped_column(Float)
    original_text: Mapped[str] = mapped_column(Text, default="")
    ocr_element_ids: Mapped[list] = mapped_column(JSONVariant, default=list)
    extraction_method: Mapped[str] = mapped_column(String(96), default="")


class CommentaryRow(Base):
    __tablename__ = "commentary"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    document_id: Mapped[str] = mapped_column(
        ForeignKey("documents.id", ondelete="CASCADE"), nullable=False, index=True)
    page_number: Mapped[int] = mapped_column(Integer, nullable=False)
    region_id: Mapped[str | None] = mapped_column(String(64))
    text: Mapped[str] = mapped_column(Text, nullable=False)
    topics: Mapped[list] = mapped_column(JSONVariant, default=list)
    primary_topic: Mapped[str] = mapped_column(String(32), default="other", index=True)
    sentiment: Mapped[str] = mapped_column(String(16), default="neutral")
    speaker: Mapped[str | None] = mapped_column(String(256))
    is_quote: Mapped[bool] = mapped_column(Boolean, default=False)
    confidence: Mapped[float] = mapped_column(Float, default=0.0)
    x: Mapped[float | None] = mapped_column(Float)
    y: Mapped[float | None] = mapped_column(Float)
    width: Mapped[float | None] = mapped_column(Float)
    height: Mapped[float | None] = mapped_column(Float)
    summary: Mapped[str | None] = mapped_column(Text)
    summary_engine: Mapped[str | None] = mapped_column(String(64))
    mentioned_metrics: Mapped[list] = mapped_column(JSONVariant, default=list)

    __table_args__ = (
        Index("ix_commentary_doc_page", "document_id", "page_number"),
    )


class GuidanceRow(Base):
    __tablename__ = "guidance"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    document_id: Mapped[str] = mapped_column(
        ForeignKey("documents.id", ondelete="CASCADE"), nullable=False, index=True)
    page_number: Mapped[int] = mapped_column(Integer, nullable=False)
    region_id: Mapped[str | None] = mapped_column(String(64))
    text: Mapped[str] = mapped_column(Text, nullable=False)
    nature: Mapped[str] = mapped_column(String(16), default="guidance", index=True)
    canonical_metric: Mapped[str | None] = mapped_column(String(96), index=True)
    metric: Mapped[str | None] = mapped_column(String(256))
    target_value: Mapped[float | None] = mapped_column(Float)
    target_unit: Mapped[str | None] = mapped_column(String(24))
    target_currency: Mapped[str | None] = mapped_column(String(16))
    target_period: Mapped[str | None] = mapped_column(String(64))
    direction: Mapped[str] = mapped_column(String(16), default="unspecified")
    trigger_phrase: Mapped[str] = mapped_column(String(256), default="")
    confidence: Mapped[float] = mapped_column(Float, default=0.0)
    requires_review: Mapped[bool] = mapped_column(Boolean, default=False)
    x: Mapped[float | None] = mapped_column(Float)
    y: Mapped[float | None] = mapped_column(Float)
    width: Mapped[float | None] = mapped_column(Float)
    height: Mapped[float | None] = mapped_column(Float)


class ValidationResultRow(Base):
    __tablename__ = "validation_results"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    document_id: Mapped[str] = mapped_column(
        ForeignKey("documents.id", ondelete="CASCADE"), nullable=False, index=True)
    rule: Mapped[str] = mapped_column(String(96), nullable=False, index=True)
    rule_family: Mapped[str] = mapped_column(String(48), default="", index=True)
    status: Mapped[str] = mapped_column(String(24), nullable=False, index=True)
    severity: Mapped[str] = mapped_column(String(16), default="WARNING")
    message: Mapped[str] = mapped_column(Text, nullable=False)
    reported_value: Mapped[float | None] = mapped_column(Float)
    calculated_value: Mapped[float | None] = mapped_column(Float)
    difference: Mapped[float | None] = mapped_column(Float)
    tolerance: Mapped[float | None] = mapped_column(Float)
    fact_ids: Mapped[list] = mapped_column(JSONVariant, default=list)
    page_numbers: Mapped[list] = mapped_column(JSONVariant, default=list)
    detail: Mapped[dict[str, Any]] = mapped_column(JSONVariant, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ReviewActionRow(Base):
    __tablename__ = "review_actions"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    document_id: Mapped[str] = mapped_column(
        ForeignKey("documents.id", ondelete="CASCADE"), nullable=False, index=True)
    target_type: Mapped[str] = mapped_column(String(24), default="fact")
    target_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    action: Mapped[str] = mapped_column(String(16), nullable=False)
    reviewer: Mapped[str] = mapped_column(String(128), default="analyst")
    previous_value: Mapped[dict[str, Any] | None] = mapped_column(JSONVariant)
    new_value: Mapped[dict[str, Any] | None] = mapped_column(JSONVariant)
    note: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class JobRow(Base):
    __tablename__ = "jobs"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    document_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    job_type: Mapped[str] = mapped_column(String(32), default="process")
    state: Mapped[str] = mapped_column(String(16), default="queued", index=True)
    priority: Mapped[int] = mapped_column(Integer, default=0)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, default=2)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error: Mapped[str | None] = mapped_column(Text)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONVariant, default=dict)
