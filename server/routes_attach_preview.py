"""HTTP предпросмотра вложений (W29B-Q3, docs/API.md §ATTACH-PREVIEW).

Подключение — одним блоком в ``server/app.py``::

    from server.routes_attach_preview import install_attach_preview_routes
    install_attach_preview_routes(App, ROUTES)

Что здесь:

* ``GET  /api/attach/preview?path=…&room=…`` — вид файла и содержимое для панели
  (изображение/PDF/голос — ссылка; текст, таблица, docx/xlsx/pptx — извлечённые
  данные; флаги «что умеет машина»: OCR, расшифровка);
* ``GET  /api/attach/file?path=…&room=…`` — байты файла для браузерного просмотра
  (картинка, PDF в iframe, аудио в плеере), только чтение;
* ``POST /api/attach/transcribe {path, room?}`` — запустить расшифровку голосового
  в фоне (штатный STT приложения, в песочнице ``TEAMROOM_STT_FAKE``);
* ``GET  /api/attach/transcribe?job=…`` — прогресс и текст задачи;
* ``POST /api/attach/ocr {path, room?, lang?}`` — OCR картинки через tesseract,
  если он найден в системе (иначе 503 с человеческим объяснением).

Пути проверяет ``attach_preview.resolve_file``: чтение, без записи, секреты и
служебные каталоги запрещены, символические ссылки за пределы корня не
разворачиваются. Новых действий записи по доске здесь нет.
"""
from __future__ import annotations

import re as _re

try:                                     # пакетный и «плоский» импорт
    from .app_common import err
    from .http_util import ApiError, Raw
except ImportError:                      # pragma: no cover
    from app_common import err           # type: ignore
    from http_util import ApiError, Raw  # type: ignore

ROUTES = [
    ("GET", r"/api/attach/preview", "h_attach_preview"),
    ("GET", r"/api/attach/file", "h_attach_file"),
    ("POST", r"/api/attach/transcribe", "h_attach_transcribe"),
    ("GET", r"/api/attach/transcribe", "h_attach_transcribe_status"),
    ("POST", r"/api/attach/ocr", "h_attach_ocr"),
]

#: Предел тела POST этих ручек: только путь и язык, тело крошечное.
MAX_BODY = 8 * 1024


class AttachPreviewRoutes:
    """Хендлеры панели предпросмотра вложений."""

    # --- общее -----------------------------------------------------------
    def _attach_mod(self):
        return self.mod("attach_preview")

    def _attach_roots(self, room_id: str):
        """Корни комнаты (папка проекта, зеркало) — как у показа файлов (B-22)."""
        from server import config as config_mod
        room_id = str(room_id or "").strip()
        if not room_id:
            return None
        room = self._room(room_id)
        if not isinstance(room, dict):
            raise err(404, "Комната не найдена", room=room_id)
        return config_mod.room_roots(self.cfg, room)

    def _attach_fail(self, exc) -> None:
        """Ошибка модуля — русским текстом и с машинным кодом для интерфейса."""
        code = str(getattr(exc, "code", "") or "attach_failed")
        status = int(getattr(exc, "status", 400) or 400)
        detail = dict(getattr(exc, "detail", {}) or {})
        detail.setdefault("code", code)
        raise ApiError(status, str(getattr(exc, "message", "") or "Не удалось прочитать файл"),
                       detail=detail) from exc

    # --- чтения ----------------------------------------------------------
    def h_attach_preview(self, query, body):
        """GET /api/attach/preview — вид и содержимое вложения для правой панели."""
        path = (query.get("path") or [""])[0].strip()
        if not path:
            raise err(400, "Нужен параметр path", field="path")
        room = (query.get("room") or [""])[0].strip()
        roots = self._attach_roots(room)
        mod = self._attach_mod()
        try:
            return 200, mod.preview(self.cfg, path, roots=roots, room=room), {}
        except Exception as exc:  # noqa: BLE001 — своя ошибка модуля или настоящая поломка
            if hasattr(exc, "code") and hasattr(exc, "status"):
                self._attach_fail(exc)
            self.log.error("attach preview %s" % path, exc)
            raise ApiError(500, "Не удалось построить предпросмотр",
                           detail={"path": path}) from exc

    def h_attach_file(self, query, body):
        """GET /api/attach/file — байты вложения для <img>, PDF-просмотра и плеера."""
        path = (query.get("path") or [""])[0].strip()
        if not path:
            raise err(400, "Нужен параметр path", field="path")
        room = (query.get("room") or [""])[0].strip()
        roots = self._attach_roots(room)
        mod = self._attach_mod()
        try:
            data, ctype = mod.file_bytes(self.cfg, path, roots=roots)
        except Exception as exc:  # noqa: BLE001
            if hasattr(exc, "code") and hasattr(exc, "status"):
                self._attach_fail(exc)
            self.log.error("attach file %s" % path, exc)
            raise ApiError(500, "Не удалось отдать файл", detail={"path": path}) from exc
        headers = {"X-Content-Type-Options": "nosniff", "Cache-Control": "no-store",
                   "Referrer-Policy": "no-referrer"}
        if str(ctype).startswith("text/html"):
            headers["Content-Security-Policy"] = "default-src 'none'; sandbox"
        return 200, Raw(data, str(ctype or "application/octet-stream"), headers), {}

    # --- расшифровка -----------------------------------------------------
    def h_attach_transcribe(self, query, body):
        """POST /api/attach/transcribe — расшифровка голосового в фоне."""
        payload = body if isinstance(body, dict) else {}
        path = str(payload.get("path") or "").strip()
        if not path:
            raise err(400, "Нужен path к голосовому файлу", field="path")
        room = str(payload.get("room") or "").strip()
        roots = self._attach_roots(room)
        mod = self._attach_mod()
        try:
            return 202, mod.start_transcribe(self.cfg, path, roots=roots), {}
        except Exception as exc:  # noqa: BLE001
            if hasattr(exc, "code") and hasattr(exc, "status"):
                self._attach_fail(exc)
            self.log.error("attach transcribe %s" % path, exc)
            raise ApiError(500, "Не удалось запустить расшифровку",
                           detail={"path": path}) from exc

    def h_attach_transcribe_status(self, query, body):
        """GET /api/attach/transcribe?job=… — прогресс и текст расшифровки."""
        job = (query.get("job") or [""])[0].strip()
        if not job:
            raise err(400, "Нужен параметр job", field="job")
        mod = self._attach_mod()
        try:
            return 200, mod.transcribe_job(job), {}
        except Exception as exc:  # noqa: BLE001
            if hasattr(exc, "code") and hasattr(exc, "status"):
                self._attach_fail(exc)
            raise ApiError(500, "Не удалось узнать состояние расшифровки") from exc

    # --- OCR -------------------------------------------------------------
    def h_attach_ocr(self, query, body):
        """POST /api/attach/ocr — распознать текст на картинке (только если есть tesseract)."""
        payload = body if isinstance(body, dict) else {}
        path = str(payload.get("path") or "").strip()
        if not path:
            raise err(400, "Нужен path к картинке", field="path")
        room = str(payload.get("room") or "").strip()
        roots = self._attach_roots(room)
        mod = self._attach_mod()
        try:
            # Проверка пути — та же, что у предпросмотра (комната/корни/секреты).
            target = mod.resolve_file(self.cfg, path, roots=roots)
            return 200, mod.run_ocr(target, str(payload.get("lang") or "rus+eng")), {}
        except Exception as exc:  # noqa: BLE001
            if hasattr(exc, "code") and hasattr(exc, "status"):
                self._attach_fail(exc)
            self.log.error("attach ocr %s" % path, exc)
            raise ApiError(500, "Не удалось распознать текст",
                           detail={"path": path}) from exc


def install_attach_preview_routes(app_cls, routes: list) -> None:
    """Подключить предпросмотр вложений к ``App``: методы миксина + строки маршрутов."""
    for name, value in vars(AttachPreviewRoutes).items():
        if name.startswith("__") or not callable(value):
            continue
        if not hasattr(app_cls, name):
            setattr(app_cls, name, value)
    existing = {(method, pattern.pattern) for method, pattern, _handler in routes}
    for method, pattern, handler in ROUTES:
        if (method, pattern) in existing:
            continue
        routes.append((method, _re.compile(pattern), handler))
