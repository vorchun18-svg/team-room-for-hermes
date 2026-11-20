/**
 * views/autonomy.js — «Уровень автономии» комнаты (W27-E6, контракт §2).
 *
 * Что человек видит и нажимает:
 *   1) три карточки-уровня простым языком: «что агент делает сам» / «что спросит»;
 *   2) предпросмотр «что изменится» ДО подтверждения (ничего не пишется в профили
 *      участников, пока человек не нажмёт «Применить уровень»);
 *   3) список «Только с моего да» — редактируемый: то, что участник не сделает
 *      сам никогда, на любом уровне;
 *   4) «Предложения по вашей истории» — что можно добавить в список, если человек
 *      часто разрешает одно и то же;
 *   5) журнал «Что агенты сделали сами» — строки без счётчика «нужно от вас»;
 *   6) заметный вид критичных запросов: что именно случится, если разрешить.
 *
 * Терминов внутреннего устройства здесь нет: только «сам делает / спросит»,
 * «только с моего да», «участник». Названия правил приходят из сервера
 * техническими — `humanClass()` переводит их на человеческий.
 *
 * Модуль проверяется без браузера: `node --test tests/autonomy-view.test.mjs`.
 */

import { h, timeAgo } from '../util.js';
import { icon } from '../icons.js';
import { ensureCss } from '../ui.js';
import { countNoun } from '../text-ru.js';

export const AUTONOMY_CSS = 'css/view-autonomy.css';

/* ─────────────────────────────────────────────────────────── три уровня */

/**
 * Уровни комнаты. `self` — что участник делает сам, `ask` — что спросит,
 * `plain` — одна строка «в двух словах» для списка и подсказок.
 */
export const LEVELS = [
  {
    id: 'careful',
    title: 'Осторожный',
    plain: 'Каждый запуск — с вашего разрешения',
    self: ['читает файлы и ищет по ним', 'раскладывает карточки и вопросы', 'пишет заметки в папке комнаты'],
    ask: ['любой запуск скрипта или команды', 'удаление и перезапись файлов', 'отправку наружу'],
    note: 'Так работают все комнаты, пока вы не выберете другой уровень.',
  },
  {
    id: 'working',
    title: 'Рабочий',
    plain: 'Свои скрипты и файлы комнаты — сам, удаление и отправку — спросит',
    recommended: true,
    self: ['запускает свои скрипты (файлом и в терминале)', 'работает с файлами в папке комнаты',
      'читает, ищет, пишет заметки'],
    ask: ['удаление и перезапись чужого', 'отправку писем и сообщений', 'реестр, службы, права администратора',
      'всё, что вне папки комнаты'],
    note: 'Рекомендуем: обычная работа идёт без вас, опасное остаётся вопросом.',
  },
  {
    id: 'free',
    title: 'Свободный',
    plain: 'Почти всё сам, кроме списка «только с моего да»',
    self: ['почти все действия', 'свои скрипты и команды без вопросов'],
    ask: ['только то, что вы внесли в «Только с моего да»'],
    warn: 'В этом уровне «спросить» уже некому: запрещённое просто останавливается, '
      + 'и участник пишет вам вопрос в карточке. Держите список «только с моего да» полным.',
  },
];

export const LEVEL_IDS = LEVELS.map((l) => l.id);

/** Уровень по id; неизвестное значение — «Рабочий» (так решено в контракте §2). */
export function levelById(id) {
  const key = String(id || '').trim();
  return LEVELS.find((l) => l.id === key) || LEVELS[1];
}

/** Уровень по умолчанию для новой комнаты. */
export const DEFAULT_LEVEL = 'working';

/* ─────────────────────────────────────── список «только с моего да» */

/** Классы по умолчанию (контракт §2, тот же список у сервера). */
export const NEVER_DEFAULTS = [
  'удаление папки целиком',
  'удаление файлов с подтверждением «всё равно»',
  'отправка писем и сообщений',
  'платежи и деньги',
  'права администратора',
  'реестр и службы Windows',
  'остановка процессов',
  'контейнеры и их хранилища',
  'скачивание и сразу запуск',
  'запись в файлы с паролями и ключами',
];

/** Привести список к одному виду: без пустых, без повторов, в порядке ввода. */
export function normalizeNever(list) {
  const out = [];
  const seen = new Set();
  for (const raw of Array.isArray(list) ? list : []) {
    const text = String(raw === null || raw === undefined ? '' : raw).replace(/\s+/g, ' ').trim();
    if (!text) continue;
    const key = text.toLowerCase();
    if (seen.has(key)) continue;
    seen.add(key);
    out.push(text);
  }
  return out;
}

/**
 * Что не так с новой записью списка. Пустая строка — годится.
 * @param {string} text
 * @param {string[]} current уже сохранённые записи
 * @returns {string}
 */
export function neverProblem(text, current = []) {
  const value = String(text === null || text === undefined ? '' : text).replace(/\s+/g, ' ').trim();
  if (!value) return 'Напишите, что участник не должен делать сам.';
  if (value.length < 3) return 'Слишком коротко: опишите действие словами (например, «удаление папки»).';
  if (value.length > 120) return 'Слишком длинно: до 120 знаков.';
  const dup = normalizeNever(current).some((item) => item.toLowerCase() === value.toLowerCase());
  if (dup) return 'Такая запись уже есть в списке.';
  return '';
}

/* ─────────────────────────────────────────── понятные названия правил */

/**
 * Техническое название правила → человеческое. Неизвестное оставляем как есть,
 * но без подчёркиваний и лишних скобок — так текст не выглядит «кодом».
 */
const CLASS_WORDS = [
  [/recursive delete|rm -rf|Remove-Item.*-Recurse|del \/s|find -delete|xargs rm/i, 'удаление файлов'],
  [/heredoc|script execution/i, 'свои скрипты'],
  [/execute_code|код на python/i, 'свой код'],
  [/sudo|администратор/i, 'права администратора'],
  [/registry|реестр/i, 'реестр'],
  [/taskkill|kill/i, 'остановка процессов'],
  [/docker|volume/i, 'контейнеры'],
  [/curl|wget|\|\s*sh/i, 'скачивание и запуск'],
  [/\.env|\.ssh|секрет|ключ/i, 'файлы с паролями'],
  [/mail|smtp|письм|отправ/i, 'отправка наружу'],
];

/**
 * @param {string} raw техническое или уже готовое название
 * @returns {string}
 */
export function humanClass(raw) {
  const text = String(raw === null || raw === undefined ? '' : raw).trim();
  if (!text) return 'действие';
  for (const [re, word] of CLASS_WORDS) if (re.test(text)) return word;
  return text.replace(/_/g, ' ');
}

/* ───────────────────────────────────────────── критичное и рутинное */

/**
 * Что считается критичным: разрушительное и «наружу». Список тот же, что у
 * сервера в классах по умолчанию (§2), — чтобы интерфейс не разошёлся с ним,
 * если сервер не прислал `severity`.
 */
const CRITICAL_HINTS = [
  /recursive delete|rm -rf|Remove-Item.*-Recurse|del \/s|find -delete|xargs rm/i,
  /taskkill|kill/i,
  /sudo|администратор|administrator/i,
  /registry|реестр/i,
  /docker\s+(rm|volume)|докер|контейнер/i,
  /curl|wget|\|\s*(ba)?sh\b/i,
  /\.env|\.ssh|\.pem|секрет|парол|ключ/i,
  /mail|smtp|письм|telegram|whatsapp|отправ/i,
  /payment|платеж|платёж|оплат/i,
  // Русские названия классов: сервер может прислать их словами — не хуже английских.
  /удалени|удалить/i,
  /остановк|остановить/i,
  /скачива/i,
];

/**
 * Критично или рутинно. Сначала верим серверу (`severity`), иначе решаем по
 * названию правила и команде: разрушительное и «наружу» — критично.
 * @param {object} item
 * @returns {'critical'|'routine'}
 */
export function classifySeverity(item) {
  const it = item || {};
  const given = String(it.severity || '').trim();
  if (given === 'critical' || given === 'routine') return given;
  const hay = [it.class, it.class_key, it.pattern_key, it.description, it.command, it.what]
    .filter(Boolean).map(String).join(' \n ');
  if (it.floor === true) return 'critical';
  if (CRITICAL_HINTS.some((re) => re.test(hay))) return 'critical';
  return 'routine';
}

/* ─────────────────────────────────────────────────── предпросмотр */

/**
 * Что изменится при выборе уровня. Данные сервера (§2: `preview[level].changes`)
 * главнее; если их нет — говорим то же самое словами из карточек уровней.
 *
 * @param {object} data ответ `GET /api/rooms/{id}/autonomy`
 * @param {string} level
 * @returns {{level:string, changes:string[], profiles:string[], note:string, warned:boolean}}
 */
export function previewFor(data, level) {
  const lvl = levelById(level);
  const raw = data && data.preview && data.preview[lvl.id];
  const changes = normalizeNever(raw && Array.isArray(raw.changes) ? raw.changes : []);
  const profiles = [];
  for (const p of (data && Array.isArray(data.profiles) ? data.profiles : [])) {
    if (!p) continue;
    profiles.push(String(p.member_title || p.profile || ''));
  }
  const note = (raw && String(raw.note || '')) || (lvl.id === 'free' ? lvl.warn || '' : lvl.note || '');
  return {
    level: lvl.id,
    changes: changes.length ? changes : lvl.self.map((s) => 'будет делать сам: ' + s),
    profiles: profiles.filter(Boolean),
    note,
    warned: Boolean(lvl.warn),
  };
}

/** Одна строка «к чему это приведёт» — для плашки и подтверждения. */
export function previewHeadline(data, level) {
  const p = previewFor(data, level);
  const title = levelById(p.level).title;
  if (!p.profiles.length) return 'Уровень «' + title + '» запомнится для комнаты: участников в ней пока нет.';
  return 'Уровень «' + title + '» применю к ' +
    countNoun(p.profiles.length, ['участнику', 'участникам', 'участникам']) + ' этой комнаты.';
}

/** Чем уровень отличается от текущего — без изменений возвращаем пусто. */
export function levelChanged(data, level) {
  const now = String((data && data.level) || DEFAULT_LEVEL);
  return String(levelById(level).id) !== String(levelById(now).id);
}

/** Тело запроса на применение уровня (всегда с подтверждением — §2). */
export function applyBody(level, never) {
  const body = { level: levelById(level).id, confirm: true };
  const list = normalizeNever(never);
  if (list.length || never !== undefined) body.never = list;
  return body;
}

/* ─────────────────────────────────────────────────────── журнал */

/**
 * Журнал: что участники сделали сами и что ждёт вашего «да». Счётчика
 * «нужно от вас» здесь нет по контракту — он живёт в карточке запроса.
 *
 * @param {object} data ответ `GET /api/rooms/{id}/autonomy/journal`
 * @returns {{self:object[], critical:object[], routine:object[], total:number}}
 */
export function journalGroups(data) {
  const items = Array.isArray(data) ? data : (data && Array.isArray(data.items) ? data.items : []);
  const self = [];
  const critical = [];
  const routine = [];
  for (const raw of items) {
    if (!raw || typeof raw !== 'object') continue;
    const item = Object.assign({}, raw, {
      kind: String(raw.kind || (raw.blocked ? 'blocked' : 'self')),
      severity: classifySeverity(raw),
      at: Number(raw.at || raw.at_s || 0) || 0,
    });
    if (item.kind === 'blocked') {
      if (item.severity === 'critical') critical.push(item);
      else routine.push(item);
    } else {
      self.push(item);
    }
  }
  const byTime = (a, b) => (b.at || 0) - (a.at || 0);
  return { self: self.sort(byTime), critical: critical.sort(byTime), routine: routine.sort(byTime),
    total: self.length + critical.length + routine.length };
}

/** Строка журнала словами: «Анна · свои скрипты · 2 часа назад · «Собрать отчёт»». */
export function journalLine(item, now) {
  const it = item || {};
  const who = String(it.member_title || it.profile || 'Участник');
  const what = humanClass(it.class || it.class_label || it.description || it.pattern_key);
  const when = it.at ? timeAgo(Number(it.at) * 1000, now) : '';
  const card = String(it.card || it.card_title || '').trim();
  return [who, what, when, card ? '«' + card + '»' : ''].filter(Boolean).join(' · ');
}

/** Итог журнала словами (без «нужно от вас»). */
export function journalSummary(data) {
  const g = journalGroups(data);
  const parts = [];
  parts.push('сделано само: ' + g.self.length);
  if (g.critical.length) parts.push('ждёт вашего да: ' + g.critical.length);
  if (g.routine.length) parts.push('остановлено, но не критично: ' + g.routine.length);
  return parts.join(' · ');
}

/** Что именно произойдёт, если разрешить: список строк для предпросмотра. */
export function whatExactly(item) {
  const it = item || {};
  const out = [];
  for (const key of ['what', 'details', 'paths', 'files']) {
    const raw = it[key];
    if (Array.isArray(raw)) for (const x of raw) { if (x) out.push(String(x)); }
    else if (typeof raw === 'string' && raw.trim()) out.push(raw.trim());
  }
  const command = String(it.command || '').trim();
  if (!out.length && command) out.push(command);
  if (Array.isArray(it.commands)) {
    for (const c of it.commands) {
      const text = typeof c === 'string' ? c : (c && c.command);
      if (text && !out.includes(String(text))) out.push(String(text));
    }
  }
  return out.slice(0, 20);
}

/** Заголовок критичной карточки: «Требует вашего «да»: удаление файлов». */
export function criticalHeadline(item) {
  const it = item || {};
  const what = humanClass(it.class || it.class_label || it.description || it.pattern_key);
  const who = String(it.member_title || it.profile || 'Участник');
  return 'Требует вашего «да»: ' + who + ' — ' + what;
}

/* ─────────────────────────────────── предложения по вашей истории */

/**
 * Предложения: что человек часто разрешает — можно добавить в список «сам
 * делает». Разрушительное сервер не предлагает вовсе; если всё же пришло —
 * помечаем, чтобы человек не добавил «удаление» в «можно сам».
 * @param {object} data ответ `GET /api/rooms/{id}/autonomy/suggest`
 * @returns {object[]}
 */
export function suggestRows(data) {
  const items = Array.isArray(data) ? data : (data && Array.isArray(data.items) ? data.items : []);
  return items.filter((it) => it && typeof it === 'object').map((it) => ({
    key: String(it.key || it.pattern_key || it.label || ''),
    label: humanClass(it.label || it.description || it.pattern_key || it.key),
    count: Number(it.count || 0) || 0,
    destructive: Boolean(it.destructive) || classifySeverity(it) === 'critical',
  })).filter((it) => it.key);
}

/* ─────────────────────────────────────────────── карточки и списки */

function chip(text, tone) {
  return h('span', { class: 'aut-chip' + (tone ? ' aut-chip--' + tone : '') }, String(text));
}

/**
 * Карточка уровня. Клик выбирает уровень (не применяет) — предпросмотр покажет,
 * что изменится.
 * @param {object} level запись из LEVELS
 * @param {{current?: string, picked?: string, onPick?: Function}} [opts]
 * @returns {HTMLElement}
 */
export function levelCard(level, opts = {}) {
  const lvl = level || LEVELS[1];
  const current = String(opts.current || '');
  const picked = String(opts.picked || '');
  const isCurrent = current === lvl.id;
  const isPicked = picked === lvl.id;
  const cls = ['aut-level'];
  if (isCurrent) cls.push('aut-level--current');
  if (isPicked) cls.push('aut-level--picked');
  const card = h('button', {
    class: cls.join(' '), type: 'button', role: 'radio',
    'aria-checked': isPicked ? 'true' : 'false', 'data-level': lvl.id,
  },
  h('span', { class: 'aut-level__head' },
    h('span', { class: 'aut-level__title' }, lvl.title),
    isCurrent ? chip('сейчас', 'current') : null,
    lvl.recommended && !isCurrent ? chip('рекомендуем', 'rec') : null),
  h('span', { class: 'aut-level__plain' }, lvl.plain),
  h('span', { class: 'aut-level__row' },
    h('span', { class: 'aut-level__label aut-level__label--self' }, icon('play-circle', { size: 12 }), 'Сам делает'),
    h('span', { class: 'aut-level__list' }, lvl.self.join(' · '))),
  h('span', { class: 'aut-level__row' },
    h('span', { class: 'aut-level__label aut-level__label--ask' }, icon('lock', { size: 12 }), 'Спросит'),
    h('span', { class: 'aut-level__list' }, lvl.ask.join(' · '))),
  lvl.warn ? h('span', { class: 'aut-level__warn' }, icon('warning', { size: 12 }), lvl.warn) : null,
  );
  card.addEventListener('click', () => { if (typeof opts.onPick === 'function') opts.onPick(lvl.id); });
  return card;
}

/**
 * Блок предпросмотра «что изменится» + подтверждение.
 * @param {object} data
 * @param {string} level
 * @param {{onApply?: Function, onCancel?: Function, busy?: boolean}} [opts]
 */
export function previewPanel(data, level, opts = {}) {
  const p = previewFor(data, level);
  const lvl = levelById(p.level);
  const same = !levelChanged(data, p.level);
  const box = h('div', { class: 'aut-preview', 'data-preview': lvl.id, role: 'group',
    'aria-label': 'Что изменится' },
  h('div', { class: 'aut-preview__head' },
    icon('eye', { size: 14 }),
    h('strong', null, 'Что изменится'),
    same ? chip('этот уровень уже включён', 'muted') : null),
  h('div', { class: 'aut-preview__lead' }, previewHeadline(data, p.level)),
  h('ul', { class: 'aut-preview__list' }, p.changes.map((line) => h('li', null, String(line)))),
  p.profiles.length
    ? h('div', { class: 'aut-preview__who' }, 'Участники: ' + p.profiles.join(', '))
    : h('div', { class: 'aut-preview__who' }, 'В комнате пока нет участников — уровень запомнится для новых.'),
  p.note ? h('div', { class: 'aut-preview__note' }, icon('info', { size: 12 }), p.note) : null,
  h('div', { class: 'aut-preview__hint' },
    'Перед изменением сохраним копию настроек участников. Отзыв — выбрать другой уровень.'),
  h('div', { class: 'aut-preview__actions' },
    h('button', {
      class: 'btn btn--primary btn--sm', type: 'button', 'data-apply-level': lvl.id,
      onclick: () => { if (typeof opts.onApply === 'function') opts.onApply(lvl.id); },
    }, icon('check', { size: 13 }), h('span', null, same ? 'Оставить этот уровень' : 'Применить уровень')),
    h('button', {
      class: 'btn btn--ghost btn--sm', type: 'button', 'data-cancel-preview': '1',
      onclick: () => { if (typeof opts.onCancel === 'function') opts.onCancel(); },
    }, 'Отмена')),
  );
  if (opts.busy) box.setAttribute('aria-busy', 'true');
  return box;
}

/**
 * Список «Только с моего да»: записи с крестиком и поле добавления.
 * @param {string[]} list
 * @param {{onRemove?: Function, onAdd?: Function, problem?: string}} [opts]
 */
export function neverList(list, opts = {}) {
  const items = normalizeNever(list);
  const input = h('input', {
    class: 'input', type: 'text', maxlength: '120', autocomplete: 'off',
    placeholder: 'Например: удаление папки вне комнаты', 'aria-label': 'Что участник не делает сам',
    'data-never-input': '1',
  });
  const problemBox = h('div', { class: 'aut-never__problem' });
  if (opts.problem) problemBox.textContent = String(opts.problem);
  const add = () => {
    if (typeof opts.onAdd !== 'function') return;
    const text = String(input.value || '').trim();
    const bad = neverProblem(text, items);
    // Плохую запись объясняем словами — молча «не сработало» человек читает как поломку.
    if (bad) { problemBox.textContent = bad; if (typeof input.focus === 'function') input.focus(); return; }
    problemBox.textContent = '';
    input.value = '';
    opts.onAdd(text);
  };
  input.addEventListener('keydown', (ev) => {
    if (ev && ev.key === 'Enter') { ev.preventDefault(); add(); }
  });
  return h('div', { class: 'aut-never', 'data-never-list': '1' },
    h('div', { class: 'aut-never__hint' },
      'Эти действия участник не выполнит сам ни на каком уровне — только с вашего «да».'),
    h('div', { class: 'aut-never__chips' },
      items.length
        ? items.map((text) => h('span', { class: 'aut-never__chip', 'data-never-item': text },
          h('span', null, text),
          h('button', {
            class: 'aut-never__x', type: 'button', title: 'Убрать из списка',
            'aria-label': 'Убрать «' + text + '»',
            onclick: () => { if (typeof opts.onRemove === 'function') opts.onRemove(text); },
          }, icon('close', { size: 11 }))))
        : h('span', { class: 'aut-never__empty' }, 'Список пуст — участник сможет всё, что разрешает уровень.'),
  ),
  h('div', { class: 'aut-never__add' }, input,
    h('button', {
      class: 'btn btn--secondary btn--sm', type: 'button', 'data-never-add': '1', onclick: add,
    }, icon('add', { size: 13 }), h('span', null, 'Добавить'))),
  problemBox,
  );
}

/**
 * «Предложения по вашей истории».
 * @param {object[]} rows результат suggestRows()
 * @param {{onAdd?: Function, onSkip?: Function}} [opts]
 */
export function suggestList(rows, opts = {}) {
  const items = Array.isArray(rows) ? rows : [];
  const box = h('div', { class: 'aut-suggest', 'data-suggest': '1' });
  if (!items.length) {
    box.appendChild(h('div', { class: 'aut-suggest__empty' },
      'Пока нечего предложить: добавления появятся, когда вы несколько раз разрешите одно и то же.'));
    return box;
  }
  for (const row of items) {
    box.appendChild(h('div', { class: 'aut-suggest__row', 'data-suggest-key': row.key },
      h('div', { class: 'aut-suggest__text' },
        h('div', { class: 'aut-suggest__label' }, row.label),
        h('div', { class: 'aut-suggest__count' },
          row.count ? 'вы разрешали ' + row.count + ' раз' : 'по вашей истории',
          row.destructive ? ' · разрушительное — добавлять не советуем' : '')),
      h('div', { class: 'aut-suggest__actions' },
        h('button', {
          class: 'btn btn--secondary btn--sm', type: 'button', 'data-suggest-add': row.key,
          onclick: () => { if (typeof opts.onAdd === 'function') opts.onAdd(row); },
        }, 'Добавить в работу сам'),
        h('button', {
          class: 'btn btn--ghost btn--sm', type: 'button', 'data-suggest-skip': row.key,
          onclick: () => { if (typeof opts.onSkip === 'function') opts.onSkip(row); },
        }, 'Не надо'))));
  }
  return box;
}

/**
 * Заметная карточка критичного запроса: заголовок, «что именно», действия.
 * Рутинные блокировки сюда не попадают — они отдельным списком, чтобы не
 * мешать важное с обычным.
 *
 * @param {object} item строка журнала с severity === 'critical'
 * @param {{onDecide?: Function, onOpen?: Function}} [opts] onDecide(item, 'once'|'deny')
 */
export function criticalCard(item, opts = {}) {
  const it = item || {};
  const exact = whatExactly(it);
  // «Нельзя разрешить» (жёсткий запрет): кнопки «Разрешить один раз» быть не должно —
  // иначе человек нажимает и ничего не происходит (тупик).
  const locked = it.floor === true || it.grantable === false;
  const box = h('div', { class: 'aut-crit' + (locked ? ' aut-crit--locked' : ''), 'data-critical': '1',
    role: 'group', 'aria-label': criticalHeadline(it) },
  h('div', { class: 'aut-crit__head' },
    icon('shield', { size: 15 }),
    h('div', { class: 'aut-crit__title' }, criticalHeadline(it)),
    locked ? chip('разрешить нельзя', 'muted') : null,
    it.at ? h('span', { class: 'aut-crit__when' }, timeAgo(Number(it.at) * 1000)) : null),
  locked
    ? h('div', { class: 'aut-crit__why' },
      'Такой запрет встроен в саму программу и не снимается разрешением. '
      + 'Можно сообщить участнику, что делать иначе, — тогда он пойдёт другим путём.')
    : null,
  it.card ? h('div', { class: 'aut-crit__card' }, 'Карточка: «' + String(it.card) + '»') : null,
  exact.length
    ? h('details', { class: 'aut-crit__more' },
      h('summary', null, 'Что именно'),
      h('div', { class: 'aut-crit__exact' }, exact.map((line) => h('code', null, line))))
    : h('div', { class: 'aut-crit__more aut-crit__more--none' },
      'Точный список не пришёл — откройте карточку, там команда целиком.'),
  h('div', { class: 'aut-crit__actions' },
    locked
      ? h('button', {
        class: 'btn btn--secondary btn--sm', type: 'button', 'data-critical-hint': it.id || '1',
        onclick: () => { if (typeof opts.onDecide === 'function') opts.onDecide(it, 'hint_agent'); },
      }, icon('lightbulb', { size: 13 }), h('span', null, 'Сообщить участнику'))
      : [
        h('button', {
          class: 'btn btn--danger btn--sm', type: 'button', 'data-critical-once': it.id || '1',
          onclick: () => { if (typeof opts.onDecide === 'function') opts.onDecide(it, 'once'); },
        }, 'Разрешить один раз'),
        h('button', {
          class: 'btn btn--ghost btn--sm', type: 'button', 'data-critical-deny': it.id || '1',
          onclick: () => { if (typeof opts.onDecide === 'function') opts.onDecide(it, 'deny'); },
        }, 'Запретить'),
      ],
    it.request_id
      ? h('a', {
        class: 'btn btn--ghost btn--sm', href: '#/room/' + encodeURIComponent(String(it.room || '')) +
          '/request/' + encodeURIComponent(String(it.request_id)), 'data-critical-open': it.request_id,
      }, icon('arrow-right', { size: 13 }), h('span', null, 'Открыть карточку'))
      : null),
  );
  return box;
}

/** Критичные запросы списком (сверху — самые свежие). */
export function criticalList(items, opts = {}) {
  const list = Array.isArray(items) ? items : [];
  if (!list.length) {
    return h('div', { class: 'aut-crit-empty' },
      'Ничего не ждёт вашего «да» — критичных запросов нет.');
  }
  const box = h('div', { class: 'aut-crit-list' });
  for (const item of list) box.appendChild(criticalCard(item, opts));
  return box;
}

/**
 * Список журнала (сделано само / остановлено).
 * @param {object[]} items
 * @param {{onOpen?: Function}} [opts]
 */
export function journalList(items, opts = {}) {
  const list = Array.isArray(items) ? items : [];
  if (!list.length) {
    return h('div', { class: 'aut-journal__empty' }, 'Пока пусто.');
  }
  const box = h('div', { class: 'aut-journal', 'data-journal': '1' });
  for (const item of list) {
    const blocked = String(item.kind) === 'blocked';
    const row = h('div', {
      class: 'aut-journal__row' + (blocked ? ' aut-journal__row--blocked' : ''),
      'data-journal-kind': blocked ? 'blocked' : 'self',
    },
    icon(blocked ? 'lock' : 'check', { size: 12 }),
    h('span', { class: 'aut-journal__text' }, journalLine(item)),
    item.request_id
      ? h('button', {
        class: 'aut-journal__link', type: 'button', 'aria-label': 'Открыть карточку',
        onclick: () => { if (typeof opts.onOpen === 'function') opts.onOpen(item); },
      }, icon('arrow-right', { size: 12 }))
      : null);
    box.appendChild(row);
  }
  return box;
}

/** Плашка «сейчас включено» — одна строка над карточками уровней. */
export function currentBanner(data) {
  const lvl = levelById((data && data.level) || DEFAULT_LEVEL);
  const never = normalizeNever(data && data.never);
  return h('div', { class: 'aut-banner', 'data-level-now': lvl.id },
    icon('shield', { size: 15 }),
    h('div', { class: 'aut-banner__text' },
      h('strong', null, 'Сейчас: ' + lvl.title),
      h('span', null, '· ' + lvl.plain.charAt(0).toLowerCase() + lvl.plain.slice(1)),
      never.length
        ? h('span', { class: 'aut-banner__never' },
          ' · только с вашего да: ' + countNoun(never.length, ['правило', 'правила', 'правил']))
        : null));
}

/* ───────────────────────────────────────────────── крепление к окну */

/**
 * Содержимое вкладки «Автономия» в настройках комнаты.
 * @param {HTMLElement} host пустой узел вкладки
 * @param {object} ctx контекст вьюхи ({api, toast})
 * @param {string} roomId
 * @returns {Promise<{reload: Function}>}
 */
export async function mountAutonomyPanel(host, ctx, roomId) {
  if (!host) return { reload: async () => {} };
  await ensureCss(AUTONOMY_CSS);
  const api = (ctx && ctx.api) || null;
  const toast = ctx && typeof ctx.toast === 'function' ? ctx.toast : () => {};
  const rid = String(roomId || '');
  const enc = encodeURIComponent(rid);

  const state = {
    data: null, picked: '', never: [], filter: 'all',
    suggest: null, journal: null, busy: false, error: '', stopped: false,
  };

  const root = h('div', { class: 'aut', 'data-autonomy': rid });
  host.appendChild(root);

  async function load() {
    state.error = '';
    try {
      const data = await api.get('/api/rooms/' + enc + '/autonomy');
      state.data = data || null;
      state.never = normalizeNever(data && data.never);
      state.picked = '';
    } catch (err) {
      state.error = (err && err.message) ? String(err.message)
        : 'Не удалось прочитать настройки автономии';
    }
    await loadJournal();
    render();
  }

  async function loadJournal() {
    try {
      const j = await api.get('/api/rooms/' + enc + '/autonomy/journal');
      state.journal = j || null;
    } catch { state.journal = state.journal || null; }
  }

  async function loadSuggest() {
    try {
      state.suggest = await api.get('/api/rooms/' + enc + '/autonomy/suggest') || { items: [] };
    } catch (err) {
      state.suggest = { items: [], error: (err && err.message) || '' };
    }
    render();
  }

  async function applyLevel(level) {
    if (state.busy) return;
    state.busy = true;
    render();
    try {
      const res = await api.post('/api/rooms/' + enc + '/autonomy', applyBody(level, state.never));
      state.data = Object.assign({}, state.data, {
        level: (res && res.level) || level,
        never: normalizeNever((res && res.never) || state.never),
        updated_at: (res && res.updated_at) || Math.floor(Date.now() / 1000),
      });
      state.never = normalizeNever(state.data.never);
      state.picked = '';
      // W28-F: «не записалось» говорим ТОЛЬКО когда это подтвердил перечитанный файл
      // (сервер присылает `warnings` из сверки с тем же config.yaml). Есть замечания —
      // это предупреждение с причиной, а не зелёный тост «включён».
      const warnings = Array.isArray(res && res.warnings) ? res.warnings.filter(Boolean) : [];
      const failed = (Array.isArray(res && res.applied) ? res.applied : [])
        .filter((row) => row && row.ok === false && !row.skipped);
      if (warnings.length || failed.length) {
        const text = warnings[0] || (res && res.message) || 'Уровень применён не у всех участников';
        toast(text + (warnings.length > 1 ? ' · ещё ' + (warnings.length - 1) : ''), 'warn');
      } else {
        toast('Уровень «' + levelById(level).title + '» включён', 'ok');
      }
      await loadJournal();
      // Перечитываем профили: в карточках — что реально лежит в config.yaml.
      load();
    } catch (err) {
      // 409 без confirm — это не ошибка: сервер прислал предпросмотр, человек решает.
      const details = (err && err.details) || {};
      const preview = details.preview
        || (details.error && details.error.detail && details.error.detail.preview) || null;
      if (preview && typeof preview === 'object') {
        state.data = Object.assign({}, state.data || {}, {
          preview: Object.assign({}, (state.data && state.data.preview) || {}, preview),
        });
      }
      toast((err && err.message) ? String(err.message) : 'Не удалось применить уровень', 'danger');
    } finally {
      state.busy = false;
      render();
    }
  }

  async function saveNever(list) {
    state.never = normalizeNever(list);
    state.busy = true;
    render();
    try {
      const res = await api.post('/api/rooms/' + enc + '/autonomy',
        { level: (state.data && state.data.level) || DEFAULT_LEVEL, never: state.never, confirm: true });
      state.data = Object.assign({}, state.data, {
        level: (res && res.level) || state.data.level,
        never: normalizeNever((res && res.never) || state.never),
      });
      toast('Список «только с моего да» сохранён', 'ok');
    } catch (err) {
      toast((err && err.message) ? String(err.message) : 'Не удалось сохранить список', 'danger');
    } finally {
      state.busy = false;
      render();
    }
  }

  function decideCritical(item, decision) {
    if (!item || !item.request_id) return;
    const body = {
      action: 'approval', decision,
      approval_id: String(item.approval_id || item.id || ''),
    };
    api.post('/api/requests/' + encodeURIComponent(String(item.request_id)) + '/actions', body)
      .then(() => {
        const text = decision === 'deny' ? 'Запрещено — участник будет искать другой способ'
          : decision === 'hint_agent' ? 'Участнику отправлена подсказка — он пойдёт другим путём'
            : 'Разрешено один раз';
        toast(text, 'ok');
        return loadJournal();
      })
      .then(render)
      .catch((err) => toast((err && err.message) ? String(err.message) : 'Не удалось ответить', 'danger'));
  }

  function openCard(item) {
    const hash = '#/room/' + encodeURIComponent(String(item.room || rid)) +
      '/request/' + encodeURIComponent(String(item.request_id));
    if (typeof window !== 'undefined' && window.location) window.location.hash = hash;
  }

  function render() {
    if (state.stopped) return;
    root.replaceChildren();
    if (state.error && !state.data) {
      root.appendChild(h('div', { class: 'aut-error' },
        h('div', null, state.error),
        h('button', { class: 'btn btn--secondary btn--sm', type: 'button', onclick: () => { load(); } },
          icon('refresh', { size: 13 }), h('span', null, 'Повторить'))));
      return;
    }
    const data = state.data || {};
    root.appendChild(currentBanner(data));

    const cards = h('div', { class: 'aut-levels', role: 'radiogroup', 'aria-label': 'Уровень автономии' });
    for (const lvl of LEVELS) {
      cards.appendChild(levelCard(lvl, {
        current: String(data.level || DEFAULT_LEVEL),
        picked: state.picked,
        onPick: (id) => { state.picked = id; render(); },
      }));
    }
    root.appendChild(h('div', { class: 'aut-section' },
      h('h3', { class: 'aut-section__title' }, 'Сколько агент делает сам'),
      h('p', { class: 'aut-section__lead' },
        'Выберите уровень — сначала покажу, что изменится. Ничего не поменяется, пока не нажмёте «Применить».'),
      cards));

    if (state.picked) {
      root.appendChild(previewPanel(data, state.picked, {
        busy: state.busy,
        onApply: (id) => applyLevel(id),
        onCancel: () => { state.picked = ''; render(); },
      }));
    }

    root.appendChild(h('div', { class: 'aut-section' },
      h('h3', { class: 'aut-section__title' }, 'Только с моего да'),
      neverList(state.never, {
        onRemove: (text) => { saveNever(state.never.filter((x) => x !== text)); },
        onAdd: (text) => { saveNever(state.never.concat([text])); },
      })));

    root.appendChild(h('div', { class: 'aut-section' },
      h('h3', { class: 'aut-section__title' }, 'Предложения по вашей истории'),
      h('p', { class: 'aut-section__lead' },
        'Смотрим, что вы разрешали чаще всего. Разрушительное не предлагаем никогда.'),
      state.suggest
        ? suggestList(suggestRows(state.suggest), {
          onAdd: (row) => { saveNever(state.never.concat([row.label])); },
          onSkip: (row) => {
            state.suggest = Object.assign({}, state.suggest, {
              items: (state.suggest.items || []).filter((it) => String(it.key || '') !== row.key),
            });
            render();
          },
        })
        : h('button', {
          class: 'btn btn--ghost btn--sm', type: 'button', 'data-suggest-load': '1',
          onclick: () => { loadSuggest(); },
        }, icon('lightbulb', { size: 13 }), h('span', null, 'Показать предложения'))));

    const g = journalGroups(state.journal);
    const journalBox = h('div', { class: 'aut-journal-wrap', 'data-journal-filter': state.filter });
    const tabs = [['all', 'Всё'], ['self', 'Сделано само'], ['critical', 'Требует вашего да']];
    const tabRow = h('div', { class: 'aut-tabs', role: 'group', 'aria-label': 'Что показать в журнале' });
    for (const [id, title] of tabs) {
      const count = id === 'self' ? g.self.length : id === 'critical' ? g.critical.length : g.total;
      const btn = h('button', {
        class: 'aut-tabs__tab' + (state.filter === id ? ' aut-tabs__tab--active' : ''),
        type: 'button', 'aria-pressed': state.filter === id ? 'true' : 'false',
        'data-journal-tab': id, onclick: () => { state.filter = id; render(); },
      }, h('span', null, title), h('span', { class: 'aut-tabs__count tnum' }, String(count)));
      tabRow.appendChild(btn);
    }
    journalBox.appendChild(tabRow);
    journalBox.appendChild(h('div', { class: 'aut-journal__summary' }, journalSummary(state.journal)));
    if (state.filter === 'critical') {
      journalBox.appendChild(criticalList(g.critical, { onDecide: decideCritical, onOpen: openCard }));
    } else if (state.filter === 'self') {
      journalBox.appendChild(journalList(g.self, { onOpen: openCard }));
    } else {
      journalBox.appendChild(criticalList(g.critical, { onDecide: decideCritical, onOpen: openCard }));
      journalBox.appendChild(h('div', { class: 'aut-journal__sub' }, 'Сделано само'));
      journalBox.appendChild(journalList(g.self.concat(g.routine), { onOpen: openCard }));
    }
    root.appendChild(h('div', { class: 'aut-section' },
      h('h3', { class: 'aut-section__title' }, 'Журнал: что участники сделали сами'),
      h('p', { class: 'aut-section__lead' },
        'Здесь записи без счётчика «нужно от вас»: это уже сделано по уровню комнаты. '
        + 'Критичное вынесено наверх отдельным видом.'),
      journalBox));

    root.appendChild(h('div', { class: 'aut-foot' },
      h('button', { class: 'btn btn--ghost btn--sm', type: 'button', onclick: () => { load(); } },
        icon('refresh', { size: 13 }), h('span', null, 'Обновить')),
      h('span', { class: 'aut-foot__hint' },
        'Изменения уровня применяются к участникам комнаты. Копия прежних настроек сохраняется, отзыв — смена уровня.')));
  }

  render();
  await load();
  loadSuggest();

  return {
    reload: load,
    unmount() { state.stopped = true; root.remove ? root.remove() : null; },
  };
}

export default {
  LEVELS, LEVEL_IDS, DEFAULT_LEVEL, NEVER_DEFAULTS, mountAutonomyPanel,
  levelById, normalizeNever, neverProblem, humanClass, classifySeverity,
  previewFor, previewHeadline, levelChanged, applyBody, journalGroups, journalLine,
  journalSummary, whatExactly, criticalHeadline, suggestRows,
};
