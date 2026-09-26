import pytest
from fastapi.testclient import TestClient
from lineage_sdk import MemoryTransport, Recorder, Redactor
from lineage_sdk.values import fingerprint
from lineage_service.app import create_app
from lineage_service.config import Settings

pytestmark = pytest.mark.integration
API_KEY = "lk_test_key_0123456789abcdefghijkl"
ADMIN = {"email": "admin@example.com", "password": "correct horse battery staple"}


@pytest.fixture
def client(pg_urls):
    settings = Settings(
        database_url=pg_urls["lineage"],
        admin_email=ADMIN["email"],
        admin_password=ADMIN["password"],
        lineage_api_key=API_KEY,
        service_token="s" * 32,
        session_secret="x" * 32,
    )
    with TestClient(create_app(settings)) as c:
        yield c


def login(c: TestClient) -> None:
    assert c.post("/v1/auth/login", json=ADMIN).status_code == 200


def incident_events() -> tuple[str, list[dict]]:
    t = MemoryTransport()
    rec = Recorder(service="kestrel-support", transport=t, redactor=Redactor("k" * 32))
    with rec.run(
        "support-turn",
        input={"message": "the trail shoes from my latest order are too small", "email": "sara.qureshi@example.com"},
    ) as run:
        with run.step("llm", "chat", input={"messages": ["..."]}) as s:
            s.output(
                {
                    "tool_calls": [
                        {
                            "id": "call_1",
                            "name": "lookup_recent_order",
                            "arguments": '{"customer_id": "C-1042", "product_query": "trail shoes"}',
                        }
                    ]
                }
            )
        with run.step(
            "tool", "lookup_recent_order", input={"customer_id": "C-1042", "product_query": "trail shoes"}
        ) as s:
            s.note_candidates(chosen="ORD-7781", alternatives=["ORD-8124"], reason="ORDER BY updated_at DESC")
            s.output({"order_id": "ORD-7781", "items": [{"sku": "FW-RIDGE-TR", "price": "129.00"}]})
        with run.step("tool", "check_refund_eligibility", input={"order_id": "ORD-7781", "sku": "FW-RIDGE-TR"}) as s:
            s.output({"eligible": True, "days_since_purchase": 35})
        with run.step(
            "tool", "create_refund_offer", input={"order_id": "ORD-7781", "sku": "FW-RIDGE-TR", "amount": "129.00"}
        ) as s:
            s.output({"refund_offer_id": "RF-3FA1C2D9", "status": "offered"})
        run.output("I've created refund offer RF-3FA1C2D9 for $129.00 on order ORD-7781.")
    return run.id, t.events


def test_requires_auth(client):
    assert client.get("/v1/runs").status_code == 401
    assert client.post("/v1/ingest", json={"events": []}).status_code == 401
    assert client.post("/v1/ingest", json={"events": []}, headers={"Authorization": "Bearer nope"}).status_code == 401


def test_ingest_query_lineage_end_to_end(client):
    run_id, events = incident_events()
    r = client.post("/v1/ingest", json={"events": events}, headers={"Authorization": f"Bearer {API_KEY}"})
    assert r.status_code == 202 and r.json() == {"applied": 6, "skipped": 0}
    # idempotent re-send
    r = client.post("/v1/ingest", json={"events": events}, headers={"Authorization": f"Bearer {API_KEY}"})
    assert r.json() == {"applied": 0, "skipped": 6}

    login(client)
    run = client.get(f"/v1/runs/{run_id}").json()
    assert [s["kind"] for s in run["steps"]] == ["input", "llm", "tool", "tool", "tool", "output"]
    assert "sara.qureshi" not in str(run)

    lin = client.get(f"/v1/runs/{run_id}/lineage", params={"fingerprint": fingerprint("ORD-7781")}).json()
    assert lin["found"] and lin["origin_kind"] == "tool_output"
    assert lin["origin"]["name"] == "lookup_recent_order"
    assert lin["candidates"] == [
        {"chosen": "ORD-7781", "alternatives": ["ORD-8124"], "reason": "ORDER BY updated_at DESC"}
    ]
    assert [n["name"] for n in lin["nodes"]] == [
        "lookup_recent_order",
        "check_refund_eligibility",
        "create_refund_offer",
        "run.output",
    ]
    assert "chosen over ORD-8124" in lin["summary"]

    # a value the model introduced itself
    lin = client.get(f"/v1/runs/{run_id}/lineage", params={"fingerprint": fingerprint("trail shoes")}).json()
    assert lin["origin_kind"] == "llm_output"


def test_list_search_diff_pin_and_keys(client):
    run_a, ev_a = incident_events()
    run_b, ev_b = incident_events()
    hdr = {"Authorization": f"Bearer {API_KEY}"}
    client.post("/v1/ingest", json={"events": ev_a + ev_b}, headers=hdr)
    login(client)
    listed = client.get("/v1/runs", params={"q": "trail shoes"}).json()
    assert {run_a, run_b} <= {r["id"] for r in listed["runs"]}
    d = client.get(f"/v1/runs/{run_a}/diff/{run_b}").json()
    assert d["identical"] is True  # volatile ids (tool call ids, run ids) are ignored
    assert client.post(f"/v1/runs/{run_a}/pin", json={"pinned": True}).json()["pinned"] is True

    created = client.post("/v1/api-keys", json={"name": "ci"}).json()
    assert created["key"].startswith("lk_")
    assert (
        client.post(
            "/v1/ingest", json={"events": []}, headers={"Authorization": f"Bearer {created['key']}"}
        ).status_code
        == 202
    )
    client.delete(f"/v1/api-keys/{created['id']}")
    assert (
        client.post(
            "/v1/ingest", json={"events": []}, headers={"Authorization": f"Bearer {created['key']}"}
        ).status_code
        == 401
    )


def test_errors_have_request_ids(client):
    login(client)
    r = client.get("/v1/runs/does-not-exist")
    assert r.status_code == 404
    body = r.json()["error"]
    assert body["code"] == "not_found" and body["request_id"] == r.headers["x-request-id"]


def test_settings_and_retention(client):
    login(client)
    assert client.put("/v1/settings", json={"retention_days": 7}).json() == {"retention_days": 7}
    assert client.get("/v1/settings").json() == {"retention_days": 7}
    assert client.get("/metrics").status_code == 200
    assert client.get("/readyz").json() == {"status": "ready"}
