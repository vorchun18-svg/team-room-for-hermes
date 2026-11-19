"""W27-QA1: сверка контрактов «сервер ↔ интерфейс» (волна 27, §1–§3).

Зачем отдельный тест
--------------------
Дефект 18.11.2026 вечером случился не в логике, а на СТЫКЕ: сервер отдавал
`approvals[].state = "closed"`, интерфейс такого слова не знал — и двадцать
закрытых пунктов рисовались как «ждёт вашего решения». Словари состояний жили в
двух файлах, и сверяли их «на глаз».

Здесь сверка машинная: python читает js-файл и сравнивает НАБОРЫ. Если кто-то
добавит состояние на сервере и забудет интерфейс (или наоборот) — тест падает.

Что проверяется
---------------
1. `server/stale.py` ↔ `web/js/views/stale-model.js` и `approvals.js`:
   состояния разрешений, состояния пунктов, причины закрытия, «завершённые»
   статусы, ключи счётчика `needs_you_count`, тела `dismiss`/`restore`/`dismiss_stale`.
2. `tools/mock_read.py: needs_you_count` ↔ `server/stale.py: counts` — один и тот же
   ответ на одну и ту же деталь (форма и числа).
3. §2 (автономия) и §3 (дежурства): имена полей из `server/autonomy.py`,
   `server/duties.py` живут и в интерфейсе, и в моке — ни одно не потерялось.
4. Живой сервер на песочнице: формы ответов `dismiss` / `restore` / `dismiss_stale`
   совпадают с моком (мок отвечает теми же ключами).

    python -m unittest tests.test_contract_w27 -v

Реальный `%LOCALAPPDATA%\\hermes`, живой 8777 и VERSION тест не трогает.
"""
from __future__ import annotations

import re
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from server import stale                                                      # noqa: E402
from tests.test_stale import _Base as SandboxAppCase                          # noqa: E402

WEB = ROOT / "web" / "js"
TOOLS = ROOT / "tools"
if str(TOOLS) not in sys.path:
    sys.path.insert(0, str(TOOLS))          # мок живёт «плоскими» импортами (mock_core …)


def js_text(rel: str) -> str:
    return (WEB / rel).read_text(encoding="utf-8")


def set_literal(text: str, name: str) -> set:
    """`export const X = new Set(['a', 'b']);` → {'a','b'} (в т.ч. многострочный)."""
    match = re.search(r"export const %s = new Set\(\[(.*?)\]\);" % re.escape(name), text, re.S)
    if match is None:
        raise AssertionError("в js не найден набор %s" % name)
    return set(re.findall(r"'([^']+)'", match.group(1)))


def object_keys(text: str, name: str) -> set:
    """Ключи объектной константы: `export const X = { a: …, b: … };`."""
    match = re.search(r"export const %s = \{(.*?)\n\};" % re.escape(name), text, re.S)
    if match is None:
        raise AssertionError("в js не найден объект %s" % name)
    return set(re.findall(r"^\s{2}([A-Za-z_][A-Za-z0-9_]*)\s*:", match.group(1), re.M))


#: Состояния, которые интерфейс знает СВЕРХ серверных (W26: подсказка участнику,
#: скрытая информационная строка, «слово, которого экран не знает»). Это не
#: расхождение контракта: сервер таких состояний не отдаёт.
JS_ONLY_APPROVAL_STATES = {"hinted", "acked", "unknown"}


class StateMirrorTest(unittest.TestCase):
    """Наборы состояний сервера и интерфейса — побуквенно."""

    @classmethod
    def setUpClass(cls):
        cls.stale_model = js_text("views/stale-model.js")
        cls.approvals = js_text("views/approvals.js")

    def test_request_finished_matches(self):
        self.assertEqual(set_literal(self.stale_model, "REQUEST_FINISHED"),
                         set(stale.FINISHED_REQUESTS),
                         "завершённые статусы запроса: сервер и интерфейс разошлись")

    def test_approval_states_match(self):
        server = set(stale.APPROVAL_STATES)
        client = object_keys(self.approvals, "APPROVAL_STATES")
        self.assertEqual(client - JS_ONLY_APPROVAL_STATES, server,
                         "состояния разрешения: сервер и интерфейс разошлись")
        # Каждое серверное состояние ОБЯЗАНО иметь подпись: иначе на экране «неизвестно».
        self.assertEqual(server - client, set(),
                         "интерфейс не знает серверных состояний разрешения")

    def test_closed_reasons_match(self):
        self.assertEqual(set(object_keys(self.stale_model, "CLOSED_REASON_RU")),
                         set(stale.CLOSED_REASONS),
                         "причины закрытия: сервер и интерфейс разошлись")
        self.assertEqual(set(stale.REASON_RU), set(stale.CLOSED_REASONS))

    def test_every_non_waiting_state_is_known_to_client(self):
        known = set_literal(self.stale_model, "NOT_WAITING")
        for kind, states in (("approval", stale.APPROVAL_STATES),
                             ("question", stale.ITEM_STATES),
                             ("decision", stale.ITEM_STATES),
                             ("step", stale.ITEM_STATES)):
            for state in states:
                if stale.waits(kind, state):
                    continue
                if kind == "step" and state in stale.STEP_CLOSED_STATES:
                    # `cancelled` — подпись шага, снятого ответом человека; её знает
                    # `step-dialog.js` (свой словарь состояний шага, WAVE14).
                    self.assertIn(state, js_text("views/step-dialog.js"),
                                  "шаг: интерфейс не знает состояния %r" % state)
                    continue
                self.assertIn(state, known,
                              "%s: состояние %r сервер не считает «ждёт», а интерфейс "
                              "может нарисовать его как ожидание" % (kind, state))

    def test_needs_you_count_keys(self):
        # Ключи, которые сервер кладёт в счётчик…
        counter = stale.counts({"open_questions": [], "decisions": [], "approvals": [],
                                "next_steps": []})
        self.assertEqual(set(counter),
                         {"questions", "decisions", "approvals", "steps", "total"})
        # …обязаны читаться интерфейсом: клиент показывает число сервера, а не своё.
        body = re.search(r"export function needsYouCount\(detail\) \{(.*?)\n\}", self.stale_model, re.S)
        self.assertIsNotNone(body, "в stale-model.js пропал needsYouCount")
        for key in counter:
            self.assertIn("raw.%s" % key, body.group(1),
                          "интерфейс не читает `needs_you_count.%s`" % key)

    def test_action_bodies_match_server_contract(self):
        for kind in stale.KINDS:
            self.assertIn("'%s'" % kind, self.stale_model)
        # action:"dismiss"|"restore" — те же слова, что в словаре сервера.
        self.assertIn("action: 'dismiss'", self.stale_model)
        self.assertIn("action: 'restore'", self.stale_model)
        self.assertEqual(set(stale.ACTION_LABELS), {"dismiss", "restore"})
        self.assertIn("scope: 'finished_requests'" if "scope: 'finished_requests'" in self.stale_model
                      else "'finished_requests'", self.stale_model)
        self.assertEqual(set(stale.STALE_SCOPES), {"finished_requests"})

    def test_ui_uses_only_known_kinds(self):
        """Интерфейс не может снять пункт «своего» вида: только четыре вида §1."""
        used = set()
        for rel in ("views/approvals.js", "views/request-aside.js", "views/request-store.js",
                    "views/request-answer.js", "views/today.js"):
            path = WEB / rel
            if not path.exists():
                continue
            used |= set(re.findall(r"(?:dismissBody|restoreBody)\('([a-z]+)'", path.read_text(encoding="utf-8")))
        self.assertTrue(used, "ни одного вызова dismissBody/restoreBody — сверка потеряла смысл")
        self.assertEqual(used - set(stale.KINDS), set(),
                         "интерфейс снимает пункт неизвестного вида")


class CounterParityTest(unittest.TestCase):
    """`needs_you_count` мока и сервера — один ответ на одну деталь."""

    DETAIL = {
        "status": "working",
        "open_questions": [{"id": "q1", "state": "open", "needs_you": True},
                           {"id": "q2", "state": "answered", "answered": True},
                           {"id": "q3", "state": "dismissed", "dismissed": True}],
        "decisions": [{"id": "d1", "status": "proposed", "open": True, "needs_you": True},
                      {"id": "d2", "status": "accepted", "open": False}],
        "approvals": [{"id": "a1", "state": "pending", "actions": [{"id": "dismiss"}],
                       "needs_you": True},
                      {"id": "a2", "state": "closed", "actions": []},
                      {"id": "a3", "state": "granted_once", "actions": []}],
        "next_steps": [{"id": "s1", "state": "open", "needs_you": True},
                       {"id": "s2", "state": "asked"},
                       {"id": "s3", "state": "done"}],
    }

    def test_mock_and_server_agree(self):
        from mock_read import needs_you_count as mock_count       # noqa: WPS433 — мок плоский
        expected = stale.counts(self.DETAIL)
        self.assertEqual(expected, {"questions": 1, "decisions": 1, "approvals": 1,
                                    "steps": 1, "total": 4})
        got = mock_count(self.DETAIL)
        self.assertEqual(set(got), set(expected), "форма счётчика мока ≠ серверной")
        self.assertEqual(got, expected, "мок считает «ждёт вас» иначе, чем сервер")

    def test_total_is_the_sum(self):
        counter = stale.counts(self.DETAIL)
        self.assertEqual(counter["total"], counter["questions"] + counter["decisions"]
                         + counter["approvals"] + counter["steps"])


class AutonomyFieldsTest(unittest.TestCase):
    """§2: имена полей автономии — сервер, интерфейс, мок."""

    #: (поле, где искать) — по контракту §2 и по тому, что реально читает интерфейс.
    #: Что интерфейс обязан читать (по нему он рисует вкладку).
    FIELDS = ("level", "never", "profiles", "preview", "changes", "journal", "items")
    #: Что сервер и мок обязаны отдавать сверх этого — даже если вкладка пока не рисует:
    #: список правил участника (`allowlist`), запреты (`deny`), режим разрешений.
    SERVER_ONLY = ("allowlist", "single_query_mode", "deny", "defaults")

    def test_server_and_client_share_names(self):
        server_src = (ROOT / "server" / "autonomy.py").read_text(encoding="utf-8")
        client_src = js_text("views/autonomy.js")
        mock_src = (TOOLS / "mock_autonomy_fe.py").read_text(encoding="utf-8") \
            if (TOOLS / "mock_autonomy_fe.py").exists() else ""
        for field in self.FIELDS:
            forms = ('"%s"' % field, "'%s'" % field, "def %s(" % field, "%s(cfg" % field)
            self.assertTrue(any(form in server_src for form in forms),
                            "§2: сервер не отдаёт поле %r" % field)
            self.assertTrue(field in client_src,
                            "§2: интерфейс не читает поле %r" % field)
        for field in self.SERVER_ONLY:
            forms = ('"%s"' % field, "'%s'" % field)
            self.assertTrue(any(form in server_src for form in forms),
                            "§2: сервер не отдаёт поле %r" % field)
            if mock_src:
                self.assertTrue(field in mock_src, "§2: мок не отдаёт поле %r" % field)

    def test_server_offers_defaults_even_if_ui_ignores_them(self):
        """§2: `defaults:{never}` — умолчания для списка «только с моего да».

        Интерфейс это поле пока НЕ читает (вкладка показывает уже сохранённый
        список масок) — расхождение записано в `docs/status/w27-qa-tests.md`.
        Сервер обязан его отдавать: иначе подсказку не из чего собрать.
        """
        server_src = (ROOT / "server" / "autonomy.py").read_text(encoding="utf-8")
        self.assertIn('"defaults"', server_src)
        self.assertIn('"never": list(NEVER_DEFAULTS)', server_src)
        self.assertNotIn("defaults", js_text("views/autonomy.js"),
                        "интерфейс начал читать `defaults` — уберите этот тест-маркер")

    def test_conflict_answer_is_409_with_preview(self):
        """§2: без `confirm:true` — 409 и предпросмотр (не молчаливое применение)."""
        src = (ROOT / "server" / "autonomy.py").read_text(encoding="utf-8")
        self.assertIn("409", src)
        self.assertIn("preview", src)
        self.assertIn("confirm", src)


class DutiesFieldsTest(unittest.TestCase):
    """§3: имена полей дежурств — сервер, интерфейс, мок."""

    FIELDS = ("job_id", "on_change", "level", "notify", "source", "template",
              "title", "history", "trash", "proposals", "found", "done")

    def test_server_and_client_share_names(self):
        server_src = "\n".join((ROOT / "server" / name).read_text(encoding="utf-8")
                               for name in ("duties.py", "routes_duties.py", "duty_rules.py"))
        client_src = "\n".join(js_text("views/" + name)
                               for name in ("duties.js", "duty-model.js", "duty-wizard.js"))
        mock_src = (TOOLS / "mock_duties_fe.py").read_text(encoding="utf-8")
        for field in self.FIELDS:
            self.assertIn(field, server_src, "§3: сервер не знает поля %r" % field)
            self.assertIn(field, client_src, "§3: интерфейс не знает поля %r" % field)
            self.assertIn(field, mock_src, "§3: мок не знает поля %r" % field)

    def test_run_result_block_keys(self):
        """Машинный блок `результат` (§3): ключи одинаковы в контракте и правилах."""
        contract = (ROOT / "server" / "contract.py").read_text(encoding="utf-8")
        for key in ("изменилось", "сделано", "обнаружено", "предложения", "ссылки"):
            self.assertIn(key, contract, "§3: в contract.py нет ключа блока «результат»: %r" % key)


class LiveShapesTest(SandboxAppCase):
    """Боевой сервер (в процессе, песочница `test_stale`): формы ответов §1 как у мока.

    Порт и HTTP здесь не нужны: `App.dispatch` — тот же путь обработки запроса, что
    у живого сервера (`tools/e2e_ui.py`, `tools/probe_stale.py` проверяют его по
    сети отдельно). Песочница своя: реальный `%LOCALAPPDATA%\\hermes` только
    читается на слепок, живой 8777 не занимается.
    """

    def _post(self, path: str, body: dict):
        """POST как у живого сервера: отказ приходит кодом и телом, а не исключением."""
        from server.http_util import ApiError
        try:
            return self.app.dispatch("POST", path, {}, body)
        except ApiError as exc:
            return exc.status, exc.payload(), {}

    def _detail(self, rid: str) -> dict:
        """Деталь через песочницу `test_stale` (там же — проверка 200 и разбор ошибок)."""
        return self.detail(rid)

    def test_needs_you_count_shape_live(self):
        detail = self._detail(self.rid())
        counter = detail.get("needs_you_count")
        self.assertIsInstance(counter, dict)
        self.assertEqual(set(counter), {"questions", "decisions", "approvals", "steps", "total"})
        self.assertEqual(counter["total"], sum(counter[k] for k in
                                               ("questions", "decisions", "approvals", "steps")))
        for item in detail.get("open_questions") or []:
            self.assertIn(item.get("state"), stale.ITEM_STATES)
            if item.get("needs_you"):
                self.assertTrue(item.get("actions"), "пункт «ждёт» без действия — тупик")
        for item in detail.get("approvals") or []:
            self.assertIn(item.get("state"), stale.APPROVAL_STATES)
        for key in ("open_questions", "decisions", "next_steps", "approvals"):
            for item in detail.get(key) or []:
                reason = item.get("closed_reason")
                if reason:
                    self.assertIn(reason, stale.CLOSED_REASONS)

    def test_dismiss_and_restore_shapes_live(self):
        rid = self.rid()
        detail = self._detail(rid)
        question = next((q for q in detail.get("open_questions") or [] if q.get("needs_you")), None)
        self.assertIsNotNone(question, "в посеянном запросе нет ждущего вопроса")
        qid = question["id"]

        status, payload, _h = self.actions(
            rid, action="dismiss", target={"kind": "question", "id": qid},
            reason="проверка формы ответа")
        self.assertEqual(status, 200, payload)
        self.assertEqual(payload.get("action"), "dismiss")
        self.assertEqual(payload.get("target"), {"kind": "question", "id": qid})
        self.assertIn("question", payload, "ответ снятия не несёт обновлённый пункт")
        self.assertEqual(payload["question"].get("state"), "dismissed")
        self.assertEqual(payload.get("state"), "dismissed")
        self.assertIn("message", payload)

        # повтор — 409 `already_decided` (клиент гасит тихо)
        status, payload, _h = self.actions(
            rid, action="dismiss", target={"kind": "question", "id": qid})
        self.assertEqual(status, 409, payload)
        self.assertEqual((payload.get("error") or {}).get("code"), "already_decided")

        status, payload, _h = self.actions(
            rid, action="restore", target={"kind": "question", "id": qid})
        self.assertEqual(status, 200, payload)
        self.assertEqual(payload.get("action"), "restore")
        self.assertIn("question", payload)
        self.assertNotEqual(payload["question"].get("state"), "dismissed",
                            "вернувшийся пункт не должен остаться «снятым»")

    def test_dismiss_stale_shape_matches_mock(self):
        room = str(self.room.get("id"))
        status, payload, _h = self._post("/api/rooms/%s/dismiss_stale" % room,
                                         {"scope": "finished_requests"})
        self.assertEqual(status, 200, payload)
        live_closed = payload.get("closed") or {}
        self.assertEqual(set(live_closed), {"questions", "decisions", "approvals", "steps"},
                         "форма `closed` у живого сервера иная, чем в §1")

        from mock_app import App                                     # noqa: WPS433
        from mock_core import FIX_DIR, WEB_DIR                       # noqa: WPS433
        mock = App(WEB_DIR, FIX_DIR, port=0)
        mock_closed = (mock.dismiss_stale("cafe", {"scope": "finished_requests"}) or {}).get("closed") or {}
        self.assertEqual(set(mock_closed), set(live_closed),
                         "мок и живой сервер отвечают разными ключами `closed`")

    def test_wrong_scope_is_400_in_both(self):
        room = str(self.room.get("id"))
        status, payload, _h = self._post("/api/rooms/%s/dismiss_stale" % room,
                                         {"scope": "everything"})
        self.assertEqual(status, 400, payload)
        from mock_app import App                                     # noqa: WPS433
        from mock_core import FIX_DIR, WEB_DIR                       # noqa: WPS433
        mock = App(WEB_DIR, FIX_DIR, port=0)
        with self.assertRaises(Exception):
            mock.dismiss_stale("cafe", {"scope": "everything"})


class RoutesUniqueTest(unittest.TestCase):
    """После слияния восьми пакетов маршрут не должен повторяться: метод+шаблон — один раз."""

    def test_no_duplicate_routes(self):
        import server.app as app_mod                                  # noqa: WPS433
        pairs = [(method, pattern.pattern) for method, pattern, _handler in app_mod.ROUTES]
        self.assertEqual(len(pairs), len(set(pairs)),
                         "в server/app.py есть повтор (метод, шаблон): %s" %
                         sorted({p for p in pairs if pairs.count(p) > 1}))
        # Один обработчик на два адреса (GET+POST одного действия) — законно;
        # проверяем, что одна пара (метод, шаблон) не объявлена дважды — выше.

    def test_handler_defined_once(self):
        src = (ROOT / "server" / "app.py").read_text(encoding="utf-8")
        defs = re.findall(r"^    def (h_[a-z0-9_]+)\(", src, re.M)
        self.assertEqual(len(defs), len(set(defs)),
                         "обработчик объявлен дважды (слияние пакетов): %s" %
                         sorted({d for d in defs if defs.count(d) > 1}))


if __name__ == "__main__":       # pragma: no cover
    unittest.main(verbosity=2)
