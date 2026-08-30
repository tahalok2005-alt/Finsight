import { AlertTriangle, Loader2 } from "lucide-react";
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

export function Empty({ title, hint }: { title: string; hint?: string }) {
  return (
    <div className="text-center py-12">
      <p className="text-sm text-ink-300">{title}</p>
      {hint && <p className="text-xs text-ink-500 mt-1.5 max-w-md mx-auto">{hint}</p>}
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

export function Tabs<T extends string>({
  tabs, active, onChange,
}: {
  tabs: { id: T; label: string; count?: number }[];
  active: T;
  onChange: (id: T) => void;
}) {
  return (
    <nav className="flex gap-1 border-b border-ink-800 overflow-x-auto">
      {tabs.map((t) => (
        <button
          key={t.id}
          onClick={() => onChange(t.id)}
          className={clsx(
            "px-3 py-2 text-sm font-medium whitespace-nowrap border-b-2 -mb-px transition-colors",
            active === t.id
              ? "border-accent-500 text-ink-100"
              : "border-transparent text-ink-400 hover:text-ink-200",
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
