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
    apply_to_room(cfg, room_id) -> {updated, skipped, failed, scanned}   # весь tenant комнаты
    verify_started(cfg, room_id) -> {checked, confirmed, mismatched, fixed}  # «ушло на» + перехват
    apply_profile(cfg, room_id, profile) -> {cards_updated, pending_running, cards_failed}
    restart_running(cfg, room_id, profile) -> {restarted, cards, warnings}
    decorate_room(cfg, detail) -> RoomDetail с model_settings/agent_default у участников
    label(provider, model, reasoning, providers=None) -> «провайдер · модель · мышление»
    sent_of(cfg, room, card) -> {provider, model, reasoning, label, text, confirmed, mismatch}
    member_sent(cfg, room, profile) -> то же по последнему запуску участника
    actual_model(cfg, board, card_id) -> {provider, model, session_id, run_id}  # факт из сессии
    models_block(cfg, room) -> str      # блок «МОДЕЛИ ИСПОЛНИТЕЛЕЙ» для тела запроса

Секреты: значения ключей НИКОГДА не читаются и не печатаются — только факт наличия
провайдера в каталоге Hermes (`configured`).
"""
from __future__ import annotations

import hashlib
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
#: каталог, собранный «из профилей» (Hermes не ответил), повторяем реже: иначе каждый
#: опрос `/api/health` снова поднимал бы мост и снова ждал его таймаут
CACHE_TTL_FAIL = 300
BRIDGE_TIMEOUT = 40                      # секунд: импорт hermes_cli ~1 с, запас на диск
#: проба интерпретатора моста (дефект 11): одного `hermes_cli` мало — падал `ruamel`
PROBE_CODE = "import hermes_cli, ruamel.yaml"
PROBE_TIMEOUT = 25
MAX_CARDS = 120                          # карточек за один проход сторожа
MAX_TENANT_CARDS = 300                   # карточек комнаты за один проход (весь tenant)
#: сколько раз перехватываем одну карточку, ушедшую не на ту модель (защита от петли)
FIXUP_MAX = 2
#: карточку моложе этого возраста не трогаем: воркер ещё поднимается (секунды)
FIXUP_MIN_AGE = 10
#: статусы, в которых карточка «уже пошла» на модель
STARTED_STATUSES = ("running", "review")
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
#: префикс tenant комнаты: по нему видно ВСЕ её карточки, включая созданные руководителем
TENANT_PREFIX = "room:"

#: публичная функция `set` (договорённость §«Интерфейсы») занимает имя — конструктор прячем
_new_set = set

#: каталог: один на процесс, ключ — путь к Hermes (в песочнице меняется редко)
_CACHE: dict = {"key": None, "at": 0.0, "data": None, "ttl": CACHE_TTL}
#: фактические модели сессий: (путь state.db, id сессии) → (время, {provider, model})
_SESSION_CACHE: dict = {}
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


def state_path(cfg, profile) -> Path:
    """Хранилище сессий профиля (`state.db`): где движок фиксирует ФАКТИЧЕСКУЮ модель.

    У `default` — корень Hermes, у остальных — `profiles/<name>/state.db`: та же
    раскладка, что у `config_path`. Файл только читается (`sqlite mode=ro`).
    """
    name = _profile(profile)
    root = _root(cfg)
    if name in ("", "default"):
        return root / "state.db"
    return root / "profiles" / name / "state.db"



def bridge_path(cfg=None) -> Path:
    """Скрипт моста: `<app>/tools/kb_bridge.py` (единственное место с hermes_cli)."""
    app_dir = Path(getattr(cfg, "app_dir", "") or Path(__file__).resolve().parent.parent)
    return app_dir / "tools" / "kb_bridge.py"


def _python(cfg, *, refresh: bool = False, probe=None) -> str:
    """Интерпретатор моста — выбранный по ПРОБЕ, а не по факту существования файла.

    Дефект 11 (20.11): раньше кандидаты перебирались «наличием файла», и вторым шёл
    `sys.executable` — он существует всегда, поэтому мост запускался интерпретатором
    сервера, в котором нет `hermes_cli`/`ruamel`, падал и молча подменял каталог
    «списком из профилей». Теперь кандидат годится только если импортирует
    `hermes_cli` И `ruamel.yaml` (проба — реальный запуск), порядок:

    1. `TEAMROOM_HERMES_PYTHON` (явное указание человека — без перебора),
    2. `installs/*/environments/*/venv/Scripts/python.exe` (окружения Hermes),
    3. `sys.executable` (свой интерпретатор — последний шанс).

    Результат кешируется по каталогу Hermes; исчез — ищем заново (обновление Hermes
    пересоздаёт venv). Та же функция обслуживает ВСЕ операции моста (`--catalog`,
    `--set`, запись мышления): второго пути выбора интерпретатора нет.
    """
    key = str(getattr(cfg, "hermes_home", ""))
    hit = _PYTHON.get(key)
    if not refresh and hit and Path(str(hit[0])).is_file():
        return str(hit[0])
    candidates = python_candidates(cfg)
    check = probe or _probe_python
    chosen = ""
    for candidate in candidates:
        try:
            if candidate and check(candidate):
                chosen = str(candidate)
                break
        except Exception:                    # noqa: BLE001 - одна проба не решает за всех
            continue
    if not chosen:
        # Ни один кандидат не умеет hermes_cli: мост всё равно нужен — отдаём последний
        # (свой интерпретатор), а причину покажет ответ моста и плашка в диалоге.
        chosen = str(candidates[-1] if candidates else (sys.executable or "python"))
    _PYTHON[key] = (str(chosen), time.time())
    return str(chosen)


def python_candidates(cfg) -> list:
    """Кандидаты интерпретатора моста по порядку (дефект 11): явный → venv → свой."""
    explicit = str(os.environ.get("TEAMROOM_HERMES_PYTHON") or "").strip()
    if explicit:
        return [explicit]
    found: list = []
    installs = _root(cfg) / "installs"
    if installs.is_dir():
        try:
            for path in sorted(installs.glob("*/environments/*/venv/Scripts/python.exe"),
                               reverse=True):
                text = str(path)
                if text not in found:
                    found.append(text)
        except OSError:
            pass
    own = str(getattr(cfg, "hermes_python", "") or "").strip()
    if own and own not in found:
        found.append(own)
    me = str(sys.executable or "").strip()
    if me and me not in found:
        found.append(me)
    return found


def _probe_python(python) -> bool:
    """Умеет ли интерпретатор импортировать `hermes_cli` и `ruamel.yaml` (реальный запуск)."""
    text = str(python or "").strip()
    if not text or not Path(text).is_file():
        return False
    env = {k: v for k, v in os.environ.items()
           if not k.startswith("HERMES_KANBAN_") and k != "HERMES_DELEGATED_CHILD_CONTEXT"}
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    try:
        done = proc.run([text, "-B", "-c", PROBE_CODE], stdin=subprocess.DEVNULL,
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                        timeout=PROBE_TIMEOUT, env=env)
    except (OSError, subprocess.SubprocessError, ValueError):
        return False
    return int(getattr(done, "returncode", 1) or 0) == 0


def reset_python_cache() -> None:
    """Забыть выбранный интерпретатор (тесты и «Обновить список» в диалоге)."""
    _PYTHON.clear()



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


def _write_guard(cfg, args: list[str]) -> None:
    """W29-I1: мост в песочнице не пишет в реальную доску — отказ ДО запуска процесса."""
    write = any(str(a) in ("--set", "--clear-model", "--clear-effort") for a in (args or []))
    if not write:
        return
    db = None
    for index, token in enumerate(args):
        if str(token) == "--db" and index + 1 < len(args):
            db = str(args[index + 1])
            break
    try:
        try:
            from . import board_guard as guard
        except ImportError:                                    # pragma: no cover
            import board_guard as guard                        # type: ignore
        found = guard.check(cfg, db=db)
    except ModelError:
        raise
    except Exception:                                          # noqa: BLE001 - защита вторична
        return
    if found:
        raise ModelError(found["message"], guard.CODE, dict(found["detail"]))


def _bridge(cfg, args: list[str]) -> dict:
    """Вызвать мост и разобрать JSON-ответ. Ошибки — `ModelError` с русским текстом."""
    _write_guard(cfg, args)
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
        # Подмена каталога: ключ кэша включает содержимое, иначе два набора тестов/демо с разными
        # каталогами в одном процессе получают чужой каталог из кэша.
        key += "|override|" + hashlib.sha1(
            json.dumps(override, sort_keys=True, ensure_ascii=False, default=str).encode("utf-8")).hexdigest()[:12]
        if not force and _CACHE["data"] is not None and _CACHE["key"] == key \
                and time.time() - float(_CACHE["at"] or 0) < CACHE_TTL:
            data = dict(_CACHE["data"])
            data["cached"] = True
            return data
        data = _norm_catalog(override, "override")
        _CACHE.update({"key": key, "at": time.time(), "data": data})
        return data
    if not force and _CACHE["data"] is not None and _CACHE["key"] == key \
            and time.time() - float(_CACHE["at"] or 0) < float(_CACHE.get("ttl") or CACHE_TTL):
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
    # Неответ Hermes кешируем дольше: иначе каждое открытие «/api/health» и диалога модели
    # снова поднимало бы мост и ждало его таймаут. Полный каталог — как раньше, 60 с.
    ttl = CACHE_TTL if data.get("source") != "profiles" else CACHE_TTL_FAIL
    _CACHE.update({"key": key, "at": time.time(), "data": data, "ttl": ttl})
    return data


def _plural(count: int, one: str, few: str, many: str) -> str:
    num = abs(int(count or 0))
    if num % 10 == 1 and num % 100 != 11:
        return one
    if 2 <= num % 10 <= 4 and num % 100 not in (12, 13, 14):
        return few
    return many


def catalog_line(cfg, *, force: bool = False) -> dict:
    """Строка «Каталог моделей: …» для `/api/health` и «Проверки системы» (дефект 11 п.4).

    Человек должен видеть, откуда список: из Hermes (сколько провайдеров и моделей) или
    «из профилей» — и почему Hermes не ответил. Секретов в строке нет.
    """
    data = catalog(cfg, force=force)
    providers = [p for p in data.get("providers") or [] if p.get("models")]
    total = sum(len(p.get("models") or []) for p in providers)
    source = str(data.get("source") or "")
    reason = " ".join(str(w) for w in (data.get("warnings") or []) if str(w).strip()).strip()
    if source in ("hermes:inventory", "hermes"):
        text = "Каталог моделей: Hermes (%d %s, %d %s)" % (
            len(providers), _plural(len(providers), "провайдер", "провайдера", "провайдеров"),
            total, _plural(total, "модель", "модели", "моделей"))
        ok = True
    else:
        text = "Каталог моделей: из профилей — %s" % (reason or "Hermes не ответил")
        if not providers:
            text = "Каталог моделей: из профилей — список пуст (%s)" % (reason or "нет настроек")
        ok = False
    return {"ok": ok, "text": text, "source": source, "providers": len(providers),
            "models": total, "reason": reason, "warnings": list(data.get("warnings") or [])}


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


#: подписи источника «по умолчанию» (W20 §1.5) — человеческие, без техники
SOURCE_RU = {"profile": "из настроек профиля", "global": "из общих настроек Hermes",
             "none": "не задана"}
GLOBAL_NOTE = "В профиле модель не задана — показана общая настройка Hermes."
NONE_NOTE = "Модель не задана ни в профиле, ни в общих настройках Hermes."


def _config_text(path: Path) -> str:
    """Текст конфига (только чтение); нет файла — пустая строка, а не ошибка."""
    try:
        return path.read_text("utf-8", "replace")
    except OSError:
        return ""


def _defaults_of(text: str) -> dict:
    """`model.provider`, `model.default` (или `model.name`), `agent.reasoning_effort`."""
    reasoning = _scalar(text, "agent", "reasoning_effort")
    if reasoning.lower() in ("false", "none", "disabled"):
        reasoning = "none"
    return {"provider": _scalar(text, "model", "provider"),
            "model": _scalar(text, "model", "default") or _scalar(text, "model", "name"),
            "reasoning": reasoning}


def default_info(cfg, profile) -> dict:
    """Настройки самого агента + ИСТОЧНИК: `profile` | `global` | `none` (W20 §1.5).

    Порядок ровно один и тот же для интерфейса и для движка:

    1. собственный `config.yaml` профиля (`default` — общий конфиг Hermes): если в нём
       есть ПАРА провайдер+модель, действует профиль — `source: "profile"`;
    2. пара в профиле неполная или модели нет — берётся общая настройка Hermes
       (корневой `config.yaml`) ЦЕЛОЙ парой, `source: "global"` и пометка: смешивать
       провайдера одного источника с моделью другого нельзя (так на карточку попадала
       чужая модель);
    3. нет нигде — пустые значения и `source: "none"`: приложение ничего не выдумывает.

    `delegation.reasoning_effort` за «по умолчанию» НЕ выдаётся: это уровень
    делегированных детей, а не самого агента. Файлы открываются только на чтение.
    """
    name = _profile(profile) or "default"
    own = _defaults_of(_config_text(config_path(cfg, name)))
    if own["provider"] and own["model"]:
        return dict(own, source="profile", note="", label=SOURCE_RU["profile"])
    if name in ("", "default"):
        # Общий конфиг — это и есть настройки профиля `default`: подменять нечем.
        return dict(own, source="none", note=NONE_NOTE, label=SOURCE_RU["none"])
    shared = _defaults_of(_config_text(_root(cfg) / "config.yaml"))
    if shared["provider"] and shared["model"]:
        return dict(shared, source="global", note=GLOBAL_NOTE, label=SOURCE_RU["global"])
    return dict(own, source="none", note=NONE_NOTE, label=SOURCE_RU["none"])


def agent_default(cfg, profile) -> dict:
    """Настройки самого агента: `model.provider`, `model.default`, `agent.reasoning_effort`.

    Ровно три поля — форма, на которую опираются карточки комнаты и старые клиенты.
    Источник и пометку отдаёт `default_info` (там же правило «профиль → общие настройки»).
    """
    info = default_info(cfg, profile)
    return {"provider": info["provider"], "model": info["model"],
            "reasoning": info["reasoning"]}


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


def validate(cfg, provider=None, model=None, reasoning=None, *, data=None,
             custom=False) -> tuple[dict, list[str]]:
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
    # «Своя модель» (W29-FIX1 п.2): человек вписал название сам и выбрал пункт
    # «использовать введённое». Модель попадает в реестр комнаты как ЕГО выбор, поэтому
    # не отказываем — но честно предупреждаем: в каталоге её нет, и если это опечатка,
    # задача не запустится. Сторожа это не обходит: пауза сторожа — про расхождение, а не
    # про каталог.
    if custom:
        warnings.append("Модели «%s» нет в каталоге Hermes — сохранена как ваш выбор; "
                        "если название с опечаткой, задача не запустится." % model)
        clean.update({"provider": row["id"], "model": model})
        return clean, warnings
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


def _same_pair(a_provider, a_model, b_provider, b_model) -> bool:
    """Та же пара «провайдер+модель»: модель — по буквам, провайдер — без учёта регистра.

    Регистр модели НЕ нормализуется (`Model-A` и `model-a` у провайдера разные модели),
    иначе «уже используется в комнате» пропускало бы не ту модель (W10-EXACT).
    """
    ap, am = str(a_provider or "").strip().lower(), str(a_model or "").strip()
    bp, bm = str(b_provider or "").strip().lower(), str(b_model or "").strip()
    if not am or am != bm:
        return False
    return not ap or not bp or ap == bp


def used_elsewhere(cfg, provider, model, *, room_map=None, members=None) -> str:
    """Где эта модель уже работает, если её нет в каталоге: «в комнате» / «в профилях» / "".

    Дефект 10 п.3: каталог — не единственный источник правды. Модель, которая уже идёт у
    участника комнаты или стоит в настройках профиля Hermes, принимается с предупреждением
    «нет в каталоге — используется как выбрано», а не отказом: иначе рабочую модель нельзя
    ни подтвердить, ни сохранить рядом с ней другого участника.
    """
    model = str(model or "").strip()
    if not model:
        return ""
    for item in (room_map or {}).values():
        if isinstance(item, dict) and _same_pair(provider, model, item.get("provider"),
                                                 item.get("model")):
            return "в комнате"
    names = [str(name) for name in (members or []) if str(name or "").strip()]
    for name in names:
        try:
            info = default_info(cfg, name)
        except Exception:                    # noqa: BLE001 - конфиг нечитаем: не источник правды
            continue
        if _same_pair(provider, model, info.get("provider"), info.get("model")):
            return "в профилях"
    return ""


def validate_map(cfg, value, members=None, *, data=None, previous=None, warnings=None) -> dict:
    """Строгая проверка поля комнаты `agent_models` (API): профили — из состава.

    Дефект 10 (20.11): проверяется ТОЛЬКО изменённый участник. Если у профиля не менялись
    провайдер и модель (например, человек поменял лишь уровень мышления), каталог заново не
    спрашивается: чужая запись, которой нет в урезанном каталоге, больше не блокирует
    сохранение соседа. Остальные записи идут как есть. Ошибка всегда называет участника.

    `previous` — текущая карта комнаты (`room["agent_models"]`), `warnings` — список, куда
    складываются предупреждения (принимаются, но не блокируют).
    """
    if value in (None, "", {}):
        return {}
    if not isinstance(value, dict):
        raise ModelError("Модели участников: нужен объект «профиль → настройки»", "bad_request",
                         {"field": "agent_models", "type": type(value).__name__})
    allowed = None
    if members is not None:
        allowed = {_profile(p) for p in members if _profile(p)}
    prev_map = previous if isinstance(previous, dict) else {}
    notes = warnings if isinstance(warnings, list) else []
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
        prev = prev_map.get(profile) if isinstance(prev_map.get(profile), dict) else {}
        # Сравнение — ПО БУКВАМ: `Model-A` и `model-a` у провайдера разные модели, и
        # «модель не менялась» не должно съедать точный выбор человека.
        prev_model = str(prev.get("model") or "").strip()
        prev_provider = str(prev.get("provider") or "").strip()
        raw_model = str(raw.get("model") or "").strip()
        raw_provider = str(raw.get("provider") or "").strip()
        same = bool(prev_model) and raw_model == prev_model and (
            not raw_provider or not prev_provider or raw_provider == prev_provider)
        if same:
            # модель не менялась — каталог не спрашиваем: проверяем только мышление
            try:
                item = {"provider": str(prev.get("provider") or ""),
                        "model": str(prev.get("model") or ""),
                        "reasoning": _check_reasoning(raw.get("reasoning"))}
            except ModelError as exc:
                raise _with_profile(profile, exc) from exc
            item["updated_at"] = _int(raw.get("updated_at")) or _int(prev.get("updated_at")) \
                or _now()
            out[profile] = item
            continue
        try:
            clean, _warnings = validate(cfg, raw.get("provider"), raw.get("model"),
                                        raw.get("reasoning"), data=data,
                                        custom=raw.get("custom") is True)
        except ModelError as exc:
            where = ""
            if str(exc.detail.get("field") or "") == "model":
                where = used_elsewhere(cfg, raw.get("provider"), raw.get("model"),
                                       room_map=prev_map,
                                       members=allowed if allowed is not None else None)
            if not where:
                raise _with_profile(profile, exc) from exc
            clean = {"provider": str(raw.get("provider") or "").strip(),
                     "model": str(raw.get("model") or "").strip(),
                     "reasoning": _check_reasoning(raw.get("reasoning"))}
            notes.append("Модели «%s» нет в каталоге Hermes — она уже работает %s, "
                         "используется как выбрано." % (clean["model"], where))
        if not clean["provider"] and not clean["reasoning"]:
            continue
        clean["updated_at"] = _int(raw.get("updated_at")) or _now()
        out[profile] = clean
    return out


def _with_profile(profile, exc) -> ModelError:
    """Ошибка участника: «для «юрист»: …» (дефект 10 п.4 — не чужая ошибка без имени)."""
    message = str(getattr(exc, "message", "") or exc)
    if message.startswith("для «"):
        return exc
    detail = dict(getattr(exc, "detail", None) or {})
    detail.setdefault("profile", str(profile))
    return ModelError("для «%s»: %s" % (profile, message),
                      str(getattr(exc, "code", "bad_request") or "bad_request"), detail)


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


def routine_model(room) -> dict:
    """Выбор комнаты «модель для рутинного сведения» (W29-Q9) или ``{}``.

    Хранится в реестре комнат полем ``routine_model``: ``{provider, model, reasoning}``.
    Пусто/неполная пара — «как у руководителя» (поведение по умолчанию).
    """
    raw = (room or {}).get("routine_model") if isinstance(room, dict) else None
    if not isinstance(raw, dict):
        return {}
    provider = str(raw.get("provider") or "").strip()
    model = str(raw.get("model") or "").strip()
    if not (provider and model):
        return {}
    reasoning = str(raw.get("reasoning") or "").strip().lower()
    return {"provider": provider, "model": model,
            "reasoning": reasoning if reasoning in EFFORTS else "",
            "updated_at": _int(raw.get("updated_at"))}


def routine_effective(cfg, room, data=None) -> dict:
    """Что применится к рутинной карточке: выбор комнаты либо модель руководителя.

    ``source``: ``routine`` — выбрана модель для рутинного сведения; ``lead`` —
    по умолчанию модель руководителя (комнаты или профиля). ``is_custom`` — выбрана
    ли лёгкая модель явно. Оценка экономии — отдельно (`routine_estimate`), здесь
    только факт выбора: цифры без данных не выдумываем.
    """
    room = room if isinstance(room, dict) else {}
    lead = str(room.get("orchestrator") or "").strip()
    base = effective(cfg, room, lead) if lead else {"provider": "", "model": "", "reasoning": ""}
    chosen = routine_model(room)
    if chosen:
        out = dict(chosen)
        out.update({"source": "routine", "is_custom": True,
                    "label": human(chosen["provider"], chosen["model"], chosen.get("reasoning"),
                                   data, cfg=cfg)})
    else:
        out = dict(base)
        out.update({"source": "lead", "is_custom": False,
                    "label": human(base.get("provider"), base.get("model"),
                                   base.get("reasoning"), data, cfg=cfg)})
    out["lead"] = {"provider": base.get("provider") or "", "model": base.get("model") or "",
                   "reasoning": base.get("reasoning") or "", "source": base.get("source") or ""}
    return out


def routine_estimate(cfg, room) -> dict:
    """Оценка экономии рутинной модели по РЕАЛЬНЫМ сессиям (`state.db`, read-only).

    Сравниваем среднюю цену вызова модели руководителя и выбранной рутинной модели.
    Нет данных по одной из них — ``savings_pct=None`` и честный текст, без выдуманных
    процентов. Возвращает ``{savings_pct, lead_usd, routine_usd, text, source}``.
    """
    room = room if isinstance(room, dict) else {}
    chosen = routine_model(room)
    lead_profile = str(room.get("orchestrator") or "").strip()
    lead = effective(cfg, room, lead_profile) if lead_profile else {}
    out = {"savings_pct": None, "lead_usd": None, "routine_usd": None,
           "source": "none", "text": ""}
    if not chosen:
        out["text"] = "Рутинные карточки идут на модели руководителя — экономия не настроена."
        return out
    try:
        try:
            from . import usage as usage_mod
        except ImportError:  # pragma: no cover - плоский импорт
            import usage as usage_mod  # type: ignore
        rates = usage_mod.model_rates(getattr(cfg, "hermes_home", None)
                                      or usage_mod._default_state_dir()) or {}
    except Exception:  # noqa: BLE001 - оценка не должна мешать странице комнаты
        rates = {}
    lead_rate_row = rates.get(str(lead.get("model") or "")) or {}
    routine_rate_row = rates.get(chosen["model"]) or {}
    lead_usd = lead_rate_row.get("usd_per_call")
    routine_usd = routine_rate_row.get("usd_per_call")
    if lead_usd and routine_usd:
        out.update({"lead_usd": lead_usd, "routine_usd": routine_usd, "source": "state.db"})
        delta = (1.0 - float(routine_usd) / float(lead_usd)) * 100.0
        out["savings_pct"] = int(round(delta))
        if delta >= 0:
            out["text"] = ("≈ %d%% дешевле за вызов по вашим сессиям ($%.3f → $%.3f)."
                           % (out["savings_pct"], float(lead_usd), float(routine_usd)))
        else:
            out["text"] = ("Дороже на %d%% за вызов по вашим сессиям ($%.3f → $%.3f) — "
                           "выбрана не более лёгкая модель."
                           % (abs(out["savings_pct"]), float(lead_usd), float(routine_usd)))
        return out
    out["text"] = ("Оценка появится после первых запусков: нужны сессии обеих моделей "
                   "в state.db — цифры не выдумываем.")
    return out


def model_settings(cfg, room, profile) -> dict:
    """Что применится к следующей карточке: `source` = `room` (выбрано) или `agent`.

    `default_source`/`default_note` — откуда взялось «по умолчанию» (W20 §1.5):
    из профиля агента, из общих настроек Hermes или не задано нигде. Интерфейсу это
    нужно, чтобы честно написать «по умолчанию (из настроек агента): …» и пометить
    значения, взятые из общих настроек.
    """
    value = effective(cfg, room, profile)
    info = default_info(cfg, profile)
    return {"provider": value["provider"], "model": value["model"],
            "reasoning": value["reasoning"], "source": value["source"],
            "updated_at": value["updated_at"],
            "default_source": info["source"], "default_note": info["note"]}


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
            "agent_default_info": default_info(cfg, name),
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
    notes: list = []
    try:
        updated = _modules().update_room(cfg, str(room_id), {"agent_models": settings},
                                         warnings=notes)
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
            "room_setting": fresh.get(name) or {}, "warnings": notes}


def set(cfg, room_id, profile, provider=None, model=None, reasoning=None, *,
        data=None, custom=False) -> dict:
    """Задать модель/мышление участнику (пустой выбор = вернуть по умолчанию).

    Дефект 10 п.3: если модели нет в каталоге, но она уже работает в этой комнате (или
    стоит в настройках профиля Hermes), выбор принимается с предупреждением — каталог не
    единственный источник правды. Ошибка называет участника.
    `custom=True` — человек выбрал пункт «своя модель»: вписанное имя принимается с
    предупреждением даже если такой модели в каталоге нет (W29-FIX1 п.2).
    """
    provider = str(provider or "").strip()
    model = "" if model is None else str(model).strip()
    if not provider and not model and (reasoning is None or str(reasoning).strip() == ""):
        return clear(cfg, room_id, profile)
    warnings: list = []
    try:
        clean, warnings = validate(cfg, provider, model, reasoning,
                                   data=data if data is not None else catalog(cfg),
                                   custom=bool(custom))
    except ModelError as exc:
        room = _modules().get_room(cfg, str(room_id)) or {}
        members = [p for p, _role in participants(room)] if room else []
        where = used_elsewhere(cfg, provider, model,
                              room_map=settings_of(room) if room else {}, members=members)
        if not where or str(exc.detail.get("field") or "") != "model":
            raise _with_profile(_profile(profile), exc) from exc
        clean = {"provider": provider, "model": model,
                 "reasoning": _check_reasoning(reasoning)}
        warnings = ["Модели «%s» нет в каталоге Hermes — она уже работает %s, "
                    "используется как выбрано." % (model, where)]
    item = {"provider": clean["provider"], "model": clean["model"],
            "reasoning": clean["reasoning"]}
    if custom:
        # Пометку «своя модель» несём через запись в реестр: `validate_map` при
        # сохранении комнаты проверяет запись заново, и без пометки она бы отклонилась
        # (в реестре её не остаётся — `normalize` хранит только провайдер/модель/мышление).
        item["custom"] = True
    if not item["provider"] and not item["reasoning"]:
        result = clear(cfg, room_id, profile)
        result["warnings"] = warnings + list(result.get("warnings") or [])
        return result
    written = _write(cfg, room_id, profile, item)
    written["warnings"] = _uniq(warnings + list(written.get("warnings") or []))
    written["cleared"] = False
    return written


def _uniq(items) -> list:
    """Одинаковые предупреждения — один раз (их добавляют два слоя проверки)."""
    out: list = []
    for item in items or []:
        text = str(item or "").strip()
        if text and text not in out:
            out.append(text)
    return out


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


def push_model(cfg, board: str, cards: list, want: dict) -> tuple[list, list]:
    """Поставить (или снять) модель карточкам штатным мостом — публичная обёртка.

    Нужна сторожу моделей (`server/model_guard.py`, WAVE23 §2.3): запись идёт тем же
    единственным путём — `tools/kb_bridge.py` → `kanban_db.set_model_override`, а не
    своим SQL. `want` без модели снимает переопределение (`--clear-model`).
    """
    return _push(cfg, board, cards, want)


def pin_cards(cfg, room, card_ids, *, board: str | None = None) -> dict:
    """Сразу записать выбор комнаты конкретным карточкам (W28-C1 п.2).

    Нужно там, где карточку создаёт или переназначает САМО приложение (`_new_request`,
    «Повторить», переназначение корня на руководителя): окна «карточка ещё без модели, а
    диспетчер уже смотрит» быть не должно. Модель берётся только из выбора комнаты —
    модель профиля на карточку не переносится (это и был баг $2,45).
    """
    out = {"updated": [], "failed": []}
    room = room if isinstance(room, dict) else {}
    ids = [str(i) for i in (card_ids or []) if str(i or "").strip()]
    if not ids or not room.get("id"):
        return out
    use = board or _board(cfg, room)
    con = _connect(cfg, use)
    if con is None:
        return out
    try:
        cards = _cards(con, ids)
    finally:
        con.close()
    groups: dict[tuple, list] = {}
    for card in cards.values():
        want = _wanted(cfg, room, card["assignee"])
        if not want["model"] and not want["reasoning"]:
            continue                      # в комнате ничего не выбрано — флаги не нужны
        if not _needs_update(card, want):
            continue
        groups.setdefault((want["provider"], want["model"], want["reasoning"]), []).append(card)
    for key, group in groups.items():
        done, failed = _push(cfg, use, group, {"provider": key[0], "model": key[1],
                                               "reasoning": key[2]})
        out["updated"] += done
        out["failed"] += failed
    return out


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
    cards = card_map(cfg, room, board=board)      # tenant комнаты, а не только деревья
    todo: list = []
    running: list = []
    for card in cards.values():
        if card["assignee"] != name:
            continue
        if card["status"] == "running":
            out["pending_running"] += 1
            # B-26: идущая карточка не перезапускается — новая модель применится со следующей.
            running.append(card)
            continue
        if card["status"] not in APPLY_STATUSES:
            continue
        if not _needs_update(card, want):
            continue
        todo.append(card)
    done, failed = _push(cfg, board, todo[:max(1, int(limit))], want)
    out["cards_updated"] = len(done)
    out["cards_failed"] = failed
    # B-26: запоминаем идущие карточки — их работу смена модели не прерывает.
    out["pending"] = mark_pending(cfg, str(room_id), name, running, model=str(want.get("model") or ""))
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
    # «По умолчанию» (замены комнаты нет) перезапускать нечего: модель не менялась, а
    # снятие с работы идущей карточки — потеря работы, а не исправление.
    if effective(cfg, room, name)["source"] != "room":
        result["warnings"].append("В комнате модель этого участника не менялась — "
                                  "перезапускать нечего.")
        return result
    want = _wanted(cfg, room, name)
    rows = card_map(cfg, room, board=board)
    for card_id, card in sorted(rows.items()):
        if card["assignee"] != name or card["status"] != "running":
            continue
        _done, failed = _push(cfg, board, [card], want)
        if failed:
            result["warnings"].append("%s: %s" % (card_id, failed[0].get("reason") or
                                                  "модель не закреплена"))
            continue
        try:
            ew.act(cfg, board, card_id, "reassign", task_id=card_id, profile=name)
        except Exception as exc:  # noqa: BLE001 - одна карточка не валит перезапуск
            result["warnings"].append("%s: %s" % (card_id, str(exc)[:160]))
            continue
        result["cards"].append(card_id)
        clear_pending(cfg, str(room_id), card_id)   # перезапустили явно — ожидание снято
    result["restarted"] = len(result["cards"])
    return result


# ---------------------------------------------------------------- «ушло на»

def _tenant(room) -> str:
    """Tenant комнаты: всё, что создано в её запросах, живёт под ним."""
    room = room or {}
    return str(room.get("tenant") or ("%s%s" % (TENANT_PREFIX, room.get("id") or ""))).strip()


def _session_id(value) -> str:
    """id сессии воркера из metadata прогона (`worker_session_id`)."""
    if isinstance(value, str):
        try:
            return _session_id(json.loads(value))
        except ValueError:
            return ""
    if not isinstance(value, dict):
        return ""
    for key in ("worker_session_id", "session_id", "session"):
        text = str(value.get(key) or "").strip()
        if text:
            return text
    return ""


def _session_model(cfg, profile, session_id) -> dict:
    """ФАКТИЧЕСКАЯ модель запуска: `sessions.model` + `billing_provider` в `state.db`.

    Движок выбирает модель в момент допуска запуска (`gateway/session_kanban.py`: политика
    берётся из `tasks.model_override/provider_override/reasoning_effort`) и записывает её
    в сессию профиля. Поэтому это единственное надёжное место, где видно, на чём карточка
    реально поехала: `task_runs.metadata` хранит только id этой сессии, а лог воркера —
    свободный текст. Читаем `mode=ro`, ничего не пишем.
    """
    sid = str(session_id or "").strip()
    if not sid:
        return {}
    path = state_path(cfg, profile)
    key = (str(path), sid)
    now = time.time()
    hit = _SESSION_CACHE.get(key)
    if hit and now - float(hit[0] or 0) < 120:
        return dict(hit[1])
    out: dict = {}
    try:
        import sqlite3
        if path.exists():
            con = sqlite3.connect("file:%s?mode=ro" % path.as_posix(), uri=True, timeout=5)
            try:
                con.row_factory = sqlite3.Row
                # `estimated_cost_usd` есть не во всех версиях Hermes — читаем только
                # существующие колонки (W23: стоимость последнего запуска нужна «ушло на»)
                cols = {str(r[1]) for r in con.execute("PRAGMA table_info(sessions)")}
                wanted = [c for c in ("model", "billing_provider", "estimated_cost_usd", "api_call_count")
                          if c in cols]
                row = con.execute("SELECT %s FROM sessions WHERE id = ?"
                                  % ", ".join(wanted), (sid,)).fetchone() if wanted else None
                if row is not None:
                    data = dict(row)
                    out = {"model": str(data.get("model") or "").strip(),
                           "provider": str(data.get("billing_provider") or "").strip()}
                    # W28-C2: число вызовов модели в этой попытке — «вызовов 3».
                    # 0 — законный ответ (попытку убили до первого запроса), отсутствие
                    # колонки — «неизвестно» (None).
                    if data.get("api_call_count") not in (None, ""):
                        try:
                            out["calls"] = int(data["api_call_count"])
                        except (TypeError, ValueError):
                            out["calls"] = None
                    # Сессия есть, а стоимость не записана (локальная модель/старая
                    # версия Hermes) — это 0, а не «неизвестно»: иначе интерфейс не
                    # отличит «не запускалась» от «запускалась бесплатно».
                    out["cost_usd"] = 0.0
                    if data.get("estimated_cost_usd") not in (None, ""):
                        try:
                            out["cost_usd"] = round(float(data["estimated_cost_usd"]), 4)
                        except (TypeError, ValueError):
                            pass
            finally:
                con.close()
    except Exception:  # noqa: BLE001 - нет файла/схемы: «неизвестно», а не падение
        out = {}
    if len(_SESSION_CACHE) > 400:
        _SESSION_CACHE.clear()
    _SESSION_CACHE[key] = (now, dict(out))
    return out


def _run_session(con, card_id) -> tuple[dict, str]:
    """Последний прогон карточки и id его сессии (из metadata, иначе из события допуска)."""
    run: dict = {}
    try:
        row = con.execute("SELECT id, profile, status, started_at, ended_at, metadata "
                          "FROM task_runs WHERE task_id = ? ORDER BY id DESC LIMIT 1",
                          (str(card_id),)).fetchone()
        run = dict(row) if row is not None else {}
    except Exception:  # noqa: BLE001 - старая схема
        run = {}
    sid = _session_id(run.get("metadata"))
    if not sid:
        try:
            row = con.execute("SELECT payload FROM task_events WHERE task_id = ? "
                              "AND kind = 'owner_admitted' ORDER BY id DESC LIMIT 1",
                              (str(card_id),)).fetchone()
            sid = _session_id((row["payload"] if row is not None else "") or "")
        except Exception:  # noqa: BLE001
            sid = ""
    return run, sid


def _run_session_from_events(con, card_id, run_id) -> str:
    """Id сессии КОНКРЕТНОГО прогона из события ``owner_admitted`` (W28-C2).

    У убитой контролем моделей попытки `task_runs.metadata` содержит только pid —
    id сессии остаётся в событии допуска, а у события есть `run_id`. Без этой
    привязки подпись попытки 1 показывала бы модель попытки 2.
    """
    if run_id in (None, ""):
        return ""
    try:
        row = con.execute("SELECT payload FROM task_events WHERE task_id = ? AND run_id = ? "
                          "AND kind = 'owner_admitted' ORDER BY id DESC LIMIT 1",
                          (str(card_id), int(run_id))).fetchone()
    except Exception:  # noqa: BLE001 - старая схема без run_id у событий
        return ""
    return _session_id((row["payload"] if row is not None else "") or "")


def actual_model(cfg, board: str, card_id: str, *, profile: str = "", run: dict | None = None,
                 con=None) -> dict:
    """Что реально ушло у карточки: `{provider, model, session_id, run_id, started_at}`.

    Порядок: подтверждение из сессии воркера → пусто («неизвестно», не выдумываем).
    `run` можно передать готовым (дерево уже прочитало прогоны) — тогда БД не трогаем.
    """
    out = {"provider": "", "model": "", "session_id": "", "run_id": None, "started_at": None,
           "source": "", "cost_usd": None, "calls": None}
    close = False
    if con is None and card_id:
        con = _connect(cfg, board)
        close = con is not None
    try:
        if run is None:
            if con is None:
                return out
            run, sid = _run_session(con, card_id)
        else:
            sid = _session_id((run or {}).get("metadata"))
            if not sid and con is not None:
                # W28-C2: сессия ИМЕННО этого прогона (у убитой попытки в metadata
                # только pid). Раньше подставлялась сессия последнего прогона, и
                # подпись «ушло на» у попытки 1 показывала модель попытки 2.
                sid = _run_session_from_events(con, card_id, (run or {}).get("id"))
        run = run or {}
        out["run_id"] = _int(run.get("id")) or None
        out["started_at"] = _int(run.get("started_at")) or None
        out["session_id"] = sid
        row = _session_model(cfg, run.get("profile") or profile, sid)
        if row.get("model"):
            out["model"] = row["model"]
            out["provider"] = row.get("provider") or ""
            out["cost_usd"] = row.get("cost_usd")
            out["calls"] = row.get("calls")
            out["source"] = "session"
    finally:
        if close:
            con.close()
    return out


def _pin(card) -> dict:
    """Переопределение, записанное в САМОЙ карточке (ключи дерева и ключи БД)."""
    card = card if isinstance(card, dict) else {}
    return {"provider": str(card.get("provider_override") or card.get("provider") or ""),
            "model": str(card.get("model_override") or card.get("model") or ""),
            "reasoning": str(card.get("reasoning_effort") or card.get("reasoning") or "")}


def _short_label(provider: str, model: str, reasoning: str) -> str:
    """«провайдер · модель · мышление» теми же id, что в подписи дерева."""
    parts = [p for p in (provider, model) if p]
    if reasoning:
        parts.append(EFFORT_SHORT.get(str(reasoning).lower(), str(reasoning)))
    return " · ".join(parts)


def sent_of(cfg, room, card, *, board: str | None = None, run: dict | None = None) -> dict:
    """«Ушло на: …» карточки: факт из запуска (если есть) + запись карточки + расхождение.

    Возвращает ``{}``, когда сказать нечего (нет ни выбора, ни переопределения).
    `mismatch` — расходится с выбором комнаты (или настройкой агента, если выбора нет).
    """
    if not isinstance(card, dict):
        return {}
    pin = _pin(card)
    profile = _profile(card.get("assignee"))
    want = effective(cfg, room, profile) if profile else {"provider": "", "model": "", "reasoning": ""}
    run = run if run is not None else None
    actual = {"model": "", "provider": "", "run_id": None, "started_at": None, "confirmed": False}
    if card.get("id") and (board is not None or run is not None):
        got = actual_model(cfg, board or "", str(card.get("id")), profile=profile, run=run)
        actual = {"model": got.get("model") or "", "provider": got.get("provider") or "",
                  "run_id": got.get("run_id"), "started_at": got.get("started_at"),
                  "cost_usd": got.get("cost_usd"),
                  "confirmed": bool(got.get("model"))}
    provider = actual["provider"] or pin["provider"]
    model = actual["model"] or pin["model"]
    reasoning = pin["reasoning"]
    if actual["model"] and not actual["provider"]:
        # W28-C2: провайдер подписывается ФАКТИЧЕСКИЙ (`sessions.billing_provider`).
        # Когда он неизвестен, а модель известна, провайдер из карточки не
        # подставляем: «ollama-cloud · claude-sonnet-5-5» — ложная пара, у которой
        # провайдер от выбора комнаты, а модель от чужого запуска.
        provider = ""
    if not (provider or model or reasoning or want.get("model")):
        return {}
    expected = {"provider": want.get("provider") or "", "model": want.get("model") or "",
                "reasoning": want.get("reasoning") or ""}
    mismatch = False
    if expected["model"] and model:
        mismatch = model != expected["model"] or (bool(provider) and bool(expected["provider"])
                                                  and provider != expected["provider"])
    if not mismatch and expected["reasoning"] and reasoning:
        mismatch = reasoning != expected["reasoning"]
    label = _short_label(provider, model, reasoning)
    return {"provider": provider, "model": model, "reasoning": reasoning,
            "label": label, "text": ("ушло на: " + label) if label else "",
            "confirmed": bool(actual["confirmed"]), "run_id": actual["run_id"],
            "cost_usd": actual.get("cost_usd"),
            "at": actual["started_at"], "expected": expected, "pinned": pin,
            "mismatch": bool(mismatch and label),
            "note": "расходится с выбором комнаты" if (mismatch and label) else ""}


def _queued_right(con, tenant: str, profile: str, *, after: int, expected: dict,
                  exclude: str = "") -> dict:
    """Карточка в очереди участника, которой уже записана ВЕРНАЯ модель (W19 B-29).

    Нужна, чтобы не пугать предупреждением, когда последний запуск был ДО смены
    модели, а следующая карточка (уже созданная) пойдёт на выбранной модели.
    Берём только карточки «ещё не поехали» (`APPLY_STATUSES`), созданные не раньше
    `after`, с точной парой провайдер+модель из выбора комнаты.
    """
    if not after or not (expected or {}).get("model"):
        return {}
    marks = ",".join("?" for _ in APPLY_STATUSES)
    sql = ("SELECT id, status, created_at, model_override, provider_override FROM tasks "
           "WHERE tenant = ? AND assignee = ? AND status IN (%s) " % marks)
    args: list = [tenant, profile, *APPLY_STATUSES]
    if exclude:
        sql += "AND id <> ? "
        args.append(str(exclude))
    sql += "ORDER BY created_at DESC LIMIT 20"
    try:
        rows = con.execute(sql, tuple(args)).fetchall()
    except Exception:  # noqa: BLE001 - старая схема без колонок модели
        return {}
    want_model = str(expected.get("model") or "")
    want_provider = str(expected.get("provider") or "")
    for row in rows:
        item = dict(row)
        if _int(item.get("created_at")) < after:
            continue
        if str(item.get("model_override") or "") != want_model:
            continue
        if want_provider and str(item.get("provider_override") or "") \
                and str(item.get("provider_override") or "") != want_provider:
            continue
        return {"id": str(item.get("id") or ""), "status": str(item.get("status") or ""),
                "created_at": _int(item.get("created_at"))}
    return {}


def member_sent(cfg, room, profile, *, board: str | None = None) -> dict:
    """«Ушло на: …» участника по ЕГО последнему запуску в комнате (для вкладки «Команда»).

    W19 B-29: расхождение — только если карточка РЕАЛЬНО ушла на другую модель ПОСЛЕ
    последней смены модели участника. Если смена позже последнего запуска или в
    очереди уже есть карточка с верной моделью, это не тревога, а ожидание:
    ``pending=true`` и текст «Новая модель применится со следующей задачи».
    """
    name = _profile(profile)
    if not name:
        return {}
    use = board or _board(cfg, room)
    tenant = _tenant(room)
    con = _connect(cfg, use)
    if con is None or not tenant:
        return {}
    try:
        try:
            row = con.execute(
                "SELECT t.id AS id, t.status AS status, t.assignee AS assignee, "
                "t.model_override AS model_override, t.provider_override AS provider_override, "
                "t.reasoning_effort AS reasoning_effort, "
                "t.created_at AS card_created_at, "
                "r.started_at AS run_started_at, r.id AS run_ref "
                "FROM task_runs r JOIN tasks t ON t.id = r.task_id "
                "WHERE t.tenant = ? AND t.assignee = ? ORDER BY r.id DESC LIMIT 1",
                (tenant, name)).fetchone()
        except Exception:  # noqa: BLE001 - старая схема без model_override
            row = None
        if row is None:
            return {}
        card = dict(row)
        sent = sent_of(cfg, room, card, board=use)
        if not sent:
            return {}
        changed_at = _int((settings_of(room).get(name) or {}).get("updated_at"))
        run_at = _int(card.get("run_started_at")) or _int(sent.get("at"))
        queued: dict = {}
        pending = False
        if sent.get("mismatch"):
            # 1) смена модели была ПОСЛЕ этого запуска — карточка физически не могла
            #    пойти на новую модель; это ожидание, а не тревога.
            if changed_at and run_at and run_at < changed_at:
                pending = True
            else:
                # 2) в очереди уже стоит карточка с верной моделью, созданная после
                #    этого запуска (и/или после смены) — следующая работа пойдёт верно.
                after = max(_int(card.get("card_created_at")), changed_at or 0)
                queued = _queued_right(con, tenant, name, after=after,
                                       expected=sent.get("expected") or {},
                                       exclude=str(card.get("id") or ""))
                pending = bool(queued)
            if pending:
                sent["mismatch"] = False
                sent["note"] = ""
        sent["changed_at"] = changed_at
        sent["run_at"] = run_at or None
        sent["pending"] = bool(pending)
        sent["pending_text"] = ("Новая модель применится со следующей задачи"
                                if pending else "")
        if queued:
            sent["queue"] = queued
        return sent
    finally:
        con.close()


def models_block(cfg, room) -> str:
    """Блок «МОДЕЛИ ИСПОЛНИТЕЛЕЙ» в тело запроса: что выбрать и как создать карточку.

    Нужен руководителю: карточки детей он создаёт сам через `hermes kanban create`, и
    если не передать `--model/--provider`, движок возьмёт модель профиля — ровно та
    ошибка, из-за которой работа уходила на `glm-5.3-flash` вместо выбранной модели
    комнаты. Уровень мышления в CLI флага не имеет — его ставит приложение по карточке
    (`kanban_db.set_reasoning_effort`), поэтому руководителю об этом думать не нужно.
    """
    room = room or {}
    selected = settings_of(room)
    lines: list[str] = []
    example = ["--model=<модель>", "--provider=<провайдер>"]
    for profile, role in participants(room):
        role_ru = ROLE_RU.get(role, role)
        item = selected.get(profile) or {}
        flags = _flags_for(cfg, room, profile) if item.get("model") else []
        if flags:
            label = _short_label(item.get("provider") or "", item["model"],
                                 item.get("reasoning") or "")
            lines.append("- %s (%s) — %s → при создании карточки: %s"
                         % (profile, role_ru, label, " ".join(flags)))
            if example == ["--model=<модель>", "--provider=<провайдер>"]:
                example = list(flags)
        else:
            value = effective(cfg, room, profile)
            own = _short_label(value.get("provider") or "", value.get("model") or "",
                               value.get("reasoning") or "")
            lines.append("- %s (%s) — модель из настроек агента%s (в комнате не выбиралась) → "
                         "флаги не нужны"
                         % (profile, role_ru, (": " + own) if own else ""))
    if not lines:
        return ""
    board = str(room.get("board") or "") or "<доска>"
    tenant = _tenant(room)
    return "\n".join([
        "<<<МОДЕЛИ ИСПОЛНИТЕЛЕЙ  (важно: модель карточки задаёт ПРИЛОЖЕНИЕ — флаги "
        "--model/--provider при создании карточек НЕ передавай)",
        "Что действует в комнате сейчас (для сведения):",
        *lines,
        "Правило (W23 §2.2): создавай карточку без флагов модели —",
        "  hermes kanban --board=%s create \"<название>\" --assignee=<профиль> "
        "--parent=<id родителя> --tenant=%s" % (board, tenant),
        "Зачем: приложение само сверяет модель карточки с ожидаемой моделью исполнителя "
        "(переопределение комнаты или настройки его профиля Hermes). Если модель не совпадает, "
        "карточку исправят ДО запуска, а запущенную на чужой модели остановят и перезапустят — "
        "поверх твоего --model всё равно поедет правильная.",
        "Уровень мышления тоже ставить не нужно: приложение проставляет его по карточке "
        "(у CLI такого флага нет).",
        "Проверить, что реально ушло: в карточке дерева строка «ушло на: провайдер · модель · "
        "уровень».",
        "МОДЕЛИ ИСПОЛНИТЕЛЕЙ>>>",
    ])


def _flags_for(cfg, room, profile) -> list[str]:
    value = effective(cfg, room, profile)
    if not value.get("model"):
        return []
    out = ["--model=%s" % value["model"]]
    if value.get("provider"):
        out.append("--provider=%s" % value["provider"])
    return out


# ---------------------------------------------------------------- карточки комнаты

def tenant_cards(cfg, board: str, tenant: str, *, limit: int = MAX_TENANT_CARDS) -> dict:
    """Все карточки комнаты по tenant (mode=ro): id → {assignee, status, model, …}.

    Это и есть ответ на главную причину бага: сторож раньше смотрел только деревья
    запросов из реестра, а руководитель создаёт карточки сам (`created_by=default`,
    без связи с родителем) — такие карточки не попадали ни в одно дерево и уезжали на
    модель профиля. Tenant у комнаты один, поэтому берём по нему.
    """
    out: dict = {}
    tenant = str(tenant or "").strip()
    if not tenant:
        return out
    con = _connect(cfg, board)
    if con is None:
        return out
    try:
        try:
            rows = con.execute(
                "SELECT id, assignee, status, title, created_at, started_at, "
                "model_override, provider_override, reasoning_effort FROM tasks "
                "WHERE tenant = ? ORDER BY created_at DESC LIMIT ?",
                (tenant, max(1, int(limit)))).fetchall()
        except Exception:  # noqa: BLE001 - нет колонок модели (старая схема)
            return out
    finally:
        con.close()
    for row in rows:
        out[str(row["id"])] = {
            "id": str(row["id"]), "assignee": str(row["assignee"] or ""),
            "status": str(row["status"] or ""), "title": str(row["title"] or ""),
            "created_at": _int(row["created_at"]), "started_at": _int(row["started_at"]),
            "model": str(row["model_override"] or ""),
            "provider": str(row["provider_override"] or ""),
            "reasoning": str(row["reasoning_effort"] or "")}
    return out


def card_map(cfg, room, *, board: str | None = None) -> dict:
    """Карточки комнаты для правок модели: tenant, а если он пуст — деревья реестра."""
    use = board or _board(cfg, room)
    cards = tenant_cards(cfg, use, _tenant(room))
    if cards:
        return cards
    out: dict = {}
    con = _connect(cfg, use)
    if con is None:
        return out
    try:
        ids: _new_set = _new_set()
        for root in _roots(cfg, (room or {}).get("id")):
            ids |= _tree_ids(cfg, use, root)
        out = _cards(con, sorted(ids))
    finally:
        con.close()
    return out


def apply_to_room(cfg, room_id, *, board: str | None = None, limit: int = MAX_CARDS) -> dict:
    """Закрепить выбор комнаты на ВСЕХ её карточках, которым ещё предстоит запуск.

    Дешёвый и быстрый перехват: один проход по tenant комнаты за тик сторожа (5 с).
    Берутся только статусы «ещё не запущена» (`APPLY_STATUSES`); идущие и завершённые
    не трогаются — для них отдельная проверка `verify_started`.
    """
    result = {"updated": [], "skipped": 0, "failed": [], "scanned": 0}
    room = _modules().get_room(cfg, str(room_id)) or {}
    if not room:
        return result
    settings = settings_of(room)
    if not settings:
        return result
    if getattr(cfg, "mock", False) or getattr(cfg, "demo", False):
        return result
    use = board or _board(cfg, room)
    cards = card_map(cfg, room, board=use)
    result["scanned"] = len(cards)
    todo: dict = {}
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
        todo.setdefault((want["provider"], want["model"], want["reasoning"]), []).append(card)
    budget = max(1, int(limit))
    for want_key, group in todo.items():
        chunk = group[:budget]
        budget -= len(chunk)
        done, failed = _push(cfg, use, chunk, {"provider": want_key[0], "model": want_key[1],
                                               "reasoning": want_key[2]})
        result["updated"] += done
        result["failed"] += failed
        if budget <= 0:
            break
    return result


# ---------------------------------------------------------------- проверка после старта

def _fixups_path(cfg, room_id) -> Path:
    return Path(cfg.state_dir) / str(room_id) / "model_fixups.json"


# --- «применится со следующей задачи» (B-26) ------------------------------
#
# Смена модели у участника во время работы НЕ должна ронять идущую карточку.
# В момент сохранения выбора мы запоминаем идущие карточки участника (id + время
# старта попытки) в `model_pending.json`; `verify_started` видит такую карточку,
# понимает, что она стартовала до смены, и не делает `reassign --reclaim` — просто
# сообщает «применится со следующей задачи». Отметка снимается сама, как только у
# карточки появляется новая попытка (другое время старта) или её перезапустили явно.

def _pending_path(cfg, room_id) -> Path:
    return Path(cfg.state_dir) / str(room_id) / "model_pending.json"


def _pending(cfg, room_id) -> dict:
    try:
        value = sf.read_json(_pending_path(cfg, room_id), {})
    except Exception:  # noqa: BLE001 - файл вторичен
        return {}
    return value if isinstance(value, dict) else {}


def pending_marked(cfg, room_id, card_id) -> dict | None:
    """Отметка «карточка начала работу ДО смены модели» (B-26), либо None."""
    item = _pending(cfg, str(room_id)).get(str(card_id))
    return item if isinstance(item, dict) else None


def mark_pending(cfg, room_id, profile, cards, *, model: str = "") -> list:
    """Запомнить идущие карточки участника на момент смены модели (B-26).

    `cards` — карточки в статусе `running` из `card_map` (id + started_at).
    Возвращает id отмеченных. Ошибка записи не ломает смену модели.
    """
    ids = []
    if not cards:
        return ids
    try:
        path = _pending_path(cfg, room_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        with sf.lock_for(path):
            data = _pending(cfg, room_id)
            for card in cards:
                cid = str((card or {}).get("id") or "")
                if not cid:
                    continue
                data[cid] = {"profile": str(profile or ""), "started_at": _int(card.get("started_at")),
                             "model": str(model or ""), "at": _now()}
                ids.append(cid)
            sf.write_json_atomic(path, data)
    except Exception:  # noqa: BLE001 - журнал ожидания вторичен
        return ids
    return ids


def clear_pending(cfg, room_id, card_id) -> bool:
    """Снять отметку ожидания (карточка перезапущена или уже на новой модели)."""
    try:
        path = _pending_path(cfg, room_id)
        with sf.lock_for(path):
            data = _pending(cfg, room_id)
            if str(card_id) not in data:
                return False
            del data[str(card_id)]
            sf.write_json_atomic(path, data)
        return True
    except Exception:  # noqa: BLE001
        return False


def _fixups(cfg, room_id) -> dict:
    try:
        value = sf.read_json(_fixups_path(cfg, room_id), {})
    except Exception:  # noqa: BLE001 - файл вторичен
        return {}
    return value if isinstance(value, dict) else {}


def _fixup_note(cfg, room_id, card_id: str, *, model: str, run_id, count: int) -> int:
    """Отметить попытку перехвата (не больше `FIXUP_MAX` на карточку)."""
    try:
        path = _fixups_path(cfg, room_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        with sf.lock_for(path):
            data = _fixups(cfg, room_id)
            data[str(card_id)] = {"model": str(model or ""), "run_id": run_id,
                                  "count": int(count), "at": _now()}
            sf.write_json_atomic(path, data)
        return int(count)
    except Exception:  # noqa: BLE001 - журнал попыток не должен ломать проверку
        return int(count)


def verify_started(cfg, room_id, *, board: str | None = None, fix: bool = True,
                   limit: int = MAX_CARDS) -> dict:
    """Сверить ФАКТ модели идущих карточек с выбором комнаты; не ту — вернуть на нужную.

    Проверка нужна потому, что модель выбирается в момент допуска запуска: если карточку
    взяли раньше, чем сторож успел проставить переопределение, она едет на модели профиля
    до конца прогона (`docs/ENGINE-FACTS`, `gateway/session_kanban.py`). Такую карточку
    останавливаем штатным `reassign <профиль> --reclaim` (предварительно закрепив выбор) —
    диспетчер запустит её заново уже на нужной модели. Попытки ограничены `FIXUP_MAX`:
    если модель так и не встала, дальше только предупреждение, без бесконечных перезапусков.
    """
    out = {"checked": 0, "confirmed": 0, "mismatched": [], "fixed": [], "failed": [], "pending": [],
           "unknown": 0, "skipped": 0}
    room = _modules().get_room(cfg, str(room_id)) or {}
    if not room:
        return out
    settings = settings_of(room)
    if not settings:
        return out
    if getattr(cfg, "mock", False) or getattr(cfg, "demo", False):
        return out
    use = board or _board(cfg, room)
    cards = card_map(cfg, room, board=use)
    con = _connect(cfg, use)
    if con is None:
        return out
    try:
        for card in cards.values():
            if card["status"] not in STARTED_STATUSES:
                continue
            profile = card["assignee"]
            if profile not in settings:
                continue
            want = _wanted(cfg, room, profile)
            if not want["model"]:
                out["skipped"] += 1        # в комнате выбора нет: сверять не с чем
                continue
            run, sid = _run_session(con, card["id"])
            if not run or not sid:
                out["unknown"] += 1        # ещё не допущена / сессии нет
                continue
            out["checked"] += 1
            got = _session_model(cfg, run.get("profile") or profile, sid)
            if not got.get("model"):
                out["unknown"] += 1
                continue
            same = got["model"] == want["model"] and \
                (not got.get("provider") or not want["provider"] or got["provider"] == want["provider"])
            if same:
                out["confirmed"] += 1
                continue
            expected_label = _short_label(want["provider"], want["model"], want["reasoning"])
            actual_label = _short_label(got.get("provider") or "", got["model"], card["reasoning"])
            # B-26: карточка стартовала ДО смены модели — это не «ушла не на ту модель»,
            # а ожидаемое поведение. Работу не прерываем: новая модель применится со
            # следующей задачи. Отметку ставит `apply_profile` в момент сохранения выбора.
            note = pending_marked(cfg, str(room_id), card["id"])
            if note is not None:
                run_started = _int(run.get("started_at")) or _int(card.get("started_at"))
                marked_start = _int(note.get("started_at"))
                if not marked_start or not run_started or marked_start == run_started:
                    out["pending"].append({"id": card["id"], "assignee": profile,
                                           "expected": expected_label, "actual": actual_label,
                                           "run_id": run.get("id"),
                                           "since": _int(note.get("at"))})
                    out["skipped"] += 1
                    continue
                clear_pending(cfg, str(room_id), card["id"])   # отметка устарела: новая попытка
            out["mismatched"].append({"id": card["id"], "assignee": profile,
                                      "expected": expected_label, "actual": actual_label,
                                      "run_id": run.get("id")})
            _journal(cfg, str(room_id),
                     "Карточка %s (%s) ушла на %s, а в комнате выбрано %s"
                     % (card["id"], profile, actual_label or "модель профиля", expected_label),
                     kind="model_changed")
            if not fix or len(out["fixed"]) >= max(1, int(limit)):
                continue
            age = _now() - (_int(card.get("started_at")) or _int(run.get("started_at")) or _now())
            if age < FIXUP_MIN_AGE:
                out["skipped"] += 1        # только что взяли — дадим воркеру шанс стартовать
                continue
            tries = int((_fixups(cfg, str(room_id)).get(card["id"]) or {}).get("count") or 0)
            if tries >= FIXUP_MAX:
                out["skipped"] += 1
                continue
            _fixup_note(cfg, str(room_id), card["id"], model=got["model"],
                        run_id=run.get("id"), count=tries + 1)
            _done, failed = _push(cfg, use, [card], want)
            if failed:
                out["failed"].append({"id": card["id"], "reason": failed[0].get("reason") or ""})
                continue
            try:
                try:
                    from . import engine_write as ew
                except ImportError:  # pragma: no cover
                    import engine_write as ew  # type: ignore
                ew.act(cfg, use, card["id"], "reassign", task_id=card["id"], profile=profile)
            except Exception as exc:  # noqa: BLE001 - одна карточка не валит проверку
                out["failed"].append({"id": card["id"], "reason": str(exc)[:200]})
                continue
            out["fixed"].append(card["id"])
            _journal(cfg, str(room_id),
                     "Карточка %s (%s) остановлена и возвращена на выбор комнаты: %s"
                     % (card["id"], profile, expected_label), kind="model_restarted")
    finally:
        con.close()
    return out


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
                         "agent_default": agent_default(cfg, profile),
                         "agent_default_info": default_info(cfg, profile)}
    detail["agent_models_meta"] = meta
    # W29-Q9: «Модель для рутинного сведения» — что применится и сколько это сэкономит.
    try:
        detail["routine"] = routine_effective(cfg, room)
        detail["routine_estimate"] = routine_estimate(cfg, room)
    except Exception:  # noqa: BLE001 - комната важнее оценки экономии
        detail.setdefault("routine", {"source": "lead", "is_custom": False})
        detail.setdefault("routine_estimate", {"savings_pct": None, "text": "", "source": "none"})
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
        item["agent_default_info"] = meta.get(profile, {}).get("agent_default_info") or \
            default_info(cfg, profile)
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
    base["agent_default_info"] = default_info(cfg, name)
    try:
        base["model_sent"] = member_sent(cfg, room, name)
    except Exception:  # noqa: BLE001 - «ушло на» вторично для ответа
        base["model_sent"] = {}
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


__all__ = ["EFFORTS", "EFFORT_LABELS", "EFFORT_SHORT", "APPLY_STATUSES", "FIXUP_MAX", "ModelError",
           "config_path", "state_path", "bridge_path", "catalog", "provider_label", "agent_default",
           "default_info", "SOURCE_RU", "GLOBAL_NOTE", "NONE_NOTE",
           "validate", "validate_map", "normalize", "settings_of", "effective", "model_settings",
           "get", "set", "clear", "label", "human", "apply_to_tree", "apply_profile",
           "restart_running", "pending_marked", "mark_pending", "clear_pending",
           "decorate_room", "card_settings", "participants",
           "apply_to_room", "verify_started", "tenant_cards", "card_map", "actual_model", "pin_cards",
           "sent_of", "member_sent", "models_block", "push_model"]
