from __future__ import annotations

import asyncio
import io
import json
import os
from types import SimpleNamespace

import pytest
from aiogram.exceptions import TelegramBadRequest
from aiogram.methods import SendPhoto
from aiohttp import FormData
from aiohttp.test_utils import TestClient, TestServer

os.environ.setdefault("TEXTS_DIR", os.path.join(os.path.dirname(__file__), "..", "texts"))

from app.config import AiSettings, RemnawaveSettings, Settings  # noqa: E402
from app.db.models import MessageSource, StoredMessage  # noqa: E402
from app.db.session import create_engine, init_db, session_factory  # noqa: E402
from app.handlers import group_topics  # noqa: E402
from app.http import server as http_server  # noqa: E402
from app.services import cabinet_bridge, cabinet_media, live_settings  # noqa: E402
from app.services import shop_webhook as shop_webhook_svc  # noqa: E402
from app.services import tickets as ticket_svc  # noqa: E402

GROUP_ID = -1001
SECRET = "s3cret"
ACCOUNT = 42
AUTH = {"Authorization": f"Bearer {SECRET}"}

PNG = b"\x89PNG\r\n\x1a\n" + b"p" * 200
JPEG = b"\xff\xd8\xff\xe0" + b"j" * 200
WEBP = b"RIFF\x00\x00\x00\x00WEBPVP8 " + b"w" * 200
GIF = b"GIF89a" + b"g" * 200
BMP = b"BM" + b"\x00" * 12 + (40).to_bytes(4, "little") + b"b" * 200
TIFF = b"II*\x00" + b"t" * 200
HEIC = b"\x00\x00\x00\x18ftypheic\x00\x00\x00\x00mif1heic" + b"h" * 200
AVIF = b"\x00\x00\x00\x1cftypavif\x00\x00\x00\x00avifmif1miaf" + b"a" * 200


# ── Форматы ─────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("data", "mime"),
    [
        (JPEG, "image/jpeg"),
        (PNG, "image/png"),
        (WEBP, "image/webp"),
        (GIF, "image/gif"),
        (BMP, "image/bmp"),
        (TIFF, "image/tiff"),
        (HEIC, "image/heic"),
        (AVIF, "image/avif"),
        (b"\x00\x00\x00\x18ftypmif1\x00\x00\x00\x00mif1miaf", "image/heif"),
        (b"\x00\x00\x00\x18ftypisom\x00\x00\x00\x00isommp41", None),  # mp4
        (b"%PDF-1.7", None),
        (b"BM-not-a-bitmap", None),
        (b"", None),
    ],
)
def test_detect_image_mime(data: bytes, mime: str | None) -> None:
    assert cabinet_media.detect_image_mime(data) == mime


def test_safe_filename_matches_content() -> None:
    assert cabinet_media.safe_filename("C:\\Users\\me\\Скрин 1.PNG", "image/png") == "Скрин 1.png"
    assert cabinet_media.safe_filename("photo.jpeg", "image/heic") == "photo.heic"
    assert cabinet_media.safe_filename(None, "image/jpeg") == "image.jpg"


# ── Фейковый Telegram ───────────────────────────────────────────────────


def _bad_request(text: str) -> TelegramBadRequest:
    return TelegramBadRequest(method=SendPhoto(chat_id=1, photo="x"), message=text)


class FakeBot:
    id = 1

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []
        self.files: dict[str, bytes] = {}
        self.reject_photos = False
        self.reject_album = False
        self.fail = False
        self._next_id = 100

    def _new_id(self) -> int:
        self._next_id += 1
        return self._next_id

    def _photo(self, data: bytes) -> SimpleNamespace:
        fid = f"photo-{self._next_id + 1}"
        self.files[fid] = data
        return SimpleNamespace(
            message_id=self._new_id(),
            photo=[
                SimpleNamespace(file_id="thumb", width=90, height=50, file_size=10),
                SimpleNamespace(file_id=fid, width=1280, height=720, file_size=len(data)),
            ],
            document=None,
        )

    async def create_forum_topic(self, chat_id, name, **kw):
        return SimpleNamespace(message_thread_id=77)

    async def edit_forum_topic(self, **kw):
        return True

    async def send_message(self, chat_id, text, **kw):
        self.calls.append(("message", {"text": text}))
        return SimpleNamespace(message_id=self._new_id())

    async def send_photo(self, chat_id, photo, caption=None, **kw):
        self.calls.append(("photo", {"caption": caption, "filename": photo.filename}))
        if self.fail:
            raise RuntimeError("telegram down")
        if self.reject_photos:
            raise _bad_request("PHOTO_INVALID_DIMENSIONS")
        return self._photo(photo.data)

    async def send_document(self, chat_id, document, caption=None, **kw):
        self.calls.append(("document", {"caption": caption, "filename": document.filename, **kw}))
        fid = f"doc-{self._next_id + 1}"
        self.files[fid] = document.data
        return SimpleNamespace(
            message_id=self._new_id(),
            photo=None,
            document=SimpleNamespace(file_id=fid, file_size=len(document.data)),
        )

    async def send_media_group(self, chat_id, media, **kw):
        self.calls.append(("album", {"captions": [m.caption for m in media]}))
        if self.reject_album:
            raise _bad_request("MEDIA_INVALID")
        return [self._photo(m.media.data) for m in media]

    async def get_file(self, file_id):
        return SimpleNamespace(file_id=file_id, file_path=f"files/{file_id}", file_size=len(self.files[file_id]))

    async def download_file(self, file_path):
        return io.BytesIO(self.files[file_path.split("/", 1)[1]])

    def kinds(self) -> list[str]:
        return [k for k, _ in self.calls]


def _settings() -> Settings:
    rw = RemnawaveSettings(
        enabled=False, base_url="", mode="local", token="", sub_public_url="",
        gate_query_name="", gate_query_value="", gate_cookie="", timeout_sec=5,
        timezone="UTC", http_host="", proxy_headers=False, forwarded_for="", forwarded_proto="",
    )
    ai = AiSettings(
        enabled=False, mode="auto", provider="gemini", models=("gemini:m",), api_keys=(),
        proxy_url="", reply_delay_sec=0, knowledge_dir="", history_limit=30,
        max_replies_per_ticket=3, timeout_sec=10, assistant_name="Бот",
    )
    return Settings(
        bot_token="t", support_group_id=GROUP_ID, admin_ids=frozenset({5}),
        database_url="", topic_name_template="{username}", topic_icon_emoji_open="",
        topic_icon_emoji_closed="", shop_webhook_url="http://shop/webhook",
        support_bridge_secret=SECRET, http_port=0, remnawave=rw, ai=ai,
    )


@pytest.fixture
def isolated(monkeypatch):
    """Глобальные настройки и ИИ, которые оставляют другие тесты, сюда не протекают."""
    monkeypatch.setattr(live_settings, "current", lambda fallback=None: fallback)
    monkeypatch.setattr(cabinet_bridge, "get_ai_service", lambda: None)
    monkeypatch.setattr(group_topics, "peek_ai_service", lambda: None)


def _run(tmp_path, scenario):
    async def main():
        engine = create_engine(f"sqlite+aiosqlite:///{tmp_path / 'db.sqlite'}")
        await init_db(engine)
        sf = session_factory(engine)
        bot = FakeBot()
        client = TestClient(TestServer(http_server.create_app(bot, sf, _settings())))
        await client.start_server()
        try:
            await scenario(client, bot, sf)
        finally:
            await client.close()
            await engine.dispose()

    asyncio.run(main())


def _payload(**kw) -> dict:
    base = {
        "account_id": ACCOUNT, "shop_ticket_id": 7, "is_new_ticket": True,
        "display_name": "@client", "telegram_label": "@client", "email": "c***t@mail.ru",
        "subscription_summary": "active", "text": "",
    }
    return base | kw


def _form(payload: dict, *files: tuple[str, bytes]) -> FormData:
    form = FormData()
    form.add_field("payload", json.dumps(payload), content_type="application/json")
    for name, data in files:
        form.add_field("files", data, filename=name, content_type="application/octet-stream")
    return form


async def _post(client, payload: dict, *files: tuple[str, bytes], headers=AUTH):
    resp = await client.post("/internal/cabinet/message", data=_form(payload, *files), headers=headers)
    return resp.status, await resp.json()


async def _rows(sf) -> list:
    async with sf() as session:
        return (await session.execute(StoredMessage.__table__.select().order_by(StoredMessage.id))).fetchall()


# ── Сообщение клиента с вложениями ──────────────────────────────────────


def test_album_without_text(tmp_path, isolated) -> None:
    async def scenario(client, bot, sf):
        status, body = await _post(
            client, _payload(client_message_id="11111111-1111-1111-1111-111111111111"),
            ("a.png", PNG), ("b.jpg", JPEG), ("c.webp", WEBP),
        )
        assert status == 200
        assert body["thread_id"] == 77
        assert [a["mime"] for a in body["attachments"]] == ["image/jpeg"] * 3
        assert body["attachments"][0]["width"] == 1280

        # Шапка обращения — подписью к первому фото альбома.
        assert bot.kinds() == ["album"]
        captions = bot.calls[0][1]["captions"]
        assert "Обращение из кабинета" in captions[0] and "см. вложения" in captions[0]
        assert captions[1:] == [None, None]

        rows = await _rows(sf)
        assert [r.content_type for r in rows] == ["photo"] * 3
        assert [r.id for r in rows] == [a["id"] for a in body["attachments"]]
        assert all(r.telegram_file_id and r.telegram_message_id for r in rows)
        assert [r.client_message_id is not None for r in rows] == [True, False, False]

    _run(tmp_path, scenario)


def test_short_text_is_caption_long_text_goes_first(tmp_path, isolated) -> None:
    async def scenario(client, bot, sf):
        status, _ = await _post(client, _payload(text="вот ошибка"), ("a.jpg", JPEG))
        assert status == 200
        assert bot.kinds() == ["photo"]
        assert "вот ошибка" in bot.calls[0][1]["caption"]

        bot.calls.clear()
        status, _ = await _post(client, _payload(is_new_ticket=False, text="д" * 1500), ("a.jpg", JPEG))
        assert status == 200
        assert bot.kinds() == ["message", "photo"]
        assert bot.calls[1][1]["caption"] is None

        rows = await _rows(sf)
        assert rows[-1].text == "д" * 1500

    _run(tmp_path, scenario)


def test_rejected_album_and_photo_fall_back_to_files(tmp_path, isolated) -> None:
    async def scenario(client, bot, sf):
        bot.reject_album = True
        bot.reject_photos = True
        status, body = await _post(client, _payload(text="длинный скрин"), ("long.png", PNG), ("b.png", PNG))
        assert status == 200
        assert bot.kinds() == ["album", "photo", "document", "photo", "document"]
        assert "длинный скрин" in bot.calls[2][1]["caption"] and bot.calls[4][1]["caption"] is None
        assert [(a["mime"], a["name"]) for a in body["attachments"]] == [
            ("image/png", "long.png"),
            ("image/png", "b.png"),
        ]
        assert [r.content_type for r in await _rows(sf)] == ["document", "document"]

    _run(tmp_path, scenario)


def test_gif_heic_avif_tiff_go_as_files(tmp_path, isolated) -> None:
    async def scenario(client, bot, sf):
        status, body = await _post(
            client, _payload(text="с айфона"),
            ("anim.gif", GIF), ("IMG_1.HEIC", HEIC), ("x.avif", AVIF), ("scan.tif", TIFF),
        )
        assert status == 200
        assert bot.kinds() == ["document"] * 4
        assert all(c[1]["disable_content_type_detection"] for c in bot.calls)
        assert [a["mime"] for a in body["attachments"]] == ["image/gif", "image/heic", "image/avif", "image/tiff"]
        assert [c[1]["filename"] for c in bot.calls] == ["anim.gif", "IMG_1.heic", "x.avif", "scan.tiff"]

    _run(tmp_path, scenario)


def test_bmp_goes_as_photo(tmp_path, isolated) -> None:
    async def scenario(client, bot, sf):
        status, body = await _post(client, _payload(text="bmp"), ("paint.bmp", BMP))
        assert status == 200
        assert bot.kinds() == ["photo"]
        assert body["attachments"][0]["mime"] == "image/jpeg"

    _run(tmp_path, scenario)


def test_limits_and_errors(tmp_path, isolated, monkeypatch) -> None:
    monkeypatch.setattr(cabinet_media, "MAX_FILE_BYTES", 1000)
    monkeypatch.setattr(cabinet_media, "MAX_TOTAL_BYTES", 1500)

    async def scenario(client, bot, sf):
        assert await _post(client, _payload(), *[("a.png", PNG)] * 6) == (400, {"error": "too_many_files"})
        assert await _post(client, _payload(), ("big.png", PNG + b"x" * 1000)) == (413, {"error": "file_too_large"})
        assert await _post(client, _payload(), ("a.png", PNG + b"x" * 700), ("b.png", PNG + b"x" * 700)) == (
            413, {"error": "file_too_large"},
        )
        assert await _post(client, _payload(), ("fake.png", b"%PDF-1.7 not an image")) == (
            415, {"error": "unsupported_media"},
        )
        assert await _post(client, _payload(text="")) == (400, {"error": "invalid payload"})
        assert await _post(client, _payload(account_id=0), ("a.png", PNG)) == (400, {"error": "invalid payload"})
        assert await _post(client, _payload(), ("a.png", PNG), headers={}) == (401, {"error": "unauthorized"})

        form = FormData()
        form.add_field("files", PNG, filename="a.png")
        resp = await client.post("/internal/cabinet/message", data=form, headers=AUTH)
        assert resp.status == 400

        assert bot.calls == []
        assert await _rows(sf) == []

    _run(tmp_path, scenario)


def test_json_request_works_as_before(tmp_path, isolated) -> None:
    async def scenario(client, bot, sf):
        resp = await client.post("/internal/cabinet/message", json=_payload(text="привет"), headers=AUTH)
        assert resp.status == 200
        assert (await resp.json())["attachments"] == []
        assert bot.kinds() == ["message"]
        rows = await _rows(sf)
        assert [(r.content_type, r.text) for r in rows] == [("text", "привет")]

        resp = await client.post("/internal/cabinet/message", json=_payload(text=""), headers=AUTH)
        assert resp.status == 400

    _run(tmp_path, scenario)


def test_retry_with_same_client_message_id(tmp_path, isolated) -> None:
    async def scenario(client, bot, sf):
        cmid = "22222222-2222-2222-2222-222222222222"
        first = await _post(client, _payload(client_message_id=cmid, text="скрины"), ("a.png", PNG), ("b.png", PNG))
        sent = len(bot.calls)
        again = await _post(client, _payload(client_message_id=cmid, text="скрины"), ("a.png", PNG), ("b.png", PNG))
        assert again == first
        assert len(again[1]["attachments"]) == 2
        assert len(bot.calls) == sent
        assert len(await _rows(sf)) == 2

    _run(tmp_path, scenario)


def test_failed_send_leaves_no_rows(tmp_path, isolated) -> None:
    async def scenario(client, bot, sf):
        bot.fail = True
        assert await _post(client, _payload(text="скрин"), ("a.png", PNG)) == (502, {"error": "failed"})
        assert await _rows(sf) == []

    _run(tmp_path, scenario)


# ── Отдача файла магазину ───────────────────────────────────────────────


def test_file_endpoint(tmp_path, isolated) -> None:
    async def scenario(client, bot, sf):
        _, body = await _post(client, _payload(text="скрин"), ("a.png", PNG), ("anim.gif", GIF))
        photo_id, gif_id = (a["id"] for a in body["attachments"])

        resp = await client.get(f"/internal/cabinet/file/{photo_id}?account_id={ACCOUNT}", headers=AUTH)
        assert resp.status == 200
        assert await resp.read() == PNG
        assert resp.headers["Content-Type"] == "image/jpeg"
        assert "immutable" in resp.headers["Cache-Control"]

        resp = await client.get(f"/internal/cabinet/file/{gif_id}?account_id={ACCOUNT}", headers=AUTH)
        assert resp.status == 200 and resp.headers["Content-Type"] == "image/gif"

        async def status(path: str, headers=AUTH) -> int:
            return (await client.get(path, headers=headers)).status

        assert await status(f"/internal/cabinet/file/{photo_id}?account_id=999") == 404
        assert await status(f"/internal/cabinet/file/{photo_id}") == 400
        assert await status(f"/internal/cabinet/file/99999?account_id={ACCOUNT}") == 404
        assert await status(f"/internal/cabinet/file/{photo_id}?account_id={ACCOUNT}", headers={}) == 401

        # Тикет из Telegram магазину не отдаётся, даже если номер совпал.
        async with sf() as session:
            async with session.begin():
                tg = await ticket_svc.create_ticket(
                    session, user_id=ACCOUNT, forum_chat_id=GROUP_ID, thread_id=5,
                    source=MessageSource.telegram.value,
                )
                row = await ticket_svc.record_message(
                    session, ticket_id=tg.id, direction="in", content_type="photo", text=None,
                    telegram_file_id=next(iter(bot.files)), telegram_message_id=1,
                )
        assert await status(f"/internal/cabinet/file/{row.id}?account_id={ACCOUNT}") == 404

    _run(tmp_path, scenario)


# ── Ответ сотрудника в тему ─────────────────────────────────────────────


def _staff_message(**kw) -> SimpleNamespace:
    base = dict(
        chat=SimpleNamespace(id=GROUP_ID), from_user=SimpleNamespace(id=5, username="admin", full_name="Admin"),
        message_id=900, text=None, caption=None, content_type="photo", photo=None, document=None,
        video=None, audio=None, voice=None, video_note=None, sticker=None,
    )
    return SimpleNamespace(**(base | kw))


def test_staff_images_reach_cabinet(tmp_path, isolated, monkeypatch) -> None:
    payloads: list[dict] = []

    async def fake_post(settings, payload, *, quiet=False):
        payloads.append(payload)

    monkeypatch.setattr(shop_webhook_svc, "_post_webhook", fake_post)

    async def scenario(client, bot, sf):
        await _post(client, _payload(text="помогите"), ("a.png", PNG))
        settings = _settings()
        bot.files["staff-photo"] = JPEG
        bot.files["staff-doc"] = PNG

        photo = [SimpleNamespace(file_id="staff-photo", width=800, height=600, file_size=len(JPEG))]
        await group_topics.staff_reply(_staff_message(photo=photo), bot, 77, sf, settings)

        doc = SimpleNamespace(file_id="staff-doc", mime_type="image/png", file_name="screen.png", file_size=len(PNG))
        await group_topics.staff_reply(
            _staff_message(message_id=901, content_type="document", document=doc, caption="вот так"),
            bot, 77, sf, settings,
        )

        video = SimpleNamespace(file_id="vid")
        await group_topics.staff_reply(
            _staff_message(message_id=902, content_type="video", video=video), bot, 77, sf, settings,
        )

        assert payloads[0]["text"] == ""
        assert payloads[0]["attachments"][0] | {"id": 0} == {
            "id": 0, "mime": "image/jpeg", "width": 800, "height": 600, "size": len(JPEG),
        }
        assert payloads[1]["text"] == "вот так"
        assert payloads[1]["attachments"][0]["mime"] == "image/png"
        assert payloads[1]["attachments"][0]["name"] == "screen.png"
        assert payloads[2]["text"] == "[💬 сообщение поддержки]"
        assert "attachments" not in payloads[2]

        # Картинку сотрудника кабинет скачивает тем же путём, что и картинку клиента.
        staff_id = payloads[1]["attachments"][0]["id"]
        resp = await client.get(f"/internal/cabinet/file/{staff_id}?account_id={ACCOUNT}", headers=AUTH)
        assert resp.status == 200 and await resp.read() == PNG

    _run(tmp_path, scenario)
