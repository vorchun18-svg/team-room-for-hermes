"""Признаки «чистого раннера» для тестов: что проверять можно, а что нечего.

На GitHub Actions (и на любой чужой машине) нет ни профилей Hermes Desktop, ни
`faster-whisper`, ни сети. Тесты, которым это нужно, обязаны **честно
пропускаться**, а не падать: `TEAMROOM_CI=1` выставляется в workflow, но условия
ниже проверяют саму среду — так тест ведёт себя одинаково и без флага.

    from tests import ci_marks
    @unittest.skipUnless(ci_marks.has_real_profiles(), ci_marks.WHY_NO_PROFILES)
    def test_profile_files_untouched(self): ...
"""
from __future__ import annotations

import os
import shutil
from pathlib import Path

#: прогон в CI (workflow ci.yml / release.yml); сама по себе не отключает проверки
CI = os.environ.get("TEAMROOM_CI", "").strip().lower() not in ("", "0", "false", "no")

IS_WINDOWS = os.name == "nt"

WHY_NO_PROFILES = ("нет установленных профилей Hermes Desktop — на чистом раннере "
                   "нечего защищать и нечего сравнивать")
WHY_NO_STT = "нет faster-whisper — распознавание речи на раннере недоступно"
WHY_NO_CLI = "нет CLI hermes — запись в канбан на раннере проверить нельзя"
#: метка CI-shim-а: по ней `cli_is_shim()` узнаёт подмену без запуска процессов
SHIM_MARK = "team-room-ci-hermes-shim"
WHY_CI_SHIM = ("CLI hermes на раннере подменён тонким shim-ом (tools/ci_hermes_shim.py): "
               "он умеет только доску песочницы, создание/завершение карточек и комментарии. "
               "Сквозные сценарии с настоящим движком проверяются на машине с Hermes")
WHY_NO_HERMES_MACHINE = ("на этой машине нет установленного Hermes (профилей, диспетчера) — "
                         "проверять нечего; на машине пользователя проверка выполняется")
WHY_WIN_SHIM_LINES = ("на Windows CLI подменён .cmd-обёрткой, а cmd.exe обрезает "
                      "многострочный аргумент — точный текст проверяется с настоящим Hermes")


def hermes_home() -> Path:
    env = os.environ.get("HERMES_HOME")
    if env:
        return Path(env).expanduser()
    local = os.environ.get("LOCALAPPDATA")
    candidates = [Path(local) / "hermes"] if local else []
    candidates.append(Path.home() / ".hermes")
    for candidate in candidates:
        if candidate.is_dir():
            return candidate
    return candidates[-1]


def profile_names() -> list[str]:
    """Имена профилей Hermes (каталоги с `config.yaml`), без чтения их содержимого."""
    root = hermes_home() / "profiles"
    if not root.is_dir():
        return []
    return sorted(p.name for p in root.iterdir() if p.is_dir() and (p / "config.yaml").is_file())


def has_real_profiles(minimum: int = 1) -> bool:
    return len(profile_names()) >= minimum


def has_hermes_cli() -> bool:
    """Есть ли на PATH `hermes` (это может быть и CI-shim — см. `cli_is_shim`)."""
    return bool(hermes_cli_path())


def hermes_cli_path() -> str:
    """Чем приложение/тесты видят `hermes`: `TEAMROOM_HERMES_CLI` или PATH."""
    env = (os.environ.get("TEAMROOM_HERMES_CLI") or "").strip()
    if env:
        return env
    return shutil.which("hermes") or ""


def cli_is_shim() -> bool:
    """`hermes` — наш тонкий CI-shim (`tools/ci_hermes_shim.py`), а не Hermes CLI.

    Признаки: флаг `TEAMROOM_CI_HERMES_SHIM=1` из workflow, имя файла shim-а или
    ответ `--version` с меткой. Сквозные сценарии с настоящим движком (диспетчер,
    декомпозиция, агенты) на shim-е не проверяются — такие тесты пропускаются
    честно, а не падают.
    """
    if (os.environ.get("TEAMROOM_CI_HERMES_SHIM") or "").strip().lower() not in ("", "0", "false", "no"):
        return True
    path = hermes_cli_path()
    if not path:
        return False
    if "ci_hermes_shim" in path.replace("\\", "/").lower():
        return True
    try:
        import subprocess
        res = subprocess.run([path, "--version"], capture_output=True, text=True,
                             encoding="utf-8", errors="replace", timeout=20)
    except Exception:  # noqa: BLE001 — не смогли спросить: считаем настоящим
        return False
    text = ((res.stdout or "") + (res.stderr or "")).lower()
    return SHIM_MARK in text


def has_real_hermes_cli() -> bool:
    """Настоящий Hermes CLI (не CI-shim) — только он умеет диспетчер и агентов."""
    return has_hermes_cli() and not cli_is_shim()


def has_hermes_machine() -> bool:
    """На машине есть установленный Hermes (профили/диспетчер) — как у пользователя."""
    return has_real_profiles()


def cli_is_windows_shim() -> bool:
    """Shim на Windows — это `.cmd`, а `cmd.exe` режет многострочные аргументы.

    Настоящий `hermes.exe` принимает текст комментария с переводами строк как один
    аргумент, а batch-обёртка обрывает его на первом переводе строки. Поэтому
    проверки точного текста многострочного комментария на Windows с shim-ом
    пропускаются честно (на машине с Hermes они выполняются).
    """
    return IS_WINDOWS and cli_is_shim()


def has_stt() -> bool:
    try:
        import faster_whisper  # noqa: F401
    except Exception:  # noqa: BLE001
        return False
    return True
