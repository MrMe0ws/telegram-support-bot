"""Показ статей раздела «Помощь»: HTML от админа или plain text с авто-ссылками."""

from __future__ import annotations

import re
from html import escape

# http(s), tg:// — один токен; дальше не пробел и не угловые скобки разметки
_URL_START = re.compile(r"(?<![\w/])((?:https?://|tg://)[^\s<>]+)", re.IGNORECASE)

# Узнаваемые теги Telegram HTML — тогда не трогаем разметку (доверяем админу)
_HTML_HINT = re.compile(
    r"<\s*/?\s*(a|b|strong|i|em|u|ins|s|strike|del|code|pre|blockquote)\b",
    re.IGNORECASE,
)


def _trim_trailing_junk(url: str) -> str:
    """Убираем хвостовую пунктуацию и закрывающую скобку у (https://…)."""
    u = url.rstrip()
    while u:
        last = u[-1]
        if last in ".,;:!?）":
            u = u[:-1]
            continue
        if last in ")]}>」":
            u = u[:-1]
            continue
        break
    return u


def linkify_plain_help_body(text: str) -> str:
    """
    Обычный текст → экранирование + кликабельные URL.
    Если в тексте уже есть HTML-теги (как в подсказке админки) — возвращаем как есть.
    """
    if _HTML_HINT.search(text):
        return text

    parts: list[str] = []
    pos = 0
    for m in _URL_START.finditer(text):
        parts.append(escape(text[pos : m.start()]))
        raw = m.group(1)
        url = _trim_trailing_junk(raw)
        if not url:
            continue
        href = escape(url, quote=True)
        parts.append(f'<a href="{href}">{escape(url)}</a>')
        pos = m.start() + len(raw)
    parts.append(escape(text[pos:]))
    return "".join(parts)
