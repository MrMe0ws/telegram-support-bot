"""
Настройки, которые админ меняет в боте (админ-панель → «⚙️ Настройки»).

Значения из .env — по умолчанию; изменённые в боте хранятся в таблице
bot_settings и важнее .env. Меняются на ходу, без перезапуска: обработчики
получают текущие настройки через middleware (см. main.py), а сервисы,
которые держат настройки у себя, подписываются на изменения.

Тексты для клиентов хранятся там же с ключом «text:<файл>.<ключ>» и
перекрывают строки из texts/<locale>/*.yml.
"""

from __future__ import annotations

import dataclasses
import logging
import os
from collections.abc import Callable
from dataclasses import dataclass
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import select

from app import i18n
from app.config import AI_MODES, Settings
from app.db.models import BotSetting
from app.services import bot_settings

log = logging.getLogger(__name__)

LOG_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR")

# Плейсхолдеры шаблона имени темы (как в private_chat / cabinet_bridge).
_TOPIC_SAMPLE = {"username": "@client", "ticket_id": 42, "uid": 123456789, "name": "Иван"}
# Telegram: название темы — до 128 символов.
_TOPIC_NAME_MAX = 128


@dataclass(frozen=True)
class Field:
    """
    Одна настройка. key — путь в Settings: «ai.mode» → Settings.ai.mode,
    «topic_name_template» → Settings.topic_name_template. log_level — не в Settings.
    """

    key: str
    kind: str  # bool | int | float | choice | str
    lo: float = 0
    hi: float = 0
    choices: tuple[str, ...] = ()
    max_len: int = 0


FIELDS: dict[str, Field] = {
    f.key: f
    for f in (
        Field("ai.enabled", "bool"),
        Field("ai.mode", "choice", choices=AI_MODES),
        Field("ai.reply_delay_sec", "float", 0, 120),
        Field("ai.max_replies_per_ticket", "int", 1, 1000),
        Field("ai.history_limit", "int", 2, 200),
        Field("ai.media_enabled", "bool"),
        Field("ai.media_max_mb", "int", 1, 20),
        Field("ai.human_button_from", "int", 0, 1000),
        Field("ai.assistant_name", "str", max_len=64),
        Field("topic_name_template", "str", max_len=_TOPIC_NAME_MAX),
        Field("topic_icon_emoji_open", "str", max_len=32),
        Field("topic_icon_emoji_closed", "str", max_len=32),
        Field("topic_icon_emoji_waiting", "str", max_len=32),
        Field("remnawave.enabled", "bool"),
        Field("remnawave.timezone", "str", max_len=64),
        Field("log_level", "choice", choices=LOG_LEVELS),
    )
}

# Тексты для клиентов, которые можно переписать в боте: (файл, ключ) в texts/<locale>.
TEXTS: tuple[tuple[str, str], ...] = (
    ("user", "start"),
    ("user", "create_ticket_prompt"),
    ("user", "ticket_accepted"),
    ("flow", "notify_user_default"),
    ("user", "blocked"),
)
TEXT_PREFIX = "text:"
TEXT_MAX_LEN = 3500


def text_key(stem: str, key: str) -> str:
    return f"{TEXT_PREFIX}{stem}.{key}"


# ── Разбор и проверка значений ──────────────────────────────────────────


def _parse_number(raw: str, field: Field) -> float:
    try:
        v = float(raw.strip().replace(",", "."))
    except ValueError:
        raise ValueError("нужно число") from None
    if not field.lo <= v <= field.hi:
        raise ValueError(f"допустимо от {field.lo:g} до {field.hi:g}")
    if field.kind == "int" and v != int(v):
        raise ValueError("нужно целое число")
    return v


def parse(key: str, raw: str):
    """Строку (из БД или от админа) → значение настройки. ValueError — не годится."""
    field = FIELDS[key]
    if field.kind == "bool":
        s = raw.strip().lower()
        if s not in ("true", "false"):
            raise ValueError("нужно true или false")
        return s == "true"
    if field.kind in ("int", "float"):
        v = _parse_number(raw, field)
        return int(v) if field.kind == "int" else v
    if field.kind == "choice":
        s = raw.strip()
        if s not in field.choices:
            raise ValueError(f"допустимо: {', '.join(field.choices)}")
        return s
    s = raw.strip()
    if field.max_len and len(s) > field.max_len:
        raise ValueError(f"не длиннее {field.max_len} символов")
    if key == "ai.assistant_name" and not s:
        raise ValueError("имя не может быть пустым")
    if key == "topic_name_template":
        _check_topic_template(s)
    if key.startswith("topic_icon_emoji_") and s and not s.isdigit():
        raise ValueError("нужен ID значка темы")
    if key == "remnawave.timezone":
        _check_timezone(s)
    return s


def dump(key: str, value) -> str:
    """Значение настройки → строка для БД."""
    if FIELDS[key].kind == "bool":
        return "true" if value else "false"
    if isinstance(value, float) and value == int(value):
        return str(int(value))
    return str(value)


def render_topic_name(template: str) -> str:
    return template.format(**_TOPIC_SAMPLE)[: _TOPIC_NAME_MAX - 1]


def _check_topic_template(template: str) -> None:
    if not template:
        raise ValueError("шаблон не может быть пустым")
    try:
        name = render_topic_name(template)
    except KeyError as e:
        raise ValueError(f"неизвестный плейсхолдер {{{e.args[0]}}}") from None
    except (ValueError, IndexError):
        raise ValueError("непарные фигурные скобки { }") from None
    if not name.strip():
        raise ValueError("название темы получается пустым")


def _check_timezone(name: str) -> None:
    try:
        ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        raise ValueError(f"часовой пояс {name!r} не найден (пример: Europe/Moscow)") from None


# ── Текущие настройки ───────────────────────────────────────────────────

_base: Settings | None = None
_current: Settings | None = None
# Изменённые в боте значения: ключ FIELDS → значение, ключ текста → строка.
_overrides: dict[str, object] = {}
_listeners: list[Callable[[Settings], None]] = []


def current(fallback: Settings | None = None) -> Settings:
    """Настройки с учётом изменений в боте. До init() — fallback (тесты, утилиты)."""
    if _current is not None:
        return _current
    if fallback is None:
        raise RuntimeError("live_settings.init() не вызван")
    return fallback


def base() -> Settings:
    """Настройки только из .env."""
    if _base is None:
        raise RuntimeError("live_settings.init() не вызван")
    return _base


def value(key: str):
    """Текущее значение настройки."""
    if key == "log_level":
        return _overrides.get(key, _env_log_level())
    return _get_path(current(), key)


def env_value(key: str):
    if key == "log_level":
        return _env_log_level()
    return _get_path(base(), key)


def is_overridden(key: str) -> bool:
    return key in _overrides


def subscribe(listener: Callable[[Settings], None]) -> None:
    """Вызывать listener с новыми настройками после каждого изменения."""
    _listeners.append(listener)


def _get_path(settings: Settings, key: str):
    obj: object = settings
    for part in key.split("."):
        obj = getattr(obj, part)
    return obj


def _env_log_level() -> str:
    raw = os.environ.get("LOG_LEVEL", "WARNING").strip().upper()
    return raw if isinstance(getattr(logging, raw, None), int) else "WARNING"


def _apply(settings: Settings, overrides: dict[str, object]) -> Settings:
    top: dict[str, object] = {}
    nested: dict[str, dict[str, object]] = {}
    for key, v in overrides.items():
        if key == "log_level" or key.startswith(TEXT_PREFIX):
            continue
        head, _, tail = key.partition(".")
        if tail:
            nested.setdefault(head, {})[tail] = v
        else:
            top[head] = v
    for head, changes in nested.items():
        top[head] = dataclasses.replace(getattr(settings, head), **changes)
    return dataclasses.replace(settings, **top) if top else settings


def _apply_log_level(level_name: str) -> None:
    level = getattr(logging, level_name, logging.WARNING)
    logging.getLogger().setLevel(level)
    # Строки aiogram «Update … is handled» — только при DEBUG (как в main._configure_logging).
    logging.getLogger("aiogram.event").setLevel(logging.DEBUG if level <= logging.DEBUG else logging.WARNING)


def _recompute() -> None:
    global _current
    _current = _apply(base(), _overrides)
    i18n.set_text_overrides(
        {
            tuple(k[len(TEXT_PREFIX) :].split(".", 1)): v  # type: ignore[misc]
            for k, v in _overrides.items()
            if k.startswith(TEXT_PREFIX)
        }
    )
    _apply_log_level(str(value("log_level")))
    for listener in _listeners:
        try:
            listener(_current)
        except Exception:
            log.exception("Настройки: подписчик не принял изменения")


async def init(session_factory, settings: Settings) -> Settings:
    """Загрузить изменения из БД поверх настроек из .env. Возвращает итоговые настройки."""
    global _base
    _base = settings
    _overrides.clear()
    _listeners.clear()
    text_keys = {text_key(stem, key) for stem, key in TEXTS}
    async with session_factory() as session:
        rows = (await session.execute(select(BotSetting))).scalars().all()
    for row in rows:
        if row.key in FIELDS:
            try:
                _overrides[row.key] = parse(row.key, row.value)
            except ValueError as e:
                log.warning("Настройка %s=%r из бота пропущена: %s", row.key, row.value, e)
        elif row.key in text_keys:
            _overrides[row.key] = row.value
    _recompute()
    if _overrides:
        log.warning("Настройки, изменённые в боте: %s", ", ".join(sorted(_overrides)))
    return current()


async def set_value(session_factory, key: str, raw: str) -> None:
    """Сохранить настройку (строкой, как её прислал админ). ValueError — не годится."""
    if key.startswith(TEXT_PREFIX):
        if key not in {text_key(s, k) for s, k in TEXTS}:
            raise ValueError(f"неизвестный текст {key!r}")
        v: object = raw
        stored = raw
    else:
        v = parse(key, raw)
        stored = dump(key, v)
        # Совпало с .env — правка не нужна: «✏️» только у того, что отличается от .env.
        if v == env_value(key):
            await reset(session_factory, key)
            return
    async with session_factory() as session:
        async with session.begin():
            await bot_settings.set_value(session, key, stored)
    _overrides[key] = v
    _recompute()


async def reset(session_factory, key: str) -> None:
    """Вернуть значение из .env (для текста — из YAML)."""
    async with session_factory() as session:
        async with session.begin():
            await bot_settings.delete_value(session, key)
    _overrides.pop(key, None)
    _recompute()


async def reset_all(session_factory) -> None:
    """Вернуть все настройки к .env, а тексты — к стандартным."""
    keys = list(FIELDS) + [text_key(stem, key) for stem, key in TEXTS]
    async with session_factory() as session:
        async with session.begin():
            for key in keys:
                await bot_settings.delete_value(session, key)
    _overrides.clear()
    _recompute()


def overridden_keys() -> list[str]:
    return sorted(_overrides)
