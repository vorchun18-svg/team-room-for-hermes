# -*- coding: utf-8 -*-
"""W29-P10 — инвентаризация ВСЕХ путей создания карточек в `server/*.py`.

Первопричина (Vault `RootCause-2026-11-20-model-selection.md`): выбор модели комнаты
применялся постфактум, и каждый новый способ создать карточку снова ломал правило. Здесь
не «список известных мест», а сканер кода: тест сам находит каждый вызов создания
карточки и требует одно из двух —

* аргументы модели собираются в ТОМ ЖЕ месте (`*model_args` / `--model` / `opt("model")`), или
* место внесено в ``EXCEPTIONS`` с обоснованием (и обоснование не устарело).

Появился новый путь без модели и без обоснования — тест красный. Исключение, которого
больше нет в коде, — тоже красный (чтобы список не гнил).

    python -m unittest tests.test_card_creation_paths -v
"""
from __future__ import annotations

import ast
import re
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

SERVER = ROOT / "server"
#: чем список аргументов `hermes kanban create` может доказать, что модель передана
MODEL_MARKERS = ("--model", "--provider", "model_args", "model_flags")

#: Пути создания карточек, которым пин НЕ нужен — с причиной. Ключ: (файл, функция).
EXCEPTIONS = {
    ("server/engine_write.py", "create_request"):
        "корневая карточка создаётся в triage: исполнителя ещё нет, пин поставить некому; "
        "модель появится при назначении — её поставит триггер БД (server/model_trigger.py) "
        "или сторож",
}


def _server_files() -> list[Path]:
    return sorted(path for path in SERVER.glob("*.py") if path.name != "__init__.py")


def _func_name(node: ast.AST, parents: dict) -> str:
    parent = parents.get(id(node))
    while parent is not None:
        if isinstance(parent, (ast.FunctionDef, ast.AsyncFunctionDef)):
            return str(parent.name)
        parent = parents.get(id(parent))
    return "<модуль>"


def _parents(tree: ast.AST) -> dict:
    out: dict = {}
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            out[id(child)] = node
    return out


def _is_model_args(expr: ast.AST) -> bool:
    """`*model_args` / `*flags` / список с `opt("model", …)` или `"--model"`."""
    if isinstance(expr, ast.Starred):
        return True
    if isinstance(expr, ast.Name) and any(mark in expr.id for mark in ("model", "flags")):
        return True
    if isinstance(expr, ast.Call):
        func = expr.func
        name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", "")
        if name in ("opt", "_opt", "flag"):
            first = expr.args[0] if expr.args else None
            if isinstance(first, ast.Constant) and str(first.value) in ("model", "provider"):
                return True
    return False


def _list_is_create(node: ast.List) -> bool:
    if not node.elts:
        return False
    first = node.elts[0]
    return isinstance(first, ast.Constant) and first.value == "create"


def _has_model(node: ast.List) -> bool:
    for element in node.elts:
        if _is_model_args(element):
            return True
        if isinstance(element, ast.Constant) and str(element.value) in MODEL_MARKERS:
            return True
    return False


def _argv_like(node: ast.List, parents: dict) -> bool:
    """Похоже ли на argv CLI: `args = ["create", …]`, есть флаг `--…` или вызов `*_opt`.

    Отсекает ложные срабатывания вида `"allowed": ["create", "edit", …]` в ответах API.
    """
    for element in node.elts:
        if isinstance(element, ast.Constant) and str(element.value).startswith("--"):
            return True
        if isinstance(element, ast.Starred):
            return True
        if isinstance(element, ast.Call):
            func = element.func
            name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", "")
            if name in ("opt", "_opt", "flag"):
                return True
    parent = parents.get(id(node))
    if isinstance(parent, ast.Assign):
        for target in parent.targets:
            if isinstance(target, ast.Name) and target.id in (
                    "args", "argv", "cmd", "command", "base"):
                return True
    return False


def find_create_sites() -> list[dict]:
    """Все места, где собирается argv создания карточки: `["create", ...]`."""
    sites: list[dict] = []
    for path in _server_files():
        text = path.read_text(encoding="utf-8", errors="replace")
        tree = ast.parse(text, filename=str(path))
        parents = _parents(tree)
        rel = path.relative_to(ROOT).as_posix()
        for node in ast.walk(tree):
            if isinstance(node, ast.List) and _list_is_create(node) and _argv_like(node, parents):
                sites.append({"file": rel, "func": _func_name(node, parents),
                              "line": int(getattr(node, "lineno", 0)),
                              "has_model": _has_model(node),
                              "kind": "argv-create"})
            elif isinstance(node, ast.Call):
                func = node.func
                name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", "")
                if name in ("create_task", "card_create", "kanban_create"):
                    sites.append({"file": rel, "func": _func_name(node, parents),
                                  "line": int(getattr(node, "lineno", 0)),
                                  "has_model": any(_is_model_args(arg) for arg in node.args)
                                  or any(kw.arg in ("model", "model_override") for kw in node.keywords),
                                  "kind": name})
    return sites


class CardCreationInventory(unittest.TestCase):
    def test_every_create_path_passes_model(self):
        """Карточка без модели — только с явным обоснованием в EXCEPTIONS."""
        missing = []
        for site in find_create_sites():
            if site["has_model"]:
                continue
            if (site["file"], site["func"]) in EXCEPTIONS:
                continue
            missing.append("%s:%s (%s) в %s()" % (site["file"], site["line"], site["kind"],
                                                 site["func"]))
        self.assertEqual([], missing,
                         "новые пути создания карточек без модели:\n  " + "\n  ".join(missing))

    def test_exceptions_are_real_and_explained(self):
        """Обоснование не гниёт: каждое исключение должно соответствовать живому месту."""
        sites = {(site["file"], site["func"]) for site in find_create_sites()}
        for key, reason in EXCEPTIONS.items():
            self.assertIn(key, sites, "исключение устарело: %s больше не создаёт карточки" % (key,))
            self.assertGreater(len(reason), 40, "у исключения %s нет внятного обоснования" % (key,))

    def test_no_raw_insert_into_tasks(self):
        """Карточки создаёт штатный движок: своего `INSERT INTO tasks` в приложении нет."""
        pattern = re.compile(r"INSERT\s+INTO\s+tasks\b", re.I)
        hits = []
        for path in _server_files():
            text = path.read_text(encoding="utf-8", errors="replace")
            for number, line in enumerate(text.splitlines(), 1):
                if pattern.search(line) and "teamroom_" not in line:
                    hits.append("%s:%s" % (path.name, number))
        self.assertEqual([], hits, "прямая запись в tasks запрещена: " + ", ".join(hits))

    def test_model_flags_have_single_source(self):
        """`opt("model")` собирается только в `engine_write._model_args` (единый источник)."""
        hits = []
        for path in _server_files():
            if path.name == "engine_write.py":
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
            for number, line in enumerate(text.splitlines(), 1):
                if re.search(r"""opt\(\s*["']model["']""", line):
                    hits.append("%s:%s" % (path.name, number))
        self.assertEqual([], hits, "флаги модели собираются не в engine_write: " + ", ".join(hits))

    def test_note_card_and_child_card_carry_model(self):
        """Два штатных пути (уточнение руководителю и дочерняя карточка) — с моделью."""
        thread_src = (SERVER / "thread.py").read_text(encoding="utf-8")
        self.assertIn("*model_args", thread_src,
                      "карточка «Уточнение» создаётся без модели комнаты")
        self.assertIn("_push_reasoning(cfg, board, card_id", thread_src,
                      "уровень мышления для «Уточнения» не ставится")
        engine_src = (SERVER / "engine_write.py").read_text(encoding="utf-8")
        self.assertIn("*model_args", engine_src, "дочерняя карточка создаётся без модели")


if __name__ == "__main__":
    unittest.main(verbosity=2)
