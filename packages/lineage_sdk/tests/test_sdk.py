import time

import httpx
import pytest
from lineage_sdk import (
    HttpTransport,
    MemoryTransport,
    RecordedRun,
    Recorder,
    Redactor,
    ReplayMissError,
)
from lineage_sdk.values import extract, fingerprint

KEY = "unit-test-redaction-key-0123456789"


# --- values ---------------------------------------------------------------


def test_fingerprint_normalises_numbers_and_case():
    assert fingerprint("129.00") == fingerprint(129) == fingerprint("129")
    assert fingerprint("ORD-7781") == fingerprint(" ord-7781 ")


def test_extract_walks_json_and_skips_noise():
    vals = {
        v.display: v.path
        for v in extract(
            {"order_id": "ORD-7781", "qty": 1, "ok": True, "items": [{"sku": "FW-RIDGE-TR", "price": "129.00"}]}
        )
    }
    assert vals == {"ORD-7781": "$.order_id", "FW-RIDGE-TR": "$.items[0].sku", "129.00": "$.items[0].price"}


def test_extract_finds_ids_and_amounts_in_prose():
    text = "I've created refund offer RF-3FA1C2D9 for $129.00 on order ORD-7781."
    found = {v.display for v in extract({"content": text})}
    assert {"RF-3FA1C2D9", "ORD-7781", "129.00"} <= found


def test_extract_looks_inside_json_string_arguments():
    found = {v.display for v in extract({"arguments": '{"order_id": "ORD-7781", "sku": "FW-RIDGE-TR"}'})}
    assert {"ORD-7781", "FW-RIDGE-TR"} <= found


# --- redaction ------------------------------------------------------------


def test_redaction_is_stable_and_keeps_business_ids():
    r = Redactor(KEY)
    a = r({"email": "Sara.Q@example.com", "order_id": "ORD-7781", "name": "Ridgeline Trail Shoes"})
    b = r("contact sara.q@example.com about ORD-7781")
    assert a["email"].startswith("<EMAIL:") and a["email"] in b
    assert a["order_id"] == "ORD-7781" and "ORD-7781" in b
    assert a["name"] == "Ridgeline Trail Shoes"


def test_redaction_learns_names_without_eating_common_words():
    r = Redactor(KEY)
    r({"full_name": "Will Harper"})
    out = r.redact_text("Hi Will, we will refund you. Will Harper confirmed.")
    assert "Harper" not in out and out.count("<NAME:") == 2 and " will refund" in out


def test_redaction_catches_phone_and_luhn_cards_only():
    r = Redactor(KEY)
    out = r.redact_text("call +1-303-555-0142, card 4242 4242 4242 4242, order 1234567890123")
    assert "555" not in out and "4242" not in out and "<CARD:" in out
    assert "1234567890123" in out  # fails Luhn, not a card


def test_different_keys_give_different_tokens():
    assert Redactor(KEY).token("EMAIL", "a@b.co") != Redactor(KEY + "x").token("EMAIL", "a@b.co")


# --- recorder -------------------------------------------------------------


def test_recorder_emits_run_steps_and_candidates_redacted():
    t = MemoryTransport()
    rec = Recorder(service="svc", transport=t, redactor=Redactor(KEY))
    with rec.run("refund", input={"email": "sara.q@example.com"}) as run:
        with run.step("tool", "lookup", input={"customer_id": "C-1042"}) as step:
            step.note_candidates(chosen="ORD-7781", alternatives=["ORD-8124"], reason="ORDER BY updated_at")
            step.output({"order_id": "ORD-7781"})
        run.output("refund on ORD-7781")
    kinds = [e["type"] for e in t.events]
    assert kinds == ["run_start", "step", "run_end"]
    assert "sara.q" not in str(t.events)
    step = t.events[1]
    assert step["seq"] == 1 and step["candidates"][0]["alternatives"] == ["ORD-8124"]


def test_exceptions_in_instrumented_code_are_recorded_and_reraised():
    t = MemoryTransport()
    rec = Recorder(service="svc", transport=t)
    with pytest.raises(ValueError), rec.run("r") as run, run.step("tool", "boom"):
        raise ValueError("bad")
    assert t.events[1]["status"] == "error" and "ValueError: bad" in t.events[1]["error"]
    assert t.events[2]["status"] == "error"


async def test_tool_decorator_wraps_async_functions():
    t = MemoryTransport()
    rec = Recorder(service="svc", transport=t)
    with rec.run("r") as run:

        @run.tool
        async def get_order(order_id: str) -> dict:
            return {"order_id": order_id}

        assert await get_order(order_id="ORD-1") == {"order_id": "ORD-1"}
    assert t.events[1]["name"] == "get_order" and t.events[1]["output"] == {"order_id": "ORD-1"}


def test_capture_failures_never_reach_the_agent():
    class Broken(MemoryTransport):
        def send(self, event):
            raise RuntimeError("transport exploded")

    rec = Recorder(service="svc", transport=Broken())
    with rec.run("r") as run, run.step("tool", "t") as step:
        step.output({"x": 1})
    # reaching here without an exception is the assertion


# --- transport ------------------------------------------------------------


def test_http_transport_batches_and_delivers():
    received: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        received.append(len(__import__("json").loads(request.content)["events"]))
        return httpx.Response(202)

    t = HttpTransport(
        "http://lineage", "k", flush_interval=0.05, client=httpx.Client(transport=httpx.MockTransport(handler))
    )
    for i in range(1200):
        t.send({"event_id": str(i)})
    assert t.flush(5)
    t.close()
    assert sum(received) == 1200 and max(received) <= 500 and t.stats.dropped == 0


def test_http_transport_drops_after_retries_without_blocking():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down")

    t = HttpTransport(
        "http://lineage",
        "k",
        flush_interval=0.01,
        max_retries=2,
        backoff=0.01,
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    start = time.perf_counter()
    for i in range(10):
        t.send({"event_id": str(i)})
    assert time.perf_counter() - start < 0.05  # send() never blocks
    assert t.flush(5)
    t.close()
    assert t.stats.dropped == 10 and t.stats.sent == 0


def test_http_transport_counts_queue_overflow():
    t = HttpTransport(
        "http://lineage",
        "k",
        max_queue=5,
        flush_interval=10,
        client=httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(202))),
    )
    t._stop.set()  # freeze the worker so the queue fills
    t._thread.join(2)
    for i in range(8):
        t.send({"event_id": str(i)})
    assert t.stats.dropped == 3


# --- replay ---------------------------------------------------------------

RUN = {
    "id": "r1",
    "steps": [
        {"seq": 1, "kind": "llm", "name": "chat", "status": "ok", "input": {}, "output": {"content": "a"}},
        {
            "seq": 2,
            "kind": "tool",
            "name": "lookup",
            "status": "ok",
            "input": {"q": "shoes"},
            "output": {"order_id": "ORD-7781"},
        },
        {
            "seq": 3,
            "kind": "tool",
            "name": "create",
            "status": "error",
            "input": {"id": 1},
            "output": None,
            "error": "boom",
        },
    ],
}


def test_recorded_run_serves_outputs_in_order():
    rr = RecordedRun(RUN)
    assert rr.next_llm() == {"content": "a"}
    assert rr.next_tool("lookup", {"q": "shoes"}) == {"order_id": "ORD-7781"}


def test_recorded_run_raises_on_mismatch_and_reproduces_errors():
    rr = RecordedRun(RUN)
    with pytest.raises(ReplayMissError):
        rr.next_tool("lookup", {"q": "boots"})
    rr.next_tool("lookup", {"q": "shoes"})
    with pytest.raises(Exception, match="boom"):
        rr.next_tool("create", {"id": 1})
    with pytest.raises(ReplayMissError):
        rr.next_tool("anything", {})
