import { useEffect, useRef, useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { Check, Circle, Loader2, Pause, Play, X } from "lucide-react";
import clsx from "clsx";
import { api } from "../../api/client";
import type { DocumentSummary, ProcessingStatus, StageRecord } from "../../types";
import { STAGES, duration, stageLabel, statusMeta } from "../../utils/lifecycle";

/**
 * What is happening to this document, right now.
 *
 * Every number here comes from the pipeline's own stage records, which are
 * written to the database as each stage reports. Nothing is simulated: if the
 * backend cannot say how far along a stage is, this shows the stage as running
 * without inventing a percentage. That is the difference between a progress
 * view and a spinner with ambitions.
 *
 * The estimate is the one derived figure, and it is derived from measured
 * throughput on *this* run -- pages completed against elapsed time -- so it
 * corrects itself as the document proceeds and disappears when there is not
 * enough evidence to make it.
 *
 * Two things here were measured in Phase 18 and are deliberate:
 *
 * The page phases -- rendering, OCR, layout -- run interleaved inside one
 * parallel loop, so the backend records the same wall clock against all three.
 * Printing that duration three times reads as though the run took three times
 * as long as it did, so the loop is shown once, and the split inside it comes
 * from `phase_seconds`, which is thread time and is labelled as a share rather
 * than as a duration.
 *
 * The estimate covers pages only, because pages are the only part of the run
 * with a per-item rate to measure. Pages are most of the wall clock but not all
 * of it -- 62% to 93% across the benchmark corpus, rising with page count -- so
 * when the last page lands there is still real work to do. The estimate is
 * withdrawn at that point instead of counting down to "~0s" through the last
 * tenth of the run.
 */

type Props = { doc: DocumentSummary; status: ProcessingStatus | undefined };

function useElapsed(startedAt?: string | null, frozen = false) {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    if (frozen) return;
    const t = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(t);
  }, [frozen]);
  if (!startedAt) return null;
  const start = new Date(startedAt).getTime();
  if (Number.isNaN(start)) return null;
  return Math.max(0, now - start);
}

/** Pages per second measured over this run, for an honest estimate.
 *
 * Samples are kept in state rather than a ref so the value is computed during
 * an effect and read during render, which is the order React expects; reading
 * a ref mid-render works but is the kind of thing that breaks quietly later.
 */
function useThroughput(done: number, total: number) {
  const [rate, setRate] = useState<{ perPage: number; remainingMs: number } | null>(null);
  const marks = useRef<{ t: number; done: number }[]>([]);

  useEffect(() => {
    const pts = marks.current;
    const last = pts[pts.length - 1];
    if (last && last.done === done) return;
    pts.push({ t: Date.now(), done });
    if (pts.length > 24) pts.shift();

    if (pts.length < 3 || done <= 0 || total <= 0 || done >= total) {
      // done >= total: the pages are read but the run is not over, and the
      // remaining stages have no measured rate. Saying "~0s" here would be a
      // guess dressed as a measurement.
      setRate(null);
      return;
    }
    const first = pts[0];
    const newest = pts[pts.length - 1];
    const dPages = newest.done - first.done;
    const dMs = newest.t - first.t;
    if (dPages < 2 || dMs < 8000) {
      // too early to say anything truthful about the rate
      setRate(null);
      return;
    }
    const perPage = dMs / dPages;
    setRate({ perPage, remainingMs: Math.max(0, total - done) * perPage });
  }, [done, total]);

  return rate;
}

/** The three phases that share one parallel loop, in the order they run. */
const PAGE_PHASES = ["rendering", "ocr", "layout"] as const;

const phaseSeconds = (r?: StageRecord): number | null => {
  const v = r?.detail?.["phase_seconds"];
  return typeof v === "number" && Number.isFinite(v) ? v : null;
};

/**
 * Rendering, OCR and layout, shown as the one loop they actually are.
 *
 * The shared wall clock is printed once on the group. Inside it, each phase
 * gets the share of thread time the backend measured -- OCR is 93-97% of it on
 * every real document in the corpus -- because the phases overlap and there is
 * no honest per-phase wall clock to print.
 */
function PagePhaseGroup({ byStage }: { byStage: Map<string, StageRecord> }) {
  const records = PAGE_PHASES.map((k) => byStage.get(k));
  const present = records.filter(Boolean) as StageRecord[];
  const window = present.reduce((m, r) => Math.max(m, r.duration_ms ?? 0), 0);
  const running = present.some((r) => r.status === "RUNNING");
  const finished = present.length > 0 && present.every(
    (r) => r.status === "SUCCEEDED" || r.status === "PARTIAL");
  const shares = records.map(phaseSeconds);
  const totalThread = shares.reduce<number>((a, v) => a + (v ?? 0), 0);

  return (
    <>
      <div className={clsx(
        "flex items-center gap-2.5 h-7 px-3 text-xs border-b border-ink-800/60",
        present.length === 0 && "opacity-45")}>
        <span className="w-3.5 flex justify-center shrink-0">
          {finished ? <Check size={12} className="text-pass-400" />
            : running ? <Loader2 size={12} className="text-run-400 animate-spin" />
              : <Circle size={7} className="text-ink-600" />}
        </span>
        <span className={clsx("flex-1 truncate",
          running ? "text-ink-50 font-medium" : "text-ink-200")}>
          Reading pages
        </span>
        <span className="num text-2xs text-ink-300 tabular-nums">
          {present[0]?.items_total
            ? `${present[0].items_done}/${present[0].items_total}` : ""}
        </span>
        {window && !running ? (
          <span className="num text-2xs text-ink-400 w-14 text-right">
            {duration(window)}
          </span>
        ) : <span className="w-14" />}
      </div>

      {PAGE_PHASES.map((key, i) => {
        const rec = records[i];
        const share = shares[i];
        return (
          <div key={key} className={clsx(
            "flex items-center gap-2.5 h-6 pl-9 pr-3 text-2xs " +
            "border-b border-ink-800/40",
            !rec && "opacity-45")}>
            <span className="flex-1 truncate text-ink-300">
              {stageLabel(key)}
            </span>
            {share !== null && totalThread > 0 ? (
              <span className="num text-ink-400 tabular-nums"
                    title="Share of the work done inside the page loop. The three phases overlap, so this is thread time, not wall clock.">
                {Math.round((share / totalThread) * 100)}% of page work
              </span>
            ) : null}
          </div>
        );
      })}
    </>
  );
}

function StageRow({ record, name }: { record?: StageRecord; name: string }) {
  const status = record?.status ?? "PENDING";
  const total = record?.items_total ?? 0;
  const done = record?.items_done ?? 0;
  const pct = total > 0 ? Math.min(100, Math.round((done / total) * 100)) : null;

  const icon = status === "SUCCEEDED" || status === "PARTIAL"
    ? <Check size={12} className="text-pass-400" />
    : status === "RUNNING"
      ? <Loader2 size={12} className="text-run-400 animate-spin" />
      : status === "FAILED"
        ? <X size={12} className="text-fail-400" />
        : status === "CANCELLED"
          ? <X size={12} className="text-ink-500" />
          : <Circle size={7} className="text-ink-600" />;

  return (
    <div className={clsx(
      "flex items-center gap-2.5 h-7 px-3 text-xs border-b border-ink-800/60",
      status === "PENDING" && "opacity-45",
    )}>
      <span className="w-3.5 flex justify-center shrink-0">{icon}</span>
      <span className={clsx("flex-1 truncate",
        status === "RUNNING" ? "text-ink-50 font-medium" : "text-ink-200")}>
        {name}
      </span>
      {total > 0 ? (
        <span className="num text-2xs text-ink-300 tabular-nums">
          {done}/{total}
          {pct !== null && status === "RUNNING" ? ` · ${pct}%` : ""}
        </span>
      ) : null}
      {record?.duration_ms && status !== "RUNNING" ? (
        <span className="num text-2xs text-ink-400 w-14 text-right">
          {duration(record.duration_ms)}
        </span>
      ) : <span className="w-14" />}
    </div>
  );
}

export function ProcessingPanel({ doc, status }: Props) {
  const qc = useQueryClient();
  const meta = statusMeta(doc.status);
  const paused = meta.phase === "paused";
  const stopped = meta.phase === "stopped" || meta.phase === "failed";

  const byStage = new Map((status?.stages ?? []).map((s) => [s.stage, s]));
  const pagesTotal = status?.pages_total || doc.page_count || 0;
  const pagesDone = status?.pages_done ?? doc.pages_processed ?? 0;
  const elapsed = useElapsed(doc.processing_started_at ?? null, paused || stopped);
  const rate = useThroughput(pagesDone, pagesTotal);

  const refresh = () => {
    qc.invalidateQueries({ queryKey: ["document", doc.id] });
    qc.invalidateQueries({ queryKey: ["status", doc.id] });
    qc.invalidateQueries({ queryKey: ["documents"] });
  };
  const pause = useMutation({ mutationFn: () => api.pauseDocument(doc.id), onSuccess: refresh });
  const resume = useMutation({ mutationFn: () => api.resumeDocument(doc.id), onSuccess: refresh });
  const cancel = useMutation({ mutationFn: () => api.cancelDocument(doc.id), onSuccess: refresh });
  const busy = pause.isPending || resume.isPending || cancel.isPending;
  const actionError = [pause, resume, cancel].find((m) => m.error)?.error as Error | undefined;

  const overallPct = pagesTotal > 0
    ? Math.min(100, Math.round((pagesDone / pagesTotal) * 100)) : 0;
  // Every page is read but the document is not finished: tables, extraction,
  // reconciliation and analysis still have to run, and on the corpus that tail
  // is 7% to 38% of the wall clock. The bar is full and honest -- it counts
  // pages -- so the caption has to carry what is left.
  const inTail = !paused && !stopped && pagesTotal > 0 && pagesDone >= pagesTotal;

  return (
    <div className="max-w-3xl mx-auto w-full p-5 space-y-4">
      {/* ---------------------------------------------------------- headline */}
      <div className="panel">
        <div className="px-4 pt-4 pb-3.5">
          <div className="flex items-start justify-between gap-4">
            <div className="min-w-0">
              <div className="flex items-center gap-2">
                <span className={meta.chip}>{meta.label}</span>
                {status?.current_stage ? (
                  <span className="text-xs text-ink-200">
                    {stageLabel(status.current_stage)}
                  </span>
                ) : null}
              </div>
              <h2 className="mt-2 text-base font-semibold text-ink-50 truncate"
                  title={doc.original_filename}>
                {doc.original_filename}
              </h2>
              <p className="text-2xs text-ink-400 mt-0.5">
                {pagesTotal ? `${pagesTotal} pages` : "—"}
                {status?.processing_mode ? ` · ${status.processing_mode}` : ""}
                {doc.progress_note ? ` · ${doc.progress_note}` : ""}
              </p>
            </div>

            <div className="flex items-center gap-1.5 shrink-0">
              {doc.can_pause ? (
                <button className="btn-ghost" disabled={busy}
                        onClick={() => pause.mutate()}>
                  <Pause size={12} /> Pause
                </button>
              ) : null}
              {doc.can_resume ? (
                <button className="btn-primary" disabled={busy}
                        onClick={() => resume.mutate()}>
                  <Play size={12} /> Resume
                </button>
              ) : null}
              {doc.can_cancel ? (
                <button className="btn-quiet" disabled={busy}
                        onClick={() => cancel.mutate()}>
                  <X size={12} /> Cancel
                </button>
              ) : null}
            </div>
          </div>

          {/* ------------------------------------------------------ progress */}
          <div className="mt-4">
            <div className="flex items-end justify-between mb-1.5">
              <span className="num text-2xl font-semibold text-ink-50 leading-none">
                {overallPct}<span className="text-base text-ink-400">%</span>
              </span>
              <span className="text-2xs text-ink-300 num">
                {pagesDone} of {pagesTotal || "?"} pages read
              </span>
            </div>
            <div className="h-1.5 bg-ink-800 rounded-full overflow-hidden">
              <div
                className={clsx("h-full rounded-full transition-[width] duration-700",
                  paused ? "bg-hold-500" : stopped ? "bg-ink-600" : "bg-run-500")}
                style={{ width: `${overallPct}%` }}
              />
            </div>

            <div className="mt-3 grid grid-cols-3 gap-3 text-center">
              <div>
                <div className="label">Elapsed</div>
                <div className="num text-sm text-ink-100 mt-0.5">
                  {elapsed !== null ? duration(elapsed) : "—"}
                </div>
              </div>
              <div>
                <div className="label">Per page</div>
                <div className="num text-sm text-ink-100 mt-0.5">
                  {rate ? `${(rate.perPage / 1000).toFixed(1)}s` : "—"}
                </div>
              </div>
              <div>
                <div className="label">Pages remaining</div>
                <div className="num text-sm text-ink-100 mt-0.5">
                  {paused ? "paused"
                    : inTail ? "all read"
                      // A warm run reads cached pages in milliseconds; rounding
                      // that to "~0s" would claim the run is finishing when
                      // only its pages are (Phase 22).
                      : rate ? (rate.remainingMs < 1000 ? "under 1s"
                        : `~${duration(rate.remainingMs)}`)
                        : "estimating…"}
                </div>
              </div>
            </div>
            {inTail ? (
              <p className="text-3xs text-ink-500 text-center mt-2">
                All pages read. The remaining stages
                {status?.current_stage
                  ? <> &mdash; currently {stageLabel(status.current_stage).toLowerCase()} &mdash;</>
                  : " "} have no per-page rate to measure, so there is no
                estimate for them.
              </p>
            ) : !rate && !paused && !stopped ? (
              <p className="text-3xs text-ink-500 text-center mt-2">
                An estimate appears once enough pages have finished to measure a
                rate from this run.
              </p>
            ) : null}
          </div>
        </div>

        <div className="px-4 py-2.5 border-t border-ink-750 bg-ink-950/40">
          <p className="text-2xs text-ink-300">
            {paused
              ? "Paused at a page boundary. Nothing is being processed; resume to continue from here."
              : stopped
                ? meta.hint ?? "This run is not active."
                : "Processing continues in the background. You can leave this page — progress is kept."}
          </p>
        </div>
      </div>

      {actionError ? (
        <div className="panel px-4 py-2.5 border-fail-500/40">
          <p className="text-xs text-fail-400">{actionError.message}</p>
        </div>
      ) : null}

      {/* ------------------------------------------------------------ stages */}
      <div className="panel overflow-hidden">
        <div className="panel-head"><span className="panel-title">Pipeline stages</span></div>
        <div>
          {STAGES.filter((s) => !PAGE_PHASES.includes(
            s.key as typeof PAGE_PHASES[number])).map((s) => (
            s.key === "document_assembly"
              ? <div key="pages-and-assembly">
                  <PagePhaseGroup byStage={byStage} />
                  <StageRow name={s.label} record={byStage.get(s.key)} />
                </div>
              : <StageRow key={s.key} name={s.label} record={byStage.get(s.key)} />
          ))}
        </div>
      </div>

      {status?.error ? (
        <div className="panel px-4 py-3 border-fail-500/40">
          <p className="text-2xs uppercase tracking-wider text-fail-400 font-semibold">
            Error
          </p>
          <p className="text-xs text-ink-200 mt-1 break-words">{status.error}</p>
        </div>
      ) : null}
    </div>
  );
}
