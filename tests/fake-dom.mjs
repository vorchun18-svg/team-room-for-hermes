/**
 * Minimal fake DOM so h()/s() can be tested under `node --test` with no browser.
 * Deliberately throws on innerHTML: if a builder ever regresses to innerHTML,
 * the test fails loudly instead of silently passing.
 */

export class FakeText {
  constructor(data) {
    this.nodeType = 3;
    this.data = String(data);
    this.parentNode = null;
  }

  get textContent() { return this.data; }
  set textContent(v) { this.data = String(v); }
}

export class FakeElement {
  constructor(tagName, namespaceURI = 'http://www.w3.org/1999/xhtml') {
    this.nodeType = 1;
    this.tagName = tagName;
    this.localName = String(tagName).toLowerCase();
    this.namespaceURI = namespaceURI;
    this.attributes = new Map();
    this.childNodes = [];
    this.parentNode = null;
    this.listeners = Object.create(null);
    const style = {};
    Object.defineProperty(style, 'setProperty', {
      value: (k, v) => { style[k] = String(v); },
      enumerable: false,
    });
    this.style = style;
  }

  setAttribute(k, v) { this.attributes.set(String(k), String(v)); }
  getAttribute(k) { return this.attributes.has(String(k)) ? this.attributes.get(String(k)) : null; }
  hasAttribute(k) { return this.attributes.has(String(k)); }
  removeAttribute(k) { this.attributes.delete(String(k)); }

  appendChild(node) {
    if (!node || typeof node.nodeType !== 'number') {
      throw new TypeError('appendChild expects a node');
    }
    node.parentNode = this;
    this.childNodes.push(node);
    return node;
  }

  get className() { return this.getAttribute('class') || ''; }
  set className(v) { this.setAttribute('class', v); }

  get id() { return this.getAttribute('id') || ''; }
  set id(v) { this.setAttribute('id', v); }

  get classList() {
    const self = this;
    const list = () => self.className.split(/\s+/).filter(Boolean);
    return {
      add: (...names) => self.setAttribute('class', [...new Set([...list(), ...names])].join(' ')),
      remove: (...names) => self.setAttribute('class', list().filter((c) => !names.includes(c)).join(' ')),
      contains: (name) => list().includes(name),
      toggle: (name, force) => {
        const has = list().includes(name);
        const want = force === undefined ? !has : !!force;
        if (want && !has) self.setAttribute('class', [...list(), name].join(' '));
        if (!want && has) self.setAttribute('class', list().filter((c) => c !== name).join(' '));
        return want;
      },
    };
  }

  get dataset() {
    const self = this;
    return new Proxy({}, {
      get(_t, prop) {
        const name = 'data-' + String(prop).replace(/[A-Z]/g, (m) => '-' + m.toLowerCase());
        const v = self.getAttribute(name);
        return v === null ? undefined : v;
      },
      set(_t, prop, value) {
        const name = 'data-' + String(prop).replace(/[A-Z]/g, (m) => '-' + m.toLowerCase());
        self.setAttribute(name, value);
        return true;
      },
      has(_t, prop) {
        const name = 'data-' + String(prop).replace(/[A-Z]/g, (m) => '-' + m.toLowerCase());
        return self.hasAttribute(name);
      },
    });
  }

  get textContent() {
    return this.childNodes.map((n) => n.textContent).join('');
  }

  set textContent(v) {
    this.childNodes = [];
    if (v !== '' && v !== null && v !== undefined) this.appendChild(new FakeText(v));
  }

  get innerHTML() {
    throw new Error('innerHTML must never be used by the UI builders');
  }

  set innerHTML(_v) {
    throw new Error('innerHTML must never be used by the UI builders');
  }

  addEventListener(type, fn) {
    (this.listeners[type] || (this.listeners[type] = [])).push(fn);
  }

  removeEventListener(type, fn) {
    const arr = this.listeners[type] || [];
    const idx = arr.indexOf(fn);
    if (idx !== -1) arr.splice(idx, 1);
  }

  /** Fire handlers registered for `type` (test convenience). */
  dispatch(type, event = { type, target: this }) {
    for (const fn of this.listeners[type] || []) fn(event);
  }

  /** Depth-first search by tag name (test convenience). */
  find(tag) {
    for (const child of this.childNodes) {
      if (child.nodeType !== 1) continue;
      if (String(child.tagName).toLowerCase() === String(tag).toLowerCase()) return child;
      const nested = child.find(tag);
      if (nested) return nested;
    }
    return null;
  }
}

export function createFakeDocument() {
  return {
    createElement(tag) { return new FakeElement(tag); },
    createElementNS(ns, tag) { return new FakeElement(tag, ns); },
    createTextNode(text) { return new FakeText(text); },
    // Как в браузере: узла с таким id в мини-DOM нет, поэтому null.
    // Тесты, которым нужен конкретный узел, переопределяют метод (dictation.test.mjs).
    getElementById() { return null; },
  };
}

export function installFakeDom() {
  globalThis.document = createFakeDocument();
  return globalThis.document;
}

export function uninstallFakeDom() {
  delete globalThis.document;
}
