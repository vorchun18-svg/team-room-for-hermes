/**
 * responsive.test.mjs — один договор об адаптиве всего каркаса.
 *
 * Проверяем не «красиво ли», а вещи, которые ломались в реальном окне 780 px:
 *   1) шапка — три зоны, у всех min-width: 0, правая группа не сжимается;
 *   2) крошки режутся многоточием и несут title; бейдж «вся комната» — в мете;
 *   3) поиск не схлопывается в ноль, а на узком превращается в иконку;
 *   4) сайдбар: 1100+/900–1099/до 899 — постоянный/узкий/выдвижной; у выдвижного
 *      есть гамбургер, кнопка закрытия, затемнение, Esc и закрытие при диалоге;
 *   5) единая шкала z-index в tokens.css: сайдбар НИЖЕ подложки диалога;
 *   6) диалог на телефоне — на весь экран, кнопки действий внизу;
 *   7) подсказка «Задайте папку проекта…» живёт в теле страницы, не в шапке.
 *
 * CSS и HTML читаем как текст (браузера в node нет); поведение (кнопка закрытия
 * меню, closeNav, крошки) — на fake-dom.
 */

import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import path from 'node:path';

import { FakeElement, installFakeDom, uninstallFakeDom } from './fake-dom.mjs';
import { crumbItems, WHOLE_ROOM_LABEL } from '../web/js/scope.js';
import { buildSidebar } from '../web/js/shell-sidebar.js';
import { closeNav } from '../web/js/shell-features.js';

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const read = (rel) => readFileSync(path.join(ROOT, rel), 'utf8');

/* Мини-DOM не умеет replaceChildren — buildSidebar собирает сайдбар через него. */
if (typeof FakeElement.prototype.replaceChildren !== 'function') {
  FakeElement.prototype.replaceChildren = function replaceChildren(...nodes) {
    this.childNodes = [];
    for (const node of nodes) if (node) this.appendChild(node);
  };
}

const TOKENS = read('web/css/tokens.css');
const COMPONENTS = read('web/css/components.css');
const BASE = read('web/css/base.css');
const INDEX = read('web/index.html');
const APP = read('web/js/app.js');
const FEATURES = read('web/js/shell-features.js');
const SIDEBAR = read('web/js/shell-sidebar.js');

/** Значение токена из :root. */
function token(name) {
  const m = new RegExp('--' + name + '\\s*:\\s*([^;]+);').exec(TOKENS);
  return m ? m[1].trim() : null;
}

/**
 * Все блоки @media (max-width: Npx) подряд: один и тот же breakpoint может
 * встречаться в файле несколько раз (точечные правки рядом с компонентом).
 */
function mediaBlock(css, maxWidth) {
  const re = new RegExp('@media\\s*\\(max-width:\\s*' + maxWidth + 'px\\)\\s*\\{', 'g');
  let out = '';
  let m;
  while ((m = re.exec(css)) !== null) {
    let depth = 0;
    let i = css.indexOf('{', m.index);
    const from = i;
    for (; i < css.length; i += 1) {
      if (css[i] === '{') depth += 1;
      else if (css[i] === '}') {
        depth -= 1;
        if (depth === 0) { out += css.slice(from, i + 1) + '\n'; break; }
      }
    }
    re.lastIndex = i;
  }
  return out;
}

/* ─────────────────────────────────────────────────────────── 1. z-index */

test('z-index: единая шкала, сайдбар ниже подложки диалога и выше затемнения', () => {
  const need = ['z-sticky', 'z-dropdown', 'z-nav-scrim', 'z-sidebar', 'z-backdrop', 'z-modal', 'z-popover', 'z-toast', 'z-palette'];
  const values = {};
  for (const name of need) {
    const raw = token(name);
    assert.ok(raw, `в tokens.css нет --${name}`);
    values[name] = Number(raw);
    assert.ok(Number.isFinite(values[name]), `--${name} не число: ${raw}`);
  }
  const order = ['z-nav-scrim', 'z-sidebar', 'z-backdrop', 'z-modal', 'z-popover', 'z-toast', 'z-palette'];
  for (let i = 1; i < order.length; i += 1) {
    assert.ok(values[order[i - 1]] < values[order[i]],
      `${order[i - 1]} (${values[order[i - 1]]}) должен быть ниже ${order[i]} (${values[order[i]]})`);
  }
  assert.ok(values['z-sticky'] < values['z-dropdown'], 'sticky ниже выпадашек');
});

test('z-index: выдвижной сайдбар не использует слой диалога', () => {
  const drawer = mediaBlock(COMPONENTS, 899);
  assert.match(drawer, /\.sidebar\s*\{[^}]*z-index:\s*var\(--z-sidebar\)/, 'сайдбар должен быть на --z-sidebar');
  assert.doesNotMatch(drawer, /\.sidebar\s*\{[^}]*z-index:\s*var\(--z-modal\)/, 'сайдбар не должен быть на --z-modal');
  assert.match(drawer, /\.sidebar__scrim\s*\{[^}]*z-index:\s*var\(--z-nav-scrim\)/, 'затемнение — на --z-nav-scrim');
  assert.match(COMPONENTS, /\.dialog-backdrop\s*\{[^}]*z-index:\s*var\(--z-backdrop\)/);
});

/* ────────────────────────────────────────────────────────── 2. шапка */

test('шапка: три зоны, min-width: 0 у сжимаемых, действия не сжимаются', () => {
  const need = ['topbar__row', 'topbar__lead', 'topbar__meta', 'topbar__actions'];
  for (const cls of need) assert.ok(COMPONENTS.includes('.' + cls), `нет правила .${cls}`);
  assert.match(COMPONENTS, /\.topbar__lead\s*\{[^}]*min-width:\s*0/);
  assert.match(COMPONENTS, /\.topbar__meta\s*\{[^}]*min-width:\s*0/);
  assert.match(COMPONENTS, /\.topbar__actions\s*\{[^}]*flex:\s*none/);
  assert.doesNotMatch(COMPONENTS, /\.topbar__row\s*\{[^}]*(?<!min-)height:\s*var\(--topbar-height\)/,
    'жёсткая высота строки не даёт мете уйти на вторую строку');
  assert.match(COMPONENTS, /\.topbar__row\s*\{[^}]*min-height:\s*var\(--topbar-height\)/);
});

test('шапка: крошки режутся многоточием и несут title, поиск не ноль', () => {
  assert.match(COMPONENTS, /\.crumbs__link,\s*\.crumbs__here\s*\{[^}]*text-overflow:\s*ellipsis/);
  assert.match(COMPONENTS, /\.crumbs__link,\s*\.crumbs__here\s*\{[^}]*min-width:\s*0/);
  // W17-G §1: подсказка крошки — полный текст запроса (`item.hint`), иначе сам текст.
  assert.match(APP, /const hint = item\.hint \|\| item\.text;/);
  assert.match(APP, /class: 'crumbs__link'[^)]*title: hint/);
  assert.match(APP, /class: 'crumbs__here'[\s\S]{0,120}title: hint/);
  assert.match(COMPONENTS, /\.topbar__search\s*\{[^}]*min-width:\s*0/);
});

test('шапка: точки перелома 1099 / 899 / 699 / 639 / 559 существуют', () => {
  for (const w of [1099, 899, 699, 639, 559]) {
    assert.match(COMPONENTS, new RegExp('@media\\s*\\(max-width:\\s*' + w + 'px\\)'), `нет @media ${w}px`);
  }
  const drawer = mediaBlock(COMPONENTS, 899);
  assert.match(drawer, /\.topbar__meta\s*\{[^}]*order:\s*9/, 'мета уходит во вторую строку');
  assert.match(drawer, /\.topbar__meta\s*\{[^}]*flex:\s*1 1 100%/, 'мета занимает всю ширину второй строки');
  const narrow = mediaBlock(COMPONENTS, 639);
  assert.match(narrow, /\.topbar__search\s*\{[^}]*width:\s*32px/, 'поиск схлопывается в кнопку 32 px');
  assert.match(narrow, /\.topbar__search-label\s*\{\s*display:\s*none/, 'подпись поиска скрыта');
});

test('шапка: бейдж «вся комната» и этап — в мете, а не в крошках', () => {
  assert.doesNotMatch(APP, /item\.badge\)\s*\{\s*nav\.appendChild/, 'бейдж не должен жить внутри .crumbs');
  assert.match(APP, /items\.find\(\(item\) => item\.badge\)/);
  assert.match(APP, /class: 'chip chip--muted crumbs__badge'/);
  assert.match(APP, /h\('div', \{ class: 'topbar__meta' \}, \.\.\.meta\)/);
  assert.equal(crumbItems({ route: { name: 'room', roomId: 'cafe', tab: 'backlog' }, roomTitle: 'Лето' })
    .find((i) => i.badge).badge, WHOLE_ROOM_LABEL);
});

/* ─────────────────────────────────────────────── 3. сайдбар и меню */

test('сайдбар: до 1100 узкий, до 900 — выдвижной с гамбургером и закрытием', () => {
  const narrow = mediaBlock(COMPONENTS, 1099);
  assert.match(narrow, /\.app\s*\{\s*grid-template-columns:\s*var\(--sidebar-width-narrow\)/, 'узкая колонка 900–1099');
  assert.ok(token('sidebar-width-narrow'), 'нет токена --sidebar-width-narrow');
  const drawer = mediaBlock(COMPONENTS, 899);
  assert.match(drawer, /\.app\s*\{\s*grid-template-columns:\s*minmax\(0, 1fr\)/);
  assert.match(drawer, /\.nav-toggle\s*\{\s*display:\s*inline-flex/);
  assert.match(drawer, /\.sidebar__close\s*\{\s*display:\s*inline-flex/);
  assert.match(drawer, /\.app\.nav-open \.sidebar\s*\{\s*transform:\s*none/);
  assert.match(COMPONENTS, /\.nav-toggle\s*\{\s*display:\s*none/, 'на широком гамбургера нет');
});

test('меню закрывается: Esc, затемнение, пункт, открытие диалога', () => {
  assert.match(FEATURES, /export function closeNav\(\)/);
  assert.match(FEATURES, /if \(app && app\.classList\.contains\('nav-open'\)\) app\.classList\.remove\('nav-open'\)/);
  const openRoom = FEATURES.slice(FEATURES.indexOf('export async function openRoomDialog'));
  assert.match(openRoom.slice(0, 400), /closeNav\(\)/, 'диалог комнаты закрывает меню');
  assert.match(FEATURES.slice(FEATURES.indexOf('export async function openPalette')).slice(0, 400), /closeNav\(\)/);
  assert.match(APP, /installHotkeys\(\{ closeNav: \(\) => setNavOpen\(false\) \}\)/);
  assert.match(APP, /class: 'sidebar__scrim'[^)]*onclick: \(\) => setNavOpen\(false\)/);
  assert.match(APP, /closest\('a\[href\^="#"\]'\)[\s\S]{0,80}setNavOpen\(false\)/);
});

test('меню: одна точка переключения (setNavOpen) и aria у гамбургера', () => {
  const writes = APP.match(/classList\.(?:add|remove|toggle)\('nav-open'/g) || [];
  assert.equal(writes.length, 1, `класс nav-open должен менять только setNavOpen, найдено ${writes.length}`);
  assert.match(APP, /'aria-expanded': navIsOpen\(\) \? 'true' : 'false'/);
  assert.match(APP, /'aria-controls': 'sidebar'/);
  assert.match(APP, /innerWidth >= 900 && navIsOpen\(\)\) setNavOpen\(false\)/, 'при расширении окна меню закрывается');
});

test('сайдбар: кнопка «Закрыть меню» вызывает onCloseNav', () => {
  const document_ = installFakeDom();
  try {
    // Мини-DOM не знает про body/querySelector: подсказываем ровно то, что нужно
    // buildSidebar (#demo-badge-slot, #room-list и т.п. — «нет узла»).
    document_.body = document_.createElement('body');
    document_.head = document_.createElement('head');
    document_.querySelector = () => null;
    const host = document_.createElement('aside');
    document_.body.appendChild(host);
    let closed = 0;
    buildSidebar(host, {
      onNewRoom() {}, onToggleNotifications() {}, onCycleTheme() {},
      onCloseNav() { closed += 1; },
    });
    const btn = host.find('button') && host.childNodes
      .flatMap((n) => (n.childNodes || []))
      .find((n) => n.nodeType === 1 && String(n.className || '').includes('sidebar__close'));
    assert.ok(btn, 'нет кнопки закрытия меню');
    assert.equal(btn.getAttribute('aria-label'), 'Закрыть меню комнат');
    btn.dispatch('click');
    assert.equal(closed, 1);
  } finally {
    uninstallFakeDom();
  }
});

test('closeNav снимает класс с #app и терпит пустой документ', () => {
  const document_ = installFakeDom();
  try {
    const app = document_.createElement('div');
    app.id = 'app';
    app.classList.add('nav-open');
    document_.body = document_.createElement('body');
    document_.body.appendChild(app);
    document_.getElementById = (id) => (id === 'app' ? app : null);
    closeNav();
    assert.equal(app.classList.contains('nav-open'), false);
    closeNav(); // повторно — без ошибок
  } finally {
    uninstallFakeDom();
  }
});

/* ─────────────────────────────────────────────── 4. диалоги и подсказка */

test('диалог: всегда выше сайдбара и на телефоне — на весь экран', () => {
  const phone = mediaBlock(COMPONENTS, 559);
  assert.match(phone, /body > \.dialog-backdrop\s*\{\s*padding:\s*0; place-items:\s*stretch/);
  assert.match(phone, /\.dialog-backdrop > \.dialog\s*\{[^}]*height:\s*100dvh/);
  assert.match(phone, /\.dialog__body\s*\{[^}]*overflow-y:\s*auto/);
  assert.match(phone, /\.dialog__foot\s*\{[^}]*position:\s*sticky/);
  assert.match(phone, /\.dialog__foot \.btn\s*\{[^}]*min-height:\s*36px/);
  assert.match(COMPONENTS, /\.dialog\s*\{[^}]*overflow:\s*hidden/, 'иначе липкий подвал не работает');
  assert.match(COMPONENTS, /\.dialog__body\s*\{[^}]*min-height:\s*0/);
});

test('подсказка папки проекта — в теле страницы, а не в шапке', () => {
  const top = INDEX.indexOf('<header class="topbar"');
  const hint = INDEX.indexOf('<div class="page-hint" id="page-hint"');
  const view = INDEX.indexOf('<main class="view"');
  assert.ok(top >= 0 && hint > top && view > hint, 'порядок: шапка → подсказка → вьюха');
  assert.match(INDEX, /id="page-hint"[^>]*hidden/);
  assert.match(APP, /function renderPageHint\(node\)/);
  assert.match(APP, /hintNode = projectDirHint\(roomCtx, \{ roomId: room\.id \}\)/,
    'подсказка уходит в тело страницы (W10)');
  assert.match(APP, /right\.push\(folderActionButton\(roomCtx, room\)\)/,
    'кнопка папки («Открыть» / «Задать») остаётся в шапке');
  assert.match(COMPONENTS, /\.page-hint\s*\{[^}]*overflow-wrap:\s*anywhere/);
});

/* ────────────────────────────────────────────── 5. текст и цели клика */

test('текст: длинные русские слова переносятся, страница не скроллится вбок', () => {
  assert.match(COMPONENTS, /overflow-wrap:\s*break-word/);
  assert.match(BASE, /overflow:\s*hidden; \/\* каркас — сетка на весь экран, скролл внутри \.view \*\//);
  assert.match(COMPONENTS, /\.app \{[^}]*grid-template-columns:\s*var\(--sidebar-width\) minmax\(0, 1fr\)/);
  assert.match(COMPONENTS, /\.view \{[\s\S]{0,120}min-height:\s*0/);
});

test('цели клика: на узком экране кнопки и строки не меньше 32 px', () => {
  const drawer = mediaBlock(COMPONENTS, 899);
  assert.match(drawer, /\.btn--sm, \.btn--icon\.btn--sm \{ min-width: 32px; min-height: 32px; \}/);
  assert.match(drawer, /\.nav-item \{ min-height: 34px; \}/);
  assert.match(drawer, /\.menu__item \{ min-height: 32px; \}/);
});

test('адаптив описан в DESIGN-NOTES, шкала слоёв — в tokens.css', () => {
  const notes = read('docs/DESIGN-NOTES.md');
  assert.match(notes, /## .*Адаптив: точки перелома и z-index/);
  for (const w of ['1099', '899', '639', '559']) assert.ok(notes.includes(w), `в заметках нет ${w}px`);
  assert.match(TOKENS, /--z-sidebar/);
  assert.match(TOKENS, /--z-nav-scrim/);
});
