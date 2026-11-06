# ============================================================
# ОБРАБОТЧИК ИИ — handlers/ai.py
# ============================================================
# Этот файл отвечает за:
#   1. Интерфейс раздела «ИИ» и его подменю;
#   2. Отправку вопросов к LM Studio (локальный OpenAI-совместимый сервер);
#   3. Помощь с расписанием, тестами, домашкой, набросками текста, планами;
#   4. Просмотр логов ИИ (для учителей и модераторов).
#
# Показательные приёмы maxo в этом файле:
#   • CallbackPayload с префиксами для ai_student_logs_<user_id>;
#   • FSM-состояние AiStates.wait_question для текстовых запросов,
#     а сам режим (question/test/...) хранится в данных FSM;
#   • asyncio.to_thread — чтобы не блокировать event loop синхронными
#     сетевыми вызовами requests;
#   • вызов асинхронного notify_moderators из handlers/moderation.py.
#
# Поток данных в разделе ИИ:
#   1. Кнопка меню (payload "ai_question" и др.) в handle_ai_menu
#      запоминает mode в данных FSM и включает состояние wait_question.
#   2. Следующий текст пользователя попадает в handle_ai_text
#      (StateFilter(AiStates)) вместе с сохранённым mode.
#   3. _get_system_prompt собирает системный промпт (роль + стиль + mode),
#      _ask_ai ходит в LM Studio; весь сетевой вызов — в to_thread.
#   4. Ответ: +1 к лимиту, запись в лог, FSM сбрасывается, ответ уходит
#      текстом или файлом .txt (если длиннее AI_MAX_OUTPUT_LENGTH).
# ============================================================

# asyncio.to_thread — вызов синхронной функции в отдельном потоке, чтобы
# блокирующий сетевой запрос к LM Studio не «замораживал» весь event loop.
import asyncio

# requests — синхронный клиент HTTP; именно им ходим в локальный
# OpenAI-совместимый сервер LM Studio (http://localhost:1234/v1/...).
# Синхронность тут принципиальна: именно из-за неё вызов оборачивается
# в asyncio.to_thread (см. handle_ai_text и _ask_ai).
import requests
from maxo import Router
# AttachmentType — типы вложений в сообщении (фото/видео/аудио/файл).
# Используется, чтобы отличить «медиа без текста» от обычного текстового
# вопроса: картинки/файлы модель в этой сборке обрабатывать не умеет.
from maxo.enums import AttachmentType
from maxo.routing.filters import StateFilter
from maxo.types import MessageCallback, MessageCreated
from maxo.utils.builders import KeyboardBuilder
# BufferedInputFile — обёртка байтов для отправки файла (используем,
# когда ответ ИИ слишком длинный и мы отправляем его .txt-файлом).
from maxo.utils.upload_media import BufferedInputFile

# database — все операции с БД раздела ИИ:
#   * check_ai_limit / increment_ai_usage — суточный лимит запросов;
#   * save_ai_log — история запросов (вопрос+ответ) для панелей;
#   * save_ai_error — лог ошибок обращения к серверу ИИ;
#   * get_user / get_user_field / is_moderator / is_student — права и профиль.
import database as db
# Набор параметров ИИ из config.py:
#   AI_API_KEY — ключ для сервера (в LM Studio необязателен, но проверяется);
#   AI_MODEL — имя модели для поля "model" в запросе;
#   AI_MAX_INPUT_LENGTH — максимум символов вопроса пользователя;
#   AI_MAX_OUTPUT_LENGTH — порог: длиннее ответ уходит файлом;
#   AI_FOLLOWUP_DEFAULT / AI_EXAMPLES_DEFAULT / AI_DETAIL_DEFAULT —
#   предпочтения «по умолчанию» для пользователей без своих настроек.
from config import (
    AI_API_KEY,
    AI_DETAIL_DEFAULT,
    AI_EXAMPLES_DEFAULT,
    AI_FOLLOWUP_DEFAULT,
    AI_MAX_INPUT_LENGTH,
    AI_MODEL,
    AI_MAX_OUTPUT_LENGTH,
    AI_MAX_TOKENS,
    AI_TIMEOUT_SECONDS,
)

from handlers.filters import CallbackPayload
from handlers.markdown import markdown_to_plain
from handlers.states import AiStates
from handlers.user_utils import get_user_id

router: Router = Router(name="ai")

# ============================================================
# КЛАВИАТУРЫ РАЗДЕЛА ИИ
# ============================================================
# Клавиатура строится из двух списков кортежей (текст_кнопки, payload).
# Такой подход (данные → кнопки) позволяет добавлять/убирать инструменты
# раздела правкой одного списка, не касаясь цикла построения.

# Базовые кнопки (доступны всем ролям).
# Формат кортежа: (текст кнопки, payload для колбэка).
# Payload — это внутренний идентификатор кнопки, по которому роутер
# решает, какой режим ИИ запустить (см. mode_for_payload ниже).
_AI_BASE_KEYS = [
    ("❓ Задать вопрос по уроку", "ai_question"),
    ("📅 Составить расписание дня", "ai_schedule"),
    ("📝 Создать тест по теме", "ai_test"),
    ("📚 Помочь с домашкой", "ai_homework"),
]

# Кнопки только для учителей и модераторов.
# Проверка правами происходит ДВАЖДЫ: при построении клавиатуры (кнопки
# просто не добавляются «студенту») и при нажатии (страховка от подделки
# payload) — см. handle_ai_menu.
_AI_TEACHER_KEYS = [
    ("📝 Набросок текста", "ai_presentation"),
    ("📖 План урока по теме", "ai_topic"),
]


def get_ai_menu_keyboard(user_id: int) -> KeyboardBuilder:
    """
    Создаёт клавиатуру раздела ИИ с учётом роли пользователя.

    Учителя и модераторы дополнительно видят инструменты
    «набросок текста» и «план урока».

    Args:
        user_id: Числовой ID пользователя.

    Returns:
        KeyboardBuilder (перед передачей в facade вызвать .build()).
    """
    # Собираем список кнопок (текст, payload) в одну переменную rows,
    # чтобы построение клавиатуры было линейным и легко читалось.
    # Копируем базовые кнопки (list() по кортежу — чтобы не менять оригинал).
    rows = list(_AI_BASE_KEYS)

    # Роль учителя/модератора даёт доступ к дополнительным ИИ-инструментам
    # (наброски текста и планы уроков). Проверяем и по роли в БД, и по списку
    # модераторов из config (см. db.is_moderator).
    role = db.get_user_field(user_id, "role")
    if role in ("teacher", "moderator") or db.is_moderator(user_id):
        # Учителю/модератору добавляем «учительские» кнопки в конец списка.
        # rows.extend деструктивно догружает список — порядок сохраняется:
        # сначала все базовые кнопки, потом учительские.
        rows.extend(_AI_TEACHER_KEYS)

    # В самом низу всегда кнопка возврата в главное меню — это
    # одинаково для всех ролей. Payload "menu_main" обработает menu.py.
    rows.append(("← Назад в меню", "menu_main"))

    kb = KeyboardBuilder()
    # Проходим по всем строкам (текст, payload) и превращаем каждую
    # в кнопку add_callback — так клавиатура строится из данных, а не вручную.
    # Данные-ориентированный цикл: если в списке появится новая кнопка,
    # она автоматически попадёт и в клавиатуру, и в роутер (по payload).
    for text, payload in rows:
        kb.add_callback(text=text, payload=payload)
    # По одной кнопке в ряд, чтобы длинный текст кнопок не обрезался.
    # Почему это важно: KeyboardBuilder по умолчанию кладёт до 7 кнопок
    # в ряд, а MAX при этом режет текст каждой кнопки до 3-5 символов
    # (см. большой комментарий в handlers/menu.py). adjust(1) раскладывает
    # все кнопки по одной в строку — тогда видно до 20 символов.
    return kb.adjust(1)


# ============================================================
# КОЛБЭКИ РАЗДЕЛА ИИ
# ============================================================
# Принимаем выбор режима ИИ, логи и префикс ai_student_logs_<id>.
#
# Один обработчик handle_ai_menu покрывает ВСЕ нажатия раздела:
#   • меню (menu_ai);
#   • запуск режимов (ai_question / ai_schedule / ai_test / ai_homework /
#     ai_presentation / ai_topic) — ветка mode_for_payload;
#   • логи (menu_ai_logs), список учеников (ai_students_history);
#   • персональную историю ученика (ai_student_logs_<id>) и свою (ai_my_history).
# Внутри — большая цепочка if по значению payload.

@router.message_callback(
    CallbackPayload(
        "menu_ai", "ai_question", "ai_schedule", "ai_test", "ai_homework",
        "ai_presentation", "ai_topic", "menu_ai_logs", "ai_students_history",
        "ai_my_history",
        # prefixes=("ai_student_logs_",) — фильтр также пропустит любой
        # payload, начинающийся с этого префикса. Так мы ловим кнопки
        # «📊 История ученика <имя>», у которых payload динамический:
        # ai_student_logs_<числовой_id>. Число в конец подставляется кодом.
        prefixes=("ai_student_logs_",),
    )
)
async def handle_ai_menu(update: MessageCallback, fsm_context) -> None:
    """Обрабатывает кнопки раздела «ИИ»."""
    # Единый обработчик для ВСЕХ кнопок раздела: списки в CallbackPayload
    # декоратора + префикс ai_student_logs_. Внутри — большая цепочка if по
    # payload: меню / режимы / логи / история учеников.
    # Типы аргументов: update — MessageCallback (нажатие кнопки),
    # fsm_context — FSMContext (состояние и временные данные пользователя).
    # payload — внутренний идентификатор нажатой кнопки (например "ai_question").
    payload = update.payload
    # user_id — кто нажал; от него зависят права и лимиты.
    # get_user_id извлекает числовой ID из объекта колбэка.
    user_id = get_user_id(update)

    # Единый барьер доступа для всего раздела ИИ: пользователь должен
    # существовать, быть одобрен модератором и не находиться в блоке.
    # Если хотя бы одно условие не выполнено — молча игнорируем нажатие.
    # Та же проверка повторяется и в handle_ai_text (текстовые запросы).
    if not db.user_exists(user_id) or not db.is_approved(user_id) or db.is_blocked(user_id):
        return

    # ============================================================
    # МЕНЮ ИИ
    # ============================================================
    if payload == "menu_ai":
        # Вход в раздел: сбрасываем FSM, чтобы обнулить возможный прошлый
        # режим (mode) и случайное состояние, оставшееся от прошлого запроса.
        # clear() удаляет и состояние wait_question, и данные (mode).
        await fsm_context.clear()

        # Проверяем лимит запросов на сегодня.
        # check_ai_limit сверяет ai_usage с суточной нормой ai_daily_limit
        # (по умолчанию 50, см. database.py).
        if not db.check_ai_limit(user_id):
            # Лимит исчерпан: показываем «галочку» Лимит и сообщение с
            # клавиатурой меню ИИ (клавиатуру оставляем, чтобы можно было
            # просто уйти назад — лимит всё равно не даст ввести вопрос).
            await update.callback_answer(text="Лимит")
            await update.edit_message(
                text="❌ Лимит запросов к ИИ на сегодня исчерпан!\n\n"
                     "Попробуй завтра или попроси учителя увеличить лимит.",
                keyboard=get_ai_menu_keyboard(user_id).build(),
            )
            return

        # Лимит есть — показываем текущее расходование лимита.
        # get_ai_usage возвращает сколько запросов уже сделано сегодня;
        # норму берём из поля пользователя ai_daily_limit (50 по умолчанию).
        # used/limit — просто числа для счётчика в тексте меню.
        used = db.get_ai_usage(user_id)
        limit = db.get_user_field(user_id, "ai_daily_limit") or 50

        await update.callback_answer(text="ИИ")
        # Информируем о счётчике «использовано/лимит» и рисуем меню.
        # get_ai_menu_keyboard учитывает роль: у учителя кнопок больше.
        await update.edit_message(
            text=f"🤖 Раздел ИИ\n\n"
                 f"Использовано запросов сегодня: {used}/{limit}\n\n"
                 "Выбери, чем я могу помочь:",
            keyboard=get_ai_menu_keyboard(user_id).build(),
        )
        return

    # ============================================================
    # ВСПОМОГАТЕЛЬНОЕ: старт текстового ввода для режима
    # ============================================================
    # Многие кнопки просто переводят пользователя в состояние
    # «ждём текст» и запоминают режим (question/schedule/test/...).
    # Словарь mode_for_payload сопоставляет payload кнопки с кортежем:
    #   (ключ режима mode, заголовок для edit_message, подсказка hint).
    #
    # Ключ mode — СЛУЖЕБНАЯ строка, по которой потом:
    #   - выбирается системный промпт в _get_system_prompt;
    #   - подписывается лог (save_ai_log);
    #   - выбирается текст кнопки «продолжить» (_MODE_META).
    # title — заголовок, который видит пользователь; hint — инструкция,
    # что именно написать ИИ в следующем сообщении.
    # Ключ mode позже сохраняется в FSM и определяет системный промпт
    # (что именно должен сделать ИИ) в _get_system_prompt.
    mode_for_payload = {
        # ai_question — базовый режим объяснения темы/вопроса по уроку.
        # mode="question" используется по умолчанию, если mode не задан.
        "ai_question": ("question", "❓ Задай свой вопрос по уроку.",
                        "Напиши что тебе непонятно, и я объясню простыми словами!\n\n"
                        "Например:\n• Что такое квадратное уравнение?\n• Как работает фотосинтез?"),
        # ai_schedule — помощь в составлении расписания дня: mode="schedule".
        "ai_schedule": ("schedule", "📅 Составим расписание дня!",
                        "Напиши:\n1. Во сколько ты просыпаешься?\n"
                        "2. Что нужно сделать за день? (уроки, кружки, отдых)\n"
                        "3. Во сколько ложишься спать?"),
        # ai_test — генерация теста по теме: mode="test". Кнопка спросит
        # тему и число вопросов, а ИИ отдаст тест с ответами и объяснением.
        "ai_test": ("test", "📝 Создам тест по теме!",
                    "Напиши тему и количество вопросов.\n\n"
                    "Например:\n• Математика 8 класс, квадратные уравнения, 5 вопросов"),
        # ai_homework — помощь с домашним заданием: mode="homework".
        "ai_homework": ("homework", "📚 Помогу с домашним заданием!",
                        "Напиши задание, и я помогу его решить.\nЯ объясню пошагово."),
        # ai_presentation — «набросок текста» ДЛЯ УЧИТЕЛЕЙ: mode="presentation".
        # Доступ проверяется в handle_ai_menu отдельно (см. ниже if payload).
        "ai_presentation": ("presentation", "📝 Набросок текста по теме!",
                            "Напиши тему, класс и для чего нужен текст "
                            "(доклад, статья, сценарий урока), и я составлю "
                            "качественный продуманный набросок текста."),
        # ai_topic — «план урока» ДЛЯ УЧИТЕЛЕЙ: mode="topic".
        "ai_topic": ("topic", "📖 Составлю план урока по теме!",
                     "Напиши предмет, класс и тему, и я составлю план урока."),
    }

    if payload in mode_for_payload:
        # Распаковываем: mode — служебный ключ режима, title — заголовок
        # сообщения, hint — подсказка, что именно надо написать.
        # Кортеж гарантированно из трёх элементов, поэтому распаковка не боится.
        mode, title, hint = mode_for_payload[payload]

        # Функции «набросок текста» и «план урока» только для учителей.
        # Пользователь с ролью student (или вообще не учитель) получает
        # отказ, даже если как-то нажмёт такой payload.
        # Это вторая линия защиты: первая — при построении клавиатуры
        # (см. get_ai_menu_keyboard), вторая — вот здесь, на моменте нажатия.
        if payload in ("ai_presentation", "ai_topic") and not _is_teacher_or_moderator(user_id):
            await update.callback_answer(text="❌")
            await update.edit_message("❌ Эта функция доступна только учителям.")
            return

        # Повторная проверка лимита (на случай, если он кончился во время
        # работы в других разделах) — не даём даже начать ввод вопроса.
        # Между меню и нажатием модератор мог изменить норму.
        if not db.check_ai_limit(user_id):
            await update.callback_answer(text="Лимит")
            await update.edit_message("❌ Лимит исчерпан!")
            return

        # Переводим в состояние «ждём вопрос к ИИ» и запоминаем режим.
        # clear() — сбросить «хвосты» прошлых данных FSM; затем update_data
        # кладёт mode (question/schedule/test/homework/presentation/topic),
        # а set_state переводит пользователя в AiStates.wait_question —
        # дальше его текст поймает handle_ai_text (StateFilter(AiStates)).
        await fsm_context.clear()
        await fsm_context.update_data(mode=mode)
        await fsm_context.set_state(AiStates.wait_question)

        kb = KeyboardBuilder()
        # Во время ввода даём только одну кнопку — «← Назад», чтобы можно
        # было вернуться в меню ИИ, не ломая состояние (payload "menu_ai"
        # опять попадёт в этот же обработчик и очистит FSM).
        kb.add_callback(text="← Назад", payload="menu_ai")

        await update.callback_answer(text="Ввод")
        # Меняем текущее сообщение: заголовок + подсказка + кнопка «Назад».
        await update.edit_message(
            text=f"{title}\n\n{hint}",
            keyboard=kb.build(),
        )
        return

    # ============================================================
    # ЛОГИ ИИ (для учителей и модераторов)
    # ============================================================
    # Раздел просмотра ЛОГОВ: кто и что спрашивал у ИИ. Истории запросов
    # сохраняются в БД функцией save_ai_log (см. handle_ai_text ниже).
    if payload == "menu_ai_logs":
        # Просмотр общих логов — только учителя и модераторы.
        # Ученику лог чужих запросов недоступен по определению.
        role = db.get_user_field(user_id, "role")
        if role not in ("teacher", "moderator"):
            await update.callback_answer(text="❌")
            await update.edit_message("❌ Эта функция доступна только учителям и модераторам.")
            return

        # get_ai_logs(limit=10) — последние 10 запросов от ВСЕХ пользователей.
        # Это общий лог по всему боту; записи уже отсортированы по времени.
        logs = db.get_ai_logs(limit=10)
        if not logs:
            # Логов нет — короткое сообщение и кнопка назад в главное меню.
            # Пустой список возможен на старте, пока ещё никто не ходил к ИИ.
            kb = KeyboardBuilder()
            kb.add_callback(text="← Назад в меню", payload="menu_main")
            await update.callback_answer(text="Логи")
            await update.edit_message("📊 Пока нет запросов к ИИ.", keyboard=kb.build())
            return

        # Формируем текст: берём последние 5 логов (срез [-5:]), кроме вопроса
        # — только первые 100 символов, чтобы строка в списке не разрасталась.
        # enumerate(..., 1) даёт нумерацию с 1, а не с 0.
        logs_text = "📊 Последние запросы к ИИ:\n\n"
        for i, log in enumerate(logs[-5:], 1):
            # Имя пользователя из лога (может отсутствовать → «Неизвестно»).
            # .get с значением по умолчанию защищает от отсутствующего ключа.
            user_name = log.get("user_name", "Неизвестно")
            # Вопрос обрезаем до 100 символов — этого хватит для превью.
            # Многоточие добавляет только превью — сам лог остаётся полным.
            question = log.get("question", "")[:100]
            # Каждая запись списка: номер, имя, обрезанный вопрос.
            logs_text += f"{i}. 👤 {user_name}\n   ❓ {question}...\n\n"

        kb = KeyboardBuilder()
        # Кнопка «История учеников» ведёт к списку учеников (см. ниже),
        # а «← Назад в меню» возвращает в главное меню бота.
        kb.add_callback(text="👨‍🏫 История учеников", payload="ai_students_history")
        kb.add_callback(text="← Назад в меню", payload="menu_main")

        await update.callback_answer(text="Логи")
        await update.edit_message(logs_text, keyboard=kb.build())
        return

    # ============================================================
    # ИСТОРИЯ УЧЕНИКОВ (учитель/модератор)
    # ============================================================
    # Учитель выбирает ученика из списка, чтобы посмотреть историю его
    # запросов к ИИ. Список строится динамически из базы: по числу учеников
    # генерируются inline-кнопки с payload ai_student_logs_<id>.
    if payload == "ai_students_history":
        # Доступ к списку учеников — снова только учителя/модераторы.
        # Студентам такой список не показываем даже косвенно.
        if not _is_teacher_or_moderator(user_id):
            await update.callback_answer(text="❌")
            await update.edit_message("❌ Эта функция доступна только учителям.")
            return

        # Перебираем всех пользователей БД и оставляем только одобренных
        # учеников (is_student) — учитель видит тех, у кого можно глянуть
        # историю запросов к ИИ.
        # Это list comprehension: по всем ID записи ищет тех, кто ученик
        # (is_student) И одобрен (is_approved) — оба условия в одном выражении.
        all_ids = db.get_all_user_ids()
        students = [uid for uid in all_ids if db.is_student(uid) and db.is_approved(uid)]

        if not students:
            # Нет ни одного ученика — сообщение и кнопка назад в меню.
            # Данных для списка нет — рано или поздно заявки появятся.
            await update.callback_answer(text="Пусто")
            await update.edit_message(
                "👨‍🏫 Пока нет ни одного ученика.",
                keyboard=_back_keyboard("menu_main").build(),
            )
            return

        # Строим клавиатуру из первых 10 учеников: текст кнопки — имя,
        # а payload — динамический ai_student_logs_<id>, чтобы ниже мы могли
        # вытащить id конкретного ученика. Счётчик shown ограничивает число.
        # Заголовок сообщения формируется заранее (text), кнопки добавляются
        # далее в цикле по первым 10 ученикам (students[:10]).
        text = f"👨‍🏫 Ученики ({len(students)}):\n\nВыбери ученика, чтобы посмотреть его историю:\n"
        kb = KeyboardBuilder()
        shown = 0
        for uid in students[:10]:
            # Достаём запись пользователя (dict из БД).
            user = db.get_user(uid)
            # Имя из full_name, иначе из name; если нет и его — заглушка "ID N".
            # (user or {}) защищает от None: пустой dict .get вернёт дефолт.
            name = (user or {}).get("full_name") or (user or {}).get("name", f"ID {uid}")
            # Длинные имена обрезаем до 28 символов — иначе кнопка скажется
            # (MAX режет текст кнопки, и имя превратится в кашу).
            if len(name) > 28:
                name = name[:28] + "…"
            # Кнопка с именем ученика и payload ai_student_logs_<id>.
            # Именно <id> потом парсится int(payload.split("_")[-1]).
            kb.add_callback(text=f"👤 {name}", payload=f"ai_student_logs_{uid}")
            shown += 1

        # Если учеников больше 10 — дописываем в текст подсказку «и ещё N»,
        # чтобы учитель понимал, что список показан не полностью.
        if len(students) > 10:
            text += f"\n... и ещё {len(students) - 10} учеников"

        # Кнопка возврата в главное меню внизу списка.
        kb.add_callback(text="← Назад в меню", payload="menu_main")

        # adjust(1) — по одной кнопке (ученику) в ряд. Иначе KeyboardBuilder
        # упаковал бы до 10 кнопок с именами учеников по 7 в ряд, и MAX
        # обрезал бы имена до 3 символов («криво»).
        await update.callback_answer(text="Ученики")
        await update.edit_message(text, keyboard=kb.adjust(1).build())
        return

    # ============================================================
    # ИСТОРИЯ КОНКРЕТНОГО УЧЕНИКА
    # ============================================================
    if payload.startswith("ai_student_logs_"):
        # Снова проверяем права — payload с префиксом очень легко подделать,
        # поэтому доступ строго по роли.
        if not _is_teacher_or_moderator(user_id):
            await update.callback_answer(text="❌")
            await update.edit_message("❌ Нет доступа.")
            return

        try:
            # Извлекаем id ученика из payload вида "ai_student_logs_123":
            # берём последний кусок после "_" и превращаем в число.
            # payload.split("_") даёт ["ai", "student", "logs", "123"].
            target_id = int(payload.split("_")[-1])
        except ValueError:
            # Если в хвосте не число (сломанный/подделанный payload) — молча
            # выходим, ничего пользователю не говорим.
            return

        # Проверяем, что ученик с таким id вообще существует в БД.
        # Права мы уже проверили выше, но самого ученика могли удалить —
        # вот для этого и нужна проверка существования.
        if not db.user_exists(target_id):
            await update.callback_answer(text="❌")
            await update.edit_message("❌ Ученик не найден.")
            return

        # Достаём запись ученика для читаемого имени и его лог запросов
        # (get_ai_logs_for_user вернёт до 20 последних записей).
        # Имя нужно для заголовка «История запросов ученика «<имя>»:».
        user = db.get_user(target_id)
        name = (user or {}).get("full_name") or (user or {}).get("name", f"ID {target_id}")
        logs = db.get_ai_logs_for_user(target_id, limit=20)

        if not logs:
            # Ученик ни разу не обращался к ИИ — история пуста, назад к списку.
            await update.callback_answer(text="Пусто")
            await update.edit_message(
                f"👤 {name} — история пуста.\n\n"
                "Ученик ещё не обращался к ИИ.",
                keyboard=_back_keyboard("ai_students_history").build(),
            )
            return

        # Собираем итоговый текст: заголовок, отформатированные логи
        # (_format_logs) и приписка, что показаны последние 20 запросов.
        # _format_logs вернёт уже читаемый список с номерами и временами.
        text = f"📊 История запросов ученика «{name}»:\n\n"
        text += _format_logs(logs)
        text += "\n\n_Показано последних 20 запросов_"

        await update.callback_answer(text="История")
        # Кнопка назад ведёт к списку учеников (ai_students_history).
        # _back_keyboard принимает payload — имя колбэка, куда вернуться.
        await update.edit_message(
            text,
            keyboard=_back_keyboard("ai_students_history").build(),
        )
        return

    # ============================================================
    # МОЯ ИСТОРИЯ (ученик)
    # ============================================================
    # Аналог «истории ученика», но для САМОГО пользователя: прав не нужно,
    # id берётся прямо из текущего апдейта.
    if payload == "ai_my_history":
        # Ученик смотрит СВОИ последние 20 запросов к ИИ (id берём из апдейта).
        logs = db.get_ai_logs_for_user(user_id, limit=20)
        if not logs:
            # Запросов ещё не было — ободряем и подсказываем разделы.
            await update.callback_answer(text="Пусто")
            await update.edit_message(
                "📊 У тебя пока нет запросов к ИИ.\n\n"
                "Воспользуйся разделами «Вопрос по уроку», «Тест» и другими!",
                keyboard=_back_keyboard("menu_main").build(),
            )
            return

        # Собираем историю пользователя в читаемый текст.
        # Структура сообщения та же, что и у истории ученика.
        text = "📊 Твоя история запросов к ИИ:\n\n"
        text += _format_logs(logs)
        text += "\n\n_Показано последних 20 запросов_"

        await update.callback_answer(text="История")
        # Назад — в главное меню (payload menu_main уходит в menu.py).
        await update.edit_message(
            text,
            keyboard=_back_keyboard("menu_main").build(),
        )


# ============================================================
# ТЕКСТОВЫЕ СООБЩЕНИЯ ДЛЯ ИИ
# ============================================================
# Этот обработчик — «ядро» раздела: тут формируется запрос к LM Studio и
# обрабатывается ответ. Его фильтр StateFilter(AiStates) ограничивает вход:
# текст принимается только тогда, когда установлено состояние wait_question
# (т.е. пользователь нажал одну из кнопок режимов в меню ИИ).

@router.message_created(StateFilter(AiStates))
async def handle_ai_text(update: MessageCreated, fsm_context) -> None:
    """
    Обрабатывает текстовый вопрос к ИИ.

    StateFilter(AiStates) — только когда пользователь в состоянии
    «ждём вопрос к ИИ». Режим (mode) берём из данных FSM.
    """
    user_id = get_user_id(update)
    text = update.text or ""
    if not user_id:
        return   # нет ID — обрабатывать нечего, выходим

    # Единый барьер доступа для текстовых запросов: пользователь существует,
    # одобрен и не заблокирован — иначе выходим молча.
    if not db.user_exists(user_id) or not db.is_approved(user_id) or db.is_blocked(user_id):
        return

    # Пока присылают фото/файл вместо текста — вежливо просим текст вопроса.
    # Модель в этой сборке отвечает по тексту, а картинку распознать не может.
    body = update.message.body
    # Отбираем вложения типов IMAGE/VIDEO/AUDIO/FILE из тела сообщения —
    # это «медиа», которое модель обработать пока не умеет.
    # Генератор с фильтром a.type in (...): пропускаем только «тяжёлые» типы.
    media_attachments = [
        a
        for a in (body.attachments or [])
        if a.type in (AttachmentType.IMAGE, AttachmentType.VIDEO,
                      AttachmentType.AUDIO, AttachmentType.FILE)
    ]
    # Если пришло медиа, а текста нет (или текст пустой) — просим написать
    # вопрос текстом. Кнопка «← Назад» ведёт в меню ИИ.
    # Условие «только медиа без текста»: если есть и текст, и фото — текст
    # обрабатывается, картинка просто игнорируется.
    if media_attachments and not text:
        kb = KeyboardBuilder()
        kb.add_callback(text="← Назад", payload="menu_ai")
        await update.answer_text(
            "🖼 Я пока отвечаю только на текстовые вопросы!\n\n"
            "Напиши свой вопрос текстом, и я помогу. "
            "Чтобы распознать текст с фото, открой «📂 Файлы и медиа» → "
            "«🖼 Текст с фото» — там OCR прочитает картинку.",
            keyboard=kb.build(),
        )
        return

    # Ограничение длины вопроса.
    if len(text) > AI_MAX_INPUT_LENGTH:
        # Слишком длинный вопрос — просим сократить; состояние FSM при этом
        # сохраняется, пользователь остаётся «в режиме ожидания вопроса».
        # Состояние НЕ сбрасываем: юзер может просто укоротить текст и
        # прислать его следующим сообщением (опять попадёт сюда).
        await update.answer_text(
            f"❌ Слишком длинный вопрос! ({len(text)} символов)\n"
            f"Максимум: {AI_MAX_INPUT_LENGTH} символов.\n"
            "Сократи свой вопрос:"
        )
        return

    # Режим хранится в FSM (по умолчанию — question).
    # get_data берёт все временные данные: режим mode был положен кнопкой
    # меню через fsm_context.update_data(mode=mode). Если вдруг отсутствует
    # (например, FSM очищен) — считаем обычным вопросом.
    data = await fsm_context.get_data()
    mode = data.get("mode", "question")

    # Проверяем лимит ещё раз (мог исчерпаться пока пользователь печатал).
    # Если лимита нет — сбрасываем состояние (пользователь больше не «в
    # режиме ввода») и сообщаем об исчерпании.
    if not db.check_ai_limit(user_id):
        await fsm_context.clear()
        await update.answer_text("❌ Лимит запросов к ИИ на сегодня исчерпан!")
        return

    # «Думаю...» — промежуточное сообщение.
    # Отправляем сразу, потому что запрос к LM Studio занимает время и
    # пользователь должен видеть, что бот жив и обрабатывает вопрос.
    # Ответ заменит это сообщение позже через edit_message/answer_text.
    await update.answer_text("🤔 Думаю...")

    # Формируем системный промпт и отправляем запрос. Сетевой вызов
    # requests — синхронный, поэтому выносим в отдельный поток.
    # _get_system_prompt собирает инструкции ИИ с учётом роли, настроек
    # стиля (simple/detailed/fun), интересов и режима (mode).
    system_prompt = _get_system_prompt(user_id, mode)
    # asyncio.to_thread(_ask_ai, ...) запускает блокирующий requests.post
    # в отдельном потоке и НЕ блокирует event loop — бот продолжает
    # отвечать другим пользователям, пока ИИ «думает».
    # _ask_ai вернёт кортеж (answer, ai_error): при сбое answer будет None.
    answer, ai_error = await asyncio.to_thread(_ask_ai, system_prompt, text)

    if answer is None:
        # Ответа нет — запрос завершился ошибкой.
        # 1) Записываем ошибку в лог для панели модератора
        #    (db.save_ai_error: user_id, режим, текст ошибки).
        try:
            db.save_ai_error(user_id, mode, ai_error or "Неизвестная ошибка ИИ")
        except Exception:  # noqa: BLE001
            # Логирование ошибки не критично — не роняем обработчик.
            pass

        # 2) Уведомляем модераторов о сбое (асинхронно).
        # notify_moderators из handlers/moderation рассылает личные сообщения
        # всем модераторам — импорт локальный, чтобы избежать цикла.
        # Импорт делаем здесь: handlers/moderation может импортировать наш
        # модуль, а локальный импорт ломает потенциальный цикл.
        try:
            from handlers.moderation import notify_moderators
            await notify_moderators(
                f"⚠️ Сбой ИИ у пользователя {user_id} "
                f"(режим: {mode}).\nПричина: {ai_error or 'неизвестная ошибка'}"
            )
        except Exception:  # noqa: BLE001
            # Если уведомить не вышло — продолжаем, это не фатально.
            pass

        # 3) Пользователю показываем понятное сообщение с причиной.
        # Причину берём из ai_error (если ИИ вернул) или заглушку.
        await update.answer_text(
            "❌ Не удалось получить ответ от ИИ.\n\n"
            f"ℹ️ Причина: {ai_error or 'неизвестная ошибка'}\n\n"
            "Попробуй ещё раз позже или задай другой вопрос.\n"
            "О проблеме уже уведомлены ответственные."
        )
        # Состояние не сбрасываем — пользователь может повторить ввод.
        # Он остаётся в AiStates.wait_question, и следующее сообщение снова
        # попадёт в этот обработчик.
        return

    # Успех: увеличиваем счётчик, сохраняем лог, сбрасываем состояние.
    # increment_ai_usage — +1 к дневному расходу лимита;
    # save_ai_log — история запроса (вопрос + ответ) для панелей;
    # fsm_context.clear() — выходим из режима ввода, FSM больше не нужен.
    db.increment_ai_usage(user_id)
    db.save_ai_log(user_id, text, answer)
    await fsm_context.clear()

    kb = KeyboardBuilder()
    # После ответа предлагаем «продолжить» в том же режиме (кнопка зависит
    # от mode: например для question — «Задать ещё вопрос») и выход в меню.
    # _mode_continue_label/_mode_continue_callback берут текст и payload из
    # словаря _MODE_META по текущему режиму.
    # Кнопка «Сохранить в файл» ведёт в мастер экспорта (handlers/export.py):
    # PDF / Word / RTF / PowerPoint с фоном, шрифтом и подписью «КУБ-СКРИПТ».
    kb.add_callback(text=_mode_continue_label(mode), payload=_mode_continue_callback(mode))
    kb.add_callback(text="📄 Сохранить в файл", payload="export_start")
    kb.add_callback(text="← Назад в меню", payload="menu_main")

    # Длинный ответ не урезаем, а отправляем файлом — так текст не теряется.
    # Порог AI_MAX_OUTPUT_LENGTH: если ответ длиннее — кодируем его в UTF-8
    # и шлём как вложение .txt через BufferedInputFile. В файл пишем текст
    # БЕЗ markdown-пометок (###, **), чтобы он читался как обычный текст.
    if len(answer) > AI_MAX_OUTPUT_LENGTH:
        file_data = markdown_to_plain(answer).encode("utf-8")
        await update.send_media(
            media=BufferedInputFile.file(file_data, "Ответ_ИИ.txt"),
            text="📄 Ответ получился длинным — сохранил его в файл!",
            keyboard=kb.build(),
        )
        return

    # Обычный (не слишком длинный) ответ — просто отправляем текстом
    # вместе с клавиатурой «продолжить / назад в меню». Markdown-пометки
    # модели (###, **, списки) убираем — пользователь видит чистый текст.
    await update.answer_text(markdown_to_plain(answer), keyboard=kb.build())


# ============================================================
# ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ
# ============================================================
# Тут собраны помощники раздела ИИ:
#   * _is_teacher_or_moderator — проверка прав (учитель/модератор);
#   * _back_keyboard — простейшая клавиатура «Назад»;
#   * _MODE_META и _mode_* — справочник режимов для кнопки «продолжить»;
#   * _format_logs — лог запросов → читаемый текст;
#   * _get_system_prompt — сборка системного промпта под пользователя;
#   * _ask_ai — непосредственный вызов LM Studio (HTML over HTTP).

def _is_teacher_or_moderator(user_id: int) -> bool:
    """True, если пользователь — учитель или модератор."""
    # Сначала смотрим роль из БД — это основной источник прав.
    role = db.get_user_field(user_id, "role")
    if role in ("teacher", "moderator"):
        return True
    try:
        # Дополнительно проверяем «статический» список модераторов из config
        # (db.is_moderator) — на случай, если роль в БД не задана.
        # db.is_moderator смотрит по списку ID модераторов из settings.
        return db.is_moderator(user_id)
    except Exception:  # noqa: BLE001
        # Если проверка упала (нет пользователя и т.п.) — считаем, что прав нет.
        # Безопасный отказ: упавшая проверка = «доступ запрещён».
        return False


def _back_keyboard(callback: str) -> KeyboardBuilder:
    """Простая клавиатура «Назад» с указанным callback."""
    # Переиспользуется во многих местах (логи, история, списки): один
    # помощник вместо десятка почти одинаковых конструкторов.
    kb = KeyboardBuilder()
    # Одна кнопка «← Назад», payload — куда вернуться (передаётся строкой,
    # например "menu_main" или "ai_students_history").
    kb.add_callback(text="← Назад", payload=callback)
    return kb


# Справочник режимов для кнопки «продолжить» после ответа ИИ:
#   ключ — mode (тот же, что кладём в FSM); значение — (текст кнопки, payload).
# Payload кнопки совпадает с payload исходной кнопки меню, поэтому нажатие
# снова поведёт пользователя в тот же режим через handle_ai_menu.
# Словарь — единый источник правды и для текста, и для payload: если добавить
# новый режим, достаточно завести запись здесь, а label/callback сами подхватятся.
_MODE_META = {
    "question": ("❓ Задать ещё вопрос", "ai_question"),
    "schedule": ("📅 Составить ещё расписание", "ai_schedule"),
    "test": ("📝 Создать ещё тест", "ai_test"),
    "homework": ("📚 Помощь с другой домашкой", "ai_homework"),
    "presentation": ("📝 Ещё один набросок текста", "ai_presentation"),
    "topic": ("📖 Составить ещё план урока", "ai_topic"),
}


def _mode_continue_label(mode: str) -> str:
    """Текст кнопки «продолжить» для режима ИИ."""
    # Безопасный доступ: если mode вдруг неизвестен — запасной вариант
    # «question» (первая запись словаря) не даёт поломаться интерфейсу.
    # [0] — первый элемент кортежа (текст кнопки).
    return _MODE_META.get(mode, _MODE_META["question"])[0]


def _mode_continue_callback(mode: str) -> str:
    """Callback кнопки «продолжить» для режима ИИ."""
    # Так же берём payload; брать [1] — второй элемент кортежа (payload).
    # .get(mode, дефолт) страхует от неизвестных ключей в данных FSM.
    return _MODE_META.get(mode, _MODE_META["question"])[1]


def _format_logs(logs: list[dict]) -> str:
    """Форматирует список логов в читаемый текст."""
    # logs — список словарей от db.get_ai_logs_for_user / get_ai_logs.
    # Каждый словарь содержит поля: time (unix-таймстамп), question,
    # answer, user_name (для общего лога).
    if not logs:
        return ""   # пустой список → пустой текст

    # Импортируем модуль time ОДИН раз на вызов функции (не в цикле!):
    # раньше import стоял внутри цикла по логам и выполнялся на каждой
    # итерации — это лишняя работа. Теперь импорт происходит вне цикла.
    import time as _time
    text = ""
    # Показываем последние 10 записей (если их больше) с нумерацией с 1.
    for i, log in enumerate(logs[-10:], 1):
        # Unix-таймстамп из лога; из него делаем строку "дд.мм чч:мм".
        ts = log.get("time", 0)
        time_str = ""
        if ts:
            try:
                # Превращаем таймстамп в локальное время и форматируем.
                # localtime(int(ts)) — объект struct_time, strftime — строка.
                time_str = _time.strftime("%d.%m %H:%M", _time.localtime(int(ts)))
            except (ValueError, TypeError, OSError):
                # Испорченный/нечисловой таймстамп — оставляем пустую строку.
                # В выводе будет прочерк «—», лог не упадёт.
                time_str = ""

        # Обрезаем вопрос и ответ, чтобы лог оставался компактным.
        question = (log.get("question") or "")[:80]
        answer = (log.get("answer") or "")[:120]

        # Собираем строку записи: время, вопрос и (если есть) начало ответа.
        # Прочерк «—» подставляется, когда время/вопрос не удалось получить.
        text += f"{i}. 🕐 {time_str.strip() or '—'}\n"
        text += f"   ❓ {question or '—'}\n"
        if answer:
            # Ответ включаем только если он вообще был (не пустой).
            # Пустой ответ = ИИ не ответил — строку не рисуем.
            text += f"   💬 {answer}\n"
        text += "\n"

    return text


def _get_system_prompt(user_id: int, mode: str) -> str:
    """Формирует системный промпт для ИИ (роль + стиль + режим)."""
    # user_id — кто спрашивает; mode — режим (question/schedule/test/...).
    # Итог — строка-инструкция, которую _ask_ai упакует в messages[0] (system).
    # Достаём профиль пользователя из БД (все настройки ИИ он хранит у себя).
    # db.get_user вернёт dict с полями профиля или None, если записи нет.
    user = db.get_user(user_id)
    # Стиль объяснений: "simple" (коротко), "detailed", "fun" — идёт в промпт.
    # Эти значения пользователь выбирает в настройках (handlers/prefs.py).
    style = user.get("ai_style", "simple") if user else "simple"
    # Интересы/темы (ai_topics) — необязательная строка про «что любит юзер».
    # Модель использует её, чтобы примеры были из знакомых пользователю тем.
    topics = user.get("ai_topics", "") if user else ""
    # followup — задавать ли наводящие вопросы в конце (по умолчанию да).
    # bool() страхует, если в БД попадёт не-булевое значение ("0"/""/None).
    followup = bool(user.get("ai_followup", AI_FOLLOWUP_DEFAULT)) if user else AI_FOLLOWUP_DEFAULT
    # examples — добавлять ли примеры из жизни (по умолчанию да).
    examples = bool(user.get("ai_examples", AI_EXAMPLES_DEFAULT)) if user else AI_EXAMPLES_DEFAULT
    # detail — глубина ответа: "normal"/"short"/"detail".
    # Отдельное поле (не путается со style) — см. ниже блок про detail.
    detail = user.get("ai_detail", AI_DETAIL_DEFAULT) if user else AI_DETAIL_DEFAULT

    # Начало любого системного промпта — базовая роль помощника школьника:
    # объяснять просто, дружелюбно, без сложных терминов без пояснений.
    # Этот «фундамент» общий для ВСЕХ режимов, дальше по коду он дополняется.
    base_prompt = (
        "Ты — помощник школьника. Твоя задача — объяснять вещи "
        "простыми и понятными словами. "
        "Не используй сложные термины без объяснения. "
        "Будь дружелюбным и supportive.\n"
        # Язык ответа задаём явно: маленькая модель без инструкции
        # уползает в английский, даже если промпт написан по-русски.
        "Отвечай ВСЕГДА на русском языке, каким бы ни был язык вопроса "
        "(названия и термины — как принято в русской школьной программе).\n\n"
        # Форматирование Markdown. Эти пометки бот потом аккуратно
        # преобразует в документ (в PDF/Word/PPTX появятся заголовки,
        # жирный текст и списки). В чате пользователь видит чистый текст —
        # разметка из ответа убирается функцией markdown_to_plain.
        "Форматируй текст с помощью Markdown-разметки, чтобы ответ был "
        "структурированным:\n"
        "- Заголовки разделов — '### Название раздела' (или '## ' для более "
        "крупного).\n"
        "- Важные термины и ключевые мысли выделяй '**жирным**'.\n"
        "- Перечни оформляй списком с '–' в начале строки.\n"
        "- Формулы, команды и числа — в '`обратных кавычках`'.\n"
        "НЕ используй markdown-пометки в случаях, когда это не нужно "
        "маленькой порции текста: одно-два предложения — без разметки.\n\n"
    )

    # Блок «стиль»: в зависимости от ai_style добавляем в промпт инструкцию
    # о манере ответа (развёрнуто / с юмором / кратко).
    if style == "detailed":
        base_prompt += "Давай подробные развёрнутые ответы с примерами.\n\n"
    elif style == "fun":
        base_prompt += "Используй юмор и интересные факты в ответах.\n\n"
    else:
        # style == "simple" (по умолчанию) — краткие, но понятные ответы.
        # Сюда же попадают любые неизвестные значения style — консервативно.
        base_prompt += "Давай краткие, но понятные ответы.\n\n"

    # Подробность ответа (кратко / обычно / подробно).
    # detail приходит из своего поля (ai_detail) и не путается со style.
    if detail == "short":
        base_prompt += (
            "Отвечай КРАТКО: 3-5 предложений, только суть, "
            "без длинных пояснений.\n\n"
        )
    elif detail == "detail":
        base_prompt += (
            "Отвечай ПОДРОБНО: полно раскрывай тему, разбивай текст "
            "на логичные части, давай максимум пояснений.\n\n"
        )
    # detail == "normal" — добавлять ничего не нужно, работает базовый стиль.

    # Примеры из жизни (вкл/выкл).
    if examples:
        base_prompt += (
            "Используй примеры и аналогии из жизни, чтобы "
            "объяснение было наглядным.\n\n"
        )
    else:
        # examples выключено — явно запрещаем аналогии, чтобы ИИ не сыпал
        # отсебятиной и держал ответ строго по существу.
        base_prompt += "Не используй примеры и аналогии, отвечай по существу.\n\n"

    # Если у пользователя указаны интересы (ai_topics) — подсказываем их
    # модели, чтобы ответы опирались на знакомые ученику темы.
    # Пустая строка topics (не заполнено) → блок просто не добавляется.
    if topics:
        base_prompt += f"Интересы пользователя: {topics}\n\n"

    # Режим (mode) — самая важная часть: определяет, ЧТО делать с запросом.
    # Каждая ветка дописывает к единому промпту свою «профессиональную»
    # инструкцию: расписание → планирование, тест → вопросы с ответами и т.д.
    if mode == "schedule":
        base_prompt += (
            "Пользователь просит помочь составить расписание дня. "
            "Помоги распланировать день, учитывая:\n"
            "- Время подъёма и отхода ко сну\n- Уроки в школе\n"
            "- Кружки и секции\n- Время на отдых и подготовку к урокам\n"
            "Сделай расписание удобным и реалистичным."
        )
    elif mode == "test":
        base_prompt += (
            "Пользователь просит создать тест по теме. "
            "Создай тест из вопросов с вариантами ответов (A, B, C, D). "
            "В конце укажи правильные ответы с объяснениями."
        )
    elif mode == "homework":
        base_prompt += (
            "Пользователь просит помочь с домашним заданием. "
            "Помоги решить задание, объясни пошагово. "
            "Не просто давай ответ, а объясняй КАК решать!"
        )
    elif mode == "presentation":
        # Важно: прямо запрещаем «делать презентацию» — набросок текста
        # нужен именно как текст, а не разметка слайдов (см. hint в меню).
        base_prompt += (
            "Пользователь — учитель и просит качественный продуманный "
            "набросок текста по теме (доклад, статья, урок, выступление). "
            "НЕ создавай презентацию, НЕ описывай слайды и НЕ разбивай "
            "материал на слайды. Напиши связный, продуманный, готовый "
            "к доработке текст с понятной структурой: вступление, "
            "основная часть (логично разбитая на смысловые абзацы с "
            "подзаголовками), вывод. Текст должен быть живым, грамотным, "
            "без воды и шаблонных фраз, адаптированным под возраст "
            "учеников класса. В конце кратко перечисли 3-5 ключевых "
            "мыслей, которые учитель может вынести на доску или в конспект."
        )
    elif mode == "topic":
        base_prompt += (
            "Пользователь — учитель и просит составить план урока по теме. "
            "Составь подробный план урока: цели, этапы, вопросы ученикам, "
            "задания и ожидаемые результаты. Урок должен быть на 40-45 минут."
        )
    else:  # question — режим по умолчанию
        # Сюда приходят и неизвестные mode (страховка от битых данных FSM).
        base_prompt += (
            "Пользователь задаёт вопрос по школьной программе. "
            "Объясни просто и понятно."
        )

    # Наводящие вопросы в конце ответа (вкл/выкл).
    # Если followup включён — дописываем в конец промпта требование задать
    # 2-3 коротких вопроса на проверку понимания и предложить углубиться.
    # Это отдельная настройка от стиля — можно хотеть и кратко, и с вопросами.
    if followup:
        base_prompt += (
            "\n\nВ КОНЦЕ своего ответа задай пользователю 2-3 коротких "
            "наводящих вопроса, чтобы проверить, понял ли он тему, "
            "и предложи углубиться в неё."
        )

    # Итоговый системный промпт полностью готов — его отправит _ask_ai.
    return base_prompt


def _extract_text_from_content(content) -> str | None:
    """
    Извлекает текст из поля content ответа OpenAI-совместимого API.

    content может быть:
      • строкой — обычный случай, возвращаем как есть;
      • списком частей (content parts) — собираем текст из dict с type="text";
      • None / пустой строке — возвращаем None.

    Returns:
        Извлечённый текст (str) или None, если текста нет.
    """
    if not content:
        return None
    # Обычный случай: content — строка.
    if isinstance(content, str):
        stripped = content.strip()
        return stripped if stripped else None
    # Некоторые серверы (OpenAI-совместимые, LM Studio в новых версиях)
    # возвращают content списком: [{"type": "text", "text": "..."}].
    if isinstance(content, list):
        parts = []
        for part in content:
            if isinstance(part, dict) and part.get("type") == "text":
                parts.append(part.get("text", ""))
        combined = " ".join(parts).strip()
        return combined if combined else None
    return None


def _strip_thinking_tags(text: str) -> str:
    """
    Убирает «рассуждательный» блок модели из текста ответа.

    Qwen3 (и другие reasoning-модели) в режиме thinking оборачивают свои
    внутренние рассуждения в fenced-маркеры (тройные/двойные тики) с
    опциональной меткой (например, "reasoning"), чаще всего в НАЧАЛЕ ответа.
    Текст ПОСЛЕ закрывающего маркера — это итоговый ответ, который
    показываем пользователю. Блок рассуждений отбрасываем, чтобы не
    засорять сообщение.

    Функция консервативна: блок убирается, только если открывающий маркер
    стоит в САМОМ НАЧАЛЕ текста. Если маркеры не найдены — текст
    возвращается без изменений.

    Returns:
        Текст без thinking-блока и маркеров.
    """
    import re
    # Строим символ "`" из chr(96), чтобы в исходнике не было тика
    # внутри строки, который путает подсветку/разметку.
    bt = chr(96)

    # Паттерн fenced-блока, начинающегося с первого символа текста:
    #   открытие: `<тики><метка?> до конца строки;
    #   содержимое: любые символы, включая переводы строк (DOTALL);
    #   закрытие: `<тики>` в начале строки (ищем после переноса строки).
    # Якорь \A — АБСОЛЮТНОЕ начало строки, поэтому блок вне начала
    # текста не трогаем (консервативность).
    def _strip_fence(n: int, text: str) -> str:
        tag = bt * n
        pattern = re.compile(
            r"\A[ \t]*" + tag + r"[A-Za-z0-9_-]*[^\n]*\n"
            r".*?"
            r"\n[ \t]*" + tag + r"[ \t]*\n",
            flags=re.DOTALL,
        )
        return pattern.sub("", text)

    for n in (3, 4, 2, 6, 1):
        text = _strip_fence(n, text)
    return text.strip()


# Маркеры, после которых в рассуждениях Qwen3 идёт ИТОГОВЫЙ ответ.
# Перебираем с конца: самый поздний (он ближе к финалу размышлений).
_ANSWER_MARKERS = (
    "Окончательный ответ:",
    "Итоговый ответ:",
    "Final answer:",
    "Final Answer:",
    "Ответ:",
    "Ответ :",
    "Answer:",
    "Answer :",
    "Вывод:",
    "Итак:",
    "Следовательно:",
    "В итоге:",
    "Так что:",
)


def _extract_answer_from_reasoning(reasoning: str) -> str | None:
    """Вытаскивает ответ из reasoning_content (запасной путь).

    Если модель «задумалась», но так и не доработала до генерации content
    (весь лимит токенов ушёл в размышления), берём хвост рассуждений —
    у Qwen3 итоговый ответ обычно стоит ПОСЛЕ маркера вроде «Ответ:».
    Возвращаем только внятный кусок (>= 40 символов), иначе None.
    """
    if not reasoning:
        return None
    text = reasoning.strip()

    # Снимаем служебную «шапку» размышлений в начале текста.
    for header in ("Thinking Process:", "Thinking:", "Reasoning:",
                   "Thought process:", "Размышления:"):
        if text.startswith(header):
            text = text[len(header):].lstrip()
            break

    # Ищем самый поздний маркер ответа — берём текст после него.
    last_idx = -1
    last_len = 0
    for marker in _ANSWER_MARKERS:
        idx = text.rfind(marker)
        if idx > last_idx:
            last_idx = idx
            last_len = len(marker)
    if last_idx != -1:
        candidate = text[last_idx + last_len:].strip()
        if len(candidate) >= 40:
            return candidate.strip()

    # Маркера нет — забираем последнюю треть (там модель обычно подводит итог).
    tail = text[2 * len(text) // 3:].strip()
    if len(tail) >= 40:
        return tail
    return None


# Знаки конца предложения. Если ответ не заканчивается одним из них —
# модель оборвалась на полуслове (частое поведение маленьких моделей),
# и мы просим её дописать ответ.
_AI_END_CHARS = ".!?…»\"})]`\n"
_AI_CONTINUE_DRAFT_MIN = 300  # с какого размера черновика «дописываем», а не переспрашиваем


def _answer_incomplete(text: str | None) -> bool:
    """
    True, если ответ ИИ «оборван» и его стоит допросить.

    Обрыв определяется по отсутствию знака конца предложения в самом
    конце текста (точка, восклицание, вопрос, многоточие, кавычка,
    скобка или перевод строки). Пустой текст — тоже «обрыв».
    """
    if not text:
        return True
    stripped = text.strip()
    if not stripped:
        return True
    return stripped[-1] not in _AI_END_CHARS


def _ask_ai(
    system_prompt: str,
    user_message: str,
    max_tries: int = 2,
) -> tuple[str | None, str | None]:
    """
    Отправляет запрос к LM Studio (локальный OpenAI-совместимый сервер).

    Маленькие локальные модели часто «задумываются» и обрывают ответ на
    полуслове (или возвращают пустой content). Поэтому делаем до
    max_tries попыток: если ответ выглядит оборванным — просим модель либо
    дописать его с места обрыва, либо ответить заново целиком.

    Args:
        system_prompt: Системный промпт (инструкции для ИИ).
        user_message: Сообщение пользователя.
        max_tries: Сколько попыток запроса делать (по умолчанию 2).

    Returns:
        Кортеж (ответ, ошибка). Если ответ None — произошла ошибка.
    """
    # Защита от недонастроенного сервера: если ключ пустой или содержит
    # заготовку «ТВОЙ...» из примера конфига — сразу говорим администратору.
    # В LM Studio API-ключ формально не нужен, но конфиг требует его задать,
    # поэтому заготовка считается «не настроено».
    if not AI_API_KEY or "ТВОЙ" in AI_API_KEY:
        return None, "администратор ещё не настроил API-ключ ИИ"

    # Эндпоинт локального LM Studio — OpenAI-совместимый /v1/chat/completions.
    # Порт 1234 — стандарт для LM Studio; сервер живёт на localhost.
    url = "http://127.0.0.1:1234/v1/chat/completions"

    last_error: str | None = None
    best_answer: str | None = None
    # Флаг «отключаем рассуждения модели». Qwen3 и подобные в режиме thinking
    # целиком расходуют max_tokens на reasoning_content и до генерации ответа
    # (content) часто не доходят. Выключаем думание через LM Studio-параметр
    # chat_template_kwargs.enable_thinking, если сервер его понимает.
    enable_thinking = True

    for attempt in range(max(1, max_tries)):
        # Повторная попытка: как именно дозапрашивать модель.
        #   • есть приличный черновик (>= _AI_CONTINUE_DRAFT_MIN) — просим
        #     дописать текст с места обрыва (продолжение присоединяем в конец);
        #   • черновика почти нет (пусто/пара символов) — переспрашиваем
        #     исходный вопрос целиком.
        draft = best_answer or ""
        combine = False
        if attempt > 0:
            if len(draft.strip()) >= _AI_CONTINUE_DRAFT_MIN:
                user_msg = (
                    "Твой предыдущий ответ оборвался на полуслове. "
                    "Продолжи его РОВНО с места обрыва, не повторяя уже "
                    "написанное, и доведи мысль до конца:\n\n"
                    f"...{draft.strip()[-80:]}\n\n"
                    "Допиши ответ."
                )
                combine = True
            else:
                user_msg = (
                    f"{user_message}\n\n"
                    "(предыдущая попытка ответа оборвалась на полуслове: "
                    "отвечай ЦЕЛИКОМ полным ответом и не обрывай текст)"
                )
        else:
            user_msg = user_message

        data = {
            "model": AI_MODEL,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_msg},
            ],
            "temperature": 0.7,
            # max_tokens НЕ равен AI_MAX_OUTPUT_LENGTH: маленькая локальная
            # модель на простой вопрос и так «выдаёт» больше нужного, а
            # 12000 токенов заставляют её раздувать ответ до небес. 2048
            # токенов (~2 страницы) с запасом покрывает любой школьный ответ.
            "max_tokens": AI_MAX_TOKENS,
        }
        # Отключаем режим «думания» (см. enable_thinking). Часть серверов не
        # знает этого параметра и ответит 400 — тогда повторим без него.
        if enable_thinking:
            data["chat_template_kwargs"] = {"enable_thinking": False}

        try:
            # Синхронный POST с JSON; timeout=AI_TIMEOUT_SECONDS (из config.py,
            # по умолчанию 15 минут) — локальная модель может долго думать.
            # requests.post — блокирующий вызов; функция целиком запускается
            # через asyncio.to_thread, поэтому блокируется только поток, а не
            # event loop всего бота.
            response = requests.post(
                url,
                headers={"Content-Type": "application/json"},
                json=data,
                timeout=AI_TIMEOUT_SECONDS,
            )

            # Некоторые OpenAI-совместимые серверы не знают параметра
            # chat_template_kwargs и отвечают 400 — пробуем повторно БЕЗ него.
            if response.status_code == 400 and enable_thinking:
                print("AI API: сервер не принял enable_thinking=false — повтор без него")
                data.pop("chat_template_kwargs", None)
                enable_thinking = False
                response = requests.post(
                    url,
                    headers={"Content-Type": "application/json"},
                    json=data,
                    timeout=AI_TIMEOUT_SECONDS,
                )

            # HTTP-код не 2xx — сервер вернул ошибку (например, 404 — модель
            # не загружена, или 500 — сбой внутри).
            if not (200 <= response.status_code < 300):
                error_text = (response.text or "").strip()
                if len(error_text) > 300:
                    error_text = error_text[:300] + "..."
                # В консоль уходит ПОЛНЫЙ текст — для разбора проблемы.
                print(f"AI API Error: {response.status_code} - {response.text}")
                last_error = (
                    f"сервер ИИ вернул ошибку (HTTP {response.status_code}): "
                    f"{error_text or 'без описания'}"
                )
                continue

            # Успех: разбираем JSON-ответ OpenAI-формата.
            result = response.json()

            # Диагностика: печатаем структуру ответа для отладки пустых ответов.
            try:
                preview = str(result)[:500]
                print(f"AI API response preview (try {attempt + 1}): {preview}")
            except Exception:  # noqa: BLE001
                pass

            # Стандартная схема: choices[0].message.content — и есть текст ответа.
            content = None
            if "choices" in result and len(result["choices"]) > 0:
                choice = result["choices"][0]
                if "message" in choice and isinstance(choice["message"], dict):
                    msg = choice["message"]

                    # content может быть: строкой, списком частей, None или пустым.
                    content = _extract_text_from_content(msg.get("content"))

                    # reasoning_content (Qwen3 thinking mode) обычно содержит
                    # рассуждения модели и НЕ является ответом. НО если content
                    # пуст, а рассуждения есть — модель «передумала» весь лимит
                    # токенов и не дошла до генерации ответа. Тогда пытаемся
                    # вытащить итог из рассуждений (после маркера «Ответ:»);
                    # если не вышло — считаем попытку пустой и дозапрашиваем.
                    if not content:
                        reasoning = _extract_text_from_content(
                            msg.get("reasoning_content")
                        )
                        salvaged = (
                            _extract_answer_from_reasoning(reasoning)
                            if reasoning else None
                        )
                        if salvaged:
                            print(
                                f"AI API: content пуст — извлекли ответ из "
                                f"reasoning_content (попытка {attempt + 1}, "
                                f"len={len(reasoning)})"
                            )
                            content = salvaged
                        else:
                            if reasoning:
                                print(
                                    f"AI API: только reasoning_content в попытке "
                                    f"{attempt + 1} (len={len(reasoning)}) — content пуст"
                                )
                            last_error = "сервер ИИ вернул пустой ответ"
                            continue

                    # Чистим от thinking-тегов модели (fenced-блоки рассуждений).
                    content = _strip_thinking_tags(content).strip()

            if content:
                if combine:
                    best_answer = best_answer.rstrip() + "\n\n" + content
                elif not best_answer or len(content) >= len(best_answer):
                    best_answer = content
                last_error = None
                # Ответ «завершён» — сразу возвращаем, дальше не пробуем.
                if not _answer_incomplete(best_answer):
                    return best_answer, None
                continue

        # Таймаут: запрос не уложился в AI_TIMEOUT_SECONDS.
        except requests.exceptions.Timeout:
            print("AI API: Timeout error")
            last_error = (
                f"сервер ИИ не ответил в течение "
                f"{AI_TIMEOUT_SECONDS} секунд (таймаут)"
            )
        # Не удалось подключиться вообще — LM Studio не запущен или порт закрыт.
        except requests.exceptions.ConnectionError:
            print("AI API: Connection error")
            last_error = (
                "не удалось подключиться к серверу ИИ "
                "(убедись, что LM Studio запущен и модель загружена)"
            )
        # Прочие ошибки HTTP-клиента requests (плохой URL, SSL, и т.п.).
        except requests.exceptions.RequestException as e:
            print(f"AI API: Request error: {e}")
            last_error = f"ошибка сети при обращении к ИИ: {e}"
        # Последняя линия обороны: любая другая неожиданная ошибка.
        except Exception as e:  # noqa: BLE001
            print(f"AI API: Unexpected error: {e}")
            last_error = f"непредвиденная ошибка: {e}"

    # Исчерпали попытки. Если хоть что-то внятное осталось — отдаём это,
    # иначе — ошибку последней попытки (или стандартную формулировку).
    if best_answer:
        return best_answer, None
    return None, last_error or "сервер ИИ вернул пустой ответ"