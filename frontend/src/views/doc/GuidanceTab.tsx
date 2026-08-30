import { useQuery } from "@tanstack/react-query";
import { ArrowDownRight, ArrowUpRight, Crosshair, Minus, TriangleAlert } from "lucide-react";
import clsx from "clsx";
import { api } from "../../api/client";
import type { BBox } from "../../types";
import { Empty, ErrorNote, Spinner } from "../../components/ui";
import { formatFull, titleCase } from "../../utils/format";

const NATURE_STYLE: Record<string, string> = {
  guidance: "bg-accent-600/10 text-accent-300 border border-accent-500/30",
  outlook: "bg-ink-800 text-ink-300 border border-ink-700",
  target: "bg-warn-900 text-warn-400 border border-warn-500/30",
};

export function GuidanceTab({
  documentId, onOpenSource,
}: {
  documentId: string;
  onOpenSource: (page: number, bbox: BBox | null) => void;
}) {
  const { data, isLoading, error, refetch } = useQuery({
    queryKey: ["guidance", documentId],
    queryFn: () => api.guidance(documentId),
  });

  if (isLoading) return <Spinner label="Loading guidance…" />;
  if (error)
    return (
      <div className="p-6">
        <ErrorNote error={error} onRetry={refetch} />
      </div>
    );

  return (
    <div className="h-full overflow-y-auto">
      <div className="px-6 py-3 border-b border-ink-800">
        <p className="text-2xs text-ink-400 flex items-start gap-1.5">
          <TriangleAlert size={12} className="mt-0.5 shrink-0 text-warn-400" />
          Forward-looking statements. These are kept strictly separate from
          historical actuals and never enter the financial fact set — a target is
          not a result.
        </p>
      </div>

      {!data?.items.length ? (
        <Empty
          title="No forward-looking guidance was found"
          hint="Only statements with an explicit forward-looking trigger are recorded here; the safe-harbour disclaimer is deliberately excluded."
        />
      ) : (
        <ul className="divide-y divide-ink-850">
          {data.items.map((g) => (
            <li key={g.id} className="p-5 hover:bg-ink-900/40 group">
              <div className="flex items-start justify-between gap-4">
                <div className="flex flex-wrap items-center gap-2">
                  <span className={clsx("chip", NATURE_STYLE[g.nature] ?? NATURE_STYLE.outlook)}>
                    {g.nature}
                  </span>
                  {g.canonical_metric && (
                    <span className="chip bg-ink-800 text-ink-300 border border-ink-700">
                      {titleCase(g.canonical_metric)}
                    </span>
                  )}
                  {g.target_period && (
                    <span className="chip bg-ink-800 text-ink-300 border border-ink-700">
                      {g.target_period}
                    </span>
                  )}
                  {g.direction !== "unspecified" && (
                    <span className="flex items-center gap-1 text-2xs text-ink-400">
                      {g.direction === "increase" && (
                        <ArrowUpRight size={12} className="text-pass-400" />
                      )}
                      {g.direction === "decrease" && (
                        <ArrowDownRight size={12} className="text-fail-400" />
                      )}
                      {g.direction === "maintain" && <Minus size={12} />}
                      {g.direction}
                    </span>
                  )}
                  {g.requires_review && (
                    <span className="chip bg-warn-900 text-warn-400 border border-warn-500/30">
                      needs review
                    </span>
                  )}
                </div>
                <button
                  className="btn-ghost btn-sm opacity-0 group-hover:opacity-100 shrink-0"
                  onClick={() => onOpenSource(g.page_number, g.bbox_relative)}
                >
                  <Crosshair size={12} /> p{g.page_number}
                </button>
              </div>

              {g.target_value !== null && (
                <p className="num text-lg font-semibold mt-2">
                  {formatFull(g.target_value, g.target_currency, g.target_unit, null)}
                </p>
              )}

              <p className="text-sm text-ink-200 mt-2 leading-relaxed">{g.text}</p>

              <p className="text-2xs text-ink-500 mt-2">
                Trigger phrase: <span className="font-mono">“{g.trigger_phrase}”</span>
                {" · confidence "}
                <span className="num">{(g.confidence * 100).toFixed(0)}%</span>
              </p>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
