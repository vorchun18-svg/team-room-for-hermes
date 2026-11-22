# -*- coding: utf-8 -*-
"""W29B-Q4: «Второе мнение» на другой модели и версии свода с различиями.

Что проверяется
---------------
1. **Разбор ответа проверяющего** (`parse_check`): «СОГЛАСНА / НЕ СОГЛАСНА /
   НЕ ХВАТАЕТ» с цитатой и пояснением → пункты и счётчики; ответ не по формату
   не выбрасывается (`source="raw"`), пустой ответ — `empty`.
2. **Модель по умолчанию** — не та, что писала свод (правило пакета), даже если
   каталог начинается с неё.
3. **Карточка-проверка ставится СРАЗУ с выбранной моделью**: тот же путь
   (`engine_write.create_child_card`), но настройка комнаты для исполнителя
   подменена парой человека, а выбор записан в состояние комнаты
   (`model_guard.set_user_model`) — иначе сторож моделей вернул бы модель комнаты.
4. **Отказы — с причиной и кодом:** свода нет, модели нет в каталоге, проверка уже
   идёт, запрос в архиве.
5. **Живой `GET /api/requests/{id}/second-opinion`** в песочнице: настоящий
   обработчик на настоящей (песочной) доске отдаёт выбор, оценку и уже заданные
   проверки. Профили Hermes и реальная доска не трогаются.

    python -m unittest tests.test_second_opinion -v
"""
from __future__ import annotations

import sys
import types
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(ROOT / "tests") not in sys.path:
    sys.path.insert(0, str(ROOT / "tests"))

import server.second_opinion as so                      # noqa: E402
from server.app_common import ACTION_ALLOWED            # noqa: E402

CATALOG = {
    "providers": [
        {"id": "oblako-a", "label": "Облако-А", "configured": True,
         "models": [{"id": "d-mini", "label": "Мини"}, {"id": "d-pro", "label": "Про"}]},
        {"id": "lokal", "label": "Локальный", "configured": True,
         "models": [{"id": "home", "label": "Домашний"}]},
    ],
    "warnings": [],
}

SVOD = (
    "## Итог\n"
    "Клиентке химчистки вернём стоимость за 3 дня, вещь заберём в понедельник.\n\n"
    "## Сроки\n"
    "Курьер работает по будням.\n\n"
    "## Риски\n"
    "Санкций нет."
)

DETAIL = {
    "id": "t_aaaabbbb",
    "archived": False,
    "synthesis": {
        "state": "ready", "version": 2, "text_md": SVOD,
        "history": [
            {"version": 1, "kind": "initial", "author": "lead-profile", "task_id": "t_11112222",
             "at": 1000, "text_md": SVOD.replace("3 дня", "2 дня")},
            {"version": 2, "kind": "revision", "author": "lead-profile", "task_id": "t_33334444",
             "at": 2000},
        ],
    },
    "tree": [
        {"id": "t_aaaabbbb", "title": "Претензия по химчистке", "assignee": "lead-profile",
         "status": "done", "created_at": 900, "summary_md": SVOD},
        {"id": "t_33334444", "title": "Обновление свода", "assignee": "lead-profile",
         "status": "done", "created_at": 1900, "summary_md": SVOD},
        {"id": "t_55556666", "title": "Второе мнение · Про", "assignee": "critic-profile",
         "status": "done", "created_at": 2100, "finished_at": 2400,
         "model_label": "Облако-А · d-pro",
         "summary_md": "- НЕ СОГЛАСНА: «вернём стоимость за 3 дня» — срок в договоре 5 дней\n"
                       "- НЕ ХВАТАЕТ: «Риски» — не сказано, что делать при отказе"},
    ],
}

ROOM = {
    "id": "cafe", "board": "board-cafe", "orchestrator": "lead-profile",
    "critic": "critic-profile",
    "agent_models": {"lead-profile": {"provider": "oblako-a", "model": "d-pro"}},
}

REC = {"root_id": "t_aaaabbbb", "text": "Ответить клиентке химчистки по претензии"}


class FakeEngine:
    """Подставной движок: запоминает, с чем позвали `create_child_card`."""

    def __init__(self) -> None:
        self.calls: list = []

    def create_child_card(self, cfg, room, board, parent_id, assignee, title, body, kind,
                          idem_key=None):
        self.calls.append({"room": room, "board": board, "parent": parent_id,
                           "assignee": assignee, "title": title, "body": body,
                           "kind": kind, "key": idem_key})
        return "t_99998888"


class FakeGuard:
    def __init__(self) -> None:
        self.chosen: list = []

    def set_user_model(self, cfg, room_id, card_id, provider="", model=""):
        self.chosen.append({"room": room_id, "card": card_id,
                            "provider": provider, "model": model})
        return {"provider": provider, "model": model}


def _cfg():
    """Конфиг для чистой логики: каталог подменён, файлов не читаем."""
    return types.SimpleNamespace(state_dir=None, model_catalog_override=CATALOG,
                                 hermes_home="fake-home", app_dir="fake-app", sandbox=True)


class SecondOpinionUnit(unittest.TestCase):
    def setUp(self) -> None:
        so.reset_modules()
        self.engine = FakeEngine()
        self.guard = FakeGuard()
        so._ENGINE = self.engine
        so._GUARD = self.guard
        self.cfg = _cfg()

    def tearDown(self) -> None:
        so.reset_modules()

    # --- разбор ответа ----------------------------------------------------
    def test_parse_verdicts_with_quotes_and_counts(self) -> None:
        parsed = so.parse_check(
            "- НЕ СОГЛАСНА: «срок 3 дня» — в договоре 5 дней\n"
            "* не хватает: «риски» — нет плана Б\n"
            "СОГЛАСНА: «курьер по будням» — верно\n"
            "просто текст без маркера — не пункт\n")
        self.assertEqual(parsed["counts"], {"agree": 1, "disagree": 1, "missing": 1})
        self.assertEqual(len(parsed["items"]), 3)
        first = parsed["items"][0]
        self.assertEqual(first["verdict"], "disagree")
        self.assertEqual(first["label"], "не согласна")
        self.assertEqual(first["quote"], "срок 3 дня")
        self.assertIn("5 дней", first["note"])
        self.assertEqual(parsed["source"], "card")

    def test_parse_keeps_off_format_answer(self) -> None:
        parsed = so.parse_check("Свод в целом верный, но срок стоит уточнить.")
        self.assertEqual(parsed["items"], [])
        self.assertEqual(parsed["source"], "raw")
        self.assertEqual(so.parse_check("")["source"], "empty")

    def test_counts_text_is_one_line(self) -> None:
        self.assertEqual(so.counts_text({"disagree": 2, "missing": 1, "agree": 0}),
                         "2 не согласна · 1 не хватает")
        self.assertEqual(so.counts_text({}), "")

    # --- выбор модели ------------------------------------------------------
    def test_choices_and_default_skip_author_model(self) -> None:
        picked = so.choices(self.cfg, ROOM, DETAIL)
        self.assertEqual(len(picked["items"]), 3)
        self.assertEqual(picked["author"]["model"], "d-pro")
        default = so.default_choice(picked["items"], picked["author"])
        self.assertNotEqual(default["model"], "d-pro",
                            "по умолчанию должна быть ДРУГАЯ модель")
        self.assertEqual(default["model"], "d-mini")
        self.assertEqual(so.split_value(so.value_of("lokal", "home")), ("lokal", "home"))

    def test_estimate_is_tokens_not_invented_dollars(self) -> None:
        est = so.estimate(self.cfg, DETAIL, SVOD)
        self.assertGreater(est["input_tokens"], len(SVOD) // 4)
        self.assertGreater(est["output_tokens"], 0)
        self.assertIn("токенов", est["text"])
        self.assertNotIn("$", est["text"])

    def test_body_has_request_svod_and_strict_format(self) -> None:
        body = so.build_body(ROOM, REC["text"], SVOD, 2)
        self.assertIn(REC["text"], body)
        self.assertIn("вернём стоимость за 3 дня", body)
        for marker in ("СОГЛАСНА", "НЕ СОГЛАСНА", "НЕ ХВАТАЕТ", "версии 2"):
            self.assertIn(marker, body)

    # --- карточка ---------------------------------------------------------
    def test_create_check_uses_chosen_model_from_the_start(self) -> None:
        res = so.create_check(self.cfg, ROOM, REC, DETAIL, provider="lokal", model="home")
        self.assertEqual(res["card_id"], "t_99998888")
        self.assertTrue(res["title"].startswith(so.TITLE_PREFIX))
        call = self.engine.calls[0]
        self.assertEqual(call["assignee"], "critic-profile")     # проверяет критик комнаты
        self.assertEqual(call["parent"], "t_aaaabbbb")           # ребёнок корня запроса
        self.assertEqual(call["kind"], so.KIND)
        self.assertEqual(call["room"]["agent_models"]["critic-profile"],
                         {"provider": "lokal", "model": "home"},
                         "карточка должна рождаться сразу с выбранной моделью")
        # модель комнаты для руководителя не изменилась
        self.assertEqual(call["room"]["agent_models"]["lead-profile"],
                         {"provider": "oblako-a", "model": "d-pro"})
        self.assertIn("t_aaaabbbb", call["key"])
        self.assertIn("home", call["key"])
        # выбор человека записан — сторож моделей его уважает
        self.assertEqual(self.guard.chosen[0]["model"], "home")
        self.assertEqual(self.guard.chosen[0]["card"], "t_99998888")

    def test_create_check_same_model_is_flagged_not_blocked(self) -> None:
        res = so.create_check(self.cfg, ROOM, REC, DETAIL, provider="oblako-a", model="d-pro")
        self.assertTrue(res["same_model"])

    def test_create_check_refusals_name_the_reason(self) -> None:
        no_svod = dict(DETAIL)
        no_svod["synthesis"] = {"state": "none", "history": []}
        with self.assertRaises(so.SecondOpinionError) as ctx:
            so.create_check(self.cfg, ROOM, REC, no_svod, provider="lokal", model="home")
        self.assertEqual(ctx.exception.code, "conflict")
        self.assertIn("свода", ctx.exception.message.lower())

        with self.assertRaises(so.SecondOpinionError) as ctx:
            so.create_check(self.cfg, ROOM, REC, DETAIL, provider="lokal", model="нет-такой")
        self.assertEqual(ctx.exception.code, "bad_request")
        self.assertIn("каталог", ctx.exception.message.lower())

        archived = dict(DETAIL)
        archived["archived"] = True
        with self.assertRaises(so.SecondOpinionError) as ctx:
            so.create_check(self.cfg, ROOM, REC, archived, provider="lokal", model="home")
        self.assertEqual(ctx.exception.code, "conflict")
        self.assertEqual(ctx.exception.detail.get("reason"), "archived")

        running = dict(DETAIL)
        running["tree"] = list(DETAIL["tree"]) + [
            {"id": "t_77778888", "title": "Второе мнение · Мини", "status": "running",
             "created_at": 3000}]
        with self.assertRaises(so.SecondOpinionError) as ctx:
            so.create_check(self.cfg, ROOM, REC, running, provider="lokal", model="home")
        self.assertEqual(ctx.exception.code, "conflict")
        self.assertEqual(self.engine.calls, [], "вторая карточка не должна создаваться")

    # --- что уже спрашивали ------------------------------------------------
    def test_checks_are_parsed_from_the_tree(self) -> None:
        found = so.checks(DETAIL)
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]["counts"], {"agree": 0, "disagree": 1, "missing": 1})
        self.assertEqual(found[0]["counts_text"], "1 не согласна · 1 не хватает")
        self.assertFalse(found[0]["running"])
        self.assertIn("d-pro", found[0]["model"])

    def test_view_gives_choice_estimate_and_state(self) -> None:
        view = so.view(self.cfg, ROOM, DETAIL)
        self.assertTrue(view["can_run"])
        self.assertEqual(view["default"]["model"], "d-mini")
        self.assertEqual(view["version"], 2)
        self.assertEqual(len(view["checks"]), 1)
        self.assertIn("токенов", view["estimate"]["text"])

    def test_contract_action_is_accepted_by_server(self) -> None:
        # интерфейс шлёт `action: "second_opinion"` — сервер обязан его знать
        self.assertIn("second_opinion", ACTION_ALLOWED)
        patterns = [(method, pattern) for method, pattern, _handler in so.ROUTES]
        self.assertEqual(len(patterns), len(set(patterns)), "дубль маршрута")


class SecondOpinionLive(unittest.TestCase):
    """Живой обработчик на песочной доске: настоящие чтения, никаких записей."""

    @classmethod
    def setUpClass(cls) -> None:
        from helpers import Sandbox          # tests/helpers.py: путь добавлен выше
        import server.app as app_mod

        cls.sb = Sandbox().__enter__()
        cls.cfg = cls.sb.cfg
        try:
            setattr(cls.cfg, "model_catalog_override", CATALOG)
        except Exception:                       # noqa: BLE001 - конфиг неизменяемый
            pass
        cls.app = app_mod.App(cls.cfg, port=8868)
        # Живой путь без HTTP-сервера: настоящая песочная доска + настоящий
        # `_detail` приложения (чтения движка), затем обработчик. Реестр комнаты
        # запросов в песочнице не наполняет, поэтому `_find_record` здесь не
        # используется — он проверяется другими тестами (test_contract_actions).
        cls.rid = cls.sb.ids["done_modern"]["root_id"]
        # Комната — минимальная запись: `_detail` нужны id и доска. Реестр комнат
        # в песочнице не наполняется, а проверяем мы не его, а чтения движка.
        cls.room = {"id": "cafe", "board": "team", "orchestrator": "", "members": []}

    @classmethod
    def tearDownClass(cls) -> None:
        try:
            cls.sb.__exit__(None, None, None)
        except Exception:                       # noqa: BLE001 - уборка вторична
            pass

    def test_get_returns_choice_estimate_and_checks(self) -> None:
        so.reset_modules()
        detail = self.app._detail(self.room, self.rid)
        data = so.view(self.cfg, self.room, detail)
        self.assertIn("models", data)
        self.assertIn("estimate", data)
        self.assertIsInstance(data["checks"], list)
        self.assertIn("can_run", data)
        # на песочной доске свод есть — проверять можно; модель автора известна или пуста
        self.assertIsInstance(data.get("author"), dict)
        self.assertTrue(data.get("estimate", {}).get("text"))


if __name__ == "__main__":                              # pragma: no cover
    unittest.main()
