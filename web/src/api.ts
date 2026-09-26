// Typed client for the lineage service (/api/lineage) and the support agent (/api/agent).

export class ApiError extends Error {
  constructor(
    public status: number,
    public code: string,
    message: string,
    public requestId: string | null,
  ) {
    super(message);
  }
}

type Service = "lineage" | "agent";

async function request<T>(service: Service, path: string, init: RequestInit & { json?: unknown } = {}): Promise<T> {
  const { json, headers, ...rest } = init;
  const res = await fetch(`/api/${service}${path}`, {
    credentials: "include",
    ...rest,
    headers: { ...(json !== undefined ? { "Content-Type": "application/json" } : {}), ...headers },
    body: json !== undefined ? JSON.stringify(json) : rest.body,
  });
  const requestId = res.headers.get("x-request-id");
  const text = await res.text();
  let body: unknown = null;
  try {
    body = text ? JSON.parse(text) : null;
  } catch {
    body = null;
  }
  if (!res.ok) {
    const err = (body as { error?: { code?: string; message?: string; request_id?: string } } | null)?.error;
    const apiErr = new ApiError(
      res.status,
      err?.code ?? "http_error",
      err?.message ?? (res.status >= 500 ? "The service is unavailable" : `Request failed (${res.status})`),
      err?.request_id ?? requestId,
    );
    if (res.status === 401 && path !== "/v1/auth/login" && path !== "/v1/auth/me") {
      window.dispatchEvent(new CustomEvent("auth:expired"));
    }
    throw apiErr;
  }
  return body as T;
}

const get = <T>(s: Service, path: string) => request<T>(s, path);
const post = <T>(s: Service, path: string, json?: unknown) => request<T>(s, path, { method: "POST", json: json ?? {} });
const put = <T>(s: Service, path: string, json: unknown) => request<T>(s, path, { method: "PUT", json });
const del = <T>(s: Service, path: string) => request<T>(s, path, { method: "DELETE" });

function qs(params: Record<string, string | number | boolean | undefined | null>): string {
  const u = new URLSearchParams();
  for (const [k, v] of Object.entries(params)) if (v !== undefined && v !== null && v !== "") u.set(k, String(v));
  const s = u.toString();
  return s ? `?${s}` : "";
}

// ---------- types ----------

export type Me = { email: string; role: string };

export type RunSummary = {
  id: string;
  service: string;
  name: string;
  status: "running" | "ok" | "error" | string;
  started_at: string;
  ended_at: string | null;
  attrs: Record<string, unknown>;
  replay_of: string | null;
  replay_mode: "live" | "recorded" | null;
  pinned: boolean;
  step_count: number;
  duration_ms: number | null;
};

export type StepValue = { direction: "in" | "out"; path: string; fingerprint: string; display: string };
export type CandidateSet = { chosen: string; alternatives: string[]; reason: string | null };

export type RunStep = {
  id: string;
  seq: number;
  kind: "llm" | "tool" | "input" | "output" | string;
  name: string;
  status: string;
  input: unknown;
  output: unknown;
  error: string | null;
  started_at: string;
  duration_ms: number | null;
  values: StepValue[];
  candidates: CandidateSet[];
};

export type RunDetail = RunSummary & {
  input: unknown;
  output: unknown;
  error: string | null;
  replays: RunSummary[];
  steps: RunStep[];
};

export type LineageNode = {
  step_id: string;
  seq: number;
  kind: string;
  name: string;
  role: "origin" | "consumer" | "carrier";
  directions: string[];
  paths: string[];
};

export type Lineage = {
  run_id: string;
  fingerprint: string;
  display: string;
  found: boolean;
  origin: LineageNode | null;
  origin_kind: "tool_output" | "llm_output" | "run_input" | "unsourced" | null;
  candidates: CandidateSet[];
  nodes: LineageNode[];
  edges: { source: string; target: string }[];
  summary: string;
};

export type DiffSide = {
  step_id: string;
  seq: number;
  kind: string;
  name: string;
  status: string;
  input: unknown;
  output: unknown;
} | null;

export type DiffRow = { index: number; a: DiffSide; b: DiffSide; changes: string[] };
export type Diff = {
  a: RunSummary;
  b: RunSummary;
  identical: boolean;
  first_divergence: DiffRow | null;
  steps: DiffRow[];
};

export type ConversationMessage = { role: "customer" | "assistant"; content: string; run_id: string | null; created_at: string };
export type Conversation = {
  id: string;
  customer_id: string;
  customer_name: string;
  customer_email: string;
  ticket_id: string;
  created_at: string;
  messages: ConversationMessage[];
};
export type TurnResult = { conversation_id: string; ticket_id: string; reply: string; run_id: string; status: string };
export type Customer = { id: string; name: string; email: string };

export type Connector = {
  name: string;
  display_name: string;
  kind: "commerce" | "helpdesk" | "payments" | "shipping";
  connected: boolean;
  adapter: "mock" | "live" | "none";
  last_sync_at: string | null;
  records: Record<string, number>;
};
export type ConnectorTest = { name: string; ok: boolean; message: string; latency_ms?: number };

export type ApiKey = {
  id: string;
  name: string;
  prefix: string;
  created_at: string;
  last_used_at: string | null;
  revoked_at: string | null;
};
export type User = { id: string; email: string; role: string; created_at: string };

// ---------- endpoints ----------

export const api = {
  me: () => get<Me>("lineage", "/v1/auth/me"),
  login: (email: string, password: string) => post<Me>("lineage", "/v1/auth/login", { email, password }),
  logout: () => post<{ ok: boolean }>("lineage", "/v1/auth/logout"),

  runs: (p: { q?: string; status?: string; include_replays?: boolean; limit?: number; offset?: number }) =>
    get<{ total: number; runs: RunSummary[] }>("lineage", `/v1/runs${qs(p)}`),
  run: (id: string) => get<RunDetail>("lineage", `/v1/runs/${encodeURIComponent(id)}`),
  lineage: (id: string, fingerprint: string) =>
    get<Lineage>("lineage", `/v1/runs/${encodeURIComponent(id)}/lineage${qs({ fingerprint })}`),
  replay: (id: string, mode: "live" | "recorded") =>
    post<{ run_id: string; replay_of: string; mode: string }>("lineage", `/v1/runs/${encodeURIComponent(id)}/replay`, { mode }),
  diff: (a: string, b: string) => get<Diff>("lineage", `/v1/runs/${encodeURIComponent(a)}/diff/${encodeURIComponent(b)}`),
  pin: (id: string, pinned: boolean) => post<{ pinned: boolean }>("lineage", `/v1/runs/${encodeURIComponent(id)}/pin`, { pinned }),

  settings: () => get<{ retention_days: number }>("lineage", "/v1/settings"),
  saveSettings: (retention_days: number) => put<{ retention_days: number }>("lineage", "/v1/settings", { retention_days }),
  apiKeys: () => get<ApiKey[]>("lineage", "/v1/api-keys"),
  createApiKey: (name: string) => post<{ id: string; name: string; prefix: string; key: string }>("lineage", "/v1/api-keys", { name }),
  revokeApiKey: (id: string) => del<{ revoked: boolean }>("lineage", `/v1/api-keys/${encodeURIComponent(id)}`),
  users: () => get<User[]>("lineage", "/v1/users"),
  createUser: (email: string, password: string, role: string) => post<User>("lineage", "/v1/users", { email, password, role }),

  conversations: () => get<Conversation[]>("agent", "/v1/conversations"),
  startConversation: (customer_email: string, message: string) =>
    post<TurnResult>("agent", "/v1/conversations", { customer_email, message }),
  sendMessage: (id: string, message: string) =>
    post<TurnResult>("agent", `/v1/conversations/${encodeURIComponent(id)}/messages`, { message }),
  customers: (q: string) => get<Customer[]>("agent", `/v1/customers${qs({ q, limit: 8 })}`),
  connectors: () => get<Connector[]>("agent", "/v1/connectors"),
  testConnector: (name: string) => post<ConnectorTest>("agent", `/v1/connectors/${encodeURIComponent(name)}/test`),
  configureConnector: (name: string, settings: Record<string, string>) =>
    put<{ name: string; saved: boolean; enabled: boolean }>("agent", `/v1/connectors/${encodeURIComponent(name)}`, { settings }),
};

export function errorText(e: unknown): string {
  if (e instanceof ApiError) return e.message;
  if (e instanceof Error) return e.message;
  return String(e);
}
