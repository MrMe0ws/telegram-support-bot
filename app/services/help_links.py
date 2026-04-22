from __future__ import annotations

import logging
from pathlib import Path

import yaml
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import HelpMenuLink

log = logging.getLogger(__name__)


def url_like(u: str) -> bool:
    ul = u.strip().lower()
    return ul.startswith(("http://", "https://", "tg://"))


def parse_help_payload(raw: str) -> tuple[str | None, str | None]:
    """
    Одна строка с http(s) или tg:// → внешняя ссылка кнопки.
    Иначе → текст статьи (HTML или plain с автоссылками на стороне показа).
    """
    raw = raw.strip()
    if not raw:
        raise ValueError("empty")
    single_line = "\n" not in raw
    if single_line and url_like(raw):
        return raw, None
    return None, raw


async def list_ordered(session: AsyncSession) -> list[HelpMenuLink]:
    q = await session.execute(
        select(HelpMenuLink).order_by(HelpMenuLink.position.asc(), HelpMenuLink.id.asc())
    )
    return list(q.scalars().all())


async def get_by_id(session: AsyncSession, link_id: int) -> HelpMenuLink | None:
    return await session.get(HelpMenuLink, link_id)


async def add_link(
    session: AsyncSession,
    *,
    title: str,
    url: str = "",
    body_text: str | None = None,
) -> HelpMenuLink:
    t = title.strip()[:255]
    u = (url or "").strip()
    b = (body_text or "").strip() or None
    is_direct = url_like(u)
    if is_direct and b:
        raise ValueError("help_link_url_and_body")
    if is_direct:
        final_url, final_body = u, None
    elif b:
        final_url, final_body = "", b
    else:
        raise ValueError("help_link_empty")

    r = await session.execute(select(func.coalesce(func.max(HelpMenuLink.position), -1)))
    mx = r.scalar_one()
    pos = int(mx) + 1
    row = HelpMenuLink(title=t, url=final_url, body_text=final_body, position=pos)
    session.add(row)
    await session.flush()
    return row


async def update_title(session: AsyncSession, link_id: int, title: str) -> HelpMenuLink | None:
    row = await session.get(HelpMenuLink, link_id)
    if row is None:
        return None
    row.title = title.strip()[:255]
    await session.flush()
    return row


async def update_url_or_body(session: AsyncSession, link_id: int, raw: str) -> HelpMenuLink | None:
    """Те же правила, что при добавлении: одна строка URL или многострочный текст."""
    row = await session.get(HelpMenuLink, link_id)
    if row is None:
        return None
    try:
        url_part, body_part = parse_help_payload(raw)
    except ValueError:
        raise ValueError("help_link_empty") from None
    if url_part:
        row.url = url_part.strip()
        row.body_text = None
    elif body_part:
        row.url = ""
        row.body_text = body_part
    await session.flush()
    return row


async def delete_link(session: AsyncSession, link_id: int) -> bool:
    row = await session.get(HelpMenuLink, link_id)
    if row is None:
        return False
    await session.delete(row)
    return True


async def move_up(session: AsyncSession, link_id: int) -> bool:
    rows = await list_ordered(session)
    ids = [r.id for r in rows]
    if link_id not in ids:
        return False
    i = ids.index(link_id)
    if i <= 0:
        return False
    a, b = rows[i - 1], rows[i]
    pa, pb = a.position, b.position
    a.position, b.position = pb, pa
    await session.flush()
    return True


async def move_down(session: AsyncSession, link_id: int) -> bool:
    rows = await list_ordered(session)
    ids = [r.id for r in rows]
    if link_id not in ids:
        return False
    i = ids.index(link_id)
    if i >= len(ids) - 1:
        return False
    a, b = rows[i], rows[i + 1]
    pa, pb = a.position, b.position
    a.position, b.position = pb, pa
    await session.flush()
    return True


def _seed_yaml_path() -> Path:
    import os

    base = Path(os.environ.get("TEXTS_DIR", "")).expanduser()
    if base and base.is_dir():
        return base / "ru" / "help_links_seed.yml"
    return Path(__file__).resolve().parent.parent.parent / "texts" / "ru" / "help_links_seed.yml"


async def seed_help_links_if_empty(session: AsyncSession) -> None:
    r = await session.execute(select(func.count()).select_from(HelpMenuLink))
    n = r.scalar_one()
    if n and int(n) > 0:
        return

    path = _seed_yaml_path()
    if not path.is_file():
        log.info("Нет файла посева помощи: %s", path)
        return

    with open(path, encoding="utf-8") as fp:
        data = yaml.safe_load(fp) or {}

    links = data.get("links") if isinstance(data, dict) else None
    if not links:
        return

    pos = 0
    for item in links:
        if not isinstance(item, dict):
            continue
        title = item.get("title")
        url = item.get("url")
        body_text = item.get("body_text")
        if not title:
            continue
        if url:
            session.add(
                HelpMenuLink(
                    title=str(title)[:255],
                    url=str(url).strip(),
                    body_text=None,
                    position=pos,
                )
            )
            pos += 1
        elif body_text is not None and str(body_text).strip():
            session.add(
                HelpMenuLink(
                    title=str(title)[:255],
                    url="",
                    body_text=str(body_text).strip(),
                    position=pos,
                )
            )
            pos += 1

    await session.flush()
    log.info("Загружен посев раздела «Помощь»: %s пунктов", pos)
