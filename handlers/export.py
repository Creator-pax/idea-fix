# ============================================================
# СОХРАНЕНИЕ ОТВЕТОВ ИИ В ФАЙЛ — handlers/export.py
# ============================================================
# Этот модуль добавляет кнопку «📄 Сохранить в файл» к ответу ИИ и
# конвертирует ответ в один из четырёх форматов:
#   • PDF          — ReportLab (+ фон рисует Pillow);
#   • Word (.docx) — python-docx  (Aspose.Words недоступен для Python 3.14);
#   • RTF          — собственный генератор RTF (у Aspose тоже ограничение);
#   • PowerPoint   — python-pptx (+ фон через Pillow, до 10 слайдов).
#
# Мастер настройки — КНОПКАМИ (колбэки export_*):
#   1. export_start     → выбор формата;
#   2. export_fmt_<fmt> → выбор шрифта (30 вариантов, все с кириллицей);
#      • 30 шрифтов не помещаются в одну клавиатуру (лимит MAX на число
#        рядов — ошибка errors.maxRows), поэтому список разбит на страницы
#        по _FONT_PAGE_SIZE штук; листание — кнопки export_font_page_<n>;
#   3. export_font_<f>  → выбор фона (4 градиента, рисует Pillow);
#   4. export_bg_<bg>   → что добавить к тексту (ИИ сгенерирует вывод или
#                         ключевые слова, или заголовок);
#   5. export_extra_<e> → (только для PPTX) число слайдов: 1/2/3/4/Авто;
#   6. export_slides_<n>→ сборка файла и отправка.
#
# Для PowerPoint каждый слайд получает:
#   • фон — градиент Pillow (рисуется на каждом слайде);
#   • шрифт — тот, что выбрал пользователь;
#   • на первом слайде заголовок крупно, на остальных — мелко;
#   • дополнение от ИИ и подпись «Команда КУБ-СКРИПТ» — на последнем.
#
# В каждый документ добавляется подпись авторов «Команда КУБ-СКРИПТ».
#
# Примечание: Aspose.Words требует Python <3.14 (ограничение пакета).
# Собранная среда — Python 3.14.6, поэтому Word/RTF реализованы на
# python-docx и ручном RTF-генераторе — формат тот же, лицензии не нужно.
# ============================================================

import asyncio          # to_thread для блокирующей сборки файлов и ИИ
import binascii         # фото в RTF: байты в hex (формат \pict)
import io               # превью: PNG-байты в памяти через BytesIO
import os               # пути к системным шрифтам, удаление temp-файлов
import shutil           # удаление временной папки с фото (rmtree)
import tempfile         # временные файлы для сгенерированных документов
import time             # уникальные имена файлов в одной папке

from maxo import Router
from maxo.routing.filters import StateFilter
from maxo.types import CallbackButton, MessageCallback, MessageCreated
from maxo.utils.builders import KeyboardBuilder
from maxo.utils.upload_media import BufferedInputFile

# БД нужна, чтобы взять ПОСЛЕДНИЙ ответ ИИ пользователя для экспорта
# (get_ai_logs_for_user) и проверить права доступа.
import database as db
# _ask_ai — синхронный вызов локального ИИ (LM Studio); используем его,
# чтобы ИИ «дописывал» контент в документ (вывод, ключевые слова, заголовок).
# Импортируем приватную функцию специально: она не создаёт циклов импорта.
from handlers.ai import _ask_ai
from handlers.filters import CallbackPayload
from handlers.markdown import (
    markdown_to_plain,
    parse_blocks,
    reportlab_inline,
    spans_to_text,
)
from handlers.states import ExportStates
from handlers.user_utils import (
    attachment_url,
    get_image_attachments,
    get_user_id,
)

router: Router = Router(name="export")

# ============================================================
# КОНСТАНТЫ МАСТЕРА
# ============================================================

# Подпись авторов — добавляется в каждый документ.
AUTHORS_SIGNATURE = "Команда «КУБ-СКРИПТ»"

# Сколько фотографий максимум можно вставить в один документ.
_MAX_PHOTOS = 6

# Максимум картинок-превью, которое бот пришлёт одной группой.
_MAX_PREVIEW_IMAGES = 4

# (ключ формата) -> (подпись кнопки, расширение файла).
_FORMATS = {
    "pdf":  ("📄 PDF", "pdf"),
    "word": ("📝 Word (.docx)", "docx"),
    "rtf":  ("📜 RTF", "rtf"),
    "pptx": ("📽 PowerPoint (.pptx)", "pptx"),
}

# (ключ шрифта) -> (человекочитаемое имя, имя ttf-файла в C:\Windows\Fonts).
# Подобраны шрифты Windows, поддерживающие кириллицу (текст на русском).
_FONTS = {
    "arial":         ("Arial", "arial.ttf"),
    "arialnarrow":   ("Arial Narrow", "ARIALN.TTF"),
    "arialblack":    ("Arial Black", "ariblk.ttf"),
    "times":         ("Times New Roman", "times.ttf"),
    "courier":       ("Courier New", "cour.ttf"),
    "georgia":       ("Georgia", "georgia.ttf"),
    "verdana":       ("Verdana", "verdana.ttf"),
    "tahoma":        ("Tahoma", "tahoma.ttf"),
    "trebuchet":     ("Trebuchet MS", "trebuc.ttf"),
    "impact":        ("Impact", "impact.ttf"),
    "comic":         ("Comic Sans MS", "comic.ttf"),
    "lucidaconsole": ("Lucida Console", "lucon.ttf"),
    "lucidasans":    ("Lucida Sans Unicode", "l_10646.ttf"),
    "mssansserif":   ("Microsoft Sans Serif", "micross.ttf"),
    "bookman":       ("Bookman Old Style", "BOOKOS.TTF"),
    "franklin":      ("Franklin Gothic", "framd.ttf"),
    "centurygothic": ("Century Gothic", "GOTHIC.TTF"),
    "garamond":      ("Garamond", "GARA.TTF"),
    "palatino":      ("Palatino Linotype", "pala.ttf"),
    "rockwell":      ("Rockwell", "ROCK.TTF"),
    "consolas":      ("Consolas", "consola.ttf"),
    "constantia":    ("Constantia", "constan.ttf"),
    "corbel":        ("Corbel", "corbel.ttf"),
    "calibri":       ("Calibri", "calibri.ttf"),
    "candara":       ("Candara", "Candara.ttf"),
    "segoeui":       ("Segoe UI", "segoeui.ttf"),
    "segoeprint":    ("Segoe Print", "segoepr.ttf"),
    "gabriola":      ("Gabriola", "Gabriola.ttf"),
    "sylfaen":       ("Sylfaen", "sylfaen.ttf"),
    "vladimir":      ("Vladimir Script", "VLADIMIR.TTF"),
}

# (ключ фона) -> (подпись кнопки, (верх, низ) градиента в RGB).
# Градиент рисуется Pillow и кладётся фоном в PDF и PowerPoint.
_BACKGROUNDS = {
    "white":     ("⚪ Белый", ((255, 255, 255), (238, 244, 252))),
    "cream":     ("🥛 Кремовый", ((254, 247, 231), (255, 235, 179))),
    "lightblue": ("💧 Голубой", ((228, 243, 255), (211, 234, 255))),
    "dark":      ("🌙 Тёмный", ((46, 50, 60), (26, 29, 35))),
    "pink":      ("🌸 Розовый", ((255, 240, 245), (255, 182, 193))),
    "green":     ("🌲 Зелёный", ((240, 255, 240), (144, 238, 144))),
    "purple":    ("🟣 Фиолетовый", ((248, 240, 255), (221, 160, 221))),
    "orange":    ("🟠 Оранжевый", ((255, 248, 240), (255, 179, 71))),
    "red":       ("🔴 Красный", ((255, 240, 240), (255, 107, 107))),
    "indigo":    ("🔵 Индиго", ((240, 240, 255), (106, 90, 205))),
}

# (ключ «что добавить») -> (подпись кнопки, использовать ли ИИ).
_EXTRAS = {
    "none":     ("🚫 Ничего", False),
    "title":    ("📌 Заголовок", True),
    "summary":  ("✍️ Вывод", True),
    "keywords": ("🔑 Ключевые слова", True),
}

# Сколько секунд ждать «доп. контент» от ИИ при сборке документа.
# Локальная модель (LM Studio) умеет «думать» минутами, а файл нужен здесь
# и сейчас — поэтому на ожидание ставим жёсткий потолок. Если ИИ не успел —
# документ собирается с безопасными заменителями (заголовок = вопрос,
# дополнения нет), процесс не «зависает» навсегда.
_EXTRA_AI_TIMEOUT_SECONDS = 25


# ============================================================
# ПОМОЩНИКИ МАСТЕРА (клавиатуры и шаги)
# ============================================================

def _cancel_button() -> KeyboardBuilder:
    """Кнопка «Отмена» — прерывает мастер."""
    kb = KeyboardBuilder()
    kb.add_callback(text="❌ Отмена", payload="export_cancel")
    return kb


def _build_options_keyboard(options: dict, extra_payload: str) -> KeyboardBuilder:
    """Строит клавиатуру из словаря {ключ: (подпись, ...)} + кнопка «Отмена».

    Каждая кнопка получает payload "<extra_payload><ключ>".
    Второй элемент кортежа (_unused) игнорируется — он есть в словарях
    _FORMATS / _FONTS / _BACKGROUNDS / _EXTRAS, но для построения кнопок
    нужна только подпись. Это единый интерфейс: один код работает со всеми
    словарями настроек мастера.
    """
    kb = KeyboardBuilder()
    for key, (label, _unused) in options.items():
        kb.add_callback(text=label, payload=f"{extra_payload}{key}")
    kb.add_callback(text="❌ Отмена", payload="export_cancel")
    return kb.adjust(1)


# Сколько шрифтов помещается на одной странице выбора. 30 шрифтов не влезают
# в одну клавиатуру — MAX отклоняет её целиком с ошибкой errors.maxRows
# (превышен лимит на число рядов кнопок). Поэтому список разбит на страницы:
# на каждой странице _FONT_PAGE_SIZE шрифтов + строка навигации + «Отмена».
_FONT_PAGE_SIZE = 6


def _font_page_count() -> int:
    """Число страниц списка шрифтов (минимум — одна)."""
    return max((len(_FONTS) + _FONT_PAGE_SIZE - 1) // _FONT_PAGE_SIZE, 1)


def _build_font_keyboard(page: int = 0) -> KeyboardBuilder:
    """Клавиатура одной страницы списка шрифтов.

    Страница содержит _FONT_PAGE_SIZE шрифтов, кнопку-счётчик страниц
    («1/5») и стрелки «⬅️/➡️» для листания (payload export_font_page_<n>),
    а внизу — «⬅️ Назад» (к выбору формата) и «❌ Отмена». Число рядов
    держится в пределах лимита MAX, чтобы не ловить errors.maxRows.

    Ряды строятся вручную: сначала ключевые шрифты, затем один ряд
    навигации (стрелки + счётчик), затем «Назад» и «Отмена» отдельными
    рядами. В конце никакого adjust(1) — он расплющил бы навигацию в столбик.
    """
    page = max(0, min(page, _font_page_count() - 1))
    keys = list(_FONTS)
    chunk = keys[page * _FONT_PAGE_SIZE : (page + 1) * _FONT_PAGE_SIZE]

    kb = KeyboardBuilder()
    for key in chunk:
        # Каждый шрифт — своим рядом (одна кнопка на строку клавиатуры).
        kb.row(CallbackButton(text=_FONTS[key][0], payload=f"export_font_{key}"))

    if _font_page_count() > 1:
        nav = [CallbackButton(text=f"{page + 1}/{_font_page_count()}", payload=f"export_font_page_{page}")]
        if page > 0:
            nav.insert(0, CallbackButton(text="⬅️", payload=f"export_font_page_{page - 1}"))
        if page < _font_page_count() - 1:
            nav.append(CallbackButton(text="➡️", payload=f"export_font_page_{page + 1}"))
        kb.row(*nav, width=len(nav))

    kb.row(CallbackButton(text="⬅️ Назад", payload="export_start"))
    kb.row(CallbackButton(text="❌ Отмена", payload="export_cancel"))
    return kb


def _step_text(step_number: int, total: int = 4, question: str | None = None) -> str:
    """Шапка сообщения-вопроса мастера (номер шага + тема ответа)."""
    theme = f"\n\nТема: {question[:80]}" if question else ""
    return f"📄 Настройка сохранения ({step_number}/{total}){theme}"


# ============================================================
# ОСНОВНОЙ ОБРАБОТЧИК МАСТЕРА
# ============================================================

@router.message_callback(
    CallbackPayload(
        "export_start", "export_cancel",
        "export_photo_yes", "export_photo_no", "export_photo_done",
        "export_photo_step", "export_preview", "export_build",
        "export_title_custom", "export_title_auto",
        prefixes=(
            "export_fmt_", "export_font_", "export_bg_", "export_extra_",
            "export_slides_",
        ),
    )
)
async def handle_export(update: MessageCallback, fsm_context) -> None:
    """Обрабатывает все нажатия кнопок раздела «Сохранить в файл»."""
    user_id = get_user_id(update)
    if not user_id:
        return

    # Единый барьер доступа: пользователь существует, одобрен, не заблокирован.
    if not db.user_exists(user_id) or not db.is_approved(user_id) or db.is_blocked(user_id):
        return

    payload = update.payload

    # ─────────────────────────── ОТМЕНА ───────────────────────────
    if payload == "export_cancel":
        await fsm_context.set_state(None)          # выходим из фото-режима
        await _cleanup_photos(fsm_context)          # убираем скачанные фото
        await update.callback_answer(text="Отменено")
        back = KeyboardBuilder()
        back.add_callback(text="← В меню ИИ", payload="menu_ai")
        back.add_callback(text="← Главное меню", payload="menu_main")
        await update.edit_message("❌ Сохранение отменено.", keyboard=back.adjust(1).build())
        return

    # ─────────────────────────── ШАГ 0: СТАРТ ───────────────────────────
    if payload == "export_start":
        # Чистим «хвосты» прошлого экспорта (скачанные фото на диске).
        await _cleanup_photos(fsm_context)
        # Берём последний ответ ИИ пользователя из лога.
        logs = db.get_ai_logs_for_user(user_id, limit=1)
        if not logs:
            await update.callback_answer(text="Нет ответа")
            await update.edit_message(
                "🤖 Сначала задай вопрос ИИ, а потом сохрани ответ в файл!"
            )
            return
        await update.callback_answer(text="Формат")
        await fsm_context.update_data(
            export_text=logs[0].get("answer") or "",
            export_question=logs[0].get("question") or "",
            export_title=None,
        )
        # Формат из _FORMATS: значения — пары (подпись, расширение).
        formats = {key: (label, ext) for key, (label, ext) in _FORMATS.items()}
        kb = _build_options_keyboard(formats, "export_fmt_")
        await update.edit_message(
            _step_text(1) + "\n\nВ какой формат сохранить ответ?",
            keyboard=kb.build(),
        )
        return

    # Данные мастера (текст ответа, вопрос, уже выбранные опции).
    data = await fsm_context.get_data()
    text = data.get("export_text") or ""
    question = data.get("export_question") or ""
    # Для PowerPoint шагов на один больше (выбор числа слайдов) — /5.
    total = 5 if data.get("export_fmt") == "pptx" else 4

    # ─────────────────────────── ШАГ 1: ФОРМАТ ───────────────────────────
    if payload.startswith("export_fmt_"):
        fmt = payload[len("export_fmt_"):]
        if fmt not in _FORMATS:
            return
        await fsm_context.update_data(export_fmt=fmt)
        # Кнопка «Назад» возвращает на выбор формата (payload export_start).
        # Шрифты разбиты на страницы (лимит MAX на ряды клавиатуры), поэтому
        # сначала показывается первая страница (page=0).
        await update.callback_answer(text="Шрифт")
        kb = _build_font_keyboard(page=0)
        await update.edit_message(
            _step_text(2, total, question) + "\n\nКакой шрифт использовать?",
            keyboard=kb.build(),
        )
        return

    # ──────────────────── ШАГ 1.5: ЛИСТАНИЕ ШРИФТОВ ────────────────────
    # Payload export_font_page_<n> начинается с export_font_, поэтому обратно
    # ветка должна идти ДО обработки выбора шрифта export_font_<ключ>.
    if payload.startswith("export_font_page_"):
        try:
            page = max(int(payload[len("export_font_page_"):]), 0)
        except ValueError:
            return
        # Не даём листать дальше последней страницы — молча фиксируем номер.
        page = min(page, _font_page_count() - 1)
        await update.callback_answer(text="Шрифт")
        kb = _build_font_keyboard(page=page)
        await update.edit_message(
            _step_text(2, total, question) + "\n\nКакой шрифт использовать?",
            keyboard=kb.build(),
        )
        return

    # ─────────────────────────── ШАГ 2: ШРИФТ ───────────────────────────
    if payload.startswith("export_font_"):
        font = payload[len("export_font_"):]
        if font not in _FONTS:
            return
        await fsm_context.update_data(export_font=font)
        await update.callback_answer(text="Фон")
        kb = _build_options_keyboard(
            {key: (label, None) for key, (label, _cols) in _BACKGROUNDS.items()},
            "export_bg_",
        )
        # «Назад» снова открывает выбор шрифта (передаём уже выбранный fmt).
        kb.add_callback(text="⬅️ Назад", payload=f"export_fmt_{data.get('export_fmt')}")
        kb.add_callback(text="❌ Отмена", payload="export_cancel")
        await update.edit_message(
            _step_text(3, total, question) + "\n\nКакой фон использовать?",
            keyboard=kb.adjust(1).build(),
        )
        return

    # ─────────────────────────── ШАГ 3: ФОН ───────────────────────────
    if payload.startswith("export_bg_"):
        bg = payload[len("export_bg_"):]
        if bg not in _BACKGROUNDS:
            return
        await fsm_context.update_data(export_bg=bg)
        await update.callback_answer(text="Дополнение")
        kb = _build_options_keyboard(
            {key: (label, None) for key, (label, _use_ai) in _EXTRAS.items()},
            "export_extra_",
        )
        kb.add_callback(text="⬅️ Назад", payload=f"export_font_{data.get('export_font')}")
        kb.add_callback(text="❌ Отмена", payload="export_cancel")
        await update.edit_message(
            _step_text(4, total, question)
            + "\n\nЧто добавить к тексту? (вывод и ключевые слова "
              "составит ИИ)",
            keyboard=kb.adjust(1).build(),
        )
        return

    # ─────────────────────────── ШАГ 4: ДОПОЛНЕНИЕ ───────────────────────────
    if payload.startswith("export_extra_"):
        extra = payload[len("export_extra_"):]
        if extra not in _EXTRAS:
            return

        fmt = data.get("export_fmt")
        font = data.get("export_font")
        bg = data.get("export_bg")
        if fmt not in _FORMATS or font not in _FONTS or bg not in _BACKGROUNDS:
            # Потеряли часть настроек (например, FSM очищен) — начинаем заново.
            await fsm_context.update_data(export_fmt=None, export_font=None, export_bg=None)
            kb = _build_options_keyboard(
                {key: (label, ext) for key, (label, ext) in _FORMATS.items()},
                "export_fmt_",
            )
            await update.edit_message(
                "⚠️ Настройки сброшены. Начни заново:\n\n"
                + _step_text(1) + "\n\nВ какой формат сохранить ответ?",
                keyboard=kb.build(),
            )
            return

        await fsm_context.update_data(export_extra=extra)

        # Для PowerPoint: спросим ещё, на сколько слайдов разбить текст.
        # Каждый слайд получит выбранный шрифт и фон (Pillow).
        if fmt == "pptx":
            await update.callback_answer(text="Слайды")
            kb = _build_options_keyboard(
                {
                    "1": ("🖼 1 слайд", None),
                    "2": ("🖼 2 слайда", None),
                    "3": ("🖼 3 слайда", None),
                    "4": ("🖼 4 слайда", None),
                    "auto": ("🖼 Авто (по абзацам)", None),
                },
                "export_slides_",
            )
            kb.add_callback(text="⬅️ Назад", payload=f"export_extra_{extra}")
            kb.add_callback(text="❌ Отмена", payload="export_cancel")
            await update.edit_message(
                _step_text(5, total, question)
                + "\n\nНа сколько слайдов разбить презентацию?",
                keyboard=kb.adjust(1).build(),
            )
            return

        # Не PowerPoint — до сборки ещё спросим про название и фото.
        await _ask_title(update, fsm_context, question, fmt, extra)
        return

    # ─────────────────────────── ШАГ 5: ЧИСЛО СЛАЙДОВ (только PPTX) ───────────────────────────
    if payload.startswith("export_slides_"):
        slides = payload[len("export_slides_"):]
        # "auto" — разбиение по абзацам; числа 1..4 — точное число слайдов.
        slide_count = None if slides == "auto" else (int(slides) if slides.isdigit() else None)
        if slide_count is None and slides != "auto":
            return

        extra = data.get("export_extra")
        fmt = data.get("export_fmt")
        font = data.get("export_font")
        bg = data.get("export_bg")
        if (
            extra not in _EXTRAS or fmt != "pptx"
            or font not in _FONTS or bg not in _BACKGROUNDS
        ):
            return

        await fsm_context.update_data(export_slides=slide_count)
        await _ask_title(update, fsm_context, question, fmt, extra)
        return

    # ─────────────────── ШАГ «НАЗВАНИЕ»: СВОЁ / АВТО ───────────────────
    # Пользователь может сам придумать название документа/презентации
    # (вводится текстом) или оставить автоматическое (из вопроса / от ИИ).
    if payload == "export_title_custom":
        fmt = data.get("export_fmt")
        if fmt not in _FORMATS:
            return
        await fsm_context.set_state(ExportStates.wait_title)
        await update.callback_answer(text="Название")
        kb = KeyboardBuilder()
        kb.add_callback(text="🤖 Авто-название", payload="export_title_auto")
        kb.add_callback(text="❌ Отмена", payload="export_cancel")
        await update.edit_message(
            "✍️ Напиши название документа/презентации текстом.\n\n"
            "Оно появится в шапке файла вместо автоматического.",
            keyboard=kb.adjust(1).build(),
        )
        return

    if payload == "export_title_auto":
        # Оставляем авто-название: сбрасываем введённое пользователем.
        await fsm_context.update_data(export_title=None)
        fmt = data.get("export_fmt")
        extra = data.get("export_extra")
        await update.callback_answer(text="Авто")
        if fmt in _FORMATS:
            await _ask_photos(update, fsm_context, question, fmt, extra)
        return

    # ─────────────────── ШАГ «ФОТО»: ДОБАВИТЬ / БЕЗ ФОТО ───────────────────
    # После выбора extra (и слайдов для PPTX) спрашиваем про фотографии.
    # Фото присылаются обычными сообщениями-картинками (см. обработчик
    # handle_export_photos ниже), завершение — кнопкой «✅ Готово».
    extra = data.get("export_extra")
    font = data.get("export_font")
    bg = data.get("export_bg")

    if payload == "export_photo_yes":
        fmt = data.get("export_fmt")
        if fmt not in _FORMATS:
            return
        await _cleanup_photos(fsm_context)          # сбрасываем старый набор
        await fsm_context.update_data(export_photos=[], export_photos_dir=None)
        await fsm_context.set_state(ExportStates.wait_photos)
        await update.callback_answer(text="Фото")
        kb = KeyboardBuilder()
        kb.add_callback(text="✅ Готово", payload="export_photo_done")
        kb.add_callback(text="❌ Отмена", payload="export_cancel")
        await update.edit_message(
            f"📷 Присылай фотографии — вставляю их в документ "
            f"(до {_MAX_PHOTOS} штук). Каждое фото добавляется сразу.\n\n"
            "Когда закончишь — нажми «✅ Готово».",
            keyboard=kb.adjust(1).build(),
        )
        return

    if payload in ("export_photo_no", "export_photo_done"):
        # Без фото или «Готово»: выходим из режима приёма фото и показываем
        # финальное меню (превью / собрать файл). Данные FSM не трогаем —
        # собранные фото сохраняются для сборки.
        await fsm_context.set_state(None)
        await _show_final_menu(update, fsm_context, question, extra)
        return

    # Повторный вызов шага «Фото» из финального меню.
    if payload == "export_photo_step":
        fmt = data.get("export_fmt")
        if fmt not in _FORMATS:
            return
        await _ask_photos(update, fsm_context, question, fmt, extra)
        return

    # ─────────────────────── ФИНАЛ: ПРЕВЬЮ ИЛИ СБОРКА ───────────────────────
    # Все настройки выбраны (плюс фото). Пользователь может сначала
    # посмотреть превью-картинки или сразу собрать готовый файл.
    if payload == "export_preview":
        fmt = data.get("export_fmt")
        if fmt not in _FORMATS or font not in _FONTS or bg not in _BACKGROUNDS:
            return
        await _generate_preview(update, fsm_context, fmt, extra, font, bg, text)
        return

    if payload == "export_build":
        fmt = data.get("export_fmt")
        if fmt not in _FORMATS or font not in _FONTS or bg not in _BACKGROUNDS:
            return
        slide_count = data.get("export_slides")
        photos = list(data.get("export_photos") or [])
        await _generate_and_send(
            update, fsm_context, fmt, extra, font, bg, slide_count, photos
        )
        return


# ============================================================
# ФОТО В ДОКУМЕНТАХ И ПРЕДПРОСМОТР
# ============================================================
# После выбора всех настроек мастер спрашивает про фотографии:
#   • «Добавить фото» — переводит пользователя в ExportStates.wait_photos,
#     и каждое пришедшее фото скачивается в temp-папку и добавляется в FSM;
#   • «Без фото» — сразу в финальное меню;
#   • финальное меню: «Показать превью» (PNG-картинки Pillow'ом) или
#     «Собрать файл» (готовый документ со вставленными фото).

def _rmtree_safe(path: str) -> None:
    """Удаляет временную папку (фото), игнорируя ошибки."""
    try:
        if path and os.path.isdir(path):
            shutil.rmtree(path, ignore_errors=True)
    except Exception:  # noqa: BLE001
        pass


# Максимальная ширина фото после нормализации. Фото из Telegram обычно JPEG,
# а для RTF нужен PNG, поэтому каждое фото пересохраняем в PNG и ужимаем —
# так все четыре сборщика (PDF/DOCX/RTF/PPTX) получают одинаковый, «родной»
# для документа битмап разумного размера.
_PHOTO_MAX_WIDTH = 1000


def _normalize_photo(source: str, dest: str) -> bool:
    """Пересохраняет фото в PNG (RGB, до _PHOTO_MAX_WIDTH). True — успех."""
    try:
        from PIL import Image

        with Image.open(source) as im:
            im = im.convert("RGB")
            if im.width > _PHOTO_MAX_WIDTH:
                ratio = _PHOTO_MAX_WIDTH / im.width
                im = im.resize(
                    (int(im.width * ratio), int(im.height * ratio)),
                    Image.LANCZOS,
                )
            im.save(dest, "PNG")
        return True
    except Exception:  # noqa: BLE001
        return False


def _valid_image(path: str) -> bool:
    """True, если файл — корректное изображение, которое откроет Pillow."""
    try:
        from PIL import Image

        with Image.open(path) as im:
            im.verify()
        return True
    except Exception:  # noqa: BLE001
        return False


async def _cleanup_photos(fsm_context) -> None:
    """Удаляет скачанные фото и вычищает их из данных FSM."""
    try:
        data = await fsm_context.get_data()
        _rmtree_safe(data.get("export_photos_dir") or "")
    except Exception:  # noqa: BLE001
        pass
    try:
        await fsm_context.update_data(export_photos=None, export_photos_dir=None)
    except Exception:  # noqa: BLE001
        pass


async def _ask_title(update, fsm_context, question: str, fmt: str, extra: str) -> None:
    """Показывает шаг «Название документа/презентации» мастера."""
    total = 5 if fmt == "pptx" else 4
    kb = KeyboardBuilder()
    kb.add_callback(text="✍️ Назвать самому", payload="export_title_custom")
    kb.add_callback(text="🤖 Авто-название", payload="export_title_auto")
    # «Назад» возвращает к выбору «что добавить» — настройки не теряются.
    kb.add_callback(text="⬅️ Назад", payload=f"export_extra_{extra}")
    kb.add_callback(text="❌ Отмена", payload="export_cancel")
    await update.callback_answer(text="Название")
    await update.edit_message(
        _step_text(total - 1, total, question)
        + "\n\nКак назвать документ?\n\n"
        "Можно ввести своё название или оставить автоматическое.",
        keyboard=kb.adjust(1).build(),
    )


async def _ask_photos(update, fsm_context, question: str, fmt: str, extra: str) -> None:
    """Показывает шаг «Добавить фото в документ?» мастера."""
    total = 5 if fmt == "pptx" else 4
    kb = KeyboardBuilder()
    kb.add_callback(text="📷 Добавить фото", payload="export_photo_yes")
    kb.add_callback(text="🚫 Без фото", payload="export_photo_no")
    # «Назад» возвращает к выбору «что добавить» — настройки не теряются.
    kb.add_callback(text="⬅️ Назад", payload=f"export_extra_{extra}")
    kb.add_callback(text="❌ Отмена", payload="export_cancel")
    await update.callback_answer(text="Фото")
    await update.edit_message(
        _step_text(total, total, question)
        + f"\n\nДобавить фотографии в документ? (до {_MAX_PHOTOS})",
        keyboard=kb.adjust(1).build(),
    )


async def _show_final_menu(update, fsm_context, question: str, extra: str) -> None:
    """Показывает финальное меню мастера: превью или сразу сборка."""
    kb = KeyboardBuilder()
    kb.add_callback(text="👁 Показать превью", payload="export_preview")
    kb.add_callback(text="📦 Собрать файл", payload="export_build")
    kb.add_callback(text="🖼 Изменить фото", payload="export_photo_step")
    kb.add_callback(text="⬅️ Назад", payload=f"export_extra_{extra}")
    kb.add_callback(text="❌ Отмена", payload="export_cancel")
    await update.callback_answer(text="Готово")
    await update.edit_message(
        "⚙️ Всё настроено!\n\nЧто дальше? Можно посмотреть превью "
        "или сразу собрать файл.",
        keyboard=kb.adjust(1).build(),
    )


@router.message_created(StateFilter(ExportStates.wait_photos))
async def handle_export_photos(update: MessageCreated, fsm_context) -> None:
    """Принимает фотографии, пока активен режим «добавить фото в документ».

    Каждое пришедшее сообщение с картинками скачивается ботом во временную
    папку (bot.download), пути сохраняются в FSM (export_photos) — их потом
    вставляет в документ _build_file. Кнопка «✅ Готово» завершает приём.
    """
    user_id = get_user_id(update)
    if not user_id:
        return
    if not db.user_exists(user_id) or not db.is_approved(user_id) or db.is_blocked(user_id):
        return

    body = update.message.body
    photos_in_msg = get_image_attachments(body)
    if not photos_in_msg:
        # Текст/файл вместо картинки — вежливо напоминаем, что ждём фото.
        kb = KeyboardBuilder()
        kb.add_callback(text="✅ Готово", payload="export_photo_done")
        kb.add_callback(text="❌ Отмена", payload="export_cancel")
        await update.answer_text(
            "Я жду фотографии для документа. Пришли картинку "
            "или нажми «✅ Готово».",
            keyboard=kb.adjust(1).build(),
        )
        return

    data = await fsm_context.get_data()
    photos = list(data.get("export_photos") or [])
    photodir = data.get("export_photos_dir")

    # Локальный импорт: bot_instance нужен только в момент скачивания.
    from bot_instance import bot

    added = 0
    errors: list[str] = []
    for attachment in photos_in_msg:
        if len(photos) >= _MAX_PHOTOS:
            break
        try:
            if not photodir:
                photodir = tempfile.mkdtemp(prefix="maxo_photos_")
            url = attachment_url(attachment)
            raw_path = os.path.join(
                photodir,
                f"photo_raw_{int(time.time())}_{len(photos)}_{added}.img",
            )
            await bot.download(url, destination=raw_path)
            if os.path.getsize(raw_path) == 0:
                errors.append("файл пустой")
                continue
            # Пересохраняем в PNG: RTF понимает только PNG-битмапы, а
            # Telegram присылает JPEG. Плюс ограничиваем ширину, чтобы
            # картинка не «распирала» страницу документа.
            norm_path = os.path.join(
                photodir,
                f"photo_{int(time.time())}_{len(photos)}_{added}.png",
            )
            if not _normalize_photo(raw_path, norm_path):
                # PNG не вышел — проверяем, валидное ли изображение в raw.
                # PDF/DOCX/PPTX умеют и исходный JPEG/WebP, поэтому для них
                # оставляем raw как есть (RTF без PNG пропустит фото —
                # это допустимая цена, зато в остальных форматах фото будет).
                if _valid_image(raw_path):
                    photos.append(raw_path)
                    added += 1
                else:
                    errors.append("не удалось обработать изображение")
                continue
            os.remove(raw_path)
            photos.append(norm_path)
            added += 1
        except Exception as e:  # noqa: BLE001
            err_text = str(e)[:200]
            if err_text not in errors:
                errors.append(err_text)
            print(f"EXPORT: photo download error: {err_text}")

    await fsm_context.update_data(export_photos=photos, export_photos_dir=photodir)

    bold = KeyboardBuilder()
    bold.add_callback(text="✅ Готово", payload="export_photo_done")
    bold.add_callback(text="❌ Отмена", payload="export_cancel")
    if added:
        if len(photos) >= _MAX_PHOTOS:
            await fsm_context.set_state(None)
            await update.answer_text(
                f"✅ Добавлено фото: {len(photos)}/{_MAX_PHOTOS} — больше не влезет.",
                keyboard=bold.adjust(1).build(),
            )
        else:
            await update.answer_text(
                f"✅ Добавлено фото: {len(photos)}/{_MAX_PHOTOS}. "
                "Пришли ещё или нажми «✅ Готово».",
                keyboard=bold.adjust(1).build(),
            )
    else:
        reason = f"\n\nℹ️ {errors[0]}" if errors else ""
        await update.answer_text(
            "Не получилось скачать фото. Попробуй ещё раз "
            "или нажми «✅ Готово»." + reason,
            keyboard=bold.adjust(1).build(),
        )


@router.message_created(StateFilter(ExportStates.wait_title))
async def handle_export_title(update: MessageCreated, fsm_context) -> None:
    """Принимает название документа/презентации, введённое пользователем.

    Название кладётся в FSM (export_title) и подставляется в шапку файла
    вместо автоматического (из вопроса / придуманного ИИ).
    """
    user_id = get_user_id(update)
    if not user_id:
        return
    if not db.user_exists(user_id) or not db.is_approved(user_id) or db.is_blocked(user_id):
        return

    title = (update.text or "").strip()
    if not title:
        kb = KeyboardBuilder()
        kb.add_callback(text="🤖 Авто-название", payload="export_title_auto")
        kb.add_callback(text="❌ Отмена", payload="export_cancel")
        await update.answer_text(
            "Я не увидел названия — напиши его текстом "
            "(до 80 символов) или выбери «Авто-название».",
            keyboard=kb.adjust(1).build(),
        )
        return

    # Ограничиваем длину — длинные названия ломают вёрстку шапки.
    if len(title) > 80:
        await update.answer_text(
            f"Название слишком длинное ({len(title)} символов). "
            "Сократи до 80 символов."
        )
        return

    await fsm_context.set_state(None)
    await fsm_context.update_data(export_title=title)

    kb = KeyboardBuilder()
    kb.add_callback(text="📷 Добавить фото", payload="export_photo_yes")
    kb.add_callback(text="🚫 Без фото", payload="export_photo_no")
    kb.add_callback(text="⬅️ Изменить название", payload="export_title_custom")
    kb.add_callback(text="❌ Отмена", payload="export_cancel")
    await update.answer_text(
        f"✍️ Название: «{title}»\n\nТеперь фотографии:",
        keyboard=kb.adjust(1).build(),
    )


def _render_preview_images(
    bg_key: str,
    font_key: str,
    title: str,
    extra_block: str,
    text: str,
    photos: list,
    slide_count: int | None,
    fmt: str,
) -> list:
    """Рисует PNG-превью страниц/слайдов через Pillow (реалистичный макет).

    Файловый формат при этом не создаётся — картинки отражают макет
    будущего документа: выбранный фон, шрифт, заголовок, текст и фото.
    """
    from PIL import Image, ImageDraw, ImageFont

    _label, (top, bottom) = _BACKGROUNDS[bg_key]
    _hex_fill, fg_rgb, accent_rgb = _palette(bg_key)
    font_name, ttf_path = _font_helper(font_key)
    ttf = ttf_path if ttf_path and os.path.exists(ttf_path) else None

    def _load_font(size):
        if ttf:
            try:
                return ImageFont.truetype(ttf, size)
            except Exception:  # noqa: BLE001
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

    preview_group = []

    # Разбираем markdown-текст на «строчки макета» с настройками шрифта.
    # Заголовки — крупнее и акцентным цветом, списки — с маркерами — это
    # повторяет то, как будет выглядеть готовый документ.
    items: list[dict] = []
    for block in parse_blocks(text):
        kind = block["kind"]
        if kind == "h":
            lvl = block.get("level", 1)
            items.append({
                "text": spans_to_text(block["spans"]),
                "size": {1: 34, 2: 30, 3: 26}.get(lvl, 26),
                "color": accent_rgb, "bold": True,
                "space_before": 18, "kind": "h",
            })
        elif kind in ("ul", "ol"):
            for idx, item in enumerate(block["items"], 1):
                marker = f"{idx}.  " if kind == "ol" else "•  "
                items.append({
                    "text": marker + spans_to_text(item),
                    "size": 22, "color": fg_rgb,
                    "bold": any("b" in st for st, _ in item),
                    "space_before": 4, "kind": "p",
                })
        elif kind == "code":
            items.append({
                "text": block["text"], "size": 18, "color": fg_rgb,
                "bold": False, "space_before": 6, "kind": "code",
            })
        elif kind == "quote":
            items.append({
                "text": "«" + spans_to_text(block["spans"]) + "»",
                "size": 22, "color": fg_rgb,
                "bold": any("b" in st for st, _ in block["spans"]),
                "space_before": 6, "italic": True, "kind": "p",
            })
        elif kind == "p":
            items.append({
                "text": spans_to_text(block["spans"]),
                "size": 22, "color": fg_rgb,
                "bold": any("b" in st for st, _ in block["spans"]),
                "space_before": 6, "kind": "p",
            })

    # Дополнение от ИИ (заголовок + текст) — тоже в макет.
    if extra_block:
        items.append({
            "text": "Дополнение", "size": 24, "color": accent_rgb,
            "bold": True, "space_before": 14, "kind": "h",
        })
        items.append({
            "text": extra_block, "size": 22, "color": fg_rgb,
            "bold": False, "space_before": 6, "kind": "p",
        })
    for it in items:
        it.setdefault("bold", False)
        it.setdefault("italic", False)

    is_pptx = fmt == "pptx"

    if is_pptx:
        # Слайды 16:9 — как в настоящей презентации: контент делится на
        # равные части по числу слайдов, шапка/заголовок сверху, галерея
        # фото внизу последнего слайда.
        W, H = 1280, 720
        num_slides = slide_count or 3
        num_slides = max(1, min(num_slides, _MAX_PREVIEW_IMAGES))
        per_slide = -(-len(items) // num_slides)
        chunks = [items[i : i + per_slide] for i in range(0, len(items), per_slide)]
        while len(chunks) < num_slides:
            chunks.append([])

        for idx, chunk_items in enumerate(chunks[:num_slides]):
            img = _gradient(W, H)
            d = ImageDraw.Draw(img)
            d.rectangle([0, 0, W, 100], fill=accent_rgb)
            d.text((40, 30), title[:80], font=_load_font(40), fill=(255, 255, 255))
            max_w = W - 220
            y = 140
            for it in chunk_items:
                font = _load_font(it["size"])
                sw = 1 if it.get("bold") else 0
                for line in _wrap(font, it["text"], max_w)[:4]:
                    d.text((48, y), line, font=font, fill=it["color"],
                           stroke_width=sw, stroke_fill=it["color"])
                    y += it["size"] + 8
                    if y > H - 160:
                        break
                y += 6
            if idx == len(chunks) - 1 and photos:
                gx, gy = 48, H - 110
                for path in photos[:_MAX_PHOTOS]:
                    try:
                        with Image.open(path) as pim:
                            pim = pim.convert("RGB")
                            pim.thumbnail((150, 95))
                            img.paste(pim, (gx, gy))
                            gx += 160
                    except Exception:  # noqa: BLE001
                        continue
            if idx == len(chunks) - 1:
                d.text((40, H - 40), AUTHORS_SIGNATURE,
                       font=_load_font(26), fill=accent_rgb)
            preview_group.append(img)
    else:
        # Документ: страницы с соотношением ~ A4. «Печатаем» строки со
        # своими шрифтами на страницы, пока не кончится место (~H-140).
        W, H = 800, 1130
        margin = 48
        max_w = W - 2 * margin
        pages = []
        page_lines = []

        ycur = 0

        def flush_page():
            nonlocal ycur
            if page_lines:
                pages.append(list(page_lines))
                page_lines.clear()
            ycur = 0

        def add_item(item):
            nonlocal ycur
            font = _load_font(item["size"])
            bold = item.get("bold") or False
            lines = _wrap(font, item["text"], max_w) or [""]
            lh = item["size"] + 8
            need = item.get("space_before", 6) + len(lines) * lh
            if page_lines and ycur + need > H - 140:
                flush_page()
            ycur += item.get("space_before", 6)
            for ln in lines:
                page_lines.append((ln, font, item["color"], bold))
                ycur += lh

        for item in items:
            add_item(item)
        flush_page()
        if not pages:
            pages = [[]]

        pages = pages[:_MAX_PREVIEW_IMAGES]
        for idx, pg in enumerate(pages):
            img = _gradient(W, H)
            d = ImageDraw.Draw(img)
            d.rectangle([0, 0, W, 84], fill=accent_rgb)
            d.text((margin, 26), title[:80], font=_load_font(34), fill=(255, 255, 255))
            y = 120
            for ln, font, color, bold in pg:
                d.text((margin, y), ln, font=font, fill=color,
                       stroke_width=(1 if bold else 0), stroke_fill=color)
                y += font.size + 8
                if y > H - 130:
                    break
            # Миниатюры фото в конце документа — как в реальном файле.
            if photos and idx == len(pages) - 1:
                thumb_x, thumb_y = margin, min(y + 30, H - 150)
                for path in photos[:_MAX_PHOTOS]:
                    try:
                        with Image.open(path) as pim:
                            pim = pim.convert("RGB")
                            pim.thumbnail((150, 110))
                            img.paste(pim, (thumb_x, thumb_y))
                            thumb_x += 160
                            if thumb_x > W - 200:
                                thumb_x = margin
                                thumb_y -= 120
                    except Exception:  # noqa: BLE001
                        continue
            d.text((margin, H - 44), AUTHORS_SIGNATURE,
                   font=_load_font(20), fill=accent_rgb)
            preview_group.append(img)

    return preview_group


async def _generate_preview(update, fsm_context, fmt: str, extra: str, font: str, bg: str, text: str) -> None:
    """Собирает PNG-превью страниц/слайдов и отправляет их группой."""
    data = await fsm_context.get_data()
    question = data.get("export_question") or ""
    photos = list(data.get("export_photos") or [])
    slide_count = data.get("export_slides")
    custom_title = (data.get("export_title") or "").strip()

    await update.callback_answer(text="Превью")
    await update.edit_message("🖼 Готовлю превью...")

    # Заголовок/дополнение считаем так же, как при сборке файла.
    try:
        title, extra_block = await asyncio.wait_for(
            asyncio.to_thread(_prepare_extra, extra, question, text, custom_title),
            timeout=_EXTRA_AI_TIMEOUT_SECONDS,
        )
    except asyncio.TimeoutError:
        title = custom_title or markdown_to_plain(question or "Ответ ИИ").strip() or "Ответ ИИ"
        extra_block = ""

    try:
        images = await asyncio.to_thread(
            _render_preview_images, bg, font, title, extra_block,
            text, photos, slide_count, fmt,
        )
    except Exception as e:  # noqa: BLE001
        print(f"EXPORT: preview error: {e}")
        images = []

    if not images:
        await update.edit_message(
            "❌ Не удалось собрать превью. Можно просто собрать файл — "
            "кнопкой ниже.",
        )
        await _show_final_menu(update, fsm_context, question, extra)
        return

    media_inputs = []
    for num, image in enumerate(images, 1):
        buf = io.BytesIO()
        image.save(buf, "PNG")
        media_inputs.append(
            BufferedInputFile.image(buf.getvalue(), f"preview_{num}.png")
        )

    # Группа картинок-превью + отдельное сообщение с кнопками действий.
    # send_media со списком шлёт альбом (нужно ≥2 фото) — одну картинку
    # отправляем одиночной.
    if len(media_inputs) > 1:
        await update.send_media(
            media=media_inputs,
            text="👁 Предпросмотр (макет документа):",
        )
    else:
        await update.send_media(
            media=media_inputs[0],
            text="👁 Предпросмотр (макет документа):",
        )
    kb = KeyboardBuilder()
    kb.add_callback(text="📦 Собрать файл", payload="export_build")
    kb.add_callback(text="🖼 Изменить фото", payload="export_photo_step")
    kb.add_callback(text="⬅️ Назад", payload=f"export_extra_{extra}")
    kb.add_callback(text="❌ Отмена", payload="export_cancel")
    await update.answer_text(
        "Всё нравится? Собери файл или измени фото.",
        keyboard=kb.adjust(1).build(),
    )


async def _generate_and_send(
    update: MessageCallback,
    fsm_context,
    fmt: str,
    extra: str,
    font: str,
    bg: str,
    slide_count: int | None,
    photos: list | None = None,
) -> None:
    """Генерирует документ и отправляет его пользователю.

    Вызывается после выбора «что добавить» (для PDF/Word/RTF) и после
    выбора числа слайдов (для PowerPoint). photos — локальные пути к
    скачанным фотографиям (их вставляет сборщик нужного формата).
    """
    data = await fsm_context.get_data()
    text = data.get("export_text") or ""
    question = data.get("export_question") or ""
    custom_title = (data.get("export_title") or "").strip()

    # Сообщение «готовим» — сборка файла занимает время.
    await update.callback_answer(text="Готовлю")
    await update.edit_message("⚙️ Собираю документ...")

    # Если выбрано дополнение через ИИ — генерируем его (в потоке).
    # Жёстко ограничиваем ожидание (см. _EXTRA_AI_TIMEOUT_SECONDS): локальная
    # модель может «задуматься» на минуты, и без потолка пользователь навсегда
    # завис бы на «⚙️ Собираю документ...». При таймауте подставляем безопасные
    # значения — заголовок из вопроса, а дополнение пропускаем.
    try:
        title, extra_block = await asyncio.wait_for(
            asyncio.to_thread(_prepare_extra, extra, question, text, custom_title),
            timeout=_EXTRA_AI_TIMEOUT_SECONDS,
        )
    except asyncio.TimeoutError:
        # ИИ не ответил за отведённое время — собираем документ без его помощи.
        title = custom_title or markdown_to_plain(question or "Ответ ИИ").strip() or "Ответ ИИ"
        extra_block = ""

    # Сборка документа (блокирующая работа с файлами) — тоже в потоке.
    try:
        path, filename = await asyncio.to_thread(
            _build_file, fmt, text, title, extra_block, font, bg, slide_count, photos
        )
    except Exception as e:  # noqa: BLE001
        print(f"Export error: {e}")
        back = KeyboardBuilder()
        back.add_callback(text="← В меню ИИ", payload="menu_ai")
        back.add_callback(text="← Главное меню", payload="menu_main")
        await update.edit_message(
            "❌ Не удалось создать файл.\n"
            "Попробуй ещё раз или выбери другой формат.",
            keyboard=back.adjust(1).build(),
        )
        return

    # Читаем байты файла и отправляем документом через send_media.
    try:
        with open(path, "rb") as fh:
            file_data = fh.read()
    except OSError as e:
        print(f"Export read error: {e}")
        file_data = None

    # Убираем временный файл (и фоновое изображение, если создалось).
    try:
        os.remove(path)
    except OSError:
        pass

    if file_data is None:
        return

    await update.send_media(
        media=BufferedInputFile.file(file_data, filename),
        text="📎 Готово! Ответ сохранён.",
    )

    # Чистим временные данные мастера — следующий экспорт начнётся заново.
    await fsm_context.update_data(
        export_text=None,
        export_question=None,
        export_fmt=None,
        export_font=None,
        export_bg=None,
        export_extra=None,
        export_slides=None,
        export_title=None,
    )
    # Фото на диске больше не нужны — удаляем temp-папку и вычищаем FSM.
    await _cleanup_photos(fsm_context)


# ============================================================
# ПОДГОТОВКА КОНТЕНТА (иИ может дополнять текст)
# ============================================================

def _prepare_extra(extra: str, question: str, text: str, custom_title: str = "") -> tuple[str, str]:
    """Готовит (заголовок, дополнение) для документа по выбору пользователя.

    Если пользователь ввёл своё название (custom_title) — оно имеет приоритет
    над автоматическим. Если выбрано Дополнение через ИИ — вызывает локальную
    модель (handlers.ai._ask_ai) и возвращает сгенерированный блок. И заголовок,
    и дополнение очищаются от markdown-пометок — в шапке документа и в
    подписи лишние ** и ### ни к чему.
    """
    title = (
        custom_title or markdown_to_plain(question or "Ответ ИИ")
    ).strip() or "Ответ ИИ"
    extra_block = ""

    # «Ничего не добавлять» — ИИ не нужен, заголовок из вопроса/свой.
    if extra == "none" or not text:
        return title, extra_block

    # Текст для модели обрезаем, чтобы не отправлять гигантские куски.
    excerpt = text[:3000]

    if extra == "title":
        # ИИ придумывает короткий заголовок, ТОЛЬКО если нет своего.
        if not custom_title:
            answer, _err = _ask_ai(
                "Ты — редактор. Верни ТОЛЬКО один короткий заголовок "
                "(до 8 слов), без кавычек, точек в конце и пояснений.",
                f"Придумай заголовок к этому тексту:\n\n{excerpt}",
            )
            if answer:
                title = markdown_to_plain(answer).strip() or title

    elif extra == "summary":
        # ИИ пишет краткий вывод к тексту.
        answer, _err = _ask_ai(
            "Ты — редактор. Верни ТОЛЬКО сам вывод (3-5 предложений), "
            "без слов «вывод:» и без пояснений.",
            f"Напиши краткий вывод к этому тексту:\n\n{excerpt}",
        )
        if answer:
            extra_block = markdown_to_plain(answer).strip()

    elif extra == "keywords":
        # ИИ выделяет ключевые слова.
        answer, _err = _ask_ai(
            "Ты — редактор. Верни ТОЛЬКО 5-7 ключевых слов через запятую, "
            "без пояснений и до/после текста.",
            f"Выдели ключевые слова этого текста:\n\n{excerpt}",
        )
        if answer:
            extra_block = markdown_to_plain(answer).strip()

    return title, extra_block


# ============================================================
# СБОРКА ФАЙЛОВ (PDF / DOCX / RTF / PPTX)
# ============================================================

def _font_helper(font_key: str) -> tuple[str, str | None]:
    """Возвращает (имя шрифта для библиотек, путь к ttf или None)."""
    name, ttf = _FONTS[font_key]
    path = os.path.join(r"C:\Windows\Fonts", ttf)
    return name, (path if os.path.exists(path) else None)


def _palette(bg_key: str) -> tuple[str, tuple, tuple]:
    """Возвращает (hex-цвет фона, цвет текста RGB, цвет заголовка RGB)."""
    _label, (top, _bottom) = _BACKGROUNDS[bg_key]
    hex_fill = "{:02X}{:02X}{:02X}".format(*top)
    if bg_key == "dark":
        fg = (240, 240, 245)    # светлый текст на тёмном фоне
        accent = (255, 255, 255)
    else:
        fg = (40, 40, 40)       # тёмный текст на светлом фоне
        accent = (20, 80, 160)
    return hex_fill, fg, accent


def _make_bg_image(bg_key: str, width: int, height: int) -> str:
    """Рисует Pillow вертикальный градиент-фон и возвращает путь к PNG."""
    from PIL import Image

    _label, (top, bottom) = _BACKGROUNDS[bg_key]
    img = Image.new("RGB", (width, height))
    pix = img.load()
    for y in range(height):
        t = y / max(1, height - 1)
        r = int(top[0] + (bottom[0] - top[0]) * t)
        g = int(top[1] + (bottom[1] - top[1]) * t)
        b = int(top[2] + (bottom[2] - top[2]) * t)
        for x in range(width):
            pix[x, y] = (r, g, b)
    fd, path = tempfile.mkstemp(suffix=".png", prefix="maxo_bg_")
    os.close(fd)
    img.save(path, "PNG")
    return path


def _temp_path(suffix: str, prefix: str) -> str:
    fd, path = tempfile.mkstemp(suffix=suffix, prefix=prefix)
    os.close(fd)
    return path


def _build_file(
    fmt: str, text: str, title: str, extra_block: str,
    font_key: str, bg_key: str, slide_count: int | None = None,
    photos: list | None = None,
) -> tuple[str, str]:
    """Собирает документ нужного формата, возвращает (путь, имя файла).

    Сюда никогда не попадают пользовательские строки-«форматы»: fmt проверен
    в обработчике (есть в _FORMATS). slide_count учитывается только для
    PowerPoint (число слайдов; None — один слайд). photos — локальные пути
    к скачанным фотографиям; сборщик вставляет их перед подписью авторов.
    """
    font_name, ttf_path = _font_helper(font_key)
    hex_fill, fg_rgb, accent_rgb = _palette(bg_key)

    ts = int(time.time())
    if fmt == "pdf":
        path = _temp_path(".pdf", "maxo_export_")
        _build_pdf(path, text, title, extra_block, font_name, ttf_path, bg_key, fg_rgb, accent_rgb, photos)
        return path, f"Ответ_ИИ_{ts}.pdf"

    if fmt == "word":
        path = _temp_path(".docx", "maxo_export_")
        _build_docx(path, text, title, extra_block, font_name, hex_fill, fg_rgb, accent_rgb, photos)
        return path, f"Ответ_ИИ_{ts}.docx"

    if fmt == "rtf":
        path = _temp_path(".rtf", "maxo_export_")
        _build_rtf(path, text, title, extra_block, font_name, hex_fill, fg_rgb, accent_rgb, photos)
        return path, f"Ответ_ИИ_{ts}.rtf"

    # fmt == "pptx"
    path = _temp_path(".pptx", "maxo_export_")
    _build_pptx(path, text, title, extra_block, font_name, bg_key, fg_rgb, accent_rgb, slide_count, photos)
    return path, f"Ответ_ИИ_{ts}.pptx"


# ─────────────────────────── PDF (ReportLab + Pillow) ───────────────────────────

def _build_pdf(path, text, title, extra_block, font_name, ttf_path, bg_key, fg_rgb, accent_rgb, photos=None):
    from reportlab.lib.colors import HexColor
    from reportlab.lib.enums import TA_CENTER
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.lib.utils import ImageReader
    from reportlab.lib.units import mm
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont
    from reportlab.platypus import Image, Paragraph, SimpleDocTemplate, Spacer

    # Регистрируем выбранный шрифт. Если его TTF нет в системе, берём
    # запасной Arial/Segoe UI: встроенный Helvetica не умеет кириллицу
    # и роняет сборку PDF на русском тексте.
    pdf_font = "Helvetica"
    font_candidates = [
        ttf_path,
        os.path.join(r"C:\Windows\Fonts", "arial.ttf"),
        os.path.join(r"C:\Windows\Fonts", "segoeui.ttf"),
    ]
    for candidate in font_candidates:
        if not candidate or not os.path.exists(candidate):
            continue
        try:
            pdfmetrics.registerFont(TTFont("MaxoFont", candidate))
            pdf_font = "MaxoFont"
            break
        except Exception:  # noqa: BLE001
            continue

    # Фон рисует Pillow — кладём PNG на каждую страницу.
    bg_path = _make_bg_image(bg_key, int(A4[0]), int(A4[1]))

    def _on_page(canvas, _doc):
        canvas.saveState()
        canvas.drawImage(
            bg_path, 0, 0, width=A4[0], height=A4[1],
            preserveAspectRatio=False, anchor="s",
        )
        canvas.restoreState()

    hex_fg = HexColor("#{:02X}{:02X}{:02X}".format(*fg_rgb))
    hex_accent = HexColor("#{:02X}{:02X}{:02X}".format(*accent_rgb))

    title_style = ParagraphStyle(
        "Title", fontName=pdf_font, fontSize=20, leading=24,
        textColor=hex_accent, spaceAfter=10,
    )
    body_style = ParagraphStyle(
        "Body", fontName=pdf_font, fontSize=12, leading=16,
        textColor=hex_fg, spaceAfter=8,
    )
    extra_style = ParagraphStyle(
        "Extra", fontName=pdf_font, fontSize=12, leading=16,
        textColor=hex_fg, spaceAfter=8, leftIndent=0,
        backColor=HexColor("#F0F2F5"), borderPadding=4,
    )
    auth_style = ParagraphStyle(
        "Auth", fontName=pdf_font, fontSize=12, leading=16,
        textColor=hex_fg, alignment=TA_CENTER, spaceBefore=14,
    )

    doc = SimpleDocTemplate(
        path, pagesize=A4,
        leftMargin=18 * mm, rightMargin=18 * mm,
        topMargin=18 * mm, bottomMargin=18 * mm,
    )

    story: list = [
        Paragraph(_esc_pdf(title), title_style),
        Spacer(1, 6),
    ]
    for block in parse_blocks(text):
        kind = block["kind"]
        if kind == "h":
            lvl = block.get("level", 1)
            hs = ParagraphStyle(
                "H" + str(lvl), fontName=pdf_font,
                fontSize={1: 20, 2: 16, 3: 14}.get(lvl, 14),
                leading={1: 26, 2: 20, 3: 18}.get(lvl, 18),
                textColor=hex_accent, spaceAfter=8, spaceBefore=12,
            )
            story.append(Paragraph(reportlab_inline(block["spans"]), hs))
        elif kind == "ul":
            for item in block["items"]:
                story.append(Paragraph(
                    "•  " + reportlab_inline(item), body_style,
                ))
        elif kind == "ol":
            for idx, item in enumerate(block["items"], 1):
                story.append(Paragraph(
                    f"{idx}.  " + reportlab_inline(item), body_style,
                ))
        elif kind == "code":
            story.append(Paragraph(
                _esc_pdf(block["text"]).replace("\n", "<br/>").replace("\t", "  "),
                extra_style,
            ))
        elif kind == "quote":
            story.append(Paragraph(
                "<i>«" + reportlab_inline(block["spans"]) + "»</i>",
                body_style,
            ))
        elif kind == "hr":
            story.append(Spacer(1, 6))
        elif kind == "p":
            story.append(Paragraph(reportlab_inline(block["spans"]), body_style))
    if extra_block:
        story.append(Spacer(1, 4))
        story.append(Paragraph(f"<b>{_esc_pdf('Дополнение')}</b>", extra_style))
        story.append(Paragraph(_esc_pdf(extra_block), extra_style))

    # Фото: вставляем перед подписью авторов. Размер подгоняем по
    # пропорциям, чтобы картинка не вылезала за поля страницы.
    if photos:
        draw_width = A4[0] - 36 * mm
        for ph_path in photos or ():
            try:
                ir = ImageReader(ph_path)
                iw, ih = ir.getSize()
                ratio = ih / iw
                w = min(draw_width, iw)
                h = w * ratio
                if h > 0.6 * A4[1]:
                    h = 0.6 * A4[1]
                    w = h / ratio
                story.append(Spacer(1, 6))
                story.append(Image(ph_path, width=w, height=h))
            except Exception:  # noqa: BLE001
                continue   # битую картинку пропускаем, остальные вставляем

    story.append(Spacer(1, 10))
    story.append(Paragraph(_esc_pdf(AUTHORS_SIGNATURE), auth_style))

    doc.build(story, onFirstPage=_on_page, onLaterPages=_on_page)
    try:
        os.remove(bg_path)
    except OSError:
        pass


def _esc_pdf(text: str) -> str:
    """Экранирует спецсимволы XML для Paragraph (reportlab)."""
    return (
        text.replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
    )


# ─────────────────────────── Word/DOCX (python-docx) ───────────────────────────

def _build_docx(path, text, title, extra_block, font_name, hex_fill, fg_rgb, accent_rgb, photos=None):
    from docx import Document
    from docx.enum.text import WD_ALIGN_PARAGRAPH
    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn
    from docx.shared import Inches, Pt, RGBColor

    doc = Document()

    # Шрифт по умолчанию для всего документа (включая кириллицу).
    style = doc.styles["Normal"]
    style.font.name = font_name
    style.font.size = Pt(12)
    rpr = style.element.get_or_add_rPr()
    rfonts = rpr.get_or_add_rFonts()
    rfonts.set(qn("w:eastAsia"), font_name)

    fg = RGBColor(*fg_rgb)
    accent = RGBColor(*accent_rgb)

    def _add_para(text_segment, size, bold, color, align=None, italic=False):
        p = doc.add_paragraph()
        if align is not None:
            p.alignment = align
        run = p.add_run(text_segment)
        run.font.name = font_name
        run._element.rPr.rFonts.set(qn("w:eastAsia"), font_name)
        run.font.size = Pt(size)
        run.font.bold = bold
        run.font.italic = italic
        run.font.color.rgb = color
        # «Фон» параграфа — заливка цветом (цвет из выбранного пользователем).
        shd = OxmlElement("w:shd")
        shd.set(qn("w:val"), "clear")
        shd.set(qn("w:auto"), "1")
        shd.set(qn("w:fill"), hex_fill)
        p._p.get_or_add_pPr().append(shd)
        return p

    def _rich_paragraph(size, color, align=None):
        p = doc.add_paragraph()
        if align is not None:
            p.alignment = align
        shd = OxmlElement("w:shd")
        shd.set(qn("w:val"), "clear")
        shd.set(qn("w:auto"), "1")
        shd.set(qn("w:fill"), hex_fill)
        p._p.get_or_add_pPr().append(shd)
        return p

    def _add_run(p, text_segment, size, bold=False, italic=False,
                 color=None, mono=False):
        # Переносы строк внутри блока кода — настоящие разрывы <w:br/>.
        for i, part in enumerate(text_segment.split("\n")):
            run = p.add_run(part)
            run.font.name = "Courier New" if mono else font_name
            run._element.rPr.rFonts.set(qn("w:eastAsia"), run.font.name)
            run.font.size = Pt(size)
            run.font.bold = bold
            run.font.italic = italic
            run.font.color.rgb = color or fg
            if i < text_segment.count("\n"):
                run.add_break()
        return run

    def _spans(p, spans, size, color=None, force_italic=False):
        for st, t in spans:
            _add_run(
                p, t, size,
                bold=("b" in st),
                italic=("i" in st) or force_italic,
                color=color,
                mono=(st == "c"),
            )

    _add_para(title, 18, True, accent)
    for block in parse_blocks(text):
        kind = block["kind"]
        if kind == "h":
            lvl = block.get("level", 1)
            hsize = {1: 16, 2: 14, 3: 13}.get(lvl, 13)
            p = _rich_paragraph(hsize, accent)
            for st, t in block["spans"]:
                _add_run(p, t, hsize,
                         bold=("b" in st) or lvl <= 2,
                         italic=("i" in st),
                         color=accent, mono=(st == "c"))
        elif kind == "ul":
            for item in block["items"]:
                p = _rich_paragraph(12, fg)
                _add_run(p, "•  ", 12, color=accent)
                _spans(p, item, 12)
        elif kind == "ol":
            for idx, item in enumerate(block["items"], 1):
                p = _rich_paragraph(12, fg)
                _add_run(p, f"{idx}.  ", 12, color=accent)
                _spans(p, item, 12)
        elif kind == "code":
            p = _rich_paragraph(11, fg)
            _add_run(p, block["text"], 11, mono=True)
        elif kind == "quote":
            p = _rich_paragraph(12, fg)
            _spans(p, block["spans"], 12, force_italic=True)
        elif kind == "p":
            p = _rich_paragraph(12, fg)
            _spans(p, block["spans"], 12)
    if extra_block:
        _add_para("Дополнение", 13, True, accent)
        _add_para(extra_block, 12, False, fg, italic=True)

    # Фото вставляем до подписи авторов (ширина ограничена 5.5 дюйма).
    for ph in photos or ():
        try:
            doc.add_picture(ph, width=Inches(5.5))
        except Exception:  # noqa: BLE001
            continue
    _add_para(AUTHORS_SIGNATURE, 13, True, fg, align=WD_ALIGN_PARAGRAPH.CENTER)

    doc.save(path)


# ─────────────────────────── RTF (собственный генератор) ───────────────────────────

def _rtf_escape(text: str) -> str:
    """Экранирует текст в формат RTF (служебные символы + юникод \\uN?)."""
    out = []
    for ch in text:
        o = ord(ch)
        if ch == "\\":
            out.append("\\\\")
        elif ch == "{":
            out.append("\\{")
        elif ch == "}":
            out.append("\\}")
        elif o <= 127:
            out.append(ch)
        else:
            n = o - 65536 if o > 32767 else o
            out.append(f"\\u{n}?")
    return "".join(out)


def _rtf_spans(spans: list) -> str:
    """Отрисовывает инлайн-спаны markdown в RTF-подсветку."""
    out = []
    for st, t in spans:
        s = _rtf_escape(t)
        if not s:
            continue
        if st == "b":
            out.append("{\\b " + s + "}")
        elif st == "i":
            out.append("{\\i " + s + "}")
        elif st == "bi":
            out.append("{\\b\\i " + s + "}")
        elif st == "c":
            out.append("{\\f1 " + s + "}")
        else:
            out.append(s)
    return "".join(out)


def _build_rtf(path, text, title, extra_block, font_name, hex_fill, fg_rgb, accent_rgb, photos=None):
    """Собирает RTF-файл вручную: \\colortbl для цветов, \\chcbpat для фона."""
    # Цвета: индекс 0 — авто, 1 — текст, 2 — фон, 3 — акцент.
    fg_str = "{};".format(_rtf_rgb(fg_rgb))
    bg_str = "{};".format(_rtf_rgb_hex(hex_fill))
    ac_str = "{};".format(_rtf_rgb(accent_rgb))

    lines = []
    lines.append(r"{\rtf1\ansi\deff0")
    lines.append(r"{\fonttbl{\f0 " + _rtf_escape(font_name) + r";}{\f1 Courier New;}}")
    lines.append(r"{\colortbl;" + fg_str + bg_str + ac_str + "}")
    lines.append(r"\f0\fs24")

    # Заголовок (жирный, акцентного цвета, с фоном).
    lines.append(r"\pard\ql\cf3\b\fs36" + r"\chcbpat2 " + _rtf_escape(title) + r"\b0\chcbpat0\par")
    lines.append(r"\fb0\fs24\par\fs24")  # сброс форматирования, пустая строка

    # Тело: markdown-блоки с инлайн-подсветкой.
    for block in parse_blocks(text):
        kind = block["kind"]
        if kind == "h":
            lvl = block.get("level", 1)
            hsize = {1: 40, 2: 32, 3: 28}.get(lvl, 28)
            lines.append(
                rf"\pard\ql\cf3\b\fs{hsize}\chcbpat2 "
                + _rtf_spans(block["spans"])
                + r"\b0\fs24\chcbpat0\par"
            )
        elif kind in ("ul", "ol"):
            for idx, item in enumerate(block["items"], 1):
                marker = f"{idx}.  " if kind == "ol" else "•  "
                lines.append(
                    r"\pard\ql\cf1\li360\chcbpat2 "
                    + _rtf_escape(marker)
                    + _rtf_spans(item)
                    + r"\chcbpat0\par"
                )
        elif kind == "code":
            lines.append(
                r"\pard\ql\cf1\f1\chcbpat2 "
                + _rtf_escape(block["text"]).replace("\n", r"\line ")
                + r"\f0\chcbpat0\par"
            )
        elif kind == "quote":
            lines.append(
                r"\pard\ql\cf1\li360\ri360\chcbpat2 "
                + _rtf_escape("«")
                + r"{\i " + _rtf_spans(block["spans"]) + r"}"
                + _rtf_escape("»")
                + r"\chcbpat0\par"
            )
        elif kind == "p":
            lines.append(
                r"\pard\ql\cf1\chcbpat2 "
                + _rtf_spans(block["spans"])
                + r"\chcbpat0\par"
            )
        lines.append("")

    # Дополнение от ИИ.
    if extra_block:
        lines.append(r"\cf3\b Дополнение\b0\par")
        lines.append(r"\cf1\chcbpat2 " + _rtf_escape(extra_block) + r"\chcbpat0\par\i0")
        lines.append("")

    # Фото вставляем как PNG-картинки (\pict\pngblip, байты в hex).
    # Современные редакторы RTF (Word, LibreOffice) умеют его показывать.
    # \picwGoal задаёт ширину в твипах (6 дюймов), чтобы большие фото не
    # «распирали» страницу шире полей.
    for ph in photos or ():
        try:
            from PIL import Image

            # RTF умеет встраивать только PNG (\pngblip). Пересохраняем
            # картинку в PNG в памяти: исходный файл может быть JPEG/WebP,
            # иначе Word/LibreOffice покажет «битое» изображение.
            with Image.open(ph) as pim:
                pim = pim.convert("RGB")
                pw, phh = pim.size
                buf = io.BytesIO()
                pim.save(buf, "PNG")
            data = buf.getvalue()
            goal_w = int(6 * 1440)
            goal_h = int(goal_w * phh / max(1, pw))
            lines.append(
                "\\pard\\qc{\\pict\\pngblip"
                f"\\picw{pw}\\pich{phh}"
                f"\\picwGoal{goal_w}\\pichGoal{goal_h} "
                + binascii.hexlify(data).decode("ascii")
                + "}\\par"
            )
        except Exception:  # noqa: BLE001
            continue

    # Подпись авторов — по центру, жирная.
    lines.append(r"\pard\qc\cf3\b " + _rtf_escape(AUTHORS_SIGNATURE) + r"\b0\cf0\par")
    lines.append("}")

    with open(path, "w", encoding="ascii", errors="replace") as fh:
        fh.write("\n".join(lines))


def _rtf_rgb(rgb: tuple) -> str:
    """Цвет (r,g,b) → RTF-запись \\redN\\greenN\\blueN."""
    return r"\red%d\green%d\blue%d" % rgb


def _rtf_rgb_hex(hex_fill: str) -> str:
    """Hex-цвет → RTF-запись."""
    r = int(hex_fill[0:2], 16)
    g = int(hex_fill[2:4], 16)
    b = int(hex_fill[4:6], 16)
    return r"\red%d\green%d\blue%d" % (r, g, b)


# ─────────────────────────── PowerPoint (python-pptx + Pillow) ───────────────────────────

def _build_pptx(
    path, text, title, extra_block, font_name, bg_key,
    fg_rgb, accent_rgb, slide_count, photos=None,
):
    """Собирает PowerPoint-презентацию с N слайдами.

    Каждый слайд получает:
      • фон — градиентная картинка, нарисованная Pillow;
      • шрифт — тот, что выбрал пользователь;
      • текст — разбитый на равные части по числу слайдов.

    Слайд 1 — заголовок крупно + начало текста.
    Слайды 2..N — заголовок мелко + продолжение.
    Дополнение от ИИ и подпись «Команда КУБ-СКРИПТ» — только на последнем слайде.
    Фото расставляются по слайдам по одному (по кругу).
    """
    from pptx import Presentation
    from pptx.dml.color import RGBColor
    from pptx.util import Inches, Pt

    # 1) Разбираем markdown-текст на «строчки слайда»: заголовки, абзацы,
    #    списки, код. Каждая строчка — словарь с инлайн-спанами.
    items: list[dict] = []
    for block in parse_blocks(text):
        kind = block["kind"]
        if kind == "h":
            items.append({
                "kind": "h", "lvl": block.get("level", 1),
                "spans": block["spans"],
            })
        elif kind in ("ul", "ol"):
            for idx, item in enumerate(block["items"], 1):
                items.append({
                    "kind": kind, "num": idx, "spans": item,
                })
        elif kind == "code":
            items.append({"kind": "code", "text": block["text"]})
        elif kind == "quote":
            items.append({
                "kind": "p",
                "spans": [("", "«")] + block["spans"] + [("", "»")],
            })
        elif kind == "p":
            items.append({"kind": "p", "spans": block["spans"]})
    if not items:
        items = [{"kind": "p", "spans": [("", text.strip() or "")]}]

    if slide_count is None or slide_count <= 0:
        # Авто: ~3 строчки на слайд, мин. 1, макс. 10.
        slide_count = min(10, max(1, (len(items) + 2) // 3))
    else:
        slide_count = min(10, max(1, slide_count))

    per_slide = -(-len(items) // slide_count)  # ceil division
    chunks: list[list[dict]] = [
        items[i : i + per_slide]
        for i in range(0, len(items), per_slide)
    ]
    # Если строчек мало — добиваем пустыми слайдами.
    while len(chunks) < slide_count:
        chunks.append([])

    # 2) Создаём презентацию.
    prs = Presentation()
    prs.slide_width = Inches(13.333)
    prs.slide_height = Inches(7.5)
    blank = prs.slide_layouts[6]

    # Фон рисует Pillow ОДИН раз — он будет на каждом слайде.
    bg_path = _make_bg_image(bg_key, 1280, 720)
    fg = RGBColor(*fg_rgb)
    accent = RGBColor(*accent_rgb)

    for idx, chunk in enumerate(chunks):
        slide = prs.slides.add_slide(blank)

        # Фон — Pillow-картинка первой на каждом слайде.
        slide.shapes.add_picture(
            bg_path, 0, 0,
            width=prs.slide_width, height=prs.slide_height,
        )

        # ── Заголовок: на первом слайде крупно, на остальных — мелко ──
        is_last = idx == len(chunks) - 1
        if idx == 0:
            hdr_top, hdr_h, hdr_sz = Inches(0.35), Inches(1.1), 30
            body_top, body_h = Inches(1.6), Inches(5.0)
        else:
            hdr_top, hdr_h, hdr_sz = Inches(0.3), Inches(0.6), 20
            body_top, body_h = Inches(1.0), Inches(5.5)
        # На последнем слайде место для текста ужимаем, если будут фото —
        # чтобы галерея не наезжала на абзацы.
        if is_last and photos:
            body_h = Inches(4.3)

        hdr_box = slide.shapes.add_textbox(
            Inches(0.6), hdr_top, Inches(12.1), hdr_h,
        )
        hdr_tf = hdr_box.text_frame
        hdr_tf.word_wrap = True
        run_hdr = hdr_tf.paragraphs[0].add_run()
        run_hdr.text = title
        run_hdr.font.name = font_name
        run_hdr.font.size = Pt(hdr_sz)
        run_hdr.font.bold = (idx == 0)
        run_hdr.font.color.rgb = accent

        # ── Текст слайда ──
        body_box = slide.shapes.add_textbox(
            Inches(0.6), body_top, Inches(12.1), body_h,
        )
        body_tf = body_box.text_frame
        body_tf.word_wrap = True

        def _add_run(p, t, size, bold=False, italic=False, mono=False, color=None):
            run = p.add_run()
            run.text = t
            run.font.name = "Courier New" if mono else font_name
            run.font.size = Pt(size)
            run.font.bold = bold
            run.font.italic = italic
            run.font.color.rgb = color or fg
            return run

        first_p = True
        for it in chunk:
            para = body_tf.paragraphs[0] if first_p else body_tf.add_paragraph()
            first_p = False
            kind = it["kind"]
            if kind == "h":
                hsz = {1: 22, 2: 19, 3: 17}.get(it.get("lvl", 1), 17)
                for st, t in it["spans"]:
                    _add_run(
                        para, t, hsz,
                        bold=("b" in st) or it.get("lvl", 3) <= 2,
                        italic=("i" in st),
                        color=accent,
                    )
                para.space_after = Pt(4)
            elif kind in ("ul", "ol"):
                marker = f"{it['num']}.  " if kind == "ol" else "•  "
                _add_run(para, marker, 16, color=accent)
                for st, t in it["spans"]:
                    _add_run(para, t, 16,
                             bold=("b" in st), italic=("i" in st),
                             mono=(st == "c"))
            elif kind == "code":
                lines = it["text"].split("\n")
                for j, ln in enumerate(lines):
                    _add_run(para, ln, 14, mono=True)
                    if j < len(lines) - 1:
                        para.add_line_break()
            else:  # p / quote
                for st, t in it["spans"]:
                    _add_run(para, t, 16,
                             bold=("b" in st), italic=("i" in st),
                             mono=(st == "c"))

        # ── Дополнение от ИИ — только на последнем слайде ──
        if is_last and extra_block:
            para = body_tf.add_paragraph()
            run = para.add_run()
            run.text = "Дополнение: " + extra_block
            run.font.name = font_name
            run.font.size = Pt(15)
            run.font.italic = True
            run.font.color.rgb = fg

        # ── Фото: галерея на последнем слайде (над подписью авторов) ──
        if is_last and photos:
            from PIL import Image as PILImage

            ph_x = Inches(0.6)
            ph_y = Inches(5.4)
            ph_h = Inches(1.0)
            for ph in photos:
                try:
                    with PILImage.open(ph) as pim:
                        w, h = pim.size
                    aspect = h / max(1, w)
                    pw = min(Inches(1.9), Inches(1.0 * max(aspect, 0.5)))
                    slide.shapes.add_picture(ph, ph_x, ph_y, width=pw, height=ph_h)
                    ph_x += pw + Inches(0.2)
                except Exception:  # noqa: BLE001
                    continue

        # ── Подпись авторов — только на последнем слайде ──
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

    prs.save(path)
    try:
        os.remove(bg_path)
    except OSError:
        pass

