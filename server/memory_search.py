"""Поиск по памяти комнаты: «почему мы так решили» (W29B-Q8).

Зачем отдельный поиск, если есть ``/api/search``: ``server/search.py`` ищет по
оперативному состоянию комнаты (запросы, бэклог, журнал, этапы) и живёт по TTL
10 с. Память — это то, что остаётся после запроса: **реестр решений**
(``data/state/<room>/memory/registry.jsonl``, append-only с хеш-цепочкой), **своды**
и отчёты, **ответы**, **журнал** и **заметки зеркала Vault** (``…/Vault/Projects/
Rooms/<room>/**.md``). Человек спрашивает «почему мы так решили», и ему нужна
цитата с датой и ссылкой на запрос, а не список похожих слов.

Правила:

* только чтение; ничего не пишется, модель не вызывается;
* пустой запрос — пустой ответ (никаких «всё подряд»);
* у каждой находки есть ``snippet`` (цитата ±80 знаков), ``at`` (дата), ``route``
  (куда идти) и ``citation`` — короткая подпись источника для человека;
* битая строка реестра не валит поиск (её показывает «Проверка», не поиск);
* нормализация, токенизация и сниппеты — те же, что в ``server/search.py``
  (``fold``/``tokens``/``_tok_score``/``_snippet``): один способ искать в
  приложении, а не два.

Публичный интерфейс::

    search(cfg, q, room=None, limit=20, kinds=None) -> dict
    ROUTES / install_memory_search_routes(App, ROUTES)
"""
from __future__ import annotations

import json
import re as _re
import time
from pathlib import Path

try:                                     # пакетный и «плоский» импорт
    from . import decision_registry as dr
    from . import search as text_search
    from .app_common import err
except ImportError:                      # pragma: no cover
    import decision_registry as dr       # type: ignore
    import search as text_search         # type: ignore
    from app_common import err            # type: ignore

ROUTES = [
    ("GET", r"/api/memory/search", "h_memory_search"),
]

#: Порядок и подписи видов памяти (интерфейс показывает их как разделы).
KINDS = ("decision", "svod", "answer", "journal", "note", "passport")
KIND_TITLES = {
    "decision": "Решение",
    "svod": "Свод",
    "answer": "Ответ",
    "journal": "Журнал",
    "note": "Заметка",
    # W30-D1: паспорт принятой задачи — итог, утверждённый человеком.
    "passport": "Принятая задача",
}
#: Вес вида в выдаче: решение — главный ответ на «почему мы так решили».
WEIGHTS = {"decision": 1.5, "svod": 1.2, "answer": 1.3, "journal": 0.8, "note": 0.9,
           "passport": 1.4}
#: Потолки чтения: поиск не должен читать гигабайты по Ctrl+K.
MAX_FILES = 200
MAX_BYTES = 262_144
MAX_LIMIT = 50
DEFAULT_LIMIT = 20
#: Подпись цитаты: откуда взято, чтобы человек верил не только тексту.
SOURCE_TITLES = {
    "decision": "реестр решений",
    "svod": "свод комнаты",
    "answer": "ответ человека",
    "journal": "журнал",
    "note": "заметка Vault",
    "passport": "паспорт принятой задачи",
}


# --------------------------------------------------------------- пути/чтение
def room_state(cfg, room_id) -> Path:
    """Каталог состояния комнаты: ``data/state/<room>``."""
    return Path(cfg.state_dir) / str(room_id)


def vault_room_dir(cfg, room_id) -> Path | None:
    """Каталог заметок комнаты в Vault (``…/Vault/Projects/Rooms/<room>``) или None."""
    base = getattr(cfg, "vault_dir", None)
    if not base:
        return None
    path = Path(base) / str(room_id)
    return path if path.is_dir() else None


def _read_text(path: Path, budget: int = MAX_BYTES) -> str:
    """Текст файла (не больше ``budget`` байт); нет файла — пустая строка."""
    try:
        with path.open("r", encoding="utf-8-sig", errors="replace") as handle:
            return handle.read(budget)
    except OSError:
        return ""


def _json_items(value) -> list[dict]:
    """Плоский список объектов из словаря/списка состояния."""
    if isinstance(value, dict):
        value = value.get("items")
    return [item for item in (value or []) if isinstance(item, dict)]


def _int(value) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _flat(value) -> str:
    return " ".join(str(value or "").split())


def _route(room: str, request_id: str) -> str:
    """Куда ведёт находка: страница запроса, иначе «От вас нужно» комнаты."""
    room = str(room or "")
    request_id = str(request_id or "")
    if room and request_id:
        return "#/room/%s/request/%s" % (room, request_id)
    return "#/room/%s/backlog" % room if room else "#/today"


# ------------------------------------------------------------------ источники
def _decision_docs(cfg, room_id: str) -> list[dict]:
    """Реестр решений: последняя по ``seq`` строка каждого решения."""
    out: list[dict] = []
    try:
        rows = dr.rows(cfg, room_id)
    except Exception:                    # noqa: BLE001 — реестр вторичен, поиск важнее
        rows = []
    current: dict = {}
    for row in rows:
        did = str(row.get("id") or "")
        if not did:
            continue
        prev = current.get(did)
        if prev is None or _int(row.get("seq")) >= _int(prev.get("seq")):
            current[did] = row
    for did, row in current.items():
        status = str(row.get("status") or "")
        text = _flat(row.get("text"))
        if not text:
            continue
        why = _flat(row.get("why"))
        title = text if len(text) <= 120 else text[:119] + "…"
        body = " ".join(x for x in (text, why, status) if x)
        out.append({
            "kind": "decision", "room": room_id, "id": did, "title": title,
            "text": body, "at": _int(row.get("at")),
            "status": status, "why": why,
            "request_id": str(row.get("source_request") or ""),
            "source": "реестр решений",
        })
    return out


def _state_docs(cfg, room_id: str) -> list[dict]:
    """Ответы и журнал комнаты: ``state.json`` + то, что отдаёт ``ingest.load_state``."""
    state: dict = {}
    raw = _read_text(room_state(cfg, room_id) / "state.json")
    if raw.strip():
        try:
            data = json.loads(raw)
            state = data if isinstance(data, dict) else {}
        except ValueError:
            state = {}
    try:
        from . import ingest as ingest_mod
    except ImportError:                  # pragma: no cover
        ingest_mod = None        # type: ignore
    if ingest_mod is not None:
        try:
            got = ingest_mod.load_state(cfg, room_id)
        except Exception:                # noqa: BLE001 — состояние вторично для поиска
            got = None
        # Дополняем, а не заменяем: у состояния из ingest бывает не всё (ответы и
        # журнал живут в файле комнаты, а ingest знает только сводку).
        if isinstance(got, dict):
            for key in ("answers", "journal"):
                if got.get(key) and not state.get(key):
                    state[key] = got[key]
    out: list[dict] = []
    for item in _json_items(state.get("answers")):
        text = _flat(item.get("text") or item.get("answer"))
        if not text:
            continue
        out.append({
            "kind": "answer", "room": room_id, "id": str(item.get("id") or ""),
            "title": text if len(text) <= 120 else text[:119] + "…",
            "text": " ".join(x for x in (text, _flat(item.get("question")),
                                         _flat(item.get("by"))) if x),
            "at": _int(item.get("at") or item.get("created_at")),
            "request_id": str(item.get("source_request") or item.get("request_id") or ""),
            "source": "ответ человека",
        })
    for item in _json_items(state.get("journal")):
        text = _flat(item.get("text") or item.get("kind"))
        if not text:
            continue
        detail = item.get("detail")
        extra = ""
        if isinstance(detail, dict):
            extra = " ".join(_flat(detail.get(k)) for k in
                             ("summary", "answer", "title", "text") if detail.get(k))
        elif detail:
            extra = _flat(detail)
        out.append({
            "kind": "journal", "room": room_id, "id": str(item.get("id") or ""),
            "title": text if len(text) <= 120 else text[:119] + "…",
            "text": " ".join(x for x in (text, _flat(item.get("kind")),
                                         _flat(item.get("actor")), extra) if x),
            "at": _int(item.get("at")),
            "request_id": str(item.get("request_id") or ""),
            "source": "журнал",
        })
    return out


def _text_docs(cfg, room_id: str) -> list[dict]:
    """Своды/отчёты комнаты и заметки Vault: markdown-файлы, по одной находке на файл."""
    bases: list[tuple[Path, str]] = [(room_state(cfg, room_id), "svod")]
    try:
        reports = Path(cfg.reports_dir) / str(room_id)
    except Exception:                    # noqa: BLE001 — каталog отчётов вторичен
        reports = None
    if reports is not None and reports.is_dir():
        bases.append((reports, "svod"))
    vault = vault_room_dir(cfg, room_id)
    if vault is not None:
        bases.append((vault, "note"))
    out: list[dict] = []
    seen = 0
    for base, default_kind in bases:
        if not base.is_dir():
            continue
        try:
            files = sorted(base.rglob("*.md"))
        except OSError:
            continue
        for path in files:
            if seen >= MAX_FILES:
                break
            seen += 1
            text = _read_text(path)
            if not text.strip():
                continue
            name = path.name
            kind = default_kind
            if default_kind == "svod" and "svod" not in name.lower() and "отч" not in name.lower():
                # Прочие .md в состоянии комнаты (справки, черновики) — заметки, не своды.
                kind = "note"
            head = next((line.strip("# ").strip() for line in text.splitlines()
                         if line.strip()), name)
            try:
                at = int(path.stat().st_mtime)
            except OSError:
                at = 0
            out.append({
                "kind": kind, "room": room_id, "id": "file:%s" % name,
                "title": head[:120] or name, "text": _flat(text),
                "at": at, "request_id": "",
                "source": "заметка Vault" if kind == "note" else "свод комнаты",
                "path": str(path),
            })
    return out


def _passport_docs(cfg, room_id: str) -> list[dict]:
    """Паспорта принятых задач комнаты (W30-D1): по находке на задачу.

    Итог, утверждённый человеком, — самая надёжная память комнаты: «что решили и
    чем закончилось». Читается только файл ``data/state/<room>/accepted.json``,
    модели не вызываются.
    """
    try:
        try:
            from . import passports as passports_mod
        except ImportError:  # pragma: no cover
            import passports as passports_mod      # type: ignore
        tasks = passports_mod.load(cfg, room_id)
    except Exception:                    # noqa: BLE001 — память вторична для поиска
        return []
    out: list[dict] = []
    for root_id, entry in (tasks or {}).items():
        entry = entry if isinstance(entry, dict) else {}
        passport = entry.get("passport") if isinstance(entry.get("passport"), dict) else {}
        title = _flat(passport.get("title") or root_id)
        outcome = _flat(passport.get("outcome"))
        decisions = " ".join(_flat(d) for d in (passport.get("decisions") or []) if d)
        if not title and not outcome:
            continue
        body = " ".join(x for x in (title, outcome, decisions) if x)
        out.append({
            "kind": "passport", "room": room_id, "id": str(root_id),
            "title": title if len(title) <= 120 else title[:119] + "…",
            "text": body, "at": _int(entry.get("at")),
            "status": "принята", "request_id": str(root_id),
            "source": "паспорт принятой задачи",
        })
    return out


def room_docs(cfg, room_id: str, kinds=None) -> list[dict]:
    """Все документы памяти комнаты (по видам, если ``kinds`` задан)."""
    want = {str(k) for k in kinds} if kinds else set(KINDS)
    docs: list[dict] = []
    if "decision" in want:
        docs.extend(_decision_docs(cfg, room_id))
    if "passport" in want:
        docs.extend(_passport_docs(cfg, room_id))
    if "svod" in want or "answer" in want or "journal" in want:
        for doc in _state_docs(cfg, room_id):
            if doc["kind"] in want:
                docs.append(doc)
    if "svod" in want or "note" in want:
        for doc in _text_docs(cfg, room_id):
            if doc["kind"] in want:
                docs.append(doc)
    for doc in docs:
        doc["title_flat"] = text_search.fold(doc["title"])
        doc["body_flat"] = text_search.fold(doc["text"])
        doc["title_words"] = [t[0] for t in text_search.tokens(doc["title_flat"])]
        doc["body_words"] = [t[0] for t in text_search.tokens(doc["body_flat"])]
    return docs


# --------------------------------------------------------------------- поиск
def _score(doc: dict, qwords, qfold: str) -> float:
    """Оценка: покрытие слов запроса × вес вида + бонус за точное вхождение."""
    total, matched = 0.0, 0
    for qt in qwords:
        s_body = text_search._tok_score(qt, doc["body_words"])
        s_title = text_search._tok_score(qt, doc["title_words"])
        value = max(s_body, s_title * 1.5)
        if value <= 0:
            continue
        matched += 1
        total += value
    if not matched:
        return 0.0
    coverage = matched / float(len(qwords))
    score = (total / len(qwords)) * WEIGHTS.get(doc["kind"], 1.0) * (0.6 + 0.4 * coverage)
    if qfold and qfold in doc["title_flat"]:
        score += 1.0
    if qfold and qfold in doc["body_flat"]:
        score += 0.6
    if matched < len(qwords):
        score *= 0.55
    return score


def _citation(doc: dict) -> str:
    """Подпись источника для человека: «Решение · 12.11 · реестр решений»."""
    kind = doc.get("kind") or ""
    title = KIND_TITLES.get(kind, SOURCE_TITLES.get(kind, "Память"))
    stamp = ""
    at = _int(doc.get("at"))
    if at > 0:
        stamp = time.strftime("%d.%m.%Y", time.localtime(at))
    parts = [title]
    if stamp:
        parts.append(stamp)
    if doc.get("status"):
        parts.append(str(doc["status"]))
    return " · ".join(parts)


def _limit(value, default: int = DEFAULT_LIMIT) -> int:
    try:
        return max(1, min(MAX_LIMIT, int(value)))
    except (TypeError, ValueError):
        return default


def search(cfg, q, room=None, limit=DEFAULT_LIMIT, kinds=None) -> dict:
    """Найти ``q`` в памяти комнаты (или всех комнат). Никогда не бросает."""
    text = _flat(q)
    if not text:
        return {"q": "", "items": [], "count": 0, "total": 0, "by_kind": {},
                "rooms": [], "note": "Пустой запрос — пустой ответ: спросите словами, "
                                     "почему что-то решили."}
    if len(text) < 2:
        return {"q": text, "items": [], "count": 0, "total": 0, "by_kind": {},
                "rooms": [], "note": "Нужно хотя бы два символа."}
    qfold = text_search.fold(text)
    qwords = [w for w, _s, _e in text_search.tokens(qfold) if w]
    if not qwords:
        return {"q": text, "items": [], "count": 0, "total": 0, "by_kind": {},
                "rooms": [], "note": "В запросе нет слов — искать нечего."}
    room_ids = list(room) if isinstance(room, (list, tuple)) else ([str(room)] if room else [])
    if not room_ids:
        try:
            room_ids = text_search._rooms(cfg)
        except Exception:                # noqa: BLE001 — список комнат вторичен
            room_ids = []
    hits: list[dict] = []
    seen: set = set()
    for room_id in room_ids:
        room_id = str(room_id or "")
        if not room_id:
            continue
        try:
            docs = room_docs(cfg, room_id, kinds)
        except Exception:                # noqa: BLE001 — битая комната не валит поиск
            continue
        for doc in docs:
            score = _score(doc, qwords, qfold)
            if score <= 0:
                continue
            key = (doc["kind"], doc["id"], doc["title_flat"][:60])
            if key in seen:
                continue
            seen.add(key)
            snippet, ranges = text_search._snippet(
                {"body": doc["text"], "body_flat": doc["body_flat"], "title": doc["title"]},
                qfold, qwords)
            hits.append({
                "kind": doc["kind"],
                "title": doc["title"],
                "snippet": snippet,
                "ranges": ranges,
                "at": doc.get("at") or None,
                "room": room_id,
                "id": doc.get("id") or "",
                "request_id": doc.get("request_id") or "",
                "route": _route(room_id, doc.get("request_id") or ""),
                "status": doc.get("status") or "",
                "source": doc.get("source") or SOURCE_TITLES.get(doc["kind"], ""),
                "citation": _citation(doc),
                "score": round(score, 3),
            })
    hits.sort(key=lambda h: (-float(h.get("score") or 0), -_int(h.get("at")),
                             KINDS.index(h["kind"]) if h["kind"] in KINDS else 9,
                             h.get("title") or ""))
    by_kind: dict = {}
    for hit in hits:
        by_kind[hit["kind"]] = by_kind.get(hit["kind"], 0) + 1
    limit = _limit(limit)
    items = hits[:limit]
    return {"q": text, "items": items, "count": len(items), "total": len(hits),
            "by_kind": by_kind, "rooms": [r for r in room_ids if r],
            "note": "" if hits else "В памяти комнаты такого нет. Попробуйте слово из "
                                    "решения или название запроса."}


# ----------------------------------------------------------------------- HTTP
class MemorySearchRoutes:
    """Обработчик поиска по памяти (``self`` — экземпляр ``server.app.App``)."""

    def h_memory_search(self, query, body):
        """``GET /api/memory/search?q=…&room=…&limit=…`` — цитаты из памяти комнат."""
        params = query or {}
        q = str(_first(params, "q") or "").strip()
        room = str(_first(params, "room") or "").strip()
        kinds_raw = str(_first(params, "kinds") or "").strip()
        kinds = [k for k in kinds_raw.replace(",", " ").split() if k in KINDS] or None
        mod = self.mod("memory_search")
        rooms = [room] if room else [str((r or {}).get("id") or "")
                                     for r in self._rooms(include_archived=False)]
        rooms = [r for r in rooms if r]
        payload = mod.search(self.cfg, q, room=rooms, limit=_limit(_first(params, "limit")),
                             kinds=kinds)
        payload["generated_at"] = int(time.time())
        return 200, payload, {"Cache-Control": "no-store"}


def _first(params: dict, key: str):
    """Первое значение параметра запроса (``dict`` из http_util)."""
    value = (params or {}).get(key)
    if isinstance(value, (list, tuple)):
        return value[0] if value else None
    return value


def install_memory_search_routes(app_cls, routes: list) -> None:
    """Подключить поиск по памяти к ``App``: метод + строка таблицы маршрутов."""
    for name, value in vars(MemorySearchRoutes).items():
        if name.startswith("__") or not callable(value):
            continue
        setattr(app_cls, name, value)
    existing = {(method, getattr(pattern, "pattern", pattern)) for method, pattern, _h in routes}
    for method, pattern, handler in ROUTES:
        if (method, pattern) in existing:
            continue
        routes.append((method, _re.compile(pattern), handler))


__all__ = ["KINDS", "KIND_TITLES", "SOURCE_TITLES", "ROUTES", "room_docs", "vault_room_dir",
           "search", "MemorySearchRoutes", "install_memory_search_routes"]
