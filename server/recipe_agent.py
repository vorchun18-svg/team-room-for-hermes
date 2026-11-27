"""Рецепт из разговора и лёгкий анализ завершённых задач (W31-C, дефекты 28–29).

Две вещи, обе — через ОБЫЧНЫЙ вызов руководителя комнаты, как «Разбить на задачи»:
приложение ставит карточку руководителю, руководитель отвечает комментарием с меткой
и JSON, приложение разбирает ответ. Модель при этом не вызывается ни разу: ни здесь,
ни в тестах (в песочнице `TEAMROOM_RECIPE_FAKE=1` подставляет детерминированный ответ
вместо живого руководителя — как `TEAMROOM_STT_FAKE` у расшифровки).

Разговор о рецепте (``dialog``)
-------------------------------
1. человек пишет или диктует, чего хочет (``start``);
2. руководитель либо задаёт уточняющие вопросы, либо сразу присылает алгоритм;
3. человек отвечает на вопросы (``answer``) или просит поправить (``fix``) —
   каждое сообщение человека уходит руководителю комментарием;
4. готовый алгоритм человек утверждает «Да» (``accept`` → рецепт сохранён) или
   правит дальше. Ручной редактор остаётся запасным путём.

Ответ руководителя — комментарий ``[Руководитель · рецепт]`` и JSON:

    {"stage": "questions", "summary": "…", "questions": ["…", "…"]}
    {"stage": "draft", "recipe": {name, description, fields[…], steps[…]}}

«Сохранить задачу как рецепт» — тот же разговор, только карточка-задание ставится
внутри завершённой задачи (``start(from_request=…)``): руководитель видит её шаги и
обобщает их в рецепт, а не переписывает механически.

Лёгкий анализ (``analysis``)
----------------------------
Раз в N дней (по умолчанию 7, напоминание по умолчанию ВЫКЛЮЧЕНО — W31-FIX3) сторож
лишь помечает в состоянии, что «пора разобрать задачи»: сколько новых завершённых
задач и прикидка токенов. Вкладка «Рецепты» показывает плашку «Пора провести разбор
задач (новых: N, примерно K токенов)» с кнопками «Запустить» и «Позже». Карточку
руководителю ставит ТОЛЬКО человек («Запустить» → ``run``); до этого модель не
вызывается ни разу. Руководитель читает заголовки и короткие сводки завершённых
задач, ищет похожие и ПРЕДЛАГАЕТ «сделать из этих N задач рецепт». Похожие на
существующие рецепты и ранее отклонённые предложения не показываются. Модель —
руководителя комнаты (у комнаты общей модели нет) или выбранная человеком из
каталога Hermes; прикидка токенов видна ДО запуска.

Стор: ``data/state/<room>/recipe-dialogs.json`` и ``recipe-analysis.json``.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import time
from pathlib import Path

try:                                     # пакетный и «плоский» импорт
    from . import recipes as recipes_mod
    from . import statefiles as sf
except ImportError:                      # pragma: no cover
    import recipes as recipes_mod        # type: ignore
    import statefiles as sf              # type: ignore

DIALOGS_FILE = "recipe-dialogs.json"
ANALYSIS_FILE = "recipe-analysis.json"
STATE_VERSION = 1

#: Метка ответа руководителя по рецепту (и её варианты — агент пишет по-разному).
MARK = "[Руководитель · рецепт]"
MARK_ALIASES = (MARK, "[Руководитель · рецепт из задачи]", "[Руководитель · алгоритм]")
#: Метка ответа по анализу завершённых задач.
OFFER_MARK = "[Руководитель · рецепты из задач]"
OFFER_ALIASES = (OFFER_MARK, "[Руководитель · анализ задач]")
#: Метка сообщений человека руководителю (по ним видно, ЧТО человек уже сказал).
HUMAN_MARK = "[Михаил · о рецепте]"

MAX_TEXT = 4000
MAX_QUESTIONS = 6
MAX_DIALOGS = 20
MAX_OFFERS = 12
MAX_CANDIDATES = 40
MAX_CANDIDATE_CHARS = 240
MAX_DISMISSED = 200
DEFAULT_DAYS = 7
MIN_DAYS, MAX_DAYS = 1, 90
DEFAULT_MIN_TASKS = 3
#: Знаков на токен русского текста (та же оценка, что у «Второго мнения»).
CHARS_PER_TOKEN = 3.0
#: Выход одного прохода анализа: короткий список предложений.
OFFER_OUTPUT_TOKENS = 900
STAGE_RU = {"asked": "руководитель готовит ответ", "questions": "нужны ваши уточнения",
            "draft": "алгоритм готов — утвердите", "accepted": "рецепт сохранён",
            "closed": "закрыт"}
OFFER_STATE_RU = {"new": "предложение", "created": "рецепт создан",
                  "dismissed": "отклонено"}


class RecipeAgentError(Exception):
    """Доменная ошибка: код API, русский текст, HTTP."""

    def __init__(self, message: str, code: str = "bad_request", http: int = 400,
                 detail: dict | None = None, **extra):
        super().__init__(message)
        self.message = str(message)
        self.code = code
        self.http = int(http)
        self.detail = dict(detail or {})
        for key, value in extra.items():
            self.detail.setdefault(key, value)

    def as_dict(self) -> dict:
        return {"code": self.code, "message": self.message, "detail": self.detail}


# --------------------------------------------------------------- общее

def _now() -> int:
    return int(time.time())


def _text(value, limit: int = MAX_TEXT) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()[:limit]


def fake_enabled() -> bool:
    """Подставной руководитель — ТОЛЬКО в песочнице (иначе ни одного токена не тратим)."""
    return (os.environ.get("TEAMROOM_RECIPE_FAKE") == "1"
            and os.environ.get("TEAMROOM_SANDBOX") == "1")


def _engine():
    try:
        from . import engine_write as mod
    except ImportError:                  # pragma: no cover
        import engine_write as mod       # type: ignore
    return mod


def _thread_mod():
    try:
        from . import thread as mod
    except ImportError:                  # pragma: no cover
        import thread as mod             # type: ignore
    return mod


def _db():
    try:
        from . import engine_db as mod
    except ImportError:                  # pragma: no cover
        import engine_db as mod          # type: ignore
    return mod


def _models():
    try:
        from . import agent_models as mod
    except ImportError:                  # pragma: no cover
        import agent_models as mod       # type: ignore
    return mod


def _usage():
    try:
        from . import usage as mod
    except ImportError:                  # pragma: no cover
        import usage as mod              # type: ignore
    return mod


def _room_id(room) -> str:
    rid = str((room or {}).get("id") or "").strip()
    if not rid:
        raise RecipeAgentError("Не передана комната рецепта.")
    return rid


def _board(room) -> str:
    board = str((room or {}).get("board") or "").strip()
    if not board:
        raise RecipeAgentError("У комнаты не указана доска канбана.", "conflict", 409)
    return board


def _board_write_guard(cfg, room) -> None:
    """W31-FIX3: песочница/тест НЕ ставит карточки рецептов на чужую (живую) доску.

    Проверка дублирует ``engine_cli._board_guard`` намеренно: она срабатывает ДО
    сборки тела и вызова движка и говорит человеку по-русски, почему карточки нет.
    Молча пропускаем только поломку самой защиты (её нет в сборке) — тогда работает
    нижняя проверка движка.
    """
    try:
        from . import board_guard as guard
    except ImportError:                  # pragma: no cover - «плоский» импорт
        try:
            import board_guard as guard  # type: ignore
        except ImportError:              # pragma: no cover - сборка без защиты
            return
    found = guard.check(cfg, _board(room))
    if found:
        raise RecipeAgentError(str(found.get("message") or "Запись в доску запрещена."),
                               guard.CODE, 409, dict(found.get("detail") or {}))


def lead_of(room) -> str:
    """Руководитель комнаты: он и делает рецепт на СВОЕЙ модели (общей у комнаты нет)."""
    room = room if isinstance(room, dict) else {}
    lead = str(room.get("orchestrator") or "").strip()
    if lead:
        return lead
    for member in room.get("members") or []:
        if isinstance(member, dict) and str(member.get("role") or "") in ("orchestrator", "leader"):
            return str(member.get("profile") or "")
    return ""


def _lead_or_fail(room) -> str:
    lead = lead_of(room)
    if not lead:
        raise RecipeAgentError("В комнате не выбран руководитель — рецепт делать некому.",
                               "no_leader", 409)
    return lead


def _lead_model(cfg, room) -> dict:
    lead = lead_of(room)
    out = {"profile": lead, "provider": "", "model": "", "label": "", "source": ""}
    if not lead:
        return out
    try:
        value = _models().effective(cfg, room, lead)
    except Exception:                    # noqa: BLE001 - подпись модели не должна ронять экран
        return out
    out.update(provider=_text(value.get("provider"), 120), model=_text(value.get("model"), 200),
               source=_text(value.get("source"), 32))
    if out["model"]:
        out["label"] = ("%s · %s" % (out["provider"], out["model"])) if out["provider"] else out["model"]
    return out


def _json_payload(text: str):
    """Первый JSON-объект из текста (ограждённый ```json или просто в прозе)."""
    raw = str(text or "")
    block = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", raw, re.S)
    if block:
        try:
            return json.loads(block.group(1))
        except ValueError:
            pass
    depth = 0
    start = -1
    for index, char in enumerate(raw):
        if char == "{":
            if depth == 0:
                start = index
            depth += 1
        elif char == "}" and depth:
            depth -= 1
            if depth == 0 and start >= 0:
                try:
                    data = json.loads(raw[start:index + 1])
                except ValueError:
                    start = -1
                    continue
                if isinstance(data, dict):
                    return data
                start = -1
    return None


def _marked(text: str, aliases) -> bool:
    head = "\n".join(str(text or "").splitlines()[:3]).lower()
    return any(alias.lower() in head for alias in aliases)


def _after_seen(items: list, holder: dict) -> list:
    """Комментарии ПОСЛЕ уже разобранного: по id (время в одну секунду не спасает)."""
    ref = str(holder.get("seen_ref") or "")
    if ref:
        for index, entry in enumerate(items):
            if str(entry.get("id") or "") == ref:
                return items[index + 1:]
        return list(items)
    return list(items)


def _body_after_mark(text: str, aliases) -> str:
    body = str(text or "")
    for alias in aliases:
        index = body.lower().find(alias.lower())
        if index >= 0:
            return body[index + len(alias):].strip()
    return body.strip()


# --------------------------------------------------------------- разговор

def dialogs_path(cfg, room_id) -> Path:
    return Path(cfg.state_dir) / str(room_id) / DIALOGS_FILE


def analysis_path(cfg, room_id) -> Path:
    return Path(cfg.state_dir) / str(room_id) / ANALYSIS_FILE


def _load_dialogs(cfg, room) -> dict:
    doc, info = sf.read_json_doc(str(dialogs_path(cfg, _room_id(room))), default=None)
    if not isinstance(doc, dict):
        doc = {"version": STATE_VERSION, "items": []}
    doc.setdefault("items", [])
    return doc


def _save_dialogs(cfg, room, doc) -> None:
    doc["version"] = STATE_VERSION
    doc["items"] = (doc.get("items") or [])[-MAX_DIALOGS:]
    sf.write_json_atomic(str(dialogs_path(cfg, _room_id(room))), doc)


def _find_dialog(doc: dict, dialog_id: str) -> dict | None:
    for item in doc.get("items") or []:
        if str(item.get("id") or "") == str(dialog_id or ""):
            return item
    return None


def _new_dialog_id(room_id: str, text: str) -> str:
    digest = hashlib.sha1(("%s|%s|%s" % (room_id, text[:200], _now())).encode("utf-8")).hexdigest()
    return "dlg-" + digest[:10]


def _dialog_public(item: dict) -> dict:
    stage = str(item.get("stage") or "asked")
    draft = item.get("draft") if isinstance(item.get("draft"), dict) else None
    return {
        "id": str(item.get("id") or ""),
        "text": str(item.get("text") or ""),
        "at": int(item.get("at") or 0), "updated_at": int(item.get("updated_at") or 0),
        "stage": stage, "stage_ru": STAGE_RU.get(stage, stage),
        "waiting": stage == "asked",
        "card_id": str(item.get("card_id") or ""),
        "request_id": str(item.get("request_id") or ""),
        "source": str(item.get("source") or "text"),
        "questions": [{"id": q.get("id") or "q%d" % (i + 1), "text": q.get("text") or ""}
                      for i, q in enumerate(item.get("questions") or []) if isinstance(q, dict)],
        "answers": str(item.get("answers") or ""),
        "draft": draft,
        "recipe_id": str(item.get("recipe_id") or ""),
        "problem": str(item.get("problem") or ""),
        "model": dict(item.get("model") or {}),
        "messages": [{"role": str(m.get("role") or ""), "text": str(m.get("text") or ""),
                      "at": int(m.get("at") or 0)}
                     for m in (item.get("messages") or []) if isinstance(m, dict)],
    }


def _say(item: dict, role: str, text: str) -> None:
    text = _text(text)
    if not text:
        return
    item.setdefault("messages", []).append({"role": role, "text": text, "at": _now()})
    item["messages"] = item["messages"][-40:]


# --------------------------------------------------------------- задание руководителю

def role_hint(room) -> str:
    """Кто есть в комнате — руководитель называет роли так, как их поймёт запуск."""
    out = []
    for person in recipes_mod.room_roles(room):
        out.append("%s (%s)" % (person.get("label") or person.get("profile"),
                                person.get("profile") or ""))
    return "; ".join(out[:8])


def _ask_body(room, item: dict, request_brief: str = "") -> str:
    """Тело карточки: что нужно от руководителя и в каком виде ответить."""
    lead = lead_of(room)
    parts = [
        "КОМУ: руководителю комнаты «%s» (%s)." % (str(room.get("title") or room.get("id") or ""), lead),
        "",
        "ЗАЧЕМ: человек хочет завести РЕЦЕПТ — шаблон повторяющегося дела комнаты.",
        "Рецепт потом запускают одной кнопкой: заполнили поля — приложение создало задачу",
        "с готовым планом и карточки шагов. Поэтому рецепт — это поля ввода и шаги с",
        "зависимостями, а не рассказ.",
        "",
        "ЧТО СКАЗАЛ ЧЕЛОВЕК:",
        str(item.get("text") or "")[:2000],
    ]
    answers = str(item.get("answers") or "").strip()
    if answers:
        parts += ["", "ЧТО ОТВЕТИЛ ЧЕЛОВЕК НА ВОПРОСЫ:", answers[:2000]]
    if request_brief:
        parts += ["", "ЗАВЕРШЁННАЯ ЗАДАЧА, ИЗ КОТОРОЙ ДЕЛАЕМ РЕЦЕПТ:", request_brief[:4000]]
    parts += [
        "",
        "РОЛИ ЭТОЙ КОМНАТЫ (пиши роль так, как она называется здесь, иначе шаг уйдёт не тому):",
        role_hint(room) or "роли не заданы",
        "",
        "КАК ОТВЕТИТЬ — комментарием в этой карточке, первой строкой метка:",
        MARK + " и JSON в ```json-блоке (человеку покажем по-русски, разберём автоматически).",
        "",
        "Если без ответов человека рецепт будет неточным — задай до %d коротких вопросов"
        " ПРОСТЫМИ СЛОВАМИ (без кодов вида D19/Q3, без «уточните параметры»):" % MAX_QUESTIONS,
        '{"stage": "questions", "summary": "одна строка, что уже понятно",'
        ' "questions": ["Сколько клиентов в день вы обслуживаете?", "…"]}',
        "Вопросы — то, без чего нельзя выбрать шаги: кто делает, что на входе, что на выходе,",
        "сколько это занимает, что считается «готово». Не больше %d, каждый — одна мысль." % MAX_QUESTIONS,
        "",
        "Если всё понятно — сразу алгоритм:",
        '{"stage": "draft", "recipe": {"name": "до 120 знаков", "description": "одна строка",'
        ' "fields": [{"id": "f1", "label": "подпись на русском", "kind": "text|file|due",'
        ' "required": true, "hint": "подсказка под полем"}],'
        ' "steps": [{"id": "s1", "role": "роль из списка выше", "title": "что сделать",'
        ' "instruction": "как именно, для исполнителя", "critic": false, "depends_on": []}]}}',
        "",
        "ПРАВИЛА АЛГОРИТМА: полей 1–%d (только то, что человек реально вводит при запуске),"
        " шагов 2–%d, зависимости — id предыдущих шагов, последний шаг обычно с \"critic\": true,"
        " если это проверка итога. Поля «что нужно» с человеческой подписью, без внутренних кодов."
        % (recipes_mod.MAX_FIELDS, recipes_mod.MAX_STEPS),
        "",
        "Ничего не создавай и не запускай: только ответ в этой карточке.",
    ]
    return "\n".join(parts).strip() + "\n"


def _request_brief(cfg, room, root_id: str) -> str:
    """Короткий пересказ завершённой задачи: название, шаги, их итоги (без моделей)."""
    board = _board(room)
    try:
        dbmod = _db()
        with dbmod._conn(cfg, board) as con:
            rows = dbmod._task_rows(con, [root_id])
            links = con.execute("SELECT child_id FROM task_links WHERE parent_id = ?",
                                (root_id,)).fetchall()
            children = dbmod._task_rows(con, [str(r[0]) for r in links])
    except Exception as exc:             # noqa: BLE001
        raise RecipeAgentError("Не удалось прочитать задачу: %s" % _text(exc, 200),
                               "engine_error", 503) from exc
    root = rows.get(root_id) or {}
    if not root:
        raise RecipeAgentError("Задача не найдена на доске комнаты.", "not_found", 404,
                               {"request_id": root_id})
    lines = ["Название: %s" % _text(root.get("title"), 200),
             "Что было нужно: %s" % _text(root.get("body"), 600)]
    ordered = sorted(children.values(), key=lambda c: (c.get("created_at") or 0, str(c.get("id"))))
    for index, card in enumerate(ordered, start=1):
        lines.append("%d) %s — %s: %s" % (index, _text(card.get("title"), 160) or "шаг",
                                          _text(card.get("assignee"), 60) or "исполнитель",
                                          _text(card.get("body"), MAX_CANDIDATE_CHARS)))
    if len(lines) == 2:
        lines.append("(у задачи нет карточек-шагов — обобщи ход работы из описания)")
    return "\n".join(lines)


def _create_card(cfg, room, item: dict, request_brief: str = "") -> str:
    """Карточка руководителю: подзадача проекта или отдельная задача (без родителя)."""
    board = _board(room)
    _board_write_guard(cfg, room)
    lead = _lead_or_fail(room)
    body = _ask_body(room, item, request_brief)
    title = "Рецепт: %s" % _text(item.get("text"), 90) or "Рецепт"
    key = "recipe-%s" % str(item.get("id") or "")
    engine = _engine()
    if item.get("request_id"):
        try:
            return str(engine.create_child_card(cfg, room, board, str(item["request_id"]), lead,
                                                title, body, "recipe_dialog", idem_key=key) or "")
        except Exception as exc:         # noqa: BLE001
            raise RecipeAgentError("Не удалось поставить карточку руководителю: %s"
                                   % _text(getattr(exc, "message_ru", None) or exc, 240),
                                   "engine_error", 502) from exc
    try:
        res = engine.create_subtask_request(cfg, room, "recipe-%s" % key, title, body,
                                            assignee=lead, idem_key=key) or {}
    except Exception as exc:             # noqa: BLE001
        raise RecipeAgentError("Не удалось поставить карточку руководителю: %s"
                               % _text(getattr(exc, "message_ru", None) or exc, 240),
                               "engine_error", 502) from exc
    return str(res.get("root_id") or "")


def start(cfg, room, text: str, *, request_id: str = "") -> dict:
    """Начать разговор: карточка руководителю + запись диалога."""
    text = _text(text)
    if len(text) < 5:
        raise RecipeAgentError("Опишите словами, какое дело повторяется: пара предложений.",
                               "bad_request", 400, {"field": "text"})
    room_id = _room_id(room)
    doc = _load_dialogs(cfg, room)
    item = {"id": _new_dialog_id(room_id, text), "text": text, "at": _now(), "updated_at": _now(),
            "stage": "asked", "source": "request" if request_id else "text",
            "request_id": str(request_id or ""), "card_id": "", "questions": [], "answers": "",
            "draft": None, "recipe_id": "", "problem": "", "messages": [], "seen_at": 0,
            "model": {}}
    brief = _request_brief(cfg, room, request_id) if request_id else ""
    item["card_id"] = _create_card(cfg, room, item, brief)
    item["model"] = _lead_model(cfg, room)
    _say(item, "human", text)
    doc["items"].append(item)
    _save_dialogs(cfg, room, doc)
    return _dialog_public(item)


def _thread(cfg, room, card_id: str) -> list:
    if not card_id:
        return []
    try:
        return _thread_mod().get_thread(cfg, card_id, room=room, board=_board(room)) or []
    except Exception:                    # noqa: BLE001 - лента вторична, ждём следующего прохода
        return []


def parse_reply(text: str) -> dict | None:
    """Комментарий руководителя → ``{stage, questions, draft, summary}`` (без записи)."""
    raw = str(text or "").strip()
    if not raw or not _marked(raw, MARK_ALIASES):
        return None
    data = _json_payload(raw)
    prose = _text(_body_after_mark(raw, MARK_ALIASES).replace("```", " "), 600)
    if not isinstance(data, dict):
        # руководитель ответил прозой: показать человеку как есть, алгоритма нет
        return {"stage": "questions", "questions": [], "draft": None,
                "summary": prose, "problems": ["ответ без JSON — разобрать как алгоритм нельзя"]}
    stage = str(data.get("stage") or "").strip().lower()
    if stage not in ("questions", "draft"):
        stage = "draft" if isinstance(data.get("recipe"), dict) else "questions"
    questions = []
    for index, q in enumerate(data.get("questions") or []):
        text_q = _text(q if isinstance(q, str) else (q or {}).get("text"), 300)
        if text_q:
            questions.append({"id": "q%d" % (index + 1), "text": text_q})
    return {"stage": stage, "questions": questions[:MAX_QUESTIONS],
            "draft": data.get("recipe") if isinstance(data.get("recipe"), dict) else None,
            "summary": _text(data.get("summary"), 600) or prose, "problems": []}


def _apply_reply(cfg, room, item: dict, reply: dict) -> None:
    item["stage"] = "questions" if reply["stage"] == "questions" else "draft"
    item["problem"] = ""
    if reply.get("summary"):
        _say(item, "lead", reply["summary"])
    if reply["stage"] == "questions":
        item["questions"] = reply.get("questions") or []
        for q in item["questions"]:
            _say(item, "questions", q["text"])
        if not item["questions"]:
            item["stage"] = "asked"          # руководитель ответил прозой — ждём дальше
    else:
        item["questions"] = []
        try:
            item["draft"] = recipes_mod.check(reply.get("draft") or {}, room)
            _say(item, "draft", "Готовый алгоритм: «%s»" % item["draft"]["name"])
        except recipes_mod.RecipeError as exc:
            item["draft"] = reply.get("draft") or {}
            item["problem"] = exc.message
            _say(item, "draft", "Прислал алгоритм, но он не проходит проверку: %s" % exc.message)


def _fake_reply(room, item: dict) -> str:
    """Подставной руководитель песочницы: вопросы на первом ходе, алгоритм на втором."""
    text = _text(item.get("text"), 200)
    if not (item.get("answers") or item.get("fix")):
        return (MARK + "\n```json\n" + json.dumps(
            {"stage": "questions", "summary": "Понял: дело повторяется, нужно оформить его шагами.",
             "questions": ["Кто в комнате делает это чаще всего?",
                           "Что человек указывает при запуске — имя клиента, файл, срок?",
                           "Что считается «готово», когда работа закончена?"]},
            ensure_ascii=False) + "\n```")
    roles = [p["label"] for p in recipes_mod.room_roles(room)] or ["руководитель"]
    return (MARK + "\n```json\n" + json.dumps(
        {"stage": "draft", "summary": "Собрал алгоритм по вашему описанию.",
         "recipe": {
             "name": (text.split(":")[0].strip()[:70] or "Повторяющееся дело"),
             "description": "Шаблон: %s" % text[:120],
             "fields": [{"id": "f1", "label": "Что за случай", "kind": "text",
                         "required": True, "hint": "одна строка: с чем работаем"}],
             "steps": [
                 {"id": "s1", "role": roles[0], "title": "Собрать факты",
                  "instruction": "Собери всё по случаю: документы, переписку, сроки.",
                  "critic": False, "depends_on": []},
                 {"id": "s2", "role": roles[-1], "title": "Свести итог",
                  "instruction": "Сведи итог по фактам и скажи, что делать дальше.",
                  "critic": True, "depends_on": ["s1"]}]}},
        ensure_ascii=False) + "\n```")


def _post_comment(cfg, room, card_id: str, text: str, author: str) -> None:
    try:
        _engine().act(cfg, _board(room), card_id, "comment", text=text, author=author)
    except Exception as exc:             # noqa: BLE001 - сообщение вторично для состояния
        if isinstance(exc, RecipeAgentError):
            raise
        raise RecipeAgentError("Не удалось передать сообщение руководителю: %s" % _text(exc, 200),
                               "engine_error", 502) from exc


def _fake_turn(cfg, room, item: dict) -> bool:
    """Один подставной ход руководителя (только песочница с TEAMROOM_RECIPE_FAKE=1)."""
    if not fake_enabled() or not item.get("card_id"):
        return False
    author = lead_of(room) or "mikhail"
    try:
        _post_comment(cfg, room, str(item["card_id"]), _fake_reply(room, item), author)
    except Exception:                    # noqa: BLE001 - без подставного хода просто ждём
        return False
    return True


def _sync(cfg, room, item: dict) -> dict:
    """Прочитать ответ руководителя (идемпотентно) и обновить диалог."""
    if item.get("stage") in ("accepted", "closed"):
        return item
    replies = []
    for entry in _thread(cfg, room, str(item.get("card_id") or "")):
        text = str(entry.get("text") or "")
        if str(entry.get("author_kind") or "") == "mikhail" or _marked(text, (HUMAN_MARK,)):
            continue
        if _marked(text, MARK_ALIASES):
            replies.append(entry)
    fresh = _after_seen(replies, item)
    if not fresh and fake_enabled() and str(item.get("stage")) == "asked":
        if _fake_turn(cfg, room, item):
            fresh = _after_seen([e for e in _thread(cfg, room, str(item.get("card_id") or ""))
                                 if str(e.get("author_kind") or "") != "mikhail"
                                 and _marked(str(e.get("text") or ""), MARK_ALIASES)], item)
    if not fresh:
        return item
    last = fresh[-1]
    reply = parse_reply(str(last.get("text") or ""))
    if not reply:
        return item
    _apply_reply(cfg, room, item, reply)
    item["seen_at"] = int(last.get("at") or 0) or _now()
    item["seen_ref"] = str(last.get("id") or "")
    item["updated_at"] = _now()
    return item


def get(cfg, room, dialog_id: str, *, sync: bool = True) -> dict:
    doc = _load_dialogs(cfg, room)
    item = _find_dialog(doc, dialog_id)
    if item is None:
        raise RecipeAgentError("Разговор о рецепте не найден — начните заново.", "not_found", 404,
                               {"dialog_id": str(dialog_id or "")})
    if sync:
        before = json.dumps(_dialog_public(item), ensure_ascii=False, sort_keys=True)
        _sync(cfg, room, item)
        if json.dumps(_dialog_public(item), ensure_ascii=False, sort_keys=True) != before:
            _save_dialogs(cfg, room, doc)
    return _dialog_public(item)


def latest(cfg, room) -> dict | None:
    """Последний незакрытый разговор — чтобы вкладка помнила, на чём остановились."""
    doc = _load_dialogs(cfg, room)
    for item in reversed(doc.get("items") or []):
        if str(item.get("stage") or "") not in ("accepted", "closed"):
            return get(cfg, room, str(item.get("id") or ""))
    return None


def _must_open(cfg, room, dialog_id: str, action_ru: str) -> tuple[dict, dict]:
    doc = _load_dialogs(cfg, room)
    item = _find_dialog(doc, dialog_id)
    if item is None:
        raise RecipeAgentError("Разговор о рецепте не найден — начните заново.", "not_found", 404)
    if str(item.get("stage") or "") in ("accepted", "closed"):
        raise RecipeAgentError("Этот разговор уже закончен — начните новый.", "conflict", 409)
    _sync(cfg, room, item)
    if str(item.get("stage") or "") == "asked" and not fake_enabled():
        raise RecipeAgentError("Руководитель ещё не ответил — подождите его ответа.", "conflict", 409)
    if str(item.get("stage") or "") not in ("questions", "draft"):
        raise RecipeAgentError("Сейчас %s — %s подождите." % (STAGE_RU.get(
            str(item.get("stage") or ""), "неясно"), action_ru), "conflict", 409)
    return doc, item


def answer(cfg, room, dialog_id: str, text: str) -> dict:
    """Ответы человека на уточняющие вопросы → комментарий руководителю."""
    text = _text(text)
    if not text:
        raise RecipeAgentError("Напишите ответы на вопросы — без них алгоритм будет неточным.",
                               "bad_request", 400, {"field": "answers"})
    doc, item = _must_open(cfg, room, dialog_id, "ответы")
    if str(item.get("stage")) != "questions" or not item.get("questions"):
        raise RecipeAgentError("Уточняющих вопросов нет — можно утверждать алгоритм или просить правку.",
                               "conflict", 409)
    item["answers"] = text
    _say(item, "human", text)
    _post_comment(cfg, room, str(item.get("card_id") or ""),
                  "%s Ответы на вопросы рецепта: %s" % (HUMAN_MARK, text),
                  str((room or {}).get("mikhail") or "mikhail"))
    item["stage"] = "asked"
    item["seen_at"] = _now()
    item["updated_at"] = _now()
    _save_dialogs(cfg, room, doc)
    return get(cfg, room, dialog_id)


def fix(cfg, room, dialog_id: str, text: str) -> dict:
    """«Исправить»: человек говорит, что поправить, руководитель переделывает."""
    text = _text(text)
    if not text:
        raise RecipeAgentError("Напишите, что поправить в алгоритме.", "bad_request", 400,
                               {"field": "text"})
    doc, item = _must_open(cfg, room, dialog_id, "правку")
    item["fix"] = text
    _say(item, "human", "Поправить: " + text)
    _post_comment(cfg, room, str(item.get("card_id") or ""),
                  "%s Поправь алгоритм: %s" % (HUMAN_MARK, text),
                  str((room or {}).get("mikhail") or "mikhail"))
    item["stage"] = "asked"
    item["seen_at"] = _now()
    item["updated_at"] = _now()
    _save_dialogs(cfg, room, doc)
    return get(cfg, room, dialog_id)


def _save_recipe(cfg, room, data: dict, source: str) -> dict:
    """Сохранить рецепт правилами ``server.recipes`` и перевести ошибку в нашу."""
    try:
        return recipes_mod.save(cfg, room, data, None, source=source)
    except recipes_mod.RecipeError as exc:
        raise RecipeAgentError(exc.message, exc.code, exc.http, dict(exc.detail)) from exc


def accept(cfg, room, dialog_id: str, name: str = "") -> dict:
    """«Да»: сохранить присланный алгоритм рецептом комнаты."""
    doc = _load_dialogs(cfg, room)
    item = _find_dialog(doc, dialog_id)
    if item is None:
        raise RecipeAgentError("Разговор о рецепте не найден — начните заново.", "not_found", 404)
    _sync(cfg, room, item)
    draft = item.get("draft") if isinstance(item.get("draft"), dict) else None
    if not draft or str(item.get("stage")) != "draft":
        raise RecipeAgentError("Алгоритма ещё нет — дождитесь ответа руководителя.", "conflict", 409)
    data = dict(draft)
    if _text(name):
        data["name"] = _text(name, recipes_mod.NAME_MAX)
    saved = _save_recipe(cfg, room, data, "dialog")
    item["stage"] = "accepted"
    item["recipe_id"] = saved["id"]
    item["updated_at"] = _now()
    _say(item, "system", "Рецепт сохранён: «%s»." % saved["name"])
    _save_dialogs(cfg, room, doc)
    return {"ok": True, "recipe": saved, "dialog": _dialog_public(item)}


def cancel(cfg, room, dialog_id: str) -> dict:
    doc = _load_dialogs(cfg, room)
    item = _find_dialog(doc, dialog_id)
    if item is None:
        raise RecipeAgentError("Разговор о рецепте не найден.", "not_found", 404)
    item["stage"] = "closed"
    item["updated_at"] = _now()
    _say(item, "system", "Разговор закрыт без рецепта.")
    _save_dialogs(cfg, room, doc)
    return {"ok": True, "dialog": _dialog_public(item)}


# --------------------------------------------------------------- анализ задач

#: W31-FIX3: напоминание о разборе ВЫКЛЮЧЕНО, пока человек не включит его сам.
#: 27.11.2026 при прогоне тестов на живой доске `team` из-за `enabled: True` появились
#: три карточки «Анализ задач: предложить рецепты», одна уже тратила токены. Токены
#: без «да» человека не тратятся: включать — осознанное действие в настройках.
DEFAULT_SETTINGS = {"enabled": False, "every_days": DEFAULT_DAYS, "min_tasks": DEFAULT_MIN_TASKS,
                    "model": {}, "only_propose": True}


def _load_analysis(cfg, room) -> dict:
    doc, info = sf.read_json_doc(str(analysis_path(cfg, _room_id(room))), default=None)
    if not isinstance(doc, dict):
        doc = {"version": STATE_VERSION}
    settings = dict(DEFAULT_SETTINGS)
    settings.update(doc.get("settings") if isinstance(doc.get("settings"), dict) else {})
    doc["settings"] = settings
    doc.setdefault("offers", [])
    doc.setdefault("dismissed", [])
    doc.setdefault("last_run_at", 0)
    doc.setdefault("last_card_at", 0)
    doc.setdefault("last_card_id", "")
    doc.setdefault("seen_at", 0)
    # W31-FIX3: «пора разобрать задачи» — пометка сторожа, а не карточка. Решение
    # принимает человек: «Запустить» (run) или «Позже» (later).
    doc.setdefault("due_at", 0)
    doc.setdefault("due_count", 0)
    doc.setdefault("due_estimate", {})
    doc.setdefault("snoozed_until", 0)
    return doc


def _save_analysis(cfg, room, doc) -> None:
    doc["version"] = STATE_VERSION
    doc["offers"] = (doc.get("offers") or [])[-MAX_OFFERS:]
    doc["dismissed"] = (doc.get("dismissed") or [])[-MAX_DISMISSED:]
    sf.write_json_atomic(str(analysis_path(cfg, _room_id(room))), doc)


def candidates(cfg, room, since: int | None = None, limit: int = MAX_CANDIDATES) -> list[dict]:
    """Завершённые задачи комнаты за окно: заголовок и короткая сводка (только чтение)."""
    board = _board(room)
    tenant = str((room or {}).get("tenant") or ("room:%s" % _room_id(room)))
    since = int(since if since is not None else _now() - DEFAULT_DAYS * 86400)
    try:
        dbmod = _db()
        with dbmod._conn(cfg, board) as con:
            rows = con.execute(
                "SELECT id, title, body, assignee, status, completed_at, created_at FROM tasks "
                "WHERE tenant = ? AND status IN ('done','accepted') "
                "AND COALESCE(completed_at, created_at, 0) >= ? "
                "ORDER BY COALESCE(completed_at, created_at, 0) DESC LIMIT ?",
                (tenant, since, int(limit))).fetchall()
    except Exception:                    # noqa: BLE001 - без доски анализ просто не нужен
        return []
    out = []
    for row in rows:
        item = dict(row)
        item["at"] = int(item.get("completed_at") or item.get("created_at") or 0)
        item["summary"] = _text(item.get("body"), MAX_CANDIDATE_CHARS)
        out.append(item)
    return out


def _norm_words(value: str) -> set:
    text = re.sub(r"[^а-яёa-z0-9 ]+", " ", str(value or "").lower())
    stop = {"и", "в", "на", "по", "с", "для", "из", "от", "до", "не", "как", "что", "это",
            "у", "к", "о", "об", "или", "за", "при", "же", "ли", "бы"}
    return {w for w in text.split() if len(w) > 2 and w not in stop}


def signature_of(data: dict) -> str:
    """Отпечаток предложения: название + шаги (по нему помним отклонённое)."""
    name = _text((data or {}).get("name") or (data or {}).get("title"), 120).lower()
    steps = " ".join(_text(s.get("title"), 80).lower() for s in (data or {}).get("steps") or []
                     if isinstance(s, dict))
    return hashlib.sha1(("%s|%s" % (name, steps)).encode("utf-8")).hexdigest()[:12]


def similar(a: str, b: str) -> bool:
    """Похожи ли два названия (одно в другое или общие слова — большинство)."""
    wa, wb = _norm_words(a), _norm_words(b)
    if not wa or not wb:
        return False
    if wa == wb:
        return True
    common = wa & wb
    if not common:
        return False
    return len(common) / float(min(len(wa), len(wb))) >= 0.6


def _existing_recipes(cfg, room) -> list[dict]:
    try:
        payload = recipes_mod.listing(cfg, room)
    except Exception:                    # noqa: BLE001
        return []
    return list(payload.get("items") or []) + list(payload.get("trash") or [])


def _duplicate_of(offer: dict, existing: list[dict]) -> str:
    """С каким существующим рецептом предложение совпадает (или пустая строка)."""
    name = _text(offer.get("name"), 120)
    for item in existing:
        if similar(name, str(item.get("name") or "")):
            return str(item.get("name") or "")
        steps = " ".join(_text(s.get("title"), 80) for s in item.get("steps") or [])
        mine = " ".join(_text(s.get("title"), 80) for s in offer.get("steps") or []
                        if isinstance(s, dict))
        if mine and steps and similar(mine, steps):
            return str(item.get("name") or "")
    return ""


def settings_view(cfg, room) -> dict:
    doc = _load_analysis(cfg, room)
    settings = dict(doc["settings"])
    lead = _lead_model(cfg, room)
    chosen = settings.get("model") if isinstance(settings.get("model"), dict) else {}
    chosen = {"provider": _text(chosen.get("provider"), 120), "model": _text(chosen.get("model"), 200)}
    models = []
    warning = ""
    try:
        catalog = _models().catalog(cfg)
        for provider in (catalog or {}).get("providers") or []:
            pid = _text(provider.get("id"), 120)
            for model in provider.get("models") or []:
                mid = _text((model or {}).get("id") if isinstance(model, dict) else model, 200)
                if pid and mid:
                    models.append({"provider": pid, "model": mid,
                                   "label": _text((model or {}).get("label"), 200) or mid})
    except Exception as exc:             # noqa: BLE001 - без каталога работаем на модели руководителя
        warning = "Список моделей недоступен: %s" % _text(exc, 160)
    return {"enabled": bool(settings.get("enabled")), "every_days": int(settings.get("every_days") or 0),
            "min_tasks": int(settings.get("min_tasks") or 0),
            "only_propose": bool(settings.get("only_propose", True)),
            "model": chosen, "lead_model": lead,
            "model_label": (("%s · %s" % (chosen["provider"], chosen["model"]))
                            if chosen["model"] and chosen["provider"] else
                            (chosen["model"] or lead.get("label") or "модель руководителя")),
            "models": models, "warning": warning,
            "last_run_at": int(doc.get("last_run_at") or 0),
            "next_run_at": (int(doc.get("last_run_at") or 0) + int(settings.get("every_days") or 0) * 86400)
                           if settings.get("enabled") else 0}


def set_settings(cfg, room, data: dict) -> dict:
    data = data if isinstance(data, dict) else {}
    doc = _load_analysis(cfg, room)
    settings = dict(doc["settings"])
    if "enabled" in data:
        settings["enabled"] = bool(data.get("enabled"))
    days = data.get("every_days")
    if days is not None:
        try:
            days = int(days)
        except (TypeError, ValueError):
            raise RecipeAgentError("«Раз в N дней» — целое число от %d до %d." % (MIN_DAYS, MAX_DAYS),
                                   "bad_request", 400, {"field": "every_days"}) from None
        if not MIN_DAYS <= days <= MAX_DAYS:
            raise RecipeAgentError("«Раз в N дней» — от %d до %d." % (MIN_DAYS, MAX_DAYS),
                                   "bad_request", 400, {"field": "every_days"})
        settings["every_days"] = days
    if "min_tasks" in data:
        try:
            min_tasks = int(data.get("min_tasks"))
        except (TypeError, ValueError):
            raise RecipeAgentError("«Не меньше N задач» — целое число от 2 до 20.",
                                   "bad_request", 400, {"field": "min_tasks"}) from None
        if not 2 <= min_tasks <= 20:
            raise RecipeAgentError("«Не меньше N задач» — от 2 до 20.", "bad_request", 400,
                                   {"field": "min_tasks"})
        settings["min_tasks"] = min_tasks
    model = data.get("model")
    if model is not None:
        if not isinstance(model, dict):
            raise RecipeAgentError("Модель анализа — пара «провайдер и модель» или пусто.",
                                   "bad_request", 400, {"field": "model"})
        provider = _text(model.get("provider"), 120)
        mid = _text(model.get("model"), 200)
        if not mid:
            settings["model"] = {}
        else:
            if not provider:
                raise RecipeAgentError("У модели нужен провайдер.", "bad_request", 400,
                                       {"field": "model"})
            known = settings_view(cfg, room)["models"]
            if known and not any(m["model"].lower() == mid.lower()
                                 and m["provider"].lower() == provider.lower() for m in known):
                raise RecipeAgentError("Модели «%s» нет в каталоге Hermes — выберите из списка."
                                       % mid[:60], "bad_request", 400, {"field": "model"})
            settings["model"] = {"provider": provider, "model": mid}
    doc["settings"] = settings
    _save_analysis(cfg, room, doc)
    return settings_view(cfg, room)


def _digest(cfg, room, tasks: list[dict], existing: list[dict]) -> str:
    """Что увидит модель: названия, короткие сводки, существующие рецепты, отказы."""
    doc = _load_analysis(cfg, room)
    lines = []
    for index, task in enumerate(tasks, start=1):
        lines.append("%d) %s (%s, %s) — %s" % (
            index, _text(task.get("title"), 160), _text(task.get("assignee"), 60) or "—",
            time.strftime("%d.%m", time.localtime(int(task.get("at") or 0))) or "—",
            task.get("summary") or ""))
    recipes_text = "; ".join("%s: %s" % (_text(r.get("name"), 80),
                                        " → ".join(_text(s.get("title"), 60)
                                                   for s in r.get("steps") or []))
                             for r in existing[:20]) or "рецептов пока нет"
    dismissed = "; ".join(_text(d.get("name"), 80) for d in doc.get("dismissed") or [] if d.get("name"))
    return "\n".join(lines), recipes_text, dismissed


def tokens_text(value) -> str:
    """Токены по-человечески: 900 · 4,2 тыс. · 31,8 тыс. (без «0 тыс.» и «31 837 тыс.»)."""
    num = max(0, int(value or 0))
    if num < 1000:
        return "%d" % num
    if num < 10000:
        return ("%.1f" % (num / 1000.0)).replace(".", ",") + " тыс."
    return _thousands(int(round(num / 1000.0))) + " тыс."


def estimate(cfg, room, tasks: list[dict] | None = None) -> dict:
    """Прикидка токенов ДО запуска: вход (сводка задач + рецепты) и выход."""
    tasks = tasks if tasks is not None else candidates(cfg, room)
    existing = _existing_recipes(cfg, room)
    digest, recipes_text, _dismissed = _digest(cfg, room, tasks, existing)
    chars = len(digest) + len(recipes_text) + 1200
    base = {}
    try:
        base = _usage().base_estimate(getattr(cfg, "state_dir", None))
    except Exception:                    # noqa: BLE001
        base = {}
    base_tokens = int(base.get("base_tokens") or 0)
    input_tokens = base_tokens + int(chars / CHARS_PER_TOKEN)
    return {"input_tokens": int(input_tokens), "output_tokens": int(OFFER_OUTPUT_TOKENS),
            "chars": chars, "base_tokens": base_tokens, "tasks": len(tasks),
            "source": _text(base.get("source"), 32) or "оценка",
            "text": "≈ %s токенов на входе и %s на выходе — один лёгкий проход."
                    % (tokens_text(input_tokens), tokens_text(OFFER_OUTPUT_TOKENS))}


def _thousands(value) -> str:
    num = int(value or 0)
    whole, rest = divmod(num, 1000)
    if not whole:
        return str(num)
    return ("%d %03d" % (whole, rest)) if rest else str(whole)


def _offer_body(room, tasks: list[dict], existing: list[dict], digest: str,
                recipes_text: str, dismissed: str) -> str:
    parts = [
        "КОМУ: руководителю комнаты «%s» (%s)." % (str(room.get("title") or room.get("id") or ""),
                                                   lead_of(room)),
        "",
        "ЗАЧЕМ: лёгкий разбор завершённых задач. Посмотри, какие дела повторялись,",
        "и предложи из них РЕЦЕПТЫ (шаблон: поля ввода + шаги с зависимостями).",
        "Ты НЕ создаёшь рецепты и НЕ запускаешь работу — только предлагаешь человеку.",
        "",
        "ЗАВЕРШЁННЫЕ ЗАДАЧИ (%d):" % len(tasks),
        digest[:12000],
        "",
        "УЖЕ ЕСТЬ РЕЦЕПТЫ (не предлагай похожие — это отказ):",
        recipes_text[:3000],
    ]
    if dismissed:
        parts += ["", "ЧЕЛОВЕК УЖЕ ОТКЛОНИЛ ЭТИ ПРЕДЛОЖЕНИЯ (не повторяй):", dismissed[:2000]]
    parts += [
        "",
        "РОЛИ КОМНАТЫ:", role_hint(room) or "роли не заданы",
        "",
        "КАК ОТВЕТИТЬ — комментарием в этой карточке, первой строкой метка %s и JSON:" % OFFER_MARK,
        "```json",
        '{"offers": [{"name": "короткое название рецепта", "why": "почему: какие задачи похожи",'
        ' "tasks": ["название задачи 1", "название задачи 2"],'
        ' "fields": [{"id": "f1", "label": "поле на русском", "kind": "text", "required": true}],'
        ' "steps": [{"id": "s1", "role": "роль из списка", "title": "шаг",'
        ' "instruction": "как сделать", "critic": false, "depends_on": []}]}]}',
        "```",
        "Не больше 3 предложений. Задач в одном предложении — не меньше 2 (иначе это не повтор).",
        "Нет повторов — ответь `{\"offers\": []}` и одной строкой почему.",
        "Вопросов человеку не задавай: если данных мало — не предлагай этот рецепт.",
    ]
    return "\n".join(parts).strip() + "\n"


def _analysis_card(cfg, room, tasks: list[dict]) -> str:
    """Карточка руководителю «предложи рецепты» — ТОЛЬКО по явному «Запустить»."""
    board = _board(room)
    _board_write_guard(cfg, room)
    lead = _lead_or_fail(room)
    doc = _load_analysis(cfg, room)
    existing = _existing_recipes(cfg, room)
    digest, recipes_text, dismissed = _digest(cfg, room, tasks, existing)
    body = _offer_body(room, tasks, existing, digest, recipes_text, dismissed)
    settings = doc["settings"]
    chosen = settings.get("model") if isinstance(settings.get("model"), dict) else {}
    card_room = dict(room)
    if chosen.get("model"):
        # Модель анализа — выбор человека; иначе руководитель работает на СВОЕЙ модели
        # (у комнаты общей модели нет). Путь тот же, что у «Второго мнения».
        overrides = dict(room.get("agent_models") or {}) if isinstance(room.get("agent_models"), dict) else {}
        overrides[lead] = {"provider": _text(chosen.get("provider"), 120),
                           "model": _text(chosen.get("model"), 200)}
        card_room["agent_models"] = overrides
    key = "recipe-analysis-%s-%s" % (_room_id(room), hashlib.sha1(
        ("%s|%s" % (_now(), len(tasks))).encode("utf-8")).hexdigest()[:8])
    try:
        res = _engine().create_subtask_request(
            cfg, card_room, "recipe-analysis-%s" % _now(), "Анализ задач: предложить рецепты",
            body, assignee=lead, idem_key=key) or {}
    except Exception as exc:             # noqa: BLE001
        raise RecipeAgentError("Не удалось поставить анализ руководителю: %s"
                               % _text(getattr(exc, "message_ru", None) or exc, 240),
                               "engine_error", 502) from exc
    return str(res.get("root_id") or "")


def parse_offers(text: str) -> dict | None:
    """Комментарий руководителя по анализу → список предложений (без записи)."""
    raw = str(text or "").strip()
    if not raw or not _marked(raw, OFFER_ALIASES):
        return None
    data = _json_payload(raw)
    if not isinstance(data, dict):
        return {"offers": [], "summary": _text(_body_after_mark(raw, OFFER_ALIASES), 400),
                "problems": ["ответ без JSON — предложения не разобрать"]}
    out = []
    for raw_offer in data.get("offers") or []:
        if not isinstance(raw_offer, dict):
            continue
        tasks = [str(t) if not isinstance(t, dict) else _text(t.get("title"), 160)
                 for t in raw_offer.get("tasks") or []]
        out.append({"name": _text(raw_offer.get("name") or raw_offer.get("title"), 120),
                    "why": _text(raw_offer.get("why"), 400),
                    "tasks": [t for t in tasks if t][:8],
                    "fields": raw_offer.get("fields") if isinstance(raw_offer.get("fields"), list) else [],
                    "steps": raw_offer.get("steps") if isinstance(raw_offer.get("steps"), list) else []})
    return {"offers": out[:3], "summary": _text(data.get("summary"), 400), "problems": []}


def _offer_public(item: dict) -> dict:
    state = str(item.get("state") or "new")
    return {"id": str(item.get("id") or ""), "name": str(item.get("name") or ""),
            "why": str(item.get("why") or ""), "tasks": list(item.get("tasks") or []),
            "fields": list(item.get("fields") or []), "steps": list(item.get("steps") or []),
            "state": state, "state_ru": OFFER_STATE_RU.get(state, state),
            "at": int(item.get("at") or 0), "recipe_id": str(item.get("recipe_id") or ""),
            "problem": str(item.get("problem") or "")}


def prompt_view(doc) -> dict:
    """Плашка вкладки «Рецепты»: «Пора провести разбор задач (новых: N, …)».

    Показывается ТОЛЬКО когда сторож пометил ``due`` (``tick``), и только человек
    решает: «Запустить» (``run``) или «Позже» (``later``). Карточку сама система
    не ставит никогда.
    """
    due_at = int(doc.get("due_at") or 0)
    count = int(doc.get("due_count") or 0)
    est = doc.get("due_estimate") if isinstance(doc.get("due_estimate"), dict) else {}
    tokens = int(est.get("input") or 0)
    out = {"show": bool(due_at), "count": count, "tokens": tokens,
           "output_tokens": int(est.get("output") or 0), "due_at": due_at,
           "snoozed_until": int(doc.get("snoozed_until") or 0), "text": ""}
    if due_at:
        out["text"] = "Пора провести разбор задач (новых: %d, примерно %s токенов)" % (
            count, tokens_text(tokens))
    return out


def analysis_view(cfg, room) -> dict:
    doc = _load_analysis(cfg, room)
    settings = settings_view(cfg, room)
    tasks = candidates(cfg, room)
    offers = [_offer_public(o) for o in doc.get("offers") or [] if isinstance(o, dict)]
    offers.sort(key=lambda o: (o["state"] != "new", -o["at"]))
    return {"settings": settings, "estimate": estimate(cfg, room, tasks),
            "candidates": {"count": len(tasks),
                           "items": [{"id": str(t.get("id") or ""), "title": _text(t.get("title"), 160),
                                      "at": int(t.get("at") or 0)} for t in tasks[:10]]},
            "offers": offers, "dismissed_count": len(doc.get("dismissed") or []),
            "last_run_at": int(doc.get("last_run_at") or 0),
            "prompt": prompt_view(doc),
            "waiting": _offers_pending(cfg, room, doc)}


def run(cfg, room, *, force: bool = False) -> dict:
    """Поставить анализ руководителю сейчас (кнопка «Запустить» / «Проверить сейчас»).

    Единственный путь, которым появляется карточка «Анализ задач»: его вызывает
    ТОЛЬКО человек (маршрут ``POST …/analysis/run``). Сторож сюда не ходит.
    """
    doc = _load_analysis(cfg, room)
    now = _now()
    if doc.get("last_card_id") and not force and _offers_pending(cfg, room, doc):
        raise RecipeAgentError("Анализ уже идёт — дождитесь ответа руководителя.", "conflict", 409)
    tasks = candidates(cfg, room)
    if not tasks:
        raise RecipeAgentError("Завершённых задач пока нет — предлагать нечего.", "conflict", 409)
    card_id = _analysis_card(cfg, room, tasks)
    doc["last_card_id"] = card_id
    doc["last_card_at"] = now
    doc["last_run_at"] = now
    doc["seen_at"] = 0
    doc["last_count"] = len(tasks)
    # Пометка «пора» снята: человек уже нажал «Запустить».
    doc["due_at"] = 0
    doc["due_count"] = 0
    doc["due_estimate"] = {}
    doc["snoozed_until"] = 0
    _save_analysis(cfg, room, doc)
    return {"ok": True, "card_id": card_id, "tasks": len(tasks),
            "message": "Анализ поставлен руководителю: он только предложит рецепты, создавать будете вы."}


def later(cfg, room, days: int | None = None) -> dict:
    """«Позже»: отложить напоминание на N дней (по умолчанию — как в настройках).

    Карточку не ставит и к модели не ходит: только снимает пометку ``due`` и
    запоминает, до какого срока про разбор не напоминать.
    """
    doc = _load_analysis(cfg, room)
    settings = doc["settings"]
    if days is None:
        days = int(settings.get("every_days") or DEFAULT_DAYS)
    try:
        days = int(days)
    except (TypeError, ValueError):
        raise RecipeAgentError("«Позже» — целое число дней от %d до %d." % (MIN_DAYS, MAX_DAYS),
                               "bad_request", 400, {"field": "days"}) from None
    if not MIN_DAYS <= days <= MAX_DAYS:
        raise RecipeAgentError("«Позже» — от %d до %d дней." % (MIN_DAYS, MAX_DAYS),
                               "bad_request", 400, {"field": "days"})
    now = _now()
    doc["due_at"] = 0
    doc["due_count"] = 0
    doc["due_estimate"] = {}
    doc["snoozed_until"] = now + days * 86400
    doc["last_prompt_at"] = now
    _save_analysis(cfg, room, doc)
    return {"ok": True, "days": days, "snoozed_until": int(doc["snoozed_until"]),
            "message": "Отложили на %d %s — напомним позже."
                       % (days, "день" if days % 10 == 1 and days % 100 != 11 else
                          ("дня" if days % 10 in (2, 3, 4) and days % 100 not in (12, 13, 14)
                           else "дней"))}


def _offers_pending(cfg, room, doc) -> bool:
    """Карточка ещё не отвечена: комментария с меткой после неё нет."""
    card_id = str(doc.get("last_card_id") or "")
    if not card_id:
        return False
    for entry in _thread(cfg, room, card_id):
        if _marked(str(entry.get("text") or ""), OFFER_ALIASES):
            return False
    return True


def sync_offers(cfg, room, *, fake: bool | None = None) -> dict:
    """Разобрать ответ руководителя по анализу; отсеять дубли и отклонённое."""
    doc = _load_analysis(cfg, room)
    card_id = str(doc.get("last_card_id") or "")
    if not card_id:
        return {"ok": True, "added": 0, "suppressed": [], "offers": []}
    replies = [e for e in _thread(cfg, room, card_id)
               if str(e.get("author_kind") or "") != "mikhail" and _marked(str(e.get("text") or ""),
                                                                           OFFER_ALIASES)]
    if not replies and (fake if fake is not None else fake_enabled()):
        # Подставной ответ песочницы: одно предложение из двух последних задач.
        tasks = candidates(cfg, room)
        if tasks:
            payload = {"offers": [{
                "name": "Разбор повторяющихся дел", "why": "две задачи похожи по ходу работы",
                "tasks": [_text(t.get("title"), 160) for t in tasks[:2]] or ["задача"],
                "fields": [{"id": "f1", "label": "Что за случай", "kind": "text", "required": True}],
                "steps": [{"id": "s1", "role": (recipes_mod.room_roles(room) or [{}])[0].get("label")
                           or "руководитель", "title": "Разобрать случай", "instruction": "",
                           "critic": False, "depends_on": []},
                          {"id": "s2", "role": "руководитель", "title": "Свести итог",
                           "instruction": "", "critic": True, "depends_on": ["s1"]}]}]}
            try:
                _post_comment(cfg, room, card_id, OFFER_MARK + "\n```json\n"
                              + json.dumps(payload, ensure_ascii=False) + "\n```",
                              lead_of(room) or "mikhail")
                replies = [e for e in _thread(cfg, room, card_id)
                           if _marked(str(e.get("text") or ""), OFFER_ALIASES)
                           and str(e.get("author_kind") or "") != "mikhail"]
            except Exception:            # noqa: BLE001 - без подставного хода ждём живого
                replies = []
    if not replies:
        return {"ok": True, "added": 0, "suppressed": [], "offers": [
            _offer_public(o) for o in doc.get("offers") or []]}
    fresh = _after_seen(replies, doc)
    if not fresh:
        return {"ok": True, "added": 0, "suppressed": [], "offers": [
            _offer_public(o) for o in doc.get("offers") or []], "duplicate": True}
    last = fresh[-1]
    last_at = int(last.get("at") or 0)
    parsed = parse_offers(str(last.get("text") or "")) or {"offers": [], "problems": []}
    existing = _existing_recipes(cfg, room)
    dismissed = {str(d.get("signature") or "") for d in doc.get("dismissed") or []}
    known = {str(o.get("signature") or "") for o in doc.get("offers") or []}
    added, suppressed = 0, []
    for offer in parsed["offers"]:
        signature = signature_of(offer)
        if not _text(offer.get("name")):
            continue
        dup = _duplicate_of(offer, existing)
        if dup:
            suppressed.append({"name": offer["name"], "reason": "уже есть рецепт «%s»" % dup})
            continue
        if signature in dismissed:
            suppressed.append({"name": offer["name"], "reason": "вы это уже отклоняли"})
            continue
        if signature in known:
            suppressed.append({"name": offer["name"], "reason": "уже предлагалось"})
            continue
        problem = ""
        try:
            clean = recipes_mod.check(offer, room)
        except recipes_mod.RecipeError as exc:
            clean, problem = offer, exc.message
        doc["offers"].append({"id": "of-" + signature, "signature": signature,
                              "name": offer["name"], "why": offer.get("why") or "",
                              "tasks": offer.get("tasks") or [],
                              "fields": clean.get("fields") or [], "steps": clean.get("steps") or [],
                              "state": "new", "at": _now(), "recipe_id": "", "problem": problem})
        known.add(signature)
        added += 1
    doc["seen_at"] = last_at or _now()
    doc["seen_ref"] = str(last.get("id") or "")
    _save_analysis(cfg, room, doc)
    return {"ok": True, "added": added, "suppressed": suppressed,
            "summary": parsed.get("summary") or "", "problems": parsed.get("problems") or [],
            "offers": [_offer_public(o) for o in doc.get("offers") or []]}


def dismiss(cfg, room, offer_id: str) -> dict:
    doc = _load_analysis(cfg, room)
    offer = next((o for o in doc.get("offers") or [] if str(o.get("id") or "") == str(offer_id)), None)
    if offer is None:
        raise RecipeAgentError("Предложение не найдено — возможно, оно уже разобрано.",
                               "not_found", 404, {"offer_id": str(offer_id or "")})
    offer["state"] = "dismissed"
    doc["dismissed"].append({"signature": str(offer.get("signature") or signature_of(offer)),
                             "name": offer.get("name") or "", "at": _now()})
    _save_analysis(cfg, room, doc)
    return {"ok": True, "offer": _offer_public(offer),
            "message": "Отклонено: это предложение больше не появится."}


def accept_offer(cfg, room, offer_id: str, name: str = "") -> dict:
    doc = _load_analysis(cfg, room)
    offer = next((o for o in doc.get("offers") or [] if str(o.get("id") or "") == str(offer_id)), None)
    if offer is None:
        raise RecipeAgentError("Предложение не найдено — возможно, оно уже разобрано.",
                               "not_found", 404, {"offer_id": str(offer_id or "")})
    if str(offer.get("state")) == "created":
        raise RecipeAgentError("Рецепт из этого предложения уже создан.", "conflict", 409,
                               {"offer_id": offer_id})
    data = {"name": _text(name) or offer.get("name") or "", "description": offer.get("why") or "",
            "fields": offer.get("fields") or [], "steps": offer.get("steps") or []}
    saved = _save_recipe(cfg, room, data, "analysis")
    offer["state"] = "created"
    offer["recipe_id"] = saved["id"]
    _save_analysis(cfg, room, doc)
    return {"ok": True, "recipe": saved, "offer": _offer_public(offer)}


def _due_estimate(cfg, room, tasks: list[dict]) -> dict:
    """Прикидка токенов для пометки «пора»: считается один раз, показывается человеку."""
    try:
        est = estimate(cfg, room, tasks)
    except Exception:                    # noqa: BLE001 - прикидка не должна мешать пометке
        est = {}
    return {"input": int(est.get("input_tokens") or 0),
            "output": int(est.get("output_tokens") or 0), "tasks": len(tasks)}


def tick(cfg, room) -> dict:
    """Раз в N дней — только ПОМЕТИТЬ «пора разобрать задачи». Карточку не ставит.

    W31-FIX3: сторож не создаёт ни карточек, ни запусков. Он пишет в состояние
    разбора ``due_at`` / ``due_count`` / ``due_estimate``; вкладка «Рецепты»
    показывает плашку «Пора провести разбор задач (новых: N, примерно K токенов)»
    с кнопками «Запустить» (``run``) и «Позже» (``later``). Токены тратятся только
    после «Запустить»: до этого модель не вызывается ни разу.

    Возвращает ``{offered: False (всегда), due, tasks, why}``; любая ошибка — тихая
    для сторожа.
    """
    doc = _load_analysis(cfg, room)
    settings = doc["settings"]
    if not settings.get("enabled"):
        return {"offered": False, "due": False, "tasks": 0, "why": "выключено"}
    now = _now()
    if int(doc.get("due_at") or 0):
        # Уже ждёт решения человека: не пересчитываем и не «напоминаем» повторно.
        return {"offered": False, "due": True, "tasks": int(doc.get("due_count") or 0),
                "tokens": int((doc.get("due_estimate") or {}).get("input") or 0),
                "why": "ждёт вашего решения"}
    snoozed = int(doc.get("snoozed_until") or 0)
    if snoozed and now < snoozed:
        return {"offered": False, "due": False, "tasks": 0, "why": "отложено"}
    every = int(settings.get("every_days") or DEFAULT_DAYS) * 86400
    last = int(doc.get("last_run_at") or 0)
    if last and now - last < every:
        return {"offered": False, "due": False, "tasks": 0, "why": "ещё рано"}
    since = max(last, now - 90 * 86400)
    tasks = candidates(cfg, room, since=since)
    need = int(settings.get("min_tasks") or DEFAULT_MIN_TASKS)
    if len(tasks) < need:
        return {"offered": False, "due": False, "tasks": len(tasks), "why": "мало задач"}
    if _offers_pending(cfg, room, doc):
        return {"offered": False, "due": False, "tasks": len(tasks), "why": "ждём ответа"}
    est = _due_estimate(cfg, room, tasks)
    doc.update({"due_at": now, "due_count": len(tasks), "due_estimate": est,
                "last_count": len(tasks)})
    _save_analysis(cfg, room, doc)
    return {"offered": False, "due": True, "tasks": len(tasks), "tokens": int(est["input"]),
            "count": len(tasks), "text": prompt_view(doc)["text"], "why": "пора разобрать задачи"}


__all__ = ["RecipeAgentError", "MARK", "MARK_ALIASES", "OFFER_MARK", "HUMAN_MARK",
           "STAGE_RU", "OFFER_STATE_RU", "fake_enabled", "lead_of",
           "dialogs_path", "analysis_path", "start", "get", "latest", "answer", "fix", "accept",
           "cancel", "parse_reply", "role_hint", "candidates", "settings_view", "set_settings",
           "estimate", "analysis_view", "prompt_view", "run", "later", "sync_offers", "dismiss",
           "accept_offer", "tick", "DEFAULT_SETTINGS",
           "parse_offers", "signature_of", "similar", "tokens_text"]
