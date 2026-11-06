# -*- coding: utf-8 -*-
"""Пакет M волны 19 — карточка команды: модель, закрепление, честное предупреждение.

Что проверяется:

* **B-28. «Закрепить в составе / Открепить»** — обычный флаг реестра комнаты
  (`rooms.set_member_pinned`) и маршрут `PATCH /api/rooms/{id}/members/{profile}`:
  закрепление/снятие сохраняется, руководитель и критик отклоняются (`409`,
  `detail.reason="mandatory_role"`), чужой профиль — `404`, без поля — `400`.
* **B-29. Предупреждение «последний запуск шёл на другой модели»** — только когда
  карточка РЕАЛЬНО ушла не на выбранную модель ПОСЛЕ последней смены модели
  (`agent_models.member_sent`): если смена позже последнего запуска или в очереди
  уже есть карточка с верной моделью — `pending=true`, `mismatch=false`,
  текст «Новая модель применится со следующей задачи». После смены ушла не туда —
  `mismatch=true`.

Правила: только песочница `tests.helpers.Sandbox` (реальный Hermes/Vault — read-only,
проверяется `snapshot_real`), CLI Hermes не вызывается, реальные агенты не запускаются.

    python -m unittest tests.test_team_fix -v
"""
from __future__ import annotations

import json
import sqlite3
import sys
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import server.agent_models as models_mod          # noqa: E402
import server.app as app_mod                      # noqa: E402
import server.rooms as rooms_mod                  # noqa: E402
from server.http_util import ApiError             # noqa: E402
from tests.helpers import Sandbox, SimAgents      # noqa: E402

ROOM = "cafe"
LEAD = "biz-director"
CRITIC = "biz-critic"
WORKER = "biz-cfo"
PINNED_WORKER = "biz-lawyer"

CATALOG = {
    "providers": [
        {"id": "llamacpp", "label": "LlamaCPP", "configured": True,
         "models": [{"id": "qwen36"}, {"id": "gemma4"}]},
        {"id": "ollama-cloud", "label": "Ollama Cloud", "configured": True,
         "models": [{"id": "glm-5.3-flash"}, {"id": "glm-5.3"}]},
        {"id": "anthropic", "label": "Anthropic", "configured": True,
         "models": [{"id": "claude-haiku-5-5"}]},
    ]
}
NEW_PAIR = ("anthropic", "claude-haiku-5-5", "max")
OLD_PAIR = ("ollama-cloud", "glm-5.3-flash", "high")
PENDING_TEXT = "Новая модель применится со следующей задачи"


class TeamFixBase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.sb = Sandbox().__enter__()
        cls.cfg = cls.sb.cfg
        cls.cfg.model_catalog_override = CATALOG
        cls.real_before = cls.sb.snapshot_real()
        cls.sa = SimAgents(cls.sb)
        cls.app = app_mod.App(cls.cfg, port=0)
        cls.app.call("rooms", "seed_defaults", ((cls.cfg,), {}))
        cls._tmp = Path(cls.sb.root) / "model-sessions"
        cls._tmp.mkdir(parents=True, exist_ok=True)
        cls._session_seq = 0

    @classmethod
    def tearDownClass(cls):
        try:
            cls.sb.assert_real_untouched(cls.real_before)
        finally:
            try:
                del cls.cfg.model_catalog_override
            except AttributeError:
                pass
            models_mod._SESSION_CACHE.clear()
            cls.sb.__exit__(None, None, None)

    def setUp(self):
        # Каждый тест — с чистого выбора модели в комнате и без «висящих» карточек
        # прошлых тестов (иначе очередь соседнего теста выдаёт себя за новую).
        rooms_mod.update_room(self.cfg, ROOM, {"agent_models": {}})
        con = self.sa.connect()
        try:
            con.execute("UPDATE tasks SET status='done' WHERE tenant='room:cafe' "
                        "AND status IN ('triage','todo','ready','blocked','queued','running','review')")
            con.commit()
        finally:
            con.close()
        models_mod._SESSION_CACHE.clear()
        self._real_state_path = models_mod.state_path
        models_mod.state_path = self._state_path

    def tearDown(self):
        models_mod.state_path = self._real_state_path
        models_mod._SESSION_CACHE.clear()

    # --- стенд: сессии воркера и прогоны карточек --------------------------
    def _state_path(self, cfg, profile) -> Path:
        name = str(profile or "default").replace("/", "_")
        path = self._tmp / ("state-%s.db" % name)
        if not path.exists():
            con = sqlite3.connect(str(path))
            try:
                con.execute("CREATE TABLE IF NOT EXISTS sessions (id TEXT PRIMARY KEY, "
                            "model TEXT, billing_provider TEXT)")
                con.commit()
            finally:
                con.close()
        return path

    def _session(self, profile: str, session_id: str, model: str, provider: str) -> None:
        con = sqlite3.connect(str(self._state_path(self.cfg, profile)))
        try:
            con.execute("INSERT OR REPLACE INTO sessions (id, model, billing_provider) "
                        "VALUES (?,?,?)", (session_id, model, provider))
            con.commit()
        finally:
            con.close()

    def _room(self) -> dict:
        return rooms_mod.get_room(self.cfg, ROOM)

    def _team(self) -> dict:
        status, body, _h = self.app.dispatch("GET", "/api/rooms/%s/team" % ROOM, {}, {})
        self.assertEqual(status, 200, body)
        return body

    def _set_room_model(self, profile: str, pair, updated_at: int | None = None) -> None:
        """Записать выбор комнаты напрямую в реестр (как это делает API)."""
        room = self._room()
        settings = dict(room.get("agent_models") or {})
        item = {"provider": pair[0], "model": pair[1], "reasoning": pair[2]}
        if updated_at is not None:
            item["updated_at"] = int(updated_at)
        settings[profile] = item
        rooms_mod.update_room(self.cfg, ROOM, {"agent_models": settings})

    def _card(self, assignee: str, *, created_at: int, status: str = "ready",
              pair=None) -> str:
        cid = self.sa.card(title="Карточка %s" % assignee, assignee=assignee,
                           status=status, tenant="room:cafe", created_by="default")
        con = self.sa.connect()
        try:
            con.execute("UPDATE tasks SET created_at = ? WHERE id = ?", (int(created_at), cid))
            if pair:
                con.execute("UPDATE tasks SET model_override = ?, provider_override = ?, "
                            "reasoning_effort = ? WHERE id = ?",
                            (pair[1], pair[0], pair[2], cid))
            con.commit()
        finally:
            con.close()
        return cid

    def _run(self, card_id: str, profile: str, session_id: str, started_at: int) -> int:
        con = self.sa.connect()
        try:
            con.execute("UPDATE tasks SET status='running', started_at=? WHERE id=?",
                        (int(started_at), card_id))
            con.execute("INSERT INTO task_runs (task_id, profile, status, started_at, metadata) "
                        "VALUES (?,?,?,?,?)",
                        (card_id, profile, "running", int(started_at),
                         json.dumps({"worker_session_id": session_id})))
            run_id = con.execute("SELECT max(id) FROM task_runs WHERE task_id = ?",
                                 (card_id,)).fetchone()[0]
            con.execute("UPDATE tasks SET current_run_id=? WHERE id=?", (run_id, card_id))
            con.execute("INSERT INTO task_events (task_id, run_id, kind, payload, created_at) "
                        "VALUES (?,?,?,?,?)",
                        (card_id, run_id, "owner_admitted",
                         json.dumps({"session_id": session_id}), int(started_at) + 1))
            con.commit()
        finally:
            con.close()
        return int(run_id)


class TestPinMember(TeamFixBase):
    """B-28: закрепление — флаг реестра комнаты, не значок на карточке."""

    def test_pin_and_unpin_worker(self):
        res = rooms_mod.set_member_pinned(self.cfg, ROOM, WORKER, True)
        self.assertTrue(res["pinned"])
        member = next(m for m in self._room()["members"] if m["profile"] == WORKER)
        self.assertTrue(member["pinned"])
        res = rooms_mod.set_member_pinned(self.cfg, ROOM, WORKER, False)
        self.assertFalse(res["pinned"])
        member = next(m for m in self._room()["members"] if m["profile"] == WORKER)
        self.assertFalse(member["pinned"])

    def test_pin_keeps_other_members(self):
        before = {m["profile"]: m["pinned"] for m in self._room()["members"]}
        rooms_mod.set_member_pinned(self.cfg, ROOM, WORKER, True)
        after = {m["profile"]: m["pinned"] for m in self._room()["members"]}
        for profile, was in before.items():
            if profile == WORKER:
                continue
            self.assertEqual(after[profile], was, "чужое закрепление не меняется")

    def test_mandatory_roles_cannot_be_pinned(self):
        for profile in (LEAD, CRITIC):
            with self.subTest(profile=profile):
                with self.assertRaises(rooms_mod.RoomsError) as ctx:
                    rooms_mod.set_member_pinned(self.cfg, ROOM, profile, True)
                self.assertEqual(ctx.exception.code, "conflict")
                self.assertEqual(ctx.exception.detail.get("reason"), "mandatory_role")

    def test_unknown_profile_not_found(self):
        with self.assertRaises(rooms_mod.RoomsError) as ctx:
            rooms_mod.set_member_pinned(self.cfg, ROOM, "ghost", True)
        self.assertEqual(ctx.exception.code, "not_found")

    def test_route_patch_pin(self):
        path = "/api/rooms/%s/members/%s" % (ROOM, WORKER)
        try:
            status, body, _h = self.app.dispatch("PATCH", path, {}, {"pinned": True})
        except ApiError as exc:                     # pragma: no cover - не должно случаться
            self.fail("PATCH pin: %s %s" % (exc.status, exc.message))
        self.assertEqual(status, 200, body)
        self.assertTrue(body["pinned"])
        self.assertTrue(next(m for m in body["members"] if m["profile"] == WORKER)["pinned"])

        status, body, _h = self.app.dispatch("PATCH", path, {}, {"pinned": False})
        self.assertEqual(status, 200, body)
        self.assertFalse(body["pinned"])

    def test_route_pin_rejections(self):
        cases = [
            ("/api/rooms/%s/members/%s" % (ROOM, WORKER), {}, 400),
            ("/api/rooms/%s/members/%s" % (ROOM, WORKER), {"pinned": None}, 400),
            ("/api/rooms/%s/members/%s" % (ROOM, WORKER), {"pinned": {"x": 1}}, 400),
            ("/api/rooms/%s/members/%s" % (ROOM, LEAD), {"pinned": True}, 409),
            ("/api/rooms/%s/members/%s" % (ROOM, "ghost"), {"pinned": True}, 404),
        ]
        for path, payload, want in cases:
            with self.subTest(path=path, payload=payload):
                try:
                    status, body, _h = self.app.dispatch("PATCH", path, {}, payload)
                except ApiError as exc:
                    status, body = exc.status, exc.payload()
                self.assertEqual(status, want, body)

    def test_team_block_mandatory_roles_are_in_members(self):
        """Руководитель и критик приходят в /team с role, а не «вне состава»."""
        members = {m["profile"]: m for m in self._team()["members"]}
        self.assertEqual(members[LEAD]["role"], "orchestrator")
        self.assertEqual(members[CRITIC]["role"], "critic")


class TestModelSentAfterChange(TeamFixBase):
    """B-29: тревога — только реальное расхождение ПОСЛЕ смены модели."""

    def test_change_after_last_run_is_pending(self):
        now = int(time.time())
        card = self._card(WORKER, created_at=now - 4000, status="done", pair=OLD_PAIR)
        self._session(WORKER, "sess-old", OLD_PAIR[1], OLD_PAIR[0])
        self._run(card, WORKER, "sess-old", now - 3600)
        self._set_room_model(WORKER, NEW_PAIR, updated_at=now - 60)

        sent = models_mod.member_sent(self.cfg, self._room(), WORKER)
        self.assertTrue(sent["confirmed"], sent)
        self.assertFalse(sent["mismatch"], "смена позже запуска — не тревога: %s" % sent)
        self.assertTrue(sent["pending"])
        self.assertEqual(sent["pending_text"], PENDING_TEXT)
        self.assertEqual(sent["text"], "ушло на: ollama-cloud · glm-5.3-flash · высокие")

    def test_wrong_model_after_change_warns(self):
        now = int(time.time())
        self._set_room_model(WORKER, NEW_PAIR, updated_at=now - 900)
        card = self._card(WORKER, created_at=now - 700)
        self._session(WORKER, "sess-new", OLD_PAIR[1], OLD_PAIR[0])
        self._run(card, WORKER, "sess-new", now - 600)

        sent = models_mod.member_sent(self.cfg, self._room(), WORKER)
        self.assertTrue(sent["mismatch"], "карточка ушла не туда ПОСЛЕ смены: %s" % sent)
        self.assertFalse(sent["pending"])
        self.assertEqual(sent["pending_text"], "")

    def test_right_card_in_queue_is_pending(self):
        now = int(time.time())
        self._set_room_model(WORKER, NEW_PAIR, updated_at=now - 900)
        card = self._card(WORKER, created_at=now - 700)
        self._session(WORKER, "sess-new", OLD_PAIR[1], OLD_PAIR[0])
        self._run(card, WORKER, "sess-new", now - 600)
        # Следующая карточка уже создана и на ней записана верная модель.
        queued = self._card(WORKER, created_at=now - 300, status="ready", pair=NEW_PAIR)

        sent = models_mod.member_sent(self.cfg, self._room(), WORKER)
        self.assertFalse(sent["mismatch"], "в очереди верная карточка: %s" % sent)
        self.assertTrue(sent["pending"])
        self.assertEqual(sent["queue"]["id"], queued)

    def test_team_endpoint_reports_pending(self):
        now = int(time.time())
        card = self._card(WORKER, created_at=now - 4000, status="done")
        self._session(WORKER, "sess-team", OLD_PAIR[1], OLD_PAIR[0])
        self._run(card, WORKER, "sess-team", now - 3600)
        self._set_room_model(WORKER, NEW_PAIR, updated_at=now - 60)

        member = next(m for m in self._team()["members"] if m["profile"] == WORKER)
        self.assertEqual(member["model_settings"]["model"], NEW_PAIR[1])
        self.assertTrue(member["model_sent"]["pending"])
        self.assertFalse(member["model_sent"]["mismatch"])

    def test_settings_updated_at_is_saved(self):
        """Время смены модели сохраняется: без него сравнение невозможно."""
        models_mod.set(self.cfg, ROOM, WORKER, NEW_PAIR[0], NEW_PAIR[1], NEW_PAIR[2])
        saved = (self._room().get("agent_models") or {}).get(WORKER) or {}
        self.assertEqual(saved.get("model"), NEW_PAIR[1])
        self.assertTrue(int(saved.get("updated_at") or 0) > 0)
        settings = models_mod.model_settings(self.cfg, self._room(), WORKER)
        self.assertEqual(settings["updated_at"], int(saved["updated_at"]))


if __name__ == "__main__":
    unittest.main(verbosity=2)
