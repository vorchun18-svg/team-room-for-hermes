"""Модель, провайдер и уровень мышления участника комнаты (WAVE 10).

Требование Михаила 03.11.2026 (docs/WAVE10-RULES.md): у каждого участника комнаты —
руководителя, критика, исполнителя — можно выбрать модель, провайдера и уровень мышления.
Замена действует **только в этой комнате** и только для её карточек; по умолчанию берутся
настройки самого агента (read-only чтение конфига профиля). Файлы профилей
(`config.yaml`, `SOUL.md`, `skills`) не меняются никогда — как и у `agent_notes`.

Как это доходит до движка:

* новые карточки — `engine_write.create_request` передаёт `--model/--provider` в
  `hermes kanban create`, а уровень мышления ставит мост (`tools/kb_bridge.py`), потому что
  флага для effort в CLI нет;
* дочерние карточки декомпозитора — сторож `apply_to_tree` из watcher: карточки
  `triage/todo/ready/blocked/queued` закрепляются на модели комнаты идемпотентно,
  `running` и `done` не трогаются;
* идущие карточки — только явная кнопка «Перезапустить на новой модели» (`restart_running`).

Публичные функции::

    catalog(cfg, force=False) -> {providers, efforts, source, cached, updated_at, warnings}
    agent_default(cfg, profile) -> {provider, model, reasoning}
    validate(cfg, provider, model, reasoning) -> (clean, warnings)
    validate_map(cfg, value, members) -> {профиль: {provider, model, reasoning, updated_at}}
    normalize(value) -> {...}                      # мягкое чтение реестра
    settings_of(room) -> {профиль: {...}}
    effective(cfg, room, profile) -> {provider, model, reasoning, source, updated_at}
    get(cfg, room_id, profile) -> {profile, role, model_settings, agent_default, ...}
    set(cfg, room_id, profile, provider, model, reasoning) -> {...}
    clear(cfg, room_id, profile) -> {...}
    apply_to_tree(cfg, room_id, root_id) -> {updated, skipped, failed}
    apply_profile(cfg, room_id, profile) -> {cards_updated, pending_running, cards_failed}
    restart_running(cfg, room_id, profile) -> {restarted, cards, warnings}
    decorate_room(cfg, detail) -> RoomDetail с model_settings/agent_default у участников
    label(provider, model, reasoning, providers=None) -> «провайдер · модель · мышление»

Секреты: значения ключей НИКОГДА не читаются и не печатаются — только факт наличия
провайдера в каталоге Hermes (`configured`).
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

try:                                    # import server.agent_models
    from . import proc                  # запуск моста — только через proc (без окна)
    from . import statefiles as sf      # noqa: F401  (единый стиль с соседями)
except ImportError:                      # python server/app.py (плоский импорт)
    import proc                          # type: ignore
    import statefiles as sf              # type: ignore  # noqa: F401

MAX_MODEL = 200                          # знаков в имени модели
CACHE_TTL = 60                           # секунд: каталог меняется только при смене ключей
BRIDGE_TIMEOUT = 40                      # секунд: импорт hermes_cli ~1 с, запас на диск
MAX_CARDS = 120                          # карточек за один проход сторожа
#: статусы, на которых замена ещё «сработает» при следующем запуске
APPLY_STATUSES = ("triage", "todo", "ready", "blocked", "queued")
#: статусы, которые сторож НЕ трогает (идущая работа и завершённое)
LOCKED_STATUSES = ("running", "review", "done", "archived")
EFFORTS = ("none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra")
EFFORT_LABELS = {"none": "Выключено", "minimal": "Минимальные", "low": "Низкие",
                 "medium": "Средние", "high": "Высокие", "xhigh": "Очень высокие",
                 "max": "Максимальные", "ultra": "Предельные"}
EFFORT_SHORT = {"none": "без размышления", "minimal": "минимальные", "low": "низкие",
                "medium": "средние", "high": "высокие", "xhigh": "очень высокие",
                "max": "максимальные", "ultra": "предельные"}
ROLE_RU = {"orchestrator": "руководитель комнаты", "critic": "критик",
           "worker": "исполнитель", "observer": "наблюдатель"}

#: публичная функция `set` (договорённость §«Интерфейсы») занимает имя — конструктор прячем
_new_set = set

#: каталог: один на процесс, ключ — путь к Hermes (в песочнице меняется редко)
_CACHE: dict = {"key": None, "at": 0.0, "data": None}
_PYTHON: dict = {}


class ModelError(Exception):
    """Ошибка выбора модели: code = bad_request|not_found|unavailable (роут отдаст 4xx)."""

    def __init__(self, message: str, code: str = "bad_request", detail: dict | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.detail = detail or {}


def _now() -> int:
    return int(time.time())


def _profile(value) -> str:
    return str(value or "").strip()


def _one_line(text) -> str:
    return " ".join(str(text or "").split())


# ---------------------------------------------------------------- пути Hermes

def _root(cfg) -> Path:
    return Path(getattr(cfg, "hermes_home", "") or "")


def config_path(cfg, profile) -> Path:
    """Конфиг профиля: `default` — корень `%HERMES_HOME%`, остальные — `profiles/<name>/`."""
    name = _profile(profile)
    root = _root(cfg)
    if name in ("", "default"):
        return root / "config.yaml"
    return root / "profiles" / name / "config.yaml"


def bridge_path(cfg=None) -> Path:
    """Скрипт моста: `<app>/tools/kb_bridge.py` (единственное место с hermes_cli)."""
    app_dir = Path(getattr(cfg, "app_dir", "") or Path(__file__).resolve().parent.parent)
    return app_dir / "tools" / "kb_bridge.py"


def _python(cfg) -> str:
    """Интерпретатор Hermes: текущий, иначе python из установок Hermes."""
    key = str(getattr(cfg, "hermes_home", ""))
    if key in _PYTHON:
        return _PYTHON[key]
    candidates: list[str] = []
    for value in (os.environ.get("TEAMROOM_HERMES_PYTHON"), sys.executable):
        if value:
            candidates.append(str(value))
    installs = _root(cfg) / "installs"
    if installs.is_dir():
        try:
            for env_dir in sorted(installs.glob("*/environments/*/venv/Scripts/python.exe")):
                candidates.append(str(env_dir))
        except OSError:
            pass
    for candidate in candidates:
        if candidate and Path(candidate).exists():
            _PYTHON[key] = candidate
            return candidate
    return sys.executable or "python"


def _env(cfg) -> dict:
    """Окружение моста: без агентской сессии, без байткода в реальном Hermes."""
    clean = ("HERMES_DELEGATED_CHILD_CONTEXT", "HERMES_KANBAN_DB", "HERMES_KANBAN_TASK",
             "HERMES_KANBAN_BOARD", "HERMES_KANBAN_HOME", "HERMES_KANBAN_WORKSPACES_ROOT",
             "HERMES_KANBAN_ATTACHMENTS_ROOT")
    env = {k: v for k, v in os.environ.items() if k not in clean}
    env["HERMES_AGENT_HOME"] = str(_root(cfg))
    env["PYTHONDONTWRITEBYTECODE"] = "1"      # ничего не пишем в реальный Hermes
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    return env


def _bridge(cfg, args: list[str]) -> dict:
    """Вызвать мост и разобрать JSON-ответ. Ошибки — `ModelError` с русским текстом."""
    script = bridge_path(cfg)
    if not script.exists():
        raise ModelError("Мост к Hermes не найден — выбор модели недоступен.", "unavailable",
                         {"script": str(script)})
    argv = [_python(cfg), "-B", str(script)] + [str(a) for a in args]
    try:
        # Скрытый запуск (CREATE_NO_WINDOW): иначе мост мигает консолью.
        done = proc.run(argv, capture_output=True, text=True, encoding="utf-8",
                        errors="replace", timeout=BRIDGE_TIMEOUT, env=_env(cfg),
                        cwd=str(getattr(cfg, "app_dir", "") or "") or None)
    except subprocess.TimeoutExpired as exc:
        raise ModelError("Hermes не ответил за %d секунд — повторите через минуту." % BRIDGE_TIMEOUT,
                         "unavailable", {"timeout": BRIDGE_TIMEOUT}) from exc
    except OSError as exc:
        raise ModelError("Не удалось запустить Hermes для выбора модели.",
                         "unavailable", {"error": str(exc)[:200]}) from exc
    payload = {}
    for line in reversed((done.stdout or "").splitlines()):
        line = line.strip()
        if not line:
            continue
        try:
            payload = json.loads(line)
            break
        except ValueError:
            continue
    if not isinstance(payload, dict):
        payload = {"ok": False, "code": "engine_error",
                   "error": (done.stderr or done.stdout or "пустой ответ")[-300:]}
    payload["_code"] = done.returncode
    return payload


# ---------------------------------------------------------------- каталог

def _model_label(model_id: str) -> str:
    """Подпись модели для интерфейса: `qwen36` → `Qwen36`, `gpt-oss:20b` остаётся как есть."""
    text = str(model_id or "").strip()
    if not text or "/" in text or ":" in text or "-" in text:
        return text
    return text[:1].upper() + text[1:]


def _norm_catalog(raw, source: str, warnings: list | None = None) -> dict:
    providers: list[dict] = []
    for item in (raw or {}).get("providers") or []:
        if not isinstance(item, dict):
            continue
        pid = str(item.get("id") or "").strip()
        if not pid:
            continue
        models = []
        seen = _new_set()
        for model in item.get("models") or []:
            mid = str((model or {}).get("id") if isinstance(model, dict) else model or "").strip()
            if not mid or mid in seen:
                continue
            seen.add(mid)
            models.append({"id": mid, "label": _model_label(mid)})
        if not bool(item.get("configured", True)):
            continue                      # решение Михаила: только провайдеры с рабочим ключом
        providers.append({"id": pid, "label": str(item.get("label") or pid),
                          "configured": True, "models": models})
    return {"providers": providers,
            "efforts": [{"id": e, "label": EFFORT_LABELS[e]} for e in EFFORTS],
            "source": source, "cached": False, "updated_at": _now(),
            "warnings": list(warnings or [])}


def _fallback_catalog(cfg) -> dict:
    """Hermes недоступен: провайдеры и модели хотя бы из конфигов профилей (read-only)."""
    providers: dict[str, dict] = {}
    try:
        try:
            from . import engine_write as ew
        except ImportError:  # pragma: no cover - плоский импорт
            import engine_write as ew  # type: ignore
        items = ew.list_profiles(cfg) or []
    except Exception:  # noqa: BLE001 - каталог не должен ронять страницу
        items = []
    for item in items:
        provider = str(item.get("provider") or "").strip()
        model = str(item.get("model") or "").strip()
        if not provider:
            continue
        row = providers.setdefault(provider, {"id": provider, "label": provider,
                                              "configured": True,
                                              "models": [], "_seen": _new_set()})
        if model and model not in row["_seen"]:
            row["_seen"].add(model)
            row["models"].append({"id": model})
    out = list(providers.values())
    for row in out:
        row.pop("_seen", None)
    return _norm_catalog({"providers": out}, "profiles",
                         ["Hermes не ответил — список собран из настроек профилей."])


def catalog(cfg, force: bool = False) -> dict:
    """Каталог выбора модели (тот же, что в Hermes Desktop): только настроенные провайдеры.

    Кэш 60 с в процессе; сетевых запросов к провайдерам нет — мост читает только дисковый
    кэш каталогов Hermes. `cfg.model_catalog_override` (dict) — подмена для тестов/демо.
    """
    override = getattr(cfg, "model_catalog_override", None)
    key = str(getattr(cfg, "hermes_home", "")) + "|" + str(getattr(cfg, "app_dir", ""))
    if isinstance(override, dict) and override.get("providers"):
        key += "|override"
        if not force and _CACHE["data"] is not None and _CACHE["key"] == key \
                and time.time() - float(_CACHE["at"] or 0) < CACHE_TTL:
            data = dict(_CACHE["data"])
            data["cached"] = True
            return data
        data = _norm_catalog(override, "override")
        _CACHE.update({"key": key, "at": time.time(), "data": data})
        return data
    if not force and _CACHE["data"] is not None and _CACHE["key"] == key \
            and time.time() - float(_CACHE["at"] or 0) < CACHE_TTL:
        data = dict(_CACHE["data"])
        data["cached"] = True
        return data
    try:
        res = _bridge(cfg, ["--catalog"])
        if not res.get("ok") and not res.get("providers"):
            raise ModelError(str(res.get("error") or "каталог недоступен"), "unavailable")
        data = _norm_catalog(res, str(res.get("source") or "hermes:inventory"))
    except ModelError as exc:
        data = _fallback_catalog(cfg)
        data["warnings"].append(str(exc.message))
    _CACHE.update({"key": key, "at": time.time(), "data": data})
    return data


def _providers(data=None) -> dict:
    return {p["id"]: p for p in (data or {}).get("providers") or [] if p.get("id")}


def provider_label(cfg, provider: str) -> str:
    row = _providers(catalog(cfg)).get(str(provider or ""))
    return str((row or {}).get("label") or provider or "")


# ---------------------------------------------------------------- дефолты профиля

def _scalar(text: str, section: str, key: str) -> str:
    """`section:` → `key: value` первого уровня вложенности без PyYAML (`:` или `=`).

    Уровень вложенности ключей секции определяется по её первому дочернему ключу, а не
    жёстко «два пробела»: конфиги Hermes пишут и 2, и 4 пробела, а ключи вложенных
    словарей (`model.aliases.deepseek: …`) при этом не должны подменять значение.
    """
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
            level = indent              # ключи этой секции — на её собственном уровне
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


def agent_default(cfg, profile) -> dict:
    """Настройки самого агента: `model.provider`, `model.default`, `agent.reasoning_effort`.

    Только собственный блок профиля: `delegation.reasoning_effort` — уровень для
    делегированных детей, а не для карточек комнаты (их берёт сам профиль), поэтому
    за «по умолчанию» он не выдаётся. Пустая строка = «как в Hermes по умолчанию».
    Файл открывается только на чтение.
    """
    path = config_path(cfg, profile)
    try:
        text = path.read_text("utf-8", "replace")
    except OSError:
        return {"provider": "", "model": "", "reasoning": ""}
    reasoning = _scalar(text, "agent", "reasoning_effort")
    if reasoning.lower() in ("false", "none", "disabled"):
        reasoning = "none"
    return {"provider": _scalar(text, "model", "provider"),
            "model": _scalar(text, "model", "default") or _scalar(text, "model", "name"),
            "reasoning": reasoning}


# ---------------------------------------------------------------- проверка

def _check_reasoning(value) -> str:
    text = "" if value is None else str(value).strip().lower()
    if not text:
        return ""
    if text in EFFORTS:
        return text
    raise ModelError("Уровень мышления: выберите один из списка (%s)."
                     % ", ".join(EFFORT_LABELS[e] for e in EFFORTS), "bad_request",
                     {"field": "reasoning", "value": value, "allowed": list(EFFORTS)})


def validate(cfg, provider=None, model=None, reasoning=None, *, data=None) -> tuple[dict, list[str]]:
    """Проверить выбор: провайдер только настроенный, модель — строго из его каталога.

    Пара «провайдер+модель» неполная — ошибка (`400`); `reasoning` независим и может быть
    пустым («как у агента»). Если каталог провайдера пуст, разрешена «своя модель» —
    с предупреждением. Возвращает `(clean, warnings)`.

    Идентификатор модели не подменяется НИКОГДА: только точное совпадение с элементом
    каталога. Регистр, префикс и подстрока не угадываются (`deepseek-v4-pro:0813` ≠
    `deepseek-v4-pro`, `Model-A` ≠ `model-a`): у провайдеров это разные модели, и
    «ближайшая» вместо выбранной — это та самая подмена. Похожие id возвращаются в
    `detail.suggestions`, но применяются только после явного выбора пользователя.
    """
    warnings: list[str] = []
    provider = str(provider or "").strip()
    model = str(model or "").strip()
    clean: dict = {"provider": "", "model": "", "reasoning": _check_reasoning(reasoning)}
    if model and not provider:
        raise ModelError("Сначала выберите провайдера — без него модель не применить.",
                         "bad_request", {"field": "provider"})
    if provider and not model:
        raise ModelError("Выберите модель провайдера «%s»." % provider, "bad_request",
                         {"field": "model", "provider": provider})
    if not provider:
        return clean, warnings
    if len(model) > MAX_MODEL:
        raise ModelError("Слишком длинное имя модели — до %d знаков." % MAX_MODEL, "bad_request",
                         {"field": "model", "length": len(model)})
    rows = _providers(data if data is not None else catalog(cfg))
    row = rows.get(provider)
    if row is None:
        # Провайдер-слаг регистронезависим, но двух «близнецов», отличающихся регистром,
        # угадывать нельзя: неоднозначность — это ошибка, а не выбор первого попавшегося.
        twins = [rows[pid] for pid in rows if pid.lower() == provider.lower()]
        if len(twins) > 1:
            raise ModelError("Провайдер «%s» указан неоднозначно — выберите точное имя."
                             % provider, "bad_request",
                             {"field": "provider", "value": provider,
                              "candidates": sorted(pid for pid in rows
                                                   if pid.lower() == provider.lower())})
        row = twins[0] if twins else None
    if row is None or not row.get("configured"):
        raise ModelError("Провайдер «%s» не настроен в Hermes — выберите другого." % provider,
                         "bad_request", {"field": "provider", "value": provider,
                                         "configured": sorted(rows)})
    known = {m["id"]: m["id"] for m in row.get("models") or []}
    if model in known:
        model = known[model]            # точный id из каталога — как есть
        clean.update({"provider": row["id"], "model": model})
        return clean, warnings
    if not known:
        warnings.append("Каталог провайдера «%s» пуст — модель «%s» задана вручную."
                         % (row.get("label") or provider, model))
        clean.update({"provider": row["id"], "model": model})
        return clean, warnings
    elsewhere = sorted(pid for pid, prow in rows.items()
                       if any(m["id"] == model for m in prow.get("models") or []))
    detail = {"field": "model", "value": model, "provider": row["id"],
              "suggestions": [mid for mid in known if mid.lower() == model.lower()][:5],
              "models": sorted(known)[:50]}
    if elsewhere:
        detail["other_providers"] = elsewhere
    message = "У провайдера «%s» нет модели «%s» — выберите из списка." \
              % (row.get("label") or provider, model)
    if elsewhere:
        message += " Она есть у: %s." % ", ".join(elsewhere)
    raise ModelError(message, "bad_request", detail)


def normalize(value) -> dict:
    """Мягкое чтение реестра: мусор и чужие записи не роняют список комнат.

    Неполная пара (`provider` без `model` или наоборот) не применяется и наружу не
    отдаётся: движку уходят только `provider`+`model` из одного источника, иначе
    получается чужая модель у чужого провайдера. Уровень мышления при этом сохраняется —
    он независим.
    """
    if not isinstance(value, dict):
        return {}
    out: dict[str, dict] = {}
    for raw_profile, raw in value.items():
        profile = _profile(raw_profile)
        if not profile or not isinstance(raw, dict):
            continue
        reasoning = str(raw.get("reasoning") or "").strip().lower()
        item = {"provider": str(raw.get("provider") or "").strip(),
                "model": str(raw.get("model") or "").strip(),
                "reasoning": reasoning if reasoning in EFFORTS else ""}
        if not (item["provider"] and item["model"]):
            item["provider"], item["model"] = "", ""   # неполная пара не действует
        try:
            item["updated_at"] = int(raw.get("updated_at") or 0)
        except (TypeError, ValueError):
            item["updated_at"] = 0
        if not item["provider"] and not item["reasoning"]:
            continue                      # пустая запись = «как у агента»
        out[profile] = item
    return out


def validate_map(cfg, value, members=None, *, data=None) -> dict:
    """Строгая проверка поля комнаты `agent_models` (API): профили — из состава."""
    if value in (None, "", {}):
        return {}
    if not isinstance(value, dict):
        raise ModelError("Модели участников: нужен объект «профиль → настройки»", "bad_request",
                         {"field": "agent_models", "type": type(value).__name__})
    allowed = None
    if members is not None:
        allowed = {_profile(p) for p in members if _profile(p)}
    out: dict[str, dict] = {}
    for raw_profile, raw in value.items():
        profile = _profile(raw_profile)
        if not profile:
            raise ModelError("Модели участников: пустое имя профиля", "bad_request",
                             {"field": "agent_models"})
        if allowed is not None and profile not in allowed:
            raise ModelError("Модель можно задать только участнику комнаты: «%s» в составе нет"
                             % profile, "bad_request",
                             {"field": "agent_models", "profile": profile,
                              "members": sorted(allowed)})
        if raw in (None, ""):
            continue
        if not isinstance(raw, dict):
            raise ModelError("Настройки модели «%s»: нужен объект" % profile, "bad_request",
                             {"field": "agent_models", "profile": profile})
        clean, _warnings = validate(cfg, raw.get("provider"), raw.get("model"),
                                   raw.get("reasoning"), data=data)
        if not clean["provider"] and not clean["reasoning"]:
            continue
        clean["updated_at"] = _int(raw.get("updated_at")) or _now()
        out[profile] = clean
    return out


def _int(value) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


# ---------------------------------------------------------------- доступ к реестру

def _modules():
    try:
        from . import rooms as rooms_mod
    except ImportError:  # pragma: no cover - плоский импорт
        import rooms as rooms_mod  # type: ignore
    return rooms_mod


def settings_of(room) -> dict:
    """Замены комнаты в канонической форме (нет поля — пусто)."""
    return normalize((room or {}).get("agent_models") if isinstance(room, dict) else {})


def participants(room) -> list[tuple[str, str]]:
    """Состав комнаты по порядку: руководитель, критик, исполнители (без дублей)."""
    try:
        from . import agent_notes as notes_mod
    except ImportError:  # pragma: no cover
        import agent_notes as notes_mod  # type: ignore
    return notes_mod.participants(room)


def role_ru(room, profile) -> str:
    name = _profile(profile)
    for item, role in participants(room):
        if item == name:
            return role
    return "исполнитель"


def effective(cfg, room, profile) -> dict:
    """Что применится к следующей карточке участника: замена комнаты поверх дефолта агента.

    Провайдер и модель — всегда одной парой из ОДНОГО источника. Смешать провайдера
    комнаты с моделью агента нельзя: именно так на карточку попадала чужая модель
    (`--model=glm-5.3-flash --provider=anthropic`). Неполная запись комнаты уже
    отброшена `normalize`, здесь страхуемся ещё раз.
    """
    name = _profile(profile)
    settings = settings_of(room).get(name) or {}
    default = agent_default(cfg, name)
    room_pair = bool(settings.get("provider") and settings.get("model"))
    if room_pair:
        provider, model = settings["provider"], settings["model"]
    else:
        provider, model = default["provider"], default["model"]
    reasoning = settings.get("reasoning") or ("" if settings else default["reasoning"])
    source = "room" if (room_pair or settings.get("reasoning")) else "agent"
    return {"provider": provider, "model": model, "reasoning": reasoning or "",
            "source": source, "updated_at": _int(settings.get("updated_at"))}


def model_settings(cfg, room, profile) -> dict:
    value = effective(cfg, room, profile)
    return {"provider": value["provider"], "model": value["model"],
            "reasoning": value["reasoning"], "source": value["source"],
            "updated_at": value["updated_at"]}


def get(cfg, room_id, profile) -> dict:
    """Настройки одного участника: замена, дефолт агента, роль."""
    room = _modules().get_room(cfg, str(room_id)) or {}
    if not room:
        raise ModelError("Комната не найдена", "not_found", {"room": str(room_id)})
    name = _profile(profile)
    if name not in [p for p, _role in participants(room)]:
        raise ModelError("«%s» не в составе комнаты — модель задаётся только участникам" % name,
                         "bad_request", {"field": "profile", "profile": name,
                                         "members": [p for p, _r in participants(room)]})
    settings = settings_of(room)
    return {"profile": name, "role": role_ru(room, name),
            "model_settings": model_settings(cfg, room, name),
            "agent_default": agent_default(cfg, name),
            "room_setting": settings.get(name) or {},
            "agent_models": settings}


def _write(cfg, room_id, profile, item: dict | None) -> dict:
    """Записать (или убрать) замену участника в реестре комнат — файлы профиля не трогаем."""
    room = _modules().get_room(cfg, str(room_id))
    if not room:
        raise ModelError("Комната не найдена", "not_found", {"room": str(room_id)})
    name = _profile(profile)
    if name not in [p for p, _role in participants(room)]:
        raise ModelError("«%s» не в составе комнаты — модель задаётся только участникам" % name,
                         "bad_request", {"field": "profile", "profile": name})
    settings = settings_of(room)
    if item is None:
        settings.pop(name, None)
    else:
        settings[name] = dict(item, updated_at=_now())
    try:
        updated = _modules().update_room(cfg, str(room_id), {"agent_models": settings})
    except ModelError:
        raise
    except Exception as exc:  # noqa: BLE001 - RoomsError и занятый реестр
        code = getattr(exc, "code", "bad_request")
        message = getattr(exc, "message", None) or str(exc) or "Не удалось сохранить модель"
        if exc.__class__.__name__ == "StateUnavailable" or code in ("db_busy", "db_error"):
            raise ModelError(message, "unavailable", getattr(exc, "detail", None) or {}) from exc
        raise ModelError(message, "not_found" if code == "not_found" else "bad_request",
                         getattr(exc, "detail", None) or {}) from exc
    fresh = settings_of(updated if isinstance(updated, dict) else room)
    return {"profile": name, "agent_models": fresh,
            "member": {"profile": name, "role": role_ru(room, name)},
            "room_setting": fresh.get(name) or {}}


def set(cfg, room_id, profile, provider=None, model=None, reasoning=None, *,
        data=None) -> dict:
    """Задать модель/мышление участнику (пустой выбор = вернуть по умолчанию)."""
    provider = str(provider or "").strip()
    model = "" if model is None else str(model).strip()
    if not provider and not model and (reasoning is None or str(reasoning).strip() == ""):
        return clear(cfg, room_id, profile)
    clean, warnings = validate(cfg, provider, model, reasoning,
                               data=data if data is not None else catalog(cfg))
    item = {"provider": clean["provider"], "model": clean["model"],
            "reasoning": clean["reasoning"]}
    if not item["provider"] and not item["reasoning"]:
        result = clear(cfg, room_id, profile)
        result["warnings"] = warnings + list(result.get("warnings") or [])
        return result
    written = _write(cfg, room_id, profile, item)
    written["warnings"] = warnings
    written["cleared"] = False
    return written


def clear(cfg, room_id, profile) -> dict:
    """Вернуть участника к настройкам агента («Вернуть по умолчанию»)."""
    written = _write(cfg, room_id, profile, None)
    written["warnings"] = []
    written["cleared"] = True
    return written


# ---------------------------------------------------------------- журнал комнаты

def _journal(cfg, room_id, text: str, *, kind: str = "model_changed", actor: str = "mikhail") -> None:
    """Запись в журнал комнаты (`journal.jsonl`, id `j_0001…`) — как у ingest."""
    try:
        path = Path(cfg.state_dir) / str(room_id) / "journal.jsonl"
        with sf.lock_for(path):
            events = sf.read_jsonl(path)
            number = 0
            for event in events:
                ident = str(event.get("id") or "")
                if ident.startswith("j_") and ident[2:].isdigit():
                    number = max(number, int(ident[2:]))
            sf.append_jsonl(path, {"id": "j_%04d" % (number + 1), "room": str(room_id),
                                   "at": _now(), "kind": kind, "actor": actor,
                                   "text": _one_line(text)})
    except Exception:  # noqa: BLE001 - журнал вторичен, модель важнее
        pass


def human(provider, model, reasoning, data=None, cfg=None) -> str:
    """«провайдер · модель · мышление» для подписей (без технических id там, где можно)."""
    label = provider or ""
    if cfg is not None and provider:
        label = provider_label(cfg, provider) or provider
    parts = [p for p in (label, model) if p]
    if reasoning:
        parts.append(EFFORT_SHORT.get(str(reasoning).lower(), str(reasoning)))
    return " · ".join(parts)


def label(card: dict, *, data=None, cfg=None) -> str:
    """`model_label` узла дерева: «провайдер · модель · мышление» по факту карточки."""
    if not isinstance(card, dict):
        return ""
    provider = str(card.get("provider_override") or card.get("provider") or "")
    model = str(card.get("model_override") or card.get("model") or "")
    reasoning = str(card.get("reasoning_effort") or "")
    if not provider and not model and not reasoning:
        return ""
    return human(provider, model, reasoning, data=data, cfg=cfg)


# ---------------------------------------------------------------- карточки доски

def _board(cfg, room) -> str:
    return str((room or {}).get("board") or getattr(cfg, "board", "") or "team")


def _connect(cfg, board: str):
    """Чтение доски: sqlite `mode=ro` (писать в БД приложение не имеет права)."""
    import sqlite3
    try:
        db = Path(cfg.kanban_db(board))
    except Exception:  # noqa: BLE001 - доски нет/путь не тот
        return None
    if not db.exists():
        return None
    try:
        con = sqlite3.connect("file:%s?mode=ro" % db.as_posix(), uri=True, timeout=5)
    except sqlite3.Error:
        return None
    con.row_factory = sqlite3.Row
    return con


def _tree_ids(cfg, board: str, root_id: str) -> _new_set:
    try:
        from .engine_write_guard import tree_ids
    except ImportError:  # pragma: no cover
        from engine_write_guard import tree_ids  # type: ignore
    try:
        return _new_set(tree_ids(cfg, board, str(root_id)))
    except Exception:  # noqa: BLE001 - чужая утилита не должна ронять проход
        return {str(root_id)}


def _cards(con, ids: list) -> dict:
    if not ids:
        return {}
    marks = ",".join("?" * len(ids))
    try:
        rows = con.execute(
            "SELECT id, assignee, status, model_override, provider_override, reasoning_effort "
            "FROM tasks WHERE id IN (%s)" % marks, ids).fetchall()
    except Exception:  # noqa: BLE001 - нет колонок (старая схема) — не трогаем карточки
        return {}
    out = {}
    for row in rows:
        out[str(row["id"])] = {
            "id": str(row["id"]), "assignee": str(row["assignee"] or ""),
            "status": str(row["status"] or ""),
            "model": str(row["model_override"] or ""),
            "provider": str(row["provider_override"] or ""),
            "reasoning": str(row["reasoning_effort"] or "")}
    return out


def _roots(cfg, room_id) -> list[str]:
    try:
        from . import registry
    except ImportError:  # pragma: no cover
        import registry  # type: ignore
    try:
        records = registry.list_requests(cfg, str(room_id), limit=None) or []
    except Exception:  # noqa: BLE001 - реестр вторичен для сторожа
        return []
    return [str(rec.get("root_id")) for rec in records if rec.get("root_id")]


def _wanted(cfg, room, profile) -> dict:
    """Что закрепить на карточках: ТОЛЬКО замена комнаты, а не дефолт агента.

    Дефолт агента на карточку не переносится: во-первых, это подмена выбора (карточка
    выглядит как «выбрано в комнате»), во-вторых, пин навсегда перекрыл бы будущую смену
    конфига профиля. Уровень мышления едет отдельно от пары — так же, как в `PUT`.
    """
    settings = settings_of(room).get(_profile(profile)) or {}
    if not (settings.get("provider") and settings.get("model")):
        return {"provider": "", "model": "", "reasoning": settings.get("reasoning") or ""}
    return {"provider": settings["provider"], "model": settings["model"],
            "reasoning": settings.get("reasoning") or ""}


def _needs_update(card: dict, want: dict) -> bool:
    if (card.get("provider") or "") != (want.get("provider") or ""):
        return True
    if (card.get("model") or "") != (want.get("model") or ""):
        return True
    return (card.get("reasoning") or "") != (want.get("reasoning") or "")


def _push(cfg, board: str, cards: list, want: dict) -> tuple[list, list]:
    """Закрепить модель/мышление мостом (одним вызовом на пачку карточек)."""
    if not cards:
        return [], []
    args = ["--db", str(cfg.kanban_db(board)), "--set"]
    for card in cards:
        args += ["--task", card["id"]]
    if want.get("model"):
        args += ["--model", want["model"]]
        if want.get("provider"):
            args += ["--provider", want["provider"]]
    else:
        args.append("--clear-model")
    if want.get("reasoning"):
        args += ["--effort", want["reasoning"]]
    else:
        args.append("--clear-effort")
    try:
        res = _bridge(cfg, args)
    except ModelError as exc:
        return [], [{"id": card["id"], "reason": str(exc.message)} for card in cards]
    done = [tid for tid in (res.get("tasks") or {}) if tid in {c["id"] for c in cards}]
    failed = [{"id": tid, "reason": str(reason)[:200]}
              for tid, reason in (res.get("errors") or {}).items()]
    if not res.get("ok") and not failed:
        reason = str(res.get("error") or "карточки не обновлены")[:200]
        failed = [{"id": card["id"], "reason": reason} for card in cards if card["id"] not in done]
    return done, failed


def push_effort(cfg, board: str, task_ids: list, reasoning: str) -> tuple[list, list]:
    """Поставить/снять уровень мышления карточкам (модель уже задана при создании)."""
    ids = [str(t) for t in task_ids if str(t or "").strip()]
    if not ids:
        return [], []
    args = ["--db", str(cfg.kanban_db(board)), "--set"]
    for task_id in ids:
        args += ["--task", task_id]
    if reasoning:
        args += ["--effort", str(reasoning)]
    else:
        args.append("--clear-effort")
    try:
        res = _bridge(cfg, args)
    except ModelError as exc:
        return [], [{"id": task_id, "reason": str(exc.message)} for task_id in ids]
    done = [tid for tid in (res.get("tasks") or {}) if tid in ids]
    failed = [{"id": tid, "reason": str(reason)[:200]}
              for tid, reason in (res.get("errors") or {}).items()]
    return done, failed


def card_label(cfg, room, profile) -> str:
    """Подпись «провайдер · модель · мышление» для выбранной замены (для журнала)."""
    value = effective(cfg, room, profile)
    return human(value["provider"], value["model"], value["reasoning"], cfg=cfg)


def apply_to_tree(cfg, room_id, root_id, *, board: str | None = None, limit: int = MAX_CARDS) -> dict:
    """Сторож: закрепить модель комнаты на карточках дерева (running/done не трогаем).

    Идемпотентно: карточка обновляется только если её модель/провайдер/уровень мышления
    отличаются от настроек комнаты. Событие `model_override_set` пишет сам движок.
    """
    result = {"updated": [], "skipped": 0, "failed": []}
    room = _modules().get_room(cfg, str(room_id)) or {}
    if not room:
        return result
    settings = settings_of(room)
    if not settings:
        return result
    if getattr(cfg, "mock", False) or getattr(cfg, "demo", False):
        return result
    root = str(root_id or "")
    if not root:
        return result
    use = board or _board(cfg, room)
    con = _connect(cfg, use)
    if con is None:
        return result
    try:
        cards = _cards(con, sorted(_tree_ids(cfg, use, root)))
    finally:
        con.close()
    batches: dict[tuple, list] = {}
    for card in cards.values():
        profile = card["assignee"]
        if not profile or profile not in settings:
            continue
        if card["status"] not in APPLY_STATUSES:
            result["skipped"] += 1
            continue
        want = _wanted(cfg, room, profile)
        if not want["model"] and not want["reasoning"]:
            continue
        if not _needs_update(card, want):
            result["skipped"] += 1
            continue
        batches.setdefault((want["provider"], want["model"], want["reasoning"]), []).append(card)
    budget = max(1, int(limit))
    for want_key, group in batches.items():
        chunk = group[:budget]
        budget -= len(chunk)
        done, failed = _push(cfg, use, chunk, {"provider": want_key[0], "model": want_key[1],
                                               "reasoning": want_key[2]})
        result["updated"] += done
        result["failed"] += failed
        if budget <= 0:
            break
    return result


def apply_profile(cfg, room_id, profile, *, limit: int = MAX_CARDS) -> dict:
    """Применить замену участника ко всем его карточкам комнаты (сразу после PUT).

    Считает и `pending_running` — сколько карточек идёт на старой модели (для кнопки
    «Перезапустить на новой модели»).
    """
    out = {"cards_updated": 0, "pending_running": 0, "cards_failed": []}
    room = _modules().get_room(cfg, str(room_id)) or {}
    if not room:
        return out
    name = _profile(profile)
    # «Вернуть по умолчанию» = снять переопределение с карточек: пустой want чистит пины,
    # иначе на карточке навсегда осталась бы копия сегодняшней настройки агента.
    want = _wanted(cfg, room, name) if effective(cfg, room, name)["source"] == "room" \
        else {"provider": "", "model": "", "reasoning": ""}
    board = _board(cfg, room)
    con = _connect(cfg, board)
    if con is None:
        return out
    ids: _new_set = _new_set()
    try:
        for root in _roots(cfg, room_id):
            ids |= _tree_ids(cfg, board, root)
        cards = _cards(con, sorted(ids))
    finally:
        con.close()
    todo: list = []
    for card in cards.values():
        if card["assignee"] != name:
            continue
        if card["status"] == "running":
            out["pending_running"] += 1
            continue
        if card["status"] not in APPLY_STATUSES:
            continue
        if not _needs_update(card, want):
            continue
        todo.append(card)
    done, failed = _push(cfg, board, todo[:max(1, int(limit))], want)
    out["cards_updated"] = len(done)
    out["cards_failed"] = failed
    return out


def restart_running(cfg, room_id, profile) -> dict:
    """«Перезапустить на новой модели»: закрепить выбор и снять с работы running-карточки.

    Порядок важен: сначала на карточку ставится модель/провайдер/мышление комнаты
    (движок разрешает менять переопределение у `running` — оно применится к следующему
    запуску), и только потом `reassign --reclaim`. Иначе диспетчер подхватил бы карточку
    со старым переопределением или с моделью профиля — то есть «перезапуск на новой
    модели» запускал бы старую.
    """
    result = {"restarted": 0, "cards": [], "warnings": []}
    room = _modules().get_room(cfg, str(room_id)) or {}
    if not room:
        raise ModelError("Комната не найдена", "not_found", {"room": str(room_id)})
    name = _profile(profile)
    if name not in [p for p, _role in participants(room)]:
        raise ModelError("«%s» не в составе комнаты" % name, "bad_request",
                         {"field": "profile", "profile": name})
    if getattr(cfg, "mock", False) or getattr(cfg, "demo", False):
        return result
    board = _board(cfg, room)
    try:
        from . import engine_write as ew
    except ImportError:  # pragma: no cover
        import engine_write as ew  # type: ignore
    # «По умолчанию» (замены комнаты нет) перезапуск не выдумывает модель: пин снимается,
    # и карточка идёт на настройках профиля.
    want = _wanted(cfg, room, name) if effective(cfg, room, name)["source"] == "room" \
        else {"provider": "", "model": "", "reasoning": ""}
    con = _connect(cfg, board)
    if con is None:
        return result
    rows: dict = {}
    try:
        cards = {}
        for root in _roots(cfg, room_id):
            for cid in _tree_ids(cfg, board, root):
                cards.setdefault(cid, root)
        rows = _cards(con, sorted(cards))
    finally:
        con.close()
    for card_id, card in sorted(rows.items()):
        if card["assignee"] != name or card["status"] != "running":
            continue
        _done, failed = _push(cfg, board, [card], want)
        if failed:
            result["warnings"].append("%s: %s" % (card_id, failed[0].get("reason") or
                                                  "модель не закреплена"))
            continue
        try:
            ew.act(cfg, board, cards.get(card_id) or card_id, "reassign",
                   task_id=card_id, profile=name)
        except Exception as exc:  # noqa: BLE001 - одна карточка не валит перезапуск
            result["warnings"].append("%s: %s" % (card_id, str(exc)[:160]))
            continue
        result["cards"].append(card_id)
    result["restarted"] = len(result["cards"])
    return result


# ---------------------------------------------------------------- комната для API

def decorate_room(cfg, detail: dict) -> dict:
    """Дополнить RoomDetail: у участников `model_settings`/`agent_default`, плюс `agent_models_meta`.

    `agent_models_meta` — настройки ВСЕХ участников (включая руководителя и критика, которых
    в `members[]` может не быть): интерфейсу нужна строка на каждого.
    """
    if not isinstance(detail, dict):
        return detail
    room = detail
    detail["agent_models"] = settings_of(room)
    meta: dict[str, dict] = {}
    for profile, role in participants(room):
        meta[profile] = {"profile": profile, "role": role,
                         "model_settings": model_settings(cfg, room, profile),
                         "agent_default": agent_default(cfg, profile)}
    detail["agent_models_meta"] = meta
    members = []
    for member in room.get("members") or []:
        if not isinstance(member, dict):
            members.append(member)
            continue
        item = dict(member)
        profile = _profile(member.get("profile"))
        item["model_settings"] = meta.get(profile, {}).get("model_settings") or \
            model_settings(cfg, room, profile)
        item["agent_default"] = meta.get(profile, {}).get("agent_default") or \
            agent_default(cfg, profile)
        members.append(item)
    detail["members"] = members
    return detail


def member_of(room, cfg, profile) -> dict:
    """Строка участника для ответа API: роль, заметка и модель (замена + дефолт агента)."""
    name = _profile(profile)
    base = {"profile": name, "role": role_ru(room, name), "note": "", "pinned": False,
            "added_at": 0}
    for member in (room or {}).get("members") or []:
        if isinstance(member, dict) and _profile(member.get("profile")) == name:
            base.update({k: member[k] for k in ("role", "note", "pinned", "added_at") if k in member})
            break
    base["model_settings"] = model_settings(cfg, room, name)
    base["agent_default"] = agent_default(cfg, name)
    return base


def announce(cfg, room_id, profile, *, cleared: bool = False) -> str:
    """Записать в журнал комнаты «модель изменена» / «возвращена к настройкам агента»."""
    room = _modules().get_room(cfg, str(room_id)) or {}
    name = _profile(profile)
    role = role_ru(room, name)
    value = effective(cfg, room, name)
    text = human(value["provider"], value["model"], value["reasoning"], cfg=cfg)
    if cleared:
        message = "Модель участника «%s» (%s) возвращена к настройкам агента: %s" % (role, name, text)
    else:
        message = "Модель участника «%s» (%s) изменена: %s" % (role, name, text)
    _journal(cfg, room_id, message)
    return message


def card_settings(cfg, room, profile, *, data=None) -> dict:
    """Что передать движку при создании карточки: модель/провайдер/effort участника."""
    value = effective(cfg, room, profile)
    provider, model, reasoning = value["provider"], value["model"], value["reasoning"]
    if model:
        try:
            clean, _warnings = validate(cfg, provider, model, reasoning,
                                        data=data if data is not None else catalog(cfg))
            provider, model, reasoning = clean["provider"], clean["model"], clean["reasoning"]
        except ModelError:
            pass                          # каталог изменился — работаем с тем, что есть
    return {"provider": provider, "model": model, "reasoning": reasoning}


__all__ = ["EFFORTS", "EFFORT_LABELS", "EFFORT_SHORT", "APPLY_STATUSES", "ModelError",
           "config_path", "bridge_path", "catalog", "provider_label", "agent_default",
           "validate", "validate_map", "normalize", "settings_of", "effective", "model_settings",
           "get", "set", "clear", "label", "human", "apply_to_tree", "apply_profile",
           "restart_running", "decorate_room", "card_settings", "participants"]
