"""Выбор папки (и файла) окном Windows для мастера дежурств (W29-P5).

Зачем серверная ручка
---------------------
Браузер не отдаёт путь к папке: у `<input type=file>` есть только выбор файла и
только его имя. Мастеру дежурства нужен ПУТЬ («Рабочая папка», «Следить за
файлом»), поэтому диалог открывает сервер на машине человека
(``System.Windows.Forms.FolderBrowserDialog``), а не страница.

Устройство — то же, что у выбора файлов (``server/routes_files_pick.py``):
PowerShell запускается через ``server/proc.py`` (``CREATE_NO_WINDOW``), невидимое
окно-владелец ``TopMost`` — иначе диалог открывается ЗА браузером; ответ пишется
в отдельный файл UTF-8 (консольная кодировка cp866 испортила бы кириллицу).

Песочница, демо и тесты подменяют диалог списком путей:

    TEAMROOM_PICK_FAKE="C:/Проекты/Альфа"

Пустая строка = «окно закрыли, ничего не выбрали».

Маршрут: ``POST /api/folders/pick {title?, mode?: "folder"|"file"}`` →
``{ok, path, paths, cancelled, kind, exists}``.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from pathlib import Path

try:                                  # пакетный и «плоский» импорт
    from . import proc
    from .routes_files_pick import FAKE_ENV, PickError, _read_lines, _timeout, parse_fake
except ImportError:                   # pragma: no cover
    import proc                        # type: ignore
    from routes_files_pick import (FAKE_ENV, PickError, _read_lines, _timeout,  # type: ignore
                                   parse_fake)

#: Заголовок окна по умолчанию.
DEFAULT_TITLE = "Выбрать папку"
#: Режимы: папка (по умолчанию) или файл.
MODES = ("folder", "file")

_FOLDER_SCRIPT = r"""
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
$dlg = New-Object System.Windows.Forms.FolderBrowserDialog
$dlg.Description = '@TITLE@'
$dlg.ShowNewFolderButton = $true
$answer = $dlg.ShowDialog($owner)
$lines = @()
if ($answer -eq [System.Windows.Forms.DialogResult]::OK) { $lines = @($dlg.SelectedPath) }
[System.IO.File]::WriteAllLines('@OUT@', $lines, (New-Object System.Text.UTF8Encoding($false)))
$dlg.Dispose()
$owner.Close()
$owner.Dispose()
"""

_FILE_SCRIPT = r"""
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
$dlg.Multiselect = $false
$dlg.CheckFileExists = $true
$dlg.Filter = 'Все файлы (*.*)|*.*'
$dlg.RestoreDirectory = $true
$answer = $dlg.ShowDialog($owner)
$lines = @()
if ($answer -eq [System.Windows.Forms.DialogResult]::OK) { $lines = @($dlg.FileName) }
[System.IO.File]::WriteAllLines('@OUT@', $lines, (New-Object System.Text.UTF8Encoding($false)))
$dlg.Dispose()
$owner.Close()
$owner.Dispose()
"""


def _powershell() -> str:
    for name in ("powershell.exe", "powershell", "pwsh.exe", "pwsh"):
        found = shutil.which(name)
        if found:
            return found
    raise PickError("pick_unavailable",
                    "Не нашёл PowerShell, чтобы открыть окно выбора папки. Впишите путь "
                    "в поле «Рабочая папка» вручную.", status=501)


def _quote_ps(value: str) -> str:
    return str(value).replace("'", "''")


def normalize_mode(value) -> str:
    """Режим диалога: ``folder`` (папка) или ``file`` (файл)."""
    mode = str(value or "folder").strip().lower()
    return mode if mode in MODES else "folder"


def fake_value() -> str | None:
    """Значение ``TEAMROOM_PICK_FAKE`` или ``None``, если диалог должен открыться."""
    return os.environ.get(FAKE_ENV)


def pick_folder(title: str = "", mode: str = "folder", tmp_dir=None) -> str:
    """Открыть окно Windows и вернуть выбранный путь ('' — окно закрыли).

    ``mode`` — ``folder`` (папка) или ``file`` (файл).
    """
    mode = normalize_mode(mode)
    fake = fake_value()
    if fake is not None:
        paths = parse_fake(fake)
        for path in paths:
            if len(path) > 1000:
                raise PickError("pick_failed", "Слишком длинный путь в %s." % FAKE_ENV,
                                status=400)
        return paths[0] if paths else ""
    if not proc.IS_WINDOWS:
        raise PickError(
            "pick_unavailable",
            "Окно выбора есть только в Windows. Впишите путь в поле вручную.", status=501)

    powershell = _powershell()
    base = Path(tmp_dir) if tmp_dir else None
    try:
        workdir = Path(tempfile.mkdtemp(prefix="tr-folder-", dir=str(base) if base else None))
    except OSError:
        workdir = Path(tempfile.mkdtemp(prefix="tr-folder-"))
    script_path = workdir / "pick.ps1"
    out_path = workdir / "picked.txt"
    caption = (str(title or "").strip() or DEFAULT_TITLE)[:120]
    template = _FILE_SCRIPT if mode == "file" else _FOLDER_SCRIPT
    script = (template.replace("@TITLE@", _quote_ps(caption))
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
                "Окно выбора не дождалось ответа и закрылось. Повторите или впишите путь "
                "вручную.", status=504) from None
        except (OSError, ValueError) as exc:
            raise PickError(
                "pick_failed",
                "Не удалось открыть окно выбора (%s). Впишите путь вручную."
                % (str(exc)[:120] or "сбой"), status=500) from exc
        paths = _read_lines(out_path)
        if not paths and int(getattr(res, "returncode", 0) or 0) != 0:
            tail = (getattr(res, "stderr", "") or "").strip().splitlines()
            raise PickError(
                "pick_failed",
                "Не удалось открыть окно выбора%s. Впишите путь вручную."
                % (": " + tail[-1][:160] if tail else ""), status=500,
                detail={"stderr": "\n".join(tail[-8:])[:1200]})
        return paths[0] if paths else ""
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


def check_path(value) -> dict:
    """Проверить выбранный путь, не открывая окно: есть ли он и что это.

    Возвращает ``{ok, path, kind, exists, message}``. Путь вне диска (сетевое
    имя) не проверяем — не выдумываем «нет такого».
    """
    text = str(value or "").strip().strip('"')
    if not text:
        return {"ok": False, "path": "", "kind": "", "exists": None,
                "message": "Путь не выбран"}
    target = Path(text)
    try:
        exists = target.exists()
        kind = "folder" if target.is_dir() else "file" if target.is_file() else ""
    except OSError:
        return {"ok": False, "path": text, "kind": "", "exists": None,
                "message": "Путь не читается — проверьте имя диска и права"}
    if not exists:
        return {"ok": False, "path": text, "kind": "", "exists": False,
                "message": "Такой папки нет на диске — выберите ещё раз"}
    return {"ok": True, "path": text, "kind": kind, "exists": True, "message": ""}


__all__ = ["DEFAULT_TITLE", "MODES", "check_path", "fake_value", "normalize_mode",
           "pick_folder"]
