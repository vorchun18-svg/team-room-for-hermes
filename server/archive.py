"""Архив запросов комнаты (W29-P4): «не актуально → в архив», возврат, остановка.

Один источник истины — доска: запрос «в архиве», если корневая карточка имеет
статус ``archived`` (так его и читает ``engine_read``). Этот модуль:
* архивирует запрос штатным глаголом канбана (``engine_write.act('archive')``) —
  Hermes сам завершает воркер идущей карточки (``kanban_db.archive_task``),
  поэтому «идущие карточки останавливаются штатно» без второго механизма;
* ДО архивации запоминает прежние статусы карточек в реестре комнаты
  (``archive_prev``), чтобы «Вернуть из архива» восстановило состояние;
* возвращает запрос: завершённые карточки → ``done``, незавершённые →
  ``blocked`` (работа приостановлена; модели не запускаются — поднимает человек
  кнопкой «Продолжить»). Штатного «unarchive» у Hermes нет, поэтому возврат идёт
  через ``kanban_atomic.restore`` — функции самой библиотеки канбана в отдельном
  процессе, как ``stop``/``resume`` (W23-AT1), а не прямой записью в БД из сервера.

Висящие пункты запроса (вопросы, решения, шаги, разрешения) закрываются и
возвращаются сами: ``stale.decorate`` считает их закрытыми у архивного запроса с
причиной ``request_archived`` и снова открывает после возврата. Второго списка
«что было закрыто» нет — данные карточек не переписываются.
"""
from __future__ import annotations

import time

try:  # пакетный и «плоский» импорт (тесты из корня проекта)
    from . import engine_read as _read
    from . import engine_write as _write
    from . import registry as _registry
except ImportError:  # pragma: no cover
    import engine_read as _read        # type: ignore
    import engine_write as _write      # type: ignore
    import registry as _registry       # type: ignore

#: Причина закрытия пунктов у архивного запроса (контракт §1, `stale.REASON_RU`).
REASON_ARCHIVED = "request_archived"
#: Статусы, которые карточка получает при возврате из архива.
RESTORE_DONE = "done"
RESTORE_PAUSED = "blocked"
#: Глубина архива: сколько записей реестра смотреть в списке (архив комнаты целиком).
ARCHIVE_LIMIT = 500


#: Через сколько секунд после завершения запрос «пора в архив» (W29-P4, мягкая подсказка).
SUGGEST_AFTER_S = 14 * 86400
#: Автоархив по умолчанию ВЫКЛЮЧЕН: подсказка — кнопка, а не действие без человека.
AUTO_DEFAULT = False
#: Больше этого числа подсказок за раз не показываем (иначе простыня вместо «пора в архив»).
SUGGEST_LIMIT = 5


def auto_enabled(room) -> bool:
    """Включён ли автоархив комнаты (по умолчанию — нет, WAVE29 §P4)."""
    value = (room or {}).get("archive_auto") if isinstance(room, dict) else None
    if value is None:
        return AUTO_DEFAULT
    return bool(value)


def suggestions(records, states, *, now=None, limit: int = SUGGEST_LIMIT) -> dict:
    """«Пора в архив»: запрос завершён ≥ 14 дней назад и с тех пор молчит.

    Ничего не пишет и не архивирует само: это список для мягкой подсказки с кнопкой
    «В архив». Автоархив по умолчанию выключен (`AUTO_DEFAULT`), поэтому тихой уборки
    без человека быть не может.
    """
    stamp = int(now or time.time())
    items: list[dict] = []
    for record in records or []:
        if not isinstance(record, dict):
            continue
        root_id = str(record.get("root_id") or "")
        if not root_id or record.get("archived_at"):
            continue
        state = (states or {}).get(root_id) or {}
        if str(state.get("status") or "") != "done":
            continue
        finished = int(state.get("finished_at") or record.get("finished_at") or 0)
        if not finished or stamp - finished < SUGGEST_AFTER_S:
            continue
        items.append({"root_id": root_id, "title": str(record.get("title") or ""),
                      "finished_at": finished,
                      "days": int((stamp - finished) // 86400)})
    items.sort(key=lambda item: item["finished_at"])
    return {"items": items[:max(1, int(limit))], "total": len(items),
            "after_days": SUGGEST_AFTER_S // 86400, "auto": AUTO_DEFAULT}


def archive_info(record: dict) -> dict:
    """Служебные поля архива из записи реестра (для ответа API)."""
    record = record if isinstance(record, dict) else {}
    prev = record.get("archive_prev") if isinstance(record.get("archive_prev"), dict) else {}
    return {"archived_at": int(record.get("archived_at") or 0),
            "archived_by": str(record.get("archived_by") or ""),
            "restored_at": int(record.get("restored_at") or 0),
            "prev": {str(k): str(v) for k, v in prev.items() if k and v}}


def _db_statuses(cfg, board: str, ids) -> dict:
    """Статусы карточек одним чтением (``engine_read.request_states``)."""
    states = _read.request_states(cfg, board, list(ids)) or {}
    return {str(k): str((v or {}).get("status") or "") for k, v in states.items()}


def _patch_record(cfg, room_id: str, root_id: str, record: dict, patch: dict) -> None:
    """Записать служебные поля архива в реестр комнаты (чужой код читает их как есть).

    Записи может не быть (карточку привязали к комнате, а реестр её не знает) —
    тогда создаём её: без `archive_prev` возврат из архива потерял бы прежние
    статусы карточек, а это обязательная часть «вернуть как было».
    """
    if not room_id or not root_id:
        return
    try:
        updated = _registry.update(cfg, str(room_id), str(root_id), dict(patch))
        if updated is not None:
            return
        base = {k: v for k, v in (record or {}).items() if k != "root_id"}
        base.update(patch)
        base["root_id"] = str(root_id)
        _registry.add_request(cfg, str(room_id), base)
    except Exception:  # noqa: BLE001 — реестр вторичен: архив уже состоялся
        pass


def archive_request(cfg, room: dict, record: dict, root_id: str, *, by: str = "",
                    reason: str = "") -> dict:
    """Убрать запрос в архив. Возвращает ``{archived, count, already, prev, at}``."""
    room = room if isinstance(room, dict) else {}
    record = record if isinstance(record, dict) else {}
    board = str(room.get("board") or getattr(cfg, "board", "") or "team")
    root_id = str(root_id or record.get("root_id") or "")
    if not root_id:
        raise _write.EngineWriteError("bad_request", "Не передан запрос для архивации")

    ids = sorted(_write.tree_ids(cfg, board, root_id))
    before = _db_statuses(cfg, board, ids)
    if before.get(root_id) == "archived":
        return {"archived": [], "count": 0, "already": True, "prev": {}, "at": 0,
                "message": "Запрос уже в архиве"}
    prev = {tid: before[tid] for tid in ids if before.get(tid) and before[tid] != "archived"}
    # Штатный глагол: Hermes архивирует корень и детей и сам завершает воркер.
    _write.act(cfg, board, root_id, "archive")
    after = _db_statuses(cfg, board, ids)
    archived = sorted(tid for tid in ids if after.get(tid) == "archived")
    at = int(time.time())
    if archived:
        _patch_record(cfg, room.get("id"), root_id, record,
                      {"archived_at": at, "archived_by": str(by or ""),
                       "archive_prev": prev, "archive_reason": str(reason or ""),
                       "restored_at": 0})
    return {"archived": archived, "count": len(archived), "already": False,
            "prev": prev, "at": at}


def restore_request(cfg, room: dict, record: dict, root_id: str, *, by: str = "") -> dict:
    """Вернуть запрос из архива. Возвращает ``{restored, count, paused, at, message}``."""
    room = room if isinstance(room, dict) else {}
    record = record if isinstance(record, dict) else {}
    board = str(room.get("board") or getattr(cfg, "board", "") or "team")
    root_id = str(root_id or record.get("root_id") or "")
    if not root_id:
        raise _write.EngineWriteError("bad_request", "Не передан запрос для возврата")

    ids = sorted(_write.tree_ids(cfg, board, root_id))
    states = _read.request_states(cfg, board, ids) or {}
    archived = [tid for tid in ids if str((states.get(tid) or {}).get("status") or "") == "archived"]
    if not archived:
        return {"restored": [], "count": 0, "paused": [], "at": 0,
                "message": "Запрос не в архиве"}
    prev = record.get("archive_prev") if isinstance(record.get("archive_prev"), dict) else {}
    items: list = []
    paused: list = []
    for tid in archived:
        was = str(prev.get(tid) or "")
        if was == "archived":
            continue                      # карточка была в архиве до этого действия
        if not was:
            # Запись архивировали до W29 (прежний статус не сохранён): завершённая
            # карточка узнаётся по `completed_at`, остальные — «приостановлено».
            finished = (states.get(tid) or {}).get("finished_at")
            was = RESTORE_DONE if finished else RESTORE_PAUSED
        status = RESTORE_DONE if was == "done" else RESTORE_PAUSED
        items.append({"task_id": tid, "status": status})
        if status != RESTORE_DONE:
            paused.append(tid)
    if not items:
        return {"restored": [], "count": 0, "paused": [], "at": 0,
                "message": "Запрос уже был в архиве до этого действия"}

    try:
        from . import kanban_atomic as _atomic
    except ImportError:  # pragma: no cover
        import kanban_atomic as _atomic    # type: ignore
    try:
        res = _atomic.restore(cfg, board, items)
    except _atomic.AtomicError as exc:
        raise _write.EngineWriteError(exc.code or "no_hermes",
                                      "Не удалось вернуть запрос из архива: %s. "
                                      "Повторите позже." % (exc.message or "нет доступа к Hermes"),
                                      detail=dict(getattr(exc, "detail", {}) or {})) from exc
    if not res.get("ok"):
        raise _write.EngineWriteError("conflict",
                                      "Не удалось вернуть запрос из архива: %s"
                                      % (res.get("message") or "карточки не на месте"))
    restored = [str(item.get("task_id")) for item in (res.get("restored") or []) if item]
    at = int(time.time())
    _patch_record(cfg, room.get("id"), root_id, record,
                  {"archived_at": 0, "archived_by": "", "archive_prev": {},
                   "restored_at": at, "restored_by": str(by or "")})
    message = "Запрос возвращён из архива"
    if paused:
        message += (" — работа приостановлена: нажмите «Продолжить» там, где нужно "
                    "продолжить, или дайте указание")
    return {"restored": restored, "count": len(restored), "paused": paused,
            "at": at, "message": message}


__all__ = ["ARCHIVE_LIMIT", "REASON_ARCHIVED", "RESTORE_DONE", "RESTORE_PAUSED",
           "SUGGEST_AFTER_S", "SUGGEST_LIMIT", "AUTO_DEFAULT", "auto_enabled",
           "suggestions", "archive_info", "archive_request", "restore_request"]
