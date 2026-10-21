"""Настройки, которые админ меняет в боте (⚙️ Настройки): хранение, проверка, экраны меню."""

from __future__ import annotations

import asyncio
import os
from dataclasses import replace
from types import SimpleNamespace

import pytest

os.environ.setdefault("TEXTS_DIR", os.path.join(os.path.dirname(__file__), "..", "texts"))

from app import i18n  # noqa: E402
from app.db.session import create_engine, init_db, session_factory  # noqa: E402
from app.handlers import admin_settings  # noqa: E402
from app.i18n import tr, tr_default  # noqa: E402
from app.services import live_settings  # noqa: E402
from app.services.ai.knowledge import KnowledgeLoader  # noqa: E402
from app.services.ai.provider import AiReply, ModelChain  # noqa: E402
from app.services.ai.service import AiService, get_ai_service, peek_ai_service, set_ai_service  # noqa: E402
from tests.test_ai import FakeBot, _settings  # noqa: E402


@pytest.fixture(autouse=True)
def _clean_state():
    yield
    live_settings._base = None
    live_settings._current = None
    live_settings._overrides.clear()
    live_settings._listeners.clear()
    i18n.set_text_overrides({})
    set_ai_service(None)
    admin_settings._icons = None


async def _sf(tmp_path):
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path / 'db.sqlite'}")
    await init_db(engine)
    return engine, session_factory(engine)


def test_change_applies_at_once_and_survives_restart(tmp_path) -> None:
    async def run():
        engine, sf = await _sf(tmp_path)
        base = _settings(tmp_path)
        await live_settings.init(sf, base)
        seen = []
        live_settings.subscribe(seen.append)

        await live_settings.set_value(sf, "ai.reply_delay_sec", "5")
        await live_settings.set_value(sf, "topic_name_template", "#{ticket_id} · {name}")
        assert live_settings.current().ai.reply_delay_sec == 5
        assert live_settings.current().topic_name_template == "#{ticket_id} · {name}"
        assert seen[-1] is live_settings.current()
        assert live_settings.is_overridden("ai.reply_delay_sec")
        # .env не трогаем: базовые настройки прежние.
        assert live_settings.base().ai.reply_delay_sec == base.ai.reply_delay_sec

        # Перезапуск: изменения подтягиваются из БД.
        restored = await live_settings.init(sf, base)
        assert restored.ai.reply_delay_sec == 5
        assert restored.topic_name_template == "#{ticket_id} · {name}"

        await live_settings.reset(sf, "ai.reply_delay_sec")
        assert live_settings.current().ai.reply_delay_sec == base.ai.reply_delay_sec
        assert not live_settings.is_overridden("ai.reply_delay_sec")
        await engine.dispose()

    asyncio.run(run())


def test_value_same_as_env_is_not_an_override(tmp_path) -> None:
    async def run():
        engine, sf = await _sf(tmp_path)
        await live_settings.init(sf, _settings(tmp_path, mode="auto"))
        await live_settings.set_value(sf, "ai.mode", "draft")
        assert live_settings.is_overridden("ai.mode")
        await live_settings.set_value(sf, "ai.mode", "auto")
        assert not live_settings.is_overridden("ai.mode")
        await engine.dispose()

    asyncio.run(run())


@pytest.mark.parametrize(
    ("key", "raw"),
    [
        ("ai.reply_delay_sec", "500"),
        ("ai.reply_delay_sec", "много"),
        ("ai.max_replies_per_ticket", "2.5"),
        ("ai.mode", "turbo"),
        ("ai.assistant_name", "   "),
        ("topic_name_template", "{nick} · #{ticket_id}"),
        ("topic_name_template", "#{ticket_id"),
        ("topic_icon_emoji_waiting", "❗"),
        ("remnawave.timezone", "Mars/Olympus"),
    ],
)
def test_bad_values_are_rejected(key, raw) -> None:
    with pytest.raises(ValueError):
        live_settings.parse(key, raw)


def test_bad_value_in_db_is_skipped(tmp_path) -> None:
    async def run():
        from app.services import bot_settings

        engine, sf = await _sf(tmp_path)
        async with sf() as session:
            async with session.begin():
                await bot_settings.set_value(session, "ai.history_limit", "100500")
                await bot_settings.set_value(session, "ai.media_enabled", "false")
        current = await live_settings.init(sf, _settings(tmp_path))
        assert current.ai.history_limit == 30
        assert current.ai.media_enabled is False
        await engine.dispose()

    asyncio.run(run())


def test_client_texts_override_yaml(tmp_path) -> None:
    async def run():
        engine, sf = await _sf(tmp_path)
        await live_settings.init(sf, _settings(tmp_path))
        default = tr("user", "start")
        key = live_settings.text_key("user", "start")

        await live_settings.set_value(sf, key, "<b>Привет!</b> {без форматирования}")
        assert tr("user", "start") == "<b>Привет!</b> {без форматирования}"
        assert tr_default("user", "start") == default

        await live_settings.init(sf, _settings(tmp_path))
        assert tr("user", "start") == "<b>Привет!</b> {без форматирования}"

        await live_settings.reset(sf, key)
        assert tr("user", "start") == default

        with pytest.raises(ValueError):
            await live_settings.set_value(sf, live_settings.text_key("admin", "no_permission"), "x")
        await engine.dispose()

    asyncio.run(run())


def test_reset_all(tmp_path) -> None:
    async def run():
        engine, sf = await _sf(tmp_path)
        base = _settings(tmp_path)
        await live_settings.init(sf, base)
        await live_settings.set_value(sf, "ai.enabled", "false")
        await live_settings.set_value(sf, "log_level", "DEBUG")
        await live_settings.set_value(sf, live_settings.text_key("user", "blocked"), "нет")
        await live_settings.reset_all(sf)
        assert live_settings.overridden_keys() == []
        assert live_settings.current() == base
        assert tr("user", "blocked") == tr_default("user", "blocked")
        assert await live_settings.init(sf, base) == base
        await engine.dispose()

    asyncio.run(run())


def test_turning_ai_off_hides_service_and_cancels_replies(tmp_path) -> None:
    async def run():
        engine, sf = await _sf(tmp_path)
        settings = await live_settings.init(sf, _settings(tmp_path, delay=10))
        svc = AiService(FakeBot(), sf, settings, _chain(settings), KnowledgeLoader(settings.ai.knowledge_dir))
        set_ai_service(svc)
        live_settings.subscribe(svc.apply_settings)
        svc.schedule(1)
        assert get_ai_service() is svc

        await live_settings.set_value(sf, "ai.enabled", "false")
        assert get_ai_service() is None
        assert peek_ai_service() is svc
        assert 1 in svc._stopped

        await live_settings.set_value(sf, "ai.mode", "draft")
        assert svc.mode == "draft"
        await live_settings.set_value(sf, "ai.enabled", "true")
        assert get_ai_service() is svc
        await asyncio.sleep(0)
        await engine.dispose()

    asyncio.run(run())


# ── Экраны меню ─────────────────────────────────────────────────────────


class IconBot(FakeBot):
    async def get_forum_topic_icon_stickers(self):
        return [
            SimpleNamespace(custom_emoji_id="111", emoji="❓"),
            SimpleNamespace(custom_emoji_id="222", emoji="✅"),
            SimpleNamespace(custom_emoji_id="333", emoji="❗"),
        ]


class _Backend:
    async def reply(self, model, system, turns):
        return AiReply(text="ok")


def _chain(settings) -> ModelChain:
    return ModelChain(settings.ai, lambda provider, _s: _Backend())


def _all_text(text: str, markup) -> str:
    labels = [b.text for row in markup.inline_keyboard for b in row]
    datas = [b.callback_data or "" for row in markup.inline_keyboard for b in row]
    assert all(len(d.encode()) <= 64 for d in datas), datas
    return "\n".join([text, *labels])


@pytest.mark.parametrize("with_ai", [True, False])
def test_every_screen_renders(tmp_path, with_ai) -> None:
    async def run():
        engine, sf = await _sf(tmp_path)
        base = replace(_settings(tmp_path), topic_icon_emoji_open="111", topic_icon_emoji_waiting="333")
        settings = await live_settings.init(sf, base)
        bot = IconBot()
        if with_ai:
            set_ai_service(AiService(bot, sf, settings, _chain(settings), KnowledgeLoader(settings.ai.knowledge_dir)))
        # Что-нибудь изменено — чтобы показались «✏️» и «Как в .env».
        await live_settings.set_value(sf, "ai.reply_delay_sec", "30")
        await live_settings.set_value(sf, "topic_icon_emoji_closed", "222")
        await live_settings.set_value(sf, live_settings.text_key("user", "start"), "<i>hi</i>")
        ctx = admin_settings.Ctx(bot=bot, session_factory=sf, settings=live_settings.current())

        screens = [await view(ctx) for view in admin_settings.SCREENS.values()]
        screens += [admin_settings.view_field(key) for key in admin_settings.FIELD_UI]
        screens += [await admin_settings.view_icons(ctx, slot) for slot in "ocw"]
        screens += [admin_settings._text_view(i) for i in range(len(live_settings.TEXTS))]
        for text, markup in screens:
            shown = _all_text(text, markup)
            assert "‹" not in shown, shown

        tickets, _ = await admin_settings.view_tickets(ctx)
        assert "❓" in tickets and "✅" in tickets and "❗" in tickets
        field, markup = admin_settings.view_field("ai.reply_delay_sec")
        assert "30 с" in field
        assert any("Как в .env" in b.text for row in markup.inline_keyboard for b in row)
        await engine.dispose()

    asyncio.run(run())


# ── Проверка ИИ ─────────────────────────────────────────────────────────


def test_check_ai_reports_each_model(tmp_path) -> None:
    from app.services.ai.provider import AiError, AiRateLimited, check_models

    class Backend:
        def __init__(self, provider):
            self.provider = provider

        async def reply(self, model, system, turns):
            if model == "busy":
                raise AiRateLimited("429 RESOURCE_EXHAUSTED")
            if model == "slow":
                await asyncio.sleep(5)
            if model == "bad":
                raise AiError("401 invalid api key")
            return AiReply(text="на месте")

    ai = replace(
        _settings(tmp_path).ai,
        models=("gemini:busy", "claude:ok", "gemini:slow", "openai:nokey", "gemini:bad"),
        api_keys=(("gemini", "k"), ("claude", "k")),
        proxy_url="http://user:secret@proxy.example:3128",
    )
    chain = ModelChain(ai, lambda provider, _s: Backend(provider))
    results = asyncio.run(check_models(chain, timeout_sec=0.2))
    assert [r.status for r in results] == ["limit", "ok", "timeout", "no_key", "error"]
    assert results[1].detail == "на месте"

    text, markup = admin_settings._check_view(chain, results, ai_enabled=False)
    assert "‹" not in text
    assert "proxy.example:3128" in text and "secret" not in text
    assert "<code>claude:ok</code>" in text and "резервная" in text
    assert "неверный ключ" in text
    assert [b.callback_data for row in markup.inline_keyboard for b in row] == ["st:ck", "st:go:ai"]
