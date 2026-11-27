/**
 * views/composer.js — вкладка «Запрос»: поле под диктовку, тонкая панель
 * действий, строка «кто работает» и идеи. Лента и кандидаты — в composer-feed.js.
 *
 * Проверка критиком — ОДИН селектор (W20-U4, P10): «Без критика / 1 раунд —
 * быстро / 2 раунда / До полного одобрения». Он же решает, уйдёт ли запрос с
 * критиком: значения переводятся в прежние поля API (`critic`, `critic_mode`,
 * `max_rounds`) функцией criticPayload(). «Отправить» на пустом поле неактивна и
 * объясняет почему; аватары команды подсказывают «имя — роль».
 *
 * Данные: POST /api/rooms/{id}/requests, GET /api/rooms/{id}, GET /api/rooms/{id}/backlog.
 * Живое обновление ленты — общий таймер здесь (3 с при активных, иначе 15 с).
 *
 * Вложения (W28-A, дефект 4): скрепка «Прикрепить файлы» открывает системное окно
 * выбора файлов Windows (несколько за раз) через серверную ручку POST
 * /api/files/pick — браузер полного пути из `<input type=file>` не отдаёт. Папки
 * и расширения не ограничены: проверяем только «файл есть, это файл, читается»
 * (POST /api/files/attach-check). Секреты (.env, ключи, токены) — с явным «Всё
 * равно прикрепить». У чипа: имя, размер, «Открыть», «Открыть в папке», «Убрать».
 * Поле пути остаётся запасным путём (несколько путей — по одному на строку).
 */

import { h, timeAgo, debounce, plainTerms, plural } from '../util.js';
import { icon } from '../icons.js';
import {
  ensureCss, avatar, emptyState, errorBanner, confirmDialog,
  lsGet, lsSet,
} from '../ui.js';
import { createMic } from '../dictation.js';
import { participantLabel, ensureProfileDirectory, profileEntry, memberOf } from '../participant-label.js';
import { createFeed } from './composer-feed.js';
import { BRIEF_LABEL, BRIEF_HINT, briefDefaultOn, briefPayload } from './plan-req.js';
// W30-F: один общий компонент прикрепления файлов (скрепка → окно Windows).
import { createAttachPicker } from '../attach-picker.js';

/**
 * Проверка критиком — ОДИН список (P10): раньше рядом стояли переключатель
 * «С критиком» и отдельный селектор раундов, который появлялся/исчезал. Человек
 * видел два управления об одном и не понимал, что выключит ответ из проверки.
 * Теперь один селектор: «Без критика / 1 раунд — быстро / 2 раунда / До полного
 * одобрения». Значения селектора остаются совместимыми с API запроса
 * (`critic`, `critic_mode`, `max_rounds`).
 */
export const CRITIC_CHOICES = [
  { value: 'off', label: 'Без критика', hint: 'Ответ уйдёт без проверки критиком' },
  { value: '1', label: '1 раунд — быстро', hint: 'Один круг: критик проверил — доработали — отдали вам' },
  { value: '2', label: '2 раунда', hint: 'Два круга проверки: тщательнее, но дольше' },
  { value: 'until_approved', label: 'До полного одобрения', hint: 'Критик проверяет, пока не скажет «принято»' },
];

/** Пояснение к селектору: одно слово «раунд» и один смысл. */
export const CRITIC_HINT = 'Раунд — один цикл «критик проверил → доработали → проверили снова»';

/**
 * Что показать в селекторе для этой комнаты: её же настройки, но с явным «Без
 * критика». Критик не настроен или проверка в комнате выключена → «Без критика».
 * @param {object|null} room
 * @returns {'off'|'1'|'2'|'until_approved'}
 */
export function criticChoiceOf(room) {
  const r = room && typeof room === 'object' ? room : {};
  if (!r.critic || r.critic_pass !== true) return 'off';
  if (String(r.critic_mode || 'rounds') === 'until_approved') return 'until_approved';
  const rounds = Math.min(2, Math.max(1, Number(r.max_rounds) || 1));
  return String(rounds);
}

/**
 * Тело запроса для выбранного варианта — ровно те поля, что ждёт API.
 * @param {string} choice значение селектора
 * @returns {{critic: boolean, critic_mode?: string, max_rounds?: number}}
 */
export function criticPayload(choice) {
  const value = String(choice === null || choice === undefined ? 'off' : choice);
  if (value === 'until_approved') return { critic: true, critic_mode: 'until_approved' };
  const rounds = Number(value);
  if (!Number.isFinite(rounds) || rounds < 1) return { critic: false };
  return { critic: true, critic_mode: 'rounds', max_rounds: Math.min(2, Math.round(rounds)) };
}

/** Подпись варианта (для подсказок и тестов). */
export function criticChoiceLabel(choice) {
  const value = String(choice === null || choice === undefined ? 'off' : choice);
  const hit = CRITIC_CHOICES.find((c) => c.value === value);
  return hit ? hit.label : CRITIC_CHOICES[0].label;
}

/** Мягкая подсказка: длинный запрос лучше разбить, но поле не ограничиваем. */
const LONG_TEXT = 100000;
const POLL_ACTIVE = 3000;
const POLL_IDLE = 15000;

const enc = (s) => encodeURIComponent(String(s == null ? '' : s));

/** Предупреждение сервера после отправки — живёт до закрытия или перезагрузки. */
let pendingWarning = null;

/**
 * Человеческое имя исполнителя: «team-ops» → «ops», «mikhail» → «Михаил».
 * Полный id остаётся в title — в тексте интерфейса системных имён нет.
 * @param {string} id
 * @returns {string}
 */
export function personName(id) {
  const raw = String(id == null ? '' : id).trim();
  if (!raw) return '';
  if (raw === 'mikhail') return 'Михаил';
  return raw.replace(/^(?:biz|team|room)-/, '');
}

/**
 * @param {HTMLElement} el
 * @param {object} ctx
 * @returns {Promise<{unmount: Function}>}
 */
export async function mount(el, ctx) {
  await ensureCss('css/view-composer.css');
  // Чипы вложений (W30-F): стили общего компонента прикрепления.
  await ensureCss('css/attach-picker.css');
  // Стили проектного режима: флажок брифа, бейджи, плашка карты.
  await ensureCss('css/plan-req.css');

  const api = ctx.api;
  const toast = typeof ctx.toast === 'function' ? ctx.toast : () => {};
  // W31-B1: блок «Задача команде» живёт во вкладке «Задачи» над доской. В этом
  // случае композер монтируется без своего заголовка и без ленты запросов
  // (лента — прошлые задачи, их место на доске), а после отправки не уводит
  // со страницы: хозяин экрана сам перечитает доску (`ctx.onSent`).
  const embedded = Boolean(ctx && ctx.embed);
  const showHeading = !embedded && ctx.heading !== false;
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
    // Выбор проверки критиком живёт в селекторе `criticValue` (P10): отдельного
    // да/нет больше нет — «Без критика» это просто первый вариант списка.
    // Вложения — объекты чипа: {path, name, size, ext, ok, code, message, secret,
    // confirmed}. `ok: null` — идёт проверка, `false` — файл не принят (причина в
    // `message`), `secret` — файл похож на секрет и ждёт «Всё равно прикрепить».
    attach: [],
    ideas: [],
    draftSavedAt: 0,
    brief: false,
    briefTouched: false,
  };

  /* ─────────────────────────────────────────────────────────────── каркас */

  const root = h('div', { class: embedded ? 'cmp cmp--embed' : 'view__inner cmp' });

  const heading = h('div', { class: 'cmp-head' },
    h('h2', { class: 'cmp-head__title' }, 'Задача команде'),
    h('p', { class: 'cmp-head__sub' }, 'Одна задача — один сводный ответ.'));

  const textarea = h('textarea', {
    class: 'textarea cmp-input',
    rows: 3,
    id: 'cmp-input',
    placeholder: 'Напишите или надиктуйте задачу. Можно длинно — текст встанет туда, где курсор.',
    'aria-label': 'Текст задачи',
    spellcheck: 'false',
    'data-mic': 'manual',
  });

  // Тихий счётчик: без верхней границы, только «сколько уже наговорено».
  const counter = h('span', { class: 'cmp-count tnum', 'aria-live': 'off' }, '0 знаков');
  const longNote = h('div', { class: 'cmp-long hidden', role: 'status' },
    'Очень длинная задача — лучше разбить на части.');

  // Один селектор проверки: «Без критика / 1 раунд — быстро / 2 раунда / До
  // полного одобрения». Прежний переключатель «С критиком» и отдельный список
  // раундов слиты (P10); `criticValue` — единственный источник выбора.
  const hasCritic = Boolean(room && room.critic);
  let criticValue = criticChoiceOf(room);
  const criticSelect = hasCritic
    ? h('select', {
      class: 'select cmp-critic-mode',
      id: 'cmp-critic-mode',
      'aria-label': 'Проверка ответа критиком',
      title: CRITIC_HINT,
    }, ...CRITIC_CHOICES.map((item) => h('option', {
      value: item.value, title: item.hint,
    }, item.label)))
    : null;
  if (criticSelect) {
    criticSelect.value = criticValue;
    criticSelect.addEventListener('change', () => {
      criticValue = criticSelect.value;
      saveDraftNow();
    });
  }
  const criticControl = criticSelect
    ? h('label', { class: 'cmp-critic', title: CRITIC_HINT },
      icon('shield', { size: 13 }), criticSelect)
    : null;

  // W30-F (дефект 15): вложения — ОДИН общий компонент `attach-picker.js`, тот же,
  // что в «Диалоге по запросу». Скрепка открывает окно выбора файлов Windows
  // (POST /api/files/pick), чипы дают «Просмотр / Открыть / Открыть в папке /
  // Убрать», предел 30, секреты — только через «Всё равно прикрепить».
  // Прикрепляется ПУТЬ, а не копия файла. Ручной путь — скрытой ссылкой «Путь»
  // внутри компонента (запасной способ, когда окно недоступно).
  const picker = createAttachPicker({
    api,
    toast,
    prefix: 'cmp-attach',
    title: 'Прикрепить файлы к задаче',
    buttonClass: 'btn btn--ghost btn--sm cmp-attach-btn',
    label: 'Прикрепить файлы',
    onChange: () => { state.attach = picker.paths(); saveDraftNow(); },
    onInsert: (text) => { insertText(text); },
  });
  const attachBtn = picker.button;
  // W28-QA2: чипы живут ВНЕ скрываемой строки пути — иначе при выборе через скрепку
  // (и при восстановлении черновика) файлы были в состоянии «прикреплён», но не видны.
  const attachList = picker.chipsHost;
  const attachRow = picker.row;

  const sendBtn = h('button', { class: 'btn btn--primary cmp-send', type: 'submit' });

  // Главный сценарий Михаила: микрофон рядом с «Отправить», заметнее прочих.
  const micBtn = createMic(textarea, { label: 'Надиктовать', className: 'cmp-mic' });

  const dictNote = h('div', { class: 'cmp-dict' },
    'Надиктуйте — текст встанет туда, где курсор. Ctrl+M — начать и остановить.');

  const pathToggle = picker.toggle;

  const bar = h('div', { class: 'cmp-bar' },
    criticControl, attachBtn, pathToggle,
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
      title: 'Главная задача проекта: руководитель комнаты предложит карту частей работы',
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
    field, attachList, briefWrap, longNote, dictNote, hint, ideasHost, draftNote, attachRow);

  const compose = h('section', { class: 'cmp-compose', 'aria-labelledby': 'cmp-input' },
    showHeading ? heading : null, form, warnHost, errHost);

  el.appendChild(root);
  root.appendChild(compose);

  /* ── лента и кандидаты — отдельный модуль ─────────────────────────────── */

  // Встроенный вид (вкладка «Задачи») ленты не показывает: прошлые задачи уже
  // на доске. Заглушка отвечает на все вопросы, которые задаёт экран.
  const feed = embedded
    ? { sections: [], load: async () => {}, count: () => 0, hasActive: () => false, destroy: () => {} }
    : createFeed({ api, roomId, toast, insertText });

  /* ─────────────────────────────────────────────────────── поле запроса */

  function autosize() {
    // Минимум — три строки (CSS), максимум — ~40 % высоты окна.
    const max = Math.max(140, Math.round(window.innerHeight * 0.40));
    textarea.style.height = 'auto';
    const next = Math.min(textarea.scrollHeight + 2, max);
    textarea.style.height = next + 'px';
    textarea.style.overflowY = textarea.scrollHeight > max ? 'auto' : 'hidden';
  }

  /** Состояние кнопки «Отправить» — одно место: пусто → неактивна и объясняет почему. */
  function syncSendState() {
    const empty = !textarea.value.trim();
    sendBtn.disabled = state.sending || empty;
    sendBtn.setAttribute('aria-disabled', sendBtn.disabled ? 'true' : 'false');
    sendBtn.setAttribute('title', empty
      ? 'Напишите или надиктуйте задачу — кнопка станет активной'
      : 'Отправить задачу (Ctrl+Enter)');
  }

  function updateCounter() {
    const n = textarea.value.length;
    counter.textContent = n.toLocaleString('ru-RU') + ' ' + plural(n, ['знак', 'знака', 'знаков']);
    counter.classList.toggle('cmp-count--warn', n >= LONG_TEXT);
    longNote.classList.toggle('hidden', n < LONG_TEXT);
    syncSendState();
  }

  const draftKey = 'draft.' + roomId;

  function saveDraftNow() {
    if (!textarea.value.trim() && !state.attach.length) {
      lsSet(draftKey, undefined);
      return;
    }
    lsSet(draftKey, {
      text: textarea.value,
      // В черновике — пути и отдельно те секреты, которые человек уже подтвердил.
      attach: picker.paths(),
      attachConfirmed: picker.confirmedPaths(),
      // Выбор проверки помним вместе с черновиком: `criticChoice` — новый ключ,
      // `critic` оставлен для старых черновиков и внешних читателей.
      critic: criticValue !== 'off', criticChoice: criticValue,
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
            text: 'Текст задачи и вложения будут удалены из этого браузера.',
            confirmLabel: 'Очистить', danger: true,
          });
          if (!ok) return;
          lsSet(draftKey, undefined);
          textarea.value = '';
          state.attach = [];
          picker.clear();
          state.draftSavedAt = 0;
          updateCounter(); autosize(); renderDraftNote();
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

  // Всё прикрепление (скрепка → окно Windows, чипы, проверка секретов, предел 30,
  // запасной ручной путь) живёт в общем компоненте `web/js/attach-picker.js`.
  // Здесь остались только привязки к экрану: черновик и проверка перед отправкой.


  /* ─────────────────────────────────────────────────────── подсказка */

  /** Одно тихое предложение: руководитель комнаты раздаст запрос команде · аватары. */
  function renderHint() {
    hint.replaceChildren();
    const director = (room && room.orchestrator) || '';
    const directorLabel = director ? teamLabel(director) : null;
    const roleWord = directorLabel && directorLabel.short ? directorLabel.short : '';

    const members = Array.isArray(room && room.members) ? room.members : [];
    const names = [];
    for (const m of members) {
      const p = m && (m.profile || m.name);
      if (p && !names.includes(p)) names.push(p);
    }
    if (room && room.critic && !names.includes(room.critic)) names.push(room.critic);

    hint.appendChild(h('span', {
      class: 'cmp-hint__sentence truncate',
      title: directorLabel ? 'Руководитель комнаты: ' + directorLabel.title : null,
    }, 'Руководитель комнаты', roleWord ? ' ' : '', roleWord ? h('span', { class: 'cmp-hint__who' }, roleWord) : null, ' раздаст задачу команде'));

    if (names.length) {
      // Подсказка у каждого аватара — «имя — роль», а не системный id профиля:
      // «Кто это?» должно отвечаться наведением, без открытия «Команды» (P10).
      const labels = names.map((n) => teamLabel(n));
      hint.appendChild(h('span', { class: 'cmp-hint__sep', 'aria-hidden': 'true' }, '·'));
      const team = h('span', {
        class: 'cmp-hint__team',
        title: 'Команда: ' + labels.map((l) => l.title).join(', '),
      });
      for (const label of labels.slice(0, 5)) team.appendChild(shortAvatar(label));
      if (names.length > 5) {
        const rest = labels.slice(5).map((l) => l.title).join(', ');
        team.appendChild(h('span', {
          class: 'avatar avatar--sm avatar--stacked cmp-hint__more',
          title: 'Ещё в команде: ' + rest,
          'aria-label': 'Ещё в команде: ' + rest,
        }, '+' + (names.length - 5)));
      }
      hint.appendChild(team);
    }
  }

  /** Подпись участника «имя — роль»: одна для подсказок и аватаров. */
  function teamLabel(profile) {
    const id = String(profile || '');
    // Участник комнаты может знать только профиль и роль, а имя и должность лежат
    // в каталоге Hermes: складываем оба источника, иначе подсказка — сухая роль.
    const member = memberOf(room, id);
    const entry = profileEntry(id);
    const merged = Object.assign({}, member || {}, {
      name: (member && member.name) || (entry && entry.name) || '',
      job: (member && member.job) || (entry && entry.job) || '',
      label: (member && member.label) || (entry && entry.label) || '',
    });
    const label = participantLabel(id, { member: merged, room });
    const role = String(label.role || '');
    const name = personName(label.short || label.name || '') || label.short || label.title || '';
    // Подпись из каталога («Антон — юрист по праву России») точнее нашей сборки;
    // когда её нет, остаётся «имя — роль», но без повтора «Исполнитель — исполнитель».
    const title = label.source !== 'role' && label.title
      ? label.title
      : (name && role && name.toLowerCase() !== role.toLowerCase() ? name + ' — ' + role : (name || role));
    return { profile: id, name, short: name, role, title };
  }

  /** Аватар с одной буквой и подсказкой: «Анна — исполнитель». */
  function shortAvatar(label) {
    const shown = (label && (label.short || label.name)) || '';
    const title = (label && label.title) || '';
    const a = avatar(shown, { size: 'sm', title });
    a.setAttribute('aria-label', title || shown || 'Участник команды');
    if (title) a.setAttribute('title', title);
    a.textContent = (Array.from(String(shown))[0] || '?').toUpperCase();
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
    // P10: на пустом поле кнопка неактивна и объясняет, чего не хватает —
    // «пустая» кнопка без подсказки выглядит как поломка.
    syncSendState();
    if (criticControl) criticControl.classList.toggle('hidden', state.sending);
  }

  function renderWarning() {
    warnHost.replaceChildren();
    if (!pendingWarning) return;
    const msg = String(pendingWarning.message
      || 'Задача создана, но движок не запущен — работа не начнётся сама.');
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
    if (!text) { toast('Введите текст задачи', 'info'); textarea.focus(); return; }
    // Непринятый файл не отправляем молча: показываем его и причину рядом с полем.
    const broken = picker.blocked();
    if (broken) {
      picker.showRow(true);
      toast(broken.secret
        ? '«' + broken.name + '» похож на секрет — нажмите «Всё равно прикрепить» или уберите файл'
        : (broken.message || 'Уберите файл, который не удалось приложить'),
      'warn');
      return;
    }
    state.sending = true;
    state.submitError = null;
    renderSubmitError(); renderSend();
    try {
      // Проверка критиком: один выбор → ровно те поля, что ждёт API
      // (`critic`, `critic_mode`, `max_rounds`). Обратная совместимость сохранена.
      const criticBody = criticPayload(criticSelect ? criticSelect.value : 'off');
      const res = await api.post('/api/rooms/' + enc(roomId) + '/requests', {
        text,
        ...criticBody,
        attach: picker.paths(),
        // «Всё равно прикрепить»: секрет уходит только с явным подтверждением.
        confirm_secret: picker.confirmedPaths().length > 0,
        // «Это бриф проекта»: поле уходит только в проектной комнате.
        ...briefPayload({ projectMode, brief: state.brief }),
      });
      const rootId = res && (res.root_id || (res.request && res.request.id));
      pendingWarning = res && res.warning ? res.warning : null;
      lsSet(draftKey, undefined);
      textarea.value = '';
      state.attach = [];
      picker.clear();
      state.draftSavedAt = 0;
      updateCounter(); autosize(); renderDraftNote();
      toast('Задача принята', 'ok');
      if (pendingWarning) toast(plainTerms(pendingWarning.message || 'Движок не запущен — работа ждёт.'), 'warn');
      if (typeof ctx.onSent === 'function') {
        // Встроенный блок: остаёмся на доске, хозяин сам её перечитает.
        try { ctx.onSent(rootId || null); } catch { /* хозяин решит сам */ }
        return;
      }
      if (rootId) {
        ctx.navigate('#/room/' + enc(roomId) + '/request/' + enc(rootId));
        return;
      }
      await feed.load();
    } catch (err) {
      state.submitError = err;
      renderSubmitError();
      toast(err && err.message ? err.message : 'Не удалось отправить задачу', 'danger');
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
        title: 'Вставить в поле задачи: ' + text,
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
    if (stopped || embedded) return;
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
    if (Array.isArray(draft.attach)) {
      const confirmed = new Set(Array.isArray(draft.attachConfirmed) ? draft.attachConfirmed : []);
      picker.set(draft.attach.filter((path) => typeof path === 'string' && path), {
        confirmed: Array.from(confirmed),
      });
    }
    // Выбор проверки из черновика: новый ключ `criticChoice`; старый `critic`
    // (да/нет) понимаем как «раунды комнаты» или «без критика».
    if (criticSelect) {
      const byChoice = String(draft.criticChoice || '');
      if (CRITIC_CHOICES.some((c) => c.value === byChoice)) {
        criticValue = byChoice;
      } else if (typeof draft.critic === 'boolean') {
        criticValue = draft.critic ? criticChoiceOf(Object.assign({}, room, { critic_pass: true })) : 'off';
      }
      criticSelect.value = criticValue;
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
  renderDraftNote();
  renderHint();
  // Подсказки аватаров — «имя — роль»: каталог профилей приходит отдельным запросом,
  // поэтому после его загрузки подпись перерисовывается один раз (P10).
  ensureProfileDirectory(api).then(() => renderHint()).catch(() => {});
  renderWarning();
  renderIdeas();

  if (!embedded) {
    feed.load().then(applyBriefDefault);
    loadIdeas();
    schedule(nextDelay());
  }

  return {
    focus() {
      try { textarea.focus(); } catch { /* фокус не критичен */ }
    },
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
