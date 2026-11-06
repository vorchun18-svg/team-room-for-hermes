#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Сверка ЖИВОГО бэкенда с контрактом: `fixtures/*.json` + `docs/API.md`.

Что делает:

1. поднимает песочницу (`tests.helpers.Sandbox`) и настоящий сервер
   (`server.app.create_app(cfg, start_watcher=False)`) в потоке на свободном порту;
2. доводит песочницу до состояния, в котором фикстуры имеют смысл: регистрирует
   посеянные деревья в реестре запросов (как это сделало бы приложение), добавляет
   деревья `fn` (ребёнок вне состава) и `critic_approved`, прогоняет один тик
   watcher-а (ingest → дорожная карта, бэклог, журнал, поиск, отчёт);
3. для каждой фикстуры делает живой GET и рекурсивно сверяет СТРУКТУРУ:
   каждый ключ фикстуры должен быть в ответе с тем же JSON-типом
   (`null` в фикстуре → допустим любой тип; пустой список в фикстуре → элементы
   не сверяются; непустой → сверяется первый элемент живого ответа);
   значения `status/state/role/kind/…` проверяются на принадлежность наборам из
   `docs/API.md`; лишние ключи живого ответа — только предупреждение;
4. проверяет POST/PATCH/DELETE-эндпоинты (создание запроса, действия
   comment/answer/revise/reassign, бэклог status/ask, участники, создание комнаты,
   adopt) — коды ответов и формы по `docs/API.md`;
5. печатает читаемый отчёт и возвращает 0 (чисто) или 1 (есть нарушения).

    python tools/check_conformance.py            # полный прогон
    python tools/check_conformance.py -v         # + лишние ключи и список фикстур
    python tools/check_conformance.py --no-posts # только GET по фикстурам

Реальная доска не трогается: всё пишется в песочницу (`TEAMROOM_*`), а слепок
`tests.helpers.snapshot_real_db` до/после доказывает невмешательство.
"""
from __future__ import annotations

import argparse
import json
import socket
import sys
import threading
import time
from pathlib import Path
from urllib import error as urlerror
from urllib import request as urlrequest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

FIX = ROOT / "fixtures"

# ------------------------------------------------------------------ наборы enum
# docs/API.md §3.3–3.5, §4, §6.
REQUEST_STATUSES = {"queued", "working", "reviewing", "synthesizing", "done", "needs_you", "failed"}
TASK_STATUSES = {"triage", "todo", "ready", "running", "review", "blocked", "scheduled", "done", "archived"}
CRITIC_STATES = {"off", "waiting", "reviewing", "approved", "revised", "failed"}
SYNTH_STATES = {"ready", "pending", "none"}
TREE_ROLES = {"worker", "critic", "synthesizer", "root", "revision"}
MEMBER_ROLES = {"worker", "critic", "observer"}
TEAM_ROLES = {"orchestrator", "worker", "critic", "observer"}
MEMBER_STATES = {"idle", "working", "waiting"}
PHASE_STATUSES = {"done", "active", "next", "blocked"}
ARTIFACT_KINDS = {"md", "json", "html", "csv", "image", "pdf", "docx", "xlsx", "pptx", "other"}
WARNING_CODES = {"legacy_metadata", "missing_artifact", "no_synthesis", "report_invalid", "schema_drift",
                 "partial_ingest", "dispatcher_down", "card_failed", "outside_room", "critic_failed",
                 "critic_rounds_exhausted"}
TIMELINE_KINDS = {"created", "decomposed", "promoted", "claimed", "started", "commented", "completed",
                  "blocked", "unblocked", "failed", "archived", "heartbeat"}
JOURNAL_KINDS = {"request_created", "request_status", "request_done", "step_added", "step_done",
                 "question_open", "question_answered", "decision_proposed", "decision_accepted",
                 "report_added", "artifact_added", "member_added", "member_removed", "room_created",
                 "room_archived", "ingest", "error", "model_changed", "model_restarted"}   # WAVE10
BACKLOG_KINDS = {"next_step", "future_question", "open_question", "decision"}
BACKLOG_STATUSES = {"open", "asked", "done", "dismissed"}
SEARCH_KINDS = {"request", "journal", "decision", "backlog", "report"}
REPORT_KINDS = {"spec", "html"}
PROFILE_KINDS = {"director", "worker", "critic", "utility"}
DECISION_STATUSES = {"proposed", "accepted", "rejected"}
DISPATCHER_STATES = {"running", "stopped", "stale", "unknown"}
VERDICTS = {"approve", "revise"}
#: WAVE10 §19: допустимые уровни мышления и источники настройки модели участника
MODEL_EFFORTS = {"none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra"}
MODEL_SOURCES = {"room", "agent"}

REQUEST_ENUMS = {
    "status": REQUEST_STATUSES,
    "tree[].status": TASK_STATUSES,
    "tree[].role": TREE_ROLES,
    "critic.state": CRITIC_STATES,
    "critic.verdict": VERDICTS,
    "synthesis.state": SYNTH_STATES,
    "synthesis.history[].kind": {"initial", "revision"},
    "synthesis.history[].verdict": VERDICTS,
    "warnings[].code": WARNING_CODES,
    "timeline[].kind": TIMELINE_KINDS,
    "artifacts[].kind": ARTIFACT_KINDS,
    "stage.phases[].status": PHASE_STATUSES,
    "next_steps[].state": BACKLOG_STATUSES,
    "decisions[].status": DECISION_STATUSES,
    "report.kind": REPORT_KINDS,
}
FEED_ENUMS = {"status": REQUEST_STATUSES, "critic.state": CRITIC_STATES, "critic.verdict": VERDICTS}
JOURNAL_ENUMS = {"items[].kind": JOURNAL_KINDS}
BACKLOG_ENUMS = {"items[].kind": BACKLOG_KINDS, "items[].status": BACKLOG_STATUSES}
SEARCH_ENUMS = {"items[].kind": SEARCH_KINDS}
REPORTS_ENUMS = {"items[].kind": REPORT_KINDS}
TEAM_ENUMS = {"members[].role": TEAM_ROLES, "members[].state": MEMBER_STATES}
PROFILES_ENUMS = {"items[].kind": PROFILE_KINDS}
ROOMS_ENUMS = {"rooms[].phase.status": PHASE_STATUSES}
ROOM_ENUMS = {"phase.status": PHASE_STATUSES, "members[].role": MEMBER_ROLES}
CANDIDATES_ENUMS = {"items[].status": TASK_STATUSES}
HEALTH_ENUMS = {"dispatcher.state": DISPATCHER_STATES, "warnings[].code": WARNING_CODES}
NO_ENUMS: dict = {}

# JSON-типы: bool отдельно от int (в Python bool — подкласс int).
JSON_TYPES = {bool: "bool", int: "int", float: "number", str: "string", list: "list", dict: "dict",
              type(None): "null"}

# Поля, которые docs/API.md §3 объявляет nullable: живой `null` в них — законный ответ,
# а не расхождение (фикстура просто показывает заполненный демо-пример).
NULLABLE = {"status_reason", "started_at", "finished_at", "duration_s", "assignee", "verdict",
            "author", "at", "source_task", "task_id", "owner", "due", "answer", "answered_at",
            "why", "current", "last", "phase", "warning", "last_run_at", "last_error",
            "schema_version", "request_id", "tenant", "decomposed_at", "size", "outcome",
            "error", "note", "asked_request", "status_reason"}


def jtype(value) -> str:
    for kind, name in JSON_TYPES.items():
        if kind is not None and isinstance(value, kind):
            if kind is float and isinstance(value, bool):
                continue
            return name
    return type(value).__name__


def same_type(expected, actual) -> bool:
    """JSON-тип совпадает; number (int/float) считается одним типом."""
    if expected == actual:
        return True
    return {expected, actual} <= {"int", "number"}


# ------------------------------------------------------------------- нарушения
class Violations:
    """Нарушения по одной сверке: missing / type (+ предупреждения empty / extra).

    `missing` и `type` — контракт нарушен (exit 1). `empty` — в фикстуре список
    непустой, а живой ответ пуст: элементы сверить нельзя, но это зависит от данных
    песочницы, а не от формы ответа, поэтому предупреждение. `extra` — лишние ключи.
    """

    def __init__(self, label: str):
        self.label = label
        self.missing: list[str] = []
        self.type: list[str] = []
        self.empty: list[str] = []
        self.extra: list[str] = []
        self.pending: list[str] = []     # поле фикстуры, которого на сервере ещё нет (волна 8)

    @property
    def failed(self) -> bool:
        return bool(self.missing or self.type)

    @property
    def count(self) -> int:
        return len(self.missing) + len(self.type)

    def line(self) -> str:
        parts = []
        for name, items in (("missing", self.missing), ("type", self.type)):
            if items:
                parts.append("%s: %s" % (name, ", ".join(items[:12]) + (" …" if len(items) > 12 else "")))
        notes = []
        if self.empty:
            notes.append("пусто: %s" % ", ".join(self.empty[:8]))
        if self.extra:
            notes.append("extra: %d" % len(self.extra))
        if self.pending:
            notes.append("ещё не реализовано: %s" % ", ".join(self.pending[:8]))
        text = "; ".join(parts)
        if notes:
            text += (" | " if text else "") + "; ".join(notes)
        return text


def _suffixes(path: list) -> list[str]:
    """Возможные суффиксы пути для поиска правила enum: «a[].b», «[].b», «b»."""
    parts = [str(p) if not isinstance(p, int) else "[]" for p in path]
    out = []
    for start in range(len(parts)):
        suffix = ".".join(parts[start:])
        suffix = suffix.replace(".[]", "[]")
        out.append(suffix)
    return out


# Поля волны 8, которые фикстуры уже показывают, а сервер ещё может не отдавать:
# RequestDetail.thread (блок A/Д1). Пока THREAD-BE не готов — это предупреждение,
# а не нарушение контракта (docs/WAVE8-RULES.md, «Интерфейсы»).
# ВОЛНА 9: бриф/карта (`brief`, `streams_summary`, `plan_link` — PLAN-BE/PLAN-CONTRACT)
# и «особые указания участникам» (`agent_notes` — AGENT-NOTES) включены в фикстуры
# раньше, чем их отдаёт живой сервер: до готовности — «ещё не реализовано», не нарушение
# (docs/WAVE9-RULES.md §7: «новые поля необязательны до готовности всех»).
# ВОЛНА 10: «модель участника» (`model_settings`, `agent_default`, `agent_models`, `model_label`)
# появляется в фикстурах раньше живой реализации (MODEL-BE/MODEL-FE) — до готовности
# это «ещё не реализовано», а не нарушение контракта (docs/WAVE10-RULES.md §18–21).
PENDING_FIELDS = {"thread", "brief", "streams_summary", "plan_link", "agent_notes",
                  "model_settings", "agent_default", "agent_models", "model_label"}


def compare(fix, live, path: list, enums: dict, out: Violations, shallow: set | None = None) -> None:
    """Рекурсивная сверка структуры: ключи/типы фикстуры против живого ответа.

    `shallow` — суффиксы путей, где вложенные ключи не сверяются, потому что их
    состав определяет не контракт, а данные: `metadata` карточки (сырая metadata
    run'а), `detail` события журнала (свой набор на каждый kind), блоки отчёта.
    """
    here = ".".join(str(p) for p in path) or "<root>"
    if fix is None:
        return  # null в фикстуре: допустим любой тип
    if live is None:
        key = str(path[-1]) if path else ""
        if key in NULLABLE:
            out.empty.append(here + " (null)")
            return
        out.type.append("%s: fixture %s, live null" % (here, jtype(fix)))
        return
    expected = jtype(fix)
    actual = jtype(live)
    if not same_type(expected, actual):
        out.type.append("%s: fixture %s, live %s" % (here, expected, actual))
        return
    if shallow and any(here.endswith(s) for s in shallow):
        return
    if isinstance(fix, dict):
        if not isinstance(live, dict):
            return
        for key, value in fix.items():
            if key not in live:
                if key in PENDING_FIELDS:
                    out.pending.append(("%s.%s" % (here, key)) if path else key)
                    continue
                out.missing.append("%s.%s" % (here, key) if path else key)
                continue
            if key in PENDING_FIELDS:
                # Поле волны 8/9: сервер уже знает ключ, но данных песочницы может не быть
                # (`null`/пустой объект) — сверяем только тип, состав не требуем.
                actual_type = jtype(live[key])
                if jtype(value) == actual_type:
                    if live[key] in (None, {}, []):
                        out.pending.append(("%s.%s" % (here, key)) if path else key)
                    continue
                out.pending.append(("%s.%s" % (here, key)) if path else key)
                continue
            compare(value, live[key], path + [key], enums, out, shallow)
        for key in live:
            if key not in fix and key not in OPTIONAL_EXTRA:
                out.extra.append("%s.%s" % (here, key) if path else key)
        return
    if isinstance(fix, list):
        if not isinstance(live, list):
            return
        if not fix:
            return  # пустой список в фикстуре: элементы не сверяем
        if shallow and any(here.endswith(s) for s in shallow):
            return
        if not live:
            out.empty.append(here)
            return
        compare(fix[0], live[0], path + [0], enums, out, shallow)
        return
    if isinstance(fix, str):
        for suffix in _suffixes(path):
            allowed = enums.get(suffix)
            if allowed is None:
                continue
            if live not in allowed:
                out.type.append("%s: %r вне допустимых %s" % (here, live, sorted(allowed)))
            break


# ------------------------------------------------------------------- песочница
def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _write_profiles(hermes_home: Path) -> None:
    """Минимальный ростер в ФАЛЬШИВОМ каталоге Hermes внутри песочницы.

    ВАЖНО: `cfg.hermes_home` — РЕАЛЬНЫЙ каталог Hermes (только чтение). Писать туда нельзя:
    28.10.2026 версия без этой защиты затёрла config.yaml и профили biz-director/biz-critic/coder.
    Все записи идут через `server.safety` (двойная защита: проверка пути + рантайм-запрет).
    """
    from server import safety

    real = safety.real_hermes_home().resolve()
    target = hermes_home.resolve()
    if target == real or real in target.parents or target in real.parents:
        raise RuntimeError("отказ: _write_profiles вызван на реальном каталоге Hermes: %s" % target)
    safety.assert_not_real(hermes_home, "ростер профилей Hermes")
    safety.guarded_mkdir(hermes_home, parents=True, exist_ok=True)
    # Состояние движка (pid диспетчера и т.п.) читается из gateway_state.json — копия реального, только чтение.
    gs = real / "gateway_state.json"
    if gs.is_file():
        safety.guarded_copy2(gs, hermes_home / "gateway_state.json")
    safety.guarded_write_text(hermes_home / "config.yaml",
                              "model:\n  default: test-model\n  provider: test\n")
    roster = {
        "biz-director": ("Директор Лето", "director", "Разбирает запрос и сводит ответ.", ["декомпозиция"]),
        "biz-critic": ("Критик", "critic", "Проверяет свод перед ответом.", ["проверка"]),
        "coder": ("Программист", "worker", "Пишет и проверяет код.", ["python"]),
        "biz-lawyer": ("Юрист проекта", "worker", "Проверяет договоры и риски.", ["право"]),
        "biz-cfo": ("Финансист Лето", "worker", "Считает юнит-экономику.", ["финансы"]),
        "biz-ops": ("Операции", "worker", "Ведёт поставщиков и упаковку.", ["операции"]),
        "biz-marketing": ("Маркетолог", "worker", "Готовит витрину и тексты.", ["маркетинг"]),
        "biz-strategy": ("Стратег", "worker", "Сравнивает рынок и цены.", ["стратегия"]),
        "biz-runner": ("Гонец Лето", "worker", "Забирает и проверяет файлы.", ["сбор"]),
    }
    for name, (title, kind, description, tags) in roster.items():
        folder = hermes_home / "profiles" / name
        folder.mkdir(parents=True, exist_ok=True)
        safety.guarded_write_text(folder / "config.yaml",
                                  "model:\n  default: test-model\n  provider: test\n")
        safety.guarded_write_text(
            folder / "profile.yaml",
            "title: %s\nkind: %s\ndescription: %s\ntags: [%s]\n"
            % (title, kind, description, ", ".join(tags)))


def _make_card(agents, title: str, assignee: str, tenant: str, parent: str | None = None,
               summary: str = "", metadata: dict | None = None) -> str:
    card = agents.card(title, assignee, status="todo", tenant=tenant, parent=parent)
    agents.start(card)
    agents.finish(card, summary or "Готово", metadata or {})
    return card


def _meta(sb, owner: str = "biz-ops") -> dict:
    """Metadata v1 для деревьев, которых нет в посеве (как у реального ответа директора)."""
    state_md = Path(sb.vault_dir) / "Лето" / "STATE.md"
    return {
        "next_steps": [{"text": "Согласовать спеку упаковки с поставщиком", "owner": owner,
                        "due": "2026-11-20"},
                       {"text": "Свести смету в один файл", "owner": owner, "due": None}],
        "open_questions": [],
        "decisions": [{"text": "Стартовать с набора из 6 моти", "why": "упаковка на набор дешевле",
                       "status": "accepted"}],
        "stage": {"packaging": "в работе: смета собрана", "production": "не начат"},
        "future_questions": ["Стоит ли выходить в B2B с наборами для кофеен?"],
        "artifacts": [{"path": str(state_md).replace("\\", "/"), "title": "STATE.md", "kind": "md"}],
    }


def _extra_trees(sb) -> dict:
    """Деревья, которых нет в посеве, но которые есть в фикстурах.

    * `outside_room` — комната `fn`: ребёнок назначен профилю вне состава;
    * `critic_approved` — комната `cafe`: ответ принят критиком (одна версия).
    """
    from tests.helpers import SimAgents

    agents = SimAgents(sb)
    made: dict = {}

    root = agents.card("Участок под базу: зона, обременения, доступ к воде", "default", tenant="room:fn")
    kids = [
        agents.card("Зона и категория земли", "lawyer", tenant="room:fn", parent=root),
        agents.card("Сверка с кадастром Росреестр", "analyst", tenant="room:fn", parent=root),
        agents.card("Доступ к воде и ЛЭП", "surveyor", tenant="room:fn", parent=root),
        agents.card("Карта участка и схема коммуникаций", "coder", tenant="room:fn", parent=root),
    ]
    agents.start(root)
    for kid in kids:
        agents.start(kid)
        agents.finish(kid, "Срез готов.")
    agents.finish(root, "## Участок\n\nТри риска и четыре запроса.", _meta(sb, "surveyor"))
    made["outside_room"] = {"root_id": root, "slug": "ext-fn-outside", "room": "fn", "critic": False,
                            "extra_cards": []}

    root = agents.card("Лето: состав набора и себестоимость — сверка с критиком", "biz-director",
                       tenant="room:cafe")
    kids = [_make_card(agents, "Срез 01-Legal: этикетка набора", "biz-lawyer", "room:cafe", root),
            _make_card(agents, "Срез 02-Ops: смета упаковки", "biz-ops", "room:cafe", root),
            _make_card(agents, "Срез 03-CFO: юнит-экономика набора", "biz-cfo", "room:cafe", root)]
    agents.start(root)
    agents.finish(root, "## Свод\n\nСебестоимость набора сходится, маржа 38%.", _meta(sb, "biz-ops"))
    critic = agents.card("Критика ответа (раунд 1)", "biz-critic", tenant="room:cafe", parent=root)
    agents.start(critic)
    agents.finish(critic, "Принято: цифры сходятся.", {"verdict": "approve", "issues": []})
    made["critic_approved"] = {"root_id": root, "slug": "ext-cafe-approved", "room": "cafe", "critic": True,
                               "extra_cards": [{"id": critic, "kind": "critic", "round": 1}]}
    return made


def _seed_registry(sb, extra: dict) -> None:
    """Записать посеянные деревья в реестр запросов — как это делает приложение."""
    from server import registry, rooms

    for room_id in ("cafe", "fn"):
        room = rooms.get_room(sb.cfg, room_id)
        if room:
            registry_path = registry.registry_path(sb.cfg, room_id)
            if registry_path.exists():
                registry_path.unlink()
    for name, info in sb.ids.items():
        if not isinstance(info, dict) or "root_id" not in info or name == "candidate":
            continue
        row = sb.one("SELECT * FROM tasks WHERE id = ?", (info["root_id"],))
        if row is None:
            continue
        cards = []
        for kind, card_id in (info.get("extra_cards") or {}).items():
            if kind in ("critic", "revision") and isinstance(card_id, str):
                cards.append({"id": card_id, "kind": kind, "round": 1, "at": row["created_at"] + 60})
        registry.add_request(sb.cfg, "cafe", {
            "root_id": info["root_id"], "slug": info["slug"], "title": row["title"],
            "text": row["body"], "created_at": row["created_at"],
            "critic": name != "done_legacy", "mode": "auto", "adopted": False,
            "extra_cards": cards, "retargeted": True, "ingested": {},
        })
    for name, info in extra.items():
        row = sb.one("SELECT * FROM tasks WHERE id = ?", (info["root_id"],))
        registry.add_request(sb.cfg, info["room"], {
            "root_id": info["root_id"], "slug": info["slug"], "title": row["title"],
            "text": row["body"], "created_at": row["created_at"], "critic": bool(info["critic"]),
            "mode": "auto", "adopted": False, "retargeted": True, "ingested": {},
            "extra_cards": info["extra_cards"],
        })


# ------------------------------------------------------------------- HTTP-клиент
class Live:
    """Живой сервер на песочнице: контекстный менеджер + GET/POST."""

    def __init__(self, posts: bool = True):
        self.posts = posts
        self.sb = None
        self.server = None
        self.thread = None
        self.base = ""
        self.extra: dict = {}
        self.before: dict = {}

    def __enter__(self) -> "Live":
        from tests.helpers import Sandbox

        self.sb = Sandbox()
        self.sb.__enter__()
        try:
            self.before = self.sb.snapshot_real()
            import dataclasses
            fake_home = Path(self.sb.root) / "fake_hermes_home"      # внутри песочницы, не реальный
            _write_profiles(fake_home)
            self.cfg = dataclasses.replace(self.sb.cfg, hermes_home=fake_home)
            self.extra = _extra_trees(self.sb)
            _seed_registry(self.sb, self.extra)
            import server.app as app_mod
            self.server = app_mod.create_app(self.cfg, start_watcher=False, port=0)
            self.base = "http://127.0.0.1:%d" % self.server.server_address[1]
            self.thread = threading.Thread(target=self.server.serve_forever,
                                           kwargs={"poll_interval": 0.2}, daemon=True)
            self.thread.start()
            self.server.http_app.watcher.tick_once()   # ingest: карта, бэклог, журнал, отчёт
        except Exception:
            self._stop()
            self.sb.__exit__(None, None, None)
            self.sb = None
            raise
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        self._stop()
        if self.sb is not None:
            if not exc_type:
                self.sb.assert_real_untouched(self.before)
            self.sb.__exit__(exc_type, exc, tb)
            self.sb = None
        return False

    def _stop(self) -> None:
        if self.server is not None:
            try:
                self.server.shutdown()
                self.server.server_close()
            except Exception:  # noqa: BLE001
                pass
            self.server = None
        if self.thread is not None:
            self.thread.join(timeout=5)
            self.thread = None

    # --- запросы ---
    def _call(self, method: str, path: str, body: dict | None = None) -> tuple:
        data = None
        headers = {"Accept": "application/json"}
        if body is not None:
            data = json.dumps(body, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json"
        req = urlrequest.Request(self.base + path, data=data, headers=headers, method=method)
        try:
            with urlrequest.urlopen(req, timeout=120) as resp:
                raw = resp.read()
                return resp.status, _payload(raw, resp.headers), dict(resp.headers)
        except urlerror.HTTPError as exc:
            raw = exc.read()
            payload, headers = _payload(raw, exc.headers), dict(exc.headers)
            exc.close()
            return exc.code, payload, headers
        except (urlerror.URLError, OSError) as exc:
            raise RuntimeError("живой сервер недоступен: %s (%s)" % (path, exc)) from exc

    def get(self, path: str) -> tuple:
        return self._call("GET", path)

    def post(self, path: str, body: dict | None = None) -> tuple:
        return self._call("POST", path, body if body is not None else {})

    def patch(self, path: str, body: dict) -> tuple:
        return self._call("PATCH", path, body)

    def delete(self, path: str) -> tuple:
        return self._call("DELETE", path)

    # --- данные песочницы ---
    def root_of(self, state: str) -> str:
        if state in self.extra:
            return self.extra[state]["root_id"]
        return self.sb.ids[state]["root_id"]


def _payload(raw: bytes, headers):
    ctype = ""
    if headers is not None:
        ctype = headers.get("Content-Type", "") or ""
    if "json" in ctype or (raw[:1] in (b"{", b"[")):
        try:
            return json.loads(raw)
        except ValueError:
            pass
    return raw


# --------------------------------------------------------------- карта фикстур
STATE_FIXTURES = {
    "request_queued.json": "queued",
    "request_working.json": "working",
    "request_reviewing.json": "reviewing",
    "request_needs_you.json": "needs_you",
    "request_done.json": "done_modern",
    "request_critic_approved.json": "critic_approved",
    "request_done_legacy.json": "done_legacy",
    "request_failed.json": "failed",
    "request_outside_room.json": "outside_room",
}


def _fixture_requests(live: Live) -> list[dict]:
    """Список сверок GET-фикстур: (файл, путь, enums, shallow)."""
    out = [
        {"file": "health.json", "path": "/api/health", "enums": HEALTH_ENUMS},
        {"file": "config.json", "path": "/api/config", "enums": NO_ENUMS},
        {"file": "profiles.json", "path": "/api/profiles", "enums": PROFILES_ENUMS},
        {"file": "rooms.json", "path": "/api/rooms", "enums": ROOMS_ENUMS},
        # W17-B §1: одно чтение на всё боковое меню (счётчики + последние запросы)
        {"file": "rooms_summary.json", "path": "/api/rooms/summary", "enums": ROOMS_ENUMS},
        {"file": "room_cafe.json", "path": "/api/rooms/cafe", "enums": ROOM_ENUMS},
        {"file": "room_fn.json", "path": "/api/rooms/fn", "enums": ROOM_ENUMS},
        {"file": "requests_cafe.json", "path": "/api/rooms/cafe/requests", "enums": FEED_ENUMS},
        {"file": "requests_fn.json", "path": "/api/rooms/fn/requests", "enums": FEED_ENUMS},
        {"file": "candidates_cafe.json", "path": "/api/rooms/cafe/candidates", "enums": CANDIDATES_ENUMS},
        {"file": "team_cafe.json", "path": "/api/rooms/cafe/team", "enums": TEAM_ENUMS},
        {"file": "team_fn.json", "path": "/api/rooms/fn/team", "enums": TEAM_ENUMS},
        {"file": "roadmap_cafe.json", "path": "/api/rooms/cafe/roadmap", "enums": {"phases[].status": PHASE_STATUSES,
                                                                                "current.status": PHASE_STATUSES}},
        {"file": "backlog_cafe.json", "path": "/api/rooms/cafe/backlog", "enums": BACKLOG_ENUMS},
        {"file": "journal_cafe.json", "path": "/api/rooms/cafe/journal", "enums": JOURNAL_ENUMS},
        {"file": "reports_cafe.json", "path": "/api/rooms/cafe/reports", "enums": REPORTS_ENUMS},
        {"file": "search.json", "path": "/api/search?q=%s", "enums": SEARCH_ENUMS, "q": True},
    ]
    for name, state in STATE_FIXTURES.items():
        out.append({"file": name, "path": "/api/requests/" + live.root_of(state), "enums": REQUEST_ENUMS})
    return out


def _report_spec_check(live: Live) -> dict:
    status, payload, _headers = live.get("/api/rooms/cafe/reports")
    key = ""
    if status == 200 and isinstance(payload, dict) and payload.get("items"):
        key = str((payload["items"][0] or {}).get("key") or "")
    return {"file": "report_unit_economics.json", "path": "/api/reports/cafe/" + (key or "none"),
            "enums": NO_ENUMS, "shallow": {"blocks"}}


def _preview_check(live: Live) -> Violations:
    """`GET /api/dispatcher/preview` (W17-B §1): чтение доски без POST и без CLI."""
    out = Violations("GET /api/dispatcher/preview")
    status, payload, _headers = live.get("/api/dispatcher/preview")
    if status != 200:
        out.type.append("HTTP %s вместо 200: %s" % (status, str(payload)[:160]))
        return out
    if not isinstance(payload, dict):
        out.type.append("ответ не объект")
        return out
    for key in ("ok", "preview", "report", "at"):
        if key not in payload:
            out.type.append("нет поля %s" % key)
    if payload.get("preview") is not True:
        out.type.append("preview != true")
    if not isinstance(payload.get("report"), dict):
        out.type.append("report не объект")
    return out


def _catalog_check(live: Live) -> tuple[Violations, str]:
    """`GET /api/models` (WAVE10 §19): форма каталога моделей.

    Пока MODEL-BE не подключил маршрут, это «ещё не реализовано», а не нарушение:
    фикстуры волны 10 обязательны лишь после готовности всех (WAVE10 §17–19).
    """
    out = Violations("GET /api/models")
    status, payload, _headers = live.get("/api/models")
    if status in (404, 405, 501):
        return out, "каталог моделей ещё не реализован (HTTP %s)" % status
    if status != 200:
        out.type.append("HTTP %s вместо 200: %s" % (status, str(payload)[:160]))
        return out, ""
    if not isinstance(payload, dict):
        out.type.append("catalog: ожидался объект")
        return out, ""
    _shape(payload, {"providers": list, "efforts": list, "source": str}, "catalog", out)
    for key, kind in (("cached", bool), ("updated_at", int), ("warnings", list)):
        if key in payload and not isinstance(payload[key], kind):
            out.type.append("catalog.%s: ожидался %s" % (key, kind.__name__))
    efforts = payload.get("efforts")
    if isinstance(efforts, list):
        if not efforts:
            out.type.append("catalog.efforts: пустой список")
        ids = []
        for i, effort in enumerate(efforts):
            where = "catalog.efforts[%s]" % i
            if not isinstance(effort, dict):
                out.type.append("%s: ожидался объект {id, label}" % where)
                continue
            _shape(effort, {"id": str, "label": str}, where, out)
            if isinstance(effort.get("id"), str):
                ids.append(effort["id"])
                if effort["id"] not in MODEL_EFFORTS:
                    out.type.append("%s.id: %r вне допустимых %s"
                                    % (where, effort["id"], sorted(MODEL_EFFORTS)))
        if len(set(ids)) != len(ids):
            out.type.append("catalog.efforts: id повторяются")
    providers = payload.get("providers")
    if isinstance(providers, list):
        if not providers:
            out.type.append("catalog.providers: пустой список")
        ids = []
        for i, provider in enumerate(providers):
            where = "catalog.providers[%s]" % i
            _shape(provider, {"id": str, "label": str, "configured": bool, "models": list}, where, out)
            if not isinstance(provider, dict):
                continue
            ids.append(provider.get("id"))
            models = provider.get("models")
            if isinstance(models, list):
                for j, model in enumerate(models):
                    _shape(model, {"id": str, "label": str}, "%s.models[%s]" % (where, j), out)
            elif models is not None:
                out.type.append("%s.models: ожидался список" % where)
        if len(set(ids)) != len(ids):
            out.type.append("catalog.providers: id провайдеров повторяются")
    return out, ""


# ------------------------------------------------------------------ POST-проверки
SUMMARY_REQUIRED = {"id": str, "room": (str, type(None)), "title": str, "status": str, "mode": str,
                    "critic": dict, "created_at": int, "progress": dict, "needs_you": bool,
                    "preview": str, "archived": bool}


def _shape(payload, spec: dict, where: str, out: Violations) -> None:
    if not isinstance(payload, dict):
        out.type.append("%s: ожидался объект, получено %s" % (where, jtype(payload)))
        return
    for key, kind in spec.items():
        if key not in payload:
            out.missing.append("%s.%s" % (where, key))
            continue
        value = payload[key]
        kinds = kind if isinstance(kind, tuple) else (kind,)
        if isinstance(value, bool) and bool not in kinds:
            out.type.append("%s.%s: ожидался %s, получено bool" % (where, key, kind))
        elif not isinstance(value, kinds):
            out.type.append("%s.%s: ожидался %s, получено %s" % (where, key, kind, jtype(value)))


def _check_summary(payload, where: str, out: Violations) -> None:
    _shape(payload, SUMMARY_REQUIRED, where, out)
    critic = payload.get("critic")
    if isinstance(critic, dict):
        _shape(critic, {"enabled": bool, "state": str, "verdict": (str, type(None)), "round": int},
               where + ".critic", out)
        if critic.get("state") not in CRITIC_STATES:
            out.type.append("%s.critic.state: %r вне допустимых" % (where, critic.get("state")))
    if payload.get("status") not in REQUEST_STATUSES:
        out.type.append("%s.status: %r вне допустимых" % (where, payload.get("status")))
    progress = payload.get("progress")
    if isinstance(progress, dict):
        _shape(progress, {"done": int, "total": int, "percent": int}, where + ".progress", out)


def post_checks(live: Live) -> list[Violations]:
    """POST/PATCH/DELETE по docs/API.md §6: коды и формы. Возвращает нарушения."""
    results: list[Violations] = []

    def check(label: str) -> Violations:
        item = Violations(label)
        results.append(item)
        return item

    # --- создание запроса
    out = check("POST /api/rooms/cafe/requests (201)")
    status, payload, headers = live.post("/api/rooms/cafe/requests",
                                         {"text": "Проверка контракта: собери короткий ответ по каналу D19."})
    if status != 201:
        out.type.append("HTTP %s вместо 201: %s" % (status, str(payload)[:200]))
    else:
        _shape(payload, {"request": dict, "root_id": str, "dispatcher": dict}, "body", out)
        _check_summary(payload.get("request"), "request", out)
        if not str(payload.get("root_id", "")).startswith("t_"):
            out.type.append("root_id: %r не t_…" % payload.get("root_id"))
        if not str(headers.get("Location", "")).startswith("/api/requests/"):
            out.missing.append("Location")
    new_rid = str(payload.get("root_id") or "") if isinstance(payload, dict) else ""

    # --- ошибки создания
    out = check("POST /api/rooms/cafe/requests mode=swarm (400)")
    status, payload, _ = live.post("/api/rooms/cafe/requests", {"text": "текст", "mode": "swarm"})
    if status != 400 or not isinstance(payload, dict) or "error" not in payload:
        out.type.append("HTTP %s вместо 400 {error}: %s" % (status, str(payload)[:160]))
    out = check("POST /api/rooms/nope/requests (404)")
    status, payload, _ = live.post("/api/rooms/nope/requests", {"text": "текст"})
    if status != 404:
        out.type.append("HTTP %s вместо 404: %s" % (status, str(payload)[:160]))

    # --- переименование названия (W17-B §4, B9 усилено)
    if new_rid:
        out = check("PATCH /api/requests/{rid} title (200) → user/locked")
        status, payload, _ = live.patch("/api/requests/%s" % new_rid,
                                        {"title": "Проверка названия"})
        if status != 200 or not isinstance(payload, dict):
            out.type.append("HTTP %s вместо 200: %s" % (status, str(payload)[:200]))
        else:
            _shape(payload, {"ok": bool, "action": str, "request": dict, "title_short": str,
                             "title_source": str, "title_locked": bool}, "body", out)
            if payload.get("title_source") != "user" or not payload.get("title_locked"):
                out.type.append("title_source/locked: %r/%r" % (payload.get("title_source"),
                                                                payload.get("title_locked")))
        out = check("PATCH /api/requests/{rid} title=\"\" (200) → derived/unlocked")
        status, payload, _ = live.patch("/api/requests/%s" % new_rid, {"title": ""})
        if status != 200 or not isinstance(payload, dict):
            out.type.append("HTTP %s вместо 200: %s" % (status, str(payload)[:200]))
        elif payload.get("title_locked") or payload.get("title_source") == "user":
            out.type.append("блокировка не снята: %r" % (payload.get("title_source"),))
        out = check("PATCH /api/requests/{rid} без title (400)")
        status, payload, _ = live.patch("/api/requests/%s" % new_rid, {})
        if status != 400 or not isinstance(payload, dict) or "error" not in payload:
            out.type.append("HTTP %s вместо 400 {error}: %s" % (status, str(payload)[:160]))

    # --- comment
    if new_rid:
        out = check("POST /api/requests/{rid}/actions comment (200)")
        status, payload, _ = live.post("/api/requests/%s/actions" % new_rid,
                                       {"action": "comment", "text": "Проверка комментария."})
        if status != 200:
            out.type.append("HTTP %s вместо 200: %s" % (status, str(payload)[:200]))
        else:
            _shape(payload, {"ok": bool, "action": str, "request": dict, "comment": dict}, "body", out)
            _shape(payload.get("comment"), {"id": int, "author": str, "text": str, "at": int},
                   "comment", out)

    # --- answer по вопросу needs_you
    needs_rid = live.root_of("needs_you")
    status, detail, _ = live.get("/api/requests/" + needs_rid)
    questions = [q for q in (detail.get("open_questions") or []) if not q.get("answered")] \
        if isinstance(detail, dict) else []
    out = check("POST /api/requests/{rid}/actions answer (200)")
    if status != 200:
        out.type.append("GET детали: HTTP %s" % status)
    elif not questions:
        out.missing.append("open_questions: нет открытого вопроса у состояния needs_you")
    else:
        status, payload, _ = live.post("/api/requests/%s/actions" % needs_rid,
                                       {"action": "answer", "question_id": questions[0]["id"],
                                        "text": "Берём WhatsApp."})
        if status != 200:
            out.type.append("HTTP %s вместо 200: %s" % (status, str(payload)[:200]))
        else:
            _shape(payload, {"ok": bool, "action": str, "request": dict, "question": dict,
                             "unblocked": list}, "body", out)
            _shape(payload.get("question"),
                   {"id": str, "text": str, "for": str, "answered": bool,
                    "answer": (str, type(None)), "answered_at": (int, type(None))}, "question", out)

    # --- revise
    run_rid = live.root_of("done_modern")
    out = check("POST /api/requests/{rid}/actions revise (200)")
    status, payload, _ = live.post("/api/requests/%s/actions" % run_rid,
                                   {"action": "revise", "text": "Добавь расчёт по V1."})
    if status != 200:
        out.type.append("HTTP %s вместо 200: %s" % (status, str(payload)[:200]))
    else:
        _shape(payload, {"ok": bool, "action": str, "request": dict, "version": int,
                         "previous_version": int, "revision_task_id": str, "note": str}, "body", out)

    # --- reassign
    work_rid = live.root_of("working")
    status, detail, _ = live.get("/api/requests/" + work_rid)
    kids = [n for n in (detail.get("tree") or []) if n.get("role") == "worker"] if isinstance(detail, dict) else []
    out = check("POST /api/requests/{rid}/actions reassign (200)")
    if not kids:
        out.missing.append("tree: нет рабочей карточки у состояния working")
    else:
        status, payload, _ = live.post("/api/requests/%s/actions" % work_rid,
                                       {"action": "reassign", "task_id": kids[0]["id"],
                                        "profile": "biz-strategy"})
        if status != 200:
            out.type.append("HTTP %s вместо 200: %s" % (status, str(payload)[:200]))
        else:
            _shape(payload, {"ok": bool, "action": str, "request": dict, "task": dict}, "body", out)
            _shape(payload.get("task"), {"id": str, "role": str, "status": str, "outside_room": bool},
                   "task", out)

    # --- бэклог: status и ask
    status, backlog, _ = live.get("/api/rooms/cafe/backlog")
    items = backlog.get("items") or [] if isinstance(backlog, dict) else []
    out = check("POST /api/rooms/cafe/backlog/{item_id} (200)")
    if not items:
        out.missing.append("items: бэклог пуст — нечего отмечать")
    else:
        item_id = str(items[0]["id"])
        status, payload, _ = live.post("/api/rooms/cafe/backlog/" + item_id, {"status": "done"})
        if status != 200:
            out.type.append("HTTP %s вместо 200: %s" % (status, str(payload)[:200]))
        else:
            _shape(payload, {"item": dict, "counts": dict}, "body", out)
            if isinstance(payload.get("item"), dict) and payload["item"].get("status") not in BACKLOG_STATUSES:
                out.type.append("item.status: %r вне допустимых" % payload["item"].get("status"))
    out = check("POST /api/rooms/cafe/backlog/{item_id}/ask (201)")
    if not items:
        out.missing.append("items: бэклог пуст — нечего спрашивать")
    else:
        status, payload, _ = live.post("/api/rooms/cafe/backlog/%s/ask" % items[0]["id"], {})
        if status != 201:
            out.type.append("HTTP %s вместо 201: %s" % (status, str(payload)[:200]))
        else:
            _shape(payload, {"request": dict, "item": dict, "root_id": str, "request_id": str}, "body", out)
            _check_summary(payload.get("request"), "request", out)
            if payload.get("root_id") != payload.get("request_id"):
                out.type.append("root_id != request_id")

    # --- участники
    out = check("POST /api/rooms/cafe/members (201) + DELETE (200)")
    status, payload, _ = live.post("/api/rooms/cafe/members", {"profile": "coder", "role": "worker"})
    if status != 201:
        out.type.append("HTTP %s вместо 201: %s" % (status, str(payload)[:200]))
    else:
        _shape(payload, {"member": dict, "members": list}, "body", out)
        _shape(payload.get("member"), {"profile": str, "role": str, "note": str, "pinned": bool,
                                       "added_at": int}, "member", out)
        status, payload, _ = live.delete("/api/rooms/cafe/members/coder")
        if status != 200:
            out.type.append("DELETE: HTTP %s вместо 200" % status)
        else:
            _shape(payload, {"ok": bool, "members": list}, "delete", out)
    status, payload, _ = live.delete("/api/rooms/cafe/members/biz-director")
    if status != 409:
        out.type.append("DELETE директора: HTTP %s вместо 409" % status)

    # --- создание комнаты
    out = check("POST /api/rooms (201)")
    status, payload, headers = live.post("/api/rooms", {"title": "Проверка конформанса", "orchestrator": "default",
                                                        "members": [{"profile": "coder"}]})
    if status != 201:
        out.type.append("HTTP %s вместо 201: %s" % (status, str(payload)[:200]))
    else:
        _shape(payload, {"id": str, "title": str, "tenant": str, "mode_default": str, "max_rounds": int,
                         "seeded": bool, "members": list, "counters": dict, "phase": (dict, type(None)),
                         "dispatcher": dict}, "body", out)
        if not str(headers.get("Location", "")).startswith("/api/rooms/"):
            out.missing.append("Location")
    out = check("PATCH /api/rooms/cafe max_rounds=2 (200) / id → 400")
    status, payload, _ = live.patch("/api/rooms/cafe", {"max_rounds": 2})
    if status != 200:
        out.type.append("HTTP %s вместо 200: %s" % (status, str(payload)[:200]))
    status, payload, _ = live.patch("/api/rooms/cafe", {"id": "other"})
    if status != 400:
        out.type.append("PATCH id: HTTP %s вместо 400" % status)
    live.patch("/api/rooms/cafe", {"max_rounds": 1})

    # --- adopt кандидата
    status, cands, _ = live.get("/api/rooms/cafe/candidates")
    items = cands.get("items") or [] if isinstance(cands, dict) else []
    out = check("POST /api/adopt (200, идемпотентно)")
    if not items:
        out.missing.append("candidates.items: нет кандидатов")
    else:
        task_id = str(items[0]["task_id"])
        status, payload, _ = live.post("/api/adopt", {"room": "cafe", "task_id": task_id})
        if status != 200:
            out.type.append("HTTP %s вместо 200: %s" % (status, str(payload)[:200]))
        else:
            _shape(payload, {"ok": bool, "request": dict, "adopted": bool, "already": bool}, "body", out)
            _check_summary(payload.get("request"), "request", out)
            status, payload, _ = live.post("/api/adopt", {"room": "cafe", "task_id": task_id})
            if status != 200 or not isinstance(payload, dict) or payload.get("already") is not True:
                out.type.append("повторный adopt: HTTP %s, already=%s" % (status, (payload or {}).get("already")))

    # --- ошибки действий и поиска
    out = check("Ошибки: actions/unknown, search?q=a, GET чужой rid (400/400/404)")
    status, payload, _ = live.post("/api/requests/%s/actions" % (new_rid or "t_deadbeef"), {"action": "nope"})
    if status not in (400, 404):
        out.type.append("unknown action: HTTP %s вместо 400" % status)
    status, payload, _ = live.get("/api/search?q=a")
    if status != 400:
        out.type.append("search?q=a: HTTP %s вместо 400" % status)
    status, payload, _ = live.get("/api/requests/t_deadbeef")
    if status != 404:
        out.type.append("GET t_deadbeef: HTTP %s вместо 404" % status)
    return results


# ------------------------------------------------------------------------ отчёт
DEFAULT_SHALLOW = {"metadata", "blocks", "detail"}
#: Необязательные поля живого ответа, которых может не быть в фикстурах:
#: `body_full` — полное тело карточки запроса для отладки (text = только текст
#: пользователя, D1 из docs/critic/wave5-ui.md).
OPTIONAL_EXTRA = {"body_full"}


def run(with_posts: bool = True, verbose: bool = False) -> int:
    """Полный прогон. 0 — чисто, 1 — есть нарушения. Печатает отчёт."""
    files = sorted(p.name for p in FIX.glob("*.json"))
    data = {}
    for name in files:
        with open(FIX / name, encoding="utf-8") as fh:
            data[name] = json.load(fh)

    get_results: list[tuple[dict, Violations]] = []
    post_results: list[Violations] = []
    pending_notes: list[str] = []
    with Live(posts=with_posts) as live:
        requests = _fixture_requests(live)
        if "search.json" in data:
            q = str((data["search.json"] or {}).get("q") or "моти")
            for item in requests:
                if item.get("q"):
                    from urllib.parse import quote
                    item["path"] = item["path"] % quote(q)
        report_spec = _report_spec_check(live)
        requests.append(report_spec)
        for item in requests:
            name = item["file"]
            if name not in data:
                continue
            out = Violations(name)
            status, payload, _headers = live.get(item["path"])
            if status != 200:
                out.type.append("HTTP %s вместо 200: %s" % (status, str(payload)[:160]))
            else:
                shallow = set(item.get("shallow") or ()) | DEFAULT_SHALLOW
                compare(data[name], payload, [], item["enums"], out, shallow)
            get_results.append((item, out))
        get_results.append(({"file": "(GET) dispatcher/preview", "path": "/api/dispatcher/preview",
                             "enums": NO_ENUMS}, _preview_check(live)))
        catalog_out, catalog_note = _catalog_check(live)
        get_results.append(({"file": "models.json", "path": "/api/models",
                             "enums": NO_ENUMS}, catalog_out))
        if catalog_note:
            pending_notes.append(catalog_note)
        if with_posts:
            post_results = post_checks(live)
            # после POST живое состояние изменилось: фикстуры сверяем уже снятыми выше

    total = sum(out.count for _item, out in get_results) + sum(out.count for out in post_results)
    extras = sum(len(out.extra) for _item, out in get_results)
    empties = sum(len(out.empty) for _item, out in get_results)
    pendings = sum(len(out.pending) for _item, out in get_results)

    print("СВЕРКА ЖИВОГО БЭКЕНДА С КОНТРАКТОМ (fixtures + docs/API.md)")
    print("=" * 78)
    print("GET по фикстурам (%d файлов):" % len(get_results))
    for item, out in get_results:
        mark = "OK  " if not out.failed else "FAIL"
        line = "  [%s] %-24s GET %s" % (mark, item["file"], item["path"])
        print(line)
        if out.failed:
            detail = out.line()
            if len(detail) > 400:
                detail = detail[:400] + " …"
            print("         " + detail)
    if post_results:
        print("POST/PATCH/DELETE по API.md:")
        for out in post_results:
            mark = "OK  " if not out.failed else "FAIL"
            print("  [%s] %s" % (mark, out.label))
            if out.failed:
                print("         " + out.line()[:400])
    for note in pending_notes:
        print("  %s — не нарушение, ждёт свою волну" % note)
    if pendings:
        print("Поля фикстур, которых сервер ещё не отдаёт (не нарушение, ждут свою волну):")
        for item, out in get_results:
            if out.pending:
                print("  %s: %s" % (item["file"], ", ".join(out.pending[:12])))
    if verbose:
        print("Лишние ключи живого ответа (предупреждение, не ошибка):")
        for item, out in get_results:
            if out.extra:
                print("  %s: %s" % (item["file"], ", ".join(out.extra[:20]) +
                                   (" …" if len(out.extra) > 20 else "")))
    print("-" * 78)
    print("ИТОГ: нарушений %d (GET %d, POST %d); предупреждений: пустых списков %d, лишних ключей %d, "
          "ещё не реализовано %d"
          % (total, sum(out.count for _i, out in get_results),
             sum(out.count for out in post_results), empties, extras, pendings))
    if total:
        print("ЕСТЬ РАСХОЖДЕНИЯ — контракт не выполнен.")
        return 1
    print("ЖИВОЙ БЭКЕНД СОВПАДАЕТ С КОНТРАКТОМ.")
    return 0


def main(argv=None) -> int:
    """Внешняя точка входа: на всё время прогона запрещает запись в реальный Hermes/Vault
    и запуск агентского CLI (`hermes dispatch|decompose|swarm|profile|...`).

    См. docs/SAFETY-RULES.md; инцидент 28.10.2026 — затёртые config.yaml/профили.
    """
    from server import safety

    with safety.forbid_real_writes(), safety.forbid_real_subprocess():
        return _main(argv)


def _main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Сверка живого бэкенда с fixtures + docs/API.md")
    parser.add_argument("-v", "--verbose", action="store_true", help="печатать лишние ключи")
    parser.add_argument("--no-posts", action="store_true", help="только GET по фикстурам")
    args = parser.parse_args(argv)
    try:
        return run(with_posts=not args.no_posts, verbose=args.verbose)
    except Exception as exc:  # noqa: BLE001 - отчёт должен быть, даже если песочница не поднялась
        print("СВЕРКА НЕ ВЫПОЛНЕНА: %(type)s: %(msg)s" % {"type": type(exc).__name__, "msg": exc})
        import traceback
        traceback.print_exc()
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
