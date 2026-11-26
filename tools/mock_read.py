# -*- coding: utf-8 -*-
"""GET-обработчики мок-сервера (чтение фикстур и живых запросов)."""
from __future__ import annotations

import copy
import mimetypes
import os
import re
import time
from urllib.parse import quote, unquote

from mock_core import (ALLOWED_EXT, DENY_NAME_PARTS, FILE_ROOTS, MIME, MOCK, MOCK_PRIVATE, OFFICE_EXT, PHASES,
                       ApiError, Raw, backlog_counts, child_id, compute_needs_you, export_html,
                       export_markdown, ics_escape, inside, is_service_question, now, short_title,
                       split_heartbeats)
import mock_backlog
import mock_ac1         # W29-AC1: результаты комнаты, входящие, расход запроса
import mock_batchb       # W29-AC2: совместимость, второе мнение, итог дня, память, уведомления
import mock_demo as demo
import mock_delos
import mock_stt
import mock_duties_fe   # W27-E8: демо-сервер дежурств комнаты
import mock_recipes      # W29B-Q1: демо-сервер рецептов комнаты
import mock_w30c         # W30-C: вопросы по-человечески и предложение руководителя
import mock_w30f        # W30-F: окно выбора файлов, «Запросы без дела»
import mock_w30d         # W30-D2: доска задач комнаты (канбан)
import mock_w30e         # W30-E: результаты агентов (блок в задаче)
from mock_write import collapse_approvals   # W22-ST: строки-группы разрешений (одно правило)

_LABEL_DASH = re.compile("\\s*[\u2014\u2013\u2015\u2012\u2212-]{1,2}\\s*")


def needs_you_count(detail: dict) -> dict:
    """`needs_you_count` запроса — то же правило, что §1 воли 27 (W27-E2).

    Считает ТОЛЬКО пункты, у которых есть действие: вопрос `open` без ответа и без
    снятия, решение `proposed`/`discuss`, разрешение `pending` c действиями. Всё
    остальное (`closed`, `dismissed`, `expired`, `granted_*`, `denied`, `answered`,
    `accepted`, `rejected`) не считается: именно из-за этого интерфейс складывал
    20 закрытых разрешений в «27» и звал решать решённое.

    Шаги считаются как на сервере (`server/stale.py: counts`): шаг ждёт человека,
    пока его состояние `open` — тот же словарь, что в `stale.waits()`. Раньше здесь
    стоял ноль, и демо показывало счётчик меньше боевого.
    """
    d = detail if isinstance(detail, dict) else {}
    questions = 0
    for q in d.get("open_questions") or []:
        if not isinstance(q, dict):
            continue
        state = str(q.get("state") or "open").lower()
        if state != "open" or q.get("answered") or q.get("dismissed"):
            continue
        questions += 1
    decisions = 0
    for item in d.get("decisions") or []:
        if not isinstance(item, dict):
            continue
        status = str(item.get("status") or "proposed").lower()
        if item.get("dismissed") or item.get("superseded"):
            continue
        if item.get("open") is True or status in ("proposed", "discuss"):
            decisions += 1
    approvals = 0
    for a in d.get("approvals") or []:
        if not isinstance(a, dict):
            continue
        if str(a.get("state") or "").lower() != "pending":
            continue
        if not a.get("actions"):
            continue
        approvals += 1
    steps = 0
    for s in d.get("next_steps") or []:
        if not isinstance(s, dict):
            continue
        state = str(s.get("state") or "open").lower()
        if state != "open" or s.get("dismissed"):
            continue
        steps += 1
    total = questions + decisions + approvals + steps
    return {"questions": questions, "decisions": decisions, "approvals": approvals,
            "steps": steps, "total": total}


def phase_rules():
    """Правила этапов — ТЕ ЖЕ, что у сервера (`server/phase_book.py`), без второго мнения.

    Демо-мок живёт отдельно от сервера, но повторять «как решать этапы» нельзя:
    два мнения разойдутся. Если серверных модулей рядом нет (мок запущен сам по
    себе), поля предложения просто не отдаются — демо продолжает работать.
    """
    try:
        import sys as _sys
        from pathlib import Path as _Path
        server_dir = _Path(__file__).resolve().parent.parent / "server"
        if str(server_dir) not in _sys.path:      # демо: те же правила этапов, что у сервера
            _sys.path.insert(0, str(server_dir))
        import phase_book as module      # noqa: WPS433 - намеренно ленивый импорт
        return module
    except Exception:  # noqa: BLE001 - демо важнее полноты
        return None


def split_label(text: str):
    """«Анна — финансы (CFO)» → («Анна», «финансы (CFO)»).

    То же правило, что в `server/profile_info.parse_description`: демо-мок живёт
    отдельно от сервера, но подпись в демо должна выглядеть одинаково.
    """
    raw = " ".join(str(text or "").split())
    if not raw:
        return "", ""
    match = _LABEL_DASH.search(raw)
    if match:
        left = raw[:match.start()].strip(" .,:;")
        right = raw[match.end():].strip(" .,:;")
        if left and len(left) <= 60 and len(left.split()) <= 4:
            return left, right
    return "", raw


def _ru_count(n: int, one: str, few: str, many: str) -> str:
    """«1 исправление», «2 исправления», «5 исправлений» — как в интерфейсе."""
    num = abs(int(n or 0))
    tail100, tail10 = num % 100, num % 10
    if 11 <= tail100 <= 14:
        word = many
    elif tail10 == 1:
        word = one
    elif 2 <= tail10 <= 4:
        word = few
    else:
        word = many
    return "%d %s" % (num, word)


# --- W24-N3: «Сегодня», дежурства и здоровье интеграций ---------------------
# Фикстуры today.json / duties.json / health_integrations.json написаны с
# абсолютными временами; демо должно выглядеть живым, поэтому мок сдвигает их на
# «сейчас − generated_at». Форма ответа при этом ровно та, что в контракте
# (docs/WAVE24-RULES.md §3): сдвигаются только значения, не поля.
TIME_KEYS = frozenset(("generated_at", "last_visit_at", "since", "at", "started_at",
                       "finished_at", "scheduled_for", "next_run_at"))
DATE_KEYS = frozenset(("due",))
EPOCH_MIN = 1_000_000_000          # всё меньше — не время (счётчики, дни), не трогаем


def shift_times(value, delta_s: int, delta_days: int):
    """Сдвинуть все времена/даты внутри структуры на delta (рекурсивно)."""
    if isinstance(value, dict):
        out = {}
        for key, item in value.items():
            if key in TIME_KEYS and isinstance(item, int) and not isinstance(item, bool) and item >= EPOCH_MIN:
                out[key] = item + delta_s
            elif key in DATE_KEYS and isinstance(item, str) and len(item) == 10 and item[4] == "-":
                out[key] = shift_date(item, delta_days)
            else:
                out[key] = shift_times(item, delta_s, delta_days)
        return out
    if isinstance(value, list):
        return [shift_times(item, delta_s, delta_days) for item in value]
    return value


def shift_date(text: str, days: int) -> str:
    """«2026-11-11» + N дней — по календарю сервера."""
    try:
        moment = time.mktime(time.strptime(text, "%Y-%m-%d"))
    except (TypeError, ValueError):
        return text
    return time.strftime("%Y-%m-%d", time.localtime(moment + days * 86400))



class MockRead:
    def get(self, path: str, query: dict) -> tuple:
        hit = mock_w30c.handle(self, "GET", path, query, {})
        if hit is not None:                 # W30-C: предложение руководителя (демо)
            return hit
        hit = mock_w30e.handle(self, "GET", path, query, {})
        if hit is not None:                 # W30-E: результаты агентов, демо-просмотр
            return hit
        hit = mock_w30f.handle(self, "GET", path, query, {})
        if hit is not None:                 # W30-F: «Запросы без дела» в форме боевого ответа
            return hit
        hit = mock_batchb.handle(self, "GET", path, query, {})
        if hit is not None:                 # W29-AC2: ручки партии B
            return hit
        hit = mock_ac1.handle(self, "GET", path, query, {})
        if hit is not None:                 # W29-AC1: результаты, входящие, расход
            return hit
        if path == "/api/health":
            return 200, self.health()
        # W29-P9: расход и лимиты по провайдерам — демо из fixtures/limits.json.
        if path == "/api/limits":
            return 200, self.limits_panel()
        # W26-D4: существование файла для чипов-ссылок свода (демо: набор путей).
        if path == "/api/files/info":
            return 200, self.files_info(query)
        # W24-N3: экран «Сегодня», дежурства и здоровье интеграций (контракт §3).
        if path == "/api/today":
            return 200, self.today()
        if path == "/api/duties":
            return 200, self.duties(query)
        # W27-E8: шаблоны дежурств и история запусков (свои блоки демо-сервера).
        if path == "/api/duty-templates":
            return 200, mock_duties_fe.templates()
        # W29B-Q1: рецепты комнаты — список, корзина, история запусков, роли.
        if re.fullmatch(r"/api/rooms/[a-z][a-z0-9_-]{1,31}/recipes", path):
            return 200, mock_recipes.listing()
        # W30-D2: доска «Задачи» комнаты — задачи по стадиям (контракт §3).
        m = re.fullmatch(r"/api/rooms/([a-z][a-z0-9_-]{1,31})/tasks", path)
        if m:
            return 200, mock_w30d.listing(self, m.group(1))
        m = re.fullmatch(r"/api/duties/([A-Za-z0-9_.-]{1,64})/history", path)
        if m:
            return 200, mock_duties_fe.history(self, m.group(1))
        # W27-E6 (контракт §2): «Уровень автономии» комнаты — три чтения.
        #   GET …/autonomy          — уровень, список «только с моего да», участники, предпросмотр
        #   GET …/autonomy/journal  — что сделано по уровню и что остановлено (severity)
        #   GET …/autonomy/suggest  — предложения по истории («вы разрешали 22 раза»)
        m = re.fullmatch(r"/api/rooms/([a-z0-9-]{2,32})/autonomy", path)
        if m:
            return 200, self.autonomy_payload(m.group(1))
        m = re.fullmatch(r"/api/rooms/([a-z0-9-]{2,32})/autonomy/journal", path)
        if m:
            return 200, self.autonomy_journal(m.group(1))
        m = re.fullmatch(r"/api/rooms/([a-z0-9-]{2,32})/autonomy/suggest", path)
        if m:
            return 200, self.autonomy_suggest(m.group(1))
        # W26-D5: дела — список комнаты/all и одно дело (форма из fixtures/delo.json)
        if path == "/api/delos" or re.fullmatch(r"/api/rooms/[a-z][a-z0-9_-]{1,31}/delos", path):
            return 200, mock_delos.payload(query, path)
        m = re.fullmatch(r"/api/delos/(dl_[A-Za-z0-9]{2,24})", path)
        if m:
            return 200, mock_delos.one(m.group(1))
        if path == "/api/health/integrations":
            return 200, self.health_integrations()
        if path == "/api/stt/status":
            return 200, mock_stt.status()
        if path == "/api/stt/dictionary":
            return 200, mock_stt.get_dictionary()
        if path == "/api/stt/recordings":
            raw = (query.get("limit") or ["20"])[0]
            try:
                limit = max(1, min(50, int(raw)))
            except (TypeError, ValueError):
                raise ApiError(400, "bad_request", "limit: целое число 1…50", {"field": "limit"})
            return 200, mock_stt.recordings(limit)
        m = re.fullmatch(r"/api/stt/recordings/([a-z0-9][a-z0-9_-]{5,40})", path)
        if m:
            return 200, mock_stt.recording(m.group(1))
        m = re.fullmatch(r"/api/stt/recordings/([a-z0-9][a-z0-9_-]{5,40})/parts/(\d{1,3})", path)
        if m:
            data, ctype = mock_stt.part(m.group(1), m.group(2))
            return 200, Raw(data, ctype, {"Cache-Control": "no-store"})
        if path == "/api/config":
            return 200, self.fixture("config.json")
        if path == "/api/approvals/grants":
            # W22-AP2: выданные разрешения участникам комнаты (демо — память).
            return 200, {"items": self.approval_grants()}
        if path == "/api/models":
            return 200, self.catalog()               # каталог моделей (WAVE10 §19)
        if path == "/api/profiles":
            return 200, self.fixture("profiles.json")
        if path == "/api/rooms":
            include = query.get("include_archived", ["0"])[0] in ("1", "true", "yes")
            # W29-P7: тот же порядок, что на сервере — закреплённые, затем `order`.
            ids = list(self.room_details)
            ids.sort(key=lambda rid: (0 if self.room(rid).get("pinned") else 1,
                                      int(self.room(rid).get("order") or 0) or 10 ** 9,
                                      list(self.room_details).index(rid)))
            rooms = [self.room_summary(rid) for rid in ids if include or not self.room(rid)["archived"]]
            return 200, {"rooms": rooms, "groups": self.groups,
                         "engine": {"ok": True, "board": "team", "warning": None},
                         "updated_at": now()}
        if path == "/api/rooms/summary":
            return 200, self.rooms_summary(query)          # W17-B §1: одно чтение на меню
        if path == "/api/dispatcher/preview":
            return 200, self.dispatcher_preview()          # W17-B §1: чтение без POST
        m = re.fullmatch(r"/api/rooms/([a-z0-9-]{2,32})/notes", path)
        if m:
            return 200, self.room_notes(m.group(1))          # WAVE14 §13
        m = re.fullmatch(r"/api/rooms/([a-z0-9-]{2,32})/notes/journal", path)
        if m:
            raw = (query.get("limit") or ["50"])[0]
            try:
                limit = int(raw)
            except (TypeError, ValueError):
                limit = 50
            return 200, self.notes_journal(m.group(1), limit)
        m = re.fullmatch(r"/api/rooms/([a-z0-9-]{2,32})/agents/([a-z0-9_.-]{2,40})/notes", path)
        if m:
            return 200, self.get_notes_entries(m.group(1), m.group(2))
        m = re.fullmatch(r"/api/rooms/([a-z0-9-]{2,32})/plan", path)
        if m:
            return 200, self.get_plan(m.group(1))
        m = re.fullmatch(r"/api/rooms/([a-z0-9-]{2,32})", path)
        if m:
            summary = self.room_summary(m.group(1))
            room = self.room(m.group(1))
            rules = phase_rules()
            return 200, {**self.with_models(room), "counters": summary["counters"],
                         "phase": summary["phase"],
                         # W20 §1.7: предложение этапов и подпись состояния — в карточке комнаты.
                         "phases": room.get("phases") or [],
                         "phases_proposal": (rules.norm_proposal(room.get("phases_proposal"))
                                             if rules else None),
                         "phases_proposal_text": (rules.state_text(room.get("phases_proposal"))
                                                  if rules else ""),
                         "dispatcher": {"ok": True, "state": "running", "warning": None}}
        m = re.fullmatch(r"/api/rooms/([a-z0-9-]{2,32})/archive", path)
        if m:
            # W29-P4: раздел «Архив» в демо — из того, что уже убрано в памяти.
            return 200, self.archive_list(m.group(1), query)
        m = re.fullmatch(r"/api/rooms/([a-z0-9-]{2,32})/requests", path)
        if m:
            return 200, self.list_requests(m.group(1), query)
        m = re.fullmatch(r"/api/rooms/([a-z0-9-]{2,32})/candidates", path)
        if m:
            return 200, self.candidates_for(m.group(1))
        m = re.fullmatch(r"/api/requests/(t_[0-9a-f]{8})", path)
        if m:
            return 200, self.request_detail(m.group(1), query)
        # W20 §1.6: «Ход работы» запроса — лента событий агентов (демо на фикстуре)
        m = re.fullmatch(r"/api/requests/(t_[0-9a-f]{8})/worklog", path)
        if m:
            return 200, self.worklog(m.group(1), query)
        m = re.fullmatch(r"/api/rooms/([a-z0-9-]{2,32})/team", path)
        if m:
            return 200, self.team(m.group(1))
        # W23-MG2: «Контроль моделей» — ожидаемая модель против последнего запуска.
        m = re.fullmatch(r"/api/rooms/([a-z0-9-]{2,32})/model-check", path)
        if m:
            return 200, self.model_check(m.group(1))
        m = re.fullmatch(r"/api/rooms/([a-z0-9-]{2,32})/roadmap", path)
        if m:
            return 200, self.roadmap(m.group(1))
        m = re.fullmatch(r"/api/rooms/([a-z0-9-]{2,32})/backlog", path)
        if m:
            return 200, self.backlog_for(m.group(1), query)
        m = re.fullmatch(r"/api/rooms/([a-z0-9-]{2,32})/journal", path)
        if m:
            return 200, self.journal(m.group(1), query)
        m = re.fullmatch(r"/api/rooms/([a-z0-9-]{2,32})/reports", path)
        if m:
            room = self.room(m.group(1))
            items = copy.deepcopy(self.fixture("reports_cafe.json")["items"]) if room["id"] == "cafe" else []
            for rid, req in self.live.items():
                if req.get("room") != room["id"]:
                    continue
                detail = self.live_detail(rid)
                if (detail.get("synthesis") or {}).get("state") != "ready":
                    continue
                ref = detail.get("report") or {}
                items.insert(0, {"key": ref.get("key") or demo.report_key(rid),
                                 "title": ref.get("title") or "Разбор запроса",
                                 "subtitle": "Разбор живого запроса · демо",
                                 "kind": "spec", "path": None, "request_id": rid,
                                 "blocks": 9, "created_at": detail["created_at"],
                                 "updated_at": detail["created_at"] + 35, "exists": True})
            return 200, {"room": room["id"], "items": items}
        m = re.fullmatch(r"/api/reports/([a-z0-9-]{2,32})/([a-z0-9-]{2,64})", path)
        if m:
            room, key = m.group(1), m.group(2)
            self.room(room)
            if key == "unit-economics":
                return 200, self.fixture("report_unit_economics.json")
            if key.startswith("demo-"):
                rid = "t_" + key[len("demo-"):]
                if re.fullmatch(r"t_[0-9a-f]{8}", rid) and rid in self.live:
                    detail = self.detail(rid)
                    if (detail.get("synthesis") or {}).get("state") != "ready":
                        raise ApiError(409, "conflict", "Ответ ещё не готов — отчёт появится после свода",
                                       {"reason": "synthesis_pending", "rid": rid})
                    return 200, demo.report_spec(detail, detail.get("text") or "")
            raise ApiError(404, "not_found", "Отчёт не найден", {"room": room, "key": key})
        if path == "/api/search":
            return 200, self.search(query)
        if path == "/api/ics":
            return self.ics(query)
        m = re.fullmatch(r"/api/export/request/(t_[0-9a-f]{8})\.(md|html)", path)
        if m:
            return self.export(m.group(1), m.group(2))
        if path == "/api/file":
            return self.file_response(query)
        raise ApiError(404, "not_found", "Неизвестный эндпоинт", {"path": path})

    @staticmethod
    def _recent_start(ts, minutes: int = 11) -> int:
        """Демо: давнюю отметку начала «освежаем» — карточка показывает «уже 11 мин»,
        а не «20 дн»: в вымышленных данных даты могли разъехаться."""
        value = int(ts or 0)
        moment = now()
        if value and moment - value > 45 * 60:
            return moment - int(minutes) * 60
        return value

    # --- W24-N3: «Сегодня», дежурства, здоровье интеграций -------------------
    def _shifted(self, name: str) -> dict:
        """Фикстура со сдвинутыми на «сейчас» временами (демо всегда живое)."""
        data = self.fixture(name)
        base = int(data.get("generated_at") or 0)
        delta = now() - base if base else 0
        return shift_times(data, delta, int(round(delta / 86400.0)))

    def today(self) -> dict:
        """GET /api/today — то же поле в поле, что отдаёт сервер (контракт §3.1)."""
        data = self._shifted("today.json")
        data.pop("_comment", None)
        seen = getattr(self, "today_seen_at", None)
        if seen:
            data["last_visit_at"] = seen
        data["generated_at"] = now()
        return data

    def today_seen(self) -> dict:
        """POST /api/today/seen — отметка захода (демо: в памяти, как на сервере).

        Контракт §3.1: `{ok, last_visit_at}`; значение меняется не чаще раза в 60 с —
        повторные отметки внутри минуты возвращают тот же момент.
        """
        moment = now()
        current = getattr(self, "today_seen_at", None)
        if not current:
            current = int(self._shifted("today.json").get("last_visit_at") or 0) or None
        if current is None or moment - current > 60:
            self.today_seen_at = moment
            current = moment
        return {"ok": True, "last_visit_at": current}

    # W26-D4: демо-набор существующих файлов (реальные пути не читаются).
    DEMO_FILES = {
        "C:/Работа/Проект Витрина/Макет витрины — финал.md",
        "C:/Работа/Проект Витрина/План работ на год.md",
        "C:/Работа/Проект Витрина/Исходники/Сетка блоков.csv",
    }

    def files_info(self, query: dict) -> dict:
        from pathlib import PurePath
        raw = (query.get("path") or [""])[0].strip()
        if not raw:
            raise ApiError(400, "bad_request", "Нужен параметр path", {"field": "path"})
        name = PurePath(raw.replace("\\", "/")).name
        if not raw.startswith("C:/"):
            return {"ok": True, "exists": None, "path": raw, "name": name, "size": None,
                    "reason": "outside_roots"}
        exists = raw in self.DEMO_FILES
        return {"ok": True, "exists": exists, "path": raw, "name": name,
                "size": 1024 if exists else None}

    def duties(self, query: dict) -> dict:
        """GET /api/duties?room=<id|all> — дежурства (контракт §3.2)."""
        data = self._shifted("duties.json")
        data.pop("_comment", None)
        room = (query.get("room") or ["all"])[0].strip() or "all"
        items = list(data.get("items") or [])
        # W29-P5: системные задачи Hermes — отдельно, в любой выборке.
        system = [item for item in items if not item.get("bound_rooms")]
        if room not in ("all", "*"):
            items = [item for item in items if room in (item.get("bound_rooms") or [])]
        data["items"] = items
        data["system"] = system
        data["generated_at"] = now()
        # W27-E8: поля дежурства комнаты (тип/правило/итог), корзина и предложения.
        payload = mock_duties_fe.enrich(self, data)
        # W29-P5: `?start=YYYY-MM-DD` — стрелки ‹ › листают неделю и в демо.
        start = (query.get("start") or [""])[0].strip()
        if start:
            payload["week"] = mock_duties_fe.demo_week(payload.get("items") or [], start)
        return payload

    def health_integrations(self) -> dict:
        """GET /api/health/integrations — сводка проверок (контракт §3.3)."""
        data = self._shifted("health_integrations.json")
        data.pop("_comment", None)
        data["generated_at"] = now()
        return data

    def limits_panel(self) -> dict:
        """GET /api/limits — демо-расход, прогресс к лимитам и строка для шапки (W29-P9)."""
        data = self.fixture("limits.json")
        data["generated_at"] = now()
        data["source"] = "fixtures"
        providers = (getattr(self, "demo_limits", None) or {}).get("providers") or {}
        data["limits"] = {"providers": copy.deepcopy(providers), "updated_at": 0}
        for key in ("5h", "day", "week"):
            window = (data.get("windows") or {}).get(key) or {}
            for row in window.get("providers") or []:
                limit = (providers.get(row.get("provider")) or {}).get(key)
                if not limit:
                    row["limit"] = None
                    row["limits"] = {}
                    continue
                used = (row.get("tokens") or {}).get("total", 0) if limit.get("kind") == "tokens" else row.get("calls", 0)
                value = int(limit.get("value") or 0)
                pct = round(100.0 * (used or 0) / value, 1) if value else 0.0
                status = "over" if pct >= 100 else "alert" if pct >= 90 else "warn" if pct >= 70 else "ok"
                row["limit"] = {"kind": limit.get("kind"), "value": value,
                                "used": used, "pct": pct, "status": status}
                row["limits"] = {key: row["limit"]}
        return data

    def health(self) -> dict:
        data = self.fixture("health.json")
        data["app"]["port"] = self.port
        data["app"]["started_at"] = self.started_at
        data["app"]["uptime_s"] = now() - self.started_at
        data.setdefault("engine", {})["mock"] = MOCK
        data["data"]["rooms"] = len(self.rooms)
        data["data"]["requests"] = len(self.requests) + len(self.live)
        return data

    # --- W17-B §1/§5: меню одним запросом, деталь без heartbeat и дубля вопросов ---
    DETAIL_FIELDS = {"text": "text", "body_full": "body_full", "dispatcher": "dispatcher",
                     "synthesis": "synthesis", "steps": "next_steps", "questions": "open_questions",
                     "answers": "answers", "decisions": "decisions", "stage": "stage",
                     "future_questions": "future_questions", "artifacts": "artifacts", "report": "report",
                     "tree": "tree", "timeline": "timeline", "thread": "thread", "warnings": "warnings",
                     "critic": "critic", "heartbeats": "heartbeats",
                     # W22-AP2: запросы разрешения агентам — часть детали запроса.
                     "approvals": "approvals",
                     # W23-MG2: лента «Контроль моделей» — тоже часть детали.
                     "model_guard": "model_guard",
                     # W30-E: «Результаты агентов» — часть детали (`?fields=agent_results`).
                     "agent_results": "agent_results"}
    DETAIL_CORE = ("id", "room", "title", "title_short", "title_source", "title_locked",
                   "status", "status_reason", "mode", "critic",
                   "created_at", "started_at", "finished_at", "duration_s", "progress", "assignee",
                   "needs_you", "preview", "archived", "brief", "plan_link", "streams_summary",
                   "pinned", "pinned_at")

    def request_detail(self, rid: str, query: dict) -> dict:
        """Деталь запроса с `?include=heartbeat` и `?fields=…` (как бэкенд)."""
        include = [part.strip() for part in (query.get("include") or [""])[0].split(",") if part.strip()]
        bad = [value for value in include if value != "heartbeat"]
        if bad:
            raise ApiError(400, "bad_request", "Неизвестный include", {"include": bad[0],
                                                                       "allowed": ["heartbeat"]})
        fields = [part.strip() for part in (query.get("fields") or [""])[0].split(",") if part.strip()]
        bad = [value for value in fields if value not in self.DETAIL_FIELDS]
        if bad:
            raise ApiError(400, "bad_request", "Неизвестное поле",
                           {"fields": bad[0], "allowed": sorted(self.DETAIL_FIELDS)})
        detail = self.detail(rid)
        # W17-R §1 (B-8): этапы запроса — те, что объявила КОМНАТА (как на сервере).
        detail["stage"] = self._stage_for_room(detail.get("room"), detail.get("stage"))
        # W21-Q1 (B-45): живое состояние свода — клиент по `auto` решает, показывать
        # ли кнопку досылки ответов. Настройки — комнаты, как на сервере.
        self._ensure_svod_live(detail)
        # W22-AP2: запросы разрешения агентам (демо); ждущий запрос держит «ждёт вас».
        approvals = self._approvals_for(rid)
        # W22-ST: одинаковые по причине блокировки — одна строка со счётчиком «×N»
        # (то же правило, что `server/approvals._same_reason`; решение по строке
        # закрывает все её запросы — см. mock_write, действие `approval`).
        detail["approvals"] = [dict(a) for a in collapse_approvals(approvals)]
        # W27-E2 §1: реестр «не актуально» (снять/вернуть) — поверх собранной детали,
        # как реестр ответов: фикстура на каждый GET новая, отметка — в памяти мока.
        try:
            stale = self.stale_overrides(rid)
        except Exception:  # noqa: BLE001 — демо без реестра продолжает работать
            stale = {}
        def _mark(kind_name, item):
            bucket = stale.get(kind_name) or {}
            key = str((item or {}).get("id") or "")
            if key and key in bucket:
                if kind_name == "approval":
                    item["state"] = "dismissed"
                elif kind_name == "question":
                    item["dismissed"] = True
                    item["state"] = "dismissed"
                elif kind_name == "decision":
                    item["open"] = False
                    item["status"] = "dismissed"
                    item["dismissed"] = True
                elif kind_name == "step":
                    item["state"] = "dismissed"
                item["closed_reason"] = bucket[key] or "dismissed"
            return item
        for _kind, _field in (("approval", "approvals"), ("question", "open_questions"),
                              ("decision", "decisions"), ("step", "next_steps")):
            for _item in detail.get(_field) or []:
                if isinstance(_item, dict):
                    _mark(_kind, _item)
        # W22-AP1: счётчик и «ждёт вас» — по тем же правилам, что на сервере:
        # завершённая карточка ждёт человека, только если участник написал о блокировке.
        # Считаем СТРОКИ (как сервер: строка-группа — одно решение), а не команды.
        pending = [a for a in detail["approvals"] if a.get("state") == "pending"]
        detail["approvals_pending"] = len(pending)
        # W27-E2 §1: ОДНО число «ждёт вас» с расшифровкой — клиент показывает его,
        # а не складывает список (иначе 20 закрытых разрешений давали «27»).
        detail["needs_you_count"] = needs_you_count(detail)
        detail["approvals_actionable"] = detail["needs_you_count"]["approvals"]
        if any(a.get("card_state") not in ("done", "archived", "failed") or a.get("stopped")
               for a in pending):
            detail["needs_you"] = True
        detail["model_guard"] = self.guard_events(str(detail.get("room") or ""))
        # W29-P2: заблокированная карточка — пункт «ждёт вашего решения» с кнопками.
        # Мок отдаёт то же, что сервер: `actions[]` у узла, `hold` у системной паузы,
        # число `needs_you_blocked` и список `blocked_waiting` (один источник).
        _waiting_cards = []
        for node in detail.get("tree") or []:
            if not isinstance(node, dict) or str(node.get("status")) != "blocked":
                continue
            if isinstance(node.get("hold"), dict):
                node["actions"] = [{"id": "resume", "label": "Снять паузу сейчас", "kind": "system"}]
                node["needs_you"] = False
                continue
            node["actions"] = [{"id": "resume"}, {"id": "instruct"}, {"id": "model"},
                               {"id": "reassign"}, {"id": "dismiss"}]
            node["needs_you"] = True
            _waiting_cards.append({"id": str(node.get("id") or ""),
                                   "title": str(node.get("title") or ""),
                                   "assignee": str(node.get("assignee") or ""),
                                   "actions": [a["id"] for a in node["actions"]]})
        detail["needs_you_blocked"] = len(_waiting_cards)
        detail["blocked_waiting"] = _waiting_cards
        # Демо: у идущих карточек показываем «уже N мин», а не месяцы.
        for node in detail.get("tree") or []:
            if isinstance(node, dict) and str(node.get("status")) in ("running", "review"):
                node["started_at"] = self._recent_start(node.get("started_at"))
        timeline, heartbeats = split_heartbeats(detail.get("timeline") or [], "heartbeat" in include)
        detail["timeline"] = timeline
        detail["heartbeats"] = heartbeats
        if not detail.get("title_short"):
            detail["title_short"] = short_title(detail.get("title") or detail.get("text") or "")
        detail.setdefault("title_source", "derived")
        detail.setdefault("title_locked", False)
        # W20-U3 (P13): закрепление запроса — в демо живёт в памяти (`pin_overrides`).
        try:
            pin = self.pin_state(rid)
            detail["pinned"], detail["pinned_at"] = pin["pinned"], pin["pinned_at"]
        except Exception:  # noqa: BLE001 - без хранилища закреплений страница живёт
            detail.setdefault("pinned", False)
            detail.setdefault("pinned_at", None)
        detail.pop("questions", None)
        # W30-E: «Результаты агентов» — как на сервере, из дерева и метаданных.
        mock_w30e.attach(self, detail)
        # W17-B §7 (B7-3): служебный вопрос «сколько раундов критика» задан настройками
        # комнаты — не показываем его и не держим на нём «ждёт тебя» (как сервер).
        questions = detail.get("open_questions")
        if isinstance(questions, list):
            kept = [q for q in questions
                    if not (isinstance(q, dict) and is_service_question(q.get("text")))]
            if len(kept) != len(questions):
                detail["open_questions"] = kept
                detail["needs_you"] = compute_needs_you(detail)
        if fields:
            wanted = set(self.DETAIL_CORE)
            for name in fields:
                if name == "questions":
                    wanted.add("open_questions")
                else:
                    wanted.add(self.DETAIL_FIELDS[name])
            for key in [key for key in detail if key not in wanted]:
                detail.pop(key, None)
        # W30-C: демо-задачи пакета C — поля контракта §1–§2 (why/context/options,
        # пояснение, questions_summary, stage_hint, proposal, project).
        mock_w30c.overlay(self, detail, rid)
        return detail

    def _ensure_svod_live(self, detail: dict) -> None:
        """`synthesis.live` у готового свода в демо (W21-Q1, B-45).

        Клиент читает `live.auto` (показывать ли кнопку досылки ответов) и
        `live.next_update_at` (когда ответы учтут). Как и на сервере, `auto` —
        это НАСТРОЙКА КОМНАТЫ (`auto_svod`), а не свойство запроса: выключил
        автообновление в настройках — кнопка досылки в демо появляется.
        Остальные поля (state/text) фикстуры не трогаем.
        """
        syn = detail.get("synthesis")
        if not isinstance(syn, dict) or syn.get("state") != "ready":
            return
        try:
            room = self.room(str(detail.get("room") or "")) or {}
        except Exception:  # noqa: BLE001 - комната вторична для страницы
            room = {}
        auto = room.get("auto_svod")
        auto = True if auto is None else bool(auto)
        gap = int(room.get("auto_svod_min_gap_s") or 600)
        version = int(syn.get("version") or 0)
        moment = now()
        live = syn.get("live") if isinstance(syn.get("live"), dict) else None
        if live:
            live["auto"] = auto
            if not auto:
                live["next_update_at"] = None
            elif not live.get("next_update_at"):
                live["next_update_at"] = moment + gap
            return
        syn["live"] = {
            "state": "fresh", "since_version": version,
            "behind": {"answers": 0, "notes": 0, "decisions": 0, "cards": 0},
            "last_event_at": None,
            "next_update_at": moment + gap if auto else None,
            "auto": auto,
            "text": "Актуально на %s · версия %s" % (time.strftime("%H:%M", time.localtime(moment)), version),
        }

    # --- «Ход работы» (W20 §1.6) ---------------------------------------------
    WORKLOG_LEVELS = ("thoughts", "steps")

    def worklog(self, rid: str, query: dict) -> dict:
        """Демо-лента «Хода работы»: вымышленные события из `fixtures/worklog_demo.json`.

        Повторяет поведение сервера по контракту (docs/API.md §W20.2): режимы
        full/brief/off, `level`, `limit`, курсор «что нового» (`since`) и «показать
        раньше» (`before`). Курсор в демо — просто номер записи, для клиента он
        непрозрачен так же, как настоящий.
        """
        detail = self.detail(rid)
        # Законченный запрос: никто не работает, итог сторожа к нему уже не пишется
        # (так же ведёт себя сервер: лента остаётся, живое состояние и итог — нет).
        finished = str(detail.get("status") or "") in ("done", "failed")
        room_id = str(detail.get("room") or "")
        try:
            room = self.room(room_id) if room_id else {}
        except Exception:  # noqa: BLE001 - комната вторична для ленты
            room = {}
        mode = str(room.get("worklog_mode") or "full")
        digest_min = room.get("digest_every_min")
        digest_min = 2 if digest_min is None else int(digest_min)
        level = (query.get("level") or [""])[0].strip().lower()
        if level and level not in self.WORKLOG_LEVELS:
            raise ApiError(400, "bad_request",
                           "Уровень хода работы: «thoughts» или «steps»",
                           {"field": "level", "value": level,
                            "allowed": list(self.WORKLOG_LEVELS)})
        raw_limit = (query.get("limit") or [""])[0].strip()
        try:
            limit = int(raw_limit) if raw_limit else 50
        except (TypeError, ValueError):
            raise ApiError(400, "bad_request", "limit: целое число 1…200", {"field": "limit"})
        if limit < 1 or limit > 200:
            raise ApiError(400, "bad_request", "limit: от 1 до 200",
                           {"field": "limit", "value": raw_limit, "max": 200})
        use_level = "steps" if mode in ("brief", "off") else (level or "thoughts")
        stamp = now()
        payload = {"request": rid, "room": room_id, "mode": mode, "level": use_level,
                   "items": [], "cursor": "0", "before": "", "more": False, "live": [],
                   "digest": None, "stuck": [], "generated_at": stamp, "warnings": []}
        if mode == "off":
            return payload
        data = self.fixture("worklog_demo.json")
        items = []
        for raw in data.get("items") or []:
            if not isinstance(raw, dict):
                continue
            kind = str(raw.get("kind") or "")
            if mode == "brief" and kind in ("thought", "message"):
                continue
            if use_level == "steps" and kind == "thought":
                continue
            items.append({"id": raw.get("id"), "ts": stamp - int(raw.get("ago") or 0),
                          "card_id": raw.get("card_id"), "profile": raw.get("profile") or "",
                          "label": raw.get("label") or "", "role": raw.get("role") or "",
                          "kind": kind, "text": raw.get("text") or "",
                          "tool": raw.get("tool") or "", "ok": raw.get("ok")})
        items.sort(key=lambda item: item["ts"])

        def _index(value) -> int:
            try:
                return max(0, min(len(items), int(str(value or "0").strip() or 0)))
            except (TypeError, ValueError):
                return 0

        since = (query.get("since") or [""])[0].strip()
        before = (query.get("before") or [""])[0].strip()
        if before:
            point = _index(before)
            older = items[:point]
            payload["items"] = older[-limit:]
            payload["more"] = len(older) > len(payload["items"])
            payload["cursor"] = str(point)
            # Курсор следующей страницы «раньше» — индекс самой старой отданной записи
            # (W21-L1): клиент листает историю цепочкой, а не расширением окна.
            page_start = point - len(payload["items"])
            payload["before"] = str(page_start) if payload["more"] else ""
        else:
            start = _index(since) if since else max(0, len(items) - limit)
            chunk = items[start:start + limit]
            payload["items"] = chunk
            payload["cursor"] = str(start + len(chunk))
            # `more` — «за пределами страницы есть записи»: при первой странице это
            # более РАННИЕ (их листают кнопкой «Показать раньше»), при опросе — новые.
            payload["more"] = (start + len(chunk) < len(items)) if since else (start > 0)
            # «Показать раньше» — от начала показанной страницы (первая страница: последние N).
            if not since and start > 0:
                payload["before"] = str(start)
        live = []
        for raw in data.get("live") or []:
            if not isinstance(raw, dict):
                continue
            since_at = stamp - int(raw.get("since_ago") or 0)
            live.append({"profile": raw.get("profile") or "", "label": raw.get("label") or "",
                         "role": raw.get("role") or "", "card_id": raw.get("card_id"),
                         "state": raw.get("state") or "working", "since": since_at,
                         "last_action": raw.get("last_action") or "",
                         "tool": raw.get("tool") or "",
                         "tools": int(raw.get("tools") or 0),
                         "messages": int(raw.get("messages") or 0),
                         "stuck": bool(raw.get("stuck")),
                         "stuck_text": raw.get("stuck_text") or ""})
        payload["live"] = [] if finished else live
        payload["stuck"] = [] if finished else [{"card_id": row["card_id"], "profile": row["profile"],
                             "label": row["label"],
                             "minutes": max(0, int((stamp - (row["since"] or stamp)) / 60)),
                             "text": row["stuck_text"]} for row in live if row["stuck"]]
        digest = data.get("digest")
        if isinstance(digest, dict) and digest_min > 0 and not finished:
            payload["digest"] = {"id": "d_demo_1", "at": stamp - int(digest.get("ago") or 0),
                                 "text": digest.get("text") or "",
                                 "minutes": int(digest.get("minutes") or 0),
                                 "workers": digest.get("workers") or [],
                                 "done": int(digest.get("done") or 0),
                                 "total": int(digest.get("total") or 0)}
        return payload

    def _stage_for_room(self, room_id, stage) -> dict:
        """Stage по этапам комнаты: чужие этапы из фикстуры не показываем.

        Демо должно вести себя как сервер: у комнаты без этапов полосы нет,
        у комнаты со своими этапами — её заголовки и её порядок (по id).
        """
        try:
            room = self.room(str(room_id or ""))
        except Exception:  # noqa: BLE001 — карта не должна ронять демо
            room = {}
        declared = room.get("phases") if isinstance(room.get("phases"), list) else []
        meta = {p.get("id"): p for p in ((stage or {}).get("phases") or []) if isinstance(p, dict)}
        phases = []
        for item in declared:
            got = meta.get(item.get("id")) or {}
            phases.append({"id": item.get("id"), "title": item.get("title"),
                           "status": got.get("status") or "next", "note": got.get("note") or ""})
        ids = {p["id"] for p in phases}
        current = (stage or {}).get("current")
        if current not in ids:
            active = next((p["id"] for p in phases if p["status"] in ("active", "blocked")), None)
            current = active or (phases[0]["id"] if phases else None)
        return {"current": current, "phases": phases}

    def reactions_of(self, rid: str) -> list:
        """Реакции руководителя в запросе (W20-U4, P9) — из готовой фикстуры.

        Форма элемента та же, что у сервера: ``{at, kind, text}``; ``kind`` — на что
        отреагировали (ответ, шаг, решение). В демо отдельной доски нет, поэтому
        источник — деталь запроса.
        """
        out = []
        try:
            detail = self.detail(rid)
        except Exception:  # noqa: BLE001 - бейдж не важнее сводки
            return out
        for key, kind in (("open_questions", "answer"), ("next_steps", "step"),
                          ("decisions", "decision")):
            for item in detail.get(key) or []:
                if not isinstance(item, dict):
                    continue
                reaction = item.get("reaction")
                if not isinstance(reaction, dict):
                    continue
                at = int(reaction.get("at") or 0)
                if not at and not reaction.get("status"):
                    continue
                out.append({"at": at, "kind": kind,
                            "text": str(reaction.get("text") or reaction.get("label") or "")})
        out.sort(key=lambda item: item["at"], reverse=True)
        return out

    def reactions_summary(self, rid: str) -> dict:
        """``{count, last_at}`` по запросу — для сводки комнат и бейджа вкладки."""
        items = self.reactions_of(rid)
        return {"count": len(items),
                "last_at": max([item["at"] for item in items], default=0)}

    def rooms_summary(self, query: dict) -> dict:
        """Счётчики и последние запросы всех комнат одним ответом (W17-B §1)."""
        raw = (query.get("requests") or ["3"])[0].strip()
        try:
            per_room = int(raw)
        except (TypeError, ValueError):
            raise ApiError(400, "bad_request", "requests: число от 0 до 10", {"field": "requests"})
        if not 0 <= per_room <= 10:
            raise ApiError(400, "bad_request", "requests: от 0 до 10", {"field": "requests"})
        include = (query.get("include_archived") or ["0"])[0] in ("1", "true", "yes")
        only = (query.get("room") or [""])[0].strip()
        rooms = []
        for rid in self.room_details:
            room = self.room(rid)
            if only and rid != only:
                continue
            if not include and room.get("archived"):
                continue
            summaries = self.summaries(rid)
            # W20-U3 (P13): закреплённые запросы — сверху «Недавнего».
            try:
                summaries = sorted(summaries, key=lambda it: (bool(self.pin_state(it["id"])["pinned"]),
                                                              int(it.get("created_at") or 0)),
                                   reverse=True)
            except Exception:  # noqa: BLE001 - без закреплений порядок прежний
                pass
            rows = []
            for item in summaries[:per_room]:
                pin = {"pinned": False, "pinned_at": None}
                try:
                    pin = self.pin_state(item["id"])
                except Exception:  # noqa: BLE001 - флаг необязателен
                    pass
                rows.append({"id": item["id"], "room": rid, "title": item.get("title") or "",
                             "title_short": item.get("title_short") or short_title(item.get("title") or ""),
                             "title_source": item.get("title_source") or "derived",
                             "title_locked": bool(item.get("title_locked")),
                             "status": item.get("status"), "needs_you": bool(item.get("needs_you")),
                             "created_at": item.get("created_at"), "finished_at": item.get("finished_at"),
                             "preview": item.get("preview") or "",
                             "pinned": bool(pin["pinned"]), "pinned_at": pin["pinned_at"]})
            last = rows
            entry = dict(self.room_summary(rid))
            # W20-U4 (P9): реакции руководителя по комнате — время и по запросам.
            # «Прочитано» живёт в браузере, поэтому отдаём только факты (как сервер).
            reaction_map = {}
            for item in summaries:
                info = self.reactions_summary(item["id"])
                if info["count"]:
                    reaction_map[item["id"]] = info
            entry.update({
                "reactions_unseen": sum(info["count"] for info in reaction_map.values()),
                "reactions_total": sum(info["count"] for info in reaction_map.values()),
                "last_reaction_at": max([info["last_at"] for info in reaction_map.values()], default=0) or None,
                "reactions": reaction_map,
                "requests_count": len(summaries),
                "last_at": max([item.get("created_at") or 0 for item in summaries], default=0) or None,
                "last": last})
            rooms.append(entry)
        if only and not rooms:
            raise ApiError(404, "not_found", "Комната не найдена", {"room": only})
        return {"ok": True, "at": now(), "count": len(rooms),
                "engine": {"ok": True, "board": "team", "warning": None}, "rooms": rooms}

    def dispatcher_preview(self) -> dict:
        """Состояние доски чтением, без POST и без CLI (W17-B §1)."""
        return {"ok": True, "preview": True, "report": self.kick_report(True), "at": now()}

    def list_requests(self, room_id: str, query: dict) -> dict:
        self.room(room_id)
        items = self.summaries(room_id)
        statuses = [s for s in query.get("status", [""])[0].split(",") if s]
        known = {"queued", "working", "reviewing", "synthesizing", "done", "needs_you", "failed"}
        bad = [s for s in statuses if s not in known]
        if bad:
            raise ApiError(400, "bad_request", "Неизвестный статус запроса",
                           {"status": bad[0], "allowed": sorted(known)})
        if statuses:
            items = [s for s in items if s["status"] in statuses]
        q = query.get("q", [""])[0].strip().casefold()
        if q:
            items = [s for s in items if q in s["title"].casefold() or q in s["preview"].casefold()]
        try:
            limit = int(query.get("limit", ["20"])[0])
        except ValueError:
            raise ApiError(400, "bad_request", "limit должен быть числом", {"field": "limit"})
        if not 1 <= limit <= 100:
            raise ApiError(400, "bad_request", "limit вне диапазона 1..100", {"limit": limit})
        return {"items": items[:limit], "next_cursor": None, "has_more": len(items) > limit}

    def candidates_for(self, room_id: str) -> dict:
        """Кандидаты на привязку: карточки с decomposed вне реестра запросов комнаты."""
        self.room(room_id)
        data = self.candidates.get(room_id)
        if not data:
            return {"room": room_id, "generated_at": now(), "count": 0, "items": []}
        items = copy.deepcopy(data["items"])
        return {"room": room_id, "generated_at": data.get("generated_at") or now(),
                "count": len(items), "items": items}

    def label_fields(self, profile: str, custom: str = "") -> dict:
        """Поля подписи участника в демо (WAVE13 §11) — как отдаёт живой сервер."""
        known = {p.get("profile"): p for p in self.fx.get("profiles.json", {}).get("items", [])}
        item = known.get(profile) or {}
        if custom:
            name, job = split_label(custom)
            text = " — ".join(x for x in (name, job) if x)
            return {"name": name, "job": job, "label": text or custom, "label_source": "member",
                    "custom_label": custom, "label_pending": False}
        return {"name": str(item.get("name") or ""), "job": str(item.get("job") or ""),
                "label": str(item.get("label") or ""),
                "label_source": str(item.get("label_source") or "role"),
                "custom_label": "", "label_pending": False}

    def member_label(self, room: dict, profile: str) -> str:
        """Ручная подпись участника из реестра комнаты демо.

        WAVE17 B-17: у руководителя и критика подпись хранится не в `members`,
        а в карте `role_labels` (`{профиль: подпись}`) — демо ведёт себя как
        живой сервер, иначе кнопка «Подпись» у них «не сохранялась» бы.
        """
        for member in room.get("members") or []:
            if str(member.get("profile") or "") == str(profile):
                return str(member.get("label") or "")
        return str((room.get("role_labels") or {}).get(str(profile)) or "")

    def team(self, room_id: str) -> dict:
        room = self.room(room_id)
        if room["id"] not in ("cafe", "fn"):
            members = []
            for m in room["members"]:
                item = {"profile": m["profile"], "title": m["profile"], "role": m["role"], "note": m["note"],
                        "pinned": m["pinned"], "state": "idle", "current": None, "last": None,
                        "counts": {"done": 0, "blocked": 0, "active": 0}}
                item.update(self.label_fields(m["profile"], self.member_label(room, m["profile"])))
                members.append(item)
            return {"room": room["id"], "generated_at": now(),
                    "dispatcher": {"ok": True, "state": "running", "pid": 9608, "warning": None},
                    "members": members}
        data = self.fixture("team_%s.json" % room["id"])
        data["generated_at"] = now()
        # Подпись, заданная в демо кнопкой «Подпись», должна быть видна в «Команде».
        for member in data.get("members") or []:
            custom = self.member_label(room, member.get("profile"))
            if custom:
                member.update(self.label_fields(member.get("profile"), custom))
            # W19 B-28: «Закрепить в составе / Открепить» меняет флаг в реестре комнаты —
            # карточка в демо должна показать это сразу.
            row = next((m for m in room.get("members") or []
                        if m.get("profile") == member.get("profile")), None)
            if row is not None and "pinned" in row:
                member["pinned"] = bool(row.get("pinned"))
            # W23-MG2: «Остановить / Продолжить» в карточке участника — состояние
            # карточки живёт в памяти мока (как реестр бэкенда), «Команда» его видит.
            current = member.get("current")
            if isinstance(current, dict) and str(current.get("status")) in ("running", "review"):
                current["started_at"] = self._recent_start(current.get("started_at"))
            states = getattr(self, "card_states", None) or {}
            if isinstance(current, dict):
                patch = states.get(str(current.get("task_id") or ""))
                if isinstance(patch, dict):
                    if patch.get("status"):
                        current["status"] = patch["status"]
                        # W23-MG2: остановленная карточка — «остановлено», а не «работает».
                        member["state"] = "paused" if patch["status"] == "paused" else "working"
                    current["stopped"] = patch.get("stopped")
        return data

    # --- «Контроль моделей» (W23-MG2) ---------------------------------------

    #: Демо-события сторожа моделей по комнатам: что остановили и что исправили.
    #: Всё вымышлено: суммы и модели — демо, не факт проекта.
    GUARD_DEMO = {
        "fn": (
            {"kind": "fixed", "card_id": "t_8c21ab77", "profile": "coder",
             "expected": {"provider": "lokal", "model": "dilizhans-kompakt"},
             "actual": {"provider": "oblako-a", "model": "dilizhans-ultra"},
             "cost_usd": None, "ago": 5400,
             "text": "Модель карточки исправлена до запуска: Дилижанс ультра (Облако-А) → "
                     "локальная Дилижанс компакт"},
            {"kind": "stopped", "card_id": "t_7b0c55a1", "profile": "coder",
             "expected": {"provider": "lokal", "model": "dilizhans-kompakt"},
             "actual": {"provider": "oblako-a", "model": "dilizhans-ultra"},
             "cost_usd": 2.45, "ago": 900,
             "text": "Запуск шёл на Дилижанс ультра (Облако-А) вместо локальной Дилижанс компакт — "
                     "остановлено и перезапущено на Дилижанс компакт"},
        ),
        # W28-C2: демо «ошибки, которых нет» — убитая контролем попытка карточки
        # `request_done.json` (t_1000a001). В запросе — спокойная строка и ссылка сюда.
        "cafe": (
            {"kind": "stopped", "card_id": "t_1000a001", "profile": "team-lawyer",
             "expected": {"provider": "ollama-cloud", "model": "deepseek-v4.1-flash:1017"},
             "actual": {"provider": "ollama-cloud", "model": "glm-5.3-flash"},
             "cost_usd": None, "ago": 3600,
             "text": "Запуск шёл на glm-5.3-flash вместо deepseek-v4.1-flash:1017 — "
                     "остановлено и перезапущено на выбранной модели"},
        ),
    }

    def guard_events(self, room_id: str) -> list:
        """События контроля моделей комнаты (демо): от старых к новым, как в журнале."""
        rows = self.GUARD_DEMO.get(str(room_id)) or ()
        moment = now()
        out = []
        for row in rows:
            item = dict(row)
            item["at"] = moment - int(item.pop("ago", 0) or 0)
            out.append(item)
        return out

    def guard_mode(self, room_id: str) -> str:
        room = self.room(room_id)
        mode = str(room.get("model_guard") or "").strip().lower()
        return mode if mode in ("enforce", "warn") else "enforce"

    @staticmethod
    def _same_model(a: dict, b: dict) -> bool:
        """Одна и та же модель: регистр и `провайдер:модель` не считаются."""
        def norm(value):
            raw = str(value or "").strip().lower()
            if ":" in raw:
                left, right = raw.split(":", 1)
                if left and right and not right[:1].isdigit():
                    raw = right
            return raw
        left, right = norm(a.get("model")), norm(b.get("model"))
        if not left or not right or left != right:
            return False
        pa, pb = str(a.get("provider") or "").lower(), str(b.get("provider") or "").lower()
        return not (pa and pb and pa != pb)

    def model_check(self, room_id: str) -> dict:
        """Сверка «куда собирается уйти запрос» — демо по данным /team и комнаты.

        Ожидаемая модель: настройка комнаты (`agent_models`), иначе модель профиля
        из каталога агентов. Фактическая — из `model_sent` последнего запуска.
        «Ждёт запуска» (модель сменили позже) — не расхождение: W19-B-29.
        """
        room = self.room(room_id)
        team = self.team(room_id)
        defaults = (self.fx.get("models.json") or {}).get("agent_defaults") or {}
        members = []
        for item in team.get("members") or []:
            profile = str(item.get("profile") or "")
            picked = (room.get("agent_models") or {}).get(profile) or {}
            if picked.get("model"):
                expected = {"provider": str(picked.get("provider") or ""),
                            "model": str(picked.get("model")), "source": "room"}
            else:
                fallback = defaults.get(profile) or {}
                expected = ({"provider": str(fallback.get("provider") or ""),
                             "model": str(fallback.get("model")), "source": "profile"}
                            if fallback.get("model") else {"provider": "", "model": "", "source": "none"})
            settings = item.get("model_settings") or {}
            card_model = ({"provider": str(settings.get("provider") or ""),
                           "model": str(settings.get("model") or ""),
                           "source": str(settings.get("source") or "room")}
                          if settings.get("model") else None)
            sent = item.get("model_sent") or {}
            last_run = None
            if sent.get("model"):
                last_run = {"provider": str(sent.get("provider") or ""),
                            "model": str(sent.get("model")),
                            "at": int(sent.get("at") or 0),
                            "cost_usd": float(sent.get("cost_usd") or 0)}
            ok = None
            problem = None
            if expected["model"]:
                if not last_run:
                    ok = None
                elif sent.get("pending"):
                    ok = True            # модель сменили позже запуска — ждём следующей карточки
                else:
                    ok = self._same_model(expected, last_run)
                if ok is False:
                    problem = "ожидалась %s (%s), запущено %s (%s)" % (
                        expected["model"], expected["provider"] or "—",
                        last_run["model"], last_run["provider"] or "—")
            else:
                problem = "модель участника не задана — контроль не вмешивается"
            members.append({
                "profile": profile,
                "title": str(item.get("title") or profile),
                "expected": expected,
                "card_model": card_model,
                "last_run": last_run,
                "ok": ok,
                "problem": problem,
            })
        events = self.guard_events(room_id)
        fixed = sum(1 for ev in events if ev.get("kind") == "fixed")
        stopped = sum(1 for ev in events if ev.get("kind") == "stopped")
        saved = sum(float(ev.get("cost_usd") or 0) for ev in events if ev.get("kind") == "stopped")
        open_now = sum(1 for m in members if m.get("ok") is False)
        mode = self.guard_mode(room_id)
        return {
            "room": str(room.get("id") or room_id),
            "members": members,
            "guard": {"mode": mode, "open": open_now, "fixed_24h": fixed,
                      "stopped_24h": stopped, "saved_usd_est": round(saved, 2),
                      "text": ("Строгий контроль: " if mode == "enforce" else "Только предупреждения: ")
                              + _ru_count(fixed, "исправление", "исправления", "исправлений")
                              + " и " + _ru_count(stopped, "остановка", "остановки", "остановок")
                              + " за сутки"},
            "warnings": [],
        }


    def roadmap(self, room_id: str) -> dict:
        """Карта проекта: этапы — свойство комнаты (W17-R B-8), не список в коде.

        В демо этапы есть только у комнаты, которая их задала (fixture
        `roadmap_cafe.json`). У остальных полоса этапов пустая — как в жизни.
        Предложение этапов (W20 §1.7) отдаётся всегда, если оно есть у комнаты.
        """
        room = self.room(room_id)
        rules = phase_rules()
        proposal = rules.norm_proposal(room.get("phases_proposal")) if rules else None
        extra = {"phases_proposal": proposal,
                 "proposal_text": rules.state_text(room.get("phases_proposal")) if rules else ""}
        if room["id"] == "cafe":
            base = self.fixture("roadmap_cafe.json")
            stored = {p.get("id"): p for p in base.get("phases") or [] if isinstance(p, dict)}
            # W20 §1.7: этапы комнаты — главный список. Правки человека (переименование,
            # порядок, удаление) видны сразу; из фикстуры берём только статусы и
            # пояснения к тем этапам, которые остались (их не переписываем).
            declared = room.get("phases")
            if not isinstance(declared, list) or not declared:
                declared = base.get("phases") or []
            phases = []
            for item in declared:
                if not isinstance(item, dict):
                    continue
                meta = dict(stored.get(item.get("id")) or {})
                meta.update({"id": item.get("id"), "title": item.get("title")})
                meta.setdefault("status", "next")
                meta.setdefault("note", "")
                phases.append(meta)
            view = dict(base, **extra)
            view["phases"] = phases
            active = next((p["id"] for p in phases if p.get("status") in ("active", "blocked")), None)
            current_id = active or (phases[0]["id"] if phases else None)
            view["current"] = next((p for p in phases if p["id"] == current_id), None)
            return view
        # W20-FC §1.7: у комнаты без своей карты этапы всё равно есть — они лежат в
        # самой комнате (принятые из предложения или добавленные человеком).
        declared = room.get("phases") if isinstance(room.get("phases"), list) else []
        phases = []
        for item in declared:
            if not isinstance(item, dict):
                continue
            phases.append({"id": item.get("id"), "title": item.get("title"),
                           "status": "next", "note": "", "updated_at": now()})
        return dict({"room": room["id"], "updated_at": now(),
                     "current": phases[0] if phases else None,
                     "phases": phases, "decisions": []}, **extra)

    def backlog_for(self, room_id: str, query: dict) -> dict:
        self.room(room_id)
        data = self.backlog_data(room_id)          # + недостающие шаги из всех запросов комнаты
        items = copy.deepcopy(data["items"])
        # W17-B §7: статус вопроса — из ответа, близнецы схлопнуты, служебные скрыты
        view = mock_backlog.apply_answers({"room": room_id, "updated_at": data["updated_at"],
                                           "items": items}, self._room_details(room_id))
        items = view.get("items") or items
        statuses = [s for s in query.get("status", [""])[0].split(",") if s]
        kinds = [s for s in query.get("kind", [""])[0].split(",") if s]
        if statuses:
            items = [i for i in items if i["status"] in statuses]
        if kinds:
            items = [i for i in items if i["kind"] in kinds]
        counts = dict(view.get("counts") or {})
        if statuses or kinds:
            counts = backlog_counts(items)
        out = {"room": room_id, "updated_at": data["updated_at"], "counts": counts, "items": items}
        if view.get("sections"):
            out["sections"] = view["sections"]
        if view.get("hidden_service"):
            out["hidden_service"] = view["hidden_service"]
        # W20 §1.7: предложение этапов ждёт решения — строка в «От вас нужно» (в ответе, не в файле).
        rules = phase_rules()
        proposal = rules.norm_proposal(self.room(room_id).get("phases_proposal")) if rules else None
        if proposal and proposal.get("state") == "proposed":
            titles = [item.get("title") or "" for item in proposal.get("items") or []]
            item = {"id": "phases_proposal", "kind": "phases_proposal", "status": "open",
                    "text": "Руководитель предлагает этапы проекта: " + ", ".join(titles[:4])
                            + ("…" if len(titles) > 4 else ""),
                    "why": "Примите все, выберите нужные или отклоните — этапы останутся вашими.",
                    "source_title": self.room(room_id).get("title") or room_id,
                    "source_request": None, "owner": self.room(room_id).get("orchestrator"),
                    "due": None, "created_at": int(proposal.get("at") or 0),
                    "updated_at": int(proposal.get("at") or 0),
                    "count": len(titles), "titles": titles,
                    "by": str(proposal.get("by") or ""),
                    "proposal_text": rules.state_text(proposal)}
            out["proposal_item"] = item
            out["items"] = [item] + out["items"]
            if out.get("sections"):
                out["sections"] = dict(out["sections"],
                                       inbox=[item] + list(out["sections"].get("inbox") or []),
                                       proposal=[item])
        else:
            out["proposal_item"] = None
        return out

    def journal(self, room_id: str, query: dict) -> dict:
        self.room(room_id)
        data = self.fixture("journal_cafe.json") if room_id == "cafe" else {
            "room": room_id, "next_cursor": None, "has_more": False, "items": []}
        q = query.get("q", [""])[0].strip().casefold()
        events = self.models_journal.get(room_id, []) + data["items"]   # «модель изменена» — сверху
        items = [e for e in events if not q or q in e["text"].casefold()]
        try:
            limit = int(query.get("limit", ["50"])[0])
        except ValueError:
            raise ApiError(400, "bad_request", "limit должен быть числом", {"field": "limit"})
        if not 1 <= limit <= 200:
            raise ApiError(400, "bad_request", "limit вне диапазона 1..200", {"limit": limit})
        return {"room": room_id, "items": items[:limit], "next_cursor": None, "has_more": len(items) > limit}

    def search(self, query: dict) -> dict:
        q = query.get("q", [""])[0].strip()
        if len(q) < 2:
            raise ApiError(400, "bad_request", "Поисковый запрос короче 2 символов", {"field": "q"})
        room = query.get("room", [""])[0] or None
        if room and room not in {r["id"] for r in self.rooms}:
            raise ApiError(404, "not_found", "Комната не найдена", {"room": room})
        data = self.fixture("search.json")
        items = [h for h in data["items"] if (not room or h["room"] == room)
                 and (q.casefold() in h["title"].casefold() or q.casefold() in h["snippet"].casefold())]
        return {"q": q, "room": room, "total": len(items), "items": items}

    def ics(self, query: dict) -> tuple:
        room_id = query.get("room", [""])[0]
        if not room_id:
            raise ApiError(400, "bad_request", "Нужен параметр room", {"field": "room"})
        self.room(room_id)
        raw = query.get("items", [""])[0]
        ids = [unquote(x) for x in raw.split(",") if x]
        all_flag = query.get("all", ["0"])[0] in ("1", "true", "yes")
        if not ids and not all_flag:
            raise ApiError(400, "bad_request", "Укажите items=<id,...> или all=1", {"field": "items"})
        items = self.backlog.get(room_id, {"items": []})["items"]
        chosen = [i for i in items if (i["id"] in ids or (all_flag and i["status"] == "open" and i["due"]))]
        if ids and not chosen:
            raise ApiError(404, "not_found", "Элементы бэклога не найдены", {"items": ids})
        lines = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//Team Room//Mock 0.1//RU",
                 "CALSCALE:GREGORIAN", "METHOD:PUBLISH"]
        for it in chosen:
            day = it["due"].replace("-", "")
            lines += ["BEGIN:VEVENT", "UID:%s-%s@team-room.local" % (room_id, re.sub(r"[^\w:-]", "_", it["id"])),
                      "DTSTAMP:%sZ" % time.strftime("%Y%m%dT%H%M%S", time.gmtime()),
                      "DTSTART;VALUE=DATE:%s" % day, "SUMMARY:%s" % ics_escape(it["text"][:200]),
                      "DESCRIPTION:%s" % ics_escape("Владелец: %s. Источник: %s" % (it.get("owner") or "—", it["source_title"])),
                      "END:VEVENT"]
        lines.append("END:VCALENDAR")
        return 200, Raw("\r\n".join(lines) + "\r\n", "text/calendar; charset=utf-8",
                        {"Content-Disposition": 'attachment; filename="%s.ics"' % room_id})

    def export(self, rid: str, fmt: str) -> tuple:
        detail = self.detail(rid)
        if detail.get("synthesis", {}).get("state") != "ready":
            raise ApiError(409, "conflict", "Ответ ещё не готов — экспортировать нечего",
                           {"reason": "synthesis_pending", "rid": rid})
        md = export_markdown(detail)
        filename = "%s.%s" % (rid, fmt)
        if fmt == "md":
            return 200, Raw(md, "text/markdown; charset=utf-8",
                            {"Content-Disposition": 'attachment; filename="%s"' % filename})
        page = export_html(detail, md)
        return 200, Raw(page, "text/html; charset=utf-8",
                        {"Content-Disposition": 'attachment; filename="%s"' % filename})

    def resolve_shared_path(self, raw: str) -> str:
        """Путь из белого списка API §2: 5 корней + расширение + чёрный список имён."""
        path = os.path.realpath(raw)
        roots = list(FILE_ROOTS) + list(getattr(self, "extra_roots", []) or [])
        if any(inside(private, path) for private in MOCK_PRIVATE):
            raise ApiError(403, "forbidden", "Исходники и фикстуры мока не отдаются",
                           {"reason": "outside_roots", "path": raw})
        if not any(inside(root, path) for root in roots if root):
            raise ApiError(403, "forbidden", "Файл вне разрешённых корней",
                           {"reason": "outside_roots", "path": raw})
        low = os.path.basename(path).lower()
        parts = [p.lower() for p in path.split(os.sep)[-4:]]
        if any(bad in part for part in parts for bad in DENY_NAME_PARTS) or \
                any(bad in low for bad in DENY_NAME_PARTS):
            raise ApiError(403, "forbidden", "Файл в чёрном списке", {"reason": "blacklisted", "path": raw})
        ext = os.path.splitext(path)[1].lower()
        if ext not in ALLOWED_EXT:
            raise ApiError(403, "forbidden", "Расширение файла не разрешено", {"reason": "outside_roots",
                                                                              "path": raw, "ext": ext})
        if not os.path.isfile(path):
            raise ApiError(404, "not_found", "Файл удалён или перемещён", {"path": raw})
        return path

    def file_response(self, query: dict) -> tuple:
        raw = query.get("path", [""])[0]
        if not raw:
            raise ApiError(400, "bad_request", "Нужен параметр path", {"field": "path"})
        path = self.resolve_shared_path(raw)
        ext = os.path.splitext(path)[1].lower()
        ctype = MIME.get(ext, mimetypes.guess_type(path)[0] or "application/octet-stream")
        name = os.path.basename(path)
        if ext in OFFICE_EXT:
            # RFC 5987: кириллица в имени не влезает в latin-1 заголовок
            disp = ('attachment; filename="%s"' % name if name.isascii() else
                    "attachment; filename=\"file%s\"; filename*=UTF-8''%s" % (ext, quote(name)))
        else:
            disp = "inline"
        headers = {"Content-Disposition": disp}
        if ext == ".svg":
            headers["Content-Security-Policy"] = "sandbox"   # API §6.6 / §2
        with open(path, "rb") as fh:
            body = fh.read()
        return 200, Raw(body, ctype, headers)

    # --- W27-E6: «Уровень автономии» комнаты (контракт §2) -------------------
    # Демо ничего не пишет в профили участников: уровень и список «только с
    # моего да» живут в памяти процесса, как и остальное в моке. Формы ответов —
    # ровно контракт §2, чтобы интерфейс проверялся на том, что отдаст сервер.
    AUTONOMY_LEVELS = ("careful", "working", "free")
    #: классы «свои скрипты» — их включает рабочий уровень
    AUTONOMY_SCRIPT_CLASSES = ["script execution via -e/-c flag", "script execution via heredoc"]
    #: маски «только с моего да» по умолчанию (тот же список у сервера)
    AUTONOMY_NEVER_DEFAULTS = [
        "удаление папки целиком",
        "удаление файлов с подтверждением «всё равно»",
        "отправка писем и сообщений",
        "платежи и деньги",
        "права администратора",
        "реестр и службы Windows",
        "остановка процессов",
        "контейнеры и их хранилища",
        "скачивание и сразу запуск",
        "запись в файлы с паролями и ключами",
    ]
    #: разрушительные классы: в журнале они получают severity=critical
    AUTONOMY_CRITICAL_WORDS = ("recursive", "registry", "sudo", "taskkill", "docker", "curl", "rm -rf")

    def _autonomy_names(self) -> dict:
        """Имена участников для подписей («Анна», «Лев») из справочника профилей."""
        try:
            data = self.fixture("profiles.json")
        except Exception:      # noqa: BLE001 — подпись не повод падать
            return {}
        out = {}
        for item in (data.get("items") or []):
            if not isinstance(item, dict):
                continue
            profile = str(item.get("profile") or "")
            name = str(item.get("name") or "").strip()
            if profile and name:
                out[profile] = name
        return out

    def _autonomy_people(self, room: dict) -> list:
        """Участники комнаты одной строкой: руководитель, критик, исполнители."""
        names = self._autonomy_names()
        profiles = [room.get("orchestrator"), room.get("critic")]
        profiles += [m.get("profile") for m in (room.get("members") or []) if isinstance(m, dict)]
        out, seen = [], set()
        for raw in profiles:
            profile = str(raw or "")
            if not profile or profile in seen:
                continue
            seen.add(profile)
            out.append({"profile": profile, "member_title": names.get(profile, profile)})
        return out

    def autonomy_state(self, room_id: str) -> dict:
        """Текущий уровень комнаты (память демо; у новой комнаты — «Рабочий»)."""
        self.room(room_id)
        store = getattr(self, "autonomy_demo", None)
        if not isinstance(store, dict):
            store = {}
            self.autonomy_demo = store
        if room_id not in store:
            room = self.room(room_id)
            saved = room.get("autonomy") if isinstance(room.get("autonomy"), dict) else {}
            level = str(saved.get("level") or "")
            if level not in self.AUTONOMY_LEVELS:
                level = "working"
            never = saved.get("never")
            never = [" ".join(str(x).split()) for x in never if str(x or "").strip()] \
                if isinstance(never, list) else []
            store[room_id] = {"level": level, "never": never or list(self.AUTONOMY_NEVER_DEFAULTS),
                              "updated_at": int(saved.get("updated_at") or (now() - 3 * 86400))}
        return store[room_id]

    def _autonomy_profiles(self, room: dict, level: str, never: list) -> list:
        """Что будет записано участникам на этом уровне (ровно, без догадок)."""
        rows = []
        for person in self._autonomy_people(room):
            if level == "careful":
                allow, mode, deny = [], "deny", []
            elif level == "free":
                allow, mode, deny = list(self.AUTONOMY_SCRIPT_CLASSES), "approve", list(never)
            else:
                allow, mode, deny = list(self.AUTONOMY_SCRIPT_CLASSES), "deny", []
            rows.append({"profile": person["profile"], "member_title": person["member_title"],
                         "allowlist": allow, "single_query_mode": mode, "deny": deny})
        return rows

    def _autonomy_preview(self, room: dict, level: str, never: list) -> dict:
        """«Что изменится» простыми словами — до подтверждения."""
        people = self._autonomy_people(room)
        if level == "careful":
            changes = [
                "любой запуск скрипта или команды участник снова спрашивает у вас",
                "постоянные разрешения, которые добавила Комната, снимаются",
                "чтение, поиск и заметки в папке комнаты остаются без вопросов",
            ]
            note = "Так комната работает «как раньше»: спокойно, но с вопросом на каждый запуск."
        elif level == "free":
            changes = [
                "почти все действия участники делают сами",
                "останавливается только список «только с моего да» (%d)" % len(never),
                "если действие запрещено, участник напишет вам вопрос в карточке — "
                "«спросить» на этом уровне некому",
            ]
            note = "Держите список «только с моего да» полным: на этом уровне в нём вся защита."
        else:
            changes = [
                "свои скрипты участники запускают сами — без вопроса",
                "удаление, отправка наружу, реестр, службы и права администратора остаются вопросами",
                "всё, что вне папки комнаты, участник спросит у вас",
                "при первой остановке участник получит подсказку: скрипт — файлом, а не одной строкой",
            ]
            note = "Рекомендуемый уровень: обычная работа идёт без вас, опасное — с вашего «да»."
        return {"changes": changes, "note": note, "affects": len(people)}

    def autonomy_payload(self, room_id: str) -> dict:
        """GET /api/rooms/{id}/autonomy (контракт §2)."""
        room = self.room(room_id)
        state = self.autonomy_state(room_id)
        level = state["level"]
        never = list(state["never"])
        return {
            "room": room["id"],
            "level": level,
            "never": never,
            "updated_at": state["updated_at"],
            "profiles": self._autonomy_profiles(room, level, never),
            "defaults": {"never": list(self.AUTONOMY_NEVER_DEFAULTS)},
            "preview": {lvl: self._autonomy_preview(room, lvl, never) for lvl in self.AUTONOMY_LEVELS},
        }

    def autonomy_journal(self, room_id: str) -> dict:
        """GET /api/rooms/{id}/autonomy/journal — что сделано само и что остановлено.

        Остановленные строки берутся из НАСТОЯЩИХ ожидающих разрешений комнаты:
        нажатие «Разрешить один раз» в демо действительно снимает блокировку.
        """
        room = self.room(room_id)
        names = {p["profile"]: p["member_title"] for p in self._autonomy_people(room)}
        stamp = now()
        items = []
        if room["id"] == "cafe":
            own = [
                ("team-ops", "свои скрипты", 42 * 60, "Собрать отчёт по упаковке за неделю", "t_a1b2c3d4"),
                ("team-cfo", "свои скрипты", 3 * 3600, "Юнит-экономика набора из 6 суфле", "t_3b7e9d11"),
                ("team-runner", "чтение файлов в папке комнаты", 5 * 3600,
                 "Дорожная карта проекта: где мы сейчас", "t_a1b2c3d4"),
                ("team-lawyer", "свои скрипты", 26 * 3600, "Разбор договора поставки упаковки", "t_5d0c11a2"),
            ]
            for profile, cls, ago, card, rid in own:
                items.append({"id": "aj_self_" + profile, "kind": "self", "room": room["id"],
                              "profile": profile, "member_title": names.get(profile, profile),
                              "class": cls, "severity": "routine", "at": stamp - ago,
                              "card": card, "request_id": rid})
        for rid, detail in self.requests.items():
            if detail.get("room") != room["id"]:
                continue
            for approval in self._approvals_for(rid):
                if approval.get("state") != "pending":
                    continue
                pattern = str(approval.get("pattern_key") or "").lower()
                critical = any(word in pattern for word in self.AUTONOMY_CRITICAL_WORDS)
                item = {
                    "id": str(approval.get("id") or ""),
                    "approval_id": str(approval.get("id") or ""),
                    "kind": "blocked",
                    "room": room["id"],
                    "profile": approval.get("profile"),
                    "member_title": names.get(str(approval.get("profile")),
                                              approval.get("member_title") or approval.get("profile")),
                    "class": approval.get("description") or approval.get("pattern_key"),
                    "severity": "critical" if critical else "routine",
                    "at": int(approval.get("at") or stamp),
                    "card": approval.get("card_title"),
                    "request_id": rid,
                    "command": approval.get("command"),
                    # W27-E6: жёсткий запрет нельзя «разрешить» — интерфейс обязан
                    # показать это честно, иначе кнопка ведёт в пустоту.
                    "floor": bool(approval.get("floor")),
                    "grantable": approval.get("grantable") is not False,
                    "actions": list(approval.get("actions") or []),
                }
                if critical:
                    item["what"] = [
                        "C:/Work/Rooms/demo/архив/упаковка — 9 файлов",
                        "C:/Work/Rooms/demo/архив/сметы — 5 файлов",
                        "всего 14 файлов, 1,8 МБ",
                        "после удаления вернуть нельзя",
                    ]
                items.append(item)
        counts = {"self": sum(1 for i in items if i["kind"] == "self"),
                  "blocked_critical": sum(1 for i in items if i["severity"] == "critical"),
                  "blocked_routine": sum(1 for i in items
                                         if i["kind"] == "blocked" and i["severity"] == "routine")}
        return {"room": room["id"], "items": items, "counts": counts, "at": now()}

    def autonomy_suggest(self, room_id: str) -> dict:
        """GET /api/rooms/{id}/autonomy/suggest — «вы разрешали 22 раза»."""
        self.room(room_id)
        state = self.autonomy_state(room_id)
        taken = {str(x).lower() for x in state["never"]}
        rows = [
            {"key": "script execution via -e/-c flag", "label": "свои скрипты одной строкой",
             "count": 22, "destructive": False},
            {"key": "script execution via heredoc", "label": "свои скрипты файлом через терминал",
             "count": 10, "destructive": False},
            {"key": "чтение файлов в папке комнаты", "label": "чтение файлов в папке комнаты",
             "count": 7, "destructive": False},
        ]
        return {"room": room_id, "ok": True, "source": "approvals suggest",
                "items": [r for r in rows if r["label"].lower() not in taken], "at": now()}
    # --- POST -------------------------------------------------------------

