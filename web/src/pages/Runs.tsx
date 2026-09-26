import { keepPreviousData, useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useState } from "react";
import { Link, useNavigate, useSearchParams } from "react-router-dom";
import { api, type RunSummary } from "../api";
import { Empty, ErrorPanel, ReplayBadge, Spinner, StatusBadge, dateTime, duration, relTime, shortId, useIsAdmin, useToast } from "../components/ui";

const PAGE = 50;

export default function Runs() {
  const [params, setParams] = useSearchParams();
  const q = params.get("q") ?? "";
  const status = params.get("status") ?? "";
  const hideReplays = params.get("replays") === "hide";
  const page = Number(params.get("page") ?? "0");
  const [search, setSearch] = useState(q);
  const navigate = useNavigate();

  useEffect(() => {
    setSearch(q);
  }, [q]);
  useEffect(() => {
    const t = setTimeout(() => {
      if (search !== q) update({ q: search || null, page: null });
    }, 300);
    return () => clearTimeout(t);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [search]);

  function update(next: Record<string, string | null>) {
    const p = new URLSearchParams(params);
    for (const [k, v] of Object.entries(next)) (v === null ? p.delete(k) : p.set(k, v));
    setParams(p, { replace: true });
  }

  const runs = useQuery({
    queryKey: ["runs", q, status, hideReplays, page],
    queryFn: () => api.runs({ q, status, include_replays: !hideReplays, limit: PAGE, offset: page * PAGE }),
    placeholderData: keepPreviousData,
    refetchInterval: 10_000,
  });

  return (
    <div className="page">
      <header className="page-head">
        <div>
          <h1>Runs</h1>
          <p className="muted">Every agent turn, captured step by step. Open a run to trace any value to its origin.</p>
        </div>
      </header>

      <div className="toolbar">
        <input
          className="search"
          placeholder="Search runs: order ID, message text, run ID…"
          value={search}
          onChange={(e) => setSearch(e.target.value)}
          aria-label="Search runs"
        />
        <select value={status} onChange={(e) => update({ status: e.target.value || null, page: null })} aria-label="Status">
          <option value="">All statuses</option>
          <option value="ok">Success</option>
          <option value="error">Error</option>
          <option value="running">Running</option>
        </select>
        <label className="check">
          <input type="checkbox" checked={hideReplays} onChange={(e) => update({ replays: e.target.checked ? "hide" : null, page: null })} />
          Hide replays
        </label>
        {runs.isFetching && <Spinner />}
      </div>

      {runs.error ? (
        <ErrorPanel error={runs.error} />
      ) : !runs.data ? (
        <Spinner label="Loading runs…" />
      ) : runs.data.runs.length === 0 ? (
        <Empty title="No runs found">{q || status ? "Try a different filter." : "Runs appear here as soon as the agent handles a conversation."}</Empty>
      ) : (
        <>
          <div className="table-wrap">
            <table className="table">
              <thead>
                <tr>
                  <th style={{ width: 28 }} aria-label="Pinned" />
                  <th>Run</th>
                  <th>Status</th>
                  <th>Conversation</th>
                  <th>Steps</th>
                  <th>Duration</th>
                  <th>Started</th>
                </tr>
              </thead>
              <tbody>
                {runs.data.runs.map((r) => (
                  <tr key={r.id} className="row-link" onClick={() => navigate(`/runs/${r.id}`)}>
                    <td onClick={(e) => e.stopPropagation()}>
                      <PinButton run={r} />
                    </td>
                    <td>
                      <Link to={`/runs/${r.id}`} className="mono" onClick={(e) => e.stopPropagation()}>
                        {shortId(r.id)}
                      </Link>{" "}
                      <span className="muted">{r.name}</span> <ReplayBadge mode={r.replay_mode} />
                    </td>
                    <td>
                      <StatusBadge status={r.status} />
                    </td>
                    <td className="mono small">
                      {String(r.attrs.ticket_id ?? "—")} <span className="muted">· {String(r.attrs.customer_id ?? "")}</span>
                    </td>
                    <td>{r.step_count}</td>
                    <td>{duration(r.duration_ms)}</td>
                    <td title={dateTime(r.started_at)}>{relTime(r.started_at)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <div className="pager">
            <span className="muted">
              {page * PAGE + 1}–{page * PAGE + runs.data.runs.length} of {runs.data.total}
            </span>
            <button className="btn btn-sm" disabled={page === 0} onClick={() => update({ page: page > 1 ? String(page - 1) : null })}>
              Previous
            </button>
            <button
              className="btn btn-sm"
              disabled={(page + 1) * PAGE >= runs.data.total}
              onClick={() => update({ page: String(page + 1) })}
            >
              Next
            </button>
          </div>
        </>
      )}
    </div>
  );
}

export function PinButton({ run }: { run: Pick<RunSummary, "id" | "pinned"> }) {
  const isAdmin = useIsAdmin();
  const qc = useQueryClient();
  const toast = useToast();
  const m = useMutation({
    mutationFn: () => api.pin(run.id, !run.pinned),
    onSuccess: (res) => {
      toast.ok(res.pinned ? "Pinned: this run is kept regardless of retention" : "Unpinned");
      void qc.invalidateQueries({ queryKey: ["runs"] });
      void qc.invalidateQueries({ queryKey: ["run", run.id] });
    },
    onError: toast.error,
  });
  if (!isAdmin) {
    return (
      <span className="pin-static" title={run.pinned ? "Pinned (exempt from retention)" : "Only admins can pin runs"}>
        {run.pinned ? "★" : ""}
      </span>
    );
  }
  return (
    <button
      className={`pin ${run.pinned ? "pinned" : ""}`}
      onClick={() => m.mutate()}
      disabled={m.isPending}
      title={run.pinned ? "Pinned (exempt from retention). Click to unpin" : "Pin: keep this run regardless of retention"}
      aria-label={run.pinned ? "Unpin run" : "Pin run"}
    >
      {run.pinned ? "★" : "☆"}
    </button>
  );
}
