import os
from dataclasses import dataclass

from app.i18n import tr


def _split_ids(raw: str) -> frozenset[int]:
    out: list[int] = []
    for part in raw.replace(" ", "").split(","):
        if not part:
            continue
        out.append(int(part))
    return frozenset(out)


@dataclass(frozen=True)
class Settings:
    bot_token: str
    support_group_id: int
    admin_ids: frozenset[int]
    database_url: str
    topic_name_template: str
    # ID кастомного emoji для иконки темы (открытый / закрытый тикет). Пустая строка — не выставлять.
    topic_icon_emoji_open: str
    topic_icon_emoji_closed: str

    @staticmethod
    def from_env() -> "Settings":
        token = os.environ.get("BOT_TOKEN", "").strip()
        if not token:
            raise RuntimeError(tr("errors", "no_bot_token"))

        gid = int(os.environ["SUPPORT_GROUP_ID"])
        admins_raw = os.environ.get("ADMIN_IDS", "")
        db = os.environ.get(
            "DATABASE_URL",
            "sqlite+aiosqlite:////data/support.db",
        )
        tpl = os.environ.get(
            "TOPIC_NAME_TEMPLATE",
            "{username} · #{ticket_id}",
        )
        icon_open = os.environ.get(
            "TOPIC_ICON_EMOJI_OPEN",
            "5377316857231450742",
        ).strip()
        icon_closed = os.environ.get(
            "TOPIC_ICON_EMOJI_CLOSED",
            "5237699328843200968",
        ).strip()
        return Settings(
            bot_token=token,
            support_group_id=gid,
            admin_ids=_split_ids(admins_raw),
            database_url=db,
            topic_name_template=tpl,
            topic_icon_emoji_open=icon_open,
            topic_icon_emoji_closed=icon_closed,
        )
