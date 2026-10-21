from __future__ import annotations

import asyncio
import logging

import aiohttp
import httpx

from app.services.ai.prompt import Turn
from app.services.ai.provider import (
    ESCALATE_DESCRIPTION,
    ESCALATE_PARAMS,
    ESCALATE_TOOL,
    AiError,
    AiRateLimited,
    AiReply,
    AiUnavailable,
    escalation_reply,
)

log = logging.getLogger(__name__)


class GeminiBackend:
    """Google Gemini через официальный SDK google-genai (ключ из Google AI Studio)."""

    def __init__(self, api_key: str, proxy_url: str, timeout_sec: float) -> None:
        from google import genai
        from google.genai import types

        self._types = types
        self._timeout = timeout_sec
        http_options = None
        if proxy_url:
            # SDK ходит в API через aiohttp/httpx: оба принимают proxy на запрос.
            http_options = types.HttpOptions(async_client_args={"proxy": proxy_url})
        self._client = genai.Client(api_key=api_key, http_options=http_options)
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
            t.Content(
                role="model" if turn.role == "assistant" else "user",
                parts=[t.Part(text=turn.text)]
                + [t.Part.from_bytes(data=m.data, mime_type=m.mime_type) for m in turn.media],
            )
            for turn in turns
        ]

    async def reply(self, model: str, system: str, turns: list[Turn]) -> AiReply:
        t = self._types
        config = t.GenerateContentConfig(
            system_instruction=system,
            tools=[self._tool],
            # Инструмент только сигнализирует «нужен человек» — исполнять его
            # силами SDK нечего, решение принимает бот.
            automatic_function_calling=t.AutomaticFunctionCallingConfig(disable=True),
            temperature=0.3,
        )
        resp = await self._generate(model, self._contents(turns), config)
        self._log_usage(model, resp)
        return parse_gemini_response(resp)

    async def _generate(self, model: str, contents: list, config):
        from google.genai import errors

        try:
            return await asyncio.wait_for(
                self._client.aio.models.generate_content(model=model, contents=contents, config=config),
                timeout=self._timeout,
            )
        except asyncio.TimeoutError as e:
            raise AiError(f"нет ответа за {self._timeout:.0f} с") from e
        except errors.APIError as e:
            if e.code == 429:
                raise AiRateLimited(e.message or "429") from e
            detail = f"{e.code} {e.status or ''} {e.message or ''}".strip()
            if e.code and e.code >= 500:
                raise AiUnavailable(detail) from e
            raise AiError(detail) from e
        except (aiohttp.ClientError, httpx.HTTPError, OSError) as e:
            # SDK ходит в сеть через aiohttp или httpx — что установлено.
            raise AiError(f"нет связи с API: {e}") from e

    @staticmethod
    def _log_usage(model: str, resp) -> None:
        usage = resp.usage_metadata
        if usage is not None:
            log.info(
                "Gemini %s: вход %s (из кэша %s), выход %s, размышления %s",
                model,
                usage.prompt_token_count,
                usage.cached_content_token_count,
                usage.candidates_token_count,
                usage.thoughts_token_count,
            )


def parse_gemini_response(resp) -> AiReply:
    """Разбор ответа Gemini: вызов escalate_to_human важнее текста рядом с ним."""
    for call in resp.function_calls or []:
        if call.name == ESCALATE_TOOL:
            return escalation_reply(call.args)

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
