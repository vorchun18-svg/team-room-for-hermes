/**
 * settings-dictation.js — раздел «Диктовка и словарь» на странице настроек.
 *
 * Простыми словами: Whisper сам не обучается, но словарь делает две вещи —
 * подсказывает слова заранее и исправляет частые ошибки после распознавания.
 * Здесь термины, автозамены, проверка на фразе и перенос словаря файлом.
 * Чистая логика (проверка файла, применение замен, подписи) — в
 * dictation-dictionary.js, её видно в node-тестах.
 */

import { h, plural, plainTerms } from '../util.js';
import { icon } from '../icons.js';
import { ApiError } from '../api.js';
import {
  ensureCss, sectionHeader, skeleton, errorBanner, emptyState, iconButton, confirmDialog,
} from '../ui.js';
import {
  MAX_REPLACEMENTS, MAX_TERMS, MAX_WORD, applyReplacements, cleanTerm, describeStatus,
  deviceRu, normalizeDictionary, sameWord, validateDictionaryFile,
} from '../dictation-dictionary.js';

const VIEW_CSS = 'css/view-dictation.css';

/* --------------------------------------------------------------- запросы */

async function readJson(res) {
  const type = res.headers && res.headers.get ? res.headers.get('Content-Type') || '' : '';
  if (!type.includes('json')) return (await res.text().catch(() => '')) || null;
  return res.json().catch(() => null);
}

function errorFrom(data, status) {
  const e = data && typeof data === 'object' ? (data.error || data) : null;
  const message = e && e.message ? plainTerms(e.message) : 'Сервер ответил ' + status;
  return new ApiError(e && e.code ? String(e.code) : 'http_' + status, message, status);
}

/** PUT /api/stt/dictionary — в api.js метода PUT нет, поэтому свой запрос. */
async function putDictionary(body) {
  let res;
  try {
    res = await fetch('/api/stt/dictionary', {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json', Accept: 'application/json' },
      body: JSON.stringify(body),
      credentials: 'same-origin',
      cache: 'no-store',
    });
  } catch {
    throw new ApiError('network', 'Нет связи с приложением', 0);
  }
  const data = await readJson(res);
  if (!res.ok) throw errorFrom(data, res.status);
  return data;
}

/* ------------------------------------------------------------------ вьюха */

/**
 * Смонтировать раздел «Диктовка и словарь».
 * @param {HTMLElement} container
 * @param {{api?: object, toast?: Function}} [ctx]
 * @returns {{unmount: Function}}
 */
export async function mountDictationSettings(container, ctx = {}) {
  const doc = globalThis.document;
  const host = container || (doc && doc.body);
  if (!host || typeof host.appendChild !== 'function') return { unmount() {} };

  await ensureCss(VIEW_CSS);

  const api = ctx && ctx.api ? ctx.api : null;
  const toast = ctx && typeof ctx.toast === 'function' ? ctx.toast : () => {};
  // id не свой: страница настроек сама даёт хосту id="set-dictation".
  const el = h('section', { class: 'dict' });
  host.appendChild(el);

  const state = {
    terms: [], replacements: [], updated_at: 0, status: null, statusError: null,
    error: null, loading: true, saving: false, editing: -1, query: '',
    preview: null, imported: null, focusTerm: null,
  };
  let alive = true;
  let alerts = null;
  let statusBox = null;
  let termsBox = null;
  let replBox = null;
  let importBox = null;
  let termError = null;
  let paintReplacements = () => {};

  const stop = () => !alive;

  /* --------------------------------------------------------------- данные */

  async function getJson(path) {
    if (api && typeof api.get === 'function') {
      try {
        return await api.get(path);
      } catch (err) {
        throw err instanceof ApiError ? err : new ApiError('unknown', String(err && err.message), 0);
      }
    }
    let res;
    try {
      res = await fetch(path, { headers: { Accept: 'application/json' }, credentials: 'same-origin', cache: 'no-store' });
    } catch {
      throw new ApiError('network', 'Нет связи с приложением', 0);
    }
    const data = await readJson(res);
    if (!res.ok) throw errorFrom(data, res.status);
    return data;
  }

  async function refreshStatus() {
    try {
      state.status = await getJson('/api/stt/status');
      state.statusError = null;
    } catch (err) {
      state.statusError = err;
    }
    if (!stop()) paintStatus();
  }

  async function load() {
    state.loading = true;
    state.error = null;
    render();
    try {
      const dict = normalizeDictionary(await getJson('/api/stt/dictionary'));
      state.terms = dict.terms;
      state.replacements = dict.replacements;
      state.updated_at = dict.updated_at;
      state.loading = false;
    } catch (err) {
      state.loading = false;
      state.error = err;
      render();
      return;
    }
    if (stop()) return;
    render();
    await refreshStatus();
  }

  /* ------------------------------------------------------------- сохранить */

  const snapshot = () => ({
    terms: state.terms.slice(),
    replacements: state.replacements.map((r) => ({ ...r })),
    updated_at: state.updated_at,
  });

  async function commit(next, okText) {
    if (state.saving) return false;
    const prev = snapshot();
    state.saving = true;
    state.terms = next.terms.slice();
    state.replacements = next.replacements.map((r) => ({ ...r }));
    state.editing = -1;
    state.query = '';
    render();
    try {
      const res = await putDictionary({ terms: state.terms, replacements: state.replacements });
      if (res && typeof res === 'object' && Array.isArray(res.terms)) {
        const saved = normalizeDictionary(res);
        state.terms = saved.terms;
        state.replacements = saved.replacements;
        state.updated_at = saved.updated_at || state.updated_at;
      }
      state.saving = false;
      if (!stop()) render();
      if (okText) toast(okText, 'ok');
      return true;
    } catch (err) {
      state.saving = false;
      state.terms = prev.terms;
      state.replacements = prev.replacements;
      state.updated_at = prev.updated_at;
      if (!stop()) {
        render();
        if (alerts) {
          const msg = err && err.code === 'network'
            ? 'Нет связи с приложением. Изменение не сохранилось — словарь остался прежним.'
            : 'Не удалось сохранить словарь' + (err && err.message ? ': ' + err.message : '') + '. Изменение отменено.';
          alerts.appendChild(errorBanner({ message: msg, code: '' }, () => {
            alerts.textContent = '';
            commit(next, okText);
          }));
        }
      }
      return false;
    }
  }

  /* ----------------------------------------------------------------- слова */

  function termsCounter() {
    const total = state.terms.length;
    if (!state.query.trim()) return `${total} ${plural(total, ['слово', 'слова', 'слов'])}`;
    const q = state.query.trim().toLowerCase().replace(/ё/g, 'е');
    const n = state.terms.filter((t) => t.toLowerCase().replace(/ё/g, 'е').includes(q)).length;
    return `Найдено ${n} из ${total}`;
  }

  function termChips() {
    const q = state.query.trim().toLowerCase().replace(/ё/g, 'е');
    if (!state.terms.length && !q) {
      return emptyState({
        icon: 'sparkle',
        title: 'Словарь пока пуст',
        text: 'Добавьте имена и термины, в которых Whisper путается: «Гранит», «Атлас», «TaskFlow», «моти», «ЕГРЮЛ», названия фирм. Дальше система предложит запоминать исправления сама.',
        action: state.focusTerm ? { label: 'Добавить первое слово', variant: 'primary', onclick: state.focusTerm } : null,
      });
    }
    const list = q ? state.terms.filter((t) => t.toLowerCase().replace(/ё/g, 'е').includes(q)) : state.terms;
    const box = h('div', { class: 'dict-chips' });
    if (!list.length) {
      box.appendChild(h('div', { class: 'dict-chips__none muted small' },
        'Ничего не нашлось. Проверьте написание или добавьте слово.'));
      return box;
    }
    for (const term of list) {
      const x = h('button', {
        class: 'dict-chip__x', type: 'button', title: 'Убрать из словаря',
        'aria-label': 'Убрать «' + term + '»',
      }, '×');
      x.addEventListener('click', () => {
        commit({ terms: state.terms.filter((t) => t !== term), replacements: state.replacements },
          'Убрали «' + term + '»');
      });
      box.appendChild(h('span', { class: 'dict-chip' }, h('span', { class: 'dict-chip__text' }, term), x));
    }
    return box;
  }

  /* ------------------------------------------------------------ автозамены */

  function replacementRow(r, idx) {
    if (state.editing === idx) {
      const from = h('input', { class: 'input input--sm dict-cell-input', type: 'text', maxlength: String(MAX_WORD) });
      const to = h('input', { class: 'input input--sm dict-cell-input', type: 'text', maxlength: String(MAX_WORD) });
      from.value = r.from;
      to.value = r.to;
      const err = h('div', { class: 'dict-row-error small', hidden: true });
      const save = h('button', { class: 'btn btn--sm btn--primary', type: 'button' }, 'Сохранить');
      const cancel = h('button', { class: 'btn btn--sm btn--ghost', type: 'button' }, 'Отмена');
      save.addEventListener('click', () => {
        const f = cleanTerm(from.value);
        const t = cleanTerm(to.value);
        const bad = !f || !t ? 'Заполните оба поля.'
          : f === t ? 'Слева и справа одно и то же.'
            : state.replacements.some((o, i) => i !== idx && sameWord(o.from, f)) ? 'Такая пара уже есть в списке.' : '';
        if (bad) { err.removeAttribute('hidden'); err.textContent = bad; return; }
        commit({
          terms: state.terms,
          replacements: state.replacements.map((o, i) => (i === idx ? { ...o, from: f, to: t } : o)),
        }, 'Исправили автозамену');
      });
      cancel.addEventListener('click', () => { state.editing = -1; paintReplacements(); });
      return h('tr', { class: 'dict-row dict-row--editing' },
        h('td', {}, from),
        h('td', { class: 'dict-cell-arrow' }, '→'),
        h('td', {}, to),
        h('td', { class: 'num tnum' }, r.count ? String(r.count) : '—'),
        h('td', { class: 'dict-cell-actions' }, h('div', { class: 'dict-cell-actions__box' }, save, cancel, err)));
    }

    const edit = iconButton({
      icon: 'edit', size: 'sm', title: 'Изменить автозамену',
      onclick: () => { state.editing = idx; paintReplacements(); },
    });
    const del = iconButton({
      icon: 'trash', size: 'sm', title: 'Удалить автозамену',
      onclick: async () => {
        const ok = await confirmDialog({
          title: 'Удалить автозамену?',
          text: '«' + r.from + '» → «' + r.to + '» больше не будет исправляться автоматически.',
          confirmLabel: 'Удалить',
          danger: true,
        });
        if (!ok || stop()) return;
        commit({ terms: state.terms, replacements: state.replacements.filter((o) => o !== r) },
          'Убрали автозамену');
      },
    });
    return h('tr', { class: 'dict-row' },
      h('td', { class: 'dict-cell-from' }, r.from),
      h('td', { class: 'dict-cell-arrow', 'aria-hidden': 'true' }, '→'),
      h('td', { class: 'dict-cell-to' }, r.to),
      h('td', { class: 'num tnum', title: 'Сколько раз автозамена сработала' }, r.count ? String(r.count) : '—'),
      h('td', { class: 'dict-cell-actions' }, h('div', { class: 'dict-cell-actions__box' }, edit, del)));
  }

  function replacementsBlock() {
    const wrap = h('div', { class: 'dict-repl' });
    if (!state.replacements.length) {
      wrap.appendChild(emptyState({
        icon: 'arrow-swap',
        title: 'Автозамен пока нет',
        text: 'Они появляются сами: когда вы поправите имя или термин после диктовки, система предложит запомнить пару. Можно добавить вручную — например «тескфло» → «TaskFlow».',
      }));
    } else {
      const body = h('tbody');
      state.replacements.forEach((r, i) => body.appendChild(replacementRow(r, i)));
      wrap.appendChild(h('div', { class: 'table-wrap dict-table-wrap' },
        h('table', { class: 'table dict-table' },
          h('thead', {}, h('tr', {},
            h('th', {}, 'Как распознал'),
            h('th', { class: 'dict-th-arrow', 'aria-label': 'Станет' }, ''),
            h('th', {}, 'Как должно быть'),
            h('th', { class: 'num' }, 'Сработало'),
            h('th', { class: 'dict-th-actions', 'aria-label': 'Действия' }, ''))),
          body)));
    }

    const from = h('input', {
      class: 'input dict-add__field', type: 'text', maxlength: String(MAX_WORD),
      placeholder: 'тескфло', 'aria-label': 'Как распознаёт',
    });
    const to = h('input', {
      class: 'input dict-add__field', type: 'text', maxlength: String(MAX_WORD),
      placeholder: 'TaskFlow', 'aria-label': 'Как должно быть',
    });
    const err = h('div', { class: 'dict-add__error small', hidden: true, role: 'alert' });
    const add = h('button', { class: 'btn btn--sm btn--secondary', type: 'button' },
      icon('add', { size: 12 }), 'Добавить');
    add.addEventListener('click', () => {
      const f = cleanTerm(from.value);
      const t = cleanTerm(to.value);
      const bad = !f || !t ? 'Заполните оба поля: как распознал и как надо.'
        : f === t ? 'Слева и справа одно и то же.'
          : state.replacements.some((o) => sameWord(o.from, f)) ? 'Такая пара уже есть — измените её в списке.'
            : state.replacements.length >= MAX_REPLACEMENTS ? `Больше ${MAX_REPLACEMENTS} автозамен не поместится.` : '';
      if (bad) { err.removeAttribute('hidden'); err.textContent = bad; return; }
      err.setAttribute('hidden', '');
      commit({
        terms: state.terms,
        replacements: state.replacements.concat([{ from: f, to: t, count: 0, added_at: 0 }]),
      }, 'Добавили автозамену');
    });
    wrap.appendChild(h('div', { class: 'dict-add' },
      h('span', { class: 'dict-add__label small muted' }, 'Новая пара:'),
      h('div', { class: 'dict-add__row' },
        from, h('span', { class: 'dict-add__arrow', 'aria-hidden': 'true' }, '→'), to, add),
      err,
      h('div', { class: 'dict-add__hint small muted' },
        'Без учёта регистра, по целым словам. Регистр первой буквы сохраняется.')));
    return wrap;
  }

  /* --------------------------------------------------------- проверка фразы */

  function paintPreview(out) {
    if (!out) return;
    out.textContent = '';
    const p = state.preview;
    if (!p) return;
    const res = p.result;
    out.appendChild(h('div', { class: 'dict-preview__caption small muted' }, 'Получится:'));
    out.appendChild(h('div', { class: 'dict-preview__text' }, res.text));
    out.appendChild(h('div', { class: 'dict-preview__stat small' },
      res.count
        ? `Сработало замен: ${res.count}` + (res.hits.length > 1
          ? ' (' + res.hits.map((x) => '«' + x.from + '»').join(', ') + ')' : '')
        : 'Ни одна автозамена здесь не сработала — текст останется как есть.'));
  }

  function previewBlock() {
    const area = h('textarea', {
      class: 'textarea dict-preview__input', rows: 3, 'data-mic': '',
      placeholder: 'Например: отправь запрос недре по тескфло до пятницы',
      'aria-label': 'Фраза для проверки',
    });
    if (state.preview && state.preview.source) area.value = state.preview.source;
    const out = h('div', { class: 'dict-preview__out' });
    const btn = h('button', { class: 'btn btn--sm btn--secondary', type: 'button' }, 'Проверить на фразе');
    btn.addEventListener('click', () => {
      const src = String(area.value || '');
      if (!src.trim()) {
        out.textContent = '';
        out.appendChild(h('div', { class: 'dict-preview__empty muted small' },
          'Напишите или продиктуйте фразу — покажу, что получится.'));
        return;
      }
      state.preview = { source: src, result: applyReplacements(src, state.replacements) };
      paintPreview(out);
    });
    const box = h('div', { class: 'dict-preview' },
      area,
      h('div', { class: 'dict-preview__actions' }, btn,
        h('span', { class: 'small muted' }, 'Считаем здесь же, теми же правилами, что и при диктовке.')),
      out);
    if (state.preview) paintPreview(out);
    return box;
  }

  /* ----------------------------------------------------------------- файлы */

  function downloadDictionary() {
    const payload = {
      version: 1,
      kind: 'dictation-dictionary',
      updated_at: state.updated_at || Math.floor(Date.now() / 1000),
      terms: state.terms,
      replacements: state.replacements.map((r) => ({ from: r.from, to: r.to, count: r.count || 0 })),
    };
    const json = JSON.stringify(payload, null, 2);
    try {
      const url = URL.createObjectURL(new Blob([json], { type: 'application/json;charset=utf-8' }));
      const a = h('a', { href: url, download: 'словарь-диктовки.json' });
      a.style.display = 'none';
      (doc && doc.body ? doc.body : el).appendChild(a);
      a.click();
      setTimeout(() => { URL.revokeObjectURL(url); if (a.parentNode) a.parentNode.removeChild(a); }, 0);
      toast('Словарь скачан', 'ok');
    } catch {
      if (alerts) {
        alerts.textContent = '';
        alerts.appendChild(errorBanner({ message: 'Браузер не дал скачать файл. Скопируйте словарь из окна ниже.', code: '' }));
        alerts.appendChild(h('textarea', { class: 'textarea dict-export-fallback', rows: 6 }, json));
      }
    }
  }

  async function readFile(file) {
    if (!file) return '';
    if (typeof file.text === 'function') return file.text();
    return new Promise((resolve, reject) => {
      const reader = new FileReader();
      reader.onload = () => resolve(String(reader.result || ''));
      reader.onerror = () => reject(new Error('Не удалось прочитать файл'));
      reader.readAsText(file);
    }).catch(() => '');
  }

  async function importFile(file) {
    let parsed = null;
    try {
      parsed = JSON.parse(await readFile(file));
    } catch {
      state.imported = { error: 'Файл не похож на JSON: возможно, он повреждён или это другой файл.' };
      paintImport();
      return;
    }
    if (stop()) return;
    const check = validateDictionaryFile(parsed);
    state.imported = check.ok ? { value: check.value, error: null } : { error: check.error };
    paintImport();
  }

  function paintImport() {
    if (!importBox) return;
    importBox.textContent = '';
    importBox.setAttribute('hidden', '');
    const imp = state.imported;
    if (!imp) return;
    importBox.removeAttribute('hidden');
    if (imp.error) {
      importBox.appendChild(h('div', { class: 'banner banner--error dict-import__error', role: 'alert' },
        icon('warning', { size: 14 }), h('span', {}, imp.error)));
      const close = h('button', { class: 'btn btn--sm btn--ghost', type: 'button' }, 'Понятно');
      close.addEventListener('click', () => { state.imported = null; paintImport(); });
      importBox.appendChild(h('div', { class: 'dict-import__actions' }, close));
      return;
    }
    const v = imp.value;
    const repl = v.replacements.length;
    importBox.appendChild(h('div', { class: 'dict-import__title' }, 'Проверьте, что загружаем'));
    importBox.appendChild(h('div', { class: 'dict-import__text small' },
      `В файле ${v.terms.length} ${plural(v.terms.length, ['слово', 'слова', 'слов'])} и ${repl} ${plural(repl, ['автозамена', 'автозамены', 'автозамен'])}.`,
      ' Текущий словарь будет заменён целиком.'));
    const ok = h('button', { class: 'btn btn--sm btn--primary', type: 'button' }, 'Заменить словарь');
    ok.addEventListener('click', async () => {
      const done = await commit({ terms: v.terms, replacements: v.replacements }, 'Словарь загружен');
      if (done) { state.imported = null; paintImport(); }
    });
    const no = h('button', { class: 'btn btn--sm btn--ghost', type: 'button' }, 'Отмена');
    no.addEventListener('click', () => { state.imported = null; paintImport(); });
    importBox.appendChild(h('div', { class: 'dict-import__actions' }, ok, no));
  }

  /* ------------------------------------------------------------- состояние */

  function paintStatus() {
    if (!statusBox || stop()) return;
    statusBox.textContent = '';
    const meta = describeStatus(state.status);
    const st = state.status || {};
    statusBox.appendChild(h('div', { class: 'dict-status__head' },
      h('span', { class: 'chip ' + meta.tone },
        h('span', { class: 'chip__dot' + (st.loading ? ' chip__dot--pulse' : '') }), meta.label),
      iconButton({
        icon: 'refresh', size: 'sm', title: 'Проверить ещё раз',
        onclick: () => refreshStatus().then(() => toast('Проверили состояние распознавания', 'info')),
      })));
    const rows = h('dl', { class: 'dict-status__rows' });
    if (st.model) rows.appendChild(h('div', { class: 'dict-status__row' }, h('dt', {}, 'Модель'), h('dd', {}, String(st.model))));
    if (st.device) rows.appendChild(h('div', { class: 'dict-status__row' }, h('dt', {}, 'Считает'), h('dd', {}, deviceRu(st.device))));
    statusBox.appendChild(rows);
    statusBox.appendChild(h('p', { class: 'dict-status__hint small' }, meta.hint));
    if (state.statusError) {
      statusBox.appendChild(h('p', { class: 'dict-status__hint small muted' },
        state.statusError && state.statusError.code === 'network'
          ? 'Состояние не узнать: нет связи с приложением.'
          : 'Состояние не узнать: ' + (state.statusError.message || 'сервер не ответил') + '.'));
    }
  }

  /* ------------------------------------------------------------- разметка */

  function block(title, count, body, hint) {
    const sec = h('section', { class: 'dict-block' },
      h('div', { class: 'dict-block__head' },
        h('h3', { class: 'dict-block__title' }, title),
        h('span', { class: 'small muted dict-block__count' }, count || '')));
    if (hint) sec.appendChild(h('p', { class: 'dict-block__hint small muted' }, hint));
    sec.appendChild(body);
    return sec;
  }

  function termsBlock() {
    const termInput = h('input', {
      class: 'input dict-add__field', type: 'text', maxlength: String(MAX_WORD),
      'data-mic': '', autocomplete: 'off', placeholder: 'Например, ЕГРЮЛ',
      'aria-label': 'Новое слово или имя',
    });
    state.focusTerm = () => { if (typeof termInput.focus === 'function') termInput.focus(); };
    termError = h('div', { class: 'dict-add__error small', hidden: true, role: 'alert' });
    const addTerm = () => {
      const t = cleanTerm(termInput.value);
      termError.setAttribute('hidden', '');
      const bad = !t ? 'Введите слово или имя — например «Гранит».'
        : t.length > MAX_WORD ? `Слишком длинно: не больше ${MAX_WORD} знаков.`
          : state.terms.some((x) => sameWord(x, t)) ? 'Такое слово уже есть в словаре.'
            : state.terms.length >= MAX_TERMS ? `Больше ${MAX_TERMS} слов не поместится.` : '';
      if (bad) { termError.removeAttribute('hidden'); termError.textContent = bad; return; }
      commit({ terms: state.terms.concat([t]), replacements: state.replacements }, 'Добавили «' + t + '»');
    };
    const add = h('button', { class: 'btn btn--sm btn--secondary', type: 'button' },
      icon('add', { size: 12 }), 'Добавить');
    add.addEventListener('click', addTerm);
    termInput.addEventListener('keydown', (ev) => {
      if (ev.key === 'Enter' && !ev.isComposing) {
        if (typeof ev.preventDefault === 'function') ev.preventDefault();
        addTerm();
      }
    });

    const search = h('input', {
      class: 'input dict-search__input', type: 'search', placeholder: 'Найти слово',
      'aria-label': 'Найти слово в словаре',
    });
    search.value = state.query;
    const counter = h('span', { class: 'small muted dict-terms__counter' }, termsCounter());
    counter.hidden = !state.query.trim();
    search.addEventListener('input', () => {
      state.query = String(search.value || '');
      counter.textContent = termsCounter();
      counter.hidden = !state.query.trim();
      if (termsBox) { termsBox.textContent = ''; termsBox.appendChild(termChips()); }
    });

    termsBox = h('div', { class: 'dict-terms' });
    termsBox.appendChild(termChips());
    const inner = h('div', { class: 'dict-terms__inner' },
      h('div', { class: 'dict-add' },
        h('div', { class: 'dict-add__row' }, termInput, add),
        termError,
        h('div', { class: 'dict-add__hint small muted' },
          'Диктовать слово тоже можно — нажмите микрофон в поле. Термины уходят в подсказку модели перед распознаванием.')),
      h('div', { class: 'dict-terms__tools' },
        state.terms.length > 6 ? search : null,
        state.query.trim() ? counter : null),
      termsBox);
    return block('Слова и имена', '', inner,
      'Имена, названия фирм, термины. Пишите так, как должно быть в тексте: «Гранит», «Атлас», «TaskFlow», «моти», «ЕГРЮЛ».');
  }

  function filesBlock() {
    const fileInput = h('input', { type: 'file', accept: '.json,application/json', class: 'hidden', 'aria-hidden': 'true' });
    const pick = h('button', { class: 'btn btn--sm btn--secondary', type: 'button' },
      icon('cloud-upload', { size: 12 }), 'Загрузить словарь');
    pick.addEventListener('click', () => { if (typeof fileInput.click === 'function') fileInput.click(); });
    fileInput.addEventListener('change', () => importFile(fileInput.files && fileInput.files[0]));
    const dl = h('button', { class: 'btn btn--sm btn--secondary', type: 'button' },
      icon('file-text', { size: 12 }), 'Скачать словарь (.json)');
    dl.addEventListener('click', downloadDictionary);
    importBox = h('div', { class: 'dict-import', hidden: true });
    const body = h('div', { class: 'dict-files__inner' },
      h('div', { class: 'dict-files__row' }, dl, pick, fileInput),
      h('p', { class: 'small muted dict-files__hint' },
        'Один файл: можно сохранить копию, перенести на другой компьютер или отдать программисту. Ничего никуда не отправляется.'),
      importBox);
    if (state.imported) paintImport();
    return block('Файл словаря', '', body, '');
  }

  function render() {
    if (stop()) return;
    el.textContent = '';
    el.appendChild(sectionHeader('Диктовка и словарь', {
      actions: [h('span', { class: 'chip chip--muted dict__count' },
        state.terms.length + ' ' + plural(state.terms.length, ['слово', 'слова', 'слов']))],
    }));
    el.appendChild(h('p', { class: 'dict__lead' },
      'Whisper сам не обучается: модель не меняется от ваших правок. Зато мы подсказываем ей слова заранее — имена, фирмы и термины попадают в подсказку перед распознаванием. А частые ошибки исправляем сразу после — по списку автозамен, который растёт из ваших исправлений.'));

    alerts = h('div', { class: 'dict__alerts' });
    el.appendChild(alerts);
    if (state.loading) {
      el.appendChild(h('div', { class: 'dict__loading' }, skeleton(5)));
      return;
    }
    if (state.error) {
      el.appendChild(errorBanner(state.error, load));
      return;
    }

    statusBox = h('div', { class: 'dict-status card card--quiet' });
    el.appendChild(statusBox);
    paintStatus();
    el.appendChild(termsBlock());

    replBox = h('div', { class: 'dict-repl-box' });
    paintReplacements = () => {
      if (!replBox || stop()) return;
      replBox.textContent = '';
      replBox.appendChild(replacementsBlock());
    };
    paintReplacements();
    el.appendChild(block('Автозамены',
      state.replacements.length
        ? state.replacements.length + ' ' + plural(state.replacements.length, ['автозамена', 'автозамены', 'автозамен'])
        : '',
      replBox,
      'Что делать, если Whisper снова ошибётся в этом слове: заменить на правильное. Срабатывает после распознавания, до того как текст попадёт в поле.'));

    el.appendChild(block('Проверить на фразе', '', previewBlock(),
      'Вставьте фразу с ошибками — покажу, каким она станет после автозамен.'));
    el.appendChild(filesBlock());

    if (state.saving) el.setAttribute('aria-busy', 'true');
    else el.removeAttribute('aria-busy');
  }

  render();
  load();

  return {
    unmount() {
      alive = false;
      if (typeof el.remove === 'function') el.remove();
      else if (el.parentNode && typeof el.parentNode.removeChild === 'function') el.parentNode.removeChild(el);
    },
  };
}

export default mountDictationSettings;
