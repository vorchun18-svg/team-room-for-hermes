/**
 * settings.js — экран «Настройки» (#/settings).
 *
 * Блоки: здоровье (движок, фоновая проверка, пути), внешний вид (тема),
 * уведомления (Notification API + localStorage), «Вопросы и ответы»,
 * горячие клавиши, новая комната.
 *
 * Уведомление о готовом ответе показывает app.js — здесь только настройка и
 * экспорт notifyIfEnabled(title, body).
 */

import { h, fmtDateTime, timeAgo, fmtDuration, plural } from '../util.js';
import { icon } from '../icons.js';
import {
  ensureCss, sectionHeader, skeleton, errorBanner, copyText, segmented,
  field, iconButton, lsGet, lsSet, openPath,
} from '../ui.js';
import { countText, COUNT_FORMS, humanApiMessage } from '../shell-labels.js';
// W27-E4: значения «Стартовой страницы» — из route-memory.js (один источник).
import { START_KEY, START_TODAY, START_OPTIONS, normalizeStartPref } from '../route-memory.js';
import { personName } from './request-util.js';
import {
  KICK_LABEL, KICK_HINT, kickPreview, kickNow, kickUsefulness, kickWhyNot, kickPanel,
} from './kick-panel.js';
// W20-U2 §15 (P11): группировка экрана, сокращение путей, один сводный статус
// и учёт несохранённых изменений — чистые функции, проверены node --test.
import {
  SETTINGS_GROUPS, shortenPathLeft, overallStatus,
  checksSummary, topProblems, CHECK_STATUS, normalizeStatus as normalizeCheckStatus,
  pendingChanges, saveSummary,
} from './settings-groups.js';

const VIEW_CSS = 'css/view-settings.css';
const THEME_KEY = 'teamroom.theme';
const NOTIFY_KEY = 'notify';
const HEALTH_POLL_MS = 20000;

/**
 * Имя пояса из Windows («Восточное Ю-Ам. время (зима)») → понятное человеку:
 * город и смещение. Неизвестное имя оставляем как есть, но с UTC-смещением.
 * @param {string} raw
 * @returns {string}
 */
export function tzLabel(raw) {
  const text = String(raw === null || raw === undefined ? '' : raw).trim();
  if (!text) return '';
  const offsetMin = -new Date().getTimezoneOffset();
  const sign = offsetMin >= 0 ? '+' : '−';
  const abs = Math.abs(offsetMin);
  const hours = Math.floor(abs / 60);
  const rest = abs % 60;
  const offset = 'UTC' + sign + hours + (rest ? ':' + String(rest).padStart(2, '0') : '');
  if (/Ю-Ам|Южная Америка|Brasilia|Bras[ií]lia|S[ãa]o[\s_-]*Paulo|E\. South America/i.test(text)) {
    return 'Бразилиа (' + offset + ')';
  }
  if (/Москва|Moscow|Russian Standard/i.test(text)) return 'Москва (' + offset + ')';
  if (/UTC|GMT/i.test(text)) return text;
  return text + ' (' + offset + ')';
}

/* -------------------------------------------------------------- уведомления */

/**
 * Показать системное уведомление, если пользователь включил их в настройках
 * и браузер разрешил. Никогда не бросает.
 * @param {string} title
 * @param {string} [body]
 * @returns {boolean} показали ли
 */
export function notifyIfEnabled(title, body) {
  try {
    if (lsGet(NOTIFY_KEY, false) !== true) return false;
    if (typeof Notification === 'undefined') return false;
    if (Notification.permission !== 'granted') return false;
    const n = new Notification(String(title || 'Комната команды'), {
      body: String(body === null || body === undefined ? '' : body),
      tag: 'teamroom-ready',
      silent: false,
    });
    if (n && typeof n.onclick === 'function') {
      n.onclick = () => { try { window.focus(); n.close(); } catch { /* ignore */ } };
    }
    return true;
  } catch {
    return false;
  }
}

/* ---------------------------------------------------------------- тема */

/**
 * Применить тему к странице.
 *
 * `persist: false` — предпросмотр в «Настройках» (W20-U2 §15): экран и подпись
 * в шапке меняются сразу, а в браузер ничего не пишется до «Сохранить настройки».
 * @param {string} pref
 * @param {{persist?: boolean}} [opts]
 * @returns {string} применённое значение
 */
function applyTheme(pref, { persist = true } = {}) {
  const value = ['auto', 'light', 'dark'].includes(pref) ? pref : 'auto';
  try { document.documentElement.dataset.theme = value; } catch { /* ignore */ }
  if (persist) {
    try { localStorage.setItem(THEME_KEY, value); } catch { /* приватный режим */ }
  }
  return value;
}

/* ------------------------------------------------------------- мелкие чипы */

function toneChip(ok, okLabel, badLabel, title) {
  return h('span', { class: 'chip ' + (ok ? 'chip--ok' : 'chip--danger'), title: title || undefined },
    h('span', { class: 'chip__dot' }), ok ? okLabel : badLabel);
}

function stateChip(state) {
  const map = {
    running: ['chip--ok', 'работает'],
    stopped: ['chip--warn', 'остановлен'],
    stale: ['chip--warn', 'устарел (pid мёртв)'],
    unknown: ['chip--muted', 'неизвестно'],
  };
  const meta = map[String(state || 'unknown')] || map.unknown;
  return h('span', { class: 'chip ' + meta[0], title: 'Состояние движка' },
    h('span', { class: 'chip__dot' + (state === 'running' ? ' chip__dot--pulse' : '') }), meta[1]);
}

/* --------------------------------------------------------------- FAQ */

/** Частые вопросы → короткие ответы и ссылки на экраны. */
const FAQ = [
  { q: 'Где ответ на мою задачу?', a: 'На странице задачи: блок «Ответ» вверху. В ленте задач у готовой задачи стоит чип «готово». Откройте задачу из вкладки «Задача».', tab: 'composer', link: 'К задачам' },
  { q: 'Что дальше делать?', a: 'Вкладка «От вас нужно»: шаги с владельцем и сроком, будущие вопросы и то, что ждёт вашего решения. Тот же блок повторён на странице задачи.', tab: 'backlog', link: 'Открыть «От вас нужно»' },
  { q: 'Что от меня ждут?', a: 'Число у комнаты слева и бейдж на вкладке «От вас нужно» — это дела, которые ждут вашего решения: вопросы, блокировки, предложенные решения. Откройте вкладку — ответить можно прямо в строке, ответ уйдёт комментарием и снимет блокировку.', tab: 'backlog', link: 'Что ждёт меня' },
  { q: 'На каком мы этапе?', a: 'Вкладка «Карта проекта»: полоса фаз и решения. Текущая фаза показана чипом в шапке комнаты.', tab: 'roadmap', link: 'Карта проекта' },
  { q: 'Кто что делает сейчас?', a: 'Вкладка «Команда»: у каждого профиля живой статус, текущая задача и последняя работа. Если все простаивают — смотрите группу «Статус» выше.', tab: 'team', link: 'Команда' },
  { q: 'Почему ничего не происходит?', a: 'Чаще всего остановился движок команды — он раздаёт задачи исполнителям. В группе «Статус» видно его состояние и есть кнопка «Проверить доску сейчас»: она один раз запускает готовые задачи, не дожидаясь минутного таймера, и показывает, что именно запустилось. Второй вариант — задача ещё не разобрана на работы: нажмите «Разложить сейчас» на странице задачи (это обращение к модели, только по вашему клику).', tab: 'team', link: 'Кто чем занят' },
  { q: 'Что сказал критик? Можно переделать?', a: 'На странице задачи есть блок «Проверка критика»: вердикт и замечания «где / в чём проблема / что исправить». Кнопка «На доработку» отправляет ответ руководителю комнаты с вашим замечанием — получится новая версия.', tab: 'composer', link: 'К задачам' },
  { q: 'Как попросить интерактивный отчёт?', a: 'Скажите в задаче прямо: «сделай интерактивный отчёт с таблицей и графиком». Команда соберёт страницу отчёта — она появится во вкладке «Результаты» и откроется на весь экран.', tab: 'reports', link: 'Результаты' },
  { q: 'Как добавить агента?', a: 'Вкладка «Команда» → «Добавить»: список профилей Hermes с описаниями. Можно добавить, например, программиста на постоянку — состав комнаты меняется в любой момент.', tab: 'team', link: 'Добавить агента' },
  { q: 'Как завести комнату?', a: 'Блок «Новая комната» ниже: название, руководитель комнаты и, если нужно, критик. После создания откройте блок «Команда» и добавьте исполнителей.', anchor: '#set-newroom', link: 'Новая комната' },
  { q: 'Что было раньше по теме?', a: 'Поиск по всему приложению — Ctrl+K, а также вкладки «Журнал» (хронология событий) и «Карта проекта» (решения).', tab: 'journal', link: 'Журнал' },
  { q: 'Хочу в календарь', a: 'Во вкладке «От вас нужно» у шагов есть срок; кнопка экспорта отдаёт .ics — файл открывается календарём, без внешней сети.', tab: 'backlog', link: 'Шаги и сроки' },
  { q: 'Хочу послать кому-то результат', a: 'На странице задачи — экспорт в .md и в автономный .html: один файл, всё внутри (текст ответа, шаги, решения, отчёт), открывается без сервера.', tab: 'composer', link: 'К задачам' },
  { q: 'Сколько это шло и кто опоздал?', a: 'На странице задачи у задач в дереве работ видны длительности и прогресс N/M по работам. Сроки шагов — во вкладке «От вас нужно».', tab: 'composer', link: 'К задачам' },
  { q: 'Что делает критик?', a: 'После того как руководитель комнаты свёл ответ, критик получает задачу и свод и отвечает вердиктом: «принято» или «на доработку» со списком замечаний. При «на доработку» руководитель комнаты делает новую версию ответа. Сколько раз проверять — задаётся один раз в настройках комнаты, руководитель об этом вас не спрашивает.', tab: 'composer', link: 'К задачам' },
  { q: 'Что такое раунд критика и сколько их ставить?', a: 'Раунд — один цикл «критик проверил → если нашёл замечания, исполнители доработали → снова проверили». 1 раунд — быстро: одна попытка исправить замечания. 2 раунда — тщательнее. Третий вариант «До полного одобрения критиком» — цикл идёт, пока критик не скажет «принято»: удобно оставить на ночь и утром получить готовое. Страховки: остановка, если замечания повторяются три раунда подряд, пауза при лимите провайдера и потолок раундов (по умолчанию 12) — по достижении цикл останавливается с понятным отчётом. Выбирается в настройках комнаты («Изменить…» → «Раунды проверки») и при постановке задачи.', tab: 'team', link: 'Как работает комната' },
  { q: 'Сколько это стоит в токенах?', a: 'Деньги и токены считает Hermes, а не это приложение. Каждая задача запускает агентов только по вашему нажатию «Отправить»: сначала руководитель комнаты разбирает задачу, потом работают исполнители, критик добавляет 1–2 запуска. Посмотреть расход: команда hermes insights --days 7 (токены и стоимость по сессиям) или hermes usage (лимиты). Здесь цифр нет намеренно — мы их не выдумываем.' },
  { q: 'Что если агент ошибся?', a: 'Три пути. «На доработку» на странице задачи: напишите, что не так, — руководитель комнаты перепишет ответ, прежние версии сохраняются. «Переназначить» задачу в дереве работ — её возьмёт другой профиль. «Повторить» на упавшей карточке. Факты всё равно проверяйте: система гарантирует, что ответ собран из работы команды и прошёл критика, а не истинность.' },
  { q: 'Куда смотреть, когда ответ готов?', a: 'Вкладка «Задача»: у готовой задачи чип «готово», откройте его — ответ вверху страницы. Если включены уведомления, придёт системное сообщение. Числа в сайдбаре показывают, сколько дел ждёт вашего решения.' },
  { q: 'Где лежат данные?', a: 'Всё локально: папка data/ (реестр комнат, состояние проекта, отчёты), зеркало для людей в Vault (STATE.md и Журнал.md) и база задач Hermes. Точные пути с кнопкой копирования — в группе «Пути и данные» выше.', anchor: '#set-health', link: 'Пути и данные' },
  { q: 'Это точно локально и безопасно?', a: 'Приложение доступно только на этом компьютере, внешних запросов нет, тексты агентов экранируются, файлы отдаются лишь из белых папок, HTML-отчёты живут в песочнице. Порт и версия — в «Технических подробностях» группы «Статус».', anchor: '#set-health', link: 'Проверить' },
  { q: 'Как запустить? Само запустится?', a: 'Запуск — start-team-room.cmd: поднимает локальный сервер и открывает страницу. Установщик может включить автозапуск сразу; если нет — его включает кнопка «Включить автозапуск» в «Проверке системы». Само приложение ничего не меняет: только по вашему нажатию и с подтверждением.', anchor: '#/settings/diagnostics', link: 'Проверка системы' },
];

const HOTKEYS = [
  { keys: ['/'], text: 'Поиск по всему приложению: задачи, решения, журнал' },
  { keys: ['Ctrl', 'K'], text: 'Командная палитра: команды, комнаты, поиск' },
  { keys: ['↑', '↓'], text: 'Двигаться по списку в палитре и в поиске' },
  { keys: ['Enter'], text: 'Открыть выбранную команду или результат' },
  { keys: ['Esc'], text: 'Закрыть палитру или диалог' },
  { keys: ['Ctrl', 'Enter'], text: 'Отправить задачу из поля задачи' },
];

/* ------------------------------------------------------------- разметка */

function kbdRow(def) {
  return h('div', { class: 'set-key' },
    h('span', { class: 'set-key__keys' }, def.keys.map((k) => h('kbd', { class: 'kbd' }, k))),
    h('span', { class: 'set-key__text' }, def.text),
  );
}

/** Строка «подпись — значение — действия». */
function infoRow(label, value, actions) {
  return h('div', { class: 'set-row' },
    h('span', { class: 'set-row__label' }, label),
    h('span', { class: 'set-row__value' }, value),
    actions ? h('span', { class: 'set-row__actions' }, actions) : null,
  );
}

/**
 * Строка пути: сокращённый вид слева, полный путь — в подсказке и при копировании.
 *
 * Сокращение делает `shortenPathLeft` (W20-U2 §15): человеку нужен хвост пути,
 * а полный адрес не теряется — он в title и в буфере обмена.
 */
function pathRow(label, path, ctx, { openable = false, note = '' } = {}) {
  if (!path) return null;
  const full = String(path);
  const shown = shortenPathLeft(full, 52);
  const copy = iconButton({
    icon: 'copy',
    title: 'Копировать путь',
    size: 'sm',
    onclick: async () => {
      const ok = await copyText(full);
      if (typeof ctx.toast === 'function') ctx.toast(ok ? 'Путь скопирован' : 'Не удалось скопировать', ok ? 'ok' : 'warn');
    },
  });
  const actions = [copy];
  if (openable) {
    actions.unshift(iconButton({
      icon: 'folder-opened', title: 'Открыть', size: 'sm',
      onclick: () => { openPath(full); },
    }));
  }
  return h('div', { class: 'set-path', 'data-path': label },
    h('span', { class: 'set-path__label' }, label),
    h('code', { class: 'set-path__text mono', title: full, 'aria-label': label + ': ' + full }, shown),
    h('span', { class: 'set-path__actions' }, actions),
    note ? h('span', { class: 'set-path__note small muted' }, note) : null);
}

/* --------------------------------------------------------------- вьюха */

async function mountSettings(el, ctx) {
  await ensureCss(VIEW_CSS);
  const api = ctx.api;
  const store = ctx.store || {};
  const roomsStore = store.rooms;
  let stopped = false;

  const root = h('div', { class: 'view__inner set' });
  el.appendChild(root);

  const lead = h('p', { class: 'set__lead muted' },
    'Одна точка настройки: статус приложения, пути к данным и проверка системы. '
      + 'Всё работает только на этом компьютере.');

  root.appendChild(lead);

  /**
   * Группа экрана (W20-U2 §15, P11).
   *
   * Раньше это был список из 15 строк подряд (PID, порт, пути вперемешку с
   * настройками). Теперь у каждой группы своё имя, пояснение и якорь — в
   * «Вопросах и ответах» на них можно ссылаться.
   */
  function groupSection({ id, anchor = '' }) {
    const meta = SETTINGS_GROUPS.find((group) => group.id === id) || { title: id, hint: '' };
    const body = h('div', { class: 'set-section__body set-group__body' });
    const section = h('section', {
      class: 'set-section set-group', id: anchor || ('set-' + id), 'data-group': id,
    },
    sectionHeader(meta.title),
    meta.hint ? h('p', { class: 'set-group__hint small muted' }, meta.hint) : null,
    body);
    root.appendChild(section);
    return { section, body };
  }

  /* ---------------------------------------------------------- статус */

  const statusGroup = groupSection({ id: 'status', anchor: 'set-health' });
  const statusBody = statusGroup.body;

  let healthSig = '';
  let healthData = null;
  // W13-F §8: предпросмотр доски (нужна ли кнопка) и итог последнего прохода.
  let kickState = null;
  let kickResult = null;
  let kickBusy = false;
  let kickSig = '';

  /* ------------------------------------------------- сводный статус */

  /** Технические строки — под «Показать подробности»: на лице только статус. */
  function techDetails(data) {
    const app = data.app || {};
    const engine = data.engine || {};
    const dispatcher = data.dispatcher || {};
    const watcher = data.watcher || {};
    const tasks = engine.tasks || {};
    const card = h('div', { class: 'set-card' });

    card.appendChild(infoRow('Версия', h('span', {},
      h('span', { class: 'mono' }, String(app.version || '—')),
      ' · порт ' + String(app.port || '—'))));
    if (app.started_at) {
      card.appendChild(infoRow('Запущено', fmtDateTime(app.started_at) + (app.uptime_s ? ' · ' + fmtDuration(app.uptime_s) : '')));
    }
    if (app.tz) card.appendChild(infoRow('Часовой пояс', tzLabel(app.tz)));
    card.appendChild(infoRow('Схема доски', toneChip(engine.schema_ok !== false && engine.ok !== false,
      'в порядке' + (engine.schema_version ? ' · v' + engine.schema_version : ''), 'проблема', 'engine.schema_ok')));
    if (tasks.total !== undefined) {
      card.appendChild(infoRow('Карточки', String(tasks.done || 0) + ' готово · ' + String(tasks.blocked || 0) + ' заблокировано · '
        + String(tasks.active || 0) + ' в работе · всего ' + countText(tasks.total || 0, ['карточка', 'карточки', 'карточек']),
      h('span', { class: 'small muted' }, 'на доске задач')));
    }
    if (engine.cli) {
      card.appendChild(infoRow('Программа Hermes', toneChip(engine.cli.ok !== false, 'найдена', 'не найдена'),
        engine.cli.path ? iconButton({
          icon: 'copy', size: 'sm', title: 'Копировать путь к hermes',
          onclick: async () => {
            const ok = await copyText(engine.cli.path);
            if (typeof ctx.toast === 'function') ctx.toast(ok ? 'Путь скопирован' : 'Не удалось скопировать', ok ? 'ok' : 'warn');
          },
        }) : null));
    }
    if (dispatcher.pid) card.appendChild(infoRow('Номер процесса', h('span', { class: 'mono' }, String(dispatcher.pid))));
    if (dispatcher.updated_at) card.appendChild(infoRow('Обновлён', timeAgo(dispatcher.updated_at)));
    if (dispatcher.warning) card.appendChild(infoRow('Предупреждение', String(dispatcher.warning)));
    card.appendChild(infoRow('Фоновая проверка',
      toneChip(watcher.running !== false, 'идёт', 'остановлена', 'Фоновая проверка читает доску и подхватывает готовые ответы'),
      watcher.last_run_at ? h('span', { class: 'small muted' }, 'последний раз ' + timeAgo(watcher.last_run_at)) : null));
    if (watcher.last_error) {
      card.appendChild(h('div', { class: 'banner banner--warn set-banner' }, icon('warning', { size: 14 }),
        h('div', { class: 'banner__text' }, 'Последняя ошибка фоновой проверки: ' + String(watcher.last_error))));
    }
    return h('details', { class: 'set-more', 'data-role': 'tech' },
      h('summary', { class: 'set-more__summary' },
        icon('chevron-right', { size: 12, className: 'set-more__chev' }),
        'Технические подробности'),
      card);
  }

  function renderStatus(data, error) {
    statusBody.replaceChildren();
    if (error) {
      statusBody.appendChild(errorBanner(error, () => loadHealth({ force: true })));
      return;
    }
    if (!data) {
      statusBody.appendChild(skeleton(3));
      return;
    }
    const app = data.app || {};
    const dispatcher = data.dispatcher || {};
    const watcher = data.watcher || {};
    const verdict = overallStatus(data);
    const meta = [];
    if (app.started_at) meta.push('Запущено ' + fmtDateTime(app.started_at) + (app.uptime_s ? ' · ' + fmtDuration(app.uptime_s) : ''));
    if (app.tz) meta.push(tzLabel(app.tz));

    statusBody.appendChild(h('div', {
      class: 'set-verdict set-verdict--' + verdict.tone,
      'data-tone': verdict.tone, role: 'status', 'aria-live': 'polite',
    },
    h('span', { class: 'set-verdict__icon', 'aria-hidden': 'true' }, icon(verdict.icon, { size: 18 })),
    h('div', { class: 'set-verdict__main' },
      h('div', { class: 'set-verdict__title' }, verdict.title),
      h('p', { class: 'set-verdict__text' }, verdict.text),
      meta.length ? h('div', { class: 'set-verdict__meta small muted' }, meta.join(' · ')) : null),
    h('div', { class: 'set-verdict__actions' },
      h('a', { class: 'btn btn--secondary btn--sm', href: '#/settings/diagnostics' },
        icon('pulse', { size: 12 }), 'Проверка системы'))));

    // Единственное действие в статусе — «Проверить доску сейчас» (W13-F §8).
    statusBody.appendChild(h('div', { class: 'set-status__engine' },
      h('span', { class: 'set-status__engine-label' }, 'Движок команды'),
      stateChip(dispatcher.state || (dispatcher.ok ? 'running' : 'stopped')),
      h('span', { class: 'spacer' }),
      kickControl()));
    if (kickResult) {
      statusBody.appendChild(h('div', { class: 'set-status__kick' },
        kickPanel(kickResult, {
          labelOf: kickLabelOf,
          onClose: () => { kickResult = null; renderStatus(healthData, null); },
        })));
    }

    for (const w of (Array.isArray(data.warnings) ? data.warnings : [])) {
      const text = typeof w === 'string' ? w : [w && w.message, w && w.code ? '(' + w.code + ')' : ''].filter(Boolean).join(' ');
      if (text) statusBody.appendChild(h('div', { class: 'banner banner--warn set-banner' },
        icon('warning', { size: 14 }), h('div', { class: 'banner__text' }, text)));
    }
    if (watcher.last_error) {
      statusBody.appendChild(h('div', { class: 'banner banner--warn set-banner' }, icon('warning', { size: 14 }),
        h('div', { class: 'banner__text' }, 'Последняя ошибка фоновой проверки: ' + String(watcher.last_error))));
    }

    statusBody.appendChild(techDetails(data));
    renderPaths(data);
  }

  /* --------------------------------------------------- пути и данные */

  const pathsGroup = groupSection({ id: 'paths' });
  const pathsBody = pathsGroup.body;

  function renderPaths(data) {
    if (!pathsBody.isConnected && !root.contains(pathsBody)) return;
    pathsBody.replaceChildren();
    if (!data) { pathsBody.appendChild(skeleton(2)); return; }
    const dataInfo = data.data || {};
    const vault = data.vault || {};
    const engine = data.engine || {};
    const rooms = Number(dataInfo.rooms || 0);
    const contents = dataInfo.dir
      ? rooms + ' ' + plural(rooms, ['комната', 'комнаты', 'комнат'])
        + ' · ' + countText(dataInfo.requests || 0, COUNT_FORMS.request)
        + (dataInfo.writable === false ? ' · только чтение' : '')
      : '';

    const card = h('div', { class: 'set-card' });
    const rows = [
      pathRow('Данные', dataInfo.dir, ctx, { openable: true, note: contents }),
      pathRow('Заметки', vault.dir, ctx, {
        openable: true,
        note: vault.dir ? 'Зеркало для людей: STATE.md и Журнал.md' : '',
      }),
      pathRow('База задач', engine.db, ctx, { note: 'доска задач команды' }),
    ].filter(Boolean);
    if (!rows.length) {
      pathsBody.appendChild(h('p', { class: 'set-hint small muted' },
        'Пути ещё не определены — их задаёт установка приложения.'));
      return;
    }
    for (const row of rows) card.appendChild(row);
    pathsBody.appendChild(card);
    if (vault.dir) {
      pathsBody.appendChild(h('div', { class: 'set-path-note small muted row row--tight' },
        toneChip(vault.ok !== false, 'зеркало доступно', 'зеркало недоступно'),
        h('span', { class: 'muted' }, ' заметки читают люди, приложение пишет их только в папку хранилища')));
    }
  }

  /* ----------------------------------------------------- диагностика */

  const diagGroup = groupSection({ id: 'diagnostics' });
  const diagBody = diagGroup.body;
  const diag = { loading: false, data: null, error: null };

  function diagActions() {
    return h('div', { class: 'set-diag__actions' },
      h('a', { class: 'btn btn--secondary btn--sm', href: '#/settings/diagnostics' },
        icon('pulse', { size: 12 }), 'Открыть проверку системы'),
      h('button', {
        class: 'btn btn--ghost btn--sm', type: 'button',
        onclick: (ev) => loadDiag(ev && ev.currentTarget),
      }, icon('refresh', { size: 12 }), 'Обновить'));
  }

  function renderDiagSummary() {
    diagBody.replaceChildren();
    if (diag.loading && !diag.data) { diagBody.appendChild(skeleton(2)); return; }
    if (!diag.data) {
      diagBody.appendChild(h('div', { class: 'banner banner--info set-banner' }, icon('info', { size: 14 }),
        h('div', { class: 'banner__text' },
          'Выжимку проверки получить не удалось' + (diag.error && diag.error.message ? ': ' + String(diag.error.message) : '')
            + '. Полная проверка открывается по кнопке — она ничего не меняет.')));
      diagBody.appendChild(diagActions());
      return;
    }
    const checks = Array.isArray(diag.data.checks) ? diag.data.checks : [];
    const summary = diag.data.summary && typeof diag.data.summary === 'object'
      ? diag.data.summary : checksSummary(checks);
    const problems = topProblems(checks, 3);

    diagBody.appendChild(h('div', { class: 'set-chips' },
      ['fail', 'warn', 'off', 'ok'].map((status) => {
        const meta = CHECK_STATUS[status];
        const count = Number(summary[status] || 0);
        return h('span', {
          class: 'chip chip--' + meta.chip + (count ? '' : ' chip--quiet'),
          'data-status': status, title: meta.label,
        }, h('span', { class: 'chip__dot' }), h('span', { class: 'tnum' }, String(count)), ' ', meta.label);
      })));

    if (problems.length) {
      diagBody.appendChild(h('ul', { class: 'set-diag__list' }, problems.map((item) => {
        const status = normalizeCheckStatus(item.status);
        const meta = CHECK_STATUS[status];
        return h('li', { class: 'set-diag__item', 'data-status': status, 'data-id': String(item.id || '') },
          h('span', { class: 'set-diag__icon set-diag__icon--' + status, 'aria-hidden': 'true' }, icon(meta.icon, { size: 13 })),
          h('span', { class: 'set-diag__title' }, String(item.title || '')),
          h('span', { class: 'set-diag__value small muted' }, String(item.value || meta.label)));
      })));
    } else {
      diagBody.appendChild(h('p', { class: 'set-hint small muted' },
        'Все обязательные пункты в порядке — делать ничего не нужно.'));
    }
    diagBody.appendChild(diagActions());
  }

  async function loadDiag(btn) {
    if (diag.loading) return;
    diag.loading = true;
    if (btn) { btn.disabled = true; btn.classList.add('is-busy'); }
    renderDiagSummary();
    try {
      const data = await api.get('/api/diagnostics');
      if (stopped) return;
      diag.data = data || null;
      diag.error = null;
    } catch (err) {
      if (stopped) return;
      diag.error = err;
    } finally {
      diag.loading = false;
      if (btn) { btn.disabled = false; btn.classList.remove('is-busy'); }
      if (!stopped) renderDiagSummary();
    }
  }


  /** Подпись исполнителя в итоге: «team-ops» → «Операции» (техническое имя — в title). */
  const kickLabelOf = (profile) => {
    const raw = String(profile == null ? '' : profile).trim();
    if (!raw) return '';
    const text = personName(raw);
    return text ? text.charAt(0).toUpperCase() + text.slice(1) : raw;
  };

  /**
   * Кнопка «Проверить доску сейчас» или объяснение, почему она не нужна (W13-F §8).
   * Пока предпросмотр доски не пришёл, показываем кнопку: скрывать её по догадке хуже.
   */
  function kickControl() {
    const use = kickUsefulness(kickState);
    if (kickState && !use.useful) {
      return h('span', { class: 'small muted', 'data-kick': 'why-not' }, kickWhyNot(kickState));
    }
    return h('button', {
      class: 'btn btn--secondary btn--sm', type: 'button', title: KICK_HINT,
      'data-kick': 'button', onclick: (e) => kickDispatcher(e.currentTarget),
    }, icon('zap', { size: 12 }), KICK_LABEL);
  }

  async function kickDispatcher(btn) {
    if (kickBusy) return;
    kickBusy = true;
    if (btn) { btn.disabled = true; btn.classList.add('is-busy'); }
    // Итог — панель с подробностями (что запущено, кто чего ждёт), а не тост.
    kickResult = await kickNow(api, '');
    kickBusy = false;
    if (btn) { btn.disabled = false; btn.classList.remove('is-busy'); }
    refreshKick();
    renderStatus(healthData, null);
    setTimeout(() => loadHealth({ force: true }), 500);
  }

  /** Предпросмотр доски: только чтение, ничего не запускает (работает и в песочнице). */
  async function refreshKick() {
    try {
      const report = await kickPreview(api);
      if (stopped) return false;
      kickState = report && (report.counts || report.cards) ? report : null;
    } catch {
      kickState = null;   // не смогли посмотреть доску — кнопку не прячем
    }
    const sig = JSON.stringify((kickState && kickState.counts) || null);
    if (sig === kickSig) return false;
    kickSig = sig;
    return true;
  }

  async function loadHealth({ silent = false, force = false } = {}) {
    if (!silent) renderStatus(null, null);
    try {
      // Без сохранённой подписи условный GET бессмыслен: каркас мог уже
      // положить ETag в общий кэш, и 304 оставил бы блок в скелетоне.
      const conditional = !force && Boolean(healthSig);
      const data = await api.get('/api/health', conditional ? { etag: true } : undefined);
      if (stopped) return;
      if (data && data.notModified) return;
      healthData = data;
      const sig = JSON.stringify(data);
      if (sig !== healthSig) {
        healthSig = sig;
        renderStatus(data, null);
      }
      // Кнопка «Проверить доску сейчас» появляется/прячется по состоянию доски.
      refreshKick().then((changed) => {
        if (changed && !stopped) renderStatus(healthData, null);
      });
      if (store.health && store.health.set) {
        store.health.set({ loading: false, ok: Boolean(data && data.ok), error: null, data });
      }
    } catch (err) {
      if (stopped) return;
      renderStatus(null, err);
    }
  }

  /* ------------------------------------------- внешний вид и уведомления */

  // Один сохраняющий блок (W20-U2 §15): поля ниже — черновик, он применяется
  // и запоминается только по кнопке «Сохранить настройки». Тема показывается
  // сразу (предпросмотр), но в хранилище попадает вместе с остальным.
  const prefsGroup = groupSection({ id: 'prefs', anchor: 'set-look' });
  const prefsBody = prefsGroup.body;

  const supported = typeof Notification !== 'undefined';
  const saved = {
    theme: (store.theme && store.theme.get && store.theme.get('pref')) || 'auto',
    notify: lsGet(NOTIFY_KEY, false) === true && supported,
  };
  const draft = { theme: saved.theme, notify: saved.notify };

  /** Подпись темы в шапке должна совпадать с тем, что видно на экране. */
  function syncThemeStore(pref) {
    if (!(store.theme && store.theme.set)) return;
    const media = typeof window !== 'undefined' && window.matchMedia
      ? window.matchMedia('(prefers-color-scheme: dark)') : null;
    store.theme.set({
      pref,
      resolved: pref === 'auto' ? (media && media.matches ? 'dark' : 'light') : pref,
    });
  }

  const permission = () => (supported ? Notification.permission : 'unsupported');
  const permissionText = { granted: 'разрешены', default: 'не запрошены', denied: 'запрещены в браузере', unsupported: 'браузер не поддерживает' };
  const statusLine = h('span', { class: 'small muted' });
  function renderPerm() {
    statusLine.textContent = 'Разрешение: ' + (permissionText[permission()] || permission());
  }

  const themeHost = h('div', { class: 'set-field__control' });
  function buildThemeSeg() {
    return segmented({
      options: [
        { value: 'auto', label: 'Как в системе' },
        { value: 'light', label: 'Светлая' },
        { value: 'dark', label: 'Тёмная' },
      ],
      value: draft.theme,
      onchange: (value) => {
        // Предпросмотр: экран меняется сразу, в браузере пока ничего не пишем.
        draft.theme = applyTheme(value, { persist: false });
        syncThemeStore(draft.theme);
        renderSaveState();
        renderThemeNote();
      },
    });
  }
  themeHost.replaceChildren(buildThemeSeg());

  const themeNote = h('p', { class: 'set-hint small muted' });
  function renderThemeNote() {
    const urlTheme = new URLSearchParams(location.search).get('theme');
    themeNote.textContent = urlTheme
      ? 'Сейчас тема задана адресом (?theme=' + urlTheme + '): при перезагрузке с этим параметром вернётся она, а не сохранённый выбор.'
      : '';
  }
  renderThemeNote();

  const checkbox = h('input', { type: 'checkbox', id: 'set-notify-check' });
  checkbox.checked = draft.notify;
  checkbox.addEventListener('change', () => {
    draft.notify = checkbox.checked === true;
    renderSaveState();
  });

  const testBtn = h('button', {
    class: 'btn btn--secondary btn--sm', type: 'button',
    onclick: () => {
      const shown = notifyIfEnabled('Комната команды', 'Так выглядит уведомление о готовом ответе.');
      if (!shown && typeof ctx.toast === 'function') ctx.toast('Уведомление не показано: проверьте разрешение', 'warn');
    },
  }, icon('bell', { size: 12 }), 'Проверить');

  prefsBody.appendChild(h('div', { class: 'set-field' },
    h('span', { class: 'field__label' }, 'Тема'),
    themeHost,
    h('p', { class: 'set-hint small muted' },
      '«Как в системе» следует за настройкой Windows. Выбор применится после «Сохранить настройки».')));
  prefsBody.appendChild(themeNote);
  prefsBody.appendChild(h('div', { class: 'set-field set-field--notify' },
    h('label', { class: 'checkbox set-switch', for: 'set-notify-check' }, checkbox,
      h('span', {}, 'Уведомлять, когда ответ готов')),
    h('p', { class: 'set-hint small muted' },
      'Системное уведомление появится, когда задача завершится или команда задаст вам вопрос. '
        + 'Показывать его будет приложение; настройка хранится в этом браузере.'),
    h('div', { class: 'row row--wrap set-notify__actions' }, statusLine, h('span', { class: 'spacer' }), testBtn)));
  if (!supported) {
    prefsBody.appendChild(h('div', { class: 'banner banner--info set-banner' }, icon('info', { size: 14 }),
      h('div', { class: 'banner__text' }, 'Этот браузер не умеет системные уведомления — настройка не повлияет на работу приложения.')));
  }

  // W24-N3 → W27-E4: «Стартовая страница» — что открывать при запуске.
  // Три варианта (дефект 3: «Комнаты» были недостижимы, «Сегодня» перекрывала
  // дом): «Сегодня», «Комнаты» (#/) и «Последнее место». Значения и ключ —
  // из route-memory.js (один источник с каркасом и тестами). Сохраняется сразу:
  // это навигационная привычка, а не черновик настроек.
  const startGet = () => {
    try { return normalizeStartPref(localStorage.getItem(START_KEY)); } catch { return START_TODAY; }
  };
  const startHint = h('p', { class: 'set-hint small muted' },
    'Что открывать при запуске. «Комнаты» — список комнат с быстрой задачей, '
      + '«Последнее место» — экран, на котором вы остановились. Явный адрес в строке '
      + '(например, ссылка на задачу) всегда сильнее настройки. Выбор сохраняется сразу.');
  const startHost = h('div', { class: 'set-field__control' },
    segmented({
      options: START_OPTIONS.map((o) => ({ value: o.value, label: o.label })),
      value: startGet(),
      onchange: (value) => {
        const next = normalizeStartPref(value);
        try { localStorage.setItem(START_KEY, next); } catch { /* приватный режим */ }
        const label = (START_OPTIONS.find((o) => o.value === next) || {}).label || '';
        if (typeof ctx.toast === 'function') {
          ctx.toast('При запуске будем открывать «' + label + '»', 'ok');
        }
      },
    }));
  prefsBody.appendChild(h('div', { class: 'set-field', 'data-setting': 'start-page' },
    h('span', { class: 'field__label' }, 'Стартовая страница'),
    startHost,
    startHint));

  const saveState = h('p', { class: 'set-save__state small muted' });
  const saveBtn = h('button', {
    class: 'btn btn--primary', type: 'button', 'data-action': 'settings-save',
    onclick: () => { savePrefs(); },
  }, icon('check', { size: 13 }), 'Сохранить настройки');
  const revertBtn = h('button', {
    class: 'btn btn--ghost', type: 'button', 'data-action': 'settings-revert',
    onclick: () => { revertPrefs(); },
  }, 'Отменить');
  const saveBar = h('div', { class: 'set-save', 'data-dirty': '0' },
    h('div', { class: 'set-save__text' }, saveState),
    h('div', { class: 'set-save__actions' }, revertBtn, saveBtn));
  prefsBody.appendChild(saveBar);

  function renderSaveState() {
    const info = saveSummary(pendingChanges(saved, draft));
    saveState.textContent = info.text;
    saveBtn.disabled = !info.dirty;
    revertBtn.disabled = !info.dirty;
    saveBar.classList.toggle('is-dirty', info.dirty);
    saveBar.setAttribute('data-dirty', info.dirty ? '1' : '0');
  }

  /** Разрешение спрашиваем в момент сохранения: это действие человека. */
  async function requestNotifyPermission() {
    if (typeof Notification === 'undefined') return 'unsupported';
    let perm = Notification.permission;
    if (perm === 'default') {
      try { perm = await Notification.requestPermission(); } catch { perm = 'denied'; }
    }
    return perm;
  }

  async function savePrefs() {
    const changes = pendingChanges(saved, draft);
    if (!changes.length) return;
    if (changes.includes('notify') && draft.notify) {
      const perm = await requestNotifyPermission();
      if (perm !== 'granted') {
        draft.notify = false;
        checkbox.checked = false;
        renderPerm();
        renderSaveState();
        if (typeof ctx.toast === 'function') {
          ctx.toast(perm === 'denied'
            ? 'Браузер запретил уведомления — разрешите их в настройках сайта'
            : 'Уведомления не разрешены', 'warn');
        }
        return;
      }
    }
    if (changes.includes('theme')) {
      applyTheme(draft.theme, { persist: true });
      syncThemeStore(draft.theme);
      saved.theme = draft.theme;
    }
    if (changes.includes('notify')) {
      lsSet(NOTIFY_KEY, draft.notify === true);
      saved.notify = draft.notify === true;
      checkbox.checked = saved.notify;
      renderPerm();
    }
    renderSaveState();
    if (typeof ctx.toast === 'function') ctx.toast('Настройки сохранены', 'ok');
  }

  function revertPrefs() {
    const changes = pendingChanges(saved, draft);
    if (!changes.length) return;
    draft.theme = saved.theme;
    draft.notify = saved.notify;
    themeHost.replaceChildren(buildThemeSeg());
    checkbox.checked = saved.notify;
    applyTheme(saved.theme, { persist: false });
    syncThemeStore(saved.theme);
    renderSaveState();
    if (typeof ctx.toast === 'function') ctx.toast('Изменения отменены', 'info');
  }

  renderPerm();
  renderSaveState();

  /* ------------------------------------ расход и лимиты (W29-P9) */

  // Панель живёт отдельным файлом (views/limits.js): экран настроек не
  // разрастается, а панель можно открыть и из шапки (чип «5 ч: …»).
  const limitsHost = h('div', { class: 'set-section', id: 'set-limits' });
  root.appendChild(limitsHost);

  /* --------------------------------------------------------------- FAQ */

  const faq = h('section', { class: 'set-section', id: 'set-faq' });
  faq.appendChild(sectionHeader('Вопросы и ответы'));
  const faqBody = h('div', { class: 'set-faq' });
  const roomId = (ctx.route && ctx.route.roomId) || (ctx.room && ctx.room.id) || ((roomsStore && roomsStore.get('list') || [])[0] || {}).id || '';

  for (const item of FAQ) {
    const details = h('details', { class: 'set-faq__item' });
    details.appendChild(h('summary', { class: 'set-faq__q' }, String(item.q)));
    const body = h('div', { class: 'set-faq__a' }, h('p', {}, String(item.a)));
    const links = h('div', { class: 'set-faq__links' });
    const href = item.tab && roomId ? '#/room/' + encodeURIComponent(roomId) + '/' + item.tab
      : (item.anchor || '');
    if (href && item.link) {
      links.appendChild(h('a', { class: 'set-faq__link', href }, item.link, icon('arrow-right', { size: 11 })));
    }
    if (links.childNodes.length) body.appendChild(links);
    details.appendChild(body);
    faqBody.appendChild(details);
  }
  faq.appendChild(faqBody);
  root.appendChild(faq);

  /* ----------------------------------------------------- горячие клавиши */

  const keys = h('section', { class: 'set-section', id: 'set-keys' });
  keys.appendChild(sectionHeader('Горячие клавиши'));
  const keysBody = h('div', { class: 'set-keys' }, HOTKEYS.map(kbdRow));
  keys.appendChild(keysBody);
  root.appendChild(keys);

  /* ---------------------------------------------------------- новая комната */

  const roomSection = h('section', { class: 'set-section', id: 'set-newroom' });
  roomSection.appendChild(sectionHeader('Новая комната'));
  const roomBody = h('div', { class: 'set-section__body' });
  roomBody.appendChild(h('p', { class: 'set-hint small muted' },
    'Комната — это проект: своя доска, руководитель комнаты и состав агентов. Название станет идентификатором (латиницей).'));

  const titleInput = h('input', { class: 'input', type: 'text', placeholder: 'Например, «Кофейня на углу»', id: 'set-room-title' });
  const orchSelect = h('select', { class: 'select', id: 'set-room-orch' });
  const criticSelect = h('select', { class: 'select', id: 'set-room-critic' });
  const createBtn = h('button', { class: 'btn btn--primary', type: 'button', onclick: () => createRoom() },
    icon('new-folder', { size: 13 }), 'Создать комнату');
  const roomNote = h('p', { class: 'set-hint small muted' });

  const form = h('div', { class: 'set-form' },
    h('div', { class: 'set-form__row' },
      field({ label: 'Название', control: titleInput, hint: 'Видно в списке комнат' }),
    ),
    h('div', { class: 'set-form__row set-form__row--two' },
      field({ label: 'Руководитель комнаты', control: orchSelect, hint: 'Раздаёт работу и сводит ответ' }),
      field({ label: 'Критик', control: criticSelect, hint: 'Проверяет свод после руководителя комнаты' }),
    ),
    h('div', { class: 'row row--wrap set-form__foot' }, createBtn, roomNote),
  );
  roomBody.appendChild(form);
  roomSection.appendChild(roomBody);
  root.appendChild(roomSection);

  let profileRoster = [];

  function fillProfiles(profiles) {
    const list = Array.isArray(profiles) ? profiles.filter((p) => p && p.profile) : [];
    profileRoster = list;
    const orchestrators = list.filter((p) => p.kind === 'director' || p.kind === 'utility');
    const orchPool = orchestrators.length ? orchestrators : list;
    orchSelect.replaceChildren(...orchPool.map((p) => h('option', { value: p.profile }, (p.title || p.profile) + ' (' + p.profile + ')')));
    criticSelect.replaceChildren(h('option', { value: '' }, 'Без критика'),
      ...list.filter((p) => /critic|критик/i.test(p.profile + ' ' + (p.title || '')))
        .map((p) => h('option', { value: p.profile }, (p.title || p.profile) + ' (' + p.profile + ')')));
    const currentOrch = (ctx.room && ctx.room.orchestrator) || 'default';
    if (Array.from(orchSelect.options).some((o) => o.value === currentOrch)) orchSelect.value = currentOrch;
    // Критик — первый из ростера, как в диалоге комнаты (D3).
    const firstCritic = list.find((p) => p.kind === 'critic');
    if (firstCritic) criticSelect.value = firstCritic.profile;
  }

  /** Состав для новой комнаты: 2–3 исполнителя, кроме руководителя комнаты и критика (D3). */
  function defaultMembers() {
    const leader = orchSelect.value;
    const critic = criticSelect.value;
    const free = profileRoster.filter((p) => p.profile !== leader && p.profile !== critic);
    const workers = free.filter((p) => String(p.kind || '') === 'worker');
    const picked = workers.slice(0, 3);
    for (const p of free) {
      if (picked.length >= 2) break;
      if (!picked.includes(p)) picked.push(p);
    }
    return picked.map((p) => ({ profile: p.profile, role: 'worker' }));
  }

  async function loadProfiles() {
    try {
      const payload = await api.get('/api/profiles');
      if (stopped) return;
      fillProfiles(payload && payload.items);
    } catch {
      if (stopped) return;
      fillProfiles([{ profile: 'default', title: 'Василий', kind: 'director' }]);
      if (typeof ctx.toast === 'function') ctx.toast('Список профилей недоступен — руководитель по умолчанию default', 'warn');
    }
  }

  async function createRoom() {
    const title = String(titleInput.value || '').trim();
    if (!title) {
      titleInput.focus();
      if (typeof ctx.toast === 'function') ctx.toast('Нужно название комнаты', 'warn');
      return;
    }
    createBtn.disabled = true;
    try {
      const payload = await api.post('/api/rooms', {
        title,
        orchestrator: orchSelect.value || 'default',
        critic: criticSelect.value || null,
        critic_pass: Boolean(criticSelect.value),
        members: defaultMembers(),
      });
      const room = (payload && payload.room) || (payload && payload.id ? payload : null);
      const id = room && room.id ? String(room.id) : '';
      if (typeof ctx.toast === 'function') ctx.toast('Комната создана', 'ok');
      titleInput.value = '';
      if (id && roomsStore && roomsStore.set) {
        const list = roomsStore.get('list') || [];
        if (!list.some((r) => r.id === id)) {
          roomsStore.set({
            list: list.concat([{
              id,
              title: room.title || title,
              icon: room.icon || 'home',
              members: [],
              counts: { working: 0, needs_you: 0, done: 0 },
              counters: { working: 0, needs_you: 0, done_today: 0 },
              phase: room.phase || null,
            }]),
          });
        }
      }
      if (id) {
        setTimeout(() => {
          if (typeof ctx.navigate === 'function') ctx.navigate('#/room/' + encodeURIComponent(id) + '/composer');
          else location.hash = '#/room/' + encodeURIComponent(id) + '/composer';
        }, 300);
      }
    } catch (err) {
      const message = humanApiMessage(err && err.message ? err.message : 'Не удалось создать комнату');
      roomNote.textContent = message;
      roomNote.classList.add('set-form__error');
      if (typeof ctx.toast === 'function') ctx.toast(message, 'danger');
    } finally {
      createBtn.disabled = false;
    }
  }

  /* ------------------------------------ разрешения агентам (W22-AP2) */

  // Раздел «Разрешения агентов»: список выданных разрешений и отзыв. Отдельный
  // файл, как «Диктовка»: экран настроек подключает его, если он на месте.
  const approvalsHost = h('div', { class: 'set-section', id: 'set-approvals' });
  root.appendChild(approvalsHost);
  (async () => {
    try {
      const m = await import('./settings-approvals.js');
      if (stopped || !m || typeof m.mountApprovalsSettings !== 'function') {
        approvalsHost.remove();
        return;
      }
      await m.mountApprovalsSettings(approvalsHost, ctx);
    } catch (err) {
      console.warn('[settings] разрешения агентам недоступны', err);
      approvalsHost.remove();
    }
  })();

  /* --------------------------------------- уведомления Windows (W29B-Q6) */

  // Раздел «Уведомления»: по умолчанию выключены, включаются здесь; виды событий,
  // тихие часы, частота, кнопка «Проверить» и журнал последних уведомлений.
  const notifyHost = h('div', { class: 'set-section', id: 'set-notify' });
  root.appendChild(notifyHost);
  (async () => {
    try {
      const m = await import('./notify-settings.js');
      if (stopped || !m || typeof m.mountNotifySettings !== 'function') {
        notifyHost.remove();
        return;
      }
      const inst = await m.mountNotifySettings(notifyHost, ctx);
      if (stopped && inst && typeof inst.unmount === 'function') inst.unmount();
    } catch (err) {
      console.warn('[settings] уведомления недоступны', err);
      notifyHost.remove();
    }
  })();

  /* ------------------------------------------- диктовка и словарь (DICT-UI) */

  /* ------------------------------------------------ хранилище (W29-P8) */

  // Раздел «Хранилище»: сколько занято чем и что уберётся. Полный экран —
  // #/settings/storage (views/storage.js), здесь — короткая сводка и переход.
  const storageHost = h('div', { class: 'set-section', id: 'set-storage' });
  root.appendChild(storageHost);
  (async () => {
    try {
      const m = await import('./storage.js');
      if (stopped || !m || typeof m.mountStorageSection !== 'function') {
        storageHost.remove();
        return;
      }
      const inst = await m.mountStorageSection(storageHost, ctx);
      if (stopped && inst && typeof inst.unmount === 'function') inst.unmount();
    } catch (err) {
      console.warn('[settings] хранилище недоступно', err);
      storageHost.remove();
    }
  })();

  /* ------------------------------------------- диктовка и словарь (DICT-UI) */

  // Раздел пишет другой исполнитель: если файла ещё нет — просто не показываем
  // его. Существование проверяем fetch-ем (404 от import() шумит в консоли).
  const dictationHost = h('div', { class: 'set-section', id: 'set-dictation' });
  root.appendChild(dictationHost);
  (async () => {
    let exists = false;
    try {
      const url = new URL('./settings-dictation.js', import.meta.url).href;
      const res = await fetch(url, { method: 'GET', cache: 'force-cache' });
      exists = Boolean(res && res.ok);
    } catch { exists = false; }
    if (stopped || !exists) { dictationHost.remove(); return; }
    try {
      const m = await import('./settings-dictation.js');
      if (stopped || !m || typeof m.mountDictationSettings !== 'function') { dictationHost.remove(); return; }
      await m.mountDictationSettings(dictationHost, ctx);
    } catch (err) {
      console.warn('[settings] диктовка недоступна', err);
      dictationHost.remove();
    }
  })();

  /* -------------------------------------------------------- старт и уборка */

  let limitsUi = null;
  (async () => {
    try {
      const m = await import('./limits.js');
      if (stopped || !m || typeof m.mountLimits !== 'function') { limitsHost.remove(); return; }
      limitsUi = await m.mountLimits(limitsHost, ctx);
    } catch (err) {
      console.warn('[settings] расход и лимиты недоступны', err);
      limitsHost.remove();
    }
  })();

  loadHealth();
  loadProfiles();
  loadDiag();
  const timer = api.poll ? api.poll(() => loadHealth({ silent: true }), HEALTH_POLL_MS, { pauseHidden: true, immediate: false }) : null;

  if (ctx.params && String(ctx.params.new || '') === '1') {
    setTimeout(() => {
      if (!roomSection.isConnected) return;
      try {
        roomSection.scrollIntoView({ block: 'start' });
        titleInput.focus({ preventScroll: true });
      } catch { /* ignore */ }
    }, 120);
  }

  return {
    unmount() {
      stopped = true;
      if (timer && typeof timer.stop === 'function') timer.stop();
      if (limitsUi && typeof limitsUi.destroy === 'function') limitsUi.destroy();
      root.remove();
    },
  };
}

/**
 * @param {HTMLElement} el
 * @param {object} ctx
 * @returns {Promise<{unmount: Function}>}
 */
export async function mount(el, ctx) {
  return mountSettings(el, ctx);
}

export default mount;
