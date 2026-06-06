from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.db.models import Base


def create_engine(database_url: str):
    return create_async_engine(database_url, echo=False)


async def _table_has_column(conn, table: str, column: str) -> bool:
    dialect = conn.engine.dialect.name
    if dialect == "sqlite":
        r = await conn.execute(text(f"PRAGMA table_info({table})"))
        return column in {row[1] for row in r.fetchall()}
    if dialect == "postgresql":
        r = await conn.execute(
            text(
                "SELECT 1 FROM information_schema.columns "
                "WHERE table_schema = current_schema() "
                "AND table_name = :table AND column_name = :column"
            ),
            {"table": table, "column": column},
        )
        return r.scalar_one_or_none() is not None
    return False


async def _ensure_migrations(conn) -> None:
    if not await _table_has_column(conn, "tickets", "last_forward_group_msg_id"):
        await conn.execute(
            text("ALTER TABLE tickets ADD COLUMN last_forward_group_msg_id BIGINT")
        )

    if await _table_has_column(conn, "help_menu_links", "id"):
        if not await _table_has_column(conn, "help_menu_links", "body_text"):
            await conn.execute(
                text("ALTER TABLE help_menu_links ADD COLUMN body_text TEXT")
            )

    if await _table_has_column(conn, "messages", "id"):
        if not await _table_has_column(conn, "messages", "client_message_id"):
            await conn.execute(
                text("ALTER TABLE messages ADD COLUMN client_message_id VARCHAR(36)")
            )
        await conn.execute(
            text(
                "CREATE UNIQUE INDEX IF NOT EXISTS ix_messages_client_message_id "
                "ON messages (client_message_id) WHERE client_message_id IS NOT NULL"
            )
        )

    if await _table_has_column(conn, "tickets", "id"):
        if not await _table_has_column(conn, "tickets", "linked_telegram_id"):
            await conn.execute(
                text("ALTER TABLE tickets ADD COLUMN linked_telegram_id BIGINT")
            )


async def init_db(engine) -> None:
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await _ensure_migrations(conn)


def session_factory(engine):
    return async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)


@asynccontextmanager
async def session_scope(factory: async_sessionmaker[AsyncSession]) -> AsyncIterator[AsyncSession]:
    async with factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
