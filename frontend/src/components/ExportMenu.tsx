/**
 * Export menu.
 *
 * It replaces a CSS-only `group-hover` dropdown that could not actually be
 * used: the menu sat 4px below the button, so the pointer left the button
 * before it reached the menu, the hover broke, and the menu vanished. The
 * backend had always served the file correctly — the export was unreachable,
 * not broken.
 *
 * This opens on click, stays open until dismissed, and closes on Escape or an
 * outside click, so the choice can be made at the reader's pace.
 */
import { useEffect, useRef, useState } from "react";
import { Download, Loader2 } from "lucide-react";

import { api } from "../api/client";

type Format = "csv" | "xlsx" | "json";

const FORMATS: { id: Format; label: string; hint: string }[] = [
  { id: "csv", label: "CSV", hint: "One row per figure, with its period and source" },
  { id: "xlsx", label: "Excel", hint: "Figures, tables, validation and commentary as sheets" },
  { id: "json", label: "JSON", hint: "The full structured record, including provenance" },
];

export function ExportMenu({ documentId }: { documentId: string }) {
  const [open, setOpen] = useState(false);
  const [busy, setBusy] = useState<Format | null>(null);
  const [error, setError] = useState<string | null>(null);
  const box = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!open) return;
    const onDown = (e: MouseEvent) => {
      if (box.current && !box.current.contains(e.target as Node)) setOpen(false);
    };
    const onKey = (e: KeyboardEvent) => { if (e.key === "Escape") setOpen(false); };
    document.addEventListener("mousedown", onDown);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("mousedown", onDown);
      document.removeEventListener("keydown", onKey);
    };
  }, [open]);

  // Fetching rather than following a link means a failure is visible as a
  // message instead of a blank tab, and the filename the server chose is kept.
  async function download(format: Format) {
    setBusy(format);
    setError(null);
    try {
      const { blob, filename } = await api.exportFile(documentId, format);
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = filename;
      document.body.appendChild(a);
      a.click();
      a.remove();
      // Revoke on the next tick; Safari needs the URL alive during the click.
      setTimeout(() => URL.revokeObjectURL(url), 10_000);
      setOpen(false);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Export failed");
    } finally {
      setBusy(null);
    }
  }

  return (
    <div className="relative" ref={box}>
      <button
        className="btn-ghost btn-sm"
        aria-haspopup="menu"
        aria-expanded={open}
        onClick={() => setOpen((v) => !v)}
      >
        <Download size={13} /> Export
      </button>

      {open && (
        <div role="menu"
             className="absolute right-0 top-full mt-1 z-30 panel py-1 min-w-[268px]">
          {FORMATS.map((f) => (
            <button
              key={f.id}
              role="menuitem"
              disabled={busy !== null}
              onClick={() => download(f.id)}
              className="w-full text-left px-3 py-2 hover:bg-ink-800 disabled:opacity-50
                         flex items-start gap-2"
            >
              <span className="w-11 shrink-0 text-xs font-medium text-ink-100">
                {busy === f.id
                  ? <Loader2 size={12} className="animate-spin inline" />
                  : f.label}
              </span>
              <span className="text-2xs text-ink-300 leading-snug">{f.hint}</span>
            </button>
          ))}
          {error && (
            <div className="px-3 py-2 text-2xs text-fail-400 border-t border-ink-800">
              {error}
            </div>
          )}
        </div>
      )}
    </div>
  );
}
