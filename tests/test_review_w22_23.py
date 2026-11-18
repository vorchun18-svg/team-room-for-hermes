# -*- coding: utf-8 -*-
"""REV3 (критик W22/W23): ложные срабатывания сторожа, безопасность разрешений, гонки.

Зачем отдельный файл: пакеты MG1 (контроль моделей) и AP1 (разрешения) проверялись
своими тестами «как задумано», а этот набор проверяет их «как бывает» — на путях, где
сторож может убить НОРМАЛЬНЫЙ запуск, где разрешение можно выдать не тому профилю и где
две одновременные записи теряют данные.

Данные только вымышленные: кафе «Лето», профили `team-*`, поддельные `kanban.db`/`state.db`
в песочнице (`tests.helpers.Sandbox`, `ApprovalCase`), CLI Hermes подменён шимом.

    python -m unittest tests.test_review_w22_23 -v
"""
from __future__ import annotations

import json
import sqlite3
import sys
import threading
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import server.approvals as ap                        # noqa: E402
import server.model_guard as guard                   # noqa: E402
import server.worklog as wl                          # noqa: E402
from tests.test_approvals import (                   # noqa: E402
    BLOCK_FLOOR, BLOCK_HEREDOC, CALL_TERMINAL, CARD, LEAD, PROFILE, ROOM_ID, ApprovalCase,
)
from tests.test_model_guard import (                 # noqa: E402
    CHEAP, EXPENSIVE, LEAD as GUARD_LEAD, ROOM, TENANT, WORKER, Base as GuardBase,
)

CALL_PY = json.dumps([{"function": {"name": "terminal", "arguments": json.dumps(
    {"command": "python -c \"print('отчёт')\""})}}])


# ---------------------------------------------------------------- сторож моделей

class GuardFalsePositiveTests(GuardBase):
    """Сторож НЕ должен останавливать нормальные запуски (W23 §2.3: работу не убиваем)."""

    def test_root_without_override_then_assigned_is_untouched(self):
        """Боевой случай 17.11: корень создан в `triage` без переопределения, потом
        Hermes назначил исполнителя с локальной моделью — сторож не вмешивается."""
        root = self.new_root("Проверь поставщика ромашки")
        con = self.sa.connect()
        try:
            con.execute("UPDATE tasks SET assignee = ?, status = 'triage' WHERE id = ?",
                        (WORKER, root))
            con.commit()
        finally:
            con.close()
        res = self.tick()
        self.assertNotIn(root, res["fixed"], res)
        self.assertNotIn(root, res["stopped"], res)
        row = self.row(root)
        self.assertEqual(row["status"], "triage")
        self.assertFalse(row.get("model_override"))
        self.assertEqual(self.kinds(root), [], "сторож записал событие в нормальную карточку")

    def test_leader_override_is_removed_before_start(self):
        """Устаревшее переопределение руководителя (sonnet) снимается ДО запуска, не убивая."""
        root = self.new_root("Проверь сроки")
        card_id = self.card("Собери факты", WORKER, "triage", parent=root,
                            model=EXPENSIVE[1], provider=EXPENSIVE[0])
        res = self.tick()
        self.assertIn(card_id, res["fixed"], res)
        self.assertNotIn(card_id, res["stopped"], res)
        row = self.row(card_id)
        self.assertEqual(row["status"], "triage")
        self.assertFalse(row.get("model_override"), row)

    def test_provider_alias_same_model_is_not_killed(self):
        """Та же модель, другой ярлык провайдера (`custom` вместо `llamacpp`) — не остановка.

        В реальных `sessions` профиля `ornith` модель `qwen36v` записана и как `llamacpp`,
        и как `custom` (старые запуски). Убивать за это нормальную работу нельзя.
        """
        root = self.new_root("Свод по заявкам")
        card_id = self.card("Собери факты", WORKER, "ready", parent=root)
        self.running(card_id, WORKER, "s_alias_1", CHEAP[1], "custom")
        res = self.tick()
        self.assertNotIn(card_id, res["stopped"], "остановили работу из-за ярлыка провайдера")
        self.assertEqual(self.status(card_id), "running")
        warned = [e for e in self.events() if e.get("kind") == "warned"
                  and str(e.get("card_id")) == card_id]
        self.assertEqual(len(warned), 1, warned)
        self.assertIn("модель та же", warned[0]["text"])

    def test_provider_alias_warns_only_once(self):
        """Повторные проходы (каждые 3 с) не заваливают журнал одинаковыми строками."""
        root = self.new_root("Свод по заявкам")
        card_id = self.card("Собери факты", WORKER, "ready", parent=root)
        self.running(card_id, WORKER, "s_alias_2", CHEAP[1], "custom")
        for _ in range(3):
            self.tick()
        warned = [e for e in self.events() if e.get("kind") == "warned"
                  and str(e.get("card_id")) == card_id]
        self.assertEqual(len(warned), 1, "журнал растёт каждые 3 секунды: %d строк" % len(warned))

    def test_warn_mode_does_not_spam(self):
        """Режим «только предупреждать» — тоже одно предупреждение, а не поток."""
        self.app.call("rooms", "update_room",
                      ((self.cfg, ROOM, {"agent_models": {}, "model_guard": "warn"}), {}))
        root = self.new_root("Свод по заявкам")
        card_id = self.card("Собери факты", WORKER, "ready", parent=root)
        self.running(card_id, WORKER, "s_warn_1", EXPENSIVE[1], EXPENSIVE[0])
        for _ in range(4):
            self.tick()
        self.assertEqual(self.status(card_id), "running", "режим warn остановил работу")
        warned = [e for e in self.events() if e.get("kind") == "warned"
                  and str(e.get("card_id")) == card_id]
        self.assertEqual(len(warned), 1, "предупреждение повторилось %d раз" % len(warned))

    def test_closed_run_of_previous_attempt_is_not_a_reason_to_stop(self):
        """Прошлый запуск закрыт (`ended_at`), новый только начался — сессия прошлого
        запуска не повод останавливать свежий."""
        root = self.new_root("Свод по заявкам")
        card_id = self.card("Собери факты", WORKER, "ready", parent=root)
        self.running(card_id, WORKER, "s_old_1", EXPENSIVE[1], EXPENSIVE[0])
        con = self.sa.connect()
        try:                                     # прошлая попытка закрыта, карточку взяли заново
            con.execute("UPDATE task_runs SET ended_at = ? WHERE task_id = ?",
                        (int(time.time()) - 30, card_id))
            con.commit()
        finally:
            con.close()
        res = self.tick()
        self.assertNotIn(card_id, res["stopped"], "остановили свежий запуск по прошлой сессии")
        self.assertEqual(self.status(card_id), "running")

    def test_restart_budget_resets_after_a_good_run(self):
        """Два старых перезапуска не должны навсегда лишать карточку автоматического лечения."""
        root = self.new_root("Свод по заявкам")
        card_id = self.card("Собери факты", WORKER, "ready", parent=root)
        self.running(card_id, WORKER, "s_good_1", CHEAP[1], CHEAP[0])
        for _ in range(guard.GUARD_MAX_RESTARTS):
            guard._bump_restarts(self.cfg, ROOM, card_id, model=EXPENSIVE[1])
        self.tick()
        restarts = (guard.read_state(self.cfg, ROOM).get("restarts") or {})
        self.assertNotIn(card_id, restarts, "лимит перезапусков израсходован навсегда")

    def test_card_with_room_model_is_not_a_mismatch(self):
        """Карточка с моделью комнаты (как у автообновления свода) — не расхождение."""
        self.app.call("rooms", "update_room",
                      ((self.cfg, ROOM, {"agent_models": {WORKER: {
                          "provider": EXPENSIVE[0], "model": EXPENSIVE[1]}},
                        "model_guard": "enforce"}), {}))
        root = self.new_root("Обнови свод")
        card_id = self.card("Обновление свода", WORKER, "ready", parent=root,
                            model=EXPENSIVE[1], provider=EXPENSIVE[0])
        res = self.tick()
        self.assertNotIn(card_id, res["fixed"], res)
        self.assertNotIn(card_id, res["stopped"], res)
        self.assertEqual(self.row(card_id)["model_override"], EXPENSIVE[1])

    def test_two_profiles_with_same_model_are_not_confused(self):
        """Два профиля: выбор комнаты для одного не переносится на карточку другого."""
        self.app.call("rooms", "update_room",
                      ((self.cfg, ROOM, {"agent_models": {
                          WORKER: {"provider": EXPENSIVE[0], "model": EXPENSIVE[1]},
                          "team-cfo": {"provider": CHEAP[0], "model": CHEAP[1]}},
                        "model_guard": "enforce"}), {}))
        root = self.new_root("Две дорожки")
        mine = self.card("Собери факты", WORKER, "ready", parent=root,
                         model=EXPENSIVE[1], provider=EXPENSIVE[0])
        other = self.card("Проверь цифры", "team-cfo", "ready", parent=root,
                          model=CHEAP[1], provider=CHEAP[0])
        res = self.tick()
        self.assertNotIn(mine, res["fixed"], res)
        self.assertNotIn(other, res["fixed"], res)
        self.assertNotIn(mine, res["stopped"], res)
        self.assertEqual(self.row(mine)["model_override"], EXPENSIVE[1])
        self.assertEqual(self.row(other)["model_override"], CHEAP[1],
                         "карточке второго профиля приписали модель первого")

    def test_card_with_unknown_profile_model_is_only_warned(self):
        """Профиль без модели: не останавливаем, только предупреждение (слепота ≠ вина)."""
        root = self.new_root("Свод по заявкам")
        card_id = self.card("Собери факты", "team-runner", "ready", parent=root)
        self.running(card_id, "team-runner", "s_unknown_1", EXPENSIVE[1], EXPENSIVE[0])
        res = self.tick()
        self.assertNotIn(card_id, res["stopped"], res)
        self.assertEqual(self.status(card_id), "running")


# ---------------------------------------------------------------- разрешения

class ApprovalSecurityTests(ApprovalCase):
    """Разрешение нельзя выдать/отозвать не тому профилю и нельзя потерять при гонке."""

    def _fake_worker(self, config_path: Path, calls: list, delay: float = 0.0):
        """Подмена подпроцесса: read-modify-write того же вида, что делает Hermes."""
        lock_free = {"value": None}

        def fake(cfg, request, timeout=0):
            calls.append(dict(request))
            if request.get("op") == "remove":
                return {"ok": True, "changed": True, "allowlist": []}
            import yaml
            before = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
            allow = [str(v) for v in (before.get("command_allowlist") or [])]
            if delay:
                time.sleep(delay)             # окно гонки: между чтением и записью
            if request.get("pattern_key") not in allow:
                allow.append(str(request.get("pattern_key")))
            before["command_allowlist"] = allow
            config_path.write_text(yaml.safe_dump(before, allow_unicode=True), encoding="utf-8")
            return {"ok": True, "changed": True, "allowlist": allow}

        return fake

    def test_revoke_rejects_profile_outside_the_room(self):
        """Чужой профиль по телу запроса — отказ (иначе пишем в чужой config.yaml)."""
        with self.assertRaises(ap.ApprovalError) as ctx:
            ap.revoke(self.cfg, "someone-else", "script execution via heredoc", room=self.room)
        self.assertEqual(ctx.exception.http, 400)
        self.assertFalse((self.root / "profiles" / "someone-else").exists())

    def test_revoke_rejects_path_traversal(self):
        """`..`, абсолютные пути и разделители — не имя профиля."""
        for bad in ("../default", "..", "a/../b", "C:/Windows", "profiles/x", "default\\x",
                    "", " ", "-rf"):
            with self.assertRaises(ap.ApprovalError) as ctx:
                ap.revoke(self.cfg, bad, "script execution via heredoc")
            self.assertEqual(ctx.exception.http, 400, bad)

    def test_pattern_key_comes_from_the_registry_not_the_client(self):
        """Ключ записи берётся из найденной блокировки, а не из тела запроса."""
        self.session([("terminal", CALL_TERMINAL, BLOCK_HEREDOC)])
        item = ap.collect(self.cfg, self.room, CARD)[0]
        calls: list = []
        real = ap.run_worker
        ap.run_worker = self._fake_worker(self.home / "config.yaml", calls)   # noqa: N806
        try:
            payload = ap.decide(self.cfg, self.room, CARD, item["id"], "always",
                                note="", extra=None)
        finally:
            ap.run_worker = real                                              # noqa: N806
        self.assertEqual(payload["approval"]["state"], "granted_always")
        self.assertEqual([c["pattern_key"] for c in calls], ["script execution via heredoc"])

    def test_floor_block_cannot_be_granted(self):
        """Жёсткий запрет Hermes не разрешается никаким решением и ничего не пишет."""
        self.session([("terminal", CALL_TERMINAL, BLOCK_FLOOR)])
        item = ap.collect(self.cfg, self.room, CARD)[0]
        self.assertTrue(item["floor"])
        calls: list = []
        real = ap.run_worker
        ap.run_worker = self._fake_worker(self.home / "config.yaml", calls)   # noqa: N806
        try:
            for decision in ("once", "always"):
                with self.assertRaises(ap.ApprovalError) as ctx:
                    ap.decide(self.cfg, self.room, CARD, item["id"], decision)
                self.assertEqual(ctx.exception.http, 422)
        finally:
            ap.run_worker = real                                              # noqa: N806
        self.assertEqual(calls, [], "жёсткий запрет всё-таки записали в профиль")

    def test_parallel_always_keeps_both_commands(self):
        """Два «разрешать всегда» одного профиля подряд: обе записи должны сохраниться."""
        self.session([("terminal", CALL_TERMINAL, BLOCK_HEREDOC)])
        self.session([("terminal", CALL_PY, json.dumps({
            "error": ("BLOCKED: Command flagged as dangerous (script execution via -c flag) "
                      "but single-query mode (-q) runs without a user present to approve it."),
            "status": "blocked", "exit_code": -1}, ensure_ascii=False))])
        items = ap.collect(self.cfg, self.room, CARD)
        self.assertGreaterEqual(len(items), 2, items)
        calls: list = []
        real = ap.run_worker
        ap.run_worker = self._fake_worker(self.home / "config.yaml", calls, delay=0.05)  # noqa: N806
        errors: list = []

        def go(ident):
            try:
                ap.decide(self.cfg, self.room, CARD, ident, "always")
            except Exception as exc:          # noqa: BLE001 - покажем в ассерте
                errors.append(exc)

        try:
            threads = [threading.Thread(target=go, args=(item["id"],)) for item in items[:2]]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=30)
        finally:
            ap.run_worker = real                                              # noqa: N806
        self.assertEqual(errors, [])
        allow = self.allowlist()
        for item in items[:2]:
            self.assertIn(item["pattern_key"], allow,
                          "одна из двух одновременных записей потерялась: %s" % allow)
        grants = ap.grants(self.cfg, self.room)["items"]
        self.assertEqual(len([g for g in grants if g["scope"] == "always"]), 2, grants)

    def test_sweep_does_not_drop_a_grant_added_meanwhile(self):
        """Снятие «разового» по завершении карточки не затирает разрешение, выданное рядом."""
        self.session([("terminal", CALL_TERMINAL, BLOCK_HEREDOC)])
        item = ap.collect(self.cfg, self.room, CARD)[0]
        calls: list = []
        real = ap.run_worker
        ap.run_worker = self._fake_worker(self.home / "config.yaml", calls)   # noqa: N806
        try:
            ap.decide(self.cfg, self.room, CARD, item["id"], "once")
            # пока сторож собирался снимать, пришло постоянное разрешение другого правила
            reg_path = ap.registry_path(self.cfg, ROOM_ID)
            reg = json.loads(reg_path.read_text(encoding="utf-8"))
            reg["grants"].append({"profile": PROFILE, "pattern_key": "recursive delete",
                                  "scope": "always", "granted_at": int(time.time()),
                                  "expires_at": None, "preexisting": False,
                                  "approval_id": "ap_deadbeef", "description": "рекурсивное удаление"})
            reg_path.write_text(json.dumps(reg, ensure_ascii=False), encoding="utf-8")
            self.set_card_status("done")
            ap.sweep(self.cfg, self.room)
        finally:
            ap.run_worker = real                                              # noqa: N806
        keys = [g["pattern_key"] for g in ap.grants(self.cfg, self.room)["items"]]
        self.assertIn("recursive delete", keys, "снятие «разового» стёрло чужое разрешение")


class ApprovalFastReadTests(ApprovalCase):
    """Чтение разрешений не должно перечитывать одну и ту же сессию на каждую карточку."""

    def test_one_session_is_parsed_once_for_a_card_tree(self):
        self.session([("terminal", CALL_TERMINAL, BLOCK_HEREDOC)])
        calls = {"value": 0}
        real = ap._parse_blocks

        def counted(cfg, profile, sid):
            calls["value"] += 1
            return real(cfg, profile, sid)

        ap.reset_cache()
        ap._parse_blocks = counted                                          # noqa: N806
        try:
            first = ap.find(self.cfg, self.room, CARD)
            ap.find(self.cfg, self.room, CARD, extra=[CARD])
        finally:
            ap._parse_blocks = real                                         # noqa: N806
        self.assertEqual(len(first), 1, first)
        self.assertEqual(calls["value"], 1, "сессия разобрана %d раз" % calls["value"])

    def test_cache_is_invalidated_when_state_db_changes(self):
        sid = self.session([("terminal", CALL_TERMINAL, BLOCK_HEREDOC)])
        ap.reset_cache()
        self.assertEqual(len(ap.find(self.cfg, self.room, CARD)), 1)
        con = sqlite3.connect(str(self.home / "state.db"))
        try:                                  # новая блокировка другим правилом
            con.execute("INSERT INTO messages (session_id, role, content, tool_calls, tool_name, "
                        "timestamp) VALUES (?, 'assistant', '', ?, NULL, ?)",
                        (sid, CALL_PY, time.time()))
            con.execute("INSERT INTO messages (session_id, role, content, tool_calls, tool_name, "
                        "timestamp) VALUES (?, 'tool', ?, NULL, 'terminal', ?)",
                        (sid, json.dumps({"error": "BLOCKED: Command flagged as dangerous "
                                                  "(script execution via -c flag) but single-query "
                                                  "mode (-q) runs without a user present.",
                                          "status": "blocked"}, ensure_ascii=False),
                         time.time() + 1))
            con.commit()
        finally:
            con.close()
        found = ap.find(self.cfg, self.room, CARD)
        self.assertEqual(len(found), 2, "кеш отдал устаревший результат: %s" % found)


# ---------------------------------------------------------------- маска секретов

class MaskTests(unittest.TestCase):
    """Секреты в команде пользователь видит скрытыми (W22 §0 п.3)."""

    def test_password_flags_and_env_secrets_are_masked(self):
        # ключ собран из частей: в тексте файла нет целого «sk-…», иначе сканер релиза
        # принимает выдуманный ключ за настоящую утечку
        fake_key = "sk" + "-proj-1234567890abcdefghij"
        cases = (
            "mysql -u root -pSuperSecret123 -e 'select 1'",
            'sshpass -p "Tr0ub4dor" ssh host true',
            "AWS_SECRET_ACCESS_KEY=abcdefghijklmnopqrstuvwxyz1234567890abcd aws s3 ls",
            "OPENAI_API_KEY=" + fake_key + " python -c 1",
        )
        for command in cases:
            masked = wl.mask(command)
            for secret in ("SuperSecret123", "Tr0ub4dor", "abcdefghijklmnopqrstuvwxyz1234567890abcd",
                           fake_key):
                if secret in command:
                    self.assertNotIn(secret, masked, "%s остался в %r" % (secret, masked))
            self.assertIn("[скрыто]", masked)

    def test_ordinary_command_is_not_over_masked(self):
        """Обычная команда читается человеком: не превращаем её в звёздочки."""
        text = wl.mask("python -m unittest tests.test_approvals -v --failfast")
        self.assertIn("unittest", text)
        self.assertIn("--failfast", text)


if __name__ == "__main__":
    unittest.main()
