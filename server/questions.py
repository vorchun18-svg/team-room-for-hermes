"""Вопросы человеку: понятный язык, тема, дедупликация, закрытые темы (W30-A).

Зачем модуль: руководитель писал вопросы «для себя» — с внутренними кодами
(``D19``, ``G1``, ``ISIC 56.10``), без «зачем это нужно» и «откуда взялось»,
и одной теме могло быть посвящено десять почти одинаковых вопросов. Человек
видел 52 пункта и не понимал, что от него хотят (дефекты 13, 14).

Здесь три вещи, все — БЕЗ вызова моделей (правила и ключевые слова):

* :func:`plain_check` — прошёл ли вопрос проверку «понятно неспециалисту»:
  нет голых внутренних кодов и сокращений без расшифровки в скобках, объём в
  норме (W31-A2: полнота формы «зачем»/«откуда» — отдельный признак, иначе
  проверка красила в «плохие» читаемые вопросы, у которых просто нет этих строк);
* :func:`humanize` — переписать машинный вопрос для ПОКАЗА, без вызова моделей:
  снять код-префикс («D19 — », «Q3. »), убрать внутренние коды из середины
  текста, развернуть частые сокращения, разложить «А) … Б) …» в варианты с
  последствиями, дописать знак вопроса и строку «откуда взялось»;
* :func:`topic_key` и :func:`dedupe` — устойчивая тема вопроса и схлопывание
  смысловых дублей (ключевые слова/идентификаторы/числа). Дубль не попадает в
  основной список: у него ``duplicate_of`` и он считается в ``hidden_duplicates``;
* :func:`close_topic`/:func:`reopen_topic` — реестр закрытых тем комнаты
  (``data/state/<room>/question_topics.json``): сказал «не актуально» — об этом
  больше не спрашиваем, а строка «не спрашивать об этом» уходит в контракт
  руководителя.

Ещё здесь :func:`shape` — одно место, которое доводит список вопросов запроса
до контракта W30 §1: ``why/context/options/blocking/plain_ok/plain_issues/
topic_key/duplicate_of``, потолок блокирующих (3), ``questions_summary`` и
``stage_hint``. Модуль чистый (никаких sqlite/сети); файл реестра пишется через
``statefiles`` — атомарно и с блокировкой по пути.
"""
from __future__ import annotations

import re
import time

try:  # пакетный и «плоский» импорт (python server/app.py)
    from . import statefiles as sf
    from .answers import text_key as _text_key
except ImportError:  # pragma: no cover
    import statefiles as sf                    # type: ignore
    from answers import text_key as _text_key  # type: ignore

#: потолок блокирующих вопросов на задачу (контракт §1, правило продукта)
MAX_BLOCKING = 3
#: объёмы из контракта §1 — длиннее человек не читает
MAX_TEXT = 300
MAX_WHY = 300
MAX_CONTEXT = 600
MAX_CONSEQUENCE = 160
MAX_OPTIONS = 4
#: порог схожести тем: доля общих значимых слов от меньшего вопроса
SIM_MIN = 0.6
#: минимум значимых слов, чтобы вообще сравнивать смысл (короткие — не сравниваем)
SIM_MIN_TOKENS = 2
#: сколько закрытых тем отдаём в контракт руководителя (знаков)
CLOSED_LINE_MAX = 600
TOPICS_FILE = "question_topics.json"

#: слова, которые есть почти в каждом вопросе и темой не являются
_STOP = frozenset((
    "или", "если", "как", "что", "это", "для", "при", "над", "без", "про", "они", "она", "оно",
    "мы", "вы", "ты", "он", "нас", "вас", "мне", "меня", "его", "её", "их", "так", "все", "всё",
    "еще", "ещё", "уже", "где", "когда", "чтобы", "надо", "можно", "нужно", "будем", "буду",
    "есть", "было", "были", "быть", "тоже", "также", "этот", "эта", "эти", "того", "ли", "же",
    "бы", "не", "ни", "на", "по", "из", "от", "до", "за", "во", "со", "то", "чем", "кто", "какой",
    "какая", "какие", "сколько", "да", "нет", "пока", "сразу", "только", "себе", "себя", "весь",
    "вся", "этом", "этого", "надо", "стоит", "нужен", "нужна", "нужны", "важно", "лучше",
))

#: внутренние коды: ``D19``, ``G1``, ``Q3`` (буква + 1–3 цифры)
_CODE_RE = re.compile(r"(?<![\w\-])([A-ZА-ЯЁ]\d{1,3})(?![\w])")
#: латинские сокращения 2–6 букв (``ISIC``, ``VAT``, ``CPF``), не часть слова
_ABBR_RE = re.compile(r"(?<![\w])([A-Z]{2,6})(?![\w])")
#: то же сокращение вместе с хвостом слова (``VAT-скидка``) — для отсева «терминов»
_ABBR_TOKEN_RE = re.compile(r"(?<![\w])([A-Z]{2,6})(?![A-Za-z])")
#: классификационные номера вида ``56.10``
_CLASS_RE = re.compile(r"(?<![\w])(\d{3,5}-\d/\d{2})(?![\w])")
#: латинские идентификаторы (ники, бренды, домены): ``leto``, ``glpbox``
_LATIN_RE = re.compile(r"[@#]?([A-Za-z][A-Za-z0-9._\-]{2,})")
_NUM_RE = re.compile(r"(?<![\w])(\d{3,})(?![\w])")
_WORD_RE = re.compile(r"[0-9a-zA-Zа-яёА-ЯЁ\-]{3,}")
_PAREN_AFTER = re.compile(r"\s*\(([^()]{2,120})\)")

#: латинские слова, которые не идентификатор, а обычное слово/валюта
_LATIN_SKIP = frozenset(("the", "and", "for", "with", "from", "www", "http", "https", "com",
                         "org", "net", "pdf", "doc", "docx", "xls", "xlsx", "png", "jpg", "csv"))


def norm(text) -> str:
    """Схлопнуть пробелы (``""`` для None) — как в остальном приложении."""
    return " ".join(str(text or "").split())


def clip(text, limit: int) -> str:
    """Обрезать по границе слова с многоточием."""
    value = norm(text)
    if len(value) <= limit:
        return value
    cut = value[: max(1, limit - 1)]
    if " " in cut:
        cut = cut.rsplit(" ", 1)[0]
    return cut.rstrip(" ,;:—-") + "…"


# ------------------------------------------------------------ понятный язык ---

#: частые сокращения сферы документов → человеческое пояснение (первое упоминание)
#: в вопросе. Список нарочно короткий: только то, в чём нельзя сомневаться.
#: Неизвестное сокращение (бренд, ник, термин комнаты) НЕ делает вопрос плохим —
#: оно уходит в мягкое замечание «проверьте, не название ли это».
GLOSSARY = {
    "ИП": "микро-предприниматель",
    "ИНН": "номер юрлица",
    "CPF": "номер физлица",
    "ОКВЭД": "классификатор видов деятельности",
    "PJ": "юрлицо",
    "PF": "физлицо",
    "ISIC": "классификатор видов деятельности",
    "VAT": "налог на добавленную стоимость",
}

#: код в НАЧАЛЕ вопроса: «D19 — », «Q3. », «G1) », «V2: »
_CODE_HEAD = re.compile(r"^\s*(?P<code>[A-ZА-ЯЁ]\d{1,3})\s*(?:[—–\-:.\)]|\s)\s*")
#: метка варианта внутри текста: «А) …», «Б) …», «A) …»
_OPTION_MARK = re.compile(r"(?:(?<=\s)|^)(?P<letter>[А-ЕA-E])\)\s*")
#: «Последствие: …» — что будет, если выбрать этот вариант
_CONSEQ = re.compile(r"\bПоследствие\s*:\s*", re.IGNORECASE)
#: «Что выбираем?» / «Что выбрать?» — служебный хвост машинной формулировки
_CHOOSE = re.compile(r"\bЧто\s+(?:выбираем|выберем|выбрать|берём|берем|делаем)\s*\?", re.IGNORECASE)
#: признаки вопроса в утвердительной формулировке (для знака «?»)
_QUESTION_CUE = re.compile(r"\b(?:или|ли|да\s*/\s*нет|сколько|какой|какая|какие|каком|кто|где|когда|"
                           r"зачем|почему|хватает|подходит|принимает|запрещает|оформлен)\b", re.IGNORECASE)


def _expanded(text: str, match) -> bool:
    """Сокращение расшифровано рядом: ``VAT (микро-предприниматель)`` или ``(VAT)``."""
    before = text[max(0, match.start() - 120): match.start()]
    after = text[match.end(): match.end() + 140]
    if before.rstrip().endswith("(") and after.lstrip().startswith(")"):
        return True                       # «(ISIC)» — стоим внутри скобок
    if _PAREN_AFTER.match(after):
        return True                       # «ISIC (классификатор видов деятельности)»
    if re.search(r"\([^()]{0,120}" + re.escape(match.group(1)) + r"[^()]{0,120}\)", text):
        return True                       # «классификатор видов деятельности (ISIC)»
    return False


def _class_expanded(text: str, match) -> bool:
    """Номер классификатора пояснён сразу за номером: ``56.10 (код вида деятельности)``."""
    return bool(_PAREN_AFTER.match(text[match.end(): match.end() + 140]))


def _dedupe(items) -> list:
    seen, out = set(), []
    for item in items:
        if item not in seen:
            seen.add(item)
            out.append(item)
    return out


def _glossary_mentioned(text: str, abbr: str) -> bool:
    """Сокращение расшифровано в этом же тексте: ``ИП (микро-предприниматель)``.

    Одного раза достаточно: человек прочитал расшифровку выше, повторять её у
    каждого упоминания — шум.
    """
    return bool(re.search(r"(?<![\w\-])" + re.escape(abbr) + r"\s*\([^()]{2,80}\)", text)
                or re.search(r"\([^()]{0,40}" + re.escape(abbr) + r"[^()]{0,40}\)", text))


def code_issues(text) -> list[str]:
    """Жёсткие причины «человек не поймёт»: голые коды, известные сокращения, классификатор."""
    value = norm(text)
    out: list[str] = []
    if not value:
        return out
    for match in _CODE_RE.finditer(value):
        out.append("Внутренний код «%s» — человек не знает, что это" % match.group(1))
    for match in _CLASS_RE.finditer(value):
        if _class_expanded(value, match):
            continue
        out.append("Номер классификатора «%s» без пояснения" % match.group(1))
    for match in _ABBR_RE.finditer(value):
        abbr = match.group(1)
        if len(abbr) < 2 or abbr in ("OK", "ID", "RU", "EN") or abbr.lower() in _LATIN_SKIP:
            continue
        if abbr in GLOSSARY and not _expanded(value, match) and not _glossary_mentioned(value, abbr):
            out.append("Сокращение «%s» без расшифровки в скобках" % abbr)
    return _dedupe(out)


def soft_issues(text) -> list[str]:
    """Мягкие замечания: незнакомое сокращение может быть названием — проверить, не гасить.

    W31-A2: раньше ЛЮБОЕ слово капсом (``PIXIE``, ``AMI``, ``VEG``, ``городе``) считалось
    «сокращением без расшифровки» и красило вопрос в «непонятные». На живой задаче
    Лето это давало ложные срабатывания на названиях знака и бренда: человек видел
    «проверка простоты не пройдена» там, где всё понятно.
    """
    value = norm(text)
    out: list[str] = []
    for match in _ABBR_RE.finditer(value):
        abbr = match.group(1)
        if len(abbr) < 2 or abbr in ("OK", "ID", "RU", "EN") or abbr.lower() in _LATIN_SKIP:
            continue
        if abbr in GLOSSARY or _expanded(value, match):
            continue
        out.append("Сокращение «%s» — проверьте: если это не название, расшифруйте" % abbr)
    return _dedupe(out)


def plain_form_ok(why=None, context=None) -> bool:
    """Заполнена ли форма вопроса: есть «зачем» и «откуда взялось» (контракт §1)."""
    return bool(norm(why)) and bool(norm(context))


def plain_check(text, why=None, context=None, options=None) -> tuple[bool, list[str]]:
    """(понятен ли человеку САМ ТЕКСТ вопроса, причины).

    W31-A2: проверка разделена на два разных вопроса — раньше они были смешаны
    и мерили не то. Замер 27.11 на живой задаче Лето: 41 из 41 вопроса помечены
    «плохими», а на экране половина читается нормально — потому что отсутствие
    строк «зачем»/«откуда» само по себе давало ``plain_ok=false``. Из 46 вопросов
    копии 24 не имели НИ ОДНОЙ текстовой проблемы.

    * ``plain_ok`` — понятен ли текст: нет голых внутренних кодов, известных
      сокращений без расшифровки, номера классификатора без пояснения; объём в
      норме; вариантов не больше четырёх. Это то, что человек видит;
    * полнота формы (``why``/``context``) — отдельный признак
      :func:`plain_form_ok` и мягкие замечания в списке. Они не делают понятный
      вопрос «непонятным», но остаются в ``plain_issues`` для руководителя.
    """
    value = norm(text)
    hard: list[str] = []
    soft: list[str] = []
    hard.extend(code_issues(value))
    if not value:
        hard.append("Пустой вопрос")
    elif len(value) > MAX_TEXT:
        hard.append("Слишком длинный вопрос: %d знаков (норма — до %d)" % (len(value), MAX_TEXT))
    if isinstance(options, list) and len(options) > MAX_OPTIONS:
        hard.append("Вариантов больше %d — человек не выберет" % MAX_OPTIONS)
    soft.extend(soft_issues(value))
    if not norm(why):
        soft.append("Нет строки «зачем этот вопрос» (why)")
    elif len(norm(why)) > MAX_WHY:
        soft.append("«Зачем» длиннее %d знаков" % MAX_WHY)
    if not norm(context):
        soft.append("Нет строки «откуда взялось» (context)")
    elif len(norm(context)) > MAX_CONTEXT:
        soft.append("«Откуда взялось» длиннее %d знаков" % MAX_CONTEXT)
    plain_ok = bool(value) and not hard
    return plain_ok, _dedupe(hard + soft)


# ------------------------------------------------- переписывание при показе ---

def _tidy(text: str) -> str:
    """Убрать следы вырезанных кодов: двойные пробелы, пробелы перед знаками, пустые «/»."""
    value = re.sub(r"\s{2,}", " ", text)
    value = re.sub(r"\s+([,;:.!?])", r"\1", value)
    value = re.sub(r"([(])\s+", r"\1", value)
    value = re.sub(r"\(\s*\)", "", value)
    value = re.sub(r"(?:\s*/\s*){2,}", " / ", value)
    value = re.sub(r"^\s*/\s*", "", value)
    value = re.sub(r"\s*/\s*$", "", value)
    return value.strip(" ,;—-")


def _is_list_label(text: str, match) -> bool:
    """Код подписывает пункт перечисления: ``V1 бот / V2 QR``, ``G1,``, ``(G2)``."""
    before = text[:match.start()]
    after = text[match.end():]
    word_then_sep = bool(re.match(r"\s+[^\s/(),;]+[\s]*[/,]", after))
    if word_then_sep:
        return True
    return bool(re.search(r"[(/,]\s*$", before)
                and re.match(r"\s+[^\s/(),;]+[\s]*[/,)]", after))


def _strip_codes(text: str) -> tuple[str, list]:
    """Убрать внутренние коды из середины текста: ``(D19)`` → «пункт плана».

    Код, который подписывает пункт перечисления (``V1 бот / V2 QR / V3 вручную``)
    или стоит один в скобках (``(G1)``), вырезается целиком: человеку нужны слова
    «бот / QR / вручную», а не номера пунктов. Одиночный код заменяется на
    «пункт плана»: выдумывать, что именно значит ``D19``, нельзя — расшифровки у
    сервера нет.
    """
    matches = list(_CODE_RE.finditer(text))
    if not matches:
        return text, []
    codes = _dedupe([match.group(1) for match in matches])
    out = text
    for match in sorted(matches, key=lambda item: item.start(), reverse=True):
        start, end = match.start(), match.end()
        bare_paren = start > 0 and end < len(text) and text[start - 1] == "(" and text[end] == ")"
        if bare_paren:
            start, end, repl = start - 1, end + 1, ""
        elif _is_list_label(text, match):
            repl = ""
        else:
            repl = "пункт плана"
        out = out[:start] + repl + out[end:]
    return _tidy(out), codes


def _expand_glossary(text: str) -> str:
    """Развернуть ПЕРВОЕ упоминание частого сокращения: «ИП (микро-предприниматель)»."""
    out = text
    for abbr in sorted(GLOSSARY, key=len, reverse=True):
        if abbr not in out:
            continue
        match = re.search(r"(?<![\w\-])(" + re.escape(abbr) + r")(?![\w])", out)
        if match is None or _expanded(out, match):
            continue
        out = out[:match.end()] + " (%s)" % GLOSSARY[abbr] + out[match.end():]
    return out


def _label_classifiers(text: str) -> str:
    """Пояснить номер классификатора: ``56.10`` → ``56.10 (код вида деятельности)``.

    Расшифровки конкретного кода у сервера нет, но ЧТО это за номер — известно
    из формы записи, и это уже снимает вопрос «а что такое 56.10».
    """
    out = text
    for match in reversed(list(_CLASS_RE.finditer(text))):
        if _class_expanded(text, match):
            continue
        out = out[:match.end()] + " (код вида деятельности)" + out[match.end():]
    return out


def _ensure_question(text: str) -> str:
    """Дописать «?», если машинная формулировка потеряла знак вопроса.

    Только для короткой формулировки без тире-вставки: у длинного текста с
    «— пояснением» знак вопроса в конце приклеивался к пояснению и читался как
    ошибка («…— без этого нельзя планировать публикации?»).
    """
    value = text.strip()
    if not value or value.endswith(("?", "!", "…", ".", ":")):
        return value
    if len(value) > 200 or "—" in value:
        return value
    first = re.match(r"^[А-Яа-яЁёA-Za-z]+", value)
    if first and (first.group(0).lower().endswith(("ть", "ться"))):
        return value                      # «Позвонить…», «Проверить…» — поручение, не вопрос
    return value + "?" if _QUESTION_CUE.search(value) else value


def _option_index(letter: str, count: int) -> int:
    """«А»/«Б»/«В» (и латиница) → номер варианта, иначе -1."""
    letter = str(letter or "").upper()
    for alphabet in ("АБВГДЕ", "ABCDEF"):
        if letter in alphabet:
            index = alphabet.index(letter)
            return index if index < count else -1
    return -1


def _parse_options(text: str):
    """Разложить машинное «А) … Б) … Что выбираем? Последствие: …» в вопрос + варианты.

    Правило простое и обратимое: подписи вариантов — то, что стоит между «А)»,
    «Б)», «В)»; «Последствие:» внутри варианта становится его последствием, а
    хвостовое «Последствие: при Б …» приписывается тому варианту, который назван.
    Если разметки нет или она неполная — возвращаем ``None``: текст не трогаем.
    """
    marks = list(_OPTION_MARK.finditer(text))
    if len(marks) < 2:
        return None
    stem = text[:marks[0].start()].strip(" ,;:—-")
    if len(stem) < 6:
        return None
    bodies = []
    for index, mark in enumerate(marks):
        end = marks[index + 1].start() if index + 1 < len(marks) else len(text)
        bodies.append(text[mark.end():end].strip(" ,;"))
    note = ""
    last = bodies[-1]
    choose = _CHOOSE.search(last)
    if choose:
        tail = last[choose.end():].strip(" ,;")
        bodies[-1] = last[:choose.start()].strip(" ,;")
        if tail:
            consequence = _CONSEQ.search(tail)
            note = _CONSEQ.sub("", tail, count=1).strip(" .,;") if consequence else tail.strip(" .,;")
    options = []
    for index, body in enumerate(bodies):
        consequence = ""
        found = _CONSEQ.search(body)
        if found:
            consequence = body[found.end():].strip(" .,;")
            body = body[:found.start()].strip(" ,;")
        body = body.strip(" .,;")
        options.append({"id": "o%d" % (index + 1), "label": clip(body, 120),
                        "consequence": clip(consequence, MAX_CONSEQUENCE)})
    if note:
        letters = re.findall(r"\bпри\s+([А-ЕA-E])\b", note)
        index = _option_index(letters[0], len(options)) if len(letters) == 1 else -1
        if index >= 0:
            options[index]["consequence"] = clip(
                (options[index]["consequence"] + " " + note).strip(), MAX_CONSEQUENCE)
            note = ""
    if not stem.endswith("?"):
        stem = stem.rstrip(" .,;:—-") + " — что выбираем?"
    return {"text": stem, "options": options[:MAX_OPTIONS], "note": note}


def humanize(text, options=None) -> dict:
    """Переписать машинный вопрос для показа человеку — без вызова моделей.

    Что делают правила (дефект 24):

    * «D19 — отправляете 5–10 сообщений вручную или готовым текстом» → «Отправляете
      5–10 сообщений вручную или готовым текстом?»: код-префикс снимается и
      остаётся в поле ``code`` (для отладки), в видимом тексте кодов нет;
    * «Q1. Доставка: А) …; Б) … Что выбираем?» → вопрос + варианты (``options``);
    * «Канал связи (V1 бот / V2 QR / V3 вручную)» → «Канал связи (бот / QR / вручную)»;
    * «Оформлен ли ИП/ИНН…» → «Оформлен ли ИП (микро-предприниматель) / ИНН
      (номер юрлица)…» — только первое упоминание, только известные сокращения;
    * потерянный «?» возвращается, если формулировка вопросительная.

    Возвращает ``{raw, text, code, codes, options, note, changed}``. Функция чистая
    и идемпотентная: на уже переписанном тексте ничего не меняет.
    """
    raw = norm(text)
    out = {"raw": raw, "text": raw, "code": "", "codes": [], "options": [], "note": "",
           "changed": False}
    if not raw:
        return out
    value = raw
    code = ""
    head = _CODE_HEAD.match(value)
    if head and not re.match(r"^\s*[A-ZА-ЯЁ]\d{1,3}[А-Яа-яA-Za-z]", value):
        code = head.group("code")
        value = value[head.end():].strip()
    value, mid_codes = _strip_codes(value)
    value = _expand_glossary(value)
    value = _label_classifiers(value)
    parsed = _parse_options(value) if not (isinstance(options, list) and options) else None
    note, new_options = "", []
    if parsed:
        value = parsed["text"]
        new_options = parsed["options"]
        note = parsed["note"]
    value = _ensure_question(value)
    if value and value[0].islower():
        value = value[0].upper() + value[1:]
    out.update(text=value or raw, code=code, codes=mid_codes or ([code] if code else []),
               options=new_options, note=note, changed=bool(value and value != raw))
    return out


def source_line(task_title="", card_title="", kind="card") -> str:
    """Строка «откуда вопрос» для человека: задача + карточка (дефект 24).

    Правило контракта §1 — у вопроса есть «откуда взялось». Старые вопросы этой
    строки не имеют, и придумывать мотив нельзя; зато известно происхождение:
    карточка исполнителя в задаче (или сообщение руководителя в ленте).
    """
    task = clip(task_title, 100)
    card = clip(card_title, 70)
    where = "Из сообщения руководителя" if str(kind) == "comment" else "Из карточки"
    if card and task:
        return "%s «%s» в задаче «%s»." % (where, card, task)
    if card:
        return "%s «%s»." % (where, card)
    if task:
        return ("%s исполнителя в задаче «%s»." % (where, task) if str(kind) != "comment"
                else "%s в задаче «%s»." % (where, task))
    return ""


# ------------------------------------------------------------------- тема ---

def _stem(word: str) -> str:
    """Грубая основа русского слова: «стирку»/«стирки» → «стирк», «обычной» → «обычн».

    Берём 5 знаков у длинных слов и 4 у пятизначных — этого хватает, чтобы
    падежи одного слова совпали, и мало, чтобы склеить разные темы («курьер» и
    «курьерский» — та же тема, а «курьер» и «доставка» — нет).
    """
    if len(word) > 5:
        return word[:5]
    if len(word) == 5:
        return word[:4]
    return word


def stems(text) -> set:
    """Значимые основы слов: «доставка»/«доставки» → «доста»."""
    out: set = set()
    for word in _WORD_RE.findall(norm(text).lower().replace("ё", "е")):
        if word in _STOP or len(word) < 4:
            continue
        out.add(_stem(word))
    return out


def ids(text) -> set:
    """Идентификаторы темы: латинские слова/ники, внутренние коды, номера классификаторов.

    Числа-количества (``210``, ``42 000``) сюда НЕ входят: они встречаются в
    половине вопросов комнаты и сделали бы «одной темой» цену и зарплату.
    """
    value = norm(text)
    out: set = set()
    for match in _LATIN_RE.finditer(value):
        word = match.group(1).lower().strip("._-")
        if len(word) < 3 or word in _LATIN_SKIP or word.isdigit():
            continue
        out.add(word.split(".")[0])
    for pattern in (_CODE_RE, _CLASS_RE):
        for match in pattern.finditer(value):
            out.add(match.group(1).lower())
    return out


def strong_ids(text) -> set:
    """Идентификаторы, которые НАЗЫВАЮТ объект: ник, бренд, код с цифрой, номер.

    Голое сокращение капсом (``VAT``, ``ISIC``, ``WIPO``) — термин сферы, а не имя:
    под ним живут разные вопросы («какой статус регистрации» и «в каком классе
    подавать»), и склейка по нему прячет от человека живой вопрос. Такие слова
    остаются обычной темой и сравниваются по значимым словам.
    """
    value = norm(text)
    weak = {match.group(1).lower().strip("._-") for match in _ABBR_TOKEN_RE.finditer(value)}
    return {item for item in ids(text) if item not in weak}


def signature(text) -> tuple[set, set]:
    """(идентификаторы, значимые слова) — подпись темы вопроса.

    Порядок важен: в :func:`similarity` и :func:`topic_key` первым идёт набор
    идентификаторов (ник, код, номер) — он «сильнее» обычных слов.
    """
    return ids(text), stems(text)


def stems_in_order(text) -> list:
    """Значимые основы в порядке появления (для выбора темы вопроса)."""
    out: list = []
    for word in _WORD_RE.findall(norm(text).lower().replace("ё", "е")):
        if word in _STOP or len(word) < 4:
            continue
        stem = _stem(word)
        if stem not in out:
            out.append(stem)
    return out


def topic_key(text) -> str:
    """Устойчивый ключ темы: идентификатор, иначе тема из начала вопроса.

    Ключ нужен для группировки в интерфейсе и для реестра закрытых тем; он
    считается из текста, не меняется между чтениями и не зависит от порядка
    вопросов. Из обычных слов берём самое длинное среди первых трёх значимых —
    в русском вопросе тема стоит в начале («Доставка на старте…» → ``достав``),
    а не «городу» из хвоста. Пустой ключ — в вопросе нет значимых слов (тогда
    ключом станет id).
    """
    word_ids, _words = signature(text)
    if word_ids:
        return sorted(word_ids, key=lambda item: (-len(item), item))[0][:24]
    ordered = stems_in_order(text)
    if ordered:
        head = ordered[:3]
        return max(head, key=len)[:24]
    return ""


def _head(text, count: int = 2) -> list:
    """Первые значимые основы вопроса (тема в русском вопросе стоит в начале)."""
    return stems_in_order(text)[:count]


def similarity(left, right) -> float:
    """Насколько два вопроса об одном: доля общих значимых слов от меньшего.

    Правила по старшинству:

    * общий идентификатор (ник, код, номер классификатора) — та же тема;
    * одинаковое НАЧАЛО вопроса (первые два значимых слова: «Цена за килограмм…»)
      и хотя бы два общих слова — та же тема: руководитель переписывает вопрос
      другими словами, а тема та же;
    * иначе — минимум три общих слова и доля :data:`SIM_MIN` от меньшего
      вопроса. Без этого «доставка» и «смена курьера» схлопнулись бы в один.
    """
    left_ids, left_words = signature(left) if isinstance(left, str) else left
    right_ids, right_words = signature(right) if isinstance(right, str) else right
    if isinstance(left, str) and isinstance(right, str):
        left_ids, right_ids = strong_ids(left), strong_ids(right)
    if left_ids and right_ids and left_ids & right_ids:
        return 1.0
    if len(left_words) < SIM_MIN_TOKENS or len(right_words) < SIM_MIN_TOKENS:
        return 0.0
    shared = left_words & right_words
    if len(shared) >= 2 and isinstance(left, str) and isinstance(right, str):
        left_head, right_head = _head(left), _head(right)
        if left_head and left_head == right_head:
            return 1.0
    if len(shared) < 3:
        return 0.0
    return len(shared) / float(min(len(left_words), len(right_words)))


def is_same_topic(left, right) -> bool:
    """Один и тот же вопрос по смыслу (см. :func:`similarity`)."""
    if _text_key(left) and _text_key(left) == _text_key(right):
        return True
    return similarity(left, right) >= SIM_MIN


def _is_waiting(question: dict) -> bool:
    """Вопрос ещё ждёт ответа (не отвечен, не снят, не отложен)."""
    if question.get("answered") or question.get("dismissed"):
        return False
    state = str(question.get("answer_state") or question.get("state") or "").lower()
    return state not in ("answered", "applied", "no_change", "received", "sent", "replied_text",
                         "dismissed", "postponed")


def raw_text(question) -> str:
    """Текст вопроса, который написал руководитель (до переписывания при показе)."""
    if not isinstance(question, dict):
        return ""
    return str(question.get("text_raw") or question.get("text") or "")


def dedupe(questions, closed_topics=None) -> tuple[list, list]:
    """Схлопнуть смысловые дубли: ``(видимые, скрытые)``.

    ``closed_topics`` — реестр :func:`read_topics`: вопрос о теме, которую человек
    закрыл как «не актуально», не показывается (``duplicate_of`` = id того
    вопроса, ``duplicate_reason`` = ``closed_topic``). Порядок видимых — как был;
    дубли — копии с полями ``duplicate_of``/``topic_key``.

    Правило «не потерять живой вопрос»: ЖДУЩИЙ вопрос скрывается только в пользу
    другого ЖДУЩЕГО того же смысла. Если такой же вопрос уже отвечен или снят,
    новый остаётся видимым — иначе руководитель спросил бы снова, а человек
    вопроса не увидел. История (отвеченные/снятые копии) скрывается всегда: её
    место в «Ответах», а не в списке.
    """
    visible: list = []
    hidden: list = []
    closed = closed_topics if isinstance(closed_topics, dict) else {}
    for question in questions or []:
        if not isinstance(question, dict):
            continue
        text = raw_text(question)
        key = topic_key(text)
        own_id = str(question.get("id") or "")
        waiting = _is_waiting(question)
        found = None
        # 1) тема уже закрыта человеком — «об этом не спрашивать».
        #    Свой собственный вопрос (он и закрыл тему) и уже снятый остаются на
        #    месте: человек должен видеть, что снял, и кнопку «Вернуть».
        if not question.get("dismissed"):
            for entry in closed.values():
                if not isinstance(entry, dict):
                    continue
                if own_id and str(entry.get("question_id") or "") == own_id:
                    continue
                if key and str(entry.get("topic_key") or "") == key:
                    found = (str(entry.get("question_id") or ""), "closed_topic", key)
                    break
                if is_same_topic(text, str(entry.get("text") or "")):
                    found = (str(entry.get("question_id") or ""), "closed_topic",
                             str(entry.get("topic_key") or key))
                    break
        # 2) такой же вопрос уже показан выше
        if found is None:
            for kept in visible:
                if waiting and not _is_waiting(kept):
                    continue          # отвеченный близнец не прячет живой вопрос
                if is_same_topic(text, raw_text(kept)):
                    found = (str(kept.get("id") or ""), "duplicate",
                             str(kept.get("topic_key") or key))
                    break
        if found is None:
            question["topic_key"] = key or ("q_" + str(question.get("id") or ""))
            question.pop("duplicate_of", None)
            visible.append(question)
            continue
        duplicate_of, reason, topic = found
        item = dict(question)
        item["topic_key"] = topic or (key or ("q_" + str(question.get("id") or "")))
        item["duplicate_of"] = duplicate_of or None
        item["duplicate_reason"] = reason
        hidden.append(item)
    return visible, hidden


# --------------------------------------------------------- потолок блокирующих ---

def cap_blocking(questions, cap: int = MAX_BLOCKING) -> int:
    """Оставить не больше ``cap`` блокирующих; остальные — «пакет решений».

    Порядок вопросов не меняется (человек видит те же 3 первых блокирующих, что
    и руководитель задумал). Снятые с блокировки получают ``decision_package:
    true``: интерфейс показывает их компактным списком, работа из-за них не стоит.
    Возвращает число блокирующих после ужима.
    """
    blocking = 0
    for question in questions or []:
        if not isinstance(question, dict):
            continue
        question.pop("decision_package", None)
        if not question.get("blocking"):
            continue
        if blocking < cap:
            blocking += 1
            continue
        question["blocking"] = False
        question["decision_package"] = True
    return blocking


def blocking_count(questions) -> int:
    """Сколько БЛОКИРУЮЩИХ вопросов реально ждёт ответа.

    W30-QA2: раньше считались все вопросы с `blocking:true`, включая уже
    отвеченные и отправленные руководителю. Тогда `questions_summary.blocking`
    расходился с подписью этапа (`stale.stage_hint`, он считает только ждущие), и
    человек видел «3 блокирующих» при двух ждущих. Один признак — `_waiting()`.
    """
    return sum(1 for item in questions or []
               if isinstance(item, dict) and item.get("blocking") and _waiting(item))


#: Состояния, в которых вопрос уже НЕ ждёт ответа человека.
_NOT_WAITING = ("dismissed", "postponed", "answered", "applied", "no_change",
                "received", "sent", "replied_text")


def _waiting(item: dict) -> bool:
    """Ждёт ли вопрос ответа: не снят, не отложен, не отвечен (один признак)."""
    if not isinstance(item, dict):
        return False
    if item.get("dismissed") or item.get("answered"):
        return False
    state = str(item.get("answer_state") or item.get("state") or "").lower()
    return state not in _NOT_WAITING


def open_count(questions) -> int:
    """Сколько вопросов реально ждут ответа (не сняты, не отложены, не отвечены)."""
    return sum(1 for item in questions or [] if _waiting(item))


def summary(questions, hidden=None, closed_topics=None) -> dict:
    """``questions_summary`` контракта §1 (плюс понятные дополнительные счётчики)."""
    visible = [item for item in questions or [] if isinstance(item, dict)]
    states = {}
    for item in visible:
        state = str(item.get("answer_state") or "open")
        states[state] = states.get(state, 0) + 1
    return {"open": open_count(visible),
            "blocking": blocking_count(visible),
            "hidden_duplicates": len([item for item in hidden or [] if isinstance(item, dict)]),
            "closed_topics": len(closed_topics or {}),
            # дополнительные (контракт разрешает добавлять поля):
            "total": len(visible),
            "explained": states.get("explained", 0),
            "postponed": states.get("postponed", 0),
            "dismissed": states.get("dismissed", 0),
            # W31-A2: `plain_bad` — только реальные проблемы ТЕКСТА (коды, известные
            # сокращения, объём). Отсутствие «зачем»/«откуда» считается отдельно:
            # раньше именно оно красило в «плохие» читаемые вопросы (41 из 41 в Лето).
            "plain_bad": len([item for item in visible if item.get("plain_ok") is False]),
            "plain_incomplete": len([item for item in visible if item.get("plain_form_ok") is False]),
            "rewritten": len([item for item in visible if item.get("text_rewritten")])}


# ------------------------------------------------------------ закрытые темы ---

def topics_path(cfg, room_id):
    """Путь реестра закрытых тем комнаты: ``data/state/<room>/question_topics.json``."""
    from pathlib import Path
    return Path(getattr(cfg, "state_dir")) / str(room_id) / TOPICS_FILE


def read_topics(cfg, room_id) -> dict:
    """Реестр закрытых тем: ``{topic_key: {text, question_id, closed_at, reason}}``."""
    if cfg is None or not room_id:
        return {}
    try:
        data = sf.read_json(topics_path(cfg, room_id), {})
    except Exception:  # noqa: BLE001 - реестр тем вторичен для чтения вопросов
        return {}
    items = data.get("topics") if isinstance(data, dict) else None
    if not isinstance(items, dict):
        return {}
    return {str(key): value for key, value in items.items() if isinstance(value, dict)}


def _write_topics(cfg, room_id, topics: dict) -> dict:
    data = {"room": str(room_id), "topics": topics,
            "updated_at": int(time.time())}
    path = topics_path(cfg, room_id)
    with sf.lock_for(path):
        sf.write_json_atomic(path, data)
    return topics


def close_topic(cfg, room_id, question, reason: str = "", at: int | None = None) -> dict:
    """Запомнить закрытую тему («об этом не спрашивать»). Возвращает запись."""
    if not isinstance(question, dict) or cfg is None or not room_id:
        return {}
    text = str(question.get("text") or "")
    key = str(question.get("topic_key") or topic_key(text) or ("q_" + str(question.get("id") or "")))
    entry = {"topic_key": key, "text": clip(text, 200),
             "question_id": str(question.get("id") or "") or None,
             "closed_at": int(at or time.time()), "reason": clip(reason or "", 200) or None,
             "request": question.get("request_id") or None}
    topics = read_topics(cfg, room_id)
    topics[key] = entry
    _write_topics(cfg, room_id, topics)
    return entry


def reopen_topic(cfg, room_id, question) -> bool:
    """Убрать тему из реестра закрытых (человек нажал «Вернуть»). True — было."""
    if cfg is None or not room_id:
        return False
    text = str((question or {}).get("text") or "")
    key = str((question or {}).get("topic_key") or topic_key(text) or "")
    topics = read_topics(cfg, room_id)
    removed = False
    for name in ([key] if key else []):
        if name in topics:
            topics.pop(name, None)
            removed = True
    if not removed and text:
        for name, entry in list(topics.items()):
            if is_same_topic(text, str(entry.get("text") or "")):
                topics.pop(name, None)
                removed = True
    if removed:
        _write_topics(cfg, room_id, topics)
    return removed


def closed_line(topics, limit: int = CLOSED_LINE_MAX) -> str:
    """Строка «не спрашивать об этом» для контракта руководителя (≤ ``limit``)."""
    if not isinstance(topics, dict) or not topics:
        return ""
    items = [entry for entry in topics.values() if isinstance(entry, dict) and entry.get("text")]
    items.sort(key=lambda item: int(item.get("closed_at") or 0))
    texts = [clip(item.get("text"), 120) for item in items]
    line = "Об этом Михаил уже сказал «не актуально» — повторно не спрашивай: " + "; ".join(texts) + "."
    return line if len(line) <= limit else clip(line, limit)


# ------------------------------------------------------- общая форма списка ---

def _norm_options(raw) -> list[dict]:
    out: list[dict] = []
    for index, item in enumerate(raw or []):
        if isinstance(item, str):
            label, consequence = item, ""
        elif isinstance(item, dict):
            label = str(item.get("label") or item.get("text") or "").strip()
            consequence = norm(item.get("consequence") or item.get("effect") or "")
        else:
            continue
        if not label:
            continue
        out.append({"id": str((item or {}).get("id") or "o%d" % (index + 1))
                    if isinstance(item, dict) else "o%d" % (index + 1),
                    "label": clip(label, 120),
                    "consequence": clip(consequence, MAX_CONSEQUENCE)})
    return out[:MAX_OPTIONS]


def decorate(question, closed_topics=None, source=None) -> dict:
    """Проставить вопросу поля контракта §1 (``why/context/options/blocking/…``).

    W31-A2: перед проверкой текст переписывается для человека (:func:`humanize`),
    исходник остаётся в ``text_raw`` — по нему считаются тема и дубли, и он же
    уходит в ``plain_source_*``, чтобы руководитель видел свою формулировку.
    ``source`` — ``{"task": …, "card": …, "kind": "card"|"comment"}``: из него
    собирается строка «откуда взялось», если её не заполнил руководитель.
    """
    question = question if isinstance(question, dict) else {}
    stored_raw = norm(question.get("text_raw") or "")
    current = str(question.get("text") or "")
    human = {"code": str(question.get("code") or ""), "codes": [], "options": [], "note": ""}
    if stored_raw:
        raw_text, text = stored_raw, current          # уже переписан раньше
    else:
        raw_text = norm(current)
        human = humanize(raw_text, question.get("options"))
        text = human["text"]
        if text and text != raw_text:
            question["text_raw"] = raw_text
            question["text_rewritten"] = True
            question["text"] = text
        if human.get("code") and not question.get("code"):
            question["code"] = human["code"]
    why = clip(question.get("why"), MAX_WHY)
    context = clip(question.get("context"), MAX_CONTEXT)
    options = _norm_options(question.get("options"))
    if not options and human.get("options"):
        options = _norm_options(human["options"])
    if not context and isinstance(source, dict):
        auto = source_line(source.get("task") or "", source.get("card") or "",
                           source.get("kind") or "card")
        if auto:
            context = auto
            question["context_source"] = "provenance"
    plain_ok, issues = plain_check(text, why, context, options)
    question["why"] = why or None
    question["context"] = context or None
    question["options"] = options
    question["blocking"] = bool(question.get("blocking"))
    question.setdefault("topic_key", topic_key(raw_text or text) or ("q_" + str(question.get("id") or "")))
    question["plain_ok"] = plain_ok
    question["plain_issues"] = issues
    question["plain_form_ok"] = plain_form_ok(why, context)
    if raw_text and raw_text != text:
        src_ok, src_issues = plain_check(raw_text, why, context, options)
        question["plain_source_ok"] = src_ok
        question["plain_source_issues"] = src_issues
    if human.get("note"):
        question["consequence_note"] = clip(human["note"], MAX_CONSEQUENCE)
    question.setdefault("explanation", None)
    question.pop("duplicate_of", None)
    return question


def _tree_titles(detail: dict) -> list:
    """Заголовки карточек дерева задачи — для строки «откуда вопрос» (W31-A2).

    Корневую карточку (её заголовок — сама задача) пропускаем: приписать вопрос
    задаче, из которой он и так пришёл, значит не сказать ничего.
    """
    out: list = []
    task_title = norm((detail or {}).get("title") or (detail or {}).get("title_short") or "")

    def walk(nodes):
        for node in nodes or []:
            if not isinstance(node, dict):
                continue
            title = norm(node.get("title"))
            if (title and title != task_title and len(title) <= 90
                    and str(node.get("role") or "") not in ("root", "lead", "dir")):
                out.append((title, stems(title)))
            walk(node.get("children"))

    walk((detail or {}).get("tree"))
    return out


def card_title_of(text, titles) -> str:
    """Карточка, из которой пришёл вопрос: заголовок с максимумом общих слов.

    Связи «вопрос → карточка» в ответе движка нет (у вопросов только ``source:
    card``), поэтому сопоставляем по значимым словам и требуем минимум два общих:
    одно общее слово — это совпадение темы комнаты, а не происхождение, и
    приписывать вопрос чужой карточке нельзя.
    """
    words = stems(text)
    best, score = "", 0
    for title, title_stems in titles or []:
        shared = len(words & title_stems)
        if shared > score:
            best, score = title, shared
    return best if score >= 2 else ""


def shape(detail: dict, room_id: str = "", cfg=None, stored=None) -> dict:
    """Довести вопросы запроса до контракта W30 §1 (одно место для всех чтений).

    Делает: переписывание машинных формулировок для показа (:func:`humanize`),
    строку «откуда взялось» из происхождения вопроса, поля формы у каждого
    вопроса, скрытие смысловых дублей и вопросов закрытых тем, потолок
    блокирующих (3), ``questions_summary`` и ``stage_hint``.
    Ничего не пишет (кроме чтения реестра тем) и не вызывает моделей; повторный
    вызов на уже обработанной детали не ломает её, а пересчитывает то же самое.
    """
    if not isinstance(detail, dict):
        return detail
    questions = [item for item in (detail.get("open_questions") or []) if isinstance(item, dict)]
    if not questions:
        detail["questions_summary"] = summary([], [], read_topics(cfg, room_id) if cfg else {})
        detail["stage_hint"] = ""
        return detail
    topics = read_topics(cfg, room_id) if (cfg is not None and room_id) else {}
    task_title = detail.get("title_short") or detail.get("title") or ""
    titles = _tree_titles(detail)
    decorated = []
    for question in questions:
        item = dict(question)
        item.setdefault("request_id", detail.get("id"))
        kind = str(item.get("source") or "card")
        source = {"task": task_title,
                  "card": card_title_of(raw_text(item), titles) if kind == "card" else "",
                  "kind": kind}
        decorated.append(decorate(item, topics, source))
    visible, hidden = dedupe(decorated, topics)
    cap_blocking(visible)
    detail["open_questions"] = visible
    detail["hidden_duplicate_questions"] = hidden
    if isinstance(detail.get("questions"), list):
        detail["questions"] = visible
    detail["questions_summary"] = summary(visible, hidden, topics)
    try:
        from . import stale as stale_mod
    except ImportError:  # pragma: no cover
        import stale as stale_mod  # type: ignore
    detail["stage_hint"] = stale_mod.stage_hint(detail)
    return detail


def option_answer(question: dict, option_id) -> str:
    """Текст ответа по выбранному варианту: подпись + последствие (для ветки)."""
    wanted = str(option_id or "")
    for option in (question or {}).get("options") or []:
        if not isinstance(option, dict) or str(option.get("id")) != wanted:
            continue
        label = str(option.get("label") or "").strip()
        effect = str(option.get("consequence") or "").strip()
        return label + (" — " + effect if effect else "")
    return ""


__all__ = ["MAX_BLOCKING", "MAX_TEXT", "MAX_WHY", "MAX_CONTEXT", "MAX_CONSEQUENCE", "MAX_OPTIONS",
           "SIM_MIN", "TOPICS_FILE", "CLOSED_LINE_MAX", "GLOSSARY",
           "norm", "clip", "code_issues", "soft_issues", "plain_check", "plain_form_ok",
           "stems", "stems_in_order", "ids",
           "signature", "humanize", "source_line", "raw_text", "card_title_of",
           "topic_key", "similarity", "is_same_topic", "dedupe", "cap_blocking",
           "blocking_count", "open_count", "summary", "decorate", "shape", "option_answer",
           "topics_path", "read_topics", "close_topic", "reopen_topic", "closed_line"]
