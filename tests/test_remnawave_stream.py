from __future__ import annotations

from app.config import RemnawaveSettings
from app.services.remnawave_client import (
    RemnawaveClient,
    _users_from_stream,
)


def _settings(**kw) -> RemnawaveSettings:
    """RemnawaveSettings — frozen dataclass со всеми обязательными полями,
    поэтому значения по умолчанию задаём явно."""
    defaults = dict(
        enabled=True,
        base_url="http://remnawave:3000",
        mode="local",
        token="t",
        sub_public_url="",
        gate_query_name="",
        gate_query_value="",
        gate_cookie="",
        timeout_sec=10.0,
        timezone="UTC",
        http_host="",
        proxy_headers=False,
        forwarded_for="",
        forwarded_proto="",
    )
    defaults.update(kw)
    return RemnawaveSettings(**defaults)


def test_users_from_stream_reads_response_users() -> None:
    """Форма ответа /api/users/stream: {"response": {"users": [...]}}."""
    raw = {
        "response": {
            "users": [
                {"id": 9, "username": "220_58347380", "telegramId": 58347380},
                {"id": 10, "username": "other", "telegramId": 111},
            ],
            "nextCursor": None,
            "hasMore": False,
        },
    }
    users = _users_from_stream(raw)
    assert [u["id"] for u in users] == [9, 10]


def test_users_from_stream_tolerates_garbage() -> None:
    for raw in (None, {}, {"response": {}}, {"response": {"users": "nope"}}, []):
        assert _users_from_stream(raw) == []


def test_stream_url_keeps_single_question_mark() -> None:
    """У стрима свои query-параметры, gate-параметр не должен ломать URL."""
    client = RemnawaveClient(_settings())
    url = client._build_url(58347380)
    assert url.count("?") == 1
    assert "telegramId=58347380" in url
    assert "/api/users/stream" in url


def test_stream_url_merges_gate_param() -> None:
    """В remote-режиме gate-параметр добавляется к существующим, а не через второй '?'."""
    st = _settings(mode="remote", gate_query_name="key", gate_query_value="secret")
    url = RemnawaveClient(st)._build_url(42)
    assert url.count("?") == 1
    assert "telegramId=42" in url and "key=secret" in url
