# -*- coding: utf-8 -*-
"""Проверка блоков отчёта reportkit (внутренний модуль).

Каждый блок проверяется своей функцией _b_<тип>: она возвращает
нормализованный dict (только известные поля) и пишет ошибки/предупреждения в ctx.
"""
from __future__ import annotations

import difflib
import re

try:  # запуск как пакет (import reportkit.validate)
    from ._schema import (MAX_BLOCKS, MAX_GRAPH_EDGES, MAX_GRAPH_NODES, MAX_NEST, MAX_STATS,
                          MAX_TABLE_ROWS, MAX_TEXT, MAX_TITLE, WARN_STATS, ALIASES,
                          BLOCK_TYPES, CHART_KINDS, COL_TYPES, ALIGNS, GRAPH_DIRECTIONS,
                          GRAPH_LAYOUTS, MATRIX_KINDS, ROOT_FIELDS, STATUSES, TONES,
                          TOTAL_KINDS, boolean, check_fields, enum, fmt_path, integer, is_num, kind,
                          lst, num, s, str_list, stub, to_num)
except ImportError:  # запуск как скрипт (python reportkit/validate.py)
    from _schema import (MAX_BLOCKS, MAX_GRAPH_EDGES, MAX_GRAPH_NODES, MAX_NEST, MAX_STATS,
                         MAX_TABLE_ROWS, MAX_TEXT, MAX_TITLE, WARN_STATS, ALIASES,
                         BLOCK_TYPES, CHART_KINDS, COL_TYPES, ALIGNS, GRAPH_DIRECTIONS,
                         GRAPH_LAYOUTS, MATRIX_KINDS, ROOT_FIELDS, STATUSES, TONES,
                         TOTAL_KINDS, boolean, check_fields, enum, fmt_path, integer, is_num, kind,
                         lst, num, s, str_list, stub, to_num)

H = fmt_path  # короткий псевдоним для путей вида blocks[0].x
FIELD_FIX = {"text": ("text", "md"), "callout": ("text", "md"), "code": ("code", "text")}
E, N, I, B, SL = enum, num, integer, boolean, str_list


# --- диспетчер ------------------------------------------------------------
def blocks(ctx, value, path, depth):
    """Проверить список блоков; вернуть нормализованный список."""
    if value is None:
        ctx.err(path, "нет списка blocks — отчёт пуст")
        return []
    if not isinstance(value, list):
        ctx.err(path, f"blocks должен быть списком [...], получено {kind(value)}")
        return []
    return [block(ctx, b, f"{path}[{i}]", depth) for i, b in enumerate(value)]


def block(ctx, b, path, depth):
    """Проверить один блок; при поломке вернуть заглушку-callout."""
    if not isinstance(b, dict):
        ctx.err(path, f"блок должен быть объектом {{'type': ...}}, получено {kind(b)}")
        return stub("Блок повреждён: ожидался объект.")
    raw = b.get("type")
    if not isinstance(raw, str) or not raw.strip():
        ctx.err(H(path, "type"), 'нет типа блока — укажите type, например "text"')
        return stub("Не указан type блока.")
    t = raw.strip().lower()
    if t in ALIASES:
        ctx.warn(H(path, "type"), f"тип {raw!r} — синоним {ALIASES[t]!r}, исправлено")
        t = ALIASES[t]
    if t not in BLOCK_TYPES:
        hint = difflib.get_close_matches(t, list(BLOCK_TYPES) + list(ALIASES), n=1, cutoff=0.45)
        if hint and hint[0] in ALIASES:
            hint = [ALIASES[hint[0]]]
        tip = f" Возможно, вы имели в виду {hint[0]!r}." if hint else ""
        ctx.err(H(path, "type"), f"неизвестный тип блока {raw!r}.{tip} "
                                 f"Допустимые типы: {', '.join(BLOCK_TYPES)}")
        return stub(f"Неизвестный тип блока: {raw}.")

    ctx.stats["blocks"] += 1
    if ctx.stats["blocks"] > MAX_BLOCKS:
        ctx.limit_once("blocks", path, f"блоков больше лимита {MAX_BLOCKS} — отчёт не будет показан")
    if t in ("tabs", "accordion", "columns") and depth > MAX_NEST:
        ctx.err(path, f"слишком глубокая вложенность (уровень {depth}, максимум {MAX_NEST})")

    if t in FIELD_FIX:
        src_key, dst_key = FIELD_FIX[t]
        val = b.get(src_key)
        if dst_key not in b and isinstance(val, str):
            ctx.warn(H(path, dst_key), f"поле {src_key} переименовано в {dst_key}")
            b = {k: v for k, v in b.items() if k != src_key}
            b[dst_key] = val
    check_fields(ctx, b, path, t)
    return HANDLERS[t](ctx, b, path, depth)


# --- блоки ----------------------------------------------------------------
def _b_heading(ctx, b, path, depth):
    return {"type": "heading", "text": s(ctx, b, "text", path, maxlen=MAX_TITLE),
            "level": I(ctx, b, "level", path, 1, lo=1, hi=3)}


def _b_text(ctx, b, path, depth):
    return {"type": "text", "md": s(ctx, b, "md", path, maxlen=MAX_TEXT)}


def _b_callout(ctx, b, path, depth):
    o = {"type": "callout", "tone": E(ctx, b, "tone", path, TONES, "info")}
    title = s(ctx, b, "title", path, req=False, maxlen=MAX_TITLE)
    if title:
        o["title"] = title
    o["md"] = s(ctx, b, "md", path, maxlen=MAX_TEXT)
    return o


def _b_stats(ctx, b, path, depth):
    items = lst(ctx, b, "items", path)
    if not items:
        ctx.err(H(path, "items"), "нужен хотя бы один показатель")
    if len(items) > MAX_STATS:
        ctx.err(H(path, "items"), f"больше {MAX_STATS} показателей не поместится — сократите")
        items = items[:MAX_STATS]
    elif len(items) > WARN_STATS:
        ctx.warn(H(path, "items"), f"{len(items)} показателей — больше {WARN_STATS} смотрится тесно")
    out = []
    for i, it in enumerate(items):
        p = f"{path}.items[{i}]"
        if not isinstance(it, dict):
            ctx.err(p, f"ожидался объект {{label, value}}, получено {kind(it)}")
            continue
        o = {"label": s(ctx, it, "label", p, maxlen=80)}
        v = it.get("value")
        if v is None:
            ctx.err(H(p, "value"), "обязательное поле отсутствует")
            v = "—"
        elif isinstance(v, bool) or not isinstance(v, (str, int, float)):
            ctx.err(H(p, "value"), f"ожидалось число или строка, получено {kind(v)}")
            v = "—"
        elif isinstance(v, str) and len(v) > 60:
            ctx.warn(H(p, "value"), "значение длиннее 60 симв. — обрезано")
            v = v[:60].rstrip() + "…"
        o["value"] = v
        delta = it.get("delta")
        if delta is not None:
            if isinstance(delta, bool) or not isinstance(delta, (str, int, float)):
                ctx.err(H(p, "delta"), f"ожидалась строка или число, получено {kind(delta)}")
            else:
                o["delta"] = delta
        hint = s(ctx, it, "hint", p, req=False, maxlen=120)
        if hint:
            o["hint"] = hint
        o["tone"] = E(ctx, it, "tone", p, TONES, "info")
        out.append(o)
    return {"type": "stats", "items": out}


def _b_table(ctx, b, path, depth):
    o = {"type": "table"}
    title = s(ctx, b, "title", path, req=False)
    if title:
        o["title"] = title
    errors_before = len(ctx.errors)
    cols = lst(ctx, b, "columns", path, req=False)
    if not cols and len(ctx.errors) == errors_before:
        ctx.err(H(path, "columns"), "таблице нужна хотя бы одна колонка (columns: [{key,label}])")
    norm_cols, keys, numeric, coltypes = [], [], set(), {}
    for i, c in enumerate(cols):
        p = f"{path}.columns[{i}]"
        if not isinstance(c, dict):
            ctx.err(p, f"колонка должна быть объектом {{key,label}}, получено {kind(c)}")
            continue
        key = s(ctx, c, "key", p, maxlen=60)
        if not key:
            continue
        if re.search(r"[^\x00-\x7f]", key):
            ctx.warn(H(p, "key"), f"ключ {key!r} не латиница — рендерер и сортировка ждут латинский key")
        if key in keys:
            ctx.err(H(p, "key"), f"колонка с ключом {key!r} уже есть — ключи должны быть уникальны")
        label = s(ctx, c, "label", p, req=False, maxlen=80) or key
        ctype = E(ctx, c, "type", p, COL_TYPES, "text")
        align = E(ctx, c, "align", p, ALIGNS, None)
        if align is None:
            align = "right" if ctype in ("number", "money", "percent") else "left"
        nc = {"key": key, "label": label, "type": ctype, "align": align}
        if ctype == "money":
            # Валюта проекта — реал (Лето, Заозёрье). Явная валюта колонки важнее.
            nc["currency"] = s(ctx, c, "currency", p, req=False, maxlen=8) or "R$"
            nc["decimals"] = I(ctx, c, "decimals", p, 2, lo=0, hi=4)
        elif ctype in ("number", "percent"):
            # percent по умолчанию с одним знаком: 48.8 -> «48,8 %»
            nc["decimals"] = I(ctx, c, "decimals", p, 1 if ctype == "percent" else 0, lo=0, hi=4)
        keys.append(key)
        coltypes[key] = ctype
        if ctype in ("number", "money", "percent"):
            numeric.add(key)
        norm_cols.append(nc)
    o["columns"] = norm_cols

    rows = lst(ctx, b, "rows", path)
    if len(rows) > MAX_TABLE_ROWS:
        ctx.err(H(path, "rows"), f"строк {len(rows)} — больше лимита {MAX_TABLE_ROWS}; разбейте таблицу")
        rows = rows[:MAX_TABLE_ROWS]
    ctx.stats["rows"] += len(rows)
    norm_rows = []
    for i, r in enumerate(rows):
        p = f"{path}.rows[{i}]"
        if not isinstance(r, dict):
            ctx.err(p, f"строка должна быть объектом {{ключ: значение}}, получено {kind(r)}")
            continue
        missing = [k for k in keys if k not in r]
        if missing:
            ctx.warn(p, "нет значений для колонок: " + ", ".join(missing[:5]))
        extra = [k for k in r if k not in keys]
        if extra:
            ctx.warn(p, "лишние ключи (нет такой колонки): " + ", ".join(map(str, extra[:5])))
        cell = {}
        for k, v in r.items():
            if isinstance(v, dict) and coltypes.get(str(k)) == "link":
                # у колонки link значение может быть объектом {title, path|url}
                lc = {}
                for lk in ("title", "label", "path", "url", "href"):
                    lv = v.get(lk)
                    if isinstance(lv, str) and lv.strip():
                        lc[lk] = lv.strip()
                if not (lc.get("path") or lc.get("url") or lc.get("href")):
                    ctx.err(H(p, k), "у ссылки нужен path/url/href")
                else:
                    cell[str(k)] = lc
                continue
            if isinstance(v, (dict, list)):
                ctx.err(H(p, k), f"значение ячейки должно быть скаляром, получено {kind(v)}")
                v = str(v)[:200]
            elif isinstance(v, str) and len(v) > 400:
                ctx.warn(H(p, k), "длинный текст ячейки (400+) — обрезан")
                v = v[:400].rstrip() + "…"
            cell[str(k)] = v
        for k in numeric:
            if k not in cell:
                continue
            v = cell[k]
            if v is None or (isinstance(v, str) and not v.strip()):
                ctx.warn(H(p, k), f"пусто в числовой колонке {k!r} — сортировка и итоги пропустят строку")
                cell[k] = None
                continue
            if not is_num(v):
                n = to_num(v) if isinstance(v, str) else None
                if n is None:
                    ctx.err(H(p, k), f"в колонке {k!r} типа {coltypes[k]} ожидалось число, получено "
                                     f"{kind(v)} — сортировка и итоги сломаются")
                    continue
                ctx.warn(H(p, k), f"число задано строкой ({v!r}) — приведено к {n:g}")
                cell[k] = n
        norm_rows.append(cell)
    o["rows"] = norm_rows
    o["sortable"] = B(ctx, b, "sortable", path, True)
    o["filter"] = B(ctx, b, "filter", path, len(norm_rows) >= 25)

    totals = b.get("totals")
    if totals is not None:
        if not isinstance(totals, dict):
            ctx.err(H(path, "totals"), f"ожидался объект {{колонка: sum|avg|count}}, получено {kind(totals)}")
        else:
            nt = {}
            for k, v in totals.items():
                p = H(path, "totals") + "." + str(k)
                if k not in keys:
                    ctx.err(p, f"колонки {k!r} нет в columns")
                    continue
                if not isinstance(v, str) or v.strip().lower() not in TOTAL_KINDS:
                    ctx.err(p, f"допустимо sum|avg|count, получено {v!r}")
                    continue
                if v.strip().lower() in ("sum", "avg") and k not in numeric:
                    ctx.warn(p, f"итог {v} по нечисловой колонке {k!r} — будет пусто")
                nt[k] = v.strip().lower()
            if nt:
                o["totals"] = nt
    foot = s(ctx, b, "footnote", path, req=False, maxlen=500)
    if foot:
        o["footnote"] = foot
    return o


def _b_chart(ctx, b, path, depth):
    o = {"type": "chart", "kind": E(ctx, b, "kind", path, CHART_KINDS, None, req=True)}
    title = s(ctx, b, "title", path, req=False)
    if title:
        o["title"] = title
    x = None
    if "x" in b:
        xv = b.get("x")
        if isinstance(xv, list):
            x = []
            for i, item in enumerate(xv):
                if isinstance(item, (str, int, float)) and not isinstance(item, bool):
                    x.append(item if isinstance(item, str) else str(item))
                else:
                    ctx.err(f"{path}.x[{i}]", f"метка должна быть строкой или числом, получено {kind(item)}")
        else:
            ctx.err(H(path, "x"), f"ожидался список меток, получено {kind(xv)}")
    if x is not None:
        o["x"] = x

    series = lst(ctx, b, "series", path)
    if not series:
        ctx.err(H(path, "series"), "нужен хотя бы один ряд данных")
    out_series = []
    for i, sr in enumerate(series):
        p = f"{path}.series[{i}]"
        if not isinstance(sr, dict):
            ctx.err(p, f"ряд должен быть объектом {{name, data}}, получено {kind(sr)}")
            continue
        name = s(ctx, sr, "name", p, req=False, maxlen=80) or f"Ряд {i + 1}"
        data = lst(ctx, sr, "data", p)
        nd = []
        for j, v in enumerate(data):
            pj = f"{p}.data[{j}]"
            if o["kind"] == "scatter":
                if not isinstance(v, dict):
                    ctx.err(pj, f"для scatter нужен объект {{x, y}}, получено {kind(v)}")
                    continue
                xv = v.get("x")
                if isinstance(xv, bool) or not isinstance(xv, (str, int, float)):
                    ctx.err(pj + ".x", f"x должен быть числом или строкой, получено {kind(xv)}")
                    continue
                yv = num(ctx, v, "y", pj, req=True)
                if yv is None:
                    continue
                nd.append({"x": xv, "y": yv})
            else:
                n = to_num(v)
                if n is None:
                    ctx.err(pj, f"ожидалось число, получено {kind(v)}")
                    continue
                if not isinstance(v, (int, float)) or isinstance(v, bool):
                    ctx.warn(pj, f"число задано строкой ({v!r}) — приведено к {n:g}")
                nd.append(n)
        if o["kind"] in ("pie", "donut"):
            if len(series) > 1:
                ctx.warn(p, "для pie/donut используется только первый ряд")
        if x is not None and o["kind"] != "scatter" and len(x) != len(nd):
            ctx.warn(p, f"меток в x: {len(x)}, значений в data: {len(nd)} — лишние обрежутся")
        ns = {"name": name, "data": nd}
        color = s(ctx, sr, "color", p, req=False, maxlen=32)
        if color:
            if not (color.startswith("#") or color.startswith("var(")):
                ctx.warn(H(p, "color"), f"цвет {color!r} не похож на #hex — рендерер подставит свой")
            ns["color"] = color
        out_series.append(ns)
    if x is None and o["kind"] in ("pie", "donut", "stacked", "area", "hbar"):
        ctx.warn(H(path, "x"), f"для {o['kind']} нужен x — иначе подписи будут по номерам (#1…#N)")
    o["series"] = out_series
    unit = s(ctx, b, "unit", path, req=False, maxlen=40)
    if unit:
        o["unit"] = unit
    note = s(ctx, b, "note", path, req=False, maxlen=500)
    if note:
        o["note"] = note
    return o


def _b_graph(ctx, b, path, depth):
    o = {"type": "graph",
         "layout": E(ctx, b, "layout", path, GRAPH_LAYOUTS, "layered"),
         "direction": E(ctx, b, "direction", path, GRAPH_DIRECTIONS, "TB", upper=True)}
    title = s(ctx, b, "title", path, req=False)
    if title:
        o["title"] = title
    nodes = lst(ctx, b, "nodes", path)
    if not nodes:
        ctx.err(H(path, "nodes"), "графу нужен хотя бы один узел")
    if len(nodes) > MAX_GRAPH_NODES:
        ctx.err(H(path, "nodes"), f"узлов {len(nodes)} — больше лимита {MAX_GRAPH_NODES}")
        nodes = nodes[:MAX_GRAPH_NODES]
    ctx.stats["nodes"] += len(nodes)
    ids, out_nodes = set(), []
    for i, n in enumerate(nodes):
        p = f"{path}.nodes[{i}]"
        if not isinstance(n, dict):
            ctx.err(p, f"узел должен быть объектом {{id,label}}, получено {kind(n)}")
            continue
        nid = s(ctx, n, "id", p, maxlen=80)
        if not nid:
            continue
        if nid in ids:
            ctx.err(H(p, "id"), f"id {nid!r} уже занят — идентификаторы узлов должны быть уникальны")
        ids.add(nid)
        label = s(ctx, n, "label", p, req=False, maxlen=120)
        if not label:
            ctx.warn(H(p, "label"), f"у узла {nid!r} нет label — подпись будет равна id")
        nn = {"id": nid, "label": label or nid}
        for k in ("group", "note"):
            val = s(ctx, n, k, p, req=False, maxlen=300 if k == "note" else 60)
            if val:
                nn[k] = val
        size = N(ctx, n, "size", p, None)
        if size is not None:
            nn["size"] = size
        out_nodes.append(nn)
    o["nodes"] = out_nodes

    edges = lst(ctx, b, "edges", path, req=False)
    if len(edges) > MAX_GRAPH_EDGES:
        ctx.err(H(path, "edges"), f"связей {len(edges)} — больше лимита {MAX_GRAPH_EDGES}")
        edges = edges[:MAX_GRAPH_EDGES]
    out_edges = []
    for i, e in enumerate(edges):
        p = f"{path}.edges[{i}]"
        if not isinstance(e, dict):
            ctx.err(p, f"связь должна быть объектом {{from,to}}, получено {kind(e)}")
            continue
        fr = s(ctx, e, "from", p, maxlen=80)
        to = s(ctx, e, "to", p, maxlen=80)
        if fr and fr not in ids:
            ctx.err(H(p, "from"), f"узла {fr!r} нет в nodes")
        if to and to not in ids:
            ctx.err(H(p, "to"), f"узла {to!r} нет в nodes")
        ne = {"from": fr, "to": to}
        lab = s(ctx, e, "label", p, req=False, maxlen=80)
        if lab:
            ne["label"] = lab
        ne["kind"] = E(ctx, e, "kind", p, ("solid", "dashed"), "solid")
        w = N(ctx, e, "weight", p, None)
        if w is not None:
            ne["weight"] = w
        out_edges.append(ne)
    o["edges"] = out_edges
    if out_nodes and not out_edges:
        ctx.warn(H(path, "edges"), "узлы без связей — граф будет точками")
    return o


def _b_timeline(ctx, b, path, depth):
    items = lst(ctx, b, "items", path)
    out = []
    for i, it in enumerate(items):
        p = f"{path}.items[{i}]"
        if not isinstance(it, dict):
            ctx.err(p, f"ожидался объект {{date,title}}, получено {kind(it)}")
            continue
        o = {"date": s(ctx, it, "date", p, maxlen=40), "title": s(ctx, it, "title", p, maxlen=MAX_TITLE)}
        for k, mx in (("md", MAX_TEXT), ("owner", 80)):
            val = s(ctx, it, k, p, req=False, maxlen=mx)
            if val:
                o[k] = val
        st = E(ctx, it, "status", p, STATUSES, None)
        if st:
            o["status"] = st
        out.append(o)
    if not out:
        ctx.err(H(path, "items"), "лента пуста")
    return {"type": "timeline", "items": out}


def _b_phases(ctx, b, path, depth):
    items = lst(ctx, b, "items", path)
    out = []
    for i, it in enumerate(items):
        p = f"{path}.items[{i}]"
        if not isinstance(it, dict):
            ctx.err(p, f"ожидался объект {{title,status}}, получено {kind(it)}")
            continue
        o = {"title": s(ctx, it, "title", p, maxlen=MAX_TITLE),
             "status": E(ctx, it, "status", p, STATUSES, "next")}
        note = s(ctx, it, "note", p, req=False, maxlen=300)
        if note:
            o["note"] = note
        out.append(o)
    if not out:
        ctx.err(H(path, "items"), "нет фаз")
    return {"type": "phases", "items": out}


def _b_compare(ctx, b, path, depth):
    o = {"type": "compare"}
    if "criteria" in b:
        o["criteria"] = SL(ctx, b.get("criteria"), H(path, "criteria"), maxlen=120, limit=20)
    opts = lst(ctx, b, "options", path)
    if not opts:
        ctx.err(H(path, "options"), "нужен хотя бы один вариант")
    out = []
    for i, op in enumerate(opts):
        p = f"{path}.options[{i}]"
        if not isinstance(op, dict):
            ctx.err(p, f"вариант должен быть объектом {{name,pros,cons}}, получено {kind(op)}")
            continue
        no = {"name": s(ctx, op, "name", p, maxlen=120),
              "pros": SL(ctx, op.get("pros"), H(p, "pros"), maxlen=200, limit=15),
              "cons": SL(ctx, op.get("cons"), H(p, "cons"), maxlen=200, limit=15)}
        score = N(ctx, op, "score", p, None)
        if score is not None:
            no["score"] = score
        verdict = s(ctx, op, "verdict", p, req=False, maxlen=300)
        if verdict:
            no["verdict"] = verdict
        out.append(no)
    o["options"] = out
    return o


def _b_matrix(ctx, b, path, depth):
    k = E(ctx, b, "kind", path, MATRIX_KINDS, None, req=True)
    o = {"type": "matrix", "kind": k}
    if k == "swot":
        sw = b.get("swot")
        if not isinstance(sw, dict):
            ctx.err(H(path, "swot"), f"для kind='swot' нужен объект swot с четырьмя списками, "
                                     f"получено {kind(sw)}")
            o["swot"] = {x: [] for x in ("strengths", "weaknesses", "opportunities", "threats")}
            return o
        out = {}
        for x in ("strengths", "weaknesses", "opportunities", "threats"):
            if x not in sw:
                ctx.err(H(path, "swot") + "." + x, "обязательный список отсутствует")
            out[x] = SL(ctx, sw.get(x), H(path, "swot") + "." + x, maxlen=200, limit=12)
        o["swot"] = out
        for x in sorted(set(sw) - set(out)):
            ctx.warn(H(path, "swot") + "." + x, "неизвестный ключ SWOT — отброшен")
        return o

    rows = SL(ctx, b.get("rows"), H(path, "rows"), maxlen=60, limit=40)
    cols = SL(ctx, b.get("cols"), H(path, "cols"), maxlen=60, limit=40)
    if not rows or not cols:
        ctx.err(H(path, "rows"), "для heat/grid нужны rows и cols")
    o["rows"], o["cols"] = rows, cols
    cells = lst(ctx, b, "cells", path)
    norm = []
    for i, row in enumerate(cells):
        p = f"{path}.cells[{i}]"
        if not isinstance(row, list):
            ctx.err(p, f"строка матрицы должна быть списком, получено {kind(row)}")
            continue
        if cols and len(row) != len(cols):
            ctx.warn(p, f"в строке {len(row)} ячеек, колонок {len(cols)} — выровнено")
        line = []
        for j, c in enumerate(row):
            if isinstance(c, str):
                line.append(c if len(c) <= 120 else c[:120] + "…")
            elif isinstance(c, bool) or isinstance(c, (int, float)) or c is None:
                line.append(c)
            else:
                ctx.err(f"{p}[{j}]", f"ячейка должна быть числом или строкой, получено {kind(c)}")
                line.append(None)
        norm.append(line)
    if rows and len(norm) != len(rows):
        ctx.warn(H(path, "cells"), f"строк в cells {len(norm)}, в rows {len(rows)}")
    o["cells"] = norm
    return o


def _b_checklist(ctx, b, path, depth):
    items = lst(ctx, b, "items", path)
    out = []
    for i, it in enumerate(items):
        p = f"{path}.items[{i}]"
        if not isinstance(it, dict):
            ctx.err(p, f"ожидался объект {{text,done}}, получено {kind(it)}")
            continue
        o = {"text": s(ctx, it, "text", p, maxlen=400), "done": B(ctx, it, "done", p, False)}
        for k, mx in (("owner", 80), ("due", 40)):
            val = s(ctx, it, k, p, req=False, maxlen=mx)
            if val:
                o[k] = val
        out.append(o)
    if not out:
        ctx.err(H(path, "items"), "чек-лист пуст")
    return {"type": "checklist", "items": out}


def _b_kanban(ctx, b, path, depth):
    lanes = lst(ctx, b, "lanes", path)
    out = []
    for i, ln in enumerate(lanes):
        p = f"{path}.lanes[{i}]"
        if not isinstance(ln, dict):
            ctx.err(p, f"колонка должна быть объектом {{title,cards}}, получено {kind(ln)}")
            continue
        cards = lst(ctx, ln, "cards", p, req=False)
        nc = []
        for j, cd in enumerate(cards):
            pc = f"{p}.cards[{j}]"
            if not isinstance(cd, dict):
                ctx.err(pc, f"карточка должна быть объектом {{title}}, получено {kind(cd)}")
                continue
            o = {"title": s(ctx, cd, "title", pc, maxlen=200)}
            for k, mx in (("note", 300), ("tag", 40)):
                val = s(ctx, cd, k, pc, req=False, maxlen=mx)
                if val:
                    o[k] = val
            nc.append(o)
        out.append({"title": s(ctx, ln, "title", p, maxlen=80), "cards": nc})
    if not out:
        ctx.err(H(path, "lanes"), "доска пуста")
    return {"type": "kanban", "lanes": out}


def _b_gallery(ctx, b, path, depth):
    images = lst(ctx, b, "images", path)
    out = []
    for i, im in enumerate(images):
        p = f"{path}.images[{i}]"
        if not isinstance(im, dict):
            ctx.err(p, f"картинка должна быть объектом {{src}}, получено {kind(im)}")
            continue
        o = {"src": s(ctx, im, "src", p, maxlen=2000)}
        cap = s(ctx, im, "caption", p, req=False, maxlen=300)
        if cap:
            o["caption"] = cap
        out.append(o)
    if not out:
        ctx.err(H(path, "images"), "галерея пуста")
    return {"type": "gallery", "images": out}


def _b_code(ctx, b, path, depth):
    o = {"type": "code", "text": s(ctx, b, "text", path, maxlen=MAX_TEXT)}
    lang = s(ctx, b, "lang", path, req=False, maxlen=24)
    if lang:
        o["lang"] = lang
    return o


def _b_tabs(ctx, b, path, depth):
    tabs = lst(ctx, b, "tabs", path)
    if not tabs:
        ctx.err(H(path, "tabs"), "нужна хотя бы одна вкладка")
    out = []
    for i, tb in enumerate(tabs):
        p = f"{path}.tabs[{i}]"
        if not isinstance(tb, dict):
            ctx.err(p, f"вкладка должна быть объектом {{title,blocks}}, получено {kind(tb)}")
            continue
        out.append({"title": s(ctx, tb, "title", p, maxlen=80),
                    "blocks": blocks(ctx, tb.get("blocks"), H(p, "blocks"), depth + 1)})
    return {"type": "tabs", "tabs": out}


def _b_accordion(ctx, b, path, depth):
    items = lst(ctx, b, "items", path)
    out = []
    for i, it in enumerate(items):
        p = f"{path}.items[{i}]"
        if not isinstance(it, dict):
            ctx.err(p, f"секция должна быть объектом {{title,blocks}}, получено {kind(it)}")
            continue
        o = {"title": s(ctx, it, "title", p, maxlen=MAX_TITLE),
             "blocks": blocks(ctx, it.get("blocks"), H(p, "blocks"), depth + 1)}
        if "open" in it:
            o["open"] = B(ctx, it, "open", p, False)
        out.append(o)
    if not out:
        ctx.err(H(path, "items"), "аккордеон пуст")
    return {"type": "accordion", "items": out}


def _b_columns(ctx, b, path, depth):
    cols = lst(ctx, b, "cols", path)
    if not cols:
        ctx.err(H(path, "cols"), "нужна хотя бы одна колонка")
    out = []
    for i, cl in enumerate(cols):
        p = f"{path}.cols[{i}]"
        if not isinstance(cl, list):
            ctx.err(p, f"колонка должна быть списком блоков, получено {kind(cl)}")
            continue
        out.append(blocks(ctx, cl, p, depth + 1))
    o = {"type": "columns", "cols": out}
    if "widths" in b:
        w = b.get("widths")
        if not isinstance(w, list):
            ctx.err(H(path, "widths"), f"ожидался список чисел, получено {kind(w)}")
        else:
            nw = []
            for i, v in enumerate(w):
                n = to_num(v)
                if n is None or n <= 0:
                    ctx.err(f"{path}.widths[{i}]", f"ожидалось положительное число, получено {v!r}")
                    continue
                nw.append(n)
            if len(nw) != len(out):
                ctx.warn(H(path, "widths"), f"ширин {len(nw)}, колонок {len(out)} — лишние игнорируются")
            if nw:
                o["widths"] = nw
    return o


def _b_links(ctx, b, path, depth):
    items = lst(ctx, b, "items", path)
    out = []
    for i, it in enumerate(items):
        p = f"{path}.items[{i}]"
        if not isinstance(it, dict):
            ctx.err(p, f"ссылка должна быть объектом {{title,path|url}}, получено {kind(it)}")
            continue
        o = {"title": s(ctx, it, "title", p, maxlen=MAX_TITLE)}
        pv = s(ctx, it, "path", p, req=False, maxlen=2000)
        url = s(ctx, it, "url", p, req=False, maxlen=2000)
        if not pv and not url:
            ctx.err(p, "нужен path (файл) или url (адрес)")
        if pv:
            o["path"] = pv
        if url:
            o["url"] = url
        note = s(ctx, it, "note", p, req=False, maxlen=300)
        if note:
            o["note"] = note
        out.append(o)
    if not out:
        ctx.err(H(path, "items"), "нет ссылок")
    return {"type": "links", "items": out}


def _b_sources(ctx, b, path, depth):
    items = lst(ctx, b, "items", path)
    out = []
    for i, it in enumerate(items):
        p = f"{path}.items[{i}]"
        if not isinstance(it, dict):
            ctx.err(p, f"источник должен быть объектом {{title,ref}}, получено {kind(it)}")
            continue
        o = {"title": s(ctx, it, "title", p, maxlen=300), "ref": s(ctx, it, "ref", p, maxlen=2000)}
        note = s(ctx, it, "note", p, req=False, maxlen=300)
        if note:
            o["note"] = note
        out.append(o)
    if not out:
        ctx.err(H(path, "items"), "нет источников")
    return {"type": "sources", "items": out}


HANDLERS = {
    "heading": _b_heading, "text": _b_text, "callout": _b_callout, "stats": _b_stats,
    "table": _b_table, "chart": _b_chart, "graph": _b_graph, "timeline": _b_timeline,
    "phases": _b_phases, "compare": _b_compare, "matrix": _b_matrix,
    "checklist": _b_checklist, "kanban": _b_kanban, "gallery": _b_gallery,
    "code": _b_code, "tabs": _b_tabs, "accordion": _b_accordion,
    "columns": _b_columns, "links": _b_links, "sources": _b_sources,
}
