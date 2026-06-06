# Релиз v1.3.0

## Вариант для GitHub Releases

## 🚀 Новое

- **Bridge к web-кабинету (remnawave-telegram-shop)** — при `SUPPORT_BOT_API=true` в shop support-bot принимает сообщения из кабинета и создаёт/продолжает forum-топики с **`MessageSource.cabinet`**
- **HTTP sidecar (aiohttp):** `POST /internal/cabinet/message` — Bearer `SUPPORT_BRIDGE_SECRET`; порт **`HTTP_PORT`** (по умолчанию `8080`)
- **Webhook в shop:** `SHOP_WEBHOOK_URL` — доставка ответов саппорта и события **`closed`** после `/close` в cabinet-топике
- **Команды для cabinet-тикетов:** `/profile` (Remnawave-профиль при привязанном TG) и `/close` с уведомлением shop

## ✨ Улучшения

- **Идемпотентность** — dedup по `client_message_id` на стороне support-bot; повтор POST из shop не дублирует сообщение в топике
- **Контекст тикета** — `linked_telegram_id`, `external_uid` вида `shop:{ticket_id}`; первое сообщение в топике с меткой кабинета, email и подпиской
- **Ответы саппорта** — для `source=cabinet` текст/caption уходит в shop webhook; автор в кабинете всегда «Поддержка»

## 🧱 Технические изменения

- **Новые модули:** `app/http/server.py`, `app/services/cabinet_bridge.py`, `app/services/shop_webhook.py`
- **Расширения:** `app/handlers/group_topics.py`, `app/services/tickets.py`, `app/db/models.py` (`MessageSource.cabinet`, `client_message_id`), auto-migration в `app/db/session.py`
- **Env:** `SHOP_WEBHOOK_URL`, `SUPPORT_BRIDGE_SECRET`, `HTTP_PORT` — см. `.env.example` и README (раздел «Bridge к web-кабинету»)
- **Авторизация bridge:** `hmac.compare_digest` для Bearer-токена на `/internal/cabinet/message`

---

## Вариант для Telegram

1.3.0 https://github.com/MrMe0ws/telegram-support-bot/releases/tag/1.3.0

🚀 Новое
• Bridge к web-кабинету shop: cabinet-сообщения → forum-топики MessageSource.cabinet
• HTTP sidecar POST /internal/cabinet/message с Bearer SUPPORT_BRIDGE_SECRET
• Webhook SHOP_WEBHOOK_URL: ответы саппорта и closed после /close
• /profile и /close для cabinet-тикетов с Remnawave при привязанном TG

✨ Улучшения
• Dedup по client_message_id — без дублей при retry shop
• external_uid shop:{id}, linked_telegram_id, контекст email/подписки в первом сообщении
• Ответы в cabinet-топике уходят в shop; автор в кабинете — Поддержка

🧱 Технические изменения
• cabinet_bridge, shop_webhook, http/server; правки group_topics и tickets
• Env SHOP_WEBHOOK_URL, SUPPORT_BRIDGE_SECRET, HTTP_PORT в README и .env.example
• Bearer через hmac.compare_digest на internal endpoint
