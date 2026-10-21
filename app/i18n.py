from __future__ import annotations

import logging
import os
from pathlib import Path

import yaml

log = logging.getLogger(__name__)


def _project_root() -> Path:
    return Path(__file__).resolve().parent.parent


def _texts_base() -> Path:
    env = os.environ.get("TEXTS_DIR")
    if env:
        return Path(env).expanduser()
    return _project_root() / "texts"


def _locale() -> str:
    return os.environ.get("BOT_LOCALE", "ru").strip().lower()


def _load_bundle() -> dict[str, dict]:
    folder = _texts_base() / _locale()
    if not folder.is_dir():
        raise FileNotFoundError(f"Нет каталога текстов: {folder}")

    merged: dict[str, dict] = {}
    for pattern in ("*.yml", "*.yaml"):
        for path in sorted(folder.glob(pattern)):
            with open(path, encoding="utf-8") as fp:
                data = yaml.safe_load(fp)
            if not data:
                continue
            stem = path.stem
            if stem in merged:
                merged[stem].update(data)
            else:
                merged[stem] = dict(data)

    log.info(
        "Тексты: locale=%s каталог=%s блоков=%s",
        _locale(),
        folder,
        len(merged),
    )
    return merged


_BUNDLE: dict[str, dict] | None = None


def texts_bundle() -> dict[str, dict]:
    global _BUNDLE
    if _BUNDLE is None:
        _BUNDLE = _load_bundle()
    return _BUNDLE


def reload_texts() -> None:
    """Сброс кэша при хот-релоаде (перезапуск процесса проще)."""
    global _BUNDLE
    _BUNDLE = None


# Тексты, переписанные админом в боте: (файл, ключ) → строка. Важнее YAML.
_OVERRIDES: dict[tuple[str, str], str] = {}


def set_text_overrides(overrides: dict[tuple[str, str], str]) -> None:
    _OVERRIDES.clear()
    _OVERRIDES.update(overrides)


def tr_default(stem: str, key: str) -> str:
    """Строка из YAML без учёта правок в боте."""
    return _lookup((stem, key), {}, use_overrides=False)


def tr(*parts: str, **kwargs: str | int | float) -> str:
    """
    Доступ к строкам из texts/<locale>/<имя>.yml:
        tr("user", "start")
        tr("admin", "block_success", uid=123)

    Имена файлов без расширения — первый аргумент (user, admin, flow, …).
    """
    if len(parts) < 2:
        raise ValueError("Нужно минимум два аргумента: блок_файла, ключ")
    return _lookup(parts, kwargs, use_overrides=True)


def _lookup(parts: tuple[str, ...], kwargs: dict, *, use_overrides: bool) -> str:
    if use_overrides and len(parts) == 2 and parts in _OVERRIDES:
        return _OVERRIDES[parts]  # type: ignore[index]

    bundle = texts_bundle()
    stem = parts[0]
    rest = parts[1:]

    if stem not in bundle:
        missing = ".".join(parts)
        log.warning("Нет блока текстов «%s»", stem)
        return f"‹{missing}›"

    node: object = bundle[stem]
    for key in rest:
        if isinstance(node, dict) and key in node:
            node = node[key]
        else:
            missing = ".".join(parts)
            log.warning("Нет ключа текстов «%s»", missing)
            return f"‹{missing}›"

    if not isinstance(node, str):
        missing = ".".join(parts)
        return f"‹{missing}:not-string›"

    text = node.strip()
    if kwargs:
        try:
            return text.format(**kwargs)
        except KeyError as e:
            log.warning(
                "Плейсхолдер для «%s»: %s",
                ".".join(parts),
                e,
            )
            return text
    return text
