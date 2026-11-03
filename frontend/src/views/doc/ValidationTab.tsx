import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { AlertTriangle, CheckCircle2, FileSearch, HelpCircle } from "lucide-react";
import clsx from "clsx";
import { api } from "../../api/client";
import type { ValidationDto } from "../../types";
import { Empty, ErrorNote, Spinner } from "../../components/ui";
import { titleCase } from "../../utils/format";

const FAMILY_HELP: Record<string, string> = {
  growth: "Each reported % change is recomputed from the two period values.",
  margin: "Each reported margin is recomputed from its numerator and denominator.",
  derived: "Accounting identities such as PBT − Tax = PAT.",
  ratio: "Reported ratios such as Net Debt / EBITDA.",
  aggregation: "Segment values summed against the reported consolidated total.",
  consistency: "Sign, range, unit and period-coverage sanity checks.",
};

export function ValidationTab({
  documentId, onShowProvenance,
}: {
  documentId: string;
  onShowProvenance: (factId: string) => void;
}) {
  const [status, setStatus] = useState<string>("");
  const [family, setFamily] = useState<string>("");

  const { data, isLoading, error, refetch } = useQuery({
    queryKey: ["validation", documentId, status, family],
    queryFn: () => api.validation(documentId, { status, family }),
  });

  if (isLoading) return <Spinner label="Loading validation results…" />;
  if (error)
    return (
      <div className="p-6">
        <ErrorNote error={error} onRetry={refetch} />
      </div>
    );
  if (!data) return null;

  const summary = data.summary ?? {};

  return (
    <div className="h-full overflow-y-auto">
      <div className="p-6 space-y-5">
        <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
          {(["PASSED", "WARNING", "FAILED", "REQUIRES_REVIEW"] as const).map((s) => (
            <button
              key={s}
              onClick={() => setStatus(status === s ? "" : s)}
              className={clsx(
                "panel p-3 text-left transition-colors",
                status === s ? "border-accent-500" : "hover:border-ink-700",
              )}
            >
              <p className="text-2xs uppercase tracking-wider text-ink-400 font-semibold">
                {s.replace("_", " ")}
              </p>
              <p
                className={clsx(
                  "text-xl font-semibold num mt-1",
                  s === "PASSED" && "text-pass-400",
                  s === "WARNING" && "text-warn-400",
                  s === "FAILED" && "text-fail-400",
                  s === "REQUIRES_REVIEW" && "text-warn-400",
                )}
              >
                {summary[s] ?? 0}
              </p>
            </button>
          ))}
        </div>

        <div className="panel p-4">
          <h3 className="text-xs font-semibold mb-3">Checks by rule family</h3>
          <div className="space-y-2">
            {Object.entries(data.by_family).map(([fam, counts]) => (
              <button
                key={fam}
                onClick={() => setFamily(family === fam ? "" : fam)}
                className={clsx(
                  "w-full flex items-center gap-3 text-left px-2 py-1.5 rounded transition-colors",
                  family === fam ? "bg-ink-800" : "hover:bg-ink-850",
                )}
              >
                <span className="text-sm text-ink-200 w-28 shrink-0">
                  {titleCase(fam)}
                </span>
                <span className="flex-1 flex gap-1 h-2 rounded-full overflow-hidden bg-ink-800">
                  {Object.entries(counts).map(([st, n]) => (
                    <span
                      key={st}
                      className={clsx(
                        st === "PASSED" && "bg-pass-500",
                        st === "WARNING" && "bg-warn-500",
                        st === "FAILED" && "bg-fail-500",
                        st === "REQUIRES_REVIEW" && "bg-warn-400",
                      )}
                      style={{
                        flex: n,
                      }}
                    />
                  ))}
                </span>
                <span className="text-2xs text-ink-500 num w-20 text-right shrink-0">
                  {Object.values(counts).reduce((a, b) => a + b, 0)} checks
                </span>
              </button>
            ))}
          </div>
          {family && (
            <p className="text-2xs text-ink-400 mt-3 flex items-start gap-1.5">
              <HelpCircle size={12} className="mt-0.5 shrink-0" />
              {FAMILY_HELP[family] ?? ""}
            </p>
          )}
        </div>

        {data.items.length === 0 ? (
          <Empty
            title="No checks match these filters"
            hint="Clear a filter above to see the rest of this document's validation results."
          />
        ) : (
          <div className="space-y-2">
            {data.items.map((v) => (
              <ValidationRow
                key={v.id}
                v={v}
                onShowProvenance={onShowProvenance}
              />
            ))}
          </div>
        )}
      </div>
    </div>
  );
}

function ValidationRow({
  v, onShowProvenance,
}: {
  v: ValidationDto;
  onShowProvenance: (factId: string) => void;
}) {
  const [open, setOpen] = useState(false);
  const tone =
    v.status === "PASSED"
      ? "border-pass-500/25"
      : v.status === "FAILED"
        ? "border-fail-500/40"
        : "border-warn-500/30";

  return (
    <div className={clsx("panel border", tone)}>
      <button
        className="w-full flex items-start gap-3 p-3 text-left"
        onClick={() => setOpen((o) => !o)}
      >
        {v.status === "PASSED" ? (
          <CheckCircle2 size={15} className="text-pass-400 mt-0.5 shrink-0" />
        ) : (
          <AlertTriangle
            size={15}
            className={clsx(
              "mt-0.5 shrink-0",
              v.status === "FAILED" ? "text-fail-400" : "text-warn-400",
            )}
          />
        )}
        <div className="min-w-0 flex-1">
          <p className="text-sm text-ink-200">{v.message}</p>
          <div className="flex flex-wrap items-center gap-x-3 gap-y-1 mt-1 text-2xs text-ink-500">
            <span className="font-mono">{v.rule}</span>
            <span>{titleCase(v.rule_family)}</span>
            {v.page_numbers.length > 0 && (
              <span className="num">
                page{v.page_numbers.length > 1 ? "s" : ""} {v.page_numbers.join(", ")}
              </span>
            )}
          </div>
        </div>
        {(v.reported_value !== null || v.calculated_value !== null) && (
          <div className="text-right shrink-0 text-2xs">
            <div>
              <span className="text-ink-500">reported </span>
              <span className="num text-ink-200">
                {v.reported_value?.toLocaleString() ?? "—"}
              </span>
            </div>
            <div>
              <span className="text-ink-500">calculated </span>
              <span
                className={clsx(
                  "num",
                  v.status === "PASSED" ? "text-pass-400" : "text-warn-400",
                )}
              >
                {v.calculated_value?.toLocaleString(undefined, {
                  maximumFractionDigits: 2,
                }) ?? "—"}
              </span>
            </div>
          </div>
        )}
      </button>

      {open && (
        <div className="px-3 pb-3 pt-0 border-t border-ink-850 mt-1">
          {v.difference !== null && (
            <p className="text-2xs text-ink-400 mt-2">
              Difference {v.difference.toLocaleString(undefined, {
                maximumFractionDigits: 4,
              })}
              {v.tolerance !== null && ` · tolerance ${v.tolerance}`}
            </p>
          )}
          {Object.keys(v.detail ?? {}).length > 0 && (
            <pre className="text-2xs text-ink-400 bg-ink-850 rounded p-2 mt-2 overflow-x-auto">
              {JSON.stringify(v.detail, null, 2)}
            </pre>
          )}
          {v.fact_ids.length > 0 && (
            <div className="mt-2 flex flex-wrap gap-1.5">
              {v.fact_ids.slice(0, 8).map((fid) => (
                <button
                  key={fid}
                  className="btn-ghost btn-sm font-mono text-2xs"
                  onClick={() => onShowProvenance(fid)}
                >
                  <FileSearch size={11} /> {fid.slice(0, 14)}
                </button>
              ))}
            </div>
          )}
        </div>
      )}
    </div>
  );
}
