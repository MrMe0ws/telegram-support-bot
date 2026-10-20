from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Protocol

import aiohttp
import httpx

from app.config import AiSettings
from app.services.ai.prompt import Turn

log = logging.getLogger(__name__)

ESCALATE_TOOL = "escalate_to_human"
ESCALATE_DESCRIPTION = (
    "Передать тикет живому сотруднику поддержки. Вызывай, когда клиент просит человека, "
    "когда ответа нет в базе знаний или ты не уверен, когда нужны действия с подпиской "
    "или деньгами, а также при жалобах и проблемах с оплатой."
)
ESCALATE_PARAMS = {
    "reason": "Коротко, почему нужен человек (для сотрудника).",
    "summary": "Суть обращения клиента в 1–3 предложениях (для сотрудника).",
}


@dataclass(frozen=True)
class AiReply:
    """Итог одного обращения к модели: либо текст клиенту, либо передача человеку."""

    text: str | None = None
    escalate_reason: str | None = None
    escalate_summary: str | None = None

    @property
    def escalate(self) -> bool:
        return self.escalate_reason is not None


class AiError(Exception):
    """Модель не дала ответа: сеть, ключ, пустой или заблокированный ответ."""


class AiRateLimited(AiError):
    """Провайдер отказал по лимиту запросов (на бесплатном тарифе — обычное дело)."""


class AiProvider(Protocol):
    async def reply(self, system: str, turns: list[Turn]) -> AiReply: ...


class GeminiProvider:
    """Google Gemini через официальный SDK google-genai (ключ из Google AI Studio)."""

    def __init__(self, settings: AiSettings) -> None:
        from google import genai
        from google.genai import types

        self._types = types
        self._model = settings.model
        self._timeout = settings.timeout_sec
        http_options = None
        if settings.proxy_url:
            # SDK ходит в API через aiohttp/httpx: оба принимают proxy на запрос.
            http_options = types.HttpOptions(async_client_args={"proxy": settings.proxy_url})
        self._client = genai.Client(api_key=settings.api_key, http_options=http_options)
        self._tool = types.Tool(
            function_declarations=[
                types.FunctionDeclaration(
                    name=ESCALATE_TOOL,
                    description=ESCALATE_DESCRIPTION,
                    parameters=types.Schema(
                        type=types.Type.OBJECT,
                        properties={
                            name: types.Schema(type=types.Type.STRING, description=desc)
                            for name, desc in ESCALATE_PARAMS.items()
                        },
                        required=list(ESCALATE_PARAMS),
                    ),
                )
            ]
        )

    def _contents(self, turns: list[Turn]) -> list:
        t = self._types
        return [
            t.Content(role="model" if turn.role == "assistant" else "user", parts=[t.Part(text=turn.text)])
            for turn in turns
        ]

    async def reply(self, system: str, turns: list[Turn]) -> AiReply:
        from google.genai import errors

        t = self._types
        config = t.GenerateContentConfig(
            system_instruction=system,
            tools=[self._tool],
            # Инструмент только сигнализирует «нужен человек» — исполнять его
            # силами SDK нечего, решение принимает бот.
            automatic_function_calling=t.AutomaticFunctionCallingConfig(disable=True),
            temperature=0.3,
        )
        try:
            resp = await asyncio.wait_for(
                self._client.aio.models.generate_content(
                    model=self._model,
                    contents=self._contents(turns),
                    config=config,
                ),
                timeout=self._timeout,
            )
        except asyncio.TimeoutError as e:
            raise AiError(f"нет ответа за {self._timeout:.0f} с") from e
        except errors.APIError as e:
            if e.code == 429:
                raise AiRateLimited(e.message or "429") from e
            raise AiError(f"{e.code} {e.status or ''} {e.message or ''}".strip()) from e
        except (aiohttp.ClientError, httpx.HTTPError, OSError) as e:
            # SDK ходит в сеть через aiohttp или httpx — что установлено.
            raise AiError(f"нет связи с API: {e}") from e

        usage = resp.usage_metadata
        if usage is not None:
            log.info(
                "Gemini %s: вход %s (из кэша %s), выход %s, размышления %s",
                self._model,
                usage.prompt_token_count,
                usage.cached_content_token_count,
                usage.candidates_token_count,
                usage.thoughts_token_count,
            )
        return parse_gemini_response(resp)


def parse_gemini_response(resp) -> AiReply:
    """Разбор ответа Gemini: вызов escalate_to_human важнее текста рядом с ним."""
    for call in resp.function_calls or []:
        if call.name == ESCALATE_TOOL:
            args = call.args or {}
            return AiReply(
                escalate_reason=str(args.get("reason") or "").strip() or "без причины",
                escalate_summary=str(args.get("summary") or "").strip() or None,
            )

    candidates = resp.candidates or []
    if not candidates:
        feedback = getattr(resp, "prompt_feedback", None)
        raise AiError(f"пустой ответ модели ({feedback})" if feedback else "пустой ответ модели")
    content = candidates[0].content
    parts = (content.parts if content else None) or []
    text = "".join(p.text for p in parts if p.text and not p.thought).strip()
    if not text:
        raise AiError(f"модель не дала текста (finish_reason={candidates[0].finish_reason})")
    return AiReply(text=text)


def create_provider(settings: AiSettings) -> AiProvider:
    if settings.provider == "gemini":
        return GeminiProvider(settings)
    raise ValueError(f"AI_PROVIDER={settings.provider!r} не поддерживается (доступно: gemini)")
