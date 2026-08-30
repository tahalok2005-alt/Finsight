import { AlertTriangle, Check, Circle, Loader2 } from "lucide-react";
import clsx from "clsx";
import type { ProcessingStatus } from "../../types";
import { ProgressBar } from "../../components/ui";
import { STAGE_LABEL, STATUS_LABEL, formatDuration } from "../../utils/format";

const ORDER = [
  "validating", "rendering", "ocr", "layout", "document_assembly",
  "tables", "charts", "extraction", "validating_facts",
];

/**
 * Real progress, not a timer.
 *
 * Each row reflects a stage record the backend actually wrote, including its
 * item counts, so the bar cannot advance past work that has not happened.
 */
export function ProcessingPanel({ status }: { status: ProcessingStatus }) {
  const byStage = new Map(status.stages.map((s) => [s.stage, s]));
  const failed = status.status === "FAILED";

  return (
    <div className="panel p-4">
      <div className="flex items-start justify-between gap-4 mb-3">
        <div>
          <p className="text-sm font-medium">
            {failed ? "Processing failed" : STATUS_LABEL[status.status] ?? status.status}
          </p>
          <p className="text-xs text-ink-400 mt-0.5">
            {status.pages_total > 0 && (
              <>
                {status.pages_done} of {status.pages_total} pages
                {status.pages_failed > 0 && (
                  <span className="text-fail-400"> · {status.pages_failed} failed</span>
                )}
              </>
            )}
          </p>
        </div>
        <span className="num text-lg font-semibold">
          {Math.round(status.progress * 100)}%
        </span>
      </div>

      <ProgressBar
        value={status.progress}
        tone={failed ? "fail" : status.progress >= 1 ? "pass" : "accent"}
      />

      {status.error && (
        <p className="mt-3 text-xs text-fail-400 bg-fail-900/30 border border-fail-500/30 rounded px-3 py-2">
          {status.error}
        </p>
      )}

      <ul className="mt-4 grid grid-cols-2 lg:grid-cols-3 gap-x-6 gap-y-1.5">
        {ORDER.map((name) => {
          const s = byStage.get(name);
          const state = s?.status ?? "PENDING";
          return (
            <li key={name} className="flex items-center gap-2 text-xs">
              {state === "SUCCEEDED" && <Check size={13} className="text-pass-400 shrink-0" />}
              {state === "PARTIAL" && (
                <AlertTriangle size={13} className="text-warn-400 shrink-0" />
              )}
              {state === "FAILED" && (
                <AlertTriangle size={13} className="text-fail-400 shrink-0" />
              )}
              {state === "RUNNING" && (
                <Loader2 size={13} className="text-accent-400 animate-spin shrink-0" />
              )}
              {state === "PENDING" && <Circle size={13} className="text-ink-600 shrink-0" />}
              <span
                className={clsx(
                  "flex-1 truncate",
                  state === "PENDING" ? "text-ink-500" : "text-ink-200",
                )}
              >
                {STAGE_LABEL[name] ?? name}
              </span>
              {s && s.items_total > 0 && (
                <span className="num text-2xs text-ink-500 shrink-0">
                  {s.items_done}/{s.items_total}
                </span>
              )}
              {s?.duration_ms != null && state !== "RUNNING" && (
                <span className="num text-2xs text-ink-600 shrink-0 w-12 text-right">
                  {formatDuration(s.duration_ms)}
                </span>
              )}
            </li>
          );
        })}
      </ul>

      {status.stages.some((s) => s.error) && (
        <ul className="mt-3 space-y-1">
          {status.stages
            .filter((s) => s.error)
            .map((s) => (
              <li key={s.stage} className="text-2xs text-fail-400">
                <span className="font-medium">{STAGE_LABEL[s.stage] ?? s.stage}:</span>{" "}
                {s.error}
              </li>
            ))}
        </ul>
      )}
    </div>
  );
}
