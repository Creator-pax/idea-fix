## config.py — настройки бота

Файл из 75 строк — набор констант, единый «источник истины» для всего проекта.
Никакой логики, только данные.

### Строки 1–16 (шапка, токен, ИИ, БД)

```python
# ============================================================
# КОНФИГУРАЦИЯ БОТА — ВСЕ НАСТРОЙКИ ЗДЕСЬ
# ============================================================

# Токен бота MAX — получи его через @MasterBot в MAX
BOT_TOKEN = "f9LHodD0cOIgHGLgvj4MHPLFHncB49id7PUIbyMc07FywMwxEM2FirvwDJdpSiHrZJprlDTMz4ymT3RUPx9t"


AI_API_KEY = "lm-studio"
AI_MODEL = "qwen3.5-0.8b"

# ============================================
# НАСТРОЙКИ БАЗЫ ДАННЫХ
# ============================================
# Имя файла для хранения данных пользователей
DATABASE_FILE = "bot_data.db"
```

- `BOT_TOKEN` — токен бота из MAX, передаётся в `Bot(token)` (см. bot_instance.py).
- `AI_API_KEY = "lm-studio"` — ключ для локального ИИ-сервера LM Studio.
- `AI_MODEL = "qwen3.5-0.8b"` — имя модели, которое умеет LM Studio.
- `DATABASE_FILE = "bot_data.db"` — имя SQLite-файла; `database.py` импортирует эту
  константу, поэтому и бот, и панель работают с одной базой.

### Строки 18–46 (лимиты и настройки ИИ)

```python
AI_DAILY_LIMIT_STUDENT = 50
AI_DAILY_LIMIT_TEACHER = 100
AI_MAX_INPUT_LENGTH = 2000
AI_MAX_OUTPUT_LENGTH = 4000
AI_TIMEOUT_SECONDS = 900
AI_FOLLOWUP_DEFAULT = 0
AI_EXAMPLES_DEFAULT = 1
AI_DETAIL_DEFAULT = "normal"
```

- `AI_DAILY_LIMIT_STUDENT` — дневной лимит запросов ИИ для ученика (50).
- `AI_DAILY_LIMIT_TEACHER` — тот же лимит для учителя (100) — больше, т.к. учителя.
- `AI_MAX_INPUT_LENGTH` — обрезка входящего текста до 2000 символов (ограничение API).
- `AI_MAX_OUTPUT_LENGTH` — сколько символов ответа брать максимум (4000).
- `AI_TIMEOUT_SECONDS = 900` — **таймаут ИИ 15 минут**. Локальная модель на слабом ПК
  может думать долго; таймаут передаётся в `requests.post` в `handlers/ai.py`.
- `AI_FOLLOWUP_DEFAULT = 0` — по умолчанию наводящие вопросы в конце ответа выключены.
- `AI_EXAMPLES_DEFAULT = 1` — примеры/аналогии в ответе включены.
- `AI_DETAIL_DEFAULT = "normal"` — степень подробности по умолчанию (`short/normal/detail`).
  Эти три значения применяются для **новых** пользователей и меняются каждым
  пользователем в «Настройки → Настройки ИИ».

### Строки 48–75 (модерация, интерфейс, имя бота)

```python
REQUIRE_MODERATION = True
MODERATOR_IDS = []
UI_SHOW_EMOJI = True
UI_COMPACT_MODE = False
UI_SHOW_STATS = True
BOT_NAME = "MAX Школьный помощник"
BOT_DESCRIPTION = "Помощник для школьников с ИИ, заметками и настраиваемым интерфейсом"
```

- `REQUIRE_MODERATION = True` — новые пользователи проходят подтверждение модератором.
- `MODERATOR_IDS = []` — список числовых ID модераторов. Пуст: модераторы назначаются
  через роль в БД (команда `/make_moderator`, см. moderation.py). Сюда можно дописать
  фиксированные ID.
- `UI_SHOW_EMOJI/UI_COMPACT_MODE/UI_SHOW_STATS` — дефолты интерфейса для новых
  пользователей (эмодзи вкл, компактный режим выкл, счётчики вкл).
- `BOT_NAME/BOT_DESCRIPTION` — название и описание, показываются в меню/карточке бота.

---

## bot_instance.py — центральные объекты

Файл из 44 строк — создаёт два глобальных объекта фреймворка `maxo`:

```python
from maxo import Bot, Dispatcher
from config import BOT_TOKEN

bot: Bot = Bot(BOT_TOKEN)
dp: Dispatcher = Dispatcher()
```

**Построчно.**

- `from maxo import Bot, Dispatcher` — импорт двух главных классов библиотеки.
- `from config import BOT_TOKEN` — импорт токена из config.py.
- `bot = Bot(BOT_TOKEN)` — клиент/точка входа в MAX Bot API. Умеет слать и редактировать
  сообщения, отвечать на колбэки, скачивать файлы (`bot.download`). Внутри уже настроены
  авторизация и привязка всех API-методов (`send_message`, `edit_message` и т.п.).
- `dp = Dispatcher()` — диспетчер: корневой узел, который принимает все апдейты от
  long-polling и раздаёт их роутерам. По умолчанию подключает FSM (MemoryStorage) и
  middlewares, поэтому обработчики получают параметра `fsm_context`.

Этот файл импортируется всеми остальными — чтобы везде был **один и тот же** экземпляр
`bot`/`dp` (синглтон процесса). Роутеры из `handlers/*` подключаются к `dp` в
`handlers/__init__.py` через `dp.include_router(...)` («дерево роутеров»).

---

## mian.py — главный файл

Точка входа бота (`python mian.py`), 178 строк. Порядок работы описан в шапке файла:
1) импорт bot/dp; 2) middleware синхронизации БД; 3) импорт handlers (подключает роутеры);
4) команды бота по сигналу `after_startup`; 5) поллинг.

### Строки 18–29 — импорты

```python
import asyncio
import dataclasses
import json
import os
from maxo import BaseMiddleware, Ctx
from maxo.routing.interfaces.middleware import NextMiddleware
from maxo.routing.signals.update import MaxoUpdate
from maxo.types import BotCommand
from bot_instance import bot, dp
```

- `asyncio` — для `asyncio.to_thread` (вынос блокирующей записи лога в поток).
- `dataclasses`/`json` — отладочное превращение апдейта в читаемый JSON.
- `os` — построение абсолютного пути к файлу лога.
- Типы maxo (`BaseMiddleware`, `Ctx`, `NextMiddleware`, `MaxoUpdate`, `BotCommand`) —
  для написания типизированного middleware и команды бота.
- `bot, dp` — центральные объекты из bot_instance.py.

### Строки 31–39 — синхронизация БД

```python
import database as db   # db.reload() — теперь «пустышка», но вызов оставлен
```

Почему: бот и панель модератора — отдельные процессы, каждый со своей копией
`bot_data.db` в памяти. Раньше `db.reload()` перечитывал файл; теперь SQLite читает
с диска при каждом запросе, поэтому функция стала «пустышкой», но вызов сохранён для
совместимости.

### Строки 42–103 — класс SyncAndLogMiddleware

```python
class SyncAndLogMiddleware(BaseMiddleware[MaxoUpdate[object]]):
    def __init__(self, debug_log_file: str | None = None) -> None:
        self._debug_log_file = debug_log_file

    async def __call__(self, update, ctx, next) -> object:
        db.reload()
        if self._debug_log_file is not None:
            await asyncio.to_thread(self._dump_update, update)
        return await next(ctx)

    def _dump_update(self, update) -> None:
        try:
            raw = dataclasses.asdict(update.update)
            text = json.dumps(raw, ensure_ascii=False, default=str)
        except Exception:
            text = str(update.update)
        try:
            with open(self._debug_log_file, "a", encoding="utf-8") as f:
                f.write("=== UPDATE ===\n"); f.write(text); f.write("\n\n")
        except Exception:
            pass
```

**Построчно.**
- `__init__(debug_log_file)` — запоминает путь к логу; `None` — логирование выключено.
- `__call__(update, ctx, next)` — вызывается ДО обработчика для каждого апдейта:
  - `db.reload()` — синхронизация БД с диска (мгновенно, «пустышка»);
  - если задан файл лога — пишет сырой апдейт через `asyncio.to_thread` (не блокируя loop);
  - `return await next(ctx)` — **обязательно** передаёт управление дальше по цепочке,
    иначе апдейт не будет обработан.
- `_dump_update` — сериализует апдейт: `asyncio.asdict` → dict, `json.dumps` → строка;
  при ошибке — просто `str(update.update)`. Записывает в файл в режиме `"a"` (append).
  Любые исключения глушатся: лог не должен ломать бота.

### Строки 106–126 — регистрация middleware и подключение обработчиков

```python
_DEBUG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "bot.debug.log")
dp.update.middleware.outer(SyncAndLogMiddleware(debug_log_file=_DEBUG_FILE))

import handlers  # noqa: F401, E402
```

- `_DEBUG_FILE` — лог кладётся рядом с проектом (абсолютный путь от этого файла).
- `dp.update.middleware.outer(...)` — middleware вешается «снаружи» всех остальных —
  выполняется раньше всех на каждом апдейте.
- `import handlers` — побочный эффект: пакет при импорте вызывает `dp.include_router(...)`
  для всех роутеров. Импорт обязан идти ПОСЛЕ middleware (иначе роутеры зарегистрируются
  раньше нужного слоя).

### Строки 129–151 — команды бота

```python
@dp.after_startup()
async def setup_commands(update) -> None:
    await bot.edit_my_commands(
        commands=[
            BotCommand(name="start", description="Регистрация и главное меню"),
            BotCommand(name="help", description="Справка по командам"),
            BotCommand(name="menu", description="Главное меню"),
            BotCommand(name="myid", description="Узнать свой ID"),
        ]
    )
```

- `@dp.after_startup()` — регистрирует команды только когда бот уже запущен и готов
  (есть `bot.state.info`). Внутри `run_polling` стартовые сигналы выполняются сами.
- `bot.edit_my_commands(...)` — задаёт список «/команды», показываемый пользователям MAX.

### Строки 160–178 — запуск поллинга

```python
if __name__ == "__main__":
    print("Bot starting...")
    print("MAX School Helper v1.0.0")
    print("Press Ctrl+C to stop")
    print("-" * 40)
    try:
        dp.run_polling(bot)
    except KeyboardInterrupt:
        print("\n" + "-" * 40)
        print("Bot stopped. Goodbye!")
    except Exception as e:
        print(f"\nError: {e}")
        raise
```

- Секция выполняется только при прямом запуске (`python mian.py`), не при импорте.
- `dp.run_polling(bot)` — блокирующий цикл приёма апдейтов; внутри срабатывают сигналы
  (регистрация команд) и раздача апдейтов по роутерам.
- `KeyboardInterrupt` — корректная остановка по Ctrl+C.
- Остальные исключения печатаются и пробрасываются (`raise`) — процесс упадёт с
  ненулевым кодом, что удобно для логов/мониторинга.

---

## reset_test_user.py

Dev-утилита (65 строк): полностью удаляет тестовый аккаунт из БД, чтобы заново пройти
регистрацию и проверить уведомление после подтверждения в панели.

```python
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import database as db

UID = 115511930

def reset():
    if not db.user_exists(UID):
        print(f"Пользователь {UID} уже отсутствует в БД — регистрация начнётся заново при /start.")
    else:
        print(f"Удаляю пользователя {UID} из БД...")
        removed = db.delete_user(UID)
        print(f"  удалено строк: {removed}")
    print("")
    print("Теперь в боте нажми /start — начнётся регистрация заново.")

if __name__ == "__main__":
    reset()
```

**Построчно.**
- `sys.path.insert(0, ...)` — добавляет корень проекта в пути поиска модулей, чтобы
  работал `import database` из любой папки запуска.
- `UID = 115511930` — ID тестового аккаунта (это первичный ключ в `users`).
- `db.user_exists(UID)` — если пользователя нет — печатаем сообщение, удалять нечего.
- `db.delete_user(UID)` — удаляет строки из всех таблиц (`users`, `notes`, `ai_log`,
  `ai_errors`) и возвращает число удалённых строк.
- Подсказки в `print` напоминают порядок: `/start` → заявка в панели → «Подтвердить» →
  уведомление на сохранённый `chat_id`.
- `if __name__ == "__main__"` — сброс выполняется только при прямом запуске.