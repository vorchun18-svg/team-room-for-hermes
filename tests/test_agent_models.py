# -*- coding: utf-8 -*-
"""W10-MODEL-BE: модель, провайдер и уровень мышления участника комнаты (WAVE10).

Что проверяется:
  * каталог ``GET /api/models`` — только провайдеры с ключом, уровни мышления, без секретов;
  * ``PUT /api/rooms/{id}/agents/{profile}/model``: ненастроенный провайдер → 400,
    чужой профиль → 400, модель не из каталога → 400, effort вне списка → 400,
    «вернуть по умолчанию» — запись стирается, действует настройка агента;
  * хранение в реестре комнаты (``agent_models``), ``members[].model_settings/agent_default``,
    запись в журнал комнаты «модель изменена»;
  * новая карточка запроса получает ``--model/--provider`` при создании, уровень мышления —
    через мост ``tools/kb_bridge.py`` (штатный ``kanban_db.set_reasoning_effort``);
  * сторож ``apply_to_tree``: дочерние карточки декомпозитора (triage/todo/ready/blocked)
    закрепляются на модели комнаты идемпотентно, ``running``/``done`` не трогаются;
  * ``POST …/restart`` снимает с работы только running-карточки участника;
  * под-запрос направления проектного режима получает модель своего владельца;
  * **точность выбора** (FIX-MODEL-BE): перебор всех моделей всех провайдеров тестового
    каталога (id-близнецы с общим префиксом, ':' и '/' в id, регистровые двойники) —
    что выбрано, то и сохранено, и то ушло в `--model/--provider` и в карточку; регистр,
    префикс и подстрока не угадываются; неполная пара в реестре не смешивается с дефолтом
    агента; `restart` закрепляет выбор до reclaim;
  * дефолты агента — на копиях РЕАЛЬНЫХ конфигов профилей (сверка с `yaml.safe_load`)
    и на синтетических граблях (любой отступ, нет `model.default`, `delegation` ≠ agent);
  * файлы профилей не тронуты (хэши config.yaml/SOUL.md до/после), реальная доска тоже;
  * в ответах и каталоге нет ключей/секретов.

Правила волны 7/10: только песочница ``tests.helpers.Sandbox``; CLI ``hermes kanban``
подменён shim'ом (карточка создаётся в песочной БД), мост к ``hermes_cli`` работает
по-настоящему, но с ``--db`` песочной доски.

    python -m unittest tests.test_agent_models -v

# SHIM-OK: CLI Hermes подменён CliShim; dispatch/decompose не выполняются никогда.
"""
from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
import subprocess as real_subprocess
import sys
import tempfile
import time
import types
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import server.agent_models as models_mod          # noqa: E402
import server.app as app_mod                      # noqa: E402
import server.engine_cli as engine_cli            # noqa: E402
import server.engine_write as ew                  # noqa: E402
import server.rooms as rooms_mod                  # noqa: E402
from server.http_util import ApiError             # noqa: E402
from tests.helpers import Sandbox, SimAgents      # noqa: E402
from tests import ci_marks                       # noqa: E402

ROOM = "cafe"
PLAN_OWNER = "biz-ops"

#: каталог для теста: как отдаёт Hermes (только настроенные) + один «без ключа» для проверки
CATALOG = {
    "providers": [
        {"id": "llamacpp", "label": "LlamaCPP", "configured": True,
         "models": [{"id": "qwen36"}, {"id": "gemma4"}, {"id": "ornith"}]},
        {"id": "ollama-cloud", "label": "Ollama Cloud", "configured": True,
         "models": [{"id": "glm-5.3-flash"}, {"id": "glm-5.3"}, {"id": "gpt-oss:20b"}]},
        {"id": "anthropic", "label": "Anthropic", "configured": True,
         "models": [{"id": "claude-sonnet-5-5"}]},
        {"id": "openrouter", "label": "OpenRouter", "configured": False,
         "models": [{"id": "some/model"}]},
    ]
}
NEW_MODEL = ("llamacpp", "qwen36", "high")


def file_hashes() -> dict:
    """Хэши конфигов и SOUL.md РЕАЛЬНЫХ профилей: их нельзя менять никогда."""
    home = ci_marks.hermes_home()          # Windows / macOS / Linux — без хардкода пути
    paths = [home / "config.yaml", home / "SOUL.md"]
    profiles = home / "profiles"
    if profiles.is_dir():
        for folder in sorted(profiles.iterdir()):
            if not folder.is_dir():
                continue
            for name in ("config.yaml", "SOUL.md", "profile.yaml"):
                paths.append(folder / name)
    out = {}
    for path in paths:
        try:
            out[str(path)] = hashlib.sha256(path.read_bytes()).hexdigest()
        except OSError:
            continue
    return out


class CliShim:
    """CLI `hermes kanban` без агентов: `create` пишет карточку в песочную БД.

    Мост к `hermes_cli` (``agent_models``) идёт мимо этого shim'а — настоящим
    subprocess'ом и с `--db` песочной доски, поэтому состояние карточек проверяется
    по-настоящему: как будто сработал движок.
    """

    def __init__(self, sa: SimAgents) -> None:
        self.calls: list[list[str]] = []
        self.sa = sa
        self._real = real_subprocess

    def __getattr__(self, name):
        return getattr(self._real, name)

    @staticmethod
    def _flag(argv, name: str) -> str:
        for token in argv:
            if token.startswith("--%s=" % name):
                return token.split("=", 1)[1]
        return ""

    def run(self, argv, **kwargs):                       # noqa: D401 - duck-typing
        argv = [str(a) for a in argv]
        self.calls.append(argv)
        verb = ""
        try:
            tail = argv[argv.index("kanban") + 1:]
        except ValueError:
            tail = argv
        skip = False
        for token in tail:
            if skip:
                skip = False
                continue
            if token == "--board":
                skip = True
                continue
            if token.startswith("-"):
                continue
            verb = token
            break
        if verb == "reassign":
            task_id = next((tok for tok in tail[1:] if tok.startswith("t_")), "")
            if task_id:
                con = self.sa.connect()
                try:
                    con.execute("UPDATE tasks SET status = 'ready', current_run_id = NULL "
                                "WHERE id = ?", (task_id,))
                    con.commit()
                finally:
                    con.close()
            return real_subprocess.CompletedProcess(argv, 0, "", "")
        if verb == "create":
            title = tail[1] if len(tail) > 1 and not tail[1].startswith("-") else "Карточка"
            assignee = self._flag(argv, "assignee")
            status = "triage" if "--triage" in argv else "todo"
            body = ""
            body_file = self._flag(argv, "body-file")
            if body_file:                      # тело карточки приходит файлом (UTF-8)
                try:
                    body = Path(body_file).read_text(encoding="utf-8")
                except OSError:
                    body = ""
            card_id = self.sa.card(title=title[:120], assignee=assignee, status=status,
                                   body=body, created_by="Комната команды")
            model, provider = self._flag(argv, "model"), self._flag(argv, "provider")
            if model:                      # движок ставит переопределение из флагов `create`
                con = self.sa.connect()
                try:
                    con.execute("UPDATE tasks SET model_override = ?, provider_override = ? "
                                "WHERE id = ?", (model, provider or None, card_id))
                    con.commit()
                finally:
                    con.close()
            return real_subprocess.CompletedProcess(
                argv, 0, json.dumps({"id": card_id, "status": status}), "")
        return real_subprocess.CompletedProcess(argv, 0, "", "")


def _cfg_hash_names(hashes: dict) -> int:
    return sum(1 for key in hashes if key.endswith(("config.yaml", "SOUL.md")))


class _Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.sb = Sandbox().__enter__()
        cls.cfg = cls.sb.cfg
        cls.cfg.model_catalog_override = CATALOG       # тестовый каталог: без Hermes и сети
        cls.real_before = cls.sb.snapshot_real()
        cls.files_before = file_hashes()
        cls.sa = SimAgents(cls.sb)
        cls.shim = CliShim(cls.sa)
        cls._real_sub = engine_cli.subprocess
        engine_cli.subprocess = cls.shim
        ew.subprocess = cls.shim
        cls.app = app_mod.App(cls.cfg, port=0)
        cls.app.call("rooms", "seed_defaults", ((cls.cfg,), {}))
        cls._counter = 0

    @classmethod
    def tearDownClass(cls):
        engine_cli.subprocess = cls._real_sub
        try:
            del ew.subprocess
        except AttributeError:
            pass
        try:
            cls.sb.assert_real_untouched(cls.real_before)
            after = file_hashes()
            assert after == cls.files_before, "файлы профилей (config.yaml/SOUL.md) изменились!"
        finally:
            try:
                del cls.cfg.model_catalog_override
            except AttributeError:
                pass
            cls.sb.__exit__(None, None, None)

    def setUp(self):
        self.shim.calls.clear()
        self.app.call("rooms", "update_room", ((self.cfg, ROOM, {"agent_models": {}}), {}))

    # --- помощники ---------------------------------------------------------
    def put(self, profile: str, payload) -> tuple:
        path = "/api/rooms/%s/agents/%s/model" % (ROOM, profile)
        try:
            return self.app.dispatch("PUT", path, {}, payload)
        except ApiError as exc:
            return exc.status, exc.payload(), {}

    def restart(self, profile: str) -> tuple:
        path = "/api/rooms/%s/agents/%s/restart" % (ROOM, profile)
        try:
            return self.app.dispatch("POST", path, {}, {})
        except ApiError as exc:
            return exc.status, exc.payload(), {}

    def set_model(self, profile: str, provider="llamacpp", model="qwen36", reasoning="high"):
        status, body, _h = self.put(profile, {"provider": provider, "model": model,
                                              "reasoning": reasoning})
        self.assertEqual(status, 200, body)
        return body

    def room(self) -> dict:
        return self.app.call("rooms", "get_room", ((self.cfg, ROOM), {}))

    def room_of_api(self) -> dict:
        status, body, _h = self.app.dispatch("GET", "/api/rooms/%s" % ROOM, {}, {})
        self.assertEqual(status, 200)
        return body

    def card(self, task_id: str) -> dict:
        row = self.sb.one("SELECT id, assignee, status, model_override, provider_override, "
                          "reasoning_effort FROM tasks WHERE id = ?", (task_id,))
        return dict(row) if row else {}

    def set_status(self, task_id: str, status: str) -> None:
        con = self.sa.connect()
        try:
            con.execute("UPDATE tasks SET status = ? WHERE id = ?", (status, task_id))
            con.commit()
        finally:
            con.close()

    def new_root(self, title: str) -> str:
        """Корневая карточка запроса комнаты, уже зарегистрированная в реестре."""
        self._counter += 1
        root_id = self.sa.card(title="%s %d" % (title, self._counter), assignee="",
                               status="triage", tenant="room:cafe")
        self.app.call("registry", "add_request",
                      ((self.cfg, ROOM, {"root_id": root_id, "slug": "s%d" % self._counter,
                                         "title": title, "created_at": int(time.time())}), {}))
        return root_id


# ---------------------------------------------------------------- каталог
class CatalogTests(_Base):
    def test_catalog_only_configured(self):
        status, body, _h = self.app.dispatch("GET", "/api/models", {}, {})
        self.assertEqual(status, 200, body)
        ids = [p["id"] for p in body["providers"]]
        self.assertIn("llamacpp", ids)
        self.assertNotIn("openrouter", ids, "провайдер без ключа показываться не должен")
        for provider in body["providers"]:
            self.assertTrue(provider["configured"])
            self.assertEqual(sorted(provider), ["configured", "id", "label", "models"])
            for model in provider["models"]:
                self.assertEqual(sorted(model), ["id", "label"])
        self.assertEqual([e["id"] for e in body["efforts"]], list(models_mod.EFFORTS))
        self.assertEqual(body["source"], "override")
        self.assertEqual(body["warnings"], [])

    def test_catalog_has_no_secrets(self):
        _status, body, _h = self.app.dispatch("GET", "/api/models", {}, {})
        text = json.dumps(body, ensure_ascii=False)
        for marker in ("sk-", "ghp_", "Bearer ", "api_key", "password", "token"):
            self.assertNotIn(marker, text, "в каталоге не должно быть секретов: %s" % marker)

    def test_catalog_is_cached(self):
        first = models_mod.catalog(self.cfg)
        second = models_mod.catalog(self.cfg)
        self.assertTrue(second["cached"])
        self.assertEqual([p["id"] for p in first["providers"]],
                         [p["id"] for p in second["providers"]])


# ---------------------------------------------------------------- проверка
class ValidateTests(_Base):
    def test_unconfigured_provider_400(self):
        status, body, _h = self.put("biz-cfo", {"provider": "openrouter", "model": "some/model"})
        self.assertEqual(status, 400, body)
        self.assertIn("не настроен", json.dumps(body, ensure_ascii=False))

    def test_model_not_in_catalog_400(self):
        status, body, _h = self.put("biz-cfo", {"provider": "llamacpp", "model": "не-модель"})
        self.assertEqual(status, 400, body)
        self.assertIn("нет модели", json.dumps(body, ensure_ascii=False))

    def test_model_without_provider_400(self):
        status, body, _h = self.put("biz-cfo", {"model": "qwen36"})
        self.assertEqual(status, 400, body)
        self.assertIn("провайдера", json.dumps(body, ensure_ascii=False))

    def test_provider_without_model_400(self):
        status, body, _h = self.put("biz-cfo", {"provider": "llamacpp"})
        self.assertEqual(status, 400, body)

    def test_effort_outside_list_400(self):
        status, body, _h = self.put("biz-cfo", {"provider": "llamacpp", "model": "qwen36",
                                                "reasoning": "hgih"})
        self.assertEqual(status, 400, body)
        self.assertIn("Уровень мышления", json.dumps(body, ensure_ascii=False))
        self.assertIn("allowed", json.dumps(body, ensure_ascii=False))

    def test_foreign_profile_400(self):
        status, body, _h = self.put("coder", {"provider": "llamacpp", "model": "qwen36"})
        self.assertEqual(status, 400, body)
        self.assertIn("составе", json.dumps(body, ensure_ascii=False))

    def test_unknown_field_400(self):
        status, body, _h = self.put("biz-cfo", {"provider": "llamacpp", "model": "qwen36",
                                                "temperature": 0.2})
        self.assertEqual(status, 400, body)

    def test_reasoning_only_is_allowed(self):
        """Уровень мышления независим от модели: можно поменять только его."""
        status, body, _h = self.put("biz-cfo", {"reasoning": "none"})
        self.assertEqual(status, 200, body)
        self.assertEqual(self.room()["agent_models"]["biz-cfo"]["reasoning"], "none")
        self.assertEqual(self.room()["agent_models"]["biz-cfo"]["provider"], "")


# ---------------------------------------------------------------- хранение и API комнаты
class RoomApiTests(_Base):
    def test_put_roundtrip_and_member_settings(self):
        body = self.set_model("biz-cfo")
        self.assertEqual(body["member"]["profile"], "biz-cfo")
        self.assertEqual(body["member"]["model_settings"]["provider"], "llamacpp")
        self.assertEqual(body["member"]["model_settings"]["source"], "room")
        default = body["member"]["agent_default"]
        self.assertTrue(default["provider"], "дефолт агента должен читаться из конфига профиля")
        self.assertIn("biz-cfo", body["agent_models"])

        detail = self.room_of_api()
        self.assertEqual(detail["agent_models"]["biz-cfo"]["model"], "qwen36")
        member = next(m for m in detail["members"] if m["profile"] == "biz-cfo")
        self.assertEqual(member["model_settings"]["reasoning"], "high")
        self.assertEqual(member["model_settings"]["source"], "room")
        self.assertEqual(member["agent_default"],
                         models_mod.agent_default(self.cfg, "biz-cfo"))
        self.assertNotEqual(member["agent_default"]["model"], "qwen36",
                            "дефолт агента — из конфига профиля, а не замена комнаты")
        self.assertIn("biz-director", detail["agent_models_meta"], "руководитель тоже в карте")
        self.assertEqual(detail["agent_models_meta"]["biz-director"]["model_settings"]["source"],
                         "agent")

    def test_reset_to_default(self):
        root_id = self.new_root("Сброс модели")
        kids = self.sa.decompose(root_id, [{"title": "Смета", "assignee": "biz-cfo",
                                            "status": "todo"}])
        self.set_model("biz-cfo")
        self.assertEqual(self.card(kids[0])["model_override"], "qwen36")
        status, body, _h = self.put("biz-cfo", {})
        self.assertEqual(self.card(kids[0])["model_override"], None,
                         "после «Вернуть по умолчанию» пин с карточки снимается")
        self.assertEqual(self.card(kids[0])["reasoning_effort"], None)
        self.assertEqual(status, 200, body)
        self.assertTrue(body["cleared"])
        self.assertEqual(self.room()["agent_models"], {})
        detail = self.room_of_api()
        member = next(m for m in detail["members"] if m["profile"] == "biz-cfo")
        self.assertEqual(member["model_settings"]["source"], "agent")
        self.assertEqual(member["model_settings"]["provider"], member["agent_default"]["provider"])

    def test_model_null_clears(self):
        self.set_model("biz-cfo")
        status, body, _h = self.put("biz-cfo", {"provider": None, "model": None,
                                                "reasoning": None})
        self.assertEqual(status, 200, body)
        self.assertEqual(self.room()["agent_models"], {})

    def test_journal_has_model_changed(self):
        self.set_model("biz-cfo")
        path = Path(self.cfg.state_dir) / ROOM / "journal.jsonl"
        events = [json.loads(line) for line in path.read_text("utf-8").splitlines() if line.strip()]
        changed = [e for e in events if e.get("kind") == "model_changed"]
        self.assertTrue(changed, "в журнале комнаты должна быть запись «модель изменена»")
        self.assertIn("Модель участника", changed[-1]["text"])
        self.assertIn("qwen36", changed[-1]["text"])

    def test_patch_accepts_agent_models(self):
        try:
            status, body, _h = self.app.dispatch(
                "PATCH", "/api/rooms/%s" % ROOM, {},
                {"agent_models": {"biz-ops": {"provider": "llamacpp", "model": "gemma4",
                                              "reasoning": "low"}}})
        except ApiError as exc:
            self.fail("PATCH с моделями не должен падать: %s" % exc)
        self.assertEqual(status, 200, body)
        self.assertEqual(body["agent_models"]["biz-ops"]["model"], "gemma4")

        with self.assertRaises(ApiError) as ctx:
            self.app.dispatch("PATCH", "/api/rooms/%s" % ROOM, {},
                              {"agent_models": {"coder": {"provider": "llamacpp",
                                                          "model": "qwen36"}}})
        self.assertEqual(ctx.exception.status, 400)

    def test_create_room_with_models(self):
        payload = {"title": "Комната с моделями", "id": "models-room", "board": "team",
                   "orchestrator": "biz-director",
                   "members": [{"profile": "biz-lawyer", "role": "worker"}],
                   "agent_models": {"biz-lawyer": {"provider": "llamacpp", "model": "ornith",
                                                   "reasoning": "max"}}}
        status, body, _h = self.app.dispatch("POST", "/api/rooms", {}, payload)
        self.assertEqual(status, 201, body)
        self.assertEqual(body["agent_models"]["biz-lawyer"]["reasoning"], "max")
        self.assertEqual(body["members"][0]["model_settings"]["model"], "ornith")


# ---------------------------------------------------------------- карточки
class CardTests(_Base):
    def test_new_request_card_gets_model_and_effort(self):
        self.set_model("biz-director")
        status, body, _h = self.app.dispatch(
            "POST", "/api/rooms/%s/requests" % ROOM, {},
            {"text": "Посчитай экономику набора из шести моти и сроки запуска."})
        self.assertEqual(status, 201, body)
        root_id = body["root_id"]
        card = self.card(root_id)
        self.assertEqual(card["model_override"], "qwen36", "модель корня запроса — у руководителя")
        self.assertEqual(card["provider_override"], "llamacpp")
        self.assertEqual(card["reasoning_effort"], "high", "effort ставится мостом")
        created = [argv for argv in self.shim.calls if "create" in argv]
        self.assertTrue(any("--model=qwen36" in argv and "--provider=llamacpp" in argv
                            for argv in created), created[-1] if created else "")

    def test_guard_applies_to_children_idempotently(self):
        self.set_model("biz-cfo")
        root_id = self.new_root("Декомпозиция")
        kids = self.sa.decompose(root_id, [
            {"title": "Смета", "assignee": "biz-cfo", "status": "todo"},
            {"title": "Проверка", "assignee": "biz-ops", "status": "todo"},
        ])
        first = models_mod.apply_to_tree(self.cfg, ROOM, root_id)
        self.assertIn(kids[0], first["updated"])
        self.assertNotIn(kids[1], first["updated"], "у biz-ops замены нет — не трогаем")
        card = self.card(kids[0])
        self.assertEqual((card["model_override"], card["provider_override"],
                          card["reasoning_effort"]), ("qwen36", "llamacpp", "high"))

        second = models_mod.apply_to_tree(self.cfg, ROOM, root_id)
        self.assertEqual(second["updated"], [], "повторный проход ничего не меняет")
        self.assertGreaterEqual(second["skipped"], 1)

    def test_guard_skips_running_and_done(self):
        self.set_model("biz-cfo")
        root_id = self.new_root("Идущая работа")
        kids = self.sa.decompose(root_id, [
            {"title": "Идёт", "assignee": "biz-cfo", "status": "running"},
            {"title": "Готово", "assignee": "biz-cfo", "status": "done"},
        ])
        res = models_mod.apply_to_tree(self.cfg, ROOM, root_id)
        self.assertEqual(res["updated"], [])
        for kid in kids:
            self.assertEqual(self.card(kid)["model_override"], None, "running/done не трогаем")

    def test_watcher_calls_guard(self):
        """Сторож подключён в watcher: карточка ребёнка получает модель после тика."""
        self.set_model("biz-cfo")
        root_id = self.new_root("Тик наблюдателя")
        kids = self.sa.decompose(root_id, [{"title": "Смета", "assignee": "biz-cfo",
                                            "status": "todo"}])
        self.app.watcher.tick_once()
        self.assertEqual(self.card(kids[0])["model_override"], "qwen36")

    def test_put_counts_pending_running(self):
        self.set_model("biz-cfo")
        root_id = self.new_root("Считаем идущие")
        kids = self.sa.decompose(root_id, [{"title": "Идёт", "assignee": "biz-cfo",
                                            "status": "running"},
                                           {"title": "В очереди", "assignee": "biz-cfo",
                                            "status": "todo"}])
        status, body, _h = self.put("biz-cfo", {"provider": "ollama-cloud",
                                                "model": "glm-5.3", "reasoning": "medium"})
        self.assertEqual(status, 200, body)
        self.assertGreaterEqual(body["applied"]["pending_running"], 1,
                                "идущая карточка участника ждёт «Перезапустить»")
        self.assertGreaterEqual(body["applied"]["cards_updated"], 2)   # корень + «В очереди»
        self.assertEqual(self.card(kids[0])["model_override"], None, "running не трогаем")
        self.assertEqual(self.card(kids[1])["model_override"], "glm-5.3")

    def test_restart_running(self):
        self.set_model("biz-cfo")
        root_id = self.new_root("Перезапуск")
        kids = self.sa.decompose(root_id, [{"title": "Идёт", "assignee": "biz-cfo",
                                            "status": "running"}])
        status, body, _h = self.restart("biz-cfo")
        self.assertEqual(status, 200, body)
        self.assertGreaterEqual(body["restarted"], 1, body)
        self.assertIn(kids[0], body["cards"])
        self.assertEqual(self.card(kids[0])["status"], "ready", "после reclaim карточка ждёт запуска")
        again = self.restart("biz-cfo")
        self.assertEqual(again[1]["restarted"], 0, "повторный перезапуск никого не находит")
        self.assertTrue(any("reassign" in argv and "--reclaim" in argv for argv in self.shim.calls),
                        "перезапуск идёт штатным reassign --reclaim")

    def test_restart_unknown_profile_is_400(self):
        status, body, _h = self.restart("coder")
        self.assertEqual(status, 400, body)
        status, body, _h = self.restart("biz-marketing")
        self.assertEqual(status, 200, body)
        self.assertEqual(body["restarted"], 0, "у этого участника идущих карточек нет")

    def test_plan_stream_request_uses_owner_model(self):
        """Под-запрос направления получает модель своего владельца, а не руководителя."""
        self.set_model("biz-director", "anthropic", "claude-sonnet-5-5", "medium")
        self.set_model(PLAN_OWNER, "llamacpp", "gemma4", "low")
        room = self.room()
        stream = {"id": "s9", "title": "Направление с моделью владельца", "goal": "Проверить",
                  "owner": PLAN_OWNER, "members": [], "depends_on": [], "due": None,
                  "status": "proposed", "created_by": "manager"}
        plan = {"streams": [stream], "brief_root_id": self.new_root("Бриф")}
        res = self.app.call("plan", "_create_request", ((self.cfg, room, stream, plan), {}))
        root_id = (res or {}).get("root_id")
        self.assertTrue(root_id, res)
        card = self.card(root_id)
        self.assertEqual(card["model_override"], "gemma4")
        self.assertEqual(card["provider_override"], "llamacpp")
        self.assertEqual(card["reasoning_effort"], "low")

    def test_tree_has_model_label(self):
        self.set_model("biz-cfo")
        root_id = self.new_root("Подпись модели")
        kids = self.sa.decompose(root_id, [{"title": "Смета", "assignee": "biz-cfo",
                                            "status": "todo"}])
        models_mod.apply_to_tree(self.cfg, ROOM, root_id)
        detail = self.app.call("engine_read", "request_detail",
                               ((self.cfg, "team", root_id, self.room()), {}))
        node = next(n for n in detail["tree"] if n["id"] == kids[0])
        self.assertEqual(node["model_label"], "llamacpp · qwen36 · высокие")
        root_node = next(n for n in detail["tree"] if n["id"] == root_id)
        self.assertIsNone(root_node["model_label"], "у карточки без замены подписи нет")


# ---------------------------------------------------------------- безопасность
class SafetyTests(_Base):
    @unittest.skipUnless(_cfg_hash_names(file_hashes()) >= 20,
                         "нет профилей Hermes Desktop — на чистом раннере нечего сравнивать")
    def test_profile_files_untouched_by_any_flow(self):
        before = file_hashes()
        self.set_model("biz-cfo")
        self.set_model("biz-director", "anthropic", "claude-sonnet-5-5", "ultra")
        self.put("biz-cfo", {})
        self.assertGreaterEqual(_cfg_hash_names(before), 20, "ожидаем 20+ файлов профилей")
        self.assertEqual(file_hashes(), before, "файлы профилей меняться не должны")

    def test_bridge_does_not_touch_real_board(self):
        """Мост пишет только в песочную БД: слепок реальной доски не меняется."""
        before = self.sb.snapshot_real()
        self.set_model("biz-cfo")
        root_id = self.new_root("Мост и реальная доска")
        self.sa.decompose(root_id, [{"title": "Смета", "assignee": "biz-cfo", "status": "todo"}])
        models_mod.apply_to_tree(self.cfg, ROOM, root_id)
        self.sb.assert_real_untouched(before)

    def test_answers_have_no_keys(self):
        _status, body, _h = self.put("biz-cfo", {"provider": "llamacpp", "model": "qwen36",
                                                 "reasoning": "high"})
        text = json.dumps(body, ensure_ascii=False)
        for marker in ("sk-", "ghp_", "Bearer ", "api_key"):
            self.assertNotIn(marker, text)


# ---------------------------------------------------------------- точность выбора
#: каталог «как настоящий»: id-близнецы с общим префиксом, ':' и '/' внутри id и одинаковые
#: короткие имена моделей у разных провайдеров (openrouter/nous отдают `anthropic/…`).
BIG_CATALOG = {"providers": [
    {"id": "ollama-cloud", "label": "Ollama Cloud", "configured": True, "models": [
        {"id": "glm-5.3-flash"}, {"id": "glm-5.3"}, {"id": "glm-5.2"},
        {"id": "deepseek-v4.1-flash"}, {"id": "deepseek-v4.1-flash:1017"},
        {"id": "deepseek-v4-pro"}, {"id": "deepseek-v4-pro:0813"},
        {"id": "gpt-oss:20b"}]},
    {"id": "anthropic", "label": "Anthropic", "configured": True, "models": [
        {"id": "claude-sonnet-5-5"}, {"id": "claude-fable-5-1"},
        {"id": "claude-sonnet-5-5:1m"}]},
    {"id": "openrouter", "label": "OpenRouter", "configured": True, "models": [
        {"id": "anthropic/claude-sonnet-5.5"}, {"id": "anthropic/claude-fable-5.1"},
        {"id": "deepseek/deepseek-v4-pro"},
        {"id": "deepseek/deepseek-v4.1-flash-1017"}]},
    {"id": "nous", "label": "Nous", "configured": True, "models": [
        {"id": "anthropic/claude-sonnet-5.5"},
        {"id": "deepseek/deepseek-v4.1-flash-1017"}]},
    {"id": "llamacpp", "label": "LlamaCPP", "configured": True, "models": [
        {"id": "qwen36"}, {"id": "gemma4"}]},
    {"id": "custom:local", "label": "Custom", "configured": True, "models": [
        {"id": "Model-A"}, {"id": "model-a"}]},
]}

#: все пары каталога: перебор «что выбрано = что ушло» без исключений
EXACT_PAIRS = [(row["id"], model["id"]) for row in BIG_CATALOG["providers"] for model in row["models"]]
#: пары для путей с деревом/перезапуском/картой (':' и '/' в id — самые «ломающиеся»)
TRICKY_PAIRS = [("ollama-cloud", "deepseek-v4.1-flash:1017"),
                ("openrouter", "anthropic/claude-sonnet-5.5"),
                ("custom:local", "Model-A")]


class ExactnessTests(_Base):
    """«Что выбрано = что сохранено = что ушло движку» — для всех моделей всех провайдеров."""

    def setUp(self):
        super().setUp()
        self.cfg.model_catalog_override = BIG_CATALOG
        models_mod.catalog(self.cfg, force=True)

    def tearDown(self):
        self.cfg.model_catalog_override = CATALOG
        models_mod.catalog(self.cfg, force=True)

    def _created_flags(self) -> list:
        """Флаги `--model/--provider` последнего `hermes kanban create`."""
        calls = [argv for argv in self.shim.calls if "create" in argv]
        return calls[-1] if calls else []

    def test_every_model_roundtrip_is_exact(self):
        """PUT → ответ/реестр/комната/команда → карточка запроса: ни одного «ближайшего»."""
        for index, (provider, model) in enumerate(EXACT_PAIRS):
            with self.subTest(provider=provider, model=model):
                # 1) участник комнаты: что сохранено и что показывает комната
                status, body, _h = self.put("biz-cfo",
                                            {"provider": provider, "model": model,
                                             "reasoning": "high"})
                self.assertEqual(status, 200, body)
                self.assertEqual(body["agent_models"]["biz-cfo"]["provider"], provider)
                self.assertEqual(body["agent_models"]["biz-cfo"]["model"], model)
                self.assertEqual(body["applied"]["saved"]["provider"], provider)
                self.assertEqual(body["applied"]["saved"]["model"], model)
                self.assertEqual(body["applied"]["settings"]["provider"], provider)
                self.assertEqual(body["applied"]["settings"]["model"], model)
                self.assertEqual(body["member"]["model_settings"]["provider"], provider)
                self.assertEqual(body["member"]["model_settings"]["model"], model)

                detail = self.room_of_api()
                self.assertEqual(detail["agent_models"]["biz-cfo"]["model"], model)
                meta = detail["agent_models_meta"]["biz-cfo"]["model_settings"]
                self.assertEqual((meta["provider"], meta["model"]), (provider, model),
                                 "agent_models_meta — то же, что сохранено")
                member = next(m for m in detail["members"] if m["profile"] == "biz-cfo")
                self.assertEqual((member["model_settings"]["provider"],
                                  member["model_settings"]["model"]), (provider, model))

                # 2) руководитель: карточка нового запроса и флаги CLI
                self.set_model("biz-director", provider, model, "high")
                status, created, _h = self.app.dispatch(
                    "POST", "/api/rooms/%s/requests" % ROOM, {},
                    {"text": "Проверка точности модели %d." % index})
                self.assertEqual(status, 201, created)
                card = self.card(created["root_id"])
                self.assertEqual((card["provider_override"], card["model_override"]),
                                 (provider, model), "карточка получила не ту модель")
                argv = self._created_flags()
                self.assertIn("--model=%s" % model, argv)
                self.assertIn("--provider=%s" % provider, argv)
                self.assertEqual([a for a in argv if a.startswith("--model=")],
                                 ["--model=%s" % model])
                self.assertEqual([a for a in argv if a.startswith("--provider=")],
                                 ["--provider=%s" % provider])

    def test_tricky_ids_through_tree_plan_and_restart(self):
        """Дерево сторожа, под-запрос направления и перезапуск — те же точные id."""
        for provider, model in TRICKY_PAIRS:
            with self.subTest(provider=provider, model=model):
                self.set_model("biz-cfo", provider, model, "low")
                root_id = self.new_root("Точная модель %s" % model)
                kids = self.sa.decompose(root_id, [
                    {"title": "В очереди", "assignee": "biz-cfo", "status": "todo"},
                    {"title": "Идёт", "assignee": "biz-cfo", "status": "running"},
                ])
                res = models_mod.apply_to_tree(self.cfg, ROOM, root_id)
                self.assertIn(kids[0], res["updated"], res)
                card = self.card(kids[0])
                self.assertEqual((card["provider_override"], card["model_override"],
                                  card["reasoning_effort"]), (provider, model, "low"))

                self.set_model("biz-ops", provider, model, "medium")
                room = self.room()
                stream = {"id": "s-" + model.replace("/", "_").replace(":", "_"),
                          "title": "Направление %s" % model, "goal": "Проверить",
                          "owner": "biz-ops", "members": [], "depends_on": [], "due": None,
                          "status": "proposed", "created_by": "manager"}
                plan = {"streams": [stream], "brief_root_id": root_id}
                made = self.app.call("plan", "_create_request", ((self.cfg, room, stream, plan), {}))
                sub = self.card((made or {}).get("root_id") or "")
                self.assertEqual((sub["provider_override"], sub["model_override"]),
                                 (provider, model), made)

                status, body, _h = self.restart("biz-cfo")
                self.assertEqual(status, 200, body)
                self.assertIn(kids[1], body["cards"])
                after = self.card(kids[1])
                self.assertEqual((after["provider_override"], after["model_override"]),
                                 (provider, model), "перезапуск не должен менять выбор")
                self.assertEqual(after["status"], "ready")

    def test_put_echo_after_clear_is_agent_default(self):
        """Ответ PUT после «по умолчанию»: applied.settings = настройки агента, saved пуст."""
        self.set_model("biz-cfo", "anthropic", "claude-sonnet-5-5", "medium")
        status, body, _h = self.put("biz-cfo", {})
        self.assertEqual(status, 200, body)
        self.assertTrue(body["cleared"])
        self.assertEqual(body["applied"]["saved"], {})
        self.assertTrue(body["applied"]["cleared"])
        default = models_mod.agent_default(self.cfg, "biz-cfo")
        self.assertEqual(body["applied"]["settings"]["provider"], default["provider"])
        self.assertEqual(body["applied"]["settings"]["model"], default["model"])
        self.assertEqual(body["applied"]["settings"]["source"], "agent")

    def test_case_only_twins_are_not_collapsed(self):
        """`Model-A` и `model-a` — разные модели: регистр не «нормализуется»."""
        status, body, _h = self.put("biz-cfo", {"provider": "custom:local", "model": "Model-A"})
        self.assertEqual(status, 200, body)
        self.assertEqual(body["agent_models"]["biz-cfo"]["model"], "Model-A")
        status, body, _h = self.put("biz-cfo", {"provider": "custom:local", "model": "model-a"})
        self.assertEqual(status, 200, body)
        self.assertEqual(body["agent_models"]["biz-cfo"]["model"], "model-a")

        status, body, _h = self.put("biz-cfo", {"provider": "custom:local", "model": "MODEL-A"})
        self.assertEqual(status, 400, body)
        detail = json.dumps(body, ensure_ascii=False)
        self.assertIn("нет модели", detail)
        self.assertIn("suggestions", detail, "похожие id — только подсказка, не выбор")
        self.assertEqual(self.room()["agent_models"]["biz-cfo"]["model"], "model-a",
                         "неудачный PUT не должен менять сохранённое")

    def test_prefix_and_substring_are_not_guessed(self):
        """`deepseek-v4-pro:081` и `v4-pro` не превращаются в `deepseek-v4-pro`."""
        for wrong in ("deepseek-v4-pro:081", "deepseek-v4-pro:08131", "v4-pro",
                      "deepseek-v4.1-flash:101"):
            with self.subTest(model=wrong):
                status, body, _h = self.put("biz-cfo", {"provider": "ollama-cloud",
                                                        "model": wrong})
                self.assertEqual(status, 400, body)
        self.assertEqual(self.room()["agent_models"], {})

    def test_model_of_another_provider_points_to_it(self):
        """Модель чужого провайдера → 400 с подсказкой, где она есть, и без записи."""
        status, body, _h = self.put("biz-cfo", {"provider": "anthropic",
                                                "model": "deepseek-v4-pro"})
        self.assertEqual(status, 400, body)
        self.assertIn("ollama-cloud", json.dumps(body, ensure_ascii=False))
        self.assertEqual(self.room()["agent_models"], {})

    def test_provider_case_twins_are_ambiguous(self):
        """Два провайдера, отличающихся только регистром, не угадываются."""
        catalog = {"providers": BIG_CATALOG["providers"] + [
            {"id": "Ollama-Cloud", "label": "Двойник", "configured": True,
             "models": [{"id": "twin-model"}]}]}
        self.cfg.model_catalog_override = catalog
        models_mod.catalog(self.cfg, force=True)
        status, body, _h = self.put("biz-cfo", {"provider": "OLLAMA-CLOUD",
                                                "model": "deepseek-v4-pro"})
        self.assertEqual(status, 400, body)
        self.assertIn("неоднозначно", json.dumps(body, ensure_ascii=False))
        status, body, _h = self.put("biz-cfo", {"provider": "Ollama-Cloud",
                                                "model": "twin-model"})
        self.assertEqual(status, 200, body)
        self.assertEqual(body["agent_models"]["biz-cfo"]["provider"], "Ollama-Cloud")

    def test_reasoning_only_does_not_pin_agent_model(self):
        """Смена только «мышления» не закрепляет на карточке модель агента."""
        root_id = self.new_root("Только мышление")
        kids = self.sa.decompose(root_id, [{"title": "В очереди", "assignee": "biz-cfo",
                                            "status": "todo"}])
        status, body, _h = self.put("biz-cfo", {"reasoning": "low"})
        self.assertEqual(status, 200, body)
        self.assertEqual(body["applied"]["settings"]["source"], "room")
        card = self.card(kids[0])
        self.assertEqual(card["reasoning_effort"], "low")
        self.assertIsNone(card["model_override"], "модель агента — не выбор комнаты")
        self.assertIsNone(card["provider_override"])


class PartialRegistryTests(_Base):
    """Неполная пара в реестре комнаты не смешивается с дефолтом агента."""

    def _write_models(self, value: dict) -> None:
        path = Path(self.cfg.data_dir) / "rooms.json"
        data = json.loads(path.read_text("utf-8"))
        for room in data["rooms"]:
            if room.get("id") == ROOM:
                room["agent_models"] = value
        path.write_text(json.dumps(data, ensure_ascii=False), "utf-8")

    def test_provider_without_model_uses_agent_pair(self):
        self._write_models({"biz-cfo": {"provider": "anthropic", "model": "",
                                       "reasoning": "", "updated_at": 1}})
        room = self.room()
        self.assertEqual(room["agent_models"], {}, "неполная пара не отдаётся наружу")
        value = models_mod.effective(self.cfg, room, "biz-cfo")
        default = models_mod.agent_default(self.cfg, "biz-cfo")
        self.assertEqual((value["provider"], value["model"]),
                         (default["provider"], default["model"]))
        self.assertEqual(value["source"], "agent")
        args, settings = ew._model_args(self.cfg, room, "biz-cfo")
        self.assertEqual(settings["provider"], default["provider"])
        self.assertNotEqual(settings["provider"], "anthropic",
                            "провайдер комнаты не должен тащить модель агента")

    def test_model_without_provider_uses_agent_pair(self):
        self._write_models({"biz-cfo": {"provider": "", "model": "claude-sonnet-5-5",
                                       "reasoning": "", "updated_at": 1}})
        room = self.room()
        self.assertEqual(room["agent_models"], {})
        value = models_mod.effective(self.cfg, room, "biz-cfo")
        default = models_mod.agent_default(self.cfg, "biz-cfo")
        self.assertEqual((value["provider"], value["model"]),
                         (default["provider"], default["model"]))
        self.assertNotEqual(value["model"], "claude-sonnet-5-5",
                            "модель комнаты не должна тащить провайдера агента")


# ---------------------------------------------------------------- «по умолчанию»
class DefaultsTest(unittest.TestCase):
    """`agent_default` на копиях РЕАЛЬНЫХ конфигов (read-only) и на синтетических граблях."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="teamroom-defaults--")
        self.home = Path(self.tmp.name) / "hermes"
        self.home.mkdir(parents=True)
        self.cfg = types.SimpleNamespace(hermes_home=str(self.home), app_dir=str(ROOT))

    def tearDown(self):
        self.tmp.cleanup()

    def _profile_dir(self, name: str) -> Path:
        if name == "default":
            return self.home
        folder = self.home / "profiles" / name
        folder.mkdir(parents=True, exist_ok=True)
        return folder

    def _write(self, profile: str, text: str) -> None:
        (self._profile_dir(profile) / "config.yaml").write_text(text, "utf-8")

    def test_real_configs_match_yaml(self):
        """Каждый профиль — ровно свои `model.provider/model.default/agent.reasoning_effort`."""
        real = _real_hermes_home()
        if real is None:
            self.skipTest("нет реального Hermes — нечего копировать")
        try:
            import yaml                      # независимый разбор: эталон для сравнения
        except ImportError:                  # pragma: no cover - стенд без PyYAML
            self.skipTest("нет PyYAML — нечем перепроверить конфиги")
        sources = [real / "config.yaml"] + sorted((real / "profiles").glob("*/config.yaml"))
        if len(sources) < 5:
            self.skipTest("мало конфигов Hermes — на чистом раннере нечего сравнивать")

        checked = 0
        for source in sources:
            name = "default" if source.parent == real else source.parent.name
            text = source.read_text("utf-8", "replace")
            data = yaml.safe_load(text) or {}
            model = data.get("model") or {}
            agent = data.get("agent") or {}
            effort = str(agent.get("reasoning_effort") or "").strip()
            if effort.lower() in ("false", "none", "disabled"):
                effort = "none"
            expected = {"provider": str(model.get("provider") or ""),
                        "model": str(model.get("default") or model.get("name") or ""),
                        "reasoning": effort}
            (self._profile_dir(name) / "config.yaml").write_text(text, "utf-8")
            self.assertEqual(models_mod.agent_default(self.cfg, name), expected,
                             "дефолт профиля «%s» разошёлся с его config.yaml" % name)
            checked += 1
        self.assertGreaterEqual(checked, 5)

        for name, expected in (("biz-cfo", ("ollama-cloud", "glm-5.3-flash", "ultra")),
                               ("biz-director", ("ollama-cloud", "glm-5.3-flash", "ultra")),
                               ("biz-critic", ("ollama-cloud", "glm-5.3-flash", "ultra"))):
            with self.subTest(profile=name):
                value = models_mod.agent_default(self.cfg, name)
                self.assertEqual((value["provider"], value["model"], value["reasoning"]),
                                 expected)
        root = models_mod.agent_default(self.cfg, "default")
        self.assertEqual((root["provider"], root["model"]), ("anthropic", "claude-sonnet-5-5"),
                         "корневой config: provider и default вместе, а не по отдельности")

    def test_four_space_indent(self):
        self._write("deep", "model:\n    provider: ollama-cloud\n"
                            "    default: deepseek-v4.1-flash:1017\n"
                            "agent:\n    reasoning_effort: ultra\n")
        self.assertEqual(models_mod.agent_default(self.cfg, "deep"),
                         {"provider": "ollama-cloud", "model": "deepseek-v4.1-flash:1017",
                          "reasoning": "ultra"})

    def test_no_model_default_and_keys_without_section(self):
        """Профиль без `model.default` — пустая модель, а не чужая; без `model` — пусто."""
        self._write("bare", "agent:\n  max_turns: 20\n  reasoning_effort: high\n")
        self.assertEqual(models_mod.agent_default(self.cfg, "bare"),
                         {"provider": "", "model": "", "reasoning": "high"})
        self._write("nomodel", "model:\n  provider: anthropic\n")
        self.assertEqual(models_mod.agent_default(self.cfg, "nomodel"),
                         {"provider": "anthropic", "model": "", "reasoning": ""})
        self.assertEqual(models_mod.agent_default(self.cfg, "нет-такого"),
                         {"provider": "", "model": "", "reasoning": ""})

    def test_delegation_effort_is_not_the_agent_effort(self):
        """`delegation.reasoning_effort` — уровень детей, а не «по умолчанию» агента."""
        self._write("coder-like", "model:\n  provider: anthropic\n  default: claude-sonnet-5-5\n"
                                  "agent:\n  max_turns: 150\n"
                                  "delegation:\n  provider: ollama-cloud\n"
                                  "  model: deepseek-v4.1-flash:1017\n"
                                  "  reasoning_effort: max\n")
        value = models_mod.agent_default(self.cfg, "coder-like")
        self.assertEqual(value["reasoning"], "", "это уровень делегатов, не агента")
        self.assertEqual((value["provider"], value["model"]),
                         ("anthropic", "claude-sonnet-5-5"))

    def test_nested_keys_do_not_leak(self):
        """`model.aliases.deepseek` не становится `model.default`."""
        self._write("nested", "model:\n  provider: ollama-cloud\n"
                              "  aliases:\n    deepseek: deepseek-v4-pro\n"
                              "  default: glm-5.3-flash\n"
                              "agent:\n  reasoning_effort: \u00aboff\u00bb\n")
        value = models_mod.agent_default(self.cfg, "nested")
        self.assertEqual(value["model"], "glm-5.3-flash")

    def test_false_reasoning_means_off(self):
        self._write("off", "model:\n  provider: llamacpp\n  default: qwen36\n"
                           "agent:\n  reasoning_effort: false\n")
        self.assertEqual(models_mod.agent_default(self.cfg, "off")["reasoning"], "none")

    def test_quoted_and_equals_values(self):
        self._write("quoted", "model:\n  provider: 'ollama-cloud'\n"
                              "  default: \"gpt-oss:20b\"\n")
        self.assertEqual(models_mod.agent_default(self.cfg, "quoted"),
                         {"provider": "ollama-cloud", "model": "gpt-oss:20b",
                          "reasoning": ""})


# ---------------------------------------------------------------- модель реально ушла (W13-A)

class RealModelTests(_Base):
    """W13-A: карточка комнаты не должна стартовать на чужой модели.

    Проверяются все пути появления карточки: приложение (корень), руководитель через CLI
    (карточка в tenant без связи с деревом), критик/доработка приложением, перезапуск,
    retry, и сверка ФАКТА из сессии воркера («ушло на: …»).
    """

    def setUp(self):
        super().setUp()
        self._real_state_path = models_mod.state_path
        self._session_dbs: dict = {}
        self._state_seq = 0
        models_mod.state_path = self._state_path
        models_mod._SESSION_CACHE.clear()
        # Класс делит одну песочницу: гасим «идущие» карточки прошлых тестов и журнал
        # перехватов, иначе проверка видит чужие запуски и считает их расхождениями.
        con = self.sa.connect()
        try:
            con.execute("UPDATE tasks SET status='done' WHERE tenant='room:cafe' "
                        "AND status='running'")
            con.commit()
        finally:
            con.close()
        try:
            (Path(self.cfg.state_dir) / ROOM / "model_fixups.json").unlink()
        except OSError:
            pass

    def tearDown(self):
        models_mod.state_path = self._real_state_path
        models_mod._SESSION_CACHE.clear()

    # --- стенд: хранилище сессий профиля ---------------------------------
    def _state_path(self, cfg, profile) -> Path:
        """Свой `state.db` на профиль: реальные сессии Hermes в тестах не читаем."""
        name = str(profile or "default")
        path = self._session_dbs.get(name)
        if path is None:
            self._state_seq += 1
            path = Path(self.sb.root) / ("state-%s-%d.db"
                                         % (name.replace("/", "_"), self._state_seq))
            con = sqlite3.connect(str(path))
            try:
                con.execute("CREATE TABLE IF NOT EXISTS sessions (id TEXT PRIMARY KEY, "
                            "model TEXT, billing_provider TEXT)")
                con.commit()
            finally:
                con.close()
            self._session_dbs[name] = path
        return path

    def _make_session(self, profile: str, session_id: str, model: str, provider: str) -> None:
        con = sqlite3.connect(str(self._state_path(self.cfg, profile)))
        try:
            con.execute("INSERT OR REPLACE INTO sessions (id, model, billing_provider) "
                        "VALUES (?,?,?)", (session_id, model, provider))
            con.commit()
        finally:
            con.close()

    def _run_with_session(self, card_id: str, profile: str, session_id: str,
                          started_at: int | None = None) -> int:
        """Запущенный прогон карточки с id сессии воркера (как пишет движок)."""
        now = int(time.time())
        started = int(started_at or (now - 120))
        con = self.sa.connect()
        try:
            con.execute("UPDATE tasks SET status='running', started_at=?, assignee=? "
                        "WHERE id=?", (started, profile, card_id))
            con.execute("INSERT INTO task_runs (task_id, profile, status, started_at, metadata) "
                        "VALUES (?,?,?,?,?)",
                        (card_id, profile, "running", started,
                         json.dumps({"worker_session_id": session_id})))
            run_id = con.execute("SELECT max(id) FROM task_runs WHERE task_id = ?",
                                 (card_id,)).fetchone()[0]
            con.execute("UPDATE tasks SET current_run_id=? WHERE id=?", (run_id, card_id))
            con.execute("INSERT INTO task_events (task_id, run_id, kind, payload, created_at) "
                        "VALUES (?,?,?,?,?)",
                        (card_id, run_id, "owner_admitted",
                         json.dumps({"session_id": session_id}), started + 1))
            con.commit()
        finally:
            con.close()
        return int(run_id)

    def _orchestrator_card(self, title: str, assignee: str, status: str = "ready") -> str:
        """Карточка, какую создаёт руководитель сам: tenant комнаты, без связи с деревом."""
        return self.sa.card(title=title, assignee=assignee, status=status, tenant="room:cafe",
                            created_by="default")

    def _journal(self) -> list:
        path = Path(self.cfg.state_dir) / ROOM / "journal.jsonl"
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text("utf-8").splitlines() if line.strip()]

    # --- 1. причина: чужая карточка не видна дереву -----------------------
    def test_orchestrator_card_is_invisible_to_tree_pass(self):
        """Сторож по ДЕРЕВУ не видит карточку руководителя — ровно причина бага 1.0.1."""
        self.set_model("biz-cfo")
        root_id = self.new_root("Дерево запроса")
        card_id = self._orchestrator_card("Смета от руководителя", "biz-cfo")

        blind = models_mod.apply_to_tree(self.cfg, ROOM, root_id)
        self.assertNotIn(card_id, blind["updated"])
        self.assertIsNone(self.card(card_id)["model_override"],
                          "карточка вне дерева — старая проверка её не видит")

        seen = models_mod.apply_to_room(self.cfg, ROOM)
        self.assertIn(card_id, seen["updated"], seen)
        card = self.card(card_id)
        self.assertEqual((card["model_override"], card["provider_override"],
                          card["reasoning_effort"]), ("qwen36", "llamacpp", "high"))

    def test_watcher_pins_room_cards_by_tenant(self):
        """Тик сторожа закрепляет модель на карточке комнаты, созданной не приложением."""
        self.set_model("biz-cfo")
        card_id = self._orchestrator_card("Черновик сметы", "biz-cfo")
        self.app.watcher.tick_once()
        card = self.card(card_id)
        self.assertEqual((card["model_override"], card["reasoning_effort"]), ("qwen36", "high"))

    def test_room_pass_is_idempotent(self):
        self.set_model("biz-cfo")
        self._orchestrator_card("Раз", "biz-cfo")
        first = models_mod.apply_to_room(self.cfg, ROOM)
        second = models_mod.apply_to_room(self.cfg, ROOM)
        self.assertTrue(first["updated"])
        self.assertEqual(second["updated"], [], "повторный проход ничего не меняет")

    # --- 2. контракт: модель в теле запроса -------------------------------
    def test_request_body_lists_models_for_every_executor(self):
        """Бриф/тело запроса прямо говорит руководителю, с какими флагами создавать детей."""
        self.set_model("biz-cfo", "ollama-cloud", "glm-5.3", "ultra")
        status, body, _h = self.app.dispatch(
            "POST", "/api/rooms/%s/requests" % ROOM, {},
            {"text": "Посчитать экономику набора из шести моти."})
        self.assertEqual(status, 201, body)
        row = self.sb.one("SELECT body FROM tasks WHERE id = ?", (body["root_id"],))
        text = row["body"]
        self.assertIn("МОДЕЛИ ИСПОЛНИТЕЛЕЙ", text)
        self.assertIn("- biz-cfo (исполнитель) — ollama-cloud · glm-5.3 · "
                      "предельные → при создании карточки: "
                      "--model=glm-5.3 --provider=ollama-cloud", text)
        self.assertIn("модель профиля: ", text)
        self.assertIn("(в комнате не выбиралась) → флаги не нужны", text)
        self.assertIn("hermes kanban --board=team create", text)
        # служебный блок не должен всплывать в интерфейсе как текст Михаила
        from server import contract as contract_mod
        self.assertNotIn("МОДЕЛИ ИСПОЛНИТЕЛЕЙ", contract_mod.visible_text(text))

    def test_models_block_written_once(self):
        self.set_model("biz-cfo")
        room = self.room()
        block = models_mod.models_block(self.cfg, room)
        self.assertIn("МОДЕЛИ ИСПОЛНИТЕЛЕЙ", block)
        body = ew._with_models_block(self.cfg, room, block)      # повторная сборка
        self.assertEqual(body.count("МОДЕЛИ ИСПОЛНИТЕЛЕЙ"), 2, "второй блок не дописывается")

    # --- 3. пути создания карточки ---------------------------------------
    def test_child_card_gets_model_and_effort_at_creation(self):
        """Критик/доработка приложением: флаги в `create` + уровень мышления мостом."""
        self.set_model("biz-cfo", "llamacpp", "gemma4", "max")
        room = self.room()
        root_id = self.new_root("Критика ответа")
        critic_like = ew.create_child_card(self.cfg, room, "team", root_id, "biz-cfo",
                                           "Критика ответа (раунд 1)", "тело", "critic")
        card = self.card(critic_like)
        self.assertEqual((card["model_override"], card["provider_override"],
                          card["reasoning_effort"]), ("gemma4", "llamacpp", "max"))
        argv = [a for a in self.shim.calls if "create" in a][-1]
        self.assertIn("--model=gemma4", argv)
        self.assertIn("--provider=llamacpp", argv)
        self.assertEqual([a for a in argv if a.startswith("--model=")], ["--model=gemma4"])

    def test_tricky_ids_survive_child_card(self):
        """`:` и `/` в id модели доходят до карточки ребёнка без подмены."""
        for provider, model in TRICKY_PAIRS:
            with self.subTest(provider=provider, model=model):
                status, _body, _h = self.put("biz-cfo", {"provider": provider, "model": model,
                                                        "reasoning": ""})
                self.assertEqual(status, 400, "в тестовом каталоге таких моделей нет")
        self.cfg.model_catalog_override = BIG_CATALOG
        try:
            models_mod.catalog(self.cfg, force=True)
            for provider, model in TRICKY_PAIRS:
                with self.subTest(provider=provider, model=model):
                    status, body, _h = self.put("biz-cfo", {"provider": provider, "model": model,
                                                           "reasoning": "low"})
                    self.assertEqual(status, 200, body)
                    root_id = self.new_root("Точная модель ребёнка")
                    card_id = ew.create_child_card(self.cfg, self.room(), "team", root_id,
                                                   "biz-cfo", "Доработка", "тело", "revision")
                    card = self.card(card_id)
                    self.assertEqual((card["provider_override"], card["model_override"]),
                                     (provider, model))
                    argv = [a for a in self.shim.calls if "create" in a][-1]
                    self.assertIn("--model=%s" % model, argv)
        finally:
            self.cfg.model_catalog_override = CATALOG
            models_mod.catalog(self.cfg, force=True)

    def test_restart_and_retry_keep_room_model(self):
        self.set_model("biz-cfo", "llamacpp", "gemma4", "medium")
        card_id = self._orchestrator_card("Идущая карточка", "biz-cfo", status="running")
        status, body, _h = self.restart("biz-cfo")
        self.assertEqual(status, 200, body)
        self.assertIn(card_id, body["cards"])
        card = self.card(card_id)
        self.assertEqual((card["status"], card["model_override"], card["reasoning_effort"]),
                         ("ready", "gemma4", "medium"))
        # retry: возвращает в очередь и НЕ теряет выбор комнаты
        retry = ew.act(self.cfg, "team", card_id, "retry", task_id=card_id)
        self.assertTrue(retry.get("ok"), retry)
        self.assertEqual(self.card(card_id)["status"], "ready")
        self.assertEqual(self.card(card_id)["model_override"], "gemma4")

    # --- 4. сверка факта и перехват --------------------------------------
    def test_verify_finds_wrong_model_and_returns_card(self):
        """Карточка уже поехала на модели профиля → останавливаем и возвращаем на выбор."""
        self.set_model("biz-cfo")                       # llamacpp / qwen36 / high
        card_id = self._orchestrator_card("Считает не на той", "biz-cfo", status="running")
        sid = "local-wrong-model-1"
        self._make_session("biz-cfo", sid, "glm-5.3-flash", "ollama-cloud")
        self._run_with_session(card_id, "biz-cfo", sid)

        res = models_mod.verify_started(self.cfg, ROOM)
        self.assertEqual(res["checked"], 1, res)
        self.assertFalse(res["confirmed"])
        self.assertEqual([m["id"] for m in res["mismatched"]], [card_id])
        self.assertIn("ollama-cloud · glm-5.3-flash", res["mismatched"][0]["actual"])
        self.assertEqual(res["fixed"], [card_id], res)

        card = self.card(card_id)
        self.assertEqual(card["status"], "ready", "после reclaim карточка ждёт нового запуска")
        self.assertEqual((card["model_override"], card["provider_override"]),
                         ("qwen36", "llamacpp"))
        self.assertTrue(any("reassign" in a and "--reclaim" in a for a in self.shim.calls),
                        "перехват идёт штатным reassign --reclaim")
        texts = [e["text"] for e in self._journal() if e.get("kind") in
                 ("model_changed", "model_restarted")]
        self.assertTrue(any("ушла на ollama-cloud · glm-5.3-flash" in t for t in texts), texts)
        self.assertTrue(any("остановлена и возвращена" in t for t in texts), texts)

    def test_verify_confirms_correct_model_and_touches_nothing(self):
        self.set_model("biz-cfo")
        card_id = self._orchestrator_card("Считает на своей", "biz-cfo", status="running")
        sid = "local-right-model-1"
        self._make_session("biz-cfo", sid, "qwen36", "llamacpp")
        self._run_with_session(card_id, "biz-cfo", sid)
        before = len(self.shim.calls)
        res = models_mod.verify_started(self.cfg, ROOM)
        self.assertEqual((res["checked"], res["confirmed"]), (1, 1), res)
        self.assertEqual(res["mismatched"], [])
        self.assertEqual(res["fixed"], [])
        self.assertEqual(self.card(card_id)["status"], "running", "карточку не трогали")
        reassigns = [a for a in self.shim.calls[before:] if "reassign" in a]
        self.assertEqual(reassigns, [])

    def test_verify_stops_after_fixup_limit(self):
        """Защита от петли: не больше `FIXUP_MAX` перехватов на карточку."""
        self.set_model("biz-cfo")
        card_id = self._orchestrator_card("Упрямая карточка", "biz-cfo", status="running")
        for attempt in range(models_mod.FIXUP_MAX + 1):
            sid = "local-loop-%d" % attempt
            self._make_session("biz-cfo", sid, "glm-5.3", "ollama-cloud")
            self._run_with_session(card_id, "biz-cfo", sid)
            res = models_mod.verify_started(self.cfg, ROOM)
            self.assertEqual([m["id"] for m in res["mismatched"]], [card_id])
            if attempt < models_mod.FIXUP_MAX:
                self.assertEqual(res["fixed"], [card_id], "первые попытки перехватываем")
            else:
                self.assertEqual(res["fixed"], [], "дальше только предупреждение")

    def test_verify_skips_fresh_and_unselected(self):
        self.set_model("biz-cfo")
        card_id = self._orchestrator_card("Только что взяли", "biz-cfo", status="running")
        sid = "local-fresh-1"
        self._make_session("biz-cfo", sid, "glm-5.3", "ollama-cloud")
        self._run_with_session(card_id, "biz-cfo", sid, started_at=int(time.time()))
        res = models_mod.verify_started(self.cfg, ROOM)
        self.assertEqual(res["mismatched"][0]["id"], card_id, "расхождение видно всегда")
        self.assertEqual(res["fixed"], [], "только что стартовавшую не снимаем")

    # --- 5. «ушло на» -----------------------------------------------------
    def test_sent_shows_actual_model_and_mismatch(self):
        self.set_model("biz-cfo", "ollama-cloud", "glm-5.3", "ultra")
        card_id = self._orchestrator_card("Куда ушло", "biz-cfo", status="running")
        sid = "local-sent-1"
        self._make_session("biz-cfo", sid, "glm-5.3-flash", "ollama-cloud")
        self._run_with_session(card_id, "biz-cfo", sid)
        card = models_mod.card_map(self.cfg, self.room())[card_id]
        sent = models_mod.sent_of(self.cfg, self.room(), card, board="team")
        self.assertEqual(sent["text"], "ушло на: ollama-cloud · glm-5.3-flash")
        self.assertTrue(sent["confirmed"])
        self.assertTrue(sent["mismatch"])
        self.assertEqual(sent["note"], "расходится с выбором комнаты")
        self.assertEqual(sent["expected"]["model"], "glm-5.3")

    def test_sent_without_run_falls_back_to_card(self):
        """Нет подтверждения из запуска — показываем запись карточки, без выдумок."""
        self.set_model("biz-cfo")
        card_id = self._orchestrator_card("Ещё не поехала", "biz-cfo")
        models_mod.apply_to_room(self.cfg, ROOM)
        card = models_mod.card_map(self.cfg, self.room())[card_id]
        sent = models_mod.sent_of(self.cfg, self.room(), card, board="team")
        self.assertFalse(sent["confirmed"])
        self.assertEqual(sent["text"], "ушло на: llamacpp · qwen36 · высокие")
        self.assertFalse(sent["mismatch"])

    def test_tree_node_has_model_sent(self):
        self.set_model("biz-cfo")
        root_id = self.new_root("Дерево с фактом")
        kids = self.sa.decompose(root_id, [{"title": "Смета", "assignee": "biz-cfo",
                                            "status": "running"}])
        sid = "local-tree-1"
        self._make_session("biz-cfo", sid, "qwen36", "llamacpp")
        self._run_with_session(kids[0], "biz-cfo", sid)
        detail = self.app.call("engine_read", "request_detail",
                               ((self.cfg, "team", root_id, self.room()), {}))
        node = next(n for n in detail["tree"] if n["id"] == kids[0])
        self.assertEqual(node["model_sent"]["text"], "ушло на: llamacpp · qwen36")
        self.assertTrue(node["model_sent"]["confirmed"])
        self.assertFalse(node["model_sent"]["mismatch"])

    def test_team_member_has_sent(self):
        self.set_model("biz-cfo", "ollama-cloud", "glm-5.3", "high")
        card_id = self._orchestrator_card("Для команды", "biz-cfo", status="running")
        sid = "local-team-1"
        self._make_session("biz-cfo", sid, "glm-5.3-flash", "ollama-cloud")
        # W19 B-29: предупреждение — только если карточка ушла не туда ПОСЛЕ смены
        # модели. Запуск явно позже смены (started_at = сейчас), иначе это ожидание
        # «Новая модель применится со следующей задачи» (см. tests/test_team_fix.py).
        self._run_with_session(card_id, "biz-cfo", sid, started_at=int(time.time()))
        status, body, _h = self.app.dispatch("GET", "/api/rooms/%s/team" % ROOM, {}, {})
        self.assertEqual(status, 200)
        member = next(m for m in body["members"] if m["profile"] == "biz-cfo")
        self.assertEqual(member["model_settings"]["model"], "glm-5.3")
        self.assertEqual(member["model_sent"]["text"], "ушло на: ollama-cloud · glm-5.3-flash")
        self.assertTrue(member["model_sent"]["mismatch"])
        self.assertFalse(member["model_sent"]["pending"])

    def test_sent_endpoint(self):
        self.set_model("biz-cfo")
        card_id = self._orchestrator_card("Эндпоинт", "biz-cfo", status="running")
        sid = "local-ep-1"
        self._make_session("biz-cfo", sid, "gemma4", "llamacpp")
        self._run_with_session(card_id, "biz-cfo", sid)
        status, body, _h = self.app.dispatch("GET", "/api/rooms/%s/models/sent" % ROOM, {}, {})
        self.assertEqual(status, 200, body)
        member = next(m for m in body["members"] if m["profile"] == "biz-cfo")
        self.assertEqual(member["model_sent"]["text"], "ушло на: llamacpp · gemma4")
        self.assertIn(card_id, [c["id"] for c in body["cards"]])

    def test_check_endpoint_reports_without_fixing(self):
        self.set_model("biz-cfo")
        card_id = self._orchestrator_card("Проверка", "biz-cfo", status="running")
        sid = "local-check-1"
        self._make_session("biz-cfo", sid, "glm-5.3", "ollama-cloud")
        self._run_with_session(card_id, "biz-cfo", sid)
        status, body, _h = self.app.dispatch("POST", "/api/rooms/%s/models/check" % ROOM, {},
                                             {"fix": False})
        self.assertEqual(status, 200, body)
        self.assertEqual([m["id"] for m in body["mismatched"]], [card_id])
        self.assertEqual(body["fixed"], [])
        self.assertEqual(self.card(card_id)["status"], "running")

    def test_no_secrets_in_sent_payload(self):
        self.set_model("biz-cfo")
        self._orchestrator_card("Секреты", "biz-cfo", status="running")
        status, body, _h = self.app.dispatch("GET", "/api/rooms/%s/models/sent" % ROOM, {}, {})
        self.assertEqual(status, 200)
        text = json.dumps(body, ensure_ascii=False)
        for marker in ("sk-", "ghp_", "Bearer ", "api_key"):
            self.assertNotIn(marker, text)


def _real_hermes_home() -> Path | None:
    """Реальный `%HERMES_HOME%` — только чтение (копии для теста)."""
    try:
        from tests.helpers import real_hermes_home
        home = Path(real_hermes_home())
    except Exception:  # noqa: BLE001 - на чистом раннере helpers может не найти путь
        return None
    return home if (home / "config.yaml").exists() else None


if __name__ == "__main__":
    unittest.main(verbosity=2)
