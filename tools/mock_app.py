# -*- coding: utf-8 -*-
"""App мок-сервера: состояние в памяти и сборка ответов.

Живой запрос эволюционирует по расписанию mock_versions: queued → working →
reviewing (критик) → synthesizing (доработка) → done. Без критика шаг reviewing
пропускается. Действия decompose/revise/critique перезапускают фазу через jump().
"""
from __future__ import annotations

import copy
import json
import os
import re
import threading
import time

from mock_core import (FIXTURE_BY_RID, ApiError, child_id, compute_needs_you, counts_as_waiting,
                       detail_to_summary, local_midnight, now)
from mock_models import MockModels
from mock_plan import MockPlan
from mock_notes import MockNotes
from mock_read import MockRead
from mock_thread import MockThread
from mock_versions import normalize_v2, phase_at, phase_start, total_seconds
from mock_write import (MockWrite, answers_mod_apply, apply_step_overrides,
                        enrich_answers)
import mock_backlog
import mock_demo as demo

REASON = {"queued": None, "working": None,
          "reviewing": "Критик проверяет свод (раунд 1)",
          "synthesizing": "Руководитель комнаты дорабатывает свод по замечаниям критика",
          "done": None}


def mock_critic_progress(round_no: int, issues_before: int, closed: int) -> dict:
    """Ход цикла для демо-мока: те же ключи, что отдаёт critic_loop.progress()."""
    before = max(int(issues_before or 0), 0)
    done = min(max(int(closed or 0), 0), before)
    return {"round": int(round_no or 0), "rounds": int(round_no or 0),
            "issues_before": before, "closed": done, "open": before - done,
            "closed_items": ["проверено: срез себестоимости"][:done],
            "open_items": [], "verdict": "", "stop": None}


class App(MockRead, MockWrite, MockThread, MockPlan, MockNotes, MockModels):

    def __init__(self, web_dir: str, fixtures_dir: str, phase_scale: float = 1.0, port: int = 8778,
                 extra_roots: list | None = None):
        self.web_dir = os.path.abspath(web_dir)
        self.fixtures_dir = os.path.abspath(fixtures_dir)
        self.extra_roots = [os.path.abspath(p) for p in (extra_roots or [])]
        self.phase_scale = max(0.001, float(phase_scale))
        self.port = port
        self.started_at = now()
        self.lock = threading.Lock()
        self.fx = {}
        for name in os.listdir(self.fixtures_dir):
            if name.endswith(".json"):
                with open(os.path.join(self.fixtures_dir, name), encoding="utf-8") as fh:
                    self.fx[name] = json.load(fh)
        self.rooms = copy.deepcopy(self.fx["rooms.json"]["rooms"])
        self.room_details = {r["id"]: copy.deepcopy(self.fx["room_%s.json" % r["id"]])
                             for r in self.rooms if "room_%s.json" % r["id"] in self.fx}
        self.requests = {rid: copy.deepcopy(self.fx[name]) for rid, name in FIXTURE_BY_RID.items()}
        self.backlog = {"cafe": copy.deepcopy(self.fx["backlog_cafe.json"])} if "backlog_cafe.json" in self.fx else {}
        for room in self.rooms:
            self.backlog.setdefault(room["id"], mock_backlog.empty(room["id"]))
        self.candidates = {"cafe": copy.deepcopy(self.fx["candidates_cafe.json"]),
                           "fn": {"room": "fn", "generated_at": now(), "count": 0, "items": []}}
        self.live = {}
        self.adopted = {}
        self.counter = 0
        self.log = []
        self.thread_init()          # лента диалога: доставленное и отложенные ответы
        self.notes_init()
        self.plan_init()            # карты проекта комнат (фикстуры plan_*.json)
        self.models_init()          # каталог моделей и настройки участников (WAVE10)

    # --- утилиты ---------------------------------------------------------

    def fixture(self, name: str) -> dict:
        return copy.deepcopy(self.fx[name])

    def room(self, room_id: str) -> dict:
        for room in self.room_details.values():
            if room["id"] == room_id:
                return room
        raise ApiError(404, "not_found", "Комната не найдена", {"room": room_id})

    def room_summary(self, room_id: str) -> dict:
        room = self.room(room_id)
        live = self.summaries(room_id)
        midnight = local_midnight()
        return {"id": room["id"], "title": room["title"], "icon": room["icon"], "archived": room["archived"],
                "orchestrator": room["orchestrator"], "critic": room["critic"],
                "members_count": len(room["members"]), "mode_default": room["mode_default"],
                "critic_pass": room["critic_pass"], "max_rounds": int(room.get("max_rounds") or 1),
                "critic_mode": str(room.get("critic_mode") or "rounds"),
                "critic_round_cap": int(room.get("critic_round_cap") or 12),
                "counters": {"working": sum(1 for s in live if s["status"] in ("working", "reviewing", "synthesizing")),
                             "needs_you": sum(1 for s in live if counts_as_waiting(s)),
                             "done_today": sum(1 for s in live if s["status"] == "done"
                                               and (s.get("finished_at") or 0) >= midnight)},
                "phase": room.get("phase"), "updated_at": now()}

    def summaries(self, room_id: str) -> list:
        out = [detail_to_summary(normalize_v2(copy.deepcopy(d)))
               for d in self.requests.values() if d.get("room") == room_id]
        out += [detail_to_summary(self.live_detail(rid)) for rid, req in self.live.items() if req["room"] == room_id]
        out.sort(key=lambda s: s["created_at"], reverse=True)
        return out

    def detail(self, rid: str) -> dict:
        self._thread_tick(rid)                     # доставить отложенный ответ руководителя
        self.plan_tick(rid)                        # «руководитель додумал» черновик карты
        if rid in self.live:
            detail = self.live_detail(rid)
        elif rid in self.requests:
            detail = normalize_v2(copy.deepcopy(self.requests[rid]))
        else:
            raise ApiError(404, "not_found", "Запрос не найден", {"rid": rid})
        detail["thread"] = self.thread_for(rid, detail)
        self.plan_meta(rid, detail)                # brief/streams_summary/plan_link (WAVE9 §4)
        enrich_answers(detail)                     # W14-D: статусы ответов и метки шагов
        # W14-H §12: шаги пересобираются на каждый GET — действия пользователя
        # («Согласен», «Убрать», …) накладываем заново, как реестр бэкенда.
        apply_step_overrides(detail, rid, self.step_overrides_for(rid))
        return detail

    # --- бэклог комнаты ----------------------------------------------------

    def _room_details(self, room_id: str) -> list:
        """Все запросы комнаты (фикстуры + живые) — источник шагов/вопросов/решений."""
        out = [(rid, normalize_v2(copy.deepcopy(d))) for rid, d in list(self.requests.items())
               if d.get("room") == room_id]
        out += [(rid, self.live_detail(rid)) for rid, req in list(self.live.items()) if req.get("room") == room_id]
        return out

    def backlog_data(self, room_id: str) -> dict:
        """Бэклог комнаты: сохранённые элементы + недостающие из всех её запросов."""
        self.room(room_id)
        data = self.backlog.setdefault(room_id, mock_backlog.empty(room_id))
        added = 0
        for rid, detail in self._room_details(room_id):
            added += mock_backlog.merge(data["items"], mock_backlog.items_from_detail(rid, detail))
        if added:
            data["updated_at"] = now()
        return mock_backlog.refresh_counts(data)

    def upsert_backlog_item(self, room_id: str, item_id: str):
        """Неизвестный id `<root>:<local>` → взять элемент из запроса и дописать в бэклог."""
        data = self.backlog_data(room_id)
        item = mock_backlog.find(data, item_id)
        if item is not None:
            return item
        root = item_id.split(":", 1)[0]
        try:
            detail = self.detail(root)
        except ApiError:
            return None
        if detail.get("room") != room_id:
            return None
        for derived in mock_backlog.items_from_detail(root, detail):
            if derived["id"] == item_id:
                data["items"].append(derived)
                mock_backlog.refresh_counts(data)
                data["updated_at"] = now()
                return derived
        return None

    # --- живой запрос -----------------------------------------------------

    def _phase(self, req: dict) -> tuple:
        offset = phase_start(req["jump_phase"], req["critic"]) if req.get("jump_phase") else 0.0
        t0 = req.get("jump_t0") or req["t0"]
        elapsed = offset + (time.monotonic() - t0) / self.phase_scale
        return phase_at(elapsed, req["critic"]), elapsed

    def jump(self, rid: str, phase: str) -> None:
        """Перезапустить эволюцию с нужной фазы (действия decompose/revise/critique)."""
        req = self.live[rid]
        if phase == "reviewing" and not req["critic"]:
            phase = "synthesizing"
        req["jump_phase"] = phase
        req["jump_t0"] = time.monotonic()

    def _history(self, req: dict, versions: int, revision_ids: list) -> list:
        """История версий: v1 — свод команды, дальше — доработки Михаила."""
        notes = [n for n in (req.get("revision_notes") or []) if str(n).strip()]
        hist = []
        for number in range(1, max(1, versions) + 1):
            if number == 1:
                hist.append({"version": 1, "kind": "initial", "task_id": req["id"],
                             "author": req["author"], "at": req["created_at"] + 30,
                             "verdict": "approve" if req["critic"] else None,
                             "note": "Первый свод команды."})
                continue
            index = number - 2
            note = notes[index] if index < len(notes) else "Доработка по замечанию."
            task_id = revision_ids[min(index, len(revision_ids) - 1)] if revision_ids else req["id"]
            hist.append({"version": number, "kind": "revision", "task_id": task_id,
                         "author": req["author"], "at": req["created_at"] + 35 + 25 * index,
                         "verdict": None, "note": str(note)[:300]})
        return hist

    def live_detail(self, rid: str) -> dict:
        """Собрать ответ живого запроса по прошедшему времени и действиям."""
        req = self.live[rid]
        phase, elapsed = self._phase(req)
        room = self.room(req["room"])
        revisions = int(req.get("extra_versions") or 0)
        created = req["created_at"]
        total = int(total_seconds(req["critic"]))

        detail = normalize_v2(self.fixture("request_queued.json"))
        detail.update({
            "id": rid, "room": req["room"], "title": req["title"], "text": req["text"],
            "mode": "auto", "critic": req["critic"], "created_at": created,
            "started_at": None if phase == "queued" else created + 4,
            "finished_at": (created + total + 25 * max(0, revisions - 1)) if phase == "done" else None,
            "duration_s": (total + 25 * max(0, revisions - 1)) if phase == "done" else None,
            "status": phase, "status_reason": None, "archived": bool(req.get("archived")),
            "preview": req["text"][:200], "next_steps": [], "decisions": [], "artifacts": [],
            "warnings": [], "future_questions": [],
            "stage": copy.deepcopy(self.fx["request_done.json"]["stage"]) if req["room"] == "cafe" else None,
            "report": {"available": False, "kind": None, "path": None, "title": None},
        })

        tree = demo.build_tree(req["title"], room, phase, revisions, created, req.get("revision_notes"))
        if not req["critic"]:
            tree = [n for n in tree if n.get("role") not in ("critic", "revision")]
        id_map, key_map = {}, {}
        worker_index = 0
        for index, node in enumerate(tree):
            if node.get("role") == "root":
                key = "root"
            elif node.get("role") == "critic":
                key = "critic"
            elif node.get("role") == "revision":
                key = "revision"
            else:
                worker_index += 1
                key = "w%d" % worker_index
            key_map[key] = child_id(rid, index) if node.get("role") != "root" else rid
            id_map[node["id"]] = key_map[key]
            node["id"] = id_map[node["id"]]
        for node in tree:
            node["parent_ids"] = [id_map.get(parent, rid) for parent in node.get("parent_ids") or []]
            node["outside_room"] = False
        for task_id, patch in (req.get("node_patch") or {}).items():
            for node in tree:
                if node["id"] == task_id:
                    node.update(patch)
        for node in tree:                     # WAVE10 §21: подпись «провайдер · модель · мышление»
            if node.get("assignee"):
                node["model_label"] = self.model_label_for(room, str(node["assignee"]))
        detail["tree"] = tree
        kids = [n for n in tree if n.get("role") != "root"]
        done = sum(1 for n in kids if n.get("status") in ("done", "archived"))
        detail["progress"] = {"done": done, "total": len(kids),
                              "percent": 100 if phase == "done" else (0 if not kids else int(100 * done / len(kids)))}

        root = next((n for n in tree if n.get("role") == "root"), None)
        if root is not None:
            root["status"] = {"queued": "triage", "working": "running", "synthesizing": "running"}.get(phase, "done")
            if phase == "done":
                root["finished_at"] = created + total + 25 * max(0, revisions - 1)
            if req.get("comments"):
                root.setdefault("comments", []).extend(copy.deepcopy(req["comments"]))

        revision_ids = [n["id"] for n in tree if n.get("role") == "revision"]
        shown_version = (1 + revisions) if phase == "done" else max(1, revisions)
        syn_state = "pending" if phase in ("queued", "working") else "ready"
        if syn_state == "pending":
            detail["synthesis"] = {"state": "pending", "version": 0, "text_md": "", "author": None,
                                   "at": None, "source_task": None, "legacy": False, "history": []}
        else:
            text_md = demo.answer_md(req["title"], req["text"], req.get("revision_notes"))
            detail["synthesis"] = {
                "state": "ready", "version": shown_version, "text_md": text_md,
                "author": req["author"], "at": created + 20,
                "source_task": revision_ids[-1] if (revision_ids and shown_version > 1) else rid,
                "legacy": False, "history": self._history(req, shown_version, revision_ids),
            }

        if phase == "reviewing":
            detail["status_reason"] = "Критик проверяет свод (раунд 1)"
        elif phase == "synthesizing":
            detail["status_reason"] = ("Руководитель комнаты дорабатывает свод по замечанию Михаила" if revisions
                                       else "Руководитель комнаты собирает финальный ответ")

        critic_id = next((n["id"] for n in tree if n.get("role") == "critic"), None)
        max_rounds = int(req.get("max_rounds") or room.get("max_rounds") or 1)
        mode = str(req.get("critic_mode") or room.get("critic_mode") or "rounds")
        round_cap = int(room.get("critic_round_cap") or 12)
        base = {"max_rounds": max_rounds, "mode": mode, "round_cap": round_cap,
                "progress": {}, "paused": None, "stopped": None, "rounds_history": []}
        if not req["critic"]:
            critic = {"enabled": False, "round": 0, "max_rounds": max_rounds, "state": "off",
                      "verdict": None, "task_id": None, "issues": [], **base}
        elif phase in ("queued", "working"):
            critic = {"enabled": True, "round": 0, "max_rounds": max_rounds, "state": "waiting",
                      "verdict": None, "task_id": None, "issues": [], **base}
        elif phase == "reviewing":
            critic = {"enabled": True, "round": 1, "max_rounds": max_rounds, "state": "reviewing",
                      "verdict": None, "task_id": critic_id, "issues": [], **base}
            critic["progress"] = mock_critic_progress(1, 2, 0)
        else:
            critic = {"enabled": True, "round": 1, "max_rounds": max_rounds, "state": "approved",
                      "verdict": "approve", "task_id": critic_id, "issues": [], **base}
            critic["progress"] = mock_critic_progress(1, 2, 2)
            critic["rounds_history"] = [{"round": 1, "verdict": "approve", "issues": [], "at": created}]
        detail["critic"] = critic

        detail["open_questions"] = []
        if phase == "done":
            question = demo.open_question(req["title"])
            answers = req.get("answers") or {}
            if question["id"] in answers:
                saved = answers[question["id"]]
                # W14-D: ответ может быть просьбой пояснить — тогда вопрос НЕ закрыт
                record = {"text": saved.get("text"), "at": saved.get("at"),
                          "answered": saved.get("answered", True),
                          "status": saved.get("status") or "sent"}
                question = answers_mod_apply(question, record)
            detail["open_questions"] = [question]
            detail["next_steps"] = demo.next_steps(req["title"])

        events = demo.timeline(created, req["title"], room, phase, revisions)
        timeline = []
        for event in events:
            if event["at"] - created <= elapsed + 1:
                at = created + max(1, int(round((event["at"] - created) * self.phase_scale)))
                timeline.append({**event, "at": at, "task_id": id_map.get(event.get("task_id"), rid)})
        for comment in req.get("comments") or []:
            timeline.append({"at": comment["at"], "kind": "commented", "task_id": rid,
                             "actor": comment["author"], "text": comment["text"][:160]})
        if phase == "done":
            for question in detail["open_questions"]:
                if question.get("answered"):
                    timeline.append({"at": question["answered_at"], "kind": "commented", "task_id": rid,
                                     "actor": "mikhail", "text": "Ответ Михаила: " + question["answer"][:150]})
        timeline.sort(key=lambda e: e["at"])
        detail["timeline"] = timeline

        if syn_state == "ready":
            variant = demo.VARIANTS[demo.variant_for(req["text"])]
            detail["report"] = {"available": True, "kind": "spec", "path": None,
                                "key": demo.report_key(rid), "title": variant["report_title"]}
        detail["needs_you"] = compute_needs_you(detail)
        return detail

    def new_live_request(self, room_id: str, text: str, title: str | None = None,
                         critic: bool = True, seed: str = "t_2a6f5c93",
                         critic_mode: str | None = None, max_rounds: int | None = None) -> dict:
        room = self.room(room_id)
        with self.lock:
            self.counter += 1
            rid = "t_%08x" % (0x9A000000 + self.counter)
            clean = re.sub(r"[#*`>]", "", text.strip().split("\n")[0]).strip()
            if len(clean) > 80:
                clean = clean[:79].rsplit(" ", 1)[0].rstrip(" ,;:") + "…"
            self.live[rid] = {"id": rid, "t0": time.monotonic(), "created_at": now(), "room": room_id,
                              "title": (title or clean or "Новый запрос")[:120], "text": text,
                              "mode": "auto", "critic": bool(critic), "seed_rid": seed,
                              "critic_mode": critic_mode or room.get("critic_mode") or "rounds",
                              "max_rounds": max_rounds,
                              "author": room["orchestrator"], "extra_versions": 0,
                              "revision_notes": [], "answers": {},
                              "jump_phase": None, "jump_t0": None}
        return self.live_detail(rid)

    # --- GET --------------------------------------------------------------
