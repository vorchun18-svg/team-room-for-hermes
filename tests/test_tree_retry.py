# -*- coding: utf-8 -*-
"""Дерево работ: идемпотентность повтора и смена модели на ходу (WAVE19, пакет T).

Что проверяется:
  * **B-25** — ``POST /api/requests/{id}/actions {action:"retry", idempotency_key}``:
    повторная доставка того же нажатия НЕ запускает действие второй раз и получает
    сохранённый ответ с ``duplicate: true``; разные ключи — два разных действия;
    мусорный ключ — 400 по-русски (а не 500); ключ живёт TTL и не растёт без границ.
  * **B-26** — смена модели участника во время работы НЕ роняет идущую карточку:
    ``apply_profile`` запоминает идущие карточки (``model_pending.json``), а
    ``verify_started`` не делает ``reassign --reclaim`` для них — новая модель
    «применится со следующей задачи»; явный ``restart`` (кнопка «Перезапустить на
    новой модели») по-прежнему снимает карточку с работы.

Стенд — песочница ``tests.helpers.Sandbox`` (как в test_agent_models): CLI Hermes
подменён shim'ом, настоящие агенты не запускаются, реальные конфиги только читаются.

    python -m unittest tests.test_tree_retry -v
"""
from __future__ import annotations

import json
import sqlite3
import sys
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import server.agent_models as models_mod          # noqa: E402
import server.idempotency as idem                 # noqa: E402
from server.http_util import ApiError             # noqa: E402
from tests.test_agent_models import _Base, ROOM   # noqa: E402  общий стенд: песочница + shim

OLD_MODEL = ("ollama-cloud", "glm-5.3-flash")
NEW_MODEL = ("llamacpp", "qwen36", "high")


# --------------------------------------------------------------- B-25: ключ повтора

class IdempotencyUnitTests(unittest.TestCase):
    """Память «этот ключ уже выполнялся» — без сервера и без песочницы."""

    def test_valid_key_accepts_client_keys(self):
        assert idem.valid_key('retry-t_abc-1m2n3o4p-deadbeef')
        self.assertTrue(idem.valid_key('a' * 6))
        self.assertTrue(idem.valid_key('r' * 120))
        for bad in ('', 'short', 'with space', 'ключ-русский', 'x' * 121, None, 5, 'a\nb'):
            self.assertFalse(idem.valid_key(bad), bad)

    def test_ensure_key_refuses_bad_key_in_russian(self):
        with self.assertRaises(idem.IdempotencyError) as ctx:
            idem.ensure_key('no')
        self.assertIn('idempotency_key', ctx.exception.message)
        self.assertEqual(ctx.exception.detail.get('field'), 'idempotency_key')

    def test_replay_returns_same_payload_and_expires(self):
        now = [1000.0]
        guard = idem.Idempotency(ttl=10, clock=lambda: now[0])
        self.assertIsNone(guard.replay('t_1:abc'))
        guard.remember('t_1:abc', {'ok': True, 'action': 'retry'})
        again = guard.replay('t_1:abc')
        self.assertEqual(again, {'ok': True, 'action': 'retry'})
        again['ok'] = False                       # копия, а не ссылка на сохранённое
        self.assertTrue(guard.replay('t_1:abc')['ok'])
        now[0] += 11
        self.assertIsNone(guard.replay('t_1:abc'), 'после TTL ключ свободен')

    def test_scope_separates_actions_and_forget(self):
        guard = idem.Idempotency(ttl=100, clock=lambda: 0.0)
        guard.remember('k1', {'a': 1}, scope='retry')
        self.assertIsNone(guard.replay('k1', scope='other'))
        self.assertEqual(guard.replay('k1', scope='retry'), {'a': 1})
        guard.forget('k1', scope='retry')
        self.assertIsNone(guard.replay('k1', scope='retry'))

    def test_memory_does_not_grow_without_bound(self):
        guard = idem.Idempotency(ttl=1000, clock=lambda: 0.0, max_items=5)
        for i in range(50):
            guard.remember('key-%d' % i, {'i': i})
        self.assertLessEqual(guard.size(), 5)


class RetryIdempotencyApiTests(_Base):
    """B-25: повторное нажатие «Повторить» не создаёт дублей (HTTP-уровень)."""

    def setUp(self):
        super().setUp()
        idem.reset()

    def _blocked_card(self, rid: str, title='Упавшая задача') -> str:
        """Карточка-срез внутри запроса (сервер проверяет принадлежность дереву)."""
        kids = self.sa.decompose(rid, [{'title': title, 'assignee': 'biz-cfo',
                                        'status': 'blocked'}])
        return kids[0]

    def _retry(self, rid, task_id, key):
        body = {'action': 'retry', 'task_id': task_id, 'idempotency_key': key}
        try:
            return self.app.dispatch('POST', '/api/requests/%s/actions' % rid, {}, body)
        except ApiError as exc:
            return exc.status, exc.payload(), {}

    def test_same_key_runs_action_once_and_returns_duplicate(self):
        rid = self.new_root('Повтор без дублей')
        card = self._blocked_card(rid)
        key = 'retry-%s-1a2b3c-abcdef' % card

        status, body, _h = self._retry(rid, card, key)
        self.assertEqual(status, 200, body)
        self.assertTrue(body.get('ok'))
        self.assertIsNone(body.get('duplicate'), 'первый раз — обычный ответ')
        unblocks = [c for c in self.shim.calls if 'unblock' in c]
        self.assertEqual(len(unblocks), 1, self.shim.calls)

        status, again, _h = self._retry(rid, card, key)
        self.assertEqual(status, 200, again)
        self.assertTrue(again.get('duplicate'), 'повтор того же нажатия — duplicate')
        self.assertEqual(again.get('action'), 'retry')
        self.assertEqual([c for c in self.shim.calls if 'unblock' in c], unblocks,
                         'действие второй раз не выполнялось')

    def test_different_keys_are_different_actions(self):
        rid = self.new_root('Два разных нажатия')
        card = self._blocked_card(rid)
        a = 'retry-%s-111111-aaaaaa' % card
        b = 'retry-%s-222222-bbbbbb' % card
        st1, body1, _h = self._retry(rid, card, a)
        st2, body2, _h = self._retry(rid, card, b)
        self.assertEqual((st1, st2), (200, 200))
        self.assertIsNone(body2.get('duplicate'))
        self.assertEqual(len([c for c in self.shim.calls if 'unblock' in c]), 2)

    def test_garbage_key_is_400_not_500(self):
        rid = self.new_root('Кривой ключ')
        card = self._blocked_card(rid)
        for bad in ('no', 'with space', ''):
            status, body, _h = self._retry(rid, card, bad)
            self.assertEqual(status, 400, (bad, body))
            detail = (body.get('error') or {}).get('detail') or {}
            self.assertEqual(detail.get('field'), 'idempotency_key', body)

    def test_retry_without_key_still_works(self):
        """Старый клиент/мок без ключа — обратная совместимость."""
        rid = self.new_root('Без ключа')
        card = self._blocked_card(rid)
        try:
            status, body, _h = self.app.dispatch(
                'POST', '/api/requests/%s/actions' % rid, {},
                {'action': 'retry', 'task_id': card})
        except ApiError as exc:
            status, body = exc.status, exc.payload()
        self.assertEqual(status, 200, body)
        self.assertTrue(body.get('ok'))


# --------------------------------------------------- B-26: смена модели на ходу

class ModelChangeOnRunningCardTests(_Base):
    """Смена модели участника не прерывает работу идущей карточки (B-26)."""

    def setUp(self):
        super().setUp()
        models_mod._SESSION_CACHE.clear()
        # Класс делит песочницу: гасим «идущие» карточки прошлых тестов.
        con = self.sa.connect()
        try:
            con.execute("UPDATE tasks SET status='done' WHERE tenant='room:cafe' "
                        "AND status='running'")
            con.commit()
        finally:
            con.close()
        path = Path(self.cfg.state_dir) / ROOM / 'model_pending.json'
        try:
            path.unlink()
        except OSError:
            pass
        # Свой state.db профилей: реальные сессии Hermes в тестах не читаем.
        self._state = Path(self.sb.root) / 'state-tree.db'
        con = sqlite3.connect(str(self._state))
        try:
            con.execute('CREATE TABLE IF NOT EXISTS sessions (id TEXT PRIMARY KEY, '
                        'model TEXT, billing_provider TEXT)')
            con.commit()
        finally:
            con.close()
        self._real_state_path = models_mod.state_path
        models_mod.state_path = lambda cfg, profile, _p=self._state: _p

    def tearDown(self):
        models_mod.state_path = self._real_state_path
        models_mod._SESSION_CACHE.clear()
        super().tearDown()

    # --- стенд ---------------------------------------------------------
    def _running_card(self, assignee='biz-cfo', started_at=None, model=OLD_MODEL[1],
                      provider=OLD_MODEL[0], title='Идёт работа') -> tuple[str, int]:
        """Карточка в работе на СТАРОЙ модели: как в жизни перед сменой модели."""
        card = self.sa.card(title=title, assignee=assignee, status='running',
                            tenant='room:cafe', created_by='default')
        started = int(started_at or (time.time() - 600))
        con = self.sa.connect()
        try:
            con.execute('UPDATE tasks SET status = ?, started_at = ? WHERE id = ?',
                        ('running', started, card))
            con.execute('INSERT INTO task_runs (task_id, profile, status, started_at, metadata) '
                        'VALUES (?,?,?,?,?)',
                        (card, assignee, 'running', started,
                         json.dumps({'worker_session_id': 'sess-%s' % card})))
            run_id = con.execute('SELECT max(id) FROM task_runs WHERE task_id = ?',
                                 (card,)).fetchone()[0]
            con.execute('UPDATE tasks SET current_run_id = ? WHERE id = ?', (run_id, card))
            con.execute('INSERT INTO task_events (task_id, run_id, kind, payload, created_at) '
                        'VALUES (?,?,?,?,?)',
                        (card, run_id, 'owner_admitted',
                         json.dumps({'session_id': 'sess-%s' % card}), started + 1))
            con.commit()
        finally:
            con.close()
        self._session(assignee, 'sess-%s' % card, model, provider)
        return card, started

    def _session(self, profile, session_id, model, provider):
        """Сессия воркера с фактической моделью (как пишет Hermes в state.db)."""
        con = sqlite3.connect(str(self._state))
        try:
            con.execute('INSERT OR REPLACE INTO sessions (id, model, billing_provider) '
                        'VALUES (?,?,?)', (session_id, model, provider))
            con.commit()
        finally:
            con.close()

    def _status(self, card: str) -> str:
        row = self.sb.one('SELECT status FROM tasks WHERE id = ?', (card,))
        return str(row['status']) if row else ''

    # --- тесты ---------------------------------------------------------
    def test_change_model_while_running_does_not_interrupt_work(self):
        """Ровно жалоба Михаила: поменял модель во время работы — карточка упала."""
        card, started = self._running_card()
        self.shim.calls.clear()

        self.set_model('biz-cfo')                      # PUT модели, пока карточка идёт

        self.assertEqual(self._status(card), 'running', 'работа не прервана')
        self.assertEqual([c for c in self.shim.calls if 'reassign' in c], [],
                         'сменa модели не снимает карточку с работы')
        note = models_mod.pending_marked(self.cfg, ROOM, card)
        self.assertIsNotNone(note, 'карточка отмечена «применится со следующей задачи»')
        self.assertEqual(note.get('started_at'), started)

    def test_verify_reports_pending_and_keeps_card_running(self):
        card, _started = self._running_card()
        self.set_model('biz-cfo')
        self.shim.calls.clear()

        res = models_mod.verify_started(self.cfg, ROOM)

        self.assertEqual([p['id'] for p in res['pending']], [card], res)
        self.assertEqual(res['mismatched'], [], 'это не «ушла не на ту модель»')
        self.assertEqual(res['fixed'], [], 'никакого reassign --reclaim')
        self.assertEqual(self._status(card), 'running')
        self.assertEqual([c for c in self.shim.calls if 'reassign' in c], [])

    def test_new_attempt_after_change_is_checked_again(self):
        """Новая попытка уже обязана идти на выбранной модели — расхождение снова ловим."""
        card, started = self._running_card()
        self.set_model('biz-cfo')                     # модель комнаты: llamacpp/qwen36
        # новая попытка на СТАРОЙ модели (гонка допуска) — уже после смены
        con = self.sa.connect()
        try:
            con.execute("INSERT INTO task_runs (task_id, profile, status, started_at, metadata) "
                        "VALUES (?,?,?,?,?)",
                        (card, 'biz-cfo', 'running', started + 30,
                         json.dumps({'worker_session_id': 'sess-late'})))
            run_id = con.execute('SELECT max(id) FROM task_runs WHERE task_id = ?',
                                 (card,)).fetchone()[0]
            con.execute('UPDATE tasks SET current_run_id = ?, started_at = ? WHERE id = ?',
                        (run_id, started + 30, card))
            con.commit()
        finally:
            con.close()
        self._session('biz-cfo', 'sess-late', OLD_MODEL[1], OLD_MODEL[0])
        self.shim.calls.clear()

        res = models_mod.verify_started(self.cfg, ROOM)

        self.assertEqual(res['pending'], [], 'отметка про старую попытку снята')
        self.assertEqual([m['id'] for m in res['mismatched']], [card], res)
        self.assertEqual(res['fixed'], [card], 'настоящую гонку допуска перехватываем')
        self.assertIsNone(models_mod.pending_marked(self.cfg, ROOM, card))

    def test_explicit_restart_still_interrupts_and_clears_pending(self):
        """«Перезапустить на новой модели» — отдельное явное действие: работу прерывает."""
        card, _started = self._running_card()
        self.set_model('biz-cfo')
        self.assertIsNotNone(models_mod.pending_marked(self.cfg, ROOM, card))
        self.shim.calls.clear()

        res = models_mod.restart_running(self.cfg, ROOM, 'biz-cfo')

        self.assertEqual(res['restarted'], 1, res)
        self.assertEqual(res['cards'], [card])
        self.assertTrue(any('reassign' in c for c in self.shim.calls), self.shim.calls)
        self.assertIsNone(models_mod.pending_marked(self.cfg, ROOM, card),
                          'после явного перезапуска ожидание снято')

    def test_pending_marks_helpers_roundtrip(self):
        card = self.sa.card(title='Отметка', assignee='biz-cfo', status='running',
                            tenant='room:cafe', created_by='default')
        marked = models_mod.mark_pending(self.cfg, ROOM, 'biz-cfo',
                                         [{'id': card, 'started_at': 1234567890}], model='qwen36')
        self.assertEqual(marked, [card])
        note = models_mod.pending_marked(self.cfg, ROOM, card)
        self.assertEqual((note['profile'], note['started_at'], note['model']),
                         ('biz-cfo', 1234567890, 'qwen36'))
        self.assertTrue(models_mod.clear_pending(self.cfg, ROOM, card))
        self.assertIsNone(models_mod.pending_marked(self.cfg, ROOM, card))
        self.assertFalse(models_mod.clear_pending(self.cfg, ROOM, card), 'повторно снимать нечего')


if __name__ == '__main__':  # pragma: no cover
    unittest.main()
