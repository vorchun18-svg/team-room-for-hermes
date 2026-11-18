/**
 * delo.js — экран «Дела» (#/delos, #/room/<id>/delos) и карточка «Дело» (#/delo/<id>).
 *
 * W26-D6. Макет: `Vault/Projects/Developer/Team-Room/Mockup-Delo-2026-11-18.html`.
 * Форма дела — контракт `docs/WAVE26-RULES.md` §1, эталон — `fixtures/delo.json`.
 *
 * Правила, которые здесь соблюдаются:
 *   • блоки карточки сворачиваются общим компонентом (`section-collapse.js`) и
 *     помнят выбор человека;
 *   • у каждого «ждёт вас» есть действие — пункт без действий не показываем
 *     пустым (`needButtons` подставляет «Утвердить»);
 *   • «Письмо наружу» — только смена состояния по нажатию: черновик → одобрено →
 *     отмечено отправленным. Сами ничего не отправляем и честно об этом пишем;
 *   • раскладка по ширине вида (`@container` + `data-layout`), без горизонтальной
 *     прокрутки; на узком виде — одна колонка;
 *   • цвета только токенами (`var(--…)`), обе темы;
 *   • «Найдено» рисует компонент D4 (`found.js`) — здесь только запасная простая
 *     отрисовка за тем же интерфейсом `renderFound(list)`.
 */

import { h } from '../util.js';
import { icon } from '../icons.js';
import { api, poll } from '../api.js';
import { ensureCss, errorBanner, skeleton } from '../ui.js';
import { createSection } from './section-collapse.js';
import { setDelosCount } from '../shell-sidebar.js';
import {
  NEED_KIND_LABEL, OUTBOX_KIND_LABEL, OUTBOX_STATE_LABEL, OUTBOX_STATE_TONE, STATUS_LABEL, STATUS_TONE,
  actionLabel, agoText, deloMeta, dueInfo, dutyState, fetchNeedsBadge, fileRows, foundGroups, foundTitle,
  foundType, foundVerdict, listCounters, needButtons, nextStepText, normalizeDelo, outboxButtons, outboxMeta,
  partRows, pluralRu, sortDelos, stageChips, stageProgress, stampText, waitingRows,
} from './delo-model.js';

export const VIEW_CSS = 'css/view-delo.css';
export const POLL_MS = 30000;

const FILTER_KEY = 'teamroom.delos.filter';
const FILTERS = [
  { id: 'all', title: 'Все' },
  { id: 'needs', title: 'Ждут вашего решения' },
  { id: 'overdue', title: 'Просрочены' },
];

/* ─────────────────────────────────────────────────────────── общие мелочи */

/** Кнопка действия: обычная кнопка, у каждого действия есть подпись и тон. */
function actionButton(spec, onClick, opts = {}) {
  const kind = spec.kind === 'primary' ? 'btn--primary'
    : spec.kind === 'ghost' ? 'btn--ghost' : 'btn--secondary';
  return h('button', {
    class: ['btn', 'btn--sm', kind, 'delo-btn'],
    type: 'button',
    'data-action': spec.action,
    title: opts.title || spec.label,
    onclick: (ev) => { ev.preventDefault(); ev.stopPropagation(); onClick(spec, ev); },
  }, spec.label);
}

/** Копировать путь: без тупиков — если буфер недоступен, говорим об этом. */
async function copyPath(path, toast) {
  const text = String(path || '');
  if (!text) return;
  try {
    if (navigator.clipboard && navigator.clipboard.writeText) {
      await navigator.clipboard.writeText(text);
      toast('Путь скопирован: ' + text, 'ok');
      return;
    }
    throw new Error('no clipboard');
  } catch {
    toast('Не удалось скопировать. Путь: ' + text, 'warn');
  }
}

/** Показать в папке: серверное выделение файла (D3); нет маршрута — честный путь. */
async function revealPath(ctx, room, path) {
  try {
    await ctx.api.post('/api/files/reveal', { path, room });
    ctx.toast('Открываю папку с файлом', 'info');
    return;
  } catch { /* маршрута ещё нет (D3) — пробуем старый и говорим путь */ }
  try {
    await ctx.api.post('/api/artifacts/reveal', { path, room });
    ctx.toast('Открываю папку с файлом', 'info');
  } catch {
    ctx.toast('Папка не открылась. Файл лежит здесь: ' + path, 'warn');
  }
}

/** Открыть файл: серверное открытие (D3), запасной путь — старый маршрут. */
async function openPath(ctx, room, path) {
  try {
    await ctx.api.post('/api/files/launch', { path, room });
    ctx.toast('Открываю файл', 'info');
  } catch (err) {
    try {
      await ctx.api.post('/api/artifacts/open', { path, room });
      ctx.toast('Открываю файл', 'info');
    } catch {
      ctx.toast('Файл не открылся: ' + String((err && err.message) || 'сервер не ответил'), 'warn');
    }
  }
}

/* ─────────────────────────────────────────────────────────── «Найдено» */

/** Запасная отрисовка «Найдено» (пока нет компонента D4 `found.js`). */
function fallbackFoundList(list, ctx) {
  const groups = foundGroups(list);
  if (!groups.length) return h('p', { class: 'delo-empty small muted' }, 'Ничего не нашли — и это тоже ответ.');
  const nodes = [];
  const verdict = foundVerdict(list);
  if (verdict) nodes.push(h('p', { class: 'delo-found__verdict' }, icon('info', { size: 12 }), verdict));
  for (const group of groups) {
    nodes.push(h('div', { class: 'delo-found__group', 'data-group': group.key },
      h('div', { class: 'delo-found__gt' }, group.title, h('span', { class: 'muted small' }, ' · ' + group.items.length)),
      ...group.items.map((item) => {
        const type = foundType(item);
        const where = String(item['где'] || item.where || '');
        const desc = String(item['описание'] || item.description || '');
        const note = String(item['комментарий'] || item.comment || '');
        const isLink = type === 'сайт' && /^https?:/i.test(where);
        return h('div', { class: 'delo-found__item', 'data-type': type },
          h('div', { class: 'delo-found__name' },
            isLink ? h('a', { href: where, target: '_blank', rel: 'noreferrer noopener' }, foundTitle(item))
              : h('span', null, foundTitle(item)),
            h('span', { class: 'chip chip--muted delo-found__type' }, type)),
          desc ? h('div', { class: 'small muted delo-found__desc' }, desc) : null,
          note ? h('div', { class: 'small delo-found__note' }, note) : null,
          h('div', { class: 'delo-row-btns' },
            isLink
              ? h('a', { class: 'btn btn--sm btn--secondary', href: where, target: '_blank', rel: 'noreferrer noopener' }, 'Перейти на сайт')
              : null,
            !isLink && where ? h('button', {
              class: 'btn btn--sm btn--secondary', type: 'button', title: 'Открыть файл',
              onclick: () => openPath(ctx, ctx.roomId, where),
            }, 'Открыть') : null,
            !isLink && where ? h('button', {
              class: 'btn btn--sm btn--ghost', type: 'button', title: 'Показать в папке',
              onclick: () => revealPath(ctx, ctx.roomId, where),
            }, 'В папке') : null,
            !isLink && where ? h('button', {
              class: 'btn btn--sm btn--ghost', type: 'button', title: 'Скопировать путь',
              onclick: () => copyPath(where, ctx.toast),
            }, 'Копировать путь') : null));
      })));
  }
  return nodes;
}

/** Попробовать компонент D4, иначе — запасная отрисовка. Тот же интерфейс. */
async function renderFoundBlock(list, ctx) {
  try {
    const mod = await import('../found.js');
    if (mod && typeof mod.renderFound === 'function') {
      const out = mod.renderFound(list);
      if (out) return out;
    }
  } catch { /* компонента D4 ещё нет — это нормально */ }
  return fallbackFoundList(list, ctx);
}

/* ─────────────────────────────────────────────────────────────── список */

function listRow(delo, ctx, nowSec) {
  const due = dueInfo(delo.due, nowSec * 1000);
  const next = nextStepText(delo);
  const stages = stageChips(delo.stages);
  const needs = delo.needs_you.length;
  return h('a', {
    class: ['delo-row', needs ? 'delo-row--needs' : ''],
    href: '#/delo/' + encodeURIComponent(delo.id),
    'data-delo': delo.id,
    'data-room': delo.room,
    'aria-label': delo.title + (needs ? ', ждёт вашего решения: ' + needs : ''),
  },
  h('div', { class: 'delo-row__head' },
    h('span', { class: 'delo-row__title', title: delo.title }, delo.title),
    needs ? h('span', { class: 'chip chip--warn delo-row__needs' },
      icon('bell', { size: 11 }), String(needs)) : null,
    h('span', { class: 'chip chip--outline delo-row__status' }, STATUS_LABEL[delo.status] || delo.status),
    h('span', { class: ['chip', 'chip--' + (due.tone === 'muted' ? 'outline' : due.tone), 'delo-row__due'] }, due.text)),
  h('div', { class: 'delo-row__pipe', 'aria-label': 'Этапы дела' },
    ...stages.flatMap((s, i) => [
      i ? h('span', { class: 'delo-pipe__sep', 'aria-hidden': 'true' }, '›') : null,
      h('span', { class: ['delo-st', 'delo-st--' + s.state] }, s.mark + ' ' + s.title),
    ].filter(Boolean)),
    h('span', { class: 'delo-row__progress tiny muted' }, stageProgress(delo.stages))),
  h('div', { class: ['delo-row__next', next.tone === 'warn' ? 'delo-row__next--warn' : ''] }, next.text),
  h('div', { class: 'delo-row__meta small muted' },
    ctx.roomTitle(delo.room) ? h('span', { class: 'delo-row__room' }, ctx.roomTitle(delo.room)) : null,
    ctx.roomTitle(delo.room) ? h('span', { class: 'delo-row__dot' }, '·') : null,
    h('span', null, deloMeta(delo, nowSec))));
}

/* ────────────────────────────────────────────────────────────── карточка */

function chip(text, tone, extraClass) {
  return h('span', { class: ['chip', 'chip--' + (tone || 'outline'), extraClass || ''] }, text);
}

function needsNode(need, ctx) {
  const kind = NEED_KIND_LABEL[String(need.kind)] || 'Решение';
  const buttons = needButtons(need);
  const row = h('div', { class: 'delo-need', 'data-need': need.id, 'data-kind': need.kind });
  const text = h('div', { class: 'delo-need__text' }, String(need.text || ''));
  const rec = need.recommended
    ? h('div', { class: 'delo-need__rec small muted' }, icon('lightbulb', { size: 12 }), String(need.recommended))
    : null;
  const own = h('div', { class: 'delo-need__own', hidden: true },
    h('textarea', { class: 'delo-input', rows: 2, placeholder: 'Ваш ответ…', 'aria-label': 'Свой вариант ответа' }),
    h('div', { class: 'delo-row-btns' },
      h('button', {
        class: 'btn btn--sm btn--primary', type: 'button',
        onclick: (ev) => {
          const ta = ev.currentTarget.closest('.delo-need__own').querySelector('textarea');
          const value = ta ? String(ta.value || '').trim() : '';
          if (!value) { ctx.toast('Впишите ответ — пустой не отправляем', 'warn'); return; }
          ctx.act('answer', { need_id: need.id, text: value }, 'Ответ отправлен');
        },
      }, 'Отправить'),
      h('button', {
        class: 'btn btn--sm btn--ghost', type: 'button',
        onclick: (ev) => { ev.currentTarget.closest('.delo-need__own').hidden = true; },
      }, 'Отмена')));
  const btnRow = h('div', { class: 'delo-row-btns' }, ...buttons.map((b) => actionButton(b, (spec) => {
    if (spec.own) { own.hidden = false; const ta = own.querySelector('textarea'); if (ta) ta.focus(); return; }
    const payload = { need_id: need.id };
    if (spec.optionId) payload.option_id = spec.optionId;
    ctx.act(spec.action, payload, 'Готово: ' + spec.label);
  })));
  row.append(h('div', { class: 'delo-need__kind' }, kind), text, rec, btnRow, own);
  return row;
}

function sectionBlock(opts) {
  return createSection(Object.assign({
    card: true,
    className: 'delo-sec delo-sec--' + opts.key,
  }, opts));
}

function resultNode(delo, ctx) {
  const parts = partRows(delo.result.parts);
  const files = fileRows(delo.result.files);
  const nodes = [];
  if (parts.length) {
    nodes.push(h('div', { class: 'delo-parts' }, ...parts.map((p) => h('div', { class: 'delo-part' },
      h('span', { class: ['delo-part__state', 'delo-part__state--' + p.state] }, p.mark + ' ' + p.label),
      h('span', { class: 'delo-part__title' }, p.title)))));
  }
  if (files.length) {
    const fileNodes = files.map((f) => {
      const head = h('div', { class: 'delo-file__head' },
        h('button', {
          class: 'delo-file__name', type: 'button', title: 'Открыть файл',
          onclick: () => openPath(ctx, delo.room, f.path),
        }, f.title),
        chip(f.roleLabel, f.role === 'final' ? 'ok' : 'muted', 'delo-file__role'));
      const note = f.note ? h('div', { class: 'small muted delo-file__note' }, f.note) : null;
      const buttons = h('div', { class: 'delo-row-btns' },
        h('button', { class: 'btn btn--sm btn--secondary', type: 'button', onclick: () => openPath(ctx, delo.room, f.path) }, 'Открыть'),
        h('button', {
          class: 'btn btn--sm btn--ghost', type: 'button', title: 'Показать в папке',
          onclick: () => revealPath(ctx, delo.room, f.path),
        }, 'Показать в папке'),
        h('button', {
          class: 'btn btn--sm btn--ghost', type: 'button', title: 'Копировать путь',
          onclick: () => copyPath(f.path, ctx.toast),
        }, 'Копировать путь'));
      return h('div', { class: 'delo-file', 'data-path': f.path },
        h('span', { class: 'delo-file__mark', 'aria-hidden': 'true' }, icon('file', { size: 14 })),
        h('div', { class: 'delo-file__body' }, head, note, buttons));
    });
    nodes.push(h('div', { class: 'delo-files' }, ...fileNodes));
  }
  if (!nodes.length) nodes.push(h('p', { class: 'delo-empty small muted' }, 'Результата пока нет: дело только началось.'));
  return nodes;
}

function outboxNode(item, delo, ctx) {
  const state = String(item.state || 'draft');
  const tone = OUTBOX_STATE_TONE[state] || 'muted';
  const head = h('div', { class: 'delo-outbox__head' },
    icon('send', { size: 13 }),
    h('span', { class: 'delo-outbox__kind' }, OUTBOX_KIND_LABEL[String(item.kind)] || 'Исходящее'),
    chip(OUTBOX_STATE_LABEL[state] || state, tone, 'delo-outbox__state'));
  const meta = outboxMeta(item);
  return h('div', { class: 'delo-outbox', 'data-outbox': item.id, 'data-state': state },
    head,
    item.subject ? h('div', { class: 'delo-outbox__subject' }, String(item.subject)) : null,
    h('div', { class: 'delo-outbox__body' }, String(item.body || '')),
    meta ? h('div', { class: 'small muted delo-outbox__meta' }, meta) : null,
    h('div', { class: 'delo-row-btns' }, ...outboxButtons(item).map((b) => actionButton(b, (spec) => {
      ctx.act(spec.action, { outbox_id: spec.outboxId || item.id }, 'Готово: ' + spec.label);
    }))),
    h('div', { class: 'delo-outbox__honest small' },
      icon('lock', { size: 12 }),
      state === 'sent'
        ? 'Отмечено отправленным вручную. Проверить доставку и ответ можно в почте.'
        : 'Письмо наружу уходит только по вашему «да». Само приложение ничего не отправляет.'));
}

function timelineNode(delo, nowSec) {
  const rows = (Array.isArray(delo.timeline) ? delo.timeline : []).slice().sort((a, b) => (Number(b.at) || 0) - (Number(a.at) || 0));
  if (!rows.length) return h('p', { class: 'delo-empty small muted' }, 'Пока ничего не происходило.');
  return h('div', { class: 'delo-tl' }, ...rows.map((ev) => h('div', { class: 'delo-tl__row' },
    h('div', { class: 'delo-tl__at tiny muted tnum' }, stampText(ev.at, nowSec)),
    h('div', { class: 'delo-tl__text' }, String(ev.text || '')),
    ev.who ? h('div', { class: 'delo-tl__who tiny muted' }, String(ev.who)) : null)));
}

function dutiesNode(delo) {
  const list = Array.isArray(delo.duties) ? delo.duties : [];
  if (!list.length) return h('p', { class: 'delo-empty small muted' }, 'Дежурств у дела нет.');
  return h('div', { class: 'delo-duties' }, ...list.map((d) => {
    const st = dutyState(d);
    return h('div', { class: 'delo-duty', 'data-duty': d.id },
      h('div', { class: 'delo-duty__text' }, String(d.title || 'Дежурство')),
      h('div', { class: 'delo-duty__meta small' },
        String(d.schedule_human || ''), ' · ', h('b', { class: 'delo-duty__state delo-duty__state--' + st.tone }, st.label)));
  }));
}

/* ────────────────────────────────────────────────────────────── вьюха */

export async function mount(el, ctx) {
  await ensureCss(VIEW_CSS);
  const apiRef = (ctx && ctx.api) || api;
  const route = (ctx && ctx.route) || { name: 'delos', roomId: null, id: null };
  const navigate = (ctx && ctx.navigate) || ((hash) => { location.hash = hash; });
  const toast = (msg, kind) => { if (ctx && typeof ctx.toast === 'function') ctx.toast(msg || '', kind || 'info'); };
  const roomsStore = ctx && ctx.store && ctx.store.rooms;
  const roomList = () => (roomsStore && typeof roomsStore.get === 'function' ? roomsStore.get('list') || [] : []);
  const roomTitle = (id) => {
    const hit = roomList().find((r) => String(r.id) === String(id));
    return hit ? String(hit.title || id) : String(id || '');
  };
  const roomCtx = { api: apiRef, toast, roomId: route.roomId || '', roomTitle };

  let stopped = false;
  let filter = 'all';
  try {
    const saved = localStorage.getItem(FILTER_KEY);
    if (saved && FILTERS.some((f) => f.id === saved)) filter = saved;
  } catch { /* приватный режим */ }

  const isCard = route.name === 'delo' && route.id;
  const root = h('div', { class: ['delo', isCard ? 'delo--card' : 'delo--list'], 'data-view': isCard ? 'delo' : 'delos' });
  el.appendChild(root);
  const host = h('div', { class: 'delo__host' });
  root.appendChild(host);

  /* --- действия --------------------------------------------------------- */

  async function act(action, payload, okText) {
    const id = isCard ? String(route.id) : String(payload && payload.delo_id || '');
    if (!id) return;
    try {
      await apiRef.post('/api/delos/' + encodeURIComponent(id) + '/actions',
        Object.assign({ action }, payload || {}));
      toast(okText || ('Готово: ' + actionLabel(action)), 'ok');
      await load();
      refreshBadge();
    } catch (err) {
      const text = String((err && err.message) || 'сервер не ответил');
      toast('Не получилось: ' + text, 'warn');
    }
  }

  const actCtx = { act, toast, api: apiRef, roomId: route.roomId || '' };

  /* --- список ----------------------------------------------------------- */

  async function loadList() {
    const ids = route.roomId ? [route.roomId] : roomList().map((r) => String(r.id));
    if (!ids.length) return [];
    const payloads = await Promise.all(ids.map((id) =>
      apiRef.get('/api/rooms/' + encodeURIComponent(id) + '/delos').catch((err) => {
        if (ids.length === 1) throw err;
        return null;
      })));
    const items = [];
    for (const payload of payloads) {
      const list = Array.isArray(payload) ? payload : (payload && Array.isArray(payload.items) ? payload.items : []);
      items.push(...list.map(normalizeDelo));
    }
    return sortDelos(items);
  }

  function filterList(items) {
    if (filter === 'needs') return items.filter((d) => d.needs_you.length);
    if (filter === 'overdue') return items.filter((d) => dueInfo(d.due).overdue);
    return items;
  }

  function renderList(items) {
    const nowSec = Math.floor(Date.now() / 1000);
    const counters = listCounters(items);
    const shown = filterList(items);
    const head = h('header', { class: 'delo__head' },
      h('div', { class: 'delo__headline' },
        h('h1', { class: 'delo__title' }, 'Дела'),
        h('p', { class: 'delo__lead small muted' },
          counters.total
            ? 'дел: ' + counters.total + ' · ' + (counters.needs
              ? 'от вас ждут ' + counters.needs + ' ' + pluralRu(counters.needs, ['решение', 'решения', 'решений'])
              : 'решений не ждут')
            : 'Пока ни одного дела')),
      h('div', { class: 'delo__filters', role: 'group', 'aria-label': 'Что показать' },
        ...FILTERS.map((f) => h('button', {
          class: ['chip', 'delo-filter', filter === f.id ? 'chip--accent' : 'chip--outline'],
          type: 'button',
          'aria-pressed': filter === f.id ? 'true' : 'false',
          'data-filter': f.id,
          onclick: () => {
            filter = f.id;
            try { localStorage.setItem(FILTER_KEY, filter); } catch { /* ignore */ }
            renderList(items);
          },
        }, f.title))));

    const body = h('div', { class: 'delo-list' });
    if (!shown.length) {
      body.appendChild(h('div', { class: 'card delo-empty' },
        icon('checklist', { size: 20 }),
        h('div', { class: 'delo-empty__title' }, counters.total ? 'По этому фильтру дел нет' : 'Дел пока нет'),
        h('p', { class: 'small muted' },
          counters.total
            ? 'Выберите «Все» — там ' + counters.total + '.'
            : 'Дела появляются из запросов комнат или создаются вручную.'),
        counters.total ? h('button', {
          class: 'btn btn--sm btn--secondary', type: 'button',
          onclick: () => { filter = 'all'; renderList(items); },
        }, 'Показать все') : null));
    } else {
      for (const delo of shown) body.appendChild(listRow(delo, { roomTitle }, nowSec));
    }
    host.replaceChildren(head, body);
  }

  /* --- карточка --------------------------------------------------------- */

  function renderCard(raw) {
    const delo = normalizeDelo(raw);
    const nowSec = Math.floor(Date.now() / 1000);
    const due = dueInfo(delo.due, nowSec * 1000);
    const stages = stageChips(delo.stages);
    const needs = delo.needs_you;

    const header = h('header', { class: 'delo__head delo-card__head' },
      h('div', { class: 'delo-card__top' },
        h('button', {
          class: 'btn btn--ghost btn--sm delo-card__back', type: 'button',
          onclick: () => navigate(route.roomId ? '#/room/' + encodeURIComponent(route.roomId) + '/delos' : '#/delos'),
        }, icon('arrow-left', { size: 13 }), 'Все дела'),
        h('h1', { class: 'delo__title delo-card__title' }, delo.title),
        needs.length ? chip('ждёт вашего решения', 'warn', 'delo-card__needs') : null,
        chip(due.text, due.tone === 'muted' ? 'outline' : due.tone, 'delo-card__due'),
        chip(STATUS_LABEL[delo.status] || delo.status, STATUS_TONE[delo.status] || 'muted')),
      h('div', { class: 'delo-card__meta small muted' },
        'Дело · комната «' + (roomTitle(delo.room) || delo.room) + '»',
        delo.owner ? ' · ведёт: ' + delo.owner : '',
        delo.members.length ? ' · исполнители: ' + delo.members.join(', ') : '',
        ' · обновлено ' + (agoText(delo.updated_at, nowSec) || 'давно')));

    const pipe = h('div', { class: 'delo-pipe', 'aria-label': 'Этапы дела' },
      ...stages.flatMap((s, i) => [
        i ? h('span', { class: 'delo-pipe__sep', 'aria-hidden': 'true' }, '›') : null,
        h('button', {
          class: ['delo-st', 'delo-st--' + s.state, 'delo-st--btn'],
          type: 'button',
          title: 'Сделать этап «' + s.title + '» текущим',
          'aria-pressed': s.state === 'now' ? 'true' : 'false',
          onclick: () => act('set_stage', { stage_id: s.id }, 'Этап: ' + s.title),
        }, s.mark + ' ' + s.title),
      ].filter(Boolean)),
      h('span', { class: 'delo-pipe__progress tiny muted' }, stageProgress(delo.stages)));

    const needsSec = sectionBlock({
      key: 'needs', id: 'delo-needs-' + delo.id, requestId: 'delo:' + delo.id,
      title: 'Ждёт вашего решения' + (needs.length ? ' · ' + needs.length : ''),
      defaultOpen: true,
    });
    needsSec.body.append(...(needs.length
      ? needs.map((n) => needsNode(n, actCtx))
      : [h('p', { class: 'delo-empty small muted' }, 'От вас сейчас ничего не нужно — дело идёт само.')]));

    const resultSec = sectionBlock({
      key: 'result', id: 'delo-result-' + delo.id, requestId: 'delo:' + delo.id,
      title: 'Результат', defaultOpen: true,
    });
    resultSec.body.append(...resultNode(delo, actCtx));

    const outboxItems = Array.isArray(delo.outbox) ? delo.outbox : [];
    const outboxSec = sectionBlock({
      key: 'outbox', id: 'delo-outbox-' + delo.id, requestId: 'delo:' + delo.id,
      title: outboxItems.length === 1 ? 'Письмо наружу' : 'Исходящие (' + outboxItems.length + ')',
      defaultOpen: true,
    });
    outboxSec.body.append(...(outboxItems.length
      ? outboxItems.map((o) => outboxNode(o, delo, actCtx))
      : [h('p', { class: 'delo-empty small muted' }, 'Наружу по этому делу ничего не готовится.')]));

    const waitingSec = sectionBlock({
      key: 'waiting', id: 'delo-wait-' + delo.id, requestId: 'delo:' + delo.id,
      title: 'Ждём', defaultOpen: true,
    });
    const waits = waitingRows(delo.waiting_on, nowSec);
    waitingSec.body.append(...(waits.length
      ? waits.map((w) => h('div', { class: 'delo-wait' },
        h('span', { class: ['delo-wait__who', w.isYou ? 'delo-wait__who--you' : ''] }, w.who),
        h('div', { class: 'delo-wait__body' },
          h('div', { class: 'delo-wait__what' }, w.what),
          w.since ? h('div', { class: 'tiny muted' }, w.since) : null)))
      : [h('p', { class: 'delo-empty small muted' }, 'Никого не ждём.')]));

    const foundHost = h('div', { class: 'delo-found' }, h('p', { class: 'small muted' }, 'Смотрим, что нашлось…'));
    const foundSec = sectionBlock({
      key: 'found', id: 'delo-found-' + delo.id, requestId: 'delo:' + delo.id,
      title: 'Найдено по делу', defaultOpen: true,
    });
    foundSec.body.append(foundHost);
    renderFoundBlock(delo.found, { toast, roomId: delo.room, api: apiRef }).then((out) => {
      if (stopped) return;
      const nodes = Array.isArray(out) ? out : [out];
      foundHost.replaceChildren(...nodes.filter(Boolean));
    }).catch(() => {
      foundHost.replaceChildren(h('p', { class: 'small muted' }, 'Блок «Найдено» не открылся.'));
    });

    const tlSec = sectionBlock({
      key: 'timeline', id: 'delo-tl-' + delo.id, requestId: 'delo:' + delo.id,
      title: 'Ход дела', defaultOpen: true,
    });
    tlSec.body.append(timelineNode(delo, nowSec));

    const dutySec = sectionBlock({
      key: 'duties', id: 'delo-duty-' + delo.id, requestId: 'delo:' + delo.id,
      title: 'Дежурства дела', defaultOpen: true,
    });
    dutySec.body.append(dutiesNode(delo));

    const main = h('div', { class: 'delo__col delo__col--main' }, needsSec.element, resultSec.element, outboxSec.element);
    const side = h('div', { class: 'delo__col delo__col--side' }, waitingSec.element, foundSec.element, tlSec.element, dutySec.element);

    const foot = h('div', { class: 'delo-card__foot' },
      h('button', {
        class: 'btn btn--sm btn--secondary', type: 'button', title: 'Открыть комнату дела',
        onclick: () => navigate('#/room/' + encodeURIComponent(delo.room) + '/composer'),
      }, icon('home', { size: 13 }), 'Комната дела'),
      delo.request_id ? h('a', {
        class: 'btn btn--sm btn--ghost',
        href: '#/room/' + encodeURIComponent(delo.room) + '/request/' + encodeURIComponent(delo.request_id),
      }, 'Запрос, из которого пришло дело') : null,
      delo.status === 'paused'
        ? h('button', { class: 'btn btn--sm btn--primary', type: 'button', onclick: () => act('resume', {}, 'Дело возобновлено') }, 'Возобновить')
        : h('button', { class: 'btn btn--sm btn--ghost', type: 'button', onclick: () => act('pause', {}, 'Дело на паузе') }, 'Пауза'),
      h('button', { class: 'btn btn--sm btn--ghost', type: 'button', onclick: () => act('done', {}, 'Дело закрыто') }, 'Закрыть дело'));

    host.replaceChildren(header, pipe, h('div', { class: 'delo__grid' }, main, side), foot);
    document.title = delo.title + ' — Дело';
  }

  /* --- загрузка --------------------------------------------------------- */

  let lastItems = [];

  async function load(first) {
    if (stopped) return;
    if (first) host.replaceChildren(skeleton(4));
    try {
      if (isCard) {
        const raw = await apiRef.get('/api/delos/' + encodeURIComponent(route.id));
        if (stopped) return;
        if (!raw) throw new Error('дело не найдено');
        renderCard(raw);
      } else {
        const items = await loadList();
        if (stopped) return;
        lastItems = items;
        renderList(items);
        setDelosCountSafe(items.reduce((sum, d) => sum + d.needs_you.length, 0));
      }
    } catch (err) {
      if (stopped) return;
      host.replaceChildren(errorBanner('Не удалось открыть «Дела»: '
        + String((err && err.message) || 'сервер не ответил'),
      () => { host.replaceChildren(); load(true).catch(() => {}); }));
    }
  }

  /** Значок меню: ошибку молчим, экран всё равно скажет правду. */
  function refreshBadge() {
    if (isCard) {
      fetchNeedsBadge(apiRef, roomList().map((r) => r.id)).then((n) => { if (n !== null) setDelosCountSafe(n); }).catch(() => {});
    }
  }

  function setDelosCountSafe(n) {
    try { setDelosCount(n); } catch { /* меню ещё не собрано */ }
  }

  let stopPoll = null;
  await load(true);
  if (!stopped) {
    stopPoll = poll(() => load(false).catch(() => {}), POLL_MS, { pauseHidden: true, onError: () => {} });
    if (isCard) refreshBadge();
  }

  return {
    unmount() {
      stopped = true;
      if (stopPoll && typeof stopPoll.stop === 'function') stopPoll.stop();
      else if (typeof stopPoll === 'function') stopPoll();
      document.title = document.title.replace(/ — Дело$/, '');
    },
    get items() { return lastItems; },
  };
}

export default { mount, VIEW_CSS, POLL_MS };
