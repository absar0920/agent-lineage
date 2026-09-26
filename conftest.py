"""Shared fixtures: one disposable Postgres (testcontainers) with `store` and `lineage` databases."""

import os
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime

import pytest
from sqlalchemy import create_engine, text

# Settings required at import time by both services; tests never call a real LLM.
os.environ.setdefault("LLM_MODE", "recorded")
os.environ.setdefault("LINEAGE_REDACTION_KEY", "test-redaction-key-0123456789abcdef")
os.environ.setdefault("SERVICE_TOKEN", "test-service-token-0123456789abcdef")
os.environ.setdefault("SESSION_SECRET", "test-session-secret-0123456789abcdef")

ANCHOR = datetime(2026, 9, 22, 10, 0, tzinfo=UTC)


@pytest.fixture(scope="session")
def pg_urls() -> Iterator[dict[str, str]]:
    from testcontainers.community.postgres import PostgresContainer

    with PostgresContainer("postgres:16", driver="psycopg2") as pg:
        admin = create_engine(pg.get_connection_url(), isolation_level="AUTOCOMMIT")
        with admin.connect() as c:
            for db in ("store", "lineage"):
                c.execute(text(f"CREATE DATABASE {db}"))
        admin.dispose()
        base = pg.get_connection_url().rsplit("/", 1)[0].replace("postgresql+psycopg2", "postgresql+asyncpg")
        urls = {"store": f"{base}/store", "lineage": f"{base}/lineage"}
        from lineage_service.migrate import upgrade as upgrade_lineage
        from support_agent.migrate import upgrade as upgrade_store

        upgrade_store(urls["store"])
        upgrade_lineage(urls["lineage"])
        yield urls


@pytest.fixture(scope="session")
def small_dataset():  # type: ignore[no-untyped-def]
    from support_agent.data.generate import generate

    return generate(anchor=ANCHOR, customers=120, orders=400, tickets=60)


@pytest.fixture
async def store_db(pg_urls, small_dataset) -> AsyncIterator["object"]:  # type: ignore[no-untyped-def]
    """Fresh store data for each test (truncate + load the small deterministic dataset)."""
    from support_agent.data.generate import TABLE_ORDER, load
    from support_agent.db import Database

    db = Database(pg_urls["store"])
    async with db.sessionmaker() as s:
        tables = ", ".join(["conversation_messages", "conversations", "refund_offers", *reversed(TABLE_ORDER)])
        await s.execute(text(f"TRUNCATE {tables} RESTART IDENTITY CASCADE"))
        await load(s, small_dataset)
    yield db
    await db.dispose()
