/**
 * agent-results.js — блок «Результаты агентов» в задаче (W30-E, дефект 16).
 *
 * Зачем: в задаче работает десяток исполнителей, и понять «кто что изучил,
 * какой вывод, где лежит файл, что осталось» было нельзя — всё тонуло в диалоге,
 * дереве карточек и файлах. Блок стоит ПОД «Сводом» и отвечает на эти четыре
 * вопроса строками, без переходов на другие экраны.
 *
 * Данные — `detail.agent_results` из `GET /api/requests/{id}` (сервер собирает из
 * дерева карточек и их метаданных, моделей не вызывает); та же форма у
 * `GET /api/requests/{id}/agent-results` (её берём для подзадач проекта).
 *
 * Кнопки — существующие ручки: «Открыть» — `/api/files/launch {mode:"open"}`,
 * «Открыть в папке» — тот же адрес с `mode:"reveal"`, «Просмотр» — панель
 * предпросмотра вложений (W29B-Q3, `./attach-preview.js`).
 *
 * Правка данных не нужна: блок только читает. Никакого innerHTML из данных —
 * только `h()`; ширина кнопок фиксированная (правило волны 30).
 */

import { h } from '../util.js';
import { icon } from '../icons.js';
import { ensureCss } from '../ui.js';
import { countNoun } from '../text-ru.js';
import { participantLabel } from '../participant-label.js';
import { createSection } from './section-collapse.js';

ensureCss('css/view-agent-results.css');

/** Сообщение человеку — через контекст страницы (своего стора у блока нет). */
function say(ctx, text, kind) {
  try {
    if (ctx && typeof ctx.toast === 'function') ctx.toast(String(text || ''), kind || 'info');
  } catch { /* тост вторичен */ }
}

/** Подпись статуса, если сервер её не прислал (`status_label`). */
const STATUS_RU = {
  done: 'готово', review: 'на проверке', running: 'в работе', blocked: 'остановлено',
  failed: 'ошибка', todo: 'не начато', queued: 'в очереди', archived: 'в архиве',
};

/** Тон статуса — цвет чипа (только класс, без данных). */
const STATUS_TONE = {
  done: 'ok', archived: 'muted', review: 'info', running: 'info',
  blocked: 'warn', failed: 'danger', todo: 'muted', queued: 'muted',
};

/** Машинный вердикт критика — по-русски (в карточке `ship`/`revise`/`rethink`). */
const VERDICT_RU = {
  ship: 'можно отдавать', ship_with_changes: 'с правками', revise: 'на доработку',
  rethink: 'переделать', approve: 'одобрено', reject: 'отклонено', pass: 'принято',
};

const STAGE_RU = {
  new: 'Новая', working: 'В работе', needs_you: 'Ждёт вас', review: 'На проверке',
  done_system: 'Готово', accepted: 'Принята', archived: 'В архиве', scouting: 'Разведка',
  overview_ready: 'Обзор сдан',
};

/** Фильтр строк: все / с файлами / что осталось (без обращения к серверу). */
const FILTERS = [
  { id: 'all', label: 'Все' },
  { id: 'files', label: 'С файлами' },
  { id: 'left', label: 'Что осталось' },
];

function agentName(row, room) {
  try {
    const label = participantLabel(row.agent || '', { room: room || null });
    if (label && (label.name || label.title)) {
      return { text: label.name || label.title, hint: [label.title, label.sub].filter(Boolean).join(' · ') };
    }
  } catch { /* подпись вторична */ }
  const raw = String((row && row.agent) || '').trim();
  return { text: raw || 'Исполнитель', hint: raw };
}

function btn(label, opts = {}) {
  const o = opts || {};
  const node = h('button', {
    class: ['btn', o.variant ? 'btn--' + o.variant : 'btn--secondary', 'btn--sm', 'agr__btn'],
    type: 'button',
    title: o.title || null,
    disabled: o.disabled ? 'disabled' : null,
  }, o.icon ? icon(o.icon, { size: 12 }) : null, h('span', null, label));
  if (typeof o.onclick === 'function') node.addEventListener('click', o.onclick);
  return node;
}

/** Серверное действие с файлом — те же ручки, что у блока «Файлы». */
async function fileAction(ctx, path, roomId, mode) {
  try {
    await ctx.api.post('/api/files/launch', { path, room: String(roomId || ''), mode });
    return true;
  } catch (err) {
    const fallback = mode === 'reveal' ? 'Не удалось открыть папку' : 'Не удалось открыть файл';
    say(ctx, (err && err.message) || fallback, 'danger');
    return false;
  }
}

/** «Просмотр» — панель предпросмотра вложений (W29B-Q3), грузится по требованию. */
async function openPreview(ctx, path, roomId, requestId) {
  try {
    const mod = await import('./attach-preview.js');
    const name = String(path).split(/[\\/]/).pop() || 'Файл';
    await mod.openAttachPreview({
      path, name, room: String(roomId || ''), requestId: String(requestId || ''),
      api: (ctx && ctx.api) || undefined,
    });
  } catch (err) {
    say(ctx, 'Не удалось открыть просмотр', 'danger');
  }
}

/** Строка файла: имя, путь, «Просмотр» / «Открыть» / «Открыть в папке». */
function fileRow(file, ctx, roomId, requestId) {
  const path = String((file && file.path) || '');
  const name = String((file && file.name) || path.split(/[\\/]/).pop() || 'Файл');
  const exists = !file || file.exists !== false;
  const acts = h('div', { class: 'agr__file-acts' },
    btn('Просмотр', {
      icon: 'eye', title: exists ? 'Посмотреть файл здесь' : 'Файла нет на месте',
      disabled: !exists,
      onclick: () => { openPreview(ctx, path, roomId, requestId); },
    }),
    btn('Открыть', {
      icon: 'open-preview', variant: 'ghost', title: 'Открыть программой по умолчанию',
      disabled: !exists,
      onclick: () => { fileAction(ctx, path, roomId, 'open'); },
    }),
    btn('Открыть в папке', {
      icon: 'folder-opened', variant: 'ghost', title: 'Показать файл в проводнике',
      disabled: !exists,
      onclick: () => { fileAction(ctx, path, roomId, 'reveal'); },
    }));
  return h('li', { class: 'agr__file' + (exists ? '' : ' agr__file--missing') },
    h('div', { class: 'agr__file-main' },
      h('span', { class: 'agr__file-name', title: path }, icon('file-text', { size: 12 }), name),
      h('span', { class: 'agr__file-path', title: path }, path)),
    exists ? null : h('span', { class: 'chip chip--warn agr__file-flag' }, 'файла нет на месте'),
    acts);
}

/** Одна карточка результата: кто, что делал, вывод, файлы, что осталось. */
function agentRow(row, ctx, room, roomId, requestId) {
  const status = String(row.status || '');
  const statusLabel = String(row.status_label || STATUS_RU[status] || status || '');
  const tag = String(row.verdict_tag || '').trim();
  const name = agentName(row, room);
  const roleText = String(row.role || '');
  const head = h('div', { class: 'agr__head' },
    h('span', { class: 'agr__person', title: name.hint || name.text }, icon('person', { size: 12 }),
      h('span', { class: 'agr__person-name' }, name.text)),
    roleText && roleText !== name.text
      ? h('span', { class: 'agr__role' }, roleText) : null,
    statusLabel
      ? h('span', { class: 'chip chip--' + (STATUS_TONE[status] || 'muted') }, statusLabel)
      : null,
    tag && VERDICT_RU[tag] ? h('span', { class: 'chip chip--info' }, VERDICT_RU[tag]) : null);

  const body = h('div', { class: 'agr__body' });
  if (row.title) body.appendChild(h('div', { class: 'agr__task', title: String(row.title) }, String(row.title)));
  if (row.verdict) {
    body.appendChild(h('p', { class: 'agr__verdict' }, String(row.verdict)));
  } else {
    body.appendChild(h('p', { class: 'agr__verdict agr__verdict--empty' },
      status === 'running' || status === 'review'
        ? 'Вывод ещё не готов — работа идёт.'
        : 'Вывода в карточке нет.'));
  }
  if (row.remaining) {
    body.appendChild(h('div', { class: 'agr__left' },
      icon('clock', { size: 12 }),
      h('span', { class: 'agr__left-label' }, 'Осталось: '),
      h('span', { class: 'agr__left-text' }, String(row.remaining))));
  }

  const files = Array.isArray(row.files) ? row.files : [];
  const fileList = h('div', { class: 'agr__files' });
  if (files.length) {
    const more = Number(row.files_total || files.length) - files.length;
    const mirrors = Number(row.files_mirrors || 0);
    const hidden = Number(row.files_hidden || 0);
    const ul = h('ul', { class: 'agr__file-list' });
    for (const file of files) ul.appendChild(fileRow(file, ctx, roomId, requestId));
    fileList.appendChild(ul);
    if (more > 0 || mirrors > 0 || hidden > 0) {
      const notes = [];
      if (more > 0) notes.push('и ещё ' + countNoun(more, ['файл', 'файла', 'файлов']));
      if (mirrors > 0) notes.push('копий в других папках: ' + mirrors);
      if (hidden > 0) notes.push('скрыто похожих на секреты: ' + hidden);
      fileList.appendChild(h('div', { class: 'agr__files-note small muted' }, notes.join(' · ')));
    }
  }

  const art = h('article', { class: 'agr__row', 'data-card': row.card_id || '', dataset: { state: status } },
    head, body, files.length ? fileList : null);
  return art;
}

/** Пустое состояние: зачем блок и что будет дальше. */
function emptyState(detail) {
  const status = String((detail && detail.status) || '');
  const running = status === 'working' || status === 'queued' || status === 'needs_you';
  return h('div', { class: 'agr__empty' },
    h('div', { class: 'agr__empty-title' }, 'Работа ещё не сдана'),
    h('p', { class: 'agr__empty-text' },
      'Здесь появится по каждому исполнителю: что он изучил или сделал, вывод в две-три строки, '
      + 'файл с результатом (путь и кнопки «Открыть») и что осталось.'),
    h('p', { class: 'agr__empty-text small muted' },
      running
        ? 'Задача в работе: строки добавятся сами, как только агенты сдадут карточки.'
        : 'Карточки исполнителей ещё не появились — как только работа начнётся, список заполнится.'));
}

/** Свёртка «по подзадачам»: у родителя проекта — ссылки и результаты детей. */
function projectBlock(detail, ctx, roomId) {
  const children = (detail && detail.project && Array.isArray(detail.project.children))
    ? detail.project.children.filter((c) => c && c.id) : [];
  if (!children.length) return null;
  const wrap = h('div', { class: 'agr__project' });
  wrap.appendChild(h('div', { class: 'agr__project-head' },
    h('span', { class: 'agr__project-title' }, 'По подзадачам'),
    h('span', { class: 'chip chip--muted tnum' },
      countNoun(children.length, ['подзадача', 'подзадачи', 'подзадач']))));
  const list = h('ul', { class: 'agr__project-list' });
  for (const child of children.slice(0, 8)) {
    const box = h('div', { class: 'agr__project-agents' });
    const toggle = h('button', {
      class: 'btn btn--ghost btn--sm agr__btn', type: 'button', 'aria-expanded': 'false',
      title: 'Показать результаты этой подзадачи',
    }, icon('chevron-right', { size: 12 }), h('span', null, 'Результаты'));
    let loaded = false;
    toggle.addEventListener('click', async () => {
      const expanded = toggle.getAttribute('aria-expanded') === 'true';
      toggle.setAttribute('aria-expanded', expanded ? 'false' : 'true');
      box.hidden = expanded;
      if (expanded || loaded) return;
      loaded = true;
      box.replaceChildren(h('div', { class: 'small muted' }, 'Читаю результаты…'));
      try {
        const res = await ctx.api.get('/api/requests/' + encodeURIComponent(child.id) + '/agent-results');
        const rows = (res && Array.isArray(res.agents)) ? res.agents : [];
        if (!rows.length) {
          box.replaceChildren(h('div', { class: 'small muted' }, 'Работа ещё не сдана.'));
          return;
        }
        box.replaceChildren(...rows.slice(0, 12).map((row) => h('div', { class: 'agr__mini' },
          h('span', { class: 'agr__mini-who' }, agentName(row, null).text),
          h('span', { class: 'agr__mini-text' }, String(row.verdict || row.title || '')),
          h('span', { class: 'chip chip--muted tnum' },
            countNoun(Number(row.files_total || 0), ['файл', 'файла', 'файлов'])))));
      } catch (err) {
        box.replaceChildren(h('div', { class: 'small muted' },
          (err && err.message) || 'Не удалось прочитать результаты подзадачи.'));
      }
    });
    box.hidden = true;
    list.appendChild(h('li', { class: 'agr__project-row' },
      h('a', {
        class: 'agr__project-link',
        href: '#/room/' + encodeURIComponent(roomId) + '/request/' + encodeURIComponent(child.id),
      }, String(child.title || child.id)),
      h('span', { class: 'chip chip--muted' }, STAGE_RU[String(child.stage || '')] || 'В работе'),
      toggle,
      box));
  }
  wrap.appendChild(list);
  if (children.length > 8) {
    wrap.appendChild(h('div', { class: 'small muted' },
      'и ещё ' + countNoun(children.length - 8, ['подзадача', 'подзадачи', 'подзадач'])));
  }
  return wrap;
}

/**
 * Построить блок «Результаты агентов».
 *
 * @param {object} detail ответ `GET /api/requests/{id}`
 * @param {object} ctx    контекст страницы (`api`, `toast`, …)
 * @param {{room?: object, roomId?: string, defaultOpen?: boolean}} [opts]
 * @returns {HTMLElement|null}
 */
export function renderAgentResults(detail, ctx, opts = {}) {
  if (!detail || typeof detail !== 'object') return null;
  const o = opts || {};
  const payload = (detail.agent_results && typeof detail.agent_results === 'object')
    ? detail.agent_results : { agents: [] };
  const agents = Array.isArray(payload.agents) ? payload.agents.filter((a) => a && typeof a === 'object') : [];
  const roomId = String(detail.room || o.roomId || '').trim();
  const requestId = String(detail.id || '').trim();
  const room = o.room || null;
  const counts = (payload.counts && typeof payload.counts === 'object') ? payload.counts : {};

  const chips = [];
  if (agents.length) {
    chips.push({ text: countNoun(agents.length, ['агент', 'агента', 'агентов']), tone: 'muted' });
  }
  const filesCount = Number(counts.files || 0);
  if (filesCount > 0) chips.push({ text: countNoun(filesCount, ['файл', 'файла', 'файлов']), tone: 'muted' });
  const leftCount = Number(counts.remaining || 0);
  if (leftCount > 0) chips.push({ text: 'что осталось: ' + leftCount, tone: 'info' });

  let filter = 'all';
  const list = h('div', { class: 'agr__list' });
  const filterBar = h('div', { class: 'agr__filters', role: 'group', 'aria-label': 'Какие результаты показывать' });
  const filterButtons = new Map();

  function matches(row) {
    if (filter === 'files') return Array.isArray(row.files) && row.files.length > 0;
    if (filter === 'left') return Boolean(row.remaining);
    return true;
  }

  function fill() {
    const rows = agents.filter(matches);
    list.replaceChildren();
    if (!rows.length) {
      list.appendChild(h('div', { class: 'agr__empty small' },
        agents.length
          ? 'По этому фильтру строк нет — выберите «Все».'
          : ''));
      return;
    }
    for (const row of rows) list.appendChild(agentRow(row, ctx, room, roomId, requestId));
  }

  for (const item of FILTERS) {
    const button = h('button', {
      class: 'btn btn--ghost btn--sm agr__btn agr__filter' + (item.id === filter ? ' is-active' : ''),
      type: 'button', 'aria-pressed': item.id === filter ? 'true' : 'false',
    }, h('span', null, item.label));
    filterButtons.set(item.id, button);
    button.addEventListener('click', () => {
      filter = item.id;
      for (const [id, node] of filterButtons) {
        node.classList.toggle('is-active', id === filter);
        node.setAttribute('aria-pressed', id === filter ? 'true' : 'false');
      }
      fill();
    });
    filterBar.appendChild(button);
  }

  function buildContent() {
    const wrap = h('div', { class: 'agr' });
    const project = projectBlock(detail, ctx, roomId);
    if (project) wrap.appendChild(project);
    if (!agents.length) {
      wrap.appendChild(emptyState(detail));
      return wrap;
    }
    if (agents.length > 2) wrap.appendChild(filterBar);
    fill();
    wrap.appendChild(list);
    return wrap;
  }

  const sec = createSection({
    key: 'agent_results',
    requestId,
    title: 'Результаты агентов',
    hint: 'Кто что сделал, какой вывод, где лежит файл и что осталось',
    defaultOpen: Boolean(o.defaultOpen),
    className: 'agr-section',
    attrs: { 'data-rp': 'agent-results' },
    summary: chips,
    content: buildContent,
  });
  return sec.element;
}

export default renderAgentResults;
