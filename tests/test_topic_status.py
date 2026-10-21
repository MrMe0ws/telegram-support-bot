from __future__ import annotations

import asyncio
import os
from dataclasses import replace
from types import SimpleNamespace

import pytest

os.environ.setdefault("TEXTS_DIR", os.path.join(os.path.dirname(__file__), "..", "texts"))

from app.services import topic_status  # noqa: E402
from app.services.ai.provider import AiError, AiRateLimited, AiReply, AiUnavailable, ModelChain  # noqa: E402
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


def test_after_staff_reply_icon_stays_open(tmp_path) -> None:
    async def scenario():
        svc, sf, bot, settings, tid, engine = await _setup_icons(tmp_path, AiReply(text="ответ"))
        try:
            set_ai_service(None)
            await topic_status.after_client_message(bot, sf, settings, tid)
            await topic_status.set_waiting(bot, sf, settings, tid, False, by_staff=True)
            assert bot.icons == [WAIT, OPEN]

            # Клиент пишет снова, ИИ передаёт людям — значок уже не прыгает.
            await topic_status.after_client_message(bot, sf, settings, tid)
            await topic_status.set_waiting(bot, sf, settings, tid, True)
            await topic_status.set_waiting(bot, sf, settings, tid, False, by_staff=True)
            assert bot.icons == [WAIT, OPEN]
        finally:
            set_ai_service(None)
            await engine.dispose()

    asyncio.run(scenario())


def test_staff_reply_before_waiting_also_locks(tmp_path) -> None:
    async def scenario():
        svc, sf, bot, settings, tid, engine = await _setup_icons(tmp_path, AiReply(text="ответ"))
        await topic_status.set_waiting(bot, sf, settings, tid, False, by_staff=True)
        await topic_status.set_waiting(bot, sf, settings, tid, True)
        assert bot.icons == []
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


# ── Резервные модели ────────────────────────────────────────────────────


class FakeBackend:
    def __init__(self, provider: str, outcomes: dict, calls: list[str]) -> None:
        self._provider = provider
        self._outcomes = outcomes
        self._calls = calls

    async def reply(self, model, system, turns):
        spec = f"{self._provider}:{model}"
        self._calls.append(spec)
        outcome = self._outcomes[spec]
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def _chain(tmp_path, outcomes: dict, keys=("gemini", "openai", "claude")) -> tuple[ModelChain, list[str]]:
    ai = replace(
        _settings(tmp_path).ai,
        models=tuple(outcomes),
        api_keys=tuple((p, "k") for p in keys),
    )
    calls: list[str] = []
    chain = ModelChain(ai, lambda provider, _settings: FakeBackend(provider, outcomes, calls))
    return chain, calls


@pytest.mark.parametrize("error", [AiRateLimited("429"), AiUnavailable("503 UNAVAILABLE"), AiError("400 bad request")])
def test_fallback_model_on_error(tmp_path, error) -> None:
    chain, calls = _chain(tmp_path, {"gemini:main": error, "gemini:lite": AiReply(text="ответ запасной")})
    reply = asyncio.run(chain.reply("system", []))
    assert reply.text == "ответ запасной"
    assert calls == ["gemini:main", "gemini:lite"]


def test_fallback_to_other_provider(tmp_path) -> None:
    chain, calls = _chain(
        tmp_path,
        {
            "claude:claude-sonnet-5-5": AiUnavailable("529 overloaded"),
            "openai:gpt-6-luna": AiRateLimited("429"),
            "gemini:gemini-2.5-flash": AiReply(text="ответ gemini"),
        },
    )
    assert asyncio.run(chain.reply("system", [])).text == "ответ gemini"
    assert calls == ["claude:claude-sonnet-5-5", "openai:gpt-6-luna", "gemini:gemini-2.5-flash"]


def test_models_without_key_are_skipped(tmp_path) -> None:
    chain, calls = _chain(
        tmp_path,
        {"openai:gpt-6-luna": AiReply(text="x"), "claude:claude-haiku-4-5": AiReply(text="ответ claude")},
        keys=("claude",),
    )
    assert asyncio.run(chain.reply("system", [])).text == "ответ claude"
    assert calls == ["claude:claude-haiku-4-5"]


def test_all_models_limited_raises_rate_limited(tmp_path) -> None:
    chain, calls = _chain(tmp_path, {"gemini:main": AiRateLimited("429"), "gemini:lite": AiRateLimited("429")})
    with pytest.raises(AiRateLimited):
        asyncio.run(chain.reply("system", []))
    assert calls == ["gemini:main", "gemini:lite"]


def test_set_models_reorders_and_validates(tmp_path) -> None:
    chain, calls = _chain(
        tmp_path, {"gemini:a": AiReply(text="a"), "claude:b": AiReply(text="b")}, keys=("gemini", "claude")
    )
    chain.set_models(("claude:b", "gemini:a", "claude:b"))
    assert chain.models == ("claude:b", "gemini:a")
    assert asyncio.run(chain.reply("system", [])).text == "b"
    with pytest.raises(ValueError):
        chain.set_models(())
    with pytest.raises(ValueError):
        chain.set_models(("openai:gpt-6-luna",))  # нет ключа
    assert chain.models == ("claude:b", "gemini:a")