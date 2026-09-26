"""LLM access: OpenRouter (OpenAI-compatible) with fallback, and recorded mode."""

import asyncio
import json
import logging
from typing import Any, ClassVar, Protocol

import httpx
from lineage_sdk import RecordedRun

log = logging.getLogger("support_agent.llm")


class LLMUnavailable(Exception):
    """No model could answer (after fallback). Surfaced to clients as 503."""


class LLMResponse(dict[str, Any]):
    """{"content": str | None, "tool_calls": [{"id", "name", "arguments"}], "model": str}"""


class LLM(Protocol):
    async def complete(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> LLMResponse: ...


class OpenRouterLLM:
    RETRYABLE: ClassVar[frozenset[int]] = frozenset({408, 409, 429, 500, 502, 503, 504})

    def __init__(
        self,
        *,
        api_key: str,
        model: str,
        fallback_model: str | None,
        base_url: str,
        timeout_s: float,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._models = [model] + ([fallback_model] if fallback_model and fallback_model != model else [])
        self._url = base_url.rstrip("/") + "/chat/completions"
        self._headers = {"Authorization": f"Bearer {api_key}", "X-Title": "Kestrel Support Agent"}
        self._client = client or httpx.AsyncClient(timeout=timeout_s)

    async def complete(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> LLMResponse:
        errors: list[str] = []
        for model in self._models:
            for attempt in range(2):  # one quick retry per model before falling back
                try:
                    resp = await self._client.post(
                        self._url,
                        headers=self._headers,
                        json={
                            "model": model,
                            "messages": messages,
                            "tools": tools,
                            "temperature": 0,
                        },
                    )
                except httpx.HTTPError as exc:
                    errors.append(f"{model}: {type(exc).__name__}")
                else:
                    if resp.status_code == 200:
                        body = resp.json()
                        if "choices" in body:
                            return _parse(body, model)
                        errors.append(f"{model}: {body.get('error', {}).get('message', 'no choices')}")
                    elif resp.status_code not in self.RETRYABLE:
                        # 400/401/403/404: retrying or falling back won't help with the same request/key.
                        raise LLMUnavailable(f"{model}: HTTP {resp.status_code} {resp.text[:200]}")
                    else:
                        errors.append(f"{model}: HTTP {resp.status_code}")
                if attempt == 0:
                    await asyncio.sleep(0.5)
            log.warning("model %s unavailable, trying fallback", model)
        raise LLMUnavailable("; ".join(errors))

    async def aclose(self) -> None:
        await self._client.aclose()


def _parse(body: dict[str, Any], requested_model: str) -> LLMResponse:
    msg = body["choices"][0]["message"]
    calls = [
        {"id": c["id"], "name": c["function"]["name"], "arguments": c["function"].get("arguments") or "{}"}
        for c in msg.get("tool_calls") or []
    ]
    return LLMResponse(content=msg.get("content"), tool_calls=calls, model=body.get("model", requested_model))


class RecordedLLM:
    """Serves captured LLM outputs (recorded replay)."""

    def __init__(self, recording: RecordedRun) -> None:
        self._rec = recording

    async def complete(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> LLMResponse:
        out = self._rec.next_llm()
        return LLMResponse(
            content=out.get("content"), tool_calls=out.get("tool_calls") or [], model=out.get("model", "recorded")
        )


class UnconfiguredLLM:
    async def complete(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> LLMResponse:
        raise LLMUnavailable("LLM_MODE=recorded: live conversations need LLM_MODE=live and OPENROUTER_API_KEY")


def tool_call_message(content: str | None, calls: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "role": "assistant",
        "content": content,
        "tool_calls": [
            {"id": c["id"], "type": "function", "function": {"name": c["name"], "arguments": c["arguments"]}}
            for c in calls
        ],
    }


def tool_result_message(call_id: str, result: Any) -> dict[str, Any]:
    return {"role": "tool", "tool_call_id": call_id, "content": json.dumps(result, default=str)}
