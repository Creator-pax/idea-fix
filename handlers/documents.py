# ============================================================
# ОБРАБОТЧИК ДОКУМЕНТОВ И КОНСТРУКТОРА СЛАЙДОВ — handlers/documents.py
# ============================================================
# Этот файл отвечает за ДВЕ большие группы функций:
#
# 1. РЕДАКТИРОВАНИЕ ДОКУМЕНТОВ ЧЕРЕЗ ИИ
#    Пользователь присылает файл (.docx / .rtf / .pdf / .pptx) →
#    бот извлекает из него текст → пользователь пишет инструкцию
#    «что исправить» → ИИ редактирует текст → бот собирает документ
#    заново в ТОМ ЖЕ формате и отправляет обратно.
#
# 2. МИНИ-КОНСТРУКТОР СЛАЙДОВ (презентаций)
#    Пошаговый мастер: заголовок → текст → число слайдов → стиль →
#    бот собирает готовый .pptx с градиентным фоном и шрифтами
#    (использует те же Pillow-градиенты и python-pptx, что и export.py).
#
# Технические детали:
#   • FSM-состояния (DocStates) определены в handlers/states.py;
#   • Все синхронные вызовы (извлечение текста, сборка файлов, ИИ)
#     выполняются через asyncio.to_thread, чтобы не блокировать
#     event loop бота;
#   • Каждый обработчик проверяет «входные ворота»: user_exists,
#     is_approved, is_blocked — единый паттерн для всего бота;
#   • Временные файлы создаются в tempfile и удаляются в finally;
#   • Сборка документов переиспользует логику из handlers/export.py
#     (шрифты, фоны, подпись «Команда КУБ-СКРИПТ»).
#
# Используемые библиотеки (все уже в requirements.txt):
#   • python-pptx — чтение/сборка PowerPoint-файлов;
#   • python-docx — чтение/сборка Word-файлов (.docx);
#   • PyPDF2     — чтение PDF-файлов (текстовый слой);
#   • Pillow     — градиентные фоны для слайдов и превью;
#   • requests   — вызов LM Studio для редактирования текста.
# ============================================================

# --- Стандартная библиотека ---
import asyncio     # asyncio.to_thread — запуск тяжёлых задач в потоках
import io          # BytesIO — байты в памяти для превью
import os          # работа с путями, расширения файлов, удаление temp
import shutil      # rmtree — удаление временных папок
import tempfile    # временные файлы для скачивания и сборки
import time        # уникальные имена файлов

# --- maxo: фреймворк бота ---
from maxo import Router
from maxo.routing.filters import StateFilter
from maxo.types import MessageCallback, MessageCreated
from maxo.utils.builders import KeyboardBuilder
from maxo.utils.upload_media import BufferedInputFile

# --- проект ---
import database as db
from config import AI_MAX_INPUT_LENGTH

from handlers.ai import _ask_ai
from handlers.export import (
    _BACKGROUNDS,
    _build_pptx,
    _build_docx,
    _build_rtf,
    _build_pdf,
    _font_helper,
    _palette,
    _make_bg_image,
)
from handlers.filters import CallbackPayload
from handlers.states import DocStates
from handlers.user_utils import attachment_filename, attachment_url, get_user_id

# Роутер — контейнер обработчиков этого модуля; подключается в handlers/__init__.py
router: Router = Router(name="documents")

# ============================================================
# КОНСТАНТЫ РАЗДЕЛА
# ============================================================

# Подпись авторов — добавляется в каждый документ (как и в export.py).
AUTHORS_SIGNATURE = "Команда «КУБ-СКРИПТ»"

# Расширения файлов, которые бот умеет читать для редактирования.
# Формат: {расширение: (название_формата,True — читается_text)}
_SUPPORTED_DOCS = {
    ".docx": ("Word (.docx)", True),
    ".pdf":  ("PDF", True),
    ".pptx": ("PowerPoint (.pptx)", True),
    ".rtf":  ("RTF", False),  # RTF читается по-особому (нет библиотеки)
}

# Максимальный размер файла для редактирования (20 МБ).
_DOC_MAX_SIZE = 20 * 1024 * 1024

# Папка для сохранения сгенерированных презентаций (рядом с проектом).
_SLIDES_OUTPUT_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "generated_slides",
)

# UI-стили конструктора слайдов (то, что видит пользователь) <-> ключи
# фонов из export.py (_BACKGROUNDS). Нельзя путать: в export.py ключ
# «голубого» — это "lightblue", а не "blue". Здесь — единственное место,
# где плашка «blue» превращается в валидный ключ фона.
_SLIDE_STYLES = {
    "white": "Белый",
    "cream": "Кремовый",
    "blue": "Голубой",
    "dark": "Тёмный",
    "pink": "Розовый",
    "green": "Зелёный",
    "purple": "Фиолетовый",
    "orange": "Оранжевый",
    "red": "Красный",
    "indigo": "Индиго",
}

# Перевод UI-имени стиля в ключ _BACKGROUNDS (см. export.py).
_SLIDE_BG_KEYS = {
    "white": "white",
    "cream": "cream",
    "blue": "lightblue",
    "dark": "dark",
    "pink": "pink",
    "green": "green",
    "purple": "purple",
    "orange": "orange",
    "red": "red",
    "indigo": "indigo",
}

# ============================================================
# КЛАВИАТУРЫ РАЗДЕЛА «ДОКУМЕНТЫ И СЛАЙДЫ»
# ============================================================

def _doc_menu_keyboard(user_id: int) -> KeyboardBuilder:
    """Клавиатура главного меню раздела: два основных действия."""
    kb = KeyboardBuilder()
    kb.add_callback(text="🛠 Конструктор слайдов", payload="doc_slides")
    kb.add_callback(text="📄 Редактировать документ", payload="doc_edit")
    kb.add_callback(text="← Назад в меню", payload="menu_main")
    return kb.adjust(1)


def _doc_cancel_keyboard() -> KeyboardBuilder:
    """Кнопка «Отмена» — прерывает текущий процесс."""
    kb = KeyboardBuilder()
    kb.add_callback(text="❌ Отмена", payload="doc_menu")
    return kb.adjust(1)


def _doc_back_keyboard() -> KeyboardBuilder:
    """Кнопка «Назад в меню документов»."""
    kb = KeyboardBuilder()
    kb.add_callback(text="← Назад", payload="doc_menu")
    return kb.adjust(1)


def _slides_count_keyboard() -> KeyboardBuilder:
    """Кнопки шага 3 конструктора: число слайдов (или «авто»)."""
    kb = KeyboardBuilder()
    kb.add_callback(text="🖼 Автоматически (ИИ)", payload="doc_slides_count_auto")
    kb.add_callback(text="🖼 2 слайда", payload="doc_slides_count_2")
    kb.add_callback(text="🖼 3 слайда", payload="doc_slides_count_3")
    kb.add_callback(text="🖼 4 слайда", payload="doc_slides_count_4")
    kb.add_callback(text="🖼 5 слайдов", payload="doc_slides_count_5")
    kb.add_callback(text="🖼 6 слайдов", payload="doc_slides_count_6")
    kb.add_callback(text="🖼 7 слайдов", payload="doc_slides_count_7")
    kb.add_callback(text="🖼 8 слайдов", payload="doc_slides_count_8")
    kb.add_callback(text="🖼 9 слайдов", payload="doc_slides_count_9")
    kb.add_callback(text="🖼 10 слайдов", payload="doc_slides_count_10")
    kb.add_callback(text="🖼 15 слайдов", payload="doc_slides_count_15")
    kb.add_callback(text="🖼 20 слайдов", payload="doc_slides_count_20")
    kb.add_callback(text="❌ Отмена", payload="doc_menu")
    return kb.adjust(1)


def _slides_style_keyboard() -> KeyboardBuilder:
    """Кнопки шага 4 конструктора: выбор фона слайдов (10 стилей)."""
    kb = KeyboardBuilder()
    kb.add_callback(text="⚪ Белый", payload="doc_slides_style_white")
    kb.add_callback(text="🥛 Кремовый", payload="doc_slides_style_cream")
    kb.add_callback(text="💧 Голубой", payload="doc_slides_style_blue")
    kb.add_callback(text="🔵 Индиго", payload="doc_slides_style_indigo")
    kb.add_callback(text="🟣 Фиолетовый", payload="doc_slides_style_purple")
    kb.add_callback(text="🌸 Розовый", payload="doc_slides_style_pink")
    kb.add_callback(text="🔴 Красный", payload="doc_slides_style_red")
    kb.add_callback(text="🟠 Оранжевый", payload="doc_slides_style_orange")
    kb.add_callback(text="🌲 Зелёный", payload="doc_slides_style_green")
    kb.add_callback(text="🌙 Тёмный", payload="doc_slides_style_dark")
    kb.add_callback(text="❌ Отмена", payload="doc_menu")
    return kb.adjust(1)


def _slides_build_keyboard() -> KeyboardBuilder:
    """Кнопки после выбора стиля: превью / сборка / смена стиля."""
    kb = KeyboardBuilder()
    kb.add_callback(text="👁 Предпросмотр", payload="doc_slides_preview")
    kb.add_callback(text="📦 Собрать презентацию", payload="doc_slides_build")
    kb.add_callback(text="🛠 Сменить стиль", payload="doc_slides_style_menu")
    kb.add_callback(text="❌ Отмена", payload="doc_menu")
    return kb.adjust(1)


# ============================================================
# ГЛАВНОЕ МЕНЮ РАЗДЕЛА «ДОКУМЕНТЫ И СЛАЙДЫ»
# ============================================================

@router.message_callback(
    CallbackPayload(
        "doc_menu", "doc_slides", "doc_edit",
        "doc_slides_ai_text", "doc_slides_style_menu",
        "doc_slides_build", "doc_slides_preview",
        "doc_edit_confirm", "doc_edit_retry",
        prefixes=("doc_slides_count_", "doc_slides_style_"),
    )
)
async def handle_doc_menu(update: MessageCallback, fsm_context) -> None:
    """
    Обрабатывает кнопки раздела «Документы и слайды».

    Раздел работает в двух режимах:
      • doc_menu — показываем главное меню раздела (мини-конструктор слайдов
        и редактирование документов);
      • doc_slides — запускаем пошаговый мастер конструктора слайдов;
      • doc_edit — переводим в режим ожидания файла документа.

    Все payload'ы начинаются с «doc_» — они перехватываются ОДНИМ обработчиком
    (см. CallbackPayload в декораторе).
    """
    payload = update.payload
    user_id = get_user_id(update)

    # Единый барьер доступа: пользователь существует, одобрен модератором
    # и не заблокирован. Без этого — молча выходим.
    if not db.user_exists(user_id) or not db.is_approved(user_id) or db.is_blocked(user_id):
        return

    # ============================================================
    # ГЛАВНОЕ МЕНЮ РАЗДЕЛА
    # ============================================================
    if payload == "doc_menu":
        # Сбрасываем любое незавершённое состояние FSM.
        # clear() удаляет и состояние, и временные данные — чистый старт.
        await fsm_context.clear()
        await update.callback_answer(text="Документы")
        await update.edit_message(
            "📂 Документы и слайды\n\n"
            "Здесь ты можешь:\n"
            "• Создать презентацию (мини-конструктор слайдов)\n"
            "• Отредактировать документ через ИИ "
            "(присылай .docx/.pdf/.pptx/.rtf)\n\n"
            "Что хочешь сделать?",
            keyboard=_doc_menu_keyboard(user_id).build(),
        )
        return

    # ============================================================
    # МИНИ-КОНСТРУКТОР СЛАЙДОВ: ШАГ 1 — ЗАГОЛОВОК
    # ============================================================
    if payload == "doc_slides":
        # Запускаем мастер: сбрасываем FSM и переводим в состояние
        # «ждём заголовок презентации».
        await fsm_context.clear()
        await fsm_context.set_state(DocStates.wait_slide_title)
        await update.callback_answer(text="Конструктор")
        kb = _doc_cancel_keyboard()
        await update.edit_message(
            "🛠 Конструктор слайдов\n\n"
            "Шаг 1: Напиши заголовок презентации.\n"
            "Это название, которое появится на первом слайде.",
            keyboard=kb.build(),
        )
        return

    # ============================================================
    # РЕДАКТИРОВАНИЕ ДОКУМЕНТА: ОЖИДАНИЕ ФАЙЛА
    # ============================================================
    if payload == "doc_edit":
        # Переводим в состояние «ждём файл документа».
        # Пользователь должен прислать .docx / .pdf / .pptx / .rtf файл.
        await fsm_context.clear()
        await fsm_context.set_state(DocStates.wait_doc_file)
        await update.callback_answer(text="Документ")
        kb = _doc_cancel_keyboard()
        await update.edit_message(
            "📄 Редактирование документа через ИИ\n\n"
            "Пришли файл в одном из форматов:\n"
            "• .docx (Word)\n"
            "• .pdf (PDF)\n"
            "• .pptx (PowerPoint)\n"
            "• .rtf\n\n"
            "Я извлеку текст, ты напишешь, что исправить, "
            "а я отредактирую и пришлю обратно!",
            keyboard=kb.build(),
        )
        return

    # ============================================================
    # МИНИ-КОНСТРУКТОР СЛАЙДОВ: ШАГ 2b — АВТОМАТИЧЕСКИЙ ТЕКСТ ОТ ИИ
    # ============================================================
    # Пользователь нажал «🤖 Автоматический (ИИ)» на шаге 2 (заголовок
    # уже сохранён). Генерируем текст презентации, потом переходим к шагу 3.
    if payload == "doc_slides_ai_text":
        data = await fsm_context.get_data()
        title = data.get("slides_title", "Презентация")
        await update.callback_answer(text="Думаю")
        await update.edit_message("🤔 ИИ создаёт текст для слайдов...")
        try:
            ai_text, _ = await asyncio.wait_for(
                asyncio.to_thread(
                    _ask_ai,
                    "Ты — помощник школьника. Напиши краткий текст для "
                    f"презентации на тему «{title}». "
                    "Разбей на 4-6 логичных абзацев/разделов. "
                    "Каждый раздел начинай с «### ». "
                    "Отвечай ТОЛЬКО текстом, без пояснений.",
                    f"Тема: {title}",
                ),
                timeout=25,
            )
        except (asyncio.TimeoutError, Exception) as e:
            print(f"DOCS: AI slide text error: {e}")
            ai_text = None

        if not (ai_text or "").strip():
            # ИИ не ответил — просим ввести текст вручную (возврат к шагу 2).
            await update.edit_message(
                "🤔 ИИ не смог сгенерировать текст.\n"
                "Напиши основной текст для слайдов (абзацы через пустую строку):",
                keyboard=_doc_cancel_keyboard().build(),
            )
            await fsm_context.set_state(DocStates.wait_slide_text)
            return

        # Текст готов — сохраняем и переходим к шагу 3 (число слайдов).
        await fsm_context.update_data(slides_text=ai_text)
        await fsm_context.set_state(DocStates.wait_slide_count)
        await update.answer_text(
            "✅ Текст сгенерирован ИИ!\n\n"
            "Шаг 3: На сколько слайдов разбить презентацию?",
            keyboard=_slides_count_keyboard().build(),
        )
        return

    # ============================================================
    # МИНИ-КОНСТРУКТОР СЛАЙДОВ: ШАГ 3b — ЧИСЛО СЛАЙДОВ (КНОПКИ)
    # ============================================================
    # Кнопки «2 слайда / 3 слайда / ... / Автоматически» на шаге 3.
    if payload.startswith("doc_slides_count_"):
        count_value = payload[len("doc_slides_count_"):]
        count = "auto" if count_value == "auto" else count_value
        # Выходим из message-состояния «ждём число», чтобы дальнейший текст
        # юзера не воспринимался как количество. Данные FSM сохраняются.
        await fsm_context.set_state(None)
        await fsm_context.update_data(slides_count=count)
        await update.callback_answer(text="Стиль")
        await update.edit_message(
            f"✅ Слайдов: {'автоматически' if count == 'auto' else count}\n\n"
            "Шаг 4: Какой фон использовать для слайдов?",
            keyboard=_slides_style_keyboard().build(),
        )
        return

    # ============================================================
    # СТИЛЬ СЛАЙДОВ: ПОВТОРНЫЙ ВЫБОР ОФОРМЛЕНИЯ
    # ============================================================
    # Кнопка «🛠 Сменить стиль» на экране сборки — снова показываем выбор
    # фона. Текст и число слайдов остаются во FSM.
    if payload == "doc_slides_style_menu":
        await fsm_context.set_state(None)
        await update.callback_answer(text="Стиль")
        await update.edit_message(
            "Шаг 4: Какой фон использовать для слайдов?",
            keyboard=_slides_style_keyboard().build(),
        )
        return

    # ============================================================
    # СТИЛЬ СЛАЙДОВ: ВЫБОР ФОНА (ШАГ 4)
    # ============================================================
    if payload.startswith("doc_slides_style_"):
        style = payload[len("doc_slides_style_"):]
        # Сохраняем выбор стиля в FSM (уже как валидный ключ фона export.py).
        bg_key = _SLIDE_BG_KEYS.get(style, "white")
        await fsm_context.set_state(None)
        await fsm_context.update_data(slides_bg=bg_key)
        data = await fsm_context.get_data()
        title = data.get("slides_title", "Презентация")
        text = data.get("slides_text", "")

        # Если текста нет (данные утеряны) — запрашиваем заново (шаг 2).
        if not text.strip():
            await update.edit_message(
                "🤔 Текст для слайдов не заполнен.\n"
                "Напиши основной текст (абзацы через пустую строку):",
                keyboard=_doc_cancel_keyboard().build(),
            )
            await fsm_context.set_state(DocStates.wait_slide_text)
            return

        # Стиль выбран — предлагаем превью / сборку / смену стиля.
        style_label = _SLIDE_STYLES.get(style, style)
        await update.callback_answer(text="Готово")
        await update.edit_message(
            f"🛠 Конструктор слайдов\n\n"
            f"Заголовок: {title}\n"
            f"Фон: {style_label}\n\n"
            "Что дальше?",
            keyboard=_slides_build_keyboard().build(),
        )
        return

    # ============================================================
    # СБОРКА И ОТПРАВКА ПРЕЗЕНТАЦИИ
    # ============================================================
    if payload == "doc_slides_build":
        data = await fsm_context.get_data()
        title = data.get("slides_title", "Презентация")
        text = data.get("slides_text", "")
        count = data.get("slides_count", "auto")
        bg = data.get("slides_bg", "white")

        if not text.strip():
            await update.callback_answer(text="Нет текста")
            await update.edit_message(
                "❌ Текст для слайдов пуст.\n"
                "Начни заново: «Документы → Конструктор слайдов».",
                keyboard=_doc_menu_keyboard(user_id).build(),
            )
            return

        await update.callback_answer(text="Собираю")
        await update.edit_message("⚙️ Собираю презентацию...")

        # Сборка в отдельном потоке — python-pptx + Pillow тяжёлые.
        try:
            slide_count = None if count == "auto" else (int(count) if str(count).isdigit() else None)
            path, filename = await asyncio.to_thread(
                _build_doc_slides, title, text, bg, slide_count
            )
        except Exception as e:
            print(f"DOCS: slides build error: {e}")
            await update.edit_message(
                "❌ Не удалось создать презентацию.\nПопробуй ещё раз.",
                keyboard=_doc_menu_keyboard(user_id).build(),
            )
            return

        # Читаем байты файла для отправки.
        try:
            with open(path, "rb") as fh:
                file_data = fh.read()
        except OSError:
            file_data = None
        try:
            os.remove(path)
        except OSError:
            pass

        if file_data is None:
            return

        await update.send_media(
            media=BufferedInputFile.file(file_data, filename),
            text="📎 Презентация готова!",
            keyboard=_doc_menu_keyboard(user_id).build(),
        )
        # Очищаем FSM — следующий вызов начнётся заново.
        await fsm_context.clear()
        return

    # ============================================================
    # ПРЕДПРОСМОТР СЛАЙДОВ
    # ============================================================
    if payload == "doc_slides_preview":
        data = await fsm_context.get_data()
        title = data.get("slides_title", "Презентация")
        text = data.get("slides_text", "")
        bg = data.get("slides_bg", "white")

        await update.callback_answer(text="Превью")
        await update.edit_message("🖼 Готовлю превью...")

        count = data.get("slides_count", "auto")
        slide_count = None if count == "auto" else (
            int(count) if str(count).isdigit() else None
        )
        try:
            images = await asyncio.to_thread(
                _render_doc_preview, title, text, bg, slide_count
            )
        except Exception as e:
            print(f"DOCS: preview error: {e}")
            images = []

        if not images:
            await update.edit_message("❌ Не удалось создать превью.")
            kb = KeyboardBuilder()
            kb.add_callback(text="📦 Собрать без превью", payload="doc_slides_build")
            kb.add_callback(text="❌ Отмена", payload="doc_menu")
            await update.answer_text(
                "Можно сразу собрать файл:", keyboard=kb.adjust(1).build()
            )
            return

        # Отправляем превью как картинки.
        media_inputs = []
        for num, image in enumerate(images, 1):
            buf = io.BytesIO()
            image.save(buf, "PNG")
            media_inputs.append(
                BufferedInputFile.image(buf.getvalue(), f"slide_preview_{num}.png")
            )

        if len(media_inputs) > 1:
            await update.send_media(media=media_inputs, text="👁 Превью слайдов:")
        else:
            await update.send_media(media=media_inputs[0], text="👁 Превью слайдов:")

        kb = KeyboardBuilder()
        kb.add_callback(text="📦 Собрать презентацию", payload="doc_slides_build")
        kb.add_callback(text="🛠 Изменить стиль", payload="doc_slides")  # перезапуск мастера
        kb.add_callback(text="❌ Отмена", payload="doc_menu")
        await update.answer_text(
            "Нравится? Собери файл!", keyboard=kb.adjust(1).build()
        )
        return

    # ============================================================
    # ПОДТВЕРЖДЕНИЕ/ПОВТОР РЕДАКТИРОВАНИЯ ДОКУМЕНТА
    # ============================================================
    if payload == "doc_edit_confirm":
        data = await fsm_context.get_data()
        doc_text = data.get("doc_text", "")
        instruction = data.get("edit_instruction", "")
        doc_format = data.get("doc_format", "docx")

        if not doc_text.strip() or not instruction.strip():
            await update.callback_answer(text="Пусто")
            await update.edit_message("❌ Данные утеряны. Прислай файл заново.",
                                      keyboard=_doc_menu_keyboard(user_id).build())
            return

        await update.callback_answer(text="ИИ")
        await update.edit_message("🤔 ИИ редактирует документ...")

        # Отправляем текст документа + инструкцию пользователя в ИИ.
        prompt = (
            "Ты — редактор документов. Пользователь прислал текст из документа "
            "и хочет, чтобы ты внёс правки. "
            "Инструкция пользователя:\n\n"
            f"{instruction}\n\n"
            "Вот исходный текст документа:\n\n"
            f"{doc_text[:AI_MAX_INPUT_LENGTH]}\n\n"
            "Верни ОТРЕДАКТИРОВАННЫЙ текст целиком (весь документ, "
            "только с нужными правками). Не добавляй комментариев, "
            "только исправленный текст."
        )

        try:
            edited_text, err = await asyncio.wait_for(
                asyncio.to_thread(_ask_ai, prompt, "Отредактируй документ."),
                timeout=60,
            )
        except asyncio.TimeoutError:
            edited_text = None
            err = "таймаут ИИ"

        if not edited_text:
            try:
                db.save_ai_error(user_id, "doc_edit", err or "Пустой ответ ИИ")
            except Exception:
                pass
            await update.edit_message(
                f"❌ ИИ не смог отредактировать.\nПричина: {err or 'неизвестная'}",
                keyboard=_doc_menu_keyboard(user_id).build(),
            )
            return

        # Сохраняем отредактированный текст.
        await fsm_context.update_data(edited_text=edited_text)

        # Собираем документ заново и отправляем.
        await update.edit_message("⚙️ Собираю отредактированный документ...")

        try:
            path, filename = await asyncio.to_thread(
                _rebuild_doc, doc_format, edited_text, doc_text,
                data.get("doc_title", ""), user_id,
            )
        except Exception as e:
            print(f"DOCS: rebuild error: {e}")
            await update.edit_message(
                "❌ Не удалось собрать документ. Попробуй снова.",
                keyboard=_doc_menu_keyboard(user_id).build(),
            )
            return

        try:
            with open(path, "rb") as fh:
                file_data = fh.read()
        except OSError:
            file_data = None
        try:
            os.remove(path)
        except OSError:
            pass

        if file_data is None:
            return

        kb = KeyboardBuilder()
        kb.add_callback(text="📄 Отредактировать ещё", payload="doc_edit")
        kb.add_callback(text="← Меню документов", payload="doc_menu")
        await update.send_media(
            media=BufferedInputFile.file(file_data, filename),
            text="✅ Документ отредактирован!",
            keyboard=kb.adjust(1).build(),
        )
        await fsm_context.clear()
        return

    if payload == "doc_edit_retry":
        # Пользователь хочет задать новую инструкцию для того же документа.
        await fsm_context.set_state(DocStates.wait_edit_instruction)
        await update.callback_answer(text="Инструкция")
        kb = _doc_cancel_keyboard()
        await update.edit_message(
            "✍️ Напиши новую инструкцию: что исправить/дополнить в документе?",
            keyboard=kb.build(),
        )
        return


# ============================================================
# ОБРАБОТЧИКИ FSM-СОСТОЯНИЙ: ПРИЁМ ТЕКСТА И ФАЙЛОВ
# ============================================================
# Ниже — обработчики сообщений, которые «ловят» текст и файлы,
# когда пользователь находится в одном из состояний DocStates.
# Каждый StateFilter привязан к конкретному шагу мастера.

# ─── ШАГ 1: ЗАГОЛОВОК ПРЕЗЕНТАЦИИ ───
@router.message_created(StateFilter(DocStates.wait_slide_title))
async def handle_slide_title(update: MessageCreated, fsm_context) -> None:
    """
    Принимает заголовок презентации от пользователя.

    Ожидает текстовое сообщение с заголовком (до 100 символов).
    После получения переходит к следующему шагу — ввод текста.
    """
    user_id = get_user_id(update)
    if not user_id:
        return
    if not db.user_exists(user_id) or not db.is_approved(user_id) or db.is_blocked(user_id):
        return

    title = (update.text or "").strip()
    if not title:
        kb = _doc_cancel_keyboard()
        await update.answer_text("❌ Напиши заголовок текстом.", keyboard=kb.build())
        return

    if len(title) > 100:
        await update.answer_text(
            f"❌ Заголовок слишком длинный ({len(title)} символов). "
            "Сократи до 100 символов."
        )
        return

    # Сохраняем заголовок и переходим к шагу 2 (текст).
    await fsm_context.update_data(slides_title=title)
    await fsm_context.set_state(DocStates.wait_slide_text)
    kb = KeyboardBuilder()
    kb.add_callback(text="🤖 Автоматический (ИИ)", payload="doc_slides_ai_text")
    kb.add_callback(text="❌ Отмена", payload="doc_menu")
    await update.answer_text(
        f"✅ Заголовок: «{title}»\n\n"
        "Шаг 2: Напиши основной текст для слайдов.\n"
        "Абзацы разделяй пустой строкой.\n\n"
        "Или нажми «🤖 Автоматический» — ИИ сгенерит текст по теме.",
        keyboard=kb.adjust(1).build(),
    )


# ─── ШАГ 2: ТЕКСТ ДЛЯ СЛАЙДОВ ───
@router.message_created(StateFilter(DocStates.wait_slide_text))
async def handle_slide_text(update: MessageCreated, fsm_context) -> None:
    """
    Принимает текст презентации (или запускает генерацию ИИ).

    Пользователь пишет абзацы для слайдов. Если текст пустой —
    следующий шаг не начнётся (защита от случайных сообщений).
    """
    user_id = get_user_id(update)
    if not user_id:
        return
    if not db.user_exists(user_id) or not db.is_approved(user_id) or db.is_blocked(user_id):
        return

    text = (update.text or "").strip()
    if not text:
        kb = _doc_cancel_keyboard()
        await update.answer_text("❌ Напиши текст для слайдов.", keyboard=kb.build())
        return

    await fsm_context.update_data(slides_text=text)
    await fsm_context.set_state(DocStates.wait_slide_count)

    kb = _slides_count_keyboard()
    await update.answer_text(
        "✅ Текст принят!\n\n"
        "Шаг 3: На сколько слайдов разбить презентацию?\n"
        "Напиши число (1-20) или выбери кнопкой.",
        keyboard=kb.build(),
    )


# ─── ШАГ 3: ЧИСЛО СЛАЙДОВ ───
@router.message_created(StateFilter(DocStates.wait_slide_count))
async def handle_slide_count(update: MessageCreated, fsm_context) -> None:
    """
    Принимает число слайдов (1-10) или «авто» для автоматического разбиения.
    """
    user_id = get_user_id(update)
    if not user_id:
        return
    if not db.user_exists(user_id) or not db.is_approved(user_id) or db.is_blocked(user_id):
        return

    text = (update.text or "").strip().lower()
    if text in ("авто", "auto", "автоматически", "а"):
        count = "auto"
    else:
        try:
            count_num = int(text)
            if count_num < 1 or count_num > 20:
                await update.answer_text("❌ Число слайдов: от 1 до 20.")
                return
            count = str(count_num)
        except ValueError:
            await update.answer_text(
                "❌ Введи число (1-20) или «авто»."
            )
            return

    await fsm_context.update_data(slides_count=count)

    # Переходим к выбору стиля фона (через callback-кнопки).
    await update.answer_text(
        f"✅ Слайдов: {'автоматически' if count == 'auto' else count}\n\n"
        "Шаг 4: Какой фон использовать для слайдов?",
        keyboard=_slides_style_keyboard().build(),
    )


# ─── ПРИЁМ ФАЙЛА ДОКУМЕНТА ДЛЯ РЕДАКТИРОВАНИЯ ───
@router.message_created(StateFilter(DocStates.wait_doc_file))
async def handle_doc_file(update: MessageCreated, fsm_context) -> None:
    """
    Принимает файл документа (.docx / .pdf / .pptx / .rtf) для редактирования.

    Из присланного файла извлекается текст. Если формат не поддерживается —
    сообщаем об этом. После успешного извлечения просим инструкцию.
    """
    user_id = get_user_id(update)
    if not user_id:
        return
    if not db.user_exists(user_id) or not db.is_approved(user_id) or db.is_blocked(user_id):
        return

    body = update.message.body
    file = body.file

    if file is None:
        kb = _doc_cancel_keyboard()
        await update.answer_text(
            "❌ Пришли именно файл документа (.docx, .pdf, .pptx, .rtf).",
            keyboard=kb.build(),
        )
        return

    # Проверяем расширение файла.
    filename = attachment_filename(file).lower()
    ext = os.path.splitext(filename)[1]

    if ext not in _SUPPORTED_DOCS:
        supported = ", ".join(f"*{e}*" for e in _SUPPORTED_DOCS)
        kb = _doc_cancel_keyboard()
        await update.answer_text(
            f"❌ Формат «{ext}» не поддерживается.\n"
            f"Поддерживаемые форматы: {supported}.",
            keyboard=kb.build(),
        )
        return

    # Проверяем размер файла.
    if file.size and file.size > _DOC_MAX_SIZE:
        await update.answer_text(
            f"❌ Файл слишком большой ({file.size // (1024*1024)} МБ). "
            "Максимум — 20 МБ."
        )
        return

    await update.answer_text("📄 Обрабатываю документ...")

    # Скачиваем файл и извлекаем текст — всё в отдельном потоке.
    tmp = tempfile.mkdtemp(prefix="maxo_doc_")
    try:
        # Безопасное имя файла (защита от path traversal).
        safe_name = "".join(c for c in filename if c.isalnum() or c in "._-") or "doc"
        path = os.path.join(tmp, safe_name)
        await _download_file(attachment_url(file), path)
        extracted_text = await asyncio.to_thread(_extract_doc_text, path, ext)
    except Exception as e:
        print(f"DOCS: file extraction error: {e}")
        try:
            db.save_ai_error(user_id, "doc_extract", str(e))
        except Exception:
            pass
        await update.answer_text(
            "❌ Не удалось извлечь текст из файла.\n"
            "Возможно, файл повреждён или защищён паролем.",
            keyboard=_doc_menu_keyboard(user_id).build(),
        )
        return
    finally:
        _rmtree_safe(tmp)

    if not extracted_text.strip():
        kb = _doc_cancel_keyboard()
        await update.answer_text(
            "😔 Текст из файла не извлечён.\n"
            "Возможно, это скан (PDF-фото) или документ без текстового слоя.",
            keyboard=kb.build(),
        )
        return

    # Сохраняем извлечённый текст и формат в FSM.
    await fsm_context.update_data(
        doc_text=extracted_text,
        doc_format=ext,
        doc_title=filename,
    )

    # Показываем превью текста (первые 500 символов) и просим инструкцию.
    preview = extracted_text[:500]
    if len(extracted_text) > 500:
        preview += "\n..."

    await fsm_context.set_state(DocStates.wait_edit_instruction)
    kb = _doc_cancel_keyboard()
    await update.answer_text(
        f"📄 Извлечённый текст ({len(extracted_text)} символов):\n\n"
        f"{preview}\n\n"
        "✍️ Напиши инструкцию: что исправить / дополнить в документе?",
        keyboard=kb.build(),
    )


# ─── ИНСТРУКЦИЯ ДЛЯ РЕДАКТИРОВАНИЯ ───
@router.message_created(StateFilter(DocStates.wait_edit_instruction))
async def handle_edit_instruction(update: MessageCreated, fsm_context) -> None:
    """
    Принимает инструкцию редактирования от пользователя.

    После получения инструкции показываем подтверждение:
    «Отредактировать документ с этой инструкцией?».
    """
    user_id = get_user_id(update)
    if not user_id:
        return
    if not db.user_exists(user_id) or not db.is_approved(user_id) or db.is_blocked(user_id):
        return

    instruction = (update.text or "").strip()
    if not instruction:
        kb = _doc_cancel_keyboard()
        await update.answer_text(
            "❌ Напиши, что исправить или дополнить в документе.",
            keyboard=kb.build(),
        )
        return

    data = await fsm_context.get_data()
    doc_text = data.get("doc_text", "")
    doc_format = data.get("doc_format", "docx")

    format_name = _SUPPORTED_DOCS.get(doc_format, ("Документ",))[0]

    await fsm_context.update_data(edit_instruction=instruction)

    # Показываем подтверждение.
    kb = KeyboardBuilder()
    kb.add_callback(text="✅ Отредактировать", payload="doc_edit_confirm")
    kb.add_callback(text="🔄 Другая инструкция", payload="doc_edit_retry")
    kb.add_callback(text="❌ Отмена", payload="doc_menu")
    await update.answer_text(
        f"📄 Формат: {format_name}\n"
        f"📝 Длина текста: {len(doc_text)} символов\n\n"
        f"Инструкция: {instruction}\n\n"
        "Отправить документ ИИ на редактирование?",
        keyboard=kb.adjust(1).build(),
    )


# ============================================================
# ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ
# ============================================================

async def _download_file(url: str, destination: str) -> None:
    """Скачивает файл по URL через bot.download."""
    from bot_instance import bot
    await bot.download(url, destination=destination)


def _rmtree_safe(path: str) -> None:
    """Безопасно удаляет временную папку (игнорируя ошибки)."""
    try:
        if path and os.path.isdir(path):
            shutil.rmtree(path, ignore_errors=True)
    except Exception:
        pass


def _extract_doc_text(path: str, ext: str) -> str:
    """
    Извлекает текст из файла нужного формата.

    Поддерживаемые форматы:
      • .docx — через python-docx (считываем абзацы);
      • .pdf  — через PyPDF2 (текстовый слой);
      • .pptx — через python-pptx (текст со всех слайдов);
      • .rtf  — «грубое» чтение (извлекаем только Unicode-литералы \\uN?).

    Если библиотека не установлена или формат неподдерживаем —
    возвращается пустая строка.
    """
    if ext == ".docx":
        return _extract_docx_text(path)
    elif ext == ".pdf":
        return _extract_pdf_text(path)
    elif ext == ".pptx":
        return _extract_pptx_text(path)
    elif ext == ".rtf":
        return _extract_rtf_text(path)
    return ""


def _extract_docx_text(path: str) -> str:
    """
    Извлекает текст из Word-документа (.docx) через python-docx.

    Считываем каждый абзац (paragraph.text) и склеиваем через
    перенос строки. Пустые абзацы пропускаем.
    """
    try:
        from docx import Document
        doc = Document(path)
        parts = []
        for para in doc.paragraphs:
            text = para.text.strip()
            if text:
                parts.append(text)
        return "\n".join(parts)
    except Exception as e:
        print(f"DOCS: docx extraction error: {e}")
        return ""


def _extract_pdf_text(path: str) -> str:
    """
    Извлекает текст из PDF через PyPDF2.

    Использует extract_text() для каждой страницы.
    Для PDF-сканов (фото страниц) вернёт пустоту —
    текстовый слой в сканах отсутствует.
    """
    try:
        from PyPDF2 import PdfReader
        reader = PdfReader(path)
        parts = []
        for page in reader.pages:
            text = page.extract_text()
            if text:
                parts.append(text)
        return "\n".join(parts)
    except Exception as e:
        print(f"DOCS: pdf extraction error: {e}")
        return ""


def _extract_pptx_text(path: str) -> str:
    """
    Извлекает текст из презентации (.pptx) через python-pptx.

    Проходит по каждому слайду, каждому shape на слайде и собирает
    текст из всех text_frame. Слайды разделяются пустой строкой.
    Формат вывода:
      [Слайд 1]
      текст слайда 1

      [Слайд 2]
      текст слайда 2
    """
    try:
        from pptx import Presentation
        prs = Presentation(path)
        parts = []
        for slide_num, slide in enumerate(prs.slides, 1):
            slide_texts = []
            for shape in slide.shapes:
                if shape.has_text_frame:
                    for para in shape.text_frame.paragraphs:
                        text = para.text.strip()
                        if text:
                            slide_texts.append(text)
            if slide_texts:
                parts.append(f"[Слайд {slide_num}]\n" + "\n".join(slide_texts))
        return "\n\n".join(parts)
    except Exception as e:
        print(f"DOCS: pptx extraction error: {e}")
        return ""


def _extract_rtf_text(path: str) -> str:
    """
    «Грубое» извлечение текста из RTF-файла.

    RTF — это размеченный текст с escaped-символами. Полный парсинг RTF
    требует отдельной библиотеки, которая не в requirements.txt. Вместо
    этого делаем «грубое» чтение: открываем файл в ascii+replace,
    вытаскиваем Unicode-литералы \\uN? (юникодные символы) и убираем
    служебные команды RTF (\\par, \\b, \\i и т.п.).

    Результат — приблизительный текст. Для точного чтения RTF нужна
    отдельная библиотека (pyth, striprtf и т.п.), но для целей ИИ
    «грубого» текста достаточно — ИИ сам разберёт структуру.
    """
    try:
        with open(path, "r", encoding="latin-1") as fh:
            raw = fh.read()
        import re
        # Кодовая страница RTF: \ansicpg1251 и т.п. По умолчанию cp1252.
        cp_match = re.search(r"\\ansicpg(\d+)", raw)
        codepage = f"cp{cp_match.group(1)}" if cp_match else "cp1252"

        def _replace_ansi(m):
            try:
                return bytes([int(m.group(1), 16)]).decode(codepage)
            except (LookupError, UnicodeDecodeError):
                return ""
        text = re.sub(r"\\'([0-9a-fA-F]{2})", _replace_ansi, raw)

        # Извлекаем Unicode-литералы \uN? → символы
        def _replace_unicode(m):
            code = int(m.group(1))
            if code > 32767:
                code -= 65536
            try:
                return chr(code)
            except (ValueError, OverflowError):
                return ""
        text = re.sub(r"\\u(\d+)\?", _replace_unicode, text)
        # Убираем RTF-команды (\\команда, включая \\b0, \\par, \\cf1 и т.п.)
        text = re.sub(r"\\[a-zA-Z]+\d*\s*", " ", text)
        # Убираем фигурные скобки { }
        text = text.replace("{", "").replace("}", "")
        # Убираем заголовок RTF
        text = re.sub(r"^\\rtf\d+.*?\n", "", text, count=1)
        # Схлопываем множественные пробелы/переносы
        text = re.sub(r"\s+", " ", text).strip()
        return text
    except Exception as e:
        print(f"DOCS: rtf extraction error: {e}")
        return ""


# ============================================================
# СБОРКА ПРЕЗЕНТАЦИИ ДЛЯ КОНСТРУКТОРА СЛАЙДОВ
# ============================================================

def _build_doc_slides(
    title: str, text: str, bg_key: str, slide_count: int | None
) -> tuple[str, str]:
    """
    Собирает PowerPoint-презентацию из данных конструктора.

    Разбивает текст на абзацы и распределяет по слайдам.
    Фон — градиент Pillow (тот же, что и в export.py).
    Подпись «Команда КУБ-СКРИПТ» — на последнем слайде.

    Возвращает (путь_к_файлу, имя_файла).
    """
    # Разбиваем текст на абзацы (разделители — пустые строки).
    paragraphs = [p.strip() for p in text.split("\n\n") if p.strip()]
    if not paragraphs:
        paragraphs = [text.strip()]

    # Определяем число слайдов.
    if slide_count is None or slide_count <= 0:
        # Автоматически: ~2 абзаца на слайд, мин 1, макс 20.
        slide_count = min(20, max(1, (len(paragraphs) + 1) // 2))
    else:
        slide_count = min(20, max(1, slide_count))

    # Разбиваем абзацы на слайды (равномерно).
    per_slide = -(-len(paragraphs) // slide_count)  # ceil division
    chunks = [paragraphs[i:i + per_slide] for i in range(0, len(paragraphs), per_slide)]
    while len(chunks) < slide_count:
        chunks.append([])

    # Стиль шрифта по умолчанию — Calibri (на всех Windows).
    font_name, _ = _font_helper("calibri")
    # Страховка от неверного ключа фона: неизвестный ключ → белый фон.
    if bg_key not in _BACKGROUNDS:
        bg_key = "white"
    _, fg_rgb, accent_rgb = _palette(bg_key)

    # Собираем презентацию.
    from pptx import Presentation
    from pptx.dml.color import RGBColor
    from pptx.util import Inches, Pt

    prs = Presentation()
    prs.slide_width = Inches(13.333)
    prs.slide_height = Inches(7.5)
    blank = prs.slide_layouts[6]

    bg_path = _make_bg_image(bg_key, 1280, 720)
    fg = RGBColor(*fg_rgb)
    accent = RGBColor(*accent_rgb)

    for idx, chunk in enumerate(chunks):
        slide = prs.slides.add_slide(blank)

        # Фон — Pillow-картинка.
        slide.shapes.add_picture(
            bg_path, 0, 0,
            width=prs.slide_width, height=prs.slide_height,
        )

        is_last = idx == len(chunks) - 1
        # Заголовок: первый слайд — крупно, остальные — мелко.
        if idx == 0:
            hdr_top, hdr_h, hdr_sz = Inches(0.35), Inches(1.1), 30
            body_top, body_h = Inches(1.6), Inches(5.0)
        else:
            hdr_top, hdr_h, hdr_sz = Inches(0.3), Inches(0.6), 20
            body_top, body_h = Inches(1.0), Inches(5.5)

        # Заголовок слайда.
        hdr_box = slide.shapes.add_textbox(Inches(0.6), hdr_top, Inches(12.1), hdr_h)
        hdr_tf = hdr_box.text_frame
        hdr_tf.word_wrap = True
        run_hdr = hdr_tf.paragraphs[0].add_run()
        run_hdr.text = title
        run_hdr.font.name = font_name
        run_hdr.font.size = Pt(hdr_sz)
        run_hdr.font.bold = (idx == 0)
        run_hdr.font.color.rgb = accent

        # Текст слайда.
        body_box = slide.shapes.add_textbox(Inches(0.6), body_top, Inches(12.1), body_h)
        body_tf = body_box.text_frame
        body_tf.word_wrap = True

        first_p = True
        for para_text in chunk:
            para = body_tf.paragraphs[0] if first_p else body_tf.add_paragraph()
            first_p = False
            run = para.add_run()
            run.text = para_text
            run.font.name = font_name
            run.font.size = Pt(16)
            run.font.color.rgb = fg
            para.space_after = Pt(6)

        # Подпись авторов — только на последнем слайде.
        if is_last:
            auth_box = slide.shapes.add_textbox(
                Inches(0.6), Inches(6.8), Inches(12.1), Inches(0.5),
            )
            auth_tf = auth_box.text_frame
            auth_tf.word_wrap = True
            run_auth = auth_tf.paragraphs[0].add_run()
            run_auth.text = AUTHORS_SIGNATURE
            run_auth.font.name = font_name
            run_auth.font.size = Pt(14)
            run_auth.font.bold = True
            run_auth.font.color.rgb = accent

    ts = int(time.time())
    filename = f"Презентация_{ts}.pptx"

    # Сохраняем в папку generated_slides.
    os.makedirs(_SLIDES_OUTPUT_DIR, exist_ok=True)
    path = os.path.join(_SLIDES_OUTPUT_DIR, filename)
    prs.save(path)

    try:
        os.remove(bg_path)
    except OSError:
        pass

    return path, filename


# ============================================================
# СБОРКА ОТРЕДАКТИРОВАННОГО ДОКУМЕНТА
# ============================================================

def _temp_path(suffix: str, prefix: str) -> str:
    fd, path = tempfile.mkstemp(suffix=suffix, prefix=prefix)
    os.close(fd)
    return path


def _rebuild_doc(
    ext: str, edited_text: str, original_text: str,
    doc_title: str, user_id: int,
) -> tuple[str, str]:
    """
    Собирает документ заново после редактирования ИИ.

    Использует тот же формат, что и исходный документ.
    Формат выбирается по расширению ext.

    Возвращает (путь_к_файлу, имя_файла).
    """
    ts = int(time.time())
    font_key = "calibri"
    font_name, _ = _font_helper(font_key)
    hex_fill, fg_rgb, accent_rgb = _palette("white")

    # Собираем документ по формату.
    if ext == ".docx":
        filename = f"Документ_{ts}.docx"
        path = _temp_path(".docx", "maxo_doc_edit_")
        _build_docx(path, edited_text, doc_title or "Документ", "",
                    font_name, hex_fill, fg_rgb, accent_rgb)
        return path, filename

    elif ext == ".pdf":
        filename = f"Документ_{ts}.pdf"
        path = _temp_path(".pdf", "maxo_doc_edit_")
        _build_pdf(path, edited_text, doc_title or "Документ", "",
                   font_name, _, "white", fg_rgb, accent_rgb)
        return path, filename

    elif ext == ".pptx":
        filename = f"Документ_{ts}.pptx"
        path = _temp_path(".pptx", "maxo_doc_edit_")
        # Разбиваем текст на слайды (по 2 абзаца).
        paragraphs = [p.strip() for p in edited_text.split("\n\n") if p.strip()]
        if not paragraphs:
            paragraphs = [edited_text.strip()]
        _build_pptx(path, edited_text, doc_title or "Презентация", "",
                    font_name, "white", fg_rgb, accent_rgb, None)
        return path, filename

    elif ext == ".rtf":
        filename = f"Документ_{ts}.rtf"
        path = _temp_path(".rtf", "maxo_doc_edit_")
        _build_rtf(path, edited_text, doc_title or "Документ", "",
                   font_name, hex_fill, fg_rgb, accent_rgb)
        return path, filename

    # Формат неизвестен — возвращаем как .txt.
    filename = f"Документ_{ts}.txt"
    path = _temp_path(".txt", "maxo_doc_edit_")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(edited_text)
    return path, filename


# ============================================================
# ПРЕВЬЮ ДЛЯ КОНСТРУКТОРА СЛАЙДОВ
# ============================================================

_MAX_DOC_PREVIEW_IMAGES = 10


def _render_doc_preview(
    title: str, text: str, bg_key: str, slide_count: int | None = None
) -> list:
    """
    Рисует PNG-превью слайдов через Pillow.

    Аналог _render_preview_images из export.py, но упрощённый.
    Число и разбиение превью повторяют реальную сборку (до 10 картинок).
    Возвращает список объектов PIL.Image.
    """
    from PIL import Image, ImageDraw, ImageFont

    # Страховка от неверного ключа фона: неизвестный ключ → белый фон.
    if bg_key not in _BACKGROUNDS:
        bg_key = "white"
    _label, (top, bottom) = _BACKGROUNDS[bg_key]
    _hex_fill, fg_rgb, accent_rgb = _palette(bg_key)
    _font_name, ttf_path = _font_helper("calibri")
    ttf = ttf_path if ttf_path and os.path.exists(ttf_path) else None

    def _load_font(size):
        if ttf:
            try:
                return ImageFont.truetype(ttf, size)
            except Exception:
                pass
        return ImageFont.load_default()

    def _gradient(w, h):
        img = Image.new("RGB", (w, h))
        d = ImageDraw.Draw(img)
        for y in range(h):
            t = y / max(1, h - 1)
            color = tuple(int(top[i] + (bottom[i] - top[i]) * t) for i in range(3))
            d.line([(0, y), (w, y)], fill=color)
        return img

    def _wrap(f, text_block, max_w):
        lines = []
        cur = ""
        for word in text_block.split():
            trial = (cur + " " + word).strip()
            if f.getlength(trial) <= max_w:
                cur = trial
            else:
                if cur:
                    lines.append(cur)
                cur = word
        if cur:
            lines.append(cur)
        return lines

    paragraphs = [p.strip() for p in text.split("\n\n") if p.strip()]
    if not paragraphs:
        paragraphs = [text.strip()]

    if slide_count is None or slide_count <= 0:
        slide_count = min(20, max(1, (len(paragraphs) + 1) // 2))
    else:
        slide_count = min(20, max(1, slide_count))

    per_slide = -(-len(paragraphs) // slide_count)
    chunks = [
        paragraphs[i:i + per_slide]
        for i in range(0, len(paragraphs), per_slide)
    ]
    while len(chunks) < slide_count:
        chunks.append([])
    chunks = chunks[:_MAX_DOC_PREVIEW_IMAGES]

    preview_images = []
    W, H = 1280, 720

    for chunk in chunks:
        img = _gradient(W, H)
        d = ImageDraw.Draw(img)

        # Заголовок — сверху (акцентный фон).
        d.rectangle([0, 0, W, 100], fill=accent_rgb)
        d.text((40, 30), title[:80], font=_load_font(40), fill=(255, 255, 255))

        # Текст слайда.
        para = "\n\n".join(chunk)
        font = _load_font(22)
        max_w = W - 120
        y = 140
        for line in _wrap(font, para, max_w)[:10]:
            d.text((60, y), line, font=font, fill=fg_rgb)
            y += 30
            if y > H - 60:
                break

        preview_images.append(img)

    return preview_images
