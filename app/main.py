from __future__ import annotations

import asyncio
import logging
import os

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage

from app.config import Settings
from app.db.session import create_engine, init_db, session_factory
from app.handlers import (
    admin_panel,
    group_topics,
    help_callbacks,
    message_reactions,
    private_chat,
)
from app.i18n import texts_bundle
from app.services.help_links import seed_help_links_if_empty
from app.services.remnawave_client import log_remnawave_connectivity

try:
    from app.http.server import create_app, start_http
except ImportError:
    create_app = None  # type: ignore
    start_http = None  # type: ignore


def _configure_logging() -> None:
    """Уровень из LOG_LEVEL (по умолчанию WARNING). Лог aiogram «Update handled» только при DEBUG."""
    raw = os.environ.get("LOG_LEVEL", "WARNING").strip().upper()
    level = getattr(logging, raw, None)
    if not isinstance(level, int):
        level = logging.WARNING
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        force=True,
    )
    aiogram_event = logging.getLogger("aiogram.event")
    if level <= logging.DEBUG:
        aiogram_event.setLevel(logging.DEBUG)
    else:
        aiogram_event.setLevel(logging.WARNING)


_configure_logging()
log = logging.getLogger(__name__)


async def main() -> None:
    texts_bundle()
    settings = Settings.from_env()
    await log_remnawave_connectivity(settings.remnawave)
    engine = create_engine(settings.database_url)
    await init_db(engine)
    sf = session_factory(engine)

    async with sf() as session:
        async with session.begin():
            await seed_help_links_if_empty(session)

    bot = Bot(
        settings.bot_token,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    dp = Dispatcher(storage=MemoryStorage())
    dp["settings"] = settings
    dp["session_factory"] = sf

    dp.include_router(admin_panel.router)
    dp.include_router(private_chat.router)
    dp.include_router(help_callbacks.router)
    dp.include_router(message_reactions.router)
    dp.include_router(group_topics.router)

    http_runner = None
    if (
        settings.support_bridge_secret
        and create_app is not None
        and start_http is not None
        and settings.http_port > 0
    ):
        http_app = create_app(bot, sf, settings)
        http_runner = await start_http(http_app, settings.http_port)

    try:
        await dp.start_polling(bot)
    finally:
        if http_runner is not None:
            await http_runner.cleanup()


if __name__ == "__main__":
    asyncio.run(main())
