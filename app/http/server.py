from __future__ import annotations

import hmac
import json
import logging

from aiohttp import BodyPartReader, web

from app.services import cabinet_media
from app.services import live_settings
from app.services.cabinet_bridge import post_cabinet_message
from app.services.cabinet_files import fetch_attachment
from app.services.cabinet_media import CabinetUpload

log = logging.getLogger(__name__)

_PAYLOAD_MAX_BYTES = 64 * 1024
_CHUNK = 64 * 1024


class _BadRequest(Exception):
    def __init__(self, status: int, error: str) -> None:
        super().__init__(error)
        self.status = status
        self.error = error


def _authorized(request: web.Request, settings) -> bool:
    secret = (settings.support_bridge_secret or "").strip()
    if not secret:
        return False
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        return False
    token = auth[7:].strip()
    return hmac.compare_digest(token, secret)


def _parse_payload(payload: dict, *, has_files: bool) -> dict:
    """Поля сообщения кабинета — одинаковые для JSON и части payload в multipart."""
    try:
        telegram_id_raw = payload.get("telegram_id")
        fields = {
            "account_id": int(payload["account_id"]),
            "shop_ticket_id": int(payload["shop_ticket_id"]),
            "is_new_ticket": bool(payload.get("is_new_ticket")),
            "client_message_id": str(payload.get("client_message_id") or "").strip() or None,
            "display_name": str(payload.get("display_name") or "").strip(),
            "telegram_label": str(payload.get("telegram_label") or "").strip(),
            "email": str(payload.get("email") or "").strip(),
            "subscription_summary": str(payload.get("subscription_summary") or "").strip(),
            "text": str(payload.get("text") or "").strip(),
            "telegram_id": int(telegram_id_raw) if telegram_id_raw not in (None, "", 0) else None,
        }
    except (KeyError, TypeError, ValueError, AttributeError):
        raise _BadRequest(400, "invalid payload") from None
    if fields["account_id"] <= 0 or fields["shop_ticket_id"] <= 0:
        raise _BadRequest(400, "invalid payload")
    # Без текста можно только с вложениями.
    if not fields["text"] and not has_files:
        raise _BadRequest(400, "invalid payload")
    return fields


async def _read_file_part(part, total_before: int) -> CabinetUpload:
    buf = bytearray()
    while chunk := await part.read_chunk(_CHUNK):
        buf += chunk
        if len(buf) > cabinet_media.MAX_FILE_BYTES or total_before + len(buf) > cabinet_media.MAX_TOTAL_BYTES:
            raise _BadRequest(413, "file_too_large")
    data = bytes(buf)
    mime = cabinet_media.detect_image_mime(data)
    if mime is None:
        raise _BadRequest(415, "unsupported_media")
    return CabinetUpload(data=data, mime=mime, filename=cabinet_media.safe_filename(part.filename, mime))


async def _read_multipart(request: web.Request) -> tuple[dict, list[CabinetUpload]]:
    """
    multipart/form-data: часть payload (JSON с полями сообщения) и части files.
    Читаем потоком и обрываем на лимитах, не дожидаясь всего тела.
    """
    reader = await request.multipart()
    payload: dict | None = None
    files: list[CabinetUpload] = []
    total = 0
    while True:
        part = await reader.next()
        if part is None:
            break
        if not isinstance(part, BodyPartReader):
            raise _BadRequest(400, "invalid payload")
        if part.name == "payload":
            raw = await part.read()
            if len(raw) > _PAYLOAD_MAX_BYTES:
                raise _BadRequest(400, "invalid payload")
            try:
                payload = json.loads(raw)
            except (UnicodeDecodeError, json.JSONDecodeError):
                raise _BadRequest(400, "invalid payload") from None
        elif part.name == "files":
            if len(files) >= cabinet_media.MAX_FILES:
                raise _BadRequest(400, "too_many_files")
            upload = await _read_file_part(part, total)
            total += len(upload.data)
            files.append(upload)
        else:
            await part.release()
    if not isinstance(payload, dict):
        raise _BadRequest(400, "invalid payload")
    return payload, files


async def cabinet_message_handler(request: web.Request) -> web.Response:
    # С учётом изменений в боте (⚙️ Настройки): шаблон имени темы, значки.
    settings = live_settings.current(request.app["settings"])
    bot = request.app["bot"]
    session_factory = request.app["session_factory"]

    if not _authorized(request, settings):
        return web.json_response({"error": "unauthorized"}, status=401)

    try:
        if request.content_type == "multipart/form-data":
            payload, files = await _read_multipart(request)
        else:
            try:
                payload = await request.json()
            except json.JSONDecodeError:
                return web.json_response({"error": "invalid json"}, status=400)
            if not isinstance(payload, dict):
                raise _BadRequest(400, "invalid payload")
            files = []
        fields = _parse_payload(payload, has_files=bool(files))
    except _BadRequest as e:
        return web.json_response({"error": e.error}, status=e.status)

    account_id = fields["account_id"]
    try:
        result = await post_cabinet_message(
            bot,
            session_factory,
            settings,
            files=files,
            **fields,
        )
    except Exception as e:
        log.exception("cabinet message failed account_id=%s: %s", account_id, e)
        return web.json_response({"error": "failed"}, status=502)

    return web.json_response(result)


async def cabinet_file_handler(request: web.Request) -> web.Response:
    """Изображение из тикета кабинета — магазин проксирует его в кабинет клиента."""
    settings = live_settings.current(request.app["settings"])
    if not _authorized(request, settings):
        return web.json_response({"error": "unauthorized"}, status=401)
    try:
        attachment_id = int(request.match_info["id"])
        account_id = int(request.query["account_id"])
    except (KeyError, ValueError):
        return web.json_response({"error": "invalid request"}, status=400)

    try:
        found = await fetch_attachment(
            request.app["bot"],
            request.app["session_factory"],
            attachment_id=attachment_id,
            account_id=account_id,
        )
    except Exception as e:
        log.warning("cabinet file %s not downloaded: %s", attachment_id, e)
        return web.json_response({"error": "failed"}, status=502)
    if found is None:
        return web.json_response({"error": "not_found"}, status=404)

    data, mime = found
    return web.Response(
        body=data,
        content_type=mime,
        headers={"Cache-Control": "private, max-age=86400, immutable"},
    )


def create_app(bot, session_factory, settings) -> web.Application:
    app = web.Application()
    app["bot"] = bot
    app["session_factory"] = session_factory
    app["settings"] = settings
    app.router.add_post("/internal/cabinet/message", cabinet_message_handler)
    app.router.add_get("/internal/cabinet/file/{id:\\d+}", cabinet_file_handler)
    return app


async def start_http(app: web.Application, port: int):
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    log.info("HTTP API listening on 0.0.0.0:%s", port)
    return runner
