"""Подписи ВСЕХ участников, включая руководителя и критика (WAVE17, B-17).

Проверяем ровно то, что просил аудит:

* подпись руководителя комнаты и критика сохраняется в реестре комнаты
  (`role_labels`), профиль Hermes при этом не меняется;
* та же операция у исполнителя — прежнее поле `label` в `members` (регрессия);
* сброс подписи (пустая строка) убирает её и у руководителя/критика;
* блок команды (`routes_read._with_labels`) отдаёт подпись руководителя и
  критика тем же полем `label`, что и у исполнителей, — интерфейс не заводит
  отдельного правила для «главных» участников.
"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from server import config, profile_info, rooms  # noqa: E402
from server.routes_read import ReadRoutes            # noqa: E402

LEAD = "default"
CRITIC = "biz-critic"
WORKER = "lawyer"


class TeamLabelsBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="tr-labels-")
        self.tmp = Path(self._tmp.name)
        self._env = mock.patch.dict(os.environ, {
            "TEAMROOM_DATA": str(self.tmp / "data"),
            "TEAMROOM_VAULT_DIR": str(self.tmp / "vault"),
            "TEAMROOM_HERMES_HOME": str(self.tmp / "hermes"),
        })
        self._env.start()
        self.cfg = config.load()
        self.cfg.ensure_dirs()
        profile_info.reset()
        self.room = rooms.create_room(self.cfg, {
            "id": "w17e", "title": "Проверка подписей", "orchestrator": LEAD, "critic": CRITIC,
            "members": [{"profile": WORKER, "note": "Юридические вопросы"}],
        })

    def tearDown(self):
        profile_info.reset()
        self._env.stop()
        self._tmp.cleanup()

    def room_now(self) -> dict:
        return rooms.get_room(self.cfg, "w17e")


class TestSetLabel(TeamLabelsBase):
    def test_lead_label_saved_in_room_registry(self):
        res = rooms.set_member_label(self.cfg, "w17e", LEAD, "  Михаил —  руководитель комнаты ")
        self.assertEqual(res["label"], "Михаил — руководитель комнаты")
        self.assertEqual(res["role_labels"][LEAD], "Михаил — руководитель комнаты")
        room = self.room_now()
        self.assertEqual(room["role_labels"][LEAD], "Михаил — руководитель комнаты")
        # Профиль Hermes не менялся: подпись живёт только в реестре комнаты.
        self.assertEqual([m["profile"] for m in room["members"]], [WORKER])
        self.assertTrue(all(m["label"] == "" for m in room["members"]))

    def test_critic_label_saved_and_reset(self):
        res = rooms.set_member_label(self.cfg, "w17e", CRITIC, "Атлас — критик, красная команда")
        self.assertEqual(res["role_labels"][CRITIC], "Атлас — критик, красная команда")
        res = rooms.set_member_label(self.cfg, "w17e", CRITIC, "")
        self.assertNotIn(CRITIC, res["role_labels"])
        self.assertEqual(self.room_now()["role_labels"], {})

    def test_worker_label_still_in_members(self):
        res = rooms.set_member_label(self.cfg, "w17e", WORKER, "Лев — договоры и визы")
        member = [m for m in res["members"] if m["profile"] == WORKER][0]
        self.assertEqual(member["label"], "Лев — договоры и визы")
        self.assertEqual(res["role_labels"], {})
        self.assertEqual(self.room_now()["members"][0]["label"], "Лев — договоры и визы")

    def test_unknown_profile_not_found(self):
        with self.assertRaises(rooms.RoomsError) as ctx:
            rooms.set_member_label(self.cfg, "w17e", "ghost", "Кто-то")
        self.assertEqual(ctx.exception.code, "not_found")

    def test_label_is_one_line_and_limited(self):
        res = rooms.set_member_label(self.cfg, "w17e", LEAD, "Имя\n—  должность   " + "х" * 300)
        self.assertNotIn("\n", res["label"])
        self.assertEqual(len(res["label"]), 120)

    def test_room_payload_has_role_labels(self):
        room = self.room_now()
        self.assertIn("role_labels", room)
        self.assertEqual(room["role_labels"], {})


class TestTeamBlockLabels(TeamLabelsBase):
    """Команда: руководитель и критик получают подпись тем же полем `label`."""

    def _routes(self, members):
        route = ReadRoutes.__new__(ReadRoutes)
        route.cfg = self.cfg
        route.log = None
        route._profile_descriptions = lambda: {LEAD: "", CRITIC: "", WORKER: ""}
        return route

    def _decorate_team(self, members):
        route = self._routes(members)
        with mock.patch.object(profile_info, "labels", lambda cfg, names, now=None: {}):
            return route._with_labels(self.room_now(), members)

    def test_lead_and_critic_labels_visible_in_team(self):
        rooms.set_member_label(self.cfg, "w17e", LEAD, "Михаил — руководитель комнаты")
        rooms.set_member_label(self.cfg, "w17e", CRITIC, "Атлас — критик, красная команда")
        members = [
            {"profile": LEAD, "role": "orchestrator"},
            {"profile": CRITIC, "role": "critic"},
            {"profile": WORKER, "role": "worker"},
        ]
        out = {m["profile"]: m for m in self._decorate_team(members)}
        self.assertEqual(out[LEAD]["label"], "Михаил — руководитель комнаты")
        self.assertEqual(out[LEAD]["label_source"], "member")
        self.assertEqual(out[LEAD]["name"], "Михаил")
        self.assertEqual(out[CRITIC]["label"], "Атлас — критик, красная команда")
        # Без подписи — роль, а не сухое «Исполнитель lawyer» (B-17).
        self.assertEqual(out[WORKER]["label"], "")
        self.assertEqual(out[WORKER]["label_source"], "role")

    def test_member_label_wins_over_role_label(self):
        # Руководитель может быть и в членах комнаты: поле участника старше карты ролей.
        rooms.set_member_label(self.cfg, "w17e", LEAD, "Из карты ролей")
        room = self.room_now()
        room["members"].append({"profile": LEAD, "role": "worker", "label": "Из участника"})
        route = self._routes([])
        with mock.patch.object(profile_info, "labels", lambda cfg, names, now=None: {}):
            out = route._with_labels(room, [{"profile": LEAD, "role": "orchestrator"}])
        self.assertEqual(out[0]["label"], "Из участника")


if __name__ == "__main__":
    unittest.main()
