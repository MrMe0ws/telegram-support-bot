from __future__ import annotations

import urllib.error

import pytest

from tools.release import (
    Footer,
    ReleaseError,
    ReleaseNotes,
    Section,
    _parse_version_line,
    _proxy_unreachable,
    format_telegram_html,
    is_collapsible,
    load_env_file,
    normalize_version,
    parse_release_notes,
    visible_length,
)

SAMPLE = """# Релиз v2.1.0

## Вариант для GitHub Releases

## 🚀 Новое

- **ИИ-ответы** клиентам.

## 🐛 Исправления

- `/profile` показывает профиль магазина.

---

## Вариант для Telegram

2.1.0 https://github.com/MrMe0ws/telegram-support-bot/releases/tag/2.1.0

🚀 Новое

• ИИ отвечает клиентам по базе знаний & зовёт человека <когда нужно>.


🐛 Исправления

• /profile показывает профиль магазина.

• Второй пункт.
"""


def test_parse_release_notes() -> None:
    n = parse_release_notes(SAMPLE.replace("\n", "\r\n"))
    assert n.version == "2.1.0"
    assert n.release_url.endswith("releases/tag/2.1.0")
    assert n.github_body.startswith("## 🚀 Новое")
    assert "---" not in n.github_body
    assert [(s.header, len(s.items)) for s in n.sections] == [("🚀 Новое", 1), ("🐛 Исправления", 2)]


def test_version_line_with_parens_and_v_prefix() -> None:
    v, url = _parse_version_line("v2.1.0 (https://github.com/x/y/releases/tag/2.1.0)")
    assert v == "2.1.0" and url == "https://github.com/x/y/releases/tag/2.1.0"
    assert normalize_version(" v2.1 ") == "2.1"


@pytest.mark.parametrize(
    "bad",
    [
        SAMPLE.replace("## Вариант для Telegram", "## Телеграм"),
        SAMPLE.replace("2.1.0 https://", "2.1.0 http://"),
        SAMPLE.replace("🚀 Новое\n\n• ИИ", "• ИИ", 1).replace("## 🚀 Новое", "## Новое"),
    ],
)
def test_parse_errors(bad: str) -> None:
    with pytest.raises(ReleaseError):
        parse_release_notes(bad)


def test_format_telegram_html() -> None:
    html = format_telegram_html(
        parse_release_notes(SAMPLE),
        Footer(text="Meows VPN Group", url="https://t.me/meows_vpn_bot", custom_emoji_id="545"),
    )
    assert html.startswith('<a href="https://github.com/MrMe0ws/telegram-support-bot/releases/tag/2.1.0">2.1.0</a>\n\n<b>🚀 Новое</b>\n\n• ')
    # HTML-символы в тексте экранируются.
    assert "&amp; зовёт человека &lt;когда нужно&gt;" in html
    # Две пустые строки между секциями, одна — между пунктами.
    assert "\n\n\n<b>🐛 Исправления</b>\n\n• /profile" in html
    assert "магазина.\n\n• Второй пункт." in html
    assert html.endswith('<tg-emoji emoji-id="545">✨</tg-emoji> <a href="https://t.me/meows_vpn_bot">Meows VPN Group</a>')
    assert "**" not in html


def test_long_secondary_sections_collapse() -> None:
    notes = ReleaseNotes(
        version="9.9.9",
        release_url="https://example.test/9.9.9",
        github_body="",
        sections=[
            Section("🚀 Новое", ["a", "b", "c", "d"]),
            Section("🐛 Исправления", ["x", "y", "z"]),
            Section("✨ Улучшения", ["p", "q"]),
        ],
    )
    html = format_telegram_html(notes, Footer())
    assert html.count("<blockquote expandable>") == 1 and html.count("</blockquote>") == 1
    assert "<b>🐛 Исправления</b>\n\n<blockquote expandable>• x\n\n• y\n\n• z</blockquote>" in html
    assert not is_collapsible(Section("🐛 Исправления", ["a", "b"]))
    assert not is_collapsible(Section("🚀 Новое", ["a", "b", "c", "d"]))


def test_github_body_has_no_telegram_markup() -> None:
    body = parse_release_notes(SAMPLE).github_body
    for forbidden in ("<blockquote", "expandable", "<tg-emoji", "•"):
        assert forbidden not in body


def test_visible_length_ignores_markup() -> None:
    assert visible_length('<a href="https://x">2.1</a> <b>&amp;</b>') == len("2.1 &")


def test_proxy_unreachable_only_when_request_never_left() -> None:
    assert _proxy_unreachable(urllib.error.URLError(ConnectionRefusedError(10061, "refused")))
    assert not _proxy_unreachable(urllib.error.URLError(TimeoutError("timed out")))
    assert not _proxy_unreachable(urllib.error.URLError("ssl error"))


def test_load_env_file_does_not_override(tmp_path, monkeypatch) -> None:
    env = tmp_path / ".env.release"
    env.write_text('# comment\nRELEASE_TG_CHAT_ID="@chat"\nRELEASE_GITHUB_REPO=a/b\n', encoding="utf-8")
    monkeypatch.delenv("RELEASE_TG_CHAT_ID", raising=False)
    monkeypatch.setenv("RELEASE_GITHUB_REPO", "keep/me")
    load_env_file(env)
    import os

    assert os.environ["RELEASE_TG_CHAT_ID"] == "@chat"
    assert os.environ["RELEASE_GITHUB_REPO"] == "keep/me"
