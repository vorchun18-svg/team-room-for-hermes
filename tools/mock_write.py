# -*- coding: utf-8 -*-
"""POST/PATCH/DELETE-обработчики мок-сервера: живые запросы, действия, комнаты, бэклог."""
from __future__ import annotations

import copy
import os
import re
from urllib.parse import unquote

from mock_core import (MAX_NOTE, MAX_TEXT, ROOT, ApiError, backlog_counts, compute_needs_you,
                       detail_to_summary, now, slugify)
from mock_versions import normalize_v2
import mock_backlog
import mock_notes
import mock_paths
import mock_plan
import mock_stt

# W14-D: тот же модуль, что у бэкенда — статусы ответа и связка вопрос↔шаг
import sys as _sys
from pathlib import Path as _Path

_SERVER_DIR = _Path(__file__).resolve().parent.parent / "server"
if str(_SERVER_DIR) not in _sys.path:
    _sys.path.insert(0, str(_SERVER_DIR))
import answers as mock_answers            # noqa: E402  (server/answers.py, чистые функции)


def answers_mod_apply(question: dict, record: dict) -> dict:
    """Наложить запись ответа на вопрос демо-данных (как бэкенд, без реестра).

    Мутирует переданный вопрос и возвращает его же — вызывающий код ждёт изменения
    на месте (в отличие от бэкенда, где вопрос копируется под запись).
    """
    return mock_answers.apply_question(question if isinstance(question, dict) else {}, record)


def apply_step_overrides(detail: dict, rid: str, overrides: dict) -> dict:
    """Наложить запомненные действия пользователя на шаги собранной детали.

    Шаги демо-запроса пересобираются на каждый GET (`live_detail`) и фикстуры
    отдаются копией, поэтому без этого действия «Согласен»/«Убрать» пропадали бы
    после первого же обновления — в бэкенде они лежат в реестре (W14-H §12).
    """
    if not isinstance(detail, dict) or not isinstance(overrides, dict):
        return detail
    steps = detail.get("next_steps")
    if not isinstance(steps, list):
        return detail
    for step in steps:
        if not isinstance(step, dict):
            continue
        saved = overrides.get(str(step.get("id") or ""))
        if not isinstance(saved, dict):
            continue
        if saved.get("drop"):
            step.update({"state": "dismissed", "state_label": "убран", "can_remove": False,
                         "can_agree": False, "can_drop": False, "reaction_pending": False,
                         "reaction_label": "шаг убран"})
        elif isinstance(saved.get("action"), dict):
            mock_answers.apply_step(step, saved["action"], None)
    return detail


def enrich_answers(detail: dict) -> dict:
    """W14-D: добавить к вопросам и шагам демо-запроса поля ответов и меток.

    Демо-мок отдаёт фикстуры как есть; здесь они догоняются до формы API, чтобы
    интерфейс работал одинаково в обоих режимах (фикстуры и живой запрос).
    """
    if not isinstance(detail, dict):
        return detail
    questions = detail.get("open_questions")
    if isinstance(questions, list):
        for question in questions:
            if isinstance(question, dict) and "answer_state" not in question:
                record = ({"text": question.get("answer"), "at": question.get("answered_at")}
                          if question.get("answered") and question.get("answer") else {})
                mock_answers.apply_question(question, record)
    steps = detail.get("next_steps")
    if isinstance(steps, list):
        for step in steps:
            if not isinstance(step, dict) or "state_label" in step:
                continue
            step.setdefault("from_question", None)
            step.setdefault("question", None)
            step.setdefault("question_link", None)
            step.setdefault("answer", None)
            step.setdefault("cancelled_by_answer", False)
            step.setdefault("cancelled_reason", None)
            step.setdefault("conflicts_with_answer", False)
            step.setdefault("can_remove", True)
            # W14-H §12: у шага нет галочки — есть действия и реакция руководителя
            step.setdefault("can_agree", True)
            step.setdefault("can_drop", True)
            step.setdefault("user_action", None)
            step.setdefault("user_action_label", None)
            step.setdefault("reaction", None)
            step.setdefault("reaction_label", "предстоит")
            step.setdefault("reaction_pending", False)
            step.setdefault("history", [])
            state = str(step.get("state") or "open")
            step["state_label"] = {"done": "сделано", "dismissed": "убран",
                                   "cancelled": mock_answers.CANCELLED_RU}.get(state, "предстоит")
    if "answers" not in detail and isinstance(questions, list):
        detail["answers"] = {
            "items": [mock_answers.answer_item(q) for q in questions
                      if isinstance(q, dict) and q.get("answer_state") not in (None, "", "open")],
            "summary": mock_answers.summary(questions, steps if isinstance(steps, list) else []),
            "pending_label": mock_answers.PENDING_RU,
            "waiting_label": mock_answers.STATE_RU["waiting_explanation"],
            "conflict_label": mock_answers.CONFLICT_RU,
            "cancelled_label": mock_answers.CANCELLED_RU,
        }
    return detail

MODES = ("auto",)
ROLES = ("worker", "critic", "observer")


def type_name(value) -> str:
    return {dict: "объект", list: "список", str: "строка", bool: "bool", int: "число", float: "число",
            type(None): "null"}.get(type(value), type(value).__name__)


def check_str(body: dict, field: str, *, required: bool = False, default=None, maxlen: int | None = None,
              allow_empty: bool = False) -> str | None:
    """Строковое поле тела: тип, длина, пустота (M1)."""
    value = body.get(field, default)
    if value is None:
        if required:
            raise ApiError(400, "bad_request", "Нужно поле «%s»" % field, {"field": field})
        return default
    if not isinstance(value, str):
        raise ApiError(400, "bad_request", "Поле «%s» должно быть строкой" % field,
                       {"field": field, "got": type_name(value)})
    if not allow_empty and not value.strip():
        raise ApiError(400, "bad_request", "Поле «%s» не может быть пустым" % field, {"field": field})
    if maxlen and len(value) > maxlen:
        raise ApiError(400, "bad_request", "Поле «%s» длиннее %d символов" % (field, maxlen),
                       {"field": field, "length": len(value), "max": maxlen})
    return value


def check_bool(body: dict, field: str, *, default=None):
    value = body.get(field, default)
    if value is None:
        return default
    if not isinstance(value, bool):
        raise ApiError(400, "bad_request", "Поле «%s» должно быть true или false" % field,
                       {"field": field, "got": type_name(value)})
    return value


def check_int(body: dict, field: str, *, default=None, lo: int | None = None, hi: int | None = None):
    value = body.get(field, default)
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, int):
        raise ApiError(400, "bad_request", "Поле «%s» должно быть целым числом" % field,
                       {"field": field, "got": type_name(value)})
    if lo is not None and value < lo or hi is not None and value > hi:
        raise ApiError(400, "bad_request", "Поле «%s» вне диапазона %s..%s" % (field, lo, hi),
                       {"field": field, "value": value})
    return value


def check_list(body: dict, field: str, *, default=None, maxlen: int | None = None, of_str: bool = False):
    value = body.get(field, default)
    if value is None:
        return default
    if not isinstance(value, list):
        raise ApiError(400, "bad_request", "Поле «%s» должно быть списком" % field,
                       {"field": field, "got": type_name(value)})
    if maxlen is not None and len(value) > maxlen:
        raise ApiError(400, "bad_request", "Поле «%s»: больше %d элементов" % (field, maxlen),
                       {"field": field, "length": len(value), "max": maxlen})
    if of_str:
        for item in value:
            if not isinstance(item, str):
                raise ApiError(400, "bad_request", "Поле «%s»: элементы должны быть строками" % field,
                               {"field": field, "got": type_name(item)})
    return value


def check_role(body: dict) -> str:
    role = check_str(body, "role", default="worker", maxlen=16)
    if role not in ROLES:
        raise ApiError(400, "bad_request", "role: worker|critic|observer",
                       {"field": "role", "value": role, "allowed": list(ROLES)})
    return role


def check_mode(value, field: str = "mode") -> str:
    """Режим только auto: swarm снят с поддержки (ARCH §12.2)."""
    mode = str(value or "auto")
    if mode == "swarm":
        raise ApiError(400, "bad_request", "Режим swarm не поддерживается: запрос разбирается только автоматически",
                       {"field": field, "mode": mode, "allowed": list(MODES)})
    if mode not in MODES:
        raise ApiError(400, "bad_request", "Допустим только режим auto",
                       {"field": field, "mode": mode, "allowed": list(MODES)})
    return mode


class MockWrite:
    # W14-H §12: действия пользователя по шагам — в памяти демо, как реестр бэкенда.
    def _remember_step(self, rid: str, step_id: str, saved: dict) -> None:
        store = getattr(self, "step_overrides", None)
        if not isinstance(store, dict):
            store = {}
            self.step_overrides = store
        store.setdefault(str(rid), {})[str(step_id)] = dict(saved or {})

    def step_overrides_for(self, rid: str) -> dict:
        store = getattr(self, "step_overrides", None)
        return (store or {}).get(str(rid)) or {}

    # W13-F (§8): итог «Проверить доску сейчас» для демо — вымышленные карточки
    def kick_report(self, preview: bool = False) -> dict:
        ready_card = {"id": "t_demo0001", "title": "Срез 02-Ops: смета упаковки", "assignee": "biz-ops",
                      "status": "ready", "reason_code": "ready", "reason": "готова к запуску",
                      "tenant": "room:cafe"}
        running_card = {"id": "t_demo0002", "title": "Срез 03-CFO: юнит-экономика набора",
                        "assignee": "biz-cfo", "status": "running", "reason_code": "running",
                        "reason": "уже идёт", "tenant": "room:cafe"}
        counts = {"ready": 1, "stuck": 0, "running": 1, "review": 0, "input": 0, "blocked": 1,
                  "deps": 1, "triage": 0, "scheduled": 0, "queued": 0, "done": 5, "total": 9}
        return {"ok": True, "kind": "preview" if preview else "started", "preview": preview,
                "spawned": 0 if preview else 1, "reclaimed": 0, "promoted": 0, "max": 3,
                "started": [] if preview else [{"id": ready_card["id"], "assignee": "biz-ops",
                                                "title": ready_card["title"]}],
                "counts": counts, "cards": [ready_card, running_card], "useful": True,
                "error": None, "at": now()}

    def post(self, path: str, query: dict, body: dict) -> tuple:
        if path == "/api/stt/learn":
            raw = check_str(body, "raw", required=True, maxlen=MAX_NOTE)
            final = check_str(body, "final", default="", maxlen=20000, allow_empty=True)
            apply = check_bool(body, "apply", default=False)
            return 200, mock_stt.learn(raw, final, apply=bool(apply))
        if path == "/api/dispatcher/kick":
            # W13-F (§8): {preview:true} — только состояние доски, ничего не запускаем
            preview = isinstance(body, dict) and body.get("preview") is True
            report = self.kick_report(preview)
            if preview:
                return 200, {"ok": True, "preview": True, "report": report, "at": now()}
            return 200, {"ok": True, "spawned": report["spawned"], "reclaimed": 0, "promoted": 0,
                         "planned": [], "max": report["max"], "report": report,
                         "dispatcher": {"ok": True, "state": "running", "warning": None}, "at": now()}
        if path == "/api/rooms/check-dir":
            return 200, self.check_dir(body)
        m = re.fullmatch(r"/api/rooms/([a-z0-9-]{2,32})/agents/([a-z0-9_.-]{2,40})/notes/entries", path)
        if m:
            return 201, self.post_note_entry(m.group(1), m.group(2), body or {})
        m = re.fullmatch(r"/api/rooms/([a-z0-9-]{2,32})/agents/([a-z0-9_.-]{2,40})/notes/review", path)
        if m:
            return 200, self.review_notes(m.group(1), m.group(2), body or {})
        m = re.fullmatch(r"/api/rooms/([a-z0-9-]{2,32})/notes/journal/([A-Za-z0-9_-]{2,40})/rollback", path)
        if m:
            return 200, self.rollback_note_event(m.group(1), m.group(2), body or {})
        m = re.fullmatch(r"/api/requests/(t_[0-9a-f]{8})/messages", path)
        if m:
            return 201, self.post_message(m.group(1), body)
        if path == "/api/rooms":
            return 201, self.create_room(body)
        m = re.fullmatch(r"/api/rooms/([a-z0-9-]{2,32})/plan/approve", path)
        if m:
            return self.approve(m.group(1), body)
        m = re.fullmatch(r"/api/rooms/([a-z0-9-]{2,32})/plan/rebuild", path)
        if m:
            return self.rebuild(m.group(1))
        m = re.fullmatch(r"/api/rooms/([a-z0-9-]{2,32})/plan/streams", path)
        if m:
            return self.add_stream(m.group(1), body)
        m = re.fullmatch(r"/api/rooms/([a-z0-9-]{2,32})", path)
        if m:
            return 200, self.patch_room(m.group(1), body)
        m = re.fullmatch(r"/api/rooms/([a-z0-9-]{2,32})/archive", path)
        if m:
            room = self.room(m.group(1))
            changed = not room["archived"]
            room["archived"] = True
            for r in self.rooms:
                if r["id"] == room["id"]:
                    r["archived"] = True
            return 200, {"ok": True, "changed": changed, "room": self.room_summary(room["id"])}
        m = re.fullmatch(r"/api/rooms/([a-z0-9-]{2,32})/members", path)
        if m:
            return 201, self.add_member(m.group(1), body)
        m = re.fullmatch(r"/api/rooms/([a-z0-9-]{2,32})/members/([a-z0-9_.-]{2,40})", path)
        if m:
            return 200, self.remove_member(m.group(1), m.group(2))
        m = re.fullmatch(r"/api/rooms/([a-z0-9-]{2,32})/agents/([a-z0-9_.-]{2,40})/restart", path)
        if m:
            return 200, self.restart_agent(m.group(1), m.group(2))     # WAVE10 §19
        m = re.fullmatch(r"/api/rooms/([a-z0-9-]{2,32})/requests", path)
        if m:
            return 201, self.create_request(m.group(1), body)
        m = re.fullmatch(r"/api/requests/(t_[0-9a-f]{8})/actions", path)
        if m:
            return self.action(m.group(1), body)
        m = re.fullmatch(r"/api/rooms/([a-z0-9-]{2,32})/backlog/(.+?)/ask", path)
        if m:
            return 201, self.ask(m.group(1), unquote(m.group(2)), body)
        m = re.fullmatch(r"/api/rooms/([a-z0-9-]{2,32})/backlog/(.+)", path)
        if m:
            return 200, self.set_backlog(m.group(1), unquote(m.group(2)), body)
        m = re.fullmatch(r"/api/rooms/([a-z0-9-]{2,32})/consolidate", path)
        if m:
            return 201, self.consolidate(m.group(1), body)
        if path == "/api/adopt":
            return 200, self.adopt(body)
        if path == "/api/open":
            return 200, self.open_path(body)
        raise ApiError(404, "not_found", "Неизвестный эндпоинт", {"path": path})

    def put(self, path: str, query: dict, body: dict) -> tuple:
        if path == "/api/stt/dictionary":
            return 200, mock_stt.put_dictionary(body)
        m = re.fullmatch(r"/api/rooms/([a-z0-9-]{2,32})/agents/([a-z0-9_.-]{2,40})/notes", path)
        if m:
            return 200, self.put_notes(m.group(1), m.group(2), body)
        m = re.fullmatch(r"/api/rooms/([a-z0-9-]{2,32})/agents/([a-z0-9_.-]{2,40})/model", path)
        if m:
            return 200, self.put_model(m.group(1), m.group(2), body)   # WAVE10 §19

        m = re.fullmatch(r"/api/rooms/([a-z0-9-]{2,32})/agents/([a-z0-9_.-]{2,40})/label", path)
        if m:
            return 200, self.put_label(m.group(1), m.group(2), body)   # WAVE13 §11
        raise ApiError(404, "not_found", "Неизвестный эндпоинт", {"path": path})

    def patch(self, path: str, query: dict, body: dict) -> tuple:
        m = re.fullmatch(r"/api/rooms/([a-z0-9-]{2,32})/agents/([a-z0-9_.-]{2,40})/notes/entries/([A-Za-z0-9_-]{2,40})", path)
        if m:
            return 200, self.patch_note_entry(m.group(1), m.group(2), m.group(3), body or {})
        m = re.fullmatch(r"/api/rooms/([a-z0-9-]{2,32})/notes/journal/([A-Za-z0-9_-]{2,40})/rollback", path)
        if m:
            return 200, self.rollback_note_event(m.group(1), m.group(2), body or {})
        m = re.fullmatch(r"/api/rooms/([a-z0-9-]{2,32})", path)
        if m:
            return 200, self.patch_room(m.group(1), body)
        m = re.fullmatch(r"/api/rooms/([a-z0-9-]{2,32})/plan/streams/([A-Za-z0-9_-]{1,16})", path)
        if m:
            return self.patch_stream(m.group(1), m.group(2), body)
        raise ApiError(404, "not_found", "Неизвестный эндпоинт", {"path": path})

    def delete(self, path: str, query: dict, body: dict) -> tuple:
        m = re.fullmatch(r"/api/rooms/([a-z0-9-]{2,32})/agents/([a-z0-9_.-]{2,40})/notes/entries/([A-Za-z0-9_-]{2,40})", path)
        if m:
            data = dict(body or {})
            raw_force = (query or {}).get("force")
            if isinstance(raw_force, (list, tuple)):
                raw_force = raw_force[0] if raw_force else ""
            if str(raw_force or "").lower() in ("1", "true", "yes"):
                data["force"] = True
            return 200, self.delete_note_entry(m.group(1), m.group(2), m.group(3), data)
        m = re.fullmatch(r"/api/stt/recordings/([a-z0-9][a-z0-9_-]{5,40})", path)
        if m:
            return 200, mock_stt.drop(m.group(1))
        m = re.fullmatch(r"/api/rooms/([a-z0-9-]{2,32})/members/([a-z0-9_.-]{2,40})", path)
        if m:
            return 200, self.remove_member(m.group(1), m.group(2))
        m = re.fullmatch(r"/api/rooms/([a-z0-9-]{2,32})/plan/streams/([A-Za-z0-9_-]{1,16})", path)
        if m:
            return self.drop_stream(m.group(1), m.group(2))
        raise ApiError(404, "not_found", "Неизвестный эндпоинт", {"path": path})

    def put_label(self, room_id: str, profile: str, body: dict) -> dict:
        """`PUT /api/rooms/{id}/agents/{profile}/label` — подпись участника (WAVE13 §11).

        Демо хранит подпись в реестре комнаты в памяти: карточка в «Команде» сразу
        показывает «Имя — должность». Файлы профилей не меняются.
        """
        room = self.room(room_id)
        label = body.get("label")
        if label is not None and not isinstance(label, str):
            raise ApiError(400, "bad_request", "Подпись: нужен текст строкой", {"field": "label"})
        members = room.get("members") or []
        for member in members:
            if str(member.get("profile") or "") != profile:
                continue
            member["label"] = " ".join(str(label or "").split())[:120]
            return {"ok": True, "profile": profile, "label": member["label"], "members": members}
        if profile in (room.get("orchestrator"), room.get("critic")):
            raise ApiError(400, "bad_request",
                           "Руководитель комнаты и критик задаются настройками комнаты",
                           {"profile": profile, "reason": "room_lead"})
        raise ApiError(404, "not_found", "Участника нет в комнате", {"profile": profile})

    def put_notes(self, room_id: str, profile: str, body: dict) -> dict:
        """`PUT /api/rooms/{id}/agents/{profile}/notes` — особые указания участнику (§9)."""
        return mock_notes.MockNotes.put_notes(self, room_id, profile, body)

    def clean_settings(self, body: dict, room: dict) -> dict:
        """Режим запуска, одновременные направления, проектный режим, указания (§3, §9)."""
        out = {}
        if "launch_mode" in body:
            mode = check_str(body, "launch_mode", default="plan_first", maxlen=16)
            if mode not in mock_plan.LAUNCH_MODES:
                raise ApiError(400, "bad_request",
                               "Режим запуска: plan_first (сначала карта) или auto (сразу)",
                               {"field": "launch_mode", "value": mode, "allowed": list(mock_plan.LAUNCH_MODES)})
            out["launch_mode"] = mode
        if "max_parallel_streams" in body:
            out["max_parallel_streams"] = check_int(body, "max_parallel_streams", default=2,
                                                    lo=mock_plan.MAX_PARALLEL[0], hi=mock_plan.MAX_PARALLEL[1])
        if "project_mode" in body:
            out["project_mode"] = check_bool(body, "project_mode", default=False)
        if "agent_notes" in body:
            # проверка идёт после состава: указания задают только участникам комнаты
            out["_agent_notes"] = body.get("agent_notes")
        if "agent_models" in body:
            # модель участника — тоже только из состава (WAVE10 §18)
            out["_agent_models"] = body.get("agent_models")
        return out

    def check_dir(self, body: dict) -> dict:
        """`POST /api/rooms/check-dir` — имитация проверки пути, без обращения к диску."""
        path = body.get("path")
        if path is None:
            raise ApiError(400, "bad_request", "Нужно поле «path» — укажите папку проекта", {"field": "path"})
        if not isinstance(path, str):
            raise ApiError(400, "bad_request", "Поле «path» должно быть строкой",
                           {"field": "path", "got": type_name(path)})
        return mock_paths.check_dir(path, app_dir=ROOT)

    def clean_project_dir(self, body: dict) -> str:
        """Папка проекта комнаты: проверка пути и нормализация (на диск не пишем)."""
        value = check_str(body, "project_dir", maxlen=mock_paths.MAX_PATH, allow_empty=True)
        if not value or not value.strip():
            return ""
        info = mock_paths.check_dir(value, app_dir=ROOT)
        if not info["allowed"]:
            raise ApiError(400, "bad_request", info["message"],
                           {"field": "project_dir", "reason": info["reason"], "path": value})
        return info["normalized"]

    def create_room(self, body: dict) -> dict:
        title = (check_str(body, "title", required=True, maxlen=200) or "").strip()
        rid = check_str(body, "id", maxlen=32) or slugify(title)
        if not re.fullmatch(r"[a-z0-9][a-z0-9-]{1,31}", str(rid)):
            raise ApiError(400, "bad_request", "Недопустимый id комнаты", {"field": "id", "id": rid})
        if any(r["id"] == rid for r in self.rooms):
            raise ApiError(409, "conflict", "Комната с таким id уже есть", {"room": rid})
        roster = {p["profile"] for p in self.fx["profiles.json"]["items"]}
        orchestrator = check_str(body, "orchestrator", required=True, maxlen=64).strip()
        if orchestrator not in roster:
            raise ApiError(400, "bad_request", "Профиля-руководителя комнаты нет в ростере",
                           {"field": "orchestrator", "profile": orchestrator, "available": sorted(roster)})
        critic = check_str(body, "critic", maxlen=64)
        if critic is not None:
            critic = critic.strip() or None
            if critic and critic not in roster:
                raise ApiError(400, "bad_request", "Профиля-критика нет в ростере",
                               {"field": "critic", "profile": critic, "available": sorted(roster)})
        icon = check_str(body, "icon", maxlen=40) or "folder"
        board = check_str(body, "board", maxlen=40) or "team"
        project_dir = self.clean_project_dir(body)
        context_files = check_list(body, "context_files", default=[], of_str=True)
        members = check_list(body, "members", default=[])
        mode = check_mode(check_str(body, "mode_default", default="auto", maxlen=16), "mode_default")
        max_rounds = check_int(body, "max_rounds", default=1, lo=1, hi=2)
        critic_mode = check_str(body, "critic_mode", maxlen=32) or "rounds"
        if critic_mode not in ("rounds", "until_approved"):
            raise ApiError(400, "bad_request", "Режим критика: rounds | until_approved",
                           {"field": "critic_mode", "allowed": ["rounds", "until_approved"]})
        critic_round_cap = check_int(body, "critic_round_cap", default=12, lo=2, hi=30)
        critic_pass = check_bool(body, "critic_pass", default=False)
        settings = self.clean_settings(body, {"orchestrator": orchestrator, "critic": critic,
                                              "members": members, "title": title, "id": rid})
        notes_raw = settings.pop("_agent_notes", None)
        models_raw = settings.pop("_agent_models", None)
        room = {"id": rid, "title": title, "icon": icon, "board": board,
                "tenant": "room:%s" % rid, "orchestrator": orchestrator, "critic": critic,
                "members": [], "context_files": list(context_files),
                "project_dir": project_dir, "mode_default": mode,
                "critic_pass": critic_pass, "max_rounds": max_rounds,
                "critic_mode": critic_mode, "critic_round_cap": critic_round_cap,
                "launch_mode": settings.get("launch_mode", "plan_first"),
                "max_parallel_streams": settings.get("max_parallel_streams", 2),
                "project_mode": settings.get("project_mode", True),
                "created_at": now(), "archived": False, "seeded": False,
                "counters": {"working": 0, "needs_you": 0, "done_today": 0},
                "phase": {"id": "idea", "title": "Идея и стратегия", "status": "active"},
                "dispatcher": {"ok": True, "state": "running", "warning": None}}
        for m in members:
            if not isinstance(m, dict):
                raise ApiError(400, "bad_request", "members: элементы должны быть объектами",
                               {"field": "members", "got": type_name(m)})
            profile = check_str(m, "profile", required=True, maxlen=64).strip()
            if profile not in roster:
                raise ApiError(400, "bad_request", "Профиля нет в ростере",
                               {"field": "members.profile", "profile": profile, "available": sorted(roster)})
            room["members"].append({"profile": profile, "role": check_role(m),
                                    "note": check_str(m, "note", default="", maxlen=300, allow_empty=True),
                                    "pinned": check_bool(m, "pinned", default=False),
                                    "added_at": now()})
        room["agent_notes"] = {}
        room["notes_warnings"] = mock_notes.apply_map(room, notes_raw) if notes_raw is not None else []
        room["agent_models"] = {}
        if models_raw is not None:
            self.apply_agent_models(room, models_raw)     # WAVE10 §18
        self.room_details[rid] = room
        self.rooms.append(room)
        return self.with_models(room)

    def patch_room(self, room_id: str, body: dict) -> dict:
        room = self.room(room_id)
        allowed = {"title", "icon", "orchestrator", "critic", "context_files", "project_dir",
                   "mode_default", "critic_pass", "max_rounds", "launch_mode", "max_parallel_streams",
                   "project_mode", "agent_notes", "agent_models",
                   "critic_mode", "critic_round_cap"}
        bad = sorted(set(body) - allowed)
        if bad:
            raise ApiError(400, "bad_request", "Поля нельзя менять", {"fields": bad, "allowed": sorted(allowed)})
        roster = {p["profile"] for p in self.fx["profiles.json"]["items"]}
        settings = self.clean_settings(body, room)
        notes_raw = settings.pop("_agent_notes", None)
        models_raw = settings.pop("_agent_models", None)
        for key in sorted(allowed & set(body)):
            if key == "mode_default":
                room[key] = check_mode(check_str(body, key, default="auto", maxlen=16), key)
            elif key == "max_rounds":
                room[key] = check_int(body, key, lo=1, hi=2)
            elif key == "critic_mode":
                value = check_str(body, key, default="rounds", maxlen=32) or "rounds"
                if value not in ("rounds", "until_approved"):
                    raise ApiError(400, "bad_request", "Режим критика: rounds | until_approved",
                                   {"field": key, "allowed": ["rounds", "until_approved"]})
                room[key] = value
            elif key == "critic_round_cap":
                room[key] = check_int(body, key, lo=2, hi=30)
            elif key == "critic_pass":
                room[key] = check_bool(body, key, default=False)
            elif key == "context_files":
                room[key] = list(check_list(body, key, default=[], of_str=True))
            elif key == "critic":
                value = check_str(body, key, maxlen=64)
                value = (value or "").strip() or None
                if value and value not in roster:
                    raise ApiError(400, "bad_request", "Профиля-критика нет в ростере",
                                   {"field": key, "profile": value, "available": sorted(roster)})
                room[key] = value
            elif key == "orchestrator":
                value = (check_str(body, key, required=True, maxlen=64) or "").strip()
                if value not in roster:
                    raise ApiError(400, "bad_request", "Профиля-руководителя комнаты нет в ростере",
                                   {"field": key, "profile": value, "available": sorted(roster)})
                room[key] = value
            elif key == "project_dir":
                room[key] = self.clean_project_dir(body)
            elif key in ("launch_mode", "max_parallel_streams", "project_mode"):
                room[key] = settings[key]           # уже проверено в clean_settings
            elif key == "agent_notes":
                room["notes_warnings"] = mock_notes.apply_map(room, notes_raw)
            elif key == "agent_models":
                self.apply_agent_models(room, models_raw)
            else:  # title, icon
                room[key] = check_str(body, key, required=True, maxlen=200).strip()
        for r in self.rooms:
            if r["id"] == room_id:
                for key in ("title", "icon", "orchestrator", "critic", "mode_default", "critic_pass",
                            "max_rounds", "archived", "launch_mode", "max_parallel_streams", "project_mode",
                            "critic_mode", "critic_round_cap"):
                    r[key] = room[key]
        summary = self.room_summary(room_id)
        return {**self.with_models(room), "counters": summary["counters"], "phase": summary["phase"],
                "dispatcher": {"ok": True, "state": "running", "warning": None}}

    def add_member(self, room_id: str, body: dict) -> dict:
        room = self.room(room_id)
        profile = (check_str(body, "profile", required=True, maxlen=64) or "").strip()
        roster = {p["profile"] for p in self.fx["profiles.json"]["items"]}
        if profile not in roster:
            raise ApiError(400, "bad_request", "Профиля нет в ростере", {"profile": profile, "available": sorted(roster)})
        if any(m["profile"] == profile for m in room["members"]) or profile in (room["orchestrator"], room["critic"]):
            raise ApiError(409, "conflict", "Профиль уже в комнате", {"profile": profile})
        member = {"profile": profile, "role": check_role(body),
                  "note": check_str(body, "note", default="", maxlen=300, allow_empty=True),
                  "pinned": check_bool(body, "pinned", default=False), "added_at": now()}
        room["members"].append(member)
        return {"member": self.member_view(room, member),
                "members": [self.member_view(room, m) for m in room["members"]]}

    def remove_member(self, room_id: str, profile: str) -> dict:
        room = self.room(room_id)
        if profile in (room["orchestrator"], room["critic"]):
            raise ApiError(409, "conflict", "Это руководитель комнаты или критик — сначала смените его",
                           {"reason": "room_lead", "profile": profile})
        before = len(room["members"])
        room["members"] = [m for m in room["members"] if m["profile"] != profile]
        if len(room["members"]) == before:
            raise ApiError(404, "not_found", "Участник не найден", {"profile": profile})
        (room.get("agent_models") or {}).pop(profile, None)   # настройка ушедшего не хранится
        return {"ok": True, "members": [self.member_view(room, m) for m in room["members"]]}

    def create_request(self, room_id: str, body: dict) -> dict:
        room = self.room(room_id)
        if room["archived"]:
            raise ApiError(409, "conflict", "Комната в архиве — новые запросы недоступны", {"room": room_id})
        text = (check_str(body, "text", required=True, maxlen=MAX_TEXT, allow_empty=False) or "").strip()
        title = check_str(body, "title", maxlen=120, allow_empty=True)
        check_mode(check_str(body, "mode", default="auto", maxlen=16), "mode")
        critic = check_bool(body, "critic", default=room["critic_pass"])
        critic_mode = check_str(body, "critic_mode", maxlen=32)
        if critic_mode is not None and critic_mode not in ("rounds", "until_approved"):
            raise ApiError(400, "bad_request", "Режим критика: rounds | until_approved",
                           {"field": "critic_mode", "allowed": ["rounds", "until_approved"]})
        req_rounds = body.get("max_rounds")
        if req_rounds is not None and (isinstance(req_rounds, bool)
                                       or not isinstance(req_rounds, int)
                                       or not 1 <= req_rounds <= 5):
            raise ApiError(400, "bad_request", "max_rounds: целое от 1 до 5",
                           {"field": "max_rounds"})
        attach = check_list(body, "attach", default=[], maxlen=10, of_str=True)
        brief = check_bool(body, "brief", default=False)
        detail = self.new_live_request(room_id, text, title, critic,
                                       critic_mode=critic_mode, max_rounds=req_rounds)
        if brief:
            self.mark_brief(detail["id"], room_id)
        summary = detail_to_summary(detail)
        summary["brief"] = bool(brief)
        return {"request": summary, "root_id": summary["id"],
                "dispatcher": {"ok": True, "warning": None}, "warning": None}

    # --- действия над запросом -------------------------------------------

    def _root(self, tree: list) -> dict | None:
        return next((n for n in tree if n.get("role") == "root"), tree[0] if tree else None)

    def _task_id(self) -> str:
        self.counter += 1
        return "t_%08x" % (0x7C000000 + self.counter)

    def _stored_decompose(self, detail: dict, rid: str) -> list:
        ts = now()
        root = self._root(detail.get("tree") or [])
        if root is not None:
            root.update({"status": "running", "started_at": ts, "finished_at": None})
        detail.update({"status": "working", "status_reason": None, "started_at": ts, "needs_you": False})
        detail.setdefault("timeline", []).append({"at": ts, "kind": "decomposed", "task_id": rid, "actor": "system",
                                                  "text": "«Разложить сейчас»: руководитель комнаты разложил запрос"})
        return []

    def _stored_revise(self, detail: dict, rid: str, text: str) -> dict:
        ts = now()
        node = {"id": self._task_id(), "parent_ids": [rid], "title": "Доработка по замечанию Михаила",
                "assignee": detail.get("assignee") or "mikhail", "role": "revision", "status": "running",
                "started_at": ts, "finished_at": None, "summary_md": "", "metadata": {"note": text},
                "comments": [{"author": "mikhail", "text": text, "at": ts}], "runs": [],
                "needs_human": False, "outside_room": False, "error": None}
        detail.setdefault("tree", []).append(node)
        syn = detail["synthesis"]
        version = int(syn.get("version") or 1) + 1
        syn["version"] = version
        syn.setdefault("history", []).append({"version": version, "kind": "revision", "task_id": node["id"],
                                              "author": node["assignee"], "at": ts, "verdict": None, "note": text})
        detail.update({"status": "synthesizing", "status_reason": "Доработка по замечанию Михаила",
                       "needs_you": False})
        if isinstance(detail.get("critic"), dict):
            detail["critic"].update({"state": "revised", "verdict": "revise"})
        detail.setdefault("timeline", []).append({"at": ts, "kind": "created", "task_id": node["id"],
                                                  "actor": "mikhail", "text": "Михаил отправил ответ на доработку"})
        return {"version": version, "previous_version": version - 1, "revision_task_id": node["id"], "note": text}

    def _stored_critique(self, detail: dict, rid: str) -> dict:
        ts = now()
        room = self.room_details.get(detail.get("room")) or {}
        critic = room.get("critic") or "critic"
        node = {"id": self._task_id(), "parent_ids": [rid], "title": "Критик: повторная проверка свода",
                "assignee": critic, "role": "critic", "status": "running", "started_at": ts, "finished_at": None,
                "summary_md": "", "metadata": {}, "comments": [], "runs": [], "needs_human": False,
                "outside_room": False, "error": None}
        detail.setdefault("tree", []).append(node)
        block = detail.setdefault("critic", {})
        block.update({"enabled": True, "round": int(block.get("round") or 0) + 1, "state": "reviewing",
                      "verdict": None, "task_id": node["id"], "issues": []})
        detail.update({"status": "reviewing", "status_reason": "Критик проверяет свод (по запросу Михаила)",
                       "needs_you": False})
        detail.setdefault("timeline", []).append({"at": ts, "kind": "claimed", "task_id": node["id"],
                                                  "actor": critic, "text": "Критик взял свод на повторную проверку"})
        return {"round": block["round"], "critic_task_id": node["id"], "state": "reviewing"}

    def action(self, rid: str, body: dict) -> tuple:
        detail = self.detail(rid)
        action = body.get("action")
        allowed = ["dispatch", "comment", "answer", "unblock", "reassign", "rework", "retry", "archive", "clone",
                   "decompose", "revise", "critique",
                   # W14-D §2/§4: «Напомнить руководителю» и «Убрать» шаг
                   # W14-H §12: действия пользователя по шагу
                   "answer_remind", "step_drop", "step_agree", "step_comment", "step_move"]
        if action not in allowed:
            raise ApiError(400, "bad_request", "Неизвестное действие", {"action": action, "allowed": allowed})
        extra = {"ok": True, "action": action}
        tree = detail.get("tree") or []
        root = self._root(tree)

        if action == "dispatch":
            # W13-F (§8): итог показывает карточку ИМЕННО этого запроса, если она готова
            report = self.kick_report(False)
            ready = next((n for n in tree if isinstance(n, dict)
                          and n.get("status") == "ready" and n.get("assignee")), None)
            if ready:
                report["started"] = [{"id": ready.get("id"), "assignee": ready.get("assignee"),
                                      "title": ready.get("title") or ""}]
                report["counts"] = dict(report["counts"], ready=0)
            extra.update({"spawned": 1, "promoted": 0, "report": report,
                          "dispatcher": {"ok": True, "state": "running", "warning": None}})
        elif action == "comment":
            text = (check_str(body, "text", required=True, maxlen=MAX_NOTE) or "").strip()
            author = (check_str(body, "author", default="mikhail", maxlen=64) or "mikhail").strip()
            self.comment_seq = getattr(self, "comment_seq", 0) + 1
            cm = {"id": self.comment_seq, "author": author, "text": text, "at": now()}   # id: int, как task_comments.id
            if root is not None:
                root.setdefault("comments", []).append(cm)
            detail.setdefault("timeline", []).append({"at": now(), "kind": "commented", "task_id": rid,
                                                      "actor": cm["author"], "text": text[:160]})
            extra["comment"] = cm
        elif action == "answer":
            qid = (check_str(body, "question_id", required=True, maxlen=40) or "").strip()
            text = (check_str(body, "text", required=True, maxlen=MAX_NOTE) or "").strip()
            question = next((q for q in detail.get("open_questions", []) if q["id"] == qid), None)
            if question is None:
                raise ApiError(404, "not_found", "Вопрос не найден", {"question_id": qid})
            if question.get("answered"):
                raise ApiError(409, "conflict", "Вопрос уже отвечен", {"question_id": qid})
            # W14-D §3: встречный вопрос вопрос НЕ закрывает (кнопка надёжнее эвристики)
            need_explanation = bool(body.get("need_explanation")) or mock_answers.is_counter_question(text)
            state = "waiting_explanation" if need_explanation else "sent"
            question.update(answers_mod_apply(question, {
                "text": text, "at": now(), "answered": not need_explanation, "status": state}))
            question["answer_at"] = now()
            unblocked = []
            for node in tree:
                if node.get("status") == "blocked" and node.get("needs_human"):
                    node["status"] = "ready"
                    node["needs_human"] = False
                    unblocked.append(node["id"])
            if unblocked:
                detail["status"] = "working"
            elif detail.get("status") == "needs_you" and not compute_needs_you({**detail, "status": ""}):
                detail["status"] = "working"          # ждали только этот ответ
            detail["status_reason"] = None
            detail["needs_you"] = compute_needs_you(detail)   # остальные открытые вопросы продолжают ждать
            # §4: ответ «нет/не нужно» снимает связанные шаги
            cancelled = []
            if not need_explanation and mock_answers.is_negation(text):
                for step in detail.get("next_steps") or []:
                    linked, _how = mock_answers.link_question(
                        step, [{"id": question["id"], "text": question.get("text") or ""}],
                        mock_answers.ACTION_MIN_SCORE)
                    if linked and str(step.get("state") or "open") not in ("done", "dismissed", "cancelled"):
                        step["state"] = "cancelled"
                        step["state_label"] = mock_answers.CANCELLED_RU
                        step["cancelled_by_answer"] = True
                        step["cancelled_reason"] = mock_answers.CANCELLED_RU
                        step["from_question"] = question["id"]
                        cancelled.append(step.get("id"))
            extra.update({"question": question, "unblocked": unblocked,
                          "answer": {"question_id": qid, "text": text, "at": now(), "state": state,
                                     "label": mock_answers.STATE_RU.get(state),
                                     "needs_explanation": need_explanation},
                          "steps": {"cancelled": cancelled,
                                    "cancelled_label": mock_answers.CANCELLED_RU}})
            # W14-D §1 / W15-C §5: в ленте ответ — ОТДЕЛЬНАЯ запись со ссылкой
            # на вопрос (как в бэкенде), иначе в демо он неотличим от сообщения.
            self.thread_seq = getattr(self, "thread_seq", 0) + 1
            mark = ("[Михаил · %s] %s" % (
                "просит пояснить по вопросу %s «%s»" % (qid, (question.get("text") or "")[:120])
                if need_explanation else
                "ответ на вопрос %s «%s»" % (qid, (question.get("text") or "")[:120]), text))
            item = {"id": "c%d" % self.thread_seq, "at": now(), "author_kind": "mikhail",
                    "author": "mikhail", "author_label": "Михаил", "task_id": None,
                    "kind": "answer", "answer_kind": "explanation_request" if need_explanation else "answer",
                    "question_id": qid, "question_text": question.get("text") or "",
                    "text": text, "text_mark": mark}
            store = getattr(self, "thread_store", None)
            if isinstance(store, dict):
                store.setdefault(rid, []).append(item)
                extra["message"] = item
        elif action == "answer_remind":
            qid = (check_str(body, "question_id", required=True, maxlen=40) or "").strip()
            question = next((q for q in detail.get("open_questions", []) if q["id"] == qid), None)
            if question is None or not question.get("answer"):
                raise ApiError(404, "not_found", "Ответ на этот вопрос не найден", {"question_id": qid})
            if question.get("reaction"):
                raise ApiError(409, "conflict", "Руководитель уже ответил на этот вопрос",
                               {"question_id": qid})
            question["leader_pending"] = True
            question["reminded_at"] = now()
            question["can_remind"] = True
            self.comment_seq = getattr(self, "comment_seq", 0) + 1
            text = mock_answers.remind_text(qid, question.get("text") or "")
            if root is not None:
                root.setdefault("comments", []).append(
                    {"id": self.comment_seq, "author": "mikhail", "text": text, "at": now()})
            extra.update({"question_id": qid, "reminded_at": question["reminded_at"],
                          "reminders": 1, "text": text, "question": question})
        elif action == "step_drop":
            step_id = (check_str(body, "step_id", required=True, maxlen=64) or "").strip()
            step = next((s for s in detail.get("next_steps") or [] if s.get("id") == step_id), None)
            if step is None:
                raise ApiError(404, "not_found", "Шаг не найден", {"step_id": step_id})
            step.update({"state": "dismissed", "state_label": "убран", "can_remove": False,
                         "can_agree": False, "can_drop": False, "reaction_pending": False,
                         "reaction_label": "шаг убран"})
            self._remember_step(rid, step_id, {"drop": True})
            extra.update({"step_id": step_id, "step": step, "dropped": [step_id],
                          "reaction_label": "шаг убран",
                          "message": "[Михаил · убрать шаг %s «%s»]" % (step_id, (step.get("text") or "")[:120])})
        elif action in ("step_agree", "step_comment", "step_move"):
            # W14-H §12: действие по шагу = ответ на ЭТОТ шаг (как ответ на вопрос).
            kind = action[len("step_"):]
            step_id = (check_str(body, "step_id", required=True, maxlen=64) or "").strip()
            if ":" in step_id:
                step_id = step_id.split(":", 1)[1]
            step = next((s for s in detail.get("next_steps") or [] if s.get("id") == step_id), None)
            if step is None:
                raise ApiError(404, "not_found", "Шаг не найден", {"step_id": step_id})
            act = {"kind": kind, "at": now()}
            if kind == "comment":
                act["text"] = (check_str(body, "text", required=True, maxlen=MAX_NOTE) or "").strip()
            if kind == "move":
                if "due" in body:
                    act["due"] = body.get("due")
                if body.get("owner"):
                    act["owner"] = str(body.get("owner")).strip()
                if body.get("direction") in ("up", "down"):
                    act["direction"] = body["direction"]
                if not any(key in act for key in ("due", "owner", "direction")):
                    raise ApiError(400, "bad_request", "Перемещение без изменений",
                                   {"allowed": ["due", "owner", "direction"]})
                order = [s.get("id") for s in detail.get("next_steps") or []]
                if act.get("direction") in ("up", "down") and step_id in order:
                    index = order.index(step_id)
                    swap = index - 1 if act["direction"] == "up" else index + 1
                    if 0 <= swap < len(order):
                        steps_list = detail["next_steps"]
                        steps_list[index], steps_list[swap] = steps_list[swap], steps_list[index]
            mock_answers.apply_step(step, act, None)
            # Демо живёт пересборкой деталей: шаги живого запроса и фикстура
            # собираются заново на каждый GET, поэтому действие надо запомнить,
            # иначе после ответа строка шага остаётся «предстоит» (в бэкенде так
            # не бывает — там действие лежит в реестре).
            self._remember_step(rid, step_id, {"action": act})
            extra.update({"step_id": step_id, "step": step,
                          "reaction_label": step.get("reaction_label"),
                          "pending_label": mock_answers.PENDING_RU,
                          "message": mock_answers.step_comment(
                              kind, step_id, step.get("text") or "", act.get("text") or "",
                              act.get("due"), act.get("owner"), act.get("direction"))})
        elif action == "unblock":
            task_id = check_str(body, "task_id", maxlen=40)
            blocked = [n for n in tree if n["status"] == "blocked" and (not task_id or n["id"] == task_id)]
            if not blocked:
                raise ApiError(409, "conflict", "Нечего разблокировать", {"task_id": task_id})
            for node in blocked:
                node["status"] = "ready"
                node["needs_human"] = False
            detail["status"] = "working"
            detail["needs_you"] = compute_needs_you(detail)
            detail["status_reason"] = None
            extra["unblocked"] = [n["id"] for n in blocked]
        elif action == "reassign":
            task_id = (check_str(body, "task_id", required=True, maxlen=40) or "").strip()
            profile = (check_str(body, "profile", required=True, maxlen=64) or "").strip()
            node = next((n for n in tree if n["id"] == task_id), None)
            if node is None:
                raise ApiError(404, "not_found", "Карточка не найдена", {"task_id": task_id})
            room = self.room(detail["room"])
            roster = {m["profile"] for m in room["members"]} | {room["orchestrator"], room["critic"]} - {None}
            if profile not in roster:
                raise ApiError(400, "bad_request", "Профиль не участвует в комнате",
                               {"profile": profile, "available": sorted(roster)})
            if node["status"] in ("done", "archived"):
                raise ApiError(409, "conflict", "Карточка уже завершена", {"task_id": task_id})
            node["assignee"] = profile
            node["outside_room"] = False
            extra["task"] = node
        elif action == "rework":
            text = (check_str(body, "text", required=True, maxlen=MAX_NOTE) or "").strip()
            task_id = check_str(body, "task_id", maxlen=40)
            node = next((n for n in tree if (n["id"] == task_id if task_id else n.get("role") == "critic")), None)
            if node is None:
                node = root
            if node is None:
                raise ApiError(404, "not_found", "Карточка не найдена", {"task_id": task_id})
            if node["status"] == "archived":
                raise ApiError(409, "conflict", "Карточка в архиве", {"task_id": node["id"]})
            node["status"] = "ready"
            node.setdefault("comments", []).append({"author": "mikhail", "text": text, "at": now()})
            detail["status"] = "working"
            detail["needs_you"] = False
            detail["status_reason"] = None
            extra["task"] = node
        elif action == "retry":
            failed = [n for n in tree if n.get("status") == "blocked" and n.get("error")]
            if not failed:
                raise ApiError(409, "conflict", "Нет упавших карточек", {"rid": rid})
            for node in failed:
                node["status"] = "ready"
                node["error"] = None
            detail["status"] = "working"
            detail["status_reason"] = None
            extra["retried"] = [n["id"] for n in failed]
        elif action == "archive":
            changed = not detail.get("archived")
            detail["archived"] = True
            if rid in self.requests:
                self.requests[rid]["archived"] = True
            extra.update({"archived": [n["id"] for n in tree], "changed": changed})
        elif action == "clone":
            clone = self.new_live_request(detail["room"], detail["text"],
                                          body.get("title") or detail["title"] + " (копия)", detail["critic"])
            s = detail_to_summary(clone)
            return 201, {"ok": True, "action": "clone", "request": s, "root_id": s["id"], "warning": None}
        elif action == "decompose":
            if detail["status"] != "queued":
                raise ApiError(409, "conflict", "Запрос уже разложен — «Разложить сейчас» не нужно",
                               {"reason": "already_decomposed", "status": detail["status"]})
            if rid in self.live:
                self.jump(rid, "working")
                child_ids = [n["id"] for n in self.live_detail(rid)["tree"] if n.get("role") != "root"]
            else:
                child_ids = self._stored_decompose(detail, rid)
            extra.update({"decomposed": True, "child_ids": child_ids,
                          "dispatcher": {"ok": True, "state": "running", "warning": None}})
        elif action == "revise":
            text = (check_str(body, "text", required=True, maxlen=MAX_NOTE) or "").strip()
            if detail["status"] in ("queued", "working"):
                raise ApiError(409, "conflict", "Ответ ещё не готов — доработка недоступна",
                               {"reason": "synthesis_pending", "status": detail["status"]})
            if rid in self.live:
                req = self.live[rid]
                req.setdefault("revision_notes", []).append(text)
                req["extra_versions"] = int(req.get("extra_versions") or 0) + 1
                self.jump(rid, "synthesizing")
                fresh = self.live_detail(rid)
                revisions = [n["id"] for n in fresh["tree"] if n.get("role") == "revision"]
                extra.update({"version": fresh["synthesis"]["version"] + 1,
                              "previous_version": fresh["synthesis"]["version"],
                              "revision_task_id": revisions[-1] if revisions else None, "note": text})
            else:
                extra.update(self._stored_revise(detail, rid, text))
        elif action == "critique":
            room = self.room(detail["room"])
            if not room.get("critic"):
                raise ApiError(409, "conflict", "У комнаты нет критика — назначьте его в настройках комнаты",
                               {"reason": "no_critic", "room": room["id"]})
            if detail["status"] in ("queued", "working"):
                raise ApiError(409, "conflict", "Ответ ещё не готов — критиковать нечего",
                               {"reason": "synthesis_pending", "status": detail["status"]})
            if rid in self.live:
                req = self.live[rid]
                req["critic"] = True
                self.jump(rid, "reviewing")
                block = self.live_detail(rid)["critic"]
                extra.update({"round": block["round"], "critic_task_id": block["task_id"], "state": block["state"]})
            else:
                extra.update(self._stored_critique(detail, rid))
        if rid in self.requests:
            self.requests[rid] = detail
        elif rid in self.live:
            live = self.live[rid]
            if action == "comment":
                live.setdefault("comments", []).append(copy.deepcopy(extra["comment"]))
            elif action == "archive":
                live["archived"] = True
            elif action == "answer":
                question = extra["question"]
                live.setdefault("answers", {})[question["id"]] = {
                    "text": question.get("answer"), "at": question.get("answered_at") or question.get("answer_at"),
                    "answered": bool(question.get("answered")),
                    "status": question.get("answer_state") or "sent"}
            elif action in ("unblock", "rework", "retry"):
                live["force_status"] = "working"
            elif action == "reassign":
                live.setdefault("node_patch", {})[extra["task"]["id"]] = {"assignee": extra["task"]["assignee"]}
        extra["request"] = detail_to_summary(self.detail(rid))
        return 200, extra

    def set_backlog(self, room_id: str, item_id: str, body: dict) -> dict:
        self.room(room_id)
        item = self.upsert_backlog_item(room_id, item_id)   # неизвестный id — из запроса (блок B)
        if item is None:
            raise mock_backlog.missing(item_id)
        data = self.backlog_data(room_id)
        status = body.get("status")
        if status not in ("open", "asked", "done", "dismissed"):
            raise ApiError(400, "bad_request", "status: open|asked|done|dismissed",
                           {"field": "status", "value": status})
        if status == "asked" and item["status"] == "asked" and item.get("asked_request"):
            raise ApiError(409, "conflict", "Уже задано запросом", {"asked_request": item["asked_request"]})
        due = check_str(body, "due", allow_empty=True) if "due" in body else item.get("due")
        if isinstance(due, str) and not due.strip():
            due = None
        if due is not None and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(due)):
            raise ApiError(400, "bad_request", "due: YYYY-MM-DD", {"field": "due"})
        item.update({"status": status, "due": due, "updated_at": now()})
        data["updated_at"] = now()
        return {"item": item, "counts": backlog_counts(data["items"])}

    def ask(self, room_id: str, item_id: str, body: dict) -> dict:
        room = self.room(room_id)
        item = self.upsert_backlog_item(room_id, item_id)
        if item is None:
            raise mock_backlog.missing(item_id)
        if item["status"] == "asked" and item.get("asked_request"):
            raise ApiError(409, "conflict", "Этот вопрос уже задан", {"asked_request": item["asked_request"]})
        text = check_str(body, "text", maxlen=MAX_TEXT, allow_empty=True) if "text" in body else None
        text = (text or item["text"]).strip()
        if not text:
            raise ApiError(400, "bad_request", "Пустой текст запроса", {"field": "text"})
        detail = self.new_live_request(room_id, text, item["text"][:120], room["critic_pass"])
        item.update({"status": "asked", "asked_request": detail["id"], "updated_at": now()})
        self.backlog_data(room_id)["updated_at"] = now()
        return {"request": detail_to_summary(detail), "item": item, "root_id": detail["id"],
                "request_id": detail["id"]}

    def consolidate(self, room_id: str, body: dict) -> dict:
        room = self.room(room_id)
        note = check_str(body, "note", maxlen=2000, allow_empty=True) or ""
        for d in list(self.live.values()) + list(self.requests.values()):
            if d.get("room") == room_id and "STATE.md" in d.get("title", "") and d.get("status") not in ("done", "archived"):
                raise ApiError(409, "conflict", "Свод STATE.md уже идёт", {"existing_root_id": d["id"]})
        text = ("Собери актуальный STATE.md комнаты «%s»: решения, шаги, фазы, открытые вопросы. "
                "Запиши в Vault и обнови зеркало. %s" % (room["title"], note)).strip()
        detail = self.new_live_request(room_id, text, "Обнови STATE.md: %s" % room["title"],
                                       room["critic_pass"])
        return {"request": detail_to_summary(detail), "root_id": detail["id"], "warning": None}

    def adopt(self, body: dict) -> dict:
        room_id = (check_str(body, "room", required=True, maxlen=32) or "").strip()
        task_id = (check_str(body, "task_id", required=True, maxlen=40) or "").strip()
        room = self.room(room_id)
        if not re.fullmatch(r"t_[0-9a-f]{8}", task_id):
            raise ApiError(400, "bad_request", "Некорректный task_id", {"field": "task_id"})
        if self.adopted.get(task_id):
            return {"ok": True, "request": detail_to_summary(self.detail(task_id)),
                    "adopted": True, "already": True, "warning": None}
        candidate = next((c for c in self.candidates.get(room_id, {"items": []})["items"] if c["id"] == task_id), None)
        if candidate is not None:
            if candidate.get("tenant") and candidate["tenant"] != room["tenant"]:
                raise ApiError(409, "conflict", "Карточка уже в другой комнате", {"tenant": candidate["tenant"]})
            detail = normalize_v2(self._candidate_detail(candidate, room_id))
            self.requests[task_id] = detail
            self.adopted[task_id] = True
            self.candidates[room_id]["items"] = [c for c in self.candidates[room_id]["items"] if c["id"] != task_id]
            self.candidates[room_id]["count"] = len(self.candidates[room_id]["items"])
            self.log.append({"at": now(), "action": "adopt", "room": room_id, "task_id": task_id})
            return {"ok": True, "request": detail_to_summary(detail), "adopted": True, "already": False,
                    "warning": {"code": "legacy_metadata",
                                "message": "Карточка создана вне приложения — metadata могут быть пустыми."}}
        detail = self.detail(task_id)
        if detail.get("room") and detail["room"] != room_id:
            raise ApiError(409, "conflict", "Карточка уже в другой комнате",
                           {"tenant": "room:%s" % detail["room"]})
        already = task_id in self.requests      # карточка уже в реестре комнаты
        self.adopted[task_id] = True
        return {"ok": True, "request": detail_to_summary(detail), "adopted": True, "already": already,
                "warning": {"code": "legacy_metadata", "message": "Карточка создана вне приложения — metadata могут быть пустыми."}}

    def _candidate_detail(self, candidate: dict, room_id: str) -> dict:
        """Минимальный RequestDetail для привязанной карточки (мок не знает её дерева)."""
        detail = self.fixture("request_queued.json")
        title = candidate.get("title") or "Привязанная карточка"
        text = candidate.get("preview") or title
        detail.update({"id": candidate["id"], "room": room_id, "title": title, "text": text,
                       "created_at": int(candidate.get("created_at") or now()),
                       "assignee": candidate.get("assignee"), "preview": text[:200]})
        root = (detail.get("tree") or [{}])[0]
        root.update({"id": candidate["id"], "title": title, "assignee": candidate.get("assignee")})
        detail["timeline"] = [{"at": detail["created_at"], "kind": "created", "task_id": candidate["id"],
                               "actor": "system", "text": "Карточка привязана к комнате вручную"}]
        return detail

    def open_path(self, body: dict) -> dict:
        raw = (check_str(body, "path", required=True, maxlen=1000) or "").strip()
        path = self.resolve_shared_path(raw)   # тот же белый список, что у GET /api/file
        return {"ok": True, "path": path, "revealed": False}
