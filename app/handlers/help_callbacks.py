from __future__ import annotations

import logging
from html import escape

from aiogram import Bot, F, Router
from aiogram.enums import ChatType, ParseMode
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import CallbackQuery

from app.i18n import tr
from app.keyboards.help_menu_kb import kb_help_main_menu, kb_help_text_back
from app.services import help_links as help_svc
from app.services.help_menu_delivery import send_help_menu_to_chat
from app.services.help_article_html import linkify_plain_help_body

log = logging.getLogger(__name__)

router = Router(name="help_callbacks")

MAX_HELP_ARTICLE_LEN = 3800


@router.callback_query(F.data == "hlp")
async def open_help_from_inline(query: CallbackQuery, session_factory):
    """Инлайн-кнопка «Помощь» под приветствием /start."""
    if not query.message or query.message.chat.type != ChatType.PRIVATE:
        await query.answer()
        return
    await send_help_menu_to_chat(query.bot, query.message.chat.id, session_factory)
    await query.answer()


@router.callback_query(F.data.startswith("hlo:"))
async def open_help_article(
    query: CallbackQuery,
    bot: Bot,
    session_factory,
):
    if not query.message or query.message.chat.type != ChatType.PRIVATE:
        await query.answer()
        return
    try:
        lid = int((query.data or "").split(":", maxsplit=1)[1])
    except (IndexError, ValueError):
        await query.answer()
        return

    async with session_factory() as session:
        link = await help_svc.get_by_id(session, lid)

    if link is None:
        await query.answer(tr("user", "help_item_not_found"), show_alert=True)
        return

    body = (link.body_text or "").strip()
    if not body:
        await query.answer(tr("user", "help_text_missing"), show_alert=True)
        return

    # Тело: либо HTML от админа (<a>, <b>, …), либо простой текст — тогда «голые» URL делаем кликабельными.
    body_html = linkify_plain_help_body(body)
    if len(body_html) > MAX_HELP_ARTICLE_LEN:
        body_html = body_html[: MAX_HELP_ARTICLE_LEN - 1] + "…"

    title_esc = escape(link.title)
    page = f"<b>{title_esc}</b>\n\n{body_html}"

    try:
        await query.message.edit_text(
            text=page,
            reply_markup=kb_help_text_back(),
            parse_mode=ParseMode.HTML,
        )
    except TelegramBadRequest as e:
        log.warning("help article edit (проверьте разметку HTML в body_text): %s", e)
        await query.answer(tr("user", "help_html_invalid"), show_alert=True)
        return

    await query.answer()


@router.callback_query(F.data == "hlb")
async def close_help_article(query: CallbackQuery, session_factory):
    if not query.message or query.message.chat.type != ChatType.PRIVATE:
        await query.answer()
        return

    async with session_factory() as session:
        async with session.begin():
            links = await help_svc.list_ordered(session)

    intro = tr("user", "help_section_intro")
    markup = kb_help_main_menu(links)

    try:
        await query.message.edit_text(text=intro, reply_markup=markup)
    except TelegramBadRequest as e:
        log.warning("help menu restore: %s", e)

    await query.answer()
