"""Retention: delete unpinned runs older than the configured number of days (SPEC §5.7)."""

import asyncio
import logging
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from lineage_service.models import AppSetting, IngestedEvent, Run

log = logging.getLogger("lineage.retention")
SETTING_KEY = "retention_days"


async def retention_days(session: AsyncSession, default: int) -> int:
    row = await session.get(AppSetting, SETTING_KEY)
    return int(row.value) if row else default


async def purge(session: AsyncSession, days: int, now: datetime | None = None) -> int:
    cutoff = (now or datetime.now(UTC)) - timedelta(days=days)
    result = await session.execute(
        delete(Run).where(Run.started_at < cutoff, Run.pinned.is_(False)).returning(Run.id))
    deleted = len(result.all())
    # The idempotency ledger only needs to outlive SDK retries (minutes); keep a day.
    await session.execute(delete(IngestedEvent).where(IngestedEvent.received_at < (now or datetime.now(UTC)) - timedelta(days=1)))
    await session.commit()
    return deleted


async def retention_loop(sm: async_sessionmaker[AsyncSession], default_days: int,
                         interval_s: float = 6 * 3600) -> None:
    while True:
        try:
            async with sm() as session:
                days = await retention_days(session, default_days)
                deleted = await purge(session, days)
            log.info("retention pass complete", extra={"event": "retention", "deleted": deleted})
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("retention pass failed")
        await asyncio.sleep(interval_s)


async def list_settings(session: AsyncSession, default_days: int) -> dict[str, int]:
    rows = {r.key: r.value for r in (await session.execute(select(AppSetting))).scalars()}
    return {"retention_days": int(rows.get(SETTING_KEY, default_days))}
