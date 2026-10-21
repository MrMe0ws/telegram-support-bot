from __future__ import annotations

import asyncio
import logging
import os

from aiogram import BaseMiddleware, Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage

from app.config import Settings
from app.db.session import create_engine, init_db, session_factory
from app.handlers import (
    admin_panel,
    admin_settings,
    ai_callbacks,
    group_topics,
    help_callbacks,
    message_reactions,
    private_chat,
)
from app.i18n import texts_bundle
from app.services.ai.knowledge import KnowledgeLoader
from app.services.ai import model_choice
from app.services.ai.provider import create_provider
from app.services.ai.service import AiService, set_ai_service
from app.services import live_settings
from app.services.help_links import seed_help_links_if_empty
from app.services.remnawave_client import log_remnawave_connectivity
from app.services.topic_status import resolve_waiting_icon

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


class LiveSettingsMiddleware(BaseMiddleware):
    """Обработчики получают настройки с учётом изменений в боте (⚙️ Настройки)."""

    async def __call__(self, handler, event, data):
        data["settings"] = live_settings.current()
        return await handler(event, data)


async def _setup_ai(bot: Bot, sf, settings: Settings) -> None:
    # Сервис создаётся и при выключенном ИИ, если есть ключи: включить его можно в боте.
    ai = settings.ai
    if not ai.is_configured:
        if ai.enabled:
            log.warning(
                "ИИ включён, но ни для одной модели (%s) не задан ключ API "
                "(GEMINI_API_KEY / OPENAI_API_KEY / ANTHROPIC_API_KEY) — ИИ выключен",
                ", ".join(ai.models) or "—",
            )
        return
    try:
        provider = create_provider(ai)
    except Exception as e:
        log.error("ИИ не запущен: %s", e)
        return
    await model_choice.load_saved(sf, provider)
    for spec in provider.models:
        if not provider.has_key(spec):
            log.warning("ИИ: для %s нет ключа API — модель пропускается", spec)
    knowledge = KnowledgeLoader(ai.knowledge_dir)
    knowledge.load()
    service = AiService(bot, sf, settings, provider, knowledge)
    set_ai_service(service)
    live_settings.subscribe(service.apply_settings)
    # WARNING, а не INFO: уровень логов по умолчанию WARNING, а включённый ИИ
    # админ должен видеть при каждом старте.
    main_model, *fallback = provider.models
    log.warning(
        "ИИ %s: %s%s, режим %s, пауза %.0f с, вложения %s",
        "включён" if ai.enabled else "настроен, но выключен (включить: админ-панель → ⚙️ Настройки)",
        main_model,
        f" (резервные: {', '.join(fallback)})" if fallback else "",
        ai.mode,
        ai.reply_delay_sec,
        f"до {ai.media_max_mb} МБ" if ai.media_enabled else "выкл",
    )


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
    settings = await resolve_waiting_icon(bot, settings)
    settings = await live_settings.init(sf, settings)
    dp = Dispatcher(storage=MemoryStorage())
    dp["settings"] = settings
    dp["session_factory"] = sf
    dp.update.outer_middleware(LiveSettingsMiddleware())
    await _setup_ai(bot, sf, settings)

    dp.include_router(admin_settings.router)
    dp.include_router(admin_panel.router)
    dp.include_router(private_chat.router)
    dp.include_router(help_callbacks.router)
    dp.include_router(ai_callbacks.router)
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
