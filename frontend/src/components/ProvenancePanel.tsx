import { useQuery } from "@tanstack/react-query";
import { ExternalLink, X } from "lucide-react";
import { api } from "../api/client";
import type { BBox } from "../types";
import { ConfidenceChip, ErrorNote, Spinner } from "./ui";
import { PageCanvas } from "./PageCanvas";
import { formatFull, titleCase } from "../utils/format";

/**
 * The evidence view: for a single fact, show the exact pixels it came from.
 *
 * This is the answer to "where did this number come from?" -- the source page,
 * the region, the table cell, the OCR text, and a crop of the precise area,
 * with the box drawn on the full page for context.
 */
export function ProvenancePanel({
  documentId, factId, onClose, onOpenPage,
}: {
  documentId: string;
  factId: string;
  onClose: () => void;
  onOpenPage?: (page: number, bbox: BBox | null) => void;
}) {
  const { data, isLoading, error, refetch } = useQuery({
    queryKey: ["provenance", documentId, factId],
    queryFn: () => api.provenance(documentId, factId),
  });

  return (
    <aside className="w-[480px] shrink-0 border-l border-ink-800 bg-ink-900 flex flex-col h-full">
      <header className="panel-head shrink-0">
        <div className="min-w-0">
          <h2 className="text-sm font-semibold">Source evidence</h2>
          <p className="text-xs text-ink-400 mt-0.5">
            Every figure traces back to the pixels it was read from
          </p>
        </div>
        <button className="btn-ghost btn-sm" onClick={onClose} aria-label="Close">
          <X size={14} />
        </button>
      </header>

      <div className="flex-1 overflow-y-auto min-h-0">
        {isLoading && <Spinner label="Resolving provenance…" />}
        {error && (
          <div className="p-4">
            <ErrorNote error={error} onRetry={refetch} />
          </div>
        )}
        {data && (
          <div className="p-4 space-y-4">
            <div>
              <p className="text-2xs uppercase tracking-wider text-ink-400 font-semibold">
                {titleCase(data.metric ?? "metric")}
              </p>
              <p className="text-2xl font-semibold num mt-1">
                {formatFull(data.value, data.currency, data.unit, null)}
              </p>
              <div className="flex flex-wrap items-center gap-2 mt-2">
                {data.period && (
                  <span className="chip bg-ink-800 text-ink-300 border border-ink-700">
                    {data.period}
                  </span>
                )}
                {typeof data.confidence === "number" && (
                  <ConfidenceChip
                    band={
                      data.confidence >= 0.85
                        ? "HIGH"
                        : data.confidence >= 0.65
                          ? "MEDIUM"
                          : "LOW"
                    }
                    value={data.confidence}
                  />
                )}
              </div>
            </div>

            <dl className="text-xs space-y-1.5 border-t border-ink-800 pt-3">
              <Row label="Document" value={documentId} mono />
              <Row label="Page" value={String(data.page_number)} />
              <Row label="Method" value={data.extraction_method || "—"} />
              {data.region?.type && <Row label="Region type" value={data.region.type} />}
              {data.table_id && <Row label="Table" value={data.table_id} mono />}
              {data.cell && (
                <Row
                  label="Cell"
                  value={`row ${data.cell.row}, col ${data.cell.col} (${data.cell.role})`}
                />
              )}
              {data.bbox && (
                <Row
                  label="Bounding box"
                  value={`x ${Math.round(data.bbox.x)}, y ${Math.round(data.bbox.y)}, ${Math.round(
                    data.bbox.width,
                  )}×${Math.round(data.bbox.height)} px`}
                  mono
                />
              )}
              {data.page_render && (
                <Row
                  label="Page render"
                  value={`${data.page_render.width}×${data.page_render.height} @ ${data.page_render.dpi} dpi`}
                  mono
                />
              )}
              <Row
                label="OCR tokens"
                value={String((data.ocr_element_ids ?? []).length)}
              />
            </dl>

            <div>
              <p className="text-2xs uppercase tracking-wider text-ink-400 font-semibold mb-1.5">
                Text as read
              </p>
              <p className="text-sm font-mono bg-ink-850 border border-ink-700 rounded p-2.5 break-words">
                {data.original_text || "—"}
              </p>
            </div>

            {data.crop_url && (
              <div>
                <p className="text-2xs uppercase tracking-wider text-ink-400 font-semibold mb-1.5">
                  Exact source area
                </p>
                <img
                  src={data.crop_url}
                  alt="Cropped source region"
                  className="w-full rounded border border-ink-700 bg-ink-900"
                />
              </div>
            )}

            {data.page_image_url && data.bbox_relative && (
              <div>
                <div className="flex items-center justify-between mb-1.5">
                  <p className="text-2xs uppercase tracking-wider text-ink-400 font-semibold">
                    Location on page {data.page_number}
                  </p>
                  {onOpenPage && (
                    <button
                      className="btn-ghost btn-sm"
                      onClick={() => onOpenPage(data.page_number, data.bbox_relative)}
                    >
                      <ExternalLink size={12} /> Open page
                    </button>
                  )}
                </div>
                <div className="h-[340px] border border-ink-700 rounded overflow-hidden">
                  <PageCanvas
                    imageUrl={data.page_image_url}
                    highlights={[
                      { bbox: data.bbox_relative, color: "#3b82f6", label: "source" },
                    ]}
                    focus={data.bbox_relative}
                    className="h-full"
                  />
                </div>
              </div>
            )}
          </div>
        )}
      </div>
    </aside>
  );
}

function Row({ label, value, mono }: { label: string; value: string; mono?: boolean }) {
  return (
    <div className="flex gap-3">
      <dt className="text-ink-400 w-28 shrink-0">{label}</dt>
      <dd className={`text-ink-200 break-all ${mono ? "font-mono text-2xs" : ""}`}>
        {value}
      </dd>
    </div>
  );
}
