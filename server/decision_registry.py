"""Реестр решений и обещаний комнаты: append-only журнал с хеш-цепочкой (W25 M1).

Зачем: `data/state/<room>/decisions.json` — перезаписываемый список «принятых»
решений. Из-за перезаписи решение, отменённое в запросе (``dismissed`` /
``superseded``), оставалось в файле как ``accepted`` и попадало в паспорт
комнаты — исполнитель работал по отменённому решению (R4 §3 п.1). Здесь
единственный источник правды: ``data/state/<room>/memory/registry.jsonl``,
одна строка = одно событие решения, ничего не перезаписывается.

Правила (E1 П5, E5 №7):

* **единственный писатель** — :func:`append`. Агент не пишет файл сам: он
  отдаёт событие в API/ingest, приложение дописывает строку;
* блокировка файла **межпроцессная** (``msvcrt`` на Windows, ``fcntl`` на
  POSIX) + ``O_APPEND`` + ``fsync``: параллельные записи не теряются;
* **хеш-цепочка**: у каждой строки ``prev_hash`` (хеш предыдущей) и ``hash``
  (sha256 от канонического JSON строки вместе с ``prev_hash``). Подмена любой
  строки ломает цепочку — :func:`verify` показывает место разрыва;
* смена статуса — **новая строка** того же ``id`` со ссылкой ``superseded``
  (``seq`` прежней строки) и ``prev_status``; ``decisions.json`` — проекция,
  пересобираемая из реестра (:func:`project_decisions`);
* ``pinned.json`` — «не сжимать никогда», с потолком :data:`PIN_LIMIT`
  записей на комнату (превышение — предупреждение в «Здоровье»).

Формат строки::

    {id, seq, at, text, why, status, prev_status, by, source_request,
     due, supersedes, prev_hash, hash}

``id`` — идентификатор решения вида ``<root_id>:d_1`` (в реестре комнаты id
всегда с областью запроса), ``seq`` — номер строки в журнале (1, 2, …).
Только stdlib.
"""
from __future__ import annotations

import hashlib
import json
import os
import time
from pathlib import Path

try:  # пакетный и «плоский» импорт
    from . import statefiles as sf
except ImportError:  # pragma: no cover
    import statefiles as sf  # type: ignore

MEMORY_DIR = "memory"
REGISTRY_FILE = "registry.jsonl"
LOCK_FILE = "registry.lock"
PINNED_FILE = "pinned.json"
SNAPSHOT_FILE = "snapshot.json"

#: Все статусы решения (R4 §4.1). Никаких других значений в реестре быть не может.
STATUSES = ("proposed", "accepted", "rejected", "dismissed", "superseded")
#: Статусы, при которых решение действует (попадает в паспорт и контекст-пакет).
LIVE_STATUSES = ("accepted",)
#: Потолок закрепления (W25 M1 п.4).
PIN_LIMIT = 30
#: Значение ``prev_hash`` первой строки.
GENESIS = "0" * 64

_HASHED_FIELDS = ("id", "seq", "at", "text", "why", "status", "prev_status", "by",
                  "source_request", "due", "supersedes")


# --------------------------------------------------------------------- пути ---

def memory_dir(cfg, room_id) -> Path:
    return Path(cfg.state_dir) / str(room_id) / MEMORY_DIR


def registry_path(cfg, room_id) -> Path:
    return memory_dir(cfg, room_id) / REGISTRY_FILE


def pinned_path(cfg, room_id) -> Path:
    return memory_dir(cfg, room_id) / PINNED_FILE


def snapshot_path(cfg, room_id) -> Path:
    return memory_dir(cfg, room_id) / SNAPSHOT_FILE


def lock_path(cfg, room_id) -> Path:
    return memory_dir(cfg, room_id) / LOCK_FILE


# ------------------------------------------------------------ хеш и цепочка ---

def canonical(payload: dict) -> str:
    """Канонический JSON для хеша: стабильный порядок ключей, без пробелов."""
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def row_hash(row: dict) -> str:
    """sha256 от полей строки вместе с ``prev_hash`` (цепочка, а не только строка)."""
    body = {key: row.get(key) for key in _HASHED_FIELDS}
    body["prev_hash"] = str(row.get("prev_hash") or GENESIS)
    return hashlib.sha256(canonical(body).encode("utf-8")).hexdigest()


# ------------------------------------------------------------------ чтение ---

def _parse_lines(text: str) -> tuple[list[dict], str | None]:
    """Строки журнала + текст последней битой строки (``None``, если всё цело)."""
    rows: list[dict] = []
    broken: str | None = None
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line:
            continue
        try:
            item = json.loads(line)
        except ValueError:
            broken = line[:200]
            continue
        if isinstance(item, dict):
            rows.append(item)
        else:
            broken = line[:200]
    return rows, broken


def rows(cfg, room_id) -> list[dict]:
    """Все строки журнала по порядку (битые строки пропускаются — их видит :func:`verify`)."""
    path = registry_path(cfg, room_id)
    try:
        text = path.read_text(encoding="utf-8-sig")
    except FileNotFoundError:
        return []
    except OSError:
        return []
    return _parse_lines(text)[0]


def read_state(cfg, room_id) -> dict:
    """Свёртка журнала: ``{id: последняя строка}`` + метаданные для записи."""
    items = rows(cfg, room_id)
    current: dict[str, dict] = {}
    for item in items:
        did = str(item.get("id") or "")
        if not did:
            continue
        prev = current.get(did)
        if prev is None or int(item.get("seq") or 0) >= int(prev.get("seq") or 0):
            current[did] = item
    last = items[-1] if items else None
    return {"rows": items, "current": current, "count": len(items),
            "last_seq": int((last or {}).get("seq") or 0),
            "last_hash": str((last or {}).get("hash") or GENESIS)}


def current(cfg, room_id) -> dict:
    return read_state(cfg, room_id)["current"]


def active(cfg, room_id) -> list[dict]:
    """Только действующие решения (``accepted``), свежие сверху, с ``id``."""
    items = [dict(row) for row in current(cfg, room_id).values()
             if str(row.get("status") or "") in LIVE_STATUSES]
    items.sort(key=lambda row: (-int(row.get("at") or 0), str(row.get("id") or "")))
    return items


def by_status(cfg, room_id) -> dict:
    out: dict[str, list[dict]] = {status: [] for status in STATUSES}
    for row in current(cfg, room_id).values():
        out.setdefault(str(row.get("status") or "proposed"), []).append(dict(row))
    return out


def verify(cfg, room_id) -> dict:
    """Проверка цепочки: ``{ok, rows, broken_at, reason}``.

    Ломается, если (а) строка изменена — её ``hash`` не совпадает с
    пересчитанным; (б) строка удалена/вставлена — ``prev_hash`` не равен хешу
    предыдущей; (в) ``seq`` не растёт на единицу; (г) есть битая JSON-строка.
    """
    path = registry_path(cfg, room_id)
    try:
        text = path.read_text(encoding="utf-8-sig")
    except FileNotFoundError:
        return {"ok": True, "rows": 0, "broken_at": None, "reason": "", "missing": True}
    except OSError as exc:
        return {"ok": False, "rows": 0, "broken_at": None, "reason": "файл не читается: %s" % exc}
    items, broken_line = _parse_lines(text)
    expected_prev = GENESIS
    for index, row in enumerate(items, start=1):
        seq = int(row.get("seq") or 0)
        if seq != index:
            return {"ok": False, "rows": len(items), "broken_at": index,
                    "reason": "seq=%s вместо %s" % (seq, index)}
        if str(row.get("prev_hash") or "") != expected_prev:
            return {"ok": False, "rows": len(items), "broken_at": index,
                    "reason": "prev_hash не совпадает с хешем предыдущей строки"}
        if row_hash(row) != str(row.get("hash") or ""):
            return {"ok": False, "rows": len(items), "broken_at": index,
                    "reason": "строка изменена: hash не совпадает"}
        expected_prev = str(row.get("hash") or "")
    if broken_line:
        return {"ok": False, "rows": len(items), "broken_at": len(items) + 1,
                "reason": "битая строка: %s" % broken_line[:80]}
    return {"ok": True, "rows": len(items), "broken_at": None, "reason": ""}


# ------------------------------------------------- межпроцессная блокировка ---

class _FileLock:
    """Эксклюзивная блокировка отдельного файла-замка (потоки + процессы).

    ``statefiles.lock_for`` сериализует только потоки одного процесса; запись в
    реестр идёт ещё и из подпроцессов (миграция, сторож), поэтому нужен замок
    уровня ОС. Ожидание — с потолком :data:`_LOCK_TIMEOUT`, чтобы зависший
    процесс не подвесил сервер навсегда.
    """

    def __init__(self, path: Path, timeout: float = 20.0):
        self.path = Path(path)
        self.timeout = timeout

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._fd = os.open(str(self.path), os.O_RDWR | os.O_CREAT, 0o644)
        deadline = time.time() + self.timeout
        while True:
            try:
                _os_lock(self._fd)
                return self
            except OSError:
                if time.time() >= deadline:
                    os.close(self._fd)
                    raise TimeoutError("реестр решений занят другим процессом")
                time.sleep(0.005)

    def __exit__(self, *exc_info):
        try:
            _os_unlock(self._fd)
        finally:
            os.close(self._fd)
        return False


def _os_lock(fd: int) -> None:
    if os.name == "nt":  # pragma: no cover - ветка Windows
        import msvcrt
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
    else:  # pragma: no cover - POSIX (CI)
        import fcntl
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)


def _os_unlock(fd: int) -> None:
    try:
        if os.name == "nt":  # pragma: no cover
            import msvcrt
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        else:  # pragma: no cover
            import fcntl
            fcntl.flock(fd, fcntl.LOCK_UN)
    except OSError:
        pass


# ------------------------------------------------------------------ запись ---

def _now() -> int:
    return int(time.time())


def _clean(value) -> str:
    return " ".join(str(value or "").split())


def _scoped(decision_id, root_id) -> str:
    did = str(decision_id or "").strip()
    if not did:
        return ""
    return did if ":" in did else ("%s:%s" % (root_id, did) if root_id else did)


def _plan_row(state: dict, entry: dict, at: int, by: str) -> dict | None:
    """Следующая строка журнала для события или ``None`` (ничего не изменилось)."""
    did = _scoped(entry.get("id"), entry.get("source_request"))
    if not did:
        return None
    status = str(entry.get("status") or "proposed").strip().lower()
    if status not in STATUSES:
        raise ValueError("неизвестный статус решения: %r" % status)
    text = _clean(entry.get("text"))
    prev = state["current"].get(did) or {}
    prev_status = str(prev.get("status") or "")
    prev_text = str(prev.get("text") or "")
    why = _clean(entry.get("why")) or None
    due = _clean(entry.get("due")) or None
    if prev and prev_status == status:
        # идемпотентность: тот же статус не плодит строк; текст обновляем только
        # пока решение открыто (закрытое решение — исторический факт)
        if status == "proposed" and text and text != prev_text:
            return {"id": did, "at": at, "text": text, "why": why or prev.get("why"),
                    "status": status, "prev_status": prev_status, "by": by,
                    "source_request": entry.get("source_request") or prev.get("source_request"),
                    "due": due or prev.get("due"), "supersedes": int(prev.get("seq") or 0) or None}
        return None
    if prev and status == "proposed" and prev_status in ("rejected", "dismissed", "superseded"):
        # «Вернуть» решение в работу — явное событие владельца, а не воскрешение:
        # новая строка допустима только при by="user".
        if by != "user":
            return None
    if prev and status == "proposed" and prev_status == "accepted" and by != "user":
        # автоматический слой (приём ответа) не понижает принятое решение до
        # «предложено»: это и есть «воскрешение» наоборот
        return None
    return {
        "id": did, "at": at, "text": text or prev_text,
        "why": why if why is not None else (prev.get("why") if status in ("dismissed", "superseded",
                                                                         "rejected") else None),
        "status": status, "prev_status": prev_status or None, "by": by,
        "source_request": entry.get("source_request") or prev.get("source_request"),
        "due": due if due is not None else prev.get("due"),
        "supersedes": int(prev.get("seq") or 0) or None,
    }


def append(cfg, room_id, entries, at: int | None = None, by: str = "app") -> list[dict]:
    """Дописать события в реестр. ЕДИНСТВЕННЫЙ писатель журнала.

    ``entries`` — список словарей ``{id, text, why, status, source_request, due}``.
    Возвращает фактически записанные строки (пустой список = всё уже было).
    Под блокировкой: перечитываем хвост (seq/prev_hash), пишем по строке,
    ``fsync`` — параллельные записи не теряются и цепочка не рвётся.
    """
    entries = [entry for entry in (entries or []) if isinstance(entry, dict)]
    if not entries:
        return []
    at = int(at or _now())
    path = registry_path(cfg, room_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    written: list[dict] = []
    with _FileLock(lock_path(cfg, room_id)):
        state = read_state(cfg, room_id)
        seq = state["last_seq"]
        prev_hash = state["last_hash"]
        for entry in entries:
            row = _plan_row(state, entry, at, by)
            if row is None:
                continue
            seq += 1
            row["seq"] = seq
            row["prev_hash"] = prev_hash
            row["hash"] = row_hash(row)
            line = json.dumps({key: row.get(key) for key in
                               ("id", "seq", "at", "text", "why", "status", "prev_status", "by",
                                "source_request", "due", "supersedes", "prev_hash", "hash")},
                              ensure_ascii=False, sort_keys=True) + "\n"
            mode = "a"
            with open(path, mode, encoding="utf-8", newline="\n") as handle:
                handle.write(line)
                handle.flush()
                os.fsync(handle.fileno())
            prev_hash = row["hash"]
            state["current"][row["id"]] = row
            written.append(row)
    return written


def set_status(cfg, room_id, decision_id, status: str, by: str = "app", why: str | None = None,
               source_request: str | None = None, text: str | None = None,
               due: str | None = None, at: int | None = None) -> list[dict]:
    """Смена статуса = новая строка со ссылкой на прежнюю (``supersedes``)."""
    return append(cfg, room_id, [{"id": decision_id, "status": status, "why": why,
                                  "source_request": source_request, "text": text, "due": due}],
                  at=at, by=by)


def sync_request(cfg, room_id, root_id: str, decisions, at: int | None = None,
                 by: str = "app") -> list[dict]:
    """Свести решения запроса (с УЖЕ разрешённым статусом) в реестр комнаты.

    ``decisions`` — элементы ``detail['decisions']``: там статус окончательный
    (``accepted``/``rejected``/``proposed`` от руководителя, ``dismissed`` от
    действия владельца, ``superseded`` от истории версий свода). Именно здесь
    отменённое решение перестаёт быть «принятым» — и паспорт комнаты этого не
    увидит (починка «воскрешения», R4 §3 п.1).
    """
    entries = []
    for item in decisions or []:
        if not isinstance(item, dict):
            continue
        entries.append({"id": item.get("id"), "text": item.get("text"), "why": item.get("why"),
                        "status": item.get("status") or "proposed", "due": item.get("due"),
                        "source_request": str(item.get("source_request") or root_id)})
    return append(cfg, room_id, entries, at=at, by=by)


# ------------------------------------------------------- проекция decisions ---

def project_decisions(cfg, room_id, room: dict | None = None) -> dict | None:
    """Пересобрать ``decisions.json`` из реестра (проекция, не источник правды).

    Читатели, которые ещё не переведены на реестр, видят те же статусы, что и
    паспорт: отменённое решение не «воскресает» даже в старом файле.
    """
    path = Path(cfg.state_dir) / str(room_id) / "decisions.json"
    items = list(current(cfg, room_id).values())
    if not items and not path.exists():
        return None
    items.sort(key=lambda row: (int(row.get("at") or 0), str(row.get("id") or "")))
    data = {"room": str(room_id), "decisions": [], "updated_at": 0, "projection_of": REGISTRY_FILE}
    for row in items:
        data["decisions"].append({
            "id": str(row.get("id") or ""), "text": str(row.get("text") or ""),
            "why": row.get("why") or None, "status": str(row.get("status") or "proposed"),
            "owner": "mikhail", "at": int(row.get("at") or 0) or None,
            "updated_at": int(row.get("at") or 0) or None,
            "source_request": row.get("source_request"),
        })
        data["updated_at"] = max(data["updated_at"], int(row.get("at") or 0))
    with sf.lock_for(path):
        sf.write_json_atomic(path, data)
    return data


# ---------------------------------------------------------------- закрепление ---

def pinned_read(cfg, room_id) -> dict:
    data = sf.read_json(pinned_path(cfg, room_id), None)
    if not isinstance(data, dict) or not isinstance(data.get("items"), list):
        data = {"room": str(room_id), "items": [], "updated_at": 0}
    data["items"] = [item for item in data["items"] if isinstance(item, dict) and item.get("id")]
    return data


def pinned_ids(cfg, room_id) -> list[str]:
    return [str(item["id"]) for item in pinned_read(cfg, room_id)["items"]]


def pin(cfg, room_id, decision_id, why: str | None = None, by: str = "user") -> dict:
    """Закрепить запись: не сжимается никогда; потолок — :data:`PIN_LIMIT`.

    При достижении потолка новая запись НЕ добавляется (иначе «закреплено»
    съест весь бюджет контекста, E5 №7), ответ несёт ``over_limit: true`` —
    это предупреждение для экрана «Здоровье».
    """
    decision_id = str(decision_id or "").strip()
    if not decision_id:
        raise ValueError("нужен id решения")
    path = pinned_path(cfg, room_id)
    with sf.lock_for(path):
        data = pinned_read(cfg, room_id)
        ids = [str(item["id"]) for item in data["items"]]
        if decision_id in ids:
            return {"added": False, "reason": "already", "count": len(ids), "limit": PIN_LIMIT,
                    "over_limit": len(ids) >= PIN_LIMIT}
        if len(ids) >= PIN_LIMIT:
            return {"added": False, "reason": "pin_limit", "count": len(ids), "limit": PIN_LIMIT,
                    "over_limit": True}
        data["items"].append({"id": decision_id, "why": _clean(why) or None,
                              "at": _now(), "by": by})
        data["room"] = str(room_id)
        data["updated_at"] = _now()
        sf.write_json_atomic(path, data)
        return {"added": True, "count": len(data["items"]), "limit": PIN_LIMIT,
                "over_limit": len(data["items"]) >= PIN_LIMIT}


def unpin(cfg, room_id, decision_id) -> dict:
    decision_id = str(decision_id or "").strip()
    path = pinned_path(cfg, room_id)
    with sf.lock_for(path):
        data = pinned_read(cfg, room_id)
        kept = [item for item in data["items"] if str(item["id"]) != decision_id]
        removed = len(kept) != len(data["items"])
        if removed:
            data["items"] = kept
            data["room"] = str(room_id)
            data["updated_at"] = _now()
            sf.write_json_atomic(path, data)
        return {"removed": removed, "count": len(kept), "limit": PIN_LIMIT,
                "over_limit": len(kept) > PIN_LIMIT}


def pin_state(cfg, room_id) -> dict:
    """Состояние закрепления для «Здоровья»: сколько, потолок, превышение."""
    items = pinned_read(cfg, room_id)["items"]
    count = len(items)
    alive = [item for item in items if str(item.get("id")) in current(cfg, room_id)]
    return {"count": count, "alive": len(alive), "limit": PIN_LIMIT,
            "over_limit": count >= PIN_LIMIT,
            "warning": ("Закреплено %d из %d — новые закрепления не добавляются"
                        % (count, PIN_LIMIT)) if count >= PIN_LIMIT else ""}


# --------------------------------------------------------------- снимок id ---

def write_snapshot(cfg, room_id) -> dict:
    """Снимок множества id решений (инвариант №2: множество только растёт)."""
    ids = sorted(current(cfg, room_id))
    path = snapshot_path(cfg, room_id)
    previous = sf.read_json(path, None)
    prev_ids = sorted({str(i) for i in (previous or {}).get("ids") or []}) \
        if isinstance(previous, dict) else []
    missing = [did for did in prev_ids if did not in ids]
    data = {"room": str(room_id), "ids": ids, "at": _now(),
            "prev_count": len(prev_ids), "missing": missing}
    with sf.lock_for(path):
        sf.write_json_atomic(path, data)
    return data


def snapshot_missing(cfg, room_id) -> list[str]:
    """id, которые были в прошлом снимке и исчезли из реестра (должно быть пусто)."""
    data = sf.read_json(snapshot_path(cfg, room_id), None)
    if not isinstance(data, dict):
        return []
    ids = set(current(cfg, room_id))
    return [str(did) for did in data.get("ids") or [] if str(did) not in ids]


# --------------------------------------------------------- зеркало в Obsidian ---

_STATUS_RU = {"proposed": "предложено", "accepted": "принято", "rejected": "отклонено",
              "dismissed": "снято", "superseded": "устарело"}


def _fmt_date(ts) -> str:
    try:
        value = int(ts or 0)
    except (TypeError, ValueError):
        return ""
    if not value:
        return ""
    import datetime as _dt
    return _dt.datetime.fromtimestamp(value).strftime("%d.%m.%Y %H:%M")


def mirror_md(cfg, room_id) -> str:
    """Читаемый вид реестра для Vault (``Решения/реестр.md``)."""
    state = read_state(cfg, room_id)
    pins = set(pinned_ids(cfg, room_id))
    lines = ["# Реестр решений комнаты «%s»" % room_id, "",
             "_Файл — зеркало журнала `memory/registry.jsonl` (append-only, хеш-цепочка). "
             "Правки руками будут перетёрты при следующем обновлении._", "",
             "Строк в журнале: %d · решений: %d · действующих: %d" % (
                 state["count"], len(state["current"]),
                 len([r for r in state["current"].values() if r.get("status") == "accepted"])), ""]
    if not state["current"]:
        lines += ["Решений пока нет.", ""]
        return "\n".join(lines)
    order = {status: index for index, status in enumerate(STATUSES)}
    items = sorted(state["current"].values(),
                   key=lambda row: (order.get(str(row.get("status")), 9),
                                    -int(row.get("at") or 0), str(row.get("id") or "")))
    for row in items:
        mark = " 📌" if str(row.get("id")) in pins else ""
        lines.append("## %s%s" % (_clean(row.get("text"))[:120] or str(row.get("id")), mark))
        lines.append("")
        lines.append("- id: `%s` · статус: **%s**%s" % (
            row.get("id"), _STATUS_RU.get(str(row.get("status")), str(row.get("status"))),
            (" (было: %s)" % _STATUS_RU.get(str(row.get("prev_status")), row.get("prev_status")))
            if row.get("prev_status") else ""))
        if row.get("why"):
            lines.append("- почему: %s" % _clean(row.get("why")))
        if row.get("source_request"):
            lines.append("- запрос: `%s`" % row.get("source_request"))
        if row.get("due"):
            lines.append("- срок: %s" % _clean(row.get("due")))
        lines.append("- обновлено: %s" % (_fmt_date(row.get("at")) or "—"))
        lines.append("")
    return "\n".join(lines)


def write_mirror(cfg, room_id) -> Path | None:
    """Записать ``Rooms/<id>/Решения/реестр.md`` (атомарная подмена)."""
    vault_root = getattr(cfg, "vault_dir", None)
    if not vault_root:
        return None
    try:
        from . import config as config_mod
    except ImportError:  # pragma: no cover
        import config as config_mod  # type: ignore
    try:
        base = config_mod.room_vault_dir(cfg, room_id)
    except Exception:  # noqa: BLE001 - зеркало вторично
        return None
    path = Path(base) / "Решения" / "реестр.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    text = mirror_md(cfg, room_id)
    tmp = path.with_suffix(".md.tmp")
    tmp.write_text(text, encoding="utf-8", newline="\n")
    os.replace(str(tmp), str(path))
    return path


# ------------------------------------------------------------- миграция ------

def migrate_from_decisions(cfg, room_id, decisions: list | None = None,
                          at: int | None = None, backup: bool = True) -> dict:
    """Перенести ``decisions.json`` в реестр. Идемпотентно (повтор не дублирует).

    Перед изменением файла (если он меняется) рядом кладётся резервная копия
    ``decisions.json.bak-<unixtime>``. Сами строки реестра не перезаписываются:
    уже известное событие просто не добавляется повторно.
    """
    path = Path(cfg.state_dir) / str(room_id) / "decisions.json"
    if decisions is None:
        data = sf.read_json(path, None)
        decisions = (data or {}).get("decisions") if isinstance(data, dict) else []
    entries = []
    for item in decisions or []:
        if not isinstance(item, dict) or not item.get("id"):
            continue
        entries.append({"id": item.get("id"), "text": item.get("text"), "why": item.get("why"),
                        "status": item.get("status") or "proposed",
                        "source_request": item.get("source_request"),
                        "due": item.get("due")})
        at_value = int(item.get("at") or item.get("updated_at") or 0)
        if at_value:
            at = max(int(at or 0), at_value)
    written = append(cfg, room_id, entries, at=at, by="migration")
    backup_path = None
    if backup and written and path.exists():
        backup_path = path.with_name("decisions.json.bak-%d" % _now())
        try:
            backup_path.write_bytes(path.read_bytes())
        except OSError:
            backup_path = None
    if written:
        project_decisions(cfg, room_id)
        write_snapshot(cfg, room_id)
    return {"room": str(room_id), "source": len(entries), "written": len(written),
            "backup": str(backup_path) if backup_path else None,
            "registry": REGISTRY_FILE}
