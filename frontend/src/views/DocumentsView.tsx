import { useMemo, useRef, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Link, useNavigate } from "react-router-dom";
import {
  AlertTriangle, ChevronRight, FileText, Pause, Play, RotateCcw, Search,
  Trash2, Upload, X,
} from "lucide-react";
import clsx from "clsx";
import { api } from "../api/client";
import type { DocumentSummary } from "../types";
import {
  dateTime, duration, isActivePhase, since, statusMeta,
} from "../utils/lifecycle";
import { ConfirmDialog, Empty, ErrorState, Spinner } from "../components/ui";
import { privacyNote, useWorkspacePolicy } from "../api/policy";

/**
 * The Documents area: everything the user has uploaded, and what it is doing.
 *
 * This is the screen that answers "where did my document go". A document
 * appears here the moment it is accepted, stays here while it processes, and
 * keeps its state when the user navigates away and comes back -- because the
 * state lives in the database, not in this component.
 *
 * The list polls while anything is active and stops when nothing is, so a queue
 * of finished documents costs nothing.
 */

type Filter = "all" | "active" | "done" | "stopped";

const FILTERS: { key: Filter; label: string }[] = [
  { key: "all", label: "All" },
  { key: "active", label: "In progress" },
  { key: "done", label: "Completed" },
  { key: "stopped", label: "Stopped" },
];

function ProgressBar({ done, total, tone = "run" }:
  { done: number; total: number; tone?: "run" | "hold" }) {
  const pct = total > 0 ? Math.min(100, Math.round((done / total) * 100)) : 0;
  return (
    <div className="flex items-center gap-2 min-w-[130px]">
      <div className="h-1 flex-1 bg-ink-800 rounded-full overflow-hidden">
        <div
          className={clsx("h-full rounded-full transition-[width] duration-500",
            tone === "hold" ? "bg-hold-500" : "bg-run-500")}
          style={{ width: `${pct}%` }}
        />
      </div>
      <span className="text-2xs num text-ink-300 w-8 text-right">{pct}%</span>
    </div>
  );
}

function RowActions({ doc, onDeleted }:
  { doc: DocumentSummary; onDeleted: (id: string) => void }) {
  const qc = useQueryClient();
  const [confirming, setConfirming] = useState(false);
  const refresh = () => qc.invalidateQueries({ queryKey: ["documents"] });

  const pause = useMutation({ mutationFn: () => api.pauseDocument(doc.id), onSuccess: refresh });
  const resume = useMutation({ mutationFn: () => api.resumeDocument(doc.id), onSuccess: refresh });
  const cancel = useMutation({ mutationFn: () => api.cancelDocument(doc.id), onSuccess: refresh });
  const requeue = useMutation({ mutationFn: () => api.reprocess(doc.id), onSuccess: refresh });
  const remove = useMutation({
    mutationFn: () => api.deleteDocument(doc.id),
    onSuccess: () => { setConfirming(false); onDeleted(doc.id); refresh(); },
  });

  const busy = pause.isPending || resume.isPending || cancel.isPending
    || requeue.isPending || remove.isPending;
  const err = [pause, resume, cancel, requeue, remove].find((m) => m.error)?.error;

  return (
    <>
      <div className="flex items-center justify-end gap-1">
        {err ? (
          <span className="text-2xs text-fail-400 mr-1 max-w-[190px] truncate"
                title={(err as Error).message}>
            {(err as Error).message}
          </span>
        ) : null}

        {doc.can_pause ? (
          <button className="btn-quiet" disabled={busy} title="Pause after the current page"
                  onClick={() => pause.mutate()}>
            <Pause size={12} /> Pause
          </button>
        ) : null}

        {doc.can_resume ? (
          <button className="btn-ghost" disabled={busy} title="Continue from where it stopped"
                  onClick={() => resume.mutate()}>
            <Play size={12} /> Resume
          </button>
        ) : null}

        {doc.can_cancel ? (
          <button className="btn-quiet" disabled={busy} title="Stop processing; no results are kept"
                  onClick={() => cancel.mutate()}>
            <X size={12} /> Cancel
          </button>
        ) : null}

        {doc.can_requeue ? (
          <button className="btn-ghost" disabled={busy} title="Process this document again"
                  onClick={() => requeue.mutate()}>
            <RotateCcw size={12} /> Re-queue
          </button>
        ) : null}

        <button className="btn-quiet text-ink-400 hover:text-fail-400"
                disabled={busy} title="Delete this document and its data"
                onClick={() => setConfirming(true)}>
          <Trash2 size={12} />
        </button>
      </div>

      <ConfirmDialog
        open={confirming}
        title="Delete this document?"
        tone="danger"
        confirmLabel={remove.isPending ? "Deleting…" : "Delete permanently"}
        busy={remove.isPending}
        onCancel={() => setConfirming(false)}
        onConfirm={() => remove.mutate()}
      >
        <p className="text-ink-200">
          <span className="text-ink-50 font-medium">{doc.original_filename}</span>{" "}
          and everything derived from it will be removed: extracted figures,
          tables, entities, insights, anomalies, review items, the rendered page
          images and the uploaded file itself.
        </p>
        {isActivePhase(doc.status) ? (
          <p className="mt-2 text-warn-400 flex items-start gap-1.5">
            <AlertTriangle size={13} className="mt-px shrink-0" />
            This document is still processing. It will be stopped first, which
            can take until the current page finishes.
          </p>
        ) : null}
        <p className="mt-2 text-ink-400">This cannot be undone.</p>
      </ConfirmDialog>
    </>
  );
}

function DocumentRow({ doc, onDeleted, selected, onToggle }:
  {
    doc: DocumentSummary;
    onDeleted: (id: string) => void;
    selected: boolean;
    onToggle: (id: string, shiftKey: boolean) => void;
  }) {
  const meta = statusMeta(doc.status);
  const navigate = useNavigate();
  const active = meta.phase === "active";
  const done = doc.pages_processed ?? 0;
  const total = doc.page_count ?? 0;

  return (
    <tr
      className="group cursor-pointer"
      onClick={() => navigate(`/documents/${doc.id}`)}
    >
      {/* The checkbox stops the click from opening the document: selecting and
          navigating are different intentions and must not be the same gesture. */}
      <td className="w-8 pr-0" onClick={(e) => e.stopPropagation()}>
        <input
          type="checkbox"
          checked={selected}
          aria-label={`Select ${doc.original_filename}`}
          onChange={(e) =>
            onToggle(doc.id, (e.nativeEvent as MouseEvent).shiftKey)}
          className="accent-accent-500 cursor-pointer align-middle"
        />
      </td>
      <td className="max-w-0">
        <div className="flex items-center gap-2.5 min-w-0">
          <FileText size={14} className="text-ink-500 shrink-0" />
          <div className="min-w-0">
            <div className="truncate text-ink-100 font-medium"
                 title={doc.original_filename}>
              {doc.original_filename}
            </div>
            <div className="text-2xs text-ink-400 truncate">
              {total ? `${total} pages` : "—"}
              {doc.size_bytes
                ? ` · ${(doc.size_bytes / 1_048_576).toFixed(1)} MB` : ""}
              {doc.company_name ? ` · ${doc.company_name}` : ""}
            </div>
          </div>
        </div>
      </td>

      <td className="max-w-0">
        <span className={meta.chip} title={meta.hint}>{meta.label}</span>
        {doc.progress_note ? (
          <div className="text-2xs text-ink-400 mt-0.5 truncate"
               title={doc.progress_note}>
            {doc.progress_note}
          </div>
        ) : null}
      </td>

      <td className="overflow-hidden">
        {active || meta.phase === "paused" ? (
          <ProgressBar done={done} total={total}
                       tone={meta.phase === "paused" ? "hold" : "run"} />
        ) : meta.phase === "done" ? (
          <span className="block text-2xs text-ink-300 num whitespace-nowrap
                           overflow-hidden text-ellipsis">
            {doc.fact_count ?? 0} figures · {doc.table_count ?? 0} tables
          </span>
        ) : (
          <span className="text-2xs text-ink-500">—</span>
        )}
      </td>

      <td className="text-2xs text-ink-300 num whitespace-nowrap">
        {meta.phase === "done" ? duration(doc.processing_ms) : "—"}
      </td>

      <td className="text-2xs text-ink-300 whitespace-nowrap"
          title={dateTime(doc.created_at)}>
        {since(doc.created_at)}
      </td>

      <td className="text-right" onClick={(e) => e.stopPropagation()}>
        <RowActions doc={doc} onDeleted={onDeleted} />
      </td>

      <td className="text-ink-600 group-hover:text-ink-300">
        <ChevronRight size={14} />
      </td>
    </tr>
  );
}

export function DocumentsView() {
  const [filter, setFilter] = useState<Filter>("all");
  const [search, setSearch] = useState("");
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [confirmingBulk, setConfirmingBulk] = useState(false);
  const [bulkBusy, setBulkBusy] = useState(false);
  const [bulkNote, setBulkNote] = useState<string | null>(null);
  const lastToggled = useRef<string | null>(null);
  const qc = useQueryClient();
  const note = privacyNote(useWorkspacePolicy());

  const q = useQuery({
    queryKey: ["documents", search],
    queryFn: () => api.listDocuments({ limit: 100, search: search || undefined }),
    // Poll only while something is moving. A settled queue makes no requests.
    refetchInterval: (query) => {
      const items = query.state.data?.items ?? [];
      return items.some((d) => isActivePhase(d.status)) ? 2500 : false;
    },
  });

  const items = q.data?.items ?? [];
  const counts = useMemo(() => {
    const c = { all: items.length, active: 0, done: 0, stopped: 0 };
    for (const d of items) {
      const p = statusMeta(d.status).phase;
      if (p === "active" || p === "paused" || p === "pending") c.active += 1;
      else if (p === "done") c.done += 1;
      else c.stopped += 1;
    }
    return c;
  }, [items]);

  const shown = useMemo(() => items.filter((d) => {
    const p = statusMeta(d.status).phase;
    if (filter === "active") return p === "active" || p === "paused" || p === "pending";
    if (filter === "done") return p === "done";
    if (filter === "stopped") return p === "stopped" || p === "failed";
    return true;
  }), [items, filter]);

  const onDeleted = (id: string) => {
    qc.setQueryData(["documents", search], (old: typeof q.data) =>
      old ? { ...old, items: old.items.filter((d) => d.id !== id) } : old);
    setSelected((prev) => {
      const next = new Set(prev);
      next.delete(id);
      return next;
    });
  };

  // Selection is held as ids rather than indices so that a row disappearing --
  // because a poll refreshed the list, or a filter changed -- can never shift
  // the selection onto a different document.
  const shownIds = shown.map((d) => d.id);
  const visibleSelected = shownIds.filter((id) => selected.has(id));
  const allVisibleSelected =
    shownIds.length > 0 && visibleSelected.length === shownIds.length;

  const toggleOne = (id: string, shiftKey: boolean) => {
    setSelected((prev) => {
      const next = new Set(prev);
      if (shiftKey && lastToggled.current) {
        const from = shownIds.indexOf(lastToggled.current);
        const to = shownIds.indexOf(id);
        if (from !== -1 && to !== -1) {
          const [lo, hi] = from < to ? [from, to] : [to, from];
          const adding = !next.has(id);
          for (const rid of shownIds.slice(lo, hi + 1)) {
            if (adding) next.add(rid);
            else next.delete(rid);
          }
          lastToggled.current = id;
          return next;
        }
      }
      if (next.has(id)) next.delete(id);
      else next.add(id);
      lastToggled.current = id;
      return next;
    });
  };

  const toggleAllVisible = () => {
    setSelected((prev) => {
      const next = new Set(prev);
      if (allVisibleSelected) shownIds.forEach((id) => next.delete(id));
      else shownIds.forEach((id) => next.add(id));
      return next;
    });
  };

  return (
    <div className="h-full flex flex-col">
      <header className="px-5 py-3.5 border-b border-ink-750 flex items-center
                         justify-between gap-4 shrink-0">
        <div>
          <h1 className="page-title">Documents</h1>
          <p className="page-sub">
            {counts.all} total
            {counts.active ? ` · ${counts.active} in progress` : ""}
            {note ? ` · ${note}` : ""}
          </p>
        </div>
        <div className="flex items-center gap-2">
          <div className="relative">
            <Search size={13}
                    className="absolute left-2 top-1/2 -translate-y-1/2 text-ink-500" />
            <input
              className="input pl-7 w-56"
              placeholder="Search by file name…"
              value={search}
              onChange={(e) => setSearch(e.target.value)}
            />
          </div>
          <Link to="/upload" className="btn-primary">
            <Upload size={13} /> Upload
          </Link>
        </div>
      </header>

      {/* The bulk bar replaces the filter row while a selection exists, so the
          destructive action and the count it applies to are never separated. */}
      {visibleSelected.length > 0 ? (
        <div className="px-5 py-2 border-b border-ink-750 flex items-center
                        gap-3 shrink-0 bg-ink-850">
          <span className="text-xs text-ink-100 num">
            {visibleSelected.length} selected
          </span>
          <button className="btn-ghost btn-sm"
                  onClick={() => { setSelected(new Set()); setBulkNote(null); }}>
            Clear selection
          </button>
          <div className="ml-auto flex items-center gap-2">
            {bulkNote ? (
              <span className="text-2xs text-ink-300">{bulkNote}</span>
            ) : null}
            {confirmingBulk ? (
              <>
                <span className="text-xs text-ink-200">
                  Permanently delete {visibleSelected.length}{" "}
                  {visibleSelected.length === 1 ? "document" : "documents"} and
                  every figure, table and page extracted from{" "}
                  {visibleSelected.length === 1 ? "it" : "them"}?
                </span>
                <button className="btn-ghost btn-sm"
                        disabled={bulkBusy}
                        onClick={() => setConfirmingBulk(false)}>
                  Cancel
                </button>
                <button
                  className="btn-sm text-fail-400 font-medium disabled:opacity-50"
                  disabled={bulkBusy}
                  onClick={async () => {
                    setBulkBusy(true);
                    setBulkNote(null);
                    try {
                      // Only what is both selected and currently visible: a
                      // document filtered out of view is not on screen to have
                      // been chosen, and must not be swept up.
                      const res = await api.bulkDelete(visibleSelected);
                      const gone = new Set([...res.deleted, ...res.missing]);
                      qc.setQueryData(["documents", search],
                        (old: typeof q.data) => old
                          ? { ...old, items: old.items.filter((d) => !gone.has(d.id)) }
                          : old);
                      setSelected((prev) => {
                        const next = new Set(prev);
                        gone.forEach((id) => next.delete(id));
                        return next;
                      });
                      const stuck = res.busy.length + res.failed.length;
                      setBulkNote(stuck
                        ? `${res.deleted.length} deleted · ${stuck} still processing, try again shortly`
                        : null);
                      qc.invalidateQueries({ queryKey: ["overview"] });
                    } catch (e) {
                      setBulkNote(e instanceof Error ? e.message : "Delete failed");
                    } finally {
                      setBulkBusy(false);
                      setConfirmingBulk(false);
                    }
                  }}
                >
                  {bulkBusy ? "Deleting…" : `Delete ${visibleSelected.length}`}
                </button>
              </>
            ) : (
              <button className="btn-ghost btn-sm text-fail-400"
                      onClick={() => setConfirmingBulk(true)}>
                <Trash2 size={13} /> Delete selected
              </button>
            )}
          </div>
        </div>
      ) : (
      <div className="px-5 py-2 border-b border-ink-750 flex items-center gap-1 shrink-0">
        {FILTERS.map((f) => (
          <button
            key={f.key}
            onClick={() => setFilter(f.key)}
            className={clsx(
              "h-6 px-2.5 rounded text-2xs font-medium transition-colors",
              filter === f.key
                ? "bg-ink-800 text-ink-100"
                : "text-ink-400 hover:text-ink-200 hover:bg-ink-850",
            )}
          >
            {f.label}
            <span className="ml-1.5 text-ink-500 num">{counts[f.key]}</span>
          </button>
        ))}
      </div>
      )}

      <div className="flex-1 overflow-auto">
        {q.isLoading ? (
          <Spinner label="Loading documents" />
        ) : q.error ? (
          <ErrorState error={q.error as Error}
                      onRetry={() => q.refetch()} />
        ) : shown.length === 0 ? (
          <Empty
            icon={FileText}
            title={items.length === 0 ? "No documents yet"
              : `Nothing ${filter === "active" ? "in progress" : filter}`}
            hint={items.length === 0
              ? "Upload a financial PDF to extract its figures, tables and periods. Processing runs in the background — you can leave this page and come back."
              : "Change the filter above to see your other documents."}
            action={items.length === 0
              ? { to: "/upload", label: "Upload a document" } : undefined}
          />
        ) : (
          <table className="grid-table table-fixed">
            {/* Fixed widths: without them a long status note stole the space
                from the file name, and every row read as "Paytm…". */}
            <thead>
              <tr>
                <th className="w-8 pr-0">
                  <input
                    type="checkbox"
                    aria-label="Select all visible documents"
                    checked={allVisibleSelected}
                    ref={(el) => {
                      if (el) {
                        el.indeterminate =
                          visibleSelected.length > 0 && !allVisibleSelected;
                      }
                    }}
                    onChange={toggleAllVisible}
                    className="accent-accent-500 cursor-pointer align-middle"
                  />
                </th>
                <th className="w-[32%]">Document</th>
                <th className="w-[16%]">Status</th>
                <th className="w-[19%]">Progress</th>
                <th className="w-[8%]">Duration</th>
                <th className="w-[9%]">Uploaded</th>
                <th className="text-right w-[12%]">Actions</th>
                <th className="w-8" />
              </tr>
            </thead>
            <tbody>
              {shown.map((d) => (
                <DocumentRow key={d.id} doc={d} onDeleted={onDeleted}
                             selected={selected.has(d.id)}
                             onToggle={toggleOne} />
              ))}
            </tbody>
          </table>
        )}
      </div>
    </div>
  );
}
