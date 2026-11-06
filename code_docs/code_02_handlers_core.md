# Часть 2. Пакет handlers: точка сборки, фильтры, состояния, утилиты

---

## handlers/__init__.py — сборка роутеров

Файл (75 строк) — «точка сборки» пакета handlers. При импорте (`import handlers`)
подключает все роутеры к общему диспетчеру `dp`.

```python
from bot_instance import dp

from handlers import ai
from handlers import export
from handlers import media
from handlers import menu
from handlers import moderation
from handlers import notes
from handlers import registration
from handlers import settings
from handlers import start

dp.include_router(menu.router)          # menu первым — перехватывает кнопку "menu_main"
dp.include_router(start.router)         # команды /help и /menu
dp.include_router(registration.router)  # регистрация: /start и ввод анкеты
dp.include_router(notes.router)         # раздел заметок (payload'ы note_*)
dp.include_router(ai.router)            # ИИ-раздел (вопросы, тесты)
dp.include_router(export.router)        # сохранение ответов ИИ в файлы
dp.include_router(settings.router)      # раздел настроек
dp.include_router(moderation.router)    # инструменты модератора
dp.include_router(media.router)         # файлы и медиа
```

**Почему так, построчно.**

- `from bot_instance import dp` — берём тот же диспетчер, что создан в bot_instance.py.
- Импорты модулей — каждый модуль при импорте создаёт свой `Router` и регистрирует
  обработчики (это «побочный эффект» импорта). Порядок импортов НЕ влияет на
  маршрутизацию — её задаёт порядок `include_router` ниже.
- **`dp.include_router(menu.router)` первым.** В maxo первый роутер, чей фильтр совпал
  с апдейтом, «забирает» его и останавливает поиск. Кнопка «← Назад в меню» (payload
  `menu_main`) универсальна и должна обрабатываться раньше всех остальных кнопок.
- Остальные роутеры фильтруют свои payload'ы взаимоисключающе, поэтому их порядок
  друг относительно друга не критичен (но сохранён логичным: анкета → заметки → ИИ →
  экспорт файлов → настройки → модерация → медиа).
- `__all__ = [...]` (строки 65–75) — перечисляет подмодули для `from handlers import *`
  (ai, export, media, menu, moderation, notes, registration, settings, start).

---

## handlers/filters.py — свой фильтр колбэков

Файл (115 строк) определяет класс `CallbackPayload` — «пропускную рамку» для кнопок.

### Строки 29–54 — импорты и начало класса

```python
from collections.abc import Iterable
from maxo import Ctx
from maxo.omit import is_defined
from maxo.routing.filters import BaseFilter
from maxo.types import MessageCallback

class CallbackPayload(BaseFilter[MessageCallback]):
```

- `Iterable` — только аннотация типа: `prefixes` — любой перечисляемый объект.
- `Ctx` — контекст обработчика maxo (FSM, данные апдейта).
- `is_defined` — проверка, что `Omittable`-поле (которое может отсутствовать) реально задано.
- `BaseFilter` — базовый класс для своих фильтров maxo.
- `MessageCallback` — тип апдейта «нажата кнопка» (user, payload, callback).
- Дженерик `BaseFilter[MessageCallback]` — говорит системе, с каким типом апдейта
  работает фильтр (типизация и автодополнение).

### Строки 75–92 — `__slots__` и конструктор

```python
    __slots__ = ("values", "prefixes")

    def __init__(self, *values: str, prefixes: Iterable[str] = ()) -> None:
        self.values = tuple(values)
        self.prefixes = tuple(prefixes)
```

- `__slots__` — фиксирует список атрибутов экземпляра: экономит память, ускоряет доступ,
  не даёт случайно присвоить посторонние атрибуты.
- Конструктор принимает неограниченное число точных значений (`*values`) и список
  префиксов (`prefixes`).
- Оба хранятся как **кортежи** — «замороженный» фильтр (нельзя случайно изменить извне).

### Строки 94–115 — главный метод `__call__`

```python
    async def __call__(self, update: MessageCallback, ctx: Ctx) -> bool:
        if not is_defined(update.payload):
            return False

        payload = update.payload

        if payload in self.values:
            return True

        return any(payload.startswith(prefix) for prefix in self.prefixes)
```

- `is_defined(update.payload)` — у кнопки payload может просто не быть (Omittable);
  если нет — фильтр не наш, возвращаем `False`.
- `payload in self.values` — точное совпадение (например `"menu_notes"`).
- `any(payload.startswith(prefix) for prefix in self.prefixes)` — совпадение по префиксу
  (например `"note_view_3".startswith("note_view_")` → True). `any` вернёт True, если
  payload начинается хотя бы с одного префикса.
- Итог: `True` — апдейт наш (маячит в обработчик), `False` — поиск подходящего роутера продолжается.

---

## handlers/states.py — FSM-состояния

Файл (155 строк) описывает **все FSM-состояния** бота. FSM — конечный автомат состояний,
штатный механизм maxo: состояние хранится в хранилище и «привязано» к пользователю и чату;
вместе с состоянием можно хранить временные данные (`fsm_context.update_data`).

```python
from maxo.fsm import State, StatesGroup

class RegistrationStates(StatesGroup):
    wait_phone_student = State()
    wait_password_student = State()
    wait_name_student = State()
    wait_class_student = State()
    wait_school_student = State()
    wait_email_student = State()
    wait_age_student = State()

    wait_phone_teacher = State()
    wait_password_teacher = State()
    wait_name_teacher = State()
    wait_school_teacher = State()
    wait_email_teacher = State()
    wait_age_teacher = State()

class AiStates(StatesGroup):
    wait_question = State()

class NotesStates(StatesGroup):
    wait_title = State()
    wait_content = State()

class SettingsStates(StatesGroup):
    wait_topics = State()
    wait_join_school = State()
    wait_join_class = State()
    wait_leave_confirm = State()
    wait_limit_uid = State()
    wait_limit_value = State()
    wait_edit_name = State()
    wait_edit_phone = State()
    wait_edit_email = State()
    wait_edit_age = State()
    wait_edit_password = State()

class MediaStates(StatesGroup):
    wait_photo = State()
    wait_pdf = State()
    wait_video_audio = State()
    wait_video_keyframes = State()

class ModerationStates(StatesGroup):
    wait_uid = State()
    wait_limit = State()
    wait_broadcast = State()
```

**Пояснение по группам.**

- `RegistrationStates` — шаги анкеты ученика (с классом) и учителя (без класса).
  Порядок объявления задаёт логику шагов: телефон → пароль → имя → класс (ученик) →
  школа → email → возраст. Каждое поле-`State()` — отдельное состояние, поэтому каждому
  шагу можно сделать свою валидацию.
- `AiStates.wait_question` — «ждём текст запроса к ИИ». Режим (вопрос/тест/расписание/домашка)
  хранится во временных данных FSM (`mode=...`), а не в состоянии.
- `NotesStates.wait_title / wait_content` — сначала заголовок заметки, потом содержимое.
  Заголовок кладётся во временные данные (`note_title`) при переходе в `wait_content`.
- `SettingsStates` — ввод тем ИИ (`wait_topics`), смена школы/класса (двухшаговая заявка
  `wait_join_school → wait_join_class`, подтверждение выхода `wait_leave_confirm`),
  смена лимита учителем (`wait_limit_uid → wait_limit_value`), редактирование персональных
  данных (по состоянию на каждое поле).
- `MediaStates` — по состоянию на каждый тип вложения: фото (OCR), PDF, видео (речь),
  видео (ключевые моменты). Обработка отличается, поэтому состояний четыре.
- `ModerationStates` — прямо в чате: ввод user_id (`wait_uid`), новый лимит (`wait_limit`),
  текст рассылки (`wait_broadcast`).

Важно про имена: `get_state()` возвращает строку вида `"RegistrationStates:wait_name_student"`.
Сравнивать эти строки вручную не нужно — есть `StateFilter(...)`, который принимает сами
объекты `State`/`StatesGroup`.

---

## handlers/user_utils.py — утилиты пользователя

Файл (147 строк) — три функции безопасного извлечения данных из апдейта.

### Зачем (строки 18–27)

У колбэка поле `update.message` — это исходное сообщение с клавиатурой, и его отправитель —
**бот**, а не пользователь. Поэтому для кнопок ID надо брать из `update.user`, а для
сообщений — из `update.user_id`. Эти функции — «рецепт против бага бота-отправителя».

### `get_user_id(update) -> int | None` (строки 49–75)

```python
def get_user_id(update: MessageCreated | MessageCallback) -> int | None:
    if isinstance(update, MessageCallback):
        return update.user.user_id
    return update.user_id
```

- Если апдейт — колбэк: возвращаем `update.user.user_id` (ID нажавшего).
- Иначе — сообщение: свойство `update.user_id` (учитывает, что отправителя может не быть → None).

### `resolve_user_id(update) -> int | None` (строки 78–120)

```python
def resolve_user_id(update) -> int | None:
    uid = get_user_id(update)
    if uid is not None:
        try:
            if _db.user_exists(uid):
                return uid
        except Exception:
            pass
    try:
        chat_id = getattr(update, "chat_id", None)
        found = _db.find_user_by_chat_id(chat_id)
        if found is not None:
            return found
    except Exception:
        pass
    return uid
```

**Построчно.** Сначала простой путь — `uid = get_user_id(update)`. Если он не None и
пользователь есть в БД — возвращаем его. Если нет (или БД временно недоступна — `try/except`,
который «проглатывает» ошибку) — пытаемся опознать по `chat_id` через
`_db.find_user_by_chat_id(chat_id)`. Не вышло — возвращаем исходный `uid` (возможно None).
Оба `except: pass` осознанно глотают исключения: resolve_user_id — последняя надежда узнать ID.

### `get_user_name(update) -> str` (строки 123–147)

```python
def get_user_name(update) -> str:
    if isinstance(update, MessageCallback):
        user = update.user
        return user.fullname or (user.name or "")
    sender = update.message.sender
    if is_defined(sender):
        return sender.fullname or (sender.name or "")
    return ""
```

- Колбэк: у `update.user` берём `fullname`, иначе `name`, иначе пустую строку.
- Сообщение: `sender` может отсутствовать (`Omittable`), проверяем `is_defined(sender)`.
- Возврат **пустой строки**, а не None: f-string подставил бы `None` в текст как слово.

---

## handlers/prefs.py — настройки интерфейса

Файл (193 строки) — «библиотека» утилит для трёх настроек интерфейса:
`show_emoji`, `compact_mode`, `show_stats`. Здесь нет роутеров, только функции.

### Ключи и регулярка (строки 43–72)

```python
UI_KEYS = ("show_emoji", "compact_mode", "show_stats")

_EMOJI_RE = re.compile(
    "[\U0000200D\U0000FE0F"
    "\U00002190-\U000021FF"
    "\U00002300-\U000023FF"
    "\U00002600-\U000027BF"
    "\U00002B00-\U00002BFF"
    "\U0001F000-\U0001FAFF]+"
)
```

- `UI_KEYS` — «белый список» ключей; ключи одновременно имена столбцов БД и ключи словаря.
- `_EMOJI_RE` — регулярка для вырезания эмодзи из текста кнопок. Покрывает: соединитель ZWJ
  (200D), вариативный селектор (FE0F), стрелки (2190–21FF), технические (2300–23FF), символы
  (2600–27BF), прочие (2B00–2BFF) и основной блок эмодзи (1F000–1FAFF). Escaped-видео
  (`\U...`) позволяет хранить файл в любой кодировке.

### Функции

```python
def btn(show_emoji: bool, text: str) -> str:
    if show_emoji:
        return text
    cleaned = _EMOJI_RE.sub("", text)
    return re.sub(r"\s{2,}", " ", cleaned).strip()
```

- `btn` — текст **кнопки** целиком. Если эмодзи включены — как есть; иначе вырезает эмодзи
  (`re.sub` на `_EMOJI_RE`), схлопывает двойные пробелы (`r"\s{2,}"` → один пробел) и чистит
  края (`.strip()`). Иногда «📝 Набросок» → «Набросок». Ранний выход упрощает код.

```python
def get_ui_prefs(user_id: int) -> dict:
    user = db.get_user(user_id) or {}
    return {
        "show_emoji": bool(user.get("show_emoji", config.UI_SHOW_EMOJI)),
        "compact_mode": bool(user.get("compact_mode", config.UI_COMPACT_MODE)),
        "show_stats": bool(user.get("show_stats", config.UI_SHOW_STATS)),
    }
```

- `get_ui_prefs` — словарь всех трёх настроек. Для каждого ключа: значение из БД или дефолт
  из config.py, приведённое к bool. Пользователь мог ни разу не открывать настройки — вернётся
  дефолт. Гарантированно возвращает все три ключа.

```python
def set_ui_pref(user_id: int, key: str, value) -> None:
    if key in UI_KEYS:
        db.set_user_field(user_id, key, 1 if value else 0)
```

- `set_ui_pref` — пишет только известный ключ (защита от мусорных столбцов); любое
  правдивое значение → 1, ложное → 0.

```python
def emoji(show_emoji: bool, symbol: str) -> str:
    return f"{symbol} " if show_emoji else ""

def ui_label(show_emoji: bool):
    return emoji(show_emoji, "🖥")
```

- `emoji` — возвращает `"🤖 "` или `""` (эмодзи с пробелом для встраивания в середину фраз).
- `ui_label` — обёртка: эмодзи-«иконка» раздела настроек интерфейса (одинарный источник).

---

## handlers/start.py — команды /help и /menu

Файл (136 строк). Здесь нет обработчика `/start` (он в registration.py) — только служебные
команды справки и быстрого возврата в меню.

### Общая настройка (строки 15–39)

```python
router: Router = Router(name="start")
```

- Импорты: `Router`, фильтр `Command`, тип `MessageCreated`, `database as db`, `get_user_id`.
- `router = Router(name="start")` — роутер с именем (видно в логах, кто обработал апдейт).

### Обработчик `/help` (строки 45–99)

```python
@router.message_created(Command("help"))
async def cmd_help(update: MessageCreated) -> None:
    user_id = get_user_id(update)
    if not db.user_exists(user_id):
        await update.answer_text(
            "📚 Справка\n\nДля начала напиши /start чтобы зарегистрироваться."
        )
        return
    help_text = (
        "📚 Справка по командам:\n\n"
        "🔹 /start — Главное меню\n"
        ...
        "• Интерфейс — эмодзи, компактный вид, счётчики"
    )
    await update.answer_text(help_text)
```

- `@router.message_created(Command("help"))` — обрабатывает сообщения вида `/help`.
- Получаем `user_id`; если пользователя нет в БД — короткая справка и ранний возврат.
- `update.answer_text(text)` — фасад maxo: отправляет текст в тот же чат.
- `help_text` — собирается склейкой строковых литералов в скобках (Python автоматически
  конкатенирует соседние строки). Разбит на блоки (команды/ИИ/другое) с эмодзи-маркерами.

### Обработчик `/menu` (строки 106–136)

```python
@router.message_created(Command("menu"))
async def cmd_menu(update: MessageCreated) -> None:
    user_id = get_user_id(update)
    if not db.user_exists(user_id) or not db.is_approved(user_id):
        await update.answer_text("Сначала напиши /start чтобы зарегистрироваться.")
        return
    from handlers.menu import show_main_menu
    await show_main_menu(update, user_id)
```

- Проверка двух условий: пользователь существует **И** одобрен модератором.
- Импорт `show_main_menu` внутрь функции — **ленивый импорт**: избегает циклической
  зависимости (menu.py сам импортирует другие модули handlers).
- `show_main_menu(update, user_id)` — отправка главного меню (см. menu.py).