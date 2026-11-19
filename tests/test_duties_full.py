"""Дежурства комнаты целиком (W27-E7 §3): мастер, правка, корзина, результат.

Всё на вымышленных данных: поддельный домашний каталог Hermes во временной папке вне
реального `%LOCALAPPDATA%\\hermes`, подставной CLI (реальные кроны не трогаются).

Проверяем то, что человек нажимает и что при этом реально происходит:

* предпросмотр даёт точную команду и ничего не меняет; создание — только по «да»;
* создание/правка/удаление — ровно `hermes cron create|edit|remove|pause|resume <id>`;
* удаление → корзина 30 дней, возврат создаёт задание заново и привязывает к комнате;
* результат запуска (блок `результат`) → тихая строка либо карточка «Принять /
  Изменить / Не надо / Сделать правилом», Дело по принятию, идемпотентность по run_id;
* предложения руководителя (≤ 2 в неделю, отказ запоминается) и диктовка → черновик.

    python -m unittest tests.test_duties_full -v
"""
from __future__ import annotations

import json
import os
import sys
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from server import delo as delo_mod             # noqa: E402
from server import duty_rules as rules_mod      # noqa: E402
from server import duty_templates as templates_mod   # noqa: E402
from server import duties as duties_mod         # noqa: E402
from server import inbox as inbox_mod           # noqa: E402
from tests.test_duties import NOW, DAY, DutiesBase, sample_home   # noqa: E402
from tests.test_duties_actions import _snapshot                 # noqa: E402

ROOM = {"id": "studio", "title": "Сайт студии", "duties": []}

RESULT_CHANGED = """```результат
изменилось: да
сделано: собрал список писем
обнаружено: 2 новых письма от контрагента А
предложения:
  - что: Ответить контрагенту А
    почему: письмо ждёт ответа три дня
    действие: создать дело
ссылки:
  - C:/Work/Rooms/demo/pismo.md
```"""

RESULT_CALM = """```результат
изменилось: нет
сделано: проверил ящик
обнаружено: ничего
предложения: []
ссылки: []
```"""


class FakeCronCli:
    """Подставной `hermes cron …`: пишет в песочный jobs.json, как настоящий."""

    def __init__(self, home: Path, code: int = 0):
        self.home = Path(home)
        self.calls: list[list[str]] = []
        self.code = code
        self.next_id = "jnew00000001"

    # --- файл заданий -----------------------------------------------------
    def _read(self) -> list:
        path = self.home / "cron" / "jobs.json"
        try:
            doc = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []
        return doc.get("jobs") if isinstance(doc.get("jobs"), list) else []

    def _write(self, rows: list) -> None:
        path = self.home / "cron" / "jobs.json"
        path.write_text(json.dumps({"jobs": rows, "updated_at": "2026-11-18T10:00:00+00:00"},
                                   ensure_ascii=False), encoding="utf-8")

    # --- разбор команды ---------------------------------------------------
    def __call__(self, args):
        args = [str(one) for one in args]
        self.calls.append(args)
        if self.code:
            return {"code": self.code, "out": "", "err": "сломалось (подставной CLI)"}
        action = args[1] if len(args) > 1 else ""
        rows = self._read()
        if action == "create":
            job_id = self.next_id
            self.next_id = "jnew%010d" % (int(self.next_id[4:]) + 1)
            rows.append({
                "id": job_id, "name": self._opt(args, "--name") or job_id,
                "prompt": args[2] if len(args) > 2 and not args[2].startswith("--") else "",
                "schedule": {"kind": "cron", "expr": args[2], "display": args[2]},
                "schedule_display": args[2], "enabled": "--paused" not in args,
                "state": "scheduled", "skill": None, "skills": [],
                "script": self._opt(args, "--script"), "no_agent": "--no-agent" in args,
                "monitor_script": self._opt(args, "--monitor-script"),
                "monitor_url": self._opt(args, "--monitor-url"),
                "workdir": self._opt(args, "--workdir"),
                "deliver": self._opt(args, "--deliver"),
                "continuity": "--continuity" in args,
            })
            self._write(rows)
            return {"code": 0, "out": "Created job: %s" % job_id, "err": ""}
        job_id = args[2] if len(args) > 2 else ""
        found = [row for row in rows if str(row.get("id")) == job_id]
        if action == "edit" and found:
            row = found[0]
            if "--schedule" in args:
                value = self._opt(args, "--schedule")
                row["schedule"] = {"kind": "cron", "expr": value, "display": value}
                row["schedule_display"] = value
            if "--prompt" in args:
                row["prompt"] = self._opt(args, "--prompt")
            if "--name" in args:
                row["name"] = self._opt(args, "--name")
            self._write(rows)
            return {"code": 0, "out": "Updated job: %s" % job_id, "err": ""}
        if action in ("pause", "resume") and found:
            found[0]["enabled"] = action == "resume"
            self._write(rows)
            return {"code": 0, "out": "%s job: %s" % (action, job_id), "err": ""}
        if action == "remove":
            self._write([row for row in rows if str(row.get("id")) != job_id])
            return {"code": 0, "out": "Removed job: %s" % job_id, "err": ""}
        if action == "run":
            return {"code": 0, "out": "Triggered job: %s" % job_id, "err": ""}
        return {"code": 0, "out": "ok", "err": ""}

    @staticmethod
    def _opt(args: list, flag: str) -> str:
        if flag in args:
            index = args.index(flag)
            if index + 1 < len(args):
                return args[index + 1]
        return ""


def wizard(**over) -> dict:
    """Поля мастера по умолчанию: слежение за ящиком, карточка при изменении."""
    data = {"title": "Проверка ящика", "kind": "monitor", "schedule": "*/30 * * * *",
            "prompt": "Проверь ящик комнаты и назови новые письма.",
            "monitor_script": "room-mail.sh", "changed_rule": "Появились новые письма",
            "on_change": "inbox", "level": "propose", "notify": ["today"], "template": "mail",
            "continuity": True}
    data.update(over)
    return data


class FullBase(DutiesBase):
    """Песочный дом Hermes + подставной CLI + комната с пустым списком дежурств."""

    def setUp(self):
        super().setUp()
        self.fake = sample_home(self.tmp)
        self.fake.write()
        self.cfg = self.fake.cfg(self.data)
        # «Дела» (D5) хранятся в cfg.state_dir — в песочнице это папка данных теста.
        self.cfg.state_dir = Path(self.data) / "state"
        self.cli = FakeCronCli(self.fake.home)
        duties_mod.set_cli_runner(self.cli)
        self._dry = os.environ.pop("TEAMROOM_DUTIES_DRY", None)
        self.write_rooms()

    def tearDown(self):
        duties_mod.reset_cli_runner()
        if self._dry is not None:
            os.environ["TEAMROOM_DUTIES_DRY"] = self._dry
        super().tearDown()

    def write_rooms(self, duties=None):
        (self.data / "rooms.json").write_text(json.dumps(
            {"rooms": [{"id": "studio", "title": "Сайт студии", "orchestrator": "anna-lead",
                        "members": [{"profile": "ivan-petrov"}],
                        "duties": list(duties or []), "created_at": 1}],
             "updated_at": NOW}, ensure_ascii=False), encoding="utf-8")

    def room(self) -> dict:
        return {"id": "studio", "title": "Сайт студии",
                "duties": list(self.rooms_now()["rooms"][0].get("duties") or [])}

    def cron_snapshot(self):
        return _snapshot(self.fake.home / "cron")

    def created(self, **over) -> dict:
        """Создать дежурство (как кнопка «Создать» после предпросмотра)."""
        return duties_mod.create(self.cfg, self.room(), wizard(**over), confirm=True)

    def add_output(self, job_id: str, body: str, stamp: int | None = None) -> None:
        self.fake.output(job_id, int(stamp or (NOW - 60)), body)


# ------------------------------------------------------------------ шаблоны

class TemplatesTests(FullBase):
    def test_seven_universal_templates(self):
        data = templates_mod.list_templates()
        ids = [one["id"] for one in data["templates"]]
        self.assertEqual(ids, ["mail", "folder", "morning", "page", "deadlines",
                               "weekly", "steps"])
        for one in data["templates"]:
            self.assertTrue(one["title"] and one["why"])
            self.assertTrue(one["wizard"]["schedule"])
            self.assertEqual(one["wizard"]["on_change"] in rules_mod.ON_CHANGE, True)
            self.assertEqual(one["wizard"]["level"] in rules_mod.LEVELS, True)

    def test_template_texts_are_universal(self):
        """Никаких названий конкретного бизнеса и реальных путей в шаблонах."""
        blob = json.dumps(templates_mod.list_templates(), ensure_ascii=False).lower()
        for bad in ("максим", "гранит", "контур", "taskflow", "локальная папка пользователя"):
            self.assertNotIn(bad, blob)

    def test_unknown_template_is_404(self):
        with self.assertRaises(templates_mod.TemplateError) as ctx:
            templates_mod.template("nosuch")
        self.assertEqual(ctx.exception.http, 404)


# ----------------------------------------------------- предпросмотр и создание

class PreviewTests(FullBase):
    def test_preview_shows_exact_command_and_changes_nothing(self):
        before = self.cron_snapshot()
        view = duties_mod.preview(self.cfg, self.room(), wizard())
        self.assertTrue(view["preview"])
        self.assertIn('cron create "*/30 * * * *"', view["command"])
        self.assertIn("--name Проверка ящика", view["command"].replace('"', ""))
        self.assertIn("--deliver room:studio", view["command"])
        self.assertIn("--monitor-script room-mail.sh", view["command"])
        self.assertIn("--continuity", view["command"])
        self.assertTrue(any("Расписание: каждые 30 минут" in line for line in view["changes"]))
        self.assertEqual(view["argv"][0:3], ["cron", "create", "*/30 * * * *"])
        self.assertEqual(self.cli.calls, [])
        self.assertEqual(before, self.cron_snapshot())     # в Hermes ничего не писали
        self.assertFalse((self.data / "state" / "studio" / "duties.json").exists())

    def test_create_requires_confirm(self):
        with self.assertRaises(duties_mod.DutiesError) as ctx:
            duties_mod.create(self.cfg, self.room(), wizard())
        self.assertEqual((ctx.exception.http, ctx.exception.code), (409, "confirm_required"))
        self.assertIn("command", ctx.exception.detail)
        self.assertEqual(self.cli.calls, [])

    def test_create_preview_true_does_not_call_cli(self):
        view = duties_mod.create(self.cfg, self.room(), wizard(), preview_only=True)
        self.assertTrue(view["preview"])
        self.assertEqual(self.cli.calls, [])

    def test_create_calls_exact_cli_and_binds(self):
        made = self.created()
        self.assertTrue(made["applied"])
        self.assertEqual(len(self.cli.calls), 1)
        argv = self.cli.calls[0]
        self.assertEqual(argv[0:3], ["cron", "create", "*/30 * * * *"])
        self.assertIn("--name", argv)
        self.assertIn("room:studio", argv)
        self.assertIn("--monitor-script", argv)
        self.assertIn("--deliver", argv)
        self.assertIn("```результат", argv[3])            # задание с контрактом результата
        job_id = made["job_id"]
        self.assertTrue(job_id)
        self.assertIn(job_id, self.rooms_now()["rooms"][0]["duties"])
        rule = rules_mod.rule(self.cfg, "studio", job_id)
        self.assertEqual(rule["on_change"], "inbox")
        self.assertEqual(rule["level"], "propose")
        self.assertEqual(rule["template"], "mail")
        self.assertEqual(rule["source"], "manual")
        self.assertTrue(any(str(job.get("id")) == job_id for job in duties_mod.jobs(self.cfg)))

    def test_create_script_kind(self):
        made = self.created(title="Дешёвая проверка", kind="script", script="room-folder.sh",
                            prompt="", monitor_script="", schedule="every 1h")
        argv = self.cli.calls[0]
        self.assertIn("--script", argv)
        self.assertIn("room-folder.sh", argv)
        self.assertIn("--no-agent", argv)
        self.assertEqual(made["job_id"] != "", True)

    def test_bad_schedule_and_fields(self):
        for bad in ("", "иногда", "0 9 * *", "every", "*/9999999 * * * * x"):
            with self.assertRaises(duties_mod.DutiesError):
                duties_mod.normalize_schedule(bad)
        with self.assertRaises(duties_mod.DutiesError):
            self.created(title="")
        with self.assertRaises(duties_mod.DutiesError) as ctx:
            self.created(script="../evil.sh", kind="script", prompt="")
        self.assertEqual(ctx.exception.http, 400)
        with self.assertRaises(duties_mod.DutiesError):
            self.created(kind="monitor", monitor_script="", monitor_url="", prompt="x")
        with self.assertRaises(duties_mod.DutiesError):
            self.created(workdir="папка/без/полного/пути")
        self.assertEqual(self.cli.calls, [])

    def test_cli_failure_is_honest(self):
        self.cli.code = 3
        with self.assertRaises(duties_mod.DutiesError) as ctx:
            self.created()
        self.assertEqual((ctx.exception.http, ctx.exception.code), (502, "cli_failed"))
        self.assertEqual(self.rooms_now()["rooms"][0]["duties"], [])


# ------------------------------------------------------------- правка и пауза

class PatchTests(FullBase):
    def setUp(self):
        super().setUp()
        self.made = self.created()
        self.job = self.made["job_id"]
        self.cli.calls.clear()

    def test_patch_schedule_needs_confirm(self):
        with self.assertRaises(duties_mod.DutiesError) as ctx:
            duties_mod.patch(self.cfg, self.job, self.room(), {"schedule": "0 9 * * 1-5"})
        self.assertEqual(ctx.exception.code, "confirm_required")
        self.assertIn("--schedule", ctx.exception.detail["command"])
        self.assertEqual(self.cli.calls, [])

    def test_patch_schedule_calls_edit(self):
        out = duties_mod.patch(self.cfg, self.job, self.room(),
                               {"schedule": "0 9 * * 1-5", "confirm": True}, confirm=True)
        self.assertTrue(out["applied"])
        self.assertEqual(self.cli.calls, [["cron", "edit", self.job, "--schedule", "0 9 * * 1-5"]])
        row = [job for job in duties_mod.jobs(self.cfg) if str(job["id"]) == self.job][0]
        self.assertEqual(row["schedule_display"], "0 9 * * 1-5")

    def test_patch_rule_only_does_not_touch_hermes(self):
        before = self.cron_snapshot()
        out = duties_mod.patch(self.cfg, self.job, self.room(),
                               {"level": "self", "changed_rule": "Новое письмо"})
        self.assertTrue(out["applied"])
        self.assertEqual(self.cli.calls, [])
        self.assertEqual(before, self.cron_snapshot())
        rule = rules_mod.rule(self.cfg, "studio", self.job)
        self.assertEqual(rule["level"], "self")
        self.assertEqual(rule["changed_rule"], "Новое письмо")

    def test_patch_pause_and_resume(self):
        duties_mod.patch(self.cfg, self.job, self.room(), {"pause": True, "confirm": True},
                         confirm=True)
        self.assertEqual(self.cli.calls[-1], ["cron", "pause", self.job])
        duties_mod.patch(self.cfg, self.job, self.room(), {"pause": False, "confirm": True},
                         confirm=True)
        self.assertEqual(self.cli.calls[-1], ["cron", "resume", self.job])

    def test_patch_nothing_to_change(self):
        out = duties_mod.patch(self.cfg, self.job, self.room(), {"schedule": "*/30 * * * *"})
        self.assertFalse(out["applied"])
        self.assertEqual(self.cli.calls, [])
        self.assertIn("Менять нечего", out["text"])


# --------------------------------------------------------- корзина и возврат

class TrashTests(FullBase):
    def setUp(self):
        super().setUp()
        self.made = self.created()
        self.job = self.made["job_id"]
        self.cli.calls.clear()

    def test_delete_needs_confirm_and_saves_to_trash(self):
        with self.assertRaises(duties_mod.DutiesError) as ctx:
            duties_mod.remove(self.cfg, self.job, self.room())
        self.assertEqual(ctx.exception.code, "confirm_required")
        self.assertEqual(self.cli.calls, [])
        out = duties_mod.remove(self.cfg, self.job, self.room(), confirm=True)
        self.assertTrue(out["applied"])
        self.assertEqual(self.cli.calls, [["cron", "remove", self.job]])
        self.assertEqual(self.rooms_now()["rooms"][0]["duties"], [])
        trash = rules_mod.trash(self.cfg, "studio")
        self.assertEqual(len(trash["items"]), 1)
        self.assertEqual(trash["items"][0]["job_id"], self.job)
        self.assertEqual(trash["items"][0]["days_left"], 30)
        self.assertNotIn(self.job, rules_mod.load(self.cfg, "studio")["rules"])

    def test_restore_creates_job_again_and_binds(self):
        duties_mod.remove(self.cfg, self.job, self.room(), confirm=True)
        self.cli.calls.clear()
        with self.assertRaises(duties_mod.DutiesError) as ctx:
            duties_mod.restore(self.cfg, self.room(), self.job)
        self.assertEqual(ctx.exception.code, "confirm_required")
        self.assertEqual(self.cli.calls, [])
        self.assertEqual(len(rules_mod.trash(self.cfg, "studio")["items"]), 1)  # не потеряли
        out = duties_mod.restore(self.cfg, self.room(), self.job, confirm=True)
        self.assertTrue(out["applied"])
        self.assertEqual(self.cli.calls[0][0:2], ["cron", "create"])
        self.assertIn(out["job_id"], self.rooms_now()["rooms"][0]["duties"])
        self.assertEqual(rules_mod.trash(self.cfg, "studio")["items"], [])
        self.assertIsNotNone(rules_mod.rule(self.cfg, "studio", out["job_id"]))

    def test_restore_unknown_is_404(self):
        with self.assertRaises(duties_mod.DutiesError) as ctx:
            duties_mod.restore(self.cfg, self.room(), "jnotthere01", confirm=True)
        self.assertEqual(ctx.exception.http, 404)


# ------------------------------------------------------------ пробный прогон

class DryRunTests(FullBase):
    def test_dry_run_shows_what_it_would_find_without_running(self):
        made = self.created()
        self.add_output(made["job_id"], RESULT_CHANGED)
        self.cli.calls.clear()
        before = self.cron_snapshot()
        out = duties_mod.dry_run(self.cfg, made["job_id"])
        self.assertFalse(out["ran"])
        self.assertEqual(out["side_effects"], "none")
        self.assertEqual(self.cli.calls, [])
        self.assertEqual(before, self.cron_snapshot())
        self.assertEqual(out["checks"]["kind"], "monitor")
        self.assertEqual(out["checks"]["monitor_script"], "room-mail.sh")
        self.assertTrue(out["last_result"]["changed"])
        self.assertIn("2 новых письма", out["last_result"]["found"])


# ------------------------------------------------- результат запуска → дело/карточка

class ProcessTests(FullBase):
    def setUp(self):
        super().setUp()
        self.made = self.created()
        self.job = self.made["job_id"]

    def test_no_change_is_silent_history_row(self):
        self.add_output(self.job, RESULT_CALM)
        stats = rules_mod.process_runs(self.cfg, self.room())
        self.assertEqual(stats["processed"], 1)
        self.assertEqual(stats["cards"], 0)
        rows = rules_mod.history(self.cfg, "studio", self.job)
        self.assertEqual(len(rows), 1)
        self.assertFalse(rows[0]["changed"])
        self.assertTrue(rows[0]["silent"])
        self.assertEqual(inbox_mod.items(self.cfg, "studio")["items"], [])

    def test_change_makes_inbox_card_with_four_buttons(self):
        self.add_output(self.job, RESULT_CHANGED)
        stats = rules_mod.process_runs(self.cfg, self.room())
        self.assertEqual((stats["processed"], stats["cards"]), (1, 1))
        listing = inbox_mod.items(self.cfg, "studio")["items"]
        self.assertEqual(len(listing), 1)
        card = listing[0]
        self.assertEqual(card["source"], "duty")
        self.assertIn("Что выполнено", card["text"])
        self.assertIn("Что обнаружено", card["text"])
        self.assertIn("Предлагаю", card["text"])
        self.assertEqual([one["id"] for one in card["actions"]],
                         ["accept", "edit", "reject", "make_rule", "delete"])
        self.assertEqual(card["duty"]["job_id"], self.job)
        self.assertEqual(card["duty"]["proposals"][0]["what"], "Ответить контрагенту А")
        self.assertEqual(listing[0]["proposal"]["state"], "waiting")
        self.assertEqual(inbox_mod.counts(inbox_mod.load(self.cfg, "studio")[0])["proposals"], 1)

    def test_process_runs_is_idempotent_by_run_id(self):
        self.add_output(self.job, RESULT_CHANGED)
        first = rules_mod.process_runs(self.cfg, self.room())
        second = rules_mod.process_runs(self.cfg, self.room())
        self.assertEqual(first["processed"], 1)
        self.assertEqual(second["processed"], 0)
        self.assertEqual(len(inbox_mod.items(self.cfg, "studio")["items"]), 1)
        self.assertEqual(len(rules_mod.history(self.cfg, "studio", self.job)), 1)

    def test_self_level_creates_delo_without_card(self):
        rules_mod.set_rule(self.cfg, "studio", self.job,
                           {"on_change": "delo", "level": "self", "changed_rule": "новое письмо"})
        self.add_output(self.job, RESULT_CHANGED)
        stats = rules_mod.process_runs(self.cfg, self.room())
        self.assertEqual(stats["self"], 1)
        self.assertEqual(stats["cards"], 0)
        delos = delo_mod.list_delos(self.cfg, "studio")["items"]
        self.assertEqual(len(delos), 1)
        self.assertIn("собрал список писем", delos[0]["title"])
        self.assertEqual([one["job_id"] for one in delos[0]["duties"]], [self.job])
        self.assertEqual(inbox_mod.items(self.cfg, "studio")["items"], [])

    def test_propose_level_only_suggests_delo(self):
        rules_mod.set_rule(self.cfg, "studio", self.job,
                           {"on_change": "delo", "level": "propose"})
        self.add_output(self.job, RESULT_CHANGED)
        rules_mod.process_runs(self.cfg, self.room())
        self.assertEqual(delo_mod.list_delos(self.cfg, "studio")["items"], [])
        self.assertEqual(len(inbox_mod.items(self.cfg, "studio")["items"]), 1)

    def test_log_rule_writes_history_only(self):
        rules_mod.set_rule(self.cfg, "studio", self.job, {"on_change": "log"})
        self.add_output(self.job, RESULT_CHANGED)
        stats = rules_mod.process_runs(self.cfg, self.room())
        self.assertEqual(stats["logs"], 1)
        self.assertEqual(inbox_mod.items(self.cfg, "studio")["items"], [])
        self.assertEqual(len(rules_mod.history(self.cfg, "studio", self.job)), 1)

    def test_output_without_block_is_silent(self):
        self.add_output(self.job, "Просто текст ответа без машинного блока.")
        stats = rules_mod.process_runs(self.cfg, self.room())
        self.assertEqual((stats["processed"], stats["silent"]), (1, 1))
        self.assertEqual(inbox_mod.items(self.cfg, "studio")["items"], [])

    def test_limit_five_per_tick(self):
        for index in range(7):
            self.add_output(self.job, RESULT_CHANGED, stamp=NOW - 600 + index * 10)
        stats = rules_mod.process_runs(self.cfg, self.room())
        self.assertEqual(stats["processed"], 5)
        self.assertEqual(stats["skipped"], 2)
        self.assertEqual(len(inbox_mod.items(self.cfg, "studio")["items"]), 5)

    def test_unbound_job_is_ignored(self):
        self.fake.output("j_daily", NOW - 60, RESULT_CHANGED)
        stats = rules_mod.process_runs(self.cfg, self.room())
        self.assertEqual(stats["processed"], 0)


# --------------------------------------------------- решения по карточке

class CardDecisionTests(FullBase):
    def setUp(self):
        super().setUp()
        self.made = self.created()
        self.job = self.made["job_id"]
        self.add_output(self.job, RESULT_CHANGED)
        rules_mod.process_runs(self.cfg, self.room())
        self.card = inbox_mod.items(self.cfg, "studio")["items"][0]

    def test_accept_creates_delo_and_closes_card(self):
        out = rules_mod.decide_card(self.cfg, self.room(), self.card["id"], "accept")
        self.assertTrue(out["ok"])
        self.assertEqual(out["item"]["status"], "processed")
        self.assertEqual(out["counts"]["proposals"], 0)
        delos = delo_mod.list_delos(self.cfg, "studio")["items"]
        self.assertEqual(len(delos), 1)
        self.assertEqual(out["item"]["resolution"]["delo_id"], delos[0]["id"])
        with self.assertRaises(rules_mod.DutyRuleError) as ctx:
            rules_mod.decide_card(self.cfg, self.room(), self.card["id"], "accept")
        self.assertEqual(ctx.exception.code, "already_decided")

    def test_make_rule_switches_rule_to_self(self):
        out = rules_mod.decide_card(self.cfg, self.room(), self.card["id"], "make_rule")
        self.assertEqual(out["item"]["status"], "processed")
        self.assertEqual(rules_mod.rule(self.cfg, "studio", self.job)["level"], "self")

    def test_reject_and_edit(self):
        edited = rules_mod.decide_card(self.cfg, self.room(), self.card["id"], "edit",
                                       {"text": "Сначала уточнить у контрагента А."})
        self.assertIn("уточнить", edited["item"]["proposal"]["body"])
        rejected = rules_mod.decide_card(self.cfg, self.room(), self.card["id"], "reject")
        self.assertEqual(rejected["item"]["status"], "processed")
        self.assertEqual(rejected["item"]["proposal"]["state"], "rejected")

    def test_unknown_card_and_action(self):
        with self.assertRaises(rules_mod.DutyRuleError) as ctx:
            rules_mod.decide_card(self.cfg, self.room(), "in_nosuch", "accept")
        self.assertEqual(ctx.exception.http, 404)
        with self.assertRaises(rules_mod.DutyRuleError):
            rules_mod.decide_card(self.cfg, self.room(), self.card["id"], "взлететь")


# ------------------------------------------------------- предложения и диктовка

class ProposalTests(FullBase):
    def test_leader_proposals_limited_to_two_per_week(self):
        rules_mod.propose(self.cfg, "studio", {"title": "Утро", "what": "обзор",
                                               "source": "manager"})
        rules_mod.propose(self.cfg, "studio", {"title": "Вечер", "what": "итог",
                                               "source": "manager"})
        with self.assertRaises(rules_mod.DutyRuleError) as ctx:
            rules_mod.propose(self.cfg, "studio", {"title": "Ночь", "what": "сон",
                                                   "source": "manager"})
        self.assertEqual(ctx.exception.code, "too_many_proposals")
        self.assertEqual(len(rules_mod.proposals(self.cfg, "studio")["items"]), 2)

    def test_never_remembers_the_decline(self):
        made = rules_mod.propose(self.cfg, "studio", {"title": "Проверять ящик",
                                                      "what": "письма", "source": "manager"})
        pid = made["proposal"]["id"]
        rules_mod.act_proposal(self.cfg, "studio", pid, "never")
        again = rules_mod.propose(self.cfg, "studio", {"title": "Проверять ящик",
                                                       "what": "письма", "source": "manager"})
        self.assertTrue(again["skipped"])
        self.assertEqual(again["reason"], "declined")

    def test_accept_proposal_returns_wizard_draft(self):
        made = rules_mod.propose(self.cfg, "studio", {"title": "Утренний обзор",
                                                      "what": "что было вчера",
                                                      "schedule": "0 8 * * 1-5",
                                                      "source": "manager"})
        out = rules_mod.act_proposal(self.cfg, "studio", made["proposal"]["id"], "create")
        self.assertEqual(out["draft"]["title"], "Утренний обзор")
        self.assertEqual(out["draft"]["schedule"], "0 8 * * 1-5")
        self.assertIn("POST /api/rooms/studio/duties", out["next"])
        self.assertEqual(self.cli.calls, [])          # крон не создан — только черновик
        self.assertEqual(rules_mod.proposals(self.cfg, "studio")["items"], [])

    def test_dictation_draft_becomes_proposal_once(self):
        item = inbox_mod.add(self.cfg, "studio", "Это будем делать постоянно: проверять ящик",
                             "dictation")
        inbox_mod.act(self.cfg, "studio", item["id"], "to_duty")
        inbox_mod.act(self.cfg, "studio", item["id"], "accept")      # заготовка duties_drafts
        first = rules_mod.sync_inbox_drafts(self.cfg, "studio")
        second = rules_mod.sync_inbox_drafts(self.cfg, "studio")
        self.assertEqual((first["made"], second["made"]), (1, 0))
        listed = rules_mod.proposals(self.cfg, "studio")["items"]
        self.assertEqual(len(listed), 1)
        self.assertEqual(listed[0]["source"], "dictation")
        self.assertEqual(self.cli.calls, [])          # без «Создать» крон не появляется

    def test_from_delo_makes_proposal(self):
        made = delo_mod.create(self.cfg, self.room(), {"title": "Проверить ящик"})
        out = rules_mod.from_delo(self.cfg, "studio", made["id"])
        self.assertFalse(out["skipped"])
        self.assertEqual(out["proposal"]["source"], "delo")
        self.assertIn("Проверить ящик", out["proposal"]["title"])


# --------------------------------------------------------------- разбор блока

class ParseResultTests(unittest.TestCase):
    def test_parses_changed_block(self):
        data = rules_mod.parse_result(RESULT_CHANGED)
        self.assertTrue(data["changed"])
        self.assertEqual(data["done"], "собрал список писем")
        self.assertEqual(data["found"], "2 новых письма от контрагента А")
        self.assertEqual(data["proposals"], [{"what": "Ответить контрагенту А",
                                              "why": "письмо ждёт ответа три дня",
                                              "action": "создать дело"}])
        self.assertEqual(data["links"], ["C:/Work/Rooms/demo/pismo.md"])

    def test_no_block_returns_none(self):
        self.assertIsNone(rules_mod.parse_result("обычный ответ без блока"))
        self.assertIsNone(rules_mod.parse_result(""))

    def test_changed_no_is_false(self):
        data = rules_mod.parse_result(RESULT_CALM)
        self.assertFalse(data["changed"])

    def test_unknown_changed_is_none(self):
        data = rules_mod.parse_result("```результат\nизменилось: может быть\n```")
        self.assertIsNone(data["changed"])

    def test_english_keys(self):
        data = rules_mod.parse_result(
            "```result\nchanged: yes\ndone: checked mailbox\nlinks:\n  - C:/Work/Rooms/demo\n```")
        self.assertTrue(data["changed"])
        self.assertEqual(data["done"], "checked mailbox")


if __name__ == "__main__":
    unittest.main(verbosity=2)
