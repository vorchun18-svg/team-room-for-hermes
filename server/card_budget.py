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

# --- W28-E: урезанное тело карточки (замер до/после — docs/status/w28-tokens.md) ---
#: Потолок КОРОТКОГО устава в корневой карточке (замер: было 4 722 знака, стало
#: 3 882 на тех же данных). Подробный устав живёт файлом в папке проекта
#: (`contract.ensure_contract_file`), в карточке — короткие правила и путь к файлу.
#: Запас до 4 400 — на длинные пути: путь папки проекта и отчётов входит в тело
#: как есть, а обрезать путь нельзя (по нему сохраняют файлы).
CONTRACT_SHORT_MAX = 4400
#: Потолок абзаца «роль руководителя / правила комнаты» в коротком уставе.
LEADER_ROLE_MAX = 900
#: Сколько знаков оставляем от результата одного родителя и от «что появилось».
#: Полный текст — в карточке (`kanban_show`) или файле, путь/номер идут рядом.
PARENT_RESULT_MAX = 1200
#: Сколько родителей вообще берём (дальше — только счётчик и пути).
PARENTS_MAX = 4
#: «Последние N» для лент: работа участника и переписка карточки.
RECENT_MAX = 3
COMMENTS_MAX = 5
#: Первые K знаков предыдущего свода, которые ещё кладём в тело доработки
#: (дальше — выжимка по разделам и ссылка на карточку версии).
PREV_PREFIX_MAX = 1200


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


def _norm_key(text: str) -> str:
    """Ключ сравнения записей ленты: без пробелов, регистра и пунктуации по краям."""
    return " ".join(str(text or "").split()).strip().lower()[:400]


def recent_unique(items, n: int = RECENT_MAX, key=None) -> list:
    """Последние ``n`` записей без повторов (W28-E: «Recent work» и «Comment thread»).

    Ленты карточки растут с каждым сообщением, и Hermes кладёт их в контекст целыми
    блоками (30 комментариев по 2 000 знаков). Правило: в теле — только последние
    ``n``, и одна и та же мысль не повторяется дважды (свод + комментарий, ответ +
    уведомление). Порядок сохраняется исходный (старые → новые), чтобы «последнее»
    читалось как последнее.
    """
    cap = max(0, int(n or 0))
    if cap <= 0:
        return []
    seen: set = set()
    kept: list = []
    for item in reversed(list(items or [])):
        value = item
        if callable(key):
            try:
                value = key(item)
            except Exception:                       # noqa: BLE001 - чужой ключ не должен ронять карточку
                value = item
        if isinstance(value, (dict, list)):
            value = str(value)
        mark = _norm_key(value)
        if mark and mark in seen:
            continue
        if mark:
            seen.add(mark)
        kept.append(item)
        if len(kept) >= cap:
            break
    kept.reverse()
    return kept


def parent_digest(results, cap: int = PARENT_RESULT_MAX, limit: int = PARENTS_MAX) -> list:
    """Резюме результатов родителей: по ``cap`` знаков на родителя + путь/карточка.

    ``results`` — ``[{id, summary|result, path?, status?}]``. Полный текст в тело не
    идёт: он лежит в карточке родителя (`kanban_show`) или файлом по ``path``.
    Возвращает ``[{id, path, chars, text, truncated}]``; пустые результаты пропускаются.
    """
    out: list = []
    for item in list(results or [])[: max(0, int(limit or 0))]:
        if not isinstance(item, dict):
            item = {"summary": str(item or "")}
        text = str(item.get("summary") or item.get("result") or "").strip()
        if not text:
            continue
        whole = len(text)
        cut = summarize(text, max(0, int(cap or 0))) if whole > int(cap or 0) else text
        out.append({"id": str(item.get("id") or ""), "path": str(item.get("path") or ""),
                    "chars": whole, "text": cut, "truncated": len(cut) < whole})
    return out


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
           "size", "over", "summarize", "allowance", "audit",
           "CONTRACT_SHORT_MAX", "LEADER_ROLE_MAX", "PARENT_RESULT_MAX", "PARENTS_MAX",
           "RECENT_MAX", "COMMENTS_MAX", "PREV_PREFIX_MAX", "recent_unique", "parent_digest"]
