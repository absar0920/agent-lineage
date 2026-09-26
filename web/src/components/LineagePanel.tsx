import { useQuery } from "@tanstack/react-query";
import { Background, Handle, Position, ReactFlow, type Edge, type Node, type NodeProps } from "@xyflow/react";
import { useMemo } from "react";
import { api, type CandidateSet, type LineageNode } from "../api";
import { ErrorPanel, Spinner } from "./ui";

const ORIGIN_LABEL: Record<string, string> = {
  tool_output: "Entered as a tool output",
  llm_output: "Introduced by the model",
  run_input: "Came from the customer's message",
  unsourced: "Used without an earlier source",
};

export function kindIcon(kind: string): string {
  return kind === "llm" ? "✦" : kind === "tool" ? "⚒" : kind === "input" ? "⇥" : kind === "output" ? "⇤" : "•";
}

export function stepLabel(kind: string, name: string): string {
  if (kind === "input") return "Customer message";
  if (kind === "output") return "Final reply";
  if (kind === "llm") return "Model call";
  return name;
}

type FlowData = { node: LineageNode; candidates: CandidateSet[] };

function LineageFlowNode({ data }: NodeProps<Node<FlowData>>) {
  const { node, candidates } = data;
  return (
    <div className={`flow-node role-${node.role}`}>
      <Handle type="target" position={Position.Top} />
      <div className="flow-node-head">
        <span className="flow-kind">{kindIcon(node.kind)}</span>
        <span className="mono muted">#{node.seq >= 1_000_000 ? "end" : node.seq}</span>
        <span className="flow-role">{node.role}</span>
      </div>
      <div className="flow-name">{stepLabel(node.kind, node.name)}</div>
      {node.role === "origin" && candidates.length > 0 && (
        <div className="flow-cands">
          chose <b className="mono">{candidates[0].chosen}</b> over{" "}
          <span className="mono">{candidates[0].alternatives.join(", ") || "nothing"}</span>
        </div>
      )}
      <Handle type="source" position={Position.Bottom} />
    </div>
  );
}

const nodeTypes = { lineage: LineageFlowNode };
const ROW = 100;

export function LineagePanel({ runId, fingerprint, onClose, onFocusStep }: {
  runId: string;
  fingerprint: string;
  onClose: () => void;
  onFocusStep: (stepId: string) => void;
}) {
  const q = useQuery({ queryKey: ["lineage", runId, fingerprint], queryFn: () => api.lineage(runId, fingerprint) });

  const { nodes, edges } = useMemo(() => {
    const data = q.data;
    if (!data?.found) return { nodes: [] as Node<FlowData>[], edges: [] as Edge[] };
    const ns: Node<FlowData>[] = data.nodes.map((n, i) => ({
      id: n.step_id,
      type: "lineage",
      position: { x: 0, y: i * ROW },
      data: { node: n, candidates: data.candidates },
      draggable: false,
    }));
    const es: Edge[] = data.edges.map((e) => ({
      id: `${e.source}->${e.target}`,
      source: e.source,
      target: e.target,
      animated: true,
      className: "flow-edge",
    }));
    return { nodes: ns, edges: es };
  }, [q.data]);

  return (
    <aside className="lineage-panel" aria-label="Lineage">
      <div className="lineage-head">
        <div>
          <div className="eyebrow">Lineage</div>
          <div className="mono lineage-value">{q.data?.display || "…"}</div>
        </div>
        <button className="btn btn-ghost btn-sm" onClick={onClose} aria-label="Close lineage panel">
          ✕
        </button>
      </div>

      {q.isLoading && <Spinner label="Tracing…" />}
      {q.error && <ErrorPanel error={q.error} />}
      {q.data && !q.data.found && <div className="muted">{q.data.summary}</div>}
      {q.data?.found && (
        <>
          <div className={`origin-callout origin-${q.data.origin_kind}`}>
            <div className="eyebrow">{ORIGIN_LABEL[q.data.origin_kind ?? ""] ?? "Origin"}</div>
            <p>{q.data.summary}</p>
            {q.data.origin && (
              <button className="btn btn-sm" onClick={() => onFocusStep(q.data!.origin!.step_id)}>
                Go to origin step #{q.data.origin.seq}
              </button>
            )}
          </div>

          {q.data.candidates.length > 0 && (
            <div className="cands-box">
              <div className="eyebrow">Selection at origin</div>
              {q.data.candidates.map((c, i) => (
                <div key={i} className="cands-row">
                  <div>
                    <span className="muted">chosen</span> <span className="chip chip-origin mono">{c.chosen}</span>
                  </div>
                  <div>
                    <span className="muted">over</span>{" "}
                    {c.alternatives.length ? (
                      c.alternatives.map((a) => (
                        <span key={a} className="chip mono">
                          {a}
                        </span>
                      ))
                    ) : (
                      <span className="muted">no alternatives</span>
                    )}
                  </div>
                  {c.reason && <div className="mono small cands-reason">{c.reason}</div>}
                </div>
              ))}
            </div>
          )}

          <div className="flow-wrap" style={{ height: Math.max(nodes.length * ROW + 20, 160) }}>
            <ReactFlow
              nodes={nodes}
              edges={edges}
              nodeTypes={nodeTypes}
              fitView
              fitViewOptions={{ padding: 0.08, minZoom: 0.9, maxZoom: 1 }}
              nodesConnectable={false}
              elementsSelectable={false}
              proOptions={{ hideAttribution: true }}
              onNodeClick={(_, n) => onFocusStep(n.id)}
              panOnDrag={false}
              preventScrolling={false}
              zoomOnScroll={false}
              zoomOnPinch={false}
              zoomOnDoubleClick={false}
            >
              <Background gap={18} size={1} />
            </ReactFlow>
          </div>

          <div className="eyebrow" style={{ marginTop: 12 }}>
            Path
          </div>
          <ol className="lineage-list">
            {q.data.nodes.map((n) => (
              <li key={n.step_id} className={`role-${n.role}`}>
                <button className="link-btn" onClick={() => onFocusStep(n.step_id)}>
                  <span className="mono muted">#{n.seq >= 1_000_000 ? "end" : n.seq}</span> {stepLabel(n.kind, n.name)}
                </button>
                <span className={`role-tag role-${n.role}`}>{n.role}</span>
                <div className="mono small muted">{n.paths.slice(0, 3).join("  ")}</div>
              </li>
            ))}
          </ol>
        </>
      )}
    </aside>
  );
}
