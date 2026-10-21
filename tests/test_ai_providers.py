from __future__ import annotations

import asyncio
import json
import os
from types import SimpleNamespace

import pytest

os.environ.setdefault("TEXTS_DIR", os.path.join(os.path.dirname(__file__), "..", "texts"))

from app.config import _ai_from_env, parse_ai_model, parse_ai_models  # noqa: E402
from app.db.session import create_engine, init_db, session_factory  # noqa: E402
from app.services.ai import model_choice  # noqa: E402
from app.services.ai.media import Media  # noqa: E402
from app.services.ai.prompt import Turn  # noqa: E402
from app.services.ai.provider import AiError, AiReply, ModelChain  # noqa: E402
from app.services.ai.providers.claude import (  # noqa: E402
    build_claude_messages,
    parse_claude_response,
    supports_effort,
)
from app.services.ai.providers.openai import build_openai_input, parse_openai_response  # noqa: E402

TURNS = [
    Turn("user", "смотри", (Media("image/png", b"png"), Media("application/pdf", b"%PDF"), Media("audio/ogg", b"x"))),
    Turn("assistant", "вижу"),
]


# ── Настройки моделей ───────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("gemini-2.5-flash", "gemini:gemini-2.5-flash"),
        ("gpt-6-luna", "openai:gpt-6-luna"),
        ("claude-sonnet-5-5", "claude:claude-sonnet-5-5"),
        (" Anthropic : claude-haiku-4-5 ", "claude:claude-haiku-4-5"),
        ("openai:my-finetune", "openai:my-finetune"),
        ("my-model", "gemini:my-model"),
    ],
)
def test_parse_ai_model(raw, expected) -> None:
    assert parse_ai_model(raw, "gemini") == expected


def test_parse_ai_models_list() -> None:
    assert parse_ai_models("claude-opus-5-5, gpt-6-luna,, claude-opus-5-5,gemini-2.5-flash", "gemini") == (
        "claude:claude-opus-5-5",
        "openai:gpt-6-luna",
        "gemini:gemini-2.5-flash",
    )
    with pytest.raises(ValueError):
        parse_ai_models("mistral:large", "gemini")


def test_env_models_and_keys(monkeypatch) -> None:
    for k in ("AI_MODEL", "AI_FALLBACK_MODELS", "AI_FALLBACK_MODEL", "AI_API_KEY", "GEMINI_API_KEY", "OPENAI_API_KEY"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("AI_PROVIDER", "claude")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "ak")
    monkeypatch.setenv("OPENAI_API_KEY", "ok")
    monkeypatch.setenv("AI_FALLBACK_MODELS", "gpt-6-luna, gemini-2.5-flash")
    ai = _ai_from_env()
    assert ai.models == ("claude:claude-opus-5-5", "openai:gpt-6-luna", "gemini:gemini-2.5-flash")
    assert ai.api_key("claude") == "ak" and not ai.has_key("gemini:gemini-2.5-flash")


def test_env_legacy_gemini(monkeypatch) -> None:
    for k in ("AI_PROVIDER", "AI_MODEL", "AI_FALLBACK_MODELS", "AI_FALLBACK_MODEL", "GEMINI_API_KEY"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("AI_API_KEY", "g")
    ai = _ai_from_env()
    assert ai.models == ("gemini:gemini-2.5-flash", "gemini:gemini-3.5-flash-lite")
    assert ai.api_key("gemini") == "g"


# ── OpenAI ──────────────────────────────────────────────────────────────


def test_openai_input_media() -> None:
    items = build_openai_input(TURNS)
    user, assistant = items
    assert user["role"] == "user" and assistant == {"role": "assistant", "content": "вижу"}
    types = [p["type"] for p in user["content"]]
    assert types == ["input_text", "input_image", "input_file", "input_text"]
    assert user["content"][1]["image_url"].startswith("data:image/png;base64,")
    assert "недоступно" in user["content"][3]["text"]


def _oa_message(text):
    return SimpleNamespace(type="message", content=[SimpleNamespace(type="output_text", text=text)])


def test_openai_parse() -> None:
    resp = SimpleNamespace(output=[SimpleNamespace(type="reasoning"), _oa_message("Откройте приложение")], status="completed")
    assert parse_openai_response(resp) == AiReply(text="Откройте приложение")

    call = SimpleNamespace(
        type="function_call", name="escalate_to_human",
        arguments=json.dumps({"reason": "просит человека", "summary": "оплата"}),
    )
    reply = parse_openai_response(SimpleNamespace(output=[_oa_message("зову"), call], status="completed"))
    assert reply.escalate and reply.escalate_reason == "просит человека"

    with pytest.raises(AiError):
        parse_openai_response(SimpleNamespace(output=[], status="incomplete", incomplete_details="max_output_tokens"))


# ── Claude ──────────────────────────────────────────────────────────────


def test_claude_messages_media() -> None:
    user, assistant = build_claude_messages(TURNS)
    assert assistant == {"role": "assistant", "content": "вижу"}
    assert [b["type"] for b in user["content"]] == ["image", "document", "text", "text"]
    assert user["content"][0]["source"]["media_type"] == "image/png"
    assert "недоступно" in user["content"][2]["text"]
    assert user["content"][3]["text"] == "смотри"


def test_claude_parse() -> None:
    text = SimpleNamespace(type="text", text="Готово")
    assert parse_claude_response(SimpleNamespace(stop_reason="end_turn", content=[text])) == AiReply(text="Готово")

    tool = SimpleNamespace(type="tool_use", name="escalate_to_human", input={"reason": "возврат"})
    reply = parse_claude_response(SimpleNamespace(stop_reason="tool_use", content=[text, tool]))
    assert reply.escalate and reply.escalate_reason == "возврат" and reply.escalate_summary is None

    with pytest.raises(AiError):
        parse_claude_response(SimpleNamespace(stop_reason="refusal", stop_details=None, content=[]))


@pytest.mark.parametrize(
    ("model", "ok"),
    [("claude-opus-5-5", True), ("claude-sonnet-5", True), ("claude-sonnet-4-6", True),
     ("claude-sonnet-4-5", False), ("claude-haiku-4-5", False)],
)
def test_claude_effort_support(model, ok) -> None:
    assert supports_effort(model) is ok


# ── Выбор моделей в боте ────────────────────────────────────────────────


def test_model_choice_persists(tmp_path) -> None:
    from dataclasses import replace

    from tests.test_ai import _settings

    async def run():
        engine = create_engine(f"sqlite+aiosqlite:///{tmp_path / 'db.sqlite'}")
        await init_db(engine)
        sf = session_factory(engine)
        ai = replace(
            _settings(tmp_path).ai,
            models=("gemini:gemini-2.5-flash", "claude:claude-sonnet-5-5"),
            api_keys=(("gemini", "g"), ("claude", "c")),
        )
        chain = ModelChain(ai, lambda p, s: None)
        await model_choice.save(sf, chain, ("claude:claude-sonnet-5-5", "gemini:gemini-2.5-flash"))
        assert model_choice.is_overridden(chain)

        fresh = ModelChain(ai, lambda p, s: None)
        await model_choice.load_saved(sf, fresh)
        assert fresh.models == ("claude:claude-sonnet-5-5", "gemini:gemini-2.5-flash")

        await model_choice.reset(sf, fresh)
        assert fresh.models == ai.models
        again = ModelChain(ai, lambda p, s: None)
        await model_choice.load_saved(sf, again)
        assert again.models == ai.models
        await engine.dispose()

    asyncio.run(run())
