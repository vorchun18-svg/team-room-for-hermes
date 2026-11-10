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

import re
import time as _time

#: метки в ветке запроса (по ним читается и лента, и реакция)
ANSWER_MARK = "[Михаил · ответ на вопрос"
EXPLAIN_MARK = "[Михаил · просит пояснить по вопросу"
REACTION_MARK = "[Руководитель · реакция на ответ"
REMIND_MARK = "[Михаил · напоминание]"

#: статусы ответа по контракту (W14-D §2; W19-D2 §2 — понятная цепочка)
STATES = ("open", "waiting_explanation", "sent", "received", "applied", "no_change")
STATE_RU = {
    "open": "ждёт вашего ответа",
    "waiting_explanation": "ждёт вашего решения после пояснения",
    # W19-D2 (B-21): «Ответ отправлен» → «Руководитель прочитал» → «Учтено».
    "sent": "ответ отправлен",
    "received": "руководитель прочитал",
    "applied": "учтено: работа изменена",
    "no_change": "учтено: менять ничего не нужно",
}
#: «Что значит» — одна подсказка на каждое состояние (B-21: не угадывать по словам).
STATE_HINT = {
    "open": "Вопрос ждёт вашего ответа — руководитель не может двигаться дальше без него.",
    "waiting_explanation": "Вы попросили пояснение: вопрос остаётся открытым, пока руководитель "
                           "не объяснит, о чём речь.",
    "sent": "Ответ ушёл руководителю. Пока он его не открыл и не отметил исход — ждите "
            "или нажмите «Напомнить руководителю».",
    "received": "Руководитель прочитал ответ и взял его в работу. «Учтено» появится, когда он "
                "запишет, что именно изменил.",
    "applied": "Руководитель учёл ответ: работа пересмотрена, карточки изменены. "
               "Их список — рядом с реакцией.",
    "no_change": "Руководитель учёл ответ и решил, что менять в работе ничего не нужно.",
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
    """Встречный вопрос: просьба пояснить, а не ответ."""
    return "%s %s «%s»] %s" % (EXPLAIN_MARK, question_id, short(question_text, 120), norm(text))


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
    return out


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
    она ловила ложные срабатывания на старых записях (боевой `q_7985e685`: в ответе
    Михаила было решение «эта папка не относится к юристу», а вопрос помечался
    «ждёт пояснения»). Такие записи реестра считаются ОТВЕТАМИ (`answered=true`),
    без перезаписи данных. Подсказка «похоже на просьбу пояснить» осталась
    отдельной функцией (`explanation_hint`) — только для журнала.
    """
    record = record if isinstance(record, dict) else {}
    text = norm(record.get("text"))
    if not text:
        return "open"
    if record.get("answered") is False or str(record.get("status") or "") == "waiting_explanation":
        return "waiting_explanation"
    if isinstance(reaction, dict) and reaction.get("status") in ("received", "applied", "no_change"):
        return str(reaction["status"])
    stored = str(record.get("status") or "")
    if stored in ("received", "applied", "no_change"):
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


def apply_question(question: dict, record, reaction=None, now: int | None = None) -> dict:
    """Наложить запись ответа и реакцию на вопрос (мутирует и возвращает его)."""
    question = question if isinstance(question, dict) else {}
    record = record if isinstance(record, dict) else {}
    text = norm(record.get("text"))
    at = int(record.get("at") or 0) or None
    state = state_of(record, reaction)
    answered = bool(text) and state not in ("waiting_explanation", "open")
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

STEP_KINDS = ("agree", "comment", "move", "drop")
STEP_KIND_RU = {"agree": "вы согласовали", "comment": "ваш комментарий",
                "move": "вы переместили", "drop": "вы убрали"}
STEP_KIND_VERB = {"agree": "согласовано вами", "comment": "прокомментировано вами",
                  "move": "перемещено вами", "drop": "убрано вами"}
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
            "comment": STEP_COMMENT_MARK, "move": STEP_MOVE_MARK}.get(str(kind))
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
DECISION_ACTIONS = ("accept", "reject", "discuss")
DECISION_ACTION_RU = {"accept": "Принять", "reject": "Отклонить", "discuss": "Обсудить"}
DECISION_ACTION_HINT = {
    "accept": "Согласиться с предложенным решением — руководитель увидит это сразу",
    "reject": "Отклонить решение: можно сразу написать, что предложить вместо",
    "discuss": "Написать или надиктовать, что обсудить — решение останется за вами",
}
DECISION_STATUS_RU = {"proposed": "предложено", "accepted": "принято вами",
                      "rejected": "отклонено вами", "discuss": "обсуждаем с руководителем"}
DECISION_STATUS_HINT = {
    "proposed": "Руководитель предлагает так; решение за вами — «Принять», «Отклонить» "
                "или «Обсудить».",
    "accepted": "Вы приняли решение. Руководитель получил ваш ответ и учтёт его в работе.",
    "rejected": "Вы отклонили решение. Руководитель получил ответ и предложит другой вариант.",
    "discuss": "Вы просите обсудить: решение остаётся за вами, пока руководитель не ответит.",
}
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
    """Наложить на решение действие Михаила и реакцию руководителя (B-20, B-21).

    * ``status`` — ``proposed`` (ждёт слова), ``accepted``/``rejected`` (решено
      Михаилом), ``discuss`` (обсуждаем: решение всё ещё за ним);
    * ``answer_state``/``answer_label``/``answer_hint`` — цепочка ответа
      («ответ отправлен → руководитель прочитал → учтено»);
    * ``leader_pending``, ``can_remind``, ``reaction`` — то же, что у вопросов.
    """
    d = dict(decision) if isinstance(decision, dict) else {}
    rec = record if isinstance(record, dict) else {}
    moment = int(now or _time.time())
    status = str(d.get("status") or "proposed")
    if not rec and (d.get("answer_text") or d.get("answered_at")):
        # Запись ответа потеряна (старый формат/фикстура), но ответ виден в самом
        # решении — читаем его оттуда, не приписывая ответ руководителю.
        rec = {"kind": "decision", "decision_id": d.get("id"), "answered": True,
               "text": norm(d.get("answer_text")), "at": int(d.get("answered_at") or 0) or None,
               "status": "sent",
               "action": _ACTION_OF_STATUS.get(status) if status in ("accepted", "rejected", "discuss") else None}
    action = str(rec.get("action") or "").strip() or _ACTION_OF_STATUS.get(str(rec.get("outcome") or ""), "")
    if action in _STATUS_OF_ACTION:
        status = _STATUS_OF_ACTION[action]
    reaction = dict(reaction) if isinstance(reaction, dict) else None
    state = state_of(rec, reaction)
    if state == "open" and reaction and str(reaction.get("status")) in ("received", "applied", "no_change"):
        # реакция есть, запись ответа потеряна — цепочка всё равно видна
        state = str(reaction["status"])
    at = int(rec.get("at") or 0) or None
    texts = norm(rec.get("text")) or None
    cards = (reaction or {}).get("cards") or []
    changes = (reaction or {}).get("changes")
    waited = max(0, moment - at) if (state == "sent" and at) else 0
    d.update({
        "status": status,
        "status_label": (DECISION_STATUS_RU.get(status, DECISION_STATUS_RU["proposed"])
                         if action or status not in ("accepted", "rejected")
                         else "принято руководителем" if status == "accepted" else "отклонено руководителем"),
        "status_hint": ("Руководитель отметил это решение сам; вашего ответа здесь не требовалось."
                        if (not action and status in ("accepted", "rejected"))
                        else DECISION_STATUS_HINT.get(status, DECISION_STATUS_HINT["proposed"])),
        "status_source": "user" if action else "leader",
        "open": status in ("proposed", "discuss"),
        "answered": bool(rec),
        "answer_action": action or None,
        "answer_action_label": DECISION_ACTION_RU.get(action) if action else None,
        "answer_text": texts,
        "answered_at": at,
        "answer_state": state,
        "answer_label": state_label(state, cards, changes) if (rec or reaction) else None,
        "answer_hint": state_hint(state) if (rec or reaction) else None,
        "reaction": reaction,
        "leader_pending": state == "sent",
        "can_remind": state == "sent",
        "reaction_waited_s": waited if state == "sent" else None,
        "reaction_overdue": bool(state == "sent" and waited >= REACTION_WAIT_S),
        "can_accept": True, "can_reject": True, "can_discuss": True,
        "actions": [{"action": key, "label": DECISION_ACTION_RU[key], "hint": DECISION_ACTION_HINT[key],
                     "current": key == action} for key in DECISION_ACTIONS],
    })
    return d


def decision_summary(decisions) -> dict:
    """Сколько решений ждут слова Михаила и сколько уже решено (для подписей)."""
    open_count = closed = 0
    for item in decisions or []:
        if not isinstance(item, dict):
            continue
        if str(item.get("status") or "proposed") in ("proposed", "discuss"):
            open_count += 1
        else:
            closed += 1
    return {"total": open_count + closed, "open": open_count, "closed": closed}


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

    Только для сравнения — в интерфейсе показывается настоящий текст. Боевые примеры
    (`fn`): q_22b45cc3 и q_18b309bf — один вопрос «Задачи агентам в TaskFlow: вариант А
    (…) или Б (…)»; q_d47fe5e7 и q_d5b4abf0 — «Ольга К. (маркшейдер…): передавать…».
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
    """Статус вопроса для «Дальше» и счётчиков: ``{state, answered, label, at, answer}``."""
    record = record if isinstance(record, dict) else {}
    state = state_of(record)
    if state == "waiting_explanation":
        return {"state": state, "answered": False, "label": "ждёт пояснения",
                "at": int(record.get("at") or 0) or None, "answer": None}
    if state == "open":
        return {"state": "open", "answered": False, "label": STATE_RU["open"], "at": None, "answer": None}
    return {"state": state, "answered": True, "label": "отвечен",
            "at": int(record.get("at") or 0) or None, "answer": norm(record.get("text")) or None}


def _records_index(records) -> tuple:
    """Реестр ответов комнаты → (по (root, q_id), по тексту вопроса)."""
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
            if not isinstance(entry, dict) or not norm(entry.get("text")):
                continue
            by_id[(root, str(qid))] = entry
            key = text_key(entry.get("question_text"))
            if len(key) >= 12:
                by_text.setdefault(key, entry)
    return by_id, by_text


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
            # W19-D2 (B-20): принятое/отклонённое решение сразу уходит из «Дальше»
            # и из счётчиков — по данным реестра ответов, без перезаписи данных.
            did = _local_decision_id(str(item.get("id") or "").partition(":")[2] or item.get("id"))
            entry = decisions.get(did)
            if isinstance(entry, dict):
                view_item = apply_decision(item, entry, None)
                item["decision_status"] = view_item["status"]
                item["status_label"] = view_item["status_label"]
                item["answer_state"] = view_item["answer_state"]
                item["answer_label"] = view_item["answer_label"]
                item["answer_text"] = view_item["answer_text"]
                item["answered_at"] = view_item["answered_at"]
                if str(item.get("status") or "open") not in ("asked", "dismissed"):
                    item["status"] = ("done" if view_item["status"] == "accepted" else
                                      "dismissed" if view_item["status"] == "rejected" else "open")
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
            current = str(item.get("status") or "open")
            if current in ("dismissed", "asked"):
                pass                     # действие пользователя сильнее: «убрал»/«уточняю»
            elif state["answered"]:
                item["status"] = "done"
                item["answered_at"] = state["at"]
                item["answer"] = state["answer"]
            else:
                # просьба пояснить и «нет ответа» — вопрос ОСТАЁТСЯ в «Дальше»
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
    pending = waiting = 0
    for question in questions or []:
        if not isinstance(question, dict):
            continue
        state = str(question.get("answer_state") or "open")
        if state == "open":
            continue
        states[state] = states.get(state, 0) + 1
        pending += 1 if state == "sent" else 0
        waiting += 1 if state == "waiting_explanation" else 0
    conflicts = [step.get("id") for step in steps or []
                 if isinstance(step, dict) and step.get("conflicts_with_answer")]
    cancelled = [step.get("id") for step in steps or []
                 if isinstance(step, dict) and step.get("cancelled_by_answer")]
    return {"answers": sum(states.values()), "states": states, "pending": pending,
            "waiting_explanation": waiting, "conflicts": conflicts, "cancelled": cancelled,
            # W17-B §3: кнопка «Отправить руководителю накопленные ответы» — есть что
            # отправлять и это осмысленно (ответы без реакции). Пусто — кнопку не показываем.
            "can_resend": pending > 0, "resend_pending": pending,
            "resend_label": "Отправить руководителю накопленные ответы"}


__all__ = ["ANSWER_MARK", "EXPLAIN_MARK", "REACTION_MARK", "REMIND_MARK", "STATES", "STATE_RU",
           "PENDING_RU", "CANCELLED_RU", "CONFLICT_RU", "REACTION_WAIT_S",
           "LINK_MIN_SCORE", "ACTION_MIN_SCORE",
           "STEP_AGREE_MARK", "STEP_DROP_MARK", "STEP_COMMENT_MARK", "STEP_MOVE_MARK",
           "STEP_REACTION_MARK", "STEP_KINDS", "STEP_KIND_RU", "STEP_KIND_VERB", "STEP_STATUS_RU",
           "norm", "short", "stems", "is_negation", "is_counter_question",
           "answer_comment", "explanation_comment", "reaction_comment", "remind_text",
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
           # W17-B §7 (B7): статус вопроса из реестра, близнецы, служебные вопросы
           "is_service_question", "text_key", "record_for", "dedupe_questions",
           "question_state", "backlog_overlay"]
