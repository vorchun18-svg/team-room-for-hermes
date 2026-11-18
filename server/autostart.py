"""Автозапуск сервера «Комнаты команды» — одна точка правды (волна 20, п.24).

Зачем модуль: до 1.1.0 автозапуск умели ставить только установщики
(`install.ps1` — задача планировщика или ярлык в «Автозагрузке», `install.sh` —
LaunchAgent / systemd --user / строка в `~/.profile`). В «Проверке системы»
пункт «Автозапуск» показывал «не настроен» и отправлял читать документацию.
Теперь у него есть кнопка «Включить автозапуск».

Правила модуля:

* **Ничего не делает молча.** `plan()` строит список действий (файл с текстом
  или команда) и его можно показать/проверить; `enable()` их выполняет и
  возвращает честный итог: что именно включилось и что не получилось.
* **Ничего не трогает на чужой машине в тестах.** `os_name`, `env`, `home`,
  `app_dir` и `runner` — параметры: тесты подставляют поддельные пути и
  поддельный запуск процессов, поэтому на реальной машине при прогоне тестов
  не появляется ни задачи планировщика, ни ярлыка, ни службы.
* **Один смысл — одно место.** Логика повторяет установщики (те же имена задачи,
  метки launchd, имена службы), но живёт в одном файле и одинаково для кнопки
  и для тестов.

Способы по системам:

===========  ==============================================  =========================
Система      Основной способ                                 Запасной
===========  ==============================================  =========================
Windows      задача планировщика ``TeamRoomServer`` (ONLOGON)  ярлык в «Автозагрузке»
macOS        ``~/Library/LaunchAgents/com.teamroom.server.plist``  файл остаётся, грузится при входе
Linux        ``~/.config/systemd/user/teamroom.service``      строка в ``~/.profile``
===========  ==============================================  =========================
"""
from __future__ import annotations

import os
import re
import sys
from pathlib import Path

try:                       # пакетный импорт (server/app.py)
    from . import proc as spawn
except ImportError:        # pragma: no cover  (python server/app.py — плоский импорт)
    import proc as spawn   # type: ignore

TASK_NAME = "TeamRoomServer"
SHORTCUT_NAME = "Командные комнаты.lnk"
SERVICE_NAME = "teamroom"
LAUNCHD_LABEL = "com.teamroom.server"
PROFILE_BEGIN = "# >>> team-room >>>"
PROFILE_END = "# <<< team-room <<<"
COMMAND_TIMEOUT = 60

KIND_WINDOWS = "windows"
KIND_MACOS = "macos"
KIND_LINUX = "linux"


class AutostartError(RuntimeError):
    """Понятная человеку причина: почему автозапуск включить нельзя."""

    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


# ------------------------------------------------------------------ окружение

def detect_os(os_name: str | None = None) -> str:
    """`windows` | `macos` | `linux` — по имени системы или по текущей машине."""
    if os_name:
        name = str(os_name).strip().lower()
        if name.startswith("win"):
            return KIND_WINDOWS
        if name in ("darwin", "mac", "macos", "osx"):
            return KIND_MACOS
        return KIND_LINUX
    if os.name == "nt":
        return KIND_WINDOWS
    if sys.platform == "darwin":
        return KIND_MACOS
    return KIND_LINUX


def _env(env: dict | None = None) -> dict:
    return dict(os.environ if env is None else env)


def user_home(home=None, env: dict | None = None) -> Path:
    """Домашний каталог пользователя (для тестов передаётся явно)."""
    if home is not None:
        return Path(home)
    data = _env(env)
    return Path(data.get("USERPROFILE") or data.get("HOME") or Path.home())


def app_home(app_dir=None) -> Path:
    """Каталог программы (там лежат `bin/`, `data/`, `server/`)."""
    if app_dir is not None:
        return Path(app_dir)
    try:
        from . import config as config_mod
    except ImportError:                     # pragma: no cover
        import config as config_mod         # type: ignore
    return Path(getattr(config_mod, "APP_DIR", "."))


def data_dir(home: Path, app_dir: Path, env: dict | None = None) -> Path:
    """Каталог данных приложения: `TEAMROOM_DATA` или `<программа>/data`."""
    env_data = _env(env).get("TEAMROOM_DATA")
    if env_data:
        return Path(env_data)
    return Path(app_dir) / "data"


def launcher_path(os_kind: str, app_dir: Path) -> Path:
    """Файл, который поднимает сервер (его и ставим в автозапуск)."""
    name = "team-room-server.cmd" if os_kind == KIND_WINDOWS else "team-room-server.sh"
    return Path(app_dir) / "bin" / name


def shortcut_path(home: Path, env: dict | None = None) -> Path:
    """Куда Windows положит ярлык «Автозагрузки»."""
    data = _env(env)
    appdata = data.get("APPDATA")
    base = Path(appdata) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup" \
        if appdata else Path(home) / "AppData" / "Roaming" / "Microsoft" / "Windows" \
        / "Start Menu" / "Programs" / "Startup"
    return base / SHORTCUT_NAME


def plist_path(home: Path) -> Path:
    return Path(home) / "Library" / "LaunchAgents" / (LAUNCHD_LABEL + ".plist")


def unit_path(home: Path) -> Path:
    return Path(home) / ".config" / "systemd" / "user" / (SERVICE_NAME + ".service")


def profile_path(home: Path) -> Path:
    for name in (".profile", ".bash_profile"):
        candidate = Path(home) / name
        if candidate.is_file():
            return candidate
    return Path(home) / ".profile"


# ------------------------------------------------------------------- описание

def _plist(launcher: Path, app_dir: Path, data: Path) -> str:
    logs = data / "logs"
    return (
        "<?xml version=\"1.0\" encoding=\"UTF-8\"?>\n"
        "<!DOCTYPE plist PUBLIC \"-//Apple//DTD PLIST 1.0//EN\" "
        "\"http://www.apple.com/DTDs/PropertyList-1.0.dtd\">\n"
        "<plist version=\"1.0\">\n<dict>\n"
        "  <key>Label</key><string>%s</string>\n"
        "  <key>ProgramArguments</key>\n  <array>\n"
        "    <string>%s</string>\n"
        "    <string>--home</string><string>%s</string>\n"
        "    <string>--quiet</string>\n"
        "  </array>\n"
        "  <key>RunAtLoad</key><true/>\n"
        "  <key>KeepAlive</key><false/>\n"
        "  <key>WorkingDirectory</key><string>%s</string>\n"
        "  <key>StandardOutPath</key><string>%s/launchd.out.log</string>\n"
        "  <key>StandardErrorPath</key><string>%s/launchd.err.log</string>\n"
        "</dict>\n</plist>\n"
        % (LAUNCHD_LABEL, launcher, app_dir, app_dir, logs, logs))


def _unit(launcher: Path, app_dir: Path, data: Path) -> str:
    return (
        "[Unit]\n"
        "Description=Командные комнаты — локальный сервер\n"
        "After=network.target\n\n"
        "[Service]\n"
        "Type=forking\n"
        "ExecStart=\"%s\" --home \"%s\" --quiet\n"
        "PIDFile=%s\n"
        "Restart=no\n\n"
        "[Install]\n"
        "WantedBy=default.target\n"
        % (launcher, app_dir, data / "run" / "server.pid"))


def _profile_block(launcher: Path) -> str:
    return (
        "%s\n"
        "# Автозапуск «Командных комнат» (локальный сервер на 127.0.0.1)\n"
        "[ -x \"%s\" ] && \"%s\" --quiet >/dev/null 2>&1\n"
        "%s\n" % (PROFILE_BEGIN, launcher, launcher, PROFILE_END))


def plan(app_dir=None, home=None, env: dict | None = None, os_name: str | None = None,
         data=None) -> dict:
    """Что именно будет сделано — без единой записи и запуска.

    Возвращает ``{os, launcher, how, actions, notes}``. Действие — словарь
    ``{kind: 'file', path, content}`` (создать/дописать файл) либо
    ``{kind: 'command', argv, why}`` (запустить системную команду).
    """
    kind = detect_os(os_name)
    env_map = _env(env)
    root = app_home(app_dir)
    user = user_home(home, env_map)
    data_root = Path(data) if data is not None else data_dir(user, root, env_map)
    launcher = launcher_path(kind, root)
    if not Path(launcher).is_file():
        shown = launcher.relative_to(root).as_posix() if _is_under(launcher, root) else str(launcher)
        raise AutostartError(
            "В установке нет файла %s — автозапуску нечего запускать. "
            "Поставьте программу установщиком (install.ps1 / install.sh)." % shown)

    actions: list = []
    notes: list = []
    if kind == KIND_WINDOWS:
        target = '"%s"' % launcher
        actions.append({"kind": "command",
                        "argv": ["schtasks", "/Create", "/F", "/городе", "ONLOGON",
                                 "/TN", TASK_NAME, "/TR", target],
                        "why": "задача планировщика при входе в Windows"})
        actions.append({"kind": "command",
                        "argv": ["schtasks", "/Query", "/TN", TASK_NAME],
                        "why": "проверка, что задача создана"})
        link = shortcut_path(user, env_map)
        actions.append({"kind": "command",
                        "argv": _powershell_shortcut_argv(launcher, link, root),
                        "why": "ярлык в «Автозагрузке», если планировщик отказал",
                        "fallback": True})
        how = "задача планировщика %s (иначе — ярлык в «Автозагрузке»)" % TASK_NAME
    elif kind == KIND_MACOS:
        path = plist_path(user)
        actions.append({"kind": "file", "path": str(path),
                        "content": _plist(Path(launcher), root, data_root),
                        "why": "файл LaunchAgent (%s)" % LAUNCHD_LABEL})
        actions.append({"kind": "command", "argv": ["launchctl", "unload", str(path)],
                        "why": "снять прежнюю регистрацию (если была)", "ignore_error": True})
        actions.append({"kind": "command", "argv": ["launchctl", "load", str(path)],
                        "why": "зарегистрировать LaunchAgent"})
        how = "LaunchAgent %s" % LAUNCHD_LABEL
    else:
        path = unit_path(user)
        actions.append({"kind": "file", "path": str(path),
                        "content": _unit(Path(launcher), root, data_root),
                        "why": "служба systemd --user (%s)" % SERVICE_NAME})
        actions.append({"kind": "command", "argv": ["systemctl", "--user", "daemon-reload"],
                        "why": "перечитать службы", "ignore_error": True})
        actions.append({"kind": "command",
                        "argv": ["systemctl", "--user", "enable", "--now",
                                 SERVICE_NAME + ".service"],
                        "why": "включить службу"})
        prof = profile_path(user)
        actions.append({"kind": "append", "path": str(prof),
                        "marker": PROFILE_BEGIN, "content": _profile_block(Path(launcher)),
                        "why": "строка в %s, если systemd отказал" % prof.name,
                        "fallback": True})
        how = "служба systemd --user %s (иначе — строка в ~/.%s)" % (SERVICE_NAME, prof.name)
    return {"os": kind, "launcher": str(launcher), "home": str(user), "how": how,
            "actions": actions, "notes": notes}


def _is_under(path: Path, parent: Path) -> bool:
    try:
        Path(path).resolve().relative_to(Path(parent).resolve())
        return True
    except (ValueError, OSError):
        return False


def _powershell_shortcut_argv(launcher: Path, link: Path, workdir: Path) -> list:
    script = (
        "$w = New-Object -ComObject WScript.Shell; "
        "$s = $w.CreateShortcut('%s'); "
        "$s.TargetPath = '%s'; $s.WorkingDirectory = '%s'; $s.WindowStyle = 7; "
        "$s.Description = 'Командные комнаты - локальный сервер'; $s.Save()"
        % (_ps(str(link)), _ps(str(launcher)), _ps(str(workdir))))
    return ["powershell", "-NoProfile", "-NonInteractive", "-Command", script]


def _ps(text: str) -> str:
    """Путь для PowerShell в одинарных кавычках (удвоить кавычку — и всё)."""
    return str(text).replace("'", "''")


# ------------------------------------------------------------------ включение

def _default_runner(argv: list, timeout: int = COMMAND_TIMEOUT):
    return spawn.run(argv, timeout=timeout, capture_output=True, text=True,
                     encoding="utf-8", errors="replace")


def enable(app_dir=None, home=None, env: dict | None = None, os_name: str | None = None,
           data=None, runner=None) -> dict:
    """Включить автозапуск. Возвращает ``{ok, how, message, written, notes}``.

    Ничего не делает, если план не построился (нет файла запуска) — тогда
    ``ok=False`` и понятная причина. Ошибки отдельных шагов не прячутся:
    итог говорит, что получилось, а что нет.
    """
    todo = plan(app_dir=app_dir, home=home, env=env, os_name=os_name, data=data)
    run = runner or _default_runner
    written: list = []
    notes: list = list(todo.get("notes") or [])
    failures: list = []
    main_ok = False              # основной способ сработал
    fallback_ok = False          # сработал запасной

    for action in todo["actions"]:
        kind = action.get("kind")
        is_fallback = bool(action.get("fallback"))
        if is_fallback and main_ok:
            continue             # основной способ уже сработал — запасной не нужен
        if kind == "file":
            path = Path(action["path"])
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(action["content"], encoding="utf-8")
                written.append(str(path))
                if is_fallback:
                    fallback_ok = True
            except OSError as exc:
                failures.append("не записался %s: %s" % (path.name, exc))
            continue
        if kind == "append":
            path = Path(action["path"])
            try:
                existing = path.read_text(encoding="utf-8") if path.is_file() else ""
                if action["marker"] not in existing:
                    path.parent.mkdir(parents=True, exist_ok=True)
                    with path.open("a", encoding="utf-8") as handle:
                        handle.write(("\n" if existing and not existing.endswith("\n") else "")
                                     + action["content"])
                    written.append(str(path))
                    if is_fallback:
                        fallback_ok = True
                elif is_fallback:
                    fallback_ok = True     # строка уже стоит: автозапуск настроен, не дублируем
            except OSError as exc:
                failures.append("не дописался %s: %s" % (path.name, exc))
            continue
        if kind != "command":
            continue
        argv = list(action.get("argv") or [])
        try:
            res = run(argv)
            code = int(getattr(res, "returncode", 0) or 0)
        except Exception as exc:                     # noqa: BLE001 — нет команды/песочница
            if action.get("ignore_error"):
                notes.append("шаг «%s» пропущен: %s" % (action.get("why", ""), exc))
            else:
                failures.append("%s: %s" % (Path(argv[0]).name, exc))
            continue
        if code == 0:
            if is_fallback:
                fallback_ok = True
            elif not action.get("ignore_error"):
                main_ok = True
            continue
        if action.get("ignore_error"):
            continue
        tail = _tail(getattr(res, "stderr", "") or getattr(res, "stdout", ""))
        failures.append("%s: %s" % (Path(argv[0]).name, tail or ("код %d" % code)))

    if todo["os"] == KIND_WINDOWS:
        ok = main_ok or fallback_ok or any(p.lower().endswith(".lnk") for p in written)
    elif todo["os"] == KIND_MACOS:
        ok = main_ok or bool(written)        # файл LaunchAgent подхватится при входе
    else:
        ok = main_ok or fallback_ok
    if ok and failures and not (main_ok or fallback_ok):
        message = "Автозапуск включён частично: %s." % todo["how"]
    elif ok:
        message = ("Автозапуск включён: %s. Сервер поднимется сам при следующем входе в систему."
                   % todo["how"])
    else:
        message = ("Автозапуск включить не удалось. " + (failures[-1] if failures else
                   "Система не приняла ни один способ — запустите установщик."))
    return {"ok": bool(ok), "how": todo["how"] if ok else "", "os": todo["os"],
            "message": message, "written": written, "notes": notes,
            "failures": failures}


def _tail(value, limit: int = 160) -> str:
    if isinstance(value, bytes):
        value = value.decode("utf-8", "replace")
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    return text[-limit:]
