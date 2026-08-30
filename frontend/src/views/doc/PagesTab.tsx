import { useEffect, useMemo, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { ChevronLeft, ChevronRight, Eye, EyeOff } from "lucide-react";
import clsx from "clsx";
import { api } from "../../api/client";
import type { BBox, RegionDto } from "../../types";
import { ErrorNote, Spinner } from "../../components/ui";
import { PageCanvas } from "../../components/PageCanvas";
import { REGION_COLOR, titleCase } from "../../utils/format";

/**
 * The document viewer: rendered page images with the layout the pipeline
 * actually saw, so a reader can check the machine's interpretation directly.
 */
export function PagesTab({
  documentId, pageCount, focus, onFocusHandled,
}: {
  documentId: string;
  pageCount: number;
  focus: { page: number; bbox: BBox | null } | null;
  onFocusHandled: () => void;
}) {
  const [page, setPage] = useState(focus?.page ?? 1);
  const [showRegions, setShowRegions] = useState(true);
  const [showOcr, setShowOcr] = useState(false);
  const [selected, setSelected] = useState<RegionDto | null>(null);
  const [focusBox, setFocusBox] = useState<BBox | null>(focus?.bbox ?? null);

  useEffect(() => {
    if (focus) {
      setPage(focus.page);
      setFocusBox(focus.bbox);
      onFocusHandled();
    }
  }, [focus, onFocusHandled]);

  const { data, isLoading, error, refetch } = useQuery({
    queryKey: ["page", documentId, page, showOcr],
    queryFn: () => api.page(documentId, page, { ocr: showOcr, regions: true }),
  });

  const highlights = useMemo(() => {
    const out = [];
    if (focusBox) {
      out.push({ bbox: focusBox, color: "#f59e0b", label: "selected fact" });
    }
    if (showOcr && data?.ocr_elements) {
      for (const el of data.ocr_elements) {
        out.push({
          id: el.id,
          bbox: el.bbox_relative,
          color: el.corroborated ? "#10b981" : "#ef4444",
          label: `${el.text.slice(0, 18)} (${(el.confidence * 100).toFixed(0)}%)`,
        });
      }
    }
    return out;
  }, [focusBox, showOcr, data]);

  return (
    <div className="h-full flex min-h-0">
      <div className="flex-1 min-w-0 flex flex-col">
        <div className="px-6 py-2.5 border-b border-ink-800 flex items-center gap-2 shrink-0">
          <button
            className="btn-ghost btn-sm"
            disabled={page <= 1}
            onClick={() => {
              setPage((p) => Math.max(1, p - 1));
              setFocusBox(null);
            }}
          >
            <ChevronLeft size={13} />
          </button>
          <input
            className="input w-16 text-center num"
            type="number"
            min={1}
            max={pageCount}
            value={page}
            onChange={(e) => {
              const n = Number(e.target.value);
              if (n >= 1 && n <= pageCount) {
                setPage(n);
                setFocusBox(null);
              }
            }}
          />
          <span className="text-xs text-ink-400">of {pageCount}</span>
          <button
            className="btn-ghost btn-sm"
            disabled={page >= pageCount}
            onClick={() => {
              setPage((p) => Math.min(pageCount, p + 1));
              setFocusBox(null);
            }}
          >
            <ChevronRight size={13} />
          </button>

          <div className="ml-4 flex items-center gap-2">
            <button
              className={clsx("btn-ghost btn-sm", showRegions && "!text-accent-400")}
              onClick={() => setShowRegions((v) => !v)}
            >
              {showRegions ? <Eye size={12} /> : <EyeOff size={12} />} Regions
            </button>
            <button
              className={clsx("btn-ghost btn-sm", showOcr && "!text-accent-400")}
              onClick={() => setShowOcr((v) => !v)}
            >
              {showOcr ? <Eye size={12} /> : <EyeOff size={12} />} OCR boxes
            </button>
          </div>

          {data && (
            <span className="ml-auto text-2xs text-ink-500 num">
              {data.render_width}×{data.render_height} @ {data.dpi} dpi ·{" "}
              {data.ocr_element_count} tokens ·{" "}
              {data.ocr_mean_confidence != null
                ? `${(data.ocr_mean_confidence * 100).toFixed(0)}% mean OCR`
                : "—"}
            </span>
          )}
        </div>

        {isLoading && <Spinner label={`Loading page ${page}…`} />}
        {error && (
          <div className="p-6">
            <ErrorNote error={error} onRetry={refetch} />
          </div>
        )}
        {data && (
          <PageCanvas
            imageUrl={api.pageImageUrl(documentId, page)}
            regions={data.regions}
            showRegions={showRegions}
            highlights={highlights}
            focus={focusBox}
            onRegionClick={setSelected}
            className="flex-1 min-h-0"
          />
        )}
      </div>

      <aside className="w-[300px] shrink-0 border-l border-ink-800 flex flex-col min-h-0">
        <div className="panel-head shrink-0">
          <h3 className="text-sm font-semibold">Layout regions</h3>
          <span className="text-2xs text-ink-500 num">
            {data?.regions?.length ?? 0}
          </span>
        </div>
        <div className="flex-1 overflow-y-auto min-h-0">
          {selected && (
            <div className="p-3 border-b border-ink-800 bg-ink-850">
              <div className="flex items-center gap-2">
                <span
                  className="w-2.5 h-2.5 rounded-sm shrink-0"
                  style={{ background: REGION_COLOR[selected.type] ?? "#64748b" }}
                />
                <span className="text-xs font-medium">{titleCase(selected.type)}</span>
                <span className="text-2xs text-ink-500 num ml-auto">
                  {(selected.confidence * 100).toFixed(0)}%
                </span>
              </div>
              <p className="text-2xs text-ink-400 mt-2 whitespace-pre-wrap max-h-48 overflow-y-auto">
                {selected.text || "(no text)"}
              </p>
              <p className="text-2xs text-ink-600 mt-2 font-mono">
                reading order {selected.reading_order} · {selected.detector}
              </p>
            </div>
          )}
          <ul className="divide-y divide-ink-850">
            {(data?.regions ?? []).map((r) => (
              <li key={r.id}>
                <button
                  className={clsx(
                    "w-full text-left px-3 py-2 hover:bg-ink-850 transition-colors",
                    selected?.id === r.id && "bg-ink-850",
                  )}
                  onClick={() => {
                    setSelected(r);
                    setFocusBox(r.bbox_relative);
                  }}
                >
                  <div className="flex items-center gap-2">
                    <span
                      className="w-2 h-2 rounded-sm shrink-0"
                      style={{ background: REGION_COLOR[r.type] ?? "#64748b" }}
                    />
                    <span className="text-2xs uppercase tracking-wide text-ink-400 font-medium">
                      {r.type.replace(/_/g, " ")}
                    </span>
                    <span className="text-2xs text-ink-600 num ml-auto">
                      #{r.reading_order}
                    </span>
                  </div>
                  <p className="text-2xs text-ink-300 mt-1 line-clamp-2">
                    {r.text.slice(0, 120) || "—"}
                  </p>
                </button>
              </li>
            ))}
          </ul>
        </div>
      </aside>
    </div>
  );
}
