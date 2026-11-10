/**
 * shell-labels.js — человеческие подписи профилей и числовых бейджей.
 *
 * Зачем: сервер отдаёт профиль как `biz-director` и подпись `biz director`
 * (это тот же id через пробел). В карточке участника выходило
 * «biz director / biz-director · biz-director» — три раза одно и то же.
 * Здесь одно правило на все экраны: показываем русское имя и роль, системный
 * id — только в title (подсказке).
 *
 * Чистые функции без DOM: проверяются node --test (tests/frontnav-labels.test.mjs).
 */

import { plural } from './util.js';

/** Роль комнаты → существительное мужского рода. */
const ROLE_NOUNS = {
  orchestrator: 'руководитель',
  director: 'руководитель',
  worker: 'исполнитель',
  critic: 'критик',
  observer: 'наблюдатель',
  utility: 'помощник',
};

/**
 * Роль комнаты → русское существительное.
 * @param {string} role
 * @returns {string}
 */
export function roleNoun(role) {
  return ROLE_NOUNS[String(role || '')] || 'исполнитель';
}

/** Роль комнаты → подпись в карточке: «руководитель комнаты», «исполнитель». */
export function roleTitle(role) {
  const key = String(role || '');
  if (key === 'orchestrator' || key === 'director') return 'руководитель комнаты';
  if (key === 'critic') return 'критик комнаты';
  if (key === 'observer') return 'наблюдатель';
  if (key === 'utility') return 'помощник';
  return 'исполнитель';
}

/**
 * Похоже ли название профиля на системный id («biz director», «default»)?
 * Без кириллицы и с теми же словами, что id, — это не имя, а идентификатор.
 * @param {string} title
 * @param {string} [profile]
 * @returns {boolean}
 */
export function isMachineName(title, profile) {
  const text = String(title === null || title === undefined ? '' : title).trim();
  if (!text) return true;
  const id = String(profile === null || profile === undefined ? '' : profile).trim().toLowerCase();
  const normalized = text.toLowerCase().replace(/[\s_]+/g, '-');
  if (id && normalized === id) return true;
  return !/[а-яё]/i.test(text);
}

/**
 * Имя для показа: человеческое название профиля, иначе роль с большой буквы.
 * @param {string} profile системный id
 * @param {string} [role] роль в комнате
 * @param {string} [title] название профиля из /api/profiles
 * @returns {string}
 */
export function displayName(profile, role, title) {
  const text = String(title === null || title === undefined ? '' : title).trim();
  if (text && !isMachineName(text, profile)) return text;
  const noun = roleNoun(role);
  return noun.charAt(0).toUpperCase() + noun.slice(1);
}

/**
 * Подпись в списке выбора профиля: без дубля «default · default».
 * @param {{profile?:string, name?:string, title?:string}} p
 * @returns {string}
 */
export function profileOptionLabel(p) {
  const profile = String((p && (p.profile || p.name)) || '').trim();
  const title = String((p && p.title) || '').trim();
  if (!profile) return title;
  if (!title || isMachineName(title, profile)) return profile;
  return title + ' · ' + profile;
}

/**
 * «9 дел», «7 запросов» — число с существительным в правильной форме.
 * @param {number} n
 * @param {string[]} forms [one, few, many]
 * @returns {string}
 */
export function countText(n, forms) {
  return String(n) + ' ' + plural(n, forms);
}

/** Формы существительных, которые нужны бейджам. */
export const COUNT_FORMS = {
  case: ['дело', 'дела', 'дел'],
  request: ['запрос', 'запроса', 'запросов'],
  question: ['вопрос', 'вопроса', 'вопросов'],
  decision: ['решение', 'решения', 'решений'],
  block: ['блокировка', 'блокировки', 'блокировок'],
  member: ['участник', 'участника', 'участников'],
  work: ['работа', 'работы', 'работ'],
};

/**
 * Название комнаты для служебных подписей: подчёркивание — часть имени
 * («Рабочее пространство»), но в тостах и уведомлениях читается как опечатка (D16).
 * @param {string} title
 * @returns {string}
 */
export function softRoomName(title) {
  return String(title === null || title === undefined ? '' : title).replace(/_/g, ' ').trim();
}

/**
 * Ответ сервера → фраза для человека: без имён полей в скобках и кодов (D3).
 * @param {string} message
 * @returns {string}
 */
export function humanApiMessage(message) {
  const text = String(message === null || message === undefined ? '' : message).replace(/\s*\([a-z_]+\)/g, '');
  if (/хотя бы один исполнитель/i.test(text)) {
    return 'Отметьте хотя бы одного исполнителя в блоке «Состав» — без него комнате некого звать.';
  }
  if (/Нужен директор|Нужен руководитель/i.test(text)) return 'Выберите руководителя комнаты — он раздаёт работу.';
  return text;
}

/**
 * Подписать числовой бейдж существительным для скринридера и подсказки (D18).
 * @param {HTMLElement|null} node бейдж (обычно .section-header__count)
 * @param {number} n
 * @param {string[]} forms
 * @returns {HTMLElement|null}
 */
export function describeCount(node, n, forms) {
  if (!node || typeof node.setAttribute !== 'function') return node;
  const text = countText(n, forms);
  node.setAttribute('aria-label', text);
  node.setAttribute('title', text);
  return node;
}

export default { roleNoun, roleTitle, isMachineName, displayName, profileOptionLabel, countText, COUNT_FORMS, describeCount };
