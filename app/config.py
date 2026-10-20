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


@dataclass(frozen=True)
class AiSettings:
    """ИИ-ответы клиентам. Всё — из .env, без переключателей в боте."""

    enabled: bool
    # auto — ИИ отвечает клиенту сам; draft — готовит черновик в тему, отправляет админ.
    mode: str
    provider: str
    api_key: str
    model: str
    # Прокси до API провайдера (из РФ Gemini напрямую недоступен), напр. http://user:pass@host:port.
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

    @property
    def is_active(self) -> bool:
        return self.enabled and bool(self.api_key) and bool(self.model)


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


def _ai_from_env() -> AiSettings:
    mode = os.environ.get("AI_MODE", "auto").strip().lower()
    if mode not in AI_MODES:
        mode = "auto"
    return AiSettings(
        enabled=_env_bool("AI_ENABLED", False),
        mode=mode,
        provider=os.environ.get("AI_PROVIDER", "gemini").strip().lower() or "gemini",
        api_key=os.environ.get("AI_API_KEY", "").strip(),
        model=os.environ.get("AI_MODEL", "gemini-2.5-flash").strip(),
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
        )
