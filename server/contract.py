"""Тексты карточек для агентов: запрос, критика, доработка (ARCH §5, §12.4).

Единственное место, где собираются тела карточек, которые приложение кладёт
в канбан. Чистые функции: ничего не читают и не пишут, только форматируют
переданные данные и пути из `cfg`.

Тело запроса = текст Михаила ДОСЛОВНО + строка «Команда комнаты…» +
контекст-файлы + огороженный блок КОМНАТА-КОНТРАКТ с реальными путями.
Тело критика = исходный запрос + ответ директора + контракт вердикта.
Тело доработки = запрос + предыдущий ответ + замечания + тот же контракт ответа.
"""
from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path

try:  # пакетный и «плоский» импорт (python server/app.py)
    from .config import state_md_path as _state_md_path
except ImportError:  # pragma: no cover
    from config import state_md_path as _state_md_path  # type: ignore

MAX_NOTE_LEN = 70

# роли комнаты → русские подписи в теле карточки
ROLE_RU = {"worker": "исполнитель", "critic": "критик", "observer": "наблюдатель", "orchestrator": "директор"}

CONTRACT_OPEN = "<<<КОМНАТА-КОНТРАКТ  (относится ТОЛЬКО к финальному ответу корневой карточки; в подзадачи не копировать)"
CONTRACT_CLOSE = "КОМНАТА-КОНТРАКТ>>>"
CRITIC_OPEN = "<<<КОНТРАКТ-КРИТИКА  (относится ТОЛЬКО к этой карточке; в подзадачи не копировать)"
CRITIC_CLOSE = "КОНТРАКТ-КРИТИКА>>>"

#: режим БРИФ (волна 9, §2): инструкция «не исследуй глубоко, выдай карту».
#: Своя пара маркеров, чтобы блок можно было вырезать на чтении, не трогая
#: служебную «шапку» и не теряя текст Михаила.
BRIEF_OPEN = ("<<<РЕЖИМ БРИФ  (главный запрос комнаты: сначала карта проекта, исследование — "
              "в направлениях; в подзадачи не копировать)")
BRIEF_CLOSE = "РЕЖИМ БРИФ>>>"
#: начало блока «КОНТЕКСТ КОМНАТЫ» (context_pack) — тоже служебный, режется на чтении
CONTEXT_BLOCK_MARK = "<<<КОНТЕКСТ КОМНАТЫ"

#: статусы направления (WAVE9 §1) и поля, которые вообще бывают у направления
PLAN_STATUSES = ("proposed", "queued", "running", "waiting_quota", "review", "done", "dropped")
STREAM_FIELDS = ("id", "title", "goal", "owner", "members", "depends_on", "due", "reason")
PATCH_FIELDS = ("title", "goal", "owner", "members", "depends_on", "due", "reason", "status")
MAX_STREAMS = 8
MAX_TITLE = 120
MIN_TITLE = 3

#: строки, которые `build_request_body` дописывает ПОСЛЕ текста Михаила
#: (контракт — отдельно, ниже); на чтении они не показываются пользователю
SERVICE_HEAD_PREFIXES = (
    "Команда комнаты (назначай подзадачи только этим профилям): ",
    "Контекст проекта (файлы комнаты, читать первыми): ",
    "Приложенные файлы (копии, читать при необходимости): ",
)


def _service_markers(marker: str) -> tuple[str, ...]:
    """Маркеры, с которых начинаются служебные блоки (их в интерфейс не показываем)."""
    marks = [BRIEF_OPEN, CONTEXT_BLOCK_MARK]
    if marker:
        marks.insert(0, marker)
    return tuple(m for m in marks if m)


def _leading_service_len(text: str) -> int:
    """Сколько знаков в начале занимают служебные блоки (если они стоят ДО текста).

    Нужно, чтобы «вырезать по первому маркеру» не съело текст Михаила, когда
    служебный блок (контекст комнаты, бриф) оказался выше него: сначала снимаем
    такой блок целиком до закрывающего маркера, и только потом режем остальное.
    """
    start = 0
    while True:
        probe = text[start:].lstrip()
        if not probe:
            return start
        offset = len(text[start:]) - len(probe)
        head = next((m for m in (BRIEF_OPEN, CONTEXT_BLOCK_MARK, CONTRACT_OPEN)
                     if probe.startswith(m)), None)
        if head is None:
            return start
        close = {BRIEF_OPEN: BRIEF_CLOSE, CONTRACT_OPEN: CONTRACT_CLOSE}.get(head, "КОНТЕКСТ КОМНАТЫ>>>")
        end = probe.find(close)
        if end < 0:
            return start          # блока не закрыли — режем как есть, по первому маркеру
        start = start + offset + end + len(close)


def visible_text(body, marker: str = CONTRACT_OPEN) -> str:
    """Текст запроса для интерфейса: только то, что написал Михаил.

    В теле карточки лежит ещё служебная часть для агентов: блок ``КОМНАТА-КОНТРАКТ``,
    режим ``БРИФ`` (волна 9) и блок ``КОНТЕКСТ КОМНАТЫ`` (волна 8), а также
    дописанная «шапка» (команда комнаты, контекст, вложения). Пользователю они не
    нужны, поэтому режем их ТОЛЬКО здесь, на чтении: в самой карточке тело остаётся
    полным, иначе агенты не увидят контракт.

    Режем по ПЕРВОМУ служебному маркеру — но если служебный блок стоит в начале
    тела (перед текстом Михаила), сначала снимаем его целиком, чтобы текст не
    потерялся. Старые карточки режутся по тому же маркеру контракта, так что
    контракт не всплывает и в них. Пользовательский текст, где встретилось слово
    «КОМНАТА-КОНТРАКТ» без полного маркера, не обрезается.
    """
    text = body if isinstance(body, str) else ""
    if not text:
        return ""
    start = _leading_service_len(text)
    if start:
        text = text[start:].lstrip("\n")
    index = -1
    for mark in _service_markers(marker):
        pos = text.find(mark)
        if pos >= 0 and (index < 0 or pos < index):
            index = pos
    if index >= 0:
        text = text[:index]
    lines = text.rstrip("\n").split("\n")
    cut = len(lines)
    while cut > 0:
        line = lines[cut - 1].strip()
        if not line or line.startswith(SERVICE_HEAD_PREFIXES):
            cut -= 1
            continue
        break
    service = any(lines[i].strip().startswith(SERVICE_HEAD_PREFIXES) for i in range(cut, len(lines)))
    if service:
        lines = lines[:cut]
    return "\n".join(lines).rstrip()


# ---------------------------------------------------------------- утилиты

def _p(value) -> str:
    """Путь в нативном для ОС виде (Windows — обратные слэши), пусто → ''."""
    if value is None or value == "":
        return ""
    try:
        return str(Path(value))
    except (TypeError, ValueError):
        return str(value)


def room_id(room: dict) -> str:
    return str(room.get("id") or "")


def room_title(room: dict) -> str:
    return str(room.get("title") or room_id(room) or "Комната")


def state_path(room: dict, cfg) -> str:
    """Путь к STATE.md комнаты — тот же, куда пишет ingest (`config.state_md_path`)."""
    return _p(_state_md_path(cfg, room_id(room)))


def reports_dir(room: dict, slug: str, cfg) -> str:
    """Каталог отчётов конкретного запроса: data/reports/<комната>/<slug>/."""
    return _p(Path(cfg.reports_dir) / room_id(room) / str(slug or ""))


def reportkit_spec(cfg) -> str:
    return _p(Path(cfg.reportkit_dir) / "SPEC.md")


def context_files(room: dict) -> list[str]:
    """Абсолютные пути контекст-файлов комнаты (как есть, без проверки существования)."""
    out: list[str] = []
    for item in room.get("context_files") or []:
        path = _p(item)
        if path and path not in out:
            out.append(path)
    return out


def _short(text: str, limit: int = MAX_NOTE_LEN) -> str:
    text = " ".join(str(text or "").split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def roster(room: dict) -> list[dict]:
    """Состав комнаты для карточки: [{profile, role, label, note}] без повторов.

    Порядок: руководитель комнаты, участники, критик. Роли — по-русски
    (ключи профилей не меняем, меняем только подписи).
    """
    out: list[dict] = []
    seen: set[str] = set()
    orchestrator = (room.get("orchestrator") or "").strip()
    if orchestrator:
        out.append({"profile": orchestrator, "role": "orchestrator",
                    "label": "руководитель комнаты", "note": ""})
        seen.add(orchestrator)
    for member in room.get("members") or []:
        profile = (member.get("profile") or "").strip() if isinstance(member, dict) else str(member).strip()
        if not profile or profile in seen:
            continue
        seen.add(profile)
        role = str((member.get("role") if isinstance(member, dict) else None) or "worker")
        note = _short(member.get("note") or "") if isinstance(member, dict) else ""
        out.append({"profile": profile, "role": role, "label": ROLE_RU.get(role, role), "note": note})
    critic = (room.get("critic") or "").strip()
    if critic and critic not in seen:
        out.append({"profile": critic, "role": "critic", "label": "критик", "note": ""})
    return out


def team_line(room: dict) -> str:
    """«Команда комнаты (назначай подзадачи только этим профилям): …»."""
    parts: list[str] = []
    for item in roster(room):
        label = "директор комнаты" if item["role"] == "orchestrator" else item["label"]
        tail = f" ({item['note']})" if item["note"] else ""
        parts.append(f"{item['profile']} — {label}{tail}")
    if not parts:
        return ""
    return "Команда комнаты (назначай подзадачи только этим профилям): " + "; ".join(parts)


def critic_settings_line(room: dict) -> str:
    """Строка контракта про критика: настройка комнаты, спрашивать не нужно (W13-E §7).

    Возвращает '' если критика в комнате нет. Руководитель не должен задавать Михаилу
    вопрос «сколько раундов/прогонов критика делать» — это свойство комнаты.
    """
    room = room if isinstance(room, dict) else {}
    critic = str(room.get("critic") or "").strip()
    if not critic:
        return ""
    mode = str(room.get("critic_mode") or "").strip()
    if mode == "until_approved":
        cap = room.get("critic_round_cap") or 12
        value = "проверяет ответ, пока не одобрит полностью (страховочный потолок — %s раундов)" % cap
    else:
        try:
            rounds = max(1, min(5, int(room.get("max_rounds") or 1)))
        except (TypeError, ValueError):
            rounds = 1
        value = "максимум %d раунд(а) проверки" % rounds
    return (f"Проверка критиком ({critic}) уже настроена в комнате: {value}. "
            "Раунд — один цикл «критик проверил → доработали → снова проверили». "
            "Не спрашивай Михаила, сколько раундов делать, и не добавляй такой вопрос "
            "в open_questions — значение берётся из настроек комнаты.")


def build_contract_block(room: dict, slug: str, cfg) -> str:
    """Огороженный блок КОМНАТА-КОНТРАКТ по ARCH §5 с реальными путями."""
    state = state_path(room, cfg)
    spec = reportkit_spec(cfg)
    rep = reports_dir(room, slug, cfg)
    ctx = [state] + context_files(room)
    return "\n".join([line for line in [
        CONTRACT_OPEN,
        critic_settings_line(room),
        f"Комната: {room_title(room)}. Контекст проекта (читать первым): {', '.join(ctx)}.",
        "Корневая карточка завершается kanban_complete(summary=<готовый ответ Михаилу в Markdown, "
        "по-русски, ОДИН связный текст, а не пересказ отчётов подряд>, metadata={...}).",
        "metadata (все поля необязательны, но заполняй, что знаешь):",
        '  title:        "<короткое название запроса, 3–60 знаков, без «Так,…» и без markdown>"   # как запрос выглядит в списках и меню; полный текст Михаила не меняется',
        '  next_steps:   [{"text","owner"(профиль),"due"(YYYY-MM-DD),"from_question"?}]   # 3–7 конкретных шагов; from_question — id вопроса вида q_…, из которого шаг',
        '  open_questions:[{"text","for":"mikhail|<профиль>"}]               # что нужно решить Михаилу',
        '  decisions:    [{"text","why","status":"proposed|accepted"}]',
        ('  changes:      ["<что изменилось в своде по сравнению с предыдущей версией>", "…"]   # W17-S §3: 3–7 коротких пунктов; Михаил видит их как «Что изменилось»' if slug == "revision" else ''),
        ('  version_reason: "<почему сделана эта версия: по замечанию Михаила / по вашим ответам на вопросы / по замечаниям критика>"  # W17-S §3: подпись версии' if slug == "revision" else ''),
        '  stage:        {"current":"<id фазы>","phases":[{"id","title","status":"done|active|next|blocked","note"}]}',
        '  future_questions:["…"]                                            # вопросы на будущее, отдельно от текущего ответа',
        '  artifacts:    [{"path","title","kind"}]                           # файлы, созданные в ходе работы',
        f'  report:       "<путь к report.json или report.html>"              # интерактивный отчёт, формат — {spec}',
        f"Если нужны таблицы/графики/связи/схемы/картинки — сделай интерактивный отчёт: "
        f"запиши JSON по {spec} в {_p(Path(rep) / 'report.json')}.",
        "Перед финальным ответом ПЕРЕЧИТАЙ комментарии карточки (kanban_show): сообщения с меткой "
        "`[Михаил · уточнение]` и `[Руководитель → …]` обязательны к учёту; если они меняют задачу — "
        "скорректируй работу, а в ответе напиши блок «Учтено из уточнений» (что именно учтено и "
        "как изменился результат).",
        "Отвечай по-русски; один связный ответ; в подзадачи этот контракт не копировать.",
        CONTRACT_CLOSE,
    ] if line])


def _join_blocks(*parts: str) -> str:
    return "\n\n".join(p.strip("\n") for p in parts if p and p.strip("\n"))


# ---------------------------------------------------------------- запрос

def _head_lines(room: dict, attach=None) -> list[str]:
    """Служебная «шапка» тела: команда комнаты, контекст-файлы, вложения."""
    head: list[str] = []
    line = team_line(room)
    if line:
        head.append(line)
    ctx = context_files(room)
    if ctx:
        head.append("Контекст проекта (файлы комнаты, читать первыми): " + "; ".join(ctx))
    if attach:
        head.append("Приложенные файлы (копии, читать при необходимости): " + "; ".join(_p(a) for a in attach))
    return head


def build_request_body(room: dict, text: str, slug: str, cfg, attach=None, brief: bool = False) -> str:
    """Тело корневой карточки: дословный текст Михаила + команда + контекст + контракт.

    `brief=True` (волна 9, §2) — тот же каркас, но вместо обычного контракта
    вставляется режим БРИФ: руководитель не исследует, а выдаёт понимание задачи,
    вопросы, блок `plan` (3–8 направлений) и `stage`.
    """
    if brief:
        return build_brief_body(room, text, slug, cfg, attach)
    original = text if text is not None else ""
    block = build_contract_block(room, slug, cfg)
    return _join_blocks(original, "\n".join(_head_lines(room, attach)), block)


# ---------------------------------------------------------------- бриф и карта

def _roster_line(room: dict) -> str:
    """«Кого можно ставить (только эти профили): …» — состав комнаты с ролями."""
    items = roster(room)
    if not items:
        return "Кого можно ставить: состав комнаты не задан — направлений без исполнителя не предлагай."
    pairs = "; ".join(f"{item['profile']} — {item['label']}" for item in items)
    return "Кого можно ставить (только эти профили): " + pairs + "."


def _brief_block(room: dict, lead: str) -> str:
    """Огороженный блок РЕЖИМ БРИФ: что выдать и как оформить блок `plan` (§2)."""
    lines = [
        BRIEF_OPEN,
        lead,
        "Выдай в ответе (по-русски, один связный текст):",
        "1) «Как я понял задачу» — 3–6 строк: что нужно получить и что уже известно.",
        "2) «Вопросы к Михаилу» — только то, без чего нельзя начать (0–5); у каждого — "
        "вариант ответа по умолчанию.",
        "3) «Карта проекта» — 3–8 направлений: что делаем, зачем, кто ведёт, кто помогает, "
        "что должно быть готово раньше, срок.",
        "4) «Этапы» — обычный блок stage (текущая фаза и фазы проекта).",
        "В metadata карточки обязательно:",
        '  plan:  {"streams":[{"title":"…","goal":"…","owner":"<профиль>","members":["<профиль>"],'
        '"depends_on":["<id направления>"],"due":"YYYY-MM-DD"|null,'
        '"reason":"почему это отдельное направление и такой порядок"}]}',
        '  stage: {"current":"<id фазы>","phases":[…]}.',
        "Правила карты: направлений 3–8; owner и members — ТОЛЬКО профили из списка ниже; "
        "depends_on — id направлений из этого же ответа (id можно не писать, приложение "
        "присвоит s1, s2, …); due — дата YYYY-MM-DD или null; reason — одна строка по-русски.",
        _roster_line(room),
        "Карточки не создавай и ничего не запускай: карту запускает Михаил кнопкой «Запустить».",
        BRIEF_CLOSE,
    ]
    return "\n".join(lines)


BRIEF_LEAD = ("Это ГЛАВНЫЙ запрос комнаты (бриф проекта). Не исследуй глубоко: карту собираешь по "
              "тексту запроса, контексту комнаты и Obsidian — подробное исследование сделают "
              "направления после «Запустить».")


class _CfgStub:
    """Минимальный cfg, если вызывающий его не передал (пути вырождаются в относительные)."""

    vault_dir = ""
    reports_dir = ""
    reportkit_dir = ""


def build_brief_body(room, text=None, slug="brief", cfg=None, attach=None) -> str:
    """Тело карточки в режиме БРИФ: текст Михаила + шапка + инструкция карты + контракт.

    Порядок аргументов — как у `build_request_body` (комната первая), но принимается
    и вызов из `engine_write.brief_body`: `build_brief_body(cfg, room, body)`.
    """
    if cfg is None and not isinstance(room, dict) and isinstance(text, dict):
        room, cfg, text = text, room, (slug if isinstance(slug, str) else "")
        slug = "brief"
    room = room if isinstance(room, dict) else {}
    cfg = cfg if cfg is not None else _CfgStub()
    original = text if isinstance(text, str) else ""
    return _join_blocks(original, "\n".join(_head_lines(room, attach)),
                        _brief_block(room, BRIEF_LEAD), build_contract_block(room, str(slug or "brief"), cfg))


_STATUS_RU_PLAN = {
    "done": "готов", "working": "в работе", "running": "в работе", "queued": "в очереди",
    "needs_you": "ждёт решения", "reviewing": "на проверке", "review": "на проверке",
    "failed": "ошибка", "blocked": "остановлен", "archived": "в архиве", "draft": "черновик",
    "synthesizing": "сводится", "triage": "ждёт разбора",
}


def _fmt_existing(index: int, item) -> str:
    """Строка перечня: «1. «Название» — готов, 2026-10-20 — одна строка итога»."""
    if isinstance(item, str):
        return f"{index}. {_short(item, 200)}"
    if not isinstance(item, dict):
        return ""
    title = _short(item.get("title") or item.get("name") or item.get("id") or "", 120) or "(без названия)"
    status = _STATUS_RU_PLAN.get(str(item.get("status") or "").lower(), str(item.get("status") or ""))
    at = item.get("finished_at") or item.get("at") or item.get("created_at")
    when = ""
    try:
        if isinstance(at, (int, float)) and at > 0:
            when = time.strftime("%d.%m.%Y", time.localtime(at))
        elif isinstance(at, str) and at.strip():
            when = at.strip()[:10]
    except (OverflowError, OSError, ValueError):
        when = ""
    head = " — ".join(part for part in (status, when) if part)
    summary = _short(item.get("summary") or item.get("summary_line") or item.get("answer")
                     or item.get("preview") or "", 160)
    row = f"{index}. «{title}»"
    row += f" — {head}" if head else ""
    row += f" — {summary}" if summary else ""
    return row


def build_map_body(room=None, existing_requests=None, text="", slug="map", cfg=None, attach=None) -> str:
    """Тело запроса «Собрать карту из запросов комнаты» (A7): перечень + режим БРИФ.

    Принимает и `(room, existing_requests, …)`, и `(existing_requests, room, …)`:
    порядок первых двух аргументов определяется по типу (комната — словарь,
    запросы — список), чтобы вызывающему не приходилось помнить.
    """
    if isinstance(room, (list, tuple)) and not isinstance(existing_requests, (list, tuple)):
        room, existing_requests = (existing_requests if isinstance(existing_requests, dict) else {}), list(room)
    room = room if isinstance(room, dict) else {}
    items = [i for i in (existing_requests or [])]
    lines = [
        "Собери карту проекта из того, что уже есть в комнате "
        "(«Собрать карту из запросов комнаты»).",
    ]
    if items:
        lines.append(f"Существующие запросы комнаты ({len(items)}):")
        rows = [_fmt_existing(idx, item) for idx, item in enumerate(items, 1)]
        lines.extend(row for row in rows if row)
        lines.append("Не предлагай заново то, что уже сделано: направления — это то, что осталось "
                     "сделать, и то, что нужно переделать.")
    else:
        lines.append("Существующих запросов в комнате пока нет — собери карту по тексту ниже.")
    lead = "\n".join(lines)
    original = text if isinstance(text, str) and text.strip() else (
        "Собери карту проекта из запросов, которые уже есть в комнате.")
    return _join_blocks(original, "\n".join(_head_lines(room, attach)),
                        _brief_block(room, lead), build_contract_block(room, slug, cfg))


# ---------------------------------------------------------------- парсер plan

def _meta_dict(meta) -> dict:
    """metadata → dict: принимаем словарь, JSON-строку и уже нормализованную форму."""
    if isinstance(meta, dict):
        return meta
    if isinstance(meta, (bytes, bytearray)):
        try:
            meta = meta.decode("utf-8", "replace")
        except Exception:  # noqa: BLE001
            return {}
    if isinstance(meta, str):
        raw = meta.strip()
        if not raw:
            return {}
        try:
            obj = json.loads(raw, strict=False)
        except (ValueError, TypeError):
            return {}
        return obj if isinstance(obj, dict) else {}
    return {}


def _block_value(meta, name: str):
    """Значение необязательного блока: ключ верхнего уровня или внутри `extra`."""
    data = _meta_dict(meta)
    if name in data:
        return data[name], True
    extra = data.get("extra")
    if isinstance(extra, dict) and name in extra:
        return extra[name], True
    return None, False


def _as_text(value, limit: int = 0) -> str:
    if value is None or isinstance(value, bool):
        return ""
    if not isinstance(value, (str, int, float)):
        return ""
    text = " ".join(str(value).split())
    return text[:limit] if limit and len(text) > limit else text


def _as_list(value) -> list[str]:
    """Список строк: строка «a, b» тоже принимается (агенты пишут по-разному)."""
    if value is None or isinstance(value, bool):
        return []
    if isinstance(value, str):
        parts = [p.strip() for p in value.replace(";", ",").split(",")]
    elif isinstance(value, (list, tuple)):
        parts = []
        for item in value:
            if isinstance(item, dict):
                parts.append(_as_text(item.get("profile") or item.get("id") or item.get("name")))
            else:
                parts.append(_as_text(item))
    else:
        return []
    out: list[str] = []
    for part in parts:
        if part and part not in out:
            out.append(part)
    return out


def _due(value):
    """Дата направления через единственный парсер нормализации (`normalize_base`)."""
    try:
        from .normalize_base import parse_due
    except ImportError:  # pragma: no cover
        from normalize_base import parse_due  # type: ignore
    return parse_due(value)


def _allowed_profiles(room) -> set[str]:
    if not isinstance(room, dict):
        return set()
    return {item["profile"] for item in roster(room)}


def _stream(raw, index: int, warnings: list[str], allowed: set[str]) -> dict | None:
    """Одно направление из блока `plan`: лишние поля отбрасываются, битое — в warnings."""
    if not isinstance(raw, dict):
        warnings.append(f"Направление №{index} пропущено: это не объект с полями.")
        return None
    title = _as_text(raw.get("title") or raw.get("name") or raw.get("what"), MAX_TITLE)
    if len(title) < MIN_TITLE:
        warnings.append(f"Направление №{index} пропущено: нет названия (3–120 знаков).")
        return None
    owner = _as_text(raw.get("owner") or raw.get("assignee") or raw.get("who")) or None
    members = _as_list(raw.get("members") or raw.get("team"))
    if allowed:
        if owner and owner not in allowed:
            warnings.append(f"Направление «{_short(title, 60)}»: исполнитель {owner} не из состава "
                            f"комнаты — оставлено без ответственного.")
            owner = None
        alien = [m for m in members if m not in allowed]
        if alien:
            warnings.append(f"Направление «{_short(title, 60)}»: участники вне состава комнаты "
                            f"убраны ({', '.join(alien)}).")
            members = [m for m in members if m in allowed]
        if owner:
            members = [m for m in members if m != owner]
    status = _as_text(raw.get("status")).lower()
    stream = {
        "id": _as_text(raw.get("id")) or None,
        "title": title,
        "goal": _as_text(raw.get("goal") or raw.get("why") or raw.get("what_for")),
        "owner": owner,
        "members": members,
        "depends_on": _as_list(raw.get("depends_on") or raw.get("after") or raw.get("blocked_by")),
        "due": _due(raw.get("due") or raw.get("deadline")),
        "reason": _as_text(raw.get("reason") or raw.get("note")),
    }
    if status:
        if status in PLAN_STATUSES:
            stream["status"] = status
        else:
            warnings.append(f"Направление «{_short(title, 60)}»: статус «{status}» неизвестен — "
                            f"будет предложено как обычно.")
    return stream


def parse_plan(meta, room=None) -> tuple[dict | None, list[str]]:
    """Блок `plan` из metadata → ({streams:[…]}|None, warnings). Никогда не бросает.

    Толерантность как у остального контракта: блока нет — не ошибка (пустой список
    предупреждений); блок битый или пустой — предупреждение по-русски, карта НЕ
    создаётся (`None`); лишние поля направления отбрасываются; исполнители вне
    состава комнаты (если `room` передан) убираются с предупреждением.
    """
    warnings: list[str] = []
    try:
        raw, present = _block_value(meta, "plan")
        if not present or raw is None:
            return None, warnings
        if isinstance(raw, list):
            items = raw
        elif isinstance(raw, dict):
            items = raw.get("streams")
            if items is None:
                items = raw.get("directions") or raw.get("plan") or raw.get("stream")
            if isinstance(items, dict):
                items = [items]
            if items is None:
                return None, warnings  # пустой объект — просто нет карты
        else:
            warnings.append("Блок plan не разобран: нужен объект с полем streams — карта не создана.")
            return None, warnings
        if not isinstance(items, (list, tuple)):
            warnings.append("Блок plan не разобран: streams должен быть списком — карта не создана.")
            return None, warnings
        if not items:
            warnings.append("Блок plan пуст — карта не создана.")
            return None, warnings
        if len(items) > MAX_STREAMS:
            warnings.append(f"В блоке plan {len(items)} направлений — взяты первые {MAX_STREAMS}.")
        allowed = _allowed_profiles(room)
        streams = []
        for index, item in enumerate(list(items)[:MAX_STREAMS], 1):
            stream = _stream(item, index, warnings, allowed)
            if stream is not None:
                streams.append(stream)
        if not streams:
            warnings.append("В блоке plan нет пригодных направлений — карта не создана.")
            return None, warnings
        return {"streams": streams}, warnings
    except Exception:  # noqa: BLE001 - парсер не имеет права ронять приём ответа
        return None, warnings + ["Блок plan не разобран из-за неожиданного формата — карта не создана."]


def _patch_change(raw, index: int, warnings: list[str], allowed: set[str]) -> dict | None:
    if not isinstance(raw, dict):
        warnings.append(f"Правка №{index} в plan_patch пропущена: это не объект.")
        return None
    stream_id = _as_text(raw.get("id") or raw.get("stream_id"))
    if not stream_id:
        warnings.append(f"Правка №{index} в plan_patch пропущена: нет id направления.")
        return None
    fields_raw = raw.get("fields") if isinstance(raw.get("fields"), dict) else {
        k: v for k, v in raw.items() if k not in ("id", "stream_id")}
    fields: dict = {}
    for key in PATCH_FIELDS:
        if key not in fields_raw:
            continue
        value = fields_raw[key]
        if key in ("members", "depends_on"):
            items = _as_list(value)
            if allowed and key == "members":
                items = [m for m in items if m in allowed]
            fields[key] = items
        elif key == "owner":
            owner = _as_text(value) or None
            if owner and allowed and owner not in allowed:
                warnings.append(f"Правка направления {stream_id}: исполнитель {owner} не из состава "
                                f"комнаты — поле не изменено.")
                continue
            fields[key] = owner
        elif key == "due":
            fields[key] = _due(value)
        elif key == "status":
            status = _as_text(value).lower()
            if status in PLAN_STATUSES:
                fields[key] = status
            else:
                warnings.append(f"Правка направления {stream_id}: статус «{status}» неизвестен — "
                                f"поле не изменено.")
        else:
            limit = MAX_TITLE if key == "title" else 0
            text = _as_text(value, limit)
            if key == "title" and text and len(text) < MIN_TITLE:
                warnings.append(f"Правка направления {stream_id}: заголовок короче 3 знаков — "
                                f"поле не изменено.")
                continue
            fields[key] = text
    if not fields:
        warnings.append(f"Правка направления {stream_id} пуста — пропущена.")
        return None
    return {"id": stream_id, "fields": fields}


def parse_plan_patch(meta, room=None) -> tuple[dict | None, list[str]]:
    """Блок `plan_patch` из metadata → ({add, change, drop, note}|None, warnings).

    Формат §2: `{add:[stream], change:[{id,fields}], drop:[{id,reason}], note}`.
    Битый блок — предупреждение и `None` (ничего не применяется). Лишние поля
    и неизвестные статусы отбрасываются с предупреждением.
    """
    warnings: list[str] = []
    try:
        raw, present = _block_value(meta, "plan_patch")
        if not present or raw is None:
            return None, warnings
        if not isinstance(raw, dict):
            warnings.append("Блок plan_patch не разобран: нужен объект — правки не применены.")
            return None, warnings
        allowed = _allowed_profiles(room)
        patch: dict = {"add": [], "change": [], "drop": [], "note": _as_text(raw.get("note")
                                                                            or raw.get("reason"))}
        add = raw.get("add")
        for index, item in enumerate(list(add) if isinstance(add, (list, tuple)) else ([add] if add else []), 1):
            stream = _stream(item, index, warnings, allowed)
            if stream is not None:
                patch["add"].append(stream)
        change = raw.get("change") or raw.get("changes")
        for index, item in enumerate(list(change) if isinstance(change, (list, tuple))
                                     else ([change] if change else []), 1):
            fixed = _patch_change(item, index, warnings, allowed)
            if fixed is not None:
                patch["change"].append(fixed)
        drop = raw.get("drop") or raw.get("remove")
        for item in (list(drop) if isinstance(drop, (list, tuple)) else ([drop] if drop else [])):
            if isinstance(item, str):
                stream_id, reason = _as_text(item), ""
            elif isinstance(item, dict):
                stream_id = _as_text(item.get("id") or item.get("stream_id"))
                reason = _as_text(item.get("reason") or item.get("why"))
            else:
                continue
            if not stream_id:
                warnings.append("В plan_patch есть удаление без id направления — пропущено.")
                continue
            patch["drop"].append({"id": stream_id, "reason": reason})
        if not (patch["add"] or patch["change"] or patch["drop"]):
            warnings.append("В plan_patch нет пригодных правок — ничего не применено.")
            return None, warnings
        return patch, warnings
    except Exception:  # noqa: BLE001 - парсер не имеет права ронять приём ответа
        return None, warnings + ["Блок plan_patch не разобран из-за неожиданного формата — "
                                 "правки не применены."]


def parse_plan_metadata(meta, room=None) -> dict:
    """Оба необязательных блока сразу: {plan, plan_patch, warnings}. Никогда не бросает."""
    plan, warnings = parse_plan(meta, room)
    patch, patch_warnings = parse_plan_patch(meta, room)
    return {"plan": plan, "plan_patch": patch, "warnings": warnings + patch_warnings}


# ---------------------------------------------------------------- критика

def _fmt_answer(answer_md: str) -> str:
    return _join_blocks("<<<ОТВЕТ ДИРЕКТОРА (проверяется)>>>", answer_md or "", "<<<КОНЕЦ ОТВЕТА>>>")


def build_critic_body(room: dict, request_text: str, answer_md: str, cfg) -> str:
    """Тело карточки-критика: исходный запрос + ответ корня + контракт вердикта (§12.4)."""
    orchestrator = room.get("orchestrator") or "директор"
    critic = room.get("critic") or "критик"
    contract_block = "\n".join([
        CRITIC_OPEN,
        f"Комната: {room_title(room)}. Ты — критик комнаты ({critic}); проверяешь ответ директора ({orchestrator}).",
        "Проверь ответ по существу: факты и ссылки на источники, полноту относительно запроса, "
        "противоречия между разделами, выполнимость шагов и ясность для Михаила.",
        "Завершай карточку так: kanban_complete(summary=<вердикт 2–4 предложения, по-русски, один связный текст>, "
        'metadata={"verdict":"approve|revise","issues":[{"where":"<раздел или факт>",'
        '"problem":"<что не так>","fix":"<что исправить>"}]}).',
        'Правила: verdict — строго "approve" или "revise"; approve — только если ответ можно отдавать Михаилу '
        "без правок; при revise — 1–5 конкретных issues с точным местом и исправлением.",
        "Отвечай по-русски; один связный ответ; в подзадачи этот контракт не копировать.",
        CRITIC_CLOSE,
    ])
    return _join_blocks(request_text, _fmt_answer(answer_md), contract_block)


# ---------------------------------------------------------------- доработка

def _fmt_issues(issues) -> str:
    """Замечания критика: список dict {where,problem,fix} или строк."""
    lines: list[str] = []
    for idx, item in enumerate(issues or [], 1):
        if isinstance(item, dict):
            where = _short(item.get("where") or "", 200)
            problem = _short(item.get("problem") or "", 400)
            fix = _short(item.get("fix") or "", 400)
            row = f"{idx}. "
            row += f"Где: {where}. " if where else ""
            row += f"Проблема: {problem}. " if problem else ""
            row += f"Исправить: {fix}" if fix else ""
            lines.append(row.rstrip())
        else:
            text = _short(item, 600)
            if text:
                lines.append(f"{idx}. {text}")
    return "\n".join(lines)


# ------------------------------------------------- дайджест «что появилось после версии»

#: сколько знаков оставляем от одного ответа/уточнения в дайджесте
DIGEST_ITEM_MAX = 700
#: сколько пунктов каждого вида берём (самые новые), чтобы тело карточки не распухло
DIGEST_ITEMS_MAX = 14
DIGEST_TOTAL_MAX = 9000
#: как читается статус ответа в дайджесте (ключи — `answers.STATE_RU`)
DIGEST_ANSWER_RU = {"sent": "ждёт реакции руководителя", "received": "руководитель прочитал",
                    "applied": "учтено руководителем", "no_change": "руководитель решил не менять",
                    "waiting_explanation": "вы просили пояснение"}


def _at_of(item) -> int:
    """Время записи числом (`at` / `answered_at` / `created_at`), иначе 0."""
    item = item if isinstance(item, dict) else {}
    for key in ("at", "answered_at", "created_at"):
        try:
            value = int(item.get(key) or 0)
        except (TypeError, ValueError):
            value = 0
        if value:
            return value
    return 0


def _digest_date(at: int) -> str:
    if not at:
        return ""
    try:
        return time.strftime("%d.%m %H:%M", time.localtime(int(at)))
    except (ValueError, OSError):      # pragma: no cover - экзотические TZ
        return ""


def version_cutoff(detail: dict) -> tuple[int, int]:
    """Момент выпуска показанной версии свода и её номер: ``(at, version)``.

    `at` = 0 — версии ещё нет (первая доработка): в дайджест попадает всё, что есть.
    """
    syn = detail.get("synthesis") if isinstance(detail, dict) else {}
    syn = syn if isinstance(syn, dict) else {}
    hist = [h for h in (syn.get("history") or []) if isinstance(h, dict)]
    if hist:
        last = hist[-1]
        return _at_of(last), int(last.get("version") or len(hist) or 0)
    return _at_of(syn), int(syn.get("version") or 0)


def _digest_answers(detail: dict, record: dict, cutoff: int) -> list[dict]:
    """Ответы Михаила на вопросы, данные ПОСЛЕ выпуска версии (вопрос → ответ)."""
    stored = (record or {}).get("answers") if isinstance((record or {}).get("answers"), dict) else {}
    enriched = {}
    block = detail.get("answers") if isinstance(detail.get("answers"), dict) else {}
    for item in block.get("items") or []:
        if isinstance(item, dict) and item.get("question_id"):
            enriched[str(item["question_id"])] = item
    out = []
    for question in detail.get("open_questions") or []:
        if not isinstance(question, dict):
            continue
        qid = str(question.get("id") or "")
        extra = enriched.get(qid) or {}
        record_q = stored.get(qid) if isinstance(stored.get(qid), dict) else {}
        answer = _as_text(question.get("answer") or extra.get("answer") or record_q.get("text"))
        at = _at_of({"at": question.get("answered_at") or extra.get("at") or record_q.get("at")})
        if not answer or at <= cutoff:
            continue
        state = str(question.get("answer_state") or extra.get("state") or record_q.get("status") or "")
        reaction = question.get("reaction") if isinstance(question.get("reaction"), dict) else {}
        out.append({"question_id": qid or None, "question": _short(question.get("text"), 500),
                    "answer": _short(answer, DIGEST_ITEM_MAX), "at": at, "state": state,
                    "state_ru": DIGEST_ANSWER_RU.get(state, ""),
                    "reaction": _short(reaction.get("text"), 300) if reaction else ""})
    return out[-DIGEST_ITEMS_MAX:]


def _digest_notes(detail: dict, thread, cutoff: int) -> list[dict]:
    """Уточнения Михаила в диалоге после версии: полный текст из ленты, иначе из событий."""
    source = thread if isinstance(thread, list) and thread else (detail.get("thread") or [])
    source = source if isinstance(source, list) and source else (detail.get("timeline") or [])
    out = []
    for item in source or []:
        if not isinstance(item, dict) or item.get("pending"):
            continue
        if str(item.get("author") or item.get("actor") or "") != "mikhail":
            continue
        if str(item.get("kind") or "") in ("event", "system"):
            continue                      # служебные строки ленты — не уточнения
        at = _at_of(item)
        text = _as_text(item.get("text"))
        if not text or at <= cutoff:
            continue
        out.append({"at": at, "text": _short(text, DIGEST_ITEM_MAX),
                    "attach": [str(p) for p in (item.get("attach") or [])][:5]})
    return out[-DIGEST_ITEMS_MAX:]


def _digest_decisions(detail: dict, cutoff: int) -> list[dict]:
    """Решения, появившиеся после версии (принятые и предложенные)."""
    out = []
    for item in detail.get("decisions") or []:
        if not isinstance(item, dict):
            continue
        at = _at_of(item)
        text = _as_text(item.get("text"))
        if not text or at <= cutoff:
            continue
        status = str(item.get("status") or "proposed")
        if status == "rejected":
            continue
        out.append({"text": _short(text, 400), "status": status,
                    "status_ru": "принято" if status == "accepted" else "предложено", "at": at})
    return out[-DIGEST_ITEMS_MAX:]


def _digest_steps(detail: dict, record: dict | None = None, cutoff: int = 0) -> list[dict]:
    """Шаги, которые Михаил согласовал, прокомментировал, переместил или убрал."""
    actions = (record or {}).get("step_actions") if isinstance((record or {}).get("step_actions"), dict) else {}
    labels = {"agree": "согласовано вами", "comment": "прокомментировано вами",
              "move": "перемещено вами", "drop": "убран"}
    out = []
    for item in detail.get("next_steps") or []:
        if not isinstance(item, dict):
            continue
        text = _as_text(item.get("text"))
        if not text:
            continue
        step_id = str(item.get("id") or "")
        state = str(item.get("state") or "")
        action = actions.get(step_id) if isinstance(actions.get(step_id), dict) else {}
        action = action or (item.get("user_action") if isinstance(item.get("user_action"), dict) else {})
        kind = str(action.get("kind") or "")
        at = int(action.get("at") or item.get("user_action_at") or 0)
        if state in ("dismissed", "cancelled"):
            label = _as_text(item.get("state_label")) or ("убран" if state == "dismissed" else "снят")
        elif kind and at > cutoff:
            label = labels.get(kind, _as_text(item.get("user_action_label")) or "учтено вами")
            if kind == "move":
                bits = []
                if action.get("due"):
                    bits.append("срок " + str(action["due"]))
                if action.get("owner"):
                    bits.append("исполнитель " + str(action["owner"]))
                if bits:
                    label += ": " + ", ".join(bits)
        elif item.get("conflicts_with_answer"):
            label = "противоречит вашему ответу"
        else:
            continue
        out.append({"text": _short(text, 400), "state": state or kind, "state_ru": label})
    return out[-DIGEST_ITEMS_MAX:]


def revision_digest(detail: dict, room: dict | None = None, record: dict | None = None,
                    thread=None, cutoff=None, version=None) -> dict:
    """Дайджест «что появилось после выпуска текущей версии свода» (W17-S §2).

    Единственное место, где решается, ЧТО из беседы попадёт в карточку доработки:
    ответы Михаила на вопросы, его уточнения в диалоге, новые решения и убранные
    шаги. Тот же расчёт использует предпросмотр «Что получит руководитель» и
    отметка «свод устарел», поэтому интерфейс и тело карточки не расходятся.

    Возвращает ``{version, since_at, count, answers, notes, decisions, steps, text}``:
    `count` — сколько ВАШИХ ответов и уточнений ещё не учтено (это число показывает
    интерфейс), `text` — готовый блок для тела карточки. Никогда не бросает.
    """
    detail = detail if isinstance(detail, dict) else {}
    try:
        cut_at, cut_version = version_cutoff(detail)
        if cutoff is not None:
            cut_at = int(cutoff or 0)
        if version is not None:
            cut_version = int(version or 0)
        answers = _digest_answers(detail, record or {}, cut_at)
        notes = _digest_notes(detail, thread, cut_at)
        decisions = _digest_decisions(detail, cut_at)
        steps = _digest_steps(detail, record or {}, cut_at)
    except Exception:  # noqa: BLE001 - дайджест не имеет права ронять доработку
        return {"version": 0, "since_at": None, "count": 0, "answers": [], "notes": [],
                "decisions": [], "steps": [], "text": ""}
    lines: list[str] = []
    if answers:
        lines.append(f"Ответы Михаила на вопросы — {len(answers)}:")
        for idx, item in enumerate(answers, 1):
            line = f"{idx}. Вопрос: {item['question']}\n   Ответ: {item['answer']}"
            if item["state_ru"]:
                line += f"\n   Реакция руководителя: {item['state_ru']}."
            if item["reaction"]:
                line += f" «{item['reaction']}»"
            lines.append(line)
    if notes:
        lines.append(f"Уточнения Михаила в диалоге — {len(notes)}:")
        for idx, item in enumerate(notes, 1):
            date = _digest_date(item["at"])
            stamp = f"[{date}] " if date else ""
            line = f"{idx}. {stamp}{item['text']}"
            if item["attach"]:
                line += "\n   Приложенные пути: " + "; ".join(item["attach"])
            lines.append(line)
    if decisions:
        lines.append(f"Решения Михаила — {len(decisions)}:")
        for idx, item in enumerate(decisions, 1):
            lines.append(f"{idx}. {item['text']} ({item['status_ru']})")
    if steps:
        lines.append(f"Шаги, которые Михаил согласовал, переместил или убрал — {len(steps)}:")
        for idx, item in enumerate(steps, 1):
            lines.append(f"{idx}. {item['text']} ({item['state_ru']})")
    text = "\n".join(lines)
    if len(text) > DIGEST_TOTAL_MAX:
        text = text[: DIGEST_TOTAL_MAX - 1].rstrip() + "\n…(дальше не поместилось)"
    return {"version": int(cut_version or 0), "since_at": cut_at or None,
            "count": len(answers) + len(notes), "answers": answers, "notes": notes,
            "decisions": decisions, "steps": steps, "text": text}


def build_revision_body(room: dict, request_text: str, prev_answer_md: str, issues=None,
                        user_note: str | None = None, cfg=None, digest=None) -> str:
    """Тело карточки-доработки: запрос + предыдущий ответ + замечания + дайджест + контракт.

    `digest` (W17-S §2) — результат `revision_digest`: ответы Михаила, его уточнения,
    решения и убранные шаги с момента выпуска текущей версии. Без него руководитель
    видит только замечание и свод, а беседу ему приходится перечитывать самому.
    """
    slug = "revision"
    if cfg is None:
        raise ValueError("build_revision_body: нужен cfg (пути контракта)")
    parts: list[str] = []
    parts.append(request_text or "")
    parts.append(_join_blocks("<<<ПРЕДЫДУЩИЙ ОТВЕТ (дорабатывается)>>>", prev_answer_md or "", "<<<КОНЕЦ ПРЕДЫДУЩЕГО ОТВЕТА>>>"))
    issues_block = _fmt_issues(issues)
    if issues_block:
        parts.append(_join_blocks("<<<ЗАМЕЧАНИЯ КРИТИКА>>>", issues_block, "<<<КОНЕЦ ЗАМЕЧАНИЙ>>>"))
    if user_note:
        parts.append(_join_blocks("<<<ЗАМЕЧАНИЕ МАКСИМА>>>", str(user_note).strip(), "<<<КОНЕЦ ЗАМЕЧАНИЯ>>>"))
    digest_text = digest.get("text") if isinstance(digest, dict) else _as_text(digest)
    if isinstance(digest_text, str) and digest_text.strip():
        version = digest.get("version") if isinstance(digest, dict) else 0
        head = ("<<<ПОСЛЕ ВЕРСИИ %s (это уже произошло — учти ВСЁ)>>>" % version if version
                else "<<<ПОСЛЕ ВЫПУСКА СВОДА (это уже произошло — учти ВСЁ)>>>")
        parts.append(head + digest_text.strip() + "\n<<<КОНЕЦ НОВОГО>>>")
    if not issues_block and not user_note and not (isinstance(digest_text, str) and digest_text.strip()):
        parts.append("Замечаний нет — перепроверь ответ и улучши слабые места.")
    parts.append("Это доработка того же запроса: выдай НОВЫЙ цельный ответ (по-русски, один связный "
                 "текст), а не пересказ прошлого и не список правок.")
    parts.append("Обязательно перечисли в metadata.changes (3–7 коротких пунктов, по-русски) ЧТО "
                 "ИЗМЕНИЛОСЬ в своде по сравнению с предыдущей версией — Михаил видит этот список "
                 "под сводом; и укажи metadata.version_reason — одной строкой, почему сделана версия "
                 "(например «по замечанию Михаила про сроки» или «учтены ответы на вопросы»).")
    parts.append(build_contract_block(room, slug, cfg))
    return _join_blocks(*parts)


def body_fingerprint(body: str) -> str:
    """Короткий отпечаток тела — для идемпотентного ключа карточек критика/доработки."""
    return hashlib.sha1((body or "").encode("utf-8")).hexdigest()[:10]
