"""Общие константы и мелкие помощники HTTP-слоя «Комнаты команды».

Живут отдельно от ``app.py``, чтобы ими пользовались и миксины хендлеров
(``routes_read``/``routes_write``), и сам ``App`` — без циклических импортов.
"""
from __future__ import annotations

import base64
import re
import secrets
from datetime import datetime

try:  # пакетный и «плоский» импорт
    from .http_util import err
except ImportError:  # pragma: no cover
    from http_util import err  # type: ignore

APP_NAME = "team-room"
#: Предел текста запроса. Мягкая страховка: текст уходит в карточку через
#: `--body-file`, а не через командную строку Windows, поэтому 1 млн знаков
#: (≈2 МБ кириллицы) — это «диктовка целиком», а не реальный предел движка.
MAX_TEXT = 1_000_000
MAX_ATTACH = 10
#: Предел текста заметки: комментарий, ответ на вопрос, доработка.
MAX_NOTE = 200_000
ROOM_STATUSES = ("queued", "working", "reviewing", "synthesizing", "done", "needs_you", "failed")
BACKLOG_STATUSES = ("open", "asked", "done", "dismissed")
BACKLOG_KINDS = ("next_step", "future_question", "open_question", "decision")
PATCH_FIELDS = {"title", "icon", "orchestrator", "critic", "context_files", "project_dir",
                "mode_default", "critic_pass", "max_rounds",
                "launch_mode", "max_parallel_streams", "project_mode", "agent_notes",
                "agent_models",
                # W17-R §1 (B-8): этапы проекта — свойство комнаты, правятся в её настройках.
                "phases"}
ACTION_ALLOWED = ("dispatch", "comment", "answer", "unblock", "reassign", "rework", "retry",
                  "archive", "clone", "decompose", "critique", "revise",
                  # W14-D §2/§4: «Напомнить руководителю» и «Убрать» шаг
                  # W14-H §12: действия по шагу — согласен / прокомментировать / переместить
                  # W17-B §3: «Отправить руководителю накопленные ответы» (только по клику)
                  # W19-D2 §1 (B-20): действие над решением «предложено»
                  "answer_remind", "step_drop", "step_agree", "step_comment", "step_move",
                  "answers_resend", "decision")
ACTION_REQUIRED = {"answer": ("question_id",), "reassign": ("task_id", "profile"),
                   "answer_remind": ("question_id",), "step_drop": ("step_id",),
                   "step_agree": ("step_id",), "step_comment": ("step_id",),
                   "step_move": ("step_id",), "decision": ("decision_id",)}
ACTION_TEXT = {"comment", "answer", "rework", "revise", "step_comment"}
# D1: строгие формы значений, которые уходят в argv CLI (см. docs/critic/wave4-backend.md §2.1)
TASK_ID_RE = re.compile(r"^t_[0-9a-f]{8}$")
PROFILE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.@+-]{0,63}$")
REPORT_KEY_RE = re.compile(r"^[A-Za-z0-9._-]{1,80}$")
AUTHOR_MAXIM = "mikhail"
UNASSIGN_VALUES = ("none", "unassign")
# D2: проход диспетчера
DISPATCH_MAX_DEFAULT = 3
DISPATCH_MAX_LIMIT = 5
DISPATCH_COOLDOWN_S = 30
_MD_PREFIX = re.compile(r"^\s{0,3}(?:#{1,6}\s*|[-*+]\s+|>\s*|\d+[.)]\s*)")


def has_nul(value) -> bool:
    """Есть ли NUL-байт в строке/ключе (вложенность — до 40 уровней)."""
    return _has_nul(value)


def _has_nul(value, depth: int = 0) -> bool:
    if depth > 40:
        return False
    if isinstance(value, str):
        return "\x00" in value
    if isinstance(value, dict):
        return any(_has_nul(key, depth + 1) or _has_nul(item, depth + 1)
                   for key, item in value.items())
    if isinstance(value, (list, tuple, set)):
        return any(_has_nul(item, depth + 1) for item in value)
    return False


def task_id_ok(value) -> bool:
    """`^t_[0-9a-f]{8}$` — единственная допустимая форма id карточки."""
    return isinstance(value, str) and bool(TASK_ID_RE.fullmatch(value))


def profile_ok(value) -> bool:
    """Имя профиля без ведущего дефиса и служебных символов."""
    return isinstance(value, str) and bool(PROFILE_RE.fullmatch(value))


# --- мелкие помощники ------------------------------------------------------
def make_slug(moment: datetime | None = None) -> str:
    """Слаг запроса: ``r-YYYYMMDD-HHMM-xxxx`` (xxxx — 4 hex)."""
    stamp = (moment or datetime.now()).strftime("%Y%m%d-%H%M")
    return "r-%s-%s" % (stamp, secrets.token_hex(2))


def ru_num(value: int) -> str:
    """Число с пробелами между разрядами: 1000000 → «1 000 000» (для текстов ошибок)."""
    value = int(value)
    sign = "-" if value < 0 else ""
    digits = str(abs(value))
    parts = []
    while digits:
        parts.append(digits[-3:])
        digits = digits[:-3]
    return sign + " ".join(reversed(parts))


def text_too_long(kind: str, length: int, limit: int) -> str:
    """Понятное русское сообщение о превышении предела текста (без техн. слов)."""
    return ("%s слишком длинный: %s знаков, а можно не больше %s. "
            "Сократите текст." % (kind, ru_num(length), ru_num(limit)))


def derive_title(text: str, limit: int = 80) -> str:
    """Заголовок из первой непустой строки текста, без markdown."""
    for line in (text or "").splitlines():
        clean = _MD_PREFIX.sub("", line).strip().strip("*_`").strip()
        clean = re.sub(r"\s+", " ", clean)
        if clean:
            return clean[:limit]
    return "Запрос"


def preview(text: str, limit: int = 200) -> str:
    clean = re.sub(r"[#*_`>\[\]()!]", " ", text or "")
    return re.sub(r"\s+", " ", clean).strip()[:limit]


def encode_cursor(offset: int) -> str:
    return base64.urlsafe_b64encode(str(int(offset)).encode("ascii")).decode("ascii").rstrip("=")


def decode_cursor(raw: str) -> int:
    try:
        padded = raw + "=" * (-len(raw) % 4)
        return max(0, int(base64.urlsafe_b64decode(padded.encode("ascii")).decode("ascii")))
    except Exception:  # noqa: BLE001 - любой мусор = неверный курсор
        raise err(400, "Некорректный cursor", field="cursor") from None


def try_call(func, *attempts):
    """Вызвать func по первой подходящей форме вызова (TypeError → следующая)."""
    last: Exception | None = None
    for args, kwargs in attempts:
        try:
            return func(*args, **kwargs)
        except TypeError as exc:      # несовпадение сигнатуры — пробуем следующую форму
            last = exc
    raise last or TypeError("нет подходящей формы вызова")


def _backlog_counts(items) -> dict:
    counts = {"open": 0, "asked": 0, "done": 0, "dismissed": 0, "total": 0}
    for item in items or []:
        status = (item or {}).get("status")
        if status in counts:
            counts[status] += 1
        counts["total"] += 1
    return counts


__all__ = ["APP_NAME", "MAX_TEXT", "MAX_ATTACH", "MAX_NOTE", "ROOM_STATUSES", "BACKLOG_STATUSES",
           "BACKLOG_KINDS", "PATCH_FIELDS", "ACTION_ALLOWED", "ACTION_REQUIRED", "ACTION_TEXT",
           "TASK_ID_RE", "PROFILE_RE", "REPORT_KEY_RE", "AUTHOR_MAXIM", "UNASSIGN_VALUES",
           "DISPATCH_MAX_DEFAULT", "DISPATCH_MAX_LIMIT", "DISPATCH_COOLDOWN_S",
           "has_nul", "task_id_ok", "profile_ok",
           "make_slug", "derive_title", "preview", "encode_cursor", "decode_cursor",
           "try_call", "_backlog_counts", "ru_num", "text_too_long", "err"]
