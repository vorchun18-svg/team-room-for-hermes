"""test_dead_ends_blocked.py — W29-P2: заблокированная карточка без тупика.

Что проверяется
---------------
1. `tools/check_dead_ends.check_blocked_cards` ловит инцидент: `blocked`-карточка
   без кнопок — тупик (фикстура `fixtures/incident_blocked_no_button.json`);
2. серверная разметка (`server/stale.py`) даёт каждой карточке кнопки, а системной
   паузе сторожа — только «снять сейчас», и не считает паузу «ждёт вас»;
3. число и список — один источник: `needs_you_blocked` равно числу карточек без
   паузы, `blocked_waiting` совпадает с деревом по id;
4. `stale.is_system_pause` понимает метку сторожа и не принимает за неё обычную
   блокировку.

Тест офлайн: ни доски, ни сервера, ни комнаты — только словари.
"""
from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "server"))
sys.path.insert(0, str(ROOT / "tools"))

import check_dead_ends as cde          # noqa: E402
import stale                           # noqa: E402


def incident() -> dict:
    with open(ROOT / "fixtures" / "incident_blocked_no_button.json", encoding="utf-8") as fh:
        return json.load(fh)


class IncidentTests(unittest.TestCase):
    """Фикстура-инцидент: карточка ждёт решения, а нажать нечего."""

    def test_incident_is_caught(self):
        problems = cde.check_blocked_cards(incident(), "t_incident")
        self.assertTrue(problems, "тупик не пойман")
        ids = [p["id"] for p in problems]
        self.assertIn("t_blocked_dead", ids)
        self.assertTrue(any("без единой кнопки" in p["why"] for p in problems), problems)

    def test_healthy_cards_pass(self):
        detail = {"id": "t_1", "needs_you_blocked": 1, "blocked_waiting": [{"id": "t_a"}],
                  "tree": [{"id": "t_a", "status": "blocked",
                            "actions": [{"id": ident} for ident in stale.BLOCKED_ACTIONS]}]}
        self.assertEqual(cde.check_blocked_cards(detail, "t_1"), [])

    def test_number_must_match_list(self):
        detail = {"id": "t_2", "needs_you_blocked": 3, "blocked_waiting": [{"id": "t_a"}],
                  "tree": [{"id": "t_a", "status": "blocked",
                            "actions": [{"id": ident} for ident in stale.BLOCKED_ACTIONS]}]}
        problems = cde.check_blocked_cards(detail, "t_2")
        self.assertTrue(any(p["kind"] == "blocked-counter" for p in problems), problems)


class ServerMarkingTests(unittest.TestCase):
    """`stale.mark_blocked_actions` — один список кнопок на сервер и интерфейс."""

    def _nodes(self):
        return [{"id": "t_root", "status": "working"},
                {"id": "t_card", "status": "blocked", "title": "Сверить цены"},
                {"id": "t_pause", "status": "blocked", "title": "Ждёт модели",
                 "hold": {"by": "guard", "auto": True, "reason": "модель не подтверждена"}}]

    def test_every_blocked_card_gets_buttons(self):
        nodes = self._nodes()
        marked = stale.mark_blocked_actions(nodes)
        self.assertEqual(marked, 2)
        card = nodes[1]
        self.assertEqual([a["id"] for a in card["actions"]], list(stale.BLOCKED_ACTIONS))
        self.assertTrue(card["needs_you"])
        pause = nodes[2]
        self.assertEqual([a["id"] for a in pause["actions"]], ["resume"])
        self.assertFalse(pause["needs_you"], "системная пауза не должна ждать человека")

    def test_decorate_gives_one_source_of_number_and_list(self):
        detail = {"id": "t_3", "status": "working", "tree": self._nodes()}
        stale.mark_blocked_actions(detail["tree"])
        stale.decorate(detail)
        self.assertEqual(detail["needs_you_blocked"], 1)
        self.assertEqual([c["id"] for c in detail["blocked_waiting"]], ["t_card"])
        # число и список согласованы — проверка «без тупиков» молчит
        self.assertEqual(cde.check_blocked_cards(detail, "t_3"), [])

    def test_system_pause_marker(self):
        self.assertTrue(stale.is_system_pause({"hold": {"by": "guard"}}))
        self.assertTrue(stale.is_system_pause({"hold": {"auto": True}}))
        self.assertFalse(stale.is_system_pause({"status": "blocked"}))
        self.assertFalse(stale.is_system_pause({"hold": {"by": "user"}}))

    def test_guard_holds_without_registry_is_empty(self):
        # Каталога состояния нет — разметка просто не появляется, страница живёт.
        class Cfg:
            state_dir = str(ROOT / "nonexistent-state-dir")

        self.assertEqual(stale.guard_holds(Cfg(), "cafe"), {})
        nodes = self._nodes()
        self.assertEqual(stale.mark_system_pauses(Cfg(), "cafe", nodes), 0)


if __name__ == "__main__":
    unittest.main()
