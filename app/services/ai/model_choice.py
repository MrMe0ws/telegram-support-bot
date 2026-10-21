"""
Цепочка моделей ИИ, выбранная админом в боте. Хранится в БД и после перезапуска
важнее AI_MODEL / AI_FALLBACK_MODELS из .env; «Как в .env» её сбрасывает.
"""

from __future__ import annotations

import logging

from app.config import parse_ai_models
from app.services import bot_settings
from app.services.ai.provider import ModelChain

log = logging.getLogger(__name__)


async def load_saved(session_factory, chain: ModelChain) -> None:
    async with session_factory() as session:
        raw = await bot_settings.get_value(session, bot_settings.AI_MODELS_KEY)
    if not raw:
        return
    try:
        chain.set_models(parse_ai_models(raw, "gemini"))
    except ValueError as e:
        log.warning("ИИ: сохранённый в боте список моделей не подошёл (%s) — беру из .env", e)


async def save(session_factory, chain: ModelChain, models: tuple[str, ...]) -> None:
    """Применить и запомнить цепочку. ValueError — цепочка не годится."""
    chain.set_models(models)
    async with session_factory() as session:
        async with session.begin():
            await bot_settings.set_value(session, bot_settings.AI_MODELS_KEY, ",".join(chain.models))


async def reset(session_factory, chain: ModelChain) -> None:
    chain.set_models(chain.env_models)
    async with session_factory() as session:
        async with session.begin():
            await bot_settings.delete_value(session, bot_settings.AI_MODELS_KEY)


def is_overridden(chain: ModelChain) -> bool:
    return chain.models != chain.env_models
