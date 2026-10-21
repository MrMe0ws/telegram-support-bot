from __future__ import annotations

import logging

from aiogram import F, Router
from aiogram.enums import ChatType
from aiogram.types import CallbackQuery

from app.db.models import TicketStatus
from app.i18n import tr
from app.services import tickets as ticket_svc
from app.services.ai.service import CB_CALL_HUMAN, CB_DRAFT_REJECT, CB_DRAFT_SEND, peek_ai_service

log = logging.getLogger(__name__)

router = Router(name="ai")


def _int_suffix(data: str, prefix: str) -> int | None:
    try:
        return int(data[len(prefix):])
    except ValueError:
        return None


@router.callback_query(F.data.startswith(CB_CALL_HUMAN), F.message.chat.type == ChatType.PRIVATE)
async def client_calls_human(query: CallbackQuery, session_factory):
    """Кнопка «Позвать человека» под ответом ИИ."""
    ticket_id = _int_suffix(query.data or "", CB_CALL_HUMAN)
    ai = peek_ai_service()
    if ticket_id is None or ai is None:
        await query.answer()
        return
    async with session_factory() as session:
        ticket = await ticket_svc.get_ticket_by_id(session, ticket_id)
    # Кнопку видит только владелец чата, но callback можно подделать — сверяем владельца.
    if ticket is None or ticket.user_id != query.from_user.id:
        await query.answer()
        return
    if ticket.status != TicketStatus.open.value:
        await query.answer(tr("ai", "client_ticket_closed"), show_alert=True)
        return
    if not await ai.escalate(ticket_id, initiator="client"):
        await query.answer(tr("ai", "client_already_escalated"), show_alert=True)
        return
    await query.answer()


@router.callback_query(F.data.startswith(CB_DRAFT_SEND) | F.data.startswith(CB_DRAFT_REJECT))
async def draft_decision(query: CallbackQuery, settings):
    """Кнопки под черновиком ИИ в теме тикета (AI_MODE=draft)."""
    if query.from_user.id not in settings.admin_ids:
        await query.answer(tr("ai", "draft_admin_only"), show_alert=True)
        return
    data = query.data or ""
    send = data.startswith(CB_DRAFT_SEND)
    draft_id = _int_suffix(data, CB_DRAFT_SEND if send else CB_DRAFT_REJECT)
    ai = peek_ai_service()
    if draft_id is None or ai is None:
        await query.answer()
        return
    user = query.from_user
    admin_label = f"@{user.username}" if user.username else (user.full_name or str(user.id))
    toast = await ai.resolve_draft(draft_id, send=send, admin_label=admin_label)
    await query.answer(toast)
