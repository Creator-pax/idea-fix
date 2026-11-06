# Часть 4. handlers/moderation.py, handlers/settings.py

---

## handlers/moderation.py — инструменты модератора

Файл (1355 строк): модерация прямо в чате — просмотр заявок на регистрацию, карточки
пользователей (блокировка, роли, лимиты, удаление), рассылка, лог ошибок ИИ, заявки на
вступление, команды `/make_moderator`, `/myid`.

### Помощники

**`get_moderator_ids() -> list[int]`** (строка 80) — собирает список ID модераторов:
`config.MODERATOR_IDS` (если заполнен) + все `user_id` из БД с ролью `moderator`
(через `db.get_all_users()` + фильтр по `role`).

**`notify_moderators(text: str) -> int`** (строка 112) — уведомляет всех модераторов:
для каждого ID из `get_moderator_ids()` шлёт сообщение через `bot.send_message`, обёрнуто в
try/except (модератор мог быть заблокирован/недоступен). Возвращает число успешных отправок.
Используется при новой заявке на регистрацию и при ошибках ИИ.

**`_notify_approved(user_id, text=None)`** (строка 148) — персональное уведомление
пользователя о подтверждении заявки (на сохранённый `chat_id`).

**`_is_moderator(user_id) -> bool`** (строка 538) — `db.is_moderator(user_id)` + проверка
по списку `get_moderator_ids()`; защитный барьер всех модераторских кнопок.

### Клавиатуры (строки 208–320)

- `get_moderation_keyboard()` — меню модератора:
  «📋 Заявки» (`mod_pending`), «👥 Все пользователи» (`mod_all_users`),
  «⚠️ Ошибки ИИ» (`mod_errors`), «📢 Рассылка» (`mod_broadcast`),
  «🏫 Вступление в школу» (`mod_join_requests`).
- `get_pending_keyboard(user_id)` — навигация по списку заявок:
  кнопка «Перейти к заявке» (payload `mod_skip_<idx>`), «Отменить».
- `_user_card_keyboard(target_id)` — набор действий над пользователем:
  заблокировать/разблокировать (`mod_user_block_<id>`), сменить роль
  (`mod_user_role_<id>` → подменю `mod_user_role_student_/teacher_/moderator_<id>`),
  сменить лимит (`mod_user_limit_<id>`), удалить (`mod_user_delete_<id>` →
  подтверждение `mod_user_delete_yes_<id>`).
- `_cancel_keyboard(back_payload="menu_moderation")` — кнопка «← Отмена».

### `_user_card_text(user, target_id) -> str` (строка 322) и `_render_user_card` (строка 374)

Собирают и отправляют «карточку пользователя»: ID, имя/ФИ, роль, школа/класс, телефон,
email, возраст, лимиты ИИ, счётчики, статус блокировки и кнопки действий
(`_user_card_keyboard`).

### `_handle_user_card_action(update, fsm_context, payload)` (строка 401)

Выполняет действие из карточки:
- `mod_user_block_<id>` / `mod_user_role_...` / `mod_user_limit_...` / `mod_user_delete_...` →
  `db.toggle_blocked / db.set_role / (FSM-ввод лимита) / db.delete_user` и перерисовка карточки.
- Удаление требует подтверждения кнопкой `mod_user_delete_yes_<id>`.
- После действия пользователь получает уведомление (если это возможно).

### `handle_moderation_menu(update, fsm_context)` (строка 597)

Главный обработчик всех модераторских кнопок. Фильтр
`CallbackPayload("menu_moderation", prefixes=("mod_",))`. Ветви:

- `menu_moderation` — показать меню модератора.
- `mod_pending` — список заявок на подтверждение (`db.get_pending_users()` → список `user_id`);
  если пусто — «Заявок нет», иначе первый пользователь через `_show_pending_user(...)`.
- `mod_approve_<id>` — `db.approve_user(user_id)` (`approved=1`, роль из `role_requested`),
  `_notify_approved(...)`, переход к следующей заявке.
- `mod_reject_<id>` — роль `rejected`, переход к следующей заявке.
- `mod_skip_<idx>` — пропустить заявку (показать следующую).
- `mod_all_users` — список пользователей по ролям (ученики/учителя/модераторы);
  по каждому — карточка через `_render_user_card`.
- `mod_user_lookup` — ввод произвольного `user_id` (FSM `ModerationStates.wait_uid`).
- `mod_errors` / `mod_errors_clear` — показать лог ошибок ИИ / очистить его.
- `mod_broadcast` — начало массовой рассылки (FSM `wait_broadcast`).
- `mod_join_requests` — переход к разделу «Вступление в школу» (обрабатывается в settings.py:
  те же заявки по школе; сюда — сводка по заявкам).

### `_show_pending_user(update, pending_list, index)` (строка 1167)

Показывает заявку `pending_list[index]`: карточка кандидата (имя, роль, школа?) с кнопками
«Подтвердить» (`mod_approve_<id>`), «Отклонить» (`mod_reject_<id>`), «Пропустить»
(`mod_skip_<idx+1>`). Индекс аккуратно не выходит за границы списка.

### `handle_mod_errors_clear(update)` (строка 955)

`db.clear_ai_errors()` + сообщение «Лог ошибок очищен».

### `handle_moderation_text(update, fsm_context)` (строка 993)

Обработчик FSM-ввода текста модератора (`StateFilter(ModerationStates)`):
- `wait_uid` — принимает user_id (ведёт к карточке пользователя через `_render_user_card`);
- `wait_limit` — новое значение дневного лимита → `db.set_user_field(uid, "ai_daily_limit", n)`;
- `wait_broadcast` — текст рассылки: `db.get_approved_chat_ids()` → цикл по чатам с
  `bot.send_message` (в try/except), в конце — отчёт «Разослано N из M».

### Команды

- `cmd_make_moderator(update)` (строка 1261) — `/make_moderator`: назначает роль `moderator`
  (вероятно, тому же пользователю или по FSM-вводу ID), с проверкой прав.
- `cmd_myid(update)` (строка 1321) — `/myid`: печатает числовой user_id отправителя.

### Парсеры

- `_parse_id(payload)` (строка 1338) — достаёт `int` из хвоста payload
  (`"mod_user_role_student_456"` → 456); сбой → None.
- `_parse_int(text)` (строка 1350) — строгое `int(text)` из пользовательского ввода (None при ошибке).

---

## handlers/settings.py — настройки пользователя

Файл (1890 строк): раздел «Настройки» — ИИ, интерфейс, персональные данные, школа/класс
(заявки на вступление), лимиты учеников (учитель), очистка истории.

### Клавиатуры (строки 95–292)

- `get_settings_keyboard(user_id)` — меню настроек: «🤖 Настройки ИИ» (`settings_ai`),
  «🖥 Интерфейс» (`settings_ui`), «👤 Персональные данные» (`settings_personal`),
  «🏫 Школа и класс» (`settings_school`), «📊 Моя статистика» (`settings_stats`),
  «🧹 Очистить историю ИИ» (`settings_clear_history`), «ℹ️ О боте» (`settings_about`),
  и для учителей — «📈 Лимиты учеников» (`settings_limits`) и «📋 Заявки на вступление»
  (`settings_join_requests`). Настройки уважают `btn()` (эмодзи).
- `_ai_settings_keyboard` — подменю ИИ: стиль (`ai_style`), подробность (`ai_detail`),
  примеры/наводящие (`ai_examples`/`ai_followup`), темы (`ai_topics`), лимит (`ai_daily_limit`).
- `_ai_style_keyboard` / `_ai_extra_keyboard` — выбор стиля
  (`settings_style_simple/detailed/fun`) и дополнительные переключатели ответа.
- `_ui_settings_keyboard` — переключатели интерфейса:
  `settings_ui_emoji`, `settings_ui_compact`, `settings_ui_stats`.
- `_personal_keyboard` — поля персональных данных: имя, телефон, email, возраст, пароль
  (`settings_edit_name/phone/email/age/password`).
- `_cancel_personal_keyboard` / `_cancel_school_keyboard` — кнопки отмены.
- `_school_keyboard(user_id)` — блок школы: «🏫 Моя школа и класс», «Вступить в школу»
  (`settings_join`), «Покинуть школу» (`settings_leave`), «Мои заявки» (`settings_join_requests`).

### `handle_settings_menu(update, fsm_context)` (строка 373)

Главный обработчик кнопок настроек. Фильтр `CallbackPayload("menu_settings", prefixes=(...))`.
Основные ветви:

- `menu_settings` / `settings_about` — меню настроек / «О боте» (версия, описание).
- `settings_ai` и подкетки — показать подменю ИИ.
- `ai_style` / `settings_style_<style>` — сменить стиль общения ИИ
  (`db.set_user_field(ai_style)`), ответ подсвечивается галочкой.
- `ai_examples` / `ai_followup` / `ai_detail` — переключатели примеров, наводящих вопросов,
  степени подробности (`short/normal/detail`).
- `ai_topics` / `ai_topics_edit` — показать/редактировать интересующие темы
  (ввод текста в FSM `wait_topics`); `ai_topics_clear` — очистить.
- `settings_ui` + переключатели `settings_ui_emoji/compact/stats` — вызывают
  `set_ui_pref(...)` и перерисовывают меню (работает и `btn()` — тексты без эмодзи).
- `settings_personal` — меню персональных данных; `settings_edit_<поле>` → FSM-ввод нового
  значения (состояния `wait_edit_*`).
- `settings_school` / `settings_join` / `settings_leave` / `settings_leave_confirm` —
  школа и класс: двухшаговая заявка на вступление (`wait_join_school → wait_join_class`),
  подтверждение выхода (`wait_leave_confirm`).
- `settings_join_requests` — список заявок (`db.get_pending_join_requests()`), листание
  `settings_join_skip_<idx>`, решения `settings_join_approve_<id>`/`settings_join_reject_<id>`
  → `db.approve_join_request / reject_join_request` (учитель обрабатывает заявки своей школы).
- `settings_limits` (учитель) — страницы `settings_limits_page_<n>` по списку учеников
  (`_limits_students()`), кнопка «Задать лимит» (`settings_limits_set`) → FSM-ввод
  `user_id` и значения (`wait_limit_uid`/`wait_limit_value`).
- `settings_clear_history` / `settings_clear_history_confirm` — очистка личной истории ИИ
  (`db.clear_ai_logs_for_user`).

### `_limits_students()` / `_limits_text(...)` / `_limits_keyboard(...)` (строки 1316–1397)

Подготавливают список учеников (роль `student`), текст «лимит использован N из M» и
клавиатуру с листанием по страницам (по N учеников на страницу).

### `_personal_edit_target(payload)` (строка 1406)

```python
def _personal_edit_target(payload: str) -> tuple[str, object]:
    # "settings_edit_name" -> ("name", "имя"), "settings_edit_phone" -> ("phone", ...)
    mapping = {
        "settings_edit_name": ("name", "имя"),
        "settings_edit_phone": ("phone", "телефон"),
        "settings_edit_email": ("email", "email"),
        "settings_edit_age": ("age", "возраст"),
        "settings_edit_password": ("password", "пароль"),
    }
    return mapping.get(payload, ("", None))
```

Возвращает пару (поле в БД, человеческое название) для ветки редактирования.

### `handle_settings_text(update, fsm_context)` (строка 1438)

Обработчик FSM-ввода настроек (`StateFilter(SettingsStates)`). По состоянию:
- `wait_topics` → `ai_topics` = введённый текст;
- `wait_join_school` → сохраняет школу, переход в `wait_join_class` (если ученик);
- `wait_join_class` → `db.create_join_request(user_id, school, class)`, заявка создана;
- `wait_leave_confirm` → `db.leave_school(user_id)`;
- `wait_limit_uid` → запоминает ID ученика, переход в `wait_limit_value`;
- `wait_limit_value` → `db.set_user_field(id, "ai_daily_limit", n)`;
- `wait_edit_*` → обновление соответствующего поля через `set_user_field`, с валидацией
  (возраст — `_parse_age`, email — «похоже на email»).

### `_split_payload_number(payload)` (строка 1794)

```python
def _split_payload_number(payload: str) -> int | None:
    try:
        return int(payload.rsplit("_", 1)[1])
    except (ValueError, IndexError):
        return None
```

Достаёт число из конца payload (`settings_join_approve_42` → 42).

### `_show_join_request(...)` (строка 1815) и `_handle_join_decision(...)` (строка 1863)

- `_show_join_request` — карточка заявки на вступление с кнопками
  «✅ Принять» / «❌ Отклонить» / «Пропустить».
- `_handle_join_decision(update, payload, accept)` — вызывает
  `db.approve_join_request(request_id, teacher_id)` (меняет статус заявки и обновляет
  `school/class_name` ученика, уведомляет ученика) либо `db.reject_join_request(...)`.