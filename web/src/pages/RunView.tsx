import { useMutation, useQuery } from "@tanstack/react-query";
import { useCallback, useEffect, useMemo, useState } from "react";
import { Link, useNavigate, useParams, useSearchParams } from "react-router-dom";
import { api, type RunDetail, type RunStep, type StepValue } from "../api";
import { LineagePanel, kindIcon, stepLabel } from "../components/LineagePanel";
import {
  Badge,
  ErrorPanel,
  JsonView,
  Modal,
  ReplayBadge,
  Spinner,
  StatusBadge,
  dateTime,
  duration,
  relTime,
  shortId,
  useIsAdmin,
  useToast,
  type ValueIndex,
} from "../components/ui";
import { PinButton } from "./Runs";

export default function RunView() {
  const { id = "" } = useParams();
  const [params, setParams] = useSearchParams();
  const selected = params.get("value");
  const run = useQuery({
    queryKey: ["run", id],
    queryFn: () => api.run(id),
    refetchInterval: (q) => (q.state.data?.status === "running" ? 1500 : false),
  });
  const [replayOpen, setReplayOpen] = useState(false);

  const select = useCallback(
    (fp: string | null) => {
      const p = new URLSearchParams(params);
      if (fp) p.set("value", fp);
      else p.delete("value");
      setParams(p, { replace: true });
    },
    [params, setParams],
  );

  const focusStep = useCallback((stepId: string) => {
    const el = document.getElementById(`step-${stepId}`);
    if (!el) return;
    el.scrollIntoView({ behavior: "smooth", block: "center" });
    el.classList.remove("flash");
    void el.offsetWidth;
    el.classList.add("flash");
  }, []);

  const index: ValueIndex = useMemo(() => {
    const m = new Map<string, string>();
    for (const s of run.data?.steps ?? []) for (const v of s.values) if (!m.has(v.display.toLowerCase())) m.set(v.display.toLowerCase(), v.fingerprint);
    return m;
  }, [run.data]);

  const lineage = useQuery({
    queryKey: ["lineage", id, selected],
    queryFn: () => api.lineage(id, selected!),
    enabled: !!selected,
  });
  const roles = useMemo(() => {
    const m = new Map<string, string>();
    for (const n of lineage.data?.nodes ?? []) m.set(n.step_id, n.role);
    return m;
  }, [lineage.data]);

  if (run.error) return <div className="page"><ErrorPanel error={run.error} /></div>;
  if (!run.data) return <div className="page"><Spinner label="Loading run…" /></div>;
  const r = run.data;

  return (
    <div className={`run-layout ${selected ? "with-panel" : ""}`}>
      <div className="page run-main">
        <RunHeader run={r} onReplay={() => setReplayOpen(true)} />

        <div className="hint">
          <span className="hint-dot" /> Click any highlighted value to see the step where it entered this run.
        </div>

        <div className="timeline">
          {r.steps.map((s) => (
            <StepCard
              key={s.id}
              step={s}
              index={index}
              selected={selected}
              role={roles.get(s.id)}
              onValue={(fp) => select(fp)}
            />
          ))}
        </div>
      </div>
      {selected && <LineagePanel runId={id} fingerprint={selected} onClose={() => select(null)} onFocusStep={focusStep} />}
      {replayOpen && <ReplayModal run={r} onClose={() => setReplayOpen(false)} />}
    </div>
  );
}

function RunHeader({ run, onReplay }: { run: RunDetail; onReplay: () => void }) {
  const isAdmin = useIsAdmin();
  const a = run.attrs;
  return (
    <header className="run-head">
      <div className="run-title">
        <PinButton run={run} />
        <h1>
          Run <span className="mono">{shortId(run.id)}</span>
        </h1>
        <StatusBadge status={run.status} />
        <ReplayBadge mode={run.replay_mode} />
        {a.legacy_order_lookup === true && (
          <Badge tone="warn" title="The legacy order lookup (ORDER BY updated_at) was enabled for this run">
            legacy lookup
          </Badge>
        )}
        <div className="spacer" />
        {!run.replay_of && isAdmin && (
          <button className="btn btn-primary" onClick={onReplay} disabled={run.status === "running"}>
            ↻ Replay
          </button>
        )}
      </div>
      <dl className="meta">
        <div>
          <dt>Started</dt>
          <dd title={dateTime(run.started_at)}>{relTime(run.started_at)}</dd>
        </div>
        <div>
          <dt>Duration</dt>
          <dd>{duration(run.duration_ms)}</dd>
        </div>
        <div>
          <dt>Steps</dt>
          <dd>{run.step_count}</dd>
        </div>
        <div>
          <dt>Service</dt>
          <dd className="mono">{run.service}</dd>
        </div>
        {"ticket_id" in a && (
          <div>
            <dt>Ticket</dt>
            <dd className="mono">{String(a.ticket_id)}</dd>
          </div>
        )}
        {"customer_id" in a && (
          <div>
            <dt>Customer</dt>
            <dd className="mono">{String(a.customer_id)}</dd>
          </div>
        )}
        {"conversation_id" in a && (
          <div>
            <dt>Conversation</dt>
            <dd>
              <Link className="mono" to={`/conversations/${String(a.conversation_id)}`}>
                {shortId(String(a.conversation_id))}
              </Link>
            </dd>
          </div>
        )}
      </dl>
      {run.error && <div className="error-inline">{run.error}</div>}
      {run.replay_of && (
        <div className="replay-note">
          Replay of <Link className="mono" to={`/runs/${run.replay_of}`}>{shortId(run.replay_of)}</Link> ·{" "}
          <Link to={`/runs/${run.replay_of}/diff/${run.id}`}>Compare with original →</Link>
        </div>
      )}
      {run.replays.length > 0 && (
        <div className="replays">
          <span className="muted">Replays:</span>
          {run.replays.map((rp) => (
            <span key={rp.id} className="replay-item">
              <Link className="mono" to={`/runs/${rp.id}`}>{shortId(rp.id)}</Link> <ReplayBadge mode={rp.replay_mode} />{" "}
              <Link className="small" to={`/runs/${run.id}/diff/${rp.id}`}>diff</Link>
            </span>
          ))}
        </div>
      )}
    </header>
  );
}

// Plumbing values (tool-call IDs, model names) carry no lineage signal worth a chip.
const NOISE_PATH = /\.(id|tool_call_id|model)$/;

function chipsFor(step: RunStep): StepValue[] {
  const wanted = (step.kind === "output" ? step.values : step.values.filter((v) => v.direction === "out")).filter(
    (v) => !NOISE_PATH.test(v.path),
  );
  const seen = new Set<string>();
  const out: StepValue[] = [];
  for (const v of wanted) {
    if (seen.has(v.fingerprint)) continue;
    seen.add(v.fingerprint);
    out.push(v);
  }
  // identifiers first, long free text last
  return out.sort((a, b) => score(b) - score(a)).slice(0, 24);
}

function score(v: StepValue): number {
  if (/^[A-Z]{2,5}-[A-Z0-9]{3,}$/.test(v.display)) return 3;
  if (/^<[A-Z_]+:/.test(v.display)) return 2;
  if (/^\d+(\.\d+)?$/.test(v.display)) return 1;
  return 0;
}

function StepCard({ step, index, selected, role, onValue }: {
  step: RunStep;
  index: ValueIndex;
  selected: string | null;
  role?: string;
  onValue: (fp: string) => void;
}) {
  const isEdge = step.kind === "input" || step.kind === "output";
  const [open, setOpen] = useState<"input" | "output" | null>(isEdge ? (step.kind === "input" ? "output" : "input") : null);
  const chips = chipsFor(step);
  useEffect(() => {
    if (role === "origin" && step.kind !== "input") setOpen("output");
  }, [role, step.kind]);

  const text = edgeText(step);

  return (
    <section id={`step-${step.id}`} className={`step-card kind-${step.kind} ${role ? `in-lineage role-${role}` : ""} ${step.status === "error" ? "step-error" : ""}`}>
      <div className="step-head">
        <span className={`step-icon icon-${step.kind}`} aria-hidden>
          {kindIcon(step.kind)}
        </span>
        <span className="mono muted step-seq">#{step.seq >= 1_000_000 ? "end" : step.seq}</span>
        <span className="step-name">{stepLabel(step.kind, step.name)}</span>
        {step.kind === "tool" && <span className="muted small">tool</span>}
        {step.candidates.length > 0 && (
          <Badge tone="warn" title="This step selected one value among several candidates">
            chose 1 of {step.candidates[0].alternatives.length + 1}
          </Badge>
        )}
        {role && <span className={`role-tag role-${role}`}>{role}</span>}
        <div className="spacer" />
        {step.status === "ok" ? <span className="ok-dot" title="succeeded">✓</span> : <StatusBadge status={step.status} />}
        <span className="muted small">{duration(step.duration_ms)}</span>
      </div>

      {text !== null && (
        <div className={`edge-text ${step.kind}`}>
          <JsonView value={text} index={index} onValue={onValue} />
        </div>
      )}

      {step.error && <div className="error-inline mono small">{step.error}</div>}

      {chips.length > 0 && (
        <div className="chips">
          {chips.map((v) => (
            <button
              key={v.fingerprint}
              className={`chip chip-btn mono ${selected === v.fingerprint ? "chip-selected" : ""}`}
              onClick={() => onValue(v.fingerprint)}
              title={`${v.path} — trace this value`}
            >
              {v.display.length > 48 ? `${v.display.slice(0, 46)}…` : v.display}
            </button>
          ))}
        </div>
      )}

      {!isEdge && (
        <div className="step-tabs">
          <button className={`tab ${open === "input" ? "active" : ""}`} onClick={() => setOpen(open === "input" ? null : "input")}>
            Input
          </button>
          <button className={`tab ${open === "output" ? "active" : ""}`} onClick={() => setOpen(open === "output" ? null : "output")}>
            Output
          </button>
        </div>
      )}
      {!isEdge && open && <JsonView value={open === "input" ? step.input : step.output} index={index} onValue={onValue} />}
    </section>
  );
}

function edgeText(step: RunStep): unknown {
  if (step.kind === "input") {
    const o = step.output as { message?: unknown } | null;
    return o && typeof o === "object" && "message" in o ? o.message : step.output;
  }
  if (step.kind === "output") return step.input;
  return null;
}

function ReplayModal({ run, onClose }: { run: RunDetail; onClose: () => void }) {
  const [mode, setMode] = useState<"live" | "recorded">("live");
  const navigate = useNavigate();
  const toast = useToast();
  const m = useMutation({
    mutationFn: () => api.replay(run.id, mode),
    onSuccess: (res) => {
      toast.ok(`Replay (${res.mode}) finished`);
      navigate(`/runs/${run.id}/diff/${res.run_id}`);
    },
    onError: toast.error,
  });
  return (
    <Modal title="Replay this run" onClose={onClose}>
      <div className="radio-cards">
        <label className={`radio-card ${mode === "live" ? "active" : ""}`}>
          <input type="radio" name="mode" checked={mode === "live"} onChange={() => setMode("live")} />
          <div>
            <strong>Live</strong>
            <p className="muted">
              Re-run the agent with tools executed against current data. Writes (refund offers, ticket notes) are dry-run. Shows what
              would happen today.
            </p>
          </div>
        </label>
        <label className={`radio-card ${mode === "recorded" ? "active" : ""}`}>
          <input type="radio" name="mode" checked={mode === "recorded"} onChange={() => setMode("recorded")} />
          <div>
            <strong>Recorded</strong>
            <p className="muted">
              Serve every tool and model output from the capture, so the agent sees exactly what it saw originally. Reproduces the
              original behaviour even if the data has changed since.
            </p>
          </div>
        </label>
      </div>
      <div className="modal-actions">
        <button className="btn" onClick={onClose}>
          Cancel
        </button>
        <button className="btn btn-primary" onClick={() => m.mutate()} disabled={m.isPending}>
          {m.isPending ? "Replaying…" : `Run ${mode} replay`}
        </button>
      </div>
    </Modal>
  );
}
