from aiogram.fsm.state import State, StatesGroup


class UserFlow(StatesGroup):
    """ЛС: после «Создать обращение» ждём текст/медиа для тикета."""

    waiting_ticket_description = State()


class AdminFlow(StatesGroup):
    """ЛС админа: блокировки и добавление пункта «Помощь»."""

    block_uid = State()
    block_reason = State()
    unblock_uid = State()
    help_title = State()
    help_pick_type = State()
    help_link_url = State()
    help_article_body = State()
    help_edit_title = State()
    help_edit_link = State()
    help_edit_body = State()
