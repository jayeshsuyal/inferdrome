import type {
  Comparison,
  RejectedRun,
  RunDetail,
  RunIndex,
  RunSummary,
  RunsPageResponse,
} from "./types";

const API_ROOT = "/api/v1";
const RUN_PAGE_LIMIT = 200;
const MAX_RUN_PAGES = 5;

export class ApiError extends Error {
  readonly status: number;
  readonly requestId: string | null;

  constructor(message: string, status: number, requestId: string | null = null) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.requestId = requestId;
  }
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function protocolError(message: string): ApiError {
  return new ApiError(`Inferdrome returned an invalid dashboard response: ${message}`, 502);
}

async function readError(response: Response): Promise<string> {
  const fallback = `Request failed with status ${response.status}`;
  try {
    const payload: unknown = await response.json();
    if (!isRecord(payload)) return fallback;
    const detail = payload.detail;
    const message = payload.message;
    if (typeof detail === "string" && detail.trim()) return detail;
    if (typeof message === "string" && message.trim()) return message;
  } catch {
    // A non-JSON failure still gets a bounded, useful message.
  }
  return fallback;
}

async function fetchJson(path: string, signal?: AbortSignal): Promise<unknown> {
  let response: Response;
  try {
    response = await fetch(`${API_ROOT}${path}`, {
      method: "GET",
      headers: { Accept: "application/json" },
      signal,
    });
  } catch (error) {
    if (error instanceof DOMException && error.name === "AbortError") throw error;
    throw new ApiError(
      "The local Inferdrome dashboard service is unavailable. Start it and try again.",
      0,
    );
  }

  if (!response.ok) {
    throw new ApiError(
      await readError(response),
      response.status,
      response.headers.get("x-request-id"),
    );
  }

  try {
    return await response.json();
  } catch {
    throw protocolError("the body is not valid JSON");
  }
}

function parseRuns(payload: unknown): RunsPageResponse {
  if (!isRecord(payload)) throw protocolError("the run index is not an object");
  if (payload.projection_version !== "inferdrome.dashboard.v1") {
    throw protocolError("the run index projection version is unsupported");
  }
  if (!Array.isArray(payload.runs)) throw protocolError("runs must be an array");
  if (!Array.isArray(payload.rejected)) throw protocolError("rejected must be an array");
  if (typeof payload.generated_at !== "string") {
    throw protocolError("generated_at must be a string");
  }
  if (!isRecord(payload.page)) throw protocolError("page metadata is missing");
  const { has_more: hasMore, limit, next_cursor: nextCursor, returned, total } = payload.page;
  if (
    typeof limit !== "number" ||
    !Number.isInteger(limit) ||
    limit < 1 ||
    limit > RUN_PAGE_LIMIT ||
    typeof returned !== "number" ||
    !Number.isInteger(returned) ||
    returned !== payload.runs.length + payload.rejected.length ||
    typeof total !== "number" ||
    !Number.isInteger(total) ||
    total < returned ||
    typeof hasMore !== "boolean" ||
    (nextCursor !== null && (
      typeof nextCursor !== "string" ||
      nextCursor.length === 0 ||
      nextCursor.length > 128
    )) ||
    hasMore !== (nextCursor !== null)
  ) {
    throw protocolError("page metadata is invalid");
  }
  return payload as unknown as RunsPageResponse;
}

function parseRunDetail(payload: unknown): RunDetail {
  if (!isRecord(payload) || payload.projection_version !== "inferdrome.dashboard.v1") {
    throw protocolError("the run detail projection version is unsupported");
  }
  if (!isRecord(payload.summary) || typeof payload.summary.run_id !== "string") {
    throw protocolError("run detail is missing summary.run_id");
  }
  if (!isRecord(payload.verification) || !isRecord(payload.execution)) {
    throw protocolError("run detail is missing verification or execution");
  }
  for (const key of ["measurements", "distributions", "context", "environment", "artifacts", "unavailable"] as const) {
    if (!Array.isArray(payload[key])) throw protocolError(`${key} must be an array`);
  }
  return payload as unknown as RunDetail;
}

function parseComparison(payload: unknown): Comparison {
  if (!isRecord(payload)) throw protocolError("comparison is not an object");
  if (payload.projection_version !== "inferdrome.dashboard.v1") {
    throw protocolError("the comparison projection version is unsupported");
  }
  if (
    payload.status !== "COMPARABLE" &&
    payload.status !== "COMPARABLE_WITH_CONTEXT_CHANGES" &&
    payload.status !== "INCOMPARABLE"
  ) {
    throw protocolError("comparison status is unknown");
  }
  if (typeof payload.baseline_run_id !== "string" || typeof payload.candidate_run_id !== "string") {
    throw protocolError("comparison is missing run identities");
  }
  for (const key of ["reasons", "metric_deltas", "context_changes"] as const) {
    if (!Array.isArray(payload[key])) throw protocolError(`${key} must be an array`);
  }
  if (payload.directionality !== "NEUTRAL") throw protocolError("comparison directionality must be neutral");
  return payload as unknown as Comparison;
}

export const api = {
  async listRuns(signal?: AbortSignal): Promise<RunIndex> {
    const runs: RunSummary[] = [];
    const rejected: RejectedRun[] = [];
    let cursor: string | null = null;
    let generatedAt: string | null = null;
    let expectedTotal: number | null = null;
    const seenRunIds = new Set<string>();
    const seenRejectedEntries = new Set<string>();
    const seenCursors = new Set<string>();

    for (let pageNumber = 0; pageNumber < MAX_RUN_PAGES; pageNumber += 1) {
      const query = new URLSearchParams({ limit: String(RUN_PAGE_LIMIT) });
      if (cursor) query.set("cursor", cursor);
      const page = parseRuns(await fetchJson(`/runs?${query.toString()}`, signal));
      generatedAt ??= page.generated_at;
      expectedTotal ??= page.page.total;
      if (page.page.total !== expectedTotal) {
        throw protocolError("the run index changed while it was being paged");
      }
      for (const run of page.runs) {
        if (seenRunIds.has(run.run_id)) {
          throw protocolError("the paged run index repeated a run");
        }
        seenRunIds.add(run.run_id);
        runs.push(run);
      }
      for (const entry of page.rejected) {
        const identity = `${entry.entry}\u0000${entry.code}`;
        if (seenRejectedEntries.has(identity)) {
          throw protocolError("the paged run index repeated a rejection");
        }
        seenRejectedEntries.add(identity);
        rejected.push(entry);
      }
      cursor = page.page.next_cursor;
      if (!cursor) {
        if (runs.length + rejected.length !== expectedTotal) {
          throw protocolError("the paged run index is incomplete");
        }
        return {
          projection_version: "inferdrome.dashboard.v1",
          generated_at: generatedAt,
          runs,
          rejected,
        };
      }
      if (seenCursors.has(cursor)) {
        throw protocolError("the run index repeated a pagination cursor");
      }
      seenCursors.add(cursor);
    }
    throw protocolError("the run index exceeded its bounded page count");
  },

  async getRun(runId: string, signal?: AbortSignal): Promise<RunDetail> {
    return parseRunDetail(await fetchJson(`/runs/${encodeURIComponent(runId)}`, signal));
  },

  async compareRuns(
    baselineRunId: string,
    candidateRunId: string,
    signal?: AbortSignal,
  ): Promise<Comparison> {
    const query = new URLSearchParams({
      baseline_run_id: baselineRunId,
      candidate_run_id: candidateRunId,
    });
    return parseComparison(await fetchJson(`/compare?${query.toString()}`, signal));
  },
};
