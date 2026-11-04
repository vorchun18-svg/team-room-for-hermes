"""Метаданные ответа → объекты API (RequestDetail/RequestSummary) — чистые функции.

Второй слой чтения: здесь только преобразование уже канонической metadata
(``server.normalize.normalize_metadata``) в «плоские» объекты контракта —
шаги, вопросы, решения, фазы, артефакты, отчёт, прогресс, блок критика.

Никакого sqlite, сети и состояния: всё принимает аргументы и возвращает
новые dict'ы. Поэтому модуль легко тестировать и он не тянет за собой
``engine_read`` (обратной зависимости нет).
"""
from __future__ import annotations

import re
from pathlib import Path

try:  # пакетный и «плоский» импорт (python -m unittest из корня проекта)
    from .config import path_allowed
except ImportError:  # pragma: no cover
    from config import path_allowed  # type: ignore

PREVIEW_MAX = 200
PHASES = [("idea", "Идея и стратегия"), ("legal", "Легализация"), ("brand", "Бренд и товарный знак"),
          ("packaging", "Упаковка"), ("production", "Производство"), ("sales", "Продажи")]
PHASE_IDS = [p[0] for p in PHASES]
CRITIC_STATES = ("off", "waiting", "reviewing", "approved", "revised", "failed")
CLOSED = ("done", "archived")
ACTIVE = ("ready", "running", "review")


def as_list(value) -> list:
    """Значение → список: None/'' → [], скаляр → [значение]."""
    if value is None or value == "":
        return []
    return value if isinstance(value, list) else [value]


# ------------------------------------------------------------------ поля ответа ---
def stage(canon: dict) -> dict:
    """Канонический stage → Stage API §3.4: всегда 6 фаз в фиксированном порядке."""
    raw = canon.get("stage") if isinstance(canon.get("stage"), dict) else {}
    got = {p.get("id"): p for p in as_list(raw.get("phases"))
           if isinstance(p, dict) and p.get("id") in PHASE_IDS}
    phases = []
    for pid, title in PHASES:
        p = got.get(pid) or {}
        status = p.get("status") if p.get("status") in ("done", "active", "next", "blocked") else "next"
        phases.append({"id": pid, "title": p.get("title") or title, "status": status,
                       "note": p.get("note") or ""})
    current = raw.get("current")
    if current not in PHASE_IDS:
        active = [p["id"] for p in phases if p["status"] in ("active", "blocked")]
        done = [p["id"] for p in phases if p["status"] == "done"]
        nxt = [p["id"] for p in phases if p["status"] == "next"]
        current = active[0] if active else (done[-1] if done else (nxt[0] if nxt else None))
    return {"current": current, "phases": phases}


def steps(canon: dict) -> list:
    out = []
    for i, item in enumerate(as_list(canon.get("next_steps")), 1):
        item = {"text": item} if isinstance(item, str) else item
        if isinstance(item, dict) and str(item.get("text") or "").strip():
            out.append({"id": item.get("id") or f"ns_{i}", "text": str(item["text"]).strip(),
                        "owner": item.get("owner"), "due": item.get("due"),
                        "state": item.get("state") or "open"})
    return out


def questions(canon: dict) -> list:
    out = []
    for i, item in enumerate(as_list(canon.get("open_questions")), 1):
        item = {"text": item, "for": "mikhail"} if isinstance(item, str) else item
        if isinstance(item, dict) and str(item.get("text") or "").strip():
            out.append({"id": item.get("id") or f"q_{i}", "text": str(item["text"]).strip(),
                        "for": item.get("for") or "mikhail", "answered": bool(item.get("answered")),
                        "answer": item.get("answer"), "answered_at": item.get("answered_at")})
    return out


def decisions(canon: dict, source: str, at) -> list:
    out = []
    for i, item in enumerate(as_list(canon.get("decisions")), 1):
        item = {"text": item} if isinstance(item, str) else item
        if not isinstance(item, dict) or not str(item.get("text") or "").strip():
            continue
        status = item.get("status") if item.get("status") in ("proposed", "accepted", "rejected") else "proposed"
        out.append({"id": item.get("id") or f"d_{i}", "text": str(item["text"]).strip(),
                    "why": item.get("why"), "status": status, "at": item.get("at") or at,
                    "source_request": source})
    return out


def future(canon: dict) -> list:
    return [str(x).strip() for x in as_list(canon.get("future_questions")) if str(x).strip()]


def all_questions(canon: dict, canons: list) -> list:
    """Открытые вопросы запроса: из ответа + из карточек-детей (id уникальны)."""
    out, seen_ids, seen_text = [], set(), set()

    def add(items):
        for q in items:
            text = (q.get("text") or "").strip()
            if not text or text in seen_text:
                continue
            seen_text.add(text)
            qid = str(q.get("id") or f"q_{len(out) + 1}")
            while qid in seen_ids:
                qid += "_2"
            seen_ids.add(qid)
            out.append(dict(q, id=qid))

    add(questions(canon))
    for node, node_canon in canons:
        if node["role"] in ("root", "synthesizer"):
            continue
        add(questions(node_canon))
    return out


def stage_filled(canon: dict) -> bool:
    st = canon.get("stage")
    return isinstance(st, dict) and any(p.get("status") in ("done", "active", "blocked")
                                        for p in as_list(st.get("phases")) if isinstance(p, dict))


def merge_canon(primary: dict, canons: list) -> dict:
    """Заполнить пустые разделы ответа из metadata корня/синтезатора/детей."""
    out = dict(primary)
    for key in ("next_steps", "open_questions", "decisions", "stage", "future_questions",
                "artifacts", "report"):
        if out.get(key) and (key != "stage" or stage_filled(out)):
            continue
        for _, node_canon in canons:
            if key == "stage":
                if stage_filled(node_canon):
                    out[key] = node_canon["stage"]
                    break
            elif node_canon.get(key):
                out[key] = node_canon[key]
                break
    return out


def kind_of(path: str) -> str:
    ext = Path(path).suffix.lower().lstrip(".")
    return {"md": "md", "json": "json", "html": "html", "htm": "html", "csv": "csv", "pdf": "pdf",
            "docx": "docx", "xlsx": "xlsx", "pptx": "pptx", "png": "image", "jpg": "image",
            "jpeg": "image", "gif": "image", "webp": "image", "svg": "image"}.get(ext, "other")


def artifacts(cfg, canon: dict, warnings: list) -> list:
    """Artifact[] с проверкой существования по белому списку config (иначе warning)."""
    out, seen = [], set()
    for item in as_list(canon.get("artifacts")):
        item = {"path": item} if isinstance(item, str) else item
        if not isinstance(item, dict) or not item.get("path"):
            continue
        path = str(item["path"]).replace("\\", "/")
        if path in seen:
            continue
        seen.add(path)
        real = path_allowed(cfg, path)  # вне корней → exists=False без ошибки
        out.append({"path": path, "title": item.get("title") or Path(path).name,
                    "kind": item.get("kind") or kind_of(path), "exists": bool(real),
                    "size": real.stat().st_size if real else None})
        if not real:
            warnings.append({"code": "missing_artifact",
                             "message": f"Артефакт недоступен: {Path(path).name}",
                             "detail": {"path": path}})
    return out


def report(cfg, canon: dict) -> dict:
    raw = canon.get("report")
    path = raw.get("path") if isinstance(raw, dict) else raw
    title = raw.get("title") if isinstance(raw, dict) else None
    if not path:
        return {"available": False, "kind": None, "path": None, "title": None}
    path = str(path).replace("\\", "/")
    real = path_allowed(cfg, path)
    if not real:
        return {"available": False, "kind": None, "path": path, "title": title}
    return {"available": True,
            "kind": "html" if Path(path).suffix.lower() in (".html", ".htm") else "spec",
            "path": str(real), "title": title or Path(path).stem}


def progress(nodes: list, status: str) -> dict:
    children = nodes[1:]
    total, done = len(children), sum(1 for n in children if n.get("status") in CLOSED)
    percent = 100 if status == "done" else (0 if total == 0 else int(100 * done / total))
    running = sorted({n["assignee"] for n in children
                      if n.get("status") in ACTIVE and n.get("assignee")})
    return {"done": done, "total": total, "percent": percent, "running": running}


def preview(text) -> str:
    if not text:
        return ""
    return re.sub(r"\s+", " ", re.sub(r"[#*_`>~\[\]()]+", " ", str(text))).strip()[:PREVIEW_MAX]


def critic_brief(critic) -> dict:
    """Сжатый блок критика для RequestSummary: {enabled, state, verdict, round}."""
    critic = critic if isinstance(critic, dict) else {}
    state = str(critic.get("state") or "off")
    verdict = critic.get("verdict")
    return {"enabled": bool(critic.get("enabled")), "state": state if state in CRITIC_STATES else "off",
            "verdict": verdict if verdict in ("approve", "revise") else None,
            "round": int(critic.get("round") or 0)}


__all__ = ["as_list", "stage", "steps", "questions", "decisions", "future", "all_questions",
           "stage_filled", "merge_canon", "kind_of", "artifacts", "report", "progress",
           "preview", "critic_brief", "PHASES", "PHASE_IDS", "CRITIC_STATES", "PREVIEW_MAX"]
