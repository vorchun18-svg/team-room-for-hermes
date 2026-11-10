"""Экспорт запроса: Markdown для чтения и ОДИН автономный HTML.

Публичный интерфейс:
    export_request_md(detail) -> str
    export_request_html(cfg, detail) -> str

HTML собирается из тех же кирпичей, что reportkit/export_html.py:
CSS web/css/* встраивается в <style>, ES-модули (md.js + рендерер отчёта)
кладутся в ``<script type="text/plain" data-module>`` и грузятся тем же
Blob-загрузчиком (`loader_js`), поэтому в файле нет ни одной внешней ссылки.
Markdown рисует web/js/md.js в браузере; если у запроса есть report spec —
он встраивается и рисуется reportkit-рендерером.
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

try:  # import server.export
    from . import reports as reports_mod
    from .config import Cfg
except ImportError:  # python server/app.py
    import reports as reports_mod
    from config import Cfg

STATUS_RU = {"queued": "в очереди", "working": "в работе", "reviewing": "на проверке",
             "synthesizing": "сводится", "done": "готово", "needs_you": "ждёт тебя",
             "failed": "ошибка"}
PHASE_RU = {"done": "готово", "active": "в работе", "next": "дальше", "blocked": "ждёт"}
PHASE_MARK = {"done": "✓", "active": "▶", "next": "·", "blocked": "⚠"}
ROLE_RU = {"root": "корень", "worker": "исполнитель", "critic": "критик",
           "synthesizer": "синтез", "revision": "доработка"}


# --- помощники ---------------------------------------------------------------
def _s(value, default="") -> str:
    return default if value is None else str(value).strip()


def _list(value) -> list:
    return value if isinstance(value, list) else []


def _dict(value) -> dict:
    return value if isinstance(value, dict) else {}


def _date(ts) -> str:
    try:
        return time.strftime("%d.%m.%Y %H:%M", time.localtime(int(ts)))
    except (TypeError, ValueError, OSError):
        return ""


def _short_date(ts) -> str:
    try:
        return time.strftime("%d.%m.%Y", time.localtime(int(ts)))
    except (TypeError, ValueError, OSError):
        return ""


def _dur(sec) -> str:
    try:
        sec = int(sec)
    except (TypeError, ValueError):
        return ""
    if sec <= 0:
        return ""
    h, m = divmod(sec // 60, 60)
    if h:
        return "%d ч %02d мин" % (h, m)
    return "%d мин" % m if m else "%d с" % sec


def _md_cell(value) -> str:
    """Безопасная ячейка markdown-таблицы."""
    return _s(value).replace("|", "\\|").replace("\n", " ") or "—"


def _title(detail: dict) -> str:
    return _s(detail.get("title")) or _s(detail.get("id")) or "Запрос"


def _status(detail: dict) -> str:
    raw = _s(detail.get("status"))
    return STATUS_RU.get(raw, raw or "—")


def _subtitle(detail: dict) -> str:
    bits = []
    if detail.get("room"):
        bits.append("комната %s" % detail["room"])
    if detail.get("id"):
        bits.append(str(detail["id"]))
    stamp = _date(detail.get("created_at")) or _short_date(time.time())
    if stamp:
        bits.append(stamp)
    return " · ".join(bits)


def _synth(detail: dict) -> dict:
    return _dict(detail.get("synthesis"))


# --- markdown ----------------------------------------------------------------
def export_request_md(detail) -> str:
    """Русский Markdown-экспорт запроса: ответ, шаги, вопросы, решения, дерево."""
    d = _dict(detail)
    syn = _synth(d)
    out = ["# " + _title(d), ""]

    meta = ["**Статус:** " + _status(d)]
    if d.get("room"):
        meta.append("**Комната:** %s" % d["room"])
    if d.get("created_at"):
        meta.append("**Создан:** " + _date(d["created_at"]))
    if d.get("duration_s"):
        meta.append("**Длительность:** " + _dur(d["duration_s"]))
    out.append(" · ".join(meta))
    out.append("")

    progress = _dict(d.get("progress"))
    if progress:
        out.append("**Прогресс:** %s из %s шагов (%s%%)" % (
            progress.get("done", 0), progress.get("total", 0), progress.get("percent", 0)))
        out.append("")

    text = _s(d.get("text"))
    if text:
        out += ["## Запрос", "", text, ""]

    out += ["## Ответ", ""]
    if syn.get("state") == "ready" and _s(syn.get("text_md")):
        out.append(_s(syn.get("text_md")))
        byline = []
        if syn.get("author"):
            byline.append("автор: %s" % syn["author"])
        if syn.get("at"):
            byline.append(_date(syn["at"]))
        if syn.get("version"):
            byline.append("версия %s" % syn["version"])
        if byline:
            out += ["", "_%s_" % " · ".join(byline)]
    elif syn.get("state") == "pending":
        out.append("_Ответ ещё готовится._")
    else:
        out.append("_Ответа пока нет._")
    out.append("")

    steps = _list(d.get("next_steps"))
    if steps:
        out += ["## Дальше", "", "| Шаг | Кто | Срок | Статус |", "|---|---|---|---|"]
        for step in steps:
            if isinstance(step, str):
                step = {"text": step}
            if not isinstance(step, dict):
                continue
            out.append("| %s | %s | %s | %s |" % (
                _md_cell(step.get("text")), _md_cell(step.get("owner")),
                _md_cell(step.get("due")), _md_cell(step.get("state") or "open")))
        out.append("")

    questions = _list(d.get("open_questions"))
    if questions:
        out += ["## Вопросы к тебе", ""]
        for q in questions:
            if isinstance(q, str):
                q = {"text": q, "for": "mikhail"}
            if not isinstance(q, dict):
                continue
            who = "Михаил" if _s(q.get("for")) in ("mikhail", "") else q["for"]
            mark = "✓ " if q.get("answered") else ""
            out.append("- %s**%s** — %s" % (mark, who, _s(q.get("text"))))
            if q.get("answered") and _s(q.get("answer")):
                out.append("  - ответ: %s" % _s(q["answer"]))
        out.append("")

    decisions = _list(d.get("decisions"))
    if decisions:
        out += ["## Решения", ""]
        for dec in decisions:
            if isinstance(dec, str):
                dec = {"text": dec}
            if not isinstance(dec, dict):
                continue
            state = {"accepted": "принято", "proposed": "предложено",
                     "rejected": "отклонено"}.get(_s(dec.get("status")), _s(dec.get("status")))
            out.append("- %s _(%s)_" % (_s(dec.get("text")), state or "—"))
            if _s(dec.get("why")):
                out.append("  - почему: %s" % _s(dec["why"]))
        out.append("")

    stage = _dict(d.get("stage"))
    phases = _list(stage.get("phases"))
    if phases:
        out += ["## Этапы проекта", ""]
        for ph in phases:
            if not isinstance(ph, dict):
                continue
            st = _s(ph.get("status"))
            line = "- %s **%s** — %s" % (PHASE_MARK.get(st, "·"), _s(ph.get("title")),
                                         PHASE_RU.get(st, st or "—"))
            if _s(ph.get("note")):
                line += " (%s)" % _s(ph["note"])
            out.append(line)
        if stage.get("current"):
            out += ["", "_Сейчас: %s_" % stage["current"]]
        out.append("")

    artifacts = _list(d.get("artifacts"))
    if artifacts:
        out += ["## Артефакты", ""]
        for art in artifacts:
            if isinstance(art, str):
                art = {"path": art}
            if not isinstance(art, dict):
                continue
            path = _s(art.get("path"))
            note = " — %s" % _s(art.get("title")) if _s(art.get("title")) else ""
            flag = "" if art.get("exists", True) else " (не найден)"
            out.append("- `%s`%s%s" % (path, note, flag))
        out.append("")

    tree = [n for n in _list(d.get("tree")) if isinstance(n, dict)]
    if tree:
        out += ["## Дерево работ", ""]
        ids = {n.get("id") for n in tree}
        for node in tree:
            parents = [p for p in _list(node.get("parent_ids")) if p in ids]
            depth = 0
            cursor = node
            guard = 0
            while [p for p in _list(cursor.get("parent_ids")) if p in ids] and guard < 12:
                parent_id = [p for p in _list(cursor.get("parent_ids")) if p in ids][0]
                cursor = next((n for n in tree if n.get("id") == parent_id), {})
                depth += 1
                guard += 1
            bits = [_s(node.get("assignee")) or "без исполнителя",
                    ROLE_RU.get(_s(node.get("role")), _s(node.get("role"))),
                    _s(node.get("status"))]
            if node.get("duration_s"):
                bits.append(_dur(node.get("duration_s")))
            out.append("%s- **%s** (%s)" % ("  " * depth, _s(node.get("title")), ", ".join(
                b for b in bits if b)))
            summary = _s(node.get("summary_md"))
            if summary:
                first = summary.split("\n")[0][:300]
                out.append("%s  %s" % ("  " * depth, first))
        out.append("")

    report = _dict(d.get("report"))
    if report.get("available") and report.get("path"):
        out += ["## Отчёт", "", "- `%s`" % _s(report.get("path")), ""]

    warnings = _list(d.get("warnings"))
    if warnings:
        out += ["## Предупреждения", ""]
        for w in warnings:
            if isinstance(w, dict):
                out.append("- %s" % _s(w.get("message") or w.get("code")))
        out.append("")

    out += ["---", "_Экспорт «Комнаты команды» · %s_" % _date(time.time()), ""]
    return "\n".join(out)


# --- HTML --------------------------------------------------------------------
def _rk_export():
    """Модуль reportkit.export_html (переиспользуем его сборку и загрузчик)."""
    root = Path(__file__).resolve().parent.parent
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    from reportkit import export_html as mod
    return mod


# Исполняется в браузере после загрузки модулей: рисует шапку, Markdown и отчёт.
PAGE_MODULE = r"""
import { renderMarkdown } from './md.js';

function node(tag, cls, text) {
  const el = document.createElement(tag);
  if (cls) el.className = cls;
  if (text !== undefined && text !== null) el.textContent = String(text);
  return el;
}

export async function renderReport(app, payload, opts) {
  const data = payload && typeof payload === 'object' ? payload : {};
  app.textContent = '';
  const wrap = node('article', 'tr-doc');
  const head = node('header', 'tr-head');
  head.appendChild(node('h1', 'tr-title', data.title || 'Запрос'));
  if (data.subtitle) head.appendChild(node('p', 'tr-sub', data.subtitle));
  wrap.appendChild(head);
  const md = node('div', 'tr-md');
  // безопасно: renderMarkdown экранирует весь ввод и отдаёт только свои теги
  md.innerHTML = renderMarkdown(data.md || '', opts || {});
  wrap.appendChild(md);
  if (data.note) wrap.appendChild(node('p', 'tr-note', data.note));
  app.appendChild(wrap);
  if (data.report && data.report.spec) {
    const sec = node('section', 'tr-report-sec');
    sec.appendChild(node('h2', 'tr-report-title', 'Отчёт'));
    const box = node('div', 'tr-report report-root');
    sec.appendChild(box);
    app.appendChild(sec);
    try {
      const mod = await import('./report/index.js');
      const fn = mod && (mod.renderReport || mod.render);
      if (typeof fn === 'function') fn(box, data.report.spec, opts || {});
      else box.textContent = 'Рендерер отчёта не найден.';
    } catch (e) {
      box.textContent = 'Отчёт не отрисован: ' + (e && e.message ? e.message : e);
    }
  }
}
"""

PAGE_CSS = """
/* страница экспорта: только раскладка, вид задают tokens/base/components */
html,body{margin:0;padding:0;background:var(--background);color:var(--foreground)}
body{font-family:var(--font-sans);line-height:1.55}
.tr-export{max-width:920px;margin:0 auto;padding:32px 22px 72px}
.tr-head{border-bottom:1px solid var(--border);padding-bottom:14px;margin-bottom:22px}
.tr-title{font-size:1.7rem;line-height:1.22;margin:0 0 6px}
.tr-sub{margin:0;color:var(--muted-foreground);font-size:.9rem}
.tr-md h2{font-size:1.2rem;margin:1.6em 0 .4em;padding-bottom:.2em;border-bottom:1px solid var(--border)}
.tr-md h1{display:none}
.tr-md table{border-collapse:collapse;width:100%;margin:.8em 0;font-size:.93rem}
.tr-md th,.tr-md td{border-bottom:1px solid var(--border);padding:6px 9px;text-align:left;vertical-align:top}
.tr-md th{color:var(--muted-foreground);font-weight:600}
.tr-md code{font-family:var(--font-mono);font-size:.9em;background:rgba(127,127,127,.12);padding:.1em .35em;border-radius:4px}
.tr-md blockquote{border-left:3px solid var(--border);margin:.8em 0;padding:.1em 0 .1em 12px;color:var(--muted-foreground)}
.tr-note{color:var(--muted-foreground);font-size:.85rem}
.tr-report-sec{margin-top:34px;border-top:1px solid var(--border);padding-top:18px}
.tr-report-title{font-size:1.15rem;margin:0 0 12px}
@media print{body{background:#fff}.tr-export{max-width:none;padding:0}}
"""


def _css(cfg: Cfg) -> str:
    """Инлайн CSS: reportkit-база + web/css/{tokens,base,components,report,report-charts}."""
    E = _rk_export()
    parts = [E.FALLBACK_CSS]
    for rel in E.CSS_ORDER:
        f = Path(cfg.web_dir) / rel
        if f.is_file():
            try:
                parts.append("/* %s */\n%s" % (rel, f.read_text(encoding="utf-8-sig")))
            except OSError:
                continue
    parts.append(PAGE_CSS)
    return "\n".join(parts)


def _json(obj) -> str:
    """JSON для <script>: без </script и без сырых < > (XSS-безопасно)."""
    E = _rk_export()
    raw = E.json_for_html(obj)
    return raw.replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")


def _report_spec(cfg: Cfg, detail: dict):
    """(spec, note) — встроенный отчёт запроса, если он есть и валиден."""
    ref = _dict(detail.get("report"))
    if not ref.get("available") or not ref.get("path"):
        return None, ""
    try:
        loaded = reports_mod.load_report(cfg, ref["path"])
    except Exception as e:  # отчёт не должен ронять экспорт
        return None, "Отчёт не встроен: %s" % e
    if loaded.get("kind") != "spec":
        return None, "Отчёт в формате HTML не встраивается в экспорт — откройте его в приложении."
    if loaded.get("errors"):
        return None, "Отчёт не встроен: ошибки валидации (%d)." % len(loaded["errors"])
    spec = loaded.get("spec") or {}
    if not _list(spec.get("blocks")):
        return None, ""
    return spec, ""


def _shell(cfg: Cfg, payload: dict, css: str, mods: dict, order: list, files: dict, has_entry: bool) -> str:
    E = _rk_export()
    title = payload.get("title") or "Запрос"
    p = ['<!doctype html>\n<html lang="ru" data-theme="auto">\n<head>\n<meta charset="utf-8">\n',
         '<meta name="viewport" content="width=device-width, initial-scale=1">\n',
         '<meta name="color-scheme" content="light dark">\n',
         '<meta name="generator" content="team-room/export.py">\n',
         "<title>%s</title>\n" % E.esc_html(title),
         "<script>try{document.documentElement.dataset.theme="
         "matchMedia('(prefers-color-scheme: dark)').matches?'dark':'light'}catch(e){}</script>\n",
         "<style>\n" + css.replace("</style", "<\\/style") + "\n</style>\n</head>\n<body>\n",
         '<main id="app" class="tr-export"><p class="tr-note">Загрузка…</p></main>\n',
         "<noscript><p>Для показа нужен JavaScript.</p></noscript>\n",
         '<script type="application/json" id="rk-spec">' + _json(payload) + "</script>\n",
         '<script type="application/json" id="rk-order">' + _json(order) + "</script>\n",
         "<script>window.__RK_FILES = " + _json(files) + ";</script>\n",
         E.FILEURL_JS + "\n"]
    for path in order:
        code = mods.get(path, "")
        if "</script" in code:
            code = code.replace("</script", "<\\/script")
        p.append('<script type="text/plain" data-module="%s">%s</script>\n' % (E.esc_html(path), code))
    p.append(E.FALLBACK_JS + "\n")
    p.append(E.loader_js("/js/tr-export.js" if has_entry else "") + "\n")
    p.append("</body>\n</html>\n")
    return "".join(p)


def export_request_html(cfg: Cfg, detail) -> str:
    """Один автономный HTML-файл: ответ, шаги, дерево, встроенный отчёт."""
    d = _dict(detail)
    E = _rk_export()
    warns = []
    md = export_request_md(d)
    spec, note = _report_spec(cfg, d)

    files = {}
    if spec is not None:
        report_path = Path(_s(_dict(d.get("report")).get("path")))
        spec, files, _n, _b = E.embed_images(spec, report_path.parent.resolve()
                                             if report_path.name else Path(cfg.data_dir), warns.append)

    payload = {
        "version": 1,
        "title": _title(d),
        "subtitle": _subtitle(d),
        "md": md,
        "note": note,
        "report": {"spec": spec} if spec else None,
    }

    css = _css(cfg)
    mods, order = E.collect_modules(Path(cfg.web_dir), "/js/report/index.js", warns.append)
    has_entry = bool(mods.get("/js/md.js"))
    if has_entry:
        mods["/js/tr-export.js"] = PAGE_MODULE
        order.append("/js/tr-export.js")
    return _shell(cfg, payload, css, mods, order, files, has_entry)


__all__ = ["export_request_md", "export_request_html"]
