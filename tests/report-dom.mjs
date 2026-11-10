/**
 * Расширение tests/fake-dom.mjs для проверки markdown-рендерера отчёта.
 *
 * fake-dom нарочно кидает на innerHTML — так ловится регресс h()/applyAttrs.
 * Но у отчёта есть ровно один разрешённый путь HTML (web/js/report/md-node.js),
 * и чтобы проверить его результат, нужен DOM, который умеет innerHTML.
 *
 * Здесь — минимальный разбор HTML: теги → элементы, текст → текстовые узлы
 * (сущности раскрываются). Скрипты не исполняются (это Node), поэтому тест
 * проверяет именно структуру: нет <script>, нет on*-атрибутов, текст непустой.
 */

import { FakeElement, FakeText } from './fake-dom.mjs';

const VOID = new Set(['br', 'img', 'hr', 'input', 'meta', 'link', 'source', 'col', 'area', 'base', 'embed', 'param', 'track', 'wbr']);
const TOKEN = /<!--[\s\S]*?-->|<\/?[a-zA-Z][^>]*>|[^<]+/g;
const ATTR = /([a-zA-Z_:][-a-zA-Z0-9_:.]*)(?:\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s"'>]+)))?/g;

export function decodeEntities(s) {
  return String(s)
    .replace(/&#(\d+);/g, (_, n) => String.fromCodePoint(Number(n)))
    .replace(/&#x([0-9a-f]+);/gi, (_, n) => String.fromCodePoint(parseInt(n, 16)))
    .replace(/&lt;/g, '<').replace(/&gt;/g, '>')
    .replace(/&quot;/g, '"').replace(/&#39;/g, "'")
    .replace(/&nbsp;/g, '\u00a0')
    .replace(/&amp;/g, '&');
}

export class HtmlElement extends FakeElement {
  constructor(tagName, namespaceURI = 'http://www.w3.org/1999/xhtml') {
    super(tagName, namespaceURI);
    this._html = '';
  }

  /** Разобрать HTML в дочерние узлы — только для тестов. */
  set innerHTML(html) {
    this._html = String(html);
    this.childNodes = [];
    const stack = [this];
    const src = String(html);
    let m;
    TOKEN.lastIndex = 0;
    while ((m = TOKEN.exec(src))) {
      const token = m[0];
      const parent = stack[stack.length - 1];
      if (token.startsWith('<!--')) continue;
      if (token.startsWith('</')) {
        const name = token.slice(2, -1).trim().toLowerCase();
        for (let i = stack.length - 1; i > 0; i -= 1) {
          if (stack[i].localName === name) { stack.length = i; break; }
        }
        continue;
      }
      if (token.startsWith('<')) {
        const nameMatch = /^<([a-zA-Z][-a-zA-Z0-9:]*)([\s\S]*?)\/?>$/.exec(token);
        if (!nameMatch) { parent.appendChild(new FakeText(token)); continue; }
        const name = nameMatch[1].toLowerCase();
        const el = new HtmlElement(name, name === 'svg' ? 'http://www.w3.org/2000/svg' : undefined);
        const attrs = nameMatch[2] || '';
        let a;
        ATTR.lastIndex = 0;
        while ((a = ATTR.exec(attrs))) {
          const key = a[1];
          const val = a[2] !== undefined ? a[2] : a[3] !== undefined ? a[3] : a[4] !== undefined ? a[4] : '';
          el.setAttribute(key, decodeEntities(val));
        }
        parent.appendChild(el);
        if (!VOID.has(name) && !/\/>$/.test(token)) stack.push(el);
        continue;
      }
      parent.appendChild(new FakeText(decodeEntities(token)));
    }
  }

  get innerHTML() { return this._html; }

  /** Фрагмент вставляется своими детьми, как в настоящем DOM. */
  appendChild(node) {
    if (node && node.nodeType === 11) {
      for (const child of [...node.childNodes]) this.appendChild(child);
      node.childNodes = [];
      return node;
    }
    return super.appendChild(node);
  }

  /** Простейшие селекторы: `.class`, `tag`, `[attr]` (для checklist/tabs). */
  querySelectorAll(sel) {
    const out = [];
    const parts = String(sel).split(',').map((s) => s.trim()).filter(Boolean);
    const walk = (node) => {
      for (const child of node.childNodes) {
        if (child.nodeType !== 1) continue;
        for (const part of parts) {
          if (part.startsWith('.') && child.classList.contains(part.slice(1))) { out.push(child); break; }
          if (part.startsWith('#') && child.id === part.slice(1)) { out.push(child); break; }
          if (part.startsWith('[') && child.hasAttribute(part.slice(1, -1))) { out.push(child); break; }
          if (/^[a-z0-9-]+$/i.test(part) && child.localName === part.toLowerCase()) { out.push(child); break; }
        }
        walk(child);
      }
    };
    walk(this);
    return out;
  }

  querySelector(sel) { return this.querySelectorAll(sel)[0] || null; }

  focus() { this._focused = true; }
}

class HtmlFragment extends HtmlElement {
  constructor() { super('#fragment'); this.nodeType = 11; }
}

/** Документ с поддержкой innerHTML (только для тестов отчёта). */
export function createHtmlDocument() {
  const doc = {
    createElement(tag) { return new HtmlElement(tag); },
    createElementNS(_ns, tag) { return new HtmlElement(tag, _ns); },
    createTextNode(text) { return new FakeText(text); },
    createDocumentFragment() { return new HtmlFragment(); },
    querySelectorAll() { return []; },
    querySelector() { return null; },
    addEventListener() {},
    removeEventListener() {},
  };
  doc.head = new HtmlElement('head');
  doc.body = new HtmlElement('body');
  return doc;
}

/** Поставить тестовый DOM + globalThis.Node (его ждёт report/index.js). */
export function installHtmlDom() {
  globalThis.document = createHtmlDocument();
  globalThis.Node = HtmlElement;
  return globalThis.document;
}

export function uninstallHtmlDom() {
  delete globalThis.document;
  delete globalThis.Node;
}

/** Собрать текст всех узлов (для проверок «непустой»). */
export function allText(node) {
  if (!node) return '';
  if (node.nodeType === 3) return node.data;
  let out = '';
  for (const child of node.childNodes || []) out += allText(child);
  return out;
}
