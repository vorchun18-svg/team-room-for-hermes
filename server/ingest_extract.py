"""Ingest: из `RequestDetail` в каноническую metadata версии ответа (шаги, вопросы, решения, фазы).

Нормализация идёт через `server.normalize` (единственная реализация, ARCH §12.8);
`version_key`/`_run_id` дают идемпотентный ключ версии ответа.
"""
from __future__ import annotations

from pathlib import Path

from .ingest_common import IngestError, PHASES, PHASE_IDS, PHASE_STATUSES, _as_int, _clean, _sha

def _normalizer():
    """server.normalize.normalize_metadata — единственная каноническая реализация (ARCH §12.8)."""
    try:
        from .normalize import normalize_metadata  # noqa: WPS433 (поздний импорт: модуль пишется параллельно)
        return normalize_metadata
    except ImportError:
        return None


def _needs_normalize(detail: dict) -> bool:
    steps = detail.get("next_steps")
    if isinstance(steps, list) and any(isinstance(s, str) for s in steps):
        return True
    questions = detail.get("open_questions")
    if isinstance(questions, list) and any(isinstance(q, str) for q in questions):
        return True
    stage = detail.get("stage")
    if isinstance(stage, dict):
        keys = list(stage.keys())
        if "phases" not in keys and any(k not in PHASE_IDS for k in keys):
            return True
        if keys and "phases" not in keys:
            return True
    return bool(detail.get("legacy"))


def _meta_from_detail(detail: dict) -> dict:
    return {
        "next_steps": detail.get("next_steps"),
        "open_questions": detail.get("open_questions"),
        "decisions": detail.get("decisions"),
        "stage": detail.get("stage"),
        "future_questions": detail.get("future_questions"),
        "artifacts": detail.get("artifacts"),
        "report": detail.get("report"),
    }


def _canonical(detail: dict) -> dict:
    """Вернуть detail с нормализованными полями (legacy → канон через server.normalize)."""
    if not _needs_normalize(detail):
        return detail
    normalize_metadata = _normalizer()
    if normalize_metadata is None:
        raise IngestError(
            "Legacy-metadata требует server/normalize.py, но модуль ещё не готов",
            "normalizer_missing", {"file": "server/normalize.py"},
        )
    summary = ""
    synthesis = detail.get("synthesis") or {}
    if isinstance(synthesis, dict):
        summary = str(synthesis.get("text_md") or "")
    summary = summary or str(detail.get("text_md") or "") or str(detail.get("summary") or "")
    normalized = normalize_metadata(_meta_from_detail(detail), summary) or {}
    merged = dict(detail)
    for key in ("next_steps", "open_questions", "decisions", "stage", "future_questions", "artifacts", "report", "warnings"):
        if key in normalized and normalized[key] is not None:
            merged[key] = normalized[key]
    return merged


def _extract(detail: dict) -> dict:
    """Канонические поля detail → плоские списки с локальными id (ns_1, q_1, d_1, fq_1)."""
    d = _canonical(detail)
    out = {"next_steps": [], "open_questions": [], "decisions": [], "future_questions": [],
           "phases": [], "artifacts": [], "report": None}

    for idx, raw in enumerate(d.get("next_steps") or [], start=1):
        if isinstance(raw, str):
            raw = {"text": raw}
        if not isinstance(raw, dict) or not _clean(raw.get("text")):
            continue
        out["next_steps"].append({
            "id": str(raw.get("id") or f"ns_{idx}"),
            "text": _clean(raw.get("text")),
            "owner": (str(raw["owner"]).strip() or None) if raw.get("owner") else None,
            "due": _clean(raw.get("due")) or None,
            "state": str(raw.get("state") or "open"),
        })

    for idx, raw in enumerate(d.get("open_questions") or [], start=1):
        if isinstance(raw, str):
            raw = {"text": raw, "for": "mikhail"}
        if not isinstance(raw, dict) or not _clean(raw.get("text")):
            continue
        out["open_questions"].append({
            "id": str(raw.get("id") or f"q_{idx}"),
            "text": _clean(raw.get("text")),
            "for": str(raw.get("for") or "mikhail"),
            "answered": bool(raw.get("answered")),
            "answer": raw.get("answer"),
            "answered_at": _as_int(raw.get("answered_at")) or None,
        })

    for idx, raw in enumerate(d.get("decisions") or [], start=1):
        if isinstance(raw, str):
            raw = {"text": raw}
        if not isinstance(raw, dict) or not _clean(raw.get("text")):
            continue
        status = str(raw.get("status") or "proposed")
        out["decisions"].append({
            "id": str(raw.get("id") or f"d_{idx}"),
            "text": _clean(raw.get("text")),
            "why": _clean(raw.get("why")) or None,
            "status": status if status in ("proposed", "accepted", "rejected") else "proposed",
            "owner": (str(raw.get("owner")).strip() if raw.get("owner") else "mikhail"),
        })

    seen_fq = set()
    for idx, raw in enumerate(d.get("future_questions") or [], start=1):
        text = _clean(raw.get("text") if isinstance(raw, dict) else raw)
        if not text or text in seen_fq:
            continue
        seen_fq.add(text)
        out["future_questions"].append({"id": f"fq_{idx}", "text": text})

    stage = d.get("stage") or {}
    phases_raw = stage.get("phases") if isinstance(stage, dict) else None
    if isinstance(phases_raw, dict):  # «id фазы → статус» без нормализатора
        phases_raw = [{"id": k, "status": v} for k, v in phases_raw.items()]
    index = {p["id"]: p for p in phases_raw or [] if isinstance(p, dict) and p.get("id") in PHASE_IDS}
    for phase_id, title in PHASES:
        if phase_id in index:
            raw = index[phase_id]
            status = str(raw.get("status") or "next")
            out["phases"].append({
                "id": phase_id,
                "title": _clean(raw.get("title")) or title,
                "status": status if status in PHASE_STATUSES else "next",
                "note": _clean(raw.get("note")),
            })

    for raw in d.get("artifacts") or []:
        if isinstance(raw, str):
            raw = {"path": raw}
        if isinstance(raw, dict) and raw.get("path"):
            out["artifacts"].append({"path": str(raw["path"]), "title": _clean(raw.get("title")),
                                     "kind": str(raw.get("kind") or _artifact_kind(raw["path"]))})
    report = d.get("report")
    if isinstance(report, str):
        report = {"path": report}
    if isinstance(report, dict) and report.get("path"):
        kind = str(report.get("kind") or ("html" if str(report["path"]).lower().endswith(".html") else "spec"))
        out["report"] = {"path": str(report["path"]), "title": _clean(report.get("title")) or None, "kind": kind}
    return out


def _artifact_kind(path: str) -> str:
    ext = Path(str(path)).suffix.lower().lstrip(".")
    return {"md": "md", "json": "json", "html": "html", "htm": "html", "csv": "csv", "png": "image",
            "jpg": "image", "jpeg": "image", "gif": "image", "webp": "image", "pdf": "pdf",
            "docx": "docx", "xlsx": "xlsx", "pptx": "pptx"}.get(ext, "other")


def _run_id(detail: dict, title: str) -> str:
    if detail.get("run_id"):
        return str(detail["run_id"])
    synthesis = detail.get("synthesis") or {}
    if isinstance(synthesis, dict) and synthesis.get("run_id"):
        return str(synthesis["run_id"])
    source = str((synthesis or {}).get("source_task") or detail.get("source_task") or detail.get("id") or "")
    at = _as_int((synthesis or {}).get("at") or detail.get("finished_at"))
    text = str((synthesis or {}).get("text_md") or detail.get("summary") or "")
    return f"{source}@{at}#{_sha(text + '|' + title)[:8]}"


def version_key(detail: dict) -> str:
    """Ключ версии ответа (run_id последней версии) — единый для watcher и ingest."""
    detail = detail if isinstance(detail, dict) else {}
    title = _clean(detail.get("title")) or str(detail.get("id") or detail.get("root_id") or "")
    return _run_id(detail, title)
