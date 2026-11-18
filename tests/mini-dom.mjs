/**
 * mini-dom.mjs — небольшой DOM для тестов интерфейса под `node --test`.
 *
 * Почему свой, а не tests/fake-dom.mjs: здесь нужны `insertBefore`, `isConnected`,
 * `querySelector` с простыми селекторами и `dispatch` — без них блоки страницы
 * запроса (свод, диалог, правая колонка) не собрать. innerHTML по-прежнему
 * запрещён: если сборщик вернётся к innerHTML, тест упадёт громко.
 */

export class MiniText {
  constructor(data) {
    this.nodeType = 3;
    this.data = String(data);
    this.parentNode = null;
  }

  get textContent() { return this.data; }
  set textContent(v) { this.data = String(v); }
}

export class MiniEl {
  constructor(tag) {
    this.nodeType = 1;
    this.localName = String(tag).toLowerCase();
    this.tagName = String(tag).toUpperCase();
    this.childNodes = [];
    this.parentNode = null;
    this.attributes = [];
    this.scrollTop = 0;
    this.scrollLeft = 0;
    this.scrollHeight = 0;
    this.clientHeight = 0;
    this.offsetHeight = 0;
    this.style = { setProperty() {} };
    this.listeners = Object.create(null);
    this.value = '';
    this.checked = false;
    this.disabled = false;
  }

  setAttribute(name, value) {
    const hit = this.attributes.find((a) => a.name === name);
    if (hit) hit.value = String(value);
    else this.attributes.push({ name, value: String(value) });
  }

  getAttribute(name) {
    const hit = this.attributes.find((a) => a.name === name);
    return hit ? hit.value : null;
  }

  hasAttribute(name) { return this.getAttribute(name) !== null; }
  removeAttribute(name) { this.attributes = this.attributes.filter((a) => a.name !== name); }

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
    const attrName = (prop) => 'data-' + String(prop).replace(/[A-Z]/g, (m) => '-' + m.toLowerCase());
    return new Proxy({}, {
      get(_t, prop) { const v = self.getAttribute(attrName(prop)); return v === null ? undefined : v; },
      set(_t, prop, value) { self.setAttribute(attrName(prop), value); return true; },
      has(_t, prop) { return self.hasAttribute(attrName(prop)); },
    });
  }

  get innerHTML() { throw new Error('innerHTML must never be used by the UI builders'); }
  set innerHTML(_v) { throw new Error('innerHTML must never be used by the UI builders'); }

  get isConnected() {
    let cur = this;
    while (cur) {
      if (cur === DOC.body) return true;
      cur = cur.parentNode;
    }
    return false;
  }

  get children() { return this.childNodes.filter((n) => n.nodeType === 1); }

  appendChild(node) {
    if (node.parentNode) node.parentNode.removeChild(node);
    node.parentNode = this;
    this.childNodes.push(node);
    return node;
  }

  insertBefore(node, ref) {
    if (node.parentNode) node.parentNode.removeChild(node);
    node.parentNode = this;
    if (!ref) { this.childNodes.push(node); return node; }
    const idx = this.childNodes.indexOf(ref);
    this.childNodes.splice(idx < 0 ? this.childNodes.length : idx, 0, node);
    return node;
  }

  removeChild(node) {
    const idx = this.childNodes.indexOf(node);
    if (idx >= 0) this.childNodes.splice(idx, 1);
    node.parentNode = null;
    return node;
  }

  replaceChildren(...nodes) {
    for (const child of [...this.childNodes]) this.removeChild(child);
    for (const node of nodes) if (node) this.appendChild(node);
  }

  append(...nodes) {
    for (const node of nodes) if (node && typeof node.nodeType === 'number') this.appendChild(node);
  }

  remove() { if (this.parentNode) this.parentNode.removeChild(this); }

  get textContent() { return this.childNodes.map((n) => n.textContent).join(''); }

  set textContent(v) {
    for (const child of [...this.childNodes]) this.removeChild(child);
    if (v !== '' && v !== null && v !== undefined) this.appendChild(new MiniText(v));
  }

  addEventListener(type, fn) { (this.listeners[type] || (this.listeners[type] = [])).push(fn); }

  removeEventListener(type, fn) {
    const arr = this.listeners[type] || [];
    const idx = arr.indexOf(fn);
    if (idx !== -1) arr.splice(idx, 1);
  }

  dispatch(type, event = {}) {
    for (const fn of [...(this.listeners[type] || [])]) fn({ type, target: this, ...event });
  }

  focus() { DOC.activeElement = this; }
  blur() { if (DOC.activeElement === this) DOC.activeElement = null; }
  scrollIntoView() { this.scrolledIntoView = true; }

  querySelector(sel) { return findAll(this, sel)[0] || null; }
  querySelectorAll(sel) { return findAll(this, sel); }
  matches(sel) { return matches(this, sel); }
}

function selectorParts(sel) {
  // Поддерживаем то, что нужно тестам: "tag.class", ".class", "#id", "[attr=value]".
  const out = { tag: '', cls: [], id: '', attr: [] };
  const re = /([a-zA-Z][\w-]*)|\.([\w-]+)|#([\w-]+)|\[([\w-]+)(?:="([^"]*)")?\]/g;
  let m;
  while ((m = re.exec(String(sel)))) {
    if (m[1]) out.tag = m[1].toLowerCase();
    else if (m[2]) out.cls.push(m[2]);
    else if (m[3]) out.id = m[3];
    else if (m[4]) out.attr.push([m[4], m[5]]);
  }
  return out;
}

function matches(el, sel) {
  if (!el || el.nodeType !== 1) return false;
  const p = selectorParts(sel);
  if (p.tag && el.localName !== p.tag) return false;
  if (p.id && el.id !== p.id) return false;
  for (const c of p.cls) if (!el.classList.contains(c)) return false;
  for (const [name, value] of p.attr) {
    const got = el.getAttribute(name);
    if (got === null) return false;
    if (value !== undefined && got !== value) return false;
  }
  return true;
}

export function findAll(root, sel) {
  const out = [];
  const stack = [...((root && root.childNodes) || [])].reverse();
  while (stack.length) {
    const node = stack.pop();
    if (!node || node.nodeType !== 1) continue;
    if (matches(node, sel)) out.push(node);
    for (let i = node.childNodes.length - 1; i >= 0; i--) stack.push(node.childNodes[i]);
  }
  return out;
}

export const DOC = {
  body: null,
  activeElement: null,
  createElement(tag) { return new MiniEl(tag); },
  createElementNS(_ns, tag) { return new MiniEl(tag); },
  createTextNode(text) { return new MiniText(text); },
  getElementById(id) { return findAll(this.body, '#' + id)[0] || null; },
  querySelector(sel) { return findAll(this.body, sel)[0] || null; },
  querySelectorAll(sel) { return findAll(this.body, sel); },
  addEventListener() {},
  removeEventListener() {},
};

DOC.body = new MiniEl('body');

/** Поставить мини-DOM в globalThis (до динамического import модулей вьюхи). */
export function installMiniDom() {
  globalThis.document = DOC;
  if (!Object.getOwnPropertyDescriptor(globalThis, 'requestAnimationFrame')) {
    globalThis.requestAnimationFrame = (fn) => setTimeout(() => fn(0), 0);
  }
  if (!globalThis.localStorage) {
    const store = new Map();
    globalThis.localStorage = {
      getItem: (k) => (store.has(k) ? store.get(k) : null),
      setItem: (k, v) => { store.set(k, String(v)); },
      removeItem: (k) => { store.delete(k); },
    };
  }
  return DOC;
}

/** Чистая страница перед каждым тестом. */
export function resetMiniDom() {
  for (const child of [...DOC.body.childNodes]) DOC.body.removeChild(child);
  DOC.activeElement = null;
}

/** Весь текст внутри узла (для проверок «что видит человек»). */
export function textOf(node) {
  return String((node && node.textContent) || '');
}

/** Все узлы с классом. */
export function byClass(root, cls) {
  return findAll(root, '.' + cls);
}

/** Все узлы с атрибутом data-*. */
export function byData(root, name, value) {
  const sel = value === undefined ? `[${name}]` : `[${name}="${value}"]`;
  return findAll(root, sel);
}

export const tick = () => new Promise((r) => setTimeout(r, 0));
