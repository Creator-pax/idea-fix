# Часть 6. database.py — слой SQLite

Файл (1767 строк) — весь доступ к SQLite. Подключение, схема, CRUD пользователей,
роли, лимиты ИИ, заметки, логи ИИ/ошибок, заявки на вступление, статистика для панели.

## Инициализация

**`_SCHEMA_READY` / `_SCHEMA_LOCK`** (строки 262–263) — флаг «схема создана один раз на
процесс» и мьютекс (защита от гонок при параллельном импорте из потоков).

**`_safe_init()`** (строка 274) — разовая подготовка:
1) если файл БД существует, но НЕ является SQLite (старый pickledb или повреждён) —
переименовывает его в `<имя>.pickledb.bak` (не теряем данные);
2) создаёт файл, если его нет;
3) выполняет `_SCHEMA` (`executescript`) — CREATE TABLE / CREATE INDEX;
4) вызывает `_migrate_users_columns()` — добавляет недостающие колонки.
Вызывается только из `_get_conn()` один раз на процесс.

**`_migrate_users_columns()`** (строка 331) — миграция: `CREATE TABLE IF NOT EXISTS` не
меняет существующую таблицу, поэтому новые колонки добавляются отдельным ALTER — для
каждой известной колонки проверяется `PRAGMA table_info(users)` и добавляются отсутствующие.

**`_get_conn()`** (строка 374):

```python
def _get_conn():
    global _SCHEMA_READY
    with _SCHEMA_LOCK:
        if not _SCHEMA_READY:
            _safe_init()
            _SCHEMA_READY = True
    conn = sqlite3.connect(DATABASE_FILE, timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=15000")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn
```

- `timeout=15` — если файл занят другим процессом, ждём до 15 секунд.
- `row_factory = sqlite3.Row` — доступ к колонкам по имени (`row["role"]`).
- `WAL` (Write-Ahead Logging) — читатели не блокируют писателя и наоборот. Критично:
  бот и панель — два процесса, работают с одним файлом.
- `busy_timeout=15000` — при занятости файла ждём (иначе мгновенный SQLITE_BUSY).

**`_connect()`** (строка 419) — контекстный менеджер, **единственный** способ работы с БД:

```python
@contextmanager
def _connect():
    conn = _get_conn()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
```

- Успех → `commit()` (изменения постоянные); ошибка → `rollback()` + `raise`.
- Соединение всегда закрывается. Транзакции стабильны: пример — `approve_join_request`
  обновляет сразу заявку и ученика — атомарно.

**`reload()` / `save()`** (строки 454/465) — «пустышки» для совместимости со старым кодом
(pickledb держал данные в памяти). Теперь каждая операция читает диск, `save()` не нужен
(commit происходит в `_connect`). `mian.py` всё ещё вызывает `db.reload()` перед каждым
апдейтом — это бесплатно и безвредно.

## Вспомогательные константы

```python
_USER_FIELDS = frozenset({ "role", "name", "full_name", "phone", "password",
    "school", "class_name", "email", "age", "chat_id", "registered_at",
    "approved", "role_requested", "ai_daily_limit", "ai_used_today",
    "ai_last_reset", "ai_topics", "ai_style", "blocked", "show_emoji",
    "compact_mode", "show_stats", "ai_followup", "ai_examples", "ai_detail" })

_INT_FIELDS = frozenset({ "chat_id", "registered_at", "approved",
    "ai_daily_limit", "ai_used_today", "ai_last_reset", "blocked",
    "show_emoji", "compact_mode", "show_stats", "ai_followup",
    "ai_examples", "age" })
```

- `_USER_FIELDS` — **белый список** имён колонок `users`: f-string в SQL подставляет только
  оттуда — это защита от SQL-инъекций (имя колонки нельзя передать плейсхолдером `?`).
- `_INT_FIELDS` — числовые колонки, перед UPDATE значение приводится к `int`.

**`_as_int(value)`** (строка 519) — аккуратное приведение к int: `None`/`""` → None,
непереводимое → None, иначе `int(value)`.

## Пользователи

**`create_user(user_id, name, role="pending")`** (строка 552):

```python
now = int(time.time())
with _connect() as conn:
    conn.execute(
        """INSERT OR REPLACE INTO users
               (user_id, name, role, registered_at, approved,
                ai_daily_limit, ai_used_today, ai_last_reset, ai_style)
           VALUES (?, ?, ?, ?, 0, 50, 0, ?, 'simple')""",
        (user_id, name, role, now, now)
    )
```

- `INSERT OR REPLACE` — есть пользователь → запись перезаписывается (перерегистрация),
  нет → создаётся. Дефолты: `approved=0`, лимит ИИ 50/день, счётчики 0, стиль `simple`.
- `ai_last_reset = now` — чтобы счётчик не сбросился мгновенно.

**`get_user(user_id)`** (строка 586) — `SELECT *` одной строки → `dict(row)`; пользователь
нет → `None`. Поле `approved` отдаётся как bool.

**`user_exists(user_id)`** (строка 618) — `SELECT 1 ...` (только факт наличия) → bool.

**`get_user_field(user_id, field)`** (строка 637):

```python
if field not in _USER_FIELDS:
    return None
with _connect() as conn:
    row = conn.execute(f"SELECT {field} FROM users WHERE user_id = ?", (user_id,)).fetchone()
return row[0] if row is not None else None
```

- Читает **одну** колонку. Имя `field` подставляется в f-string только после проверки по
  белому списку. `row[0]` — значение единственной выбранной колонки.

**`set_user_field(user_id, field, value)`** (строка 662):

```python
if field not in _USER_FIELDS:
    return
if field == "approved":
    value = 1 if value else 0
elif field in _INT_FIELDS:
    value = _as_int(value)
with _connect() as conn:
    conn.execute(f"UPDATE users SET {field} = ? WHERE user_id = ?", (value, user_id))
```

- Неизвестное поле молча игнорируется. `approved` → 0/1; числовые поля — через `_as_int`.

**`approve_user(user_id)`** (строка 694):

```python
requested = get_user_field(user_id, "role_requested") or "student"
if requested not in ("student", "teacher", "moderator"):
    requested = "student"
with _connect() as conn:
    conn.execute("UPDATE users SET approved = 1, role = ? WHERE user_id = ?", (requested, user_id))
```

- Итоговая роль берётся из `role_requested` (что выбрал пользователь при регистрации),
  неизвестные значения → `student`. Один UPDATE — атомарно «подтверждён и с ролью».

**`set_role(user_id, role)`** (строка 723) — прямой `UPDATE role` (для панели модератора).

**`delete_user(user_id)`** (строка 742) — полное удаление из 4 таблиц:

```python
total = 0
for table in ("users", "notes", "ai_log", "ai_errors"):
    with _connect() as conn:
        cur = conn.execute(f"DELETE FROM {table} WHERE user_id = ?", (user_id,))
        total += cur.rowcount
return total
```

- Прогон по таблицам, где есть `user_id`; `join_requests` намеренно НЕ трогает (история
  заявок может пригодиться). `cursor.rowcount` — сколько строк реально удалено.

## Проверки ролей и статусов

- `is_moderator(user_id)` (785) — `role == "moderator"`.
- `is_teacher(user_id)` (812) — `role == "teacher"`.
- `is_student(user_id)` (818) — `role == "student"`.
- `is_approved(user_id)` (826) — `approved == 1`.
- `is_blocked(user_id)` (855) — `blocked == 1`.
- `set_blocked(user_id, blocked)` (876) — запись флага.
- `toggle_blocked(user_id)` (893) — инверсия флага (панель модератора).

Все — аналогичные короткие `SELECT`/`UPDATE` через `_connect`.

## Списки пользователей

- `get_all_users()` (923) — все пользователи списком словарей (панель модератора).
- `get_approved_chat_ids()` (949) — `chat_id` всех одобренных (рассылка).
- `get_all_user_ids()` (967) — все user_id.
- `find_user_by_chat_id(chat_id)` (987) — user_id по chat_id (fallback в resolve_user_id).
- **`get_pending_users()`** (1010):

```python
with _connect() as conn:
    rows = conn.execute(
        "SELECT user_id FROM users WHERE role = 'pending' ORDER BY registered_at, user_id"
    ).fetchall()
return [row["user_id"] for row in rows]
```

- Возвращает **список user_id** (не словарей!) со статусом `pending`, отсортированные по
  времени регистрации. Панель модератора строит по ним список заявок.

## Лимиты ИИ

Идея — «скользящее окно» 24 часа (86400 сек), а не привязка к календарным суткам.

**`check_ai_limit(user_id)`** (строка 1046):

```python
row = conn.execute("SELECT ai_used_today, ai_last_reset, ai_daily_limit FROM users WHERE user_id = ?", ...).fetchone()
if row is None:
    return False
now = int(time.time())
last_reset = row["ai_last_reset"] or 0
used = row["ai_used_today"] or 0
limit = row["ai_daily_limit"] or 50
if now - last_reset > 86400:
    ... UPDATE users SET ai_used_today = 0, ai_last_reset = now ...
    used = 0
return used < limit
```

- Три колонки одним SELECT. Прошло > 24 часов → сбрасывает счётчик прямо здесь.
- Возвращает `used < limit` (можно ли спросить ИИ).

**`increment_ai_usage(user_id)`** (строка 1091) — `ai_used_today = ai_used_today + 1`
прямо в SQL — атомарно, без гонок между потоками.

**`get_ai_usage(user_id)`** (строка 1103) — значение `ai_used_today` для UI (0, если нет).

## Заметки

- **`save_note(user_id, title, content)`** (1137) — INSERT в `notes` с `time=now`.
- **`get_notes(user_id)`** (1155) — все заметки пользователя, отсортированы по `time` (или
  `id`); каждой строке добавляется **`index`** — порядковый номер в списке (он же — хвост
  payload кнопок `note_view_<index>`). Возвращает список словарей
  `{index, title, content, time}`.
- **`delete_note(user_id, note_index)`** (1192) — берёт список через `get_notes`, по
  `note_index` находит реальный `id` и удаляет строку. Индекс, а не ID — чтобы совпадать
  с кнопками бота.

## Логи ИИ и ошибок

- **`save_ai_log(user_id, question, answer)`** (1240) — INSERT в `ai_log` (+ имя пользователя
  на момент запроса, время).
- **`get_ai_logs(limit=20)`** (1262) — последние логи по всему боту (для панели).
- **`get_ai_logs_for_user(user_id, limit=20)`** (1289) — логи конкретного ученика.
- **`clear_ai_logs_for_user(user_id)`** (1312) — очистка истории (возвращает число удалённых).
- **`save_ai_error(user_id, mode, error_text)`** (1344) — INSERT в `ai_errors` (режим сбоя,
  текст ошибки, время).
- **`get_ai_errors(limit=50)`** (1364) — последние ошибки (для панели).
- **`clear_ai_errors()`** (1385) — очистить весь лог ошибок.

## Заявки на вступление в школу/класс

- **`create_join_request(user_id, target_school, target_class)`** (1416) — INSERT заявки
  со статусом `pending`; старые `pending`-заявки того же ученика переводятся в `rejected`
  (нельзя копить параллельные). Возвращает id новой заявки.
- **`get_pending_join_requests()`** (1445) — все ожидающие заявки.
- **`get_pending_join_requests_for_school(school)`** (1469) — ожидающие по конкретной школе
  (учитель видит заявки именно своей школы).
- **`approve_join_request(request_id, teacher_id)`** (1498) — в одной транзакции: заявке
  → `approved`, `teacher_id=teacher_id`; ученику (владельцу заявки) → обновляются
  `school`/`class_name`. Возвращает bool успеха.
- **`reject_join_request(request_id, teacher_id)`** (1539) — заявке → `rejected`.
- **`get_user_join_requests(user_id)`** (1571) — история заявок пользователя (10 шт.).
- **`get_join_request_by_id(request_id)`** (1591) — одна заявка.
- **`leave_school(user_id)`** (1618) — очистка `school`/`class_name`.
- **`get_teachers_by_school(school)`** (1639) — user_id учителей этой школы (для уведомления
  о новой заявке).

## Дополнительные выборки для панели модератора

- **`get_chat_ids_by_role(role)`** (1677) — `chat_id` пользователей с данной ролью.
- **`reset_ai_usage(user_id)`** (1713) — обнуление счётчика ИИ (панель).
- **`get_total_notes_count()`** (1728) — общее число заметок (статистика).
- **`count_ai_requests_total()`** (1737) — общее число запросов ИИ (статистика).
- **`get_all_join_requests(limit=100)`** (1746) — вся история заявок (вкладка панели).