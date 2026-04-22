from __future__ import annotations

import logging

from aiogram import Bot, F, Router
from aiogram.enums import ChatType
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import MessageReactionUpdated

from app.services import reaction_bridge as reaction_bridge_svc

log = logging.getLogger(__name__)

router = Router(name="message_reactions")


@router.message_reaction(
    F.chat.type.in_({ChatType.SUPERGROUP, ChatType.GROUP}),
)
async def mirror_group_admin_reaction_to_user_dm(
    reaction: MessageReactionUpdated,
    bot: Bot,
    session_factory,
    settings,
):
    if reaction.chat.id != settings.support_group_id:
        return
    actor = reaction.user
    if actor is None or actor.id not in settings.admin_ids:
        return

    gmid = reaction.message_id

    async with session_factory() as session:
        resolved = await reaction_bridge_svc.resolve_dm_message_for_group_forward(
            session,
            forum_chat_id=reaction.chat.id,
            group_message_id=gmid,
        )

    if resolved is None:
        return

    user_id, dm_mid = resolved
    try:
        await bot.set_message_reaction(
            chat_id=user_id,
            message_id=dm_mid,
            reaction=reaction.new_reaction,
        )
    except TelegramBadRequest as e:
        log.debug(
            "set_message_reaction ЛС user=%s dm_msg=%s: %s",
            user_id,
            dm_mid,
            e,
        )
    except Exception as e:
        log.warning(
            "set_message_reaction ЛС failed user=%s dm_msg=%s: %s",
            user_id,
            dm_mid,
            e,
        )


@router.message_reaction(F.chat.type == ChatType.PRIVATE)
async def mirror_dm_reaction_to_staff_message(
    reaction: MessageReactionUpdated,
    bot: Bot,
    session_factory,
    settings,
):
    user = reaction.user
    if user is None:
        return

    dm_msg_id = reaction.message_id

    async with session_factory() as session:
        resolved = await reaction_bridge_svc.resolve_staff_message(
            session,
            user_id=user.id,
            dm_message_id=dm_msg_id,
        )

    if resolved is None:
        return

    forum_chat_id, staff_message_id = resolved
    if forum_chat_id != settings.support_group_id:
        return

    try:
        await bot.set_message_reaction(
            chat_id=forum_chat_id,
            message_id=staff_message_id,
            reaction=reaction.new_reaction,
        )
    except TelegramBadRequest as e:
        log.debug(
            "set_message_reaction forum=%s staff_msg=%s: %s",
            forum_chat_id,
            staff_message_id,
            e,
        )
    except Exception as e:
        log.warning(
            "set_message_reaction failed forum=%s staff_msg=%s: %s",
            forum_chat_id,
            staff_message_id,
            e,
        )
