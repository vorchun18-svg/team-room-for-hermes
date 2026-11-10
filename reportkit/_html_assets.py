# -*- coding: utf-8 -*-
"""Встроенные ассеты автономного HTML-отчёта (вынесены из export_html.py, Q1).

Здесь только строковые константы: загрузчик ES-модулей через Blob-URL,
CSS/JS упрощённого просмотрщика и хелпер fileUrl. Логика — в export_html.py.
"""
from __future__ import annotations

FILEURL_JS = r"""<script>
window.__rkFileUrl = function (p) {
  if (p === null || p === undefined) return '#';
  var s = String(p).replace(/\\/g, '/');
  var m = window.__RK_FILES || {};
  if (m[s]) return m[s];
  if (m[s.replace(/^\.\//, '')]) return m[s.replace(/^\.\//, '')];
  if (/^https?:/i.test(s)) return s;
  if (/^[A-Za-z]:\//.test(s)) return 'file:///' + s;
  return s;
};
</script>"""

LOADER = r"""<script>
(function () {
  var src = {}, nodes = document.querySelectorAll('script[data-module]');
  for (var i = 0; i < nodes.length; i++) src[nodes[i].getAttribute('data-module')] = nodes[i].textContent;
  var order = JSON.parse(document.getElementById('rk-order').textContent);
  var urls = {};
  function norm(dir, spec) {
    var parts = (dir + '/' + spec).split('/'), out = [];
    for (var i = 0; i < parts.length; i++) {
      var p = parts[i];
      if (!p || p === '.') continue;
      if (p === '..') { out.pop(); continue; }
      out.push(p);
    }
    return '/' + out.join('/');
  }
  function rewrite(code, path) {
    var dir = path.slice(0, path.lastIndexOf('/'));
    var out = code.replace(/(\bfrom\s*|\bimport\s*\(?\s*)(['"])([^'"\n]+)\2/g, function (m, pre, q, spec) {
      if (spec.charAt(0) === '.') {
        var key = norm(dir, spec);
        if (urls[key]) return pre + JSON.stringify(urls[key]);
      }
      return m;
    });
    /* import(выражение) — так рендерер лениво грузит charts.js/graph.js:
       подставляем путь текущего модуля, чтобы резолв шёл от него, а не от blob:. */
    return out.replace(/\bimport\s*\(\s*(?!['"])/g, 'window.__rkDyn(' + JSON.stringify(path) + ', ');
  }
  for (var j = 0; j < order.length; j++) {
    var path = order[j];
    try {
      urls[path] = URL.createObjectURL(new Blob([rewrite(src[path] || '', path)], { type: 'text/javascript' }));
    } catch (e) { console.error('reportkit: модуль ' + path, e); }
  }
  window.__rkDyn = function (base, target) {
    var key = String(target === null || target === undefined ? '' : target);
    if (key.charAt(0) === '.') key = norm(base.slice(0, base.lastIndexOf('/')), key);
    if (!urls[key]) return Promise.reject(new Error('reportkit: модуль не собран: ' + target));
    return import(urls[key]);
  };
  var spec = JSON.parse(document.getElementById('rk-spec').textContent);
  var app = document.getElementById('app');
  /* fileUrl — функция (так её вызывает md.js) и одновременно строка-база
     (так её использует new URL(s, fileUrl) в index.js). */
  var baseUrl = document.baseURI || location.href;
  function fileUrl(p) { try { return window.__rkFileUrl(p); } catch (e) { return p; } }
  fileUrl.toString = function () { return baseUrl; };
  var opts = { fileUrl: fileUrl, basePath: baseUrl };
  var entry = __ENTRY__;
  function fallback(note) {
    if (typeof window.__reportkitFallback === 'function') window.__reportkitFallback(app, spec, note);
  }
  (async function () {
    if (!entry || !urls[entry]) { fallback('Рендерер не найден: web/js/report/index.js отсутствует.'); return; }
    var mod;
    try { mod = await import(urls[entry]); }
    catch (e) { console.error('reportkit: точка входа упала', e); fallback('Рендерер не загрузился: ' + e.message); return; }
    var fn = mod && (mod.renderReport || mod.render);
    if (typeof fn !== 'function') { fallback('В рендерере нет функции renderReport.'); return; }
    try { await fn(app, spec, opts); }
    catch (e) { console.error('reportkit: renderReport упал', e); fallback('Ошибка отрисовки: ' + e.message); }
  })();
})();
</script>"""

FALLBACK_CSS = r"""
/* Базовый слой reportkit: работает и без web/css (тогда правит вид fallback-просмотр). */
:root{
  --background:#fcfcfb; --foreground:#1b1b1a; --card:#ffffff; --border:#e4e4e0;
  --muted-foreground:#6b6b66; --accent:#3f6cf6; --ring:#3f6cf6;
  --radius:10px; --font-sans:"Segoe UI",system-ui,-apple-system,Arial,sans-serif;
  --font-mono:"Cascadia Mono",Consolas,"Courier New",monospace;
}
[data-theme="dark"]{
  --background:#141413; --foreground:#e9e9e4; --card:#1c1c1b; --border:#2d2d2a;
  --muted-foreground:#9a9a93; --accent:#7aa2ff; --ring:#7aa2ff;
}
html,body{margin:0;padding:0;background:var(--background);color:var(--foreground)}
body{font-family:var(--font-sans);line-height:1.55;padding:28px 20px 60px}
#app{max-width:1080px;margin:0 auto}
h1{font-size:1.6rem;line-height:1.25;margin:.2em 0 .3em}
h2{font-size:1.22rem;margin:1.6em 0 .4em}
h3{font-size:1.05rem;margin:1.2em 0 .3em}
p{margin:.55em 0}
a{color:var(--accent)}
code{font-family:var(--font-mono);font-size:.9em;background:rgba(127,127,127,.12);padding:.1em .35em;border-radius:4px}
pre{font-family:var(--font-mono);font-size:.85rem;background:var(--card);border:1px solid var(--border);border-radius:var(--radius);padding:14px;overflow:auto}
table{border-collapse:collapse;width:100%;margin:.8em 0;font-size:.92rem}
th,td{border-bottom:1px solid var(--border);padding:7px 10px;text-align:left;vertical-align:top}
th{color:var(--muted-foreground);font-weight:600;font-size:.82rem;text-transform:none}
img{max-width:100%;height:auto;border-radius:var(--radius)}
ul,ol{margin:.5em 0;padding-left:1.3em}
.rk-note{color:var(--muted-foreground);font-size:.85rem;border-left:2px solid var(--border);padding-left:10px}
.rk-sub{color:var(--muted-foreground);margin-top:-.2em}
.rk-summary{font-size:1.02rem;border-left:3px solid var(--accent);padding:2px 0 2px 14px;margin:1em 0}
.rk-callout{border:1px solid var(--border);border-left:3px solid var(--muted-foreground);border-radius:var(--radius);padding:10px 14px;margin:1em 0;background:var(--card)}
.rk-callout.success{border-left-color:#2f9e63}.rk-callout.warn{border-left-color:#d09a1f}
.rk-callout.danger{border-left-color:#d0483f}
.rk-callout .rk-ct{font-weight:600;margin-bottom:.2em}
.rk-stats{display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:10px;margin:1em 0}
.rk-stat{border:1px solid var(--border);border-radius:var(--radius);padding:10px 12px;background:var(--card)}
.rk-stat .rk-l{font-size:.8rem;color:var(--muted-foreground)}
.rk-stat .rk-v{font-size:1.35rem;font-weight:600;line-height:1.2}
.rk-stat .rk-d{font-size:.8rem;color:var(--muted-foreground)}
.rk-grid2{display:grid;grid-template-columns:1fr 1fr;gap:16px}
.rk-cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:12px}
.rk-card{border:1px solid var(--border);border-radius:var(--radius);padding:10px 12px;background:var(--card)}
.rk-tag{display:inline-block;font-size:.72rem;border:1px solid var(--border);border-radius:999px;padding:0 8px;color:var(--muted-foreground)}
.rk-phases{display:flex;flex-wrap:wrap;gap:8px;margin:1em 0;padding:0;list-style:none}
.rk-phases li{border:1px solid var(--border);border-radius:999px;padding:4px 12px;font-size:.85rem}
.rk-phases .done{border-color:#2f9e63}.rk-phases .active{border-color:var(--accent);color:var(--accent)}
.rk-phases .blocked{border-color:#d0483f;color:#d0483f}
.rk-tl{list-style:none;padding:0;margin:1em 0;border-left:2px solid var(--border)}
.rk-tl li{position:relative;padding:0 0 14px 16px}
.rk-tl li:before{content:"";position:absolute;left:-5px;top:.45em;width:8px;height:8px;border-radius:50%;background:var(--muted-foreground)}
.rk-tl .done:before{background:#2f9e63}.rk-tl .active:before{background:var(--accent)}
.rk-tl .blocked:before{background:#d0483f}
.rk-tl .rk-d8{font-size:.8rem;color:var(--muted-foreground)}
.rk-yes,.rk-no{margin:.2em 0}
.rk-yes:before{content:"+ ";color:#2f9e63}.rk-no:before{content:"− ";color:#d0483f}
@media (max-width:820px){.rk-grid2{grid-template-columns:1fr}}
"""

FALLBACK_JS = r"""<script>
/* Упрощённый просмотрщик: используется, только если web/js/report/index.js нет
   или рендерер упал. Полноценный вид даёт настоящий рендерер. */
window.__reportkitFallback = function (root, spec, note) {
  var esc = function (s) {
    return String(s === null || s === undefined ? '' : s).replace(/[&<>"']/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
    });
  };
  var arr = function (x) { return Array.isArray(x) ? x : []; };
  var md = function (s) {
    var t = esc(s).replace(/`([^`]+)`/g, '<code>$1</code>')
      .replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>')
      .replace(/(^|[\s(])\*([^*\n]+)\*/g, '$1<em>$2</em>');
    return t.split(/\n{2,}/).map(function (p) { return '<p>' + p.replace(/\n/g, '<br>') + '</p>'; }).join('');
  };
  var fmt = function (v, c) {
    if (v === null || v === undefined) return '';
    if (typeof v !== 'number') return esc(v);
    var d = c && typeof c.decimals === 'number' ? c.decimals : 0;
    var parts = v.toFixed(d).split('.');
    parts[0] = parts[0].replace(/\B(?=(\d{3})+(?!\d))/g, ' ');
    var s = parts.join(',');
    if (c && c.type === 'percent') return s + '%';
    if (c && c.type === 'money') return s + ' ' + (c.currency || '');
    return s;
  };
  var table = function (b) {
    var cols = arr(b.columns), rows = arr(b.rows);
    var h = '<table><thead><tr>' + cols.map(function (c) {
      return '<th>' + esc(c.label || c.key) + '</th>';
    }).join('') + '</tr></thead><tbody>';
    h += rows.map(function (r) {
      return '<tr>' + cols.map(function (c) { return '<td>' + fmt(r[c.key], c) + '</td>'; }).join('') + '</tr>';
    }).join('');
    return h + '</tbody></table>';
  };
  var phase = function (st) { return st === 'done' ? 'done' : st === 'active' ? 'active' : st === 'blocked' ? 'blocked' : 'next'; };
  var render = function (b) {
    if (!b || typeof b !== 'object') return '';
    var t = b.type, h = [];
    switch (t) {
      case 'heading':
        return '<h' + (b.level || 2) + '>' + esc(b.text) + '</h' + (b.level || 2) + '>';
      case 'text': return md(b.md);
      case 'callout':
        return '<div class="rk-callout ' + esc(b.tone || 'info') + '">' +
          (b.title ? '<div class="rk-ct">' + esc(b.title) + '</div>' : '') + md(b.md) + '</div>';
      case 'stats':
        return '<div class="rk-stats">' + arr(b.items).map(function (it) {
          return '<div class="rk-stat"><div class="rk-l">' + esc(it.label) + '</div><div class="rk-v">' +
            esc(it.value) + '</div>' + (it.delta ? '<div class="rk-d">' + esc(it.delta) + '</div>' : '') +
            (it.hint ? '<div class="rk-d">' + esc(it.hint) + '</div>' : '') + '</div>';
        }).join('') + '</div>';
      case 'table':
        return (b.title ? '<h3>' + esc(b.title) + '</h3>' : '') + table(b) +
          (b.footnote ? '<p class="rk-note">' + esc(b.footnote) + '</p>' : '');
      case 'chart':
        h.push('<div class="rk-card"><div class="rk-ct">' + esc(b.title || 'Диаграмма') + '</div>');
        h.push('<p class="rk-note">' + esc(b.kind) + (b.unit ? ', ' + esc(b.unit) : '') + '</p>');
        h.push('<table><thead><tr><th></th>' + arr(b.series).map(function (s) {
          return '<th>' + esc(s.name) + '</th>';
        }).join('') + '</tr></thead><tbody>');
        arr(b.x).forEach(function (x, i) {
          h.push('<tr><td>' + esc(x) + '</td>' + arr(b.series).map(function (s) {
            var v = arr(s.data)[i];
            if (v && typeof v === 'object') v = v.y;
            return '<td>' + fmt(v) + '</td>';
          }).join('') + '</tr>');
        });
        h.push('</tbody></table></div>');
        return h.join('');
      case 'graph':
        return '<div class="rk-card"><div class="rk-ct">' + esc(b.title || 'Связи') + '</div><ul>' +
          arr(b.edges).map(function (e) {
            return '<li>' + esc(e.from) + ' → ' + esc(e.to) + (e.label ? ' <span class="rk-tag">' + esc(e.label) + '</span>' : '') + '</li>';
          }).join('') + '</ul><p class="rk-note">Узлов: ' + arr(b.nodes).length + '</p></div>';
      case 'timeline':
        return '<ul class="rk-tl">' + arr(b.items).map(function (it) {
          return '<li class="' + phase(it.status) + '"><div class="rk-d8">' + esc(it.date) + (it.owner ? ' · ' + esc(it.owner) : '') +
            '</div><strong>' + esc(it.title) + '</strong>' + (it.md ? md(it.md) : '') + '</li>';
        }).join('') + '</ul>';
      case 'phases':
        return '<ul class="rk-phases">' + arr(b.items).map(function (it) {
          return '<li class="' + phase(it.status) + '">' + esc(it.title) + '</li>';
        }).join('') + '</ul>';
      case 'compare':
        return '<div class="rk-cards">' + arr(b.options).map(function (o) {
          return '<div class="rk-card"><div class="rk-ct">' + esc(o.name) + (o.score !== undefined ? ' · ' + esc(o.score) : '') + '</div>' +
            arr(o.pros).map(function (p) { return '<div class="rk-yes">' + esc(p) + '</div>'; }).join('') +
            arr(o.cons).map(function (c) { return '<div class="rk-no">' + esc(c) + '</div>'; }).join('') +
            (o.verdict ? '<p class="rk-note">' + esc(o.verdict) + '</p>' : '') + '</div>';
        }).join('') + '</div>';
      case 'matrix':
        if (b.kind === 'swot' && b.swot) {
          return '<div class="rk-grid2">' + ['strengths', 'weaknesses', 'opportunities', 'threats'].map(function (k) {
            return '<div class="rk-card"><div class="rk-ct">' + k + '</div>' +
              arr(b.swot[k]).map(function (x) { return '<div>· ' + esc(x) + '</div>'; }).join('') + '</div>';
          }).join('') + '</div>';
        }
        return '<table><thead><tr><th></th>' + arr(b.cols).map(function (c) { return '<th>' + esc(c) + '</th>'; }).join('') +
          '</tr></thead><tbody>' + arr(b.cells).map(function (row, i) {
            return '<tr><th>' + esc(arr(b.rows)[i] || '') + '</th>' + arr(row).map(function (c) { return '<td>' + fmt(c) + '</td>'; }).join('') + '</tr>';
          }).join('') + '</tbody></table>';
      case 'checklist':
        return '<ul>' + arr(b.items).map(function (it) {
          return '<li>' + (it.done ? '✓ ' : '☐ ') + esc(it.text) +
            (it.owner ? ' <span class="rk-tag">' + esc(it.owner) + '</span>' : '') +
            (it.due ? ' <span class="rk-d8">' + esc(it.due) + '</span>' : '') + '</li>';
        }).join('') + '</ul>';
      case 'kanban':
        return '<div class="rk-cards">' + arr(b.lanes).map(function (l) {
          return '<div class="rk-card"><div class="rk-ct">' + esc(l.title) + '</div>' + arr(l.cards).map(function (c) {
            return '<div class="rk-card" style="margin:6px 0"><strong>' + esc(c.title) + '</strong>' +
              (c.note ? '<div class="rk-note">' + esc(c.note) + '</div>' : '') + '</div>';
          }).join('') + '</div>';
        }).join('') + '</div>';
      case 'gallery':
        return '<div class="rk-cards">' + arr(b.images).map(function (im) {
          return '<figure style="margin:0"><img src="' + esc(im.src) + '" alt="' + esc(im.caption || '') + '">' +
            (im.caption ? '<figcaption class="rk-note">' + esc(im.caption) + '</figcaption>' : '') + '</figure>';
        }).join('') + '</div>';
      case 'code':
        return '<pre><code>' + esc(b.text) + '</code></pre>';
      case 'tabs':
        return arr(b.tabs).map(function (tab) {
          return '<h3>' + esc(tab.title) + '</h3>' + arr(tab.blocks).map(render).join('');
        }).join('');
      case 'accordion':
        return arr(b.items).map(function (it) {
          return '<details' + (it.open ? ' open' : '') + '><summary>' + esc(it.title) + '</summary>' +
            arr(it.blocks).map(render).join('') + '</details>';
        }).join('');
      case 'columns':
        return '<div class="rk-grid2">' + arr(b.cols).map(function (col) {
          return '<div>' + arr(col).map(render).join('') + '</div>';
        }).join('') + '</div>';
      case 'links':
        return '<ul>' + arr(b.items).map(function (it) {
          var href = it.url || window.__rkFileUrl(it.path || '');
          return '<li><a href="' + esc(href) + '">' + esc(it.title) + '</a>' +
            (it.note ? ' — ' + esc(it.note) : '') + '</li>';
        }).join('') + '</ul>';
      case 'sources':
        return '<ol class="rk-src">' + arr(b.items).map(function (it) {
          return '<li>' + esc(it.title) + ' — <span class="rk-note">' + esc(it.ref) + '</span></li>';
        }).join('') + '</ol>';
      default:
        return '<pre class="rk-note">' + esc(JSON.stringify(b)) + '</pre>';
    }
  };
  var out = ['<div class="rk-report">'];
  if (note) out.push('<p class="rk-note">' + esc(note) + '. Показан упрощённый вид.</p>');
  out.push('<h1>' + esc(spec.title || 'Отчёт') + '</h1>');
  if (spec.subtitle) out.push('<p class="rk-sub">' + esc(spec.subtitle) + '</p>');
  if (spec.summary) out.push('<div class="rk-summary">' + md(spec.summary) + '</div>');
  arr(spec.blocks).forEach(function (b) { out.push(render(b)); });
  out.push('</div>');
  // Безопасно: каждая подстановка выше проходит через esc()/md() (md сначала экранирует).
  root.innerHTML = out.join('');
};
</script>"""


# --- сборка ---------------------------------------------------------------
