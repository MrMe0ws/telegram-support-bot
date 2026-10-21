from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Callable, Protocol

from app.config import AI_PROVIDERS, AiSettings
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
# JSON Schema аргументов escalate_to_human — для OpenAI и Claude.
ESCALATE_SCHEMA = {
    "type": "object",
    "properties": {name: {"type": "string", "description": desc} for name, desc in ESCALATE_PARAMS.items()},
    "required": list(ESCALATE_PARAMS),
    "additionalProperties": False,
}

# Текстовый файл клиента уходит модели текстом; длиннее — обрезается.
MAX_TEXT_ATTACHMENT_CHARS = 100_000

PROVIDER_TITLES = {"gemini": "Gemini", "openai": "OpenAI", "claude": "Claude"}


@dataclass(frozen=True)
class AiReply:
    """Итог одного обращения к модели: либо текст клиенту, либо передача человеку."""

    text: str | None = None
    escalate_reason: str | None = None
    escalate_summary: str | None = None

    @property
    def escalate(self) -> bool:
        return self.escalate_reason is not None


def escalation_reply(args: dict | None) -> AiReply:
    args = args or {}
    return AiReply(
        escalate_reason=str(args.get("reason") or "").strip() or "без причины",
        escalate_summary=str(args.get("summary") or "").strip() or None,
    )


class AiError(Exception):
    """Модель не дала ответа: сеть, ключ, пустой или заблокированный ответ."""


class AiRateLimited(AiError):
    """Провайдер отказал по лимиту запросов (на бесплатном тарифе — обычное дело)."""


class AiUnavailable(AiError):
    """Модель перегружена или у провайдера сбой (5xx) — другая модель может ответить."""


class AiProvider(Protocol):
    async def reply(self, system: str, turns: list[Turn]) -> AiReply: ...


class ModelBackend(Protocol):
    """API одного провайдера; модель — параметр запроса."""

    async def reply(self, model: str, system: str, turns: list[Turn]) -> AiReply: ...


def unsupported_media_note(mime_type: str) -> str:
    return f"[вложение {mime_type} этой модели недоступно]"


def attachment_text(data: bytes) -> str:
    text = data.decode("utf-8", errors="replace")
    if len(text) > MAX_TEXT_ATTACHMENT_CHARS:
        text = text[:MAX_TEXT_ATTACHMENT_CHARS] + "\n…[файл обрезан]"
    return text


def split_spec(spec: str) -> tuple[str, str]:
    provider, model = spec.split(":", 1)
    return provider, model


def _create_backend(provider: str, settings: AiSettings) -> ModelBackend:
    key = settings.api_key(provider)
    if provider == "gemini":
        from app.services.ai.providers.gemini import GeminiBackend

        return GeminiBackend(key, settings.proxy_url, settings.timeout_sec)
    if provider == "openai":
        from app.services.ai.providers.openai import OpenAiBackend

        return OpenAiBackend(key, settings.proxy_url, settings.timeout_sec)
    if provider == "claude":
        from app.services.ai.providers.claude import ClaudeBackend

        return ClaudeBackend(key, settings.proxy_url, settings.timeout_sec)
    raise ValueError(f"провайдер {provider!r} не поддерживается (доступно: {', '.join(AI_PROVIDERS)})")


class ModelChain:
    """
    Цепочка моделей: отвечает первая, остальные — резервные. Если модель не дала
    ответа (лимит, перегрузка, сбой, нет связи), запрос уходит следующей — в том
    числе модели другого провайдера.

    Порядок можно менять на ходу (админ-панель); модели провайдеров без ключа
    пропускаются.
    """

    def __init__(
        self,
        settings: AiSettings,
        backend_factory: Callable[[str, AiSettings], ModelBackend] = _create_backend,
    ) -> None:
        self._settings = settings
        self._factory = backend_factory
        self._backends: dict[str, ModelBackend] = {}
        self._models: tuple[str, ...] = ()
        self.set_models(settings.models)

    @property
    def models(self) -> tuple[str, ...]:
        return self._models

    @property
    def env_models(self) -> tuple[str, ...]:
        return self._settings.models

    @property
    def proxy_url(self) -> str:
        return self._settings.proxy_url

    def has_key(self, spec: str) -> bool:
        return self._settings.has_key(spec)

    def set_models(self, models: tuple[str, ...] | list[str]) -> None:
        """Новая цепочка. ValueError — пусто, или ни у одной модели нет ключа."""
        models = tuple(dict.fromkeys(models))
        if not models:
            raise ValueError("список моделей пуст")
        for spec in models:
            if split_spec(spec)[0] not in AI_PROVIDERS:
                raise ValueError(f"неизвестный провайдер в {spec!r}")
        if not any(self.has_key(m) for m in models):
            raise ValueError("ни для одной модели не задан ключ API")
        self._models = models

    def _backend(self, provider: str) -> ModelBackend:
        backend = self._backends.get(provider)
        if backend is None:
            backend = self._factory(provider, self._settings)
            self._backends[provider] = backend
        return backend

    async def reply(self, system: str, turns: list[Turn]) -> AiReply:
        usable = [m for m in self._models if self.has_key(m)]
        last: AiError | None = None
        for i, spec in enumerate(usable):
            provider, model = split_spec(spec)
            try:
                return await self._backend(provider).reply(model, system, turns)
            except AiError as e:
                last = e
                if i + 1 < len(usable):
                    log.warning("ИИ: %s не ответила (%s) — пробуем %s", spec, e, usable[i + 1])
                else:
                    log.warning("ИИ: %s не ответила (%s)", spec, e)
            except Exception as e:
                # Сбой SDK или неверные настройки клиента — пробуем следующую модель.
                log.exception("ИИ: сбой запроса к %s", spec)
                last = AiError(f"{spec}: {e}")
        if last is None:
            raise AiError("нет моделей с ключом API")
        raise last


@dataclass(frozen=True)
class ModelCheck:
    """Итог тестового запроса к одной модели («🧪 Проверить ИИ» в настройках)."""

    spec: str
    # ok | no_key | limit | unavailable | timeout | error
    status: str
    seconds: float = 0.0
    # Ответ модели или текст ошибки.
    detail: str = ""


_CHECK_SYSTEM = "Это проверка связи с ботом поддержки. Ответь одним коротким словом."
_CHECK_TURNS = [Turn("user", "Проверка связи. Ты на месте?")]


async def check_models(chain: ModelChain, timeout_sec: float) -> list[ModelCheck]:
    """Тестовый запрос ко всем моделям цепочки разом: какая отвечает, сколько думает, что ломается."""

    async def one(spec: str) -> ModelCheck:
        if not chain.has_key(spec):
            return ModelCheck(spec, "no_key")
        provider, model = split_spec(spec)
        started = time.monotonic()
        try:
            reply = await asyncio.wait_for(
                chain._backend(provider).reply(model, _CHECK_SYSTEM, _CHECK_TURNS), timeout_sec
            )
        except asyncio.TimeoutError:
            return ModelCheck(spec, "timeout", time.monotonic() - started)
        except AiRateLimited as e:
            return ModelCheck(spec, "limit", time.monotonic() - started, str(e))
        except AiUnavailable as e:
            return ModelCheck(spec, "unavailable", time.monotonic() - started, str(e))
        except Exception as e:
            return ModelCheck(spec, "error", time.monotonic() - started, str(e) or type(e).__name__)
        # Вызов «позвать человека» вместо текста — тоже ответ: модель работает.
        detail = reply.text or (f"escalate: {reply.escalate_reason}" if reply.escalate else "")
        return ModelCheck(spec, "ok", time.monotonic() - started, detail)

    return list(await asyncio.gather(*(one(spec) for spec in chain.models)))


def create_provider(settings: AiSettings) -> ModelChain:
    return ModelChain(settings)
