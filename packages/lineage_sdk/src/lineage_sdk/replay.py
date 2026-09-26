"""Recorded replay (SPEC §5.6).

Serves a captured run's tool and LLM outputs back to the agent in order, so the
agent sees exactly the world it saw originally, even if the underlying systems
have changed since. A call that was not captured raises `ReplayMissError`; it
never silently falls through to live execution.
"""

import json
from collections.abc import Callable
from typing import Any


class ReplayMissError(Exception):
    def __init__(self, seq: int, name: str, args: Any, reason: str) -> None:
        super().__init__(f"replay miss at call {seq} ({name}): {reason}")
        self.seq, self.name, self.args, self.reason = seq, name, args, reason


class RecordedToolError(Exception):
    """The captured tool call failed; replay reproduces the failure."""


def _canonical(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str)


class RecordedRun:
    def __init__(self, run: dict[str, Any], *, redact: Callable[[Any], Any] | None = None) -> None:
        steps = sorted(run.get("steps", []), key=lambda s: s["seq"])
        self.run_id: str = run["id"]
        self._tools = [s for s in steps if s["kind"] == "tool"]
        self._llm = [s for s in steps if s["kind"] == "llm"]
        self._redact = redact or (lambda o: o)
        self._tool_i = 0
        self._llm_i = 0

    def next_llm(self) -> Any:
        if self._llm_i >= len(self._llm):
            raise ReplayMissError(self._llm_i + 1, "llm", None, "no more captured LLM responses")
        step = self._llm[self._llm_i]
        self._llm_i += 1
        if step["status"] != "ok":
            raise RecordedToolError(step.get("error") or "captured LLM call failed")
        return step["output"]

    def next_tool(self, name: str, args: Any) -> Any:
        i = self._tool_i
        if i >= len(self._tools):
            raise ReplayMissError(i + 1, name, args, "no more captured tool calls")
        step = self._tools[i]
        if step["name"] != name:
            raise ReplayMissError(i + 1, name, args, f"captured call was {step['name']!r}")
        if _canonical(self._redact(json.loads(_canonical(args)))) != _canonical(step["input"]):
            raise ReplayMissError(i + 1, name, args, "arguments differ from the captured call")
        self._tool_i += 1
        if step["status"] != "ok":
            raise RecordedToolError(step.get("error") or f"captured call to {name} failed")
        return step["output"]

    @property
    def exhausted(self) -> bool:
        return self._tool_i >= len(self._tools) and self._llm_i >= len(self._llm)
