"""Lineage query and run diff (SPEC §5.5, §5.6)."""

import json
from dataclasses import asdict, dataclass, field
from itertools import pairwise
from typing import Any

from lineage_sdk.values import fingerprint as fp_of
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from lineage_service.models import Candidate, Step, StepValue


@dataclass
class LineageNode:
    step_id: str
    seq: int
    kind: str
    name: str
    role: str  # origin | consumer | carrier
    directions: list[str]
    paths: list[str]


@dataclass
class LineageResult:
    run_id: str
    fingerprint: str
    display: str
    found: bool
    origin: LineageNode | None = None
    origin_kind: str | None = None  # tool_output | llm_output | run_input | unsourced
    candidates: list[dict[str, Any]] = field(default_factory=list)
    nodes: list[LineageNode] = field(default_factory=list)
    edges: list[dict[str, str]] = field(default_factory=list)
    summary: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


async def lineage(session: AsyncSession, run_id: str, fingerprint: str) -> LineageResult:
    rows = (
        await session.execute(
            select(StepValue, Step)
            .join(Step, StepValue.step_id == Step.id)
            .where(StepValue.run_id == run_id, StepValue.fingerprint == fingerprint)
            .order_by(Step.seq)
        )
    ).all()
    if not rows:
        return LineageResult(
            run_id=run_id,
            fingerprint=fingerprint,
            display="",
            found=False,
            summary="This value does not appear in the run.",
        )

    by_step: dict[str, dict[str, Any]] = {}
    for value, step in rows:
        entry = by_step.setdefault(step.id, {"step": step, "dirs": set(), "paths": []})
        entry["dirs"].add(value.direction)
        entry["paths"].append(f"{value.direction}:{value.path}")
    ordered = sorted(by_step.values(), key=lambda e: e["step"].seq)
    display = rows[0][0].display

    # Origin: first step that emits the value without having received it.
    origin_entry = next((e for e in ordered if "out" in e["dirs"] and "in" not in e["dirs"]), None)
    if origin_entry is None:
        origin_entry, origin_kind = ordered[0], "unsourced"
    else:
        origin_kind = {"input": "run_input", "llm": "llm_output"}.get(origin_entry["step"].kind, "tool_output")

    nodes: list[LineageNode] = []
    started = False
    for e in ordered:
        s = e["step"]
        if s.id == origin_entry["step"].id:
            started = True
            role = "origin"
        elif not started:
            continue  # appearances before the origin can't be part of this value's flow
        else:
            role = "consumer" if "in" in e["dirs"] else "carrier"
        nodes.append(
            LineageNode(
                step_id=s.id,
                seq=s.seq,
                kind=s.kind,
                name=s.name,
                role=role,
                directions=sorted(e["dirs"]),
                paths=e["paths"],
            )
        )
    edges = [{"source": a.step_id, "target": b.step_id} for a, b in pairwise(nodes)]

    cands = (
        (
            await session.execute(
                select(Candidate).where(Candidate.run_id == run_id, Candidate.step_id == origin_entry["step"].id)
            )
        )
        .scalars()
        .all()
    )
    candidates: list[dict[str, Any]] = [
        {"chosen": c.chosen, "alternatives": c.alternatives, "reason": c.reason}
        for c in cands
        if c.chosen_fingerprint == fingerprint or any(fp_of(a) == fingerprint for a in c.alternatives)
    ]

    o = nodes[0]
    summary = {
        "tool_output": f"{display} entered the run at step {o.seq} as output of tool `{o.name}`",
        "llm_output": f"{display} was introduced by the model at step {o.seq} (not returned by any tool)",
        "run_input": f"{display} came from the run input",
        "unsourced": f"{display} is used at step {o.seq} but never produced by an earlier step",
    }[origin_kind]
    if candidates:
        c = candidates[0]
        alternatives: list[str] = c["alternatives"]
        summary += f"; it was chosen over {', '.join(alternatives)}"
        if c["reason"]:
            summary += f" ({c['reason']})"
    consumers = [n for n in nodes if n.role == "consumer"]
    if consumers:
        summary += f"; then consumed by {len(consumers)} later step(s)."
    return LineageResult(
        run_id=run_id,
        fingerprint=fingerprint,
        display=display,
        found=True,
        origin=o,
        origin_kind=origin_kind,
        candidates=candidates,
        nodes=nodes,
        edges=edges,
        summary=summary,
    )


# --- diff -------------------------------------------------------------------

VOLATILE_KEYS = {
    "id",
    "tool_call_id",
    "call_id",
    "created",
    "created_at",
    "updated_at_fetched",
    "duration_ms",
    "dry_run",
    "usage",
    "model",
}


def _stable(obj: Any) -> Any:
    if isinstance(obj, dict):
        return {k: _stable(v) for k, v in sorted(obj.items()) if k not in VOLATILE_KEYS}
    if isinstance(obj, list):
        return [_stable(v) for v in obj]
    if isinstance(obj, str) and obj[:1] in "{[":
        try:
            return _stable(json.loads(obj))
        except ValueError:
            return obj
    return obj


def _same(a: Any, b: Any) -> bool:
    return json.dumps(_stable(a), sort_keys=True, default=str) == json.dumps(_stable(b), sort_keys=True, default=str)


def diff_steps(a: list[Step], b: list[Step]) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    first: dict[str, Any] | None = None
    for i in range(max(len(a), len(b))):
        sa = a[i] if i < len(a) else None
        sb = b[i] if i < len(b) else None
        changes: list[str] = []
        if sa is None or sb is None:
            changes.append("missing")
        else:
            if (sa.kind, sa.name) != (sb.kind, sb.name):
                changes.append("step")
            if not _same(sa.input, sb.input):
                changes.append("input")
            if not _same(sa.output, sb.output):
                changes.append("output")
            if sa.status != sb.status:
                changes.append("status")
        row = {"index": i, "a": _brief(sa), "b": _brief(sb), "changes": changes}
        rows.append(row)
        if changes and first is None:
            first = row
    return {"identical": first is None, "first_divergence": first, "steps": rows}


def _brief(s: Step | None) -> dict[str, Any] | None:
    if s is None:
        return None
    return {
        "step_id": s.id,
        "seq": s.seq,
        "kind": s.kind,
        "name": s.name,
        "status": s.status,
        "input": s.input,
        "output": s.output,
    }
