"""
Вложения клиента для модели: фото, голосовые, аудио и файлы, которые Gemini
понимает сам. Видео и кружки не отправляются — дорого и долго, с ними ИИ
передаёт тикет человеку.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import PurePosixPath

# Типы сообщений, вложения которых можно показать модели.
MEDIA_CONTENT_TYPES = frozenset({"photo", "voice", "audio", "document"})

# Расширение файла на серверах Telegram → MIME для Gemini. Telegram сохраняет
# расширение исходного файла (documents/file_12.pdf, music/file_3.mp3).
_MIME_BY_EXT = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".webp": "image/webp",
    ".heic": "image/heic",
    ".heif": "image/heif",
    ".ogg": "audio/ogg",
    ".oga": "audio/ogg",
    ".opus": "audio/ogg",
    ".mp3": "audio/mp3",
    ".wav": "audio/wav",
    ".aac": "audio/aac",
    ".flac": "audio/flac",
    ".aiff": "audio/aiff",
    ".pdf": "application/pdf",
    ".txt": "text/plain",
    ".log": "text/plain",
    ".csv": "text/plain",
    ".json": "text/plain",
    ".md": "text/plain",
}


@dataclass(frozen=True)
class Media:
    """Вложение, которое уходит модели вместе с сообщением клиента."""

    mime_type: str
    data: bytes


def media_mime(content_type: str, file_path: str | None) -> str | None:
    """MIME вложения или None, если модели его не показать."""
    if content_type == "photo":
        return "image/jpeg"
    if content_type == "voice":
        return "audio/ogg"
    if content_type not in MEDIA_CONTENT_TYPES or not file_path:
        return None
    return _MIME_BY_EXT.get(PurePosixPath(file_path).suffix.lower())
