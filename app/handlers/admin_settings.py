"""
Админ-панель → «⚙️ Настройки» (callback-префикс st:).

Экраны: главный → ИИ (с подменю «Модели»), Тикеты и темы, Тексты для клиентов,
Интеграции, Система. Переключатели меняются одним нажатием, числа и строки —
на отдельном экране настройки: готовые значения, «Своё значение», «Как в .env».
Всё применяется сразу (services/live_settings.py).
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from dataclasses import dataclass
from html import escape as html_escape
from urllib.parse import urlsplit

from aiogram import Bot, F, Router
from aiogram.enums import ChatType
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import BaseFilter, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message
from sqlalchemy import func, select

from app.config import AI_PROVIDERS, parse_ai_models
from app.db.models import Ticket, TicketStatus
from app.i18n import tr, tr_default
from app.keyboards.settings_kb import (
    back_row,
    btn,
    kb,
    kb_cancel,
    kb_field,
    kb_home,
    kb_icons,
    kb_reset_all,
    kb_text_preview,
    slot_key,
    slot_of,
)
from app.services import live_settings
from app.services.ai import model_choice
from app.services.ai.provider import PROVIDER_TITLES, ModelChain, ModelCheck, check_models
from app.services.ai.service import peek_ai_service
from app.services.remnawave_client import RemnawaveClient
from app.states import SettingsFlow

log = logging.getLogger(__name__)

router = Router(name="admin_settings")

_STARTED = time.monotonic()


class _AdminOnly(BaseFilter):
    async def __call__(self, event, settings) -> bool:
        user = event.from_user
        return user is not None and user.id in settings.admin_ids


router.callback_query.filter(_AdminOnly())
router.message.filter(F.chat.type == ChatType.PRIVATE, _AdminOnly())


@dataclass(frozen=True)
class FieldUi:
    """Где настройка живёт в меню и какие значения предложить кнопками."""

    parent: str
    presets: tuple[str, ...] = ()
    custom: bool = True


FIELD_UI: dict[str, FieldUi] = {
    "ai.reply_delay_sec": FieldUi("st:go:ai", ("0", "5", "10", "20", "30", "60")),
    "ai.max_replies_per_ticket": FieldUi("st:go:ai", ("3", "5", "10", "15", "30")),
    "ai.history_limit": FieldUi("st:go:ai", ("10", "20", "30", "50", "100")),
    "ai.media_max_mb": FieldUi("st:go:ai", ("5", "10", "15", "20")),
    "ai.human_button_from": FieldUi("st:go:ai", ("0", "1", "2", "3", "5")),
    "ai.assistant_name": FieldUi("st:go:ai"),
    "topic_name_template": FieldUi(
        "st:go:tickets",
        ("{username} · #{ticket_id}", "#{ticket_id} · {name}", "{name} ({username}) · #{ticket_id}"),
    ),
    "remnawave.timezone": FieldUi(
        "st:go:integrations",
        (
            "Europe/Moscow",
            "Europe/Kaliningrad",
            "Europe/Samara",
            "Asia/Yekaterinburg",
            "Asia/Novosibirsk",
            "Europe/Minsk",
            "Asia/Almaty",
            "UTC",
        ),
    ),
    "log_level": FieldUi("st:go:system", live_settings.LOG_LEVELS, custom=False),
}

# Переключатели: после нажатия перерисовывается экран, где стоит кнопка.
TOGGLE_PARENT = {
    "ai.enabled": "st:go:ai",
    "ai.mode": "st:go:ai",
    "ai.media_enabled": "st:go:ai",
    "remnawave.enabled": "st:go:integrations",
}


# ── Вывод значений ──────────────────────────────────────────────────────


def _slug(key: str) -> str:
    return key.replace(".", "_")


def fmt(key: str, value, *, html: bool = True) -> str:
    """Значение настройки для человека: «20 с», «с 1-го ответа», «никогда»."""
    if live_settings.FIELDS[key].kind == "bool":
        return tr("settings", "on" if value else "off")
    if key == "ai.human_button_from" and int(value) == 0:
        return tr("settings", "f_ai_human_button_from_never")
    if isinstance(value, float):
        value = f"{value:g}"
    shown = tr("settings", f"f_{_slug(key)}_value", v=html_escape(str(value)) if html else str(value))
    return shown if html else re.sub(r"</?code>", "", shown)


def _edited(key: str) -> str:
    return f" {tr('settings', 'edited_mark')}" if live_settings.is_overridden(key) else ""


def _models_edited(chain: ModelChain) -> str:
    return f" {tr('settings', 'edited_mark')}" if model_choice.is_overridden(chain) else ""


def _source(key: str) -> str:
    if not live_settings.is_overridden(key):
        return tr("settings", "source_env")
    return tr("settings", "source_bot", value=fmt(key, live_settings.env_value(key)))


def _reset_label(key: str) -> str | None:
    if not live_settings.is_overridden(key):
        return None
    return tr("settings", "btn_reset", value=fmt(key, live_settings.env_value(key), html=False))


async def _show(query: CallbackQuery, text: str, markup: InlineKeyboardMarkup) -> None:
    """Перерисовать экран в том же сообщении (или прислать новое, если старое не правится)."""
    if query.message is None:
        return
    try:
        await query.message.edit_text(text, reply_markup=markup)
    except TelegramBadRequest as e:
        if "not modified" not in str(e):
            await query.message.answer(text, reply_markup=markup)


# ── Значки тем ──────────────────────────────────────────────────────────

# Набор значков тем Telegram: (custom_emoji_id, эмодзи). Не меняется — берём один раз.
_icons: list[tuple[str, str]] | None = None


async def _topic_icons(bot: Bot) -> list[tuple[str, str]]:
    global _icons
    if _icons is None:
        try:
            stickers = await bot.get_forum_topic_icon_stickers()
        except Exception as e:
            log.warning("Набор значков тем не получен: %s", e)
            return []
        _icons = [(s.custom_emoji_id, s.emoji or "•") for s in stickers if s.custom_emoji_id]
    return _icons


async def _icon_label(bot: Bot, icon_id: str) -> str:
    if not icon_id:
        return tr("settings", "icon_none")
    for known_id, emoji in await _topic_icons(bot):
        if known_id == icon_id:
            return emoji
    return tr("settings", "icon_unknown", id=icon_id)


# ── Экраны ──────────────────────────────────────────────────────────────


@dataclass
class Ctx:
    bot: Bot
    session_factory: object
    settings: object


def _ai_chain() -> ModelChain | None:
    svc = peek_ai_service()
    return svc.chain if svc is not None else None


def _texts_edited() -> int:
    return sum(live_settings.is_overridden(live_settings.text_key(s, k)) for s, k in live_settings.TEXTS)


def _ai_summary(s) -> str:
    if peek_ai_service() is None:
        return tr("settings", "home_ai_none")
    if not s.ai.enabled:
        return tr("settings", "home_ai_off")
    return tr("settings", "home_ai_on", mode=tr("settings", f"mode_short_{s.ai.mode}"))


async def view_home(ctx: Ctx):
    s = ctx.settings
    edited = _texts_edited()
    texts = (
        tr("settings", "home_texts_edited", n=edited, total=len(live_settings.TEXTS))
        if edited
        else tr("settings", "home_texts_default")
    )
    text = tr(
        "settings",
        "home",
        ai=_ai_summary(s),
        topic=html_escape(s.topic_name_template),
        texts=texts,
        remnawave=_remnawave_state(s),
    )
    return text, kb_home()


async def view_ai(ctx: Ctx):
    s = ctx.settings.ai
    chain = _ai_chain()
    if chain is None:
        return tr("settings", "ai_not_configured"), kb([back_row("st:go:home")])
    main, *fallback = chain.models
    models = tr(
        "settings",
        "ai_models_summary",
        main=html_escape(main),
        fallback=f" {tr('settings', 'ai_models_fallback', n=len(fallback))}" if fallback else "",
    )
    text = tr(
        "settings",
        "ai",
        state=tr("settings", "ai_state_on" if s.enabled else "ai_state_off"),
        mode=tr("settings", f"mode_{s.mode}"),
        models=models,
    )

    def field_btn(label_key: str, key: str):
        value = live_settings.value(key)
        return btn(tr("settings", label_key, value=fmt(key, value, html=False)) + _edited(key), f"st:f:{key}")

    rows = [
        [
            btn(
                tr("settings", "ai_btn_enabled_on" if s.enabled else "ai_btn_enabled_off") + _edited("ai.enabled"),
                "st:t:ai.enabled",
            )
        ],
        [btn(tr("settings", f"ai_btn_mode_{s.mode}") + _edited("ai.mode"), "st:t:ai.mode")],
        [
            btn(tr("settings", "ai_btn_models") + _models_edited(chain), "st:go:models"),
            btn(tr("settings", "ai_btn_check"), "st:ck"),
        ],
        [field_btn("ai_btn_delay", "ai.reply_delay_sec")],
        [field_btn("ai_btn_limit", "ai.max_replies_per_ticket")],
        [field_btn("ai_btn_history", "ai.history_limit")],
        [
            btn(
                tr("settings", "ai_btn_media_on" if s.media_enabled else "ai_btn_media_off")
                + _edited("ai.media_enabled"),
                "st:t:ai.media_enabled",
            )
        ],
        [field_btn("ai_btn_media_size", "ai.media_max_mb")] if s.media_enabled else [],
        [field_btn("ai_btn_human", "ai.human_button_from")],
        [field_btn("ai_btn_name", "ai.assistant_name")],
        back_row("st:go:home"),
    ]
    return text, kb(rows)


def _models_view(chain: ModelChain):
    lines = [
        tr(
            "settings",
            "models_line",
            idx=idx,
            mark=f"{tr('settings', 'models_main_mark')} " if idx == 1 else "",
            spec=html_escape(spec),
            note="" if chain.has_key(spec) else f" {tr('settings', 'models_no_key')}",
        )
        for idx, spec in enumerate(chain.models, start=1)
    ]
    keys = " · ".join(f"{PROVIDER_TITLES[p]} {'✅' if chain.has_key(f'{p}:') else '❌'}" for p in AI_PROVIDERS)
    overridden = model_choice.is_overridden(chain)
    text = tr(
        "settings",
        "models",
        lines="\n".join(lines),
        keys=keys,
        source=tr("settings", "models_source_bot" if overridden else "models_source_env"),
    )
    rows = [[btn(f"{'⭐' if i == 0 else '▫️'} {spec}", f"st:mm:{i}")] for i, spec in enumerate(chain.models)]
    rows.append([btn(tr("settings", "models_btn_edit"), "st:me")])
    if overridden:
        rows.append([btn(tr("settings", "models_btn_reset"), "st:mr")])
    rows.append(back_row("st:go:ai"))
    return text, kb(rows)


async def view_models(ctx: Ctx):
    chain = _ai_chain()
    if chain is None:
        return await view_ai(ctx)
    return _models_view(chain)


async def view_tickets(ctx: Ctx):
    s = ctx.settings
    icons = {slot: await _icon_label(ctx.bot, getattr(s, slot_key(slot))) for slot in "ocw"}
    text = tr(
        "settings",
        "tickets",
        template=html_escape(s.topic_name_template),
        example=html_escape(live_settings.render_topic_name(s.topic_name_template)),
        icon_open=icons["o"],
        icon_closed=icons["c"],
        icon_waiting=icons["w"],
    )
    rows = [
        [btn(tr("settings", "tickets_btn_name") + _edited("topic_name_template"), "st:f:topic_name_template")],
        [btn(tr("settings", "tickets_btn_icon_open", icon=icons["o"]) + _edited(slot_key("o")), "st:ic:o")],
        [btn(tr("settings", "tickets_btn_icon_closed", icon=icons["c"]) + _edited(slot_key("c")), "st:ic:c")],
        [btn(tr("settings", "tickets_btn_icon_waiting", icon=icons["w"]) + _edited(slot_key("w")), "st:ic:w")],
        back_row("st:go:home"),
    ]
    return text, kb(rows)


async def view_icons(ctx: Ctx, slot: str):
    key = slot_key(slot)
    name = key.removeprefix("topic_icon_emoji_")
    icons = await _topic_icons(ctx.bot)
    if not icons:
        return tr("settings", "icons_unavailable"), kb([back_row("st:go:tickets")])
    current = live_settings.value(key)
    if live_settings.is_overridden(key):
        env_icon = await _icon_label(ctx.bot, live_settings.env_value(key))
        source = tr("settings", "source_bot", value=env_icon)
        reset = tr("settings", "btn_reset", value=env_icon)
    else:
        source, reset = tr("settings", "source_env"), None
    text = tr(
        "settings",
        "icons",
        slot=tr("settings", f"icon_slot_{name}"),
        hint=tr("settings", f"icon_hint_{name}"),
        icon=await _icon_label(ctx.bot, current),
        source=source,
    )
    return text, kb_icons(slot, icons, current, reset)


async def view_texts(ctx: Ctx):
    rows = []
    for i, (stem, key) in enumerate(live_settings.TEXTS):
        title = tr("settings", f"t_{stem}_{key}_title")
        rows.append([btn(title + _edited(live_settings.text_key(stem, key)), f"st:tv:{i}")])
    rows.append(back_row("st:go:home"))
    return tr("settings", "texts"), kb(rows)


def _text_view(i: int):
    stem, key = live_settings.TEXTS[i]
    tkey = live_settings.text_key(stem, key)
    edited = live_settings.is_overridden(tkey)
    text = tr(
        "settings",
        "text_view",
        title=tr("settings", f"t_{stem}_{key}_title"),
        hint=tr("settings", f"t_{stem}_{key}_hint"),
        source=tr("settings", "text_source_bot" if edited else "text_source_default"),
        text=tr(stem, key),
    )
    rows = [[btn(tr("settings", "text_btn_edit"), f"st:te:{i}")]]
    if edited:
        rows.append([btn(tr("settings", "btn_reset_text"), f"st:tr:{i}")])
    rows.append(back_row("st:go:texts"))
    return text, kb(rows)


def _remnawave_ready(s) -> bool:
    return bool(s.remnawave.token and s.remnawave.base_url)


def _remnawave_state(s) -> str:
    if not _remnawave_ready(s):
        return tr("settings", "rw_not_configured")
    if not s.remnawave.enabled:
        return tr("settings", "rw_off")
    return tr("settings", "rw_on", url=html_escape(s.remnawave.base_url))


async def view_integrations(ctx: Ctx):
    s = ctx.settings
    text = tr(
        "settings",
        "integrations",
        remnawave=_remnawave_state(s),
        tz=html_escape(s.remnawave.timezone),
        cabinet=tr(
            "settings", "cabinet_on" if s.shop_webhook_url and s.support_bridge_secret else "cabinet_off"
        ),
    )
    rows = []
    if _remnawave_ready(s):
        rows.append(
            [
                btn(
                    tr("settings", "int_btn_rw_on" if s.remnawave.enabled else "int_btn_rw_off")
                    + _edited("remnawave.enabled"),
                    "st:t:remnawave.enabled",
                )
            ]
        )
        if s.remnawave.enabled:
            rows.append([btn(tr("settings", "int_btn_rw_check"), "st:rwc")])
    rows.append(
        [btn(tr("settings", "int_btn_tz", value=s.remnawave.timezone) + _edited("remnawave.timezone"), "st:f:remnawave.timezone")]
    )
    rows.append(back_row("st:go:home"))
    return text, kb(rows)


def _uptime() -> str:
    minutes = int(time.monotonic() - _STARTED) // 60
    d, rest = divmod(minutes, 24 * 60)
    h, m = divmod(rest, 60)
    if d:
        return tr("settings", "uptime_days", d=d, h=h)
    if h:
        return tr("settings", "uptime_hours", h=h, m=m)
    return tr("settings", "uptime_minutes", m=m)


async def view_system(ctx: Ctx):
    async with ctx.session_factory() as session:
        open_tickets = (
            await session.execute(select(func.count(Ticket.id)).where(Ticket.status == TicketStatus.open.value))
        ).scalar_one()
    chain = _ai_chain()
    overridden = len(live_settings.overridden_keys()) + (1 if chain and model_choice.is_overridden(chain) else 0)
    level = live_settings.value("log_level")
    text = tr(
        "settings",
        "system",
        uptime=_uptime(),
        open_tickets=open_tickets,
        ai=_ai_summary(ctx.settings),
        log_level=level,
        overridden=overridden,
    )
    rows = [
        [btn(tr("settings", "sys_btn_log", value=level) + _edited("log_level"), "st:f:log_level")],
        [btn(tr("settings", "btn_refresh"), "st:go:system")],
    ]
    if overridden:
        rows.append([btn(tr("settings", "sys_btn_reset_all"), "st:ra")])
    rows.append(back_row("st:go:home"))
    return text, kb(rows)


SCREENS = {
    "home": view_home,
    "ai": view_ai,
    "models": view_models,
    "tickets": view_tickets,
    "texts": view_texts,
    "integrations": view_integrations,
    "system": view_system,
}


def view_field(key: str):
    field = live_settings.FIELDS[key]
    ui = FIELD_UI[key]
    value = live_settings.value(key)
    hint_kwargs = {}
    if key == "topic_name_template":
        hint_kwargs["example"] = html_escape(live_settings.render_topic_name(value))
    text = tr(
        "settings",
        "field",
        title=tr("settings", f"f_{_slug(key)}_title"),
        hint=tr("settings", f"f_{_slug(key)}_hint", **hint_kwargs),
        value=fmt(key, value),
        source=_source(key),
    )
    # Строки не проверяем: часовые пояса без tzdata (Windows) не прошли бы проверку.
    presets = [
        (
            fmt(key, live_settings.parse(key, raw) if field.kind in ("int", "float") else raw, html=False),
            live_settings.dump(key, value) == raw,
        )
        for raw in ui.presets
    ]
    markup = kb_field(key, presets, custom=ui.custom and field.kind != "choice", reset_label=_reset_label(key), back=ui.parent)
    return text, markup


async def _screen_for(ctx: Ctx, target: str):
    """Экран по callback: st:go:<экран> или st:f:<настройка>."""
    if target.startswith("st:f:"):
        return view_field(target[len("st:f:") :])
    return await SCREENS[target[len("st:go:") :]](ctx)


async def _redraw(query: CallbackQuery, ctx: Ctx, target: str) -> None:
    text, markup = await _screen_for(ctx, target)
    await _show(query, text, markup)


def _ctx(bot: Bot, session_factory, settings) -> Ctx:
    return Ctx(bot=bot, session_factory=session_factory, settings=settings)


def _ctx_now(bot: Bot, session_factory) -> Ctx:
    """После изменения настроек: settings из middleware уже устарели."""
    return Ctx(bot=bot, session_factory=session_factory, settings=live_settings.current())


# ── Навигация ───────────────────────────────────────────────────────────


@router.callback_query(F.data.startswith("st:go:"))
async def open_screen(query: CallbackQuery, state: FSMContext, bot: Bot, session_factory, settings):
    name = (query.data or "")[len("st:go:") :]
    if name not in SCREENS:
        await query.answer(tr("settings", "stale"), show_alert=True)
        return
    await state.clear()
    await _redraw(query, _ctx(bot, session_factory, settings), f"st:go:{name}")
    await query.answer()


@router.callback_query(F.data.startswith("st:f:"))
async def open_field(query: CallbackQuery, bot: Bot, session_factory, settings):
    key = (query.data or "")[len("st:f:") :]
    if key not in FIELD_UI:
        await query.answer(tr("settings", "stale"), show_alert=True)
        return
    await _redraw(query, _ctx(bot, session_factory, settings), f"st:f:{key}")
    await query.answer()


# ── Изменение значений ──────────────────────────────────────────────────


@router.callback_query(F.data.startswith("st:t:"))
async def toggle(query: CallbackQuery, bot: Bot, session_factory, settings):
    key = (query.data or "")[len("st:t:") :]
    parent = TOGGLE_PARENT.get(key)
    if parent is None:
        await query.answer(tr("settings", "stale"), show_alert=True)
        return
    field = live_settings.FIELDS[key]
    current = live_settings.value(key)
    if field.kind == "bool":
        new = not current
    else:
        choices = field.choices
        new = choices[(choices.index(current) + 1) % len(choices)]
    if key == "ai.enabled" and new and peek_ai_service() is None:
        await query.answer(tr("settings", "home_ai_none"), show_alert=True)
        return
    await live_settings.set_value(session_factory, key, live_settings.dump(key, new))
    await _redraw(query, _ctx_now(bot, session_factory), parent)
    if key == "ai.enabled":
        toast = tr("settings", "ai_toast_on" if new else "ai_toast_off")
    elif key == "ai.mode":
        toast = tr("settings", "ai_toast_mode", mode=tr("settings", f"mode_{new}"))
    elif key == "ai.media_enabled":
        toast = tr("settings", "ai_toast_media_on" if new else "ai_toast_media_off")
    else:
        toast = tr("settings", "rw_toast_on" if new else "rw_toast_off")
    await query.answer(toast)


@router.callback_query(F.data.startswith("st:p:"))
async def pick_preset(query: CallbackQuery, bot: Bot, session_factory):
    key, _, idx = (query.data or "")[len("st:p:") :].rpartition(":")
    ui = FIELD_UI.get(key)
    if ui is None or not idx.isdigit() or int(idx) >= len(ui.presets):
        await query.answer(tr("settings", "stale"), show_alert=True)
        return
    await live_settings.set_value(session_factory, key, ui.presets[int(idx)])
    await _redraw(query, _ctx_now(bot, session_factory), f"st:f:{key}")
    await query.answer(tr("settings", "saved"))


@router.callback_query(F.data.startswith("st:r:"))
async def reset_value(query: CallbackQuery, bot: Bot, session_factory):
    key = (query.data or "")[len("st:r:") :]
    if key not in live_settings.FIELDS:
        await query.answer(tr("settings", "stale"), show_alert=True)
        return
    await live_settings.reset(session_factory, key)
    ctx = _ctx_now(bot, session_factory)
    slot = slot_of(key)
    if slot is not None:
        text, markup = await view_icons(ctx, slot)
        await _show(query, text, markup)
    else:
        await _redraw(query, ctx, f"st:f:{key}" if key in FIELD_UI else TOGGLE_PARENT.get(key, "st:go:home"))
    await query.answer(tr("settings", "reset_done"))


@router.callback_query(F.data.startswith("st:e:"))
async def ask_custom(query: CallbackQuery, state: FSMContext):
    key = (query.data or "")[len("st:e:") :]
    if key not in FIELD_UI:
        await query.answer(tr("settings", "stale"), show_alert=True)
        return
    field = live_settings.FIELDS[key]
    if field.kind in ("int", "float"):
        limits = tr("settings", "ask_limits_number", lo=f"{field.lo:g}", hi=f"{field.hi:g}")
    else:
        limits = tr("settings", "ask_limits_text", n=field.max_len)
    await state.set_state(SettingsFlow.value)
    await state.update_data(key=key)
    await query.answer()
    if query.message:
        await query.message.answer(
            tr("settings", "ask_value", title=tr("settings", f"f_{_slug(key)}_title"), limits=limits),
            reply_markup=kb_cancel(),
        )


def _is_input(message: Message) -> bool:
    """Текст для ввода. Команды и кнопка «Админ-панель» — не значение: их обработают другие роутеры."""
    text = message.text or ""
    return bool(text) and not text.startswith("/") and text.strip() != tr("admin", "btn_admin_panel")


@router.message(StateFilter(SettingsFlow.value), _is_input)
async def receive_custom(message: Message, state: FSMContext, session_factory):
    key = (await state.get_data()).get("key")
    if key not in FIELD_UI:
        await state.clear()
        return
    try:
        await live_settings.set_value(session_factory, key, message.text or "")
    except ValueError as e:
        await message.answer(tr("settings", "bad_value", error=html_escape(str(e))), reply_markup=kb_cancel())
        return
    await state.clear()
    text, markup = view_field(key)
    await message.answer(tr("settings", "saved"))
    await message.answer(text, reply_markup=markup)


@router.callback_query(F.data == "st:x")
async def cancel_input(query: CallbackQuery, state: FSMContext):
    await state.clear()
    if query.message:
        try:
            await query.message.edit_text(tr("settings", "cancelled"), reply_markup=None)
        except TelegramBadRequest:
            pass
    await query.answer()


# ── Модели ИИ ───────────────────────────────────────────────────────────


@router.callback_query(F.data.startswith("st:mm:"))
async def models_make_main(query: CallbackQuery, session_factory):
    chain = _ai_chain()
    idx = (query.data or "")[len("st:mm:") :]
    if chain is None or not idx.isdigit() or int(idx) >= len(chain.models):
        await query.answer(tr("settings", "stale"), show_alert=True)
        return
    i = int(idx)
    models = chain.models
    spec = models[i]
    if i > 0:
        try:
            await model_choice.save(session_factory, chain, (spec, *models[:i], *models[i + 1 :]))
        except ValueError as e:
            await query.answer(str(e), show_alert=True)
            return
        await _show(query, *_models_view(chain))
    await query.answer(tr("settings", "models_main_set", spec=spec))


@router.callback_query(F.data == "st:mr")
async def models_reset(query: CallbackQuery, session_factory):
    chain = _ai_chain()
    if chain is None:
        await query.answer(tr("settings", "stale"), show_alert=True)
        return
    await model_choice.reset(session_factory, chain)
    await _show(query, *_models_view(chain))
    await query.answer(tr("settings", "reset_done"))


@router.callback_query(F.data == "st:me")
async def models_edit_start(query: CallbackQuery, state: FSMContext):
    chain = _ai_chain()
    if chain is None:
        await query.answer(tr("settings", "stale"), show_alert=True)
        return
    await state.set_state(SettingsFlow.models)
    await query.answer()
    if query.message:
        await query.message.answer(
            tr("settings", "models_ask", current=html_escape(", ".join(chain.models))),
            reply_markup=kb_cancel(),
        )


@router.message(StateFilter(SettingsFlow.models), _is_input)
async def models_edit_receive(message: Message, state: FSMContext, session_factory, settings):
    chain = _ai_chain()
    if chain is None:
        await state.clear()
        return
    try:
        models = parse_ai_models(message.text or "", settings.ai.provider)
        await model_choice.save(session_factory, chain, models)
    except ValueError as e:
        await message.answer(tr("settings", "bad_value", error=html_escape(str(e))), reply_markup=kb_cancel())
        return
    await state.clear()
    text, markup = _models_view(chain)
    await message.answer(tr("settings", "saved"))
    await message.answer(text, reply_markup=markup)


# ── Проверка ИИ ─────────────────────────────────────────────────────────

# Сколько ждать каждую модель при проверке: дольше админ ждать не станет.
_CHECK_TIMEOUT_SEC = 30
_check_lock = asyncio.Lock()


def _proxy_label(url: str) -> str:
    """Прокси без логина и пароля: http://host:port."""
    if not url:
        return tr("settings", "check_proxy_off")
    try:
        parts = urlsplit(url)
        shown = f"{parts.scheme}://{parts.hostname}" + (f":{parts.port}" if parts.port else "")
    except ValueError:
        shown = "…"
    return tr("settings", "check_proxy_on", proxy=html_escape(shown))


def _check_view(chain: ModelChain, results: list[ModelCheck], ai_enabled: bool):
    lines: list[str] = []
    for idx, r in enumerate(results, start=1):
        sec = f"{r.seconds:.1f}"
        if r.status == "ok":
            status = tr("settings", "check_ok", sec=sec)
        elif r.status == "timeout":
            status = tr("settings", "check_timeout", sec=_CHECK_TIMEOUT_SEC)
        elif r.status in ("no_key", "limit", "unavailable"):
            status = tr("settings", f"check_{r.status}")
        else:
            status = tr("settings", "check_failed")
        lines.append(
            tr(
                "settings",
                "check_line",
                idx=idx,
                mark=f"{tr('settings', 'models_main_mark')} " if idx == 1 else "",
                spec=html_escape(r.spec),
                status=status,
            )
        )
        if r.detail:
            key = "check_reply" if r.status == "ok" else "check_error"
            lines.append(tr("settings", key, text=html_escape(r.detail[:200])))

    answering = [r for r in results if r.status == "ok"]
    if answering and answering[0].spec == chain.models[0]:
        verdict = tr("settings", "check_verdict_main")
    elif answering:
        verdict = tr("settings", "check_verdict_fallback", spec=html_escape(answering[0].spec))
    else:
        verdict = tr("settings", "check_verdict_none")
    hints = []
    if not chain.proxy_url and any(r.status in ("error", "timeout") for r in results):
        hints.append(tr("settings", "check_hint_proxy"))
    if any(r.status == "error" and re.search(r"\b40[13]\b", r.detail) for r in results):
        hints.append(tr("settings", "check_hint_key"))
    if hints:
        verdict = "\n\n".join([verdict, *hints])

    text = tr(
        "settings",
        "check",
        proxy=_proxy_label(chain.proxy_url),
        state=tr("settings", "check_ai_on" if ai_enabled else "check_ai_off"),
        lines="\n".join(lines),
        verdict=verdict,
    )
    markup = kb([[btn(tr("settings", "check_btn_again"), "st:ck")], back_row("st:go:ai")])
    return text, markup


@router.callback_query(F.data == "st:ck")
async def check_ai(query: CallbackQuery, settings):
    chain = _ai_chain()
    if chain is None or query.message is None:
        await query.answer(tr("settings", "home_ai_none"), show_alert=True)
        return
    if _check_lock.locked():
        await query.answer(tr("settings", "check_busy"))
        return
    async with _check_lock:
        # Ответ на нажатие — сразу: Telegram ждёт его недолго, а модели думают дольше.
        await query.answer()
        progress = await query.message.answer(tr("settings", "check_running", sec=_CHECK_TIMEOUT_SEC))
        results = await check_models(chain, _CHECK_TIMEOUT_SEC)
        text, markup = _check_view(chain, results, settings.ai.enabled)
        try:
            await progress.edit_text(text, reply_markup=markup)
        except TelegramBadRequest:
            await query.message.answer(text, reply_markup=markup)


# ── Значки тем ──────────────────────────────────────────────────────────


@router.callback_query(F.data.startswith("st:ic:"))
async def icons_open(query: CallbackQuery, bot: Bot, session_factory, settings):
    slot = (query.data or "")[len("st:ic:") :]
    if slot not in ("o", "c", "w"):
        await query.answer(tr("settings", "stale"), show_alert=True)
        return
    text, markup = await view_icons(_ctx(bot, session_factory, settings), slot)
    await _show(query, text, markup)
    await query.answer()


@router.callback_query(F.data.startswith("st:is:"))
async def icons_set(query: CallbackQuery, bot: Bot, session_factory):
    slot, _, icon_id = (query.data or "")[len("st:is:") :].partition(":")
    if slot not in ("o", "c", "w") or not (icon_id == "-" or icon_id.isdigit()):
        await query.answer(tr("settings", "stale"), show_alert=True)
        return
    key = slot_key(slot)
    await live_settings.set_value(session_factory, key, "" if icon_id == "-" else icon_id)
    ctx = _ctx_now(bot, session_factory)
    text, markup = await view_tickets(ctx)
    await _show(query, text, markup)
    name = key.removeprefix("topic_icon_emoji_")
    await query.answer(
        tr(
            "settings",
            "icons_toast",
            slot=tr("settings", f"icon_slot_{name}"),
            icon=await _icon_label(bot, live_settings.value(key)),
        )
    )


# ── Тексты для клиентов ─────────────────────────────────────────────────


def _text_index(raw: str) -> int | None:
    return int(raw) if raw.isdigit() and int(raw) < len(live_settings.TEXTS) else None


@router.callback_query(F.data.startswith("st:tv:"))
async def text_open(query: CallbackQuery, state: FSMContext):
    i = _text_index((query.data or "")[len("st:tv:") :])
    if i is None:
        await query.answer(tr("settings", "stale"), show_alert=True)
        return
    await state.clear()
    await _show(query, *_text_view(i))
    await query.answer()


async def _ask_text(message: Message, state: FSMContext, i: int) -> None:
    stem, key = live_settings.TEXTS[i]
    await state.set_state(SettingsFlow.text)
    await state.update_data(text_idx=i, pending=None)
    await message.answer(
        tr("settings", "text_ask", title=tr("settings", f"t_{stem}_{key}_title")),
        reply_markup=kb_cancel(),
    )


@router.callback_query(F.data.startswith("st:te:"))
async def text_edit_start(query: CallbackQuery, state: FSMContext):
    i = _text_index((query.data or "")[len("st:te:") :])
    if i is None or query.message is None:
        await query.answer(tr("settings", "stale"), show_alert=True)
        return
    await query.answer()
    await _ask_text(query.message, state, i)


@router.callback_query(F.data == "st:tw")
async def text_edit_retry(query: CallbackQuery, state: FSMContext):
    i = (await state.get_data()).get("text_idx")
    if i is None or query.message is None:
        await query.answer(tr("settings", "stale"), show_alert=True)
        return
    await query.answer()
    try:
        await query.message.edit_reply_markup(reply_markup=None)
    except TelegramBadRequest:
        pass
    await _ask_text(query.message, state, i)


@router.message(StateFilter(SettingsFlow.text), _is_input)
async def text_receive(message: Message, state: FSMContext, bot: Bot):
    i = (await state.get_data()).get("text_idx")
    if i is None:
        await state.clear()
        return
    stem, key = live_settings.TEXTS[i]
    html = message.html_text.strip()
    if len(html) > live_settings.TEXT_MAX_LEN:
        await message.answer(
            tr("settings", "text_too_long", n=len(html), max=live_settings.TEXT_MAX_LEN),
            reply_markup=kb_cancel(),
        )
        return
    await message.answer(tr("settings", "text_preview_header", title=tr("settings", f"t_{stem}_{key}_title")))
    try:
        # Превью и есть проверка: если Telegram не принял разметку — клиенту её не отправить.
        await bot.send_message(message.chat.id, html, reply_markup=kb_text_preview())
    except TelegramBadRequest as e:
        await message.answer(tr("settings", "text_bad_html", error=html_escape(str(e))), reply_markup=kb_cancel())
        return
    await state.update_data(pending=html)


@router.callback_query(F.data == "st:ts")
async def text_save(query: CallbackQuery, state: FSMContext, session_factory):
    data = await state.get_data()
    i, pending = data.get("text_idx"), data.get("pending")
    if i is None or not pending:
        await query.answer(tr("settings", "stale"), show_alert=True)
        return
    stem, key = live_settings.TEXTS[i]
    tkey = live_settings.text_key(stem, key)
    # Совпал со стандартным — правка не нужна, просто возвращаем стандартный.
    if pending == tr_default(stem, key):
        await live_settings.reset(session_factory, tkey)
    else:
        await live_settings.set_value(session_factory, tkey, pending)
    await state.clear()
    if query.message:
        try:
            await query.message.edit_reply_markup(reply_markup=None)
        except TelegramBadRequest:
            pass
        text, markup = _text_view(i)
        await query.message.answer(text, reply_markup=markup)
    await query.answer(tr("settings", "text_saved"))


@router.callback_query(F.data.startswith("st:tr:"))
async def text_reset(query: CallbackQuery, session_factory):
    i = _text_index((query.data or "")[len("st:tr:") :])
    if i is None:
        await query.answer(tr("settings", "stale"), show_alert=True)
        return
    stem, key = live_settings.TEXTS[i]
    await live_settings.reset(session_factory, live_settings.text_key(stem, key))
    await _show(query, *_text_view(i))
    await query.answer(tr("settings", "text_reset_done"))


# ── Интеграции и система ────────────────────────────────────────────────


@router.callback_query(F.data == "st:rwc")
async def remnawave_check(query: CallbackQuery, settings):
    rw = settings.remnawave
    if not rw.is_configured:
        await query.answer(tr("settings", "rw_check_disabled"), show_alert=True)
        return
    try:
        await RemnawaveClient(rw).probe()
    except Exception as e:
        await query.answer(tr("settings", "rw_check_fail", error=str(e)[:150]), show_alert=True)
        return
    await query.answer(tr("settings", "rw_check_ok"), show_alert=True)


@router.callback_query(F.data == "st:ra")
async def reset_all_ask(query: CallbackQuery):
    await _show(query, tr("settings", "reset_all_ask"), kb_reset_all())
    await query.answer()


@router.callback_query(F.data == "st:ray")
async def reset_all_do(query: CallbackQuery, bot: Bot, session_factory):
    await live_settings.reset_all(session_factory)
    chain = _ai_chain()
    if chain is not None and model_choice.is_overridden(chain):
        await model_choice.reset(session_factory, chain)
    await _redraw(query, _ctx_now(bot, session_factory), "st:go:system")
    await query.answer(tr("settings", "reset_all_done"), show_alert=True)


# ── Ввод не того типа ───────────────────────────────────────────────────


@router.message(StateFilter(SettingsFlow), ~F.text)
async def expect_text(message: Message):
    await message.answer(tr("settings", "expect_text"), reply_markup=kb_cancel())
