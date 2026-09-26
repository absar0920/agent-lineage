import { createContext, useCallback, useContext, useEffect, useMemo, useState, type ReactNode } from "react";
import { useQuery } from "@tanstack/react-query";
import { ApiError, api } from "../api";

// ---------- roles ----------

/** Admins can change things; viewers can only investigate. The server enforces this too. */
export function useIsAdmin(): boolean {
  const me = useQuery({ queryKey: ["me"], queryFn: api.me, retry: false, staleTime: 60_000 });
  return me.data?.role === "admin";
}

export function ReadOnlyNote({ children }: { children: ReactNode }) {
  return <div className="readonly-note muted small">🔒 {children}</div>;
}

// ---------- formatting ----------

export function relTime(iso: string | null | undefined): string {
  if (!iso) return "never";
  const diff = (Date.now() - new Date(iso).getTime()) / 1000;
  const abs = Math.abs(diff);
  const fmt = (n: number, u: string) => `${Math.round(n)} ${u}${Math.round(n) === 1 ? "" : "s"}`;
  const s =
    abs < 45 ? "just now"
    : abs < 3600 ? fmt(abs / 60, "min")
    : abs < 86400 ? fmt(abs / 3600, "hour")
    : fmt(abs / 86400, "day");
  if (s === "just now") return s;
  return diff >= 0 ? `${s} ago` : `in ${s}`;
}

export function dateTime(iso: string | null | undefined): string {
  if (!iso) return "—";
  return new Date(iso).toLocaleString(undefined, { dateStyle: "medium", timeStyle: "medium" });
}

export function duration(ms: number | null | undefined): string {
  if (ms === null || ms === undefined) return "—";
  if (ms < 1000) return `${ms} ms`;
  return `${(ms / 1000).toFixed(ms < 10000 ? 2 : 1)} s`;
}

export function shortId(id: string): string {
  return id.length > 12 ? id.slice(0, 8) : id;
}

// ---------- small components ----------

export function Badge({ tone = "neutral", children, title }: { tone?: string; children: ReactNode; title?: string }) {
  return (
    <span className={`badge badge-${tone}`} title={title}>
      {children}
    </span>
  );
}

export function StatusBadge({ status }: { status: string }) {
  const tone = status === "ok" ? "ok" : status === "error" ? "error" : status === "running" ? "info" : "warn";
  const label = status === "ok" ? "success" : status;
  return <Badge tone={tone}>{label}</Badge>;
}

export function ReplayBadge({ mode }: { mode: string | null }) {
  if (!mode) return null;
  return (
    <Badge tone={mode === "live" ? "violet" : "teal"} title={mode === "live" ? "Tools re-executed against current data" : "Outputs served from capture"}>
      replay · {mode}
    </Badge>
  );
}

export function Spinner({ label }: { label?: string }) {
  return (
    <span className="spinner-wrap" role="status">
      <span className="spinner" aria-hidden />
      {label && <span className="muted">{label}</span>}
    </span>
  );
}

export function ErrorPanel({ error, title }: { error: unknown; title?: string }) {
  const e = error instanceof ApiError ? error : null;
  return (
    <div className="error-panel" role="alert">
      <strong>{title ?? (e?.code === "llm_unavailable" ? "Model unavailable" : "Something went wrong")}</strong>
      <div>{error instanceof Error ? error.message : String(error)}</div>
      {e?.requestId && <div className="muted mono small">request id {e.requestId}</div>}
    </div>
  );
}

export function Empty({ title, children }: { title: string; children?: ReactNode }) {
  return (
    <div className="empty">
      <div className="empty-title">{title}</div>
      {children && <div className="muted">{children}</div>}
    </div>
  );
}

export function CopyButton({ text, label = "Copy" }: { text: string; label?: string }) {
  const [done, setDone] = useState(false);
  return (
    <button
      type="button"
      className="btn btn-ghost btn-sm"
      onClick={() => {
        void navigator.clipboard?.writeText(text).then(() => {
          setDone(true);
          setTimeout(() => setDone(false), 1500);
        });
      }}
    >
      {done ? "Copied" : label}
    </button>
  );
}

export function Modal({ title, onClose, children }: { title: string; onClose: () => void; children: ReactNode }) {
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && onClose();
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);
  return (
    <div className="modal-backdrop" onMouseDown={onClose}>
      <div className="modal" role="dialog" aria-modal aria-label={title} onMouseDown={(e) => e.stopPropagation()}>
        <div className="modal-head">
          <h3>{title}</h3>
          <button className="btn btn-ghost btn-sm" onClick={onClose} aria-label="Close">
            ✕
          </button>
        </div>
        {children}
      </div>
    </div>
  );
}

// ---------- toasts ----------

type Toast = { id: number; tone: "ok" | "error" | "info"; text: string; detail?: string };
const ToastCtx = createContext<(t: Omit<Toast, "id">) => void>(() => {});

export function ToastProvider({ children }: { children: ReactNode }) {
  const [toasts, setToasts] = useState<Toast[]>([]);
  const push = useCallback((t: Omit<Toast, "id">) => {
    const id = Date.now() + Math.random();
    setToasts((ts) => [...ts, { ...t, id }]);
    setTimeout(() => setToasts((ts) => ts.filter((x) => x.id !== id)), t.tone === "error" ? 8000 : 4000);
  }, []);
  return (
    <ToastCtx.Provider value={push}>
      {children}
      <div className="toasts" aria-live="polite">
        {toasts.map((t) => (
          <div key={t.id} className={`toast toast-${t.tone}`}>
            <div>{t.text}</div>
            {t.detail && <div className="mono small muted">{t.detail}</div>}
          </div>
        ))}
      </div>
    </ToastCtx.Provider>
  );
}

export function useToast() {
  const push = useContext(ToastCtx);
  return useMemo(
    () => ({
      ok: (text: string) => push({ tone: "ok", text }),
      info: (text: string) => push({ tone: "info", text }),
      error: (e: unknown) =>
        push({
          tone: "error",
          text: e instanceof Error ? e.message : String(e),
          detail: e instanceof ApiError && e.requestId ? `request id ${e.requestId}` : undefined,
        }),
    }),
    [push],
  );
}

// ---------- JSON view with clickable values ----------

export type ValueIndex = Map<string, string>; // lowercase display -> fingerprint

const TOKEN_RE = /([A-Z]{2,5}-[A-Z0-9]{3,}|<[A-Z_]+:[0-9a-f]{6,}>|\b(?:ch|re|pi)_[A-Za-z0-9]{8,}\b|\$?\d+\.\d{2})/g;

function Tokens({ text, index, onValue }: { text: string; index?: ValueIndex; onValue?: (fp: string) => void }) {
  if (!index || !onValue) return <>{text}</>;
  const whole = index.get(text.toLowerCase());
  if (whole) return <ValueLink fp={whole} onValue={onValue}>{text}</ValueLink>;
  const parts: ReactNode[] = [];
  let last = 0;
  for (const m of text.matchAll(TOKEN_RE)) {
    const raw = m[0];
    const key = raw.replace(/^\$/, "").toLowerCase();
    const fp = index.get(key);
    if (!fp || m.index === undefined) continue;
    parts.push(text.slice(last, m.index));
    parts.push(
      <ValueLink key={m.index} fp={fp} onValue={onValue}>
        {raw}
      </ValueLink>,
    );
    last = m.index + raw.length;
  }
  if (!parts.length) return <>{text}</>;
  parts.push(text.slice(last));
  return <>{parts}</>;
}

function ValueLink({ fp, onValue, children }: { fp: string; onValue: (fp: string) => void; children: ReactNode }) {
  return (
    <button type="button" className="value-link" onClick={() => onValue(fp)} title="Trace where this value came from">
      {children}
    </button>
  );
}

export function JsonView({ value, index, onValue }: { value: unknown; index?: ValueIndex; onValue?: (fp: string) => void }) {
  return (
    <pre className="json">
      <JsonNode value={value} depth={0} index={index} onValue={onValue} />
    </pre>
  );
}

function JsonNode({ value, depth, index, onValue }: { value: unknown; depth: number; index?: ValueIndex; onValue?: (fp: string) => void }): ReactNode {
  const pad = "  ".repeat(depth + 1);
  const end = "  ".repeat(depth);
  if (value === null || value === undefined) return <span className="j-null">null</span>;
  if (typeof value === "string") {
    const parsed = maybeJson(value);
    if (parsed !== undefined) return <JsonNode value={parsed} depth={depth} index={index} onValue={onValue} />;
    return (
      <span className="j-str">
        "<Tokens text={value} index={index} onValue={onValue} />"
      </span>
    );
  }
  if (typeof value === "number") {
    const fp = index?.get(String(value)) ?? index?.get(value.toFixed(2));
    return fp && onValue ? (
      <ValueLink fp={fp} onValue={onValue}>
        <span className="j-num">{value}</span>
      </ValueLink>
    ) : (
      <span className="j-num">{value}</span>
    );
  }
  if (typeof value === "boolean") return <span className="j-bool">{String(value)}</span>;
  if (Array.isArray(value)) {
    if (!value.length) return "[]";
    return (
      <>
        {"[\n"}
        {value.map((v, i) => (
          <span key={i}>
            {pad}
            <JsonNode value={v} depth={depth + 1} index={index} onValue={onValue} />
            {i < value.length - 1 ? ",\n" : "\n"}
          </span>
        ))}
        {end}]
      </>
    );
  }
  const entries = Object.entries(value as Record<string, unknown>);
  if (!entries.length) return "{}";
  return (
    <>
      {"{\n"}
      {entries.map(([k, v], i) => (
        <span key={k}>
          {pad}
          <span className="j-key">"{k}"</span>:{" "}
          <JsonNode value={v} depth={depth + 1} index={index} onValue={onValue} />
          {i < entries.length - 1 ? ",\n" : "\n"}
        </span>
      ))}
      {end}
      {"}"}
    </>
  );
}

function maybeJson(s: string): unknown {
  const t = s.trim();
  if (t.length < 2 || (t[0] !== "{" && t[0] !== "[")) return undefined;
  try {
    return JSON.parse(t);
  } catch {
    return undefined;
  }
}
