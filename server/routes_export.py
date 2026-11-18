"""Экспорт свода запроса одним действием (W20-U3, пункт 17 / P13).

Один маршрут на все три кнопки блока «Свод команды»::

    POST /api/requests/{id}/export   {"format": "obsidian"|"html"}

* ``obsidian`` — файл кладётся в ТУ ЖЕ зеркальную папку комнаты в хранилище
  заметок, куда приложение ведёт ``Запросы/<дата> <название> (<id>).md``
  (`server/vault_mirror.sync_request`). Повторный экспорт обновляет ту же
  заметку, а дописанное человеком вне служебных маркеров сохраняется.
  Ответ — JSON: ``{ok, path, folder, name, changed}``; в демо (``TEAMROOM_MOCK``)
  и без хранилища — ``{ok:false, skipped, reason}`` без выдуманного пути.
* ``html`` — автономный файл (тот же ``server/export.py:export_request_html``,
  что и ``GET /api/export/request/{id}.html``): скачивается вложением, ни одной
  внешней ссылки внутри.
* ``print`` — печать браузера: сервер не нужен, но формат принимается, чтобы
  интерфейс мог отправить один запрос на все кнопки; отвечает `204` и подсказкой
  (``hint: "print_css"``), а печать запускает клиент по ``@media print``.

Маршрут отдельным модулем (а не в ``routes_write.py``), чтобы правки волны 20 в
общих файлах не пересекались. Подключение — одним блоком в ``server/app.py``::

    from server.routes_export import install_export_routes
    install_export_routes(App, ROUTES)
"""
from __future__ import annotations

import re as _re

try:  # пакетный и «плоский» импорт
    from .http_util import ApiError, Raw, err
except ImportError:  # pragma: no cover
    from http_util import ApiError, Raw, err  # type: ignore

RID_RE = r"(?P<rid>[A-Za-z0-9_\-.]{1,80})"
BASE = r"/api/requests/" + RID_RE

ROUTES = [
    ("POST", BASE + r"/export", "h_request_export"),
]

#: форматы, которые понимает маршрут (порядок — для сообщения об ошибке)
FORMATS = ("obsidian", "html", "print")


def _as_str(value) -> str:
    return "" if value is None else str(value).strip()


class ExportRoutes:
    """Обработчики экспорта свода (``self`` — экземпляр ``server.app.App``)."""

    def h_request_export(self, query, body, rid):
        """``POST /api/requests/{id}/export`` — свод в заметки, в файл или на печать."""
        data = body if isinstance(body, dict) else {}
        fmt = _as_str(data.get("format")).lower()
        if not fmt:
            raise err(400, "Укажите формат: obsidian, html или print",
                      field="format", allowed=list(FORMATS))
        if fmt not in FORMATS:
            raise err(400, "Формат: obsidian, html или print",
                      field="format", allowed=list(FORMATS), value=fmt)
        if fmt == "print":
            # Печать собирает браузер: сервер только подтверждает формат.
            return 200, {"ok": True, "format": "print", "hint": "print_css"}, \
                {"Cache-Control": "no-store"}

        detail, rec = self._export_detail(rid)

        if fmt == "obsidian":
            try:
                res = self.call("export", "export_request_obsidian", ((self.cfg, detail), {}),
                                ((detail,), {}))
            except ApiError:
                raise
            except Exception as exc:  # noqa: BLE001 - хранилище может быть занято
                self.log.error("export obsidian %s" % rid, exc)
                raise ApiError(502, "Не удалось положить свод в заметки — попробуйте ещё раз",
                               detail={"rid": rid, "error": str(exc)[-300:]}) from exc
            payload = dict(res or {})
            payload.setdefault("ok", False)
            payload["format"] = "obsidian"
            payload["room"] = detail.get("room")
            payload["request"] = rid
            return 200, payload, {"Cache-Control": "no-store"}

        # html: автономный файл вложением (как GET /api/export/request/{id}.html)
        try:
            html_text = self.call("export", "export_request_html", ((self.cfg, detail), {}),
                                  ((detail,), {}))
        except ApiError:
            raise
        except Exception as exc:  # noqa: BLE001 - экспорт может упасть на рендере
            self.log.error("export html %s" % rid, exc)
            raise ApiError(502, "Не удалось собрать HTML-экспорт",
                           detail={"rid": rid, "stderr": str(exc)[-1000:]}) from exc
        slug = _as_str((rec or {}).get("slug")) or _as_str(rid) or "request"
        name = slug + ".html"
        csp = self._report_csp() if hasattr(self, "_report_csp") else None
        headers = {"Content-Disposition": 'attachment; filename="%s"' % name,
                   "Cache-Control": "no-store"}
        if csp:
            headers["Content-Security-Policy"] = csp
        return 200, Raw(html_text or "", "text/html; charset=utf-8", headers), {}


def install_export_routes(app_cls, routes: list) -> None:
    """Подключить экспорт свода к ``App``: метод + строка таблицы маршрутов."""
    for name, value in vars(ExportRoutes).items():
        if name.startswith("__") or not callable(value):
            continue
        setattr(app_cls, name, value)
    existing = {(method, pattern) for method, pattern, _handler in routes}
    for method, pattern, handler in ROUTES:
        if (method, pattern) in existing:
            continue
        routes.append((method, _re.compile(pattern), handler))
