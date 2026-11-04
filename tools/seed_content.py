"""Содержимое песочницы: деревья карточек Лето во всех состояниях запроса.

Отдельный модуль, чтобы `tools/seed_sandbox.py` оставался про запись в БД.
Id детерминированы: `t_` + sha1("<slug>:<key>")[:8] — тесты могут на них
ссылаться, а повторная сборка в тот же root даёт те же значения.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

BOARD = "team"
ROOM = "cafe"
ROOM_TITLE = "Лето"
TENANT = "room:cafe"
BASE_TS = int(datetime(2026, 10, 28, 9, 31, tzinfo=timezone(timedelta(hours=-3))).timestamp())

CONTRACT = """
<<<КОМНАТА-КОНТРАКТ (относится ТОЛЬКО к финальному ответу корневой карточки; в подзадачи не копировать)
Комната: Лето. Контекст проекта (читать первым): STATE.md, Decisions.md.
Корневая карточка завершается kanban_complete(summary=<готовый ответ Михаилу в Markdown, по-русски, ОДИН связный текст>, metadata={...}).
metadata: next_steps [{text, owner, due}], open_questions [{text, for}], decisions [{text, why, status}],
stage {current, phases [{id, title, status, note}]}, future_questions [...], artifacts [{path, title, kind}],
report "<путь к report.json>".
КОМНАТА-КОНТРАКТ>>>"""


def _tid(slug: str, key: str) -> str:
    """Детерминированный id карточки — тесты могут на него ссылаться."""
    return "t_" + hashlib.sha1(f"{slug}:{key}".encode("utf-8")).hexdigest()[:8]


def _ts(minutes: int) -> int:
    return BASE_TS + minutes * 60


def _body(text: str) -> str:
    return f"{text.strip()}\n{CONTRACT}"


def _card(slug: str, key: str, title: str, *, status: str | None = None,
          assignee: str | None = None,
          tenant: str | None = TENANT, created_by: str = "Михаил", created_at: int | None = None,
          request: str | None = None, started_at: int | None = None,
          completed_at: int | None = None, runs: tuple = (), comments: tuple = (),
          events: tuple = (), idempotency_key: str | None = None,
          consecutive_failures: int = 0, last_failure_error: str | None = None) -> dict:
    # Статус не задан → done, если карточка завершена, иначе blocked (не стартует).
    if status is None:
        status = "done" if completed_at else "blocked"
    return {
        "id": _tid(slug, key), "title": title, "status": status, "assignee": assignee,
        "tenant": tenant, "created_by": created_by, "created_at": created_at or BASE_TS,
        "body": _body(request) if request else CONTRACT.strip(),
        "started_at": started_at, "completed_at": completed_at,
        "idempotency_key": idempotency_key, "runs": list(runs), "comments": list(comments),
        "events": list(events), "consecutive_failures": consecutive_failures,
        "last_failure_error": last_failure_error,
    }


def _run(profile: str | None, status: str, outcome: str | None, summary: str,
         metadata: dict | None, *, at: int, ended: int | None = None,
         error: str | None = None) -> dict:
    return {"profile": profile, "status": status, "outcome": outcome, "summary": summary,
            "metadata": metadata, "started_at": at, "ended_at": ended or at + 420,
            "error": error}


def _comment(author: str, text: str, at: int) -> dict:
    return {"author": author, "body": text, "created_at": at}


def _event(kind: str, payload: dict | None, at: int) -> dict:
    return {"kind": kind, "payload": payload or {}, "created_at": at}


# ---------------------------------------------------------------- наполнение


def vault_files(vault: Path) -> dict[str, Path]:
    """Файлы проекта в песочном Vault — на них ссылаются artifacts."""
    files = {
        "state": vault / "Лето" / "STATE.md",
        "unit": vault / "Лето" / "01-Strategy" / "unit-economics.md",
        "packaging": vault / "Лето" / "03-Ops" / "packaging-quotes.md",
        "decisions": vault / "Лето" / "Decisions.md",
        "svod": vault / "Лето" / "00-Director" / "Tasks" / "svod-leto-2026-10-27.md",
    }
    files["state"].parent.mkdir(parents=True, exist_ok=True)
    files["state"].write_text(
        "# Лето — состояние проекта\n\n"
        "Этап: упаковка. Бренд «Лето», проверка в Роспатенте: по классам 30/43/35.\n"
        "Заявление на ИП подано 27.10.2026, ждём свидетельство о регистрации.\n"
        "Кухня во Заозёрье, тестовая мощность 150 моти в неделю.\n", encoding="utf-8")
    files["unit"].parent.mkdir(parents=True, exist_ok=True)
    files["unit"].write_text(
        "# Юнит-экономика набора из 6 моти\n\n"
        "- Себестоимость набора: R$ 21,40 (моти R$ 12,10 + упаковка R$ 7,30 + наклейка R$ 2,00)\n"
        "- Цена набора: R$ 42,00, маржа 49%\n"
        "- Доставка по Заозёрью: R$ 9,00, забирает 21% маржи\n", encoding="utf-8")
    files["packaging"].parent.mkdir(parents=True, exist_ok=True)
    files["packaging"].write_text(
        "# Котировки упаковки (Заозёрье)\n\n"
        "| Поставщик | Лоток 18×12, крафт | MOQ | Срок |\n|---|---|---|---|\n"
        "| Embalagens городе | R$ 6,90 | 100 | 5 дней |\n"
        "| Kraft Sul | R$ 7,40 | 50 | 7 дней |\n"
        "| Caixa & Cia | R$ 6,20 | 250 | 12 дней |\n", encoding="utf-8")
    files["decisions"].write_text(
        "# Решения Михаила\n\n"
        "- 24.09.2026 — название бренда «Лето» (принято)\n"
        "- 26.10.2026 — старт с набора из 6 моти, не поштучно (принято)\n"
        "- 27.10.2026 — подавать в Роспатент после подтверждения ИП (принято)\n", encoding="utf-8")
    files["svod"].parent.mkdir(parents=True, exist_ok=True)
    files["svod"].write_text(
        "# Свод по проекту Лето — 27.10.2026\n\n"
        "Бренд и легализация почти закрыты, упаковка в работе, производство и продажи\n"
        "не начаты. Главный блокер — канал связи и решение по подаче Роспатент.\n", encoding="utf-8")
    return files


def report_spec(title: str) -> dict:
    """Копия эталонного отчёта reportkit (или минимальный валидный, если его нет)."""
    example = Path(__file__).resolve().parent.parent / "reportkit" / "examples" / "unit-economics.report.json"
    if example.is_file():
        try:
            spec = json.loads(example.read_text(encoding="utf-8"))
            spec["title"] = title
            return spec
        except (ValueError, OSError):
            pass
    return {"version": 1, "title": title,
            "summary": "Себестоимость и маржа набора из 6 моти",
            "blocks": [{"type": "text", "text": "Отчёт не скопирован: reportkit/examples недоступен."}]}


# --- деревья состояний -------------------------------------------------------


def _tree_queued() -> dict:
    slug = "r-20261028-0931-a1b2"
    request = ("Разбери, стоит ли Лето ставить витрину с моти на фестивале Sabores de Заозёрье "
               "14–16 ноября. Нужны смета участия, прогноз продаж на три дня, кто из команды нужен "
               "на месте и что это даёт запуску. Если выгодно — план на две недели.")
    return {
        "name": "queued", "slug": slug, "root_key": "root", "child_keys": [],
        "cards": {"root": _card(
            slug, "root", "Лето: витрина на фестивале Sabores de Заозёрье — участвуем?",
            status="triage", assignee=None, created_at=_ts(0), request=request,
            idempotency_key=slug,
            events=(_event("created", {"status": "triage"}, _ts(0)),))},
        "links": [], "extra_cards": {},
    }


def _tree_working() -> dict:
    slug = "r-20261028-0940-b2c3"
    request = ("Собери варианты упаковки набора из 6 моти и смету: лоток, крафт-коробка, наклейка. "
               "Ограничение — себестоимость упаковки не выше R$ 8 на набор, тираж от 100 штук. "
               "Проверь, что писать на этикетке по СанПиН.")
    root = _card(slug, "root", "Лето: упаковка набора из 6 моти — варианты и смета",
                 status="todo", assignee="biz-director", created_at=_ts(10), request=request,
                 idempotency_key=slug,
                 events=(_event("created", {"status": "triage"}, _ts(10)),
                         _event("decomposed", {"child_ids": [
                             _tid(slug, "legal"), _tid(slug, "ops"),
                             _tid(slug, "marketing"), _tid(slug, "strategy")],
                             "root_assignee": "biz-director"}, _ts(14))))
    cards = {
        "root": root,
        "legal": _card(slug, "legal", "Срез 01-Legal: что писать на этикетке набора моти по СанПиН",
                       assignee="biz-lawyer", created_by="auto-decomposer", created_at=_ts(15),
                       completed_at=_ts(44),
                       runs=(_run("biz-lawyer", "done", "completed",
                                  "Срез 01-Legal: обязательные поля этикетки — состав, аллергены "
                                  "(молоко, соя), вес нетто, срок хранения при −18 °C, ИНН после ИП. "
                                  "СанПиН требует также «produto artesanal» без медицинских обещаний.",
                                  {"stage": {"legal": "готов: чек-лист этикетки на 12 пунктов"}},
                                  at=_ts(16)),)),
        "ops": _card(slug, "ops", "Срез 03-Ops: смета упаковки у 10 поставщиков Заозёрья",
                     status="running", assignee="biz-ops", created_by="auto-decomposer",
                     created_at=_ts(15), started_at=_ts(18),
                     runs=(_run("biz-ops", "running", None, "", None, at=_ts(18), ended=_ts(18)),),
                     events=(_event("claimed", {"profile": "biz-ops"}, _ts(17)),)),
        "marketing": _card(slug, "marketing", "Срез 04-Marketing: как набор из 6 моти выглядит на витрине",
                           status="running", assignee="biz-marketing", created_by="auto-decomposer",
                           created_at=_ts(15), started_at=_ts(19),
                           runs=(_run("biz-marketing", "running", None, "", None,
                                      at=_ts(19), ended=_ts(19)),),
                           events=(_event("claimed", {"profile": "biz-marketing"}, _ts(19)),)),
        "strategy": _card(slug, "strategy", "Срез 05-Strategy: цена набора из 6 моти и маржа",
                          status="ready", assignee="biz-strategy", created_by="auto-decomposer",
                          created_at=_ts(15)),
    }
    links = [(cards[k]["id"], root["id"]) for k in ("legal", "ops", "marketing", "strategy")]
    cards["root"]["comments"] = [_comment("mikhail", "Упаковка важнее цены: набор должен выглядеть "
                                          "как подарок, а не как коробка с печеньем.", _ts(12))]
    return {"name": "working", "slug": slug, "root_key": "root",
            "child_keys": ["legal", "ops", "marketing", "strategy"],
            "cards": cards, "links": links, "extra_cards": {}}


def _tree_reviewing() -> dict:
    slug = "r-20261028-0952-c3d4"
    request = ("Предложи имя для линейки моти со вкусом кайпириньи и проверь его по Роспатент. "
               "Нужно 3 варианта с обоснованием, проверка классов 30/43/35 и рекомендация.")
    root = _card(slug, "root", "Лето: имя для линейки моти с кайпириньей",
                 status="done", assignee="biz-director", created_at=_ts(20), request=request,
                 started_at=_ts(26), completed_at=_ts(88), idempotency_key=slug,
                 runs=(_run("biz-director", "done", "completed",
                            "Рекомендую «Caipi Лето»: два слога, читается по-португальски, "
                            "не пересекается с «Лето» по звучанию. Резерв — «Moti Caipi» и "
                            "«Doce Caipi». Проверка Роспатент: 30 и 43 свободны, 35 — занят частично.",
                            {"next_steps": [{"text": "Подать в Роспатент по классам 30 и 43 на «Caipi Лето»",
                                             "owner": "biz-lawyer", "due": "2026-11-06"}],
                             "stage": {"current": "brand", "phases": [
                                 {"id": "brand", "title": "Бренд и товарный знак", "status": "active",
                                  "note": "Три варианта имени проверены, ждём подачи"}]}},
                            at=_ts(70)),),
                 events=(_event("created", {"status": "triage"}, _ts(20)),
                         _event("decomposed", {"child_ids": [
                             _tid(slug, "lawyer"), _tid(slug, "marketing"), _tid(slug, "cfo")],
                             "root_assignee": "biz-director"}, _ts(24)),
                         _event("completed", {"status": "done"}, _ts(88))))
    cards = {
        "root": root,
        "lawyer": _card(slug, "lawyer", "Срез 01-Legal: проверка трёх имён по Роспатент (кл. 30/43/35)",
                        assignee="biz-lawyer", created_by="auto-decomposer", created_at=_ts(25),
                        completed_at=_ts(62),
                        runs=(_run("biz-lawyer", "done", "completed",
                                   "«Caipi Лето» — свободно по 30 и 43, кл. 35 занят частично другим "
                                   "заявителем из Сан-Паулу; «Moti Caipi» — свободно по 30; "
                                   "«Doce Caipi» — есть похожая заявка 2024 года.",
                                   {"stage": {"legal": "готов: проверка в Роспатенте по трём именам"}},
                                   at=_ts(26)),)),
        "marketing": _card(slug, "marketing", "Срез 04-Marketing: как звучат имена для аудитории 25–40",
                           assignee="biz-marketing", created_by="auto-decomposer",
                           created_at=_ts(25), completed_at=_ts(58),
                           runs=(_run("biz-marketing", "done", "completed",
                                      "«Caipi Лето» лучше всего запоминается в опросе 12 человек; "
                                      "«Doce Caipi» путают с названием кашасы.",
                                      {"stage": {"brand": "готово: тест имён на 12 респондентах"}},
                                      at=_ts(26)),)),
        "cfo": _card(slug, "cfo", "Срез 02-CFO: цена линейки и вклад в маржу",
                     assignee="biz-cfo", created_by="auto-decomposer", created_at=_ts(25),
                     completed_at=_ts(56),
                     runs=(_run("biz-cfo", "done", "completed",
                                "Линейка из 3 вкусов даёт +8% к среднему чеку, но требует "
                                "отдельной партии 60 моти в неделю.",
                                {"spend": "0 R$"}, at=_ts(26)),)),
        "critic": _card(slug, "critic", "Критика: имя «Caipi Лето» — проверить риски Роспатент кл. 35",
                        status="running", assignee="biz-critic", created_at=_ts(90),
                        started_at=_ts(92),
                        runs=(_run("biz-critic", "running", None, "", None, at=_ts(92), ended=_ts(92)),),
                        events=(_event("claimed", {"profile": "biz-critic"}, _ts(91)),)),
    }
    links = [(cards[k]["id"], root["id"]) for k in ("lawyer", "marketing", "cfo")]
    links.append((root["id"], cards["critic"]["id"]))
    return {"name": "reviewing", "slug": slug, "root_key": "root",
            "child_keys": ["lawyer", "marketing", "cfo", "critic"],
            "cards": cards, "links": links, "extra_cards": {"critic": cards["critic"]["id"]}}


def _tree_needs_you() -> dict:
    slug = "r-20261028-1005-d4e5"
    request = ("Определи, через какой канал Лето начинает первые продажи: Instagram, WhatsApp "
               "или офлайн-витрина в Заозёрье. Нужен план на 15 разговоров и бюджет.")
    question = ("Какой канал берём для первых 15 разговоров: Instagram (витрина + прямые), "
                "WhatsApp (рассылка по знакомым) или офлайн-витрина в пятницу?")
    root = _card(slug, "root", "Лето: канал первых продаж — Instagram, WhatsApp или витрина?",
                 status="todo", assignee="biz-director", created_at=_ts(30), request=request,
                 idempotency_key=slug,
                 comments=(_comment("biz-runner", "Без ответа Михаила поле D19 стоит: канал "
                                     "не выбран, 32 дня без прогресса.", _ts(52)),),
                 events=(_event("created", {"status": "triage"}, _ts(30)),
                         _event("decomposed", {"child_ids": [
                             _tid(slug, "channels"), _tid(slug, "d19")],
                             "root_assignee": "biz-director"}, _ts(34))))
    cards = {
        "root": root,
        "channels": _card(slug, "channels", "Срез 04-Marketing: сравнение трёх каналов по цене контакта",
                          assignee="biz-marketing", created_by="auto-decomposer",
                          created_at=_ts(35), completed_at=_ts(50),
                          runs=(_run("biz-marketing", "done", "completed",
                                     "Instagram даёт дешевле всего первый контакт (R$ 0,80), "
                                     "WhatsApp — выше конверсия в заказ у знакомых, "
                                     "офлайн-витрина требует R$ 350 на аренду места.",
                                     {"next_steps": ["Свести стоимость контакта в таблицу"],
                                      "stage": {"sales": "в работе: сравнение каналов"}},
                                     at=_ts(36)),)),
        "d19": _card(slug, "d19", "Поле D19: 15 разговоров с кондитерскими Заозёрья",
                     status="blocked", assignee="biz-runner", created_by="auto-decomposer",
                     created_at=_ts(35),
                     runs=(_run("biz-runner", "blocked", "blocked",
                                "Остановился: канал не выбран, начинать 15 разговоров нечем. "
                                "Нужен ответ Михаила по Q1.",
                                {"open_questions": [{"text": question, "for": "mikhail"}],
                                 "stage": {"sales": "в работе → ждёт решения Михаила"}},
                                at=_ts(40), ended=_ts(46)),
                           ),
                     comments=(_comment("biz-runner", "Вопрос к Михаилу: " + question, _ts(47)),),
                     events=(_event("blocked", {"reason": "ждёт ответа Михаила"}, _ts(46)),)),
    }
    links = [(cards["channels"]["id"], root["id"]), (cards["d19"]["id"], root["id"])]
    return {"name": "needs_you", "slug": slug, "root_key": "root",
            "child_keys": ["channels", "d19"], "cards": cards, "links": links,
            "extra_cards": {"blocked_child": cards["d19"]["id"], "question": question}}


def _tree_done_modern() -> dict:
    """Современный контракт ARCH §5 + интерактивный отчёт + критик и доработка."""
    slug = "r-20261027-1815-e5f6"
    request = ("Посчитай юнит-экономику набора из 6 моти: себестоимость, цену, маржу и точку "
               "безубыточности на месяц. Учти упаковку, доставку и налоги ИП. Нужен отчёт с "
               "таблицами по сценариям цены.")
    root = _card(slug, "root", "Лето: юнит-экономика набора из 6 моти — сходится ли",
                 status="done", assignee="biz-director", created_at=_ts(100), request=request,
                 started_at=_ts(140), completed_at=_ts(196), idempotency_key=slug,
                 runs=(_run("biz-director", "done", "completed",
                            "Набор из 6 моти сходится при цене от R$ 39: себестоимость R$ 21,40, "
                            "маржа 45–49%. При цене R$ 42 и доставке за счёт клиента точка "
                            "безубыточности — 68 наборов в месяц. Ниже R$ 36 набор уходит в ноль.",
                            {"next_steps": [{"text": "Свести финальную спеку упаковки набора (лоток 18×12, крафт, наклейка)",
                                             "owner": "biz-ops", "due": "2026-10-31"}],
                             "stage": {"current": "packaging", "phases": [
                                 {"id": "packaging", "title": "Упаковка", "status": "active"}]}},
                            at=_ts(180), ended=_ts(182)),
                           _run("biz-director", "done", "completed",
                                "Версия 2 после критики: маржа пересчитана с учётом роста какао "
                                "на 7% — набор из 6 моти держит 46% при цене R$ 42, "
                                "точка безубыточности 71 набор в месяц.",
                                _modern_metadata(slug), at=_ts(190), ended=_ts(196)),),
                 events=(_event("created", {"status": "triage"}, _ts(100)),
                         _event("decomposed", {"child_ids": [
                             _tid(slug, "cfo"), _tid(slug, "ops"),
                             _tid(slug, "marketing"), _tid(slug, "lawyer"),
                             _tid(slug, "synth")], "root_assignee": "biz-director"}, _ts(104)),
                         _event("completed", {"status": "done"}, _ts(196))))
    cards = {
        "root": root,
        "cfo": _card(slug, "cfo", "Срез 02-CFO: себестоимость и маржа набора из 6 моти",
                     assignee="biz-cfo", created_by="auto-decomposer", created_at=_ts(105),
                     completed_at=_ts(150),
                     runs=(_run("biz-cfo", "done", "completed",
                                "Себестоимость набора R$ 21,40: моти R$ 12,10, упаковка R$ 7,30, "
                                "наклейка R$ 2,00. При цене R$ 42 маржа 49%, после налога ИП — 46%.",
                                {"stage": {"production": "готов: калькуляция себестоимости"}},
                                at=_ts(106)),)),
        "ops": _card(slug, "ops", "Срез 03-Ops: упаковка и логистика набора",
                     assignee="biz-ops", created_by="auto-decomposer", created_at=_ts(105),
                     completed_at=_ts(146),
                     runs=(_run("biz-ops", "done", "completed",
                                "Лоток 18×12 крафт — R$ 6,90 у Embalagens городе, MOQ 100. "
                                "Доставка по Заозёрью R$ 9, забирает 21% маржи.",
                                {"stage": {"packaging": "готово: котировка и MOQ"}}, at=_ts(106)),)),
        "marketing": _card(slug, "marketing", "Срез 04-Marketing: цена набора на витрине",
                           assignee="biz-marketing", created_by="auto-decomposer",
                           created_at=_ts(105), completed_at=_ts(142),
                           runs=(_run("biz-marketing", "done", "completed",
                                      "Цена R$ 42 читается как подарок; R$ 36 — как «просто коробка». "
                                      "В тесте 9 из 12 выбрали набор за R$ 42.",
                                      {"stage": {"sales": "готово: тест цены"}}, at=_ts(106)),)),
        "lawyer": _card(slug, "lawyer", "Срез 01-Legal: налоги ИП и этикетка",
                        assignee="biz-lawyer", created_by="auto-decomposer", created_at=_ts(105),
                        completed_at=_ts(138),
                        runs=(_run("biz-lawyer", "done", "completed",
                                   "ИП: DAS ~R$ 76 в месяц, лимит оборота R$ 81 000 в год. "
                                   "На этикетке — состав, аллергены, вес нетто, «produto artesanal».",
                                   {"stage": {"legal": "готов: налоги и этикетка"}}, at=_ts(106)),)),
        "synth": _card(slug, "synth", "Свести юнит-экономику в один ответ",
                       assignee="biz-director", created_by="auto-decomposer",
                       created_at=_ts(105), completed_at=_ts(178),
                       runs=(_run("biz-director", "done", "completed",
                                  "Свод: себестоимость, цена, маржа и точка безубыточности сведены "
                                  "в одну картину, расхождения по доставке помечены.",
                                  _modern_metadata(slug), at=_ts(150)),)),
        "critic": _card(slug, "critic", "Критика: проверить маржу и точку безубыточности",
                        assignee="biz-critic", created_at=_ts(198), completed_at=_ts(212),
                        runs=(_run("biz-critic", "done", "completed",
                                   "Вердикт: доработать. Какао подорожало на 7% с 20.10, "
                                   "себестоимость моти пересчитана не была; точка безубыточности "
                                   "занижена на 3 набора.",
                                   {"verdict": "revise",
                                    "issues": [{"where": "Срез 02-CFO",
                                                "problem": "Себестоимость без роста какао",
                                                "fix": "Пересчитать моти с ценой какао от 20.10"},
                                               {"where": "Ответ корня",
                                                "problem": "Точка безубыточности 68 наборов",
                                                "fix": "Поднять до 71 с учётом новой себестоимости"}]},
                                   at=_ts(199)),)),
        "revision": _card(slug, "revision", "Доработка: пересчитать маржу после роста какао",
                          status="done", assignee="biz-director", created_at=_ts(214),
                          started_at=_ts(220), completed_at=_ts(238),
                          runs=(_run("biz-director", "done", "completed",
                                     "Пересчитал: моти R$ 12,85, набор R$ 22,15, маржа 46% при "
                                     "цене R$ 42, точка безубыточности 71 набор в месяц.",
                                     {"verdict": "approve"},
                                     at=_ts(222), ended=_ts(238)),)),
    }
    links = [(cards[k]["id"], root["id"]) for k in ("cfo", "ops", "marketing", "lawyer", "synth")]
    links += [(cards[k]["id"], cards["synth"]["id"]) for k in ("cfo", "ops", "marketing", "lawyer")]
    links.append((root["id"], cards["critic"]["id"]))
    links.append((cards["critic"]["id"], cards["revision"]["id"]))
    return {"name": "done_modern", "slug": slug, "root_key": "root",
            "child_keys": ["cfo", "ops", "marketing", "lawyer", "synth", "critic", "revision"],
            "cards": cards, "links": links,
            "extra_cards": {"synthesizer": cards["synth"]["id"],
                            "critic": cards["critic"]["id"],
                            "revision": cards["revision"]["id"]},
            "report": True}


def _modern_metadata(slug: str) -> dict:
    """Полный metadata нового контракта (ARCH §5) — эталон для engine_read/ingest."""
    return {
        "next_steps": [
            {"text": "Согласовать с Асей финальную спеку упаковки набора из 6 моти (лоток 18×12, крафт, наклейка)",
             "owner": "biz-ops", "due": "2026-10-31"},
            {"text": "Запросить котировки у трёх поставщиков упаковки в Заозёрье по готовой спеке",
             "owner": "biz-ops", "due": "2026-11-04"},
            {"text": "Поднять цену набора до R$ 42 или убрать доставку из расчёта — решить по марже",
             "owner": "biz-cfo", "due": "2026-11-02"},
            {"text": "Проверить состав и аллергены на этикетке по СанПиН до первой партии",
             "owner": "biz-lawyer", "due": "2026-11-05"},
            {"text": "Собрать 15 предзаказов через витрину в Instagram и замерить конверсию",
             "owner": "biz-marketing", "due": "10.11.2026"},
        ],
        "open_questions": [
            {"text": "Держим цену R$ 42 без доставки или R$ 45 с доставкой по Заозёрью?",
             "for": "mikhail"},
            {"text": "Кто пишет поставщикам упаковки: Илья или Вера?", "for": "biz-director"},
        ],
        "decisions": [
            {"text": "Стартовать с набора из 6 моти, не с поштучной продажи",
             "why": "упаковка на набор дешевле на 18%, средний чек выше", "status": "accepted"},
            {"text": "Подавать в Роспатент после подтверждения ИП",
             "why": "ИП даёт скидку около 50% на таксу", "status": "accepted"},
            {"text": "Держать маржу не ниже 45% при любой цене набора",
             "why": "ниже 45% не покрывается доставка и DAS", "status": "proposed"},
        ],
        "stage": {"current": "packaging", "phases": [
            {"id": "idea", "title": "Идея и стратегия", "status": "done",
             "note": "VISION, экономика и план 90 дней утверждены 14–16.09"},
            {"id": "legal", "title": "Легализация", "status": "done",
             "note": "Заявление на ИП подано 27.10, чек-лист этикетки СанПиН готов"},
            {"id": "brand", "title": "Бренд и товарный знак", "status": "done",
             "note": "«Лето», проверка в Роспатенте: свободно по кл. 30/43/35"},
            {"id": "packaging", "title": "Упаковка", "status": "active",
             "note": "Котировки собраны, ждём спеку SKU от Аси"},
            {"id": "production", "title": "Производство", "status": "next",
             "note": "Кухня есть, тестовой партии нет"},
            {"id": "sales", "title": "Продажи", "status": "next", "note": "0 продаж"},
        ]},
        "future_questions": [
            "Стоит ли выходить в B2B с наборами для кофеен Заозёрья?",
            "Нужен ли отдельный бренд для линии без сахара?",
            "Когда нанимать второго кондитера: при 150 или 300 наборах в неделю?",
        ],
        "artifacts": [
            {"path": "VAULT/Лето/01-Strategy/unit-economics.md", "title": "Юнит-экономика набора",
             "kind": "md"},
            {"path": "VAULT/Лето/03-Ops/packaging-quotes.md", "title": "Котировки упаковки",
             "kind": "md"},
            {"path": "REPORT", "title": "Интерактивный отчёт по марже", "kind": "json"},
        ],
        "report": "REPORT",
        "spend": "0 R$, внешних сервисов нет",
        "worker_session_id": "local-cafe-20261027-unit-economics",
    }


def _tree_done_legacy() -> dict:
    """Старый формат metadata — как у реального t_605613e0, но про Лето и моти."""
    slug = "r-20261027-1402-f6a7"
    request = ("Дай картину по проекту Лето: где мы сейчас, что мешает и пять следующих шагов. "
               "Без общих слов — по фактам из Vault.")
    root = _card(slug, "root", "Лето: картина по проекту — где мы и что дальше",
                 status="done", assignee="biz-director", created_at=_ts(60), request=request,
                 started_at=_ts(120), completed_at=_ts(176), idempotency_key=slug,
                 runs=(_run("biz-director", "done", "completed",
                            "Картина по «Лету»: бренд и легализация почти закрыты, упаковка в работе, "
                            "производство и продажи не начаты. Главный блокер — канал связи и "
                            "решение по Роспатент. Пять шагов ниже, первые два — за Михаилом.",
                            _legacy_metadata(), at=_ts(160), ended=_ts(176)),),
                 events=(_event("created", {"status": "triage"}, _ts(60)),
                         _event("decomposed", {"child_ids": [
                             _tid(slug, "lawyer"), _tid(slug, "ops"), _tid(slug, "strategy"),
                             _tid(slug, "runner"), _tid(slug, "synth")],
                             "root_assignee": "biz-director"}, _ts(64)),
                         _event("completed", {"status": "done"}, _ts(176))))
    cards = {
        "root": root,
        "lawyer": _card(slug, "lawyer", "Срез 01-Legal: статус ИП, свидетельство и СЭС",
                        assignee="biz-lawyer", created_by="auto-decomposer", created_at=_ts(65),
                        completed_at=_ts(120),
                        runs=(_run("biz-lawyer", "done", "completed",
                                   "Нормы и гайды готовы, факт ИП — нет данных. "
                                   "свидетельство о регистрации и заключение о размещении не получены.",
                                   {"stage": {"legal": "в работе → ждёт действия Михаила"}},
                                   at=_ts(66)),)),
        "ops": _card(slug, "ops", "Срез 03-Ops: упаковка, кухня и мощность",
                     assignee="biz-ops", created_by="auto-decomposer", created_at=_ts(65),
                     completed_at=_ts(118),
                     runs=(_run("biz-ops", "done", "completed",
                                "Котировки упаковки собраны, MOQ 100 не подтверждён; "
                                "замер смены 150 моти в неделю (18.09).",
                                {"stage": {"ops": "исследования готовы, закупок нет"}},
                                at=_ts(66)),)),
        "strategy": _card(slug, "strategy", "Срез 05-Strategy: каналы, B2B и цены",
                          assignee="biz-strategy", created_by="auto-decomposer",
                          created_at=_ts(65), completed_at=_ts(114),
                          runs=(_run("biz-strategy", "done", "completed",
                                     "Канальный, B2B и контент-план готовы, ни один не запущен. "
                                     "Поле D19 стоит 32 дня.",
                                     {"stage": {"sales": "не начат: 0 продаж"}}, at=_ts(66)),)),
        "runner": _card(slug, "runner", "Срез 06-Runner: собрать Decisions.md и противоречия",
                        assignee="biz-runner", created_by="auto-decomposer", created_at=_ts(65),
                        completed_at=_ts(110),
                        runs=(_run("biz-runner", "done", "completed",
                                   "В Decisions.md 7 противоречий: VISION 300/нед ≠ 150/нед, "
                                   "ROADMAP.md от 16.09 устарел, D19 «закрыт запуском» ≠ blocked 32 дня.",
                                   {"contradictions_flagged": 7}, at=_ts(66)),)),
        "synth": _card(slug, "synth", "Свести картину по проекту в один ответ",
                       assignee="biz-director", created_by="auto-decomposer",
                       created_at=_ts(65), completed_at=_ts(158),
                       runs=(_run("biz-director", "done", "completed",
                                  "Свод: легализация и бренд почти закрыты, упаковка в работе, "
                                  "производство и продажи не начаты.",
                                  _legacy_metadata(), at=_ts(120)),)),
    }
    links = [(cards[k]["id"], root["id"]) for k in ("lawyer", "ops", "strategy", "runner", "synth")]
    links += [(cards[k]["id"], cards["synth"]["id"]) for k in ("lawyer", "ops", "strategy", "runner")]
    return {"name": "done_legacy", "slug": slug, "root_key": "root",
            "child_keys": ["lawyer", "ops", "strategy", "runner", "synth"],
            "cards": cards, "links": links,
            "extra_cards": {"synthesizer": cards["synth"]["id"]}}


def _legacy_metadata() -> dict:
    """Ровно те ключи и формы, что в реальном t_605613e0 (строки, stage-словарь)."""
    return {
        "artifacts_doc": "VAULT/Лето/00-Director/Tasks/svod-leto-2026-10-27.md",
        "contradictions_flagged": [
            "VISION 300/нед ≠ 150/нед (18.09)",
            "ROADMAP.md (16.09) устарел — читать только STATE.md",
            "D19 «закрыт запуском» 24.09 ≠ blocked 32 дня",
            "«Лето без подачи» (24.09) не пересчитано после свободно 08.10",
        ],
        "next_steps": [
            "Ш1 Михаил (1–2 мин, 0 R$): снять блокер канала исходящих TG/WA — выбрать V1, V2 или V3; "
            "→ Марина стартует поле D19 (6 дней, ≥15 разговоров), снимает блокер 32 дня",
            "Ш2 Антон (0 R$, без Михаила): ратифицировать TRADEMARK-GUIDE.md и чек-лист "
            "«ИП за один вечер» (ОКВЭД 10.71, не 56.10)",
            "Ш3 Михаил (по чек-листу Ш2, 0 R$, ~1 ч): зарегистрировать ИП и запросить "
            "свидетельство о регистрации и заключение о размещении по ALVARA-GUIDE",
            "Ш4 Марина + Михаил (0 R$): первый контакт с СЭС по четырём готовым PT-запросам "
            "(диспенса, такса 56.10, зонирование апартаментов, atestado)",
            "Ш5 Илья → Глеб, Атлас: «Лист решений Михаила» одним документом — вопрос, "
            "варианты, цена, срок, дефолт",
        ],
        "open_questions": [
            "Q1 Канал для D19: V1 бот / V2 QR / V3 вручную + новый дедлайн (старый 05.10 истёк) — Михаил",
            "Q2 Подача в Роспатент «Лето»: кл. 30 (440 R$) или 30+43 (880 R$ со скидкой для ИП)? — Михаил",
            "Q3 Фактический статус ИП / свидетельство о регистрации / заключение о размещении — нет данных",
            "Q4 Утвердить kill-критерии D8 (<50 наборов/мес на 60-й день → ревизия) — Михаил",
            "Q5 Бренд-пак: ник IG/TikTok, домен leto-cafe.ru, финал лейаута, слоган — Михаил",
        ],
        "stage": {
            "idea": "готов: VISION и брифы утверждены 14–16.09, карта решений D1–D20 отработана",
            "legal": "в работе → ждёт действия/решения Михаила: нормы/гайды готовы, факт ИП — "
                     "нет данных; 4 вопроса в СЭС PMF открыты",
            "brand": "в работе → ждёт решения: «Лето» утверждён 24.09, проверка в Роспатенте: свободно, "
                     "подачи нет — go/no-go Михаила",
            "ops": "исследования готовы (рынок/каналы/B2B/GEO), поле D19 не начато",
            "packaging": "в работе: исследование готово (10 производителей городе, старт-вариант "
                         "~1,6–2,4 R$/уп), котировки не отправлены",
            "production": "не начат: кухня есть, замер смены 150/нед, тестовых партий нет",
            "sales": "не начат: 0 продаж, гейт G1 не закрыт, валидация D19 = 0 разговоров",
        },
        "spend": "0 R$, внешних сервисов нет",
        "artifacts": ["VAULT/Лето/00-Director/Tasks/svod-leto-2026-10-27.md"],
        "worker_session_id": "local-cafe-20261027-panorama",
    }


def _tree_failed() -> dict:
    slug = "r-20261028-1018-a7b8"
    request = ("Замерь тестовую партию 30 моти на домашней кухне: время на партию, выход, "
               "себестоимость одной штуки. Отчёт — таблицей.")
    root = _card(slug, "root", "Лето: тестовая партия 30 моти на домашней кухне",
                 status="blocked", assignee="biz-director", created_at=_ts(70), request=request,
                 idempotency_key=slug, consecutive_failures=2,
                 last_failure_error="дочерняя карточка «Пробный прогон 30 моти» упала 2 раза: "
                                    "timeout after 1800s",
                 comments=(_comment("system", "Воркер упал 2 раза подряд: превышено время "
                                     "выполнения 1800 с. Нужен перезапуск или разбиение задачи.",
                                     _ts(92)),),
                 events=(_event("created", {"status": "triage"}, _ts(70)),
                         _event("decomposed", {"child_ids": [_tid(slug, "batch")],
                                               "root_assignee": "biz-director"}, _ts(74)),
                         _event("blocked", {"reason": "воркер упал 2 раза"}, _ts(92))))
    cards = {
        "root": root,
        "batch": _card(slug, "batch", "Пробный прогон 30 моти: время, выход и себестоимость",
                       status="blocked", assignee="biz-runner", created_by="auto-decomposer",
                       created_at=_ts(75), consecutive_failures=2,
                       last_failure_error="worker crashed: timeout after 1800s (партия 30 моти)",
                       runs=(_run("biz-runner", "crashed", "crashed", "",
                                  None, at=_ts(76), ended=_ts(80),
                                  error="timeout after 1800s"),
                             _run("biz-runner", "crashed", "crashed", "",
                                  None, at=_ts(84), ended=_ts(90),
                                  error="worker exited with code 1: OSError: [Errno 28] No space left on device"),),
                       events=(_event("failed", {"reason": "timeout"}, _ts(80)),
                               _event("failed", {"reason": "crash"}, _ts(90)),
                               _event("blocked", {"reason": "2 падения подряд"}, _ts(91)))),
    }
    return {"name": "failed", "slug": slug, "root_key": "root", "child_keys": ["batch"],
            "cards": cards, "links": [(cards["batch"]["id"], root["id"])],
            "extra_cards": {"failed_child": cards["batch"]["id"]}}


def _tree_candidate() -> dict:
    """Карточка с событием `decomposed`, которой нет в реестре комнаты."""
    slug = "ext-20261026-1200-cand"
    request = ("Сравни поставщиков заморозки для моти в Заозёрье: цена, срок, "
               "минимальная партия. Кто надёжнее для первых 200 наборов?")
    root = _card(slug, "root", "Лето: поставщик заморозки для моти — кто надёжнее",
                 status="todo", assignee="biz-director", tenant=None, created_by="auto-decomposer",
                 created_at=_ts(-600), request=request,
                 events=(_event("created", {"status": "triage"}, _ts(-600)),
                         _event("decomposed", {"child_ids": [_tid(slug, "a"), _tid(slug, "b")],
                                               "root_assignee": "biz-director"}, _ts(-596))))
    cards = {
        "root": root,
        "a": _card(slug, "a", "Срез: цены и MOQ трёх поставщиков заморозки",
                   status="done", assignee="biz-ops", tenant=None, created_by="auto-decomposer",
                   created_at=_ts(-595), completed_at=_ts(-560),
                   runs=(_run("biz-ops", "done", "completed",
                              "Frio Sul — R$ 18,90/кг, MOQ 20 кг, срок 3 дня; "
                              "Gelmax — R$ 17,40/кг, MOQ 50 кг; Polar городе — R$ 21,00/кг, MOQ 10 кг.",
                              {"stage": {"production": "готово: сравнение поставщиков"}},
                              at=_ts(-594)),)),
        "b": _card(slug, "b", "Срез: надёжность поставщиков по отзывам и срокам",
                   status="done", assignee="biz-runner", tenant=None, created_by="auto-decomposer",
                   created_at=_ts(-595), completed_at=_ts(-556),
                   runs=(_run("biz-runner", "done", "completed",
                              "Frio Sul срывал сроки 2 раза за квартал, Gelmax — 0 срывов, "
                              "но MOQ 50 кг закрывает 250 наборов.",
                              {"stage": {"production": "готово: надёжность"}}, at=_ts(-594)),)),
    }
    return {"name": "candidate", "slug": slug, "root_key": "root", "child_keys": ["a", "b"],
            "cards": cards,
            "links": [(cards["a"]["id"], root["id"]), (cards["b"]["id"], root["id"])],
            "extra_cards": {"event": "decomposed", "in_registry": False}}


TREES = (_tree_queued, _tree_working, _tree_reviewing, _tree_needs_you,
         _tree_done_modern, _tree_done_legacy, _tree_failed, _tree_candidate)

