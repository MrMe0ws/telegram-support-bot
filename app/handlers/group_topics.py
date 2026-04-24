from __future__ import annotations

import logging
from html import escape

from aiogram import Bot, F, Router
from aiogram.enums import ParseMode
from aiogram.filters import Command
from aiogram.types import Message

from app.i18n import tr
from app.services import reaction_bridge as reaction_bridge_svc
from app.services import tickets as ticket_svc
from app.services.ticket_flow import close_ticket_by_id

log = logging.getLogger(__name__)

router = Router(name="group_topics")


@router.message(
    F.chat.type.in_({"supergroup", "group"}),
    F.message_thread_id.as_("tid"),
    Command("close"),
)
async def close_in_topic(
    message: Message,
    bot: Bot,
    tid: int,
    session_factory,
    settings,
):
    if message.chat.id != settings.support_group_id:
        return
    if message.from_user.id not in settings.admin_ids:
        await message.reply(tr("group", "close_admin_only"))
        return

    async with session_factory() as session:
        ticket = await ticket_svc.get_ticket_by_thread(session, message.chat.id, tid)

    if ticket is None:
        await message.reply(tr("group", "no_open_ticket_in_thread"))
        return

    _, msg = await close_ticket_by_id(
        bot,
        session_factory,
        settings,
        ticket_id=ticket.id,
        notify_user_text=tr("flow", "notify_user_default"),
        notify_thread_text=tr("flow", "notify_thread_close_command"),
    )
    await message.reply(msg)


@router.message(
    F.chat.type.in_({"supergroup", "group"}),
    F.message_thread_id.as_("tid"),
    Command("profile"),
)
async def profile_in_topic(
    message: Message,
    bot: Bot,
    tid: int,
    session_factory,
    settings,
):
    if message.chat.id != settings.support_group_id:
        return
    if message.from_user.id not in settings.admin_ids:
        await message.reply(tr("group", "profile_admin_only"))
        return

    async with session_factory() as session:
        ticket = await ticket_svc.get_ticket_by_thread(session, message.chat.id, tid)

    if ticket is None:
        await message.reply(tr("group", "no_open_ticket_in_thread"))
        return

    try:
        chat = await bot.get_chat(ticket.user_id)
    except Exception as e:
        await message.reply(tr("callbacks", "profile_load_failed", error=str(e)))
        return

    username = getattr(chat, "username", None)
    if username:
        line_u = f"@{escape(username)}"
    else:
        line_u = escape(tr("callbacks", "profile_no_username"))
    name_part = (
        escape(chat.full_name)
        if chat.full_name
        else escape(tr("callbacks", "profile_name_empty"))
    )
    body = tr(
        "callbacks",
        "profile_body_html",
        line_user=line_u,
        user_id=ticket.user_id,
        name=name_part,
    )
    await message.reply(body, parse_mode=ParseMode.HTML)


@router.message(
    F.chat.type.in_({"supergroup", "group"}),
    F.message_thread_id.as_("tid"),
)
async def staff_reply(
    message: Message,
    bot: Bot,
    tid: int,
    session_factory,
    settings,
):
    # Только наш бот — это пересланные из ЛС сообщения клиента, не ретранслируем в ЛС.
    # Обычные «боты» (напр. анонимный админ супергруппы) не отсекаем через is_bot.
    if not message.from_user or message.from_user.id == bot.id:
        return
    if message.chat.id != settings.support_group_id:
        return

    async with session_factory() as session:
        async with session.begin():
            ticket = await ticket_svc.get_ticket_by_thread(session, message.chat.id, tid)
            if ticket is None:
                log.debug(
                    "Нет открытого тикета для chat=%s thread=%s",
                    message.chat.id,
                    tid,
                )
                return

            client_uid = ticket.user_id

            ct = message.content_type or "unknown"
            text = message.text or message.caption
            fid = None
            if message.photo:
                fid = message.photo[-1].file_id
            elif message.document:
                fid = message.document.file_id
            elif message.video:
                fid = message.video.file_id
            elif message.audio:
                fid = message.audio.file_id
            elif message.voice:
                fid = message.voice.file_id
            elif message.video_note:
                fid = message.video_note.file_id
            elif message.sticker:
                fid = message.sticker.file_id

            await ticket_svc.record_message(
                session,
                ticket_id=ticket.id,
                direction="out",
                content_type=ct,
                text=text,
                telegram_file_id=fid,
                telegram_message_id=message.message_id,
            )

    try:
        copied = await bot.copy_message(
            chat_id=client_uid,
            from_chat_id=message.chat.id,
            message_id=message.message_id,
        )
    except Exception as e:
        log.warning(
            "Не удалось copy_message клиенту user_id=%s: %s",
            client_uid,
            e,
        )
        try:
            await bot.send_message(
                chat_id=client_uid,
                text=text or tr("group", "support_reply_fallback"),
            )
        except Exception as e2:
            log.error(
                "Не удалось отправить текст клиенту user_id=%s: %s",
                client_uid,
                e2,
            )
    else:
        try:
            async with session_factory() as session:
                async with session.begin():
                    await reaction_bridge_svc.save_dm_staff_mapping(
                        session,
                        user_id=client_uid,
                        dm_message_id=copied.message_id,
                        forum_chat_id=message.chat.id,
                        staff_message_id=message.message_id,
                    )
        except Exception as e_map:
            log.warning(
                "Не сохранена связка для реакций user_id=%s: %s",
                client_uid,
                e_map,
            )
