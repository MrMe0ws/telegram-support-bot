from __future__ import annotations

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from app.db.models import HelpMenuLink
from app.i18n import tr


def kb_start_inline(*, is_admin: bool = False) -> InlineKeyboardMarkup:
    """Инлайн под приветствием /start: «Помощь»; для админа ещё «Админ-панель» (callback aop)."""
    row = [
        InlineKeyboardButton(
            text=tr("user", "btn_help"),
            callback_data="hlp",
        ),
    ]
    if is_admin:
        row.append(
            InlineKeyboardButton(
                text=tr("admin", "btn_admin_panel"),
                callback_data="aop",
            ),
        )
    return InlineKeyboardMarkup(inline_keyboard=[row])


def kb_help_main_menu(links: list[HelpMenuLink]) -> InlineKeyboardMarkup | None:
    rows: list[list[InlineKeyboardButton]] = []
    for link in links:
        title = link.title[:64]
        raw_url = (link.url or "").strip()
        body = (link.body_text or "").strip()
        ul = raw_url.lower()
        if ul.startswith(("http://", "https://", "tg://")):
            rows.append([InlineKeyboardButton(text=title, url=raw_url)])
        elif body:
            rows.append(
                [
                    InlineKeyboardButton(
                        text=title,
                        callback_data=f"hlo:{link.id}",
                    ),
                ]
            )
    return InlineKeyboardMarkup(inline_keyboard=rows) if rows else None


def kb_help_text_back() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=tr("user", "help_text_back_btn"),
                    callback_data="hlb",
                ),
            ],
        ]
    )
