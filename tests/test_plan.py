"""Карта проекта (волна 9): проверка, очередь, запуск, правки, HTTP (§1–§4, §7).

Всё в песочнице: ``TEAMROOM_DATA``/``TEAMROOM_VAULT_DIR``/``TEAMROOM_HERMES_HOME``
указывают на временный каталог, запись в реальный Hermes/Vault и запуск агентов
запрещены (``server.safety``). Под-запросы не создаются по-настоящему: движок
записи подменён (``SimAgents``-стиль), поэтому тест не будит ни одного агента.

Что проверяем (§7 ТЗ): валидацию (циклы, лимит 8, чужие исполнители), очередь
(зависимости, ``max_parallel_streams``, ``waiting_quota``), ``approve`` (ровно N
под-запросов и без дублей при повторе — идемпотентность по ``stream.id``),
``apply_patch`` (A6: авто/с подтверждением), битый и пустой блок ``plan``,
обратную совместимость, атомарную запись, параллельные ``tick`` и маршруты.
"""
from __future__ import annotations

import http.client
import json
import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from server import config, plan, registry, rooms  # noqa: E402

_GUARDS: list = []


def setUpModule():
    """Никаких записей в реальный Hermes/Vault и никаких агентских процессов."""
    from server import safety

    _GUARDS.extend([safety.forbid_real_writes(), safety.forbid_real_subprocess()])
    for cm in _GUARDS:
        cm.__enter__()


def tearDownModule():
    while _GUARDS:
        cm = _GUARDS.pop()
        try:
            cm.__exit__(None, None, None)
        except Exception:  # noqa: BLE001 - снятие патчей не должно ронять уборку
            pass


ORCH = "lead"
WORKERS = ("worker-a", "worker-b", "worker-c")
ALL = (ORCH,) + WORKERS


def mkstream(title, owner="worker-a", **extra):
    data = {"title": title, "goal": "Цель направления «%s»" % title,
            "owner": owner, "members": [], "depends_on": [], "due": None}
    data.update(extra)
    return data


class FakeEngine:
    """Подмена ``engine_write``: «создаёт» под-запросы в памяти.

    ``create`` имитирует ``engine_write.create_request`` (в том числе ключ
    идемпотентности по ``stream.id``), ``summary``/``detail`` — чтение дерева
    карточки движком. Ничего не пишет на диск и не запускает агентов.
    """

    def __init__(self):
        self.created: list = []
        self.calls = 0
        self.by_stream: dict = {}
        self.status: dict = {}          # rid -> статус запроса
        self.quota: set = set()         # rid'ы, у которых дети упали на лимит
        self.delay = 0.0
        self.fail_with = None
        self._lock = threading.Lock()

    def create(self, cfg, room, stream, active_plan):
        if self.fail_with is not None:
            raise plan.PlanError(self.fail_with, "conflict")
        if self.delay:
            time.sleep(self.delay)
        sid = stream.get("id")
        with self._lock:
            self.calls += 1
            if sid in self.by_stream:                     # идемпотентность по stream.id
                return {"root_id": self.by_stream[sid]}
            rid = "t_%08x" % (0xa1000000 + self.calls)
            self.by_stream[sid] = rid
            self.created.append(sid)
            self.status.setdefault(rid, "working")
        return {"root_id": rid}

    def summary(self, cfg, room, rid):
        return {"id": rid, "status": self.status.get(rid, "working")}

    def detail(self, cfg, room, rid):
        if rid in self.quota:
            return {"tree": [{"title": "Срез", "status": "failed",
                              "error": {"code": "provider", "message": "HTTP 429: usage limit"}}]}
        return {"tree": []}

    def finish(self, stream_id):
        self.status[self.by_stream[stream_id]] = "done"

    def rid(self, stream_id):
        return self.by_stream[stream_id]


class PlanBase(unittest.TestCase):
    """Общая песочница: конфиг во временном каталоге + комната проекта."""

    max_parallel = 2

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="tr-plan-")
        self.tmp = Path(self._tmp.name)
        self._saved = {k: os.environ.get(k) for k in
                       ("TEAMROOM_DATA", "TEAMROOM_VAULT_DIR", "TEAMROOM_HERMES_HOME")}
        os.environ.update({
            "TEAMROOM_DATA": str(self.tmp / "data"),
            "TEAMROOM_VAULT_DIR": str(self.tmp / "vault"),
            "TEAMROOM_HERMES_HOME": str(self.tmp / "hermes"),
        })
        self.cfg = config.load()
        self.cfg.ensure_dirs()
        self.room = self.make_room()
        self.fake = FakeEngine()
        self._patches = [
            mock.patch.object(plan, "_create_request", self.fake.create),
            mock.patch.object(plan, "_summary", self.fake.summary),
            mock.patch.object(plan, "_detail", self.fake.detail),
        ]
        for patch in self._patches:
            patch.start()

    def tearDown(self):
        for patch in reversed(self._patches):
            patch.stop()
        for key, old in self._saved.items():
            if old is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = old
        self._tmp.cleanup()

    def make_room(self, **extra):
        data = {"title": "Моти", "orchestrator": ORCH,
                "members": [{"profile": p} for p in WORKERS],
                "max_parallel_streams": self.max_parallel,
                # W17-R §3: у НОВОЙ комнаты режим «сразу, без подтверждения».
                # Здесь проверяется ручной путь («Сначала показать план»), поэтому
                # режим задаём явно, как это делает человек в настройках комнаты.
                "launch_mode": "plan_first"}
        data.update(extra)
        return rooms.create_room(self.cfg, data)

    # --- помощники --------------------------------------------------------
    def ingest(self, streams, root="t_brief001", room=None):
        return plan.ingest_plan(self.cfg, self.room["id"], root,
                                {"plan": {"streams": streams}}, room=room or self.room)

    def approve(self, streams="all"):
        return plan.approve(self.cfg, self.room["id"], streams, room=self.room)

    def tick(self):
        return plan.tick(self.cfg, self.room["id"], room=self.room)

    def current(self):
        return plan.load(self.cfg, self.room["id"])

    def by_id(self, stream_id):
        for stream in self.current()["streams"]:
            if stream["id"] == stream_id:
                return stream
        raise AssertionError("нет направления %s" % stream_id)


# ------------------------------------------------------------------ проверка
class ValidationTests(PlanBase):
    def test_ids_are_assigned_and_unknown_fields_dropped(self):
        result = plan.validate_plan(self.room, [
            mkstream("Рецептура моти", secret="утечка", request_id="t_12345678"),
            mkstream("Упаковка и бренд", owner="worker-b"),
        ])
        self.assertEqual([s["id"] for s in result["streams"]], ["s1", "s2"])
        self.assertEqual(result["warnings"], [])
        for stream in result["streams"]:
            self.assertNotIn("secret", stream)
            self.assertIsNone(stream["request_id"])
            self.assertEqual(stream["status"], "proposed")
            self.assertEqual(stream["created_by"], "manager")

    def test_eight_streams_limit(self):
        result = plan.validate_plan(self.room, [mkstream("Направление %d" % i)
                                                for i in range(1, 11)])
        self.assertEqual(len(result["streams"]), plan.MAX_STREAMS)
        self.assertTrue(any("восьм" in w for w in result["warnings"]), result["warnings"])

    def test_cycle_is_broken_with_warning(self):
        result = plan.validate_plan(self.room, [
            mkstream("Первое", depends_on=["s2"]),
            mkstream("Второе", owner="worker-b", depends_on=["s1"]),
        ])
        deps = {s["id"]: s["depends_on"] for s in result["streams"]}
        self.assertTrue(any("цикл" in w.lower() for w in result["warnings"]), result["warnings"])
        for sid, values in deps.items():
            for dep in values:
                self.assertNotIn(dep, deps)

    def test_foreign_people_are_rejected_or_removed(self):
        result = plan.validate_plan(self.room, [
            mkstream("Чужое", owner="stranger"),
            mkstream("Своё", owner="worker-a", members=["worker-b", "stranger"]),
        ])
        titles = [s["title"] for s in result["streams"]]
        self.assertEqual(titles, ["Своё"])
        self.assertEqual(result["streams"][0]["members"], ["worker-b"])
        joined = " ".join(result["warnings"])
        self.assertIn("stranger", joined)
        self.assertIn("не в составе комнаты", joined)

    def test_short_title_and_bad_due(self):
        result = plan.validate_plan(self.room, [
            mkstream("ok"),
            {"title": "ab"},
            mkstream("Срок", due="2026-13-40"),
            mkstream("Нормальный срок", owner="worker-b", due="2026-11-20"),
        ])
        titles = [s["title"] for s in result["streams"]]
        self.assertNotIn("ab", titles)
        due = {s["title"]: s["due"] for s in result["streams"]}
        self.assertIsNone(due["Срок"])
        self.assertEqual(due["Нормальный срок"], "2026-11-20")
        self.assertTrue(any("не похож на дату" in w for w in result["warnings"]))

    def test_unknown_dependency_is_removed(self):
        result = plan.validate_plan(self.room, [mkstream("Один", depends_on=["s9", "s1"])])
        self.assertEqual(result["streams"][0]["depends_on"], [])
        self.assertTrue(any("не найдена" in w for w in result["warnings"]))

    def test_empty_and_broken_blocks_are_not_errors(self):
        self.assertEqual(plan.validate_plan(self.room, None), {"streams": [], "warnings": []})
        broken = plan.validate_plan(self.room, "не список")
        self.assertEqual(broken["streams"], [])
        self.assertTrue(broken["warnings"])

    def test_room_without_profiles_accepts_any_owner(self):
        """Старая комната без состава не должна терять все направления."""
        result = plan.validate_plan({"id": "old"}, [mkstream("Что-то", owner="x")])
        self.assertEqual(len(result["streams"]), 1)


# -------------------------------------------------------------------- ingest
class IngestTests(PlanBase):
    def test_ingest_saves_draft_and_logs(self):
        res = self.ingest([mkstream("Рецептура"), mkstream("Упаковка", owner="worker-b")])
        self.assertTrue(res["changed"])
        stored = self.current()
        self.assertEqual(stored["status"], "draft")
        self.assertEqual(stored["brief_root_id"], "t_brief001")
        self.assertEqual([s["status"] for s in stored["streams"]], ["proposed", "proposed"])
        logs = [(item["kind"], item["text"]) for item in stored["log"]]
        self.assertEqual(logs[-1][0], "created")
        # W17-R §3 (B-10): единый понятный термин — «части работы», не «направления».
        self.assertIn("частей работы — 2", logs[-1][1])

    def test_empty_or_broken_block_changes_nothing(self):
        self.ingest([mkstream("Рецептура")])
        before = self.current()
        self.assertEqual(plan.ingest_plan(self.cfg, self.room["id"], "t_brief001",
                                          {}, room=self.room)["skipped"], "no_block")
        broken = plan.ingest_plan(self.cfg, self.room["id"], "t_brief001",
                                  {"plan": {"streams": "ой"}}, room=self.room)
        self.assertFalse(broken["changed"])
        self.assertTrue(broken["warnings"])
        self.assertEqual(self.current()["streams"], before["streams"])

    def test_started_plan_is_not_overwritten(self):
        self.ingest([mkstream("Рецептура")])
        self.approve()
        res = self.ingest([mkstream("Совсем другое")])
        self.assertEqual(res["skipped"], "plan_started")
        self.assertEqual([s["title"] for s in self.current()["streams"]], ["Рецептура"])

    def test_project_mode_off_is_skipped(self):
        room = self.make_room(title="Старая", project_mode=False)
        res = plan.ingest_plan(self.cfg, room["id"], "t_brief001",
                               {"plan": {"streams": [mkstream("Рецептура")]}}, room=room)
        self.assertEqual(res["skipped"], "project_mode_off")
        self.assertIsNone(plan.load(self.cfg, room["id"]))


# ----------------------------------------------------------- approve и очередь
class QueueTests(PlanBase):
    def test_approve_creates_exactly_n_and_is_idempotent(self):
        self.ingest([mkstream("Рецептура"), mkstream("Упаковка", owner="worker-b"),
                     mkstream("Цена", owner="worker-c")])
        first = self.approve()
        self.assertEqual(first["started"], ["s1", "s2"])        # max_parallel_streams = 2
        self.assertEqual(self.fake.calls, 2)
        second = self.approve()
        self.assertEqual(second["started"], [])
        self.assertEqual(self.fake.calls, 2)                    # повтор не создаёт дублей
        self.assertEqual(sorted(self.fake.created), ["s1", "s2"])

    def test_dependencies_hold_the_queue(self):
        self.ingest([mkstream("Рецептура"),
                     mkstream("Упаковка", owner="worker-b", depends_on=["s1"])])
        self.approve()
        self.assertEqual(self.fake.created, ["s1"])
        self.tick()
        self.assertEqual(self.fake.created, ["s1"])             # s2 ждёт s1
        self.fake.finish("s1")
        self.tick()
        self.assertEqual(self.by_id("s1")["status"], "done")
        self.assertEqual(self.by_id("s2")["status"], "running")
        self.assertEqual(self.fake.created, ["s1", "s2"])

    def test_parallel_limit_one(self):
        room = self.make_room(title="Один слот", max_parallel_streams=1)
        plan.ingest_plan(self.cfg, room["id"], "t_brief001",
                         {"plan": {"streams": [mkstream("Первое"), mkstream("Второе")]}},
                         room=room)
        plan.approve(self.cfg, room["id"], "all", room=room)
        self.assertEqual(self.fake.created, ["s1"])
        self.fake.finish("s1")
        plan.tick(self.cfg, room["id"], room=room)
        self.assertEqual(self.fake.created, ["s1", "s2"])

    def test_waiting_quota_stops_the_queue_and_resumes(self):
        self.ingest([mkstream("Рецептура"), mkstream("Упаковка", owner="worker-b")])
        self.approve()
        self.fake.quota.add(self.fake.rid("s1"))
        self.tick()
        self.assertEqual(self.by_id("s1")["status"], "waiting_quota")
        self.assertTrue(any(item["kind"] == "quota" for item in self.current()["log"]))
        self.fake.finish("s2")                                   # слот освободился
        self.fake.quota.discard(self.fake.rid("s1"))
        self.fake.status[self.fake.rid("s1")] = "working"
        self.tick()
        self.assertEqual(self.by_id("s1")["status"], "running")

    def test_launch_failure_keeps_stream_in_queue(self):
        self.ingest([mkstream("Рецептура")])
        self.fake.fail_with = "Лимит провайдера"
        res = self.approve()
        self.assertEqual(res["started"], [])
        stream = self.by_id("s1")
        self.assertEqual(stream["status"], "queued")
        self.assertIn("Лимит провайдера", stream["reason"])
        self.assertTrue(any("Не удалось запустить" in item["text"] for item in self.current()["log"]))

    def test_parallel_ticks_do_not_duplicate_streams(self):
        self.fake.delay = 0.02
        self.room = self.make_room(title="Четыре слота", max_parallel_streams=4)
        self.ingest([mkstream("Направление %d" % i) for i in range(1, 4)])
        self.approve("all")
        errors: list = []

        def run():
            try:
                self.tick()
            except Exception as exc:  # noqa: BLE001 - поток не должен молча упасть
                errors.append(exc)

        threads = [threading.Thread(target=run) for _ in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(20)
        self.assertEqual(errors, [])
        self.assertEqual(sorted(self.fake.created), ["s1", "s2", "s3"])
        self.assertEqual(len(set(self.fake.rid(s) for s in self.fake.created)), 3)

    def test_file_is_written_atomically(self):
        self.ingest([mkstream("Рецептура"), mkstream("Упаковка", depends_on=["s1"])])
        self.approve()
        path = plan.plan_path(self.cfg, self.room["id"])
        self.assertTrue(path.is_file())
        data = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(data["status"], "running")   # оба направления стартовали
        leftovers = [p.name for p in path.parent.glob("*.tmp*")]
        self.assertEqual(leftovers, [])

    def test_plan_becomes_done(self):
        self.ingest([mkstream("Рецептура")])
        self.approve()
        self.fake.finish("s1")
        self.tick()
        stored = self.current()
        self.assertEqual(stored["streams"][0]["status"], "done")
        self.assertEqual(stored["status"], "done")

    def test_approve_requires_project_mode_and_plan(self):
        room = self.make_room(title="Без режима", project_mode=False)
        with self.assertRaises(plan.PlanError) as ctx:
            plan.approve(self.cfg, room["id"], "all", room=room)
        self.assertEqual(ctx.exception.code, "conflict")
        with self.assertRaises(plan.PlanError) as ctx:
            plan.approve(self.cfg, self.room["id"], "all", room=self.room)
        self.assertEqual(ctx.exception.code, "not_found")
        with self.assertRaises(plan.PlanError) as ctx:
            self.ingest([mkstream("Рецептура")])
            self.approve(["s9"])
        self.assertEqual(ctx.exception.code, "not_found")


class DropAndManualTests(PlanBase):
    def test_add_stream_is_manual_and_proposed(self):
        self.ingest([mkstream("Рецептура")])
        self.approve()
        res = plan.add_stream(self.cfg, self.room["id"], mkstream("Новое", owner="worker-b"),
                              room=self.room)
        stream = res["stream"]
        self.assertEqual((stream["status"], stream["created_by"]), ("proposed", "mikhail"))
        self.assertEqual(len(self.fake.created), 1)              # без approve не запускается
        self.assertTrue(any(item["kind"] == "added" for item in res["plan"]["log"]))

    def test_add_stream_respects_the_limit(self):
        self.ingest([mkstream("Направление %d" % i) for i in range(8)])
        with self.assertRaises(plan.PlanError) as ctx:
            plan.add_stream(self.cfg, self.room["id"], mkstream("Девятое"), room=self.room)
        self.assertEqual(ctx.exception.code, "conflict")

    def test_parallel_add_stream_stops_at_eight(self):
        """Лимит проверяется под блокировкой: 8 потоков не дают 11 направлений (волна 9)."""
        self.ingest([mkstream("Направление %d" % i) for i in range(1, 4)])
        done, refused = [], []
        barrier = threading.Barrier(8)

        def add(index):
            try:
                barrier.wait(10)
                plan.add_stream(self.cfg, self.room["id"],
                                mkstream("Параллельное %d" % index, owner="worker-b"),
                                room=self.room)
                done.append(index)
            except plan.PlanError as exc:
                refused.append(exc.code)
            except Exception as exc:  # noqa: BLE001 - поток не должен молча упасть
                refused.append(str(exc))

        threads = [threading.Thread(target=add, args=(i,)) for i in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(20)
        stored = self.current()["streams"]
        self.assertEqual(len(stored), plan.MAX_STREAMS)
        self.assertEqual(len({s["id"] for s in stored}), plan.MAX_STREAMS)
        self.assertEqual((len(done), len(refused)), (5, 3))
        self.assertEqual(set(refused), {"conflict"})

    def test_parallel_patch_adds_stop_at_eight(self):
        """Тот же лимит для правок карты от руководителя (`apply_patch.add`)."""
        self.ingest([mkstream("Направление %d" % i) for i in range(1, 7)])
        barrier = threading.Barrier(6)
        warnings: list = []

        def add(index):
            barrier.wait(10)
            res = plan.apply_patch(self.cfg, self.room["id"],
                                   {"add": [mkstream("Правка %d" % index, owner="worker-c")]},
                                   source="manager", room=self.room)
            warnings.extend(res.get("warnings") or [])

        threads = [threading.Thread(target=add, args=(i,)) for i in range(6)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(20)
        stored = self.current()["streams"]
        self.assertEqual(len(stored), plan.MAX_STREAMS)
        self.assertEqual(len({s["id"] for s in stored}), plan.MAX_STREAMS)
        self.assertTrue(any("восемь" in w for w in warnings), warnings)

    def test_drop_keeps_stream_and_logs(self):
        self.ingest([mkstream("Рецептура"), mkstream("Упаковка", owner="worker-b")])
        self.approve()
        res = plan.drop_stream(self.cfg, self.room["id"], "s2", "передумали", room=self.room)
        stream = self.by_id("s2")
        self.assertEqual(stream["status"], "dropped")
        self.assertTrue(any(item["kind"] == "dropped" for item in res["plan"]["log"]))
        self.assertEqual(len(self.current()["streams"]), 2)       # не удалено физически


class LaunchModeTests(PlanBase):
    """`launch_mode: auto` — направления идут в очередь без «Запустить» (волна 9, §6)."""

    def test_auto_starts_after_ingest_with_log(self):
        room = self.make_room(title="Сразу", launch_mode="auto", max_parallel_streams=1)
        res = plan.ingest_plan(self.cfg, room["id"], "t_brief001",
                               {"plan": {"streams": [mkstream("Первое"), mkstream("Второе")]}},
                               room=room)
        self.assertTrue(res.get("auto"))
        self.assertEqual(res["started"], ["s1"])                 # лимит комнаты = 1
        stored = plan.load(self.cfg, room["id"])
        self.assertEqual(stored["status"], "running")
        self.assertEqual([s["status"] for s in stored["streams"]], ["running", "queued"])
        logs = [item["text"] for item in stored["log"]]
        self.assertTrue(any("Запуск без подтверждения" in t and "режим комнаты" in t for t in logs), logs)
        self.assertEqual(self.fake.created, ["s1"])

    def test_auto_tick_finishes_the_draft_without_approve(self):
        """Страховка в tick: черновик в комнате «сразу» не ждёт кнопку."""
        room = self.make_room(title="Сразу-тик", launch_mode="auto", max_parallel_streams=2)
        plan._update(self.cfg, room["id"], lambda p: dict(
            p, status="draft", streams=[dict(plan._norm_stream(mkstream("Одно")),
                                             id="s1", status="proposed")]))
        res = plan.tick(self.cfg, room["id"], room=room)
        self.assertEqual(res["started"], ["s1"])
        self.assertEqual(plan.load(self.cfg, room["id"])["status"], "running")

    def test_plan_first_waits_for_approve(self):
        room = self.make_room(title="План", launch_mode="plan_first")
        res = plan.ingest_plan(self.cfg, room["id"], "t_brief001",
                               {"plan": {"streams": [mkstream("Рецептура")]}}, room=room)
        self.assertFalse(res.get("auto"))
        self.assertEqual(res["started"] if "started" in res else [], [])
        self.assertEqual(plan.load(self.cfg, room["id"])["status"], "draft")
        self.assertEqual(self.fake.created, [])
        tick = plan.tick(self.cfg, room["id"], room=room)
        self.assertEqual((tick["started"], tick["skipped"]), ([], "draft"))
        self.assertEqual(plan.approve(self.cfg, room["id"], "all", room=room)["started"], ["s1"])


class StreamBodyTests(PlanBase):
    """Тело под-запроса: название брифа вместо `t_…`, технические id — в служебной части."""

    def test_brief_title_replaces_technical_id(self):
        registry.add_request(self.cfg, self.room["id"], {
            "root_id": "t_brief001", "slug": "brief-1", "title": "Набор моти к декабрю",
            "text": "Моти", "created_at": int(time.time()), "brief": True})
        self.ingest([mkstream("Рецептура")])
        stored = self.current()
        body = plan._stream_body(self.cfg, stored, stored["streams"][0], self.room)
        self.assertIn("«Набор моти к декабрю»", body)
        self.assertNotIn("Бриф: t_", body)
        head, _, service = body.partition("Служебное")
        self.assertNotIn("t_brief001", head)
        self.assertIn("t_brief001", service)
        self.assertIn("направление s1", service)

    def test_body_asks_to_pass_context_to_subtasks(self):
        self.ingest([mkstream("Рецептура")])
        stored = self.current()
        body = plan._stream_body(self.cfg, stored, stored["streams"][0], self.room)
        self.assertIn("в тело каждой карточки перенеси суть", body)

    def test_owner_label_is_russian_not_profile_id(self):
        room = self.make_room(title="Подписи", orchestrator="biz-director",
                              members=[{"profile": "biz-ops", "note": "Операции: поставки и упаковка"},
                                       {"profile": "biz-cfo", "role": "worker", "note": ""}])
        result = plan.validate_plan(room, [mkstream("Рецептура", owner="biz-ops"),
                                           mkstream("Смета", owner="biz-cfo"),
                                           mkstream("Свод", owner="biz-director")])
        labels = {s["title"]: (s["owner"], s["owner_label"]) for s in result["streams"]}
        self.assertEqual(labels["Рецептура"], ("biz-ops", "Операции"))
        self.assertEqual(labels["Смета"], ("biz-cfo", "Экономика"))
        self.assertEqual(labels["Свод"][1], "руководитель комнаты")
        for _owner, label in labels.values():
            self.assertNotIn("biz-", label)
        self.ingest([mkstream("Рецептура")])
        stored = plan.load(self.cfg, self.room["id"])
        plan.fill_labels(stored, self.room)
        self.assertTrue(all(s.get("owner_label") for s in stored["streams"]))


# ------------------------------------------------------------------ правки A6
class PatchTests(PlanBase):
    def test_auto_fields_are_applied(self):
        self.ingest([mkstream("Рецептура"), mkstream("Упаковка", owner="worker-b")])
        self.approve()
        res = plan.apply_patch(self.cfg, self.room["id"], {"change": [
            {"id": "s2", "fields": {"title": "Упаковка и бренд", "goal": "Выбрать поставщика",
                                    "due": "2026-11-20", "members": ["worker-c"]},
             "reason": "Михаил уточнил"}]}, source="manager", room=self.room)
        self.assertTrue(res["changed"])
        stream = self.by_id("s2")
        self.assertEqual(stream["title"], "Упаковка и бренд")
        self.assertEqual(stream["goal"], "Выбрать поставщика")
        self.assertEqual(stream["due"], "2026-11-20")
        self.assertEqual(stream["members"], ["worker-c"])
        self.assertEqual(stream["status"], "running")            # статус не сброшен
        log = self.current()["log"][-1]
        self.assertEqual(log["kind"], "changed")
        self.assertIn("Михаил уточнил", log["text"])

    def test_new_stream_from_manager_needs_approve(self):
        self.ingest([mkstream("Рецептура")])
        self.approve()
        res = plan.apply_patch(self.cfg, self.room["id"], {
            "add": [mkstream("Сертификация", owner="worker-b")], "note": "из сообщения Михаила"},
            source="manager", room=self.room)
        self.assertEqual(res["applied"]["add"], ["s2"])
        self.assertEqual(self.by_id("s2")["status"], "proposed")
        self.assertEqual(len(self.fake.created), 1)
        self.approve(["s2"])
        self.assertEqual(self.by_id("s2")["status"], "running")

    def test_restart_of_finished_stream_needs_confirmation(self):
        self.ingest([mkstream("Рецептура")])
        self.approve()
        self.fake.finish("s1")
        self.tick()
        self.assertEqual(self.by_id("s1")["status"], "done")
        res = plan.apply_patch(self.cfg, self.room["id"],
                               {"change": [{"id": "s1", "fields": {"status": "running"}}]},
                               source="manager", room=self.room)
        self.assertEqual(self.by_id("s1")["status"], "proposed")
        self.assertTrue(any("требует подтверждения" in w for w in res["warnings"]))
        self.assertEqual(len(self.fake.created), 1)

    def test_bad_date_and_unknown_stream_are_warnings(self):
        self.ingest([mkstream("Рецептура")])
        res = plan.apply_patch(self.cfg, self.room["id"], {"change": [
            {"id": "s1", "fields": {"due": "завтра"}},
            {"id": "s9", "fields": {"title": "Не важно"}}]}, room=self.room)
        self.assertIsNone(self.by_id("s1")["due"])
        text = " ".join(res["warnings"])
        self.assertIn("не похож на дату", text)
        self.assertIn("такого направления нет", text)

    def test_drop_from_patch(self):
        self.ingest([mkstream("Рецептура"), mkstream("Упаковка", owner="worker-b")])
        res = plan.apply_patch(self.cfg, self.room["id"],
                               {"drop": [{"id": "s2", "reason": "не сейчас"}]},
                               source="manager", room=self.room)
        self.assertEqual(res["applied"]["drop"], ["s2"])
        self.assertEqual(self.by_id("s2")["status"], "dropped")
        self.assertEqual(len(self.current()["streams"]), 2)

    def test_patch_without_fields_changes_nothing(self):
        self.ingest([mkstream("Рецептура")])
        res = plan.apply_patch(self.cfg, self.room["id"], {"note": "просто заметка"},
                               source="manager", room=self.room)
        self.assertFalse(res["changed"])
        self.assertTrue(any("просто заметка" in item["text"] for item in res["plan"]["log"]))

    def test_patch_on_draft_keeps_it_draft(self):
        self.ingest([mkstream("Рецептура")])
        plan.apply_patch(self.cfg, self.room["id"],
                         {"change": [{"id": "s1", "fields": {"goal": "Новая цель"}}]},
                         room=self.room)
        self.assertEqual(self.current()["status"], "draft")


# -------------------------------------------------- сводка, связь с запросом
class MetaTests(PlanBase):
    def test_detail_meta_for_brief_and_stream(self):
        self.ingest([mkstream("Рецептура"), mkstream("Упаковка", owner="worker-b")])
        self.approve()
        brief = plan.detail_meta(self.cfg, self.room["id"], "t_brief001")
        self.assertTrue(brief["brief"] is False or brief["brief"] is True)   # ключ есть
        self.assertEqual(brief["streams_summary"]["total"], 2)
        self.assertIsNone(brief["plan_link"])
        rid = self.fake.rid("s1")
        meta = plan.detail_meta(self.cfg, self.room["id"], rid)
        self.assertEqual(meta["plan_link"]["stream_id"], "s1")
        self.assertEqual(meta["plan_link"]["title"], "Рецептура")
        self.assertIsNone(meta["streams_summary"])
        self.assertFalse(meta["brief"])

    def test_detail_meta_without_plan(self):
        meta = plan.detail_meta(self.cfg, self.room["id"], "t_00000000")
        self.assertEqual(meta, {"brief": False, "plan_link": None, "streams_summary": None})

    def test_brief_flag_comes_from_registry(self):
        registry.add_request(self.cfg, self.room["id"], {
            "root_id": "t_brief001", "slug": "brief-1", "title": "Главный запрос",
            "text": "Моти", "created_at": int(time.time()), "brief": True})
        self.ingest([mkstream("Рецептура")], root="t_brief001")
        self.assertTrue(plan.detail_meta(self.cfg, self.room["id"], "t_brief001")["brief"])


# ------------------------------------------------------------------- HTTP §4
class HttpTests(PlanBase):
    """Маршруты §4 на живом сервере (порт 0, песочница, движок подменён)."""

    def setUp(self):
        super().setUp()
        import server.app as app_mod

        self.server = app_mod.create_app(self.cfg, start_watcher=False, port=0)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever,
                                       kwargs={"poll_interval": 0.1}, daemon=True)
        self.thread.start()

    def tearDown(self):
        try:
            self.server.shutdown()
        finally:
            self.server.server_close()
            self.thread.join(5)
        super().tearDown()

    def call(self, method, path, body=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        headers = {"Host": "127.0.0.1:%d" % self.port,
                   "Origin": "http://127.0.0.1:%d" % self.port,
                   "Content-Type": "application/json"}
        payload = None if body is None else json.dumps(body, ensure_ascii=False).encode("utf-8")
        try:
            conn.request(method, path, body=payload, headers=headers)
            res = conn.getresponse()
            raw = res.read()
            data = json.loads(raw.decode("utf-8")) if raw else None
            return res.status, data
        finally:
            conn.close()

    def rid(self):
        return self.room["id"]

    def test_plan_lifecycle_over_http(self):
        base = "/api/rooms/%s/plan" % self.rid()
        status, data = self.call("GET", base)
        self.assertEqual((status, data["plan"]), (200, None))
        self.ingest([mkstream("Рецептура")])                     # черновик руководителя
        status, data = self.call("GET", base)
        self.assertEqual((status, data["plan"]["status"]), (200, "draft"))
        status, data = self.call("POST", base + "/streams",
                                 {"title": "Упаковка и бренд", "goal": "Поставщик",
                                  "owner": "worker-b"})
        self.assertEqual(status, 201)
        self.assertEqual(data["plan"]["streams"][1]["created_by"], "mikhail")
        status, data = self.call("PATCH", base + "/streams/s2", {"goal": "Новая цель"})
        self.assertEqual(status, 200)
        self.assertEqual(data["plan"]["streams"][1]["goal"], "Новая цель")
        status, data = self.call("POST", base + "/approve", {"streams": "all"})
        self.assertEqual(status, 200)
        self.assertEqual(data["started"], ["s1", "s2"])
        status, data = self.call("DELETE", base + "/streams/s2", {"reason": "не сейчас"})
        self.assertEqual(status, 200)
        self.assertEqual(data["plan"]["streams"][1]["status"], "dropped")

    def test_errors_are_russian_4xx(self):
        status, data = self.call("GET", "/api/rooms/nosuchroom/plan")
        self.assertEqual(status, 404)
        self.assertIn("комнат", data["error"]["message"].lower())
        status, data = self.call("POST", "/api/rooms/%s/plan/approve" % self.rid(),
                                 {"streams": 42})
        self.assertEqual(status, 400)
        self.assertTrue(data["error"]["message"])
        status, data = self.call("PATCH", "/api/rooms/%s/plan/streams/s1" % self.rid(), {})
        self.assertEqual(status, 400)
        status, data = self.call("POST", "/api/rooms/%s/plan/streams" % self.rid(), {"title": ""})
        self.assertEqual(status, 400)
        # карты ещё нет: approve честно 404, а не 500
        status, data = self.call("POST", "/api/rooms/%s/plan/approve" % self.rid(), {})
        self.assertEqual(status, 404)
        self.assertIn("карты", data["error"]["message"].lower())
        status, data = self.call("GET", "/api/rooms/%s/plan" % self.rid())
        self.assertEqual((status, data["plan"]), (200, None))

    def test_request_detail_has_plan_meta(self):
        """Ключи §4 обязаны быть в ответе даже без карты (иначе фикстуры расходятся)."""
        self.ingest([mkstream("Рецептура")], root="t_brief001")
        status, data = self.call("GET", "/api/rooms/%s/plan" % self.rid())
        self.assertEqual((status, data["plan"]["status"]), (200, "draft"))
        self.assertEqual(data["summary"]["total"], 1)
        # подпись исполнителя по-русски рядом с техническим id (волна 9, п.6)
        stream = data["plan"]["streams"][0]
        self.assertEqual(stream["owner"], "worker-a")
        self.assertEqual(stream["owner_label"], plan.owner_label(self.room, "worker-a"))
        self.assertTrue(stream["owner_label"])


class DetailIntegrationTests(unittest.TestCase):
    """RequestDetail на настоящем дереве карточек: ``brief``/``plan_link``/сводка."""

    @classmethod
    def setUpClass(cls):
        from tests.helpers import Sandbox

        cls.sb = Sandbox().__enter__()
        cls.cfg = cls.sb.cfg
        cls.room = rooms.create_room(cls.cfg, {"title": "Проверка", "orchestrator": ORCH,
                                              "members": [{"profile": p} for p in WORKERS]})
        cls.rid = cls.sb.state("working")["root_id"]
        cls.child = cls.sb.state("working")["child_ids"][0]
        registry.add_request(cls.cfg, cls.room["id"], {
            "root_id": cls.rid, "slug": "plan-link-1", "title": "Под-запрос направления",
            "text": "Рецептура", "created_at": int(time.time()), "brief": False})
        plan._update(cls.cfg, cls.room["id"], lambda p: dict(
            p, status="running", brief_root_id="t_briefroot",
            streams=[dict(plan._norm_stream({"id": "s1", "title": "Рецептура",
                                             "status": "running", "request_id": cls.rid})),
                     dict(plan._norm_stream({"id": "s2", "title": "Упаковка",
                                             "status": "done"}))]))

    @classmethod
    def tearDownClass(cls):
        cls.sb.__exit__(None, None, None)

    def meta(self, rid):
        return plan.detail_meta(self.cfg, self.room["id"], rid)

    def test_stream_request_has_plan_link(self):
        meta = self.meta(self.rid)
        self.assertEqual(meta["plan_link"]["stream_id"], "s1")
        self.assertEqual(meta["plan_link"]["title"], "Рецептура")
        self.assertIsNone(meta["streams_summary"])

    def test_brief_has_streams_summary(self):
        meta = self.meta("t_briefroot")                 # карточки может не быть — сводка есть
        self.assertEqual(meta["streams_summary"], {"total": 2, "running": 1, "done": 1,
                                                   "status": "running"})
        self.assertIsNone(meta["plan_link"])


if __name__ == "__main__":
    unittest.main()
