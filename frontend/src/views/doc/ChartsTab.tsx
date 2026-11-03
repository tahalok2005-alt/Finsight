import { useQuery } from "@tanstack/react-query";
import { Crosshair, Info } from "lucide-react";
import { api } from "../../api/client";
import type { BBox } from "../../types";
import { Empty, ErrorNote, Spinner } from "../../components/ui";
import { titleCase } from "../../utils/format";

/**
 * Charts are shown with an explicit statement of what was and was not read.
 *
 * Values appear only when they were *printed* on the chart and recognised by
 * OCR. Nothing is inferred from bar heights, so a chart with no data labels
 * shows its text and says plainly that no values were extracted.
 */
export function ChartsTab({
  documentId, onOpenSource,
}: {
  documentId: string;
  onOpenSource: (page: number, bbox: BBox | null) => void;
}) {
  const { data, isLoading, error, refetch } = useQuery({
    queryKey: ["charts", documentId],
    queryFn: () => api.charts(documentId),
  });

  if (isLoading) return <Spinner label="Loading charts…" />;
  if (error)
    return (
      <div className="p-6">
        <ErrorNote error={error} onRetry={refetch} />
      </div>
    );
  if (!data?.items.length)
    return (
      <Empty
        title="No charts were detected"
        hint="Text-and-table releases legitimately contain none; a graphical block is only recorded when its ink and colour profile distinguish it from prose."
      />
    );

  return (
    <div className="h-full overflow-y-auto p-6">
      <div className="panel p-3 mb-5 flex items-start gap-2">
        <Info size={14} className="text-accent-400 mt-0.5 shrink-0" />
        <p className="text-2xs text-ink-400 leading-relaxed">
          Chart values are read only from data labels printed on the chart.
          Deriving numbers from bar or point geometry is not attempted, because it
          cannot be done reliably at this resolution — an uncertain result that
          says so is worth more than a confident fabrication. Every chart is
          flagged for review.
        </p>
      </div>

      <div className="grid grid-cols-1 lg:grid-cols-2 gap-5">
        {data.items.map((c) => (
          <article key={c.id} className="panel overflow-hidden">
            <div className="panel-head">
              <div className="min-w-0">
                <h3 className="text-sm font-semibold truncate">
                  {c.title || `${titleCase(c.chart_type)} on page ${c.page_number}`}
                </h3>
                <p className="text-2xs text-ink-500 mt-0.5">
                  {titleCase(c.chart_type)} · type confidence{" "}
                  <span className="num">{(c.type_confidence * 100).toFixed(0)}%</span>
                </p>
              </div>
              <button
                className="btn-ghost btn-sm shrink-0"
                onClick={() => onOpenSource(c.page_number, c.bbox_relative)}
              >
                <Crosshair size={12} /> p{c.page_number}
              </button>
            </div>

            {c.crop_url && (
              <img
                src={c.crop_url}
                alt={c.title || "Detected chart"}
                className="w-full bg-ink-900 border-b border-ink-800"
              />
            )}

            <div className="p-4 space-y-3">
              <div
                className={
                  c.values_extracted
                    ? "text-2xs text-warn-400"
                    : "text-2xs text-ink-400"
                }
              >
                {c.notes}
              </div>

              {c.data_labels.length > 0 && (
                <div>
                  <p className="text-2xs uppercase tracking-wider text-ink-400 font-semibold mb-1.5">
                    Printed data labels
                  </p>
                  <div className="flex flex-wrap gap-1.5">
                    {c.data_labels.map((d, i) => (
                      <span
                        key={i}
                        className="chip bg-ink-800 text-ink-200 border border-ink-700 num"
                      >
                        {d.value?.toLocaleString() ?? "—"}
                      </span>
                    ))}
                  </div>
                </div>
              )}

              {c.categories.length > 0 && (
                <Labels title="Categories" values={c.categories} />
              )}
              {c.legend_labels.length > 0 && (
                <Labels title="Legend" values={c.legend_labels} />
              )}
              {c.axis_labels.length > 0 && (
                <Labels title="Surrounding text" values={c.axis_labels} />
              )}
            </div>
          </article>
        ))}
      </div>
    </div>
  );
}

function Labels({ title, values }: { title: string; values: string[] }) {
  return (
    <div>
      <p className="text-2xs uppercase tracking-wider text-ink-400 font-semibold mb-1.5">
        {title}
      </p>
      <div className="flex flex-wrap gap-1.5">
        {values.slice(0, 12).map((v, i) => (
          <span key={i} className="chip bg-ink-800 text-ink-300 border border-ink-700">
            {v}
          </span>
        ))}
      </div>
    </div>
  );
}
