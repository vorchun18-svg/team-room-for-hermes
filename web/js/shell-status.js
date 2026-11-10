/**
 * shell-status.js — баннеры состояния в шапке и проверка «движок жив».
 * Вынесено из app.js, чтобы каркас оставался в пределах ~600 строк (§10.5).
 * В текстах — «движок» и «база задач», без технических имён служб.
 */

import { h, plainTerms } from './util.js';
import { icon } from './icons.js';
import { health } from './store.js';

let D = null;

/**
 * @param {{pick:Function, toast:Function, retry:Function}} deps
 *   pick(obj, 'a.b') — чтение по пути; retry — перезагрузить health+rooms.
 */
export function initStatusBanners(deps) {
  D = deps;
}

/**
 * Работает ли движок команды (терпимо к форме /api/health).
 * @param {object|null} data ответ /api/health
 * @returns {boolean|null}
 */
export function dispatcherRunning(data) {
  if (!data) return null;
  const d = data.dispatcher || data.gateway || D.pick(data, 'health.dispatcher');
  if (!d) return data.ok !== false;
  if (typeof d.running === 'boolean') return d.running;
  if (typeof d.alive === 'boolean') return d.alive;
  const s = String(d.status || d.state || '').toLowerCase();
  if (!s) return true;
  return ['running', 'ok', 'alive', 'healthy', 'working', 'active'].includes(s);
}

function bannerNode(kind, iconName, text, action) {
  const icons = { warn: 'warning', error: 'error', info: 'info', ok: 'pass' };
  return h('div', { class: 'banner banner--' + kind, role: kind === 'error' ? 'alert' : 'status' },
    icon(icons[kind] || 'info', { size: 14 }),
    h('div', { class: 'banner__text' }, text),
    action || null,
  );
}

/** Перерисовать #banners по текущему состоянию health. */
export function renderBanners() {
  const host = document.querySelector('#banners');
  if (!host) return;
  host.replaceChildren();
  const hd = health.get('data');

  if (health.get('error')) {
    host.appendChild(bannerNode('error', 'error',
      'Нет связи с приложением. Запустите его заново (start-team-room.cmd).',
      h('button', { class: 'btn btn--ghost btn--sm', onclick: () => D.retry() }, 'Повторить')));
    return;
  }
  if (!hd) return;

  if (dispatcherRunning(hd) === false) {
    host.appendChild(bannerNode('warn', 'warning',
      'Движок команды остановлен — новые запросы не начнут выполняться, пока он не запустится.',
      h('button', { class: 'btn btn--ghost btn--sm', onclick: () => { D.retryHealth(); D.toast('Проверяю движок…', 'info'); } }, 'Проверить')));
  }
  if (D.pick(hd, 'engine.schema_ok') === false) {
    host.appendChild(bannerNode('error', 'error', 'Данные движка изменили формат — приложение только показывает их, без правок.'));
  }
  if (hd.ok === false && !health.get('error')) {
    host.appendChild(bannerNode('warn', 'warning', 'Приложение сообщает о проблеме: ' + plainTerms(hd.message || hd.detail || 'подробности — в «Настройки → Здоровье»')));
  }
}

/**
 * Блок состояния в подвале сайдбара: движок, связь, «локально» и путь данных.
 * @returns {void}
 */
export function renderHealthBlock() {
  const box = document.querySelector('#health');
  if (!box) return;
  const hd = health.get('data');
  const disp = hd ? dispatcherRunning(hd) : null;
  const schemaOk = hd ? D.pick(hd, 'engine.schema_ok') : undefined;

  const row = (state, name, note) => h('div', { class: 'health__row health__row--' + state, title: note || '' },
    icon(state === 'ok' ? 'pass-filled' : state === 'warn' ? 'warning' : 'error', { size: 11 }),
    h('span', { class: 'truncate' }, name),
  );

  const rows = [];
  if (health.get('loading')) rows.push(row('warn', 'Проверяю связь…'));
  else if (health.get('error')) rows.push(row('bad', 'Нет связи с приложением'));
  else {
    rows.push(row(disp ? 'ok' : 'warn', disp ? 'Движок работает' : 'Движок остановлен'));
    rows.push(row(schemaOk === false ? 'bad' : 'ok', schemaOk === false ? 'Данные движка изменились' : 'Связь с движком'));
  }
  rows.push(originRow(hd));
  box.replaceChildren(...rows);
}

/** Строка «локально» с адресом и путём данных в подсказке. */
function originRow(hd) {
  const dataPath = D.pick(hd, 'paths.data') || D.pick(hd, 'data_dir') || D.pick(hd, 'dataDir') || 'data/';
  const vault = D.pick(hd, 'paths.vault') || D.pick(hd, 'vault_dir') || '';
  const title = 'Только на этом компьютере.\nДанные: ' + dataPath + (vault ? '\nЗаметки: ' + vault : '');
  return h('div', { class: 'health__origin', title },
    icon('lock', { size: 11 }),
    h('span', { class: 'truncate' }, 'локально'),
  );
}
