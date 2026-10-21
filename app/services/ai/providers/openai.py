from __future__ import annotations

import asyncio
import base64
import json
import logging

from app.services.ai.media import Media
from app.services.ai.prompt import Turn
from app.services.ai.provider import (
    ESCALATE_DESCRIPTION,
    ESCALATE_SCHEMA,
    ESCALATE_TOOL,
    AiError,
    AiRateLimited,
    AiReply,
    AiUnavailable,
    attachment_text,
    escalation_reply,
    unsupported_media_note,
)

log = logging.getLogger(__name__)

# Изображения, которые OpenAI принимает во входе (HEIC — нет).
_IMAGE_MIME = frozenset({"image/jpeg", "image/png", "image/webp", "image/gif"})
_MAX_OUTPUT_TOKENS = 16000


class OpenAiBackend:
    """OpenAI через официальный SDK openai, Responses API (ключ: platform.openai.com)."""

    def __init__(self, api_key: str, proxy_url: str, timeout_sec: float) -> None:
        import openai

        self._timeout = timeout_sec
        http_client = openai.DefaultAsyncHttpxClient(proxy=proxy_url) if proxy_url else None
        # Повторы — дело цепочки моделей: при отказе сразу пробуем резервную.
        self._client = openai.AsyncOpenAI(api_key=api_key, max_retries=1, http_client=http_client)
        self._tools = [
            {
                "type": "function",
                "name": ESCALATE_TOOL,
                "description": ESCALATE_DESCRIPTION,
                "parameters": ESCALATE_SCHEMA,
                "strict": True,
            }
        ]

    async def reply(self, model: str, system: str, turns: list[Turn]) -> AiReply:
        import openai

        try:
            resp = await asyncio.wait_for(
                self._client.responses.create(
                    model=model,
                    instructions=system,
                    input=build_openai_input(turns),
                    tools=self._tools,
                    max_output_tokens=_MAX_OUTPUT_TOKENS,
                    # Историю тикета бот хранит сам — на серверах OpenAI она не нужна.
                    store=False,
                ),
                timeout=self._timeout,
            )
        except asyncio.TimeoutError as e:
            raise AiError(f"нет ответа за {self._timeout:.0f} с") from e
        except openai.RateLimitError as e:
            raise AiRateLimited(e.message or "429") from e
        except openai.APIStatusError as e:
            detail = f"{e.status_code} {e.message or ''}".strip()
            if e.status_code >= 500:
                raise AiUnavailable(detail) from e
            raise AiError(detail) from e
        except openai.APIConnectionError as e:
            raise AiError(f"нет связи с API: {e}") from e
        self._log_usage(model, resp)
        return parse_openai_response(resp)

    @staticmethod
    def _log_usage(model: str, resp) -> None:
        usage = getattr(resp, "usage", None)
        if usage is None:
            return
        details = getattr(usage, "input_tokens_details", None)
        log.info(
            "OpenAI %s: вход %s (из кэша %s), выход %s",
            model,
            usage.input_tokens,
            getattr(details, "cached_tokens", None),
            usage.output_tokens,
        )


def _media_part(media: Media) -> dict:
    b64 = base64.b64encode(media.data).decode("ascii")
    if media.mime_type in _IMAGE_MIME:
        return {"type": "input_image", "image_url": f"data:{media.mime_type};base64,{b64}", "detail": "auto"}
    if media.mime_type == "application/pdf":
        return {"type": "input_file", "filename": "file.pdf", "file_data": f"data:application/pdf;base64,{b64}"}
    if media.mime_type == "text/plain":
        return {"type": "input_text", "text": f"[текст вложения]\n{attachment_text(media.data)}"}
    # Голосовые и аудио Responses API во входе не принимает.
    return {"type": "input_text", "text": unsupported_media_note(media.mime_type)}


def build_openai_input(turns: list[Turn]) -> list[dict]:
    items: list[dict] = []
    for turn in turns:
        if turn.role == "assistant":
            items.append({"role": "assistant", "content": turn.text})
            continue
        content = [{"type": "input_text", "text": turn.text}] + [_media_part(m) for m in turn.media]
        items.append({"role": "user", "content": content})
    return items


def parse_openai_response(resp) -> AiReply:
    """Разбор ответа Responses API: вызов escalate_to_human важнее текста рядом с ним."""
    texts: list[str] = []
    for item in resp.output or []:
        if item.type == "function_call" and item.name == ESCALATE_TOOL:
            try:
                args = json.loads(item.arguments or "{}")
            except json.JSONDecodeError:
                args = {}
            return escalation_reply(args if isinstance(args, dict) else {})
        if item.type == "message":
            for part in item.content or []:
                if part.type == "output_text" and part.text:
                    texts.append(part.text)
                elif part.type == "refusal":
                    raise AiError(f"модель отказалась отвечать: {part.refusal}")
    text = "".join(texts).strip()
    if not text:
        details = getattr(resp, "incomplete_details", None)
        raise AiError(f"модель не дала текста (status={resp.status}, {details})")
    return AiReply(text=text)
