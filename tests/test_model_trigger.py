# -*- coding: utf-8 -*-
"""W29-P10 — триггер БД: пин модели вставляется вместе с карточкой, гонки нет.

Проверяется на ПЕСОЧНИЦЕ (`tests.helpers.Sandbox`, копия доски во временной папке) и
НАСТОЯЩЕЙ библиотекой Hermes из venv (`hermes_cli.kanban_db`), а не подделкой:

1. `status` до установки: таблицы/триггеров нет, схема `tasks` подходит, ничего не пишется;
2. `install` создаёт таблицу + два триггера, делает резервную копию доски, идемпотентен;
3. установка на доску без нужных колонок — отказ `bad_schema` и НИ ОДНОГО объекта;
4. карточка `create_task` получает пин сразу (модель/провайдер/мышление) — до claim;
5. штатный specifier (`specify_triage_task`: triage → todo с исполнителем) — у карточки пин
   уже на коммите перехода, до того как её увидит диспетчер;
6. штатный декомпозитор (`decompose_triage_task`, та же функция, что зовёт
   `kanban_decompose._apply_fanout`) — у ВСЕХ детей пин на вставке;
7. гонка: 1000 вставок при живом «диспетчере» (отдельное соединение, читает без пауз) —
   0 карточек без пина в любой момент; на каждой — пин в момент `claim`;
8. `sync` идемпотентен, убирает участников, которых больше нет; без установки — пропуск;
9. правка моделей комнаты через реестр (`rooms.update_room`) обновляет таблицу;
10. `uninstall` снимает триггеры: новые карточки больше не пинятся, пины на месте;
11. «Проверка» (`model_trigger.diag_item`): off без установки, ok после, warn после
    пропажи триггеров (обновление Hermes) — и ничего не ставит сама.

Чистая машина без Hermes: тесты 4–7, 9 честно пропускаются (нет `hermes_cli`).

    python -m unittest tests.test_model_trigger -v
"""
from __future__ import annotations

import shutil
import sqlite3
import sys
import threading
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import server.kanban_atomic as atomic                 # noqa: E402
import server.model_trigger as trigger               # noqa: E402
import server.rooms as rooms_mod                     # noqa: E402
from tests.helpers import Sandbox                    # noqa: E402

ROOM = "cafe"
TENANT = "room:cafe"
CHEAP = {"provider": "llamacpp", "model": "qwen36v", "reasoning": "low"}
STRONG = {"provider": "ollama-cloud", "model": "gemma4:31b", "reasoning": "high"}
ROWS = {"coder": CHEAP, "team-director": STRONG}


def _hermes():
    """Штатная библиотека Hermes из venv (или None — тогда тесты с ней пропускаются)."""
    global _HERMES
    if _HERMES is None:
        tree = atomic.hermes_tree(_cfg_holder["cfg"]) if _cfg_holder.get("cfg") else None
        if tree is None:
            return None
        try:
            if str(tree) not in sys.path:
                sys.path.insert(0, str(tree))
            from hermes_cli import kanban_db as kb              # noqa: PLC0415
            from hermes_cli import kanban_db_connect as kbc    # noqa: PLC0415
            from hermes_cli import kanban_db_graph as kbg      # noqa: PLC0415
        except Exception:                                       # noqa: BLE001
            return None
        _HERMES = (kb, kbc, kbg)
    return _HERMES


_HERMES = None
_cfg_holder: dict = {}


def _pin_of(db: Path, card: str) -> dict:
    con = sqlite3.connect("file:%s?mode=ro" % str(db).replace("\\", "/"), uri=True)
    con.row_factory = sqlite3.Row
    try:
        row = con.execute("SELECT model_override, provider_override, reasoning_effort, status, "
                          "assignee FROM tasks WHERE id = ?", (card,)).fetchone()
    finally:
        con.close()
    return dict(row) if row is not None else {}


class TriggerBase(unittest.TestCase):
    def setUp(self) -> None:
        self._sb = Sandbox()
        self.sb = self._sb.__enter__()
        _cfg_holder["cfg"] = self.sb.cfg
        self.db = self.sb.db

    def tearDown(self) -> None:
        try:
            self._sb.__exit__(None, None, None)
        finally:
            _cfg_holder["cfg"] = None

    def make_room(self, room_id: str = "laundry", title: str = "Химчистка") -> dict:
        """Комната в песочнице: реестр комнат тесты создают сами (песочница пустая)."""
        return rooms_mod.create_room(self.sb.cfg, {
            "id": room_id, "title": title, "orchestrator": "team-director",
            "board": "team", "members": [{"profile": "coder", "role": "worker"}]})


class InstallFlow(TriggerBase):
    def test_status_is_read_only_before_install(self):
        before = self.db.stat().st_mtime_ns
        state = trigger.status(self.db)
        self.assertTrue(state["exists"])
        self.assertFalse(state["installed"])
        self.assertTrue(state["schema"]["ok"], state["schema"])
        self.assertEqual(0, state["rows"])
        self.assertEqual(before, self.db.stat().st_mtime_ns, "status не должен писать в доску")

    def test_install_creates_objects_and_backup(self):
        result = trigger.install(self.db, rows=ROWS, tenant=TENANT)
        self.assertTrue(result["healthy"], result.get("problems"))
        self.assertTrue(result["backup"], "резервная копия не сделана")
        self.assertTrue((self.db.parent / result["backup"]).is_file())
        names = {row[0] for row in sqlite3.connect(str(self.db)).execute(
            "SELECT name FROM sqlite_master WHERE type = 'trigger'")}
        self.assertTrue(set(trigger.TRIGGERS) <= names, names)
        self.assertEqual(2, result["rows_written"])
        state = trigger.status(self.db)
        self.assertTrue(state["installed"])
        self.assertTrue(state["healthy"])
        self.assertEqual(2, state["rows"])
        self.assertIn("Защита выбора модели: установлена", state["text"])

    def test_install_is_idempotent(self):
        first = trigger.install(self.db, rows=ROWS, tenant=TENANT)
        second = trigger.install(self.db, rows=ROWS, tenant=TENANT)
        self.assertTrue(second["healthy"])
        triggers = [row[0] for row in sqlite3.connect(str(self.db)).execute(
            "SELECT name FROM sqlite_master WHERE type = 'trigger' "
            "AND name LIKE 'trg_teamroom%'")]
        self.assertEqual(sorted(trigger.TRIGGERS), sorted(triggers))
        self.assertTrue(any(item.startswith("replaced:") for item in second["created"]))
        self.assertEqual(2, len(set(triggers)))
        self.assertEqual(2, first["rows_written"])

    def test_install_refuses_foreign_schema(self):
        path = self.db.parent / "foreign.db"
        con = sqlite3.connect(str(path))
        con.execute("CREATE TABLE tasks (id TEXT PRIMARY KEY, title TEXT)")
        con.commit()
        con.close()
        with self.assertRaises(trigger.TriggerError) as ctx:
            trigger.install(path, backup=False)
        self.assertEqual("bad_schema", ctx.exception.code)
        left = {row[0] for row in sqlite3.connect(str(path)).execute(
            "SELECT name FROM sqlite_master WHERE type IN ('table', 'trigger')")}
        self.assertEqual({"tasks"}, left, "после отказа в доске не должно появиться объектов")

    def test_uninstall_stops_pinning(self):
        trigger.install(self.db, rows=ROWS, tenant=TENANT)
        payload = trigger.uninstall(self.db)
        self.assertTrue(payload["ok"])
        self.assertEqual(sorted(trigger.TRIGGERS), sorted(payload["dropped"]))
        self.assertFalse(trigger.status(self.db)["installed"])
        hermes = _hermes()
        if hermes is None:
            self.skipTest("нет hermes_cli")
        kb, kbc, _kbg = hermes
        con = kbc.connect(db_path=self.db)
        try:
            card = kb.create_task(con, title="После снятия", assignee="coder", tenant=TENANT)
            self.assertEqual("", _pin_of(self.db, card).get("model_override") or "")
            left = sqlite3.connect(str(self.db)).execute(
                "SELECT count(*) FROM sqlite_master WHERE type='trigger' AND "
                "name LIKE 'trg_teamroom%'").fetchone()[0]
            self.assertEqual(0, left)
        finally:
            con.close()


class SyncAndRegistry(TriggerBase):
    def test_sync_upserts_and_removes(self):
        skipped = trigger.sync(self.db, TENANT, ROWS)
        self.assertEqual("not_installed", skipped["skipped"], "без установки таблицу не ведём")
        trigger.install(self.db, tenant=TENANT)
        result = trigger.sync(self.db, TENANT, ROWS)
        self.assertEqual(2, result["synced"])
        self.assertEqual(0, result["removed"])
        changed = {"coder": dict(CHEAP, model="cheap-small-2")}
        trigger.sync(self.db, TENANT, changed)
        con = sqlite3.connect(str(self.db))
        try:
            rows = dict(con.execute("SELECT assignee, model FROM %s WHERE tenant = ?"
                                    % trigger.TABLE, (TENANT,)).fetchall())
        finally:
            con.close()
        self.assertEqual({"coder": "cheap-small-2"}, rows, "исчезнувший участник должен уйти")

    def test_room_registry_change_updates_table(self):
        hermes = _hermes()
        if hermes is None:
            self.skipTest("нет hermes_cli")
        trigger.install(self.db, tenant="room:laundry")
        self.make_room()
        rooms_mod.update_room(self.sb.cfg, "laundry", {"agent_models": {
            "coder": {"provider": CHEAP["provider"], "model": CHEAP["model"],
                      "reasoning": CHEAP["reasoning"]}}})
        con = sqlite3.connect(str(self.db))
        try:
            rows = dict(con.execute("SELECT assignee, model FROM %s WHERE tenant = ?"
                                    % trigger.TABLE, ("room:laundry",)).fetchall())
        finally:
            con.close()
        self.assertEqual(CHEAP["model"], rows.get("coder"))
        self.assertEqual(1, len(rows), "в таблице ровно то, что выбрано в комнате")


class PinWithRealHermes(TriggerBase):
    def setUp(self) -> None:
        super().setUp()
        self.hermes = _hermes()
        if self.hermes is None:
            self.skipTest("нет hermes_cli (чистая машина)")
        trigger.install(self.db, rows=ROWS, tenant=TENANT)

    def _con(self):
        kb, kbc, _kbg = self.hermes
        return kb, kbc.connect(db_path=self.db)

    def test_create_task_pins_on_insert(self):
        kb, con = self._con()
        try:
            card = kb.create_task(con, title="Витрина кофейни", assignee="coder",
                                  tenant=TENANT, created_by="тест")
        finally:
            con.close()
        pin = _pin_of(self.db, card)
        self.assertEqual(CHEAP["model"], pin["model_override"])
        self.assertEqual(CHEAP["provider"], pin["provider_override"])
        self.assertEqual("low", pin["reasoning_effort"])

    def test_card_with_foreign_tenant_untouched(self):
        kb, con = self._con()
        try:
            card = kb.create_task(con, title="Чужая доска", assignee="coder", tenant="board:x")
        finally:
            con.close()
        self.assertIsNone(_pin_of(self.db, card)["model_override"])

    def test_explicit_pin_wins(self):
        kb, con = self._con()
        try:
            card = kb.create_task(con, title="Своя модель", assignee="coder", tenant=TENANT,
                                  model_override="manual-model", provider_override="manual")
        finally:
            con.close()
        self.assertEqual("manual-model", _pin_of(self.db, card)["model_override"])

    def test_specifier_promotes_with_pin_already_set(self):
        """triage → todo штатным specifier: пин появляется на коммите, до диспетчера."""
        kb, con = self._con()
        try:
            root = kb.create_task(con, title="Корень запроса", triage=True, tenant=TENANT,
                                  created_by="тест")
            self.assertIsNone(_pin_of(self.db, root)["model_override"],
                              "у корня в triage нет исполнителя — пина быть не должно")
            ok = kb.specify_triage_task(con, root, assignee="coder", author="triage-specifier")
            self.assertTrue(ok)
        finally:
            con.close()
        pin = _pin_of(self.db, root)
        self.assertIn(pin["status"], ("todo", "ready"))
        self.assertEqual(CHEAP["model"], pin["model_override"])

    def test_decompose_children_pinned_like_apply_fanout(self):
        """Та же функция, что зовёт `kanban_decompose._apply_fanout`: дети с пином на вставке."""
        kb, con = self._con()
        _kbg = self.hermes[2]
        try:
            root = kb.create_task(con, title="Свод по доставке", triage=True, tenant=TENANT,
                                  created_by="тест")
            children = _kbg.decompose_triage_task(
                con, root, root_assignee="team-director",
                children=[{"title": "Посчитать доставку", "assignee": "coder"},
                          {"title": "Проверить цены", "assignee": "team-director"}],
                author="auto-decomposer")
            self.assertTrue(children)
            pins = [_pin_of(self.db, child) for child in children]
            claims = []
            for child in children:
                claims.append(kb.claim_task(con, child))
        finally:
            con.close()
        self.assertEqual([CHEAP["model"], STRONG["model"]],
                         [pin["model_override"] for pin in pins])
        self.assertTrue(all(claim is not None for claim in claims), "детей не удалось взять")

    def test_race_dispatcher_never_sees_unpinned(self):
        """1000 вставок при живом «диспетчере»: 0 карточек без пина в любой момент."""
        kb, kbc, _kbg = self.hermes
        race_tenant = "room:race"
        trigger.sync(self.db, race_tenant, {"coder": CHEAP})
        writer = kbc.connect(db_path=self.db)
        uri = "file:%s?mode=ro" % str(self.db).replace("\\", "/")
        stop = threading.Event()
        violations: list = []
        polls = [0]

        def watch():
            """«Диспетчер»: своё соединение, читает без пауз, ищет карточку без пина."""
            reader = sqlite3.connect(uri, uri=True)
            try:
                while not stop.is_set():
                    row = reader.execute(
                        "SELECT id, status FROM tasks WHERE tenant = ? "
                        "AND model_override IS NULL LIMIT 1", (race_tenant,)).fetchone()
                    polls[0] += 1
                    if row is not None:
                        violations.append(tuple(row))
            finally:
                reader.close()

        thread = threading.Thread(target=watch, daemon=True)
        thread.start()
        unpinned = 0
        claimed_without_pin = 0
        try:
            for index in range(1000):
                card = kb.create_task(writer, title="Прогон %d" % index, assignee="coder",
                                      tenant=race_tenant, created_by="тест")
                row = writer.execute("SELECT model_override FROM tasks WHERE id = ?",
                                     (card,)).fetchone()
                if not row or not row[0]:
                    unpinned += 1
                if index % 10 == 0:
                    kb.claim_task(writer, card)
                    got = writer.execute("SELECT model_override FROM tasks WHERE id = ?",
                                         (card,)).fetchone()
                    if not got or not got[0]:
                        claimed_without_pin += 1
        finally:
            stop.set()
            thread.join(timeout=5)
            writer.close()
        self.assertEqual(0, unpinned, "карточки без пина сразу после коммита")
        self.assertEqual(0, claimed_without_pin, "диспетчер взял карточку без пина")
        self.assertGreater(polls[0], 100, "«диспетчер» не успел прочитать ни одной карточки")
        self.assertEqual([], violations, "диспетчер видел карточку без пина: %s" % violations[:3])
        con = sqlite3.connect(str(self.db))
        try:
            counted = con.execute("SELECT count(*) FROM tasks WHERE tenant = ? AND "
                                  "model_override IS NULL", (race_tenant,)).fetchone()[0]
        finally:
            con.close()
        self.assertEqual(0, counted)


class RealBoardCopy(TriggerBase):
    """Совместимость с НАСТОЯЩЕЙ доской: ставим защиту на КОПИЮ (реальная — только чтение)."""

    def test_real_board_copy_accepts_protection(self):
        hermes = _hermes()
        if hermes is None:
            self.skipTest("нет hermes_cli")
        kb, kbc, _kbg = hermes
        from tests.helpers import real_hermes_home
        source = real_hermes_home() / "kanban" / "boards" / "team" / "kanban.db"
        if not source.is_file():
            self.skipTest("на машине нет доски team")
        copy = self.db.parent / "real-board-copy.db"
        src = sqlite3.connect("file:%s?mode=ro" % str(source).replace("\\", "/"), uri=True)
        try:
            dst = sqlite3.connect(str(copy))
            try:
                src.backup(dst)
            finally:
                dst.close()
        finally:
            src.close()
        check = sqlite3.connect(str(copy))
        try:
            before = check.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
        finally:
            check.close()
        result = trigger.install(copy, rows={"coder": CHEAP}, tenant="room:cafe")
        self.assertTrue(result["healthy"], result.get("problems"))
        check = sqlite3.connect(str(copy))
        try:
            after = check.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
            names = {row[0] for row in check.execute("SELECT name FROM sqlite_master "
                                                    "WHERE type IN ('table', 'trigger')")}
        finally:
            check.close()
        self.assertEqual(before, after, "установка не должна менять карточки")
        self.assertIn(trigger.TABLE, names)
        con = kbc.connect(db_path=copy)
        try:
            card = kb.create_task(con, title="Проба на копии доски", assignee="coder",
                                  tenant="room:cafe", created_by="тест")
        finally:
            con.close()
        pin = _pin_of(copy, card)
        self.assertEqual(CHEAP["model"], pin["model_override"])
        self.assertEqual("low", pin["reasoning_effort"])
        trigger.uninstall(copy)
        self.assertFalse(trigger.status(copy)["installed"])


class Diagnostics(TriggerBase):
    def test_diag_off_then_ok_then_warn(self):
        self.make_room()
        room = rooms_mod.get_room(self.sb.cfg, "laundry")
        empty = trigger.diag_item(self.sb.cfg, room)
        self.assertIn(empty["status"], ("off", "warn"))
        trigger.install(self.db, rows=ROWS, tenant=TENANT)
        ok = trigger.diag_item(self.sb.cfg, room)
        self.assertEqual("ok", ok["status"], ok)
        con = sqlite3.connect(str(self.db))
        try:                                   # так выглядит обновление Hermes: триггеры сняты
            con.execute("DROP TRIGGER %s" % trigger.TRIGGER_INSERT)
            con.commit()
        finally:
            con.close()
        warn = trigger.diag_item(self.sb.cfg, room)
        self.assertEqual("warn", warn["status"], warn)
        self.assertIn("Переустановите", " ".join([warn.get("hint") or ""]
                                                 + list(warn.get("details") or [])))
        state = trigger.status(self.db)
        self.assertFalse(state["installed"], "проверка не должна сама себя лечить записью")


if __name__ == "__main__":
    unittest.main(verbosity=2)
