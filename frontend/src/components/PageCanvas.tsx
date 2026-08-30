import { useEffect, useMemo, useRef, useState } from "react";
import clsx from "clsx";
import { Loader2, Maximize2, Minus, Plus } from "lucide-react";
import type { BBox, RegionDto } from "../types";
import { REGION_COLOR } from "../utils/format";

export interface Highlight {
  /** Relative (0..1) coordinates, so the overlay is DPI-independent. */
  bbox: BBox;
  color?: string;
  label?: string;
  id?: string;
}

/**
 * Renders a page image with coordinate-accurate overlays.
 *
 * Every box is supplied in *relative* (0..1) space, which is why the overlay
 * stays aligned at any zoom level and does not need to know the render DPI.
 */
export function PageCanvas({
  imageUrl,
  regions,
  highlights,
  showRegions = false,
  focus,
  onRegionClick,
  className,
}: {
  imageUrl: string;
  regions?: RegionDto[];
  highlights?: Highlight[];
  showRegions?: boolean;
  /** A highlight to scroll into view and pulse. */
  focus?: BBox | null;
  onRegionClick?: (region: RegionDto) => void;
  className?: string;
}) {
  const [zoom, setZoom] = useState(1);
  const [loaded, setLoaded] = useState(false);
  const [failed, setFailed] = useState(false);
  const scrollRef = useRef<HTMLDivElement>(null);
  const imgWrapRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    setLoaded(false);
    setFailed(false);
  }, [imageUrl]);

  // bring the focused box into view once the image has laid out
  useEffect(() => {
    if (!focus || !loaded || !scrollRef.current || !imgWrapRef.current) return;
    const wrap = imgWrapRef.current;
    const scroller = scrollRef.current;
    const top = focus.y * wrap.clientHeight;
    const left = focus.x * wrap.clientWidth;
    scroller.scrollTo({
      top: Math.max(0, top - scroller.clientHeight / 2.5),
      left: Math.max(0, left - scroller.clientWidth / 2.5),
      behavior: "smooth",
    });
  }, [focus, loaded, zoom]);

  const overlays = useMemo<Highlight[]>(() => {
    const out: Highlight[] = [];
    if (showRegions && regions) {
      for (const r of regions) {
        out.push({
          id: r.id,
          bbox: r.bbox_relative,
          color: REGION_COLOR[r.type] ?? "#64748b",
          label: r.type,
        });
      }
    }
    if (highlights) out.push(...highlights);
    return out;
  }, [regions, highlights, showRegions]);

  return (
    <div className={clsx("flex flex-col min-h-0", className)}>
      <div className="flex items-center gap-1.5 px-2 py-1.5 border-b border-ink-800 bg-ink-900">
        <button
          className="btn-ghost btn-sm"
          onClick={() => setZoom((z) => Math.max(0.4, +(z - 0.25).toFixed(2)))}
          aria-label="Zoom out"
        >
          <Minus size={13} />
        </button>
        <span className="text-xs num text-ink-400 w-12 text-center">
          {Math.round(zoom * 100)}%
        </span>
        <button
          className="btn-ghost btn-sm"
          onClick={() => setZoom((z) => Math.min(4, +(z + 0.25).toFixed(2)))}
          aria-label="Zoom in"
        >
          <Plus size={13} />
        </button>
        <button
          className="btn-ghost btn-sm"
          onClick={() => setZoom(1)}
          aria-label="Reset zoom"
        >
          <Maximize2 size={13} /> Fit
        </button>
        <div className="ml-auto text-2xs text-ink-500">
          {overlays.length > 0 && `${overlays.length} overlay${overlays.length === 1 ? "" : "s"}`}
        </div>
      </div>

      <div ref={scrollRef} className="flex-1 overflow-auto bg-ink-950 p-4 min-h-0">
        <div
          ref={imgWrapRef}
          className="relative mx-auto shadow-2xl"
          style={{ width: `${zoom * 100}%`, maxWidth: zoom <= 1 ? "820px" : "none" }}
        >
          {!loaded && !failed && (
            <div className="absolute inset-0 flex items-center justify-center text-ink-500">
              <Loader2 size={20} className="animate-spin" />
            </div>
          )}
          <img
            src={imageUrl}
            alt="Rendered document page"
            className={clsx(
              "w-full block bg-white rounded transition-opacity",
              loaded ? "opacity-100" : "opacity-0",
            )}
            onLoad={() => setLoaded(true)}
            onError={() => setFailed(true)}
          />
          {failed && (
            <div className="p-8 text-center text-sm text-fail-400 bg-ink-900 rounded border border-fail-500/30">
              The rendered image for this page could not be loaded.
            </div>
          )}

          {loaded &&
            overlays.map((h, i) => {
              const isFocus =
                focus &&
                Math.abs(focus.x - h.bbox.x) < 1e-6 &&
                Math.abs(focus.y - h.bbox.y) < 1e-6;
              const region = regions?.find((r) => r.id === h.id);
              return (
                <div
                  key={h.id ?? `hl-${i}`}
                  onClick={region && onRegionClick ? () => onRegionClick(region) : undefined}
                  className={clsx(
                    "absolute group",
                    region && onRegionClick && "cursor-pointer",
                    isFocus && "animate-pulse",
                  )}
                  style={{
                    left: `${h.bbox.x * 100}%`,
                    top: `${h.bbox.y * 100}%`,
                    width: `${h.bbox.width * 100}%`,
                    height: `${h.bbox.height * 100}%`,
                    border: `2px solid ${h.color ?? "#3b82f6"}`,
                    background: `${h.color ?? "#3b82f6"}${isFocus ? "40" : "1a"}`,
                    boxShadow: isFocus ? `0 0 0 3px ${h.color ?? "#3b82f6"}55` : undefined,
                    borderRadius: 2,
                  }}
                >
                  {h.label && (
                    <span
                      className="absolute -top-[15px] left-0 text-[9px] px-1 rounded-sm
                                 font-medium text-white whitespace-nowrap opacity-0
                                 group-hover:opacity-100 transition-opacity"
                      style={{ background: h.color ?? "#3b82f6" }}
                    >
                      {h.label}
                    </span>
                  )}
                </div>
              );
            })}
        </div>
      </div>
    </div>
  );
}
