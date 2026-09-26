import httpx
import pytest
import respx
from lineage_sdk import MemoryTransport, Recorder
from support_agent.agent.tools import (
    ToolContext,
    ToolError,
    check_refund_eligibility,
    create_refund_offer,
    get_order,
)
from support_agent.config import Settings
from support_agent.connectors.registry import build_connectors
from support_agent.llm.client import LLMUnavailable, OpenRouterLLM

from conftest import ANCHOR

pytestmark = pytest.mark.integration


def ctx(store_db, **kw) -> ToolContext:
    settings = Settings(
        database_url="unused",
        llm_mode="recorded",
        lineage_redaction_key="k" * 32,
        service_token="s" * 32,
        connector_latency_ms=(0, 0),
    )
    return ToolContext(
        customer_id="C-1042",
        ticket_id="TCK-1",
        conversation_id="conv-1",
        connectors=build_connectors(store_db.sessionmaker, settings),
        refund_approval_threshold=200,
        now=ANCHOR,
        **kw,
    )


def step():
    rec = Recorder(service="t", transport=MemoryTransport())
    with rec.run("r") as run, run.step("tool", "t") as s:
        return s


async def test_fixed_eligibility_checks_the_returns_ledger(store_db):
    out = await check_refund_eligibility(ctx(store_db), step(), order_id="ORD-7781", sku="FW-RIDGE-TR")
    assert out["eligible"] is False and "already returned" in out["reason"]
    legacy = await check_refund_eligibility(
        ctx(store_db, legacy_order_lookup=True), step(), order_id="ORD-7781", sku="FW-RIDGE-TR"
    )
    assert legacy["eligible"] is True  # the original defect, kept for the regression test


async def test_model_cannot_read_other_customers_orders(store_db):
    with pytest.raises(ToolError, match="not found for this customer"):
        await get_order(ctx(store_db), step(), order_id="ORD-3600")


async def test_refund_amount_and_approval_are_server_side(store_db):
    out = await create_refund_offer(ctx(store_db), step(), order_id="ORD-8124", sku="FW-RIDGE-TR")
    assert out["amount"] == "129.00" and out["status"] == "offered"
    low = ctx(store_db)
    low.refund_approval_threshold = 100
    low.conversation_id = "conv-2"
    assert (await create_refund_offer(low, step(), order_id="ORD-8124", sku="FW-RIDGE-TR"))[
        "status"
    ] == "needs_approval"


def llm(**kw) -> OpenRouterLLM:
    return OpenRouterLLM(
        api_key="k",
        model="primary/model",
        fallback_model="fallback/model",
        base_url="https://openrouter.test/api/v1",
        timeout_s=5,
        **kw,
    )


OK = {
    "model": "fallback/model",
    "choices": [
        {
            "message": {
                "content": None,
                "tool_calls": [
                    {
                        "id": "c1",
                        "type": "function",
                        "function": {"name": "get_order", "arguments": '{"order_id": "ORD-1"}'},
                    }
                ],
            }
        }
    ],
}


@respx.mock
async def test_openrouter_falls_back_on_rate_limit():
    route = respx.post("https://openrouter.test/api/v1/chat/completions")
    route.side_effect = [httpx.Response(429), httpx.Response(429), httpx.Response(200, json=OK)]
    out = await llm().complete([{"role": "user", "content": "hi"}], [])
    assert out["model"] == "fallback/model" and out["tool_calls"][0]["name"] == "get_order"
    assert [c.request.read() for c in route.calls][2].find(b"fallback/model") > 0


@respx.mock
async def test_openrouter_auth_errors_fail_fast():
    respx.post("https://openrouter.test/api/v1/chat/completions").mock(return_value=httpx.Response(401, text="bad key"))
    with pytest.raises(LLMUnavailable, match="401"):
        await llm().complete([{"role": "user", "content": "hi"}], [])
