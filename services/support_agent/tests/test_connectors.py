from datetime import timedelta

import pytest
from sqlalchemy import func, select

from conftest import ANCHOR
from support_agent.connectors.base import ConnectorError
from support_agent.connectors.mock import DryRunPayments, MockShopify, MockStripe
from support_agent.connectors.registry import apply_carrier_event, connector_statuses
from support_agent.data import models as m
from support_agent.data.generate import INCIDENT_CUSTOMER, INCIDENT_NEW_ORDER, INCIDENT_OLD_ORDER

pytestmark = pytest.mark.integration
FAST = {"latency_ms": (0, 0)}


async def test_incident_ordering_depends_on_sort_column(store_db):
    shop = MockShopify(store_db.sessionmaker, **FAST)
    by_updated = await shop.list_orders(INCIDENT_CUSTOMER, product_query="trail shoes", sort="updated_at")
    by_placed = await shop.list_orders(INCIDENT_CUSTOMER, product_query="trail shoes", sort="placed_at")
    assert [o["order_id"] for o in by_updated] == [INCIDENT_OLD_ORDER, INCIDENT_NEW_ORDER]
    assert [o["order_id"] for o in by_placed] == [INCIDENT_NEW_ORDER, INCIDENT_OLD_ORDER]


async def test_old_incident_order_is_already_refunded(store_db):
    shop = MockShopify(store_db.sessionmaker, **FAST)
    returns = await shop.get_returns(INCIDENT_OLD_ORDER)
    assert [r["status"] for r in returns] == ["refunded"]
    assert await shop.get_returns(INCIDENT_NEW_ORDER) == []


async def test_carrier_event_touches_order_and_flips_updated_at_ordering(store_db):
    shop = MockShopify(store_db.sessionmaker, **FAST)
    async with store_db.sessionmaker() as s:
        out = await apply_carrier_event(s, tracking_number="1ZKESTREL8124", status="delivered",
                                        location="Denver, CO", occurred_at=ANCHOR + timedelta(hours=1))
    assert out == {"shipment_id": "SHP-8124", "order_id": INCIDENT_NEW_ORDER, "status": "delivered"}
    by_updated = await shop.list_orders(INCIDENT_CUSTOMER, product_query="trail shoes", sort="updated_at")
    assert by_updated[0]["order_id"] == INCIDENT_NEW_ORDER


async def test_find_customer_by_email_is_case_insensitive(store_db):
    shop = MockShopify(store_db.sessionmaker, **FAST)
    c = await shop.find_customer(email="SARA.QURESHI@example.com")
    assert c and c["customer_id"] == INCIDENT_CUSTOMER


async def test_refund_offer_is_idempotent(store_db):
    stripe = MockStripe(store_db.sessionmaker, **FAST)
    kw = {"order_id": INCIDENT_NEW_ORDER, "order_item_id": 1, "sku": "FW-RIDGE-TR", "amount": "129.00",
          "status": "offered", "idempotency_key": "conv-1:turn-1"}
    a = await stripe.create_refund_offer(**kw)
    b = await stripe.create_refund_offer(**kw)
    assert a["refund_offer_id"] == b["refund_offer_id"]
    async with store_db.sessionmaker() as s:
        assert (await s.execute(select(func.count()).select_from(m.RefundOffer))).scalar_one() == 1


async def test_dry_run_payments_never_write(store_db):
    out = await DryRunPayments().create_refund_offer(order_id="ORD-1", order_item_id=1, sku="X", amount="1.00",
                                                     status="offered", idempotency_key="k")
    assert out["dry_run"] is True
    async with store_db.sessionmaker() as s:
        assert (await s.execute(select(func.count()).select_from(m.RefundOffer))).scalar_one() == 0


async def test_injected_errors_are_retryable(store_db):
    shop = MockShopify(store_db.sessionmaker, latency_ms=(0, 0), error_rate=1.0)
    with pytest.raises(ConnectorError) as exc:
        await shop.get_order(INCIDENT_NEW_ORDER)
    assert exc.value.retryable


async def test_three_connectors_connected_and_five_available(store_db):
    async with store_db.sessionmaker() as s:
        statuses = await connector_statuses(s)
    connected = sorted(c.name for c in statuses if c.connected)
    assert connected == ["shopify", "stripe", "zendesk"]
    assert len(statuses) == 8
    assert next(c for c in statuses if c.name == "shopify").records["customers"] == 120
