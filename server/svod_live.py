"""Живой свод (W20-S1, B-36): версия свода растёт сама от событий запроса.

Модуль НИЧЕГО не запускает сам: он решает, когда пора обновить свод, и ставит
карточку обновления ТЕМ ЖЕ путём, что кнопка «Доработать с учётом ответов»
(`engine_write.create_child_card` + `contract.build_revision_body` + тот же
дайджест «что появилось после версии»). Модель запускает обычный движок —
приложение только пишет карточку, как и раньше.

Смысл (контракт волны 20, §1.1):
* события — ответы на вопросы, сообщения Михаила в диалоге, решения и
  завершённые карточки работы (доработки/критик/«Уточнение» и сами карточки
  обновления свода событием НЕ считаются — иначе свод обновлялся бы вечно);
* «тишина» (`auto_svod_quiet_s`) — ждём, пока события перестанут идти;
* минимальный интервал (`auto_svod_min_gap_s`) — не чаще раза в N минут;
* идемпотентность — один запуск на набор событий (ключ набора в реестре).

Волна 21 (W21-S2) добавила четыре правила поверх этого:
* ответ на вопрос руководителя — приоритетное событие: «тишина» 60 с, потолок частоты не
  держит (`answer_priority`, `quiet_for`, `update_time`);
* пока на запросе идёт работа команды, свод не собирается (`busy_cards`,
  состояние `waiting_cards`);
* строки «Снять — неактуально»/«Вернуть» у решения событием не считаются (`_is_status_action`);
* после неудачной карточки обновления — «остывание» 10 → 20 → 40 мин, максимум 2 ч
  (`failed_updates`, `reason="cooldown"`), и честная причина, если отметку о старте
  сохранить нельзя (`state_problem`, `log_problem`).

Один смысл — одно место: что именно «появилось после версии», считает
`contract.revision_digest` (там же, где тело карточки и предпросмотр в интерфейсе).
"""
from __future__ import annotations

import hashlib
import re
import time
from pathlib import Path

try:                                     # пакетный импорт (server.svod_live)
    from . import answers as _answers
    from . import contract as _contract
    from . import engine_write as _engine
    from . import statefiles as _sf
    from .engine_db import SVOD_KEY_PREFIX
except ImportError:                      # python server/app.py — «плоский» импорт
    import answers as _answers           # type: ignore
    import contract as _contract         # type: ignore
    import engine_write as _engine       # type: ignore
    import statefiles as _sf             # type: ignore
    from engine_db import SVOD_KEY_PREFIX  # type: ignore

# --- значения по умолчанию и границы (контракт §1.1, docs/API.md «W20-SVOD») ---
DEFAULT_AUTO = True
DEFAULT_QUIET_S = 180
DEFAULT_MIN_GAP_S = 600
QUIET_RANGE = (30, 3600)
GAP_RANGE = (60, 86400)
#: W28-E: накопление перед автообновлением свода. Пока событий меньше ``min_events``
#: и с первого необработанного прошло меньше ``max_wait_s`` — руководителя не будим
#: (замеры 19.11: автосвод стоил 8–38 вызовов модели на каждое сообщение).
DEFAULT_MIN_EVENTS = 3
DEFAULT_MAX_WAIT_S = 900
EVENTS_RANGE = (1, 10)
MAX_WAIT_RANGE = (60, 86400)
ACCUMULATE_TEXT = "накопится событий или %s (кнопка «Обновить сейчас»)"

#: Заголовок карточки-сообщения (server/thread.py NOTE_TITLE). Карточки сообщений —
#: не «работа» для свода: их содержимое уже учтено как сообщение в диалоге.
NOTE_TITLE = "Уточнение от Михаила"
#: С чего начинается заметка автоматической карточки (её видит руководитель).
AUTO_NOTE_PREFIX = "Автообновление: "
#: Роли карточек, которые сами являются версиями свода или его проверкой.
SERVICE_ROLES = ("root", "critic", "revision", "synthesizer")
CLOSED_STATUSES = ("done", "archived")
#: Карточку обновления создали, но в дереве её ещё нет — считаем «идёт».
BUSY_GRACE_S = 900
#: Файл «льготного старта» в каталоге состояния: когда автообновление включилось
#: и сколько карточек поставлено за последний час по каждой комнате.
START_FILE = "svod_started.json"
#: Не больше N автоматических карточек в час на комнату: лавина доработок по
#: массе накопившихся событий недопустима (Михаил увидел бы очередь из карточек).
MAX_CARDS_PER_HOUR = 6
HOUR_S = 3600
#: W21-S2 (B-45): ответ на вопрос руководителя — приоритетное событие. Человек уже
#: ответил, ждать полной «тишины» незачем: 60 с вместо 180 с, и потолок частоты такую
#: доработку не задерживает. Лимит `MAX_CARDS_PER_HOUR` при этом остаётся.
ANSWERS_QUIET_S = 60
#: W21-S2 (B-46): статусы карточек команды, из-за которых свод собирать рано — их ещё
#: ведут руководитель и исполнители, и собранная версия тут же оказалась бы устаревшей.
WAIT_STATUSES = ("triage", "todo", "ready", "running", "review", "scheduled", "blocked")
WAITING_CARDS_TEXT = "Обновится после завершения работы команды"
#: W21-REV2: потолок ожидания карточек команды. Карточка «в работе», у которой нет
#: признаков жизни (в дереве есть только время старта — сердцебиений оно не знает),
#: держала бы свод вечно: страница неделями показывала бы «Обновится после
#: завершения работы команды». Через два часа ожидания свод собирается, а причина
#: пишется в `live.text` и в заметку карточки («…не дожидаясь карточек»).
BUSY_MAX_S = 7200
BUSY_MAX_TEXT = "работа команды идёт дольше двух часов"
#: W21-S2 (остатки ревью волны 1): «остывание» после неудачного обновления —
#: 10 → 20 → 40 минут, но не больше двух часов. Иначе сторож пробовал бы снова
#: на каждом тике (каждые 10–15 с) и шумел в журнале.
COOLDOWN_BASE_S = 600
COOLDOWN_MAX_S = 7200
#: Сколько попыток подряд делаем сами, прежде чем оставить решение человеку
#: (кнопка «Обновить сейчас» работает всегда).
MAX_AUTO_RETRIES = 5
COOLDOWN_TEXT = "Обновление не удалось — повтор в %s, кнопка «Обновить сейчас» работает"
#: B-49: защита от цикла «карточка вышла, а версии нет». Набор событий (`event_key`) тот же,
#: руководитель тот же — повторная карточка почти наверняка снова не даст версии (в боевом
#: случае так сгорели три запуска подряд). Поэтому сторож останавливается уже после ПЕРВОЙ
#: успешной карточки, оставившей события «неучтёнными» (`LOOP_GUARD_AFTER`).
LOOP_GUARD_AFTER = 1
#: Жёсткий потолок на один набор событий: третьей автоматической карточки не будет ни при
#: каких условиях — даже если две уже были выпущены до этого исправления (правило «не больше
#: двух автопопыток на набор событий»). Дальше — только кнопка «Обновить сейчас».
MAX_AUTO_TRIES_PER_KEY = 2
LOOP_STALE_TEXT = "Обновление выполнено, но что-то не учтено — нажмите «Обновить сейчас»"
#: Заголовок карточки обновления, которую ставит приложение (`create_update_card`).
#: B-49: по нему карточка опознаётся, даже если запись реестра потерялась.
UPDATE_TITLE_PREFIX = "Обновление свода"
#: Ключ РУЧНОЙ карточки обновления («Обновить сейчас», «Доработать с учётом ответов»):
#: `svod-<rid>-m<версия>-<отпечаток>`. У сторожа — `svod-<rid>-<отпечаток>` (см. `_is_manual`).
_MANUAL_KEY = re.compile(r"-m\d+-")
#: Статусы карточки обновления, которыми попытка считается неудачной.
FAILED_CARD_STATUSES = ("failed", "crashed", "blocked", "timeout")
#: Подпись «учтено, менять нечего» — одна на `delivery` и на строки снятия решения.
NO_CHANGE_TEXT = "Учтено, менять нечего"
#: Запасной «старт» для случая без каталога состояния (тесты, чужая сборка):
#: один на процесс, чтобы поведение было предсказуемым.
_FALLBACK_START = {"started_at": 0, "launches": {}}


class SvodError(Exception):
    """Ошибка живого свода: ``code`` — как у API (``conflict`` → 409)."""

    def __init__(self, code: str, message: str, detail: dict | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.detail = detail or {}


# --------------------------------------------------------------- настройки ---
def _int(value, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _clamp(value, bounds: tuple, default: int) -> int:
    number = _int(value, -1)
    if number < 0:
        return default
    return max(bounds[0], min(bounds[1], number))


def settings(room) -> dict:
    """Настройки автообновления комнаты с границами.

    ``{auto_svod, quiet_s, min_gap_s, min_events, max_wait_s}``. Нет поля (старая
    запись комнаты) — значения по умолчанию контракта. Мусор в поле не ломает свод:
    берём значение по умолчанию.

    W28-E: ``min_events`` / ``max_wait_s`` — накопление. Замеры 19.11: автоматическое
    «Обновление свода» могло идти 8–38 вызовов модели на КАЖДОЕ сообщение (2,9–5,4 млн
    токенов кэш-чтения за сутки). Теперь автообновление ждёт накопления: не меньше
    ``min_events`` событий ИЛИ ``max_wait_s`` секунд с первого необработанного —
    что наступит раньше. Кнопка «Обновить сейчас» работает мгновенно и настроек не
    спрашивает. Дефолт безопасный для публичного продукта (3 события / 15 минут);
    комната может поставить ``auto_svod_min_events: 1`` — тогда поведение прежнее.
    """
    room = room if isinstance(room, dict) else {}
    auto = room.get("auto_svod")
    return {"auto_svod": DEFAULT_AUTO if auto is None else bool(auto),
            "quiet_s": _clamp(room.get("auto_svod_quiet_s"), QUIET_RANGE, DEFAULT_QUIET_S),
            "min_gap_s": _clamp(room.get("auto_svod_min_gap_s"), GAP_RANGE, DEFAULT_MIN_GAP_S),
            "min_events": _clamp(room.get("auto_svod_min_events"), EVENTS_RANGE, DEFAULT_MIN_EVENTS),
            "max_wait_s": _clamp(room.get("auto_svod_max_wait_s"), MAX_WAIT_RANGE, DEFAULT_MAX_WAIT_S)}


def accumulate(ev: dict, conf: dict, now=None) -> dict:
    """Хватит ли событий, чтобы будить руководителя (W28-E).

    ``{ok, total, need, first_at, wait_left}``. ``ok`` — накопление закрыто: событий
    не меньше ``min_events`` ИЛИ с первого необработанного прошло ``max_wait_s``.
    Ответ на вопрос руководителя (B-45) накопления не ждёт: человек уже ответил, и
    ждать «ещё два события» означало бы держать его вопрос без ответа.
    """
    now = _now(now)
    ev = ev if isinstance(ev, dict) else {}
    conf = conf if isinstance(conf, dict) else {}
    total = _int(ev.get("total"), 0)
    need = max(1, _int(conf.get("min_events"), DEFAULT_MIN_EVENTS))
    max_wait = max(0, _int(conf.get("max_wait_s"), DEFAULT_MAX_WAIT_S))
    first_at = 0
    for item in ev.get("items") or []:
        at = _int((item or {}).get("at"), 0) if isinstance(item, dict) else 0
        if at and (not first_at or at < first_at):
            first_at = at
    if not first_at:
        first_at = _int(ev.get("first_at"), 0) or _int(ev.get("last_at"), 0)
    wait_left = max(0, (first_at + max_wait) - now) if first_at and max_wait else 0
    ok = total >= need or not wait_left or answer_priority(ev)
    return {"ok": bool(ok), "total": total, "need": need, "first_at": first_at or None,
            "wait_left": int(wait_left)}


def _now(now=None) -> int:
    return _int(now, 0) or int(time.time())


# ------------------------------------------------- «льготный старт» и лимит ---
def _start_path(cfg) -> Path | None:
    """Путь файла «льготного старта» (`<каталог состояния>/svod_started.json`)."""
    state_dir = getattr(cfg, "state_dir", None) if cfg is not None else None
    if not state_dir:
        return None
    try:
        return Path(str(state_dir)) / START_FILE
    except (TypeError, ValueError):
        return None


def _load_start(cfg, now=None) -> dict:
    """`{started_at, launches, readonly}` — когда включилось и что уже поставили.

    Файла нет — создаём с текущим временем (первый запуск этой версии: всё, что
    случилось раньше, автообновление не поднимает). Файл занят/нечитаем — считаем
    «старт сейчас» и НЕ пишем поверх (иначе потеряем историю лимита).
    """
    now = _now(now)
    path = _start_path(cfg)
    if path is None:
        if not _int(_FALLBACK_START.get("started_at"), 0):
            _FALLBACK_START["started_at"] = now
        return _FALLBACK_START
    try:
        data, info = _sf.read_json_doc(path, None)
    except Exception:                                    # noqa: BLE001 - состояние вторично
        return {"started_at": now, "launches": {}, "readonly": True}
    state = str((info or {}).get("state") or "")
    if state in ("unreadable",):
        return {"started_at": now, "launches": {}, "readonly": True}
    if state == "corrupt":
        try:
            _sf.quarantine(path)                         # копия ДО перезаписи
        except Exception:                                # noqa: BLE001
            pass
        data = None
    data = data if isinstance(data, dict) else {}
    started = _int(data.get("started_at"), 0)
    launches = data.get("launches") if isinstance(data.get("launches"), dict) else {}
    out = {"started_at": started or now, "launches": launches, "readonly": False}
    if not started:
        _save_start(path, out)                           # первый запуск: запоминаем момент
    return out


def _save_start(path, state: dict) -> bool:
    """Записать файл «льготного старта» (атомарно, под блокировкой файла)."""
    if path is None:
        return False
    payload = {"started_at": _int((state or {}).get("started_at"), 0),
               "launches": {str(k): [_int(t, 0) for t in (v or [])][-20:]
                            for k, v in ((state or {}).get("launches") or {}).items()
                            if isinstance(v, list)}}
    try:
        with _sf.lock_for(path):
            _sf.write_json_atomic(path, payload)
        return True
    except Exception:                                    # noqa: BLE001 - свод не должен падать
        return False


def start_at(cfg, now=None) -> int:
    """Момент включения автообновления: события раньше него свод не поднимают.

    Зачем: после переноса в боевое дерево у пользователя есть старые запросы с
    «неучтёнными» ответами. Без порога первый же тик поставил бы по карточке на
    каждый такой запрос — очередь доработок из прошлого.
    """
    return _int(_load_start(cfg, now).get("started_at"), 0)


def launches_last_hour(cfg, room, now=None) -> int:
    """Сколько автоматических карточек обновления поставлено комнате за час."""
    now = _now(now)
    room_id = str((room or {}).get("id") or "") if isinstance(room, dict) else str(room or "")
    if not room_id:
        return 0
    state = _load_start(cfg, now)
    stamps = ((state.get("launches") or {}).get(room_id) or [])
    return len([t for t in (_int(x, 0) for x in stamps if isinstance(stamps, list)) if t > now - HOUR_S])


def hour_limit_reached(cfg, room, now=None) -> bool:
    """Пора остановиться: комната уже получила `MAX_CARDS_PER_HOUR` карточек за час."""
    return launches_last_hour(cfg, room, now) >= MAX_CARDS_PER_HOUR


def _note_launch(cfg, room, now=None) -> None:
    """Запомнить поставленную карточку (для лимита в час)."""
    now = _now(now)
    path = _start_path(cfg)
    state = _load_start(cfg, now)
    if state.get("readonly"):
        return
    room_id = str((room or {}).get("id") or "") if isinstance(room, dict) else str(room or "")
    if not room_id:
        return
    launches = dict(state.get("launches") or {})
    stamps = [t for t in (_int(x, 0) for x in (launches.get(room_id) or [])) if t > now - HOUR_S]
    stamps.append(now)
    launches[room_id] = stamps
    state = {"started_at": state.get("started_at"), "launches": launches}
    _save_start(path, state)
    if path is None:
        _FALLBACK_START["launches"] = launches


#: Сколько раз в час пишем в журнал про несохраняемую отметку старта (по комнатам).
_PROBLEM_LOGGED: dict = {}


def _problem_of(cfg, info) -> str:
    """Причина, по которой отметку «когда включилось» сохранить нельзя ('' — всё хорошо)."""
    if cfg is None:
        return ""
    if _start_path(cfg) is None:
        return "no_state_dir"
    if isinstance(info, dict) and info.get("readonly"):
        return "unreadable"
    return ""


PROBLEM_TEXT = {
    "no_state_dir": "нет каталога состояния",
    "unreadable": "отметка в папке данных недоступна",
}


def state_problem(cfg, now=None) -> str:
    """Почему автообновление не может запомнить свой старт (``''`` — может).

    Раньше такой случай молчал: `start_at` каждый раз возвращал «сейчас», свод
    фактически не обновлялся, и понять это было нельзя. Теперь причина видна
    в журнале сторожа (`log_problem`) и в `live.text` (`compute`).
    """
    if cfg is None:
        return ""
    try:
        return _problem_of(cfg, _load_start(cfg, now))
    except Exception:                                    # noqa: BLE001 - свод не падает
        return "unreadable"


def log_problem(cfg, room, log, now=None) -> str:
    """Записать в журнал сторожа, что отметка автообновления не сохраняется.

    Не чаще раза в час на комнату: иначе один и тот же сбой забивал бы журнал
    каждые 10–15 секунд (замечание ревью волны 1, §5.5).
    """
    problem = state_problem(cfg, now)
    if not problem or log is None:
        return problem
    now = _now(now)
    room_id = str((room or {}).get("id") or "") if isinstance(room, dict) else str(room or "")
    if now - _int(_PROBLEM_LOGGED.get(room_id), 0) >= HOUR_S:
        _PROBLEM_LOGGED[room_id] = now
        try:
            log.warning("живой свод: автообновление не может сохранить отметку (%s) — комната %s",
                        problem, room_id or "?")
        except Exception:                                # noqa: BLE001
            pass
    return problem


def _hhmm(ts) -> str:
    stamp = _int(ts, 0)
    if not stamp:
        return ""
    return time.strftime("%H:%M", time.localtime(stamp))


def _due_text(next_at, now, verb: str = "обновится") -> tuple:
    """``(next_update_at, «обновится в 14:05»)`` — но НИКОГДА про прошлое (B-49).

    Расчётное время легко оказывается позади: запрос сутки стоял «неучтённым» (свод
    собирался, а версия не зарегистрировалась), и «тишина + потолок частоты» давно прошли.
    Старый код всё равно печатал «обновится в 11:47» — время, которое прошло 18 часов
    назад, — и свод выглядел сломанным. Теперь про прошлое говорим «в ближайшие минуты»,
    а `next_update_at` не отдаём вовсе: иначе клиент нарисовал бы то же прошлое.
    """
    moment = _int(next_at, 0)
    if moment and moment > _int(now, 0):
        return moment, "%s в %s" % (verb, _hhmm(moment))
    return None, "%s в ближайшие минуты" % verb


def _ru(number: int, one: str, few: str, many: str) -> str:
    """«1 ответ», «2 ответа», «5 ответов» — подпись без технических форм."""
    number = abs(int(number))
    tail, hundred = number % 100, number % 10
    if 11 <= tail <= 14:
        word = many
    elif hundred == 1:
        word = one
    elif 2 <= hundred <= 4:
        word = few
    else:
        word = many
    return "%d %s" % (number, word)


def _join_ru(parts: list) -> str:
    """«2 ответа, 1 сообщение и 3 карточки» — список по-русски."""
    parts = [p for p in parts if p]
    if not parts:
        return ""
    if len(parts) == 1:
        return parts[0]
    return ", ".join(parts[:-1]) + " и " + parts[-1]


def _counts_ru(behind: dict, with_cards: bool = True) -> str:
    behind = behind if isinstance(behind, dict) else {}
    parts = []
    if behind.get("answers"):
        parts.append(_ru(behind["answers"], "ответ", "ответа", "ответов"))
    if behind.get("notes"):
        parts.append(_ru(behind["notes"], "сообщение", "сообщения", "сообщений"))
    if behind.get("decisions"):
        parts.append(_ru(behind["decisions"], "решение", "решения", "решений"))
    if with_cards and behind.get("cards"):
        parts.append(_ru(behind["cards"], "карточка", "карточки", "карточек"))
    return _join_ru(parts)


# ------------------------------------------------------------ версия свода ---
def version_of(detail) -> tuple:
    """``(at, version)`` показанной версии свода — из `contract.version_cutoff`."""
    fn = getattr(_contract, "version_cutoff", None)
    if not callable(fn):
        return 0, 0
    try:
        at, version = fn(detail if isinstance(detail, dict) else {})
        return _int(at, 0), _int(version, 0)
    except Exception:  # noqa: BLE001 - подпись версии не должна ронять чтение
        return 0, 0


def cutoff_of(detail) -> tuple:
    """``(учтено_до, version)`` — до какого момента события уже учтены (B-49).

    Не «когда версия вышла», а «когда карточка ушла руководителю» (`posted_at`): всё, что
    Михаил написал, пока карточка работала, в неё не попало и должно остаться в `behind`.
    Нет `posted_at` — время выпуска, как раньше (`contract.accounted_at`).
    """
    at, version = version_of(detail)
    fn = getattr(_contract, "accounted_at", None)
    if callable(fn):
        try:
            seen = _int(fn(detail if isinstance(detail, dict) else {}), 0)
        except Exception:  # noqa: BLE001 - граница не должна ронять чтение
            seen = 0
        if seen:
            return seen, version
    return at, version


def ready(detail) -> bool:
    """Свод есть: состояние `ready` либо у версии есть номер."""
    syn = (detail or {}).get("synthesis") if isinstance(detail, dict) else {}
    syn = syn if isinstance(syn, dict) else {}
    if str(syn.get("state") or "") == "ready":
        return True
    return _int(syn.get("version"), 0) > 0


def history_of(detail) -> list:
    syn = (detail or {}).get("synthesis") if isinstance(detail, dict) else {}
    hist = syn.get("history") if isinstance(syn, dict) else None
    return [h for h in hist if isinstance(h, dict)] if isinstance(hist, list) else []


def app_versions(detail) -> list:
    """Версии свода, которые выпустило ПРИЛОЖЕНИЕ своей карточкой обновления (B-50)."""
    return [entry for entry in history_of(detail)
            if entry.get("auto") or str(entry.get("source") or "") == "app"]


def own_decision_times(detail) -> set:
    """Время версий, выпущенных карточкой обновления приложения (B-50).

    Решения такой версии — её же результат, и время у них — время версии (`at`). На
    следующем тике они выглядели «новым событием»: приложение ставило карточку по
    собственному результату, новая карточка снова перечисляла решение — бесконечный
    цикл с запуском модели раз в ~30 минут. Правило «собственные карточки автоматики
    событием не считаются» распространено и на их решения (ответы/сообщения/шаги и
    завершённые карточки отсекаются отдельно: `_cards_after`, `_no_event_marks`).
    """
    out = set()
    for entry in app_versions(detail):
        at = _int(entry.get("at"), 0)
        if at:
            out.add(at)
    return out


def _thread_of(detail, thread=None) -> list:
    for source in (thread, (detail or {}).get("thread") if isinstance(detail, dict) else None,
                   (detail or {}).get("timeline") if isinstance(detail, dict) else None):
        if isinstance(source, list) and source:
            return source
    return []


def _digest(detail, room, rec, thread, cutoff=None, cfg=None) -> dict:
    """Дайджест «что появилось после версии» — тот же, что у кнопки «Доработать».

    W30-B §2: если у задачи есть подзадачи проекта, в контекст пересборки свода
    добавляется блок «Итоги подзадач» (название, статус, вывод по каждой). Одна
    добавка, без второго дайджеста: агент видит итоги там же, где остальные события.
    """
    empty = {"version": 0, "count": 0, "answers": [], "notes": [], "decisions": [],
             "steps": [], "text": ""}
    fn = getattr(_contract, "revision_digest", None)
    data = dict(empty)
    if callable(fn):
        try:
            data = dict(fn(detail if isinstance(detail, dict) else {}, room, rec or {},
                           thread, cutoff) or empty)
        except Exception:  # noqa: BLE001 - без дайджеста свод всё равно живёт
            data = dict(empty)
    extra = _child_results(cfg, room, rec)
    if extra:
        text = str(data.get("text") or "").strip()
        data["text"] = (text + "\n\n" + extra).strip() if text else extra
    return data


def _child_results(cfg, room, rec) -> str:
    """«Итоги подзадач» для свода родителя (W30-B, ленивый импорт; пусто — нет проекта)."""
    root_id = str((rec or {}).get("root_id") or "")
    if not cfg or not root_id:
        return ""
    try:
        try:
            from . import projects as mod
        except ImportError:                                   # pragma: no cover
            import projects as mod                            # type: ignore
        return mod.child_results_text(cfg, room, root_id) or ""
    except Exception:  # noqa: BLE001 - итоги подзадач не должны ломать свод
        return ""


def _cards_after(detail, rec, cutoff: int) -> list:
    """Завершённые карточки РАБОТЫ после версии (без версий, критика и сообщений)."""
    extra_ids = {str(c.get("id")) for c in ((rec or {}).get("extra_cards") or [])
                 if isinstance(c, dict) and c.get("id")}
    out = []
    for node in (detail or {}).get("tree") or []:
        if not isinstance(node, dict):
            continue
        task_id = str(node.get("id") or "")
        if not task_id:
            continue
        if str(node.get("role") or "") in SERVICE_ROLES:
            continue
        if task_id in extra_ids:
            continue                      # доработка/критик — источник версии, не событие
        if str(node.get("title") or "").strip() == NOTE_TITLE:
            continue                      # карточка сообщения: сообщение считаем в диалоге
        if str(node.get("title") or "").strip().startswith(UPDATE_TITLE_PREFIX):
            # B-50: карточка обновления свода приложения — её завершение и есть версия,
            # а не событие для следующей версии. Заголовок проверяем и здесь: запись
            # реестра могла потеряться (B-49), а роль в дереве — оказаться не `revision`.
            continue
        if str(node.get("status") or "") not in CLOSED_STATUSES:
            continue
        finished = _int(node.get("finished_at"), 0)
        if finished <= cutoff:
            continue
        out.append({"id": task_id, "at": finished, "title": str(node.get("title") or ""),
                    "assignee": str(node.get("assignee") or "")})
    out.sort(key=lambda item: (item["at"], item["id"]))
    return out


def _is_status_action(text) -> bool:
    """Строка «Снять — неактуально»/«Вернуть» у решения (B-47) — не событие свода.

    Такая строка меняет только список решений: её содержимое руководителю ничего
    нового не сообщает (смена статуса и так видна при чтении). Считать её событием
    значило будить агента из-за уборки списка — ровно то, на что жаловался Михаил.
    """
    value = str(text or "")
    if not _answers.decision_kind_of(value):
        return False
    tail = value.split("]", 1)[1].strip().lower() if "]" in value else ""
    return tail.startswith("снять") or tail.startswith("вернуть")


def _no_event_marks(thread) -> set:
    """Время строк ленты, которые событием свода не считаются (флаг `svod_event`)."""
    marks = set()
    for item in thread or []:
        if isinstance(item, dict) and item.get("svod_event") is False:
            at = _int(item.get("at"), 0)
            if at:
                marks.add(at)
    return marks


def events(detail, room=None, rec=None, thread=None, since=0) -> dict:
    """События после версии: ``{behind, items, total, last_at, version, version_at}``.

    ``since`` — «льготный старт»: события раньше этого момента свод не поднимает
    (старые ответы и сообщения, накопившиеся до включения автообновления, не
    должны ставить доработки). По умолчанию 0 — все события (кнопка «Обновить
    сейчас» и ручная доработка видят всё).
    """
    at, version = version_of(detail)
    seen, _cut_version = cutoff_of(detail)
    digest = _digest(detail, room, rec, thread, seen or at)
    answers = [a for a in digest.get("answers") or [] if isinstance(a, dict)]
    no_event = _no_event_marks(_thread_of(detail, thread))
    notes = [n for n in digest.get("notes") or []
             if isinstance(n, dict) and not _is_status_action(n.get("text"))
             and _int(n.get("at"), 0) not in no_event]
    decisions = [d for d in digest.get("decisions") or [] if isinstance(d, dict)]
    own_at = own_decision_times(detail)
    if own_at:
        # B-50: решения, которые перечислила карточка обновления свода самого приложения,
        # — её собственный результат, а не событие. Иначе приложение ставило новую
        # карточку по своему же ответу (бесконечный цикл).
        decisions = [d for d in decisions if _int(d.get("at"), 0) not in own_at]
    cards = _cards_after(detail, rec, seen or at)
    items = []
    for answer in answers:
        items.append({"kind": "answer", "id": str(answer.get("question_id") or ""),
                      "at": _int(answer.get("at"), 0), "label": "ответ на вопрос"})
    for note in notes:
        items.append({"kind": "note", "at": _int(note.get("at"), 0), "label": "сообщение",
                      "id": "n%d" % _int(note.get("at"), 0)})
    for decision in decisions:
        items.append({"kind": "decision", "id": str(decision.get("text") or "")[:80],
                      "at": _int(decision.get("at"), 0), "label": "решение"})
    for card in cards:
        items.append({"kind": "card", "id": card["id"], "at": card["at"],
                      "label": "карточка «%s»" % (card.get("title") or card["id"])[:60]})
    floor = _int(since, 0)
    if floor:
        items = [item for item in items if item["at"] >= floor]
    items.sort(key=lambda item: (item["at"], item["kind"], item["id"]))
    behind = {"answers": 0, "notes": 0, "decisions": 0, "cards": 0}
    for item in items:
        key = {"answer": "answers", "note": "notes", "decision": "decisions",
               "card": "cards"}.get(item["kind"])
        if key:
            behind[key] += 1
    return {"behind": behind, "items": items, "total": sum(behind.values()),
            "last_at": max([item["at"] for item in items], default=0),
            "version": version, "version_at": at}


def event_key(ev: dict) -> str:
    """Отпечаток набора событий: один запуск на один набор (§1.1, идемпотентность)."""
    items = (ev or {}).get("items") or []
    seed = "|".join(sorted("%s:%s:%s" % (i.get("kind"), i.get("id"), i.get("at"))
                           for i in items if isinstance(i, dict)))
    return hashlib.sha1(seed.encode("utf-8")).hexdigest()[:16]


def _accounted(detail, room, rec, thread, cutoff: int) -> dict:
    """Сколько ответов/сообщений/решений уже учтено показанной версией."""
    digest = _digest(detail, room, rec, thread, 0)
    def upto(items) -> int:
        return len([i for i in items or [] if isinstance(i, dict) and _int(i.get("at"), 0) <= cutoff])
    return {"answers": upto(digest.get("answers")), "notes": upto(digest.get("notes")),
            "decisions": upto(digest.get("decisions"))}


# ---------------------------------------------- карточки обновления в работе ---
def _tree_update_cards(detail, nodes=None) -> list:
    """Карточки обновления свода, найденные по ЗАГОЛОВКУ в дереве (B-49).

    Запись реестра может потеряться (файл занят, старая сборка) — тогда карточка, которую
    приложение поставило само, оставалась бы «невидимой»: ни версии, ни состояния
    `updating`, ни «остывания». Заголовок «Обновление свода…» однозначен: так ставит только
    приложение, руководителю такой заголовок не предлагается.
    """
    nodes = nodes if nodes is not None else (detail or {}).get("tree") or []
    out = []
    for node in nodes:
        if not isinstance(node, dict) or not node.get("id"):
            continue
        title = str(node.get("title") or "").strip()
        if not title.startswith(UPDATE_TITLE_PREFIX):
            continue
        out.append({"id": str(node["id"]), "svod": True,
                    "auto": title.endswith("(авто)"), "title": title,
                    "at": _int(node.get("created_at"), 0) or _int(node.get("started_at"), 0)})
    return out


def _cards_known(detail, rec=None) -> list:
    """Все известные карточки обновления: реестр запроса + найденные по заголовку."""
    cards, seen = [], set()
    for source in (((rec or {}).get("extra_cards") or []),
                   ((detail or {}).get("extra_cards") or []),
                   _tree_update_cards(detail)):
        for card in source if isinstance(source, list) else []:
            if not isinstance(card, dict) or not card.get("id"):
                continue
            card_id = str(card["id"])
            if card_id in seen:
                continue
            if not (card.get("svod") or card.get("auto")):
                continue                      # ручная доработка — не «обновление свода»
            seen.add(card_id)
            cards.append(card)
    return cards


def open_update_cards(detail, rec=None, now=None) -> list:
    """Карточки обновления свода, которые ещё идут (состояние `updating`)."""
    now = _now(now)
    nodes = {str(n.get("id")): n for n in (detail or {}).get("tree") or []
             if isinstance(n, dict) and n.get("id")}
    out = []
    for card in _cards_known(detail, rec):
        card_id = str(card.get("id"))
        node = nodes.get(card_id)
        if node is None:
            if now - _int(card.get("at"), 0) <= BUSY_GRACE_S:
                out.append(card)          # создали только что, дерево ещё не перечитали
            continue
        status = str(node.get("status") or "")
        if status in CLOSED_STATUSES:
            continue
        if status in FAILED_CARD_STATUSES or node.get("error"):
            # W21-S2: упавшая попытка не «идёт» — иначе страница вечно показывала бы
            # «Обновляется…». Повтор считает `failed_updates` (остывание).
            continue
        out.append(card)
    return out


def _svod_card_ids(detail, rec=None, now=None) -> set:
    """id карточек обновления свода: свои карточки работу команды не имитируют."""
    return {str(c.get("id")) for c in _cards_known(detail, rec) if c.get("id")}


def card_started_at(node) -> int:
    """Когда карточка начала текущую попытку (``0`` — признаков времени нет).

    В дереве нет сердцебиений: есть только ``started_at`` и время запусков
    (``runs[]``). Этого достаточно, чтобы отличить «карточку только что взяли»
    от «карточка висит с прошлой недели» (см. :data:`BUSY_MAX_S`).
    """
    node = node if isinstance(node, dict) else {}
    stamps = [_int(node.get("started_at"), 0), _int(node.get("finished_at"), 0)]
    for run in node.get("runs") or []:
        if isinstance(run, dict):
            stamps.append(_int(run.get("started_at"), 0))
            stamps.append(_int(run.get("ended_at"), 0))
    return max(stamps + [0])


def _busy_split(detail, rec=None, now=None) -> tuple:
    """``(свежие, зависшие)`` карточки команды — см. :func:`busy_cards`.

    «Зависшие» — карточки, которые держат свод дольше :data:`BUSY_MAX_S`. Они свод
    больше НЕ держат (иначе одна брошенная карточка «в работе» останавливала бы
    обновление навсегда), но их список идёт наверх: причина попадает в `live.text`
    и в заметку карточки обновления.
    """
    now = _now(now)
    skip = _svod_card_ids(detail, rec, now)
    fresh, stalled = [], []
    for node in (detail or {}).get("tree") or []:
        if not isinstance(node, dict) or not node.get("id"):
            continue
        task_id = str(node["id"])
        if task_id in skip:
            continue
        if str(node.get("title") or "").strip() == NOTE_TITLE:
            continue
        if node.get("needs_human"):
            continue                      # «ждёт человека» — не держим свод
        status = str(node.get("status") or "")
        if status not in WAIT_STATUSES:
            continue
        item = {"id": task_id, "title": str(node.get("title") or "")[:80],
                "status": status, "assignee": str(node.get("assignee") or "")}
        started = card_started_at(node)
        if started and now - started >= BUSY_MAX_S:
            item["since"] = started
            stalled.append(item)
        else:
            fresh.append(item)
    fresh.sort(key=lambda item: (item["status"], item["id"]))
    stalled.sort(key=lambda item: (item.get("since") or 0, item["id"]))
    return fresh, stalled


def busy_cards(detail, rec=None, now=None) -> list:
    """Карточки команды, из-за которых свод собирать рано (W21-S2, B-46).

    Пока руководитель или исполнители ведут цепочку (`running|ready|todo|triage|
    review|scheduled`, а также `blocked`, если это ожидание зависимости), свод не
    собирается: он вышел бы устаревшим в ту же минуту. Исключения:

    * сами карточки обновления свода (иначе свод ждал бы сам себя);
    * карточки-сообщения «Уточнение от Михаила» (их содержимое уже в диалоге);
    * карточки, которые ждут человека (`blocked` с вопросом, `needs_human`) —
      иначе свод ждал бы вечно, пока Михаил не ответит;
    * карточки, которые держат свод дольше ``BUSY_MAX_S`` (W21-REV2: брошенная
      карточка «в работе» без сердцебиений не должна замораживать свод навсегда).

    Список зависших карточек — :func:`stalled_cards`.
    """
    return _busy_split(detail, rec, now)[0]


def stalled_cards(detail, rec=None, now=None) -> list:
    """Карточки команды, которые держат свод дольше :data:`BUSY_MAX_S` (W21-REV2)."""
    return _busy_split(detail, rec, now)[1]


def _svod_cards(detail, rec=None, auto_only: bool = False) -> list:
    """Карточки обновления свода, свежие первыми (`auto_only` — без ручных «Обновить сейчас»)."""
    out = _cards_known(detail, rec)
    if auto_only:
        out = [c for c in out if not _is_manual(c)]
    out.sort(key=lambda card: (_int(card.get("at"), 0), str(card["id"])), reverse=True)
    return out


def _is_manual(card) -> bool:
    """Карточку обновления поставил ЧЕЛОВЕК (кнопка «Обновить/Доработать»), а не сторож.

    Ключ ручной карточки — `svod-<rid>-m<версия>-<отпечаток>`, у сторожа — `svod-<rid>-<отпечаток>`;
    заголовок ручной — «Обновление свода» без «(авто)». Неудача ручной попытки не должна
    включать «остывание» автоматических.
    """
    card = card if isinstance(card, dict) else {}
    if card.get("manual") is True:
        return True
    if _MANUAL_KEY.search(str(card.get("event_key") or "")):
        return True
    title = str(card.get("title") or "").strip()
    return bool(title) and not title.endswith("(авто)")


def _card_failed(node) -> bool:
    """Попытка обновления не удалась: карточка упала, зависла или заблокирована."""
    node = node if isinstance(node, dict) else {}
    status = str(node.get("status") or "")
    if status in FAILED_CARD_STATUSES:
        return True
    return bool(node.get("error")) and status not in CLOSED_STATUSES


def card_completed(node) -> bool:
    """Карточка обновления ЗАВЕРШИЛАСЬ успешно (`outcome=completed`) — B-49.

    Смотрим последний запуск: карточка могла упасть и успешно перезапуститься. Ровно это
    (и только это) считается «версия выпущена». Упавшая, отменённая и брошенная карточка
    версией не становится — её разбирает «остывание» (:func:`failed_updates`).
    """
    node = node if isinstance(node, dict) else {}
    if str(node.get("status") or "") not in CLOSED_STATUSES:
        return False
    runs = [r for r in node.get("runs") or [] if isinstance(r, dict)]
    if not runs:
        return not node.get("error")
    last = runs[-1]
    outcome = str(last.get("outcome") or last.get("status") or "").lower()
    if outcome:
        return outcome in ("completed", "done")
    return not (last.get("error") or node.get("error"))


def _same_key(card_key, key: str) -> bool:
    """Ключ набора событий у карточки и у текущего расчёта — это один набор?

    В реестре карточки лежит ключ идемпотентности `svod-<rid>-<отпечаток>`, а `plan` считает
    отпечаток (`event_key`) без префикса: сравниваем и точно, и по хвосту.
    """
    card_key, key = str(card_key or ""), str(key or "")
    if not card_key or not key:
        return False
    return card_key == key or card_key.endswith("-" + key)


def update_tries(detail, rec=None, key: str = "") -> dict:
    """Сколько автоматических карточек обновления уже ВЫПУЩЕНО на этот набор событий (B-49).

    ``{count, cards, key, limit}``. Нужно для защиты от цикла: если на тот же набор событий
    уже была успешная карточка, а `behind` всё равно не ноль, значит версия не зарегистрировалась
    (нет текста свода и т. п.). Повторять нет смысла — набор и руководитель те же: при
    ``count >= LOOP_GUARD_AFTER`` автоматика останавливается, дальше только кнопка «Обновить
    сейчас». ``limit`` — жёсткий потолок карточек на набор (``MAX_AUTO_TRIES_PER_KEY``).

    Считаются: записи реестра с ключом набора ``key`` (ручные не в счёт) и карточки,
    найденные по заголовку «Обновление свода (авто)» без записи в реестре (если они
    завершились после показанной версии).
    """
    key = str(key or "")
    nodes = {str(n.get("id")): n for n in (detail or {}).get("tree") or []
             if isinstance(n, dict) and n.get("id")}
    version_at = version_of(detail)[0]
    ids, count = [], 0
    for card in _svod_cards(detail, rec, auto_only=True):
        card_id = str(card.get("id") or "")
        node = nodes.get(card_id)
        card_key = str(card.get("event_key") or "")
        if card_key and not _same_key(card_key, key):
            continue
        if node is None:
            continue                      # дерево ещё не перечитало карточку
        if not card_completed(node):
            continue                      # неудача — это «остывание», а не выпущенная версия
        if not card_key and _int(node.get("finished_at"), 0) <= version_at:
            continue                      # по заголовку: только попытки ПОСЛЕ показанной версии
        count += 1
        ids.append(card_id)
    return {"count": count, "cards": ids, "key": key, "limit": MAX_AUTO_TRIES_PER_KEY,
            "guard_after": LOOP_GUARD_AFTER}


def failed_updates(detail, rec=None) -> dict:
    """Последняя неудачная попытка обновления и «остывание» (W21-S2, остатки ревью).

    ``{attempts, at, next_at, card_id, event_key, status}`` или ``{}``. ``attempts`` —
    сколько неудач подряд (свежие карточки обновления), поэтому ожидание растёт
    10 → 20 → 40 минут (не больше двух часов). Нет неудач — пусто. Ручные карточки
    («Обновить сейчас») в «остывание» не входят: это действие человека, а не сторожа.
    """
    nodes = {str(n.get("id")): n for n in (detail or {}).get("tree") or []
             if isinstance(n, dict) and n.get("id")}
    attempts, last = 0, None
    for card in _svod_cards(detail, rec, auto_only=True):
        node = nodes.get(str(card["id"]))
        if node is None:
            continue                      # дерево ещё не перечитало карточку
        if not _card_failed(node):
            break                         # дошло до удачной попытки — цепочка кончилась
        attempts += 1
        if last is None:
            last = card
    if not attempts or last is None:
        return {}
    node = nodes.get(str(last.get("id"))) or {}
    runs = [r for r in node.get("runs") or [] if isinstance(r, dict)]
    at = max([_int(node.get("finished_at"), 0), _int(last.get("at"), 0)]
             + [_int(r.get("ended_at"), 0) for r in runs] + [0])
    wait = min(COOLDOWN_BASE_S * (2 ** max(0, attempts - 1)), COOLDOWN_MAX_S)
    return {"attempts": attempts, "at": at, "next_at": at + wait,
            "card_id": str(last.get("id") or ""), "event_key": str(last.get("event_key") or ""),
            "status": str(node.get("status") or "")}


# ------------------------------------------------------------------ тексты ---
def _behind_text(behind: dict) -> str:
    """«Отстаёт: 2 ответа и 1 сообщение не учтены» — с согласованием числа.

    W21-REV2: было «1 сообщение не учтены» (глагол всегда во множественном). Одна
    единица требует единственного числа, причём род зависит от вида события:
    «1 ответ не учтён», «1 сообщение не учтено», «1 решение не учтено».
    """
    counts = _counts_ru(behind)
    if not counts:
        return "Отстаёт"
    behind = behind if isinstance(behind, dict) else {}
    total = sum(_int(value, 0) for value in behind.values())
    if total == 1:
        kind = next((k for k, v in behind.items() if _int(v, 0) == 1), "")
        return "Отстаёт: %s не %s" % (counts, "учтён" if kind == "answers" else "учтено")
    return "Отстаёт: %s не учтены" % counts


def _fresh_text(at, version, counts: dict) -> str:
    head = "Актуально"
    if at:
        head += " на %s" % _hhmm(at)
    head += " · версия %d" % version
    tail = _join_ru([
        _ru(counts.get("answers") or 0, "ответ", "ответа", "ответов") if counts.get("answers") else "",
        _ru(counts.get("notes") or 0, "сообщение", "сообщения", "сообщений") if counts.get("notes") else "",
        _ru(counts.get("decisions") or 0, "решение", "решения", "решений") if counts.get("decisions") else "",
    ])
    if tail:
        head += " · учтено: " + tail
    return head


def last_event_kind(ev: dict) -> str:
    """Вид последнего события набора (``answer``/``note``/``decision``/``card``/'')."""
    items = [i for i in (ev or {}).get("items") or [] if isinstance(i, dict)]
    if not items:
        return ""
    last = max(items, key=lambda item: (_int(item.get("at"), 0), str(item.get("kind") or "")))
    return str(last.get("kind") or "")


def answer_priority(ev: dict) -> bool:
    """Последнее событие — ответ на вопрос руководителя (W21-S2, B-45).

    Тогда ждать полной «тишины» и потолка частоты незачем: человек уже ответил,
    руководителю нужно учесть ответ, а не выдерживать паузу.
    """
    return last_event_kind(ev) == "answer"


def quiet_for(ev: dict, conf: dict) -> int:
    """Сколько «тишины» ждать: 60 с после ответа (B-45), иначе настройка комнаты."""
    quiet = _int((conf or {}).get("quiet_s"), DEFAULT_QUIET_S)
    if answer_priority(ev):
        return min(quiet, ANSWERS_QUIET_S)     # короче настройки, но не длиннее
    return quiet


def update_time(ev: dict, conf: dict, version_at: int) -> int:
    """Когда свод обновится: «тишина» + потолок частоты (``auto_svod_min_gap_s``).

    Потолок частоты не задерживает доработку, вызванную ответом на вопрос (B-45):
    «минимальный интервал» защищал от лавины, а не от ответа человека.
    """
    last = _int((ev or {}).get("last_at"), 0)
    moment = last + quiet_for(ev, conf)
    if not answer_priority(ev):
        moment = max(moment, _int(version_at, 0) + _int((conf or {}).get("min_gap_s"), DEFAULT_MIN_GAP_S))
    return moment


def compute(detail, room=None, rec=None, thread=None, now=None, since=0, cfg=None,
            problem="") -> dict:
    """``synthesis.live`` — состояние живого свода по контракту §1.1.

    ``since`` — «льготный старт»: события раньше него не считаются событиями
    свода (см. :func:`events`), иначе после переноса в боевое дерево блок
    показывал бы «Отстаёт: 12 ответов» по старым запросам.
    ``problem`` — причина, по которой отметка о старте не сохраняется
    (:func:`state_problem`): тогда автообновление невозможно, и это честно
    пишется в `text`, а не молчит.
    """
    now = _now(now)
    conf = settings(room)
    at, version = version_of(detail)
    ev = events(detail, room, rec, thread, since)
    live = {"state": "fresh", "since_version": version, "behind": dict(ev["behind"]),
            "last_event_at": ev["last_at"] or None, "next_update_at": None,
            "auto": bool(conf["auto_svod"]), "text": ""}
    if not version or not ready(detail):
        live.update(state="off", since_version=version,
                    text="Свод ещё не выпущен — появится после первого ответа команды.")
        return live
    if open_update_cards(detail, rec, now):
        live.update(state="updating", text="Обновляется… версия %d" % (version + 1))
        return live
    if ev["total"] <= 0:
        live["text"] = _fresh_text(at, version, _accounted(detail, room, rec, thread, at))
        return live
    if not conf["auto_svod"]:
        live.update(state="stale",
                    text=_behind_text(ev["behind"]) + " — автообновление свода выключено, обновите вручную")
        return live
    lead = str((room or {}).get("orchestrator") or "").strip() if isinstance(room, dict) else ""
    if not lead:
        live.update(state="stale",
                    text=_behind_text(ev["behind"]) + " — в комнате не выбран руководитель")
        return live
    if problem:
        live.update(state="stale",
                    text=_behind_text(ev["behind"]) + " — автообновление свода не может "
                         "сохранить отметку (%s), нажмите «Обновить сейчас»"
                         % PROBLEM_TEXT.get(problem, problem))
        return live
    busy, stalled = _busy_split(detail, rec, now)
    if busy:
        # B-46: цепочка руководителя/исполнителей ещё идёт — свод вышел бы устаревшим
        live.update(state="waiting_cards", text=WAITING_CARDS_TEXT)
        return live
    failure = failed_updates(detail, rec)
    if failure:
        if now < failure["next_at"]:
            live.update(state="pending", next_update_at=failure["next_at"],
                        text=COOLDOWN_TEXT % _hhmm(failure["next_at"]))
            return live
        if failure["attempts"] >= MAX_AUTO_RETRIES:
            live.update(state="stale",
                        text=_behind_text(ev["behind"]) + " — обновление не удалось несколько раз, "
                             "нажмите «Обновить сейчас»")
            return live
    # B-49: защита от цикла. На этот же набор событий уже выпускали карточки, а `behind` не
    # ноль — значит версия не зарегистрировалась (нет текста свода и т. п.). Больше не
    # запускаем автоматически: говорим прямо и оставляем решение человеку.
    tries = update_tries(detail, rec, event_key(ev))
    if tries["count"] >= LOOP_GUARD_AFTER:
        live.update(state="stale", text=LOOP_STALE_TEXT)
        return live
    next_at = update_time(ev, conf, at)
    # ВАЖНО: состояние зависит от «сейчас», а ВЫХОД — нет (ETag не должен дрожать от
    # сдвига часов, W17-B §5 / B11). Время в тексте — из данных: когда свод обновится.
    # W21-REV2: если свод ждал карточек дольше двух часов, причина сказана в тексте —
    # иначе «обновится в 14:20» выглядело бы как обычная «тишина».
    # B-49: обещать прошлое нельзя — см. `_due_text`.
    due_at, due = _due_text(next_at, now)
    if stalled:
        live.update(state="pending", next_update_at=due_at,
                    text=_behind_text(ev["behind"]) + " — %s, %s"
                    % (BUSY_MAX_TEXT, _due_text(next_at, now, "обновлю")[1]))
        return live
    # W28-E: накопление — не будим руководителя на каждое событие. Порог частоты уже
    # прошёл, но событий мало и окно ожидания не истекло — ждём (кнопка работает всегда).
    acc = accumulate(ev, conf, now)
    if not acc["ok"]:
        acc_at, acc_due = _due_text(acc["first_at"] + _int(conf.get("max_wait_s"), DEFAULT_MAX_WAIT_S),
                                    now)
        live.update(state="pending", next_update_at=acc_at,
                    text=_behind_text(ev["behind"]) + " — " + ACCUMULATE_TEXT % acc_due)
        return live
    live.update(state="pending", next_update_at=due_at,
                text=_behind_text(ev["behind"]) + " — %s" % due)
    return live


# -------------------------------------------------------------------- план ---
def plan(detail, room=None, rec=None, thread=None, now=None, since=0) -> dict:
    """Пора ли ставить карточку обновления. Чистая функция (её гоняет сторож).

    ``action``: ``create`` — ставим карточку; ``wait`` — ждём; ``none`` — нечего делать.
    ``since`` — «льготный старт»: события раньше него не поднимают свод.

    ``reason`` объясняет ожидание: ``updating`` (карточка уже идёт), ``waiting_cards``
    (работает команда, B-46), ``cooldown`` (прошлая попытка не удалась), ``quiet``,
    ``min_gap``, ``same_events``, ``hour_limit``.
    """
    now = _now(now)
    rec = rec or {}
    conf = settings(room)
    at, version = version_of(detail)
    ev = events(detail, room, rec, thread, since)
    out = {"action": "none", "reason": "fresh", "events": ev, "settings": conf,
           "next_update_at": None, "key": ""}
    if not version or not ready(detail):
        out["reason"] = "no_svod"
        return out
    if ev["total"] <= 0:
        return out
    if open_update_cards(detail, rec, now):
        out["reason"] = "updating"
        return out
    if not conf["auto_svod"]:
        out["reason"] = "auto_off"
        return out
    lead = str((room or {}).get("orchestrator") or "").strip() if isinstance(room, dict) else ""
    if not lead:
        out["reason"] = "no_leader"
        return out
    if busy_cards(detail, rec, now):
        # B-46: не собираем свод, пока идёт цепочка карточек руководителя/исполнителей
        out.update(action="wait", reason="waiting_cards")
        return out
    stalled = stalled_cards(detail, rec, now)
    if stalled:
        # W21-REV2: карточка держит свод дольше двух часов (брошена/зависла) —
        # обновляемся, но причину несём наверх (live.text и заметка карточки).
        out["stalled"] = [item["id"] for item in stalled]
    failure = failed_updates(detail, rec)
    if failure:
        if now < failure["next_at"]:
            out.update(action="wait", reason="cooldown", next_update_at=failure["next_at"])
            return out
        if failure["attempts"] >= MAX_AUTO_RETRIES:
            out["reason"] = "retry_limit"
            return out
    next_at = update_time(ev, conf, at)
    out["next_update_at"] = next_at
    if now < next_at:
        out.update(action="wait", reason="quiet")
        return out
    # W28-E: накопление (min_events / max_wait_s) — ждём пачки событий, а не будим
    # руководителя на каждое. Ответ на вопрос (B-45) накопления не ждёт: `accumulate`
    # уже учитывает приоритет, а кнопка «Обновить сейчас» идёт другим путём.
    acc = accumulate(ev, conf, now)
    if not acc["ok"]:
        out.update(action="wait", reason="accumulate",
                   next_update_at=acc["first_at"] + _int(conf.get("max_wait_s"), DEFAULT_MAX_WAIT_S),
                   accumulate=acc)
        return out
    # ответ на вопрос (B-45) не ждёт потолка частоты: человек уже ответил
    if not answer_priority(ev):
        launched = _int(rec.get("svod_launched_at"), 0)
        if launched and now - launched < conf["min_gap_s"]:
            out.update(action="wait", reason="min_gap", next_update_at=launched + conf["min_gap_s"])
            return out
    key = event_key(ev)
    # B-49: на этот набор событий уже выпускали карточки, а события всё ещё не учтены —
    # третьей автоматической попытки не будет (иначе запуски агента уходят впустую).
    if update_tries(detail, rec, key)["count"] >= LOOP_GUARD_AFTER:
        out.update(action="none", reason="loop_guard", key=key)
        return out
    retry = bool(failure) and (failure.get("event_key") or "") in ("", key)
    if rec.get("svod_event_key") == key and not retry:
        out.update(action="none", reason="same_events", key=key)
        return out
    out.update(action="create", reason="ready", key=key)
    return out


# ------------------------------------------------------------ карточка-версия ---
def auto_note(ev: dict, stalled=None) -> str:
    """Заметка карточки обновления: что именно изменилось (её читает руководитель).

    ``stalled`` — карточки команды, которые держали свод дольше ``BUSY_MAX_S``
    (W21-REV2). Тогда в заметке честно сказано, что свод собран, не дожидаясь их:
    иначе руководитель не поймёт, почему версия появилась посреди «работы».
    """
    items = [i for i in (ev or {}).get("items") or [] if isinstance(i, dict)]
    counts = _counts_ru((ev or {}).get("behind") or {})
    note = AUTO_NOTE_PREFIX + ("учтены события после версии — %s." % counts if counts else "обнови свод.")
    if stalled:
        titles = [str(c.get("title") or c.get("id") or "")[:60] for c in stalled if isinstance(c, dict)]
        note += " Собрано, не дожидаясь карточек (%s): %s." % (
            BUSY_MAX_TEXT, "; ".join(t for t in titles if t))
    labels = []
    for item in items[:12]:
        label = str(item.get("label") or item.get("kind") or "")
        if label and label not in labels:
            labels.append(label)
    if labels:
        note += " Что учесть: " + "; ".join(labels[:8]) + "."
    return note


def _previous_answer(detail, rec) -> str:
    """Текст предыдущей версии: последняя завершённая доработка, иначе ответ корня."""
    nodes = {str(n.get("id")): n for n in (detail or {}).get("tree") or []
             if isinstance(n, dict) and n.get("id")}
    best = None
    for card in (rec or {}).get("extra_cards") or []:
        if not isinstance(card, dict) or not card.get("id"):
            continue
        if str(card.get("kind") or "") != "revision":
            continue
        node = nodes.get(str(card["id"]))
        if not node or str(node.get("status") or "") not in CLOSED_STATUSES:
            continue
        text = str(node.get("summary_md") or "").strip()
        if not text:
            continue
        key = (_int(card.get("at"), 0), str(card["id"]))
        if best is None or key > best[0]:
            best = (key, text)
    if best:
        return best[1]
    syn = (detail or {}).get("synthesis") if isinstance(detail, dict) else {}
    return str((syn or {}).get("text_md") or "")


def _previous_card(detail, rec) -> str:
    """id карточки прошлой версии свода — для ссылки из выжимки (W25-M2).

    Тот же выбор, что у `_previous_answer`: последняя завершённая доработка, иначе
    карточка ответа корня. Пустая строка — ссылки нет, текст скажет про дерево.
    """
    nodes = {str(n.get("id")): n for n in (detail or {}).get("tree") or []
             if isinstance(n, dict) and n.get("id")}
    best = None
    for card in (rec or {}).get("extra_cards") or []:
        if not isinstance(card, dict) or not card.get("id"):
            continue
        if str(card.get("kind") or "") != "revision":
            continue
        node = nodes.get(str(card["id"]))
        if not node or str(node.get("status") or "") not in CLOSED_STATUSES:
            continue
        if not str(node.get("summary_md") or "").strip():
            continue
        key = (_int(card.get("at"), 0), str(card["id"]))
        if best is None or key > best[0]:
            best = (key, str(card["id"]))
    if best:
        return best[1]
    return str(((detail or {}).get("synthesis") or {}).get("source_task") or "")


def _registry():
    try:
        from . import registry as reg          # type: ignore
    except ImportError:                        # pragma: no cover - плоский импорт
        import registry as reg                 # type: ignore
    return reg


def _register(cfg, room, rec, card_id: str, ev: dict, note: str, key: str, now: int,
              manual: bool = False) -> bool:
    """Записать карточку обновления в реестр запроса с пометками «своя» и «свод»."""
    room_id = str((room or {}).get("id") or "")
    root_id = str((rec or {}).get("root_id") or "")
    if not room_id or not root_id:
        return False
    behind = (ev or {}).get("behind") or {}
    entry = {"id": card_id, "kind": "revision",
             "round": max(1, len([c for c in (rec or {}).get("extra_cards") or []
                                  if isinstance(c, dict) and c.get("kind") == "revision"]) + 1),
             "at": now, "posted_at": now, "note": " ".join(str(note or "").split())[:400],
             "reason": "auto", "answers_count": _int(behind.get("answers"), 0) + _int(behind.get("notes"), 0),
             "changes": [], "auto": True, "svod": True, "event_key": key, "manual": bool(manual),
             "events": {k: _int(v, 0) for k, v in behind.items()}}
    try:
        _registry().add_extra_card(cfg, room_id, root_id, entry)
        return True
    except Exception:  # noqa: BLE001 - карточка уже создана, реестр догонит стороной
        return False


def create_update_card(cfg, room, rec, detail, ev: dict, note: str = "", thread=None,
                       manual: bool = False, now=None) -> str:
    """Поставить карточку обновления свода (тот же путь, что кнопка «Доработать»)."""
    now = _now(now)
    room = room if isinstance(room, dict) else {}
    rec = rec if isinstance(rec, dict) else {}
    root_id = str(rec.get("root_id") or (detail or {}).get("id") or "")
    board = str(room.get("board") or "")
    lead = str(room.get("orchestrator") or "").strip()
    if not root_id or not board:
        raise SvodError("bad_request", "Не хватает запроса или доски комнаты для обновления свода.")
    if not lead:
        raise SvodError("conflict", "В комнате не выбран руководитель — обновить свод некому.",
                        {"reason": "no_leader"})
    if not ready(detail):
        raise SvodError("conflict", "Свода ещё нет — обновлять нечего.", {"reason": "no_svod"})
    digest = _digest(detail, room, rec, thread, cfg=cfg)   # W30-B: + «Итоги подзадач»
    request_text = str(rec.get("text") or (detail or {}).get("text") or "")
    answer = _previous_answer(detail, rec)
    note = note or auto_note(ev)
    # W25-M2: прошлый свод идёт выжимкой + ссылкой на карточку версии (полный текст — там).
    body = _contract.build_revision_body(room, request_text, answer, [], note, cfg, digest=digest,
                                         prev_card=_previous_card(detail, rec))
    # B-49b: карточку обновления ставим РЕБЁНКОМ КОРНЯ, а не последней карточки свода.
    # Раньше родителем был `synthesis.source_task` (предыдущая карточка обновления), из-за
    # чего цепочка «корень → … → обновление → обновление → …» уходила в глубину, и карточки
    # глубже MAX_DEPTH выпадали из дерева запроса — вместе с версиями свода, `delivery`,
    # лимитом 6 карточек в час и защитой от цикла. Порядок «одна за другой» даёт не связь,
    # а `open_update_cards` (новую карточку не ставим, пока идёт предыдущая), поэтому
    # цепочка не нужна; идемпотентность по-прежнему держит ключ `svod-<rid>-…`.
    parent = root_id
    title = "Обновление свода" if manual else "Обновление свода (авто)"
    version = _int((ev or {}).get("version"), 0)
    seed = event_key(ev) or "none"
    key = "%s%s-%s%s" % (SVOD_KEY_PREFIX, root_id, "m%d-" % version if manual else "", seed)
    card_id = _engine.create_child_card(cfg, room, board, parent, lead, title, body,
                                        "revision", idem_key=key, routine=True)
    _register(cfg, room, rec, card_id, ev, note, key, now, manual=manual)
    return card_id


def _remember(cfg, room, rec, ev: dict, key: str, now: int) -> None:
    """Запомнить набор событий: повторный проход по тем же событиям карточку не плодит."""
    room_id = str((room or {}).get("id") or "")
    root_id = str((rec or {}).get("root_id") or "")
    if not room_id or not root_id:
        return
    patch = {"svod_event_key": key or event_key(ev), "svod_launched_at": now,
             "svod_version": _int((ev or {}).get("version"), 0)}
    try:
        _registry().update(cfg, room_id, root_id, patch)
    except Exception:  # noqa: BLE001 - ключ вторичен: карточка уже стоит
        pass


def _accepted(cfg, room, rec, detail) -> bool:
    """Принята ли задача (W30-D1): принятая заморожена, как архивная.

    Принимаем и флаг детали (его ставит страница запроса), и файл отметки —
    свод зовут и из сторожа, где деталь может быть собрана другим путём.
    """
    if isinstance(detail, dict) and detail.get("accepted"):
        return True
    root_id = str((rec or {}).get("root_id") or (detail or {}).get("id") or "")
    if not root_id:
        return False
    try:
        try:
            from . import passports as _passports
        except ImportError:  # pragma: no cover - плоский импорт
            import passports as _passports   # type: ignore
        return bool(_passports.is_accepted(cfg, room, root_id))
    except Exception:  # noqa: BLE001 - отметка вторична для свода
        return False


def _board_blocked(cfg, room) -> str:
    """W29-I1: причина «писать в доску нельзя» (песочница + реальный дом Hermes)."""
    try:
        try:
            from . import board_guard as guard
        except ImportError:                                   # pragma: no cover
            import board_guard as guard                       # type: ignore
        found = guard.check(cfg, str((room or {}).get("board") or getattr(cfg, "board", "") or ""))
    except Exception:                                         # noqa: BLE001 - защита вторична
        return ""
    return str((found or {}).get("message") or "")


def tick(cfg, room, rec, detail, thread=None, now=None, since=None, log=None) -> dict:
    """Шаг сторожа: если пора — поставить карточку обновления свода.

    Возвращает решение `plan()` (``action``: create/wait/none) либо ``created`` с
    ``card_id``. Ошибки движка летят наверх — их считает сторож, тик не рушится.

    Два предохранителя (без них первый же тик после обновления поставил бы по
    карточке на каждый старый запрос):

    * «льготный старт» — события раньше момента включения автообновления не
      считаются (`reason="grace_start"`), ``since`` можно задать явно;
    * лимит `MAX_CARDS_PER_HOUR` карточек в час на комнату
      (`reason="hour_limit"`).

    ``log`` — журнал сторожа: если отметку о старте сохранить нельзя, это пишется
    туда (не чаще раза в час на комнату), а не пропадает молча.
    """
    now = _now(now)
    # W29-P4: архивный запрос — свод не обновляем (никаких карточек и моделей).
    # W30-D1: принятая задача заморожена — то же самое, ни одной карточки свода.
    if (isinstance(room, dict) and room.get("archived")) or \
            (isinstance(detail, dict) and detail.get("archived")):
        return {"action": "none", "reason": "archived"}
    if _accepted(cfg, room, rec, detail):
        return {"action": "none", "reason": "accepted"}
    # W29-I1: песочница с реальной доской — свод не ставит карточек.
    blocked = _board_blocked(cfg, room)
    if blocked:
        return {"action": "none", "reason": "sandbox_real_board", "text": blocked}
    floor = _int(since, -1)
    if floor < 0:
        floor = start_at(cfg, now)
    log_problem(cfg, room, log, now)
    decision = plan(detail, room, rec, thread, now, floor)
    if decision.get("action") != "create":
        return decision
    ev = decision["events"]
    if hour_limit_reached(cfg, room, now):
        decision.update(action="wait", reason="hour_limit")
        return decision
    note = auto_note(ev, stalled_cards(detail, rec, now))
    card_id = create_update_card(cfg, room, rec, detail, ev, note=note, thread=thread, now=now)
    _remember(cfg, room, rec, ev, decision.get("key") or "", now)
    _note_launch(cfg, room, now)
    decision.update(action="created", card_id=card_id)
    return decision


def refresh_now(cfg, room, rec, detail, note: str = "", thread=None, now=None) -> dict:
    """Действие ``svod_refresh`` — «Обновить сейчас» (вручную, без ожидания тишины)."""
    now = _now(now)
    if open_update_cards(detail, rec, now):
        raise SvodError("conflict", "Обновление свода уже идёт — дождитесь новой версии.",
                        {"reason": "updating"})
    blocked = _board_blocked(cfg, room)          # W29-I1: песочница не пишет в доску
    if blocked:
        raise SvodError("sandbox_real_board", blocked, {"reason": "sandbox_real_board"})
    ev = events(detail, room, rec, thread)
    card_id = create_update_card(cfg, room, rec, detail, ev, note=note, thread=thread,
                                 manual=True, now=now)
    _remember(cfg, room, rec, ev, "manual:%s" % (note or ev["version"]), now)
    return {"ok": True, "card_id": card_id, "behind": ev["behind"]}


# ---------------------------------------------------------------- доставка ---
def _entry_for(history: list, at: int) -> dict:
    """Версия, которая учла сообщение в момент ``at`` (первая версия после него)."""
    chosen = {}
    for entry in history:
        if _int(entry.get("at"), 0) >= at:
            chosen = entry
            break
    return chosen


def delivery_for(item: dict, live: dict, detail: dict, rec=None, now=None, since=0) -> dict:
    """``delivery`` сообщения Михаила в диалоге: ждёт / в работе / учтено / не учтено."""
    at = _int((item or {}).get("at"), 0)
    live = live if isinstance(live, dict) else {}
    state = str(live.get("state") or "")
    version = _int(live.get("since_version"), 0)
    out = {"state": "waiting", "version": None, "cards": [], "reason": "", "text": ""}
    if state == "off":
        out.update(reason="no_svod", text="Ждёт выпуска свода")
        return out
    history = history_of(detail)
    cutoff = _int(history[-1].get("at"), 0) if history else 0
    if at and cutoff and at <= cutoff:
        entry = _entry_for(history, at)
        number = _int(entry.get("version"), 0) or version
        cards = [str(entry.get("task_id") or "")] if entry.get("task_id") else []
        if entry.get("auto") and not (entry.get("changes") or []):
            out.update(state="applied_nochange", version=number, cards=cards,
                       text=NO_CHANGE_TEXT)
        else:
            out.update(state="applied", version=number, cards=cards,
                       text="Учтено в версии %d" % number if number else "Учтено в своде")
        return out
    if _int(since, 0) and at and at < _int(since, 0):
        # написано до включения автообновления: свод такие сообщения не поднимает,
        # но и «ждёт» писать нельзя — оно не обновится само
        out.update(state="declined", reason="grace_start",
                   text="Не учтено — написано до включения автообновления; "
                        "«Обновить сейчас» учтёт вручную")
        return out
    busy = open_update_cards(detail, rec, now)
    if busy:
        out.update(state="working", cards=[str(c.get("id")) for c in busy],
                   text="В работе: " + _ru(len(busy), "карточка", "карточки", "карточек"))
        return out
    if state == "stale":
        live_text = str(live.get("text") or "")
        reason = "auto_off" if live.get("auto") is False else "no_leader"
        tail = "автообновление свода выключено" if reason == "auto_off" else "нет руководителя комнаты"
        out.update(state="declined", reason=reason,
                   text="Не учтено — %s, нажмите «Обновить сейчас»" % tail
                   if reason == "auto_off" else "Не учтено — %s" % tail)
        if live_text:
            out["reason_text"] = live_text
        return out
    if state == "waiting_cards":
        # B-46: свод ждёт, пока команда закончит цепочку карточек
        out.update(reason="waiting_cards", text="Ждёт завершения работы команды")
        return out
    next_at = _int(live.get("next_update_at"), 0)
    # B-49: прошедшее время не обещаем — если расчётное уже позади, говорим «в ближайшие минуты»
    hint = " — в %s" % _hhmm(next_at) if next_at else " — в ближайшие минуты"
    out.update(text="Ждёт обновления свода" + hint)
    return out


def _no_change_delivery() -> dict:
    """``delivery`` строки, которая не является событием свода (B-47)."""
    return {"state": "applied_nochange", "version": None, "cards": [],
            "reason": "decision_status", "text": NO_CHANGE_TEXT}


def apply_delivery(detail, live: dict, rec=None, thread=None, now=None, since=0) -> None:
    """Проставить ``delivery`` всем сообщениям Михаила в ленте запроса.

    Строки о снятии/возврате решения (B-47) получают «Учтено, менять нечего»:
    свод их не поднимает, ждать от них нечего.
    """
    items = _thread_of(detail, thread)
    for item in items:
        if not isinstance(item, dict):
            continue
        if str(item.get("kind") or "") != "message":
            continue
        author = str(item.get("author") or item.get("author_kind") or "")
        if author not in ("mikhail", "Михаил", "human"):
            continue
        text = item.get("text_mark") or item.get("text")
        if item.get("svod_event") is False or _is_status_action(text):
            item["delivery"] = _no_change_delivery()
            continue
        item["delivery"] = delivery_for(item, live, detail, rec, now, since)


def attach(detail, room=None, rec=None, thread=None, now=None, cfg=None, since=None) -> dict:
    """Дополнить деталь запроса: ``synthesis.live`` и подписи доставки сообщений.

    ``cfg`` нужен, чтобы прочитать «льготный старт» (когда включилось
    автообновление): события до него свод не поднимает. Без ``cfg`` порог нулевой —
    поведение прежнее (моки, тесты).
    """
    if not isinstance(detail, dict):
        return {}
    floor = _int(since, -1)
    problem = _problem_of(cfg, None) if cfg is not None else ""
    info = {}
    if floor < 0:
        if cfg is not None:
            info = _load_start(cfg, now)
            floor = _int(info.get("started_at"), 0)
            problem = problem or _problem_of(cfg, info)
        else:
            floor = 0
    live = compute(detail, room, rec, thread, now, floor, cfg=cfg, problem=problem)
    syn = detail.get("synthesis")
    if isinstance(syn, dict):
        syn["live"] = live
    apply_delivery(detail, live, rec, thread, now, floor)
    return live
