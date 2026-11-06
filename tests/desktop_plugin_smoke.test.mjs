// Проверка плагина «Командные комнаты» без Electron: мини-React + мини-DOM.
//
// Что доказываем (то, что нельзя проверить `node --check`):
//  · регистрация даёт ровно три вклада — страница /team-room, строка меню
//    (order 55) и команда палитры, которая ведёт на /team-room;
//  · страница создаёт <webview> ИМПЕРАТИВНО, без partition (иначе нет прав на
//    микрофон), с src на 127.0.0.1:8777 и без рамок;
//  · did-fail-load главного фрейма показывает русскую плашку с кнопками,
//    ошибка подкадра — игнорируется, did-finish-load убирает плашку;
//  · если тег <webview> не поднялся — уходим на запасной <iframe>.
//
//   node --test tests/desktop_plugin_smoke.test.mjs

import assert from 'node:assert/strict'
import { test } from 'node:test'
import { mkdirSync, writeFileSync, readFileSync } from 'node:fs'
import { pathToFileURL } from 'node:url'
import path from 'node:path'
import os from 'node:os'

let WEBVIEW_OK = true

class El {
  constructor(tag) {
    this.tagName = tag
    this.attrs = {}
    this.children = []
    this.style = {}
    this.listeners = {}
    this.removed = false
  }
  setAttribute(k, v) { this.attrs[k] = String(v) }
  getAttribute(k) { return Object.prototype.hasOwnProperty.call(this.attrs, k) ? this.attrs[k] : null }
  addEventListener(t, f) { (this.listeners[t] ||= []).push(f) }
  removeEventListener(t, f) { this.listeners[t] = (this.listeners[t] || []).filter(g => g !== f) }
  appendChild(c) { this.children.push(c); c.parent = this; return c }
  replaceChildren() { this.children = [] }
  removeChild(c) { this.children = this.children.filter(x => x !== c) }
  remove() { this.removed = true; if (this.parent) this.parent.removeChild(this) }
  querySelectorAll(tag) {
    const out = []
    const walk = node => { for (const c of node.children || []) { if (c.tagName === tag) out.push(c); walk(c) } }
    walk(this)
    return out
  }
  text() {
    const parts = []
    const walk = node => {
      for (const c of node.children || []) {
        if (c && typeof c.text === 'string') parts.push(c.text)
        walk(c)
      }
    }
    walk(this)
    return parts.join(' ')
  }
}

globalThis.document = {
  documentElement: { classList: { contains: () => false }, getAttribute: () => null },
  createElement(tag) {
    const el = new El(tag)
    if (tag === 'webview' && WEBVIEW_OK) el.getWebContentsId = () => 42
    return el
  }
}
const windowListeners = {}
globalThis.window = {
  matchMedia: () => ({ matches: false }),
  open: () => true,
  addEventListener: (name, fn) => { (windowListeners[name] ||= []).push(fn) },
  removeEventListener: (name, fn) => { windowListeners[name] = (windowListeners[name] || []).filter(g => g !== fn) }
}
const realSetTimeout = globalThis.setTimeout.bind(globalThis)
const realClearTimeout = globalThis.clearTimeout.bind(globalThis)
const liveTimers = new Set()
globalThis.setTimeout = (fn, ms, ...rest) => {
  const id = realSetTimeout(fn, ms, ...rest)
  liveTimers.add(id)
  return id
}
globalThis.clearTimeout = id => { liveTimers.delete(id); return realClearTimeout(id) }
const realWarn = console.warn
console.warn = () => {}

const dir = path.join(os.tmpdir(), 'teamroom-plugin-smoke-' + process.pid)
mkdirSync(dir, { recursive: true })

writeFileSync(
  path.join(dir, 'react.mjs'),
  `
export const __hooks = { slots: [], idx: 0, pending: [] }
const render = () => { if (globalThis.__rerender) globalThis.__rerender() }
export function useState(init) {
  const hooks = __hooks
  const i = hooks.idx++
  if (!(i in hooks.slots)) hooks.slots[i] = typeof init === 'function' ? init() : init
  const set = v => {
    const next = typeof v === 'function' ? v(hooks.slots[i]) : v
    if (Object.is(next, hooks.slots[i])) return
    hooks.slots[i] = next
    if (!hooks.scheduled) { hooks.scheduled = true; queueMicrotask(() => { hooks.scheduled = false; render() }) }
  }
  return [hooks.slots[i], set]
}
export function useRef(init) {
  const i = __hooks.idx++
  if (!(i in __hooks.slots)) __hooks.slots[i] = { current: init }
  return __hooks.slots[i]
}
export function useEffect(fn, deps) {
  const i = __hooks.idx++
  const prev = __hooks.slots[i]
  const changed = !prev || !prev.deps || !deps || deps.some((d, j) => d !== prev.deps[j])
  if (changed) __hooks.pending.push({ fn, deps, i, prev })
  __hooks.slots[i] = { deps, cleanup: prev && prev.cleanup }
}
export function useCallback(fn) { return fn }
`
)

writeFileSync(
  path.join(dir, 'jsx.mjs'),
  'export const jsx = (type, props) => ({ type, props: props || {} })\nexport const jsxs = jsx\n'
)

writeFileSync(
  path.join(dir, 'sdk.mjs'),
  `
export const ROUTES_AREA = 'routes'
export const SIDEBAR_NAV_AREA = 'sidebar.nav'
export const PALETTE_AREA = 'palette'
export const host = {
  navigate: p => { globalThis.__navigated = p },
  os: { openExternal: u => { globalThis.__opened = u; return true } }
}
`
)

const pluginSource = path.join(
  path.dirname(new URL(import.meta.url).pathname.replace(/^\/([A-Za-z]:)/, '$1')),
  '..',
  'desktop-plugin',
  'plugin.js'
)
const source = readFileSync(pluginSource, 'utf8')
const rewritten = source
  .replace(/from '@hermes\/plugin-sdk'/g, `from ${JSON.stringify(pathToFileURL(path.join(dir, 'sdk.mjs')).href)}`)
  .replace(/from 'react\/jsx-runtime'/g, `from ${JSON.stringify(pathToFileURL(path.join(dir, 'jsx.mjs')).href)}`)
  .replace(/from 'react'/g, `from ${JSON.stringify(pathToFileURL(path.join(dir, 'react.mjs')).href)}`)
writeFileSync(path.join(dir, 'plugin.js'), rewritten)
// Node 26: .js в временной папке без package.json считается CommonJS — объявляем модуль явно.
writeFileSync(path.join(dir, 'package.json'), '{"type":"module"}\n')

const mod = await import(pathToFileURL(path.join(dir, 'plugin.js')).href)
const { __hooks: hooks } = await import(pathToFileURL(path.join(dir, 'react.mjs')).href)
const plugin = mod.default

// ── мини-рендерер с позиционным согласованием (как React, но на 40 строк) ───
function mount(vnode, prev) {
  if (vnode === null || vnode === undefined || vnode === false) return null
  const { type, props } = vnode
  if (typeof type === 'function') return mount(type(props), prev)

  const el = prev && prev.tagName === type ? prev : document.createElement(type)
  for (const [key, value] of Object.entries(props || {})) {
    if (key === 'children') continue
    if (key === 'style' && value && typeof value === 'object') { Object.assign(el.style, value); continue }
    if (key === 'ref') { if (typeof value === 'function') value(el); else if (value) value.current = el; continue }
    if (key.startsWith('on') && typeof value === 'function') { el.addEventListener(key.slice(2).toLowerCase(), value); continue }
    el[key] = value
  }

  const children = props && props.children
  if (children === undefined) return el // содержимое держит императивный код (webview)

  const list = (Array.isArray(children) ? children.flat(Infinity) : [children])
    .filter(child => child !== null && child !== undefined && child !== false && child !== true)
  const previous = el.children || []
  const next = []
  list.forEach((child, index) => {
    const prevChild = previous[index]
    if (typeof child === 'string' || typeof child === 'number') {
      next.push(prevChild && typeof prevChild.text === 'string' ? prevChild : { text: String(child) })
      return
    }
    const node = mount(child, prevChild && prevChild.tagName ? prevChild : undefined)
    if (node) next.push(node)
  })
  el.children = next
  return el
}

const registrations = []
plugin.register({ register: contribution => { registrations.push(contribution); return () => {} } })

const page = registrations.find(c => c.area === 'routes')

let tree = null
globalThis.__rerender = () => { flush() }
globalThis.__rerender = () => { flush() }

function renderTree(prev) {
  hooks.idx = 0
  hooks.pending = []
  const el = mount(page.render(), prev)
  for (const effect of hooks.pending) {
    if (effect.prev && typeof effect.prev.cleanup === 'function') effect.prev.cleanup()
    const cleanup = effect.fn()
    hooks.slots[effect.i] = { deps: effect.deps, cleanup }
  }
  return el
}

function flush() {
  tree = renderTree(tree)
}

flush()

const settle = () => new Promise(resolve => realSetTimeout(resolve, 15))

function fire(event, payload) {
  const view = tree.querySelectorAll('webview')[0]
  assert.ok(view, 'webview исчез')
  for (const handler of view.listeners[event] || []) handler(payload)
}

/** Событие у запасного <iframe> (когда тег <webview> недоступен). */
function fireFrame(event, payload) {
  const frame = tree.querySelectorAll('iframe')[0]
  assert.ok(frame, 'iframe исчез')
  for (const handler of frame.listeners[event] || []) handler(payload || {})
}

/** Сообщение из страницы в окно хозяина (путь <iframe>). */
function postMessage(data) {
  const event = { data, origin: 'http://127.0.0.1:8777', source: null }
  for (const handler of windowListeners.message || []) handler(event)
}

/** Кнопка с текстом (мини-рендерер держит текст отдельными узлами). */
function findButton(label) {
  const walk = node => {
    if (!node) return null
    for (const child of node.children || []) {
      if (child && typeof child.text === 'string' && child.text.trim() === label && node.listeners && node.listeners.click) {
        return node
      }
      const hit = walk(child)
      if (hit) return hit
    }
    return null
  }
  return walk(tree)
}

/** Свежее состояние страницы: как будто плагин только что открыли. */
function reset() {
  stopTimers()
  hooks.slots = []
  tree = null
  flush()
}

// Снять таймеры страницы (watchdog загрузки и автоповтор), иначе процесс висит.
function stopTimers() {
  for (const slot of hooks.slots) {
    if (slot && typeof slot.cleanup === 'function') {
      try { slot.cleanup() } catch { /* уже снят */ }
      slot.cleanup = null
    }
  }
}

test('регистрирует страницу, строку меню и команду палитры', () => {
  assert.equal(plugin.id, 'team-room')
  assert.equal(plugin.name, 'Командные комнаты')
  assert.equal(registrations.length, 3)

  assert.equal(page.data.path, '/team-room')

  const nav = registrations.find(c => c.area === 'sidebar.nav')
  assert.equal(nav.order, 55)
  assert.equal(nav.data.label, 'Командные комнаты')
  assert.equal(nav.data.codicon, 'organization')
  assert.equal(nav.data.path, '/team-room')

  const palette = registrations.find(c => c.area === 'palette')
  assert.equal(palette.data.id, 'team-room.open')
  palette.data.run()
  assert.equal(globalThis.__navigated, '/team-room')
})

test('<webview> создаётся императивно, без partition, во всю область', () => {
  const views = tree.querySelectorAll('webview')
  assert.equal(views.length, 1)
  const view = views[0]
  assert.match(view.attrs.src, /^http:\/\/127\.0\.0\.1:8777\/\?theme=(light|dark)$/)
  assert.equal(view.getAttribute('partition'), null, 'partition ломает права на микрофон')
  assert.match(view.style.cssText, /border:0/)
  assert.match(view.style.cssText, /height:100%/)
  assert.deepEqual(
    Object.keys(view.listeners).sort(),
    ['crashed', 'did-fail-load', 'did-finish-load', 'did-navigate',
      'did-navigate-in-page', 'dom-ready', 'render-process-gone']
  )
  assert.equal(tree.text().includes('запускаются'), false)
})

test('did-finish-load не показывает плашку', async () => {
  fire('did-finish-load', {})
  await settle()
  assert.equal(tree.text().includes('Командные комнаты ещё запускаются'), false)
})

test('первый запуск: сервер не ответил — плашка «ещё запускаются» с кнопками', async () => {
  reset()
  await settle()
  fire('did-fail-load', { errorCode: -102, errorDescription: 'ERR_CONNECTION_REFUSED', isMainFrame: true })
  await settle()
  const text = tree.text()
  assert.match(text, /Командные комнаты ещё запускаются/)
  assert.match(text, /до двух минут после входа в Windows/)
  assert.match(text, /Повторить/)
  assert.match(text, /Открыть в браузере/)
  assert.match(text, /ERR_CONNECTION_REFUSED/)
})

test('B-16: перезапуск сервера не сносит уже загруженную страницу', async () => {
  reset()
  await settle()
  fire('did-finish-load', {})
  await settle()
  const before = tree.querySelectorAll('webview')[0]
  assert.ok(before)

  fire('did-fail-load', { errorCode: -102, errorDescription: 'ERR_CONNECTION_REFUSED', isMainFrame: true })
  await settle()

  const after = tree.querySelectorAll('webview')[0]
  assert.equal(after, before, 'webview пересоздан — так нельзя: страница была жива')
  assert.equal(after.removed, false)
  const text = tree.text()
  assert.equal(/Командные комнаты ещё запускаются/.test(text), false, 'страницу не закрываем плашкой')
  assert.match(text, /Сервер перезапускается/)

  // Сервер вернулся: тихая плашка уходит, страницу не трогаем.
  fire('did-finish-load', {})
  await settle()
  assert.equal(tree.text().includes('Сервер перезапускается'), false)
})

test('ошибка подкадра игнорируется, ERR_ABORTED тоже', async () => {
  fire('did-finish-load', {})
  await settle()
  fire('did-fail-load', { errorCode: -105, isMainFrame: false })
  fire('did-fail-load', { errorCode: -3, isMainFrame: true })
  await settle()
  assert.equal(tree.text().includes('ещё запускаются'), false)
  assert.equal(tree.text().includes('Сервер перезапускается'), false)
})

test('B-16: маршрут запоминается и открывается заново при пересоздании webview', async () => {
  reset()
  await settle()
  fire('did-finish-load', {})
  await settle()
  fire('did-navigate-in-page', {
    url: 'http://127.0.0.1:8777/?theme=light#/room/fn/request/t_9f3c2a10',
    isMainFrame: true
  })
  await settle()

  // Окно гостя закрылось — единственный случай, когда страницу пересоздаём.
  fire('render-process-gone', {})
  await settle()
  const again = findButton('Повторить')
  assert.ok(again, 'нет кнопки «Повторить»')
  for (const handler of again.listeners.click || []) handler({ currentTarget: again })
  await settle()

  const views = tree.querySelectorAll('webview')
  assert.equal(views.length, 1)
  assert.match(views[0].attrs.src, /#\/room\/fn\/request\/t_9f3c2a10$/,
    'пересоздание открыло не тот маршрут: ' + views[0].attrs.src)
})

test('B-16: возврат «на все комнаты» забывает прежний маршрут', async () => {
  fire('did-navigate-in-page', { url: 'http://127.0.0.1:8777/?theme=light#/', isMainFrame: true })
  await settle()
  fire('render-process-gone', {})
  await settle()
  const again = findButton('Повторить')
  assert.ok(again)
  for (const handler of again.listeners.click || []) handler({ currentTarget: again })
  await settle()
  const views = tree.querySelectorAll('webview')
  assert.match(views[0].attrs.src, /\?theme=(light|dark)$/, 'дома хэш не нужен: ' + views[0].attrs.src)
})

test('без поддержки <webview> — запасной <iframe> с микрофоном', async () => {
  WEBVIEW_OK = false
  hooks.slots = []
  tree = null
  flush()
  await settle()
  await settle()
  const views = tree.querySelectorAll('webview')
  const frames = tree.querySelectorAll('iframe')
  assert.equal(views.length, 0, 'тег без getWebContentsId не считается рабочим')
  assert.equal(frames.length, 1, 'ожидался запасной <iframe>')
  assert.match(frames[0].attrs.allow, /microphone/)
  assert.match(frames[0].attrs.src, /127\.0\.0\.1:8777/)
})

test('B-16: <iframe> присылает маршрут сообщением — он же попадает в src', async () => {
  // Страница сообщает маршрут хозяину: {type:'team-room:route', hash}
  postMessage({ type: 'team-room:route', version: 1, hash: '#/room/cafe/backlog' })
  await settle()
  // Пересоздание запасного пути (сервер не ответил) — src должен нести тот же хэш.
  fireFrame('error', {})
  await settle()
  const again = findButton('Повторить')
  assert.ok(again, 'нет кнопки «Повторить»')
  for (const handler of again.listeners.click || []) handler({ currentTarget: again })
  await settle()
  const frames = tree.querySelectorAll('iframe')
  assert.equal(frames.length, 1)
  assert.match(frames[0].attrs.src, /#\/room\/cafe\/backlog$/, 'iframe открыт не на том маршруте: ' + frames[0].attrs.src)
  WEBVIEW_OK = true
})

test('завершение: снять таймеры страницы', () => {
  stopTimers()
  // таймеры страницы (watchdog и автоповтор) держали бы цикл событий
  for (const id of liveTimers) realClearTimeout(id)
  liveTimers.clear()
  assert.equal(liveTimers.size, 0)
})
