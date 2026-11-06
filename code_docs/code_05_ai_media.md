# Часть 5. handlers/ai.py, handlers/media.py

---

## handlers/ai.py — ИИ-функции

Файл (1083 строки): раздел «ИИ» — меню с режимами (вопрос по уроку, расписание, тест,
домашка, набросок, план урока, история учеников), лимиты, системные промпты, вызов
локального LM Studio (OpenAI-совместимый API).

### `get_ai_menu_keyboard(user_id) -> KeyboardBuilder` (строка 108)

Собирает клавиатуру меню ИИ с учётом роли. Кнопки:
- `ai_question` — «❓ Вопрос по уроку» (режим `question`);
- `ai_schedule` — «📅 Расписание дня» (`schedule`);
- `ai_test` — «📝 Создать тест» (`test`);
- `ai_homework` — «📚 Помощь с домашкой» (`homework`);
- для учителя/модератора: `ai_presentation` («📝 Набросок текста»), `ai_topic`
  («📖 План урока по теме»), `ai_students_history` («👨🏫 История учеников»);
- ученику: `ai_my_history` («📊 Моя история»);
- `menu_main` — «← Назад в меню».

Каждая кнопка при нажатии кладёт `mode` во временные данные FSM, а текст-запрос
ожидается обработчиком `handle_ai_text` (состояние `wait_question`).

### `handle_ai_menu(update, fsm_context)` (строка 180)

Единый обработчик всех кнопок раздела ИИ (`CallbackPayload("menu_ai", prefixes=("ai_",))`).

**Вход в раздел (`menu_ai`):**
```python
await fsm_context.clear()
if not db.check_ai_limit(user_id):
    await update.callback_answer(text="Лимит")
    await update.edit_message(
        text="❌ Лимит запросов к ИИ на сегодня исчерпан!...",
        keyboard=get_ai_menu_keyboard(user_id).build(),
    )
    return
used = db.get_ai_usage(user_id)
limit = db.get_user_field(user_id, "ai_daily_limit") or 50
await update.callback_answer(text="ИИ")
await update.edit_message(
    text=f"🤖 Раздел ИИ\n\nИспользовано запросов сегодня: {used}/{limit}\n\nВыбери, чем я могу помочь:",
    keyboard=get_ai_menu_keyboard(user_id).build(),
)
```
- `fsm_context.clear()` — сброс прошлого режима (`mode`) и состояния.
- `db.check_ai_limit(user_id)` — если лимит исчерпан — сообщение + меню (без состояния).
- Иначе — текст со счётчиком `used/limit` через `get_ai_usage` / `ai_daily_limit`.
- `update.callback_answer(text=...)` — короткое «всплывающее» сообщение на кнопке;
  `update.edit_message(...)` — заменяет сообщение с клавиатурой на новое.

**Старт текстового ввода.** Словарь `mode_for_payload` (строка 258) сопоставляет payload
кнопки с кортежем `(mode, title, hint)`, например:
`"ai_question": ("question", "❓ Задай свой вопрос по уроку.", "Напиши что тебе непонятно...")`.
Обработчик по нажатию:
- вызывает `fsm_context.update_data(mode=mode)` — запоминает режим;
- `fsm_context.set_state(AiStates.wait_question)` — «ждём текст»;
- `update.edit_message(title, hint)` — просит пользователя написать вопрос.

**История / логи:**
- `ai_students_history` — список учеников; по кнопке ученика — его логи ИИ
  (`db.get_ai_logs_for_user`, красиво через `_format_logs`).
- `ai_my_history` — собственная история ученика.
- `menu_ai_logs` — общие логи ИИ (для учителя/модератора).

### `handle_ai_text(update, fsm_context)` (строка 558)

Обработчик текстового запроса к ИИ (`StateFilter(AiStates)`).

```python
user_id = get_user_id(update)
text = update.text or ""
if not user_id: return
if not db.user_exists(user_id) or not db.is_approved(user_id) or db.is_blocked(user_id):
    return
media_attachments = [a for a in (body.attachments or [])
                     if a.type in (AttachmentType.IMAGE, VIDEO, AUDIO, FILE)]
if media_attachments and not text:
    return  # просим написать текст, кнопка «Назад»
if len(text) > AI_MAX_INPUT_LENGTH:
    return  # «слишком длинный вопрос»

data = await fsm_context.get_data()
mode = data.get("mode", "question")

if not db.check_ai_limit(user_id):
    await fsm_context.clear()
    await update.answer_text("❌ Лимит запросов к ИИ на сегодня исчерпан!")
    return

await update.answer_text("🤔 Думаю...")
system_prompt = _get_system_prompt(user_id, mode)
answer, ai_error = await asyncio.to_thread(_ask_ai, system_prompt, text)

if answer is None:
    db.save_ai_error(user_id, mode, ai_error or "...")
    from handlers.moderation import notify_moderators
    await notify_moderators(f"⚠️ Сбой ИИ у пользователя {user_id} (режим: {mode}).\nПричина: ...")
    await update.answer_text("❌ Не удалось получить ответ от ИИ.\n\nПричина: ...")
    return

db.increment_ai_usage(user_id)
db.save_ai_log(user_id, text, answer)
await fsm_context.clear()
# отправка ответа + кнопки _MODE_META («продолжить», «вернуться в меню»)
```

**Построчно.**
- Барьер: существует + одобрен + не заблокирован.
- Если пришло медиа и нет текста — просим написать текстом (модель отвечает по тексту).
- Длинный вопрос — просим сократить, **состояние FSM сохраняется**.
- `mode = data.get("mode", "question")` — режим из временных данных FSM.
- Повторная проверка лимита «в последний момент»; при провале — `clear()` + сообщение.
- `"🤔 Думаю..."` — промежуточное сообщение: пользователь видит, что бот жив.
- `asyncio.to_thread(_ask_ai, ...)` — синхронный `requests.post` уходит в отдельный поток,
  event loop не блокируется.
- Ошибка: `db.save_ai_error(...)` (лог для панели), `notify_moderators(...)` (личные
  уведомления модераторам — импорт локальный, чтобы не создавать цикл), понятный ответ
  пользователю. Состояние не сбрасывается — можно повторить сразу.
- Успех: `db.increment_ai_usage(user_id)` (+1 к расходу лимита), `db.save_ai_log(...)`
  (история запроса), сброс FSM, ответ пользователю с кнопками «Продолжить в этом же режиме»
  (`_mode_continue_callback(mode)`) и «Меню ИИ».

### Вспомогательные функции

- `_is_teacher_or_moderator(user_id)` (строка 729) — проверка роли для расширенного меню.
- `_back_keyboard(callback)` (строка 746) — кнопка «← Назад» с нужным payload.
- `_mode_continue_label/mode` (строки 773/781) — текст и payload кнопки «Продолжить»
  в зависимости от режима.
- `_format_logs(logs)` (строка 788) — превращает список логов ИИ в красивый текст:
  по каждому запросу — «вопрос → ответ» с разделителями.
- `_get_system_prompt(user_id, mode)` (строка 832) — собирает системный промпт:
  роль пользователя (ученик/учитель), включённые опции (примеры `ai_examples`, подробность
  `ai_detail`, наводящие вопросы `ai_followup`), интересы (`ai_topics`) и инструкции для
  конкретного режима (`question/schedule/test/homework/presentation/topic`), **плюс** при
  включённом `followup` добавляет в конец требование задать 2–3 наводящих вопроса.

### `_ask_ai(system_prompt, user_message) -> (answer, error)` (строка 975)

Синхронная функция вызова LM Studio (запускается через `asyncio.to_thread`).

```python
if not AI_API_KEY or "ТВОЙ" in AI_API_KEY:
    return None, "администратор ещё не настроил API-ключ ИИ"

url = "http://localhost:1234/v1/chat/completions"
data = {
    "model": AI_MODEL,
    "messages": [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_message},
    ],
    "temperature": 0.7,
    "max_tokens": AI_MAX_OUTPUT_LENGTH,
}
response = requests.post(url, headers={"Content-Type": "application/json"},
                         json=data, timeout=AI_TIMEOUT_SECONDS)

if response.status_code != 200:
    error_text = (response.text or "").strip()
    if len(error_text) > 300:
        error_text = error_text[:300] + "..."
    print(f"AI API Error: {response.status_code} - {response.text}")
    return None, f"сервер ИИ вернул ошибку (HTTP {response.status_code}): {error_text or 'без описания'}"

result = response.json()
if "choices" in result and len(result["choices"]) > 0:
    choice = result["choices"][0]
    if "message" in choice and "content" in choice["message"]:
        return choice["message"]["content"], None
```

- Заглушка ключа («ТВОЙ...») — считается «не настроено», сразу ошибка.
- Эндпоинт `http://localhost:1234/v1/chat/completions` — стандарт LM Studio.
- Тело запроса — OpenAI-совместимые поля: `model`, `messages` (system+user),
  `temperature=0.7`, `max_tokens=AI_MAX_OUTPUT_LENGTH`.
- **`timeout=AI_TIMEOUT_SECONDS` — таймаут 15 минут** (config.py); локальная модель на
  слабом ПК думает долго, поэтому ждём щедро. Таймаут ловится в `except` ниже функции.
- Любой HTTP-код ≠200 — ошибка: текст ответа обрезается до 300 символов в коде для
  пользователя, полный текст печатается в консоль.
- `choices[0].message.content` — извлечение ответа по стандартной схеме OpenAI;
  при «пирамиде ключей» не сходящейся — провал в общий `except Exception` → кортеж
  `(None, причину)`, который обрабатывает `handle_ai_text`.

---

## handlers/media.py — файлы и медиа

Файл (1288 строк): раздел «Файлы и медиа» — OCR с фото (easyocr), текст из PDF (PyPDF2),
распознавание речи из видео (moviepy + SpeechRecognition), ключевые моменты видео
(moviepy), ИИ-постобработка извлечённого текста.

### Флаги доступности (строки 78–127)

```python
try:
    import easyocr
    OCR_AVAILABLE = True
except Exception:
    OCR_AVAILABLE = False

try:
    from PyPDF2 import PdfReader
    PDF_AVAILABLE = True
except Exception:
    PDF_AVAILABLE = False

try:
    import moviepy
    MOVIEPY_AVAILABLE = True
except Exception:
    MOVIEPY_AVAILABLE = False

try:
    import speech_recognition
    SPEECH_AVAILABLE = True
except Exception:
    SPEECH_AVAILABLE = False
```

- Тяжёлые библиотеки импортируются «планово» через try/except: если пакет не установлен,
  модуль всё равно импортируется, просто флаг = False. Каждый обработчик ПЕРЕД работой
  проверяет свой флаг и вежливо сообщает «функция недоступна», а не падает ImportError.
- `_video_clip_class()` (строка 1092) достаёт `VideoFileClip` совместимо с версиями:
  в moviepy 2.x класс на верхнем уровне (`moviepy.VideoFileClip`), в 1.x — в
  `moviepy.editor`; функция пробует оба варианта.

### Константы (строки 129–139)

```python
MEDIA_MAX_SIZE = 60 * 1024 * 1024      # лимит размера файла — 60 МБ
_SCENE_CHANGE_THRESHOLD = 18.0          # порог «смены сцены» (яркость кадров)
_MAX_KEY_FRAMES = 5                     # максимум ключевых моментов
_MIN_SHOT_GAP = 3.0                     # мин. интервал между моментами, сек
```

### Клавиатуры (строки 162–245)

- `_media_menu_keyboard(user_id)` — меню раздела: «🖼 Текст с фото» (`photo`), «📄 Текст из
  PDF» (`pdf`), «🎬 Речь из видео» (`video_audio`), «🎥 Ключевые моменты видео»
  (`video_keyframes`), «← Назад». В зависимости от флагов доступности кнопки могут
  помечаться «недоступно».
- `_media_cancel_keyboard()` — кнопка «❌ Отмена» (возврат в меню раздела).
- `_result_keyboard()` — кнопки ИИ-постобработки текста: «✏️ Исправить ошибки»
  (`ai_fix_errors`), «📝 Решить задание» (`ai_solve_task`). Текст берётся из FSM-ключа
  `media_text`, обрабатывается `_ask_ai` в потоке.
- `_done_keyboard()` — кнопки после ИИ-постобработки («Продолжить с текстом»/«Готово»).

### `handle_media_menu(update, fsm_context)` (строка 247)

Колбэк-обработчик кнопок раздела. Сначала «входной барьер» (существует+одобрен+не
заблокирован), затем по payload:
- `menu_media` — показать меню раздела (`_media_menu_keyboard`) со списком доступных функций.
- `photo` → `fsm_context.update_data(mode="photo")` + `set_state(MediaStates.wait_photo)`.
- `pdf` → `set_state(MediaStates.wait_pdf)`.
- `video_audio` → `set_state(MediaStates.wait_video_audio)`.
- `video_keyframes` → `set_state(MediaStates.wait_video_keyframes)`.
- `media_text` / `ai_fix_errors` / `ai_solve_task` — ИИ-постобработка текста из FSM.
- `menu_main` — возврат в главное меню.

### `handle_media_attachments(update, fsm_context)` (строка 473)

Принимает медиафайл, соответствующий текущему состоянию FSM.

```python
state = await fsm_context.get_state()
body = update.message.body

if state == MediaStates.wait_photo:
    photo = body.photo[0] if body.photo else None
    if photo is None:
        await update.answer_text("🖼 Пришли именно фото с текстом.", keyboard=_media_cancel_keyboard().build())
        return
    await _process_photo(update, fsm_context, user_id, photo)
    return

if state == MediaStates.wait_pdf:
    file = body.file
    if file is None or not (file.filename or "").lower().endswith(".pdf"):
        await update.answer_text("📄 Пришли документ в формате PDF.", keyboard=_media_cancel_keyboard().build())
        return
    await _process_pdf(update, fsm_context, user_id, file)
    return

if state in (MediaStates.wait_video_audio, MediaStates.wait_video_keyframes):
    video = body.video[0] if body.video else None
    if video is None:
        await update.answer_text("🎬 Пришли именно видео.", keyboard=_media_cancel_keyboard().build())
        return
    mode = "keyframes" if state == MediaStates.wait_video_keyframes else "speech"
    await _process_video(update, fsm_context, user_id, video, mode)
    return

# любое другое сообщение — показываем меню-подсказку (FSM не сбрасываем)
await update.answer_text("📂 Файлы и медиа\n\nВыбери действие:", keyboard=_media_menu_keyboard(user_id).build())
```

- `update.message.body` — структура присланного сообщения: `body.photo` (список),
  `body.file` (один документ), `body.video` (список), `update.text`.
- Медиа не того типа — подсказка «пришли именно ...», состояние FSM НЕ меняется
  (можно прислать корректный файл следующим сообщением).

### `_process_photo(update, fsm_context, user_id, photo)` (строка 569) — OCR

```python
if not OCR_AVAILABLE:
    await update.answer_text("❌ Распознавание текста с фото пока недоступно. ...",
                             keyboard=_media_menu_keyboard(user_id).build())
    return
url = photo.payload.url   # прямая ссылка на изображение
```

Далее — конвейер:
1) `tempfile.mkdtemp()` — временная папка; 2) `_download_to(url, path)` через
`bot.download`; 3) `await asyncio.to_thread(_ocr_image, path)` — OCR (easyocr) в отдельном
потоке (на CPU занимает секунды и не должен блокировать loop); 4) результат — текст,
сохраняется в FSM как `media_text`; 5) `_send_result(...)` отправляет пользователю текст +
кнопки постобработки; 6) `finally: _rmtree_safe(...)` — удаление временной папки.

### `_process_pdf(..., file)` (строка 649)

Аналогичный конвейер для PDF: проверка `PDF_AVAILABLE`, скачивание файла,
`asyncio.to_thread(_pdf_text, path)` (PyPDF2 читает страницы, собирает текст),
запись `media_text` в FSM, `_send_result`.

### `_process_video(..., video, mode)` (строка 732)

Ветвление по `mode`:
- `speech` — требуется `MOVIEPY_AVAILABLE` и `SPEECH_AVAILABLE`: скачивание видео,
  `asyncio.to_thread(_extract_speech, ...)` — moviepy достаёт аудиодорожку в WAV,
  `speech_recognition` распознаёт речь (Google Web Speech), отдаёт текст.
- `keyframes` — `MOVIEPY_AVAILABLE`: `_extract_key_moments(...)` — покадровый анализ
  (`_save_frame`), поиск «смены сцены» по порогу `_SCENE_CHANGE_THRESHOLD`, отбор до
  `_MAX_KEY_FRAMES` кадров с интервалом ≥ `_MIN_SHOT_GAP`; результат — список моментов с
  временны́ми отметками (`_fmt_ts`) и миниатюрами (отправка через `BufferedInputFile`).
- Ошибки этапов: `db.save_ai_error(user_id, "media", ...)` + `print` + понятный ответ
  пользователю.

Также в файле:
- `_send_result(update, header, content)` (строка 910) — обрезает текст до
  `AI_MAX_OUTPUT_LENGTH` и отправляет с `_result_keyboard()`.
- `_rmtree_safe(path)` (строка 943) — удаление временной папки без краха при отсутствии.
- `_safe_filename(name)` (строка 956) — «санитизация» имени файла (убирает опасные символы).
- `_download_to(url, destination)` (строка 971) — `bot.download(url)` в файл.
- `_pick_video_url(urls)` (строка 982) — выбор подходящего url видео из набора.
- `_fmt_ts(seconds)` (строка 1000) — форматирование секунд в `ММ:СС`.
- `_get_ocr_reader()` (строка 1031) — создание/кэширование easyocr-ридера.
- `_ocr_image(path)` (строка 1048) — распознавание одной картинки.
- `_pdf_text(path)` (строка 1069) — извлечение текста из PDF.
- `_extract_speech(video_path, workdir)` (строка 1107) — речь из видео в текст.
- `_extract_key_moments(video_path, workdir)` (строка 1159) — ключевые кадры.
- `_save_frame(video_path, thumb_path, t)` (строка 1238) — сохранение кадра в момент t.