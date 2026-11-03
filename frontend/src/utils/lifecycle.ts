/**
 * One vocabulary for document state, shared by every screen.
 *
 * The backend has fourteen statuses because the pipeline has fourteen things it
 * can be doing. A person reading a queue does not want fourteen words: they
 * want to know whether it is working, waiting for them, finished, or stopped.
 * So each backend status maps to a short label, a phase, and a tone -- and
 * every screen renders from this table rather than inventing its own wording.
 * That is what stops the same state being called "Processing" in one place and
 * "OCR_PROCESSING" in another.
 */
export type Phase = "pending" | "active" | "paused" | "done" | "stopped" | "failed";

export interface StatusMeta {
  /** what the user sees */
  label: string;
  /** coarse grouping used for filters, sorting and iconography */
  phase: Phase;
  /** chip class from index.css */
  chip: string;
  /** one line explaining the state where it needs explaining */
  hint?: string;
}

const ACTIVE_STAGE = (label: string): StatusMeta => ({
  label, phase: "active", chip: "chip-run",
});

export const STATUS: Record<string, StatusMeta> = {
  UPLOADED: { label: "Uploaded", phase: "pending", chip: "chip-neutral",
    hint: "Stored and waiting to be queued." },
  QUEUED: { label: "Queued", phase: "pending", chip: "chip-neutral",
    hint: "Waiting for a free worker." },
  VALIDATING: ACTIVE_STAGE("Validating"),
  RENDERING: ACTIVE_STAGE("Rendering pages"),
  OCR_PROCESSING: ACTIVE_STAGE("Reading text"),
  LAYOUT_ANALYSIS: ACTIVE_STAGE("Analysing layout"),
  TABLE_ANALYSIS: ACTIVE_STAGE("Reconstructing tables"),
  EXTRACTION: ACTIVE_STAGE("Extracting figures"),
  VALIDATING_FACTS: ACTIVE_STAGE("Checking figures"),
  ANALYSING: ACTIVE_STAGE("Analysing"),
  PAUSED: { label: "Paused", phase: "paused", chip: "chip-hold",
    hint: "Stopped at a page boundary. Resuming continues from there." },
  COMPLETED: { label: "Completed", phase: "done", chip: "chip-pass" },
  PARTIALLY_COMPLETED: { label: "Partial", phase: "done", chip: "chip-warn",
    hint: "Some pages could not be processed." },
  REQUIRES_REVIEW: { label: "Needs review", phase: "done", chip: "chip-warn",
    hint: "Finished, with figures flagged for a person to confirm." },
  CANCELLED: { label: "Cancelled", phase: "stopped", chip: "chip-neutral",
    hint: "Stopped by you. No results were kept." },
  INTERRUPTED: { label: "Interrupted", phase: "stopped", chip: "chip-warn",
    hint: "The server stopped mid-run. Re-queue to continue." },
  FAILED: { label: "Failed", phase: "failed", chip: "chip-fail" },
};

export function statusMeta(status?: string | null): StatusMeta {
  if (!status) return { label: "Unknown", phase: "pending", chip: "chip-neutral" };
  return STATUS[status] ?? { label: status, phase: "pending", chip: "chip-neutral" };
}

export const isActivePhase = (s?: string | null) => {
  const p = statusMeta(s).phase;
  return p === "active" || p === "paused" || p === "pending";
};

export const hasResults = (s?: string | null) =>
  statusMeta(s).phase === "done";

/** Ordered stage list, matching what the pipeline actually reports. */
export const STAGES: { key: string; label: string }[] = [
  { key: "validating", label: "Validating document" },
  { key: "rendering", label: "Rendering pages" },
  { key: "ocr", label: "Reading text (OCR)" },
  { key: "layout", label: "Analysing layout" },
  { key: "document_assembly", label: "Assembling document" },
  { key: "tables", label: "Reconstructing tables" },
  { key: "charts", label: "Detecting charts" },
  { key: "extraction", label: "Extracting figures" },
  { key: "validating_facts", label: "Checking figures" },
  { key: "analysis", label: "Financial analysis" },
];

export const stageLabel = (key: string) =>
  STAGES.find((s) => s.key === key)?.label ?? key.replace(/_/g, " ");

/** Human duration, e.g. "4m 12s". Sub-minute stays in seconds. */
export function duration(ms?: number | null): string {
  if (ms === null || ms === undefined || Number.isNaN(ms)) return "—";
  const s = Math.max(0, Math.round(ms / 1000));
  if (s < 60) return `${s}s`;
  const m = Math.floor(s / 60);
  if (m < 60) return `${m}m ${String(s % 60).padStart(2, "0")}s`;
  return `${Math.floor(m / 60)}h ${String(m % 60).padStart(2, "0")}m`;
}

/** "3 minutes ago" — relative time for a queue that updates while watched. */
export function since(iso?: string | null): string {
  if (!iso) return "—";
  const t = new Date(iso).getTime();
  if (Number.isNaN(t)) return "—";
  const s = Math.max(0, Math.round((Date.now() - t) / 1000));
  if (s < 45) return "just now";
  if (s < 3600) return `${Math.round(s / 60)}m ago`;
  if (s < 86400) return `${Math.round(s / 3600)}h ago`;
  return new Date(iso).toLocaleDateString(undefined,
    { day: "numeric", month: "short" });
}

export function dateTime(iso?: string | null): string {
  if (!iso) return "—";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "—";
  return d.toLocaleString(undefined, {
    day: "numeric", month: "short", hour: "2-digit", minute: "2-digit",
  });
}
