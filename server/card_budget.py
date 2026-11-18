"""Бюджеты тела карточки (WAVE25 §M2, основание — R4 §4.3) и подгонка под них.

Зачем отдельный модуль: цифры бюджета нужны в трёх местах сразу — при сборке
карточки (`contract`, `svod_live`), в проверке (`tests/test_card_budget.py`,
`tools/check_card_budgets.py`) и в отчёте сторожа. Пока цифры живут в одном
месте, «бюджет» — это правило, а не пожелание.

Правило ступеней «пакет → ссылки → поиск» (R4 §4.4): в теле карточки остаётся
только то, без чего работа начнётся неправильно (пакет); всё остальное — путём
и номером карточки (ссылки). Поэтому прошлый свод в доработке заменяется
выжимкой с указателем на карточку версии, а не полным текстом.

Что здесь есть:

* ``BUDGETS`` — потолки тела по типу карточки (знаки);
* ``CEILINGS`` — потолки частей (дайджест, выжимка прошлой версии, контракт);
* ``kind_of`` — тип карточки по её роли (slug) и названию — одна функция, чтобы
  тесты и живые замеры считали одинаково;
* ``summarize`` — детерминированная выжимка Markdown (без модели): заголовки и
  первые строки разделов, всегда ≤ потолка;
* ``allowance`` — сколько знаков можно отдать дайджесту и выжимке при известной
  «жёсткой» части тела;
* ``audit`` — сводка замеров (сколько карточек, максимум, сколько сверх бюджета).

Модуль ничего не читает и не пишет: только цифры и чистые функции.
"""
from __future__ import annotations

import re

#: Потолки тела карточки по типу (знаки). R4 §4.3: «тело карточки не должно
#: превышать 8 000 знаков ни в одном типе».
BUDGETS = {
    "leader": 6000,      # руководитель, корневой запрос
    "worker": 2500,      # исполнитель (подзадача направления)
    "critic": 8000,      # критик (версия свода + критерии)
    "svod": 8000,        # обновление свода (доработка)
    "service": 2000,     # приёмщик почты и прочие служебные карточки
}

KIND_RU = {"leader": "руководитель", "worker": "исполнитель", "critic": "критик",
           "svod": "обновление свода", "service": "служебная"}

#: Потолки частей тела (знаки).
DIGEST_MAX = 4000        # «что появилось после версии»
PREV_SUMMARY_MAX = 3000  # выжимка прошлой версии свода
CONTRACT_FULL_MAX = 4600  # полный контракт (корневая карточка)
CONTRACT_CORE_MAX = 1400  # ядро контракта (доработка, свод)
#: Меньше этого резать бессмысленно: выжимка без смысла хуже, чем «см. карточку».
MIN_PREV = 400
MIN_DIGEST = 600

#: slug карточки → тип. Один источник соответствия для сборки и замеров.
SLUG_KIND = {"request": "leader", "brief": "leader", "map": "leader",
             "revision": "svod", "critic": "critic", "stream": "worker",
             "worker": "worker", "intake": "service"}

_URL_RE = re.compile(r"^(?:[-*+]\s|\d+[.)]\s|#{1,6}\s|>|\|)")


def kind_of(slug="", title="") -> str:
    """Тип карточки: по роли, а если роли нет — по названию (живые замеры).

    Названия — те, что ставит приложение (`svod_live.UPDATE_TITLE`,
    `thread.NOTE_TITLE`); чужое имя не угадываем и считаем «служебным», чтобы
    замер не выдавал чужую карточку за карточку руководителя.
    """
    name = str(slug or "").strip().lower()
    if name in BUDGETS:            # уже тип, а не slug (замеры и отчёты)
        return name
    kind = SLUG_KIND.get(name)
    if kind:
        return kind
    name = str(title or "").strip().lower()
    if not name:
        return "service"
    if name.startswith("обновление свода") or name.startswith("доработка ответа"):
        return "svod"
    if "критик" in name:
        return "critic"
    if name.startswith(("уточнение от", "сообщение максиму", "контекст запроса")):
        return "leader"
    if name.startswith(("направление", "задача", "подзадача")):
        return "worker"
    return "service"


def limit(kind: str) -> int:
    """Потолок тела карточки этого типа (знаки). Принимает и тип, и slug роли."""
    name = str(kind or "").strip().lower()
    name = SLUG_KIND.get(name, name)
    return int(BUDGETS.get(name, BUDGETS["service"]))


def size(body) -> int:
    """Размер тела в знаках (не байтах: кириллица в UTF-8 — 2 байта на знак)."""
    return len(str(body or ""))


def over(kind: str, body) -> int:
    """Насколько тело больше потолка (0 — в бюджете)."""
    return max(0, size(body) - limit(kind))


def summarize(md, max_chars: int = PREV_SUMMARY_MAX, note: str = "") -> str:
    """Детерминированная выжимка Markdown: заголовки + начала разделов.

    Не «сжатие моделью»: берём структуру (заголовки, первый абзац и первый
    пункт каждого раздела), поэтому выжимка не врёт и не пересказывает. Если
    текст и так короче потолка — возвращаем как есть, ни знака не меняя.
    """
    text = str(md or "").strip()
    limit_chars = max(0, int(max_chars or 0))
    if not text or limit_chars <= 0:
        return ""
    if len(text) <= limit_chars:
        return text
    tail = ("\n… " + note) if note else "\n… (выжимка)"
    room = max(0, limit_chars - len(tail))
    lines: list[str] = []
    budget_after_head = 0
    for raw in text.splitlines():
        line = raw.rstrip()
        body = line.strip()
        if not body:
            continue
        if _URL_RE.match(body) and not body.startswith("#"):
            if budget_after_head > 0:
                lines.append(line)
                budget_after_head -= 1
            continue
        lines.append(line)
        budget_after_head = 1 if not body.startswith("#") else 2
    picked = "\n".join(lines) or text
    if len(picked) > room:
        cut = picked[:room]
        # режем по границе строки, а не посреди слова
        if "\n" in cut:
            cut = cut.rsplit("\n", 1)[0]
        picked = cut
    return (picked.rstrip() + tail)[:limit_chars]


def allowance(kind: str, fixed_chars: int, *, digest_max: int = DIGEST_MAX,
              prev_max: int = PREV_SUMMARY_MAX) -> dict:
    """Сколько знаков остаётся дайджесту и выжимке прошлой версии.

    Приоритет — дайджесту («что появилось» — это и есть работа доработки):
    сначала ужимается выжимка прошлой версии до ``MIN_PREV``, потом дайджест.
    Возвращает ``{"digest": int, "prev": int, "spare": int}``.
    """
    spare = max(0, limit(kind) - max(0, int(fixed_chars or 0)))
    prev = min(prev_max, max(MIN_PREV, spare - digest_max))
    if prev > spare:
        prev = spare
    digest = min(digest_max, max(0, spare - prev))
    if digest < MIN_DIGEST and spare > prev + MIN_DIGEST:
        digest = MIN_DIGEST
    return {"digest": int(digest), "prev": int(prev), "spare": int(spare)}


def audit(rows) -> dict:
    """Сводка замеров: по типам — сколько карточек, максимум, сколько сверх.

    ``rows`` — список ``{kind, chars, id?}``. Считаем только то, что дали: никаких
    собственных чтений доски, чтобы замер можно было повторить на фикстуре.
    """
    out: dict = {"total": 0, "over": 0, "kinds": {}}
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        kind = kind_of(row.get("kind") or row.get("slug"), row.get("title") or "")
        chars = int(row.get("chars") or 0)
        item = out["kinds"].setdefault(kind, {"count": 0, "max": 0, "sum": 0,
                                              "over": 0, "limit": limit(kind)})
        item["count"] += 1
        item["sum"] += chars
        item["max"] = max(item["max"], chars)
        if chars > item["limit"]:
            item["over"] += 1
            out["over"] += 1
        out["total"] += 1
    for item in out["kinds"].values():
        item["avg"] = int(round(item["sum"] / item["count"])) if item["count"] else 0
    return out


__all__ = ["BUDGETS", "KIND_RU", "DIGEST_MAX", "PREV_SUMMARY_MAX", "CONTRACT_FULL_MAX",
           "CONTRACT_CORE_MAX", "MIN_PREV", "MIN_DIGEST", "SLUG_KIND", "kind_of", "limit",
           "size", "over", "summarize", "allowance", "audit"]
