/**
 * second-opinion.js — «Второе мнение» на другой модели (W29B-Q4, п. 1).
 *
 * Что видит человек
 * -----------------
 * В карточке «Свод» (под текстом) — блок «Второе мнение»:
 *   • кнопка «Спросить другую модель» — открывает окно выбора;
 *   • уже заданные проверки: модель, когда, счётчик «2 не согласна · 1 не хватает»,
 *     пункты с цитатами и переход «к фрагменту» (подсветка в тексте свода).
 *
 * Окно выбора — честное: сверху «свод писал <модель>», по умолчанию выбрана ДРУГАЯ
 * модель из каталога (правило пакета), ниже — оценка расхода и предупреждение, если
 * каталог пришёл неполным. Запуск — только по кнопке: это один вызов модели.
 *
 * Данные берутся из `GET /api/requests/{id}/second-opinion` (сервер:
 * `server/second_opinion.py`). Новых обращений к модели при показе нет; пока идёт
 * проверка, блок перечитывает состояние раз в 6 секунд и перестаёт, когда ответил.
 *
 * Чистые помощники (`modelValue`, `pickerOptions`, `estimateLine`, `checkTitle`,
 * `verdictChip`, `itemText`) вынесены наружу — их проверяет node-тест.
 */

import { h } from '../util.js';
import { icon } from '../icons.js';
import { ensureCss, buildDialog, emptyState } from '../ui.js';
import { findQuote, plural } from '../svod-diff.js';

try { ensureCss('css/svod-diff.css'); } catch { /* без CSS блок всё равно читается */ }

/** Сколько проверок показываем в блоке (остальные — «ещё N»). */
export const CHECKS_SHOWN = 4;
/** Пауза перечитывания, пока идёт проверка, мс. */
export const POLL_MS = 6000;

/* ── чистые помощники ─────────────────────────────────────────────────── */

/** Значение выбора модели: «провайдер|модель» (как на сервере). */
export function modelValue(item) {
  const m = item && typeof item === 'object' ? item : {};
  const provider = String(m.provider || '').trim();
  const model = String(m.model || '').trim();
  return provider ? provider + '|' + model : model;
}

/** Подпись модели в списке: «Облако-А · Дилижанс про». */
export function modelLabel(item) {
  const m = item && typeof item === 'object' ? item : {};
  const provider = String(m.provider_label || m.provider || '').trim();
  const label = String(m.label || m.model || '').trim();
  return provider && label ? provider + ' · ' + label : (label || provider);
}

/**
 * Пункты выпадающего списка по провайдерам (группами), текущая модель — выбранной.
 * @param {Array} models `data.models` сервера
 * @param {string} current значение `провайдер|модель`
 * @returns {{groups: Array<{label: string, items: Array<{value: string, label: string, current: boolean}>}>,
 *            count: number, current: string}}
 */
export function pickerOptions(models, current) {
  const list = Array.isArray(models) ? models.filter((m) => m && m.model) : [];
  const groups = [];
  const byProvider = new Map();
  for (const item of list) {
    const value = modelValue(item);
    const key = String(item.provider || '');
    let group = byProvider.get(key);
    if (!group) {
      group = { label: String(item.provider_label || item.provider || 'Модели'), items: [] };
      byProvider.set(key, group);
      groups.push(group);
    }
    group.items.push({ value, label: String(item.label || item.model), current: value === current });
  }
  return { groups, count: list.length, current: String(current || '') };
}

/** Строка оценки расхода до запуска («≈ 34 тыс. токенов…»). */
export function estimateLine(estimate) {
  const e = estimate && typeof estimate === 'object' ? estimate : {};
  const own = String(e.text || '').trim();
  if (own) return own;
  const input = Number(e.input_tokens) || 0;
  const output = Number(e.output_tokens) || 0;
  if (!input && !output) return '';
  return '≈ ' + Math.round(input / 1000) + ' тыс. токенов на входе и '
    + Math.round(output / 1000) + ' тыс. на выходе.';
}

/** Заголовок проверки: «Дилижанс про · 14:20 · юрист». */
export function checkTitle(check, timeText) {
  const c = check && typeof check === 'object' ? check : {};
  const bits = [];
  if (c.model) bits.push(String(c.model));
  if (timeText) bits.push(String(timeText));
  if (c.assignee) bits.push(String(c.assignee));
  return bits.join(' · ');
}

/** Пункт проверки одной строкой (для title и для теста). */
export function itemText(item) {
  const it = item && typeof item === 'object' ? item : {};
  const quote = String(it.quote || '').trim();
  const note = String(it.note || '').trim();
  const label = String(it.label || '').trim();
  if (quote && note) return label + ': «' + quote + '» — ' + note;
  if (quote) return label + ': «' + quote + '»';
  return note || label;
}

/** Счётчик проверки словами: «2 не согласна · 1 не хватает» (или состояние). */
export function checkStateText(check) {
  const c = check && typeof check === 'object' ? check : {};
  if (c.running) return 'проверяет…';
  if (c.failed) return 'проверка не удалась';
  const counts = c.counts && typeof c.counts === 'object' ? c.counts : {};
  const parts = [];
  for (const [key, label] of [['disagree', 'не согласна'], ['missing', 'не хватает'], ['agree', 'согласна']]) {
    const n = Number(counts[key]) || 0;
    if (n) parts.push(n + ' ' + label);
  }
  if (parts.length) return parts.join(' · ');
  if (String(c.source || '') === 'raw') return 'ответ не по формату — текстом';
  if (c.text) return 'без расхождений';
  return 'нет ответа';
}

/** Время проверки: «14:20» или «вчера» — коротко, без секунд. */
export function timeText(at, now) {
  const t = Number(at) || 0;
  if (!t) return '';
  const then = new Date(t * 1000);
  const nowDate = new Date((Number(now) || Date.now() / 1000) * 1000);
  const same = then.toDateString() === nowDate.toDateString();
  const hh = String(then.getHours()).padStart(2, '0');
  const mm = String(then.getMinutes()).padStart(2, '0');
  return same ? hh + ':' + mm : then.toLocaleDateString('ru-RU', { day: 'numeric', month: 'short' });
}

/* ── подсветка фрагмента свода ─────────────────────────────────────────── */

/** Убрать прошлые подсветки: марки раскрываем, класс мигания снимаем. */
function clearMarks(host) {
  if (!host || typeof host.querySelectorAll !== 'function') return;
  for (const mark of Array.from(host.querySelectorAll('mark.so-hit'))) {
    const parent = mark.parentNode;
    if (!parent) continue;
    parent.replaceChild(document.createTextNode(mark.textContent || ''), mark);
    parent.normalize();
  }
  for (const node of Array.from(host.querySelectorAll('.so-flash'))) node.classList.remove('so-flash');
}

/**
 * Показать цитату в тексте свода: подсветить и прокрутить.
 * Если цитата размазана по нескольким узлам — подсвечиваем весь абзац.
 * @returns {boolean} нашли ли фрагмент
 */
export function markQuote(host, quote) {
  if (!host || !quote) return false;
  clearMarks(host);
  const walker = document.createTreeWalker(host, NodeFilter.SHOW_TEXT, null);
  let node = walker.nextNode();
  while (node) {
    const hit = findQuote(node.textContent || '', quote);
    if (hit) {
      const text = node.textContent || '';
      const range = document.createRange();
      range.setStart(node, Math.max(0, Math.min(hit.index, text.length)));
      range.setEnd(node, Math.max(0, Math.min(hit.index + hit.length, text.length)));
      const mark = document.createElement('mark');
      mark.className = 'so-hit';
      try {
        range.surroundContents(mark);
        if (typeof mark.scrollIntoView === 'function') mark.scrollIntoView({ block: 'center', behavior: 'smooth' });
        return true;
      } catch { /* диапазон через узлы — идём к абзацу */ }
    }
    node = walker.nextNode();
  }
  for (const block of Array.from(host.querySelectorAll('p, li, td, h1, h2, h3, h4'))) {
    if (findQuote(block.textContent || '', quote)) {
      block.classList.add('so-flash');
      if (typeof block.scrollIntoView === 'function') block.scrollIntoView({ block: 'center', behavior: 'smooth' });
      setTimeout(() => block.classList.remove('so-flash'), 2600);
      return true;
    }
  }
  return false;
}

/* ── фабрика: блок в карточке свода + окно выбора ──────────────────────── */

/**
 * @param {{ctx: object, rid: string, runAction: Function, getDetail: Function,
 *          root?: HTMLElement, toast?: Function}} R
 */
export function createSecondOpinion(R) {
  const deps = R && typeof R === 'object' ? R : {};
  const ctx = deps.ctx || {};
  const rid = String(deps.rid || '');
  const api = ctx.api || {};
  let data = null;          // ответ сервера (view)
  let loading = false;
  let poll = null;
  let host = null;          // узел блока (для перерисовки без всей страницы)

  const toast = (text, tone) => {
    if (typeof ctx.toast === 'function') ctx.toast(text, tone || 'info');
  };

  function path() {
    return '/api/requests/' + encodeURIComponent(rid) + '/second-opinion';
  }

  /** Перечитать состояние проверок. Сбой не рушит страницу: блок молчит. */
  async function load(force = false) {
    if (!rid || typeof api.get !== 'function') return data;
    if (loading) return data;
    loading = true;
    try {
      const res = await api.get(path(), force ? { cache: 'no-store' } : undefined);
      data = res && typeof res === 'object' ? res : null;
    } catch (err) {
      if (!data) console.warn('[second-opinion] состояние не прочитано:', err);
    } finally {
      loading = false;
    }
    schedulePoll();
    return data;
  }

  function schedulePoll() {
    if (poll) { clearTimeout(poll); poll = null; }
    const running = Boolean(data && data.running);
    if (!running || typeof setTimeout !== 'function') return;
    poll = setTimeout(async () => {
      poll = null;
      await load(true);
      paint();
    }, POLL_MS);
  }

  /** Перерисовать только блок (страница не перерисовывается целиком). */
  function paint() {
    const target = host;
    if (!target || !target.parentNode) return;
    const parent = target.parentNode;
    const fresh = buildBlock();          // buildBlock сам переносит `host` на новый узел
    if (typeof parent.replaceChild === 'function') parent.replaceChild(fresh, target);
    else { parent.removeChild(target); parent.appendChild(fresh); }   // упрощённый DOM
  }

  /* ── окно выбора модели и запуск ─────────────────────────────────────── */

  async function openDialog() {
    const view = await load(true);
    if (!view) {
      toast('Не удалось прочитать список моделей — проверьте связь с приложением', 'danger');
      return null;
    }
    if (view.can_run === false && view.why_not) {
      toast(String(view.why_not), 'warn');
      return null;
    }
    const models = Array.isArray(view.models) ? view.models : [];
    const author = view.author && typeof view.author === 'object' ? view.author : {};
    const def = view.default && typeof view.default === 'object' ? view.default : {};
    const current = modelValue(def) || (models[0] ? modelValue(models[0]) : '');
    const options = pickerOptions(models, current);

    const select = h('select', { class: 'select so-dlg__select', id: 'so-model',
      'aria-label': 'Модель для независимой проверки' });
    for (const group of options.groups) {
      const g = h('optgroup', { label: group.label });
      for (const item of group.items) {
        g.appendChild(h('option', { value: item.value, selected: item.current ? 'selected' : null },
          item.label));
      }
      select.appendChild(g);
    }
    if (!options.count) {
      select.appendChild(h('option', { value: '' }, 'Список моделей пуст'));
      select.disabled = true;
    } else if (options.count > 1) {
      select.value = current;
    }

    const note = h('div', { class: 'dialog__hint so-dlg__note' });
    const authorLabel = String(author.label || author.model || '').trim();
    const syncNote = () => {
      const provider = String(select.value || '').split('|')[0];
      const model = String(select.value || '').split('|').slice(1).join('|') || select.value;
      const same = Boolean(author.model) && String(author.model).toLowerCase() === String(model).toLowerCase();
      const bits = [];
      if (authorLabel) {
        bits.push(same
          ? 'Выбрана та же модель, что писала свод (' + authorLabel + '): проверка будет от неё же.'
          : 'Свод писала ' + authorLabel + '. По умолчанию выбрана другая модель.');
      }
      if (!provider) bits.push('Провайдер не указан — сервер возьмёт первый, где есть эта модель.');
      note.textContent = bits.join(' ');
      note.classList.toggle('so-dlg__note--warn', same);
    };
    select.addEventListener('change', syncNote);
    syncNote();

    const warn = String(view.warning || '').trim()
      ? h('div', { class: 'banner banner--warn so-dlg__warn' },
        icon('warning', { size: 13 }),
        h('span', { class: 'banner__text' }, String(view.warning).slice(0, 240)))
      : null;

    const estimate = h('div', { class: 'so-dlg__estimate', 'data-so-estimate': '' },
      icon('pulse', { size: 13 }),
      h('span', null, estimateLine(view.estimate) || 'Оценка расхода недоступна — проверка всё равно один вызов модели.'));

    const body = h('div', { class: 'so-dlg' },
      h('p', { class: 'dialog__text so-dlg__lead' },
        'Независимая проверка свода версии ' + (Number(view.version) || 0)
        + ': другая модель скажет, где свод расходится с запросом и чего в нём не хватает. '
        + 'Ничего не меняется в своде — вы получите только список расхождений.'),
      h('label', { class: 'so-dlg__label', for: 'so-model' }, 'Модель для проверки'),
      select,
      note,
      warn,
      estimate,
      h('div', { class: 'dialog__hint' },
        'Проверка — один вызов модели, запускается только этим нажатием. '
        + 'Ответ появится в блоке «Второе мнение» под сводом.'));

    const runBtn = h('button', {
      type: 'button', class: 'btn btn--primary', 'data-action': 'second-opinion-run',
      disabled: options.count ? null : true,
    }, icon('shield', { size: 13 }), 'Запустить проверку');
    const cancelBtn = h('button', { type: 'button', class: 'btn btn--secondary' }, 'Отмена');

    return new Promise((resolve) => {
      let done = false;
      let shell = null;
      const finish = (value) => {
        if (done) return;
        done = true;
        if (shell) shell.close();
        resolve(value);
      };
      cancelBtn.addEventListener('click', () => finish(null));
      runBtn.addEventListener('click', async () => {
        const provider = String(select.value || '').split('|')[0];
        const model = String(select.value || '').split('|').slice(1).join('|') || select.value;
        if (!String(model || '').trim()) { toast('Выберите модель', 'warn'); return; }
        runBtn.disabled = true;
        runBtn.classList.add('is-busy');
        const payload = { action: 'second_opinion', model: String(model).trim() };
        if (provider) payload.provider = provider;
        let res = null;
        try {
          res = typeof deps.runAction === 'function'
            ? await deps.runAction('second_opinion', payload, {
              toast: 'Проверка запущена — ответ появится под сводом',
            })
            : await api.post('/api/requests/' + encodeURIComponent(rid) + '/actions', payload);
        } catch (err) {
          toast(String((err && err.message) || 'Не удалось запустить проверку'), 'danger');
        } finally {
          runBtn.disabled = false;
          runBtn.classList.remove('is-busy');
        }
        if (res) {
          if (res.message) toast(String(res.message), 'ok');
          await load(true);
          paint();
        }
        finish(res || null);
      });
      shell = buildDialog({
        title: 'Второе мнение',
        body,
        buttons: [cancelBtn, runBtn],
        initialFocus: select,
        onCancel: () => finish(null),
      });
    });
  }

  /* ── блок под сводом ─────────────────────────────────────────────────── */

  function itemsBlock(check) {
    const items = Array.isArray(check.items) ? check.items : [];
    if (!items.length) {
      const text = String(check.text || '').trim();
      if (!text) return null;
      // Ответ не по формату — показываем как есть, без выдуманных вердиктов.
      return h('div', { class: 'so-check__raw' }, h('div', { class: 'small muted' }, text.slice(0, 2000)));
    }
    const list = h('ul', { class: 'so-check__items' });
    for (const item of items) {
      const row = h('li', { class: 'so-item so-item--' + String(item.verdict || 'agree') },
        h('span', { class: 'so-item__label' }, String(item.label || '')),
        h('span', { class: 'so-item__text' }, itemText({ ...item, label: '' }).replace(/^:\s*/, '')));
      if (item.quote) {
        row.appendChild(h('button', {
          type: 'button', class: 'btn btn--link btn--sm so-item__jump',
          'data-so-quote': String(item.quote).slice(0, 120),
          title: 'Показать это место в своде',
          onclick: () => {
            const body = deps.root && deps.root.querySelector
              ? deps.root.querySelector('.req-answer__body') : null;
            const ok = body ? markQuote(body, item.quote) : false;
            if (!ok) toast('Такого фрагмента в показанной версии нет — возможно, свод уже другой', 'warn');
          },
        }, icon('search', { size: 12 }), 'к фрагменту'));
      }
      list.appendChild(row);
    }
    return list;
  }

  function checkBlock(check) {
    const state = checkStateText(check);
    const head = h('div', { class: 'so-check__head' },
      h('span', { class: 'so-check__model', title: String(check.model || '') },
        checkTitle(check, timeText(check.at))),
      h('span', { class: 'chip chip--muted so-check__state' }, state));
    const box = h('div', {
      class: 'so-check' + (check.running ? ' so-check--running' : '')
        + (check.failed ? ' so-check--failed' : ''),
      'data-so-card': String(check.card_id || ''),
    }, head);
    if (check.running) {
      box.appendChild(h('div', { class: 'small muted so-check__wait' },
        'Модель проверяет свод — ответ появится здесь сам.'));
    } else if (check.failed) {
      box.appendChild(h('div', { class: 'small muted so-check__wait' },
        'Проверка завершилась без ответа. Можно запустить ещё раз.'));
    } else {
      const items = itemsBlock(check);
      if (items) box.appendChild(items);
    }
    return box;
  }

  /**
   * Блок «Второе мнение» для карточки свода. Возвращает `null`, пока данных нет и
   * запрос ещё не сделан: пустая рамка на первом экране не нужна.
   */
  function buildBlock() {
    const view = data;
    const checks = view && Array.isArray(view.checks) ? view.checks : [];
    const shown = checks.slice(-CHECKS_SHOWN).reverse();
    const running = Boolean(view && view.running);
    const canRun = view ? view.can_run !== false : true;
    const btn = h('button', {
      type: 'button', class: 'btn btn--secondary btn--sm so-head__btn',
      'data-action': 'second-opinion',
      title: 'Попросить другую модель проверить свод: только расхождения, свод не меняется',
      disabled: canRun ? null : true,
      onclick: () => openDialog(),
    }, icon(running ? 'sync' : 'shield-check', { size: 13 }),
    running ? 'Проверка идёт…' : 'Спросить другую модель');

    const head = h('div', { class: 'so-head' },
      h('h3', { class: 'so-head__title' },
        h('span', { class: 'so-head__dot', 'aria-hidden': 'true' }), 'Второе мнение'),
      checks.length
        ? h('span', { class: 'chip chip--muted tnum' }, plural(checks.length, 'проверка', 'проверки', 'проверок'))
        : null,
      h('div', { class: 'spacer' }),
      btn);

    const el = h('div', { class: 'so-block', 'data-second-opinion': '' }, head);
    if (!view) {
      el.appendChild(h('div', { class: 'small muted so-block__hint' },
        'Независимая проверка свода на другой модели — по кнопке, с оценкой расхода.'));
    } else if (!shown.length) {
      el.appendChild(h('div', { class: 'small muted so-block__hint' },
        'Свод ещё никто не проверял другой моделью. Это не меняет свод: вы получите '
        + 'список «согласна / не согласна / не хватает» с цитатами.'));
      if (view.why_not && !canRun) {
        el.appendChild(h('div', { class: 'small muted so-block__why' }, String(view.why_not)));
      }
    } else {
      for (const check of shown) el.appendChild(checkBlock(check));
      if (checks.length > shown.length) {
        el.appendChild(h('div', { class: 'small muted so-block__hint' },
          'Ещё ' + (checks.length - shown.length) + ' — в дереве работ.'));
      }
    }
    host = el;
    return el;
  }

  /** Первый показ: подтянуть состояние и перерисовать, если блок уже стоит. */
  async function start() {
    await load();
    paint();
    return data;
  }

  return { buildBlock, open: openDialog, start, refresh: load, data: () => data };
}

export default { createSecondOpinion, pickerOptions, modelValue, modelLabel, estimateLine,
  checkTitle, checkStateText, itemText, markQuote, timeText, CHECKS_SHOWN, POLL_MS };
