# -*- coding: utf-8 -*-
"""W29-I1: «песочница не пишет на реальную доску Hermes» — единая защита.

Инцидент 21.11.2026: песочный сервер (свой ``TEAMROOM_DATA``, но с НАСТОЯЩИМ домом Hermes)
снял пины и паузы у восьми боевых карточек: ``TEAMROOM_DATA`` песочницу доски не даёт —
доска берётся из ``hermes_home``/``HERMES_KANBAN_HOME``.

Что проверяется здесь (ни одна проверка не пишет в настоящий Hermes — отказ происходит
ДО вызова CLI/библиотеки/моста, а «разрешённый» случай идёт в подставной запуск):

1. Песочница + реальный дом Hermes → отказ у CLI (``engine_cli._run``), у библиотеки
   (``kanban_atomic.run/launch``), у моста (``kb_bridge --set`` и ``agent_models._bridge``).
2. Песочница + СВОЯ доска → запись разрешена (обычная работа тестов и демо).
3. Боевой режим (свои данные по умолчанию) + реальная доска → запись разрешена.
4. Обход — ТОЛЬКО ``TEAMROOM_ALLOW_REAL_BOARD=1``.
5. Сторожа (``model_guard.tick/preflight``, ``watcher``, ``svod_live``, ``critic_loop``)
   в таком режиме вообще не идут в доску.
"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(ROOT / "tools") not in sys.path:
    sys.path.insert(0, str(ROOT / "tools"))

import server.agent_models as models_mod                    # noqa: E402
import server.board_guard as guard                          # noqa: E402
import server.config as config_mod                          # noqa: E402
import server.critic_loop as critic_mod                     # noqa: E402
import server.engine_cli as cli                             # noqa: E402
import server.engine_write as ew                            # noqa: E402
import server.kanban_atomic as atomic                       # noqa: E402
import server.model_guard as mg                             # noqa: E402
import server.recipe_agent as recipe_agent                  # noqa: E402
import server.svod_live as svod                             # noqa: E402
import server.watcher as watcher_mod                        # noqa: E402
import kb_bridge                                            # noqa: E402
from server.engine_cli import EngineWriteError               # noqa: E402
from tests.helpers import _is_under, _outside_hermes_base    # noqa: E402

ENV_KEYS = ("HERMES_HOME", "HERMES_KANBAN_HOME", "TEAMROOM_HERMES_HOME", "TEAMROOM_DATA",
            "TEAMROOM_VAULT_DIR", "TEAMROOM_SANDBOX", guard.ALLOW_ENV, "TEAMROOM_TEST_TMP")
BOARD = "team"
CARD = "t_0000cafe"


class _Env:
    """Временная подмена окружения: тест видит ровно те пути, которые сам задал."""

    def __init__(self, **values):
        self.values = values
        self.saved: dict = {}

    def __enter__(self):
        for key in ENV_KEYS:
            self.saved[key] = os.environ.get(key)
            os.environ.pop(key, None)
        for key, value in self.values.items():
            if value is not None:
                os.environ[key] = str(value)
        return self

    def __exit__(self, *_exc):
        for key, value in self.saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        return False


def _fake_home(base: Path, name: str) -> Path:
    """Поддельный дом Hermes: `profiles/` и доска `kanban/boards/team/kanban.db`."""
    home = Path(base) / name
    (home / "profiles" / "worker-profile").mkdir(parents=True, exist_ok=True)
    db = home / "kanban" / "boards" / BOARD / "kanban.db"
    db.parent.mkdir(parents=True, exist_ok=True)
    db.write_bytes(b"")                       # файл-заглушка: CLI его не откроет
    return home


class _StubProc:
    """Подставной запуск: настоящий `hermes` в тестах не вызывается никогда."""

    def __init__(self, rc: int = 0, out: str = ""):
        self.calls: list = []
        self.rc = rc
        self.out = out

    def run(self, argv, **_kw):
        self.calls.append(list(argv))

        class _Done:
            returncode = self.rc
            stdout = self.out
            stderr = ""

        return _Done()


class SandboxRealBoardTest(unittest.TestCase):
    """Песочница (свои данные) + РЕАЛЬНЫЙ дом Hermes → отказ на всех путях записи."""

    @classmethod
    def setUpClass(cls):
        cls.base = Path(tempfile.mkdtemp(prefix="w29i1-guard-", dir=str(_outside_hermes_base())))
        cls.real = _fake_home(cls.base, "hermes")               # «реальный» дом Hermes
        cls.data = cls.base / "data-sandbox"                    # свои данные приложения
        cls.data.mkdir(parents=True, exist_ok=True)

    @classmethod
    def tearDownClass(cls):
        import shutil
        shutil.rmtree(cls.base, ignore_errors=True)

    def env(self, **extra):
        values = {"HERMES_HOME": self.real, "HERMES_KANBAN_HOME": self.real,
                  "TEAMROOM_DATA": self.data, "TEAMROOM_VAULT_DIR": "off",
                  "TEAMROOM_SANDBOX": "1"}
        values.update(extra)
        return _Env(**values)

    def cfg(self):
        return config_mod.load()

    # --- сама проверка -----------------------------------------------------
    def test_check_names_sandbox_and_real_home(self):
        with self.env():
            cfg = self.cfg()
            self.assertFalse(cfg.sandbox, "TEAMROOM_HERMES_HOME не задан — это не cfg.sandbox")
            found = guard.check(cfg, BOARD)
            self.assertIsNotNone(found, "песочница с реальной доской не поймана")
            self.assertEqual(found["code"], "sandbox_real_board")
            self.assertIn(guard.ALLOW_ENV, found["message"],
                          "в причине нет кнопки-обхода — тупик для человека")
            self.assertIn("TEAMROOM_SANDBOX=1", " ".join(found["detail"]["signs"]))
            self.assertEqual(guard.status(cfg)["blocked"], True)

    def test_engine_cli_write_refused_before_subprocess(self):
        stub = _StubProc()
        saved = cli.proc
        cli.proc = stub
        try:
            with self.env():
                cfg = self.cfg()
                with self.assertRaises(EngineWriteError) as ctx:
                    cli._run(cfg, BOARD, ["comment", CARD, "--", "текст"])
                self.assertEqual(ctx.exception.code, "sandbox_real_board")
                self.assertEqual(ctx.exception.http_status, 409)
                with self.assertRaises(EngineWriteError):
                    cli._run(cfg, BOARD, ["block", CARD, "--kind=needs_input"])
                self.assertEqual([], stub.calls, "CLI всё-таки запустили")
        finally:
            cli.proc = saved

    def test_engine_write_card_block_refused(self):
        with self.env():
            cfg = self.cfg()
            with self.assertRaises(EngineWriteError) as ctx:
                ew.card_block(cfg, BOARD, CARD, reason="проверка")
            self.assertEqual(ctx.exception.code, "sandbox_real_board")

    def test_reads_are_not_blocked(self):
        """Чтение боевой доски песочнице не запрещено — только запись."""
        stub = _StubProc(out="[]")
        saved = cli.proc
        cli.proc = stub
        try:
            with self.env():
                cfg = self.cfg()
                self.assertTrue(guard.is_write_command(["comment", CARD]))
                self.assertFalse(guard.is_write_command(["show", CARD, "--json"]))
                out, _ms = cli._run(cfg, BOARD, ["show", CARD, "--json"])
                self.assertEqual("[]", out)
                self.assertEqual(1, len(stub.calls), "чтение должно доходить до CLI")
        finally:
            cli.proc = saved

    def test_kanban_atomic_refused(self):
        with self.env():
            cfg = self.cfg()
            req = {"op": "stop", "task_id": CARD, "db": str(cfg.kanban_db(BOARD)),
                   "home": str(cfg.kanban_home), "tree": str(cfg.kanban_home)}
            res = atomic.run(req)
            self.assertFalse(res.get("ok"), res)
            self.assertEqual(res.get("code"), "sandbox_real_board", res)
            with self.assertRaises(atomic.AtomicError) as ctx:
                atomic.launch(cfg, req)
            self.assertEqual(ctx.exception.code, "sandbox_real_board")

    def test_kb_bridge_set_refused(self):
        with self.env():
            cfg = self.cfg()
            argv = ["--set", "--db", str(cfg.kanban_db(BOARD)),
                    "--task", CARD, "--model", "model-x"]
            with self.assertRaises(SystemExit) as ctx:
                kb_bridge.main(argv)
            text = str(ctx.exception.code)
            self.assertIn("sandbox_real_board", text)
            self.assertIn(guard.ALLOW_ENV, text)

    def test_agent_models_bridge_refused_before_process(self):
        stub = _StubProc()
        saved = models_mod.proc
        models_mod.proc = stub
        try:
            with self.env():
                cfg = self.cfg()
                with self.assertRaises(models_mod.ModelError) as ctx:
                    models_mod._bridge(cfg, ["--set", "--db", str(cfg.kanban_db(BOARD)),
                                             "--task", CARD, "--model", "model-x"])
                self.assertEqual(ctx.exception.code, "sandbox_real_board")
                self.assertEqual([], stub.calls, "мост всё-таки запустили")
                # каталог — чтение: не блокируется
        finally:
            models_mod.proc = saved

    def test_watchers_do_not_touch_board(self):
        with self.env():
            cfg = self.cfg()
            self.assertTrue(watcher_mod.board_block_reason(cfg))
            room = {"id": "cafe", "board": BOARD, "tenant": "room:cafe", "mode": "enforce",
                    "agent_models": {"worker-profile": {"provider": "prov-a",
                                                        "model": "model-a"}}}
            tick = mg.tick(cfg, room)
            self.assertEqual(tick.get("skipped_reason"), "sandbox_real_board", tick)
            pre = mg.preflight(cfg, room)
            self.assertEqual(pre.get("skipped_reason"), "sandbox_real_board", pre)
            self.assertEqual([], pre.get("pinned") or [])
            svod_out = svod.tick(cfg, room, {"root_id": CARD}, {})
            self.assertEqual(svod_out.get("reason"), "sandbox_real_board", svod_out)
            report = critic_mod.run_step(cfg, room, {"root_id": CARD},
                                         {"tree": [], "root_id": CARD})
            self.assertEqual([r.get("reason") for r in report], ["sandbox_real_board"])

    # --- W31-FIX3: инцидент 27.11.2026 (три карточки «Анализ задач» на живой доске) ---
    def test_temp_data_dir_alone_is_not_a_sandbox_sign(self):
        """Временные данные без явной пометки — НЕ песочница (решение 22.11 сохранено).

        Тесты, читающие реальную доску (`test_e2e.RealBoardAdoptTest`), работают во
        временных данных; признак «временный каталог» блокировал им запись зеркал.
        Инцидент 27.11 (карточки «Анализ задач») закрыт у источника: разбор выключен по
        умолчанию и сам карточек не создаёт (tests/test_recipe_agent.py).
        """
        import shutil
        temp_base = Path(tempfile.gettempdir())
        if _is_under(temp_base, self.real):
            self.skipTest("системный temp внутри «реального» дома Hermes")
        data = Path(tempfile.mkdtemp(prefix="w31f3-", dir=str(temp_base)))
        try:
            with _Env(HERMES_HOME=self.real, HERMES_KANBAN_HOME=self.real,
                      TEAMROOM_DATA=data, TEAMROOM_VAULT_DIR="off"):
                cfg = self.cfg()
                self.assertIsNone(guard.check(cfg, BOARD))
        finally:
            shutil.rmtree(data, ignore_errors=True)


class AllowedCasesTest(unittest.TestCase):
    """Своя доска и боевой режим — запись разрешена; обход — только переменной."""

    @classmethod
    def setUpClass(cls):
        cls.base = Path(tempfile.mkdtemp(prefix="w29i1-ok-", dir=str(_outside_hermes_base())))

    @classmethod
    def tearDownClass(cls):
        import shutil
        shutil.rmtree(cls.base, ignore_errors=True)

    def test_sandbox_with_own_board_is_allowed(self):
        own = _fake_home(self.base, "sandbox-home")
        with _Env(TEAMROOM_HERMES_HOME=own, TEAMROOM_DATA=self.base / "data-own",
                  TEAMROOM_VAULT_DIR="off"):
            cfg = config_mod.load()
            self.assertTrue(cfg.sandbox)
            self.assertIsNone(guard.check(cfg, BOARD),
                              "песочница со своей доской не должна блокироваться")

    def test_production_default_data_is_allowed(self):
        real = _fake_home(self.base, "hermes-prod")
        with _Env(HERMES_HOME=real, HERMES_KANBAN_HOME=real, TEAMROOM_VAULT_DIR="off"):
            cfg = config_mod.load()
            self.assertEqual(Path(cfg.data_dir), Path(config_mod.APP_DIR) / "data")
            self.assertIsNone(guard.check(cfg, BOARD), "боевой режим заблокирован — плохо")

    def test_allow_env_is_the_only_key(self):
        real = _fake_home(self.base, "hermes-allow")
        with _Env(HERMES_HOME=real, HERMES_KANBAN_HOME=real,
                  TEAMROOM_DATA=self.base / "data-allow", TEAMROOM_VAULT_DIR="off",
                  TEAMROOM_SANDBOX="1"):
            cfg = config_mod.load()
            self.assertIsNotNone(guard.check(cfg, BOARD))
            os.environ[guard.ALLOW_ENV] = "1"
            try:
                self.assertIsNone(guard.check(cfg, BOARD), "явный обход не сработал")
                self.assertFalse(guard.status(cfg)["blocked"])
            finally:
                os.environ.pop(guard.ALLOW_ENV, None)
            os.environ[guard.SANDBOX_ENV] = "1"           # и явный признак песочницы
            try:
                self.assertIsNotNone(guard.check(cfg, BOARD),
                                     "TEAMROOM_SANDBOX без обхода должен запрещать")
            finally:
                os.environ.pop(guard.SANDBOX_ENV, None)


class StatusTextTest(unittest.TestCase):
    """Текст причины понятен человеку: что не так и что сделать."""

    def test_texts(self):
        with _Env():
            quiet = guard.status(None)
            self.assertFalse(quiet["blocked"])
            self.assertTrue(quiet["text"])

    def test_message_has_reason_and_action(self):
        """Текст отказа называет и причину, и действие (иначе это тупик)."""
        message = guard.MESSAGE.format(signs="данные не по умолчанию",
                                       root="дом Hermes", allow=guard.ALLOW_ENV)
        self.assertIn(guard.ALLOW_ENV, message)
        self.assertIn("песочниц", message.lower())
        self.assertIn("Запись в доску отклонена", message)


if __name__ == "__main__":
    unittest.main(verbosity=2)
