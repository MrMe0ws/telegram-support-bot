from __future__ import annotations

import asyncio
import contextlib
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from html import escape

from aiogram import Bot
from aiogram.enums import ChatAction, ParseMode
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy import func, select, update

from app.config import Settings
from app.db.models import AiDraft, AiDraftStatus, MessageSource, StoredMessage, Ticket, TicketStatus
from app.i18n import tr
from app.services import shop_webhook as shop_webhook_svc
from app.services import tickets as ticket_svc
from app.services.ai.knowledge import KnowledgeLoader
from app.services.ai.formatting import to_plain_text, to_telegram_html
from app.services.ai.prompt import AI_NOTE, build_turns, client_card, system_prompt
from app.services.ai.provider import AiError, AiProvider, AiRateLimited, AiReply
from app.services.remnawave_client import RemnawaveClient

log = logging.getLogger(__name__)

# Telegram держит «печатает…» около 5 секунд — обновляем чуть чаще.
_TYPING_EVERY_SEC = 4.5

CB_CALL_HUMAN = "aih:"
CB_DRAFT_SEND = "aid:s:"
CB_DRAFT_REJECT = "aid:x:"


@dataclass(frozen=True)
class _TicketSnapshot:
    id: int
    user_id: int
    source: str
    forum_chat_id: int
    thread_id: int | None
    external_uid: str | None
    linked_telegram_id: int | None

    @property
    def is_telegram(self) -> bool:
        return self.source == MessageSource.telegram.value

    @property
    def is_cabinet(self) -> bool:
        return self.source == MessageSource.cabinet.value


def _snapshot(t: Ticket) -> _TicketSnapshot:
    return _TicketSnapshot(
        id=t.id,
        user_id=t.user_id,
        source=t.source,
        forum_chat_id=t.forum_chat_id,
        thread_id=t.thread_id,
        external_uid=t.external_uid,
        linked_telegram_id=t.linked_telegram_id,
    )


def kb_call_human(ticket_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=tr("ai", "btn_call_human"), callback_data=f"{CB_CALL_HUMAN}{ticket_id}")]
        ]
    )


def kb_draft(draft_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text=tr("ai", "btn_draft_send"), callback_data=f"{CB_DRAFT_SEND}{draft_id}"),
                InlineKeyboardButton(text=tr("ai", "btn_draft_reject"), callback_data=f"{CB_DRAFT_REJECT}{draft_id}"),
            ]
        ]
    )


class AiService:
    """
    Очередь ответов ИИ по тикетам.

    На каждое сообщение клиента сдвигается срок ответа по тикету: клиент часто
    пишет несколькими сообщениями, и отвечать надо на все разом. Пока идёт
    ожидание и думает модель, клиент в Telegram видит «печатает…».

    Задачу ответа никогда не отменяем снаружи: отмена посреди запроса к SQLite
    оставляла брошенное соединение с блокировкой, и следующая запись ждала её
    до 5 секунд. Вместо этого задача сама проверяет срок и флаг остановки в
    безопасных точках, а пришедшее во время генерации сообщение просто
    отправляет её на второй круг с полной историей.
    """

    def __init__(
        self,
        bot: Bot,
        session_factory,
        settings: Settings,
        provider: AiProvider,
        knowledge: KnowledgeLoader,
    ) -> None:
        self._bot = bot
        self._sf = session_factory
        self._settings = settings
        self._ai = settings.ai
        self._provider = provider
        self._knowledge = knowledge
        self._tasks: dict[int, asyncio.Task] = {}
        self._typing_tasks: dict[int, asyncio.Task] = {}
        # Срок ответа по тикету (loop.time()); сдвигается каждым сообщением клиента.
        self._deadline: dict[int, float] = {}
        # Тикеты, где ответ отменён: человек ответил, тикет закрыт или передан людям.
        self._stopped: set[int] = set()

    @property
    def mode(self) -> str:
        return self._ai.mode

    # ── Очередь ─────────────────────────────────────────────────────────

    def schedule(self, ticket_id: int) -> None:
        """Сообщение клиента: ответить, когда он замолчит на AI_REPLY_DELAY_SEC."""
        loop = asyncio.get_running_loop()
        self._deadline[ticket_id] = loop.time() + self._ai.reply_delay_sec
        self._stopped.discard(ticket_id)
        task = self._tasks.get(ticket_id)
        if task is None or task.done():
            self._tasks[ticket_id] = asyncio.create_task(self._run(ticket_id))

    def cancel(self, ticket_id: int) -> None:
        """Не отвечать по тикету. Задача увидит флаг в ближайшей безопасной точке."""
        self._stopped.add(ticket_id)
        self._deadline.pop(ticket_id, None)
        self._stop_typing(ticket_id)

    def _stop_typing(self, ticket_id: int) -> None:
        task = self._typing_tasks.pop(ticket_id, None)
        if task is not None:
            task.cancel()

    async def _typing(self, chat_id: int) -> None:
        while True:
            with contextlib.suppress(Exception):
                await self._bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)
            await asyncio.sleep(_TYPING_EVERY_SEC)

    async def _wait_quiet(self, ticket_id: int) -> float | None:
        """Ждёт, пока клиент замолчит. Возвращает срок, на который отвечаем, или None — отбой."""
        loop = asyncio.get_running_loop()
        while True:
            if ticket_id in self._stopped:
                return None
            deadline = self._deadline.get(ticket_id)
            if deadline is None:
                return None
            left = deadline - loop.time()
            if left <= 0:
                return deadline
            await asyncio.sleep(left)

    async def _run(self, ticket_id: int) -> None:
        # Последний срок, на который этот круг уже отреагировал. Если к концу круга
        # срок другой — клиент написал ещё, и нужен следующий круг.
        seen = self._deadline.get(ticket_id)
        try:
            snap = await self._load_open(ticket_id)
            if snap is None:
                return
            # В режиме черновиков клиенту ничего не пишется сразу — и «печатает…»
            # висело бы зря, пока админ не дойдёт до темы.
            if self.mode == "auto" and snap.is_telegram:
                self._typing_tasks[ticket_id] = asyncio.create_task(self._typing(snap.user_id))

            while True:
                answered_deadline = await self._wait_quiet(ticket_id)
                if answered_deadline is None:
                    return
                seen = answered_deadline
                prepared = await self._prepare(ticket_id)
                if prepared is None:
                    return
                snap, turns, ai_replies = prepared
                if ai_replies >= self._ai.max_replies_per_ticket:
                    await self.escalate(ticket_id, initiator="limit")
                    return

                kb = self._knowledge.load()
                reply = await self._provider.reply(system_prompt(self._ai.assistant_name, kb.text), turns)
                if ticket_id in self._stopped:
                    return
                # Пока модель думала, клиент дописал — этот ответ уже неполный.
                if self._deadline.get(ticket_id) != answered_deadline:
                    continue
                self._deadline.pop(ticket_id, None)
                self._stop_typing(ticket_id)
                await self._act(ticket_id, reply)
                return
        except AiRateLimited as e:
            log.warning("ИИ: лимит запросов, тикет %s: %s", ticket_id, e)
            await self._topic_notice(ticket_id, tr("ai", "topic_rate_limited_html"))
        except AiError as e:
            log.warning("ИИ: нет ответа, тикет %s: %s", ticket_id, e)
            await self._topic_notice(ticket_id, tr("ai", "topic_error_html", error=escape(str(e))))
        except Exception:
            log.exception("ИИ: сбой при ответе в тикете %s", ticket_id)
        finally:
            self._stop_typing(ticket_id)
            if self._tasks.get(ticket_id) is asyncio.current_task():
                del self._tasks[ticket_id]
            # Клиент написал, пока шёл этот круг (ответ уходил или запрос упал), —
            # нужен следующий: schedule() не запускает вторую задачу, пока жива эта.
            pending = self._deadline.get(ticket_id)
            if pending is not None and pending != seen and ticket_id not in self._stopped:
                self._tasks[ticket_id] = asyncio.create_task(self._run(ticket_id))

    async def _load_open(self, ticket_id: int) -> _TicketSnapshot | None:
        async with self._sf() as session:
            t = await ticket_svc.get_ticket_by_id(session, ticket_id)
            if t is None or t.status != TicketStatus.open.value or t.ai_paused:
                return None
            return _snapshot(t)

    async def _prepare(self, ticket_id: int):
        async with self._sf() as session:
            t = await ticket_svc.get_ticket_by_id(session, ticket_id)
            if t is None or t.status != TicketStatus.open.value or t.ai_paused:
                return None
            snap = _snapshot(t)
            rows = (
                await session.execute(
                    select(StoredMessage)
                    .where(StoredMessage.ticket_id == ticket_id)
                    .order_by(StoredMessage.id.desc())
                    .limit(self._ai.history_limit)
                )
            ).scalars().all()
            messages = list(reversed(rows))
            # Последним ответил человек — значит, отвечать уже не нам.
            if not messages or messages[-1].direction != "in":
                return None
            ai_replies = (
                await session.execute(
                    select(func.count(StoredMessage.id)).where(
                        StoredMessage.ticket_id == ticket_id,
                        StoredMessage.direction == "out",
                        StoredMessage.raw_note == AI_NOTE,
                    )
                )
            ).scalar_one()

        card = client_card(
            await self._remnawave_user(snap),
            tz_name=self._settings.remnawave.timezone,
            now=datetime.now(timezone.utc),
        )
        return snap, build_turns(card, messages), ai_replies

    async def _remnawave_user(self, snap: _TicketSnapshot):
        rw = self._settings.remnawave
        tg_id = snap.user_id if snap.is_telegram else snap.linked_telegram_id
        if not rw.is_configured or not tg_id:
            return None
        try:
            return await RemnawaveClient(rw).fetch_user_by_telegram_id(tg_id)
        except Exception as e:
            log.warning("ИИ: подписка клиента не получена (тикет %s): %s", snap.id, e)
            return None

    async def _act(self, ticket_id: int, reply: AiReply) -> None:
        if reply.escalate:
            await self.escalate(
                ticket_id,
                initiator="ai",
                reason=reply.escalate_reason,
                summary=reply.escalate_summary,
            )
        elif self.mode == "auto":
            await self.send_to_client(ticket_id, reply.text or "", post_topic_copy=True)
        else:
            await self._post_draft(ticket_id, reply.text or "")

    # ── Доставка ────────────────────────────────────────────────────────

    async def _topic_send(self, snap: _TicketSnapshot, html: str, reply_markup=None) -> Message | None:
        if snap.thread_id is None:
            return None
        try:
            return await self._bot.send_message(
                chat_id=snap.forum_chat_id,
                message_thread_id=snap.thread_id,
                text=html,
                reply_markup=reply_markup,
            )
        except Exception as e:
            log.warning("ИИ: сообщение в тему тикета %s не отправлено: %s", snap.id, e)
            return None

    async def _topic_notice(self, ticket_id: int, html: str) -> None:
        async with self._sf() as session:
            t = await ticket_svc.get_ticket_by_id(session, ticket_id)
            if t is None:
                return
            snap = _snapshot(t)
        await self._topic_send(snap, html)

    async def _to_client(self, snap: _TicketSnapshot, text: str, *, message_id: int | None, with_button: bool) -> bool:
        """
        Ответ клиенту в формате его канала. text — разметка модели (см. formatting):
        в Telegram она становится HTML, в чат кабинета уходит простым текстом —
        кабинет показывает сообщение как есть.
        """
        if snap.is_cabinet:
            ticket = Ticket(id=snap.id, user_id=snap.user_id, external_uid=snap.external_uid)
            await shop_webhook_svc.deliver_staff_reply(
                self._settings,
                ticket,
                text=to_plain_text(text),
                author_label=self._ai.assistant_name,
                support_bot_message_id=message_id or 0,
            )
            return True
        markup = kb_call_human(snap.id) if with_button else None
        try:
            await self._bot.send_message(
                chat_id=snap.user_id,
                text=to_telegram_html(text),
                parse_mode=ParseMode.HTML,
                reply_markup=markup,
            )
            return True
        except TelegramBadRequest as e:
            # Разметку Telegram не принял — тот же ответ простым текстом лучше, чем никакого.
            log.warning("ИИ: HTML ответа отклонён (%s), отправляем простым текстом", e)
            try:
                await self._bot.send_message(
                    chat_id=snap.user_id,
                    text=to_plain_text(text),
                    parse_mode=None,
                    reply_markup=markup,
                )
                return True
            except Exception as e2:
                log.warning("ИИ: ответ клиенту %s не доставлен: %s", snap.user_id, e2)
                return False
        except Exception as e:
            log.warning("ИИ: ответ клиенту %s не доставлен: %s", snap.user_id, e)
            return False

    async def send_to_client(
        self,
        ticket_id: int,
        text: str,
        *,
        post_topic_copy: bool,
        topic_message_id: int | None = None,
    ) -> bool:
        async with self._sf() as session:
            t = await ticket_svc.get_ticket_by_id(session, ticket_id)
            if t is None or t.status != TicketStatus.open.value:
                return False
            snap = _snapshot(t)

        if post_topic_copy:
            copy = await self._topic_send(
                snap,
                tr("ai", "topic_ai_reply_html", name=escape(self._ai.assistant_name), text=to_telegram_html(text)),
            )
            topic_message_id = copy.message_id if copy else None

        delivered = await self._to_client(snap, text, message_id=topic_message_id, with_button=True)
        if not delivered:
            await self._topic_send(snap, tr("ai", "topic_delivery_failed_html"))
            return False

        async with self._sf() as session:
            async with session.begin():
                await ticket_svc.record_message(
                    session,
                    ticket_id=ticket_id,
                    direction="out",
                    content_type="text",
                    text=text,
                    telegram_file_id=None,
                    telegram_message_id=topic_message_id,
                    raw_note=AI_NOTE,
                )
        return True

    # ── Черновики (AI_MODE=draft) ───────────────────────────────────────

    async def _stale_pending_drafts(self, ticket_id: int) -> None:
        async with self._sf() as session:
            async with session.begin():
                rows = (
                    await session.execute(
                        select(AiDraft).where(
                            AiDraft.ticket_id == ticket_id,
                            AiDraft.status == AiDraftStatus.pending.value,
                        )
                    )
                ).scalars().all()
                stale = [(d.topic_message_id, d.text) for d in rows]
                if rows:
                    await session.execute(
                        update(AiDraft)
                        .where(AiDraft.id.in_([d.id for d in rows]))
                        .values(status=AiDraftStatus.stale.value)
                    )
                t = await ticket_svc.get_ticket_by_id(session, ticket_id)
                chat_id = t.forum_chat_id if t else None
        for msg_id, text in stale:
            if msg_id and chat_id:
                with contextlib.suppress(Exception):
                    await self._bot.edit_message_text(
                        chat_id=chat_id,
                        message_id=msg_id,
                        text=tr("ai", "draft_stale_html", text=to_telegram_html(text)),
                    )

    async def _post_draft(self, ticket_id: int, text: str) -> None:
        # Старый черновик отвечал на прошлую версию разговора — отправлять его поздно.
        await self._stale_pending_drafts(ticket_id)
        async with self._sf() as session:
            async with session.begin():
                t = await ticket_svc.get_ticket_by_id(session, ticket_id)
                if t is None or t.status != TicketStatus.open.value:
                    return
                snap = _snapshot(t)
                draft = AiDraft(ticket_id=ticket_id, text=text, status=AiDraftStatus.pending.value)
                session.add(draft)
                await session.flush()
                draft_id = draft.id
        msg = await self._topic_send(
            snap,
            tr("ai", "topic_draft_html", text=to_telegram_html(text)),
            reply_markup=kb_draft(draft_id),
        )
        if msg is not None:
            async with self._sf() as session:
                async with session.begin():
                    await session.execute(
                        update(AiDraft).where(AiDraft.id == draft_id).values(topic_message_id=msg.message_id)
                    )

    async def resolve_draft(self, draft_id: int, *, send: bool, admin_label: str) -> str:
        """Кнопка под черновиком. Возвращает текст всплывающего ответа админу."""
        async with self._sf() as session:
            async with session.begin():
                draft = await session.get(AiDraft, draft_id)
                if draft is None or draft.status != AiDraftStatus.pending.value:
                    return tr("ai", "draft_done")
                t = await ticket_svc.get_ticket_by_id(session, draft.ticket_id)
                if t is None or t.status != TicketStatus.open.value:
                    draft.status = AiDraftStatus.stale.value
                    return tr("ai", "draft_ticket_closed")
                # Статус меняем сразу: второе нажатие (или второй админ) не отправит дубль.
                draft.status = AiDraftStatus.sent.value if send else AiDraftStatus.rejected.value
                ticket_id, text, msg_id, chat_id = draft.ticket_id, draft.text, draft.topic_message_id, t.forum_chat_id

        if send:
            ok = await self.send_to_client(ticket_id, text, post_topic_copy=False, topic_message_id=msg_id)
            if not ok:
                async with self._sf() as session:
                    async with session.begin():
                        await session.execute(
                            update(AiDraft).where(AiDraft.id == draft_id).values(status=AiDraftStatus.pending.value)
                        )
                return tr("ai", "draft_send_failed")
            new_text = tr("ai", "draft_sent_html", admin=escape(admin_label), text=to_telegram_html(text))
        else:
            new_text = tr("ai", "draft_rejected_html", admin=escape(admin_label), text=to_telegram_html(text))
        if msg_id:
            with contextlib.suppress(Exception):
                await self._bot.edit_message_text(chat_id=chat_id, message_id=msg_id, text=new_text)
        return tr("ai", "draft_sent_toast" if send else "draft_rejected_toast")

    # ── Человек берёт тикет ─────────────────────────────────────────────

    async def _set_paused(self, ticket_id: int, paused: bool) -> bool:
        """Возвращает True, если состояние изменилось."""
        async with self._sf() as session:
            async with session.begin():
                t = await ticket_svc.get_ticket_by_id(session, ticket_id)
                if t is None or bool(t.ai_paused) == paused:
                    return False
                t.ai_paused = paused
                return True

    async def pause_by_staff(self, ticket_id: int) -> None:
        """
        Сотрудник ответил в теме сам.

        auto — ИИ замолкает в тикете, чтобы не говорить поверх человека.
        draft — каждый ответ и так проходит через человека, поэтому ИИ остаётся
        подсказчиком: снимаем только черновик, на который сотрудник ответил по-своему.
        """
        self.cancel(ticket_id)
        if self.mode == "auto":
            await self._set_paused(ticket_id, True)
        await self._stale_pending_drafts(ticket_id)

    async def set_enabled_in_ticket(self, ticket_id: int, enabled: bool) -> None:
        """/ai on | /ai off в теме тикета."""
        await self._set_paused(ticket_id, not enabled)
        if enabled:
            # Если клиент ждёт ответа — ИИ ответит; если последним писал человек, _prepare промолчит.
            self.schedule(ticket_id)
        else:
            self.cancel(ticket_id)
            await self._stale_pending_drafts(ticket_id)

    async def escalate(
        self,
        ticket_id: int,
        *,
        initiator: str,
        reason: str | None = None,
        summary: str | None = None,
    ) -> bool:
        """
        Передать тикет людям. initiator: ai — решила модель, client — кнопка
        клиента, limit — ИИ исчерпал лимит ответов. False — тикет уже у людей.
        """
        self.cancel(ticket_id)
        if not await self._set_paused(ticket_id, True):
            return False
        async with self._sf() as session:
            t = await ticket_svc.get_ticket_by_id(session, ticket_id)
            if t is None:
                return False
            snap = _snapshot(t)

        if initiator == "client":
            notice = tr("ai", "topic_escalated_client_html")
        elif initiator == "limit":
            notice = tr("ai", "topic_escalated_limit_html", n=self._ai.max_replies_per_ticket)
        else:
            notice = tr(
                "ai",
                "topic_escalated_ai_html",
                reason=escape(reason or "—"),
                summary=escape(summary or "—"),
            )
        await self._topic_send(snap, notice)

        # В режиме черновиков клиент с ИИ не общался — сообщать ему не о чем.
        if initiator == "client" or self.mode == "auto":
            await self._to_client(snap, tr("ai", "client_escalated"), message_id=None, with_button=False)
        return True


_instance: AiService | None = None


def set_ai_service(service: AiService | None) -> None:
    global _instance
    _instance = service


def get_ai_service() -> AiService | None:
    """None — ИИ выключен (AI_ENABLED) или не настроен."""
    return _instance
