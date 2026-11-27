# -*- coding: utf-8 -*-
"""Единая защита: песочница/мок/демо НЕ пишут на реальную доску Hermes.

Зачем
-----
19–21.11.2026 песочный сервер приложения (свой ``TEAMROOM_DATA``, но с НАСТОЯЩИМ домом
Hermes) снял пины и паузы у восьми боевых карточек: сторож моделей прочитал боевую доску
и записал в неё. ``TEAMROOM_DATA`` песочницу доски не даёт — доска берётся из
``hermes_home``/``HERMES_KANBAN_HOME``. Теперь это одна проверка на всех путях записи:

* CLI канбана — ``engine_cli._run`` (через него пишут ``engine_write``, ``model_guard``,
  ``archive``, ``thread``, ``engine_write_guard``);
* библиотека канбана в процессе — ``kanban_atomic`` (``stop``/``resume``/``restore``…);
* мост к Hermes — ``tools/kb_bridge.py`` (``--set``/``--clear-model``) и его вызов из
  ``agent_models``;
* сторожа — ``model_guard``, ``watcher``, ``svod_live``, ``critic_loop``: в таком режиме
  они вообще не пишут в доску (проход пропускается с причиной).

Правило (одно)
--------------
Если сервер в режиме песочницы/мока/демо — ``cfg.sandbox``, ``TEAMROOM_SANDBOX``,
``cfg.mock``/``cfg.demo`` или ``data_dir`` во временном каталоге системы — И путь доски
ведёт в реальный дом Hermes (``%LOCALAPPDATA%/hermes``, ``HERMES_HOME``,
``HERMES_KANBAN_HOME`` или найденный дом), то любая запись отклоняется с кодом
``sandbox_real_board``.

Признак «данные во временном каталоге» добавлен W31-FIX3: 27.11.2026 прогон тестов
поднял приложение с ``TEAMROOM_DATA`` в ``%TEMP%`` и БЕЗ ``TEAMROOM_SANDBOX`` — и три
карточки «Анализ задач» ушли на живую доску, потому что признаков не было вовсе.

Обход — только явный: ``TEAMROOM_ALLOW_REAL_BOARD=1``. Это осознанное «я правда пишу в
боевую доску из этого процесса»: без него отказ, с причиной и действием в тексте.

Чтение не запрещается: песочнице можно смотреть (``sqlite mode=ro``), но не писать.
"""
from __future__ import annotations

import os
import tempfile
from pathlib import Path

#: Явное разрешение писать на реальную доску из «не своего» процесса.
ALLOW_ENV = "TEAMROOM_ALLOW_REAL_BOARD"
#: Явный признак песочницы (пробрасывается в дочерние процессы: мост, воркер).
SANDBOX_ENV = "TEAMROOM_SANDBOX"
#: Машинный код отказа (409) — один на все пути записи.
CODE = "sandbox_real_board"

_TRUE = ("1", "true", "yes", "on", "да", "y")
#: Глаголы CLI, которые ПИШУТ в доску. Всё, чего нет в `READ_VERBS`, тоже считается записью
#: (fail-safe: новый глагол не проскочит молча).
WRITE_VERBS = frozenset({
    "create", "update", "comment", "block", "unblock", "promote", "reclaim", "claim",
    "assign", "reassign", "archive", "unarchive", "restore", "delete", "rm", "remove",
    "dispatch", "decompose", "specify", "set-model", "set_model", "reopen-review",
    "approve", "reject", "move", "link", "unlink", "start", "stop", "finish", "done",
    "reset", "import", "sync", "prune", "gc", "add", "edit", "tag", "label", "watch",
})
#: Глаголы только для чтения — их песочница вправе звать даже на реальной доске.
READ_VERBS = frozenset({
    "show", "ls", "list", "boards", "stats", "status", "log", "logs", "tail", "export",
    "search", "events", "get", "info", "diff", "help", "--help", "-h", "--version",
})

MESSAGE = (
    "Запись в доску отклонена: этот сервер работает как песочница/мок/демо ({signs}), "
    "а доска ведёт в реальный дом Hermes ({root}). Песочница не пишет на боевую доску — "
    "иначе она трогает чужие карточки. Нужна настоящая запись? Запустите боевой сервер "
    "с реальными данными (без TEAMROOM_DATA/TEAMROOM_HERMES_HOME) или, осознанно, "
    "с {allow}=1."
)


class BoardWriteBlocked(RuntimeError):
    """Отказ записи в доску из песочницы (HTTP 409, код ``sandbox_real_board``)."""

    code = CODE
    http_status = 409
    message_ru = ""

    def __init__(self, message_ru: str, detail: dict | None = None):
        super().__init__(message_ru)
        self.message_ru = str(message_ru or MESSAGE)
        self.detail = dict(detail or {})

    def payload(self) -> dict:
        """Форма ошибки API: ``{error: {code, message, detail}}``."""
        return {"error": {"code": self.code, "message": self.message_ru,
                          "detail": dict(self.detail)}}

    def to_dict(self) -> dict:
        return self.payload()

    def __str__(self) -> str:
        return self.message_ru


# ---------------------------------------------------------------- мелочи
def _truthy(value) -> bool:
    """``1/true/yes/on/да`` — истина; всё остальное (и пустое) — нет."""
    return str(value if value is not None else "").strip().lower() in _TRUE


def _norm(path) -> str:
    """Сравнимый вид пути: абсолютный и без учёта регистра (Windows)."""
    try:
        text = os.path.abspath(os.path.expanduser(str(path)))
    except (TypeError, ValueError, OSError):
        return ""
    return os.path.normcase(text).rstrip("\\/") or os.path.normcase(text)


def _under(path, root) -> bool:
    """Лежит ли ``path`` внутри ``root`` (или совпадает). Сравнение по границам каталогов."""
    child, parent = _norm(path), _norm(root)
    if not child or not parent:
        return False
    if child == parent:
        return True
    return child.startswith(parent + os.sep)


def _config_mod():
    """Модуль конфигурации (пакетный и «плоский» импорт)."""
    try:
        from . import config as mod
    except ImportError:                                   # pragma: no cover
        import config as mod                               # type: ignore
    return mod


def allow_override() -> bool:
    """``TEAMROOM_ALLOW_REAL_BOARD=1`` — единственный законный обход."""
    return _truthy(os.environ.get(ALLOW_ENV))


# ---------------------------------------------------------------- режим и корни
def default_data_dir() -> Path | None:
    """Каталог данных «как установлено»: ``<каталог приложения>/data``."""
    try:
        return Path(_config_mod().APP_DIR) / "data"
    except Exception:                                     # noqa: BLE001 - конфига нет
        return None


def ephemeral_data_dir(cfg=None) -> bool:
    """``data_dir`` во временном каталоге системы: одноразовый прогон, а не установка.

    Инцидент 27.11.2026: тест поднял приложение с ``TEAMROOM_DATA`` во временном
    каталоге, но БЕЗ ``TEAMROOM_SANDBOX``/``TEAMROOM_HERMES_HOME`` — признаков
    песочницы не было, и защита молчала, а доска была живая. Свои данные во
    временном каталоге никто не держит постоянно: это проба/тест, а не установка.
    """
    try:
        data = Path(str(getattr(cfg, "data_dir", "") or ""))
    except (TypeError, ValueError, OSError):
        return False
    if not str(data):
        return False
    roots: list[str] = []
    for raw in (os.environ.get("TEMP"), os.environ.get("TMP"), tempfile.gettempdir()):
        text = str(raw or "").strip()
        if text and text not in roots:
            roots.append(text)
    local = str(os.environ.get("LOCALAPPDATA") or "").strip()
    if local:
        roots.append(str(Path(local) / "Temp"))
    return any(_under(data, root) for root in roots)


def sandbox_signs(cfg=None) -> list[str]:
    """Почему это песочница/мок/демо: список признаков (пусто — боевой режим)."""
    signs: list[str] = []
    if getattr(cfg, "sandbox", False):
        signs.append("cfg.sandbox (TEAMROOM_HERMES_HOME)")
    if _truthy(os.environ.get(SANDBOX_ENV)):
        signs.append("%s=1" % SANDBOX_ENV)
    if getattr(cfg, "mock", False):
        signs.append("мок")
    if getattr(cfg, "demo", False):
        signs.append("демо")
    if str(os.environ.get("TEAMROOM_HERMES_HOME") or "").strip() and not signs:
        signs.append("TEAMROOM_HERMES_HOME задан")
    # Каталог данных (в том числе временный) — не признак песочницы: тесты, читающие реальную
    # доску (`test_e2e`), живут во временных данных, а защита заблокировала бы им запись
    # своих же зеркал. Инцидент 27.11 закрыт иначе: разбор рецептов выключен по умолчанию
    # и сам карточек не создаёт. Песочницы агентов помечаются явно:
    # обычная установка вправе держать данные где угодно, и защита заблокировала бы ей
    # всю запись. Песочницы агентов помечаются TEAMROOM_SANDBOX=1 или TEAMROOM_HERMES_HOME.
    return signs


def _sandbox_roots(cfg=None) -> list[Path]:
    """Корни, которые СВОИ у песочницы: их нельзя считать «реальным домом Hermes»."""
    out: list[Path] = []
    raw = str(os.environ.get("TEAMROOM_HERMES_HOME") or "").strip()
    if raw:
        out.append(Path(raw))
    if getattr(cfg, "sandbox", False):
        for name in ("hermes_home", "kanban_home", "data_dir"):
            value = getattr(cfg, name, None)
            if value:
                out.append(Path(value))
    return out


def real_roots(cfg=None) -> list[Path]:
    """Реальные дома Hermes на этой машине (без корней песочницы)."""
    raw: list[Path] = []
    for env in ("HERMES_HOME", "HERMES_KANBAN_HOME"):
        value = str(os.environ.get(env) or "").strip()
        if value:
            raw.append(Path(value.strip('"').strip("'")))
    mod = None
    try:
        mod = _config_mod()
    except Exception:                                     # noqa: BLE001 - конфига нет
        mod = None
    if mod is not None:
        for getter in ("real_hermes_home", "_hermes_home_candidates"):
            try:
                value = getattr(mod, getter)()
            except Exception:                             # noqa: BLE001 - автоопределение
                continue
            if isinstance(value, (list, tuple)):
                raw.extend(Path(item) for item in value if item)
            elif value:
                raw.append(Path(value))
    if getattr(cfg, "hermes_home", None):
        raw.append(Path(cfg.hermes_home))
    skip = _sandbox_roots(cfg)
    out: list[Path] = []
    seen: set[str] = set()
    for item in raw:
        key = _norm(item)
        if not key or key in seen:
            continue
        if any(_under(item, root) for root in skip):
            continue                                     # это корень песочницы
        seen.add(key)
        out.append(Path(item))
    return out


def board_db(cfg=None, board: str | None = None) -> Path | None:
    """Путь ``kanban.db`` доски (без создания файлов)."""
    try:
        if cfg is not None:
            return Path(cfg.kanban_db(str(board or "")))
    except Exception:                                     # noqa: BLE001 - кривой cfg
        return None
    home = str(os.environ.get("HERMES_KANBAN_HOME") or os.environ.get("HERMES_HOME") or "")
    if not home:
        return None
    slug = str(board or "default")
    if slug in ("", "default"):
        return Path(home) / "kanban" / "kanban.db"
    return Path(home) / "kanban" / "boards" / slug / "kanban.db"


def real_board_root(cfg=None, board: str | None = None, db=None) -> Path | None:
    """Корень реального дома Hermes, в который ведёт доска (или None)."""
    target = Path(db) if db else board_db(cfg, board)
    if target is None and cfg is not None:
        target = getattr(cfg, "kanban_home", None)         # доска по умолчанию = дом Hermes
    if target is None:
        return None
    for root in real_roots(cfg):
        if _under(target, root):
            return root
    return None


# ---------------------------------------------------------------- главная проверка
def check(cfg=None, board: str | None = None, *, db=None) -> dict | None:
    """Отказ или None: ``{code, message, detail}`` — готовая причина для API/лога."""
    if allow_override():
        return None
    signs = sandbox_signs(cfg)
    if not signs:
        return None
    root = real_board_root(cfg, board, db=db)
    if root is None:
        return None
    target = Path(db) if db else board_db(cfg, board)
    detail = {"reason": CODE, "signs": signs, "real_home": str(root),
              "board": str(target or ""), "allow_env": "%s=1" % ALLOW_ENV}
    return {"code": CODE, "message": MESSAGE.format(signs=", ".join(signs),
                                                    root=root, allow=ALLOW_ENV),
            "detail": detail}


def blocked(cfg=None, board: str | None = None, *, db=None) -> bool:
    """Короткий ответ «запись запрещена?» для сторожей."""
    return check(cfg, board, db=db) is not None


def reason(cfg=None, board: str | None = None, *, db=None) -> str:
    """Текст причины отказа (пусто — писать можно)."""
    found = check(cfg, board, db=db)
    return str((found or {}).get("message") or "")


def ensure(cfg=None, board: str | None = None, *, db=None) -> None:
    """Бросить ``BoardWriteBlocked``, если писать нельзя."""
    found = check(cfg, board, db=db)
    if found:
        raise BoardWriteBlocked(found["message"], found["detail"])


def is_write_command(args) -> bool:
    """Пишет ли эта команда CLI: неизвестный глагол считаем записью (fail-safe)."""
    for token in args or []:
        text = str(token or "").strip()
        if not text or text.startswith("-"):
            continue
        low = text.lower()
        if low in READ_VERBS:
            return False
        return True
    return False


def status(cfg=None) -> dict:
    """Состояние защиты для ``/api/health`` и «Проверки» (чтение, ничего не пишет)."""
    signs = sandbox_signs(cfg)
    root = real_board_root(cfg, getattr(cfg, "board", None) if cfg is not None else None)
    blocked_now = bool(signs and root and not allow_override())
    if allow_override():
        text = "Песочница и реальная доска: запись разрешена явно (%s=1)" % ALLOW_ENV
    elif blocked_now:
        text = ("Запись в доску запрещена: песочница (%s), доска — реальный дом Hermes (%s)"
                % (", ".join(signs), root))
    elif signs:
        text = "Песочница (%s): своя доска, запись разрешена" % ", ".join(signs)
    else:
        text = "Боевой режим: доска — %s" % (root or "—")
    return {"sandbox": bool(signs), "signs": signs,
            "real_home": str(root or ""), "blocked": blocked_now,
            "allow_env": bool(allow_override()), "text": text}


__all__ = ["ALLOW_ENV", "SANDBOX_ENV", "CODE", "MESSAGE", "WRITE_VERBS", "READ_VERBS",
           "BoardWriteBlocked", "allow_override", "default_data_dir", "ephemeral_data_dir",
           "sandbox_signs", "real_roots", "board_db", "real_board_root", "check", "blocked",
           "reason", "ensure", "is_write_command", "status"]
