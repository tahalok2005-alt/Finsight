export interface BBox {
  x: number;
  y: number;
  width: number;
  height: number;
}

export type DocumentStatus =
  | "UPLOADED" | "QUEUED" | "VALIDATING" | "RENDERING" | "OCR_PROCESSING"
  | "LAYOUT_ANALYSIS" | "TABLE_ANALYSIS" | "EXTRACTION" | "VALIDATING_FACTS"
  | "ANALYSING"
  | "COMPLETED" | "PARTIALLY_COMPLETED" | "REQUIRES_REVIEW" | "FAILED";

export type ConfidenceBand = "HIGH" | "MEDIUM" | "LOW" | "REQUIRES_REVIEW";
export type ValidationStatus =
  | "PASSED" | "WARNING" | "FAILED" | "REQUIRES_REVIEW" | "NOT_CHECKED";

/** Which lifecycle actions the server will currently accept for a document.
 *  Computed server-side so the UI never re-derives the state machine. */
export interface DocumentControl {
  document_id: string;
  status: DocumentStatus;
  progress_note: string | null;
  can_pause: boolean;
  can_resume: boolean;
  can_cancel: boolean;
  can_requeue: boolean;
  can_open: boolean;
  can_delete: boolean;
}

export interface ControlAck {
  document_id: string;
  status: string;
  detail?: string;
}

export interface DocumentSummary {
  id: string;
  filename: string;
  original_filename: string;
  content_hash: string;
  size_bytes: number;
  page_count: number;
  status: DocumentStatus;
  title: string | null;
  company_name: string | null;
  report_period: string | null;
  report_type: string | null;
  document_currency: string | null;
  document_unit: string | null;
  is_scanned: boolean;
  created_at: string;
  updated_at: string;
  processing_started_at?: string | null;
  processing_finished_at?: string | null;
  processing_ms: number | null;
  error: string | null;
  pages_processed: number;
  pages_failed: number;
  /** where a stopped run got to, e.g. "Paused at page 41 of 165" */
  progress_note?: string | null;
  can_pause?: boolean;
  can_resume?: boolean;
  can_cancel?: boolean;
  can_requeue?: boolean;
  can_open?: boolean;
  can_delete?: boolean;
  fact_count?: number;
  table_count?: number;
  chart_count?: number;
  commentary_count?: number;
  guidance_count?: number;
  validation_warning_count?: number;
  review_queue_count?: number;
  sections?: Section[];
  job_id?: string;
}

export interface Section {
  id: string;
  title: string;
  entity: string | null;
  start_page: number;
  end_page: number;
}

export interface StageRecord {
  stage: string;
  status: "PENDING" | "RUNNING" | "SUCCEEDED" | "PARTIAL" | "FAILED"
    | "SKIPPED" | "CANCELLED";
  engine: string | null;
  items_total: number;
  items_done: number;
  items_failed: number;
  duration_ms: number | null;
  error: string | null;
  detail: Record<string, unknown>;
  finished_at: string | null;
}

export interface ProcessingStatus {
  document_id: string;
  status: DocumentStatus;
  progress: number;
  current_stage: string | null;
  stages: StageRecord[];
  pages_total: number;
  pages_done: number;
  pages_failed: number;
  error: string | null;
  updated_at: string | null;
  run_id: string | null;
  engine_versions: Record<string, unknown>;
  processing_mode: string | null;
  native_text_audit: {
    extraction_fed_by_native_text?: boolean;
    corroboration_accesses?: number;
    allowed_by_scope?: Record<string, number>;
  };
}

export interface Provenance {
  document_id: string;
  page_number: number;
  region_id: string | null;
  table_id: string | null;
  cell_id: string | null;
  chart_id: string | null;
  bbox: BBox | null;
  bbox_relative: BBox | null;
  original_text: string;
  ocr_element_ids: string[];
  extraction_method: string;
  page_image_url: string | null;
  crop_url: string | null;
}

export interface ConfidenceBreakdown {
  ocr: number;
  layout: number;
  structure: number;
  semantic: number;
  consistency: number;
  corroboration: number;
  overall: number;
  band: ConfidenceBand;
  notes: string[];
}

export type PeriodBasis =
  | "stated" | "derived" | "inherited" | "unknown"
  /** The column was a segment, a geography or a unit — never a period. */
  | "not_applicable";

/** Why a figure was put in front of a person. Mirrors the backend enum. */
export type ReviewReason =
  | "validation_failed" | "conflicting_sources" | "value_unparsed"
  | "unit_undetermined" | "implausible_value" | "ambiguous_period"
  | "low_confidence" | "value_disputed" | "metric_ambiguous"
  | "narrative_unconfirmed";

/**
 * What one processing run made of a document.
 *
 * `understanding` is the half a reader wants: not how long a stage took, but
 * how much of what was extracted is fully placed in time, attributed to a
 * reporting entity, and agreed with by the rest of the document.
 */
export interface DocumentUnderstanding {
  pages_processed: number;
  figures_extracted: number;
  tables_understood: number;
  figures_requiring_review: number;
  figures_without_a_period: number;
  figures_without_an_entity: number;
  figures_corroborated: number;
  contradictions: number;
  processing_ms: number | null;
  charts_detected: number;
  charts_analytically_readable: number;
}

export interface DocumentDiagnostics {
  document_id: string;
  understanding: DocumentUnderstanding;
  facts_by_strategy: Record<string, number>;
  regions_by_type: Record<string, number>;
  total_ms: number | null;
}

export interface FinancialFact {
  fact_id: string;
  document_id: string;
  metric: string;
  canonical_metric: string;
  metric_display: string;
  entity: string | null;
  kind: string;
  nature: string;
  value: number | null;
  corrected_value: number | null;
  effective_value: number | null;
  raw_value: string;
  value_type: string;
  currency: string;
  unit: string;
  /** Carried to the figure from its table or document, not printed with it. */
  currency_inferred?: boolean;
  unit_inferred?: boolean;
  base_value: number | null;
  period: string | null;
  period_type: string | null;
  /** Exact dates the source supports, ISO-8601. Null means the document never
   *  established one — never fill this in from the label. */
  period_start?: string | null;
  period_end?: string | null;
  /** The resolver's confidence in the period, distinct from the value's. */
  period_confidence?: number | null;
  /** How the period was arrived at. */
  period_basis?: PeriodBasis | null;
  fiscal_year: number | null;
  quarter: number | null;
  period_raw: string | null;
  comparison_period: string | null;
  comparison_value: number | null;
  growth: number | null;
  growth_type: string | null;
  growth_reported: boolean;
  confidence: number;
  confidence_band: ConfidenceBand;
  confidence_breakdown: ConfidenceBreakdown | null;
  validation_status: ValidationStatus;
  requires_review: boolean;
  review_reason: ReviewReason | null;
  /** A sentence the backend wrote saying what to look at. */
  review_explanation: string | null;
  review_state: string;
  source_page: number;
  original_text: string;
  context_text: string;
  meta: Record<string, unknown>;
  table_id: string | null;
  cell_id: string | null;
  region_id: string | null;
  provenance?: Provenance;
  /** standalone | consolidated | segment | subsidiary | unknown ... */
  entity_type?: string | null;
  /** How reconciliation classified this figure against others in its slot. */
  duplicate_resolution?: string | null;
  /** The basis the source states for this figure, where it states one. */
  qualification?: Qualification | null;
}

export interface Qualification {
  qualification_type: string;
  qualification_label: string;
  qualification_source: string;
  footnote_reference?: string | null;
  source_text?: string;
}

export interface ImageQuality {
  skew_degrees?: number;
  noise?: number;
  contrast?: number;
  illumination_range?: number;
  sharpness?: number;
  text_height_px?: number;
  /** Defects a transformation could repair. */
  defects?: string[];
  /** Measured and above threshold, but deliberately not acted on. */
  observations?: string[];
}

export interface PageInfo {
  id: string;
  document_id: string;
  page_number: number;
  pdf_width: number;
  pdf_height: number;
  render_width: number;
  render_height: number;
  dpi: number;
  image_url: string;
  thumbnail_url: string;
  has_text_layer: boolean;
  is_scanned: boolean;
  ocr_engine: string | null;
  ocr_element_count: number;
  ocr_mean_confidence: number | null;
  /** Measured capture defects of this page: skew, noise, contrast,
   *  illumination, sharpness, effective text height. */
  image_quality: ImageQuality;
  /** Which image the OCR text was read from. Always the rendered page, so
   *  every bounding box addresses pixels this UI can actually show. */
  ocr_image: string;
  region_count: number;
  table_count: number;
  chart_count: number;
  status: string;
  error: string | null;
}

export interface RegionDto {
  id: string;
  type: string;
  confidence: number;
  reading_order: number;
  column_index: number;
  text: string;
  detector: string;
  meta: Record<string, unknown>;
  bbox: BBox;
  bbox_relative: BBox;
}

export interface OCRElementDto {
  id: string;
  text: string;
  confidence: number;
  corroborated: boolean;
  aux_text: string | null;
  currency_glyph: string | null;
  region_id: string | null;
  bbox: BBox;
  bbox_relative: BBox;
}

export interface PageDetail extends PageInfo {
  regions?: RegionDto[];
  ocr_elements?: OCRElementDto[];
}

export interface TableColumnDto {
  index: number;
  header: string;
  header_path: string[];
  normalized_period: string | null;
  is_growth_column: boolean;
  growth_type: string | null;
  numeric_ratio: number;
}

export interface TableCellDto {
  id: string;
  row: number;
  col: number;
  text: string;
  role: string;
  is_numeric: boolean;
  numeric_value: number | null;
  confidence: number;
  bbox: BBox;
  bbox_relative: BBox;
}

export interface TableDto {
  id: string;
  page_number: number;
  region_id: string | null;
  title: string;
  caption: string;
  n_rows: number;
  n_cols: number;
  detector: string;
  confidence: number;
  has_ruling_lines: boolean;
  unit_hint: string | null;
  currency_hint: string | null;
  entity: string | null;
  columns: TableColumnDto[];
  warnings: string[];
  continued_from_table_id: string | null;
  bbox: BBox;
  bbox_relative: BBox;
  page_image_url: string;
  grid: string[][];
  cells: TableCellDto[];
}

export interface ChartDto {
  id: string;
  page_number: number;
  chart_type: string;
  type_confidence: number;
  title: string;
  axis_labels: string[];
  legend_labels: string[];
  categories: string[];
  data_labels: { label: string | null; value: number | null; confidence: number }[];
  unit_hint: string | null;
  confidence: number;
  values_extracted: boolean;
  requires_review: boolean;
  notes: string;
  bbox: BBox;
  bbox_relative: BBox;
  page_image_url: string;
  crop_url: string | null;
}

export interface CommentaryDto {
  id: string;
  page_number: number;
  region_id: string | null;
  text: string;
  topics: string[];
  primary_topic: string;
  sentiment: string;
  speaker: string | null;
  is_quote: boolean;
  confidence: number;
  summary: string | null;
  mentioned_metrics: string[];
  bbox: BBox | null;
  bbox_relative: BBox | null;
  page_image_url: string;
}

export interface GuidanceDto {
  id: string;
  page_number: number;
  text: string;
  nature: string;
  canonical_metric: string | null;
  metric: string | null;
  target_value: number | null;
  target_unit: string | null;
  target_currency: string | null;
  target_period: string | null;
  direction: string;
  trigger_phrase: string;
  confidence: number;
  requires_review: boolean;
  bbox: BBox | null;
  bbox_relative: BBox | null;
  page_image_url: string;
}

export interface ValidationDto {
  id: string;
  rule: string;
  rule_family: string;
  status: ValidationStatus;
  severity: string;
  message: string;
  reported_value: number | null;
  calculated_value: number | null;
  difference: number | null;
  tolerance: number | null;
  fact_ids: string[];
  page_numbers: number[];
  detail: Record<string, unknown>;
}

export interface ReviewItem {
  fact_id: string;
  metric: string;
  metric_display: string;
  canonical_metric: string;
  entity: string | null;
  value: number | null;
  corrected_value: number | null;
  raw_value: string;
  unit: string;
  currency: string;
  period: string | null;
  period_type?: string | null;
  period_start?: string | null;
  period_end?: string | null;
  period_confidence?: number | null;
  period_basis?: PeriodBasis | null;
  confidence: number;
  confidence_band: ConfidenceBand;
  confidence_breakdown: ConfidenceBreakdown | null;
  validation_status: ValidationStatus;
  review_state: string;
  source_page: number;
  original_text: string;
  kind: string;
  table_id: string | null;
  cell_id: string | null;
  region_id: string | null;
  /** Why this figure is in the queue, in the reviewer's terms. */
  review_reason?: string;
  anomalies?: {
    anomaly_id: string;
    type: string;
    severity: Severity;
    message: string;
    reason: string;
  }[];
}

export interface ComparisonSide {
  value: number | null;
  period: string | null;
  basis: string;
  confidence: number | null;
  fact_id: string | null;
  page: number | null;
}

export interface ComparisonRow {
  canonical_metric: string;
  metric: string;
  entity: string;
  left: ComparisonSide;
  right: ComparisonSide;
  unit: string | null;
  currency: string | null;
  value_type: string | null;
  change: number | null;
  change_pct: number | null;
  change_basis: "calculated" | "reported" | "not_comparable";
  note: string | null;
}

export interface Overview {
  documents: {
    total: number;
    by_status: Record<string, number>;
    processing: number;
    failed: number;
  };
  extraction: {
    facts: number; tables: number; charts: number;
    commentary: number; guidance: number;
    confidence_bands: Record<string, number>;
    top_metrics: { metric: string; count: number }[];
  };
  validation: {
    summary: Record<string, number>;
    warnings: number; failures: number; passed: number;
  };
  review_queue: number;
  recent_documents: DocumentSummary[];
  jobs: { queued: number; running: number; failed: number; active_workers: number };
}

export interface Paged<T> {
  items: T[];
  total: number;
  limit?: number;
  offset?: number;
  has_more?: boolean;
}

/* -------------------------------------------------- financial intelligence */

export type StatementType =
  | "income_statement" | "balance_sheet" | "cash_flow" | "equity" | "ratio"
  | "segment" | "operational" | "notes" | "unknown";

export type EntityType =
  | "consolidated" | "standalone" | "company" | "subsidiary" | "segment"
  | "business_unit" | "joint_venture" | "associate" | "unknown";

export type DerivationStatus = "reported" | "calculated" | "reviewed";
export type Severity = "info" | "low" | "medium" | "high";

export interface EntityDto {
  entity_id: string;
  entity_name: string;
  entity_type: EntityType;
  parent_entity_id: string | null;
  aliases: string[];
  confidence: number;
  source: string;
  first_page: number | null;
  evidence: string;
  fact_count: number;
}

export interface EntityNode extends EntityDto {
  children: EntityNode[];
}

export interface SummaryValue {
  fact_id: string;
  period: string | null;
  period_type: string | null;
  period_start: string | null;
  period_end: string | null;
  period_confidence: number | null;
  period_basis: PeriodBasis | null;
  value: number | null;
  effective_value: number | null;
  unit: string;
  currency: string;
  value_type: string;
  growth: number | null;
  growth_reported: boolean;
  growth_calculated: number | null;
  change_bps: number | null;
  confidence: number;
  validation_status: string;
  requires_review: boolean;
  derivation: DerivationStatus;
  source_page: number | null;
}

export interface SummaryMetric {
  canonical_metric: string;
  metric_display: string;
  entity_id: string | null;
  entity_name: string | null;
  entity_type: EntityType;
  values: SummaryValue[];
}

/** Whether a statement's figures can honestly be drawn as a comparative grid. */
export interface ComparativeVerdict {
  renderable: boolean;
  periods: string[];
  row_count: number;
  dated_values: number;
  total_values: number;
  fill: number;
  reason: string | null;
}

export interface StatementGroup {
  statement: StatementType;
  comparative: ComparativeVerdict;
  categories: { category: string; metrics: SummaryMetric[]; metric_count: number }[];
  fact_count: number;
}

export interface DerivedMetricDto {
  id: string;
  canonical_metric: string;
  metric_display: string;
  entity_id: string | null;
  entity_name: string | null;
  period: string | null;
  value: number | null;
  value_type: string;
  unit: string;
  currency: string;
  formula: string;
  inputs: Record<string, string>;
  input_values: Record<string, number>;
  reported_fact_id: string | null;
  reported_value: number | null;
  agrees_with_reported: boolean | null;
  difference: number | null;
  confidence: number;
  derivation: DerivationStatus;
  source_pages: number[];
  notes: string;
}

export interface InsightDto {
  id: string;
  insight_type: string;
  headline: string;
  detail: string;
  entity_id: string | null;
  entity_name: string | null;
  metric: string | null;
  periods: string[];
  supporting_fact_ids: string[];
  value: number | null;
  comparison_value: number | null;
  change: number | null;
  change_unit: string | null;
  derivation: DerivationStatus;
  confidence: number;
  source_pages: number[];
}

export interface AnomalyDto {
  id: string;
  anomaly_type: string;
  severity: Severity;
  message: string;
  reason: string;
  entity_id: string | null;
  entity_name: string | null;
  metric: string | null;
  period: string | null;
  observed_value: number | null;
  expected_value: number | null;
  deviation: number | null;
  threshold: number | null;
  fact_ids: string[];
  source_pages: number[];
  requires_review: boolean;
}

export interface RiskDto {
  id: string;
  page_number: number;
  region_id: string | null;
  text: string;
  category: string;
  categories: string[];
  entity_id: string | null;
  entity_name: string | null;
  period: string | null;
  trigger_phrase: string;
  severity: Severity;
  is_forward_looking: boolean;
  confidence: number;
  bbox: { x: number; y: number; width: number; height: number } | null;
}

export interface RelationDto {
  id: string;
  from_fact_id: string;
  to_fact_id: string;
  kind: string;
  detail: Record<string, any>;
  confidence: number;
}

export interface ConfidenceComponentDto {
  name: string;
  value: number;
  weight: number;
  reason: string;
}

export interface FactExplanationDto {
  fact_id: string;
  document_id: string;
  metric: string;
  metric_display: string;
  canonical_metric: string;
  metric_definition: string;
  value: number | null;
  formatted_value: string;
  entity_name: string | null;
  entity_type: EntityType;
  period: string | null;
  period_basis: string;
  unit: string;
  currency: string;
  source_page: number;
  why_this_metric: string;
  why_this_period: string;
  why_this_entity: string;
  why_this_unit: string;
  confidence: number;
  confidence_band: string;
  confidence_components: ConfidenceComponentDto[];
  validation_summary: Record<string, any>[];
  derivation: DerivationStatus;
  derivation_note: string;
  formula: string | null;
  inputs: Record<string, any>[];
  related_facts: Record<string, any>[];
  provenance: Record<string, any> | null;
  review_history: Record<string, any>[];
  requires_review: boolean;
  generated_by: string;
}

export interface TableSemanticsDto {
  id: string;
  table_id: string;
  page_number: number;
  table_type: string;
  statement: StatementType;
  title: string;
  entity_id: string | null;
  entity_name: string | null;
  periods: string[];
  current_period: string | null;
  comparison_period: string | null;
  sequential_period: string | null;
  unit: string | null;
  currency: string | null;
  columns: Record<string, any>[];
  row_metrics: Record<string, string>;
  confidence: number;
  notes: string[];
}
