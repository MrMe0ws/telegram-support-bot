from __future__ import annotations

import logging

from aiogram import Bot

from app.db.models import TicketStatus
from app.i18n import tr
from app.services import tickets as ticket_svc

log = logging.getLogger(__name__)


async def close_ticket_by_id(
    bot: Bot,
    session_factory,
    *,
    ticket_id: int,
    notify_user_text: str | None,
    notify_thread_text: str | None,
) -> tuple[bool, str]:
    """
    Закрывает открытый тикет в БД и топик в группе.
    Возвращает (успех, сообщение для пользователя callback).
    """
    forum_chat_id: int | None = None
    thread_id: int | None = None
    user_id: int | None = None

    async with session_factory() as session:
        async with session.begin():
            ticket = await ticket_svc.get_ticket_by_id(session, ticket_id)
            if ticket is None:
                return False, tr("flow", "ticket_not_found")
            if ticket.status != TicketStatus.open.value:
                return False, tr("flow", "already_closed")
            forum_chat_id = ticket.forum_chat_id
            thread_id = ticket.thread_id
            user_id = ticket.user_id
            await ticket_svc.close_ticket(session, ticket.id)

    if notify_thread_text and forum_chat_id is not None and thread_id is not None:
        try:
            await bot.send_message(
                chat_id=forum_chat_id,
                message_thread_id=thread_id,
                text=notify_thread_text,
            )
        except Exception as e:
            log.warning("notify thread on close: %s", e)

    if forum_chat_id is not None and thread_id is not None:
        try:
            await bot.close_forum_topic(
                chat_id=forum_chat_id,
                message_thread_id=thread_id,
            )
        except Exception as e:
            log.warning("close_forum_topic: %s", e)

    if notify_user_text and user_id is not None:
        try:
            await bot.send_message(chat_id=user_id, text=notify_user_text)
        except Exception as e:
            log.warning("notify user on close: %s", e)

    return True, tr("flow", "closed_ok")
