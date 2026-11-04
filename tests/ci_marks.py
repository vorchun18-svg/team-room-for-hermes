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
    return shutil.which("hermes") is not None


def has_stt() -> bool:
    try:
        import faster_whisper  # noqa: F401
    except Exception:  # noqa: BLE001
        return False
    return True
