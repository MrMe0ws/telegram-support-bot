"""
Изображения в чате кабинета: что принимаем от клиента, как отправляем в тему
и что рассказываем о вложении магазину.

Форматы те же, что клиент может прислать боту в Telegram. JPEG, PNG, WebP и BMP
уходят в тему фото — Telegram их пережмёт и покажет картинкой. GIF, HEIC/HEIF,
AVIF и TIFF уходят файлом: фото из них Telegram не делает (а GIF превратил бы
в видео), файл сохраняет оригинал.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import PurePath

MAX_FILES = 5
MAX_FILE_BYTES = 10 * 1024 * 1024  # лимит sendPhoto
MAX_TOTAL_BYTES = 25 * 1024 * 1024
# Больше Bot API скачать не даст — такое вложение кабинет не покажет.
MAX_DOWNLOAD_BYTES = 20 * 1024 * 1024

PHOTO_MIMES = frozenset({"image/jpeg", "image/png", "image/webp", "image/bmp"})
FILE_MIMES = frozenset({"image/gif", "image/heic", "image/heif", "image/avif", "image/tiff"})
IMAGE_MIMES = PHOTO_MIMES | FILE_MIMES

EXT_BY_MIME = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
    "image/bmp": ".bmp",
    "image/gif": ".gif",
    "image/heic": ".heic",
    "image/heif": ".heif",
    "image/avif": ".avif",
    "image/tiff": ".tiff",
}
_MIME_BY_EXT = {ext: mime for mime, ext in EXT_BY_MIME.items()} | {
    ".jpeg": "image/jpeg",
    ".tif": "image/tiff",
}

_HEIC_BRANDS = {b"heic", b"heix", b"heim", b"heis", b"hevc", b"hevx"}
_HEIF_BRANDS = {b"mif1", b"msf1"}
_AVIF_BRANDS = {b"avif", b"avis"}


def detect_image_mime(data: bytes) -> str | None:
    """MIME по первым байтам файла; None — не изображение из списка."""
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if data[:2] == b"BM" and len(data) >= 18 and int.from_bytes(data[14:18], "little") in (12, 40, 52, 56, 64, 108, 124):
        return "image/bmp"
    if data[:4] in (b"II*\x00", b"MM\x00*"):
        return "image/tiff"
    if data[4:8] == b"ftyp":
        return _isobmff_mime(data)
    return None


def _isobmff_mime(data: bytes) -> str | None:
    """HEIC/HEIF/AVIF: основной бренд ftyp и совместимые бренды за ним."""
    box_size = int.from_bytes(data[0:4], "big")
    end = min(max(box_size, 16), len(data), 64)
    brands = [data[8:12]] + [data[i : i + 4] for i in range(16, end - 3, 4)]
    if any(b in _AVIF_BRANDS for b in brands):
        return "image/avif"
    if any(b in _HEIC_BRANDS for b in brands):
        return "image/heic"
    if any(b in _HEIF_BRANDS for b in brands):
        return "image/heif"
    return None


def mime_by_name(file_name: str | None) -> str | None:
    if not file_name:
        return None
    return _MIME_BY_EXT.get(PurePath(file_name).suffix.lower())


def safe_filename(name: str | None, mime: str) -> str:
    """Имя для Telegram: без пути, с расширением, которое соответствует содержимому."""
    stem = PurePath((name or "").replace("\\", "/")).stem.strip()[:64] or "image"
    return stem + EXT_BY_MIME[mime]


@dataclass(frozen=True)
class CabinetUpload:
    """Файл, который клиент приложил в кабинете."""

    data: bytes
    mime: str
    filename: str

    @property
    def as_photo(self) -> bool:
        return self.mime in PHOTO_MIMES


@dataclass(frozen=True)
class AttachmentMeta:
    """Вложение в Telegram: как его хранить в messages и что о нём знает магазин."""

    content_type: str  # photo | document
    file_id: str
    mime: str
    width: int | None = None
    height: int | None = None
    size: int | None = None
    name: str | None = None

    def to_json(self) -> str:
        """Поле messages.attachment_meta (file_id и content_type лежат в своих колонках)."""
        d = asdict(self)
        del d["content_type"], d["file_id"]
        return json.dumps({k: v for k, v in d.items() if v is not None}, ensure_ascii=False)

    def to_api(self, attachment_id: int) -> dict:
        """Описание вложения для магазина: id — строка messages, по нему файл отдаётся."""
        out: dict = {"id": attachment_id, "mime": self.mime}
        for key in ("width", "height", "size", "name"):
            value = getattr(self, key)
            if value is not None:
                out[key] = value
        return out


def meta_from_row(content_type: str, file_id: str | None, raw: str | None) -> AttachmentMeta | None:
    """Вложение из строки messages; None — у строки нет изображения."""
    if not file_id or content_type not in ("photo", "document"):
        return None
    try:
        d = json.loads(raw) if raw else {}
    except (TypeError, ValueError):
        d = {}
    mime = d.get("mime") or ("image/jpeg" if content_type == "photo" else None)
    if mime not in IMAGE_MIMES:
        return None
    return AttachmentMeta(
        content_type=content_type,
        file_id=file_id,
        mime=mime,
        width=d.get("width"),
        height=d.get("height"),
        size=d.get("size"),
        name=d.get("name"),
    )


def meta_from_sent(message, upload: CabinetUpload) -> AttachmentMeta:
    """Вложение из ответа send_photo / send_document / элемента send_media_group."""
    if message.photo:
        p = message.photo[-1]
        return AttachmentMeta("photo", p.file_id, "image/jpeg", p.width, p.height, p.file_size)
    d = message.document
    return AttachmentMeta(
        "document", d.file_id, upload.mime, size=d.file_size or len(upload.data), name=upload.filename
    )


def staff_attachment(message) -> AttachmentMeta | None:
    """Изображение в ответе сотрудника: сжатое фото или картинка файлом (скриншот с ПК)."""
    if message.photo:
        p = message.photo[-1]
        return AttachmentMeta("photo", p.file_id, "image/jpeg", p.width, p.height, p.file_size)
    d = message.document
    if d is None:
        return None
    mime = (d.mime_type or "").lower()
    if mime not in IMAGE_MIMES:
        mime = mime_by_name(d.file_name) or ""
    if mime not in IMAGE_MIMES:
        return None
    if d.file_size and d.file_size > MAX_DOWNLOAD_BYTES:
        return None
    return AttachmentMeta("document", d.file_id, mime, size=d.file_size, name=d.file_name)
