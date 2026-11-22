"""«Второе мнение» на другой модели и версии свода (W29B-Q4).

Зачем
-----
Свод пишет руководитель комнаты на своей модели. Человеку нужен независимый
взгляд: та же работа, но ДРУГОЙ моделью, и ответ в виде короткого списка
расхождений — «согласна / не согласна / не хватает» — со ссылками на фрагменты
свода. Второй взгляд не запускается сам: только по кнопке и только после
показа оценки расхода.

Что здесь
----------
* ``choices()``      — что можно выбрать: модели каталога Hermes (тот же, что в
  диалоге модели участника) и модель, которая писала свод.
* ``default_choice()`` — модель по умолчанию: ПЕРВАЯ из каталога, которая НЕ та,
  что писала свод (правило пакета), иначе — первая доступная.
* ``estimate()``     — оценка расхода до запуска: токены входа (база запроса из
  ``usage.base_estimate`` + текст свода) и выхода, без выдуманных цен.
* ``build_body()``   — тело карточки-проверки: запрос, свод версии N и строгий
  формат ответа, чтобы разбор не гадал.
* ``create_check()`` — карточка-проверка СРАЗУ с выбранной моделью: та же дорога,
  что у остальных карточек (``engine_write.create_child_card``), но модель пары
  берётся из выбора человека, а не из комнаты; выбор человека запоминается
  (``model_guard.set_user_model``), иначе сторож моделей вернул бы модель комнаты.
* ``parse_check()``  — разбор готового ответа в пункты ``agree|disagree|missing``
  с цитатой и пояснением. Ответ не по формату не выбрасывается: он остаётся
  текстом (``source="raw"``), человек читает как есть.
* ``checks()``       — что уже спрашивали по этому запросу: карточки с названием
  ``Второе мнение`` из дерева запроса, их модель, состояние и разобранный ответ.

Модель НЕ запускается и НЕ меняется ни в одном профиле Hermes: выбор живёт в
состоянии комнаты (``model_guard.set_user_model``) и в самой карточке.
"""
from __future__ import annotations

import re as _re
import time as _time

try:  # пакетный и «плоский» импорт
    from .http_util import ApiError
except ImportError:  # pragma: no cover
    from http_util import ApiError            # type: ignore

#: Название карточки-проверки. По нему же её находят в дереве запроса.
TITLE_PREFIX = "Второе мнение"
#: Вид карточки в движке (не «revision»: это не доработка свода).
KIND = "second_opinion"
#: Вердикты пунктов — один словарь на сервер и интерфейс.
VERDICTS = ("agree", "disagree", "missing")
VERDICT_RU = {"agree": "согласна", "disagree": "не согласна", "missing": "не хватает"}
#: Ожидаемый ответ: до 800 токенов выхода — проверка короткая по условию.
OUTPUT_TOKENS = 700
#: Знаков на токен для русского текста (замер по сводам комнаты).
CHARS_PER_TOKEN = 3.0

#: Подменяемые модули (тесты кладут сюда подделки; в работе — настоящие).
_ENGINE = None
_MODELS = None
_GUARD = None
_USAGE = None


class SecondOpinionError(Exception):
    """Ошибка «второго мнения» с кодом для HTTP-слоя."""

    def __init__(self, code: str, message: str, detail: dict | None = None) -> None:
        super().__init__(message)
        self.code = str(code or "bad_request")
        self.message = message
        self.detail = dict(detail or {})


# ---------------------------------------------------------------- модули ---
def _engine():
    global _ENGINE
    if _ENGINE is not None:
        return _ENGINE
    try:
        from . import engine_write as mod
    except ImportError:  # pragma: no cover
        import engine_write as mod              # type: ignore
    _ENGINE = mod
    return mod


def _models():
    global _MODELS
    if _MODELS is not None:
        return _MODELS
    try:
        from . import agent_models as mod
    except ImportError:  # pragma: no cover
        import agent_models as mod             # type: ignore
    _MODELS = mod
    return mod


def _guard():
    global _GUARD
    if _GUARD is not None:
        return _GUARD
    try:
        from . import model_guard as mod
    except ImportError:  # pragma: no cover
        import model_guard as mod              # type: ignore
    _GUARD = mod
    return mod


def _usage():
    global _USAGE
    if _USAGE is not None:
        return _USAGE
    try:
        from . import usage as mod
    except ImportError:  # pragma: no cover
        import usage as mod                    # type: ignore
    _USAGE = mod
    return mod


def reset_modules() -> None:
    """Сбросить подмены модулей (тесты)."""
    global _ENGINE, _MODELS, _GUARD, _USAGE
    _ENGINE = _MODELS = _GUARD = _USAGE = None


# ---------------------------------------------------------------- помощники ---
def _text(value) -> str:
    return str(value or "").strip()


def _entry(detail) -> dict:
    """Запись истории свода, которую сейчас видит человек (последняя)."""
    syn = (detail or {}).get("synthesis") if isinstance(detail, dict) else None
    syn = syn if isinstance(syn, dict) else {}
    history = [v for v in (syn.get("history") or []) if isinstance(v, dict)]
    if history:
        return sorted(history, key=lambda v: int(v.get("version") or 0))[-1]
    return {}


def svod_of(detail) -> dict:
    """Текст и номер показанной версии свода: ``{version, text, author, task_id}``."""
    syn = (detail or {}).get("synthesis") if isinstance(detail, dict) else None
    syn = syn if isinstance(syn, dict) else {}
    entry = _entry(detail)
    text = _text(syn.get("text_md"))
    version = int(syn.get("version") or entry.get("version") or 0)
    if entry and entry.get("text_md") and int(entry.get("version") or 0) == version:
        text = _text(entry.get("text_md")) or text
    return {"version": version, "text": text,
            "author": _text(entry.get("author") or syn.get("author")),
            "task_id": _text(entry.get("task_id") or syn.get("source_task"))}


def _author_profile(room, detail) -> str:
    """Кто писал свод: автор версии, иначе руководитель комнаты."""
    svod = svod_of(detail)
    return svod["author"] or _text((room or {}).get("orchestrator"))


def _model_pairs(data) -> list[dict]:
    """Плоский список моделей каталога: провайдер, модель, подписи, значение."""
    out: list[dict] = []
    providers = (data or {}).get("providers") if isinstance(data, dict) else None
    for item in providers or []:
        if not isinstance(item, dict):
            continue
        pid = _text(item.get("id"))
        plabel = _text(item.get("label")) or pid
        if not pid:
            continue
        for model in item.get("models") or []:
            mid = _text((model or {}).get("id") if isinstance(model, dict) else model)
            if not mid:
                continue
            label = _text((model or {}).get("label")) or mid
            out.append({"provider": pid, "provider_label": plabel, "model": mid,
                        "label": label, "value": "%s|%s" % (pid, mid)})
    return out


def value_of(provider, model) -> str:
    """Значение выбора для интерфейса: ``провайдер|модель``."""
    return "%s|%s" % (_text(provider), _text(model))


def split_value(value) -> tuple[str, str]:
    """Обратно из ``провайдер|модель``; одно слово — это модель без провайдера."""
    raw = _text(value)
    if "|" in raw:
        provider, model = raw.split("|", 1)
        return provider.strip(), model.strip()
    return "", raw


def author_model(cfg, room, detail) -> dict:
    """Модель, которая писала свод: выбор комнаты для автора (``agent_models``)."""
    profile = _author_profile(room, detail)
    out = {"profile": profile, "provider": "", "model": "", "label": "", "source": ""}
    if not profile:
        return out
    try:
        value = _models().effective(cfg, room, profile)
    except Exception:  # noqa: BLE001 - модель автора не должна ронять экран
        return out
    out.update(provider=_text(value.get("provider")), model=_text(value.get("model")),
               source=_text(value.get("source")))
    if out["model"]:
        out["label"] = "%s · %s" % (out["provider"], out["model"]) if out["provider"] else out["model"]
    return out


def choices(cfg, room, detail=None) -> dict:
    """Модели для выбора и модель автора свода.

    ``{"items": [...], "author": {...}, "warning": ""}``. Каталог — тот же, что в
    диалоге модели участника (``agent_models.catalog``), никакой своей таблицы.
    """
    out = {"items": [], "author": {}, "warning": ""}
    try:
        data = _models().catalog(cfg)
    except Exception as exc:  # noqa: BLE001 - без каталога выбор не предлагаем
        data = {}
        out["warning"] = "Список моделей недоступен: %s" % str(exc)[:160]
    out["items"] = _model_pairs(data)
    if not out["items"]:
        out["warning"] = out["warning"] or "Каталог моделей пуст — выбрать нечего."
    warnings = (data or {}).get("warnings") if isinstance(data, dict) else None
    if not out["warning"] and warnings:
        out["warning"] = _text((warnings or [""])[0])[:200]
    out["author"] = author_model(cfg, room, detail or {})
    return out


def default_choice(items: list[dict], author: dict) -> dict:
    """Модель по умолчанию: не та, что писала свод (первая подходящая)."""
    used = (_text((author or {}).get("provider")), _text((author or {}).get("model")))
    for item in items or []:
        if not used[1] or _text(item.get("model")).lower() != used[1].lower():
            return dict(item)
    return dict((items or [{}])[0]) if items else {}


def estimate(cfg, detail, svod_text: str = "") -> dict:
    """Оценка расхода до запуска: токены, а не выдуманные доллары.

    Вход = база запроса (медиана из ``state.db``, ``usage.base_estimate``) +
    текст свода и задания. Выход — короткий список расхождений (``OUTPUT_TOKENS``).
    """
    text = _text(svod_text) or svod_of(detail).get("text") or ""
    base = {}
    try:
        state_dir = getattr(cfg, "state_dir", None)
        base = _usage().base_estimate(state_dir)
    except Exception:  # noqa: BLE001 - оценка без базы остаётся грубой, но честной
        base = {}
    base_tokens = int(base.get("base_tokens") or 0)
    input_tokens = base_tokens + int(len(text) / CHARS_PER_TOKEN) + 900
    return {"input_tokens": int(input_tokens), "output_tokens": int(OUTPUT_TOKENS),
            "chars": len(text), "base_tokens": base_tokens,
            "source": _text(base.get("source")) or "оценка",
            "text": "≈ %s тыс. токенов на входе и %s тыс. на выходе — одна проверка."
                    % (_thousands(input_tokens), _thousands(OUTPUT_TOKENS))}


def _thousands(value) -> str:
    num = int(value or 0)
    whole, rest = divmod(num, 1000)
    if not whole:
        return str(num)
    return ("%d %03d" % (whole, rest)) if rest else str(whole)


# ---------------------------------------------------------------- тело карточки ---
def build_body(room, request_text: str, svod_text: str, version: int = 0) -> str:
    """Тело карточки-проверки: что проверить и в каком виде ответить.

    Свод и запрос вкладываются целиком: проверяющий не обязан читать дерево,
    поэтому проверка дешевле обычного запуска и не зависит от его истории.
    """
    lead = _text((room or {}).get("orchestrator"))
    head = "Ты — независимый проверяющий. Тебя не было в работе над этим сводом."
    if lead:
        head += " Свод писал другой участник комнаты («%s»)." % lead
    parts = [
        head,
        "",
        "Проверь свод версии %d по тексту запроса и скажи, где он расходится с фактами,"
        " логикой или запросом, и чего в нём не хватает." % int(version or 0),
        "",
        "## Запрос",
        _text(request_text)[:8000],
        "",
        "## Свод версии %d" % int(version or 0),
        _text(svod_text)[:24000],
        "",
        "## Как ответить",
        "Только пункты, по одному в строке, без вступлений и без итогового резюме."
        " Если расхождений нет — одна строка «СОГЛАСНА: свод верен».",
        "",
        "- `СОГЛАСНА: «точная цитата из свода» — чем подтверждается`",
        "- `НЕ СОГЛАСНА: «точная цитата из свода» — что неверно и как правильно`",
        "- `НЕ ХВАТАЕТ: «чего именно нет» — почему без этого свод неполон`",
        "",
        "Цитата — дословно из свода, коротко (до 120 знаков). Не больше 8 пунктов.",
        "Ничего не меняй в своде и не пиши новый — только расхождения.",
    ]
    return "\n".join(parts)


# ---------------------------------------------------------------- разбор ответа ---
#: Маркеры вердикта в начале строки (русские и латинские, с «не» впереди).
_MARKERS = (
    ("не согласна", "disagree"), ("не согласен", "disagree"), ("not agree", "disagree"),
    ("disagree", "disagree"), ("не хватает", "missing"), ("missing", "missing"),
    ("согласна", "agree"), ("согласен", "agree"), ("agree", "agree"),
)
_QUOTE_RE = _re.compile(r"[«\"“]([^»\"”]{1,300})[»\"”]")
_BULLET_RE = _re.compile(r"^\s*(?:[-*+•]|\d+[.)])\s*")


def _split_line(line: str) -> tuple[str, str]:
    """Вердикт и остаток строки по маркеру в начале (после списка и `**`)."""
    text = _BULLET_RE.sub("", str(line or "")).strip().strip("*").strip()
    low = text.lower()
    for marker, verdict in _MARKERS:
        if low.startswith(marker):
            rest = text[len(marker):].lstrip(" :—-–—\t")
            return verdict, rest.strip().strip("*").strip()
    return "", text


def parse_check(text: str) -> dict:
    """Разобрать ответ проверяющего в ``{items, counts, summary, source}``.

    Строка без маркера не теряется: она становится пунктом ``note`` без вердикта
    (``kind="note"``), чтобы человек видел всё, что написали, а не только шаблон.
    """
    out = {"items": [], "counts": {key: 0 for key in VERDICTS}, "summary": "",
           "source": "card" if _text(text) else "empty"}
    for raw in _text(text).splitlines():
        line = raw.strip()
        if not line:
            continue
        verdict, rest = _split_line(line)
        if not verdict:
            continue
        quoted = _QUOTE_RE.search(rest)
        quote = quoted.group(1).strip() if quoted else ""
        note = rest
        if quote:
            note = rest.replace(quoted.group(0), " ").strip(" —-–\t")
        note = _re.sub(r"\s+", " ", note).strip()
        if not quote and not note:
            continue
        out["items"].append({"verdict": verdict, "label": VERDICT_RU[verdict],
                             "quote": quote[:300], "note": note[:600]})
        out["counts"][verdict] += 1
        if len(out["items"]) >= 24:
            break
    if not out["items"] and _text(text):
        out["source"] = "raw"     # ответ не по формату — покажем текстом, не потеряем
    return out


def counts_text(counts) -> str:
    """«2 не согласна · 1 не хватает» — одна строка для карточки и панели."""
    c = counts if isinstance(counts, dict) else {}
    parts = []
    for key in ("disagree", "missing", "agree"):
        num = int(c.get(key) or 0)
        if num:
            parts.append("%d %s" % (num, VERDICT_RU[key]))
    return " · ".join(parts)


# ---------------------------------------------------------------- карточки ---
def is_check_node(node) -> bool:
    """Карточка-проверка по названию (движок сохраняет наше название как есть)."""
    return _text((node or {}).get("title")).startswith(TITLE_PREFIX)


def checks(detail) -> list[dict]:
    """Уже запущенные проверки по этому запросу — из дерева, без второго источника."""
    tree = (detail or {}).get("tree") if isinstance(detail, dict) else None
    out: list[dict] = []
    for node in tree or []:
        if not isinstance(node, dict) or not is_check_node(node):
            continue
        summary = _text(node.get("summary_md"))
        status = _text(node.get("status"))
        parsed = parse_check(summary) if summary else {"items": [], "counts": {k: 0 for k in VERDICTS},
                                                       "source": "empty", "summary": ""}
        out.append({
            "card_id": _text(node.get("id")),
            "title": _text(node.get("title")),
            "at": int(node.get("created_at") or 0),
            "finished_at": int(node.get("finished_at") or 0),
            "status": status,
            "running": status in ("todo", "ready", "running", "review", "blocked"),
            "failed": status in ("failed", "cancelled") or bool(node.get("error")),
            "assignee": _text(node.get("assignee")),
            "model": _text(node.get("model_label")),
            "items": parsed["items"],
            "counts": parsed["counts"],
            "counts_text": counts_text(parsed["counts"]),
            "text": summary,
            "source": parsed["source"],
            "error": _text(node.get("error")),
        })
    out.sort(key=lambda c: (c["at"], c["card_id"]))
    return out


def view(cfg, room, detail) -> dict:
    """Всё для панели «Второе мнение»: выбор, оценка, что уже спрашивали."""
    room = room if isinstance(room, dict) else {}
    svod = svod_of(detail)
    picked = choices(cfg, room, detail)
    author = author_model(cfg, room, detail)
    default = default_choice(picked["items"], author)
    found = checks(detail)
    running = next((c for c in found if c["running"]), None)
    can_run = bool(svod["text"]) and bool(picked["items"]) and not running
    why_not = ""
    if not svod["text"]:
        why_not = "Свода ещё нет — проверять нечего."
    elif not picked["items"]:
        why_not = "Список моделей пуст: выбрать модель для проверки нельзя."
    elif running:
        why_not = "Проверка уже идёт — дождитесь её ответа."
    return {"ok": True, "version": svod["version"], "author": author,
            "models": picked["items"], "warning": picked["warning"],
            "default": default, "estimate": estimate(cfg, detail, svod["text"]),
            "checks": found, "can_run": can_run, "why_not": why_not,
            "running": bool(running)}


def create_check(cfg, room, rec, detail, provider: str = "", model: str = "",
                 assignee: str = "") -> dict:
    """Создать карточку-проверку на выбранной модели. Возвращает ``{card_id, ...}``."""
    room = room if isinstance(room, dict) else {}
    rec = rec if isinstance(rec, dict) else {}
    detail = detail if isinstance(detail, dict) else {}
    if detail.get("archived") or room.get("archived"):
        raise SecondOpinionError("conflict", "Запрос в архиве — второе мнение не запускаем.",
                                 {"reason": "archived"})
    svod = svod_of(detail)
    if not svod["text"]:
        raise SecondOpinionError("conflict", "Свода ещё нет — проверять нечего.",
                                 {"reason": "no_svod"})
    provider, model = _text(provider), _text(model)
    if not model:
        raise SecondOpinionError("bad_request", "Выберите модель для проверки.", {"field": "model"})
    items = _model_pairs(_catalog_safe(cfg))
    known = [i for i in items if i["model"].lower() == model.lower()
             and (not provider or i["provider"].lower() == provider.lower())]
    if items and not known:
        raise SecondOpinionError("bad_request",
                                 "Модели «%s» нет в каталоге — выберите из списка." % model[:60],
                                 {"field": "model", "value": model[:60]})
    if known and not provider:
        provider = known[0]["provider"]
    running = next((c for c in checks(detail) if c["running"]), None)
    if running:
        raise SecondOpinionError("conflict", "Проверка уже идёт — дождитесь её ответа.",
                                 {"reason": "running", "card_id": running["card_id"]})
    author = author_model(cfg, room, detail)
    if author.get("model") and author["model"].lower() == model.lower() \
            and (not author.get("provider") or not provider
                 or author["provider"].lower() == provider.lower()):
        # Не запрет, а честное предупреждение: человек мог выбрать ту же модель сам.
        same_model = True
    else:
        same_model = False

    who = _text(assignee) or _text(room.get("critic")) or _text(room.get("orchestrator"))
    if not who:
        raise SecondOpinionError("conflict", "В комнате не выбран ни руководитель, ни критик — "
                                             "проверку некому выполнить.", {"reason": "no_member"})
    board = _text(room.get("board"))
    root_id = _text(rec.get("root_id") or detail.get("id") or "")
    if not board or not root_id:
        raise SecondOpinionError("bad_request", "Не хватает доски или запроса для проверки.",
                                 {"reason": "no_board"})

    version = int(svod["version"] or 0)
    label = next((i["label"] for i in known), model)
    title = "%s · %s" % (TITLE_PREFIX, label)
    body = build_body(room, _text(rec.get("text") or detail.get("text")), svod["text"], version)

    # Модель пары — выбор человека: та же дорога создания карточки, что у всех,
    # но настройка комнаты для исполнителя подменена на выбранную модель. Так
    # карточка рождается СРАЗУ с моделью (окна «сначала модель комнаты» нет).
    settings = dict((room.get("agent_models") or {})) if isinstance(room.get("agent_models"), dict) else {}
    settings[who] = {"provider": provider, "model": model}
    card_room = dict(room)
    card_room["agent_models"] = settings
    key = "so-%s-v%s-%s-%s" % (root_id, version, _slug(provider), _slug(model))
    try:
        card_id = _engine().create_child_card(cfg, card_room, board, root_id, who, title, body,
                                              KIND, idem_key=key)
    except SecondOpinionError:
        raise
    except Exception as exc:  # noqa: BLE001 - движок: понятная ошибка, не 500
        raise SecondOpinionError("unavailable", "Не удалось поставить проверку: %s" % str(exc)[:240],
                                 {"reason": "engine", "root_id": root_id}) from exc
    card_id = _text(card_id)
    # Выбор человека по карточке — чтобы сторож моделей не вернул модель комнаты.
    try:
        _guard().set_user_model(cfg, room.get("id"), card_id, provider=provider, model=model)
    except Exception:  # noqa: BLE001 - карточка уже с моделью; запись выбора вторичнa
        pass
    return {"ok": True, "action": "second_opinion", "card_id": card_id, "title": title,
            "provider": provider, "model": model, "label": label, "assignee": who,
            "version": version, "same_model": same_model,
            "estimate": estimate(cfg, detail, svod["text"]),
            "message": "Проверка поставлена: «%s» на модели %s." % (who, label)}


def _slug(value) -> str:
    return _re.sub(r"[^a-z0-9]+", "-", _text(value).lower())[:32] or "m"


def _catalog_safe(cfg) -> dict:
    try:
        return _models().catalog(cfg) or {}
    except Exception:  # noqa: BLE001 - каталог не должен мешать проверке
        return {}


# ---------------------------------------------------------------- HTTP ---
class SecondOpinionRoutes:
    """Обработчики «второго мнения» (``self`` — экземпляр ``server.app.App``)."""

    def h_second_opinion(self, query, body, rid):
        """``GET /api/requests/{id}/second-opinion`` — выбор модели, оценка, ответы."""
        room, rec = self._find_record(rid)
        detail = self._detail(room, rid, rec.get("extra_cards"), rec.get("critic"),
                              rec.get("answers"))
        return view(self.cfg, room, detail)

    def h_second_opinion_create(self, room, rec, rid, body):
        """Действие ``second_opinion``: ``{provider?, model, assignee?}``."""
        payload = body if isinstance(body, dict) else {}
        model = payload.get("model")
        provider = payload.get("provider")
        for name, value in (("model", model), ("provider", provider)):
            if value is not None and not isinstance(value, str):
                raise ApiError(400, "Поле %s должно быть строкой" % name, code="bad_request",
                               detail={"field": name})
        if not _text(model):
            raise ApiError(400, "Выберите модель для проверки", code="bad_request",
                           detail={"field": "model"})
        detail = self._detail(room, rid, rec.get("extra_cards"), rec.get("critic"),
                              rec.get("answers"))
        try:
            return create_check(self.cfg, room, rec, detail,
                                provider=_text(provider), model=_text(model),
                                assignee=_text(payload.get("assignee")))
        except SecondOpinionError as exc:
            status = {"bad_request": 400, "conflict": 409, "unavailable": 503}.get(exc.code, 400)
            raise ApiError(status, exc.message,
                           code=None if exc.code == "bad_request" else exc.code,
                           detail=dict(exc.detail or {})) from exc


ROUTES = [
    ("GET", r"/api/requests/(?P<rid>t_[A-Za-z0-9_]+)/second-opinion", "h_second_opinion"),
]


def _route_response(func):
    """Обработчик возвращает данные — оборачиваем в (200, тело, заголовки)."""
    import functools

    @functools.wraps(func)
    def inner(self, *args, **kwargs):
        result = func(self, *args, **kwargs)
        if isinstance(result, tuple) and len(result) == 3:
            return result
        return 200, result, {"Cache-Control": "no-store"}
    return inner


def install_second_opinion_routes(app_cls, routes: list) -> None:
    """Подключить «второе мнение» к ``App``: методы миксина + строка маршрутов."""
    for name, value in vars(SecondOpinionRoutes).items():
        if name.startswith("__") or not callable(value):
            continue
        if name == "h_second_opinion":          # маршрут GET: диспетчер ждёт (код, тело, заголовки)
            value = _route_response(value)
        setattr(app_cls, name, value)
    existing = {(method, pattern.pattern) for method, pattern, _handler in routes}
    for method, pattern, handler in ROUTES:
        compiled = _re.compile(pattern)
        if (method, compiled.pattern) in existing:
            continue
        routes.append((method, compiled, handler))


__all__ = ["SecondOpinionRoutes", "SecondOpinionError", "ROUTES",
           "install_second_opinion_routes", "view", "create_check", "parse_check",
           "choices", "default_choice", "estimate", "build_body", "checks",
           "svod_of", "author_model", "counts_text", "VERDICTS", "VERDICT_RU",
           "TITLE_PREFIX", "OUTPUT_TOKENS"]
