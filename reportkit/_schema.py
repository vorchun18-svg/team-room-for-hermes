# -*- coding: utf-8 -*-
"""Общие константы и примитивы валидатора reportkit (внутренний модуль).

Здесь: лимиты, перечисления, синонимы типов, класс Ctx (ошибки/счётчики)
и маленькие функции проверки полей. Пользовательский вход — validate.py.
"""
from __future__ import annotations

VERSION = 1

# --- лимиты ---------------------------------------------------------------
MAX_BLOCKS = 200          # всего блоков, включая вложенные
MAX_TABLE_ROWS = 5000     # строк в одной таблице
MAX_GRAPH_NODES = 500
MAX_GRAPH_EDGES = 2000
MAX_NEST = 3              # глубина вложенности tabs/accordion/columns
MAX_TEXT = 20000          # символов в одном md-поле
MAX_TITLE = 200           # символов в заголовке/подписи
MAX_STATS = 24            # элементов в stats (13..24 — предупреждение)
WARN_STATS = 12

TONES = ("info", "success", "warn", "danger")
STATUSES = ("done", "active", "next", "blocked")
COL_TYPES = ("text", "number", "money", "percent", "date", "status", "link")
ALIGNS = ("left", "center", "right")
CHART_KINDS = ("bar", "hbar", "line", "area", "pie", "donut", "stacked", "scatter")
GRAPH_LAYOUTS = ("layered", "force", "radial")
GRAPH_DIRECTIONS = ("LR", "TB")
MATRIX_KINDS = ("heat", "swot", "grid")
TOTAL_KINDS = ("sum", "avg", "count")

BLOCK_TYPES = (
    "heading", "text", "callout", "stats", "table", "chart", "graph", "timeline",
    "phases", "compare", "matrix", "checklist", "kanban", "gallery", "code",
    "tabs", "accordion", "columns", "links", "sources",
)

# Синонимы, которые агенты пишут чаще всего: приводим к канону с предупреждением.
ALIASES = {
    "md": "text", "markdown": "text", "paragraph": "text", "paragraphs": "text",
    "kpi": "stats", "kpis": "stats", "metrics": "stats",
    "tables": "table",
    "charts": "chart", "plot": "chart", "diagram": "chart",
    "graphs": "graph", "mindmap": "graph",
    "roadmap": "phases", "stages": "phases", "stage": "phases",
    "timelines": "timeline", "milestones": "timeline",
    "swot": "matrix", "heatmap": "matrix",
    "todo": "checklist", "todos": "checklist", "list": "checklist",
    "board": "kanban", "cards": "kanban",
    "images": "gallery", "image": "gallery", "img": "gallery", "photos": "gallery",
    "pre": "code", "snippet": "code", "codeblock": "code",
    "tab": "tabs", "spoiler": "accordion", "details": "accordion", "faq": "accordion",
    "cols": "columns",
    "link": "links", "references": "sources", "source": "sources",
}

KNOWN_FIELDS = {
    "heading": {"type", "text", "level"},
    "text": {"type", "md"},
    "callout": {"type", "tone", "title", "md"},
    "stats": {"type", "items"},
    "table": {"type", "title", "columns", "rows", "sortable", "filter", "totals", "footnote"},
    "chart": {"type", "kind", "title", "x", "series", "unit", "note"},
    "graph": {"type", "title", "layout", "direction", "nodes", "edges"},
    "timeline": {"type", "items"},
    "phases": {"type", "items"},
    "compare": {"type", "criteria", "options"},
    "matrix": {"type", "kind", "rows", "cols", "cells", "swot"},
    "checklist": {"type", "items"},
    "kanban": {"type", "lanes"},
    "gallery": {"type", "images"},
    "code": {"type", "lang", "text"},
    "tabs": {"type", "tabs"},
    "accordion": {"type", "items"},
    "columns": {"type", "cols", "widths"},
    "links": {"type", "items"},
    "sources": {"type", "items"},
}

ROOT_FIELDS = {"version", "title", "subtitle", "summary", "blocks"}


class Ctx:
    """Состояние одного прогона: ошибки, предупреждения, счётчики."""

    def __init__(self):
        self.errors = []
        self.warnings = []
        self._seen = set()
        self.stats = {"blocks": 0, "rows": 0, "nodes": 0}
        self._limits_hit = set()

    def err(self, path, msg):
        self.errors.append(f"{path}: {msg}" if path else msg)

    def warn(self, path, msg):
        line = f"{path}: {msg}" if path else msg
        if line not in self._seen:
            self._seen.add(line)
            self.warnings.append(line)

    def limit_once(self, key, path, msg):
        if key not in self._limits_hit:
            self._limits_hit.add(key)
            self.err(path, msg)


def fmt_path(path, key):
    return f"{path}.{key}" if path else key


def kind(v):
    if v is None:
        return "null"
    if isinstance(v, bool):
        return "логическое (true/false)"
    if isinstance(v, str):
        return "строка"
    if isinstance(v, (int, float)):
        return "число"
    if isinstance(v, list):
        return "список"
    if isinstance(v, dict):
        return "объект"
    return type(v).__name__


def is_num(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def is_int(v):
    return isinstance(v, int) and not isinstance(v, bool)


def to_num(v):
    """'1 234,5' -> 1234.5; нечисло -> None."""
    if is_num(v):
        return float(v)
    if isinstance(v, str):
        s = (v.strip().replace("\u00a0", "").replace("\u202f", "")
             .replace(" ", "").replace(",", "."))
        try:
            return float(s)
        except ValueError:
            return None
    return None


def stub(reason):
    """Заглушка вместо нераспознанного блока — чтобы JSON остался валидным."""
    return {"type": "callout", "tone": "danger", "title": "Блок не распознан", "md": reason}


# --- примитивы полей ------------------------------------------------------
def s(ctx, obj, key, path, *, req=True, default="", maxlen=MAX_TITLE):
    """Строковое поле: trim, обрезка длинного, мягкое приведение чисел."""
    v = obj.get(key)
    p = fmt_path(path, key)
    if v is None:
        if req:
            ctx.err(p, "обязательное поле отсутствует")
        return default
    if isinstance(v, bool) or not isinstance(v, (str, int, float)):
        ctx.err(p, f"ожидалась строка, получено {kind(v)}")
        return default
    if not isinstance(v, str):
        ctx.warn(p, f"значение приведено к строке (было {kind(v)})")
        v = str(v)
    v = v.strip()
    if not v and req:
        ctx.err(p, "пустая строка")
    if maxlen and len(v) > maxlen:
        ctx.warn(p, f"слишком длинный текст ({len(v)} симв.) — обрезан до {maxlen}")
        v = v[:maxlen].rstrip() + "…"
    return v


def lst(ctx, obj, key, path, *, req=True):
    v = obj.get(key)
    p = fmt_path(path, key)
    if v is None:
        if req:
            ctx.err(p, "обязательное поле отсутствует (ожидался список [...])")
        return []
    if not isinstance(v, list):
        ctx.err(p, f"ожидался список [...], получено {kind(v)}")
        return []
    return v


def enum(ctx, obj, key, path, allowed, default, *, req=False, upper=False):
    v = obj.get(key)
    p = fmt_path(path, key)
    if v is None:
        if req:
            ctx.err(p, f"обязательное поле отсутствует; допустимо: {', '.join(allowed)}")
        return default
    if not isinstance(v, str):
        ctx.err(p, f"ожидалась строка из {', '.join(allowed)}, получено {kind(v)}")
        return default
    v = v.strip().upper() if upper else v.strip().lower()
    if v not in allowed:
        ctx.err(p, f"недопустимое значение {v!r}; допустимо: {', '.join(allowed)}")
        return default
    return v


def num(ctx, obj, key, path, default=None, *, req=False):
    v = obj.get(key)
    p = fmt_path(path, key)
    if v is None:
        if req:
            ctx.err(p, "обязательное поле отсутствует (ожидалось число)")
        return default
    n = to_num(v)
    if n is None:
        ctx.err(p, f"ожидалось число, получено {kind(v)}")
        return default
    if not is_num(v):
        ctx.warn(p, f"число задано строкой ({v!r}) — приведено к {n:g}")
    return n


def integer(ctx, obj, key, path, default=None, *, req=False, lo=None, hi=None):
    v = obj.get(key)
    p = fmt_path(path, key)
    if v is None:
        if req:
            ctx.err(p, "обязательное поле отсутствует (ожидалось целое число)")
        return default
    if isinstance(v, str) and v.strip().lstrip("+-").isdigit():
        ctx.warn(p, f"число задано строкой ({v!r}) — приведено к целому")
        v = int(v.strip())
    if not is_int(v):
        ctx.err(p, f"ожидалось целое число, получено {kind(v)}")
        return default
    if lo is not None and v < lo:
        ctx.err(p, f"значение {v} меньше допустимого минимума {lo}")
        return default
    if hi is not None and v > hi:
        ctx.err(p, f"значение {v} больше допустимого максимума {hi}")
        return default
    return v


def boolean(ctx, obj, key, path, default=None):
    """true/false, а также 'да'/'нет'/'done' и т.п. — с предупреждением."""
    v = obj.get(key)
    p = fmt_path(path, key)
    if v is None:
        return default
    if isinstance(v, bool):
        return v
    if isinstance(v, str):
        t = v.strip().lower()
        if t in ("true", "1", "yes", "y", "да", "истина", "on", "done", "готово", "выполнено", "✓"):
            ctx.warn(p, f"строка {v!r} понята как true")
            return True
        if t in ("false", "0", "no", "n", "нет", "ложь", "off", "", "todo", "не готово"):
            ctx.warn(p, f"строка {v!r} понята как false")
            return False
    if is_num(v):
        ctx.warn(p, f"число {v} понято как {'true' if v else 'false'}")
        return bool(v)
    ctx.err(p, f"ожидалось true/false, получено {kind(v)}")
    return default


def str_list(ctx, value, path, *, maxlen=MAX_TITLE, limit=50):
    """Список строк: плюсы/минусы, критерии, строки матрицы."""
    if value is None:
        return []
    if not isinstance(value, list):
        ctx.err(path, f"ожидался список строк, получено {kind(value)}")
        return []
    out = []
    if len(value) > limit:
        ctx.warn(path, f"элементов {len(value)} — больше {limit}, лишние отброшены")
        value = value[:limit]
    for i, v in enumerate(value):
        p = f"{path}[{i}]"
        if isinstance(v, str):
            t = v.strip()
            if len(t) > maxlen:
                ctx.warn(p, f"слишком длинный текст — обрезан до {maxlen}")
                t = t[:maxlen].rstrip() + "…"
            out.append(t)
        elif is_num(v):
            ctx.warn(p, "число в списке строк приведено к строке")
            out.append(str(v))
        else:
            ctx.err(p, f"ожидалась строка, получено {kind(v)}")
    return out


def check_fields(ctx, b, path, btype):
    """Предупредить о полях, которых рендерер не знает (они отбрасываются)."""
    for k in sorted(set(b) - KNOWN_FIELDS[btype]):
        ctx.warn(fmt_path(path, k), f"поле не используется блоком {btype} и отброшено")
