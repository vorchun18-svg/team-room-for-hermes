"""Универсальные шаблоны дежурств комнаты (W27-E7, `docs/WAVE27-RULES.md` §3).

Шаблон — это **заготовка мастера**, а не отдельная сущность: он заполняет четыре
шага мастера («что проверять» → «когда» → «что считать изменением» → «что делать»),
а расписание всё равно живёт в Hermes (`hermes cron create`). Ничего не создаётся
и не запускается от одного лишь выбора шаблона.

Правило волны: ни терминов конкретного бизнеса, ни реальных имён и путей. Скрипты
называются условно (`room-mail.sh`) и живут в каталоге скриптов Hermes.

    from server import duty_templates
    duty_templates.list_templates()["templates"]
"""
from __future__ import annotations

import time

#: Виды задания: модель по заданию, скрипт без модели, слежение за изменением.
KINDS = ("model", "script", "monitor")
KIND_TITLES = {
    "model": "Модель по заданию",
    "script": "Скрипт без модели",
    "monitor": "Слежение за изменением",
}
#: Действия при изменении (что делать) и уровни автономии.
ON_CHANGE = ("log", "inbox", "delo", "step", "question")
ON_CHANGE_TITLES = {
    "log": "только запись в журнал",
    "inbox": "карточка в «Входящие»",
    "delo": "предложить Дело",
    "step": "предложить шаг в Деле",
    "question": "вопрос мне",
}
LEVELS = ("self", "propose", "ask")
LEVEL_TITLES = {"self": "сам (обратимое внутри комнаты)", "propose": "предложить",
                "ask": "спросить"}
#: Куда сообщить (подсказки интерфейсу; доставку делает Hermes `--deliver room:`).
NOTIFY = ("today", "room", "windows")
NOTIFY_TITLES = {"today": "«Сегодня»", "room": "бейдж комнаты",
                 "windows": "уведомление Windows"}

#: Сколько шаблонов отдаём (их семь — ограничение на будущее, не на сейчас).
MAX_TEMPLATES = 20


def _wizard(*, kind: str, schedule: str, prompt: str = "", script: str = "",
            monitor_script: str = "", monitor_url: str = "", changed_rule: str,
            on_change: str, level: str, notify=(), catch_up=None, continuity=True) -> dict:
    """Поля мастера одним словарём — ровно те, что понимает `POST …/duties`."""
    return {
        "kind": kind,
        "schedule": schedule,
        "prompt": prompt,
        "script": script,
        "monitor_script": monitor_script,
        "monitor_url": monitor_url,
        "changed_rule": changed_rule,
        "on_change": on_change,
        "level": level,
        "notify": list(notify),
        "catch_up": catch_up,
        "continuity": bool(continuity),
    }


#: Семь универсальных шаблонов. Порядок — как в §3 правил волны.
TEMPLATES = [
    {
        "id": "mail",
        "title": "Проверять почту/ящик на новое",
        "why": "Новое письмо по делу не должно ждать, пока вы откроете ящик.",
        "wizard": _wizard(
            kind="monitor", schedule="*/30 * * * *", monitor_script="room-mail.sh",
            prompt="Проверь почтовый ящик комнаты. Новые письма — по одному: от кого, "
                   "о чём, что требуется. Ничего не отправляй.",
            changed_rule="Появились новые письма (сравнение с прошлым запуском).",
            on_change="inbox", level="propose", notify=("today", "room"), continuity=True),
        "hint": "Скрипт печатает список писем стабильно, без даты и времени внутри — "
                "иначе каждое изменение будет «новым».",
    },
    {
        "id": "folder",
        "title": "Следить за папкой",
        "why": "Файлы от коллег и подрядчиков появляются без предупреждения.",
        "wizard": _wizard(
            kind="monitor", schedule="every 1h", monitor_script="room-folder.sh",
            prompt="Посмотри, что нового появилось в папке комнаты. Назови файлы и что "
                   "с ними делать. Ничего не перемещай и не удаляй.",
            changed_rule="В папке появились или изменились файлы.",
            on_change="inbox", level="propose", notify=("room",), continuity=True),
        "hint": "Рабочая папка задаётся в мастере (шаг «что проверять»).",
    },
    {
        "id": "morning",
        "title": "Утренний обзор комнаты",
        "why": "Одна сводка утром вместо десяти вопросов в течение дня.",
        "wizard": _wizard(
            kind="model", schedule="0 8 * * 1-5",
            prompt="Собери утренний обзор комнаты: что сделано вчера, что просрочено, "
                   "что ждёт человека, что предлагаешь на сегодня. Коротко, списком.",
            changed_rule="Есть что показать: просрочки, новые пункты «ждёт вас» или "
                         "изменения по сравнению с прошлым запуском.",
            on_change="inbox", level="propose", notify=("today", "room"), continuity=True),
        "hint": "Если за сутки ничего не изменилось, дежурство молчит — это нормально.",
    },
    {
        "id": "page",
        "title": "Следить за страницей/ценой",
        "why": "Цена или условия на странице меняются молча.",
        "wizard": _wizard(
            kind="monitor", schedule="0 10 * * *", monitor_url="https://example.com/page",
            prompt="Сравни страницу с прошлым запуском. Если изменились цена, сроки или "
                   "условия — назови что именно и насколько. Ничего не заказывай.",
            changed_rule="Текст страницы изменился (Hermes сравнивает побайтно).",
            on_change="inbox", level="propose", notify=("today",), continuity=True),
        "hint": "Адрес страницы — вымышленный пример: подставьте свой.",
    },
    {
        "id": "deadlines",
        "title": "Напоминать о сроках",
        "why": "Срок проходит незаметно, если о нём не напоминают заранее.",
        "wizard": _wizard(
            kind="model", schedule="0 9 * * 1-5",
            prompt="Проверь сроки комнаты: что истекает сегодня и в ближайшие три дня. "
                   "По каждому — что это, когда срок, кто отвечает.",
            changed_rule="Есть срок, который истекает сегодня или в ближайшие 3 дня.",
            on_change="inbox", level="propose", notify=("today", "room"), continuity=True),
        "hint": "Повторные напоминания об одном сроке не создаются — сверка с прошлым "
                "запуском включена.",
    },
    {
        "id": "weekly",
        "title": "Еженедельный отчёт",
        "why": "Итог недели нужен одному человеку и в одно и то же время.",
        "wizard": _wizard(
            kind="model", schedule="0 10 * * 5",
            prompt="Собери отчёт за неделю: что сделано, что не сделано и почему, что "
                   "ждёт вашего решения, план на следующую неделю. Одним связным текстом.",
            changed_rule="Неделя закончилась — отчёт всегда считается изменением.",
            on_change="inbox", level="propose", notify=("today", "room"), continuity=True),
        "hint": "Отчёт — карточка во «Входящих»: принять, изменить или «не надо».",
    },
    {
        "id": "steps",
        "title": "Проверять выполнение шагов",
        "why": "Шаг «в работе» неделю — это застрявшая работа, а не прогресс.",
        "wizard": _wizard(
            kind="model", schedule="every 4h",
            prompt="Проверь шаги комнаты: что просрочено, что не двигалось больше двух "
                   "дней, что ждёт чужого ответа. Ничего не закрывай сам.",
            changed_rule="Появился просроченный шаг или шаг без движения больше 2 дней.",
            on_change="step", level="propose", notify=("today",), continuity=True),
        "hint": "Шаг создаётся только по вашему «Принять» — и только внутри комнаты.",
    },
]

_BY_ID = {item["id"]: item for item in TEMPLATES}


class TemplateError(Exception):
    """Неизвестный шаблон: 404 по-русски (форма как у `DutiesError`)."""

    def __init__(self, message: str, http: int = 404, code: str = "not_found", detail=None):
        super().__init__(message)
        self.message = str(message)
        self.http = int(http)
        self.code = str(code)
        self.detail = dict(detail or {})


def _public(item: dict) -> dict:
    """Копия шаблона без общих ссылок (вызывающий может править смело)."""
    wizard = dict(item.get("wizard") or {})
    wizard["notify"] = list(wizard.get("notify") or [])
    return {"id": item["id"], "title": item["title"], "why": item.get("why", ""),
            "hint": item.get("hint", ""), "kind_title": KIND_TITLES.get(wizard.get("kind"), ""),
            "on_change_title": ON_CHANGE_TITLES.get(wizard.get("on_change"), ""),
            "level_title": LEVEL_TITLES.get(wizard.get("level"), ""),
            "schedule_human": _human(wizard.get("schedule")), "wizard": wizard}


def _human(schedule) -> str:
    """Русское расписание шаблона — тем же переводчиком, что и у дежурств."""
    try:
        from . import duties as duties_mod
    except ImportError:                  # pragma: no cover - плоский импорт
        import duties as duties_mod      # type: ignore
    try:
        return duties_mod.schedule_human(str(schedule or ""))
    except Exception:                    # noqa: BLE001 — подпись не должна валить список
        return str(schedule or "")


def list_templates() -> dict:
    """``GET /api/duty-templates`` — все универсальные шаблоны."""
    return {"generated_at": int(time.time()),
            "kinds": [{"id": key, "title": KIND_TITLES[key]} for key in KINDS],
            "on_change": [{"id": key, "title": ON_CHANGE_TITLES[key]} for key in ON_CHANGE],
            "levels": [{"id": key, "title": LEVEL_TITLES[key]} for key in LEVELS],
            "notify": [{"id": key, "title": NOTIFY_TITLES[key]} for key in NOTIFY],
            "templates": [_public(item) for item in TEMPLATES[:MAX_TEMPLATES]]}


def template(template_id) -> dict:
    """Один шаблон по id (``404`` по-русски, если такого нет)."""
    wanted = str(template_id or "").strip()
    item = _BY_ID.get(wanted)
    if item is None:
        raise TemplateError("Такого шаблона нет: %s" % (wanted or "пусто"), 404, "not_found",
                            {"template": wanted[:60], "allowed": list(_BY_ID)})
    return _public(item)


def wizard_of(template_id) -> dict:
    """Поля мастера из шаблона (пусто, если шаблон не найден — вызов не падает)."""
    item = _BY_ID.get(str(template_id or "").strip())
    return dict(item["wizard"]) if item else {}


__all__ = ["KINDS", "KIND_TITLES", "LEVELS", "LEVEL_TITLES", "MAX_TEMPLATES", "NOTIFY",
           "NOTIFY_TITLES", "ON_CHANGE", "ON_CHANGE_TITLES", "TEMPLATES", "TemplateError",
           "list_templates", "template", "wizard_of"]
