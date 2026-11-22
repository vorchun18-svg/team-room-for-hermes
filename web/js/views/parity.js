/**
 * parity.js — экран «Совместимость с Hermes» (#/settings/parity, W29B-Q2).
 *
 * Комната — продолжение Hermes, а не надстройка. Экран отвечает на вопрос «чем
 * запуск карточки в этой комнате отличается от обычного запуска агента в
 * Hermes»: по каждому участнику — профиль, модель и её ИСТОЧНИК, наборы
 * инструментов, навыки, MCP-серверы, политика разрешений (уровень автономии,
 * allowlist, single_query_mode) и расхождения с профилем. Красным — только то,
 * что меняет поведение запуска (например, комната задаёт одну модель, профиль —
 * другую).
 *
 * Данные — GET /api/rooms/{id}/parity (server/parity.py, только чтение).
 * Единственное исправление здесь — «Вернуть модель профиля» (снимает замену
 * модели в реестре комнаты): профили Hermes экран не меняет намеренно.
 */

import { h } from '../util.js';
import { icon } from '../icons.js';
import { ensureCss, sectionHeader, skeleton, errorBanner } from '../ui.js';

const VIEW_CSS = 'css/view-parity.css';

/* ── чистые функции (проверяются node --test) ─────────────────────────────── */

export const STATUS_LABEL = {
  ok: 'совпадает', warn: 'есть отличия', fail: 'расхождение', off: 'не проверено',
};

/** Сводка по участникам (если сервер её не прислал — считаем сами). */
export function summarize(members) {
  const out = { ok: 0, warn: 0, fail: 0, off: 0, total: 0 };
  for (const item of Array.isArray(members) ? members : []) {
    if (!item || typeof item !== 'object') continue;
    const status = STATUS_LABEL[item.status] ? item.status : 'off';
    out[status] += 1;
    out.total += 1;
  }
  return out;
}

/** Модель одной строкой: «glm-4 — ollama-cloud · задано комнатой». */
export function modelLine(member) {
  const model = (member && member.model) || {};
  const name = String(model.model || '').trim();
  if (!name) return 'модель не задана';
  const provider = String(model.provider || '').trim();
  const source = model.source === 'room' ? 'задано комнатой' : 'из профиля';
  return name + (provider ? ' — ' + provider : '') + ' · ' + source;
}

/** Строка «навыки / MCP / инструменты» без нулей-обманок. */
export function assetsLine(member) {
  const skills = (member && member.skills) || {};
  const mcp = (member && member.mcp) || {};
  const tools = (member && member.tools) || {};
  const parts = [];
  const own = Number(skills.own) || 0;
  const shared = Number(skills.shared) || 0;
  // «(общих N)» — когда навыки берутся из общего каталога Hermes: у профиля своих нет
  // или их меньше. Иначе число читается как «у профиля ровно столько».
  parts.push('навыков: ' + (own || shared || 0)
    + (shared > own ? ' (общих ' + shared + ')' : ''));
  parts.push('MCP: ' + (mcp.count ? mcp.names.join(', ') : 'нет'));
  if (tools.count) parts.push('наборов инструментов: ' + tools.count);
  return parts.join(' · ');
}

/** Политика разрешений одной строкой. */
export function permissionLine(member) {
  const perm = (member && member.permissions) || {};
  const mode = String(perm.single_query_mode || 'deny');
  const auto = Number(perm.allowlist_count) || 0;
  const level = String(perm.level_label || '');
  const tail = mode === 'approve' ? 'запуск кода без вопросов'
    : (auto ? 'разрешено без вопроса: ' + auto : 'каждый запуск скрипта — вопрос');
  return 'уровень комнаты «' + (level || '—') + '» · ' + tail;
}

/** Сколько расхождений требуют внимания (для заголовка и крошек). */
export function attentionCount(members) {
  return (Array.isArray(members) ? members : [])
    .filter((m) => m && (m.status === 'fail' || m.status === 'warn')).length;
}

/* ── разметка ─────────────────────────────────────────────────────────────── */

function chip(status, text) {
  return h('span', { class: 'par__chip par__chip--' + status }, text || STATUS_LABEL[status] || '');
}

function diffRow(diff, member, onFix, onOpen) {
  const fix = (diff && diff.fix) || {};
  const canFix = fix.kind === 'reset_room_model' && typeof onFix === 'function';
  const openSettings = fix.kind === 'open_models' || fix.kind === 'open_autonomy';
  return h('li', { class: 'par__diff par__diff--' + (diff.severity || 'warn') },
    h('span', { class: 'par__diff-text' }, String(diff.text || '')),
    canFix ? h('button', {
      type: 'button', class: 'btn btn--secondary btn--sm', 'data-fix': 'reset_model',
      'data-profile': member.profile,
      onclick: () => onFix(member),
    }, String(fix.label || 'Исправить'))
      : null,
    openSettings && typeof onOpen === 'function' ? h('button', {
      type: 'button', class: 'btn btn--link btn--sm',
      'data-open': fix.kind, 'data-profile': member.profile,
      onclick: () => onOpen(fix, member),
    }, String(fix.label || 'Открыть')) : null);
}

function memberCard(member, ctx) {
  const status = STATUS_LABEL[member.status] ? member.status : 'off';
  const diffs = Array.isArray(member.diffs) ? member.diffs : [];
  const differences = Array.isArray(member.differences) ? member.differences : [];
  return h('article', { class: 'par__card par__card--' + status, 'data-profile': member.profile,
    'data-status': status },
  h('header', { class: 'par__card-head' },
    h('div', { class: 'par__who' },
      h('span', { class: 'par__name' }, String(member.label || member.profile || '')),
      member.description ? h('span', { class: 'par__desc small muted' }, String(member.description)) : null,
      h('span', { class: 'par__tech small muted' },
        String(member.profile || '') + ' · ' + String(member.role || '') + ' · '
          + String(member.profile_dir || ''))),
    chip(status)),
  h('dl', { class: 'par__facts' },
    h('dt', null, 'Модель'), h('dd', null, modelLine(member)),
    h('dt', null, 'Навыки и средства'), h('dd', null, assetsLine(member)),
    h('dt', null, 'Разрешения'), h('dd', null, permissionLine(member))),
  differences.length ? h('ul', { class: 'par__differences small' },
    ...differences.map((text) => h('li', null, String(text)))) : null,
  diffs.length ? h('ul', { class: 'par__diffs' },
    ...diffs.map((diff) => diffRow(diff, member, ctx.onFix, ctx.onOpen))) : null);
}

/* ── экран ────────────────────────────────────────────────────────────────── */

export async function mount(el, ctx = {}) {
  if (!el) return { unmount() {} };
  await ensureCss(VIEW_CSS);
  const api = ctx.api || null;
  const route = ctx.route || {};
  const query = (route && route.query) || {};
  let stopped = false;
  let rooms = [];
  let roomId = String(query.room || (ctx.store && ctx.store.currentRoom && ctx.store.currentRoom.id) || '');
  let data = null;
  let error = null;
  let busy = false;

  const root = h('div', { class: 'par' });
  el.replaceChildren(root);
  root.appendChild(sectionHeader('Совместимость с Hermes'));
  root.appendChild(h('p', { class: 'par__intro small muted' },
    'Что у каждого участника взято из профиля Hermes, что из общих настроек, а что задала комната. '
      + 'Красным — только то, что меняет запуск карточки. Экран только читает: профили Hermes не меняются.'));

  const picker = h('div', { class: 'par__picker' });
  const body = h('div', { class: 'par__body' });
  root.appendChild(picker);
  root.appendChild(body);

  function roomOptions() {
    picker.replaceChildren();
    if (rooms.length < 2) return;
    const select = h('select', { class: 'par__select', 'aria-label': 'Комната для проверки' },
      ...rooms.map((room) => h('option', { value: String(room.id), selected: String(room.id) === roomId },
        String(room.title || room.id))));
    select.addEventListener('change', () => {
      roomId = String(select.value || '');
      data = null;
      load();
    });
    picker.appendChild(h('label', { class: 'par__picker-label small muted' }, 'Комната:'));
    picker.appendChild(select);
  }

  function fix(member) {
    if (busy || !api || !roomId) return;
    busy = true;
    Promise.resolve(api.post('/api/rooms/' + encodeURIComponent(roomId) + '/parity/fix',
      { action: 'reset_model', profile: member.profile }))
      .then((payload) => {
        if (stopped) return;
        data = payload || null;
        if (typeof ctx.toast === 'function') {
          ctx.toast('Модель «' + (member.label || member.profile) + '» — снова как в профиле', 'ok');
        }
        render();
      })
      .catch((err) => {
        if (stopped) return;
        if (typeof ctx.toast === 'function') ctx.toast((err && err.message) || 'Не удалось вернуть модель профиля', 'danger');
      })
      .then(() => { busy = false; });
  }

  function openFix(_fix, member) {
    // Правка профиля — только в настройках Hermes; здесь ведём в настройки комнаты.
    if (typeof ctx.toast === 'function') {
      ctx.toast('Модель и уровень участника «' + (member.label || member.profile)
        + '» меняются в настройках комнаты', 'info');
    }
    if (typeof ctx.navigate === 'function' && roomId) {
      ctx.navigate('#/room/' + encodeURIComponent(roomId) + '/team');
    }
  }

  function render() {
    body.replaceChildren();
    if (error) {
      body.appendChild(errorBanner((error && error.message) || 'Совместимость не прочитана', () => load()));
      return;
    }
    if (!data) { body.appendChild(skeleton(3)); return; }
    const members = Array.isArray(data.members) ? data.members : [];
    const summary = summarize(members);
    const attention = attentionCount(members);
    body.appendChild(h('div', { class: 'par__summary' },
      chip(summary.fail ? 'fail' : (summary.warn ? 'warn' : 'ok'),
        summary.fail ? 'расхождений моделей: ' + summary.fail
          : (attention ? 'отличий: ' + attention : 'всё совпадает')),
      h('span', { class: 'par__summary-text small muted' },
        'участников: ' + summary.total + ' · уровень комнаты: «' + String(data.level_label || data.level || '—') + '»'),
      h('button', { type: 'button', class: 'btn btn--secondary btn--sm', onclick: () => load() },
        'Обновить')));
    if (!members.length) {
      body.appendChild(h('div', { class: 'banner banner--info' },
        h('div', { class: 'banner__text' },
          'В комнате нет участников — сравнивать нечего. Добавьте участников в «Настройках комнаты → Состав».'),
        h('a', { class: 'btn btn--secondary btn--sm',
          href: '#/room/' + encodeURIComponent(roomId) + '/team' }, 'Открыть «Команду»')));
      return;
    }
    const list = h('div', { class: 'par__list' });
    for (const member of members) {
      list.appendChild(memberCard(member, { onFix: fix, onOpen: openFix }));
    }
    body.appendChild(list);
    if (Array.isArray(data.notes) && data.notes.length) {
      body.appendChild(h('ul', { class: 'par__notes small muted' },
        ...data.notes.map((note) => h('li', null, String(note)))));
    }
  }

  function load() {
    if (!api || typeof api.get !== 'function') {
      error = { message: 'Нет связи с приложением — обновите страницу' };
      render();
      return Promise.resolve();
    }
    render();
    if (!rooms.length) {
      return Promise.resolve(api.get('/api/rooms'))
        .then((payload) => {
          const list = Array.isArray(payload) ? payload : (payload && payload.rooms) || [];
          rooms = list.filter((room) => room && room.id && !room.archived);
          if (!roomId && rooms.length) roomId = String(rooms[0].id);
          roomOptions();
          return loadRoom();
        })
        .catch((err) => {
          if (stopped) return;
          error = err; render();
        });
    }
    return loadRoom();
  }

  function loadRoom() {
    if (!roomId) {
      error = { message: 'Выберите комнату — совместимость считается по её участникам' };
      render();
      return Promise.resolve();
    }
    return Promise.resolve(api.get('/api/rooms/' + encodeURIComponent(roomId) + '/parity'))
      .then((payload) => {
        if (stopped) return;
        data = payload || null;
        error = null;
        render();
      })
      .catch((err) => {
        if (stopped) return;
        data = null;
        error = err;
        render();
      });
  }

  render();
  load();

  return {
    unmount() { stopped = true; },
    reload: () => load(),
  };
}

export default { mount };
