from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import BotSetting

# Цепочка моделей ИИ, выбранная в админ-панели: «провайдер:модель» через запятую.
AI_MODELS_KEY = "ai_models"


async def get_value(session: AsyncSession, key: str) -> str | None:
    row = await session.get(BotSetting, key)
    return row.value if row is not None else None


async def set_value(session: AsyncSession, key: str, value: str) -> None:
    row = await session.get(BotSetting, key)
    if row is None:
        session.add(BotSetting(key=key, value=value))
    else:
        row.value = value


async def delete_value(session: AsyncSession, key: str) -> None:
    row = await session.get(BotSetting, key)
    if row is not None:
        await session.delete(row)
