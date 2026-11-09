from __future__ import annotations

from datetime import datetime

from sqlalchemy import delete, select, update
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


async def update_ticket_external_uid(
    session: AsyncSession,
    ticket_id: int,
    external_uid: str,
) -> None:
    await session.execute(
        update(Ticket)
        .where(Ticket.id == ticket_id)
        .values(external_uid=external_uid)
    )


async def update_ticket_cabinet_links(
    session: AsyncSession,
    ticket_id: int,
    *,
    telegram_id: int | None = None,
    customer_id: int | None = None,
    remnawave_user_id: int | None = None,
) -> None:
    """
    Привязки тикета кабинета из очередного сообщения. Пустые значения не затирают
    сохранённые: магазин мог не успеть найти профиль в панели именно в этот раз.
    """
    values = {
        column: value
        for column, value in (
            ("linked_telegram_id", telegram_id),
            ("linked_customer_id", customer_id),
            ("remnawave_user_id", remnawave_user_id),
        )
        if value is not None and value > 0
    }
    if not values:
        return
    await session.execute(update(Ticket).where(Ticket.id == ticket_id).values(**values))


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
    *,
    open_only: bool = True,
) -> Ticket | None:
    clauses = [
        Ticket.forum_chat_id == forum_chat_id,
        Ticket.thread_id == thread_id,
    ]
    if open_only:
        clauses.append(Ticket.status == TicketStatus.open.value)
    q = await session.execute(select(Ticket).where(*clauses))
    return q.scalar_one_or_none()


async def get_message_by_client_id(
    session: AsyncSession,
    client_message_id: str,
) -> StoredMessage | None:
    """Найти сообщение по idempotency key (для dedup cabinet-сообщений)."""
    q = await session.execute(
        select(StoredMessage).where(StoredMessage.client_message_id == client_message_id)
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
    client_message_id: str | None = None,
    attachment_meta: str | None = None,
) -> StoredMessage:
    msg = StoredMessage(
        ticket_id=ticket_id,
        direction=direction,
        content_type=content_type,
        text=text,
        telegram_file_id=telegram_file_id,
        telegram_message_id=telegram_message_id,
        raw_note=raw_note,
        client_message_id=client_message_id,
        attachment_meta=attachment_meta,
    )
    session.add(msg)
    await session.flush()
    return msg


async def update_message_media(
    session: AsyncSession,
    message_id: int,
    *,
    content_type: str,
    telegram_file_id: str,
    telegram_message_id: int,
    attachment_meta: str,
) -> None:
    """Вложение кабинета ушло в тему: теперь известны file_id и сообщение в Telegram."""
    await session.execute(
        update(StoredMessage)
        .where(StoredMessage.id == message_id)
        .values(
            content_type=content_type,
            telegram_file_id=telegram_file_id,
            telegram_message_id=telegram_message_id,
            attachment_meta=attachment_meta,
        )
    )


async def delete_messages(session: AsyncSession, message_ids: list[int]) -> None:
    if message_ids:
        await session.execute(delete(StoredMessage).where(StoredMessage.id.in_(message_ids)))


async def list_messages_by_note(
    session: AsyncSession,
    ticket_id: int,
    raw_note: str,
) -> list[StoredMessage]:
    """Строки одного сообщения кабинета с вложениями — их связывает общий raw_note."""
    q = await session.execute(
        select(StoredMessage)
        .where(StoredMessage.ticket_id == ticket_id, StoredMessage.raw_note == raw_note)
        .order_by(StoredMessage.id)
    )
    return list(q.scalars().all())


async def get_message_with_ticket(
    session: AsyncSession,
    message_id: int,
) -> tuple[StoredMessage, Ticket] | None:
    q = await session.execute(
        select(StoredMessage, Ticket)
        .join(Ticket, Ticket.id == StoredMessage.ticket_id)
        .where(StoredMessage.id == message_id)
    )
    row = q.first()
    return (row[0], row[1]) if row else None
