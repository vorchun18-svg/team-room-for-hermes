# -*- coding: utf-8 -*-
"""W28-B (дефект 6): действия разрешений «клиент ↔ сервер» — сверка и живая проба.

Зачем отдельный тест
--------------------
Дефект 19.11.2026: карточка «нельзя штатно» (`execute_code`) шлёт
``POST /api/requests/{rid}/actions {"action":"hint_agent"|"trust_profile", …}``, а
серверный список ``ACTION_ALLOWED`` этих имён не знал — кнопки отвечали
``400 «Неизвестное действие»`` ещё до всякой логики. Серверное действие
(``approvals.decide`` → ``act_block``) было написано и протестировано отдельно,
к ручке не подключено; фронтовые тесты подменяли ответ. Ни один тест не сверял
набор имён «что шлёт интерфейс» с набором «что принимает сервер».

Что проверяется
---------------
1. **Сверка имён (python читает js).** Все имена действий, которые интерфейс
   отправляет на ``/api/requests/{rid}/actions`` (литералы ``action: '…'``,
   динамические ``'step_' + kind``, имена из карточки разрешения), обязаны быть
   в ``ACTION_ALLOWED`` / ``ACTION_REQUIRED`` сервера. Любое новое имя, которого
   сервер не знает, — провал. Имена, которые на эту ручку НЕ уходят (значения
   решений, чужие ручки), перечислены явно с причиной, и таблица сама проверяется
   на гниение: лишнего в ней быть не может, спрятать там серверное имя нельзя.
2. **Живой HTTP (порт 8853, песочница).** Поднимается настоящий сервер в потоке:
   поддельная доска (карточки в песочной ``kanban.db``), подставной канал записи
   комментария (``engine_write.act``), поддельный ``HERMES_HOME``
   (``<данные>/hermes-home``) — реальный профиль не трогается.
   * ``hint_agent`` → 200, состояние `hinted`, ``needs_you_count.approvals`` −1;
   * ``trust_profile`` с ``ttl`` → 200, состояние `trusted`, запись ТОЛЬКО в
     поддельный профиль (там `single_query_mode: approve`);
   * ``trust_profile`` без срока → 422 `ttl_required` с причиной;
   * ``trust_profile`` у жёсткого запрета (`floor`) → 422 `floor` с причиной;
   * повтор того же действия → 409 `already_decided` (тихо, не 500).
3. **Сторож `tools/check_dead_ends.py --press`**: у каждого ждущего пункта кнопка
   при нажатии правда меняет состояние.

Только песочница: реальный ``%LOCALAPPDATA%\\hermes``, живой 8777 и `VERSION`
не трогаются, реальных агентов и кронов тест не запускает. Данные вымышленные.

    python -m unittest tests.test_contract_actions -v
"""
from __future__ import annotations

import json
import os
import re
import socket
import sqlite3
import sys
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(ROOT / "tools") not in sys.path:
    sys.path.insert(0, str(ROOT / "tools"))

import check_dead_ends as cde                    # noqa: E402
import server.app as app_mod                     # noqa: E402
import server.approvals as ap                    # noqa: E402
import server.engine_write as ew                 # noqa: E402
import server.registry as registry               # noqa: E402
import server.rooms as rooms_mod                 # noqa: E402
from server.app_common import ACTION_ALLOWED, ACTION_REQUIRED   # noqa: E402
from tests.helpers import Sandbox, SimAgents     # noqa: E402

WEB = ROOT / "web" / "js"
#: Порт живой пробы (правила волны: песочница 8851–8899).
PORT = 8853

MEMBER = "worker-profile"
LEAD = "lead-profile"
CARD_TITLE = "Собрать сводку по заявкам"

#: Литералы имён действия в js: `action: 'hint_agent'` (склейку `'step_' + kind`
#: отсекаем — её проверяет ACTION_CONCAT).
ACTION_LITERAL = re.compile(r"\baction:\s*'([A-Za-z_][A-Za-z0-9_]*)'(?!\s*\+)")
#: Склейка имени: `action: 'step_' + kind` — имя собирается во время работы.
ACTION_CONCAT = re.compile(r"\baction:\s*'([A-Za-z_]+_)'\s*\+")
#: Значения `kind` рядом: по ним достраивается склеенное имя.
KIND_LITERAL = re.compile(r"\bkind:\s*'([A-Za-z_][A-Za-z0-9_]*)'")

#: Имена, которые на ``/api/requests/{rid}/actions`` НЕ уходят. Каждое — с причиной,
#: чтобы новое имя нельзя было «спрятать» в таблице незаметно.
#: Проверки: (а) имя встречается в js, (б) его нет в ACTION_ALLOWED.
NOT_REQUEST_ACTIONS = {
    # значения решений: уходят ПОЛЕМ `decision` внутри `action:"approval"`/`"decision"`
    "once": "решение по разрешению — поле decision при action:'approval'",
    "always": "решение по разрешению — поле decision при action:'approval'",
    "deny": "решение по разрешению — поле decision при action:'approval'",
    # W30-D1: «accept» стал настоящим действием запроса (приём задачи), поэтому в
    # таблице исключений его больше нет — иначе стража «спрятать серверное имя»
    # срабатывает справедливо. «reject»/«discuss» остаются значениями решения.
    "reject": "решение руководителя — поле outcome/decision при action:'decision'",
    "discuss": "решение руководителя — поле outcome/decision при action:'decision'",
    # W30-B §2: решение по предложению задач уходит своей ручкой
    # `POST /api/requests/{id}/proposal/resolve` (поле action), а не на /actions.
    "accept_all": "предложение задач: POST /api/requests/{id}/proposal/resolve (action=accept_all)",
    "reject_all": "предложение задач: POST /api/requests/{id}/proposal/resolve (action=reject_all)",
    "apply": "предложение задач: POST /api/requests/{id}/proposal/resolve (action=apply)",
    "custom": "предложение задач: свой вариант — POST /api/requests/{id}/proposal/resolve (action=custom)",
    "ack": "«Понятно, скрыть» у жёсткого запрета — только вид, серверу не шлётся",
    # другие ручки: у них свой набор действий
    "approve": "дело: POST /api/delos/{id}/actions",
    "done": "дело: POST /api/delos/{id}/actions",
    # W29B-Q5: доска дел переносит карточку между колонками — эти имена уходят
    # на /api/delos/{id}/actions (сервер: server/delo.py:ACTIONS), не на /api/requests.
    "pause": "дело: POST /api/delos/{id}/actions — колонка «На паузе» доски",
    "resume": "дело: POST /api/delos/{id}/actions — возврат из «На паузе»",
    "reopen": "дело: POST /api/delos/{id}/actions — возврат из «Закрыто»/«Отменено»",
    "mark_sent": "дело/исходящие: POST /api/delos/{id}/actions",
    "approve_outbox": "исходящие: POST /api/outbox/…",
    "cancel_outbox": "исходящие: POST /api/outbox/…",
    "edit_outbox": "исходящие: POST /api/outbox/…",
    # W29-I1: пункт «Предлагаю собрать карту проекта» — своя ручка со своим набором
    # действий (`build`/`later`/`dismiss`), она их и принимает: POST /api/rooms/{id}/results/offer
    "later": "карта проекта: POST /api/rooms/{id}/results/offer (action=later)",
    # W29-I1: поле строки вида «Сегодня» (`daily-model.js`) — по нему строится ссылка,
    # на сервер как действие не уходит вовсе.
    "open": "поле строки «Сегодня»: означает «открыть ссылку», не действие API",
    # W29-I1: панель «Паритет моделей» — своя ручка исправления.
    "reset_model": "паритет моделей: POST /api/rooms/{id}/parity/fix (action=reset_model)",
}


def js_files() -> list[Path]:
    """Все файлы интерфейса (без тестов и сборок)."""
    return sorted(WEB.rglob("*.js"))


def js_text() -> dict:
    """``{путь: текст}`` — читаем один раз на класс тестов."""
    return {str(path.relative_to(WEB)).replace("\\", "/"): path.read_text(encoding="utf-8")
            for path in js_files()}


def sent_action_names(texts: dict) -> tuple[set, set]:
    """Имена действий из js: (литералы, склеенные по префиксу)."""
    literals: set = set()
    prefixes: set = set()
    for text in texts.values():
        literals |= set(ACTION_LITERAL.findall(text))
        prefixes |= set(ACTION_CONCAT.findall(text))
    return literals, prefixes


def approval_card_actions(text: str) -> set:
    """Имена кнопок карточки разрешения (``approvalActionsFor``/``approvalStaleActions``).

    Это и есть класс дефекта 6: имя кнопки уходит на сервер как ``action``. Объект
    кнопки с полем ``decision`` сервер получает как ``{action:"approval", decision:…}``
    — его имя действием не является и в набор не попадает.
    """
    out: set = set()
    for line in text.splitlines():
        match = re.search(r"\{\s*action:\s*'([A-Za-z_][A-Za-z0-9_]*)'", line)
        if match is None:
            continue
        if "decision:" in line:
            continue
        out.add(match.group(1))
    return out


class ActionNamesContractTest(unittest.TestCase):
    """Сверка «что шлёт интерфейс» ↔ «что принимает сервер» (дефект 6)."""

    @classmethod
    def setUpClass(cls):
        cls.texts = js_text()
        cls.approvals_js = cls.texts.get("views/approvals.js") or ""

    def test_every_sent_action_is_accepted_by_server(self):
        literals, prefixes = sent_action_names(self.texts)
        unknown = sorted(name for name in literals
                         if name not in ACTION_ALLOWED and name not in NOT_REQUEST_ACTIONS)
        self.assertEqual([], unknown,
                         "интерфейс шлёт действия, которых сервер не знает (%s): "
                         "добавьте имя в ACTION_ALLOWED/ACTION_REQUIRED в "
                         "server/app_common.py и проведите его в обработчик" % (", ".join(unknown)))
        for prefix in sorted(prefixes):
            # `action: 'step_' + kind`: имя собирается во время работы, поэтому сверяем
            # его словарь — все литералы `step_*` в интерфейсе и все серверные `step_*`.
            names = set(re.findall(r"'(%s[A-Za-z0-9_]+)'" % re.escape(prefix),
                                   "\n".join(self.texts.values())))
            self.assertTrue(names, "у склейки «%s» нет ни одного имени в js" % prefix)
            for name in sorted(names):
                self.assertIn(name, ACTION_ALLOWED,
                              "интерфейс собирает действие «%s», которого сервер не знает" % name)
            known = [name for name in ACTION_ALLOWED if name.startswith(prefix)]
            self.assertTrue(known, "у префикса «%s» нет ни одного серверного действия" % prefix)

    def test_approval_card_buttons_are_server_actions(self):
        """Кнопки карточки разрешения — самый близкий к дефекту набор."""
        names = approval_card_actions(self.approvals_js)
        self.assertTrue(names, "не нашли кнопок разрешения в web/js/views/approvals.js")
        for needed in ("hint_agent", "trust_profile"):
            self.assertIn(needed, names, "карточка «нельзя штатно» потеряла кнопку «%s»" % needed)
        local_only = {name: NOT_REQUEST_ACTIONS[name] for name in names
                      if name not in ACTION_ALLOWED}
        self.assertEqual(set(local_only), {"ack"},
                         "кнопки разрешения не принимаются сервером: %s" % local_only)

    def test_extra_actions_require_approval_id(self):
        for name in ("hint_agent", "trust_profile"):
            self.assertIn(name, ACTION_ALLOWED, "%s должен приниматься ручкой /actions" % name)
            self.assertEqual(ACTION_REQUIRED.get(name), ("approval_id",),
                             "у %s обязателен approval_id" % name)
        for name in ("hint_agent", "trust_profile", "dismiss"):
            self.assertIn(name, ap.EXTRA_DECISIONS, "сервер не знает недоговорное действие %s" % name)

    def test_w30_question_and_task_actions_are_accepted(self):
        """W30 (§1, §2, §3): новые действия волны принимаются ручкой /actions.

        Стража «что шлёт интерфейс» ловит только имена, дошедшие до js. Здесь —
        явный перечень действий волны: если имя уберут из ``ACTION_ALLOWED``,
        кнопки вопросов и приёма задачи начнут отвечать 400 «Неизвестное действие».
        У ``split_to_tasks`` кнопки пока нет (действие для старых больших задач,
        документировано в docs/API.md) — сервер обязан принимать его и без клиента.
        """
        for name in ("answer_option", "answer_custom", "explain_more", "postpone",
                     "dismiss", "restore", "accept", "reopen_accepted", "split_to_tasks"):
            self.assertIn(name, ACTION_ALLOWED, "действие волны «%s» сервер не принимает" % name)
        for name in ("answer_custom", "explain_more", "postpone"):
            self.assertEqual(ACTION_REQUIRED.get(name), ("question_id",),
                             "у «%s» обязателен question_id" % name)
        self.assertEqual(ACTION_REQUIRED.get("answer_option"), ("question_id", "option_id"),
                         "выбор варианта без option_id не отличить от своего ответа")
        joined = "\n".join(self.texts.values())
        for name in ("answer_option", "answer_custom", "explain_more", "postpone",
                     "move"):
            # W31: приём/возврат задачи в интерфейсе идут единым действием `move`
            # (to: working|review|done); `accept`/`reopen_accepted` остались у сервера для
            # старых клиентов и проверяются выше по ACTION_ALLOWED.
            self.assertIn("'%s'" % name, joined, "у действия «%s» нет кнопки в интерфейсе" % name)

    def test_exempt_table_does_not_rot(self):
        """Таблица исключений: имя есть в js и НЕ является серверным действием."""
        literals, _prefixes = sent_action_names(self.texts)
        joined = "\n".join(self.texts.values())
        for name, why in NOT_REQUEST_ACTIONS.items():
            self.assertIn("'%s'" % name, joined, "исключение «%s» больше не встречается в js" % name)
            self.assertNotIn(name, ACTION_ALLOWED,
                             "«%s» — серверное действие, его нельзя держать в исключениях" % name)
            self.assertTrue(why, "у исключения «%s» нет причины" % name)

    def test_ttl_keys_of_trust_card_are_accepted(self):
        """Сроки из карточки доверия (`card`/`min30`/`forever`) сервер понимает."""
        ttl = re.search(r"export const TRUST_TTL = \[(.*?)\n\];", self.approvals_js, re.S)
        self.assertIsNotNone(ttl, "в approvals.js нет списка сроков доверия")
        keys = re.findall(r"ttl:\s*'([^']+)'", ttl.group(1))
        self.assertTrue(keys, "список сроков доверия пуст")
        for key in keys:
            plan = ap.parse_ttl(key)
            self.assertIn(plan["scope"], ("card", "minutes", "forever"),
                          "срок «%s» разобран непонятно: %s" % (key, plan))


# --------------------------------------------------------------- живая проба ---

BLOCK_CODE = json.dumps({
    "status": "error",
    "error": ("BLOCKED: execute_code runs arbitrary local Python (including subprocess calls "
              "that bypass shell-string approval checks). Single-query mode (-q) runs without a "
              "user present to approve it. Use normal tools instead.")}, ensure_ascii=False)
BLOCK_HEREDOC = json.dumps({
    "error": ("BLOCKED: Command flagged as dangerous (script execution via heredoc) but "
              "single-query mode (-q) runs without a user present to approve it. Find an "
              "alternative approach that avoids this command."),
    "status": "blocked", "exit_code": -1}, ensure_ascii=False)
CALL_HEREDOC = json.dumps([{"function": {"name": "terminal", "arguments": json.dumps(
    {"command": "python - <<'EOF'\nprint('сводка')\nEOF"})}}])
BLOCK_FLOOR = json.dumps({
    "error": ("BLOCKED (hardline): delete in root path. This is never allowed, even with "
              "approval."), "status": "blocked", "exit_code": -1}, ensure_ascii=False)
CALL_CODE = json.dumps([{"function": {"name": "execute_code",
                                      "arguments": json.dumps({"code": "print('сводка')"})}}])
CALL_FLOOR = json.dumps([{"function": {"name": "terminal", "arguments": json.dumps(
    {"command": "rm -rf /"})}}])

PROFILE_CONFIG = """\
# профиль участника комнаты (вымышленный)
approvals:
  mode: smart
  single_query_mode: deny
agent:
  max_turns: 40
command_allowlist: []
"""


def _record(root_id: str) -> dict:
    """Запись реестра запроса (как у боевой комнаты, только вымышленная)."""
    return {"root_id": root_id, "slug": "r-20261120-1140-w28b", "title": "Сводка по заявкам",
            "text": "Собери сводку по заявкам за неделю", "created_at": int(time.time()),
            "critic": False, "mode": "auto", "adopted": False, "extra_cards": [],
            "ingested": {}, "retargeted": False}


def _port_free(port: int) -> bool:
    """Свободен ли порт: на Windows второй сокет молча занимает занятый порт."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.6)
        return sock.connect_ex(("127.0.0.1", port)) != 0


class LiveActionsTest(unittest.TestCase):
    """Живой сервер в песочнице на 8853: кнопки разрешения меняют состояние."""

    @classmethod
    def setUpClass(cls):
        if not _port_free(PORT):
            raise unittest.SkipTest("порт %d занят другим процессом — проба не запускается" % PORT)
        cls.sb = Sandbox().__enter__()
        cls.cfg = cls.sb.cfg
        cls.cfg.port = PORT
        cls.real_before = cls.sb.snapshot_real()
        cls.sa = SimAgents(cls.sb)
        cls.app = app_mod.App(cls.cfg, port=PORT)
        cls.room = rooms_mod.create_room(cls.cfg, {
            "title": "Заявки кафе «Лето»", "orchestrator": LEAD,
            "members": [{"profile": MEMBER, "role": "worker", "label": "Исполнитель по заявкам"}]})
        # --- подставной канал записи комментария (вместо CLI): только запоминаем
        cls.comments: list = []
        cls._real_act = ew.act

        def fake_act(cfg, board, root_id, action, **kw):
            cls.comments.append({"root_id": root_id, "action": action, "kw": dict(kw)})
            return {"ok": True, "local": True}

        ew.act = fake_act
        # --- подставная запись доверия: пишем ТОЛЬКО в поддельный дом профиля
        cls.workers: list = []
        cls._real_worker = ap.run_worker

        def fake_worker(cfg, request, timeout: int = 0):
            cls.workers.append(dict(request))
            op = str(request.get("op") or "")
            home = Path(request.get("home") or "")
            conf = home / "config.yaml"
            text = conf.read_text(encoding="utf-8") if conf.is_file() else ""
            if op == "trust":
                conf.parent.mkdir(parents=True, exist_ok=True)
                conf.write_text(text.replace("single_query_mode: deny",
                                             "single_query_mode: approve"), encoding="utf-8")
                return {"ok": True, "op": "trust", "previous": "deny", "value": "approve"}
            if op == "untrust":
                conf.write_text(text.replace("single_query_mode: approve",
                                             "single_query_mode: deny"), encoding="utf-8")
                return {"ok": True, "op": "untrust", "previous": "approve", "value": "deny"}
            return {"ok": False, "code": "failed", "message": "неожиданная операция"}

        ap.run_worker = fake_worker
        cls.server = app_mod.create_app(cls.cfg, start_watcher=False, port=PORT)
        cls.thread = threading.Thread(target=cls.server.serve_forever,
                                      kwargs={"poll_interval": 0.1}, daemon=True)
        cls.thread.start()
        deadline = time.time() + 20
        while time.time() < deadline:
            try:
                cls.http("GET", "/api/health")
                break
            except (urllib.error.URLError, OSError):
                time.sleep(0.2)

    @classmethod
    def tearDownClass(cls):
        try:
            cls.server.shutdown()
            cls.server.server_close()
            try:
                cls.server.http_app.stop()
            except Exception:                    # noqa: BLE001 - остановка вторична
                pass
        finally:
            ap.run_worker = cls._real_worker
            ew.act = cls._real_act
            try:
                cls.sb.assert_real_untouched(cls.real_before)
            finally:
                cls.sb.__exit__(None, None, None)

    # --- помощники --------------------------------------------------------
    @classmethod
    def http(cls, method: str, path: str, body: dict | None = None) -> tuple:
        """Запрос к живому серверу: (код, тело). Ошибки — кодом и телом, не исключением."""
        data = None if body is None else json.dumps(body, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request("http://127.0.0.1:%d%s" % (PORT, path), data=data,
                                         method=method,
                                         headers={"Content-Type": "application/json",
                                                  "Accept": "application/json"})
        try:
            with urllib.request.urlopen(request, timeout=30) as response:   # noqa: S310 - локально
                raw = response.read().decode("utf-8", "replace")
                return response.status, (json.loads(raw or "{}") if raw else {})
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode("utf-8", "replace")
            try:
                payload = json.loads(raw or "{}")
            except ValueError:
                payload = {"raw": raw[:400]}
            return exc.code, payload

    @property
    def room_id(self) -> str:
        return str(self.room.get("id"))

    def fake_home(self) -> Path:
        """Поддельный дом Hermes песочницы (реальный профиль не трогаем)."""
        return ap.hermes_root(self.cfg)

    def profile_dir(self) -> Path:
        return ap.profile_home(self.cfg, MEMBER)

    def request_with_block(self, *blocks) -> str:
        """Запрос с карточкой участника, у которой в сессии блокировка Hermes."""
        root = self.sa.card("Собери сводку по заявкам", LEAD, status="todo",
                            tenant="room:" + self.room_id)
        child = self.sa.card(CARD_TITLE, MEMBER, status="running",
                            tenant="room:" + self.room_id, parent=root)
        home = self.profile_dir()
        home.mkdir(parents=True, exist_ok=True)
        (home / "config.yaml").write_text(PROFILE_CONFIG, encoding="utf-8")
        state = home / "state.db"
        con = sqlite3.connect(str(state))
        con.execute("CREATE TABLE IF NOT EXISTS messages (id INTEGER PRIMARY KEY AUTOINCREMENT, "
                    "session_id TEXT, role TEXT, content TEXT, tool_calls TEXT, tool_name TEXT, "
                    "timestamp REAL, active INTEGER DEFAULT 1, compacted INTEGER DEFAULT 0)")
        con.execute("CREATE TABLE IF NOT EXISTS sessions (id TEXT PRIMARY KEY, started_at INTEGER, "
                    "ended_at INTEGER, message_count INTEGER, tool_call_count INTEGER, "
                    "last_activity_at INTEGER, profile_name TEXT, source TEXT)")
        sid = "s_%d" % (int(time.time() * 1000) % 10 ** 8)
        now = time.time()
        con.execute("INSERT INTO sessions (id, started_at, last_activity_at, message_count, "
                    "tool_call_count, profile_name, source) VALUES (?, ?, ?, ?, ?, ?, 'cli')",
                    (sid, int(now) - 500, int(now), 4, len(blocks), MEMBER))
        con.execute("INSERT INTO messages (session_id, role, content, timestamp) VALUES (?,?,?,?)",
                    (sid, "user", "Карточка %s: собери сводку по заявкам за неделю" % child,
                     now - 480))
        for kind, call, text in blocks:
            con.execute("INSERT INTO messages (session_id, role, content, tool_calls, tool_name, "
                        "timestamp) VALUES (?, 'assistant', '', ?, '', ?)",
                        (sid, call, now - 400))
            con.execute("INSERT INTO messages (session_id, role, content, tool_calls, tool_name, "
                        "timestamp) VALUES (?, 'tool', ?, '', ?, ?)",
                        (sid, text, kind, now - 399))
        con.execute("INSERT INTO messages (session_id, role, content, timestamp) VALUES (?,?,?,?)",
                    (sid, "assistant", "Готово: сводка собрана.", now - 300))
        con.commit()
        con.close()
        registry.add_request(self.cfg, self.room_id, _record(root))
        return root

    def approvals_of(self, rid: str) -> tuple:
        """``(approvals[], needs_you_count)`` живого ответа ``GET /api/requests/{id}``."""
        status, detail = self.http("GET", "/api/requests/%s" % rid)
        self.assertEqual(status, 200, detail)
        return detail.get("approvals") or [], (detail.get("needs_you_count") or {})

    def actions(self, rid: str, **body) -> tuple:
        return self.http("POST", "/api/requests/%s/actions" % rid, body)

    # --- пробы ------------------------------------------------------------
    def test_hint_agent_hints_and_drops_counter(self):
        rid = self.request_with_block(("execute_code", CALL_CODE, BLOCK_CODE))
        items, counter = self.approvals_of(rid)
        item = next((a for a in items if a.get("mode") == "workaround"), None)
        self.assertIsNotNone(item, "недоговорная блокировка не появилась: %s" % items)
        self.assertEqual(item.get("state"), "workaround")
        self.assertIn("hint_agent", [a["id"] for a in item.get("actions") or []])
        before = counter.get("approvals")
        self.assertIsNotNone(before, "в ответе нет needs_you_count")

        self.comments.clear()
        status, payload = self.actions(rid, action="hint_agent", approval_id=item["id"])
        self.assertEqual(status, 200, payload)
        self.assertEqual((payload.get("approval") or {}).get("state"), "hinted")
        self.assertEqual(payload.get("action"), "approval")
        self.assertTrue(payload.get("message"))
        # подсказка ушла подставным каналом записи — одной строкой в ветку карточки
        self.assertEqual([c["action"] for c in self.comments], ["comment"])
        self.assertIn("файл", self.comments[0]["kw"].get("text") or "")
        after_items, after = self.approvals_of(rid)
        hinted = next(a for a in after_items if a.get("id") == item["id"])
        self.assertEqual(hinted.get("state"), "hinted")
        self.assertNotIn(hinted.get("state"), ("pending", "workaround", "info"))

        # повтор того же нажатия — 409 already_decided, а не 500 и не «тишина»
        status, again = self.actions(rid, action="hint_agent", approval_id=item["id"])
        self.assertEqual(status, 409, again)
        self.assertEqual((again.get("error") or {}).get("code"), "already_decided")

    def test_trust_profile_writes_only_fake_home(self):
        rid = self.request_with_block(("execute_code", CALL_CODE, BLOCK_CODE))
        items, _counter = self.approvals_of(rid)
        item = next(a for a in items if a.get("mode") == "workaround")
        conf = self.profile_dir() / "config.yaml"
        self.assertIn("single_query_mode: deny", conf.read_text(encoding="utf-8"))

        self.workers.clear()
        status, payload = self.actions(rid, action="trust_profile", approval_id=item["id"],
                                      ttl="min30")
        self.assertEqual(status, 200, payload)
        self.assertEqual((payload.get("approval") or {}).get("state"), "trusted")
        self.assertEqual((payload.get("trust") or {}).get("scope"), "minutes")
        self.assertEqual((payload.get("trust") or {}).get("ttl_min"), 30)
        # запись — в поддельный дом профиля, и только туда
        self.assertEqual(len(self.workers), 1, self.workers)
        home = Path(self.workers[0]["home"])
        self.assertEqual(home, self.profile_dir(), "доверие записано не в профиль участника")
        self.assertTrue(str(home).startswith(str(self.fake_home())),
                        "запись ушла мимо поддельного HERMES_HOME: %s" % home)
        self.assertNotEqual(home, ap.profile_home(self.cfg, "default"),
                            "доверие записано в корневой профиль, а не в профиль участника")
        self.assertIn("single_query_mode: approve", conf.read_text(encoding="utf-8"))
        after_items, after = self.approvals_of(rid)
        trusted = next(a for a in after_items if a.get("id") == item["id"])
        self.assertEqual(trusted.get("state"), "trusted")

    def test_trust_without_ttl_is_422_with_reason(self):
        rid = self.request_with_block(("execute_code", CALL_CODE, BLOCK_CODE))
        items, _counter = self.approvals_of(rid)
        item = next(a for a in items if a.get("mode") == "workaround")
        status, payload = self.actions(rid, action="trust_profile", approval_id=item["id"])
        self.assertEqual(status, 422, payload)
        error = payload.get("error") or {}
        self.assertEqual(error.get("code"), "ttl_required")
        self.assertTrue(error.get("message"), "422 без причины — тупик для человека")

    def test_trust_on_floor_is_422_with_reason(self):
        rid = self.request_with_block(("terminal", CALL_FLOOR, BLOCK_FLOOR))
        items, _counter = self.approvals_of(rid)
        item = next((a for a in items if a.get("mode") == "floor"), None)
        self.assertIsNotNone(item, "жёсткий запрет не появился: %s" % items)
        status, payload = self.actions(rid, action="trust_profile", approval_id=item["id"],
                                      ttl="forever")
        self.assertEqual(status, 422, payload)
        self.assertEqual((payload.get("error") or {}).get("code"), "floor")
        self.assertTrue((payload.get("error") or {}).get("message"))
        # у жёсткого запрета путь решения всё равно есть — подсказка участнику
        status, payload = self.actions(rid, action="hint_agent", approval_id=item["id"])
        self.assertEqual(status, 200, payload)

    def test_unknown_action_is_still_refused_and_names_allowed(self):
        rid = self.request_with_block(("execute_code", CALL_CODE, BLOCK_CODE))
        status, payload = self.actions(rid, action="hint_bot", approval_id="ap_x")
        self.assertEqual(status, 400, payload)
        error = payload.get("error") or {}
        self.assertEqual(error.get("code"), "bad_request")
        allowed = (error.get("detail") or {}).get("allowed") or []
        for name in ("hint_agent", "trust_profile"):
            self.assertIn(name, allowed, "сервер не называет «%s» среди допустимых" % name)

    def test_hint_on_counted_item_drops_needs_you_by_one(self):
        """Счётчик: пункт, который правда «ждёт решения», после подсказки уходит из него.

        У «нельзя штатно» счётчик и так 0 (W26-D1: это не вопрос к вам), поэтому −1
        проверяется на разрешаемой блокировке — контракт `hint_agent` одинаков для всех.
        """
        rid = self.request_with_block(("terminal", CALL_HEREDOC, BLOCK_HEREDOC))
        items, counter = self.approvals_of(rid)
        item = items[0]
        self.assertEqual(item.get("mode"), "grant")
        self.assertEqual(item.get("state"), "pending")
        self.assertEqual(counter.get("approvals"), 1, counter)
        status, payload = self.actions(rid, action="hint_agent", approval_id=item["id"])
        self.assertEqual(status, 200, payload)
        self.assertEqual((payload.get("approval") or {}).get("state"), "hinted")
        _items, after = self.approvals_of(rid)
        self.assertEqual(after.get("approvals"), counter.get("approvals") - 1, after)
        self.assertFalse((after.get("total") or 0) > (counter.get("total") or 0))

    def test_results_offer_actions_are_accepted(self):
        """Пункт «Предлагаю собрать карту проекта»: своя ручка принимает `later`.

        W29-I1: интерфейс (`backlog.js`) шлёт `{action:'later'}` на
        `POST /api/rooms/{id}/results/offer` — контракт один, и он проверяется живым
        запросом, а не таблицей исключений.
        """
        status, payload = self.http("POST", "/api/rooms/%s/results/offer" % self.room_id,
                                    {"action": "later"})
        self.assertEqual(200, status, payload)
        self.assertEqual("later", payload.get("action"), payload)
        status, payload = self.http("POST", "/api/rooms/%s/results/offer" % self.room_id,
                                    {"action": "собрать-всё"})
        self.assertEqual(400, status, payload)
        allowed = ((payload.get("error") or {}).get("detail") or {}).get("allowed") or []
        self.assertIn("later", allowed, "сервер не называет допустимое действие `later`")
        self.assertIn("build", allowed)

    def test_press_every_pending_button_changes_state(self):
        """Сторож `check_dead_ends --press` на живом сервере песочницы."""
        rid = self.request_with_block(("execute_code", CALL_CODE, BLOCK_CODE))
        items, _counter = self.approvals_of(rid)
        report = cde.walk_press("http://127.0.0.1:%d" % PORT, room=self.room_id)
        self.assertEqual([], report.get("problems"), report)
        self.assertGreaterEqual(report.get("pressed") or 0, 1,
                                "сторож не нажал ни одной кнопки: %s" % report)
        after_items, _after = self.approvals_of(rid)
        pressed = next((a for a in after_items if a.get("id") == (items[0] or {}).get("id")), None)
        if pressed is not None:
            self.assertNotIn(pressed.get("state"), ("pending", "workaround", "info"),
                             "после нажатия пункт всё ещё «ждёт»")


if __name__ == "__main__":
    unittest.main(verbosity=2)
