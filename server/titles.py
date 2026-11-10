"""Название запроса штатным механизмом Hermes (W17-B §4, аудит B9 усилено 06.11).

Михаил: «название сеанса у Hermes формируется по первому сообщению, а у нас в
„Недавнем“ — сырой текст диктовки; пусть нейронка делает краткое понятное название,
и оно может меняться по ходу беседы».

Как у Hermes (`hermes-agent/agent/title_generator.py`), две ступени и приоритет
источников:

1. ``derived`` — мгновенное детерминированное название (``derive_title``, ≤ 48 знаков).
   Считается СРАЗУ при создании запроса на нашей стороне: запуск интерпретатора
   Hermes занял бы секунды и заблокировал бы создание запроса, а алгоритм
   воспроизведён один в один (первая осмысленная строка, вводные «Так,», «Давай…»
   отброшены). Как только ответил мост, название уточняется результатом самого
   Hermes-модуля (``source="hermes"`` в ответе моста).
2. ``llm`` — улучшение одним вызовом МАЛОЙ модели через ``generate_title`` и
   ``auxiliary.title_generation`` из конфига Hermes: какая модель настроена у
   пользователя, та и работает. Вызов идёт в фоне (мост
   ``tools/title_bridge.py``, интерпретатор Hermes), создание запроса не ждёт.
3. ``leader`` — ``metadata.title`` руководителя комнаты (приоритет выше ``llm``).
4. ``user`` — переименование пользователем (``PATCH /api/requests/{id}``):
   ``title_source="user"``, ``title_locked=true``; такое название не перезаписывается
   никогда. Пустое значение снимает блокировку и возвращает автоматическое.

Название может обновляться по ходу беседы: после выпуска свода и после
существенных уточнений — но не чаще :data:`REFRESH_MIN_S` и никогда для
``user``-названий.

Ничего не падает и не блокирует: нет Hermes/модуля/модели, мост завис или упал —
остаётся ``derived``.
"""
from __future__ import annotations

import hashlib
import json
import os
import queue
import sys
import threading
import time
from pathlib import Path

try:                                        # import server.titles
    from . import proc
    from . import registry
    from .title_short import (TITLE_LIMIT, capitalize, clean, cut, first_sentence,
                             short_title, strip_fillers)
except ImportError:                          # python server/app.py (плоский импорт)
    import proc                              # type: ignore
    import registry                          # type: ignore
    from title_short import (TITLE_LIMIT, capitalize, clean, cut,  # type: ignore
                             first_sentence, short_title, strip_fillers)

#: источники названия по возрастанию силы (как в Hermes: derived < llm < user,
#: плюс наш ``leader`` — метаданные руководителя комнаты)
SOURCES = ("derived", "llm", "leader", "user")
PRIORITY = {"derived": 0, "llm": 1, "leader": 2, "user": 3}
#: как часто пересчитывать название по ходу беседы (не чаще раза в N секунд)
REFRESH_MIN_S = 300
#: сколько ждём мост (импорт Hermes ~1 с + вызов малой модели)
BRIDGE_TIMEOUT = 90
#: бюджет входа моста (``MAX_TITLE_INPUT_CHARS`` Hermes)
MAX_INPUT = 1000
FALLBACK_TITLE = "Запрос"

_LOG: list = []                     # последние ошибки моста (диагностика, без шума)
_QUEUE: "queue.Queue | None" = None
_WORKER: "threading.Thread | None" = None
_LOCK = threading.Lock()


def _log(message: str) -> None:
    """Запомнить последнюю ошибку (мост) для диагностики; ничего не бросает."""
    try:
        _LOG.append(str(message)[:300])
        del _LOG[:-20]
    except Exception:  # noqa: BLE001 - диагностика не важнее работы
        pass


def recent_errors() -> list:
    """Последние ошибки моста (для ``/api/diagnostics`` и отчётов)."""
    return list(_LOG)


# ---------------------------------------------------------------- приоритеты ---

def fields(rec) -> dict:
    """Название из реестра в канонической форме (никогда не бросает).

    ``{title_short, title_source, title_locked}`` — ровно те поля, что отдаёт API
    (``docs/API.md`` §3.3).
    """
    rec = rec if isinstance(rec, dict) else {}
    short = clean(rec.get("title_short"))
    source = str(rec.get("title_source") or "").strip().lower()
    if source not in SOURCES:
        source = "derived" if short else ""
    locked = bool(rec.get("title_locked")) and source == "user"
    if source == "user" and short:
        locked = True
    return {"title_short": short, "title_source": source or "derived", "title_locked": locked}


def overlay(item, rec) -> dict:
    """Наложить сохранённое название на сводку/деталь (приоритет источников).

    ``item`` — ``RequestSummary``/деталь (там ``title_short`` уже посчитан из
    ``metadata.title`` руководителя или из текста: ``title_source`` = ``leader``
    либо ``derived``). Побеждает источник с большим приоритетом.
    """
    if not isinstance(item, dict):
        return item
    stored = fields(rec)
    base_source = str(item.get("title_source") or "").strip().lower()
    if base_source not in SOURCES:
        base_source = "derived" if clean(item.get("title_short")) else ""
    base_short = clean(item.get("title_short"))
    chosen_short, chosen_source = base_short, base_source or "derived"
    if stored["title_short"] and \
            PRIORITY[stored["title_source"]] > PRIORITY.get(chosen_source, 0):
        chosen_short, chosen_source = stored["title_short"], stored["title_source"]
    item["title_short"] = chosen_short or FALLBACK_TITLE
    item["title_source"] = chosen_source
    item["title_locked"] = bool(stored["title_locked"] or chosen_source == "user")
    return item


def seed(text, title: str = "", user: bool = False) -> dict:
    """Поля названия для НОВОЙ записи реестра (``derived`` сразу, без моста).

    ``user=True`` — название задал пользователь при создании (оно же не
    перезаписывается и не улучшается моделью).
    """
    if user and clean(title):
        value = cut(clean(title))
        return {"title_short": value, "title_source": "user", "title_locked": True,
                "title_auto": "", "title_updated_at": int(time.time())}
    value = derive(text)
    return {"title_short": value, "title_source": "derived", "title_locked": False,
            "title_auto": value, "title_updated_at": int(time.time())}


def derive(text, limit: int = TITLE_LIMIT) -> str:
    """Запасное название без Hermes: как ``derive_title`` — первая осмысленная строка.

    Никогда не бросает и не возвращает пустоту (пустой текст → «Запрос»).
    """
    try:
        return short_title(text, limit)
    except Exception:  # noqa: BLE001 - название не важнее создания запроса
        return FALLBACK_TITLE


# ---------------------------------------------------------------- запись ---

def _store(cfg, room_id, rid, patch: dict) -> dict:
    """Записать поля в реестр; ошибка реестра не мешает работе."""
    try:
        return registry.update(cfg, room_id, rid, dict(patch)) or {}
    except Exception as exc:  # noqa: BLE001 - реестр вторичен
        _log("реестр названия %s: %s" % (rid, exc))
        return {}


def _present(value: str) -> str:
    """Название для интерфейса: без вводных «Так,»/«Давай…», с заглавной буквы.

    Текст названия берём у Hermes (его ``derive_title``), а показываем по правилам
    интерфейса: диктовка начинается с «Так, значит, …», и такое название в меню
    бесполезно. Предел — :data:`TITLE_LIMIT`.
    """
    return cut(capitalize(strip_fillers(clean(value))))


def apply(cfg, room_id, rid, title, source: str, at=None) -> dict:
    """Поставить название источника ``source``, если его приоритет не ниже текущего.

    Пользовательское (``user``) не перезаписывается НИКОГДА; ``leader`` сильнее
    ``llm``; ``derived`` только заполняет пустое.
    """
    value = clean(title)
    source = str(source or "").strip().lower()
    if not value or source not in SOURCES:
        return {}
    value = _present(value)
    if not value:
        return {}
    current = {}
    try:
        current = registry.get(cfg, room_id, rid) or {}
    except Exception as exc:  # noqa: BLE001
        _log("реестр названия (чтение) %s: %s" % (rid, exc))
    now_fields = fields(current)
    if current.get("title_locked") or now_fields["title_locked"]:
        return {}                               # имя задано человеком — не трогаем
    if PRIORITY[source] < PRIORITY[now_fields["title_source"]]:
        return {}
    patch = {"title_short": value, "title_source": source,
             "title_updated_at": int(at or time.time())}
    if source != "user":
        patch["title_auto"] = value
    return _store(cfg, room_id, rid, patch)


def rename(cfg, room_id, rid, title) -> dict:
    """Переименование пользователем: ``user`` + ``title_locked``; пусто — снять блокировку."""
    value = clean(title)
    if not value:
        current = {}
        try:
            current = registry.get(cfg, room_id, rid) or {}
        except Exception:  # noqa: BLE001
            current = {}
        back = clean(current.get("title_auto")) or derive(current.get("text") or "")
        return _store(cfg, room_id, rid, {"title_short": back, "title_source": "derived",
                                          "title_locked": False,
                                          "title_updated_at": int(time.time())})
    value = cut(value)
    return _store(cfg, room_id, rid, {"title_short": value, "title_source": "user",
                                      "title_locked": True, "title_user": value,
                                      "title_updated_at": int(time.time())})


# ---------------------------------------------------------------- мост ---

def app_dir(cfg=None) -> Path:
    """Каталог приложения (там лежат ``tools/``)."""
    base = getattr(cfg, "app_dir", "") or Path(__file__).resolve().parent.parent
    return Path(base)


def bridge_path(cfg=None) -> Path:
    """Скрипт моста: ``<app>/tools/title_bridge.py``."""
    return app_dir(cfg) / "tools" / "title_bridge.py"


def _python(cfg) -> str:
    """Интерпретатор Hermes (там есть пакет ``agent`` и его зависимости).

    Порядок: ``TEAMROOM_HERMES_PYTHON`` → venv установок Hermes → текущий.
    ``sys.executable`` (обычный python сервера) пакета ``agent`` не содержит, поэтому
    он последний: без Hermes мост вернёт запасное название, а не упадёт.
    """
    candidates: list[str] = []
    value = os.environ.get("TEAMROOM_HERMES_PYTHON")
    if value:
        candidates.append(str(value))
    try:
        try:
            from . import agent_models as models
        except ImportError:                    # pragma: no cover
            import agent_models as models      # type: ignore
        root = models._root(cfg)               # noqa: SLF001 - тот же корень Hermes
        installs = root / "installs"
        if installs.is_dir():
            candidates.extend(str(p) for p in sorted(
                installs.glob("*/environments/*/venv/Scripts/python.exe")))
            candidates.extend(str(p) for p in sorted(
                installs.glob("*/environments/*/venv/bin/python")))
    except Exception:  # noqa: BLE001 - путь к Hermes не обязателен
        pass
    candidates.append(sys.executable or "python")
    for candidate in candidates:
        try:
            if candidate and Path(candidate).exists():
                return candidate
        except OSError:
            continue
    return sys.executable or "python"


def _env(cfg) -> dict:
    """Окружение моста: без агентской сессии, без записи байткода в Hermes."""
    clean_keys = ("HERMES_DELEGATED_CHILD_CONTEXT", "HERMES_KANBAN_DB", "HERMES_KANBAN_TASK",
                  "HERMES_KANBAN_BOARD", "HERMES_KANBAN_HOME", "HERMES_KANBAN_WORKSPACES_ROOT",
                  "HERMES_KANBAN_ATTACHMENTS_ROOT")
    env = {k: v for k, v in os.environ.items() if k not in clean_keys}
    try:
        from . import agent_models as models
        env.update({k: v for k, v in models._env(cfg).items()
                    if k in ("HERMES_AGENT_HOME",)})
    except Exception:  # noqa: BLE001 - путь к Hermes не обязателен
        pass
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    return env


def _run_bridge(cfg, mode: str, text: str = "") -> dict:
    """Вызвать мост и разобрать JSON. Любая ошибка → ``{"ok": False, ...}``."""
    script = bridge_path(cfg)
    if not script.exists():
        return {"ok": False, "code": "unavailable", "error": "мост не найден"}
    argv = [_python(cfg), "-B", str(script), mode]
    try:
        done = proc.run(argv, input=text or "", capture_output=True, text=True,
                        encoding="utf-8", errors="replace", timeout=BRIDGE_TIMEOUT,
                        env=_env(cfg), cwd=str(app_dir(cfg)) or None)
    except Exception as exc:  # noqa: BLE001 - зависший/запрещённый процесс не мешает
        _log("мост названий (%s): %s" % (mode, exc))
        return {"ok": False, "code": "unavailable", "error": "%s: %s" % (exc.__class__.__name__, exc)}
    payload: dict = {}
    for line in reversed((getattr(done, "stdout", "") or "").splitlines()):
        line = line.strip()
        if not line:
            continue
        try:
            parsed = json.loads(line)
        except ValueError:
            continue
        if isinstance(parsed, dict):
            payload = parsed
            break
    if not payload:
        _log("мост названий (%s): пустой ответ" % mode)
        return {"ok": False, "code": "engine_error", "error": "пустой ответ моста"}
    return payload


def generate(cfg, text: str) -> dict:
    """Название через Hermes: ``{ok, derived, title, source, model, error}``. Не бросает."""
    payload = _run_bridge(cfg, "--generate", text)
    return {"ok": bool(payload.get("ok")),
            "derived": clean(payload.get("derived")),
            "title": clean(payload.get("title")),
            "source": str(payload.get("source") or ""),
            "model": payload.get("model") or {},
            "error": payload.get("error") or ""}


def available(cfg) -> dict:
    """Есть ли Hermes-модуль названий (``--check``, модель НЕ вызывается)."""
    return _run_bridge(cfg, "--check")


# ---------------------------------------------------------------- задания ---

def _worker_loop() -> None:
    """Фоновой поток: одно задание за раз, ошибки не роняют поток."""
    while True:
        job = _QUEUE.get() if _QUEUE is not None else None
        if job is None:
            return
        cfg, room_id, rid, text, kind = job
        try:
            process(cfg, room_id, rid, text, kind)
        except Exception as exc:  # noqa: BLE001 - фон не должен падать
            _log("название %s: %s" % (rid, exc))


def _start_worker() -> None:
    global _QUEUE, _WORKER
    with _LOCK:
        if _WORKER is not None and _WORKER.is_alive():
            return
        if _QUEUE is None:
            _QUEUE = queue.Queue(maxsize=64)
        _WORKER = threading.Thread(target=_worker_loop, name="teamroom-titles", daemon=True)
        _WORKER.start()


def enqueue(cfg, room_id, rid, text, kind: str = "initial") -> bool:
    """Поставить улучшение названия в фон. Никогда не блокирует и не бросает."""
    if str(os.environ.get("TEAMROOM_TITLE_WORKER") or "").strip().lower() in ("off", "0", "no"):
        return False
    try:
        _start_worker()
        if _QUEUE is None:
            return False
        _QUEUE.put_nowait((cfg, room_id, rid, text or "", kind))
        return True
    except queue.Full:
        _log("очередь названий переполнена (%s)" % rid)
        return False
    except Exception as exc:  # noqa: BLE001
        _log("очередь названий: %s" % exc)
        return False


def drain(timeout: float = 10.0) -> bool:
    """Дождаться, пока фоновая очередь опустеет (для тестов и остановки)."""
    deadline = time.monotonic() + max(0.0, float(timeout or 0))
    while _QUEUE is not None and not _QUEUE.empty() and time.monotonic() < deadline:
        time.sleep(0.02)
    return bool(_QUEUE is None or _QUEUE.empty())


def process(cfg, room_id, rid, text, kind: str = "initial") -> dict:
    """Одно задание: спросить Hermes и записать ``derived`` → ``llm`` (синхронно)."""
    res = generate(cfg, text)
    out = {"ok": bool(res.get("ok")), "derived": "", "llm": "", "error": res.get("error") or ""}
    if res.get("derived"):
        applied = apply(cfg, room_id, rid, res["derived"], "derived")
        if applied:
            out["derived"] = applied.get("title_short") or ""
    if res.get("title"):
        applied = apply(cfg, room_id, rid, res["title"], "llm",
                        at=int(time.time()))
        if applied:
            out["llm"] = applied.get("title_short") or ""
    if kind == "refresh":
        _store(cfg, room_id, rid, {"title_refresh_at": int(time.time())})
    return out


def start(cfg, room, rid: str, text: str, user: bool = False) -> dict:
    """Создание запроса: ``derived`` уже записан в реестр, ``llm`` — в фоне."""
    if user:
        return {"scheduled": False, "title_source": "user"}
    scheduled = enqueue(cfg, room.get("id") if isinstance(room, dict) else room, rid, text, "initial")
    return {"scheduled": scheduled, "title_source": "derived"}


# ------------------------------------------------- обновление по ходу беседы ---

def refresh_input(rec, detail=None, answers=None) -> str:
    """Вход для пересчёта названия: запрос + свод/ответы (в бюджете Hermes)."""
    rec = rec if isinstance(rec, dict) else {}
    parts: list = []
    text = clean(rec.get("text"))
    if text:
        parts.append(text[:400])
    detail = detail if isinstance(detail, dict) else {}
    synthesis = detail.get("synthesis") if isinstance(detail.get("synthesis"), dict) else {}
    if synthesis:
        head = clean(synthesis.get("title") or "") or clean(synthesis.get("summary_md") or
                                                             synthesis.get("text") or "")
        if head:
            parts.append(head[:400])
    for decision in (detail.get("decisions") or [])[-3:]:
        if isinstance(decision, dict) and clean(decision.get("text")):
            parts.append(clean(decision.get("text"))[:200])
    for qid, record in list((answers or {}).items())[-3:]:
        if isinstance(record, dict) and clean(record.get("text")):
            parts.append("%s: %s" % (clean(record.get("question_text"))[:120],
                                     clean(record.get("text"))[:200]))
    value = "\n".join(part for part in parts if part)
    return value[:MAX_INPUT]


def maybe_refresh(cfg, room, rec, detail=None, answers=None, force: bool = False) -> dict:
    """Пересчитать название по новому содержанию — не чаще :data:`REFRESH_MIN_S`.

    Вызывается сторожем после выпуска свода и обработчиком ответа после
    существенных уточнений. ``user``-название не трогается; пустой вход — пропуск.
    """
    rec = rec if isinstance(rec, dict) else {}
    rid = str(rec.get("root_id") or "")
    if not rid or not isinstance(room, dict):
        return {"scheduled": False, "reason": "no_request"}
    if fields(rec)["title_locked"] or rec.get("title_locked"):
        return {"scheduled": False, "reason": "locked"}
    now = int(time.time())
    last = int(rec.get("title_refresh_at") or 0)
    if not force and last and now - last < REFRESH_MIN_S:
        return {"scheduled": False, "reason": "too_soon", "retry_after": REFRESH_MIN_S - (now - last)}
    text = refresh_input(rec, detail, answers or rec.get("answers"))
    if not text:
        return {"scheduled": False, "reason": "empty"}
    detail = detail if isinstance(detail, dict) else {}
    answers = answers if isinstance(answers, dict) else (rec.get("answers") or {})
    has_new = bool(detail.get("synthesis")) or bool(detail.get("decisions")) or bool(answers)
    if not has_new:
        # один и тот же текст запроса сам по себе — не повод звать модель снова
        return {"scheduled": False, "reason": "no_new_content"}
    # содержимое не изменилось с прошлого пересчёта — модель не зовём (иначе каждые
    # 5 минут один и тот же запрос стоил бы денег)
    key = hashlib.sha1(text.encode("utf-8", "replace")).hexdigest()[:16]
    if not force and str(rec.get("title_refresh_key") or "") == key:
        return {"scheduled": False, "reason": "unchanged"}
    # отметку ставим ДО постановки: иначе сторож на каждом тике ставил бы задание заново
    _store(cfg, room.get("id"), rid, {"title_refresh_at": now, "title_refresh_key": key})
    scheduled = enqueue(cfg, room.get("id"), rid, text, "refresh")
    return {"scheduled": scheduled, "reason": "" if scheduled else "queue"}


__all__ = ["SOURCES", "PRIORITY", "REFRESH_MIN_S", "BRIDGE_TIMEOUT", "MAX_INPUT",
           "FALLBACK_TITLE", "fields", "overlay", "seed", "derive", "apply", "rename",
           "generate", "available", "bridge_path", "enqueue", "drain", "process", "start",
           "refresh_input", "maybe_refresh", "recent_errors"]
