from __future__ import annotations

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from app.db.models import HelpMenuLink
from app.i18n import tr
from app.services import help_links as help_svc


def kb_admin_home() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=tr("admin", "panel_btn_block"),
                    callback_data="ap:b",
                ),
                InlineKeyboardButton(
                    text=tr("admin", "panel_btn_unblock"),
                    callback_data="ap:u",
                ),
            ],
            [
                InlineKeyboardButton(
                    text=tr("admin", "panel_btn_help_links"),
                    callback_data="ap:h",
                ),
            ],
            [
                InlineKeyboardButton(
                    text=tr("admin", "panel_btn_close_msg"),
                    callback_data="ap:x",
                ),
            ],
        ]
    )


def kb_cancel_fsm() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=tr("admin", "cancel"),
                    callback_data="ap:ca",
                ),
            ],
        ]
    )


def kb_block_skip_reason() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=tr("admin", "block_skip_reason_btn"),
                    callback_data="ap:bs",
                ),
                InlineKeyboardButton(
                    text=tr("admin", "cancel"),
                    callback_data="ap:ca",
                ),
            ],
        ]
    )


def kb_help_add_pick_type() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=tr("admin", "help_add_type_link_btn"),
                    callback_data="ap:hty:u",
                ),
                InlineKeyboardButton(
                    text=tr("admin", "help_add_type_text_btn"),
                    callback_data="ap:hty:t",
                ),
            ],
            [
                InlineKeyboardButton(
                    text=tr("admin", "cancel"),
                    callback_data="ap:ca",
                ),
            ],
        ]
    )


def kb_help_edit_menu(link: HelpMenuLink) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = [
        [
            InlineKeyboardButton(
                text=tr("admin", "help_edit_btn_title"),
                callback_data=f"ap:hti:{link.id}",
            ),
        ],
    ]
    u = (link.url or "").strip()
    b = (link.body_text or "").strip()
    has_url = help_svc.url_like(u)
    if has_url:
        rows.append(
            [
                InlineKeyboardButton(
                    text=tr("admin", "help_edit_btn_url"),
                    callback_data=f"ap:hur:{link.id}",
                ),
            ]
        )
    if b:
        rows.append(
            [
                InlineKeyboardButton(
                    text=tr("admin", "help_edit_btn_body"),
                    callback_data=f"ap:htx:{link.id}",
                ),
            ]
        )
    if not has_url and not b:
        rows.append(
            [
                InlineKeyboardButton(
                    text=tr("admin", "help_edit_btn_url"),
                    callback_data=f"ap:hur:{link.id}",
                ),
            ]
        )
        rows.append(
            [
                InlineKeyboardButton(
                    text=tr("admin", "help_edit_btn_body"),
                    callback_data=f"ap:htx:{link.id}",
                ),
            ]
        )
    rows.append(
        [
            InlineKeyboardButton(
                text=tr("admin", "help_edit_back_list"),
                callback_data="ap:h",
            ),
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def kb_help_links_list(links: list[HelpMenuLink]) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    for link in links:
        rows.append(
            [
                InlineKeyboardButton(
                    text=tr("admin", "help_link_btn_up"),
                    callback_data=f"ap:hu:{link.id}",
                ),
                InlineKeyboardButton(
                    text=tr("admin", "help_link_btn_down"),
                    callback_data=f"ap:hd:{link.id}",
                ),
                InlineKeyboardButton(
                    text=tr("admin", "help_link_btn_del"),
                    callback_data=f"ap:hk:{link.id}",
                ),
                InlineKeyboardButton(
                    text=tr("admin", "help_link_btn_edit"),
                    callback_data=f"ap:hme:{link.id}",
                ),
            ]
        )
    rows.append(
        [
            InlineKeyboardButton(
                text=tr("admin", "help_link_add_btn"),
                callback_data="ap:ha",
            ),
        ]
    )
    rows.append(
        [
            InlineKeyboardButton(
                text=tr("admin", "back_to_panel"),
                callback_data="ap:ho",
            ),
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def kb_help_delete_confirm(link_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=tr("admin", "confirm_yes"),
                    callback_data=f"ap:hy:{link_id}",
                ),
                InlineKeyboardButton(
                    text=tr("admin", "confirm_no"),
                    callback_data="ap:hn",
                ),
            ],
        ]
    )
