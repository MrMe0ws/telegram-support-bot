from __future__ import annotations

from datetime import datetime
from html import escape
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from app.config import RemnawaveSettings
from app.services.remnawave_client import RemnawaveUser

_GIB = 1024**3
_DASH = "—"


def _zone(tz_name: str) -> ZoneInfo:
    try:
        return ZoneInfo(tz_name)
    except ZoneInfoNotFoundError:
        return ZoneInfo("UTC")


def _format_dt(raw: str | None, tz_name: str) -> str:
    if not raw:
        return _DASH
    s = raw.strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return escape(raw)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=ZoneInfo("UTC"))
    local = dt.astimezone(_zone(tz_name))
    return escape(local.strftime("%d.%m.%Y %H:%M"))


def _format_gb(bytes_val: int | None) -> str:
    if bytes_val is None:
        return _DASH
    return f"{bytes_val / _GIB:.2f}"


def _format_status(status: str | None) -> str:
    if not status:
        return _DASH
    return escape(str(status).upper())


def _code(value: str | None) -> str:
    if value is None or value == "":
        return _DASH
    return f"<code>{escape(str(value))}</code>"


def _line(label: str, value_html: str) -> str:
    return f"{label}: {value_html}"


def format_remnawave_profile_block(
    user: RemnawaveUser,
    *,
    telegram_username_line: str,
    telegram_id: int,
    settings: RemnawaveSettings,
) -> str:
    """HTML-блок подписки Remnawave для /profile (ParseMode.HTML)."""
    tz = settings.timezone
    used = _format_gb(user.used_traffic_bytes)
    limit = _format_gb(user.traffic_limit_bytes)
    traffic = f"{used} / {limit} ГБ" if used != _DASH or limit != _DASH else _DASH

    lines = [
        f"👤 {telegram_username_line}",
        _line("🆔 TG ID", _code(str(telegram_id))),
        _line("⚡️ Статус", _format_status(user.status)),
        "",
        _line("📆 До", _format_dt(user.expire_at, tz)),
        _line("📊 Трафик", escape(traffic) if traffic != _DASH else _DASH),
        _line(
            "♻️ Сброс лимита",
            escape(str(user.traffic_limit_strategy)) if user.traffic_limit_strategy else _DASH,
        ),
        _line("🔄 Последний сброс", _format_dt(user.last_traffic_reset_at, tz)),
        "",
        _line(
            "📲 Лимит устройств",
            _code(str(user.hwid_device_limit))
            if user.hwid_device_limit is not None
            else _DASH,
        ),
        "",
        _line("🔑 Логин в панели", _code(user.username)),
        _line(
            "📝 Описание",
            _code(user.description) if user.description else _DASH,
        ),
        _line("🏷 Тег", _code(user.tag) if user.tag else _DASH),
        _line(
            "🛡 Внутр. сквады",
            escape(user.internal_squads) if user.internal_squads else _DASH,
        ),
        _line("🟢 Активность", _format_dt(user.last_activity_at, tz)),
        "",
        "🔗 Ссылка подписки:",
    ]
    if user.subscription_url:
        lines.append(_code(user.subscription_url))
    else:
        lines.append(_DASH)
    return "\n".join(lines)
