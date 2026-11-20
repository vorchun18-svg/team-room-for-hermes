"""Выбор файлов для вложений: системное окно Windows и действия с чипом (W28-A).

Зачем серверная ручка
---------------------
Браузер не отдаёт полный путь из ``<input type=file>`` (только имя и содержимое),
а вложению нужен ПУТЬ: агент читает файл своими инструментами. Поэтому диалог
открывает сервер на машине человека (``System.Windows.Forms.OpenFileDialog``,
``Multiselect``) и возвращает выбранные пути. Запуск — через ``server/proc.py``:
``CREATE_NO_WINDOW`` (без мигающей чёрной консоли) плюс невидимое окно-владелец
``TopMost`` — иначе диалог открывается ЗА окном браузера, и кажется, что скрепка
не работает.

Песочница, демо и тесты подменяют диалог списком путей:

    TEAMROOM_PICK_FAKE="C:/Проекты/Альфа/смета.xlsx\nC:/Загрузки/договор v3.doc"

(JSON-массив тоже понимается). Пустая строка = «окно закрыли, ничего не выбрали».

Маршруты:

* ``POST /api/files/pick``          ``{title?}`` → ``{paths:[...], cancelled:bool}``;
* ``POST /api/files/attach-check``  ``{attach:[...], confirm_secret?}`` → по каждому
  пути ``{ok, code, message, name, size, ext, secret}`` — для чипов и «Всё равно
  прикрепить»;
* ``POST /api/files/attach-open``   ``{path, mode:"open"|"reveal"}`` — «Открыть» и
  «Открыть в папке» у чипа вложения. Проверка та же, что у вложения (файл есть,
  это файл, читается): белый список папок и расширений здесь не применяется.

Подключение — в ``server/app.py``::

    from server.routes_files_pick import install_pick_routes
    install_pick_routes(App)
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

try:  # пакетный и «плоский» импорт
    from . import attachments, proc
    from .app_common import MAX_ATTACH, err
    from .files import BLOCKED_EXT, select_command
except ImportError:  # pragma: no cover
    import attachments  # type: ignore
    import proc  # type: ignore
    from app_common import MAX_ATTACH, err  # type: ignore
    from files import BLOCKED_EXT, select_command  # type: ignore

#: Переменная песочницы: список путей вместо живого диалога.
FAKE_ENV = "TEAMROOM_PICK_FAKE"
#: Сколько ждать, пока человек выберет файлы (сек).
TIMEOUT_ENV = "TEAMROOM_PICK_TIMEOUT"
DEFAULT_TIMEOUT = 300
#: Заголовок окна по умолчанию (по-русски: интерфейс тоже русский).
DEFAULT_TITLE = "Прикрепить файлы"

ROUTES = [
    ("POST", r"/api/files/pick", "h_files_pick"),
    ("POST", r"/api/files/attach-check", "h_files_attach_check"),
    ("POST", r"/api/files/attach-open", "h_files_attach_open"),
]

#: Скрипт диалога. Пишется во временный .ps1 и запускается файлом: так не нужно
#: экранировать кавычки в путях и кириллице внутри аргументов командной строки.
#: Ответ уходит в отдельный файл UTF-8 — консольная кодировка PowerShell (cp866)
#: испортила бы кириллические имена.
DIALOG_SCRIPT = r"""
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing
[System.Windows.Forms.Application]::EnableVisualStyles()
$owner = New-Object System.Windows.Forms.Form
$owner.Text = '@TITLE@'
$owner.TopMost = $true
$owner.ShowInTaskbar = $false
$owner.FormBorderStyle = [System.Windows.Forms.FormBorderStyle]::None
$owner.StartPosition = [System.Windows.Forms.FormStartPosition]::CenterScreen
$owner.Size = New-Object System.Drawing.Size(1, 1)
$owner.Opacity = 0
$owner.Show()
$owner.Activate()
$dlg = New-Object System.Windows.Forms.OpenFileDialog
$dlg.Title = '@TITLE@'
$dlg.Multiselect = $true
$dlg.CheckFileExists = $true
$dlg.CheckPathExists = $true
$dlg.Filter = 'Все файлы (*.*)|*.*'
$dlg.RestoreDirectory = $true
$answer = $dlg.ShowDialog($owner)
$lines = @()
if ($answer -eq [System.Windows.Forms.DialogResult]::OK) { $lines = @($dlg.FileNames) }
[System.IO.File]::WriteAllLines('@OUT@', $lines, (New-Object System.Text.UTF8Encoding($false)))
$dlg.Dispose()
$owner.Close()
$owner.Dispose()
"""


class PickError(Exception):
    """Диалог не открылся / не ответил: код + русский текст с действием."""

    def __init__(self, code: str, message: str, status: int = 400, detail: dict | None = None):
        super().__init__(message)
        self.code = str(code or "pick_error")
        self.message = str(message)
        self.status = int(status)
        self.detail = dict(detail or {})


def parse_fake(value) -> list:
    """Разобрать ``TEAMROOM_PICK_FAKE``: JSON-массив или пути по одному на строку."""
    text = str(value or "").replace("\r\n", "\n").strip()
    if not text:
        return []
    if text.startswith("["):
        try:
            data = json.loads(text)
        except ValueError:
            data = None
        if isinstance(data, list):
            return _dedupe(str(item).strip() for item in data)
    return _dedupe(line.strip() for line in text.split("\n"))


def _dedupe(items) -> list:
    out, seen = [], set()
    for item in items:
        key = os.path.normcase(item)
        if not item or key in seen:
            continue
        seen.add(key)
        out.append(item)
    return out


def _powershell() -> str:
    """Настоящий powershell.exe (5.1) или pwsh, если его нет."""
    for name in ("powershell.exe", "powershell", "pwsh.exe", "pwsh"):
        found = shutil.which(name)
        if found:
            return found
    raise PickError("pick_unavailable",
                    "Не нашёл PowerShell, чтобы открыть окно выбора файлов. Вставьте путь "
                    "в поле «Путь к файлу» — можно несколько, по одному на строку.",
                    status=501)


def _quote_ps(value: str) -> str:
    """Строка для одинарных кавычек PowerShell: ``'`` удваивается."""
    return str(value).replace("'", "''")


def _timeout() -> float:
    try:
        value = float(os.environ.get(TIMEOUT_ENV) or DEFAULT_TIMEOUT)
    except (TypeError, ValueError):
        return float(DEFAULT_TIMEOUT)
    return value if value > 0 else float(DEFAULT_TIMEOUT)


def _read_lines(path: Path) -> list:
    try:
        text = path.read_text(encoding="utf-8-sig", errors="replace")
    except OSError:
        return []
    return _dedupe(line.strip() for line in text.splitlines())


def pick_files(title: str = "", tmp_dir=None) -> list:
    """Открыть окно выбора файлов и вернуть пути (пусто — окно закрыли).

    ``tmp_dir`` — куда положить скрипт и файл ответа (по умолчанию системный
    временный каталог; сервер передаёт свой ``data/tmp``).
    """
    fake = os.environ.get(FAKE_ENV)
    if fake is not None:
        paths = parse_fake(fake)
        for path in paths:
            if len(path) > 1000:
                raise PickError("pick_failed", "Слишком длинный путь в %s." % FAKE_ENV,
                                status=400)
        return paths
    if not proc.IS_WINDOWS:
        raise PickError(
            "pick_unavailable",
            "Окно выбора файлов есть только в Windows. Вставьте путь в поле «Путь к "
            "файлу» — можно несколько, по одному на строку.", status=501)

    powershell = _powershell()
    base = Path(tmp_dir) if tmp_dir else None
    try:
        workdir = Path(tempfile.mkdtemp(prefix="tr-pick-", dir=str(base) if base else None))
    except OSError:
        workdir = Path(tempfile.mkdtemp(prefix="tr-pick-"))
    script_path = workdir / "pick.ps1"
    out_path = workdir / "picked.txt"
    caption = (str(title or "").strip() or DEFAULT_TITLE)[:120]
    script = (DIALOG_SCRIPT.replace("@TITLE@", _quote_ps(caption))
                           .replace("@OUT@", _quote_ps(str(out_path))))
    try:
        script_path.write_text(script, encoding="utf-8-sig")
        try:
            res = proc.run([powershell, "-NoProfile", "-NonInteractive", "-STA",
                            "-ExecutionPolicy", "Bypass", "-WindowStyle", "Hidden",
                            "-File", str(script_path)],
                           timeout=_timeout(), capture_output=True, text=True,
                           encoding="utf-8", errors="replace")
        except subprocess.TimeoutExpired:
            raise PickError(
                "pick_timeout",
                "Окно выбора файлов не дождалось ответа и закрылось. Повторите или "
                "вставьте путь в поле «Путь к файлу» — можно несколько, по одному на строку.",
                status=504) from None
        except (OSError, ValueError) as exc:
            raise PickError(
                "pick_failed",
                "Не удалось открыть окно выбора файлов (%s). Вставьте путь в поле «Путь к "
                "файлу» — можно несколько, по одному на строку." % (str(exc)[:120] or "сбой"),
                status=500) from exc
        paths = _read_lines(out_path)
        if not paths and int(getattr(res, "returncode", 0) or 0) != 0:
            tail = (getattr(res, "stderr", "") or "").strip().splitlines()
            raise PickError(
                "pick_failed",
                "Не удалось открыть окно выбора файлов%s. Вставьте путь в поле «Путь к "
                "файлу» — можно несколько, по одному на строку."
                % (": " + tail[-1][:160] if tail else ""), status=500,
                detail={"stderr": "\n".join(tail[-8:])[:1200]})
        return paths
    finally:
        for leftover in (script_path, out_path):
            try:
                leftover.unlink()
            except OSError:
                pass
        try:
            workdir.rmdir()
        except OSError:
            pass


def _payload(body) -> dict:
    return body if isinstance(body, dict) else {}


class PickRoutes:
    """Обработчики выбора файлов (``self`` — экземпляр ``server.app.App``)."""

    def _pick_cfg(self):
        return getattr(self, "cfg", None)

    # --- POST /api/files/pick -------------------------------------------
    def h_files_pick(self, query, body):
        """Открыть окно выбора файлов Windows и вернуть пути (W28-A, дефект 4).

        Тело: ``{title?}``. Ответ: ``{ok, paths:[...], cancelled:bool, count, max}``.
        ``total``/``dropped`` — сколько выбрано в окне и сколько не влезло в предел
        (лишние не теряются молча: клиент говорит об этом человеку).
        ``cancelled:true`` — человек закрыл окно (это не ошибка, чипов не будет).
        """
        data = _payload(body)
        title = data.get("title")
        if title is not None and not isinstance(title, str):
            raise err(400, "title: строка", field="title")
        tmp_dir = None
        cfg = self._pick_cfg()
        try:
            tmp_dir = getattr(cfg, "tmp_dir", None)
        except (AttributeError, TypeError):
            tmp_dir = None
        try:
            paths = pick_files(title or "", tmp_dir=tmp_dir)
        except PickError as exc:
            raise err(exc.status, exc.message, exc.code, **exc.detail)
        total = len(paths)
        kept = paths[:MAX_ATTACH]
        return 200, {"ok": True, "paths": kept, "cancelled": not kept,
                     "count": len(kept), "total": total, "dropped": total - len(kept),
                     "max": MAX_ATTACH}, {}

    # --- POST /api/files/attach-check -----------------------------------
    def h_files_attach_check(self, query, body):
        """Проверить вложения ДО отправки: имя, размер, «похоже на секрет», причина отказа.

        Тело: ``{attach:[...], confirm_secret?:bool}`` (принимается и ``paths``).
        Ответ: ``{ok, items:[{path, ok, code, message, name, size, ext, secret}],
        needs_confirm:[...], accepted:[...], max}``. Ошибкой считается только
        кривое тело: непригодный путь — это ``ok:false`` у своего элемента, чтобы
        человек видел причину рядом с файлом, а не одну общую ошибку.
        """
        data = _payload(body)
        raw = data.get("attach")
        if raw is None:
            raw = data.get("paths")
        if raw in (None, ""):
            return 200, {"ok": True, "items": [], "needs_confirm": [], "accepted": [],
                         "max": MAX_ATTACH}, {}
        if isinstance(raw, str) or not isinstance(raw, list):
            raise err(400, "attach: список путей к файлам", field="attach")
        if len(raw) > MAX_ATTACH:
            raise err(400, "attach: не больше %d путей — уберите лишние." % MAX_ATTACH,
                      field="attach", count=len(raw), limit=MAX_ATTACH)
        confirm = data.get("confirm_secret") is True
        items, accepted, needs = [], [], []
        for value in raw:
            try:
                info = attachments.check(value, confirm_secret=confirm)
            except attachments.AttachmentError as exc:
                items.append({"path": exc.path, "ok": False, "code": exc.code,
                              "message": exc.message,
                              "name": Path(exc.path or "").name,
                              "size": None, "ext": "", "secret": exc.code == "needs_confirm"})
                if exc.code == "needs_confirm":
                    needs.append(exc.path)
                continue
            items.append({"path": info["path"], "ok": True, "code": "ok", "message": "",
                          "name": info["name"], "size": info["size"], "ext": info["ext"],
                          "secret": info["secret"]})
            accepted.append(info["path"])
        return 200, {"ok": True, "items": items, "needs_confirm": needs,
                     "accepted": accepted, "max": MAX_ATTACH}, {}

    # --- POST /api/files/attach-open ------------------------------------
    def h_files_attach_open(self, query, body):
        """«Открыть» / «Открыть в папке» для чипа вложения.

        Тело: ``{path, mode?: "open"|"reveal"}``. Проверка — «файл есть, это файл,
        читается» (``attachments.check``), без белого списка папок и расширений:
        файл выбрал человек. Исполняемые типы не запускаем — показываем в папке.
        """
        data = _payload(body)
        path = data.get("path")
        if not isinstance(path, str) or not path.strip():
            raise err(400, "Нужен path", field="path")
        mode = str(data.get("mode") or "open").strip().lower()
        if mode not in ("open", "reveal"):
            raise err(400, "mode: open или reveal", field="mode", value=mode)
        try:
            info = attachments.check(path)
        except attachments.AttachmentError as exc:
            raise err(exc.status, exc.message, exc.code, **exc.detail) from None
        target = Path(info["path"])
        note = ""
        if mode == "open" and info["ext"] in BLOCKED_EXT:
            mode = "reveal"
            note = "Программу из вложения не запускаю — показал файл в папке."
        opened = False
        try:
            if mode == "reveal":
                if proc.IS_WINDOWS:
                    proc.popen_line(select_command(target), gui=True)
                else:
                    opened = proc.open_path(target.parent)
                    if not opened:
                        raise OSError("не удалось открыть папку")
                opened = True
            else:
                opened = proc.open_path(target)
        except (OSError, ValueError) as exc:
            raise err(500, "Не удалось открыть «%s»: %s" % (info["name"], str(exc)[:120]),
                      "launch_failed", path=str(target)) from exc
        if not opened:
            raise err(500, "Не удалось открыть «%s» — нет программы по умолчанию. "
                           "Откройте файл вручную." % info["name"],
                      "launch_failed", path=str(target))
        payload = {"ok": True, "path": str(target), "mode": mode, "opened": opened,
                   "revealed": mode == "reveal"}
        if note:
            payload["note"] = note
        return 200, payload, {}


def install_pick_routes(app_cls) -> None:
    """Подключить обработчики выбора файлов к ``App`` (строки ROUTES — в app.py)."""
    for name, value in vars(PickRoutes).items():
        if name.startswith("__") or not callable(value):
            continue
        setattr(app_cls, name, value)


__all__ = ["DIALOG_SCRIPT", "FAKE_ENV", "PickError", "PickRoutes", "ROUTES",
           "install_pick_routes", "parse_fake", "pick_files"]
