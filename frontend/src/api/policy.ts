import { useQuery } from "@tanstack/react-query";
import { api } from "./client";

/**
 * How this instance treats a visitor's documents, as the server reports it.
 *
 * On a public instance each browser has a private workspace and documents are
 * deleted after a fixed time; the dashboard says so wherever someone is about
 * to upload or is looking at their list, rather than leaving it to a page
 * nobody reads. The limits come from the server too, so the size check in the
 * browser can never disagree with the one that is actually enforced.
 */
export interface WorkspacePolicy {
  enabled: boolean;
  retention_hours: number | null;
  max_upload_mb: number;
  max_pages: number;
}

const FALLBACK: WorkspacePolicy = {
  enabled: false,
  retention_hours: null,
  max_upload_mb: 200,
  max_pages: 500,
};

export function useWorkspacePolicy(): WorkspacePolicy {
  const q = useQuery({
    queryKey: ["system"],
    queryFn: api.system,
    staleTime: 5 * 60_000,
  });
  return { ...FALLBACK, ...(q.data?.workspaces ?? {}) };
}

/** "24 hours", "3 days", "30 minutes". */
export function formatRetention(hours: number): string {
  if (hours >= 48 && hours % 24 === 0) return `${hours / 24} days`;
  if (hours >= 1) {
    const h = Math.round(hours * 10) / 10;
    return `${h} hour${h === 1 ? "" : "s"}`;
  }
  const m = Math.max(1, Math.round(hours * 60));
  return `${m} minute${m === 1 ? "" : "s"}`;
}

/** One sentence for the places a visitor uploads or reviews documents. */
export function privacyNote(p: WorkspacePolicy): string | null {
  if (!p.enabled) return null;
  const kept = p.retention_hours
    ? `deleted ${formatRetention(p.retention_hours)} after upload`
    : "kept until you delete them";
  return `Only this browser can see your documents. They are ${kept}.`;
}
