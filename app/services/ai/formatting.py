"""
Оформление ответов ИИ под канал доставки.

Модель пишет в небольшом подмножестве Markdown (см. FORMAT_RULES): так ей
привычнее всего, и одним текстом можно обслужить оба канала.

    Telegram          → HTML (parse_mode=HTML): жирный, курсив, код, ссылки.
    Чат web-кабинета  → простой текст: кабинет показывает сообщение как есть,
                        без разметки, поэтому жирный превращается в обычный
                        текст, а ссылка — в «текст (адрес)».

Разметка вне списка (заголовки, таблицы) не поддерживается каналами — её
переводим в обычные строки, а не показываем клиенту символы «#» и «|».
"""

from __future__ import annotations

import re
from html import escape


# Что модели разрешено использовать — подставляется в системный запрос.
# Описывает возможности рендера, а не вкус: тон и стиль задаются в базе знаний.
FORMAT_RULES = """Оформление (бот сам переведёт его в формат Telegram или чата сайта):
• **жирный** — для самого важного, изредка;
• списки — строками, начинающимися с «• » или «1. »; без вложенных списков;
• ссылки — [текст](https://адрес);
• `код` — только для ключей, адресов и команд, которые клиент будет копировать.
Не используй заголовки (#), таблицы, курсив и цитаты — в чате они не отображаются."""

_TOKEN = "\x00{}\x00"
_TOKEN_RE = re.compile("\x00(\\d+)\x00")

_CODE = re.compile(r"`([^`\n]+)`")
_LINK = re.compile(r"\[([^\]\n]+)\]\((https?://[^)\s]+)\)")
_BOLD = re.compile(r"\*\*(.+?)\*\*|__(.+?)__", re.S)
_ITALIC = re.compile(r"(?<![\w*])\*(?!\s)([^*\n]+?)(?<!\s)\*(?![\w*])")
_STRIKE = re.compile(r"~~(.+?)~~")
_HEADING = re.compile(r"^[ \t]*#{1,6}[ \t]+(.+?)[ \t]*#*[ \t]*$", re.M)
_BULLET = re.compile(r"^[ \t]*[*\-+•][ \t]+", re.M)
_NUMBERED = re.compile(r"^[ \t]*(\d+)[.)][ \t]+", re.M)
_QUOTE = re.compile(r"^[ \t]*>[ \t]?", re.M)
_TABLE_RULE = re.compile(r"^[ \t]*\|?[ \t]*:?-{3,}:?[ \t]*(\|[ \t]*:?-{3,}:?[ \t]*)*\|?[ \t]*(\n|$)", re.M)
_TABLE_ROW = re.compile(r"^[ \t]*\|(.+)\|[ \t]*$", re.M)
_BLANK_LINES = re.compile(r"\n{3,}")


def _normalize_lines(text: str) -> str:
    """Общая для обоих каналов чистка структуры: списки, заголовки, таблицы, пустые строки."""
    text = text.replace("\r\n", "\n").strip()
    text = _TABLE_RULE.sub("", text)
    text = _TABLE_ROW.sub(lambda m: " · ".join(c.strip() for c in m.group(1).split("|")), text)
    text = _QUOTE.sub("", text)
    text = _BULLET.sub("• ", text)
    text = _NUMBERED.sub(lambda m: f"{m.group(1)}. ", text)
    return _BLANK_LINES.sub("\n\n", text).strip()


def _protect(text: str, pattern: re.Pattern, render, store: list[str]) -> str:
    """Выносит фрагмент в заглушку, чтобы правила жирного и курсива не задели код и адреса."""

    def repl(m: re.Match) -> str:
        store.append(render(m))
        return _TOKEN.format(len(store) - 1)

    return pattern.sub(repl, text)


def _restore(text: str, store: list[str]) -> str:
    return _TOKEN_RE.sub(lambda m: store[int(m.group(1))], text)


def to_telegram_html(markdown: str) -> str:
    """Ответ модели → HTML для parse_mode=HTML в Telegram."""
    store: list[str] = []
    text = _normalize_lines(markdown)
    text = _protect(text, _CODE, lambda m: f"<code>{escape(m.group(1))}</code>", store)
    text = _protect(
        text,
        _LINK,
        lambda m: f'<a href="{escape(m.group(2), quote=True)}">{escape(m.group(1))}</a>',
        store,
    )
    text = escape(text, quote=False)
    text = _HEADING.sub(r"<b>\1</b>", text)
    text = _BOLD.sub(lambda m: f"<b>{m.group(1) or m.group(2)}</b>", text)
    text = _STRIKE.sub(r"<s>\1</s>", text)
    text = _ITALIC.sub(r"<i>\1</i>", text)
    return _restore(text, store)


def to_plain_text(markdown: str) -> str:
    """Ответ модели → простой текст для чата web-кабинета (разметку он не показывает)."""
    store: list[str] = []
    text = _normalize_lines(markdown)
    text = _protect(text, _CODE, lambda m: m.group(1), store)

    def link(m: re.Match) -> str:
        label, url = m.group(1).strip(), m.group(2)
        return url if label == url else f"{label} ({url})"

    text = _protect(text, _LINK, link, store)
    text = _HEADING.sub(r"\1", text)
    text = _BOLD.sub(lambda m: m.group(1) or m.group(2), text)
    text = _STRIKE.sub(r"\1", text)
    text = _ITALIC.sub(r"\1", text)
    return _restore(text, store)
