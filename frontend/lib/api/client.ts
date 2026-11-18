import { z } from "zod";

/** RFC 9457 problem details returned by the ClaimOS API. */
export const problemSchema = z.object({
  type: z.string(),
  title: z.string(),
  status: z.number().int(),
  code: z.string(),
  detail: z.string().optional(),
  request_id: z.string().optional(),
  errors: z
    .array(z.object({ loc: z.array(z.union([z.string(), z.number()])), msg: z.string(), type: z.string() }))
    .optional(),
});
export type Problem = z.infer<typeof problemSchema>;

export class ApiError extends Error {
  readonly status: number;
  readonly code: string;
  readonly requestId: string | undefined;
  readonly problem: Problem | undefined;

  constructor(status: number, code: string, message: string, problem?: Problem, requestId?: string) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.code = code;
    this.problem = problem;
    this.requestId = requestId ?? problem?.request_id;
  }

  /** First field-level message, falling back to the problem detail. */
  get userMessage(): string {
    const first = this.problem?.errors?.[0];
    return first ? `${String(first.loc.at(-1))}: ${first.msg}` : this.message;
  }
}

export const API_BASE = "/api/v1";
export const CSRF_COOKIES = ["__Host-claimos_csrf", "claimos_csrf"];

type Method = "GET" | "POST" | "PUT" | "PATCH" | "DELETE";

type RequestOptions = {
  body?: unknown;
  headers?: Record<string, string>;
  signal?: AbortSignal;
  /** HTTP statuses whose body should be parsed with the success schema (e.g. 503 readiness). */
  acceptStatuses?: number[];
};

export function readCsrfToken(cookieString: string = typeof document === "undefined" ? "" : document.cookie) {
  for (const part of cookieString.split(";")) {
    const [name, ...rest] = part.trim().split("=");
    if (name && CSRF_COOKIES.includes(name)) return decodeURIComponent(rest.join("="));
  }
  return undefined;
}

/**
 * Call the API and validate the response at the boundary. Never trust an unvalidated shape.
 * Unsafe methods carry the CSRF token (double-submit cookie) automatically.
 */
export async function apiRequest<T>(
  method: Method,
  path: string,
  schema: z.ZodType<T>,
  options: RequestOptions = {},
): Promise<T> {
  const { acceptStatuses = [], body, headers = {}, signal } = options;
  const finalHeaders: Record<string, string> = { Accept: "application/json", ...headers };
  if (body !== undefined) finalHeaders["Content-Type"] = "application/json";
  if (method !== "GET") {
    const csrf = readCsrfToken();
    if (csrf) finalHeaders["X-CSRF-Token"] = csrf;
  }

  let response: Response;
  try {
    response = await fetch(`${API_BASE}${path}`, {
      method,
      credentials: "same-origin",
      headers: finalHeaders,
      body: body === undefined ? undefined : JSON.stringify(body),
      signal,
    });
  } catch {
    throw new ApiError(0, "NETWORK_ERROR", "The API could not be reached");
  }

  const requestId = response.headers.get("x-request-id") ?? undefined;
  const payload: unknown = response.status === 204 ? undefined : await response.json().catch(() => undefined);

  if (response.ok || acceptStatuses.includes(response.status)) {
    const parsed = schema.safeParse(payload);
    if (!parsed.success) {
      throw new ApiError(response.status, "INVALID_RESPONSE", "The API returned an unexpected response shape", undefined, requestId);
    }
    return parsed.data;
  }

  const problem = problemSchema.safeParse(payload);
  if (problem.success) {
    throw new ApiError(response.status, problem.data.code, problem.data.detail ?? problem.data.title, problem.data, requestId);
  }
  throw new ApiError(response.status, "HTTP_ERROR", `Request failed with status ${response.status}`, undefined, requestId);
}

/** Multipart upload (no JSON content type; the browser sets the boundary). Carries the CSRF token. */
export async function apiUpload<T>(path: string, schema: z.ZodType<T>, form: FormData): Promise<T> {
  const headers: Record<string, string> = { Accept: "application/json" };
  const csrf = readCsrfToken();
  if (csrf) headers["X-CSRF-Token"] = csrf;
  let response: Response;
  try {
    response = await fetch(`${API_BASE}${path}`, { method: "POST", credentials: "same-origin", headers, body: form });
  } catch {
    throw new ApiError(0, "NETWORK_ERROR", "The upload could not reach the server");
  }
  const requestId = response.headers.get("x-request-id") ?? undefined;
  const payload: unknown = await response.json().catch(() => undefined);
  if (response.ok) {
    const parsed = schema.safeParse(payload);
    if (!parsed.success) throw new ApiError(response.status, "INVALID_RESPONSE", "Unexpected upload response", undefined, requestId);
    return parsed.data;
  }
  const problem = problemSchema.safeParse(payload);
  if (problem.success) throw new ApiError(response.status, problem.data.code, problem.data.detail ?? problem.data.title, problem.data, requestId);
  throw new ApiError(response.status, "HTTP_ERROR", `Upload failed with status ${response.status}`, undefined, requestId);
}

export function apiGet<T>(path: string, schema: z.ZodType<T>, options: Omit<RequestOptions, "body"> = {}) {
  return apiRequest("GET", path, schema, options);
}

export const noContent = z.undefined();
