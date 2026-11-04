#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Экспорт отчёта reportkit в ОДИН автономный HTML-файл (без сети, без сборщика).

    python export_html.py report.json out.html [--theme auto|light|dark] [--web DIR]

Как это работает:
  1. JSON проверяется тем же validate.py (при ошибках экспорт не делается);
  2. CSS из web/css/ встраивается в <style>;
  3. ES-модули рендерера (web/js/report/index.js и всё, что он импортирует)
     кладутся в <script type="text/plain" data-module="/js/...">; загрузчик
     в браузере создаёт Blob-URL в топологическом порядке, подменяя
     относительные import-спецификаторы, и импортирует точку входа;
  4. локальные картинки <= 2 МБ из разрешённых корней -> data: URI;
  5. вызывается renderReport(app, spec, {fileUrl}).

Если web/ ещё не написан (пишут параллельно) — берём что есть, предупреждаем
и показываем упрощённый встроенный просмотрщик.
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import re
import sys
from pathlib import Path

try:
    from .validate import load_file, format_report
except ImportError:
    from validate import load_file, format_report

HERE = Path(__file__).resolve().parent
PROJECT = HERE.parent
DEFAULT_WEB = PROJECT / "web"

CSS_ORDER = ("css/tokens.css", "css/base.css", "css/components.css",
             "css/report.css", "css/report-charts.css", "css/report-graph.css")
ENTRY = "/js/report/index.js"
MAX_IMAGE = 2 * 1024 * 1024
IMG_EXT = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".avif"}
SPEC_RE = re.compile(r"""(?:\bfrom\s*|\bimport\s*\(?\s*)(['"])([^'"\n]+)\1""")
EXTERNAL_REF_RE = re.compile(r"""(?:src|href)\s*=\s*["']https?://""", re.I)

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass


# --- модули ---------------------------------------------------------------
def resolve_spec(base_dir, spec):
    """'./x.js' из '/js/report' -> '/js/report/x.js'. Внешние ('react') -> None."""
    if not spec.startswith("."):
        return None
    out = []
    for p in (base_dir + "/" + spec).split("/"):
        if p in ("", "."):
            continue
        if p == "..":
            if out:
                out.pop()
            continue
        out.append(p)
    return "/" + "/".join(out)


def seed_modules(web: Path, entry: str):
    """Точка входа + все модули каталога рендерера + md/util.

    Каталог берём целиком: рендерер грузит часть модулей лениво
    (import(переменная)), статический анализ такие связи не видит.
    """
    seeds = [entry]
    report_dir = web / entry.lstrip("/").rsplit("/", 1)[0]
    if report_dir.is_dir():
        for f in sorted(report_dir.glob("*.js")):
            seeds.append("/" + f.relative_to(web).as_posix())
    for rel in ("js/md.js", "js/util.js"):
        if (web / rel).is_file():
            seeds.append("/" + rel)
    out = []
    for x in seeds:
        if x not in out:
            out.append(x)
    return out


def collect_modules(web: Path, entry: str, warn):
    """Собрать точку входа и транзитивные импорты. Возвращает (mods, order)."""
    mods, order, state = {}, [], {}

    def visit(spec_path, parent=None):
        if state.get(spec_path) == 2:
            return
        if state.get(spec_path) == 1:
            warn(f"циклический импорт {spec_path}" + (f" (из {parent})" if parent else ""))
            return
        f = web / spec_path.lstrip("/")
        if not f.is_file():
            if parent:
                warn(f"модуль {spec_path} не найден (импортируется из {parent}) — подключён пустой заглушкой")
                mods.setdefault(spec_path, "export {};\n")
                order.append(spec_path)
                state[spec_path] = 2
            return
        try:
            code = f.read_text(encoding="utf-8-sig")
        except OSError as e:
            warn(f"не удалось прочитать {f}: {e}")
            return
        state[spec_path] = 1
        base_dir = spec_path.rsplit("/", 1)[0]
        for _q, spec in SPEC_RE.findall(code):
            dep = resolve_spec(base_dir, spec)
            if dep is None:
                if not spec.startswith(("/", "data:", "node:")):
                    warn(f"внешний import {spec!r} в {spec_path} — офлайн не разрешается")
                continue
            visit(dep, spec_path)
        state[spec_path] = 2
        mods[spec_path] = code
        order.append(spec_path)

    for seed in seed_modules(web, entry):
        visit(seed)
    return mods, order


# --- картинки -------------------------------------------------------------
def allowed_roots(report_dir: Path):
    """Откуда разрешено брать картинки в отчёт: каталог отчёта, приложение,
    хранилище Obsidian и доски Hermes — всё определяется, никаких путей автора."""
    roots = [report_dir, PROJECT]
    for name in ("TEAMROOM_VAULT_DIR", "TEAMROOM_HOME"):
        value = os.environ.get(name)
        if value and Path(value).expanduser().is_dir():
            roots.append(Path(value).expanduser())
    la = os.environ.get("LOCALAPPDATA")
    if la:
        roots.append(Path(la) / "hermes" / "kanban" / "boards")
    extra = os.environ.get("TEAMROOM_ALLOWED_ROOTS")
    if extra:
        roots.extend(Path(x) for x in extra.split(os.pathsep) if x.strip())
    out = []
    for r in roots:
        try:
            out.append(os.path.normcase(str(r.resolve())))
        except OSError:
            continue
    return out


def _inside(path: Path, roots) -> bool:
    p = os.path.normcase(str(path))
    return any(p == r or p.startswith(r + os.sep) for r in roots)


def _mime(suffix: str) -> str:
    return {"svg": "image/svg+xml", "png": "image/png", "jpg": "image/jpeg",
            "jpeg": "image/jpeg", "gif": "image/gif", "webp": "image/webp",
            "avif": "image/avif"}.get(suffix.lower().lstrip("."), "application/octet-stream")


def embed_images(spec, report_dir: Path, warn):
    """Заменить src локальных картинок на data: URI -> (spec, file_map, n, bytes)."""
    roots = allowed_roots(report_dir)
    embedded, count, total = {}, 0, 0

    def handle(node, key):
        nonlocal count, total
        v = node[key]
        if not isinstance(v, str) or v.startswith(("data:", "http://", "https://")):
            return
        p = Path(v)
        if not p.is_absolute():
            p = report_dir / p
        try:
            rp = p.resolve()
        except OSError:
            return
        if rp.suffix.lower() not in IMG_EXT:
            return
        if not rp.is_file():
            warn(f"картинка не найдена: {v} — ссылка оставлена как есть")
            return
        if not _inside(rp, roots):
            warn(f"картинка вне разрешённых корней: {v} — не встроена")
            return
        size = rp.stat().st_size
        if size > MAX_IMAGE:
            warn(f"картинка больше 2 МБ ({size / 1048576:.1f} МБ): {v} — не встроена")
            return
        try:
            data = base64.b64encode(rp.read_bytes()).decode("ascii")
        except OSError as e:
            warn(f"не удалось прочитать картинку {v}: {e}")
            return
        uri = f"data:{_mime(rp.suffix)};base64,{data}"
        node[key] = uri
        embedded[str(rp).replace("\\", "/")] = uri
        embedded[os.path.normcase(str(rp)).replace("\\", "/")] = uri
        count += 1
        total += size

    def walk(node):
        if isinstance(node, list):
            for v in node:
                walk(v)
        elif isinstance(node, dict):
            for k, v in list(node.items()):
                if k == "src" and isinstance(v, str):
                    handle(node, k)
                elif isinstance(v, (dict, list)):
                    walk(v)

    walk(spec)
    return spec, embedded, count, total


# --- HTML -----------------------------------------------------------------
def esc_html(s):
    return (str(s).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


def json_for_html(obj):
    return json.dumps(obj, ensure_ascii=False).replace("</", "<\\/")


def loader_js(entry):
    return LOADER.replace("__ENTRY__", json.dumps(entry))


def build_html(spec, css, mods, order, files, theme, has_entry):
    title = spec.get("title") or "Отчёт"
    p = []
    p.append('<!doctype html>\n<html lang="ru" data-theme="%s">\n<head>\n<meta charset="utf-8">\n'
             % ("auto" if theme == "auto" else theme))
    p.append('<meta name="viewport" content="width=device-width, initial-scale=1">\n')
    p.append('<meta name="color-scheme" content="light dark">\n')
    p.append('<meta name="generator" content="reportkit/export_html.py">\n')
    p.append(f"<title>{esc_html(title)}</title>\n")
    if theme == "auto":
        p.append("<script>try{document.documentElement.dataset.theme="
                 "matchMedia('(prefers-color-scheme: dark)').matches?'dark':'light'}catch(e){}</script>\n")
    p.append("<style>\n" + css.replace("</style", "<\\/style") + "\n</style>\n</head>\n<body>\n")
    p.append('<main id="app" class="report-root"><p class="rk-note">Загрузка отчёта…</p></main>\n')
    p.append("<noscript><p>Для показа отчёта нужен JavaScript.</p></noscript>\n")
    p.append('<script type="application/json" id="rk-spec">' + json_for_html(spec) + "</script>\n")
    p.append('<script type="application/json" id="rk-order">' + json_for_html(order) + "</script>\n")
    p.append("<script>window.__RK_FILES = " + json_for_html(files) + ";</script>\n")
    p.append(FILEURL_JS + "\n")
    for path in order:
        code = mods.get(path, "")
        if "</script" in code:
            code = code.replace("</script", "<\\/script")
        p.append(f'<script type="text/plain" data-module="{esc_html(path)}">' + code + "</script>\n")
    p.append(FALLBACK_JS + "\n")
    p.append(loader_js(ENTRY if has_entry else "") + "\n")
    p.append("</body>\n</html>\n")
    return "".join(p)


try:  # запуск как пакет
    from ._html_assets import FALLBACK_CSS, FALLBACK_JS, FILEURL_JS, LOADER
except ImportError:  # запуск как скрипт
    from _html_assets import FALLBACK_CSS, FALLBACK_JS, FILEURL_JS, LOADER


def export(report_path, out_path, theme="auto", web_dir=None, quiet=False):
    """Собрать автономный HTML. Возвращает (ok, errors, warnings, info)."""
    report_path = Path(report_path)
    out_path = Path(out_path)
    web = Path(web_dir) if web_dir else DEFAULT_WEB
    warns = []

    spec, errors, warnings = load_file(report_path)
    warns.extend(warnings)
    if errors:
        return False, errors, warns, {"spec": spec}

    css_parts, css_have, css_missing = [], [], []
    for rel in CSS_ORDER:
        f = web / rel
        if f.is_file():
            try:
                css_parts.append(f"/* {rel} */\n" + f.read_text(encoding="utf-8-sig"))
                css_have.append(rel)
            except OSError as e:
                warns.append(f"не удалось прочитать {f}: {e}")
                css_missing.append(rel)
        else:
            css_missing.append(rel)
    if css_missing:
        warns.append("нет файлов стилей: " + ", ".join(css_missing) + " — взят базовый слой reportkit")
    css = FALLBACK_CSS + ("\n" + "\n".join(css_parts) if css_parts else "")

    mods, order = collect_modules(web, ENTRY, warns.append)
    has_entry = ENTRY in mods and bool(mods.get(ENTRY))
    if not has_entry:
        warns.append("нет рендерера web/js/report/index.js — в файл встроен упрощённый просмотрщик")

    spec, files, n_img, img_bytes = embed_images(spec, report_path.parent.resolve(), warns.append)
    html = build_html(spec, css, mods, order, files, theme, has_entry)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(html, encoding="utf-8")
    ext = len(EXTERNAL_REF_RE.findall(html))
    if ext:
        warns.append(f"в HTML остались {ext} внешних ссылок (src/href http…) — офлайн они не откроются")
    info = {"title": spec.get("title") or "", "css_have": css_have, "css_missing": css_missing, "modules": order,
            "has_entry": has_entry, "images": n_img, "image_bytes": img_bytes,
            "size": len(html.encode("utf-8")), "external": ext, "out": str(out_path)}
    return True, [], warns, info


def main(argv=None):
    ap = argparse.ArgumentParser(description="Экспорт отчёта reportkit в автономный HTML.")
    ap.add_argument("report", help="путь к report.json")
    ap.add_argument("out", help="куда записать HTML")
    ap.add_argument("--theme", choices=("auto", "light", "dark"), default="auto",
                    help="тема оформления (по умолчанию auto — как в системе)")
    ap.add_argument("--web", metavar="DIR", help="каталог web/ проекта (по умолчанию ../web)")
    ap.add_argument("--quiet", action="store_true", help="не печатать предупреждения")
    args = ap.parse_args(argv)

    ok, errors, warns, info = export(args.report, args.out, args.theme, args.web, args.quiet)
    if not ok:
        print(format_report(info.get("spec") or {}, errors, warns, source=Path(args.report).name))
        print("\nЭкспорт отменён: сначала исправьте ошибки (см. reportkit/SPEC.md).")
        return 1

    if not args.quiet:
        print(f"Отчёт: «{info.get('title') or Path(args.report).name}»")
        if warns:
            print(f"Предупреждения ({len(warns)}):")
            for w in warns:
                print(f"  - {w}")
        print(f"CSS: встроено {len(info['css_have'])}"
              + (f", нет: {', '.join(info['css_missing'])}" if info["css_missing"] else ""))
        print(f"Модули: {len(info['modules'])}" + ("" if info["has_entry"] else " (рендерер отсутствует)"))
        if info["images"]:
            print(f"Картинок встроено: {info['images']} ({info['image_bytes'] / 1048576:.1f} МБ)")
    kb = info["size"] / 1024
    print(f"Файл: {info['out']} — {kb:.1f} КБ ({info['size']} байт)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
