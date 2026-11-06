# ============================================================
# ОБРАБОТЧИК РЕГИСТРАЦИИ — handlers/registration.py
# ============================================================
# Этот файл отвечает за:
#   1. Команду /start — приветствие и начало регистрации;
#   2. Выбор роли (ученик/учитель) по кнопкам;
#   3. Пошаговый ввод данных через FSM:
#        - ученик: телефон → пароль → имя → класс → школа → email → возраст
#        - учитель: телефон → пароль → имя → школа → email → возраст
#      (email и возраст — необязательные шаги, их можно пропустить)
#   4. Сохранение данных в базу.
#
# Как теперь устроены обработчики (сравните с maxgram):
#   • Вместо @bot.command / @bot.on используется роутер maxo:
#       router = Router()
#     и декорпторы router.message_created(...) / router.message_callback(...).
#   • Вместо синхронной функции с ctx теперь асинхронный обработчик,
#     первым аргументом принимающий типизированный апдейт: либо
#     MessageCreated (новое сообщение), либо MessageCallback (нажатие).
#   • Для отправки ответов используются фасады самого апдейта:
#       await update.answer_text(...)   — просто отправить сообщение
#       await update.reply_text(...)    — ответить, сославшись на сообщение
#       await update.edit_message(...)  — отредактировать текущее сообщение
#       await update.callback_answer(...) — «галочка» на нажатой кнопке
#   • Фильтры: Command("start") — команда, CallbackPayload(...) — кнопки,
#     StateFilter(...) — «мы сейчас ждём ввода в таком-то состоянии».
#   • Состояния и временные данные живут в FSM (см. handlers/states.py).
# ============================================================

# --- Импорты ---
# Router — базовый класс maxo, вокруг которого строится модуль: в него
# регистрируются все обработчики и он потом подключается к Dispatcher.
from maxo import Router
# Command("start") — фильтр команды /start; StateFilter(...) — фильтр
# «пользователь сейчас находится в одном из указанных состояний FSM».
from maxo.routing.filters import Command, StateFilter
# Типы апдейтов: MessageCreated (пришло новое сообщение), MessageCallback
# (пользователь нажал на кнопку с payload). По ним типизируются аргументы
# обработчиков, и фасады update.* знают, что можно вызвать.
from maxo.types import MessageCallback, MessageCreated
# KeyboardBuilder собирает клавиатуру (кнопка = текст + payload),
# метод .build() в конце отдаёт готовую структуру для keyboard=...
from maxo.utils.builders import KeyboardBuilder

# database — тонкая обёртка над БД: create_user, user_exists, set_user_field,
# approve_user и т.д. Все поля заявки пишутся сюда же (панель модератора).
import database as db
# Флаг REQUIRE_MODERATION: True — все заявки идут на модерацию, False —
# регистрация автоматически подтверждается сразу.
from config import REQUIRE_MODERATION

# CallbackPayload(...) — фильтр maxo по payload кнопки; добавляем его как
# свою обёртку на случай нестандартных payload'ов (см. handlers/filters.py).
from handlers.filters import CallbackPayload
# RegistrationStates — все состояния FSM регистрации: wait_phone_student,
# wait_password_student, ..., wait_age_teacher (см. handlers/states.py).
from handlers.states import RegistrationStates
# Вспомогательные функции работы с пользователем: get_user_name (имя/ник),
# resolve_user_id (общий ID для обоих типов апдейта).
from handlers.user_utils import get_user_name, resolve_user_id

# Каждый модуль бота создаёт свой Router. Позже handlers/__init__.py
# подключит все такие роутеры к общему Dispatcher'у.
router: Router = Router(name="registration")


# ============================================================
# КЛАВИАТУРЫ ДЛЯ РЕГИСТРАЦИИ
# ============================================================

def _role_keyboard() -> KeyboardBuilder:
    """
    Клавиатура выбора роли (показывается после /start).

    В maxo клавиатуры собираются через KeyboardBuilder: метод
    add_callback(text, payload) добавляет кнопку, которая при нажатии
    пришлёт боту колбэк с указанным payload. Метод build() возвращает
    готовую структуру, которую надо передать в keyboard=... фасада.

    Здесь намеренно НЕТ adjust(): две кнопки стоят в одном ряду, а их
    тексты короткие («Я Ученик» / «Я Учитель» — до 10 символов), что
    соответствует лимиту MAX для 2 кнопок в ряду. Если текст кнопки
    вырастет длиннее 10 символов — добавь .adjust(1).
    """
    # Создаём пустую клавиатуру — в неё будем по одной добавлять кнопки.
    kb = KeyboardBuilder()
    # Первая кнопка «Я Ученик» несёт payload "reg_student": по нему роутер
    # (см. handle_registration_callbacks) поймёт, что выбран ученик.
    kb.add_callback(text="Я Ученик", payload="reg_student")
    # Вторая кнопка «Я Учитель» → payload "reg_teacher" — учитель.
    # Обе кнопки без .adjust() встанут в один ряд (тексты короткие).
    kb.add_callback(text="Я Учитель", payload="reg_teacher")
    # Возвращаем собранную клавиатуру; .build() вызовет вызывающая сторона.
    return kb


def _back_keyboard() -> KeyboardBuilder:
    """Клавиатура «Отмена» (используется во время регистрации).

    Одна кнопка на весь ряд — текст «Отмена» короткий и в любой
    раскладке поместится целиком.
    """
    kb = KeyboardBuilder()
    # Кнопка «Отмена» → payload "reg_cancel". Она есть на каждом шаге:
    # пользователь может в любой момент прервать регистрацию.
    kb.add_callback(text="Отмена", payload="reg_cancel")
    return kb


def _skip_keyboard() -> KeyboardBuilder:
    """Клавиатура для необязательных шагов регистрации (email/возраст).

    «Пропустить» позволяет не указывать данные и перейти дальше,
    «Отмена» — прекратить регистрацию. По одной кнопке в ряд,
    чтобы текст не обрезался (лимит 20 символов для 1 кнопки).
    """
    kb = KeyboardBuilder()
    # «Пропустить» → payload "reg_skip": на шаге email/возраст это
    # означает «не заполнять поле, едем дальше».
    kb.add_callback(text="Пропустить", payload="reg_skip")
    # «Отмена» → payload "reg_cancel": полный выход из регистрации.
    kb.add_callback(text="Отмена", payload="reg_cancel")
    # .adjust(1) — по одной кнопке в ряд (текст до 20 символов виден целиком).
    return kb.adjust(1)


# ============================================================
# КОМАНДА /start
# ============================================================

@router.message_created(Command("start"))
async def cmd_start(update: MessageCreated) -> None:
    """
    Обрабатывает команду /start.

    Логика та же, что и в старой версии:
      • если пользователь уже подтверждён — показываем главное меню;
      • если зарегистрирован, но не подтверждён — просим подождать;
      • если новый — начинаем регистрацию с выбора роли.
    """
    # Обязательно сохраняем chat_id пользователя: именно по нему бот
    # сможет написать личное уведомление после подтверждения модератором.
    # В maxo chat_id доступен у апдейта через свойство update.chat_id.
    # resolve_user_id возвращает числовой ID пользователя (для MessageCreated
    # и для MessageCallback он достаётся одинаково).
    user_id = resolve_user_id(update)
    # get_user_name — читаемое имя (ФИ, ник или «Пользователь»): подставляем
    # его в приветствие, чтобы обращаться к человеку по имени.
    user_name = get_user_name(update)
    # chat_id через getattr — этот атрибут есть не у всех обёрток апдейтов,
    # поэтому берём аккуратно, со значением None по умолчанию.
    chat_id = getattr(update, "chat_id", None)

    # Диагностика в консоль (как было в старой версии).
    # Весь блок обёрнут в try/except: логирование не должно ронять бота,
    # даже если что-то пошло не так (например, БД ещё не инициализирована).
    try:
        # Собираем диагностическую строку: кто написал /start, в каком
        # он чате и есть ли вообще в базе.
        _dbg = (
            f"[CMD_START] user_id={user_id} chat_id={chat_id} "
            f"exists={bool(db.user_exists(user_id))}"
        )
        # Если пользователь найден — дописываем к диагностике его статус:
        # подтверждён ли модератором и какая у него роль (student/teacher).
        if user_id and db.user_exists(user_id):
            _dbg += (
                f" approved={bool(db.is_approved(user_id))} "
                f"role={db.get_user_field(user_id, 'role')}"
            )
        # Печатаем строку в stdout. flush=True — чтобы не откладывать вывод
        # буфера, а сразу видеть все запуски /start в логе бота.
        print(_dbg, flush=True)
    except Exception:  # noqa: BLE001
        # Любая ошибка диагностики игнорируется — это не критично.
        pass

    # Пользователь уже есть в базе.
    if db.user_exists(user_id):
        # Ветка 1: пользователь зарегистрировался и модератор его одобрил.
        if db.is_approved(user_id):
            # Подтверждён — открываем главное меню (асинхронная функция).
            # Импорт делаем прямо здесь, внутри функции: лениво и без
            # циклических импортов (menu тоже импортирует другие модули).
            from handlers.menu import show_main_menu
            await show_main_menu(update, user_id)
        else:
            # Ветка 2: запись в БД есть, но роль ещё "pending" — заявка
            # находится на рассмотрении у модератора. Дальше регистрацию
            # не продолжаем: просто вежливо просим подождать.
            await update.answer_text(
                f"Привет, {user_name}!\n"
                "Твоя заявка ещё не подтверждена модератором.\n"
                "Подожди немного — скоро тебя подтвердят!"
            )
        # После любой из двух веток выходим — для новых пользователей /start
        # обработался бы иначе (ниже).
        return

    # Новый пользователь — начинаем регистрацию с выбора роли.
    # answer_text(text, keyboard=...) — отправляем сообщение с клавиатурой.
    # Здесь же виден payload'ы: кнопки пришлют "reg_student"/"reg_teacher",
    # которые обработает handle_registration_callbacks.
    await update.answer_text(
        f"Привет, {user_name}! 👋\n\n"
        "Я — MAX Школьный помощник! 🤖\n\n"
        "Я помогаю школьникам с:\n"
        "• Объяснением уроков простыми словами\n"
        "• Составлением расписания дня\n"
        "• Созданием тестов по темам\n"
        "• Помощью с домашними заданиями\n"
        "• Заметками и настраиваемым интерфейсом\n\n"
        "Сначала нужно зарегистрироваться.\n"
        "Кто ты?",
        # Передаём собранную клавиатуру выбора роли.
        keyboard=_role_keyboard().build(),
    )


# ============================================================
# ВЫБОР РОЛИ / ОТМЕНА (КОЛБЭКИ)
# ============================================================

@router.message_callback(CallbackPayload("reg_student", "reg_teacher", "reg_cancel", "reg_skip"))
async def handle_registration_callbacks(update: MessageCallback, fsm_context) -> None:
    """
    Обрабатывает нажатия кнопок во время регистрации.

    Фильтр CallbackPayload(...) пропускает сюда только те колбэки,
    чей payload входит в указанный набор. Нажатия других кнопок сюда
    не дойдут — их обработают роутеры других модулей.

    fsm_context — это автоматически подставленный maxo объект FSMContext.
    Методы:
      await fsm_context.set_state(RegistrationStates.wait_phone_student)
      await fsm_context.update_data(phone="...")   — сохранить временные данные
      await fsm_context.get_data()                 — получить все временные данные
      await fsm_context.clear()                    — сбросить состояние и данные
    """
    # В maxo payload у колбэка — Omittable[str]. Мы уже отфильтровали
    # колбэки без payload через CallbackPayload, поэтому безопасно берём
    # его через update.payload (он точно определён).
    payload = update.payload
    # ID и имя нажавшего пользователя — нужны для create_user и edit_message.
    user_id = resolve_user_id(update)
    user_name = get_user_name(update)

    # ============================================================
    # ВЫБОР РОЛИ «Я УЧЕНИК»
    # ============================================================
    if payload == "reg_student":
        # Создаём запись в БД со статусом pending (ожидает модерации).
        # Пока пользователь заполняет анкету, у него уже есть запись —
        # модератор видит заявку в панели «в процессе».
        db.create_user(user_id, user_name, role="pending")
        # Сохраняем chat_id — по нему придёт уведомление после одобрения.
        if getattr(update, "chat_id", None):
            db.set_user_field(user_id, "chat_id", update.chat_id)
        # Запоминаем выбранную роль, чтобы approve_user восстановил её.
        # Отдельное поле role_requested хранит "student"/"teacher" до того,
        # как заявка пройдёт модерацию (при одобрении из него берётся роль).
        db.set_user_field(user_id, "role_requested", "student")

        # Переводим пользователя в состояние «ждём телефон». Это то, что
        # раньше делал state_manager.set_state(...). Получать сюда
        # стадии FSM позволяет тип State.
        # Конкретно — ШАГ 1 из 7 для ученика: ждём ввод телефона.
        await fsm_context.set_state(RegistrationStates.wait_phone_student)
        # Возвращаем True, чтобы фильтр/обработчик считался выполненным.

        # Меняем сообщение (перезаписываем ту же кнопочную карточку)
        # и отвечаем на колбэк. callback_answer — «галочка» кнопки,
        # edit_message — обновляет текст текущего сообщения.
        await update.callback_answer(text="Отлично!")
        await update.edit_message(
            # Далее пользователь видит, КАКОЙ шаг идёт и что писать.
            # Шаг 1 из 7 — именно 7 шагов у ученика (добавляется класс).
            text="📚 Ты — ученик!\n\n"
            "📱 Шаг 1 из 7. Введи свой номер телефона.\n"
            "Нужен для связи с тобой.\n\n"
            "Пример: +79001234567",
            # Рядом всегда кнопка «Отмена» — чтобы можно было выйти.
            keyboard=_back_keyboard().build(),
        )

    # ============================================================
    # ВЫБОР РОЛИ «Я УЧИТЕЛЬ»
    # ============================================================
    elif payload == "reg_teacher":
        # Аналогично ученику: создаём запись со статусом pending...
        db.create_user(user_id, user_name, role="pending")
        # ..., сохраняем chat_id для будущего уведомления об одобрении.
        if getattr(update, "chat_id", None):
            db.set_user_field(user_id, "chat_id", update.chat_id)
        # В role_requested кладём "teacher" — при модерации роль восстановится.
        db.set_user_field(user_id, "role_requested", "teacher")

        # ШАГ 1 из 6 для учителя: ждём телефон (у учителя 6 шагов —
        # без указания класса).
        await fsm_context.set_state(RegistrationStates.wait_phone_teacher)

        await update.callback_answer(text="Отлично!")
        await update.edit_message(
            text="👨‍🏫 Ты — учитель!\n\n"
            "📱 Шаг 1 из 6. Введи свой номер телефона.\n"
            "Нужен для связи с тобой.\n\n"
            "Пример: +79001234567",
            keyboard=_back_keyboard().build(),
        )

    # ============================================================
    # ПРОПУСТИТЬ НЕОБЯЗАТЕЛЬНЫЙ ШАГ (email / возраст)
    # ============================================================
    elif payload == "reg_skip":
        # Кнопка «Пропустить» ведёт себя по-разному на разных шагах,
        # поэтому сначала узнаём текущее состояние FSM.
        state = await fsm_context.get_state()

        # email (ученик) → пропускаем и просим возраст
        if state == RegistrationStates.wait_email_student:
            # Пустой email в FSM: в update_data кладём "", чтобы финализация
            # не потеряла ключ и знала, что email намеренно не заполнен.
            await fsm_context.update_data({"email": ""})
            # Дублируем поле в БД (пустое) — панель модератора видит пробел.
            db.set_user_field(user_id, "email", "")
            # Переходим к ШАГУ 7 из 7 — ждём возраст ученика.
            await fsm_context.set_state(RegistrationStates.wait_age_student)
            await update.callback_answer(text="Пропущено")
            await update.edit_message(
                text="✅ Email пропущен.\n\n"
                "🎂 Шаг 7 из 7. Сколько тебе лет?\n"
                "Можешь указать возраст числом.\n\n"
                "Или нажми «Пропустить» — и закончим регистрацию.",
                # Здесь тоже клавиатура «Пропустить/Отмена»: возраст
                # тоже необязательный, им можно пренебречь.
                keyboard=_skip_keyboard().build(),
            )

        # возраст (ученик) → финализируем
        elif state == RegistrationStates.wait_age_student:
            # Возраст пропущен → кладём 0 как «не указано».
            await fsm_context.update_data({"age": 0})
            await update.callback_answer(text="Готово")
            # Все шаги ученика пройдены — вызываем общую финализацию:
            # она соберёт все данные из FSM, запишет их в БД и очистит FSM.
            await _finish_student(update, fsm_context, user_id)

        # email (учитель) → пропускаем и просим возраст
        elif state == RegistrationStates.wait_email_teacher:
            # Тот же сценарий, что и у ученика: пустой email в FSM и БД.
            await fsm_context.update_data({"email": ""})
            db.set_user_field(user_id, "email", "")
            # ШАГ 5 из 6 для учителя — далее ждём возраст.
            await fsm_context.set_state(RegistrationStates.wait_age_teacher)
            await update.callback_answer(text="Пропущено")
            await update.edit_message(
                text="✅ Email пропущен.\n\n"
                "🎂 Шаг 5 из 6. Сколько вам лет?\n"
                "Можете указать возраст числом.\n\n"
                "Или нажмите «Пропустить» — и закончим регистрацию.",
                keyboard=_skip_keyboard().build(),
            )

        # возраст (учитель) → финализируем
        elif state == RegistrationStates.wait_age_teacher:
            # Возраст учителя пропущен → 0 как «не указано».
            await fsm_context.update_data({"age": 0})
            await update.callback_answer(text="Готово")
            # Все шаги учителя пройдены — финализируем регистрацию.
            await _finish_teacher(update, fsm_context, user_id)

    # ============================================================
    # ОТМЕНА РЕГИСТРАЦИИ
    # ============================================================
    elif payload == "reg_cancel":
        # fsm_context.clear() полностью сбрасывает и состояние, и данные —
        # это аналог clear_state() из старого state_manager.
        await fsm_context.clear()

        await update.callback_answer(text="Отменено")
        # Сообщение обновляем без клавиатуры — кнопок больше не нужно,
        # чтобы продолжить, надо снова написать /start.
        await update.edit_message(
            text="Регистрация отменена.\n"
            "Напиши /start чтобы начать заново."
        )


# ============================================================
# ТЕКСТОВЫЕ СООБЩЕНИЯ ВО ВРЕМЯ РЕГИСТРАЦИИ
# ============================================================
# Фильтр StateFilter(RegistrationStates) означает: обработчик сработает
# только если пользователь находится ЛИБО в одном из регистрационных
# состояний. Все другие тексты будут обрабатываться другими модулями.
# (StateFilter с целой StatesGroup принимает её как «любое состояние
# этой группы».)

@router.message_created(StateFilter(RegistrationStates))
async def handle_registration_text(update: MessageCreated, fsm_context) -> None:
    """
    Обрабатывает текстовые сообщения во время регистрации.

    В зависимости от текущего состояния (get_state) сохраняем введённые
    данные и переходим к следующему шагу. Временные данные (телефон,
    пароль, имя) складываем в FSM через update_data — это замена
    user_temp_data. В БД данные сохраняются сразу (как было и в старой
    версии, чтобы модератор видел заявку уже в процессе).
    """
    # Всегда обновляем chat_id пользователя «на всякий случай».
    # Если заявка уже создана и пользователь пишет из нового чата —
    # уведомление об одобрении придёт в последний активный чат.
    user_id = resolve_user_id(update)
    if user_id and getattr(update, "chat_id", None) and db.user_exists(user_id):
        db.set_user_field(user_id, "chat_id", update.chat_id)

    # Текущее состояние FSM (строка типа "RegistrationStates:wait_...").
    # Именно по нему роутер понимает, какой шаг сейчас заполняет
    # пользователь: телефон, пароль, имя, класс и т.д.
    state = await fsm_context.get_state()

    # Текст пришедшего сообщения. В maxo текст лежит в update.text.
    text = update.text or ""
    # Если сообщение пустое (например, только медиа) — делать нечего:
    # форсировать ввод текста не будем, просто выходим из обработчика.
    if not text:
        return

    # ============================================================
    # ВСПОМОГАТЕЛЬНАЯ ФУНКЦИЯ ПЕРЕХОДА «на следующий шаг»
    # ============================================================
    # Раньше при каждом шаге вызывалась связка
    #   update_temp_data(user_id, key, value) + set_state(user_id, next)
    # Теперь это делается так:
    async def next_step(next_state, data_key, data_value):
        # Сохраняем очередное значение во временные данные FSM.
        # data_key — имя поля ("phone"/"password"/"full_name"...), data_value —
        # сырой текст из сообщения. Эти данные позже заберёт _finish_student
        # через fsm_context.get_data().
        await fsm_context.update_data({data_key: data_value})
        # Помимо FSM сразу пишем значение в БД (для панели модератора).
        # Так заявка постепенно «наполняется» ещё до финализации.
        db.set_user_field(user_id, data_key, data_value)
        # Переходим к следующему состоянию: next_state — это очередная
        # стадия RegistrationStates (например, wait_password_student).
        await fsm_context.set_state(next_state)

    # ============================================================
    # УЧЕНИК: ШАГ 1 — ТЕЛЕФОН
    # ============================================================
    if state == RegistrationStates.wait_phone_student:
        # Валидация телефона: минимум 7 символов и хотя бы одна цифра.
        # Это грубая проверка формата — достаточно, чтобы отсечь мусор
        # (полное форматирование номера тут не требуется).
        if len(text) < 7 or not any(c.isdigit() for c in text):
            # Ошибка: не уходим из состояния, пользователь остаётся на
            # ШАГЕ 1 и может ввести номер ещё раз.
            await update.answer_text(
                "❌ Некорректный номер телефона.\n"
                "Попробуй ещё раз (например: +79001234567):",
                keyboard=_back_keyboard().build(),
            )
            return
        # Валидация прошла: сохраняем "phone" в FSM и БД и переходим к
        # следующему состоянию — ждём пароль ученика (ШАГ 2 из 7).
        await next_step(RegistrationStates.wait_password_student, "phone", text)
        await update.answer_text(
            "✅ Номер принят!\n\n"
            "🔒 Шаг 2 из 7. Придумай пароль (минимум 4 символа).\n"
            "Он нужен для входа в бота на других устройствах.",
            keyboard=_back_keyboard().build(),
        )

    # ============================================================
    # УЧЕНИК: ШАГ 2 — ПАРОЛЬ
    # ============================================================
    elif state == RegistrationStates.wait_password_student:
        if len(text) < 4:
            # Пароль короче 4 символов — повторяем просьбу, остаёмся на шаге.
            await update.answer_text(
                "❌ Пароль слишком короткий!\n"
                "Нужно минимум 4 символа. Попробуй ещё раз:",
                keyboard=_back_keyboard().build(),
            )
            return
        # Пароль валиден: сохраняем "password", переходим на ШАГ 3 — имя.
        await next_step(RegistrationStates.wait_name_student, "password", text)
        await update.answer_text(
            "✅ Пароль принят!\n\n"
            "👤 Шаг 3 из 7. Как тебя зовут? (Имя и фамилия)\n"
            "Например: Иванов Иван",
            keyboard=_back_keyboard().build(),
        )

    # ============================================================
    # УЧЕНИК: ШАГ 3 — ИМЯ
    # ============================================================
    elif state == RegistrationStates.wait_name_student:
        if len(text) < 2:
            # Слишком короткое имя (1 символ) — просим ввести заново.
            await update.answer_text(
                "❌ Имя слишком короткое.\n"
                "Попробуй ещё раз:",
                keyboard=_back_keyboard().build(),
            )
            return
        # Сохраняем "full_name", переходим на ШАГ 4 — класс ученика.
        await next_step(RegistrationStates.wait_class_student, "full_name", text)
        await update.answer_text(
            "✅ Имя принято!\n\n"
            "📚 Шаг 4 из 7. В каком ты классе?\n"
            "Например: 9А, 5Б, 11В",
            keyboard=_back_keyboard().build(),
        )

    # ============================================================
    # УЧЕНИК: ШАГ 4 — КЛАСС
    # ============================================================
    elif state == RegistrationStates.wait_class_student:
        if len(text) < 1:
            # Пустая строка не считается классом — просим указать класс.
            await update.answer_text(
                "❌ Укажи свой класс.",
                keyboard=_back_keyboard().build(),
            )
            return
        # Сохраняем "class_name" (этот шаг есть только у ученика), далее
        # ШАГ 5 — школа. У учителя такого состояния нет вовсе.
        await next_step(RegistrationStates.wait_school_student, "class_name", text)
        await update.answer_text(
            "✅ Класс принят!\n\n"
            "🏫 Шаг 5 из 7. Напиши свою школу.\n\n"
            "Формат: Республика/область, город/район, номер школы\n\n"
            "Просто напиши название, как ты знаешь свою школу:",
            keyboard=_back_keyboard().build(),
        )

    # ============================================================
    # УЧЕНИК: ШАГ 5 — ШКОЛА (далее email → возраст)
    # ============================================================
    elif state == RegistrationStates.wait_school_student:
        if len(text) < 1:
            await update.answer_text(
                "❌ Укажи свою школу.",
                keyboard=_back_keyboard().build(),
            )
            return

        # Сохраняем школу без отдельной функции next_step (школа пишется
        # напрямую): важно, что дальше у ученика идёт НЕОБЯЗАТЕЛЬНЫЙ email.
        await fsm_context.update_data({"school": text})
        db.set_user_field(user_id, "school", text)
        # Переходим к ШАГУ 6 из 7 — email (можно пропустить).
        await fsm_context.set_state(RegistrationStates.wait_email_student)
        await update.answer_text(
            "✅ Школа принята!\n\n"
            "📧 Шаг 6 из 7. Укажи свой email (необязательно).\n\n"
            "Пригодится для восстановления доступа.\n"
            "Напиши email или нажми «Пропустить»:",
            # Здесь вместо «Отмена» — клавиатура «Пропустить/Отмена»:
            # email не обязателен, и пользователь должен это видеть.
            keyboard=_skip_keyboard().build(),
        )

    # ============================================================
    # УЧЕНИК: ШАГ 6 — EMAIL (необязательный)
    # ============================================================
    elif state == RegistrationStates.wait_email_student:
        # Обрезаем пробелы по краям — лишние пробелы в email не нужны.
        email = text.strip()
        # Простейшая валидация email: есть символ @ и точка. Этого хватает,
        # чтобы не сохранять откровенно бессмысленные строки.
        if "@" not in email or "." not in email:
            await update.answer_text(
                "❌ Email выглядит некорректно.\n"
                "Введи ещё раз (например: ivan@mail.ru) или нажми «Пропустить»:",
                keyboard=_skip_keyboard().build(),
            )
            return
        # Email валиден: сохраняем "email" и переходим на ШАГ 7 — возраст
        # (тоже необязательный).
        await next_step(RegistrationStates.wait_age_student, "email", email)
        await update.answer_text(
            "✅ Email принят!\n\n"
            "🎂 Шаг 7 из 7. Сколько тебе лет? (необязательно)\n\n"
            "Напиши возраст числом или нажми «Пропустить»:",
            keyboard=_skip_keyboard().build(),
        )

    # ============================================================
    # УЧЕНИК: ШАГ 7 — ВОЗРАСТ (ФИНАЛ)
    # ============================================================
    elif state == RegistrationStates.wait_age_student:
        # Парсим возраст: _parse_age вернёт int либо None при невалидном.
        age = _parse_age(text)
        if age is None:
            # Возраст не число или вне диапазона — просим ввести заново,
            # либо пропустить (клавиатура «Пропустить/Отмена»).
            await update.answer_text(
                "❌ Возраст — это число от 1 до 120.\n"
                "Например: 12\n\n"
                "Или нажми «Пропустить»:",
                keyboard=_skip_keyboard().build(),
            )
            return
        # Возраст валиден: кладём в FSM и в БД. Далее — ФИНАЛ: все данные
        # собраны, вызываем финализацию ученика (суммирует и чистит FSM).
        await fsm_context.update_data({"age": age})
        db.set_user_field(user_id, "age", age)
        await _finish_student(update, fsm_context, user_id)

    # ============================================================
    # УЧИТЕЛЬ: ШАГ 1 — ТЕЛЕФОН
    # ============================================================
    elif state == RegistrationStates.wait_phone_teacher:
        # Та же валидация телефонного номера, что и у ученика.
        if len(text) < 7 or not any(c.isdigit() for c in text):
            await update.answer_text(
                "❌ Некорректный номер телефона.\n"
                "Попробуй ещё раз (например: +79001234567):",
                keyboard=_back_keyboard().build(),
            )
            return
        # Телефон учителя принят → ШАГ 2 из 6 — пароль.
        await next_step(RegistrationStates.wait_password_teacher, "phone", text)
        await update.answer_text(
            "✅ Номер принят!\n\n"
            "🔒 Шаг 2 из 6. Придумай пароль (минимум 4 символа).\n"
            "Он нужен для входа в бота на других устройствах.",
            keyboard=_back_keyboard().build(),
        )

    # ============================================================
    # УЧИТЕЛЬ: ШАГ 2 — ПАРОЛЬ
    # ============================================================
    elif state == RegistrationStates.wait_password_teacher:
        if len(text) < 4:
            await update.answer_text(
                "❌ Пароль слишком короткий!\n"
                "Нужно минимум 4 символа. Попробуй ещё раз:",
                keyboard=_back_keyboard().build(),
            )
            return
        # Пароль принят → ШАГ 3 из 6 — имя учителя.
        await next_step(RegistrationStates.wait_name_teacher, "password", text)
        await update.answer_text(
            "✅ Пароль принят!\n\n"
            "👤 Шаг 3 из 6. Как тебя зовут? (Имя и фамилия)\n"
            "Например: Петрова Мария Ивановна",
            keyboard=_back_keyboard().build(),
        )

    # ============================================================
    # УЧИТЕЛЬ: ШАГ 3 — ИМЯ
    # ============================================================
    elif state == RegistrationStates.wait_name_teacher:
        if len(text) < 2:
            await update.answer_text(
                "❌ Имя слишком короткое.\n"
                "Попробуй ещё раз:",
                keyboard=_back_keyboard().build(),
            )
            return
        # Имя принято → ШАГ 4 из 6 — школа (у учителя нет шага «класс»).
        await next_step(RegistrationStates.wait_school_teacher, "full_name", text)
        await update.answer_text(
            "✅ Имя принято!\n\n"
            "🏫 Шаг 4 из 6. Напиши в какой школе ты работаешь.\n\n"
            "Формат: Республика/область, город/район, номер школы\n\n"
            "Примеры:\n"
            "• Республика Дагестан, г. Махачкала, школа №15\n"
            "• г. Москва, школа №179\n\n"
            "Просто напиши название, как ты знаешь свою школу:",
            keyboard=_back_keyboard().build(),
        )

    # ============================================================
    # УЧИТЕЛЬ: ШАГ 4 — ШКОЛА (далее email → возраст)
    # ============================================================
    elif state == RegistrationStates.wait_school_teacher:
        if len(text) < 1:
            await update.answer_text(
                "❌ Укажи свою школу.",
                keyboard=_back_keyboard().build(),
            )
            return

        # Сохраняем школу учителя напрямую и переходим к email (ШАГ 5).
        await fsm_context.update_data({"school": text})
        db.set_user_field(user_id, "school", text)
        await fsm_context.set_state(RegistrationStates.wait_email_teacher)
        await update.answer_text(
            "✅ Школа принята!\n\n"
            "📧 Шаг 5 из 6. Укажи свой email (необязательно).\n\n"
            "Пригодится для восстановления доступа.\n"
            "Напиши email или нажми «Пропустить»:",
            keyboard=_skip_keyboard().build(),
        )

    # ============================================================
    # УЧИТЕЛЬ: ШАГ 5 — EMAIL (необязательный)
    # ============================================================
    elif state == RegistrationStates.wait_email_teacher:
        email = text.strip()
        # Минимальная проверка на @ и точку — как у ученика.
        if "@" not in email or "." not in email:
            await update.answer_text(
                "❌ Email выглядит некорректно.\n"
                "Введи ещё раз (например: uchitel@mail.ru) или нажми «Пропустить»:",
                keyboard=_skip_keyboard().build(),
            )
            return
        # Email принят → ШАГ 6 из 6 — возраст учителя.
        await next_step(RegistrationStates.wait_age_teacher, "email", email)
        await update.answer_text(
            "✅ Email принят!\n\n"
            "🎂 Шаг 6 из 6. Сколько вам лет? (необязательно)\n\n"
            "Напишите возраст числом или нажмите «Пропустить»:",
            keyboard=_skip_keyboard().build(),
        )

    # ============================================================
    # УЧИТЕЛЬ: ШАГ 6 — ВОЗРАСТ (ФИНАЛ)
    # ============================================================
    elif state == RegistrationStates.wait_age_teacher:
        age = _parse_age(text)
        if age is None:
            # Невалидный возраст — остаёмся на шаге, можно пропустить.
            await update.answer_text(
                "❌ Возраст — это число от 1 до 120.\n"
                "Например: 35\n\n"
                "Или нажмите «Пропустить»:",
                keyboard=_skip_keyboard().build(),
            )
            return
        # Возраст принят — финализируем регистрацию учителя.
        await fsm_context.update_data({"age": age})
        db.set_user_field(user_id, "age", age)
        await _finish_teacher(update, fsm_context, user_id)


# ============================================================
# ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ДЛЯ РЕГИСТРАЦИИ
# ============================================================

def _parse_age(text: str):
    """
    Парсит возраст из текста. Возвращает int или None при ошибке.

    Допустимый диапазон: 1..120 (для школьников — обычно 5..18,
    но на случай учителей и ошибок даём запас).
    """
    try:
        # Пробуем превратить строку в целое число (например "12" → 12).
        n = int(text)
    except (TypeError, ValueError):
        # Пустая строка, "abc", "12.5" и т.п. → не число → вернём None,
        # чтобы обработчик попросил ввести возраст заново.
        return None
    # Возвращаем число ТОЛЬКО если оно в пределах 1..120; иначе None —
    # выходит за границы диапазона, значит введено некорректно.
    return n if 1 <= n <= 120 else None


async def _finish_student(
    update: MessageCreated | MessageCallback,
    fsm_context,
    user_id: int,
) -> None:
    """
    Финализирует регистрацию ученика: сохраняет данные, очищает FSM
    и отправляет итоговое сообщение.
    """
    # Забираем из FSM ВСЕ накопленные за шаги данные: phone, password,
    # full_name, class_name, school, email, age. На этом этапе FSM играет
    # роль «черновика анкеты» — из него переносим поля в БД.
    data = await fsm_context.get_data()

    # Переносим каждое поле в БД. .get(..., "") защищает от пропусков:
    # если какое-то поле не заполнялось, в БД уйдёт пустая строка.
    db.set_user_field(user_id, "phone", data.get("phone", ""))
    db.set_user_field(user_id, "password", data.get("password", ""))
    db.set_user_field(user_id, "full_name", data.get("full_name", ""))
    db.set_user_field(user_id, "class_name", data.get("class_name", ""))
    # school могла быть записана в БД ещё на шаге «школа» напрямую —
    # поэтому берём или из FSM, или уже из БД, чтобы не затереть пустым.
    school = data.get("school") or db.get_user_field(user_id, "school") or ""
    db.set_user_field(user_id, "school", school)
    # email и age необязательные: если не заполнены — пустая строка/0.
    db.set_user_field(user_id, "email", data.get("email", ""))
    db.set_user_field(user_id, "age", data.get("age", 0))

    # Анкета полностью сохранена — состояние FSM больше не нужно: сбрасываем
    # и состояние, и временные данные. Пользователь больше не «в регистрации».
    await fsm_context.clear()

    # Достаём необязательные поля ещё раз (могут быть пустыми) — от этого
    # зависит, включать ли их строки в итоговую сводку (summary).
    email = data.get("email", "")
    age = data.get("age", 0)

    # Собираем сводку анкеты для сообщения пользователю — по одной строке
    # на поле, чтобы в чате было наглядно видно, что он ввёл.
    summary = (
        f"🎭 Роль: Ученик\n"
        f"👤 Имя: {data.get('full_name')}\n"
        f"📚 Класс: {data.get('class_name')}\n"
        f"🏫 Школа: {school}\n"
        f"📱 Телефон: {data.get('phone')}\n"
    )
    # Email и возраст добавляем в сводку только если они реально заполнены -
    # иначе в сообщении висели бы пустые строки «📧 Email: ».
    if email:
        summary += f"📧 Email: {email}\n"
    if age:
        summary += f"🎂 Возраст: {age}\n"

    # Дальше два сценария в зависимости от флага REQUIRE_MODERATION из config.
    if REQUIRE_MODERATION:
        # 1) Модерация включена: заявка ушла на проверку. Пользователь видит
        # сводку и предупреждение, что нужно дождаться подтверждения.
        await update.answer_text(
            "✅ Регистрация завершена!\n\n"
            f"📋 Твои данные:\n"
            f"{summary}\n"
            "⏳ Твоя заявка отправлена на модерацию.\n"
            "Подожди, пока модератор подтвердит твою заявку.\n"
            "После подтверждения ты сможешь пользоваться ботом!"
        )
    else:
        # 2) Модерация отключена: одобряем пользователя прямо сейчас
        # (approve_user сделает его активным), и он сразу может работать.
        db.approve_user(user_id)
        await update.answer_text(
            "✅ Регистрация завершена!\n\n"
            f"📋 Твои данные:\n"
            f"{summary}\n"
            "🎉 Теперь ты можешь пользоваться ботом!\n"
            "Напиши /start чтобы открыть меню."
        )


async def _finish_teacher(
    update: MessageCreated | MessageCallback,
    fsm_context,
    user_id: int,
) -> None:
    """
    Финализирует регистрацию учителя: сохраняет данные, очищает FSM
    и отправляет итоговое сообщение.
    """
    # Достаём накопленные данные учителя из FSM (у учителя нет class_name).
    data = await fsm_context.get_data()

    # Переносим поля учителя в БД — всё как у ученика, но без «класса».
    db.set_user_field(user_id, "phone", data.get("phone", ""))
    db.set_user_field(user_id, "password", data.get("password", ""))
    db.set_user_field(user_id, "full_name", data.get("full_name", ""))
    # school берём из FSM либо из БД, где она уже могла быть сохранена.
    school = data.get("school") or db.get_user_field(user_id, "school") or ""
    db.set_user_field(user_id, "school", school)
    db.set_user_field(user_id, "email", data.get("email", ""))
    db.set_user_field(user_id, "age", data.get("age", 0))

    # FSM больше не нужен — сбрасываем состояние и временные данные.
    await fsm_context.clear()

    email = data.get("email", "")
    age = data.get("age", 0)

    # Сводка для учителя — без строки про класс.
    summary = (
        f"🎭 Роль: Учитель\n"
        f"👤 Имя: {data.get('full_name')}\n"
        f"🏫 Школа: {school}\n"
        f"📱 Телефон: {data.get('phone')}\n"
    )
    # Добавляем email/возраст в сводку только если они заполнены.
    if email:
        summary += f"📧 Email: {email}\n"
    if age:
        summary += f"🎂 Возраст: {age}\n"

    if REQUIRE_MODERATION:
        # Заявка учителя отправлена на модерацию.
        await update.answer_text(
            "✅ Регистрация завершена!\n\n"
            f"📋 Твои данные:\n"
            f"{summary}\n"
            "⏳ Твоя заявка отправлена на модерацию.\n"
            "Подожди, пока модератор подтвердит твою заявку.\n"
            "После подтверждения ты сможешь пользоваться ботом!"
        )
    else:
        # Модерации нет: сразу одобряем учителя, назначаем ему роль "teacher"
        # (в отличие от ученика, которому роль ставит approve_user из
        # role_requested — у учителя здесь явный db.set_role).
        db.approve_user(user_id)
        db.set_role(user_id, "teacher")
        await update.answer_text(
            "✅ Регистрация завершена!\n\n"
            f"📋 Твои данные:\n"
            f"{summary}\n"
            "🎉 Теперь ты можешь пользоваться ботом!\n"
            "Напиши /start чтобы открыть меню."
        )