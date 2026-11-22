# -*- coding: utf-8 -*-
"""W29-I1: безопасное снятие пауз сторожа моделей (P1 был опасен).

Что было не так
----------------
P1 снимал «любую» паузу карточки, если в последнем событии `blocked` встречался маркер
`OUR_PAUSE_MARKERS` («модель не подтверждена»). Значит, пауза ЧЕЛОВЕКА, у которой в тексте
случайно оказался тот же оборот, снималась сторожем. Плюс сторож снимал пин, если в реестре
комнаты не было выбора моделей, и мог ставить паузу повторно — цикл «блок ↔ снять», на
котором размыкатель петель Hermes уводит карточку в `triage` (`block_loop_detected`).

Правила, которые проверяются здесь
----------------------------------
1. Снимается ТОЛЬКО пауза из собственных `holds` комнаты с ТОЧНЫМ `PARK_REASON` (либо
   пауза до W29, у которой последнее событие несёт ту же причину дословно).
2. Пауза человека (другая причина) не снимается никогда — даже если модель верна.
3. Пин не снимается, если в реестре комнаты нет записей `agent_models`.
4. Карточку НАШЕЙ причиной сторож держит один раз за жизнь: после снятия второй блокировки
   не ставит (иначе цикл), вмешательств ровно два — «удержал» и «снял».

    python -m unittest tests.test_model_guard_safety -v
"""
from __future__ import annotations

import json
import sys
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import server.agent_models as models_mod          # noqa: E402
import server.model_guard as guard                # noqa: E402
from tests.test_model_guard_preflight import (Base, EXPECTED, WORKER,  # noqa: E402
                                               ROOM)

HUMAN_REASON = "жду ответа от клиента — сниму паузу сам"


def blocked_event(agents, card_id: str, reason: str, at: int | None = None) -> None:
    """Событие `blocked` в доске песочницы (как его пишет CLI/человек)."""
    con = agents.connect()
    try:
        con.execute("INSERT INTO task_events (task_id, kind, payload, created_at) "
                    "VALUES (?,?,?,?)",
                    (card_id, "blocked",
                     json.dumps({"reason": reason, "kind": "needs_input"}, ensure_ascii=False),
                     int(at or time.time())))
        con.commit()
    finally:
        con.close()


class PauseSafetyTests(Base):
    """Пауза человека, точная причина, пин без реестра, один блок за жизнь."""

    def broken_push(self):
        """Мост «не может поставить модель» — так пауза и появляется."""
        def push(cfg, board, cards, want):
            return [], [{"id": c["id"], "reason": "мост недоступен"} for c in cards]
        return push

    def test_human_pause_with_similar_words_is_not_released(self):
        """Причина человека с теми же словами — НЕ наша пауза, снимать нельзя."""
        self.select_room_model()
        cid = self.card(assignee=WORKER, status="blocked",
                        model=EXPECTED[1], provider=EXPECTED[0])
        blocked_event(self.sa, cid, "модель не подтверждена человеком: ждём согласования")
        res = self.preflight()
        self.assertNotIn(cid, res.get("released") or [], res)
        self.assertEqual("blocked", self.status(cid), "сняли паузу, поставленную человеком")

    def test_hold_with_other_reason_is_not_released(self):
        """В `holds` запись есть, но причина другая — это не наша пауза."""
        self.select_room_model()
        cid = self.card(assignee=WORKER, status="blocked",
                        model=EXPECTED[1], provider=EXPECTED[0])

        def change(data):
            data["holds"][cid] = {"at": int(time.time()) - 5, "reason": HUMAN_REASON}

        guard._mutate(self.cfg, ROOM, change)
        res = self.preflight()
        self.assertNotIn(cid, res.get("released") or [], res)
        self.assertEqual("blocked", self.status(cid))

    def test_no_pin_removal_when_room_has_no_models(self):
        """Реестр комнаты пуст — пин с доски не снимаем (это снесло 8 боевых карточек)."""
        cid = self.card(assignee=WORKER, status="blocked", model="pinned-model",
                        provider="prov-a")
        res = self.preflight()
        self.assertEqual("no_agent_models", res.get("skipped_reason"), res)
        self.assertEqual("pinned-model", self.row(cid).get("model_override"),
                         "сторож снял пин при пустом реестре комнаты")
        self.assertEqual("blocked", self.status(cid))

    def test_one_park_per_card_no_block_unblock_cycle(self):
        """Цикл «блок ↔ снять» запрещён: один блок, одно снятие, дальше тишина."""
        self.select_room_model()
        real = models_mod.push_model
        models_mod.push_model = self.broken_push()
        try:
            cid = self.card(assignee=WORKER, status="ready")
            first = self.preflight()
            self.assertIn(cid, first.get("held") or [], first)
            self.assertEqual("blocked", self.status(cid))
            self.assertIn(cid, list(self.states().get("park_once") or {}),
                          "факт блокировки не записан — цикл не остановить")
            blocks = [c for c in self.shim.calls if "block" in c]
            self.assertEqual(1, len(blocks), blocks)

            # Модель так и не встала: старый удерживающий след → снятие по потолку.
            def change(data):
                data["holds"][cid] = {"at": int(time.time()) - (guard.HOLD_MAX_S + 30),
                                      "reason": guard.PARK_REASON}

            guard._mutate(self.cfg, ROOM, change)
            second = self.preflight()
            self.assertIn(cid, second.get("released") or [], second)
            self.assertNotEqual("blocked", self.status(cid))

            # Дальше карточка снова в ready без модели — но ВТОРОЙ раз не блокируем.
            for _step in range(3):
                again = self.preflight()
                self.assertNotIn(cid, again.get("held") or [], again)
            self.assertNotEqual("blocked", self.status(cid),
                                "карточка снова в паузе — цикл «блок ↔ снять» вернулся")
            self.assertEqual(1, len([c for c in self.shim.calls if "block" in c]),
                             "сторож блокировал карточку больше одного раза")
            kinds = [item.get("kind") for item in guard.incidents(self.cfg, ROOM)]
            self.assertEqual(1, kinds.count("held"), kinds)
            self.assertEqual(1, len([k for k in kinds if k.startswith("released")]), kinds)
        finally:
            models_mod.push_model = real

    def test_own_hold_with_exact_reason_is_released(self):
        """Своя пауза с точной причиной и верной моделью снимается (не регресс P1)."""
        self.select_room_model()
        cid = self.card(assignee=WORKER, status="blocked",
                        model=EXPECTED[1], provider=EXPECTED[0])

        def change(data):
            data["holds"][cid] = {"at": int(time.time()) - 5, "reason": guard.PARK_REASON}

        guard._mutate(self.cfg, ROOM, change)
        res = self.preflight()
        self.assertIn(cid, res.get("released") or [], res)
        self.assertNotEqual("blocked", self.status(cid))


class BoardGuardInWatchersTests(Base):
    """Песочница с реальной доской: сторож не пишет даже при полном реестре комнаты."""

    def test_preflight_skips_when_board_is_real(self):
        self.select_room_model()
        cid = self.card(assignee=WORKER, status="ready")
        import os
        from server import board_guard as bg
        saved = bg.real_roots
        os.environ[bg.SANDBOX_ENV] = "1"          # явный признак песочницы
        # «Реальную» доску изображаем подменой корня: настоящий Hermes не трогаем.
        bg.real_roots = lambda cfg=None: [Path(self.cfg.kanban_home)]
        try:
            res = self.preflight()
        finally:
            bg.real_roots = saved
            os.environ.pop(bg.SANDBOX_ENV, None)
        self.assertEqual("sandbox_real_board", res.get("skipped_reason"), res)
        self.assertEqual([], res.get("pinned") or [])
        self.assertEqual([], res.get("held") or [])
        self.assertEqual("ready", self.status(cid), "сторож тронул доску в песочнице")


if __name__ == "__main__":
    unittest.main(verbosity=2)
