# Telegram Support Bot

Бот технической поддержки для VPN и других сервисов: клиент пишет в ЛС боту, сообщения попадают в **топики форум-супергруппы**, ответы саппорта возвращаются клиенту **от имени бота**. 

Поддерживаются **текст**, **фото**, **видео**, **видеосообщения (кружки)**, **документы**, **аудиофайлы**, **голосовые сообщения**, **стикеры** и **реакции** на сообщения (зеркалирование ЛС ↔ топик). 
Так же доступны блокировки, история в **SQLite**, **админ-панель** в ЛС (инлайн-кнопки), закрытие тикета командами **`/close`** (у клиента и у админа в теме), **`/profile`** в теме для админа.
Поддержка Remnawave Panel и Telegram Meows Shop bot VPN(cabinet)

---

<img width="auto" height="auto" alt="image" src="https://github.com/user-attachments/assets/e8d1da1b-5c06-41db-bc50-2dbf7c48c08d" />
<<<<<<< HEAD
=======

>>>>>>> 6286e4bc16fc3f3ba0928b5e472d15e0f8940fd8

---

## Стек

- Python 3.12, **aiogram 3**, **SQLite** (`aiosqlite`), SQLAlchemy 2  
- Запуск в **Docker Compose**  
- Тексты интерфейса — **YAML** в каталоге `texts/<локаль>/` (по умолчанию `ru`), загрузка через `app.i18n.tr(...)`

---

## Требования по стороне Telegram

1. **Группа-супергруппа с темами** (Forum). Бот добавлен как **администратор** с правом **управлять темами** (Manage topics).  
2. Взять числовой **ID группы** (отрицательное значение вида `-100…`) и указать его в `SUPPORT_GROUP_ID`.  
3. В [@BotFather](https://t.me/BotFather): для бота **`/setprivacy` → Disable**, иначе бот не увидит обычные сообщения саппортов в группе (только команды и ответы себе).  
4. Получить **токен** бота (`BOT_TOKEN`) и Telegram **ID** администраторов для `ADMIN_IDS` (через запятую).

---

## Быстрый старт (Docker)

1. Скопируйте окружение:

   ```bash
   cp .env.example .env
   ```

2. Заполните в `.env` как минимум: `BOT_TOKEN`, `SUPPORT_GROUP_ID`, `ADMIN_IDS`.

3. Сборка и запуск:

   ```bash
   docker compose build
   docker compose up -d
   ```

4. Логи:

   ```bash
   docker compose logs -f
   ```

База SQLite создаётся в томе `./data` (файл по умолчанию `/data/support.db` внутри контейнера).

---

## Переменные окружения

| Переменная | Обязательно | Описание |
|------------|-------------|----------|
| `BOT_TOKEN` | да | Токен от @BotFather |
| `SUPPORT_GROUP_ID` | да | ID супергруппы с темами |
| `ADMIN_IDS` | да для админ-функций | Через запятую: Telegram user id админов |
| `DATABASE_URL` | нет | По умолчанию `sqlite+aiosqlite:////data/support.db` |
| `TOPIC_NAME_TEMPLATE` | нет | Шаблон имени топика: `{username}`, `{ticket_id}`, `{uid}`, `{name}` |
| `TOPIC_ICON_EMOJI_OPEN` | нет | ID кастомного emoji для иконки **открытого** тикета (по умолчанию «?»). Пустая строка — без иконки при создании темы |
| `TOPIC_ICON_EMOJI_CLOSED` | нет | ID кастомного emoji перед закрытием темы (**закрытый** тикет). Пустая строка — только `closeForumTopic` без смены иконки |
| `BOT_LOCALE` | нет | Подкаталог в `texts/` (по умолчанию `ru`) |
| `TEXTS_DIR` | нет | Корень каталога `texts` (в образе задано `/app/texts`) |
| `REMNAWAVE_ENABLED` | нет | `true` — подтягивать подписку из Remnawave в `/profile` (нужен `REMNAWAVE_TOKEN`) |
| `REMNAWAVE_URL` | нет* | Базовый URL панели без `/api`, напр. `http://remnawave:3000` или `https://panel.example.com` |
| `REMNAWAVE_MODE` | нет | `local` — прямой доступ (Docker-сеть); `remote` — внешний URL + опционально gate |
| `REMNAWAVE_TOKEN` | нет* | API token (Bearer) из панели Remnawave |
| `REMNAWAVE_HTTP_HOST` | нет* | Заголовок `Host` (= **`PANEL_DOMAIN`** в `.env` панели). Рекомендуется при `REMNAWAVE_URL=http://remnawave:3000` |
| `REMNAWAVE_PROXY_HEADERS` | нет | Для `local` по умолчанию `true`: `X-Forwarded-For` + `X-Forwarded-Proto: https` (требование панели) |
| `REMNAWAVE_FORWARDED_FOR` | нет | Значение `X-Forwarded-For` (по умолчанию `127.0.0.1`) |
| `REMNAWAVE_FORWARDED_PROTO` | нет | Значение `X-Forwarded-Proto` (по умолчанию `https`) |
| `REMNAWAVE_SUB_PUBLIC_URL` | нет | База ссылки подписки, напр. `https://sub.example.com` → `{base}/{shortUuid}` |
| `REMNAWAVE_GATE_QUERY_NAME` / `VALUE` | нет | Query-параметр для nginx gate (`remote`) |
| `REMNAWAVE_GATE_COOKIE` | нет | Cookie для nginx gate (`remote`) |
| `REMNAWAVE_TIMEOUT_SEC` | нет | Таймаут HTTP к панели (по умолчанию 10) |
| `BOT_TIMEZONE` | нет | Часовой пояс дат в `/profile` (по умолчанию `UTC`) |
| `SHOP_WEBHOOK_URL` | нет* | URL webhook shop для ответов саппорта из cabinet-тикетов (см. раздел «Bridge к web-кабинету») |
| `SUPPORT_BRIDGE_SECRET` | нет* | Общий Bearer-секрет с shop (`SUPPORT_BRIDGE_SECRET` в remnawave-telegram-shop) |
| `HTTP_PORT` | нет | Порт HTTP sidecar для `POST /internal/cabinet/message` (по умолчанию `8080`) |

\* `REMNAWAVE_*` (кроме перечисленных отдельно) обязательны, если `REMNAWAVE_ENABLED=true`.  
\* `SHOP_WEBHOOK_URL` и `SUPPORT_BRIDGE_SECRET` обязательны вместе, если shop включил `SUPPORT_BOT_API=true`.

### Remnawave в `/profile`

При включённой интеграции команда **`/profile`** в теме тикета запрашивает `GET /api/users/by-telegram-id/{telegramId}` и показывает статус подписки, трафик, HWID, тег, сквады и ссылку на sub.

**Режим `local`** (бот и панель на одной машине):

```env
REMNAWAVE_ENABLED=true
REMNAWAVE_URL=http://remnawave:3000
REMNAWAVE_MODE=local
REMNAWAVE_TOKEN=...
REMNAWAVE_HTTP_HOST=panel.example.com
REMNAWAVE_SUB_PUBLIC_URL=https://sub.example.com
```

Подключите контейнер бота к сети compose панели (`remnawave-network`, см. `docker-compose.yml`). Панель Remnawave в production **без nginx** требует заголовки reverse proxy — при `REMNAWAVE_MODE=local` бот выставляет их сам (`REMNAWAVE_PROXY_HEADERS=true` по умолчанию). **`REMNAWAVE_HTTP_HOST`** лучше задать как **`PANEL_DOMAIN`** (например `panel.domain.ru`).

Проверка из контейнера бота:

```bash
docker compose exec support-bot python -c "import asyncio; from app.config import Settings; from app.services.remnawave_client import RemnawaveClient; s=Settings.from_env().remnawave; asyncio.run(RemnawaveClient(s).probe()); print('ok')"
```

Если панель не в общей сети — `http://host.docker.internal:3000` и `extra_hosts` в compose.

**Режим `remote`** (бот на другой машине, панель за nginx с «секретной» ссылкой):

```env
REMNAWAVE_MODE=remote
REMNAWAVE_URL=https://panel.example.com
REMNAWAVE_GATE_QUERY_NAME=hKzPrYmE
REMNAWAVE_GATE_QUERY_VALUE=...
# REMNAWAVE_GATE_COOKIE=hKzPrYmE=...; Path=/; ...
```

Токены и значения gate **не коммитьте** в git.

---

## Bridge к web-кабинету (remnawave-telegram-shop)

Опциональная связка с **[remnawave-telegram-shop](https://github.com/Jolymmiels/remnawave-telegram-shop)**: пользователь пишет в SPA-кабинете VPN shop, сообщения попадают в **тот же форум** саппорта, ответы саппорта возвращаются в кабинет через webhook.

### Архитектура

```
Кабинет (SPA) → shop API → POST /internal/cabinet/message → топик (source=cabinet)
Саппорт отвечает в TG → shop webhook → polling в кабинете
Telegram DM в этом боте — без изменений (source=telegram)
```

В shop включается **`SUPPORT_BOT_API=true`** (миграция `000036_cabinet_support`). Подробная настройка shop — **`documentation/cabinet/SETUP-GUIDE-RU.md`** в репозитории shop.

### HTTP API (sidecar)

Параллельно с long polling Telegram поднимается aiohttp-сервер на **`HTTP_PORT`** (по умолчанию `8080`):

| Метод | Путь | Описание |
|-------|------|----------|
| `POST` | `/internal/cabinet/message` | Принять сообщение из shop; заголовок `Authorization: Bearer <SUPPORT_BRIDGE_SECRET>` |

Тело запроса (JSON): `account_id`, `shop_ticket_id`, `text`, `display_name`, `telegram_label`, `email`, `subscription_summary`, `is_new_ticket`, опционально **`client_message_id`** (UUID, idempotency — колонка `messages.client_message_id`, миграция при старте в `app/db/session.py`).

### Переменные окружения

| Переменная | Описание |
|------------|----------|
| `SHOP_WEBHOOK_URL` | Webhook shop, напр. `http://shop:8080/cabinet/api/internal/support/webhook` |
| `SUPPORT_BRIDGE_SECRET` | Тот же секрет, что `SUPPORT_BRIDGE_SECRET` в shop |
| `HTTP_PORT` | Порт sidecar (по умолчанию `8080`) |

**Shop `.env` (пример, та же docker-сеть):**

```env
SUPPORT_BOT_API=true
SUPPORT_BOT_API_URL=http://support-bot:8080
SUPPORT_BRIDGE_SECRET=<общий-секрет>
```

**Support-bot `.env`:**

```env
SHOP_WEBHOOK_URL=http://shop:8080/cabinet/api/internal/support/webhook
SUPPORT_BRIDGE_SECRET=<тот-же-секрет>
HTTP_PORT=8080
```

Имена сервисов `shop` / `support-bot` — примеры; используйте DNS-имена из вашего `docker-compose.yml`. Не `localhost` между контейнерами.

### Поведение для саппорта

- Cabinet-тикеты — отдельный канал **`MessageSource.cabinet`**: в топике видны метка кабинета, email/TG/подписка из shop.
- Ответ саппорта в топике **не уходит в Telegram DM** пользователю — только webhook в shop.
- **`/close`** в топике cabinet-тикета шлёт событие `closed` в shop; история в модале кабинета после закрытия не показывается (новое сообщение = новый тикет).

### Ограничения MVP

- Из кабинета принимается только **текст**.
- **Медиа** от саппорта в кабинет **не доставляются**.
- **Один open-тикет** на `account_id` (на стороне shop).

**Код:** `app/http/server.py`, `app/services/cabinet_bridge.py`, `app/services/shop_webhook.py`, `app/handlers/group_topics.py` (ответы и `/close` для `source=cabinet`).

---

## Редактирование текстов (локализация)

- Все пользовательские строки и подписи кнопок лежат в **`texts/ru/*.yml`** (можно добавить `texts/en/` и переключать `BOT_LOCALE=en`).  
- Файлы по смыслу: `user.yml`, `admin.yml`, `group.yml`, `callbacks.yml`, `flow.yml`, `keyboards.yml`, `errors.yml`.  
- Шаблон посева ссылок для раздела «Помощь»: `texts/ru/help_links_seed.yml` (один раз при пустой таблице в БД).  
- В посеве и в БД у текстовых пунктов поле **`body_text: |`** — это синтаксис **YAML** (литеральный блок: `|` не входит в текст, а значит «всё ниже с отступом — многострочная строка»). Внутри можно писать **HTML-разметку Telegram** (`<b>`, `<i>`, `<a href="...">` и т.д., см. [документацию Bot API](https://core.telegram.org/bots/api#html-style)).
- В коде используется вызов вида `tr("user", "start")` или с подстановкой `tr("admin", "block_success", uid=123)`.  
- После изменения YAML перезапустите процесс бота (контейнер).

---

## Действия и интерфейс

### Клиент (личка с ботом)

| Действие | Описание |
|----------|----------|
| `/start` | Приветствие + **нижняя клавиатура**: «Создать обращение» и «Помощь»; для пользователей из `ADMIN_IDS` добавляется строка **«Админ-панель»** |
| **Создать обращение** | Включает сценарий: сначала приглашение описать проблему, затем обычный флоу (тикет + пересылка в группу) |
| **Помощь** | Текст `help_section_intro` и кнопки из БД: **ссылка** (открывается во внешнем браузере) или **статья** (текст в боте и «Назад»); список настраивается в админ-панели |
| `/help` | Тот же раздел, что и кнопка «Помощь» |
| `/close` | Закрыть своё открытое обращение (уведомление уходит в тему группы) |
| Любое сообщение / медиа (вне сценария с кнопки) | Сразу в топик — как раньше (прямой чат с поддержкой) |

### Администратор (`ADMIN_IDS`), личка — без команд вручную

Управление через **нижнюю кнопку «Админ-панель»** или команду **`/admin`** — открывается сообщение с **инлайн-клавиатурой**:

| Раздел | Что делает |
|--------|------------|
| **Заблокировать** | Запрос ID → затем причина или кнопка «Без причины» |
| **Разблокировать** | Запрос Telegram ID одним сообщением |
| **Кнопки «Помощь»** | Список пунктов с кнопками **▲ / ▼ / удалить**, добавление пошагово (заголовок → URL), кнопка **«В панель»** назад |
| **Закрыть** | Убирает инлайн-клавиатуру у этого сообщения панели |

Тексты кнопок и подсказки лежат в **`texts/ru/admin.yml`** (`tr("admin", …)`).

После обновления бота отправьте **`/start`**, чтобы появилась нижняя строка «Админ-панель».

### Администратор в группе поддержки (внутри темы тикета)

| Действие | Описание |
|----------|----------|
| `/close` | Закрыть тикет (только администраторы из `ADMIN_IDS`) |
| `/profile` | Карточка клиента: Telegram + подписка Remnawave (если `REMNAWAVE_ENABLED`) |
| Сообщение саппорта в теме | Копируется клиенту в ЛС от бота **без** инлайн-кнопок |

Команды `/close` и `/profile` в теме доступны только пользователям из **`ADMIN_IDS`**.

Реакции администратора на **пересланные сообщения клиента** в топике дублируются на соответствующее сообщение клиента в ЛС (через бота).

---

## Устройство репозитория (кратко)

```
app/
  main.py              # точка входа, long polling Telegram
  config.py            # Settings из env
  i18n.py              # загрузка texts/<locale>/*.yml, функция tr()
  states.py            # FSM-состояния (админка, сценарий обращения)
  db/                  # модели SQLAlchemy, сессия
  handlers/            # роутеры aiogram: private_chat, admin_panel, group_topics, help_callbacks, message_reactions
  http/                # aiohttp sidecar: POST /internal/cabinet/message (bridge к shop)
  keyboards/           # инлайн-клавиатуры (тексты из YAML)
  services/            # тикеты, «Помощь», реакции, cabinet_bridge, shop_webhook, remnawave_client, remnawave_profile
texts/ru/              # тексты интерфейса (*.yml)
docker-compose.yml
Dockerfile
requirements.txt
```

## Частые проблемы

- **Ответы из группы не доходят до клиента** — включите отключение privacy: @BotFather → `/setprivacy` → **Disable**.    
- **Бот не создаёт топики** — проверьте, что группа с **темами**, бот — **админ** с правом **Manage topics**.
- **Remnawave: `Server disconnected`** — включите `REMNAWAVE_PROXY_HEADERS=true` (для `local` уже по умолчанию), задайте `REMNAWAVE_HTTP_HOST` = `PANEL_DOMAIN`; проверьте сеть (`getent hosts remnawave`).

---
