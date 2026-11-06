"""Подписи участников: человеческое имя и должность из описания профиля Hermes.

WAVE 13, пункт 11 (docs/WAVE13-RULES.md, пакет G). Участники комнаты назывались
техническим именем профиля (`biz-cfo`) и общей ролью «исполнитель» — по карточке
нельзя было понять, кто это и чем занимается. Описание у профилей Hermes есть:

    hermes profile describe biz-cfo  →  «Анна — финансы и экономика (CFO)»

Правила (дословно из замечания Михаила):

* подпись = имя + должность из описания профиля, техническое имя — отдельной
  мелкой строкой в интерфейсе;
* описание берём командой `hermes profile describe <имя>` (только чтение, через
  `server/proc.py` — без чёрных окон), ~1,3 с на профиль, поэтому кэшируем и
  обновляем В ФОНЕ: ответ API никогда не ждёт CLI;
* профиль без описания (`default`, `realtor`) — не ошибка: подпись соберёт
  интерфейс из роли в комнате или из ручной подписи участника (`label`);
* в песочнице (тесты, проверки) реальные профили Hermes НЕ опрашиваются:
  подпись берётся из `profile.yaml` (`description`), который сервер уже читает.

Публично::

    parse_description(text) -> {"name", "job"}      # чистая функция разбора
    label_text(name, job) -> str                   # «Анна — финансы (CFO)»
    decorate(description, entry, custom) -> dict   # поля подписи для API
    labels(cfg, profiles) -> {профиль: подпись}    # кэш + фоновое обновление
    read_description(cfg, profile) -> str          # один вызов CLI
    set_reader(fn) / reset() / wait()              # подмена и ожидание в тестах
"""
from __future__ import annotations

import re
import subprocess
import threading
import time

try:                                     # import server.profile_info
    from . import proc                   # запуск CLI только через proc (без окна)
except ImportError:                      # python server/app.py (плоский импорт)
    import proc                          # type: ignore

#: описание профиля меняют вручную — раз в смену перепроверить достаточно
TTL = 6 * 3600
#: профиль без описания перепроверяем чаще: его могли только что дописать
TTL_MISS = 300
#: `hermes profile describe` ~1,3 с; запас на холодный диск и антивирус
TIMEOUT = 8
#: сколько разборов одновременно (каждый — отдельный процесс CLI)
MAX_WORKERS = 2
MAX_NAME = 60
MAX_JOB = 300
MAX_CUSTOM = 120

#: имя профиля уходит в argv — как в app_common.PROFILE_RE (без ведущего «-»)
PROFILE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.@+-]{0,63}$")

#: тире всех видов, которыми люди разделяют имя и должность
_DASHES = "\u2014\u2013\u2015\u2012\u2212"
#: «Анна — финансы», «Анна - финансы», «Анна -- финансы»
_SPACED_DASH = re.compile(r"\s+[%s-]{1,2}\s+" % _DASHES)
#: «Анна—финансы»: ASCII-дефис сюда НЕ входит, иначе «юрист-ИП» разрежется не там
_TIGHT_DASH = re.compile(r"\s*[%s]\s*" % _DASHES)
#: ответ CLI для профиля без описания: «(no description set for 'default')»
_NO_DESCRIPTION = re.compile(r"no description set|описание не задано|без описания", re.I)
_SENTENCE_PUNCT = re.compile(r"[.,;:!?]")

_CACHE: dict = {}
_LOCK = threading.Lock()
_INFLIGHT: set = set()
_THREADS: list = []
_SLOTS = threading.BoundedSemaphore(MAX_WORKERS)
_READER = None


def _clean(text) -> str:
    """Однострочный текст без кавычек по краям (описание из YAML бывает в «ёлочках»)."""
    raw = " ".join(str(text if text is not None else "").split())
    return raw.strip(" \t\"'«»")


def _now() -> float:
    return time.time()


# --------------------------------------------------------------- разбор описания

def parse_description(text) -> dict:
    """«Анна — финансы и экономика (CFO)» → `{"name": "Анна", "job": "финансы и экономика (CFO)"}`.

    Разбор нарочно осторожный:

    * тире любых видов (`—`, `–`, `―`, `‒`, `−`, `-`, `--`) разделяет имя и должность;
    * слева от тире — только короткое имя (≤ 60 знаков, ≤ 4 слов, без знаков
      конца предложения); иначе это обычный текст с тире внутри, и имя не выдумываем;
    * без тире имя не угадываем: весь текст становится должностью (описанием);
    * пустое описание и ответ CLI «(no description set…)» → обе части пустые.
    """
    text = _clean(text)
    if not text or _NO_DESCRIPTION.search(text):
        return {"name": "", "job": ""}
    match = _SPACED_DASH.search(text) or _TIGHT_DASH.search(text)
    if match is not None:
        left = text[:match.start()].strip(" .,:;'\"«»")
        right = text[match.end():].strip(" .,:;'\"«»")
        if left and len(left) <= MAX_NAME and len(left.split()) <= 4 and not _SENTENCE_PUNCT.search(left):
            return {"name": left, "job": right[:MAX_JOB]}
    return {"name": "", "job": text[:MAX_JOB]}


#: где естественно оборвать длинную должность для заголовка карточки
_SHORT_BREAK = re.compile(r"\s*[(:;]|\s+[—–]\s+")
SHORT_JOB = 80


def short_job(job) -> str:
    """Длинная должность → короткая часть для заголовка карточки.

    «локальный исполнитель-помощник (модель Qwen3.6-35B-A3B; …): выполняет …»
    → «локальный исполнитель-помощник». Короткие должности не трогаем — в
    «Анна — финансы и экономика (CFO)» скобки значимы. Полный текст остаётся в
    `description` и показывается в карточке под заголовком.
    """
    text = _clean(job)
    if len(text) <= SHORT_JOB:
        return text
    match = _SHORT_BREAK.search(text)
    if match and match.start() >= 12:
        return text[:match.start()].strip(" .,:;—-")
    cut = text[:SHORT_JOB].rsplit(" ", 1)[0].strip(" .,:;—-")
    return cut or text[:SHORT_JOB].strip()


def label_text(name, job) -> str:
    """Части подписи → готовая строка: «Анна — финансы (CFO)», «Анна», «финансы»."""
    parts = [_clean(name), _clean(job)]
    return " — ".join(part for part in parts if part)


def decorate(description="", entry=None, custom="") -> dict:
    """Поля подписи для API: имя, должность, готовая строка и источник.

    Порядок источников (первый непустой выигрывает):

    1. `custom` — ручная подпись участника в этой комнате (поле `label`);
    2. `entry` — ответ `hermes profile describe` (кэш `labels`);
    3. `description` — описание из `profile.yaml` (есть сразу, без CLI);
    4. роль в комнате — пустая подпись, интерфейс покажет роль (`label_source=role`).
    """
    entry = entry if isinstance(entry, dict) else {}
    custom = _clean(custom)[:MAX_CUSTOM]
    text = _clean(description)
    if custom:
        parsed, source = parse_description(custom), "member"
    elif entry.get("name") or entry.get("job"):
        parsed, source = {"name": entry.get("name") or "", "job": entry.get("job") or ""}, "describe"
    else:
        parsed = parse_description(text)
        source = "profile" if (parsed["name"] or parsed["job"]) else "role"
    name, job = parsed["name"], parsed["job"]
    return {"name": name, "job": job, "label": label_text(name, short_job(job)),
            "label_source": source,
            "description": text, "custom_label": custom,
            "label_pending": bool(entry.get("pending"))}


# ------------------------------------------------------------------- чтение CLI

def _enabled(cfg) -> bool:
    """Можно ли спрашивать Hermes: в песочнице — нет (реальные профили не трогаем)."""
    if getattr(cfg, "sandbox", False):
        return False
    return bool(str(getattr(cfg, "hermes_cli", "") or "").strip())


def read_description(cfg, profile) -> str:
    """`hermes profile describe <имя>` → текст описания; любая ошибка → "".

    Запуск только через `server/proc.py` (`CREATE_NO_WINDOW`): иначе на Windows
    на каждый профиль мигало бы чёрное консольное окно.
    """
    name = str(profile or "").strip()
    cli = str(getattr(cfg, "hermes_cli", "") or "").strip()
    if not name or not cli or not PROFILE_RE.match(name):
        return ""
    try:
        res = proc.run([cli, "profile", "describe", name], capture_output=True, text=True,
                       encoding="utf-8", errors="replace", timeout=TIMEOUT)
    except (OSError, ValueError, subprocess.SubprocessError):
        return ""
    out = str(getattr(res, "stdout", "") or "").strip()
    if getattr(res, "returncode", 0) != 0 and not out:
        return ""
    return out


# ------------------------------------------------------------------------- кэш

def _public(name: str, cached: dict, live: bool, stale: bool) -> dict:
    """Запись кэша → то, что видят `labels()` и интерфейс."""
    has = bool(cached.get("name") or cached.get("job"))
    return {"profile": name,
            "name": str(cached.get("name") or ""),
            "job": str(cached.get("job") or ""),
            "label": label_text(cached.get("name"), cached.get("job")),
            "raw": str(cached.get("raw") or ""),
            "source": "describe" if has else "none",
            "stale": bool(stale),
            "pending": bool(live and stale)}


def _store(name: str, raw: str) -> None:
    parsed = parse_description(raw)
    with _LOCK:
        _CACHE[name] = {"raw": _clean(raw), "name": parsed["name"], "job": parsed["job"],
                        "at": _now(), "missing": not (parsed["name"] or parsed["job"])}
        _INFLIGHT.discard(name)


def _refresh(cfg, names) -> None:
    for name in names:
        try:
            with _SLOTS:
                raw = (_READER or read_description)(cfg, name)
        except Exception:           # noqa: BLE001 — подпись не должна ронять сервер
            raw = ""
        _store(name, raw)


def _kick(cfg, names) -> None:
    """Обновить подписи в фоне: ответ API ждать CLI не должен."""
    if not names:
        return
    try:
        thread = threading.Thread(target=_refresh, args=(cfg, list(names)),
                                  name="profile-labels", daemon=True)
        thread.start()
    except RuntimeError:            # нет ресурсов на поток — снимем метки, попробуем позже
        with _LOCK:
            for name in names:
                _INFLIGHT.discard(name)
        return
    with _LOCK:
        _THREADS[:] = [t for t in _THREADS if t.is_alive()]
        _THREADS.append(thread)


def labels(cfg, profiles, now=None) -> dict:
    """Подписи профилей: `{профиль: {name, job, label, raw, source, stale, pending}}`.

    Никогда не ждёт CLI: свежий кэш отдаётся сразу, устаревший — сразу и с
    `stale=True` (обновление в фоне), отсутствующий — пустой с `pending=True`.
    """
    stamp = _now() if now is None else float(now)
    names: list = []
    for item in profiles or []:
        name = str((item.get("profile") if isinstance(item, dict) else item) or "").strip()
        if name and name not in names:
            names.append(name)
    live = _enabled(cfg)
    out: dict = {}
    todo: list = []
    with _LOCK:
        for name in names:
            cached = _CACHE.get(name)
            ttl = TTL_MISS if (cached and cached.get("missing")) else TTL
            if cached and (stamp - float(cached.get("at") or 0)) < ttl:
                out[name] = _public(name, cached, live, stale=False)
                continue
            out[name] = _public(name, cached, live, stale=True) if cached else {
                "profile": name, "name": "", "job": "", "label": "", "raw": "",
                "source": "none", "stale": False, "pending": live}
            if live and name not in _INFLIGHT:
                _INFLIGHT.add(name)
                todo.append(name)
    _kick(cfg, todo)
    return out


def prime(cfg, profiles) -> dict:
    """Прогреть кэш (заодно запускает фоновое обновление) — удобно из тестов."""
    return labels(cfg, profiles)


# ------------------------------------------------------------ подмена в тестах

def set_reader(fn) -> None:
    """Подменить чтение описания (тесты не запускают настоящий CLI)."""
    global _READER
    _READER = fn


def reset() -> None:
    """Очистить кэш и подмену; фоновые потоки доигрывают сами."""
    global _READER
    _READER = None
    with _LOCK:
        _CACHE.clear()
        _INFLIGHT.clear()


def wait(timeout: float = 10.0) -> bool:
    """Дождаться фоновых обновлений (только для тестов и проверок)."""
    deadline = time.monotonic() + max(0.0, float(timeout))
    while time.monotonic() < deadline:
        with _LOCK:
            alive = [t for t in _THREADS if t.is_alive()]
            _THREADS[:] = alive
        if not alive:
            return True
        alive[0].join(max(0.01, deadline - time.monotonic()))
    return False


def cached(profile) -> dict:
    """Что уже лежит в кэше (для отчётов и тестов): копия записи или {}."""
    with _LOCK:
        item = _CACHE.get(str(profile or "").strip())
        return dict(item) if item else {}
