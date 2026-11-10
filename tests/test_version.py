"""Одна версия на весь продукт и рабочие GitHub Actions.

Проверки:

* `VERSION` в корне существует, валиден и совпадает с тем, что отдают сервер
  (`server/config.py` → `/api/health`), плагин (`PLUGIN_VERSION`) и сборка
  (`tools/build_release.py` → имя архива, тег, `package.json`);
* `CHANGELOG.md` содержит раздел текущей версии (без него сборка релиза падает);
* `.github/workflows/ci.yml` и `release.yml` существуют, содержат нужные шаги,
  имена артефактов соответствуют `VERSION` и в них нет секретов.

Только stdlib: YAML разбирается PyYAML, если он есть; иначе — строгая проверка
структуры по тексту (ключевые строки и отступы), чтобы тест работал на чистом раннере.
"""
from __future__ import annotations

import os
import re
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from server import config as config_mod  # noqa: E402

VERSION_PATH = ROOT / "VERSION"
PLUGIN_PATH = ROOT / "desktop-plugin" / "plugin.js"
BUILD_PATH = ROOT / "tools" / "build_release.py"
WORKFLOWS = ROOT / ".github" / "workflows"
CI_YML = WORKFLOWS / "ci.yml"
RELEASE_YML = WORKFLOWS / "release.yml"

SEMVER = re.compile(r"^\d+\.\d+\.\d+(?:[-+.][0-9A-Za-z.\-+]+)?$")


def read_version() -> str:
    return VERSION_PATH.read_text(encoding="utf-8").strip()


def load_yaml(path: Path):
    """Разбор YAML, если есть PyYAML; иначе None (проверки идут по тексту)."""
    try:
        import yaml  # type: ignore
    except Exception:  # noqa: BLE001
        return None
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def job_steps(doc) -> dict:
    """{job: [step, ...]} из разобранного workflow."""
    return {name: spec.get("steps", []) for name, spec in doc.get("jobs", {}).items()}


def step_text(step: dict) -> str:
    return " ".join(str(step.get(key, "")) for key in ("name", "run", "uses", "with"))


class TestVersionFile(unittest.TestCase):
    def test_file_exists_and_is_valid(self):
        self.assertTrue(VERSION_PATH.is_file(), "нет файла VERSION в корне проекта")
        version = read_version()
        self.assertRegex(version, SEMVER, "VERSION должен быть вида 1.0.0")
        self.assertFalse(version.startswith("v"), "в VERSION не должно быть префикса v")
        self.assertEqual(len(VERSION_PATH.read_text(encoding="utf-8").split()), 1,
                         "в VERSION должна быть ровно одна строка с версией")
        self.assertFalse(VERSION_PATH.read_bytes().startswith(b"\xef\xbb\xbf"), "в VERSION не должно быть BOM")

    def test_no_hardcoded_version_left(self):
        """Число версии живёт только в VERSION — иначе архив и сервер разойдутся."""
        version = read_version()
        for rel in ("server/config.py", "tools/build_release.py", "desktop-plugin/plugin.js"):
            text = (ROOT / rel).read_text(encoding="utf-8")
            self.assertRegex(text, r"PLUGIN_VERSION|VERSION", "%s не упоминает версию" % rel)
            self.assertNotRegex(
                text,
                r'^\s*VERSION\s*=\s*["\']\d+\.\d+\.\d+',
                "%s хардкодит версию числом вместо файла VERSION" % rel,
            )
        # сама цифра в этих файлах встречаться не должна (кроме тестов и CHANGELOG)
        plugin = PLUGIN_PATH.read_text(encoding="utf-8")
        self.assertRegex(plugin, r"PLUGIN_VERSION\s*=\s*['\"]%s['\"]" % re.escape(version))

    def test_server_reports_same_version(self):
        self.assertEqual(config_mod.VERSION, read_version(),
                         "server/config.py отдаёт не ту версию, что в VERSION")

    def test_plugin_version_matches(self):
        found = re.search(r"PLUGIN_VERSION\s*=\s*['\"]([^'\"]+)['\"]", PLUGIN_PATH.read_text(encoding="utf-8"))
        self.assertIsNotNone(found, "в плагине нет PLUGIN_VERSION")
        self.assertEqual(found.group(1), read_version(),
                         "версия плагина должна совпадать с VERSION")

    def test_build_release_uses_version_file(self):
        sys.path.insert(0, str(ROOT / "tools"))
        import build_release  # noqa: E402  (читает VERSION при импорте)

        version = read_version()
        self.assertEqual(build_release.VERSION, version)
        self.assertEqual(build_release.PKG_NAME, "team-room-%s" % version,
                         "имя архива должно быть team-room-<version>")
        source = BUILD_PATH.read_text(encoding="utf-8")
        self.assertIn('VERSION_FILE = "VERSION"', source)
        self.assertIn('PKG_NAME + ".zip"', source)
        self.assertIn('PKG_NAME + ".tar.gz"', source)

    def test_changelog_has_current_version(self):
        path = ROOT / "CHANGELOG.md"
        self.assertTrue(path.is_file(), "нет CHANGELOG.md — релизу нечего рассказать об изменениях")
        text = path.read_text(encoding="utf-8")
        version = read_version()
        self.assertIn("## %s" % version, text, "в CHANGELOG.md нет раздела версии %s" % version)

    def test_readme_mentions_version_source(self):
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        version = read_version()
        self.assertTrue("VERSION" in readme or version in readme,
                        "в README нет ни файла VERSION, ни текущей версии %s" % version)
        self.assertIn("badge.svg", readme, "в README нет бейджа CI")


class TestReleaseTextIntegrity(unittest.TestCase):
    """Обезличивание не должно ломать файлы: CRLF остаётся CRLF, код компилируется."""

    def test_sanitize_keeps_line_endings_and_syntax(self):
        import tempfile

        sys.path.insert(0, str(ROOT / "tools"))
        import build_release  # noqa: E402

        table = {
            "text_ext": [".py", ".ps1"],
            "literals": [{"from": "User", "to": "User"}],
            "regexes": [],
        }
        # CRLF + продолжение выражения через обратный слеш: при удвоении переводов
        # строк между строками появляется пустая, и Python падает с
        # «expected 'else' after if expression» — так ломался собранный релиз.
        source = [
            "x = ('a',",
            "     'User')",
            "label = 'yes' if x and x[0] not in ('b',",
            "                                  'User') \\",
            "    else 'none'",
        ]
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "crlf.py").write_bytes(("\r\n".join(source) + "\r\n").encode("utf-8"))
            (root / "lf.py").write_bytes(b"name = 'User'\n")

            build_release.sanitize_tree(root, table)

            crlf = (root / "crlf.py").read_bytes()
            self.assertNotIn(b"\r\r", crlf, "переводы строк удвоились")
            self.assertIn(b"\r\n", crlf, "CRLF не сохранился")
            self.assertIn(b"User", crlf, "замена не применилась")
            compile(crlf.decode("utf-8"), "crlf.py", "exec")

            self.assertEqual((root / "lf.py").read_bytes(), b"name = 'User'\n",
                             "LF-файл переписан неправильно")


@unittest.skipUnless(WORKFLOWS.is_dir(), "в релизной копии нет .github — проверять нечего")

class TestWorkflows(unittest.TestCase):
    def test_files_exist(self):
        for path in (CI_YML, RELEASE_YML):
            self.assertTrue(path.is_file(), "нет %s" % path.relative_to(ROOT))

    def test_ci_matrix(self):
        text = CI_YML.read_text(encoding="utf-8")
        self.assertIn("windows-latest", text)
        self.assertIn("ubuntu-latest", text)
        for py in ("3.11", "3.13"):
            self.assertIn("'%s'" % py, text.replace('"%s"' % py, "'%s'" % py),
                          "в матрице CI нет Python %s" % py)
        self.assertRegex(text, r"node-version:\s*['\"]?2[2-9]", "нужен Node 22+ (node --test)")

    def test_ci_runs_all_checks(self):
        text = CI_YML.read_text(encoding="utf-8")
        for needle in (
            "unittest discover -s tests",
            "node --test tests/",
            "tools/check_conformance.py",
            "tools/check_fixtures.py",
            "tools/build_release.py",
            "tools/release_scan.py",
        ):
            self.assertIn(needle, text, "в ci.yml нет шага: %s" % needle)

    def test_ci_scripts_exist(self):
        """Каждый python-скрипт, который зовёт CI, действительно есть в репозитории."""
        text = CI_YML.read_text(encoding="utf-8") + RELEASE_YML.read_text(encoding="utf-8")
        for rel in sorted(set(re.findall(r"python (tools/[A-Za-z0-9_]+\.py)", text))):
            self.assertTrue((ROOT / rel).is_file(), "workflow зовёт несуществующий %s" % rel)
        self.assertTrue((ROOT / "tests").is_dir())

    def test_release_on_tag_and_uploads_assets(self):
        text = RELEASE_YML.read_text(encoding="utf-8")
        self.assertRegex(text, r"tags:\s*\[\s*['\"]v\*['\"]\s*\]", "релиз должен запускаться на тег v*")
        self.assertIn("softprops/action-gh-release", text)
        self.assertIn("team-room-", text)
        self.assertIn("--run-tests", text, "релизный архив должен собираться с прогоном тестов")
        self.assertIn("--no-git", text)

    def test_artifact_names_match_version(self):
        version = read_version()
        text = RELEASE_YML.read_text(encoding="utf-8")
        self.assertIn("team-room-${{ needs.build.outputs.version }}.zip", text)
        self.assertIn("team-room-${{ needs.build.outputs.version }}.tar.gz", text)
        self.assertIn("VERSION", text, "релиз должен сверять тег с VERSION")
        self.assertNotIn("team-room-%s.zip" % version, text,
                         "версию в workflow не хардкодим — только из VERSION")

    def test_no_secrets_and_tokens(self):
        """CI не должен требовать секретов, а в файлах не должно быть токенов."""
        patterns = (
            (r"\$\{\{\s*secrets\.", "workflow ссылается на secrets.*"),
            (r"ghp_[A-Za-z0-9]{20,}", "похоже на GitHub-токен"),
            (r"sk-[A-Za-z0-9]{20,}", "похоже на API-ключ"),
            (r"AKIA[0-9A-Z]{16}", "похоже на ключ AWS"),
            (r"-----BEGIN [A-Z ]*PRIVATE KEY-----", "приватный ключ"),
            (r"(?i)\bpassword\s*[:=]\s*\S", "пароль в файле"),
        )
        for path in sorted(WORKFLOWS.glob("*.yml")):
            text = path.read_text(encoding="utf-8")
            for pattern, why in patterns:
                self.assertNotRegex(text, pattern, "%s: %s" % (path.name, why))

    def test_permissions_are_minimal(self):
        for path, expected in ((CI_YML, "contents: read"), (RELEASE_YML, "contents: write")):
            text = path.read_text(encoding="utf-8")
            self.assertIn("permissions:", text, "%s: нужен явный блок permissions" % path.name)
            self.assertIn(expected, text, "%s: ожидалось %s" % (path.name, expected))

    def test_yaml_is_parseable(self):
        for path in (CI_YML, RELEASE_YML):
            raw = path.read_text(encoding="utf-8")
            doc = load_yaml(path)
            if doc is None:
                # PyYAML нет — строгая проверка структуры по тексту
                self.assertIn("jobs:", raw)
                self.assertRegex(raw, r"(?m)^\s{2}[a-z][a-z0-9_-]*:\s*$", "в %s нет job'ов" % path.name)
                self.assertNotIn("\t", raw, "%s: табуляция недопустима в YAML" % path.name)
                continue
            self.assertIsInstance(doc, dict, "%s: верхний уровень — словарь" % path.name)
            self.assertIn("jobs", doc)
            for name, steps in job_steps(doc).items():
                self.assertTrue(steps, "в %s job %s без шагов" % (path.name, name))
                for step in steps:
                    self.assertTrue(step.get("uses") or step.get("run"),
                                    "в %s job %s есть шаг без uses/run" % (path.name, name))


if __name__ == "__main__":
    unittest.main()
