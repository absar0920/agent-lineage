"""Mock adapters backed by the `store` database.

They mimic the real APIs' shapes, IDs and latency, and can inject retryable
errors (CONNECTOR_ERROR_RATE) so retry paths are exercised.
"""

import asyncio
import hashlib
import random
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Literal

from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from support_agent.connectors.base import ConnectorError
from support_agent.data import models as m


def _iso(dt: datetime | None) -> str | None:
    return dt.astimezone(UTC).isoformat() if dt else None


def _money(d: Decimal | None) -> str | None:
    return f"{d:.2f}" if d is not None else None


class _MockBase:
    name = "mock"

    def __init__(self, sessionmaker: async_sessionmaker[AsyncSession], *,
                 latency_ms: tuple[int, int] = (20, 150), error_rate: float = 0.0,
                 rng: random.Random | None = None) -> None:
        self._sm = sessionmaker
        self._latency = latency_ms
        self._error_rate = error_rate
        self._rng = rng or random.Random()

    async def _simulate(self) -> None:
        lo, hi = self._latency
        if hi > 0:
            await asyncio.sleep(self._rng.randint(lo, hi) / 1000)
        if self._error_rate and self._rng.random() < self._error_rate:
            raise ConnectorError(self.name, "503 Service Unavailable (simulated)", retryable=True)

    async def health(self) -> bool:
        async with self._sm() as s:
            await s.execute(select(1))
        return True


def order_to_dict(o: m.Order) -> dict[str, Any]:
    return {
        "order_id": o.id,
        "customer_id": o.customer_id,
        "placed_at": _iso(o.placed_at),
        "updated_at": _iso(o.updated_at),
        "status": o.status,
        "total": _money(o.total),
        "currency": o.currency,
        "items": [{"order_item_id": i.id, "sku": i.sku, "name": i.name, "price": _money(i.price), "qty": i.qty}
                  for i in o.items],
    }


class MockShopify(_MockBase):
    name = "shopify"

    async def find_customer(self, *, email: str | None = None, customer_id: str | None = None) -> dict[str, Any] | None:
        await self._simulate()
        async with self._sm() as s:
            q = select(m.Customer)
            q = q.where(func.lower(m.Customer.email) == email.lower()) if email else q.where(m.Customer.id == customer_id)
            c = (await s.execute(q)).scalar_one_or_none()
        if not c:
            return None
        return {"customer_id": c.id, "full_name": c.name, "email": c.email, "phone": c.phone}

    async def list_orders(self, customer_id: str, *, product_query: str | None = None,
                          sort: Literal["placed_at", "updated_at"] = "placed_at", limit: int = 20) -> list[dict[str, Any]]:
        await self._simulate()
        order_col = m.Order.updated_at if sort == "updated_at" else m.Order.placed_at
        async with self._sm() as s:
            q = select(m.Order).where(m.Order.customer_id == customer_id)
            if product_query:
                words = [w for w in product_query.lower().split() if len(w) > 2]
                if words:
                    q = q.where(m.Order.id.in_(select(m.OrderItem.order_id).where(
                        or_(*[func.lower(m.OrderItem.name).contains(w) for w in words]))))
            rows = (await s.execute(q.order_by(order_col.desc()).limit(limit))).scalars().all()
        return [order_to_dict(o) for o in rows]

    async def get_order(self, order_id: str) -> dict[str, Any] | None:
        await self._simulate()
        async with self._sm() as s:
            o = await s.get(m.Order, order_id)
            return order_to_dict(o) if o else None

    async def get_shipment(self, order_id: str) -> dict[str, Any] | None:
        await self._simulate()
        async with self._sm() as s:
            sh = (await s.execute(select(m.Shipment).where(m.Shipment.order_id == order_id))).scalar_one_or_none()
            if not sh:
                return None
            events = (await s.execute(select(m.ShipmentEvent).where(m.ShipmentEvent.shipment_id == sh.id)
                                      .order_by(m.ShipmentEvent.occurred_at))).scalars().all()
        return {"shipment_id": sh.id, "carrier": sh.carrier, "tracking_number": sh.tracking_number,
                "status": sh.status, "updated_at": _iso(sh.updated_at),
                "events": [{"status": e.status, "location": e.location, "at": _iso(e.occurred_at)} for e in events]}

    async def get_returns(self, order_id: str) -> list[dict[str, Any]]:
        await self._simulate()
        async with self._sm() as s:
            rows = (await s.execute(select(m.Return, m.OrderItem).join(m.OrderItem, m.Return.order_item_id == m.OrderItem.id)
                                    .where(m.OrderItem.order_id == order_id))).all()
        return [{"return_id": r.id, "order_item_id": r.order_item_id, "sku": i.sku, "status": r.status,
                 "reason": r.reason, "refund_amount": _money(r.refund_amount)} for r, i in rows]


class MockStripe(_MockBase):
    name = "stripe"

    async def create_refund_offer(self, *, order_id: str, order_item_id: int, sku: str, amount: str,
                                  status: Literal["offered", "needs_approval"], idempotency_key: str) -> dict[str, Any]:
        await self._simulate()
        offer_id = "RF-" + hashlib.sha256(idempotency_key.encode()).hexdigest()[:8].upper()
        async with self._sm() as s:
            existing = (await s.execute(select(m.RefundOffer).where(m.RefundOffer.idempotency_key == idempotency_key))).scalar_one_or_none()
            if existing is None:
                s.add(m.RefundOffer(id=offer_id, order_id=order_id, order_item_id=order_item_id, sku=sku,
                                    amount=Decimal(amount), status=status, idempotency_key=idempotency_key))
                try:
                    await s.commit()
                except IntegrityError:
                    await s.rollback()
            else:
                offer_id, status = existing.id, existing.status  # type: ignore[assignment]
        return {"refund_offer_id": offer_id, "order_id": order_id, "sku": sku, "amount": amount, "status": status}


class MockZendesk(_MockBase):
    name = "zendesk"

    async def create_ticket(self, *, customer_id: str, subject: str, body: str) -> dict[str, Any]:
        await self._simulate()
        now = datetime.now(UTC)
        async with self._sm() as s:
            n = (await s.execute(select(func.count()).select_from(m.Ticket))).scalar_one()
            tid = f"TCK-{20000 + n}"
            s.add(m.Ticket(id=tid, customer_id=customer_id, subject=subject[:200], status="open",
                           tags=["ai-agent"], created_at=now, updated_at=now))
            await s.flush()
            s.add(m.TicketComment(ticket_id=tid, author="customer", public=True, body=body, created_at=now))
            await s.commit()
        return {"ticket_id": tid, "status": "open"}

    async def add_comment(self, *, ticket_id: str, body: str, public: bool, author: str) -> dict[str, Any]:
        await self._simulate()
        now = datetime.now(UTC)
        async with self._sm() as s:
            t = await s.get(m.Ticket, ticket_id)
            if t is None:
                raise ConnectorError(self.name, f"ticket {ticket_id} not found")
            c = m.TicketComment(ticket_id=ticket_id, author=author, public=public, body=body, created_at=now)
            s.add(c)
            t.updated_at = now
            await s.commit()
            return {"ticket_id": ticket_id, "comment_id": c.id, "public": public}


class DryRunPayments:
    """Used during replay: never writes, returns deterministic IDs."""

    name = "stripe:dry-run"

    async def health(self) -> bool:
        return True

    async def create_refund_offer(self, *, order_id: str, order_item_id: int, sku: str, amount: str,
                                  status: Literal["offered", "needs_approval"], idempotency_key: str) -> dict[str, Any]:
        offer_id = "RF-" + hashlib.sha256(idempotency_key.encode()).hexdigest()[:8].upper()
        return {"refund_offer_id": offer_id, "order_id": order_id, "sku": sku, "amount": amount,
                "status": status, "dry_run": True}


class DryRunHelpdesk:
    name = "zendesk:dry-run"

    async def health(self) -> bool:
        return True

    async def create_ticket(self, *, customer_id: str, subject: str, body: str) -> dict[str, Any]:
        return {"ticket_id": "TCK-DRYRUN", "status": "open", "dry_run": True}

    async def add_comment(self, *, ticket_id: str, body: str, public: bool, author: str) -> dict[str, Any]:
        return {"ticket_id": ticket_id, "comment_id": 0, "public": public, "dry_run": True}
