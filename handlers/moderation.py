# ============================================================
# ОБРАБОТЧИК МОДЕРАЦИИ — handlers/moderation.py
# ============================================================
# Этот файл отвечает за:
#   1. Просмотр заявок на регистрацию;
#   2. Подтверждение / отклонение пользователей;
#   3. Просмотр всех пользователей;
#   4. Блокировку/разблокировку пользователей;
#   5. Изменение дневного лимита ИИ ученику;
#   6. Массовую рассылку сообщений всем пользователям;
#   7. Просмотр лога ошибок ИИ прямо в чате;
#   8. Назначение модераторов (команда /makemod);
#   9. Уведомления модераторам и пользователям (команда /myid).
# ============================================================
# Вся логика разбита на: колбэки панели модерации (handle_moderation_menu),
# отдельный обработчик очистки лога ошибок, текстовый FSM-ввод
# (handle_moderation_text) и две команды — /makemod и /myid.
# ============================================================
# ВАЖНЫЕ ЗАМЕЧАНИЯ ПО ЧИТАЕМОСТИ ФАЙЛА:
#   - Ветвление в handle_moderation_menu идёт ПО ПОРЯДКУ elif:
#     сначала точные payload (menu_moderation, mod_pending, ...),
#     затем префиксные (mod_approve_*, mod_reject_*, mod_skip_*,
#     mod_user_*) — все обрабатываются по месту, кроме mod_user_*,
#     которые уходят в отдельную функцию _handle_user_card_action.
#   - Многие payload'ы переводят пользователя в одно из состояний
#     ModerationStates (wait_uid / wait_limit / wait_broadcast), после чего
#     реальным вводом занимается handle_moderation_text. Связь «кнопка ->
#     состояние -> обработчик текста» — ключ к пониманию этого файла.
#   - Все обращения к базе идут через module helpers из database.py
#     и никогда напрямую — так можно менять хранилище не трогая кнопки.
# ============================================================

import time

# Стандартный модуль time нужен для форматирования unix-времени,
# которое хранится в записях лога ошибок ИИ (ниже, в ветке mod_errors).
# В дальнейшем он используется через атрибут time.strftime / time.localtime.

# maxo — фреймворк бота: Router собирает обработчики сообщений/колбэков,
# Command и StateFilter — фильтры маршрутизации, MessageCallback /
# MessageCreated — типы событий, KeyboardBuilder — конструктор клавиатур.
from maxo import Router
from maxo.routing.filters import Command, StateFilter
from maxo.types import MessageCallback, MessageCreated
from maxo.utils.builders import KeyboardBuilder

# database — самодельный тонкий слой над БД (см. database.py):
# здесь мы используем get_user, get_pending_users, toggle_blocked и т.п.
import database as db
# bot_instance — глобальный экземпляр бота (для bot.send_message в рассылках).
from bot_instance import bot

# CallbackPayload — свой фильтр по точному payload кнопки либо по префиксу.
from handlers.filters import CallbackPayload
# ModerationStates — состояния FSM ввода модератора (wait_uid и др.).
from handlers.states import ModerationStates
# get_user_id — единый способ вытащить user_id из апдейта.
from handlers.user_utils import get_user_id

# Единый роутер раздела модерации.
router: Router = Router(name="moderation")


# ============================================================
# УВЕДОМЛЕНИЯ
# ============================================================
# Этот блок собирает список модераторов и рассылает им уведомления,
# а также шлёт личные поздравления пользователям после одобрения заявки.
# Все функции вспомогательные (не обработчики) и используются
# как внутри файла, так и из других разделов бота.
# ============================================================
# КЛЮЧЕВАЯ ОСОБЕННОСТЬ ОТПРАВКИ В MAX:
# сообщения в личные чаты уходят по chat_id, а не по user_id.
# user_id — это внутренний идентификатор пользователя в базе, тогда
# как chat_id — идентификатор конкретного чата в Telegram. Поэтому
# почти каждая функция здесь сначала делает db.get_user(...), извлекает
# chat_id и только потом вызывает bot.send_message(chat_id=...).
# ============================================================

def get_moderator_ids() -> list[int]:
    """
    Возвращает список user_id модераторов.

    Модераторы = МОДЕРАТОРЫ из config.MODERATOR_IDS + пользователи
    с ролью "moderator" в БД.

    Returns:
        Список числовых ID модераторов.
    """
    # ЛОГИКА ОБЪЕДИНЕНИЯ ИСТОЧНИКОВ (двухшаговая):
    # 1) классический источник — конфиг, где модераторы заданы вручную;
    # 2) динамический источник — таблица пользователей, где роль могла быть
    #    назначена через /makemod или панель модератора (mod_user_role_...).
    # Итоговый список используется и для рассылок, и для проверки прав.
    from config import MODERATOR_IDS

    # MODERATOR_IDS из config — это базовый «статический» состав модераторов,
    # который не зависит от состояния БД и задаётся в конфигурации бота.
    # Далее он дополняется динамическими модераторами из таблицы пользователей.
    ids = list(MODERATOR_IDS or [])
    # Проходим по ВСЕМ user_id в БД — один запрос get_all_user_ids
    # возвращает список всех зарегистрированных пользователей.
    for uid in db.get_all_user_ids():
        # Проверяем роль каждого пользователя; "not in ids" не даёт
        # задвоить модератора, который уже есть в config.MODERATOR_IDS.
        if db.get_user_field(uid, "role") == "moderator" and uid not in ids:
            ids.append(uid)
    # Возвращаем полный список модераторов (config + БД).
    return ids


async def notify_moderators(text: str) -> int:
    """
    Отправляет сообщение всем модераторам в личные чаты (асинхронно).

    Используется, например, при сбоях ИИ. Отправка идёт через
    АСИНХРОННЫЙ метод bot.send_message(...) — потому функция и async.

    Args:
        text: Текст уведомления.

    Returns:
        Количество успешно отправленных уведомлений.
    """
    sent = 0
    # Идём по всем ID модераторов и шлём каждому сообщение в личку.
    for mid in get_moderator_ids():
        # Извлекаем из БД запись о модераторе: интересен только chat_id,
        # по которому идёт реальная доставка сообщения в Telegram.
        user = db.get_user(mid)
        chat_id = (user or {}).get("chat_id")
        if not chat_id:
            # Не знаем, куда писать модератору (нет сохранённого чата) — пропускаем.
            print(f"MODERATION: No chat_id for moderator {mid}, cannot notify")
            continue
        try:
            # В maxo отправка в личный чат идёт по chat_id (не user_id!).
            await bot.send_message(chat_id=chat_id, text=text)
            # Успешную отправку учитываем в счётчике результата.
            sent += 1
        except Exception as e:  # noqa: BLE001
            # Сбой одной отправки не должен прерывать уведомление остальных.
            print(f"MODERATION: Could not notify moderator {mid}: {e}")
    # Возвращаем число реально отправленных уведомлений (для статистики).
    return sent


async def _notify_approved(user_id: int, text: str | None = None) -> None:
    """
    Отправляет пользователю сообщение о том, что заявка одобрена (async).

    Отправка идёт от имени бота в личный чат пользователя по
    сохранённому chat_id (не user_id! — MAX разделяет логин-ID и ID чата).

    Args:
        user_id: Числовой ID пользователя.
        text: Текст уведомления (если None — стандартный текст).
    """
    # Стандартный текст поздравления определён ЗДЕСЬ, а не в точке вызова.
    # Это позволяет переиспользовать уведомление из разных веток панели
    # и при желании передать кастомный текст через параметр text.
    if text is None:
        # Стандартное поздравление по умолчанию.
        text = (
            "🎉 Поздравляем! Твоя заявка подтверждена!\n\n"
            "Теперь ты можешь пользоваться MAX Школьным помощником.\n"
            "Напиши /start, чтобы открыть меню."
        )
        # Текст подставляется по умолчанию, но вызывающий код вправе
        # передать своё (кастомное) сообщение через параметр text.

    # Достаём запись пользователя, чтобы узнать его chat_id для лички.
    user = db.get_user(user_id)
    chat_id = (user or {}).get("chat_id")
    if not chat_id:
        # У пользователя не сохранён чат — написать ему невозможно,
        # фиксируем это в консольном логе и выходим без ошибки.
        print(f"MODERATION: No chat_id stored for user {user_id}, cannot notify")
        return
    try:
        # Асинхронная отправка поздравления в личный чат пользователя.
        await bot.send_message(chat_id=chat_id, text=text)
        print(f"MODERATION: Notification sent to user {user_id} (chat {chat_id})")
    except Exception as e:  # noqa: BLE001
        # Отправка может упасть (пользователь заблокировал бота и т.п.) —
        # ловим исключение, чтобы не сломать обработку заявки.
        print(f"MODERATION: Could not notify user {user_id}: {e}")


# ============================================================
# КЛАВИАТУРЫ МОДЕРАЦИИ
# ============================================================
# Здесь собраны ВСЕ построители клавиатур раздела модерации:
#   - get_moderation_keyboard — главное меню панели;
#   - get_pending_keyboard — обработка конкретной заявки;
#   - _cancel_keyboard — кнопка отмены для любых FSM-вводов;
#   - _user_card_keyboard — быстрые действия на карточке пользователя.
# Payload каждой кнопки — это контракт: строгое значение, по которому
# её обработает роутер (см. decorator CallbackPayload и его prefixes).
# ============================================================
# ЕДИНЫЙ ФОРМАТ PAYLOAD КНОПОК:
#   панель: <mod_префикс> или <mod_префикс>_<числовой id>
#   пример: "mod_pending", "mod_approve_123", "mod_user_role_student_456".
# Формат "последняя часть после '_' — это число" поддерживается утилитой
# _parse_id, а разбор "роль/действие" — строковым split в обработчиках.
# ============================================================

def get_moderation_keyboard() -> KeyboardBuilder:
    """
    Клавиатура главного раздела модерации.

    Все кнопки короткие (до 20 символов), поэтому по одной в ряд —
    текст не обрезается (см. лимиты MAX в handlers/menu.py).
    """
    # Клавиатура строится каждый раз заново — она статична, поэтому
    # результат одинаков при любом количестве вызовов. Единственная
    # динамика — количество заявок, но оно показывается в ТЕКСТЕ сообщения
    # (переменная pending_count), а не в кнопках.
    kb = KeyboardBuilder()
    # Каждая кнопка — действие панели модератора.
    # Порядок кнопок = порядок строк в меню: заявки, пользователи,
    # поиск, блокировка, лимиты, рассылка, ошибки, вступление, назад.
    kb.add_callback(text="📋 Заявки", payload="mod_pending")
    # mod_pending -> просмотр заявок на регистрацию (подтвердить/отклонить).
    kb.add_callback(text="👥 Все пользователи", payload="mod_all_users")
    # mod_all_users -> список всех пользователей с ролями/статусами.
    kb.add_callback(text="🔎 Пользователь по ID", payload="mod_user_lookup")
    # mod_user_lookup -> FSM wait_uid; по введённому ID открывается карточка.
    kb.add_callback(text="🚫 Блокировка", payload="mod_block_user")
    # mod_block_user -> FSM wait_uid; блокировка по введённому ID.
    kb.add_callback(text="🔧 Лимиты ИИ", payload="mod_set_limit")
    # mod_set_limit -> FSM wait_uid; смена дневного лимита ИИ ученику.
    kb.add_callback(text="📢 Рассылка", payload="mod_broadcast")
    # mod_broadcast -> FSM wait_broadcast; массовая отправка сообщения.
    kb.add_callback(text="⚠️ Ошибки ИИ", payload="mod_errors")
    # mod_errors -> лог последних ошибок ИИ (очистка через mod_errors_clear).
    kb.add_callback(text="🏫 На вступление", payload="mod_join_requests")
    # mod_join_requests -> заявки на вступление в школы со всех школ.
    kb.add_callback(text="← Назад в меню", payload="menu_main")
    # Возврат в главное меню — обрабатывает роутер handlers/menu.py.
    # adjust(1) — по одной кнопке в ряд, чтобы длинные тексты не резались.
    return kb.adjust(1)


def get_pending_keyboard(user_id: int) -> KeyboardBuilder:
    """
    Клавиатура обработки конкретной заявки (подтвердить/отклонить).

    ДВЕ кнопки в ряд — это короткие действия («✅ Подтвердить» и
    «❌ Отклонить»), и 2 кнопки в ряду разрешают до 10 символов текста.

    В payload кнопок «зашивается» user_id пользователя, чью заявку
    обрабатываем: mod_approve_<id> / mod_reject_<id>.

    Args:
        user_id: ID пользователя, чью заявку обрабатываем.
    """
    # Обратите внимание: визуальный текст кнопки («✅ Подтвердить») никак
    # не связан с payload. Payload — это внутренний код действия, а текст —
    # только подпись. Обработчик смотрит исключительно на payload.
    kb = KeyboardBuilder()
    # Кнопка подтверждения заявки на регистрацию.
    kb.add_callback(text="✅ Подтвердить", payload=f"mod_approve_{user_id}")
    # В payload зашит ID (user_id) заявки: mod_approve_<id>.
    # Обработчик вытащит ID из хвоста payload через _parse_id.
    kb.add_callback(text="❌ Отклонить", payload=f"mod_reject_{user_id}")
    # В payload зашит ID (user_id) заявки: mod_reject_<id>.
    # Отклонение не удаляет пользователя — только меняет роль на "rejected".
    # Для коротких кнопок достаточно двух в ряд: adjust(2) — их не вызываем
    # явно, две кнопки умещаются в ряд и по умолчанию.
    return kb


def _cancel_keyboard(back_payload: str = "menu_moderation") -> KeyboardBuilder:
    """Клавиатура «Отмена» для FSM-ввода модератора."""
    # Универсальная кнопка выхода из любого режима ввода. Аргумент
    # back_payload позволяет настроить, куда возвращаться: по умолчанию в
    # панель модерации, но при необходимости можно указать любой другой.
    kb = KeyboardBuilder()
    # Отмена в любом FSM-вводе модератора: по умолчанию возвращает
    # в панель модерации (menu_moderation), но можно указать другой payload.
    # Эта клавиатура крепится ко всем сообщениям FSM, чтобы пользователь
    # в любой момент мог выйти из режима ввода без ошибочных данных.
    kb.add_callback(text="← Отмена", payload=back_payload)
    return kb


# ============================================================
# КАРТОЧКА ПОЛЬЗОВАТЕЛЯ ПО ID
# ============================================================

def _user_card_keyboard(target_id: int) -> KeyboardBuilder:
    """
    Клавиатура быстрых действий для карточки пользователя.

    Одна кнопка в ряд — тексты ролей длинные.
    """
    # Важный нюанс: каждая кнопка этой клавиатуры уходит в обработчик
    # _handle_user_card_action, который разбирает payload по символу "_".
    # Поэтому в название роли и в ID цели НЕ должны попадать подчёркивания.
    kb = KeyboardBuilder()
    # Каждая кнопка действия несёт ID цели в payload: mod_user_<действие>_<id>.
    # Обрабатываются в _handle_user_card_action по префиксу payload.
    kb.add_callback(text="🚫 Блок / разблок", payload=f"mod_user_block_{target_id}")
    # mod_user_block_<id> -> toggle_blocked: блокирует или разблокирует.
    kb.add_callback(text="🔧 Сменить лимит ИИ", payload=f"mod_user_limit_{target_id}")
    # mod_user_limit_<id> -> открывает FSM ввод нового лимита ИИ.
    kb.add_callback(text="🎭 Роль: ученик", payload=f"mod_user_role_student_{target_id}")
    # mod_user_role_student_<id> -> set_role("student") + approve.
    kb.add_callback(text="🎭 Роль: учитель", payload=f"mod_user_role_teacher_{target_id}")
    # mod_user_role_teacher_<id> -> set_role("teacher") + approve.
    kb.add_callback(text="🎭 Роль: модератор", payload=f"mod_user_role_moderator_{target_id}")
    # mod_user_role_moderator_<id> -> set_role("moderator") + approve.
    kb.add_callback(text="🗑 Удалить", payload=f"mod_user_delete_{target_id}")
    # mod_user_delete_<id> -> двухшаговое подтверждение удаления из БД.
    kb.add_callback(text="← Назад", payload="menu_moderation")
    # "← Назад" всегда возвращает в корень панели модератора.
    # adjust(1) — тексты длинные, поэтому кнопки в столбик.
    return kb.adjust(1)


def _user_card_text(user: dict, target_id: int) -> str:
    """Текст карточки пользователя на основе записи из БД."""
    # Собираем все поля из записи БД (с подстановками по умолчанию).
    # Запись user — это словарь из database.py; каждое поле может
    # отсутствовать, поэтому везде стоят .get(...) с дефолтами,
    # а строковые поля дополнительно обрезаются через .strip().
    full_name = (user.get("full_name") or "").strip()
    max_name = (user.get("name") or "").strip()
    phone = user.get("phone") or "Не указан"
    school = user.get("school") or "—"
    class_name = user.get("class_name") or ""
    role = user.get("role") or "?"
    approved = bool(user.get("approved"))
    blocked = bool(user.get("blocked"))
    ai_limit = user.get("ai_daily_limit", 50)
    # ai_daily_limit по умолчанию 50 — столько же, сколько в настройках.

    # Сопоставляем внутренний код роли с человекочитаемым названием для карточки.
    # Внутренние коды ролей в БД: student/teacher/moderator/pending/rejected.
    role_display = {
        "student": "Ученик",
        "teacher": "Учитель",
        "moderator": "Модератор",
        "pending": "На рассмотрении",
        "rejected": "Отклонён",
    }.get(role, role)
    # .get(role, role): неизвестную роль показываем как есть, без ошибки.

    # Собираем текст карточки построчно: каждый блок добавляем к строке.
    card = f"👤 Карточка пользователя (ID: {target_id})\n\n"
    card += f"👤 Имя: {full_name or max_name or 'Не указано'}\n"
    # Если имя в MAX отличается от полного имени — показываем оба.
    if full_name and max_name and full_name != max_name:
        card += f"└ (MAX: {max_name})\n"
    # Телефон и школу показываем всегда (с подстановкой заглушек).
    card += (
        f"📱 Телефон: {phone}\n"
        f"🏫 Школа: {school}\n"
    )
    # Класс — необязательное поле, строку добавляем только если он указан.
    if class_name:
        card += f"📚 Класс: {class_name}\n"
    # Роль/статусы/лимит — финальный блок карточки.
    card += (
        f"🎭 Роль: {role_display}\n"
        f"✅ Подтверждён: {'да' if approved else 'нет'}\n"
        f"🚫 Заблокирован: {'да' if blocked else 'нет'}\n"
        f"🔧 Лимит ИИ: {ai_limit} запросов/день\n"
    )
    return card


async def _render_user_card(update: MessageCallback, target_id: int) -> None:
    """Показывает карточку пользователя с кнопками действий."""
    # Эта функция — общая точка отрисовки карточки. Её вызывают и панель
    # поиска (action == "lookup"), и каждый обработчик действий карточки
    # (после смены роли/блокировки), чтобы показать обновлённые данные.
    # Первым делом — запрос записи пользователя по ID из БД.
    user = db.get_user(target_id)
    if not user:
        # Пользователя нет в БД — показываем ошибку и возвращаемся в панель.
        # get_user вернул None: запись либо удалена, либо ID не существует.
        await update.callback_answer(text="❌")
        await update.edit_message(
            f"❌ Пользователь с ID {target_id} не найден.",
            # Вместе с ошибкой сразу отдаём главную клавиатуру модерации.
            keyboard=get_moderation_keyboard().build(),
        )
        return
    # Иначе строим текст карточки и клавиатуру быстрых действий.
    # Сначала короткая "вспышка" на кнопке (callback_answer), затем подменяем
    # сообщение целиком — edit_message заменяет и текст, и клавиатуру.
    await update.callback_answer(text="Карточка")
    await update.edit_message(
        _user_card_text(user, target_id),
        keyboard=_user_card_keyboard(target_id).build(),
    )


async def _handle_user_card_action(update, fsm_context, payload: str) -> None:
    """
    Обрабатывает быстрые действия на карточке пользователя
    (payload'ы mod_user_*_<id>).
    """
    target_id = _parse_id(payload)
    if target_id is None:
        # В payload не оказалось числа — это не наш колбэк, тихо выходим.
        return

    # Смена роли: mod_user_role_<role>_<id>
    # Payload разбирается на части по "_" — роль лежит на позиции [3]:
    # parts = ["mod", "user", "role", "<role>", "<id>"].
    if payload.startswith("mod_user_role_"):
        try:
            role = payload.split("_")[3]
        except IndexError:
            # Некорректный формат payload (нет сегмента с ролью) — пропускаем.
            return
        if role not in ("student", "teacher", "moderator"):
            # Допустимые роли жёстко ограничены — остальное игнорируем.
            # Это защита от подделки payload сторонним клиентом.
            return
        # Загружаем целевую запись пользователя из БД.
        user = db.get_user(target_id)
        if not user:
            await update.edit_message(f"❌ Пользователь с ID {target_id} не найден.")
            return
        # Записываем роль и приводим пользователя в активное состояние:
        # approved=True и снимаем блокировку.
        # Три отдельных вызова БД: запись роли, отметка одобрения, снятие блокировки.
        db.set_role(target_id, role)
        db.approve_user(target_id)
        db.set_blocked(target_id, False)
        name = user.get("full_name") or user.get("name", "?")
        # Логируем факт смены роли в консоль для разбора инцидентов.
        print(f"MODERATION: Role {role} set for {target_id} ({name}) by {get_user_id(update)}")
        # После изменения роли перерисовываем карточку (данные обновились).
        await _render_user_card(update, target_id)
        return

    # Блокировка/разблокировка
    # mod_user_block_<id> — переключатель: заблокирован -> разблокирован и наоборот.
    if payload.startswith("mod_user_block_"):
        user = db.get_user(target_id)
        if not user:
            await update.edit_message(f"❌ Пользователь с ID {target_id} не найден.")
            return
        # toggle_blocked инвертирует флаг blocked в БД и возвращает новое значение.
        # То есть одна и та же кнопка и блокирует, и разблокирует.
        db.toggle_blocked(target_id)
        name = user.get("full_name") or user.get("name", "?")
        # Читаем новое состояние, чтобы залогировать результат переключения.
        new_status = db.get_user_field(target_id, "blocked")
        print(
            f"MODERATION: User {target_id} ({name}) "
            f"{'blocked' if new_status else 'unblocked'} by {get_user_id(update)}"
        )
        # Перерисовываем карточку — в ней поменялось поле «Заблокирован».
        await _render_user_card(update, target_id)
        return

    # Смена лимита ИИ (открываем FSM-ввод нового значения)
    # mod_user_limit_<id> -> переходим в ModerationStates.wait_limit,
    # предварительно сохранив действие и ID цели в FSM-данные.
    if payload.startswith("mod_user_limit_"):
        user = db.get_user(target_id)
        if not user:
            await update.edit_message(f"❌ Пользователь с ID {target_id} не найден.")
            return
        # clear() сбрасывает FSM к статистике, чтобы не протащить лишние данные.
        await fsm_context.clear()
        # action="set_limit" нужно FSM-обработчику текста, чтобы понять, что делать.
        # target_uid запоминаем, чтобы ввод нового числа применить к этой цели.
        await fsm_context.update_data(action="set_limit", target_uid=target_id)
        await fsm_context.set_state(ModerationStates.wait_limit)
        current_limit = user.get("ai_daily_limit", 50)
        name = user.get("full_name") or user.get("name", "?")
        await update.callback_answer(text="Лимит")
        await update.edit_message(
            f"🔧 Текущий лимит у {name}: {current_limit} запросов/день.\n\n"
            "Введи новое значение (например, 100):",
            # К сообщению крепим кнопку «← Отмена» для выхода из режима ввода.
            keyboard=_cancel_keyboard().build(),
        )
        return

    # Удаление пользователя (двухшаговое подтверждение)
    # Первый шаг — подтверждение: mod_user_delete_yes_<id> (и удаляет).
    # Порядок проверок важен: "mod_user_delete_yes_" проверяется РАНЬШЕ
    # общего "mod_user_delete_", иначе попадали бы в ветку подтверждения.
    if payload.startswith("mod_user_delete_yes_"):
        user = db.get_user(target_id)
        name = user.get("full_name") or user.get("name", "?") if user else f"ID {target_id}"
        # Фактическое удаление записи из БД (необратимая операция).
        db.delete_user(target_id)
        await update.callback_answer(text="Удалено")
        await update.edit_message(
            f"🗑 Пользователь {name} (ID: {target_id}) удалён из базы.",
            # После удаления показываем главное меню модератора.
            keyboard=get_moderation_keyboard().build(),
        )
        return

    # Второй шаг — запрос подтверждения: mod_user_delete_<id>.
    # Само удаление НЕ выполняется, пока модератор не нажмёт «Да, удалить».
    if payload.startswith("mod_user_delete_"):
        user = db.get_user(target_id)
        if not user:
            await update.edit_message(f"❌ Пользователь с ID {target_id} не найден.")
            return
        name = user.get("full_name") or user.get("name", "?")
        kb = KeyboardBuilder()
        # Подтверждение ведёт на mod_user_delete_yes_<id>, отмена — в панель.
        kb.add_callback(
            text="✅ Да, удалить",
            payload=f"mod_user_delete_yes_{target_id}",
        )
        kb.add_callback(text="← Отмена", payload="menu_moderation")
        await update.callback_answer(text="Удаление")
        await update.edit_message(
            f"🗑 Точно удалить **{name}** (ID: {target_id})?\n"
            "Действие необратимо.",
            # Кнопки в столбик (adjust(1)), чтобы случайно не нажать «Да» рядом с «Отмена».
            keyboard=kb.adjust(1).build(),
        )
        return


# ============================================================
# ПРАВА МОДЕРАТОРА
# ============================================================
# Единая проверка прав используется во ВСЕХ точках входа раздела:
# в обработчике панели, в FSM-вводе текста и в командах /makemod, /myid.
# Проверка максимально простая, чтобы её нельзя было обойти.
# ============================================================

def _is_moderator(user_id: int) -> bool:
    """True, если пользователь имеет права модератора (config или роль в БД)."""
    from config import MODERATOR_IDS
    # Проверка: либо ID числится в статическом списке config.MODERATOR_IDS,
    # либо роль "moderator" проставлена в БД через db.is_moderator.
    # OR достаточно — любой из источников даёт права модератора.
    return user_id in (MODERATOR_IDS or []) or db.is_moderator(user_id)


# ============================================================
# ОБРАБОТЧИК РАЗДЕЛА «МОДЕРАЦИЯ»
# ============================================================
# Главный (и единственный) обработчик колбэков панели модерации.
# Держит на себе все кнопки меню и карточек; работает только после
# проверки _is_moderator. Начинается с регистрации калбэков на роутере,
# затем — исходная логика ветвления по payload.
# ============================================================

# Регистрируем все колбэки панели модерации: точные payload'ы перечислены
# явно, а payload'ы с числовым суффиксом — через prefixes.
# ПРИНЦИП МАРШРУТИЗАЦИИ:
#   - точные payload'ы (menu_moderation, mod_pending, ...) идут в качестве
#     аргументов CallbackPayload — они сопоставляются по равенству;
#   - payload'ы с переменной частью (mod_approve_<id> и т.п.) передаются
#     списком prefixes — CallbackPayload матчит их по началу строки;
#   - все ветки собирает ОДНА функция, поэтому порядок elif внутри неё
#     обязан быть правильным (см. комментарии перед каждой веткой).
# Этот единый обработчик экономит код и упрощает дальнейшие правки.
# МАРШРУТИЗАЦИЯ ПО КНОПКАМ:
#   menu_moderation   — точный payload кнопки «← Назад в меню»;
#   mod_pending        — точный payload кнопки «📋 Заявки»;
#   mod_all_users      — точный payload кнопки «👥 Все пользователи»;
#   mod_user_lookup    — точный payload кнопки «🔎 Пользователь по ID»;
#   mod_block_user     — точный payload кнопки «🚫 Блокировка»;
#   mod_set_limit      — точный payload кнопки «🔧 Лимиты ИИ»;
#   mod_broadcast      — точный payload кнопки «📢 Рассылка»;
#   mod_errors         — точный payload кнопки «⚠️ Ошибки ИИ»;
#   mod_join_requests  — точный payload кнопки «🏫 На вступление»;
#   mod_approve_*      — префикс кнопки «✅ Подтвердить» из карточки заявки;
#   mod_reject_*       — префикс кнопки «❌ Отклонить» из карточки заявки;
#   mod_skip_*         — префикс кнопки «⏭ Пропустить» из карточки заявки;
#   mod_user_block_*   — префикс кнопки «🚫 Блок / разблок» из карточки юзера;
#   mod_user_role_*    — префикс кнопки смены роли из карточки юзера;
#   mod_user_limit_*   — префикс кнопки смены лимита из карточки юзера;
#   mod_user_delete_*  — префикс кнопки удаления из карточки юзера.
# Точная карта: какие кнопки/ветки к чему ведут — в комментариях выше
# (раздел «КЛАВИАТУРЫ МОДЕРАЦИИ» и docstrings к построителям клавиатур).
@router.message_callback(
    CallbackPayload(
        "menu_moderation", "mod_pending", "mod_all_users",
        "mod_block_user", "mod_set_limit", "mod_broadcast", "mod_errors",
        "mod_join_requests", "mod_user_lookup",
        prefixes=(
            "mod_approve_", "mod_reject_", "mod_skip_",
            "mod_user_block_", "mod_user_role_", "mod_user_limit_",
            "mod_user_delete_", "mod_user_delete_yes_",
        ),
    )
)
async def handle_moderation_menu(update: MessageCallback, fsm_context) -> None:
    """Обрабатывает кнопки раздела модерации."""
    # Один общий обработчик держит ВСЕ ветки панели модерации.
    # Ветвление идёт по точному значению payload (elif по строкам) либо
    # по префиксу payload (payload.startswith). FSM-контекст передаётся,
    # потому что многие ветки переводят пользователя в состояние ввода.
    payload = update.payload
    user_id = get_user_id(update)

    # Общая защита раздела: без прав модератора все кнопки недоступны.
    # Проверка стоит в самом начале, чтобы любой чужой call-back
    # получал отказ, даже не доходя до веток.
    if not _is_moderator(user_id):
        await update.callback_answer(text="❌")
        await update.edit_message("❌ У тебя нет прав модератора.")
        return

    # ============================================================
    # ГЛАВНОЕ МЕНЮ МОДЕРАЦИИ
    # ============================================================
    # «Корень» панели: показывает количество ожидающих заявок и клавиатуру.
    if payload == "menu_moderation":
        await fsm_context.clear()
        # Считаем, сколько заявок на регистрацию ждут обработки.
        # get_pending_users возвращает список ID пользователей с approved=False.
        pending_count = len(db.get_pending_users())
        mod_text = (
            "🛡 Панель модератора\n\n"
            f"📋 Заявок на регистрацию: {pending_count}\n\n"
            "Выбери действие:"
        )
        await update.callback_answer(text="Модерация")
        await update.edit_message(mod_text, keyboard=get_moderation_keyboard().build())

    # ============================================================
    # ПРОСМОТР ЗАЯВОК
    # ============================================================
    # Показываем первую ожидающую заявку через _show_pending_user.
    elif payload == "mod_pending":
        await fsm_context.clear()
        pending = db.get_pending_users()
        if not pending:
            # Свободных заявок нет — сообщаем об этом.
            await update.callback_answer(text="Заявки")
            await update.edit_message(
                "📋 Нет заявок на регистрацию.\n\n"
                "Все пользователи уже подтверждены!",
                keyboard=get_moderation_keyboard().build(),
            )
            return
        # Передаём список и индекс 0 — с первой заявки.
        # Дальше листание идёт кнопками «⏭ Пропустить» и мод_approve/reject.
        await _show_pending_user(update, pending, 0)

    # ============================================================
    # ПОДТВЕРЖДЕНИЕ ПОЛЬЗОВАТЕЛЯ
    # ============================================================
    # Payload mod_approve_<id>: подтверждаем заявку на регистрацию.
    # ID извлекается из хвоста payload; если это не число — молча выходим.
    elif payload.startswith("mod_approve_"):
        target_id = _parse_id(payload)
        if target_id is None:
            return

        # ПОСЛЕДОВАТЕЛЬНОСТЬ ДЕЙСТВИЙ ПРИ ПОДТВЕРЖДЕНИИ:
        #   1) снять блокировку (мало ли, пользователь был заблокирован);
        #   2) проставить approved=True (флаг «заявка одобрена»);
        #   3) прочитать имя пользователя для текста ответа;
        #   4) ответить модератору и уведомить пользователя в личку.
        # Снимаем блокировку и помечаем пользователя как одобренного.
        # Два независимых обновления БД: флаг blocked=False и approved=True.
        db.set_blocked(target_id, False)
        db.approve_user(target_id)
        # Подтягиваем свежую запись, чтобы взять имя для текста ответа.
        user = db.get_user(target_id)
        name = user.get("full_name") or user.get("name", "Неизвестно") if user else "Неизвестно"

        await update.callback_answer(text="Подтверждено")
        await update.edit_message(f"✅ Пользователь {name} подтверждён!")

        # Отправляем пользователю письмо с поздравлением в личку.
        await _notify_approved(target_id)
        # Логируем в консоль факт подтверждения и кто его выполнил.
        print(f"MODERATION: User {target_id} ({name}) approved by {user_id}")

        # Показываем следующую заявку, если осталась.
        # get_pending_users перезапрашивается, чтобы учесть только что
        # обработанную заявку и не показать её повторно.
        pending = db.get_pending_users()
        if pending:
            await _show_pending_user(update, pending, 0)
        else:
            # Заявок больше нет — итоговое сообщение со «счётчиком».
            await update.answer_text(
                "📋 Все заявки обработаны!",
                keyboard=get_moderation_keyboard().build(),
            )

    # ============================================================
    # ОТКЛОНЕНИЕ ПОЛЬЗОВАТЕЛЯ
    # ============================================================
    # Payload mod_reject_<id>: помечаем роль как "rejected" и снимаем approved.
    # Пользователь не удаляется из БД — он просто остаётся «отклонённым».
    elif payload.startswith("mod_reject_"):
        target_id = _parse_id(payload)
        if target_id is None:
            return

        # ОТЛИЧИЕ ОТ ПОДТВЕРЖДЕНИЯ: здесь не пишем пользователю в личку,
        # а только меняем его статус в БД и отвечаем модератору в чате.
        # Пишем новую роль и снимаем флаг одобрения в БД.
        # Оба изменения делаются через set_user_field — по одной колонке.
        db.set_user_field(target_id, "role", "rejected")
        db.set_user_field(target_id, "approved", False)

        user = db.get_user(target_id)
        name = user.get("full_name") or user.get("name", "Неизвестно") if user else "Неизвестно"

        await update.callback_answer(text="Отклонено")
        await update.edit_message(f"❌ Заявка от {name} отклонена.")
        # Логируем отказ в консоль вместе с ID модератора, принявшего решение.
        print(f"MODERATION: User {target_id} ({name}) rejected by {user_id}")

        # Показываем следующую заявку, если осталась.
        # Логика полностью повторяет ветку подтверждения (см. выше).
        pending = db.get_pending_users()
        if pending:
            await _show_pending_user(update, pending, 0)
        else:
            await update.answer_text(
                "📋 Все заявки обработаны!",
                keyboard=get_moderation_keyboard().build(),
            )

    # ============================================================
    # ПРОСМОТР ВСЕХ ПОЛЬЗОВАТЕЛЕЙ
    # ============================================================
    # Простой текстовый список первых 15 пользователей с маркерами статуса.
    elif payload == "mod_all_users":
        await fsm_context.clear()
        all_ids = db.get_all_user_ids()
        if not all_ids:
            await update.callback_answer(text="Пользователи")
            await update.edit_message(
                "👥 Пока нет пользователей.",
                keyboard=get_moderation_keyboard().build(),
            )
            return

        users_text = f"👥 Все пользователи ({len(all_ids)}):\n\n"
        # Проходим максимум по первым 15 пользователям базы.
        # Ограничение защищает от слишком длинного сообщения в Telegram.
        # Полный обзор остальных доступен через поиск по ID (mod_user_lookup).
        for uid in all_ids[:15]:
            user = db.get_user(uid)
            if user:
                name = user.get("full_name") or user.get("name", "?")
                role = user.get("role", "?")
                # Маркеры статуса: ✅ одобрен / ⏳ ожидает / ❌ отклонён.
                # Логика: если approved=True — ✅; иначе роль "rejected" даёт ❌;
                # любое другое состояние (например, pending) означает ⏳.
                approved = "✅" if user.get("approved") else ("❌" if role == "rejected" else "⏳")
                blocked = "🚫" if user.get("blocked") else ""
                # Иконка роли для наглядности списка.
                # Словарь ролей->эмодзи; неизвестная роль получает "❓".
                role_emoji = {
                    "student": "📚",
                    "teacher": "👨‍🏫",
                    "moderator": "🛡",
                    "pending": "⏳",
                    "rejected": "❌",
                }.get(role, "❓")
                # Склейка маркеров: сначала блокировка, затем статус одобрения.
                status_marks = blocked + approved
                users_text += f"{role_emoji} {name} (ID: {uid}) {status_marks}\n"

        # Если пользователей больше 15 — показываем счётчик оставшихся.
        if len(all_ids) > 15:
            users_text += f"\n... и ещё {len(all_ids) - 15} пользователей"

        # Легенда маркеров для читаемости списка.
        users_text += "\n\nМаркеры: 🚫 — заблокирован"
        await update.callback_answer(text="Пользователи")
        await update.edit_message(users_text, keyboard=get_moderation_keyboard().build())

    # ============================================================
    # БЛОКИРОВКА / РАЗБЛОКИРОВКА
    # ============================================================
    # Сохраняем действие "block" во FSM и ждём user_id (wait_uid).
    # Блокировка не требует самого ID сейчас: сначала ждём ввод текста,
    # а сам переключатель сработает уже в обработчике текста.
    elif payload == "mod_block_user":
        await fsm_context.clear()
        await fsm_context.update_data(action="block")
        await fsm_context.set_state(ModerationStates.wait_uid)

        await update.callback_answer(text="Блокировка")
        # Подсказка: переключатель (уже заблокированного — разблокирует).
        await update.edit_message(
            "🚫 Введи user_id пользователя.\n\n"
            "Если пользователь сейчас заблокирован — он будет разблокирован, "
            "если нет — заблокирован.",
            keyboard=_cancel_keyboard().build(),
        )

    # ============================================================
    # СМЕНА ЛИМИТА ИИ
    # ============================================================
    # Сохраняем действие "set_limit" во FSM и ждём user_id (wait_uid).
    # Дальше по тексту понадобится только ID ученика, сам лимит спросим
    # отдельным шагом (wait_limit), чтобы не перехватить случайный ввод.
    elif payload == "mod_set_limit":
        await fsm_context.clear()
        await fsm_context.update_data(action="set_limit")
        await fsm_context.set_state(ModerationStates.wait_uid)

        await update.callback_answer(text="Лимиты")
        await update.edit_message(
            "🔧 Введи user_id ученика, которому нужно изменить лимит ИИ.",
            keyboard=_cancel_keyboard().build(),
        )

    # ============================================================
    # ПОИСК ПОЛЬЗОВАТЕЛЯ ПО ID
    # ============================================================
    # Сохраняем действие "lookup" во FSM и ждём user_id (wait_uid).
    # Результат поиска — карточка пользователя с быстрыми действиями.
    elif payload == "mod_user_lookup":
        await fsm_context.clear()
        await fsm_context.update_data(action="lookup")
        await fsm_context.set_state(ModerationStates.wait_uid)

        await update.callback_answer(text="Поиск")
        await update.edit_message(
            "🔎 Введи user_id пользователя, данные которого нужно посмотреть.\n\n"
            "Покажется карточка с полной информацией и быстрыми действиями.",
            keyboard=_cancel_keyboard().build(),
        )

    # ============================================================
    # ДЕЙСТВИЯ НАД ПОЛЬЗОВАТЕЛЕМ ИЗ КАРТОЧКИ
    # ============================================================
    # Все payload'ы mod_user_*_<id> (блок/роль/лимит/удаление) делегируются
    # общей функции _handle_user_card_action. Здесь не дублируем логику —
    # просто передаём payload целиком в отдельную функцию.
    elif payload.startswith("mod_user_"):
        await _handle_user_card_action(update, fsm_context, payload)

    # ============================================================
    # РАССЫЛКА ВСЕМ ПОЛЬЗОВАТЕЛЯМ
    # ============================================================
    # Переход в FSM wait_broadcast: следующий текст станет текстом рассылки.
    # Никакого предзаполненного действия не нужно — всё решит сам текст.
    elif payload == "mod_broadcast":
        await fsm_context.clear()
        await fsm_context.set_state(ModerationStates.wait_broadcast)

        await update.callback_answer(text="Рассылка")
        await update.edit_message(
            "📢 Напиши текст сообщения для рассылки всем подтверждённым "
            "пользователям.\n\n"
            "Сообщение будет отправлено всем, кроме заблокированных.",
            keyboard=_cancel_keyboard().build(),
        )

    # ============================================================
    # ОШИБКИ ИИ
    # ============================================================
    # Показываем последние 10 ошибок из лога ИИ (db.get_ai_errors).
    elif payload == "mod_errors":
        await fsm_context.clear()
        # Запрос лога ошибок с лимитом 10 — берём только последние записи.
        errors = db.get_ai_errors(limit=10)
        if not errors:
            await update.callback_answer(text="Ошибки")
            await update.edit_message(
                "⚠️ Лог ошибок ИИ пуст — всё работает отлично!",
                keyboard=get_moderation_keyboard().build(),
            )
            return

        text = "⚠️ Последние ошибки ИИ:\n\n"
        # Идём с конца лога (reversed), чтобы показывать самые свежие ошибки.
        for i, err in enumerate(reversed(errors), 1):
            # Каждая запись лога: время, имя пользователя, режим и текст ошибки.
            # Поля в записи необязательны, поэтому везде стоят .get с дефолтами.
            ts = err.get("time", 0)
            # Преобразуем unix-время в читаемую строку "дд.мм чч:мм".
            # localtime вызывается только при наличии ts, иначе "—".
            time_str = time.strftime("%d.%m %H:%M", time.localtime(int(ts))) if ts else "—"
            name = err.get("user_name") or "?"
            mode = err.get("mode") or "?"
            # Обрезаем длинный текст ошибки до 100 символов, чтобы не раздувать сообщение.
            # Срез [:100] гарантирует, что одно сообщение не превысит лимит Telegram.
            error = (err.get("error") or "—")[:100]
            # Нумерованная строка одной ошибки: время, пользователь, режим, текст.
            text += f"{i}. 🕐 {time_str}\n"
            text += f"   👤 {name} ({mode})\n"
            text += f"   ❌ {error}\n\n"

        kb = KeyboardBuilder()
        # Очистка лога обрабатывается отдельным обработчиком mod_errors_clear.
        # Сначала — кнопка очистки, затем возврат в панель модерации.
        kb.add_callback(text="🗑 Очистить лог", payload="mod_errors_clear")
        kb.add_callback(text="← Назад", payload="menu_moderation")

        await update.callback_answer(text="Ошибки")
        await update.edit_message(text, keyboard=kb.adjust(1).build())

    # ============================================================
    # ПРОСМОТР ЗАЯВОК НА ВСТУПЛЕНИЕ В ШКОЛУ
    # ============================================================
    # Отличие от settings_join_requests: здесь показываются ВСЕ ожидающие
    # заявки по всем школам сразу (глобальная модерация, а не только своя школа).
    elif payload == "mod_join_requests":
        await fsm_context.clear()
        # get_pending_join_requests возвращает все заявки на вступление.
        # В отличие от модерации регистраций, эти заявки приходят и от
        # ученика, и от учителя, и сохраняются отдельной сущностью в БД.
        requests = db.get_pending_join_requests()
        if not requests:
            await update.callback_answer(text="Заявки")
            await update.edit_message(
                "🏫 Нет заявок на вступление в школу.",
                keyboard=get_moderation_keyboard().build(),
            )
            return
        # Показываем первую заявку. Кнопки «Принять/Отклонить» используют
        # payload'ы settings_join_* — их обрабатывает роутер настроек
        # (handlers/settings.py), который подключён раньше.
        # Импорт делается локально и лениво, чтобы не создавать циклическую
        # зависимость между модулями модерации и настроек.
        from handlers.settings import _show_join_request
        await _show_join_request(update, requests, 0)

    # ============================================================
    # ПРОПУСТИТЬ ЗАЯВКУ
    # ============================================================
    # Payload mod_skip_<N> — пропускаем заявку под номером N
    # в списке ожидающих и показываем следующую.
    elif payload.startswith("mod_skip_"):
        next_index = _parse_id(payload)
        if next_index is None:
            return
        # Перезапрашиваем список заявок (он мог измениться с прошлого показа).
        pending = db.get_pending_users()
        await _show_pending_user(update, pending, next_index)


# ============================================================
# ОЧИСТКА ЛОГА ОШИБОК ИИ
# ============================================================
# Отдельный обработчик вынесен вне handle_moderation_menu, потому что у
# кнопки mod_errors_clear нет variable-парта в payload — это одинарный
# точный payload. Держим его изолированно и без FSM.
# ============================================================

@router.message_callback(CallbackPayload("mod_errors_clear"))
async def handle_mod_errors_clear(update: MessageCallback) -> None:
    """Очищает лог ошибок ИИ (из чата модератора)."""
    user_id = get_user_id(update)
    # Для очистки тоже нужны права модератора — иначе молча игнорируем.
    if not _is_moderator(user_id):
        return

    # Считаем текущее число записей (с большим лимитом), чтобы показать,
    # сколько именно было удалено.
    # Лимит 10000 — верхняя граница: больше записей в логе вряд ли накопится.
    count = len(db.get_ai_errors(limit=10000))
    # Собственно очистка таблицы лога ошибок в БД.
    db.clear_ai_errors()

    kb = KeyboardBuilder()
    # Единственная кнопка — возврат в панель модерации.
    kb.add_callback(text="← Назад", payload="menu_moderation")

    await update.callback_answer(text="Очищено")
    await update.edit_message(
        f"🗑 Лог ошибок ИИ очищен (удалено записей: {count}).",
        keyboard=kb.build(),
    )


# ============================================================
# ТЕКСТОВЫЙ ВВОД ИНСТРУМЕНТОВ МОДЕРАТОРА
# ============================================================
# Этот обработчик ловит ЛЮБОЕ текстовое сообщение, пока пользователь
# находится в одном из состояний ModerationStates (StateFilter по классу).
# Три состояния — три ветки:
#   1) wait_uid       — ждём user_id (для блокировки/лимита/поиска);
#   2) wait_limit     — ждём само число нового лимита ИИ;
#   3) wait_broadcast — ждём текст будущей рассылки.
# Выход из любого состояния возможен кнопкой «← Отмена» или очисткой FSM.
# ============================================================

@router.message_created(StateFilter(ModerationStates))
async def handle_moderation_text(update: MessageCreated, fsm_context) -> None:
    """
    Обрабатывает текстовый ввод во время работы инструментов модератора:
    user_id для блокировки/лимита, само значение лимита и текст рассылки.
    """
    # Сначала достаём user_id отправителя — по нему проверяем права.
    user_id = get_user_id(update)
    if not _is_moderator(user_id):
        # Модератор потерял права — выходим из FSM, чтобы не зависнуть во вводе.
        # Это же защита: без прав чужие тексты не попадут в ветки обработки.
        await fsm_context.clear()
        return

    text = (update.text or "").strip()
    if not text:
        # Пустое сообщение (например, картинка) — не обрабатываем.
        # Ветки ждут только осмысленный текст от модератора.
        return

    # Текущее состояние FSM и сохранённые данные нужны для ветвления логики.
    # state определяет, ЧТО мы ждём (ID, лимит или текст), а data (поле
    # "action") — КАК применить введённое значение (блок/лимит/поиск).
    state = await fsm_context.get_state()
    data = await fsm_context.get_data()

    # ============================================================
    # ЖДЁМ USER_ID (блокировка или смена лимита, поиск)
    # ============================================================
    # Состояние wait_uid; конкретное действие лежит во FSM-поле "action"
    # ("lookup", "block" или "set_limit").
    if state == ModerationStates.wait_uid:
        # Введённый текст обязан быть числом — это user_id.
        # _parse_int вернёт None для любого нечислового ввода.
        target_id = _parse_int(text)
        if target_id is None:
            await update.answer_text("❌ Нужно ввести числовой ID. Попробуй ещё раз:")
            return
        # Проверяем существование пользователя в БД до любых операций.
        # Пользователь с несуществующим ID просто не должен появляться
        # в ветках блокировки/лимита/поиска.
        if not db.user_exists(target_id):
            await update.answer_text(f"❌ Пользователь с ID {target_id} не найден.")
            return

        # Читаем сохранённое действие, чтобы понять ветку обработки.
        # Это то самое значение, которое кнопка панели записала в FSM
        # на предыдущем шаге (см. ветки mod_block_user / mod_set_limit / ...).
        action = data.get("action")

        # --- Поиск: показываем карточку пользователя ---
        if action == "lookup":
            user = db.get_user(target_id)
            if not user:
                await update.answer_text(
                    f"❌ Пользователь с ID {target_id} не найден. Попробуй ещё раз."
                )
                return
            # Завершаем FSM (данные больше не нужны) и рисуем карточку.
            await fsm_context.clear()
            kb = _user_card_keyboard(target_id)
            card = _user_card_text(user, target_id)
            # Дописываем приглашение выбрать действие под карточкой.
            card += "\nВыбери действие ниже:"
            await update.answer_text(card, keyboard=kb.build())
            return

        # --- Блокировка/разблокировка: переключаем флаг ---
        if action == "block":
            # toggle_blocked меняет флаг на противоположный и возвращает
            # новое значение — по нему и строим текст ответа.
            new_status = db.toggle_blocked(target_id)
            user = db.get_user(target_id)
            name = user.get("full_name") or user.get("name", "?") if user else f"ID {target_id}"
            status_text = "🚫 заблокирован" if new_status else "✅ разблокирован"
            await fsm_context.clear()
            await update.answer_text(
                f"Готово! Пользователь {name} (ID: {target_id}) {status_text}."
            )
        else:  # set_limit
            # --- Смена лимита: сохраняем ID цели и переходим в wait_limit ---
            # Здесь не вводим сам лимит сразу: сначала фиксируем ID в FSM-данных
            # (target_uid), а числом займётся следующий шаг wait_limit.
            await fsm_context.update_data(target_uid=target_id)
            await fsm_context.set_state(ModerationStates.wait_limit)
            user = db.get_user(target_id)
            name = user.get("full_name") or user.get("name", "?") if user else f"ID {target_id}"
            current_limit = user.get("ai_daily_limit", 50) if user else 50
            await update.answer_text(
                f"🔧 Текущий лимит у {name}: {current_limit} запросов/день.\n\n"
                "Введи новое значение (например, 100):"
            )
        return

    # ============================================================
    # ЖДЁМ НОВЫЙ ЛИМИТ
    # ============================================================
    # Состояние wait_limit: ждём число (1—10000) для дневного лимита ИИ.
    if state == ModerationStates.wait_limit:
        # ID цели берём из FSM-данных, сохранённых на прошлом шаге.
        target_uid = data.get("target_uid")
        new_limit = _parse_int(text)
        if new_limit is None or new_limit < 1 or new_limit > 10000:
            # Диапазон шире, чем в настройках (0 здесь запрещён):
            # лимит всегда положительное число.
            await update.answer_text("❌ Введи число от 1 до 10000.")
            return

        # Записываем новый дневной лимит в БД целевого ученика.
        db.set_user_field(target_uid, "ai_daily_limit", new_limit)
        # Подтягиваем имя пользователя, чтобы дать наглядный ответ.
        user = db.get_user(target_uid)
        name = user.get("full_name") or user.get("name", "?") if user else f"ID {target_uid}"
        await fsm_context.clear()
        await update.answer_text(
            f"✅ Лимит для {name} (ID: {target_uid}) установлен: {new_limit} запросов/день."
        )
        return

    # ============================================================
    # ЖДЁМ ТЕКСТ РАССЫЛКИ
    # ============================================================
    # Состояние wait_broadcast: текст сообщения рассылаем всем подтверждённым.
    if state == ModerationStates.wait_broadcast:
        # chat_id всех подтверждённых (не заблокированных) пользователей.
        # get_approved_chat_ids сам отсекает заблокированных.
        chat_ids = db.get_approved_chat_ids()
        if not chat_ids:
            await fsm_context.clear()
            await update.answer_text("📢 Некому рассылать: нет подтверждённых пользователей.")
            return

        # Сначала показываем «заглушку», чтобы пользователь видел, что идёт работа.
        # Дальше цикл отправки может быть долгим при большой базе пользователей.
        await update.answer_text("⏳ Отправляю рассылку...")

        sent = 0
        failed = 0
        # Последовательно шлём сообщение в каждый chat_id подтверждённого.
        # Порядок неважен; важно перехватывать исключения по каждому адресату.
        for chat_id in chat_ids:
            try:
                # Реальная отправка в личный чат пользователя.
                # Именно chat_id, а не user_id — так устроен вызов bot.send_message.
                await bot.send_message(chat_id=chat_id, text=text)
                sent += 1
            except Exception as e:  # noqa: BLE001
                # Сбой на одном получателе не прерывает рассылку остальным.
                # Падение случается, например, если пользователь заблокировал бота.
                failed += 1
                print(f"MODERATION: Broadcast failed to {chat_id}: {e}")

        # Выходим из FSM и подводим итог: успешно / не удалось.
        # После clear() следующий текст пользователя обработается как обычный.
        await fsm_context.clear()
        await update.answer_text(
            f"📢 Рассылка завершена.\n"
            f"✅ Отправлено: {sent}\n"
            f"❌ Не удалось: {failed}"
        )
        return


# ============================================================
# ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ
# ============================================================
# В этот блок вынесена единственная функция показа заявки (_show_pending_user).
# Она вызывается из трёх мест:
#   - mod_pending         (начало просмотра заявок, index=0);
#   - mod_approve_*/reject (после обработки текущей — следующая);
#   - mod_skip_<N>        (кнопка «Пропустить» с индексом следующей).
# Функция рекурсивно листает список до тех пор, пока не встретит
# живую заявку или не дойдёт до конца списка.
# ============================================================

async def _show_pending_user(update: MessageCallback, pending_list: list[int], index: int) -> None:
    """
    Показывает одну заявку из списка ожидающих.

    Args:
        update: Колбэк-апдейт (по нему отвечаем).
        pending_list: Список ID ожидающих пользователей.
        index: Индекс текущей заявки.
    """
    if index >= len(pending_list):
        # Все заявки закончились — итоговое сообщение и назад в панель.
        # Такое возможно, когда мы «дошли до конца» кнопкой «Пропустить».
        await update.callback_answer(text="Готово")
        await update.edit_message(
            "📋 Все заявки обработаны!",
            keyboard=get_moderation_keyboard().build(),
        )
        return

    # Берём ID текущей заявки по индексу в списке ожидающих.
    target_user_id = pending_list[index]
    # Загружаем полную запись пользователя для построения карточки.
    user = db.get_user(target_user_id)

    if not user:
        # Запись исчезла из БД между показами — перескакиваем к следующей.
        # Рекурсивно сдвигаемся на одну позицию вправо.
        await _show_pending_user(update, pending_list, index + 1)
        return

    # Собираем все поля для карточки заявки.
    # Разница с карточкой пользователя: тут показываем данные для решения
    # «принять или отклонить», включая школьные поля.
    full_name = user.get("full_name") or ""
    max_name = user.get("name") or ""
    phone = user.get("phone", "Не указан")
    school = user.get("school", "")
    class_name = user.get("class_name", "")
    role = user.get("role", "pending")
    blocked = user.get("blocked", False)

    # Определяем, кем себя указал пользователь: ученик или учитель.
    # Значения "student" и "pending" трактуем как «Ученик».
    role_text = "Ученик" if role in ("student", "pending") else "Учитель"
    if role == "teacher":
        role_text = "Учитель"

    name_display = full_name or max_name or "Не указано"

    # Заголовок заявки с порядковым номером и общим количеством.
    pending_text = (
        f"📋 Заявка {index + 1}/{len(pending_list)}\n\n"
        f"👤 Имя: {name_display}\n"
    )
    # Если имя в MAX отличается от полного — показываем оба источника.
    if full_name and max_name and full_name != max_name:
        pending_text += f"└ (MAX: {max_name})\n"
    elif not full_name and max_name:
        # У новых пользователей MAX ещё может не быть полного имени.
        pending_text += "└ (пока ник из MAX; имя появится после ввода)\n"
    pending_text += (
        f"📱 Телефон: {phone or '—'}\n"
        f"🏫 Школа: {school or '—'}\n"
    )
    # Класс показываем только если он указан в заявке.
    if class_name:
        pending_text += f"📚 Класс: {class_name}\n"
    pending_text += f"🎭 Роль: {role_text}\n"
    # Пометка о блокировке, если пользователь уже заблокирован.
    if blocked:
        pending_text += "🚫 Заблокирован\n"
    pending_text += f"🆔 ID: {target_user_id}"

    # Клавиатура обработки заявки: «Подтвердить» / «Отклонить».
    kb = get_pending_keyboard(target_user_id)
    # «Пропустить» добавляем, только если позади есть ещё заявки.
    if index + 1 < len(pending_list):
        # Следующий индекс заявки зашиваем в payload mod_skip_<N>.
        kb.add_callback(text="⏭ Пропустить", payload=f"mod_skip_{index + 1}")

    await update.callback_answer(text="Заявка")
    await update.edit_message(pending_text, keyboard=kb.build())


# ============================================================
# КОМАНДА /makemod — НАЗНАЧЕНИЕ МОДЕРАТОРА
# ============================================================
# Команда CLI-формата: пользователь пишет "/makemod <user_id>".
# Работает ТОЛЬКО у действующих модераторов; обычный пользователь
# получит отказ. Противоположная команда (снятие роли) не реализована —
# роль снимается через карточку пользователя в панели.
# ============================================================

@router.message_created(Command("makemod"))
async def cmd_make_moderator(update: MessageCreated) -> None:
    """
    Назначает пользователя модератором. Использование: /makemod <user_id>.

    Доступно только текущим модераторам.
    """
    user_id = get_user_id(update)

    # Право назначать модераторов есть только у действующих модераторов.
    # Это защита: обычный пользователь через /makemod не сможет повысить себя.
    if not _is_moderator(user_id):
        await update.answer_text("❌ У тебя нет прав модератора.")
        return

    # Разбираем аргументы команды: "/makemod <user_id>".
    # update.text — вся строка команды, делим её по пробелам.
    parts = (update.text or "").split()
    if len(parts) < 2:
        # Не указан user_id — показываем подсказку по синтаксису.
        await update.answer_text(
            "📋 Использование: /makemod <user_id>\n\n"
            "Пример: /makemod 12345678"
        )
        return

    # Второй токен после команды должен быть числом — это и есть user_id.
    try:
        target_id = int(parts[1])
    except ValueError:
        await update.answer_text("❌ Некорректный ID пользователя.")
        return

    # Проверяем, что такой пользователь реально существует в базе.
    if not db.user_exists(target_id):
        await update.answer_text(f"❌ Пользователь с ID {target_id} не найден.")
        return

    # Назначаем роль moderator и приводим пользователя в активное состояние.
    # Три БД-вызова подряд: новая роль, отметка одобрения и снятие блокировки.
    db.set_role(target_id, "moderator")
    db.approve_user(target_id)
    db.set_blocked(target_id, False)

    # Читаем запись, чтобы в ответе показать имя пользователя.
    user = db.get_user(target_id)
    name = user.get("full_name") or user.get("name", "Неизвестно")

    await update.answer_text(
        f"✅ Пользователь {name} (ID: {target_id}) назначен модератором!"
    )


# ============================================================
# КОМАНДА /myid — УЗНАТЬ СВОЙ ID
# ============================================================
# Минимальная команда без аргументов: просто печатает user_id отправителя.
# Не требует прав модератора — доступна каждому пользователю.
# ============================================================

@router.message_created(Command("myid"))
async def cmd_myid(update: MessageCreated) -> None:
    """Показывает ID текущего пользователя."""
    # Команда доступна всем: ID нужен, чтобы передать модератору себя,
    # например для /makemod. Права не проверяем намеренно.
    user_id = get_user_id(update)
    await update.answer_text(f"🆔 Твой ID: {user_id}")


# ============================================================
# ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ
# ============================================================
# Парсеры чисел: оба используются по всему файлу и должны быть
# максимально безопасными — никогда не бросают исключений наружу.
# _parse_id  работает с payload кнопок (формат "<префикс>_<число>"),
# _parse_int работает с пользовательским текстом из FSM-ввода.
# ============================================================

def _parse_id(payload: str) -> int | None:
    """Извлекает число из payload вида "<префикс>_<id>". При ошибке — None."""
    try:
        # Число всегда стоит ПОСЛЕ последнего подчёркивания в payload.
        # Например, "mod_approve_123" -> 123, "mod_skip_5" -> 5.
        # Служит и для вытаскивания index у mod_skip_<N>.
        return int(payload.split("_")[-1])
    except ValueError:
        # Хвост payload оказался не числом — считаем, что это не наш колбэк.
        return None


def _parse_int(text: str) -> int | None:
    """Аккуратно превращает строку в int. При ошибке — None."""
    try:
        # int() сам обрежет лишние пробелы и знак, если они есть.
        return int(text)
    except (TypeError, ValueError):
        # Текст не является целым числом (пусто, буквы, дробь и т.п.).
        # Возвращаем None, чтобы вызывающий код показал подсказку.
        return None