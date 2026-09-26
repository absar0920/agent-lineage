"""The support agent's tool-use loop, instrumented with Lineage."""

import logging
from dataclasses import dataclass
from typing import Any

from lineage_sdk import RecordedRun, Recorder
from lineage_sdk.replay import RecordedToolError

from support_agent.agent.tools import TOOL_SPECS, ToolContext, ToolError, effective_args, parse_args, run_tool
from support_agent.connectors.base import ConnectorError
from support_agent.llm.client import LLM, LLMUnavailable, tool_call_message, tool_result_message

log = logging.getLogger("support_agent.agent")
SERVICE = "kestrel-support"
CONNECTOR_RETRIES = 2

SYSTEM_PROMPT = """You are the customer support assistant for Kestrel Outdoor Co., an online outdoor-gear store.

You are talking to a verified, logged-in customer. You can look up their orders, check shipment status,
check refund eligibility, and create refund offers using the tools provided.

Rules:
- Identify the order the customer means with lookup_recent_order before acting on it.
- If lookup_recent_order reports other_matching_orders and the customer's wording does not make clear which
  order they mean, ask them to confirm (mention the order dates) instead of guessing.
- Only create a refund offer after check_refund_eligibility returned eligible=true for that order and sku.
- Refund offers with status needs_approval must be described as pending review by a team member.
- Never invent order IDs, amounts, or policies. If a tool returns an error, explain briefly and offer help.
- Be concise and friendly. Plain text, no markdown headings."""


@dataclass
class TurnResult:
    reply: str
    run_id: str
    status: str  # ok | error | escalated


@dataclass
class Turn:
    message: str
    history: list[dict[str, str]]  # prior {"role": "customer"|"assistant", "content"}
    ctx: ToolContext


async def run_turn(turn: Turn, *, llm: LLM, recorder: Recorder, max_iterations: int = 8,
                   recording: RecordedRun | None = None, replay_of: str | None = None,
                   replay_mode: str | None = None) -> TurnResult:
    ctx = turn.ctx
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": SYSTEM_PROMPT + f"\n\nCustomer ID: {ctx.customer_id}. Ticket: {ctx.ticket_id}."},
        *({"role": "user" if m["role"] == "customer" else "assistant", "content": m["content"]} for m in turn.history),
        {"role": "user", "content": turn.message},
    ]
    attrs = {"conversation_id": ctx.conversation_id, "ticket_id": ctx.ticket_id, "customer_id": ctx.customer_id,
             "legacy_order_lookup": ctx.legacy_order_lookup}
    run_input = {"message": turn.message, "history": turn.history}

    with recorder.run("support-turn", input=run_input, attrs=attrs, replay_of=replay_of,
                      replay_mode=replay_mode) as run:
        reply: str | None = None
        status = "ok"
        for _ in range(max_iterations):
            with run.step("llm", "chat.completions", input={"messages": messages}) as step:
                try:
                    response = await llm.complete(messages, TOOL_SPECS)
                except (LLMUnavailable, RecordedToolError) as exc:
                    step.fail(exc)
                    run.fail(exc)
                    raise LLMUnavailable(str(exc)) from exc
                step.output({"content": response.get("content"), "tool_calls": response.get("tool_calls"),
                             "model": response.get("model")})
            calls = response.get("tool_calls") or []
            if not calls:
                reply = (response.get("content") or "").strip()
                break
            messages.append(tool_call_message(response.get("content"), calls))
            for call in calls:
                result = await _execute(call, ctx, run, recording)
                messages.append(tool_result_message(call["id"], result))
        if reply is None:
            status = "escalated"
            reply = ("I'm sorry, I couldn't resolve this automatically. I've passed your request to a team "
                     "member who will follow up on this ticket.")
            run.fail("max iterations reached; escalated to a human")

        # Post the reply to the helpdesk ticket (a captured, replayable step like any tool).
        post_args = {"ticket_id": ctx.ticket_id, "body": reply}
        with run.step("tool", "post_ticket_reply", input=post_args) as step:
            try:
                if recording is not None:
                    step.output(recording.next_tool("post_ticket_reply", post_args))
                else:
                    step.output(await ctx.connectors.helpdesk.add_comment(
                        ticket_id=ctx.ticket_id, body=reply, public=True, author="ai"))
            except Exception as exc:  # the customer still gets the reply; the failure is visible in lineage
                step.fail(exc)
                log.exception("posting reply to ticket failed")
        run.output(reply)
    return TurnResult(reply=reply, run_id=run.id, status=status)


async def _execute(call: dict[str, Any], ctx: ToolContext, run: Any, recording: RecordedRun | None) -> Any:
    name = call["name"]
    try:
        args = effective_args(name, parse_args(call["arguments"]), ctx)
    except ToolError as exc:
        args = {"_raw": call["arguments"]}
        with run.step("tool", name, input=args) as step:
            step.fail(exc)
            step.output({"error": str(exc)})
        return {"error": str(exc)}

    with run.step("tool", name, input=args) as step:
        try:
            if recording is not None:
                result = recording.next_tool(name, args)
            else:
                result = await _with_retries(name, args, ctx, step)
        except (ToolError, ConnectorError, RecordedToolError, TypeError) as exc:
            step.fail(exc)
            result = {"error": str(exc)}
        step.output(result)
    return result


async def _with_retries(name: str, args: dict[str, Any], ctx: ToolContext, step: Any) -> Any:
    for attempt in range(CONNECTOR_RETRIES + 1):
        try:
            return await run_tool(name, args, ctx, step)
        except ConnectorError as exc:
            if not exc.retryable or attempt == CONNECTOR_RETRIES:
                raise
            log.warning("retrying %s after connector error: %s", name, exc)
    raise AssertionError("unreachable")
