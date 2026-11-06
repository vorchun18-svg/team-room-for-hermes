#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Сверка fixtures/*.json со схемами docs/API.md (обязательные поля, типы, enum, связи).

    python tools/check_fixtures.py            # проверить все фикстуры
    python tools/check_fixtures.py -v         # + список проверенных файлов

Коды выхода: 0 — всё сходится; 1 — есть расхождения (печатаются путём `файл: поле`).
Скрипт не зависит от мок-сервера и не пишет на диск.
"""
from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FIX = ROOT / "fixtures"

STATUSES = {"queued", "working", "reviewing", "synthesizing", "done", "needs_you", "failed"}
CRITIC_STATES = {"off", "waiting", "reviewing", "approved", "revised", "failed"}
SYNTH_STATES = {"pending", "ready", "none"}
ROLES = {"worker", "critic", "synthesizer", "root", "revision"}
TASK_STATUSES = {"triage", "todo", "ready", "running", "review", "blocked", "scheduled", "done", "archived"}
PHASE_IDS = ["idea", "legal", "brand", "packaging", "production", "sales"]
PHASE_STATUSES = {"done", "active", "next", "blocked"}
ARTIFACT_KINDS = {"md", "json", "html", "csv", "image", "pdf", "docx", "xlsx", "pptx", "other"}
WARNING_CODES = {"legacy_metadata", "missing_artifact", "no_synthesis", "report_invalid", "schema_drift",
                 "partial_ingest", "dispatcher_down", "card_failed", "outside_room", "critic_failed",
                 "critic_rounds_exhausted"}
TIMELINE_KINDS = {"created", "decomposed", "promoted", "claimed", "started", "commented", "completed",
                  "blocked", "unblocked", "failed", "archived", "heartbeat"}   # W17-B §5
JOURNAL_KINDS = {"request_created", "request_status", "request_done", "step_added", "step_done",
                 "question_open", "question_answered", "decision_proposed", "decision_accepted",
                 "report_added", "artifact_added", "member_added", "member_removed", "room_created",
                 "room_archived", "ingest", "error", "model_changed", "model_restarted"}   # WAVE10
THREAD_KINDS = {"message", "comment", "event"}
AUTHOR_KINDS = {"mikhail", "agent", "system"}
PLAN_STATUSES = {"draft", "approved", "running", "done"}
STREAM_STATUSES = {"proposed", "queued", "running", "waiting_quota", "review", "done", "dropped"}
LOG_KINDS = {"created", "changed", "added", "launched", "dropped", "quota"}
LOG_WHO = {"manager", "mikhail", "system"}
LAUNCH_MODES = {"plan_first", "auto"}
#: короткие подписи мышления в `model_label` — как `EFFORT_SHORT` в `server/agent_models.py`
EFFORT_SHORT = ("без размышления", "минимальные", "низкие", "средние", "высокие", "очень высокие",
                "максимальные", "предельные")
#: WAVE10 — «модель, провайдер и уровень мышления участника»
MODEL_SOURCES = {"room", "agent"}
MODEL_EFFORTS = {"none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra"}
MODEL_EFFORT_ORDER = ["none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra"]
MAX_AGENT_NOTE = 8000
BACKLOG_KINDS = {"next_step", "future_question", "open_question", "decision"}
BACKLOG_STATUSES = {"open", "asked", "done", "dismissed"}
RID = re.compile(r"^t_[0-9a-f]{8}$")
ROOM_ID = re.compile(r"^[a-z0-9][a-z0-9-]{1,31}$")


class Check:
    def __init__(self):
        self.errors = []
        self.checked = []

    def ok(self, cond, where, msg):
        if not cond:
            self.errors.append(f"{where}: {msg}")

    def field(self, obj, where, name, types, *, null=False):
        if not isinstance(obj, dict) or name not in obj:
            self.errors.append(f"{where}: нет поля {name}")
            return None
        value = obj[name]
        if value is None and null:
            return None
        if not isinstance(value, types) or (isinstance(value, bool) and bool not in
                                             (types if isinstance(types, tuple) else (types,))):
            self.errors.append(f"{where}.{name}: ожидался {types}, получено {type(value).__name__}")
            return None
        return value

    def enum(self, obj, where, name, allowed, null=False):
        value = self.field(obj, where, name, str, null=null)
        if value is not None or (isinstance(obj, dict) and obj.get(name) is None and null):
            if value is not None and value not in allowed:
                self.errors.append(f"{where}.{name}: {value!r} вне {sorted(allowed)}")
        return value


def load(name: str) -> dict:
    with open(FIX / name, encoding="utf-8") as fh:
        return json.load(fh)


def check_critic_ref(c: Check, block, where):
    if not isinstance(block, dict):
        c.errors.append(f"{where}: critic должен быть объектом CriticRef")
        return
    if set(block) != {"enabled", "state", "verdict", "round"}:
        c.errors.append(f"{where}: ключи CriticRef {sorted(block)} != ['enabled','round','state','verdict']")
    c.field(block, where, "enabled", bool)
    c.enum(block, where, "state", CRITIC_STATES)
    c.field(block, where, "verdict", str, null=True)
    c.field(block, where, "round", int)


def check_critic_block(c: Check, block, where):
    need = {"enabled", "round", "max_rounds", "state", "verdict", "issues", "task_id"}
    if not isinstance(block, dict) or not need <= set(block):
        c.errors.append(f"{where}: critic должен быть CriticBlock с ключами {sorted(need)}")
        return
    c.field(block, where, "enabled", bool)
    c.field(block, where, "round", int)
    c.field(block, where, "max_rounds", int)
    c.enum(block, where, "state", CRITIC_STATES)
    c.field(block, where, "verdict", str, null=True)
    c.field(block, where, "task_id", str, null=True)
    if not isinstance(block.get("issues"), list):
        c.errors.append(f"{where}.issues: ожидался список")
    else:
        for i, issue in enumerate(block["issues"]):
            if not isinstance(issue, dict) or not {"where", "problem", "fix"} <= set(issue):
                c.errors.append(f"{where}.issues[{i}]: нужны where/problem/fix")


def check_stage(c: Check, stage, where, *, with_updated: bool = False):
    if not isinstance(stage, dict):
        c.errors.append(f"{where}: stage должен быть объектом")
        return
    c.field(stage, where, "current", str, null=True)
    phases = stage.get("phases")
    if not isinstance(phases, list) or len(phases) != 6:
        c.errors.append(f"{where}.phases: ожидалось 6 фаз")
        return
    if [p.get("id") for p in phases] != PHASE_IDS:
        c.errors.append(f"{where}.phases: порядок {[p.get('id') for p in phases]} != {PHASE_IDS}")
    for i, phase in enumerate(phases):
        p = f"{where}.phases[{i}]"
        c.field(phase, p, "title", str)
        c.field(phase, p, "note", str)
        c.enum(phase, p, "status", PHASE_STATUSES)
        if with_updated:
            c.field(phase, p, "updated_at", int, null=True)


def check_summary(c: Check, item, where, *, ref_critic: bool = True):
    need = {"id", "room", "title", "status", "status_reason", "mode", "critic", "created_at", "started_at",
            "finished_at", "duration_s", "progress", "assignee", "needs_you", "preview", "archived"}
    if not isinstance(item, dict) or not need <= set(item):
        missing = sorted(need - set(item or {}))
        c.errors.append(f"{where}: нет полей RequestSummary {missing}")
        return
    rid = c.field(item, where, "id", str)
    if rid and not RID.match(rid):
        c.errors.append(f"{where}.id: {rid!r} не t_xxxxxxxx")
    c.field(item, where, "room", str)
    c.field(item, where, "title", str)
    c.enum(item, where, "status", STATUSES)
    c.field(item, where, "status_reason", str, null=True)
    c.ok(item.get("mode") == "auto", where, "mode должен быть 'auto'")
    if ref_critic:
        check_critic_ref(c, item.get("critic"), f"{where}.critic")
    c.field(item, where, "created_at", int)
    c.field(item, where, "started_at", int, null=True)
    c.field(item, where, "finished_at", int, null=True)
    c.field(item, where, "duration_s", int, null=True)
    c.field(item, where, "assignee", str, null=True)
    c.field(item, where, "needs_you", bool)
    c.field(item, where, "preview", str)
    c.field(item, where, "archived", bool)
    prog = item.get("progress")
    if not isinstance(prog, dict) or not {"done", "total", "percent"} <= set(prog):
        c.errors.append(f"{where}.progress: нужны done/total/percent")
        return
    done, total, percent = prog["done"], prog["total"], prog["percent"]
    if not all(isinstance(v, int) for v in (done, total, percent)):
        c.errors.append(f"{where}.progress: значения должны быть int")
        return
    c.ok(0 <= done <= total, where, f"progress.done {done} > total {total}")
    expect = 100 if item.get("status") == "done" else (0 if total == 0 else (100 * done) // total)
    c.ok(percent == expect, where, f"progress.percent {percent} != {expect}")
    st, fin, dur = item.get("started_at"), item.get("finished_at"), item.get("duration_s")
    if dur is not None and isinstance(st, int) and isinstance(fin, int):
        c.ok(dur == fin - st, where, f"duration_s {dur} != finished_at-started_at ({fin - st})")
    if fin is None:
        c.ok(dur is None, where, "duration_s должен быть null, пока запрос не завершён")


def check_thread(c: Check, thread, where: str, ids: list) -> None:
    """ThreadItem (WAVE8, блок A): поля, типы, порядок по времени, связь с деревом."""
    if not isinstance(thread, list):
        c.errors.append(f"{where}: thread должен быть списком")
        return
    if not thread:
        c.errors.append(f"{where}: лента пустая — в демо она должна быть заполнена")
        return
    times, seen = [], set()
    for i, item in enumerate(thread):
        tp = f"{where}[{i}]"
        c.field(item, tp, "id", str)
        c.field(item, tp, "at", int)
        c.enum(item, tp, "author_kind", AUTHOR_KINDS)
        c.field(item, tp, "author", str)
        c.field(item, tp, "author_label", str)
        c.field(item, tp, "text", str)
        c.field(item, tp, "task_id", str, null=True)
        c.enum(item, tp, "kind", THREAD_KINDS)
        if not isinstance(item, dict):
            continue
        if isinstance(item.get("text"), str):
            c.ok(bool(item["text"].strip()), tp, "пустой текст в ленте")
        if isinstance(item.get("task_id"), str):
            c.ok(item["task_id"] in ids, tp, f"task_id {item['task_id']} нет в дереве запроса")
        if item.get("author_kind") == "mikhail":
            c.ok(item.get("author") == "mikhail", tp, "author_kind mikhail → author=«mikhail»")
            c.ok(item.get("kind") in ("message", "comment"), tp, "сообщение Михаила не может быть событием")
        if item.get("author_kind") == "system":
            c.ok(item.get("kind") == "event", tp, "системная запись должна быть kind=event")
        if item.get("author_kind") == "agent":
            label = str(item.get("author_label") or "")
            c.ok(label and label != item.get("author"), tp,
                 "подпись агента должна быть человеческой ролью, а не именем профиля")
            if str(item.get("author") or "").startswith("biz-"):
                c.ok("·" in label, tp, "подпись агента Лето — «Имя · роль» (Глеб · руководитель комнаты)")
        if isinstance(item.get("id"), str):
            c.ok(item["id"] not in seen, tp, f"id {item['id']} повторяется в ленте")
            seen.add(item["id"])
        if isinstance(item.get("at"), int):
            times.append(item["at"])
    c.ok(times == sorted(times), where, "лента должна идти по времени (at по возрастанию)")


def check_project_dir(c: Check, room, where: str) -> None:
    """Поле комнаты `project_dir` (Д2): необязательное, но если есть — абсолютный путь."""
    if not isinstance(room, dict) or "project_dir" not in room:
        return
    value = c.field(room, where, "project_dir", str, null=True)
    if not isinstance(value, str) or not value.strip():
        return
    c.ok(re.match(r"^[A-Za-z]:\\", value) is not None, where,
         f"project_dir {value!r} должен быть абсолютным путём вида C:\\Папка")
    c.ok(".." not in value.replace("/", "\\").split("\\"), where, "project_dir без «..»")
    low = value.casefold()
    for bad in ("c:\\windows", "c:\\program files", "appdata\\local\\hermes", "hermes\\skills"):
        c.ok(not low.startswith(bad), where, f"project_dir не должен быть внутри {bad}")


def roster(room: dict) -> list:
    """Состав комнаты: руководитель + участники + критик (WAVE9 §1, §9)."""
    out = []
    for profile in [room.get("orchestrator")] + [m.get("profile") for m in room.get("members") or []]:
        if isinstance(profile, str) and profile and profile not in out:
            out.append(profile)
    if isinstance(room.get("critic"), str) and room["critic"] and room["critic"] not in out:
        out.append(room["critic"])
    return out


def check_room_settings(c: Check, room, where: str) -> None:
    """Поля комнаты волны 9: режим запуска, одновременные направления, проектный режим (§3)."""
    if not isinstance(room, dict):
        return
    if "launch_mode" in room:
        c.enum(room, where, "launch_mode", LAUNCH_MODES)
    if "max_parallel_streams" in room:
        value = c.field(room, where, "max_parallel_streams", int)
        if isinstance(value, int):
            c.ok(1 <= value <= 4, where, f"max_parallel_streams {value} вне 1..4")
    if "project_mode" in room:
        c.field(room, where, "project_mode", bool)


def check_agent_notes(c: Check, room, where: str) -> None:
    """`agent_notes` (§9): ключи — профили состава, текст 1..8000 знаков, updated_at int."""
    if not isinstance(room, dict) or "agent_notes" not in room:
        return
    notes = room.get("agent_notes")
    if notes is None:
        return
    if not isinstance(notes, dict):
        c.errors.append(f"{where}.agent_notes: ожидался объект «профиль: {text, updated_at}»")
        return
    people = roster(room)
    for profile, item in notes.items():
        np_ = f"{where}.agent_notes[{profile}]"
        c.ok(profile in people, np_, f"профиль {profile} не в составе комнаты")
        if not isinstance(item, dict):
            c.errors.append(f"{np_}: ожидался объект {{text, updated_at}}")
            continue
        text = c.field(item, np_, "text", str)
        c.field(item, np_, "updated_at", int)
        if isinstance(text, str):
            c.ok(bool(text.strip()), np_, "пустые указания не хранятся — поле очищается")
            c.ok(len(text) <= MAX_AGENT_NOTE, np_, f"указания {len(text)} знаков > {MAX_AGENT_NOTE}")


def catalog_index(c: Check, catalog: dict) -> dict:
    """Проверка каталога моделей и индексов `провайдер → модели` (WAVE10 §17–19).

    Каталог демонстрационный: 4 вымышленных провайдера, у одного `configured: false`
    (проверка фильтра «только провайдеры с рабочим ключом»), усилия — 7 значений.
    """
    if not isinstance(catalog, dict):
        c.errors.append("models: каталог должен быть объектом")
        return {}
    providers = c.field(catalog, "models", "providers", list) or []
    efforts = catalog.get("efforts")
    c.field(catalog, "models", "source", str)
    c.field(catalog, "models", "cached", bool)
    c.field(catalog, "models", "updated_at", int)
    c.field(catalog, "models", "warnings", list)
    c.ok(isinstance(efforts, list) and efforts, "models", "efforts должен быть непустым списком")
    if isinstance(efforts, list):
        ids = []
        for i, effort in enumerate(efforts):
            ep = f"models.efforts[{i}]"
            if not isinstance(effort, dict):
                c.errors.append(f"{ep}: ожидался объект {{id, label}} (docs/API.md §6.11)")
                continue
            eid = c.enum(effort, ep, "id", MODEL_EFFORTS)
            c.field(effort, ep, "label", str)
            if isinstance(eid, str):
                ids.append(eid)
        c.ok(len(set(ids)) == len(ids), "models", "efforts повторяются")
        c.ok(ids == [e for e in MODEL_EFFORT_ORDER if e in ids], "models",
             "efforts должны идти от none к ultra")
    index, ids, configured = {}, [], []
    for i, provider in enumerate(providers):
        pp = f"models.providers[{i}]"
        pid = c.field(provider, pp, "id", str)
        c.field(provider, pp, "label", str)
        state = c.field(provider, pp, "configured", bool)
        models = c.field(provider, pp, "models", list) or []
        if isinstance(pid, str):
            ids.append(pid)
        if state is True:
            configured.append(pid)
        c.ok(1 <= len(models) <= 12, pp, f"у провайдера {len(models)} моделей — нужно 1..12")
        mids = []
        for j, model in enumerate(models):
            mp = f"{pp}.models[{j}]"
            mid = c.field(model, mp, "id", str)
            c.field(model, mp, "label", str)
            if isinstance(mid, str):
                mids.append(mid)
        c.ok(len(set(mids)) == len(mids), pp, "id моделей повторяются")
        if isinstance(pid, str):
            index[pid] = {"label": provider.get("label"), "configured": state is True,
                          "models": {str(m.get("id")): m.get("label") for m in models if isinstance(m, dict)}}
    c.ok(len(providers) >= 3, "models", "провайдеров меньше 3 — фильтр по ключу не проверить")
    c.ok(len(set(ids)) == len(ids), "models", "id провайдеров повторяются")
    c.ok(bool(configured), "models", "нет ни одного настроенного провайдера")
    c.ok(len(configured) < len(ids), "models", "нужен провайдер с configured:false — иначе фильтр не проверить")
    return index


def check_model_settings(c: Check, item, where: str, index: dict, *, with_source: bool) -> dict:
    """`model_settings`/`agent_default` участника: провайдер и модель — из каталога."""
    if not isinstance(item, dict):
        c.errors.append(f"{where}: ожидался объект {{provider, model, reasoning}}")
        return {}
    need = ["provider", "model", "reasoning"] + (["source", "updated_at"] if with_source else [])
    c.ok(set(item) == set(need), where, f"ключи {sorted(item)} != {sorted(need)}")
    provider_id = c.field(item, where, "provider", str)
    model_id = c.field(item, where, "model", str)
    reasoning = c.enum(item, where, "reasoning", MODEL_EFFORTS)
    if with_source:
        c.enum(item, where, "source", MODEL_SOURCES)
        c.field(item, where, "updated_at", int)
    provider = index.get(provider_id)
    if provider is None:
        c.errors.append(f"{where}.provider: {provider_id!r} нет в каталоге моделей")
    else:
        c.ok(provider["configured"], where,
             f"провайдер {provider_id!r} не настроен, а выбран для участника")
        c.ok(model_id in provider["models"], where,
             f"модели {model_id!r} нет у провайдера {provider_id!r}")
    c.ok(bool(reasoning), where, "reasoning обязателен")
    return item


def check_member_models(c: Check, room, where: str, index: dict, catalog: dict) -> None:
    """Новые поля волны 10 у комнаты: `agent_models` и `model_settings`/`agent_default` участников.

    Все поля необязательные: старые фикстуры без них остаются валидными.
    """
    if not isinstance(room, dict):
        return
    people = roster(room)
    stored = room.get("agent_models")
    if stored is not None:
        if not isinstance(stored, dict):
            c.errors.append(f"{where}.agent_models: ожидался объект «профиль: настройка»")
            stored = {}
        for profile, item in stored.items():
            ap = f"{where}.agent_models[{profile}]"
            c.ok(profile in people, ap, f"профиль {profile} не в составе комнаты")
            if not isinstance(item, dict):
                c.errors.append(f"{ap}: ожидался объект {{provider, model, reasoning, updated_at}}")
                continue
            check_model_settings(c, {k: item.get(k) for k in ("provider", "model", "reasoning")},
                                 ap, index, with_source=False)
            c.field(item, ap, "updated_at", int)
    defaults = catalog.get("agent_defaults") if isinstance(catalog, dict) else None
    for i, member in enumerate(room.get("members") or []):
        mp = f"{where}.members[{i}]"
        profile = (member or {}).get("profile") if isinstance(member, dict) else None
        settings = (member or {}).get("model_settings") if isinstance(member, dict) else None
        agent_default = (member or {}).get("agent_default") if isinstance(member, dict) else None
        if settings is None and agent_default is None:
            continue                      # поле необязательное
        if settings is not None:
            check_model_settings(c, settings, mp + ".model_settings", index, with_source=True)
        if agent_default is not None:
            check_model_settings(c, agent_default, mp + ".agent_default", index, with_source=False)
            if isinstance(defaults, dict) and profile in defaults:
                expected = defaults[profile]
                c.ok({k: agent_default.get(k) for k in ("provider", "model", "reasoning")}
                     == {k: expected.get(k) for k in ("provider", "model", "reasoning")}, mp,
                     "agent_default расходится с models.json.agent_defaults[%s]" % profile)
        if isinstance(settings, dict) and isinstance(agent_default, dict):
            has_room = isinstance(stored, dict) and profile in stored
            want = "room" if has_room else "agent"
            c.ok(settings.get("source") == want, mp,
                 f"source {settings.get('source')!r}, а в agent_models профиля "
                 f"{'есть' if has_room else 'нет'} — ожидалось {want!r}")
            if has_room:
                c.ok({k: settings.get(k) for k in ("provider", "model", "reasoning")}
                     == {k: stored[profile].get(k) for k in ("provider", "model", "reasoning")}, mp,
                     "model_settings расходится с agent_models комнаты")


def check_model_label(c: Check, node, where: str, index: dict) -> None:
    """`model_label` карточки — «провайдер · модель · мышление» по каталогу (необязательное поле)."""
    if not isinstance(node, dict) or "model_label" not in node:
        return
    label = c.field(node, where, "model_label", str)
    if not isinstance(label, str):
        return
    parts = [p.strip() for p in label.split(" · ")]
    c.ok(len(parts) == 3, where, f"model_label {label!r} — «провайдер · модель · мышление»")
    c.ok(bool(label.strip()) and len(label) <= 120, where, "model_label пуст или длиннее 120 знаков")
    if len(parts) != 3 or not index:
        return
    providers = {p.get("label"): p for p in index.values()}
    provider = providers.get(parts[0])
    c.ok(provider is not None, where, f"провайдера {parts[0]!r} нет в каталоге")
    if provider is not None:
        c.ok(parts[1] in provider["models"].values(), where,
             f"модели {parts[1]!r} нет у провайдера {parts[0]!r}")
    labels = set(EFFORT_SHORT)
    c.ok(parts[2] in labels, where, f"мышления {parts[2]!r} нет среди подписей усилий")


def check_plan(c: Check, plan, where: str, room: dict, request_ids: set) -> None:
    """Plan/Stream/LogItem (WAVE9 §1): поля, лимиты, состав, зависимости без циклов."""
    if not isinstance(plan, dict):
        c.errors.append(f"{where}: карта должна быть объектом")
        return
    c.field(plan, where, "version", int)
    brief = c.field(plan, where, "brief_root_id", str)
    if isinstance(brief, str) and brief:
        c.ok(brief in request_ids, where, f"brief_root_id {brief} нет среди запросов комнаты")
    c.field(plan, where, "updated_at", int)
    c.enum(plan, where, "status", PLAN_STATUSES)
    streams = plan.get("streams")
    if not isinstance(streams, list) or not streams:
        c.errors.append(f"{where}.streams: нужен непустой список направлений")
        return
    c.ok(len(streams) <= 8, where, f"в карте {len(streams)} направлений — больше 8 нельзя")
    ids = [s.get("id") for s in streams if isinstance(s, dict)]
    c.ok(len(set(ids)) == len(ids), where, "id направлений повторяются")
    people = roster(room)
    for i, stream in enumerate(streams):
        sp = f"{where}.streams[{i}]"
        sid = c.field(stream, sp, "id", str)
        if isinstance(sid, str):
            c.ok(re.fullmatch(r"s\d{1,3}", sid) is not None, sp, f"id направления {sid!r} — вида s1")
        title = c.field(stream, sp, "title", str)
        if isinstance(title, str):
            c.ok(3 <= len(title) <= 120, sp, f"название {len(title)} знаков вне 3..120")
        c.field(stream, sp, "goal", str)
        owner = c.field(stream, sp, "owner", str)
        if isinstance(owner, str):
            c.ok(owner in people, sp, f"ответственный {owner} не участвует в комнате")
        members = stream.get("members")
        if not isinstance(members, list):
            c.errors.append(f"{sp}.members: ожидался список профилей")
        else:
            for member in members:
                c.ok(member in people, sp, f"участник {member} не участвует в комнате")
        deps = stream.get("depends_on")
        if not isinstance(deps, list):
            c.errors.append(f"{sp}.depends_on: ожидался список id направлений")
            deps = []
        for dep in deps:
            c.ok(dep in ids, sp, f"зависимость {dep} — такого направления нет в карте")
            c.ok(dep != sid, sp, "направление не может зависеть от себя")
        due = stream.get("due")
        if due is not None:
            c.ok(isinstance(due, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", due) is not None, sp,
                 "due — дата вида 2026-11-20 или null")
        c.enum(stream, sp, "status", STREAM_STATUSES)
        rid = stream.get("request_id")
        if rid is not None:
            c.ok(isinstance(rid, str) and RID.match(rid) is not None, sp, "request_id — t_xxxxxxxx или null")
            if isinstance(rid, str):
                c.ok(rid in request_ids, sp, f"request_id {rid} нет среди запросов комнаты")
        c.ok(bool(str(stream.get("reason") or "").strip()), sp, "у направления нужна причина (reason)")
        c.enum(stream, sp, "created_by", {"manager", "mikhail"})
        status = stream.get("status")
        if status in ("running", "review"):
            c.ok(stream.get("request_id") is not None, sp,
                 f"направление в статусе {status} должно ссылаться на под-запрос")
    # циклы в depends_on — как server/plan.validate_plan
    graph = {s["id"]: [d for d in s.get("depends_on") or [] if isinstance(d, str)]
             for s in streams if isinstance(s, dict) and s.get("id")}
    state = {}

    def walk(node: str, path: list) -> bool:
        if state.get(node) == 1:
            c.errors.append(f"{where}: цикл зависимостей {' → '.join(path[path.index(node):] + [node])}")
            return True
        if state.get(node) == 2:
            return False
        state[node] = 1
        for dep in graph.get(node, []):
            if dep in graph and walk(dep, path + [node]):
                return True
        state[node] = 2
        return False

    for sid in graph:
        if state.get(sid) is None and walk(sid, []):
            break
    log = plan.get("log")
    if not isinstance(log, list) or not log:
        c.errors.append(f"{where}.log: журнал плана должен быть заполнен причинами")
        return
    prev = None
    for i, item in enumerate(log):
        lp = f"{where}.log[{i}]"
        at = c.field(item, lp, "at", int)
        c.enum(item, lp, "who", LOG_WHO)
        c.enum(item, lp, "kind", LOG_KINDS)
        text = c.field(item, lp, "text", str)
        if isinstance(text, str):
            c.ok(bool(text.strip()), lp, "пустая запись журнала")
        stream_id = item.get("stream_id")
        if stream_id is not None:
            c.ok(stream_id in ids, lp, f"stream_id {stream_id} нет в карте")
        if isinstance(at, int):
            if prev is not None:
                c.ok(at >= prev, lp, "журнал должен идти по времени")
            prev = at


def check_plan_links(c: Check, plans: dict, details: dict) -> None:
    """`plan_link` под-запроса и `streams_summary` брифа сходятся с картой комнаты (§4)."""
    for name, detail in details.items():
        if not isinstance(detail, dict):
            continue
        plan = plans.get(detail.get("room"))
        link = detail.get("plan_link")
        if link is not None:
            where = f"{name}.plan_link"
            if plan is None:
                c.errors.append(f"{where}: у комнаты {detail.get('room')} нет карты — ссылка лишняя")
                continue
            stream = next((s for s in plan["streams"] if s.get("id") == link.get("stream_id")), None)
            if stream is None:
                c.errors.append(f"{where}: направления {link.get('stream_id')} нет в карте")
                continue
            c.ok(link.get("title") == stream.get("title"), where,
                 f"title {link.get('title')!r} != карта {stream.get('title')!r}")
            c.ok(link.get("status") == stream.get("status"), where,
                 f"status {link.get('status')!r} != карта {stream.get('status')!r}")
            c.ok(stream.get("request_id") == detail.get("id"), where,
                 "у направления request_id должен указывать на этот запрос")
        if detail.get("brief") is True:
            where = f"{name}.streams_summary"
            if not isinstance(plan, dict):
                c.errors.append(f"{where}: бриф без карты")
                continue
            if plan.get("brief_root_id") != detail.get("id"):
                c.errors.append(f"{where}: brief_root_id {plan.get('brief_root_id')} != {detail.get('id')}")
            summary = detail.get("streams_summary") or {}
            streams = plan.get("streams") or []
            c.ok(summary.get("total") == len(streams), where,
                 f"total {summary.get('total')} != {len(streams)}")
            c.ok(summary.get("done") == sum(1 for s in streams if s.get("status") == "done"), where,
                 "done не сходится с картой")
            c.ok(summary.get("running") == sum(1 for s in streams
                                               if s.get("status") in ("running", "waiting_quota")), where,
                 "running не сходится с картой (running + waiting_quota)")
            c.ok(summary.get("status") == plan.get("status"), where,
                 f"status {summary.get('status')!r} != карты {plan.get('status')!r}")


def expected_needs_you(d: dict) -> bool:
    """API.md §3.3: status == needs_you, ИЛИ неотвеченный вопрос Михаилу, ИЛИ карточка ждёт человека."""
    if d.get("status") == "needs_you":
        return True
    if any((q.get("for") or "mikhail") == "mikhail" and not q.get("answered")
           for q in d.get("open_questions") or []):
        return True
    return any(n.get("needs_human") for n in (d.get("tree") or [])[1:])


def check_detail(c: Check, d, where, summaries, model_index: dict | None = None):
    check_summary(c, d, where, ref_critic=False)
    if isinstance(d, dict) and isinstance(d.get("needs_you"), bool):
        c.ok(d["needs_you"] == expected_needs_you(d), where,
             "needs_you %s != %s (status, вопросы Михаилу, needs_human; done + вопрос → needs_you:true)"
             % (d["needs_you"], expected_needs_you(d)))
    for name, types in (("text", str), ("dispatcher", dict), ("synthesis", dict), ("warnings", list),
                        ("tree", list), ("timeline", list), ("next_steps", list), ("open_questions", list),
                        ("decisions", list), ("stage", dict), ("future_questions", list), ("artifacts", list),
                        ("report", dict)):
        c.field(d, where, name, types)
    check_critic_block(c, d.get("critic"), f"{where}.critic")
    # body_full — необязательное поле: полное тело карточки для отладки (D1).
    # Показывается, только если сервер его прислал; в фикстурах может не быть.
    if isinstance(d, dict) and "body_full" in d:
        c.field(d, where, "body_full", str)
    syn = d.get("synthesis") or {}
    c.enum(syn, f"{where}.synthesis", "state", SYNTH_STATES)
    c.field(syn, f"{where}.synthesis", "version", int)
    c.field(syn, f"{where}.synthesis", "text_md", str)
    c.field(syn, f"{where}.synthesis", "author", str, null=True)
    c.field(syn, f"{where}.synthesis", "at", int, null=True)
    c.field(syn, f"{where}.synthesis", "source_task", str, null=True)
    c.field(syn, f"{where}.synthesis", "legacy", bool)
    hist = syn.get("history")
    c.ok(isinstance(hist, list), f"{where}.synthesis", "history должен быть списком")
    if isinstance(hist, list) and hist:
        for i, ver in enumerate(hist):
            vp = f"{where}.synthesis.history[{i}]"
            c.field(ver, vp, "version", int)
            c.enum(ver, vp, "kind", {"initial", "revision"})
            c.field(ver, vp, "task_id", str, null=True)
            c.field(ver, vp, "author", str, null=True)
            c.field(ver, vp, "at", int, null=True)
            c.field(ver, vp, "verdict", str, null=True)
            c.field(ver, vp, "note", str)
        c.ok(hist[-1].get("version") == syn.get("version"), where,
             f"version {syn.get('version')} != последней истории {hist[-1].get('version')}")
        c.ok([h.get("version") for h in hist] == list(range(1, len(hist) + 1)), where,
             "версии в history должны идти 1..N")
    check_stage(c, d.get("stage"), f"{where}.stage")
    tree = d.get("tree") or []
    ids = [node.get("id") for node in tree if isinstance(node, dict)]
    if not tree:
        c.errors.append(f"{where}.tree: пустое дерево")
    else:
        c.ok(tree[0].get("role") == "root", where, "tree[0] должен быть корнем")
        c.ok(sum(1 for n in tree if n.get("role") == "root") == 1, where, "root должен быть один")
    c.ok(len(set(ids)) == len(ids), where, "id в tree повторяются")
    for i, node in enumerate(tree):
        np_ = f"{where}.tree[{i}]"
        c.field(node, np_, "id", str)
        c.field(node, np_, "title", str)
        c.field(node, np_, "assignee", str, null=True)
        c.enum(node, np_, "role", ROLES)
        c.enum(node, np_, "status", TASK_STATUSES)
        c.field(node, np_, "outside_room", bool)
        c.field(node, np_, "metadata", dict)
        c.field(node, np_, "comments", list)
        c.field(node, np_, "runs", list)
        c.field(node, np_, "needs_human", bool)
        check_model_label(c, node, np_, model_index or {})
        parents = node.get("parent_ids")
        if not isinstance(parents, list):
            c.errors.append(f"{np_}.parent_ids: ожидался список")
        else:
            for parent in parents:
                c.ok(parent in ids, np_, f"parent_id {parent} нет в дереве")
        if i == 0 and parents:
            c.ok(parents == [], np_, "у корня parent_ids должен быть []")
    src = syn.get("source_task")
    if src is not None:
        c.ok(src in ids, f"{where}.synthesis", f"source_task {src} нет в дереве")
    c.ok((d.get("progress") or {}).get("total") == len(tree) - 1, where,
         "progress.total должен равняться len(tree)-1")
    for i, art in enumerate(d.get("artifacts") or []):
        ap = f"{where}.artifacts[{i}]"
        c.field(art, ap, "path", str)
        c.field(art, ap, "title", str)
        c.enum(art, ap, "kind", ARTIFACT_KINDS)
        c.field(art, ap, "exists", bool)
        c.field(art, ap, "size", int, null=True)
        if art.get("exists") is False:
            c.ok(art.get("size") is None, ap, "size должен быть null, если файла нет")
    for i, warn in enumerate(d.get("warnings") or []):
        c.enum(warn, f"{where}.warnings[{i}]", "code", WARNING_CODES)
        c.field(warn, f"{where}.warnings[{i}]", "message", str)
    for i, ev in enumerate(d.get("timeline") or []):
        ep = f"{where}.timeline[{i}]"
        c.field(ev, ep, "at", int)
        c.enum(ev, ep, "kind", TIMELINE_KINDS)
        c.field(ev, ep, "task_id", str, null=True)
        c.field(ev, ep, "actor", str)
        c.field(ev, ep, "text", str)
    if isinstance(d, dict) and "thread" in d:
        check_thread(c, d["thread"], f"{where}.thread", ids)
    for i, step in enumerate(d.get("next_steps") or []):
        sp = f"{where}.next_steps[{i}]"
        c.field(step, sp, "id", str)
        c.field(step, sp, "text", str)
        c.field(step, sp, "owner", str, null=True)
        c.field(step, sp, "due", str, null=True)
        c.enum(step, sp, "state", BACKLOG_STATUSES)
    for i, q in enumerate(d.get("open_questions") or []):
        qp = f"{where}.open_questions[{i}]"
        c.field(q, qp, "id", str)
        c.field(q, qp, "for", str)
        c.field(q, qp, "answered", bool)
        c.field(q, qp, "answer", str, null=True)
    report = d.get("report") or {}
    c.field(report, f"{where}.report", "available", bool)
    c.field(report, f"{where}.report", "kind", str, null=True)
    c.field(report, f"{where}.report", "path", str, null=True)


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    verbose = "-v" in argv or "--verbose" in argv
    c = Check()
    files = sorted(p.name for p in FIX.glob("*.json"))
    data = {name: load(name) for name in files}
    c.checked = files
    catalog = data.get("models.json") or {}
    index = catalog_index(c, catalog)          # каталог моделей (WAVE10)

    health = data["health.json"]
    c.field(health, "health", "ok", bool)
    for block in ("app", "engine", "dispatcher", "data", "vault", "watcher"):
        c.field(health, "health", block, dict)
    c.field(health["app"], "health.app", "tz", str)
    c.field(health["app"], "health.app", "port", int)
    engine = health["engine"]
    for key in ("ok", "warning", "board", "db", "schema_ok", "schema_version", "tasks", "cli"):
        if key not in engine:
            c.errors.append(f"health.engine: нет поля {key}")
    c.enum(health["dispatcher"], "health.dispatcher", "state", {"running", "stopped", "stale", "unknown"})
    c.ok(not any("ok" in block and not isinstance(block, dict) for block in
                 (health["engine"], health["dispatcher"])), "health", "engine/dispatcher должны быть объектами")

    rooms = data["rooms.json"]["rooms"]
    for i, room in enumerate(rooms):
        check_summary_room(c, room, f"rooms.rooms[{i}]")

    for name in ("room_cafe.json", "room_fn.json"):
        room = data[name]
        wp = name.removesuffix(".json")
        c.field(room, wp, "id", str)
        c.field(room, wp, "tenant", str)
        c.ok(room.get("tenant") == "room:%s" % room.get("id"), wp, "tenant != room:<id>")
        c.ok(room.get("mode_default") == "auto", wp, "mode_default должен быть auto")
        c.field(room, wp, "max_rounds", int)
        c.field(room, wp, "seeded", bool)
        members = room.get("members")
        c.ok(isinstance(members, list), wp, "members должен быть списком")
        for i, member in enumerate(members or []):
            mp = f"{wp}.members[{i}]"
            c.field(member, mp, "profile", str)
            c.enum(member, mp, "role", {"worker", "critic", "observer"})
            c.field(member, mp, "note", str)
            c.field(member, mp, "pinned", bool)
            c.field(member, mp, "added_at", int)
        c.ok(room.get("orchestrator") not in {m.get("profile") for m in members or []}, wp,
             "руководитель комнаты не должен быть в members")
        check_project_dir(c, room, wp)
        check_room_settings(c, room, wp)
        check_agent_notes(c, room, wp)
        check_member_models(c, room, wp, index, catalog)

    details = {}
    for name in files:
        if name.startswith("request_") and name.endswith(".json"):
            details[name] = data[name]
    for name, detail in details.items():
        check_detail(c, detail, name.removesuffix(".json"), None, model_index=index)

    # --- карты проекта и их отражение в запросах (волна 9) ---
    plans = {}
    for name, room_id in (("plan_cafe.json", "cafe"), ("plan_fn.json", "fn")):
        plan = data.get(name)
        if plan is None:
            continue
        room = data.get("room_%s.json" % room_id) or {}
        ids = {d.get("id") for d in details.values() if d.get("room") == room_id}
        check_plan(c, plan, name.removesuffix(".json"), room, ids)
        plans[room_id] = plan
    check_plan_links(c, plans, details)

    for name in ("requests_cafe.json", "requests_fn.json"):
        feed = data[name]
        wp = name.removesuffix(".json")
        items = feed.get("items")
        c.ok(isinstance(items, list), wp, "items должен быть списком")
        for i, item in enumerate(items or []):
            check_summary(c, item, f"{wp}.items[{i}]")
        if isinstance(items, list):
            times = [i.get("created_at") for i in items]
            c.ok(times == sorted(times, reverse=True), wp, "лента должна идти по убыванию created_at")
            for item in items:
                detail = next((d for d in details.values() if d.get("id") == item.get("id")), None)
                if detail is None:
                    c.errors.append(f"{wp}: {item.get('id')} нет среди request_*.json")
                    continue
                c.ok(item.get("status") == detail.get("status"), f"{wp}.{item.get('id')}",
                     f"status {item.get('status')} != {detail.get('status')}")
                c.ok(item.get("critic", {}).get("state") == detail.get("critic", {}).get("state"),
                     f"{wp}.{item.get('id')}", "critic.state расходится с деталью")
                c.ok(item.get("needs_you") == detail.get("needs_you"), f"{wp}.{item.get('id')}",
                     f"needs_you {item.get('needs_you')} != {detail.get('needs_you')} в детали")

    for room_id, room_file in (("cafe", "room_cafe.json"), ("fn", "room_fn.json")):
        feed = data["requests_%s.json" % room_id]
        ids = {i.get("id") for i in feed.get("items") or []}
        for name, detail in details.items():
            if detail.get("room") == room_id and detail.get("id") not in ids:
                c.errors.append(f"requests_{room_id}.json: нет {detail.get('id')} из {name}")
        c.ok(all(i.get("room") == room_id for i in feed.get("items") or []), "requests_%s" % room_id,
             "room в ленте не совпадает")
        # counters.needs_you = запросы со статусом needs_you + done с needs_you:true (API.md §3.2)
        waiting = sum(1 for i in feed.get("items") or []
                      if i.get("status") == "needs_you" or (i.get("status") == "done" and i.get("needs_you")))
        in_rooms = next((r for r in rooms if r.get("id") == room_id), {})
        for label, counters in ((f"rooms.json[{room_id}]", in_rooms.get("counters")),
                                (room_file, data[room_file].get("counters"))):
            c.ok((counters or {}).get("needs_you") == waiting, label,
                 f"counters.needs_you {(counters or {}).get('needs_you')} != {waiting} (по ленте {room_id})")

    cand = data["candidates_cafe.json"]
    c.field(cand, "candidates_cafe", "count", int)
    c.ok(cand.get("count") == len(cand.get("items") or []), "candidates_cafe", "count != len(items)")
    for i, item in enumerate(cand.get("items") or []):
        cp = f"candidates_cafe.items[{i}]"
        for key in ("id", "title", "status", "assignee", "created_at", "children", "tenant"):
            if key not in item:
                c.errors.append(f"{cp}: нет поля {key}")
        if "task_id" in item:
            c.ok(item["task_id"] == item.get("id"), cp, "task_id должен совпадать с id")
        c.enum(item, cp, "status", TASK_STATUSES)

    road = data["roadmap_cafe.json"]
    c.field(road, "roadmap_cafe", "room", str)
    check_stage(c, {"current": (road.get("current") or {}).get("id"), "phases": road.get("phases")},
                "roadmap_cafe", with_updated=True)

    backlog = data["backlog_cafe.json"]
    items = backlog.get("items") or []
    c.field(backlog, "backlog_cafe", "counts", dict)
    for i, item in enumerate(items):
        bp = f"backlog_cafe.items[{i}]"
        c.field(item, bp, "id", str)
        c.enum(item, bp, "kind", BACKLOG_KINDS)
        c.field(item, bp, "text", str)
        c.field(item, bp, "owner", str, null=True)
        c.field(item, bp, "due", str, null=True)
        c.enum(item, bp, "status", BACKLOG_STATUSES)
        c.field(item, bp, "source_request", str)
        c.field(item, bp, "source_title", str)
        c.field(item, bp, "asked_request", str, null=True)
        if isinstance(item.get("id"), str):
            c.ok(":" in item["id"], bp, "id элемента должен быть <root>:<kind>_N")
    counts = backlog.get("counts") or {}
    c.ok(counts.get("total") == len(items), "backlog_cafe", "counts.total != len(items)")

    journal = data["journal_cafe.json"]
    for i, event in enumerate(journal.get("items") or []):
        jp = f"journal_cafe.items[{i}]"
        c.field(event, jp, "id", str)
        c.field(event, jp, "at", int)
        c.enum(event, jp, "kind", JOURNAL_KINDS)
        c.field(event, jp, "room", str)
        c.field(event, jp, "request_id", str, null=True)
        c.field(event, jp, "task_id", str, null=True)
        c.field(event, jp, "actor", str)
        c.field(event, jp, "text", str)
        c.field(event, jp, "detail", dict)

    for name in ("team_cafe.json", "team_fn.json"):
        wp = name.removesuffix(".json")
        c.field(data[name], wp, "dispatcher", dict)
        for i, member in enumerate(data[name].get("members") or []):
            mp = f"{wp}.members[{i}]"
            c.field(member, mp, "profile", str)
            c.enum(member, mp, "role", {"orchestrator", "worker", "critic", "observer"})
            c.enum(member, mp, "state", {"idle", "working", "waiting"})
            c.field(member, mp, "counts", dict)

    for name in ("reports_cafe.json",):
        for i, item in enumerate(data[name].get("items") or []):
            rp = f"{name}.items[{i}]"
            c.enum(item, rp, "kind", {"spec", "html"})
            c.field(item, rp, "key", str)
            c.field(item, rp, "path", str)

    for i, hit in enumerate(data["search.json"].get("items") or []):
        sp = f"search.items[{i}]"
        c.enum(hit, sp, "kind", {"request", "journal", "decision", "backlog", "report"})
        c.field(hit, sp, "title", str)
        c.field(hit, sp, "snippet", str)
        c.field(hit, sp, "score", (int, float))
        c.field(hit, sp, "route", str)

    for i, prof in enumerate(data["profiles.json"].get("items") or []):
        pp = f"profiles.items[{i}]"
        c.field(prof, pp, "profile", str)
        c.enum(prof, pp, "kind", {"director", "worker", "critic", "utility"})
        c.field(prof, pp, "tags", list)
        c.field(prof, pp, "available", bool)

    cfg = data["config.json"]
    for key in ("app", "paths", "allowed_roots", "limits", "features"):
        c.field(cfg, "config", key, list if key == "allowed_roots" else dict)

    spec = data["report_unit_economics.json"]
    c.ok(spec.get("version") == 1, "report_unit_economics", "version должен быть 1")
    c.ok(isinstance(spec.get("blocks"), list) and spec["blocks"], "report_unit_economics",
         "blocks должен быть непустым списком")

    if verbose:
        print("проверено файлов: %d" % len(c.checked))
        for name in c.checked:
            print("  •", name)
    if c.errors:
        print("РАСХОЖДЕНИЯ (%d):" % len(c.errors))
        for line in c.errors:
            print("  -", line)
        return 1
    print("fixtures=%d ok — все обязательные поля, типы, enum и связи сходятся с API.md" % len(c.checked))
    return 0


def check_summary_room(c: Check, room, where):
    need = {"id", "title", "icon", "archived", "orchestrator", "critic", "members_count", "mode_default",
            "critic_pass", "max_rounds", "counters", "phase", "updated_at"}
    if not isinstance(room, dict) or not need <= set(room):
        c.errors.append(f"{where}: нет полей RoomSummary {sorted(need - set(room or {}))}")
        return
    rid = c.field(room, where, "id", str)
    if rid and not ROOM_ID.match(rid):
        c.errors.append(f"{where}.id: {rid!r} не слаг комнаты")
    c.field(room, where, "title", str)
    c.field(room, where, "archived", bool)
    c.field(room, where, "orchestrator", str)
    c.field(room, where, "critic", str, null=True)
    c.field(room, where, "members_count", int)
    c.ok(room.get("mode_default") == "auto", where, "mode_default должен быть auto")
    c.field(room, where, "critic_pass", bool)
    c.field(room, where, "max_rounds", int)
    counters = room.get("counters")
    if not isinstance(counters, dict) or not {"working", "needs_you", "done_today"} <= set(counters):
        c.errors.append(f"{where}.counters: нужны working/needs_you/done_today")
    c.field(room, where, "phase", dict, null=True)
    c.field(room, where, "updated_at", int)
    if "project_dir" in room:      # необязательное поле волны 8 (Д2)
        c.field(room, where, "project_dir", str, null=True)


if __name__ == "__main__":
    raise SystemExit(main())
