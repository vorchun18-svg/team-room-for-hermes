# -*- coding: utf-8 -*-
"""Сценарий демо-запроса для мок-сервера «Комнаты команды».

Новый запрос в демо не «висит»: руководитель комнаты раздаёт его на четыре ветки, агенты
работают, критик проверяет свод, а готовый ответ приходит с интерактивным
отчётом (таблица, графики, граф) и одним вопросом Михаилу. Здесь только текст
и структуры — состояние и расписание фаз живут в mock_app/mock_versions.

Всё выдумано: цифры, сроки и имена поставщиков — демо, не факт проекта.
"""
from __future__ import annotations

import re

# --- тема запроса ----------------------------------------------------------

_MARK = re.compile(r"[#*`>]+")


def subject(text: str, limit: int = 72) -> str:
    """Короткая тема из первой строки запроса — для заголовков карточек."""
    for raw in str(text or "").splitlines():
        line = _MARK.sub("", raw).strip().strip("«»\"'")
        if not line:
            continue
        line = re.split(r"(?<=[.!?])\s", line)[0].strip(" .!?;:")
        if len(line) > limit:
            line = line[:limit].rsplit(" ", 1)[0].rstrip(" ,;:") + "…"
        return line or "Новый запрос"
    return "Новый запрос"


def variant_for(text: str) -> str:
    low = str(text or "").casefold()
    if any(w in low for w in ("едок", "развоз", "канал", "продаж", "доставк", "telegram")):
        return "channels"
    if any(w in low for w in ("упаков", "поставщ", "moq", "тираж", "короб")):
        return "packaging"
    if any(w in low for w in ("роспатент", "бренд", "товарн", "класс 30", "leto")):
        return "brand"
    if any(w in low for w in ("ип", "оквэд", "сэс", "юр", "легал", "разрешение", "налог")):
        return "legal"
    return "roadmap"


# --- тексты веток ----------------------------------------------------------

VARIANTS = {
    "channels": {
        "report_title": "Каналы первых продаж: Telegram, кофейни или агрегаторы",
        "summary": "Для первых 100 наборов выгоднее прямой Telegram и две B2B-кофейни: маржа 19,85 и 168,5 ₽ против 122,9 ₽ у Развоз и 101,9 ₽ у Едок. Агрегаторы имеет смысл подключать после третьей партии, когда спрос подтверждён.",
        "cards": (
            "Собрать комиссии и условия «Едок» и «Развоз»",
            "Посчитать маржу набора по каналам",
            "Проверить охват и сроки выплат",
            "Свести рекомендацию по первым 100 наборам",
        ),
        "card_notes": (
            "Комиссии: «Едок» 23%, «Развоз» 18% (тариф «Доставка»); выплаты 14 и 7 дней.",
            "Маржа набора при цене 420 ₽: Telegram 19,85; B2B 16,85; Развоз 12,29; Едок 10,19.",
            "Охват в Заозёрье у агрегаторов шире, но заказы стартуют с 1–2 в день.",
            "Рекомендация: Telegram + две кофейни сейчас, агрегаторы — после 100 наборов.",
        ),
        "answer": """## Короткий ответ

Для первых 100 наборов — **Telegram и две кофейни в Заозёрье**. «Едок» и «Развоз» подключать после третьей партии: комиссия съедает 18–23% цены, а спрос ещё не подтверждён.

| Канал | Комиссия | Маржа набора | Когда подключать |
|---|---|---|---|
| Telegram | 0% | 198,5 ₽ | сразу |
| B2B-кофейни | 0% | 168,5 ₽ | сразу, две точки |
| Развоз | 18% | 122,9 ₽ | после 100 наборов |
| Едок | 23% | 101,9 ₽ | не раньше ноября |

**Почему так.** Набор стоит 420 ₽, себестоимость — 221,5 ₽. Агрегатор забирает комиссию с полной цены, а не с маржи, поэтому разница почти в 100 ₽ на заказ при первых ста наборах — это около 10 000 ₽, которые лучше потратить на упаковку и тестовую партию.

**Риски.** У кофеен заказ идёт «на витрину»: если набор не продался за день, его списывают. Поэтому старт — 12 наборов на точку, дальше по факту.

**Что дальше.** Запустить поле D19 в Telegram, собрать 20 предзаказов и только потом вести переговоры с агрегаторами.""",
        "question": "Начинаем с Telegram и двух кофеен — или сразу подключаем Развоз, чтобы проверить спрос на охвате?",
        "next_steps": (
            ("Запустить поле D19 в Telegram и собрать 20 предзаказов", "mikhail", "2026-10-31"),
            ("Договориться с двумя кофейнями о 12 наборах на витрину", "biz-marketing", "2026-11-02"),
            ("Посчитать факт маржи после первой партии", "biz-cfo", "2026-11-06"),
        ),
    },
    "packaging": {
        "report_title": "Упаковка первого тиража: цена против MOQ",
        "summary": "Для 100 наборов выгоднее поставщик A: MOQ 100 и 42 ₽ за короб. Вариант B дешевле на 19%, но требует 300 коробов и заморозит 10 200 ₽ на складе.",
        "cards": (
            "Собрать шорт-лист поставщиков упаковки в городе",
            "Запросить котировки и MOQ",
            "Сверить цену с себестоимостью набора",
            "Выбрать поставщика под первый тираж",
        ),
        "card_notes": (
            "В шорт-листе 10 поставщиков Заозёрья, трое работают с малым тиражом.",
            "Котировки: A — 42 ₽ при MOQ 100; B — 34 ₽ при MOQ 300; C — 31 ₽ при MOQ 500.",
            "При 100 наборах упаковка — 4 200 ₽, это 12% себестоимости набора.",
            "Рекомендация: A на первый тираж, B — со второй партии.",
        ),
        "answer": """## Короткий ответ

На первый тираж из 100 наборов — **поставщик A**: MOQ 100 и 42 ₽ за короб. Он дороже на 19%, зато не требует замораживать деньги в 300–500 коробах, которые ещё некуда девать.

| Поставщик | MOQ | Цена короба | Сумма на 100 наборов | Комментарий |
|---|---|---|---|---|
| A | 100 | 42 ₽ | 4 200 ₽ | срок 7 дней, образцы есть |
| B | 300 | 34 ₽ | 10 200 ₽ | выгодно со второй партии |
| C | 500 | 31 ₽ | 15 500 ₽ | нужен склад, срок 12 дней |

**Почему так.** При цене набора 420 ₽ упаковка за 42 ₽ — это 10% выручки и 19% себестоимости. Экономия 8 ₽ на коробе не стоит 6 000 ₽, замороженных в лишнем тираже.

**Риски.** У A нет ламинации под холод: для моти нужен слой PE. Просим образец и тест на конденсат до оплаты.

**Что дальше.** Заказать 120 коробов (100 + запас), проверить первую партию и вернуться к B на втором тираже.""",
        "question": "Берём 100 коробов у A по 42 ₽ или сразу 300 у B по 34 ₽ и ищем место под склад?",
        "next_steps": (
            ("Заказать образцы у A и проверить слой PE", "biz-ops", "2026-10-30"),
            ("Оплатить 120 коробов у A после теста", "mikhail", "2026-11-01"),
            ("Посчитать склад под второй тираж у B", "biz-cfo", "2026-11-05"),
        ),
    },
    "brand": {
        "report_title": "Подача в Роспатент «Лето»: класс 30 или 30+43",
        "summary": "Подавать после свидетельства ИП: скидка около 50% на таксу. Класс 30 закрывает десерты, класс 43 — кафе и доставку; для старта достаточно 30, но 30+43 защищает будущую витрину.",
        "cards": (
            "Проверить классы 30 и 43 в Роспатент",
            "Посчитать таксы со скидкой для ИП",
            "Оценить риск чужой подачи",
            "Свести go/no-go по подаче",
        ),
        "card_notes": (
            "Класс 30 — десерты и кондитерка, класс 43 — кафе и доставка еды.",
            "Таксы: 4 400 ₽ за класс 30, 8 800 ₽ за 30+43 со скидкой ИП.",
            "Проверка в Роспатенте: свободно: совпадений по «Лето» в 30/43/35 нет.",
            "Рекомендация: подавать 30+43 сразу после свидетельство, до 10.11.2026.",
        ),
        "answer": """## Короткий ответ

Подавать **30+43 после свидетельство о регистрации**: ИП даёт скидку около 50%, итого 8 800 ₽ за два класса. Ждать дольше 10.11.2026 рискованно — скрининг чистый, но окно может закрыть чужая заявка.

| Вариант | Что закрывает | Такса | Риск |
|---|---|---|---|
| Класс 30 | десерты, кондитерка | 4 400 ₽ | кафе и доставка не защищены |
| Классы 30+43 | десерты + кафе/доставка | 8 800 ₽ | переплата 4 400 ₽, если кафе не будет |
| Подождать | — | 0 | чужая подача закрывает имя |

**Почему так.** Название уже в бренд-паке и на тестовой упаковке. Если «Лето» займут в классе 30, переделка упаковки обойдётся дороже 4 400 ₽.

**Риски.** Certidão ИП ещё не пришла; без неё скидки не будет. Проверить статус через Госуслуги до оплаты.

**Что дальше.** Получить свидетельство, оплатить 8 800 ₽ и подать 30+43 одним пакетом до 10.11.2026.""",
        "question": "Подаём 30+43 за 8 800 ₽ или только класс 30 за 4 400 ₽ и возвращаемся к 43 позже?",
        "next_steps": (
            ("Проверить свидетельство о регистрации через Госуслуги", "biz-lawyer", "2026-10-30"),
            ("Оплатить таксу Роспатент после подтверждения ИП", "mikhail", "2026-11-03"),
            ("Подать заявку 30+43 до 10.11.2026", "biz-lawyer", "2026-11-10"),
        ),
    },
    "legal": {
        "report_title": "ИП и ОКВЭД для производства моти: что проверить до открытия",
        "summary": "Для моти подходит ОКВЭД 10.71, а не 56.10. До первой партии нужны ИП, разрешение и заключение о размещении в СЭС; без них кухню не сертифицируют.",
        "cards": (
            "Проверить ОКВЭД 10.71 для ИП",
            "Собрать требования СЭС",
            "Сверить нормы штата городе",
            "Свести чек-лист легализации",
        ),
        "card_notes": (
            "10.71 разрешён для ИП, 56.10 — нет.",
            "СЭС: план помещения, список оборудования, обучение по СанПиН.",
            "В городе дополнительно нужен разрешение municipal и онлайн-касса.",
            "Чек-лист на 12 шагов, первые три — до открытия кухни.",
        ),
        "answer": """## Короткий ответ

Открывать ИП можно сейчас, но **ОКВЭД 10.71**, не 56.10. До первой партии — заключение о размещении в СЭС и разрешение municipal; без них кухню не сертифицируют, а наборы нельзя продавать легально.

| Шаг | Что нужно | Срок | Кто |
|---|---|---|---|
| ИП | ОКВЭД 10.71, ИНН | 1 день | Михаил |
| СЭС | план кухни, оборудование, СанПиН | 2–4 недели | biz-lawyer |
| Alvará | адрес, договор аренды | 1–2 недели | biz-ops |
| онлайн-касса | доступ в prefeitura | 1 день | Михаил |

**Почему так.** 56.10 — общепит, для ИП он не входит в разрешённые виды. Ошибка в ОКВЭД приведёт к отказу и повторной подаче.

**Риски.** Помещение есть, но не сертифицировано: до СЭС тестовую партию делать нельзя.

**Что дальше.** Подать заявление на ИП, параллельно запросить заключение о размещении и собрать пакет документов.""",
        "question": "Подаём ИП сейчас и параллельно готовим СЭС — или ждём свидетельство и стартуем одним пакетом?",
        "next_steps": (
            ("Подать заявление на ИП с ОКВЭД 10.71 через Госуслуги", "mikhail", "2026-11-01"),
            ("Запросить заключение о размещении в СЭС", "biz-lawyer", "2026-11-05"),
            ("Собрать пакет документов на разрешение", "biz-ops", "2026-11-08"),
        ),
    },
    "roadmap": {
        "report_title": "Разбор запроса: факты, ограничения и следующий шаг",
        "summary": "Команда прошла четыре ветки и свела ответ: запрос выполним, но упирается в два ограничения — неподтверждённые цифры и один ждущий решения вопрос. Первый шаг — проверить факты на тестовой партии.",
        "cards": (
            "Разобрать, что уже известно",
            "Собрать цифры и ограничения",
            "Проверить риски и нормы",
            "Свести варианты и рекомендацию",
        ),
        "card_notes": (
            "Собраны факты из Vault и прошлых ответов комнаты.",
            "Цифры пересчитаны на партию 100 наборов; расхождения отмечены.",
            "Риски: легализация, упаковка и спрос — три открытых пункта.",
            "Рекомендация: сначала тестовая партия, потом расширение.",
        ),
        "answer": """## Короткий ответ

Запрос разобран на четыре ветки. Выполнимо, но **сначала тестовая партия на 100 наборов**: она подтвердит себестоимость и спрос, а не только расчёт.

| Ветка | Что выяснили | Ограничение |
|---|---|---|
| Факты | данные есть в Vault и прошлых сводах | часть цифр — оценка, не факт |
| Цифры | себестоимость набора 221,5 ₽ | без факта закупки |
| Риски | ИП, СЭС, упаковка | часть шагов не закрыта |
| Рекомендация | тестовая партия + 20 предзаказов | нужен твой ответ |

**Что мешает.** Один вопрос требует решения человека — он в карточке справа. Остальное команда может делать сама.

**Что дальше.** Зафиксировать решение, собрать партию и вернуться с фактом: после него цифры можно считать подтверждёнными.""",
        "question": "Начинаем с тестовой партии на 100 наборов или сразу расширяем план до 90 дней?",
        "next_steps": (
            ("Подтвердить план тестовой партии", "mikhail", "2026-10-31"),
            ("Собрать смету партии по факту закупки", "biz-cfo", "2026-11-04"),
            ("Вернуться с фактом продаж и пересчитать план", "biz-director", "2026-11-10"),
        ),
    },
}

ROLE_STATUS = {"done": "done", "approved": "done", "running": "running", "ready": "ready",
               "triage": "triage", "blocked": "blocked"}


def _node(node_id, title, assignee, role, status, parent, created_at, note, start=0, finish=None):
    return {
        "id": node_id, "parent_ids": [parent] if parent else [], "title": title,
        "assignee": assignee, "role": role, "status": status,
        "started_at": (created_at + start) if status in ("done", "running") else None,
        "finished_at": (created_at + finish) if finish is not None else None,
        "summary_md": note, "metadata": {}, "comments": [],
        "runs": ([{"id": 1, "profile": assignee, "status": "done" if status == "done" else "running",
                   "outcome": "completed" if status == "done" else None,
                   "started_at": created_at + start, "ended_at": (created_at + finish) if finish is not None else None,
                   "summary": note, "error": None}] if status in ("done", "running") else []),
        "needs_human": False, "outside_room": False, "error": None,
    }


def build_tree(title, room, phase, revisions=0, created_at=0, revision_notes=None):
    """Дерево работ демо-запроса под текущую фазу (id условные — их заменит App)."""
    variant = VARIANTS[variant_for(title)]
    orchestrator = room.get("orchestrator") or "default"
    workers = [m["profile"] for m in room.get("members") or [] if m.get("role", "worker") == "worker"]
    if not workers:
        workers = [orchestrator]
    nodes = [_node("root", title, orchestrator, "root",
                   {"queued": "triage", "working": "running"}.get(phase, "done"),
                   None, created_at, "", start=0, finish=None if phase in ("queued", "working") else 35)]
    if phase == "queued":
        return nodes

    statuses = {
        "working": ("done", "running", "ready", "ready"),
        "reviewing": ("done", "done", "done", "done"),
        "synthesizing": ("done", "done", "done", "done"),
        "done": ("done", "done", "done", "done"),
    }.get(phase, ("done", "done", "done", "done"))
    for i, card in enumerate(variant["cards"]):
        status = statuses[i]
        finish = 10 + i * 5 if status == "done" else None
        nodes.append(_node("w%d" % (i + 1), card, workers[i % len(workers)], "worker", status,
                           "root", created_at, variant["card_notes"][i], start=4 + i * 2, finish=finish))

    if room.get("critic") and phase in ("reviewing", "synthesizing", "done"):
        nodes.append(_node("critic", "Критик: проверка свода (раунд 1)", room["critic"], "critic",
                           "running" if phase == "reviewing" else "done", "root", created_at,
                           "" if phase == "reviewing" else "Замечаний нет: цифры сходятся, вопрос к Михаилу уместен.",
                           start=12, finish=None if phase == "reviewing" else 20))
    if revisions > 0 and phase in ("synthesizing", "done"):
        notes = [str(n).strip() for n in (revision_notes or []) if str(n).strip()]
        note = ("Учтено замечание Михаила: " + notes[-1][:240]) if notes else "Свод доработан по замечанию Михаила."
        nodes.append(_node("revision", "Доработка свода по замечаниям Михаила", orchestrator, "revision",
                           "running" if phase == "synthesizing" else "done", "root", created_at,
                           note, start=20, finish=None if phase == "synthesizing" else 35))
    return nodes


def answer_md(title, text, revision_notes=None):
    """Текст свода: базовая версия + след доработки, если Михаил просил."""
    variant = VARIANTS[variant_for(text)]
    body = variant["answer"]
    notes = [n for n in (revision_notes or []) if str(n).strip()]
    if notes:
        body += "\n\n## Что изменилось в версии 2\n\n"
        for i, note in enumerate(notes, 1):
            body += "%d. Учтено замечание Михаила: %s\n" % (i, str(note).strip()[:300])
        body += "\nЦифры и вывод пересчитаны; вопрос к Михаилу остался один."
    return body


def next_steps(text):
    variant = VARIANTS[variant_for(text)]
    return [{"id": "ns_%d" % (i + 1), "text": t, "owner": owner, "due": due, "state": "open"}
            for i, (t, owner, due) in enumerate(variant["next_steps"])]


def open_question(text):
    variant = VARIANTS[variant_for(text)]
    return {"id": "q_1", "text": variant["question"], "for": "mikhail", "answered": False,
            "answer": None, "answered_at": None}


def report_key(rid):
    """Ключ отчёта в URL: только [a-z0-9-] (подчёркивание в id запроса не годится)."""
    text = str(rid)
    return "demo-" + (text[2:] if text.startswith("t_") else text)


def timeline(created_at, title, room, phase, revisions=0):
    """События запроса по времени; App отфильтрует по прошедшим секундам."""
    orchestrator = room.get("orchestrator") or "default"
    events = [
        {"at": created_at, "kind": "created", "task_id": "root", "actor": "mikhail",
         "text": "Запрос создан в комнате %s" % room.get("title", room.get("id", ""))},
        {"at": created_at + 3, "kind": "decomposed", "task_id": "root", "actor": orchestrator,
         "text": "Руководитель комнаты разложил запрос на четыре ветки"},
        {"at": created_at + 5, "kind": "claimed", "task_id": "w1", "actor": "team",
         "text": "Команда взяла ветки в работу"},
        {"at": created_at + 11, "kind": "completed", "task_id": "w1", "actor": "team",
         "text": "Первые ветки закрыты, цифры сведены"},
    ]
    if room.get("critic"):
        events.append({"at": created_at + 12, "kind": "claimed", "task_id": "critic",
                       "actor": room["critic"], "text": "Критик взял свод на проверку"})
        events.append({"at": created_at + 20, "kind": "completed", "task_id": "critic",
                       "actor": room["critic"], "text": "Критик проверил свод: замечаний нет"})
    if revisions:
        events.append({"at": created_at + 35, "kind": "commented", "task_id": "root", "actor": "mikhail",
                       "text": "Михаил отправил ответ на доработку"})
    events.append({"at": created_at + 35 + 25 * max(0, revisions - 1), "kind": "completed",
                   "task_id": "root", "actor": orchestrator,
                   "text": "Ответ готов: версия %d" % (1 + max(0, revisions))})
    return events


def report_spec(detail, text):
    """Интерактивный отчёт по живому запросу: таблица, два графика и граф."""
    variant = VARIANTS[variant_for(text)]
    tree = detail.get("tree") or []
    version = int((detail.get("synthesis") or {}).get("version") or 1)
    status_ru = {"done": "готово", "running": "в работе", "ready": "ждёт", "triage": "ждёт",
                 "blocked": "блок"}
    short = {"root": "Запрос", "critic": "Критик", "revision": "Доработка",
             "w1": "Факты", "w2": "Цифры", "w3": "Риски", "w4": "Свод"}

    key_by_node = {}
    worker_index = 0
    for node in tree:
        role = node.get("role") or "worker"
        if role == "root":
            key = "root"
        elif role == "critic":
            key = "critic"
        elif role == "revision":
            key = "revision"
        else:
            worker_index += 1
            key = "w%d" % worker_index
        key_by_node[node.get("id")] = key

    rows, labels, values, nodes, edges = [], [], [], [], []
    confidence = {"root": 100, "w1": 86, "w2": 78, "w3": 70, "w4": 90, "critic": 93, "revision": 95}
    seen_keys = set()
    for node in tree:
        key = key_by_node.get(node.get("id"), "root")
        if key in seen_keys:
            continue
        seen_keys.add(key)
        state = status_ru.get(node.get("status"), "ждёт")
        share = {"готово": 100, "в работе": 60, "ждёт": 25, "блок": 0}.get(state, 25)
        label = short.get(key, key)
        rows.append({"card": str(node.get("title") or "")[:90], "who": node.get("assignee") or "—",
                     "state": state, "ready": share})
        labels.append(label)
        values.append(confidence.get(key, 70))
        role = node.get("role") or "worker"
        group = {"root": "Руководитель комнаты", "critic": "Критик", "revision": "Доработка"}.get(role, "Команда")
        nodes.append({"id": key, "label": label, "group": group,
                      "note": str(node.get("title") or "")[:120]})

    known = {n["id"] for n in nodes}
    for node in tree:
        if node.get("role") == "root":
            continue
        child = key_by_node.get(node.get("id"))
        if child not in known:
            continue
        parent = "root"
        if node.get("role") == "revision" and "critic" in known:
            parent = "critic"
        edges.append({"from": parent, "to": child,
                      "label": "замечания" if parent == "critic" else "ветка" if node.get("role") == "worker" else "свод",
                      "kind": "solid"})

    answered = sum(1 for q in detail.get("open_questions") or [] if q.get("answered"))
    open_count = sum(1 for q in detail.get("open_questions") or [] if not q.get("answered"))
    steps = detail.get("next_steps") or []
    blocks = [
        {"type": "heading", "text": "Коротко", "level": 2},
        {"type": "text", "md": variant["summary"]},
        {"type": "stats", "items": [
            {"label": "Карточек в дереве", "value": str(len(tree)), "hint": "включая руководителя комнаты и критика"},
            {"label": "Версия ответа", "value": "v%d" % version, "tone": "info"},
            {"label": "Уверенность команды", "value": "%d%%" % (round(sum(values) / len(values)) if values else 0),
             "hint": "оценка веток, не факт", "tone": "success" if values and min(values) >= 80 else "warn"},
            {"label": "Вопросов к Михаилу", "value": str(open_count), "hint": "отвечено: %d" % answered,
             "tone": "warn" if open_count else "success"},
        ]},
        {"type": "table", "title": "Ветки работ", "sortable": True,
         "columns": [
             {"key": "card", "label": "Карточка", "type": "text"},
             {"key": "who", "label": "Исполнитель", "type": "text"},
             {"key": "state", "label": "Статус", "type": "status"},
             {"key": "ready", "label": "Готовность, %", "type": "percent", "decimals": 0},
         ],
         "rows": rows, "totals": {"ready": "avg"},
         "footnote": "Демо: цифры выдуманы, карточки не запускали реальных агентов."},
        {"type": "chart", "kind": "hbar", "title": "Уверенность в ветках, %", "unit": "%",
         "x": labels, "series": [{"name": "Оценка", "data": values}]},
        {"type": "chart", "kind": "donut", "title": "Куда ушло время команды, %", "unit": "%",
         "x": ["Сбор фактов", "Расчёты", "Проверка рисков", "Свод"],
         "series": [{"name": "Доля", "data": [35, 25, 20, 20]}]},
        {"type": "graph", "title": "Кто что сделал", "layout": "layered", "direction": "LR",
         "nodes": nodes, "edges": edges},
        {"type": "callout", "tone": "warn", "title": "Срок — до пятницы",
         "md": "Ответ собран мок-сервером: агенты не запускались, цифры и поставщики выдуманы. "
               "Проверять по нему решения нельзя — только смотреть, как устроен ответ."},
        {"type": "checklist", "title": "Что сделать дальше",
         "items": [{"text": s["text"], "done": s.get("state") == "done", "owner": s.get("owner"),
                    "due": s.get("due")} for s in steps]},
    ]
    return {"version": 1, "title": variant["report_title"],
            "subtitle": "комната %s · демо · данные выдуманы" % detail.get("room", ""),
            "summary": variant["summary"], "blocks": blocks}
