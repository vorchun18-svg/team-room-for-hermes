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
             # W20 §1.7 (F-2): предложение этапов руководителем. Пишется только
             # маршрутами `phases/*` и приёмом metadata — не интерфейсом комнаты.
             "phases_proposal",
             # Особые указания участникам этой комнаты (WAVE9 §9): только реестр комнаты,
             # файлы профилей (SOUL.md/config.yaml/skills) не меняются никогда.
             "agent_notes",
             # Журнал указаний (WAVE14 §13): кто/что/зачем/источник + откат изменения.
             "agent_notes_log",
             # Модель/провайдер/мышление участника ТОЛЬКО в этой комнате (WAVE10 §«Интерфейсы»).
             "agent_models",
             # W29-Q9: модель для рутинного сведения (автосвод и «Уточнение»); пусто —
             # модель руководителя. Хранится в реестре комнаты, профили не меняются.
             "routine_model",
             # W20-S1 §1.1: живой свод — автообновление, «тишина» и потолок частоты.
             "auto_svod", "auto_svod_quiet_s", "auto_svod_min_gap_s",
             # «Ход работы» (W20 §1.6): подробность ленты и частота промежуточного итога.
             "worklog_mode", "digest_every_min",
             # Контроль моделей (W23 §2.3): enforce (исправляем сами) | warn (предупреждать).
             "model_guard",
             # Дежурства комнаты (W24 §3.2): только привязка id заданий Hermes —
             # сами задания живут в Hermes и читаются на чтение (`server/duties.py`).
             "duties",
             # Уровень автономии комнаты (W27-E5 §2): `{level, never, updated_at}`.
             # Пишется ТОЛЬКО маршрутом `POST /api/rooms/{id}/autonomy` (после записи
             # в профили) — интерфейс комнаты поле не редактирует.
             "autonomy")
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
# «Ход работы» (W20 §1.6, WAVE18 пакет W): сколько подробностей показывать и как часто
# сторож кладёт промежуточный итог. По умолчанию — как в Hermes: подробно и раз в 2 минуты.
WORKLOG_MODES = ("full", "brief", "off")
DEFAULT_WORKLOG_MODE = "full"
DIGEST_CHOICES = (0, 1, 2, 5)
DEFAULT_DIGEST_MIN = 2
#: Дежурства комнаты (W24 §3.2): сколько id заданий можно привязать и как они выглядят.
MAX_ROOM_DUTIES = 40
_DUTY_ID_RE = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")

# --- порядок комнат в меню (W29-P7) ----------------------------------------
# Порядок, закрепление и группы живут в реестре комнат (`data/rooms.json`), а не
# в профилях Hermes: это личное устройство меню, а не настройка агентов.
#   * `order`  — целое ≥ 1: место комнаты в общем списке (0 — «ещё не задавали»);
#   * `pinned` — флаг «закрепить наверху» (📌);
#   * `group`  — id группы из `groups` реестра ("" — без группы).
# Список групп лежит рядом с комнатами: `{"rooms": [...], "groups": [...]}`.
MAX_GROUPS = 20
MAX_GROUP_TITLE = 60
_GROUP_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,23}$")


def _norm_group(value) -> str:
    """Чтение поля `group`: мусор в файле — «без группы», список не падает."""
    text = str(value or "").strip().lower()
    return text if _GROUP_ID_RE.match(text) else ""


def _norm_order(value) -> int:
    """Чтение поля `order`: только целое ≥ 1, иначе 0 («не задавали»)."""
    if isinstance(value, bool):
        return 0
    try:
        number = int(value)
    except (TypeError, ValueError):
        return 0
    return number if number >= 1 else 0


def _check_group_title(value) -> str:
    """Название группы: одна строка 1…60 знаков (ошибка — RoomsError)."""
    text = " ".join(str(value or "").split())
    if not text:
        raise RoomsError("У группы должно быть название", "bad_request", {"field": "groups.title"})
    if len(text) > MAX_GROUP_TITLE:
        raise RoomsError("Название группы длиннее %d знаков" % MAX_GROUP_TITLE, "bad_request",
                         {"field": "groups.title", "length": len(text), "max": MAX_GROUP_TITLE})
    return text


def _check_groups(value) -> list[dict]:
    """Проверить список групп `[{id, title, collapsed}]` (порядок сохраняем)."""
    if value is None:
        return []
    if not isinstance(value, list):
        raise RoomsError("Группы комнат — список", "bad_request", {"field": "groups"})
    if len(value) > MAX_GROUPS:
        raise RoomsError("Слишком много групп (не больше %d)" % MAX_GROUPS, "bad_request",
                         {"field": "groups", "limit": MAX_GROUPS})
    out: list[dict] = []
    seen: set[str] = set()
    for raw in value:
        if not isinstance(raw, dict):
            raise RoomsError("Группа комнат — объект с id и названием", "bad_request",
                             {"field": "groups", "value": str(raw)[:40]})
        group_id = str(raw.get("id") or "").strip().lower()
        if not _GROUP_ID_RE.match(group_id):
            raise RoomsError("id группы: латиница, цифры, - и _, до 24 знаков",
                             "bad_request", {"field": "groups.id", "value": group_id[:40]})
        if group_id in seen:
            raise RoomsError("Группа «%s» уже есть" % group_id, "bad_request",
                             {"field": "groups.id", "value": group_id, "reason": "duplicate"})
        seen.add(group_id)
        out.append({"id": group_id, "title": _check_group_title(raw.get("title")),
                    "collapsed": bool(raw.get("collapsed"))})
    return out


def _norm_groups(value) -> list[dict]:
    """Чтение реестра: битая группа не роняет меню — просто не показываем её."""
    try:
        return _check_groups(value)
    except RoomsError:
        return []


def _groups_doc(data: dict) -> list[dict]:
    return _norm_groups(data.get("groups")) if isinstance(data, dict) else []


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


# --- «Ход работы» (W20 §1.6) ------------------------------------------------
#: подписи для интерфейса — один источник, чтобы FE не собирал слова сам
WORKLOG_MODE_LABELS = {"full": "Подробно (с рассуждениями)",
                       "brief": "Кратко (только действия)",
                       "off": "Выключено"}
DIGEST_LABELS = {0: "Выключено", 1: "Каждую минуту", 2: "Каждые 2 минуты",
                 5: "Каждые 5 минут"}


def _check_worklog_mode(value) -> str:
    """Показывать ход работы: full | brief | off (пусто — прежнее «подробно»)."""
    mode = str(value or "").strip().lower()
    if not mode:
        return DEFAULT_WORKLOG_MODE
    if mode not in WORKLOG_MODES:
        raise RoomsError("Ход работы: «Подробно», «Кратко» или «Выключено»", "bad_request",
                         {"field": "worklog_mode", "value": value,
                          "allowed": list(WORKLOG_MODES)})
    return mode


def _norm_worklog_mode(value) -> str:
    mode = str(value or "").strip().lower()
    return mode if mode in WORKLOG_MODES else DEFAULT_WORKLOG_MODE


def _check_digest_min(value) -> int:
    """Промежуточный итог каждые N минут: 0 (выкл.), 1, 2, 5."""
    if isinstance(value, bool) or not isinstance(value, int):
        raise RoomsError("Промежуточный итог: 0, 1, 2 или 5 минут", "bad_request",
                         {"field": "digest_every_min", "value": value,
                          "allowed": list(DIGEST_CHOICES)})
    if value not in DIGEST_CHOICES:
        raise RoomsError("Промежуточный итог: 0, 1, 2 или 5 минут", "bad_request",
                         {"field": "digest_every_min", "value": value,
                          "allowed": list(DIGEST_CHOICES)})
    return int(value)


def _norm_digest_min(value) -> int:
    try:
        minutes = int(value)
    except (TypeError, ValueError):
        return DEFAULT_DIGEST_MIN
    if isinstance(value, bool) or minutes not in DIGEST_CHOICES:
        return DEFAULT_DIGEST_MIN
    return minutes


def _norm_card_watchdog(value) -> dict:
    """Пороги сторожа зависаний (W25-M2): `{warn_min, stop_min, hard_stop}`.

    Читается из `rooms.json`; битое значение — умолчания (10/20, остановка включена),
    чтобы комната не осталась без сторожа из-за опечатки в файле.
    """
    try:
        try:
            from . import watchdog as wd
        except ImportError:                  # pragma: no cover - плоский импорт
            import watchdog as wd            # type: ignore
        return wd.normalize(value)
    except Exception:                        # noqa: BLE001 - пороги вторичны
        return {"warn_min": 10, "stop_min": 20, "hard_stop": True}


def worklog_block(mode, digest) -> dict:
    """Готовая строка настроек «Хода работы» для карточки комнаты (W20 §1.6)."""
    name = _norm_worklog_mode(mode)
    minutes = _norm_digest_min(digest)
    return {"mode": name, "mode_label": WORKLOG_MODE_LABELS[name],
            "digest_every_min": minutes, "digest_label": DIGEST_LABELS[minutes],
            "on": name != "off"}


# --- контроль моделей (W23 §2.3) --------------------------------------------
#: `enforce` — приложение само исправляет модель карточки и останавливает чужой запуск;
#: `warn` — только предупреждает (ничего не останавливает и не переписывает).
MODEL_GUARD_MODES = ("enforce", "warn")
DEFAULT_MODEL_GUARD = "enforce"


def _check_model_guard(value) -> str:
    """Режим контроля моделей: `enforce` (по умолчанию) | `warn`."""
    mode = str(value or "").strip().lower()
    if not mode:
        return DEFAULT_MODEL_GUARD
    if mode not in MODEL_GUARD_MODES:
        raise RoomsError("Контроль моделей: «Строго» или «Только предупреждать»", "bad_request",
                         {"field": "model_guard", "value": value,
                          "allowed": list(MODEL_GUARD_MODES)})
    return mode


def _norm_model_guard(value) -> str:
    mode = str(value or "").strip().lower()
    return mode if mode in MODEL_GUARD_MODES else DEFAULT_MODEL_GUARD


def model_guard_block(value) -> dict:
    """Готовая строка настройки для карточки комнаты (W23.7) — слова для человека."""
    labels = {"enforce": "Строго: неверную модель исправляю сам",
              "warn": "Только предупреждать — решаю я"}
    hints = {"enforce": "Расхождение исправляется до запуска; запущенное на чужой модели "
                        "останавливается и перезапускается",
             "warn": "Приложение только показывает расхождение — останавливать и "
                     "перезапускать ничего не будет"}
    mode = _norm_model_guard(value)
    return {"mode": mode, "on": True, "label": labels[mode], "hint": hints[mode],
            "modes": [{"id": m, "label": labels[m]} for m in MODEL_GUARD_MODES]}


# --- дежурства комнаты (W24 §3.2) -----------------------------------------
# Комната только ПРИВЯЗЫВАЕТ задания Hermes к себе: сами задания, расписание и
# выводы остаются в Hermes и читаются на чтение (`server/duties.py`). Ничего в
# Hermes приложение не пишет и не запускает.

def _check_duties(value) -> list[str]:
    """Список id дежурств: строки без повторов, разумной длины (порядок сохраняем)."""
    if value is None:
        return []
    if not isinstance(value, list):
        raise RoomsError("Дежурства комнаты — список", "bad_request", {"field": "duties"})
    out: list[str] = []
    for raw in value:
        job_id = str(raw or "").strip()
        if not job_id:
            continue
        if not _DUTY_ID_RE.fullmatch(job_id):
            raise RoomsError("Недопустимый номер дежурства", "bad_request",
                             {"field": "duties", "value": job_id[:64]})
        if job_id not in out:
            out.append(job_id)
    if len(out) > MAX_ROOM_DUTIES:
        raise RoomsError("Слишком много дежурств у комнаты (не больше %d)" % MAX_ROOM_DUTIES,
                         "bad_request", {"field": "duties", "limit": MAX_ROOM_DUTIES})
    return out


def _norm_duties(value) -> list[str]:
    """Чтение поля из старой записи: битое значение — «ничего не привязано»."""
    try:
        return _check_duties(value)
    except RoomsError:
        return []


# --- уровень автономии комнаты (W27-E5 §2) --------------------------------
# В реестре комнаты живёт ТОЛЬКО выбор уровня и список «только с моего да»
# (`{level, never, updated_at}`). Сами настройки — в профилях Hermes, их пишет
# `server/autonomy.py` отдельным процессом; комната лишь помнит, что выбрано.

def _autonomy_mod():
    """Модуль уровней автономии (лениво: rooms ← autonomy, обратной зависимости нет)."""
    try:
        from . import autonomy as mod
    except ImportError:  # pragma: no cover - плоский импорт
        import autonomy as mod  # type: ignore
    return mod


def _check_autonomy(value) -> dict:
    """Проверить запись `{level, never}` (ошибки — RoomsError, роут отдаст 400)."""
    mod = _autonomy_mod()
    try:
        return mod.validate(value)
    except mod.AutonomyError as exc:
        raise RoomsError(exc.message, "bad_request", dict(exc.detail or {})) from exc


def _norm_autonomy(value) -> dict:
    """Чтение реестра: мусор не роняет список комнат — уровень берём по умолчанию."""
    try:
        return _autonomy_mod().normalize(value)
    except Exception:  # noqa: BLE001 - реестр комнат важнее одного поля
        return {"level": "working", "never": [], "updated_at": 0}



# --- живой свод (W20-S1 §1.1): автообновление, «тишина», потолок частоты ---------
SVOD_QUIET_RANGE = (30, 3600)       # сек: сколько ждать «тишину» после последнего события
SVOD_GAP_RANGE = (60, 86400)        # сек: не чаще раза в N секунд
DEFAULT_SVOD_AUTO = True
DEFAULT_SVOD_QUIET_S = 180
DEFAULT_SVOD_MIN_GAP_S = 600


def _check_auto_svod(value) -> bool:
    """Автообновление свода: да/нет (ошибка — понятная, с полем)."""
    return _check_bool(value, "auto_svod")


def _check_svod_quiet(value) -> int:
    """«Тишина» перед обновлением свода — целое 30…3600 секунд."""
    return _check_seconds(value, "auto_svod_quiet_s", SVOD_QUIET_RANGE,
                          "«тишина» перед обновлением свода — от 30 до 3600 секунд")


def _check_svod_gap(value) -> int:
    """Потолок частоты обновлений свода — целое 60…86400 секунд."""
    return _check_seconds(value, "auto_svod_min_gap_s", SVOD_GAP_RANGE,
                          "обновление свода — не чаще раза в 60…86400 секунд")


def _check_seconds(value, field: str, bounds: tuple, message: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise RoomsError(message[0].upper() + message[1:], "bad_request",
                         {"field": field, "value": value, "min": bounds[0], "max": bounds[1]})
    if not bounds[0] <= value <= bounds[1]:
        raise RoomsError(message[0].upper() + message[1:], "bad_request",
                         {"field": field, "value": value, "min": bounds[0], "max": bounds[1]})
    return int(value)


def _norm_svod_seconds(value, default: int, bounds: tuple) -> int:
    """Значение из старой записи: мусор и выход за границы — значение по умолчанию."""
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    if isinstance(value, bool) or not bounds[0] <= number <= bounds[1]:
        return default
    return number


def svod_block(room: dict) -> dict:
    """Готовая строка «живого свода» для карточки комнаты (W20-SVOD)."""
    room = room if isinstance(room, dict) else {}
    auto = DEFAULT_SVOD_AUTO if room.get("auto_svod") is None else bool(room.get("auto_svod"))
    quiet = _norm_svod_seconds(room.get("auto_svod_quiet_s"), DEFAULT_SVOD_QUIET_S, SVOD_QUIET_RANGE)
    gap = _norm_svod_seconds(room.get("auto_svod_min_gap_s"), DEFAULT_SVOD_MIN_GAP_S, SVOD_GAP_RANGE)
    return {"auto_svod": auto, "auto_svod_quiet_s": quiet, "auto_svod_min_gap_s": gap,
            "auto_svod_label": ("Включено: свод обновляется сам" if auto else "Выключено: только по кнопке"),
            "quiet_label": "не раньше чем через %d мин тишины" % max(1, round(quiet / 60)),
            "gap_label": "не чаще раза в %d мин" % max(1, round(gap / 60))}


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


def _norm_proposal(value) -> dict | None:
    """Чтение реестра: предложение этапов руководителем (W20 §1.7).

    Мусор и пустое предложение — `None` (предложения нет). Ошибок чтения нет:
    реестр комнат важнее формы одного поля.
    """
    return phase_mod.norm_proposal(value)


def _settle_proposal(phases, proposal) -> dict | None:
    """После правки этапов пользователем: совпало с предложением — оно принято.

    Плашка «Руководитель предлагает этапы» не должна висеть, если человек сам
    собрал в редакторе ровно те же этапы: это и есть его решение.
    """
    return _norm_proposal(phase_mod.reconcile(phases, proposal))


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


def _check_agent_models(cfg, value, members, *, previous=None, warnings=None) -> dict:
    """Модель/провайдер/мышление участников (WAVE10): только настроенный провайдер.

    Профиль — из состава комнаты, пара «провайдер+модель» обязательна, уровень мышления —
    из списка движка. Ошибки — RoomsError (роут отдаст 400 по-русски).

    Дефект 10: `previous` — текущая карта комнаты. Проверяется только изменённый участник,
    остальные пишутся как есть; предупреждения складываются в `warnings` (не отказ).
    """
    mod = _agent_models_mod()
    try:
        return mod.validate_map(cfg, value, members=members, previous=previous,
                                warnings=warnings)
    except mod.ModelError as exc:
        raise RoomsError(exc.message, "bad_request", dict(exc.detail or {})) from exc


def _norm_agent_models(value) -> dict:
    """Чтение реестра: мусор и записи убранных участников не роняют список комнат."""
    try:
        return _agent_models_mod().normalize(value)
    except Exception:  # noqa: BLE001 - реестр комнат важнее моделей
        return {}


def _check_routine_model(cfg, value, *, warnings=None) -> dict:
    """«Модель для рутинного сведения» (W29-Q9): пара из каталога или пусто.

    Пустое значение — «как у руководителя» (поведение по умолчанию). Модель
    проверяется тем же `agent_models.validate`, что и модели участников: чужой
    провайдер/несуществующая модель — ошибка с полем, а не тихая подмена.
    """
    if value in (None, "", {}):
        return {}
    if not isinstance(value, dict):
        raise RoomsError("routine_model должен быть объектом {provider, model}",
                         "bad_request", {"field": "routine_model"})
    mod = _agent_models_mod()
    try:
        clean, warns = mod.validate(cfg, value.get("provider"), value.get("model"),
                                    value.get("reasoning"))
    except mod.ModelError as exc:
        raise RoomsError(exc.message, "bad_request", dict(exc.detail or {})) from exc
    for text in warns or []:
        if warnings is not None:
            warnings.append(text)
    if not (clean.get("provider") and clean.get("model")):
        return {}
    clean["updated_at"] = _now()
    return clean


def _norm_routine_model(value) -> dict:
    """Мягкое чтение поля: мусор в файле — «как у руководителя», список не падает."""
    try:
        return _agent_models_mod().routine_model({"routine_model": value})
    except Exception:  # noqa: BLE001 - реестр комнат важнее модели рутины
        return {}


def _drop_default_overrides(cfg, models: dict, members) -> dict:
    """Новая комната: не хранить записи, совпадающие с настройками самих агентов (W20 §1.5).

    Переопределение комнаты — это ВЫБОР пользователя. Если при создании прислали ровно
    то, что и так стоит в профиле (бывает, когда интерфейс отправляет показанные
    значения), хранить это нельзя: запись навсегда перекрыла бы будущую смену
    настроек профиля и выглядела бы как «выбрано в комнате». Сравниваем провайдера,
    модель и мышление; профиль, которого нет в составе, уже отсеян `validate_map`.
    """
    if not models:
        return {}
    mod = _agent_models_mod()
    out: dict = {}
    for profile, item in models.items():
        if not isinstance(item, dict):
            continue
        try:
            default = mod.default_info(cfg, profile)
        except Exception:  # noqa: BLE001 - не прочитали настройки: запись оставляем
            default = {}
        same = (item.get("provider") or "") == (default.get("provider") or "") \
            and (item.get("model") or "") == (default.get("model") or "") \
            and (item.get("reasoning") or "") == (default.get("reasoning") or "")
        if same and (default.get("provider") and default.get("model")):
            continue
        out[profile] = item
    return out


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
    svod = svod_block(room)
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
        # W20 §1.7 (F-2): предложение этапов руководителем — ждёт решения человека.
        "phases_proposal": _norm_proposal(room.get("phases_proposal")),
        # Особые указания участникам (WAVE9 §9): нет поля — пусто.
        "agent_notes": _norm_agent_notes(room.get("agent_notes")),
        # Журнал изменений указаний (WAVE14 §13): кто/что/зачем/источник, новые сверху.
        "agent_notes_log": _norm_notes_log(room.get("agent_notes_log")),
        # Модель/провайдер/мышление участников (WAVE10): нет записи — «как у агента».
        "agent_models": _norm_agent_models(room.get("agent_models")),
        # W29-Q9: модель рутинного сведения — нет поля/пусто → модель руководителя.
        "routine_model": _norm_routine_model(room.get("routine_model")),
        # «Ход работы» (W20 §1.6): у старых комнат полей нет — «подробно» и раз в 2 минуты.
        "worklog_mode": _norm_worklog_mode(room.get("worklog_mode")),
        "digest_every_min": _norm_digest_min(room.get("digest_every_min")),
        "worklog": worklog_block(room.get("worklog_mode"), room.get("digest_every_min")),
        # Сторож зависаний (W25-M2): пороги комнаты; нет поля — умолчания 10/20/вкл.
        "card_watchdog": _norm_card_watchdog(room.get("card_watchdog")),
        # Живой свод (W20-S1 §1.1): у старых комнат полей нет — обновляется сам,
        # «тишина» 3 минуты, потолок частоты 10 минут.
        "auto_svod": svod["auto_svod"],
        "auto_svod_quiet_s": svod["auto_svod_quiet_s"],
        "auto_svod_min_gap_s": svod["auto_svod_min_gap_s"],
        "svod": svod,
        # Контроль моделей (W23 §2.3): у старых комнат поля нет — «строго».
        "model_guard": _norm_model_guard(room.get("model_guard")),
        "model_guard_label": model_guard_block(room.get("model_guard"))["label"],
        "model_guard_block": model_guard_block(room.get("model_guard")),
        # Дежурства комнаты (W24 §3.2): id заданий Hermes; у старых комнат поля нет — пусто.
        "duties": _norm_duties(room.get("duties")),
        # Уровень автономии (W27-E5 §2): у старых комнат поля нет — «Рабочий» по умолчанию,
        # но профили НЕ меняются: уровень применяется только по действию человека.
        "autonomy": _norm_autonomy(room.get("autonomy")),
        "created_at": int(room.get("created_at") or 0),
        # Порядок комнат в меню (W29-P7): порядок, закрепление и группа — поля реестра.
        "order": _norm_order(room.get("order")),
        "pinned": bool(room.get("pinned")),
        "group": _norm_group(room.get("group")),
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


def _menu_key(room: dict) -> tuple:
    """Ключ порядка меню (W29-P7): закреплённые сверху, затем `order`, затем создание.

    Комнаты, у которых порядок ещё не задавали (`order` = 0), встают по времени
    создания — как было до волны 29. Один и тот же порядок на сервере и в меню:
    интерфейс не пересортировывает список по-своему.
    """
    order = _norm_order(room.get("order"))
    return (0 if room.get("pinned") else 1,
            order if order else 10 ** 9,
            int(room.get("created_at") or 0),
            str(room.get("id") or ""))


def list_rooms(cfg, include_archived: bool = False) -> list[dict]:
    """Все комнаты (полные Room) в порядке меню: закреплённые, затем `order`."""
    rooms = [_norm_room(r) for r in _load(cfg)["rooms"] if isinstance(r, dict)]
    if not include_archived:
        rooms = [r for r in rooms if not r["archived"]]
    rooms.sort(key=_menu_key)
    return rooms


def list_groups(cfg) -> list[dict]:
    """Группы комнат из реестра (W29-P7): `[{id, title, collapsed}]`."""
    return _groups_doc(_load(cfg))


def order_sequence(rooms: list[dict], ids: list[str]) -> list[str]:
    """Полная последовательность id: сначала присланные, затем остальные как были.

    Нужна, чтобы «перетащили одну комнату» не сбрасывало места остальных: порядок
    всегда остаётся полным и без дыр, даже если клиент прислал часть списка.
    """
    known = [str(r.get("id")) for r in rooms]
    out: list[str] = []
    seen: set[str] = set()
    for raw in ids or []:
        rid = str(raw or "").strip()
        if rid and rid not in seen:
            out.append(rid)
            seen.add(rid)
    for rid in known:
        if rid not in seen:
            out.append(rid)
            seen.add(rid)
    return out


def check_layout(cfg, payload) -> tuple[dict, list[dict]]:
    """Проверить раскладку меню ДО записи: `(порядок_и_флаги, группы)`.

    Формат тела `PUT /api/rooms/order`:
        {"order": ["cafe", "laundry", …],          # необязательно
         "groups": [{"id","title","collapsed"}],   # необязательно, полная замена
         "rooms": {"cafe": {"pinned": true, "group": "g1"}}}   # необязательно

    Возвращает `({room_id: {…поля…}}, группы)`; ошибки — RoomsError по-русски.
    Ничего не пишет: запись делает `apply_layout` под замком.
    """
    if not isinstance(payload, dict):
        raise RoomsError("Раскладка меню — объект", "bad_request", {"field": "body"})
    data = _load(cfg)
    known = [str(r.get("id")) for r in data["rooms"] if isinstance(r, dict)]
    groups = _check_groups(payload["groups"]) if "groups" in payload else _groups_doc(data)
    group_ids = {g["id"] for g in groups}

    fields: dict[str, dict] = {}
    if "order" in payload:
        raw_order = payload.get("order")
        if not isinstance(raw_order, list):
            raise RoomsError("Порядок комнат — список id", "bad_request", {"field": "order"})
        seen: set[str] = set()
        for raw in raw_order:
            rid = str(raw or "").strip()
            if not rid:
                raise RoomsError("В порядке комнат пустой id", "bad_request", {"field": "order"})
            if rid not in known:
                raise RoomsError("Комнаты «%s» нет — порядок не сохранён" % rid, "bad_request",
                                 {"field": "order", "value": rid, "known": known[:20]})
            if rid in seen:
                raise RoomsError("Комната «%s» в списке дважды" % rid, "bad_request",
                                 {"field": "order", "value": rid, "reason": "duplicate"})
            seen.add(rid)
        for pos, rid in enumerate(order_sequence(data["rooms"], raw_order)):
            fields.setdefault(rid, {})["order"] = pos + 1

    raw_rooms = payload.get("rooms")
    if raw_rooms is not None:
        if not isinstance(raw_rooms, dict):
            raise RoomsError("Правки комнат — объект {id: {pinned, group}}", "bad_request",
                             {"field": "rooms"})
        for rid, patch in raw_rooms.items():
            rid = str(rid or "").strip()
            if rid not in known:
                raise RoomsError("Комнаты «%s» нет" % rid, "bad_request",
                                 {"field": "rooms", "value": rid})
            if not isinstance(patch, dict):
                raise RoomsError("Правка комнаты — объект", "bad_request",
                                 {"field": "rooms." + rid})
            extra = sorted(set(patch) - {"pinned", "group"})
            if extra:
                raise RoomsError("В раскладке меняются только закрепление и группа",
                                 "bad_request", {"fields": extra, "allowed": ["pinned", "group"]})
            target = fields.setdefault(rid, {})
            if "pinned" in patch:
                target["pinned"] = _check_bool(patch["pinned"], "pinned")
            if "group" in patch:
                group = _norm_group(patch["group"])
                if group and group not in group_ids:
                    raise RoomsError("Группы «%s» нет — комната осталась на месте" % group,
                                     "bad_request", {"field": "rooms." + rid + ".group",
                                                     "value": group})
                target["group"] = group
    return fields, groups


def apply_layout(cfg, payload) -> dict:
    """Сохранить порядок/закрепление/группы комнат одним движением (W29-P7).

    Чтение-проверка-запись под замком реестра: два окна не затрут друг друга.
    Возвращает `{changed, rooms, groups}` — комнаты уже в порядке меню.
    """
    path = _rooms_path(cfg)
    with sf.lock_for(path):
        fields, groups = check_layout(cfg, payload)
        data = _load(cfg)
        changed = data.get("groups") != groups
        data["groups"] = groups
        valid_groups = {g["id"] for g in groups}
        for room in data["rooms"]:
            if not isinstance(room, dict):
                continue
            rid = str(room.get("id"))
            patch = fields.get(rid) or {}
            for key, value in patch.items():
                if room.get(key) != value:
                    room[key] = value
                    changed = True
            # Группу удалили — комната остаётся в меню, просто без группы.
            if _norm_group(room.get("group")) not in valid_groups and room.get("group"):
                room["group"] = ""
                changed = True
        if changed:
            _save(cfg, data)
        rooms = [_norm_room(r) for r in data["rooms"] if isinstance(r, dict) and not r.get("archived")]
        rooms.sort(key=_menu_key)
        return {"changed": changed, "rooms": rooms, "groups": groups}


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
        # W20 §1.7: предложение этапов появляется только от руководителя — при создании его нет.
        "phases_proposal": _norm_proposal(data.get("phases_proposal")),
        # Указания — участникам этой комнаты (профили сверяем с составом этого же запроса).
        "agent_notes": _check_agent_notes(
            data.get("agent_notes"),
            [orchestrator] + [str(m.get("profile") or "").strip() for m in members]
            + ([str(data.get("critic") or "").strip()] if data.get("critic") else []),
        ),
        # Модели участников этой комнаты (WAVE10): так же — профиль из состава этого запроса.
        # W20 §1.5: при создании override'ов НЕТ — только явный выбор пользователя,
        # поэтому записи, равные настройкам самих агентов, не храним.
        "agent_models": _drop_default_overrides(
            cfg,
            _check_agent_models(
                cfg, data.get("agent_models"),
                [orchestrator] + [str(m.get("profile") or "").strip() for m in members]
                + ([str(data.get("critic") or "").strip()] if data.get("critic") else []),
            ),
            [orchestrator] + [str(m.get("profile") or "").strip() for m in members]
            + ([str(data.get("critic") or "").strip()] if data.get("critic") else []),
        ),
        # W29-Q9: модель для рутинного сведения (автосвод, «Уточнение»). При создании
        # обычно пусто — «как у руководителя»; выбор делается в настройках комнаты.
        "routine_model": _check_routine_model(cfg, data.get("routine_model")),
        # «Ход работы» (W20 §1.6): по умолчанию подробно, промежуточный итог раз в 2 минуты.
        "worklog_mode": _check_worklog_mode(data.get("worklog_mode")),
        "digest_every_min": (_check_digest_min(data["digest_every_min"])
                             if "digest_every_min" in data else DEFAULT_DIGEST_MIN),
        # Живой свод (W20-S1 §1.1): новая комната обновляет свод сама.
        "auto_svod": (_check_auto_svod(data["auto_svod"]) if "auto_svod" in data
                      else DEFAULT_SVOD_AUTO),
        "auto_svod_quiet_s": (_check_svod_quiet(data["auto_svod_quiet_s"])
                              if "auto_svod_quiet_s" in data else DEFAULT_SVOD_QUIET_S),
        "auto_svod_min_gap_s": (_check_svod_gap(data["auto_svod_min_gap_s"])
                                if "auto_svod_min_gap_s" in data else DEFAULT_SVOD_MIN_GAP_S),
        # Контроль моделей (W23 §2.3): новая комната — «строго» (требование 17.11).
        "model_guard": (_check_model_guard(data["model_guard"]) if "model_guard" in data
                        else DEFAULT_MODEL_GUARD),
        # Дежурства (W24 §3.2): при создании обычно пусто — привязка идёт отдельным PUT.
        "duties": _check_duties(data.get("duties")),
        "created_at": now,
        # W29-P7: новая комната встаёт в конец меню, без закрепления и без группы.
        "order": 0,
        "pinned": False,
        "group": "",
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
        # W29-P7: порядок — «после всех», чтобы новая комната не влезла в середину.
        room["order"] = max([_norm_order(r.get("order")) for r in current["rooms"]
                             if isinstance(r, dict)] or [0]) + 1
        current["rooms"].append(room)
        _save(cfg, current)
    return room


def update_room(cfg, room_id: str, patch: dict, *, warnings=None) -> dict:
    """Частичное обновление; id/board/tenant менять нельзя.

    `warnings` — необязательный список: сюда складываются предупреждения о записях, которые
    приняты без проверки каталога (дефект 10: «модель не в каталоге — используется как
    выбрано»). Отказом они не становятся.
    """
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
                    # W20 §1.7: человек сам собрал ровно предложенные этапы — предложение
                    # считается принятым (плашка не висит зря). Порядок/названия — его.
                    room["phases_proposal"] = _settle_proposal(room["phases"],
                                                               room.get("phases_proposal"))
                elif key == "phases_proposal":
                    # Пишут только внутренние маршруты (phases/*) и приём metadata;
                    # из интерфейса комнаты поле не редактируется.
                    room["phases_proposal"] = _norm_proposal(value)
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
                    # Дефект 10: каталогом проверяем ТОЛЬКО изменённых участников; записи
                    # без изменений (например, у кого поменяли лишь мышление) идут как есть.
                    room["agent_models"] = _check_agent_models(
                        cfg, value, [m for m in members if m],
                        previous=room.get("agent_models"), warnings=warnings)
                elif key == "routine_model":
                    # W29-Q9: модель рутинного сведения; пусто — «как у руководителя».
                    room["routine_model"] = _check_routine_model(cfg, value, warnings=warnings)
                elif key == "worklog_mode":
                    room["worklog_mode"] = _check_worklog_mode(value)
                elif key == "digest_every_min":
                    room["digest_every_min"] = _check_digest_min(value)
                elif key == "auto_svod":
                    # Живой свод (W20-S1 §1.1): переключатель «Автообновление свода».
                    room["auto_svod"] = _check_auto_svod(value)
                elif key == "auto_svod_quiet_s":
                    room["auto_svod_quiet_s"] = _check_svod_quiet(value)
                elif key == "auto_svod_min_gap_s":
                    room["auto_svod_min_gap_s"] = _check_svod_gap(value)
                elif key == "model_guard":
                    # W23 §2.3: «строго» (исправляем сами) | «только предупреждать».
                    room["model_guard"] = _check_model_guard(value)
                elif key == "duties":
                    # W24 §3.2: привязка дежурств Hermes к комнате (только id, без правки Hermes).
                    room["duties"] = _check_duties(value)
                elif key == "autonomy":
                    # W27-E5 §2: выбор уровня автономии. Пишет только маршрут
                    # `POST /api/rooms/{id}/autonomy` — после записи в профили.
                    room["autonomy"] = _check_autonomy(value)
                else:
                    room[key] = value
            if "project_dir" in patch:
                _warm_project_dir(cfg, room)  # папка создаётся до записи реестра
            data["rooms"][idx] = room
            _save(cfg, data)
            # W29-P10: выбор модели комнаты — в служебную таблицу доски (только если защита
            # установлена человеком). Ошибка синхронизации не мешает сохранить выбор.
            if "agent_models" in patch:
                try:                               # noqa: SIM105 - вторичный шаг
                    from . import model_trigger as trigger_mod
                except ImportError:                # pragma: no cover - плоский импорт
                    trigger_mod = None             # type: ignore
                if trigger_mod is not None:
                    trigger_mod.sync_room(cfg, room)
            return _norm_room(room)
    raise RoomsError("Комната не найдена", "not_found", {"room": str(room_id)})


_UNSET = object()


def set_phases_state(cfg, room_id: str, phases=_UNSET, proposal=_UNSET, compute=None) -> dict:
    """W20 §1.7 (P1): записать этапы и/или предложение этапов ОДНИМ движением.

    Нужно, потому что решение «принять этапы» меняет сразу два поля комнаты, а
    между чтением и записью предложение может обновиться. Всё чтение-решение-
    запись идёт под замком реестра комнат:

    * `phases` — новый список этапов комнаты (проверяется как в PATCH);
    * `proposal` — новое предложение (`None` — «предложения нет»);
    * `compute(room) -> {поле: значение}` — решение считается на СВЕЖЕЙ копии
      комнаты (так руководитель не затирает правки, сделанные секунду назад).

    Возвращает `{changed, room}`; комната — в каноническом виде (`_norm_room`).
    """
    path = _rooms_path(cfg)
    with sf.lock_for(path):
        data = _load(cfg)
        for idx, room in enumerate(data["rooms"]):
            if str(room.get("id")) != str(room_id):
                continue
            patch: dict = {}
            if callable(compute):
                got = compute(dict(room)) or {}
                if not isinstance(got, dict) or set(got) - {"phases", "phases_proposal"}:
                    raise RoomsError("Решение по этапам: неизвестные поля", "bad_request",
                                     {"fields": sorted(got) if isinstance(got, dict) else []})
                patch.update(got)
            if phases is not _UNSET:
                patch["phases"] = phases
            if proposal is not _UNSET:
                patch["phases_proposal"] = proposal
            if not patch:
                return {"changed": False, "room": _norm_room(room)}
            # Порядок важен: сначала этапы человека, затем явное предложение
            # (при «принять» приходит и то, и другое — итог решает `decide`).
            if "phases" in patch:
                room["phases"] = _check_phases(patch["phases"])
                room["phases_proposal"] = _settle_proposal(room["phases"],
                                                           room.get("phases_proposal"))
            if "phases_proposal" in patch:
                room["phases_proposal"] = _norm_proposal(patch["phases_proposal"])
            data["rooms"][idx] = room
            _save(cfg, data)
            return {"changed": True, "room": _norm_room(room)}
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


def set_member_pinned(cfg, room_id: str, profile: str, pinned: bool) -> dict:
    """«Закрепить в составе / Открепить» обычного исполнителя (W19 B-28).

    Закрепление — флаг реестра комнаты: участник остаётся в составе, даже если
    задачи давно не приходят. Руководитель комнаты и критик закреплены всегда
    (они — настройки комнаты), поэтому для них операция отклоняется.

    Возвращает `{profile, pinned, members}` — как ждёт `PATCH …/members/{profile}`.
    """
    profile = str(profile or "").strip()
    value = bool(pinned)
    path = _rooms_path(cfg)
    with sf.lock_for(path):
        data = _load(cfg)
        for idx, room in enumerate(data["rooms"]):
            if str(room.get("id")) != str(room_id):
                continue
            if profile and profile in (str(room.get("orchestrator") or ""),
                                       str(room.get("critic") or "")):
                raise RoomsError(
                    "Руководитель комнаты и критик всегда в составе — их роль задаётся "
                    "настройками комнаты", "conflict",
                    {"reason": "mandatory_role", "profile": profile},
                )
            members = list(room.get("members") or [])
            for pos, member in enumerate(members):
                if str(member.get("profile")) != profile:
                    continue
                fresh = dict(member)
                fresh["pinned"] = value
                members[pos] = fresh
                room["members"] = members
                data["rooms"][idx] = room
                _save(cfg, data)
                return {"profile": profile, "pinned": value, "members": members}
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
        "orchestrator": "team-director",
        "critic": "team-critic",
        "members": [
            {"profile": "team-lawyer", "role": "worker", "note": "Юрист: ИП, СЭС, Роспатент, договоры", "pinned": True, "added_at": SEED_CREATED},
            {"profile": "team-ops", "role": "worker", "note": "Операции: поставщики, упаковка, доставка", "pinned": True, "added_at": SEED_CREATED},
            {"profile": "team-strategy", "role": "worker", "note": "Стратегия, бренд, план 90 дней", "pinned": False, "added_at": SEED_CREATED},
            {"profile": "team-marketing", "role": "worker", "note": "Каналы, контент, D19", "pinned": False, "added_at": SEED_CREATED + 100},
            {"profile": "team-cfo", "role": "worker", "note": "Экономика, налоги, сметы", "pinned": False, "added_at": SEED_CREATED + 100},
            {"profile": "team-runner", "role": "worker", "note": "Сбор фактов по Vault и доске", "pinned": False, "added_at": SEED_CREATED + 200},
            {"profile": "team-critic", "role": "critic", "note": "Проверяет свод перед ответом Михаилу", "pinned": True, "added_at": SEED_CREATED + 200},
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
