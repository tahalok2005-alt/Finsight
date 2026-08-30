import { useEffect, useMemo, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { ArrowRight } from "lucide-react";
import clsx from "clsx";
import { api } from "../api/client";
import { Empty, ErrorNote, Panel, Spinner } from "../components/ui";
import { formatFull, formatGrowth } from "../utils/format";

const BASIS_STYLE: Record<string, string> = {
  reported: "bg-accent-600/10 text-accent-300 border border-accent-500/30",
  calculated: "bg-ink-800 text-ink-400 border border-ink-700",
  not_comparable: "bg-warn-900 text-warn-400 border border-warn-500/30",
};

export function CompareView() {
  const [left, setLeft] = useState("");
  const [right, setRight] = useState("");
  const [leftPeriod, setLeftPeriod] = useState("");
  const [rightPeriod, setRightPeriod] = useState("");
  const [entity, setEntity] = useState("");

  const docs = useQuery({
    queryKey: ["documents", "compare"],
    queryFn: () => api.listDocuments({ limit: 100 }),
  });

  const ready = useMemo(
    () => (docs.data?.items ?? []).filter((d) => (d.fact_count ?? 0) > 0),
    [docs.data],
  );

  useEffect(() => {
    if (ready.length && !left) {
      setLeft(ready[0].id);
      setRight(ready.length > 1 ? ready[1].id : ready[0].id);
    }
  }, [ready, left]);

  const leftPeriods = useQuery({
    queryKey: ["periods", left],
    queryFn: () => api.financials(left, { limit: 1 }),
    enabled: !!left,
  });
  const rightPeriods = useQuery({
    queryKey: ["periods", right],
    queryFn: () => api.financials(right, { limit: 1 }),
    enabled: !!right,
  });

  const comparison = useQuery({
    queryKey: ["compare", left, right, leftPeriod, rightPeriod, entity],
    queryFn: () =>
      api.compare({
        left, right,
        left_period: leftPeriod || undefined,
        right_period: rightPeriod || undefined,
        entity: entity || undefined,
      }),
    enabled: !!left && !!right,
  });

  return (
    <div className="h-full overflow-y-auto">
      <header className="px-6 py-5 border-b border-ink-800">
        <h1 className="text-lg font-semibold">Compare reports</h1>
        <p className="text-sm text-ink-400 mt-0.5">
          Two documents, or two periods within one — like-for-like only
        </p>
      </header>

      <div className="p-6 space-y-5">
        {docs.isLoading && <Spinner label="Loading documents…" />}
        {ready.length === 0 && !docs.isLoading && (
          <Empty
            title="Nothing to compare yet"
            hint="Process at least one document with extracted facts."
          />
        )}

        {ready.length > 0 && (
          <>
            <div className="panel p-4 grid grid-cols-1 md:grid-cols-[1fr_auto_1fr] gap-4 items-end">
              <Side
                label="Baseline"
                documents={ready}
                value={left}
                onChange={setLeft}
                period={leftPeriod}
                onPeriod={setLeftPeriod}
                periods={leftPeriods.data?.facets.periods ?? []}
              />
              <div className="flex justify-center pb-2">
                <ArrowRight size={18} className="text-ink-500" />
              </div>
              <Side
                label="Comparison"
                documents={ready}
                value={right}
                onChange={setRight}
                period={rightPeriod}
                onPeriod={setRightPeriod}
                periods={rightPeriods.data?.facets.periods ?? []}
              />
            </div>

            {comparison.data && comparison.data.entities.length > 1 && (
              <div className="flex flex-wrap items-center gap-1.5">
                <span className="text-xs text-ink-400 mr-1">Entity:</span>
                <button
                  className={clsx("btn-ghost btn-sm", !entity && "!text-accent-400")}
                  onClick={() => setEntity("")}
                >
                  All
                </button>
                {comparison.data.entities.map((e) => (
                  <button
                    key={e}
                    className={clsx("btn-ghost btn-sm", entity === e && "!text-accent-400")}
                    onClick={() => setEntity(entity === e ? "" : e)}
                  >
                    {e}
                  </button>
                ))}
              </div>
            )}

            {comparison.isLoading && <Spinner label="Comparing…" />}
            {comparison.error && (
              <ErrorNote error={comparison.error} onRetry={comparison.refetch} />
            )}

            {comparison.data && (
              <>
                <Panel
                  title="Metric comparison"
                  subtitle={`${comparison.data.left.period ?? "—"} → ${comparison.data.right.period ?? "—"}`}
                  bodyClassName="p-0"
                >
                  <table className="w-full">
                    <thead>
                      <tr>
                        <th className="th">Metric</th>
                        <th className="th">Entity</th>
                        <th className="th text-right">Baseline</th>
                        <th className="th text-right">Comparison</th>
                        <th className="th text-right">Change</th>
                        <th className="th">Basis</th>
                      </tr>
                    </thead>
                    <tbody>
                      {comparison.data.comparisons
                        .filter(
                          (c) => c.left.value !== null || c.right.value !== null,
                        )
                        .map((c) => (
                          <tr
                            key={`${c.canonical_metric}-${c.entity}`}
                            className="hover:bg-ink-850/60"
                          >
                            <td className="td font-medium">{c.metric}</td>
                            <td className="td text-xs text-ink-400">{c.entity}</td>
                            <td className="td text-right num">
                              {formatFull(c.left.value, c.currency, c.unit, c.value_type)}
                              <div className="text-2xs text-ink-600">
                                {c.left.period ?? ""}
                                {c.left.basis === "reviewed" && " · reviewed"}
                              </div>
                            </td>
                            <td className="td text-right num">
                              {formatFull(c.right.value, c.currency, c.unit, c.value_type)}
                              <div className="text-2xs text-ink-600">
                                {c.right.period ?? ""}
                                {c.right.basis === "reviewed" && " · reviewed"}
                              </div>
                            </td>
                            <td className="td text-right">
                              {c.change_basis === "not_comparable" ? (
                                <span className="text-warn-400 text-2xs">
                                  not comparable
                                </span>
                              ) : c.change_pct !== null ? (
                                <span
                                  className={clsx(
                                    "num font-medium",
                                    c.change_pct > 0
                                      ? "text-pass-400"
                                      : c.change_pct < 0
                                        ? "text-fail-400"
                                        : "",
                                  )}
                                >
                                  {formatGrowth(c.change_pct)}
                                </span>
                              ) : c.change !== null ? (
                                <span className="num text-ink-300">
                                  {c.change > 0 ? "+" : ""}
                                  {c.change.toLocaleString(undefined, {
                                    maximumFractionDigits: 2,
                                  })}
                                  {c.value_type === "percentage" && " pp"}
                                </span>
                              ) : (
                                <span className="text-ink-600">—</span>
                              )}
                            </td>
                            <td className="td">
                              <span className={clsx("chip", BASIS_STYLE[c.change_basis])}>
                                {c.change_basis.replace("_", " ")}
                              </span>
                              {c.note && (
                                <p className="text-2xs text-ink-500 mt-1 max-w-[220px]">
                                  {c.note}
                                </p>
                              )}
                            </td>
                          </tr>
                        ))}
                    </tbody>
                  </table>
                </Panel>

                <Panel title="Guidance changes">
                  <div className="grid grid-cols-1 md:grid-cols-3 gap-4 text-xs">
                    <GuidanceList
                      title="Added"
                      items={comparison.data.guidance_changes.added}
                      tone="text-pass-400"
                    />
                    <GuidanceList
                      title="Retained"
                      items={comparison.data.guidance_changes.retained}
                      tone="text-ink-300"
                    />
                    <GuidanceList
                      title="Removed"
                      items={comparison.data.guidance_changes.removed}
                      tone="text-fail-400"
                    />
                  </div>
                </Panel>
              </>
            )}
          </>
        )}
      </div>
    </div>
  );
}

function Side({
  label, documents, value, onChange, period, onPeriod, periods,
}: {
  label: string;
  documents: { id: string; company_name: string | null; original_filename: string; report_period: string | null }[];
  value: string;
  onChange: (v: string) => void;
  period: string;
  onPeriod: (v: string) => void;
  periods: string[];
}) {
  return (
    <div>
      <label className="text-2xs uppercase tracking-wider text-ink-400 font-semibold">
        {label}
      </label>
      <select
        className="input w-full mt-1.5"
        value={value}
        onChange={(e) => {
          onChange(e.target.value);
          onPeriod("");
        }}
      >
        {documents.map((d) => (
          <option key={d.id} value={d.id}>
            {d.company_name ?? d.original_filename}
            {d.report_period ? ` — ${d.report_period}` : ""}
          </option>
        ))}
      </select>
      <select
        className="input w-full mt-2"
        value={period}
        onChange={(e) => onPeriod(e.target.value)}
      >
        <option value="">Document's primary period</option>
        {periods.map((p) => (
          <option key={p} value={p}>{p}</option>
        ))}
      </select>
    </div>
  );
}

function GuidanceList({
  title, items, tone,
}: { title: string; items: string[]; tone: string }) {
  return (
    <div>
      <p className="text-2xs uppercase tracking-wider text-ink-400 font-semibold mb-1.5">
        {title}
      </p>
      {items.length === 0 ? (
        <p className="text-ink-600">—</p>
      ) : (
        <ul className="space-y-1">
          {items.map((m) => (
            <li key={m} className={tone}>
              {m}
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
