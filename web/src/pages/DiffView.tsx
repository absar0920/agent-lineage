import { useQuery } from "@tanstack/react-query";
import { useEffect, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { api, type DiffRow, type DiffSide, type RunSummary } from "../api";
import { kindIcon, stepLabel } from "../components/LineagePanel";
import { ErrorPanel, JsonView, ReplayBadge, Spinner, StatusBadge, relTime, shortId } from "../components/ui";

export default function DiffView() {
  const { a = "", b = "" } = useParams();
  const diff = useQuery({ queryKey: ["diff", a, b], queryFn: () => api.diff(a, b) });

  useEffect(() => {
    const idx = diff.data?.first_divergence?.index;
    if (idx === undefined) return;
    const t = setTimeout(() => document.getElementById(`diff-row-${idx}`)?.scrollIntoView({ behavior: "smooth", block: "center" }), 150);
    return () => clearTimeout(t);
  }, [diff.data]);

  if (diff.error) return <div className="page"><ErrorPanel error={diff.error} /></div>;
  if (!diff.data) return <div className="page"><Spinner label="Comparing runs…" /></div>;
  const d = diff.data;
  const first = d.first_divergence;
  const firstSide = first?.a ?? first?.b;

  return (
    <div className="page">
      <header className="page-head">
        <div>
          <h1>Compare runs</h1>
          <p className="muted">Steps are aligned in order. Tool-call IDs, timings and model names are ignored.</p>
        </div>
      </header>

      <div className="diff-cols diff-heads">
        <RunCard run={d.a} label="A" />
        <RunCard run={d.b} label="B" />
      </div>

      {d.identical ? (
        <div className="banner banner-ok">
          <strong>Identical.</strong> Every step saw the same inputs and produced the same outputs, so run B reproduces run A exactly.
        </div>
      ) : (
        first && (
          <div className="banner banner-diverge">
            <strong>
              Runs diverge at step {first.index + 1}
              {firstSide ? `: ${stepLabel(firstSide.kind, firstSide.name)}` : ""}
            </strong>{" "}
            — {describe(first.changes)}.{" "}
            {first.changes.includes("output") && !first.changes.includes("input") && (
              <span>
                Same input, different output: the step read state that changed between the runs. Everything after this point follows from it.
              </span>
            )}
          </div>
        )
      )}

      <div className="diff-rows">
        {d.steps.map((row) => (
          <DiffRowView key={row.index} row={row} isFirst={first?.index === row.index} />
        ))}
      </div>
    </div>
  );
}

function describe(changes: string[]): string {
  const map: Record<string, string> = {
    output: "output differs",
    input: "input differs",
    status: "status differs",
    step: "a different step ran",
    missing: "one run has no matching step",
  };
  return changes.map((c) => map[c] ?? c).join(", ");
}

function RunCard({ run, label }: { run: RunSummary; label: string }) {
  return (
    <div className="diff-run-card">
      <span className="diff-label">{label}</span>
      <Link className="mono" to={`/runs/${run.id}`}>
        {shortId(run.id)}
      </Link>
      <StatusBadge status={run.status} />
      <ReplayBadge mode={run.replay_mode} />
      {!run.replay_mode && <span className="badge badge-neutral">original</span>}
      <span className="muted small">{relTime(run.started_at)}</span>
    </div>
  );
}

function DiffRowView({ row, isFirst }: { row: DiffRow; isFirst: boolean }) {
  const changed = row.changes.length > 0;
  const [open, setOpen] = useState(isFirst);
  useEffect(() => setOpen(isFirst), [isFirst]);
  const show: "input" | "output" = row.changes.includes("output") || !row.changes.includes("input") ? "output" : "input";
  return (
    <div id={`diff-row-${row.index}`} className={`diff-row ${changed ? "changed" : ""} ${isFirst ? "first" : ""}`}>
      <button className="diff-row-head" onClick={() => setOpen(!open)} aria-expanded={open}>
        <span className="mono muted">{row.index + 1}</span>
        <SideTitle side={row.a ?? row.b} />
        <div className="spacer" />
        {changed ? (
          row.changes.map((c) => (
            <span key={c} className="badge badge-warn">
              {c}
            </span>
          ))
        ) : (
          <span className="badge badge-ok">same</span>
        )}
        <span className="muted">{open ? "▾" : "▸"}</span>
      </button>
      {open && (
        <div className="diff-cols">
          <SideBody side={row.a} field={show} highlight={row.changes.includes(show)} />
          <SideBody side={row.b} field={show} highlight={row.changes.includes(show)} />
        </div>
      )}
    </div>
  );
}

function SideTitle({ side }: { side: DiffSide }) {
  if (!side) return <span className="muted">—</span>;
  return (
    <span className="diff-step-title">
      <span className={`step-icon icon-${side.kind}`}>{kindIcon(side.kind)}</span> {stepLabel(side.kind, side.name)}
    </span>
  );
}

function SideBody({ side, field, highlight }: { side: DiffSide; field: "input" | "output"; highlight: boolean }) {
  if (!side) return <div className="diff-side empty-side muted">No step</div>;
  const value = side.kind === "output" ? side.input : side.kind === "input" ? side.output : side[field];
  return (
    <div className={`diff-side ${highlight ? "hl" : ""}`}>
      <div className="eyebrow">{side.kind === "output" || side.kind === "input" ? "text" : field}</div>
      <JsonView value={value} />
    </div>
  );
}
