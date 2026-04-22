"""Отправка клиенту экрана «Помощь» (список из БД)."""

from __future__ import annotations

from aiogram import Bot

from app.i18n import tr
from app.keyboards.help_menu_kb import kb_help_main_menu
from app.services import help_links as help_svc


async def send_help_menu_to_chat(bot: Bot, chat_id: int, session_factory) -> None:
    async with session_factory() as session:
        async with session.begin():
            links = await help_svc.list_ordered(session)
    intro = tr("user", "help_section_intro")
    markup = kb_help_main_menu(links)
    await bot.send_message(chat_id, intro, reply_markup=markup)
