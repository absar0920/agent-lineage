"""Connector catalog, construction, status, and the carrier webhook handler."""

import random
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from support_agent.config import Settings
from support_agent.connectors.base import ConnectorKind, Connectors, ConnectorStatus
from support_agent.connectors.mock import (
    DryRunHelpdesk,
    DryRunPayments,
    MockShopify,
    MockStripe,
    MockZendesk,
)
from support_agent.data import models as m

CATALOG: dict[str, tuple[str, ConnectorKind]] = {
    "shopify": ("Shopify", "commerce"),
    "zendesk": ("Zendesk", "helpdesk"),
    "stripe": ("Stripe", "payments"),
    "woocommerce": ("WooCommerce", "commerce"),
    "gorgias": ("Gorgias", "helpdesk"),
    "intercom": ("Intercom", "helpdesk"),
    "freshdesk": ("Freshdesk", "helpdesk"),
    "shipstation": ("ShipStation", "shipping"),
}
# Connectors that have an adapter in this release.
ADAPTERS = {"shopify", "zendesk", "stripe"}


def build_connectors(
    sm: async_sessionmaker[AsyncSession], settings: Settings, *, dry_run: bool = False, rng: random.Random | None = None
) -> Connectors:
    kw: dict[str, Any] = {
        "latency_ms": settings.connector_latency_ms,
        "error_rate": settings.connector_error_rate,
        "rng": rng,
    }
    commerce = MockShopify(sm, **kw)
    if dry_run:
        return Connectors(commerce=commerce, payments=DryRunPayments(), helpdesk=DryRunHelpdesk())
    return Connectors(commerce=commerce, payments=MockStripe(sm, **kw), helpdesk=MockZendesk(sm, **kw))


async def connector_statuses(session: AsyncSession) -> list[ConnectorStatus]:
    configs = {c.name: c for c in (await session.execute(select(m.ConnectorConfig))).scalars()}

    async def count(model: type[m.Base]) -> int:
        return int((await session.execute(select(func.count()).select_from(model))).scalar_one())

    records = {
        "shopify": {
            "customers": await count(m.Customer),
            "orders": await count(m.Order),
            "products": await count(m.Product),
        },
        "zendesk": {"tickets": await count(m.Ticket), "comments": await count(m.TicketComment)},
        "stripe": {
            "payments": await count(m.Payment),
            "refunds": await count(m.Refund),
            "refund_offers": await count(m.RefundOffer),
        },
    }
    out = []
    for name, (display, kind) in CATALOG.items():
        cfg = configs.get(name)
        connected = bool(cfg and cfg.enabled and name in ADAPTERS)
        out.append(
            ConnectorStatus(
                name=name,
                display_name=display,
                kind=kind,
                connected=connected,
                adapter="mock" if name in ADAPTERS else "none",
                last_sync_at=cfg.last_sync_at if cfg else None,
                records=records.get(name, {}) if connected else {},
            )
        )
    return out


async def save_connector_config(session: AsyncSession, name: str, settings: dict[str, Any]) -> None:
    """Store settings for a connector. Only connectors with an adapter can be enabled."""
    cfg = await session.get(m.ConnectorConfig, name)
    if cfg is None:
        cfg = m.ConnectorConfig(name=name)
        session.add(cfg)
    cfg.settings = settings
    cfg.enabled = name in ADAPTERS
    cfg.updated_at = datetime.now(UTC)
    await session.commit()


async def apply_carrier_event(
    session: AsyncSession, *, tracking_number: str, status: str, location: str, occurred_at: datetime
) -> dict[str, Any] | None:
    """Carrier webhook (ShipStation-shaped). Touches the order row, like real integrations do."""
    sh = (
        await session.execute(select(m.Shipment).where(m.Shipment.tracking_number == tracking_number))
    ).scalar_one_or_none()
    if sh is None:
        return None
    session.add(m.ShipmentEvent(shipment_id=sh.id, status=status, location=location, occurred_at=occurred_at))
    sh.status, sh.updated_at = status, occurred_at
    order = await session.get(m.Order, sh.order_id)
    assert order is not None
    order.updated_at = occurred_at
    if status == "delivered":
        order.status = "delivered"
    await session.commit()
    return {"shipment_id": sh.id, "order_id": order.id, "status": status}
