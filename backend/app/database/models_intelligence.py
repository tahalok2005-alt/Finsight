"""ORM tables for the financial intelligence layer.

Kept in a separate module from the core document tables so the extraction
schema and the analysis schema can be read independently, but sharing one
``Base`` so they live in the same metadata and migrate together.

Every table carries ``document_id`` with a cascade, matching the convention in
``models.py``: per-document queries need no multi-level joins and deleting a
document cleans up everything derived from it.
"""
from __future__ import annotations

from typing import Any

from sqlalchemy import (
    Boolean, Float, ForeignKey, Index, Integer, String, Text, UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.database.models import Base, JSONVariant


class EntityRow(Base):
    """A reporting unit named by a document (consolidated, subsidiary, segment)."""
    __tablename__ = "entities"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    document_id: Mapped[str] = mapped_column(
        ForeignKey("documents.id", ondelete="CASCADE"), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(256), nullable=False)
    entity_type: Mapped[str] = mapped_column(String(24), default="unknown", index=True)
    parent_entity_id: Mapped[str | None] = mapped_column(String(64), index=True)
    aliases: Mapped[list] = mapped_column(JSONVariant, default=list)
    confidence: Mapped[float] = mapped_column(Float, default=0.0)
    source: Mapped[str] = mapped_column(String(32), default="heading")
    first_page: Mapped[int | None] = mapped_column(Integer)
    evidence: Mapped[str] = mapped_column(Text, default="")

    __table_args__ = (
        Index("ix_entities_doc_type", "document_id", "entity_type"),
        UniqueConstraint("document_id", "name", name="uq_entity_doc_name"),
    )


class TableSemanticsRow(Base):
    """Semantic interpretation layered over a reconstructed table."""
    __tablename__ = "table_semantics"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    document_id: Mapped[str] = mapped_column(
        ForeignKey("documents.id", ondelete="CASCADE"), nullable=False, index=True)
    table_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    page_number: Mapped[int] = mapped_column(Integer, nullable=False)
    table_type: Mapped[str] = mapped_column(String(32), default="other", index=True)
    statement: Mapped[str] = mapped_column(String(24), default="unknown", index=True)
    title: Mapped[str] = mapped_column(Text, default="")
    entity_id: Mapped[str | None] = mapped_column(String(64), index=True)
    entity_name: Mapped[str | None] = mapped_column(String(256))
    periods: Mapped[list] = mapped_column(JSONVariant, default=list)
    current_period: Mapped[str | None] = mapped_column(String(64))
    comparison_period: Mapped[str | None] = mapped_column(String(64))
    sequential_period: Mapped[str | None] = mapped_column(String(64))
    unit: Mapped[str | None] = mapped_column(String(24))
    currency: Mapped[str | None] = mapped_column(String(16))
    columns: Mapped[list] = mapped_column(JSONVariant, default=list)
    row_metrics: Mapped[dict[str, Any]] = mapped_column(JSONVariant, default=dict)
    confidence: Mapped[float] = mapped_column(Float, default=0.0)
    notes: Mapped[list] = mapped_column(JSONVariant, default=list)


class DerivedMetricRow(Base):
    """A computed figure, with the formula and inputs that produced it.

    Stored separately from ``financial_facts`` so a calculated number can never
    be mistaken for one the document reported.
    """
    __tablename__ = "derived_metrics"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    document_id: Mapped[str] = mapped_column(
        ForeignKey("documents.id", ondelete="CASCADE"), nullable=False, index=True)
    canonical_metric: Mapped[str] = mapped_column(String(96), nullable=False, index=True)
    metric_display: Mapped[str] = mapped_column(String(256), default="")
    entity_id: Mapped[str | None] = mapped_column(String(64), index=True)
    entity_name: Mapped[str | None] = mapped_column(String(256))
    period: Mapped[str | None] = mapped_column(String(64), index=True)
    value: Mapped[float | None] = mapped_column(Float)
    value_type: Mapped[str] = mapped_column(String(24), default="unknown")
    unit: Mapped[str] = mapped_column(String(24), default="none")
    currency: Mapped[str] = mapped_column(String(16), default="UNKNOWN")
    formula: Mapped[str] = mapped_column(String(256), default="")
    inputs: Mapped[dict[str, Any]] = mapped_column(JSONVariant, default=dict)
    input_values: Mapped[dict[str, Any]] = mapped_column(JSONVariant, default=dict)
    reported_fact_id: Mapped[str | None] = mapped_column(String(64))
    reported_value: Mapped[float | None] = mapped_column(Float)
    agrees_with_reported: Mapped[bool | None] = mapped_column(Boolean)
    difference: Mapped[float | None] = mapped_column(Float)
    confidence: Mapped[float] = mapped_column(Float, default=0.0)
    derivation: Mapped[str] = mapped_column(String(16), default="calculated")
    source_pages: Mapped[list] = mapped_column(JSONVariant, default=list)
    notes: Mapped[str] = mapped_column(Text, default="")

    __table_args__ = (
        Index("ix_derived_doc_metric_period", "document_id", "canonical_metric",
              "period"),
    )


class FactRelationRow(Base):
    """A typed link between two facts -- the relationship layer."""
    __tablename__ = "fact_relations"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    document_id: Mapped[str] = mapped_column(
        ForeignKey("documents.id", ondelete="CASCADE"), nullable=False, index=True)
    from_fact_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    to_fact_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    kind: Mapped[str] = mapped_column(String(24), nullable=False, index=True)
    detail: Mapped[dict[str, Any]] = mapped_column(JSONVariant, default=dict)
    confidence: Mapped[float] = mapped_column(Float, default=1.0)

    __table_args__ = (
        UniqueConstraint("from_fact_id", "to_fact_id", "kind",
                         name="uq_relation_triple"),
        Index("ix_relations_doc_kind", "document_id", "kind"),
    )


class RiskRow(Base):
    """A risk or cautionary statement extracted from the narrative."""
    __tablename__ = "risks"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    document_id: Mapped[str] = mapped_column(
        ForeignKey("documents.id", ondelete="CASCADE"), nullable=False, index=True)
    page_number: Mapped[int] = mapped_column(Integer, nullable=False)
    region_id: Mapped[str | None] = mapped_column(String(64))
    text: Mapped[str] = mapped_column(Text, nullable=False)
    category: Mapped[str] = mapped_column(String(32), default="other", index=True)
    categories: Mapped[list] = mapped_column(JSONVariant, default=list)
    entity_id: Mapped[str | None] = mapped_column(String(64))
    entity_name: Mapped[str | None] = mapped_column(String(256))
    period: Mapped[str | None] = mapped_column(String(64))
    trigger_phrase: Mapped[str] = mapped_column(String(256), default="")
    severity: Mapped[str] = mapped_column(String(16), default="info")
    is_forward_looking: Mapped[bool] = mapped_column(Boolean, default=False)
    confidence: Mapped[float] = mapped_column(Float, default=0.0)
    x: Mapped[float | None] = mapped_column(Float)
    y: Mapped[float | None] = mapped_column(Float)
    width: Mapped[float | None] = mapped_column(Float)
    height: Mapped[float | None] = mapped_column(Float)


class InsightRow(Base):
    """A deterministic, source-backed observation."""
    __tablename__ = "insights"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    document_id: Mapped[str] = mapped_column(
        ForeignKey("documents.id", ondelete="CASCADE"), nullable=False, index=True)
    insight_type: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    headline: Mapped[str] = mapped_column(Text, nullable=False)
    detail: Mapped[str] = mapped_column(Text, default="")
    entity_id: Mapped[str | None] = mapped_column(String(64), index=True)
    entity_name: Mapped[str | None] = mapped_column(String(256))
    metric: Mapped[str | None] = mapped_column(String(96), index=True)
    periods: Mapped[list] = mapped_column(JSONVariant, default=list)
    supporting_fact_ids: Mapped[list] = mapped_column(JSONVariant, default=list)
    value: Mapped[float | None] = mapped_column(Float)
    comparison_value: Mapped[float | None] = mapped_column(Float)
    change: Mapped[float | None] = mapped_column(Float)
    change_unit: Mapped[str | None] = mapped_column(String(24))
    derivation: Mapped[str] = mapped_column(String(16), default="calculated")
    confidence: Mapped[float] = mapped_column(Float, default=0.0)
    source_pages: Mapped[list] = mapped_column(JSONVariant, default=list)


class AnomalyRow(Base):
    """An explainable flag on something unusual in the extracted data."""
    __tablename__ = "anomalies"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    document_id: Mapped[str] = mapped_column(
        ForeignKey("documents.id", ondelete="CASCADE"), nullable=False, index=True)
    anomaly_type: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    severity: Mapped[str] = mapped_column(String(16), default="medium", index=True)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    reason: Mapped[str] = mapped_column(Text, default="")
    entity_id: Mapped[str | None] = mapped_column(String(64))
    entity_name: Mapped[str | None] = mapped_column(String(256))
    metric: Mapped[str | None] = mapped_column(String(96))
    period: Mapped[str | None] = mapped_column(String(64))
    observed_value: Mapped[float | None] = mapped_column(Float)
    expected_value: Mapped[float | None] = mapped_column(Float)
    deviation: Mapped[float | None] = mapped_column(Float)
    threshold: Mapped[float | None] = mapped_column(Float)
    fact_ids: Mapped[list] = mapped_column(JSONVariant, default=list)
    source_pages: Mapped[list] = mapped_column(JSONVariant, default=list)
    requires_review: Mapped[bool] = mapped_column(Boolean, default=True)
