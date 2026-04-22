from __future__ import annotations

import logging

from aiogram import Bot, F, Router
from aiogram.enums import ChatType
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import BaseFilter, Command, CommandStart, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import Message

from app.config import Settings
from app.db.models import MessageSource
from app.i18n import tr
from app.keyboards.help_menu_kb import kb_start_inline
from app.services.help_menu_delivery import send_help_menu_to_chat
from app.services import reaction_bridge as reaction_bridge_svc
from app.services import tickets as ticket_svc
from app.services.ticket_flow import close_ticket_by_id
from app.states import AdminFlow, UserFlow

log = logging.getLogger(__name__)

router = Router(name="private")


async def send_help_section(message: Message, session_factory) -> None:
    await send_help_menu_to_chat(message.bot, message.chat.id, session_factory)


class MainMenuButton(BaseFilter):
    async def __call__(self, message: Message) -> bool:
        if message.chat.type != ChatType.PRIVATE or not message.text:
            return False
        if message.text.startswith("/"):
            return False
        t = message.text.strip()
        return t in (
            tr("user", "btn_create_ticket"),
            tr("user", "btn_help"),
        )


def _is_missing_thread_error(exc: BaseException) -> bool:
    return "thread not found" in str(exc).lower()


async def _create_forum_topic_open(
    bot: Bot,
    settings: Settings,
    forum_chat_id: int,
    topic_name: str,
):
    """Новая тема с иконкой «открытый тикет» (если задано в настройках)."""
    kwargs: dict = {"chat_id": forum_chat_id, "name": topic_name[:127]}
    if settings.topic_icon_emoji_open:
        kwargs["icon_custom_emoji_id"] = settings.topic_icon_emoji_open
    return await bot.create_forum_topic(**kwargs)


async def _recreate_support_topic(
    bot: Bot,
    session_factory,
    settings,
    message: Message,
    *,
    uid: int,
    ticket_id: int,
) -> int:
    """Новый топик в группе поддержки и обновление тикета (старый thread_id больше не используется)."""
    forum_chat_id = settings.support_group_id
    username = message.from_user.username or message.from_user.full_name or str(uid)
    topic_name = settings.topic_name_template.format(
        username=username,
        ticket_id=ticket_id,
        uid=uid,
        name=message.from_user.full_name or "",
    )[:127]
    forum = await _create_forum_topic_open(bot, settings, forum_chat_id, topic_name)
    new_tid = forum.message_thread_id
    async with session_factory() as session:
        async with session.begin():
            await ticket_svc.update_ticket_thread(
                session,
                ticket_id,
                thread_id=new_tid,
                clear_last_forward=True,
            )
    log.info(
        "Тикет %s: пересоздан топик в группе, thread_id=%s",
        ticket_id,
        new_tid,
    )
    return new_tid


async def _rename_topic_if_needed(
    bot: Bot,
    *,
    chat_id: int,
    message_thread_id: int,
    new_name: str,
) -> None:
    """Telegram возвращает TOPIC_NOT_MODIFIED, если имя совпадает с уже установленным (в т.ч. после нормализации)."""
    try:
        await bot.edit_forum_topic(
            chat_id=chat_id,
            message_thread_id=message_thread_id,
            name=new_name[:127],
        )
    except TelegramBadRequest as e:
        err = str(e).upper()
        if "TOPIC_NOT_MODIFIED" in err:
            return
        log.warning("edit_forum_topic: %s", e)


def _content_summary(message: Message) -> tuple[str, str | None, str | None]:
    """content_type, text, file_id for storage."""
    if message.text:
        return "text", message.text, None
    if message.caption:
        base = message.content_type if message.content_type != "text" else "unknown"
        return base, message.caption, None
    if message.photo:
        fid = message.photo[-1].file_id
        return "photo", None, fid
    if message.document:
        return "document", message.document.file_name or "", message.document.file_id
    if message.video:
        return "video", message.video.file_name or "", message.video.file_id
    if message.audio:
        return "audio", message.audio.title or "", message.audio.file_id
    if message.voice:
        return "voice", None, message.voice.file_id
    if message.video_note:
        return "video_note", None, message.video_note.file_id
    if message.sticker:
        return "sticker", message.sticker.emoji or "", message.sticker.file_id
    if message.contact:
        c = message.contact
        return "contact", f"{c.first_name or ''} {c.phone_number or ''}".strip(), None
    return message.content_type or "unknown", None, None


@router.message(CommandStart())
async def cmd_start(message: Message, session_factory, settings):
    async with session_factory() as session:
        async with session.begin():
            blocked = await ticket_svc.is_blocked(session, message.from_user.id)
        if blocked:
            await message.answer(tr("user", "blocked"))
            return
    is_admin = message.from_user.id in settings.admin_ids
    await message.answer(
        tr("user", "start"),
        reply_markup=kb_start_inline(is_admin=is_admin),
    )


@router.message(Command("help"))
async def cmd_help(message: Message, session_factory):
    await send_help_section(message, session_factory)


@router.message(Command("close"), F.chat.type == ChatType.PRIVATE)
async def cmd_close_private(message: Message, bot: Bot, session_factory, settings):
    uid = message.from_user.id
    async with session_factory() as session:
        ticket = await ticket_svc.get_open_ticket(session, uid)
    if ticket is None:
        await message.answer(tr("user", "no_open_ticket"))
        return
    await close_ticket_by_id(
        bot,
        session_factory,
        settings,
        ticket_id=ticket.id,
        notify_user_text=None,
        notify_thread_text=tr("flow", "notify_thread_user_button"),
    )
    await message.answer(tr("flow", "closed_ok"))


@router.message(UserFlow.waiting_ticket_description, F.chat.type == "private")
async def receive_ticket_after_prompt(
    message: Message,
    state: FSMContext,
    bot: Bot,
    session_factory,
    settings,
):
    if message.text:
        t = message.text.strip()
        if t == tr("user", "btn_help"):
            await state.clear()
            await send_help_section(message, session_factory)
            return
        if t == tr("user", "btn_create_ticket"):
            await message.answer(tr("user", "create_ticket_prompt"))
            return

    await state.clear()
    await process_support_message(message, bot, session_factory, settings)


@router.message(MainMenuButton())
async def on_main_menu(
    message: Message,
    state: FSMContext,
    session_factory,
):
    if not message.text:
        return
    text = message.text.strip()
    if text == tr("user", "btn_create_ticket"):
        await state.set_state(UserFlow.waiting_ticket_description)
        await message.answer(tr("user", "create_ticket_prompt"))
        return
    if text == tr("user", "btn_help"):
        await state.clear()
        await send_help_section(message, session_factory)


@router.message(
    F.chat.type == "private",
    ~StateFilter(UserFlow.waiting_ticket_description),
    ~StateFilter(AdminFlow),
)
async def user_to_support(message: Message, bot: Bot, session_factory, settings):
    await process_support_message(message, bot, session_factory, settings)


async def process_support_message(
    message: Message, bot: Bot, session_factory, settings
):
    uid = message.from_user.id
    rename_after_commit: tuple[int, int, str] | None = None

    async with session_factory() as session:
        async with session.begin():
            if await ticket_svc.is_blocked(session, uid):
                await message.answer(tr("user", "blocked"))
                return

            ticket = await ticket_svc.get_open_ticket(session, uid)
            thread_id = ticket.thread_id if ticket else None
            forum_chat_id = settings.support_group_id

            if ticket is None:
                username = message.from_user.username or message.from_user.full_name or str(uid)
                tid_placeholder = 0
                topic_name = settings.topic_name_template.format(
                    username=username,
                    ticket_id=tid_placeholder,
                    uid=uid,
                    name=message.from_user.full_name or "",
                )
                forum = await _create_forum_topic_open(
                    bot,
                    settings,
                    forum_chat_id,
                    topic_name[:127],
                )
                thread_id = forum.message_thread_id
                ticket = await ticket_svc.create_ticket(
                    session,
                    user_id=uid,
                    forum_chat_id=forum_chat_id,
                    thread_id=thread_id,
                    source=MessageSource.telegram.value,
                )
                proper_name = settings.topic_name_template.format(
                    username=username,
                    ticket_id=ticket.id,
                    uid=uid,
                    name=message.from_user.full_name or "",
                )
                final_name = proper_name[:127]
                if final_name != topic_name[:127]:
                    rename_after_commit = (
                        forum_chat_id,
                        thread_id,
                        final_name,
                    )

            ct, txt, fid = _content_summary(message)
            await ticket_svc.record_message(
                session,
                ticket_id=ticket.id,
                direction="in",
                content_type=ct,
                text=txt,
                telegram_file_id=fid,
                telegram_message_id=message.message_id,
            )

    if rename_after_commit is not None:
        cid, tid, fname = rename_after_commit
        await _rename_topic_if_needed(bot, chat_id=cid, message_thread_id=tid, new_name=fname)

    ticket_pk = ticket.id
    current_thread_id = thread_id
    group_msg_id: int | None = None

    async def on_forward_ok(fwd_id: int) -> None:
        nonlocal group_msg_id
        group_msg_id = fwd_id
        try:
            async with session_factory() as session:
                async with session.begin():
                    await reaction_bridge_svc.save_group_user_mapping(
                        session,
                        user_id=uid,
                        dm_message_id=message.message_id,
                        forum_chat_id=forum_chat_id,
                        group_message_id=fwd_id,
                    )
        except Exception as e:
            log.warning("Связка пересланного сообщения для реакций: %s", e)

    async def send_fallback(th: int) -> None:
        nonlocal group_msg_id
        fb = await bot.send_message(
            chat_id=forum_chat_id,
            message_thread_id=th,
            text=tr("user", "forward_fallback", uid=uid),
        )
        group_msg_id = fb.message_id

    try:
        sent = await bot.forward_message(
            chat_id=forum_chat_id,
            from_chat_id=message.chat.id,
            message_id=message.message_id,
            message_thread_id=current_thread_id,
        )
        await on_forward_ok(sent.message_id)
    except TelegramBadRequest as e:
        if _is_missing_thread_error(e):
            log.warning(
                "Топик тикета %s недоступен — создаём резервный; старый thread_id сброшен в БД",
                ticket_pk,
            )
            current_thread_id = await _recreate_support_topic(
                bot,
                session_factory,
                settings,
                message,
                uid=uid,
                ticket_id=ticket_pk,
            )
            try:
                sent = await bot.forward_message(
                    chat_id=forum_chat_id,
                    from_chat_id=message.chat.id,
                    message_id=message.message_id,
                    message_thread_id=current_thread_id,
                )
                await on_forward_ok(sent.message_id)
            except TelegramBadRequest as e2:
                log.warning(
                    "forward в новый топик (uid=%s): %s — пробуем текстом",
                    uid,
                    e2,
                )
                try:
                    await send_fallback(current_thread_id)
                except TelegramBadRequest as e3:
                    log.warning(
                        "fallback в новый топик не удалось (uid=%s): %s",
                        uid,
                        e3,
                    )
        else:
            log.warning(
                "forward_message в топик не удалось (uid=%s): %s — пробуем текстом",
                uid,
                e,
            )
            try:
                await send_fallback(current_thread_id)
            except TelegramBadRequest as e2:
                if _is_missing_thread_error(e2):
                    log.warning(
                        "Топик тикета %s недоступен — создаём резервный",
                        ticket_pk,
                    )
                    current_thread_id = await _recreate_support_topic(
                        bot,
                        session_factory,
                        settings,
                        message,
                        uid=uid,
                        ticket_id=ticket_pk,
                    )
                    try:
                        sent = await bot.forward_message(
                            chat_id=forum_chat_id,
                            from_chat_id=message.chat.id,
                            message_id=message.message_id,
                            message_thread_id=current_thread_id,
                        )
                        await on_forward_ok(sent.message_id)
                    except TelegramBadRequest as e3:
                        log.warning(
                            "forward после пересоздания (uid=%s): %s",
                            uid,
                            e3,
                        )
                        try:
                            await send_fallback(current_thread_id)
                        except Exception as e4:
                            log.warning(
                                "Сообщение в группу не доставлено (uid=%s): %s",
                                uid,
                                e4,
                            )
                else:
                    log.warning(
                        "fallback send_message в топик не удалось (uid=%s): %s",
                        uid,
                        e2,
                    )
    except Exception as e:
        log.warning("forward_message в топик не удалось (uid=%s): %s", uid, e)
        try:
            await send_fallback(current_thread_id)
        except TelegramBadRequest as e2:
            if _is_missing_thread_error(e2):
                log.warning(
                    "Топик тикета %s недоступен — создаём резервный",
                    ticket_pk,
                )
                current_thread_id = await _recreate_support_topic(
                    bot,
                    session_factory,
                    settings,
                    message,
                    uid=uid,
                    ticket_id=ticket_pk,
                )
                try:
                    sent = await bot.forward_message(
                        chat_id=forum_chat_id,
                        from_chat_id=message.chat.id,
                        message_id=message.message_id,
                        message_thread_id=current_thread_id,
                    )
                    await on_forward_ok(sent.message_id)
                except Exception as e3:
                    log.warning(
                        "После пересоздания топика (uid=%s): %s",
                        uid,
                        e3,
                    )
                    try:
                        await send_fallback(current_thread_id)
                    except Exception as e4:
                        log.warning(
                            "Сообщение в группу не доставлено (uid=%s): %s",
                            uid,
                            e4,
                        )
            else:
                log.warning(
                    "fallback send_message в топик не удалось (uid=%s): %s",
                    uid,
                    e2,
                )

    if group_msg_id is not None:
        async with session_factory() as session:
            async with session.begin():
                await ticket_svc.update_last_forward_group_msg(
                    session,
                    ticket_pk,
                    group_msg_id,
                )
