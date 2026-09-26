import asyncio

from alembic import context
from sqlalchemy.ext.asyncio import create_async_engine

from lineage_service.db import Base
from lineage_service import models_registry  # noqa: F401  (registers all tables on Base.metadata)

config = context.config
target_metadata = Base.metadata


def _run(connection) -> None:  # type: ignore[no-untyped-def]
    context.configure(connection=connection, target_metadata=target_metadata, compare_type=True)
    with context.begin_transaction():
        context.run_migrations()


async def _main() -> None:
    engine = create_async_engine(config.get_main_option("sqlalchemy.url"))
    async with engine.connect() as conn:
        await conn.run_sync(_run)
    await engine.dispose()


asyncio.run(_main())
