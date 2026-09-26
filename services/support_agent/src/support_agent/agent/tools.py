"""Agent tools. Identity-bearing arguments (customer, ticket) and money amounts are
injected by the runtime, never taken from the model."""

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from lineage_sdk import Step

from support_agent.connectors.base import Connectors

REFUND_WINDOW_DAYS = 45

TOOL_SPECS: list[dict[str, Any]] = [
    {"type": "function", "function": {
        "name": "lookup_recent_order",
        "description": "Find the customer's most recent order containing a product. Use this to identify "
                       "which order the customer is talking about.",
        "parameters": {"type": "object", "properties": {
            "product_query": {"type": "string", "description": "Product words from the customer's message"}},
            "required": ["product_query"]}}},
    {"type": "function", "function": {
        "name": "get_order",
        "description": "Get an order's details and items.",
        "parameters": {"type": "object", "properties": {"order_id": {"type": "string"}}, "required": ["order_id"]}}},
    {"type": "function", "function": {
        "name": "get_shipment_status",
        "description": "Get carrier tracking status for an order.",
        "parameters": {"type": "object", "properties": {"order_id": {"type": "string"}}, "required": ["order_id"]}}},
    {"type": "function", "function": {
        "name": "check_refund_eligibility",
        "description": "Check whether an item in an order can be refunded under store policy.",
        "parameters": {"type": "object", "properties": {"order_id": {"type": "string"}, "sku": {"type": "string"}},
                       "required": ["order_id", "sku"]}}},
    {"type": "function", "function": {
        "name": "create_refund_offer",
        "description": "Create a refund offer for an eligible item. Only call after check_refund_eligibility "
                       "returned eligible=true for the same order and sku.",
        "parameters": {"type": "object", "properties": {"order_id": {"type": "string"}, "sku": {"type": "string"}},
                       "required": ["order_id", "sku"]}}},
    {"type": "function", "function": {
        "name": "add_ticket_note",
        "description": "Add an internal note to the support ticket (not visible to the customer).",
        "parameters": {"type": "object", "properties": {"note": {"type": "string"}}, "required": ["note"]}}},
]
TOOL_NAMES = {t["function"]["name"] for t in TOOL_SPECS}


class ToolError(Exception):
    """Returned to the model as {"error": ...}; the step is marked failed."""


@dataclass
class ToolContext:
    customer_id: str
    ticket_id: str
    conversation_id: str
    connectors: Connectors
    refund_approval_threshold: float
    legacy_order_lookup: bool = False
    now: datetime | None = None

    def clock(self) -> datetime:
        return self.now or datetime.now(UTC)


def parse_args(raw: str) -> dict[str, Any]:
    try:
        args = json.loads(raw or "{}")
    except ValueError as exc:
        raise ToolError(f"arguments are not valid JSON: {exc}") from exc
    if not isinstance(args, dict):
        raise ToolError("arguments must be a JSON object")
    return args


def effective_args(name: str, args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    """The arguments the tool actually runs with (what gets captured)."""
    allowed = {p for t in TOOL_SPECS if t["function"]["name"] == name
               for p in t["function"]["parameters"]["properties"]}
    clean = {k: v for k, v in args.items() if k in allowed}
    if name in ("lookup_recent_order",):
        clean["customer_id"] = ctx.customer_id
    if name == "add_ticket_note":
        clean["ticket_id"] = ctx.ticket_id
    return clean


async def run_tool(name: str, args: dict[str, Any], ctx: ToolContext, step: Step) -> Any:
    if name not in TOOL_NAMES:
        raise ToolError(f"unknown tool {name!r}")
    fn = _TOOLS[name]
    return await fn(ctx, step, **args)


async def _own_order(ctx: ToolContext, order_id: str) -> dict[str, Any]:
    order = await ctx.connectors.commerce.get_order(str(order_id))
    # Authorization: the model may only see the current customer's orders.
    if order is None or order["customer_id"] != ctx.customer_id:
        raise ToolError(f"order {order_id} not found for this customer")
    return order


async def lookup_recent_order(ctx: ToolContext, step: Step, *, customer_id: str, product_query: str) -> dict[str, Any]:
    sort = "updated_at" if ctx.legacy_order_lookup else "placed_at"
    orders = await ctx.connectors.commerce.list_orders(customer_id, product_query=product_query, sort=sort)
    if not orders:
        return {"found": False, "message": f"No orders containing '{product_query}'"}
    chosen, others = orders[0], orders[1:]
    step.note_candidates(chosen=chosen["order_id"], alternatives=[o["order_id"] for o in others],
                         reason=f"ORDER BY {sort} DESC LIMIT 1")
    result: dict[str, Any] = {"found": True, "order_id": chosen["order_id"], "placed_at": chosen["placed_at"],
                              "status": chosen["status"], "items": chosen["items"]}
    if not ctx.legacy_order_lookup and others:
        # Make ambiguity visible to the model so it can confirm with the customer.
        result["other_matching_orders"] = [{"order_id": o["order_id"], "placed_at": o["placed_at"]} for o in others]
    return result


async def get_order(ctx: ToolContext, step: Step, *, order_id: str) -> dict[str, Any]:
    return await _own_order(ctx, order_id)


async def get_shipment_status(ctx: ToolContext, step: Step, *, order_id: str) -> dict[str, Any]:
    await _own_order(ctx, order_id)
    shipment = await ctx.connectors.commerce.get_shipment(str(order_id))
    return shipment or {"order_id": order_id, "status": "not_shipped"}


async def check_refund_eligibility(ctx: ToolContext, step: Step, *, order_id: str, sku: str) -> dict[str, Any]:
    order = await _own_order(ctx, order_id)
    item = next((i for i in order["items"] if i["sku"] == sku), None)
    if item is None:
        return {"eligible": False, "reason": f"{sku} is not in order {order_id}"}
    placed = datetime.fromisoformat(order["placed_at"])
    days = (ctx.clock() - placed).days
    if order["status"] != "delivered":
        return {"eligible": False, "reason": f"order is {order['status']}, not delivered yet", "days_since_purchase": days}
    if days > REFUND_WINDOW_DAYS:
        return {"eligible": False, "reason": f"outside the {REFUND_WINDOW_DAYS}-day window", "days_since_purchase": days}
    if not ctx.legacy_order_lookup:
        returns = await ctx.connectors.commerce.get_returns(order_id)
        done = [r for r in returns if r["order_item_id"] == item["order_item_id"] and r["status"] in ("received", "refunded")]
        if done:
            return {"eligible": False, "reason": f"item already returned ({done[0]['return_id']}, {done[0]['status']})",
                    "days_since_purchase": days}
    return {"eligible": True, "order_id": order_id, "sku": sku, "price": item["price"], "days_since_purchase": days}


async def create_refund_offer(ctx: ToolContext, step: Step, *, order_id: str, sku: str) -> dict[str, Any]:
    order = await _own_order(ctx, order_id)
    item = next((i for i in order["items"] if i["sku"] == sku), None)
    if item is None:
        raise ToolError(f"{sku} is not in order {order_id}")
    amount = Decimal(item["price"]) * item["qty"]
    status = "needs_approval" if amount > Decimal(str(ctx.refund_approval_threshold)) else "offered"
    return await ctx.connectors.payments.create_refund_offer(
        order_id=order_id, order_item_id=item["order_item_id"], sku=sku, amount=f"{amount:.2f}", status=status,
        idempotency_key=f"{ctx.conversation_id}:{order_id}:{sku}")


async def add_ticket_note(ctx: ToolContext, step: Step, *, ticket_id: str, note: str) -> dict[str, Any]:
    return await ctx.connectors.helpdesk.add_comment(ticket_id=ticket_id, body=note[:4000], public=False, author="ai")


_TOOLS = {f.__name__: f for f in (lookup_recent_order, get_order, get_shipment_status, check_refund_eligibility,
                                  create_refund_offer, add_ticket_note)}
