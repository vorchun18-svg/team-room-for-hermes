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
import subprocess as real_subprocess
import sys
import time
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
            card_id = self.sa.card(title=title[:120], assignee=assignee, status=status,
                                   created_by="Комната команды")
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


if __name__ == "__main__":
    unittest.main(verbosity=2)
