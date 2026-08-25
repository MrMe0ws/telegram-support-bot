from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode, urlparse

import aiohttp

from app.config import RemnawaveSettings

log = logging.getLogger(__name__)


class RemnawaveApiError(Exception):
    """Ошибка HTTP/API Remnawave (не «пользователь не найден»)."""


@dataclass(frozen=True)
class RemnawaveUser:
    username: str | None
    status: str | None
    expire_at: str | None
    used_traffic_bytes: int | None
    traffic_limit_bytes: int | None
    traffic_limit_strategy: str | None
    last_traffic_reset_at: str | None
    hwid_device_limit: int | None
    description: str | None
    tag: str | None
    internal_squads: str | None
    last_activity_at: str | None
    subscription_url: str | None
    telegram_id: int | None


def _looks_like_user_dict(obj: dict[str, Any]) -> bool:
    # Remnawave 3.0.0 удалил поле `uuid` у пользователя: остались числовой `id`
    # и `shortUuid`. Проверка на "uuid" оставлена ради совместимости с 2.8,
    # но на 3.x она уже не срабатывает.
    return (
        "telegramId" in obj
        or "shortUuid" in obj
        or "uuid" in obj
        or ("username" in obj and "status" in obj)
    )


def _users_from_stream(data: Any) -> list[dict[str, Any]]:
    """Достаёт список пользователей из ответа GET /api/users/stream.

    Форма: {"response": {"users": [...], "nextCursor": ..., "hasMore": ...}}.
    Обычная распаковка (_unwrap_user_payload) сюда не подходит: она ищет первый
    похожий на пользователя словарь и внутрь списка `users` не заходит.
    """
    if not isinstance(data, dict):
        return []
    response = data.get("response")
    if not isinstance(response, dict):
        return []
    users = response.get("users")
    if not isinstance(users, list):
        return []
    return [u for u in users if isinstance(u, dict)]


def _find_user_dict(data: Any, *, depth: int = 0) -> dict[str, Any] | None:
    if depth > 6:
        return None
    if isinstance(data, dict):
        if _looks_like_user_dict(data):
            return data
        for key in ("user", "data", "result"):
            nested = data.get(key)
            if isinstance(nested, dict):
                found = _find_user_dict(nested, depth=depth + 1)
                if found:
                    return found
        inner = data.get("response")
        if inner is not None and inner is not data:
            found = _find_user_dict(inner, depth=depth + 1)
            if found:
                return found
    elif isinstance(data, list):
        for item in data:
            found = _find_user_dict(item, depth=depth + 1)
            if found:
                return found
    return None


def _unwrap_user_payload(data: Any) -> dict[str, Any] | None:
    if not isinstance(data, dict):
        return None
    found = _find_user_dict(data)
    if found:
        return found
    return data if _looks_like_user_dict(data) else None


def _first(*values: Any) -> Any:
    for v in values:
        if v is not None and v != "":
            return v
    return None


def _int_or_none(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _squad_names(raw: Any) -> str | None:
    if not raw:
        return None
    if isinstance(raw, str):
        return raw
    if not isinstance(raw, list):
        return None
    names: list[str] = []
    for item in raw:
        if isinstance(item, str):
            names.append(item)
        elif isinstance(item, dict):
            name = item.get("name") or item.get("title")
            if name:
                names.append(str(name))
    return ", ".join(names) if names else None


def _parsed_user_meaningful(user: RemnawaveUser) -> bool:
    return bool(
        user.username
        or user.status
        or user.expire_at
        or user.traffic_limit_bytes is not None
        or user.subscription_url,
    )


def parse_remnawave_user(data: dict[str, Any], *, sub_public_url: str) -> RemnawaveUser:
    user_traffic = data.get("userTraffic") if isinstance(data.get("userTraffic"), dict) else {}

    used = _first(
        user_traffic.get("usedTrafficBytes"),
        data.get("usedTrafficBytes"),
    )
    online_at = _first(
        user_traffic.get("onlineAt"),
        data.get("onlineAt"),
        data.get("lastOnline"),
        data.get("lastActivity"),
    )

    short_uuid = _first(data.get("shortUuid"), data.get("short_uuid"))
    sub_url: str | None = None
    raw_sub = data.get("subscriptionUrl") or data.get("subscription_url")
    if isinstance(raw_sub, str) and raw_sub.strip():
        sub_url = raw_sub.strip()
    elif short_uuid and sub_public_url:
        sub_url = f"{sub_public_url}/{short_uuid}"

    return RemnawaveUser(
        username=_first(data.get("username"), data.get("userName")),
        status=_first(data.get("status"), data.get("userStatus")),
        expire_at=_first(data.get("expireAt"), data.get("expire_at")),
        used_traffic_bytes=_int_or_none(used),
        traffic_limit_bytes=_int_or_none(
            _first(data.get("trafficLimitBytes"), data.get("traffic_limit_bytes")),
        ),
        traffic_limit_strategy=_first(
            data.get("trafficLimitStrategy"),
            data.get("traffic_limit_strategy"),
        ),
        last_traffic_reset_at=_first(
            data.get("lastTrafficResetAt"),
            data.get("trafficResetAt"),
            data.get("last_traffic_reset_at"),
        ),
        hwid_device_limit=_int_or_none(
            _first(data.get("hwidDeviceLimit"), data.get("hwid_device_limit")),
        ),
        description=_first(data.get("description"), data.get("note")),
        tag=_first(data.get("tag"), data.get("userTag")),
        internal_squads=_squad_names(
            _first(
                data.get("activeInternalSquads"),
                data.get("internalSquads"),
                data.get("internal_squads"),
            ),
        ),
        last_activity_at=str(online_at) if online_at is not None else None,
        subscription_url=sub_url,
        telegram_id=_int_or_none(_first(data.get("telegramId"), data.get("telegram_id"))),
    )


def _resolve_http_host(settings: RemnawaveSettings) -> str | None:
    if settings.http_host:
        return settings.http_host
    host = urlparse(settings.base_url).hostname
    return host or None


class RemnawaveClient:
    def __init__(self, settings: RemnawaveSettings) -> None:
        self._settings = settings

    def _api_url(self, path: str, params: dict[str, Any] | None = None) -> str:
        base = self._settings.base_url.rstrip("/") + path
        query: dict[str, Any] = dict(params or {})
        if (
            self._settings.mode == "remote"
            and self._settings.gate_query_name
            and self._settings.gate_query_value
        ):
            query[self._settings.gate_query_name] = self._settings.gate_query_value
        if not query:
            return base
        # Параметры собираются в один словарь, а не приклеиваются через "?" по очереди:
        # у /api/users/stream уже есть свои параметры, и второй "?" сломал бы URL.
        return f"{base}?{urlencode(query)}"

    def _build_url(self, telegram_id: int) -> str:
        # Remnawave 3.0.0 удалил GET /api/users/by-telegram-id/{id}.
        # Замена — стрим с фильтром; size=100 с запасом: у одного Telegram-аккаунта
        # профилей в панели считанные единицы.
        return self._api_url(
            "/api/users/stream",
            {"telegramId": str(telegram_id), "size": 100},
        )

    def _headers(self) -> dict[str, str]:
        headers = {
            "Authorization": f"Bearer {self._settings.token}",
            "Accept": "application/json",
            "Connection": "close",
        }
        host = _resolve_http_host(self._settings)
        if host:
            headers["Host"] = host
        if self._settings.mode == "remote" and self._settings.gate_cookie:
            headers["Cookie"] = self._settings.gate_cookie
        if self._settings.proxy_headers:
            headers["X-Forwarded-For"] = self._settings.forwarded_for
            headers["X-Forwarded-Proto"] = self._settings.forwarded_proto
        return headers

    def _session(self) -> aiohttp.ClientSession:
        timeout = aiohttp.ClientTimeout(total=self._settings.timeout_sec)
        connector = aiohttp.TCPConnector(force_close=True, enable_cleanup_closed=True)
        return aiohttp.ClientSession(timeout=timeout, connector=connector)

    async def _request_json(self, url: str) -> tuple[int, Any]:
        headers = self._headers()
        last_err: Exception | None = None
        for attempt in range(2):
            try:
                async with self._session() as session:
                    async with session.get(url, headers=headers) as resp:
                        status = resp.status
                        if status == 204:
                            return status, None
                        try:
                            data = await resp.json(content_type=None)
                        except aiohttp.ContentTypeError:
                            text = (await resp.text())[:500]
                            raise RemnawaveApiError(
                                f"HTTP {status}: ответ не JSON ({text[:120]})",
                            )
                        return status, data
            except aiohttp.ServerDisconnectedError as e:
                last_err = e
                log.warning(
                    "Remnawave: Server disconnected (попытка %s/2) url=%s host=%s",
                    attempt + 1,
                    url.split("?", 1)[0],
                    headers.get("Host", "—"),
                )
            except aiohttp.ClientError as e:
                last_err = e
                log.warning(
                    "Remnawave request failed (попытка %s/2): %s url=%s",
                    attempt + 1,
                    e,
                    url.split("?", 1)[0],
                )
        if last_err is not None:
            raise RemnawaveApiError(str(last_err)) from last_err
        raise RemnawaveApiError("неизвестная ошибка запроса")

    async def probe(self) -> None:
        """Проверка связи при старте: ожидаем 404 или 200 на тестовый telegram id."""
        url = self._build_url(0)
        status, _ = await self._request_json(url)
        if status not in (200, 404, 422):
            raise RemnawaveApiError(f"HTTP {status} на probe")

    async def _fetch_user_payload(self, path: str) -> dict[str, Any] | None:
        status, data = await self._request_json(self._api_url(path))
        if status == 404:
            return None
        if status in (401, 403):
            raise RemnawaveApiError(
                f"HTTP {status}: проверьте REMNAWAVE_TOKEN и доступ к API",
            )
        if status >= 400:
            text = str(data)[:200] if data is not None else ""
            raise RemnawaveApiError(f"HTTP {status}: {text}")
        payload = _unwrap_user_payload(data)
        if not payload:
            log.warning(
                "Remnawave: не найден объект пользователя в ответе, keys=%s",
                list(data.keys()) if isinstance(data, dict) else type(data),
            )
        return payload

    async def fetch_user_by_id(self, user_id: int) -> RemnawaveUser | None:
        """GET /api/users/{userId}. До 3.0.0 путь принимал uuid."""
        payload = await self._fetch_user_payload(f"/api/users/{user_id}")
        if not payload:
            return None
        return parse_remnawave_user(
            payload,
            sub_public_url=self._settings.sub_public_url,
        )

    async def fetch_user_by_telegram_id(
        self,
        telegram_id: int,
    ) -> RemnawaveUser | None:
        status, data = await self._request_json(self._build_url(telegram_id))
        if status == 404:
            return None
        if status in (401, 403):
            raise RemnawaveApiError(
                f"HTTP {status}: проверьте REMNAWAVE_TOKEN и доступ к API",
            )
        if status >= 400:
            text = str(data)[:200] if data is not None else ""
            raise RemnawaveApiError(f"HTTP {status}: {text}")

        # Фильтр стрима перепроверяем сами. Раньше отбор гарантировал сам эндпоинт
        # by-telegram-id, теперь это query-параметр: если панель его проигнорирует,
        # сюда приедут чужие профили, и оператор увидит в /profile чужую подписку.
        candidates = [
            u for u in _users_from_stream(data)
            if _int_or_none(u.get("telegramId")) == telegram_id
        ]
        if not candidates:
            return None

        payload = candidates[0]
        user = parse_remnawave_user(
            payload,
            sub_public_url=self._settings.sub_public_url,
        )
        if _parsed_user_meaningful(user):
            return user

        # Стрим отдаёт урезанную карточку — догружаем полную по числовому id.
        user_id = _int_or_none(payload.get("id"))
        if user_id:
            log.info(
                "Remnawave: stream отдал профиль без полей, догружаем GET /api/users/%s",
                user_id,
            )
            detailed = await self.fetch_user_by_id(user_id)
            if detailed and _parsed_user_meaningful(detailed):
                return detailed

        log.warning(
            "Remnawave: разбор без полей, payload keys=%s",
            list(payload.keys())[:24],
        )
        return None


async def log_remnawave_connectivity(settings: RemnawaveSettings) -> None:
    if not settings.is_configured:
        return
    host = _resolve_http_host(settings) or "—"
    try:
        await RemnawaveClient(settings).probe()
        log.info(
            "Remnawave: API доступен (%s, Host: %s)",
            settings.base_url,
            host,
        )
    except RemnawaveApiError as e:
        log.warning(
            "Remnawave: API недоступен (%s, Host: %s, proxy_headers=%s): %s — "
            "для local нужны REMNAWAVE_PROXY_HEADERS=true (по умолчанию) и "
            "REMNAWAVE_HTTP_HOST=PANEL_DOMAIN панели",
            settings.base_url,
            host,
            settings.proxy_headers,
            e,
        )
