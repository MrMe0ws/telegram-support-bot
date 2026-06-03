from __future__ import annotations

from app.config import RemnawaveSettings
from app.services.remnawave_client import RemnawaveUser, parse_remnawave_user
from app.services.remnawave_profile import format_remnawave_profile_block


def test_parse_remnawave_user_from_response_wrapper() -> None:
    from app.services.remnawave_client import _unwrap_user_payload

    raw = {
        "response": {
            "username": "1388_8157396837",
            "status": "EXPIRED",
            "expireAt": "2026-05-31T21:04:00.000Z",
            "trafficLimitBytes": 214748364800,
            "trafficLimitStrategy": "MONTH",
            "lastTrafficResetAt": "2026-06-01T00:20:00.042Z",
            "hwidDeviceLimit": 2,
            "description": "cat_tac_cat",
            "tag": "TG_TAG",
            "shortUuid": "DSuahCNj_vvXH795",
            "telegramId": 8157396837,
            "subscriptionUrl": "https://sub.me0ws.ru/DSuahCNj_vvXH795",
            "activeInternalSquads": [{"uuid": "x", "name": "Default-Squad"}],
            "userTraffic": {"usedTrafficBytes": 0, "onlineAt": None},
        },
    }
    payload = _unwrap_user_payload(raw)
    assert payload is not None
    user = parse_remnawave_user(payload, sub_public_url="https://sub.example.com")
    assert user.username == "1388_8157396837"
    assert user.status == "EXPIRED"
    assert user.used_traffic_bytes == 0
    assert user.traffic_limit_bytes == 214748364800
    assert user.subscription_url == "https://sub.me0ws.ru/DSuahCNj_vvXH795"

    nested = {"response": {"user": raw["response"]}}
    assert _unwrap_user_payload(nested) is not None
    assert user.internal_squads == "Default-Squad"
    assert user.description == "cat_tac_cat"

    # Без unwrap поля лежат во вложенном response — парсер видит только обёртку.
    bare = parse_remnawave_user(raw, sub_public_url="https://sub.example.com")
    assert bare.username is None
    assert bare.status is None


def test_format_remnawave_profile_contains_key_fields() -> None:
    user = RemnawaveUser(
        username="1044_276586913",
        status="ACTIVE",
        expire_at="2026-07-03T10:27:00.000Z",
        used_traffic_bytes=0,
        traffic_limit_bytes=200 * 1024**3,
        traffic_limit_strategy="MONTH",
        last_traffic_reset_at="2026-06-03T10:27:00.000Z",
        hwid_device_limit=2,
        description="marinatsygankova",
        tag="TG_TAG",
        internal_squads="Default-Squad",
        last_activity_at="2026-06-03T10:58:00.000Z",
        subscription_url="https://sub.example.com/PdYb6LonHb3129VK",
        telegram_id=276586913,
    )
    settings = RemnawaveSettings(
        enabled=True,
        base_url="http://localhost:3000",
        mode="local",
        token="x",
        sub_public_url="https://sub.example.com",
        gate_query_name="",
        gate_query_value="",
        gate_cookie="",
        timeout_sec=10.0,
        timezone="UTC",
        http_host="panel.example.com",
        proxy_headers=True,
        forwarded_for="127.0.0.1",
        forwarded_proto="https",
    )
    html = format_remnawave_profile_block(
        user,
        telegram_username_line="@testuser",
        telegram_id=276586913,
        settings=settings,
    )
    assert "@testuser" in html
    assert "276586913" in html
    assert "ACTIVE" in html
    assert "200.00" in html
    assert "Default-Squad" in html
    assert "PdYb6LonHb3129VK" in html
    assert "Доп. слоты HWID" not in html
