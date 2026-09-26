"""SPEC §8: the reference incident, end to end across both services over real HTTP."""

from datetime import timedelta

import pytest
from lineage_sdk.values import fingerprint
from sqlalchemy import create_engine, text

from conftest import ANCHOR
from tests.fakes import PolicyLLM
from tests.servers import stack

pytestmark = pytest.mark.integration
MESSAGE = (
    "Hi, this is Sara (sara.qureshi@example.com, +1-303-555-0142). The trail shoes from my latest order "
    "are too small, can I get a refund?"
)


def find_step(run: dict, name: str) -> dict:
    return next(s for s in run["steps"] if s["name"] == name)


def test_reference_incident_is_diagnosed_and_replay_explains_itself(pg_urls, store_db):
    with stack(pg_urls, llm=PolicyLLM(), now=ANCHOR, legacy=True) as st:
        # 1. The silent failure: every step succeeds, the refund targets the already-refunded order.
        r = st.agent.post("/v1/conversations", json={"customer_email": "sara.qureshi@example.com", "message": MESSAGE})
        assert r.status_code == 201, r.text
        turn = r.json()
        assert "ORD-7781" in turn["reply"] and turn["status"] == "ok"
        run = st.wait_for_run(turn["run_id"])
        assert all(s["status"] == "ok" for s in run["steps"])
        assert [s["name"] for s in run["steps"] if s["kind"] == "tool"] == [
            "lookup_recent_order",
            "check_refund_eligibility",
            "create_refund_offer",
            "post_ticket_reply",
        ]

        # 2. Lineage: one query from the refunded order ID to its origin, with the alternative it beat.
        lin = st.lineage.get(f"/v1/runs/{run['id']}/lineage", params={"fingerprint": fingerprint("ORD-7781")}).json()
        assert lin["origin"]["name"] == "lookup_recent_order" and lin["origin_kind"] == "tool_output"
        assert lin["candidates"] == [
            {"chosen": "ORD-7781", "alternatives": ["ORD-8124"], "reason": "ORDER BY updated_at DESC LIMIT 1"}
        ]
        assert "create_refund_offer" in [n["name"] for n in lin["nodes"]]

        # 3. The world changes: the carrier confirms delivery of the other order.
        r = st.agent.post(
            "/v1/webhooks/carrier",
            json={
                "tracking_number": "1ZKESTREL8124",
                "status": "delivered",
                "location": "Denver, CO",
                "occurred_at": (ANCHOR + timedelta(hours=1)).isoformat(),
            },
        )
        assert r.status_code == 200, r.text

        # 4. Live replay re-executes tools against current state -> "can't reproduce".
        live = st.lineage.post(f"/v1/runs/{run['id']}/replay", json={"mode": "live"})
        assert live.status_code == 200, live.text
        live_run = st.wait_for_run(live.json()["run_id"])
        assert live_run["replay_of"] == run["id"] and live_run["replay_mode"] == "live"
        assert "ORD-8124" in str(live_run["output"]) and "ORD-7781" not in str(live_run["output"])

        # 5. Recorded replay serves what the tools saw at the time -> reproduced exactly.
        rec = st.lineage.post(f"/v1/runs/{run['id']}/replay", json={"mode": "recorded"})
        assert rec.status_code == 200, rec.text
        rec_run = st.wait_for_run(rec.json()["run_id"])
        assert "ORD-7781" in str(rec_run["output"])

        # 6. The diff points at the step whose output changed.
        d = st.lineage.get(f"/v1/runs/{run['id']}/diff/{live_run['id']}").json()
        assert d["first_divergence"]["a"]["name"] == "lookup_recent_order"
        assert d["first_divergence"]["changes"] == ["output"]
        assert st.lineage.get(f"/v1/runs/{run['id']}/diff/{rec_run['id']}").json()["identical"] is True

        # Replays never write to external systems.
        with create_engine(pg_urls["store"].replace("+asyncpg", "+psycopg2")).connect() as c:
            assert c.execute(text("SELECT count(*) FROM refund_offers")).scalar_one() == 1

    # PII: raw email and phone never reach the lineage database.
    with create_engine(pg_urls["lineage"].replace("+asyncpg", "+psycopg2")).connect() as c:
        dump = " ".join(
            str(row)
            for t in ("runs", "steps", "step_values", "candidates")
            for row in c.execute(text(f"SELECT * FROM {t}"))
        )
    assert "sara.qureshi@example.com" not in dump and "555-0142" not in dump and "3035550142" not in dump
    assert "ORD-7781" in dump  # business identifiers are kept


def test_fixed_agent_asks_instead_of_guessing(pg_urls, store_db):
    with stack(pg_urls, llm=PolicyLLM(), now=ANCHOR, legacy=False) as st:
        turn = st.agent.post(
            "/v1/conversations", json={"customer_email": "sara.qureshi@example.com", "message": MESSAGE}
        ).json()
        assert "Which one do you mean?" in turn["reply"]
        run = st.wait_for_run(turn["run_id"])
        lookup = find_step(run, "lookup_recent_order")
        assert lookup["output"]["order_id"] == "ORD-8124"
        assert [o["order_id"] for o in lookup["output"]["other_matching_orders"]] == ["ORD-7781"]
        assert lookup["candidates"][0]["reason"] == "ORDER BY placed_at DESC LIMIT 1"


def test_capture_is_fail_open_when_lineage_is_down(pg_urls, store_db):
    with stack(pg_urls, llm=PolicyLLM(), now=ANCHOR, lineage_up=False) as st:
        r = st.agent.post("/v1/conversations", json={"customer_email": "sara.qureshi@example.com", "message": MESSAGE})
        assert r.status_code == 201 and "Which one do you mean?" in r.json()["reply"]
        transport = st.agent_app.state.recorder.transport
        assert transport.flush(10)
        assert transport.stats.dropped > 0 and transport.stats.sent == 0
        assert "lineage_dropped_events_total" in st.agent.get("/metrics").text
