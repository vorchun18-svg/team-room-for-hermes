"""Диалог по запросу: комментарии канбана как лента + сообщение руководителю комнаты.

Две задачи (docs/WAVE8-RULES.md, Блок A + ДОПОЛНЕНИЕ Д1):

1. **Чтение** — ``get_thread(cfg, root_id)``: все комментарии канбана по дереву
   запроса (корень + дочерние) по времени, события («приняли уточнение», «версия
   готова», «руководитель передал») и итоги карточек «Уточнение» как сообщения
   команды. Форма элемента — ``ThreadItem`` из договорённости волны 8.
2. **Запись** — ``post_message``: сообщение Михаила идёт РУКОВОДИТЕЛЮ комнаты
   (комментарий на корень + отдельная карточка «Уточнение от Михаила»), а он уже
   раздаёт работу участникам. Рассылка на все дочерние карточки отменена.

Правила записи те же, что у ``engine_write``: только CLI ``hermes kanban``
(``--board`` перед глаголом), тело — ``--body-file``, комментарий — позициональным
текстом после ``--``, значения argv проверяет ``engine_write_guard``
(id по регэкспу, принадлежность дереву запроса, NUL). Чтение — SQLite ``mode=ro``.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from pathlib import Path

try:  # пакетный и «плоский» импорт (python server/app.py)
    from . import answers
    from .app_common import MAX_TEXT
    from .config import path_allowed
    from .engine_cli import EngineWriteError, _run, _run_json, opt
    from .engine_write import CREATED_BY, _write_body
    from .engine_write_guard import assert_task_id, no_nul, tree_ids
except ImportError:  # pragma: no cover
    import answers  # type: ignore
    from app_common import MAX_TEXT  # type: ignore
    from config import path_allowed  # type: ignore
    from engine_cli import EngineWriteError, _run, _run_json, opt  # type: ignore
    from engine_write import CREATED_BY, _write_body  # type: ignore
    from engine_write_guard import assert_task_id, no_nul, tree_ids  # type: ignore

DEFAULT_BOARD = "team"
TENANT_PREFIX = "room:"

#: метка сообщения Михаила (по ней исполнители обязаны перечитать комментарии)
NOTE_MARK = "[Михаил · уточнение]"
#: метка передачи работы руководителем участнику
LEAD_MARK = "[Руководитель → "
NOTE_TITLE = "Уточнение от Михаила"
NOTE_KEY_PREFIX = "tr-note-"
URGENT_LINE = ("СРОЧНО: пересмотри план целиком — перечитай все карточки запроса, "
               "пересобери оставшиеся шаги (новые карточки — kanban_create) и отрази новый "
               "план в своде. Уже идущую работу не останавливай.")

#: какие инструменты канбана реально есть у профиля-руководителя (tools/kanban_tools.py):
#: kanban_show, kanban_comment (можно писать на чужие карточки), kanban_create,
#: kanban_link, kanban_complete, kanban_block, kanban_unblock, kanban_attach…
#: kanban_list — только «оркестраторский» (у воркера скрыт); переназначения
#: (assign/reassign) среди инструментов агента НЕТ — только CLI.
LEAD_TOOLS = ("kanban_show", "kanban_comment", "kanban_create", "kanban_link", "kanban_complete")

#: роли по-русски (ключи профилей и API не меняем — только подписи)
ROLE_RU = {"orchestrator": "руководитель комнаты", "critic": "критик",
           "worker": "исполнитель", "observer": "наблюдатель"}

_STATUS_RU = {"queued": "в очереди", "triage": "ждёт разбора", "scheduled": "запланирован",
              "todo": "ждёт зависимостей", "ready": "готов к работе", "running": "в работе",
              "review": "на проверке", "blocked": "ждёт решения", "done": "готов",
              "archived": "в архиве"}
_MAX_ITEMS = 500


# ---------------------------------------------------------------- служебное

def _room(cfg, room):
    """Комната словарём: принимаем и dict, и id (интерфейс волны 8 — ``room_id``)."""
    if isinstance(room, dict):
        return room
    if isinstance(room, str) and room.strip():
        try:
            from . import rooms as rooms_mod
        except ImportError:  # pragma: no cover
            import rooms as rooms_mod  # type: ignore
        return rooms_mod.get_room(cfg, room.strip()) or {}
    return {}


def _board_of(room: dict, board: str | None = None) -> str:
    return str(board or (room or {}).get("board") or DEFAULT_BOARD)


def _tenant(room: dict) -> str:
    return str(room.get("tenant") or (TENANT_PREFIX + str(room.get("id") or ""))).strip()


def _db(cfg, board: str) -> Path:
    return Path(cfg.kanban_db(board))


def _connect(cfg, board: str):
    """Соединение ``mode=ro`` или None, если БД нет (доска не создана — не ошибка)."""
    db = _db(cfg, board)
    if not db.exists():
        return None
    try:
        con = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True, timeout=5)
    except sqlite3.Error:
        return None
    con.row_factory = sqlite3.Row
    return con


def _rows(con, sql: str, params) -> list:
    try:
        return con.execute(sql, params).fetchall()
    except sqlite3.Error:
        return []


def _marks(values) -> str:
    return ",".join("?" * len(values))


def _json(value) -> dict:
    try:
        data = json.loads(value or "{}", strict=False)
        return data if isinstance(data, dict) else {}
    except (json.JSONDecodeError, TypeError, ValueError):
        return {}


def card_row(cfg, board: str, task_id: str) -> dict:
    """Строка карточки (status/assignee/title/ошибки) — только чтение."""
    con = _connect(cfg, board)
    if con is None:
        return {}
    try:
        rows = _rows(con, "SELECT * FROM tasks WHERE id = ?", (str(task_id),))
    finally:
        con.close()
    return dict(rows[0]) if rows else {}


# ---------------------------------------------------------------- подписи ролей

def _profiles(cfg) -> dict:
    """Ростер: {профиль: описание} (файлы профилей, без subprocess)."""
    try:
        from . import engine_write as ew
    except ImportError:  # pragma: no cover
        import engine_write as ew  # type: ignore
    try:
        items = ew.list_profiles(cfg) or []
    except Exception:  # noqa: BLE001 - ростер недоступен → показываем имя профиля
        return {}
    out = {}
    for item in items:
        if isinstance(item, dict) and item.get("name"):
            out[str(item["name"])] = str(item.get("description") or "")
    return out


def _display_name(profile: str, room: dict, roster: dict) -> str:
    """Имя человека из описания профиля («Антон — юрист …» → «Антон»), иначе профиль."""
    for member in (room or {}).get("members") or []:
        if isinstance(member, dict) and member.get("profile") == profile and member.get("title"):
            return str(member["title"])
    description = str(roster.get(profile) or "").strip()
    for dash in (" — ", " - ", " – "):
        if dash in description:
            head = description.split(dash, 1)[0].strip()
            if 0 < len(head) <= 40:
                return head
    return profile


def role_label(profile: str, room: dict) -> str:
    """Роль по-русски: руководитель комнаты / критик / исполнитель / наблюдатель."""
    name = str(profile or "").strip()
    if not name:
        return ""
    if name == str((room or {}).get("orchestrator") or ""):
        return ROLE_RU["orchestrator"]
    if name == str((room or {}).get("critic") or ""):
        return ROLE_RU["critic"]
    for member in (room or {}).get("members") or []:
        if isinstance(member, dict) and member.get("profile") == name:
            return ROLE_RU.get(str(member.get("role") or "worker"), "участник")
    return "участник"


def author_label(cfg, room: dict, profile: str, roster: dict | None = None) -> str:
    """Подпись автора: «Михаил» или «Антон · исполнитель» (не `biz-lawyer`)."""
    name = str(profile or "").strip()
    if not name or name == "mikhail":
        return "Михаил"
    if name == "system":
        return "Диалог запроса"
    roster = _profiles(cfg) if roster is None else roster
    role = role_label(name, room)
    person = _display_name(name, room, roster)
    return f"{person} · {role}" if role else person


# ---------------------------------------------------------------- карточки «Уточнение»

def _note_cards(con, root_id: str, tenant: str) -> list[dict]:
    """Карточки «Уточнение от Михаила» этого запроса (не дочерние корня, tenant комнаты)."""
    key_like = NOTE_KEY_PREFIX + root_id + "%"
    rows = _rows(con, "SELECT id,title,body,status,assignee,created_at,idempotency_key "
                      "FROM tasks WHERE COALESCE(idempotency_key,'') LIKE ?", (key_like,))
    if not rows and tenant:
        rows = _rows(con, "SELECT id,title,body,status,assignee,created_at,idempotency_key "
                          "FROM tasks WHERE title = ? AND COALESCE(tenant,'') = ?",
                     (NOTE_TITLE, tenant))
    out = []
    for row in rows:
        body = str(row["body"] or "")
        if root_id not in body:      # чужая карточка с похожим ключом — не наша
            continue
        out.append(dict(row))
    return out


def _open_note(cfg, board: str, root_id: str, tenant: str) -> dict:
    """Незавершённая карточка «Уточнение» этого запроса (для пакетирования)."""
    con = _connect(cfg, board)
    if con is None:
        return {}
    try:
        cards = _note_cards(con, root_id, tenant)
    finally:
        con.close()
    for card in cards:
        if str(card.get("status") or "") in ("triage", "todo", "ready", "running"):
            return card
    return {}


# ---------------------------------------------------------------- чтение ленты

def _comment_item(cfg, room: dict, row, roster: dict) -> dict:
    author = str(row["author"] or "mikhail")
    mikhail = author in ("mikhail", "Михаил")
    body = str(row["body"] or "")
    item = {"id": "c%d" % int(row["id"]), "at": int(row["created_at"] or 0),
            "author_kind": "mikhail" if mikhail else "agent",
            "author": "mikhail" if mikhail else author,
            "author_label": "Михаил" if mikhail else author_label(cfg, room, author, roster),
            "text": body, "task_id": str(row["task_id"] or "") or None,
            "kind": "message" if mikhail else "comment"}
    # W14-D §1: ответ на вопрос — ОТДЕЛЬНЫЙ тип записи со ссылкой на вопрос,
    # а не обычное сообщение в общем чате.
    kind = answers.answer_kind_of(body) if mikhail else ""
    if kind:
        item["kind"] = "answer"
        item["answer_kind"] = kind
        item["question_id"] = answers.question_id_of(body) or None
        item["question_text"] = answers.question_text_of(body) or None
        item["text"] = answers.answer_text_of(body)
        item["text_mark"] = body
    return item


_EVENT_TEXT = {
    "decomposed": "Руководитель разложил запрос на карточки.",
    "blocked": "Карточка ждёт решения.",
    "unblocked": "Работа продолжена.",
    "completed": "Шаг готов.",
    "review_requested": "Карточка ушла на проверку.",
    "reclaimed": "Карточка возвращена в очередь.",
}
_EVENT_KINDS = tuple(_EVENT_TEXT)


def _event_item(row, titles: dict) -> dict | None:
    kind = str(row["kind"] or "")
    if kind not in _EVENT_TEXT:
        return None
    payload = _json(row["payload"])
    task_id = str(row["task_id"] or "")
    title = titles.get(task_id) or ""
    text = _EVENT_TEXT[kind]
    if kind == "decomposed":
        children = payload.get("child_ids")
        if isinstance(children, list) and children:
            text = "Руководитель разложил запрос: карточек — %d." % len(children)
    elif title:
        text = "%s «%s»" % (_EVENT_TEXT[kind].rstrip("."), _short(title, 80))
    return {"id": "e%d" % int(row["id"]), "at": int(row["created_at"] or 0),
            "author_kind": "system", "author": "system", "author_label": "Диалог запроса",
            "text": text, "task_id": task_id or None, "kind": "event"}


def _short(text: str, limit: int = 90) -> str:
    text = " ".join(str(text or "").split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _run_rows(con, ids: list) -> list:
    if not ids:
        return []
    return _rows(con, "SELECT id,task_id,profile,status,outcome,summary,ended_at,metadata "
                      "FROM task_runs WHERE task_id IN (%s) ORDER BY COALESCE(ended_at,0), id"
                 % _marks(ids), ids)


def _versions(con, root_id: str) -> list:
    """Завершённые run'ы корня → «Версия N готова» (счёт по порядку завершения)."""
    rows = _run_rows(con, [root_id])
    out, number = [], 0
    for row in rows:
        status = str(row["status"] or "")
        if not (status in ("done", "completed") or str(row["outcome"] or "") == "completed"):
            continue
        number += 1
        out.append({"id": "v%d" % int(row["id"]), "at": int(row["ended_at"] or 0),
                    "author_kind": "system", "author": "system",
                    "author_label": "Диалог запроса",
                    "text": "Версия %d готова." % number, "task_id": root_id, "kind": "event"})
    return out


def get_thread(cfg, root_id, *, room=None, board=None, limit: int = _MAX_ITEMS) -> list:
    """Лента диалога запроса ``ThreadItem[]`` по времени (см. договорённость волны 8)."""
    root = assert_task_id(root_id, "rid")
    room = _room(cfg, room)
    board = _board_of(room, board)
    con = _connect(cfg, board)
    if con is None:
        return []
    try:
        notes = _note_cards(con, root, _tenant(room))
        tree = set(tree_ids(cfg, board, root)) or {root}
        tree |= {str(note["id"]) for note in notes}
        ids = sorted(tree)
        roster = _profiles(cfg)
        titles = {str(r["id"]): str(r["title"] or "")
                  for r in _rows(con, "SELECT id,title FROM tasks WHERE id IN (%s)" % _marks(ids), ids)}
        items: list = []
        for row in _rows(con, "SELECT id,task_id,author,body,created_at FROM task_comments "
                              "WHERE task_id IN (%s) ORDER BY created_at, id" % _marks(ids), ids):
            items.append(_comment_item(cfg, room, row, roster))
        for row in _rows(con, "SELECT id,task_id,kind,payload,created_at FROM task_events "
                              "WHERE task_id IN (%s) AND kind IN (%s) ORDER BY created_at, id"
                         % (_marks(ids), _marks(list(_EVENT_KINDS))), ids + list(_EVENT_KINDS)):
            item = _event_item(row, titles)
            if item:
                items.append(item)
        items.extend(_versions(con, root))
        for note in notes:      # «приняли уточнение» + итог карточки как сообщение команды
            items.append({"id": "n%s" % note["id"], "at": int(note["created_at"] or 0),
                          "author_kind": "system", "author": "system",
                          "author_label": "Диалог запроса",
                          "text": "Уточнение передано руководителю комнаты.",
                          "task_id": str(note["id"]), "kind": "event"})
            done = [r for r in _run_rows(con, [note["id"]])
                    if str(r["status"] or "") in ("done", "completed")
                    or str(r["outcome"] or "") == "completed"]
            summary = str((done[-1]["summary"] if done else "") or "").strip()
            if summary:
                lead = str(note.get("assignee") or "")
                items.append({"id": "s%s" % done[-1]["id"], "at": int(done[-1]["ended_at"] or 0),
                              "author_kind": "agent", "author": lead,
                              "author_label": author_label(cfg, room, lead, roster),
                              "text": summary, "task_id": str(note["id"]), "kind": "comment"})
    finally:
        con.close()
    items.sort(key=lambda item: (int(item.get("at") or 0), 0 if item.get("kind") != "event" else 1,
                                 str(item.get("id"))))
    return items[: max(1, int(limit))]


# ---------------------------------------------------------------- запись

def _validate_text(text) -> str:
    value = no_nul(text, "text")
    value = value.strip() if isinstance(value, str) else ""
    if not value:
        raise EngineWriteError("bad_request", "Пустое сообщение — напишите текст.",
                              detail={"field": "text"})
    if len(value) > MAX_TEXT:
        raise EngineWriteError("bad_request",
                              "Слишком длинное сообщение: %d знаков, а можно не больше %d."
                              % (len(value), MAX_TEXT),
                              detail={"field": "text", "length": len(value), "limit": MAX_TEXT})
    return value


def _attach_paths(cfg, attach) -> list:
    """Пути вложений: список строк из разрешённых мест (иначе 403), без NUL."""
    if attach in (None, ""):
        return []
    if not isinstance(attach, (list, tuple)):
        raise EngineWriteError("bad_request", "attach: список путей к файлам.",
                              detail={"field": "attach"})
    out = []
    for path in list(attach)[:10]:
        text = no_nul(path, "attach").strip()
        if not text:
            continue
        if not path_allowed(cfg, text):
            raise EngineWriteError("forbidden", "Вложение вне разрешённых путей: %s" % text,
                                  detail={"path": text}, http_status=403)
        out.append(text)
    return out


def _lead(room: dict, row: dict) -> str:
    """Руководитель комнаты; если он не задан — исполнитель корня."""
    lead = str(room.get("orchestrator") or row.get("assignee") or "").strip()
    if not lead:
        raise EngineWriteError("conflict",
                              "У комнаты не назначен руководитель, а у запроса — исполнитель: "
                              "сообщение некому передать.",
                              detail={"reason": "no_lead"})
    return lead


def _team_line(cfg, room: dict) -> str:
    """Кого касается: профили комнаты с ролями по-русски."""
    roster, seen, parts = _profiles(cfg), set(), []
    lead = str(room.get("orchestrator") or "")
    for name in [lead] + [m.get("profile") for m in (room.get("members") or []) if isinstance(m, dict)] \
            + [room.get("critic")]:
        name = str(name or "").strip()
        if not name or name in seen:
            continue
        seen.add(name)
        parts.append("%s (%s)" % (name, role_label(name, room)))
    return "; ".join(parts)


def build_note_body(cfg, room: dict, root: str, row: dict, text: str, attach=None,
                    lead: str = "", urgent: bool = False) -> str:
    """Тело карточки «Уточнение от Михаила»: текст + инструкция руководителю (Д1 а–е)."""
    tenant = _tenant(room)
    title = str(row.get("title") or "")
    status = _STATUS_RU.get(str(row.get("status") or ""), str(row.get("status") or ""))
    lines = [
        "СООБЩЕНИЕ МАКСИМА (дословно, передано приложением «Комната команды»):",
        "<<<УТОЧНЕНИЕ>>>", text, "<<<КОНЕЦ УТОЧНЕНИЯ>>>", "",
        "Запрос: %s «%s» (сейчас %s)." % (root, _short(title, 120), status or "неизвестно"),
    ]
    if attach:
        lines.append("Приложенные файлы: " + "; ".join(attach))
    lines += [
        "",
        "Ты — руководитель комнаты «%s» (%s). Сообщение пришло ТЕБЕ; остальные участники "
        "его не получали." % (str(room.get("title") or room.get("id") or "комната"), lead),
        "Что сделать:",
    ]
    if urgent:
        lines.append(URGENT_LINE)
    team = _team_line(cfg, room)
    lines += [
        "а) Перечитай корень и все карточки запроса: kanban_show(task_id=\"%s\") — статусы, тела, "
        "комментарии; дочерние карточки видны в parents/children." % root,
        "б) Реши, кого это касается по компетенции, а кого — нет. Состав комнаты: %s." % (team or "—"),
        "в) Каждому затронутому — комментарий на ЕГО карточку: "
        "kanban_comment(task_id=\"<карточка>\", body=\"[Руководитель → <роль>] <что учесть и что "
        "сделать>\"). Если нужной карточки нет — создай её: kanban_create(title=…, "
        "assignee=<профиль>, parents=[\"%s\"], tenant=\"%s\", body=…), при необходимости свяжи "
        "kanban_link. Переназначить существующую карточку инструментом нельзя — создавай новую."
        % (root, tenant),
        "г) Остальных не трогай, работу не останавливай: kanban_block — только если без ответа "
        "Михаила продолжать нельзя.",
        "д) На корне оставь комментарий: kanban_comment(task_id=\"%s\", body=\"Принял; передал: "
        "<кому и что>\")." % root,
        "е) Заверши СВОЮ карточку: kanban_complete(summary=<короткий свод: что учтено и кому "
        "передано, по-русски>, metadata={\"relayed\":[{\"to\":\"<профиль>\",\"what\":\"…\"}], "
        "\"acknowledged\":[\"…\"]}). В своде обязательно блок «Учтено из уточнений».",
    ]
    if bool(room.get("project_mode")):
        lines += [
            "ж) Проектный режим включён: если сообщение меняет карту проекта, приложи к своей "
            "карточке блок plan_patch и кратко объясни причину — "
            "metadata={\"plan_patch\":{\"add\":[{\"title\":\"…\",\"goal\":\"…\",\"owner\":\"<профиль>\","
            "\"members\":[\"<профиль>\"],\"depends_on\":[\"<id>\"],\"due\":\"YYYY-MM-DD\","
            "\"reason\":\"…\"}], \"change\":[{\"id\":\"s1\",\"fields\":{\"due\":\"YYYY-MM-DD\","
            "\"owner\":\"<профиль>\"}}], \"drop\":[{\"id\":\"s1\",\"reason\":\"почему\"}], "
            "\"note\":\"почему меняешь карту\"}}. Поля направления те же, что в карте: title, goal, "
            "owner, members, depends_on, due, reason (и status). Правки текста, сроков, "
            "исполнителей и статуса приложение применит само; новые направления и перезапуск "
            "сделанного ждут «Запустить» у Михаила. Исполнители — только из состава комнаты.",
        ]
    lines += [
        "",
        "Твой свод и комментарии попадут в диалог запроса автоматически. Работать можно только "
        "инструментами канбана (%s) и своими обычными навыками Hermes." % ", ".join(LEAD_TOOLS),
    ]
    return "\n".join(lines)


def _comment(cfg, board: str, task_id: str, text: str, author: str = "mikhail") -> int:
    """Комментарий CLI: id идёт позиционально, текст — после `--` (без инъекций опций)."""
    _out, ms = _run(cfg, board, ["comment", task_id, opt("author", author), "--", text])
    return int(ms or 0)


def _create_note(cfg, room: dict, board: str, root: str, row: dict, text: str, attach,
                  lead: str, urgent: bool, body: str | None = None,
                  key_seed: str = "") -> str:
    body = body or build_note_body(cfg, room, root, row, text, attach, lead, urgent)
    key = NOTE_KEY_PREFIX + root + "-" + hashlib.sha1(
        (text + "|" + str(int(urgent)) + "|" + key_seed).encode("utf-8")).hexdigest()[:10]
    path = _write_body(cfg, body, "note-" + root)
    try:
        args = ["create", NOTE_TITLE, opt("assignee", lead), opt("tenant", _tenant(room)),
                opt("created-by", CREATED_BY), opt("idempotency-key", key),
                opt("body-file", path), "--json"]
        data, _ms = _run_json(cfg, board, args)
    finally:
        try:
            path.unlink()          # временное тело не оставляем
        except OSError:
            pass
    card_id = str((data or {}).get("id") or "")
    if not card_id:
        raise EngineWriteError("engine_error", "Канбан не вернул id карточки «Уточнение».",
                              json.dumps(data, ensure_ascii=False)[:400])
    return card_id


def _revise(cfg, room: dict, board: str, root: str, text: str) -> dict:
    """«На доработку» готового запроса (существующий revise → версия N+1)."""
    try:
        from . import critic_loop as loop
        from . import engine_read as reader
        from . import registry as reg
    except ImportError:  # pragma: no cover
        return {"ok": False, "warning": "Модуль доработки недоступен — отправьте «На доработку» вручную."}
    try:
        record = reg.get(cfg, str(room.get("id") or ""), root) or {}
        detail = reader.request_detail(cfg, board, root, room, record.get("extra_cards") or [],
                                       record.get("critic"))
        res = loop.manual_revision(cfg, room, record, detail, text) or {}
    except EngineWriteError as exc:
        return {"ok": False, "code": exc.code, "message": str(exc)}
    except Exception as exc:  # noqa: BLE001 - сообщение уже передано, доработка вторична
        return {"ok": False, "warning": "Доработку не удалось запустить: %s" % exc}
    return {"ok": True, "task_id": res.get("task_id"), "version": res.get("version")}


def _failed(row: dict) -> bool:
    """Запрос упал: явная ошибка карточки при незавершённом статусе."""
    status = str(row.get("status") or "")
    if status in ("done", "archived", "running"):
        return False
    return bool(row.get("last_failure_error")) or int(row.get("consecutive_failures") or 0) > 0


def _retry(cfg, board: str, root: str) -> dict:
    """«Повторить с уточнением»: существующий retry (promote/unblock/reopen-review)."""
    try:
        from . import engine_write as ew
    except ImportError:  # pragma: no cover
        return {"ok": False, "warning": "Модуль записи недоступен — повторите запрос вручную."}
    try:
        res = ew.act(cfg, board, root, "retry", task_id=root)
    except EngineWriteError as exc:
        if exc.code == "conflict":
            return {"ok": False, "code": "conflict", "message": str(exc)}
        raise
    return {"ok": True, "detail": res.get("detail"), "task_id": res.get("task_id")}


def _last_comment_text(cfg, board: str, root: str) -> dict:
    """Комментарий, который только что записали на корень (для ``message``)."""
    con = _connect(cfg, board)
    if con is None:
        return {}
    try:
        rows = _rows(con, "SELECT id,task_id,author,body,created_at FROM task_comments "
                          "WHERE task_id = ? ORDER BY id DESC LIMIT 1", (root,))
    finally:
        con.close()
    if not rows or NOTE_MARK not in str(rows[0]["body"] or ""):
        return {}
    row = rows[0]
    return {"id": "c%d" % int(row["id"]), "at": int(row["created_at"] or 0),
            "author_kind": "mikhail", "author": "mikhail", "author_label": "Михаил",
            "text": str(row["body"] or ""), "task_id": root, "kind": "message"}


def post_message(cfg, room, rid, text, attach=None, urgent: bool = False) -> dict:
    """Сообщение Михаила руководителю комнаты (Д1).

    Возвращает ``{message, thread, routed_to, merged}`` + диагностику ``created``:
    комментарий на корень всегда, карточка «Уточнение от Михаила» — если открытой нет
    (иначе новый текст дописывается комментарием к ней). Готовый запрос дополнительно
    уходит «На доработку», упавший — повторяется с уточнением.
    """
    root = assert_task_id(rid, "rid")
    room = _room(cfg, room)
    if not room:
        raise EngineWriteError("not_found", "Комната запроса не найдена.",
                              detail={"reason": "room_not_found"}, http_status=404)
    board = _board_of(room)
    value = _validate_text(text)
    row = card_row(cfg, board, root)
    if not row:
        raise EngineWriteError("not_found", "Запрос не найден на доске комнаты.",
                              detail={"reason": "no_card", "rid": root}, http_status=404)
    attach = _attach_paths(cfg, attach)
    lead = _lead(room, row)
    comment = NOTE_MARK + " " + value
    if attach:
        comment += "\n\nПриложенные файлы:\n" + "\n".join("- " + path for path in attach)
    _comment(cfg, board, root, comment)                      # 1) сразу видно в диалоге
    note = _open_note(cfg, board, root, _tenant(room))       # 2) пакетирование
    if note:                                                 #    открытая — дописываем
        card_id = assert_task_id(str(note["id"]), "task_id")
        _comment(cfg, board, card_id, comment)
        merged = True
    else:                                                    #    иначе — новая карточка
        card_id = _create_note(cfg, room, board, root, row, value, attach, lead, urgent)
        merged = False
    result = {"routed_to": lead, "merged": merged, "task_id": card_id,
              "status": str(row.get("status") or "")}
    if result["status"] == "done":                            # 3) готово → на доработку
        result["revision"] = _revise(cfg, room, board, root, value)
    elif _failed(row):                                       #    упало → повтор
        result["retry"] = _retry(cfg, board, root)
    thread = get_thread(cfg, root, room=room, board=board)
    message = _last_comment_text(cfg, board, root) or {
        "id": "c0", "at": int(time.time()), "author_kind": "mikhail", "author": "mikhail",
        "author_label": "Михаил", "text": comment, "task_id": root, "kind": "message"}
    result["message"], result["thread"] = message, thread
    _sync_vault(cfg, room, root)
    return result


def build_answer_body(cfg, room: dict, root: str, row: dict, question: dict, text: str,
                      lead: str, need_explanation: bool = False) -> str:
    """Тело карточки «Уточнение» для ОТВЕТА НА ВОПРОС (W14-D §1–§3).

    Отличие от обычного уточнения: руководитель обязан ответить короткой реакцией
    в ветке (её видит Михаил у вопроса) и учесть ответ в работе; встречный вопрос
    ответом не считается.
    """
    qid = str((question or {}).get("id") or "").strip() or "q_0"
    qtext = _short((question or {}).get("text") or "", 200)
    title = str(row.get("title") or "")
    status = _STATUS_RU.get(str(row.get("status") or ""), str(row.get("status") or ""))
    marker, caption = (("<<<ПРОСЬБА ПОЯСНИТЬ>>>", "МАКСИМ ПРОСИТ ПОЯСНЕНИЕ (это НЕ ответ)")
                       if need_explanation else
                       ("<<<ОТВЕТ>>>", "ОТВЕТ МАКСИМА НА ВОПРОС (отвечено именно на этот вопрос)"))
    lines = [
        "СООБЩЕНИЕ МАКСИМА (дословно, передано приложением «Комната команды»):",
        caption + ":",
        "Вопрос: %s «%s»" % (qid, qtext),
        marker, text, "<<<КОНЕЦ>>>", "",
        "Запрос: %s «%s» (сейчас %s)." % (root, _short(title, 120), status or "неизвестно"),
        "",
        "Ты — руководитель комнаты «%s» (%s). Ответ пришёл ТЕБЕ; остальные участники "
        "его не получали." % (str(room.get("title") or room.get("id") or "комната"), lead),
    ]
    if need_explanation:
        lines += [
            "Что сделать (по шагам):",
            "а) Вопрос %s ОСТАЁТСЯ ОТКРЫТЫМ: Михаил просит пояснить, а не ответил. Не считай "
            "его решённым и не выбирай за него." % qid,
            "б) Ответь ПОЯСНЕНИЕМ в ветке простым русским языком, 2–4 предложения, без жаргона: "
            "kanban_comment(task_id=\"%s\", body=\"[Руководитель · пояснение по вопросу %s] …\") — "
            "что это такое, зачем нужно и какие есть варианты выбора." % (root, qid),
            "в) После пояснения снова жди решения Михаила; остальную работу не останавливай.",
            "г) Заверши свою карточку: kanban_complete(summary=<что пояснил и по-русски>, "
            "metadata={\"explained\":[\"%s\"]})." % qid,
        ]
    else:
        lines += [
            "Что сделать (по шагам):",
            "а) Перечитай корень и все карточки запроса: kanban_show(task_id=\"%s\") — статусы, "
            "тела, комментарии." % root,
            "б) СРАЗУ ответь короткой РЕАКЦИЕЙ на корне — её Михаил видит у вопроса "
            "(один из трёх вариантов):",
            "   • принял, ничего не меняем: kanban_comment(task_id=\"%s\", "
            "body=\"%s %s] Принято: <что именно>; ничего менять не нужно\")"
            % (root, answers.REACTION_MARK, qid),
            "   • принял, работа пересматривается: kanban_comment(task_id=\"%s\", "
            "body=\"%s %s] Работа пересмотрена: карточки t_… — <что переделать>\")"
            % (root, answers.REACTION_MARK, qid),
            "   • принял и уже учёл: «%s %s] Принято: <что>; учтено в карточках t_…»"
            % (answers.REACTION_MARK, qid),
            "в) Учти ответ в работе: комментарии на карточки участников, при необходимости — "
            "новые карточки (kanban_create) и доработка уже сделанного. Если ответ меняет "
            "готовое — поставь доработку сразу, это должно быть видно в карточках.",
            "г) СНАЧАЛА сверься с ответом, потом создавай шаги: шаг, противоречащий ответу "
            "(по этому вопросу), создавать нельзя; при ответе «нет/не нужно» связанные шаги "
            "снимаются приложением автоматически.",
            "д) В своде укажи блок «Учтено из ответов»: вопрос → что сделано.",
            "е) Заверши СВОЮ карточку с ЯВНЫМ исходом реакции — по нему приложение "
            "показывает «ответ отправлен → руководитель прочитал → учтено» "
            "(не угадывает по словам):",
            "   kanban_complete(summary=<короткий свод по-русски>, metadata={\"reaction\": "
            "{\"question_id\":\"%s\", \"outcome\":\"read|applied|no_change\", "
            "\"cards\":[\"t_…\"], \"changes\":<сколько карточек изменено>}, "
            "\"acknowledged\":[\"…\"]})." % qid,
            "   outcome: read — прочитал; applied — учтено, карточки изменены; "
            "no_change — учтено, менять ничего не нужно.",
        ]
    if bool(room.get("project_mode")):
        lines += [
            "ж) Проектный режим: если ответ меняет карту проекта, приложи блок plan_patch "
            "(add/change/drop) и кратко объясни причину; у нового направления укажи "
            "\"from_question\":\"%s\"." % qid,
        ]
    lines += [
        "",
        "Твой комментарий-реакция и свод попадут в диалог запроса автоматически. Работать "
        "можно только инструментами канбана (%s) и своими обычными навыками Hermes."
        % ", ".join(LEAD_TOOLS),
    ]
    return "\n".join(lines)


def post_answer_note(cfg, room, rid, question: dict, text: str,
                     need_explanation: bool = False) -> dict:
    """Передать ответ руководителю карточкой «Уточнение» с контрактом реакции.

    Комментарий в ветке пишет вызывающий (``engine_write.act("answer")`` — он же
    разблокирует карточки); здесь только рабочая карточка руководителю: одна на
    запрос, повторный ответ дописывается комментарием (``merged``).
    """
    root = assert_task_id(rid, "rid")
    room = _room(cfg, room)
    if not room:
        raise EngineWriteError("not_found", "Комната запроса не найдена.",
                              detail={"reason": "room_not_found"}, http_status=404)
    board = _board_of(room)
    row = card_row(cfg, board, root)
    if not row:
        raise EngineWriteError("not_found", "Запрос не найден на доске комнаты.",
                              detail={"reason": "no_card", "rid": root}, http_status=404)
    lead = _lead(room, row)
    value = _validate_text(text)
    qid = str((question or {}).get("id") or "").strip() or "q_0"
    mark = (answers.explanation_comment(qid, (question or {}).get("text") or "", value)
            if need_explanation else answers.answer_comment(qid, (question or {}).get("text") or "", value))
    note = _open_note(cfg, board, root, _tenant(room))
    if note:
        card_id = assert_task_id(str(note["id"]), "task_id")
        _comment(cfg, board, card_id, mark)
        merged = True
    else:
        body = build_answer_body(cfg, room, root, row, question or {}, value, lead, need_explanation)
        card_id = _create_note(cfg, room, board, root, row, value, None, lead, False,
                               body=body, key_seed=qid)
        merged = False
    return {"task_id": card_id, "merged": merged, "routed_to": lead, "question_id": qid}


def build_decision_body(cfg, room: dict, root: str, row: dict, decision: dict, outcome: str,
                        text: str, lead: str) -> str:
    """Тело карточки «Уточнение» для решения, которое ждало слова Михаила (W19-D2, B-20).

    Руководитель получает ровно то, что нужно: какое решение, что с ним сделал
    Михаил и как отметить исход. Исход — ``metadata.reaction.outcome`` (B-21), а не
    догадка по словам ответа.
    """
    did = str(decision.get("id") or "d_0")
    what = {"accept": "принял это решение", "reject": "отклонил это решение",
            "discuss": "просит обсудить это решение"}.get(str(outcome), "ответил по решению")
    title = str(row.get("title") or "")
    status = _STATUS_RU.get(str(row.get("status") or ""), str(row.get("status") or ""))
    lines = [
        "СООБЩЕНИЕ МАКСИМА (передано приложением «Комната команды»):",
        "ЕГО РЕШЕНИЕ ПО ВАШЕМУ ПРЕДЛОЖЕНИЮ:",
        "Решение: %s «%s»" % (did, _short(decision.get("text") or "", 200)),
    ]
    if decision.get("why"):
        lines.append("Почему предлагалось: %s" % _short(decision.get("why") or "", 300))
    lines += ["Что сделал Михаил: %s." % what]
    if text:
        lines += ["<<<ОТВЕТ>>>", text, "<<<КОНЕЦ>>>"]
    lines += [
        "",
        "Запрос: %s «%s» (сейчас %s)." % (root, _short(title, 120), status or "неизвестно"),
        "",
        "Ты — руководитель комнаты «%s» (%s). Это решение касается ТОЛЬКО этого "
        "запроса; остальные участники сообщение не получали."
        % (str(room.get("title") or room.get("id") or "комната"), lead),
        "Что сделать (по шагам):",
        "а) Учти решение в работе: перечитай корень и карточки запроса "
        "(kanban_show(task_id=\"%s\")) и при необходимости поставь доработку." % root,
    ]
    if str(outcome) == "discuss":
        lines += [
            "б) Михаил просит ОБСУДИТЬ: ответь в ветке простым русским языком, 2–4 "
            "предложения — что предлагаешь и почему; само решение остаётся за ним.",
            "в) Ответ: kanban_comment(task_id=\"%s\", body=\"%s %s] …\") — коротко, по делу."
            % (root, answers.DECISION_REACTION_MARK, did),
        ]
    else:
        lines += [
            "б) Ответь короткой РЕАКЦИЕЙ на решение в ветке — её Михаил видит у решения: "
            "kanban_comment(task_id=\"%s\", body=\"%s %s] Учтено: изменено 2 карточки — "
            "t_…, t_…\") или «… Учтено: менять ничего не нужно»."
            % (root, answers.DECISION_REACTION_MARK, did),
        ]
    lines += [
        "в) Заверши СВОЮ карточку с явным исходом (по нему приложение показывает "
        "статус — «ответ отправлен → руководитель прочитал → учтено»):",
        "   kanban_complete(summary=<что сделал, по-русски>, metadata={\"reaction\": "
        "{\"decision_id\": \"%s\", \"outcome\": \"read|applied|no_change\", "
        "\"cards\": [\"t_…\"], \"changes\": <сколько карточек изменено>}})." % did,
        "   outcome: read — прочитал; applied — учтено, карточки изменены; "
        "no_change — учтено, менять ничего не нужно.",
        "",
        "Работать можно только инструментами канбана (%s) и своими обычными навыками Hermes."
        % ", ".join(LEAD_TOOLS),
    ]
    return "\n".join(lines)


def post_decision_note(cfg, room, rid, decision: dict, outcome: str, text: str = "") -> dict:
    """Передать решение руководителю карточкой «Уточнение» (W19-D2, B-20).

    Одна карточка на запрос: если незавершённая карточка «Уточнение» уже есть,
    сообщение дописывается в неё комментарием (``merged``), как у ответов на вопросы.
    """
    root = assert_task_id(rid, "rid")
    room = _room(cfg, room)
    if not room:
        raise EngineWriteError("not_found", "Комната запроса не найдена.",
                               detail={"reason": "room_not_found"}, http_status=404)
    board = _board_of(room)
    row = card_row(cfg, board, root)
    if not row:
        raise EngineWriteError("not_found", "Запрос не найден на доске комнаты.",
                               detail={"reason": "no_card", "rid": root}, http_status=404)
    lead = _lead(room, row)
    did = str((decision or {}).get("id") or "").strip() or "d_0"
    mark = answers.decision_comment(outcome, did, (decision or {}).get("text") or "", text)
    note = _open_note(cfg, board, root, _tenant(room))
    if note:
        card_id = assert_task_id(str(note["id"]), "task_id")
        _comment(cfg, board, card_id, mark)
        merged = True
    else:
        body = build_decision_body(cfg, room, root, row, decision or {}, outcome, text, lead)
        card_id = _create_note(cfg, room, board, root, row, text or mark, None, lead, False,
                               body=body, key_seed=did)
        merged = False
    return {"task_id": card_id, "merged": merged, "routed_to": lead, "decision_id": did}


def build_answers_digest_body(cfg, room: dict, root: str, row: dict, items: list,
                              lead: str) -> str:
    """Тело карточки «Уточнение» для НАКОПЛЕННЫХ ответов (W17-B §3, аудит B6).

    Пять ответов Михаила висели без реакции руководителя 6+ часов: они пришли до
    контракта 1.0.2. Автоматически такие ответы не отправляются (это запускает
    настоящего агента) — только по кнопке. Здесь руководитель получает ОДНУ карточку
    со всеми ответами и требованием отреагировать на каждый.
    """
    title = str(row.get("title") or "")
    status = _STATUS_RU.get(str(row.get("status") or ""), str(row.get("status") or ""))
    rows = [item for item in items or [] if isinstance(item, dict)]
    lines = [
        "СООБЩЕНИЕ МАКСИМА (передано приложением «Комната команды» по его кнопке):",
        "НАКОПЛЕННЫЕ ОТВЕТЫ БЕЗ ВАШЕЙ РЕАКЦИИ — %d:" % len(rows),
        "<<<ОТВЕТЫ>>>",
    ]
    for number, item in enumerate(rows, start=1):
        lines.append("%d) вопрос %s «%s»" % (number, str(item.get("question_id") or "q_0"),
                                            _short(item.get("question_text") or "", 200)))
        lines.append("   ответ: %s" % str(item.get("answer") or "")[:1200])
    lines += [
        "<<<КОНЕЦ>>>", "",
        "Запрос: %s «%s» (сейчас %s)." % (root, _short(title, 120), status or "неизвестно"),
        "",
        "Ты — руководитель комнаты «%s» (%s). Эти ответы уже ЕСТЬ в работе — Михаил их "
        "дал, но реакции он не видел." % (str(room.get("title") or room.get("id") or "комната"), lead),
        "", "Что сделать (по шагам):",
        "а) Перечитай корень и карточки запроса: kanban_show(task_id=\"%s\")." % root,
        "б) На КАЖДЫЙ пункт выше ответь короткой реакцией на корне — её Михаил видит "
        "у вопроса: kanban_comment(task_id=\"%s\", body=\"%s q_x] Принято: …\") — "
        "«Ничего менять не нужно» или «Работа пересмотрена: карточки t_…»."
        % (root, answers.REACTION_MARK),
        "в) Если ответ меняет уже сделанное — поставь доработку сразу; если отменяет шаг — "
        "шаг должен быть снят (приложение снимает его само при ответе «нет»).",
        "г) В своде добавь блок «Учтено из ответов»: вопрос → что сделано.",
        "д) Заверши свою карточку: kanban_complete(summary=<короткий свод по-русски>, "
        "metadata={\"acknowledged\":[\"q_…\"]}).",
        "",
        "Работать можно только инструментами канбана (%s) и своими обычными навыками Hermes."
        % ", ".join(LEAD_TOOLS),
    ]
    return "\n".join(lines)


def post_answers_digest(cfg, room, rid, items) -> dict:
    """Одна карточка руководителю со всеми накопленными ответами (W17-B §3).

    Вызывается ТОЛЬКО действием ``answers_resend`` по клику пользователя: любая
    автопересылка запускает настоящего агента-руководителя. Комментарий в ветке
    (``[Михаил · накопленные ответы] …``) пишется здесь же, карточка «Уточнение» —
    одна на запрос: открытая дописывается, иначе создаётся новая.
    """
    root = assert_task_id(rid, "rid")
    room = _room(cfg, room)
    if not room:
        raise EngineWriteError("not_found", "Комната запроса не найдена.",
                              detail={"reason": "room_not_found"}, http_status=404)
    rows = [item for item in items or [] if isinstance(item, dict) and item.get("question_id")]
    if not rows:
        raise EngineWriteError("bad_request", "Нечего отправлять: ответов без реакции нет.",
                              detail={"reason": "nothing_to_resend"}, http_status=409)
    board = _board_of(room)
    row = card_row(cfg, board, root)
    if not row:
        raise EngineWriteError("not_found", "Запрос не найден на доске комнаты.",
                              detail={"reason": "no_card", "rid": root}, http_status=404)
    lead = _lead(room, row)
    text = answers.digest_comment(rows)
    _comment(cfg, board, root, text)
    note = _open_note(cfg, board, root, _tenant(room))
    if note:
        card_id = assert_task_id(str(note["id"]), "task_id")
        _comment(cfg, board, card_id, text)
        merged = True
    else:
        body = build_answers_digest_body(cfg, room, root, row, rows, lead)
        card_id = _create_note(cfg, room, board, root, row, text, None, lead, False,
                               body=body, key_seed="digest-" + str(len(rows)))
        merged = False
    return {"task_id": card_id, "merged": merged, "routed_to": lead, "text": text,
            "count": len(rows), "at": int(time.time())}


def remind_lead(cfg, room, rid, question: dict, ago_min: int = 0) -> dict:
    """«Напомнить руководителю»: короткий сигнал в ветке, что ждём реакцию на ответ.

    Ответ уже записан, реакции нет — Михаил напоминает. Комментарий идёт и на
    корень, и в открытую карточку «Уточнение» (если она есть), чтобы руководитель
    увидел его при следующем проходе.
    """
    root = assert_task_id(rid, "rid")
    room = _room(cfg, room)
    if not room:
        raise EngineWriteError("not_found", "Комната запроса не найдена.",
                              detail={"reason": "room_not_found"}, http_status=404)
    board = _board_of(room)
    row = card_row(cfg, board, root)
    if not row:
        raise EngineWriteError("not_found", "Запрос не найден на доске комнаты.",
                              detail={"reason": "no_card", "rid": root}, http_status=404)
    lead = _lead(room, row)
    qid = str((question or {}).get("id") or "").strip() or "q_0"
    text = answers.remind_text(qid, (question or {}).get("text") or "", int(ago_min or 0))
    _comment(cfg, board, root, text)
    note = _open_note(cfg, board, root, _tenant(room))
    if note:
        _comment(cfg, board, assert_task_id(str(note["id"]), "task_id"), text)
    return {"task_id": root, "routed_to": lead, "text": text, "question_id": qid}


def _sync_vault(cfg, room: dict, root: str) -> None:
    """Зеркало Obsidian (модуль соседа может быть не готов — молча пропускаем)."""
    try:
        from . import vault_mirror as mirror
    except ImportError:
        return
    try:
        sync = getattr(mirror, "sync_request", None)
        if callable(sync):
            sync(cfg, str(room.get("id") or ""), root)
    except Exception:  # noqa: BLE001 - заметка вторична, сообщение уже записано
        pass


__all__ = ["NOTE_MARK", "LEAD_MARK", "NOTE_TITLE", "URGENT_LINE", "LEAD_TOOLS", "ROLE_RU",
           "get_thread", "post_message", "build_note_body", "author_label", "role_label",
           "card_row", "NOTE_KEY_PREFIX",
           # W14-D: ответ на вопрос — своя метка, свой бриф, своя карточка руководителю
           # W17-B §3: накопленные ответы — одна карточка по кнопке
           "build_answer_body", "post_answer_note", "remind_lead",
           "build_answers_digest_body", "post_answers_digest"]
