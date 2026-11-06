/**
 * participant-label.js — единая подпись участника команды (WAVE13, пункт 11).
 *
 * Замечание Михаила: карточки подписаны только техническим именем профиля
 * («biz-ops», «biz-strategy») и общей ролью «исполнитель» — кто это и чем
 * занимается, непонятно. У профилей Hermes описание есть, и сервер уже разбирает
 * его на имя и должность (`server/profile_info.py`): «Анна — финансы и экономика (CFO)».
 *
 * Правило одно на все экраны — «Команда», выбор участников при создании комнаты,
 * чипы участников в запросах, дерево работ, адресаты вопросов, карта проекта:
 *
 *   participantLabel(profile, {room}) → {profile, name, job, title, short, role}
 *
 * `title`  — заголовок («Анна — финансы и экономика (CFO)»);
 * `short`  — для чипов и строк («Анна»);
 * `role`   — роль в комнате (её показываем бейджем, а не заголовком);
 * `profile`— техническое имя профиля (мелкой строкой под заголовком);
 * `source` — откуда подпись: member | describe | profile | role.
 *
 * Данные приходят из /api/profiles (имя и должность уже разобраны сервером) и из
 * /api/rooms/{id}/team (у участника поля label/name/job). Пока каталог не загружен,
 * подпись не ломается: показываем роль, как раньше.
 *
 * Чистые функции (без DOM) — проверяются node --test (tests/participant-label.test.mjs).
 */

import { displayName, roleTitle, roleNoun } from './shell-labels.js';
import { api as defaultApi } from './api.js';

/** Каталог профилей: profile → {profile, name, job, label, description, title}. */
const directory = new Map();
let loading = null;
let loadedAt = 0;
const TTL_MS = 60000;

/**
 * Длинная должность → короткая часть для заголовка («Ornith — локальный
 * исполнитель-помощник», а не 300 знаков). Полный текст остаётся описанием.
 * То же правило, что `short_job` в server/profile_info.py.
 */
export function shortJob(job, limit = 80) {
  const text = String(job || '').replace(/\s+/g, ' ').trim();
  if (text.length <= limit) return text;
  const m = text.match(/\s*[(:;]|\s+[—–]\s+/);
  if (m && m.index >= 12) return text.slice(0, m.index).replace(/[\s.,:;—-]+$/, '');
  const cut = text.slice(0, limit);
  return (cut.includes(' ') ? cut.slice(0, cut.lastIndexOf(' ')) : cut).replace(/[\s.,:;—-]+$/, '');
}

/** Собрать «Имя — должность» из частей (пустые части выбрасываем). */
export function assembleLabel(name, job) {
  const parts = [String(name || '').trim(), String(job || '').trim()].filter(Boolean);
  return parts.join(' — ');
}

/** Наполнить каталог из ответа /api/profiles (`{items}` или массив). */
export function setProfileDirectory(payload) {
  const items = Array.isArray(payload) ? payload : (payload && Array.isArray(payload.items) ? payload.items : []);
  for (const item of items) {
    if (!item || typeof item !== 'object') continue;
    const profile = String(item.profile || item.name || '').trim();
    if (!profile) continue;
    directory.set(profile, {
      profile,
      name: String(item.name || ''),
      job: String(item.job || ''),
      label: String(item.label || ''),
      label_source: String(item.label_source || ''),
      description: String(item.description || ''),
      title: String(item.title || ''),
    });
  }
  loadedAt = Date.now();
  return directory;
}

/** Очистить каталог (тесты и смена профилей в настройках). */
export function clearProfileDirectory() {
  directory.clear();
  loadedAt = 0;
  loading = null;
}

/** Что уже известно о профиле (без запроса к серверу). */
export function profileEntry(profile) {
  return directory.get(String(profile || '').trim()) || null;
}

/** Загрузить каталог один раз на страницу (повторные вызовы — из памяти). */
export async function ensureProfileDirectory(api, { force = false } = {}) {
  if (!api || typeof api.get !== 'function') return directory;
  if (!force && loadedAt && (Date.now() - loadedAt) < TTL_MS) return directory;
  if (loading) return loading;
  loading = api.get('/api/profiles')
    .then((data) => setProfileDirectory(data))
    .catch(() => directory)
    .then((value) => { loading = null; return value; });
  return loading;
}

/**
 * Ленивая подгрузка каталога: подпись нужна уже при первой отрисовке чипов, а
 * справочник приходит одним запросом. Без сети/сервера просто остаёмся на роли.
 */
function warmDirectory() {
  if (loadedAt || loading) return;
  ensureProfileDirectory(defaultApi).catch(() => {});
}

/** Участник комнаты по профилю (room.members). */
export function memberOf(room, profile) {
  const id = String(profile || '').trim();
  if (!id || !room || !Array.isArray(room.members)) return null;
  return room.members.find((m) => m && String(m.profile || '') === id) || null;
}

/** Роль профиля в комнате: руководитель и критик — по настройкам комнаты. */
export function roleOf(room, profile) {
  const id = String(profile || '').trim();
  if (!room || !id) return '';
  if (String(room.orchestrator || '') === id) return 'orchestrator';
  if (String(room.critic || '') === id) return 'critic';
  const member = memberOf(room, id);
  return member ? String(member.role || 'worker') : '';
}

/**
 * Подпись из готовых частей (без обращения к каталогу): карточка «Команды» уже
 * получает name/job от сервера и не должна искать их второй раз.
 * @param {{profile?: string, name?: string, job?: string, role?: string,
 *          title?: string, source?: string, pending?: boolean}} parts
 */
export function labelOf(parts = {}) {
  const p = parts || {};
  const id = String(p.profile == null ? '' : p.profile).trim();
  const role = String(p.role || '');
  const name = String(p.name || '');
  const job = String(p.job || '');
  // `label` от сервера уже короткий (заголовок); иначе собираем сами.
  const title = String(p.label || '').trim() || assembleLabel(name, shortJob(job));
  // Нет описания (например `default`) — подпись из роли в комнате: «Руководитель
  // комнаты», «Исполнитель». Роль короткая и понятная, в отличие от названия профиля.
  const roleText = role ? upper(roleTitle(role)) : '';
  const fallback = title || roleText || displayName(id, role, p.title || '');
  return {
    profile: id,
    name,
    job,
    title: fallback,
    short: name || fallback,
    role: roleTitle(role),
    roleShort: roleNoun(role),
    roleKey: role,
    sub: id,
    source: title ? String(p.source || 'profile') : 'role',
    pending: p.pending === true,
  };
}

/** Первая буква заглавной (JS без зависимостей). */
function upper(text) {
  const s = String(text || '');
  return s ? s.charAt(0).toUpperCase() + s.slice(1) : s;
}

/**
 * Подпись участника: имя и должность, иначе роль (как до WAVE13).
 * @param {string} profile технический профиль
 * @param {{member?: object, role?: string, room?: object, entry?: object}} [opts]
 * @returns {{profile: string, name: string, job: string, title: string, short: string,
 *            role: string, sub: string, source: string, pending: boolean}}
 */
export function participantLabel(profile, opts = {}) {
  const id = String(profile == null ? '' : profile).trim();
  const room = opts.room || null;
  const member = opts.member || (room ? memberOf(room, id) : null);
  const role = String((member && member.role) || opts.role || (room ? roleOf(room, id) : '') || '');
  const entry = opts.entry || (id ? directory.get(id) : null) || null;
  if (!entry && !(member && (member.name || member.job)) && id) warmDirectory();

  const source = (member || entry || {});
  // WAVE17 B-17: руководитель и критик не лежат в `members` — их подпись
  // приходит из карты комнаты `role_labels`. Источник тот же, что у «Подписи»
  // исполнителя, поэтому все экраны получают её без отдельных правил.
  const roleLabel = String(((room && room.role_labels) || {})[id] || '').trim();
  const custom = String(source.label || '').trim() || roleLabel;
  return labelOf({
    profile: id,
    name: source.name || '',
    job: source.job || '',
    label: custom,
    role,
    title: (member && member.title) || (entry && entry.title) || '',
    source: custom && !source.label ? 'member' : (source.label_source || ''),
    pending: source.label_pending === true,
  });
}

export default {
  assembleLabel,
  shortJob,
  setProfileDirectory,
  clearProfileDirectory,
  profileEntry,
  ensureProfileDirectory,
  memberOf,
  roleOf,
  labelOf,
  participantLabel,
};
