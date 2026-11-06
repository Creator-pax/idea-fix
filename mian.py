# ============================================================
# ГЛАВНЫЙ ФАЙЛ — mian.py
# ============================================================
# Точка входа в программу.
# Запуск: python mian.py
# ======================================    ======================
#
# Что здесь происходит по порядку:
#   1. Импортируем библиотеку maxo (фреймворк для ботов MAX) и экземпляры
#      бота/диспетчера из bot_instance.py;
#   2. Регистрируем middleware синхронизации БД (чтобы бот видел изменения
#      из панели модератора) и лог входящих обновлений;
#   3. Импортируем пакет handlers — это подключает все роутеры к диспетчеру;
#   4. По сигналу after_startup ставим команды бота (реально, «/start» и т.д.);
#   5. Запускаем поллинг (dp.run_polling) — блокирующий цикл приёма апдейтов.
# ============================================================

# --- Внешние библиотеки ---
import asyncio         # to_thread для выноса блокирующего файлового I/O
import dataclasses     # dataclasses.asdict — сериализация объекта апдейта
import json            # превращаем апдейт в читаемый JSON для отладки
import os              # построение абсолютного пути к файлу лога

from maxo import BaseMiddleware, Ctx            # базовый класс middleware и контекст
from maxo.routing.interfaces.middleware import NextMiddleware  # тип «следующий в цепочке»
from maxo.routing.signals.update import MaxoUpdate            # тип обёртки апдейта
from maxo.types import BotCommand               # описание команды бота

from bot_instance import bot, dp   # инстансы бота и диспетчера (создаются в bot_instance.py)

# ============================================
# СИНХРОНИЗАЦИЯ БАЗЫ С DESKTOP-ПАНЕЛЬЮ МОДЕРАТОРА
# ============================================
# Бот и панель модератора (moderator_app.py) — отдельные процессы, каждый
# держит свою копию bot_data.db в памяти. Перед обработкой каждого обновления
# перечитываем БД с диска, чтобы бот видел изменения, которые панель записала
# (подтверждение заявки, роль и т.д.), и наоборот.
import database as db   # db.reload() — теперь «пустышка», но вызов оставлен
                        # для единообразия и будущих версий


class SyncAndLogMiddleware(BaseMiddleware[MaxoUpdate[object]]):
    """
    Middleware на диспетчер: перед каждой обработкой апдейта
    перечитывает БД с диска и (опционально) пишет отладочный лог.

    В maxo не рекомендуется переопределять private-методы бота на лету
    (как это делалось в старом коде через bot._process_update).
    Вместо этого используется штатный механизм — outer middleware
    на dp.update.middleware.
    """

    def __init__(self, debug_log_file: str | None = None) -> None:
        # Храним путь к файлу лога (None — логирование выключено).
        self._debug_log_file = debug_log_file

    async def __call__(
        self,
        update: MaxoUpdate[object],   # обёртка апдейта от MAX
        ctx: Ctx,                     # контекст текущего запроса
        next: NextMiddleware[MaxoUpdate[object]],  # следующий middleware/роутер
    ) -> object:
        # Перечитываем БД с диска (синхронно, это дёшево для SQLite —
        # открыть соединение, прочитать, закрыть).
        db.reload()

        # Отладочная запись входящих обновлений (диагностика).
        if self._debug_log_file is not None:
            # Запись в файл — блокирующая операция, выносим её в поток,
            # чтобы не задерживать async-цикл обработки апдейтов.
            await asyncio.to_thread(self._dump_update, update)

        # Передаём управление дальше по цепочке middleware — ровно после
        # нашей работы. Без await next() апдейт не был бы обработан вообще.
        return await next(ctx)

    def _dump_update(self, update: MaxoUpdate[object]) -> None:
        """
        Пишет сырой внешний вид обновления в файл, чтобы было видно,
        как MAX отдаёт данные. Уберите блок, когда не нужно.

        update — это обёртка MaxoUpdate, а содержимое (например
        MessageCreated) лежит в update.update.
        """
        try:
            # dataclasses.asdict — рекурсивно превращает dataclass в словарь,
            # после чего json.dumps делает из него читаемую строку.
            raw = dataclasses.asdict(update.update)
            text = json.dumps(raw, ensure_ascii=False, default=str)
        except Exception:  # noqa: BLE001
            # Если сериализация не удалась (нестандартный объект внутри) —
            # хотя бы строковое представление.
            text = str(update.update)
        try:
            # Открываем лог в режиме append: дописываем в конец, а не стираем.
            with open(self._debug_log_file, "a", encoding="utf-8") as f:
                f.write("=== UPDATE ===\n")
                f.write(text)
                f.write("\n\n")
        except Exception:  # noqa: BLE001
            # Ошибку записи лога молча пропускаем — она не должна
            # ломать работу самого бота.
            pass


# Файл отладочного лога (как раньше bot.debug.log).
# Абсолютный путь строится от директории этого файла, чтобы лог всегда
# оказывался рядом с проектом (а не в текущей рабочей папке запуска).
_DEBUG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "bot.debug.log")

# Регистрируем middleware на диспетчер (до включения роутеров).
# outer — «внешний» слой: он выполняется раньше всех остальных middleware
# на каждом входящем обновлении.
dp.update.middleware.outer(SyncAndLogMiddleware(debug_log_file=_DEBUG_FILE))


# ============================================
# ИМПОРТ ОБРАБОТЧИКОВ
# ============================================
# Импортируем все обработчики: это подключает их Router'ы к диспетчеру
# (dp.include_router(...) выполняется в handlers/__init__.py).
# Импорт обязан идти ПОСЛЕ регистрации middleware — иначе роутеры
# зарегистрируются раньше, чем middleware-слой диспетчера.
# noqa: F401 — имя import делается ради побочного эффекта подключения;
# E402 — разрешаем импорт не в начале файла (после создания middleware).
import handlers  # noqa: F401, E402


# ============================================
# УСТАНОВКА КОМАНД БОТА
# ============================================
# Эти команды будут отображаться в интерфейсе MAX
# как подсказки при вводе /
#
# ВНИМАНИЕ: работает только ПОСЛЕ старта бота (нужен bot.state.info),
# поэтому вешаем на сигнал after_startup — он выполняется уже внутри
# цикла поллинга, когда бот готов.


@dp.after_startup()
async def setup_commands(update) -> None:
    """Регистрирует список команд бота (аналог bot.set_my_commands)."""
    # edit_my_commands заменяет «/команды», показываемые пользователям MAX.
    await bot.edit_my_commands(
        commands=[
            BotCommand(name="start", description="Регистрация и главное меню"),
            BotCommand(name="help", description="Справка по командам"),
            BotCommand(name="menu", description="Главное меню"),
            BotCommand(name="myid", description="Узнать свой ID"),
        ]
    )


# ============================================
# ЗАПУСК БОТА
# ============================================
# Секция выполняется только когда файл запущен напрямую (python mian.py),
# а не импортирован из другого модуля (__name__ == "__main__").

if __name__ == "__main__":
    print("Bot starting...")
    print("MAX School Helper v1.0.0")
    print("Press Ctrl+C to stop")
    print("-" * 40)

    try:
        # Запускаем поллинг (блокирующий вызов; стартовые сигналы,
        # в т.ч. регистрация команд, выполняются внутри).
        dp.run_polling(bot)
    except KeyboardInterrupt:
        # Обработка нажатия Ctrl+C — стандартный способ остановить бота.
        print("\n" + "-" * 40)
        print("Bot stopped. Goodbye!")
    except Exception as e:
        # Обработка непредвиденных ошибок: печатаем и пробрасываем,
        # чтобы процесс завершился с не-нулевым кодом (удобно для логов).
        print(f"\nError: {e}")
        raise