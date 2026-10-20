"""
Релиз из RELEASE_NOTES.md: GitHub Release и анонс в Telegram-форум.

Инструмент разработчика, в Docker-образ бота не входит (образ копирует только
app/ и texts/). Только стандартная библиотека — ставить ничего не нужно.

Из корня репозитория:

    python tools/release.py preview    # разобрать заметки и показать оба варианта
    python tools/release.py github     # gh release create из блока для GitHub
    python tools/release.py telegram   # отправить пост в тему форума
    python tools/release.py publish    # github, затем telegram

Настройки — .env.release в корне (образец .env.release.sample, в git не кладём).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from html import escape
from pathlib import Path

GITHUB_HEADING = "## Вариант для GitHub Releases"
TELEGRAM_HEADING = "## Вариант для Telegram"

# Telegram не принимает сообщение длиннее 4096 символов (разметка не считается).
TELEGRAM_MAX_CHARS = 4096

# Секции, которые в посте сворачиваются в раскрывающуюся цитату, если в них
# от COLLAPSIBLE_MIN_ITEMS пунктов. «Новое» не сворачивается никогда: ради него
# пост и читают. Заголовок приходит с эмодзи — сверяем по вхождению.
COLLAPSIBLE_HEADERS = ("Улучшения", "Исправления", "При обновлении", "Технические изменения", "Breaking changes")
COLLAPSIBLE_MIN_ITEMS = 3

ROOT = Path(__file__).resolve().parent.parent


class ReleaseError(Exception):
    pass


@dataclass
class Section:
    header: str
    items: list[str] = field(default_factory=list)


@dataclass
class ReleaseNotes:
    version: str
    release_url: str
    github_body: str
    sections: list[Section]


@dataclass
class Footer:
    text: str = ""
    url: str = ""
    custom_emoji_id: str = ""
    custom_emoji_alt: str = ""


# ── Разбор RELEASE_NOTES.md ─────────────────────────────────────────────


def normalize_version(version: str) -> str:
    """Теги в репозитории без «v»: 2.1.0, а не v2.1.0."""
    return version.strip().removeprefix("v")


def parse_release_notes(content: str) -> ReleaseNotes:
    content = content.replace("\r\n", "\n").strip()
    if not content:
        raise ReleaseError("RELEASE_NOTES.md пуст")
    gh = content.find(GITHUB_HEADING)
    tg = content.find(TELEGRAM_HEADING)
    if gh < 0:
        raise ReleaseError(f"нет раздела «{GITHUB_HEADING}»")
    if tg < 0:
        raise ReleaseError(f"нет раздела «{TELEGRAM_HEADING}»")
    if tg < gh:
        raise ReleaseError("раздел для Telegram должен идти после раздела для GitHub")

    github_body = _strip_trailing_rule(content[gh + len(GITHUB_HEADING) : tg])
    version, url, sections = _parse_telegram_block(content[tg + len(TELEGRAM_HEADING) :].strip())
    return ReleaseNotes(version=version, release_url=url, github_body=github_body, sections=sections)


def _strip_trailing_rule(text: str) -> str:
    lines = text.strip().split("\n")
    while lines and lines[-1].strip() == "---":
        lines.pop()
    return "\n".join(lines).strip()


def _parse_version_line(line: str) -> tuple[str, str]:
    """«2.1.0 https://github.com/…/releases/tag/2.1.0» или «2.1.0 (https://…)»."""
    fields = line.split()
    if not fields:
        raise ReleaseError("блок Telegram: пустая строка версии")
    version = normalize_version(fields[0].strip("()"))
    if not version:
        raise ReleaseError("блок Telegram: пустая версия")
    if len(fields) == 1:
        raise ReleaseError("блок Telegram: в строке версии нужна ссылка на релиз")
    url = " ".join(fields[1:]).strip().removeprefix("(").removesuffix(")").strip()
    if not url.startswith("https://"):
        raise ReleaseError(f"блок Telegram: ссылка на релиз должна начинаться с https:// (а не {url!r})")
    return version, url


def _parse_telegram_block(raw: str) -> tuple[str, str, list[Section]]:
    lines = raw.split("\n")
    if not lines or not lines[0].strip():
        raise ReleaseError("блок Telegram: нет строки с версией")
    version, url = _parse_version_line(lines[0].strip())
    sections: list[Section] = []
    for line in lines[1:]:
        line = line.strip()
        if not line:
            continue
        if line.startswith("•"):
            if not sections:
                raise ReleaseError(f"блок Telegram: пункт без заголовка секции: {line!r}")
            sections[-1].items.append(line.removeprefix("•").strip())
        else:
            sections.append(Section(header=line))
    return version, url, sections


# ── Пост для Telegram ───────────────────────────────────────────────────


def is_collapsible(section: Section) -> bool:
    return len(section.items) >= COLLAPSIBLE_MIN_ITEMS and any(
        name in section.header for name in COLLAPSIBLE_HEADERS
    )


def format_telegram_html(notes: ReleaseNotes, footer: Footer, title: str = "") -> str:
    """
    HTML для Bot API (parse_mode=HTML): над версией — жирный заголовок поста
    (чей это релиз: в ту же тему пишут и другие проекты), версия — ссылка на
    релиз, заголовки секций жирные, пустая строка между пунктами, две — между
    секциями. Длинные второстепенные секции — в <blockquote expandable>.
    """
    out = [f"<b>{escape(title.strip(), quote=False)}</b>\n"] if title.strip() else []
    out.append(f'<a href="{escape(notes.release_url)}">{escape(notes.version)}</a>')
    for i, sec in enumerate(notes.sections):
        out.append("\n\n" if i == 0 else "\n\n\n")
        out.append(f"<b>{escape(sec.header, quote=False)}</b>")
        if is_collapsible(sec):
            # Пустая строка перед первым пунктом внутри цитаты съела бы короткое превью.
            body = "\n\n".join(f"• {escape(item, quote=False)}" for item in sec.items)
            out.append(f"\n\n<blockquote expandable>{body}</blockquote>")
        else:
            out.extend(f"\n\n• {escape(item, quote=False)}" for item in sec.items)

    if footer.text.strip() and footer.url.strip():
        out.append("\n\n")
        if footer.custom_emoji_id.strip():
            alt = footer.custom_emoji_alt.strip() or "✨"
            out.append(f'<tg-emoji emoji-id="{escape(footer.custom_emoji_id.strip())}">{escape(alt)}</tg-emoji> ')
        out.append(f'<a href="{escape(footer.url.strip())}">{escape(footer.text.strip(), quote=False)}</a>')
    return "".join(out)


def visible_length(html: str) -> int:
    """Длина текста так, как её считает Telegram: без тегов, с раскрытыми сущностями."""
    import html as html_mod
    import re

    return len(html_mod.unescape(re.sub(r"<[^>]+>", "", html)))


# ── Настройки ───────────────────────────────────────────────────────────


def load_env_file(path: Path) -> None:
    """Простой разбор .env: KEY=value, комментарии с #. Уже заданные переменные не трогаем."""
    if not path.is_file():
        return
    for raw in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        os.environ.setdefault(key.strip(), value)


def _env(key: str) -> str:
    return os.environ.get(key, "").strip()


def load_footer() -> Footer:
    return Footer(
        text=_env("RELEASE_TG_FOOTER_TEXT"),
        url=_env("RELEASE_TG_FOOTER_URL"),
        custom_emoji_id=_env("RELEASE_TG_FOOTER_CUSTOM_EMOJI_ID"),
        custom_emoji_alt=_env("RELEASE_TG_FOOTER_CUSTOM_EMOJI_ALT"),
    )


# ── Публикация ──────────────────────────────────────────────────────────


def create_github_release(notes: ReleaseNotes, repo: str) -> None:
    tag = normalize_version(notes.version)
    args = ["gh", "release", "create", tag, "--title", tag, "--notes", notes.github_body]
    if repo:
        args += ["--repo", repo]
    try:
        subprocess.run(args, check=True)
    except FileNotFoundError as e:
        raise ReleaseError("не найден gh (GitHub CLI): https://cli.github.com") from e
    except subprocess.CalledProcessError as e:
        raise ReleaseError(f"gh release create завершился с кодом {e.returncode}") from e


def _proxy_unreachable(err: Exception) -> bool:
    """
    Запрос заведомо не ушёл: не подключились к самому прокси. Только тогда можно
    повторить напрямую — иначе анонс мог уже дойти и ушёл бы в канал дважды.
    """
    reason = getattr(err, "reason", err)
    return isinstance(reason, (ConnectionRefusedError, ConnectionResetError)) or (
        isinstance(reason, OSError) and getattr(reason, "errno", None) in (10061, 111)
    )


def _post(url: str, payload: bytes, proxy: str) -> dict:
    handlers = [urllib.request.ProxyHandler({"https": proxy, "http": proxy})] if proxy else []
    opener = urllib.request.build_opener(*handlers)
    req = urllib.request.Request(url, data=payload, headers={"Content-Type": "application/json"})
    try:
        with opener.open(req, timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        # Ответ Telegram с ошибкой — в теле описание (без токена).
        try:
            return json.loads(e.read().decode("utf-8"))
        except ValueError:
            raise ReleaseError(f"Telegram ответил {e.code}") from None


def send_telegram(html: str) -> None:
    token = _env("RELEASE_TG_BOT_TOKEN")
    chat_id = _env("RELEASE_TG_CHAT_ID")
    if not token:
        raise ReleaseError("RELEASE_TG_BOT_TOKEN не задан (.env.release)")
    if not chat_id:
        raise ReleaseError("RELEASE_TG_CHAT_ID не задан (.env.release)")
    length = visible_length(html)
    if length > TELEGRAM_MAX_CHARS:
        raise ReleaseError(
            f"пост для Telegram — {length} символов (лимит {TELEGRAM_MAX_CHARS}): "
            "сократите раздел для Telegram в RELEASE_NOTES.md"
        )
    body: dict = {"chat_id": chat_id, "text": html, "parse_mode": "HTML", "disable_web_page_preview": True}
    thread = _env("RELEASE_TG_MESSAGE_THREAD_ID")
    if thread:
        if not thread.isdigit():
            raise ReleaseError("RELEASE_TG_MESSAGE_THREAD_ID должен быть числом")
        body["message_thread_id"] = int(thread)
    payload = json.dumps(body).encode("utf-8")
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    proxy = _env("RELEASE_TG_PROXY_URL")
    if proxy and not proxy.lower().startswith(("http://", "https://")):
        # urllib умеет только HTTP-прокси; у Clash/FlClash он на том же порту, что и mixed.
        raise ReleaseError("RELEASE_TG_PROXY_URL: нужен http:// прокси (например http://127.0.0.1:7890)")

    try:
        result = _post(url, payload, proxy)
    except (urllib.error.URLError, OSError) as e:
        if not (proxy and _proxy_unreachable(e)):
            # str(e) не содержит URL с токеном — urllib его не печатает.
            raise ReleaseError(f"Telegram недоступен (прокси {proxy or 'нет'}): {e}") from None
        print(f"telegram: прокси {proxy} недоступен, пробую напрямую")
        try:
            result = _post(url, payload, "")
        except (urllib.error.URLError, OSError) as e2:
            raise ReleaseError(f"Telegram недоступен ни через прокси, ни напрямую: {e2}") from None
    if not result.get("ok"):
        raise ReleaseError(f"Telegram: {result.get('description') or result}")


# ── CLI ─────────────────────────────────────────────────────────────────

USAGE = __doc__


def main(argv: list[str]) -> int:
    if not argv or argv[0] in ("-h", "--help"):
        print(USAGE)
        return 0
    cmd = argv[0]
    if cmd not in ("preview", "github", "telegram", "publish"):
        print(USAGE, file=sys.stderr)
        print(f"release: неизвестная команда {cmd!r}", file=sys.stderr)
        return 2

    load_env_file(ROOT / ".env.release")
    notes_path = Path(_env("RELEASE_NOTES_PATH") or "RELEASE_NOTES.md")
    if not notes_path.is_absolute():
        notes_path = ROOT / notes_path
    try:
        notes = parse_release_notes(notes_path.read_text(encoding="utf-8"))
        repo = _env("RELEASE_GITHUB_REPO")
        html = format_telegram_html(notes, load_footer(), _env("RELEASE_TG_TITLE"))

        if cmd == "preview":
            print(f"version: {notes.version}")
            print(f"release_url: {notes.release_url}")
            print(f"github_repo: {repo or '(origin)'}")
            print(f"telegram: {visible_length(html)} / {TELEGRAM_MAX_CHARS} символов")
            print("--- GitHub ---")
            print(notes.github_body)
            print("--- Telegram HTML ---")
            print(html)
        elif cmd == "github":
            create_github_release(notes, repo)
        elif cmd == "telegram":
            send_telegram(html)
            print("telegram: сообщение отправлено")
        else:
            create_github_release(notes, repo)
            print("github: релиз создан")
            try:
                send_telegram(html)
            except ReleaseError as e:
                raise ReleaseError(
                    f"GitHub-релиз создан, а Telegram — нет ({e}). Повторить: python tools/release.py telegram"
                ) from None
            print("telegram: сообщение отправлено")
    except (ReleaseError, OSError) as e:
        print(f"release: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    # Консоль Windows по умолчанию не в UTF-8 — без этого падает печать эмодзи.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    sys.exit(main(sys.argv[1:]))
