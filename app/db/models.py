from __future__ import annotations

import enum
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class MessageSource(enum.StrEnum):
    telegram = "telegram"
    vk = "vk"
    max = "max"


class TicketStatus(enum.StrEnum):
    open = "open"
    closed = "closed"


class Ticket(Base):
    __tablename__ = "tickets"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(BigInteger, index=True)
    external_uid: Mapped[str | None] = mapped_column(String(255), nullable=True)
    source: Mapped[str] = mapped_column(String(32), default=MessageSource.telegram.value)
    status: Mapped[str] = mapped_column(String(16), default=TicketStatus.open.value)
    forum_chat_id: Mapped[int] = mapped_column(BigInteger)
    thread_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    last_forward_group_msg_id: Mapped[int | None] = mapped_column(
        BigInteger, nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    messages: Mapped[list["StoredMessage"]] = relationship(back_populates="ticket")


class GroupDmUserMessageMap(Base):
    """Пересланное в топик сообщение клиента → исходное сообщение в ЛС (для реакций админа → клиент)."""

    __tablename__ = "group_dm_user_message_map"
    __table_args__ = (
        UniqueConstraint(
            "forum_chat_id",
            "group_message_id",
            name="uq_gdum_forum_group_msg",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(BigInteger, index=True)
    dm_message_id: Mapped[int] = mapped_column(BigInteger)
    forum_chat_id: Mapped[int] = mapped_column(BigInteger)
    group_message_id: Mapped[int] = mapped_column(BigInteger)


class DmStaffMessageMap(Base):
    """Связка: сообщение бота в ЛС (копия ответа саппорта) → исходное сообщение в топике."""

    __tablename__ = "dm_staff_message_map"
    __table_args__ = (
        UniqueConstraint(
            "user_id",
            "dm_message_id",
            name="uq_dm_staff_user_dm_msg",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(BigInteger, index=True)
    dm_message_id: Mapped[int] = mapped_column(BigInteger)
    forum_chat_id: Mapped[int] = mapped_column(BigInteger)
    staff_message_id: Mapped[int] = mapped_column(BigInteger)


class StoredMessage(Base):
    __tablename__ = "messages"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ticket_id: Mapped[int] = mapped_column(ForeignKey("tickets.id"), index=True)
    direction: Mapped[str] = mapped_column(String(8))
    content_type: Mapped[str] = mapped_column(String(32))
    text: Mapped[str | None] = mapped_column(Text, nullable=True)
    telegram_file_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    telegram_message_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    raw_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    ticket: Mapped["Ticket"] = relationship(back_populates="messages")


class BlockedUser(Base):
    __tablename__ = "blocked_users"

    user_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class ExternalSource(Base):
    """Регистрация внешних каналов (VK / Max): включено и секреты — через env."""

    __tablename__ = "external_sources"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    kind: Mapped[str] = mapped_column(String(16), unique=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=False)


class HelpMenuLink(Base):
    """Пункты раздела «Помощь»: внешняя ссылка или текст по callback; порядок — position."""

    __tablename__ = "help_menu_links"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    title: Mapped[str] = mapped_column(String(255))
    url: Mapped[str] = mapped_column(Text, default="")
    body_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    position: Mapped[int] = mapped_column(Integer, index=True)
