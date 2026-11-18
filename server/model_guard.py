"""Контроль моделей: запрос уходит ТОЛЬКО на ту модель, которая ожидается (WAVE23 §2).

Причина пакета (проверено на живых данных 17.11): карточка `t_375d5a5f` комнаты `fn`
(исполнитель `ornith`, локальная Qwen) выполнилась на `anthropic/claude-sonnet-5-5` за
$2,45. Приложение при создании корневой карточки писало `model_override` модели
**руководителя** (`status='triage'`, `assignee=null`), а когда Hermes назначал
исполнителя, переопределение оставалось. Сверки «модель карточки ↔ ожидаемая модель
исполнителя» не было нигде — только подпись «проверьте».

Что делает модуль:

* ``expected_model`` — **единственный** источник правды, какая модель ожидается у
  участника: выбор человека для карточки → переопределение комнаты → `config.yaml`
  профиля Hermes → «неизвестно»;
* ``override_for_card`` — что вообще можно писать в карточку при создании (только выбор
  комнаты; модель профиля не «пинится»);
* ``tick`` — быстрый сторож (свой цикл ≤ 3 с, `server/watcher.py`): до запуска исправляет
  переопределение штатной функцией движка, а запущенное на чужой модели **останавливает
  и перезапускает** на верной; «неизвестно» — только предупреждение;
* ``model_check`` — `GET /api/rooms/{id}/model-check`: что ожидается, что записано в
  карточке, на чём был последний запуск, сколько исправлений/остановок за сутки;
* ``card_stop`` / ``card_resume`` / ``card_set_model`` — те же три действия, что кнопки
  остановки в Hermes, для одной карточки (остановить без потери работы, продолжить,
  сменить модель и продолжить);
* ``attach`` — лента `model_guard[]` в деталях запроса; ``diag_item`` — пункт «Проверки
  системы».

Правила безопасности: реальные профили и `state.db` — только чтение (`sqlite mode=ro`);
в доску пишем только CLI `hermes kanban` и штатным мостом `tools/kb_bridge.py`; чужие
tenant и доски не трогаем; при неопределённой модели работу не убиваем.

    from server.model_guard import tick, expected_model, model_check
"""
from __future__ import annotations

import sqlite3
import time
from pathlib import Path

try:                                        # import server.model_guard
    from . import agent_models as models
    from . import statefiles as sf
except ImportError:                          # python server/app.py (плоский импорт)
    import agent_models as models            # type: ignore
    import statefiles as sf                  # type: ignore

#: режимы контроля: `enforce` — исправляем сами (по умолчанию), `warn` — только предупреждаем
MODES = ("enforce", "warn")
DEFAULT_MODE = "enforce"
MODE_LABELS = {"enforce": "Строго: неверную модель исправляю сам",
               "warn": "Только предупреждать — решаю я"}
MODE_HINTS = {
    "enforce": "Расхождение исправляется до запуска; запущенное на чужой модели "
               "останавливается и перезапускается",
    "warn": "Приложение только показывает расхождение — останавливать и перезапускать "
            "ничего не будет",
}
#: свой быстрый цикл сторожа (секунды) — отдельно от общего тика наблюдателя
GUARD_TICK = 3.0
#: карточку моложе этого возраста не останавливаем: воркер мог ещё не записать сессию
GUARD_MIN_AGE = 15
#: сколько раз одна карточка перезапускается автоматически (защита от петли)
GUARD_MAX_RESTARTS = 2
#: как часто повторять одно и то же предупреждение по одной карточке (журнал не копим)
WARN_TTL = 1800
#: статусы «ещё не запущена» — здесь правится переопределение
PENDING_STATUSES = ("triage", "todo", "ready", "blocked", "queued", "scheduled")
#: статусы «пошла» — здесь сверяется факт из сессии воркера
RUNNING_STATUSES = ("running",)
#: карточки этих статусов не трогаем никогда
LOCKED_STATUSES = ("review", "done", "archived")
#: кольцо событий в состоянии комнаты (журнал комнаты — полный, это только для ленты)
MAX_EVENTS = 200
#: как часто повторять предупреждение «модель неизвестна» по одной карточке
UNKNOWN_TTL = 86400
TENANT_PREFIX = "room:"
#: префикс служебных сообщений приложения в ветке запроса
NOTE_PREFIX = "[Контроль моделей]"
#: слова, которыми человек просит продолжить (сообщение возобновляет остановленную карточку)
RESUME_WORDS = ("продолжай", "продолжи", "продолжайте", "продолжаем", "продолжить",
                "дальше", "поехали", "возобнови", "возобновить", "go on", "continue")
#: маркер вопроса «От вас нужно» (тот же, что читает `server/answers.py`)
LEAD_QUESTION_MARK = "[Руководитель · вопрос]"

#: алиасы моделей: только реальные пары «одно и то же имя по-разному».
#: Похожие имена (qwen36 / qwen36v, glm-5.3 / glm-5.3-flash) — РАЗНЫЕ модели.
ALIASES = {
    "claude-5-5-sonnet": "claude-sonnet-5-5",
    "claude-sonnet-5.5": "claude-sonnet-5-5",
    "claude-sonnet-5": "claude-sonnet-5-5",
}
#: алиасы провайдеров (только написание одного и того же слага)
PROVIDER_ALIASES = {"llama.cpp": "llamacpp", "llama-cpp": "llamacpp", "llama_cpp": "llamacpp"}


# ---------------------------------------------------------------- нормализация

def canon_model(value) -> str:
    """Имя модели для сравнения: регистр, пробелы, `:latest` и известные алиасы.

    Ничего не «угадываем»: `qwen36v` не превращается в `qwen36`, дата-суффикс
    (`deepseek-v4-pro:0813`) не отбрасывается — это разные модели у провайдера.
    """
    text = " ".join(str(value or "").strip().lower().split())
    if text.endswith(":latest"):
        text = text[: -len(":latest")].strip()
    return ALIASES.get(text, text)


def canon_provider(value) -> str:
    text = str(value or "").strip().lower()
    return PROVIDER_ALIASES.get(text, text)


def _qualified(value) -> tuple[str, str]:
    """`провайдер/модель` → (провайдер, модель); иначе пусто (двоеточие не режем)."""
    text = str(value or "").strip()
    if "/" not in text:
        return "", ""
    left, right = text.split("/", 1)
    if not left.strip() or not right.strip():
        return "", ""
    return canon_provider(left), canon_model(right)


def same_model(expected, actual) -> bool:
    """Одна и та же модель? Пустая модель — «неизвестно», а не «совпало»."""
    exp = expected if isinstance(expected, dict) else {}
    got = actual if isinstance(actual, dict) else {}
    exp_model = canon_model(exp.get("model"))
    got_model = canon_model(got.get("model"))
    if not exp_model or not got_model:
        return False
    q_provider, q_model = _qualified(got.get("model"))
    got_provider = canon_provider(got.get("provider"))
    if q_model:
        got_model = q_model
        got_provider = got_provider or q_provider
    if got_model != exp_model:
        return False
    exp_provider = canon_provider(exp.get("provider"))
    if exp_provider and got_provider and exp_provider != got_provider:
        return False              # та же модель у платного провайдера — это те же деньги
    return True


def short(provider, model, reasoning="") -> str:
    """«провайдер · модель · мышление» без лишних пустых частей."""
    parts = [p for p in (str(provider or ""), str(model or "")) if p]
    if reasoning:
        parts.append(models.EFFORT_SHORT.get(str(reasoning).lower(), str(reasoning)))
    return " · ".join(parts)


def _one_line(text) -> str:
    return " ".join(str(text or "").split())


# ---------------------------------------------------------------- режим комнаты

def normalize_mode(value) -> str:
    mode = str(value or "").strip().lower()
    return mode if mode in MODES else DEFAULT_MODE


def mode_of(room) -> str:
    """Режим контроля комнаты: `enforce` (по умолчанию) или `warn`."""
    room = room if isinstance(room, dict) else {}
    return normalize_mode(room.get("model_guard"))


def mode_block(room) -> dict:
    """Готовая строка настройки для карточки комнаты (W23.7)."""
    name = mode_of(room)
    return {"mode": name, "on": True, "label": MODE_LABELS[name], "hint": MODE_HINTS[name],
            "modes": [{"id": m, "label": MODE_LABELS[m]} for m in MODES]}


# ---------------------------------------------------------------- ожидаемая модель

def _scalar(text: str, section: str, key: str) -> str:
    """`section:` → `key: value` первого уровня вложенности (без PyYAML)."""
    inside = False
    level: int | None = None
    for raw in (text or "").splitlines():
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        indent = len(raw) - len(raw.lstrip(" "))
        line = raw.strip()
        if indent == 0:
            inside = line.split(":", 1)[0].strip() == section
            level = None
            continue
        if not inside or line.startswith("-"):
            continue
        if level is None:
            level = indent
        if indent != level:
            continue
        name = line.split(":", 1)[0]
        if name != key:
            continue
        rest = line[len(name):].lstrip()
        if rest[:1] in (":", "="):
            rest = rest[1:]
        value = rest.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        return value
    return ""


def _config_text(path: Path) -> str:
    try:
        return Path(path).read_text("utf-8", "replace")
    except OSError:
        return ""


def base_url_of(cfg, profile, *, shared: bool = False) -> str:
    """`model.base_url` профиля (для локального провайдера) — только чтение."""
    path = (Path(getattr(cfg, "hermes_home", "") or "") / "config.yaml") if shared \
        else models.config_path(cfg, profile)
    return _scalar(_config_text(path), "model", "base_url")


def expected_model(cfg, room, profile, card_id=None) -> dict:
    """Ожидаемая модель участника (W23.1): user → room → profile → none.

    `card_id` учитывает выбор человека для КОНКРЕТНОЙ карточки (`card_model`): он
    сильнее настроек комнаты, сторож такой выбор не «исправляет».
    """
    out = {"provider": "", "model": "", "reasoning": "", "source": "none",
           "base_url": "", "label": ""}
    name = str(profile or "").strip()
    room = room if isinstance(room, dict) else {}
    if not name:
        return out
    room_id = str(room.get("id") or "")
    if card_id and room_id:
        chosen = (user_models(cfg, room_id).get(str(card_id)) or {})
        if chosen.get("model"):
            out.update(provider=str(chosen.get("provider") or ""),
                       model=str(chosen.get("model") or ""), source="user")
            out["label"] = short(out["provider"], out["model"])
            return out
    settings = models.settings_of(room).get(name) or {}
    if settings.get("provider") and settings.get("model"):
        out.update(provider=settings["provider"], model=settings["model"],
                   reasoning=str(settings.get("reasoning") or ""), source="room")
        out["label"] = short(out["provider"], out["model"], out["reasoning"])
        return out
    info = models.default_info(cfg, name)
    if info.get("provider") and info.get("model"):
        out.update(provider=info["provider"], model=info["model"],
                   reasoning=str(info.get("reasoning") or ""), source="profile",
                   base_url=base_url_of(cfg, name, shared=(info.get("source") == "global")))
        out["label"] = short(out["provider"], out["model"], out["reasoning"])
    return out


def override_for_card(room, profile) -> dict:
    """Что можно записать в карточку при создании: ТОЛЬКО выбор комнаты.

    Модель профиля не переносим: она и так действует (движок читает `config.yaml`), а
    «пин» навсегда перекрыл бы будущую смену настроек агента. Именно перенос модели
    руководителя на корневую карточку и сжёг $2,45 у `ornith`.
    """
    out = {"provider": "", "model": "", "reasoning": ""}
    settings = models.settings_of(room).get(str(profile or "").strip()) or {}
    if settings.get("provider") and settings.get("model"):
        out.update(provider=settings["provider"], model=settings["model"],
                   reasoning=str(settings.get("reasoning") or ""))
    return out


def _want_for(cfg, room, profile, card_id=None) -> dict:
    """Желаемое состояние карточки: выбор человека для неё, иначе выбор комнаты."""
    room = room if isinstance(room, dict) else {}
    room_id = str(room.get("id") or "")
    if card_id and room_id:
        chosen = (user_models(cfg, room_id).get(str(card_id)) or {})
        if chosen.get("model"):
            return {"provider": str(chosen.get("provider") or ""),
                    "model": str(chosen.get("model") or ""), "reasoning": ""}
    return override_for_card(room, profile)


def needs_update(card: dict, want: dict) -> bool:
    """Расходится ли запись карточки с желаемой (по нормализации, не по буквам)."""
    card = card if isinstance(card, dict) else {}
    want = want if isinstance(want, dict) else {}
    have = {"provider": card.get("provider") or "", "model": card.get("model") or ""}
    if not want.get("model"):
        return bool(have["model"] or have["provider"])
    return not same_model(want, have)


def _apply_want(cfg, board: str, card: dict, want: dict):
    """Записать модель/мышление карточке штатным мостом (одно место записи)."""
    card_effort = str((card or {}).get("reasoning") or "")
    push = getattr(models, "push_model", None)
    if not callable(push):
        return [], [{"id": card.get("id"), "reason": "мост к Hermes недоступен"}]
    done, failed = push(cfg, board, [card], want)
    # уровень мышления: из выбора комнаты, иначе снять (модель профиля его не задаёт)
    if want.get("reasoning") != card_effort:
        models.push_effort(cfg, board, [card.get("id")], str(want.get("reasoning") or ""))
    return done, failed


# ---------------------------------------------------------------- состояние комнаты

def state_path(cfg, room_id) -> Path:
    return Path(cfg.state_dir) / str(room_id) / "model_guard.json"


def _blank_state() -> dict:
    return {"updated_at": 0, "restarts": {}, "user_model": {}, "stopped": {},
            "unknown": {}, "warned": {}, "events": []}


def read_state(cfg, room_id) -> dict:
    """Состояние сторожа комнаты (мусор и старые файлы не роняют чтение)."""
    try:
        raw = sf.read_json(state_path(cfg, room_id), {})
    except Exception:                        # noqa: BLE001 - файл вторичен
        raw = {}
    data = _blank_state()
    if not isinstance(raw, dict):
        return data
    for key in ("restarts", "user_model", "stopped", "unknown", "warned"):
        value = raw.get(key)
        if isinstance(value, dict):
            data[key] = {str(k): v for k, v in value.items() if isinstance(v, dict)}
    events = raw.get("events")
    data["events"] = [e for e in (events or []) if isinstance(e, dict)][-MAX_EVENTS:]
    try:
        data["updated_at"] = int(raw.get("updated_at") or 0)
    except (TypeError, ValueError):
        data["updated_at"] = 0
    return data


def _mutate(cfg, room_id, change) -> dict:
    """Прочитать → изменить → записать под замком (одно место записи состояния)."""
    path = state_path(cfg, room_id)
    with sf.lock_for(path):
        data = read_state(cfg, room_id)
        change(data)
        data["updated_at"] = int(time.time())
        data["events"] = [e for e in data.get("events") or [] if isinstance(e, dict)][-MAX_EVENTS:]
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            sf.write_json_atomic(path, data)
        except Exception:                    # noqa: BLE001 - состояние вторично
            pass
    return data


def user_models(cfg, room_id) -> dict:
    """Выбор модели человеком для конкретных карточек: `{task_id: {provider, model, at}}`."""
    return dict(read_state(cfg, room_id).get("user_model") or {})


def set_user_model(cfg, room_id, card_id, provider="", model="") -> dict:
    """Запомнить (или снять) выбор человека для карточки."""
    card_id = str(card_id or "")
    entry = {"provider": str(provider or ""), "model": str(model or ""),
             "at": int(time.time())} if model else None

    def change(data):
        if entry is None:
            data["user_model"].pop(card_id, None)
        else:
            data["user_model"][card_id] = entry

    _mutate(cfg, room_id, change)
    return entry or {}


def stopped_by(cfg, room_id, card_id) -> str:
    """Кто остановил карточку: `user` | `guard` | `""`."""
    item = (read_state(cfg, room_id).get("stopped") or {}).get(str(card_id)) or {}
    return str(item.get("by") or "")


def mark_stopped(cfg, room_id, card_id, *, by: str = "user", reason: str = "", model: str = "",
                 provider: str = "", cost_usd=None) -> dict:
    """Запомнить остановку карточки (W23.8 + W23-ST).

    `model`/`provider`/`cost_usd` — что было в работе на момент остановки: интерфейсу
    нужна строка «Остановлено вами в 10:41 · модель: …, сэкономлено ≈ $…», а взять её
    больше неоткуда — прогон к этому моменту уже снят (`reclaim`). Нет данных —
    `""`/`None`, без выдумывания.
    """
    item = {"by": str(by or "user"), "at": int(time.time()), "reason": _one_line(reason)[:200],
            "model": _one_line(model)[:120], "provider": _one_line(provider)[:60], "cost_usd": None}
    try:
        if cost_usd not in (None, ""):
            item["cost_usd"] = round(float(cost_usd), 4)
    except (TypeError, ValueError):
        item["cost_usd"] = None

    def change(data):
        data["stopped"][str(card_id)] = item

    _mutate(cfg, room_id, change)
    return item


def clear_stopped(cfg, room_id, card_id) -> bool:
    removed = {"value": False}

    def change(data):
        removed["value"] = data["stopped"].pop(str(card_id), None) is not None

    _mutate(cfg, room_id, change)
    return bool(removed["value"])


def stopped_by_user(cfg, room_id) -> dict:
    """`{task_id: {...}}` — карточки, остановленные человеком и ещё не продолженные."""
    items = read_state(cfg, room_id).get("stopped") or {}
    return {str(k): v for k, v in items.items() if str((v or {}).get("by") or "") == "user"}


def _restarts(cfg, room_id, card_id) -> int:
    try:
        return int((read_state(cfg, room_id).get("restarts") or {}).get(str(card_id), {}).get("count") or 0)
    except (TypeError, ValueError):
        return 0


def _clear_restarts(cfg, room_id, card_id) -> bool:
    """Сбросить бюджет перезапусков: карточка поехала на верной модели — проблема решена.

    Иначе два старых расхождения «съедали» весь лимит навсегда, и следующая (новая)
    проблема сразу парковала карточку в `blocked` без единой попытки.
    """
    removed = {"value": False}

    def change(data):
        removed["value"] = data["restarts"].pop(str(card_id), None) is not None

    _mutate(cfg, room_id, change)
    return bool(removed["value"])


def _bump_restarts(cfg, room_id, card_id, model="") -> int:
    count = {"value": 0}

    def change(data):
        item = data["restarts"].setdefault(str(card_id), {})
        item["count"] = int(item.get("count") or 0) + 1
        item["at"] = int(time.time())
        item["model"] = str(model or "")
        count["value"] = item["count"]

    _mutate(cfg, room_id, change)
    return count["value"]


def _journal(cfg, room_id, text: str, *, kind: str = "model_guard", actor: str = "app") -> None:
    """Строка в журнал комнаты (`journal.jsonl`) — как у `agent_models._journal`."""
    try:
        path = Path(cfg.state_dir) / str(room_id) / "journal.jsonl"
        with sf.lock_for(path):
            events = sf.read_jsonl(path)
            number = 0
            for event in events:
                ident = str((event or {}).get("id") or "")
                if ident.startswith("j_") and ident[2:].isdigit():
                    number = max(number, int(ident[2:]))
            sf.append_jsonl(path, {"id": "j_%04d" % (number + 1), "room": str(room_id),
                                   "at": int(time.time()), "kind": kind, "actor": actor,
                                   "text": _one_line(text)[:400]})
    except Exception:                        # noqa: BLE001 - журнал вторичен
        pass


def record(cfg, room_id, kind: str, *, card_id="", root_id="", profile="",
           expected=None, actual=None, cost_usd=None, text="", keep_journal: bool = False,
           actor: str = "app") -> dict:
    """Событие контроля: кольцо в состоянии комнаты + (по умолчанию) журнал комнаты."""
    event = {"at": int(time.time()), "kind": str(kind), "card_id": str(card_id or ""),
             "root_id": str(root_id or ""), "profile": str(profile or ""),
             "expected": {"provider": str((expected or {}).get("provider") or ""),
                          "model": str((expected or {}).get("model") or "")},
             "actual": {"provider": str((actual or {}).get("provider") or ""),
                        "model": str((actual or {}).get("model") or "")},
             "cost_usd": (None if cost_usd in (None, "") else round(float(cost_usd), 4)),
             "text": _one_line(text)}

    def change(data):
        data["events"] = (data.get("events") or []) + [event]

    _mutate(cfg, room_id, change)
    if not keep_journal and event["text"]:
        # `actor` — кто действовал: приложение (`app`) или человек (`mikhail`).
        _journal(cfg, room_id, event["text"], kind="model_guard_" + str(kind),
                 actor=str(actor or "app"))
    return event


def events(cfg, room_id, root_id=None, *, limit: int = 50) -> list:
    """События контроля комнаты (новые первыми); `root_id` — только по одному запросу.

    `root_id` пишется в событие в момент вмешательства (подъём по `task_links`), поэтому
    фильтр по нему дешёвый и не требует чтения доски на странице запроса.
    """
    items = [e for e in read_state(cfg, room_id).get("events") or [] if isinstance(e, dict)]
    if root_id:
        wanted = str(root_id)
        items = [e for e in items if str(e.get("root_id") or "") == wanted]
    items = sorted(items, key=lambda e: int(e.get("at") or 0), reverse=True)
    return items[:max(1, int(limit))]


def counters(cfg, room_id, *, now=None, window: int = 86400) -> dict:
    """Счётчики сторожа за сутки: исправления, остановки, оценка сэкономленного."""
    since = int(now or time.time()) - int(window)
    items = [e for e in read_state(cfg, room_id).get("events") or []
             if int(e.get("at") or 0) >= since]
    fixed = sum(1 for e in items if e.get("kind") == "fixed")
    stopped = sum(1 for e in items if e.get("kind") == "stopped")
    warned = sum(1 for e in items if e.get("kind") == "warned")
    saved = 0.0
    for event in items:
        if event.get("kind") in ("stopped", "stopped_by_user") and event.get("cost_usd"):
            saved += float(event["cost_usd"])
    return {"fixed_24h": fixed, "stopped_24h": stopped, "warned_24h": warned,
            "saved_usd_est": round(saved, 2)}


def counters_text(data: dict, mode: str) -> str:
    """Человеческая строка: «Строгий контроль: 3 исправления и 1 остановка за сутки…»."""
    fixed = int((data or {}).get("fixed_24h") or 0)
    stopped = int((data or {}).get("stopped_24h") or 0)
    saved = float((data or {}).get("saved_usd_est") or 0)
    lead = "Строгий контроль" if mode == "enforce" else "Только предупреждения"
    if not fixed and not stopped:
        return lead + ": расхождений за сутки не было"
    parts = []
    if fixed:
        parts.append("%d %s" % (fixed, models_ru(fixed, "исправление", "исправления", "исправлений")))
    if stopped:
        parts.append("%d %s" % (stopped, models_ru(stopped, "остановка", "остановки", "остановок")))
    text = lead + ": " + " и ".join(parts) + " за сутки"
    if saved > 0:
        text += ", сэкономлено ≈ $%s" % ("%.2f" % saved)
    return text


def models_ru(count: int, one: str, few: str, many: str) -> str:
    """Русская форма слова по числу (1 исправление, 2 исправления, 5 исправлений)."""
    count = abs(int(count))
    if count % 10 == 1 and count % 100 != 11:
        return one
    if 2 <= count % 10 <= 4 and count % 100 not in (12, 13, 14):
        return few
    return many


# ---------------------------------------------------------------- сессии и доска

def _connect(cfg, board: str):
    try:
        db = Path(cfg.kanban_db(board))
    except Exception:                        # noqa: BLE001 - доски нет
        return None
    if not db.exists():
        return None
    try:
        con = sqlite3.connect("file:%s?mode=ro" % db.as_posix(), uri=True, timeout=5)
    except sqlite3.Error:
        return None
    con.row_factory = sqlite3.Row
    return con


def _columns(con, table: str) -> set:
    try:
        return {str(row[1]) for row in con.execute("PRAGMA table_info(%s)" % table)}
    except sqlite3.Error:
        return set()


_SESSIONS: dict = {}


def session_info(cfg, profile, session_id) -> dict:
    """Факт запуска из `state.db` профиля: модель, провайдер, стоимость (`mode=ro`).

    `estimated_cost_usd` есть не во всех версиях Hermes — читаем только существующие
    колонки. Нет файла/сессии — пустой словарь («неизвестно», не выдумываем).
    """
    sid = str(session_id or "").strip()
    if not sid:
        return {}
    path = models.state_path(cfg, profile)
    key = (str(path), sid)
    now = time.time()
    hit = _SESSIONS.get(key)
    if hit and now - float(hit[0] or 0) < 120:
        return dict(hit[1])
    out: dict = {}
    try:
        if Path(path).exists():
            con = sqlite3.connect("file:%s?mode=ro" % Path(path).as_posix(), uri=True, timeout=5)
            try:
                con.row_factory = sqlite3.Row
                cols = _columns(con, "sessions")
                if cols:
                    wanted = [c for c in ("model", "billing_provider", "estimated_cost_usd")
                              if c in cols]
                    row = con.execute("SELECT %s FROM sessions WHERE id = ?"
                                      % ", ".join(wanted), (sid,)).fetchone()
                    if row is not None:
                        data = dict(row)
                        out = {"model": str(data.get("model") or "").strip(),
                               "provider": str(data.get("billing_provider") or "").strip()}
                        cost = data.get("estimated_cost_usd")
                        if cost not in (None, ""):
                            try:
                                out["cost_usd"] = round(float(cost), 4)
                            except (TypeError, ValueError):
                                pass
            finally:
                con.close()
    except Exception:                        # noqa: BLE001 - нет файла/схемы: «неизвестно»
        out = {}
    if len(_SESSIONS) > 500:
        _SESSIONS.clear()
    _SESSIONS[key] = (now, dict(out))
    return out


def same_name(expected, actual) -> bool:
    """Та же МОДЕЛЬ, если не смотреть на написание провайдера.

    Один и тот же локальный адрес Hermes подписывает по-разному: в реальных `sessions`
    профиля `ornith` модель `qwen36v` встречается и как `llamacpp`, и как `custom` (и как
    `openai` у OpenAI-совместимых серверов). Расхождение ярлыка провайдера — НЕ повод
    убивать нормальный запуск; это только предупреждение.
    """
    exp = expected if isinstance(expected, dict) else {}
    got = actual if isinstance(actual, dict) else {}
    exp_model = canon_model(exp.get("model"))
    got_model = canon_model(got.get("model"))
    return bool(exp_model) and exp_model == got_model


def _root_of(con, card_id, *, limit: int = 20) -> str:
    """Корень дерева запроса для карточки (подъём по `task_links.parent_id`)."""
    current = str(card_id or "")
    seen: set = set()
    for _ in range(max(1, int(limit))):
        if not current or current in seen:
            break
        seen.add(current)
        try:
            row = con.execute("SELECT parent_id FROM task_links WHERE child_id = ? "
                              "ORDER BY parent_id LIMIT 1", (current,)).fetchone()
        except sqlite3.Error:
            break
        parent = str((row["parent_id"] if row is not None else "") or "")
        if not parent:
            break
        current = parent
    return current


def _card_row(con, card_id) -> dict:
    try:
        row = con.execute("SELECT id, title, status, assignee, model_override, "
                          "provider_override, reasoning_effort FROM tasks WHERE id = ?",
                          (str(card_id),)).fetchone()
    except sqlite3.Error:
        return {}
    return dict(row) if row is not None else {}


def _ew():
    """Модуль записи в канбан (ленивый импорт — циклов между модулями нет)."""
    try:
        from . import engine_write as mod
    except ImportError:                      # pragma: no cover - плоский импорт
        import engine_write as mod           # type: ignore
    return mod


def _note(cfg, room, root_id, text) -> str:
    """Сообщение приложения в ветку запроса (автор — руководитель комнаты).

    Так же делает `agent_notes` и напоминание об оценке: служебный комментарий пишет
    приложение, но в ветке он от лица руководителя комнаты — иначе агенты его не увидят
    в своей истории. Текст начинается с `[Контроль моделей]`, чтобы человек видел автора.
    """
    lead = str((room or {}).get("orchestrator") or "").strip()
    target = str(root_id or "").strip()
    if not lead or not target:
        return ""
    try:
        _ew().act(cfg, str((room or {}).get("board") or ""), target, "comment",
                  text="%s %s" % (NOTE_PREFIX, _one_line(text)), task_id=target, author=lead)
    except Exception:                        # noqa: BLE001 - сообщение вторично
        return ""
    return text


# ---------------------------------------------------------------- быстрый сторож

def _actual_of(cfg, con, card: dict) -> dict:
    """Факт запуска карточки: `{provider, model, cost_usd, session_id, run_id, started_at}`.

    Берём ТОЛЬКО открытый прогон: закрытый (`ended_at`) — это прошлый запуск, и его
    сессия ничего не говорит о текущем. Иначе сторож мог остановить свежий запуск по
    устаревшей сессии предыдущей попытки.
    """
    try:
        run, sid = models._run_session(con, card.get("id"))       # noqa: SLF001 - общий код
    except Exception:                        # noqa: BLE001 - старая схема
        run, sid = {}, ""
    if isinstance(run, dict) and run.get("ended_at"):
        run, sid = {}, ""
    out = {"provider": "", "model": "", "cost_usd": None, "session_id": sid,
           "run_id": (run or {}).get("id"), "started_at": int((run or {}).get("started_at") or 0),
           "profile": str((run or {}).get("profile") or card.get("assignee") or "")}
    if not sid:
        return out
    got = session_info(cfg, out["profile"], sid)
    out["model"] = str(got.get("model") or "")
    out["provider"] = str(got.get("provider") or "")
    out["cost_usd"] = got.get("cost_usd")
    return out


def _warn_once(cfg, room_id, card_id, *, key="", profile="", expected=None, actual=None,
               cost_usd=None, text="") -> bool:
    """Предупреждение по одной карточке — не чаще раза в `WARN_TTL` на тот же повод.

    Без этого в режиме «только предупреждать» каждый проход (3 с) писал новую строку в
    журнал комнаты: за сутки — десятки тысяч строк, в которых тонут настоящие события.
    """
    now = int(time.time())
    seen = (read_state(cfg, room_id).get("warned") or {}).get(str(card_id)) or {}
    if (now - int(seen.get("at") or 0) < WARN_TTL
            and str(seen.get("key") or "") == str(key or "")):
        return False

    def change(data):
        data["warned"][str(card_id)] = {"at": now, "key": str(key or "")}

    _mutate(cfg, room_id, change)
    record(cfg, room_id, "warned", card_id=card_id, profile=profile, expected=expected,
           actual=actual, cost_usd=cost_usd, text=text)
    return True


def _unknown_once(cfg, room_id, card_id, *, profile, expected, text) -> bool:
    """Предупреждение «модель неизвестна» — не чаще раза в сутки на карточку."""
    now = int(time.time())
    seen = read_state(cfg, room_id).get("unknown") or {}
    if now - int((seen.get(str(card_id)) or {}).get("at") or 0) < UNKNOWN_TTL:
        return False

    def change(data):
        data["unknown"][str(card_id)] = {"at": now, "profile": str(profile or "")}

    _mutate(cfg, room_id, change)
    record(cfg, room_id, "unknown", card_id=card_id, profile=profile, expected=expected,
           text=text, keep_journal=True)
    return True


def tick(cfg, room, *, now=None, log=None, limit: int = 80) -> dict:
    """Один проход быстрого сторожа по комнате (W23.3).

    Возвращает счётчики и списки карточек: `fixed`, `stopped`, `warned`, `unknown`,
    `skipped`. Ничего не делает в демо/моке, без доски, без карточек tenant комнаты.
    """
    room = room if isinstance(room, dict) else {}
    out = {"room": str(room.get("id") or ""), "mode": mode_of(room), "checked": 0,
           "fixed": [], "stopped": [], "warned": [], "unknown": 0, "skipped": 0,
           "errors": [], "skipped_reason": ""}
    room_id = out["room"]
    if not room_id or room.get("archived"):
        out["skipped_reason"] = "no_room"
        return out
    if getattr(cfg, "mock", False) or getattr(cfg, "demo", False):
        out["skipped_reason"] = "demo"
        return out
    tenant = str(room.get("tenant") or (TENANT_PREFIX + room_id))
    if not tenant.startswith(TENANT_PREFIX):
        out["skipped_reason"] = "not_room_tenant"
        return out
    board = str(room.get("board") or getattr(cfg, "board", "") or "team")
    con = _connect(cfg, board)
    if con is None:
        out["skipped_reason"] = "no_board"
        return out
    stamp = int(now or time.time())
    gmode = mode_of(room)
    try:
        try:
            cards = models.card_map(cfg, room)
        except Exception as exc:             # noqa: BLE001 - чтение доски вторично
            out["errors"].append("карточки: %s" % str(exc)[:120])
            out["skipped_reason"] = "cards_unreadable"
            return out
        for card in list(cards.values())[:max(1, int(limit))]:
            status = str(card.get("status") or "")
            profile = str(card.get("assignee") or "")
            cid = str(card.get("id") or "")
            if not cid or not profile or status in LOCKED_STATUSES:
                continue
            if status not in PENDING_STATUSES and status not in RUNNING_STATUSES:
                continue
            # состояние комнаты читаем ОДИН раз на карточку (в нём и «кто остановил», и бюджет)
            state = read_state(cfg, room_id)
            stopper = str(((state.get("stopped") or {}).get(cid) or {}).get("by") or "")
            try:
                tries = int(((state.get("restarts") or {}).get(cid) or {}).get("count") or 0)
            except (TypeError, ValueError):
                tries = 0
            # Человек остановил — сторож не поднимает. Сторож сам запарковал (после 2
            # перезапусков) — тоже не трогает, пока карточку не поднимут заново.
            if stopper == "user" or (stopper == "guard" and status == "blocked"):
                out["skipped"] += 1
                continue
            try:
                expected = expected_model(cfg, room, profile, card_id=cid)
            except Exception as exc:         # noqa: BLE001 - конфиг не читается
                out["errors"].append("модель %s: %s" % (profile, str(exc)[:120]))
                continue
            if expected.get("source") == "none" or not expected.get("model"):
                out["unknown"] += 1
                _unknown_once(cfg, room_id, cid, profile=profile, expected=expected,
                              text="Модель для «%s» неизвестна — сверить не с чем "
                                   "(в профиле и в комнате модель не задана)" % profile)
                continue
            if status in PENDING_STATUSES:
                want = _want_for(cfg, room, profile, cid)
                if not needs_update(card, want):
                    out["checked"] += 1
                    continue
                if gmode != "enforce":
                    out["warned"].append(cid)
                    _warn_once(cfg, room_id, cid,
                               key="pending|%s|%s" % (short(card.get("provider"), card.get("model")),
                                                       short(expected["provider"], expected["model"])),
                               profile=profile, expected=expected,
                               actual={"provider": card.get("provider"), "model": card.get("model")},
                               text="Карточка «%s» пойдёт на %s, а ожидается %s — править не буду "
                                    "(режим «только предупреждать»)"
                                    % (profile, short(card.get("provider"), card.get("model"))
                                       or "модель профиля", short(expected["provider"],
                                                                 expected["model"])))
                    continue
                done, failed = _apply_want(cfg, board, card, want)
                if failed:
                    out["errors"].append("мост: %s" % str((failed or [{}])[0].get("reason"))[:120])
                    continue
                root_id = _root_of(con, cid)
                was = short(card.get("provider"), card.get("model")) or "не задана"
                now_label = short(want.get("provider"), want.get("model"),
                                  want.get("reasoning")) or \
                    "модель профиля (%s)" % short(expected["provider"], expected["model"])
                out["fixed"].append(cid)
                record(cfg, room_id, "fixed", card_id=cid, root_id=root_id, profile=profile,
                       expected=expected,
                       actual={"provider": card.get("provider"), "model": card.get("model")},
                       text="Модель карточки исправлена до запуска: %s → %s" % (was, now_label))
                continue
            # --- карточка уже идёт: сверяем ФАКТ из сессии воркера
            got = _actual_of(cfg, con, card)
            if not got.get("model"):
                out["unknown"] += 1          # воркер ещё не записал сессию — не трогаем
                continue
            out["checked"] += 1
            expected_pair = {"provider": expected["provider"], "model": expected["model"]}
            if same_model(expected_pair, got):
                if tries:
                    _clear_restarts(cfg, room_id, cid)   # поехали верно — бюджет снова полный
                continue
            root_id = _root_of(con, cid)
            actual_label = short(got.get("provider"), got.get("model"))
            expected_label = short(expected["provider"], expected["model"])
            if same_name(expected_pair, got):
                # модель ТА ЖЕ, расходится только ярлык провайдера (custom/llamacpp/openai у
                # одного локального адреса) — нормальный запуск не убиваем (W23 §2.3)
                _warn_once(cfg, room_id, cid, key="provider|%s|%s" % (actual_label, expected_label),
                           profile=profile, expected=expected, actual=got,
                           cost_usd=got.get("cost_usd"),
                           text="Запуск идёт на модели %s, провайдер записан как «%s» вместо "
                                "«%s» — модель та же, работу не останавливаю"
                                % (expected["model"], got.get("provider") or "—",
                                   expected.get("provider") or "—"))
                continue
            if gmode != "enforce":
                out["warned"].append(cid)
                _warn_once(cfg, room_id, cid, key="running|%s|%s" % (actual_label, expected_label),
                           profile=profile, expected=expected, actual=got,
                           cost_usd=got.get("cost_usd"),
                           text="Запуск идёт на %s вместо ожидаемой %s — останавливать не буду "
                                "(режим «только предупреждать»)" % (actual_label, expected_label))
                continue
            started = int(got.get("started_at") or card.get("started_at") or 0)
            if started and stamp - started < GUARD_MIN_AGE:
                out["skipped"] += 1          # только что взяли — дадим воркеру шанс
                continue
            if tries >= GUARD_MAX_RESTARTS:
                _park(cfg, room, card, room_id=room_id, root_id=root_id,
                      profile=profile, expected=expected, actual=got, gmode=gmode)
                out["stopped"].append(cid)
                continue
            want = _want_for(cfg, room, profile, cid)
            # Уровень мышления пишем ДО снятия работы: его читают при запуске воркера.
            effort = str(want.get("reasoning") or "")
            if effort != str(card.get("reasoning") or ""):
                try:
                    models.push_effort(cfg, board, [cid], effort)
                except Exception:            # noqa: BLE001 - мышление вторично
                    pass
            # Один процесс (W23-AT1): `set_model` и `reclaim` подряд, без окна `ready` между
            # двумя вызовами — иначе диспетчер подхватил бы карточку со старым переопределением.
            try:
                restarted = _ew().card_restart_on_model(
                    cfg, board, cid, model=want.get("model") or None,
                    provider=str(want.get("provider") or ""),
                    reason="контроль моделей: неверная модель")
            except Exception as exc:         # noqa: BLE001 - одна карточка не рушит проход
                out["errors"].append("остановка %s: %s" % (cid, str(exc)[:120]))
                continue
            if not restarted.get("ok"):
                out["errors"].append("не удалось остановить %s" % cid)
                continue
            _bump_restarts(cfg, room_id, cid, model=str(got.get("model") or ""))
            out["stopped"].append(cid)
            record(cfg, room_id, "stopped", card_id=cid, root_id=root_id, profile=profile,
                   expected=expected, actual=got, cost_usd=got.get("cost_usd"),
                   text="Запуск шёл на %s вместо %s — остановлено и перезапущено на %s"
                        % (actual_label, expected_label, expected_label))
            _note(cfg, room, root_id or cid,
                  "Запуск шёл на %s вместо %s — остановила и перезапустила на %s"
                  % (actual_label, expected_label, expected_label))
    finally:
        con.close()
    return out


def _park(cfg, room, card, *, room_id, root_id, profile, expected, actual, gmode) -> None:
    """После 2 перезапусков: остановить, поставить на паузу, спросить человека.

    Остановка и пауза — одно действие (`card_stop`, W23-AT1): карточка ни на миг не остаётся
    в очереди. Дальше карточку ведёт человек (`card_model` / `card_resume`).
    """
    cid = str(card.get("id") or "")
    board = str((room or {}).get("board") or getattr(cfg, "board", "") or "team")
    try:
        # Остановка и пауза — ОДНО действие (W23-AT1): воркер завершается и карточка сразу
        # удерживается, поэтому диспетчер не подхватывает её между `reclaim` и `block`.
        _ew().card_stop(cfg, board, cid, reason="модель не совпадает, нужно решение",
                        kind="needs_input")
    except Exception:                        # noqa: BLE001 - пауза не встала: человек увидит текст
        pass
    mark_stopped(cfg, room_id, cid, by="guard", reason="модель не совпадает")
    text = ("Карточка %s (%s) идёт на %s вместо ожидаемой %s. Перезапускала %d раза — "
            "нужно решение" % (cid, profile, short(actual.get("provider"), actual.get("model")),
                               short(expected.get("provider"), expected.get("model")),
                               GUARD_MAX_RESTARTS))
    record(cfg, room_id, "stopped", card_id=cid, root_id=root_id, profile=profile,
           expected=expected, actual=actual, cost_usd=actual.get("cost_usd"), text=text)
    if gmode != "enforce":
        return
    _note(cfg, room, root_id or cid,
          "%s Модель не совпадает: ожидалась %s, запущено %s. Автоматически перезапускала %d "
          "раза и остановила — нужен ваш выбор: сменить модель карточки или поправить "
          "настройки агента."
          % (LEAD_QUESTION_MARK, short(expected.get("provider"), expected.get("model")),
             short(actual.get("provider"), actual.get("model")), GUARD_MAX_RESTARTS))


# ---------------------------------------------------------------- model-check

def _title_of(room, profile) -> str:
    """«Имя — роль» участника (подписи комнаты, как на экране «Команда»)."""
    name = str(profile or "")
    label = ""
    for member in (room or {}).get("members") or []:
        if isinstance(member, dict) and str(member.get("profile") or "") == name:
            label = str(member.get("label") or "")
            break
    if not label:
        roles = (room or {}).get("role_labels") or {}
        label = str(roles.get(name) or "")
    role = models.ROLE_RU.get(models.role_ru(room, name), "")
    if label and role:
        return "%s — %s" % (label, role)
    return label or role or name


def model_check(cfg, room) -> dict:
    """`GET /api/rooms/{id}/model-check` — «куда собирается уйти запрос» (W23.4)."""
    room = room if isinstance(room, dict) else {}
    room_id = str(room.get("id") or "")
    out = {"room": room_id, "at": int(time.time()), "members": [], "guard": {},
           "warnings": []}
    board = str(room.get("board") or getattr(cfg, "board", "") or "team")
    cards = {}
    try:
        cards = models.card_map(cfg, room)
    except Exception as exc:                 # noqa: BLE001 - доска вторична
        out["warnings"].append("Карточки не прочитаны: %s" % str(exc)[:120])
    con = _connect(cfg, board)
    try:
        for profile, _role in models.participants(room):
            expected = expected_model(cfg, room, profile)
            if expected.get("source") == "none":
                out["warnings"].append("У «%s» модель не задана — сверить не с чем" % profile)
            card_model = None
            last_run = None
            problem = None
            ok = True
            known = bool(expected.get("model"))
            mine = [c for c in cards.values() if str(c.get("assignee") or "") == profile]
            pinned = [c for c in mine if str(c.get("model") or "")]
            if pinned:
                card_model = {"provider": str(pinned[0].get("provider") or ""),
                              "model": str(pinned[0].get("model") or "")}
                if known and needs_update(pinned[0], {
                        "provider": expected["provider"], "model": expected["model"]}):
                    ok = False
                    problem = ("%s: карточка пойдёт на %s, а ожидается %s"
                               % (profile, short(card_model["provider"], card_model["model"]),
                                  short(expected["provider"], expected["model"])))
            if con is not None:
                for card in sorted(mine, key=lambda c: int(c.get("created_at") or 0), reverse=True):
                    got = _actual_of(cfg, con, card)
                    if got.get("model"):
                        last_run = {"provider": got.get("provider") or "",
                                    "model": got.get("model") or "",
                                    "at": int(got.get("started_at") or 0),
                                    "cost_usd": got.get("cost_usd"),
                                    "card_id": str(card.get("id") or "")}
                        break
                if last_run and known and not same_model(expected, last_run):
                    ok = False
                    problem = ("%s: ожидалась %s (%s), запущено %s (%s)"
                               % (profile, expected["model"], expected.get("provider") or "—",
                                  last_run["model"], last_run["provider"] or "—"))
            if not known:
                # модель участника определить нельзя: не «всё хорошо» и не «плохо»,
                # а «контроль не вмешивается» — интерфейс рисует нейтральный статус
                ok = None if not card_model else ok
                problem = problem or ("%s: модель участника не задана — контроль не вмешивается"
                                      % profile)
            out["members"].append({"profile": profile, "title": _title_of(room, profile),
                                   "expected": expected, "card_model": card_model,
                                   "last_run": last_run,
                                   "ok": None if ok is None else bool(ok), "problem": problem})
        guard = counters(cfg, room_id)
        gmode = mode_of(room)
        open_now = sum(1 for c in cards.values()
                       if str(c.get("assignee") or "") and str(c.get("status") or "") in PENDING_STATUSES
                       and needs_update(c, _want_for(cfg, room, str(c.get("assignee") or ""),
                                                     str(c.get("id") or ""))))
        guard.update({"mode": gmode, "open": open_now,
                      "text": counters_text(guard, gmode)})
        out["guard"] = guard
    finally:
        if con is not None:
            con.close()
    if not cards and room_id:
        out["warnings"].append("Карточки комнаты не прочитаны — проверьте доску канбана")
    return out


def stopped_nodes(detail, room, cfg) -> int:
    """Проставить `stopped` в узлы дерева, остановленные человеком (W23-ST).

    MG2 рисует по этому полю строку «Остановлено вами в 10:41 · модель: …» и кнопку
    «Продолжить»; в дереве статус карточки — `blocked`, а кто и когда её остановил,
    видно только в реестре сторожа (`data/state/<room>/model_guard.json: stopped`).
    Возвращает число размеченных узлов. Сбой реестра не портит страницу запроса.
    """
    if not isinstance(detail, dict):
        return 0
    room_id = str((room if isinstance(room, dict) else {}).get("id") or detail.get("room") or "")
    if not room_id:
        return 0
    try:
        paused = stopped_by_user(cfg, room_id)
    except Exception:                        # noqa: BLE001 - разметка вторична
        return 0
    if not paused:
        return 0
    marked = 0
    for node in detail.get("tree") or []:
        if not isinstance(node, dict):
            continue
        item = paused.get(str(node.get("id") or ""))
        if not isinstance(item, dict):
            continue
        node["stopped"] = {"by": "user", "at": int(item.get("at") or 0),
                           "model": str(item.get("model") or ""),
                           "provider": str(item.get("provider") or ""),
                           "cost_usd": item.get("cost_usd")}
        marked += 1
    return marked


def attach(detail, room, cfg) -> None:
    """Поле `model_guard[]` в деталях запроса (W23.5) — готовые русские тексты."""
    if not isinstance(detail, dict):
        return
    room = room if isinstance(room, dict) else {}
    room_id = str(room.get("id") or detail.get("room") or "")
    if not room_id:
        return
    try:
        items = events(cfg, room_id, detail.get("id"), limit=50)
    except Exception:                        # noqa: BLE001 - лента вторична
        items = []
    detail["model_guard"] = items
    stopped_nodes(detail, room, cfg)         # W23-ST: `stopped` у карточки в дереве


def diag_item(cfg, room=None, *, rooms=None) -> dict:
    """Пункт «Контроль моделей» для «Проверки системы» (W23.6)."""
    targets = [room] if isinstance(room, dict) else list(rooms or [])
    problems: list = []
    fixed = stopped = 0
    mode = DEFAULT_MODE
    checked_cards = 0
    for item in targets:
        if not isinstance(item, dict) or not item.get("id"):
            continue
        mode = mode_of(item)
        data = counters(cfg, item.get("id"))
        fixed += int(data.get("fixed_24h") or 0)
        stopped += int(data.get("stopped_24h") or 0)
        try:
            check = model_check(cfg, item)
        except Exception as exc:             # noqa: BLE001 - проверка не должна падать
            problems.append("проверка не удалась: %s" % str(exc)[:120])
            continue
        checked_cards += len(check.get("members") or [])
        problems += [str(m.get("problem")) for m in check.get("members") or []
                     if m.get("problem")]
    if not targets:
        return _item("off", "Строгий контроль: проверять нечего — комнат нет")
    status = "warn" if (problems or mode == "warn") else "ok"
    value = counters_text({"fixed_24h": fixed, "stopped_24h": stopped,
                           "saved_usd_est": 0}, mode)
    return _item(status, value, problems)


def _item(status: str, value: str, problems=None) -> dict:
    return {"id": "model_guard", "title": "Контроль моделей", "status": status,
            "value": str(value or ""),
            "hint": "Запрос уходит только на модель из настроек агента — расхождения "
                    "исправляются до запуска",
            "fix": None, "details": [str(p)[:300] for p in (problems or [])][:5]}


# ---------------------------------------------------------------- карточка: действия

class GuardError(Exception):
    """Ошибка действия над карточкой: code = not_found|conflict|bad_request."""

    def __init__(self, message: str, code: str = "bad_request", detail: dict | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.detail = detail or {}


def _require_card(cfg, room, board: str, task_id: str) -> dict:
    """Карточка комнаты: только свой tenant и только «живая» работа (W23.8)."""
    room = room if isinstance(room, dict) else {}
    room_id = str(room.get("id") or "")
    tenant = str(room.get("tenant") or (TENANT_PREFIX + room_id))
    con = _connect(cfg, board)
    if con is None:
        raise GuardError("Доска канбана недоступна", "unavailable", {"board": board})
    try:
        row = con.execute("SELECT id, title, status, assignee, tenant, model_override, "
                          "provider_override FROM tasks WHERE id = ?",
                          (str(task_id or ""),)).fetchone()
        item = dict(row) if row is not None else {}
        root_id = _root_of(con, task_id) if item else ""
    finally:
        con.close()
    if not item or str(item.get("tenant") or "") != tenant:
        raise GuardError("Карточка не из этого запроса", "not_found",
                         {"task_id": str(task_id or ""), "room": room_id})
    item["root_id"] = root_id
    item["_tenant"] = tenant
    return item


def _status_ru(status: str) -> str:
    return {"running": "работает", "ready": "в очереди", "blocked": "остановлена",
            "todo": "ждёт других карточек", "triage": "ещё не разложена",
            "review": "ждёт проверки", "done": "завершена",
            "archived": "в архиве"}.get(str(status or ""), str(status or ""))


def _guard_status(status: str, action: str) -> None:
    """Разрешённые статусы: только там, где работа идёт или вот-вот пойдёт."""
    status = str(status or "")
    if status in ("done", "archived"):
        raise GuardError("Карточка уже %s" % _status_ru(status), "conflict",
                         {"status": status, "action": action})
    if status in ("triage", "todo"):
        raise GuardError("Карточка ещё не запускалась (%s) — модель применится при запуске"
                         % _status_ru(status), "conflict", {"status": status, "action": action})
    if status == "review":
        raise GuardError("Карточка ждёт проверки — останавливать и менять ей модель нечего",
                         "conflict", {"status": status, "action": action})
    if status not in ("running", "ready", "blocked"):
        raise GuardError("Действие недоступно в состоянии «%s»" % _status_ru(status),
                         "conflict", {"status": status, "action": action})


def _reread(cfg, board: str, task_id: str) -> dict:
    con = _connect(cfg, board)
    if con is None:
        return {}
    try:
        return _card_row(con, task_id)
    finally:
        con.close()


def card_stop(cfg, room, board: str, task_id: str, *, note: str = "") -> dict:
    """Остановить работу карточки, не теряя прогресс (W23.8)."""
    card = _require_card(cfg, room, board, task_id)
    status = str(card.get("status") or "")
    _guard_status(status, "card_stop")
    if status == "blocked":
        mark_stopped(cfg, room.get("id"), task_id, by="user", reason=note or "остановлено пользователем",
                     model=str(card.get("model_override") or ""),
                     provider=str(card.get("provider_override") or ""))
        return _card_result(cfg, room, board, task_id, action="card_stop",
                            message="Карточка уже остановлена — работа стоит, прогресс на месте",
                            detail={"paused": True})
    # W23-ST: что было в работе на момент остановки (модель и стоимость) — снимаем ДО
    # `reclaim`: после него открытого прогона нет, и взять эти данные будет неоткуда.
    actual = {}
    if status == "running":
        con = _connect(cfg, board)
        if con is not None:
            try:
                actual = _actual_of(cfg, con, card)
            except Exception:                 # noqa: BLE001 - подпись вторична, остановка важнее
                actual = {}
            finally:
                con.close()
    ew = _ew()                            # ленивый импорт: циклов между модулями нет
    # Остановка — ОДНО действие (W23-AT1): воркер завершается и карточка сразу удерживается,
    # окна `ready` между `reclaim` и `block` нет, результат проверяется по доске.
    res = {}
    error = ""
    stopped = False
    try:
        res = ew.card_stop(cfg, board, task_id, reason=note or "остановлено пользователем")
    except Exception as exc:              # noqa: BLE001 - уже снята/успела завершиться
        if str(getattr(exc, "code", "")) == "not_found":
            raise GuardError("Карточка не найдена в доске канбана", "not_found",
                             {"task_id": task_id}) from exc
        error = str(exc)[:200]
    stopped = bool(res.get("terminated")) or status == "running"
    paused = bool(res.get("ok"))
    mark_stopped(cfg, room.get("id"), task_id, by="user", reason=note or "остановлено пользователем",
                 model=str(actual.get("model") or card.get("model_override") or ""),
                 provider=str(actual.get("provider") or card.get("provider_override") or ""),
                 cost_usd=actual.get("cost_usd"))
    record(cfg, room.get("id"), "stopped_by_user", card_id=task_id, root_id=card.get("root_id"),
           profile=str(card.get("assignee") or ""),
           expected=expected_model(cfg, room, card.get("assignee"), card_id=task_id),
           actual={"provider": card.get("provider_override"), "model": card.get("model_override")},
           text="Карточка %s (%s) остановлена вами" % (task_id, card.get("assignee") or "—"),
           actor="mikhail")
    if paused:
        message = ("Карточка остановлена. Работа и папка сохранены — «Продолжить» вернёт её "
                   "в очередь")
    elif res.get("worker_alive"):
        message = ("Работа снята с карточки, но процесс воркера ещё жив — нажмите «Остановить» "
                   "ещё раз (или проверьте разрешения на завершение процесса)")
    elif error:
        message = "Не удалось остановить работу: %s" % error
    else:
        message = "Работа снята, но поставить карточку на паузу не удалось — она снова в очереди"
    return _card_result(cfg, room, board, task_id, action="card_stop", message=message,
                        detail={"paused": bool(paused), "stopped": bool(stopped),
                                "worker_alive": bool(res.get("worker_alive")),
                                "path": str(res.get("path") or "")})


def card_resume(cfg, room, board: str, task_id: str, *, note: str = "", dispatch: bool = True) -> dict:
    """Продолжить карточку: вернуть в очередь (воркер стартует заново с контекстом)."""
    card = _require_card(cfg, room, board, task_id)
    status = str(card.get("status") or "")
    if status == "running":
        raise GuardError("Карточка уже работает", "conflict",
                         {"status": status, "action": "card_resume"})
    _guard_status(status, "card_resume")
    ew = _ew()
    steps = []
    if status == "blocked":
        try:
            res = ew.card_resume(cfg, board, task_id, reason=note or "продолжаем")
        except Exception as exc:          # noqa: BLE001 - например, статус успел измениться
            raise GuardError("Не удалось снять паузу: %s" % str(exc)[:160], "conflict",
                             {"status": status, "action": "card_resume"}) from exc
        steps.extend([str(step) for step in (res.get("steps") or [])])
        if not res.get("ok"):
            raise GuardError("Не удалось снять паузу: карточка в состоянии «%s»"
                             % _status_ru(str(res.get("status") or "")), "conflict",
                             {"status": str(res.get("status") or ""),
                              "action": "card_resume"})
    now_card = _reread(cfg, board, task_id) or card
    if str(now_card.get("status") or "") == "todo":
        try:
            ew.card_promote(cfg, board, task_id)
            steps.append("promote")
        except Exception:                 # noqa: BLE001 - пусть решает движок по родителям
            pass
    clear_stopped(cfg, room.get("id"), task_id)
    record(cfg, room.get("id"), "resumed", card_id=task_id, root_id=card.get("root_id"),
           profile=str(card.get("assignee") or ""),
           expected=expected_model(cfg, room, card.get("assignee"), card_id=task_id),
           text="Карточка %s (%s) продолжена" % (task_id, card.get("assignee") or "—"),
           actor="mikhail")
    if dispatch and not getattr(cfg, "sandbox", False):
        try:
            ew.dispatch_board(cfg, board, max_cards=1, confirm=True)
        except Exception:                 # noqa: BLE001 - диспетчер подхватит сам
            pass
    return _card_result(cfg, room, board, task_id, action="card_resume",
                        message="Карточка продолжает работу — агент вернётся к ней с того же места",
                        detail={"steps": steps})


def card_set_model(cfg, room, board: str, task_id: str, *, model=None, provider=None,
                   note: str = "") -> dict:
    """Сменить модель карточки (или вернуть к модели профиля) и продолжить (W23.8)."""
    card = _require_card(cfg, room, board, task_id)
    status = str(card.get("status") or "")
    _guard_status(status, "card_model")
    raw = "" if model is None else str(model).strip()
    clear = raw.lower() in ("", "none", "null", "-")
    provider = str(provider or "").strip()
    if not clear:
        if len(raw) > models.MAX_MODEL:
            raise GuardError("Слишком длинное имя модели — до %d знаков" % models.MAX_MODEL,
                             "bad_request", {"field": "model"})
        if raw.startswith("-"):
            raise GuardError("Некорректное имя модели", "bad_request", {"field": "model"})
        try:
            clean, _warnings = models.validate(cfg, provider or None, raw, None)
        except models.ModelError as exc:
            raise GuardError(str(exc.message), "bad_request",
                             dict(getattr(exc, "detail", {}) or {})) from exc
        raw = str(clean.get("model") or raw)
        provider = str(clean.get("provider") or provider)
    ew = _ew()
    steps: list = []
    resumed = False
    try:
        # Один процесс (W23-AT1): модель записывается и работа снимается/снимается пауза
        # подряд — диспетчер не успевает подхватить карточку со старым переопределением.
        res = ew.card_model_and_resume(
            cfg, board, task_id, model=None if clear else raw,
            provider="" if clear else provider,
            reason=note or "меняем модель и продолжаем")
    except Exception as exc:              # noqa: BLE001 - движок отказал по-русски
        raise GuardError(str(exc)[:200] or "Не удалось изменить модель карточки",
                         "conflict", {"status": status, "action": "card_model"}) from exc
    steps.extend([str(step) for step in (res.get("steps") or [])])
    if clear:
        set_user_model(cfg, room.get("id"), task_id, "", "")
    else:
        set_user_model(cfg, room.get("id"), task_id, provider, raw)
    resumed = bool(res.get("ok")) and status in ("running", "blocked")
    if not res.get("ok") and not res.get("worker_alive"):
        raise GuardError("Не удалось продолжить карточку на новой модели — она в состоянии «%s»"
                         % _status_ru(str(res.get("status") or "")), "conflict",
                         {"status": str(res.get("status") or ""), "action": "card_model"})
    if resumed:
        clear_stopped(cfg, room.get("id"), task_id)
    expected = expected_model(cfg, room, card.get("assignee"), card_id=task_id)
    label = short(expected.get("provider"), expected.get("model")) or "модель профиля"
    record(cfg, room.get("id"), "model_changed", card_id=task_id, root_id=card.get("root_id"),
           profile=str(card.get("assignee") or ""), expected=expected,
           actual={"provider": card.get("provider_override"), "model": card.get("model_override")},
           text="Модель карточки %s: %s → %s" % (
               task_id,
               short(card.get("provider_override"), card.get("model_override")) or "не задана",
               label),
           actor="mikhail")
    message = ("Модель карточки: %s. Работа перезапущена на ней" % label if resumed
               else "Модель карточки: %s. Применится при следующем запуске" % label)
    if resumed and not getattr(cfg, "sandbox", False):
        try:
            ew.dispatch_board(cfg, board, max_cards=1, confirm=True)
        except Exception:                 # noqa: BLE001 - диспетчер подхватит сам
            pass
    return _card_result(cfg, room, board, task_id, action="card_model", message=message,
                        detail={"steps": steps, "resumed": bool(resumed)},
                        model={"provider": expected.get("provider") or "",
                               "model": expected.get("model") or "",
                               "source": expected.get("source") or "none"})


def _card_result(cfg, room, board: str, task_id: str, *, action: str, message: str,
                 detail=None, model=None) -> dict:
    now_card = _reread(cfg, board, task_id) or {}
    if model is None:
        expected = expected_model(cfg, room, now_card.get("assignee") or "",
                                  card_id=task_id)
        model = {"provider": expected.get("provider") or "",
                 "model": expected.get("model") or "",
                 "source": expected.get("source") or "none"}
    return {"ok": True, "action": action, "task_id": str(task_id),
            "status": str(now_card.get("status") or ""), "model": model,
            "message": message, "detail": dict(detail or {})}


def resume_stopped(cfg, room, board: str, root_id: str, *, note: str = "") -> list:
    """Возобновить карточки запроса, остановленные человеком (после его сообщения)."""
    room = room if isinstance(room, dict) else {}
    room_id = str(room.get("id") or "")
    if not room_id:
        return []
    items = stopped_by_user(cfg, room_id)
    if not items:
        return []
    con = _connect(cfg, board)
    if con is None:
        return []
    try:
        ids = set()
        try:
            from .engine_write_guard import tree_ids
            ids = set(tree_ids(cfg, board, str(root_id)))
        except Exception:                 # noqa: BLE001 - без дерева смотрим по корню
            ids = {str(root_id)}
    finally:
        con.close()
    resumed = []
    for task_id in sorted(items):
        if ids and task_id not in ids:
            continue
        try:
            res = card_resume(cfg, room, board, task_id, note=note, dispatch=False)
        except Exception:                 # noqa: BLE001 - одна карточка не мешает другим
            continue
        if res.get("ok"):
            resumed.append(task_id)
    return resumed


def is_resume_text(text: str) -> bool:
    """Похоже ли сообщение на «продолжай» (только для текста журнала/подсказки)."""
    low = " ".join(str(text or "").lower().split())
    return any(word in low for word in RESUME_WORDS)


__all__ = ["MODES", "DEFAULT_MODE", "MODE_LABELS", "MODE_HINTS", "GUARD_TICK", "GUARD_MIN_AGE",
           "GUARD_MAX_RESTARTS", "PENDING_STATUSES", "RUNNING_STATUSES", "ALIASES",
           "GuardError", "canon_model", "canon_provider", "same_model", "same_name", "short",
           "normalize_mode", "mode_of", "mode_block", "expected_model", "override_for_card",
           "needs_update", "state_path", "read_state", "events", "counters", "counters_text",
           "user_models", "set_user_model", "stopped_by", "stopped_by_user", "mark_stopped",
           "clear_stopped", "record", "tick", "model_check", "attach", "diag_item", "stopped_nodes",
           "session_info", "card_stop", "card_resume", "card_set_model", "resume_stopped",
           "is_resume_text", "base_url_of"]
