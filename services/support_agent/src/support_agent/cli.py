"""Operator CLI: `python -m support_agent.cli {migrate|seed|reseed}`."""

import argparse
import asyncio
import json

from sqlalchemy import func, select, text

from support_agent.config import get_settings
from support_agent.data import models as m
from support_agent.data.generate import TABLE_ORDER, default_anchor, generate, load
from support_agent.db import Database
from support_agent.migrate import upgrade


async def seed(database_url: str, force: bool = False) -> dict[str, int] | None:
    db = Database(database_url)
    try:
        async with db.sessionmaker() as session:
            has_data = (await session.execute(select(func.count()).select_from(m.Customer))).scalar_one()
            if has_data and not force:
                return None
            if force:
                tables = ", ".join(["conversation_messages", "conversations", "refund_offers", *reversed(TABLE_ORDER)])
                await session.execute(text(f"TRUNCATE {tables} RESTART IDENTITY CASCADE"))
            return await load(session, generate(anchor=default_anchor()))
    finally:
        await db.dispose()


def main() -> None:
    parser = argparse.ArgumentParser(prog="support_agent.cli")
    parser.add_argument("command", choices=["migrate", "seed", "reseed"])
    args = parser.parse_args()
    url = get_settings().database_url
    upgrade(url)
    if args.command in ("seed", "reseed"):
        counts = asyncio.run(seed(url, force=args.command == "reseed"))
        print(json.dumps(counts or {"skipped": "store already has data"}, indent=2))


if __name__ == "__main__":
    main()
