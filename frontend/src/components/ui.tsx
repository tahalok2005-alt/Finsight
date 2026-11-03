import { AlertTriangle, Loader2, X } from "lucide-react";
import type { LucideIcon } from "lucide-react";
import { Link } from "react-router-dom";
import type { ReactNode } from "react";
import clsx from "clsx";
import {
  BAND_STYLE, STATUS_LABEL, VALIDATION_STYLE, statusStyle,
} from "../utils/format";
import type { ConfidenceBand, ValidationStatus } from "../types";

export function Panel({
  title, subtitle, actions, children, className, bodyClassName,
}: {
  title?: ReactNode;
  subtitle?: ReactNode;
  actions?: ReactNode;
  children: ReactNode;
  className?: string;
  bodyClassName?: string;
}) {
  return (
    <section className={clsx("panel", className)}>
      {(title || actions) && (
        <header className="panel-head">
          <div className="min-w-0">
            {title && (
              <h2 className="text-sm font-semibold text-ink-100 truncate">{title}</h2>
            )}
            {subtitle && (
              <p className="text-xs text-ink-400 mt-0.5 truncate">{subtitle}</p>
            )}
          </div>
          {actions && <div className="flex items-center gap-2 shrink-0">{actions}</div>}
        </header>
      )}
      <div className={bodyClassName ?? "p-4"}>{children}</div>
    </section>
  );
}

export function Spinner({ label }: { label?: string }) {
  return (
    <div className="flex items-center justify-center gap-2 py-10 text-ink-400 text-sm">
      <Loader2 size={16} className="animate-spin" />
      {label ?? "Loading…"}
    </div>
  );
}

export function ErrorNote({ error, onRetry }: { error: unknown; onRetry?: () => void }) {
  const message =
    error instanceof Error ? error.message : "Something went wrong.";
  return (
    <div className="flex items-start gap-3 p-4 rounded-lg bg-fail-900/40 border border-fail-500/30 text-sm">
      <AlertTriangle size={16} className="text-fail-400 mt-0.5 shrink-0" />
      <div className="min-w-0 flex-1">
        <p className="text-fail-400 font-medium">Request failed</p>
        <p className="text-ink-300 mt-1 break-words">{message}</p>
      </div>
      {onRetry && (
        <button className="btn-ghost btn-sm" onClick={onRetry}>
          Retry
        </button>
      )}
    </div>
  );
}

/**
 * An empty state that says why it is empty and what to do about it.
 *
 * "No data" is not an empty state; it is a shrug. Every one of these carries a
 * reason and, where there is one, the next action.
 */
export function Empty({
  title, hint, icon: Icon, action,
}: {
  title: string;
  hint?: string;
  icon?: LucideIcon;
  action?: { to: string; label: string } | { onClick: () => void; label: string };
}) {
  return (
    <div className="flex flex-col items-center justify-center text-center py-16 px-6">
      {Icon ? (
        <div className="mb-3 h-9 w-9 rounded-md bg-ink-850 border border-ink-750
                        flex items-center justify-center">
          <Icon size={17} className="text-ink-400" />
        </div>
      ) : null}
      <p className="text-sm font-medium text-ink-100">{title}</p>
      {hint ? (
        <p className="text-xs text-ink-300 mt-1.5 max-w-sm leading-relaxed">{hint}</p>
      ) : null}
      {action ? (
        <div className="mt-4">
          {"to" in action ? (
            <Link to={action.to} className="btn-primary">{action.label}</Link>
          ) : (
            <button className="btn-primary" onClick={action.onClick}>
              {action.label}
            </button>
          )}
        </div>
      ) : null}
    </div>
  );
}

/** A full-panel error with a retry, for a query that failed outright. */
export function ErrorState({ error, onRetry }:
  { error: unknown; onRetry?: () => void }) {
  const message = error instanceof Error ? error.message : "Something went wrong.";
  return (
    <div className="flex flex-col items-center justify-center text-center py-16 px-6">
      <div className="mb-3 h-9 w-9 rounded-md bg-fail-900 border border-fail-500/30
                      flex items-center justify-center">
        <AlertTriangle size={17} className="text-fail-400" />
      </div>
      <p className="text-sm font-medium text-ink-100">This did not load</p>
      <p className="text-xs text-ink-300 mt-1.5 max-w-md break-words">{message}</p>
      {onRetry ? (
        <button className="btn-ghost mt-4" onClick={onRetry}>Try again</button>
      ) : null}
    </div>
  );
}

/** A modal confirmation for an action that cannot be undone. */
export function ConfirmDialog({
  open, title, children, confirmLabel = "Confirm", cancelLabel = "Cancel",
  tone = "default", busy = false, onConfirm, onCancel,
}: {
  open: boolean;
  title: string;
  children?: ReactNode;
  confirmLabel?: string;
  cancelLabel?: string;
  tone?: "default" | "danger";
  busy?: boolean;
  onConfirm: () => void;
  onCancel: () => void;
}) {
  if (!open) return null;
  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-ink-980/75 px-4"
      onClick={onCancel}
      role="dialog"
      aria-modal="true"
    >
      <div
        className="w-full max-w-md panel shadow-pop"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="panel-head">
          <span className="text-sm font-semibold text-ink-50">{title}</span>
          <button className="btn-quiet" onClick={onCancel} aria-label="Close">
            <X size={14} />
          </button>
        </div>
        <div className="px-4 py-3.5 text-xs leading-relaxed">{children}</div>
        <div className="px-4 py-3 border-t border-ink-750 flex justify-end gap-2">
          <button className="btn-ghost" onClick={onCancel} disabled={busy}>
            {cancelLabel}
          </button>
          <button
            className={tone === "danger" ? "btn-danger" : "btn-primary"}
            onClick={onConfirm}
            disabled={busy}
          >
            {confirmLabel}
          </button>
        </div>
      </div>
    </div>
  );
}

export function ConfidenceChip({
  band, value, className,
}: { band: ConfidenceBand; value?: number; className?: string }) {
  return (
    <span className={clsx("chip", BAND_STYLE[band], className)}>
      {band.replace("_", " ")}
      {value !== undefined && (
        <span className="num opacity-70">{(value * 100).toFixed(0)}</span>
      )}
    </span>
  );
}

export function ValidationChip({ status }: { status: ValidationStatus }) {
  return (
    <span className={clsx("chip", VALIDATION_STYLE[status])}>
      {status.replace(/_/g, " ").toLowerCase()}
    </span>
  );
}

export function StatusChip({ status }: { status: string }) {
  return (
    <span className={clsx("chip", statusStyle(status))}>
      {STATUS_LABEL[status] ?? status}
    </span>
  );
}

export function Stat({
  label, value, hint, tone,
}: {
  label: string;
  value: ReactNode;
  hint?: ReactNode;
  tone?: "pass" | "warn" | "fail" | "accent";
}) {
  const toneClass = {
    pass: "text-pass-400",
    warn: "text-warn-400",
    fail: "text-fail-400",
    accent: "text-accent-400",
  }[tone ?? "accent"];
  return (
    <div className="panel p-4">
      <p className="text-2xs uppercase tracking-wider text-ink-400 font-semibold">
        {label}
      </p>
      <p className={clsx("text-2xl font-semibold mt-1.5 num", toneClass)}>{value}</p>
      {hint && <p className="text-xs text-ink-500 mt-1">{hint}</p>}
    </div>
  );
}

export function ProgressBar({
  value, tone = "accent", className,
}: { value: number; tone?: "accent" | "pass" | "warn" | "fail"; className?: string }) {
  const bar = {
    accent: "bg-accent-500", pass: "bg-pass-500",
    warn: "bg-warn-500", fail: "bg-fail-500",
  }[tone];
  return (
    <div className={clsx("h-1.5 bg-ink-800 rounded-full overflow-hidden", className)}>
      <div
        className={clsx("h-full transition-all duration-500", bar)}
        style={{ width: `${Math.max(0, Math.min(100, value * 100))}%` }}
      />
    </div>
  );
}

/**
 * A quiet segmented control for the sections inside one tab.
 *
 * The document screen used to carry eleven tabs in a single row, which asked
 * the reader to hold eleven possibilities in mind before clicking anything.
 * Four tabs carry the structure now, and the lesser divisions sit here, inside
 * the tab they belong to, where they are only a question once you are already
 * in that part of the document.
 */
export function SubNav<T extends string>({
  items, active, onChange,
}: {
  items: { id: T; label: string; count?: number }[];
  active: T;
  onChange: (id: T) => void;
}) {
  if (items.length < 2) return null;
  return (
    <div className="inline-flex items-center gap-0.5 p-0.5 bg-ink-800 rounded-md">
      {items.map((it) => (
        <button
          key={it.id}
          onClick={() => onChange(it.id)}
          className={clsx(
            "px-3 h-7 rounded text-xs font-medium whitespace-nowrap transition-colors",
            active === it.id
              ? "bg-ink-900 text-ink-100 shadow-panel"
              : "text-ink-400 hover:text-ink-200",
          )}
        >
          {it.label}
          {it.count !== undefined && it.count > 0 && (
            <span className="ml-1.5 text-2xs num text-ink-500">{it.count}</span>
          )}
        </button>
      ))}
    </div>
  );
}

export function Tabs<T extends string>({
  tabs, active, onChange,
}: {
  tabs: { id: T; label: string; count?: number }[];
  active: T;
  onChange: (id: T) => void;
}) {
  return (
    <nav className="flex gap-6 border-b border-ink-750 overflow-x-auto">
      {tabs.map((t) => (
        <button
          key={t.id}
          onClick={() => onChange(t.id)}
          className={clsx(
            "px-0.5 pb-3 pt-1 text-sm font-medium whitespace-nowrap border-b-2 -mb-px transition-colors",
            active === t.id
              ? "border-accent-600 text-ink-50"
              : "border-transparent text-ink-400 hover:text-ink-100",
          )}
        >
          {t.label}
          {t.count !== undefined && (
            <span className="ml-1.5 text-2xs num text-ink-500">{t.count}</span>
          )}
        </button>
      ))}
    </nav>
  );
}
