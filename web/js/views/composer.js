/**
 * views/composer.js — вкладка «Запрос»: поле под диктовку, тонкая панель
 * действий, строка «кто работает» и идеи. Лента и кандидаты — в composer-feed.js.
 *
 * Данные: POST /api/rooms/{id}/requests, GET /api/rooms/{id}, GET /api/rooms/{id}/backlog.
 * Живое обновление ленты — общий таймер здесь (3 с при активных, иначе 15 с).
 */

import { h, timeAgo, debounce, plainTerms, plural } from '../util.js';
import { icon } from '../icons.js';
import {
  ensureCss, avatar, emptyState, errorBanner, pathChip, confirmDialog,
  lsGet, lsSet,
} from '../ui.js';
import { createMic } from '../dictation.js';
import { createFeed } from './composer-feed.js';
import { BRIEF_LABEL, BRIEF_HINT, briefDefaultOn, briefPayload } from './plan-req.js';

/** Мягкая подсказка: длинный запрос лучше разбить, но поле не ограничиваем. */
const LONG_TEXT = 100000;
const MAX_ATTACH = 10;
const POLL_ACTIVE = 3000;
const POLL_IDLE = 15000;

const enc = (s) => encodeURIComponent(String(s == null ? '' : s));

/** Предупреждение сервера после отправки — живёт до закрытия или перезагрузки. */
let pendingWarning = null;

/**
 * Человеческое имя исполнителя: «biz-ops» → «ops», «mikhail» → «Михаил».
 * Полный id остаётся в title — в тексте интерфейса системных имён нет.
 * @param {string} id
 * @returns {string}
 */
export function personName(id) {
  const raw = String(id == null ? '' : id).trim();
  if (!raw) return '';
  if (raw === 'mikhail') return 'Михаил';
  return raw.replace(/^(?:biz|room)-/, '');
}

/**
 * @param {HTMLElement} el
 * @param {object} ctx
 * @returns {Promise<{unmount: Function}>}
 */
export async function mount(el, ctx) {
  await ensureCss('css/view-composer.css');
  // Стили проектного режима: флажок брифа, бейджи, плашка карты.
  await ensureCss('css/plan-req.css');

  const api = ctx.api;
  const toast = typeof ctx.toast === 'function' ? ctx.toast : () => {};
  const roomId = String((ctx.room && ctx.room.id) || (ctx.params && ctx.params.roomId) || (ctx.route && ctx.route.roomId) || '');
  if (!roomId) {
    el.appendChild(h('div', { class: 'view__inner' },
      emptyState({ icon: 'comment-discussion', title: 'Комната не выбрана', text: 'Выберите комнату в списке слева.' })));
    return { unmount() {} };
  }

  let room = ctx.room && ctx.room.id ? ctx.room : null;
  // В /api/rooms сводка без участников — для строки «кто работает» берём деталь.
  if (!room || room.critic_pass === undefined || !room.orchestrator || !Array.isArray(room.members) || !room.members.length) {
    try {
      const full = await api.get('/api/rooms/' + enc(roomId));
      room = Object.assign({}, room || {}, full || {});
    } catch {
      /* живём на сводке: имя руководителя подставится заглушкой */
    }
  }

  // Проектный режим комнаты: флажок «Это бриф проекта» виден только здесь.
  const projectMode = Boolean(room && room.project_mode === true);

  const state = {
    sending: false,
    submitError: null,
    critic: Boolean(room && room.critic_pass) && Boolean(room && room.critic),
    attach: [],
    attachOpen: false,
    ideas: [],
    draftSavedAt: 0,
    brief: false,
    briefTouched: false,
  };

  /* ─────────────────────────────────────────────────────────────── каркас */

  const root = h('div', { class: 'view__inner cmp' });

  const heading = h('div', { class: 'cmp-head' },
    h('h2', { class: 'cmp-head__title' }, 'Запрос команде'),
    h('p', { class: 'cmp-head__sub' }, 'Один запрос — один сводный ответ.'));

  const textarea = h('textarea', {
    class: 'textarea cmp-input',
    rows: 3,
    id: 'cmp-input',
    placeholder: 'Напишите или надиктуйте запрос. Можно длинно — текст встанет туда, где курсор.',
    'aria-label': 'Текст запроса',
    spellcheck: 'false',
    'data-mic': 'manual',
  });

  // Тихий счётчик: без верхней границы, только «сколько уже наговорено».
  const counter = h('span', { class: 'cmp-count tnum', 'aria-live': 'off' }, '0 знаков');
  const longNote = h('div', { class: 'cmp-long hidden', role: 'status' },
    'Очень длинный запрос — лучше разбить на части.');

  const criticToggle = room && room.critic
    ? h('button', {
      class: 'cmp-toggle', type: 'button', role: 'switch',
      'aria-checked': state.critic ? 'true' : 'false',
      title: 'Проверка ответа вторым агентом: критик читает свод перед отправкой',
      'aria-label': 'Проверка ответа вторым агентом — с критиком',
      onclick: () => {
        state.critic = !state.critic;
        criticToggle.setAttribute('aria-checked', state.critic ? 'true' : 'false');
        criticToggle.classList.toggle('is-on', state.critic);
        if (criticModeSelect) criticModeSelect.classList.toggle('hidden', !state.critic);
        saveDraftNow();
      },
    },
    h('span', { class: 'cmp-toggle__track', 'aria-hidden': 'true' }, h('span', { class: 'cmp-toggle__knob' })),
    h('span', { class: 'cmp-toggle__label' }, 'С критиком'))
    : null;
  if (criticToggle) criticToggle.classList.toggle('is-on', state.critic);

  /* Режим проверки при постановке запроса (W13-E §9): 1 / 2 / до полного одобрения.
     По умолчанию — из настроек комнаты; смена действует только на этот запрос. */
  const roomMode = String((room && room.critic_mode) || 'rounds');
  const criticModeSelect = room && room.critic
    ? h('select', {
      class: 'select cmp-critic-mode' + (state.critic ? '' : ' hidden'),
      id: 'cmp-critic-mode',
      'aria-label': 'Сколько раз проверять критиком',
      title: 'Раунд — один цикл «критик проверил → доработали → проверили снова»',
    },
    h('option', { value: '1' }, '1 раунд — быстро'),
    h('option', { value: '2' }, '2 раунда — тщательнее'),
    h('option', { value: 'until_approved' }, 'До полного одобрения'),
    )
    : null;
  if (criticModeSelect) {
    criticModeSelect.value = roomMode === 'until_approved'
      ? 'until_approved'
      : String(Math.min(2, Math.max(1, Number(room.max_rounds) || 1)));
    criticModeSelect.addEventListener('change', () => { saveDraftNow(); });
  }

  const pathInput = h('input', {
    class: 'input', type: 'text', placeholder: 'D:/Проекты/Комната/STATE.md',
    'aria-label': 'Путь к файлу',
    'data-mic': 'off',
  });

  const attachBtn = h('button', {
    class: 'btn btn--ghost btn--icon btn--sm cmp-attach-btn', type: 'button',
    title: 'Прикрепить путь к файлу', 'aria-label': 'Прикрепить путь к файлу',
    'aria-expanded': 'false',
  }, icon('attach', { size: 14 }));

  const attachAdd = h('button', { class: 'btn btn--secondary btn--sm', type: 'button' }, 'Добавить');
  const attachRow = h('div', { class: 'cmp-attach hidden' },
    h('div', { class: 'row cmp-attach__row' }, pathInput, attachAdd),
    h('div', { class: 'field__hint' }, 'Абсолютный путь из разрешённых папок: папка данных приложения, хранилище Obsidian, папки проекта.'),
  );
  const attachList = h('div', { class: 'cmp-attach__chips' });
  attachRow.appendChild(attachList);

  const sendBtn = h('button', { class: 'btn btn--primary cmp-send', type: 'submit' });

  // Главный сценарий Михаила: микрофон рядом с «Отправить», заметнее прочих.
  const micBtn = createMic(textarea, { label: 'Надиктовать', className: 'cmp-mic' });

  const dictNote = h('div', { class: 'cmp-dict' },
    'Надиктуйте — текст встанет туда, где курсор. Ctrl+M — начать и остановить.');

  attachBtn.addEventListener('click', () => {
    state.attachOpen = !state.attachOpen;
    attachRow.classList.toggle('hidden', !state.attachOpen);
    attachBtn.setAttribute('aria-expanded', state.attachOpen ? 'true' : 'false');
    if (state.attachOpen) pathInput.focus();
  });

  const bar = h('div', { class: 'cmp-bar' },
    criticToggle, criticModeSelect, attachBtn,
    h('div', { class: 'cmp-bar__right' }, counter, micBtn, sendBtn),
  );

  const field = h('div', { class: 'cmp-field' }, textarea, bar);

  /* Бриф проекта: флажок виден только в комнате с проектным режимом. */
  const briefInput = h('input', { type: 'checkbox', id: 'cmp-brief' });
  briefInput.checked = state.brief;
  const briefHint = h('div', { class: 'cmp-brief__hint hidden' }, BRIEF_HINT);
  const briefWrap = h('div', { class: 'cmp-brief' + (projectMode ? '' : ' hidden') },
    h('label', {
      class: 'checkbox cmp-brief__check',
      for: 'cmp-brief',
      title: 'Главный запрос проекта: руководитель комнаты предложит карту частей работы',
    }, briefInput, h('span', null, BRIEF_LABEL)),
    briefHint);
  briefInput.addEventListener('change', () => {
    state.brief = Boolean(briefInput.checked);
    state.briefTouched = true;
    briefHint.classList.toggle('hidden', !state.brief);
    saveDraftNow();
  });

  const hint = h('div', { class: 'cmp-hint' });
  const draftNote = h('div', { class: 'cmp-draft hidden' });
  const warnHost = h('div', { class: 'cmp-warn-host' });
  const errHost = h('div', { class: 'cmp-err-host' });
  const ideasHost = h('div', { class: 'cmp-ideas hidden' });

  const form = h('form', { class: 'cmp-form', novalidate: true },
    field, briefWrap, longNote, dictNote, hint, ideasHost, draftNote, attachRow);

  const compose = h('section', { class: 'cmp-compose', 'aria-labelledby': 'cmp-input' },
    heading, form, warnHost, errHost);

  el.appendChild(root);
  root.appendChild(compose);

  /* ── лента и кандидаты — отдельный модуль ─────────────────────────────── */

  const feed = createFeed({ api, roomId, toast, insertText });

  /* ─────────────────────────────────────────────────────── поле запроса */

  function autosize() {
    // Минимум — три строки (CSS), максимум — ~40 % высоты окна.
    const max = Math.max(140, Math.round(window.innerHeight * 0.40));
    textarea.style.height = 'auto';
    const next = Math.min(textarea.scrollHeight + 2, max);
    textarea.style.height = next + 'px';
    textarea.style.overflowY = textarea.scrollHeight > max ? 'auto' : 'hidden';
  }

  function updateCounter() {
    const n = textarea.value.length;
    counter.textContent = n.toLocaleString('ru-RU') + ' ' + plural(n, ['знак', 'знака', 'знаков']);
    counter.classList.toggle('cmp-count--warn', n >= LONG_TEXT);
    longNote.classList.toggle('hidden', n < LONG_TEXT);
    sendBtn.disabled = state.sending || !textarea.value.trim();
    sendBtn.setAttribute('title', 'Отправить запрос');
  }

  const draftKey = 'draft.' + roomId;

  function saveDraftNow() {
    if (!textarea.value.trim() && !state.attach.length) {
      lsSet(draftKey, undefined);
      return;
    }
    lsSet(draftKey, {
      text: textarea.value, attach: state.attach.slice(), critic: state.critic,
      brief: state.brief, briefTouched: state.briefTouched, at: Date.now(),
    });
  }
  const saveDraftSoon = debounce(saveDraftNow, 350);

  function renderDraftNote() {
    if (!state.draftSavedAt) { draftNote.classList.add('hidden'); draftNote.replaceChildren(); return; }
    draftNote.classList.remove('hidden');
    draftNote.replaceChildren(
      icon('history', { size: 12 }),
      h('span', null, 'Черновик сохранён ' + timeAgo(state.draftSavedAt)),
      h('button', {
        class: 'btn btn--link btn--sm', type: 'button',
        onclick: async () => {
          const ok = await confirmDialog({
            title: 'Очистить черновик?',
            text: 'Текст запроса и вложения будут удалены из этого браузера.',
            confirmLabel: 'Очистить', danger: true,
          });
          if (!ok) return;
          lsSet(draftKey, undefined);
          textarea.value = '';
          state.attach = [];
          state.draftSavedAt = 0;
          renderAttach(); updateCounter(); autosize(); renderDraftNote();
          textarea.focus();
        },
      }, 'Очистить'),
    );
  }

  function insertText(text) {
    const src = String(text || '').trim();
    if (!src) return;
    const start = textarea.selectionStart == null ? textarea.value.length : textarea.selectionStart;
    const end = textarea.selectionEnd == null ? start : textarea.selectionEnd;
    const before = textarea.value.slice(0, start);
    const after = textarea.value.slice(end);
    const glue = before && !/\s$/.test(before) ? '\n' : '';
    textarea.value = before + glue + src + after;
    const pos = (before + glue + src).length;
    try { textarea.setSelectionRange(pos, pos); } catch { /* ignore */ }
    textarea.focus();
    updateCounter(); autosize(); saveDraftSoon(); renderDraftNote();
    compose.scrollIntoView({ block: 'nearest', behavior: 'smooth' });
  }

  textarea.addEventListener('input', () => {
    updateCounter(); autosize(); saveDraftSoon();
    state.draftSavedAt = Date.now(); renderDraftNote();
  });
  textarea.addEventListener('keydown', (ev) => {
    if (ev.key === 'Enter' && (ev.ctrlKey || ev.metaKey)) {
      ev.preventDefault();
      submit();
    }
  });

  /* ──────────────────────────────────────────────────────── вложения */

  function addAttach(raw) {
    const path = String(raw || '').trim();
    if (!path) return;
    if (state.attach.includes(path)) { toast('Этот путь уже прикреплён', 'info'); return; }
    if (state.attach.length >= MAX_ATTACH) { toast('Не больше 10 вложений', 'warn'); return; }
    if (path.length > 500) { toast('Слишком длинный путь', 'warn'); return; }
    state.attach.push(path);
    pathInput.value = '';
    renderAttach(); saveDraftNow();
  }

  function renderAttach() {
    attachList.replaceChildren();
    if (!state.attach.length) return;
    for (const path of state.attach) {
      attachList.appendChild(h('div', { class: 'cmp-attach__item' },
        pathChip(path),
        h('button', {
          class: 'btn btn--ghost btn--icon btn--sm', type: 'button',
          title: 'Убрать «' + path + '»', 'aria-label': 'Убрать «' + path + '»',
          onclick: () => { state.attach = state.attach.filter((p) => p !== path); renderAttach(); saveDraftNow(); },
        }, icon('close', { size: 12 })),
      ));
    }
  }

  attachAdd.addEventListener('click', () => addAttach(pathInput.value));
  pathInput.addEventListener('keydown', (ev) => {
    if (ev.key === 'Enter') { ev.preventDefault(); addAttach(pathInput.value); }
    if (ev.key === 'Escape') {
      state.attachOpen = false;
      attachRow.classList.add('hidden');
      attachBtn.setAttribute('aria-expanded', 'false');
    }
  });

  /* ─────────────────────────────────────────────────────── подсказка */

  /** Одно тихое предложение: руководитель комнаты раздаст запрос команде · аватары. */
  function renderHint() {
    hint.replaceChildren();
    const director = (room && room.orchestrator) || '';
    const name = personName(director);
    const roleWord = name === 'director' || name === 'default' ? '' : name;

    const members = Array.isArray(room && room.members) ? room.members : [];
    const names = [];
    for (const m of members) {
      const p = m && (m.profile || m.name);
      if (p && !names.includes(p)) names.push(p);
    }
    if (room && room.critic && !names.includes(room.critic)) names.push(room.critic);

    hint.appendChild(h('span', {
      class: 'cmp-hint__sentence truncate',
      title: director ? 'Руководитель комнаты: ' + director : null,
    }, 'Руководитель комнаты', roleWord ? ' ' : '', roleWord ? h('span', { class: 'cmp-hint__who' }, roleWord) : null, ' раздаст запрос команде'));

    if (names.length) {
      hint.appendChild(h('span', { class: 'cmp-hint__sep', 'aria-hidden': 'true' }, '·'));
      const team = h('span', { class: 'cmp-hint__team', title: 'Команда: ' + names.join(', ') });
      for (const n of names.slice(0, 5)) team.appendChild(shortAvatar(n));
      if (names.length > 5) {
        team.appendChild(h('span', { class: 'avatar avatar--sm avatar--stacked cmp-hint__more' }, '+' + (names.length - 5)));
      }
      hint.appendChild(team);
    }
  }

  /** Аватар с одной буквой: «MA/BO/BD» из системных id в тексте не нужны. */
  function shortAvatar(raw) {
    const name = personName(raw) || raw;
    const a = avatar(name, { size: 'sm', title: raw });
    a.textContent = (Array.from(String(name))[0] || '?').toUpperCase();
    return a;
  }

  /* ─────────────────────────────────────────────────────────── отправка */

  /**
   * Пустая комната — бриф по умолчанию: главный запрос проекта.
   * Как только запросы есть, флажок включается только руками.
   */
  function applyBriefDefault() {
    if (!projectMode) return;
    const next = briefDefaultOn({
      projectMode,
      emptyRoom: feed.count() === 0,
      touched: state.briefTouched,
      brief: state.brief,
    });
    if (next === state.brief) return;
    state.brief = next;
    briefInput.checked = next;
    briefHint.classList.toggle('hidden', !next);
  }

  function renderSend() {
    sendBtn.replaceChildren();
    if (state.sending) {
      sendBtn.appendChild(icon('loading', { size: 14, className: 'codicon-modifier-spin' }));
      sendBtn.appendChild(h('span', null, 'Отправляю…'));
      sendBtn.classList.add('is-busy');
    } else {
      sendBtn.appendChild(icon('send', { size: 14 }));
      sendBtn.appendChild(h('span', { class: 'cmp-send__label' }, 'Отправить'));
      sendBtn.appendChild(h('kbd', { class: 'kbd cmp-send__kbd' }, 'Ctrl↵'));
      sendBtn.classList.remove('is-busy');
    }
    sendBtn.disabled = state.sending || !textarea.value.trim();
    if (criticToggle) criticToggle.classList.toggle('hidden', state.sending);
  }

  function renderWarning() {
    warnHost.replaceChildren();
    if (!pendingWarning) return;
    const msg = String(pendingWarning.message
      || 'Запрос создан, но движок не запущен — работа не начнётся сама.');
    warnHost.appendChild(h('div', { class: 'banner banner--warn', role: 'status' },
      icon('warning', { size: 14 }),
      h('div', { class: 'banner__text' }, msg),
      h('button', { class: 'btn btn--ghost btn--sm', type: 'button', onclick: () => { pendingWarning = null; renderWarning(); } }, 'Скрыть'),
    ));
  }

  function renderSubmitError() {
    errHost.replaceChildren();
    if (!state.submitError) return;
    errHost.appendChild(errorBanner(state.submitError, () => { state.submitError = null; renderSubmitError(); submit(); }));
  }

  async function submit() {
    const text = textarea.value.trim();
    if (state.sending) return;
    // До очистки поля: если Михаил поправил распознанное — предложим запомнить.
    try { micBtn.mic.learnNow(); } catch { /* необязательная подсказка */ }
    if (!text) { toast('Введите текст запроса', 'info'); textarea.focus(); return; }
    state.sending = true;
    state.submitError = null;
    renderSubmitError(); renderSend();
    try {
      const res = await api.post('/api/rooms/' + enc(roomId) + '/requests', {
        text,
        critic: Boolean(state.critic),
        // Режим проверки: 1 / 2 раунда либо «до полного одобрения» (W13-E §9).
        ...(state.critic && criticModeSelect
          ? (criticModeSelect.value === 'until_approved'
            ? { critic_mode: 'until_approved' }
            : { critic_mode: 'rounds', max_rounds: Number(criticModeSelect.value) || 1 })
          : {}),
        attach: state.attach.slice(),
        // «Это бриф проекта»: поле уходит только в проектной комнате.
        ...briefPayload({ projectMode, brief: state.brief }),
      });
      const rootId = res && (res.root_id || (res.request && res.request.id));
      pendingWarning = res && res.warning ? res.warning : null;
      lsSet(draftKey, undefined);
      textarea.value = '';
      state.attach = [];
      state.draftSavedAt = 0;
      renderAttach(); updateCounter(); autosize(); renderDraftNote();
      toast('Запрос принят', 'ok');
      if (pendingWarning) toast(plainTerms(pendingWarning.message || 'Движок не запущен — работа ждёт.'), 'warn');
      if (rootId) {
        ctx.navigate('#/room/' + enc(roomId) + '/request/' + enc(rootId));
        return;
      }
      await feed.load();
    } catch (err) {
      state.submitError = err;
      renderSubmitError();
      toast(err && err.message ? err.message : 'Не удалось отправить запрос', 'danger');
    } finally {
      state.sending = false;
      renderSend();
    }
  }

  form.addEventListener('submit', (ev) => { ev.preventDefault(); submit(); });

  /* ───────────────────────────────────────────────────────── идеи/бэклог */

  async function loadIdeas() {
    try {
      const data = await api.get('/api/rooms/' + enc(roomId) + '/backlog?kind=future_question&status=open&limit=50');
      const items = Array.isArray(data) ? data : (data && Array.isArray(data.items) ? data.items : []);
      state.ideas = items.filter((i) => String(i.kind) === 'future_question' && String(i.status) === 'open').slice(0, 3);
    } catch {
      state.ideas = [];
    }
    renderIdeas();
  }

  /** Чипы-идеи — одной строкой, до трёх, с обрезкой по ширине. */
  function renderIdeas() {
    ideasHost.replaceChildren();
    if (!state.ideas.length) { ideasHost.classList.add('hidden'); return; }
    ideasHost.classList.remove('hidden');
    ideasHost.appendChild(h('span', { class: 'cmp-ideas__label' }, 'Идеи'));
    for (const idea of state.ideas) {
      const text = String(idea.text || '');
      const chip = h('button', {
        class: 'chip chip--outline cmp-idea', type: 'button',
        title: 'Вставить в поле запроса: ' + text,
      // B8: чип обрезается многоточием — полный текст остаётся в подсказке.
      }, icon('lightbulb', { size: 11 }), h('span', { class: 'cmp-idea__text', title: text }, text));
      chip.addEventListener('click', () => insertText(text));
      ideasHost.appendChild(chip);
    }
  }

  /* ────────────────────────────────────────────────────────────── лента */

  for (const node of feed.sections) root.appendChild(node);

  /* ────────────────────────────────────────────────────────────── опрос */

  let timer = null;
  let stopped = false;

  function schedule(delay) {
    if (stopped) return;
    clearTimeout(timer);
    timer = setTimeout(tick, delay);
  }

  function nextDelay() {
    return feed.hasActive() ? POLL_ACTIVE : POLL_IDLE;
  }

  async function tick() {
    if (stopped) return;
    if (typeof document !== 'undefined' && document.hidden) { schedule(POLL_IDLE); return; }
    await feed.load();
    schedule(nextDelay());
  }

  function onVisible() {
    if (typeof document !== 'undefined' && !document.hidden && !stopped) {
      clearTimeout(timer);
      tick();
    }
  }

  function onResize() { autosize(); }

  document.addEventListener('visibilitychange', onVisible);
  window.addEventListener('resize', onResize);

  /* ─────────────────────────────────────────────────────── первичный вид */

  const draft = lsGet(draftKey, null);
  if (draft && typeof draft === 'object' && typeof draft.text === 'string' && draft.text) {
    textarea.value = draft.text;
    if (Array.isArray(draft.attach)) state.attach = draft.attach.filter((p) => typeof p === 'string').slice(0, MAX_ATTACH);
    if (typeof draft.critic === 'boolean' && room && room.critic) {
      state.critic = draft.critic;
      if (criticToggle) {
        criticToggle.setAttribute('aria-checked', draft.critic ? 'true' : 'false');
        criticToggle.classList.toggle('is-on', draft.critic);
      }
    }
    // Флажок брифа помним вместе с черновиком: Михаил не должен включать заново.
    if (typeof draft.brief === 'boolean' && projectMode) {
      state.brief = draft.brief;
      state.briefTouched = draft.briefTouched === true;
      briefInput.checked = draft.brief;
      briefHint.classList.toggle('hidden', !draft.brief);
    }
    state.draftSavedAt = Number(draft.at || Date.now());
  }

  updateCounter();
  autosize();
  renderSend();
  renderAttach();
  renderDraftNote();
  renderHint();
  renderWarning();
  renderIdeas();

  feed.load().then(applyBriefDefault);
  loadIdeas();
  schedule(nextDelay());

  return {
    unmount() {
      stopped = true;
      clearTimeout(timer);
      saveDraftSoon.cancel();
      saveDraftNow();
      document.removeEventListener('visibilitychange', onVisible);
      window.removeEventListener('resize', onResize);
      feed.destroy();
      root.remove();
    },
  };
}

export default { mount };
