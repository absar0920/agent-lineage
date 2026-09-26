"""Connector interfaces. The agent depends only on these; adapters are swappable."""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal, Protocol

ConnectorKind = Literal["commerce", "helpdesk", "payments", "shipping"]


class ConnectorError(Exception):
    def __init__(self, connector: str, message: str, *, retryable: bool = False) -> None:
        super().__init__(f"{connector}: {message}")
        self.connector = connector
        self.retryable = retryable


@dataclass
class ConnectorStatus:
    name: str
    display_name: str
    kind: ConnectorKind
    connected: bool
    adapter: Literal["mock", "live", "none"]
    last_sync_at: datetime | None = None
    records: dict[str, int] = field(default_factory=dict)


class CommerceConnector(Protocol):
    name: str

    async def health(self) -> bool: ...
    async def find_customer(self, *, email: str | None = None, customer_id: str | None = None) -> dict[str, Any] | None: ...
    async def list_orders(self, customer_id: str, *, product_query: str | None = None,
                          sort: Literal["placed_at", "updated_at"] = "placed_at", limit: int = 20) -> list[dict[str, Any]]: ...
    async def get_order(self, order_id: str) -> dict[str, Any] | None: ...
    async def get_shipment(self, order_id: str) -> dict[str, Any] | None: ...
    async def get_returns(self, order_id: str) -> list[dict[str, Any]]: ...


class PaymentsConnector(Protocol):
    name: str

    async def health(self) -> bool: ...
    async def create_refund_offer(self, *, order_id: str, order_item_id: int, sku: str, amount: str,
                                  status: Literal["offered", "needs_approval"],
                                  idempotency_key: str) -> dict[str, Any]: ...


class HelpdeskConnector(Protocol):
    name: str

    async def health(self) -> bool: ...
    async def create_ticket(self, *, customer_id: str, subject: str, body: str) -> dict[str, Any]: ...
    async def add_comment(self, *, ticket_id: str, body: str, public: bool, author: str) -> dict[str, Any]: ...


@dataclass
class Connectors:
    commerce: CommerceConnector
    payments: PaymentsConnector
    helpdesk: HelpdeskConnector
