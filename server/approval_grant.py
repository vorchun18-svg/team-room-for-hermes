"""Запись разрешения в профиль Hermes — ОТДЕЛЬНЫМ процессом (WAVE22 §2.3).

Зачем отдельный процесс: список постоянных разрешений Hermes
(``command_allowlist`` в ``config.yaml`` профиля) читается и пишется функциями самого
Hermes, а они живут в его дереве и его интерпретаторе. Сервер «Комнаты команды» —
stdlib Python, поэтому правку делает этот скрипт, запущенный интерпретатором Hermes
через ``server/proc.py`` (без окна). Никакой ручной правки YAML: только
``tools.approval.save_permanent_allowlist`` и ``hermes_cli.config.save_config``.

Протокол: одна JSON-строка на входе (аргументом или в stdin) и одна на выходе.

    → {"op": "add",    "home": "<домашний каталог профиля>",
       "pattern_key": "script execution via heredoc"}
    → {"op": "remove", "home": "…", "pattern_key": "recursive delete"}
    → {"op": "list",   "home": "…"}
    → {"op": "check",  "home": "…", "kind": "terminal"|"execute_code",
       "command": "python - <<'EOF'…"}

    ← {"ok": true, "op": "add", "pattern_key": "…", "allowlist": ["…"],
       "changed": true, "home": "…"}
    ← {"ok": false, "code": "no_hermes"|"bad_request"|"failed", "message": "…"}

``home`` — это ``HERMES_HOME`` профиля: для профиля ``default`` — корень Hermes,
для остальных — ``<корень>/profiles/<имя>``. Скрипт пишет ТОЛЬКО в присланный
каталог; путь проверяется (абсолютный, существует, не файл).

``check`` — сверка с живым Hermes (только на поддельном профиле!): спрашивает у
``check_all_command_guards`` / ``check_execute_code_guard``, что он думает о команде
в режиме «без человека» (``HERMES_SINGLE_QUERY_SESSION=1``,
``approvals.single_query_mode: deny``). Ничего не пишет.

Публично: ``run(req) -> dict``, ``main(argv) -> int``.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

#: Пределы входных значений: ключ списка — обычная строка описания правила.
MAX_KEY = 200
MAX_COMMAND = 8000
#: Сколько масок «только с моего да» принимаем (уровень «Свободный»).
MAX_KEYS = 200
#: Сколько ждать импорт Hermes (его дерево большое, но не бесконечное).
MAX_ALLOWLIST = 500


class GrantError(Exception):
    """Отказ с кодом для вызывающей стороны (``no_hermes``, ``bad_request``, ``failed``)."""

    def __init__(self, message: str, code: str = "failed"):
        super().__init__(message)
        self.message = message
        self.code = code


# ---------------------------------------------------------------- помощники

def _clean_key(value) -> str:
    """Ключ списка разрешений: непустая строка без переводов строк и NUL."""
    key = str(value or "").strip()
    if not key:
        raise GrantError("Нужен ключ разрешения", "bad_request")
    if len(key) > MAX_KEY:
        raise GrantError("Слишком длинный ключ разрешения", "bad_request")
    if any(ch in key for ch in "\r\n\x00"):
        raise GrantError("Ключ разрешения содержит перевод строки", "bad_request")
    return key


def _clean_home(value) -> Path:
    """Домашний каталог профиля: абсолютный, существует, каталог (не файл)."""
    text = str(value or "").strip().strip('"').strip("'")
    if not text:
        raise GrantError("Не указан домашний каталог профиля", "bad_request")
    home = Path(text)
    if not home.is_absolute():
        raise GrantError("Домашний каталог профиля должен быть абсолютным путём", "bad_request")
    if not home.is_dir():
        raise GrantError("Домашний каталог профиля не найден", "bad_request")
    return home


def _hermes_tree(home: Path, explicit: str = "") -> Path:
    """Дерево ``hermes-agent`` (там лежит пакет ``tools``) — рядом с домашним каталогом."""
    if explicit:
        tree = Path(explicit)
        if (tree / "tools" / "approval.py").is_file():
            return tree
    candidates = [home / "hermes-agent", home.parent / "hermes-agent"]
    for candidate in candidates:
        if (candidate / "tools" / "approval.py").is_file():
            return candidate
    raise GrantError("Не нашёл дерево Hermes рядом с профилем — нечего вызывать", "no_hermes")


def _prepare(home: Path, tree: Path) -> None:
    """Окружение и пути для импорта Hermes: ТОЛЬКО присланный каталог профиля."""
    os.environ["HERMES_HOME"] = str(home)
    for name in ("HERMES_YOLO_MODE", "HERMES_PROFILE"):
        os.environ.pop(name, None)
    os.environ.setdefault("PYTHONIOENCODING", "utf-8")
    os.environ.setdefault("PYTHONUTF8", "1")
    for path in (str(tree),):
        if path not in sys.path:
            sys.path.insert(0, path)


def _read_allowlist(home: Path) -> list[str]:
    """``command_allowlist`` профиля как список строк (только чтение, без Hermes)."""
    path = home / "config.yaml"
    if not path.is_file():
        return []
    try:
        import yaml                                        # noqa: PLC0415 - есть не всегда
        with open(path, "r", encoding="utf-8-sig") as fh:
            data = yaml.safe_load(fh) or {}
    except Exception:                                       # noqa: BLE001 - конфиг мог быть пуст
        return []
    raw = data.get("command_allowlist") if isinstance(data, dict) else None
    if isinstance(raw, str):
        try:
            import yaml                                    # noqa: PLC0415
            raw = yaml.safe_load(raw)
        except Exception:                                  # noqa: BLE001
            raw = []
    if not isinstance(raw, list):
        return []
    return [str(item) for item in raw if isinstance(item, str)]


# ---------------------------------------------------------------- операции

def op_list(req: dict) -> dict:
    home = _clean_home(req.get("home"))
    return {"ok": True, "op": "list", "home": str(home), "allowlist": _read_allowlist(home)}


def op_add(req: dict) -> dict:
    """Добавить ключ в список постоянных разрешений штатным путём Hermes."""
    home = _clean_home(req.get("home"))
    key = _clean_key(req.get("pattern_key"))
    tree = _hermes_tree(home, str(req.get("tree") or ""))
    before = _read_allowlist(home)
    _prepare(home, tree)
    try:
        from tools import approval as approval                     # noqa: PLC0415
    except Exception as exc:                                        # noqa: BLE001
        raise GrantError("Hermes не импортируется (%s)" % (str(exc)[:200] or "ошибка"),
                         "no_hermes") from exc
    try:
        approval.load_permanent_allowlist()
        approval.save_permanent_allowlist({key})
    except Exception as exc:                                        # noqa: BLE001
        raise GrantError("Не удалось записать разрешение: %s" % str(exc)[:200], "failed") from exc
    after = _read_allowlist(home)
    if key not in after:
        raise GrantError("Hermes не записал разрешение в список профиля", "failed")
    return {"ok": True, "op": "add", "home": str(home), "pattern_key": key,
            "allowlist": after[:MAX_ALLOWLIST], "changed": sorted(after) != sorted(before)}


def op_remove(req: dict) -> dict:
    """Убрать ключ из списка постоянных разрешений (через ``save_config`` Hermes).

    ``save_permanent_allowlist`` умеет только добавлять, поэтому удаление — та же
    штатная пара «прочитать конфиг → изменить список → записать конфиг», что и у самого
    Hermes (``hermes_cli.config``). Другие ключи конфига не меняются.
    """
    home = _clean_home(req.get("home"))
    key = _clean_key(req.get("pattern_key"))
    tree = _hermes_tree(home, str(req.get("tree") or ""))
    _prepare(home, tree)
    try:
        from hermes_cli.config import load_config, save_config    # noqa: PLC0415
    except Exception as exc:                                        # noqa: BLE001
        raise GrantError("Hermes не импортируется (%s)" % (str(exc)[:200] or "ошибка"),
                         "no_hermes") from exc
    try:
        config = load_config()
    except Exception as exc:                                        # noqa: BLE001
        raise GrantError("Не удалось прочитать настройки профиля: %s" % str(exc)[:200],
                         "failed") from exc
    current = config.get("command_allowlist")
    if isinstance(current, str):
        current = _read_allowlist(home)
    current = [str(item) for item in (current or []) if isinstance(item, str)]
    if key not in current:
        return {"ok": True, "op": "remove", "home": str(home), "pattern_key": key,
                "allowlist": current, "changed": False, "absent": True}
    config["command_allowlist"] = [item for item in current if item != key]
    try:
        save_config(config)
    except Exception as exc:                                        # noqa: BLE001
        raise GrantError("Не удалось сохранить настройки профиля: %s" % str(exc)[:200],
                         "failed") from exc
    after = _read_allowlist(home)
    return {"ok": True, "op": "remove", "home": str(home), "pattern_key": key,
            "allowlist": after[:MAX_ALLOWLIST], "changed": key not in after}


def op_check(req: dict) -> dict:
    """Спросить у Hermes, разрешена ли команда — в режиме «без человека» (ничего не пишет)."""
    home = _clean_home(req.get("home"))
    tree = _hermes_tree(home, str(req.get("tree") or ""))
    kind = str(req.get("kind") or "terminal")
    command = str(req.get("command") or "")
    if len(command) > MAX_COMMAND:
        raise GrantError("Слишком длинная команда для сверки", "bad_request")
    _prepare(home, tree)
    os.environ["HERMES_SINGLE_QUERY_SESSION"] = "1"
    os.environ["HERMES_INTERACTIVE"] = "1"
    try:
        from tools import approval as approval                     # noqa: PLC0415
    except Exception as exc:                                        # noqa: BLE001
        raise GrantError("Hermes не импортируется (%s)" % (str(exc)[:200] or "ошибка"),
                         "no_hermes") from exc
    try:
        if kind == "execute_code":
            result = approval.check_execute_code_guard(command, "local")
        else:
            result = approval.check_all_command_guards(command, "local")
    except Exception as exc:                                        # noqa: BLE001
        raise GrantError("Hermes не смог проверить команду: %s" % str(exc)[:200], "failed") from exc
    return {"ok": True, "op": "check", "home": str(home), "kind": kind,
            "approved": bool(result.get("approved")),
            "pattern_key": result.get("pattern_key"),
            "description": result.get("description"),
            "detect": list(_detect(command)) if kind != "execute_code" else None}


def _detect(command: str) -> tuple:
    """``detect_dangerous_command`` без поломки, если функции нет в этой версии Hermes."""
    try:
        from tools.approval import detect_dangerous_command            # noqa: PLC0415
        return detect_dangerous_command(command)
    except Exception:                                                  # noqa: BLE001
        return (False, None, None)


# ---------------------------------------------------------------- доверие (W26-D1)

def _read_single_query_mode(home: Path) -> str:
    """``approvals.single_query_mode`` профиля как строка (только чтение)."""
    path = home / "config.yaml"
    if not path.is_file():
        return ""
    try:
        import yaml                                            # noqa: PLC0415
        with open(path, "r", encoding="utf-8-sig") as fh:
            data = yaml.safe_load(fh) or {}
    except Exception:                                           # noqa: BLE001
        return ""
    approvals = data.get("approvals") if isinstance(data, dict) else None
    if not isinstance(approvals, dict):
        return ""
    return str(approvals.get("single_query_mode") or "").strip().lower()


def _write_single_query_mode(home: Path, tree: Path, value) -> tuple:
    """Записать ``approvals.single_query_mode`` штатной парой Hermes.

    ``value=None`` — убрать ключ (вернуть поведение «как было в Hermes по умолчанию»).
    Возвращает ``(было, стало)``. Другие ключи конфига не меняются: правку делает сам
    Hermes (``hermes_cli.config.load_config`` → ``save_config``), а не наш YAML-текст.
    """
    before = _read_single_query_mode(home)
    _prepare(home, tree)
    try:
        from hermes_cli.config import load_config, save_config    # noqa: PLC0415
    except Exception as exc:                                        # noqa: BLE001
        raise GrantError("Hermes не импортируется (%s)" % (str(exc)[:200] or "ошибка"),
                         "no_hermes") from exc
    try:
        config = load_config()
    except Exception as exc:                                        # noqa: BLE001
        raise GrantError("Не удалось прочитать настройки профиля: %s" % str(exc)[:200],
                         "failed") from exc
    if not isinstance(config, dict):
        config = {}
    approvals = config.get("approvals")
    approvals = dict(approvals) if isinstance(approvals, dict) else {}
    if value is None:
        approvals.pop("single_query_mode", None)
    else:
        approvals["single_query_mode"] = str(value)
    config["approvals"] = approvals
    try:
        save_config(config)
    except Exception as exc:                                        # noqa: BLE001
        raise GrantError("Не удалось сохранить настройки профиля: %s" % str(exc)[:200],
                         "failed") from exc
    return before, _read_single_query_mode(home)


def op_trust(req: dict) -> dict:
    """Доверить профилю запуск кода: ``approvals.single_query_mode: approve``.

    Только по явному действию человека и только в присланный профиль. Срок обязателен
    и хранится в реестре комнаты — снимает запись ``op_untrust`` или сторож.
    """
    home = _clean_home(req.get("home"))
    scope = str(req.get("ttl") or "").strip().lower()
    if scope not in ("minutes", "card", "forever"):
        raise GrantError("Нужен срок доверия: minutes, card или forever", "bad_request")
    tree = _hermes_tree(home, str(req.get("tree") or ""))
    before, after = _write_single_query_mode(home, tree, "approve")
    if after != "approve":
        raise GrantError("Hermes не записал «доверие» в настройки профиля", "failed")
    return {"ok": True, "op": "trust", "home": str(home), "scope": scope,
            "previous": before, "value": after, "changed": before != after}


def op_untrust(req: dict) -> dict:
    """Отозвать доверие: вернуть прежнее ``single_query_mode`` (или убрать ключ)."""
    home = _clean_home(req.get("home"))
    previous = str(req.get("previous") or "").strip().lower()
    if previous in ("approve",):
        # «навсегда так и было» — не наш случай: доверие ставим мы, и снимать его надо
        previous = ""
    tree = _hermes_tree(home, str(req.get("tree") or ""))
    before, after = _write_single_query_mode(home, tree, previous or None)
    return {"ok": True, "op": "untrust", "home": str(home), "previous": before,
            "value": after, "restored": previous or "(по умолчанию Hermes)",
            "changed": after != "approve"}


def _mode_matches(want: str, have: str) -> bool:
    """Совпадает ли записанный режим с желаемым (``deny`` — умолчание Hermes).

    Hermes пишет конфиг без значений, равных умолчанию: ``single_query_mode: deny``
    может не оказаться в файле, и это ТО ЖЕ поведение, а не «не записалось».
    """
    target = str(want or "").strip().lower()
    got = str(have or "").strip().lower()
    if target == got:
        return True
    return target == "deny" and got == ""


def _read_deny(home: Path) -> list[str]:
    """``approvals.deny`` профиля как список строк (только чтение, без Hermes)."""
    path = home / "config.yaml"
    if not path.is_file():
        return []
    try:
        import yaml                                            # noqa: PLC0415 - есть не всегда
        with open(path, "r", encoding="utf-8-sig") as fh:
            data = yaml.safe_load(fh) or {}
    except Exception:                                           # noqa: BLE001
        return []
    approvals = data.get("approvals") if isinstance(data, dict) else None
    raw = approvals.get("deny") if isinstance(approvals, dict) else None
    if not isinstance(raw, list):
        return []
    return [str(item) for item in raw if isinstance(item, str)]


def op_apply(req: dict) -> dict:
    """Применить уровень автономии комнаты к ОДНОМУ профилю (W27-E5, §2).

    Одна запись — три ключа штатного конфига Hermes: список разрешений
    (``command_allowlist``), список «только с моего да» (``approvals.deny``) и
    поведение запусков без человека (``approvals.single_query_mode``). Правку делает
    сам Hermes (``hermes_cli.config.load_config`` → ``save_config``), поэтому
    посторонние ключи и их форма не меняются.

    ``None`` в ``deny``/``single_query_mode`` — «не трогать»; ``""`` в
    ``single_query_mode`` — убрать ключ (вернуть умолчание Hermes).
    """
    home = _clean_home(req.get("home"))
    add = [_clean_key(key) for key in (req.get("allowlist_add") or [])]
    remove = [_clean_key(key) for key in (req.get("allowlist_remove") or [])]
    deny = req.get("deny")
    if deny is not None:
        if not isinstance(deny, list):
            raise GrantError("Список «только с моего да» должен быть списком масок",
                             "bad_request")
        deny = [_clean_key(mask) for mask in deny][:MAX_KEYS]
    mode = req.get("single_query_mode")
    if mode is not None:
        mode = str(mode).strip().lower()
        if mode not in ("approve", "deny", ""):
            raise GrantError("single_query_mode: нужно approve, deny или пусто",
                             "bad_request")
    if not add and not remove and deny is None and mode is None:
        return {"ok": True, "op": "apply", "home": str(home), "changed": False,
                "allowlist": _read_allowlist(home)}
    tree = _hermes_tree(home, str(req.get("tree") or ""))
    before = _read_allowlist(home)
    before_mode = _read_single_query_mode(home)
    _prepare(home, tree)
    try:
        from hermes_cli.config import load_config, save_config    # noqa: PLC0415
    except Exception as exc:                                        # noqa: BLE001
        raise GrantError("Hermes не импортируется (%s)" % (str(exc)[:200] or "ошибка"),
                         "no_hermes") from exc
    try:
        config = load_config()
    except Exception as exc:                                        # noqa: BLE001
        raise GrantError("Не удалось прочитать настройки профиля: %s" % str(exc)[:200],
                         "failed") from exc
    if not isinstance(config, dict):
        config = {}
    current = config.get("command_allowlist")
    if not isinstance(current, list):
        current = _read_allowlist(home)
    current = [str(item) for item in current if isinstance(item, str)]
    kept = [key for key in current if key not in remove]
    for key in add:
        if key not in kept:
            kept.append(key)
    config["command_allowlist"] = kept
    approvals = config.get("approvals")
    approvals = dict(approvals) if isinstance(approvals, dict) else {}
    if deny is not None:
        approvals["deny"] = list(deny)
    if mode is not None:
        if mode:
            approvals["single_query_mode"] = mode
        else:
            approvals.pop("single_query_mode", None)
    config["approvals"] = approvals
    try:
        save_config(config)
    except Exception as exc:                                        # noqa: BLE001
        raise GrantError("Не удалось сохранить настройки профиля: %s" % str(exc)[:200],
                         "failed") from exc
    after = _read_allowlist(home)
    after_deny = _read_deny(home)
    after_mode = _read_single_query_mode(home)
    # Подтверждение ФАЙЛА, а не ответа инструмента: прибавленный ключ обязан быть в списке
    # сразу после записи. Иначе отказываем здесь — сервер не должен показывать «записано»
    # там, где файл пуст, и наоборот.
    missing = [key for key in add if key not in after]
    if missing:
        raise GrantError("Hermes не записал в список разрешений: %s"
                         % ", ".join(missing)[:200], "failed")
    if mode and not _mode_matches(mode, after_mode):
        raise GrantError("Hermes не записал «запуски без человека»: ожидалось «%s», в файле «%s»"
                         % (mode, after_mode or "нет"), "failed")
    if deny is not None and sorted(after_deny) != sorted(deny):
        raise GrantError("Hermes не записал список «только с моего да» целиком "
                         "(в файле %d из %d)" % (len(after_deny), len(deny)), "failed")
    return {"ok": True, "op": "apply", "home": str(home), "config": str(home / "config.yaml"),
            "allowlist": after[:MAX_ALLOWLIST], "changed": after != before,
            "single_query_mode": after_mode,
            "previous_single_query_mode": before_mode,
            "deny": after_deny[:MAX_ALLOWLIST]}


OPS = {"list": op_list, "add": op_add, "remove": op_remove, "check": op_check,
       "trust": op_trust, "untrust": op_untrust, "apply": op_apply}


def run(req: dict) -> dict:
    """Выполнить операцию из запроса. Ошибки — словарём с ``ok: false`` (не исключением)."""
    if not isinstance(req, dict):
        return {"ok": False, "code": "bad_request", "message": "Запрос должен быть объектом"}
    op = str(req.get("op") or "list").strip().lower()
    func = OPS.get(op)
    if func is None:
        return {"ok": False, "code": "bad_request", "message": "Неизвестная операция: %s" % op}
    try:
        return func(req)
    except GrantError as exc:
        return {"ok": False, "op": op, "code": exc.code, "message": exc.message}
    except Exception as exc:                                            # noqa: BLE001
        return {"ok": False, "op": op, "code": "failed",
                "message": "Не удалось: %s" % (str(exc)[:240] or exc.__class__.__name__)}


# ---------------------------------------------------------------- запуск

def _utf8_stdio() -> None:
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass


def main(argv=None) -> int:
    """Один запрос — один ответ. Код возврата 0 даже при отказе: отказ в JSON."""
    _utf8_stdio()
    args = list(sys.argv[1:] if argv is None else argv)
    raw = args[0] if args else ""
    if not raw or raw == "-":
        try:
            raw = sys.stdin.read()
        except Exception:                                               # noqa: BLE001
            raw = ""
    try:
        req = json.loads(raw) if raw.strip() else {}
    except ValueError:
        result = {"ok": False, "code": "bad_request", "message": "Некорректный JSON-запрос"}
    else:
        result = run(req)
    sys.stdout.write(json.dumps(result, ensure_ascii=False) + "\n")
    sys.stdout.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main())
