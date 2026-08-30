import type {
  ChartDto, CommentaryDto, ComparisonRow, DocumentSummary, FinancialFact,
  GuidanceDto, Overview, PageDetail, PageInfo, Paged, ProcessingStatus,
  ReviewItem, TableDto, ValidationDto,
} from "../types";

export const API = "/api/v1";

export class ApiError extends Error {
  status: number;
  code: string;
  detail?: Record<string, unknown>;

  constructor(
    status: number,
    code: string,
    message: string,
    detail?: Record<string, unknown>,
  ) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.code = code;
    this.detail = detail;
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`${API}${path}`, {
    ...init,
    headers: {
      ...(init?.body && !(init.body instanceof FormData)
        ? { "Content-Type": "application/json" }
        : {}),
      ...init?.headers,
    },
  });
  if (!res.ok) {
    let code = "http_error";
    let message = `${res.status} ${res.statusText}`;
    let detail: Record<string, unknown> | undefined;
    try {
      const body = await res.json();
      if (body?.error) {
        code = body.error.code ?? code;
        message = body.error.message ?? message;
        detail = body.error.detail;
      }
    } catch {
      /* the response had no JSON body; keep the status text */
    }
    throw new ApiError(res.status, code, message, detail);
  }
  if (res.status === 204) return undefined as T;
  return (await res.json()) as T;
}

const qs = (params: Record<string, unknown>) => {
  const sp = new URLSearchParams();
  for (const [k, v] of Object.entries(params)) {
    if (v !== undefined && v !== null && v !== "") sp.set(k, String(v));
  }
  const s = sp.toString();
  return s ? `?${s}` : "";
};

export interface FactQuery {
  metric?: string;
  entity?: string;
  period?: string;
  nature?: string;
  kind?: string;
  band?: string;
  validation_status?: string;
  requires_review?: boolean;
  min_confidence?: number;
  sort?: string;
  order?: string;
  limit?: number;
  offset?: number;
}

export interface FactsResponse extends Paged<FinancialFact> {
  facets: { metrics: string[]; entities: string[]; periods: string[] };
}

export const api = {
  overview: () => request<Overview>("/overview"),
  system: () => request<Record<string, any>>("/system"),
  health: () => request<Record<string, any>>("/health"),
  metrics: () =>
    request<{ metrics: any[]; families: Record<string, string[]> }>("/metrics"),

  listDocuments: (p: { limit?: number; offset?: number; status?: string; search?: string } = {}) =>
    request<Paged<DocumentSummary>>(`/documents${qs(p)}`),
  getDocument: (id: string) => request<DocumentSummary>(`/documents/${id}`),
  getStatus: (id: string) => request<ProcessingStatus>(`/documents/${id}/status`),
  deleteDocument: (id: string) =>
    request<void>(`/documents/${id}`, { method: "DELETE" }),
  reprocess: (id: string, forceOcr = false) =>
    request<{ job_id: string }>(
      `/documents/${id}/reprocess${qs({ force_ocr: forceOcr })}`,
      { method: "POST" },
    ),

  upload: (file: File, onProgress?: (pct: number) => void) =>
    new Promise<DocumentSummary>((resolve, reject) => {
      const form = new FormData();
      form.append("file", file);
      const xhr = new XMLHttpRequest();
      xhr.open("POST", `${API}/documents`);
      xhr.upload.onprogress = (e) => {
        if (e.lengthComputable && onProgress) {
          onProgress(Math.round((e.loaded / e.total) * 100));
        }
      };
      xhr.onload = () => {
        try {
          const body = JSON.parse(xhr.responseText);
          if (xhr.status >= 200 && xhr.status < 300) resolve(body);
          else
            reject(
              new ApiError(
                xhr.status,
                body?.error?.code ?? "upload_failed",
                body?.error?.message ?? "Upload failed",
                body?.error?.detail,
              ),
            );
        } catch {
          reject(new ApiError(xhr.status, "upload_failed", "Upload failed"));
        }
      };
      xhr.onerror = () =>
        reject(new ApiError(0, "network_error", "Network error during upload"));
      xhr.send(form);
    }),

  pages: (id: string) => request<Paged<PageInfo>>(`/documents/${id}/pages`),
  page: (id: string, n: number, opts: { ocr?: boolean; regions?: boolean } = {}) =>
    request<PageDetail>(
      `/documents/${id}/pages/${n}${qs({
        include_ocr: opts.ocr ?? false,
        include_regions: opts.regions ?? true,
      })}`,
    ),
  pageImageUrl: (id: string, n: number) => `${API}/documents/${id}/pages/${n}/image`,
  thumbnailUrl: (id: string, n: number) =>
    `${API}/documents/${id}/pages/${n}/thumbnail`,

  financials: (id: string, q: FactQuery = {}) =>
    request<FactsResponse>(`/documents/${id}/financials${qs(q as any)}`),
  fact: (id: string, factId: string) =>
    request<FinancialFact & { validations: ValidationDto[]; review_history: any[] }>(
      `/documents/${id}/financials/${factId}`,
    ),
  provenance: (id: string, factId: string) =>
    request<Record<string, any>>(`/documents/${id}/provenance/${factId}`),

  tables: (id: string, page?: number) =>
    request<Paged<TableDto>>(`/documents/${id}/tables${qs({ page })}`),
  charts: (id: string) => request<Paged<ChartDto>>(`/documents/${id}/charts`),
  commentary: (id: string, q: { topic?: string; sentiment?: string } = {}) =>
    request<Paged<CommentaryDto> & { topics: { topic: string; count: number }[] }>(
      `/documents/${id}/commentary${qs(q)}`,
    ),
  guidance: (id: string) =>
    request<Paged<GuidanceDto> & { note: string }>(`/documents/${id}/guidance`),
  validation: (id: string, q: { status?: string; family?: string } = {}) =>
    request<
      Paged<ValidationDto> & {
        summary: Record<string, number>;
        by_family: Record<string, Record<string, number>>;
      }
    >(`/documents/${id}/validation${qs(q)}`),

  reviewQueue: (id: string, q: { limit?: number; offset?: number; include_resolved?: boolean } = {}) =>
    request<{ total: number; items: ReviewItem[] }>(`/documents/${id}/reviews${qs(q)}`),
  submitReview: (
    id: string,
    payload: {
      target_type?: string;
      target_id: string;
      action: string;
      reviewer?: string;
      note?: string;
      new_value?: Record<string, unknown>;
    },
  ) =>
    request<Record<string, any>>(`/documents/${id}/reviews`, {
      method: "POST",
      body: JSON.stringify({ target_type: "fact", reviewer: "analyst", ...payload }),
    }),
  reviewHistory: (id: string, targetId?: string) =>
    request<Paged<any>>(`/documents/${id}/reviews/history${qs({ target_id: targetId })}`),

  search: (q: string, documentId?: string) =>
    request<{ query: string; total: number; results: Record<string, any[]> }>(
      `/search${qs({ q, document_id: documentId })}`,
    ),

  compare: (p: {
    left: string;
    right: string;
    left_period?: string;
    right_period?: string;
    entity?: string;
    metrics?: string;
  }) =>
    request<{
      left: Record<string, any>;
      right: Record<string, any>;
      entities: string[];
      comparisons: ComparisonRow[];
      guidance_changes: Record<string, any>;
    }>(`/compare${qs(p)}`),

  exportUrl: (id: string, format: "json" | "csv" | "xlsx") =>
    `${API}/documents/${id}/export?format=${format}`,
};
