"""Deterministic synthetic data for the mock connectors.

Every install (and every test) with the same seed and anchor gets identical data.
All people, emails, phones and addresses are fake (Faker).

The reference incident (SPEC §4.4) is included as customer C-1042.
"""

import random
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from faker import Faker
from sqlalchemy import insert, text
from sqlalchemy.ext.asyncio import AsyncSession

from support_agent.data import models as m

INCIDENT_CUSTOMER = "C-1042"
INCIDENT_OLD_ORDER = "ORD-7781"  # returned + refunded; return processing bumped updated_at
INCIDENT_NEW_ORDER = "ORD-8124"  # delivered; the order the customer means
INCIDENT_SKU = "FW-RIDGE-TR"

CATEGORIES: dict[str, tuple[list[str], list[str], tuple[int, int], bool]] = {
    # category: (prefixes, nouns, price range, refundable)
    "footwear": (
        ["Ridgeline", "Summit", "Basalt", "Cedar", "Talus"],
        ["Trail Shoes", "Hiking Boots", "Approach Shoes", "Camp Sandals"],
        (69, 219),
        True,
    ),
    "jackets": (
        ["Stormline", "Alder", "Granite", "Cirrus", "Fjord"],
        ["Rain Jacket", "Down Jacket", "Softshell", "Fleece Hoodie"],
        (59, 349),
        True,
    ),
    "packs": (
        ["Kestrel", "Cairn", "Scree", "Pika", "Couloir"],
        ["Daypack 22L", "Backpack 45L", "Hydration Pack", "Duffel 60L"],
        (39, 259),
        True,
    ),
    "tents": (["Meadow", "Tarn", "Col", "Bivy", "Saddle"], ["1P Tent", "2P Tent", "Tarp Shelter"], (129, 499), True),
    "sleep": (
        ["Ember", "Drift", "Loft", "Hearth", "Dusk"],
        ["Sleeping Bag", "Sleeping Pad", "Camp Pillow"],
        (29, 329),
        True,
    ),
    "cooking": (["Firebox", "Ember", "Coal", "Kettle"], ["Stove", "Cook Set", "Titanium Mug"], (19, 149), True),
    "accessories": (
        ["Kestrel", "Trailmark", "Switchback", "Ridge"],
        ["Headlamp", "Trekking Poles", "Merino Socks", "Buff"],
        (12, 89),
        True,
    ),
    "nutrition": (["Trailfuel"], ["Energy Bars 12pk", "Electrolyte Mix"], (14, 39), False),
}

CARRIERS = ["UPS", "FedEx", "USPS", "DHL"]
CITIES = [
    "Denver, CO",
    "Salt Lake City, UT",
    "Reno, NV",
    "Memphis, TN",
    "Louisville, KY",
    "Columbus, OH",
    "Portland, OR",
    "Phoenix, AZ",
    "Chicago, IL",
    "Dallas, TX",
]
RETURN_REASONS = [
    "too small",
    "too large",
    "changed mind",
    "defective",
    "not as described",
    "arrived late",
    "wrong item",
]
TICKET_TEMPLATES = [
    ("Where is my order {order}?", ["shipping", "wismo"]),
    ("Refund request for {item}", ["refund"]),
    ("{item} is too small, can I exchange?", ["exchange", "sizing"]),
    ("Package marked delivered but not received ({order})", ["shipping", "lost"]),
    ("Damaged {item} on arrival", ["damaged", "refund"]),
    ("Change shipping address for {order}", ["address-change"]),
    ("Question about sizing for {item}", ["sizing", "pre-sale"]),
    ("Return label for {order}", ["returns"]),
    ("Charged twice for {order}?", ["billing"]),
]


@dataclass
class Dataset:
    rows: dict[str, list[dict[str, Any]]] = field(default_factory=dict)

    def add(self, table: str, row: dict[str, Any]) -> None:
        self.rows.setdefault(table, []).append(row)

    def count(self, table: str) -> int:
        return len(self.rows.get(table, []))


def money(value: float) -> Decimal:
    return Decimal(f"{value:.2f}")


def generate(
    anchor: datetime, seed: int = 20260922, customers: int = 1000, orders: int = 4500, tickets: int = 1200
) -> Dataset:
    rng = random.Random(seed)
    fake = Faker("en_US")
    fake.seed_instance(seed)
    ds = Dataset()

    # --- products ---
    products: list[dict[str, Any]] = []
    for category, (prefixes, nouns, (lo, hi), refundable) in CATEGORIES.items():
        for prefix in prefixes:
            for noun in nouns:
                if len(products) >= 120:
                    break
                sku = f"{category[:2].upper()}-{prefix[:5].upper()}-{''.join(w[0] for w in noun.split()).upper()}"
                if any(p["sku"] == sku for p in products):
                    continue
                products.append(
                    {
                        "sku": sku,
                        "name": f"{prefix} {noun}",
                        "category": category,
                        "price": money(rng.randint(lo, hi) - 0.01),
                        "refundable": refundable,
                    }
                )
    ridge = next(p for p in products if p["name"] == "Ridgeline Trail Shoes")
    ridge.update(sku=INCIDENT_SKU, price=money(129))
    for p in products:
        ds.add("products", p)

    # --- customers ---
    customer_ids = [f"C-{1000 + i}" for i in range(customers)]
    for cid in customer_ids:
        first, last = fake.first_name(), fake.last_name()
        ds.add(
            "customers",
            {
                "id": cid,
                "name": f"{first} {last}",
                "email": f"{first}.{last}.{cid[2:]}@{fake.free_email_domain()}".lower(),
                "phone": fake.numerify("+1-###-###-####"),
                "address": fake.address().replace("\n", ", "),
                "created_at": anchor - timedelta(days=rng.randint(30, 900)),
            },
        )
    ds.rows["customers"][customer_ids.index(INCIDENT_CUSTOMER)].update(
        name="Sara Qureshi", email="sara.qureshi@example.com"
    )

    # --- orders (18 months, seasonal peaks in Nov/Dec and May/Jun) ---
    start = anchor - timedelta(days=548)
    placed: list[datetime] = []
    while len(placed) < orders:
        t = start + timedelta(seconds=rng.randint(0, int((anchor - start).total_seconds()) - 86400 * 2))
        weight = 2.2 if t.month in (11, 12) else 1.5 if t.month in (5, 6) else 1.0
        if rng.random() < weight / 2.2:
            placed.append(t)
    placed.sort()

    # ~6% of customers buy the same product twice -> natural "which order?" ambiguity.
    repeat_buyers = set(rng.sample(customer_ids, k=int(customers * 0.06)))
    last_purchase: dict[str, str] = {}
    item_id = 1
    next_num = 3600
    reserved = {INCIDENT_OLD_ORDER, INCIDENT_NEW_ORDER}
    for t in placed:
        order_id = f"ORD-{next_num}"
        while order_id in reserved:
            next_num += 1
            order_id = f"ORD-{next_num}"
        next_num += 1
        cid = rng.choice(customer_ids)
        if cid == INCIDENT_CUSTOMER:
            cid = customer_ids[0]
        chosen = rng.sample(products, k=rng.choices([1, 2, 3, 4], weights=[40, 32, 18, 10])[0])
        if cid in repeat_buyers and cid in last_purchase and rng.random() < 0.5:
            chosen[0] = next(p for p in products if p["sku"] == last_purchase[cid])
        last_purchase[cid] = chosen[0]["sku"]
        item_id = _add_order(ds, rng, fake, anchor, order_id, cid, t, chosen, item_id)

    item_id = _add_incident(ds, anchor, ridge, item_id)

    # --- helpdesk tickets ---
    orders_by_customer: dict[str, list[dict[str, Any]]] = {}
    for o in ds.rows["orders"]:
        orders_by_customer.setdefault(o["customer_id"], []).append(o)
    items_by_order: dict[str, list[dict[str, Any]]] = {}
    for it in ds.rows["order_items"]:
        items_by_order.setdefault(it["order_id"], []).append(it)
    for n in range(tickets):
        cid = rng.choice([c for c in orders_by_customer if c != INCIDENT_CUSTOMER])
        order = rng.choice(orders_by_customer[cid])
        item = items_by_order[order["id"]][0]
        template, tags = rng.choice(TICKET_TEMPLATES)
        created = order["placed_at"] + timedelta(days=rng.randint(1, 40), hours=rng.randint(0, 23))
        created = min(created, anchor - timedelta(hours=1))
        solved = created < anchor - timedelta(days=5) or rng.random() < 0.5
        tid = f"TCK-{20000 + n}"
        ds.add(
            "tickets",
            {
                "id": tid,
                "customer_id": cid,
                "subject": template.format(order=order["id"], item=item["name"]),
                "status": "solved" if solved else rng.choice(["new", "open", "pending"]),
                "tags": tags,
                "created_at": created,
                "updated_at": created + timedelta(hours=rng.randint(1, 72)),
            },
        )
        ds.add(
            "ticket_comments",
            {
                "ticket_id": tid,
                "author": "customer",
                "public": True,
                "body": fake.paragraph(nb_sentences=3),
                "created_at": created,
            },
        )
        if solved:
            ds.add(
                "ticket_comments",
                {
                    "ticket_id": tid,
                    "author": "agent",
                    "public": True,
                    "body": fake.paragraph(nb_sentences=2),
                    "created_at": created + timedelta(hours=rng.randint(1, 30)),
                },
            )

    for name, enabled in [
        ("shopify", True),
        ("zendesk", True),
        ("stripe", True),
        ("woocommerce", False),
        ("gorgias", False),
        ("intercom", False),
        ("freshdesk", False),
        ("shipstation", False),
    ]:
        ds.add(
            "connector_configs",
            {
                "name": name,
                "enabled": enabled,
                "settings": {},
                "last_sync_at": anchor - timedelta(minutes=rng.randint(2, 50)) if enabled else None,
                "updated_at": anchor,
            },
        )
    return ds


def _add_order(
    ds: Dataset,
    rng: random.Random,
    fake: Faker,
    anchor: datetime,
    order_id: str,
    cid: str,
    t: datetime,
    products: list[dict[str, Any]],
    item_id: int,
) -> int:
    age = (anchor - t).days
    status = "delivered" if age > 9 else rng.choice(["paid", "fulfilled", "shipped", "delivered"])
    if rng.random() < 0.02:
        status = "cancelled"
    total = Decimal(0)
    order_items = []
    for p in products:
        qty = rng.choices([1, 2], weights=[90, 10])[0]
        order_items.append(
            {"id": item_id, "order_id": order_id, "sku": p["sku"], "name": p["name"], "price": p["price"], "qty": qty}
        )
        total += p["price"] * qty
        item_id += 1
    updated = t + timedelta(days=min(age, rng.randint(2, 8)))
    ds.add(
        "orders",
        {
            "id": order_id,
            "customer_id": cid,
            "placed_at": t,
            "updated_at": updated,
            "status": status,
            "total": total,
            "currency": "USD",
        },
    )
    for it in order_items:
        ds.add("order_items", it)
    pay_id = f"ch_{fake.hexify('^' * 24)}"
    ds.add(
        "payments",
        {
            "id": pay_id,
            "order_id": order_id,
            "amount": total,
            "status": "refunded" if status == "cancelled" else "succeeded",
            "created_at": t,
        },
    )
    if status in ("shipped", "delivered"):
        _add_shipment(ds, rng, fake, order_id, t, status, anchor)
    if status == "delivered":
        for it in order_items:
            if rng.random() < 0.08:
                requested = updated + timedelta(days=rng.randint(1, 20))
                if requested >= anchor:
                    continue
                rstatus = rng.choice(["requested", "received", "refunded", "refunded"])
                received = requested + timedelta(days=rng.randint(3, 9)) if rstatus != "requested" else None
                if received and received >= anchor:
                    rstatus, received = "requested", None
                ret_id = f"RET-{50000 + ds.count('returns')}"
                amount = it["price"] * it["qty"]
                ds.add(
                    "returns",
                    {
                        "id": ret_id,
                        "order_item_id": it["id"],
                        "reason": rng.choice(RETURN_REASONS),
                        "status": rstatus,
                        "requested_at": requested,
                        "received_at": received,
                        "refund_amount": amount if rstatus == "refunded" else None,
                    },
                )
                if rstatus == "refunded" and received:
                    ds.add(
                        "refunds",
                        {
                            "id": f"re_{fake.hexify('^' * 24)}",
                            "payment_id": pay_id,
                            "order_item_id": it["id"],
                            "amount": amount,
                            "status": "succeeded",
                            "created_at": received + timedelta(hours=6),
                        },
                    )
                    # Return processing touches the order row — the root of the reference incident.
                    ds.rows["orders"][-1]["updated_at"] = received + timedelta(hours=6)
    return item_id


def _add_shipment(
    ds: Dataset, rng: random.Random, fake: Faker, order_id: str, t: datetime, status: str, anchor: datetime
) -> None:
    ship_id = f"SHP-{order_id[4:]}"
    carrier = rng.choice(CARRIERS)
    events = [("label_created", t + timedelta(hours=rng.randint(4, 30)))]
    events.append(("in_transit", events[-1][1] + timedelta(hours=rng.randint(6, 30))))
    final = "in_transit"
    if status == "delivered":
        roll = rng.random()
        final = "lost" if roll < 0.01 else "delivered"
        if roll < 0.05:
            events.append(("delayed", events[-1][1] + timedelta(days=rng.randint(1, 3))))
        events.append((final, events[-1][1] + timedelta(days=rng.randint(1, 5))))
    events = [(s, min(ts, anchor - timedelta(minutes=5))) for s, ts in events]
    ds.add(
        "shipments",
        {
            "id": ship_id,
            "order_id": order_id,
            "carrier": carrier,
            "tracking_number": fake.bothify("1Z#########???").upper(),
            "status": final,
            "updated_at": events[-1][1],
        },
    )
    for s, ts in events:
        ds.add(
            "shipment_events", {"shipment_id": ship_id, "status": s, "location": rng.choice(CITIES), "occurred_at": ts}
        )


def _add_incident(ds: Dataset, anchor: datetime, ridge: dict[str, Any], item_id: int) -> int:
    """Two orders of the same shoes; the older one was returned and refunded recently."""
    old_placed = anchor - timedelta(days=35)
    old_returned = anchor - timedelta(days=3)
    old_updated = anchor - timedelta(days=2)
    new_placed = anchor - timedelta(days=10)
    new_updated = anchor - timedelta(days=7)
    for oid, placed, updated in [
        (INCIDENT_OLD_ORDER, old_placed, old_updated),
        (INCIDENT_NEW_ORDER, new_placed, new_updated),
    ]:
        ds.add(
            "orders",
            {
                "id": oid,
                "customer_id": INCIDENT_CUSTOMER,
                "placed_at": placed,
                "updated_at": updated,
                "status": "delivered",
                "total": ridge["price"],
                "currency": "USD",
            },
        )
        ds.add(
            "order_items",
            {
                "id": item_id,
                "order_id": oid,
                "sku": INCIDENT_SKU,
                "name": ridge["name"],
                "price": ridge["price"],
                "qty": 1,
            },
        )
        pay_id = f"ch_incident_{oid[4:]}"
        ds.add(
            "payments",
            {"id": pay_id, "order_id": oid, "amount": ridge["price"], "status": "succeeded", "created_at": placed},
        )
        ship_id = f"SHP-{oid[4:]}"
        delivered = placed + timedelta(days=3)
        ds.add(
            "shipments",
            {
                "id": ship_id,
                "order_id": oid,
                "carrier": "UPS",
                "tracking_number": f"1ZKESTREL{oid[4:]}",
                "status": "delivered",
                "updated_at": delivered,
            },
        )
        for s, ts in [
            ("label_created", placed + timedelta(hours=8)),
            ("in_transit", placed + timedelta(days=1)),
            ("delivered", delivered),
        ]:
            ds.add(
                "shipment_events", {"shipment_id": ship_id, "status": s, "location": "Denver, CO", "occurred_at": ts}
            )
        if oid == INCIDENT_OLD_ORDER:
            ds.add(
                "returns",
                {
                    "id": "RET-49990",
                    "order_item_id": item_id,
                    "reason": "too small",
                    "status": "refunded",
                    "requested_at": old_returned - timedelta(days=6),
                    "received_at": old_returned,
                    "refund_amount": ridge["price"],
                },
            )
            ds.add(
                "refunds",
                {
                    "id": "re_incident_7781",
                    "payment_id": pay_id,
                    "order_item_id": item_id,
                    "amount": ridge["price"],
                    "status": "succeeded",
                    "created_at": old_returned,
                },
            )
        item_id += 1
    return item_id


TABLE_ORDER = [
    "products",
    "customers",
    "orders",
    "order_items",
    "payments",
    "refunds",
    "shipments",
    "shipment_events",
    "returns",
    "tickets",
    "ticket_comments",
    "connector_configs",
]
MODELS: dict[str, type[m.Base]] = {
    "products": m.Product,
    "customers": m.Customer,
    "orders": m.Order,
    "order_items": m.OrderItem,
    "payments": m.Payment,
    "refunds": m.Refund,
    "shipments": m.Shipment,
    "shipment_events": m.ShipmentEvent,
    "returns": m.Return,
    "tickets": m.Ticket,
    "ticket_comments": m.TicketComment,
    "connector_configs": m.ConnectorConfig,
}


async def load(session: AsyncSession, ds: Dataset, chunk: int = 2000) -> dict[str, int]:
    for table in TABLE_ORDER:
        rows = ds.rows.get(table, [])
        for i in range(0, len(rows), chunk):
            await session.execute(insert(MODELS[table]), rows[i : i + chunk])
    # Explicit ids were inserted; move serial sequences past them.
    for table in ("order_items", "shipment_events", "ticket_comments"):
        await session.execute(
            text(f"SELECT setval(pg_get_serial_sequence('{table}', 'id'), COALESCE(MAX(id), 1)) FROM {table}")  # noqa: S608
        )
    await session.commit()
    return {t: ds.count(t) for t in TABLE_ORDER}


def default_anchor() -> datetime:
    return datetime.now(UTC).replace(minute=0, second=0, microsecond=0)
