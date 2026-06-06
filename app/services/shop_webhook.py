from __future__ import annotations

import logging
from html import escape

import aiohttp

from app.config import Settings
from app.db.models import Ticket

log = logging.getLogger(__name__)


def _webhook_configured(settings: Settings) -> bool:
    return bool(settings.shop_webhook_url and settings.support_bridge_secret)


async def deliver_staff_reply(
    settings: Settings,
    ticket: Ticket,
    *,
    text: str,
    author_label: str,
    support_bot_message_id: int,
) -> None:
    if not _webhook_configured(settings):
        log.warning("cabinet webhook not configured; staff reply not delivered")
        return
    shop_ticket_id = _shop_ticket_id(ticket)
    payload = {
        "event": "message",
        "account_id": ticket.user_id,
        "shop_ticket_id": shop_ticket_id,
        "support_bot_ticket_id": ticket.id,
        "support_bot_message_id": support_bot_message_id,
        "text": text,
        "author_label": author_label,
    }
    await _post_webhook(settings, payload)


async def notify_ticket_closed(settings: Settings, ticket: Ticket) -> None:
    if not _webhook_configured(settings):
        return
    payload = {
        "event": "closed",
        "account_id": ticket.user_id,
        "shop_ticket_id": _shop_ticket_id(ticket),
        "support_bot_ticket_id": ticket.id,
    }
    await _post_webhook(settings, payload)


def shop_ticket_id(ticket: Ticket) -> int:
    if ticket.external_uid and ticket.external_uid.startswith("shop:"):
        try:
            return int(ticket.external_uid.split(":", 1)[1])
        except ValueError:
            return 0
    return 0


def _shop_ticket_id(ticket: Ticket) -> int:
    return shop_ticket_id(ticket)


async def _post_webhook(settings: Settings, payload: dict) -> None:
    headers = {
        "Authorization": f"Bearer {settings.support_bridge_secret}",
        "Content-Type": "application/json",
    }
    timeout = aiohttp.ClientTimeout(total=15)
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(
                settings.shop_webhook_url,
                json=payload,
                headers=headers,
            ) as resp:
                body = await resp.text()
                if resp.status < 200 or resp.status >= 300:
                    log.warning(
                        "shop webhook failed status=%s body=%s payload_event=%s",
                        resp.status,
                        body[:500],
                        payload.get("event"),
                    )
    except Exception as e:
        log.warning("shop webhook request failed: %s", e)


def format_staff_author(message) -> str:
    if not message.from_user:
        return "Поддержка"
    username = getattr(message.from_user, "username", None)
    if username:
        return f"@{username}"
    name = (message.from_user.full_name or "").strip()
    return name or "Поддержка"


def format_cabinet_first_message(
    *,
    telegram_label: str,
    email: str,
    subscription_summary: str,
    question: str,
) -> str:
    return (
        "📩 Обращение из кабинета\n"
        f"👤 TG: {escape(telegram_label or 'не привязан')}\n"
        f"📧 Email: {escape(email or '—')}\n"
        f"📦 Подписка: {escape(subscription_summary or 'нет данных')}\n"
        "💬 Вопрос:\n"
        f"{escape(question)}"
    )


def format_cabinet_followup(*, display_name: str, text: str) -> str:
    return f"💬 {escape(display_name or 'Пользователь')}: {escape(text)}"
