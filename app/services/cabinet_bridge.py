from __future__ import annotations

import asyncio
import logging
from collections import defaultdict

from aiogram import Bot
from aiogram.enums import ParseMode

from app.config import Settings
from app.db.models import MessageSource
from app.handlers.private_chat import _create_forum_topic_open, _rename_topic_if_needed
from app.services import shop_webhook as shop_webhook_svc
from app.services import tickets as ticket_svc

log = logging.getLogger(__name__)

_cabinet_locks: defaultdict[int, asyncio.Lock] = defaultdict(asyncio.Lock)


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
) -> dict:
    uid = account_id
    rename_after_commit: tuple[int, int, str] | None = None
    ticket_id: int
    thread_id: int
    forum_chat_id = settings.support_group_id

    created_new = False
    body: str

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
                            return {
                                "support_bot_ticket_id": ticket.id,
                                "thread_id": ticket.thread_id or 0,
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

    if rename_after_commit is not None:
        cid, tid, fname = rename_after_commit
        await _rename_topic_if_needed(
            bot,
            chat_id=cid,
            message_thread_id=tid,
            new_name=fname,
        )

    sent = await bot.send_message(
        chat_id=forum_chat_id,
        message_thread_id=thread_id,
        text=body,
        parse_mode=ParseMode.HTML,
    )

    async with session_factory() as session:
        async with session.begin():
            await ticket_svc.update_last_forward_group_msg(
                session,
                ticket_id,
                sent.message_id,
            )

    return {
        "support_bot_ticket_id": ticket_id,
        "thread_id": thread_id,
    }
