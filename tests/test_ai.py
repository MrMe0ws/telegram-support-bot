from __future__ import annotations

import asyncio
import os
from dataclasses import replace
from types import SimpleNamespace

import pytest
from google.genai import types

os.environ.setdefault("TEXTS_DIR", os.path.join(os.path.dirname(__file__), "..", "texts"))

from app.config import AiSettings, RemnawaveSettings, Settings  # noqa: E402
from app.db.models import AiDraft, MessageSource, StoredMessage, Ticket  # noqa: E402
from app.db.session import create_engine, init_db, session_factory  # noqa: E402
from app.services import reaction_bridge as reaction_bridge_svc  # noqa: E402
from app.services import tickets as ticket_svc  # noqa: E402
from app.services.ai.knowledge import KnowledgeLoader  # noqa: E402
from app.services.ai.prompt import AI_NOTE, Turn, build_turns  # noqa: E402
from app.services.ai.provider import AiError, AiReply, parse_gemini_response  # noqa: E402
from app.services.ai.service import AiService  # noqa: E402


# ── База знаний ─────────────────────────────────────────────────────────


def test_knowledge_loads_md_recursively_and_reloads(tmp_path) -> None:
    (tmp_path / "a.md").write_text("Тарифы: 100 ₽", encoding="utf-8")
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "b.md").write_text("Как подключить iPhone", encoding="utf-8")
    (tmp_path / "notes.txt").write_text("не база", encoding="utf-8")
    (tmp_path / "empty.md").write_text("   ", encoding="utf-8")

    loader = KnowledgeLoader(str(tmp_path))
    kb = loader.load()
    assert kb.files == ("a.md", "sub/b.md")
    assert "Тарифы: 100 ₽" in kb.text and "не база" not in kb.text

    # Правка файла подхватывается без перезапуска.
    (tmp_path / "a.md").write_text("Тарифы: 200 ₽ — новая цена", encoding="utf-8")
    assert "200 ₽" in loader.load().text


def test_knowledge_missing_dir_is_empty(tmp_path) -> None:
    kb = KnowledgeLoader(str(tmp_path / "nope")).load()
    assert kb.text == "" and kb.files == ()


# ── История для модели ──────────────────────────────────────────────────


def _msg(direction: str, text: str | None, *, note: str | None = None, ct: str = "text"):
    return SimpleNamespace(direction=direction, text=text, raw_note=note, content_type=ct)


def test_build_turns_merges_roles_and_marks_staff() -> None:
    turns = build_turns(
        "Статус подписки: ACTIVE",
        [
            _msg("in", "Привет"),
            _msg("in", "не работает VPN"),
            _msg("out", "Перезапустите приложение", note=AI_NOTE),
            _msg("out", "Проверил — всё ок"),
            _msg("in", None, ct="photo"),
        ],
    )
    # Справка о клиенте и два первых сообщения клиента — одна реплика user.
    assert [t.role for t in turns] == ["user", "assistant", "user"]
    assert "Служебная справка" in turns[0].text
    assert "Привет" in turns[0].text and "не работает VPN" in turns[0].text
    assert "Перезапустите приложение" in turns[1].text
    assert "[Поддержка] Проверил — всё ок" in turns[1].text
    assert "[Поддержка] Перезапустите" not in turns[1].text
    assert turns[2].text == "[клиент прислал фото]"


# ── Разбор ответа Gemini ────────────────────────────────────────────────


def _resp(parts) -> types.GenerateContentResponse:
    return types.GenerateContentResponse(
        candidates=[types.Candidate(content=types.Content(role="model", parts=parts))]
    )


def test_parse_text_skips_thoughts() -> None:
    reply = parse_gemini_response(
        _resp([types.Part(text="думаю…", thought=True), types.Part(text="Откройте приложение")])
    )
    assert reply == AiReply(text="Откройте приложение")


def test_parse_escalation_wins_over_text() -> None:
    reply = parse_gemini_response(
        _resp(
            [
                types.Part(text="Сейчас позову"),
                types.Part(
                    function_call=types.FunctionCall(
                        name="escalate_to_human",
                        args={"reason": "просит человека", "summary": "не проходит оплата"},
                    )
                ),
            ]
        )
    )
    assert reply.escalate
    assert reply.escalate_reason == "просит человека"
    assert reply.escalate_summary == "не проходит оплата"


def test_parse_empty_raises() -> None:
    with pytest.raises(AiError):
        parse_gemini_response(types.GenerateContentResponse(candidates=[]))


# ── Сервис на настоящей SQLite ──────────────────────────────────────────

GROUP_ID = -1001
CLIENT_ID = 555


class FakeBot:
    def __init__(self) -> None:
        self.sent: list[dict] = []
        self.edited: list[dict] = []
        self.typing = 0
        self._next_id = 1000

    async def send_message(self, chat_id, text, **kw):
        self._next_id += 1
        self.sent.append({"chat_id": chat_id, "text": text, "message_id": self._next_id, **kw})
        return SimpleNamespace(message_id=self._next_id)

    async def send_chat_action(self, chat_id, action):
        self.typing += 1

    async def edit_message_text(self, chat_id, message_id, text, **kw):
        self.edited.append({"chat_id": chat_id, "message_id": message_id, "text": text})

    def to_client(self) -> list[str]:
        return [m["text"] for m in self.sent if m["chat_id"] == CLIENT_ID]

    def to_topic(self) -> list[str]:
        return [m["text"] for m in self.sent if m["chat_id"] == GROUP_ID]


class FakeProvider:
    def __init__(self, reply: AiReply) -> None:
        self.reply_value = reply
        self.calls: list[list[Turn]] = []

    async def reply(self, system: str, turns: list[Turn]) -> AiReply:
        self.calls.append(turns)
        return self.reply_value


def _settings(tmp_path, mode: str = "auto", delay: float = 0.05) -> Settings:
    rw = RemnawaveSettings(
        enabled=False, base_url="", mode="local", token="", sub_public_url="",
        gate_query_name="", gate_query_value="", gate_cookie="", timeout_sec=5,
        timezone="UTC", http_host="", proxy_headers=False, forwarded_for="", forwarded_proto="",
    )
    ai = AiSettings(
        enabled=True, mode=mode, provider="gemini", api_key="k", model="m", proxy_url="",
        reply_delay_sec=delay, knowledge_dir=str(tmp_path / "kb"), history_limit=30,
        max_replies_per_ticket=3, timeout_sec=10, assistant_name="Бот",
    )
    return Settings(
        bot_token="t", support_group_id=GROUP_ID, admin_ids=frozenset({1}),
        database_url="", topic_name_template="{username}", topic_icon_emoji_open="",
        topic_icon_emoji_closed="", shop_webhook_url="", support_bridge_secret="",
        http_port=0, remnawave=rw, ai=ai,
    )


async def _setup(tmp_path, reply: AiReply, mode: str = "auto"):
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path / 'db.sqlite'}")
    await init_db(engine)
    sf = session_factory(engine)
    async with sf() as session:
        async with session.begin():
            t = await ticket_svc.create_ticket(
                session, user_id=CLIENT_ID, forum_chat_id=GROUP_ID, thread_id=7,
                source=MessageSource.telegram.value,
            )
            ticket_id = t.id
    bot = FakeBot()
    provider = FakeProvider(reply)
    settings = _settings(tmp_path, mode)
    svc = AiService(bot, sf, settings, provider, KnowledgeLoader(settings.ai.knowledge_dir))
    return svc, sf, bot, provider, ticket_id, engine


async def _client_says(sf, ticket_id: int, text: str) -> None:
    async with sf() as session:
        async with session.begin():
            await ticket_svc.record_message(
                session, ticket_id=ticket_id, direction="in", content_type="text",
                text=text, telegram_file_id=None, telegram_message_id=None,
            )


async def _wait(svc: AiService) -> None:
    for _ in range(100):
        if not svc._tasks:
            return
        await asyncio.sleep(0.02)
    raise AssertionError("ответ ИИ не завершился")


def test_auto_reply_debounced_and_recorded(tmp_path) -> None:
    async def scenario():
        svc, sf, bot, provider, tid, engine = await _setup(tmp_path, AiReply(text="Нажмите **Обновить** <app> & ok"))
        await _client_says(sf, tid, "привет")
        svc.schedule(tid)
        await _client_says(sf, tid, "не работает")
        svc.schedule(tid)  # второе сообщение до ответа — ждём заново, отвечаем один раз
        await _wait(svc)

        assert len(provider.calls) == 1
        assert "привет" in provider.calls[0][0].text and "не работает" in provider.calls[0][0].text
        # В Telegram разметка модели становится HTML, а «<» и «&» экранируются.
        assert bot.to_client() == ["Нажмите <b>Обновить</b> &lt;app&gt; &amp; ok"]
        client_msg = next(m for m in bot.sent if m["chat_id"] == CLIENT_ID)
        assert client_msg["parse_mode"] == "HTML"
        assert bot.typing >= 1
        assert any("<b>Обновить</b> &lt;app&gt;" in t for t in bot.to_topic())

        async with sf() as session:
            rows = (await session.execute(StoredMessage.__table__.select())).fetchall()
        assert [r.raw_note for r in rows if r.direction == "out"] == [AI_NOTE]
        await engine.dispose()

    asyncio.run(scenario())


def test_client_reaction_on_ai_reply_reaches_topic(tmp_path) -> None:
    async def scenario():
        svc, sf, bot, provider, tid, engine = await _setup(tmp_path, AiReply(text="ответ"))
        await _client_says(sf, tid, "вопрос")
        svc.schedule(tid)
        await _wait(svc)

        topic_copy = next(m for m in bot.sent if m["chat_id"] == GROUP_ID)
        dm = next(m for m in bot.sent if m["chat_id"] == CLIENT_ID)
        async with sf() as session:
            resolved = await reaction_bridge_svc.resolve_staff_message(
                session, user_id=CLIENT_ID, dm_message_id=dm["message_id"]
            )
        assert resolved == (GROUP_ID, topic_copy["message_id"])
        await engine.dispose()

    asyncio.run(scenario())


def test_staff_reply_pauses_ai(tmp_path) -> None:
    async def scenario():
        svc, sf, bot, provider, tid, engine = await _setup(tmp_path, AiReply(text="ответ"))
        await _client_says(sf, tid, "вопрос")
        svc.schedule(tid)
        await svc.pause_by_staff(tid)  # сотрудник ответил раньше ИИ
        await asyncio.sleep(0.15)
        assert provider.calls == [] and bot.to_client() == []

        await _client_says(sf, tid, "ещё вопрос")
        svc.schedule(tid)
        await _wait(svc)
        assert provider.calls == []  # тикет у человека — ИИ молчит

        await svc.set_enabled_in_ticket(tid, True)  # /ai on
        await _wait(svc)
        assert bot.to_client() == ["ответ"]
        await engine.dispose()

    asyncio.run(scenario())


def test_ai_escalation_pauses_and_notifies(tmp_path) -> None:
    async def scenario():
        svc, sf, bot, provider, tid, engine = await _setup(
            tmp_path, AiReply(escalate_reason="просит человека", escalate_summary="хочет возврат")
        )
        await _client_says(sf, tid, "позовите человека")
        svc.schedule(tid)
        await _wait(svc)

        assert any("Нужен человек" in t and "хочет возврат" in t for t in bot.to_topic())
        assert len(bot.to_client()) == 1 and "специалисту" in bot.to_client()[0]
        async with sf() as session:
            assert (await session.get(Ticket, tid)).ai_paused
        # Повторная эскалация (кнопка клиента) уже ничего не меняет.
        assert await svc.escalate(tid, initiator="client") is False
        await engine.dispose()

    asyncio.run(scenario())


def test_reply_limit_hands_over(tmp_path) -> None:
    async def scenario():
        svc, sf, bot, provider, tid, engine = await _setup(tmp_path, AiReply(text="ответ"))
        for i in range(3):  # max_replies_per_ticket=3
            await _client_says(sf, tid, f"вопрос {i}")
            svc.schedule(tid)
            await _wait(svc)
        await _client_says(sf, tid, "вопрос 4")
        svc.schedule(tid)
        await _wait(svc)
        assert len(provider.calls) == 3
        assert any("ответил уже 3 раз" in t for t in bot.to_topic())
        await engine.dispose()

    asyncio.run(scenario())


def test_draft_mode_waits_for_admin(tmp_path) -> None:
    async def scenario():
        svc, sf, bot, provider, tid, engine = await _setup(tmp_path, AiReply(text="черновик"), mode="draft")
        await _client_says(sf, tid, "вопрос")
        svc.schedule(tid)
        await _wait(svc)

        assert bot.to_client() == [] and bot.typing == 0
        draft_msg = next(m for m in bot.sent if m["chat_id"] == GROUP_ID)
        assert "Черновик" in draft_msg["text"] and draft_msg["reply_markup"] is not None
        async with sf() as session:
            draft = (await session.execute(AiDraft.__table__.select())).fetchone()

        assert "Отправлено" in await svc.resolve_draft(draft.id, send=True, admin_label="@admin")
        assert bot.to_client() == ["черновик"]
        # Второе нажатие не шлёт дубль.
        assert "обработан" in await svc.resolve_draft(draft.id, send=True, admin_label="@admin")
        assert bot.to_client() == ["черновик"]
        await engine.dispose()

    asyncio.run(scenario())


def test_new_draft_makes_old_one_stale(tmp_path) -> None:
    async def scenario():
        svc, sf, bot, provider, tid, engine = await _setup(tmp_path, AiReply(text="первый"), mode="draft")
        await _client_says(sf, tid, "вопрос")
        svc.schedule(tid)
        await _wait(svc)
        provider.reply_value = replace(provider.reply_value, text="второй")
        await _client_says(sf, tid, "уточнение")
        svc.schedule(tid)
        await _wait(svc)

        async with sf() as session:
            rows = (await session.execute(AiDraft.__table__.select().order_by(AiDraft.id))).fetchall()
        assert [r.status for r in rows] == ["stale", "pending"]
        assert any("устарел" in e["text"] for e in bot.edited)
        assert "обработан" in await svc.resolve_draft(rows[0].id, send=True, admin_label="@a")
        await engine.dispose()

    asyncio.run(scenario())


def test_message_during_generation_restarts_with_full_history(tmp_path) -> None:
    async def scenario():
        svc, sf, bot, provider, tid, engine = await _setup(tmp_path, AiReply(text="ответ"))
        original = provider.reply

        async def slow_reply(system, turns):
            result = await original(system, turns)
            if len(provider.calls) == 1:
                # Клиент дописал, пока модель думала над первым сообщением.
                await _client_says(sf, tid, "и ещё деталь")
                svc.schedule(tid)
            return result

        provider.reply = slow_reply
        await _client_says(sf, tid, "вопрос")
        svc.schedule(tid)
        await _wait(svc)

        assert len(provider.calls) == 2
        assert "и ещё деталь" in provider.calls[1][-1].text
        assert bot.to_client() == ["ответ"]  # неполный первый ответ не ушёл
        await engine.dispose()

    asyncio.run(scenario())


def test_draft_mode_keeps_drafting_after_staff_reply(tmp_path) -> None:
    async def scenario():
        svc, sf, bot, provider, tid, engine = await _setup(tmp_path, AiReply(text="черновик"), mode="draft")
        await _client_says(sf, tid, "вопрос")
        svc.schedule(tid)
        await _wait(svc)
        await svc.pause_by_staff(tid)  # админ ответил сам

        async with sf() as session:
            assert not (await session.get(Ticket, tid)).ai_paused
            statuses = [r.status for r in (await session.execute(AiDraft.__table__.select())).fetchall()]
        assert statuses == ["stale"]

        await _client_says(sf, tid, "ещё вопрос")
        svc.schedule(tid)
        await _wait(svc)
        assert len(provider.calls) == 2
        await engine.dispose()

    asyncio.run(scenario())



# ── Оформление под канал ────────────────────────────────────────────────

MODEL_REPLY = (
    "Есть два варианта:\n\n"
    "1.  **Поделиться подпиской:**\n"
    "    *   займёт одно из ваших устройств;\n"
    "    *   **Подписка → Подключить доп. устройство**.\n\n\n"
    "## Итог\n"
    "Ключ: `vless://a_b*c`, инструкция [тут](https://example.com/a_b?x=1&y=2). "
    "Тариф 2*3 < 10 & дешевле."
)


def test_telegram_html_renders_markdown() -> None:
    from app.services.ai.formatting import to_telegram_html

    assert to_telegram_html(MODEL_REPLY) == (
        "Есть два варианта:\n\n"
        "1. <b>Поделиться подпиской:</b>\n"
        "• займёт одно из ваших устройств;\n"
        "• <b>Подписка → Подключить доп. устройство</b>.\n\n"
        "<b>Итог</b>\n"
        "Ключ: <code>vless://a_b*c</code>, инструкция "
        '<a href="https://example.com/a_b?x=1&amp;y=2">тут</a>. '
        "Тариф 2*3 &lt; 10 &amp; дешевле."
    )


def test_plain_text_for_cabinet() -> None:
    from app.services.ai.formatting import to_plain_text

    assert to_plain_text(MODEL_REPLY) == (
        "Есть два варианта:\n\n"
        "1. Поделиться подпиской:\n"
        "• займёт одно из ваших устройств;\n"
        "• Подписка → Подключить доп. устройство.\n\n"
        "Итог\n"
        "Ключ: vless://a_b*c, инструкция тут (https://example.com/a_b?x=1&y=2). "
        "Тариф 2*3 < 10 & дешевле."
    )


def test_table_becomes_lines() -> None:
    from app.services.ai.formatting import to_plain_text

    table = "| Тариф | Цена |\n|---|---|\n| 1 мес | 100 ₽ |"
    assert to_plain_text(table) == "Тариф · Цена\n1 мес · 100 ₽"
