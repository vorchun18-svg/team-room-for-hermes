/**
 * Inline Markdown → HTML (the inline half of md.js).
 *
 * Split out so both files stay reviewable. Public entry point is mdInline();
 * md.js re-exports it, so consumers import everything from './md.js'.
 *
 * Security: the source is never treated as HTML. Every text run goes through
 * esc(); the only tags emitted are the ones this file builds. Link targets are
 * scheme-checked — javascript:/data:/vbscript:/anything-unknown is dropped and
 * only the label text survives.
 */

import { esc } from './util.js';

const MAX_INLINE_DEPTH = 8;
const SCHEME_RE = /^([a-z][a-z0-9+.-]*):/i;

/** Default local-file resolver: the app serves whitelisted files at /api/file. */
export const defaultFileUrl = (p) => '/api/file?path=' + encodeURIComponent(p);

/* ------------------------------------------------------------ inline level */

/**
 * Validate a link target.
 * @returns {{kind:'external'|'local'|'anchor', href?:string, path?:string}|null}
 *   null means "unsafe / empty → drop the link".
 */
function safeUrl(raw) {
  const u = String(raw === null || raw === undefined ? '' : raw).trim();
  if (!u) return null;
  if (/^[A-Za-z]:[\\/]/.test(u) || u.startsWith('\\\\')) return { kind: 'local', path: u }; // C:\… , \\server\…
  const probe = u.replace(/[\u0000-\u0020\u007f]/g, '');
  const m = SCHEME_RE.exec(probe);
  if (!m) {
    if (u[0] === '#') return { kind: 'anchor', href: u };
    return { kind: 'local', path: u };
  }
  const scheme = m[1].toLowerCase();
  if (scheme === 'http' || scheme === 'https' || scheme === 'mailto') {
    return { kind: 'external', href: u.replace(/[\u0000-\u001f\u007f]/g, '') };
  }
  if (scheme === 'file') {
    let p = u.replace(/^file:\/*/i, '');
    p = p.replace(/^localhost\//i, '/');
    try { p = decodeURIComponent(p); } catch { /* keep raw */ }
    return p ? { kind: 'local', path: p } : null;
  }
  return null; // javascript:, data:, vbscript:, everything else
}

/** Find the ']' that closes the '[' at openIdx, skipping escapes and code spans. */
function matchBracket(src, openIdx) {
  let depth = 0;
  let i = openIdx;
  const n = src.length;
  while (i < n) {
    const c = src[i];
    if (c === '\\') { i += 2; continue; }
    if (c === '`') {
      let run = 1;
      while (src[i + run] === '`') run++;
      const fence = '`'.repeat(run);
      const close = src.indexOf(fence, i + run);
      if (close !== -1) { i = close + run; continue; }
      i += run; continue;
    }
    if (c === '[') { depth++; i++; continue; }
    if (c === ']') { depth--; if (depth === 0) return i; i++; continue; }
    i++;
  }
  return -1;
}

/** Parse '(url "title")' starting right after '('. */
function parseDest(src, start) {
  let i = start;
  const n = src.length;
  const skipWs = () => {
    while (i < n && (src[i] === ' ' || src[i] === '\t' || src[i] === '\n')) i++;
  };
  skipWs();
  let url = '';
  if (src[i] === '<') {
    const close = src.indexOf('>', i + 1);
    if (close === -1) return null;
    url = src.slice(i + 1, close);
    i = close + 1;
  } else {
    let depth = 0;
    while (i < n) {
      const c = src[i];
      if (c === '\\' && (src[i + 1] === '(' || src[i + 1] === ')' || src[i + 1] === '\\')) {
        url += src[i + 1]; i += 2; continue;
      }
      if (c === '(') { depth++; url += c; i++; continue; }
      if (c === ')') { if (depth === 0) break; depth--; url += c; i++; continue; }
      if (c === ' ' || c === '\t' || c === '\n') break;
      url += c; i++;
    }
  }
  skipWs();
  let title = '';
  const q = src[i];
  if (q === '"' || q === "'" || q === '(') {
    const endCh = q === '(' ? ')' : q;
    i++;
    let t = '';
    while (i < n && src[i] !== endCh) {
      if (src[i] === '\\' && i + 1 < n) { t += src[i + 1]; i += 2; continue; }
      t += src[i]; i++;
    }
    if (i >= n) return null;
    title = t;
    i++;
  }
  skipWs();
  if (src[i] !== ')') return null;
  return { url, title, end: i + 1 };
}

/** Build the <a> for a link, or return the label alone when the URL is unsafe. */
function linkHtml(url, labelHtml, title) {
  const safe = safeUrl(url);
  if (!safe) return labelHtml;
  const t = title ? ` title="${esc(title)}"` : '';
  if (safe.kind === 'external') {
    return `<a href="${esc(safe.href)}"${t} target="_blank" rel="noopener noreferrer">${labelHtml}</a>`;
  }
  if (safe.kind === 'anchor') return `<a href="${esc(safe.href)}"${t}>${labelHtml}</a>`;
  return `<a href="#" class="md-file" data-path="${esc(safe.path)}"${t}>${labelHtml}</a>`;
}

const DATA_IMG_RE = /^data:image\/(png|jpe?g|gif|webp|bmp);base64,[A-Za-z0-9+/=\s]+$/i;

/** Build the <img> for an image, or its alt text when the source is refused. */
function imageHtml(alt, url, title, ctx) {
  const altHtml = esc(alt);
  const u = String(url === null || url === undefined ? '' : url).trim();
  if (/^data:image\//i.test(u)) {
    return DATA_IMG_RE.test(u)
      ? `<img class="md-img" src="${esc(u.replace(/\s+/g, ''))}" alt="${altHtml}">`
      : altHtml; // svg+xml and other data URIs are refused
  }
  const safe = safeUrl(u);
  if (!safe || safe.kind === 'anchor') return altHtml;
  if (safe.kind === 'external') {
    const label = alt ? altHtml : esc(u);
    return `<a class="md-img-link" href="${esc(safe.href)}" target="_blank" rel="noopener noreferrer">${label}</a>`;
  }
  const src = ctx.fileUrl(safe.path);
  return `<img class="md-img" src="${esc(src)}" alt="${altHtml}"${title ? ` title="${esc(title)}"` : ''} loading="lazy">`;
}

/** Что может стоять перед открывающим '_' / '__' (кроме начала строки и пробела). */
const UNDERSCORE_OPENERS = "([{<>«„“‘\"'—–";

/**
 * Может ли серия '_' длиной run, начинающаяся в позиции i, открыть выделение.
 * Строже CommonMark: перед ней — начало текста, пробел или скобка/кавычка/тире,
 * а не буква, цифра, точка и т.п. (window.__xss, os.path.__file__, snake_case),
 * и сразу за ней — не пробел.
 */
function canOpenUnderscore(src, i, run) {
  const prev = i > 0 ? src[i - 1] : '';
  const next = i + run < src.length ? src[i + run] : '';
  if (!next || /\s/.test(next)) return false;
  if (prev === '' || /\s/.test(prev)) return true;
  return UNDERSCORE_OPENERS.includes(prev);
}

/** Find the closing delimiter `mark`, skipping escapes and code spans. */
function findCloser(src, from, mark, ch) {
  const len = mark.length;
  let i = from;
  const n = src.length;
  while (i < n) {
    const c = src[i];
    if (c === '\\') { i += 2; continue; }
    if (c === '`') {
      let run = 1;
      while (src[i + run] === '`') run++;
      const fence = '`'.repeat(run);
      const close = src.indexOf(fence, i + run);
      if (close !== -1) { i = close + run; continue; }
      i += run; continue;
    }
    if (src.startsWith(mark, i)) {
      const before = i > 0 ? src[i - 1] : '';
      const after = i + len < n ? src[i + len] : '';
      let ok = before !== '' && before !== ' ' && before !== '\n' && before !== '\t';
      if (ok && len === 1) {
        if (src[i + 1] === ch || before === ch) ok = false; // part of a run
      }
      // '_' / '__' внутри слова (snake_case, a__b) не закрывает выделение.
      if (ok && ch === '_' && /[\p{L}\p{N}]/u.test(after)) ok = false;
      // '__init__.py': за закрывающим '_' идёт «.расширение» — это одно слово-имя.
      if (ok && ch === '_' && after === '.' && /[\p{L}\p{N}]/u.test(src[i + len + 1] || '')) ok = false;
      if (ok && len === 2 && src[i + 2] === ch) { i += 1; continue; } // '***' → closer starts one later
      if (ok) return i;
      i += len;
      continue;
    }
    i++;
  }
  return -1;
}

/** Render inline markup. Never emits unescaped source text. */
export function inlineScan(src, ctx, depth) {
  if (depth > MAX_INLINE_DEPTH) return esc(src);
  let out = '';
  let text = '';
  const n = src.length;
  let i = 0;
  const flush = () => { if (text) { out += esc(text); text = ''; } };

  while (i < n) {
    const ch = src[i];

    if (ch === '\n') {
      const tl = text.length;
      if (tl >= 2 && text[tl - 1] === ' ' && text[tl - 2] === ' ') {
        text = text.replace(/ +$/, '');
        flush();
        out += '<br>';
      } else if (tl >= 1 && text[tl - 1] === '\r') {
        text = text.slice(0, -1);
        text += ' ';
      } else {
        text += ' ';
      }
      i++;
      continue;
    }

    if (ch === '\\') {
      const nx = src[i + 1];
      if (nx && /[\\`*_{}[\]()#+\-.!~>|"'$]/.test(nx)) { text += nx; i += 2; continue; }
      text += '\\';
      i++;
      continue;
    }

    if (ch === '`') {
      let run = 1;
      while (src[i + run] === '`') run++;
      const fence = '`'.repeat(run);
      const close = src.indexOf(fence, i + run);
      if (close !== -1) {
        let code = src.slice(i + run, close).replace(/\n/g, ' ');
        if (code.length > 2 && code[0] === ' ' && code[code.length - 1] === ' ' && code.trim()) {
          code = code.slice(1, -1);
        }
        flush();
        out += '<code class="md-code-inline">' + esc(code) + '</code>';
        i = close + run;
        continue;
      }
      text += fence;
      i += run;
      continue;
    }

    if (ch === '!' && src[i + 1] === '[') {
      const close = matchBracket(src, i + 1);
      if (close !== -1 && src[close + 1] === '(') {
        const dest = parseDest(src, close + 2);
        if (dest) {
          flush();
          out += imageHtml(src.slice(i + 2, close), dest.url, dest.title, ctx);
          i = dest.end;
          continue;
        }
      }
      text += ch;
      i++;
      continue;
    }

    if (ch === '[') {
      const close = matchBracket(src, i);
      if (close !== -1 && src[close + 1] === '(') {
        const dest = parseDest(src, close + 2);
        if (dest) {
          const label = inlineScan(src.slice(i + 1, close), ctx, depth + 1);
          flush();
          out += linkHtml(dest.url, label, dest.title);
          i = dest.end;
          continue;
        }
      }
      text += ch;
      i++;
      continue;
    }

    if ((ch === 'h' || ch === 'H') && /^https?:\/\//i.test(src.slice(i, i + 8))) {
      const prev = i > 0 ? src[i - 1] : '';
      if (!/[\p{L}\p{N}_/@.]/u.test(prev)) {
        let j = i;
        while (j < n && !/[\s<>"'`]/.test(src[j])) j++;
        let url = src.slice(i, j);
        let tail = '';
        while (url.length && /[.,;:!?]$/.test(url)) { tail = url.slice(-1) + tail; url = url.slice(0, -1); }
        while (url.endsWith(')') && (url.match(/\(/g) || []).length < (url.match(/\)/g) || []).length) {
          tail = ')' + tail;
          url = url.slice(0, -1);
        }
        if (url.length > 8) {
          flush();
          out += `<a href="${esc(url)}" target="_blank" rel="noopener noreferrer">${esc(url)}</a>` + esc(tail);
          i = j;
          continue;
        }
      }
      text += ch;
      i++;
      continue;
    }

    if (ch === '~' && src[i + 1] === '~') {
      const close = src.indexOf('~~', i + 2);
      if (close > i + 2) {
        flush();
        out += '<del>' + inlineScan(src.slice(i + 2, close), ctx, depth + 1) + '</del>';
        i = close + 2;
        continue;
      }
      text += ch;
      i++;
      continue;
    }

    if (ch === '*' || ch === '_') {
      let run = 1;
      while (src[i + run] === ch) run++;
      if (ch === '_' && !canOpenUnderscore(src, i, run)) {
        // window.__xss, snake_case, a_b_c — подчёркивания остаются текстом.
        text += ch.repeat(run);
        i += run;
        continue;
      }
      const capped = run >= 3 ? 3 : run;
      const mark = ch.repeat(capped);
      const close = findCloser(src, i + capped, mark, ch);
      if (close !== -1 && close > i + capped) {
        const inner = inlineScan(src.slice(i + capped, close), ctx, depth + 1);
        flush();
        if (capped === 3) out += `<strong><em>${inner}</em></strong>`;
        else if (capped === 2) out += `<strong>${inner}</strong>`;
        else out += `<em>${inner}</em>`;
        i = close + capped;
        continue;
      }
      text += mark;
      i += capped;
      continue;
    }

    text += ch;
    i++;
  }
  flush();
  return out;
}

/**
 * Render only inline markup (no block wrapping).
 * @param {string} src
 * @param {{fileUrl?: Function}} [opts]
 * @returns {string} safe HTML
 */
export function mdInline(src, opts = {}) {
  const ctx = { fileUrl: (opts && typeof opts.fileUrl === 'function') ? opts.fileUrl : defaultFileUrl };
  return inlineScan(String(src === null || src === undefined ? '' : src), ctx, 0);
}
