from __future__ import annotations

from datetime import datetime

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import BlockedUser, MessageSource, StoredMessage, Ticket, TicketStatus


async def is_blocked(session: AsyncSession, user_id: int) -> bool:
    row = await session.get(BlockedUser, user_id)
    return row is not None


async def block_user(session: AsyncSession, user_id: int, reason: str | None) -> None:
    session.add(BlockedUser(user_id=user_id, reason=reason, created_at=datetime.utcnow()))


async def unblock_user(session: AsyncSession, user_id: int) -> None:
    row = await session.get(BlockedUser, user_id)
    if row:
        await session.delete(row)


async def get_open_ticket(
    session: AsyncSession,
    user_id: int,
    *,
    source: str = MessageSource.telegram.value,
) -> Ticket | None:
    q = await session.execute(
        select(Ticket).where(
            Ticket.user_id == user_id,
            Ticket.source == source,
            Ticket.status == TicketStatus.open.value,
        )
    )
    return q.scalar_one_or_none()


async def create_ticket(
    session: AsyncSession,
    *,
    user_id: int,
    forum_chat_id: int,
    thread_id: int,
    source: str = MessageSource.telegram.value,
    external_uid: str | None = None,
) -> Ticket:
    t = Ticket(
        user_id=user_id,
        forum_chat_id=forum_chat_id,
        thread_id=thread_id,
        source=source,
        status=TicketStatus.open.value,
        external_uid=external_uid,
    )
    session.add(t)
    await session.flush()
    return t


async def get_ticket_by_id(session: AsyncSession, ticket_id: int) -> Ticket | None:
    return await session.get(Ticket, ticket_id)


async def update_last_forward_group_msg(
    session: AsyncSession,
    ticket_id: int,
    group_message_id: int,
) -> None:
    await session.execute(
        update(Ticket)
        .where(Ticket.id == ticket_id)
        .values(last_forward_group_msg_id=group_message_id)
    )


async def update_ticket_thread(
    session: AsyncSession,
    ticket_id: int,
    *,
    thread_id: int,
    clear_last_forward: bool = True,
) -> None:
    vals: dict = {"thread_id": thread_id}
    if clear_last_forward:
        vals["last_forward_group_msg_id"] = None
    await session.execute(update(Ticket).where(Ticket.id == ticket_id).values(**vals))


async def close_ticket(session: AsyncSession, ticket_id: int) -> None:
    await session.execute(
        update(Ticket)
        .where(Ticket.id == ticket_id)
        .values(status=TicketStatus.closed.value)
    )


async def get_ticket_by_thread(
    session: AsyncSession,
    forum_chat_id: int,
    thread_id: int,
) -> Ticket | None:
    q = await session.execute(
        select(Ticket).where(
            Ticket.forum_chat_id == forum_chat_id,
            Ticket.thread_id == thread_id,
            Ticket.status == TicketStatus.open.value,
        )
    )
    return q.scalar_one_or_none()


async def record_message(
    session: AsyncSession,
    *,
    ticket_id: int,
    direction: str,
    content_type: str,
    text: str | None,
    telegram_file_id: str | None,
    telegram_message_id: int | None,
    raw_note: str | None = None,
) -> None:
    session.add(
        StoredMessage(
            ticket_id=ticket_id,
            direction=direction,
            content_type=content_type,
            text=text,
            telegram_file_id=telegram_file_id,
            telegram_message_id=telegram_message_id,
            raw_note=raw_note,
        )
    )
