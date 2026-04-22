from __future__ import annotations

import logging
from html import escape as html_escape

from aiogram import Bot, F, Router
from aiogram.enums import ChatType, ContentType
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import BaseFilter, Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from app.i18n import tr
from app.keyboards.admin_kb import (
    kb_admin_home,
    kb_block_skip_reason,
    kb_cancel_fsm,
    kb_help_add_pick_type,
    kb_help_delete_confirm,
    kb_help_edit_menu,
    kb_help_links_list,
)
from app.services import help_links as help_svc
from app.services import tickets as ticket_svc
from app.states import AdminFlow

log = logging.getLogger(__name__)

router = Router(name="admin_panel")


class AdminPanelReplyButton(BaseFilter):
    """Текст нижней кнопки «Админ-панель» (сравнение через tr на каждый апдейт)."""

    async def __call__(self, message: Message) -> bool:
        if message.chat.type != ChatType.PRIVATE or not message.text:
            return False
        if message.text.startswith("/"):
            return False
        return message.text.strip() == tr("admin", "btn_admin_panel")


def _admin_ok(settings, uid: int | None) -> bool:
    return uid is not None and uid in settings.admin_ids


async def _answer_admin_denied(message: Message) -> None:
    await message.answer(tr("admin", "no_permission"))


async def _send_admin_home(bot: Bot, chat_id: int) -> None:
    await bot.send_message(
        chat_id=chat_id,
        text=tr("admin", "panel_intro"),
        reply_markup=kb_admin_home(),
    )


def _helplink_row_icon(row) -> str:
    u = (row.url or "").strip()
    if help_svc.url_like(u):
        return "🔗"
    if (row.body_text or "").strip():
        return "📄"
    return "❔"


def _help_edit_preview(link) -> str:
    title_esc = html_escape((link.title or "")[:200])
    lines = [tr("admin", "help_edit_header", id=link.id, title=title_esc)]
    u = (link.url or "").strip()
    b = (link.body_text or "").strip()
    if help_svc.url_like(u):
        lines.append(
            tr("admin", "help_edit_current_url", url=html_escape(u[:800])),
        )
    elif b:
        snippet = b[:400] + ("…" if len(b) > 400 else "")
        lines.append(
            tr("admin", "help_edit_current_body", snippet=html_escape(snippet)),
        )
    else:
        lines.append(tr("admin", "help_edit_empty_payload"))
    lines.append("")
    lines.append(tr("admin", "help_edit_actions_hint"))
    return "\n".join(lines)


def _help_links_body(rows: list) -> str:
    if not rows:
        return tr("admin", "helplink_usage_list")
    lines = [tr("admin", "helplink_list_header")]
    for idx, row in enumerate(rows, start=1):
        lines.append(
            tr(
                "admin",
                "helplink_list_line",
                idx=idx,
                icon=_helplink_row_icon(row),
                link_id=row.id,
                pos=row.position,
                title=row.title[:120],
            )
        )
    lines.append("")
    lines.append(tr("admin", "help_links_hint"))
    return "\n".join(lines)


@router.message(Command("admin"), F.chat.type == ChatType.PRIVATE)
async def cmd_admin(message: Message, settings):
    if not _admin_ok(settings, message.from_user.id if message.from_user else None):
        await _answer_admin_denied(message)
        return
    await message.answer(tr("admin", "panel_intro"), reply_markup=kb_admin_home())


@router.message(AdminPanelReplyButton())
async def open_admin_from_button(message: Message, settings):
    if not _admin_ok(settings, message.from_user.id if message.from_user else None):
        await message.answer(tr("admin", "no_permission"))
        return
    await message.answer(tr("admin", "panel_intro"), reply_markup=kb_admin_home())


@router.callback_query(F.data == "aop")
async def admin_open_from_start_inline(query: CallbackQuery, settings, bot: Bot):
    """Инлайн «Админ-панель» под приветствием /start."""
    uid = query.from_user.id if query.from_user else None
    if not _admin_ok(settings, uid):
        await query.answer(tr("admin", "no_permission"), show_alert=True)
        return
    await query.answer()
    await _send_admin_home(bot, uid)


@router.callback_query(F.data == "ap:ho")
async def back_to_home(query: CallbackQuery, settings):
    if not query.from_user or not _admin_ok(settings, query.from_user.id):
        await query.answer(tr("admin", "no_permission"), show_alert=True)
        return
    if not query.message:
        await query.answer()
        return
    try:
        await query.message.edit_text(
            text=tr("admin", "panel_intro"),
            reply_markup=kb_admin_home(),
        )
    except TelegramBadRequest:
        await query.message.answer(tr("admin", "panel_intro"), reply_markup=kb_admin_home())
    await query.answer()


@router.callback_query(F.data == "ap:x")
async def close_panel_message(query: CallbackQuery, settings):
    if not query.from_user or not _admin_ok(settings, query.from_user.id):
        await query.answer(tr("admin", "no_permission"), show_alert=True)
        return
    if query.message:
        try:
            await query.message.edit_text(
                tr("admin", "panel_closed"),
                reply_markup=None,
            )
        except TelegramBadRequest:
            pass
    await query.answer()


@router.callback_query(F.data == "ap:ca")
async def cancel_fsm(query: CallbackQuery, state: FSMContext, settings, bot: Bot):
    if not query.from_user or not _admin_ok(settings, query.from_user.id):
        await query.answer(tr("admin", "no_permission"), show_alert=True)
        return
    await state.clear()
    await query.answer(tr("admin", "cancel_done"))
    await _send_admin_home(bot, query.from_user.id)


@router.callback_query(F.data == "ap:b")
async def block_start(query: CallbackQuery, state: FSMContext, settings, bot: Bot):
    if not query.from_user or not _admin_ok(settings, query.from_user.id):
        await query.answer(tr("admin", "no_permission"), show_alert=True)
        return
    await query.answer()
    await state.set_state(AdminFlow.block_uid)
    await bot.send_message(
        chat_id=query.from_user.id,
        text=tr("admin", "block_ask_uid"),
        reply_markup=kb_cancel_fsm(),
    )


@router.callback_query(F.data == "ap:u")
async def unblock_start(query: CallbackQuery, state: FSMContext, settings, bot: Bot):
    if not query.from_user or not _admin_ok(settings, query.from_user.id):
        await query.answer(tr("admin", "no_permission"), show_alert=True)
        return
    await query.answer()
    await state.set_state(AdminFlow.unblock_uid)
    await bot.send_message(
        chat_id=query.from_user.id,
        text=tr("admin", "unblock_ask_uid"),
        reply_markup=kb_cancel_fsm(),
    )


@router.callback_query(F.data == "ap:h")
async def help_links_open(query: CallbackQuery, settings, session_factory):
    if not query.from_user or not _admin_ok(settings, query.from_user.id):
        await query.answer(tr("admin", "no_permission"), show_alert=True)
        return
    if not query.message:
        await query.answer()
        return

    async with session_factory() as session:
        async with session.begin():
            rows = await help_svc.list_ordered(session)

    text = _help_links_body(rows)
    markup = kb_help_links_list(rows)

    try:
        await query.message.edit_text(text=text, reply_markup=markup)
    except TelegramBadRequest:
        await query.message.answer(text=text, reply_markup=markup)
    await query.answer()


@router.callback_query(F.data.startswith("ap:hu:"))
async def help_link_up(query: CallbackQuery, settings, session_factory):
    if not query.from_user or not _admin_ok(settings, query.from_user.id):
        await query.answer(tr("admin", "no_permission"), show_alert=True)
        return
    try:
        lid = int((query.data or "").split(":")[2])
    except (IndexError, ValueError):
        await query.answer()
        return

    ok = False
    async with session_factory() as session:
        async with session.begin():
            ok = await help_svc.move_up(session, lid)

    await query.answer(
        tr("admin", "helplink_move_ok") if ok else tr("admin", "helplink_move_edge")
    )
    if not query.message:
        return
    async with session_factory() as session:
        async with session.begin():
            rows = await help_svc.list_ordered(session)
    text = _help_links_body(rows)
    markup = kb_help_links_list(rows)
    try:
        await query.message.edit_text(text=text, reply_markup=markup)
    except TelegramBadRequest:
        pass


@router.callback_query(F.data.startswith("ap:hd:"))
async def help_link_down(query: CallbackQuery, settings, session_factory):
    if not query.from_user or not _admin_ok(settings, query.from_user.id):
        await query.answer(tr("admin", "no_permission"), show_alert=True)
        return
    try:
        lid = int((query.data or "").split(":")[2])
    except (IndexError, ValueError):
        await query.answer()
        return

    ok = False
    async with session_factory() as session:
        async with session.begin():
            ok = await help_svc.move_down(session, lid)

    await query.answer(
        tr("admin", "helplink_move_ok") if ok else tr("admin", "helplink_move_edge")
    )
    if not query.message:
        return
    async with session_factory() as session:
        async with session.begin():
            rows = await help_svc.list_ordered(session)
    text = _help_links_body(rows)
    markup = kb_help_links_list(rows)
    try:
        await query.message.edit_text(text=text, reply_markup=markup)
    except TelegramBadRequest:
        pass


@router.callback_query(F.data.startswith("ap:hk:"))
async def help_link_delete_ask(query: CallbackQuery, settings):
    if not query.from_user or not _admin_ok(settings, query.from_user.id):
        await query.answer(tr("admin", "no_permission"), show_alert=True)
        return
    try:
        lid = int((query.data or "").split(":")[2])
    except (IndexError, ValueError):
        await query.answer()
        return
    if not query.message:
        await query.answer()
        return
    try:
        await query.message.edit_text(
            text=tr("admin", "help_delete_confirm", id=lid),
            reply_markup=kb_help_delete_confirm(lid),
        )
    except TelegramBadRequest as e:
        log.debug("delete ask edit: %s", e)
    await query.answer()


@router.callback_query(F.data == "ap:hn")
async def help_link_delete_cancel(query: CallbackQuery, settings, session_factory):
    if not query.from_user or not _admin_ok(settings, query.from_user.id):
        await query.answer(tr("admin", "no_permission"), show_alert=True)
        return
    if not query.message:
        await query.answer()
        return

    async with session_factory() as session:
        async with session.begin():
            rows = await help_svc.list_ordered(session)

    text = _help_links_body(rows)
    markup = kb_help_links_list(rows)
    try:
        await query.message.edit_text(text=text, reply_markup=markup)
    except TelegramBadRequest:
        pass
    await query.answer()


@router.callback_query(F.data.startswith("ap:hy:"))
async def help_link_delete_do(query: CallbackQuery, settings, session_factory):
    if not query.from_user or not _admin_ok(settings, query.from_user.id):
        await query.answer(tr("admin", "no_permission"), show_alert=True)
        return
    try:
        lid = int((query.data or "").split(":")[2])
    except (IndexError, ValueError):
        await query.answer()
        return

    deleted = False
    async with session_factory() as session:
        async with session.begin():
            deleted = await help_svc.delete_link(session, lid)

    await query.answer(
        tr("admin", "helplink_deleted", id=lid) if deleted else tr("admin", "helplink_not_found")
    )

    async with session_factory() as session:
        async with session.begin():
            rows = await help_svc.list_ordered(session)

    text = _help_links_body(rows)
    markup = kb_help_links_list(rows)
    if query.message:
        try:
            await query.message.edit_text(text=text, reply_markup=markup)
        except TelegramBadRequest:
            await query.message.answer(text=text, reply_markup=markup)


@router.callback_query(F.data == "ap:ha")
async def help_link_add_start(query: CallbackQuery, state: FSMContext, settings, bot: Bot):
    if not query.from_user or not _admin_ok(settings, query.from_user.id):
        await query.answer(tr("admin", "no_permission"), show_alert=True)
        return
    await query.answer()
    await state.set_state(AdminFlow.help_title)
    await bot.send_message(
        chat_id=query.from_user.id,
        text=tr("admin", "help_ask_title"),
        reply_markup=kb_cancel_fsm(),
    )


@router.callback_query(F.data.startswith("ap:hty:"))
async def help_add_pick_type(
    query: CallbackQuery,
    state: FSMContext,
    settings,
    bot: Bot,
):
    if not query.from_user or not _admin_ok(settings, query.from_user.id):
        await query.answer(tr("admin", "no_permission"), show_alert=True)
        return
    suffix = (query.data or "").split(":")[-1]
    data = await state.get_data()
    title = data.get("help_link_title") or ""
    if not title:
        await state.clear()
        await query.answer()
        await bot.send_message(query.from_user.id, tr("admin", "help_title_empty"))
        await _send_admin_home(bot, query.from_user.id)
        return

    await query.answer()
    if suffix == "u":
        await state.set_state(AdminFlow.help_link_url)
        await bot.send_message(
            chat_id=query.from_user.id,
            text=tr("admin", "help_ask_url_only"),
            reply_markup=kb_cancel_fsm(),
        )
        return
    if suffix == "t":
        await state.set_state(AdminFlow.help_article_body)
        await bot.send_message(
            chat_id=query.from_user.id,
            text=tr("admin", "help_ask_article_body"),
            reply_markup=kb_cancel_fsm(),
        )
        return
    await state.clear()
    await _send_admin_home(bot, query.from_user.id)


@router.callback_query(F.data.startswith("ap:hme:"))
async def help_link_edit_open(query: CallbackQuery, settings, session_factory):
    if not query.from_user or not _admin_ok(settings, query.from_user.id):
        await query.answer(tr("admin", "no_permission"), show_alert=True)
        return
    try:
        lid = int((query.data or "").split(":")[2])
    except (IndexError, ValueError):
        await query.answer()
        return

    async with session_factory() as session:
        link = await help_svc.get_by_id(session, lid)

    if link is None:
        await query.answer(tr("admin", "helplink_not_found"), show_alert=True)
        return

    text = _help_edit_preview(link)
    markup = kb_help_edit_menu(link)
    if not query.message:
        await query.answer()
        return
    try:
        await query.message.edit_text(text=text, reply_markup=markup)
    except TelegramBadRequest:
        await query.message.answer(text=text, reply_markup=markup)
    await query.answer()


async def _help_edit_prompt_message(
    bot: Bot,
    uid: int,
    *,
    text_key: str,
    state: FSMContext,
    link_id: int,
    next_state,
):
    await state.update_data(edit_help_id=link_id)
    await state.set_state(next_state)
    await bot.send_message(
        chat_id=uid,
        text=tr("admin", text_key),
        reply_markup=kb_cancel_fsm(),
    )


@router.callback_query(F.data.startswith("ap:hti:"))
async def help_edit_title_start(
    query: CallbackQuery,
    state: FSMContext,
    settings,
    bot: Bot,
):
    if not query.from_user or not _admin_ok(settings, query.from_user.id):
        await query.answer(tr("admin", "no_permission"), show_alert=True)
        return
    try:
        lid = int((query.data or "").split(":")[2])
    except (IndexError, ValueError):
        await query.answer()
        return
    await query.answer()
    await _help_edit_prompt_message(
        bot,
        query.from_user.id,
        text_key="help_ask_edit_title",
        state=state,
        link_id=lid,
        next_state=AdminFlow.help_edit_title,
    )


@router.callback_query(F.data.startswith("ap:hur:"))
async def help_edit_url_start(
    query: CallbackQuery,
    state: FSMContext,
    settings,
    bot: Bot,
):
    if not query.from_user or not _admin_ok(settings, query.from_user.id):
        await query.answer(tr("admin", "no_permission"), show_alert=True)
        return
    try:
        lid = int((query.data or "").split(":")[2])
    except (IndexError, ValueError):
        await query.answer()
        return
    await query.answer()
    await _help_edit_prompt_message(
        bot,
        query.from_user.id,
        text_key="help_ask_edit_url",
        state=state,
        link_id=lid,
        next_state=AdminFlow.help_edit_link,
    )


@router.callback_query(F.data.startswith("ap:htx:"))
async def help_edit_body_start(
    query: CallbackQuery,
    state: FSMContext,
    settings,
    bot: Bot,
):
    if not query.from_user or not _admin_ok(settings, query.from_user.id):
        await query.answer(tr("admin", "no_permission"), show_alert=True)
        return
    try:
        lid = int((query.data or "").split(":")[2])
    except (IndexError, ValueError):
        await query.answer()
        return
    await query.answer()
    await _help_edit_prompt_message(
        bot,
        query.from_user.id,
        text_key="help_ask_edit_body",
        state=state,
        link_id=lid,
        next_state=AdminFlow.help_edit_body,
    )


@router.callback_query(F.data == "ap:bs")
async def block_skip_reason(
    query: CallbackQuery,
    state: FSMContext,
    settings,
    session_factory,
    bot: Bot,
):
    if not query.from_user or not _admin_ok(settings, query.from_user.id):
        await query.answer(tr("admin", "no_permission"), show_alert=True)
        return
    data = await state.get_data()
    uid = data.get("block_target_uid")
    if uid is None:
        await state.clear()
        await query.answer()
        return
    await query.answer()

    async with session_factory() as session:
        async with session.begin():
            await ticket_svc.block_user(session, uid, None)
    await state.clear()
    if query.message:
        await query.message.answer(tr("admin", "block_success", uid=uid))
    await _send_admin_home(bot, query.from_user.id)


@router.message(StateFilter(AdminFlow.block_uid), F.chat.type == ChatType.PRIVATE, F.text)
async def block_receive_uid(
    message: Message,
    state: FSMContext,
    settings,
):
    if not _admin_ok(settings, message.from_user.id if message.from_user else None):
        await state.clear()
        return
    raw = message.text.strip().split(maxsplit=1)[0]
    try:
        uid = int(raw)
    except ValueError:
        await message.answer(tr("admin", "block_bad_id"))
        return
    await state.update_data(block_target_uid=uid)
    await state.set_state(AdminFlow.block_reason)
    await message.answer(tr("admin", "block_ask_reason"), reply_markup=kb_block_skip_reason())


@router.message(StateFilter(AdminFlow.block_reason), F.chat.type == ChatType.PRIVATE, F.text)
async def block_receive_reason(
    message: Message,
    state: FSMContext,
    settings,
    session_factory,
    bot: Bot,
):
    if not _admin_ok(settings, message.from_user.id if message.from_user else None):
        await state.clear()
        return
    data = await state.get_data()
    uid = data.get("block_target_uid")
    if uid is None:
        await state.clear()
        return
    reason = message.text.strip() or None

    async with session_factory() as session:
        async with session.begin():
            await ticket_svc.block_user(session, uid, reason)
    await state.clear()
    await message.answer(tr("admin", "block_success", uid=uid))
    await _send_admin_home(bot, message.chat.id)


@router.message(StateFilter(AdminFlow.unblock_uid), F.chat.type == ChatType.PRIVATE, F.text)
async def unblock_receive_uid(
    message: Message,
    state: FSMContext,
    settings,
    session_factory,
    bot: Bot,
):
    if not _admin_ok(settings, message.from_user.id if message.from_user else None):
        await state.clear()
        return
    raw = message.text.strip().split()[0]
    try:
        uid = int(raw)
    except ValueError:
        await message.answer(tr("admin", "unblock_bad_id"))
        return

    async with session_factory() as session:
        async with session.begin():
            await ticket_svc.unblock_user(session, uid)
    await state.clear()
    await message.answer(tr("admin", "unblock_success", uid=uid))
    await _send_admin_home(bot, message.chat.id)


@router.message(StateFilter(AdminFlow.help_title), F.chat.type == ChatType.PRIVATE, F.text)
async def help_receive_title(message: Message, state: FSMContext, settings):
    if not _admin_ok(settings, message.from_user.id if message.from_user else None):
        await state.clear()
        return
    title = message.text.strip()
    if not title:
        await message.answer(tr("admin", "help_title_empty"))
        return
    await state.update_data(help_link_title=title)
    await state.set_state(AdminFlow.help_pick_type)
    await message.answer(
        tr("admin", "help_pick_type_intro"),
        reply_markup=kb_help_add_pick_type(),
    )


@router.message(StateFilter(AdminFlow.help_pick_type), F.chat.type == ChatType.PRIVATE, F.text)
async def help_pick_type_ignore_text(message: Message, settings):
    if not _admin_ok(settings, message.from_user.id if message.from_user else None):
        return
    await message.answer(tr("admin", "help_pick_type_use_buttons"))


@router.message(StateFilter(AdminFlow.help_link_url), F.chat.type == ChatType.PRIVATE, F.text)
async def help_receive_link_url(
    message: Message,
    state: FSMContext,
    settings,
    session_factory,
    bot: Bot,
):
    if not _admin_ok(settings, message.from_user.id if message.from_user else None):
        await state.clear()
        return
    raw = (message.text or "").strip()
    if not raw or "\n" in raw:
        await message.answer(tr("admin", "help_url_single_line_expected"))
        return
    if not help_svc.url_like(raw):
        await message.answer(tr("admin", "helplink_bad_url"))
        return
    data = await state.get_data()
    title = data.get("help_link_title") or ""
    if not title:
        await state.clear()
        await message.answer(tr("admin", "help_title_empty"))
        return

    try:
        async with session_factory() as session:
            async with session.begin():
                row = await help_svc.add_link(session, title=title, url=raw)
    except ValueError:
        await message.answer(tr("admin", "help_link_validation"))
        return

    await state.clear()
    await message.answer(tr("admin", "helplink_added", id=row.id, title=row.title[:80]))

    async with session_factory() as session:
        async with session.begin():
            rows = await help_svc.list_ordered(session)

    text = _help_links_body(rows)
    markup = kb_help_links_list(rows)
    await bot.send_message(
        chat_id=message.chat.id,
        text=text,
        reply_markup=markup,
    )


@router.message(StateFilter(AdminFlow.help_article_body), F.chat.type == ChatType.PRIVATE, F.text)
async def help_receive_article_body(
    message: Message,
    state: FSMContext,
    settings,
    session_factory,
    bot: Bot,
):
    if not _admin_ok(settings, message.from_user.id if message.from_user else None):
        await state.clear()
        return
    raw = (message.text or "").strip()
    if not raw:
        await message.answer(tr("admin", "help_payload_empty"))
        return
    data = await state.get_data()
    title = data.get("help_link_title") or ""
    if not title:
        await state.clear()
        await message.answer(tr("admin", "help_title_empty"))
        return

    try:
        async with session_factory() as session:
            async with session.begin():
                row = await help_svc.add_link(session, title=title, body_text=raw)
    except ValueError:
        await message.answer(tr("admin", "help_link_validation"))
        return

    await state.clear()
    await message.answer(tr("admin", "helplink_added", id=row.id, title=row.title[:80]))

    async with session_factory() as session:
        async with session.begin():
            rows = await help_svc.list_ordered(session)

    text = _help_links_body(rows)
    markup = kb_help_links_list(rows)
    await bot.send_message(
        chat_id=message.chat.id,
        text=text,
        reply_markup=markup,
    )


@router.message(StateFilter(AdminFlow.help_edit_title), F.chat.type == ChatType.PRIVATE, F.text)
async def help_receive_edit_title(
    message: Message,
    state: FSMContext,
    settings,
    session_factory,
    bot: Bot,
):
    if not _admin_ok(settings, message.from_user.id if message.from_user else None):
        await state.clear()
        return
    data = await state.get_data()
    lid = data.get("edit_help_id")
    if lid is None:
        await state.clear()
        return
    title = message.text.strip()
    if not title:
        await message.answer(tr("admin", "help_title_empty"))
        return

    async with session_factory() as session:
        async with session.begin():
            row = await help_svc.update_title(session, int(lid), title)

    await state.clear()
    if row is None:
        await message.answer(tr("admin", "helplink_not_found"))
        await _send_admin_home(bot, message.chat.id)
        return

    await message.answer(tr("admin", "help_edit_saved"))

    async with session_factory() as session:
        async with session.begin():
            rows = await help_svc.list_ordered(session)

    await bot.send_message(
        chat_id=message.chat.id,
        text=_help_links_body(rows),
        reply_markup=kb_help_links_list(rows),
    )


@router.message(StateFilter(AdminFlow.help_edit_link), F.chat.type == ChatType.PRIVATE, F.text)
async def help_receive_edit_link(
    message: Message,
    state: FSMContext,
    settings,
    session_factory,
    bot: Bot,
):
    if not _admin_ok(settings, message.from_user.id if message.from_user else None):
        await state.clear()
        return
    data = await state.get_data()
    lid = data.get("edit_help_id")
    if lid is None:
        await state.clear()
        return
    raw = (message.text or "").strip()
    if not raw or "\n" in raw:
        await message.answer(tr("admin", "help_url_single_line_expected"))
        return
    if not help_svc.url_like(raw):
        await message.answer(tr("admin", "helplink_bad_url"))
        return

    try:
        async with session_factory() as session:
            async with session.begin():
                row = await help_svc.update_url_or_body(session, int(lid), raw)
    except ValueError:
        await message.answer(tr("admin", "help_link_validation"))
        return

    await state.clear()
    if row is None:
        await message.answer(tr("admin", "helplink_not_found"))
        await _send_admin_home(bot, message.chat.id)
        return

    await message.answer(tr("admin", "help_edit_saved"))

    async with session_factory() as session:
        async with session.begin():
            rows = await help_svc.list_ordered(session)

    await bot.send_message(
        chat_id=message.chat.id,
        text=_help_links_body(rows),
        reply_markup=kb_help_links_list(rows),
    )


@router.message(StateFilter(AdminFlow.help_edit_body), F.chat.type == ChatType.PRIVATE, F.text)
async def help_receive_edit_body(
    message: Message,
    state: FSMContext,
    settings,
    session_factory,
    bot: Bot,
):
    if not _admin_ok(settings, message.from_user.id if message.from_user else None):
        await state.clear()
        return
    data = await state.get_data()
    lid = data.get("edit_help_id")
    if lid is None:
        await state.clear()
        return
    raw = (message.text or "").strip()
    if not raw:
        await message.answer(tr("admin", "help_payload_empty"))
        return

    try:
        async with session_factory() as session:
            async with session.begin():
                row = await help_svc.update_url_or_body(session, int(lid), raw)
    except ValueError:
        await message.answer(tr("admin", "help_link_validation"))
        return

    await state.clear()
    if row is None:
        await message.answer(tr("admin", "helplink_not_found"))
        await _send_admin_home(bot, message.chat.id)
        return

    await message.answer(tr("admin", "help_edit_saved"))

    async with session_factory() as session:
        async with session.begin():
            rows = await help_svc.list_ordered(session)

    await bot.send_message(
        chat_id=message.chat.id,
        text=_help_links_body(rows),
        reply_markup=kb_help_links_list(rows),
    )


@router.message(
    StateFilter(AdminFlow),
    F.chat.type == ChatType.PRIVATE,
    F.content_type != ContentType.TEXT,
)
async def admin_fsm_non_text(message: Message, state: FSMContext, settings):
    if not _admin_ok(settings, message.from_user.id if message.from_user else None):
        await state.clear()
        return
    await message.answer(tr("admin", "expect_text"))
