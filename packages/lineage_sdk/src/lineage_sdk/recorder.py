"""Recorder: the SDK's public capture API (SPEC §5.1).

    rec = Recorder(transport=HttpTransport(url, key), redactor=Redactor(secret), service="kestrel-support")
    with rec.run("refund-request", input=msg) as run:
        with run.step("tool", "lookup_recent_order", input=args) as step:
            step.note_candidates(chosen=..., alternatives=[...], reason="...")
            step.output(result)
        run.output(answer)

Everything here is fail-open: capture problems are logged, never raised into
the instrumented code. Exceptions raised *by* the instrumented code inside a
`step`/`run` block are recorded and re-raised unchanged.
"""

import functools
import inspect
import json
import logging
import time
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import Any, Literal, TypeVar

from lineage_sdk.transport import NullTransport, Transport

log = logging.getLogger("lineage_sdk")
StepKind = Literal["llm", "tool", "input", "output"]
F = TypeVar("F", bound=Callable[..., Any])


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _jsonable(obj: Any) -> Any:
    try:
        return json.loads(json.dumps(obj, default=str))
    except Exception:
        return repr(obj)


def _safe(fn: Callable[..., None]) -> Callable[..., None]:
    @functools.wraps(fn)
    def wrapper(*args: Any, **kwargs: Any) -> None:
        try:
            fn(*args, **kwargs)
        except Exception:
            log.exception("lineage capture error in %s (ignored)", fn.__name__)

    return wrapper


class Step:
    def __init__(self, run: "Run", seq: int, kind: StepKind, name: str, input: Any) -> None:
        self.run = run
        self.id = str(uuid.uuid4())
        self.seq = seq
        self.kind = kind
        self.name = name
        self._input = input
        self._output: Any = None
        self._candidates: list[dict[str, Any]] = []
        self._status = "ok"
        self._error: str | None = None
        self._started_at = _now()
        self._t0 = time.perf_counter()

    def output(self, value: Any) -> None:
        self._output = value

    def note_candidates(self, *, chosen: Any, alternatives: list[Any], reason: str | None = None) -> None:
        """Record that `chosen` was selected from several possibilities at this step."""
        self._candidates.append({"chosen": chosen, "alternatives": list(alternatives), "reason": reason})

    def fail(self, error: BaseException | str) -> None:
        self._status = "error"
        self._error = error if isinstance(error, str) else f"{type(error).__name__}: {error}"

    @_safe
    def _emit(self) -> None:
        red = self.run.recorder.redact
        self.run.recorder.transport.send({
            "event_id": str(uuid.uuid4()), "type": "step", "run_id": self.run.id, "step_id": self.id,
            "seq": self.seq, "kind": self.kind, "name": self.name, "status": self._status,
            "error": red(self._error) if self._error else None,
            "input": red(_jsonable(self._input)), "output": red(_jsonable(self._output)),
            "started_at": self._started_at, "duration_ms": int((time.perf_counter() - self._t0) * 1000),
            "candidates": [red(_jsonable(c)) for c in self._candidates],
        })


class Run:
    def __init__(self, recorder: "Recorder", name: str, input: Any, attrs: dict[str, Any],
                 run_id: str | None, replay_of: str | None, replay_mode: str | None) -> None:
        self.recorder = recorder
        self.id = run_id or str(uuid.uuid4())
        self.name = name
        self._seq = 0
        self._output: Any = None
        self._status = "ok"
        self._error: str | None = None
        self._start(input, attrs, replay_of, replay_mode)

    @_safe
    def _start(self, input: Any, attrs: dict[str, Any], replay_of: str | None, replay_mode: str | None) -> None:
        red = self.recorder.redact
        self.recorder.transport.send({
            "event_id": str(uuid.uuid4()), "type": "run_start", "run_id": self.id,
            "service": self.recorder.service, "name": self.name, "input": red(_jsonable(input)),
            "attrs": red(_jsonable(attrs)), "started_at": _now(),
            "replay_of": replay_of, "replay_mode": replay_mode,
        })

    def _next_seq(self) -> int:
        self._seq += 1
        return self._seq

    @contextmanager
    def step(self, kind: StepKind, name: str, input: Any = None) -> Iterator[Step]:
        step = Step(self, self._next_seq(), kind, name, input)
        try:
            yield step
        except BaseException as exc:
            step.fail(exc)
            raise
        finally:
            step._emit()

    def tool(self, fn: F) -> F:
        """Wrap a tool function (sync or async) so every call is captured as a step."""
        if inspect.iscoroutinefunction(fn):
            @functools.wraps(fn)
            async def awrapper(*args: Any, **kwargs: Any) -> Any:
                with self.step("tool", fn.__name__, input=kwargs or list(args)) as step:
                    result = await fn(*args, **kwargs)
                    step.output(result)
                    return result
            return awrapper  # type: ignore[return-value]

        @functools.wraps(fn)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            with self.step("tool", fn.__name__, input=kwargs or list(args)) as step:
                result = fn(*args, **kwargs)
                step.output(result)
                return result
        return wrapper  # type: ignore[return-value]

    def output(self, value: Any) -> None:
        self._output = value

    def fail(self, error: BaseException | str) -> None:
        self._status = "error"
        self._error = error if isinstance(error, str) else f"{type(error).__name__}: {error}"

    @_safe
    def _end(self) -> None:
        red = self.recorder.redact
        self.recorder.transport.send({
            "event_id": str(uuid.uuid4()), "type": "run_end", "run_id": self.id, "status": self._status,
            "output": red(_jsonable(self._output)), "error": red(self._error) if self._error else None,
            "ended_at": _now(),
        })


class Recorder:
    def __init__(self, *, service: str, transport: Transport | None = None,
                 redactor: Callable[[Any], Any] | None = None) -> None:
        self.service = service
        self.transport: Transport = transport or NullTransport()
        self._redactor = redactor

    def redact(self, obj: Any) -> Any:
        if self._redactor is None:
            return obj
        try:
            return self._redactor(obj)
        except Exception:
            log.exception("redaction failed; dropping value to avoid leaking PII")
            return "<REDACTION_FAILED>"

    @contextmanager
    def run(self, name: str, *, input: Any = None, attrs: dict[str, Any] | None = None,
            run_id: str | None = None, replay_of: str | None = None,
            replay_mode: str | None = None) -> Iterator[Run]:
        run = Run(self, name, input, attrs or {}, run_id, replay_of, replay_mode)
        try:
            yield run
        except BaseException as exc:
            run.fail(exc)
            raise
        finally:
            run._end()

    def close(self, timeout: float = 5.0) -> None:
        try:
            self.transport.close(timeout)
        except Exception:
            log.exception("lineage transport close failed (ignored)")
