# ============================================================
# ОБРАБОТЧИК ФАЙЛОВ И МЕДИА — handlers/media.py
# ============================================================
# Этот файл отвечает за раздел «Файлы и медиа»:
#   1. Текст с фото (OCR через easyocr);
#   2. Текст из PDF (PyPDF2);
#   3. Распознавание речи из видео (moviepy + SpeechRecognition);
#   4. Ключевые моменты видео (moviepy: анализ кадров + миниатюры).
#
# Тяжёлые библиотеки импортируются ПЛАНОВО (через try/except на уровне
# модуля) и выставляют флаги доступности OCR_AVAILABLE / PDF_AVAILABLE /
# MOVIEPY_AVAILABLE / SPEECH_AVAILABLE. Если зависимость не установлена,
# раздел честно говорит об этом пользователю, а не падает.
#
# Вся синхронная тяжёлая работа (OCR, PDF, ffmpeg-обработка, распознавание
# речи) выполняется через asyncio.to_thread, чтобы не блокировать event
# loop (тот же приём, что и в handlers/ai.py).
#
# Общая схема обработки медиа одинакова во всех четырёх ветках:
#   1. Пользователь нажимает кнопку меню → FSM переводится в состояние
#      «ждём файл» (MediaStates.wait_photo / wait_pdf / ...).
#   2. Пользователь присылает вложение → обработчик handle_media_attachments
#      (StateFilter(MediaStates)) по текущему состоянию выбирает ветку:
#        - wait_photo            → фото  → OCR (easyocr);
#        - wait_pdf              → PDF   → текст (PyPDF2);
#        - wait_video_audio      → видео → распознавание речи (moviepy+S2T);
#        - wait_video_keyframes  → видео → ключевые моменты (анализ кадров).
#   3. Файл скачивается по прямой ссылке (bot.download) во временную папку,
#      созданную через tempfile.mkdtemp.
#   4. Извлечённый текст кладётся во временные данные FSM под ключом
#      media_text — его потом забирают кнопки ИИ-постобработки
#      («Исправить ошибки» / «Решить задание»).
#   5. Временная папка удаляется в finally через _rmtree_safe даже при ошибке.
#
# Ошибки любого этапа пишутся в лог для модераторов (db.save_ai_error),
# в консоль (print) и показываются пользователю понятным сообщением.
#
# Состояния FSM раздела перечислены в handlers/states.py (MediaStates);
# временные данные — ключ media_text (извлечённый текст/моменты), который
# переносится между обработчиками и используется ИИ-постобработкой.
# ============================================================

# --- Стандартная библиотека ---
import asyncio     # asyncio.to_thread для запуска тяжёлых задач в потоках
import os          # работа с путями (временные папки, имена файлов)
import shutil      # удаление временных папок (rmtree) в _rmtree_safe
import tempfile    # создание временных каталогов для скачанных файлов

# --- maxo: фреймворк бота ---
# Пояснение по типам апдейтов, с которыми работаем в этом модуле:
#   MessageCreated  — пользователь ПРИСЛАЛ сообщение (фото/файл/видео).
#   MessageCallback — пользователь НАЖАЛ inline-кнопку (пришёл payload).
# Оба объекта предоставляют фасады-методы (answer_text / edit_message /
# send_media), а доступ к данным — через update.payload / update.message.body.
from maxo import Router                      # регистрация обработчиков этого модуля
from maxo.omit import is_defined             # проверка «поле вообще есть» у объектов API
from maxo.routing.filters import StateFilter # фильтр «сообщение пришло в состоянии FSM»
from maxo.types import MessageCallback, MessageCreated  # типы колбэка / нового сообщения
from maxo.utils.builders import KeyboardBuilder         # построитель inline-клавиатур
from maxo.utils.upload_media import BufferedInputFile   # отправка файлов «из буфера»

# --- проект ---
# database — единая точка доступа к БД: лимиты ИИ (check_ai_limit,
# increment_ai_usage), логи ошибок (save_ai_error), логи запросов (save_ai_log),
# проверки прав (user_exists / is_approved / is_blocked).
import database as db                    # все операции с БД (лимиты, логи, ошибки)
from config import AI_MAX_INPUT_LENGTH, AI_MAX_OUTPUT_LENGTH  # лимиты длин текста для ИИ

from handlers.ai import _ask_ai          # вызов ИИ (синхронная функция, сюда — в потоке)
from handlers.filters import CallbackPayload  # обёртка списка payload'ов для колбэка
from handlers.markdown import markdown_to_plain  # убираем md-пометки из ответа
from handlers.prefs import btn, get_ui_prefs # эмодзи-префиксы и настройки пользователя
from handlers.states import MediaStates       # FSM-состояния раздела «Файлы и медиа»
from handlers.user_utils import (  # извлечение user_id и разбор вложений
    attachment_filename,
    attachment_url,
    get_image_attachments,
    get_user_id,
    get_video_attachments,
)

# Роутер — контейнер всех обработчиков этого модуля; подключается в mian.py.
router: Router = Router(name="media")

# Плановый импорт тяжёлых библиотек: даже если easyocr/moviepy/PyPDF2 не
# установлены, модуль спокойно импортируется, просто флаг = False.
# ----------------------------------------------------------------
# easyocr — нейросеть распознавания текста на фото (OCR). При поднятии
# процесса грузит модели, поэтому импорт и инициализация могут быть долгими.
# Если пакет не установлен — ловим любое исключение и ставим флаг False.
try:
    import easyocr
    OCR_AVAILABLE = True
except Exception:  # noqa: BLE001
    OCR_AVAILABLE = False
# Логика флагов проста: try импортирует модуль, успех → флаг True,
# любое исключение (пакет не установлен, битая сборка) → флаг False.
# Дальше каждый обработчик ПЕРЕД работой проверяет свой флаг и, если он
# False, отправляет пользователю вежливое сообщение «функция недоступна»,
# а не падает с ImportError на всём модуле бота.

# PyPDF2 — чтение текстового слоя из PDF-документов.
try:
    from PyPDF2 import PdfReader
    PDF_AVAILABLE = True
except Exception:  # noqa: BLE001
    PDF_AVAILABLE = False

# moviepy — обработка видео (извлечение аудио, покадровый анализ, кадры).
# Импортируем только сам пакет; конкретный класс берём отдельно в
# _video_clip_class(), т.к. в разных версиях moviepy он лежит по-разному.
try:
    import moviepy  # noqa: F401
    MOVIEPY_AVAILABLE = True
except Exception:  # noqa: BLE001
    MOVIEPY_AVAILABLE = False
# ПОЧЕМУ не импортируем VideoFileClip сразу? В moviepy 2.x класс лежит в
# верхнеуровневом модуле moviepy, а в 1.x — в подмодуле moviepy.editor.
# Чтобы поддержать обе версии, класс достаём отдельной функцией
# _video_clip_class() (см. ниже), которая сама определяет, откуда брать класс.

# speech_recognition — распознавание речи (Google Web Speech API).
try:
    import speech_recognition
    SPEECH_AVAILABLE = True
except Exception:  # noqa: BLE001
    SPEECH_AVAILABLE = False

# ИТОГ по четырём флагам: они обеспечивают «мягкое» вырождение функций, а не
# крах модуля. Какой флаг где проверяется:
#   _process_photo  → OCR_AVAILABLE (easyocr нужен только для фото);
#   _process_pdf    → PDF_AVAILABLE (PyPDF2 только для документов);
#   _process_video  → MOVIEPY_AVAILABLE (база для обоих режимов видео) и
#                     дополнительно SPEECH_AVAILABLE — только для «речи».

# Ограничения.
MEDIA_MAX_SIZE = 60 * 1024 * 1024  # 60 МБ

# --- Параметры поиска «ключевых моментов» видео ---
# Порог «смены сцены» для поиска ключевых моментов видео (средняя разница
# кадров в оттенках серого, 0..255).
_SCENE_CHANGE_THRESHOLD = 18.0
# Сколько ключевых моментов максимум показываем.
_MAX_KEY_FRAMES = 5
# Минимальный интервал между двумя моментами (секунды).
_MIN_SHOT_GAP = 3.0

# ============================================================
# КЛАВИАТУРЫ РАЗДЕЛА «ФАЙЛЫ И МЕДИА»
# ============================================================
# Отдельные функции-фабрики возвращают готовые клавиатуры, чтобы не
# дублировать конструкторы в каждом обработчике.
#   1) _media_menu_keyboard  — главное меню раздела;
#   2) _media_cancel_keyboard — кнопка «Отмена» при ожидании файла;
#   3) _result_keyboard      — кнопки постобработки текста через ИИ;
#   4) _done_keyboard        — кнопки после завершения ИИ-обработки.
#
# Общее правило: функция-фабрика возвращает KeyboardBuilder (НЕ готовый
# объект клавиатуры), а вызывающая сторона в каждом месте самостоятельно
# вызывает .build(), когда формирует сообщение. Так клавиатуру можно
# переиспользовать в разных фасадах (edit_message / answer_text / send_media).
#
# Каждая кнопка помимо текста несёт payload — внутреннюю строку, по которой
# при нажатии роутер направит апдейт в обработчик handle_media_menu (см. ниже).
# Тексты на кнопках — это эмодзи + короткое действие: их видит пользователь,
# а payload — «машинный» идентификатор, которого пользователь не видит.


def _media_menu_keyboard(user_id: int) -> KeyboardBuilder:
    """Клавиатура раздела «Файлы и медиа»."""
    # Функция-фабрика: принимает числовой user_id и возвращает KeyboardBuilder.
    # Важно: возвращается ИМЕННО builder, а не готовый объект — вызывающая
    # сторона сама решает, когда позвать .build() перед отправкой сообщения.
    # Получаем настройки интерфейса (показывать ли эмодзи) для этого пользователя.
    # get_ui_prefs вернёт dict вида {"show_emoji": bool, ...}; если записи в БД
    # нет — будут подставлены значения по умолчанию из handlers/prefs.py.
    prefs = get_ui_prefs(user_id)
    # Создаём пустой построитель — в него по одной добавляем кнопки.
    kb = KeyboardBuilder()
    # btn(flag, text) добавляет/убирает эмодзи в зависимости от настройки.
    # Каждая кнопка несёт payload — его потом ловит handle_media_menu.
    # Payload — это внутренняя строка-идентификатор: по ней роутер понимает,
    # какую кнопку нажали (media_photo / media_pdf / media_speech / ...).
    kb.add_callback(text=btn(prefs["show_emoji"], "Текст с фото"), payload="media_photo")
    kb.add_callback(text=btn(prefs["show_emoji"], "Текст из PDF"), payload="media_pdf")
    kb.add_callback(text=btn(prefs["show_emoji"], "Распознать речь"), payload="media_speech")
    kb.add_callback(text=btn(prefs["show_emoji"], "Ключевые моменты"), payload="media_keyframes")
    # Кнопка возврата в ГЛАВНОЕ меню — её payload "menu_main" обработает
    # уже другой модуль (handlers/menu.py), а не раздел медиа.
    kb.add_callback(text="← Назад в меню", payload="menu_main")
    # adjust(1) — все кнопки в один столбец (одна на строку).
    # Причина та же, что в handlers/ai.py: без adjust кнопки упаковались бы
    # по несколько в ряд, и MAX обрезал бы их тексты до 3–5 символов.
    return kb.adjust(1)


def _media_cancel_keyboard() -> KeyboardBuilder:
    """Клавиатура «Отмена» во время ожидания медиафайла."""
    # Показывается на экранах «пришли файл»: единственная кнопка спасает
    # пользователя, если он передумал присылать файл.
    # Используется в НЕСКОЛЬКИХ местах: это связано с тем, что разные
    # «скелеты» ожидания (фото/PDF/видео) делят одну и ту же клавиатуру.
    kb = KeyboardBuilder()
    # payload="menu_media" возвращает в меню раздела одним нажатием.
    # Обработает её тот же handle_media_menu (ветка menu_media) — там
    # FSM очистится, и ожидание файла будет снято.
    kb.add_callback(text="← Отмена", payload="menu_media")
    return kb.adjust(1)


def _result_keyboard() -> KeyboardBuilder:
    """Кнопки постобработки извлечённого текста."""
    kb = KeyboardBuilder()
    # После извлечения текста предлагаем: исправить ошибки ИИ, решить
    # задание ИИ, завершить («Готово») или вернуться в раздел.
    # Все четыре payload обрабатываются одним колбэком handle_media_menu,
    # который различает их по значению update.payload.
    kb.add_callback(text="✏️ Исправить ошибки", payload="media_ai_fix")
    kb.add_callback(text="📚 Решить задание", payload="media_ai_solve")
    kb.add_callback(text="✅ Готово", payload="media_done")
    kb.add_callback(text="← Файлы и медиа", payload="menu_media")
    return kb.adjust(1)


def _done_keyboard() -> KeyboardBuilder:
    """Кнопки после ИИ-обработки текста."""
    kb = KeyboardBuilder()
    # «Готово» завершает работу с медиа (media_done очистит FSM и текст),
    # «← Файлы и медиа» возвращает в раздел (menu_media — перерисует меню).
    kb.add_callback(text="✅ Готово", payload="media_done")
    kb.add_callback(text="← Файлы и медиа", payload="menu_media")
    return kb.adjust(1)


# ============================================================
# КОЛБЭКИ РАЗДЕЛА «ФАЙЛЫ И МЕДИА»
# ============================================================
# Один обработчик обслуживает ВСЕ кнопки раздела: в декораторе перечислены
# все payload'ы (через CallbackPayload), а внутри — ветвление по update.payload.
#
# Порядок ветвления в обработчике:
#   1) menu_media       — нарисовать меню раздела (и сбросить FSM);
#   2) media_photo / media_pdf / media_speech / media_keyframes —
#      выбрать инструмент и перейти в FSM-состояние «ждём файл»;
#   3) media_done       — завершить работу (очистить FSM);
#   4) media_ai_fix / media_ai_solve — ИИ-постобработка извлечённого текста.

@router.message_callback(
    CallbackPayload(
        "menu_media", "media_photo", "media_pdf", "media_speech",
        "media_keyframes", "media_ai_fix", "media_ai_solve", "media_done",
    )
)
async def handle_media_menu(update: MessageCallback, fsm_context) -> None:
    """Обрабатывает кнопки раздела «Файлы и медиа»."""
    # Формат аргументов: update — колбэк (сообщение от inline-кнопки),
    # fsm_context — объект FSMContext, автоматически подставленный maxo.
    # FSMContext даёт: set_state / get_state / update_data / get_data / clear.
    # Каждый payload из декоратора обрабатывается ВНУТРИ этой функции —
    # ниже идёт цепочка if / elif по значению payload.
    payload = update.payload      # что за кнопка была нажата
    user_id = get_user_id(update) # ID пользователя, нажавшего кнопку

    # «Входные ворота»: работаем только с подтверждёнными, не заблокированными
    # пользователями из БД. Для всех остальных молча выходим.
    # Проверяем ТРИ условия одновременно:
    #   1) запись есть в БД (иначе get акторов ниже упадёт);
    #   2) модератор одобрил заявку (is_approved);
    #   3) пользователь не заблокирован (is_blocked).
    if not db.user_exists(user_id) or not db.is_approved(user_id) or db.is_blocked(user_id):
        return

    # ============================================================
    # ГЛАВНОЕ МЕНЮ РАЗДЕЛА
    # ============================================================
    if payload == "menu_media":
        # Сбрасываем FSM: пользователь мог «застрять» в состоянии ожидания
        # файла, а теперь он вернулся в меню — состояние нужно очистить.
        # fsm_context.clear() удаляет И состояние, И временные данные (media_text).
        await fsm_context.clear()
        # callback_answer — краткая «галочка» с текстом, появляется на кнопке.
        await update.callback_answer(text="Файлы")
        # Перерисовываем текущее сообщение текстом меню и клавиатурой раздела.
        # edit_message сменит и текст, и inline-клавиатуру без нового сообщения.
        await update.edit_message(
            text=(
                "📂 Файлы и медиа\n\n"
                "Я могу помочь с учебными файлами:\n"
                "• Фото с текстом → распознаю текст (OCR)\n"
                "• PDF → вытащу текст из документа\n"
                "• Видео → распознаю речь или найду ключевые моменты\n\n"
                "Выбери, что сделать:"
            ),
            # Строим клавиатуру для ЭТОГО пользователя (учитывая его настройки
            # эмодзи) и сразу переводим в готовый вид через .build().
            keyboard=_media_menu_keyboard(user_id).build(),
        )
        return

    # ============================================================
    # ВЫБОР ИНСТРУМЕНТА: переводим в состояние «ждём файл»
    # ============================================================
    # Что здесь происходит: нажатие кнопки-инструмента (media_photo и т.п.)
    # переключает FSM в соответствующее состояние ожидания. «Ожидание» —
    # это ещё НЕ сам процесс распознавания: файл придёт отдельным сообщением,
    # которое перехватит StateFilter(MediaStates) в handle_media_attachments.
    # Связка «кнопка → состояние» и есть суть данного блока: FSM хранит на
    # стороне maxo, и между обработчиками состояние «переезжает» само.
    # Формат кортежа: (состояние FSM, заголовок, подсказка).
    # Словарь-конфигурация: по каждому payload'у инструмента знаем, в какое
    # состояние FSM перевести пользователя и что ему написать в подсказке.
    # Значения состояний берутся из handlers/states.py (MediaStates):
    #   wait_photo / wait_pdf / wait_video_audio / wait_video_keyframes.
    # Именно эти состояния потом ловит StateFilter(MediaStates) в обработчике
    # handle_media_attachments — так мы связываем «кнопка» и «ожидание файла».
    mode_for_payload = {
        "media_photo": (
            MediaStates.wait_photo,          # дальше ждём фото
            "🖼 Текст с фото!",
            "Пришли фото с текстом (страница учебника, конспект, задание), "
            "и я распознаю текст с него.",
        ),
        # media_pdf — «Текст из PDF»: переводим в wait_pdf, ждём документ .pdf.
        # Кнопка выбора инструмента и сама обработка разделены: здесь только
        # «настраиваем ожидание», а разбором займётся _process_pdf.
        "media_pdf": (
            MediaStates.wait_pdf,            # дальше ждём PDF-файл
            "📄 Текст из PDF!",
            "Пришли документ в формате PDF, и я вытащу из него текст.",
        ),
        # media_speech — «Распознать речь»: ждём видео с речевой дорожкой.
        # Режим речи использует moviepy (извлечение звука) + speech_recognition.
        "media_speech": (
            MediaStates.wait_video_audio,    # дальше ждём видео (для речи)
            "🎬 Распознавание речи!",
            "Пришли видео, и я распознаю произнесённую в нём речь.",
        ),
        # media_keyframes — «Ключевые моменты»: ждём видео для покадрового
        # анализа смен сцен (moviepy + numpy) и выдачи миниатюр-кадров.
        "media_keyframes": (
            MediaStates.wait_video_keyframes,  # дальше ждём видео (для моментов)
            "🎞 Ключевые моменты видео!",
            "Пришли видео, и я найду ключевые моменты и прикреплю кадры.",
        ),
    }
    # Перед переводом в FSM-состояние показываем пользователю заголовок и
    # подсказку (title/hint), чтобы он знал, какой файл от него ждут.

    # Если нажата одна из кнопок инструментов — переводим в нужное состояние.
    # (Все четыре payload инструментов лежат в ключах mode_for_payload; если
    # payload сюда не попал — значит это меню/постобработка, идём ниже.)
    if payload in mode_for_payload:
        # Достаём (состояние, заголовок, подсказку) из словаря.
        # Распаковка кортежа из трёх элементов: MediaStates, str, str.
        state, title, hint = mode_for_payload[payload]
        await fsm_context.clear()          # убираем старое состояние/данные FSM
        await fsm_context.set_state(state) # включаем ожидание файла
        await update.callback_answer(text="Ввод")
        # Показываем заголовок и подсказку + кнопку «Отмена».
        # Заголовок с эмодзи (title) и текст-подсказку (hint) пользователь
        # видит в отредактированном сообщении; «Отмена» возвращает в меню
        # раздела (payload "menu_media" — тот же обработчик выше).
        await update.edit_message(
            text=f"{title}\n\n{hint}",
            keyboard=_media_cancel_keyboard().build(),
        )
        return

    # ============================================================
    # ПОСТОБРАБОТКА ИЗВЛЕЧЁННОГО ТЕКСТА ЧЕРЕЗ ИИ
    # ============================================================
    # Сюда попадаем только при payload media_ai_fix / media_ai_solve / media_done.
    # Извлечённый ранее текст хранится во временных данных FSM.
    # get_data() возвращает ВСЮ временную «корзинку», а не одно поле —
    # поэтому читаем ключ media_text через dict.get с запасным значением "".
    data = await fsm_context.get_data()
    media_text = (data.get("media_text") or "").strip()

    if payload == "media_done":
        # Пользователь закончил работу с медиа: очищаем FSM и прощаемся.
        # Текст в FSM (media_text) тоже удаляется — повторная кнопка «Исправить
        # ошибки» уже не найдёт текста, и это корректно (работа завершена).
        await fsm_context.clear()
        await update.callback_answer(text="Готово")
        # Вместо прощального текста рисуем меню раздела, чтобы было удобно
        # продолжить работу с другими файлами одним нажатием.
        await update.edit_message(
            text="Отлично! Если что-то ещё понадобится — загляни в разделы бота. 📚",
            keyboard=_media_menu_keyboard(user_id).build(),
        )
        return

    # media_ai_fix («исправить ошибки распознавания») и media_ai_solve
    # («решить задание») — обе идут через ИИ, отличаются только промптом.
    if payload in ("media_ai_fix", "media_ai_solve"):
        if not media_text:
            # Нечего обрабатывать — текст так и не был извлечён.
            # Если media_text пустой, ИИ некуда применить промпт — доходим
            # до отказа сразу, не дёргая сервер.
            await update.callback_answer(text="Пусто")
            await update.edit_message("❌ Текст не найден. Пришли медиафайл заново.")
            return

        # Проверяем дневной лимит запросов к ИИ (счётчик в БД).
        # check_ai_limit сверяет число уже сделанных запросов с нормой
        # ai_daily_limit (по умолчанию 50, см. database.py).
        if not db.check_ai_limit(user_id):
            await update.callback_answer(text="Лимит")
            await update.edit_message("❌ Лимит запросов к ИИ на сегодня исчерпан!")
            return

        # Сообщаем начало обработки и меняем сообщение на «думаю...».
        await update.callback_answer(text="Думаю")
        await update.edit_message("🤔 Обрабатываю текст...")

        # Выбираем промпт: исправление ошибок или решение задания.
        # _ask_ai — синхронная (блокирующая) функция, гоняем её в отдельном
        # потоке через asyncio.to_thread, чтобы не блокировать event loop.
        # media_text обрезаем до AI_MAX_INPUT_LENGTH — лимит контекста ИИ.
        # Результат _ask_ai — кортеж (answer, ai_error): либо текст ответа,
        # либо None + строка-причина ошибки.
        prompt = _FIX_PROMPT if payload == "media_ai_fix" else _SOLVE_PROMPT
        answer, ai_error = await asyncio.to_thread(
            _ask_ai, prompt, media_text[:AI_MAX_INPUT_LENGTH]
        )

        if answer is None:
            # ИИ не ответил — сохраняем ошибку в лог для модераторов и
            # сообщаем пользователю причину (если ИИ её вернул).
            # try/except вокруг записи лога: сбой при записи лога не должен
            # ронять обработку в самом неподходящем месте.
            try:
                db.save_ai_error(user_id, "media_ai", ai_error or "Неизвестная ошибка ИИ")
            except Exception:  # noqa: BLE001
                pass  # запись лога не критична — не даём ей уронить обработку
            # Возвращаем сообщение с причиной и клавиатурой результата —
            # пользователь может попробовать снова («Исправить» опять).
            await update.edit_message(
                text=(
                    "❌ Не удалось получить ответ от ИИ.\n\n"
                    f"ℹ️ Причина: {ai_error or 'неизвестная ошибка'}\n\n"
                    "Попробуй ещё раз позже."
                ),
                keyboard=_result_keyboard().build(),
            )
            return

        # Успешный ответ: учитываем запрос в дневном лимите и пишем в лог.
        # increment_ai_usage — +1 к суточному счётчику; save_ai_log сохраняет
        # (кто, какой текст, какой ответ) для панели модератора/учителя.
        db.increment_ai_usage(user_id)
        db.save_ai_log(user_id, ("ИИ-обработка медиа: " + media_text[:100]), answer)

        # Если ответ больше лимита сообщения — отправляем файлом .txt.
        # AI_MAX_OUTPUT_LENGTH — порог (см. config.py); файл собираем как
        # BufferedInputFile.file(байты_UTF8, имя) и шлём через send_media.
        # В файл идёт текст без markdown-пометок (###, **).
        if len(answer) > AI_MAX_OUTPUT_LENGTH:
            file_data = markdown_to_plain(answer).encode("utf-8")
            await update.send_media(
                media=BufferedInputFile.file(file_data, "Ответ_ИИ.txt"),
                text="📄 Ответ получился длинным — сохранил его в файл!",
                keyboard=_done_keyboard().build(),
            )
            return

        # Короткий ответ — печатаем прямо в сообщении.
        # edit_message заменяет «🤔 Обрабатываю текст...» на сам ответ.
        # Markdown-пометки модели убираем — пользователь видит чистый текст.
        await update.edit_message(
            markdown_to_plain(answer), keyboard=_done_keyboard().build()
        )
        return


# ============================================================
# МЕДИАФАЙЛЫ, ПРИСЛАННЫЕ ВО ВРЕМЯ ОЖИДАНИЯ
# ============================================================
# Обработчик «ловит» любые новые сообщения, пока пользователь находится в
# одном из состояний MediaStates (см. StateFilter(MediaStates) в декораторе).
# Дальше ветвление идёт по конкретному состоянию: к фото, PDF или видео.

@router.message_created(StateFilter(MediaStates))
async def handle_media_attachments(update: MessageCreated, fsm_context) -> None:
    """Принимает медиафайл, соответствующий текущему состоянию FSM."""
    # update здесь — MessageCreated (новое сообщение), его данные лежат
    # в update.message.body: фото/файлы/видео/текст присланного сообщения.
    user_id = get_user_id(update)
    # Та же проверка «входных ворот», что и в колбэке.
    # Если пользователь не в базе / не одобрен / заблокирован — игнорируем.
    if not db.user_exists(user_id) or not db.is_approved(user_id) or db.is_blocked(user_id):
        return

    # Текущее состояние FSM решает, что именно мы ждём.
    # get_state() вернёт одно из состояний MediaStates (строку вида
    # "MediaStates:wait_photo"). По нему выбираем ветку обработки ниже.
    state = await fsm_context.get_state()
    body = update.message.body   # содержимое присланного сообщения
    # Строение body у MessageCreated:
    #   body.photo — список присланных фотографий (альбом = несколько);
    #   body.file  — вложенный документ/файл (один на сообщение);
    #   body.video — список видео (как и фото, может быть альбомом);
    #   update.text — текст сообщения (пустой, если прислали только медиа).

    # --- ожидание фото (OCR) ---
    if state == MediaStates.wait_photo:
        # Берём первое фото из присланного альбома (или None, если фото нет).
        # body.photo — список вложений-фотографий в сообщении; [0] — первое.
        photos = get_image_attachments(body)
        photo = photos[0] if photos else None
        if photo is None:
            # Прислали не фото — подсказываем, что нужен именно снимок.
            # Состояние FSM НЕ меняем: пользователь остаётся в wait_photo и
            # может прислать корректное фото следующим сообщением.
            await update.answer_text(
                "🖼 Пришли именно фото с текстом.",
                keyboard=_media_cancel_keyboard().build(),
            )
            return
        # Фото есть — передаём в приватную обработку OCR (отдельная функция).
        await _process_photo(update, fsm_context, user_id, photo)
        return

    # --- ожидание PDF ---
    if state == MediaStates.wait_pdf:
        file = body.file
        # Проверяем, что это документ и у него расширение .pdf.
        # file — вложение типа «файл»; filename — имя файла, присланное юзером.
        # Приводим имя к нижнему регистру и смотрим окончание ".pdf".
        if file is None or not attachment_filename(file).lower().endswith(".pdf"):
            await update.answer_text(
                "📄 Пришли документ в формате PDF.",
                keyboard=_media_cancel_keyboard().build(),
            )
            return
        # Расширение корректное — начинаем извлечение текста из PDF.
        await _process_pdf(update, fsm_context, user_id, file)
        return

    # --- ожидание видео (речь или ключевые моменты) ---
    if state in (MediaStates.wait_video_audio, MediaStates.wait_video_keyframes):
        videos = get_video_attachments(body)
        video = videos[0] if videos else None
        if video is None:
            # Прислали не видео — просим именно ролик.
            await update.answer_text(
                "🎬 Пришли именно видео.",
                keyboard=_media_cancel_keyboard().build(),
            )
            return
        # Определяем режим обработки видео по состоянию FSM.
        # Если пользователь выбирал «Ключевые моменты» → keyframes,
        # во всех остальных видео-состояниях (включая wait_video_audio) → speech.
        mode = "keyframes" if state == MediaStates.wait_video_keyframes else "speech"
        # mode передаётся в _process_video и разводит две ветки анализа ролика.
        await _process_video(update, fsm_context, user_id, video, mode)
        return

    # Любое другое сообщение в медиа-состоянии — возвращаем в меню.
    # (Если пользователь прислал, например, текст, а мы ждём файл.)
    # FSM при этом НЕ сбрасываем: сообщение могло быть случайным, и юзер
    # ещё может прислать нужный файл. Просто показываем меню-подсказку.
    await update.answer_text(
        "📂 Файлы и медиа\n\nВыбери действие:",
        keyboard=_media_menu_keyboard(user_id).build(),
    )


# ============================================================
# ФОТО (OCR)
# ============================================================
# OCR расшифровывается как Optical Character Recognition — распознавание
# печатного/рукописного текста по картинке. Здесь нейросеть easyocr.
# Весь конвейер фото:
#   1) проверка флага OCR_AVAILABLE (см. шапку) — библиотека установлена?
#   2) получение прямой ссылки photo.payload.url на файл изображения;
#   3) скачивание в temp-папку через _download_to (bot.download);
#   4) вызов _ocr_image в отдельном потоке (asyncio.to_thread) — так как
#      распознавание на CPU занимает секунды и блокирует event loop;
#   5) сохранение текста в FSM (media_text) и вывод через _send_result.

async def _process_photo(update: MessageCreated, fsm_context, user_id: int, photo) -> None:
    """Распознаёт текст на фото через easyocr."""
    # Входные данные: photo — объект вложения (photo), у которого есть поле
    # .payload.url — прямая временная ссылка на изображение для скачивания.
    if not OCR_AVAILABLE:
        # Библиотека не установлена — честно сообщаем и возвращаем в меню.
        # Флаг OCR_AVAILABLE выставляется при импорте модуля (см. шапку файла);
        # если он False, вычисления всё равно невозможны, поэтому уходим сразу.
        await update.answer_text(
            "❌ Распознавание текста с фото пока недоступно.\n\n"
            "Библиотека easyocr не установлена. Попроси администратора "
            "установить зависимости из requirements.txt.",
            keyboard=_media_menu_keyboard(user_id).build(),
        )
        return

    # Для скачивания нужна прямая ссылка на файл фото из payload вложения.
    # .url — это уже готовая для bot.download ссылка на медиа-файл.
    url = attachment_url(photo)
    if not url:
        await update.answer_text("❌ Не удалось получить ссылку на фото.")
        return

    # Стартовое сообщение «обрабатываю», чтобы пользователь не думал, что бот завис.
    # OCR на CPU занимает несколько секунд — без этого сообщения пауза
    # выглядела бы как молчание бота.
    await update.answer_text("🖼 Обрабатываю фото...")

    # Вся работа — во временной папке, которую гарантированно уберём в finally.
    # tempfile.mkdtemp создаёт уникальный временный каталог; префикс maxo_media_
    # помогает опознавать такие папки при ручной уборке.
    tmp = tempfile.mkdtemp(prefix="maxo_media_")
    try:
        path = os.path.join(tmp, "photo.jpg")       # локальный файл для easyocr
        await _download_to(url, path)               # скачивание через bot.download
        # _ocr_image — тяжёлая синхронная работа (нейросеть!), в потоке.
        # asyncio.to_thread переносит её в отдельный поток — event loop при
        # этом продолжает обслуживать других пользователей.
        ocr_text = await asyncio.to_thread(_ocr_image, path)
    except Exception as e:  # noqa: BLE001
        # Любая ошибка распознавания: логируем в консоль и в БД для модератора.
        # save_ai_error упакует (user_id, "media_ocr", текст ошибки) — эти
        # записи потом видит модераторская панель.
        print(f"MEDIA: OCR error: {e}")
        try:
            db.save_ai_error(user_id, "media_ocr", str(e))
        except Exception:  # noqa: BLE001
            pass   # запись лога — не фатальна для ответа пользователю
        await update.answer_text(
            "❌ Не удалось распознать текст на фото.\n"
            "Попробуй фото получше и ещё раз.",
            keyboard=_media_menu_keyboard(user_id).build(),
        )
        return
    finally:
        _rmtree_safe(tmp)   # временная папка всегда удаляется, даже при ошибке
        # Порядок гарантии: finally выполняется и после успеха (когда текст
        # уже в FSM), и после исключения — засорять диск мы не будем.

    # Кладём распознанный текст во временные данные FSM — его заберут
    # кнопки «Исправить ошибки» / «Решить задание» (строки 201-259).
    # update_data(media_text=...) дополняет «корзинку» FSM одним ключом.
    ocr_text = (ocr_text or "").strip()
    await fsm_context.update_data(media_text=ocr_text)
    # Показываем результат с клавиатурой постобработки.
    # _send_result сам решит: текст в сообщении или (если длинный) файлом.
    await _send_result(update, "🖼 Готово! Вот текст с фото:", ocr_text)


# ============================================================
# PDF
# ============================================================
# Извлечение текста из PDF делится на два принципиально разных случая:
#   • PDF с «текстовым слоем» (создан в Word/LaTeX) → PyPDF2 умеет вытянуть
#     текст напрямую через extract_text();
#   • PDF-скан (фотографии страниц) → текстового слоя нет, и PyPDF2 вернёт
#     пустоту — именно поэтому пользователю показывается «возможно, это скан».
# Конвейер PDF повторяет фото: флаг PDF_AVAILABLE → ссылка → лимит 60 МБ →
# скачивание → чтение в потоке → FSM (media_text) → _send_result.

async def _process_pdf(update: MessageCreated, fsm_context, user_id: int, file) -> None:
    """Вытаскивает текст из PDF через PyPDF2."""
    # file — вложение-документ; нужные поля: file.filename (имя), file.size
    # (размер в байтах) и file.payload.url (прямая ссылка для скачивания).
    if not PDF_AVAILABLE:
        # PyPDF2 не установлен — сообщаем и предлагаем вернуться в меню.
        await update.answer_text(
            "❌ Чтение PDF пока недоступно.\n\n"
            "Библиотека PyPDF2 не установлена. Попроси администратора "
            "установить зависимости из requirements.txt.",
            keyboard=_media_menu_keyboard(user_id).build(),
        )
        return

    url = attachment_url(file)   # прямая ссылка на файл документа
    if not url:
        await update.answer_text("❌ Не удалось получить ссылку на файл.")
        return

    # Лимит размера вложения (60 МБ) — защита от слишком больших PDF.
    # MEDIA_MAX_SIZE = 60*1024*1024 байт; file.size может быть None (неизвестен)
    # — тогда проверка пропускается (условие or None ложно).
    if file.size and file.size > MEDIA_MAX_SIZE:
        await update.answer_text("❌ Файл слишком большой (максимум 60 МБ).")
        return

    # Сообщение «обрабатываю» — сразу, до долгого скачивания и разбора.
    await update.answer_text("📄 Обрабатываю PDF...")

    # Временная папка для файла; удаляем её в finally (см. ниже).
    tmp = tempfile.mkdtemp(prefix="maxo_media_")
    try:
        # _safe_filename экранирует имя файла от путей и спецсимволов.
        # Имя файла приходит от пользователя — это потенциально опасные данные
        # (могут содержать пути типа ../), поэтому его обязательно чистим.
        name = _safe_filename(attachment_filename(file) or "doc.pdf")
        path = os.path.join(tmp, name)
        # Скачиваем документ на диск, после чего его прочитает _pdf_text.
        await _download_to(url, path)
        # Чтение текстового слоя PDF — синхронная работа, запускаем в потоке.
        # asyncio.to_thread не даёт заморозить event loop на время разбора.
        pdf_text = await asyncio.to_thread(_pdf_text, path)
    except Exception as e:  # noqa: BLE001
        # Ошибка чтения/скачивания — пишем в консоль и в лог модератора.
        print(f"MEDIA: PDF error: {e}")
        try:
            db.save_ai_error(user_id, "media_pdf", str(e))
        except Exception:  # noqa: BLE001
            pass   # ошибка логирования не должна перекрывать основной ответ
        await update.answer_text(
            "❌ Не удалось прочитать PDF.\n\n"
            "Возможно, файл повреждён или это скан без текстового слоя.",
            keyboard=_media_menu_keyboard(user_id).build(),
        )
        return
    finally:
        _rmtree_safe(tmp)
        # Уборка запускается в любом сценарии: файл мог даже не докачаться —
        # но временную папку всё равно необходимо подчистить.

    # Аналогично фото: сохраняем текст в FSM и показываем результат.
    # Так кнопки постобработки («Исправить ошибки» и др.) найдут текст.
    pdf_text = (pdf_text or "").strip()
    await fsm_context.update_data(media_text=pdf_text)
    await _send_result(update, "📄 Готово! Вот текст из PDF:", pdf_text)


# ============================================================
# ВИДЕО (речь / ключевые моменты)
# ============================================================
# Единственная ветвь, где обработкам «речи» и «ключевых моментов» предшествует
# общий этап: получить детали видео по токену и скачать файл по прямой ссылке.
# Дальше разветвляемся по mode ("speech" / "keyframes").
#
# порядок общего этапа видео:
#   1) флаг MOVIEPY_AVAILABLE (а для речи и SPEECH_AVAILABLE);
#   2) video.payload.token → запрос деталей у bot.get_video_attachment_details;
#   3) выбор лучшего разрешения через _pick_video_url (720p → ... → 144p);
#   4) скачивание ролика во временную папку;
#   5) развилка: speech → _extract_speech (WAV + Google), keyframes →
#      _extract_key_moments (анализ кадров + JPG-миниатюры);
#   6) текст/моменты → FSM (media_text) → вывод, временные файлы удаляются.

async def _process_video(update: MessageCreated, fsm_context, user_id: int, video, mode: str) -> None:
    """Обрабатывает видео: распознавание речи или ключевые моменты."""
    # mode — строка: "speech" (распознавание речи) или "keyframes" (моменты).
    # Сначала общие проверки доступности библиотек, затем общее скачивание,
    # а в конце — развилка по mode на две ветки анализа видео.
    if not MOVIEPY_AVAILABLE:
        # moviepy нужен обоим режимам (извлечение аудио и кадров).
        await update.answer_text(
            "❌ Обработка видео пока недоступна.\n\n"
            "Библиотека moviepy не установлена. Попроси администратора "
            "установить зависимости из requirements.txt.",
            keyboard=_media_menu_keyboard(user_id).build(),
        )
        return

    # Режиму «речь» дополнительно нужен speech_recognition.
    # Для «ключевых моментов» speech_recognition НЕ нужен — поэтому проверка
    # ставится только на ветку mode == "speech".
    if mode == "speech" and not SPEECH_AVAILABLE:
        await update.answer_text(
            "❌ Распознавание речи пока недоступно.\n\n"
            "Библиотека SpeechRecognition не установлена. Попроси "
            "администратора установить зависимости из requirements.txt.",
            keyboard=_media_menu_keyboard(user_id).build(),
        )
        return

    # Токен нужен для запроса деталей. Если видео пришло обычным файлом,
    # токена может не быть — тогда используем прямую ссылку из payload.
    token = getattr(getattr(video, "payload", None), "token", None)
    direct_url = attachment_url(video)
    if not token and not direct_url:
        await update.answer_text("❌ Не удалось получить ссылку на видео.")
        return

    await update.answer_text("🎬 Обрабатываю видео...")

    # Получаем прямые ссылки на разные разрешения ролика.
    # bot импортируется здесь (а не в шапке), чтобы не создавать циклический
    # импорт: bot_instance тянет роутеры, включая наш модуль.
    from bot_instance import bot
    video_url = direct_url
    if token:
        try:
            # get_video_attachment_details возвращает детали вложения, включая
            # набор URL по разрешениям (mp4_720 / mp4_480 / ... / mp4_144).
            details = await bot.get_video_attachment_details(video_token=token)
        except Exception as e:  # noqa: BLE001
            # Сеть/API недоступны — если прямой ссылки нет, сообщаем об ошибке.
            print(f"MEDIA: video details error: {e}")
            if not direct_url:
                await update.answer_text(
                    "❌ Не удалось получить информацию о видео.",
                    keyboard=_media_menu_keyboard(user_id).build(),
                )
                return
        else:
            # fields могли не прийти — is_defined проверяет «поле существует».
            urls = details.urls if is_defined(details.urls) else None
            # Выбираем наилучшее из доступных разрешений (720p → 144p).
            video_url = _pick_video_url(urls) or direct_url

    if not video_url:
        await update.answer_text(
            "❌ Не удалось получить прямую ссылку на видео.",
            keyboard=_media_menu_keyboard(user_id).build(),
        )
        return

    # С этого момента обе ветки (speech и keyframes) работают с локальной
    # копией файла; скачиваем её в уникальную временную папку.
    tmp = tempfile.mkdtemp(prefix="maxo_media_")
    try:
        video_path = os.path.join(tmp, "video.mp4")   # локальная копия видео
        await _download_to(video_url, video_path)

        if mode == "speech":
            # Извлечение речи: moviepy вытаскивает звук, speech_recognition
            # распознаёт. Вся тяжёлая синхронная работа — в потоке.
            # _extract_speech возвращает кортеж (текст, ошибка): при неудаче
            # текст=None, а ошибка — человекочитаемая строка о причине.
            transcript, error = await asyncio.to_thread(_extract_speech, video_path, tmp)
            if error:
                # Функция вернула текст ошибки — показываем её пользователю.
                await update.answer_text(
                    f"❌ Не удалось распознать речь.\n\nℹ️ {error}",
                    keyboard=_media_menu_keyboard(user_id).build(),
                )
                return
            transcript = (transcript or "").strip()
            # Сохраняем распознанный текст в FSM для дальнейшей постобработки.
            # Это тот же ключ media_text, что и у фото/PDF — кнопки
            # «Исправить ошибки» / «Решить задание» работают одинаково.
            await fsm_context.update_data(media_text=transcript)
            await _send_result(update, "🎬 Готово! Вот что было сказано в видео:", transcript)
            return

        # --- режим «ключевые моменты» ---
        # Анализ кадров + сохранение миниатюр — синхронная тяжёлая задача.
        # Возвращает список кортежей (секунды, путь_к_миниатюре).
        key_frames = await asyncio.to_thread(_extract_key_moments, video_path, tmp)
        if not key_frames:
            # Резких смен сцены не нашлось (или не спаслись кадры).
            await update.answer_text(
                "😔 Не удалось выделить ключевые моменты в этом видео.",
                keyboard=_media_menu_keyboard(user_id).build(),
            )
            return

        # Список таймкодов (формат HH:MM:SS / MM:SS) — по одному на кадр.
        # _fmt_ts переводит секунды (float) в читаемое время.
        lines = [f"• {_fmt_ts(t)}" for t, _ in key_frames]
        text = "🎞 Ключевые моменты видео:\n\n" + "\n".join(lines)
        # Текст моментов тоже храним в FSM (для ИИ-постобработки).
        await fsm_context.update_data(media_text=text)

        # Читаем миниатюры кадров и оборачиваем их в BufferedInputFile —
        # так их можно прислать группой фотографий (media=media_inputs).
        # Кадр считывается с диска в байты, затем BufferedInputFile.image
        # формирует из байтов «виртуальный файл» изображения для отправки.
        media_inputs = []
        for i, (_, path) in enumerate(key_frames, 1):
            try:
                with open(path, "rb") as f:
                    data = f.read()
            except OSError:
                continue   # битый кадр пропускаем, остальные шлём
            media_inputs.append(BufferedInputFile.image(data, f"frame_{i}.jpg"))

        if len(media_inputs) > 1:
            # Несколько кадров — отправляем как альбом (media принимает список).
            # send_media со списком BufferedInputFile шлёт группу фотографий.
            # Обратите внимание: список внутри одного send_media — это один
            # «медиаальбом» из кадров ключевых моментов видео.
            await update.send_media(
                media=media_inputs,
                text=text,
                keyboard=_result_keyboard().build(),
            )
        elif media_inputs:
            # Один кадр — отправляем просто как одиночную фотографию.
            await update.send_media(
                media=media_inputs[0],
                text=text,
                keyboard=_result_keyboard().build(),
            )
        else:
            # Кадры не спаслись — показываем хотя бы список таймкодов текстом.
            await update.answer_text(text, keyboard=_result_keyboard().build())
    except Exception as e:  # noqa: BLE001
        # Общий перехват для всего видео-конвейера (скачивание, ffmpeg, ...).
        # Сюда попадают ошибки скачивания, работы moviepy, чтения файлов.
        print(f"MEDIA: video processing error: {e}")
        try:
            db.save_ai_error(user_id, "media_video", str(e))
        except Exception:  # noqa: BLE001
            pass   # ошибка записи лога не должна маскировать исходную
        await update.answer_text(
            "❌ Не удалось обработать видео.\n"
            "Попробуй другое видео или повтори позже.",
            keyboard=_media_menu_keyboard(user_id).build(),
        )
    finally:
        _rmtree_safe(tmp)   # временные файлы (mp4/wav/jpg) всегда подчищаем


# ============================================================
# ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ
# ============================================================
# Общие помощники раздела: показ результата, уборка временных файлов,
# безопасные имена файлов, скачивание и выбор URL видео.
# Все они — маленькие функции с единственной ответственностью:
#   • _send_result    — единая точка вывода извлечённого текста;
#   • _rmtree_safe    — безопасное удаление временной папки (всегда успех);
#   • _safe_filename  — санитизация имени файла (безопасность);
#   • _download_to    — скачивание медиа по прямой ссылке через bot.download;
#   • _pick_video_url — выбор лучшего разрешения из деталей вложения;
#   • _fmt_ts         — секунды → читаемое время HH:MM:SS / MM:SS.

async def _send_result(update: MessageCreated, header: str, content: str) -> None:
    """
    Показывает извлечённый текст, при необходимости отправляя файлом.
    """
    # Общая точка вывода результата для фото/PDF/речи: принимает заголовок
    # (header) и сам текст (content), а решение «сообщение или файл»
    # принимает один раз здесь — код не дублируется в каждом обработчике.
    if not content:
        # Пустой результат — честно говорим, что текст не извлёкся.
        # get_user_id(update) or 0 — защита от None в user_id для клавиатуры.
        await update.answer_text(
            "😔 Не удалось извлечь текст из вложения.",
            keyboard=_media_menu_keyboard(get_user_id(update) or 0).build(),
        )
        return

    if len(content) > AI_MAX_OUTPUT_LENGTH:
        # Текст слишком длинный для одного сообщения — отдаём файлом .txt.
        # Кодируем строку в UTF-8 (байты) и оборачиваем в BufferedInputFile,
        # чтобы send_media смог отправить это как вложение .txt документа.
        file_data = content.encode("utf-8")
        await update.send_media(
            media=BufferedInputFile.file(file_data, "Извлечённый_текст.txt"),
            text=f"{header}\n\n📄 Текст большой — сохранил его в файл:",
            keyboard=_result_keyboard().build(),
        )
        return

    # Обычный случай: печатаем заголовок + извлечённый текст в сообщении.
    # Клавиатура _result_keyboard подсказывает следующий шаг пользователю.
    await update.answer_text(f"{header}\n\n{content}", keyboard=_result_keyboard().build())


def _rmtree_safe(path: str) -> None:
    """Удаляет временную папку, игнорируя ошибки."""
    # Многоразовый помощник: вызывается в finally всех обработчиков медиа,
    # чтобы временные файлы не копились на диске сервера.
    try:
        # ignore_errors=True гасит «файл занят/не найден» и другие ошибки ОС.
        # Например, ffmpeg-процесс мог не закрыть файл — rmtree всё равно
        # попытается удалить остальное.
        shutil.rmtree(path, ignore_errors=True)
    except Exception:  # noqa: BLE001
        pass   # не даём уборке временных файлов уронить основной workflow


def _safe_filename(name: str) -> str:
    """Чистит имя файла от путей и небезопасных символов."""
    # Имя приходит из присланного пользователем файла — это недоверенные
    # данные: внутри может быть относительный путь (например "../../x.pdf"),
    # поэтому имя обязательно очищаем перед записью на диск.
    name = os.path.basename(name or "doc.pdf")
    # basename отбрасывает любые пути — остаётся только имя файла
    # (защита от «../…» в имени файла).
    # Оставляем только буквы/цифры и символы ., _,- — остальное выкидываем.
    # Символы вроде «/», «\», «:» и пробелы убираются, чтобы имя было
    # безопасным для записи на диск в обеих ОС.
    cleaned = "".join(c for c in name if c.isalnum() or c in "._-")
    return cleaned or "doc.pdf"   # пустой результат заменяем именем по умолчанию


async def _download_to(url: str, destination: str) -> None:
    """Скачивает файл по прямой ссылке через встроенный bot.download."""
    # Единая точка скачивания: и фото, и PDF, и видео скачиваются одинаково —
    # асинхронно, без блокировки event loop. bot — точка доступа к API.
    # bot импортируем локально (см. комментарий в _process_video про циклы).
    # destination — локальный путь, куда будет записан файл (уже во временной
    # папке); url — прямая ссылка на медиа, полученная из payload вложения.
    from bot_instance import bot
    await bot.download(url, destination=destination)


def _pick_video_url(urls) -> str | None:
    """Выбирает первое доступное разрешение видео (720p → 144p)."""
    # urls — объект деталей вложения с атрибутами-URL по разрешениям.
    if urls is None:
        return None   # деталей нет вообще — вернуть None, вызывающий откажет
    # Идём от лучшего разрешения к худшему — берём первый существующий файл.
    # Порядок перебора определяет приоритет: если есть 720p — возьмём его,
    # иначе 480p, потом 360p и т.д. до самого маленького 144p.
    for attr in ("mp4_720", "mp4_480", "mp4_360", "mp4_240", "mp4_144"):
        raw = getattr(urls, attr)
        # is_defined — «атрибут реально присутствует в ответе API».
        # Проверяем и наличие поля (is_defined), и непустоту значения (raw),
        # чтобы не вернуть пустую строку вместо рабочей ссылки.
        if is_defined(raw) and raw:
            return raw   # нашли рабочее разрешение — сразу возвращаем URL
    return None   # ни одно разрешение не оказалось доступным


def _fmt_ts(seconds: float) -> str:
    """Форматирует секунды как время HH:MM:SS / MM:SS."""
    s = max(0, int(seconds))          # отрицательные секунды не бывают
    h, rem = divmod(s, 3600)          # часы и остаток
    m, sec = divmod(rem, 60)          # минуты и секунды в остатке
    if h:
        # Есть часы (видео ≥ 1 часа) → формат HH:MM:SS с ведущими нулями.
        return f"{h:02d}:{m:02d}:{sec:02d}"   # длинные видео — с часами
    # Видео короче часа → формат MM:SS (без блока часов).
    return f"{m:02d}:{sec:02d}"               # короткие — MM:SS


# ============================================================
# OCR (фото)
# ============================================================
# easyocr.Reader — это ОДНА тяжёлая общая модель (скачивает/держит сети
# в памяти). Её инициализация занимает ~1 секунду и требует ~1 ГБ памяти,
# поэтому делаем её лениво и ровно один раз на процесс (singleton).
#
# Почему singleton критичен: каждый экземпляр easyocr.Reader подгружает в
# память свёрточные сети распознавания (~сотни МБ). Если бы мы создавали
# Reader на каждый запрос — к 10-му фото бот упал бы от нехватки памяти.
# Поэтому объект один на весь процесс, а доступ к нему — через функцию
# _get_ocr_reader(), которая создаёт его только при первом вызове.
#
# Дополнительно ленивая инициализация ускоряет СТАРТ бота: пока никто не
# прислал фото с текстом, загрузка моделей OCR вообще не происходит.

_ocr_reader = None   # кеш объекта easyocr.Reader (глобальный singleton)


def _get_ocr_reader():
    """Лениво создаёт easyocr.Reader (тяжёлая инициализация: ~1 сек)."""
    # «Ленивое» создание: Reader инициализируется НЕ при импорте модуля, а
    # только при первом реальном распознавании. Это значит, что бот без
    # единого OCR-запроса вообще не тратит память/время на загрузку моделей.
    # global — меняем модульную переменную _ocr_reader.
    global _ocr_reader
    if _ocr_reader is None:
        # Языки: русский + английский. gpu=False — работаем на CPU (совместимо
        # с любым окружением; на GPU инициализация ещё быстрее, но требует CUDA).
        # singleton: повторные вызовы вернут тот же объект Reader — повторно
        # грузить нейросети в память не нужно (это ~1 ГБ на каждый Reader!).
        _ocr_reader = easyocr.Reader(["ru", "en"], gpu=False)
    # Возвращаем готовый (уже созданный или созданный сейчас) Reader.
    return _ocr_reader


def _ocr_image(path: str) -> str:
    """Распознаёт текст на изображении. Возвращает строку."""
    # Синхронная функция — вызывается из _process_photo через asyncio.to_thread
    # (потому и не async). Принимает путь к локальному JPG-файлу.
    reader = _get_ocr_reader()                      # берём общий (один) Reader
    # detail=0 — вернуть только строки текста без координат рамок;
    # paragraph=True — склеивать строки в абзацы (удобнее для чтения).
    # readtext возвращает список строк/абзацев распознанного текста.
    lines = reader.readtext(path, detail=0, paragraph=True)
    return "\n".join(lines)                         # абзацы через перенос строки


# ============================================================
# PDF (текстовый слой)
# ============================================================
# PyPDF2 — лёгкая библиотека для разбора PDF-файлов. Её extract_text()
# работает только с «текстовым слоем»: теми символами, что зашиты в
# документ как текст. Скан PDF (фото страниц) текстового слоя не имеет,
# поэтому для сканов этот инструмент бессилен — в этом случае функция
# вернёт пустую строку, и обработчик честно об этом скажет.

def _pdf_text(path: str) -> str:
    """Извлекает текст из всех страниц PDF."""
    # Синхронная функция, вызывается через asyncio.to_thread из _process_pdf.
    reader = PdfReader(path)         # открываем документ
    parts = []
    # extract_text() использует текстовый слой PDF. Для сканов (картинок
    # вместо текста) он вернёт None/пустоту — так и задумано.
    for page in reader.pages:        # reader.pages — список страниц документа
        text = page.extract_text()   # вытаскиваем текст текущей страницы
        if text:
            parts.append(text)       # пустые страницы пропускаем
    # Склеиваем тексты всех страниц переносами строк — итоговая строка.
    return "\n".join(parts)


# ============================================================
# ВИДЕО (речь и ключевые моменты)
# ============================================================
# Обе функции принимают путь к локальному видео и рабочую папку, куда можно
# класть промежуточные файлы (WAV-дорожку или JPG-кадры). Возвращают данные
# так, чтобы вызывающая сторона сама решила, что делать с результатом.
# Обе — синхронные: вызываются из _process_video через asyncio.to_thread.

def _video_clip_class():
    """Возвращает класс VideoFileClip из moviepy v2 (или v1)."""
    # Напоминание из шапки: версии moviepy хранят VideoFileClip в разных
    # местах, поэтому у нас единая точка получения класса.
    try:
        # moviepy ≥ 2.0: модуль верхнего уровня.
        from moviepy import VideoFileClip
        return VideoFileClip
    except ImportError:
        # moviepy 1.x: класс живёт в подмодуле moviepy.editor.
        # ImportError перехватываем — значит v2 не установлен, берём v1.
        from moviepy.editor import VideoFileClip
        return VideoFileClip


def _extract_speech(video_path: str, workdir: str):
    """
    Извлекает речь из видео через moviepy + Google Speech Recognition.

    Returns:
        Кортеж (текст, ошибка). Если ошибка не None — распознать не вышло.
    """
    # video_path — локальный mp4; workdir — временная папка, куда положим WAV.
    VideoFileClip = _video_clip_class()
    clip = VideoFileClip(video_path)                              # открываем ролик
    audio_path = os.path.join(workdir, "audio.wav")               # файл звуковой дорожки
    try:
        if clip.audio is None:
            return None, "в видео не найден звук"                  # видео без звука
        # Экспортируем звук в WAV: 16 кГц (оптимум для распознавания речи),
        # PCM 16-bit. logger=None отключает прогресс-бар ffmpeg.
        # write_audiofile запускает ffmpeg для конвертации аудио-дорожки.
        clip.audio.write_audiofile(
            audio_path, fps=16000, codec="pcm_s16le", logger=None
        )
    except Exception as e:  # noqa: BLE001
        # ffmpeg не отработал (нет аудио-кодека, файл повреждён и т.п.).
        print(f"MEDIA: audio extract error: {e}")
        return None, "не удалось извлечь звук из видео"
    finally:
        # Закрываем клип (освобождает память и дескрипторы) даже при ошибке.
        # moviepy держит открытый файл и память — без close() они утекут.
        try:
            clip.close()
        except Exception:  # noqa: BLE001
            pass

    # Распознавание речи по готовому WAV-файлу.
    # Recognizer — объект, который умеет «слушать» аудио и звать сервис.
    recognizer = speech_recognition.Recognizer()
    try:
        # record(source) считывает весь трек разом (не большими кусками).
        # AudioFile — обёртка над WAV: открывает файл и отдаёт его данные.
        with speech_recognition.AudioFile(audio_path) as source:
            audio_data = recognizer.record(source)
        # Google Web Speech API, распознаём русскую речь.
        # recognize_google — бесплатный веб-сервис Google (требует интернет).
        text = recognizer.recognize_google(audio_data, language="ru-RU")
        return text, None   # успех: текст + отсутствие ошибки
    except speech_recognition.UnknownValueError:
        # Google получил аудио, но не разобрал речь (нет голоса/тихо).
        return None, "речь так и не распознана (в видео может не быть голоса)"
    except speech_recognition.RequestError as e:
        # Сеть до Google не дошла либо сервис ответил ошибкой.
        return None, f"сервис распознавания речи недоступен: {e}"


def _extract_key_moments(video_path: str, workdir: str):
    """
    Находит ключевые моменты видео по резкой смене кадров.

    Returns:
        Список кортежей (время в секундах, путь к миниатюре-кадру).
    """
    import numpy as np  # numpy нужен только для видео-анализа
    # numpy позволяет быстро считать среднюю разницу между кадрами; импорт
    # локальный — модуль numpy не трогаем, пока не пришло видео на обработку.

    VideoFileClip = _video_clip_class()
    clip = VideoFileClip(video_path)   # открываем ролик для покадрового анализа
    shots = []          # таймкоды, где произошла заметная смена сцены
    prev = None         # предыдущий «серый» кадр (для сравнения)
    # Анализируем не каждый кадр, а ~1 раз в секунду — быстро и достаточно
    # точно для «ключевых моментов». Полный покадровый проход был бы в
    # 20–60 раз медленнее и дал бы почти тот же список смен сцены.
    try:
        # iter_frames с fps=1.0 идёт по кадрам и отдаёт (время, массив пикселей).
        # with_times=True — кортежи (timestamp_float, numpy_массив_кадр).
        for t, frame in clip.iter_frames(fps=1.0, with_times=True):
            if frame.ndim == 3:
                # Цветной кадр (высота, ширина, 3) → усредняем по каналам RGB,
                # получая «серый» кадр (высота, ширина). Так сравниваем не 3
                # канала, а один — проще и быстрее.
                gray = frame.mean(axis=2)
            else:
                gray = frame   # уже чёрно-белый — используем как есть
            if gray.size == 0:
                continue       # пустой кадр пропускаем
            # Кадры могут приходить и в диапазоне 0..1 (float).
            # moviepy отдаёт пиксели либо 0..255 (uint8), либо 0..1 (float)
            # — чтобы разница считалась в одном масштабе, приводим к 0..255.
            if gray.max() <= 1.0:
                gray = gray * 255.0   # нормализуем float-кадр до 0..255
            # Сэмплируем каждый 6-й пиксель по обеим осям — сравнение быстрее,
            # точность всё равно достаточна для детекции смены сцены.
            gray = gray[::6, ::6]
            gray = gray.astype(np.float32)
            # Сравниваем с предыдущим кадром (кроме самого первого — превью None).
            if prev is not None:
                # Средняя абсолютная разница с предыдущим кадром 0..255.
                # np.abs(gray - prev) — матрица модулей разницы по пикселям;
                # .mean() — среднее по всей матрице: одно число-«метрика».
                # Чем оно больше, тем сильнее изменилась картинка между двумя
                # соседними секундами; порог _SCENE_CHANGE_THRESHOLD (=18.0)
                # отделяет реальную смену сцены от шума/мелкого движения.
                diff = float(np.abs(gray - prev).mean())
                if diff > _SCENE_CHANGE_THRESHOLD:
                    shots.append(t)   # резкая смена → запоминаем таймкод
            prev = gray   # текущий кадр становится «предыдущим»
    finally:
        # Всегда закрываем клип — освобождаем файл и память.
        try:
            clip.close()
        except Exception:  # noqa: BLE001
            pass

    # Избавляемся от слишком близких друг к другу смен кадров.
    # (Панорама или «тряска» дают серию всплесков — оставляем только первый.)
    # Условие t - distinct[-1] >= _MIN_SHOT_GAP отсекает «дубль» смены, если
    # она случилась меньше чем за _MIN_SHOT_GAP секунд от предыдущей.
    distinct = []
    for t in shots:
        if not distinct or t - distinct[-1] >= _MIN_SHOT_GAP:
            distinct.append(t)   # таймкод уходит в финальный список

    # По первым _MAX_KEY_FRAMES моментам сохраняем миниатюры-кадры.
    # Срез [:5] ограничивает число моментов (не спамим пользователя).
    key_frames = []
    for i, t in enumerate(distinct[:_MAX_KEY_FRAMES]):
        thumb = os.path.join(workdir, f"frame_{i + 1}.jpg")
        # _save_frame вытаскивает кадр в момент t и сохраняет JPG.
        if _save_frame(video_path, thumb, t):
            key_frames.append((t, thumb))   # в список попадают только спасённые
    return key_frames   # кортежи (секунды, путь к миниатюре)


def _save_frame(video_path: str, thumb_path: str, t: float) -> bool:
    """Сохраняет кадр видео в момент времени t. Возвращает успех."""
    # Синхронная функция; возвращает bool (успех), чтобы вызывающий мог
    # пропустить неудачный кадр, не обрушив весь список моментов.
    VideoFileClip = _video_clip_class()
    clip = VideoFileClip(video_path)
    try:
        # save_frame вытаскивает кадр в момент t и пишет JPG в thumb_path.
        # moviepy декодирует видео до позиции t и рендерит кадр в файл.
        clip.save_frame(thumb_path, t=t)
        return os.path.exists(thumb_path)   # проверяем, что файл реально создан
    except Exception:  # noqa: BLE001
        # Кадр не спасли (повреждённый фрагмент и т.п.) — логируем таймкод.
        print(f"MEDIA: save_frame error at t={t}")
        return False   # сообщаем вызывающему: кадр пропустить
    finally:
        # Закрываем клип всегда — даже при успешном сохранении.
        try:
            clip.close()
        except Exception:  # noqa: BLE001
            pass


# ============================================================
# ПРОМПТЫ ДЛЯ ИИ-ПОСТОБРАБОТКИ
# ============================================================
# Два режима постобработки извлечённого текста. Промпты — это инструкции
# для LLM, поэтому важно, чтобы они точно описывали желаемое поведение:
#   • _FIX_PROMPT  — «исправить ошибки распознавания» (media_ai_fix);
#   • _SOLVE_PROMPT — «решить задание / объяснить материал» (media_ai_solve).
# Оба промпта отдаются первым аргументом в _ask_ai (как «системная» роль),
# а сам извлечённый текст — вторым (как сообщение пользователя).
# Промпты написаны на русском — модель в LM Studio настроена отвечать на том
# же языке, на котором с ней общаются.

_FIX_PROMPT = (
    "Ты — редактор школьного помощника. Перед тобой текст, извлечённый "
    "распознаванием (с фото, из PDF или из речи). В нём могут быть ошибки "
    "распознавания, обрезанные символы и мусорные фрагменты. Исправь текст "
    "так, чтобы его было удобно читать: верни структуру, поправь орфографию "
    "и пунктуацию, убери артефакты распознавания. Сохрани СМЫСЛ исходного "
    "текста и его содержание — не решай задания от себя. "
    "Отвечай ТОЛЬКО исправленным текстом, без пояснений."
)

_SOLVE_PROMPT = (
    "Ты — помощник школьника. Перед тобой текст задания или учебного "
    "материала. Реши задание (если это задача) и объясни решение простыми "
    "словами по шагам, или кратко объясни суть материала, если это текст. "
    "Отвечай по-русски, подробно и понятно."
)