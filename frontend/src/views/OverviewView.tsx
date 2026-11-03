import { useQuery } from "@tanstack/react-query";
import { Link } from "react-router-dom";
import {
  AlertTriangle, CheckCircle2, ChevronRight, ClipboardList, FileText, Table2,
  Upload,
} from "lucide-react";
import { api } from "../api/client";
import {
  Empty, ErrorNote, Panel, ProgressBar, Spinner, Stat, StatusChip,
} from "../components/ui";
import {
  TERMINAL_STATUSES, formatDuration, relativeTime,
} from "../utils/format";
import { statusMeta } from "../utils/lifecycle";

export function OverviewView() {
  const { data, isLoading, error, refetch } = useQuery({
    queryKey: ["overview"],
    queryFn: api.overview,
    // poll while anything is still processing so the dashboard stays live
    refetchInterval: (q) =>
      (q.state.data?.documents.processing ?? 0) > 0 ? 2500 : false,
  });

  if (isLoading) return <Spinner label="Loading dashboard…" />;
  if (error)
    return (
      <div className="p-6">
        <ErrorNote error={error} onRetry={refetch} />
      </div>
    );
  if (!data) return null;

  // Work, not statistics: what is running, and what is waiting on a person.
  const recent = data.recent_documents;
  const inFlight = recent.filter((d) => !TERMINAL_STATUSES.has(d.status));
  const attention = recent.filter((d) =>
    ["FAILED", "INTERRUPTED", "REQUIRES_REVIEW", "PARTIALLY_COMPLETED"]
      .includes(d.status));

  return (
    <div className="h-full overflow-y-auto">
      <header className="px-6 py-5 border-b border-ink-800 flex items-center justify-between">
        <div>
          <h1 className="text-lg font-semibold">Overview</h1>
          <p className="text-sm text-ink-400 mt-0.5">
            Extraction, validation and review across every processed report
          </p>
        </div>
        <Link to="/upload" className="btn-primary">
          <Upload size={14} /> Upload report
        </Link>
      </header>

      <div className="p-6 space-y-6">
        <div className="grid grid-cols-2 lg:grid-cols-4 gap-4">
          <Stat
            label="Documents"
            value={data.documents.total}
            hint={
              data.documents.processing > 0
                ? `${data.documents.processing} processing now`
                : data.documents.failed > 0
                  ? `${data.documents.failed} failed`
                  : "all runs finished"
            }
            tone={data.documents.failed > 0 ? "warn" : "accent"}
          />
          <Stat
            label="Financial figures"
            value={data.extraction.facts.toLocaleString()}
            hint={`${data.extraction.tables} tables · ${data.extraction.charts} charts`}
            tone="pass"
          />
          <Stat
            label="Validation warnings"
            value={data.validation.warnings + data.validation.failures}
            hint={`${data.validation.passed} checks passed`}
            tone={
              data.validation.failures > 0
                ? "fail"
                : data.validation.warnings > 0
                  ? "warn"
                  : "pass"
            }
          />
          <Stat
            label="Review queue"
            value={data.review_queue}
            hint="facts awaiting a human decision"
            tone={data.review_queue > 0 ? "warn" : "pass"}
          />
        </div>

        <div className="grid grid-cols-1 lg:grid-cols-3 gap-6">
          <Panel
            title="Recent documents"
            subtitle="Newest first"
            className="lg:col-span-2"
            bodyClassName="p-0"
          >
            {data.recent_documents.length === 0 ? (
              <Empty
                title="No documents yet"
                hint="Upload an earnings release, quarterly result or annual report to begin."
              />
            ) : (
              <table className="w-full">
                <thead>
                  <tr>
                    <th className="th">Document</th>
                    <th className="th">Period</th>
                    <th className="th">Pages</th>
                    <th className="th">Status</th>
                    <th className="th">Time</th>
                  </tr>
                </thead>
                <tbody>
                  {data.recent_documents.map((d) => (
                    <tr key={d.id} className="hover:bg-ink-850/60">
                      <td className="td">
                        <Link to={`/documents/${d.id}`} className="link font-medium">
                          {d.company_name || d.original_filename}
                        </Link>
                        <p className="text-2xs text-ink-500 truncate max-w-[280px]">
                          {d.original_filename}
                        </p>
                        {d.error && (
                          <p className="text-2xs text-fail-400 mt-0.5 truncate max-w-[280px]">
                            {d.error}
                          </p>
                        )}
                      </td>
                      <td className="td text-ink-300">{d.report_period ?? "—"}</td>
                      <td className="td num text-ink-300">{d.page_count}</td>
                      <td className="td">
                        <StatusChip status={d.status} />
                        {!TERMINAL_STATUSES.has(d.status) && (
                          // real page progress, never a placeholder value
                          <ProgressBar
                            value={(d.pages_processed ?? 0)
                              / Math.max(1, d.page_count ?? 1)}
                            className="mt-1.5 w-20"
                          />
                        )}
                      </td>
                      <td className="td text-2xs text-ink-500">
                        {relativeTime(d.created_at)}
                        <br />
                        {formatDuration(d.processing_ms)}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
          </Panel>

          <div className="space-y-4">
            {/* Two panels replace the confidence histogram and the "most
                extracted metrics" list that used to sit here.  Both of those
                described the corpus without giving anyone anything to do; a
                landing screen should surface work, not statistics. */}
            <Panel title="Needs your attention"
                   subtitle="Documents waiting on a person"
                   bodyClassName="p-0">
              {attention.length === 0 ? (
                <Empty title="Nothing waiting"
                       hint="No document is failed, interrupted or flagged for review." />
              ) : (
                <ul className="divide-y divide-ink-800">
                  {attention.slice(0, 6).map((d) => {
                    const meta = statusMeta(d.status);
                    return (
                      <li key={d.id}>
                        <Link to={`/documents/${d.id}`}
                              className="flex items-center gap-2.5 px-3.5 h-11
                                         hover:bg-ink-850 transition-colors">
                          <span className={meta.chip}>{meta.label}</span>
                          <span className="flex-1 min-w-0 truncate text-xs text-ink-100">
                            {d.company_name || d.original_filename}
                          </span>
                          <ChevronRight size={13} className="text-ink-500 shrink-0" />
                        </Link>
                      </li>
                    );
                  })}
                </ul>
              )}
            </Panel>

            <Panel title="Processing now" bodyClassName="p-0">
              {inFlight.length === 0 ? (
                <Empty title="Nothing processing"
                       hint="Uploaded documents appear here while they run." />
              ) : (
                <ul className="divide-y divide-ink-800">
                  {inFlight.map((d) => {
                    const total = d.page_count || 0;
                    const done = d.pages_processed || 0;
                    const pct = total ? Math.round((done / total) * 100) : 0;
                    return (
                      <li key={d.id} className="px-3.5 py-2.5">
                        <Link to={`/documents/${d.id}`}
                              className="text-xs text-ink-100 hover:text-accent-300
                                         truncate block">
                          {d.original_filename}
                        </Link>
                        <div className="flex items-center gap-2 mt-1.5">
                          <div className="h-1 flex-1 bg-ink-800 rounded-full overflow-hidden">
                            <div className="h-full bg-run-500 rounded-full"
                                 style={{ width: `${pct}%` }} />
                          </div>
                          <span className="text-2xs num text-ink-300">
                            {done}/{total || "?"}
                          </span>
                        </div>
                      </li>
                    );
                  })}
                </ul>
              )}
            </Panel>
          </div>
        </div>

        <div className="grid grid-cols-1 md:grid-cols-3 gap-6">
          <Panel title="Validation outcome">
            <div className="space-y-2 text-sm">
              <Line
                icon={<CheckCircle2 size={14} className="text-pass-400" />}
                label="Passed"
                value={data.validation.passed}
              />
              <Line
                icon={<AlertTriangle size={14} className="text-warn-400" />}
                label="Warnings"
                value={data.validation.warnings}
              />
              <Line
                icon={<AlertTriangle size={14} className="text-fail-400" />}
                label="Failures"
                value={data.validation.failures}
              />
            </div>
            <p className="text-2xs text-ink-500 mt-3 leading-relaxed">
              Every reported growth rate, margin and accounting identity is
              recomputed from the extracted values. Discrepancies are surfaced,
              never corrected silently.
            </p>
          </Panel>

          <Panel title="Extraction volume">
            <div className="space-y-2 text-sm">
              <Line
                icon={<FileText size={14} className="text-accent-400" />}
                label="Figures"
                value={data.extraction.facts}
              />
              <Line
                icon={<Table2 size={14} className="text-accent-400" />}
                label="Tables"
                value={data.extraction.tables}
              />
              <Line
                icon={<ClipboardList size={14} className="text-accent-400" />}
                label="Commentary"
                value={data.extraction.commentary}
              />
              <Line
                icon={<ClipboardList size={14} className="text-warn-400" />}
                label="Guidance"
                value={data.extraction.guidance}
              />
            </div>
          </Panel>

          <Panel title="Processing jobs">
            <div className="space-y-2 text-sm">
              <Line label="Queued" value={data.jobs.queued} />
              <Line label="Running" value={data.jobs.running} />
              <Line label="Failed" value={data.jobs.failed} />
              <Line label="Active workers" value={data.jobs.active_workers} />
            </div>
            <p className="text-2xs text-ink-500 mt-3 leading-relaxed">
              Documents are processed on background workers; the API never blocks
              on extraction.
            </p>
          </Panel>
        </div>
      </div>
    </div>
  );
}

function Line({
  icon, label, value,
}: { icon?: React.ReactNode; label: string; value: number }) {
  return (
    <div className="flex items-center gap-2">
      {icon}
      <span className="flex-1 text-ink-300">{label}</span>
      <span className="num font-medium">{value.toLocaleString()}</span>
    </div>
  );
}
