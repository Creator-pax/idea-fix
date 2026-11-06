# ============================================================
# БАЗА ДАННЫХ — SQLITE (все данные хранятся в bot_data.db)
# ============================================================
#
# Раньше здесь была библиотека pickledb (словарь, сохраняемый в файл).
# pickledb хранил ВСЁ одним JSON-блоком внутри процесса, из-за чего:
#   1. Бот и панель модератора (два разных процесса) могли затереть
#      изменения друг друга;
#   2. При каждом обращении внутри async-цикла методы возвращали
#      корутины, и приходилось гонять их в отдельных потоках.
#
# Теперь БД — это SQLite (встроен в Python, ничего ставить не нужно):
#   • каждая операция открывает своё соединение и читает данные с диска,
#     поэтому бот и панель модератора всегда видят свежие изменения друг
#     друга (никакого reload() вручную не нужно — функция оставлена для
#     совместимости со старым кодом);
#   • SQLite сам решает вопросы блокировок между процессами
#     (режим WAL + busy_timeout);
#   • без pickledb отпала и вся обёртка с потоками/корутинами.
#
# Схема (5 таблиц):
#   users          — все пользователи (одна строка на пользователя):
#                    анкета, роль, школа/класс, лимиты ИИ, настройки UI
#                    и блокировка — всё в одной широкой таблице;
#   notes          — заметки пользователей (заголовок + содержимое);
#   ai_log         — история запросов к ИИ (для учителей/модераторов);
#   ai_errors      — лог ошибок ИИ (для панели модератора);
#   join_requests  — заявки учеников на вступление в школу/класс.
#
# Все функции ниже делятся на блоки:
#   1. Инициализация БД (_safe_init, миграции, _get_conn, _connect);
#   2. «Пустышки» reload()/save() — совместимость со старым кодом;
#   3. CRUD пользователей (создание, чтение, изменение, удаление);
#   4. Проверки ролей и статусов (модератор/учитель/ученик/блокировка);
#   5. Списки пользователей для панели модератора и рассылок;
#   6. Лимиты ИИ (дневная норма запросов);
#   7. Заметки;
#   8. Логи ИИ и ошибок ИИ;
#   9. Заявки на вступление в школу/класс;
#  10. Дополнительные выборки для десктоп-панели (рассылки/статистика).
#
# ЕДИНЫЙ ПАТТЕРН ВСЕХ ФУНКЦИЙ:
#   — любое обращение идёт через `with _connect() as conn:`;
#   — _connect() сам открывает соединение, фиксирует транзакцию (commit)
#     или откатывает её (rollback) и всегда закрывает соединение;
#   — плейсхолдеры "?" защищают от SQL-инъекций (значения никогда не
#     склеиваются со строкой запроса вручную);
#   — имена колонок в f-string запросах берутся ТОЛЬКО из белых списков.
# ============================================================
#
# ============================================================
# КРАТКИЙ СПРАВОЧНИК ФУНКЦИЙ (таблица → возвращаемое значение)
# ============================================================
# Инициализация:
#   _safe_init()                              — подготовка файла и схемы; None
#   _migrate_users_columns()                  — миграция колонок users; None
#   _get_conn()                               — новое соединение; sqlite3.Connection
#   _connect()                                — контекстный менеджер; yield conn
# Совместимость:
#   reload() / save()                         — «пустышки»; None
# Пользователи (таблица users):
#   create_user(user_id, name, role)          — вставка/перезапись строки; None
#   get_user(user_id)                         — вся строка; dict | None
#   user_exists(user_id)                      — есть ли строка; bool
#   get_user_field(user_id, field)            — одна колонка; int | str | None
#   set_user_field(user_id, field, value)     — UPDATE одной колонки; None
#   approve_user(user_id)                     — approved=1 и роль; None
#   set_role(user_id, role)                   — назначить роль; None
#   delete_user(user_id)                      — удаление из 4 таблиц; int
# Проверки ролей / статусов:
#   is_moderator / is_teacher / is_student    — сравнение роли; bool
#   is_approved(user_id)                      — подтверждён ли; bool
#   is_blocked / set_blocked / toggle_blocked — флаг blocked; bool
# Списки пользователей:
#   get_all_users()                           — все пользователи; list[dict]
#   get_approved_chat_ids()                   — чаты для рассылки; list[int]
#   get_all_user_ids()                        — все user_id; list[int]
#   find_user_by_chat_id(chat_id)             — user_id по чату; int | None
#   get_pending_users()                       — ждущие подтверждения; list[int]
# Лимиты ИИ (поля в users):
#   check_ai_limit(user_id)                   — можно ли запрос; bool
#   increment_ai_usage(user_id)               — +1 к счётчику; None
#   get_ai_usage(user_id)                     — сколько использовано; int
# Заметки (таблица notes):
#   save_note(user_id, title, content)        — INSERT; None
#   get_notes(user_id)                        — заметки с индексами; list[dict]
#   delete_note(user_id, note_index)          — удаление по индексу; bool
# Логи ИИ (таблица ai_log):
#   save_ai_log(user_id, question, answer)    — INSERT; None
#   get_ai_logs(limit)                        — последние логи; list[dict]
#   get_ai_logs_for_user(user_id, limit)      — логи ученика; list[dict]
#   clear_ai_logs_for_user(user_id)           — очистить историю; int
# Ошибки ИИ (таблица ai_errors):
#   save_ai_error(user_id, mode, error_text)  — INSERT; None
#   get_ai_errors(limit)                      — последние ошибки; list[dict]
#   clear_ai_errors()                         — очистить весь лог; None
# Заявки (таблица join_requests):
#   create_join_request(user_id, school, cls) — INSERT + pending; int (id)
#   get_pending_join_requests()               — все ожидающие; list[dict]
#   get_pending_join_requests_for_school(s)   — ожидающие по школе; list[dict]
#   approve_join_request(id, teacher_id)      — принять + обновить ученика; bool
#   reject_join_request(id, teacher_id)       — отклонить; bool
#   get_user_join_requests(user_id)           — история (10 шт.); list[dict]
#   get_join_request_by_id(id)                — одна заявка; dict | None
#   leave_school(user_id)                     — очистить школу/класс; None
#   get_teachers_by_school(school)            — учителя для уведомления; list[int]
# Панель модератора:
#   get_chat_ids_by_role(role)                — чаты по роли; list[int]
#   reset_ai_usage(user_id)                   — обнулить счётчик ИИ; None
#   get_total_notes_count()                   — всего заметок; int
#   count_ai_requests_total()                 — всего запросов ИИ; int
#   get_all_join_requests(limit)              — вся история заявок; list[dict]
# ============================================================

# --- Стандартная библиотека ---
import os                     # проверка существования файла БД, переименование
import sqlite3                # драйвер SQLite (встроен в Python, внешних зависимостей нет)
import threading              # мьютекс для однократной инициализации схемы
import time                   # Unix-время для меток registered_at / time
from contextlib import contextmanager  # декоратор превращает функцию в контекст-менеджер _connect

# Путь к файлу БД берём из config.py — единый источник истины: и бот,
# и панель модератора используют один и тот же путь к bot_data.db.
from config import DATABASE_FILE        # путь к файлу базы (из config.py)


# ============================================================
# СХЕМА БАЗЫ ДАННЫХ
# ============================================================
# Создаём таблицы через CREATE TABLE IF NOT EXISTS, поэтому функция
# безопасно вызывается при каждом запуске (и ботом, и панелью модератора).
# Индексы ускоряют выборки «заметки/логи по user_id».
#
# Про первичные ключи (PK):
#   • users.user_id — INTEGER PRIMARY KEY: user_id приходит из MAX, это
#     внешний стабильный идентификатор, поэтому он и есть ключ;
#   • у остальных таблиц id = INTEGER PRIMARY KEY AUTOINCREMENT: id выдаёт
#     сама БД, строго возрастая, поэтому сортировка ORDER BY id совпадает
#     с хронологическим порядком создания строк.
#
# Типы данных (SQLite «мягкий» к типам, но мы держим дисциплину):
#   • INTEGER — числа: user_id, chat_id, счётчики, флаги 0/1, время (сек);
#   • TEXT — строки, по умолчанию '' (чтобы код не ловил None);
#   • NOT NULL DEFAULT ... — любая новая строка гарантированно имеет
#     осмысленное значение, даже если INSERT не указал колонку.
#
# Соглашения о значениях:
#   • флаги (approved, blocked, show_emoji и т.п.) — 0 или 1, НЕ булево,
#     т.к. SQLite не имеет типа BOOLEAN;
#   • время (registered_at, time) — Unix-секунды (целое число), сравнивать
#     и сортировать их тривиально.
#
# Про индексы (CREATE INDEX ... в конце _SCHEMA):
#   idx_notes_user, idx_ai_log_user, idx_join_requests_user — ускорители
#   запросов «WHERE user_id = ?». Без индекса SQLite сканировал бы таблицу
#   построчно (O(n) на каждый запрос); с индексом поиск идёт по B-дереву
#   (O(log n)) — разница заметна, когда таблицы разрастаются.
#
# ВАЖНО для тех, кто будет править схему: CREATE TABLE IF NOT EXISTS НЕ
# менят уже существующую таблицу. Новые колонки добавляются отдельно —
# функцией _migrate_users_columns() ниже.

# Большая строка SQL выполняется одной командой executescript() при
# инициализации. Каждая таблица — один логический блок схемы.
# Внимание: внутри _SCHEMA комментарии — только те, что были в исходном
# SQL (символы "--"); смысловые пояснения вынесены в #-комментарии выше.
# ВАЖНО: внутри этой строки нельзя ставить # — это испортит SQL,
# Python-комментарии разрешены только снаружи строки.
_SCHEMA = """
-- Таблица пользователей: ЯДРО БД. Одна строка = один человек в системе.
-- user_id — первичный ключ, он же числовой ID из MAX (уникален всегда).
CREATE TABLE IF NOT EXISTS users (
    user_id        INTEGER PRIMARY KEY,            -- Числовой ID пользователя (из MAX)
    name           TEXT NOT NULL DEFAULT '',       -- Имя из профиля MAX
    full_name      TEXT NOT NULL DEFAULT '',       -- Имя и фамилия, введённые при регистрации
    role           TEXT NOT NULL DEFAULT 'pending',-- Роль: pending/student/teacher/moderator/rejected
    phone          TEXT NOT NULL DEFAULT '',       -- Телефон (заполняется при регистрации)
    password       TEXT NOT NULL DEFAULT '',       -- Пароль (заполняется при регистрации)
    school         TEXT NOT NULL DEFAULT '',       -- Школа
    class_name     TEXT NOT NULL DEFAULT '',       -- Класс (для учеников)
    email          TEXT NOT NULL DEFAULT '',       -- Электронная почта (опционально)
    age            INTEGER NOT NULL DEFAULT 0,     -- Возраст (опционально)
    chat_id        INTEGER,                        -- ID чата MAX (по нему шлём уведомления)
    registered_at  INTEGER,                        -- Unix-время регистрации
    approved       INTEGER NOT NULL DEFAULT 0,     -- 1 = подтверждён модератором, 0 = нет
    role_requested TEXT NOT NULL DEFAULT '',       -- Роль, которую выбрал пользователь при регистрации
    ai_daily_limit INTEGER NOT NULL DEFAULT 50,    -- Лимит запросов ИИ в день
    ai_used_today  INTEGER NOT NULL DEFAULT 0,     -- Сколько запросов ИИ использовано сегодня
    ai_last_reset  INTEGER NOT NULL DEFAULT 0,     -- Когда в последний раз сбрасывался счётчик
    ai_topics      TEXT NOT NULL DEFAULT '',       -- Интересные темы для ИИ
    ai_style       TEXT NOT NULL DEFAULT 'simple', -- Стиль общения ИИ: simple/detailed/fun
    blocked        INTEGER NOT NULL DEFAULT 0,     -- 1 = заблокирован модератором, 0 = нет
    show_emoji     INTEGER NOT NULL DEFAULT 1,     -- Показывать ли эмодзи в интерфейсе
    compact_mode   INTEGER NOT NULL DEFAULT 0,     -- Компактное меню
    show_stats     INTEGER NOT NULL DEFAULT 1,     -- Показывать счётчики в главном меню
    ai_followup    INTEGER NOT NULL DEFAULT 0,     -- Задавать ли наводящие вопросы в конце ответа
    ai_examples    INTEGER NOT NULL DEFAULT 1,     -- Показывать ли примеры/аналогии в ответе
    ai_detail      TEXT NOT NULL DEFAULT 'normal'  -- Степень подробности: short/normal/detail
);

-- Заметки пользователей. id — автоинкремент (AUTOINCREMENT даёт строго
-- возрастающие id, чтобы сортировка «по id» совпадала с порядком создания).
CREATE TABLE IF NOT EXISTS notes (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,     -- Внутренний ID заметки
    user_id INTEGER NOT NULL,                      -- Чей это заметка
    title   TEXT NOT NULL DEFAULT '',              -- Заголовок
    content TEXT NOT NULL DEFAULT '',              -- Содержимое
    time    INTEGER NOT NULL DEFAULT 0             -- Unix-время создания
);
-- Индекс по user_id: выборки «все заметки пользователя» становятся быстрыми.
CREATE INDEX IF NOT EXISTS idx_notes_user ON notes(user_id);

-- Журнал запросов к ИИ. Хранит ВСЁ: кто, что и когда спрашивал.
-- Нужен учителям и модераторам для контроля и разбора ответов.
CREATE TABLE IF NOT EXISTS ai_log (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,   -- Внутренний ID записи
    user_id   INTEGER NOT NULL,                    -- Кто спрашивал
    user_name TEXT NOT NULL DEFAULT '',            -- Имя пользователя (на момент запроса)
    question  TEXT NOT NULL DEFAULT '',            -- Вопрос
    answer    TEXT NOT NULL DEFAULT '',            -- Ответ ИИ
    time      INTEGER NOT NULL DEFAULT 0           -- Unix-время запроса
);
CREATE INDEX IF NOT EXISTS idx_ai_log_user ON ai_log(user_id);

-- Лог СБОЕВ ИИ (отдельно от обычных запросов) — для панели модератора.
-- Здесь видно, где и у кого ИИ отдал ошибку.
CREATE TABLE IF NOT EXISTS ai_errors (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,   -- Внутренний ID записи
    user_id   INTEGER NOT NULL,                    -- У кого возникла ошибка
    user_name TEXT NOT NULL DEFAULT '',            -- Имя пользователя
    mode      TEXT NOT NULL DEFAULT '',            -- Режим: question/schedule/test/homework
    error     TEXT NOT NULL DEFAULT '',            -- Текст ошибки
    time      INTEGER NOT NULL DEFAULT 0           -- Unix-время ошибки
);
CREATE INDEX IF NOT EXISTS idx_ai_errors_id ON ai_errors(id);

-- Заявки учеников на вступление в школу/класс (учитель подтверждает)
CREATE TABLE IF NOT EXISTS join_requests (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,  -- Внутренний ID заявки
    user_id       INTEGER NOT NULL,                   -- Кто подал заявку
    target_school TEXT NOT NULL DEFAULT '',            -- Школа, в которую хотят вступить
    target_class  TEXT NOT NULL DEFAULT '',            -- Класс, в который хотят вступить
    status        TEXT NOT NULL DEFAULT 'pending',     -- pending / approved / rejected
    teacher_id    INTEGER,                            -- Кто обработал (user_id учителя)
    time          INTEGER NOT NULL DEFAULT 0           -- Unix-время подачи заявки
);
-- Индекс по user_id: история заявок конкретного ученика.
CREATE INDEX IF NOT EXISTS idx_join_requests_user ON join_requests(user_id);
-- Индекс по status: выборка «все pending-заявки» (главный экран панели).
CREATE INDEX IF NOT EXISTS idx_join_requests_status ON join_requests(status);
"""


# ============================================================
# ИНИЦИАЛИЗАЦИЯ БАЗЫ
# ============================================================
# Переменная-флаг нужна, чтобы схема создавалась только один раз на процесс.
# Защищаем мьютексом на случай, если database.py импортируют из разных потоков.
# Дальше любую функцию можно дёргать сколько угодно раз — за каждый вызов
# создаётся только «лёгкое» соединение, без повторного CREATE TABLE.

_SCHEMA_READY = False        # были ли уже созданы таблицы в этом процессе?
_SCHEMA_LOCK = threading.Lock()  # защита от гонок при параллельном импорте


# ------------------------------------------------------------------
# def _safe_init() — разовая «подготовка» базы:
#   1) если файл есть, но это не SQLite (старый pickledb) — переименовать
#      в *.pickledb.bak, чтобы не потерять данные;
#   2) создать файл (если нет) и выполнить _SCHEMA;
#   3) добавить недостающие колонки (миграция).
# Вызывается только из _get_conn() один раз на процесс.
# ------------------------------------------------------------------
def _safe_init():
    """
    Готовит файл БД к работе и создаёт схему.

    Если файл DATABASE_FILE существует, но НЕ является базой SQLite
    (например, это старый файл из pickledb или повреждённый файл),
    переносим его в <имя>.pickledb.bak и создаём новую чистую базу.
    """
    # Шаг 1. Если файл уже есть — проверяем, настоящая ли это SQLite-база.
    if os.path.exists(DATABASE_FILE):
        # Пробуем открыть файл как SQLite: даже пустая база отвечает
        # на запрос к sqlite_master. Если файл не является БД — будет
        # исключение sqlite3.DatabaseError, значит это старый формат.
        probe = sqlite3.connect(DATABASE_FILE)      # временное пробное соединение
        try:
            # sqlite_master — служебная таблица SQLite; если файл валидный,
            # запрос отработает (даже если таблиц приложений ещё нет).
            probe.execute("SELECT name FROM sqlite_master LIMIT 1").fetchone()
            probe.close()                           # файл — настоящая SQLite-база
        except sqlite3.DatabaseError:
            probe.close()
            # Файл не является SQLite — это, скорее всего, старый pickledb.
            # Оставляем его резервной копией с суффиксом .pickledb.bak,
            # чтобы данные не пропали безвозвратно.
            backup = DATABASE_FILE + ".pickledb.bak"
            try:
                if os.path.exists(backup):
                    os.remove(backup)        # старый бэкап перезаписываем
                # os.rename — атомарная операция в рамках файловой системы.
                os.rename(DATABASE_FILE, backup)
                print(f"DB: старый файл {DATABASE_FILE} перенесён в {backup}, создана новая SQLite-база.")
            except OSError as e:
                # Если переименовать не удалось (напр., занят другим процессом) —
                # не падаем, а просто логируем и оставляем файл как есть.
                print(f"DB: не удалось перенести старый файл: {e}")

    # Шаг 2. Создаём (если нет) файл и все таблицы. Операция идемпотентна.
    conn = sqlite3.connect(DATABASE_FILE)
    try:
        conn.executescript(_SCHEMA)   # выполняем весь _SCHEMA разом
        conn.commit()                 # фиксируем создание таблиц
    finally:
        conn.close()                  # соединение закрываем всегда

    # Миграция старых баз: добавляем отсутствующие колонки таблицы users
    # (схема через CREATE TABLE IF NOT EXISTS не добавляет новые колонки
    # в уже существующую таблицу, поэтому делаем это вручную).
    _migrate_users_columns()


# ------------------------------------------------------------------
# def _migrate_users_columns() — «мягкая» миграция схемы.
# Для каждой колонки из _ADDITIONAL_COLUMNS проверяет, есть ли она уже
# в таблице users (PRAGMA table_info), и если нет — добавляет через
# ALTER TABLE. Безопасно вызывать при каждом запуске: повторные вызовы
# ничего не меняют (колонка уже есть → пропускаем).
# ------------------------------------------------------------------
def _migrate_users_columns():
    """
    Добавляет недостающие колонки в таблицу users (для баз, созданных
    более ранними версиями бота). Безопасно вызывать при каждом запуске.
    """
    # Словарь «название колонки -> её SQL-определение». Новые колонки,
    # которых не было в первой версии бота, добавляются ALTER TABLE.
    # Порядок значений: сначала фичи ИИ/интерфейса, потом анкетные поля.
    _ADDITIONAL_COLUMNS = {
        "blocked": "INTEGER NOT NULL DEFAULT 0",          # блокировка пользователя
        "show_emoji": "INTEGER NOT NULL DEFAULT 1",       # эмодзи в интерфейсе
        "compact_mode": "INTEGER NOT NULL DEFAULT 0",     # компактное меню
        "show_stats": "INTEGER NOT NULL DEFAULT 1",       # счётчики в меню
        "ai_followup": "INTEGER NOT NULL DEFAULT 0",      # наводящие вопросы ИИ
        "ai_examples": "INTEGER NOT NULL DEFAULT 1",      # примеры в ответе ИИ
        "ai_detail": "TEXT NOT NULL DEFAULT 'normal'",    # детальность ответа ИИ
        "email": "TEXT NOT NULL DEFAULT ''",              # электронная почта
        "age": "INTEGER NOT NULL DEFAULT 0",              # возраст
    }
    conn = sqlite3.connect(DATABASE_FILE)
    try:
        # PRAGMA table_info — служебный запрос, возвращающий по одной строке
        # на каждую колонку таблицы; имя колонки лежит во второй позиции [1].
        # Собираем множество уже существующих имён — «что есть в базе сейчас».
        cols = {row[1] for row in conn.execute("PRAGMA table_info(users)")}
        for name, ddl in _ADDITIONAL_COLUMNS.items():
            if name not in cols:
                # Колонки ещё нет — добавляем. ALTER TABLE ADD COLUMN быстр
                # и безопасен для уже заполненных таблиц (все значения станут
                # дефолтными из DDL).
                conn.execute(f"ALTER TABLE users ADD COLUMN {name} {ddl}")
        conn.commit()     # все ALTER фиксируем одной транзакцией
    finally:
        conn.close()      # закрываем соединение даже при ошибке


# ------------------------------------------------------------------
# def _get_conn() — единственное место, где создаётся соединение.
#   • схемы: гарантированно инициализирована (флаг + мьютекс);
#   • row_factory = sqlite3.Row — доступ к колонкам по ИМЕНИ;
#   • PRAGMA WAL / busy_timeout — корректная работа двух процессов;
#   • timeout=15 — сколько ждать, пока файл освободится.
# ------------------------------------------------------------------
def _get_conn():
    """
    Открывает соединение с базой.

    Каждая операция работает через СВОЁ соединение (SQLite-соединения
    нельзя разделять между потоками, а модель «одна операция = одно
    соединение» даёт ровно то поведение, которое было в pickledb:
    каждый вызов видит данные с диска, включая изменения другого процесса).
    """
    global _SCHEMA_READY
    # Однократная инициализация схемы — под мьютексом, чтобы два потока
    # не начали создавать таблицы одновременно.
    with _SCHEMA_LOCK:
        if not _SCHEMA_READY:
            _safe_init()          # создаём файл/таблицы один раз — на всём процессе
            _SCHEMA_READY = True  # помечаем, что схема готова (дальше только connect)

    # timeout=15 — если файл занят другим процессом, ждём до 15 секунд.
    conn = sqlite3.connect(DATABASE_FILE, timeout=15)
    # row_factory=Row — доступ к колонкам по имени: row["role"] вместо row[0].
    conn.row_factory = sqlite3.Row   # доступ к колонкам по имени: row["role"]
    # WAL — режим журнала (Write-Ahead Logging), при котором читатели не
    # блокируют писателя и наоборот. Критично здесь, т.к. бот и панель
    # модератора — два процесса и оба могут писать в один файл БД.
    conn.execute("PRAGMA journal_mode=WAL")
    # Если файл временно занят другим процессом — ждём до 15 секунд
    # (без этого получили бы мгновенный SQLITE_BUSY).
    conn.execute("PRAGMA busy_timeout=15000")
    # Внешние ключи (пока схема их не использует, но включает на всякий случай).
    conn.execute("PRAGMA foreign_keys=ON")
    return conn   # готовое соединение отдаём вызывающему коду


# ------------------------------------------------------------------
# def _connect() — контекстный менеджер (декоратор @contextmanager).
# ЕДИНСТВЕННЫЙ способ работы с БД во всём модуле:
#   with _connect() as conn:  →  conn.execute(...)
# Гарантии:
#   * успех   → conn.commit()   (все изменения внутри with становятся ПОСТОЯННЫМИ);
#   * ошибка  → conn.rollback() (все изменения внутри with ОТМЕНЯЮТСЯ) + raise;
#   * всегда  → conn.close()    (не течёт соединениями).
# Благодаря этому транзакции вроде approve_join_request (обновить заявку +
# обновить ученика) применяются атомарно: либо всё, либо ничего.
# ------------------------------------------------------------------
@contextmanager
def _connect():
    """
    Контекстный менеджер для работы с базой.

    Пример использования:
        with _connect() as conn:
            conn.execute("INSERT ...", (...))
    При успешном завершении блока изменения фиксируются, при ошибке —
    откатываются, соединение всегда закрывается.

    Это единая точка входа для ВСЕХ операций записи/чтения в этом файле —
    так гарантируется «свежее» соединение и корректный commit/rollback.
    """
    conn = _get_conn()          # открываем свежее соединение
    try:
        yield conn              # отдаём соединение в with-блок (здесь работает весь код пользователя)
        conn.commit()           # успех → фиксируем транзакцию
    except Exception:
        conn.rollback()         # ошибка → откатываем всё, что изменили
        raise                   # и пробрасываем исключение наверх
    finally:
        conn.close()            # соединение закрываем в любом случае


# ============================================================
# СОВМЕСТИМОСТЬ СО СТАРЫМ КОДОМ
# ============================================================
# Раньше приходилось явно вызывать reload()/save() (pickledb держал
# данные в памяти). В SQLite каждая операция уже читает/пишет диск,
# поэтому эти функции — «пустышки», оставленные, чтобы старые вызовы
# (например, mian.py, moderator_app.py) не ломались.
# Их можно смело вызывать сколько угодно раз — они ничего не делают.
# mian.py вызывает db.reload() в начале каждого апдейта — это теперь
# бесплатно и безвредно.

def reload():
    """
    (Совместимость) Перечитывание данных с диска.

    В SQLite не требуется: каждая операция открывает свежее соединение
    и видит данные с диска. Оставлено, чтобы старый код не падал.
    """
    # Ничего делать не нужно — данные всегда читаются прямо с диска.
    pass


def save():
    """
    (Совместимость) Сохранение изменений на диск.

    В SQLite не требуется: каждое изменение фиксируется при закрытии
    соединения (commit). Оставлено для совместимости со старым кодом.
    """
    pass    # фиксация уже происходит в _connect() на commit


# ============================================================
# ВСПОМОГАТЕЛЬНЫЕ КОНСТАНТЫ
# ============================================================
#
# Белый список колонок таблицы users. Он используется в set_user_field /
# get_user_field вместо произвольных строк, чтобы:
#   • нельзя было случайно записать данные в несуществующую колонку;
#   • исключить SQL-инъекции (имя колонки выбирается только отсюда).
# В SQL имя колонки нельзя передать плейсхолдером "?", поэтому такие поля
# подставляются через f-string — и единственная защита от подстановки
# мусора/вредного кода — эта проверка по белому списку.
#
# Любая функция, которая строит SQL через f-string, обязана проверять
# имя колонки по этому списку — тогда в запрос никогда не попадёт
# произвольная строка от пользователя.

# Белый список допустимых имён колонок users (для f-string запросов).
# frozenset — неизменяемый и хэшируемый; проверка field in _USER_FIELDS
# работает за O(1) независимо от размера списка.
_USER_FIELDS = frozenset({
    # анкетные данные
    "role", "name", "full_name", "phone", "password", "school",
    "class_name", "email", "age",
    # системные поля
    "chat_id", "registered_at", "approved",
    "role_requested", "ai_daily_limit", "ai_used_today",
    "ai_last_reset", "ai_topics", "ai_style",
    # флаги интерфейса и блокировки
    "blocked", "show_emoji", "compact_mode", "show_stats",
    "ai_followup", "ai_examples", "ai_detail",
})

# Поля, которые должны храниться как целые числа (или NULL).
# Используется в set_user_field для приведения значения перед UPDATE.
# Сюда входят ВСЕ числовые колонки users; строковые поля (name, role и
# т.п.) в этот список не входят — их сохраняем как есть, без приведения.
_INT_FIELDS = frozenset({
    "chat_id", "registered_at", "approved",
    "ai_daily_limit", "ai_used_today", "ai_last_reset",
    "blocked", "show_emoji", "compact_mode", "show_stats",
    "ai_followup", "ai_examples", "age",
})


def _as_int(value):
    """Аккуратно приводит значение к int (при ошибке — None)."""
    if value is None or value == "":
        return None           # пустое значение → NULL
    try:
        return int(value)     # поле успешно заводится в целое число
    except (TypeError, ValueError):
        return None           # непереводимое значение → NULL


# ============================================================
# РАБОТА С ПОЛЬЗОВАТЕЛЯМИ
# ============================================================
#
# Основной блок: создание/получение пользователей, чтение и запись полей,
# подтверждение модератором, роли, удаление, проверки статусов.
#
# Таблица: users (см. схему выше).
# Часто используемые поля функций этого блока:
#   role — роль (pending/student/teacher/moderator/rejected);
#   approved — флаг подтверждения модератором (0/1);
#   chat_id — ID чата MAX, по которому шлём уведомления;
#   registered_at — Unix-время первой регистрации.
#
# ВАЖНО про user_id: это НЕ chat_id и НЕ автоинкремент базы — это тот
# числовой ID, который присылает MAX. Он стабилен и используется как
# первичный ключ во всём модуле.
# ============================================================

# --- create_user(user_id, name, role) -- создание (или «перерегистрация»).
# INSERT OR REPLACE: строка с таким же user_id заменяется целиком. Здесь же
# задаются ДЕФОЛТНЫЕ значения: approved=0 (ждёт модератора), лимит ИИ=50,
# счётчики=0, стиль='simple'. Создание записывает момент регистрации.
def create_user(user_id, name, role="pending"):
    """
    Создаёт нового пользователя в базе.

    INSERT OR REPLACE означает: если пользователь с таким ID уже есть —
    запись обновляется («перерегистрация»), если нет — создаётся.

    Args:
        user_id: Числовой ID пользователя в MAX
        name: Имя пользователя (из профиля MAX)
        role: Роль. Возможные значения:
              - "pending"     — ожидает подтверждения модератором
              - "student"     — ученик
              - "teacher"     — учитель
              - "moderator"   — модератор
    """
    now = int(time.time())   # единый timestamp «момент регистрации»
    with _connect() as conn:
        # INSERT OR REPLACE: при повторной регистрации строка с тем же
        # user_id будет перезаписана (сброс approved, лимита и т.д.).
        conn.execute(
            """INSERT OR REPLACE INTO users
                   (user_id, name, role, registered_at, approved,
                    ai_daily_limit, ai_used_today, ai_last_reset, ai_style)
               VALUES (?, ?, ?, ?, 0, 50, 0, ?, 'simple')""",
            (user_id, name, role, now, now)
        )   # approved=0 (ещё не подтверждён), лимит ИИ 50/день, стиль simple
            # ai_last_reset тоже = now, чтобы счётчик не сбросился мгновенно.


# --- get_user(user_id) -- чтение ВСЕХ полей одной строки.
# Возвращает: dict всех колонок (approved уже как bool) или None.
# SELECT * — здесь оправдан: вызывающему нужен полный профиль (меню,
# проверка роли, вывод анкеты и т.д.).
def get_user(user_id):
    """
    Получает все данные пользователя из базы.

    Args:
        user_id: Числовой ID пользователя

    Returns:
        Словарь с полями пользователя (см. схему users) или None, если не найден.
    """
    with _connect() as conn:
        # SELECT * — все колонки таблицы одной строкой и одним запросом
        # (для словаря пользователя нужны все поля сразу).
        row = conn.execute(
            "SELECT * FROM users WHERE user_id = ?", (user_id,)
        ).fetchone()      # fetchone — одна строка или None

    if row is None:
        return None           # пользователя нет в базе

    # sqlite3.Row → обычный dict, чтобы код-потребитель работал как раньше
    # (доступ по строковым ключам и поддержка .get()).
    user = dict(row)
    # В базе approved хранится как 0/1; наружу отдаём булево значение,
    # чтобы вызывающий код не зависел от числового представления.
    user["approved"] = bool(user.get("approved"))
    return user   # {"user_id": ..., "name": ..., "role": ..., ...}


# --- user_exists(user_id) -- проверка существования.
# SELECT 1 (а не SELECT *): нам нужен только ФАКТ наличия строки.
# Возвращает: bool.
def user_exists(user_id):
    """
    Проверяет, зарегистрирован ли пользователь.

    Returns:
        True если есть строка в таблице users, False если нет.
    """
    with _connect() as conn:
        # SELECT 1 — нам не нужны данные, только факт наличия строки
        # (экономнее и нагляднее, чем SELECT *).
        row = conn.execute(
            "SELECT 1 FROM users WHERE user_id = ?", (user_id,)
        ).fetchone()
    return row is not None   # есть строка → пользователь существует


# --- get_user_field(user_id, field) -- чтение ОДНОЙ колонки.
# Поле подставляется в f-string, поэтому ТОЛЬКО из _USER_FIELDS.
# Возвращает: значение колонки (int/str/None) или None, если поле неверное.
def get_user_field(user_id, field):
    """
    Получает конкретное поле пользователя.

    Args:
        user_id: Числовой ID пользователя
        field: Название поля (только из _USER_FIELDS)

    Returns:
        Значение поля или None (если поле неверное или пользователь не найден).
    """
    # Защита от SQL-инъекции: с f-string работает только имя из белого списка.
    if field not in _USER_FIELDS:
        return None   # неизвестное поле не пропускаем в SQL
    with _connect() as conn:
        # f-string в SELECT безопасен: field уже проверен в _USER_FIELDS.
        row = conn.execute(
            f"SELECT {field} FROM users WHERE user_id = ?", (user_id,)
        ).fetchone()
    return row[0] if row is not None else None   # row[0] — первая (единственная) колонка


# --- set_user_field(user_id, field, value) -- запись ОДНОЙ колонки.
# Перед UPDATE значение нормализуется: approved → 0/1, _INT_FIELDS → int.
# Возвращает: None (всегда; на результат обычно не смотрят).
def set_user_field(user_id, field, value):
    """
    Устанавливает конкретное поле пользователя.

    Args:
        user_id: Числовой ID пользователя
        field: Название поля (только из _USER_FIELDS)
        value: Новое значение
    """
    if field not in _USER_FIELDS:
        return   # неизвестное поле молча игнорируем (не бросаем исключение)

    # Нормализуем значение под тип колонки.
    if field == "approved":
        # approved хранится как 0/1 — приводим булево значение.
        value = 1 if value else 0
    elif field in _INT_FIELDS:
        # Целочисленные поля приводим через _as_int (None → NULL).
        value = _as_int(value)

    with _connect() as conn:
        # f"UPDATE ... SET {field} = ?" безопасно: field уже из белого списка.
        conn.execute(
            f"UPDATE users SET {field} = ? WHERE user_id = ?",
            (value, user_id),
        )   # значение всегда передаётся параметром, никогда — строкой


# --- approve_user(user_id) -- подтверждение модератором.
# Ключевая тонкость: итоговая роль берётся из role_requested (что выбрал
# сам пользователь при регистрации), а не «наугад». Один UPDATE меняет
# и approved, и role — атомарно.
def approve_user(user_id):
    """
    Подтверждает пользователя модератором.

    Роль берётся из сохранённого при регистрации поля role_requested
    (student/teacher/moderator), чтобы, например, учитель не был ошибочно
    подтверждён как ученик. Если роль не сохранялась — по умолчанию ученик.

    Args:
        user_id: Числовой ID пользователя
    """
    # Какую роль запросил пользователь при регистрации?
    requested = get_user_field(user_id, "role_requested") or "student"
    # Валидация: допускаем только известные роли. Любые левые значения
    # (пустая строка, опечатка) превращаем в "student".
    if requested not in ("student", "teacher", "moderator"):
        requested = "student"   # любое неизвестное значение → ученик

    with _connect() as conn:
        # Подтверждаем (approved=1) и сразу выставляем итоговую роль —
        # одной транзакцией, чтобы не было «подтверждён, но без роли».
        conn.execute(
            "UPDATE users SET approved = 1, role = ? WHERE user_id = ?",
            (requested, user_id),
        )


# --- set_role(user_id, role) -- прямое назначение роли.
# Используется панелью модератора для ручной смены роли без регистрации.
def set_role(user_id, role):
    """
    Устанавливает роль пользователя.

    Args:
        user_id: Числовой ID пользователя
        role: Новая роль (student/teacher/moderator/rejected)
    """
    with _connect() as conn:
        # Простой UPDATE одной колонки; роль приходит из панели модератора
        # (модератор может вручную сменить роль без перерегистрации).
        conn.execute(
            "UPDATE users SET role = ? WHERE user_id = ?", (role, user_id)
        )


# --- delete_user(user_id) -- полное удаление пользователя.
# Удаляет строки с данным user_id ИЗ ВСЕХ таблиц, где он встречается.
# Возвращает: общее число удалённых строк (для отчёта в скрипте сброса).
def delete_user(user_id):
    """
    Полностью удаляет пользователя и все его данные из всех таблиц.

    Используется, например, скриптом reset_test_user.py, чтобы «обнулить»
    тестовый аккаунт и заново пройти регистрацию.

    Args:
        user_id: Числовой ID пользователя

    Returns:
        Сколько всего строк удалено (пользователь + его заметки/логи).
    """
    total = 0
    # user_id присутствует во всех таблицах (users, notes, ai_log, ai_errors).
    # Проходим по всем таблицам, где есть колонка user_id, и удаляем строки.
    # join_requests намеренно НЕ трогаем: история заявок может пригодиться.
    for table in ("users", "notes", "ai_log", "ai_errors"):
        with _connect() as conn:
            # rowcount — сколько строк реально удалено этим DELETE
            # (курсор после execute возвращает значение в .rowcount).
            cur = conn.execute(
                f"DELETE FROM {table} WHERE user_id = ?", (user_id,)
            )
            total += cur.rowcount   # копим общее число удалённых строк
    return total


# ============================================================
# ПРОВЕРКИ РОЛЕЙ И СТАТУСОВ
# ============================================================
#
# Быстрые предикаты-проверки, которыми пронизан весь код бота:
#   is_moderator/is_teacher/is_student — сравнение роли;
#   is_approved                       — статус подтверждения;
#   is_blocked                         — под блокировкой ли пользователь.
# Каждый возвращает bool и никогда не бросает исключений: если строки в
# БД нет — вернётся False (надёжнее, чем падать с ошибкой).
# ============================================================

# --- is_moderator: ДВА источника прав.
#   1) программный список MODERATOR_IDS в config.py (работает всегда);
#   2) роль 'moderator' в БД (выдана через панель).
def is_moderator(user_id):
    """
    Проверяет, является ли пользователь модератором.

    Пользователь считается модератором, если:
    1. Его ID указан в списке MODERATOR_IDS в config.py, ИЛИ
    2. Его роль в базе данных — "moderator"

    Args:
        user_id: Числовой ID пользователя

    Returns:
        True если модератор, False если нет.
    """
    # Локальный импорт, чтобы не тащить config.py в шапку модуля (избегаем
    # потенциальных циклов импорта и лишних зависимостей).
    from config import MODERATOR_IDS

    # Сначала проверяем «программный» список модераторов в конфиге —
    # он работает даже без строки в БД (например, сразу после старта).
    if user_id in (MODERATOR_IDS or []):
        return True

    # Затем — роль в базе данных (модератор мог быть назначен панелью).
    return get_user_field(user_id, "role") == "moderator"


def is_teacher(user_id):
    """True, если роль пользователя 'teacher'."""
    # Быстрая проверка одной колонки через get_user_field (SELECT одной колонки).
    return get_user_field(user_id, "role") == "teacher"


def is_student(user_id):
    """True, если роль пользователя 'student'."""
    return get_user_field(user_id, "role") == "student"


# --- is_approved: отдельное поле approved (0/1) — пользователь прошёл
# подтверждение модератором. Отделено от роли: сначала роль 'pending',
# потом подтверждают → approved=1 и роль становится конечной.
def is_approved(user_id):
    """
    Проверяет, подтверждён ли пользователь модератором.

    Returns:
        True если подтверждён, False если нет (или пользователь не найден).
    """
    with _connect() as conn:
        row = conn.execute(
            "SELECT approved FROM users WHERE user_id = ?", (user_id,)
        ).fetchone()
    # bool(0/1) превращает значение колонки в булево; None → False
    # (нет пользователя — считаем «не подтверждён»).
    return bool(row["approved"]) if row is not None else False


# ============================================================
# БЛОКИРОВКА ПОЛЬЗОВАТЕЛЕЙ
# ============================================================
#
# Заблокированный пользователь не видит меню бота — ему показывается заглушка.
# Блокировка хранится в колонке users.blocked (0/1).
# Блокировка НЕ удаляет пользователя и НЕ сбрасывает его роль — её можно
# снять в любой момент, и всё вернётся на свои места.
# Отличие от «pending»: pending ждёт ПЕРВОГО подтверждения, а blocked —
# это уже отключение РАБОЧЕГО (подтверждённого) аккаунта.
# ============================================================

# --- is_blocked: чтение флага blocked. Нет строки → False.
def is_blocked(user_id):
    """
    Проверяет, заблокирован ли пользователь модератором.

    Заблокированный пользователь не может пользоваться ботом
    (у него отображается заглушка вместо меню).

    Args:
        user_id: Числовой ID пользователя

    Returns:
        True если заблокирован, False если нет (или пользователь не найден).
    """
    with _connect() as conn:
        row = conn.execute(
            "SELECT blocked FROM users WHERE user_id = ?", (user_id,)
        ).fetchone()
    return bool(row["blocked"]) if row is not None else False   # нет строки → не заблокирован


# --- set_blocked: явная установка флага (True/False → 1/0).
def set_blocked(user_id, blocked):
    """
    Устанавливает статус блокировки пользователя.

    Args:
        user_id: Числовой ID пользователя
        blocked: True — заблокировать, False — разблокировать.
    """
    with _connect() as conn:
        conn.execute(
            "UPDATE users SET blocked = ? WHERE user_id = ?",
            (1 if blocked else 0, user_id),   # булево → 0/1 для SQLite
        )


# --- toggle_blocked: переключатель «заблокирован → разблокирован → ...».
# Возвращает новый статус, чтобы панель сразу показала актуальное состояние.
def toggle_blocked(user_id):
    """
    Переключает статус блокировки пользователя (заблокирован ↔ разблокирован).

    Args:
        user_id: Числовой ID пользователя

    Returns:
        Новый статус блокировки (True — теперь заблокирован, False — разблокирован).
    """
    new_status = not is_blocked(user_id)   # инвертируем текущий статус
    set_blocked(user_id, new_status)       # и применяем в базе
    return new_status                      # возвращаем новый статус (для UI)


# ============================================================
# СПИСКИ ПОЛЬЗОВАТЕЛЕЙ
# ============================================================
#
# Выборки целых групп пользователей: всех, подтверждённых, по ролям и т.д.
# Нужны панели модератора, рассылкам и меню.
#
# Общий приём: fetchall() возвращает список sqlite3.Row; дальше из каждой
# строки берём нужную колонку по имени (row["user_id"]) и складываем
# получившиеся значения в плоский список (list comprehension).
# ============================================================

# --- get_all_users: полный список для панели модератора.
# Сортировка registered_at, user_id — стабильная и предсказуемая.
# Возвращает список dict'ов (approved/blocked уже как bool).
def get_all_users():
    """
    Получает список всех пользователей в виде словарей (уже отсортированных).
    Удобно для панели модератора и рассылок.

    Returns:
        Список словарей с данными пользователей.
    """
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM users ORDER BY registered_at, user_id"
        ).fetchall()   # сначала по времени регистрации, потом по ID
                       # (реальный порядок по времени даже при одинаковых registered_at)
    users = []
    for row in rows:
        user = dict(row)
        # 0/1 в колонках превращаем в булево для удобства UI.
        user["approved"] = bool(user.get("approved"))
        user["blocked"] = bool(user.get("blocked"))
        users.append(user)
    return users


# --- get_approved_chat_ids: для массовой РАССЫЛКИ.
# Только approved=1, только не заблокированные, только с непустым chat_id
# (без chat_id сообщение не доставить в принципе).
def get_approved_chat_ids():
    """
    Получает chat_id всех подтверждённых и не заблокированных пользователей.

    Используется для массовой рассылки сообщений модератором.

    Returns:
        Список числовых chat_id.
    """
    with _connect() as conn:
        rows = conn.execute(
            "SELECT chat_id FROM users "
            "WHERE approved = 1 AND blocked = 0 AND chat_id IS NOT NULL"
        ).fetchall()   # без chat_id достучаться до пользователя нельзя —
                       # такие строки (chat_id IS NULL) просто пропускаем
    return [row["chat_id"] for row in rows]

# --- get_all_user_ids: все зарегистрированные ID (для панели/рассылок).
def get_all_user_ids():
    """
    Получает список всех зарегистрированных user_id.

    Сортировка по времени регистрации, затем по ID — стабильный порядок
    для отображения в панели модератора.

    Returns:
        Список числовых ID (может быть пустым).
    """
    with _connect() as conn:
        rows = conn.execute(
            "SELECT user_id FROM users ORDER BY registered_at, user_id"
        ).fetchall()
    return [row["user_id"] for row in rows]


# --- find_user_by_chat_id: ОБРАТНЫЙ поиск — по chat_id находим user_id.
# MAX разделяет логин и чат, поэтому chat_id уникален и удобен для опознания
# (например, при входящем сообщении мы знаем chat_id, но не всегда user_id).
def find_user_by_chat_id(chat_id):
    """
    Находит user_id пользователя по его chat_id.

    MAX разделяет логин-ID и идентификатор чата, поэтому chat_id — надёжный
    способ опознать пользователя (он сохраняется при регистрации).

    Args:
        chat_id: Идентификатор чата пользователя

    Returns:
        user_id (число) если найден, иначе None.
    """
    if not chat_id:
        return None   # пустой chat_id не ищем (None/0/"" отсекаем сразу)
    with _connect() as conn:
        row = conn.execute(
            "SELECT user_id FROM users WHERE chat_id = ?", (chat_id,)
        ).fetchone()
    return row["user_id"] if row is not None else None   # обратный поиск по chat_id


# --- get_pending_users: кто ещё ждёт подтверждения (вкладка панели).
def get_pending_users():
    """
    Получает список пользователей, ожидающих подтверждения (role='pending').

    Returns:
        Список user_id со статусом "pending".
    """
    with _connect() as conn:
        rows = conn.execute(
            "SELECT user_id FROM users WHERE role = 'pending' ORDER BY registered_at, user_id"
        ).fetchall()
    return [row["user_id"] for row in rows]


# ============================================================
# ЛИМИТЫ ИИ
# ============================================================
#
# У каждого пользователя есть дневной лимит запросов к ИИ. Счётчик обнуляется
# автоматически, если с последнего сброса прошло больше суток.
# Поля в users:
#   ai_daily_limit — сколько запросов разрешено в сутки;
#   ai_used_today  — сколько уже использовано сегодня;
#   ai_last_reset  — Unix-время последнего обнуления счётчика.
#
# Идея: лимит считается «скользящим окном» 24 часа, а не привязкой
# к календарным суткам — это проще и предсказуемее в реализации.
# Схема работы:
#   check_ai_limit()   → можно ли сейчас делать запрос (и автосброс счётчика);
#   increment_ai_usage() → +1 к счётчику ПОСЛЕ успешного ответа ИИ;
#   get_ai_usage()     → сколько уже потрачено (для UI панели).
# ============================================================

# --- check_ai_limit: «можно ли спросить ИИ?».
# Читает сразу 3 колонки одним SELECT; если прошло > 24 часов с последнего
# сброса — обнуляет счётчик прямо здесь (и снова разрешает запрос).
def check_ai_limit(user_id):
    """
    Проверяет, не превышен ли дневной лимит запросов к ИИ.
    Автоматически сбрасывает счётчик, если с последнего сброса прошло больше
    24 часов (86400 секунд).

    Args:
        user_id: Числовой ID пользователя

    Returns:
        True если можно отправлять запросы, False если лимит превышен.
    """
    with _connect() as conn:
        # Читаем сразу три колонки, нужных для подсчёта лимита,
        # одним запросом (без трёх отдельных SELECT).
        row = conn.execute(
            "SELECT ai_used_today, ai_last_reset, ai_daily_limit "
            "FROM users WHERE user_id = ?",
            (user_id,),
        ).fetchone()

    if row is None:
        return False   # пользователя нет — работать с ИИ нельзя

    now = int(time.time())
    last_reset = row["ai_last_reset"] or 0       # когда сбрасывали счётчик
    used = row["ai_used_today"] or 0             # сколько потрачено
    limit = row["ai_daily_limit"] or 50          # лимит (по умолчанию 50)

    # Новый день → обнуляем счётчик использованных запросов.
    # 86400 секунд = ровно 24 часа (простой «скользящий» период).
    if now - last_reset > 86400:
        with _connect() as conn:
            conn.execute(
                "UPDATE users SET ai_used_today = 0, ai_last_reset = ? "
                "WHERE user_id = ?",
                (now, user_id),
            )
        used = 0   # локально тоже обнуляем, чтобы верно сравнить ниже

    return used < limit   # лимит не превышен, пока потрачено меньше лимита


# --- increment_ai_usage: +1 к счётчику после каждого ответа ИИ.
# Инкремент сделан прямо в SQL-выражении (ai_used_today + 1) — атомарно.
def increment_ai_usage(user_id):
    """Увеличивает счётчик использованных запросов к ИИ на 1."""
    with _connect() as conn:
        # Инкремент прямо в SQL — атомарно, без гонок между потоками
        # (прочитать-изменить-записать в Python могло бы потерять вызовы).
        conn.execute(
            "UPDATE users SET ai_used_today = ai_used_today + 1 WHERE user_id = ?",
            (user_id,),
        )


# --- get_ai_usage: сколько запросов уже использовано сегодня (для отчётов).
def get_ai_usage(user_id):
    """
    Получает количество использованных запросов к ИИ за сегодня.

    Returns:
        Число использованных запросов (0, если пользователь не найден).
    """
    with _connect() as conn:
        row = conn.execute(
            "SELECT ai_used_today FROM users WHERE user_id = ?", (user_id,)
        ).fetchone()
    return row["ai_used_today"] if row is not None else 0


# ============================================================
# ЗАМЕТКИ
# ============================================================
#
# Личные заметки пользователей: сохранить, получить список, удалить по
# индексу в списке (индекс, а не ID — чтобы совпадать с кнопками бота).
#
# Таблица: notes. Поля:
#   id — внутренний автоинкрементный ключ;
#   user_id — владелец заметки;
#   title / content — заголовок и содержимое;
#   time — Unix-время создания.
#
# ВАЖНО про «индекс»: бот показывает заметки кнопками «1», «2», «3»…,
# поэтому для удаления пользователь присылает ИНДЕКС в списке, а не id.
# Индекс = позиция строки при ORDER BY id (0, 1, 2, ...). Если удалить одну,
# индексы остальных поменяются — но бот перечитывает список перед показом.
# ============================================================

# --- save_note: INSERT новой заметки. Время проставляем ПРИ создании.
def save_note(user_id, title, content):
    """
    Сохраняет заметку пользователя.

    Args:
        user_id: Числовой ID пользователя
        title: Заголовок заметки
        content: Содержимое заметки
    """
    with _connect() as conn:
        conn.execute(
            "INSERT INTO notes (user_id, title, content, time) VALUES (?, ?, ?, ?)",
            (user_id, title, content, int(time.time())),   # время создания
        )   # time принудительно в int (SQLite любит целые числа для сравнений)


# --- get_notes: список заметок пользователя С ИНДЕКСАМИ.
# ORDER BY id = хронологический порядок. Возвращает dict с ключом "index".
def get_notes(user_id):
    """
    Получает все заметки пользователя.

    Заметки возвращаются в порядке создания (старые сначала). Индекс заметки
    ("index") — это её позиция в этом списке, по нему работают кнопки
    просмотра/удаления в боте.

    Returns:
        Список словарей: {"index", "title", "content", "time"}.
    """
    with _connect() as conn:
        rows = conn.execute(
            "SELECT id, title, content, time FROM notes "
            "WHERE user_id = ? ORDER BY id",   # ORDER BY id = по времени создания
            (user_id,),
        ).fetchall()   # id растёт монотонно, поэтому порядок = хронологический

    notes = []
    for i, row in enumerate(rows):
        # В каждом словаре кроме данных храним «index» — позицию в списке,
        # которую понимают кнопки бота (0, 1, 2, ...).
        # enumerate даёт i = 0, 1, 2... ровно по порядку строк.
        notes.append({
            "index": i,                     # позиция заметки в списке
            "id": row["id"],                # внутренний ID в БД
            "title": row["title"],
            "content": row["content"],
            "time": row["time"],
        })
    return notes


# --- delete_note: удаление ПО ИНДЕКСУ, но физически — по внутреннему id.
# Сначала получаем список id в ТОМ ЖЕ порядке, что и get_notes, затем
# берём элемент с нужным индексом и удаляем по id (безопасно, чужие не
# заденем). Вне диапазона → False.
def delete_note(user_id, note_index):
    """
    Удаляет заметку пользователя по индексу в списке (как его отдаёт
    get_notes). Индекс, а не ID, чтобы совпадать с кнопками в боте.

    Args:
        user_id: Числовой ID пользователя
        note_index: Индекс заметки (начиная с 0)

    Returns:
        True если заметка удалена, False если индекс вне диапазона.
    """
    with _connect() as conn:
        # Получаем ID заметок именно в том порядке, в каком их видит бот
        # (тот же ORDER BY id, что и в get_notes — иначе индекс «съедет»).
        rows = conn.execute(
            "SELECT id FROM notes WHERE user_id = ? ORDER BY id", (user_id,)
        ).fetchall()

    if note_index < 0 or note_index >= len(rows):
        return False   # индекс за пределами списка — удалять нечего

    note_id = rows[note_index]["id"]   # переводим индекс в внутренний ID
    with _connect() as conn:
        conn.execute("DELETE FROM notes WHERE id = ?", (note_id,))
    return True   # удалили по внутреннему ID, чтобы не удалить чужую заметку


# ============================================================
# ИСТОРИЯ ЗАПРОСОВ К ИИ (для учителей)
# ============================================================
#
# Логи «вопрос → ответ ИИ» сохраняются для каждого запроса, чтобы учителя
# и модераторы могли посмотреть, что спрашивали ученики.
#
# Таблица: ai_log. Поля:
#   id — автоинкремент;
#   user_id — кто спрашивал;
#   user_name — ИМЯ НА МОМЕНТ ЗАПРОСА (снимок: позже не изменится);
#   question / answer — сам диалог;
#   time — когда был запрос.
#
# Общий приём «LIMIT + reverse()»: чтобы показать последние N записей «от
# старых к новым», сначала берём свежие (ORDER BY id DESC LIMIT N), а потом
# разворачиваем список в памяти. Так не нужен вложенный запрос.
# ============================================================

# --- save_ai_log: сохранить вопрос и ответ. Имя берём ДО записи — это снимок.
def save_ai_log(user_id, question, answer):
    """
    Сохраняет лог запроса к ИИ (для просмотра учителями).

    Args:
        user_id: Числовой ID пользователя
        question: Вопрос пользователя
        answer: Ответ ИИ
    """
    # Имя подхватываем на момент записи — оно уже не изменится позже
    # (даже если пользователь сменит имя, в логе останется старое).
    user_name = get_user_field(user_id, "name") or ""
    with _connect() as conn:
        # INSERT INTO ai_log — добавляем новую строку в журнал запросов ИИ.
        conn.execute(
            "INSERT INTO ai_log (user_id, user_name, question, answer, time) "
            "VALUES (?, ?, ?, ?, ?)",
            (user_id, user_name, question, answer, int(time.time())),
        )


# --- get_ai_logs: последние N записей по ВСЕМ пользователям (для учителя).
def get_ai_logs(limit=20):
    """
    Получает последние записи лога запросов к ИИ.

    Сначала берём самые свежие записи (ORDER BY id DESC LIMIT ?), затем
    разворачиваем их в порядке возрастания, чтобы формат совпадал со старым
    поведением (логи выдаются «от старых к новым»).

    Args:
        limit: Максимальное количество записей

    Returns:
        Список словарей: {"user_id", "user_name", "question", "answer", "time"}.
    """
    with _connect() as conn:
        rows = conn.execute(
            "SELECT user_id, user_name, question, answer, time FROM ai_log "
            "ORDER BY id DESC LIMIT ?",   # самые свежие — первыми в выборке
            (limit,),
        ).fetchall()
    rows.reverse()   # разворачиваем: старые → новые
    # Каждую строку sqlite3.Row превращаем в обычный dict — удобно по имени.
    # dict(r) возвращает {"user_id": ..., "user_name": ..., ...}
    return [dict(r) for r in rows]


# --- get_ai_logs_for_user: то же, но ТОЛЬКО для конкретного ученика.
def get_ai_logs_for_user(user_id, limit=20):
    """
    Получает логи запросов к ИИ для конкретного пользователя.

    Args:
        user_id: Числовой ID пользователя
        limit: Максимальное количество записей

    Returns:
        Список словарей с логами (самые свежие в конце).
    """
    with _connect() as conn:
        rows = conn.execute(
            "SELECT user_id, user_name, question, answer, time FROM ai_log "
            "WHERE user_id = ? ORDER BY id DESC LIMIT ?",
            (user_id, limit),
        ).fetchall()
    rows.reverse()   # та же нормализация порядка, что и в get_ai_logs
    return [dict(r) for r in rows]


# --- clear_ai_logs_for_user: вычистить историю одного ученика.
# Возвращает число удалённых строк (для подтверждения в UI).
def clear_ai_logs_for_user(user_id):
    """
    Удаляет всю историю запросов к ИИ конкретного пользователя.

    Args:
        user_id: Числовой ID пользователя

    Returns:
        Сколько записей удалено.
    """
    with _connect() as conn:
        cur = conn.execute("DELETE FROM ai_log WHERE user_id = ?", (user_id,))
        return cur.rowcount   # rowcount — сколько строк удалено


# ============================================================
# ЛОГ ОШИБОК ИИ (для модераторов)
# ============================================================
#
# Отдельная таблица под сбои ИИ: что пошло не так, у кого, в каком режиме.
# Ошибка — это НЕ обычный запрос: её показывает только панель модератора,
# поэтому храним отдельно от ai_log.
#
# Таблица: ai_errors. Поля:
#   id — автоинкремент;
#   user_id / user_name — кто («у кого сломалось»);
#   mode — режим запроса: question / schedule / test / homework;
#   error — текст ошибки (raw, например traceback или сообщение API);
#   time — когда произошла ошибка.
# ============================================================

# --- save_ai_error: записать факт сбоя. Имя по умолчанию «Неизвестно».
def save_ai_error(user_id, mode, error_text):
    """
    Сохраняет ошибку обращения к ИИ (для просмотра модераторами).

    Args:
        user_id: Числовой ID пользователя
        mode: Режим запроса (question/schedule/test/homework)
        error_text: Текст ошибки
    """
    # Имя фиксируем «здесь и сейчас»; для ошибок по умолчанию «Неизвестно».
    user_name = get_user_field(user_id, "name") or "Неизвестно"
    with _connect() as conn:
        conn.execute(
            "INSERT INTO ai_errors (user_id, user_name, mode, error, time) "
            "VALUES (?, ?, ?, ?, ?)",
            (user_id, user_name, mode, error_text, int(time.time())),
        )


# --- get_ai_errors: последние ошибки для панели (та же схема LIMIT+reverse).
def get_ai_errors(limit=50):
    """
    Получает последние записи лога ошибок ИИ (свежие в конце списка).

    Args:
        limit: Максимальное количество записей

    Returns:
        Список словарей: {"user_id", "user_name", "mode", "error", "time"}.
    """
    with _connect() as conn:
        rows = conn.execute(
            "SELECT user_id, user_name, mode, error, time FROM ai_errors "
            "ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
    rows.reverse()   # так же, как в логах ИИ: старые → новые
    return [dict(r) for r in rows]


# --- clear_ai_errors: полностью очистить лог ошибок (кнопка в панели).
def clear_ai_errors():
    """Полностью очищает лог ошибок ИИ."""
    with _connect() as conn:
        conn.execute("DELETE FROM ai_errors")   # без WHERE — удаляем всё


# ============================================================
# ЗАЯВКИ НА ВСТУПЛЕНИЕ В ШКОЛУ/КЛАСС
# ============================================================
#
# Ученик подаёт заявку (с кнопками школы и класса), учитель или модератор
# подтверждает/отклоняет её. При подтверждении школа и класс ученика
# обновляются на указанные в заявке.
#
# Таблица: join_requests. Поля:
#   id — автоинкремент;
#   user_id — кто подал заявку;
#   target_school / target_class — куда хочет вступить;
#   status — pending / approved / rejected;
#   teacher_id — кто обработал (user_id учителя или модератора);
#   time — когда подана.
#
# ЖИЗНЕННЫЙ ЦИКЛ:
#   pending  → approved  (школа/класс ученика обновляются на целевые)
#   pending  → rejected  (ученик не вступает, данные не меняются)
# Повторная обработка НЕВОЗМОЖНА: и approve, и reject проверяют,
# что заявка ещё в status='pending'.
# ============================================================

# --- create_join_request: INSERT заявки, сразу status='pending'.
# Возвращает lastrowid — ID новой заявки (нужен для кнопок «подтвердить»).
def create_join_request(user_id, target_school, target_class):
    """
    Создаёт заявку ученика на вступление в школу/класс.

    Заявка создаётся со статусом 'pending' и ждёт подтверждения
    учителя из указанной школы.

    Args:
        user_id: Числовой ID ученика
        target_school: Название школы
        target_class: Название класса

    Returns:
        ID созданной заявки.
    """
    with _connect() as conn:
        # status вставляем константой 'pending' прямо в SQL —
        # новую заявку никто никогда не создаёт в другом статусе.
        cur = conn.execute(
            "INSERT INTO join_requests (user_id, target_school, target_class, status, time) "
            "VALUES (?, ?, ?, 'pending', ?)",
            (user_id, target_school, target_class, int(time.time())),
        )
        return cur.lastrowid   # lastrowid — ID только что созданной строки


# --- get_pending_join_requests: ВСЕ ожидающие заявки для панели.
# LEFT JOIN users добавляет имя ученика и его ТЕКУЩИЕ школу/класс —
# чтобы сравнить «откуда уходит» ⇒ «куда идёт».
def get_pending_join_requests():
    """
    Получает все заявки на вступление со статусом 'pending'.

    Returns:
        Список словарей с данными заявок (включая имя ученика).
    """
    with _connect() as conn:
        # LEFT JOIN users — подтягиваем имя ученика (jr.* — всё из заявки,
        # плюс поля пользователя full_name/name и его текущие школа/класс,
        # чтобы сравнить «откуда» ⇒ «куда»).
        # LEFT (а не INNER) JOIN: заявка покажется даже если ученика удалили.
        rows = conn.execute(
            "SELECT jr.*, u.full_name, u.name, u.school as current_school, u.class_name as current_class "
            "FROM join_requests jr "
            "LEFT JOIN users u ON jr.user_id = u.user_id "
            "WHERE jr.status = 'pending' "
            "ORDER BY jr.time, jr.id"   # старые заявки сначала
        ).fetchall()
    return [dict(r) for r in rows]


# --- get_pending_join_requests_for_school: те же заявки, но ТОЛЬКО для
# школы учителя (учитель видит «свои» заявки и не трогает чужие).
def get_pending_join_requests_for_school(school_name):
    """
    Получает заявки на вступление для конкретной школы.

    Args:
        school_name: Название школы

    Returns:
        Список словарей с данными заявок.
    """
    with _connect() as conn:
        # Та же выборка, но с фильтром по целевой школе заявки
        # (учитель видит заявки только «в свою» школу).
        rows = conn.execute(
            "SELECT jr.*, u.full_name, u.name, u.school as current_school, u.class_name as current_class "
            "FROM join_requests jr "
            "LEFT JOIN users u ON jr.user_id = u.user_id "
            "WHERE jr.status = 'pending' AND jr.target_school = ? "
            "ORDER BY jr.time, jr.id",
            (school_name,),
        ).fetchall()
    return [dict(r) for r in rows]


# --- approve_join_request: подтверждение + обновление данных ученика.
# ВАЖНО: обе изменяющие операции (заявка → approved; users.school/class)
# выполняются ВНУТРИ ОДНОГО with _connect() — значит и зафиксируются одним
# commit'ом. Это атомарность: не бывает «заявка принята, но ученик не
# переведён», и наоборот.
def approve_join_request(request_id, teacher_id):
    """
    Подтверждает заявку на вступление и обновляет данные ученика.

    Меняет школу и класс ученика на указанные в заявке.

    Args:
        request_id: ID заявки
        teacher_id: user_id учителя, подтвердившего заявку

    Returns:
        True если заявка обработана, False если не найдена.
    """
    with _connect() as conn:
        # Берём заявку ТОЛЬКО в статусе pending — повторно её обработать
        # нельзя (условие status = 'pending' в WHERE защищает от двойного
        # подтверждения даже в момент гонки двух запросов).
        row = conn.execute(
            "SELECT * FROM join_requests WHERE id = ? AND status = 'pending'",
            (request_id,),
        ).fetchone()

        if row is None:
            return False   # не найдена или уже обработана

        request = dict(row)
        # Помечаем заявку принятой и запоминаем, кто её подтвердил.
        conn.execute(
            "UPDATE join_requests SET status = 'approved', teacher_id = ? WHERE id = ?",
            (teacher_id, request_id),
        )
        # Обновляем у ученика школу и класс на целевые из заявки.
        conn.execute(
            "UPDATE users SET school = ?, class_name = ? WHERE user_id = ?",
            (request["target_school"], request["target_class"], request["user_id"]),
        )
        return True   # обе транзакции зафиксируются одним commit'ом в _connect()


# --- reject_join_request: отклонение. Ученика и его данные НЕ трогаем —
# только статус заявки и кто отклонил.
def reject_join_request(request_id, teacher_id):
    """
    Отклоняет заявку на вступление.

    Args:
        request_id: ID заявки
        teacher_id: user_id учителя, отклонившего заявку

    Returns:
        True если заявка обработана, False если не найдена.
    """
    with _connect() as conn:
        # Проверяем, что заявка ещё «живая» (pending) — SELECT 1 только
        # для факта существования, без чтения лишних данных.
        row = conn.execute(
            "SELECT 1 FROM join_requests WHERE id = ? AND status = 'pending'",
            (request_id,),
        ).fetchone()

        if row is None:
            return False   # нет pending-заявки с таким id → отклонять нечего

        # Меняем статус на rejected (данные ученика не трогаем — он просто
        # не попадает в эту школу).
        conn.execute(
            "UPDATE join_requests SET status = 'rejected', teacher_id = ? WHERE id = ?",
            (teacher_id, request_id),
        )
        return True


# --- get_user_join_requests: история заявок ОДНОГО ученика (последние 10).
def get_user_join_requests(user_id):
    """
    Получает историю заявок пользователя на вступление.

    Args:
        user_id: Числовой ID пользователя

    Returns:
        Список словарей с данными заявок.
    """
    with _connect() as conn:
        # Последние 10 заявок пользователя, самые свежие — первыми.
        rows = conn.execute(
            "SELECT * FROM join_requests WHERE user_id = ? ORDER BY time DESC LIMIT 10",
            (user_id,),
        ).fetchall()
    return [dict(r) for r in rows]


# --- get_join_request_by_id: одна заявка по id + имя ученика (LEFT JOIN).
def get_join_request_by_id(request_id):
    """
    Получает заявку по ID.

    Args:
        request_id: ID заявки

    Returns:
        Словарь с данными заявки или None.
    """
    with _connect() as conn:
        # С именем ученика через LEFT JOIN, по конкретному id заявки.
        # LEFT JOIN: если ученик удалён — заявка всё равно вернётся,
        # но поля имени будут None.
        row = conn.execute(
            "SELECT jr.*, u.full_name, u.name "
            "FROM join_requests jr "
            "LEFT JOIN users u ON jr.user_id = u.user_id "
            "WHERE jr.id = ?",
            (request_id,),
        ).fetchone()
    return dict(row) if row else None


# --- leave_school: ученик «выходит» из школы/класса.
# Ставим ПУСТЫЕ СТРОКИ (не NULL), чтобы вызывающий код, ожидающий str,
# не ловил исключения. Учётная запись и роль сохраняются.
def leave_school(user_id):
    """
    Удаляет ученика из школы и класса (ставит пустые строки).

    Пользователь остаётся зарегистрированным, но теряет привязку
    к школе/классу. Может потом подать заявку на вступление.

    Args:
        user_id: Числовой ID пользователя
    """
    with _connect() as conn:
        # Просто очищаем поля: школа и класс становятся пустыми строками
        # (не NULL — чтобы код, ожидающий строку, не ловил исключения).
        conn.execute(
            "UPDATE users SET school = '', class_name = '' WHERE user_id = ?",
            (user_id,),
        )


# --- get_teachers_by_school: кому уведомить о новой заявке.
# Ищем teacher/moderator этой школы, исключаем заблокированных.
def get_teachers_by_school(school_name):
    """
    Получает user_id учителей и модераторов, работающих в указанной школе.

    Нужно, чтобы уведомить учителя о новой заявке ученика на вступление.

    Args:
        school_name: Название школы

    Returns:
        Список числовых user_id.
    """
    if not school_name:
        return []   # без названия школы искать бессмысленно
    with _connect() as conn:
        # Берём роль teacher/moderator в этой школе и исключаем
        # заблокированных (blocked = 0) — им уведомления не шлём.
        rows = conn.execute(
            "SELECT user_id FROM users "
            "WHERE school = ? AND role IN ('teacher', 'moderator') AND blocked = 0",
            (school_name,),
        ).fetchall()
    return [row["user_id"] for row in rows]


# ============================================================
# ДОПОЛНИТЕЛЬНЫЕ ФУНКЦИИ ДЛЯ ПАНЕЛИ МОДЕРАТОРА
# ============================================================
#
# Специфичные для десктоп-приложения выборки: рассылки по ролям, сброс
# лимита, счётчики для карточек статистики, общий список заявок.
# Эти функции вызываются ТОЛЬКО из moderator_app.py (панель модератора),
# но живут здесь, чтобы всё общение с БД было в одном модуле.
# ============================================================

# --- get_chat_ids_by_role: рассылка из панели по целевой группе.
# role="all" → все подтверждённые; иначе — только с заданной ролью.
# В обоих случаях: approved=1, blocked=0, chat_id не NULL.
def get_chat_ids_by_role(role):
    """
    Получает chat_id подтверждённых и не заблокированных пользователей
    с указанной ролью. Для role="all" — все подтверждённые.

    Используется для рассылки по целевой аудитории из панели модератора.

    Args:
        role: "all", "student", "teacher" или "moderator".

    Returns:
        Список числовых chat_id.
    """
    # Валидация: неизвестная роль = «всем» (role=None ниже означает без фильтра).
    if role not in ("student", "teacher", "moderator"):
        role = None   # "all" и любой мусор → рассылка всем подтверждённым
    with _connect() as conn:
        if role:
            # Конкретная роль: добавляем условие role = ?.
            rows = conn.execute(
                "SELECT chat_id FROM users "
                "WHERE approved = 1 AND blocked = 0 AND role = ? AND chat_id IS NOT NULL",
                (role,),
            ).fetchall()
        else:
            # Все подтверждённые и не заблокированные (нет фильтра по роли).
            rows = conn.execute(
                "SELECT chat_id FROM users "
                "WHERE approved = 1 AND blocked = 0 AND chat_id IS NOT NULL"
            ).fetchall()
    return [row["chat_id"] for row in rows]


# --- reset_ai_usage: «вернуть» ученику лимит сегодня (кнопка в панели).
# Обнуляем только ai_used_today; ai_last_reset не трогаем, чтобы не сбить
# логику автосброса следующего дня.
def reset_ai_usage(user_id):
    """
    Обнуляет счётчик использованных запросов ИИ за сегодня.

    Args:
        user_id: Числовой ID пользователя
    """
    with _connect() as conn:
        conn.execute(
            "UPDATE users SET ai_used_today = 0 WHERE user_id = ?",
            (user_id,),
        )   # только счётчик; ai_last_reset не трогаем (не ломаем автосброс)


# --- get_total_notes_count: счётчик для карточки статистики панели.
def get_total_notes_count():
    """Считает общее количество заметок во всех таблицах."""
    with _connect() as conn:
        # COUNT(*) всегда возвращает ровно одну строку с одним числом.
        row = conn.execute("SELECT COUNT(*) AS c FROM notes").fetchone()
    return row["c"] if row else 0   # COUNT(*) всегда возвращает число


# --- count_ai_requests_total: всего запросов к ИИ за всё время (статистика).
def count_ai_requests_total():
    """Считает общее количество запросов к ИИ в логе."""
    with _connect() as conn:
        row = conn.execute("SELECT COUNT(*) AS c FROM ai_log").fetchone()
    return row["c"] if row else 0   # агрегат для карточки статистики


# --- get_all_join_requests: ВСЯ история заявок (включая обработанные).
# Для панели: видно, что с чем случилось. Свежие — сверху.
def get_all_join_requests(limit=100):
    """
    Получает все заявки на вступление в школу (с данными ученика),
    включая уже обработанные. Свежие — первыми.

    Args:
        limit: Максимум записей.

    Returns:
        Список словарей с данными заявок.
    """
    with _connect() as conn:
        # Без фильтра по статусу — видно всю историю заявок
        # (и pending, и approved, и rejected в одном списке).
        rows = conn.execute(
            "SELECT jr.*, u.full_name, u.name "
            "FROM join_requests jr "
            "LEFT JOIN users u ON jr.user_id = u.user_id "
            "ORDER BY jr.time DESC, jr.id DESC LIMIT ?",   # новые заявки сверху
            (limit,),
        ).fetchall()
    return [dict(r) for r in rows]