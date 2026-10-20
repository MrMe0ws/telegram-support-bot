from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)

# Порог для предупреждения в логе: дальше база заметно дорожает и упирается
# в бесплатные лимиты провайдера (~4 символа на токен).
_WARN_CHARS = 400_000


@dataclass(frozen=True)
class KnowledgeBase:
    text: str
    files: tuple[str, ...]


class KnowledgeLoader:
    """
    База знаний — все *.md из папки (с подпапками), склеенные в один текст.

    Перечитывается, только когда файлы поменялись: админ правит .md на сервере,
    и следующий ответ ИИ уже идёт по новой версии — без перезапуска бота.
    """

    def __init__(self, directory: str) -> None:
        self._dir = Path(directory)
        self._signature: tuple | None = None
        self._cached = KnowledgeBase(text="", files=())

    def _scan(self) -> list[Path]:
        if not self._dir.is_dir():
            return []
        return sorted(p for p in self._dir.rglob("*.md") if p.is_file())

    def load(self) -> KnowledgeBase:
        paths = self._scan()
        try:
            signature = tuple((str(p), p.stat().st_mtime_ns, p.stat().st_size) for p in paths)
        except OSError:
            signature = None
        if signature is not None and signature == self._signature:
            return self._cached

        parts: list[str] = []
        names: list[str] = []
        for p in paths:
            try:
                body = p.read_text(encoding="utf-8").strip()
            except (OSError, UnicodeDecodeError) as e:
                log.warning("База знаний: не прочитан %s: %s", p, e)
                continue
            if not body:
                continue
            rel = p.relative_to(self._dir).as_posix()
            names.append(rel)
            parts.append(f"### Файл: {rel}\n\n{body}")

        text = "\n\n".join(parts)
        if len(text) > _WARN_CHARS:
            log.warning(
                "База знаний большая: %s символов — ответы будут дороже и медленнее",
                len(text),
            )
        if not names:
            log.warning("База знаний пуста: нет .md в %s", self._dir.resolve())
        else:
            log.info("База знаний загружена: %s файл(ов), %s символов", len(names), len(text))

        self._signature = signature
        self._cached = KnowledgeBase(text=text, files=tuple(names))
        return self._cached
