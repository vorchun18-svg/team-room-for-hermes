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

#: статусы ответа по контракту (W14-D §2)
STATES = ("open", "waiting_explanation", "sent", "received", "applied", "no_change")
STATE_RU = {
    "open": "ждёт вашего ответа",
    "waiting_explanation": "ждёт вашего решения после пояснения",
    "sent": "ответ отправлен — руководитель ещё не ответил",
    "received": "принято руководителем",
    "applied": "учтено: работа пересмотрена",
    "no_change": "принято: менять ничего не нужно",
}
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


def reaction_state(text) -> str:
    """Класс реакции по тексту: applied / no_change / received."""
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


def reactions_from_thread(items, questions=None) -> dict:
    """Реакции руководителя из ленты: ``{question_id: {text, at, status, cards, author}}``.

    Основной путь — метка ``[Руководитель · реакция на ответ q_x]`` (контракт в
    ``docs/AGENT-GUIDE.md``). Запасной — обычный комментарий руководителя, который
    явно называет id вопроса или дословно перекрывает текст вопроса; такая реакция
    помечается ``implicit`` и в интерфейсе показывается так же.
    """
    out: dict = {}
    by_stem: dict = {}
    for question in questions or []:
        if isinstance(question, dict) and question.get("id"):
            by_stem[str(question["id"])] = stems(question.get("text"))
    for item in items or []:
        if not isinstance(item, dict) or item.get("author_kind") != "agent":
            continue
        text = norm(item.get("text"))
        if not text:
            continue
        qid, implicit = "", False
        if text.startswith(REACTION_MARK) or "реакция на ответ" in text.lower():
            qid = _qid_from(text)
        if not qid:
            mentioned = _qid_from(text)
            if mentioned and mentioned in by_stem:
                qid, implicit = mentioned, True
        if not qid and by_stem:
            mine = stems(text)
            best, score = "", 0
            for candidate, words in by_stem.items():
                common = len(mine & words)
                if common > score:
                    best, score = candidate, common
            if score >= 3 and reaction_state(text) in ("applied", "no_change"):
                qid, implicit = best, True
        if not qid:
            continue
        at = int(item.get("at") or 0)
        previous = out.get(qid)
        if previous and int(previous.get("at") or 0) > at:
            continue
        out[qid] = {"text": text, "at": at, "status": reaction_state(text),
                    "cards": _cards_of(text), "author": item.get("author") or "",
                    "author_label": item.get("author_label") or "",
                    "implicit": implicit, "task_id": item.get("task_id")}
    return out


# ------------------------------------------------------------------ применение ---

def state_of(record: dict | None, reaction: dict | None = None) -> str:
    """Статус ответа: реестр + найденная реакция руководителя."""
    record = record if isinstance(record, dict) else {}
    text = norm(record.get("text"))
    if not text:
        return "open"
    if record.get("answered") is False or str(record.get("status") or "") == "waiting_explanation":
        return "waiting_explanation"
    if "status" not in record and "answered" not in record and is_counter_question(text):
        # старые записи (до W14-D) писали только текст: встречный вопрос ошибочно
        # считался ответом (п. 10). Восстанавливаем смысл по тексту.
        return "waiting_explanation"
    if isinstance(reaction, dict) and reaction.get("status") in ("received", "applied", "no_change"):
        return str(reaction["status"])
    stored = str(record.get("status") or "")
    if stored in ("received", "applied", "no_change"):
        return stored
    return "sent"


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
    question["answer_label"] = STATE_RU.get(state, STATE_RU["open"])
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
    """Видимая подпись реакции на действие по шагу (§12: «принято/учтено/убран/срок изменён»)."""
    kind = str(kind or "")
    status = str(status or "")
    if kind == "drop" or status == "removed":
        return "шаг убран"
    if status == "moved" or kind == "move":
        return "срок изменён" if due_changed else "порядок изменён"
    if status == "no_change":
        return "принято: менять не нужно"
    if status == "applied":
        return "учтено: работа пересмотрена"
    if status == "received":
        return "принято"
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
            "waiting_explanation": waiting, "conflicts": conflicts, "cancelled": cancelled}


__all__ = ["ANSWER_MARK", "EXPLAIN_MARK", "REACTION_MARK", "REMIND_MARK", "STATES", "STATE_RU",
           "PENDING_RU", "CANCELLED_RU", "CONFLICT_RU", "REACTION_WAIT_S",
           "LINK_MIN_SCORE", "ACTION_MIN_SCORE",
           "STEP_AGREE_MARK", "STEP_DROP_MARK", "STEP_COMMENT_MARK", "STEP_MOVE_MARK",
           "STEP_REACTION_MARK", "STEP_KINDS", "STEP_KIND_RU", "STEP_KIND_VERB", "STEP_STATUS_RU",
           "norm", "short", "stems", "is_negation", "is_counter_question",
           "answer_comment", "explanation_comment", "reaction_comment", "remind_text",
           "answer_kind_of", "answer_text_of", "question_id_of", "question_text_of",
           "reaction_state", "reactions_from_thread",
           "state_of", "apply_question", "answer_item", "link_question", "link_steps",
           "sort_steps", "step_id_of", "step_comment", "step_reaction_state", "step_reaction_label",
           "step_reactions_from_thread", "apply_step", "steps_log_for",
           "steps_to_cancel", "summary"]
