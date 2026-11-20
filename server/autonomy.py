"""«Уровни автономии комнаты» (W27-E5, контракт WAVE27 §2).

Зачем
-----
Карточки комнаты — «конечные» запуски Hermes (``hermes chat -q``): человека за
клавиатурой нет, поэтому ``approvals.single_query_mode: deny`` (умолчание) блокирует
ЛЮБУЮ «опасную» команду сразу — включая безобидный ``python -c "print(1)"``. Человек
сидит и согласовывает вместе с агентом (проверено: 47 блокировок, из них 25
``execute_code``, 22 ``python -c``, 10 heredoc; ни одного удаления).

Модуль даёт комнате **три уровня** — поверх штатных настроек Hermes, ничего не
изобретая:

* ``careful`` («Осторожный») — как сейчас: ``single_query_mode: deny``, пустой список
  разрешений; записи, добавленные Team Room, снимаются;
* ``working`` («Рабочий») — ``command_allowlist += ["script execution via -e/-c flag",
  "script execution via heredoc"]``: агент сам запускает свои скрипты, а удаление,
  реестр, ``sudo``, ``docker rm``, ``curl|sh``, запись в ``.env``/``.ssh`` остаются
  вопросами к человеку;
* ``free`` («Свободный») — ``approvals.single_query_mode: approve`` +
  ``approvals.deny = never`` (список «только с моего да»): почти всё само, кроме
  перечисленного. Спросить в этом режиме некого — запрещённое просто блокируется,
  и агент обязан спросить человека вопросом в «Нужен ваш ответ».

Границы (жёсткие)
-----------------
* Запись — ТОЛЬКО в профили участников ЭТОЙ комнаты, ТОЛЬКО по явному
  ``confirm: true``, всегда с копией старого ``config.yaml``
  (``data/backups/autonomy/``); учёт записей — ``data/state/autonomy-applied.json``,
  отзыв = смена уровня (``careful`` возвращает что было).
* hardline-пол Hermes (удаление корня, формат диска, fork-бомба, shutdown) не
  трогается ничем: он в коде Hermes, а не в конфиге.
* В песочнице пишем только в поддельный ``HERMES_HOME`` (``<data>/hermes-home``) —
  тем же путём, что и разрешения (``server/approvals.py``).

Публично: ``LEVELS``, ``normalize``, ``validate``, ``room_settings``, ``view``,
``apply_level``, ``journal``, ``journal_add``, ``note_decision``, ``suggest``,
``probe``, ``profile_state``, ``severity_of``, ``set_cli_runner``,
``reset_cli_runner``, ``set_writer``, ``reset_writer``, ``AutonomyError``.
"""
from __future__ import annotations

import fnmatch
import json
import os
import shutil
import time
from pathlib import Path

try:                                        # пакетный импорт
    from . import approvals as ap
    from . import statefiles as sf
except ImportError:                          # python server/app.py (плоский импорт)
    import approvals as ap                   # type: ignore
    import statefiles as sf                  # type: ignore


class AutonomyError(Exception):
    """Отказ с кодом для HTTP-слоя (``bad_request``, ``conflict``, ``no_hermes``…)."""

    def __init__(self, message: str, http: int = 400, code: str = "bad_request",
                 detail: dict | None = None):
        super().__init__(message)
        self.message = message
        self.http = int(http)
        self.code = code
        self.detail = detail or {}


# ---------------------------------------------------------------- константы

LEVELS = ("careful", "working", "free")
#: Уровень по умолчанию: новая комната — «Рабочий» (§2).
DEFAULT_LEVEL = "working"

LEVEL_LABEL = {"careful": "Осторожный", "working": "Рабочий", "free": "Свободный"}

#: Что агент делает сам на «Рабочем» — классы штатного списка разрешений Hermes.
WORKING_RULES = ("script execution via -e/-c flag", "script execution via heredoc")

#: Правило контракта для модели (дублирует ``contract.approval_rule`` — короткая форма).
WORKING_RULE_SHORT = "скрипты — файлом и через терминал (`write_file` + `python файл.py`)"

#: «Только с моего да» по умолчанию (``approvals.deny`` на «Свободном»).
#: Маски — регистронезависимые glob'ы Hermes; классы из docs.security (78 правил).
NEVER_DEFAULTS = (
    # рекурсивное удаление
    "*rm -rf*", "*rm -fr*", "*rm -r *", "*rmdir /s*", "*Remove-Item*Recurse*",
    "*Remove-Item* -r *", "*del /s*", "*del /q*", "*find*-delete*", "*xargs rm*",
    # процессы, права, реестр, службы
    "*taskkill*", "*kill -9*", "*sudo *", "*chmod 777*",
    "*reg add*", "*reg delete*", "*regedit*", "*Set-ItemProperty*", "*New-ItemProperty*",
    "*sc delete*", "*Stop-Service*",
    # контейнеры
    "*docker rm*", "*docker volume rm*", "*docker system prune*",
    # «наружу» и секреты
    "*| sh*", "*|sh*", "*| bash*", "*|bash*", "*curl* -d *", "*curl*-X POST*",
    "*.env*", "*.ssh*", "*id_rsa*", "*.npmrc*", "*.pypirc*",
    # диск и питание
    "*mkfs*", "*format *:", "*shutdown *", "*reboot*",
)

#: Подписи масок по-русски: интерфейс показывает «что именно» запрещено.
NEVER_LABELS = {
    "*rm -rf*": "рекурсивное удаление (rm -rf)",
    "*rm -fr*": "рекурсивное удаление (rm -fr)",
    "*rm -r *": "удаление папки с содержимым (rm -r)",
    "*rmdir /s*": "удаление папки с содержимым (rmdir /s)",
    "*Remove-Item*Recurse*": "удаление с содержимым (Remove-Item -Recurse)",
    "*Remove-Item* -r *": "удаление с содержимым (Remove-Item -r)",
    "*del /s*": "удаление файлов (del /s)",
    "*del /q*": "удаление без вопросов (del /q)",
    "*find*-delete*": "удаление найденного (find -delete)",
    "*xargs rm*": "удаление списком (xargs rm)",
    "*taskkill*": "убийство процессов (taskkill)",
    "*kill -9*": "жёсткое убийство процессов (kill -9)",
    "*sudo *": "права администратора (sudo)",
    "*chmod 777*": "открытые права (chmod 777)",
    "*reg add*": "запись в реестр (reg add)",
    "*reg delete*": "удаление из реестра (reg delete)",
    "*regedit*": "правка реестра (regedit)",
    "*Set-ItemProperty*": "запись в реестр (Set-ItemProperty)",
    "*New-ItemProperty*": "запись в реестр (New-ItemProperty)",
    "*sc delete*": "удаление службы (sc delete)",
    "*Stop-Service*": "остановка службы (Stop-Service)",
    "*docker rm*": "удаление контейнера (docker rm)",
    "*docker volume rm*": "удаление данных контейнера (docker volume rm)",
    "*docker system prune*": "очистка docker (system prune)",
    "*| sh*": "передача скачанного в shell (| sh)",
    "*|sh*": "передача скаченного в shell (|sh)",
    "*| bash*": "передача скачанного в shell (| bash)",
    "*|bash*": "передача скаченного в shell (|bash)",
    "*curl* -d *": "отправка данных наружу (curl -d)",
    "*curl*-X POST*": "отправка данных наружу (curl -X POST)",
    "*.env*": "секреты окружения (.env)",
    "*.ssh*": "ключи доступа (.ssh)",
    "*id_rsa*": "личный ключ (id_rsa)",
    "*.npmrc*": "токены npm (.npmrc)",
    "*.pypirc*": "токены Python-пакетов (.pypirc)",
    "*mkfs*": "форматирование диска (mkfs)",
    "*format *:": "форматирование диска (format)",
    "*shutdown *": "выключение компьютера (shutdown)",
    "*reboot*": "перезагрузка (reboot)",
}

#: Классы Hermes, которые считаем КРИТИЧНЫМИ (разрушительные и «наружу»).
CRITICAL_KEYS = (
    "recursive delete", "delete", "remove", "rmdir", "unlink", "truncate",
    "sudo", "registry", "reg ", "service", "taskkill", "kill", "chmod", "chown",
    "docker", "pipe", "curl", "wget", "fetch", "download", "sql", "drop",
    "env", "ssh", "credential", "secret", "disk", "format", "mkfs", "shutdown",
    "reboot", "git push", "publish", "email", "send", "payment", "purchase",
)
#: Классы «рутины»: запуск своего кода и чтение — то, ради чего уровень и нужен.
ROUTINE_KEYS = ("script execution", "heredoc", "-e/-c", "read", "list", "grep",
                "cat ", "search", "note", "write_file")

#: Сколько строк журнала и профилей отдаём.
JOURNAL_MAX = 200
MAX_NEVER = 200
MAX_MASK = 200
#: Сколько ждём `hermes approvals suggest|test`.
CLI_TIMEOUT_S = 25


def _now() -> int:
    return int(time.time())


# ---------------------------------------------------------------- severity

def severity_of(pattern_key=None, command: str = "", floor: bool = False) -> str:
    """``critical`` (разрушительное/наружу) или ``routine`` — для заметного вида.

    Критичные блокировки интерфейс показывает отдельно («Требует вашего
    подтверждения: удаление 14 файлов в …») с предпросмотром «что именно»;
    рутина — обычной строкой, чтобы не мешать.
    """
    if floor:
        return "critical"
    key = str(pattern_key or "").strip().lower()
    if key:
        if any(needle in key for needle in CRITICAL_KEYS):
            return "critical"
        if any(needle in key for needle in ROUTINE_KEYS):
            return "routine"
    text = str(command or "").lower()
    if text:
        for mask in NEVER_DEFAULTS:
            if fnmatch.fnmatchcase(text, mask.lower()):
                return "critical"
    return "routine"


# ---------------------------------------------------------------- форма записи

def _clean_mask(value) -> str:
    text = str(value or "").strip()
    if not text:
        raise AutonomyError("Пустая маска в списке «только с моего да»",
                            detail={"reason": "empty_mask"})
    if len(text) > MAX_MASK:
        raise AutonomyError("Слишком длинная маска (до %d знаков)" % MAX_MASK,
                            detail={"limit": MAX_MASK})
    if any(ch in text for ch in "\r\n\x00"):
        raise AutonomyError("Маска не может содержать перевод строки")
    return text


def normalize(value) -> dict:
    """Запись комнаты как есть: ``{level, never, updated_at}`` (мусор → умолчания).

    Чтение реестра комнат не должно падать из-за одного поля: непонятный уровень —
    «Рабочий» (умолчание), мусор в списке — отброшен.
    """
    data = value if isinstance(value, dict) else {}
    level = str(data.get("level") or "").strip().lower()
    if level not in LEVELS:
        level = DEFAULT_LEVEL
    never = [str(m).strip() for m in (data.get("never") or [])
             if isinstance(m, str) and str(m).strip()][:MAX_NEVER] \
        if isinstance(data.get("never"), list) else []
    return {"level": level, "never": never, "updated_at": int(data.get("updated_at") or 0)}


def validate(value) -> dict:
    """Проверить присланную настройку уровня: ``{level, never?}`` → запись комнаты."""
    data = value if isinstance(value, dict) else {}
    level = str(data.get("level") or "").strip().lower()
    if level not in LEVELS:
        raise AutonomyError("Уровень автономии: нужно careful, working или free",
                            detail={"field": "level", "allowed": list(LEVELS)})
    never = data.get("never")
    if never is None:
        masks = list(NEVER_DEFAULTS)
    else:
        if not isinstance(never, list):
            raise AutonomyError("Поле never должно быть списком масок",
                                detail={"field": "never", "type": type(never).__name__})
        masks = [_clean_mask(m) for m in never][:MAX_NEVER]
    return {"level": level, "never": masks, "updated_at": _now()}


def room_settings(room: dict) -> dict:
    """Настройка уровня у комнаты (нет записи — «Рабочий» и умолчания «только с моего да»)."""
    block = normalize((room or {}).get("autonomy"))
    if not block.get("never"):
        block["never"] = list(NEVER_DEFAULTS)
    return block


# ---------------------------------------------------------------- файлы состояния

def applied_path(cfg) -> Path:
    return Path(cfg.state_dir) / "autonomy-applied.json"


def journal_path(cfg, room_id) -> Path:
    return Path(cfg.state_dir) / str(room_id) / "autonomy-journal.jsonl"


def backups_dir(cfg) -> Path:
    return Path(cfg.data_dir) / "backups" / "autonomy"


def _read_applied(cfg) -> dict:
    try:
        data = sf.read_json(applied_path(cfg), {})
    except Exception:                       # noqa: BLE001 - учёт вторичен
        return {"rooms": {}}
    if not isinstance(data, dict):
        return {"rooms": {}}
    rooms = data.get("rooms")
    return {"rooms": rooms if isinstance(rooms, dict) else {}}


def _save_applied(cfg, data: dict) -> None:
    path = applied_path(cfg)
    path.parent.mkdir(parents=True, exist_ok=True)
    data["updated_at"] = _now()
    with sf.lock_for(path):
        sf.write_json_atomic(path, data)


def _ledger(cfg, room_id) -> dict:
    entry = (_read_applied(cfg)["rooms"].get(str(room_id)) or {})
    return entry if isinstance(entry, dict) else {}


def _ledger_profiles(cfg, room_id) -> dict:
    profiles = _ledger(cfg, room_id).get("profiles")
    return profiles if isinstance(profiles, dict) else {}


# ---------------------------------------------------------------- конфиг профиля

def _read_keys(path: Path) -> tuple[dict, str]:
    """Прочитать три ключа конфига и сказать, ЧЕМ читали: ``(данные, "yaml"|"raw"|"none")``.

    Почему не «просто yaml»: сервер «Комнаты команды» — stdlib Python, PyYAML в нём
    необязателен. Без запасного разбора `profile_state` молча возвращал пустой список
    разрешений, и перепроверка после записи объявляла «не записалось» там, где Hermes
    записал всё (боевой случай 19.11: `ok:false`, `allowlist_size 0` при заполненном
    `command_allowlist`). Запасной разбор читает ТОТ ЖЕ файл текстом, поэтому его ответ —
    подтверждение файла, а не догадка.
    """
    path = Path(path)
    if not path.is_file():
        return {}, "none"
    try:
        import yaml                                            # noqa: PLC0415 - необязателен
        with open(path, "r", encoding="utf-8-sig") as fh:
            data = yaml.safe_load(fh) or {}
        if isinstance(data, str):                # конфиг из одной строки-скаляра
            data = {}
        if isinstance(data, dict):
            return data, "yaml"
    except Exception:                                          # noqa: BLE001 - ниже текстовый разбор
        pass
    try:
        text = path.read_text(encoding="utf-8-sig", errors="replace")
    except OSError:                                            # pragma: no cover - файл исчез
        return {}, "none"
    return _parse_keys_text(text), "raw"


def _parse_keys_text(text: str) -> dict:
    """Разбор БЕЗ PyYAML: ``command_allowlist`` и ``approvals.{deny, single_query_mode}``.

    Понимает только те формы, которые пишет сам Hermes: список строками и скаляры в
    секции ``approvals``. Всё остальное просто не попадает в ответ (не выдумываем).
    """
    allow: list[str] = []
    deny: list[str] = []
    section = ""                     # текущая секция верхнего уровня ("", "approvals", …)
    list_target = ""                 # какой список сейчас наполняется: "allow"/"deny"/""
    for raw_line in str(text or "").splitlines():
        line = raw_line.rstrip()
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        indent = len(line) - len(line.lstrip(" \t"))
        stripped = line.strip()
        # Элемент списка: PyYAML пишет его и с отступом, и БЕЗ него (``- ключ`` под
        # `command_allowlist:` идёт с indent 0) — поэтому проверяем раньше секций.
        if stripped.startswith("-") and list_target:
            value = stripped[1:].strip().strip('"').strip("'")
            if value:
                (allow if list_target == "allow" else deny).append(value)
            continue
        if indent == 0:
            name, _, rest = stripped.partition(":")
            section = name.strip()
            list_target = ""
            value = rest.strip()
            if section == "command_allowlist":
                if value.startswith("["):            # `command_allowlist: []` / `["a", "b"]`
                    allow.extend(_split_inline_list(value))
                elif value:
                    allow.extend(_split_inline_list(value))
                else:
                    list_target = "allow"
            continue
        if section == "approvals" and indent > 0:
            name, _, rest = stripped.partition(":")
            key = name.strip()
            value = rest.strip().strip('"').strip("'")
            if key in ("deny", "never"):
                if value.startswith("["):
                    deny = _split_inline_list(value)
                    list_target = ""
                elif value:
                    deny = _split_inline_list(value)
                    list_target = "deny"
                else:
                    deny = []
                    list_target = "deny"
                continue
            if key == "single_query_mode":
                continue                 # значение забирает `_parse_single_query_mode`
            list_target = ""
            continue
    out: dict = {"command_allowlist": allow}
    approvals: dict = {"deny": deny}
    mode = _parse_single_query_mode(text)
    if mode is not None:
        approvals["single_query_mode"] = mode
    out["approvals"] = approvals
    return out


def _parse_single_query_mode(text: str) -> str | None:
    """``approvals.single_query_mode`` текстом: значение или ``None``, если ключа нет."""
    inside = False
    for raw_line in str(text or "").splitlines():
        if not raw_line.strip() or raw_line.lstrip().startswith("#"):
            continue
        indent = len(raw_line) - len(raw_line.lstrip(" \t"))
        stripped = raw_line.strip()
        if indent == 0:
            inside = stripped.startswith("approvals:")
            continue
        if inside and stripped.startswith("single_query_mode:"):
            value = stripped.split(":", 1)[1].strip().strip('"').strip("'")
            return value.lower()
    return None


def _split_inline_list(value: str) -> list[str]:
    """Разобрать ``[]`` / ``["a", 'b']`` / ``[a, b]`` в список строк (без PyYAML)."""
    text = str(value or "").strip()
    if not (text.startswith("[") and text.endswith("]")):
        return [text] if text else []
    body = text[1:-1].strip()
    if not body:
        return []
    out: list[str] = []
    for chunk in body.split(","):
        item = chunk.strip().strip('"').strip("'").strip()
        if item:
            out.append(item)
    return out


def profile_state(cfg, profile: str, home=None) -> dict:
    """Текущее состояние трёх ключей профиля (только чтение, без Hermes).

    ``exists`` — есть ли ``config.yaml``; ``allowlist``/``deny`` — списки строк;
    ``single_query_mode`` — как записано (``""`` — ключа нет); ``read_by`` — чем прочитан
    файл (``yaml``/``raw``/``none``).

    ``home`` — ЯВНЫЙ домашний каталог профиля: после записи перепроверка обязана читать
    тот же файл, в который писала (его вернул исполнитель записи), а не пересчитывать
    путь. Профиль ``default`` живёт в КОРНЕ дома Hermes, остальные — в ``profiles/<имя>``;
    это считает ``approvals.profile_home``.
    """
    if home is not None and str(home).strip():
        base = Path(str(home))
        path = base / "config.yaml"
    else:
        base = ap.profile_home(cfg, profile)
        path = ap.config_path(cfg, profile)
    out = {"profile": str(profile), "home": str(base),
           "exists": path.is_file(), "config": str(path),
           "allowlist": [], "deny": [], "single_query_mode": "", "read_by": "none"}
    if not out["exists"]:
        return out
    data, read_by = _read_keys(path)
    out["read_by"] = read_by
    if isinstance(data, dict):
        raw = data.get("command_allowlist")
        if isinstance(raw, str):
            raw = _split_inline_list(raw)
        if isinstance(raw, list):
            out["allowlist"] = [str(item) for item in raw if isinstance(item, str)]
        approvals = data.get("approvals")
        if isinstance(approvals, dict):
            deny = approvals.get("deny")
            if isinstance(deny, str):
                deny = _split_inline_list(deny)
            if isinstance(deny, list):
                out["deny"] = [str(item) for item in deny if isinstance(item, str)]
            out["single_query_mode"] = str(approvals.get("single_query_mode") or "").strip().lower()
    return out


# ---------------------------------------------------------------- предпросмотр

def _add_text(profile: str, key: str) -> str:
    return "«%s»: в список разрешений добавится «%s»" % (profile, key)


def _remove_text(profile: str, key: str) -> str:
    return "«%s»: из списка разрешений уйдёт «%s» (запись Team Room)" % (profile, key)


def plan_profile(level: str, never: list[str], current: dict, previous: dict | None) -> dict:
    """Что именно изменится в одном профиле: ``{add, remove, deny, sqm, changes}``.

    ``previous`` — учёт Team Room по этому профилю (``allowlist_added``, ``deny_before``,
    ``single_query_mode_before``). Ничего не пишет: только считает.
    """
    prev = previous if isinstance(previous, dict) else {}
    ours_allow = [str(k) for k in (prev.get("allowlist_added") or []) if isinstance(k, str)]
    deny_before = [str(k) for k in (prev.get("deny_before") or []) if isinstance(k, str)]
    current_allow = [str(k) for k in (current.get("allowlist") or [])]
    current_deny = [str(k) for k in (current.get("deny") or [])]
    name = str(current.get("profile") or "")
    plan = {"add": [], "remove": [], "deny": None, "sqm": None, "changes": []}

    if level == "careful":
        plan["remove"] = [k for k in ours_allow if k in current_allow]
        plan["deny"] = [m for m in current_deny if m not in [
            str(x) for x in (prev.get("deny_added") or [])]] if current_deny else []
        plan["sqm"] = "deny"
    elif level == "working":
        plan["add"] = [k for k in WORKING_RULES if k not in current_allow]
        plan["deny"] = [m for m in current_deny if m not in [
            str(x) for x in (prev.get("deny_added") or [])]]
        plan["sqm"] = "deny"
    else:                                   # free
        plan["add"] = [k for k in WORKING_RULES if k not in current_allow]
        # Свой список «только с моего да» НЕ затираем: чужие записи в approvals.deny
        # остаются (уровень только добавляет к ним умолчания).
        merged = list(dict.fromkeys([str(m) for m in (never or NEVER_DEFAULTS)] + current_deny))
        plan["deny"] = merged
        plan["sqm"] = "approve"

    if plan["deny"] is not None and plan["deny"] == current_deny:
        plan["deny"] = None                 # менять нечего — не пишем ключ зря
    if plan["sqm"] is not None and plan["sqm"] == current.get("single_query_mode"):
        plan["sqm"] = None

    for key in plan["add"]:
        plan["changes"].append(_add_text(name, key))
    for key in plan["remove"]:
        plan["changes"].append(_remove_text(name, key))
    if plan["deny"] is not None:
        plan["changes"].append("«%s»: «только с моего да» — %d записей (approvals.deny)"
                               % (name, len(plan["deny"])))
    if plan["sqm"] is not None:
        was = current.get("single_query_mode") or "(как в Hermes)"
        plan["changes"].append("«%s»: запуски без человека — %s → %s"
                               % (name, was, plan["sqm"]))
    return plan


def room_profiles(room: dict) -> list[str]:
    """Профили комнаты: руководитель, критик и участники (без повторов)."""
    room = room if isinstance(room, dict) else {}
    out: list = []
    values = [room.get("orchestrator"), room.get("critic")] + \
             [m.get("profile") for m in (room.get("members") or []) if isinstance(m, dict)]
    for value in values:
        name = str(value or "").strip()
        if name and name not in out:
            out.append(name)
    return out


def preview(cfg, room: dict, level: str | None = None, never: list[str] | None = None) -> dict:
    """Что изменится на каждом уровне — ДО подтверждения (§2, ``preview``)."""
    room = room if isinstance(room, dict) else {}
    profiles = room_profiles(room)
    current = {name: profile_state(cfg, name) for name in profiles}
    ledger_profiles = _ledger_profiles(cfg, room.get("id") or "")
    never_list = [str(m) for m in (never if never is not None else
                                   room_settings(room).get("never") or list(NEVER_DEFAULTS))]
    out = {}
    for candidate in LEVELS:
        changes: list[str] = []
        rows: list[dict] = []
        for name in profiles:
            plan = plan_profile(candidate, never_list, current[name],
                                ledger_profiles.get(name) or {})
            changes.extend(plan["changes"])
            rows.append({"profile": name, "add": plan["add"], "remove": plan["remove"],
                         "deny": None if plan["deny"] is None else len(plan["deny"]),
                         "single_query_mode": plan["sqm"],
                         "missing": not current[name]["exists"]})
        out[candidate] = {"level": candidate, "label": LEVEL_LABEL[candidate],
                          "changes": changes, "profiles": rows,
                          "unchanged": not changes}
    if level:
        out = {"level": level, "preview": out}
    else:
        out = {"preview": out}
    return out


# ---------------------------------------------------------------- запись в профиль

#: Подставной исполнитель записи (тесты): функция ``(cfg, request) -> dict``.
_writer = None
#: Подставной CLI Hermes (тесты, проба): функция ``(args) -> {code,out,err}``.
_cli_runner = None


def set_writer(writer) -> None:
    """Подменить запись в профиль (тесты). ``None`` — вернуть штатную (Hermes)."""
    global _writer
    _writer = writer


def reset_writer() -> None:
    set_writer(None)


def set_cli_runner(runner) -> None:
    """Подменить CLI Hermes (`approvals suggest|test`). ``None`` — вернуть настоящий."""
    global _cli_runner
    _cli_runner = runner


def reset_cli_runner() -> None:
    set_cli_runner(None)


def write_profile(cfg, request: dict) -> dict:
    """Применить изменение настроек профиля штатным путём Hermes (``approval_grant``).

    Штатная запись идёт отдельным процессом интерпретатора Hermes
    (``approvals.run_worker``); в тестах и пробе исполнитель подменяется.
    """
    request = dict(request or {})
    if _writer is not None:
        return _writer(cfg, request)
    return ap.run_worker(cfg, request)


def backup_config(cfg, room_id, profile: str) -> dict:
    """Копия старого ``config.yaml`` профиля перед правкой (``data/backups/autonomy``)."""
    source = ap.config_path(cfg, profile)
    folder = backups_dir(cfg)
    folder.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime())
    name = "%s-%s-%s.yaml" % (str(room_id or "room"), str(profile or "profile"), stamp)
    target = folder / name
    if source.is_file():
        shutil.copyfile(str(source), str(target))
    else:
        target.write_text("# профиль без config.yaml на момент копии\n", encoding="utf-8")
    try:
        rel = target.relative_to(Path(cfg.data_dir)).as_posix()
    except ValueError:                      # pragma: no cover - data_dir всегда родитель
        rel = str(target)
    return {"path": str(target), "rel": rel, "at": _now()}


def _same(left, right) -> bool:
    """Одинаковые записи списка? Сравнение без регистра и лишних пробелов.

    Hermes хранит классы правил как человеческие строки; регистр/пробелы в них может
    отличаться от того, что мы просили, — это та же запись, а не «не записалось».
    """
    return " ".join(str(left or "").split()).casefold() == " ".join(str(right or "").split()).casefold()


def _has_key(keys, key) -> bool:
    """Есть ли ``key`` в списке записей (сравнение ``_same``)."""
    return any(_same(item, key) for item in (keys or []))


def _mode_ok(want, have) -> bool:
    """Совпадает ли записанный режим с желаемым (``deny`` — умолчание Hermes).

    Hermes не пишет в файл значения, равные умолчанию: ``single_query_mode: deny`` может
    отсутствовать — поведение при этом то же («без человека — спрашивать»), поэтому это
    не «не записалось».
    """
    target = str(want or "").strip().lower()
    got = str(have or "").strip().lower()
    return target == got or (target == "deny" and got == "")


def _verify(profile: str, after: dict, plan: dict, reported: dict | None = None) -> list[str]:
    """Сверить, что Hermes записал именно то, что просили (список замечаний).

    Читается ФАЙЛ, в который писала запись (``after`` собран из него же). Если файл
    перечитать не удалось (``read_by: none``), но сама запись прочитала его сразу после
    записи и видит ключи (``reported``), говорим «записано, перечитать не удалось» —
    «не записалось» без подтверждения файла не пишем.
    """
    notes: list[str] = []
    allow = after.get("allowlist") or []
    was_read = str(after.get("read_by") or "") != "none"
    reported_allow = (reported or {}).get("allowlist") or []
    for key in plan.get("add") or []:
        if _has_key(allow, key):
            continue
        if not was_read and _has_key(reported_allow, key):
            notes.append("«%s»: разрешение «%s» записано, но перечитать файл не удалось"
                         % (profile, key))
            continue
        notes.append("«%s»: разрешение «%s» не записалось" % (profile, key))
    for key in plan.get("remove") or []:
        if _has_key(allow, key):
            notes.append("«%s»: запись «%s» осталась в списке" % (profile, key))
    if plan.get("sqm") and not _mode_ok(plan["sqm"], after.get("single_query_mode")):
        if not was_read and _mode_ok(plan["sqm"], (reported or {}).get("single_query_mode")):
            notes.append("«%s»: «запуски без человека» записаны, перечитать файл не удалось"
                         % profile)
        else:
            notes.append("«%s»: «запуски без человека» = «%s» вместо «%s»"
                         % (profile, after.get("single_query_mode") or "нет", plan["sqm"]))
    if plan.get("deny") is not None:
        want = [str(m) for m in plan["deny"]]
        have = [str(m) for m in (after.get("deny") or [])]
        if len(want) != len(have) or not all(_has_key(have, mask) for mask in want):
            notes.append("«%s»: список «только с моего да» записался не целиком" % profile)
    return notes


def apply_level(cfg, room: dict, level: str, never=None, confirm: bool = False,
                now: int | None = None) -> dict:
    """Применить уровень к профилям комнаты (§2). Без ``confirm`` — только предпросмотр.

    Возвращает ``{ok, level, never, applied[], profiles[], warnings[], preview}``;
    без ``confirm`` — ``AutonomyError(409, confirm_required)`` с предпросмотром.
    """
    now = _now() if now is None else int(now)
    room = room if isinstance(room, dict) else {}
    room_id = str(room.get("id") or "")
    if not room_id:
        raise AutonomyError("Непонятно, в какой комнате менять уровень",
                            detail={"reason": "no_room"})
    block = validate({"level": level, "never": never})
    current_never = block["never"]
    if not confirm:
        shots = preview(cfg, room, never=current_never)
        raise AutonomyError(
            "Уровень автономии меняет настройки агентов комнаты — подтвердите",
            409, "confirm_required",
            {"preview": shots["preview"], "level": block["level"],
             "room": room_id, "confirm": True})
    profiles = room_profiles(room)
    if not profiles:
        raise AutonomyError("В комнате нет участников — менять нечего", 409,
                            "no_profiles", {"room": room_id})
    ledger = _read_applied(cfg)
    entry = ledger["rooms"].get(room_id)
    entry = dict(entry) if isinstance(entry, dict) else {}
    applied_profiles = dict(entry.get("profiles") or {})
    applied: list[dict] = []
    warnings: list[str] = []
    for name in profiles:
        current = profile_state(cfg, name)
        previous = applied_profiles.get(name) or {}
        if not current["exists"]:
            applied.append({"profile": name, "ok": False, "changes": [],
                            "error": "профиль не найден (нет config.yaml)",
                            "skipped": True})
            warnings.append("«%s»: профиль не найден — пропущен" % name)
            continue
        plan = plan_profile(block["level"], current_never, current, previous)
        if not plan["changes"]:
            applied_profiles[name] = _record_plan(name, block["level"], plan, current,
                                                 previous, now, backup=None)
            applied.append({"profile": name, "ok": True, "changes": [], "unchanged": True})
            continue
        backup = backup_config(cfg, room_id, name)
        request = {"op": "apply", "home": current["home"],
                   "allowlist_add": plan["add"], "allowlist_remove": plan["remove"],
                   "deny": plan["deny"], "single_query_mode": plan["sqm"]}
        try:
            result = write_profile(cfg, request)
        except Exception as exc:            # noqa: BLE001 - отчёт по профилю, не общий сбой
            code = str(getattr(exc, "code", "") or "failed")
            message = str(getattr(exc, "message", "") or exc)[:300]
            applied.append({"profile": name, "ok": False, "changes": plan["changes"],
                            "error": message, "code": code,
                            "backup": backup["rel"]})
            warnings.append("«%s»: %s" % (name, message))
            continue
        written_home = str((result or {}).get("home") or "") or current["home"]
        # Перечитываем ТОТ ЖЕ файл, в который писала запись (её же домашний каталог), и
        # тем же путём, что считает `profile_home` (default — корень дома Hermes).
        after = profile_state(cfg, name, home=written_home)
        notes = _verify(name, after, plan, result if isinstance(result, dict) else None)
        warnings.extend(notes)
        applied_profiles[name] = _record_plan(name, block["level"], plan, current,
                                             previous, now, backup=backup["rel"])
        applied.append({"profile": name, "ok": not notes, "changes": plan["changes"],
                        "warnings": notes, "backup": backup["rel"],
                        "home": written_home, "config": after.get("config") or "",
                        "read_by": after.get("read_by") or "",
                        "single_query_mode": after.get("single_query_mode"),
                        "allowlist_size": len(after.get("allowlist") or []),
                        "deny_size": len(after.get("deny") or [])})
        _ = result
    entry.update({"level": block["level"], "never": current_never, "updated_at": now,
                  "profiles": applied_profiles})
    ledger["rooms"][room_id] = entry
    _save_applied(cfg, ledger)
    # реестр комнат — только после профилей: упавшая запись не должна врать в настройках
    _save_room_block(cfg, room_id, {"level": block["level"], "never": current_never,
                                    "updated_at": now})
    journal_add(cfg, room_id, {
        "kind": "level", "at": now, "level": block["level"],
        "profile": "", "class": "", "card": "",
        "text": "Уровень автономии → «%s»; профилей изменено: %d"
                % (LEVEL_LABEL[block["level"]], len([a for a in applied if a.get("changes")]))})
    return {"ok": True, "room": room_id, "level": block["level"],
            "level_label": LEVEL_LABEL[block["level"]], "never": current_never,
            "applied": applied, "warnings": warnings, "at": now,
            "message": _apply_message(applied, block["level"])}


def _record_plan(name: str, level: str, plan: dict, current: dict, previous: dict,
                 now: int, backup) -> dict:
    """Учёт Team Room по профилю: что именно добавили МЫ (для честного отзыва)."""
    added_before = [str(k) for k in (previous.get("allowlist_added") or []) if isinstance(k, str)]
    added_now = [k for k in dict.fromkeys(added_before + list(plan["add"]))
                 if k not in (plan["remove"] or [])]
    deny_before = [str(x) for x in (previous.get("deny_before")
                                    or current.get("deny") or [])]
    if plan["deny"] is None:
        deny_added = [str(k) for k in (previous.get("deny_added") or []) if isinstance(k, str)]
    else:
        deny_added = [m for m in plan["deny"] if m not in deny_before]
    record = {"level": level, "at": now, "allowlist_added": added_now,
              "allowlist_before": list(previous.get("allowlist_before")
                                       or current.get("allowlist") or []),
              "deny_before": deny_before,
              "deny_added": deny_added,
              "single_query_mode_before": previous.get("single_query_mode_before",
                                                       current.get("single_query_mode") or "")}
    if backup:
        record["backup"] = backup
    return record


def _apply_message(applied: list[dict], level: str) -> str:
    changed = [a for a in applied if a.get("changes")]
    failed = [a for a in applied if a.get("ok") is False]
    text = "Уровень «%s»: изменено профилей — %d" % (LEVEL_LABEL.get(level, level), len(changed))
    if failed:
        text += ", не удалось — %d" % len(failed)
    return text + "."


def _save_room_block(cfg, room_id: str, block: dict) -> None:
    """Записать ``autonomy`` в реестр комнат (тот же замок и атомарная запись)."""
    try:
        from . import rooms as rooms_mod
    except ImportError:                     # pragma: no cover - плоский импорт
        import rooms as rooms_mod           # type: ignore
    try:
        rooms_mod.update_room(cfg, room_id, {"autonomy": block})
    except rooms_mod.RoomsError as exc:
        raise AutonomyError(exc.message, 409, "room_update_failed",
                            dict(exc.detail or {})) from exc


# ---------------------------------------------------------------- чтение для API

def view(cfg, room: dict) -> dict:
    """``GET /api/rooms/{id}/autonomy`` — уровень, профили, умолчания, предпросмотр."""
    room = room if isinstance(room, dict) else {}
    settings = room_settings(room)
    applied = _ledger(cfg, room.get("id") or "")
    profiles = []
    for name in room_profiles(room):
        state = profile_state(cfg, name)
        profiles.append({"profile": name,
                         "label": _label(room, name),
                         "exists": state["exists"],
                         "allowlist": state["allowlist"],
                         "deny": state["deny"],
                         "single_query_mode": state["single_query_mode"] or "deny",
                         "read_by": state["read_by"],
                         "auto_allowed": [k for k in WORKING_RULES
                                          if k in state["allowlist"]]})
    shots = preview(cfg, room, never=settings.get("never"))
    return {"room": room.get("id") or "", "level": settings["level"],
            "level_label": LEVEL_LABEL[settings["level"]],
            "never": settings["never"],
            "updated_at": int(settings.get("updated_at") or 0),
            "applied_level": str(applied.get("level") or ""),
            "applied_at": int(applied.get("updated_at") or 0),
            "applies_to": [p["profile"] for p in profiles],
            "profiles": profiles,
            "defaults": {"never": list(NEVER_DEFAULTS),
                         "working_rules": list(WORKING_RULES)},
            "never_labels": {m: NEVER_LABELS.get(m, m) for m in settings["never"]},
            "levels": [{"level": key, "label": LEVEL_LABEL[key],
                        "hint": LEVEL_HINT[key]} for key in LEVELS],
            "preview": shots["preview"],
            "at": _now()}


LEVEL_HINT = {
    "careful": "Агент сам только читает и ищет; любой запуск скрипта и любая опасная "
               "команда — вопрос к вам.",
    "working": "Агент сам запускает свои скрипты и работает в папке комнаты; удаление, "
               "запись в реестр, sudo, отправка наружу — вопрос к вам.",
    "free": "Агент делает почти всё сам; блокируется только список «только с моего да» "
            "— и в этом режиме спросить нельзя: агент задаст вам вопрос в запросе.",
}


def _label(room: dict, profile: str) -> str:
    try:
        from . import worklog as wl
    except ImportError:                     # pragma: no cover - плоский импорт
        import worklog as wl                # type: ignore
    try:
        return wl._label_of(room, profile)
    except Exception:                       # noqa: BLE001 - подпись вторична
        return str(profile or "участник")


# ---------------------------------------------------------------- журнал

def journal_add(cfg, room_id, entry: dict) -> None:
    """Строка журнала «что агент сделал сам по уровню». Сбой журнала ничего не ломает."""
    try:
        path = journal_path(cfg, room_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        row = {"at": int(entry.get("at") or _now()), "kind": str(entry.get("kind") or "auto"),
               "level": str(entry.get("level") or ""), "profile": str(entry.get("profile") or ""),
               "class": str(entry.get("class") or "")[:160],
               "card": str(entry.get("card") or "")[:120],
               "text": " ".join(str(entry.get("text") or "").split())[:600],
               "severity": str(entry.get("severity") or "routine")}
        with sf.lock_for(path):
            sf.append_jsonl(path, row)
    except Exception:                       # noqa: BLE001 - журнал вторичен
        pass


def journal(cfg, room: dict, limit: int = 60) -> dict:
    """``GET /api/rooms/{id}/autonomy/journal`` — журнал, новые сверху, без счётчиков."""
    room_id = str((room or {}).get("id") or "")
    try:
        rows = sf.read_jsonl(journal_path(cfg, room_id))
    except Exception:                       # noqa: BLE001 - журнала может не быть
        rows = []
    items = [r for r in rows if isinstance(r, dict)]
    items.sort(key=lambda r: int(r.get("at") or 0), reverse=True)
    try:
        limit = max(1, min(JOURNAL_MAX, int(limit)))
    except (TypeError, ValueError):
        limit = 60
    out = items[:limit]
    return {"room": room_id, "items": out, "count": len(out), "total": len(items),
            "level": room_settings(room)["level"],
            "note": "Что агенты сделали сами по уровню. Это не «нужно от вас»: "
                    "счётчик здесь не растёт.",
            "at": _now()}


def note_decision(cfg, room: dict, item: dict, decision: str) -> None:
    """Записать решение по блокировке в журнал уровня (зовётся из ``approvals``).

    Строка честная: если класс уже разрешён уровнем комнаты, помечаем ``auto`` —
    «уровень пропустил бы это сам»; иначе это решение человека.
    """
    try:
        room = room if isinstance(room, dict) else {}
        level = room_settings(room)["level"]
        pattern_key = str(item.get("pattern_key") or "")
        severity = severity_of(pattern_key, str(item.get("command") or ""),
                               bool(item.get("floor")))
        auto = severity == "routine" and level in ("working", "free")
        journal_add(cfg, room.get("id") or "", {
            "kind": "decision", "level": level, "profile": str(item.get("profile") or ""),
            "class": str(item.get("description") or pattern_key or "команда"),
            "card": str(item.get("card_title") or item.get("task_id") or ""),
            "severity": severity,
            "text": "%s: %s%s" % ("уровень пропустил бы сам" if auto else "решено человеком",
                                  item.get("description") or pattern_key or "команда",
                                  " — «%s»" % decision if decision else "")})
    except Exception:                       # noqa: BLE001 - журнал не должен ломать решение
        pass


# ---------------------------------------------------------------- подсказки (CLI)

def cli_bin(cfg=None) -> str:
    """Штатный CLI Hermes (`hermes`); в тестах и пробе — подставной."""
    return str(getattr(cfg, "hermes_bin", None) or os.environ.get("TEAMROOM_HERMES_BIN")
               or "hermes")


def cli_runner(cfg):
    """Исполнитель CLI: подставной, если задан, иначе подставной из песочницы, иначе Hermes."""
    return _cli_runner or _fake_runner(cfg) or _default_runner(cfg)


#: Переменная песочницы/тестов: подставные подсказки без вызова настоящего `hermes`.
#: ``TEAMROOM_SUGGEST_FAKE=1`` — встроенный пример, ``=путь/к.json`` — свой ответ.
SUGGEST_FAKE_ENV = "TEAMROOM_SUGGEST_FAKE"

#: Встроенный ответ `hermes approvals suggest --json` для песочницы и тестов.
FAKE_SUGGEST = {
    "proposals": [
        {"pattern": "script execution via -e/-c flag", "kind": "glob", "count": 12,
         "classes": ["script execution"], "example": "python -c \"print(1)\""},
        {"pattern": "python", "kind": "glob", "count": 7, "classes": ["read"],
         "example": "python tools/build.py"},
        {"pattern": "git status", "kind": "literal", "count": 3, "classes": ["read"],
         "example": "git status"},
    ],
}


def fake_suggest_enabled() -> bool:
    """Включён ли подставной режим подсказок (``TEAMROOM_SUGGEST_FAKE``)."""
    return bool(str(os.environ.get(SUGGEST_FAKE_ENV) or "").strip())


def _fake_suggest_payload() -> dict:
    """Ответ подставного CLI: файл из переменной или встроенный пример."""
    raw = str(os.environ.get(SUGGEST_FAKE_ENV) or "").strip()
    if raw and raw not in ("1", "true", "yes", "on"):
        try:
            data = json.loads(Path(raw).read_text(encoding="utf-8-sig"))
            if isinstance(data, dict):
                return data
        except Exception:                   # noqa: BLE001 - файла нет/битый: берём пример
            pass
    return dict(FAKE_SUGGEST)


def _fake_runner(cfg):
    """Подставной CLI для песочницы: настоящий `hermes` не вызывается вовсе."""
    if not fake_suggest_enabled():
        return None
    payload = json.dumps(_fake_suggest_payload(), ensure_ascii=False)

    def run(args):
        verbs = [str(item) for item in (args or [])]
        if verbs[:2] == ["approvals", "suggest"]:
            return {"code": 0, "out": payload, "err": "", "fake": True}
        # `approvals test` в песочнице: вердикт по маске, без запуска Hermes.
        verdict = "allow" if "--" not in verbs else "ask"
        return {"code": 0, "out": json.dumps({"verdict": verdict}, ensure_ascii=False),
                "err": "", "fake": True}

    return run


def _default_runner(cfg):
    """Настоящий вызов CLI: только `approvals suggest|test`, без shell, с таймаутом."""
    binary = cli_bin(cfg)
    home = str(ap.hermes_root(cfg))

    def run(args):
        env = dict(os.environ)
        if home:
            env["HERMES_HOME"] = home
        try:
            import subprocess
            try:
                from . import proc as proc_mod
            except ImportError:             # pragma: no cover - плоский импорт
                import proc as proc_mod     # type: ignore
            done = proc_mod.run([binary] + list(args), capture_output=True, text=True,
                                timeout=CLI_TIMEOUT_S, env=env)
        except FileNotFoundError:
            return {"code": 127, "out": "", "err": "команда hermes не найдена"}
        except Exception as exc:            # noqa: BLE001 - таймаут и прочее
            return {"code": 124, "out": "", "err": str(exc)[:200]}
        return {"code": int(getattr(done, "returncode", 1) or 0),
                "out": str(getattr(done, "stdout", "") or ""),
                "err": str(getattr(done, "stderr", "") or "")}

    return run


def _json_of(text: str):
    """Последний JSON-объект в выводе CLI (текст перед ним не мешает)."""
    text = str(text or "").strip()
    if not text:
        return None
    try:
        return json.loads(text)
    except ValueError:
        pass
    start = text.find("{")
    end = text.rfind("}")
    if start >= 0 and end > start:
        try:
            return json.loads(text[start:end + 1])
        except ValueError:
            return None
    return None


def suggest(cfg, room: dict, days: int = 90, limit: int = 10) -> dict:
    """``GET …/autonomy/suggest`` — обёртка ``hermes approvals suggest`` (только чтение).

    Ничего не пишет: предлагает записи для списка разрешений по истории «разрешить».
    Разрушительные классы Hermes не предлагает никогда — это его собственное правило.
    """
    room = room if isinstance(room, dict) else {}
    profiles = room_profiles(room)
    profile = profiles[0] if profiles else ""
    args = ["approvals", "suggest", "--json", "--days", str(max(0, int(days))),
            "--limit", str(max(1, min(50, int(limit))))]
    try:
        res = cli_runner(cfg)(args)
    except Exception as exc:                # noqa: BLE001 - подсказки необязательны
        return {"room": room.get("id") or "", "available": False, "items": [],
                "message": "Подсказки сейчас недоступны: %s" % str(exc)[:160], "at": _now()}
    code = int(res.get("code") or 0)
    data = _json_of(res.get("out") or "")
    if code != 0 or not isinstance(data, dict):
        reason = " ".join(str(res.get("err") or "").split())[:200] or "нет ответа"
        return {"room": room.get("id") or "", "available": False, "items": [],
                "message": "Подсказки сейчас недоступны: %s" % reason,
                "command": " ".join([cli_bin(cfg)] + args), "at": _now()}
    current = profile_state(cfg, profile) if profile else {"allowlist": []}
    known = {str(k).lower() for k in current.get("allowlist") or []}
    items = []
    for row in data.get("proposals") or []:
        if not isinstance(row, dict):
            continue
        pattern = str(row.get("pattern") or "").strip()
        if not pattern:
            continue
        items.append({"pattern": pattern, "kind": str(row.get("kind") or "glob"),
                      "count": int(row.get("count") or 0),
                      "classes": [str(c) for c in (row.get("classes") or [])][:5],
                      "example": str(row.get("example") or ""),
                      "already": pattern.lower() in known,
                      "severity": severity_of(pattern, str(row.get("example") or "")),
                      "text": "«%s» — одобряли %d раз(а)" % (pattern, int(row.get("count") or 0))})
    return {"room": room.get("id") or "", "profile": profile, "available": True,
            "items": items, "count": len(items),
            "days": int(days),
            "source": "hermes" if _cli_runner is not None else ("fake" if fake_suggest_enabled() else "hermes"),
            "message": ("Нашлось предложений: %d" % len(items)) if items else
                       "По истории пока нечего предложить — опасные классы Hermes не предлагает.",
            "at": _now()}


def probe(cfg, room: dict | None = None, commands: list | None = None,
          kind: str = "terminal") -> dict:
    """``hermes approvals test`` по набору команд — сухая проверка вердикта.

    Ничего не исполняет: спрашивает у Hermes (в пробе — у подставного CLI), что он
    сделает с командой. Возвращает вердикты ``allow | ask | deny``.
    """
    args_common = ["approvals", "test", "--json", "--"]
    rows = []
    for command in (commands or []):
        text = str(command)
        try:
            res = cli_runner(cfg)(args_common + [text])
        except Exception as exc:            # noqa: BLE001 - проба не должна падать
            rows.append({"command": text, "verdict": "error", "exit": 1,
                         "detail": str(exc)[:200]})
            continue
        data = _json_of(res.get("out") or "") or {}
        verdict = str(data.get("verdict") or "").strip().lower()
        if verdict not in ("allow", "ask", "deny"):
            verdict = {0: "allow", 2: "ask", 3: "deny"}.get(int(res.get("code") or 0), "error")
        rows.append({"command": text,
                     "verdict": verdict,
                     "exit": int(data.get("exit_code") or res.get("code") or 0),
                     "rule": str(data.get("rule") or ""),
                     "detail": str(data.get("detail") or "")[:300],
                     "severity": severity_of(None, text)})
    summary = {"allow": 0, "ask": 0, "deny": 0, "error": 0}
    for row in rows:
        summary[row["verdict"]] = summary.get(row["verdict"], 0) + 1
    return {"ok": not summary["error"], "kind": kind, "commands": rows,
            "summary": summary, "at": _now()}
