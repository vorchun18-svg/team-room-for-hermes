"""Отчёты комнаты: индекс, поиск, валидация, отдача файлов отчёта.

Публичный интерфейс (фиксирован задачей):
    list_reports(cfg, room_id) -> list[ReportIndexItem]
    find_report(cfg, room_id, key) -> dict|None       # key = slug | root_id | путь
    load_report(cfg, path) -> {kind:'spec', spec, errors, warnings} | {kind:'html', url, ...}
    html_headers() -> dict                            # заголовки для HTML-отчёта
    serve_report_file(cfg, room, key, rel) -> (status, headers, body:bytes|Path)

Правила:
  * битый report.json не роняет список и не даёт 500 — ошибки отдаются в поле
    ``errors``, чтобы UI показал баннер;
  * HTML-отчёт открывается только через /reports/… с CSP sandbox;
  * статика отчёта (картинки, вложения) — только внутри папки отчёта,
    никаких ``..``/абсолютных путей/ADS.
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
from pathlib import Path
from urllib.parse import quote

try:  # import server.reports
    from .config import Cfg
    from .files import safe_resolve
    from .http_util import ApiError
except ImportError:  # python server/app.py
    from config import Cfg  # type: ignore
    from files import safe_resolve  # type: ignore
    from http_util import ApiError  # type: ignore

REPORT_JSON = "report.json"
REPORT_HTML = "report.html"
# D3: key отчёта — только слаг, без путей и «..»
KEY_RE = re.compile(r"^[A-Za-z0-9._-]{1,80}$")

# что можно отдать как статику отчёта
STATIC_MIME = {
    ".html": "text/html; charset=utf-8",
    ".htm": "text/html; charset=utf-8",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".svg": "image/svg+xml",
    ".pdf": "application/pdf",
    ".json": "application/json; charset=utf-8",
    ".csv": "text/csv; charset=utf-8",
    ".md": "text/plain; charset=utf-8",
    ".txt": "text/plain; charset=utf-8",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
}
HTML_MIME = "text/html; charset=utf-8"
SLUG_RE = re.compile(r"^r-\d{8}-\d{4}-[0-9a-z]+$")
TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>|<h1[^>]*>(.*?)</h1>", re.I | re.S)


# --- reportkit --------------------------------------------------------------
_RK = None


def _rk():
    """Импорт reportkit.validate (namespace-пакет) с запасным путём."""
    global _RK
    if _RK is None:
        root = Path(__file__).resolve().parent.parent
        if str(root) not in sys.path:
            sys.path.insert(0, str(root))
        try:
            from reportkit.validate import summarize, validate
        except Exception:  # pragma: no cover - reportkit всегда рядом
            validate = summarize = None
        _RK = (validate, summarize)
    return _RK


def _fallback_validate(spec):
    """Минимальная проверка, если reportkit недоступен."""
    errors, warnings = [], []
    if not isinstance(spec, dict):
        return {}, ["корень отчёта должен быть объектом {...}"], []
    out = {"version": 1, "title": str(spec.get("title") or ""), "blocks": []}
    if not out["title"]:
        errors.append("title: обязательное поле отсутствует")
    blocks = spec.get("blocks")
    if not isinstance(blocks, list) or not blocks:
        errors.append("blocks: нужен непустой список блоков")
    else:
        out["blocks"] = blocks
    for k in ("subtitle", "summary"):
        if isinstance(spec.get(k), str) and spec[k].strip():
            out[k] = spec[k].strip()
    return out, errors, warnings


def _validate(spec):
    validate, _ = _rk()
    if validate is None:
        return _fallback_validate(spec)
    try:
        return validate(spec)
    except Exception:  # валидатор не должен ронять запрос
        return _fallback_validate(spec)


def _summarize(spec) -> dict:
    _, summarize = _rk()
    if summarize is None:
        blocks = spec.get("blocks") if isinstance(spec, dict) else []
        return {"blocks": len(blocks or []), "rows": 0, "nodes": 0}
    try:
        return summarize(spec)
    except Exception:
        return {"blocks": 0, "rows": 0, "nodes": 0}


# --- внешние источники -------------------------------------------------------
def _ingest_state(cfg: Cfg, room_id: str) -> dict:
    """Состояние здания проекта (ingest) с запасным чтением state.json."""
    try:
        from . import ingest  # noqa: WPS433 — чужой модуль, пишется параллельно
    except Exception:
        ingest = None
    if ingest is not None:
        try:
            state = ingest.load_state(cfg, room_id)
            if isinstance(state, dict):
                return state
        except Exception:
            pass
    for name in ("state.json",):
        p = Path(cfg.data_dir) / "state" / room_id / name
        if p.is_file():
            try:
                data = json.loads(p.read_text(encoding="utf-8-sig"))
                if isinstance(data, dict):
                    return data
            except (OSError, ValueError):
                pass
    return {}


def _request_id_for_slug(cfg: Cfg, room_id: str, slug: str):
    """root_id по слагу запроса (реестр), если он есть."""
    if not SLUG_RE.match(slug or ""):
        return None
    try:
        from . import registry
    except Exception:
        registry = None
    if registry is not None:
        try:
            for req in registry.list_requests(cfg, room_id, 500) or []:
                if isinstance(req, dict) and req.get("slug") == slug:
                    return req.get("root_id")
        except Exception:
            pass
    p = Path(cfg.data_dir) / "state" / room_id / "requests.json"
    try:
        data = json.loads(p.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return None
    for req in data.get("requests") or []:
        if isinstance(req, dict) and req.get("slug") == slug:
            return req.get("root_id")
    return None


# --- индекс ------------------------------------------------------------------
def reports_root(cfg: Cfg) -> Path:
    return Path(cfg.data_dir) / "reports"


def _read_spec(path: Path):
    """(spec, errors) — ошибки чтения/JSON не бросаем."""
    try:
        raw = path.read_text(encoding="utf-8-sig")
    except OSError as e:
        return {}, ["файл: не удалось прочитать %s — %s" % (path.name, e.strerror or e)]
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        return {}, ["файл: некорректный JSON — строка %d, позиция %d: %s" % (e.lineno, e.colno, e.msg)]
    if not isinstance(data, dict):
        return {}, ["корень отчёта должен быть объектом {...}"]
    return data, []


def _html_title(path: Path):
    try:
        head = path.read_text(encoding="utf-8", errors="replace")[:20000]
    except OSError:
        return None
    m = TITLE_RE.search(head)
    if not m:
        return None
    raw = (m.group(1) or m.group(2) or "").strip()
    raw = re.sub(r"<[^>]+>", " ", raw)
    raw = re.sub(r"\s+", " ", raw).strip()
    return raw[:200] or None


def _stat_time(path: Path) -> tuple:
    try:
        st = path.stat()
        return int(st.st_ctime), int(st.st_mtime)
    except OSError:
        now = int(time.time())
        return now, now


def _key_for(path: Path, base: Path) -> str:
    """Слаг отчёта: папка внутри data/reports/<room>/ либо dir-<hash>."""
    try:
        rel = path.relative_to(base)
    except ValueError:
        rel = None
    if rel is not None and len(rel.parts) >= 3:
        return rel.parts[1]           # data/reports/<room>/<key>/report.json
    if rel is not None and len(rel.parts) == 2:
        return path.stem              # data/reports/<room>/report.json
    import hashlib
    h = hashlib.sha1(str(path.resolve()).encode("utf-8")).hexdigest()[:6]
    return "%s-%s" % (path.parent.name or "report", h)


def _item_for_path(cfg: Cfg, room_id: str, path: Path) -> dict | None:
    """ReportIndexItem по файлу отчёта; None, если файл не отчёт."""
    try:
        p = path.resolve(strict=True)
    except (OSError, ValueError):
        return None
    if not p.is_file():
        return None
    kind = "spec" if p.suffix.lower() == ".json" else "html" if p.suffix.lower() in (".html", ".htm") else None
    if kind is None:
        return None
    base = reports_root(cfg).resolve()
    key = _key_for(p, base)
    created, updated = _stat_time(p)
    title, subtitle, blocks = None, None, 0
    if kind == "spec":
        spec, _errors = _read_spec(p)
        if isinstance(spec, dict):
            title = str(spec.get("title") or "").strip() or None
            subtitle = str(spec.get("subtitle") or "").strip() or None
            blocks = _summarize(spec).get("blocks", 0)
    else:
        title = _html_title(p)
    item = {
        "key": key,
        "title": title or key,
        "subtitle": subtitle or "",
        "kind": kind,
        "path": str(p),
        "request_id": _request_id_for_slug(cfg, room_id, key),
        "blocks": int(blocks or 0),
        "created_at": created,
        "updated_at": updated,
        "exists": True,
    }
    return item


def list_reports(cfg: Cfg, room_id: str) -> list:
    """Все отчёты комнаты: сканирование data/reports/<room>/ + упоминания ingest."""
    room_id = str(room_id or "")
    base = reports_root(cfg) / room_id
    items, seen = [], set()

    def add(item):
        if not item:
            return
        real = item["path"]
        if real in seen:
            return
        seen.add(real)
        items.append(item)

    if base.is_dir():
        for dirpath, dirnames, filenames in os.walk(base):
            rel = Path(dirpath).relative_to(base).parts
            if len(rel) >= 3:  # глубже трёх уровней не ищем
                dirnames[:] = []
                continue
            dirnames.sort()
            for name in (REPORT_JSON, REPORT_HTML):
                if name in filenames:
                    add(_item_for_path(cfg, room_id, Path(dirpath) / name))
                    break

    for extra in _ingest_reports(cfg, room_id):
        add(extra)

    items.sort(key=lambda it: (it.get("updated_at") or 0, it.get("key") or ""), reverse=True)
    return items


def _ingest_reports(cfg: Cfg, room_id: str) -> list:
    """Отчёты, упомянутые в состоянии ingest (журнал/артефакты)."""
    state = _ingest_state(cfg, room_id)
    paths = []
    for entry in state.get("reports") or []:
        if isinstance(entry, dict):
            paths.append(str(entry.get("path") or ""))
        elif isinstance(entry, str):
            paths.append(entry)
    for entry in state.get("artifacts") or []:
        if isinstance(entry, dict):
            paths.append(str(entry.get("path") or ""))
    for ev in state.get("journal") or []:
        if not isinstance(ev, dict):
            continue
        detail = ev.get("detail") if isinstance(ev.get("detail"), dict) else {}
        for cand in (ev.get("path"), detail.get("path"), detail.get("report")):
            if isinstance(cand, str) and cand.lower().endswith((".json", ".html")):
                paths.append(cand)
    out = []
    for raw in paths:
        if not raw:
            continue
        p = Path(raw)
        if not p.is_absolute():
            p = Path(cfg.data_dir) / raw
        if p.name not in (REPORT_JSON, REPORT_HTML):
            # ingest может ссылаться на папку отчёта
            for name in (REPORT_JSON, REPORT_HTML):
                if (p / name).is_file():
                    p = p / name
                    break
            else:
                continue
        item = _item_for_path(cfg, room_id, p)
        if item and not _is_insane(cfg, item["path"]):
            out.append(item)
    return out


def _is_insane(cfg: Cfg, path: str) -> bool:
    """Отчёт вне разрешённых корней не показываем (кроме папки данных)."""
    try:
        p = Path(path).resolve()
    except (OSError, ValueError):
        return True
    for root in (reports_root(cfg).resolve(), Path(cfg.data_dir).resolve()):
        try:
            p.relative_to(root)
            return False
        except ValueError:
            continue
    return True


# --- поиск отчёта ------------------------------------------------------------
def key_ok(key) -> bool:
    """Слаг отчёта: ``[A-Za-z0-9._-]{1,80}``, без ``..`` и скрытых имён."""
    text = "" if key is None else str(key).strip()
    if not text or not KEY_RE.fullmatch(text):
        return False
    return ".." not in text and text.strip(".") != ""


def find_report(cfg: Cfg, room_id: str, key):
    """Найти отчёт по слагу или root_id запроса. None — нет; 400 — недопустимый key."""
    if key is None:
        return None
    text = str(key).strip()
    if not text:
        return None
    if not key_ok(text):
        # пути (в т.ч. абсолютные и `..`) и всё вне формы слага — 400, без чтения диска
        raise ApiError(400, "Недопустимый ключ отчёта: ожидается слаг [A-Za-z0-9._-]{1,80}",
                       detail={"key": text[:120]})

    items = list_reports(cfg, room_id)
    for it in items:
        if text in (it.get("key"), it.get("request_id")):
            return it
    low = text.lower()
    for it in items:
        title = (it.get("title") or "").lower()
        if low and (low in (it.get("key") or "").lower() or low == title):
            return it
    return None


# --- чтение отчёта -----------------------------------------------------------
def _url_for(cfg: Cfg, path: Path) -> str:
    """URL статики отчёта: /reports/<room>/<key>/<файл>."""
    p = path.resolve()
    base = reports_root(cfg).resolve()
    try:
        rel = p.relative_to(base)
    except ValueError:
        rel = None
    if rel is not None and len(rel.parts) >= 3:
        parts = [quote(x, safe="") for x in rel.parts]
        return "/reports/" + "/".join(parts)
    if rel is not None and len(rel.parts) == 2:
        return "/reports/%s/%s/%s" % (quote(rel.parts[0], safe=""), quote(p.stem, safe=""),
                                      quote(p.name, safe=""))
    room, key = _locate(cfg, p)
    if not room or not key:
        return ""
    return "/reports/%s/%s/%s" % (quote(room, safe=""), quote(key, safe=""), quote(p.name, safe=""))


def _locate(cfg: Cfg, path: Path):
    """(room, key) для файла отчёта: по индексу всех комнат."""
    target = str(path.resolve())
    roots = reports_root(cfg)
    if not roots.is_dir():
        return None, None
    for room_dir in sorted(roots.iterdir()):
        if not room_dir.is_dir():
            continue
        for it in list_reports(cfg, room_dir.name):
            if it.get("path") == target:
                return room_dir.name, it.get("key")
    return None, None


def _report_roots(cfg: Cfg) -> list:
    """Белые корни отчётов: ``data/reports``, ``data`` (относительные пути) и Vault."""
    out = []
    for root in (reports_root(cfg), Path(cfg.data_dir), Path(cfg.vault_dir)):
        try:
            resolved = Path(root).resolve()
        except (OSError, ValueError):
            continue
        if resolved.is_dir() and resolved not in out:
            out.append(resolved)
    return out


def report_path(cfg: Cfg, path) -> Path | None:
    """Разрешённый путь отчёта внутри белых корней (или None).

    Единственная точка чтения отчёта: сначала ``safe_resolve(root, …)`` для одного
    из корней, поэтому абсолютный путь вне ``data/reports``/Vault не читается.
    """
    raw = "" if path is None else str(path).strip()
    if not raw:
        return None
    for root in _report_roots(cfg):
        found = safe_resolve(root, raw)
        if found is not None:
            return found
    return None


def load_report(cfg: Cfg, path) -> dict:
    """Прочитать отчёт. Ошибки валидации не бросаем — отдаём в ``errors``."""
    if path is None:
        return {"kind": "spec", "spec": {}, "errors": ["путь к отчёту не указан"], "warnings": []}
    p = report_path(cfg, path)
    if p is None:
        shown = Path(str(path)).name or "report"
        return {"kind": "spec", "spec": {},
                "errors": ["файл: отчёт вне разрешённых каталогов или не найден — %s" % shown],
                "warnings": [], "path": str(path)}

    if p.suffix.lower() in (".html", ".htm"):
        url = _url_for(cfg, p)
        return {"kind": "html", "url": url, "title": _html_title(p) or p.parent.name,
                "path": str(p.resolve()), "errors": [], "warnings": []}

    spec, read_errors = _read_spec(p)
    if read_errors:
        return {"kind": "spec", "spec": {}, "errors": read_errors, "warnings": [],
                "path": str(p.resolve()), "title": p.parent.name}
    normalized, errors, warnings = _validate(spec)
    return {"kind": "spec", "spec": normalized, "errors": list(errors), "warnings": list(warnings),
            "path": str(p.resolve()), "title": (normalized or {}).get("title") or spec.get("title") or ""}


# --- отдача HTML-отчёта ------------------------------------------------------
def html_headers() -> dict:
    """Заголовки для HTML-отчёта: sandbox-iframe, без сети, без inline-угроз."""
    return {
        "Content-Type": HTML_MIME,
        "Content-Security-Policy": ("sandbox allow-scripts; default-src 'none'; "
                                    "img-src data: blob:; style-src 'unsafe-inline'; "
                                    "script-src 'unsafe-inline'"),
        "X-Content-Type-Options": "nosniff",
        "Referrer-Policy": "no-referrer",
        "Cache-Control": "no-cache",
        "X-Frame-Options": "SAMEORIGIN",
    }


def _err(status: int, message: str) -> tuple:
    body = (message + "\n").encode("utf-8")
    return status, {"Content-Type": "text/plain; charset=utf-8", "Content-Length": str(len(body)),
                    "X-Content-Type-Options": "nosniff", "Cache-Control": "no-store"}, body


def _safe_seg(seg: str) -> bool:
    """Один сегмент пути: без .., слэшей, ADS, NUL."""
    return bool(seg) and seg not in (".", "..") and not any(c in seg for c in '/\\:*?"<>|\x00')


def _inside(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except (ValueError, OSError):
        return False


def serve_report_file(cfg: Cfg, room: str, key: str, rel: str) -> tuple:
    """Отдать файл из папки отчёта (html — с CSP, картинки — как картинки)."""
    room, key = str(room or ""), str(key or "")
    rel = str(rel or "").replace("\\", "/").strip().lstrip("/")
    if not _safe_seg(room) or not _safe_seg(key) or not key_ok(key):
        return _err(403, "Недопустимый путь отчёта.")
    if not rel:
        rel = REPORT_JSON
    segs = [s for s in rel.split("/") if s]
    if not segs or any(not _safe_seg(s) for s in segs):
        return _err(403, "Путь внутри отчёта содержит запрещённые элементы.")
    if len(rel) > 1 and rel[1] == ":":
        return _err(403, "Абсолютные пути в отчёте запрещены.")

    room_root = reports_root(cfg) / room / key
    cand = safe_resolve(room_root, "/".join(segs)) if room_root.is_dir() else None
    if cand is not None:
        return _serve(cand)
    raw_cand = room_root.joinpath(*segs)
    if raw_cand.is_file():
        # файл есть, но запрещён (тип/имя/выход за папку отчёта) — это 403, не «не найден»
        return _err(403, "Файл отчёта запрещён к показу: тип или имя вне белого списка.")
    # отчёт может лежать глубже (упомянут в ingest) — ищем по индексу
    for it in list_reports(cfg, room):
        if it.get("key") != key:
            continue
        base = Path(it["path"]).parent
        cand = safe_resolve(base, "/".join(segs))
        if cand is not None:
            return _serve(cand)
        if base.joinpath(*segs).is_file():
            return _err(403, "Файл отчёта запрещён к показу: тип или имя вне белого списка.")
    if (room_root / REPORT_JSON).exists() or room_root.is_dir():
        return _err(404, "Файл отчёта не найден.")
    return _err(404, "Отчёт не найден.")


def _serve(path: Path) -> tuple:
    ext = path.suffix.lower()
    ctype = STATIC_MIME.get(ext)
    if ctype is None:
        return _err(403, "Тип файла не разрешён к показу.")
    try:
        size = path.stat().st_size
    except OSError:
        return _err(404, "Файл отчёта не найден.")
    if ext in (".html", ".htm"):
        headers = html_headers()
    else:
        headers = {"Content-Type": ctype, "X-Content-Type-Options": "nosniff",
                   "Referrer-Policy": "no-referrer", "Cache-Control": "no-cache"}
        if ext == ".svg":
            headers["Content-Security-Policy"] = "sandbox; default-src 'none'; style-src 'unsafe-inline'"
    headers["Content-Length"] = str(size)
    return 200, headers, path
