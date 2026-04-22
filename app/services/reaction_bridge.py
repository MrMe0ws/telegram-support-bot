from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import DmStaffMessageMap, GroupDmUserMessageMap


async def save_dm_staff_mapping(
    session: AsyncSession,
    *,
    user_id: int,
    dm_message_id: int,
    forum_chat_id: int,
    staff_message_id: int,
) -> None:
    session.add(
        DmStaffMessageMap(
            user_id=user_id,
            dm_message_id=dm_message_id,
            forum_chat_id=forum_chat_id,
            staff_message_id=staff_message_id,
        )
    )


async def save_group_user_mapping(
    session: AsyncSession,
    *,
    user_id: int,
    dm_message_id: int,
    forum_chat_id: int,
    group_message_id: int,
) -> None:
    session.add(
        GroupDmUserMessageMap(
            user_id=user_id,
            dm_message_id=dm_message_id,
            forum_chat_id=forum_chat_id,
            group_message_id=group_message_id,
        )
    )


async def resolve_dm_message_for_group_forward(
    session: AsyncSession,
    *,
    forum_chat_id: int,
    group_message_id: int,
) -> tuple[int, int] | None:
    """Возвращает (user_id, dm_message_id) или None."""
    q = await session.execute(
        select(GroupDmUserMessageMap.user_id, GroupDmUserMessageMap.dm_message_id).where(
            GroupDmUserMessageMap.forum_chat_id == forum_chat_id,
            GroupDmUserMessageMap.group_message_id == group_message_id,
        )
    )
    row = q.one_or_none()
    if row is None:
        return None
    return row[0], row[1]


async def resolve_staff_message(
    session: AsyncSession,
    *,
    user_id: int,
    dm_message_id: int,
) -> tuple[int, int] | None:
    q = await session.execute(
        select(DmStaffMessageMap.forum_chat_id, DmStaffMessageMap.staff_message_id).where(
            DmStaffMessageMap.user_id == user_id,
            DmStaffMessageMap.dm_message_id == dm_message_id,
        )
    )
    row = q.one_or_none()
    if row is None:
        return None
    return row[0], row[1]
