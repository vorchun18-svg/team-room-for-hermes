"""Ответ на вопрос: связка «вопрос ↔ ответ», статусы, реакция руководителя (W14-D).

Источник требований — ``Defects-2026-11-04-first-use.md`` пункты 5, 6, 10:

* ответ Михаила хранится КАК ОТВЕТ НА КОНКРЕТНЫЙ ВОПРОС (метка ``[Михаил · ответ на
  вопрос q_x «текст вопроса»]``), а не как обычное сообщение в общем чате;
* у ответа есть статус ``sent → received → applied → no_change`` и короткая
  реакция руководителя («Принято: …», «Работа пересмотрена: карточки …»); пока
  реакции нет — пометка «руководитель ещё не ответил» и кнопка «Напомнить»;
* встречный вопрос («поясни, что это») НЕ закрывает вопрос: остаётся ``open`` со
  статусом «ждёт вашего решения после пояснения» + сигнал руководителю;
* ответ «нет/не нужно» снимает связанные шаги («снят по вашему ответу»), а шаг,
  появившийся ПОСЛЕ такого ответа, помечается ``conflicts_with_answer``
  («противоречит вашему ответу» + «Убрать»).

Модуль чистый: никакого sqlite/CLI/сети. Формирует тексты, читает записи реестра
ответов и комментарии ленты, возвращает объекты API. Запись — в
``thread``/``routes_write``, чтение — в ``engine_read``/``routes_read``.
"""
from __future__ import annotations

import hashlib
import re
import time as _time

#: метки в ветке запроса (по ним читается и лента, и реакция)
ANSWER_MARK = "[Михаил · ответ на вопрос"
EXPLAIN_MARK = "[Михаил · просит пояснить по вопросу"
REACTION_MARK = "[Руководитель · реакция на ответ"
#: W30-A §5: пояснение руководителя прямо под вопросом («пояснено — выберите ответ»)
EXPLAINED_MARK = "[Руководитель · пояснение по вопросу"
#: варианты написания метки пояснения, которые тоже считаем пояснением
_EXPLAINED_ALIASES = (EXPLAINED_MARK, "[Руководитель · пояснение]",
                      "[Руководитель · пояснение к вопросу")
REMIND_MARK = "[Михаил · напоминание]"
#: W27-E1 §1: «Не актуально» и «Вернуть» — один вид метки для всех пунктов.
STALE_MARK = "[Михаил · закрыто как неактуальное"
RESTORE_MARK = "[Михаил · снова актуально"

#: W21-Q1 (B-44): стандартная просьба, когда «Пояснить» нажали БЕЗ текста.
#: Раньше пустое поле блокировало кнопку, а вопрос бывает просто непонятен —
#: писать нечего. Формулировка одна на сервер, ветку и демо-мок.
DEFAULT_EXPLAIN_TEXT = ("Поясни вопрос подробнее: зачем он, какие варианты ответа "
                        "и их последствия — простыми словами")

#: статусы ответа по контракту (W14-D §2; W19-D2 §2 — понятная цепочка;
#: W20-D1 §1.2 — «руководитель ответил текстом» вместо вечного «прочитал»)
STATES = ("open", "waiting_explanation", "explained", "sent", "received", "applied", "no_change",
          "replied_text", "dismissed", "postponed")
STATE_RU = {
    "open": "ждёт вашего ответа",
    "waiting_explanation": "ждёт вашего решения после пояснения",
    # W30-A §5: руководитель объяснил — вопрос тот же, но теперь понятно, что отвечать
    "explained": "пояснено — выберите ответ",
    # W30-A §1: «Отложить» — человек вернётся к вопросу позже
    "postponed": "отложено — вернётся позже",
    # W19-D2 (B-21): «Ответ отправлен» → «Руководитель прочитал» → «Учтено».
    "sent": "ответ отправлен",
    "received": "руководитель прочитал",
    "applied": "учтено: работа изменена",
    "no_change": "учтено: менять ничего не нужно",
    # W20-D1 §1.2 (B-41): реакция ЕСТЬ, но записана не по формату — не врём «реакции нет».
    "replied_text": "руководитель ответил текстом — посмотрите в ленте",
    # W20-D1 §1.2 (B-38): «Это не вопрос» — элемент больше не ждёт ответа.
    "dismissed": "это не вопрос",
}
#: «Что значит» — одна подсказка на каждое состояние (B-21: не угадывать по словам).
STATE_HINT = {
    "open": "Вопрос ждёт вашего ответа — руководитель не может двигаться дальше без него.",
    "waiting_explanation": "Вы попросили пояснение: вопрос остаётся открытым, пока руководитель "
                           "не объяснит, о чём речь.",
    # W30-A §5: подсказка «пока руководитель не объяснит» больше не нужна — он объяснил
    "explained": "Руководитель объяснил вопрос — прочитайте пояснение под вопросом и выберите "
                 "ответ. Сам вопрос не изменился.",
    "postponed": "Вы отложили вопрос: он вернётся в список позже. Работа из-за него не стоит, "
                 "если вопрос не был блокирующим.",
    "sent": "Ответ ушёл руководителю. Пока он его не открыл и не отметил исход — ждите "
            "или нажмите «Напомнить руководителю».",
    "received": "Руководитель прочитал ответ и взял его в работу. «Учтено» появится, когда он "
                "запишет, что именно изменил.",
    "applied": "Руководитель учёл ответ: работа пересмотрена, карточки изменены. "
               "Их список — рядом с реакцией.",
    "no_change": "Руководитель учёл ответ и решил, что менять в работе ничего не нужно.",
    "replied_text": "Руководитель ответил в ленте, но не по формату реакции (без метки и исхода). "
                    "Ответ есть — прочитайте его сообщение в диалоге.",
    "dismissed": "Вы отметили, что это не вопрос: элемент убран из «Нужен ваш ответ» "
                 "и не считается в счётчиках.",
}
#: короткая расшифровка цепочки — для подсказки у блока («что значит»)
CHAIN_HINT = ("Цепочка ответа: «ответ отправлен» → «руководитель прочитал» → «учтено» "
              "(с изменением карточек или «менять ничего не нужно»). "
              "Исход указывает сам руководитель в метаданных реакции.")
PENDING_RU = "руководитель ещё не ответил"
CANCELLED_RU = "снят по вашему ответу"
CONFLICT_RU = "противоречит вашему ответу"

#: сколько секунд «разумно» ждать реакции руководителя на ответ
REACTION_WAIT_S = 180

_TASK_RE = re.compile(r"t_[0-9a-f]{8}")
_QID_RE = re.compile(r"(q_[0-9A-Za-z_]+)")

#: слова-маркеры встречного вопроса (нужно пояснение, решения нет)
_ASK = ("что такое", "что значит", "что это", "в каком смысле", "поясни", "объясни",
        "уточни", "не понял", "непонятно", "расскажи", "сформулируй", "как понять",
        "что ты имеешь в виду", "зачем", "почему")
#: явное решение в тексте — значит это ответ, а не встречный вопрос
_DECISION = ("да,", "да.", "нет,", "нет.", "не нужно", "не надо", "не добавляем", "добавляем",
             "согласен", "не согласен", "делаем", "убираем", "оставляем", "выбираю", "берём",
             "берем", "утверждаю", "подтверждаю", "отказываемся", "никакого", "никаких")
#: «нет» в начале ответа — ответ-отказ (снимает шаги)
_NEG = ("нет", "не нужно", "не надо", "не добавля", "не трога", "не рассматр", "не требуется",
        "никакого", "никаких", "не будем", "не стоит", "не делаем", "не делай", "не надоб",
        "отказ", "снима", "убира", "не подтвержда", "не согласен")

_STOP = frozenset((
    "или", "если", "как", "что", "это", "для", "при", "над", "без", "про", "они", "она", "оно",
    "мы", "вы", "ты", "он", "она", "нас", "вас", "мне", "меня", "его", "её", "их", "так", "все",
    "всё", "еще", "ещё", "уже", "где", "когда", "чтобы", "надо", "можно", "нужно", "будем",
    "буду", "есть", "было", "были", "быть", "тоже", "также", "этот", "эта", "эти", "того",
    "ли", "же", "бы", "не", "ни", "на", "по", "из", "от", "до", "за", "во", "со", "то",
))


def norm(text) -> str:
    """Схлопнуть пробелы ('' для None)."""
    return " ".join(str(text or "").split())


def short(text, limit: int = 120) -> str:
    """Обрезать по границе слова с многоточием."""
    value = norm(text)
    return value if len(value) <= limit else value[: max(1, limit - 1)].rstrip() + "…"


def stems(text) -> set:
    """Значимые основы слов: «геолога»/«геологу» → «геолог» (для связки шаг↔вопрос)."""
    out = set()
    for word in re.findall(r"[0-9a-zA-Zа-яёА-ЯЁ\-]{3,}", norm(text).lower()):
        if word in _STOP:
            continue
        out.add(word[:6] if len(word) > 6 else word)
    return out


#: короткие подтверждения, которые НЕ меняют план (W29-Q9, п.3). Намеренно без «да/нет»:
#: «да» может утвердить шаг, «нет» снимает связанные шаги — это уже решение, ему нужен
#: руководитель. Здесь только «услышал/принял/спасибо».
ACK_WORDS = frozenset((
    "спасибо", "благодарю", "понял", "поняла", "понятно", "ясно", "ок", "окей", "ok", "okay",
    "хорошо", "ладно", "принято", "принял", "приняла", "учту", "учёл", "учел", "услышал",
    "услышала", "отлично", "супер", "ага", "угу", "всё", "все",
))


def is_simple_ack(text) -> bool:
    """Короткое подтверждение без изменения плана: «Спасибо, понял», «Принято».

    Консервативно: до 6 слов и 60 знаков, каждое слово — из списка подтверждений;
    отрицания («не», «нет») исключают фразу. Всё остальное — обычный ответ, который
    идёт руководителю карточкой.
    """
    value = norm(text).lower()
    if not value or len(value) > 60:
        return False
    words = re.findall(r"[0-9a-zа-яё]+", value)
    if not words or len(words) > 6:
        return False
    if any(word in ("не", "нет", "ни", "без") for word in words):
        return False
    return all(word in ACK_WORDS for word in words)


def is_negation(text) -> bool:
    """Ответ-отказ: шаги по этому вопросу снимаются."""
    value = norm(text).lower()
    if not value:
        return False
    for marker in _NEG:
        index = value.find(marker)
        if index < 0:
            continue
        # «нет» значимо в начале фразы или как отдельное слово
        if marker == "нет":
            if index == 0 or re.search(r"(^|[.!?;,\s])нет([\s,.!?;:]|$)", value):
                return True
            continue
        return True
    return False


def is_counter_question(text) -> bool:
    """Встречный вопрос («поясни, что это») — запасной вариант к явной кнопке.

    Явная кнопка «Мне нужно пояснение» надёжнее; эвристика ловит случаи, когда
    пользователь просто написал вопрос вместо решения. Если в тексте есть явное
    решение («да», «нет», «не нужно») — это ответ, даже с просьбой пояснить деталь.
    """
    value = norm(text).lower()
    if not value:
        return False
    if any(word in value for word in _DECISION):
        return False
    return any(word in value for word in _ASK)


# --------------------------------------------------------------------- тексты ---

def answer_comment(question_id, question_text, text) -> str:
    """Комментарий-ответ в ветке: видно, на КАКОЙ вопрос отвечено."""
    return "%s %s «%s»] %s" % (ANSWER_MARK, question_id, short(question_text, 120), norm(text))


def explanation_comment(question_id, question_text, text) -> str:
    """Встречный вопрос: просьба пояснить, а не ответ.

    W21-Q1 (B-44): текста может не быть — тогда в ветку идёт стандартная просьба
    ``DEFAULT_EXPLAIN_TEXT`` (кнопка «Пояснить» работает и с пустым полем).
    """
    body = norm(text) or DEFAULT_EXPLAIN_TEXT
    return "%s %s «%s»] %s" % (EXPLAIN_MARK, question_id, short(question_text, 120), body)


def explain_comment(question_id, question_text, text) -> str:
    """W30-A §5: метка ПОЯСНЕНИЯ руководителя — шаблон для контракта руководителя."""
    return "%s %s] %s" % (EXPLAINED_MARK, question_id, norm(text))


def explained_kind_of(text) -> bool:
    """Комментарий — пояснение руководителя по вопросу (§5)."""
    value = norm_start(text)
    return any(value.startswith(mark) for mark in _EXPLAINED_ALIASES)


def _explained_qid(text) -> str:
    """id вопроса из метки пояснения (``… вопросу q_x] …``); пусто — не нашли."""
    value = norm_start(text)
    head = value.split("]", 1)[0]
    match = _QID_RE.search(head)
    return match.group(1) if match else ""


def explained_text_of(text) -> str:
    """Текст пояснения без метки: ``[… q_x] Вот что имеется в виду`` → ``Вот…``."""
    value = norm_start(text)
    return norm(value.split("]", 1)[1]) if "]" in value else norm(value)


def explanations_from_thread(items) -> dict:
    """Пояснения руководителя из ленты: ``{qid: {text, at, comment_id}}`` (W30-A §5).

    Берём последнее пояснение по каждому вопросу: руководитель мог пояснить
    дважды, и человеку нужен свежий текст. Метка явная — угадывать по словам не
    нужно, поэтому «пояснение» не путается с обычным ответом в диалоге.
    """
    out: dict = {}
    for item in items or []:
        if not isinstance(item, dict) or item.get("author_kind") != "agent":
            continue
        raw = str(item.get("text") or "")
        if not explained_kind_of(raw):
            continue
        qid = _explained_qid(raw)
        body = explained_text_of(raw)
        if not qid or not body:
            continue
        at = int(item.get("at") or 0)
        prev = out.get(qid)
        if prev and int(prev.get("at") or 0) > at:
            continue
        out[qid] = {"text": body, "at": at, "comment_id": str(item.get("id") or "") or None,
                    "author": str(item.get("author") or "") or None}
    return out


#: метка одной карточки «накопленные ответы» (W17-B §3, B6)
DIGEST_MARK = "[Михаил · накопленные ответы]"


def digest_comment(items, limit: int = 180) -> str:
    """Одно сообщение руководителю со всеми ответами без реакции (W17-B §3).

    ``items`` — ``[{question_id, question_text, answer, at}]`` в порядке вопросов.
    Нумерация и id вопроса — чтобы руководитель мог ответить реакцией на каждый;
    текст ответа обрезается по границе слова (полный он в реестре и в ветке).
    """
    rows = [item for item in items or [] if isinstance(item, dict)]
    lines = ["%s Ответы без вашей реакции — %d. Ответь на КАЖДЫЙ короткой реакцией в ветке:"
             % (DIGEST_MARK, len(rows))]
    for number, item in enumerate(rows, start=1):
        qid = str(item.get("question_id") or "q_0")
        qtext = short(item.get("question_text") or "", 120)
        answer = short(item.get("answer") or "", limit)
        lines.append("%d) %s «%s» — %s" % (number, qid, qtext, answer))
    lines.append("Формат реакции: «%s %s] Учтено: изменено 2 карточки — t_…» (или "
                 "«Учтено: менять ничего не нужно»), и явный исход в метаданных реакции: "
                 "metadata={\"reaction\":{\"question_id\":\"%s\",\"outcome\":\"read|applied|no_change\"}}."
                 % (REACTION_MARK, str((rows[0] if rows else {}).get("question_id") or "q_x"),
                    str((rows[0] if rows else {}).get("question_id") or "q_x")))
    return "\n".join(lines)


def reaction_comment(question_id, state, text) -> str:
    """Готовая метка реакции руководителя (её пишет он сам в ветке)."""
    return "%s %s] %s" % (REACTION_MARK, question_id, norm(text))


def remind_text(question_id, question_text, ago_min: int = 0) -> str:
    """Напоминание руководителю: ждём реакцию на ответ."""
    wait = (" уже %d мин" % ago_min) if ago_min else ""
    return ("%s Жду реакции на ответ к вопросу %s «%s»%s. Ответь коротко в ветке: "
            "«%s %s] Принято: …» (или «Ничего менять не нужно», «Работа пересмотрена: карточки …»)."
            % (REMIND_MARK, question_id, short(question_text, 120), wait, REACTION_MARK, question_id))


# ------------------------------------------- вопросы руководителя из комментариев ---
#
# W20-D1 §1.2 (B-38): руководитель задаёт вопросы в диалоге, а не в metadata. Раньше
# такой текст оставался прозой и в «Нужен ваш ответ» ничего не появлялось. Теперь:
#
# * строки ``[Руководитель · вопрос] текст`` и ``[Руководитель · решение] текст``
#   в комментарии руководителя регистрируются как открытые вопросы/решения
#   (``source="comment"``);
# * страховка — нумерованные/маркированные пункты под заголовками «Что решить»,
#   «Что нужно решить», «Нужен ваш ответ», «Вопросы к вам/тебе» в комментарии
#   руководителя предлагаются кандидатами (``source="candidate"``, кнопка
#   «Это не вопрос» → ``question_dismiss`` → ``answer_state="dismissed"``).

LEAD_QUESTION_MARK = "[Руководитель · вопрос]"
LEAD_DECISION_MARK = "[Руководитель · решение]"
#: старые/смешанные написания меток, которые тоже считаем вопросом руководителя
_LEAD_QUESTION_ALIASES = (LEAD_QUESTION_MARK, "[Руководитель · вопросы]",
                          "[Руководитель · вопрос к Михаилу]")
_LEAD_DECISION_ALIASES = (LEAD_DECISION_MARK, "[Руководитель · решения]",
                          "[Руководитель · предложение]")

#: заголовки, под которыми руководитель перечисляет вопросы (сравнение — по началу строки)
QUESTION_HEADINGS = ("что решить", "что нужно решить", "что решаешь", "что решить тебе",
                     "что решить вам", "нужен ваш ответ", "нужен твой ответ",
                     "вопросы к вам", "вопросы к тебе", "вопросы")
#: сколько пунктов из одного комментария берём максимум
MAX_COMMENT_ITEMS = 20
#: маркер пункта списка: «1)», «1.», «-», «•», «–», «*»
_LIST_ITEM = re.compile(r"^\s*(?:\d{1,2}[).]|[-–—•*·])\s*(?P<text>.+)$")
_HEADING_LINE = re.compile(r"^\s*(?:\*\*|__)?\s*(?P<head>[^:.\n]{3,80})\s*(?:\*\*|__)?\s*:?\s*$")


def lead_comment_kind(text) -> str:
    """Вид записи руководителя по метке: ``question`` / ``decision`` / ``""``."""
    value = norm_start(text)
    if not value:
        return ""
    for mark in _LEAD_QUESTION_ALIASES:
        if value.startswith(mark):
            return "question"
    for mark in _LEAD_DECISION_ALIASES:
        if value.startswith(mark):
            return "decision"
    return ""


def norm_start(text) -> str:
    """Начало строки без ведущих пробелов (метка стоит в начале комментария)."""
    return str(text or "").lstrip()


def mark_text_of(text) -> str:
    """Текст метки без маркера: ``[Руководитель · вопрос] Что…`` → ``Что…``."""
    value = norm_start(text)
    if "]" in value and value.startswith("["):
        return norm(value.split("]", 1)[1])
    return norm(value)


def comment_item_id(text, comment_id, index: int = 0, prefix: str = "cq") -> str:
    """Стабильный id элемента из комментария: ``cq_<8hex>`` (не меняется между чтениями)."""
    seed = "%s|%s|%s" % (str(comment_id or ""), norm(text), int(index or 0))
    return "%s_%s" % (prefix, hashlib.sha1(seed.encode("utf-8")).hexdigest()[:8])


def _heading_matches(line: str) -> bool:
    """Строка — заголовок списка вопросов («Что решить тебе:», «**Нужен ваш ответ**»)."""
    match = _HEADING_LINE.match(norm(line))
    if not match:
        return False
    head = match.group("head").strip().lower()
    head = head.strip("*_ ").rstrip(":.").strip()
    if not head or len(head) > 60:
        return False
    return any(head.startswith(prefix) for prefix in QUESTION_HEADINGS)


def candidates_from_comment(text, comment_id, at: int = 0) -> list:
    """Пункты под заголовком «Что решить…» → кандидаты в вопросы (``source="candidate"``).

    Ничего не угадываем: берём только нумерованные/маркированные пункты, идущие
    НЕПОСРЕДСТВЕННО под заголовком. Пустая строка, новый заголовок или обычная проза
    список заканчивают. Пункты длиннее 400 знаков отбрасываются (это уже абзац).
    """
    out: list = []
    inside = False
    started = False
    for line in str(text or "").splitlines():
        stripped = line.strip()
        if not stripped:
            # пустая строка ПОСЛЕ пунктов список заканчивает; пустая строка между
            # заголовком и первым пунктом — обычное дело, её пропускаем
            if inside and started:
                inside = False
            continue
        if _heading_matches(stripped):
            inside, started = True, False
            continue
        if not inside:
            continue
        match = _LIST_ITEM.match(stripped)
        if not match:
            inside = False                  # проза после списка — список закончился
            continue
        item = norm(match.group("text"))
        if not item or len(item) > 400:
            continue
        out.append({"text": item})
        started = True
        if len(out) >= MAX_COMMENT_ITEMS:
            break
    return out


def questions_from_thread(items, lead=None, dismissed=None) -> list:
    """Вопросы и решения руководителя из ленты: ``source=comment|candidate``.

    Возвращает элементы того же вида, что ``open_questions`` (``id``, ``text``,
    ``for``, ``source``, ``comment_id``, ``at``, ``kind``), плюс ``dismissed`` —
    если пользователь нажал «Это не вопрос» (реестр ответов, запись ``dismissed``).

    Кто имеет право «задавать вопрос» (W21-L1, решение оркестратора):

    * **кандидаты из свободного текста** (пункты под «Что решить…») — ТОЛЬКО
      сообщения руководителя комнаты (``lead`` = ``room["orchestrator"]``).
      У комнаты нет руководителя — кандидатов нет вовсе: список «что решить»
      исполнителя или критика адресован не Михаилу, и угадывать нечего;
    * **явная метка** ``[Руководитель · вопрос] …`` — от любого агента: её ставят
      осознанно, двусмысленности нет (руководитель мог писать из другой карточки);
    * ``[Руководитель · решение] …`` — по-прежнему от руководителя комнаты
      (в комнате без руководителя — от любого агента: там нет «своего»).

    Дубли по нормальному тексту схлопываются.
    """
    leads = _lead_names(lead)
    dismissed = dismissed if isinstance(dismissed, dict) else {}
    out: list = []
    seen: set = set()
    for item in items or []:
        if not isinstance(item, dict) or item.get("author_kind") != "agent":
            continue
        # Разбор идёт по СЫРОМУ тексту: переводы строк — часть структуры списка
        # (norm() схлопнул бы их и список «Что решить» перестал бы быть списком).
        raw = str(item.get("text") or "")
        if not raw.strip():
            continue
        at = int(item.get("at") or 0)
        comment_id = str(item.get("id") or "")
        kind = lead_comment_kind(raw)
        found: list = []
        if kind:
            if kind == "decision" and not _from_leader(item, leads):
                continue                    # решения — только от руководителя
            body = mark_text_of(raw)
            if body:
                found.append({"text": body, "kind": kind, "source": "comment"})
        else:
            # Свободный текст: только руководитель комнаты и только если он известен.
            if not leads or not _from_leader(item, leads):
                continue
            for candidate in candidates_from_comment(raw, comment_id, at):
                found.append({"text": candidate["text"], "kind": "question",
                              "source": "candidate"})
        for index, entry in enumerate(found):
            key = text_key(entry["text"])
            if not key or key in seen:
                continue
            seen.add(key)
            qid = comment_item_id(entry["text"], comment_id, index,
                                  "cq" if entry["kind"] == "question" else "cd")
            record = dismissed.get(qid) if isinstance(dismissed, dict) else None
            out.append({"id": qid, "text": entry["text"], "for": "mikhail",
                        "answered": False, "answer": None, "answered_at": None,
                        "kind": entry["kind"], "source": entry["source"],
                        "comment_id": comment_id, "at": at or None,
                        "dismissed": bool(isinstance(record, dict)
                                          and (record.get("dismissed") or
                                               str(record.get("status") or "") == "dismissed")),
                        "task_id": item.get("task_id")})
    return out


def mark_asks(items, questions) -> None:
    """Проставить ``asks:[id]`` сообщениям руководителя с зарегистрированными вопросами.

    W20-D1 §1.2 (B-38): в ленте видно «ждёт ваших ответов: N → справа». Связь —
    по ``comment_id`` вопроса; сообщение без вопросов поля не получает (обратная
    совместимость: старые клиенты ничего не теряют).
    """
    by_comment: dict = {}
    for question in questions or []:
        if not isinstance(question, dict):
            continue
        if str(question.get("kind") or "question") != "question":
            continue
        if question.get("dismissed"):
            continue
        comment_id = str(question.get("comment_id") or "")
        if not comment_id:
            continue
        by_comment.setdefault(comment_id, []).append(str(question.get("id") or ""))
    for item in items or []:
        if not isinstance(item, dict):
            continue
        asked = by_comment.get(str(item.get("id") or "")) or []
        asked = [qid for qid in asked if qid]
        if asked:
            item["asks"] = asked
        elif "asks" in item:
            item.pop("asks", None)


# ------------------------------------------------------------- разбор из ленты ---

def _qid_from(text) -> str:
    match = _QID_RE.search(str(text or ""))
    return match.group(1) if match else ""


def answer_kind_of(text) -> str:
    """Вид записи по метке: ``answer`` / ``explanation_request`` / ``""``."""
    value = str(text or "")
    if value.startswith(ANSWER_MARK):
        return "answer"
    if value.startswith(EXPLAIN_MARK):
        return "explanation_request"
    return ""


def answer_text_of(text) -> str:
    """Текст ответа без метки: ``[… q_x «вопрос»] ответ`` → ``ответ``."""
    value = str(text or "")
    if "]" in value and value.startswith("["):
        return norm(value.split("]", 1)[1])
    return norm(value)


def question_id_of(text) -> str:
    """id вопроса из метки ответа (``q_…``) или ''."""
    return _qid_from(text)


def question_text_of(text) -> str:
    """Текст вопроса из метки ``[… q_x «текст вопроса»]`` ('' если нет)."""
    match = re.search(r"«([^»]{1,200})»", str(text or ""))
    return norm(match.group(1)) if match else ""


def _plural(n: int, one: str, few: str, many: str) -> str:
    """Русская форма числа: 1 карточка / 2 карточки / 5 карточек."""
    count = abs(int(n)) % 100
    if 11 <= count <= 14:
        return many
    tail = count % 10
    if tail == 1:
        return one
    if 2 <= tail <= 4:
        return few
    return many


def state_label(state, cards=None, changes=None) -> str:
    """Видимая подпись статуса ответа (B-21): третья ступень уточняется числом карточек."""
    key = str(state or "")
    if key == "applied":
        count = changes if changes is not None else len(cards or [])
        try:
            count = int(count)
        except (TypeError, ValueError):
            count = 0
        if count > 0:
            return "учтено: изменено %d %s" % (count, _plural(count, "карточка", "карточки", "карточек"))
        return STATE_RU["applied"]
    return STATE_RU.get(key, STATE_RU["open"])


def state_hint(state) -> str:
    """Подсказка «что значит» для статуса ответа (B-21)."""
    return STATE_HINT.get(str(state or ""), STATE_HINT["open"])


#: явный исход реакции в тексте: `outcome=applied` / `исход: no_change` / `учтено: …`
_OUTCOME_TAG = re.compile(r"(?:outcome|исход)\s*[:=]\s*[\"«']?([A-Za-zА-Яа-я_]+)", re.IGNORECASE)
_OUTCOME_ALIASES = {
    "read": "received", "received": "received", "прочитал": "received", "прочитано": "received",
    "applied": "applied", "учтено": "applied", "учёл": "applied", "учел": "applied",
    "no_change": "no_change", "nochange": "no_change", "без_изменений": "no_change",
    "ничего": "no_change",
}


def outcome_of(text) -> str:
    """Явный исход реакции из текста (``outcome=…``/``исход: …``) или ''.

    W19-D2 (B-21): основная дорога — ``metadata.reaction.outcome`` (читается в
    :func:`reactions_from_tree`). Этот разбор — тот же исход, но записанный в
    комментарии реакции по контракту: ``… outcome=applied``. Пусто — исход не
    указан, тогда (и только тогда) работает запасной разбор по словам.
    """
    match = _OUTCOME_TAG.search(norm(text))
    if not match:
        return ""
    value = match.group(1).strip().lower()
    return _OUTCOME_ALIASES.get(value, value if value in ("received", "applied", "no_change") else "")


def reaction_state(text, outcome=None) -> str:
    """Класс реакции: applied / no_change / received.

    Порядок (B-21): явный ``outcome`` (из метаданных реакции) → метка ``outcome=…``
    в тексте → запасной разбор по словам (только старые записи без исхода).
    """
    explicit = norm(outcome).lower() or outcome_of(text)
    if explicit in ("received", "applied", "no_change"):
        return explicit
    value = norm(text).lower()
    if any(word in value for word in ("ничего менять не нужно", "менять не нужно", "без изменений",
                                      "ничего не меняем", "изменения не нужны", "ничего менять не надо")):
        return "no_change"
    if any(word in value for word in ("пересмотр", "перезапуск", "доработ", "передела", "возвращ",
                                      "в работу", "пересобр", "карточк")):
        return "applied"
    return "received"



def _cards_of(text) -> list:
    out = []
    for card in _TASK_RE.findall(str(text or "")):
        if card not in out:
            out.append(card)
    return out


#: порог запасного (implicit) поиска реакции по совпадению основ слов.
#: Аудит B-12: при пороге 3 посторонний комментарий руководителя про ДРУГОЕ
#: уточнение засчитывался реакцией на вопрос `q_7decf20e`. Нужно не только число
#: общих слов, но и доля от слов вопроса — иначе редкий длинный вопрос «ловит»
#: любой отчёт об уточнениях.
IMPLICIT_MIN_SCORE = 4
IMPLICIT_MIN_RATIO = 0.5


def _lead_names(lead) -> set:
    """Профили руководителя комнаты (``str``/список) в нижнем регистре."""
    values = [lead] if isinstance(lead, str) else list(lead or [])
    return {str(value).strip().lower() for value in values if str(value or "").strip()}


def _from_leader(item, leads: set) -> bool:
    """Запись ленты — от руководителя комнаты (а не от любого агента).

    Аудит B-12: раньше подходил ЛЮБОЙ ``author_kind="agent"`` (критик, исполнитель),
    поэтому чужая реплика закрывала ответ. ``leads`` пусто → считаем, что комната
    не сообщила руководителя: тогда подходит любой агент (совместимость с моками).
    """
    if not isinstance(item, dict) or item.get("author_kind") != "agent":
        return False
    if not leads:
        return True
    return str(item.get("author") or "").strip().lower() in leads


def _after_answer(at: int, record) -> bool:
    """Реакция записана НЕ РАНЬШЕ ответа (аудит B-12: время обязательно).

    Время в ленте — секунды, поэтому «в ту же секунду» считается «после»: ответ и
    реакция на него физически не могут быть раньше ответа, а вот комментарий,
    написанный ДО ответа (боевой случай 05.11 16:48 против ответа 16:52),
    отсекается. Нет времени ответа (старая запись без ``at``) — проверять нечего.
    """
    answered_at = int((record or {}).get("at") or 0) if isinstance(record, dict) else 0
    return not answered_at or at >= answered_at


def reactions_from_thread(items, questions=None, answers=None, lead=None) -> dict:
    """Реакции руководителя из ленты: ``{question_id: {text, at, status, cards, …}}``.

    ЕДИНСТВЕННЫЙ путь поиска реакции — им пользуются окно вопроса, кнопка
    «Напомнить руководителю» и пересылка накопленных ответов (аудит B-12: раньше
    окно и кнопка искали по-разному и расходились).

    Правила (W17-B §8):
    * автор — ТОЛЬКО руководитель комнаты (``lead`` = ``room["orchestrator"]``);
    * реакция — только ПОСЛЕ ответа (``answers[qid]["at"]``);
    * основной путь — метка ``[Руководитель · реакция на ответ q_x]``
      (контракт ``docs/AGENT-GUIDE.md``);
    * запасной (``implicit``) — комментарий руководителя, который называет id
      вопроса, либо перекрывает текст вопроса по основам слов
      (:data:`IMPLICIT_MIN_SCORE` + :data:`IMPLICIT_MIN_RATIO`). Запасной путь
      работает только для вопросов, у которых есть ответ: без ответа реакции быть
      не может.
    """
    out: dict = {}
    records = answers if isinstance(answers, dict) else {}
    leads = _lead_names(lead)
    known: dict = {}
    rec_of: dict = {}
    for question in questions or []:
        if not isinstance(question, dict) or not question.get("id"):
            continue
        qid = str(question["id"])
        known[qid] = question
        record = record_for(question, records) if records else None
        if isinstance(record, dict) and norm(record.get("text")):
            rec_of[qid] = record
        for alias in question.get("aliases") or []:
            known.setdefault(str(alias), question)
            if qid in rec_of:
                rec_of.setdefault(str(alias), rec_of[qid])
    by_stem = {qid: stems((known.get(qid) or {}).get("text")) for qid in rec_of}
    for item in items or []:
        if not _from_leader(item, leads):
            continue
        text = norm(item.get("text"))
        if not text:
            continue
        at = int(item.get("at") or 0)
        qid, implicit = "", False
        if text.startswith(REACTION_MARK) or "реакция на ответ" in text.lower():
            qid = _qid_from(text)
        if not qid:
            mentioned = _qid_from(text)
            if mentioned and mentioned in known and mentioned in rec_of:
                qid, implicit = mentioned, True
        if not qid and by_stem and reaction_state(text) in ("applied", "no_change"):
            mine = stems(text)
            best, score, ratio = "", 0, 0.0
            for candidate, words in by_stem.items():
                common = len(mine & words)
                share = common / max(1, len(words))
                if common > score or (common == score and share > ratio):
                    best, score, ratio = candidate, common, share
            if best and score >= IMPLICIT_MIN_SCORE and ratio >= IMPLICIT_MIN_RATIO:
                qid, implicit = best, True
        if not qid:
            continue
        if not _after_answer(at, rec_of.get(qid)):
            continue                    # комментарий ДО ответа — не реакция (B-12)
        status = reaction_state(text)
        cards = _cards_of(text)
        previous = out.get(qid)
        if previous and int(previous.get("at") or 0) > at:
            continue
        out[qid] = {"text": text, "at": at, "status": status,
                    "label": state_label(status, cards),
                    # W19-D2 (B-21): исход указан явно (метка `outcome=…`) или
                    # выведен запасным разбором по словам старой записи.
                    "outcome": outcome_of(text) or None, "guessed": not outcome_of(text),
                    "cards": cards, "changes": len(cards) or None,
                    "source": "thread", "author": item.get("author") or "",
                    "author_label": item.get("author_label") or "",
                    "implicit": implicit, "task_id": item.get("task_id")}
    # W20-D1 §1.2 (B-41): реакция ЕСТЬ, но записана не по формату. Раньше вопрос навсегда
    # оставался «ответ отправлен / реакции нет» (боевой q_8c82dd07: руководитель ответил
    # «Принято: на письмо от 02.10 отдельно не отвечаем…», исход не распознан).
    # «Мягкое» состояние честнее: «руководитель ответил текстом — посмотрите в ленте».
    for qid in list(rec_of):
        question = known.get(qid) or {}
        if str(question.get("id") or "") != qid or qid in out:
            continue
        reply = _text_reply_after(items, rec_of.get(qid), leads,
                                  stems(question.get("text")), qid)
        if not reply:
            continue
        out[qid] = {"text": reply.get("text") or "", "at": int(reply.get("at") or 0),
                    "status": "replied_text", "label": STATE_RU["replied_text"],
                    "outcome": None, "guessed": True, "cards": [], "changes": None,
                    "source": "thread", "author": reply.get("author") or "",
                    "author_label": reply.get("author_label") or "",
                    "implicit": True, "unparsed": True, "task_id": reply.get("task_id")}
    return out


#: с каких слов начинается короткая реакция руководителя (даже без метки и исхода)
_REPLY_OPENERS = ("принято", "принял", "принимаю", "учтено", "учёл", "учел", "согласен",
                  "понял", "понятно", "работа пересмотрена", "работа пересмотр",
                  "ничего менять", "изменения", "сделано", "готово", "ок,", "ок.")
#: служебные метки — это НЕ текстовая реакция на ответ (собираются на вызов:
#: часть меток объявлена ниже по файлу — на импорте их ещё нет)
_NOT_A_REPLY = ("[Руководитель → ",)


def _not_a_reply_marks() -> tuple:
    """Служебные метки, которые не считаются текстовой реакцией на ответ."""
    return tuple(_NOT_A_REPLY) + (ANSWER_MARK, EXPLAIN_MARK, REACTION_MARK, REMIND_MARK,
                                  DIGEST_MARK, LEAD_QUESTION_MARK, LEAD_DECISION_MARK,
                                  DECISION_MARK, DECISION_REACTION_MARK, STEP_AGREE_MARK,
                                  STEP_DROP_MARK, STEP_COMMENT_MARK, STEP_MOVE_MARK,
                                  STEP_REACTION_MARK)


def _looks_like_reply(text, qid: str, words: set) -> bool:
    """Похоже ли сообщение руководителя на ответ по существу вопроса.

    Три дороги (в порядке надёжности): (1) назван id вопроса — самый точный признак;
    (2) начинается со слова реакции («Принято…», «Учтено…»); (3) перекрывает текст
    вопроса двумя значимыми основами. Служебные метки реакцией не считаются.
    """
    if not text:
        return False
    for mark in _not_a_reply_marks():
        if text.startswith(mark):
            return False
    if qid and qid in text:
        return True
    low = text.lower()
    if any(low.startswith(opener) for opener in _REPLY_OPENERS):
        return True
    if words:
        return len(stems(text) & words) >= 2
    return False


def _text_reply_after(items, record, leads: set, words: set, qid: str = "") -> dict | None:
    """Последнее сообщение руководителя ПОСЛЕ ответа, похожее на ответ по делу (B-41)."""
    answered_at = int((record or {}).get("at") or 0) if isinstance(record, dict) else 0
    if not norm((record or {}).get("text")):
        return None                      # реакции без ответа быть не может
    qid = str(qid or (record or {}).get("question_id") or "")
    found = None
    for item in items or []:
        if not _from_leader(item, leads):
            continue
        text = norm(item.get("text"))
        if not text:
            continue
        at = int(item.get("at") or 0)
        if answered_at and at < answered_at:
            continue
        if not _looks_like_reply(text, qid, words):
            continue
        found = item
    return dict(found) if found else None


def reactions_from_tree(nodes) -> dict:
    """Реакции из ``metadata.reaction`` карточек дерева: ``{question_id|decision_id: …}``.

    W19-D2 (B-21): исход задаёт САМ руководитель в метаданных реакции — его карточка
    завершается с ``metadata={"reaction": {"question_id": "q_x", "outcome": "applied",
    "cards": ["t_…"]}}`` (или ``decision_id`` для решения). Это первая дорога чтения,
    комментарий без метки ``outcome`` — запасная (старые записи).

    Поддерживаются формы: один объект, список объектов, а также ключ
    ``answer_reaction`` (старый контракт карточки «Уточнение»).
    """
    out: dict = {}
    for node in nodes or []:
        if not isinstance(node, dict):
            continue
        meta = node.get("metadata")
        if not isinstance(meta, dict):
            continue
        extra = meta.get("extra") if isinstance(meta.get("extra"), dict) else {}
        raw = extra.get("reaction") or extra.get("answer_reaction") or meta.get("reaction") \
            or meta.get("answer_reaction")
        if not raw:
            continue
        items = raw if isinstance(raw, list) else [raw]
        at_default = int(node.get("ended_at") or node.get("at") or 0)
        for item in items:
            if not isinstance(item, dict):
                continue
            key = str(item.get("question_id") or item.get("decision_id")
                      or item.get("question") or item.get("decision") or "").strip()
            if not key:
                continue
            status = reaction_state(item.get("text") or "", item.get("outcome") or item.get("status"))
            cards = [str(c) for c in (item.get("cards") or []) if str(c).strip()]
            try:
                changes = int(item.get("changes")) if item.get("changes") is not None else None
            except (TypeError, ValueError):
                changes = None
            at = int(item.get("at") or 0) or at_default
            previous = out.get(key)
            if previous and int(previous.get("at") or 0) > at:
                continue
            out[key] = {"text": norm(item.get("text")), "at": at, "status": status,
                        "label": state_label(status, cards, changes),
                        "outcome": str(item.get("outcome") or item.get("status") or status),
                        "guessed": False, "cards": cards,
                        "changes": changes if changes is not None else (len(cards) or None),
                        "source": "metadata", "node": str(node.get("id") or ""),
                        "author": str(node.get("assignee") or ""), "author_label": "",
                        "implicit": False, "task_id": str(node.get("id") or "")}
    return out


def merge_reactions(primary, secondary) -> dict:
    """Слить реакции: явные (метаданные) главнее, текст добираем из ленты."""
    out: dict = dict(secondary or {})
    for key, value in (primary or {}).items():
        if not isinstance(value, dict):
            continue
        old = out.get(key)
        merged = dict(value)
        if isinstance(old, dict):
            if not merged.get("text"):
                merged["text"] = old.get("text") or ""
            if not merged.get("at"):
                merged["at"] = int(old.get("at") or 0)
            if not merged.get("cards"):
                merged["cards"] = list(old.get("cards") or [])
            merged.setdefault("author_label", old.get("author_label") or "")
            merged["implicit"] = bool(old.get("implicit")) and not merged.get("outcome")
        out[key] = merged
    return out



def reaction_for_question(question, reactions) -> dict | None:
    """Реакция для вопроса с учётом id-близнецов (``aliases``)."""
    if not isinstance(question, dict):
        return None
    reactions = reactions if isinstance(reactions, dict) else {}
    found = reactions.get(str(question.get("id")))
    if isinstance(found, dict):
        return found
    for alias in question.get("aliases") or []:
        found = reactions.get(str(alias))
        if isinstance(found, dict):
            return found
    return None


def reaction_lookup(question, questions, records, thread_items, lead=None) -> tuple:
    """(запись ответа, реакция) — ОДИН поиск для окна вопроса и кнопки «Напомнить».

    ``questions`` — весь список вопросов запроса (а не один вопрос!): запасное
    сопоставление по тексту зависит от набора, и именно из-за разных наборов окно
    и кнопка расходились (аудит B-12).
    """
    record = record_for(question, records) if isinstance(records, dict) else None
    reactions = reactions_from_thread(thread_items, questions, records, lead=lead)
    return record, reaction_for_question(question, reactions)


# ------------------------------------------------------------------ применение ---

def state_of(record: dict | None, reaction: dict | None = None) -> str:
    """Статус ответа: реестр + найденная реакция руководителя.

    W17-B §2 (аудит B5): встречный вопрос — ТОЛЬКО явное действие
    ``need_explanation`` (кнопка). Эвристика по тексту больше не меняет статус:
    она ловила ложные срабатывания на старых записях (проверочный `q_7985e685`: в ответе
    было решение «эта папка не относится к юристу», а вопрос помечался
    «ждёт пояснения»). Такие записи реестра считаются ОТВЕТАМИ (`answered=true`),
    без перезаписи данных. Подсказка «похоже на просьбу пояснить» осталась
    отдельной функцией (`explanation_hint`) — только для журнала.
    """
    record = record if isinstance(record, dict) else {}
    text = norm(record.get("text"))
    # W20-D1 (B-38): «Это не вопрос» — элемент не ждёт ответа и не считается в счётчиках.
    if record.get("dismissed") or str(record.get("status") or "") == "dismissed":
        return "dismissed"
    # W30-A §1: «Отложить» — записи без ответа, но и не «ждёт сейчас».
    if str(record.get("status") or "") == "postponed":
        until = int(record.get("postpone_until") or 0) or 0
        return "postponed" if until > int(_time.time()) else "open"
    if not text:
        return "open"
    if record.get("answered") is False or str(record.get("status") or "") == "waiting_explanation":
        return "waiting_explanation"
    if isinstance(reaction, dict) and reaction.get("status") in ("received", "applied", "no_change",
                                                                "replied_text"):
        return str(reaction["status"])
    stored = str(record.get("status") or "")
    if stored in ("received", "applied", "no_change", "replied_text"):
        return stored
    return "sent"


def explanation_hint(record: dict | None) -> bool:
    """Подсказка для журнала: текст старой записи похож на просьбу пояснить.

    На статус ответа НЕ влияет (W17-B §2): решение принимает только явная кнопка
    «Мне нужно пояснение». Нужна, чтобы в журнале было видно, какие старые ответы
    стоит перечитать.
    """
    record = record if isinstance(record, dict) else {}
    if "answered" in record or "status" in record:
        return False                     # новая запись: смысл уже записан явно
    return is_counter_question(record.get("text"))


def apply_question(question: dict, record, reaction=None, now: int | None = None,
                   explanation=None) -> dict:
    """Наложить запись ответа и реакцию на вопрос (мутирует и возвращает его).

    W30-A §5: ``explanation`` — пояснение руководителя (текст из метки
    ``[Руководитель · пояснение по вопросу <id>]``). Пока ответа нет, вопрос
    получает состояние ``explained`` («пояснено — выберите ответ»): текст вопроса
    не меняется, но человек видит объяснение прямо под ним и кнопки вариантов
    активны. С ответом пояснение остаётся полем — оно не перебивает статус.
    """
    question = question if isinstance(question, dict) else {}
    record = record if isinstance(record, dict) else {}
    text = norm(record.get("text"))
    at = int(record.get("at") or 0) or None
    state = state_of(record, reaction)
    explained = explanation if isinstance(explanation, dict) else {}
    explained_text = norm(explained.get("text"))
    if explained_text:
        question["explanation"] = explained_text
        question["explained_at"] = int(explained.get("at") or 0) or None
        question["explained_by"] = str(explained.get("author") or "") or None
    else:
        question["explanation"] = None
        question["explained_at"] = None
        question["explained_by"] = None
    # «Пояснено — выберите ответ» только пока вопрос реально ждёт ответа.
    # W30-QA2: живой поток «Пояснить → пояснение руководителя» раньше застревал:
    # просьба о пояснении записывается как запись со статусом
    # `waiting_explanation`, и пояснение её не снимало — вопрос навсегда оставался
    # «ждёт пояснения» с кнопкой «Напомнить», хотя руководитель уже объяснил.
    # Пояснение СВЕЖЕЕ просьбы → вопрос пояснён и ждёт ответа человека.
    explained_at = int(explained.get("at") or 0) or 0
    if explained_text and state == "open":
        state = "explained"
    elif (explained_text and state == "waiting_explanation"
          and (not explained_at or explained_at >= int(at or 0))):
        state = "explained"
    answered = bool(text) and state not in ("waiting_explanation", "open", "dismissed",
                                            "postponed", "explained")
    question["answered"] = answered
    question["answer"] = text or None
    question["answered_at"] = at if answered else None
    question["answer_state"] = state
    question["answer_label"] = state_label(state, (reaction or {}).get("cards") if isinstance(reaction, dict) else None,
                                           (reaction or {}).get("changes") if isinstance(reaction, dict) else None)
    question["answer_hint"] = state_hint(state)
    question["answer_chain_hint"] = CHAIN_HINT
    question["needs_explanation"] = state == "waiting_explanation"
    question["explanation_requested_at"] = at if state == "waiting_explanation" else None
    question["explanation_text"] = text or None if state == "waiting_explanation" else None
    question["reaction"] = dict(reaction) if isinstance(reaction, dict) else None
    question["reminded_at"] = int(record.get("reminded_at") or 0) or None
    question["leader_pending"] = state == "sent"
    # W20-D1 §1.2 (B-38, B-41): «это не вопрос» и «ответил текстом» — состояния, а не
    # повод считать элемент ждущим ответа. `needs_answer` — единственный признак для
    # «Нужен ваш ответ» и счётчиков (фронт не пересчитывает по-своему).
    question["dismissed"] = state == "dismissed"
    question["is_question"] = not question["dismissed"]
    question["answered_text_only"] = state == "replied_text"
    # W30-A §1: отложенный вопрос не «ждёт вас сейчас» — у него своё состояние,
    # и в счётчики «нужен ответ» он не входит, пока срок не прошёл.
    question["needs_answer"] = bool(not answered and state not in ("dismissed", "replied_text",
                                                                  "postponed"))
    # Срок «Отложить» живёт в записи реестра, а состояние читается по вопросу
    # (`stale.question_state`) — иначе отложенный пункт снова считался бы «ждёт вас».
    until = record.get("postpone_until")
    question["postpone_until"] = (int(until) if state == "postponed"
                                   and isinstance(until, (int, float)) and until > 0 else None)
    question["postponed_at"] = (int(record.get("postponed_at") or 0) or None
                                if state == "postponed" else None)
    # «за разумное время» реакции нет — подсвечиваем, что пора напомнить
    waited = max(0, int(now or _time.time()) - at) if (state == "sent" and at) else 0
    question["reaction_waited_s"] = waited if state == "sent" else None
    question["reaction_overdue"] = bool(state == "sent" and waited >= REACTION_WAIT_S)
    question["can_remind"] = state == "sent"
    question["answer_changes_done"] = bool(
        question["reaction"] and str(question["reaction"].get("status")) == "applied"
        and question["reaction"].get("cards"))
    return question


def answer_item(question: dict) -> dict:
    """Плоская запись ответа для блока ``detail.answers`` (список «вопрос — ответ»)."""
    question = question if isinstance(question, dict) else {}
    return {"question_id": question.get("id"), "question_text": question.get("text") or "",
            "answer": question.get("answer"), "at": question.get("answered_at"),
            "state": question.get("answer_state") or "open",
            "label": question.get("answer_label") or STATE_RU["open"],
            "hint": question.get("answer_hint") or state_hint(question.get("answer_state")),
            "reaction": question.get("reaction"), "needs_explanation": bool(question.get("needs_explanation"))}


# ------------------------------------------------------------ связка шаг↔вопрос ---

def _best_question(step: dict, questions: list) -> tuple[dict | None, int]:
    words = stems(step.get("text"))
    best, score = None, 0
    for question in questions:
        if not isinstance(question, dict) or not question.get("id"):
            continue
        common = len(words & stems(question.get("text")))
        if common > score:
            best, score = question, common
    return (best, score) if score >= 2 else (None, score)


#: порог совпадения основ слов, при котором связь «шаг ↔ вопрос» РАБОТАЕТ
#: (снимает шаг, ставит метку конфликта). Ниже порога связь только показывается:
#: снять чужой шаг по неточному совпадению хуже, чем не снять.
LINK_MIN_SCORE = 2
ACTION_MIN_SCORE = 3


def link_question(step: dict, questions: list, min_score: int = LINK_MIN_SCORE) -> tuple[dict | None, str]:
    """Вопрос-источник шага: явный ``from_question`` или совпадение текста."""
    explicit = norm(step.get("from_question"))
    if explicit:
        for question in questions:
            if isinstance(question, dict) and str(question.get("id")) == explicit:
                return question, "explicit"
    found, score = _best_question(step, questions)
    return (found, "text") if found and score >= min_score else (None, "")


def link_steps(steps, questions, answers=None, dropped=None, record=None, thread=None) -> list:
    """Обогатить шаги: источник-вопрос, ответ, «снят по вашему ответу», конфликт.

    * шаг, связанный с ответом-отказом и существовавший на момент ответа →
      ``state="cancelled"`` («снят по вашему ответу»);
    * шаг, появившийся ПОСЛЕ ответа-отказа (или связь найдена, а снимка шагов нет) →
      ``conflicts_with_answer`` + ``can_remove`` («Убрать»);
    * шаг из ``dropped`` (Михаил убрал) → ``state="dismissed"``;
    * действие пользователя по шагу (``agree|comment|move|drop``) и реакция
      руководителя на него — из реестра (``record["step_actions"]``,
      ``record["steps_log"]``, ``record["steps_order"]``) и ленты (``thread``).
    """
    answers = answers if isinstance(answers, dict) else {}
    record = record if isinstance(record, dict) else {}
    dropped = {str(item) for item in (dropped or [])}
    actions = record.get("step_actions") if isinstance(record.get("step_actions"), dict) else {}
    order = record.get("steps_order") if isinstance(record.get("steps_order"), list) else []
    log = record.get("steps_log") if isinstance(record.get("steps_log"), list) else []
    reactions = step_reactions_from_thread(thread)
    questions = [q for q in (questions or []) if isinstance(q, dict) and q.get("id")]
    out = []
    for raw in steps or []:
        if not isinstance(raw, dict):
            continue
        step = dict(raw)
        step_id = str(step.get("id") or "")
        question, how = link_question(step, questions)
        decisive, decisive_how = link_question(step, questions, ACTION_MIN_SCORE)
        record_q = answers.get(str(question.get("id"))) if question else None
        record_q = record_q if isinstance(record_q, dict) else {}
        answered = bool(question and question.get("answer_state") not in (None, "", "open",
                                                                         "waiting_explanation")
                        and norm(record_q.get("text")))
        negated = answered and is_negation(record_q.get("text"))
        snapshot = [str(item) for item in (record_q.get("steps_at_answer") or [])]
        step["from_question"] = (question or {}).get("id")
        step["question_link"] = how or None
        step["question"] = ({"id": question.get("id"), "text": question.get("text") or ""}
                            if question else None)
        step["answer"] = norm(record_q.get("text")) or None
        step["cancelled_by_answer"] = False
        step["cancelled_reason"] = None
        step["conflicts_with_answer"] = False
        step["can_remove"] = True
        if step_id in dropped:
            step["state"] = "dismissed"
            step["state_label"] = "убран"
        elif negated and decisive_how:
            # точная связь (явный from_question или сильное совпадение текста)
            if snapshot and str(step.get("id") or "") in snapshot:
                step["state"] = "cancelled"
                step["state_label"] = CANCELLED_RU
                step["cancelled_by_answer"] = True
                step["cancelled_reason"] = CANCELLED_RU
            else:
                step["conflicts_with_answer"] = True
                step["state_label"] = CONFLICT_RU
        else:
            step["state_label"] = {"done": "сделано", "cancelled": CANCELLED_RU,
                                   "dismissed": "убран"}.get(str(step.get("state")), "предстоит")
        apply_step(step, actions.get(step_id), reactions.get(step_id),
                   steps_log_for(log, step_id))
        out.append(step)
    return sort_steps(out, order)


def sort_steps(steps, order) -> list:
    """Порядок шагов из реестра («Переместить»): сначала явный порядок, остальные — как есть."""
    order = [str(item) for item in (order or [])]
    out = [s for s in steps or [] if isinstance(s, dict)]
    if not order:
        for index, step in enumerate(out):
            step["order"] = index
        return out
    by_id = {}
    for step in out:
        by_id[str(step.get("id") or "")] = step
    ordered = []
    for step_id in order:
        step = by_id.pop(step_id, None)
        if step is not None:
            ordered.append(step)
    rest = [step for step in out if str(step.get("id") or "") in by_id]
    result = ordered + rest
    for index, step in enumerate(result):
        step["order"] = index
    return result


# ------------------------------------------- действия пользователя по шагу ---
#
# Тот же механизм, что у вопросов (§5/§6/§12): каждое действие с шагом уходит
# руководителю КАК ОТВЕТ НА ЭТОТ ШАГ (метка в ветке), а его реакция
# (``[Руководитель · реакция на шаг ns_x]``) видна прямо в шаге.

STEP_AGREE_MARK = "[Михаил · согласен с шагом"
STEP_DROP_MARK = "[Михаил · убрать шаг"
STEP_COMMENT_MARK = "[Михаил · комментарий к шагу"
STEP_MOVE_MARK = "[Михаил · переместить шаг"
STEP_REACTION_MARK = "[Руководитель · реакция на шаг"

STEP_KINDS = ("agree", "comment", "move", "drop", "restore")
STEP_KIND_RU = {"agree": "вы согласовали", "comment": "ваш комментарий",
                "move": "вы переместили", "drop": "вы убрали",
                # W27-E1 §1: «Вернуть» — шаг снова в работе, это тоже ответ на шаг
                "restore": "вы вернули"}
STEP_KIND_VERB = {"agree": "согласовано вами", "comment": "прокомментировано вами",
                  "move": "перемещено вами", "drop": "убрано вами",
                  "restore": "возвращено вами"}
STEP_STATUS_RU = {"open": "предстоит", "asked": "спрашивает", "done": "сделано",
                  "dismissed": "убран", "cancelled": CANCELLED_RU}

_SID_RE = re.compile(r"(ns_[0-9A-Za-z_\-]+)")


def step_id_of(text) -> str:
    """id шага из метки (``ns_1``) или ''."""
    match = _SID_RE.search(str(text or ""))
    return match.group(1) if match else ""


def step_comment(kind, step_id, step_text, text="", due=None, owner=None, direction=None) -> str:
    """Метка действия по шагу в ветке: руководитель видит, к какому шагу это относится."""
    head = {"agree": STEP_AGREE_MARK, "drop": STEP_DROP_MARK,
            "comment": STEP_COMMENT_MARK, "move": STEP_MOVE_MARK,
            # W27-E1 §1: «Вернуть» — шаг снова в работе (обратное «Убрать»)
            "restore": RESTORE_MARK}.get(str(kind))
    if not head:
        raise ValueError("неизвестное действие по шагу: %r" % (kind,))
    mark = "%s %s «%s»]" % (head, step_id, short(step_text, 120))
    if kind == "comment":
        return "%s %s" % (mark, norm(text))
    if kind == "move":
        what = []
        if due is not None:
            what.append("срок %s" % (due or "убран"))
        if owner:
            what.append("исполнитель %s" % owner)
        if direction:
            what.append("выше" if direction == "up" else "ниже")
        return "%s %s" % (mark, ", ".join(what) or "порядок")
    return mark


def step_reaction_state(text) -> str:
    """Класс реакции руководителя по шагу: removed / moved / no_change / received."""
    value = norm(text).lower()
    if any(word in value for word in ("шаг убран", "шаг снят", "убираю шаг", "снимаю шаг", "убран")):
        return "removed"
    if any(word in value for word in ("срок", "дата", "перенес", "перенесл", "порядок", "перестав")):
        return "moved"
    if any(word in value for word in ("ничего менять не нужно", "менять не нужно", "без изменений",
                                      "ничего не меняем", "изменения не нужны")):
        return "no_change"
    return reaction_state(text)


def step_reaction_label(kind, status, due_changed=False) -> str:
    """Видимая подпись реакции на действие по шагу (§12; W19-D2 — слова цепочки B-21).

    Те же слова, что у вопроса и решения: «ответ отправлен» → «руководитель прочитал» →
    «учтено: работа изменена» / «учтено: менять не нужно». Технические подписи
    «срок изменён»/«порядок изменён»/«шаг убран» остаются как были.
    """
    kind = str(kind or "")
    status = str(status or "")
    if kind == "drop" or status == "removed":
        return "шаг убран"
    if status == "moved" or kind == "move":
        return "срок изменён" if due_changed else "порядок изменён"
    if status == "no_change":
        return "учтено: менять не нужно"
    if status == "applied":
        return STATE_RU["applied"]
    if status == "received":
        return STATE_RU["received"]
    return PENDING_RU


def step_reactions_from_thread(items) -> dict:
    """Реакции руководителя на действия по шагам: ``{ns_id: {text, at, status, label, cards}}``."""
    out: dict = {}
    for item in items or []:
        if not isinstance(item, dict) or item.get("author_kind") != "agent":
            continue
        text = norm(item.get("text"))
        if not text or not text.startswith(STEP_REACTION_MARK):
            continue
        step_id = step_id_of(text)
        if not step_id:
            continue
        at = int(item.get("at") or 0)
        previous = out.get(step_id)
        if previous and int(previous.get("at") or 0) > at:
            continue
        status = step_reaction_state(text)
        out[step_id] = {"text": answer_text_of(text), "at": at, "status": status,
                        "label": step_reaction_label("", status),
                        "cards": _cards_of(text), "author": item.get("author") or "",
                        "author_label": item.get("author_label") or "", "implicit": False}
    return out


def apply_step(step: dict, action=None, reaction=None, history=None, now: int | None = None) -> dict:
    """Наложить на шаг действие пользователя, реакцию руководителя и историю.

    Действие пользователя (``agree|comment|move|drop``) — это ОТВЕТ на этот шаг
    (механизм §5/§6): пока руководитель не отреагировал, шаг показывает
    «руководитель ещё не ответил»; после — «принято / учтено / срок изменён /
    шаг убран». «Переместить» меняет срок/исполнителя/порядок прямо в шаге.
    """
    step = step if isinstance(step, dict) else {}
    action = action if isinstance(action, dict) else {}
    moment = int(now or _time.time())
    step["state"] = str(step.get("state") or "open")
    step.setdefault("state_label", STEP_STATUS_RU.get(step["state"], "предстоит"))
    kind = str(action.get("kind") or "")
    due_changed = bool(kind == "move" and "due" in action)
    if action:
        step["user_action"] = {"kind": kind, "at": int(action.get("at") or 0) or None,
                               "text": norm(action.get("text")) or None,
                               "due": action.get("due"), "owner": action.get("owner") or None,
                               "direction": action.get("direction") or None,
                               "label": STEP_KIND_RU.get(kind, kind)}
        step["user_action_label"] = STEP_KIND_VERB.get(kind, "")
        step["user_action_at"] = step["user_action"]["at"]
        if kind == "move":
            if "due" in action:
                step["due"] = action.get("due") or None
                step["due_changed_by_user"] = True
            if action.get("owner"):
                step["owner"] = action.get("owner")
                step["owner_changed_by_user"] = True
    reaction = reaction if isinstance(reaction, dict) else None
    step["reaction"] = dict(reaction) if reaction else None
    step["reaction_status"] = (reaction or {}).get("status")
    pending = bool(kind) and kind != "drop" and not reaction
    if pending:
        step["reaction_label"] = PENDING_RU
    elif kind or reaction:
        step["reaction_label"] = step_reaction_label(kind, (reaction or {}).get("status"), due_changed)
    else:
        # Ни действия пользователя, ни реакции ещё не было — подписи реакции нет.
        step["reaction_label"] = None
    step["reaction_pending"] = pending
    step["reaction_label_pending"] = PENDING_RU if pending else None
    waited = 0
    if pending and step["user_action_at"]:
        waited = max(0, moment - int(step["user_action_at"]))
    step["reaction_waited_s"] = waited if pending else None
    step["reaction_overdue"] = bool(pending and waited >= REACTION_WAIT_S)
    events = []
    for record in history or []:
        if not isinstance(record, dict):
            continue
        events.append({"at": int(record.get("at") or 0) or None,
                       "kind": str(record.get("kind") or ""),
                       "label": STEP_KIND_VERB.get(str(record.get("kind") or ""),
                                                  STEP_KIND_RU.get(str(record.get("kind") or ""), "")),
                       "text": norm(record.get("text")) or None,
                       "due": record.get("due"), "owner": record.get("owner"),
                       "direction": record.get("direction")})
    if reaction:
        events.append({"at": reaction.get("at"), "kind": "reaction", "label": "реакция руководителя",
                       "text": reaction.get("text"), "due": None, "owner": None, "direction": None})
    events.sort(key=lambda e: int(e.get("at") or 0))
    step["history"] = events
    step["can_agree"] = step["state"] not in ("done", "dismissed", "cancelled")
    step["can_drop"] = step["state"] not in ("done", "dismissed", "cancelled")
    return step


def steps_log_for(log, step_id) -> list:
    """История действий по шагу из журнала реестра (``steps_log``)."""
    return [record for record in (log or [])
            if isinstance(record, dict) and str(record.get("step_id")) == str(step_id)]


def steps_to_cancel(steps, question, answer_text) -> list:
    """id шагов, которые снимаются ответом-отказом (на момент ответа они уже были)."""
    if not is_negation(answer_text):
        return []
    questions = [question] if isinstance(question, dict) else []
    out = []
    for step in steps or []:
        if not isinstance(step, dict) or not step.get("id"):
            continue
        linked, _how = link_question(step, questions, ACTION_MIN_SCORE)
        if linked and str(step.get("state") or "open") not in ("done", "dismissed", "cancelled"):
            out.append(str(step["id"]))
    return out


# ------------------------------------------------------------ решения (W19-D2) ---
# B-20: «решение предложено» — это тоже то, что ждёт слова Михаила. Действия
# («Принять», «Отклонить», «Обсудить») уходят руководителю ТЕМ ЖЕ механизмом, что
# ответы на вопросы и действия по шагам: метка в ветке
# ``[Михаил · решение d_x «текст решения»] Принять: …``, запись в реестре
# (``answers[d_x]`` с ``kind="decision"``) и видимый статус ответа
# («ответ отправлен → руководитель прочитал → учтено»).
# B-21: исход задаёт руководитель (``metadata.reaction.outcome``), слова — запасной
# вариант для старых записей.

DECISION_MARK = "[Михаил · решение"
DECISION_REACTION_MARK = "[Руководитель · реакция на решение"
#: W20-D1 §1.2 (B-37): «Снять — неактуально» и «Вернуть» рядом с принять/отклонить/обсудить.
DECISION_ACTIONS = ("accept", "reject", "discuss", "dismiss", "restore")
DECISION_ACTION_RU = {"accept": "Принять", "reject": "Отклонить", "discuss": "Обсудить",
                      "dismiss": "Снять — неактуально", "restore": "Вернуть"}
DECISION_ACTION_HINT = {
    "accept": "Согласиться с предложенным решением — руководитель увидит это сразу",
    "reject": "Отклонить решение: можно сразу написать, что предложить вместо",
    "discuss": "Написать или надиктовать, что обсудить — решение останется за вами",
    "dismiss": "Убрать устаревшее или не ваше решение: уйдёт из «Нужен ваш ответ», "
               "в списке «Решения» останется серым — можно вернуть",
    "restore": "Вернуть снятое решение в работу: снова «предложено»",
}
DECISION_STATUS_RU = {"proposed": "предложено", "accepted": "принято вами",
                      "rejected": "отклонено вами", "discuss": "обсуждаем с руководителем",
                      # W20-D1 §1.2 (B-37): снятое и устаревшее — не ждут ответа
                      "dismissed": "снято — не актуально", "superseded": "устарело"}
DECISION_STATUS_HINT = {
    "proposed": "Руководитель предлагает так; решение за вами — «Принять», «Отклонить» "
                "или «Обсудить».",
    "accepted": "Вы приняли решение. Руководитель получил ваш ответ и учтёт его в работе.",
    "rejected": "Вы отклонили решение. Руководитель получил ответ и предложит другой вариант.",
    "discuss": "Вы просите обсудить: решение остаётся за вами, пока руководитель не ответит.",
    "dismissed": "Вы сняли решение как неактуальное: оно не ждёт ответа и не считается "
                 "в счётчиках. Вернуть можно в любой момент.",
    "superseded": "Решение больше не действует: руководитель его отозвал или в новой версии "
                  "свода его нет. В работу оно не идёт.",
}
#: статусы, которые НЕ ждут слова владельца и не попадают в «Нужен ваш ответ»
DECISION_CLOSED = ("accepted", "rejected", "dismissed", "superseded")
#: статусы, которые показываются серым (снято/устарело)
DECISION_MUTED = ("dismissed", "superseded")
_SILENT_STATUSES = DECISION_MUTED
DECISION_STATUSES = tuple(DECISION_STATUS_RU)
_STATE_OF_DECISION_STATUS = {"accept": "accepted", "reject": "rejected", "discuss": "discuss",
                            "dismiss": "dismissed", "restore": "proposed"}
_STATUS_OF_ACTION = {"accept": "accepted", "reject": "rejected", "discuss": "discuss"}
_ACTION_OF_STATUS = {"accepted": "accept", "rejected": "reject", "discuss": "discuss"}

_DID_RE = re.compile(r"(d_[0-9A-Za-z_\-]+)")


def decision_id_of(text) -> str:
    """id решения из метки (``d_1``) или ''."""
    match = _DID_RE.search(str(text or ""))
    return match.group(1) if match else ""


def decision_kind_of(text) -> bool:
    """Запись ветки — решение Михаила (метка ``[Михаил · решение …]``)."""
    return str(text or "").startswith(DECISION_MARK)


def decision_action_of(text) -> str:
    """Действие из метки решения: ``accept|reject|discuss`` или ''."""
    value = str(text or "")
    if not value.startswith(DECISION_MARK):
        return ""
    tail = value.split("]", 1)[1].strip().lower() if "]" in value else ""
    for action, word in (("accept", "принять"), ("reject", "отклонить"), ("discuss", "обсудить")):
        if tail.startswith(word):
            return action
    return ""


def decision_comment(action, decision_id, decision_text, text="") -> str:
    """Метка решения в ветке: видно, какое решение и что с ним сделал Михаил."""
    label = DECISION_ACTION_RU.get(str(action), "Решение")
    head = "%s %s «%s»]" % (DECISION_MARK, str(decision_id or "d_0"), short(decision_text, 120))
    body = norm(text)
    return "%s %s: %s" % (head, label, body) if body else "%s %s" % (head, label)


def stale_comment(action: str, kind: str, ident, text="", reason=None) -> str:
    """Метка «Не актуально» / «Вернуть» для любого пункта (W27-E1, контракт §1).

    Одна формулировка на вопросы, решения, разрешения и шаги: по метке видно, что
    человек не принимал решение, а всего лишь убрал/вернул пункт — и почему.
    """
    action = str(action or "dismiss").strip().lower()
    kind_ru = {"question": "вопрос", "decision": "решение",
               "approval": "запрос разрешения", "step": "шаг"}.get(str(kind or ""), "пункт")
    head = ("%s: %s %s «%s»]"
            % (STALE_MARK if action != "restore" else RESTORE_MARK, kind_ru,
               str(ident or ""), short(text, 120)))
    body = norm(reason)
    tail = ("Вернул в работу" if action == "restore" else "Решение не принималось")
    return "%s %s%s" % (head, tail, (": " + body) if body else "")


def decision_reaction_comment(decision_id, text) -> str:
    """Готовая метка реакции руководителя на решение (пишет он сам в ветке)."""
    return "%s %s] %s" % (DECISION_REACTION_MARK, decision_id, norm(text))


def decision_reactions_from_thread(items, lead=None) -> dict:
    """Реакции руководителя на решения из ленты: ``{d_id: {text, at, status, …}}``.

    Исход — явный (``outcome=…`` в тексте) или, для старых записей, по словам.
    Автор — руководитель комнаты (``lead``), как у реакций на ответы (B-12).
    """
    out: dict = {}
    leads = _lead_names(lead)
    for item in items or []:
        if not _from_leader(item, leads):
            continue
        text = norm(item.get("text"))
        if not text.startswith(DECISION_REACTION_MARK):
            continue
        did = decision_id_of(text)
        if not did:
            continue
        at = int(item.get("at") or 0)
        previous = out.get(did)
        if previous and int(previous.get("at") or 0) > at:
            continue
        status = reaction_state(text)
        cards = _cards_of(text)
        out[did] = {"text": answer_text_of(text), "at": at, "status": status,
                    "label": state_label(status, cards), "cards": cards,
                    "outcome": outcome_of(text) or None, "guessed": not outcome_of(text),
                    "changes": len(cards) or None, "source": "thread",
                    "author": item.get("author") or "",
                    "author_label": item.get("author_label") or "", "implicit": False,
                    "task_id": item.get("task_id")}
    return out


def _local_decision_id(value) -> str:
    """``t_825ada4c:d_1`` → ``d_1`` (в реестре и метках живёт короткий id)."""
    text = str(value or "").strip()
    return text.split(":", 1)[1] if ":" in text else text


def decision_index(records) -> dict:
    """Реестр ответов комнаты → ``{d_id: запись решения}`` (для бэклога и детали)."""
    out: dict = {}
    for record in records or []:
        if not isinstance(record, dict):
            continue
        stored = record.get("answers")
        if not isinstance(stored, dict):
            continue
        for key, entry in stored.items():
            if not isinstance(entry, dict):
                continue
            if str(entry.get("kind") or "") != "decision" and not str(key).startswith("d_"):
                continue
            out[_local_decision_id(key)] = entry
    return out


def decision_record_for(decision, records) -> dict | None:
    """Запись решения из реестра ответов (по id, с учётом префикса карточки)."""
    if not isinstance(records, dict) or not isinstance(decision, dict):
        return None
    did = _local_decision_id(decision.get("id"))
    entry = records.get(did)
    if not isinstance(entry, dict):
        for key in (str(decision.get("id") or ""), did):
            if key and isinstance(records.get(key), dict):
                entry = records[key]
                break
    if isinstance(entry, dict):
        return entry
    for key, value in records.items():
        if isinstance(value, dict) and str(value.get("kind")) == "decision" \
                and _local_decision_id(value.get("decision_id") or key) == did:
            return value
    return None


def apply_decision(decision: dict, record=None, reaction=None, now: int | None = None) -> dict:
    """Наложить на решение действие Михаила и реакцию руководителя (B-20, B-21, B-37).

    * ``status`` — ``proposed`` (ждёт слова), ``accepted``/``rejected`` (решено
      Михаилом), ``discuss`` (обсуждаем: решение всё ещё за ним), ``dismissed``
      («Снять — неактуально», B-37) и ``superseded`` («устарело»: руководитель отозвал
      решение или его нет в новой версии свода);
    * ``open`` — ждёт ли решение слова владельца (``dismissed``/``superseded`` — нет);
    * ``answer_state``/``answer_label``/``answer_hint`` — цепочка ответа
      («ответ отправлен → руководитель прочитал → учтено»);
    * ``leader_pending``, ``can_remind``, ``reaction`` — то же, что у вопросов.

    История действий (``record["history"]``) важна: «снял → вернул» оставляет снятие
    в журнале, а решает ПОСЛЕДНЕЕ действие пользователя.
    """
    d = dict(decision) if isinstance(decision, dict) else {}
    rec = record if isinstance(record, dict) else {}
    moment = int(now or _time.time())
    status = str(d.get("status") or "proposed")
    if status not in DECISION_STATUSES:
        status = "proposed"
    if not rec and (d.get("answer_text") or d.get("answered_at")):
        # Запись ответа потеряна (старый формат/фикстура), но ответ виден в самом
        # решении — читаем его оттуда, не приписывая ответ руководителю.
        rec = {"kind": "decision", "decision_id": d.get("id"), "answered": True,
               "text": norm(d.get("answer_text")), "at": int(d.get("answered_at") or 0) or None,
               "status": "sent",
               "action": _ACTION_OF_STATUS.get(status) if status in ("accepted", "rejected", "discuss") else None}
    history = [item for item in (rec.get("history") or []) if isinstance(item, dict)]
    action = str(rec.get("action") or "").strip() \
        or _ACTION_OF_STATUS.get(str(rec.get("outcome") or ""), "")
    if history:
        action = str((history[-1] or {}).get("action") or action)
    elif action:
        history = [{"action": action, "at": int(rec.get("at") or 0) or None, "text": None}]
    previous = status
    dismissed_at = None
    if action == "dismiss":
        status, dismissed_at = "dismissed", int(rec.get("at") or 0) or None
        for item in history:
            if str(item.get("action")) == "dismiss":
                dismissed_at = int(item.get("at") or 0) or dismissed_at
    elif action == "restore":
        # вернули в работу: снова «предложено» (или «обсуждаем», если так и было)
        status = "discuss" if previous == "discuss" else "proposed"
    elif action in _STATUS_OF_ACTION:
        status = _STATUS_OF_ACTION[action]
    superseded = d.get("superseded") if isinstance(d.get("superseded"), dict) else None
    if superseded:
        # W20-D1 §1.2 (B-37): отзыв руководителем / отсутствие в новой версии свода
        # сильнее любого прежнего действия пользователя.
        previous, status = status, "superseded"
    reaction = dict(reaction) if isinstance(reaction, dict) else None
    state = state_of(rec, reaction)
    if state == "open" and reaction and str(reaction.get("status")) in ("received", "applied",
                                                                        "no_change", "replied_text"):
        # реакция есть, запись ответа потеряна — цепочка всё равно видна
        state = str(reaction["status"])
    at = int(rec.get("at") or 0) or None
    texts = norm(rec.get("text")) or None
    cards = (reaction or {}).get("cards") or []
    changes = (reaction or {}).get("changes")
    waited = max(0, moment - at) if (state == "sent" and at) else 0
    muted = status in DECISION_MUTED
    if muted:
        state, texts, at = status, None, None
    d.update({
        "status": status,
        "status_label": (DECISION_STATUS_RU.get(status, DECISION_STATUS_RU["proposed"])
                         if action or muted or status not in ("accepted", "rejected")
                         else "принято руководителем" if status == "accepted" else "отклонено руководителем"),
        "status_hint": (DECISION_STATUS_HINT.get(status, DECISION_STATUS_HINT["proposed"])
                        if muted or action or not status in ("accepted", "rejected")
                        else "Руководитель отметил это решение сам; вашего ответа здесь не требовалось."),
        "status_source": "leader" if (superseded or (not action and status in ("accepted", "rejected"))) else "user",
        "status_muted": bool(muted),
        "previous_status": previous if previous != status else None,
        "open": status in ("proposed", "discuss"),
        "needs_answer": status in ("proposed", "discuss"),
        "answered": bool(rec),
        "dismissed": status == "dismissed",
        "dismissed_at": dismissed_at if status == "dismissed" else None,
        "superseded": bool(superseded),
        "superseded_by": (superseded or {}).get("by") if superseded else None,
        "superseded_at": (int((superseded or {}).get("at") or 0) or None) if superseded else None,
        "superseded_why": (superseded or {}).get("why") if superseded else None,
        "answer_action": action or None,
        "answer_action_label": DECISION_ACTION_RU.get(action) if action else None,
        "answer_text": texts,
        "answered_at": at,
        "answer_state": state,
        "answer_label": (state_label(state, cards, changes) if (rec or reaction) else None),
        "answer_hint": state_hint(state) if (rec or reaction) else None,
        "reaction": reaction,
        "leader_pending": state == "sent",
        "can_remind": state == "sent",
        "reaction_waited_s": waited if state == "sent" else None,
        "reaction_overdue": bool(state == "sent" and waited >= REACTION_WAIT_S),
        "can_accept": status in ("proposed", "discuss"),
        "can_reject": status in ("proposed", "discuss"),
        "can_discuss": status in ("proposed", "discuss"),
        # W20-D1 (B-37): «Снять — неактуально» есть у всего, что ещё не устарело, и
        # «Вернуть» — у снятого. Кнопки живут отдельно от трёх основных действий,
        # чтобы старые клиенты не получили незнакомый элемент в списке действий.
        "can_dismiss": status in ("proposed", "discuss", "accepted", "rejected"),
        "can_restore": status == "dismissed",
        "actions": [{"action": key, "label": DECISION_ACTION_RU[key], "hint": DECISION_ACTION_HINT[key],
                     "current": key == action} for key in ("accept", "reject", "discuss")],
        "muted_actions": [
            {"action": key, "label": DECISION_ACTION_RU[key], "hint": DECISION_ACTION_HINT[key],
             "available": bool(d.get("can_" + key))}
            for key in ("dismiss", "restore")
        ],
    })
    return d


def decision_summary(decisions) -> dict:
    """Сколько решений ждут слова Михаила и сколько уже решено (для подписей).

    W20-D1 (B-37): снятые (``dismissed``) и устаревшие (``superseded``) считаются
    закрытыми и не попадают в «Нужен ваш ответ»; их число отдаём отдельно, чтобы
    блок «Решения» мог сказать «2 снято, 1 устарело».
    """
    open_count = closed = dismissed = superseded = 0
    for item in decisions or []:
        if not isinstance(item, dict):
            continue
        status = str(item.get("status") or "proposed")
        if status in ("proposed", "discuss"):
            open_count += 1
            continue
        closed += 1
        if status == "dismissed":
            dismissed += 1
        elif status == "superseded":
            superseded += 1
    return {"total": open_count + closed, "open": open_count, "closed": closed,
            "dismissed": dismissed, "superseded": superseded,
            "muted": dismissed + superseded}


# ------------------------------------- версии свода: автоматическое «устарело» ---
#
# W20-D1 §1.2 (B-37 п.2): решение перестаёт действовать в двух случаях — руководитель
# отозвал его (``metadata.decisions_retracted:[d_…]`` в ЛЮБОЙ своей карточке) или его
# нет в новой версии свода. Данные не переписываются: «устарело» — наложение при чтении.
#
# W21-S5 (B-50): «решения нет в новой версии» действует ТОЛЬКО если руководитель прямо
# заявил, что перечислил полный список — ``metadata.svod.decisions_full = true``.
# Без флага список решений карточки — дополнение (он пишет только новое/открытое), а не
# сведение: иначе одно новое решение гасило все прежние (на боевых данных 12.11 два
# принятых решения посерели именно так). Явный отзыв работает как раньше.


def _truthy_flag(value) -> bool:
    """Флаг из metadata: ``true``/``1``/``"да"`` — да; пусто и ``false`` — нет."""
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    text = str(value or "").strip().lower()
    return text in ("true", "1", "yes", "да", "истина", "on")


def _decision_ids_of(raw) -> list:
    """Список решений из metadata/канона → id (строка/словарь/список)."""
    out: list = []
    if isinstance(raw, dict):
        raw = [raw]
    for item in raw if isinstance(raw, list) else []:
        if isinstance(item, str):
            value = item.strip()
            if value:
                out.append(value)
            continue
        if isinstance(item, dict) and item.get("id"):
            out.append(str(item["id"]).strip())
    return out


def _card_decisions(canon) -> list:
    """Решения карточки в порядке свода: ``[{id, text, why, status}]``."""
    out: list = []
    for index, item in enumerate((canon or {}).get("decisions") or [], start=1):
        if isinstance(item, str):
            item = {"text": item}
        if not isinstance(item, dict) or not str(item.get("text") or "").strip():
            continue
        out.append({"id": str(item.get("id") or "d_%d" % index).strip(),
                    "text": norm(item.get("text")),
                    "why": item.get("why"),
                    "status": str(item.get("status") or "proposed")})
    return out


def decision_versions(nodes, canons=None) -> list:
    """Версии свода с решениями: ``[{version, at, id, decisions, retracted, full}]``.

    ``nodes`` — дерево запроса (у каждой карточки ``metadata``), ``canons`` — список
    ``(node, canon)`` из чтения. Версия берётся из ``metadata.svod.version``, если
    руководитель её указал; иначе — порядковый номер карточки с решениями. Карточки
    без решений и без отзывов в историю не попадают (это не новая версия свода).

    ``full`` (W21-S5, B-50) — руководитель заявил ``metadata.svod.decisions_full=true``,
    то есть перечислил ПОЛНЫЙ список решений. Только такая версия может объявить
    прежние решения устаревшими; без флага список — дополнение.
    """
    by_node = {}
    for pair in canons or []:
        if isinstance(pair, (list, tuple)) and len(pair) == 2 and isinstance(pair[1], dict):
            node = pair[0] if isinstance(pair[0], dict) else {}
            by_node[str(node.get("id") or "")] = pair[1]
    out: list = []
    seen_nodes = set()
    cards = [node for node in nodes or [] if isinstance(node, dict)]
    cards.sort(key=lambda node: int(node.get("ended_at") or node.get("at") or 0))
    for node in cards:
        node_id = str(node.get("id") or "")
        canon = by_node.get(node_id)
        if canon is None:
            canon = node.get("canon") if isinstance(node.get("canon"), dict) else {}
        meta = node.get("metadata") if isinstance(node.get("metadata"), dict) else {}
        extra = meta.get("extra") if isinstance(meta.get("extra"), dict) else {}
        decisions = _card_decisions(canon)
        retracted = _decision_ids_of(meta.get("decisions_retracted")) \
            or _decision_ids_of(extra.get("decisions_retracted"))
        if not decisions and not retracted:
            continue
        svod = meta.get("svod") if isinstance(meta.get("svod"), dict) else {}
        # Список строк в `metadata.decisions` — заметки исполнителя («вырезано по ответу…»),
        # а не сведение решений. Новой версией он не считается: иначе все прежние решения
        # объявлялись бы «нет в новой версии» и серели (найдено на боевых данных 11.11).
        raw_meta = meta.get("decisions") if isinstance(meta.get("decisions"), list) else []
        notes_only = bool(raw_meta) and not any(isinstance(item, dict) for item in raw_meta)
        if notes_only and not retracted and not svod:
            continue
        if node_id and node_id in seen_nodes:
            continue
        seen_nodes.add(node_id)
        try:
            version = int(svod.get("version")) if svod.get("version") is not None else None
        except (TypeError, ValueError):
            version = None
        out.append({"version": version, "at": int(node.get("ended_at") or node.get("at") or 0),
                    "id": node_id, "decisions": decisions, "retracted": retracted,
                    "full": _truthy_flag(svod.get("decisions_full"))})
    out.sort(key=lambda item: (int(item.get("at") or 0), int(item.get("version") or 0)))
    for index, item in enumerate(out, start=1):
        if item.get("version") is None:
            item["version"] = index
    return out


def superseded_decisions(versions) -> dict:
    """``{d_id: {by, at, why}}`` — отозванные и исчезнувшие из новой версии решения.

    Правила (W21-S5, B-50 уточнил первое):
    * явный отзыв действует с указанной версии (``why="retracted"``) — как раньше;
    * решение, которое было в версии N и отсутствует в версии M > N, устаревает с
      версии M (``why="absent"``) ТОЛЬКО если версия M заявила полный список
      (``full``, то есть ``metadata.svod.decisions_full=true``). Без флага список
      решений карточки — дополнение: одно новое решение не гасит прежние;
    * решение, снова появившееся в более поздней версии, перестаёт быть «исчезнувшим»
      (последнее слово руководителя сильнее прежней отметки); явный отзыв так не
      снимается.

    Более позднее сведение (отзыв) сильнее раннего «исчезло».
    """
    out: dict = {}
    seen: dict = {}
    for item in sorted(versions or [], key=lambda v: int(v.get("version") or 0)):
        version = int(item.get("version") or 0)
        entries = [entry for entry in item.get("decisions") or [] if entry.get("id")]
        present = {entry["id"] for entry in entries}
        for did in item.get("retracted") or []:
            out[did] = {"by": version, "at": int(item.get("at") or 0), "why": "retracted"}
        # «Решения нет в новой версии» считаем только по версии, которая СОДЕРЖИТ список
        # решений И заявила его полным: карточка с одним новым решением (или с одним
        # отзывом) не объявляет все прежние решения устаревшими.
        if entries and item.get("full"):
            for did, first in list(seen.items()):
                if did not in present and first < version and did not in out:
                    out[did] = {"by": version, "at": int(item.get("at") or 0), "why": "absent"}
        for did in present:
            seen.setdefault(did, version)
            if (out.get(did) or {}).get("why") == "absent":
                out.pop(did, None)        # решение вернулось в свод — оно снова действует
    return out


def carried_decisions(versions, live_ids=None) -> list:
    """Решения прежних версий, которые действующая версия не повторила (W21-S5, B-50).

    Список решений карточки без ``decisions_full`` — ДОПОЛНЕНИЕ, а не сведение: то, что
    руководитель не перечислил снова, продолжает действовать (в карточке он обычно пишет
    только новое или открытое). Возвращает такие решения элементами ``decisions[]``,
    чтобы они не пропали из блока «Решения»; помеченные «устарело» сюда не попадают.
    """
    marks = superseded_decisions(versions)
    live = {str(item) for item in (live_ids or [])}
    known: dict = {}
    for item in sorted(versions or [], key=lambda v: int(v.get("version") or 0)):
        for entry in item.get("decisions") or []:
            did = str(entry.get("id") or "")
            if not did or did in known:
                continue
            known[did] = {"id": did, "text": entry.get("text") or "", "why": entry.get("why"),
                          "status": str(entry.get("status") or "proposed"),
                          "at": int(item.get("at") or 0), "source_request": None,
                          "carried": True, "from_version": int(item.get("version") or 0)}
    return [entry for did, entry in known.items() if did not in live and did not in marks]


def retired_decisions(versions, live_ids=None) -> list:
    """Устаревшие решения как элементы ``decisions[]`` (для блока «Решения»).

    Возвращает только те, которых НЕТ в действующем списке: иначе решение задвоится.
    Элемент несёт ``status="superseded"``, ``superseded_by`` и исходный текст, чтобы
    владелец видел, что именно устарело.
    """
    live = {str(item) for item in (live_ids or [])}
    marks = superseded_decisions(versions)
    known: dict = {}
    for item in versions or []:
        for entry in item.get("decisions") or []:
            if entry.get("id") and entry["id"] not in known:
                known[entry["id"]] = entry
    out = []
    for did, info in marks.items():
        if did in live or did not in known:
            continue
        entry = known[did]
        out.append({"id": did, "text": entry.get("text") or "", "why": entry.get("why"),
                    "status": "superseded", "superseded": dict(info),
                    "at": info.get("at"), "source_request": None})
    return out


def apply_superseded(decisions, versions) -> list:
    """Пометить действующие решения «устарело» по истории версий (B-37, наложение).

    Ничего не переписывает: добавляет ``superseded``/``status``. Решения, которых нет
    в новой версии свода, попадают в список отдельными элементами (см.
    :func:`retired_decisions`) — здесь только отметки для тех, что ещё в списке.
    """
    marks = superseded_decisions(versions)
    out = []
    for item in decisions or []:
        if not isinstance(item, dict):
            continue
        entry = dict(item)
        info = marks.get(str(entry.get("id") or ""))
        if info:
            entry["superseded"] = dict(info)
            entry["status"] = "superseded"
        out.append(entry)
    return out


# --------------------------------------------------- статус вопросов (W17-B §7) ---
# Аудит B7: «вопросы в „Дальше“ не знают об ответе». Реестр ответов — ЕДИНСТВЕННЫЙ
# источник истины статуса вопроса. Бэклог, счётчики вкладки и деталь запроса считают
# статус одним и тем же кодом, поэтому ответ исчезает из «ждут вашего решения» сразу.

#: вводные префиксы, которые не делают вопрос другим вопросом
_QUESTION_PREFIX = re.compile(r"^(?:не\s+к\s+спеху|не\s+срочно|на\s+будущее|потом)\s*[:—–-]\s*",
                              re.IGNORECASE)
#: скобочные уточнения (в т.ч. «(рекомендация: …)») — сравниваем вопросы без них:
#: руководитель часто переписывает пояснение в скобках, а вопрос остаётся тем же
_PARENS = re.compile(r"\([^()]{0,300}\)")
_PUNCT = re.compile(r"[^\w]+", re.UNICODE)


def is_service_question(text) -> bool:
    """Служебный вопрос, ответ на который уже задан настройками комнаты (§7в, B7-3).

    «Сколько раундов/прогонов критика?» — руководитель не должен переспрашивать;
    вопрос скрывается и из детали, и из бэклога, и из счётчиков.
    """
    flat = norm(text).lower()
    if not flat or "критик" not in flat:
        return False
    if "прогон" in flat or "раунд" in flat or "цикл" in flat:
        return True
    return "сколько" in flat and ("провер" in flat or "доработ" in flat)


def text_key(text) -> str:
    """Ключ вопроса для поиска близнецов: регистр, пробелы, знаки, префиксы, скобки.

    Только для сравнения — в интерфейсе показывается настоящий текст. Примеры из
    вымышленного образца (`fixtures/questions_live_fn.json`): q_22b45cc3 и q_18b309bf —
    один вопрос «Задачи агентам: вариант А (…) или Б (…)»; q_d47fe5e7 и q_d5b4abf0 —
    «Ольга К. (кладовщик…): передавать складские данные…».
    """
    value = norm(text).lower()
    for _ in range(3):
        stripped = _QUESTION_PREFIX.sub("", value)
        if stripped == value:
            break
        value = stripped
    value = _PARENS.sub(" ", value)
    value = value.replace("ё", "е")
    return " ".join(_PUNCT.sub(" ", value).split())


def record_for(question: dict, records) -> dict | None:
    """Запись ответа для вопроса: по id, затем по id-близнецам, затем по тексту.

    ``records`` — реестр ответов запроса (``answers[q_id]``). Текстовая связка нужна,
    когда руководитель выдал тот же вопрос с новым id (B7-2), — ответ, данный на один
    id, должен закрывать все близнецы.
    """
    if not isinstance(records, dict) or not isinstance(question, dict):
        return None
    record = records.get(str(question.get("id")))
    if isinstance(record, dict):
        return record
    for alias in question.get("aliases") or []:
        record = records.get(str(alias))
        if isinstance(record, dict):
            return record
    key = text_key(question.get("text"))
    if len(key) < 12:
        return None
    for other in records.values():
        if isinstance(other, dict) and text_key(other.get("question_text")) == key:
            return other
    return None


def dedupe_questions(questions) -> list:
    """Схлопнуть вопросы-близнецы в один элемент, id остальных — в ``aliases`` (B7-2).

    Группа — по :func:`text_key`. Показывается самый полный текст (самый длинный),
    признаки ответа берутся у любого отвеченного близнеца. Порядок — как пришёл.
    """
    groups: dict = {}
    order: list = []
    for question in questions or []:
        if not isinstance(question, dict):
            continue
        key = text_key(question.get("text")) or ("id:" + str(question.get("id")))
        base = groups.get(key)
        if base is None:
            base = dict(question)
            base["aliases"] = []
            groups[key] = base
            order.append(key)
            continue
        if len(norm(question.get("text"))) > len(norm(base.get("text"))):
            keep_id = base.get("id")
            aliases = base.get("aliases") or []
            base.update(question)
            base["id"] = keep_id                      # стабильный ключ элемента
            base["aliases"] = aliases
        alias = str(question.get("id") or "")
        if alias and alias != str(base.get("id")) and alias not in base["aliases"]:
            base["aliases"].append(alias)
        if question.get("answered") and not base.get("answered"):
            for field in ("answered", "answer", "answered_at", "answer_state", "answer_label",
                          "needs_explanation", "reaction", "leader_pending", "can_remind",
                          "reaction_overdue", "reaction_waited_s"):
                if field in question:
                    base[field] = question[field]
    return [groups[key] for key in order]


def question_state(record: dict | None) -> dict:
    """Статус вопроса для «От вас нужно» и счётчиков: ``{state, answered, label, at, answer}``."""
    record = record if isinstance(record, dict) else {}
    state = state_of(record)
    if state == "dismissed":
        # W20-D1 (B-38): «Это не вопрос» — элемент уходит из списков и счётчиков.
        return {"state": state, "answered": False, "label": STATE_RU["dismissed"],
                "at": int(record.get("at") or 0) or None, "answer": None}
    if state == "waiting_explanation":
        return {"state": state, "answered": False, "label": "ждёт пояснения",
                "at": int(record.get("at") or 0) or None, "answer": None}
    if state == "open":
        return {"state": "open", "answered": False, "label": STATE_RU["open"], "at": None, "answer": None}
    if state == "replied_text":
        # B-41: «руководитель ответил текстом — посмотрите в ленте». Ответ есть, цепочку
        # не выдумываем: вопрос не ждёт владельца, но и «учтено» не подписываем.
        return {"state": state, "answered": True, "label": STATE_RU["replied_text"],
                "at": int(record.get("at") or 0) or None, "answer": norm(record.get("text")) or None}
    return {"state": state, "answered": True, "label": "отвечен",
            "at": int(record.get("at") or 0) or None, "answer": norm(record.get("text")) or None}


def _records_index(records) -> tuple:
    """Реестр ответов комнаты → (по (root, q_id), по тексту вопроса).

    W20-D1 (B-38): запись «это не вопрос» хранится без текста ответа, но по id должна
    находиться — иначе снятый вопрос снова окажется в «От вас нужно» и в счётчиках.
    """
    by_id: dict = {}
    by_text: dict = {}
    for record in records or []:
        if not isinstance(record, dict):
            continue
        stored = record.get("answers")
        if not isinstance(stored, dict):
            continue
        root = str(record.get("root_id") or "")
        for qid, entry in stored.items():
            if not isinstance(entry, dict):
                continue
            by_id[(root, str(qid))] = entry
            text = norm(entry.get("text"))
            if not text:
                continue
            key = text_key(entry.get("question_text"))
            if len(key) >= 12:
                by_text.setdefault(key, entry)
    return by_id, by_text


def _scoped_decision(records, root: str, did: str) -> dict | None:
    """Запись решения ИМЕННО этого запроса (id решения считается по тексту, поэтому
    у двух запросов с одинаковым решением бывает один и тот же ``d_…``).
    """
    if not root:
        return None
    for record in records or []:
        if not isinstance(record, dict) or str(record.get("root_id") or "") != root:
            continue
        stored = record.get("answers")
        if not isinstance(stored, dict):
            continue
        for key, value in stored.items():
            if isinstance(value, dict) and _local_decision_id(key) == did:
                return value
    return None


def backlog_overlay(view: dict, records) -> dict:
    """Наложить реестр ответов на бэклог комнаты (B7-1, B7-2, B7-3).

    * статус вопроса — только из реестра: ответ есть → ``done`` («отвечен»),
      просьба пояснить → ``open`` + «ждёт пояснения», иначе ``open``;
    * вопросы-близнецы схлопываются в один элемент (``aliases``), счётчики — по уникальным;
    * служебные вопросы (раунды критика) не показываются вовсе.

    Данные на диске не меняются: это чтение, миграция без перезаписи.
    """
    if not isinstance(view, dict):
        return view
    items = [item for item in view.get("items") or [] if isinstance(item, dict)]
    by_id, by_text = _records_index(records)
    decisions = decision_index(records)
    merged: dict = {}
    hidden = []
    out: list = []
    for raw in items:
        item = dict(raw)
        kind = str(item.get("kind") or "")
        if kind == "decision":
            # W19-D2 (B-20): принятое/отклонённое решение сразу уходит из «От вас нужно»
            # и из счётчиков — по данным реестра ответов, без перезаписи данных.
            # W20-D1 (B-37): снятое и устаревшее — тоже (статус `dismissed`).
            did = _local_decision_id(str(item.get("id") or "").partition(":")[2] or item.get("id"))
            root_id = str(item.get("id") or "").partition(":")[0]
            entry = _scoped_decision(records, root_id, did) or decisions.get(did)
            if isinstance(entry, dict):
                view_item = apply_decision(item, entry, None)
                item["decision_status"] = view_item["status"]
                item["status_label"] = view_item["status_label"]
                item["answer_state"] = view_item["answer_state"]
                item["answer_label"] = view_item["answer_label"]
                item["answer_text"] = view_item["answer_text"]
                item["answered_at"] = view_item["answered_at"]
                item["needs_answer"] = view_item.get("needs_answer")
                if str(item.get("status") or "open") not in ("asked", "dismissed"):
                    if view_item["status"] in ("dismissed", "superseded"):
                        item["status"] = "dismissed"
                    elif view_item["status"] == "accepted":
                        item["status"] = "done"
                    elif view_item["status"] == "rejected":
                        item["status"] = "dismissed"
                    else:
                        item["status"] = "open"
            elif item.get("status") not in ("asked", "dismissed") \
                    and str(item.get("decision_status") or "") in ("dismissed", "superseded"):
                item["status"] = "dismissed"
            out.append(item)
            continue
        if kind in ("open_question", "future_question"):
            if is_service_question(item.get("text")):
                hidden.append({"id": item.get("id"), "text": item.get("text")})
                continue
            root, _sep, qid = str(item.get("id") or "").partition(":")
            entry = by_id.get((root, qid)) or by_text.get(text_key(item.get("text")))
            state = question_state(entry)
            item["answer_state"] = state["state"]
            item["state_label"] = state["label"]
            item["needs_answer"] = state["state"] not in ("dismissed", "replied_text") \
                and not state["answered"]
            current = str(item.get("status") or "open")
            if current in ("dismissed", "asked"):
                pass                     # действие пользователя сильнее: «убрал»/«уточняю»
            elif state["state"] == "dismissed":
                # W20-D1 (B-38): «Это не вопрос» — убираем из «От вас нужно» и счётчиков
                item["status"] = "dismissed"
            elif state["answered"]:
                item["status"] = "done"
                item["answered_at"] = state["at"]
                item["answer"] = state["answer"]
            else:
                # просьба пояснить и «нет ответа» — вопрос ОСТАЁТСЯ в «От вас нужно»
                item["status"] = "open"
            key = text_key(item.get("text"))
            if key and key in merged:
                base = merged[key]
                if item.get("id") and item["id"] != base.get("id") \
                        and item["id"] not in (base.get("aliases") or []):
                    base.setdefault("aliases", []).append(item["id"])
                if len(norm(item.get("text"))) > len(norm(base.get("text"))):
                    keep = base.get("id")
                    aliases = base.get("aliases") or []
                    base.update(item)
                    base["id"] = keep
                    base["aliases"] = aliases
                if item.get("status") == "done" and base.get("status") != "done":
                    base.update({"status": "done", "answer_state": item.get("answer_state"),
                                 "state_label": item.get("state_label"),
                                 "answered_at": item.get("answered_at"), "answer": item.get("answer")})
                continue
            item["aliases"] = []
            if key:
                merged[key] = item
            out.append(item)
            continue
        out.append(item)
    counts = {status: 0 for status in ("open", "asked", "done", "dismissed")}
    for item in out:
        status = str(item.get("status") or "open")
        counts[status] = counts.get(status, 0) + 1
    counts["total"] = len(out)
    counts["hidden_service"] = len(hidden)
    sections = {
        "inbox": [i for i in out if i.get("status") in ("open", "asked")
                  and i.get("kind") in ("open_question", "decision")],
        "next_steps": [i for i in out if i.get("kind") == "next_step"
                       and i.get("status") in ("open", "asked")],
        "future_questions": [i for i in out if i.get("kind") == "future_question"],
        "decisions": [i for i in out if i.get("kind") == "decision"],
    }
    result = dict(view)
    result["items"] = out
    result["counts"] = counts
    result["sections"] = sections
    if hidden:
        result["hidden_service"] = hidden
    return result


def summary(questions, steps) -> dict:
    """Сводка для интерфейса: сколько ответов, ждут реакции, ждут пояснения, конфликты."""
    states = {}
    pending = waiting = dismissed = replied_text = 0
    for question in questions or []:
        if not isinstance(question, dict):
            continue
        state = str(question.get("answer_state") or "open")
        if state in ("open", "dismissed"):
            continue                # «Это не вопрос» (W20-D1, B-38) — не ответ и не счётчик
        states[state] = states.get(state, 0) + 1
        pending += 1 if state == "sent" else 0
        waiting += 1 if state == "waiting_explanation" else 0
        replied_text += 1 if state == "replied_text" else 0
    conflicts = [step.get("id") for step in steps or []
                 if isinstance(step, dict) and step.get("conflicts_with_answer")]
    cancelled = [step.get("id") for step in steps or []
                 if isinstance(step, dict) and step.get("cancelled_by_answer")]
    return {"answers": sum(states.values()), "states": states, "pending": pending,
            "waiting_explanation": waiting, "conflicts": conflicts, "cancelled": cancelled,
            "dismissed": dismissed, "replied_text": replied_text,
            # W17-B §3: кнопка «Отправить руководителю накопленные ответы» — есть что
            # отправлять и это осмысленно (ответы без реакции). Пусто — кнопку не показываем.
            # W21-Q1 (B-45): `can_resend` оставлен как есть (совместимость и старые
            # клиенты); ВИДИМОСТЬ кнопки решает клиент по `synthesis.live.auto`.
            # `resend_hint` — спокойная подпись вместо «N ответа без реакции».
            "can_resend": pending > 0, "resend_pending": pending,
            "resend_label": "Отправить руководителю накопленные ответы",
            "resend_hint": "Ответы переданы руководителю — он учтёт их в ближайшем обновлении свода",
            "resend_hint_soon": "Ответы переданы руководителю — он учтёт их в ближайшее время"}


__all__ = ["ANSWER_MARK", "EXPLAIN_MARK", "REACTION_MARK", "REMIND_MARK", "STATES", "STATE_RU",
           "PENDING_RU", "CANCELLED_RU", "CONFLICT_RU", "REACTION_WAIT_S",
           "LINK_MIN_SCORE", "ACTION_MIN_SCORE",
           "STEP_AGREE_MARK", "STEP_DROP_MARK", "STEP_COMMENT_MARK", "STEP_MOVE_MARK",
           "STEP_REACTION_MARK", "STEP_KINDS", "STEP_KIND_RU", "STEP_KIND_VERB", "STEP_STATUS_RU",
           "STALE_MARK", "RESTORE_MARK", "stale_comment",
           "norm", "short", "stems", "is_negation", "is_counter_question",
           "answer_comment", "explanation_comment", "reaction_comment", "remind_text",
           "EXPLAINED_MARK", "explain_comment", "explained_kind_of", "explained_text_of",
           "explanations_from_thread",
           "DIGEST_MARK", "digest_comment", "explanation_hint",
           "answer_kind_of", "answer_text_of", "question_id_of", "question_text_of",
           "reaction_state", "reactions_from_thread", "reaction_for_question", "reaction_lookup",
           "IMPLICIT_MIN_SCORE", "IMPLICIT_MIN_RATIO",
           "state_of", "apply_question", "answer_item", "link_question", "link_steps",
           "sort_steps", "step_id_of", "step_comment", "step_reaction_state", "step_reaction_label",
           "step_reactions_from_thread", "apply_step", "steps_log_for",
           "steps_to_cancel", "summary",
           # W19-D2 (B-21): цепочка статусов ответа и явный исход реакции
           "STATE_HINT", "CHAIN_HINT", "state_label", "state_hint", "outcome_of",
           "reactions_from_tree", "merge_reactions",
           # W19-D2 (B-20): решения «предложено» — действия и статус
           "DECISION_MARK", "DECISION_REACTION_MARK", "DECISION_ACTIONS", "DECISION_ACTION_RU",
           "DECISION_ACTION_HINT", "DECISION_STATUS_RU", "DECISION_STATUS_HINT",
           "decision_id_of", "decision_kind_of", "decision_action_of", "decision_comment",
           "decision_reaction_comment", "decision_reactions_from_thread", "decision_index",
           "decision_record_for", "apply_decision", "decision_summary",
           # W20-D1 (B-37): снятое/устаревшее решение, отзыв и версии свода
           "DECISION_STATUSES", "DECISION_CLOSED", "DECISION_MUTED",
           "decision_versions", "superseded_decisions", "retired_decisions", "apply_superseded",
           "carried_decisions",
           # W20-D1 (B-38, B-41): вопросы руководителя из комментариев и «ответил текстом»
           "LEAD_QUESTION_MARK", "LEAD_DECISION_MARK", "QUESTION_HEADINGS", "MAX_COMMENT_ITEMS",
           "lead_comment_kind", "mark_text_of", "comment_item_id", "candidates_from_comment",
           "questions_from_thread", "mark_asks",
           # W17-B §7 (B7): статус вопроса из реестра, близнецы, служебные вопросы
           "is_service_question", "text_key", "record_for", "dedupe_questions",
           "question_state", "backlog_overlay",
           # W29-Q9: короткие подтверждения не будят руководителя
           "is_simple_ack", "ACK_WORDS"]
