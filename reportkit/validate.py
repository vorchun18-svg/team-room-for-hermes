#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Валидатор и нормализатор интерактивных отчётов «Комнаты команды» (reportkit).

Публичный интерфейс:
    validate(spec) -> (normalized, errors, warnings)
    load_file(path) -> (normalized, errors, warnings)
    summarize(spec) -> {"blocks": N, "rows": N, "nodes": N}

CLI:
    python validate.py report.json                  # проверить (exit 0/1)
    python validate.py report.json --write out.json # сохранить нормализованный
    python validate.py report.json --quiet          # только ошибки
    python validate.py report.json --json           # машиночитаемый вывод

Ошибки (errors) = отчёт показывать нельзя. Предупреждения (warnings) =
данные мягко исправлены/обрезаны, показывать можно.
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

try:  # import reportkit.validate
    from ._schema import (BLOCK_TYPES, COL_TYPES, MAX_BLOCKS, MAX_GRAPH_EDGES, MAX_GRAPH_NODES,
                          MAX_NEST, MAX_STATS, MAX_TABLE_ROWS, MAX_TEXT, MAX_TITLE, ROOT_FIELDS,
                          STATUSES, TONES, VERSION, Ctx, kind, s)
    from ._blocks import blocks as validate_blocks
except ImportError:  # python reportkit/validate.py
    from _schema import (BLOCK_TYPES, COL_TYPES, MAX_BLOCKS, MAX_GRAPH_EDGES, MAX_GRAPH_NODES,
                         MAX_NEST, MAX_STATS, MAX_TABLE_ROWS, MAX_TEXT, MAX_TITLE, ROOT_FIELDS,
                         STATUSES, TONES, VERSION, Ctx, kind, s)
    from _blocks import blocks as validate_blocks


def validate(spec: dict):
    """Проверить и нормализовать отчёт.

    Возвращает (normalized, errors, warnings). При непустом errors normalized
    всё равно пригоден для отладки, но показывать отчёт нельзя.
    """
    ctx = Ctx()
    if not isinstance(spec, dict):
        ctx.err("", f"корень отчёта должен быть объектом {{...}}, получено {kind(spec)}")
        return {}, ctx.errors, ctx.warnings

    out = {}
    for k in sorted(set(spec) - ROOT_FIELDS):
        ctx.warn(k, "неизвестное поле отчёта — отброшено")

    if "version" not in spec:
        ctx.warn("version", "нет поля version — принято 1")
    else:
        v = spec.get("version")
        if isinstance(v, str) and v.strip().isdigit():
            ctx.warn("version", f"version задан строкой ({v!r}) — приведено к 1")
        elif v != VERSION:
            ctx.err("version", f"поддерживается только version=1, получено {v!r}")
    out["version"] = VERSION

    out["title"] = s(ctx, spec, "title", "", req=True)
    sub = s(ctx, spec, "subtitle", "", req=False)
    if sub:
        out["subtitle"] = sub
    summ = s(ctx, spec, "summary", "", req=False, maxlen=2000)
    if summ:
        out["summary"] = summ

    out["blocks"] = validate_blocks(ctx, spec.get("blocks"), "blocks", 1)
    if not out["blocks"] and not ctx.errors:
        ctx.err("blocks", "список блоков пуст — показывать нечего (нужен хотя бы один блок)")
    warn_mixed_currency(ctx, out)
    return out, ctx.errors, ctx.warnings


# --- валюта ---------------------------------------------------------------
CURRENCY_NAMES = {"RUB": "₽", "BRL": "R$", "USD": "$"}


def currency_families(spec) -> list:
    """Какие валюты упомянуты в отчёте: RUB (₽/руб), BRL (R$/BRL), USD ($/USD).

    Смотрим все строки, включая подписи колонок, unit графиков и текст: подсказка
    графика «190 ₽» рядом с «R$ 81» — та же ошибка, что и таблица «Сумма, ₽».
    """
    found = set()

    def visit(value):
        if isinstance(value, dict):
            for item in value.values():
                visit(item)
        elif isinstance(value, list):
            for item in value:
                visit(item)
        elif isinstance(value, str):
            text = value
            if re.search(r"R\s*\$|\bBRL\b", text, re.I):
                found.add("BRL")
            if "₽" in text or re.search(r"(?<![A-Za-z])RUB(?![A-Za-z])|руб", text, re.I):
                found.add("RUB")
            if re.search(r"(?<![A-Za-z])\$(?![A-Za-z])|\bUSD\b|долл", text, re.I):
                found.add("USD")

    visit(spec)
    return sorted(found)


def warn_mixed_currency(ctx: Ctx, spec) -> None:
    """Предупреждение (не ошибка): в одном отчёте разные валюты."""
    families = currency_families(spec)
    if len(families) > 1:
        names = ", ".join(CURRENCY_NAMES.get(f, f) for f in families)
        ctx.warn("currency", f"в отчёте смешаны валюты: {names} — оставьте одну валюту "
                              "и один порядок величин, иначе итоги не сходятся")


def load_file(path):
    """Прочитать JSON-файл и провалидировать. Ошибки чтения попадают в errors."""
    p = Path(path)
    try:
        raw = p.read_text(encoding="utf-8-sig")
    except OSError as e:
        return {}, [f"файл: не удалось прочитать {p} — {e.strerror or e}"], []
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        return {}, [f"файл: некорректный JSON — строка {e.lineno}, позиция {e.colno}: {e.msg}"], []
    return validate(data)


def summarize(spec) -> dict:
    """Счётчики для CLI/индекса: блоки, строки таблиц, узлы графов."""
    st = {"blocks": 0, "rows": 0, "nodes": 0}

    def walk(items):
        if not isinstance(items, list):
            return
        for b in items:
            if not isinstance(b, dict):
                continue
            st["blocks"] += 1
            t = b.get("type")
            if t == "table":
                st["rows"] += len(b.get("rows") or [])
            elif t == "graph":
                st["nodes"] += len(b.get("nodes") or [])
            elif t == "tabs":
                for tb in b.get("tabs") or []:
                    walk(tb.get("blocks"))
            elif t == "accordion":
                for it in b.get("items") or []:
                    walk(it.get("blocks"))
            elif t == "columns":
                for cl in b.get("cols") or []:
                    walk(cl)

    if isinstance(spec, dict):
        walk(spec.get("blocks"))
    return st


def format_report(spec, errors, warnings, source=""):
    """Читаемый русский отчёт для CLI."""
    lines = []
    title = spec.get("title") if isinstance(spec, dict) else None
    head = f"Отчёт: «{title}»" if title else "Отчёт: (без названия)"
    if source:
        head += f" — {source}"
    lines.append(head)
    st = summarize(spec) if spec else {"blocks": 0, "rows": 0, "nodes": 0}
    lines.append(f"Блоков: {st['blocks']} (лимит {MAX_BLOCKS}), строк таблиц: {st['rows']} "
                 f"(лимит {MAX_TABLE_ROWS}), узлов графов: {st['nodes']} (лимит {MAX_GRAPH_NODES})")
    if errors:
        lines.append("")
        lines.append(f"ОШИБКИ ({len(errors)}) — отчёт не будет показан:")
        lines.extend(f"  {i}. {e}" for i, e in enumerate(errors, 1))
    if warnings:
        lines.append("")
        lines.append(f"ПРЕДУПРЕЖДЕНИЯ ({len(warnings)}) — исправлено автоматически:")
        lines.extend(f"  - {w}" for w in warnings)
    lines.append("")
    if errors:
        lines.append("Итог: ЕСТЬ ОШИБКИ. Исправьте их и запустите проверку снова "
                     "(адрес поля в начале строки, например blocks[3].series[0].data).")
    else:
        lines.append("Итог: OK — отчёт корректен."
                     + (" Предупреждения не мешают показу." if warnings else ""))
    return "\n".join(lines)


def main(argv=None):
    import argparse

    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass

    ap = argparse.ArgumentParser(description="Проверка и нормализация отчёта reportkit (JSON).")
    ap.add_argument("file", help="путь к report.json")
    ap.add_argument("--write", metavar="PATH", help="сохранить нормализованный JSON")
    ap.add_argument("--quiet", action="store_true", help="только ошибки, без предупреждений")
    ap.add_argument("--json", action="store_true", help="машиночитаемый вывод")
    args = ap.parse_args(argv)

    spec, errors, warnings = load_file(args.file)
    if args.quiet:
        warnings = []
    if args.json:
        print(json.dumps({"ok": not errors, "errors": errors, "warnings": warnings,
                          "stats": summarize(spec)}, ensure_ascii=False, indent=2))
    else:
        print(format_report(spec, errors, warnings, source=Path(args.file).name))
    if args.write and not errors:
        Path(args.write).write_text(json.dumps(spec, ensure_ascii=False, indent=2), encoding="utf-8")
        if not args.json:
            print(f"Нормализованный JSON записан: {args.write}")
    return 0 if not errors else 1


if __name__ == "__main__":
    sys.exit(main())
