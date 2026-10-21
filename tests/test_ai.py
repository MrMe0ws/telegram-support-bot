from __future__ import annotations

import asyncio
import io
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
from app.services.ai.provider import AiError, AiReply  # noqa: E402
from app.services.ai.providers.gemini import parse_gemini_response  # noqa: E402
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
    assert turns[2].text == "[клиент прислал фото — вложение недоступно]"


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

    # Файлы Telegram: file_id → (путь на серверах Telegram, содержимое).
    files: dict[str, tuple[str, bytes]] = {}
    downloads = 0

    async def get_file(self, file_id):
        path, data = self.files[file_id]
        return SimpleNamespace(file_id=file_id, file_path=path, file_size=len(data))

    async def download_file(self, file_path):
        self.downloads += 1
        data = next(d for p, d in self.files.values() if p == file_path)
        return io.BytesIO(data)

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
        enabled=True, mode=mode, provider="gemini", models=("gemini:m",), api_keys=(("gemini", "k"),),
        proxy_url="",
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


# ── Вложения: фото, голосовые, файлы ────────────────────────────────────


async def _client_sends(sf, ticket_id: int, content_type: str, file_id: str, text: str | None = None) -> None:
    async with sf() as session:
        async with session.begin():
            await ticket_svc.record_message(
                session, ticket_id=ticket_id, direction="in", content_type=content_type,
                text=text, telegram_file_id=file_id, telegram_message_id=None,
            )


def test_photo_and_voice_reach_model(tmp_path) -> None:
    async def scenario():
        svc, sf, bot, provider, tid, engine = await _setup(tmp_path, AiReply(text="вижу ошибку"))
        bot.files = {
            "ph": ("photos/file_1.jpg", b"jpeg-bytes"),
            "vc": ("voice/file_2.oga", b"ogg-bytes"),
            "vid": ("videos/file_3.mp4", b"mp4-bytes"),
        }
        await _client_sends(sf, tid, "photo", "ph", text="вот скрин")
        await _client_sends(sf, tid, "voice", "vc")
        await _client_sends(sf, tid, "video", "vid")
        svc.schedule(tid)
        await _wait(svc)

        turn = provider.calls[0][-1]
        assert [(m.mime_type, m.data) for m in turn.media] == [
            ("image/jpeg", b"jpeg-bytes"),
            ("audio/ogg", b"ogg-bytes"),
        ]
        assert "[клиент прислал фото — см. вложение] вот скрин" in turn.text
        assert "[клиент прислал голосовое сообщение — см. вложение]" in turn.text
        # Видео модели не отправляется — только пометка.
        assert "[клиент прислал видео — вложение недоступно]" in turn.text
        assert bot.to_client() == ["вижу ошибку"]
        await engine.dispose()

    asyncio.run(scenario())


def test_oversized_and_unknown_files_are_not_sent(tmp_path) -> None:
    async def scenario():
        svc, sf, bot, provider, tid, engine = await _setup(tmp_path, AiReply(text="ок"))
        svc.apply_settings(replace(svc._settings, ai=replace(svc._ai, media_max_mb=1)))
        bot.files = {
            "big": ("documents/file_1.pdf", b"x" * (1024 * 1024 + 1)),
            "zip": ("documents/file_2.zip", b"zip"),
            "log": ("documents/file_3.log", b"error: timeout"),
        }
        await _client_sends(sf, tid, "document", "big", text="отчёт.pdf")
        await _client_sends(sf, tid, "document", "zip", text="архив.zip")
        await _client_sends(sf, tid, "document", "log", text="happ.log")
        svc.schedule(tid)
        await _wait(svc)

        turn = provider.calls[0][-1]
        assert [(m.mime_type, m.data) for m in turn.media] == [("text/plain", b"error: timeout")]
        assert "файл — вложение недоступно] отчёт.pdf" in turn.text
        assert "файл — вложение недоступно] архив.zip" in turn.text
        assert "файл — см. вложение] happ.log" in turn.text
        await engine.dispose()

    asyncio.run(scenario())


def test_media_disabled_downloads_nothing(tmp_path) -> None:
    async def scenario():
        svc, sf, bot, provider, tid, engine = await _setup(tmp_path, AiReply(text="ок"))
        svc.apply_settings(replace(svc._settings, ai=replace(svc._ai, media_enabled=False)))
        bot.files = {"ph": ("photos/file_1.jpg", b"jpeg")}
        await _client_sends(sf, tid, "photo", "ph")
        svc.schedule(tid)
        await _wait(svc)

        assert bot.downloads == 0
        assert provider.calls[0][-1].media == ()
        assert "вложение недоступно" in provider.calls[0][-1].text
        await engine.dispose()

    asyncio.run(scenario())


def test_gemini_request_carries_media(tmp_path) -> None:
    from app.services.ai.media import Media
    from app.services.ai.providers.gemini import GeminiBackend

    contents = GeminiBackend("k", "", 10)._contents(
        [Turn("user", "смотри", (Media("image/png", b"png"),)), Turn("assistant", "вижу")]
    )
    assert [c.role for c in contents] == ["user", "model"]
    assert contents[0].parts[0].text == "смотри"
    assert contents[0].parts[1].inline_data.mime_type == "image/png"
    assert contents[0].parts[1].inline_data.data == b"png"
    assert len(contents[1].parts) == 1


def test_photo_caption_keeps_file() -> None:
    from app.handlers.private_chat import _content_summary

    empty = dict(
        text=None, caption=None, photo=None, document=None, video=None, audio=None,
        voice=None, video_note=None, sticker=None, contact=None, content_type="photo",
    )
    photo = SimpleNamespace(**{
        **empty,
        "caption": "не подключается",
        "photo": [SimpleNamespace(file_id="small"), SimpleNamespace(file_id="big")],
    })
    assert _content_summary(photo) == ("photo", "не подключается", "big")
    gif = SimpleNamespace(**{**empty, "caption": "смешно", "content_type": "animation"})
    assert _content_summary(gif) == ("animation", "смешно", None)


# ── Кнопка «Позвать человека» ───────────────────────────────────────────


async def _two_ai_replies(tmp_path, button_from: int) -> list:
    svc, sf, bot, provider, tid, engine = await _setup(tmp_path, AiReply(text="ответ"))
    svc.apply_settings(replace(svc._settings, ai=replace(svc._ai, human_button_from=button_from)))
    for question in ("первый вопрос", "второй вопрос"):
        await _client_says(sf, tid, question)
        svc.schedule(tid)
        await _wait(svc)
    await engine.dispose()
    return [m.get("reply_markup") for m in bot.sent if m["chat_id"] == CLIENT_ID]


def test_human_button_from_second_reply(tmp_path) -> None:
    first, second = asyncio.run(_two_ai_replies(tmp_path, 2))
    assert first is None and second is not None


def test_human_button_disabled(tmp_path) -> None:
    assert asyncio.run(_two_ai_replies(tmp_path, 0)) == [None, None]


def test_human_button_default_from_first_reply(tmp_path) -> None:
    first, second = asyncio.run(_two_ai_replies(tmp_path, 1))
    assert first is not None and second is not None
