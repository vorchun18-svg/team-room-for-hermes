"""HTTP-маршруты «Проверки системы» (WAVE 12 §12.2).

Подключение — одним блоком в ``server/app.py``::

    from server.routes_diag import install_diag_routes
    install_diag_routes(App, ROUTES)

* ``GET /api/diagnostics[?room=<id>]`` — пункты «что настроено / что нет / что
  сделать» (см. ``server/diagnostics.py``), без секретов и полных путей;
* ``POST /api/diagnostics/autostart {action:"enable"}`` — кнопка «Включить
  автозапуск»: ставит сервер в автозапуск системы (``server/autostart.py``),
  только по явному действию пользователя (интерфейс спрашивает подтверждение);
* ``GET /api/docs?name=INSTALL.md`` — текст документации приложению, чтобы
  кнопка «Открыть документацию» работала без выхода в проводник. Отдаём только
  белый список файлов из ``docs/`` — никаких путей из запроса.
"""
from __future__ import annotations

import re as _re
from pathlib import Path

try:  # пакетный и «плоский» импорт
    from .http_util import Raw, err
except ImportError:  # pragma: no cover
    from http_util import Raw, err  # type: ignore

#: что приложение показывает своим экраном «Проверка системы»
DOCS = ("INSTALL.md", "USER-GUIDE.md")

ROUTES = [
    ("GET", r"/api/diagnostics", "h_diagnostics"),
    ("POST", r"/api/diagnostics/autostart", "h_diagnostics_autostart"),
    ("GET", r"/api/docs", "h_docs"),
]


class DiagRoutes:
    """Обработчики диагностики (``self`` — экземпляр ``server.app.App``)."""

    def h_diagnostics(self, query, body):
        """``GET /api/diagnostics`` — сводка и список пунктов проверки."""
        room_id = ((query.get("room") or [""])[0] or "").strip()
        room = None
        if room_id:
            room = self._room(room_id)          # нет комнаты → 404 по-русски
        module = self.mod("diagnostics")
        payload = module.report(self.cfg, port=self.port, board=self.board, room=room)
        # Проверка зависит от минуты (порт, логи, диск) — кэшировать нельзя.
        return 200, payload, {"Cache-Control": "no-store"}

    def h_diagnostics_autostart(self, query, body):
        """``POST /api/diagnostics/autostart {action:"enable"}`` — включить автозапуск.

        Ничего не делает без явного действия пользователя: интерфейс спрашивает
        подтверждение и только потом присылает запрос. Итог — человеческий текст
        (что включилось), без полных путей и служебных слов.
        """
        action = str((body or {}).get("action") or "").strip().lower() if isinstance(body, dict) else ""
        # Только явное действие: пустое тело (случайный POST, чужой скрипт) не имеет
        # права ставить задачу планировщика и ярлык в «Автозагрузку» на машине.
        if action != "enable":
            raise err(400, "Нужно явное действие: {action: \"enable\"}", field="action",
                      allowed=["enable"])
        try:
            module = self.mod("autostart")
        except Exception:  # noqa: BLE001 — модуля нет в сборке
            raise err(500, "В этой сборке нет модуля автозапуска") from None
        app_dir = Path(str(getattr(self.cfg, "app_dir", "") or "."))
        try:
            result = module.enable(app_dir=app_dir)
        except getattr(module, "AutostartError", Exception) as exc:
            raise err(400, getattr(exc, "message", None) or str(exc)) from None
        except Exception as exc:  # noqa: BLE001 — не роняем экран проверки
            raise err(500, "Не получилось включить автозапуск: %s" % exc) from None
        diag = self.mod("diagnostics")
        payload = {
            "ok": bool(result.get("ok")),
            "message": str(result.get("message") or ""),
            "how": str(result.get("how") or ""),
            "os": str(result.get("os") or ""),
            "written": [diag.safe_path(path) for path in (result.get("written") or [])][:5],
            "notes": [str(note) for note in (result.get("notes") or [])][:5],
        }
        return 200, payload, {"Cache-Control": "no-store"}

    def h_docs(self, query, body):
        """``GET /api/docs?name=INSTALL.md`` — текст документа из ``docs/``."""
        name = ((query.get("name") or ["INSTALL.md"])[0] or "INSTALL.md").strip() or "INSTALL.md"
        if name not in DOCS:
            raise err(400, "Этот документ приложению недоступен", field="name",
                      allowed=list(DOCS))
        path = Path(str(self.cfg.app_dir)) / "docs" / name
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            raise err(404, "Документ не найден в установке", name=name) from None
        return 200, Raw(text, "text/markdown; charset=utf-8"), {"Cache-Control": "no-cache"}


def install_diag_routes(app_cls, routes: list) -> None:
    """Подключить проверку системы к ``App``: методы миксина + строки маршрутов."""
    for name, value in vars(DiagRoutes).items():
        if name.startswith("__") or not callable(value):
            continue
        setattr(app_cls, name, value)
    existing = {(method, pattern.pattern) for method, pattern, _handler in routes}
    for method, pattern, handler in ROUTES:
        if (method, pattern) in existing:
            continue
        routes.append((method, _re.compile(pattern), handler))
