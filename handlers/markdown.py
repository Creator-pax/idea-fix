# ============================================================
# MARKDOWN-ОБРАБОТКА ОТВЕТОВ ИИ — handlers/markdown.py
# ============================================================
# Локальные модели (Qwen3 и др.) любят отвечать в Markdown:
#   # заголовки, **жирный**, *курсив*, `- списки`, "`код`".
# Этот модуль разбирает такой текст в «блоки» и «спаны» и умеет
# отдавать его в нескольких представлениях:
#   • parse_blocks  — структура документа (заголовки/абзацы/списки/код);
#   • parse_spans   — инлайн-разметка внутри одной строки;
#   • spans_to_text — «плоский» текст без маркеров (** и ### исчезают);
#   • reportlab_inline — инлайн-теги для reportlab Paragraph;
#   • markdown_to_plain — весь текст без markdown-пометок (для
#     сообщений в чате и .txt-файлов).
# Модуль используется в handlers/ai.py (показ ответа) и в
# handlers/export.py (сборка PDF/DOCX/RTF/PPTX и превью).
# ============================================================

import re

# Стили спанов: "" (обычный), "b" (жирный), "i" (курсив),
# "bi" (жирный+курсив), "c" (моноширинный код), "s" (зачёркнутый).
Span = tuple[str, str]   # (style, text)

# Regex инлайн-разметки. Порядок альтернатив важен: сначала тройные
# маркеры (***жирный+курсив***), потом двойные (**жирный**, `код`,
# ~~зачёркнутый~~), потом одинарные (*курсив*).
_INLINE_RE = re.compile(
    r"(\*\*\*.+?\*\*\*"
    r"|___.+?___"
    r"|\*\*.+?\*\*"
    r"|__.+?__"
    r"|\*.+?\*"
    r"|_.+?_"
    r"|`[^`\n]*`"
    r"|~~.+?~~)",
    re.DOTALL,
)

# Заголовок "# Level" / "### Level".
_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*)$")
# Горизонтальная линия "---" / "***" / "___".
_HR_RE = re.compile(r"^([-*_])\1{2,}\s*$")
# Список: "- item", "* item", "+ item", "1. item", "2) item".
_LIST_RE = re.compile(r"^\s*([-*+]|\d+[.)])\s+(.*)$")
# Блочная цитата "> текст".
_QUOTE_RE = re.compile(r"^\s*>\s?(.*)$")


# ============================================================
# ИНЛАЙН-РАЗМЕТКА (СПАНЫ)
# ============================================================

def _token_style(tok: str) -> tuple[str, str]:
    """Определяет стиль одного инлайн-токена и возвращает (стиль, текст)."""
    if tok.startswith("***") and tok.endswith("***") and len(tok) > 6:
        return "bi", tok[3:-3]
    if tok.startswith("___") and tok.endswith("___") and len(tok) > 6:
        return "bi", tok[3:-3]
    if tok.startswith("**") and tok.endswith("**") and len(tok) > 4:
        return "b", tok[2:-2]
    if tok.startswith("__") and tok.endswith("__") and len(tok) > 4:
        return "b", tok[2:-2]
    if tok.startswith("*") and tok.endswith("*") and len(tok) > 2:
        return "i", tok[1:-1]
    if tok.startswith("_") and tok.endswith("_") and len(tok) > 2:
        return "i", tok[1:-1]
    if tok.startswith("`") and tok.endswith("`") and len(tok) >= 2:
        return "c", tok[1:-1]
    if tok.startswith("~~") and tok.endswith("~~") and len(tok) > 4:
        return "s", tok[2:-2]
    return "", tok


def parse_spans(text: str) -> list[Span]:
    """Разбирает строку на спаны: [("b", "текст"), ("", " ещё")]."""
    if not text:
        return [("", "")]

    spans: list[Span] = []
    pos = 0
    for m in _INLINE_RE.finditer(text):
        if m.start() > pos:
            spans.append(("", text[pos:m.start()]))
        style, content = _token_style(m.group(0))
        # Внутри жирного/кода могут быть «остатки» разметки — повторяем
        # разбор содержимого для вложенных одинарных токенов.
        sub = parse_spans(content) if (style and style != "c") else [("", content)]
        for sub_style, sub_text in sub:
            merged = _merge_styles(style, sub_style)
            spans.append((merged, sub_text))
        pos = m.end()
    if pos < len(text):
        spans.append(("", text[pos:]))
    return spans


def _merge_styles(outer: str, inner: str) -> str:
    """Склеивает стили вложенных токенов (b + i = bi)."""
    if not inner:
        return outer
    if outer == "b" and inner == "i":
        return "bi"
    if outer == "i" and inner == "b":
        return "bi"
    return outer or inner


def spans_to_text(spans: list[Span]) -> str:
    """Возвращает «плоский» текст спанов без markdown-маркеров."""
    return "".join(text for _style, text in spans)


# ============================================================
# БЛОКИ ДОКУМЕНТА
# ============================================================

def _paragraph_spans(lines: list[str]) -> list[Span]:
    """Склеивает строки абзаца в один и разбирает инлайн-разметку."""
    joined = " ".join(line.strip() for line in lines if line.strip())
    return parse_spans(joined)


def parse_blocks(text: str) -> list[dict]:
    """Разбирает текст на блоки документа.

    Каждый блок — словарь:
      {"kind": "h", "level": 1..3, "spans": [...]}   — заголовок;
      {"kind": "p", "spans": [...]}                  — абзац;
      {"kind": "ul" | "ol", "items": [спан-списки]}  — список;
      {"kind": "code", "text": "..."}                — блок кода;
      {"kind": "quote", "spans": [...]}              — цитата;
      {"kind": "hr"}                                 — разделитель.
    """
    if not text or not text.strip():
        return [{"kind": "p", "spans": [("", "")]}]

    # Чиним типичные ошибки модели (пробелы после #, -, 1.) — до разбора,
    # чтобы заголовки и списки гарантированно находились.
    text = restore_markdown_grammar(text)
    lines = text.split("\n")
    blocks: list[dict] = []
    i, n = 0, len(lines)

    while i < n:
        line = lines[i]
        stripped = line.strip()

        # --- fenced-блок кода ```...``` ---
        if stripped.startswith("```"):
            buf: list[str] = []
            i += 1
            while i < n and not lines[i].strip().startswith("```"):
                buf.append(lines[i])
                i += 1
            i += 1  # пропускаем закрывающую ``` (если была)
            blocks.append({"kind": "code", "text": "\n".join(buf)})
            continue

        # --- заголовок "# ..." ---
        mh = _HEADING_RE.match(stripped)
        if mh:
            blocks.append({
                "kind": "h",
                "level": min(len(mh.group(1)), 3),
                "spans": parse_spans(mh.group(2).strip()),
            })
            i += 1
            continue

        # --- разделитель "---" ---
        if _HR_RE.fullmatch(stripped):
            blocks.append({"kind": "hr"})
            i += 1
            continue

        # --- цитата "> ..." ---
        if stripped.startswith(">"):
            quote_lines: list[str] = []
            while i < n:
                mq = _QUOTE_RE.match(lines[i])
                if not mq:
                    break
                quote_lines.append(mq.group(1))
                i += 1
            blocks.append({
                "kind": "quote",
                "spans": parse_spans(" ".join(q for q in quote_lines if q)),
            })
            continue

        # --- список ("- ", "1.") ---
        ml = _LIST_RE.match(line)
        if ml:
            marker = ml.group(1)
            ordered = not (marker in ("-", "*", "+"))
            items: list[list[Span]] = []
            while i < n:
                mi = _LIST_RE.match(lines[i])
                if not mi:
                    break
                items.append(parse_spans(mi.group(2)))
                i += 1
            blocks.append({"kind": "ol" if ordered else "ul", "items": items})
            continue

        # --- пустая строка ---
        if not stripped:
            i += 1
            continue

        # --- абзац: собираем строки до пустой/структурной строки ---
        buf: list[str] = []
        while i < n:
            l2 = lines[i]
            s2 = l2.strip()
            if not s2:
                break
            if (_HEADING_RE.match(s2)
                    or s2.startswith("```")
                    or s2.startswith(">")
                    or _LIST_RE.match(l2)
                    or _HR_RE.fullmatch(s2)):
                break
            buf.append(l2)
            i += 1
        blocks.append({"kind": "p", "spans": _paragraph_spans(buf)})

    return blocks


# ============================================================
# ПРЕДСТАВЛЕНИЯ ДЛЯ РАЗНЫХ ФОРМАТОВ
# ============================================================

def restore_markdown_grammar(text: str) -> str:
    """«Чинит» типичные ошибки markdown, которые делает маленькая модель.

    Модель вроде qwen3.5-0.8b часто пишет #Заголовок, -пункт и 1.пункт БЕЗ
    пробела после маркера — из-за этого парсер не видит ни заголовок, ни
    список, и в документ попадает голый текст с «хвостами» разметки.
    Функция расставляет пробелы после маркеров (кроме блока ```код,
    содержимое которого не трогаем):
      #Заголовок  →  # Заголовок
      ##Итог      →  ## Итог
      -пункт      →  - пункт
      1.первый    →  1. первый
    """
    if not text:
        return text

    out: list[str] = []
    in_code = False
    for line in text.split("\n"):
        if line.lstrip().startswith("```"):
            in_code = not in_code
            out.append(line)
            continue
        if in_code:
            out.append(line)   # содержимое кода не трогаем
            continue

        # #Заголовок / ##Итог без пробела после решёток. Якорь (?=[^\s#]):
        # следующий символ НЕ пробел и НЕ «левая» решётка — это не даёт
        # жадному #{1,6} с backtracking украсть "#" у "### Раздел".
        fixed = re.sub(r"^([ \t]*#{1,6})(?=[^\s#])", r"\1 ", line)
        # -пункт / +пункт без пробела (только если строка НЕ заканчивается
        # тем же маркером — иначе это *курсив* типа «*слово*» в начале строки).
        fixed = re.sub(r"^([ \t]*)([-+])(?=\S)", r"\1\2 ", fixed)
        if fixed.startswith("*") and not fixed.rstrip().endswith("*"):
            fixed = re.sub(r"^([ \t]*)\*(?=\S)", r"\1* ", fixed)
        # 1.пункт / 2)пункт без пробела после номера. (?=[^\s\d)]) не даёт
        # превратить "1.5 грамма" в "1. 5 грамма".
        fixed = re.sub(r"^([ \t]*)(\d+[.)])(?=[^\s\d)])", r"\1\2 ", fixed)
        out.append(fixed)
    return "\n".join(out)


def reportlab_inline(spans: list[Span]) -> str:
    """Инлайн-теги reportlab Paragraph (все символы экранированы)."""
    parts: list[str] = []
    for style, text in spans:
        esc = (
            text.replace("&", "&amp;")
                .replace("<", "&lt;")
                .replace(">", "&gt;")
        )
        if style == "b":
            esc = f"<b>{esc}</b>"
        elif style == "i":
            esc = f"<i>{esc}</i>"
        elif style == "bi":
            esc = f"<b><i>{esc}</i></b>"
        elif style == "c":
            esc = f'<font face="Courier">{esc}</font>'
        elif style == "s":
            esc = f"<strike>{esc}</strike>"
        parts.append(esc)
    return "".join(parts)


def markdown_to_plain(text: str) -> str:
    """Убирает из текста все markdown-пометки (для чата и .txt)."""
    if not text:
        return ""
    out: list[str] = []
    for block in parse_blocks(text):
        kind = block["kind"]
        if kind == "h":
            out.append(spans_to_text(block["spans"]))
        elif kind == "p" or kind == "quote":
            content = spans_to_text(block["spans"])
            if kind == "quote" and content:
                content = "«" + content + "»"
            out.append(content)
        elif kind in ("ul", "ol"):
            for idx, item in enumerate(block["items"], 1):
                marker = (f"{idx}. " if kind == "ol" else "• ")
                out.append(marker + spans_to_text(item))
        elif kind == "code":
            out.append(block["text"])
        elif kind == "hr":
            out.append("—" * 24)
        out.append("")
    return _remove_stray_markdown("\n".join(out).strip("\n"))


def _remove_stray_markdown(text: str) -> str:
    """Финальная зачистка текста: убирает «осиротевшие» markdown-маркеры.

    После разбора спанов matching-пары уже сняты — всё, что осталось в
    тексте (незакрытые **, застрявшие #, тики), это мусор модели. Убираем
    только явно разметочные последовательности, не трогая одиночные *
    и _ (они могут быть частью текста: «5*2», «_важно_»).
    """
    if not text:
        return text
    text = re.sub(r"\*\*", "", text)            # незакрытая **
    text = text.replace("`", "")                # одиночные тики
    text = re.sub(r"#{1,6}\s*", "", text)       # остатки #-маркеров
    text = text.replace("~~", "")               # остатки перечёркивания
    text = re.sub(r"[ \t]{2,}", " ", text)      # двойные пробелы → один
    return text.strip()