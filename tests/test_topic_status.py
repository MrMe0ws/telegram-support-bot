from __future__ import annotations

import asyncio
import os
from dataclasses import replace
from types import SimpleNamespace

import pytest

os.environ.setdefault("TEXTS_DIR", os.path.join(os.path.dirname(__file__), "..", "texts"))

from app.services import topic_status  # noqa: E402
from app.services.ai.provider import AiError, AiRateLimited, AiReply, AiUnavailable, GeminiProvider  # noqa: E402
from app.services.ai.service import set_ai_service  # noqa: E402
from tests.test_ai import GROUP_ID, FakeBot, _client_says, _settings, _setup, _wait  # noqa: E402

WAIT = "111"
OPEN = "222"


class IconBot(FakeBot):
    def __init__(self) -> None:
        super().__init__()
        self.icons: list[str] = []

    async def edit_forum_topic(self, chat_id, message_thread_id, icon_custom_emoji_id):
        assert chat_id == GROUP_ID and message_thread_id == 7
        self.icons.append(icon_custom_emoji_id)

    async def get_forum_topic_icon_stickers(self):
        return [
            SimpleNamespace(emoji="❓", custom_emoji_id=OPEN),
            SimpleNamespace(emoji="❗️", custom_emoji_id=WAIT),
        ]


async def _setup_icons(tmp_path, reply: AiReply, mode: str = "auto"):
    svc, sf, _, provider, tid, engine = await _setup(tmp_path, reply, mode)
    bot = IconBot()
    settings = replace(svc._settings, topic_icon_emoji_open=OPEN, topic_icon_emoji_waiting=WAIT)
    svc._bot, svc._settings = bot, settings
    return svc, sf, bot, settings, tid, engine


# ── Значок «ждёт человека» ──────────────────────────────────────────────


def test_escalation_marks_waiting_and_ai_reply_clears(tmp_path) -> None:
    async def scenario():
        svc, sf, bot, settings, tid, engine = await _setup_icons(
            tmp_path, AiReply(escalate_reason="нет в базе", escalate_summary="вопрос")
        )
        await _client_says(sf, tid, "вопрос")
        svc.schedule(tid)
        await _wait(svc)
        assert bot.icons == [WAIT]

        # Повторная отметка не дёргает Telegram.
        await topic_status.set_waiting(bot, sf, settings, tid, True)
        assert bot.icons == [WAIT]

        # Ответ клиенту (сотрудник или ИИ) снимает значок.
        await topic_status.set_waiting(bot, sf, settings, tid, False)
        assert bot.icons == [WAIT, OPEN]
        await engine.dispose()

    asyncio.run(scenario())


def test_ai_error_marks_waiting(tmp_path) -> None:
    async def scenario():
        svc, sf, bot, settings, tid, engine = await _setup_icons(tmp_path, AiReply(text="x"))

        async def broken(system, turns):
            raise AiRateLimited("429")

        svc._provider.reply = broken
        await _client_says(sf, tid, "вопрос")
        svc.schedule(tid)
        await _wait(svc)
        assert bot.icons == [WAIT]
        await engine.dispose()

    asyncio.run(scenario())


def test_client_message_icon_depends_on_who_answers(tmp_path) -> None:
    async def scenario():
        svc, sf, bot, settings, tid, engine = await _setup_icons(tmp_path, AiReply(text="ответ"))
        try:
            # ИИ в режиме auto ответит сам — значок не нужен.
            set_ai_service(svc)
            await topic_status.after_client_message(bot, sf, settings, tid)
            assert bot.icons == []

            # ИИ в тикете молчит — ответит человек.
            await svc.set_enabled_in_ticket(tid, False)
            await topic_status.after_client_message(bot, sf, settings, tid)
            assert bot.icons == [WAIT]

            # Без ИИ вовсе — тоже человек.
            await topic_status.set_waiting(bot, sf, settings, tid, False)
            set_ai_service(None)
            await topic_status.after_client_message(bot, sf, settings, tid)
            assert bot.icons == [WAIT, OPEN, WAIT]
        finally:
            set_ai_service(None)
            await engine.dispose()

    asyncio.run(scenario())


def test_draft_mode_client_message_waits_for_human(tmp_path) -> None:
    async def scenario():
        svc, sf, bot, settings, tid, engine = await _setup_icons(tmp_path, AiReply(text="ч"), mode="draft")
        try:
            set_ai_service(svc)
            await topic_status.after_client_message(bot, sf, settings, tid)
            assert bot.icons == [WAIT]
        finally:
            set_ai_service(None)
            await engine.dispose()

    asyncio.run(scenario())


def test_icon_disabled_does_nothing(tmp_path) -> None:
    async def scenario():
        svc, sf, bot, settings, tid, engine = await _setup_icons(tmp_path, AiReply(text="ответ"))
        settings = replace(settings, topic_icon_emoji_waiting="")
        await topic_status.after_client_message(bot, sf, settings, tid)
        await topic_status.set_waiting(bot, sf, settings, tid, True)
        assert bot.icons == []
        await engine.dispose()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("❗", WAIT), ("❗️", WAIT), ("12345", "12345"), ("", ""), ("🦄", "")],
)
def test_resolve_waiting_icon(tmp_path, raw, expected) -> None:
    settings = replace(_settings(tmp_path), topic_icon_emoji_waiting=raw)
    resolved = asyncio.run(topic_status.resolve_waiting_icon(IconBot(), settings))
    assert resolved.topic_icon_emoji_waiting == expected


# ── Запасная модель ─────────────────────────────────────────────────────


def _provider(tmp_path, outcomes: dict) -> tuple[GeminiProvider, list[str]]:
    ai = replace(_settings(tmp_path).ai, model="main", fallback_model="lite")
    provider = GeminiProvider(ai)
    calls: list[str] = []

    async def fake_generate(model, contents, config):
        calls.append(model)
        outcome = outcomes[model]
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    provider._generate = fake_generate
    return provider, calls


def _text_resp(text: str):
    from google.genai import types

    return types.GenerateContentResponse(
        candidates=[types.Candidate(content=types.Content(role="model", parts=[types.Part(text=text)]))]
    )


@pytest.mark.parametrize("error", [AiRateLimited("429"), AiUnavailable("503 UNAVAILABLE")])
def test_fallback_model_on_limit_or_overload(tmp_path, error) -> None:
    provider, calls = _provider(tmp_path, {"main": error, "lite": _text_resp("ответ запасной")})
    reply = asyncio.run(provider.reply("system", []))
    assert reply.text == "ответ запасной"
    assert calls == ["main", "lite"]


def test_fallback_not_used_for_other_errors(tmp_path) -> None:
    provider, calls = _provider(tmp_path, {"main": AiError("400 bad request"), "lite": _text_resp("x")})
    with pytest.raises(AiError):
        asyncio.run(provider.reply("system", []))
    assert calls == ["main"]


def test_both_models_limited_raises_rate_limited(tmp_path) -> None:
    provider, calls = _provider(tmp_path, {"main": AiRateLimited("429"), "lite": AiRateLimited("429")})
    with pytest.raises(AiRateLimited):
        asyncio.run(provider.reply("system", []))
    assert calls == ["main", "lite"]


def test_fallback_same_as_main_is_skipped(tmp_path) -> None:
    ai = replace(_settings(tmp_path).ai, model="main", fallback_model="main")
    assert GeminiProvider(ai)._models == ["main"]
