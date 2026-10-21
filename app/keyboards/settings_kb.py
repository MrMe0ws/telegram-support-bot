"""Клавиатуры раздела «⚙️ Настройки» (callback-префикс st:)."""

from __future__ import annotations

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from app.i18n import tr

# Telegram обрезает длинные подписи кнопок — режем сами, аккуратно.
_BTN_MAX = 48

# Буква значка в callback → суффикс настройки topic_icon_emoji_*.
_SLOTS = {"o": "open", "c": "closed", "w": "waiting"}


def slot_key(slot: str) -> str:
    return f"topic_icon_emoji_{_SLOTS[slot]}"


def slot_of(key: str) -> str | None:
    for slot, name in _SLOTS.items():
        if key == f"topic_icon_emoji_{name}":
            return slot
    return None


def btn(text: str, data: str) -> InlineKeyboardButton:
    if len(text) > _BTN_MAX:
        text = text[: _BTN_MAX - 1] + "…"
    return InlineKeyboardButton(text=text, callback_data=data)


def kb(rows: list[list[InlineKeyboardButton]]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[r for r in rows if r])


def back_row(data: str) -> list[InlineKeyboardButton]:
    return [btn(tr("settings", "btn_back"), data)]


def chunk(buttons: list[InlineKeyboardButton], size: int) -> list[list[InlineKeyboardButton]]:
    return [buttons[i : i + size] for i in range(0, len(buttons), size)]


def kb_home() -> InlineKeyboardMarkup:
    return kb(
        [
            [btn(tr("settings", "home_btn_ai"), "st:go:ai")],
            [btn(tr("settings", "home_btn_tickets"), "st:go:tickets")],
            [btn(tr("settings", "home_btn_texts"), "st:go:texts")],
            [btn(tr("settings", "home_btn_integrations"), "st:go:integrations")],
            [btn(tr("settings", "home_btn_system"), "st:go:system")],
            [btn(tr("settings", "btn_to_panel"), "ap:ho")],
        ]
    )


def kb_field(
    key: str,
    presets: list[tuple[str, bool]],
    *,
    custom: bool,
    reset_label: str | None,
    back: str,
) -> InlineKeyboardMarkup:
    """Экран одной настройки: готовые значения, «Своё значение», «Как в .env», «Назад»."""
    buttons = [
        btn(tr("settings", "current_mark", label=label) if current else label, f"st:p:{key}:{i}")
        for i, (label, current) in enumerate(presets)
    ]
    longest = max((len(label) for label, _ in presets), default=0)
    max_per_row = 5 if longest <= 6 else 2 if longest <= 18 else 1
    # Ровные ряды: 6 значений — 3+3, а не 5+1.
    row_count = -(-len(buttons) // max_per_row)
    rows = chunk(buttons, -(-len(buttons) // row_count)) if buttons else []
    if custom:
        rows.append([btn(tr("settings", "btn_custom"), f"st:e:{key}")])
    if reset_label is not None:
        rows.append([btn(reset_label, f"st:r:{key}")])
    rows.append(back_row(back))
    return kb(rows)


def kb_cancel() -> InlineKeyboardMarkup:
    return kb([[btn(tr("settings", "btn_cancel"), "st:x")]])


def kb_icons(
    slot: str,
    icons: list[tuple[str, str]],
    current_id: str,
    reset_label: str | None,
) -> InlineKeyboardMarkup:
    """Сетка значков тем: (custom_emoji_id, эмодзи). Текущий — в точках."""
    buttons = [
        btn(f"·{emoji}·" if icon_id == current_id else emoji, f"st:is:{slot}:{icon_id}")
        for icon_id, emoji in icons
    ]
    rows = chunk(buttons, 8)
    rows.append([btn(tr("settings", "icons_btn_none"), f"st:is:{slot}:-")])
    if reset_label is not None:
        rows.append([btn(reset_label, f"st:r:{slot_key(slot)}")])
    rows.append(back_row("st:go:tickets"))
    return kb(rows)


def kb_text_preview() -> InlineKeyboardMarkup:
    return kb(
        [
            [btn(tr("settings", "text_btn_save"), "st:ts")],
            [btn(tr("settings", "text_btn_retry"), "st:tw"), btn(tr("settings", "btn_cancel"), "st:x")],
        ]
    )


def kb_reset_all() -> InlineKeyboardMarkup:
    return kb(
        [
            [
                btn(tr("settings", "reset_all_yes"), "st:ray"),
                btn(tr("settings", "reset_all_no"), "st:go:system"),
            ]
        ]
    )
