from __future__ import annotations

import asyncio
import logging
import re
from collections import defaultdict
from collections.abc import Sequence
from html import unescape

from aiogram import Bot
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import BufferedInputFile, InputMediaPhoto

from app.config import Settings
from app.db.models import MessageSource
from app.handlers.private_chat import _create_forum_topic_open, _rename_topic_if_needed
from app.services import cabinet_media
from app.services import shop_webhook as shop_webhook_svc
from app.services import tickets as ticket_svc
from app.services import topic_status
from app.services.ai.service import get_ai_service
from app.services.cabinet_media import AttachmentMeta, CabinetUpload

log = logging.getLogger(__name__)

_cabinet_locks: defaultdict[int, asyncio.Lock] = defaultdict(asyncio.Lock)

# Лимит подписи к фото и файлу в Telegram (в символах UTF-16 после разбора HTML).
_CAPTION_LIMIT = 1024
_TAG_RE = re.compile(r"<[^>]+>")


def _caption_fits(html_text: str) -> bool:
    visible = unescape(_TAG_RE.sub("", html_text))
    return len(visible.encode("utf-16-le")) // 2 <= _CAPTION_LIMIT


def _group_note(shop_ticket_id: int, client_message_id: str | None) -> str:
    """raw_note строк с вложениями: по cmid повтор запроса находит всю группу."""
    note = f"shop_ticket_id={shop_ticket_id}"
    return f"{note};cmid={client_message_id}" if client_message_id else note


def _attachments_of(rows) -> list[dict]:
    out = []
    for row in rows:
        meta = cabinet_media.meta_from_row(row.content_type, row.telegram_file_id, row.attachment_meta)
        if meta is not None:
            out.append(meta.to_api(row.id))
    return out


async def _send_one(bot: Bot, upload: CabinetUpload, caption: str | None, target: dict):
    """Одно вложение: фото, а если Telegram его не принял (размеры, формат) — файлом."""
    parse_mode = ParseMode.HTML if caption else None
    if upload.as_photo:
        try:
            return await bot.send_photo(
                photo=BufferedInputFile(upload.data, upload.filename),
                caption=caption,
                parse_mode=parse_mode,
                **target,
            )
        except TelegramBadRequest as e:
            log.info("Фото %s не принято (%s) — отправляем файлом", upload.filename, e)
    # Без определения типа: иначе Telegram сделает из GIF видео, а из картинки — фото.
    return await bot.send_document(
        document=BufferedInputFile(upload.data, upload.filename),
        caption=caption,
        parse_mode=parse_mode,
        disable_content_type_detection=True,
        **target,
    )


async def send_to_topic(
    bot: Bot,
    *,
    chat_id: int,
    thread_id: int,
    body: str,
    files: Sequence[CabinetUpload],
) -> tuple[list[tuple[int, AttachmentMeta]], int]:
    """
    Сообщение клиента в тему: текст и вложения.

    Текст — подписью к первому вложению, если влезает, иначе отдельным сообщением
    перед ними. Несколько фото — одним альбомом; не вышло (или среди файлов есть
    не-фото) — по одному.

    Возвращает (telegram message_id и вложение по каждому файлу, id последнего сообщения).
    """
    target = {"chat_id": chat_id, "message_thread_id": thread_id}
    if not files:
        sent = await bot.send_message(text=body, parse_mode=ParseMode.HTML, **target)
        return [], sent.message_id

    caption: str | None = body
    if not _caption_fits(body):
        await bot.send_message(text=body, parse_mode=ParseMode.HTML, **target)
        caption = None

    if len(files) > 1 and all(f.as_photo for f in files):
        media = [
            InputMediaPhoto(
                media=BufferedInputFile(f.data, f.filename),
                caption=caption if i == 0 else None,
                parse_mode=ParseMode.HTML if i == 0 and caption else None,
            )
            for i, f in enumerate(files)
        ]
        try:
            msgs = await bot.send_media_group(media=media, **target)
        except TelegramBadRequest as e:
            log.info("Альбом из кабинета не принят (%s) — отправляем по одному", e)
        else:
            sent_files = [(m.message_id, cabinet_media.meta_from_sent(m, f)) for m, f in zip(msgs, files)]
            return sent_files, msgs[-1].message_id

    sent_files = []
    for i, upload in enumerate(files):
        msg = await _send_one(bot, upload, caption if i == 0 else None, target)
        sent_files.append((msg.message_id, cabinet_media.meta_from_sent(msg, upload)))
    return sent_files, sent_files[-1][0]


async def post_cabinet_message(
    bot: Bot,
    session_factory,
    settings: Settings,
    *,
    account_id: int,
    shop_ticket_id: int,
    is_new_ticket: bool,
    client_message_id: str | None,
    display_name: str,
    telegram_label: str,
    email: str,
    subscription_summary: str,
    text: str,
    telegram_id: int | None = None,
    files: Sequence[CabinetUpload] = (),
) -> dict:
    uid = account_id
    rename_after_commit: tuple[int, int, str] | None = None
    ticket_id: int
    thread_id: int
    forum_chat_id = settings.support_group_id

    created_new = False
    body: str
    file_row_ids: list[int] = []

    async with _cabinet_locks[uid]:
        async with session_factory() as session:
            async with session.begin():
                # Idempotency check: если сообщение с таким client_message_id уже есть — не дублируем
                if client_message_id:
                    existing = await ticket_svc.get_message_by_client_id(session, client_message_id)
                    if existing:
                        ticket = await ticket_svc.get_ticket_by_id(session, existing.ticket_id)
                        if ticket:
                            log.info(
                                "Duplicate cabinet message client_id=%s, returning existing ticket=%s",
                                client_message_id,
                                ticket.id,
                            )
                            rows = [existing]
                            if existing.raw_note and ";cmid=" in existing.raw_note:
                                rows = await ticket_svc.list_messages_by_note(
                                    session, existing.ticket_id, existing.raw_note
                                )
                            return {
                                "support_bot_ticket_id": ticket.id,
                                "thread_id": ticket.thread_id or 0,
                                "attachments": _attachments_of(rows),
                            }

                ticket = await ticket_svc.get_open_ticket(
                    session,
                    uid,
                    source=MessageSource.cabinet.value,
                )
                if ticket is None:
                    if not is_new_ticket:
                        raise RuntimeError("open cabinet ticket not found")
                    username = display_name or str(uid)
                    topic_name = settings.topic_name_template.format(
                        username=username,
                        ticket_id=0,
                        uid=uid,
                        name=username,
                    )
                    forum = await _create_forum_topic_open(
                        bot,
                        settings,
                        forum_chat_id,
                        topic_name[:127],
                    )
                    thread_id = forum.message_thread_id
                    external_uid = f"shop:{shop_ticket_id}"
                    ticket = await ticket_svc.create_ticket(
                        session,
                        user_id=uid,
                        forum_chat_id=forum_chat_id,
                        thread_id=thread_id,
                        source=MessageSource.cabinet.value,
                        external_uid=external_uid,
                    )
                    proper_name = settings.topic_name_template.format(
                        username=username,
                        ticket_id=ticket.id,
                        uid=uid,
                        name=username,
                    )
                    final_name = proper_name[:127]
                    if final_name != topic_name[:127]:
                        rename_after_commit = (forum_chat_id, thread_id, final_name)
                    created_new = True
                else:
                    thread_id = ticket.thread_id or 0
                    if thread_id <= 0:
                        raise RuntimeError("cabinet ticket has no thread")
                    expected_uid = f"shop:{shop_ticket_id}"
                    if ticket.external_uid != expected_uid:
                        await ticket_svc.update_ticket_external_uid(
                            session,
                            ticket.id,
                            expected_uid,
                        )

                ticket_id = ticket.id

                if telegram_id and telegram_id > 0:
                    await ticket_svc.update_ticket_linked_telegram_id(
                        session,
                        ticket_id,
                        telegram_id,
                    )

                if created_new or is_new_ticket:
                    body = shop_webhook_svc.format_cabinet_first_message(
                        telegram_label=telegram_label,
                        email=email,
                        subscription_summary=subscription_summary,
                        question=text,
                    )
                else:
                    body = shop_webhook_svc.format_cabinet_followup(
                        display_name=display_name,
                        text=text,
                    )

                if not files:
                    await ticket_svc.record_message(
                        session,
                        ticket_id=ticket_id,
                        direction="in",
                        content_type="text",
                        text=text,
                        telegram_file_id=None,
                        telegram_message_id=None,
                        raw_note=f"shop_ticket_id={shop_ticket_id}",
                        client_message_id=client_message_id,
                    )
                else:
                    # По строке на файл; текст — у первой, как подпись к фото в Telegram.
                    # file_id появится после отправки в тему.
                    note = _group_note(shop_ticket_id, client_message_id)
                    for i, upload in enumerate(files):
                        row = await ticket_svc.record_message(
                            session,
                            ticket_id=ticket_id,
                            direction="in",
                            content_type="photo" if upload.as_photo else "document",
                            text=(text or None) if i == 0 else None,
                            telegram_file_id=None,
                            telegram_message_id=None,
                            raw_note=note,
                            client_message_id=client_message_id if i == 0 else None,
                        )
                        file_row_ids.append(row.id)

    if rename_after_commit is not None:
        cid, tid, fname = rename_after_commit
        await _rename_topic_if_needed(
            bot,
            chat_id=cid,
            message_thread_id=tid,
            new_name=fname,
        )

    try:
        sent_files, last_message_id = await send_to_topic(
            bot,
            chat_id=forum_chat_id,
            thread_id=thread_id,
            body=body,
            files=files,
        )
    except Exception:
        # Вложения без file_id никому не нужны: ни ИИ, ни кабинету — убираем,
        # чтобы повтор из кабинета не оставил в тикете пустых строк.
        if file_row_ids:
            async with session_factory() as session:
                async with session.begin():
                    await ticket_svc.delete_messages(session, file_row_ids)
        raise

    attachments: list[dict] = []
    async with session_factory() as session:
        async with session.begin():
            await ticket_svc.update_last_forward_group_msg(
                session,
                ticket_id,
                last_message_id,
            )
            for row_id, (tg_message_id, meta) in zip(file_row_ids, sent_files):
                await ticket_svc.update_message_media(
                    session,
                    row_id,
                    content_type=meta.content_type,
                    telegram_file_id=meta.file_id,
                    telegram_message_id=tg_message_id,
                    attachment_meta=meta.to_json(),
                )
                attachments.append(meta.to_api(row_id))

    await topic_status.after_client_message(bot, session_factory, settings, ticket_id)
    ai = get_ai_service()
    if ai is not None:
        ai.schedule(ticket_id)

    return {
        "support_bot_ticket_id": ticket_id,
        "thread_id": thread_id,
        "attachments": attachments,
    }
