"""
Значок темы «ждёт человека»: по списку тем видно, где клиенту нужен сотрудник.

Значок ставится, когда клиент написал, а отвечать будет человек (ИИ выключен,
молчит в этом тикете или готовит только черновики), когда ИИ передал тикет
людям или не смог ответить. Снимается, когда клиенту ответили — сотрудник или ИИ.
После первого ответа сотрудника значок до закрытия тикета больше не ставится.

Уведомления «бот изменил значок темы» бот удаляет (handlers/group_topics.py).
"""

from __future__ import annotations

import dataclasses
import logging

from aiogram import Bot
from sqlalchemy import select, update

from app.config import Settings
from app.db.models import Ticket, TicketStatus

log = logging.getLogger(__name__)

_VARIATION_SELECTOR = "️"


def _norm_emoji(value: str) -> str:
    return value.replace(_VARIATION_SELECTOR, "").strip()


async def resolve_waiting_icon(bot: Bot, settings: Settings) -> Settings:
    """
    TOPIC_ICON_EMOJI_WAITING можно задать ID кастомного эмодзи или самим эмодзи
    (❗, 🔥…). Эмодзи переводится в ID по набору значков тем Telegram —
    другие эмодзи значком темы быть не могут.
    """
    raw = settings.topic_icon_emoji_waiting
    if not raw or raw.isdigit():
        return settings
    try:
        stickers = await bot.get_forum_topic_icon_stickers()
    except Exception as e:
        log.warning("Значок «ждёт человека» выключен: не получен набор значков тем: %s", e)
        return dataclasses.replace(settings, topic_icon_emoji_waiting="")
    wanted = _norm_emoji(raw)
    for sticker in stickers:
        if sticker.custom_emoji_id and _norm_emoji(sticker.emoji or "") == wanted:
            return dataclasses.replace(settings, topic_icon_emoji_waiting=sticker.custom_emoji_id)
    available = " ".join(s.emoji for s in stickers if s.emoji)
    log.warning(
        "Значок «ждёт человека» выключен: %s нет среди значков тем Telegram. Доступны: %s",
        raw,
        available,
    )
    return dataclasses.replace(settings, topic_icon_emoji_waiting="")


async def set_waiting(
    bot: Bot,
    session_factory,
    settings: Settings,
    ticket_id: int,
    waiting: bool,
    *,
    by_staff: bool = False,
) -> None:
    """
    Поставить или снять значок. Telegram дёргается, только если состояние сменилось.

    Значок ставится один раз — пока сотрудник не ответил. После ответа сотрудника
    (by_staff) тема до закрытия остаётся «открытой», чтобы новые сообщения клиента
    не гоняли значок туда-обратно.
    """
    icon_waiting = settings.topic_icon_emoji_waiting
    if not icon_waiting:
        return
    async with session_factory() as session:
        async with session.begin():
            if by_staff:
                await session.execute(
                    update(Ticket).where(Ticket.id == ticket_id).values(staff_replied=True)
                )
            clauses = [
                Ticket.id == ticket_id,
                Ticket.status == TicketStatus.open.value,
                Ticket.awaiting_human != waiting,
            ]
            if waiting:
                clauses.append(Ticket.staff_replied.is_(False))
            changed = await session.execute(
                update(Ticket).where(*clauses).values(awaiting_human=waiting)
            )
            if changed.rowcount == 0:
                return
            row = (
                await session.execute(
                    select(Ticket.forum_chat_id, Ticket.thread_id).where(Ticket.id == ticket_id)
                )
            ).one()
    forum_chat_id, thread_id = row
    if thread_id is None:
        return
    try:
        await bot.edit_forum_topic(
            chat_id=forum_chat_id,
            message_thread_id=thread_id,
            # Пустая строка убирает значок — так и задумано, если «открытый» не задан.
            icon_custom_emoji_id=icon_waiting if waiting else settings.topic_icon_emoji_open,
        )
    except Exception as e:
        if "TOPIC_NOT_MODIFIED" not in str(e):
            log.warning("Значок темы тикета %s не сменён: %s", ticket_id, e)


async def after_client_message(bot: Bot, session_factory, settings: Settings, ticket_id: int) -> None:
    """Клиент написал: если ответит не ИИ — тема ждёт человека."""
    if not settings.topic_icon_emoji_waiting:
        return
    from app.services.ai.service import get_ai_service

    ai = get_ai_service()
    if ai is not None and ai.mode == "auto":
        async with session_factory() as session:
            paused = (
                await session.execute(select(Ticket.ai_paused).where(Ticket.id == ticket_id))
            ).scalar_one_or_none()
        # ИИ ответит сам; не сможет — поставит значок он же.
        if paused is False:
            return
    await set_waiting(bot, session_factory, settings, ticket_id, True)
