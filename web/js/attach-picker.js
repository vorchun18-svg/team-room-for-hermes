/**
 * attach-picker.js — ОДИН компонент «Прикрепить файлы» (W30-F, дефект 15).
 *
 * Зачем общий компонент
 * ---------------------
 * Скрепка жила в двух местах по-разному: в композере открывалось системное окно
 * Windows (`POST /api/files/pick`), а в «Диалоге по запросу» — диалог «Путь к
 * файлу» с ручным вводом, лимитом 10 и без проверки секретов. Здесь одна
 * реализация: ею пользуются композер, диалог запроса и любое следующее место,
 * где человек прикрепляет файл.
 *
 * Что важно человеку
 * -------------------
 *  • Нажал скрепку → открылось окно выбора файлов, можно взять несколько сразу.
 *  • Прикрепляется ПУТЬ, а не копия: файл не загружается и не копируется, агент
 *    сам берёт его там, где он лежит. Никакой отправки содержимого здесь нет.
 *  • У каждого чипа: имя, размер, «Просмотр», «Открыть», «Открыть в папке», «Убрать».
 *  • Файл-секрет (`.env`, ключ, токен) не теряется: причина на чипе и кнопка
 *    «Всё равно прикрепить» — решает человек.
 *  • Предел — 30 путей (как `app_common.MAX_ATTACH` на сервере); лишние не
 *    теряются молча: компонент говорит, сколько взял.
 *  • Ручной ввод пути — только скрытая ссылка «вставить путь» внутри компонента
 *    (запасной путь, когда окно недоступно: не-Windows, окно закрыли).
 *
 * Совместимость классов
 * ---------------------
 * Узлы получают общий класс (`attach-chip`, `attach-chip__name`, …) и класс с
 * префиксом вызывающего (`cmp-attach__item`, `thr-attach__item`). Префикс
 * сохраняет прежнюю вёрстку композера и стили диалога запроса.
 *
 * @example
 *   const picker = createAttachPicker({ api, toast, prefix: 'cmp-attach' });
 *   bar.appendChild(picker.button);
 *   form.appendChild(picker.chipsHost);
 *   form.appendChild(picker.row);
 *   picker.paths();   // ['C:/…/смета.xlsx', …] — то, что уходит на сервер
 */

import { h } from './util.js';
import { icon } from './icons.js';
import { ensureCss } from './ui.js';
import { openAttachPreview } from './views/attach-preview.js';

/** Предел вложений — тот же, что у сервера (`app_common.MAX_ATTACH`). */
export const MAX_ATTACH = 30;

/** Имя файла из пути: последний кусок после «/» или «\». */
export function baseName(path) {
  const parts = String(path == null ? '' : path).split(/[\\/]/);
  return parts[parts.length - 1] || String(path == null ? '' : path);
}

/** Размер по-человечески: 940 Б, 12,4 КБ, 3 МБ. */
export function sizeText(bytes) {
  const n = Number(bytes);
  if (!Number.isFinite(n) || n < 0) return '';
  if (n < 1024) return Math.round(n) + ' Б';
  const units = ['КБ', 'МБ', 'ГБ'];
  let value = n / 1024;
  let i = 0;
  while (value >= 1024 && i < units.length - 1) { value /= 1024; i += 1; }
  const text = value >= 10 ? Math.round(value) : Math.round(value * 10) / 10;
  return String(text).replace('.', ',') + ' ' + units[i];
}

/** Пути из поля: по одному на строку, пустые строки пропускаем. */
export function splitPaths(text) {
  const src = String(text == null ? '' : text);
  const lines = [];
  let line = '';
  for (const ch of src) {
    const code = ch.codePointAt(0);
    if (code === 10 || code === 13) {
      if (line.trim()) lines.push(line.trim());
      line = '';
      continue;
    }
    line += ch;
  }
  if (line.trim()) lines.push(line.trim());
  return lines;
}

/** Один и тот же файл, записанный по-разному (`C:\a\b` и `C:/a/b`). */
export function samePath(a, b) {
  const norm = (p) => String(p == null ? '' : p).trim().replace(/\\/g, '/').replace(/\/+$/, '').toLowerCase();
  return norm(a) === norm(b);
}

/** Новый чип вложения в состоянии «проверяю». */
function attachItem(path) {
  return {
    path, name: baseName(path), size: null, ext: '',
    ok: null, code: 'checking', message: '', secret: false, confirmed: false,
  };
}

/**
 * Создать компонент прикрепления файлов.
 *
 * @param {object} opts
 *   `api` — клиент приложения (`ctx.api`); `toast(message, kind)` — сообщения;
 *   `prefix` — префикс классов (`cmp-attach`, `thr-attach`); `title` — заголовок
 *   окна выбора файлов; `label` — подпись на кнопке ('' — только иконка);
 *   `buttonClass` — классы кнопки-скрепки; `max` — предел (по умолчанию 30);
 *   `onChange(items, picker)` — после каждого изменения (черновик, синхронизация);
 *   `onInsert(text)` — «вставить расшифровку» из предпросмотра.
 * @returns {object} узлы и действия компонента
 */
export function createAttachPicker(opts = {}) {
  const o = opts || {};
  const api = o.api;
  const toast = typeof o.toast === 'function' ? o.toast : () => {};
  const prefix = String(o.prefix || 'attach');
  const max = Math.max(1, Number(o.max) || MAX_ATTACH);
  const dialogTitle = String(o.title || 'Прикрепить файлы');
  const cls = (suffix) => prefix + '__' + suffix;

  ensureCss('css/attach-picker.css');

  const state = { items: [], picking: false, rowOpen: false };

  /* ─────────────────────────────────────────────────────────────── узлы */

  const button = h('button', {
    class: o.buttonClass || 'btn btn--ghost btn--sm',
    type: 'button',
    title: o.buttonTitle || 'Выбрать файлы в окне Windows — можно несколько сразу',
    'aria-label': 'Прикрепить файлы',
    'aria-expanded': 'false',
  }, icon('attach', { size: 14 }),
  o.label === '' ? null : h('span', { class: 'attach-picker__label ' + cls('btn-label') },
    o.label || 'Прикрепить файлы'));

  const chipsHost = h('div', { class: 'attach-chips ' + cls('chips') });

  const pathInput = h('textarea', {
    class: 'input attach-row__input ' + cls('path'), rows: 2,
    placeholder: 'C:/Проекты/Витрина/смета.xlsx — или несколько путей, по одному на строку',
    'aria-label': 'Путь к файлу', 'data-mic': 'off', spellcheck: 'false',
  });

  const pathAdd = h('button', { class: 'btn btn--secondary btn--sm', type: 'button' }, 'Прикрепить путь');

  const row = h('div', { class: 'attach-row hidden ' + cls('row') },
    h('div', { class: 'row attach-row__line ' + cls('row-line') }, pathInput, pathAdd),
    h('div', { class: 'field__hint' },
      'Запасной путь: вставьте один или несколько путей — по одному на строку. '
      + 'Скрепка открывает окно выбора файлов Windows.'));

  const toggle = h('button', {
    class: 'btn btn--ghost btn--sm attach-picker__path-btn ' + cls('path-btn'),
    type: 'button',
    title: 'Вставить путь к файлу вручную (несколько — по одному на строку)',
    'aria-label': 'Вставить путь к файлу',
  }, 'Путь');

  /* ─────────────────────────────────────────────────────── состояние */

  function changed() {
    if (typeof o.onChange === 'function') {
      try { o.onChange(state.items.slice(), api_picker); } catch { /* необязательно */ }
    }
  }

  function render() {
    chipsHost.replaceChildren();
    chipsHost.classList.toggle('hidden', !state.items.length);
    for (const item of state.items) chipsHost.appendChild(chipNode(item));
  }

  function chipNode(item) {
    const bad = item.ok === false;
    const chip = h('div', {
      class: 'attach-chip ' + cls('item') + (bad ? ' attach-chip--bad ' + cls('item--bad') : ''),
      'data-path': item.path,
    });
    chip.appendChild(h('span', {
      class: 'attach-chip__name ' + cls('name'), title: item.path,
    }, item.name || baseName(item.path)));
    const size = item.size == null ? '' : sizeText(item.size);
    if (size) chip.appendChild(h('span', { class: 'attach-chip__size tnum ' + cls('size') }, size));
    if (item.ok === null) {
      chip.appendChild(h('span', { class: 'attach-chip__state ' + cls('state') }, 'проверяю…'));
    } else if (bad) {
      chip.appendChild(h('span', {
        class: 'attach-chip__why ' + cls('why'), title: item.message,
      }, item.message || 'Файл недоступен'));
    }
    const actions = h('div', { class: 'attach-chip__actions ' + cls('actions') });
    if (bad && item.secret) {
      actions.appendChild(h('button', {
        class: 'btn btn--secondary btn--sm attach-chip__act ' + cls('act'), type: 'button',
        title: 'Прикрепить, хотя файл похож на секрет — вы решаете сами',
        onclick: () => confirmSecret(item),
      }, 'Всё равно прикрепить'));
    }
    if (!bad) {
      actions.appendChild(h('button', {
        class: 'btn btn--ghost btn--sm attach-chip__act ' + cls('act'), type: 'button',
        title: 'Посмотреть «' + item.name + '» здесь, не открывая программу',
        onclick: () => previewAttach(item),
      }, icon('eye', { size: 12 }), 'Просмотр'));
      actions.appendChild(attachAction(item, 'open', 'Открыть',
        'Открыть «' + item.name + '» программой по умолчанию'));
      actions.appendChild(attachAction(item, 'reveal', 'Открыть в папке',
        'Показать «' + item.name + '» в проводнике'));
    }
    actions.appendChild(h('button', {
      class: 'btn btn--ghost btn--sm attach-chip__act ' + cls('act'), type: 'button',
      title: 'Убрать «' + item.path + '»', 'aria-label': 'Убрать «' + item.path + '»',
      onclick: () => removePath(item.path),
    }, icon('close', { size: 12 }), 'Убрать'));
    chip.appendChild(actions);
    return chip;
  }

  function attachAction(item, mode, label, title) {
    return h('button', {
      class: 'btn btn--ghost btn--sm attach-chip__act ' + cls('act'), type: 'button',
      title,
      onclick: () => { openAttach(item, mode); },
    }, label);
  }

  /* ─────────────────────────────────────────────────────── действия */

  /** «Открыть» / «Открыть в папке» у чипа: действие делает сервер. */
  async function openAttach(item, mode) {
    try {
      const res = await api.post('/api/files/attach-open', { path: item.path, mode });
      if (res && res.note) toast(res.note, 'info');
    } catch (err) {
      toast(err && err.message ? err.message : 'Не удалось открыть файл', 'danger');
    }
  }

  /** «Просмотр» чипа (W29B-Q3): содержимое файла без внешней программы. */
  function previewAttach(item) {
    openAttachPreview({
      path: item.path,
      name: item.name || baseName(item.path),
      api,
      onInsert: typeof o.onInsert === 'function' ? o.onInsert : undefined,
    }).catch((err) => {
      toast((err && err.message) || 'Не удалось открыть предпросмотр', 'danger');
    });
  }

  /**
   * Добавить пути (из окна выбора или из поля) и сразу проверить их на сервере.
   * Ничего не блокируем по папкам и расширениям: проверяем только «файл есть,
   * это файл, читается» (POST /api/files/attach-check).
   */
  async function add(paths) {
    const fresh = [];
    for (const raw of (Array.isArray(paths) ? paths : [paths])) {
      const path = String(raw == null ? '' : raw).trim();
      if (!path) continue;
      if (state.items.some((item) => samePath(item.path, path))) {
        toast('Этот путь уже прикреплён', 'info');
        continue;
      }
      if (state.items.length + fresh.length >= max) {
        toast('Не больше ' + max + ' вложений — уберите лишние', 'warn');
        break;
      }
      if (path.length > 500) { toast('Слишком длинный путь', 'warn'); continue; }
      fresh.push(attachItem(path));
    }
    if (!fresh.length) return;
    // Чипы видны всегда: блок пути скрыт, но прикреплённые файлы человек видит.
    // `rowOnAdd:false` — компактный диалог: поле пути раскрывается только по кнопке
    // «Путь» или когда окно не открылось (тупика нет).
    if (o.rowOnAdd !== false) showRow(true);
    state.items.push(...fresh);
    render(); changed();
    await checkPaths(fresh);
  }

  /**
   * Проверка вложений через сервер: имя, размер, «похоже на секрет», причина
   * отказа. Старый сервер без ручки не мешает прикреплять: чип остаётся с именем.
   */
  async function checkPaths(items) {
    const list = (items || []).filter(Boolean);
    if (!list.length) return;
    const confirmedSecret = list.some((item) => item.secret && item.confirmed);
    let data = null;
    try {
      data = await api.post('/api/files/attach-check', {
        attach: list.map((item) => item.path),
        confirm_secret: confirmedSecret,
      });
    } catch (err) {
      for (const item of list) { item.ok = true; item.code = 'ok'; item.message = ''; }
      render(); changed();
      if (err && err.status && err.status !== 404) {
        toast(err.message || 'Не удалось проверить файлы', 'warn');
      }
      return;
    }
    const rows = data && Array.isArray(data.items) ? data.items : [];
    rows.forEach((row, index) => {
      const item = list[index];
      if (!item || !row) return;
      if (row.path) item.path = String(row.path);
      item.name = row.name || item.name || baseName(item.path);
      item.size = row.size == null ? item.size : Number(row.size);
      item.ext = row.ext || item.ext;
      item.secret = row.secret === true;
      item.code = String(row.code || (row.ok ? 'ok' : 'error'));
      item.message = String(row.message || '');
      // Секрет считаем принятым только после явного «Всё равно прикрепить».
      item.ok = row.ok === true || (item.secret && item.confirmed);
    });
    render(); changed();
    const broken = list.find((item) => item.ok === false && !item.secret);
    if (broken) toast(broken.message || 'Файл не удалось приложить — уберите его', 'warn');
  }

  /** «Всё равно прикрепить»: человек сам решил — секрет принимается. */
  function confirmSecret(item) {
    item.confirmed = true;
    item.ok = true;
    item.code = 'ok';
    item.message = '';
    render(); changed();
    toast('«' + item.name + '» будет прикреплён', 'info');
  }

  function removePath(path) {
    state.items = state.items.filter((item) => !samePath(item.path, path));
    render(); changed();
  }

  /** Заменить весь список (восстановление черновика, ответ сервера). */
  async function set(paths, config = {}) {
    const list = Array.isArray(paths) ? paths : [];
    const confirmed = new Set((config && Array.isArray(config.confirmed) ? config.confirmed : [])
      .map((p) => String(p == null ? '' : p).trim().toLowerCase()));
    const items = [];
    for (const raw of list.slice(0, max)) {
      const path = String(raw == null ? '' : raw).trim();
      if (!path) continue;
      if (items.some((item) => samePath(item.path, path))) continue;
      const item = attachItem(path);
      if (confirmed.has(path.toLowerCase())) { item.confirmed = true; item.ok = true; item.code = 'ok'; }
      items.push(item);
    }
    state.items = items;
    render(); changed();
    const unchecked = items.filter((item) => item.ok === null);
    if (unchecked.length) await checkPaths(unchecked);
  }

  function clear() {
    state.items = [];
    render(); changed();
  }

  function showRow(show) {
    state.rowOpen = show === undefined ? !state.rowOpen : Boolean(show);
    row.classList.toggle('hidden', !state.rowOpen);
    button.setAttribute('aria-expanded', state.rowOpen ? 'true' : 'false');
    if (state.rowOpen) { try { pathInput.focus(); } catch { /* необязательно */ } }
  }

  /** Скрестка: системное окно выбора файлов Windows (POST /api/files/pick). */
  async function open() {
    if (state.picking) return;
    state.picking = true;
    button.disabled = true;
    button.setAttribute('aria-busy', 'true');
    const label = button.querySelector('.attach-picker__label') || button.querySelector('.' + cls('btn-label'));
    const before = label ? label.textContent : '';
    if (label) label.textContent = 'Окно выбора…';
    try {
      const res = await api.post('/api/files/pick', { title: dialogTitle });
      if (res && Number(res.dropped) > 0) {
        toast('Взял первые ' + max + ' из ' + res.total + ' — остальные прикрепите следующим разом', 'warn');
      }
      if (res && Array.isArray(res.paths) && res.paths.length) await add(res.paths);
      else if (res && res.cancelled) toast('Выбор файлов отменён', 'info');
    } catch (err) {
      // Тупика нет: причина названа, поле пути раскрыто как запасной путь.
      toast(err && err.message ? err.message : 'Не удалось открыть окно выбора файлов', 'warn');
      showRow(true);
    } finally {
      state.picking = false;
      button.disabled = false;
      button.removeAttribute('aria-busy');
      if (label) label.textContent = before || (o.label || 'Прикрепить файлы');
    }
  }

  button.addEventListener('click', () => { open(); });
  toggle.addEventListener('click', () => { showRow(); });

  pathAdd.addEventListener('click', () => {
    const lines = splitPaths(pathInput.value);
    if (!lines.length) { toast('Вставьте путь к файлу', 'info'); pathInput.focus(); return; }
    pathInput.value = '';
    add(lines);
  });
  pathInput.addEventListener('keydown', (ev) => {
    // Enter прикрепляет набранные строки; Shift+Enter — обычный перенос строки.
    if (ev.key === 'Enter' && !ev.shiftKey) {
      ev.preventDefault();
      const lines = splitPaths(pathInput.value);
      if (lines.length) { pathInput.value = ''; add(lines); }
      return;
    }
    if (ev.key === 'Escape') showRow(false);
  });

  const api_picker = {
    button, chipsHost, row, toggle, pathInput,
    /** Все прикреплённые пути (как они уйдут на сервер). */
    paths: () => state.items.map((item) => item.path),
    /** Только принятые пути: непригодные и неподтверждённые секреты сюда не входят. */
    goodPaths: () => state.items.filter((item) => item.ok !== false).map((item) => item.path),
    /** Пути, подтверждённые как «секрет, но нужен». */
    confirmedPaths: () => state.items.filter((item) => item.secret && item.confirmed).map((item) => item.path),
    /** Есть ли файл, который нельзя отправить (причина уже видна на чипе). */
    blocked: () => state.items.find((item) => item.ok === false) || null,
    items: () => state.items.slice(),
    count: () => state.items.length,
    add, set, clear, open,
    showRow,
    isRowOpen: () => state.rowOpen,
    isPicking: () => state.picking,
    recheck: () => checkPaths(state.items.slice()),
    render,
  };
  return api_picker;
}

export default createAttachPicker;
