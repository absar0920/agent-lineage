"""A deterministic stand-in for the LLM that follows the support policy.

It behaves like a competent model: identify the order, check eligibility, create
the offer, and ask the customer when the lookup reports ambiguity.
"""

import json
import re
import uuid
from typing import Any

from support_agent.llm.client import LLMResponse


def _call(name: str, **args: Any) -> LLMResponse:
    return LLMResponse(
        content=None,
        model="scripted/policy",
        tool_calls=[{"id": f"call_{uuid.uuid4().hex[:8]}", "name": name, "arguments": json.dumps(args)}],
    )


def _say(text: str) -> LLMResponse:
    return LLMResponse(content=text, tool_calls=[], model="scripted/policy")


class PolicyLLM:
    def __init__(self) -> None:
        self.calls = 0

    async def complete(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> LLMResponse:
        self.calls += 1
        last = messages[-1]
        if last["role"] == "user":
            words = re.findall(r"[a-z]+", last["content"].lower())
            query = " ".join(w for w in words if w in {"trail", "shoes", "jacket", "tent", "boots", "pack"}) or "shoes"
            return _call("lookup_recent_order", product_query=query)
        result = json.loads(last["content"])
        tool = _last_tool_name(messages)
        if "error" in result:
            return _say(f"Sorry, something went wrong: {result['error']}")
        if tool == "lookup_recent_order":
            if not result.get("found"):
                return _say("I couldn't find an order with that product.")
            if result.get("other_matching_orders"):
                dates = ", ".join(o["placed_at"][:10] for o in [result, *result["other_matching_orders"]])
                return _say(f"I found more than one order with that item (placed {dates}). Which one do you mean?")
            return _call("check_refund_eligibility", order_id=result["order_id"], sku=result["items"][0]["sku"])
        if tool == "check_refund_eligibility":
            if not result["eligible"]:
                return _say(f"I'm sorry, that item isn't eligible for a refund: {result['reason']}.")
            return _call("create_refund_offer", order_id=result["order_id"], sku=result["sku"])
        if tool == "create_refund_offer":
            return _say(
                f"I've created refund offer {result['refund_offer_id']} for ${result['amount']} "
                f"on order {result['order_id']}."
            )
        return _say("How else can I help?")


def _last_tool_name(messages: list[dict[str, Any]]) -> str:
    for msg in reversed(messages):
        if msg["role"] == "assistant" and msg.get("tool_calls"):
            return msg["tool_calls"][-1]["function"]["name"]
    return ""
