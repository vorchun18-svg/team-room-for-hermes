"""Вопросы человеку: понятный язык, тема, дедупликация, закрытые темы (W30-A).

Зачем модуль: руководитель писал вопросы «для себя» — с внутренними кодами
(``D19``, ``G1``, ``ISIC 56.10``), без «зачем это нужно» и «откуда взялось»,
и одной теме могло быть посвящено десять почти одинаковых вопросов. Человек
видел 52 пункта и не понимал, что от него хотят (дефекты 13, 14).

Здесь три вещи, все — БЕЗ вызова моделей (правила и ключевые слова):

* :func:`plain_check` — прошёл ли вопрос проверку «понятно неспециалисту»:
  нет голых внутренних кодов и сокращений без расшифровки в скобках, есть
  «зачем» и «откуда взялось», объём в норме;
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


def code_issues(text) -> list[str]:
    """Голые внутренние коды и сокращения без расшифровки — причины, по-русски."""
    value = norm(text)
    out: list[str] = []
    if not value:
        return out
    for match in _CODE_RE.finditer(value):
        out.append("Внутренний код «%s» — человек не знает, что это" % match.group(1))
    for match in _CLASS_RE.finditer(value):
        out.append("Номер классификатора «%s» без пояснения" % match.group(1))
    for match in _ABBR_RE.finditer(value):
        abbr = match.group(1)
        if len(abbr) < 2 or abbr in ("OK", "ID", "RU", "EN"):
            continue
        if _expanded(value, match):
            continue
        out.append("Сокращение «%s» без расшифровки в скобках" % abbr)
    seen, unique = set(), []
    for item in out:
        if item not in seen:
            seen.add(item)
            unique.append(item)
    return unique


def plain_check(text, why=None, context=None, options=None) -> tuple[bool, list[str]]:
    """(понятно ли человеку, причины). ``plain_ok`` контракта §1.

    Норма — вопрос до 300 знаков, «зачем» до 300, «откуда взялось» до 600, не
    больше четырёх вариантов, без голых кодов. Причины перечисляются словами:
    это подпись для отладки и для руководителя, а не код ошибки.
    """
    value = norm(text)
    issues = code_issues(value)
    if not value:
        issues.append("Пустой вопрос")
    elif len(value) > MAX_TEXT:
        issues.append("Слишком длинный вопрос: %d знаков (норма — до %d)" % (len(value), MAX_TEXT))
    if not norm(why):
        issues.append("Нет строки «зачем этот вопрос» (why)")
    elif len(norm(why)) > MAX_WHY:
        issues.append("«Зачем» длиннее %d знаков" % MAX_WHY)
    if not norm(context):
        issues.append("Нет строки «откуда взялось» (context)")
    elif len(norm(context)) > MAX_CONTEXT:
        issues.append("«Откуда взялось» длиннее %d знаков" % MAX_CONTEXT)
    if isinstance(options, list) and len(options) > MAX_OPTIONS:
        issues.append("Вариантов больше %d — человек не выберет" % MAX_OPTIONS)
    # «понятно» = нет кодов и есть объяснение (why или context); остальное — мягкие замечания
    hard = [item for item in issues
            if item.startswith(("Внутренний код", "Номер классификатора", "Сокращение", "Слишком длинный",
                                "Пустой вопрос", "Вариантов больше"))]
    plain_ok = bool(value) and not hard and bool(norm(why) or norm(context))
    return plain_ok, issues


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
        text = str(question.get("text") or "")
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
                if is_same_topic(text, str(kept.get("text") or "")):
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
            "plain_bad": len([item for item in visible if item.get("plain_ok") is False])}


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


def decorate(question, closed_topics=None) -> dict:
    """Проставить вопросу поля контракта §1 (``why/context/options/blocking/…``)."""
    question = question if isinstance(question, dict) else {}
    text = str(question.get("text") or "")
    why = clip(question.get("why"), MAX_WHY)
    context = clip(question.get("context"), MAX_CONTEXT)
    options = _norm_options(question.get("options"))
    plain_ok, issues = plain_check(text, why, context, options)
    question["why"] = why or None
    question["context"] = context or None
    question["options"] = options
    question["blocking"] = bool(question.get("blocking"))
    question.setdefault("topic_key", topic_key(text) or ("q_" + str(question.get("id") or "")))
    question["plain_ok"] = plain_ok
    question["plain_issues"] = issues
    question.setdefault("explanation", None)
    question.pop("duplicate_of", None)
    return question


def shape(detail: dict, room_id: str = "", cfg=None, stored=None) -> dict:
    """Довести вопросы запроса до контракта W30 §1 (одно место для всех чтений).

    Делает: поля формы у каждого вопроса, скрытие смысловых дублей и вопросов
    закрытых тем, потолок блокирующих (3), ``questions_summary`` и ``stage_hint``.
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
    decorated = []
    for question in questions:
        item = dict(question)
        item.setdefault("request_id", detail.get("id"))
        decorated.append(decorate(item, topics))
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
           "SIM_MIN", "TOPICS_FILE", "CLOSED_LINE_MAX",
           "norm", "clip", "code_issues", "plain_check", "stems", "stems_in_order", "ids",
           "signature",
           "topic_key", "similarity", "is_same_topic", "dedupe", "cap_blocking",
           "blocking_count", "open_count", "summary", "decorate", "shape", "option_answer",
           "topics_path", "read_topics", "close_topic", "reopen_topic", "closed_line"]
