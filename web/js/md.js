/**
 * Safe Markdown → HTML renderer for agent answers.
 *
 * Security model: the source is NEVER treated as HTML. Every text run goes
 * through esc(); the only tags in the output are the ones this file emits.
 * Link targets are scheme-checked (javascript:/data:/vbscript:/unknown → the
 * link is dropped and only its label text remains).
 *
 * Supported: #–#### headings, paragraphs, **bold**, *italic*, ~~strike~~,
 * `inline code`, ``` fenced code ``` (language-xx class, no highlighting),
 * > quotes, nested ul/ol (3+ levels), task lists, GFM tables with alignment,
 * ---, links/images (local paths → data-path / fileUrl), autolinks.
 */

import { esc } from './util.js';
import { inlineScan, defaultFileUrl } from './md-inline.js';

export { mdInline } from './md-inline.js';

const MAX_LIST_DEPTH = 8;

const FENCE_RE = /^ {0,3}(`{3,}|~{3,})[ \t]*(.*)$/;
const HEADING_RE = /^ {0,3}(#{1,6})[ \t]+(.*)$/;
const HR_RE = /^ {0,3}(?:(?:\*[ \t]*){3,}|(?:-[ \t]*){3,}|(?:_[ \t]*){3,})$/;
const QUOTE_RE = /^ {0,3}>[ \t]?(.*)$/;
const LIST_RE = /^( *)([-*+]|\d{1,9}[.)])[ \t]+(.*)$/;
const TABLE_DELIM_RE = /^[ \t]*\|?[ \t]*:?-+:?[ \t]*(?:\|[ \t]*:?-+:?[ \t]*)*\|?[ \t]*$/;

/**
 * Normalize line endings and whitespace before parsing.
 * @param {string} src
 * @returns {string}
 */
function normalize(src) {
  let s = src === null || src === undefined ? '' : String(src);
  if (s.charCodeAt(0) === 0xfeff) s = s.slice(1);
  s = s.replace(/\r\n?/g, '\n');
  // strip control chars except \n and \t (keeps output clean, kills NUL tricks)
  s = s.replace(/[\u0000-\u0008\u000b\u000c\u000e-\u001f]/g, '');
  // expand tabs only in the leading indentation
  s = s.replace(/^[ \t]+/gm, (m) => m.replace(/\t/g, '    '));
  return s;
}

/** Number of leading spaces. */
function indentOf(line) {
  let k = 0;
  while (k < line.length && line[k] === ' ') k++;
  return k;
}

function dedent(line, count) {
  let k = 0;
  while (k < count && line[k] === ' ') k++;
  return line.slice(k);
}

/* ------------------------------------------------------------- block level */

/** Split a table row into trimmed cells; respects `\|` and code spans. */
function splitRow(line) {
  let s = String(line).trim();
  if (s.startsWith('|')) s = s.slice(1);
  if (s.endsWith('|') && !s.endsWith('\\|')) s = s.slice(0, -1);
  const cells = [];
  let cur = '';
  let inCode = false;
  let codeRun = 0;
  let i = 0;
  const n = s.length;
  while (i < n) {
    const c = s[i];
    if (c === '\\' && s[i + 1] === '|') { cur += '|'; i += 2; continue; }
    if (c === '`') {
      let run = 1;
      while (s[i + run] === '`') run++;
      if (!inCode) { inCode = true; codeRun = run; } else if (run === codeRun) inCode = false;
      cur += s.slice(i, i + run);
      i += run;
      continue;
    }
    if (c === '|' && !inCode) { cells.push(cur); cur = ''; i++; continue; }
    cur += c;
    i++;
  }
  cells.push(cur);
  return cells.map((x) => x.trim());
}

function isTableStart(lines, i) {
  if (i + 1 >= lines.length) return false;
  const line = lines[i];
  if (line.indexOf('|') === -1) return false;
  const delim = lines[i + 1];
  if (!TABLE_DELIM_RE.test(delim)) return false;
  return splitRow(line).length === splitRow(delim).length;
}

function parseTable(lines, start, ctx) {
  const header = splitRow(lines[start]);
  const delim = splitRow(lines[start + 1]);
  const cols = header.length;
  const align = [];
  for (let c = 0; c < cols; c++) {
    const d = delim[c] || '';
    const l = d.startsWith(':');
    const r = d.endsWith(':');
    align.push(l && r ? 'center' : r ? 'right' : l ? 'left' : '');
  }
  const rows = [];
  let i = start + 2;
  while (i < lines.length && lines[i].trim() && lines[i].indexOf('|') !== -1) {
    const cells = splitRow(lines[i]);
    const norm = [];
    for (let c = 0; c < cols; c++) norm.push(cells[c] === undefined ? '' : cells[c]);
    rows.push(norm);
    i++;
  }
  const styleOf = (c) => (align[c] ? ` style="text-align:${align[c]}"` : '');
  const head = header.map((cell, c) => `<th${styleOf(c)}>${inlineScan(cell, ctx, 0)}</th>`).join('');
  const body = rows
    .map((cells) => '<tr>' + cells.map((cell, c) => `<td${styleOf(c)}>${inlineScan(cell, ctx, 0)}</td>`).join('') + '</tr>')
    .join('\n');
  return {
    html: `<table class="md-table"><thead><tr>${head}</tr></thead><tbody>\n${body}\n</tbody></table>`,
    next: i,
  };
}

function isBlockStart(lines, i, ctx) {
  const line = lines[i];
  if (!line.trim()) return false;
  if (FENCE_RE.test(line) || HEADING_RE.test(line) || HR_RE.test(line) || QUOTE_RE.test(line)) return true;
  if (LIST_RE.test(line) && ctx.depth <= MAX_LIST_DEPTH) return true;
  return isTableStart(lines, i);
}

function parseList(lines, start, ctx) {
  const first = LIST_RE.exec(lines[start]);
  if (!first) return { html: '', next: start + 1 };
  const baseIndent = first[1].length;
  const ordered = /^\d/.test(first[2]);
  const startNum = ordered ? parseInt(first[2], 10) : 1;
  const items = [];
  let i = start;
  const n = lines.length;

  while (i < n) {
    const line = lines[i];
    if (!line.trim()) {
      // a blank line only ends the list when no same-level item follows
      let j = i + 1;
      while (j < n && !lines[j].trim()) j++;
      const nm = j < n ? LIST_RE.exec(lines[j]) : null;
      if (nm && nm[1].length === baseIndent && /^\d/.test(nm[2]) === ordered) { i = j; continue; }
      break;
    }
    const m = LIST_RE.exec(line);
    if (!m) break;
    if (m[1].length !== baseIndent) break;
    if (/^\d/.test(m[2]) !== ordered) break;
    const contentIndent = baseIndent + m[2].length + 1;
    const itemLines = [m[3]];
    i++;
    while (i < n) {
      const raw = lines[i];
      if (!raw.trim()) {
        let j = i + 1;
        while (j < n && !lines[j].trim()) j++;
        if (j >= n) break;
        const nm = LIST_RE.exec(lines[j]);
        const nInd = nm ? nm[1].length : indentOf(lines[j]);
        if (nInd > baseIndent) { itemLines.push(''); i++; continue; }
        break;
      }
      const nm = LIST_RE.exec(raw);
      const ind = nm ? nm[1].length : indentOf(raw);
      if (ind <= baseIndent) break;
      itemLines.push(dedent(raw, Math.min(ind, contentIndent)));
      i++;
    }
    items.push(itemLines);
  }

  const tag = ordered ? 'ol' : 'ul';
  const startAttr = ordered && startNum !== 1 ? ` start="${startNum}"` : '';
  const html = items.map((itemLines) => {
    const content = itemLines.slice();
    let check = '';
    const task = /^\[([ xX])\][ \t]+/.exec(content[0] || '');
    if (task) {
      content[0] = (content[0] || '').slice(task[0].length);
      check = `<input type="checkbox" class="md-check" disabled${task[1].toLowerCase() === 'x' ? ' checked' : ''}> `;
    }
    let body;
    if (ctx.depth >= MAX_LIST_DEPTH) {
      body = inlineScan(content.join('\n'), ctx, 0);
    } else {
      body = renderBlocks(content, { ...ctx, depth: ctx.depth + 1 });
      // tight list: unwrap the single paragraph so `<li>текст</li>` (and text before a nested list)
      if ((body.match(/<p>/g) || []).length === 1) {
        const only = /^<p>([\s\S]*?)<\/p>/.exec(body);
        if (only) body = only[1] + body.slice(only[0].length);
      }
    }
    return `<li${task ? ' class="md-task"' : ''}>${check}${body}</li>`;
  }).join('\n');

  return { html: `<${tag}${startAttr}>\n${html}\n</${tag}>`, next: i };
}

function renderBlocks(lines, ctx) {
  const out = [];
  const n = lines.length;
  let i = 0;

  while (i < n) {
    const line = lines[i];
    if (!line.trim()) { i++; continue; }

    const fence = FENCE_RE.exec(line);
    if (fence) {
      const marker = fence[1];
      const info = String(fence[2] || '');
      const lang = String(info.split(/[ \t]+/)[0] || '').replace(/[^A-Za-z0-9_+#.-]/g, '');
      const body = [];
      i++;
      const closeRe = new RegExp('^ {0,3}' + (marker[0] === '`' ? '`' : '~') + '{' + marker.length + ',}[ \\t]*$');
      while (i < n && !closeRe.test(lines[i])) { body.push(lines[i]); i++; }
      if (i < n) i++;
      out.push('<pre class="md-code"><code' + (lang ? ` class="language-${lang}"` : '') + '>'
        + esc(body.join('\n')) + '</code></pre>');
      continue;
    }

    const heading = HEADING_RE.exec(line);
    if (heading) {
      const level = Math.min(heading[1].length, 4);
      const textSrc = heading[2].replace(/[ \t]+#+[ \t]*$/, '');
      out.push(`<h${level}>${inlineScan(textSrc, ctx, 0)}</h${level}>`);
      i++;
      continue;
    }

    if (HR_RE.test(line)) { out.push('<hr>'); i++; continue; }

    if (QUOTE_RE.test(line)) {
      const buf = [];
      while (i < n) {
        const m = QUOTE_RE.exec(lines[i]);
        if (m) { buf.push(m[1]); i++; continue; }
        if (!lines[i].trim() && i + 1 < n && QUOTE_RE.test(lines[i + 1])) { buf.push(''); i++; continue; }
        break;
      }
      out.push('<blockquote>' + renderBlocks(buf, { ...ctx, depth: ctx.depth + 1 }) + '</blockquote>');
      continue;
    }

    if (isTableStart(lines, i)) {
      const tbl = parseTable(lines, i, ctx);
      out.push(tbl.html);
      i = tbl.next;
      continue;
    }

    if (LIST_RE.test(line) && ctx.depth < MAX_LIST_DEPTH) {
      const res = parseList(lines, i, ctx);
      if (res.next > i) { out.push(res.html); i = res.next; continue; }
    }

    const para = [line];
    i++;
    while (i < n && lines[i].trim() && !isBlockStart(lines, i, ctx)) { para.push(lines[i]); i++; }
    out.push('<p>' + inlineScan(para.join('\n'), ctx, 0) + '</p>');
  }

  return out.join('\n');
}

/**
 * Render Markdown to safe HTML. The result is not wrapped in a container.
 * @param {string} src
 * @param {{fileUrl?: (path:string)=>string}} [opts] fileUrl maps local image
 *   paths to a URL; default '/api/file?path=<encoded>'
 * @returns {string} HTML
 */
export function renderMarkdown(src, opts = {}) {
  const ctx = {
    fileUrl: (opts && typeof opts.fileUrl === 'function') ? opts.fileUrl : defaultFileUrl,
    depth: 0,
  };
  return renderBlocks(normalize(src).split('\n'), ctx);
}

/**
 * Plain text for snippets: markup stripped, whitespace collapsed, truncated.
 * @param {string} src
 * @param {number} [maxLen]
 * @returns {string}
 */
export function mdToText(src, maxLen) {
  let t = normalize(src);
  t = t.replace(/^ {0,3}(`{3,}|~{3,}).*$/gm, ' ');
  t = t.replace(/!\[([^\]]*)\]\([^)]*\)/g, '$1');
  t = t.replace(/\[([^\]]*)\]\([^)]*\)/g, '$1');
  t = t.replace(/^ {0,3}#{1,6}[ \t]+/gm, '');
  t = t.replace(/^[ \t]*>[ \t]?/gm, '');
  t = t.replace(/^[ \t]*([-*+]|\d{1,9}[.)])[ \t]+(?:\[[ xX]\][ \t]+)?/gm, '');
  t = t.replace(/^[ \t]*\|?[ \t:|-]{4,}$/gm, ' ');
  t = t.replace(/^[ \t]*([-*_])(?:[ \t]*\1){2,}[ \t]*$/gm, ' ');
  t = t.replace(/\|/g, ' ');
  t = t.replace(/<[^>]*>/g, '');
  t = t.replace(/<\/?(script|style|svg|iframe)[^>]*>/gi, '');
  t = t.replace(/\*\*|~~|`/g, '');
  t = t.replace(/\*([^*\n]+)\*/g, '$1');
  t = t.replace(/(^|[\s(«"'])_{1,2}([^_\n]+)_{1,2}(?=[\s).,!?:;»"']|$)/g, '$1$2');
  t = t.replace(/[ \t]+/g, ' ');
  t = t.replace(/ ?\n ?/g, ' ');
  t = t.replace(/\s+/g, ' ').trim();
  const limit = Number(maxLen);
  if (isFinite(limit) && limit > 1 && t.length > limit) {
    t = t.slice(0, limit - 1).trimEnd() + '…';
  }
  return t;
}
