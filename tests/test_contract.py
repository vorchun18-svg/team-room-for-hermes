# -*- coding: utf-8 -*-
"""Тесты тел карточек (server/contract.py): дословный текст, блоки контракта, пути.

Запуск из корня проекта:
    python -m unittest tests.test_contract -v
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from server import contract  # noqa: E402


class FakeCfg:
    """Минимальный cfg: контракту нужны только три каталога."""

    def __init__(self, base: Path):
        self.vault_dir = base / "Vault" / "Rooms"
        self.reports_dir = base / "data" / "reports"
        self.reportkit_dir = base / "reportkit"


def make_room(**over):
    room = {
        "id": "cafe",
        "title": "Лето",
        "board": "team",
        "tenant": "room:cafe",
        "orchestrator": "team-director",
        "critic": "team-critic",
        "members": [
            {"profile": "team-lawyer", "role": "worker", "note": "юрист по праву России"},
            {"profile": "team-ops", "role": "worker", "note": ""},
            {"profile": "team-critic", "role": "critic", "note": "критик"},
        ],
        "context_files": [r"C:\TeamRoom\Vault\Projects\Rooms\cafe\CONTEXT.md"],
        "critic_pass": True,
    }
    room.update(over)
    return room


REQUEST = ("Проверь, пожалуйста, «Алвара Sanitário» — что делать и в каком порядке?\r\n"
           "Второй абзац: кавычки \"двойные\", тире — и дефис - внутри.\r\n"
           "- строк, начинающихся с дефиса, тоже касается\r\n"
           "Третья строка без завершающего перевода.")


class RequestBodyTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="tr-contract-")
        self.cfg = FakeCfg(Path(self.tmp.name))
        self.room = make_room()

    def tearDown(self):
        self.tmp.cleanup()

    def test_text_is_verbatim_and_first(self):
        body = contract.build_request_body(self.room, REQUEST, "r-20261028-0931-ab12", self.cfg)
        self.assertTrue(body.startswith(REQUEST), "тело должно начинаться с текста пользователя")
        self.assertIn(REQUEST, body)             # CRLF, кавычки и дефисы сохранены
        self.assertIn("\r\n", body)
        self.assertNotIn("\ufeff", body)
        self.assertIn("«Алвара Sanitário»", body)

    def test_team_line_lists_only_room_profiles(self):
        body = contract.build_request_body(self.room, "текст", "r-slug", self.cfg)
        self.assertIn("Команда комнаты (назначай подзадачи только этим профилям):", body)
        line = next(l for l in body.splitlines() if l.startswith("Команда комнаты"))
        for profile in ("team-director", "team-lawyer", "team-ops", "team-critic"):
            self.assertIn(profile, line)
        self.assertNotIn("team-cfo", line)
        self.assertIn("директор комнаты", line)
        self.assertIn("юрист по праву России", line)

    def test_context_files_listed(self):
        body = contract.build_request_body(self.room, "текст", "r-slug", self.cfg)
        self.assertIn("Контекст проекта (файлы комнаты, читать первыми):", body)
        self.assertIn(str(Path(r"C:\TeamRoom\Vault\Projects\Rooms\cafe\CONTEXT.md")), body)

    def test_contract_block_has_real_paths_and_words(self):
        body = contract.build_request_body(self.room, "текст", "r-20261028-0931-ab12", self.cfg)
        self.assertIn(contract.CONTRACT_OPEN, body)
        self.assertIn(contract.CONTRACT_CLOSE, body)
        state = str(Path(self.cfg.vault_dir) / "cafe" / "STATE.md")
        spec = str(Path(self.cfg.reportkit_dir) / "SPEC.md")
        report = str(Path(self.cfg.reports_dir) / "cafe" / "r-20261028-0931-ab12" / "report.json")
        self.assertIn(state, body)
        self.assertIn(spec, body)
        self.assertIn(report, body)
        # ключи metadata из ARCH §5
        for key in ("next_steps", "open_questions", "decisions", "stage",
                    "future_questions", "artifacts", "report"):
            self.assertIn(key, body)
        # формулировки, которые требует контракт
        self.assertIn("по-русски", body)
        self.assertIn("ОДИН связный текст", body)
        self.assertIn("в подзадачи не копировать", body)
        self.assertIn("kanban_complete(summary=", body)

    def test_attach_paths_appended(self):
        attach = [r"C:\TeamRoom\Team\team-room\data\uploads\r-slug\brief.pdf",
                  r"C:\TeamRoom\Team\team-room\data\uploads\r-slug\photo.png"]
        body = contract.build_request_body(self.room, "текст", "r-slug", self.cfg, attach=attach)
        self.assertIn("Приложенные файлы", body)
        for path in attach:
            self.assertIn(path, body)

    def test_room_without_members(self):
        room = make_room(members=[], critic=None, context_files=[])
        body = contract.build_request_body(room, "текст", "r-slug", self.cfg)
        self.assertIn("team-director — директор комнаты", body)
        self.assertIn(contract.CONTRACT_OPEN, body)
        # путь STATE.md всё равно подставлен
        self.assertIn(str(Path(self.cfg.vault_dir) / "cafe" / "STATE.md"), body)


class CriticBodyTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="tr-contract-")
        self.cfg = FakeCfg(Path(self.tmp.name))
        self.room = make_room()
        self.answer = "## Ответ\n\nМаржа 46%, точка безубыточности 71 набор."

    def tearDown(self):
        self.tmp.cleanup()

    def test_critic_body_contains_request_answer_and_verdict_contract(self):
        body = contract.build_critic_body(self.room, REQUEST, self.answer, self.cfg)
        self.assertIn(REQUEST, body)
        self.assertIn(self.answer, body)
        self.assertIn(contract.CRITIC_OPEN, body)
        self.assertIn(contract.CRITIC_CLOSE, body)
        self.assertIn('"verdict":"approve|revise"', body)
        self.assertIn('"issues"', body)
        for key in ("where", "problem", "fix"):
            self.assertIn(key, body)
        self.assertIn("по-русски", body)
        self.assertIn("в подзадачи", body)
        self.assertIn("team-critic", body)
        self.assertIn("team-director", body)
        # у критика нет контракта ответа корня — он не должен его копировать
        self.assertNotIn(contract.CONTRACT_OPEN, body)

    def test_critic_body_keeps_cyrillic_quotes(self):
        body = contract.build_critic_body(self.room, "Вопрос «в кавычках» — и тире", "Ответ «так»", self.cfg)
        self.assertIn("Вопрос «в кавычках» — и тире", body)
        self.assertIn("Ответ «так»", body)


class RevisionBodyTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="tr-contract-")
        self.cfg = FakeCfg(Path(self.tmp.name))
        self.room = make_room()
        self.prev = "## Ответ\n\nМаржа 49%."
        self.issues = [
            {"where": "Срез 02-CFO", "problem": "себестоимость без роста какао",
             "fix": "пересчитать суфле с ценой какао от 20.10"},
            "В шаге 2 нет срока",
        ]

    def tearDown(self):
        self.tmp.cleanup()

    def test_revision_body_has_prev_answer_issues_and_contract(self):
        body = contract.build_revision_body(self.room, REQUEST, self.prev, self.issues,
                                            "Сделай короче, без таблиц", self.cfg)
        self.assertIn(REQUEST, body)
        self.assertIn(self.prev, body)
        self.assertIn("ЗАМЕЧАНИЯ КРИТИКА", body)
        self.assertIn("Где: Срез 02-CFO", body)
        self.assertIn("Проблема: себестоимость без роста какао", body)
        self.assertIn("Исправить: пересчитать суфле с ценой какао от 20.10", body)
        self.assertIn("В шаге 2 нет срока", body)
        self.assertIn("ЗАМЕЧАНИЕ МИХАИЛА", body)
        self.assertIn("Сделай короче, без таблиц", body)
        self.assertIn("НОВЫЙ цельный ответ", body)
        self.assertIn(contract.CONTRACT_OPEN, body)
        self.assertIn("ОДИН связный текст", body)
        self.assertIn("по-русски", body)

    def test_revision_body_without_issues(self):
        body = contract.build_revision_body(self.room, "запрос", "прошлый ответ", None, None, self.cfg)
        self.assertNotIn("ЗАМЕЧАНИЯ КРИТИКА", body)
        self.assertNotIn("ЗАМЕЧАНИЕ МИХАИЛА", body)
        self.assertIn("Замечаний нет", body)
        self.assertIn(contract.CONTRACT_OPEN, body)

    def test_revision_requires_cfg(self):
        with self.assertRaises(ValueError):
            contract.build_revision_body(self.room, "запрос", "ответ", None, None, None)

    def test_fingerprint_is_stable_and_sensitive(self):
        a = contract.body_fingerprint("тело карточки")
        self.assertEqual(a, contract.body_fingerprint("тело карточки"))
        self.assertNotEqual(a, contract.body_fingerprint("тело карточки "))
        self.assertEqual(len(a), 10)


class QuestionsContractTest(unittest.TestCase):
    """W30-A §2: блок «Как задавать вопросы Михаилу» и «не спрашивать об этом»."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="tr-contract-w30a-")
        self.cfg = FakeCfg(Path(self.tmp.name))
        self.cfg.state_dir = Path(self.tmp.name) / "data" / "state"
        self.room = make_room()

    def tearDown(self):
        self.tmp.cleanup()

    def test_card_has_the_question_form(self):
        body = contract.build_request_body(self.room, "текст", "r-slug", self.cfg)
        self.assertIn('"why"', body, "форма вопроса в карточке: «зачем»")
        self.assertIn('"context"', body, "форма вопроса в карточке: «откуда взялось»")
        self.assertIn('"consequence"', body, "варианты с последствиями")
        self.assertIn('"blocking"', body, "признак «блокирует работу»")

    def test_full_contract_teaches_the_language(self):
        text = contract.full_contract_text(self.room, "request", self.cfg)
        self.assertIn("Как задавать вопросы Михаилу", text)
        self.assertIn("не больше 3", text)
        self.assertIn("Хорошо:", text)
        self.assertIn("Плохо:", text)
        # примеры нейтральные: химчистка/грузоперевозки, без выдуманных кодов в «хорошем»
        self.assertIn("стирк", text.lower())
        self.assertIn("D19", text, "плохой пример должен показывать именно код без расшифровки")

    def test_recon_stage_rule_is_in_the_contract(self):
        text = contract.full_contract_text(self.room, "request", self.cfg)
        self.assertIn("Разведка → Обзор", text)
        self.assertIn("ОДНОГО блокирующего", text)

    def test_closed_topics_line_appears_and_disappears(self):
        import server.questions as q_mod
        self.assertEqual(contract.closed_topics_rule(self.room, self.cfg), "")
        q_mod.close_topic(self.cfg, "cafe", {"id": "q_1",
                                             "text": "Ставим цену 210 за килограмм или 240?"})
        line = contract.closed_topics_rule(self.room, self.cfg)
        self.assertIn("не актуально", line)
        self.assertIn("килограмм", line)
        self.assertLessEqual(len(line), 600)
        q_mod.reopen_topic(self.cfg, "cafe", {"id": "q_1",
                                              "text": "Ставим цену 210 за килограмм или 240?"})
        self.assertEqual(contract.closed_topics_rule(self.room, self.cfg), "")


if __name__ == "__main__":
    unittest.main(verbosity=2)
