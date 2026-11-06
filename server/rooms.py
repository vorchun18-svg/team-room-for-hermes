"""Реестр комнат: data/rooms.json — состав, участники, контекст проекта.

Комната = проект: своя доска/tenant, руководитель комнаты, критик и исполнители.
Правила валидации — docs/API.md §6.2 и ARCH §12 (members >= 1, orchestrator обязателен).
"""
from __future__ import annotations

import os
import re
import time
from pathlib import Path

try:                                    # import server.rooms
    from . import config as config_mod
    from . import phase_book as phase_mod
    from . import project_dir as pdir
    from . import statefiles as sf
except ImportError:                      # python server/app.py (плоский импорт)
    import config as config_mod         # type: ignore
    import phase_book as phase_mod      # type: ignore
    import project_dir as pdir          # type: ignore
    import statefiles as sf              # type: ignore

ROOMS_FILE = "rooms.json"
MEMBER_ROLES = ("worker", "critic", "observer")
MODES = ("auto",)
# «Проектный режим» (волна 9, решения A1/A2): настройки КОНКРЕТНОЙ комнаты.
LAUNCH_MODES = ("plan_first", "auto")          # «сначала план» | «сразу без подтверждения»
# W17-R §3 (решение Михаила 06.11): НОВАЯ комната сразу запускает части работы
# без подтверждения. У существующих комнат без поля режим прежний — «сначала
# план»: их настройки по решению Михаила не меняем.
DEFAULT_LAUNCH_MODE = "plan_first"
DEFAULT_LAUNCH_MODE_NEW = "auto"
DEFAULT_PARALLEL_STREAMS = 2
MIN_PARALLEL_STREAMS = 1
MAX_PARALLEL_STREAMS = 4
UPDATABLE = ("title", "icon", "orchestrator", "critic", "context_files", "project_dir",
             "mode_default", "critic_pass", "max_rounds",
             # Режим критика (W13-E §9): «N раундов» либо «до полного одобрения».
             "critic_mode", "critic_round_cap",
             "launch_mode", "max_parallel_streams", "project_mode",
             # Этапы проекта — свойство КОМНАТЫ (W17-R §1, B-8): необязательный
             # список `[{id, title}]`, правится в настройках комнаты.
             "phases",
             # Особые указания участникам этой комнаты (WAVE9 §9): только реестр комнаты,
             # файлы профилей (SOUL.md/config.yaml/skills) не меняются никогда.
             "agent_notes",
             # Журнал указаний (WAVE14 §13): кто/что/зачем/источник + откат изменения.
             "agent_notes_log",
             # Модель/провайдер/мышление участника ТОЛЬКО в этой комнате (WAVE10 §«Интерфейсы»).
             "agent_models")
IMMUTABLE = ("id", "board", "tenant")
DEFAULT_BOARD = "team"
DEFAULT_MAX_ROUNDS = 1
# Режим критика (W13-E §9): «rounds» — N раундов, «until_approved» — до полного
# одобрения критиком. Мягкий потолок страхует ночной цикл от бесконечности.
CRITIC_MODES = ("rounds", "until_approved")
DEFAULT_CRITIC_MODE = "rounds"
DEFAULT_CRITIC_ROUND_CAP = 12
MIN_CRITIC_ROUND_CAP = 2
MAX_CRITIC_ROUND_CAP = 30

# --- транслитерация (id комнаты из русского названия) ----------------------

_TRANSLIT = {
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "e", "ж": "zh",
    "з": "z", "и": "i", "й": "y", "к": "k", "л": "l", "м": "m", "н": "n", "о": "o",
    "п": "p", "р": "r", "с": "s", "т": "t", "у": "u", "ф": "f", "х": "h", "ц": "c",
    "ч": "ch", "ш": "sh", "щ": "sch", "ъ": "", "ы": "y", "ь": "", "э": "e", "ю": "yu",
    "я": "ya",
}
_SLUG_RE = re.compile(r"^[a-z][a-z0-9_-]{1,31}$")     # D16: минимум 2 символа
_BOARD_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
_ICON_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,39}$")
MAX_ROUNDS = 5
# D16: зарезервированные имена Windows — такой каталог (state/<id>, vault/<id>) не создаётся
RESERVED_IDS = frozenset(
    ["con", "prn", "aux", "nul"]
    + ["com%d" % i for i in range(1, 10)]
    + ["lpt%d" % i for i in range(1, 10)]
)


class RoomsError(Exception):
    """Ошибка работы с комнатами: code = bad_request|conflict|not_found."""

    def __init__(self, message: str, code: str = "bad_request", detail: dict | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.detail = detail or {}


def slugify(text: str) -> str:
    """Латинский слаг: «Лето» → «leto» (только a-z0-9_-)."""
    out = []
    for ch in str(text or "").strip().lower():
        if ch in _TRANSLIT:
            out.append(_TRANSLIT[ch])
        elif ch.isascii() and (ch.isalnum() or ch in "-_"):
            out.append(ch)
        else:
            out.append("-")
    slug = re.sub(r"[-_]{2,}", "-", "".join(out)).strip("-_")
    slug = slug[:32].strip("-_")
    if not slug or not slug[0].isalpha():
        slug = ("room-" + slug).strip("-")[:32]
    return slug


def _now() -> int:
    return int(time.time())


def _rooms_path(cfg) -> Path:
    return Path(cfg.data_dir) / ROOMS_FILE


def _load(cfg) -> dict:
    """Прочитать rooms.json. Битый — копия в ``.corrupt-<ts>``; занятый — ошибка (D4)."""
    path = _rooms_path(cfg)
    data, info = sf.read_json_doc(path, None)
    if info["state"] == "unreadable":
        raise sf.StateUnavailable(
            "Не удалось прочитать файл комнат «%s»: он занят другим процессом или нет прав. "
            "Данные не изменены — повторите через пару секунд." % path,
            {"path": str(path), "error": info.get("error")},
        )
    if info["state"] == "corrupt":
        sf.quarantine(path)          # копия ДО любой перезаписи
        data = None
    if not isinstance(data, dict) or not isinstance(data.get("rooms"), list):
        data = {"version": 1, "rooms": [], "updated_at": 0}
    return data


def _save(cfg, data: dict) -> None:
    data["updated_at"] = _now()
    sf.write_json_atomic(_rooms_path(cfg), data)


# --- валидация полей комнаты (D7/D16) --------------------------------------

def _check_id(room_id: str) -> str:
    if not _SLUG_RE.match(room_id):
        raise RoomsError(
            "id комнаты: латиница, цифры, - и _, 2..32 символа, начинается с буквы",
            "bad_request", {"field": "id", "value": room_id},
        )
    if room_id.lower() in RESERVED_IDS:
        raise RoomsError(
            "id «%s» зарезервирован Windows (con, prn, aux, nul, com1..9, lpt1..9) — выберите другой"
            % room_id, "bad_request", {"field": "id", "value": room_id, "reason": "reserved"},
        )
    return room_id


def _check_board(value) -> str:
    board = str(value or "").strip()
    if not _BOARD_RE.match(board):
        raise RoomsError("board: слаг доски из латиницы, цифр, - и _", "bad_request",
                         {"field": "board", "value": value})
    return board


def _check_icon(value) -> str:
    if value is None:
        return "folder"
    if not isinstance(value, str) or not _ICON_RE.match(value.strip()):
        raise RoomsError("icon: имя иконки (латиница, цифры, - и _)", "bad_request",
                         {"field": "icon", "value": value})
    return value.strip()


def _check_bool(value, field: str) -> bool:
    """Только настоящий bool: строка «false» — ошибка, а не False/True (D7)."""
    if not isinstance(value, bool):
        raise RoomsError("%s: ожидается true или false" % field, "bad_request",
                         {"field": field, "type": type(value).__name__})
    return value


def _check_rounds(value) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise RoomsError("max_rounds: целое число 1..%d" % MAX_ROUNDS, "bad_request",
                         {"field": "max_rounds", "value": value})
    if value < 1 or value > MAX_ROUNDS:
        raise RoomsError("max_rounds: от 1 до %d" % MAX_ROUNDS, "bad_request",
                         {"field": "max_rounds", "value": value, "max": MAX_ROUNDS})
    return int(value)


def _check_critic_mode(value) -> str:
    """Режим критика: «rounds» (N раундов) или «until_approved» (до одобрения).

    Пустое значение — прежнее поведение («rounds»): старые комнаты и клиенты
    продолжают работать без правок.
    """
    mode = str(value or "").strip()
    if not mode:
        return DEFAULT_CRITIC_MODE
    if mode not in CRITIC_MODES:
        raise RoomsError(
            "Режим критика: «rounds» (число раундов) или «until_approved» (до полного одобрения)",
            "bad_request",
            {"field": "critic_mode", "value": value, "allowed": list(CRITIC_MODES)},
        )
    return mode


def _norm_critic_mode(value) -> str:
    mode = str(value or "").strip()
    return mode if mode in CRITIC_MODES else DEFAULT_CRITIC_MODE


def _check_critic_cap(value) -> int:
    """Мягкий потолок раундов для режима «до одобрения»: целое 2..30."""
    if isinstance(value, bool) or not isinstance(value, int):
        raise RoomsError("critic_round_cap: целое число %d..%d"
                         % (MIN_CRITIC_ROUND_CAP, MAX_CRITIC_ROUND_CAP), "bad_request",
                         {"field": "critic_round_cap", "value": value})
    if value < MIN_CRITIC_ROUND_CAP or value > MAX_CRITIC_ROUND_CAP:
        raise RoomsError("critic_round_cap: от %d до %d"
                         % (MIN_CRITIC_ROUND_CAP, MAX_CRITIC_ROUND_CAP), "bad_request",
                         {"field": "critic_round_cap", "value": value,
                          "min": MIN_CRITIC_ROUND_CAP, "max": MAX_CRITIC_ROUND_CAP})
    return int(value)


def _norm_critic_cap(value) -> int:
    try:
        cap = int(value)
    except (TypeError, ValueError):
        return DEFAULT_CRITIC_ROUND_CAP
    if isinstance(value, bool) or cap < MIN_CRITIC_ROUND_CAP or cap > MAX_CRITIC_ROUND_CAP:
        return DEFAULT_CRITIC_ROUND_CAP
    return cap


def _check_launch_mode(value) -> str:
    """«Сначала план» (plan_first) или «сразу без подтверждения» (auto).

    Старое/пустое значение — рекомендуемое «сначала план»: ничего не запускаем
    без явного «Запустить» (A1).
    """
    mode = str(value or "").strip()
    if not mode:
        return DEFAULT_LAUNCH_MODE
    if mode not in LAUNCH_MODES:
        raise RoomsError(
            "Режим запуска: «сначала план» или «сразу без подтверждения»",
            "bad_request",
            {"field": "launch_mode", "value": value, "allowed": list(LAUNCH_MODES)},
        )
    return mode


def _check_parallel_streams(value) -> int:
    """Сколько направлений идёт одновременно: целое 1..4 (A2)."""
    if isinstance(value, bool) or not isinstance(value, int):
        raise RoomsError(
            "Одновременно направлений: целое число от %d до %d"
            % (MIN_PARALLEL_STREAMS, MAX_PARALLEL_STREAMS),
            "bad_request",
            {"field": "max_parallel_streams", "value": value},
        )
    if value < MIN_PARALLEL_STREAMS or value > MAX_PARALLEL_STREAMS:
        raise RoomsError(
            "Одновременно направлений: от %d до %d"
            % (MIN_PARALLEL_STREAMS, MAX_PARALLEL_STREAMS),
            "bad_request",
            {"field": "max_parallel_streams", "value": value,
             "min": MIN_PARALLEL_STREAMS, "max": MAX_PARALLEL_STREAMS},
        )
    return int(value)


def _norm_launch_mode(value) -> str:
    mode = str(value or "").strip()
    return mode if mode in LAUNCH_MODES else DEFAULT_LAUNCH_MODE


def _norm_project_mode(value) -> bool:
    """Старое/мусорное значение — выключен: включаем только по явному «да» (A1).

    `"false"`, `"нет"`, `0` в файле не должны тихо включать проектный режим.
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value == 1
    if isinstance(value, str):
        return value.strip().lower() in ("true", "1", "да", "yes")
    return False


def _norm_parallel_streams(value) -> int:
    """Старые записи без поля — 2; мусор в файле не роняет чтение списка."""
    if isinstance(value, bool):
        return DEFAULT_PARALLEL_STREAMS
    try:
        number = int(value)
    except (TypeError, ValueError):
        return DEFAULT_PARALLEL_STREAMS
    return number if MIN_PARALLEL_STREAMS <= number <= MAX_PARALLEL_STREAMS else DEFAULT_PARALLEL_STREAMS


def _check_phases(value) -> list[dict]:
    """Этапы проекта комнаты (W17-R §1, B-8): список `{id, title}`, пустой допустим.

    Пустой список — «у комнаты этапов нет»: полосы этапов и блока «где мы сейчас»
    на карте не будет, никаких заглушек. Шаблонов по умолчанию нет.
    """
    try:
        return phase_mod.check(value)
    except phase_mod.PhaseError as exc:
        raise RoomsError(exc.message, "bad_request", dict(exc.detail or {})) from exc


def _norm_phases(value) -> list[dict] | None:
    """Чтение реестра: список этапов как есть; нет поля — `None` (миграция чтением)."""
    if value is None:
        return None
    return phase_mod.norm(value)


def _agent_notes_mod():
    """Модуль указаний (лениво: rooms ← agent_notes, обратной зависимости нет)."""
    try:
        from . import agent_notes as mod
    except ImportError:  # pragma: no cover - плоский импорт
        import agent_notes as mod  # type: ignore
    return mod


def _check_agent_notes(value, members) -> dict:
    """Особые указания участникам (WAVE9 §9): профиль — из состава, текст ≤ 8 000.

    Пустая строка/нет поля — указаний нет. Ошибки — RoomsError (роут отдаст 400).
    """
    mod = _agent_notes_mod()
    try:
        notes, _warnings = mod.validate(value, members=members)
    except mod.NotesError as exc:
        raise RoomsError(exc.message, "bad_request", dict(exc.detail or {})) from exc
    return notes


def _norm_agent_notes(value) -> dict:
    """Чтение реестра: мусор и указания убранных участников не роняют список."""
    try:
        return _agent_notes_mod().normalize(value)
    except Exception:  # noqa: BLE001 - реестр комнат важнее указаний
        return {}


def _norm_notes_log(value) -> list:
    """Журнал изменений указаний (WAVE14 §13): список событий, новые сверху."""
    try:
        mod = _agent_notes_mod()
        raw = value if isinstance(value, list) else []
        limit = getattr(mod, "MAX_LOG", 200)
        return [dict(one) for one in raw if isinstance(one, dict)][:int(limit)]
    except Exception:  # noqa: BLE001 - журнал не должен ронять реестр
        return []


def _agent_models_mod():
    """Модуль моделей (лениво: rooms ← agent_models, обратной зависимости нет)."""
    try:
        from . import agent_models as mod
    except ImportError:  # pragma: no cover - плоский импорт
        import agent_models as mod  # type: ignore
    return mod


def _check_agent_models(cfg, value, members) -> dict:
    """Модель/провайдер/мышление участников (WAVE10): только настроенный провайдер.

    Профиль — из состава комнаты, пара «провайдер+модель» обязательна, уровень мышления —
    из списка движка. Ошибки — RoomsError (роут отдаст 400 по-русски).
    """
    mod = _agent_models_mod()
    try:
        return mod.validate_map(cfg, value, members=members)
    except mod.ModelError as exc:
        raise RoomsError(exc.message, "bad_request", dict(exc.detail or {})) from exc


def _norm_agent_models(value) -> dict:
    """Чтение реестра: мусор и записи убранных участников не роняют список комнат."""
    try:
        return _agent_models_mod().normalize(value)
    except Exception:  # noqa: BLE001 - реестр комнат важнее моделей
        return {}


def _check_project_dir(cfg, value) -> str:
    """Путь к папке проекта комнаты: пустая строка — «папка не задана».

    Невалидный путь — RoomsError с русским сообщением (роут отдаст 400).
    """
    if value is None:
        return ""
    if not isinstance(value, str):
        raise RoomsError("Папка проекта: нужен путь строкой", "bad_request",
                         {"field": "project_dir", "type": type(value).__name__})
    if not value.strip():
        return ""
    result = pdir.check_dir(cfg, value.strip())
    if not result.get("allowed"):
        raise RoomsError(result.get("reason") or "Путь к папке проекта не подходит",
                         "bad_request", {"field": "project_dir", "value": value,
                                         "reason": result.get("reason"),
                                         "normalized": result.get("normalized")})
    return result["normalized"]


def _warm_project_dir(cfg, room: dict) -> None:
    """Создать папку комнаты и обновить «Содержание» — до записи реестра."""
    path = str((room or {}).get("project_dir") or "")
    if not path:
        return
    try:
        pdir.ensure_dir(cfg, path)
    except pdir.DirError as exc:
        raise RoomsError(exc.message, "bad_request",
                         {"field": "project_dir", "value": path}) from exc
    pdir.update_index(cfg, room)


def _check_paths(value, field: str) -> list[str]:
    if not isinstance(value, list):
        raise RoomsError("%s: список путей" % field, "bad_request",
                         {"field": field, "type": type(value).__name__})
    out = []
    for item in value:
        if not isinstance(item, str) or not item.strip():
            raise RoomsError("%s: каждый элемент — непустой путь" % field, "bad_request",
                             {"field": field, "value": item})
        out.append(item.strip())
    return out


def _check_profile(value, field: str) -> str | None:
    """Профиль из строки; None/пусто допустимы для critic (не для orchestrator)."""
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    if not isinstance(value, str):
        raise RoomsError("%s: имя профиля строкой" % field, "bad_request",
                         {"field": field, "type": type(value).__name__})
    return value.strip()


def _norm_member_label(value) -> str:
    """Ручная подпись участника (WAVE13 §11): одна строка без управляющих символов.

    120 знаков достаточно для «Имя — должность»; пустое значение — сброс к
    описанию профиля Hermes.
    """
    if value is None:
        return ""
    text = " ".join(str(value).split())
    return text[:120]


def _norm_role_labels(value) -> dict:
    """Подписи руководителя и критика (WAVE17 B-17): `{профиль: «Имя — должность»}`.

    Руководитель и критик не лежат в `members` (они — настройки комнаты), поэтому
    их подпись хранится отдельной картой реестра. Пустая подпись в карту не
    попадает (значит «взять из описания профиля Hermes»). Профиль Hermes не
    меняется: правка касается только этой комнаты.
    """
    if not isinstance(value, dict):
        return {}
    out: dict = {}
    for profile, label in value.items():
        name = str(profile or "").strip()
        text = _norm_member_label(label)
        if name and text:
            out[name] = text
    return out


def _norm_member(member: dict, now: int | None = None) -> dict:
    if not isinstance(member, dict) or not str(member.get("profile") or "").strip():
        raise RoomsError("У участника должен быть profile", "bad_request", {"field": "profile"})
    role = str(member.get("role") or "worker")
    if role not in MEMBER_ROLES:
        raise RoomsError(f"Неизвестная роль участника: {role}", "bad_request", {"allowed": list(MEMBER_ROLES)})
    return {
        "profile": str(member["profile"]).strip(),
        "role": role,
        "note": str(member.get("note") or ""),
        "pinned": bool(member.get("pinned")),
        "added_at": int(member.get("added_at") or now or _now()),
        # WAVE13 §11: подпись участника в этой комнате («Анна — финансы (CFO)»).
        # Пустая — значит «взять из описания профиля Hermes». Профиль не меняется.
        "label": _norm_member_label(member.get("label")),
    }


def _norm_paths(value) -> list[str]:
    """Список путей из чего угодно: строка «abc» не должна превращаться в ['a','b','c']."""
    if isinstance(value, str):
        return [value.strip()] if value.strip() else []
    if not isinstance(value, (list, tuple)):
        return []
    return [str(p).strip() for p in value if isinstance(p, str) and p.strip()]


def _norm_rounds(value) -> int:
    try:
        rounds = int(value)
    except (TypeError, ValueError):
        return DEFAULT_MAX_ROUNDS
    return rounds if 1 <= rounds <= MAX_ROUNDS else DEFAULT_MAX_ROUNDS


def _norm_room(room: dict) -> dict:
    """Каноническая форма Room (ARCH §3): поля не теряем, недостающие дополняем."""
    out = {
        "id": str(room.get("id") or ""),
        "title": str(room.get("title") or ""),
        "icon": str(room.get("icon") or "folder"),
        "board": str(room.get("board") or DEFAULT_BOARD),
        "tenant": str(room.get("tenant") or f"room:{room.get('id') or ''}"),
        "orchestrator": str(room.get("orchestrator") or ""),
        "critic": (str(room["critic"]) if room.get("critic") else None),
        # WAVE13 §11: у участника всегда есть поле подписи (`""` — из описания профиля).
        "members": [dict(m, label=str(m.get("label") or ""))
                    for m in (room.get("members") or []) if isinstance(m, dict)],
        # WAVE17 B-17: подписи руководителя и критика — они не в members, а в
        # настройках комнаты; храним отдельной картой `{профиль: подпись}`.
        "role_labels": _norm_role_labels(room.get("role_labels")),
        "context_files": _norm_paths(room.get("context_files")),
        "project_dir": str(room.get("project_dir") or ""),
        "mode_default": str(room.get("mode_default") or "auto"),
        "critic_pass": bool(room.get("critic_pass")),
        "max_rounds": _norm_rounds(room.get("max_rounds")),
        # Режим критика (W13-E §9): нет поля — прежние «1–2 раунда».
        "critic_mode": _norm_critic_mode(room.get("critic_mode")),
        "critic_round_cap": _norm_critic_cap(room.get("critic_round_cap")),
        # Проектный режим: у старых записей полей нет — выключен, ждёт включения (A1).
        "launch_mode": _norm_launch_mode(room.get("launch_mode")),
        "max_parallel_streams": _norm_parallel_streams(room.get("max_parallel_streams")),
        "project_mode": _norm_project_mode(room.get("project_mode")),
        # Этапы проекта — свойство комнаты (W17-R §1): нет поля в записи — `None`,
        # и этапы подтягиваются миграцией чтением из карты самой комнаты.
        "phases": _norm_phases(room["phases"]) if "phases" in room else None,
        # Особые указания участникам (WAVE9 §9): нет поля — пусто.
        "agent_notes": _norm_agent_notes(room.get("agent_notes")),
        # Журнал изменений указаний (WAVE14 §13): кто/что/зачем/источник, новые сверху.
        "agent_notes_log": _norm_notes_log(room.get("agent_notes_log")),
        # Модель/провайдер/мышление участников (WAVE10): нет записи — «как у агента».
        "agent_models": _norm_agent_models(room.get("agent_models")),
        "created_at": int(room.get("created_at") or 0),
        "archived": bool(room.get("archived")),
        "seeded": bool(room.get("seeded")),
    }
    for key, value in room.items():
        out.setdefault(key, value)
    return out


def _check_mode(value) -> str:
    mode = str(value or "auto")
    if mode not in MODES:
        raise RoomsError("Режим swarm не поддерживается — только auto", "bad_request",
                         {"allowed": list(MODES), "value": mode})
    return mode


def list_rooms(cfg, include_archived: bool = False) -> list[dict]:
    """Все комнаты (полные Room), по порядку создания."""
    rooms = [_norm_room(r) for r in _load(cfg)["rooms"] if isinstance(r, dict)]
    if not include_archived:
        rooms = [r for r in rooms if not r["archived"]]
    rooms.sort(key=lambda r: (r["created_at"], r["id"]))
    return rooms


def get_room(cfg, room_id: str) -> dict | None:
    rid = str(room_id)
    for room in _load(cfg)["rooms"]:
        if str(room.get("id")) == rid:
            return _norm_room(room)
    return None


def create_room(cfg, data: dict) -> dict:
    """Создать комнату: id из title, tenant=room:<id>, board=team по умолчанию."""
    if not isinstance(data, dict):
        raise RoomsError("Тело комнаты должно быть объектом", "bad_request")
    title = str(data.get("title") or "").strip()
    if not title:
        raise RoomsError("Нужно название комнаты (title)", "bad_request", {"field": "title"})
    orchestrator = str(data.get("orchestrator") or "").strip()
    if not orchestrator:
        raise RoomsError("Нужен руководитель комнаты (orchestrator)", "bad_request", {"field": "orchestrator"})
    room_id = _check_id(str(data.get("id") or "").strip() or slugify(title))
    members_raw = data.get("members") or []
    if not isinstance(members_raw, list) or len(members_raw) < 1:
        raise RoomsError("В комнате должен быть хотя бы один исполнитель (members)", "bad_request", {"field": "members"})
    if data.get("critic") is not None and _check_profile(data.get("critic"), "critic") == orchestrator:
        raise RoomsError("critic не может совпадать с orchestrator", "bad_request",
                         {"field": "critic", "value": data.get("critic")})
    now = _now()
    members = [_norm_member(m, now) for m in members_raw]
    project_dir = _check_project_dir(cfg, data.get("project_dir"))
    room = {
        "id": room_id,
        "title": title,
        "icon": _check_icon(data.get("icon")),
        "board": _check_board(data.get("board") or DEFAULT_BOARD),
        "tenant": f"room:{room_id}",
        "orchestrator": orchestrator,
        "critic": _check_profile(data.get("critic"), "critic"),
        "members": members,
        "context_files": _check_paths(data.get("context_files") or [], "context_files"),
        "project_dir": project_dir,
        "mode_default": _check_mode(data.get("mode_default")),
        "critic_pass": _check_bool(data["critic_pass"], "critic_pass") if "critic_pass" in data else False,
        "max_rounds": _check_rounds(data["max_rounds"]) if "max_rounds" in data else DEFAULT_MAX_ROUNDS,
        "critic_mode": _check_critic_mode(data.get("critic_mode")),
        "critic_round_cap": (_check_critic_cap(data["critic_round_cap"])
                             if "critic_round_cap" in data else DEFAULT_CRITIC_ROUND_CAP),
        # Новая комната сразу работает в проектном режиме; части работы запускаются
        # сразу, без подтверждения (W17-R §3, решение Михаила 06.11).
        "launch_mode": _check_launch_mode(data["launch_mode"] if "launch_mode" in data else DEFAULT_LAUNCH_MODE_NEW),
        "max_parallel_streams": (_check_parallel_streams(data["max_parallel_streams"])
                                 if "max_parallel_streams" in data else DEFAULT_PARALLEL_STREAMS),
        "project_mode": _check_bool(data["project_mode"], "project_mode") if "project_mode" in data else True,
        # Этапы проекта (W17-R §1): по умолчанию их НЕТ — пользователь задаёт сам.
        "phases": _check_phases(data.get("phases")),
        # Указания — участникам этой комнаты (профили сверяем с составом этого же запроса).
        "agent_notes": _check_agent_notes(
            data.get("agent_notes"),
            [orchestrator] + [str(m.get("profile") or "").strip() for m in members]
            + ([str(data.get("critic") or "").strip()] if data.get("critic") else []),
        ),
        # Модели участников этой комнаты (WAVE10): так же — профиль из состава этого запроса.
        "agent_models": _check_agent_models(
            cfg, data.get("agent_models"),
            [orchestrator] + [str(m.get("profile") or "").strip() for m in members]
            + ([str(data.get("critic") or "").strip()] if data.get("critic") else []),
        ),
        "created_at": now,
        "archived": False,
        "seeded": bool(data.get("seeded")),
    }
    path = _rooms_path(cfg)
    with sf.lock_for(path):
        current = _load(cfg)
        if any(str(r.get("id")) == room_id for r in current["rooms"] if isinstance(r, dict)):
            raise RoomsError(f"Комната «{room_id}» уже есть", "conflict", {"room": room_id})
        # папка проекта создаётся ДО записи реестра: нет папки — нет комнаты
        _warm_project_dir(cfg, room)
        current["rooms"].append(room)
        _save(cfg, current)
    return room


def update_room(cfg, room_id: str, patch: dict) -> dict:
    """Частичное обновление; id/board/tenant менять нельзя."""
    if not isinstance(patch, dict):
        raise RoomsError("patch должен быть объектом", "bad_request")
    bad = [k for k in patch if k in IMMUTABLE]
    if bad:
        raise RoomsError("Эти поля менять нельзя", "bad_request", {"fields": bad, "immutable": list(IMMUTABLE)})
    unknown = [k for k in patch if k not in UPDATABLE]
    if unknown:
        raise RoomsError("Неизвестные поля", "bad_request", {"fields": unknown, "allowed": list(UPDATABLE)})
    if "mode_default" in patch:
        _check_mode(patch["mode_default"])
    path = _rooms_path(cfg)
    with sf.lock_for(path):
        data = _load(cfg)
        for idx, room in enumerate(data["rooms"]):
            if str(room.get("id")) != str(room_id):
                continue
            for key, value in patch.items():
                if key == "critic":
                    critic = _check_profile(value, "critic")
                    if critic and critic == str(room.get("orchestrator") or ""):
                        raise RoomsError("critic не может совпадать с orchestrator", "bad_request",
                                         {"field": "critic", "value": critic})
                    room["critic"] = critic
                elif key == "context_files":
                    room["context_files"] = _check_paths(value, "context_files")
                elif key == "critic_pass":
                    room["critic_pass"] = _check_bool(value, "critic_pass")
                elif key == "max_rounds":
                    room["max_rounds"] = _check_rounds(value)
                elif key == "critic_mode":
                    room["critic_mode"] = _check_critic_mode(value)
                elif key == "critic_round_cap":
                    room["critic_round_cap"] = _check_critic_cap(value)
                elif key == "icon":
                    room["icon"] = _check_icon(value)
                elif key == "title":
                    if not str(value or "").strip():
                        raise RoomsError("Название не может быть пустым", "bad_request", {"field": "title"})
                    room["title"] = str(value).strip()
                elif key == "orchestrator":
                    if not str(value or "").strip():
                        raise RoomsError("Руководитель комнаты обязателен", "bad_request", {"field": "orchestrator"})
                    if room.get("critic") and str(value).strip() == str(room.get("critic")):
                        raise RoomsError("orchestrator не может совпадать с critic", "bad_request",
                                         {"field": "orchestrator", "value": value})
                    room["orchestrator"] = str(value).strip()
                elif key == "project_dir":
                    room["project_dir"] = _check_project_dir(cfg, value)
                elif key == "launch_mode":
                    room["launch_mode"] = _check_launch_mode(value)
                elif key == "phases":
                    room["phases"] = _check_phases(value)
                elif key == "max_parallel_streams":
                    room["max_parallel_streams"] = _check_parallel_streams(value)
                elif key == "project_mode":
                    room["project_mode"] = _check_bool(value, "project_mode")
                elif key == "agent_notes":
                    # Состав для сверки: текущий + новый руководитель/критик из этого же патча
                    # (участников добавляют отдельно — их указания идут через PUT …/notes).
                    lead = str(patch.get("orchestrator") or room.get("orchestrator") or "").strip()
                    critic = str(patch.get("critic") or room.get("critic") or "").strip()
                    members = [lead, critic] + [str(m.get("profile") or "").strip()
                                                for m in (room.get("members") or [])
                                                if isinstance(m, dict)]
                    room["agent_notes"] = _check_agent_notes(value, [m for m in members if m])
                elif key == "agent_notes_log":
                    # Журнал ведёт приложение (agent_notes) и API — не интерфейс комнаты;
                    # поле нужно, чтобы запись прошла через update_room.
                    room["agent_notes_log"] = _norm_notes_log(value)
                elif key == "agent_models":
                    # Состав для сверки — тот же, что у указаний: текущий + правки этого патча.
                    lead = str(patch.get("orchestrator") or room.get("orchestrator") or "").strip()
                    critic = str(patch.get("critic") or room.get("critic") or "").strip()
                    members = [lead, critic] + [str(m.get("profile") or "").strip()
                                                for m in (room.get("members") or [])
                                                if isinstance(m, dict)]
                    room["agent_models"] = _check_agent_models(
                        cfg, value, [m for m in members if m])
                else:
                    room[key] = value
            if "project_dir" in patch:
                _warm_project_dir(cfg, room)  # папка создаётся до записи реестра
            data["rooms"][idx] = room
            _save(cfg, data)
            return _norm_room(room)
    raise RoomsError("Комната не найдена", "not_found", {"room": str(room_id)})


def add_member(cfg, room_id: str, member: dict) -> dict:
    """Добавить участника; повторно того же профиля — конфликт.

    Возвращает `{member, members}` — как ждёт app.py (POST /members).
    """
    new_member = _norm_member(member)
    path = _rooms_path(cfg)
    with sf.lock_for(path):
        data = _load(cfg)
        for idx, room in enumerate(data["rooms"]):
            if str(room.get("id")) != str(room_id):
                continue
            members = list(room.get("members") or [])
            if any(str(m.get("profile")) == new_member["profile"] for m in members):
                raise RoomsError("Этот профиль уже в комнате", "conflict", {"member": new_member["profile"]})
            members.append(new_member)
            room["members"] = members
            data["rooms"][idx] = room
            _save(cfg, data)
            return {"member": new_member, "members": members}
    raise RoomsError("Комната не найдена", "not_found", {"room": str(room_id)})


def set_member_label(cfg, room_id: str, profile: str, label: str) -> dict:
    """Подпись участника в комнате (WAVE13 §11 / WAVE17 B-17): `«Имя — должность»` или сброс.

    Одна и та же операция для ВСЕХ участников (B-17): исполнитель — поле `label`
    в `members`, руководитель комнаты и критик — карта `role_labels` реестра
    комнаты. Раньше для руководителя/критика был отказ `room_lead` («подпись
    меняется в настройках комнаты»), хотя такого поля там не было.

    Хранится только в реестре комнаты (`rooms.json`), файлы профиля Hermes не
    меняются. Пустая подпись — вернуться к описанию профиля. Возвращает
    `{profile, label, members, role_labels}` — как ждёт app.py
    (PUT …/agents/{profile}/label).
    """
    profile = str(profile or "").strip()
    text = _norm_member_label(label)
    path = _rooms_path(cfg)
    with sf.lock_for(path):
        data = _load(cfg)
        for idx, room in enumerate(data["rooms"]):
            if str(room.get("id")) != str(room_id):
                continue
            members = list(room.get("members") or [])
            for pos, member in enumerate(members):
                if str(member.get("profile")) != profile:
                    continue
                fresh = dict(member)
                fresh["label"] = text
                members[pos] = fresh
                room["members"] = members
                data["rooms"][idx] = room
                _save(cfg, data)
                return {"profile": profile, "label": text, "members": members,
                        "role_labels": _norm_role_labels(room.get("role_labels"))}
            is_lead = profile in (str(room.get("orchestrator") or ""), str(room.get("critic") or ""))
            if is_lead:
                # B-17: подпись руководителя и критика — та же кнопка «Подпись».
                labels = _norm_role_labels(room.get("role_labels"))
                if text:
                    labels[profile] = text
                else:
                    labels.pop(profile, None)
                room["role_labels"] = labels
                data["rooms"][idx] = room
                _save(cfg, data)
                return {"profile": profile, "label": text,
                        "members": room.get("members") or [], "role_labels": labels}
            raise RoomsError("Участник не найден", "not_found", {"profile": profile})
    raise RoomsError("Комната не найдена", "not_found", {"room": str(room_id)})


def remove_member(cfg, room_id: str, profile: str) -> dict:
    """Удалить исполнителя. Руководителя комнаты и критика удалить нельзя (сначала PATCH).

    Возвращает `{ok, members}` — как ждёт app.py (DELETE /members/{profile}).
    """
    profile = str(profile or "")
    path = _rooms_path(cfg)
    with sf.lock_for(path):
        data = _load(cfg)
        for idx, room in enumerate(data["rooms"]):
            if str(room.get("id")) != str(room_id):
                continue
            if profile == str(room.get("orchestrator")) or (room.get("critic") and profile == str(room.get("critic"))):
                raise RoomsError(
                    "Руководителя комнаты и критика нельзя удалить — смените их в настройках комнаты",
                    "conflict", {"reason": "room_lead", "profile": profile},
                )
            members = list(room.get("members") or [])
            left = [m for m in members if str(m.get("profile")) != profile]
            if len(left) == len(members):
                raise RoomsError("Участник не найден", "not_found", {"profile": profile})
            room["members"] = left
            # Указания убранного участника комнате больше не принадлежат (WAVE9 §9).
            notes = _norm_agent_notes(room.get("agent_notes"))
            notes.pop(profile, None)
            room["agent_notes"] = notes
            # Замена модели убранного участника комнате больше не принадлежит (WAVE10).
            models = _norm_agent_models(room.get("agent_models"))
            models.pop(profile, None)
            room["agent_models"] = models
            data["rooms"][idx] = room
            _save(cfg, data)
            return {"ok": True, "members": left}
    raise RoomsError("Комната не найдена", "not_found", {"room": str(room_id)})


def archive_room(cfg, room_id: str) -> dict:
    """Архивировать комнату. `{room, changed}`; повторный вызов changed=False."""
    path = _rooms_path(cfg)
    with sf.lock_for(path):
        data = _load(cfg)
        for idx, room in enumerate(data["rooms"]):
            if str(room.get("id")) != str(room_id):
                continue
            changed = not bool(room.get("archived"))
            room["archived"] = True
            data["rooms"][idx] = room
            if changed:
                _save(cfg, data)
            return {"room": _norm_room(room), "changed": changed}
    raise RoomsError("Комната не найдена", "not_found", {"room": str(room_id)})


# --- seed ------------------------------------------------------------------

SEED_CREATED = 1793059000  # фиксированная база: порядок комнат стабилен


def _vault_roots(cfg) -> list[Path]:
    """Кандидаты корня Vault: из cfg.vault_dir (…/Projects/Rooms) и его родителей."""
    roots: list[Path] = []
    try:
        vault_dir = config_mod.vault_root_dir(cfg)
        if vault_dir is None:
            return []
        if vault_dir.name.lower() == "rooms" and vault_dir.parent.name.lower() == "projects":
            roots.append(vault_dir.parent.parent)
        roots.append(vault_dir)
    except (TypeError, ValueError):
        pass
    unique: list[Path] = []
    for root in roots:
        if root not in unique:
            unique.append(root)
    return unique


def _vault_path(cfg, *parts: str) -> str:
    """Абсолютный путь в Vault, только если файл/папка реально есть (read-only)."""
    for root in _vault_roots(cfg):
        candidate = root.joinpath(*parts)
        if candidate.exists():
            return str(candidate)
    return ""


def _seed_cafe(cfg) -> dict:
    base = _vault_path(cfg, "Projects", "Business areas", "Лето")
    context = [p for p in (
        _vault_path(cfg, "Projects", "Business areas", "Лето", name)
        for name in ("BRIEF.md", "ROADMAP.md", "VISION.md")
    ) if p]
    decisions = _vault_path(cfg, "Projects", "Team", "Decisions.md")
    if decisions:
        context.append(decisions)
    return {
        "id": "cafe",
        "title": "Лето",
        "icon": "cake",
        "board": "team",
        "tenant": "room:cafe",
        "orchestrator": "biz-director",
        "critic": "biz-critic",
        "members": [
            {"profile": "biz-lawyer", "role": "worker", "note": "Юрист: ИП, СЭС, Роспатент, договоры", "pinned": True, "added_at": SEED_CREATED},
            {"profile": "biz-ops", "role": "worker", "note": "Операции: поставщики, упаковка, доставка", "pinned": True, "added_at": SEED_CREATED},
            {"profile": "biz-strategy", "role": "worker", "note": "Стратегия, бренд, план 90 дней", "pinned": False, "added_at": SEED_CREATED},
            {"profile": "biz-marketing", "role": "worker", "note": "Каналы, контент, D19", "pinned": False, "added_at": SEED_CREATED + 100},
            {"profile": "biz-cfo", "role": "worker", "note": "Экономика, налоги, сметы", "pinned": False, "added_at": SEED_CREATED + 100},
            {"profile": "biz-runner", "role": "worker", "note": "Сбор фактов по Vault и доске", "pinned": False, "added_at": SEED_CREATED + 200},
            {"profile": "biz-critic", "role": "critic", "note": "Проверяет свод перед ответом Михаилу", "pinned": True, "added_at": SEED_CREATED + 200},
        ],
        "context_files": context,
        "project_dir": base,
        "mode_default": "auto",
        "critic_pass": True,
        "max_rounds": 1,
        # Демо-комната Лето ведёт свою карту проекта: этапы принадлежат ЕЙ
        # (W17-R §1, B-8) — это её данные, а не список в коде.
        # Настройки проектного режима у демо-комнат прежние (A1): включить их
        # должен человек. Здесь только этапы — они и есть данные этой комнаты.
        "phases": [
            {"id": "idea", "title": "Идея и стратегия"},
            {"id": "legal", "title": "Легализация"},
            {"id": "brand", "title": "Бренд и товарный знак"},
            {"id": "packaging", "title": "Упаковка"},
            {"id": "production", "title": "Производство"},
            {"id": "sales", "title": "Продажи"},
        ],
        "created_at": SEED_CREATED,
        "archived": False,
        "seeded": True,
    }


def _seed_fn(cfg) -> dict:
    # Проект «Рабочее пространство» в Vault пока не заведён: не выдумываем пути.
    decisions = _vault_path(cfg, "Projects", "Team", "Decisions.md")
    context = [decisions] if decisions else []
    return {
        "id": "fn",
        "title": "Рабочее пространство",
        "icon": "law",
        "board": "team",
        "tenant": "room:fn",
        "orchestrator": "default",
        "critic": None,
        "members": [
            {"profile": "lawyer", "role": "worker", "note": "Юридические вопросы", "pinned": True, "added_at": SEED_CREATED + 400},
            {"profile": "coder", "role": "worker", "note": "Код и автоматизация", "pinned": False, "added_at": SEED_CREATED + 400},
            {"profile": "surveyor", "role": "worker", "note": "Проверка фактов и источников", "pinned": False, "added_at": SEED_CREATED + 500},
        ],
        "context_files": context,
        "project_dir": "",
        "mode_default": "auto",
        "critic_pass": False,
        "max_rounds": 1,
        # У «Руководства_FN» своих этапов нет — и полосы этапов не будет.
        "phases": [],
        "created_at": SEED_CREATED + 400,
        "archived": False,
        "seeded": True,
    }


def seed_enabled(cfg) -> bool:
    """Создавать ли демо-комнаты при первом запуске.

    Демо-комнаты (и их пути контекста) имеют смысл, только когда есть Vault, из
    которого они берут материалы. На чистой машине без Vault список комнат пуст —
    это нормальный первый запуск. Явно: ``TEAMROOM_SEED=1/0``; мок и демо — всегда.
    """
    env = str(os.environ.get("TEAMROOM_SEED") or "").strip().lower()
    if env in ("0", "false", "no", "off"):
        return False
    if env in ("1", "true", "yes", "on"):
        return True
    if getattr(cfg, "mock", False) or getattr(cfg, "demo", False):
        return True
    return config_mod.vault_root_dir(cfg) is not None


def seed_defaults(cfg) -> bool:
    """Создать cafe и fn, если data/rooms.json ещё нет.

    Битый файл НЕ затирается молча: сначала копия ``rooms.json.corrupt-<ts>``
    (и warning в /api/health), только потом сид. Если файл занят/нет прав —
    ошибка ``StateUnavailable``, сид не пишется (D4).
    Идемпотентно: True только когда файл создан этим вызовом.
    """
    if not seed_enabled(cfg):
        return False
    path = _rooms_path(cfg)
    with sf.lock_for(path):
        existing, info = sf.read_json_doc(path, None)
        if info["state"] == "unreadable":
            raise sf.StateUnavailable(
                "Не удалось прочитать «%s» (%s). Файл комнат не перезаписан — "
                "данные не подменяются значениями по умолчанию."
                % (path, info.get("error") or "занят"),
                {"path": str(path), "error": info.get("error")},
            )
        if info["state"] == "ok" and isinstance(existing, dict) and isinstance(existing.get("rooms"), list):
            return False
        if info["state"] == "corrupt":
            sf.quarantine(path)          # копия до замены — пользователь не теряет комнаты
        data = {"version": 1, "rooms": [_seed_cafe(cfg), _seed_fn(cfg)], "updated_at": _now()}
        sf.write_json_atomic(path, data)
    return True
