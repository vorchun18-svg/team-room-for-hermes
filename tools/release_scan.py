#!/usr/bin/env python3
"""Проверка публичного релиза: нет ли в нём секретов и личных данных.

Что ищет:

1. **Секреты** — ключи (`sk-`, `ghp_`, `sk-ant-`, AWS), `OLLAMA_API_KEY=`,
   присвоение `password`/`token`/`api_key`, заголовок `Bearer` с токеном,
   приватные ключи;
2. **Личное** — шаблоны из `tools/release_sanitize.json` (`scan_patterns`):
   личные пути, имя пользователя, логин, почты `@mail.ru`/`@gmail.com`, названия
   компаний и брендов, имена демо-профилей;
3. **Мусор и утечки файлов** — `.env*`, `auth.json`, `credentials.json`,
   `*.db`, `*.sqlite`, `*.session`, `*.pem`, `*.key`;
4. **Крупные и неожиданные бинарники** — файлы > 1 МБ и бинарники, которых
   в релизе быть не должно. Исключение — точные пути из `scan_allow_binaries`
   таблицы (демо-аудио `fixtures/dictation-ru.wav` для e2e); маски вида `*.wav`
   намеренно не поддерживаются.

Отчёт: «найдено N». Код возврата: 0 — чисто, 1 — есть находки, 2 — ошибка.

Примеры:
    python tools/release_scan.py
    python tools/release_scan.py --path C:/.../team-room-1.0.0 --verbose

Только stdlib.
"""

from __future__ import annotations

import argparse
import fnmatch
import json
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
TABLE_PATH = HERE / "release_sanitize.json"

ALLOWED_BINARY_EXT = {
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".ico", ".svg",
    ".ttf", ".otf", ".woff", ".woff2", ".pdf",
}
MAX_BYTES = 1048576
SKIP_DIRS = {".git", "__pycache__", "node_modules", ".pytest_cache"}

#: если таблицы release_sanitize.json рядом нет (её нет в публичном релизе),
#: проверяем хотя бы секреты — по этому встроенному набору
BUILTIN_SECRET_RULES = (
    ("секрет: ключ OpenAI sk-", r"\bsk-[A-Za-z0-9_-]{20,}"),
    ("секрет: токен GitHub", r"\bgh[pousr]_[A-Za-z0-9]{20,}"),
    ("секрет: ключ Anthropic", r"\bsk-ant-[A-Za-z0-9_-]{16,}"),
    ("секрет: OLLAMA_API_KEY=", r"""OLLAMA_API_KEY\s*[=:]\s*["']?[A-Za-z0-9_\-]{16,}"""),
    ("секрет: присвоение пароля", r"""(?i)(?:password|passwd|pwd)\s*[=:]\s*["'][^"'\s]{6,}["']"""),
    ("секрет: присвоение токена", r"""(?i)(?:api[_-]?key|access[_-]?token|secret[_-]?key)\s*[=:]\s*["'][^"'\s]{12,}["']"""),
    ("секрет: заголовок Bearer с токеном", r"Bearer\s+[A-Za-z0-9._\-]{20,}"),
    ("секрет: приватный ключ", r"-----BEGIN (?:RSA |OPENSSH |EC |PGP )?PRIVATE KEY-----"),
)

#: заведомо учебные токены в тестах — не находка (работает и без таблицы)
BUILTIN_ALLOW = (
    ("tests/*", r"sk-[A-Za-z]{3,}[0-9]{4}"),
)

#: демо-аудио, которое осознанно лежит в релизе: синтезированная фраза для e2e.
#: Только точный путь — маски `*.wav` не допускаются никогда.
BUILTIN_ALLOW_BINARIES = ("fixtures/dictation-ru.wav",)


def find_package() -> Path:
    """Если запустили без --path: ищем собранный пакет в cache/scratch/release."""
    local = Path(__import__("os").environ.get("LOCALAPPDATA") or (Path.home() / "AppData" / "Local"))
    root = local / "hermes" / "cache" / "scratch" / "release"
    found = sorted(p for p in root.glob("team-room-*") if p.is_dir())
    return found[-1] if found else ROOT


def load_table(path: Path) -> dict:
    with path.open("r", encoding="utf-8") as fh:
        return json.load(fh)


def iter_files(pkg: Path, skip_dirs: set[str]):
    for path in sorted(pkg.rglob("*")):
        if not path.is_file():
            continue
        if any(part in skip_dirs for part in path.relative_to(pkg).parts[:-1]):
            continue
        yield path


def is_binary(path: Path) -> bool:
    try:
        chunk = path.open("rb").read(8192)
    except OSError:
        return False
    if b"\x00" in chunk:
        return True
    text = chunk.decode("utf-8", "replace")
    # обрывок многобайтового символа на границе чтения — не признак бинарника
    return text.count("\ufffd") > max(8, len(text) // 200)


def snippet(line: str, limit: int = 120) -> str:
    text = line.strip().replace("\t", " ")
    return text[:limit] + ("…" if len(text) > limit else "")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Поиск секретов и личных данных в собранном релизе")
    parser.add_argument("path", nargs="?", default=None, help="папка релиза (по умолчанию — последняя сборка)")
    parser.add_argument("--path", dest="path_opt", default=None, help="то же явно")
    parser.add_argument("--table", default=str(TABLE_PATH), help="таблица обезличивания")
    parser.add_argument("--verbose", action="store_true", help="показывать все находки по строкам")
    parser.add_argument("--max-findings", type=int, default=200, help="сколько строк показать")
    args = parser.parse_args(argv)

    pkg = Path(args.path_opt or args.path or find_package()).resolve()
    if not pkg.is_dir():
        print("Нет такой папки: %s" % pkg)
        return 2

    table_path = Path(args.table)
    table: dict = {}
    if table_path.is_file():
        try:
            table = load_table(table_path)
        except (OSError, ValueError) as exc:
            print("Не прочитал таблицу %s: %s" % (table_path, exc))
            return 2
    else:
        print("Таблицы %s нет — проверяю только секреты (встроенный набор)." % table_path)

    skip_dirs = set(table.get("scan_skip_dirs") or SKIP_DIRS) | SKIP_DIRS
    skip_files = {name.lower() for name in table.get("scan_skip_files", [])}
    max_bytes = int(table.get("scan_max_bytes") or MAX_BYTES)

    rules: list[tuple[str, re.Pattern, tuple[str, ...]]] = []
    for rule in table.get("scan_secret_patterns", []):
        rules.append(("секрет: " + rule["name"], re.compile(rule["pattern"]), ()))
    for rule in table.get("scan_patterns", []):
        # paths (необязательно) ограничивает правило файлами по маске
        rules.append(("личное: " + rule["name"], re.compile(rule["pattern"]),
                      tuple(rule.get("paths") or ())))
    if not table.get("scan_secret_patterns"):
        for name, pattern in BUILTIN_SECRET_RULES:
            rules.append((name, re.compile(pattern), ()))

    allow_rules = [(item["path"], re.compile(item["pattern"])) for item in table.get("scan_allow", [])]
    allow_rules += [(glob, re.compile(pattern)) for glob, pattern in BUILTIN_ALLOW]
    #: разрешённые бинарники — ТОЛЬКО точные пути (например демо-аудио для e2e),
    #: не маски *.wav: любой другой wav остаётся находкой
    allow_binaries = set(BUILTIN_ALLOW_BINARIES)
    allow_binaries |= {str(name).replace("\\", "/") for name in table.get("scan_allow_binaries", [])}

    deny_files = {n.lower() for n in table.get("scan_deny_files", [])}
    deny_ext = {e.lower() for e in table.get("scan_deny_ext", [])}

    findings: list[tuple[str, str, str]] = []  # (вид, файл, что)
    allowed_binaries: list[str] = []
    scanned = 0
    total_bytes = 0

    for path in iter_files(pkg, skip_dirs):
        rel = path.relative_to(pkg).as_posix()
        try:
            size = path.stat().st_size
        except OSError:
            continue
        total_bytes += size
        low = path.name.lower()

        if low in deny_files or low.startswith(".env"):
            findings.append(("запрещённый файл", rel, path.name))
            continue
        if path.suffix.lower() in deny_ext:
            findings.append(("запрещённое расширение", rel, path.name))
            continue
        if rel.lower() in skip_files:
            continue
        if size > max_bytes:
            findings.append(("крупный файл", rel, "%.1f МБ" % (size / 1048576)))

        binary = is_binary(path)
        if binary:
            if path.suffix.lower() not in ALLOWED_BINARY_EXT:
                if rel in allow_binaries:
                    allowed_binaries.append(rel)
                else:
                    findings.append(("неожиданный бинарник", rel, "%d байт" % size))
            continue

        scanned += 1
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for number, line in enumerate(text.splitlines(), 1):
            if any(fnmatch.fnmatch(rel, glob) and pattern.search(line) for glob, pattern in allow_rules):
                continue
            for kind, pattern, rule_paths in rules:
                if rule_paths and not any(fnmatch.fnmatch(rel, glob) for glob in rule_paths):
                    continue
                if pattern.search(line):
                    findings.append((kind, rel, "строка %d: %s" % (number, snippet(line))))
                    break

    by_kind: dict[str, int] = {}
    for kind, _, _ in findings:
        by_kind[kind] = by_kind.get(kind, 0) + 1

    print("ПРОВЕРКА РЕЛИЗА: %s" % pkg)
    print("  файлов проверено на текст: %d, всего %.2f МБ" % (scanned, total_bytes / 1048576))
    if allowed_binaries:
        print("  разрешённые бинарники по списку: %s" % ", ".join(sorted(allowed_binaries)))
    if findings:
        limit = max(1, args.max_findings)
        for kind, rel, what in findings[:limit]:
            print("  [%s] %s — %s" % (kind, rel, what))
        if len(findings) > limit:
            print("  … и ещё %d" % (len(findings) - limit))
        print("  по видам: %s" % ", ".join("%s × %d" % (k, v) for k, v in sorted(by_kind.items())))
    print("найдено %d" % len(findings))
    if findings:
        print("РЕЛИЗ НЕ ЧИСТ: исправьте таблицу release_sanitize.json и пересоберите.")
        return 1
    print("Чисто: секретов, личных данных и лишних файлов не найдено.")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
