"""Run Alembic migrations programmatically (used on service start and by the CLI)."""

import asyncio
from pathlib import Path

from alembic import command
from alembic.config import Config

MIGRATIONS = Path(__file__).parent / "migrations"


def alembic_config(database_url: str) -> Config:
    cfg = Config()
    cfg.set_main_option("script_location", str(MIGRATIONS))
    cfg.set_main_option("sqlalchemy.url", database_url)
    return cfg


def upgrade(database_url: str) -> None:
    command.upgrade(alembic_config(database_url), "head")


async def upgrade_async(database_url: str) -> None:
    # Alembic's env.py runs its own event loop; keep it off the caller's loop.
    await asyncio.to_thread(upgrade, database_url)
