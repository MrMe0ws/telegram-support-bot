from __future__ import annotations

import asyncio
import base64
import logging
import re

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

# Изображения, которые Claude принимает во входе (HEIC — нет).
_IMAGE_MIME = frozenset({"image/jpeg", "image/png", "image/webp", "image/gif"})
_MAX_TOKENS = 16000

# Модели, у которых Anthropic умеет сам переотправить запрос другой модели, если
# эта откажется отвечать по правилам безопасности (stop_reason=refusal).
_SERVER_FALLBACK_MODELS = frozenset({"claude-fable-5-1", "claude-opus-5-5", "claude-opus-5", "claude-sonnet-5-5"})
_SERVER_FALLBACK_BETA = "server-side-fallback-2026-07-01"

# Параметр effort есть у Claude 4.6 и новее (у Haiku 4.5 — нет).
_EFFORT_MODEL = re.compile(r"^claude-(opus|sonnet|fable|mythos)-(\d+)(?:-(\d+))?")


def supports_effort(model: str) -> bool:
    m = _EFFORT_MODEL.match(model)
    if not m:
        return False
    major, minor = int(m.group(2)), int(m.group(3) or 0)
    return major >= 5 or (major == 4 and minor >= 6)


class ClaudeBackend:
    """Anthropic Claude через официальный SDK anthropic (ключ: platform.claude.com)."""

    def __init__(self, api_key: str, proxy_url: str, timeout_sec: float) -> None:
        import anthropic

        self._timeout = timeout_sec
        http_client = anthropic.DefaultAsyncHttpxClient(proxy=proxy_url) if proxy_url else None
        # Повторы — дело цепочки моделей: при отказе сразу пробуем резервную.
        self._client = anthropic.AsyncAnthropic(api_key=api_key, max_retries=1, http_client=http_client)
        self._tools = [
            {
                "name": ESCALATE_TOOL,
                "description": ESCALATE_DESCRIPTION,
                "input_schema": ESCALATE_SCHEMA,
                "strict": True,
            }
        ]

    async def reply(self, model: str, system: str, turns: list[Turn]) -> AiReply:
        import anthropic

        params: dict = {
            "model": model,
            "max_tokens": _MAX_TOKENS,
            # База знаний в системном промпте неизменна — кэшируем её, и на каждом
            # ответе она стоит в разы дешевле.
            "system": [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
            "tools": self._tools,
            "messages": build_claude_messages(turns),
        }
        if supports_effort(model):
            # Ответы поддержки короткие — глубокие размышления тут только тратят время.
            params["output_config"] = {"effort": "low"}
        if model in _SERVER_FALLBACK_MODELS:
            params["betas"] = [_SERVER_FALLBACK_BETA]
            params["fallbacks"] = "default"
        try:
            resp = await asyncio.wait_for(self._client.beta.messages.create(**params), timeout=self._timeout)
        except asyncio.TimeoutError as e:
            raise AiError(f"нет ответа за {self._timeout:.0f} с") from e
        except anthropic.RateLimitError as e:
            raise AiRateLimited(e.message or "429") from e
        except anthropic.APIStatusError as e:
            detail = f"{e.status_code} {e.message or ''}".strip()
            # 529 — перегрузка, 5xx — сбой у провайдера.
            if e.status_code >= 500:
                raise AiUnavailable(detail) from e
            raise AiError(detail) from e
        except anthropic.APIConnectionError as e:
            raise AiError(f"нет связи с API: {e}") from e
        self._log_usage(model, resp)
        return parse_claude_response(resp)

    @staticmethod
    def _log_usage(model: str, resp) -> None:
        usage = getattr(resp, "usage", None)
        if usage is not None:
            log.info(
                "Claude %s: вход %s (из кэша %s, в кэш %s), выход %s",
                model,
                usage.input_tokens,
                usage.cache_read_input_tokens,
                usage.cache_creation_input_tokens,
                usage.output_tokens,
            )


def _media_block(media: Media) -> dict:
    if media.mime_type in _IMAGE_MIME:
        data = base64.b64encode(media.data).decode("ascii")
        return {"type": "image", "source": {"type": "base64", "media_type": media.mime_type, "data": data}}
    if media.mime_type == "application/pdf":
        data = base64.b64encode(media.data).decode("ascii")
        return {"type": "document", "source": {"type": "base64", "media_type": "application/pdf", "data": data}}
    if media.mime_type == "text/plain":
        return {
            "type": "document",
            "source": {"type": "text", "media_type": "text/plain", "data": attachment_text(media.data)},
        }
    # Голосовые и аудио Claude не принимает.
    return {"type": "text", "text": unsupported_media_note(media.mime_type)}


def build_claude_messages(turns: list[Turn]) -> list[dict]:
    messages: list[dict] = []
    for turn in turns:
        if turn.role == "assistant":
            messages.append({"role": "assistant", "content": turn.text})
            continue
        # Вложения — перед текстом: так Claude разбирает их лучше.
        content = [_media_block(m) for m in turn.media] + [{"type": "text", "text": turn.text}]
        messages.append({"role": "user", "content": content})
    return messages


def parse_claude_response(resp) -> AiReply:
    """Разбор ответа Claude: вызов escalate_to_human важнее текста рядом с ним."""
    if resp.stop_reason == "refusal":
        details = getattr(resp, "stop_details", None)
        category = getattr(details, "category", None)
        raise AiError(f"модель отказалась отвечать ({category or 'refusal'})")
    texts: list[str] = []
    for block in resp.content or []:
        if block.type == "tool_use" and block.name == ESCALATE_TOOL:
            return escalation_reply(block.input if isinstance(block.input, dict) else {})
        if block.type == "text" and block.text:
            texts.append(block.text)
    text = "".join(texts).strip()
    if not text:
        raise AiError(f"модель не дала текста (stop_reason={resp.stop_reason})")
    return AiReply(text=text)
