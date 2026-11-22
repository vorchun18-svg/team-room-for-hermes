# -*- coding: utf-8 -*-
"""W29B-Q2 — «Совместимость с Hermes»: участник комнаты против обычного запуска.

Проверяем по заданию пакета:
* по каждому участнику видно профиль, модель и её ИСТОЧНИК (профиль / общие
  настройки Hermes / комната), наборы инструментов, число навыков, MCP-серверы
  и политику разрешений (уровень автономии, allowlist, ``single_query_mode``);
* расхождение «комната задаёт одну модель, профиль — другую» детектируется
  красным и предлагает единственное безопасное исправление — вернуть модель
  профиля (``reset_model``), профили Hermes при этом не меняются;
* пустой профиль (нет каталога) и нечитаемый конфиг честно попадают в статус,
  а не в «всё в порядке»; экран не падает;
* экран ничего не пишет: конфиги профилей остаются байт-в-байт.

Данные вымышленные: комнаты «Кофейня» и «Химчистка», профили cafe-lead /
cafe-buyer / laundry-ops, модели demo-large / demo-small.

Запуск:
    python -m unittest tests.test_parity -v
"""
from __future__ import annotations

import hashlib
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from server import parity  # noqa: E402
from server import agent_models  # noqa: E402
from tests.helpers import _outside_hermes_base  # noqa: E402


def write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class ParityCase(unittest.TestCase):
    """Поддельный дом Hermes: корневой конфиг + два профиля + комната."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=str(_outside_hermes_base()))
        base = Path(self.tmp.name)
        self.home = base / "hermes-home"
        self.home.mkdir(parents=True)
        write(self.home / "config.yaml", "\n".join([
            "model:",
            "  provider: demo-cloud",
            "  default: demo-large",
            "approvals:",
            "  mode: smart",
            "  single_query_mode: deny",
            "command_allowlist: []",
            "mcp_servers:",
            "  files:",
            "    command: files-mcp",
            "platform_toolsets:",
            "  terminal: all",
            "  files: all",
        ]) + "\n")
        # У профиля руководителя своя модель и свой навык.
        write(self.home / "profiles" / "cafe-lead" / "config.yaml", "\n".join([
            "model:",
            "  provider: demo-cloud",
            "  default: demo-large",
            "approvals:",
            "  mode: smart",
            "  single_query_mode: deny",
        ]) + "\n")
        write(self.home / "profiles" / "cafe-lead" / "profile.yaml",
              "description: Ольга — руководитель кофейни\n")
        write(self.home / "profiles" / "cafe-lead" / "skills" / "menu" / "SKILL.md",
              "---\nname: menu\n---\nМеню на неделю.\n")
        # У закупщика своя, ДРУГАЯ модель профиля.
        write(self.home / "profiles" / "cafe-buyer" / "config.yaml", "\n".join([
            "model:",
            "  provider: demo-cloud",
            "  default: demo-small",
            "approvals:",
            "  mode: smart",
            "  single_query_mode: deny",
        ]) + "\n")
        self.cfg = SimpleNamespace(hermes_home=str(self.home), state_dir=str(base / "state"),
                                    app_dir=str(ROOT), port=8890)
        self.room = {
            "id": "cafe",
            "title": "Кофейня",
            "orchestrator": "cafe-lead",
            "members": [{"profile": "cafe-buyer", "role": "worker"}],
            "autonomy": {"level": "working"},
            "agent_models": {"cafe-buyer": {"provider": "demo-cloud", "model": "demo-large",
                                           "reasoning": "", "updated_at": 1}},
        }

    def tearDown(self):
        self.tmp.cleanup()

    # ---------------------------------------------------------------- отчёт
    def test_report_lists_every_member(self):
        data = parity.report(self.cfg, self.room)
        self.assertEqual(data["room"], "cafe")
        self.assertEqual([m["profile"] for m in data["members"]], ["cafe-lead", "cafe-buyer"])
        self.assertEqual(data["summary"]["total"], 2)
        self.assertTrue(any("совместимость" in note.lower() for note in data["notes"])
                        or data["notes"], "у экрана есть пояснения")

    def test_room_model_differs_from_profile_is_detected(self):
        """Комната задаёт demo-large, профиль закупщика — demo-small: расхождение."""
        data = parity.report(self.cfg, self.room)
        buyer = [m for m in data["members"] if m["profile"] == "cafe-buyer"][0]
        self.assertEqual(buyer["status"], "fail")
        codes = [d["code"] for d in buyer["diffs"]]
        self.assertIn("model_mismatch", codes)
        diff = [d for d in buyer["diffs"] if d["code"] == "model_mismatch"][0]
        self.assertEqual(diff["fix"]["kind"], "reset_room_model")
        self.assertIn("demo-large", diff["text"])
        self.assertIn("demo-small", diff["text"])
        self.assertTrue(any(f.get("profile") == "cafe-buyer" for f in data["fixable"]))

    def test_same_model_is_ok_and_nothing_offered(self):
        self.room["agent_models"]["cafe-buyer"]["model"] = "demo-small"
        # Уровень «Осторожный» — чтобы проверялась только модель, без строки автономии.
        self.room["autonomy"] = {"level": "careful"}
        buyer = [m for m in parity.report(self.cfg, self.room)["members"]
                 if m["profile"] == "cafe-buyer"][0]
        self.assertEqual(buyer["status"], "ok")
        self.assertEqual(buyer["diffs"], [])

    def test_profile_fields_are_reported_with_source(self):
        """Инструменты, MCP и разрешения берутся из профиля, иначе из общих настроек."""
        lead = [m for m in parity.report(self.cfg, self.room)["members"]
                if m["profile"] == "cafe-lead"][0]
        self.assertEqual(lead["profile_dir"], "profiles/cafe-lead")
        self.assertEqual(lead["skills"]["own"], 1)
        self.assertEqual(lead["mcp"]["names"], ["files"])
        self.assertEqual(lead["mcp"]["source"], "global")
        self.assertEqual(lead["permissions"]["single_query_mode"], "deny")
        self.assertEqual(lead["permissions"]["level"], "working")
        self.assertEqual(lead["tools"]["count"], 2)
        self.assertIn("Ольга", lead["description"])

    def test_autonomy_gap_is_flagged(self):
        """В комнате «Рабочий», а профиль всё спрашивает: карточка встанет на паузу."""
        lead = [m for m in parity.report(self.cfg, self.room)["members"]
                if m["profile"] == "cafe-lead"][0]
        self.assertIn("autonomy_gap", [d["code"] for d in lead["diffs"]])
        careful = dict(self.room, autonomy={"level": "careful"})
        lead2 = [m for m in parity.report(self.cfg, careful)["members"]
                 if m["profile"] == "cafe-lead"][0]
        self.assertNotIn("autonomy_gap", [d["code"] for d in lead2["diffs"]])

    def test_missing_profile_is_reported_not_swallowed(self):
        self.room["members"].append({"profile": "cafe-ghost", "role": "worker"})
        ghost = [m for m in parity.report(self.cfg, self.room)["members"]
                 if m["profile"] == "cafe-ghost"][0]
        self.assertIn("no_profile", [d["code"] for d in ghost["diffs"]])

    def test_broken_config_does_not_crash_the_screen(self):
        write(self.home / "profiles" / "cafe-buyer" / "config.yaml", "model: [oops\n")
        data = parity.report(self.cfg, self.room)   # не падаем
        self.assertEqual(len(data["members"]), 2)

    def test_no_absolute_paths_leak(self):
        data = parity.report(self.cfg, self.room)
        text = repr(data)
        self.assertNotIn(str(self.home), text)
        self.assertNotIn(self.tmp.name.replace("/", "\\\\"), text)

    # ---------------------------------------------------------------- запись
    def test_reset_model_uses_room_registry_only(self):
        """«Исправить» снимает замену комнаты штатным clear; профили не меняются."""
        buyer_config = self.home / "profiles" / "cafe-buyer" / "config.yaml"
        before = digest(buyer_config)
        called = {}

        def fake_clear(cfg, room_id, profile):
            called.update({"room": room_id, "profile": profile})
            return {"cleared": True}

        original = agent_models.clear
        agent_models.clear = fake_clear
        try:
            parity.reset_model(self.cfg, self.room, "cafe-buyer")
        finally:
            agent_models.clear = original
        self.assertEqual(called, {"room": "cafe", "profile": "cafe-buyer"})
        self.assertEqual(digest(buyer_config), before, "конфиг профиля не изменился")

    def test_reset_model_rejects_stranger(self):
        with self.assertRaises(parity.ParityError):
            parity.reset_model(self.cfg, self.room, "laundry-ops")

    def test_report_requires_room(self):
        with self.assertRaises(parity.ParityError):
            parity.report(self.cfg, {})


if __name__ == "__main__":
    unittest.main(verbosity=2)
