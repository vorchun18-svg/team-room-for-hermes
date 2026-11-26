# -*- coding: utf-8 -*-
"""W30-E — демо «Результаты агентов» (страница задачи, порты 8860–8861).

Три вещи, которых нет в общем моке:

1. ``GET /api/requests/{id}/agent-results`` — то же, что отдаёт боевой сервер
   (сборка — настоящий ``server/agent_results.py``: демо показывает ровно ту
   форму, что придёт с сервера, а не выдуманную);
2. ключ ``agent_results`` в детали задачи (``attach`` вызывается из
   ``mock_read.request_detail`` — одна строка подключения);
3. ``GET /api/attach/preview`` — крошечный текстовый предпросмотр, чтобы кнопка
   «Просмотр» в демо что-то показывала (в общем моке ручки нет; в демо честно
   сказано «в демо показываем только текст»).

Файлы результатов: фикстуры ссылаются на вымышленные пути вне диска, поэтому в
демо они переписываются на настоящие маленькие файлы в ``data/demo-results/`` —
тогда «Открыть», «Открыть в папке» и «Просмотр» работают по-настоящему. Один
файл НЕ создаётся: чтобы было видно состояние «файла нет на месте».

Данные — только вымышленные (химчистка, кофейня, ларёк), модели не вызываются.
"""
from __future__ import annotations

import os
import re

from mock_core import DATA_DIR, ApiError

DEMO_DIR = os.path.join(DATA_DIR, "demo-results")
#: Сколько файлов результата готовим на одного исполнителя (демо).
MAX_DEMO_FILES = 3
#: Кому НЕ создаём файл: показывает подпись «файла нет на месте».
SKIP_AGENT = "team-ops"

_PREVIEW_MAX = 20000


def _safe_name(name: str) -> str:
    value = re.sub(r"[^0-9A-Za-zА-Яа-яЁё._ -]+", "_", str(name or "")).strip(" .")
    return value[:80] or "file.md"


def _demo_text(agent: str, name: str) -> str:
    """Нейтральный текст файла-результата (вымышленные данные)."""
    return (
        "# Результат работы: %s\n\n"
        "Файл демонстрационный — вымышленные данные для проверки интерфейса.\n\n"
        "## Вывод\n\n"
        "Проверено 3 позиции, две подходят, третья ждёт цены поставщика.\n\n"
        "## Что внутри\n\n"
        "- Шаги проверки и даты;\n"
        "- таблица с ценами;\n"
        "- что осталось сделать.\n\n"
        "Исполнитель: %s. Файл: %s.\n" % (name, agent or "агент", name)
    )


def _agent_dir(agent: str) -> str:
    return os.path.join(DEMO_DIR, _safe_name(agent or "agent"))


def _ensure_file(agent: str, name: str) -> str:
    """Создать демо-файл (если его нет) и вернуть полный путь."""
    folder = _agent_dir(agent)
    path = os.path.join(folder, _safe_name(name))
    if agent == SKIP_AGENT:                 # нарочно отсутствующий файл
        return path
    try:
        if not os.path.isfile(path):
            os.makedirs(folder, exist_ok=True)
            with open(path, "w", encoding="utf-8") as handle:
                handle.write(_demo_text(agent, os.path.basename(path)))
    except OSError:                         # демо без диска живёт и так
        pass
    return path


def _rewrite_files(node: dict) -> None:
    """Пути артефактов фикстуры → настоящие демо-файлы (для «Открыть»/«Просмотр»)."""
    meta = node.get("metadata")
    if not isinstance(meta, dict):
        return
    agent = str(node.get("assignee") or "")
    for key in ("artifacts", "artifact", "files", "changed_files"):
        raw = meta.get(key)
        if not raw:
            continue
        items = raw if isinstance(raw, list) else [raw]
        paths = []
        for index, item in enumerate(items[:MAX_DEMO_FILES]):
            if isinstance(item, dict):
                name = str(item.get("path") or item.get("title") or "")
            else:
                name = str(item or "")
            name = os.path.basename(name.replace("\\", "/")) if name else ""
            if not name and isinstance(item, dict):
                name = str(item.get("title") or "") or "result.md"
            if not name:
                continue
            if "." not in name:
                name += ".md"
            paths.append(_ensure_file(agent, "%d-%s" % (index + 1, name) if index else name))
        if paths:
            meta[key] = paths


def _build(detail: dict) -> dict:
    """Собрать блок настоящим серверным модулем (та же форма, что на боевом)."""
    try:
        import sys
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        if root not in sys.path:            # мок запускают из tools/, пакета «server» нет
            sys.path.insert(0, root)
        from server import agent_results as module
    except Exception:                       # noqa: BLE001 — демо живёт без сервера
        return {"agents": [], "counts": {}}
    payload = module.build((detail or {}).get("tree"), room=_room_of(detail))
    payload.pop("generated_at", None)
    return payload


def _room_of(detail: dict) -> dict:
    return {"id": str((detail or {}).get("room") or ""),
            "role_labels": {"team-director": "руководитель", "team-critic": "критик"}}


def attach(server, detail: dict) -> dict:
    """Переписать файлы на демо-файлы и положить ``agent_results`` в деталь."""
    if not isinstance(detail, dict):
        return detail
    for node in detail.get("tree") or []:
        if isinstance(node, dict):
            _rewrite_files(node)
    detail["agent_results"] = _build(detail)
    return detail


# --- ручки -----------------------------------------------------------------

def handle(server, method: str, path: str, query: dict, body: dict):
    """Свои GET-ручки демо. ``None`` — не наша, пусть отвечает общий мок."""
    if method != "GET":
        return None
    match = re.fullmatch(r"/api/requests/(t_[0-9a-f]{8})/agent-results", path)
    if match:
        detail = server.detail(match.group(1))
        attach(server, detail)
        payload = detail.get("agent_results") or {"agents": [], "counts": {}}
        return 200, payload
    if path == "/api/attach/preview":
        return _preview(query)
    return None


def _preview(query: dict):
    """Текстовый предпросмотр демо-файла (картинки и PDF в демо не разбираем)."""
    raw = (query.get("path") or [""])[0].strip()
    if not raw:
        raise ApiError(400, "bad_request", "Нужен параметр path", {"field": "path"})
    path = os.path.abspath(raw)
    if not os.path.isfile(path):
        raise ApiError(404, "not_found", "Файла нет на месте",
                       {"code": "missing", "path": raw})
    if os.path.commonpath([path, os.path.abspath(DEMO_DIR)]) != os.path.abspath(DEMO_DIR):
        raise ApiError(403, "forbidden", "В демо предпросмотр есть только у файлов результатов",
                       {"code": "outside", "path": raw})
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            text = handle.read(_PREVIEW_MAX)
    except OSError as exc:
        raise ApiError(500, "read_failed", "Не удалось прочитать файл",
                       {"path": raw, "error": str(exc)[:200]}) from exc
    return 200, {"kind": "text", "name": os.path.basename(path), "path": path,
                 "bytes": os.path.getsize(path), "text": text,
                 "note": "Демо: показываем текст файла. Другие типы — на боевом сервере.",
                 "can": {"ocr": False, "transcribe": False}}
