import logging
import os
from dataclasses import dataclass

from app.i18n import tr


def _split_ids(raw: str) -> frozenset[int]:
    out: list[int] = []
    for part in raw.replace(" ", "").split(","):
        if not part:
            continue
        out.append(int(part))
    return frozenset(out)


def _env_bool(key: str, default: bool = False) -> bool:
    raw = os.environ.get(key)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


@dataclass(frozen=True)
class RemnawaveSettings:
    enabled: bool
    base_url: str
    mode: str
    token: str
    sub_public_url: str
    gate_query_name: str
    gate_query_value: str
    gate_cookie: str
    timeout_sec: float
    timezone: str
    """Заголовок Host (как PANEL_DOMAIN в .env панели). Часто нужен при REMNAWAVE_URL=http://remnawave:3000."""
    http_host: str
    # Remnawave proxyCheckMiddleware в production: без X-Forwarded-* сокет рвётся (Server disconnected).
    proxy_headers: bool
    forwarded_for: str
    forwarded_proto: str

    @property
    def is_configured(self) -> bool:
        return self.enabled and bool(self.token) and bool(self.base_url)


AI_MODES = ("auto", "draft")

# Провайдеры ИИ и модель по умолчанию для каждого.
AI_PROVIDERS = ("gemini", "openai", "claude")
AI_PROVIDER_ALIASES = {"google": "gemini", "chatgpt": "openai", "gpt": "openai", "anthropic": "claude"}
AI_DEFAULT_MODELS = {
    "gemini": "gemini-2.5-flash",
    "openai": "gpt-6-luna",
    "claude": "claude-opus-5-5",
}


def normalize_ai_provider(raw: str) -> str:
    p = raw.strip().lower()
    return AI_PROVIDER_ALIASES.get(p, p)


def guess_ai_provider(model: str) -> str | None:
    """Провайдер по имени модели: gemini-… → gemini, claude-… → claude, gpt-… / o3 → openai."""
    m = model.strip().lower()
    if m.startswith("gemini") or m.startswith("gemma"):
        return "gemini"
    if m.startswith("claude"):
        return "claude"
    if m.startswith(("gpt", "chatgpt", "o1", "o3", "o4")):
        return "openai"
    return None


def parse_ai_model(raw: str, default_provider: str) -> str:
    """
    Одна модель из настроек → «провайдер:модель».

    Провайдер можно указать явно (claude:claude-sonnet-5-5), иначе он угадывается
    по имени модели, а если не угадался — берётся default_provider.
    Неизвестный провайдер — ValueError.
    """
    s = raw.strip()
    if not s:
        raise ValueError("пустое имя модели")
    if ":" in s:
        head, tail = s.split(":", 1)
        provider = normalize_ai_provider(head)
        if provider in AI_PROVIDERS:
            model = tail.strip()
            if not model:
                raise ValueError(f"не указана модель: {s!r}")
            return f"{provider}:{model}"
        if guess_ai_provider(s) is None:
            raise ValueError(f"неизвестный провайдер {head.strip()!r} (доступно: {', '.join(AI_PROVIDERS)})")
    return f"{guess_ai_provider(s) or default_provider}:{s}"


def parse_ai_models(raw: str, default_provider: str) -> tuple[str, ...]:
    """Список моделей через запятую → «провайдер:модель» без повторов, порядок сохраняется."""
    out: list[str] = []
    for part in raw.split(","):
        if not part.strip():
            continue
        spec = parse_ai_model(part, default_provider)
        if spec not in out:
            out.append(spec)
    return tuple(out)


@dataclass(frozen=True)
class AiSettings:
    """ИИ-ответы клиентам. Значения — из .env; большую часть админ может поменять в боте (⚙️ Настройки)."""

    enabled: bool
    # auto — ИИ отвечает клиенту сам; draft — готовит черновик в тему, отправляет админ.
    mode: str
    # Провайдер для моделей, у которых его не видно по имени.
    provider: str
    # Цепочка «провайдер:модель»: первая — основная, остальные — резервные по порядку.
    # На следующую бот переходит, если предыдущая не ответила (лимит, сбой, нет связи).
    models: tuple[str, ...]
    # Ключи API по провайдерам; модели провайдера без ключа пропускаются.
    api_keys: tuple[tuple[str, str], ...]
    # Прокси до API провайдеров (из РФ они напрямую недоступны), напр. http://user:pass@host:port.
    proxy_url: str
    # Сколько ждать тишины после сообщения клиента, прежде чем отвечать: клиент часто
    # пишет несколькими сообщениями подряд.
    reply_delay_sec: float
    knowledge_dir: str
    history_limit: int
    # Сколько раз ИИ может ответить в одном тикете, прежде чем передать его человеку.
    max_replies_per_ticket: int
    timeout_sec: float
    assistant_name: str
    # Показывать модели фото, голосовые и файлы клиента (только Telegram-тикеты).
    media_enabled: bool = True
    # Файлы больше этого размера модели не отправляются — тикет уходит человеку.
    media_max_mb: int = 10
    # С какого по счёту ответа ИИ в тикете показывать клиенту кнопку «Позвать человека»;
    # 0 — не показывать (позвать человека клиент может и словами).
    human_button_from: int = 1

    def api_key(self, provider: str) -> str:
        return dict(self.api_keys).get(provider, "")

    def has_key(self, spec: str) -> bool:
        return bool(self.api_key(spec.split(":", 1)[0]))

    @property
    def is_configured(self) -> bool:
        """Есть модель с ключом API — ИИ можно включить (в .env или в боте)."""
        return any(self.has_key(m) for m in self.models)

    @property
    def is_active(self) -> bool:
        return self.enabled and self.is_configured


def _env_int(key: str, default: int, lo: int, hi: int) -> int:
    try:
        v = int(os.environ.get(key, "") or default)
    except ValueError:
        v = default
    return max(lo, min(hi, v))


def _env_float(key: str, default: float, lo: float, hi: float) -> float:
    try:
        v = float(os.environ.get(key, "") or default)
    except ValueError:
        v = default
    return max(lo, min(hi, v))


def _ai_models_from_env(provider: str) -> tuple[str, ...]:
    main = os.environ.get("AI_MODEL", "").strip() or AI_DEFAULT_MODELS[provider]
    fallback = os.environ.get("AI_FALLBACK_MODELS")
    if fallback is None:
        # Старое имя переменной; без неё у Gemini по умолчанию есть запасная модель.
        fallback = os.environ.get("AI_FALLBACK_MODEL")
    if fallback is None:
        fallback = "gemini-3.5-flash-lite" if provider == "gemini" else ""
    out: list[str] = []
    for part in f"{main},{fallback}".split(","):
        if not part.strip():
            continue
        try:
            spec = parse_ai_model(part, provider)
        except ValueError as e:
            logging.getLogger(__name__).warning("ИИ: модель %r пропущена: %s", part.strip(), e)
            continue
        if spec not in out:
            out.append(spec)
    return tuple(out)


def _ai_from_env() -> AiSettings:
    mode = os.environ.get("AI_MODE", "auto").strip().lower()
    if mode not in AI_MODES:
        mode = "auto"
    provider = normalize_ai_provider(os.environ.get("AI_PROVIDER", "") or "gemini")
    if provider not in AI_PROVIDERS:
        logging.getLogger(__name__).warning("AI_PROVIDER=%r не поддерживается — беру gemini", provider)
        provider = "gemini"
    keys = {
        "gemini": os.environ.get("GEMINI_API_KEY", "").strip(),
        "openai": os.environ.get("OPENAI_API_KEY", "").strip(),
        "claude": os.environ.get("ANTHROPIC_API_KEY", "").strip(),
    }
    # AI_API_KEY — общий ключ из прошлых версий: он для провайдера из AI_PROVIDER.
    if not keys[provider]:
        keys[provider] = os.environ.get("AI_API_KEY", "").strip()
    return AiSettings(
        enabled=_env_bool("AI_ENABLED", False),
        mode=mode,
        provider=provider,
        models=_ai_models_from_env(provider),
        api_keys=tuple((p, k) for p, k in keys.items() if k),
        proxy_url=os.environ.get("AI_PROXY_URL", "").strip(),
        reply_delay_sec=_env_float("AI_REPLY_DELAY_SEC", 20.0, 0.0, 120.0),
        knowledge_dir=os.environ.get("AI_KNOWLEDGE_DIR", "knowledge").strip() or "knowledge",
        history_limit=_env_int("AI_HISTORY_LIMIT", 30, 2, 200),
        max_replies_per_ticket=_env_int("AI_MAX_REPLIES_PER_TICKET", 15, 1, 1000),
        timeout_sec=_env_float("AI_TIMEOUT_SEC", 60.0, 5.0, 300.0),
        assistant_name=os.environ.get("AI_ASSISTANT_NAME", "ИИ-помощник").strip() or "ИИ-помощник",
        media_enabled=_env_bool("AI_MEDIA", True),
        # Telegram отдаёт ботам файлы не больше 20 МБ.
        media_max_mb=_env_int("AI_MEDIA_MAX_MB", 10, 1, 20),
        human_button_from=_env_int("AI_HUMAN_BUTTON_FROM", 1, 0, 1000),
    )


@dataclass(frozen=True)
class Settings:
    bot_token: str
    support_group_id: int
    admin_ids: frozenset[int]
    database_url: str
    topic_name_template: str
    # ID кастомного emoji для иконки темы (открытый / закрытый тикет). Пустая строка — не выставлять.
    topic_icon_emoji_open: str
    topic_icon_emoji_closed: str
    shop_webhook_url: str
    support_bridge_secret: str
    http_port: int
    remnawave: RemnawaveSettings
    ai: AiSettings
    # Значок темы «ждёт человека»: ID кастомного эмодзи или само эмодзи из набора
    # значков тем (при старте переводится в ID). Пусто — не менять значок.
    topic_icon_emoji_waiting: str = ""

    @staticmethod
    def from_env() -> "Settings":
        token = os.environ.get("BOT_TOKEN", "").strip()
        if not token:
            raise RuntimeError(tr("errors", "no_bot_token"))

        gid = int(os.environ["SUPPORT_GROUP_ID"])
        admins_raw = os.environ.get("ADMIN_IDS", "")
        db = os.environ.get(
            "DATABASE_URL",
            "sqlite+aiosqlite:////data/support.db",
        )
        tpl = os.environ.get(
            "TOPIC_NAME_TEMPLATE",
            "{username} · #{ticket_id}",
        )
        icon_open = os.environ.get(
            "TOPIC_ICON_EMOJI_OPEN",
            "5377316857231450742",
        ).strip()
        icon_closed = os.environ.get(
            "TOPIC_ICON_EMOJI_CLOSED",
            "5237699328843200968",
        ).strip()

        rw_enabled = _env_bool("REMNAWAVE_ENABLED", False)
        rw_url = os.environ.get("REMNAWAVE_URL", "").strip().rstrip("/")
        rw_mode = os.environ.get("REMNAWAVE_MODE", "local").strip().lower()
        if rw_mode not in ("local", "remote"):
            rw_mode = "local"
        rw_token = os.environ.get("REMNAWAVE_TOKEN", "").strip()
        rw_sub = os.environ.get("REMNAWAVE_SUB_PUBLIC_URL", "").strip().rstrip("/")
        rw_timeout = float(os.environ.get("REMNAWAVE_TIMEOUT_SEC", "10") or "10")
        rw_tz = os.environ.get("BOT_TIMEZONE", "UTC").strip() or "UTC"
        proxy_headers = _env_bool(
            "REMNAWAVE_PROXY_HEADERS",
            default=(rw_mode == "local"),
        )

        remnawave = RemnawaveSettings(
            enabled=rw_enabled,
            base_url=rw_url,
            mode=rw_mode,
            token=rw_token,
            sub_public_url=rw_sub,
            gate_query_name=os.environ.get("REMNAWAVE_GATE_QUERY_NAME", "").strip(),
            gate_query_value=os.environ.get("REMNAWAVE_GATE_QUERY_VALUE", "").strip(),
            gate_cookie=os.environ.get("REMNAWAVE_GATE_COOKIE", "").strip(),
            timeout_sec=max(1.0, rw_timeout),
            timezone=rw_tz,
            http_host=os.environ.get("REMNAWAVE_HTTP_HOST", "").strip(),
            proxy_headers=proxy_headers,
            forwarded_for=os.environ.get("REMNAWAVE_FORWARDED_FOR", "127.0.0.1").strip(),
            forwarded_proto=os.environ.get(
                "REMNAWAVE_FORWARDED_PROTO",
                "https",
            ).strip(),
        )

        return Settings(
            bot_token=token,
            support_group_id=gid,
            admin_ids=_split_ids(admins_raw),
            database_url=db,
            topic_name_template=tpl,
            topic_icon_emoji_open=icon_open,
            topic_icon_emoji_closed=icon_closed,
            shop_webhook_url=os.environ.get("SHOP_WEBHOOK_URL", "").strip(),
            support_bridge_secret=os.environ.get("SUPPORT_BRIDGE_SECRET", "").strip(),
            http_port=int(os.environ.get("HTTP_PORT", "8080") or "8080"),
            remnawave=remnawave,
            ai=_ai_from_env(),
            topic_icon_emoji_waiting=os.environ.get("TOPIC_ICON_EMOJI_WAITING", "❗").strip(),
        )
