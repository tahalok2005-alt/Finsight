import { useMemo, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { ArrowDownUp, Crosshair, FileSearch, Filter } from "lucide-react";
import clsx from "clsx";
import { api, type FactQuery } from "../../api/client";
import type { BBox, FinancialFact } from "../../types";
import {
  ConfidenceChip, Empty, ErrorNote, Spinner, ValidationChip,
} from "../../components/ui";
import { formatFull, formatGrowth } from "../../utils/format";

export function FactsTab({
  documentId, onShowProvenance, onOpenSource,
}: {
  documentId: string;
  onShowProvenance: (factId: string) => void;
  onOpenSource: (page: number, bbox: BBox | null) => void;
}) {
  const [query, setQuery] = useState<FactQuery>({
    limit: 300, sort: "page", order: "asc",
  });
  const [text, setText] = useState("");

  const { data, isLoading, error, refetch } = useQuery({
    queryKey: ["facts", documentId, query],
    queryFn: () => api.financials(documentId, query),
  });

  const rows = useMemo(() => {
    if (!data) return [];
    if (!text.trim()) return data.items;
    const needle = text.toLowerCase();
    return data.items.filter(
      (f) =>
        f.metric.toLowerCase().includes(needle) ||
        f.canonical_metric.toLowerCase().includes(needle) ||
        (f.entity ?? "").toLowerCase().includes(needle) ||
        (f.period ?? "").toLowerCase().includes(needle),
    );
  }, [data, text]);

  const set = (patch: Partial<FactQuery>) =>
    setQuery((q) => ({ ...q, ...patch, offset: 0 }));

  return (
    <div className="h-full flex flex-col min-h-0">
      <div className="px-6 py-3 border-b border-ink-800 flex flex-wrap items-center gap-2 shrink-0">
        <div className="relative">
          <Filter size={13} className="absolute left-2.5 top-2.5 text-ink-500" />
          <input
            className="input pl-8 w-56"
            placeholder="Filter metric, entity, period…"
            value={text}
            onChange={(e) => setText(e.target.value)}
          />
        </div>
        <select
          className="input"
          value={query.entity ?? ""}
          onChange={(e) => set({ entity: e.target.value || undefined })}
        >
          <option value="">All entities</option>
          {data?.facets.entities.map((e) => (
            <option key={e} value={e}>{e}</option>
          ))}
        </select>
        <select
          className="input"
          value={query.period ?? ""}
          onChange={(e) => set({ period: e.target.value || undefined })}
        >
          <option value="">All periods</option>
          {data?.facets.periods.map((p) => (
            <option key={p} value={p}>{p}</option>
          ))}
        </select>
        <select
          className="input"
          value={query.band ?? ""}
          onChange={(e) => set({ band: e.target.value || undefined })}
        >
          <option value="">Any confidence</option>
          <option value="HIGH">High</option>
          <option value="MEDIUM">Medium</option>
          <option value="LOW">Low</option>
          <option value="REQUIRES_REVIEW">Requires review</option>
        </select>
        <select
          className="input"
          value={query.validation_status ?? ""}
          onChange={(e) => set({ validation_status: e.target.value || undefined })}
        >
          <option value="">Any validation</option>
          <option value="PASSED">Passed</option>
          <option value="WARNING">Warning</option>
          <option value="FAILED">Failed</option>
          <option value="REQUIRES_REVIEW">Requires review</option>
          <option value="NOT_CHECKED">Not checked</option>
        </select>
        <button
          className={clsx("btn-ghost btn-sm", query.requires_review && "!text-warn-400")}
          onClick={() =>
            set({ requires_review: query.requires_review ? undefined : true })
          }
        >
          Needs review
        </button>
        <button
          className="btn-ghost btn-sm"
          onClick={() =>
            set({
              sort: query.sort === "confidence" ? "page" : "confidence",
              order: query.sort === "confidence" ? "asc" : "asc",
            })
          }
        >
          <ArrowDownUp size={12} />
          {query.sort === "confidence" ? "By confidence" : "By page"}
        </button>
        <span className="ml-auto text-xs text-ink-400 num">
          {rows.length}
          {data && rows.length !== data.total && ` of ${data.total}`} facts
        </span>
      </div>

      <div className="flex-1 overflow-auto min-h-0">
        {isLoading && <Spinner label="Loading financial facts…" />}
        {error && (
          <div className="p-6">
            <ErrorNote error={error} onRetry={refetch} />
          </div>
        )}
        {data && rows.length === 0 && (
          <Empty
            title="No facts match these filters"
            hint="Clear a filter, or check the Validation tab if extraction reported problems."
          />
        )}
        {rows.length > 0 && (
          <table className="w-full">
            <thead>
              <tr>
                <th className="th">Metric</th>
                <th className="th">Entity</th>
                <th className="th">Period</th>
                <th className="th text-right">Value</th>
                <th className="th text-right">Change</th>
                <th className="th">Confidence</th>
                <th className="th">Validation</th>
                <th className="th">Source</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((f) => (
                <FactRow
                  key={f.fact_id}
                  fact={f}
                  onShowProvenance={onShowProvenance}
                  onOpenSource={onOpenSource}
                />
              ))}
            </tbody>
          </table>
        )}
      </div>
    </div>
  );
}

function FactRow({
  fact: f, onShowProvenance, onOpenSource,
}: {
  fact: FinancialFact;
  onShowProvenance: (id: string) => void;
  onOpenSource: (page: number, bbox: BBox | null) => void;
}) {
  const corrected = f.corrected_value !== null;
  const changeBps = f.meta?.change_bps as number | undefined;
  const reportedBps = f.meta?.reported_change_bps as number | undefined;

  return (
    <tr className="hover:bg-ink-850/60 group">
      <td className="td">
        <div className="font-medium">{f.metric_display || f.metric}</div>
        <div className="text-2xs text-ink-500 truncate max-w-[220px]" title={f.metric}>
          {f.canonical_metric}
          {f.kind === "narrative" && " · from narrative"}
        </div>
      </td>
      <td className="td text-ink-300 text-xs">{f.entity ?? "—"}</td>
      <td className="td">
        <span className="chip bg-ink-800 border border-ink-700 text-ink-300">
          {f.period ?? "unattributed"}
        </span>
      </td>
      <td className="td text-right">
        <div className="num font-medium">
          {formatFull(f.effective_value, f.currency, f.unit, f.value_type)}
        </div>
        {corrected && (
          <div className="text-2xs text-warn-400">
            corrected · extracted {formatFull(f.value, f.currency, f.unit, f.value_type)}
          </div>
        )}
        <div className="text-2xs text-ink-600 font-mono">{f.raw_value}</div>
      </td>
      <td className="td text-right">
        {f.growth !== null ? (
          <>
            <span
              className={clsx(
                "num font-medium",
                f.growth > 0 ? "text-pass-400" : f.growth < 0 ? "text-fail-400" : "",
              )}
            >
              {formatGrowth(f.growth)}
            </span>
            <div className="text-2xs text-ink-500">
              {f.growth_type ?? ""} {f.growth_reported ? "reported" : "calculated"}
            </div>
          </>
        ) : reportedBps !== undefined || changeBps !== undefined ? (
          <>
            <span className="num text-ink-300">
              {(reportedBps ?? changeBps)!.toFixed(0)} bps
            </span>
            <div className="text-2xs text-ink-500">
              {reportedBps !== undefined ? "reported" : "calculated"}
            </div>
          </>
        ) : (
          <span className="text-ink-600">—</span>
        )}
      </td>
      <td className="td">
        <ConfidenceChip band={f.confidence_band} value={f.confidence} />
      </td>
      <td className="td">
        <ValidationChip status={f.validation_status} />
      </td>
      <td className="td">
        <div className="flex items-center gap-1">
          <button
            className="btn-ghost btn-sm opacity-0 group-hover:opacity-100 transition-opacity"
            onClick={() => onShowProvenance(f.fact_id)}
            title="Show the exact source"
          >
            <FileSearch size={12} />
          </button>
          <button
            className="btn-ghost btn-sm opacity-0 group-hover:opacity-100 transition-opacity"
            onClick={() =>
              onOpenSource(f.source_page, f.provenance?.bbox_relative ?? null)
            }
            title="Open the source page"
          >
            <Crosshair size={12} />
          </button>
          <span className="text-2xs text-ink-500 num ml-1">p{f.source_page}</span>
        </div>
      </td>
    </tr>
  );
}
