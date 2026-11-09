"""Отдача вложений тикетов кабинета магазину: файлы хранит Telegram, бот их скачивает."""

from __future__ import annotations

from aiogram import Bot

from app.db.models import MessageSource
from app.services import cabinet_media
from app.services import tickets as ticket_svc


async def fetch_attachment(
    bot: Bot,
    session_factory,
    *,
    attachment_id: int,
    account_id: int,
) -> tuple[bytes, str] | None:
    """
    Байты и MIME вложения. None — вложения нет или оно чужое: тикет не из кабинета,
    другой аккаунт, у строки нет изображения. Ошибки Telegram пробрасываются.
    """
    async with session_factory() as session:
        found = await ticket_svc.get_message_with_ticket(session, attachment_id)
    if found is None:
        return None
    msg, ticket = found
    if ticket.source != MessageSource.cabinet.value or ticket.user_id != account_id:
        return None
    meta = cabinet_media.meta_from_row(msg.content_type, msg.telegram_file_id, msg.attachment_meta)
    if meta is None:
        return None

    f = await bot.get_file(meta.file_id)
    if not f.file_path:
        return None
    buf = await bot.download_file(f.file_path)
    data = buf.read() if buf is not None else b""
    if not data:
        return None
    return data, meta.mime
