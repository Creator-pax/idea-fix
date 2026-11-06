# ============================================================
# УТИЛИТЫ ДЛЯ ПОЛУЧЕНИЯ ДАННЫХ ПОЛЬЗОВАТЕЛЯ — handlers/user_utils.py
# ============================================================
# В старом maxgram данные пользователя лежали в разных местах в
# зависимости от типа обновления, и приходилось рекурсивно искать
# словарь с user_id. В maxo всё строго типизировано:
#
#   • MessageCreated (новое сообщение):
#       update.user_id    -> int | None (из message.sender)
#       update.message.sender -> Omittable[User] (отправитель)
#       update.text       -> str | None (текст сообщения)
#
#   • MessageCallback (нажатие кнопки):
#       update.user       -> User (это настоящий нажавший пользователь!)
#       update.payload    -> Omittable[str] (данные кнопки)
#       update.callback   -> Callback
#
# ВАЖНАЯ ДЕТАЛЬ: у колбэка поле update.message — это исходное
# сообщение с клавиатурой, и его отправитель — БОТ, а НЕ пользователь.
# Поэтому для колбэков пользователя всегда надо брать из update.user,
# а не из update.message.sender. Ниже есть отдельные функции на каждый
# тип, чтобы не напороться на эту ловушку.
#
# Зачем отдельный файл: get_user_id / resolve_user_id / get_user_name
# вызываются из десятков обработчиков (меню, заметки, ИИ, модерация).
# Собрав их в одном месте, мы гарантируем, что «ловушка бота-отправителя»
# обходится одинаково во всём проекте.
# ============================================================

# Импортируем базу данных под псевдонимом _db. Префикс "_" — служебная
# пометка «внутренний импорт», чтобы имя не путалось с локальными
# переменными функций (например db в других модулях).
import database as _db

# AttachmentType — тип вложения в сообщении (IMAGE / VIDEO / FILE / ...).
from maxo.enums import AttachmentType

# is_defined — проверка, что Omittable-поле реально задано (не «пусто»).
from maxo.omit import is_defined

# Два типа апдейтов, с которыми работает бот: новое сообщение и колбэк.
from maxo.types import MessageCallback, MessageCreated

# ТРИ функции этого файла и их роли:
#   get_user_id     — «быстрый» путь: ID напрямую из апдейта;
#   resolve_user_id — «надёжный» путь: прямой ID либо поиск по chat_id;
#   get_user_name   — имя пользователя из апдейта (для приветствий).
# Обработчики обычно зовут get_user_id; resolve_user_id берётся там, где
# ID из апдейта может оказаться чужим/пустым (пересылки, старые апдейты).


def get_user_id(update: MessageCreated | MessageCallback) -> int | None:
    """
    Безопасно получает user_id пользователя из апдейта.

    Функция работает сразу с обоими типами обновлений:
      • message_created — использует встроенное свойство update.user_id;
      • message_callback — берёт update.user.user_id (настоящего нажавшего).

    Returns:
        Числовой ID пользователя или None, если получить не удалось.
    """
    # isinstance — проверка фактического типа объекта во время выполнения.
    # Если это колбэк (нажатие кнопки), ID берём из update.user.
    if isinstance(update, MessageCallback):
        # У колбэка пользователь всегда есть (это тот, кто нажал кнопку).
        # update.user.user_id — целое число (int) — ID нажавшего.
        return update.user.user_id
    # Ветка выше — единственная, где обращаемся к update.user:
    # у обычного сообщения такого атрибута нет, там пользователь — SENDER.

    # У MessageCreated свойство user_id уже учитывает, что отправителя
    # может не быть (тогда вернётся None).
    return update.user_id
    # ИТОГ: две ветки по типу апдейта, и в каждой — свой «правильный»
    # источник ID. Эта функция — главный рецепт против бага «бота-сэндёра».
    # Если бот когда-нибудь перестанет понимать нажатия кнопок — проблема
    # почти наверняка в том, что где-то не использовали get_user_id/update.user.


def resolve_user_id(update: MessageCreated | MessageCallback) -> int | None:
    """
    Надёжно определяет user_id пользователя с запасным вариантом.

    MAX может отдавать идентификатор пользователя по-разному в разных
    типах обновлений. Иногда единственного user_id из апдейта
    недостаточно — тогда пробуем опознать пользователя по chat_id,
    который сохраняется в БД при регистрации.

    Returns:
        Числовой user_id или None, если опознать не удалось.
    """
    # Пробуем простой способ — взять user_id напрямую из апдейта.
    uid = get_user_id(update)
    # uid может быть None — тогда сразу переходим к запасному пути ниже
    # (поиск по chat_id). if uid is not None — быстрое отсечение пустого
    # результата без лишних обращений к БД.
    if uid is not None:
        try:
            # Проверяем, что такой пользователь действительно есть в БД.
            if _db.user_exists(uid):
                return uid  # найден — это как раз наш пользователь
        except Exception:  # noqa: BLE001
            pass  # если БД временно недоступна — не падаем, идём в fallback ниже
    # try/except здесь — «мягкая» деградация: база может быть недоступна,
    # и это не должно ронять обработку колбэка с ошибкой 500.

    # Fallback: ищем пользователя по chat_id (как в старой версии).
    try:
        # getattr — берём атрибут chat_id, если он есть у данного апдейта
        # (у некоторых типов обновлений его может не быть).
        chat_id = getattr(update, "chat_id", None)
        # find_user_by_chat_id — поиск пользователя по chat_id в БД.
        found = _db.find_user_by_chat_id(chat_id)
        if found is not None:
            return found  # нашли по chat_id — возвращаем его user_id
    except Exception:  # noqa: BLE001
        pass

    return uid  # опознать не удалось — возвращаем исходный (возможно, None)
    # Оба catch-блока «проглатывают» исключения осознанно: resolve_user_id —
    # последняя надежда обработчика узнать ID, падать с ошибкой тут не нужно;
    # в худшем случае вернётся None, и обработчик просто его проигнорирует.


def get_user_name(update: MessageCreated | MessageCallback) -> str:
    """
    Безопасно получает имя пользователя из апдейта.

    Returns:
        Строка с именем или пустая строка, если имени нет.
    """
    # Для колбэка пользователь — простой объект User.
    if isinstance(update, MessageCallback):
        # update.user — объект User нажавшего кнопку.
        user = update.user
        # fullname (полное имя) если есть, иначе name, иначе пустая строка.
        return user.fullname or (user.name or "")

    # У MessageCreated отправитель может отсутствовать (Omittable).
    sender = update.message.sender
    # Оба пути возвращают имя ПО ОДНОЙ схеме: сначала fullname, потом name,
    # потом пусто. Так приветствия выглядят одинаково для кнопок и сообщений.
    if is_defined(sender):  # отправитель реально задан в апдейте
        return sender.fullname or (sender.name or "")  # та же логика имён, что у колбэка

    return ""  # отправителя нет — возвращаем пустую строку
    # Пустая строка, а не None, выбрана специально: вызывающий код почти
    # всегда подставляет имя в текст (f"...{name}..."), и None превратился
    # бы в слово "None"; пустая же строка просто исчезает из сообщения.


# ============================================================
# ВЛОЖЕНИЯ-КАРТИНКИ И ВИДЕО — handlers/user_utils.py
# ============================================================
# MAX не всегда присылает изображение как AttachmentType.IMAGE: часть
# клиентов отправляет его обычным файлом (AttachmentType.FILE) с именем
# вида "hash.jfif" или "hash.png". Из-за этого body.photo пустой, хотя
# картинка реально пришла. Ниже — единая проверка для всех обработчиков.

IMAGE_EXTENSIONS = frozenset({
    ".jpg", ".jpeg", ".jpe", ".jfif", ".png", ".webp", ".bmp", ".gif",
    ".tif", ".tiff",
})
VIDEO_EXTENSIONS = frozenset({
    ".mp4", ".m4v", ".mov", ".webm", ".mkv", ".avi", ".3gp",
})


def attachment_url(attachment) -> str | None:
    """Возвращает прямую ссылку вложения или None, если её нет."""
    payload = getattr(attachment, "payload", None)
    url = getattr(payload, "url", None) if payload is not None else None
    return url if isinstance(url, str) and url else None


def attachment_filename(attachment) -> str:
    """Возвращает имя файла вложения (или имя из URL), если оно есть."""
    name = getattr(attachment, "filename", None)
    if isinstance(name, str) and name:
        return name
    payload = getattr(attachment, "payload", None)
    name = getattr(payload, "filename", None) if payload is not None else None
    if isinstance(name, str) and name:
        return name
    url = attachment_url(attachment) or ""
    path = url.split("?", 1)[0]
    return path.rsplit("/", 1)[-1]


def _attachment_extension(attachment) -> str:
    name = attachment_filename(attachment).lower()
    dot = name.rfind(".")
    return name[dot:] if dot >= 0 else ""


def is_image_attachment(attachment) -> bool:
    """True для фото MAX и для картинки, присланной как обычный файл."""
    if getattr(attachment, "type", None) == AttachmentType.IMAGE:
        return True
    return (
        getattr(attachment, "type", None) == AttachmentType.FILE
        and _attachment_extension(attachment) in IMAGE_EXTENSIONS
    )


def is_video_attachment(attachment) -> bool:
    """True для видео MAX и для ролика, присланного как обычный файл."""
    if getattr(attachment, "type", None) == AttachmentType.VIDEO:
        return True
    return (
        getattr(attachment, "type", None) == AttachmentType.FILE
        and _attachment_extension(attachment) in VIDEO_EXTENSIONS
    )


def _merge_attachments(primary, extra) -> list:
    """Объединяет списки вложений, не дублируя их по URL."""
    merged = list(primary or [])
    seen = {url for a in merged if (url := attachment_url(a))}
    for attachment in extra or []:
        url = attachment_url(attachment)
        if url and url not in seen:
            merged.append(attachment)
            seen.add(url)
        elif not url and attachment not in merged:
            merged.append(attachment)
    return merged


def get_image_attachments(body) -> list:
    """Все картинки сообщения, у которых есть ссылка для скачивания."""
    attachments = _merge_attachments(
        getattr(body, "attachments", None),
        getattr(body, "photo", None),
    )
    return [
        a for a in attachments
        if is_image_attachment(a) and attachment_url(a)
    ]


def get_video_attachments(body) -> list:
    """Все видео сообщения, у которых есть ссылка для скачивания."""
    attachments = _merge_attachments(
        getattr(body, "attachments", None),
        getattr(body, "video", None),
    )
    return [
        a for a in attachments
        if is_video_attachment(a) and attachment_url(a)
    ]