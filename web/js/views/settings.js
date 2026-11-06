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
import { personName } from './request-util.js';
import {
  KICK_LABEL, KICK_HINT, kickPreview, kickNow, kickUsefulness, kickWhyNot, kickPanel,
} from './kick-panel.js';

const VIEW_CSS = 'css/view-settings.css';
const THEME_KEY = 'teamroom.theme';
const NOTIFY_KEY = 'notify';
const HEALTH_POLL_MS = 20000;

const THEME_NAMES = { auto: 'как в системе', light: 'светлая', dark: 'тёмная' };

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
  if (/Ю-Ам|Южная Америка|Brasilia|Bras[ií]lia|S[ãa]o Paulo|E\. South America/i.test(text)) {
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

function applyTheme(pref) {
  const value = ['auto', 'light', 'dark'].includes(pref) ? pref : 'auto';
  try { document.documentElement.dataset.theme = value; } catch { /* ignore */ }
  try { localStorage.setItem(THEME_KEY, value); } catch { /* приватный режим */ }
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

/** Вопросы Михаила → короткие ответы и ссылки на экраны. */
const FAQ = [
  { q: 'Где ответ на мой запрос?', a: 'На странице запроса: блок «Ответ» вверху. В ленте запросов у готового запроса стоит чип «готово». Откройте запрос из вкладки «Запрос».', tab: 'composer', link: 'К запросам' },
  { q: 'Что дальше делать?', a: 'Вкладка «Дальше»: шаги с владельцем и сроком, будущие вопросы и то, что ждёт вашего решения. Тот же блок повторён на странице запроса.', tab: 'backlog', link: 'Открыть «Дальше»' },
  { q: 'Что от меня ждут?', a: 'Число у комнаты слева и бейдж на вкладке «Дальше» — это дела, которые ждут вашего решения: вопросы, блокировки, предложенные решения. Откройте вкладку — ответить можно прямо в строке, ответ уйдёт комментарием и снимет блокировку.', tab: 'backlog', link: 'Что ждёт меня' },
  { q: 'На каком мы этапе?', a: 'Вкладка «Карта проекта»: полоса фаз и решения. Текущая фаза показана чипом в шапке комнаты.', tab: 'roadmap', link: 'Карта проекта' },
  { q: 'Кто что делает сейчас?', a: 'Вкладка «Команда»: у каждого профиля живой статус, текущая задача и последняя работа. Если все простаивают — смотрите «Здоровье» ниже.', tab: 'team', link: 'Команда' },
  { q: 'Почему ничего не происходит?', a: 'Чаще всего остановился движок команды — он раздаёт задачи исполнителям. В блоке «Здоровье» видно его состояние и есть кнопка «Проверить доску сейчас»: она один раз запускает готовые задачи, не дожидаясь минутного таймера, и показывает, что именно запустилось. Второй вариант — запрос ещё не разобран на работы: нажмите «Разложить сейчас» на странице запроса (это обращение к модели, только по вашему клику).', tab: 'team', link: 'Кто чем занят' },
  { q: 'Что сказал критик? Можно переделать?', a: 'На странице запроса есть блок «Проверка критика»: вердикт и замечания «где / в чём проблема / что исправить». Кнопка «На доработку» отправляет ответ руководителю комнаты с вашим замечанием — получится новая версия.', tab: 'composer', link: 'К запросам' },
  { q: 'Как попросить интерактивный отчёт?', a: 'Скажите в запросе прямо: «сделай интерактивный отчёт с таблицей и графиком». Команда соберёт страницу отчёта — она появится во вкладке «Отчёты» и откроется на весь экран.', tab: 'reports', link: 'Отчёты' },
  { q: 'Как добавить агента?', a: 'Вкладка «Команда» → «Добавить»: список профилей Hermes с описаниями. Можно добавить, например, программиста на постоянку — состав комнаты меняется в любой момент.', tab: 'team', link: 'Добавить агента' },
  { q: 'Как завести комнату?', a: 'Блок «Новая комната» ниже: название, руководитель комнаты и, если нужно, критик. После создания откройте блок «Команда» и добавьте исполнителей.', anchor: '#set-newroom', link: 'Новая комната' },
  { q: 'Что было раньше по теме?', a: 'Поиск по всему приложению — Ctrl+K, а также вкладки «Журнал» (хронология событий) и «Карта проекта» (решения).', tab: 'journal', link: 'Журнал' },
  { q: 'Хочу в календарь', a: 'Во вкладке «Дальше» у шагов есть срок; кнопка экспорта отдаёт .ics — файл открывается календарём, без внешней сети.', tab: 'backlog', link: 'Шаги и сроки' },
  { q: 'Хочу послать кому-то результат', a: 'На странице запроса — экспорт в .md и в автономный .html: один файл, всё внутри (текст ответа, шаги, решения, отчёт), открывается без сервера.', tab: 'composer', link: 'К запросам' },
  { q: 'Сколько это шло и кто опоздал?', a: 'На странице запроса у задач в дереве работ видны длительности и прогресс N/M по работам. Сроки шагов — во вкладке «Дальше».', tab: 'composer', link: 'К запросам' },
  { q: 'Что делает критик?', a: 'После того как руководитель комнаты свёл ответ, критик получает запрос и свод и отвечает вердиктом: «принято» или «на доработку» со списком замечаний. При «на доработку» руководитель комнаты делает новую версию ответа. Сколько раз проверять — задаётся один раз в настройках комнаты, руководитель об этом вас не спрашивает.', tab: 'composer', link: 'К запросам' },
  { q: 'Что такое раунд критика и сколько их ставить?', a: 'Раунд — один цикл «критик проверил → если нашёл замечания, исполнители доработали → снова проверили». 1 раунд — быстро: одна попытка исправить замечания. 2 раунда — тщательнее. Третий вариант «До полного одобрения критиком» — цикл идёт, пока критик не скажет «принято»: удобно оставить на ночь и утром получить готовое. Страховки: остановка, если замечания повторяются три раунда подряд, пауза при лимите провайдера и потолок раундов (по умолчанию 12) — по достижении цикл останавливается с понятным отчётом. Выбирается в настройках комнаты («Изменить…» → «Раунды проверки») и при постановке запроса.', tab: 'team', link: 'Как работает комната' },
  { q: 'Сколько это стоит в токенах?', a: 'Деньги и токены считает Hermes, а не это приложение. Каждый запрос запускает агентов только по вашему нажатию «Отправить»: сначала руководитель комнаты разбирает запрос, потом работают исполнители, критик добавляет 1–2 запуска. Посмотреть расход: команда hermes insights --days 7 (токены и стоимость по сессиям) или hermes usage (лимиты). Здесь цифр нет намеренно — мы их не выдумываем.' },
  { q: 'Что если агент ошибся?', a: 'Три пути. «На доработку» на странице запроса: напишите, что не так, — руководитель комнаты перепишет ответ, прежние версии сохраняются. «Переназначить» задачу в дереве работ — её возьмёт другой профиль. «Повторить» на упавшей карточке. Факты всё равно проверяйте: система гарантирует, что ответ собран из работы команды и прошёл критика, а не истинность.' },
  { q: 'Куда смотреть, когда ответ готов?', a: 'Вкладка «Запрос»: у готового запроса чип «готово», откройте его — ответ вверху страницы. Если включены уведомления, придёт системное сообщение. Числа в сайдбаре показывают, сколько дел ждёт вашего решения.' },
  { q: 'Где лежат данные?', a: 'Всё локально: папка data/ (реестр комнат, состояние проекта, отчёты), зеркало для людей в Vault (STATE.md и Журнал.md) и база задач Hermes. Точные пути с кнопкой копирования — в блоке «Здоровье» выше.', anchor: '#set-health', link: 'Пути в «Здоровье»' },
  { q: 'Это точно локально и безопасно?', a: 'Приложение доступно только на этом компьютере, внешних запросов нет, тексты агентов экранируются, файлы отдаются лишь из белых папок, HTML-отчёты живут в песочнице. Порт и версия — в «Здоровье».', anchor: '#set-health', link: 'Проверить' },
  { q: 'Как запустить? Само запустится?', a: 'Запуск — start-team-room.cmd: поднимает локальный сервер и открывает страницу. В автозапуск ничего не ставится: это делается только по отдельной просьбе.' },
];

const HOTKEYS = [
  { keys: ['/'], text: 'Поиск по всему приложению: запросы, решения, журнал' },
  { keys: ['Ctrl', 'K'], text: 'Командная палитра: команды, комнаты, поиск' },
  { keys: ['↑', '↓'], text: 'Двигаться по списку в палитре и в поиске' },
  { keys: ['Enter'], text: 'Открыть выбранную команду или результат' },
  { keys: ['Esc'], text: 'Закрыть палитру или диалог' },
  { keys: ['Ctrl', 'Enter'], text: 'Отправить запрос из поля запроса' },
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

/** Строка пути с копированием (и «открыть папку», если это каталог). */
function pathRow(label, path, ctx, { openable = false } = {}) {
  if (!path) return null;
  const copy = iconButton({
    icon: 'copy',
    title: 'Копировать путь',
    size: 'sm',
    onclick: async () => {
      const ok = await copyText(path);
      if (typeof ctx.toast === 'function') ctx.toast(ok ? 'Путь скопирован' : 'Не удалось скопировать', ok ? 'ok' : 'warn');
    },
  });
  const actions = [copy];
  if (openable) {
    actions.unshift(iconButton({
      icon: 'folder-opened', title: 'Открыть', size: 'sm',
      onclick: () => { openPath(path); },
    }));
  }
  return h('div', { class: 'set-row set-row--path' },
    h('span', { class: 'set-row__label' }, label),
    h('code', { class: 'set-row__path mono truncate', title: path }, path),
    h('span', { class: 'set-row__actions' }, actions),
  );
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
    'Одна точка настройки: здоровье движка, тема, уведомления и ответы на частые вопросы. '
      + 'Всё работает только на этом компьютере.');
  root.appendChild(lead);

  /* ---------------------------------------------------------- здоровье */

  const healthSection = h('section', { class: 'set-section', id: 'set-health' });
  const healthBody = h('div', { class: 'set-section__body' });
  healthSection.appendChild(sectionHeader('Здоровье'));
  healthSection.appendChild(healthBody);
  root.appendChild(healthSection);

  let healthSig = '';
  let healthData = null;
  // W13-F §8: предпросмотр доски (нужна ли кнопка) и итог последнего прохода.
  let kickState = null;
  let kickResult = null;
  let kickBusy = false;
  let kickSig = '';

  function healthChips(data) {
    const engine = data.engine || {};
    const dispatcher = data.dispatcher || {};
    const watcher = data.watcher || {};
    const app = data.app || {};
    return [
      toneChip(engine.schema_ok !== false && engine.ok !== false, 'схема БД в порядке', 'проблема со схемой БД', 'engine.schema_ok'),
      stateChip(dispatcher.state || (dispatcher.ok ? 'running' : 'stopped')),
      toneChip(watcher.running !== false, 'фоновая проверка идёт', 'фоновая проверка остановлена', 'Фоновая проверка следит за доской и подхватывает готовые ответы'),
      toneChip(app.sandbox !== true, 'обычный режим', 'песочница'),
    ];
  }

  function renderHealth(data, error) {
    healthBody.replaceChildren();
    if (error) {
      healthBody.appendChild(errorBanner(error, () => loadHealth({ force: true })));
      return;
    }
    if (!data) {
      healthBody.appendChild(skeleton(4));
      return;
    }
    const app = data.app || {};
    const engine = data.engine || {};
    const dispatcher = data.dispatcher || {};
    const watcher = data.watcher || {};
    const dataInfo = data.data || {};
    const vault = data.vault || {};

    healthBody.appendChild(h('div', { class: 'set-chips' }, healthChips(data)));

    const card = h('div', { class: 'set-card' });
    card.appendChild(infoRow('Версия', h('span', {},
      h('span', { class: 'mono' }, String(app.version || '—')),
      ' · порт ' + String(app.port || '—'))));
    if (app.started_at) {
      card.appendChild(infoRow('Запущено', fmtDateTime(app.started_at) + (app.uptime_s ? ' · ' + fmtDuration(app.uptime_s) : '')));
    }
    if (app.tz) card.appendChild(infoRow('Часовой пояс', tzLabel(app.tz)));

    card.appendChild(h('div', { class: 'set-card__sep' }));
    card.appendChild(infoRow('Движок', 'доска задач' + (engine.schema_version ? ' · схема v' + engine.schema_version : '')));
    const tasks = engine.tasks || {};
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

    card.appendChild(h('div', { class: 'set-card__sep' }));
    card.appendChild(infoRow('Движок команды', stateChip(dispatcher.state || (dispatcher.ok ? 'running' : 'stopped')),
      kickControl()));
    if (kickResult) {
      card.appendChild(h('div', { style: { marginTop: 'var(--space-2)' } },
        kickPanel(kickResult, {
          labelOf: kickLabelOf,
          onClose: () => { kickResult = null; renderHealth(healthData, null); },
        })));
    }
    if (dispatcher.pid) card.appendChild(infoRow('Номер процесса', h('span', { class: 'mono' }, String(dispatcher.pid))));
    if (dispatcher.updated_at) card.appendChild(infoRow('Обновлён', timeAgo(dispatcher.updated_at)));
    if (dispatcher.warning) card.appendChild(infoRow('Предупреждение', String(dispatcher.warning)));

    card.appendChild(h('div', { class: 'set-card__sep' }));
    card.appendChild(infoRow('Фоновая проверка', toneChip(watcher.running !== false, 'идёт', 'остановлена', 'Фоновая проверка читает доску и подхватывает готовые ответы'),
      watcher.last_run_at ? h('span', { class: 'small muted' }, 'последний раз ' + timeAgo(watcher.last_run_at)) : null));
    if (watcher.last_error) {
      card.appendChild(h('div', { class: 'banner banner--warn set-banner' }, icon('warning', { size: 14 }),
        h('div', { class: 'banner__text' }, 'Последняя ошибка фоновой проверки: ' + String(watcher.last_error))));
    }

    card.appendChild(h('div', { class: 'set-card__sep' }));
    const dataPath = pathRow('Данные', dataInfo.dir, ctx, { openable: true });
    if (dataPath) card.appendChild(dataPath);
    if (dataInfo.dir) {
      card.appendChild(infoRow('Содержимое', String(dataInfo.rooms || 0) + ' ' + plural(dataInfo.rooms, ['комната', 'комнаты', 'комнат'])
        + ' · ' + countText(dataInfo.requests || 0, COUNT_FORMS.request)
        + (dataInfo.writable === false ? ' · только чтение' : '')));
    }
    const vaultPath = pathRow('Заметки', vault.dir, ctx, { openable: true });
    if (vaultPath) card.appendChild(vaultPath);
    if (vault.dir) card.appendChild(infoRow('Зеркало', h('span', { class: 'row row--tight' },
      toneChip(vault.ok !== false, 'доступно', 'недоступно'),
      h('span', { class: 'small muted' }, 'STATE.md и Журнал.md'))));
    const dbPath = pathRow('База задач', engine.db, ctx);
    if (dbPath) card.appendChild(dbPath);
    healthBody.appendChild(card);

    const warnings = Array.isArray(data.warnings) ? data.warnings : [];
    for (const w of warnings) {
      const text = typeof w === 'string' ? w : [w && w.message, w && w.code ? '(' + w.code + ')' : ''].filter(Boolean).join(' ');
      if (text) healthBody.appendChild(h('div', { class: 'banner banner--warn set-banner' },
        icon('warning', { size: 14 }), h('div', { class: 'banner__text' }, text)));
    }

    // Точка входа в «Проверку системы» (WAVE 12): что настроено и что сделать.
    healthBody.appendChild(h('p', { class: 'set-hint small set-diag-link' },
      h('a', { class: 'set-faq__link', href: '#/settings/diagnostics' },
        icon('pulse', { size: 12 }), 'Проверка системы'),
      h('span', { class: 'muted' }, ' — версия, доска, диктовка, Vault, порт, автозапуск, плагин и последние ошибки.')));
  }

  /** Подпись исполнителя в итоге: «biz-ops» → «Операции» (техническое имя — в title). */
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
    renderHealth(healthData, null);
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
    if (!silent) renderHealth(null, null);
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
        renderHealth(data, null);
      }
      // Кнопка «Проверить доску сейчас» появляется/прячется по состоянию доски.
      refreshKick().then((changed) => {
        if (changed && !stopped) renderHealth(healthData, null);
      });
      if (store.health && store.health.set) {
        store.health.set({ loading: false, ok: Boolean(data && data.ok), error: null, data });
      }
    } catch (err) {
      if (stopped) return;
      renderHealth(null, err);
    }
  }

  /* ------------------------------------------------------------ внешний вид */

  const look = h('section', { class: 'set-section', id: 'set-look' });
  look.appendChild(sectionHeader('Внешний вид'));
  const lookBody = h('div', { class: 'set-section__body' });
  lookBody.appendChild(h('p', { class: 'set-hint small muted' },
    'Тема меняется сразу и запоминается в браузере. «Как в системе» следует за настройкой Windows.'));

  const currentPref = (store.theme && store.theme.get && store.theme.get('pref')) || 'auto';
  const themeSeg = segmented({
    options: [
      { value: 'auto', label: 'Как в системе' },
      { value: 'light', label: 'Светлая' },
      { value: 'dark', label: 'Тёмная' },
    ],
    value: currentPref,
    onchange: (value) => {
      const pref = applyTheme(value);
      if (store.theme && store.theme.set) {
        const media = window.matchMedia ? window.matchMedia('(prefers-color-scheme: dark)') : null;
        store.theme.set({ pref, resolved: pref === 'auto' ? (media && media.matches ? 'dark' : 'light') : pref });
      }
      if (typeof ctx.toast === 'function') ctx.toast('Тема: ' + THEME_NAMES[pref], 'info');
      renderThemeNote();
    },
  });
  const themeNote = h('p', { class: 'set-hint small muted' });
  function renderThemeNote() {
    const urlTheme = new URLSearchParams(location.search).get('theme');
    themeNote.textContent = urlTheme
      ? 'Сейчас тема задана адресом (?theme=' + urlTheme + '): при перезагрузке с этим параметром вернётся она, а не сохранённый выбор.'
      : '';
  }
  renderThemeNote();
  lookBody.appendChild(h('div', { class: 'set-field' },
    h('span', { class: 'field__label' }, 'Тема'),
    themeSeg,
  ));
  lookBody.appendChild(themeNote);
  look.appendChild(lookBody);
  root.appendChild(look);

  /* ---------------------------------------------------------- уведомления */

  const notify = h('section', { class: 'set-section', id: 'set-notify' });
  notify.appendChild(sectionHeader('Уведомления'));
  const notifyBody = h('div', { class: 'set-section__body' });

  const supported = typeof Notification !== 'undefined';
  let enabled = lsGet(NOTIFY_KEY, false) === true && supported;
  const permission = () => (supported ? Notification.permission : 'unsupported');
  const permissionText = { granted: 'разрешены', default: 'не запрошены', denied: 'запрещены в браузере', unsupported: 'браузер не поддерживает' };

  const checkbox = h('input', { type: 'checkbox' });
  checkbox.checked = enabled;
  const statusLine = h('span', { class: 'small muted' });

  function renderPerm() {
    const perm = permission();
    statusLine.textContent = 'Разрешение: ' + (permissionText[perm] || perm);
  }

  async function toggleNotify() {
    if (typeof Notification === 'undefined') {
      checkbox.checked = false;
      if (typeof ctx.toast === 'function') ctx.toast('Браузер не поддерживает системные уведомления', 'warn');
      return;
    }
    if (checkbox.checked) {
      let perm = Notification.permission;
      if (perm === 'default') {
        try { perm = await Notification.requestPermission(); } catch { perm = 'denied'; }
      }
      if (perm !== 'granted') {
        checkbox.checked = false;
        enabled = false;
        lsSet(NOTIFY_KEY, false);
        renderPerm();
        if (typeof ctx.toast === 'function') {
          ctx.toast(perm === 'denied'
            ? 'Браузер запретил уведомления — разрешите их в настройках сайта'
            : 'Уведомления не разрешены', 'warn');
        }
        return;
      }
      enabled = true;
      lsSet(NOTIFY_KEY, true);
    } else {
      enabled = false;
      lsSet(NOTIFY_KEY, false);
    }
    renderPerm();
    if (typeof ctx.toast === 'function') ctx.toast(enabled ? 'Уведомления включены' : 'Уведомления выключены', 'info');
  }

  checkbox.addEventListener('change', toggleNotify);
  const testBtn = h('button', {
    class: 'btn btn--secondary btn--sm', type: 'button',
    onclick: () => {
      const shown = notifyIfEnabled('Комната команды', 'Так выглядит уведомление о готовом ответе.');
      if (!shown && typeof ctx.toast === 'function') ctx.toast('Уведомление не показано: проверьте разрешение', 'warn');
    },
  }, icon('bell', { size: 12 }), 'Проверить');

  notifyBody.appendChild(h('label', { class: 'checkbox set-switch' }, checkbox,
    h('span', {}, 'Уведомлять, когда ответ готов')));
  notifyBody.appendChild(h('p', { class: 'set-hint small muted' },
    'Системное уведомление появится, когда запрос завершится или команда задаст вам вопрос. '
      + 'Показывать его будет приложение; настройка хранится в этом браузере.'));
  notifyBody.appendChild(h('div', { class: 'row row--wrap set-notify__actions' }, statusLine, h('span', { class: 'spacer' }), testBtn));
  if (!supported) {
    notifyBody.appendChild(h('div', { class: 'banner banner--info set-banner' }, icon('info', { size: 14 }),
      h('div', { class: 'banner__text' }, 'Этот браузер не умеет системные уведомления — настройка не повлияет на работу приложения.')));
  }
  renderPerm();
  notify.appendChild(notifyBody);
  root.appendChild(notify);

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

  loadHealth();
  loadProfiles();
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
