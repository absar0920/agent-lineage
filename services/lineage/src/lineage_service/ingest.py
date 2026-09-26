"""Ingest: apply SDK events to storage. Idempotent and order-tolerant.

- Event IDs are recorded in `ingested_events`; a re-sent event is skipped.
- A step that arrives before its run_start creates a placeholder run.
- The run input is stored as step seq 0 (kind `input`) and the final output as a
  trailing `output` step, so the lineage query treats them like any other step.
"""

from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

from lineage_sdk.values import extract, fingerprint
from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from lineage_service.models import Candidate, IngestedEvent, Run, Step, StepValue

OUTPUT_SEQ_BASE = 1_000_000  # keeps the output step after any real step


def _ts(value: str | None) -> datetime:
    return datetime.fromisoformat(value) if value else datetime.now(UTC)


async def ingest(session: AsyncSession, events: Sequence[dict[str, Any]]) -> dict[str, int]:
    applied = skipped = 0
    for event in events:
        inserted = await session.execute(
            pg_insert(IngestedEvent).values(id=event["event_id"]).on_conflict_do_nothing().returning(IngestedEvent.id)
        )
        if inserted.scalar_one_or_none() is None:
            skipped += 1
            continue
        match event.get("type"):
            case "run_start":
                await _run_start(session, event)
            case "step":
                await _step(session, event)
            case "run_end":
                await _run_end(session, event)
            case other:
                raise ValueError(f"unknown event type: {other!r}")
        applied += 1
    await session.commit()
    return {"applied": applied, "skipped": skipped}


async def _ensure_run(session: AsyncSession, run_id: str, started_at: datetime) -> None:
    await session.execute(
        pg_insert(Run)
        .values(
            id=run_id,
            service="unknown",
            name="(pending)",
            status="running",
            started_at=started_at,
            attrs={},
        )
        .on_conflict_do_nothing()
    )


async def _run_start(session: AsyncSession, e: dict[str, Any]) -> None:
    started = _ts(e.get("started_at"))
    values = {
        "service": e["service"],
        "name": e["name"],
        "input": e.get("input"),
        "attrs": e.get("attrs") or {},
        "started_at": started,
        "replay_of": e.get("replay_of"),
        "replay_mode": e.get("replay_mode"),
    }
    stmt = pg_insert(Run).values(id=e["run_id"], status="running", **values)
    await session.execute(stmt.on_conflict_do_update(index_elements=[Run.id], set_=values))
    await _write_step(
        session,
        run_id=e["run_id"],
        step_id=f"{e['run_id']}:input",
        seq=0,
        kind="input",
        name="run.input",
        status="ok",
        input=None,
        output=e.get("input"),
        error=None,
        started_at=started,
        duration_ms=0,
        candidates=[],
    )


async def _step(session: AsyncSession, e: dict[str, Any]) -> None:
    started = _ts(e.get("started_at"))
    await _ensure_run(session, e["run_id"], started)
    await _write_step(
        session,
        run_id=e["run_id"],
        step_id=e["step_id"],
        seq=e["seq"],
        kind=e["kind"],
        name=e["name"],
        status=e.get("status", "ok"),
        input=e.get("input"),
        output=e.get("output"),
        error=e.get("error"),
        started_at=started,
        duration_ms=e.get("duration_ms"),
        candidates=e.get("candidates") or [],
    )


async def _run_end(session: AsyncSession, e: dict[str, Any]) -> None:
    ended = _ts(e.get("ended_at"))
    await _ensure_run(session, e["run_id"], ended)
    await session.execute(
        update(Run)
        .where(Run.id == e["run_id"])
        .values(status=e.get("status", "ok"), output=e.get("output"), error=e.get("error"), ended_at=ended)
    )
    if e.get("output") is not None:
        last = (
            await session.execute(
                select(func.max(Step.seq)).where(Step.run_id == e["run_id"], Step.seq < OUTPUT_SEQ_BASE)
            )
        ).scalar_one() or 0
        await _write_step(
            session,
            run_id=e["run_id"],
            step_id=f"{e['run_id']}:output",
            seq=OUTPUT_SEQ_BASE + last + 1,
            kind="output",
            name="run.output",
            status="ok",
            input=e.get("output"),
            output=None,
            error=None,
            started_at=ended,
            duration_ms=0,
            candidates=[],
        )


async def _write_step(
    session: AsyncSession,
    *,
    run_id: str,
    step_id: str,
    seq: int,
    kind: str,
    name: str,
    status: str,
    input: Any,
    output: Any,
    error: str | None,
    started_at: datetime,
    duration_ms: int | None,
    candidates: list[dict[str, Any]],
) -> None:
    result = await session.execute(
        pg_insert(Step)
        .values(
            id=step_id,
            run_id=run_id,
            seq=seq,
            kind=kind,
            name=name,
            status=status,
            input=input,
            output=output,
            error=error,
            started_at=started_at,
            duration_ms=duration_ms,
        )
        .on_conflict_do_nothing()
        .returning(Step.id)
    )
    if result.scalar_one_or_none() is None:
        return  # step already stored (same id or same seq)
    rows = [
        {
            "run_id": run_id,
            "step_id": step_id,
            "seq": seq,
            "direction": direction,
            "path": v.path,
            "fingerprint": v.fingerprint,
            "display": v.display,
        }
        for direction, payload in (("in", input), ("out", output))
        for v in extract(payload)
    ]
    if rows:
        await session.execute(pg_insert(StepValue), rows)
    for c in candidates:
        await session.execute(
            pg_insert(Candidate).values(
                run_id=run_id,
                step_id=step_id,
                chosen=str(c.get("chosen")),
                chosen_fingerprint=fingerprint(c.get("chosen")),
                alternatives=[str(a) for a in c.get("alternatives") or []],
                reason=c.get("reason"),
            )
        )
