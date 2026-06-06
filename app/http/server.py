from __future__ import annotations

import hmac
import json
import logging

from aiohttp import web

from app.services.cabinet_bridge import post_cabinet_message

log = logging.getLogger(__name__)


def _authorized(request: web.Request, settings) -> bool:
    secret = (settings.support_bridge_secret or "").strip()
    if not secret:
        return False
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        return False
    token = auth[7:].strip()
    return hmac.compare_digest(token, secret)


async def cabinet_message_handler(request: web.Request) -> web.Response:
    settings = request.app["settings"]
    bot = request.app["bot"]
    session_factory = request.app["session_factory"]

    if not _authorized(request, settings):
        return web.json_response({"error": "unauthorized"}, status=401)

    try:
        payload = await request.json()
    except json.JSONDecodeError:
        return web.json_response({"error": "invalid json"}, status=400)

    try:
        account_id = int(payload["account_id"])
        shop_ticket_id = int(payload["shop_ticket_id"])
        is_new_ticket = bool(payload.get("is_new_ticket"))
        client_message_id = str(payload.get("client_message_id") or "").strip() or None
        display_name = str(payload.get("display_name") or "").strip()
        telegram_label = str(payload.get("telegram_label") or "").strip()
        email = str(payload.get("email") or "").strip()
        subscription_summary = str(payload.get("subscription_summary") or "").strip()
        text = str(payload.get("text") or "").strip()
        telegram_id_raw = payload.get("telegram_id")
        telegram_id = int(telegram_id_raw) if telegram_id_raw not in (None, "", 0) else None
    except (KeyError, TypeError, ValueError):
        return web.json_response({"error": "invalid payload"}, status=400)

    if account_id <= 0 or shop_ticket_id <= 0 or not text:
        return web.json_response({"error": "invalid payload"}, status=400)

    try:
        result = await post_cabinet_message(
            bot,
            session_factory,
            settings,
            account_id=account_id,
            shop_ticket_id=shop_ticket_id,
            is_new_ticket=is_new_ticket,
            client_message_id=client_message_id,
            display_name=display_name,
            telegram_label=telegram_label,
            email=email,
            subscription_summary=subscription_summary,
            text=text,
            telegram_id=telegram_id,
        )
    except Exception as e:
        log.exception("cabinet message failed account_id=%s: %s", account_id, e)
        return web.json_response({"error": "failed"}, status=502)

    return web.json_response(result)


def create_app(bot, session_factory, settings) -> web.Application:
    app = web.Application()
    app["bot"] = bot
    app["session_factory"] = session_factory
    app["settings"] = settings
    app.router.add_post("/internal/cabinet/message", cabinet_message_handler)
    return app


async def start_http(app: web.Application, port: int):
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    log.info("HTTP API listening on 0.0.0.0:%s", port)
    return runner
