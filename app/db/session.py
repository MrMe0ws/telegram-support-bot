from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.db.models import Base


def create_engine(database_url: str):
    return create_async_engine(database_url, echo=False)


async def _ensure_sqlite_migrations(conn) -> None:
    if "sqlite" not in str(conn.engine.url).lower():
        return
    r = await conn.execute(text("PRAGMA table_info(tickets)"))
    cols = {row[1] for row in r.fetchall()}
    if "last_forward_group_msg_id" not in cols:
        await conn.execute(
            text("ALTER TABLE tickets ADD COLUMN last_forward_group_msg_id BIGINT")
        )

    r2 = await conn.execute(text("PRAGMA table_info(help_menu_links)"))
    cols_h = {row[1] for row in r2.fetchall()}
    if cols_h and "body_text" not in cols_h:
        await conn.execute(text("ALTER TABLE help_menu_links ADD COLUMN body_text TEXT"))


async def init_db(engine) -> None:
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await _ensure_sqlite_migrations(conn)


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
