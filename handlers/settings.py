# ============================================================
# ОБРАБОТЧИК НАСТРОЕК — handlers/settings.py
# ============================================================
# Этот файл отвечает за:
#   1. Настройку стиля общения ИИ;
#   2. Настройку интересующих тем для ИИ;
#   3. Настройку интерфейса (эмодзи, компактный вид, счётчики);
#   4. Просмотр статистики и информации о боте;
#   5. Настройку лимитов запросов к ИИ (для учителей/модераторов);
#   6. Управление школой и классом (выйти из школы, подать заявку
#      на вступление в другую школу/класс — учитель подтверждает).
# ============================================================
# Логика в этом файле построена вокруг ОДНОГО большого обработчика
# handle_settings_menu, который разбирает payload каждой кнопки и
# выполняет соответствующую ветку через if/elif. Ниже по тексту кода
# каждая ветка подробно прокомментирована.
# ============================================================
# ВНИМАНИЕ: в файле много FSM-состояний (SettingsStates). Каждое из них
# «замораживает» диалог до получения следующего текстового сообщения.
# Где именно устанавливается состояние — помечено комментариями.
# ============================================================

from maxo import Router
# Модуль роутера maxo: через router.message_callback / router.message_created
# регистрируются обработчики колбэков и текстовых сообщений.
from maxo.routing.filters import StateFilter
# StateFilter — фильтр входящих сообщений по активному FSM-состоянию.
from maxo.types import MessageCallback, MessageCreated
# MessageCallback — колбэк от нажатия кнопки; MessageCreated — текст/команда.
from maxo.utils.builders import KeyboardBuilder
# KeyboardBuilder — удобный конструктор клавиатур (метод .build()).

import database as db
# БД: пользователи, настройки ИИ, заявки в школы, статистика, логи.
from config import AI_DETAIL_DEFAULT, AI_EXAMPLES_DEFAULT, AI_FOLLOWUP_DEFAULT
# Дефолтные значения AI-настроек, если у пользователя они ещё не заданы.

from handlers.filters import CallbackPayload
# CallbackPayload — класс для декларативного описания списка payload'ов.
from handlers.prefs import btn, emoji, get_ui_prefs, set_ui_pref
# btn()/emoji() добавляют эмодзи к текстам; get/set_ui_pref — UI-преференсы.
from handlers.states import SettingsStates
# Группа состояний FSM раздела настроек (ввод тем, школы, лимитов и т.д.).
from handlers.user_utils import get_user_id
# get_user_id принимает апдейт и возвращает числовой ID отправителя.

# Единый роутер модуля: регистрирует все колбэки и текстовые вводы настроек.
router: Router = Router(name="settings")

# ------------------------------------------------------------------
# КАРТА РАЗДЕЛА НАСТРОЕК (схема переходов между экранами)
# ------------------------------------------------------------------
# menu_settings   -> корень: кнопки на все разделы настроек.
# settings_ai     -> сводка настроек ИИ + подразделы:
#                     settings_ai_style   -> выбор стиля (simple/detailed/fun);
#                     settings_ai_extra   -> тумблеры followup/examples/length;
#                     settings_topics     -> темы ИИ (добавить/очистить/ввести);
# settings_ui     -> тумблеры интерфейса: эмодзи, компакт, счётчики.
# settings_school -> школа и класс: заявки, выход, смена школы.
# settings_personal -> карточка личных данных + редактирование (FSM).
# settings_stats  -> статистика пользователя (и по всей базе для учителей).
# settings_limits -> лимиты ИИ учеников (только учителя/модераторы).
# settings_about  -> статичная справка о боте.
# settings_clear_history -> очистка истории диалогов с ИИ (2 шага).
# ------------------------------------------------------------------
# FSM-состояния (SettingsStates), используемые в этом файле:
#   wait_topics, wait_join_school, wait_join_class, wait_leave_confirm,
#   wait_edit_name/phone/email/age/password, wait_limit_uid, wait_limit_value.
# ------------------------------------------------------------------

# ============================================================
# ГОТОВЫЕ ТЕМЫ ДЛЯ ИИ (кнопки быстрого добавления)
# ============================================================
# Кортеж: (ключ для payload, название темы). Название добавляется в ai_topics.
# Ключ уходит в payload кнопки (settings_topic_add_<key>), а читаемое
# название темы — в текст кнопки и в список ai_topics пользователя.
# Порядок элементов определяет порядок кнопок на экране.
TOPIC_PRESETS = [
    ("math", "Математика"),
    ("rus", "Русский язык"),
    ("physics", "Физика"),
    ("chem", "Химия"),
    ("bio", "Биология"),
    ("history", "История"),
    ("eng", "Английский язык"),
    ("prog", "Программирование"),
]

# ============================================================
# КЛАВИАТУРЫ
# ============================================================
# Все функции-«клавиатуры» строят KeyboardBuilder, который потом вызывает
# handle_settings_menu. Payload каждой кнопки однозначно ведёт в свою ветку.

def get_settings_keyboard(user_id: int) -> KeyboardBuilder:
    """
    Создаёт клавиатуру главного раздела настроек.

    Пункт «Лимиты ИИ для учеников» виден только учителям/модераторам.
    Пункты интерфейса доступны всем.
    """
    # Читаем UI-преференсы пользователя: флаг show_emoji определяет,
    # добавлять ли эмодзи к тексту кнопок.
    prefs = get_ui_prefs(user_id)
    # b() — короткая обёртка: если эмодзи выключены, возвращает текст без них.
    b = lambda s: btn(prefs["show_emoji"], s)

    kb = KeyboardBuilder()
    # Каждая кнопка — отдельный payload, все они обрабатываются ниже в
    # handle_settings_menu через if/elif по строке update.payload.
    kb.add_callback(text=b("🤖 Настройки ИИ"),          payload="settings_ai")
    # settings_ai -> ветка «Настройки ИИ» (стиль / доп. настройки / темы).
    kb.add_callback(text=b("🖥 Интерфейс"),             payload="settings_ui")
    # settings_ui -> ветка «Настройки интерфейса» (эмодзи / компакт / счётчики).
    kb.add_callback(text=b("🏫 Школа и класс"),         payload="settings_school")
    # settings_school -> раздел «Школа и класс»: заявки, выход, смена школы.
    kb.add_callback(text=b("📋 Персональные данные"),   payload="settings_personal")
    # settings_personal -> карточка с личными данными + кнопки settings_edit_*.
    kb.add_callback(text=b("🧹 Очистить историю ИИ"),   payload="settings_clear_history")
    # settings_clear_history -> запрос подтверждения в settings_clear_history_confirm.

    # Пункт «Лимиты ИИ учеников» виден только учителям/модераторам.
    # По роли из БД (db.get_user_field) решаем, добавлять ли кнопку.
    role = db.get_user_field(user_id, "role")
    if role in ("teacher", "moderator"):
        # У привилегированных ролей добавляем специфичную кнопку лимитов.
        kb.add_callback(text=b("🔧 Лимиты ИИ учеников"), payload="settings_limits")
        # settings_limits -> раздел изменения дневных лимитов ИИ (тоже только для этих ролей).
    kb.add_callback(text=b("📊 Моя статистика"),       payload="settings_stats")
    # settings_stats -> статистика запросов ИИ / заметок (для учителей — и по всем).
    kb.add_callback(text=b("ℹ️ О боте"),                payload="settings_about")
    # settings_about -> статичная справка о возможностях бота.
    kb.add_callback(text="← Назад в меню",                   payload="menu_main")
    # Возврат в главное меню — этот payload обрабатывает роутер handlers/menu.py.
    return kb.adjust(1)
    # adjust(1) — по одной кнопке в каждой строке клавиатуры.


def _ai_settings_keyboard(user_id: int) -> KeyboardBuilder:
    """Клавиатура раздела «Настройки ИИ» (стиль / доп. настройки / темы)."""
    prefs = get_ui_prefs(user_id)
    b = lambda s: btn(prefs["show_emoji"], s)
    # Префикс "_" в имени функции означает: только внутреннее использование.

    kb = KeyboardBuilder()
    # Три подраздела настроек ИИ + выход в корень настроек.
    kb.add_callback(text=b("💬 Стиль ответа"),    payload="settings_ai_style")
    # settings_ai_style -> выбор стиля ИИ (вниз: settings_style_simple/detailed/fun).
    kb.add_callback(text=b("🧩 Дополнительно"),   payload="settings_ai_extra")
    # settings_ai_extra -> доп. настройки: наводящие вопросы, примеры, подробность.
    kb.add_callback(text=b("🎯 Темы и интересы"), payload="settings_topics")
    # settings_topics -> управление темами ИИ (добавить/очистить/ввести свои).
    kb.add_callback(text="← Назад",                    payload="menu_settings")
    # Возврат к «корню» настроек (главное меню настроек).
    return kb.adjust(1)


def _ai_style_keyboard(user_id: int) -> KeyboardBuilder:
    """Клавиатура выбора стиля ответа ИИ."""
    prefs = get_ui_prefs(user_id)
    b = lambda s: btn(prefs["show_emoji"], s)

    kb = KeyboardBuilder()
    # Три фиксированных стиля: каждый соответствует значению поля ai_style в БД.
    kb.add_callback(text=b("💬 Простой"),    payload="settings_style_simple")
    # settings_style_simple -> пишет в БД ai_style="simple".
    kb.add_callback(text=b("📖 Подробный"),  payload="settings_style_detailed")
    # settings_style_detailed -> пишет в БД ai_style="detailed".
    kb.add_callback(text=b("😄 Интересный"), payload="settings_style_fun")
    # settings_style_fun -> пишет в БД ai_style="fun".
    kb.add_callback(text="← Назад",                 payload="settings_ai")
    # Возврат в раздел «Настройки ИИ».
    return kb.adjust(1)


def _ai_extra_keyboard(user_id: int) -> KeyboardBuilder:
    """Клавиатура дополнительных настроек ИИ (значения меняются динамически)."""
    prefs = get_ui_prefs(user_id)
    # b() — обёртка над btn(): добавляет эмодзи только если они включены.
    b = lambda s: btn(prefs["show_emoji"], s)

    user = db.get_user(user_id) or {}
    # Читаем текущие значения AI-тумблеров, чтобы показать их в подписях кнопок.
    # Значения по умолчанию берутся из config (AI_FOLLOWUP_DEFAULT и т.д.),
    # если у пользователя поле ещё не задано.
    followup = bool(user.get("ai_followup", AI_FOLLOWUP_DEFAULT))
    # followup — включены ли наводящие вопросы в конце ответа ИИ.
    examples = bool(user.get("ai_examples", AI_EXAMPLES_DEFAULT))
    # examples — добавляет ли ИИ примеры и аналогии в ответы.
    detail = user.get("ai_detail", AI_DETAIL_DEFAULT) or AI_DETAIL_DEFAULT
    # detail — степень подробности: short / normal / detail.

    # ✅/❌ — наглядная индикация текущего состояния тумблера на кнопке.
    followup_label = "✅ Да" if followup else "❌ Нет"
    examples_label = "✅ Да" if examples else "❌ Нет"
    # Текстовые ярлыки для трёх степеней подробности ответа.
    detail_label = {
        "short": "Кратко",
        "detail": "Подробно",
        "normal": "Обычно",
    }.get(detail, "Обычно")
    # Если в БД лежит неизвестное значение — показываем «Обычно».

    kb = KeyboardBuilder()
    # Кнопка-тумблер наводящих вопросов: текст меняется в зависимости от состояния.
    kb.add_callback(text=b(f"🗨 Наводящие вопросы: {followup_label}"), payload="settings_ai_followup")
    # settings_ai_followup -> инвертирует ai_followup в БД.
    kb.add_callback(text=b(f"💡 Примеры в ответе: {examples_label}"),  payload="settings_ai_examples")
    # settings_ai_examples -> инвертирует ai_examples в БД.
    kb.add_callback(text=b(f"📏 Подробность: {detail_label}"),        payload="settings_ai_length")
    # settings_ai_length -> циклично меняет ai_detail: short -> normal -> detail.
    kb.add_callback(text="← Назад", payload="settings_ai")
    return kb.adjust(1)


def _ui_settings_keyboard(user_id: int) -> KeyboardBuilder:
    """Клавиатура настроек интерфейса (тексты меняются в зависимости от значений)."""
    prefs = get_ui_prefs(user_id)
    b = lambda s: btn(prefs["show_emoji"], s)

    # Подписи кнопок зависят от НАСТОЯЩИХ значений UI-преференсов,
    # поэтому текст на кнопке показывает текущее состояние (✅/❌).
    emoji_label   = "✅ Включены" if prefs["show_emoji"] else "❌ Выключены"
    # emoji_label — подпись для тумблера эмодзи в меню.
    compact_label = "✅ Включён" if prefs["compact_mode"] else "❌ Выключен"
    # compact_label — подпись для тумблера компактного режима меню.
    stats_label   = "✅ Включены" if prefs["show_stats"] else "❌ Выключены"
    # stats_label — подпись для тумблера счётчиков в меню.

    kb = KeyboardBuilder()
    # Три независимых UI-тумблера: эмодзи в меню, компактный режим, счётчики.
    kb.add_callback(text=b(f"🎨 Эмодзи: {emoji_label}"),      payload="settings_ui_emoji")
    # settings_ui_emoji -> инвертирует show_emoji через set_ui_pref.
    kb.add_callback(text=b(f"📋 Компактный режим: {compact_label}"), payload="settings_ui_compact")
    # settings_ui_compact -> инвертирует compact_mode через set_ui_pref.
    kb.add_callback(text=b(f"📊 Счётчики в меню: {stats_label}"),   payload="settings_ui_stats")
    # settings_ui_stats -> инвертирует show_stats через set_ui_pref.
    kb.add_callback(text="← Назад", payload="menu_settings")
    return kb.adjust(1)


def _personal_keyboard(user_id: int) -> KeyboardBuilder:
    """Клавиатура раздела «Персональные данные» (редактирование полей)."""
    prefs = get_ui_prefs(user_id)
    b = lambda s: btn(prefs["show_emoji"], s)

    kb = KeyboardBuilder()
    # Пять полей для редактирования. Каждая кнопка переводит бота
    # в собственное FSM-состояние ожидания текста (см. _personal_edit_target).
    kb.add_callback(text=b("👤 Изменить имя"),     payload="settings_edit_name")
    # settings_edit_name -> FSM wait_edit_name (ждём новое полное имя).
    kb.add_callback(text=b("📱 Изменить телефон"), payload="settings_edit_phone")
    # settings_edit_phone -> FSM wait_edit_phone (ждём новый телефон).
    kb.add_callback(text=b("📧 Изменить email"),   payload="settings_edit_email")
    # settings_edit_email -> FSM wait_edit_email (ждём email; «—» = очистить).
    kb.add_callback(text=b("🎂 Изменить возраст"), payload="settings_edit_age")
    # settings_edit_age -> FSM wait_edit_age (число; 0 = очистить).
    kb.add_callback(text=b("🔒 Изменить пароль"),  payload="settings_edit_password")
    # settings_edit_password -> FSM wait_edit_password (мин. 4 символа).
    kb.add_callback(text="← Назад", payload="menu_settings")
    return kb.adjust(1)


def _cancel_personal_keyboard() -> KeyboardBuilder:
    """Клавиатура «Отмена» при редактировании персональных данных."""
    kb = KeyboardBuilder()
    # Отмена сбрасывает FSM-ввод и возвращает в карточку «Персональные данные»
    # (payload settings_personal). FSM при этом не очищается принудительно —
    # ветка settings_personal сама вызывает fsm_context.clear().
    kb.add_callback(text="← Отмена", payload="settings_personal")
    return kb


# ============================================================
# КЛАВИАТУРЫ РАЗДЕЛА «ШКОЛА И КЛАСС»
# ============================================================

def _cancel_school_keyboard() -> KeyboardBuilder:
    """
    Клавиатура «Отмена» для процессов заявки на вступление.

    Одна короткая кнопка; возврат в раздел «Школа и класс».
    """
    kb = KeyboardBuilder()
    # Отмена FSM-ввода школы/класса: payload settings_school ведёт обратно
    # в раздел, а сам обработчик этой ветки вызывает fsm_context.clear().
    kb.add_callback(text="← Отмена", payload="settings_school")
    return kb


def _school_keyboard(user_id: int) -> KeyboardBuilder:
    """
    Клавиатура раздела «Школа и класс».

    Доступные действия зависят от текущих данных пользователя:
      • если школа НЕ указана — кнопка «Подать заявку на вступление»
        (ученик хочет присоединиться к школе и классу);
      • если школа указана — кнопки «Перейти в другую школу» и
        «Выйти из школы»;
      • для учителей/модераторов — кнопка «Заявки на вступление»
        (подтвердить/отклонить просьбы учеников).

    Args:
        user_id: Числовой ID пользователя.
    """
    user = db.get_user(user_id) or {}
    # Берём актуальную школу и роль пользователя из БД — от них зависит,
    # какие кнопки показать в разделе «Школа и класс».
    school = (user.get("school") or "").strip()
    # school — название школы (пустая строка, если не привязан).
    role = user.get("role", "")
    # role — роль пользователя: student / teacher / moderator.

    b = lambda s: btn(get_ui_prefs(user_id)["show_emoji"], s)

    kb = KeyboardBuilder()
    if school:
        # Уже в школе: можно перейти в другую или выйти из этой.
        kb.add_callback(text=b("🔄 Перейти в другую школу"), payload="settings_join")
        # settings_join -> FSM wait_join_school (ввод названия новой школы).
        kb.add_callback(text=b("🚪 Выйти из школы"), payload="settings_leave")
        # settings_leave -> FSM wait_leave_confirm (подтверждение выхода).
    else:
        # Школы нет: можно подать заявку на вступление.
        kb.add_callback(text=b("📝 Подать заявку"), payload="settings_join")
        # Тот же payload settings_join, но в контексте «заявки» для школьника.

    # Учитель/модератор видит входящие заявки на вступление в его школу.
    if role in ("teacher", "moderator"):
        kb.add_callback(text=b("📋 Заявки на вступление"), payload="settings_join_requests")
        # settings_join_requests -> панель заявок (только учителя/модераторы).

    kb.add_callback(text="← Назад", payload="menu_settings")
    return kb.adjust(1)


# ============================================================
# КОЛБЭКИ НАСТРОЕК
# ============================================================
# Начинается самый большой блок файла — единый обработчик всех колбэков,
# разбитый на если/elif-ветки по строке payload.

# Ниже одним декоратором регистрируются ВСЕ колбэки раздела настроек.
# Точные payload'ы перечислены явно, а payload'ы с суффиксом-числом
# (например settings_join_approve_<id>) — через параметр prefixes.
@router.message_callback(
    CallbackPayload(
        # Полные (точные) payload'ы кнопок, обрабатываемые этим роутером:
        "menu_settings", "settings_ai", "settings_style_simple",
        "settings_style_detailed", "settings_style_fun", "settings_topics",
        "settings_topics_edit", "settings_topics_clear", "settings_topics_type",
        "settings_stats",
        "settings_about", "settings_limits", "settings_limits_set",
        "settings_ui", "settings_ai_style", "settings_ai_extra",
        "settings_ai_followup", "settings_ai_examples", "settings_ai_length",
        "settings_ui_emoji", "settings_ui_compact", "settings_ui_stats",
        "settings_school", "settings_join", "settings_leave",
        "settings_leave_confirm", "settings_join_requests",
        "settings_personal", "settings_clear_history",
        "settings_clear_history_confirm",
        "settings_edit_name", "settings_edit_phone", "settings_edit_email",
        "settings_edit_age", "settings_edit_password",
        # Префиксные payload'ы: совпадение по НАЧАЛУ строки payload.
        # Остаток строки (например <id>) разбирается уже в самой ветке.
        prefixes=(
            "settings_join_approve_", "settings_join_reject_",
            "settings_join_skip_", "settings_limit_",
            "settings_limits_page_", "settings_topic_add_",
        ),
    )
)
async def handle_settings_menu(update: MessageCallback, fsm_context) -> None:
    """Обрабатывает кнопки раздела «Настройки»."""
    # Прямая строка payload и ID пользователя (логин-ID) берутся из апдейта.
    payload = update.payload
    user_id = get_user_id(update)

    # Единый «входной фильтр»: отвечаем только существующим и подтверждённым
    # пользователям. Если заявка не одобрена модератором — молча выходим.
    if not db.user_exists(user_id) or not db.is_approved(user_id):
        return

    # UI-преференсы нужны почти каждой ветке: эмодзи в текстах и кнопках.
    prefs = get_ui_prefs(user_id)
    # e() — компактная обёртка добавления эмодзи к произвольной строке.
    e = lambda s: emoji(prefs["show_emoji"], s)

    # ------------------------------------------------------------------
    # Принцип работы обработчика:
    # 1) берём payload нажатой кнопки;
    # 2) последовательно сравниваем его с каждым известным payload'ом
    #    в цепочке if/elif — срабатывает первая совпавшая ветка;
    # 3) ветка может выполнить запись в БД (db.set_user_field / set_ui_pref),
    #    перевести бота в FSM-состояние (set_state) либо просто показать
    #    экран с клавиатурой;
    # 4) ответ всегда отправляется через update.edit_message(...) и/или
    #    callback_answer(...) (всплывающая плашка).
    # ------------------------------------------------------------------

    # ============================================================
    # ГЛАВНОЕ МЕНЮ НАСТРОЕК
    # ============================================================
    # Payload "menu_settings" — «корень» раздела настроек. Показывает
    # клавиатуру get_settings_keyboard() и используется как точка возврата.
    if payload == "menu_settings":
        await update.callback_answer(text="Настройки")
        # Текстом ответа идёт общее приветствие раздела настроек.
        await update.edit_message(
            f"{e('⚙️')} Настройки\n\nВыбери, что настроить:",
            keyboard=get_settings_keyboard(user_id).build(),
        )
        # Остальные ветки не выполняются — следующая ветка elif.

    # ------------------------------------------------------------------
    # Итог ветки "menu_settings": сбрасывается FSM, показывается корень
    # настроек. Никакие данные в БД не пишутся — только ответ пользователю.
    # ------------------------------------------------------------------
    # ============================================================
    # ПЕРСОНАЛЬНЫЕ ДАННЫЕ
    # ============================================================
    # Карточка с текущими личными данными + кнопки редактирования.
    # В БД записей не происходит; ветка только формирует текст карточки.
    elif payload == "settings_personal":
        await fsm_context.clear()
        # Сбрасываем любое активное FSM-состояние: мы выходим из режимов ввода.
        user = db.get_user(user_id) or {}
        # Достаём все поля из БД; пустые значения подменяем «не указано».
        name = user.get("full_name") or user.get("name") or "не указано"
        # name — сначала полное имя, затем ник из MAX, иначе «не указано».
        phone = user.get("phone") or "не указан"
        email = user.get("email") or "не указан"
        age = user.get("age") or 0
        password = user.get("password") or ""
        # Пароль не показываем открытым текстом — только заглушку.
        password_masked = "••••" if password else "не задан"
        # password_masked — маскируем пароль: точки либо «не задан».

        # Собираем единый текст карточки по всем полям пользователя.
        text = (
            f"{e('📋')} Персональные данные\n\n"
            f"{e('👤')} Имя: {name}\n"
            f"{e('📱')} Телефон: {phone}\n"
            f"{e('📧')} Email: {email}\n"
            f"{e('🎂')} Возраст: {age if age else 'не указан'}\n"
            f"{e('🔒')} Пароль: {password_masked}\n\n"
            "Выбери поле, которое хочешь изменить:"
        )
        await update.callback_answer(text="Данные")
        # Перерисовываем сообщение с клавиатурой редактирования полей.
        await update.edit_message(text, keyboard=_personal_keyboard(user_id).build())

    # ------------------------------------------------------------------
    # Итог ветки "settings_personal": FSM очищен, показана карточка с
    # именем/телефоном/email/возрастом (пароль маскируется). Запись в БД
    # произойдёт лишь после FSM-ввода нового значения в handle_settings_text.
    # ------------------------------------------------------------------
    # ============================================================
    # ОЧИСТИТЬ ИСТОРИЮ ИИ
    # ============================================================
    # Первый шаг: показываем предупреждение и просим подтверждение.
    # БД пока не трогаем — ждём нажатия кнопки подтверждения.
    elif payload == "settings_clear_history":
        # Сбрасываем FSM на случай, если до этого был незавершённый ввод.
        await fsm_context.clear()
        await update.callback_answer(text="Очистить?")
        kb = KeyboardBuilder()
        # Подтверждение ведёт на settings_clear_history_confirm, отмена — в меню.
        kb.add_callback(text="✅ Да, очистить", payload="settings_clear_history_confirm")
        kb.add_callback(text="❌ Отмена", payload="menu_settings")
        await update.edit_message(
            "🧹 Очистить всю историю запросов к ИИ?\n\n"
            "Это удалит записи о твоих вопросах и ответах навсегда.\n"
            "Статистика использования ИИ тоже обнулится.",
            keyboard=kb.adjust(1).build(),
        )

    # ------------------------------------------------------------------
    # Итог ветки "settings_clear_history": FSM сброшен, показан запрос
    # подтверждения. Удаление из БД отложено до нажатия кнопки «Да, очистить».
    # ------------------------------------------------------------------
    # ============================================================
    # ОЧИСТИТЬ ИСТОРИЮ ИИ (второй шаг): фактическое удаление из БД
    # ============================================================
    # Второй шаг: фактическая очистка. Возврат в главное меню.
    elif payload == "settings_clear_history_confirm":
        # Удаляем логи диалогов с ИИ и обнуляем счётчики использования.
        db.clear_ai_logs_for_user(user_id)
        # Первый вызов — физическое удаление всех запросов/ответов из логов.
        db.reset_ai_usage(user_id)
        # Второй вызов — сброс счётчика израсходованных запросов за день.
        await update.callback_answer(text="Очищено")
        kb = KeyboardBuilder()
        # После очистки предлагаем единственное действие — в главное меню.
        kb.add_callback(text="← Назад в меню", payload="menu_main")
        await update.edit_message(
            "🧹 История запросов к ИИ очищена!\n\n"
            "Все вопросы и ответы удалены.",
            keyboard=kb.build(),
        )

    # ============================================================
    # РЕДАКТИРОВАНИЕ ПЕРСОНАЛЬНЫХ ДАННЫХ (переход к FSM-вводу)
    # ============================================================
    # Одна группа payload'ов settings_edit_name/phone/email/age/password.
    # _personal_edit_target() преобразует payload -> (поле в БД, State FSM).
    elif payload in (
        "settings_edit_name", "settings_edit_phone", "settings_edit_email",
        "settings_edit_age", "settings_edit_password",
    ):
        # Узнаём, какое поле БД редактируем и в какое FSM-состояние войти.
        db_field, state = _personal_edit_target(payload)
        # Входим в FSM: сначала очищаем старое состояние, потом запоминаем,
        # какое поле редактируем (edit_field) и переводим автомат в нужное состояние.
        await fsm_context.clear()
        await fsm_context.update_data(edit_field=db_field)
        # update_data сохраняет имя поля во FSM-данные для следующего шага.
        await fsm_context.set_state(state)
        # set_state переводит автомат: теперь текст пойдёт в handle_settings_text.
        # После этого шага бот "заморожен" в ожидании ТОЛЬКО текстового ввода
        # пользователя — любые кнопки игнорируются до ввода или отмены.

        # Подсказки ввода для каждого поля; для email/age есть спец. значения
        # «-» (очистить email) и «0» (очистить возраст).
        prompts = {
            "full_name": "👤 Напиши новое имя (имя и фамилия):",
            "phone": "📱 Напиши новый номер телефона (например: +79001234567):",
            "email": "📧 Напиши новый email (например: ivan@mail.ru):\n\n«—» — очистить email",
            "age": "🎂 Напиши новый возраст числом (например: 12):\n\n«0» — очистить возраст",
            "password": "🔒 Придумай новый пароль (минимум 4 символа):",
        }
        await update.callback_answer(text="Ввод")
        await update.edit_message(
            prompts.get(db_field, "Введи новое значение:"),
            # При ошибке/отмене можно вернуться к карточке данных.
            keyboard=_cancel_personal_keyboard(),
        )

    # ============================================================
    # НАСТРОЙКИ ИИ
    # ============================================================
    # Сводный экран: показывает текущий стиль, тумблеры и темы,
    # кнопки ведут на подразделы (settings_ai_style/extra/topics).
    elif payload == "settings_ai":
        # Каждая точка входа в «корень» сбрасывает FSM от прошлых операций.
        await fsm_context.clear()

        user = db.get_user(user_id)
        # Читаем все AI-настройки сразу, чтобы показать их в одном тексте.
        current_style = user.get("ai_style", "simple") if user else "simple"
        # current_style — текущее значение стиля из БД (по умолчанию simple).
        topics = user.get("ai_topics", "") if user else ""
        # topics — строка интересов через запятую (может быть пустой).
        followup = bool(user.get("ai_followup", AI_FOLLOWUP_DEFAULT)) if user else AI_FOLLOWUP_DEFAULT
        # флаг наводящих вопросов.
        examples = bool(user.get("ai_examples", AI_EXAMPLES_DEFAULT)) if user else AI_EXAMPLES_DEFAULT
        # флаг примеров в ответе.
        detail = user.get("ai_detail", AI_DETAIL_DEFAULT) if user else AI_DETAIL_DEFAULT
        # степень подробности ответа.

        # Сопоставляем хранимое значение стиля с человекочитаемой подписью.
        style_text = {
            "simple":    "💬 Простой",
            "detailed":  "📖 Подробный",
            "fun":       "😄 Интересный",
        }.get(current_style, "💬 Простой")
        # Неизвестный стиль подменяется дефолтом «Простой».

        # Готовим читаемые подписи для всех остальных полей сводки.
        topics_text = topics if topics else "Не заданы"
        followup_text = "✅ Да" if followup else "❌ Нет"
        examples_text = "✅ Да" if examples else "❌ Нет"
        detail_text = {
            "short": "Кратко",
            "detail": "Подробно",
            "normal": "Обычно",
        }.get(detail, "Обычно")

        # Собираем весь сводный текст из подписей выше.
        settings_text = (
            f"{e('🤖')} Настройки ИИ\n\n"
            f"{e('💬')} Стиль: {style_text}\n"
            f"{e('🗨')} Наводящие вопросы: {followup_text}\n"
            f"{e('💡')} Примеры: {examples_text}\n"
            f"{e('📏')} Подробность: {detail_text}\n"
            f"{e('🎯')} Темы: {topics_text}\n\n"
            "Что хочешь изменить?"
        )
        await update.callback_answer(text="ИИ")
        await update.edit_message(settings_text, keyboard=_ai_settings_keyboard(user_id).build())

    # ------------------------------------------------------------------
    # Итог ветки "settings_ai": сводный текст без записи в БД; все данные
    # читаются из записи пользователя и показываются как «текущее состояние».
    # ------------------------------------------------------------------
    # ============================================================
    # ВЫБОР СТИЛЯ ИИ
    # ============================================================
    # Показывает три варианта стиля (settings_style_simple/detailed/fun).
    # Состояние FSM не задействуется — выбор делается одной кнопкой.
    elif payload == "settings_ai_style":
        await fsm_context.clear()
        await update.callback_answer(text="Стиль")
        await update.edit_message(
            f"{e('💬')} Выбери стиль общения ИИ:",
            keyboard=_ai_style_keyboard(user_id).build(),
        )

    # ============================================================
    # ДОПОЛНИТЕЛЬНЫЕ НАСТРОЙКИ ИИ
    # ============================================================
    # Показывает тумблеры «наводящие вопросы / примеры» и «подробность».
    # Ветка не пишет в БД — только рисует экран с текущими значениями,
    # а сами изменения делают нижестоящие ветки settings_ai_*.
    elif payload == "settings_ai_extra":
        await fsm_context.clear()
        await update.callback_answer(text="Дополнительно")
        await update.edit_message(
            f"{e('🧩')} Дополнительные настройки ИИ\n\n"
            "Включай и выключай наводящие вопросы, примеры "
            "и подбирай подробность ответа:",
            keyboard=_ai_extra_keyboard(user_id).build(),
        )

    # ============================================================
    # ВКЛ/ВЫКЛ НАВОДЯЩИХ ВОПРОСОВ
    # ============================================================
    # Инвертируем текущее значение ai_followup и сразу записываем в БД
    # числом (1/0). После сохранения перерисовываем экран доп. настроек.
    # Запись в БД: db.set_user_field(user_id, "ai_followup", 1|0).
    elif payload == "settings_ai_followup":
        user = db.get_user(user_id) or {}
        # Логическое НЕ от текущего значения: true -> false и обратно.
        new_value = not bool(user.get("ai_followup", AI_FOLLOWUP_DEFAULT))
        # Запись в БД: 1 (включено) или 0 (выключено).
        db.set_user_field(user_id, "ai_followup", 1 if new_value else 0)
        # set_user_field пишет скалярное значение в колонку таблицы пользователя.
        await update.callback_answer(text="Сохранено")
        status = "включены" if new_value else "выключены"
        # Текст результата и обновлённая клавиатура с новым состоянием тумблера.
        await update.edit_message(
            f"✅ Наводящие вопросы {status}.\n\n"
            f"{'ИИ будет в конце ответа задавать 2-3 вопроса для углубления темы.' if new_value else 'ИИ больше не будет задавать наводящие вопросы в конце ответа.'}",
            keyboard=_ai_extra_keyboard(user_id).build(),
        )

    # ============================================================
    # ВКЛ/ВЫКЛ ПРИМЕРОВ В ОТВЕТЕ
    # ============================================================
    # Аналогично followup, но поле ai_examples (1/0) в БД.
    # Принцип одинаковый: НЕ от текущего значения -> запись -> перерисовка.
    elif payload == "settings_ai_examples":
        user = db.get_user(user_id) or {}
        # Инвертируем текущее значение флага «примеры в ответе».
        new_value = not bool(user.get("ai_examples", AI_EXAMPLES_DEFAULT))
        # Сохраняем 1/0 в поле ai_examples пользователя.
        db.set_user_field(user_id, "ai_examples", 1 if new_value else 0)
        await update.callback_answer(text="Сохранено")
        status = "включены" if new_value else "выключены"
        await update.edit_message(
            f"✅ Примеры {status}.\n\n"
            f"{'ИИ будет добавлять в ответы примеры и аналогии из жизни.' if new_value else 'ИИ не будет использовать примеры и аналогии в ответах.'}",
            keyboard=_ai_extra_keyboard(user_id).build(),
        )

    # ============================================================
    # ПОДРОБНОСТЬ ОТВЕТА (цикл: кратко → обычно → подробно)
    # ============================================================
    # Не тумблер, а циклический переключатель: каждое нажатие сдвигает
    # ai_detail на следующую ступень в порядке short -> normal -> detail.
    # После третьей ступени снова возвращается к первой (замыкание цикла).
    elif payload == "settings_ai_length":
        user = db.get_user(user_id) or {}
        current = user.get("ai_detail", AI_DETAIL_DEFAULT) or AI_DETAIL_DEFAULT
        # current — текущая ступень подробности из БД.
        order = ("short", "normal", "detail")
        # order — порядок цикла: кратко -> обычно -> подробно.
        try:
            # Берём индекс текущего значения и увеличиваем на 1 (по кругу).
            next_idx = (order.index(current) + 1) % len(order)
            # % len(order) замыкает цикл: после "detail" снова идёт "short".
        except ValueError:
            # Если в БД лежит что-то неизвестное — стартуем с дефолта.
            next_idx = order.index(AI_DETAIL_DEFAULT)
        new_value = order[next_idx]
        # Записываем следующую ступень подробности в БД.
        db.set_user_field(user_id, "ai_detail", new_value)

        # Человекочитаемая подпись нового значения для ответа.
        label = {
            "short": "Кратко",
            "normal": "Обычно",
            "detail": "Подробно",
        }.get(new_value, "Обычно")
        await update.callback_answer(text="Сохранено")
        await update.edit_message(
            f"✅ Подробность ответа: {label}.\n\n"
            "Изменить ещё раз — нажми кнопку «Подробность».",
            keyboard=_ai_extra_keyboard(user_id).build(),
        )

    # ============================================================
    # СМЕНА СТИЛЯ ИИ
    # ============================================================
    # Три отдельные ветки: каждая просто пишет фиксированный стиль
    # (simple/detailed/fun) в поле ai_style БД и подтверждает выбор.
    # FSM не используется — стиль применяется немедленно по нажатию кнопки.
    elif payload == "settings_style_simple":
        # Записываем «простой» стиль в БД.
        db.set_user_field(user_id, "ai_style", "simple")
        await update.callback_answer(text="Сохранено")
        await update.edit_message(
            "✅ Стиль изменён на: 💬 Простой\n\n"
            "ИИ будет отвечать кратко и понятно.",
            keyboard=_ai_style_keyboard(user_id).build(),
        )

    elif payload == "settings_style_detailed":
        # Записываем «подробный» стиль в БД.
        db.set_user_field(user_id, "ai_style", "detailed")
        await update.callback_answer(text="Сохранено")
        await update.edit_message(
            "✅ Стиль изменён на: 📖 Подробный\n\n"
            "ИИ будет давать развёрнутые ответы с примерами.",
            keyboard=_ai_style_keyboard(user_id).build(),
        )

    elif payload == "settings_style_fun":
        # Записываем «интересный» стиль в БД.
        db.set_user_field(user_id, "ai_style", "fun")
        await update.callback_answer(text="Сохранено")
        await update.edit_message(
            "✅ Стиль изменён на: 😄 Интересный\n\n"
            "ИИ будет использовать юмор и интересные факты.",
            keyboard=_ai_style_keyboard(user_id).build(),
        )

    # ============================================================
# ИНТЕРЕСУЮЩИЕ ТЕМЫ
# ============================================================
# Обзорный экран: показывает текущий список тем и кнопки управления
# (добавить из списка / ввести свои, очистить, назад).
# Чтение из БД: поле ai_topics; запись происходит в ветках ниже.
    elif payload == "settings_topics":
        await fsm_context.clear()
        user = db.get_user(user_id)
        topics = user.get("ai_topics", "") if user else ""
        # Условная строка: если темы есть — показываем их список.
        topics_text = f"Текущие темы: {topics}\n\n" if topics else "Темы пока не заданы.\n\n"

        b = lambda s: btn(prefs["show_emoji"], s)

        kb = KeyboardBuilder()
        kb.add_callback(text=b("📝 Добавить из списка / ввести свои"), payload="settings_topics_edit")
        # settings_topics_edit -> показывает готовые темы TOPIC_PRESETS и вход в FSM wait_topics.
        if topics:
            # Кнопка «Очистить» показывается только если темы уже заданы.
            kb.add_callback(text=b("🧹 Очистить темы"), payload="settings_topics_clear")
            # settings_topics_clear -> пишет пустую строку в ai_topics.
        kb.add_callback(text="← Назад", payload="settings_ai")

        await update.callback_answer(text="Темы")
        # Текст-инструкция: как задать темы (готовыми кнопками или текстом).
        await update.edit_message(
            f"{e('🎯')} Интересные темы\n\n"
            f"{topics_text}"
            "Темы помогают ИИ лучше понимать, "
            "о чём ты хочешь говорить.\n\n"
            "Как задать:\n• выбрать готовые темы кнопками ниже\n"
            "• или написать свои через запятую",
            keyboard=kb.build(),
        )

    # ============================================================
# ДОБАВИТЬ/ВВЕСТИ ТЕМЫ
# ============================================================
# Показывает кнопки быстрого добавления (settings_topic_add_<key>) и
# переводит пользователя в FSM wait_topics — чтобы можно было просто
# написать темы текстом, не нажимая кнопок.
# ВАЖНО: здесь устанавливается FSM-состояние SettingsStates.wait_topics,
# поэтому следующее текстовое сообщение уйдёт в handle_settings_text.
    elif payload == "settings_topics_edit":
        await fsm_context.clear()
        # Установка FSM-состояния: следующий текст пойдёт в wait_topics.
        await fsm_context.set_state(SettingsStates.wait_topics)

        b = lambda s: btn(prefs["show_emoji"], s)

        kb = KeyboardBuilder()
        # Каждая готовая тема — кнопка с payload settings_topic_add_<key>.
        for key, topic in TOPIC_PRESETS:
            # Цикл по всем пресетам: собираем кнопки быстрого добавления.
            kb.add_callback(text=b(f"➕ {topic}"), payload=f"settings_topic_add_{key}")
        kb.add_callback(text=b("✏️ Ввести свои темы"), payload="settings_topics_type")
        # settings_topics_type -> тот же FSM wait_topics, но с чистой подсказкой.
        kb.add_callback(text="← Отмена", payload="settings_topics")

        await update.callback_answer(text="Ввод")
        # Пример формата ввода своих тем через запятую.
        await update.edit_message(
            "🎯 Выбери готовую тему кнопкой или напиши свои темы "
            "через запятую.\n\n"
            "Пример:\nМатематика, программирование, космос, музыка",
            keyboard=kb.adjust(1).build(),
        )

    # ============================================================
    # БЫСТРОЕ ДОБАВЛЕНИЕ ГОТОВОЙ ТЕМЫ
    # ============================================================
    # Payload вида settings_topic_add_<key>, где key — ключ из TOPIC_PRESETS.
    # Достаём название темы по ключу и добавляем к списку ai_topics.
    # Итоговая запись в БД: db.set_user_field(user_id, "ai_topics", "...")
    # со склейкой старого и нового списка через запятую с защитой от дублей.
    elif payload.startswith("settings_topic_add_"):
        key = payload.split("_", 3)[-1]
        # key — последний фрагмент payload (например "math" из settings_topic_add_math).
        # Ищем в TOPIC_PRESETS запись, у которой ключ совпадает с нашей.
        topic = next((t for k, t in TOPIC_PRESETS if k == key), None)
        # next() возвращает название темы либо None, если ключ не найден.
        if topic is None:
            # Неизвестный ключ — молча игнорируем (не трогаем данные).
            return

        await fsm_context.clear()

        user = db.get_user(user_id) or {}
        current = user.get("ai_topics", "") or ""
        # Разбиваем старый список по запятым, чистим пустые куски.
        parts = [p.strip() for p in current.split(",") if p.strip()]
        # parts — список существующих тем (без пустых элементов).
        # Защита от дублей: добавляем тему только если её ещё нет (без учёта регистра).
        if not any(p.lower() == topic.lower() for p in parts):
            parts.append(topic)
        # Записываем обновлённый список обратно в БД (одной строкой через ", ").
        db.set_user_field(user_id, "ai_topics", ", ".join(parts))

        await update.callback_answer(text="Добавлено")
        topics_text = f"Твои темы: {', '.join(parts)}\n\n" if parts else ""
        await update.edit_message(
            f"✅ Тема «{topic}» добавлена!\n\n{topics_text}"
            "Можно добавить ещё одну или ввести свои.",
            keyboard=_ai_settings_keyboard(user_id).build(),
        )

    # ============================================================
    # ОЧИСТКА ТЕМ
    # ============================================================
    # Просто записывает пустую строку в ai_topics.
    # Запись в БД: db.set_user_field(user_id, "ai_topics", "").
    # Немедленный эффект: ИИ перестаёт учитывать прежние темы.
    elif payload == "settings_topics_clear":
        db.set_user_field(user_id, "ai_topics", "")
        # Пустая строка в БД означает «тем нет».
        await update.callback_answer(text="Очищено")
        await update.edit_message(
            "🧹 Темы очищены.\n\n"
            "Теперь ИИ будет отвечать без учёта конкретных интересов.",
            keyboard=_ai_settings_keyboard(user_id).build(),
        )

    # ============================================================
    # ВВОД СВОИХ ТЕМ (переход к FSM-вводу)
    # ============================================================
    # Устанавливает то же состояние wait_topics, что и settings_topics_edit,
    # но без кнопок готовых тем — только поле для свободного ввода.
    # FSM-состояние: SettingsStates.wait_topics (текст -> ai_topics).
    elif payload == "settings_topics_type":
        await fsm_context.clear()
        await fsm_context.set_state(SettingsStates.wait_topics)
        # Снова wait_topics: текст будет целиком записан в ai_topics.

        kb = KeyboardBuilder()
        # Одна кнопка отмены; ввод тем — только текстом.
        kb.add_callback(text="← Отмена", payload="settings_topics")

        await update.callback_answer(text="Ввод")
        await update.edit_message(
            "🎯 Напиши свои интересные темы через запятую.\n\n"
            "Пример:\nМатематика, программирование, космос, музыка",
            keyboard=kb.build(),
        )

    # ============================================================
    # НАСТРОЙКИ ИНТЕРФЕЙСА
    # ============================================================
    # Обзорный экран UI-настроек; сами тумблеры обрабатываются ниже.
    # Запись в БД не производится — только отрисовка экрана и клавиатуры;
    # изменять значения интерфейса будут ветки settings_ui_emoji/compact/stats.
    elif payload == "settings_ui":
        await update.callback_answer(text="Интерфейс")
        # Простой экран-обёртка над клавиатурой UI-настроек.
        await update.edit_message(
            f"{e('🖥')} Настройки интерфейса\n\n"
            "Здесь ты можешь настроить внешний вид бота под себя.",
            keyboard=_ui_settings_keyboard(user_id).build(),
        )

    # Тумблер «Эмодзи»: инвертируем show_emoji и сохраняем через set_ui_pref
    # (преференсы хранятся отдельно от основных данных — см. handlers/prefs.py).
    elif payload == "settings_ui_emoji":
        current = prefs["show_emoji"]
        # Инвертируем текущее значение флага эмодзи.
        set_ui_pref(user_id, "show_emoji", not current)
        # set_ui_pref сохраняет преференс в отдельное хранилище UI-настроек.
        status = "включены" if not current else "выключены"
        await update.callback_answer(text="Сохранено")
        await update.edit_message(
            f"✅ Эмодзи {status}.\n\n"
            f"Теперь в меню и настройках {'будут отображаться' if not current else 'скрыты'} эмодзи.",
            keyboard=_ui_settings_keyboard(user_id).build(),
        )

    # Тумблер «Компактный режим»: инвертируем compact_mode.
    elif payload == "settings_ui_compact":
        current = prefs["compact_mode"]
        # Инвертируем текущее значение компактного режима.
        set_ui_pref(user_id, "compact_mode", not current)
        status = "включён" if not current else "выключен"
        await update.callback_answer(text="Сохранено")
        await update.edit_message(
            f"✅ Компактный режим {status}.\n\n"
            f"{'Главное меню теперь будет кратким — без развёрнутых подписей.' if not current else 'Главное меню снова подробное.'}",
            keyboard=_ui_settings_keyboard(user_id).build(),
        )

    # Тумблер «Счётчики в меню»: инвертируем show_stats.
    elif payload == "settings_ui_stats":
        current = prefs["show_stats"]
        # Инвертируем текущее значение флага счётчиков.
        set_ui_pref(user_id, "show_stats", not current)
        status = "включены" if not current else "выключены"
        await update.callback_answer(text="Сохранено")
        await update.edit_message(
            f"✅ Счётчики в меню {status}.\n\n"
            f"{'Теперь в главном меню будут отображаться запросы ИИ и количество заметок.' if not current else 'Счётчики скрыты — меню более компактное.'}",
            keyboard=_ui_settings_keyboard(user_id).build(),
        )

    # ============================================================
    # СТАТИСТИКА
    # ============================================================
    # Личная статистика: счётчики ИИ и заметок. Для учителей/модераторов —
    # расширенная сводка по всем пользователям БД.
    # Данные читаются из БД (db.get_ai_usage / get_notes / get_all_user_ids),
    # но ни одна запись не модифицируется — ветка чисто «читающая».
    elif payload == "settings_stats":
        user = db.get_user(user_id)
        if not user:
            return

        ai_used  = db.get_ai_usage(user_id)
        # ai_used — сколько запросов к ИИ потрачено сегодня.
        ai_limit = user.get("ai_daily_limit", 50)
        # ai_limit — дневной лимит запросов (по умолчанию 50).
        notes    = db.get_notes(user_id)
        # notes — все заметки пользователя.

        # Базовый блок личной статистики пользователя.
        stats_text = (
            f"{e('📊')} Твоя статистика\n\n"
            f"{e('🤖')} Запросов к ИИ сегодня: {ai_used}/{ai_limit}\n"
            f"{e('📝')} Заметок: {len(notes)}\n"
        )

        # Дополнительная статистика по всей базе — только для привилегированных ролей.
        if db.is_teacher(user_id) or db.is_moderator(user_id):
            all_ids = db.get_all_user_ids()
            stats_text += f"\n{e('👥')} Всего пользователей: {len(all_ids)}\n"
            # Считаем по одному через db.is_student/is_teacher — у каждой роли своя функция.
            students = sum(1 for uid in all_ids if db.is_student(uid))
            # Количество учеников в базе.
            teachers = sum(1 for uid in all_ids if db.is_teacher(uid))
            # Количество учителей в базе.
            pending  = len(db.get_pending_users())
            # Сколько заявок ещё ждут модерации.
            stats_text += (
                f"{e('📚')} Учеников: {students}\n"
                f"{e('👨‍🏫')} Учителей: {teachers}\n"
                f"{e('⏳')} Ожидают: {pending}\n"
            )

        kb = KeyboardBuilder()
        # Единственная кнопка — назад в корень настроек.
        kb.add_callback(text="← Назад", payload="menu_settings")

        await update.callback_answer(text="Статистика")
        await update.edit_message(stats_text, keyboard=kb.build())

    # ============================================================
    # О БОТЕ
    # ============================================================
    # Статичная страница-справка с версией и перечнем возможностей.
    # Никаких запросов к БД и FSM — просто текст с кнопкой «Назад».
    elif payload == "settings_about":
        # Обычный текст без обращений к БД — чистая витрина возможностей.
        about_text = (
            f"{e('ℹ️')} О боте\n\n"
            "MAX Школьный помощник — это бот, который помогает "
            "школьникам с учёбой.\n\n"
            f"{e('🤖')} Возможности:\n"
            "• Ответы на вопросы по урокам\n"
            "• Составление расписания дня\n"
            "• Создание тестов по темам\n"
            "• Помощь с домашними заданиями\n"
            "• Заметки\n"
            f"• Настройка интерфейса под себя\n\n"
            f"{e('📚')} ИИ объясняет простыми словами!\n\n"
            "Версия: 1.1.0"
        )
        kb = KeyboardBuilder()
        kb.add_callback(text="← Назад", payload="menu_settings")

        await update.callback_answer(text="О боте")
        await update.edit_message(about_text, keyboard=kb.build())

    # ============================================================
    # ШКОЛА И КЛАСС
    # ============================================================
    # Обзорный экран: текущая школа/класс + статус последней заявки на вступление.
    # Чтение из БД: school, class_name + последняя заявка пользователя.
    # Эта ветка НЕ пишет в БД — пишут ветки settings_leave_confirm и т.п.
    elif payload == "settings_school":
        await fsm_context.clear()
        user = db.get_user(user_id)
        school = (user.get("school") or "").strip() if user else ""
        class_name = (user.get("class_name") or "").strip() if user else ""
        # school/class_name — текущая школа и класс пользователя.

        # Показываем ученику статус его последней заявки на вступление.
        last_request = ""
        user_requests = db.get_user_join_requests(user_id)
        # user_requests — все заявки ученика (отсортированы, свежие первыми).
        if user_requests:
            r = user_requests[0]  # уже отсортированы: свежие первыми
            # Самая свежая заявка пользователя на вступление.
            # Маппинг статуса заявки в БД на текст для пользователя.
            status_map = {
                "pending": "⏳ Ожидает подтверждения учителя",
                "approved": "✅ Подтверждена",
                "rejected": "❌ Отклонена",
            }
            last_request = (
                f"\n{e('📋')} Твоя последняя заявка:\n"
                f"🏫 {r.get('target_school')} / {r.get('target_class')} — "
                f"{status_map.get(r.get('status'), r.get('status'))}\n"
            )
            # Неизвестный статус выводится «как есть» через второй .get().

        if school:
            # Есть школа — показываем её и класс.
            info = (
                f"{e('🏫')} Школа и класс\n\n"
                f"{e('🏫')} Школа: {school}\n"
                f"{e('📚')} Класс: {class_name or '—'}\n"
            )
        else:
            # Школы нет — предлагаем подать заявку.
            info = (
                f"{e('🏫')} Школа и класс\n\n"
                "Сейчас ты не привязан к школе.\n"
                "Подай заявку — учитель подтвердит её.\n"
            )

        # Дописываем подсказку о последней заявке (если она была).
        info += last_request

        await update.callback_answer(text="Школа")
        await update.edit_message(
            info + "\nЧто хочешь сделать?",
            keyboard=_school_keyboard(user_id).build(),
        )

    # ============================================================
    # ПОДАТЬ ЗАЯВКУ НА ВСТУПЛЕНИЕ В ШКОЛУ
    # ============================================================
    # Переход в FSM wait_join_school: следующий текст пользователя будет
    # воспринят как название школы (обрабатывает handle_settings_text).
    # FSM-состояние: SettingsStates.wait_join_school -> далее wait_join_class.
    elif payload == "settings_join":
        await fsm_context.clear()
        # FSM: ждём название школы от пользователя.
        await fsm_context.set_state(SettingsStates.wait_join_school)

        await update.callback_answer(text="Заявка")
        # Инструкция: название школы + пример (учитель из этой школы утвердит заявку).
        await update.edit_message(
            "📝 Подать заявку на вступление в школу/класс\n\n"
            "🏫 Напиши название школы, в которую хочешь вступить.\n"
            "Учитель из этой школы увидит заявку и подтвердит её.\n\n"
            "Пример:\nШкола №15, г. Махачкала",
            keyboard=_cancel_school_keyboard(),
        )

    # ============================================================
    # ВЫЙТИ ИЗ ШКОЛЫ
    # ============================================================
    # Переход в FSM wait_leave_confirm: ждём явного подтверждения кнопкой.
    # FSM-состояние: SettingsStates.wait_leave_confirm.
    elif payload == "settings_leave":
        await fsm_context.clear()
        # FSM: ждём подтверждение выхода кнопкой.
        await fsm_context.set_state(SettingsStates.wait_leave_confirm)

        kb = KeyboardBuilder()
        # Подтверждение -> settings_leave_confirm, отмена -> обратно в раздел.
        kb.add_callback(text="✅ Да, выйти", payload="settings_leave_confirm")
        kb.add_callback(text="❌ Остаться", payload="settings_school")

        await update.callback_answer(text="Выход")
        # Предупреждение: личные данные (заметки, настройки) сохранятся.
        await update.edit_message(
            "🚪 Выйти из школы?\n\n"
            "Ты покинешь свою школу и класс.\n"
            "Твои заметки и настройки сохранятся.\n"
            "Потом ты сможешь подать заявку в другую школу.",
            keyboard=kb.adjust(1).build(),
        )

    # ============================================================
    # ВЫЙТИ ИЗ ШКОЛЫ (подтверждено)
    # ============================================================
    # Фактический выход: db.leave_school() обнуляет school и class_name в БД.
    # Запись в БД: db.leave_school(user_id) -> school="" + class_name="".
    elif payload == "settings_leave_confirm":
        await fsm_context.clear()
        db.leave_school(user_id)
        # leave_school стирает школу и класс из записи пользователя.
        await update.callback_answer(text="Готово")
        await update.edit_message(
            "🚪 Ты вышел из школы.\n\n"
            "Теперь ты можешь подать заявку на вступление в другую "
            "школу или класс.\n\n"
            "Твои заметки, настройки и статистика сохранились.",
            keyboard=_school_keyboard(user_id).build(),
        )

    # ============================================================
    # РАССМОТРЕНИЕ ЗАЯВОК НА ВСТУПЛЕНИЕ (учитель/модератор)
    # ============================================================
    # Доступ только учителям/модераторам. Показывает заявки, которые
    # адресованы в ШКОЛУ текущего учителя.
    # Чтение из БД: get_pending_join_requests_for_school(teacher_school).
    elif payload == "settings_join_requests":
        role = db.get_user_field(user_id, "role")
        if role not in ("teacher", "moderator"):
            # Посторонним права доступа нет — короткое сообщение и выход.
            await update.callback_answer(text="❌")
            await update.edit_message("❌ Только учитель может подтверждать заявки.")
            return

        teacher = db.get_user(user_id) or {}
        teacher_school = (teacher.get("school") or "").strip()
        # teacher_school — школа, к которой привязан учитель.

        # Учитель принимает заявки, направленные в ЕГО школу.
        # Если школа не указана — висящих заявок для него нет.
        # ВАЖНО: фильтрация по школе происходит СТОРОНОЙ БД:
        # get_pending_join_requests_for_school(teacher_school) возвращает
        # только заявки с target_school == учительской школе.
        requests = db.get_pending_join_requests_for_school(teacher_school) if teacher_school else []
        if not requests:
            await update.callback_answer(text="Заявки")
            await update.edit_message(
                "📋 Нет новых заявок на вступление в твою школу.",
                keyboard=_school_keyboard(user_id).build(),
            )
            return

        # Показываем первую (самую свежую) заявку из списка.
        await _show_join_request(update, requests, 0)

    # ============================================================
    # ПОДТВЕРДИТЬ / ОТКЛОНИТЬ ЗАЯВКУ НА ВСТУПЛЕНИЕ
    # ============================================================
    # Payload'ы вида settings_join_approve_<id> / settings_join_reject_<id>
    # делегируются общей функции _handle_join_decision (accept=True/False).
    # Внутри _handle_join_decision: запись в БД + уведомление ученика в личку.
    elif payload.startswith("settings_join_approve_"):
        # Ветка подтверждения: accept=True.
        await _handle_join_decision(update, payload, accept=True)

    elif payload.startswith("settings_join_reject_"):
        # Ветка отклонения: accept=False.
        await _handle_join_decision(update, payload, accept=False)

    # ============================================================
    # ПРОПУСТИТЬ ЗАЯВКУ
    # ============================================================
    # Payload settings_join_skip_<N> — пропускаем N-ю заявку и показываем
    # следующую. Список заявок пересчитывается заново из БД.
    elif payload.startswith("settings_join_skip_"):
        # Извлекаем индекс следующей заявки из хвоста payload.
        next_index = _split_payload_number(payload)
        if next_index is None:
            return
        teacher = db.get_user(user_id) or {}
        teacher_school = (teacher.get("school") or "").strip()
        # Заново тянем актуальный список заявок для школы учителя.
        requests = db.get_pending_join_requests_for_school(teacher_school) if teacher_school else []
        await _show_join_request(update, requests, next_index)

    # ============================================================
    # ЛИМИТЫ ИИ (для учителей и модераторов)
    # ============================================================
    # Раздел управления дневными лимитами запросов учеников к ИИ.
    # Чтение из БД: _limits_students() + get_ai_usage().
    elif payload == "settings_limits":
        role = db.get_user_field(user_id, "role")
        if role not in ("teacher", "moderator"):
            # Проверка прав — раздел только для привилегированных ролей.
            await update.callback_answer(text="❌")
            await update.edit_message("❌ Нет доступа.")
            return

        await fsm_context.clear()
        await update.callback_answer(text="Лимиты")
        # Экран со списком учеников и их текущими лимитами.
        await update.edit_message(
            f"{e('🔧')} Лимиты ИИ для учеников\n\n"
            "Выбери ученика, чтобы изменить его дневной лимит запросов к ИИ.",
            keyboard=_limits_keyboard(user_id).build(),
        )

    # ============================================================
    # Кнопка «Ввести ID ученика»: переход в FSM wait_limit_uid.
    # Один и тот же FSM-механизм, что и у face-ввода — здесь просто
    # просим ввести не название школы, а числовой ID ученика.
    elif payload == "settings_limits_set":
        role = db.get_user_field(user_id, "role")
        if role not in ("teacher", "moderator"):
            await update.callback_answer(text="❌")
            await update.edit_message("❌ Нет доступа.")
            return

        await fsm_context.clear()
        # FSM: ждём числовой ID ученика от учителя.
        await fsm_context.set_state(SettingsStates.wait_limit_uid)
        await update.callback_answer(text="ID ученика")
        # Просим ввести числовой ID ученика текстом.
        await update.edit_message(
            f"{e('🔢')} Введи ID ученика, которому нужно изменить лимит.\n\n"
            "ID виден учителю в панели модератора (колонка «ID»).\n\n"
            "Пример:\n123456789",
            keyboard=_cancel_limits_keyboard(),
        )

    # Пагинация списка учеников: payload settings_limits_page_<номер страницы>.
    elif payload.startswith("settings_limits_page_"):
        # ВАЖНО: ветка обслуживает листание списка — пересчитывает страницу
        # и перерисовывает текст и клавиатуру, не производя записей в БД.
        try:
            # Номер страницы берём из хвоста payload и не даём уйти в минус.
            page = max(int(payload.rsplit("_", 1)[-1]), 0)
        except ValueError:
            # Некорректный номер страницы — молча игнорируем.
            return
        await update.callback_answer(text="Страница")
        students = _limits_students()
        # Вычисляем число страниц (размер страницы — _LIMITS_PAGE_SIZE).
        page_count = max((len(students) + _LIMITS_PAGE_SIZE - 1) // _LIMITS_PAGE_SIZE, 1)
        if page >= page_count:
            # Страница вышла за границу — прижимаем к последней.
            page = page_count - 1
        text = _limits_text(students, page)
        await update.edit_message(text, keyboard=_limits_keyboard(user_id, students=students, page=page).build())

    # Кнопка конкретного ученика: payload settings_limit_<user_id>.
    # Открывает FSM wait_limit_value для ввода нового лимита.
    # FSM-состояние: SettingsStates.wait_limit_value.
    elif payload.startswith("settings_limit_"):
        role = db.get_user_field(user_id, "role")
        if role not in ("teacher", "moderator"):
            await update.callback_answer(text="❌")
            return

        # Извлекаем ID целевого ученика из payload кнопки.
        target_id = _split_payload_number(payload)
        if target_id is None or not db.user_exists(target_id):
            await update.callback_answer(text="❌")
            await update.edit_message("❌ Ученик не найден.")
            return

        target = db.get_user(target_id) or {}
        name = target.get("full_name") or target.get("name", "Ученик")
        current_limit = target.get("ai_daily_limit", 50)

        # Сохраняем ID цели во FSM (limit_target) — понадобится на шаге ввода.
        # Возможны два пути сюда: 1) кнопка ученика в списке (settings_limit_<id>),
        # 2) ручной ввод ID (wait_limit_uid -> wait_limit_value). Путь различается
        # только происхождением target_id — результат идентичен (ждём число лимита).
        await fsm_context.clear()
        await fsm_context.set_state(SettingsStates.wait_limit_value)
        # update_data: сохраняем ID ученика, чей лимит будем менять.
        await fsm_context.update_data(limit_target=target_id)

        await update.callback_answer(text="Лимит")
        # Показываем текущее значение лимита и просим ввести новое число.
        await update.edit_message(
            f"{e('🔧')} Изменение лимита ИИ\n\n"
            f"{e('👤')} Ученик: {name}\n"
            f"{e('📊')} Текущий лимит: {current_limit} запросов в день\n\n"
            "Введи новое число от 1 до 999.\n"
            "0 — полностью запретить запросы к ИИ.",
            keyboard=_cancel_limits_keyboard(),
        )


# ============================================================
# ЛИМИТЫ ИИ — ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ
# ============================================================

# Сколько учеников показывается на одной странице списка лимитов.
_LIMITS_PAGE_SIZE = 8


def _limits_students() -> list[int]:
    """Возвращает ID всех учеников (для раздела «Лимиты ИИ»)."""
    # Берём все ID из БД и оставляем только тех, у кого роль student
    # (db.is_student). Учителя/модераторы в список для лимитов не попадают.
    # Возвращается ТОЛЬКО список ID — имена/лимиты подтягиваются отдельно.
    all_ids = db.get_all_user_ids()
    # Списочное выражение-фильтр: оставляем ID, для которых is_student True.
    return [uid for uid in all_ids if db.is_student(uid)]


def _limits_text(students: list[int], page: int = 0) -> str:
    """Текст страницы со списком учеников и их лимитами."""
    if not students:
        # Пустая база — просто информативное сообщение.
        return "🔧 В школе пока нет учеников."

    # «Нарезаем» полный список на страницу: page * размер .. page * размер + размер.
    start = page * _LIMITS_PAGE_SIZE
    chunk = students[start : start + _LIMITS_PAGE_SIZE]
    # chunk — срез списка на текущей странице (до _LIMITS_PAGE_SIZE элементов).

    lines = ["🔧 Лимиты ИИ для учеников\n"]
    for sid in chunk:
        # Для каждого ученика: имя, текущий лимит (по умолчанию 50) и
        # сколько запросов уже израсходовано сегодня.
        u = db.get_user(sid) or {}
        name = u.get("full_name") or u.get("name", "Ученик")
        limit = u.get("ai_daily_limit", 50)
        used = db.get_ai_usage(sid)
        # Строка вида: "🎓 Имя — использовано/лимит".
        lines.append(f"🎓 {name} — {used}/{limit}")

    # Счётчик страниц показываем, только если страницы реально есть.
    page_count = max((len(students) + _LIMITS_PAGE_SIZE - 1) // _LIMITS_PAGE_SIZE, 1)
    if page_count > 1:
        lines.append(f"\nСтраница {page + 1} из {page_count}")
    return "\n".join(lines)


def _limits_keyboard(
    user_id: int,
    students: list[int] | None = None,
    page: int = 0,
) -> KeyboardBuilder:
    """Клавиатура со списком учеников и управлением лимитами ИИ."""
    if students is None:
        # Если список не передан — пересчитываем заново из БД.
        students = _limits_students()

    kb = KeyboardBuilder()
    start = page * _LIMITS_PAGE_SIZE
    chunk = students[start : start + _LIMITS_PAGE_SIZE]
    for sid in chunk:
        u = db.get_user(sid) or {}
        name = u.get("full_name") or u.get("name", "Ученик")
        limit = u.get("ai_daily_limit", 50)
        used = db.get_ai_usage(sid)
        # В payload зашивается user_id ученика: settings_limit_<uid>
        # (обрабатывается веткой settings_limit_* в handle_settings_menu).
        kb.add_callback(text=f"{name} — {used}/{limit}", payload=f"settings_limit_{sid}")

    # Навигация по страницам: «⬅️» назад / «➡️» вперёд, если есть куда листать.
    page_count = max((len(students) + _LIMITS_PAGE_SIZE - 1) // _LIMITS_PAGE_SIZE, 1)
    nav = []
    if page > 0:
        # Есть предыдущая страница — добавляем кнопку «назад».
        nav.append(("⬅️", f"settings_limits_page_{page - 1}"))
    if page < page_count - 1:
        # Есть следующая страница — добавляем кнопку «вперёд».
        nav.append(("➡️", f"settings_limits_page_{page + 1}"))
    # Добавляем кнопки навигации в клавиатуру.
    for label, p in nav:
        kb.add_callback(text=label, payload=p)

    # Ручной ввод ID и кнопка обновления списка.
    kb.add_callback(text="🔢 Ввести ID ученика", payload="settings_limits_set")
    kb.add_callback(text="🔄 Обновить", payload="settings_limits")
    kb.add_callback(text="← Назад", payload="menu_settings")
    return kb.adjust(1)


def _cancel_limits_keyboard() -> KeyboardBuilder:
    """Клавиатура «Отмена» для процессов изменения лимитов ИИ."""
    kb = KeyboardBuilder()
    # Отмена в FSM-вводах лимитов: payload settings_limits возвращает
    # к списку учеников с лимитами (и очищает состояние FSM там).
    kb.add_callback(text="← Отмена", payload="settings_limits")
    return kb


def _personal_edit_target(payload: str) -> tuple[str, object]:
    """
    Возвращает пару (поле в БД, состояние FSM) для кнопки
    редактирования персональных данных.

    Args:
        payload: payload кнопки вида "settings_edit_name" и т.п.

    Returns:
        Кортеж (db_field, state).
    """
    # Словарь «payload кнопки -> (имя колонки в БД, состояние FSM-ожидания)».
    # Через него кнопки редактирования превращаются в универсальный ввод:
    # поле сохраняется во FSM, а текст ждёт обработчик handle_settings_text.
    targets = {
        "settings_edit_name": ("full_name", SettingsStates.wait_edit_name),
        "settings_edit_phone": ("phone", SettingsStates.wait_edit_phone),
        "settings_edit_email": ("email", SettingsStates.wait_edit_email),
        "settings_edit_age": ("age", SettingsStates.wait_edit_age),
        "settings_edit_password": ("password", SettingsStates.wait_edit_password),
    }
    # Неизвестный payload не должен ломаться: падаем на имени по умолчанию.
    return targets.get(payload, ("full_name", SettingsStates.wait_edit_name))


# ============================================================
# ТЕКСТОВЫЙ ВВОД ТЕМ
# ============================================================
# Этот обработчик ловит ВСЕ текстовые сообщения, приходящие, пока активен
# один из FSM-состояний группы SettingsStates.

@router.message_created(StateFilter(SettingsStates))
async def handle_settings_text(update: MessageCreated, fsm_context) -> None:
    """
    Обрабатывает текстовый ввод в разделе «Настройки»:
      1. Интересующие темы для ИИ (wait_topics);
      2. Название школы для заявки на вступление (wait_join_school);
      3. Класс для заявки на вступление (wait_join_class).

    StateFilter(SettingsStates) — срабатывает, только когда пользователь
    находится в одном из состояний группы настроек.
    """
    user_id = get_user_id(update)
    text = update.text or ""
    if not text:
        return

    # Текущее FSM-состояние определяет, какую логику ввода применять.
    state = await fsm_context.get_state()

    # Далее идёт серия проверок state == <конкретное состояние>:
    # какая из них сработает — та логика и выполнится. Порядок важен:
    # первым обрабатывается группа состояний редактирования личных данных,
    # затем ID-лимиты, и только потом темы/школа/класс.

    # ============================================================
    # РЕДАКТИРОВАНИЕ ПЕРСОНАЛЬНЫХ ДАННЫХ
    # ============================================================
    # Обрабатываем ввод для любого из пяти полей: имя, телефон, email,
    # возраст, пароль. Конкретное поле лежит во FSM (edit_field),
    # туда его записал payload settings_edit_* ранее.
    if state in (
        SettingsStates.wait_edit_name,
        SettingsStates.wait_edit_phone,
        SettingsStates.wait_edit_email,
        SettingsStates.wait_edit_age,
        SettingsStates.wait_edit_password,
    ):
        data = await fsm_context.get_data()
        field = data.get("edit_field", "full_name")
        value = text.strip()
        # value — очищенный от пробелов ввод пользователя.
        # Дальше — по цепочке if/elif по имени поля: для каждого поля
        # своя валидация, своя запись в БД и своя подпись для ответа.

        # --- Валидация и запись имени ---
        if field == "full_name":
            if len(value) < 2:
                # Слишком короткое имя — просим ввести ещё раз (FSM не меняется).
                await update.answer_text(
                    "❌ Имя слишком короткое. Попробуй ещё раз:",
                    keyboard=_cancel_personal_keyboard(),
                )
                return
            # Минимальная длина пройдена — сохраняем полное имя.
            db.set_user_field(user_id, "full_name", value)
            label = "Имя"

        # --- Валидация и запись телефона ---
        elif field == "phone":
            if len(value) < 7 or not any(c.isdigit() for c in value):
                # Минимальная проверка: не менее 7 символов и хотя бы одна цифра.
                await update.answer_text(
                    "❌ Некорректный номер телефона. Попробуй ещё раз:",
                    keyboard=_cancel_personal_keyboard(),
                )
                return
            # Проверка пройдена — сохраняем номер телефона как есть.
            db.set_user_field(user_id, "phone", value)
            label = "Телефон"

        # --- Валидация и запись email ---
        elif field == "email":
            if value == "-":
                # Специальное значение: пользователь хочет ОЧИСТИТЬ email.
                db.set_user_field(user_id, "email", "")
                label = "Email"
            elif "@" in value and "." in value:
                # Простейшая проверка формата: есть @ и точка.
                db.set_user_field(user_id, "email", value)
                label = "Email"
            else:
                await update.answer_text(
                    "❌ Email выглядит некорректно. Попробуй ещё раз:",
                    keyboard=_cancel_personal_keyboard(),
                )
                return

        # --- Валидация и запись возраста ---
        elif field == "age":
            try:
                age = int(value)
            except ValueError:
                # Введено не число — просим повторить.
                await update.answer_text(
                    "❌ Возраст должен быть числом. Попробуй ещё раз:",
                    keyboard=_cancel_personal_keyboard(),
                )
                return
            if age == 0:
                # Специальное значение: пользователь хочет ОЧИСТИТЬ возраст.
                db.set_user_field(user_id, "age", 0)
                label = "Возраст"
            elif 1 <= age <= 120:
                # Нормальный возраст — сохраняем как есть.
                db.set_user_field(user_id, "age", age)
                label = "Возраст"
            else:
                # Вне допустимого диапазона — просим ввести ещё раз.
                await update.answer_text(
                    "❌ Возраст должен быть от 1 до 120. Попробуй ещё раз:",
                    keyboard=_cancel_personal_keyboard(),
                )
                return

        # --- Валидация и запись пароля ---
        elif field == "password":
            if len(value) < 4:
                await update.answer_text(
                    "❌ Пароль слишком короткий (минимум 4 символа). Попробуй ещё раз:",
                    keyboard=_cancel_personal_keyboard(),
                )
                return
            # Пароль сохраняется в открытом виде в БД (повторяем существующую логику).
            db.set_user_field(user_id, "password", value)
            label = "Пароль"

        else:
            # Фолбэк для неизвестных полей: записываем как есть.
            db.set_user_field(user_id, field, value)
            label = "Поле"

        # После успешной записи: выходим из FSM и показываем карточку данных.
        await fsm_context.clear()
        # clear() завершает FSM-режим: дальнейшие тексты больше не перехватываются.
        await update.answer_text(
            f"✅ {label} обновлено!\n\n"
            "Изменения сохранены.",
            keyboard=_personal_keyboard(user_id).build(),
        )
        return

    # ============================================================
    # ВВОД ID УЧЕНИКА ДЛЯ ИЗМЕНЕНИЯ ЛИМИТА ИИ
    # ============================================================
    # Состояние wait_limit_uid: ожидаем числовой ID ученика, чтобы
    # перейти к вводу нового лимита (wait_limit_value).
    # ВАЖНО: никакие данные в БД на этом шаге ещё не пишутся —
    # ID только проверяется на существование и сохраняется во FSM.
    if state == SettingsStates.wait_limit_uid:
        try:
            target_id = int(text)
        except ValueError:
            # Введено не число — просим повторить (FSM не сбрасывается).
            await update.answer_text(
                "❌ ID должен быть числом. Попробуй ещё раз.",
                keyboard=_cancel_limits_keyboard(),
            )
            return

        # Проверяем, что пользователь с таким ID существует и является учеником.
        target = db.get_user(target_id)
        if not target or not db.is_student(target_id):
            await update.answer_text(
                "❌ Ученик с таким ID не найден. Попробуй ещё раз.",
                keyboard=_cancel_limits_keyboard(),
            )
            return

        name = target.get("full_name") or target.get("name", "Ученик")
        current_limit = target.get("ai_daily_limit", 50)

        # Запоминаем ID цели во FSM (limit_target) и переходим к вводу значения.
        await fsm_context.update_data(limit_target=target_id)
        await fsm_context.set_state(SettingsStates.wait_limit_value)
        await update.answer_text(
            f"👤 Ученик: {name}\n"
            f"📊 Текущий лимит: {current_limit} запросов в день\n\n"
            "Введи новое число от 1 до 999.\n"
            "0 — полностью запретить запросы к ИИ.",
            keyboard=_cancel_limits_keyboard(),
        )
        return

    # ============================================================
    # ВВОД НОВОГО ЗНАЧЕНИЯ ЛИМИТА ИИ
    # ============================================================
    # Состояние wait_limit_value: ждём само число лимита (0—999).
    # Здесь происходит ЕДИНСТВЕННАЯ запись в БД: ai_daily_limit.
    if state == SettingsStates.wait_limit_value:
        try:
            new_limit = int(text)
        except ValueError:
            # Введено не число — просим повторить.
            await update.answer_text(
                "❌ Лимит должен быть числом. Попробуй ещё раз.",
                keyboard=_cancel_limits_keyboard(),
            )
            return

        if not (0 <= new_limit <= 999):
            # Диапазон жёстко ограничен: 0 (полный запрет) .. 999.
            await update.answer_text(
                "❌ Лимит должен быть от 0 до 999. Попробуй ещё раз.",
                keyboard=_cancel_limits_keyboard(),
            )
            return

        # Достаём сохранённый ранее ID ученика из FSM-данных.
        # limit_target сюда положили либо ветка settings_limit_<id>,
        # либо предыдущий шаг wait_limit_uid.
        data = await fsm_context.get_data()
        target_id = data.get("limit_target")
        if not target_id:
            await update.answer_text("❌ Что-то пошло не так. Попробуй ещё раз.")
            return

        target = db.get_user(target_id)
        if not target:
            await update.answer_text("❌ Ученик не найден.")
            return

        # Единственная запись в БД: новое значение дневного лимита.
        db.set_user_field(target_id, "ai_daily_limit", new_limit)
        await fsm_context.clear()

        name = target.get("full_name") or target.get("name", "Ученик")
        # Красивое описание: 0 трактуется как «полный запрет».
        description = "запросов в день" if new_limit else "запрещено (0)"
        await update.answer_text(
            f"✅ Лимит обновлён!\n\n"
            f"👤 Ученик: {name}\n"
            f"📊 Новый лимит: {new_limit} {description}\n\n"
            "Изменения действуют с сегодняшнего дня.",
            keyboard=_limits_keyboard(user_id).build(),
        )
        return

    # ============================================================
    # ВВОД ТЕМ ДЛЯ ИИ
    # ============================================================
    # Состояние wait_topics: весь введённый текст целиком идёт в ai_topics.
    # Запись в БД: db.set_user_field(user_id, "ai_topics", text) — текст
    # принимается БЕЗ валидации, как есть (включая запятые и регистр).
    if state == SettingsStates.wait_topics:
        # Пишем весь текст пользователя (как есть) в поле темы.
        db.set_user_field(user_id, "ai_topics", text)
        await fsm_context.clear()

        kb = KeyboardBuilder()
        # Кнопка подтверждения ведёт обратно в раздел «Настройки ИИ».
        kb.add_callback(text="✅ Отлично!", payload="settings_ai")

        await update.answer_text(
            f"✅ Темы сохранены!\n\n"
            f"Твои интересные темы: {text}\n\n"
            "Теперь ИИ будет учитывать твои интересы.",
            keyboard=kb.build(),
        )
        return

    # ============================================================
    # ВВОД ШКОЛЫ ДЛЯ ЗАЯВКИ НА ВСТУПЛЕНИЕ
    # ============================================================
    # Состояние wait_join_school: это ПЕРВЫЙ шаг заявки.
    # Школу пока не сохраняем в БД — кладём во FSM и просим указать класс.
    # FSM-состояние: SettingsStates.wait_join_school.
    # Никаких записей в БД на этом шаге — только валидация длины строки
    # и перемещение автомата в состояние wait_join_class.
    if state == SettingsStates.wait_join_school:
        # Сохраняем школу во временные данные FSM и просим указать класс.
        await fsm_context.update_data(join_school=text.strip())
        await fsm_context.set_state(SettingsStates.wait_join_class)
        # Следующее сообщение будет воспринято как название класса.

        await update.answer_text(
            f"🏫 Школа: {text.strip()}\n\n"
            "📚 Теперь напиши класс, в который хочешь вступить.\n"
            "Например: 9А, 5Б, 11В",
            keyboard=_cancel_school_keyboard(),
        )
        return

    # ============================================================
    # ВВОД КЛАССА И СОЗДАНИЕ ЗАЯВКИ
    # ============================================================
    # Состояние wait_join_class: второй (финальный) шаг заявки.
    # На этом шаге из FSM достаём школу, берём класс из текста
    # и создаём полноценную запись заявки в БД (join_requests).
    # fsm_context.clear() выполняется после записи — FSM завершает свою работу.
    if state == SettingsStates.wait_join_class:
        data = await fsm_context.get_data()
        school = (data.get("join_school") or "").strip()
        cls = text.strip()

        if not school:
            # Школа вдруг не сохранилась во FSM — обрываем заявку.
            await fsm_context.clear()
            await update.answer_text("Что-то пошло не так. Попробуй ещё раз.")
            return

        # Создаём заявку: статус pending + школа/класс из ввода.
        # Запись в БД делает db.create_join_request (таблица заявок на вступление).
        # Параметры: user_id (подавший заявку), school (название), cls (класс).
        db.create_join_request(user_id, school, cls)
        await fsm_context.clear()
        # FSM завершён: пользователь вернулся в обычный режим обработки кнопок.

        # Уведомляем учителей этой школы — появилась новая заявка.
        # Ищем учителей по названию школы и шлём каждому в личку сообщение.
        teachers = db.get_teachers_by_school(school)
        if teachers:
            # Бот-экземпляр импортируем локально, чтобы избежать циклов импорта.
            from bot_instance import bot
            student_user = db.get_user(user_id) or {}
            student_name = (
                student_user.get("full_name") or student_user.get("name", "Ученик")
            )
            for teacher_id in teachers:
                teacher = db.get_user(teacher_id) or {}
                chat_id = teacher.get("chat_id")
                # Если у учителя не сохранён chat_id — пропускаем (не можем написать).
                if not chat_id:
                    continue
                try:
                    await bot.send_message(
                        chat_id=chat_id,
                        text=(
                            f"📝 Новая заявка на вступление!\n\n"
                            f"👤 {student_name} хочет вступить в твою школу.\n"
                            f"🏫 Школа: {school}\n"
                            f"📚 Класс: {cls}\n\n"
                            "Проверь заявку в «Настройки → Школа и класс "
                            "→ Заявки на вступление»."
                        ),
                    )
                except Exception as e:  # noqa: BLE001
                    # Сбой отправки не должен ронять весь сценарий заявки.
                    print(f"SETTINGS: Could not notify teacher {teacher_id}: {e}")

        await update.answer_text(
            f"✅ Заявка отправлена!\n\n"
            f"🏫 Школа: {school}\n"
            f"📚 Класс: {cls}\n\n"
            "⏳ Ожидай подтверждения учителя.\n"
            "Как только учитель подтвердит заявку, ты получишь уведомление.",
            keyboard=_school_keyboard(user_id).build(),
        )
        return


# ============================================================
# ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ РАЗДЕЛА «ШКОЛА И КЛАСС»
# ============================================================

def _split_payload_number(payload: str) -> int | None:
    """
    Извлекает число из payload вида "<префикс>_<число>".

    Используется для кнопок «Пропустить заявку» (settings_join_skip_N).

    Args:
        payload: Строка колбэка.

    Returns:
        Число (индекс заявки) или None, если распарсить не удалось.
    """
    try:
        return int(payload.rsplit("_", 1)[-1])
        # rsplit("_", 1)[-1] берёт всё после ПОСЛЕДНЕГО подчёркивания,
        # поэтому корректно работает и с "settings_limits_page_3",
        # и с "settings_join_approve_42".
    except (TypeError, ValueError):
        return None


async def _show_join_request(update: MessageCallback, requests: list[dict], index: int) -> None:
    """
    Показывает одну заявку на вступление учителю.

    Args:
        update: Колбэк-апдейт (по нему отвечаем).
        requests: Список заявок (словари из get_pending_join_requests_for_school).
        index: Индекс текущей заявки в списке.
    """
    if index >= len(requests):
        # Все заявки закончились — финальное сообщение и возврат к клавиатуре школы.
        await update.callback_answer(text="Готово")
        await update.edit_message(
            "📋 Все заявки обработаны!",
            keyboard=_school_keyboard(get_user_id(update)).build(),
        )
        return

    # Берём текущую заявку и строим вокруг неё кнопки действий.
    # Каждый словарь заявки содержит: id, user_id, target_school, target_class,
    # current_school, current_class, full_name, name.
    request = requests[index]

    kb = KeyboardBuilder()
    # В payload «Принять/Отклонить» зашит ID заявки из БД:
    # settings_join_approve_<id> / settings_join_reject_<id>.
    kb.add_callback(text="✅ Принять", payload=f"settings_join_approve_{request['id']}")
    kb.add_callback(text="❌ Отклонить", payload=f"settings_join_reject_{request['id']}")
    # «Пропустить» есть, только если заявки ещё не закончились.
    if index + 1 < len(requests):
        # Пропускаем текущую заявку и переходим к следующей.
        kb.add_callback(text="⏭ Пропустить", payload=f"settings_join_skip_{index + 1}")

    await update.callback_answer(text="Заявка")
    await update.edit_message(
        f"📋 Заявка на вступление {index + 1}/{len(requests)}\n\n"
        f"👤 Ученик: {request.get('full_name') or request.get('name') or 'Неизвестно'}\n"
        f"🏫 Школа: {request.get('target_school')}\n"
        f"📚 Класс: {request.get('target_class')}\n\n"
        # Показываем, к какой школе/классу ученик привязан СЕЙЧАС
        # (это отличает «переход в другую школу» от простого вступления).
        f"Сейчас данные ученика:\n"
        f"🏫 Школа: {request.get('current_school') or '—'}\n"
        f"📚 Класс: {request.get('current_class') or '—'}",
        keyboard=kb.adjust(1).build(),
    )


async def _handle_join_decision(update: MessageCallback, payload: str, accept: bool) -> None:
    """
    Обрабатывает подтверждение/отклонение заявки на вступление учителем.

    Args:
        update: Колбэк-апдейт.
        payload: Строка вида "settings_join_approve_<id>" / "settings_join_reject_<id>".
        accept: True — принять заявку, False — отклонить.
    """
    teacher_id = get_user_id(update)
    request_id = _split_payload_number(payload)
    # Извлекаем numeric ID заявки из payload.

    if request_id is None:
        return

    # Меняем статус заявки в БД и проверяем, что она действительно найдена
    # (return-значение True, если запись была обработана и убрана из листа ожидания).
    removed = False
    if accept:
        # Подтверждаем заявку: approve_join_request меняет статус и привязывает ученика.
        removed = db.approve_join_request(request_id, teacher_id)
    else:
        # Отклоняем заявку: reject_join_request обновляет статус на rejected.
        removed = db.reject_join_request(request_id, teacher_id)

    if not removed:
        # Заявку уже обработали ранее (двойное нажатие) — сообщаем об этом.
        await update.callback_answer(text="?")
        await update.edit_message("❌ Заявка уже обработана или не найдена.")
        return

    # Достаём данные обработанной заявки для ответа и уведомления ученика.
    request = db.get_join_request_by_id(request_id)
    student_name = (
        (request.get("full_name") or request.get("name") or "Ученик")
        if request else "Ученик"
    )
    student_id = request["user_id"] if request else None

    if accept:
        await update.callback_answer(text="Принято")
        await update.edit_message(f"✅ Ученик {student_name} принят в школу и класс!")
        # Уведомляем ученика, что его приняли.
        # Уведомление идёт в личный чат через bot.send_message(chat_id=...).
        if student_id:
            from bot_instance import bot
            student = db.get_user(student_id) or {}
            chat_id = student.get("chat_id")
            if chat_id:
                # Отправляем уведомление об успешном принятии в школу.
                try:
                    await bot.send_message(
                        chat_id=chat_id,
                        text=(
                            "🎉 Твоя заявка на вступление подтверждена!\n\n"
                            f"🏫 Школа: {request.get('target_school')}\n"
                            f"📚 Класс: {request.get('target_class')}\n\n"
                            "Добро пожаловать! "
                            "Напиши /start, чтобы открыть меню."
                        ),
                    )
                except Exception as e:  # noqa: BLE001
                    # Сбой отправки — логируем, но не роняем весь flow.
                    print(f"SETTINGS: Could not notify student {student_id}: {e}")
    else:
        # Ветка отклонения: тоже пишем в БД статус и уведомляем ученика.
        await update.callback_answer(text="Отклонено")
        await update.edit_message(f"❌ Заявка от {student_name} отклонена.")
        if student_id:
            from bot_instance import bot
            student = db.get_user(student_id) or {}
            chat_id = student.get("chat_id")
            if chat_id:
                # Уведомляем ученика об отклонении.
                try:
                    await bot.send_message(
                        chat_id=chat_id,
                        text="❌ Твоя заявка на вступление в школу была отклонена учителем.",
                    )
                except Exception as e:  # noqa: BLE001
                    # Сбой — логируем.
                    print(f"SETTINGS: Could not notify student {student_id}: {e}")

    # Показываем следующую заявку, если остались.
    # Список пересчитывается заново, т.к. обработанная заявка уже удалена.
    teacher = db.get_user(teacher_id) or {}
    teacher_school = (teacher.get("school") or "").strip()
    remaining = (
        db.get_pending_join_requests_for_school(teacher_school)
        if teacher_school else []
    )
    if remaining:
        await _show_join_request(update, remaining, 0)
    else:
        await update.answer_text(
            "📋 Все заявки обработаны!",
            keyboard=_school_keyboard(teacher_id).build(),
        )