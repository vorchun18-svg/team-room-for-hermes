/**
 * agent-model-row.js — редактор одного участника: провайдер, поиск моделей,
 * «Размышление» и «Усилия». Собирается в agent-model-ui.js (mountAgentModel).
 *
 * Возвращает не узел, а строку с методами: так вкладка «Команда» и диалог
 * комнаты работают с одним и тем же кодом, а тесты (`node --test`) — без браузера.
 */

import { h, uid } from './util.js';
import { icon } from './icons.js';
import { field, segmented } from './ui.js';
import { countNoun } from './text-ru.js';
import { humanApiMessage } from './shell-labels.js';
import {
  THINKING_OFF, normalizeReasoning, effortOptions, providersOf, modelLabelOf, defaultsText,
  settingsLabel, filterModels, flatModels, shortModelText, allProviders,
} from './agent-model-model.js';

const ROLE_RU = {
  orchestrator: 'руководитель комнаты',
  critic: 'критик',
  worker: 'исполнитель',
  observer: 'наблюдатель',
};

const enc = (s) => encodeURIComponent(String(s === null || s === undefined ? '' : s));

/* Мини-DOM в тестах не знает querySelector: обходим детей сами. */
function childEls(node) {
  return (node && node.childNodes ? Array.from(node.childNodes) : [])
    .filter((n) => n && n.nodeType === 1);
}

function hasClass(node, name) {
  return String((node && node.className) || '').split(/\s+/).includes(name);
}

/* ─────────────────────────────────────────────────────────── один участник */

function personLabel(person) {
  return String(person && (person.title || person.profile) || '');
}

function personRole(role) {
  return ROLE_RU[String(role || 'worker')] || ROLE_RU.worker;
}

/**
 * Собрать редактор участника. Внутренняя функция: снаружи — mountAgentModel.
 * @returns {object} строка с методами для тестов и для самой панели
 */
export function buildRow(person, env) {
  const {
    catalog, mode, room, ctx, onChange, defaultsOf, initialOf, applyOverride, remember,
  } = env;
  const profile = String(person.profile || '');
  const defaults = defaultsOf(profile);
  const initial = initialOf(profile);
  const efforts = effortOptions(catalog);
  const known = new Set(efforts.map((e) => e.value));
  const fallbackEffort = (() => {
    const d = normalizeReasoning(defaults.reasoning);
    return d && d !== THINKING_OFF && known.has(d) ? d : 'medium';
  })();

  const st = {
    provider: initial.provider || defaults.provider || (providersOf(catalog)[0] || {}).id || '',
    model: initial.model || defaults.model || '',
    reasoning: normalizeReasoning(initial.reasoning !== undefined && initial.reasoning !== null
      && initial.reasoning !== '' ? initial.reasoning : defaults.reasoning),
    source: String(initial.source || 'agent'),
    dirty: String(initial.source || 'agent') === 'room',
    query: '',
    active: -1,
    // Список моделей — обычный блок под полем, а не всплывающая панель:
    // он открыт СРАЗУ, при любом провайдере, в том числе уже выбранном.
    listOpen: true,
    panelOpen: mode === 'live',
    busy: false,
    saved: '',
    pending: 0,
    restarted: 0,
    error: '',
  };
  // Источник «room» у участника = уже изменено для этой комнаты.
  if (st.source === 'room') remember(profile, st);

  const row = h('div', { class: 'agent-model__row', dataset: { profile } });
  const nameText = personLabel(person);
  const roleText = personRole(person.role);

  const chip = h('span', { class: 'chip chip--outline agent-model__chip' });
  const changedChip = h('span', { class: 'chip chip--outline agent-model__changed hidden' }, 'изменено для этой комнаты');
  const editText = h('span', null, 'Изменить');
  const editBtn = h('button', { class: 'btn btn--ghost btn--sm agent-model__edit', type: 'button' },
    icon('edit', { size: 11 }), editText);
  editBtn.addEventListener('click', () => setOpen(!st.panelOpen));

  const summary = h('div', { class: 'agent-model__summary' },
    h('span', { class: 'agent-model__who', title: 'Профиль: ' + profile },
      h('span', { class: 'agent-model__name' }, nameText),
      h('span', { class: 'agent-model__role' }, ' · ' + roleText)),
    chip,
    changedChip,
    editBtn);

  const panel = h('div', { class: 'agent-model__panel' + (st.panelOpen ? '' : ' hidden') });

  const providerSelect = h('select', { class: 'select agent-model__provider', 'aria-label': 'Провайдер модели для ' + nameText });
  const providerWarn = h('div', { class: 'agent-model__warn hidden' }, 'Провайдер больше не настроен — выберите другой.');
  providerSelect.addEventListener('change', () => {
    st.provider = String(providerSelect.value || '');
    st.dirty = true;
    st.query = '';
    search.value = '';
    st.active = -1;
    st.listOpen = true;
    paintProvider();
    paintModels();
    paint();
    touch();
  });

  const search = h('input', {
    class: 'input agent-model__search',
    type: 'search',
    autocomplete: 'off',
    spellcheck: 'false',
    placeholder: 'Найти модель — например, sonnet',
    'aria-label': 'Модель для ' + nameText,
    role: 'combobox',
    'aria-autocomplete': 'list',
    'aria-expanded': 'true',
    // Модель выбирают из списка, а не диктуют: авто-микрофон сюда не ставим.
    dataset: { mic: 'off' },
  });
  const listId = uid('am-models');
  search.setAttribute('aria-controls', listId);
  const list = h('div', { class: 'agent-model__models', id: listId, role: 'listbox', 'aria-label': 'Модели' });
  const listEmpty = h('div', { class: 'agent-model__models-empty hidden' });
  const current = h('div', { class: 'agent-model__current small muted' });

  // Стрелка-раскрывашка: то же, что у обычного выпадающего списка.
  const toggleBtn = h('button', {
    class: 'agent-model__toggle',
    type: 'button',
    'aria-controls': listId,
    'aria-expanded': 'true',
    'aria-label': 'Скрыть список моделей для ' + nameText,
    title: 'Показать или скрыть список моделей',
  }, icon('chevron-down', { size: 14 }));

  const openList = () => {
    if (st.listOpen) { paintList(); return; }
    st.listOpen = true;
    paintList();
  };

  toggleBtn.addEventListener('click', (ev) => {
    if (ev && typeof ev.preventDefault === 'function') ev.preventDefault();
    st.listOpen = !st.listOpen;
    paintList();
    if (st.listOpen && typeof search.focus === 'function') {
      try { search.focus(); } catch { /* нет фокуса */ }
    }
  });

  // Нажатие в поле тоже раскрывает список: он не должен «просыпаться» только
  // после смены провайдера.
  search.addEventListener('focus', openList);
  search.addEventListener('click', openList);

  function readQuery(open) {
    st.query = String(search.value || '');
    st.active = -1;
    if (open) st.listOpen = true;
    paintModels();
    paint();
  }

  search.addEventListener('input', () => { readQuery(true); });
  // Родная «×» у поля type=search чистит значение событием `search`, а не `input`.
  search.addEventListener('search', () => { readQuery(true); });
  search.addEventListener('keydown', (ev) => {
    const key = ev && ev.key;
    if (key === 'ArrowDown' || key === 'ArrowUp') {
      if (typeof ev.preventDefault === 'function') ev.preventDefault();
      st.listOpen = true;
      moveActive(key === 'ArrowDown' ? 1 : -1);
      return;
    }
    if (key === 'Enter') {
      if (typeof ev.preventDefault === 'function') ev.preventDefault();
      if (st.listOpen) chooseActive();
      else { st.listOpen = true; paintList(); }
      return;
    }
    if (key === 'Escape') {
      if (typeof ev.preventDefault === 'function') ev.preventDefault();
      if (typeof ev.stopPropagation === 'function') ev.stopPropagation();
      if (st.listOpen) {
        // Свернуть список и заодно сбросить фильтр: браузер сам очищает
        // type=search по Esc, и список не должен остаться «отфильтрованным» впустую.
        st.listOpen = false;
        st.query = '';
        search.value = '';
        paintModels();
        return;
      }
      if (mode !== 'live') setOpen(false);
    }
  });

  const thinkInput = h('input', { type: 'checkbox', class: 'agent-model__think-input' });
  thinkInput.addEventListener('change', () => {
    st.reasoning = thinkInput.checked ? (st.reasoning === THINKING_OFF || !st.reasoning ? fallbackEffort : st.reasoning) : THINKING_OFF;
    st.dirty = true;
    st.saved = '';
    paintEfforts();
    paint();
    touch();
  });
  const thinkLabel = h('label', { class: 'checkbox agent-model__think' }, thinkInput,
    h('span', null, 'Размышление'));

  const effortsHost = h('div', { class: 'agent-model__efforts' });
  const effortsHint = h('div', { class: 'agent-model__hint small muted' }, 'Мышление выключено — участник отвечает сразу.');

  const resetBtn = h('button', { class: 'btn btn--ghost btn--sm agent-model__reset', type: 'button' }, 'Вернуть по умолчанию');
  resetBtn.addEventListener('click', () => { reset(); });
  const applyBtn = h('button', { class: 'btn btn--primary btn--sm agent-model__apply', type: 'button' }, 'Применить');
  applyBtn.addEventListener('click', () => { save(); });
  const restartBtn = h('button', { class: 'btn btn--secondary btn--sm agent-model__restart hidden', type: 'button' },
    'Перезапустить на новой модели');
  restartBtn.addEventListener('click', () => { restart(); });

  const defaultLine = h('div', { class: 'agent-model__default small muted' });
  const status = h('div', { class: 'agent-model__status', role: 'status', 'aria-live': 'polite' });
  // role="alert" включаем только когда ошибка есть: пустой узел не должен
  // выглядеть как живое предупреждение на странице.
  const errorLine = h('div', { class: 'agent-model__error hidden' });

  const actions = h('div', { class: 'agent-model__actions' }, defaultLine,
    h('div', { class: 'agent-model__buttons' }, resetBtn, mode === 'live' ? applyBtn : null, restartBtn));

  panel.appendChild(field({ label: 'Провайдер', control: providerSelect, hint: 'Только те, к которым уже подключён ключ.' }));
  panel.appendChild(providerWarn);
  panel.appendChild(h('div', { class: 'field agent-model__model-field' },
    h('span', { class: 'field__label' }, 'Модель'),
    h('div', { class: 'agent-model__search-wrap' }, search, toggleBtn),
    list,
    listEmpty,
    current));
  panel.appendChild(h('div', { class: 'agent-model__thinking' },
    thinkLabel,
    field({ label: 'Усилия', control: effortsHost, hint: 'Чем выше уровень, тем дольше участник думает над задачей.' }),
    effortsHint));
  panel.appendChild(actions);
  panel.appendChild(status);
  panel.appendChild(errorLine);

  row.appendChild(summary);
  row.appendChild(panel);

  /* ── отрисовка ─────────────────────────────────────────────────────── */

  function differs() {
    return st.provider !== defaults.provider
      || st.model !== defaults.model
      || normalizeReasoning(st.reasoning) !== normalizeReasoning(defaults.reasoning);
  }

  /** Изменено для этой комнаты = участник тронут И отличается от настроек агента. */
  function changed() {
    return st.dirty && differs();
  }

  function paintProvider() {
    const list2 = allProviders(catalog);
    providerSelect.replaceChildren();
    const configured = providersOf(catalog);
    for (const p of configured) {
      const opt = h('option', { value: p.id, title: p.id }, p.label);
      providerSelect.appendChild(opt);
    }
    const lost = list2.find((p) => p.id === st.provider && !p.configured);
    if (lost) {
      // Уже выбранный провайдер перестал быть настроенным — показать и предупредить.
      providerSelect.appendChild(h('option', { value: lost.id, title: lost.id },
        lost.label + ' — больше не настроен'));
    }
    if (!configured.length && !lost) {
      providerSelect.appendChild(h('option', { value: '' }, 'Нет настроенных провайдеров'));
    }
    providerSelect.value = st.provider;
    const warn = Boolean(lost) || (!configured.length && Boolean(st.provider));
    providerWarn.classList.toggle('hidden', !warn);
    providerSelect.disabled = !configured.length && !lost;
  }

  function groups() {
    return filterModels(catalog, { provider: st.provider, query: st.query });
  }

  function optionNodes() {
    return childEls(list).filter((n) => hasClass(n, 'agent-model__option'));
  }

  function paintModels() {
    const gs = groups();
    const flat = flatModels(gs);
    list.replaceChildren();
    if (!flat.length) {
      const provider = providersOf(catalog).find((p) => p.id === st.provider);
      // Список остаётся «открытым» по состоянию: очистят поиск — он вернётся сам.
      if (st.query && String(st.query).trim()) {
        listEmpty.textContent = 'Ничего не нашлось. Проверьте написание или выберите провайдера.';
        listEmpty.classList.remove('hidden');
      } else if (provider && !provider.models.length) {
        listEmpty.textContent = 'У этого провайдера нет списка моделей — впишите название вручную и нажмите Enter.';
        listEmpty.classList.remove('hidden');
      } else if (!providersOf(catalog).length) {
        listEmpty.textContent = 'Нет ни одного настроенного провайдера. Проверьте ключи в настройках Hermes.';
        listEmpty.classList.remove('hidden');
      } else {
        listEmpty.classList.add('hidden');
      }
      paintList();
      return;
    }
    listEmpty.classList.add('hidden');
    let index = -1;
    for (const g of gs) {
      list.appendChild(h('div', { class: 'agent-model__group-label', title: 'Провайдер: ' + g.id }, g.label));
      for (const m of g.models) {
        index += 1;
        const chosen = String(m.id) === String(st.model) && String(g.id) === String(st.provider);
        const opt = h('button', {
          class: 'agent-model__option' + (chosen ? ' agent-model__option--chosen' : '')
            + (index === st.active ? ' agent-model__option--active' : ''),
          type: 'button',
          role: 'option',
          'aria-selected': chosen ? 'true' : 'false',
          dataset: { index: String(index), provider: g.id, model: m.id },
          title: 'Модель: ' + m.id,
        }, h('span', { class: 'agent-model__option-label' }, m.label),
        chosen ? icon('check', { size: 11 }) : null);
        // Клик выбирает ИМЕННО эту строку: провайдер и id берём у самого узла,
        // а не общий счётчик цикла (иначе все обработчики видели последний индекс
        // и в комнату уходила последняя модель списка).
        opt.addEventListener('click', () => { setModel(g.id, m.id); });
        list.appendChild(opt);
      }
    }
    paintList();
    // Список раскрыт — подкручиваем к действующей модели: у Ollama Cloud их 26,
    // иначе выбранная строка остаётся за пределами видимой части блока.
    if (st.listOpen && !String(st.query || '').trim()) {
      const nodes = optionNodes();
      const at = nodes.findIndex((n) => hasClass(n, 'agent-model__option--chosen'));
      if (at >= 0 && typeof nodes[at].scrollIntoView === 'function') {
        try { nodes[at].scrollIntoView({ block: 'nearest' }); } catch { /* нет раскладки */ }
      }
    }
  }

  function listOpenPaint(open) {
    list.classList.toggle('hidden', !open);
  }

  function paintList() {
    const nodes = optionNodes();
    const open = Boolean(st.listOpen) && nodes.length > 0;
    listOpenPaint(open);
    nodes.forEach((node, i) => {
      node.classList.toggle('agent-model__option--active', i === st.active);
    });
    if (open && st.active >= 0 && nodes[st.active] && typeof nodes[st.active].scrollIntoView === 'function') {
      try { nodes[st.active].scrollIntoView({ block: 'nearest' }); } catch { /* нет раскладки */ }
    }
    search.setAttribute('aria-expanded', open ? 'true' : 'false');
    toggleBtn.setAttribute('aria-expanded', open ? 'true' : 'false');
    toggleBtn.setAttribute('aria-label', (open ? 'Скрыть' : 'Показать') + ' список моделей для ' + nameText);
  }

  function moveActive(delta) {
    const flat = flatModels(groups());
    if (!flat.length) { st.active = -1; paintList(); return; }
    const next = st.active < 0
      ? (delta > 0 ? 0 : flat.length - 1)
      : Math.max(0, Math.min(flat.length - 1, st.active + delta));
    st.active = next;
    paintList();
  }

  function chooseIndex(index) {
    const flat = flatModels(groups());
    const hit = flat[index];
    if (!hit) {
      // Свободный ввод: у провайдера нет списка моделей — берём введённое.
      const typed = String(st.query || '').trim();
      if (typed) { setModel(st.provider, typed); }
      return false;
    }
    setModel(hit.provider, hit.id);
    return true;
  }

  function chooseActive() {
    if (st.active < 0) return chooseIndex(0);
    return chooseIndex(st.active);
  }

  function setModel(provider, model) {
    st.provider = String(provider || st.provider);
    st.model = String(model || '');
    st.query = '';
    search.value = '';
    st.active = -1;
    // Список — блок под полем: после выбора оставляем его открытым, чтобы было
    // видно галочку и можно было сразу выбрать другую модель.
    st.listOpen = true;
    st.dirty = true;
    st.saved = '';
    st.error = '';
    paintProvider();
    paintModels();
    paint();
    touch();
  }

  function paintEfforts() {
    const off = normalizeReasoning(st.reasoning) === THINKING_OFF;
    effortsHost.replaceChildren();
    const value = off ? '' : (known.has(normalizeReasoning(st.reasoning)) ? normalizeReasoning(st.reasoning) : fallbackEffort);
    const seg = segmented({
      options: efforts,
      value,
      onchange: (v) => {
        st.reasoning = String(v);
        st.dirty = true;
        st.saved = '';
        paint();
        touch();
      },
    });
    seg.classList.add('agent-model__segments');
    if (off) {
      seg.setAttribute('aria-disabled', 'true');
      seg.classList.add('agent-model__segments--off');
      for (const btn of childEls(seg)) btn.disabled = true;
    }
    effortsHost.appendChild(seg);
    effortsHost.appendChild(effortsHint);
    effortsHint.classList.toggle('hidden', !off);
    thinkInput.checked = !off;
  }

  function paintCurrent() {
    const label = st.model ? modelLabelOf(catalog, st.provider, st.model) : '';
    current.textContent = 'Выбрано: ' + shortModelText(catalog, st);
    current.setAttribute('title', settingsLabel(catalog, st));
    // В самом поле видно текущую модель — как у обычного выпадающего списка.
    // Свой ввод остаётся фильтром: он заменяет подсказку, а не выбор.
    search.setAttribute('placeholder', label || 'Найти модель — например, sonnet');
    search.setAttribute('aria-label', 'Модель для ' + nameText
      + (label ? ': сейчас ' + label : '') + '. Начните вводить название, чтобы отфильтровать список.');
  }

  function paint() {
    const isChanged = changed();
    const ids = [st.provider, st.model].filter(Boolean).join(' · ');
    const title = settingsLabel(catalog, st) + (ids ? ' · ' + ids : '') + ' (профиль: ' + profile + ')';
    chip.textContent = 'модель: ' + shortModelText(catalog, st);
    chip.setAttribute('title', title);
    changedChip.classList.toggle('hidden', !isChanged);
    panel.classList.toggle('hidden', !st.panelOpen);
    editBtn.setAttribute('aria-expanded', st.panelOpen ? 'true' : 'false');
    editText.textContent = st.panelOpen ? 'Свернуть' : 'Изменить';
    defaultLine.textContent = defaultsText(catalog, defaults);
    defaultLine.setAttribute('title', 'Настройки самого агента: ' + (defaults.model || 'не заданы'));
    resetBtn.classList.toggle('hidden', !isChanged && mode !== 'live');
    applyBtn.disabled = st.busy;
    restartBtn.classList.toggle('hidden', !st.pending);
    restartBtn.disabled = st.busy;
    paintCurrent();
    errorLine.textContent = st.error;
    errorLine.classList.toggle('hidden', !st.error);
    if (st.error) errorLine.setAttribute('role', 'alert');
    else errorLine.removeAttribute('role');
    if (st.saved) {
      status.textContent = st.saved;
      status.classList.add('agent-model__status--done');
    } else {
      status.textContent = '';
      status.classList.remove('agent-model__status--done');
    }
  }

  function touch() {
    // Запоминаем выбор: при перерисовке состава он не должен потеряться.
    remember(profile, changed() ? snapshot() : null);
    if (typeof onChange !== 'function') return;
    onChange(profile, changed() ? snapshot() : null);
  }

  function snapshot() {
    return { provider: st.provider, model: st.model, reasoning: normalizeReasoning(st.reasoning) };
  }

  function setOpen(open) {
    st.panelOpen = Boolean(open);
    if (st.panelOpen) {
      // Список моделей открыт сразу — ждать смены провайдера не нужно.
      st.listOpen = true;
      paintProvider();
      paintModels();
      paintEfforts();
      paint();
      if (typeof search.focus === 'function') { try { search.focus(); } catch { /* нет фокуса */ } }
      return;
    }
    st.listOpen = false;
    paint();
  }

  function reset() {
    st.provider = defaults.provider;
    st.model = defaults.model;
    st.reasoning = normalizeReasoning(defaults.reasoning);
    st.query = '';
    search.value = '';
    st.active = -1;
    st.listOpen = true;
    st.dirty = false;
    st.saved = '';
    st.error = '';
    if (mode === 'live') {
      // «Вернуть по умолчанию» на вкладке «Команда» сразу возвращает настройку агента.
      st.pending = 0;
      save(true);
      return;
    }
    remember(profile, null);
    paintProvider();
    paintModels();
    paintEfforts();
    paint();
    touch();
  }

  function showError(message) {
    st.error = String(message || '');
    paint();
  }

  function applyResult(res, wasDefault) {
    const applied = res && res.applied && typeof res.applied === 'object' ? res.applied : {};
    const pending = Number(applied.pending_running) || 0;
    const cards = Number(applied.cards_updated) || 0;
    st.pending = pending;
    st.restarted = 0;
    st.source = wasDefault ? 'agent' : 'room';
    const head = wasDefault
      ? 'Сохранено. Следующая задача пойдёт на модели по умолчанию'
      : 'Сохранено. Следующая задача уже пойдёт на новой модели';
    st.saved = pending
      ? head + '; ' + countNoun(pending, ['задача', 'задачи', 'задач']) + ' в работе доделают на старой'
      : head + '.';
    if (cards) st.saved += ' Поправил ' + countNoun(cards, ['задачу', 'задачи', 'задач']) + ' в очереди.';
    applyOverride(profile, wasDefault ? null : snapshot());
    if (wasDefault) remember(profile, null); else remember(profile, snapshot());
    if (typeof person.onSaved === 'function') person.onSaved(profile);
    paintProvider();
    paintModels();
    paintEfforts();
    paint();
    touch();
  }

  async function save(forceDefault) {
    if (st.busy) return null;
    const api = ctx && ctx.api;
    const roomId = String((room && room.id) || (ctx && ctx.room && ctx.room.id) || '');
    if (!api || typeof api.put !== 'function' || !roomId) {
      showError('Сохранение недоступно — обновите страницу и попробуйте снова.');
      return null;
    }
    const asDefault = forceDefault === true || !changed();
    const body = asDefault ? {} : snapshot();
    st.busy = true;
    st.error = '';
    st.saved = '';
    paint();
    try {
      const res = await api.put('/api/rooms/' + enc(roomId) + '/agents/' + enc(profile) + '/model', body);
      st.busy = false;
      applyResult(res || {}, asDefault);
      return res;
    } catch (err) {
      st.busy = false;
      showError(humanApiMessage((err && err.message) || 'Не удалось сохранить модель'));
      paint();
      return null;
    }
  }

  async function restart() {
    if (st.busy) return null;
    const api = ctx && ctx.api;
    const roomId = String((room && room.id) || (ctx && ctx.room && ctx.room.id) || '');
    if (!api || typeof api.post !== 'function' || !roomId) {
      showError('Перезапуск недоступен — обновите страницу и попробуйте снова.');
      return null;
    }
    st.busy = true;
    paint();
    try {
      const res = await api.post('/api/rooms/' + enc(roomId) + '/agents/' + enc(profile) + '/restart', {});
      const n = Number(res && res.restarted) || 0;
      st.busy = false;
      st.pending = 0;
      st.saved = n
        ? 'Перезапущено: ' + countNoun(n, ['задача', 'задачи', 'задач']) + ' — теперь на новой модели.'
        : 'В работе ничего не осталось: следующая задача пойдёт на новой модели.';
      paint();
      return res;
    } catch (err) {
      st.busy = false;
      showError(humanApiMessage((err && err.message) || 'Не удалось перезапустить задачи'));
      paint();
      return null;
    }
  }

  function setReasoningChecked(on) {
    st.reasoning = on
      ? (normalizeReasoning(st.reasoning) === THINKING_OFF ? fallbackEffort : st.reasoning)
      : THINKING_OFF;
    st.saved = '';
    paintEfforts();
    paint();
    touch();
  }

  paintProvider();
  paintModels();
  paintEfforts();
  paint();

  return {
    profile,
    element: row,
    panel,
    open() { setOpen(true); },
    setOpen,
    isOpen() { return st.panelOpen; },
    /** Открыт ли список моделей (это блок под полем, а не всплывающая панель). */
    listOpen() { return st.listOpen; },
    /** Поле выбора модели — для проверок диктовки и подсказки с текущей моделью. */
    searchInput() { return search; },
    state() {
      return {
        provider: st.provider,
        model: st.model,
        reasoning: normalizeReasoning(st.reasoning),
        changed: changed(),
        source: st.source,
        pending: st.pending,
        saved: st.saved,
        error: st.error,
      };
    },
    provider() { return st.provider; },
    model() { return st.model; },
    reasoning() { return normalizeReasoning(st.reasoning); },
    setProvider(id) {
      st.provider = String(id || '');
      st.dirty = true;
      st.query = '';
      search.value = '';
      st.active = -1;
      st.listOpen = true;
      paintProvider();
      paintModels();
      paint();
      touch();
      return st.provider;
    },
    setQuery(text) {
      st.query = String(text === null || text === undefined ? '' : text);
      search.value = st.query;
      st.active = -1;
      st.listOpen = true;
      paintModels();
      paint();
      return this.models();
    },
    models() { return flatModels(groups()); },
    visibleGroupLabels() { return Array.from(list.childNodes || [])
      .filter((n) => n && n.nodeType === 1 && String(n.className || '').includes('agent-model__group-label'))
      .map((n) => n.textContent); },
    choose(index) { return chooseIndex(Number(index)); },
    chooseModel(provider, model) { setModel(provider, model); return st.model; },
    key(key) {
      // Клавиатура списка: ↑ ↓ Enter Esc — без настоящих событий (тесты).
      if (key === 'ArrowDown' || key === 'ArrowUp') { st.listOpen = true; moveActive(key === 'ArrowDown' ? 1 : -1); }
      else if (key === 'Enter') { if (st.listOpen) chooseActive(); }
      else if (key === 'Escape') { if (st.listOpen) { st.listOpen = false; paintList(); } else setOpen(false); }
      return st.active;
    },
    setThinking(on) { setReasoningChecked(on); return st.reasoning; },
    setEffort(value) {
      const v = normalizeReasoning(value);
      st.reasoning = v === THINKING_OFF ? THINKING_OFF : (v || fallbackEffort);
      st.dirty = true;
      paintEfforts();
      paint();
      touch();
      return st.reasoning;
    },
    efforts() { return efforts.slice(); },
    defaultsText() { return defaultLine.textContent; },
    reset,
    save,
    restart,
    chipText() { return chip.textContent; },
    statusText() { return status.textContent; },
    errorText() { return errorLine.textContent; },
    setBusy(v) { st.busy = Boolean(v); paint(); },
    destroy() { row.remove(); },
  };
}
