# -*- coding: utf-8 -*-
"""POST/PATCH/DELETE-обработчики мок-сервера: живые запросы, действия, комнаты, бэклог."""
from __future__ import annotations

import copy
import hashlib
import os
import re
from urllib.parse import unquote

from mock_core import (MAX_ATTACH, MAX_NOTE, MAX_TEXT, ROOT, ApiError, backlog_counts, compute_needs_you,
                       detail_to_summary, now, short_title, slugify)
from mock_versions import normalize_v2
import mock_backlog
import mock_delos
import mock_notes
import mock_duties_fe   # W27-E8: дежурства комнаты
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
#: W20 §1.7: этапы проекта — те же правила, что у сервера (без второго мнения о решениях)
import phase_book as mock_phases          # noqa: E402

#: W20-W1 §1.6: подписи настроек «Хода работы» — как в server/rooms.py
WORKLOG_MODE_LABELS = {"full": "Подробно (с рассуждениями)",
                       "brief": "Кратко (только действия)", "off": "Выключено"}

# --- W22-AP2: демо «Разрешения агентам» -------------------------------------
# Живая проверка интерфейса на моке: у одного демо-запроса есть запросы
# разрешения (AP1 добавит серверную часть; здесь — только демо-данные, всё
# вымышленное). Сколько запросов показать, задаётся в строке запуска:
#   MOCK_APPROVALS=12 python tools/mock_server.py --port 8854
APPROVAL_DEMO_RIDS = {"t_5c8e4a02"}
# W27-E2: завершённая карточка с 20 ЗАКРЫТЫМИ разрешениями — стенд дефекта 1.
CLOSED_APPROVAL_DEMO_RIDS = {"t_a1b2c3d4"}
_AP_MEMBERS = [("team-ops", "Анна"), ("team-lawyer", "Лев"), ("team-critic", "Марина")]


def _ap_long_command(n: int) -> str:
    """Длинная вымышленная команда с путями без пробелов (проверка вёрстки).

    Путь вымышленный и переносимый: никакого домашнего каталога автора (tests.test_portable
    сканирует исходники на такие пути).
    """
    head = ("cd C:/Projects/cafe-leto/2026/packaging/orders "
            "&& python tools/build_report.py "
            "--input 2026-11-17/orders_full_export_v12.csv "
            "--output C:/Projects/cafe-leto/2026/packaging/reports/final/"
            "packaging_orders_2026_11_17.html")
    tail = "".join(" --extra-param-%d=C:/Projects/cafe-leto/2026/packaging/"
                   "attachments/supplier_romashka_receipt_%02d.csv" % (i + 1, i + 1)
                   for i in range(10))
    text = head + tail
    while len(text) < n:
        text += "\n# проверка шага %d: сверка строк отчёта с выгрузкой" % len(text)
    return text[:n]


def demo_approvals(rid: str) -> list:
    """Демо-запросы разрешения: три по умолчанию, больше — по env.

    W22-ST: первые два — один и тот же участник и одно правило (`heredoc`), поэтому в
    демо видно ровно то, ради чего сделано схлопывание: ОДНА строка «Анна хочет
    выполнить команду · ×3 · команд: 2» с последней командой и раскрываемым списком.
    `execute_code` и жёсткий запрет (`floor`) идут следующими — на них видно плашки.
    """
    try:
        count = max(1, min(24, int(os.environ.get("MOCK_APPROVALS", "3"))))
    except (TypeError, ValueError):
        count = 3
    base = [
        ("terminal", "запуск скрипта через heredoc", "script execution via heredoc", 0, False,
         _ap_long_command(3000), 2),
        # та же причина и тот же участник: разные команды → одна строка со счётчиком
        ("terminal", "запуск скрипта через heredoc", "script execution via heredoc", 0, False,
         "python - <<'EOF'\nimport csv\nprint(len(list(csv.DictReader(open('orders.csv')))))\nEOF", 1),
        ("execute_code", "execute_code — произвольный код на Python", "execute_code", 1, False,
         "import csv, pathlib\nrows = list(csv.DictReader(open('orders_full_export_v12.csv')))\n"
         "print(len(rows))", 1),
        ("terminal", "удаление каталога целиком", "recursive delete", 2, True,
         "rm -rf C:/Projects/cafe-leto/2026/packaging/archive", 1),
    ]
    extra_desc = [
        ("terminal", "чтение служебного файла настроек", "script execution via -e/-c flag"),
        ("terminal", "запись файла отчёта в папку проекта", "script execution via heredoc"),
        ("execute_code", "execute_code — произвольный код на Python", "execute_code"),
        ("terminal", "запуск сборки страницы отчёта", "script execution via -e/-c flag"),
    ]
    items = []
    stamp = now() - 900
    for i in range(count):
        if i < len(base):
            kind, desc, pattern, who, floor, command, repeats = base[i]
        else:
            kind, desc, pattern = extra_desc[i % len(extra_desc)]
            who, floor, repeats = i % 3, False, 1
            command = (_ap_long_command(180 + i * 40) if i % 2 else
                       "python -c \"import json;print(json.load(open('summary.json')))\"")
        profile, member = _AP_MEMBERS[who % len(_AP_MEMBERS)]
        grantable = (not floor) and kind != "execute_code"
        items.append({
            "id": "ap_%08x" % (0x5A17C0DE + i * 7919),
            "task_id": rid,
            "profile": profile,
            "member_title": member,
            "kind": kind,
            "command": command,
            "description": desc,
            "pattern_key": pattern,
            "floor": floor,
            # W22-AP1: execute_code в работе без человека Hermes не разрешает списком —
            # демо говорит правду, а не показывает недоступную кнопку как рабочую.
            "grantable": grantable,
            # W26-D2: вид блокировки — три разных вида строки в интерфейсе.
            "dead_end": "floor" if floor else ("workaround" if not grantable else "grantable"),
            # W26-D1: действия, которые есть у этого вида (счётчик «нужно от вас»
            # считает только те, у кого действие есть).
            "actions": (["ack", "hint_agent"] if floor
                        else ["hint_agent", "trust_profile", "dismiss"] if not grantable
                        else ["once", "always", "deny"]),
            "state": "pending",
            "at": stamp + i * 60,
            "decided_at": None,
            "card_state": "blocked",
            "count": repeats,
            "note": ("жёсткий запрет политики Hermes — разрешить нельзя" if floor
                     else "" if grantable else "такую блокировку снять нельзя — нужен другой способ"),
            "card_title": "Собрать отчёт по упаковке за неделю",
            "expires_at": None,
        })
    return items
def demo_closed_approvals(rid: str, count: int = 20) -> list:
    """Закрытые разрешения завершённой карточки (W27-E2, дефект 1).

    Ровно то, из-за чего пакет и появился: у готового запроса 20 запросов разрешений
    со `state:"closed"` и `closed_reason:"card_finished"`. Раньше интерфейс такого
    состояния не знал, рисовал их как «ждёт вашего решения» и складывал в счётчик —
    у человека выходило «27» и девять красных плашек 409.

    Демо-данные вымышленные: «Контрагент А», пути `C:/Work/Rooms/demo`.
    """
    reasons = [("terminal", "запуск скрипта сборки отчёта", "script execution via heredoc"),
               ("terminal", "чтение выгрузки заказов", "script execution via -e/-c flag")]
    stamps = []
    base = now() - 5400
    items = []
    for i in range(max(1, int(count))):
        kind, desc, pattern = reasons[i % len(reasons)]
        profile, member = _AP_MEMBERS[i % len(_AP_MEMBERS)]
        # 15 «можно разрешить» + 5 «нельзя штатно» — как в боевых данных дефекта.
        workaround = i >= count - 5
        items.append({
            "id": "ap_closed_%03d" % i,
            "task_id": rid,
            "profile": profile,
            "member_title": member,
            "kind": "execute_code" if workaround else kind,
            "command": ("python -c \"import csv;print(sum(1 for _ in csv.DictReader("
                        "open('C:/Work/Rooms/demo/orders.csv'))))\""
                        if workaround else
                        "python tools/build_report.py --input C:/Work/Rooms/demo/orders.csv "
                        "--output C:/Work/Rooms/demo/report.html"),
            "description": desc,
            "pattern_key": "execute_code" if workaround else pattern,
            "floor": False,
            "grantable": not workaround,
            "dead_end": "workaround" if workaround else "grantable",
            "actions": (["hint_agent", "trust_profile", "dismiss"] if workaround
                        else ["once", "always", "deny"]),
            # W27-E2 §1: сервер уже закрыл пункт — решать нечего.
            "state": "closed",
            "closed_reason": "card_finished",
            "closed_at": base + i * 30,
            "at": base + i * 30,
            "decided_at": base + i * 30 + 20,
            "card_state": "done",
            "count": 1,
            "note": "",
            "card_title": "Собрать отчёт по упаковке за неделю",
            "expires_at": None,
        })
        stamps.append(items[-1]["id"])
    return items


def collapse_approvals(items: list) -> list:
    """Схлопнуть одинаковые по причине блокировки (W22-ST) — как сервер.

    Демо-двойник `server/approvals._same_reason`: ждущие запросы одного участника с одним
    правилом в одной карточке становятся одной строкой со счётчиком, последней командой
    и списком `commands[]`. Решённые и записи без правила остаются по одной.
    """
    groups: dict = {}
    out: list = []
    for item in items or []:
        if not isinstance(item, dict):
            continue
        pattern = str(item.get("pattern_key") or "")
        if item.get("state") != "pending" or not pattern:
            out.append(item)
            continue
        key = (str(item.get("task_id") or ""), str(item.get("profile") or ""), pattern)
        groups.setdefault(key, []).append(item)
    for _key, members in groups.items():
        if len(members) == 1:
            out.append(members[0])
            continue
        members = sorted(members, key=lambda row: (int(row.get("at") or 0), str(row.get("id"))),
                         reverse=True)
        base = dict(members[0])
        commands = [{"id": str(row.get("id") or ""), "command": str(row.get("command") or ""),
                     "at": int(row.get("at") or 0), "count": max(1, int(row.get("count") or 1))}
                    for row in members]
        base["group"] = "gr_" + hashlib.sha1("\u0001".join(_key).encode("utf-8")).hexdigest()[:8]
        base["approval_ids"] = [str(c.get("id") or "") for c in commands]
        base["commands"] = commands
        base["command"] = commands[0]["command"]
        base["count"] = sum(row["count"] for row in commands)
        base["at"] = min(int(row.get("at") or 0) for row in members)
        out.append(base)
    return out


DIGEST_LABELS = {0: "Выключено", 1: "Каждую минуту", 2: "Каждые 2 минуты", 5: "Каждые 5 минут"}


def _svod_block(room: dict) -> dict:
    """Готовые подписи живого свода для демо-карточки комнаты (W20-S1 §1.1)."""
    auto = bool(room.get("auto_svod"))
    quiet = int(room.get("auto_svod_quiet_s") or 180)
    gap = int(room.get("auto_svod_min_gap_s") or 600)

    def _minutes(seconds: int) -> str:
        return "%d мин" % (seconds // 60) if seconds % 60 == 0 else "%d с" % seconds

    return {"auto_svod": auto, "auto_svod_quiet_s": quiet, "auto_svod_min_gap_s": gap,
            "auto_svod_label": ("Включено: свод обновляется сам" if auto
                                else "Выключено: свод обновляется кнопкой «Обновить сейчас»"),
            "quiet_label": "не раньше чем через %s тишины" % _minutes(quiet),
            "gap_label": "не чаще раза в %s" % _minutes(gap)}


def svod_defaults(room: dict) -> dict:
    """Поля живого свода у комнаты демо (W20-S1 §1.1).

    У комнат, созданных раньше, полей нет — читаются как «включено, 3 минуты,
    10 минут»; интерфейс показывает то же, что отдал бы боевой сервер.
    """
    if not isinstance(room, dict):
        return room
    room.setdefault("auto_svod", True)
    room.setdefault("auto_svod_quiet_s", 180)
    room.setdefault("auto_svod_min_gap_s", 600)
    room["svod"] = _svod_block(room)
    return room


def _worklog_block(mode, minutes) -> dict:
    """Строка режима «Хода работы» для комнаты в демо (как отдаёт сервер)."""
    name = str(mode or "full").strip().lower()
    if name not in WORKLOG_MODE_LABELS:
        name = "full"
    try:
        every = int(minutes)
    except (TypeError, ValueError):
        every = 2
    if every not in DIGEST_LABELS:
        every = 2
    return {"mode": name, "mode_label": WORKLOG_MODE_LABELS[name],
            "digest_every_min": every, "digest_label": DIGEST_LABELS[every],
            "on": name != "off"}


def apply_decision_overrides(detail: dict, rid: str, overrides: dict) -> dict:
    """Наложить запомненные ответы Михаила на решения собранной детали (W19-D2, B-20).

    Демо-деталь пересобирается на каждый GET, а в бэкенде ответ по решению лежит в
    реестре (`answers[d_x]`). Без наложения «Принять» пропадало после обновления.
    """
    if not isinstance(detail, dict) or not isinstance(overrides, dict):
        return detail
    decisions = detail.get("decisions")
    if not isinstance(decisions, list):
        return detail
    for decision in decisions:
        if not isinstance(decision, dict):
            continue
        record = overrides.get(str(decision.get("id") or ""))
        if isinstance(record, dict):
            view = mock_answers.apply_decision(decision, record, None)
            decision.update(view)
    return detail


def enrich_decisions(detail: dict) -> dict:
    """W19-D2 (B-20, B-21): решения демо-детали — в форму API.

    У решения должны быть `open`, статус с честной подписью (кто решил), действия
    и цепочка ответа — те же поля, что отдаёт сервер (`routes_read._answers_view`).
    """
    if not isinstance(detail, dict):
        return detail
    decisions = detail.get("decisions")
    if not isinstance(decisions, list):
        return detail
    detail["decisions"] = [mock_answers.apply_decision(d, None, d.get("reaction"))
                           if isinstance(d, dict) else d for d in decisions]
    detail.setdefault("decision_actions", {
        "labels": dict(mock_answers.DECISION_ACTION_RU),
        "hints": dict(mock_answers.DECISION_ACTION_HINT),
        "status_labels": dict(mock_answers.DECISION_STATUS_RU),
        "chain_hint": mock_answers.CHAIN_HINT,
    })
    return detail


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
            action = saved["action"]
            mock_answers.apply_step(step, action, None)
            # W21-L1: в демо руководитель «отвечает» на согласие сразу — иначе строку
            # «Шаг учтён» (недоделка U4) на демо-сервере увидеть нечем. На настоящем
            # сервере это его реакция в ветке (`reaction.status = applied`).
            if str(action.get("kind") or "") == "agree" and not step.get("reaction"):
                stamp = int(action.get("at") or 0) + 60
                step["reaction"] = {"status": "applied", "at": stamp, "cards": [],
                                    "text": "Учёл: шаг остаётся в плане на эту неделю",
                                    "label": "учтено: работа изменена"}
                step["reaction_status"] = "applied"
                step["reaction_label"] = "учтено: работа изменена"
                step["reaction_pending"] = False
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

    # W19-D2 (B-20): ответы по решениям — в памяти демо, как реестр бэкенда.
    def _remember_decision(self, rid: str, decision_id: str, record: dict) -> None:
        store = getattr(self, "decision_overrides", None)
        if not isinstance(store, dict):
            store = {}
            self.decision_overrides = store
        store.setdefault(str(rid), {})[str(decision_id)] = dict(record or {})

    def decision_overrides_for(self, rid: str) -> dict:
        store = getattr(self, "decision_overrides", None)
        return (store or {}).get(str(rid)) or {}

    # W13-F (§8): итог «Проверить доску сейчас» для демо — вымышленные карточки
    def kick_report(self, preview: bool = False) -> dict:
        ready_card = {"id": "t_demo0001", "title": "Срез 02-Ops: смета упаковки", "assignee": "team-ops",
                      "status": "ready", "reason_code": "ready", "reason": "готова к запуску",
                      "tenant": "room:cafe"}
        running_card = {"id": "t_demo0002", "title": "Срез 03-CFO: юнит-экономика набора",
                        "assignee": "team-cfo", "status": "running", "reason_code": "running",
                        "reason": "уже идёт", "tenant": "room:cafe"}
        counts = {"ready": 1, "stuck": 0, "running": 1, "review": 0, "input": 0, "blocked": 1,
                  "deps": 1, "triage": 0, "scheduled": 0, "queued": 0, "done": 5, "total": 9}
        return {"ok": True, "kind": "preview" if preview else "started", "preview": preview,
                "spawned": 0 if preview else 1, "reclaimed": 0, "promoted": 0, "max": 3,
                "started": [] if preview else [{"id": ready_card["id"], "assignee": "team-ops",
                                                "title": ready_card["title"]}],
                "counts": counts, "cards": [ready_card, running_card], "useful": True,
                "error": None, "at": now()}

    # --- W22-AP2: демо-разрешения агентам (память процесса) -----------------
    # W27-E2 §1: реестр «не актуально» — как реестр ответов на сервере.
    # Деталь запроса собирается из фикстур (копия на каждый GET), поэтому отметка
    # «снято» должна жить отдельно от неё, иначе кнопка «Закрыть все неактуальные»
    # отчитывалась бы о закрытии, а список оставался прежним.
    def stale_overrides(self, rid: str) -> dict:
        store = getattr(self, "stale_dismissed", None)
        if not isinstance(store, dict):
            store = {}
            self.stale_dismissed = store
        return store.setdefault(str(rid), {})

    def remember_stale(self, rid: str, kind: str, item_id: str, on: bool, reason: str = "") -> None:
        bucket = self.stale_overrides(rid).setdefault(str(kind), {})
        key = str(item_id)
        if on:
            bucket[key] = reason or "dismissed"
        else:
            bucket.pop(key, None)

    def _approvals_for(self, rid: str) -> list:
        store = getattr(self, "approvals_demo", None)
        if not isinstance(store, dict):
            store = {}
            self.approvals_demo = store
        if rid not in store:
            if rid in APPROVAL_DEMO_RIDS:
                store[rid] = demo_approvals(rid)
            elif rid in CLOSED_APPROVAL_DEMO_RIDS:
                # W27-E2: 20 закрытых разрешений у завершённой карточки (дефект 1).
                store[rid] = demo_closed_approvals(rid)
            else:
                store[rid] = []
        return store[rid]

    def approval_grants(self) -> list:
        """Выданные разрешения: одно вечное в демо + выданные из приложения."""
        store = getattr(self, "approval_grants_demo", None)
        if not isinstance(store, list):
            store = [{
                "profile": "team-ops",
                "member_title": "Анна",
                "pattern_key": "script execution via -e/-c flag",
                "description": "выполнение кода python -c",
                "scope": "always",
                "granted_at": now() - 86400,
                "expires_at": None,
                "source": "room",
            }]
            self.approval_grants_demo = store
        return [dict(g) for g in store]

    def revoke_approval(self, body: dict) -> dict:
        profile = (check_str(body, "profile", required=True, maxlen=64) or "").strip()
        pattern = (check_str(body, "pattern_key", required=True, maxlen=160) or "").strip()
        store = getattr(self, "approval_grants_demo", None)
        if not isinstance(store, list):
            store = self.approval_grants()
        before = len(store)
        store[:] = [g for g in store if not (g.get("profile") == profile
                                             and g.get("pattern_key") == pattern)]
        self.approval_grants_demo = store
        return {"ok": True, "removed": before - len(store), "profile": profile,
                "pattern_key": pattern}

    def dismiss_stale(self, room: str, body: dict) -> dict:
        """Закрыть неактуальное у завершённых запросов комнаты (W27-E2 §1).

        Правило §1: закрываются только пункты запросов со статусом done/archived;
        решённое человеком (`granted_*`, `denied`, `accepted`, `rejected`) не
        трогается — это история, а не мусор. Возврат — действием `restore`.
        """
        scope = str((body or {}).get("scope") or "finished_requests").strip()
        if scope not in ("finished_requests",):
            raise ApiError(400, "bad_request", "scope: finished_requests",
                           {"field": "scope", "allowed": ["finished_requests"]})
        closed = {"questions": 0, "decisions": 0, "approvals": 0, "steps": 0}
        seen = set()
        for rid, raw in list(getattr(self, "requests", {}).items()) + list(getattr(self, "live", {}).items()):
            if rid in seen:
                continue
            seen.add(rid)
            try:
                detail = self.detail(rid)
            except Exception:  # noqa: BLE001 — одна карточка не мешает остальным
                continue
            if str(detail.get("room") or "") != str(room):
                continue
            if str(detail.get("status") or "") not in ("done", "archived") and not detail.get("archived"):
                continue
            stamp = now()
            for q in detail.get("open_questions") or []:
                if isinstance(q, dict) and not q.get("answered") and not q.get("dismissed"):
                    q["dismissed"] = True
                    q["state"] = "dismissed"
                    q["closed_reason"] = "request_done"
                    self.remember_stale(rid, "question", str(q.get("id") or ""), True, "request_done")
                    closed["questions"] += 1
            for d in detail.get("decisions") or []:
                if isinstance(d, dict) and not d.get("dismissed") and not d.get("superseded") \
                        and (d.get("open") is True or str(d.get("status") or "proposed") == "proposed"):
                    d["open"] = False
                    d["status"] = "dismissed"
                    d["dismissed"] = True
                    d["closed_reason"] = "request_done"
                    self.remember_stale(rid, "decision", str(d.get("id") or ""), True, "request_done")
                    closed["decisions"] += 1
            items = self._approvals_for(rid)
            for a in items:
                if isinstance(a, dict) and str(a.get("state")) == "pending":
                    a["state"] = "dismissed"
                    a["closed_reason"] = "request_done"
                    a["decided_at"] = stamp
                    self.remember_stale(rid, "approval", str(a.get("id") or ""), True, "request_done")
                    closed["approvals"] += 1
            for s in detail.get("next_steps") or []:
                if isinstance(s, dict) and str(s.get("state") or "open") == "open":
                    s["state"] = "dismissed"
                    s["closed_reason"] = "request_done"
                    self.remember_stale(rid, "step", str(s.get("id") or ""), True, "request_done")
                    closed["steps"] += 1
        total = sum(closed.values())
        return {"ok": True, "scope": scope, "room": room, "closed": closed, "total": total,
                "message": ("Закрыто неактуальных: %d" % total if total
                            else "Неактуальных пунктов не осталось"),
                "at": now()}

    def post(self, path: str, query: dict, body: dict) -> tuple:
        # W27-E8: дежурства комнаты — мастер (предпросмотр и создание), корзина,
        # сухой прогон, действия и ответы на предложения. Демо-состояние в памяти.
        m = re.fullmatch(r"/api/duties/proposals/([A-Za-z0-9_.-]{1,64})/actions", path)
        if m:
            return 200, mock_duties_fe.proposal_action(self, m.group(1), body)
        m = re.fullmatch(r"/api/rooms/([a-z][a-z0-9_-]{1,31})/duties", path)
        if m:
            return 200, mock_duties_fe.create(self, m.group(1), body)
        m = re.fullmatch(r"/api/duties/([A-Za-z0-9_.-]{1,64})/(restore|dry_run|actions)", path)
        if m:
            if m.group(2) == "restore":
                return 200, mock_duties_fe.restore(self, m.group(1))
            if m.group(2) == "dry_run":
                return 200, mock_duties_fe.dry_run(self, m.group(1))
            return 200, mock_duties_fe.act(self, m.group(1), body)
        # W26-D5: дела — создание и действия (демо в памяти, форма как у сервера)
        m = re.fullmatch(r"/api/rooms/([a-z][a-z0-9_-]{1,31})/delos", path)
        if m:
            return 201, mock_delos.create(m.group(1), body or {})
        m = re.fullmatch(r"/api/delos/(dl_[A-Za-z0-9]{2,24})/actions", path)
        if m:
            return 200, mock_delos.act(m.group(1), body or {})
        # W27-E6: «Уровень автономии» (контракт §2) — только с confirm:true.
        m = re.fullmatch(r"/api/rooms/([a-z0-9-]{2,32})/autonomy", path)
        if m:
            return 200, self.autonomy_apply(m.group(1), body or {})
        if path == "/api/approvals/revoke":
            return 200, self.revoke_approval(body or {})     # W22-AP2
        # W27-E2 §1: массово «Закрыть все неактуальные» у завершённых запросов комнаты.
        m = re.fullmatch(r"/api/rooms/([a-z][a-z0-9_-]{1,31})/dismiss_stale", path)
        if m:
            return 200, self.dismiss_stale(m.group(1), body or {})
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
        if path in ("/api/files/launch", "/api/artifacts/open"):
            # B-22 WAVE19 + W26-D3: демо/мок ничего не запускает — только подтверждает приём.
            # /api/files/launch {mode:"open"|"reveal"} — «Открыть» и «Показать в папке».
            target = body.get("path") if isinstance(body, dict) else ""
            room = str((body or {}).get("room") or "").strip() if isinstance(body, dict) else ""
            mode = str((body or {}).get("mode") or ("open" if path == "/api/files/launch" else "reveal")).strip().lower() if isinstance(body, dict) else "open"
            if mode not in ("open", "reveal"):
                raise ApiError(400, "bad_request", "mode: open или reveal", {"field": "mode"})
            if not isinstance(target, str) or not target.strip():
                raise ApiError(400, "bad_request", "Нужен path", {"field": "path"})
            if not room:
                raise ApiError(400, "bad_request", "Нужен room", {"field": "room"})
            self.room(room)
            return 200, {"ok": True, "path": target.strip(), "mode": mode,
                         "opened": True, "revealed": mode == "reveal", "mock": True}
        m = re.fullmatch(r"/api/requests/(t_[A-Za-z0-9_]+)/export", path)
        if m:
            return self.export_request(m.group(1), body or {})
        if path == "/api/rooms/check-dir":
            return 200, self.check_dir(body)
        # W20 §1.7: этапы проекта — решение по предложению и «предложи этапы» (демо).
        m = re.fullmatch(r"/api/rooms/([a-z0-9-]{2,32})/phases/proposal", path)
        if m:
            return 200, self.decide_phases(m.group(1), body or {})
        m = re.fullmatch(r"/api/rooms/([a-z0-9-]{2,32})/phases/propose", path)
        if m:
            return 201, self.propose_phases(m.group(1), body or {})
        m = re.fullmatch(r"/api/rooms/([a-z0-9-]{2,32})/agents/([a-z0-9_.-]{2,40})/notes/entries", path)
        if m:
            return 201, self.post_note_entry(m.group(1), m.group(2), body or {})
        m = re.fullmatch(r"/api/rooms/([a-z0-9-]{2,32})/agents/([a-z0-9_.-]{2,40})/notes/entries/([A-Za-z0-9_-]{2,40})/pin", path)
        if m:
            return 200, self.pin_note_entry(m.group(1), m.group(2), m.group(3), body or {})
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
        m = re.fullmatch(r"/api/duties/([A-Za-z0-9_.-]{1,64})", path)
        if m:                       # W27-E8: правка расписания/задания/правила
            return 200, mock_duties_fe.patch(self, m.group(1), body)
        m = re.fullmatch(r"/api/rooms/([a-z0-9-]{2,32})/members/([a-z0-9_.-]{2,40})", path)
        if m:
            return 200, self.pin_member(m.group(1), m.group(2), body or {})   # W19 B-28
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
        m = re.fullmatch(r"/api/requests/(t_[A-Za-z0-9_]+)", path)
        if m:
            return 200, self.patch_request(m.group(1), body or {})
        raise ApiError(404, "not_found", "Неизвестный эндпоинт", {"path": path})

    # W20-U3 (P13): закрепление запроса в памяти демо, как поле реестра бэкенда.
    def _pin_store(self) -> dict:
        store = getattr(self, "pin_overrides", None)
        if not isinstance(store, dict):
            store = {}
            self.pin_overrides = store
        return store

    def pin_state(self, rid: str) -> dict:
        """`{pinned, pinned_at}` запроса: демо хранит закрепление в памяти."""
        saved = self._pin_store().get(str(rid)) or {}
        detail = self.detail(rid)
        return {"pinned": bool(saved.get("pinned", detail.get("pinned"))),
                "pinned_at": int(saved.get("pinned_at") or detail.get("pinned_at") or 0) or None}

    def toggle_pin(self, rid: str, body: dict) -> dict:
        """`PATCH {pinned}` — закрепить/снять; текст, название и ответ не меняются."""
        value = body.get("pinned")
        if value is None or isinstance(value, (dict, list)):
            raise ApiError(400, "bad_request", "pinned: ожидается true или false",
                           {"field": "pinned", "value": str(value)})
        pinned = (value.strip().lower() in ("1", "true", "yes", "on")
                  if isinstance(value, str) else bool(value))
        at = int(now()) if pinned else 0
        self._pin_store()[str(rid)] = {"pinned": pinned, "pinned_at": at}
        summary = detail_to_summary(dict(self.detail(rid), pinned=pinned, pinned_at=at or None))
        return {"ok": True, "action": "pin" if pinned else "unpin", "pinned": pinned,
                "pinned_at": at, "request": summary}

    def export_request(self, rid: str, body: dict) -> tuple:
        """`POST /api/requests/{id}/export {format}` в демо (W20-U3, P13).

        Демо ничего не пишет на диск: `obsidian` честно отвечает «заметки не
        создаются», `html` отдаёт тот же автономный файл, что и GET, `print` —
        подтверждение формата (печать собирает браузер).
        """
        fmt = str((body or {}).get("format") or "").strip().lower()
        allowed = ["obsidian", "html", "print"]
        if not fmt:
            raise ApiError(400, "bad_request", "Укажите формат: obsidian, html или print",
                           {"field": "format", "allowed": allowed})
        if fmt not in allowed:
            raise ApiError(400, "bad_request", "Формат: obsidian, html или print",
                           {"field": "format", "allowed": allowed, "value": fmt})
        detail = self.detail(rid)
        if fmt == "print":
            return 200, {"ok": True, "format": "print", "hint": "print_css"}
        if fmt == "obsidian":
            ready = (detail.get("synthesis") or {}).get("state") == "ready"
            if not ready:
                raise ApiError(409, "conflict", "Ответ ещё не готов — экспортировать нечего",
                               {"reason": "synthesis_pending", "rid": rid})
            return 200, {"ok": False, "format": "obsidian", "skipped": "mock",
                         "reason": "В демонстрационном режиме заметки не создаются — "
                                   "откройте рабочую комнату.", "path": ""}
        return self.export(rid, "html")

    # W17-B §4: переименование названия запроса — в памяти демо, как реестр бэкенда
    def patch_request(self, rid: str, body: dict) -> dict:
        """`PATCH /api/requests/{id} {title}` → `title_source=user`, `title_locked`.

        W20-U3: тем же маршрутом приходит `{pinned}` — закрепление запроса.
        Пустая строка в `title` снимает блокировку (название снова автоматическое).
        """
        detail = self.detail(rid)
        if "pinned" in body and "title" not in body:
            return self.toggle_pin(rid, body)
        if "title" not in body:
            raise ApiError(400, "bad_request", "Нужно поле title или pinned",
                           {"field": "title"})
        title = body.get("title")
        if title is None:
            title = ""
        if not isinstance(title, str):
            raise ApiError(400, "bad_request", "title: ожидается строка", {"field": "title"})
        flat = " ".join(title.split())
        if len(flat) > 120:
            raise ApiError(400, "bad_request", "title длиннее 120 символов",
                           {"field": "title", "length": len(flat)})
        store = getattr(self, "title_overrides", None)
        if not isinstance(store, dict):
            store = {}
            self.title_overrides = store
        if not flat:
            store.pop(str(rid), None)
            short = short_title(detail.get("title") or detail.get("text") or "")
            saved = {"title_short": short, "title_source": "derived", "title_locked": False}
        else:
            saved = {"title_short": flat, "title_source": "user", "title_locked": True}
            store[str(rid)] = dict(saved)
        summary = detail_to_summary(dict(self.detail(rid), **saved))
        return {"ok": True, "action": "rename", "request": summary,
                "pinned": summary.get("pinned"),
                "title_short": saved["title_short"], "title_source": saved["title_source"],
                "title_locked": saved["title_locked"]}

    def delete(self, path: str, query: dict, body: dict) -> tuple:
        m = re.fullmatch(r"/api/duties/([A-Za-z0-9_.-]{1,64})", path)
        if m:                       # W27-E8: удаление → корзина на 30 дней
            return 200, mock_duties_fe.delete(self, m.group(1))
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
            return {"ok": True, "profile": profile, "label": member["label"], "members": members,
                    "role_labels": room.get("role_labels") or {}}
        if profile in (room.get("orchestrator"), room.get("critic")):
            # WAVE17 B-17: у руководителя и критика та же кнопка «Подпись» —
            # подпись хранится в реестре комнаты (карта role_labels).
            text = " ".join(str(label or "").split())[:120]
            labels = dict(room.get("role_labels") or {})
            if text:
                labels[profile] = text
            else:
                labels.pop(profile, None)
            room["role_labels"] = labels
            return {"ok": True, "profile": profile, "label": text, "members": members,
                    "role_labels": labels}
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
                "worklog_mode": "full", "digest_every_min": 2,
                # W23.7: у новой комнаты — жёсткий контроль моделей (как на сервере)
                "model_guard": "enforce",
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
        svod_defaults(room)                              # W20-S1 §1.1: живой свод новой комнаты
        self.room_details[rid] = room
        self.rooms.append(room)
        return self.with_models(room)

    def patch_room(self, room_id: str, body: dict) -> dict:
        room = self.room(room_id)
        allowed = {"title", "icon", "orchestrator", "critic", "context_files", "project_dir",
                   "mode_default", "critic_pass", "max_rounds", "launch_mode", "max_parallel_streams",
                   "project_mode", "agent_notes", "agent_models",
                   "critic_mode", "critic_round_cap",
                   # W20-FC §1.7: этапы комнаты — свойство комнаты (правит человек).
                   "phases",
                   # W20-S1 §1.1: живой свод — переключатель, «тишина», потолок частоты
                   "auto_svod", "auto_svod_quiet_s", "auto_svod_min_gap_s",
                   # W20-W1 §1.6: «Ход работы» — подробность ленты и частота итога
                   "worklog_mode", "digest_every_min",
                   # W23-MG2 §W23.7: контроль моделей — «строго» или «только предупреждать»
                   "model_guard"}
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
            elif key == "auto_svod":
                # W20-S1 §1.1: автообновление свода — да/нет
                room[key] = check_bool(body, key, default=True)
            elif key == "auto_svod_quiet_s":
                room[key] = check_int(body, key, lo=30, hi=3600, default=180)
            elif key == "auto_svod_min_gap_s":
                room[key] = check_int(body, key, lo=60, hi=86400, default=600)
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
            elif key == "phases":
                # W20-FC §1.7: редактор этапов сохраняет их свойством комнаты
                # (те же правила, что у сервера — второго мнения в демо нет).
                try:
                    room[key] = mock_phases.check(body.get(key))
                except mock_phases.PhaseError as exc:
                    raise ApiError(400, "bad_request", exc.message,
                                   dict(exc.detail or {})) from exc
                # Пользователь собрал ровно предложенные этапы — предложение принято.
                room["phases_proposal"] = mock_phases.reconcile(room[key],
                                                                room.get("phases_proposal"))
            elif key == "project_dir":
                room[key] = self.clean_project_dir(body)
            elif key in ("launch_mode", "max_parallel_streams", "project_mode"):
                room[key] = settings[key]           # уже проверено в clean_settings
            elif key == "agent_notes":
                room["notes_warnings"] = mock_notes.apply_map(room, notes_raw)
            elif key == "agent_models":
                self.apply_agent_models(room, models_raw)
            elif key == "worklog_mode":
                # W20-W1 §1.6: Подробно / Кратко / Выключено
                value = (check_str(body, key, default="full", maxlen=16) or "full").strip().lower()
                if value not in ("full", "brief", "off"):
                    raise ApiError(400, "bad_request",
                                   "Ход работы: «Подробно», «Кратко» или «Выключено»",
                                   {"field": key, "value": value,
                                    "allowed": ["full", "brief", "off"]})
                room[key] = value
            elif key == "model_guard":
                # W23-MG2 §W23.7: строго (по умолчанию) | только предупреждать.
                value = (check_str(body, key, default="enforce", maxlen=16) or "enforce").strip().lower()
                if value not in ("enforce", "warn"):
                    raise ApiError(400, "bad_request",
                                   "Контроль моделей: «строго» или «только предупреждать»",
                                   {"field": key, "value": value, "allowed": ["enforce", "warn"]})
                room[key] = value
            elif key == "digest_every_min":
                minutes = check_int(body, key, lo=0, hi=5, default=2)
                if minutes not in (0, 1, 2, 5):
                    raise ApiError(400, "bad_request", "Промежуточный итог: 0, 1, 2 или 5 минут",
                                   {"field": key, "value": minutes, "allowed": [0, 1, 2, 5]})
                room[key] = minutes
            else:  # title, icon
                room[key] = check_str(body, key, required=True, maxlen=200).strip()
        room.setdefault("worklog_mode", "full")          # W20-W1 §1.6: старое = «подробно»
        room.setdefault("digest_every_min", 2)
        # W23.7: настройка новая, у комнат из старых фикстур её нет — берём значение
        # сервера по умолчанию, иначе копирование полей ниже падало (KeyError → 500).
        room.setdefault("model_guard", "enforce")
        room["worklog"] = _worklog_block(room.get("worklog_mode"), room.get("digest_every_min"))
        svod_defaults(room)                              # W20-S1 §1.1: живой свод и его подписи
        for r in self.rooms:
            if r["id"] == room_id:
                for key in ("title", "icon", "orchestrator", "critic", "mode_default", "critic_pass",
                            "max_rounds", "archived", "launch_mode", "max_parallel_streams", "project_mode",
                            "worklog_mode", "digest_every_min", "worklog",
                            "auto_svod", "auto_svod_quiet_s", "auto_svod_min_gap_s", "svod",
                            "critic_mode", "critic_round_cap", "model_guard"):
                    r[key] = room[key]
        summary = self.room_summary(room_id)
        return {**self.with_models(room), "counters": summary["counters"], "phase": summary["phase"],
                "dispatcher": {"ok": True, "state": "running", "warning": None}}

    # --- этапы проекта (W20 §1.7, демо) --------------------------------------
    def decide_phases(self, room_id: str, body: dict) -> dict:
        """Решение по предложению этапов: те же правила, что у сервера.

        Меняется только демо-копия комнаты в памяти процесса: следующий GET уже
        показывает принятые этапы на карте и убирает строку из «От вас нужно».
        """
        room = self.room(room_id)
        if room.get("archived"):
            raise ApiError(409, "archived", "Комната в архиве — этапы не меняются",
                           {"room": room_id})
        action = check_str(body, "action", required=True, maxlen=20)
        if action not in mock_phases.PROPOSAL_ACTIONS:
            raise ApiError(400, "bad_request", "Действие: accept, accept_selected или reject",
                           {"field": "action", "allowed": list(mock_phases.PROPOSAL_ACTIONS)})
        ids = body.get("ids")
        chosen: list = []
        if ids is not None:
            if not isinstance(ids, list) or not all(isinstance(x, str) for x in ids):
                raise ApiError(400, "bad_request", "ids: список идентификаторов этапов",
                               {"field": "ids"})
            chosen = [x.strip() for x in ids if x.strip()]
        if action == "accept_selected" and not chosen:
            raise ApiError(400, "bad_request", "Выберите хотя бы один этап", {"field": "ids"})
        proposal = mock_phases.norm_proposal(room.get("phases_proposal"))
        if proposal is None:
            raise ApiError(409, "no_proposal", "Руководитель ещё не предлагал этапы", {})
        if proposal.get("state") != "proposed":
            wanted = "rejected" if action == "reject" else "accepted"
            if proposal.get("state") == wanted:
                return {"ok": True, "action": action, "changed": False,
                        "state": proposal["state"], "phases": room.get("phases") or [],
                        "proposal": proposal, "added": [], "skipped": [],
                        "text": mock_phases.state_text(proposal)}
            raise ApiError(409, "already_decided", "Предложение этапов уже решено",
                           {"state": proposal.get("state")})
        try:
            decided = mock_phases.decide(room.get("phases") or [], proposal, action, chosen)
        except mock_phases.PhaseError as exc:
            raise ApiError(400, "bad_request", exc.message, dict(exc.detail or {})) from exc
        room["phases"] = decided["phases"]
        room["phases_proposal"] = decided["proposal"]
        return {"ok": True, "action": action, "changed": True, "state": decided["state"],
                "phases": decided["phases"], "proposal": decided["proposal"],
                "added": decided["added"], "skipped": decided["skipped"],
                "text": mock_phases.state_text(decided["proposal"])}

    def propose_phases(self, room_id: str, body: dict) -> dict:
        """«Предложите этапы»: в демо ответ руководителя подставлен сразу.

        Настоящий сервер поставил бы карточку и ждал ответа модели. В демо это
        невозможно (моделей нет), поэтому предложение появляется сразу — с честной
        пометкой `simulated`, чтобы интерфейс показал «демо-подстановку».
        """
        room = self.room(room_id)
        note = check_str(body, "note", default="", maxlen=2000, allow_empty=True)
        pending = mock_phases.norm_proposal(room.get("phases_proposal"))
        if pending and pending.get("state") == "proposed":
            raise ApiError(409, "proposal_pending",
                           "Руководитель уже предложил этапы — сначала примите или отклоните",
                           {"at": pending.get("at")})
        body_text = mock_phases.propose_body(room, room.get("phases") or [], note)
        items = mock_phases.norm_items([
            {"title": "Идея и проверка спроса", "description": "что предлагаем и кому это нужно"},
            {"title": "Документы и договор", "description": "что готовим и с кем подписываем"},
            {"title": "Первые продажи", "description": "канал, цена, первая партия"},
        ])
        room["phases_proposal"] = {"at": int(now()), "by": room.get("orchestrator") or "",
                                   "state": "proposed", "items": items,
                                   "signature": mock_phases.signature(items),
                                   "source": "demo"}
        return {"ok": True, "action": "phases_propose", "mock": True, "simulated": True,
                "root_id": None, "title": mock_phases.PROPOSE_TITLE, "warning": None,
                "request": None, "body": body_text,
                "phases_proposal": room["phases_proposal"],
                "text": mock_phases.state_text(room["phases_proposal"])}

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

    def pin_member(self, room_id: str, profile: str, body: dict) -> dict:
        """`PATCH …/members/{profile} {pinned}` — закрепление в составе (W19 B-28)."""
        room = self.room(room_id)
        if profile in (room["orchestrator"], room["critic"]):
            raise ApiError(409, "conflict",
                           "Руководитель комнаты и критик всегда в составе",
                           {"reason": "mandatory_role", "profile": profile})
        if "pinned" not in body:
            raise ApiError(400, "bad_request", "Нужно поле pinned: true или false",
                           {"field": "pinned"})
        value = check_bool(body, "pinned", default=None)
        if value is None:
            raise ApiError(400, "bad_request", "Нужно поле pinned: true или false",
                           {"field": "pinned"})
        for member in room["members"]:
            if member["profile"] != profile:
                continue
            member["pinned"] = value
            return {"ok": True, "profile": profile, "pinned": value,
                    "members": [self.member_view(room, m) for m in room["members"]]}
        raise ApiError(404, "not_found", "Участник не найден", {"profile": profile})

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
        attach = check_list(body, "attach", default=[], maxlen=MAX_ATTACH, of_str=True)
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
                # W28-C2: attempts[] есть у каждой карточки (даже пустой) — контракт API
                "attempts": [], "needs_human": False, "outside_room": False, "error": None}
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
                "summary_md": "", "metadata": {}, "comments": [], "runs": [], "attempts": [],
                "needs_human": False, "outside_room": False, "error": None}
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
        # W19-T (B-25): повторное нажатие «Повторить» с тем же ключом не создаёт дублей.
        idem_key = body.get("idempotency_key")
        idem_slot = ""
        if action == "retry" and idem_key is not None:
            if not isinstance(idem_key, str) or not re.match(r"^[A-Za-z0-9._:\-]{6,120}$", idem_key):
                raise ApiError(400, "bad_request",
                               "idempotency_key: 6–120 знаков, буквы, цифры и «.-_:»",
                               {"field": "idempotency_key", "value": str(idem_key)[:120]})
            store = getattr(self, "idem_seen", None)
            if not isinstance(store, dict):
                store = {}
                self.idem_seen = store
            idem_slot = "%s:%s:%s" % (rid, body.get("task_id") or "", idem_key)
            seen = store.get(idem_slot)
            if isinstance(seen, dict):
                extra = dict(seen)
                extra["duplicate"] = True
                return 200, extra
        allowed = ["dispatch", "comment", "answer", "unblock", "reassign", "rework", "retry", "archive", "clone",
                   "decompose", "revise", "critique",
                   # W14-D §2/§4: «Напомнить руководителю» и «Убрать» шаг
                   # W14-H §12: действия пользователя по шагу
                   "answer_remind", "step_drop", "step_agree", "step_comment", "step_move",
                   # W17-B §3: «Отправить руководителю накопленные ответы»
                   # W19-D2 §1 (B-20): действия над решением «предложено»
                   # W20-S1 §1.1 (B-36): «Обновить сейчас» — версия свода по кнопке
                   # W20-D1 §1.2 (B-37, B-38): «Снять/Вернуть» решение и «Это не вопрос»
                   # W22-AP2: решение по запросу разрешения агенту
                   # W23-MG2: управление работой карточки, как в самом Hermes
                   "answers_resend", "decision", "svod_refresh", "question_dismiss", "approval",
                   "card_stop", "card_resume", "card_model",
                   # W26-D2: рычаги для блокировок, которые нельзя снять разрешением
                   "hint_agent", "trust_profile", "dismiss",
                   # W27-E2 §1: «Не актуально»/«Вернуть» с `target:{kind,id}`
                   "restore"]
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
        elif action in ("card_stop", "card_resume", "card_model"):
            # W23-MG2 «Контроль моделей»: управление работой карточки — как в самом
            # Hermes. Состояние демо живёт в памяти (`card_states` по номеру задачи):
            # его накладывают и деталь запроса, и состав комнаты.
            task_id = (check_str(body, "task_id", required=True, maxlen=40) or "").strip()
            node = next((n for n in tree if isinstance(n, dict) and str(n.get("id")) == task_id), None)
            if node is None:
                raise ApiError(404, "not_found", "Карточка не найдена", {"task_id": task_id})
            states = getattr(self, "card_states", None)
            if not isinstance(states, dict):
                states = {}
                self.card_states = states
            stamp = now()
            if action == "card_stop":
                if str(node.get("status") or "") not in ("running", "review", "revision"):
                    raise ApiError(409, "conflict", "Карточка уже не идёт", {"task_id": task_id})
                sent = node.get("model_sent") if isinstance(node.get("model_sent"), dict) else {}
                # W23-ST: та же форма, что отдаёт сервер (`server/model_guard.stopped_nodes`):
                # `model`/`provider` — строками, `cost_usd` — числом или null.
                patch = {"status": "blocked",
                         "stopped": {"by": "user", "at": stamp,
                                     "model": str(sent.get("model") or ""),
                                     "provider": str(sent.get("provider") or ""),
                                     "cost_usd": (float(sent["cost_usd"])
                                                  if isinstance(sent.get("cost_usd"), (int, float))
                                                  else None)},
                         "finished_at": None}
                states[task_id] = patch
                node.update(patch)
                detail.setdefault("timeline", []).append(
                    {"at": stamp, "kind": "stopped", "task_id": task_id, "actor": "mikhail",
                     "text": "Остановлено вами"})
                extra["text"] = "Работа остановлена"
            elif action == "card_resume":
                patch = {"status": "running", "stopped": None, "finished_at": None,
                         "started_at": node.get("started_at") or stamp}
                states[task_id] = patch
                node.update(patch)
                detail.setdefault("timeline", []).append(
                    {"at": stamp, "kind": "claimed", "task_id": task_id, "actor": "mikhail",
                     "text": "Продолжено вами"})
                extra["text"] = "Работа продолжена"
            else:
                provider = (check_str(body, "provider", default="", maxlen=64) or "").strip()
                model = (check_str(body, "model", default="", maxlen=120) or "").strip()
                # Пустой выбор — «по умолчанию модель профиля»: переопределения нет.
                settings = {"provider": provider, "model": model, "source": "card"} if model else {}
                patch = {"model_settings": settings or None}
                if str(node.get("status") or "") == "paused":
                    patch["status"] = "running"
                    patch["stopped"] = None
                states[task_id] = dict(states.get(task_id) or {}, **patch)
                node.update(patch)
                # Демо-правда: модель карточки живёт в `card_states` (как выбор человека
                # для ЭТОЙ карточки в `server/model_guard.py:user_model`). Настройки
                # комнаты (`agent_models`) демо НЕ меняет: там выбор всех её карточек.
                detail.setdefault("timeline", []).append(
                    {"at": stamp, "kind": "model", "task_id": task_id, "actor": "mikhail",
                     "text": "Модель карточки изменена"})
                extra["model"] = {"provider": provider, "model": model}
                extra["text"] = "Модель изменена"
            extra["task"] = node
            # Контракт ответа (docs/API.md, «W23.8 Управление карточкой») — один для демо и
            # боевого сервера: ok/action/task_id/status/model/message/detail.
            expected = ((self.room(str(detail.get("room") or "")).get("agent_models") or {})
                        .get(str(node.get("assignee") or "")) or {})
            fallback = ((self.fx.get("models.json") or {}).get("agent_defaults") or {}) \
                .get(str(node.get("assignee") or "")) or {}
            picked = (node.get("model_settings") if isinstance(node.get("model_settings"), dict)
                      else {})
            extra.update({
                "ok": True, "action": action, "task_id": task_id,
                "status": str(node.get("status") or ""),
                "model": {"provider": str(picked.get("provider") or expected.get("provider")
                                          or fallback.get("provider") or ""),
                          "model": str(picked.get("model") or expected.get("model")
                                       or fallback.get("model") or ""),
                          "source": ("user" if picked.get("model") else
                                     "room" if expected.get("model") else "profile")},
                "message": str(extra.get("text") or ""),
                "detail": {"paused": str(node.get("status") or "") == "paused"},
            })
        elif action in ("dismiss", "restore") and isinstance(body.get("target"), dict):
            # W27-E2 §1: ОДНО действие на все виды пунктов — вопрос, решение,
            # разрешение, шаг. `target:{kind,id}`; «уже так» — 409 already_decided,
            # который клиент гасит тихо (перерисовка вместо красной плашки).
            tgt = body.get("target") or {}
            kind = str(tgt.get("kind") or "").strip().lower()
            tid = str(tgt.get("id") or "").strip()
            if not tid:
                raise ApiError(400, "bad_request", "Нужен target.id", {"field": "target.id"})
            reason = str(body.get("reason") or "").strip()
            stamp = now()
            item = None
            if kind == "approval":
                items = self._approvals_for(rid)
                item = next((x for x in items if str(x.get("id")) == tid), None)
                if item is None:
                    raise ApiError(404, "not_found", "Запрос разрешения не найден", {"approval_id": tid})
                if action == "dismiss":
                    if str(item.get("state")) != "pending":
                        raise ApiError(409, "already_decided", "Пункт уже не ждёт решения",
                                       {"approval_id": tid, "state": item.get("state")})
                    item["state"] = "dismissed"
                    item["closed_reason"] = "dismissed"
                    item["decided_at"] = stamp
                else:
                    if str(item.get("state")) == "pending":
                        raise ApiError(409, "already_decided", "Пункт уже ждёт решения",
                                       {"approval_id": tid, "state": "pending"})
                    item["state"] = "pending"
                    item["decided_at"] = None
                    item.pop("closed_reason", None)
            elif kind == "question":
                item = next((q for q in detail.get("open_questions") or []
                             if str(q.get("id")) == tid), None)
                if item is None:
                    raise ApiError(404, "not_found", "Вопрос не найден", {"question_id": tid})
                if action == "dismiss":
                    if item.get("answered") or item.get("dismissed"):
                        raise ApiError(409, "already_decided", "Вопрос уже не ждёт ответа",
                                       {"question_id": tid})
                    item["dismissed"] = True
                    item["state"] = "dismissed"
                    item["closed_reason"] = "dismissed"
                    item["dismissed_at"] = stamp
                else:
                    if not item.get("dismissed"):
                        raise ApiError(409, "already_decided", "Вопрос и так ждёт ответа",
                                       {"question_id": tid})
                    item["dismissed"] = False
                    item["state"] = "open"
                    item.pop("closed_reason", None)
                    item.pop("dismissed_at", None)
            elif kind == "decision":
                item = next((d for d in detail.get("decisions") or []
                             if str(d.get("id")) == tid), None)
                if item is None:
                    raise ApiError(404, "not_found", "Решение не найдено", {"decision_id": tid})
                if action == "dismiss":
                    if item.get("open") is False or str(item.get("status")) != "proposed":
                        raise ApiError(409, "already_decided", "Решение уже не ждёт слова",
                                       {"decision_id": tid, "status": item.get("status")})
                    item["open"] = False
                    item["status"] = "dismissed"
                    item["dismissed"] = True
                    item["closed_reason"] = "dismissed"
                else:
                    if item.get("open") is True or str(item.get("status")) == "proposed":
                        raise ApiError(409, "already_decided", "Решение и так ждёт слова",
                                       {"decision_id": tid})
                    item["open"] = True
                    item["status"] = "proposed"
                    item["dismissed"] = False
                    item.pop("closed_reason", None)
            elif kind == "step":
                item = next((s for s in detail.get("next_steps") or []
                             if str(s.get("id")) == tid), None)
                if item is None:
                    raise ApiError(404, "not_found", "Шаг не найден", {"step_id": tid})
                if action == "dismiss":
                    if str(item.get("state")) == "dismissed":
                        raise ApiError(409, "already_decided", "Шаг уже убран", {"step_id": tid})
                    item["state"] = "dismissed"
                    item["closed_reason"] = "dismissed"
                else:
                    item["state"] = "open"
                    item.pop("closed_reason", None)
            else:
                raise ApiError(400, "bad_request", "target.kind: question|decision|approval|step",
                               {"field": "target.kind", "kind": kind})
            detail.setdefault("timeline", []).append(
                {"at": stamp, "kind": kind, "task_id": rid, "actor": "mikhail",
                 "text": ("Пункт закрыт как неактуальный" if action == "dismiss"
                          else "Пункт возвращён в работу") + ((" · " + reason) if reason else "")})
            # Отметка живёт в реестре: деталь собирается заново на каждый GET.
            self.remember_stale(rid, kind, tid, action == "dismiss", reason)
            extra.update({kind: dict(item) if isinstance(item, dict) else item,
                          "target": {"kind": kind, "id": tid},
                          "message": ("Закрыто как неактуальное: вернуть можно в «Закрытые»"
                                      if action == "dismiss" else "Пункт снова ждёт вашего решения")})
        elif action == "approval":
            # W22-AP2: «Разрешить один раз / Разрешать всегда / Запретить».
            aid = (check_str(body, "approval_id", required=True, maxlen=40) or "").strip()
            decision = (check_str(body, "decision", required=True, maxlen=10) or "").strip()
            if decision not in ("once", "always", "deny"):
                raise ApiError(400, "bad_request", "decision: once|always|deny",
                               {"field": "decision", "allowed": ["once", "always", "deny"]})
            items = self._approvals_for(rid)
            ap = next((x for x in items if x.get("id") == aid), None)
            if ap is None:
                raise ApiError(404, "not_found", "Запрос разрешения не найден",
                               {"approval_id": aid})
            if ap.get("state") != "pending":
                # W27-E2 §1: код `already_decided` — клиент гасит его тихо
                # (перерисовка), а не красной плашкой.
                raise ApiError(409, "already_decided", "Разрешение уже решено",
                               {"approval_id": aid, "state": ap.get("state")})
            if ap.get("floor"):
                raise ApiError(422, "unprocessable",
                               "Запрещено политикой безопасности Hermes — разрешить нельзя",
                               {"approval_id": aid, "floor": True})
            if ap.get("grantable") is False:
                # W22-AP1: execute_code в режиме без человека Hermes не разрешает списком
                raise ApiError(422, "unprocessable",
                               "Эту блокировку нельзя снять разрешением: Hermes не применяет "
                               "список разрешений к execute_code в работе без человека. "
                               "Попросите участника сделать то же через терминал.",
                               {"approval_id": aid, "grantable": False})
            state_after = {"once": "granted_once", "always": "granted_always",
                           "deny": "denied"}[decision]
            # W22-ST: решение по строке-группе закрывает ВСЕ её запросы (та же причина —
            # тот же участник, правило и карточка), как на сервере.
            same_reason = [x for x in items
                           if x.get("state") == "pending"
                           and str(x.get("task_id") or "") == str(ap.get("task_id") or "")
                           and str(x.get("profile") or "") == str(ap.get("profile") or "")
                           and str(x.get("pattern_key") or "") == str(ap.get("pattern_key") or "")]
            for row in same_reason or [ap]:
                row["state"] = state_after
                row["decided_at"] = now()
            grant = None
            if decision in ("once", "always"):
                if decision == "once":
                    for row in (same_reason or [ap]):
                        row["expires_at"] = now() + 1800
                grant = {"profile": ap.get("profile"), "pattern_key": ap.get("pattern_key"),
                         "description": ap.get("description"),
                         "scope": "always" if decision == "always" else "once"}
                if decision == "always":
                    store = getattr(self, "approval_grants_demo", None)
                    if not isinstance(store, list):
                        store = self.approval_grants()
                    if not any(g.get("profile") == grant["profile"]
                               and g.get("pattern_key") == grant["pattern_key"] for g in store):
                        store.append({"profile": grant["profile"],
                                      "member_title": ap.get("member_title"),
                                      "pattern_key": grant["pattern_key"],
                                      "description": grant["description"],
                                      "scope": "always", "granted_at": now(),
                                      "expires_at": None, "source": "room"})
                    self.approval_grants_demo = store
            extra.update({
                "approval": dict(ap),
                "applied": [str(row.get("id") or "") for row in (same_reason or [ap])],
                "grant": grant,
                "restarted": decision != "deny" and str(detail.get("status") or "") in
                ("blocked", "failed", "done"),
                "message": ("Разрешено один раз" if decision == "once"
                            else "Разрешено всегда" if decision == "always" else "Запрещено"),
            })
        elif action in ("hint_agent", "trust_profile", "dismiss"):
            # W26-D2/D1: у «нельзя штатно» и жёсткого запрета тоже есть действия.
            # Демо ничего не пишет в профили и не зовёт Hermes: только состояние в
            # памяти процесса — честная подмена для проверки интерфейса.
            aid = (check_str(body, "approval_id", required=True, maxlen=40) or "").strip()
            items = self._approvals_for(rid)
            ap = next((x for x in items if x.get("id") == aid), None)
            if ap is None:
                raise ApiError(404, "not_found", "Запрос разрешения не найден", {"approval_id": aid})
            if ap.get("state") != "pending":
                # W27-E2 §1: код `already_decided` — клиент гасит его тихо
                # (перерисовка), а не красной плашкой.
                raise ApiError(409, "already_decided", "Разрешение уже решено",
                               {"approval_id": aid, "state": ap.get("state")})
            dead = str(ap.get("dead_end") or ("floor" if ap.get("floor") else
                                              "grantable" if ap.get("grantable") else "workaround"))
            if action == "trust_profile" and dead != "workaround":
                raise ApiError(422, "unprocessable",
                               "Доверие профилю предлагается только для того, что нельзя снять разрешением",
                               {"approval_id": aid, "dead_end": dead})
            ttl = None
            if action == "trust_profile":
                ttl = (check_str(body, "ttl", required=True, maxlen=10) or "").strip()
                if ttl not in ("min30", "card", "forever"):
                    raise ApiError(400, "bad_request", "Срок доверия: min30, card или forever",
                                   {"field": "ttl", "allowed": ["min30", "card", "forever"]})
            stamp = now()
            state_after = {"hint_agent": "hinted", "dismiss": "dismissed",
                           "trust_profile": "granted_always"}[action]
            ap["state"] = state_after
            ap["decided_at"] = stamp
            ap["action"] = action
            if ttl:
                ap["trust_ttl"] = ttl
            grant = None
            if action == "trust_profile":
                expires = {"min30": stamp + 1800, "card": None, "forever": None}[ttl]
                grant = {"profile": ap.get("profile"), "member_title": ap.get("member_title"),
                         "pattern_key": str(ap.get("pattern_key") or "execute_code"),
                         "description": str(ap.get("description") or "execute_code"),
                         "scope": "always", "ttl": ttl, "granted_at": stamp,
                         "expires_at": expires, "source": "room"}
                store = getattr(self, "approval_grants_demo", None)
                if not isinstance(store, list):
                    store = self.approval_grants()
                store.append(dict(grant))
                self.approval_grants_demo = store
            text_ru = {"hint_agent": ("Участнику отправлена подсказка: execute_code в фоне недоступен — "
                                      "сделай то же через файл и терминал"),
                       "dismiss": "Пункт закрыт как неактуальный",
                       "trust_profile": "Доверие профилю выдано (%s)" % (ttl or "")}[action]
            detail.setdefault("timeline", []).append(
                {"at": stamp, "kind": "approval", "task_id": ap.get("task_id") or rid,
                 "actor": "mikhail", "text": text_ru})
            extra.update({"approval": dict(ap), "grant": grant, "text": text_ru,
                          "message": text_ru, "dead_end": dead})
        elif action == "answer":
            qid = (check_str(body, "question_id", required=True, maxlen=40) or "").strip()
            # W21-Q1 (B-44): «Пояснить» работает и БЕЗ текста, «Отправить» — нет.
            need_explanation = body.get("need_explanation") is True
            raw_text = body.get("text")
            text = raw_text.strip() if isinstance(raw_text, str) else ""
            if not need_explanation and mock_answers.is_counter_question(text):
                need_explanation = True           # запасная эвристика — как раньше
            if not text:
                if not need_explanation:
                    raise ApiError(400, "bad_request", "Нужен текст", {"field": "text"})
                text = mock_answers.DEFAULT_EXPLAIN_TEXT   # стандартная просьба
            if len(text) > MAX_NOTE:
                raise ApiError(400, "bad_request",
                               "Текст длиннее %d знаков" % MAX_NOTE,
                               {"field": "text", "limit": MAX_NOTE})
            question = next((q for q in detail.get("open_questions", []) if q["id"] == qid), None)
            if question is None:
                raise ApiError(404, "not_found", "Вопрос не найден", {"question_id": qid})
            if question.get("answered"):
                raise ApiError(409, "conflict", "Вопрос уже отвечен", {"question_id": qid})
            # W21-Q1 (B-44): двойное нажатие «Пояснить» в окне 120 с — одна просьба
            guard = getattr(self, "_explain_guard", None)
            if not isinstance(guard, dict):
                guard = self._explain_guard = {}
            explain_key = (rid, qid, text)
            previous = guard.get(explain_key)
            if need_explanation and previous and now() - previous[0] < 120:
                replay = dict(previous[1])
                replay["duplicate"] = True
                return 200, replay
            # W14-D §3: встречный вопрос вопрос НЕ закрывает (кнопка надёжнее эвристики)
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
            if need_explanation:
                # W21-Q1 (B-44): запомнили просьбу — повтор в окне 120 с её же и получит.
                self._explain_guard[explain_key] = (now(), dict(extra))
        elif action == "answer_remind":
            qid = (check_str(body, "question_id", required=True, maxlen=40) or "").strip()
            question = next((q for q in detail.get("open_questions", []) if q["id"] == qid), None)
            if question is None or not question.get("answer"):
                raise ApiError(404, "not_found", "Ответ на этот вопрос не найден", {"question_id": qid})
            if question.get("reaction"):
                raise ApiError(409, "conflict", "Руководитель уже ответил на этот вопрос",
                               {"question_id": qid, "reaction": question.get("reaction")})
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
        elif action == "svod_refresh":
            # W20-S1 §1.1 (B-36): «Обновить сейчас» — версия свода по кнопке.
            # Пока обновление идёт, второй раз не запускаем (как сервер — 409).
            synthesis = detail.setdefault("synthesis", {})
            live = synthesis.get("live") if isinstance(synthesis.get("live"), dict) else {}
            if live.get("state") == "updating":
                raise ApiError(409, "conflict", "Обновление свода уже идёт — дождитесь новой версии.",
                               {"reason": "updating"})
            version = int(synthesis.get("version") or 0) + 1
            card_id = self._task_id()
            ts = now()
            detail.setdefault("tree", []).append(
                {"id": card_id, "parent_ids": [rid], "title": "Обновление свода",
                 "assignee": detail.get("assignee") or "", "role": "revision",
                 "status": "running", "started_at": ts, "finished_at": None,
                 "summary_md": "", "metadata": {}, "comments": [], "runs": [], "attempts": [],
                 "needs_human": False, "outside_room": False, "error": None})
            synthesis["live"] = dict(live, state="updating", behind=dict(live.get("behind") or {}),
                                     text="Обновляется… версия %d" % version)
            detail.setdefault("timeline", []).append(
                {"at": ts, "kind": "claimed", "task_id": card_id, "actor": detail.get("assignee"),
                 "text": "Свод обновляется — версия %d" % version})
            extra.update({"card_id": card_id, "behind": dict(live.get("behind") or {})})
        elif action == "answers_resend":
            # W17-B §3: одна карточка со всеми ответами без реакции — только по клику
            pending = [q for q in detail.get("open_questions") or []
                       if q.get("answer") and q.get("answer_state") == "sent"]
            if not pending:
                raise ApiError(409, "conflict",
                               "Нечего отправлять: на все ответы руководитель уже отреагировал.",
                               {"reason": "nothing_to_resend", "pending": 0})
            items = [{"question_id": q["id"], "question_text": q.get("text") or "",
                      "answer": q.get("answer") or ""} for q in pending]
            text = mock_answers.digest_comment(items)
            self.comment_seq = getattr(self, "comment_seq", 0) + 1
            if root is not None:
                root.setdefault("comments", []).append(
                    {"id": self.comment_seq, "author": "mikhail", "text": text, "at": now()})
            store = getattr(self, "thread_store", None)
            if isinstance(store, dict):
                store.setdefault(rid, []).append(
                    {"id": "c%d" % self.comment_seq, "at": now(), "author_kind": "mikhail",
                     "author": "mikhail", "author_label": "Михаил", "text": text,
                     "task_id": rid, "kind": "message"})
            for question in pending:
                question["resent_at"] = now()
            extra.update({"sent": len(items), "question_ids": [q["id"] for q in pending],
                          "task_id": root.get("id") if isinstance(root, dict) else rid,
                          "at": now(),
                          "note": {"task_id": None, "merged": False, "routed_to": None}})
        elif action == "decision":
            # W19-D2 §1 (B-20): «Принять» / «Отклонить» / «Обсудить» решение.
            decision_id = (check_str(body, "decision_id", required=True, maxlen=64) or "").strip()
            if ":" in decision_id:
                decision_id = decision_id.split(":", 1)[1]
            outcome = (check_str(body, "decision", required=True, maxlen=16) or "").strip().lower()
            if outcome not in mock_answers.DECISION_ACTIONS:
                raise ApiError(400, "bad_request", "decision: accept|reject|discuss",
                               {"field": "decision", "value": outcome,
                                "allowed": list(mock_answers.DECISION_ACTIONS)})
            text = (check_str(body, "text", default="", maxlen=MAX_NOTE, allow_empty=True) or "").strip()
            if outcome == "discuss" and not text:
                raise ApiError(400, "bad_request", "Напишите или надиктуйте, что обсудить",
                               {"field": "text"})
            decision = next((d for d in detail.get("decisions") or []
                             if str(d.get("id")) == decision_id), None)
            if decision is None:
                raise ApiError(404, "not_found", "Решение не найдено", {"decision_id": decision_id})
            if outcome in ("dismiss", "restore"):
                # W20-D1 §1.2 (B-37): «Снять — неактуально» / «Вернуть» — фильтр списка:
                # решение уходит из «Нужен ваш ответ», но остаётся серым в «Решениях».
                record = {"kind": "decision", "decision_id": decision_id,
                          "decision_text": str(decision.get("text") or ""), "action": outcome,
                          "history": [{"action": outcome, "at": now(), "text": text or None}],
                          "outcome": "dismissed" if outcome == "dismiss" else "proposed",
                          "text": text or mock_answers.DECISION_ACTION_RU[outcome],
                          "user_text": text or None, "at": now(), "answered": True,
                          "status": "dismissed" if outcome == "dismiss" else "sent"}
            else:
                record = {"kind": "decision", "decision_id": decision_id,
                          "decision_text": str(decision.get("text") or ""), "action": outcome,
                          "outcome": {"accept": "accepted", "reject": "rejected",
                                      "discuss": "discuss"}[outcome],
                          "text": text or mock_answers.DECISION_ACTION_RU[outcome],
                          "user_text": text or None, "at": now(), "answered": True, "status": "sent"}
            self._remember_decision(rid, decision_id, record)
            view = mock_answers.apply_decision(decision, record, None)
            decision.update(view)
            mark = mock_answers.decision_comment(outcome, decision_id,
                                                 str(decision.get("text") or ""), text)
            self.thread_seq = getattr(self, "thread_seq", 0) + 1
            store = getattr(self, "thread_store", None)
            if isinstance(store, dict):
                store.setdefault(rid, []).append(
                    {"id": "c%d" % self.thread_seq, "at": now(), "author_kind": "mikhail",
                     "author": "mikhail", "author_label": "Михаил", "task_id": rid,
                     "kind": "answer", "answer_kind": "decision",
                     "question_id": None, "decision_id": decision_id,
                     "text": text or mock_answers.DECISION_ACTION_RU[outcome], "text_mark": mark})
            detail["needs_you"] = compute_needs_you(detail)
            extra.update({"decision_id": decision_id, "decision": view, "message": mark,
                          "action_label": mock_answers.DECISION_ACTION_RU[outcome],
                          "status": view.get("status"),
                          "status_label": view.get("status_label"),
                          "status_hint": view.get("status_hint"),
                          "open": bool(view.get("open")),
                          "muted": bool(view.get("status_muted")),
                          "answer_label": view.get("answer_label"),
                          "pending_label": mock_answers.PENDING_RU,
                          "chain_hint": mock_answers.CHAIN_HINT})
        elif action == "question_dismiss":
            # W20-D1 §1.2 (B-38): «Это не вопрос» — вопрос уходит из «Нужен ваш ответ»
            # и из счётчиков; данные не меняются, отметка живёт в памяти демо-сервера.
            qid = (check_str(body, "question_id", required=True, maxlen=64) or "").strip()
            question = next((q for q in detail.get("open_questions") or []
                             if str(q.get("id")) == qid), None)
            if question is None:
                raise ApiError(404, "not_found", "Вопрос не найден", {"question_id": qid})
            record = {"kind": "question", "question_id": qid, "dismissed": True,
                      "status": "dismissed", "answered": False, "text": "", "at": now(),
                      "question_text": str(question.get("text") or ""),
                      "source": question.get("source") or "card"}
            mock_answers.apply_question(question, record)
            # W20-FA: отметка переживает пересборку демо-детали (живой запрос
            # собирается на каждый GET — без памяти «Это не вопрос» пропадал бы).
            dismissed_store = getattr(self, "dismissed_questions", None)
            if not isinstance(dismissed_store, dict):
                dismissed_store = {}
                self.dismissed_questions = dismissed_store
            dismissed_store.setdefault(str(rid), {})[qid] = dict(record)
            detail["needs_you"] = compute_needs_you(detail)
            extra.update({"question_id": qid, "question": question,
                          "answer_state": question.get("answer_state"),
                          "answer_label": question.get("answer_label"),
                          "dismissed": True,
                          "needs_answer": bool(question.get("needs_answer"))})
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
            # W19-T: статус запоминаем в памяти демо (фикстура не перезаписывается), иначе
            # следующий GET снова показал бы ошибку и «уже принятый» перезапуск.
            store = getattr(self, "retry_overrides", None)
            if not isinstance(store, dict):
                store = {}
                self.retry_overrides = store
            seen = store.setdefault(str(rid), {})
            for node in failed:
                node["status"] = "ready"
                node["error"] = None
                # W28-C2: перезапуск — новая попытка; прежние остаются в «Попытки · N»,
                # красное снимается (как на сервере: ошибка — только по итогу карточки).
                attempts = node.get("attempts")
                if isinstance(attempts, list):
                    for item in attempts:
                        if isinstance(item, dict):
                            item["final"] = False
                seen[str(node["id"])] = {"status": "ready", "error": None}
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
        if idem_slot:
            self.idem_seen[idem_slot] = dict(extra)     # повтор того же нажатия — тот же ответ
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
    # --- W27-E6: применение уровня автономии (контракт §2) -------------------
    def autonomy_apply(self, room_id: str, body: dict) -> dict:
        """POST /api/rooms/{id}/autonomy — без confirm:true отвечаем 409 и предпросмотром.

        В демо «применение» — память процесса и поле `autonomy` в записи комнаты;
        в профили участников мок не пишет никогда (это делает сервер E5).
        """
        room = self.room(room_id)
        level = str(body.get("level") or "").strip()
        if level not in self.AUTONOMY_LEVELS:
            raise ApiError(400, "bad_request", "Уровень: осторожный, рабочий или свободный",
                           {"field": "level", "allowed": list(self.AUTONOMY_LEVELS)})
        never = None
        if isinstance(body.get("never"), list):
            never = []
            for raw in body["never"]:
                text = " ".join(str(raw or "").split())
                if not text:
                    continue
                if len(text) > 120:
                    raise ApiError(400, "bad_request", "Запись списка длиннее 120 знаков",
                                   {"field": "never"})
                if text.lower() not in [x.lower() for x in never]:
                    never.append(text)
        payload = self.autonomy_payload(room_id)
        if body.get("confirm") is not True:
            raise ApiError(409, "confirm_required",
                           "Уровень меняется только с подтверждением — сначала покажите предпросмотр",
                           {"level": level, "preview": {level: payload["preview"][level]}})
        state = self.autonomy_state(room_id)
        state["level"] = level
        state["never"] = list(never if never is not None else state["never"])
        state["updated_at"] = now()
        applied = [{"profile": row["profile"], "member_title": row["member_title"],
                    "allowlist": row["allowlist"], "single_query_mode": row["single_query_mode"],
                    "deny": row["deny"]}
                   for row in self._autonomy_profiles(room, level, state["never"])]
        room["autonomy"] = {"level": level, "never": list(state["never"]),
                            "updated_at": state["updated_at"]}
        return {"ok": True, "room": room_id, "level": level, "never": list(state["never"]),
                "applied": applied, "updated_at": state["updated_at"],
                "backup": "data/backups/autonomy/%s-%d.yaml" % (room_id, int(state["updated_at"])),
                "mock": True}
