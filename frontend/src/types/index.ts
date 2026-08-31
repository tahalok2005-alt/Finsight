export interface BBox {
  x: number;
  y: number;
  width: number;
  height: number;
}

export type DocumentStatus =
  | "UPLOADED" | "QUEUED" | "VALIDATING" | "RENDERING" | "OCR_PROCESSING"
  | "LAYOUT_ANALYSIS" | "TABLE_ANALYSIS" | "EXTRACTION" | "VALIDATING_FACTS"
  | "COMPLETED" | "PARTIALLY_COMPLETED" | "REQUIRES_REVIEW" | "FAILED";

export type ConfidenceBand = "HIGH" | "MEDIUM" | "LOW" | "REQUIRES_REVIEW";
export type ValidationStatus =
  | "PASSED" | "WARNING" | "FAILED" | "REQUIRES_REVIEW" | "NOT_CHECKED";

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
  processing_ms: number | null;
  error: string | null;
  pages_processed: number;
  pages_failed: number;
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
  status: "PENDING" | "RUNNING" | "SUCCEEDED" | "PARTIAL" | "FAILED" | "SKIPPED";
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
  base_value: number | null;
  period: string | null;
  period_type: string | null;
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
  review_state: string;
  source_page: number;
  original_text: string;
  context_text: string;
  meta: Record<string, unknown>;
  table_id: string | null;
  cell_id: string | null;
  region_id: string | null;
  provenance?: Provenance;
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
