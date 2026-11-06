# Часть 3. handlers/menu.py, handlers/notes.py, handlers/registration.py

---

## handlers/menu.py — главное меню

Файл (508 строк): главное меню, его клавиатура, обработчик кнопок меню и показ меню.

### `get_main_menu_keyboard(user_id) -> KeyboardBuilder` (строка 87)

Собирает клавиатуру главного меню **в зависимости от роли**.

```python
role = db.get_user_field(user_id, "role")

base = [
    ("❓ Вопрос по уроку", "ai_question"),
    ("📅 Расписание дня", "ai_schedule"),
    ("📝 Создать тест", "ai_test"),
    ("📚 Помощь с домашкой", "ai_homework"),
    ("📄 Файлы и медиа", "menu_media"),
    ("📋 Мои заметки", "menu_notes"),
    ("👤 Мой профиль", "menu_profile"),
    ("⚙️ Настройки", "menu_settings"),
]

teacher_extra = [
    ("📝 Набросок текста", "ai_presentation"),
    ("📖 План урока по теме", "ai_topic"),
    ("👨🏫 История учеников", "ai_students_history"),
    ("📊 Логи ИИ", "menu_ai_logs"),
]

kb = KeyboardBuilder()
if db.is_moderator(user_id) or role in ("teacher", "moderator"):
    kb.add_callback(text="👨🏫 История учеников", payload="ai_students_history")
    kb.add_callback(text="📊 Логи ИИ", payload="menu_ai_logs")
    if db.is_moderator(user_id):
        kb.add_callback(text="🛡 Модерация", payload="menu_moderation")

for text, payload in base:
    kb.add_callback(text=text, payload=payload)

if role in ("teacher", "moderator") or db.is_moderator(user_id):
    for text, payload in teacher_extra:
        kb.add_callback(text=text, payload=payload)

if role not in ("teacher", "moderator") and not db.is_moderator(user_id):
    kb.add_callback(text="📊 Моя история", payload="ai_my_history")

return kb.adjust(1)
```

**Построчно.**

- `role = db.get_user_field(...)` — роль из БД ("student"/"teacher"/"moderator"/None).
- `base` — кнопки, видимые всем: 4 ИИ-функции и 4 раздела бота. Каждый кортеж — 
  `(текст который видит пользователь, payload который понимает код)`. Payload меняться не должен.
- `teacher_extra` — инструменты для учителя/модератора (набросок, план урока, история,
  логи ИИ) — в меню ученика этих кнопок нет.
- `kb = KeyboardBuilder()` — «холст» клавиатуры; `add_callback` (текст+payload) каждый раз
  добавляет кнопку в конец.
- Проверка `db.is_moderator(user_id) or role in (...)` — **двойная страховка**: роль могла
  смениться в БД, но `is_moderator` ещё True. Оба `if` (а не `elif`) — модератор получает и
  учительские кнопки, и кнопку «🛡 Модерация».
- `kb.adjust(1)` — по **одной кнопке в ряд**: при нескольких кнопках в ряду MAX обрезает
  длинный текст, поэтому все кнопки кладутся вертикально.

### `show_main_menu(update, user_id)` (строка 223)

Отправляет/обновляет главное меню. Вызывается из /start, /menu, после подтверждения.

```python
user = db.get_user(user_id)
if not user:
    await update.answer_text("Произошла ошибка. Напиши /start")
    return

prefs = get_ui_prefs(user_id)
show_emoji = prefs["show_emoji"]
compact    = prefs["compact_mode"]
show_s     = prefs["show_stats"]

name = user.get("full_name") or user.get("name", "Пользователь")
role = user.get("role", "unknown")
role_emoji = {"student": "📚", "teacher": "👨🏫", "moderator": "🛡"}.get(role, "👤")
role_text  = {"student": "Ученик", "teacher": "Учитель", "moderator": "Модератор"}.get(role, "Неизвестно")

if compact:
    text = f"Привет, {name}!\n\n" "Выбери раздел:"
else:
    # обычный вид: приветствие + роль + школа/класс + счётчики (если show_s)
    ...
```

- `db.get_user` — если записи нет — просим написать /start.
- Три флага интерфейса из prefs.py (эмодзи/компакт/счётчики).
- `name` — `full_name or name or "Пользователь"`.
- `role_emoji/role_text` — словари-мапперы роли в иконку и человеческое название.
- Компактный режим: короткое приветствие без счётчиков и роли.
- Далее (не показано): обычный режим собирает текст с ролью, школой/классом и счётчиками
  (`ai_used_today/ai_daily_limit`, количество заметок), после чего отправляет сообщение с
  `get_main_menu_keyboard(user_id).build()` (через `answer_text` для сообщений или
  `edit_message` для колбэков).

### `handle_menu_callbacks(update, fsm_context)` (строка 327)

Обработчик всех кнопок меню. Фильтр `CallbackPayload(prefixes=("menu_main", "menu_profile", "menu_settings", "menu_notes", "menu_media", "menu_ai_logs", "menu_moderation", ...))`.
Ветви по payload:
- `menu_main` — показать главное меню (возврат из любого раздела).
- `menu_profile` — карточка профиля пользователя.
- `menu_settings` — открыть раздел настроек.
- `menu_notes` — открыть раздел заметок.
- `menu_media` — открыть раздел «Файлы и медиа».
- `menu_ai_logs` — открыть логи ИИ (учитель/модератор).
- `menu_moderation` — открыть инструменты модератора (только модератор).

Перед обработкой — защитный барьер (существует + одобрен + не заблокирован), как в notes.py.
Незарегистрированные/заблокированные молча игнорируются.

### `_is_registration_state(state)` (строка 473)

```python
def _is_registration_state(state: str | None) -> bool:
    return bool(state and state.startswith("RegistrationStates"))
```

Вспомогательная проверка: «находится ли пользователь сейчас в одном из шагов анкеты».
Используется, чтобы не «перебивать» активную анкету случайными действиями из меню.

---

## handlers/notes.py — заметки

Файл (510 строк): раздел личных заметок пользователя.

### Клавиатуры (строки 58–103)

```python
def _list_keyboard():
    kb = KeyboardBuilder()
    kb.add_callback(text="📝 Создать новую", payload="note_create")
    kb.add_callback(text="← Назад в меню", payload="menu_main")
    return kb.adjust(1)

def _back_to_list_keyboard(action_text, action_payload):
    kb = KeyboardBuilder()
    kb.add_callback(text=action_text, payload=action_payload)
    kb.add_callback(text="← Назад к списку", payload="menu_notes")
    return kb.adjust(1)
```

- `_list_keyboard` — экран пустого списка: «Создать новую» + «Назад в меню».
- `_back_to_list_keyboard` — кнопка действия (например «📝 Создать ещё одну») + возврат
  к списку. Обе раскладывают кнопки по одной в ряд (`adjust(1)`) — чтобы MAX не обрезал
  длинные тексты.

### `handle_notes_menu(update, fsm_context)` (строка 126)

Один обработчик на все кнопки раздела: фильтр
`CallbackPayload("menu_notes", "note_create", prefixes=("note_view_", "note_delete_"))`.

```python
payload = update.payload
user_id = get_user_id(update)

if not db.user_exists(user_id) or not db.is_approved(user_id) or db.is_blocked(user_id):
    return

if payload == "menu_notes":
    await fsm_context.clear()
    notes = db.get_notes(user_id)
    if not notes:
        # пустой список: предлагаем создать первую
    else:
        # список заметок; каждая заметка — своя кнопка note_view_<i>
elif payload == "note_create":
    # переход в FSM: set_state(NotesStates.wait_title)
elif payload starts with "note_view_":
    index = _parse_index(payload)
    # показать одну заметку (db.get_notes + notes[index]) + back к списку
elif payload starts with "note_delete_":
    index = _parse_index(payload)
    # db.delete_note(user_id, index) + перерисовать список
```

- Сначала «входной барьер»: существует + одобрен + не заблокирован (молча выходим иначе).
- `menu_notes` — сбрасывает FSM (`fsm_context.clear()`), тянет заметки и рисует список.
  Список пуст → предложение создать. Есть заметки → по кнопке `note_view_<i>` на каждую.
- `note_create` — включает FSM-ввод заголовка (`set_state(NotesStates.wait_title)`).
- `note_view_<i>` — показывает заметку по индексу, под кнопкой «← Назад к списку».
- `note_delete_<i>` — удаляет заметку (`db.delete_note`) и перерисовывает список.

### `handle_notes_text(update, fsm_context)` (строка 391)

Обработчик FSM-ввода текста (фильтр `StateFilter(NotesStates)`):
- В состоянии `wait_title`: принимаем заголовок, сохраняем во временные данные
  (`fsm_context.update_data(note_title=...)`) и переходим в `wait_content`.
- В состоянии `wait_content`: содержимое + заголовок из временных данных →
  `db.save_note(user_id, title, content)`. После сохранения показываем «Заметка создана»,
  под кнопкой «Создать ещё одну»/«Назад к списку» (`_back_to_list_keyboard`).

### `_parse_index(payload)` (строка 490)

```python
def _parse_index(payload: str) -> int | None:
    try:
        return int(payload.rsplit("_", 1)[1])
    except (ValueError, IndexError):
        return None
```

Безопасно достаёт число из payload вида `note_view_3`: `rsplit("_",1)[1]` — часть после
последнего `_`, `int(...)` — в число. Не получается (None/mусор) → `None`, вызов аккуратно
обрабатывается наверху.

---

## handlers/registration.py — регистрация

Файл (905 строк): приём `/start`, выбор роли, многошаговая анкета (FSM), финал и
создание записи в БД.

### Клавиатуры (строки 71–130)

- `_role_keyboard()` — кнопки «Я ученик» (`reg_student`), «Я учитель» (`reg_teacher`).
- `_back_keyboard()` — кнопка «← Назад» (возврат на выбор роли).
- `_skip_keyboard()` — кнопка «Пропустить» (`reg_skip`) для опциональных шагов (email, возраст).

### `cmd_start(update)` (строка 132) — обработчик `/start`

```python
@router.message_created(CommandStart())
async def cmd_start(update: MessageCreated) -> None:
    user_id = resolve_user_id(update)
    user_name = get_user_name(update)
    chat_id = getattr(update, "chat_id", None)
    # диагностика в консоль (try/except, print flush=True)

    if db.user_exists(user_id):
        if db.is_approved(user_id):
            from handlers.menu import show_main_menu
            await show_main_menu(update, user_id)
        else:
            await update.answer_text(f"Привет, {user_name}!\nТвоя заявка ещё не подтверждена...")
        return

    await update.answer_text(
        f"Привет, {user_name}! 👋 ... Сначала нужно зарегистрироваться.\nКто ты?",
        keyboard=_role_keyboard().build(),
    )
```

- `resolve_user_id` — ID пользователя (с fallback по chat_id).
- `chat_id = getattr(update, "chat_id", None)` — **важно**: сохраняется в БД, чтобы после
  подтверждения модератором бот смог написать личное уведомление.
- Ветки: одобрен → меню; есть в БД, но не одобрен → «подожди»; новый → анкета с выбором роли.
- Ветка «подтверждён» — ленивый импорт `show_main_menu` (избегаем циклических импортов).

### `handle_registration_callbacks(update, fsm_context)` (строка 225)

Фильтр `CallbackPayload("reg_student", "reg_teacher", "reg_cancel", "reg_skip")`. Ветви:
- `reg_student` — `set_state(RegistrationStates.wait_phone_student)`, просим телефон.
- `reg_teacher` — `set_state(RegistrationStates.wait_phone_teacher)`, просим телефон.
- `reg_cancel` — `fsm_context.clear()`, выход из анкеты.
- `reg_skip` — пропуск опционального шага (email/возраст): переходим к следующему шагу
  через общий механизм `next_step` (см. ниже).

### `handle_registration_text(update, fsm_context)` (строка 399)

Обработчик FSM-ввода анкеты (`StateFilter(RegistrationStates)`). Ветви по состоянию:
- телефон → проверка на цифры и длину (только цифры, ~10+ символов), сохраняем в data;
- пароль → длина не менее 4 символов;
- имя → непустое (имя и фамилия);
- класс (ученик) → валидация формата (например «5А», «9Б»);
- школа → непустое название;
- email (опц.) → простая проверка «похоже на email»;
- возраст (опц.) → `_parse_age(text)` (число в разумном диапазоне).

Каждый шаг после валидации продвигает анкету через `next_step(...)`.

### `next_step(next_state, data_key, data_value)` (строка 434)

```python
async def next_step(next_state, data_key, data_value):
    async def wrapper(update, fsm_context, user_id, user_name, chat_id, current_data):
        await fsm_context.update_data(**{data_key: data_value})
        if next_state is None:
            await _finish_student(...)  # или _finish_teacher(...)
        else:
            await fsm_context.set_state(next_state)
            # и задать вопрос следующего шага
    return wrapper
```

Общий «движок» перехода между шагами: сохраняет значение текущего шага во временных данных
FSM, затем либо переводит в следующее состояние (с новым вопросом), либо завершает анкету
(для последнего шага `next_state=None`).

### `_parse_age(text)` (строка 745)

Парсит возраст: число в диапазоне 0–120 (или аналогичном). Не число/вне диапазона → None
(вызывающий код просит ввести заново).

### `_finish_student(...)` / `_finish_teacher(...)` (строки 764/842)

Завершение анкеты:
- Из временных данных FSM собирается полный профиль (phone, password, name/surname, school,
  class (ученик), email, age).
- `db.create_user(user_id, name=full_name, role="pending")` + `db.set_user_field(...)` для
  каждого поля анкеты; фиксируется `registered_at`, `chat_id` и `role_requested`.
- Если `REQUIRE_MODERATION` — уведомляются модераторы (`notify_moderators` из moderation.py):
  «пришла новая заявка», с кнопкой просмотра. Иначе пользователь сразу одобряется.
- Пользователь получает сообщение «Заявка отправлена на рассмотрение» и кнопку главного меню
  (после подтверждения).

**Итог:** анкета строится на цепочке состояний RegistrationStates; у ученика 7 шагов, у
учителя 6 (без класса); email и возраст опциональны (кнопка «Пропустить»).