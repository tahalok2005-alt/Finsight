import type { ConfidenceBand, DocumentStatus, ValidationStatus } from "../types";

/** Format a financial value, keeping the document's own scale. */
export function formatValue(
  value: number | null | undefined,
  opts: {
    unit?: string | null;
    valueType?: string | null;
    currency?: string | null;
  } = {},
): string {
  if (value === null || value === undefined || Number.isNaN(value)) return "—";
  const { unit, valueType, currency } = opts;
  if (valueType === "percentage" || unit === "percent") {
    return `${value.toFixed(value % 1 === 0 ? 1 : 2)}%`;
  }
  if (unit === "bps") return `${value.toFixed(0)} bps`;
  if (valueType === "ratio") return value.toFixed(2);
  const abs = Math.abs(value);
  const digits = abs >= 1000 ? 0 : abs >= 10 ? 1 : 2;
  // Indian digit grouping (1,44,271) is correct for rupee amounts and wrong for
  // everything else, so the grouping follows the figure's own currency.
  const locale = currency === "INR" ? "en-IN" : "en-US";
  return value.toLocaleString(locale, {
    minimumFractionDigits: digits,
    maximumFractionDigits: digits,
  });
}

const CURRENCY_SYMBOL: Record<string, string> = {
  INR: "₹", USD: "$", EUR: "€", GBP: "£", JPY: "¥", AED: "AED ",
};

export function currencySymbol(code?: string | null): string {
  if (!code || code === "UNKNOWN") return "";
  return CURRENCY_SYMBOL[code] ?? `${code} `;
}

/** Unit label as the document expressed it -- never silently converted. */
export function unitLabel(unit?: string | null): string {
  if (!unit || unit === "none" || unit === "unit") return "";
  if (unit === "percent" || unit === "bps") return "";
  return unit;
}

export function formatFull(
  value: number | null,
  currency?: string | null,
  unit?: string | null,
  valueType?: string | null,
): string {
  const num = formatValue(value, { unit, valueType, currency });
  if (num === "—") return num;
  if (valueType === "percentage" || unit === "percent" || unit === "bps") return num;
  const sym = currencySymbol(currency);
  const u = unitLabel(unit);
  return `${sym}${num}${u ? ` ${u}` : ""}`.trim();
}

export function formatGrowth(g: number | null | undefined): string {
  if (g === null || g === undefined) return "—";
  const sign = g > 0 ? "+" : "";
  return `${sign}${g.toFixed(1)}%`;
}

export function formatBytes(n: number): string {
  if (n < 1024) return `${n} B`;
  if (n < 1024 ** 2) return `${(n / 1024).toFixed(0)} KB`;
  return `${(n / 1024 ** 2).toFixed(1)} MB`;
}

export function formatDuration(ms: number | null | undefined): string {
  if (!ms) return "—";
  if (ms < 1000) return `${Math.round(ms)} ms`;
  if (ms < 60_000) return `${(ms / 1000).toFixed(1)} s`;
  const m = Math.floor(ms / 60_000);
  const s = Math.round((ms % 60_000) / 1000);
  return `${m}m ${s}s`;
}

export function formatDate(iso: string | null | undefined): string {
  if (!iso) return "—";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "—";
  return d.toLocaleString(undefined, {
    year: "numeric", month: "short", day: "numeric",
    hour: "2-digit", minute: "2-digit",
  });
}

export function relativeTime(iso: string | null | undefined): string {
  if (!iso) return "—";
  const then = new Date(iso).getTime();
  if (Number.isNaN(then)) return "—";
  const secs = Math.round((Date.now() - then) / 1000);
  if (secs < 60) return "just now";
  if (secs < 3600) return `${Math.floor(secs / 60)}m ago`;
  if (secs < 86400) return `${Math.floor(secs / 3600)}h ago`;
  return `${Math.floor(secs / 86400)}d ago`;
}

export const BAND_STYLE: Record<ConfidenceBand, string> = {
  HIGH: "bg-pass-900 text-pass-400 border border-pass-500/30",
  MEDIUM: "bg-accent-600/10 text-accent-300 border border-accent-500/30",
  LOW: "bg-warn-900 text-warn-400 border border-warn-500/30",
  REQUIRES_REVIEW: "bg-fail-900 text-fail-400 border border-fail-500/30",
};

export const VALIDATION_STYLE: Record<ValidationStatus, string> = {
  PASSED: "bg-pass-900 text-pass-400 border border-pass-500/30",
  WARNING: "bg-warn-900 text-warn-400 border border-warn-500/30",
  FAILED: "bg-fail-900 text-fail-400 border border-fail-500/30",
  REQUIRES_REVIEW: "bg-warn-900 text-warn-400 border border-warn-500/30",
  NOT_CHECKED: "bg-ink-800 text-ink-400 border border-ink-700",
};

export const STATUS_STYLE: Record<string, string> = {
  COMPLETED: "bg-pass-900 text-pass-400 border border-pass-500/30",
  REQUIRES_REVIEW: "bg-warn-900 text-warn-400 border border-warn-500/30",
  PARTIALLY_COMPLETED: "bg-warn-900 text-warn-400 border border-warn-500/30",
  FAILED: "bg-fail-900 text-fail-400 border border-fail-500/30",
};

export function statusStyle(status: DocumentStatus | string): string {
  return STATUS_STYLE[status] ?? "bg-accent-600/10 text-accent-300 border border-accent-500/30";
}

export const STATUS_LABEL: Record<string, string> = {
  UPLOADED: "Uploaded",
  QUEUED: "Queued",
  VALIDATING: "Validating",
  RENDERING: "Rendering pages",
  OCR_PROCESSING: "Running OCR",
  LAYOUT_ANALYSIS: "Analysing layout",
  TABLE_ANALYSIS: "Reconstructing tables",
  EXTRACTION: "Extracting facts",
  VALIDATING_FACTS: "Validating figures",
  COMPLETED: "Completed",
  PARTIALLY_COMPLETED: "Partially completed",
  REQUIRES_REVIEW: "Requires review",
  FAILED: "Failed",
};

export const TERMINAL_STATUSES = new Set([
  "COMPLETED", "PARTIALLY_COMPLETED", "REQUIRES_REVIEW", "FAILED",
]);

export const STAGE_LABEL: Record<string, string> = {
  validating: "Validation",
  rendering: "Page rendering",
  ocr: "OCR",
  layout: "Layout analysis",
  document_assembly: "Document assembly",
  tables: "Table reconstruction",
  charts: "Chart detection",
  extraction: "Financial extraction",
  validating_facts: "Fact validation",
};

export const REGION_COLOR: Record<string, string> = {
  table: "#3b82f6",
  chart: "#a855f7",
  figure: "#a855f7",
  kpi_block: "#10b981",
  section_heading: "#f59e0b",
  title: "#f59e0b",
  paragraph: "#64748b",
  list: "#64748b",
  footnote: "#475569",
  caption: "#94a3b8",
  header: "#334155",
  footer: "#334155",
  page_number: "#334155",
  misc: "#475569",
};

export function titleCase(s: string): string {
  return s
    .replace(/_/g, " ")
    .replace(/\b\w/g, (c) => c.toUpperCase());
}
