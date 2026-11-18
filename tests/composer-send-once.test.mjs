/**
 * composer-send-once.test.mjs — W20-C1 (B-40, B-39): диалог монтируется ОДИН раз,
 * и «Отправить» срабатывает после каждой перерисовки страницы.
 *
 * Что здесь воспроизведено (и почему это красный тест на 1.0.4):
 *  * страница запроса на каждый `render()` собирала блок диалога заново и
 *    «накладывала» новое дерево на живое точечно (`live-patch.js`). После первой
 *    отправки живая кнопка «Отправить» заменялась кнопкой НОВОЙ сборки, а поле с
 *    надиктованным текстом оставалось от старой — кнопка читала значение своего,
 *    не вставленного в страницу поля и отвечала «Напишите сообщение»;
 *  * на 1.0.4 у `mountThread` не было ни `update(detail)`, ни метки
 *    `data-keep-tree`, ни `data-keep` на поле, ни блокировки «пусто → неактивна»,
 *    поэтому этот файл падал уже на первой проверке контракта.
 *
 * DOM свой (мини): нужны insertBefore/removeChild/attributes, которых нет в
 * tests/fake-dom.mjs.
 */

import test from 'node:test';
import assert from 'node:assert/strict';

/* ───────────────────────────────────────────────────────────────── мини-DOM */

class Text {
  constructor(data) {
    this.nodeType = 3;
    this.data = String(data);
    this.parentNode = null;
  }

  get textContent() { return this.data; }
  set textContent(v) { this.data = String(v); }
}

class El {
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

  get isConnected() {
    let cur = this;
    while (cur) {
      if (cur === DOC.body) return true;
      cur = cur.parentNode;
    }
    return false;
  }

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

  remove() { if (this.parentNode) this.parentNode.removeChild(this); }

  get textContent() {
    return this.childNodes.map((n) => n.textContent).join('');
  }

  set textContent(v) {
    for (const child of [...this.childNodes]) this.removeChild(child);
    if (v !== '' && v !== null && v !== undefined) this.appendChild(new Text(v));
  }

  addEventListener(type, fn) {
    (this.listeners[type] || (this.listeners[type] = [])).push(fn);
  }

  removeEventListener(type, fn) {
    const arr = this.listeners[type] || [];
    const idx = arr.indexOf(fn);
    if (idx !== -1) arr.splice(idx, 1);
  }

  dispatch(type, event = {}) {
    for (const fn of [...(this.listeners[type] || [])]) fn({ type, target: this, ...event });
  }

  focus() { DOC.activeElement = this; }

  querySelector(sel) { return findAll(this, sel)[0] || null; }

  querySelectorAll(sel) { return findAll(this, sel); }

  matches(sel) { return matches(this, sel); }
}

function selectorParts(sel) {
  // Поддерживаем только то, что нужно тесту: "tag.class", ".class", "#id", "[attr]".
  const out = { tag: '', cls: [], id: '', attr: [] };
  const re = /([a-zA-Z][\w-]*)|\.([\w-]+)|#([\w-]+)|\[([\w-]+)(?:="([^"]*)")?\]/g;
  let m;
  while ((m = re.exec(sel))) {
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

function findAll(root, sel) {
  const out = [];
  const stack = [...(root.childNodes || [])].reverse();
  while (stack.length) {
    const node = stack.pop();
    if (!node || node.nodeType !== 1) continue;
    if (matches(node, sel)) out.push(node);
    for (let i = node.childNodes.length - 1; i >= 0; i--) stack.push(node.childNodes[i]);
  }
  return out;
}

const DOC = {
  body: null,
  activeElement: null,
  createElement(tag) { return new El(tag); },
  createElementNS(_ns, tag) { return new El(tag); },
  createTextNode(text) { return new Text(text); },
  getElementById() { return null; },
  querySelector(sel) { return findAll(this.body, sel)[0] || null; },
  querySelectorAll(sel) { return findAll(this.body, sel); },
  addEventListener() {},
  removeEventListener() {},
};

DOC.body = new El('body');
globalThis.document = DOC;
if (!Object.getOwnPropertyDescriptor(globalThis, 'requestAnimationFrame')) {
  globalThis.requestAnimationFrame = (fn) => setTimeout(() => fn(0), 0);
}

const { h } = await import('../web/js/util.js');
const { applyLivePatch, captureSnapshot } = await import('../web/js/live-patch.js');
const { mountThread: mountThreadRaw } = await import('../web/js/views/request-thread.js');

/**
 * У-1 (волна 21): диалог по умолчанию свёрнут — тесты открывают его так же,
 * как человек: заголовком общего компонента (иначе ленты и поля нет в DOM).
 */
function mountThread(...args) {
  const ctl = mountThreadRaw(...args);
  if (ctl && typeof ctl.expand === 'function') ctl.expand();
  return ctl;
}

/* ───────────────────────────────────────────────────────────── помощники */

const tick = () => new Promise((r) => setTimeout(r, 0));
const nowS = () => Math.floor(Date.now() / 1000);

/** Каждый тест начинается с чистой страницы: иначе чужое поле в документе
 *  становится «видимым полем» для отсоединённого блока (и это правильно — см.
 *  проверку про ВИДИМОЕ поле, но мешает остальным тестам). */
function resetBody() {
  for (const child of [...DOC.body.childNodes]) DOC.body.removeChild(child);
  DOC.activeElement = null;
}

function textareaOf(root) { return root.querySelector('textarea.thr__input'); }
function sendButtonOf(root) { return root.querySelector('.thr__send'); }
function allText(root) { return String(root.textContent || ''); }

function makeCtx(post, extra = {}) {
  const calls = [];
  const toasts = [];
  const ctx = {
    room: { id: 'cafe', orchestrator: 'team-director' },
    toast: (msg, kind) => toasts.push({ msg, kind }),
    api: {
      post: async (path, body) => {
        calls.push({ path, body });
        return post ? post(path, body) : { message: { id: 'c' + calls.length, text: body.text }, thread: [] };
      },
    },
    ...extra,
  };
  return { ctx, calls, toasts };
}

/**
 * Страница запроса «как request.js»: новый блок диалога на каждой сборке НЕ
 * создаётся — накладываем дерево точечно и обновляем живой блок через update().
 */
function makePage(ctx, rid, firstDetail) {
  const root = new El('div');
  root.setAttribute('class', 'req');
  DOC.body.appendChild(root);
  let ctl = null;

  function page(detail, extraBlocks = 1) {
    const out = h('div', { class: 'req' });
    const main = h('div', { class: 'req__main' });
    out.appendChild(main);
    for (let i = 0; i < extraBlocks; i++) {
      main.appendChild(h('div', { class: 'req-answer', 'data-key': 'answer' + i }, 'Свод версии ' + i));
    }
    main.appendChild(h('div', { class: 'req-thread', 'data-keep-tree': '' }));
    return out;
  }

  function ensureThread() {
    const slot = root.querySelector('.req-thread');
    if (!slot) return null;
    if (!ctl) {
      ctl = mountThread(slot, ctx, { roomId: 'cafe', rid, detail: firstDetail });
      return ctl;
    }
    if (ctl.element.parentNode !== slot) slot.appendChild(ctl.element);
    ctl.update(firstDetail);
    return ctl;
  }

  function render(detail, extraBlocks = 1) {
    firstDetail = detail;
    const snap = captureSnapshot(root, DOC);
    applyLivePatch(root, page(detail, extraBlocks), { doc: DOC, snapshot: snap, draftKeys: new Map() });
    return ensureThread();
  }

  return { root, render, ctl: () => ctl };
}

const THREAD = [
  { id: 'c1', at: nowS() - 600, author_kind: 'mikhail', author: 'mikhail', author_label: 'Михаил',
    text: 'Уточните сроки', kind: 'message', task_id: null },
  { id: 'c2', at: nowS() - 300, author_kind: 'agent', author: 'team-director',
    author_label: 'Глеб · руководитель комнаты', text: 'Принял, уточню', kind: 'comment', task_id: null },
];

/* ───────────────────────────────────────────────────────────────── тесты */

test('контракт §1.3: mountThread возвращает update (страница монтирует диалог один раз)', () => {
  resetBody();
  const { ctx } = makeCtx();
  const host = new El('div');
  const ctl = mountThread(host, ctx, { roomId: 'cafe', rid: 't_root', detail: { id: 't_root', status: 'working', thread: THREAD } });
  assert.equal(typeof ctl.update, 'function', 'нужен update(detail)');
  assert.equal(typeof ctl.focus, 'function');
  assert.equal(typeof ctl.destroy, 'function');
  assert.ok(ctl.element, 'узел блока');
});

test('поле и блок помечены «не заменять»: data-keep и data-keep-tree', () => {
  resetBody();
  const { ctx } = makeCtx();
  const host = new El('div');
  const ctl = mountThread(host, ctx, { roomId: 'cafe', rid: 't_root', detail: { id: 't_root', status: 'working', thread: [] } });
  assert.equal(textareaOf(ctl.element).getAttribute('data-keep'), 'thread-draft');
  assert.notEqual(ctl.element.getAttribute('data-keep-tree'), null,
    'блок диалога страница не пересобирает');
});

test('«Отправить» неактивна на пустом поле и оживает от текста', async () => {
  resetBody();
  const { ctx } = makeCtx();
  const host = new El('div');
  const ctl = mountThread(host, ctx, { roomId: 'cafe', rid: 't_root', detail: { id: 't_root', status: 'working', thread: [] } });
  const ta = textareaOf(ctl.element);
  const send = sendButtonOf(ctl.element);

  assert.equal(send.disabled, true, 'пустое поле — нажимать нечего');

  ta.value = 'Уточнение';
  ta.dispatch('input');
  assert.equal(send.disabled, false, 'есть текст — кнопка доступна');

  ta.value = '   ';
  ta.dispatch('input');
  assert.equal(send.disabled, true, 'одни пробелы — по-прежнему нечего отправлять');
});

test('N отправок подряд с перерисовками между ними: 0 потерь (B-40)', async () => {
  resetBody();
  const posts = [];
  const { ctx, toasts } = makeCtx((path, body) => {
    posts.push(body);
    return {
      message: { id: 'c' + (100 + posts.length), at: nowS(), author: 'mikhail', author_kind: 'mikhail',
        author_label: 'Михаил', text: body.text, kind: 'message' },
      thread: THREAD.concat([{ id: 'c' + (100 + posts.length), at: nowS(), author: 'mikhail',
        author_kind: 'mikhail', author_label: 'Михаил', text: body.text, kind: 'message' }]),
    };
  });
  const page = makePage(ctx, 't_root', { id: 't_root', status: 'working', thread: THREAD });
  const ctl = page.render({ id: 't_root', status: 'working', thread: THREAD });
  const ta0 = textareaOf(ctl.element);
  const send0 = sendButtonOf(ctl.element);

  const N = 6;
  for (let i = 1; i <= N; i += 1) {
    // 1) страница перерисовалась (опрос) — данные новые, блок тот же
    page.render({
      id: 't_root', status: i === N ? 'done' : 'working',
      thread: THREAD.concat([{ id: 'c2' + i, at: nowS(), author_kind: 'agent', author: 'team-director',
        author_label: 'Глеб · руководитель комнаты', text: 'Обновление ' + i, kind: 'comment', task_id: null }]),
    }, i);   // i блоков «свода» — дерево каждый раз разное

    const ta = textareaOf(ctl.element);
    const send = sendButtonOf(ctl.element);
    assert.equal(ta, ta0, `поле осталось тем же узлом (шаг ${i})`);
    assert.equal(send, send0, `кнопка осталась тем же узлом (шаг ${i})`);
    assert.equal(ta.isConnected, true, 'поле в странице');

    // 2) надиктовали и отправили
    ta.value = 'Сообщение ' + i + ': срок — до 30 ноября, MOQ 500 шт.';
    ta.dispatch('input');
    assert.equal(send.disabled, false, `кнопка активна (шаг ${i})`);
    send.dispatch('click');
    await tick();
    await tick();

    assert.equal(posts.length, i, `шаг ${i}: ровно одна отправка`);
    assert.match(posts[i - 1].text, new RegExp('Сообщение ' + i), 'ушёл именно набранный текст');
    assert.equal(ta.value, '', `шаг ${i}: поле очищено`);
    assert.equal(allText(ctl.element).includes('Напишите сообщение'), false,
      'тоста «Напишите сообщение» при непустом поле быть не может');
  }

  assert.equal(posts.length, N, 'ни одна отправка не потерялась');
  assert.equal(toasts.filter((t) => /Напишите сообщение/.test(t.msg)).length, 0, 'ни одного ложного «Напишите сообщение»');
});

test('обработчик читает ВИДИМОЕ поле страницы, а не своё замыкание (B-40)', async () => {
  resetBody();
  const { ctx, calls } = makeCtx();
  const host = new El('div');                 // ЭТОТ блок в страницу не вставлен
  const ctl = mountThread(host, ctx, { roomId: 'cafe', rid: 't_root', detail: { id: 't_root', status: 'working', thread: [] } });

  // На экране — другое поле того же класса (как «поле от старой сборки»).
  const visible = h('textarea', { class: 'textarea thr__input' });
  visible.value = 'Текст из видимого поля';
  DOC.body.appendChild(visible);
  const prevQuery = DOC.querySelector;
  DOC.querySelector = (sel) => (String(sel).includes('thr__input') ? visible : prevQuery.call(DOC, sel));

  try {
    sendButtonOf(ctl.element).dispatch('click');
    await tick();
    await tick();
  } finally {
    DOC.querySelector = prevQuery;
    DOC.body.removeChild(visible);
  }

  assert.equal(calls.length, 1, 'отправка состоялась, а не «Напишите сообщение»');
  assert.equal(calls[0].body.text, 'Текст из видимого поля');
});

test('двойное нажатие не отправляет дважды; ключ client_id переживает ошибку (B-39)', async () => {
  resetBody();
  let release = null;
  const { ctx, calls } = makeCtx(() => new Promise((resolve) => { release = resolve; }));
  const host = new El('div');
  const ctl = mountThread(host, ctx, { roomId: 'cafe', rid: 't_root', detail: { id: 't_root', status: 'working', thread: [] } });
  const ta = textareaOf(ctl.element);
  const send = sendButtonOf(ctl.element);

  ta.value = 'Одно нажатие';
  ta.dispatch('input');
  send.dispatch('click');
  await tick();

  assert.equal(send.disabled, true, 'во время отправки кнопка заблокирована');
  assert.match(allText(ctl.element), /Отправляется…/, 'подпись «Отправляется…»');
  send.dispatch('click');            // второе нажатие — пока идёт первая отправка
  await tick();
  assert.equal(calls.length, 1, 'второго запроса нет');

  release({ message: { id: 'c9', text: 'Одно нажатие' }, thread: [] });
  await tick();
  await tick();
  assert.equal(calls[0].body.client_id && calls[0].body.client_id.length >= 6, true, 'ключ нажатия ушёл на сервер');
});

test('повтор после ошибки связи идёт с прежним client_id (сервер не создаст дубль)', async () => {
  resetBody();
  let fail = true;
  const { ctx, calls } = makeCtx((path, body) => {
    if (fail) { fail = false; return Promise.reject({ code: 'network' }); }
    return { message: { id: 'c7', text: body.text }, thread: [] };
  });
  const host = new El('div');
  const ctl = mountThread(host, ctx, { roomId: 'cafe', rid: 't_root', detail: { id: 't_root', status: 'working', thread: [] } });
  const ta = textareaOf(ctl.element);
  const send = sendButtonOf(ctl.element);

  ta.value = 'Важное уточнение про сроки';
  ta.dispatch('input');
  send.dispatch('click');
  await tick();
  await tick();

  assert.equal(calls.length, 1);
  assert.equal(ta.value, 'Важное уточнение про сроки', 'текст остался в поле');

  send.dispatch('click');
  await tick();
  await tick();
  assert.equal(calls.length, 2, 'повтор ушёл');
  assert.equal(calls[1].body.client_id, calls[0].body.client_id,
    'тот же ключ нажатия: если первая попытка дошла, сервер ответит duplicate');
});

test('duplicate:true от сервера — второе сообщение не появляется, тост понятный', async () => {
  resetBody();
  const { ctx, toasts } = makeCtx((path, body) => ({
    duplicate: true,
    message: { id: 'c5', at: nowS(), author: 'mikhail', author_kind: 'mikhail', author_label: 'Михаил',
      text: body.text, kind: 'message' },
    thread: THREAD,
  }));
  const host = new El('div');
  const ctl = mountThread(host, ctx, { roomId: 'cafe', rid: 't_root', detail: { id: 't_root', status: 'working', thread: THREAD } });
  const ta = textareaOf(ctl.element);
  ta.value = 'Уже отправленное';
  ta.dispatch('input');
  sendButtonOf(ctl.element).dispatch('click');
  await tick();
  await tick();

  const mine = findAll(ctl.element, '.thr-msg--mine');
  const texts = mine.map((el) => String(el.textContent));
  assert.equal(texts.filter((t) => t.includes('Уже отправленное')).length, 1, 'сообщение в ленте одно');
  assert.ok(toasts.some((t) => /уже отправлено/.test(t.msg)), 'человеку сказали, что второго не будет');
});

test('update(detail): лента и статус обновляются без пересборки блока', async () => {
  resetBody();
  const { ctx } = makeCtx();
  const host = new El('div');
  const ctl = mountThread(host, ctx, { roomId: 'cafe', rid: 't_root', detail: { id: 't_root', status: 'working', thread: THREAD } });
  const ta = textareaOf(ctl.element);
  const send = sendButtonOf(ctl.element);
  ta.value = 'Черновик';
  ta.dispatch('input');

  ctl.update({
    id: 't_root', status: 'done',
    thread: THREAD.concat([{ id: 'v1', at: nowS(), author_kind: 'system', author: 'system',
      author_label: 'Комната команды', text: 'Версия 2 готова', kind: 'event', task_id: null }]),
  });
  await tick();

  assert.equal(textareaOf(ctl.element), ta, 'поле то же');
  assert.equal(sendButtonOf(ctl.element), send, 'кнопка та же');
  assert.equal(ta.value, 'Черновик', 'черновик не потерян');
  assert.ok(allText(ctl.element).includes('Версия 2 готова'), 'новое событие в ленте');
  assert.ok(findAll(ctl.element, '.thr__revise').length === 1,
    'у готового запроса появился переключатель «и доработать свод»');
  assert.equal(ctl.element.getAttribute('data-keep-tree') !== null, true);
});

test('live-patch не заменяет поддерево data-keep-tree', () => {
  resetBody();
  const root = new El('div');
  root.setAttribute('class', 'req');
  const live = h('div', { class: 'req-thread', 'data-keep-tree': '' });
  const field = h('textarea', { class: 'thr__input' });
  field.value = 'набрано';
  live.appendChild(field);
  root.appendChild(live);
  DOC.body.appendChild(root);

  const next = h('div', { class: 'req' });
  const placeholder = h('div', { class: 'req-thread', 'data-keep-tree': '' });
  next.appendChild(placeholder);
  applyLivePatch(root, next, { doc: DOC, draftKeys: new Map() });

  assert.equal(root.querySelector('.req-thread'), live, 'живой блок остался на месте');
  assert.equal(root.querySelector('textarea.thr__input'), field, 'поле с текстом не заменено');
  assert.equal(field.value, 'набрано');
});
