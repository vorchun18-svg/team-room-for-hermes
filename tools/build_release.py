#!/usr/bin/env python3
"""Сборка ЧИСТОГО публичного релиза «Комнаты команды».

Версия берётся из файла `VERSION` в корне проекта (одна цифра правды).

Собирает обезличенную копию проекта, ничего не меняя в исходнике:

    %LOCALAPPDATA%/hermes/cache/scratch/release/team-room-<version>/

Что делает по шагам:

1. копирует только то, что можно публиковать (server, web, desktop-plugin,
   reportkit, рабочие tools, tests, демо-fixtures, публичные docs, скриншоты);
2. кладёт в копию `VERSION` и проверяет, что `server/config.py` читает версию
   оттуда, а `PLUGIN_VERSION` в плагине совпадает с ней;
3. обезличивает текст по таблице `tools/release_sanitize.json` (данные, не код):
   личные пути, имя пользователя, бренды, почты, демо-профили;
4. пишет README, LICENSE (MIT, автор vorchun), CHANGELOG, .gitignore
   и проверяет, что в CHANGELOG есть раздел текущей версии;
5. при `--run-tests` прогоняет в копии `unittest` (кроме test_guard) и `node --test`;
6. делает локальный `git init` + коммит нейтральным автором и тег `v<version>`;
7. собирает `team-room-<version>.zip` и `.tar.gz` с папкой `team-room-<version>/`
   внутри (их и качают установщики из GitHub Release).

Ничего не публикует: ни push, ни сетевых вызовов. Пишет только внутрь
`.../cache/scratch/release/` (или `--out-dir`).

Примеры:
    python tools/build_release.py
    python tools/build_release.py --run-tests
    python tools/build_release.py --no-git --out-dir dist

Только stdlib (Pillow — если есть, для сжатия скриншотов).
"""

from __future__ import annotations

import argparse
import fnmatch
import gzip
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))
from server import proc as spawn  # noqa: E402  (единый скрытый запуск процессов)
TABLE_PATH = HERE / "release_sanitize.json"
VERSION_FILE = "VERSION"
#: подстановка версии в README/CHANGELOG (никаких вторых цифр в текстах)
VERSION_TOKEN = "__VERSION__"


def read_version() -> str:
    """Версия — только из `VERSION` в корне: одна цифра правды для архива, тега и CHANGELOG."""
    path = ROOT / VERSION_FILE
    if not path.is_file():
        raise SystemExit("ОТКАЗ: нет файла %s в корне проекта" % VERSION_FILE)
    text = path.read_text(encoding="utf-8").strip()
    if not re.match(r"^\d+\.\d+\.\d+([-.+][0-9A-Za-z.\-+]+)?$", text or ""):
        raise SystemExit("ОТКАЗ: %s должен содержать версию вида 1.0.0, а не %r" % (VERSION_FILE, text))
    return text.splitlines()[0].strip()


VERSION = read_version()
PKG_NAME = "team-room-%s" % VERSION

COPY_DIRS = ("server", "web", "desktop-plugin", "reportkit", "tests", "fixtures")

#: необязательные каталоги установки: их пишет установщик (INSTALL), к моменту
#: сборки они могут ещё не существовать — тогда просто предупреждаем
COPY_OPTIONAL_DIRS = ("bin",)

#: файлы установки в корне: кладём, если есть
COPY_ROOT_OPTIONAL = ("install.ps1", "install.sh")

#: рабочие инструменты; отладочные (probe_*, mic_*, seed_*, shot, cdp, stt_stub) не публикуем
COPY_TOOLS = (
    "install_desktop_plugin.py",
    "team_room_guard.py",
    "stop_team_room.py",
    "e2e_ui.py",
    "check_conformance.py",
    "check_desktop_module.py",
    "check_fixtures.py",
    "mock_app.py",
    "mock_backlog.py",
    "mock_core.py",
    "mock_demo.py",
    "mock_models.py",
    "mock_notes.py",
    "mock_paths.py",
    "mock_plan.py",
    "mock_read.py",
    "mock_server.py",
    "mock_stt.py",
    "mock_thread.py",
    "mock_versions.py",
    "mock_write.py",
    # поддержка тестов в копии (tests/helpers.py импортирует seed_sandbox)
    "seed_sandbox.py",
    "seed_content.py",
    # мост к hermes_cli: без него не применяются модель и мышление карточки
    "kb_bridge.py",
    # сам сборщик и сканер — чтобы релиз можно было пересобрать
    "build_release.py",
    "release_scan.py",
    # проверка переносимости на «чистой машине» (без наших путей и Vault)
    "portability_check.py",
)

COPY_DOCS = (
    "USER-GUIDE.md",
    "AGENT-GUIDE.md",
    "API.md",
    "ARCHITECTURE.md",
    "DESIGN-NOTES.md",
    "DEMO.md",
    "DESKTOP-MODULE.md",
    "INTEGRATION-HERMES.md",
    "SAFETY-RULES.md",
    "INSTALL.md",          # пишет INSTALL; копируем, когда появится
    "RELEASING.md",        # как выпускается версия — нужно тому, кто пересобирает
)

#: скриншоты для README: только из docs/shots/final, только без бренда на картинке
SHOT_CANDIDATES = (
    "backlog-1280.png",
    "roadmap-1280-dark.png",
    "team-780.png",
    "composer-1280.png",
    "room-model-dialog.png",
)
SHOTS_LIMIT = 8
SHOT_MAX_WIDTH = 1280

IGNORE_PARTS = {"__pycache__", ".git", "node_modules", ".pytest_cache", "data", ".mypy_cache"}

#: отладочные страницы-макеты интерфейса в релиз не идут
COPY_SKIP_GLOBS: tuple[str, ...] = ()

#: файлы установки, которых может ещё не быть (их пишет установщик)
OPTIONAL_DOCS = ("INSTALL.md",)


# ---------------------------------------------------------------- пути и защита
def local_appdata() -> Path:
    env = os.environ.get("LOCALAPPDATA")
    if env:
        return Path(env)
    return Path.home() / "AppData" / "Local"


def scratch_dir() -> Path:
    env = os.environ.get("BH_AGENT_WORKSPACE")
    if env and Path(env).is_dir():
        return Path(env)
    return local_appdata() / "hermes" / "cache" / "scratch"


def release_root() -> Path:
    return scratch_dir() / "release"


def package_dir() -> Path:
    return release_root() / PKG_NAME


def guard_paths() -> Path:
    """Проверяет, что пишем только в .../cache/scratch/release и не в исходник."""
    pkg = package_dir()
    root = release_root().resolve()
    target = pkg.resolve()
    if root not in target.parents:
        raise SystemExit("ОТКАЗ: %s вне %s" % (target, root))
    if pkg.name != PKG_NAME or not pkg.name.startswith("team-room-"):
        raise SystemExit("ОТКАЗ: подозрительное имя пакета %s" % pkg.name)
    source = ROOT.resolve()
    if target == source or source in target.parents:
        raise SystemExit("ОТКАЗ: цель внутри исходника проекта")
    return pkg


def force_rmtree(path: Path) -> None:
    """Удаляет каталог, снимая read-only (git ставит его на объекты в .git/objects)."""
    def _on_error(func, target, exc):  # noqa: ANN001
        try:
            os.chmod(target, 0o777)
            func(target)
        except OSError:
            pass

    try:
        shutil.rmtree(path, onexc=_on_error)
    except TypeError:  # python < 3.12
        shutil.rmtree(path, onerror=lambda f, p, e: _on_error(f, p, e))


def load_table() -> dict:
    if not TABLE_PATH.is_file():
        print("  ! нет таблицы %s — обезличивание пропущено" % TABLE_PATH.name)
        return {}
    with TABLE_PATH.open("r", encoding="utf-8") as fh:
        return json.load(fh)


# ------------------------------------------------------------------- копирование
def _ignore(dirname: str, names: list[str]) -> set[str]:
    skip = set()
    for name in names:
        if name in IGNORE_PARTS or name.endswith((".pyc", ".pyo")) or name.startswith("_debug"):
            skip.add(name)
    return skip


def copy_tree(src: Path, dst: Path) -> int:
    if not src.is_dir():
        print("  ! нет каталога %s" % src)
        return 0
    count = 0
    for path in src.rglob("*"):
        if path.is_dir():
            continue
        rel = path.relative_to(src)
        if any(part in IGNORE_PARTS or part.startswith("_debug") for part in rel.parts):
            continue
        posix = rel.as_posix()
        if any(fnmatch.fnmatch(posix, glob) for glob in COPY_SKIP_GLOBS):
            continue
        if path.suffix.lower() in (".pyc", ".pyo", ".log") or ".bak" in path.name:
            continue
        target = dst / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, target)
        count += 1
    return count


def copy_file(src: Path, dst: Path) -> int:
    if not src.is_file():
        print("  ! нет файла %s" % src)
        return 0
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dst)
    return 1


def copy_shots(pkg: Path) -> list[str]:
    src_dir = ROOT / "docs" / "shots" / "final"
    dst_dir = pkg / "docs" / "shots"
    picked = [n for n in SHOT_CANDIDATES if (src_dir / n).is_file()][:SHOTS_LIMIT]
    if not picked:
        picked = [p.name for p in sorted(src_dir.glob("*.png"))][:SHOTS_LIMIT]
    dst_dir.mkdir(parents=True, exist_ok=True)
    try:
        from PIL import Image  # необязательная зависимость
    except Exception:  # noqa: BLE001
        Image = None
    out = []
    for name in picked:
        src = src_dir / name
        dst = dst_dir / name
        if Image is None:
            shutil.copy2(src, dst)
        else:
            try:
                with Image.open(src) as img:
                    img = img.convert("RGB") if img.mode not in ("RGB", "RGBA") else img
                    if img.width > SHOT_MAX_WIDTH:
                        height = round(img.height * SHOT_MAX_WIDTH / img.width)
                        img = img.resize((SHOT_MAX_WIDTH, height), Image.LANCZOS)
                    img.save(dst, format="PNG", optimize=True)
            except Exception as exc:  # noqa: BLE001
                print("  ! скриншот %s не сжат (%s) — копирую как есть" % (name, exc))
                shutil.copy2(src, dst)
        out.append(name)
    return out


# --------------------------------------------------------------- обезличивание
#: множитель для пересчёта сумм: реалы → рубли (порядок цен для города Заозёрье)
MONEY_SCALE = 10
MONEY_SYMBOL = "₽"


def _fmt_money(value: float) -> str:
    """420.0 → «420», 221.5 → «221,50», 1234 → «1 234» (русская запись)."""
    text = ("%.2f" % value).rstrip("0").rstrip(".")
    whole, _, frac = text.partition(".")
    whole = "{:,}".format(int(whole)).replace(",", " ")
    return whole + ("," + frac if frac else "") if frac else whole


def _scale_money(num_text: str) -> str:
    raw = num_text.replace("\u00a0", "").replace(" ", "").strip(" .,")
    if not raw:
        return num_text
    if "," in raw and "." in raw:
        raw = raw.replace(".", "").replace(",", ".")
    elif "," in raw:
        raw = raw.replace(",", ".")
    try:
        return _fmt_money(float(raw) * MONEY_SCALE)
    except ValueError:
        return num_text


def _paths_ok(rel: str | None, globs) -> bool:
    if not globs:
        return True
    if rel is None:
        return False
    return any(fnmatch.fnmatch(rel, glob) for glob in globs)


def _word_pattern(old: str) -> re.Pattern:
    """Целое слово: границы слова, но «\\n»/«\\t» в JSON — тоже граница (это escape, не буква n)."""
    letter = "0-9A-Za-zА-Яа-яЁё_"
    left = r"(?:(?<![%s])|(?<=\\n)|(?<=\\t))" % letter
    return re.compile(left + re.escape(old) + r"(?![%s])" % letter)


def apply_table(text: str, table: dict, counts: dict, rel: str | None = None) -> str:
    """Замены по таблице: целые слова (если не указано mode=substring), длинные — первыми.

    Два прохода: второй ловит фразы, которые получились после первого (например
    «развилка по товарному знаку» из «развилка по товарному знаку»), и не портит уже заменённое.
    """
    items = sorted(table.get("literals", []), key=lambda i: len(i["from"]), reverse=True)
    for _pass in range(2):
        for item in items:
            old, new = item["from"], item["to"]
            if not old or not _paths_ok(rel, item.get("paths")):
                continue
            if item.get("mode") == "substring":
                n = text.count(old)
                if n:
                    text = text.replace(old, new)
            else:
                text, n = _word_pattern(old).subn(new, text)
            if n:
                counts[old] = counts.get(old, 0) + n
    for rule in table.get("regexes", []):
        if not _paths_ok(rel, rule.get("paths")):
            continue
        flags = re.MULTILINE if "m" in rule.get("flags", "") else 0
        if "i" in rule.get("flags", ""):
            flags |= re.IGNORECASE
        pattern = re.compile(rule["pattern"], flags)
        if rule.get("kind") == "money":
            text, n = pattern.subn(
                lambda m: _scale_money(m.group(1)) + " " + MONEY_SYMBOL, text)
        else:
            text, n = pattern.subn(rule["to"], text)
        if n:
            key = rule.get("name") or rule["pattern"]
            counts[key] = counts.get(key, 0) + n
    return text


def sanitize_tree(pkg: Path, table: dict) -> dict:
    text_ext = {e.lower() for e in table["text_ext"]}
    counts: dict[str, int] = {}
    touched = 0
    files = sorted(p for p in pkg.rglob("*") if p.is_file())
    # сначала текст, потом имена (переименование опирается на ту же таблицу)
    for path in files:
        if path.suffix.lower() not in text_ext:
            continue
        try:
            data = path.read_bytes()
            raw = data.decode("utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        # Стиль переводов строк сохраняем (.ps1/.cmd в Windows должны быть CRLF, иначе
        # PowerShell 5.1 и cmd читают файл с ошибками), но замены делаем по тексту с
        # ОДИНАРНЫМИ \n: иначе запись `newline="\r\n"` превращает каждый \r\n в \r\r\n,
        # а пустая строка после "\" ломает продолжение выражения в Python.
        crlf = b"\r\n" in data
        text = raw.replace("\r\n", "\n").replace("\r", "\n")
        new = apply_table(text, table, counts, path.relative_to(pkg).as_posix())
        if new != text:
            out = new.replace("\n", "\r\n") if crlf else new
            path.write_bytes(out.encode("utf-8"))
            touched += 1
    renamed = 0
    for path in sorted((p for p in pkg.rglob("*") if p.is_file()), key=lambda p: len(p.name), reverse=True):
        new_name = apply_table(path.name, table, {})
        if new_name and new_name != path.name:
            target = path.with_name(new_name)
            if not target.exists():
                path.rename(target)
                renamed += 1
    return {"counts": counts, "files_touched": touched, "renamed": renamed,
            "total": sum(counts.values())}


def scale_fixture_money(pkg: Path) -> list[str]:
    """Суммы в отчётах-фикстурах: числа в колонках money и в графиках — ×MONEY_SCALE.

    Текстовые суммы уже пересчитало правило kind=money; здесь — только числа JSON,
    иначе в одном отчёте были бы «420 ₽» в подписи и «42,00 ₽» в таблице.
    """
    done = []
    for path in sorted((pkg / "fixtures").glob("report_*.json")):
        try:
            doc = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        changed = False
        for block in doc.get("blocks", []):
            if block.get("type") == "table":
                keys = {c.get("key") for c in block.get("columns", []) if c.get("type") == "money"}
                for row in block.get("rows", []):
                    for key in keys:
                        if isinstance(row.get(key), (int, float)):
                            row[key] = round(float(row[key]) * MONEY_SCALE, 2)
                            changed = True
            elif block.get("type") == "chart" and block.get("unit") in ("₽", "BRL", "R$"):
                for series in block.get("series", []):
                    data = series.get("data")
                    if isinstance(data, list):
                        series["data"] = [round(float(v) * MONEY_SCALE, 2) if isinstance(v, (int, float)) else v
                                          for v in data]
                        changed = True
        if changed:
            path.write_text(json.dumps(doc, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            done.append(path.name)
    return done


def patch_config(pkg: Path) -> list[str]:
    """Версия релиза: `VERSION` в корне копии + проверка, что сервер и плагин её читают.

    Никаких правок числа в коде: `server/config.py` берёт версию из файла, поэтому
    архив, `/api/health` и `PLUGIN_VERSION` не могут разойтись.
    """
    problems: list[str] = []
    (pkg / VERSION_FILE).write_text(VERSION + "\n", encoding="utf-8", newline="\n")
    # тег внутри архива: установщики печатают «версия v1.0.0» по этому файлу
    (pkg / "docs").mkdir(parents=True, exist_ok=True)
    (pkg / "docs" / "RELEASE-TAG.txt").write_text("v%s\n" % VERSION, encoding="utf-8", newline="\n")

    path = pkg / "server" / "config.py"
    if not path.is_file():
        return ["нет server/config.py в копии"]
    text = path.read_text(encoding="utf-8")
    if re.search(r"""^VERSION\s*=\s*["']\d""", text, re.M):
        problems.append("server/config.py хардкодит версию — нужен файл VERSION")
    if "VERSION" not in text or VERSION_FILE not in text:
        problems.append("server/config.py не читает файл VERSION")

    plugin = pkg / "desktop-plugin" / "plugin.js"
    if plugin.is_file():
        found = re.search(r"PLUGIN_VERSION\s*=\s*['\"]([^'\"]+)['\"]", plugin.read_text(encoding="utf-8"))
        if not found:
            problems.append("в плагине нет PLUGIN_VERSION")
        elif found.group(1) != VERSION:
            problems.append("PLUGIN_VERSION %s ≠ VERSION %s" % (found.group(1), VERSION))
    return problems


# ------------------------------------------------------------ файлы релиза
README = """# Комната команды (Team Room) __VERSION__

[![CI](https://github.com/vorchun18-svg/team-room-for-hermes/actions/workflows/ci.yml/badge.svg)](https://github.com/vorchun18-svg/team-room-for-hermes/actions/workflows/ci.yml)

Локальная «комната команды» для небольшой команды людей и ИИ-агентов: комнаты,
запросы, дорожная карта, бэклог, отчёты с графиками и журнал. Работает на вашем
компьютере, без облака, без внешних библиотек и без сборки.

* Сервер — Python 3.11+ (только стандартная библиотека).
* Интерфейс — обычные ES-модули и CSS, открывается в браузере или внутри Hermes Desktop.
* Данные — папка `data/` рядом с программой; в репозиторий она не попадает.

## Требования

Обязательно: Windows 10/11 и Python 3.11+. Hermes Desktop — хост приложения
(без него страница открывается в браузере, меню и сторож не работают).

Необязательно — приложение работает и без этого, разница видна в `/api/health`
(блок `features`) и `/api/stt/status`:

| Что | Если есть | Если нет |
|---|---|---|
| `hermes` CLI | запись карточек в канбан | режим просмотра: чтение работает, запись объясняется плашкой |
| `faster-whisper` **или** STT-провайдер, настроенный в Hermes | диктовка в любом поле | текст печатается руками, микрофон сообщает «диктовка недоступна» |
| хранилище Obsidian (Vault) | зеркало комнат в заметках | работаем без зеркала, подсказываем `TEAMROOM_VAULT_DIR` |

## Что умеет

* **Комнаты.** У каждой комнаты — название, иконка, руководитель, критик и состав
  участников, проверка ответа критиком и число раундов, доска задач, папка проекта,
  проектный режим, запуск направлений (сразу или сначала план) и сколько направлений
  идёт одновременно (1–4). Всё это в одном диалоге настроек.
* **Модели участников.** Любому участнику — руководителю, критику, исполнителю — можно
  выбрать провайдера, модель и уровень мышления. Замена действует только в этой комнате:
  файлы профиля (`config.yaml`, `SOUL.md`, навыки) не меняются, по умолчанию участник
  работает на своей обычной модели. Новые карточки уходят на выбранную модель сами,
  для идущих есть кнопка «Перезапустить на новой модели».
* **Особые указания участнику.** Добавка к роли, действующая только в этой комнате:
  правила, на что смотреть, как делать иначе. Файлы профилей не меняются.
* **Папка проекта.** Одно место для результатов у каждой комнаты: подпапка на каждый
  запрос, автосодержание `README.md`, кнопка «Открыть папку проекта». Путь проверяется
  до сохранения — системные и служебные каталоги не принимаются.
* **Проектный режим.** Главный запрос (бриф) → руководитель предлагает карту из 3–8
  направлений → «Запустить» → направления идут по очереди, соблюдая зависимости.
* **Диалог по запросу.** Уточнение уходит руководителю комнаты, он передаёт нужным
  участникам и отвечает в ленте.
* **Дальше, отчёты, журнал.** Шаги с владельцем и сроком, вопросы к вам, решения;
  интерактивные отчёты (таблицы, графики, связи) с выгрузкой в один HTML-файл.
* **Диктовка.** Микрофон у каждого поля, словарь терминов и автозамены; аудио не покидает
  компьютер.

## Установка одной командой

Windows, PowerShell (Hermes Desktop уже установлен):

```
powershell -NoProfile -ExecutionPolicy Bypass -Command "irm https://raw.githubusercontent.com/vorchun18-svg/team-room-for-hermes/main/install.ps1 | iex"
```

Одна строка скачивает релиз, ставит приложение и модуль «Командные комнаты» в Hermes
Desktop, поднимает сервер и открывает страницу. Повторный запуск безопасен; удаление —
`install.ps1 -Uninstall`. Подробности, параметры и требования — [docs/INSTALL.md](docs/INSTALL.md).

Приложение ставится в `%LOCALAPPDATA%\\TeamRoom` (данные — там же, в `data/`).
Ничего не нужно настраивать: пути, Hermes и Obsidian определяются сами. Если Vault не
найден — приложение работает без зеркала и пишет подсказку `TEAMROOM_VAULT_DIR`;
если не установлен `faster-whisper` — диктовка сообщает об этом, остальное работает.

## Быстрый старт (демо на выдуманных данных)

```
start-demo-room.cmd            # Windows, порт 8778
python tools/mock_server.py --port 8778
```

Откройте <http://127.0.0.1:8778>. Демо ничего не пишет в настоящие доски и Vault.

## Боевой режим

```
python server/app.py --port 8777      # http://127.0.0.1:8777
python tools/team_room_guard.py --dry-run
python tools/install_desktop_plugin.py --help
```

Все пути настраиваются переменными окружения (см. `server/config.py`):
`TEAMROOM_HOME` (по умолчанию `%USERPROFILE%\\TeamRoom`), `TEAMROOM_DATA`,
`TEAMROOM_VAULT_DIR`, `TEAMROOM_PORT`, `TEAMROOM_HERMES_HOME`,
`TEAMROOM_HERMES_CLI`, `TEAMROOM_EXTRA_ROOTS`.

## Документация

| Файл | О чём |
|---|---|
| `docs/USER-GUIDE.md` | как пользоваться, простыми словами |
| `docs/DEMO.md` | демо-стенд на выдуманных данных |
| `docs/AGENT-GUIDE.md` | как в комнате работают ИИ-агенты |
| `docs/API.md` | HTTP API |
| `docs/ARCHITECTURE.md` | устройство сервера и интерфейса |
| `docs/DESIGN-NOTES.md` | решения по интерфейсу |
| `docs/INSTALL.md` | установка одной командой, параметры, удаление |
| `docs/SAFETY-RULES.md` | правила безопасности и песочница |
| `docs/DESKTOP-MODULE.md` | модуль внутри Hermes Desktop |
| `docs/INTEGRATION-HERMES.md` | связка с Hermes (канбан, профили) |
| `reportkit/SPEC.md` | формат отчётов |

## Скриншоты

Демо-стенд на выдуманных данных: кофейня-кондитерская «Лето» в Заозёрье.

| | |
|---|---|
| ![Дальше и вопросы](docs/shots/backlog-1280.png) | ![Карта проекта](docs/shots/roadmap-1280-dark.png) |
| ![Команда](docs/shots/team-780.png) | ![Запрос команде](docs/shots/composer-1280.png) |
| ![Модель участника](docs/shots/room-model-dialog.png) | Диалог комнаты: провайдер, модель и уровень мышления участника — только для этой комнаты. |

## Тесты

```
python -m unittest discover -s tests -p "test_*.py"
node --test tests/
python tools/portability_check.py          # две песочницы: «чистая машина» и копия живой структуры
```

## Лицензия

MIT — см. `LICENSE`. Автор — vorchun.
"""

LICENSE = """MIT License

Copyright (c) 2026 vorchun

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR OTHER DEALINGS IN THE SOFTWARE.
"""

GITIGNORE = """# данные приложения и журналы
data/
*.log

# резервные копии
*.bak
*.bak-*
*.bak.*

# python
__pycache__/
*.py[cod]
.venv/
venv/

# node
node_modules/

# система
.DS_Store
Thumbs.db
"""

#: без него Node считает .js «CommonJS» по package.json родительского каталога
PACKAGE_JSON = """{
  "name": "team-room",
  "version": "%s",
  "private": true,
  "type": "module",
  "description": "Комната команды: локальное приложение для команд агентов Hermes",
  "scripts": {
    "test": "node --test tests/"
  }
}
""" % VERSION


def changelog_text(table: dict) -> str:
    """CHANGELOG репозитория — единственная история версий; в релиз идёт он же."""
    src = ROOT / "CHANGELOG.md"
    if not src.is_file():
        return ""
    text = src.read_text(encoding="utf-8").replace(VERSION_TOKEN, VERSION)
    return apply_table(text, table, {}, "CHANGELOG.md") if table else text


def write_release_files(pkg: Path, shots: list[str], table: dict | None = None) -> None:
    (pkg / "README.md").write_text(README.replace(VERSION_TOKEN, VERSION), encoding="utf-8", newline="\n")
    (pkg / "LICENSE").write_text(LICENSE, encoding="utf-8", newline="\n")
    text = changelog_text(table or {})
    if text:
        (pkg / "CHANGELOG.md").write_text(text, encoding="utf-8", newline="\n")
    (pkg / ".gitignore").write_text(GITIGNORE, encoding="utf-8", newline="\n")
    (pkg / "package.json").write_text(PACKAGE_JSON, encoding="utf-8", newline="\n")


def check_python_syntax(pkg: Path) -> list[str]:
    """После обезличивания каждый .py должен компилироваться.

    Таблица замен работает по тексту и может случайно задеть строковый литерал —
    такой релиз нельзя отдавать: сервер упадёт на импорте. Проверяем сразу.
    """
    problems: list[str] = []
    for path in sorted(pkg.rglob("*.py")):
        rel = path.relative_to(pkg).as_posix()
        if "__pycache__" in rel:
            continue
        try:
            compile(path.read_text(encoding="utf-8"), rel, "exec")
        except (SyntaxError, UnicodeDecodeError, ValueError) as exc:
            line = getattr(exc, "lineno", None)
            problems.append("%s%s: %s" % (rel, ":%s" % line if line else "", exc))
    return problems


def check_changelog(pkg: Path) -> list[str]:
    """CHANGELOG обязан начинаться разделом текущей версии — иначе релиз «без истории»."""
    path = pkg / "CHANGELOG.md"
    if not path.is_file():
        return ["нет CHANGELOG.md"]
    text = path.read_text(encoding="utf-8")
    if "## %s " % VERSION not in text and "## %s\n" % VERSION not in text:
        return ["в CHANGELOG.md нет раздела «## %s»" % VERSION]
    return []


# --------------------------------------------------------------------- архивы
#: единая дата файлов в архиве: сборка воспроизводима байт в байт
ARCHIVE_STAMP = (2026, 1, 1, 0, 0, 0)


def make_archives(pkg: Path, out_dir: Path) -> dict:
    """`team-room-<version>.zip` и `.tar.gz` — ровно то, что качает установщик.

    Внутри архива одна папка `team-room-<version>/` (установщики ищут в ней `server/app.py`).
    Порядок файлов сортирован, даты фиксированы — повторная сборка даёт тот же архив.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    files = sorted(p for p in pkg.rglob("*")
                   if p.is_file() and ".git" not in p.relative_to(pkg).parts)
    rels = [p.relative_to(pkg).as_posix() for p in files]

    zip_path = out_dir / (PKG_NAME + ".zip")
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for rel, path in zip(rels, files):
            info = zipfile.ZipInfo(PKG_NAME + "/" + rel, date_time=ARCHIVE_STAMP)
            info.compress_type = zipfile.ZIP_DEFLATED
            mode = 0o755 if rel.endswith((".sh", ".cmd", ".ps1")) else 0o644
            info.external_attr = (0o100000 | mode) << 16
            zf.writestr(info, path.read_bytes())

    tar_path = out_dir / (PKG_NAME + ".tar.gz")
    with gzip.GzipFile(tar_path, "wb", mtime=0) as gz:
        with tarfile.open(fileobj=gz, mode="w") as tf:
            for rel, path in zip(rels, files):
                info = tf.gettarinfo(str(path), arcname=PKG_NAME + "/" + rel)
                info.mtime = 0
                info.uid = info.gid = 0
                info.uname = info.gname = ""
                if rel.endswith((".sh", ".cmd", ".ps1")):
                    info.mode = 0o755
                with path.open("rb") as fh:
                    tf.addfile(info, fh)

    problems: list[str] = []
    # Установщики качают стабильное имя (без версии) — публикуем оба: с версией
    # (каноническое, для прямой ссылки на выпуск) и без неё (для releases/latest/download).
    aliases = {"zip": out_dir / "team-room.zip", "tar": out_dir / "team-room.tar.gz"}
    for src, alias in ((zip_path, aliases["zip"]), (tar_path, aliases["tar"])):
        try:
            shutil.copy2(src, alias)
        except OSError as exc:
            problems.append("не скопировал %s: %s" % (alias.name, exc))

    try:
        with zipfile.ZipFile(zip_path) as zf:
            bad = zf.testzip()
            names = set(zf.namelist())
    except (OSError, zipfile.BadZipFile) as exc:
        problems.append("zip не читается: %s" % exc)
        names = set()
    if bad:
        problems.append("zip повреждён в %s" % bad)
    need = ["VERSION", "install.ps1", "server/app.py", "docs/RELEASE-TAG.txt"]
    for extra in ("install.sh", "docs/INSTALL.md"):
        if (pkg / extra).is_file():
            need.append(extra)
    for item in need:
        if names and PKG_NAME + "/" + item not in names:
            problems.append("в архиве нет %s" % item)
    if names and len(names) != len(files):
        problems.append("в архиве %d файлов, в папке %d" % (len(names), len(files)))
    return {"zip": str(zip_path), "tar": str(tar_path),
            "zip_alias": str(aliases["zip"]), "tar_alias": str(aliases["tar"]),
            "zip_bytes": zip_path.stat().st_size if zip_path.is_file() else 0,
            "tar_bytes": tar_path.stat().st_size if tar_path.is_file() else 0,
            "files": len(files), "problems": problems}


# ------------------------------------------------------------ проверки
#: признаки путей автора: релиз обязан быть переносимым на чужую машину
#: логин владельца собираем из частей, чтобы не держать литерал в исходниках
OWNER_LOGIN = "vorchun" + "18"
FORBIDDEN_PATHS = (
    ("личный путь C:\\TeamRoom", re.compile(r"[A-Za-z]:[\\/]{1,4}Hermes\b")),
    ("имя пользователя Windows", re.compile(r"\bMarks\b")),
    ("логин владельца", re.compile(OWNER_LOGIN + r"(?!-svg/team-room-for-hermes)")),
    ("путь /c/Users/…", re.compile(r"/c/Users/[A-Za-z]")),
    ("профиль C:\\Users\\<имя>", re.compile(r"[A-Za-z]:[\\/]{1,4}Users[\\/]{1,4}(?!User\b|Public\b|Default\b|All Users\b)[A-Za-zА-Яа-я]")),
)
#: не текстовые файлы: пути в них не ищем
PATH_SCAN_SKIP_EXT = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".ico", ".woff", ".woff2",
                      ".ttf", ".db", ".sqlite", ".zip", ".pdf", ".pyc"}


def scan_absolute_paths(pkg: Path) -> list[dict]:
    """grep по релизу: ни одного пути автора (машина, логин, профиль Windows)."""
    found: list[dict] = []
    for path in sorted(p for p in pkg.rglob("*") if p.is_file()):
        if ".git" in path.parts or path.suffix.lower() in PATH_SCAN_SKIP_EXT:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        rel = path.relative_to(pkg).as_posix()
        for lineno, line in enumerate(text.splitlines(), 1):
            for name, pattern in FORBIDDEN_PATHS:
                if pattern.search(line):
                    found.append({"file": rel, "line": lineno, "what": name,
                                  "text": line.strip()[:160]})
    return found


def run(cmd: list[str], cwd: Path, timeout: int = 600,
        env: dict | None = None) -> tuple[int, str]:
    try:
        proc = spawn.run(cmd, cwd=str(cwd), capture_output=True, text=True,
                         encoding="utf-8", errors="replace", timeout=timeout, env=env)
        return proc.returncode, (proc.stdout or "") + (proc.stderr or "")
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 127, str(exc)


def test_workdir() -> Path:
    """Куда копировать релиз для прогона тестов — ВНЕ %LOCALAPPDATA%/hermes.

    Тесты `test_project_dir` / `test_safety` запрещают писать в служебные каталоги
    Hermes, а сам релиз по заданию лежит в `.../hermes/cache/scratch/release`.
    Поэтому тесты идут на временной копии в системном TEMP и удаляются после.
    """
    base = os.environ.get("TEMP") or os.environ.get("TMP") or tempfile.gettempdir()
    return Path(base) / "teamroom-release-test"


def test_env() -> dict:
    """Окружение прогона: без унаследованных TEAMROOM_* (тесты не должны уйти в чужие папки)."""
    return {k: v for k, v in os.environ.items() if not k.startswith("TEAMROOM_")}


def prepare_test_copy(pkg: Path) -> Path:
    work = test_workdir()
    if work.exists():
        force_rmtree(work)
    shutil.copytree(pkg, work,
                    ignore=shutil.ignore_patterns(".git", "__pycache__", "node_modules", ".pytest_cache"))
    return work


def python_exe() -> str:
    return sys.executable


def node_exe() -> str | None:
    env = os.environ.get("TEAMROOM_NODE")
    if env and Path(env).is_file():
        return env
    known = local_appdata() / "hermes" / "tools" / "node-26.12.1-win32-x64" / "node.exe"
    if known.is_file():
        return str(known)
    return shutil.which("node")


RUNNER = '''
import sys, unittest
sys.path.insert(0, ".")
loader = unittest.TestLoader()
suite = loader.discover("tests", pattern="test_*.py", top_level_dir="tests")

def walk(s):
    for t in s:
        if isinstance(t, unittest.TestSuite):
            yield from walk(t)
        else:
            yield t

all_tests = list(walk(suite))
keep = [t for t in all_tests if "test_guard" not in type(t).__module__]
print("тестов: %d, запускаю без test_guard: %d" % (len(all_tests), len(keep)), flush=True)
result = unittest.TextTestRunner(verbosity=1).run(unittest.TestSuite(keep))
print("ИТОГ: run=%d failures=%d errors=%d skipped=%d" % (
    result.testsRun, len(result.failures), len(result.errors), len(result.skipped)))
sys.exit(0 if result.wasSuccessful() else 1)
'''


def run_tests(pkg: Path) -> dict:
    """unittest (кроме test_guard: он лезет в настоящий Hermes) и node --test.

    Прогон идёт во временной копии вне hermes-home (`test_workdir()`): тесты
    безопасности проекта справедливо бракуют путь внутри `%LOCALAPPDATA%/hermes`.
    """
    out: dict = {}
    env = test_env()
    work = prepare_test_copy(pkg)
    out["_workdir"] = str(work)
    try:
        runner = work / "unittest_runner.py"
        runner.write_text(RUNNER, encoding="utf-8", newline="\n")
        code, text = run([python_exe(), str(runner)], work, timeout=1500, env=env)
        lines = [line for line in text.strip().splitlines() if line.strip()]
        bad = [line.strip() for line in lines if re.match(r"^(FAIL|ERROR): ", line)]
        tail = [line.strip() for line in lines
                if re.match(r"^(тестов:|ИТОГ:|Ran |FAILED|OK)", line.strip())][-3:]
        out["unittest"] = {"code": code, "tail": tail + bad[:12] or lines[-6:]}
        node = node_exe()
        if not node:
            out["node"] = {"code": 127, "tail": ["node не найден"]}
        else:
            code, text = run([node, "--test", "tests/"], work, timeout=900, env=env)
            kept = [line.strip() for line in text.splitlines()
                    if re.search(r"^[#\u2139] (pass|fail|tests|duration|suites|cancelled|skipped)",
                                 line.strip())]
            out["node"] = {"code": code, "tail": kept[-8:]}
        # переносимость: две песочницы прямо в копии релиза (чистая машина и копия структуры)
        portable = work / "tools" / "portability_check.py"
        if portable.is_file():
            code, text = run([python_exe(), str(portable)], work, timeout=900, env=env)
            lines = [line.strip() for line in text.splitlines() if line.strip()]
            out["portable"] = {"code": code, "tail": lines[-6:]}
        return out
    finally:
        try:
            force_rmtree(work)
        except OSError:
            pass


def git_release(pkg: Path, author_name: str, author_email: str, remote_url: str | None) -> dict:
    steps = []
    config = [
        ["config", "user.name", author_name],
        ["config", "user.email", author_email],
        ["config", "commit.gpgsign", "false"],
        ["config", "tag.gpgsign", "false"],
    ]
    if remote_url:
        # только прописываем origin: никаких push и сетевых вызовов
        config.append(["remote", "add", "origin", remote_url])
    for args in (
        ["init", "-b", "main"],
        *config,
        ["add", "-A"],
        ["commit", "-m", "Team Room %s" % VERSION],
        ["tag", "-a", "v%s" % VERSION, "-m", "Team Room %s" % VERSION],
    ):
        code, text = run(["git"] + args, pkg, timeout=180)
        steps.append({"cmd": "git " + " ".join(args[:3]), "code": code,
                      "out": text.strip().splitlines()[-3:]})
        if code != 0 and args[0] not in ("commit", "tag", "remote"):
            break
    code, text = run(["git", "log", "-1", "--format=%h %an <%ae> %s"], pkg)
    steps.append({"cmd": "git log -1", "code": code, "out": text.strip().splitlines()})
    if remote_url:
        code, text = run(["git", "remote", "-v"], pkg)
        steps.append({"cmd": "git remote -v", "code": code, "out": text.strip().splitlines()})
    return {"steps": steps}


def tree_stats(pkg: Path) -> dict:
    files = [p for p in pkg.rglob("*") if p.is_file()]
    total = 0
    for p in files:
        try:
            total += p.stat().st_size
        except OSError:
            pass
    per_dir: dict[str, int] = {}
    for p in files:
        rel = p.relative_to(pkg).parts[0]
        per_dir[rel] = per_dir.get(rel, 0) + 1
    return {"files": len(files), "bytes": total, "mb": round(total / 1048576, 2),
            "per_dir": dict(sorted(per_dir.items()))}


# ----------------------------------------------------------------------- сборка
def build(args) -> int:
    table = load_table()
    pkg = guard_paths()

    print("Релиз %s" % PKG_NAME)
    print("Источник: %s" % ROOT)
    print("Цель:     %s" % pkg)

    if pkg.exists():
        print("  удаляю прошлую сборку")
        force_rmtree(pkg)
    pkg.mkdir(parents=True)

    copied = 0
    for name in COPY_DIRS:
        n = copy_tree(ROOT / name, pkg / name)
        print("  %-14s %4d файлов" % (name + "/", n))
        copied += n
    tools_dir = pkg / "tools"
    missing = []
    for name in COPY_TOOLS:
        if copy_file(ROOT / "tools" / name, tools_dir / name):
            copied += 1
        else:
            missing.append(name)
    print("  %-14s %4d файлов" % ("tools/", len(COPY_TOOLS) - len(missing)))
    (pkg / "docs").mkdir(exist_ok=True)
    docs_missing = []
    docs_optional_missing = []
    for name in COPY_DOCS:
        if copy_file(ROOT / "docs" / name, pkg / "docs" / name):
            copied += 1
        elif name in OPTIONAL_DOCS:
            docs_optional_missing.append("docs/" + name)
        else:
            docs_missing.append(name)
    print("  %-14s %4d файлов" % ("docs/", len(COPY_DOCS) - len(docs_missing)))
    for name in ("start-demo-room.cmd", "start-team-room.cmd", "stop-team-room.cmd"):
        if copy_file(ROOT / name, pkg / name):
            copied += 1
    # установка: install.ps1 + bin/ (лаунчеры) — их пишет установщик, может ещё не быть
    install_missing: list[str] = list(docs_optional_missing)
    for name in COPY_ROOT_OPTIONAL:
        if copy_file(ROOT / name, pkg / name):
            copied += 1
        else:
            install_missing.append(name)
    for name in COPY_OPTIONAL_DIRS:
        if (ROOT / name).is_dir():
            n = copy_tree(ROOT / name, pkg / name)
            print("  %-14s %4d файлов" % (name + "/", n))
            copied += n
        else:
            install_missing.append(name + "/")
    shots = copy_shots(pkg)
    copied += len(shots)
    print("  %-14s %4d файлов (сжатые)" % ("docs/shots/", len(shots)))

    # CI и релизные workflow: без них репозиторий не проверяется и не собирает артефакты.
    workflows = ROOT / ".github" / "workflows"
    if workflows.is_dir():
        n = copy_tree(workflows, pkg / ".github" / "workflows")
        print("  %-14s %4d файлов" % (".github/workflows/", n))
        copied += n
    if missing:
        print("  ! не найдены tools: %s" % ", ".join(missing))
    if docs_missing:
        print("  ! не найдены docs: %s" % ", ".join(docs_missing))
    if install_missing:
        print("  ! установка пока не в репозитории: %s (появится от INSTALL)"
              % ", ".join(install_missing))

    problems = patch_config(pkg)
    for p in problems:
        print("  ! config.py: %s" % p)
    print("  config.py: дефолты через TEAMROOM_HOME/%USERPROFILE%")

    report = sanitize_tree(pkg, table)
    print("  обезличено: %d замен в %d файлах, переименовано %d файлов"
          % (report["total"], report["files_touched"], report["renamed"]))
    scaled = scale_fixture_money(pkg)
    if scaled:
        print("  суммы в отчётах пересчитаны в рубли (×%d): %s" % (MONEY_SCALE, ", ".join(scaled)))

    write_release_files(pkg, shots, table)
    copied += 5
    if copy_file(ROOT / VERSION_FILE, pkg / VERSION_FILE):
        copied += 1
    else:
        missing.append(VERSION_FILE)
    syntax_problems = check_python_syntax(pkg)
    problems += syntax_problems
    for p_ in syntax_problems[:5]:
        print("  ! синтаксис после обезличивания: %s" % p_)
    changelog_problems = check_changelog(pkg)
    problems += changelog_problems
    for p in changelog_problems:
        print("  ! CHANGELOG: %s" % p)
    print("  версия: %s (из VERSION)" % VERSION)

    stats = tree_stats(pkg)
    print("  итого: %d файлов, %.2f МБ" % (stats["files"], stats["mb"]))

    scan = None
    paths_bad: list[dict] = []
    scanner = HERE / "release_scan.py"
    if not scanner.is_file():
        scanner = pkg / "tools" / "release_scan.py"
    if scanner.is_file():
        cmd = [python_exe(), str(scanner), str(pkg)]
        if TABLE_PATH.is_file():
            cmd += ["--table", str(TABLE_PATH)]
        code, text = run(cmd, pkg)
        scan = {"code": code, "out": text.strip().splitlines()[-14:]}
        print("\n--- release_scan.py (код %d) ---" % code)
        print(text.strip())

    paths_bad = scan_absolute_paths(pkg)
    print("\n--- пути автора (grep по релизу): %d ---" % len(paths_bad))
    for item in paths_bad[:20]:
        print("  %s:%s  %s: %s" % (item["file"], item["line"], item["what"], item["text"]))
    if not paths_bad:
        print("  абсолютных путей машины автора нет")

    tests = None
    if args.run_tests:
        print("\n--- тесты в копии ---")
        tests = run_tests(pkg)
        if tests.get("_workdir"):
            print("  прогон во временной копии: %s" % tests["_workdir"])
        for key, value in tests.items():
            if key.startswith("_"):
                continue
            print("  %s: код %s" % (key, value["code"]))
            for line in value["tail"][-6:]:
                print("    %s" % line)

    git = None
    if not args.no_git:
        print("\n--- git ---")
        git = git_release(pkg, args.author_name, args.author_email, args.remote_url)
        for step in git["steps"]:
            print("  %s → %s %s" % (step["cmd"], step["code"], " ".join(step["out"])[:120]))

    archive = None
    if not args.no_archive:
        print("\n--- архивы ---")
        out_dir = Path(args.out_dir) if args.out_dir else release_root()
        archive = make_archives(pkg, out_dir)
        print("  %s (%.2f МБ, %d файлов)" % (archive["zip"], archive["zip_bytes"] / 1048576,
                                            archive["files"]))
        print("  %s (%.2f МБ)" % (archive["tar"], archive["tar_bytes"] / 1048576))
        print("  копии для releases/latest/download: %s, %s"
              % (Path(archive["zip_alias"]).name, Path(archive["tar_alias"]).name))
        for item in archive["problems"]:
            print("  ! %s" % item)
        if not archive["problems"]:
            print("  архивы проверены: VERSION, install.ps1, server/app.py на месте")

    summary = {
        "version": VERSION,
        "package": str(pkg),
        "author": {"name": args.author_name, "email": args.author_email},
        "remote": args.remote_url,
        "sanitize": {k: v for k, v in report["counts"].items() if v},
        "sanitize_total": report["total"],
        "renamed": report["renamed"],
        "money_scaled": scaled,
        "shots": shots,
        "stats": stats,
        "scan": scan,
        "tests": tests,
        "git": git,
        "archive": archive,
        "missing_tools": missing,
        "missing_docs": docs_missing,
        "missing_install": install_missing,
        "author_paths": paths_bad,
        "config_problems": problems,
    }
    out = release_root() / "release-report.json"
    out.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print("\nОтчёт: %s" % out)

    bad = bool(problems or missing or docs_missing or paths_bad)
    if install_missing and not args.allow_missing_install:
        bad = True
    if scan and scan["code"] != 0:
        bad = True
    if tests and any(v["code"] != 0 for k, v in tests.items() if not k.startswith("_")):
        bad = True
    if archive and archive["problems"]:
        bad = True
    return 1 if bad else 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Сборка публичного релиза «Комнаты команды» %s" % VERSION)
    parser.add_argument("--run-tests", action="store_true", help="прогнать unittest и node --test в копии")
    parser.add_argument("--no-git", action="store_true", help="не делать git init/коммит/тег")
    parser.add_argument("--no-archive", action="store_true",
                        help="не собирать team-room-<version>.zip и .tar.gz")
    parser.add_argument("--out-dir", default=None,
                        help="куда положить архивы (по умолчанию .../cache/scratch/release)")
    parser.add_argument("--allow-missing-install", action="store_true",
                        help="не считать ошибкой отсутствие install.ps1/bin//docs/INSTALL.md "
                             "(пока установщик их не написал)")
    parser.add_argument("--author-name", default="Team Room",
                        help="имя автора коммита (по умолчанию Team Room)")
    parser.add_argument("--author-email", default="noreply@example.com",
                        help="почта автора коммита (по умолчанию noreply@example.com)")
    parser.add_argument("--remote-url", default=None,
                        help="прописать origin (без push), например https://github.com/<логин>/team-room.git")
    parser.add_argument("--print-package", action="store_true",
                        help="только напечатать путь папки релиза (для CI, ничего не собирает)")
    args = parser.parse_args(argv)
    if args.print_package:
        print(guard_paths())
        return 0
    return build(args)


if __name__ == "__main__":
    sys.exit(main())
