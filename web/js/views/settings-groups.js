/**
 * settings-groups.js — чистая логика экранов «Настройки» и «Проверка системы»
 * (W20-U2, пункт 15 / P11).
 *
 * Здесь нет DOM: только преобразования ответов API в то, что рисуют
 * views/settings.js и views/diagnostics.js. Проверяется node --test
 * (tests/settings-groups.test.mjs), поэтому любая правка правил группировки
 * ловится тестом, а не глазами.
 *
 * Три группы «Настроек» — из аудита 06.11 (P11):
 *   Статус → Пути и данные → Диагностика.
 * «Внешний вид и уведомления» — единственный блок, который сохраняется кнопкой.
 */

/* ── Группы экрана «Настройки» ────────────────────────────────────────────── */

export const SETTINGS_GROUPS = [
  {
    id: 'status',
    title: 'Статус',
    hint: 'Одна строка ответа: всё ли работает и что делать, если нет.',
  },
  {
    id: 'paths',
    title: 'Пути и данные',
    hint: 'Где лежат данные приложения. Видны последние папки пути — они и нужны человеку.',
  },
  {
    id: 'diagnostics',
    title: 'Диагностика',
    hint: 'Что настроено, а что нет. Подробный разбор — в «Проверке системы».',
  },
  {
    id: 'prefs',
    title: 'Внешний вид и уведомления',
    hint: 'Эти настройки сохраняются одной кнопкой — черновик видно сразу, но применится после «Сохранить».',
  },
];

/** Группа по идентификатору (для подписей и якорей). */
export function settingsGroup(id) {
  const key = String(id || '').trim();
  return SETTINGS_GROUPS.find((g) => g.id === key) || null;
}

/* ── Пути: сокращение слева ───────────────────────────────────────────────── */

const SEPARATORS = /[\\/]/;

/**
 * Сократить путь СЛЕВА, оставив хвост (папку/файл) — он и нужен человеку.
 *
 * `C:\Users\User\AppData\Local\hermes\cache\scratch\w20u2\data` → `…\scratch\w20u2\data`.
 * Полный путь всегда уходит в подсказку (title) и в копирование — ничего не теряется.
 *
 * @param {string} path
 * @param {number} [maxChars] желаемая длина результата (≥ 12)
 * @returns {string}
 */
export function shortenPathLeft(path, maxChars = 46) {
  const text = String(path === null || path === undefined ? '' : path).trim();
  if (!text) return '';
  const max = Math.max(12, Math.trunc(Number(maxChars) || 46));
  if (text.length <= max) return text;
  // Хвост без одного знака под «…»; режем по разделителю, чтобы не рвать имя папки.
  let tail = text.slice(text.length - (max - 1));
  const cut = tail.search(SEPARATORS);
  if (cut > -1 && cut < tail.length - 6) tail = tail.slice(cut);
  return '…' + tail;
}

/** Последний сегмент пути: «данные» в строке «Содержимое» не дублируют путь. */
export function baseName(path) {
  const text = String(path === null || path === undefined ? '' : path).trim().replace(/[\\/]+$/, '');
  if (!text) return '';
  const parts = text.split(SEPARATORS).filter(Boolean);
  return parts.length ? parts[parts.length - 1] : text;
}

/* ── Проверки «Проверки системы»: группы ──────────────────────────────────── */

/** Порядок статусов: сначала то, что мешает работать. */
export const STATUS_RANK = { fail: 0, warn: 1, off: 2, ok: 3 };

/**
 * Статусы проверки: подпись, значок, тон. Один словарь на «Настройки» и
 * «Проверку системы» — иначе один и тот же пункт называется по-разному.
 */
export const CHECK_STATUS = {
  ok: { label: 'в порядке', icon: 'check', chip: 'ok', tone: 'ok' },
  warn: { label: 'обратите внимание', icon: 'warning', chip: 'warn', tone: 'warn' },
  fail: { label: 'не работает', icon: 'error', chip: 'danger', tone: 'danger' },
  off: { label: 'не настроено', icon: 'circle-slash', chip: 'muted', tone: 'muted' },
};

/**
 * Группы проверок: одна таблица — одно место. Новый пункт без группы попадает
 * в «Прочее» и не пропадает с экрана.
 */
export const CHECK_GROUPS = [
  {
    id: 'program',
    title: 'Программа и место на диске',
    hint: 'Версия, система, порт и свободное место.',
    ids: ['version', 'system', 'port', 'disk'],
  },
  {
    id: 'hermes',
    title: 'Hermes и агенты',
    hint: 'Без Hermes комната только показывает доску и не запускает агентов.',
    ids: ['hermes_home', 'hermes_cli', 'plugin'],
  },
  {
    id: 'data',
    title: 'Доска, зеркало и папки',
    hint: 'Где хранятся задачи, заметки для людей и файлы проекта.',
    ids: ['board', 'vault', 'project'],
  },
  {
    id: 'comfort',
    title: 'Запуск и удобства',
    hint: 'Автозапуск, диктовка и последние ошибки.',
    ids: ['autostart', 'dictation', 'logs'],
  },
  {
    id: 'other',
    title: 'Прочее',
    hint: '',
    ids: [],
  },
];

const GROUP_BY_ID = new Map();
for (const group of CHECK_GROUPS) for (const id of group.ids) GROUP_BY_ID.set(id, group.id);

/** Группа пункта проверки: известные id — по таблице, остальные — «Прочее». */
export function checkGroupId(checkId) {
  const key = String(checkId === null || checkId === undefined ? '' : checkId).trim();
  return GROUP_BY_ID.get(key) || 'other';
}

/** Нормализовать статус: неизвестный считаем предупреждением (как diagnostics.js). */
export function normalizeStatus(status) {
  const key = String(status === null || status === undefined ? '' : status).trim().toLowerCase();
  return Object.prototype.hasOwnProperty.call(STATUS_RANK, key) ? key : 'warn';
}

/** Счётчики по статусам. */
export function checksSummary(checks) {
  const out = { ok: 0, warn: 0, fail: 0, off: 0, total: 0 };
  for (const item of Array.isArray(checks) ? checks : []) {
    if (!item || typeof item !== 'object') continue;
    out[normalizeStatus(item.status)] += 1;
    out.total += 1;
  }
  return out;
}

/** Худший статус набора (fail → warn → off → ok); null для пустого. */
export function worstStatus(items) {
  let worst = null;
  for (const item of Array.isArray(items) ? items : []) {
    const status = normalizeStatus(item && item.status);
    if (worst === null || STATUS_RANK[status] < STATUS_RANK[worst]) worst = status;
  }
  return worst;
}

/** Проблемы и предупреждения, которые стоит показать сразу (fail, затем warn). */
export function topProblems(checks, limit = 3) {
  const list = (Array.isArray(checks) ? checks : [])
    .filter((item) => item && typeof item === 'object')
    .map((item, index) => ({ item, index }))
    .filter((entry) => ['fail', 'warn'].includes(normalizeStatus(entry.item.status)))
    .sort((a, b) => (STATUS_RANK[normalizeStatus(a.item.status)] - STATUS_RANK[normalizeStatus(b.item.status)])
      || (a.index - b.index))
    .map((entry) => entry.item);
  const max = Math.max(0, Math.trunc(Number(limit) || 0));
  return max ? list.slice(0, max) : list;
}

/**
 * Разложить проверки по группам.
 *
 * Внутри группы — «сначала проблемы»; группы идут по худшему статусу, поэтому
 * красный пункт всегда наверху экрана (и первым в разметке). Пустые группы не
 * возвращаются вовсе — заголовков без содержимого на экране нет.
 *
 * @param {Array<object>} checks
 * @returns {Array<{id:string,title:string,hint:string,items:object[],worst:string,counts:object}>}
 */
export function groupChecks(checks) {
  const buckets = new Map(CHECK_GROUPS.map((group) => [group.id, []]));
  for (const item of Array.isArray(checks) ? checks : []) {
    if (!item || typeof item !== 'object') continue;
    buckets.get(checkGroupId(item.id)).push(item);
  }
  const out = [];
  CHECK_GROUPS.forEach((group, order) => {
    const items = buckets.get(group.id) || [];
    if (!items.length) return;
    const sorted = items
      .map((item, index) => ({ item, index }))
      .sort((a, b) => (STATUS_RANK[normalizeStatus(a.item.status)] - STATUS_RANK[normalizeStatus(b.item.status)])
        || (a.index - b.index))
      .map((entry) => entry.item);
    const worst = worstStatus(sorted);
    out.push({
      id: group.id,
      title: group.title,
      hint: group.hint,
      items: sorted,
      worst,
      counts: checksSummary(sorted),
      order,
    });
  });
  return out
    .sort((a, b) => (STATUS_RANK[a.worst] - STATUS_RANK[b.worst]) || (a.order - b.order))
    .map(({ order, ...rest }) => rest);
}

/* ── Один сводный статус «Настроек» ───────────────────────────────────────── */

/**
 * Сводный статус приложения по ответу GET /api/health.
 *
 * Один понятный вывод вместо трёх «работает» подряд (аудит 06.11):
 * движок, фоновая проверка, запись данных и предупреждения сервера.
 *
 * @param {object|null} health
 * @returns {{tone:string, icon:string, title:string, text:string, issues:string[]}}
 */
export function overallStatus(health) {
  if (!health || typeof health !== 'object') {
    return { tone: 'muted', icon: 'sync', title: 'Проверяю…', text: 'Читаю состояние приложения.', issues: [] };
  }
  const app = health.app && typeof health.app === 'object' ? health.app : {};
  const engine = health.engine && typeof health.engine === 'object' ? health.engine : {};
  const dispatcher = health.dispatcher && typeof health.dispatcher === 'object' ? health.dispatcher : {};
  const watcher = health.watcher && typeof health.watcher === 'object' ? health.watcher : {};
  const data = health.data && typeof health.data === 'object' ? health.data : {};
  const issues = [];

  for (const warning of Array.isArray(health.warnings) ? health.warnings : []) {
    const text = typeof warning === 'string'
      ? warning
      : [warning && warning.message, warning && warning.code ? '(' + warning.code + ')' : ''].filter(Boolean).join(' ');
    if (text) issues.push(text);
  }

  // Доска не читается — это остановка работы, а не «замечание».
  if (engine.schema_ok === false || engine.ok === false) {
    return {
      tone: 'danger', icon: 'error', title: 'Движок не отвечает',
      text: 'Доска задач не читается: задачи не раздаются и ответы не приходят. Начните с «Проверки системы».',
      issues: ['доска задач не читается'].concat(issues),
    };
  }
  const dispatcherState = String(dispatcher.state || (dispatcher.ok === false ? 'stopped' : 'running'));
  if (dispatcherState !== 'running') {
    return {
      tone: 'danger', icon: 'error', title: 'Движок команды не запущен',
      text: 'Пока движок стоит, готовые задачи не уходят исполнителям. Перезапустите приложение.',
      issues: ['движок команды не запущен'].concat(issues),
    };
  }

  if (watcher.running === false) issues.push('фоновая проверка остановлена');
  if (data.writable === false) issues.push('данные открыты только для чтения');

  if (issues.length) {
    const shown = issues.slice(0, 2).join(' · ');
    const rest = issues.length > 2 ? ' · ещё ' + (issues.length - 2) : '';
    return {
      tone: 'warn', icon: 'warning', title: 'Работает, но есть замечания',
      text: shown + rest, issues,
    };
  }
  if (app.sandbox === true) {
    return {
      tone: 'muted', icon: 'beaker', title: 'Песочница',
      text: 'Настоящие агенты не запускаются: данные и запуски изолированы.',
      issues: [],
    };
  }
  return {
    tone: 'ok', icon: 'check', title: 'Всё работает',
    text: 'Доска, движок и фоновая проверка в порядке.',
    issues: [],
  };
}

/* ── Один сохраняющий блок ────────────────────────────────────────────────── */

/** Подписи изменяемых настроек для строки «что не сохранено». */
export const PREF_LABELS = {
  theme: 'тема',
  notify: 'уведомления',
};

/**
 * Что именно человек поменял и ещё не сохранил.
 * @param {object} saved что лежит в хранилище
 * @param {object} draft что выбрано на экране
 * @returns {string[]} ключи изменений в порядке PREF_LABELS
 */
export function pendingChanges(saved, draft) {
  const before = saved && typeof saved === 'object' ? saved : {};
  const after = draft && typeof draft === 'object' ? draft : {};
  const out = [];
  for (const key of Object.keys(PREF_LABELS)) {
    if (!Object.prototype.hasOwnProperty.call(after, key)) continue;
    if (!Object.prototype.hasOwnProperty.call(before, key)) continue;
    if (String(before[key]) !== String(after[key])) out.push(key);
  }
  return out;
}

/**
 * Строка под кнопкой сохранения: либо «всё сохранено», либо список изменений.
 * @param {string[]} changes
 * @returns {{dirty:boolean, text:string}}
 */
export function saveSummary(changes) {
  const list = Array.isArray(changes) ? changes.filter((key) => PREF_LABELS[key]) : [];
  if (!list.length) return { dirty: false, text: 'Все настройки сохранены.' };
  return {
    dirty: true,
    text: 'Не сохранено: ' + list.map((key) => PREF_LABELS[key]).join(', ') + '.',
  };
}
