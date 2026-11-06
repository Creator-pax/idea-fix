# code.md — подробное объяснение кода проекта «MAX Школьный помощник»

> Версия: 1.1.0. Проект — max-бот для школы (фреймворк `maxo`, библиотека `customtkinter`)
> плюс десктоп-панель модератора. Это **индекс**: подробное объяснение каждого модуля
> разбито на отдельные файлы в папке [`code_docs/`](code_docs/). Внешние фреймворки
> `maxo/`, `maxgram/`, тесты и примеры сюда не входят.

## Содержание (по файлам)

| Файл | Что документирует |
|------|-------------------|
| [code_docs/code_01_config_root.md](code_docs/code_01_config_root.md) | Корневые файлы: `config.py`, `bot_instance.py`, `mian.py`, `reset_test_user.py` |
| [code_docs/code_02_handlers_core.md](code_docs/code_02_handlers_core.md) | `handlers/__init__.py`, `filters.py`, `states.py`, `user_utils.py`, `prefs.py`, `start.py` |
| [code_docs/code_03_menu_notes_registration.md](code_docs/code_03_menu_notes_registration.md) | `handlers/menu.py`, `notes.py`, `registration.py` |
| [code_docs/code_04_moderation_settings.md](code_docs/code_04_moderation_settings.md) | `handlers/moderation.py`, `settings.py` |
| [code_docs/code_05_ai_media.md](code_docs/code_05_ai_media.md) | `handlers/ai.py`, `media.py` |
| [code_docs/code_06_database.md](code_docs/code_06_database.md) | `database.py` — слой SQLite |
| [code_docs/code_07_moderator_app.md](code_docs/code_07_moderator_app.md) | `moderator_app.py` — десктоп-панель модератора |
| [code_docs/code_08_export.md](code_docs/code_08_export.md) | `handlers/export.py` — сохранение ответов ИИ в PDF/Word/RTF/PPTX |

## Обзор проекта

**Назначение.** Школьный бот: регистрация с модерацией, главное меню, ИИ-помощник
(вопрос по уроку, тест, расписание, домашка), заметки, настройки интерфейса,
раздел «Файлы и медиа» (OCR, PDF, распознавание речи, ключевые моменты видео),
инструменты модератора прямо в чате, сохранение ответов ИИ в файлы —
и десктоп-панель модератора.

**Структура проекта.**

```
MaxO/
├── config.py            # все константы/токены/лимиты
├── bot_instance.py      # bot и dp (объекты фреймворка maxo)
├── mian.py              # точка входа: middleware, импорт handlers, поллинг
├── database.py          # весь слой SQLite (bot_data.db)
├── moderator_app.py     # десктоп-панель модератора (customtkinter)
├── reset_test_user.py   # dev-утилита: сброс тестового аккаунта
├── code.md              # этот файл-индекс документации
├── code_docs/           # документация по модулям (отдельные .md)
├── bot_data.db          # база данных (создаётся автоматически)
├── handlers/            # все обработчики бота (по роутеру на подсистему)
├── maxo/                # внешний фреймворк (ставится: pip install ./maxo)
└── maxgram/             # внешний фреймворк (старьё, не используется)
```

**Запуск.**

```
# бот (из корня проекта, желательно в venv):
.\.venv\Scripts\python.exe mian.py

# панель модератора:
.\.venv\Scripts\python.exe moderator_app.py
```

**Как связаны процессы.**

- Бот и панель — **отдельные процессы**, оба читают один файл `bot_data.db` (SQLite).
- Бот держит `bot` (клиент MAX API) и `dp` (диспетчер апдейтов) — из `bot_instance.py`.
- `mian.py` навешивает на диспетчер middleware синхронизации БД (`db.reload()` перед каждым апдейтом),
  подключает все роутеры из `handlers/__init__.py` и запускает long-polling.
- Панель модератора использует те же `database.*` функции и `bot_instance.bot` для уведомлений.

---
