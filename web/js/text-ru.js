/**
 * text-ru.js — русские формы и «человеческие» подмены слов для интерфейса.
 * Чистые функции без DOM. Публично доступны через util.js (реэкспорт):
 *   import { fromForm, fromCount, plainTerms } from './util.js';
 */

/**
 * Склонение после «из»: число управляет родительным падежом.
 * Формы те же, что у plural(): [one, few, many] — именительный падеж; здесь
 * используются родительные: few (= род. ед.) и many (= род. мн.).
 *   fromForm(4, ['работа','работы','работ'])  → 'работ'   («1 из 4 работ»)
 *   fromForm(1, …)  → 'работы'   («из 1 работы»)    fromForm(21, …) → 'работы'
 *   fromForm(5, …)  → 'работ'    fromForm(11, …)    → 'работ'
 * Дробное число — родительный единственного («из 1,5 работы»).
 * @param {number} n число после «из»
 * @param {string[]} forms [one, few, many]
 * @returns {string}
 */
export function fromForm(n, forms) {
  const f = Array.isArray(forms) ? forms : [];
  const one = f[0] !== undefined ? f[0] : '';
  const few = f[1] !== undefined ? f[1] : one;
  const many = f[2] !== undefined ? f[2] : few;
  const num = Math.abs(Number(n));
  if (!isFinite(num)) return many;
  if (!Number.isInteger(num)) return few;
  if (num % 10 === 1 && num % 100 !== 11) return few;
  return many;
}

/**
 * «1 из 4 работ»: прогресс done/total с правильным падежом после «из».
 * @param {number} done
 * @param {number} total
 * @param {string[]} forms [one, few, many] — как у plural()
 * @returns {string}
 */
export function fromCount(done, total, forms) {
  return `${done} из ${total} ${fromForm(total, forms)}`;
}

/**
 * Именительный падеж по числу: 1 задача / 2 задачи / 5 задач.
 * @param {number} n
 * @param {string[]} forms [one, few, many]
 * @returns {string}
 */
function pluralForm(n, forms) {
  const f = Array.isArray(forms) ? forms : [];
  const one = f[0] !== undefined ? f[0] : '';
  const few = f[1] !== undefined ? f[1] : one;
  const many = f[2] !== undefined ? f[2] : few;
  const num = Math.abs(Number(n));
  if (!isFinite(num)) return many;
  const i = Math.floor(num);
  const mod10 = i % 10;
  const mod100 = i % 100;
  if (mod100 >= 11 && mod100 <= 14) return many;
  if (mod10 === 1) return one;
  if (mod10 >= 2 && mod10 <= 4) return few;
  return many;
}

/**
 * «2 попытки», «5 задач» — число вместе с существительным в нужной форме.
 * @param {number} n
 * @param {string[]} forms [one, few, many]
 * @returns {string}
 */
export function countNoun(n, forms) {
  const num = Number(n);
  if (!isFinite(num)) return '';
  const i = Math.trunc(num);
  return `${i} ${pluralForm(i, forms)}`;
}

/**
 * Глагол согласуется с числом: «1 часть работы ждёт», «3 части работы ждут».
 * Формы — [единственное, множественное]; «11» и «21» ведут себя как в русском
 * (11 ждут, 21 ждёт).
 * @param {number} n
 * @param {string[]} forms [one, many]
 * @returns {string}
 */
export function pluralVerb(n, forms) {
  const f = Array.isArray(forms) ? forms : [];
  const one = f[0] !== undefined ? f[0] : '';
  const many = f[1] !== undefined ? f[1] : one;
  const num = Math.abs(Number(n));
  if (!isFinite(num)) return many;
  if (!Number.isInteger(num)) return many;
  if (num % 100 >= 11 && num % 100 <= 14) return many;
  return num % 10 === 1 ? one : many;
}

const EXECUTOR_FORMS = { '': 'исполнитель', 'а': 'исполнителя', 'у': 'исполнителю', 'ом': 'исполнителем', 'е': 'исполнителе' };
const ENGINE_FORMS = { '': 'движок', 'а': 'движка', 'у': 'движку', 'ом': 'движком', 'е': 'движке' };
const FAIL_FORMS = { '': 'не справился', 'а': 'не справилась', 'о': 'не справилось', 'и': 'не справились' };

/** «упал/упала/упало/упали» → «не справился/не справилась/…»: род сохраняем. */
function failVerb(ending) {
  return FAIL_FORMS[ending || ''] || FAIL_FORMS[''];
}

/**
 * Заменяет технические слова в текстах от сервера понятными: «диспетчер» →
 * «движок», «Шлюз Hermes (pid 12)» → «Движок команды», «доска канбана» →
 * «доска задач». Падеж и регистр первой буквы сохраняются.
 * @param {*} text
 * @returns {string}
 */
export function plainTerms(text) {
  let s = String(text === null || text === undefined ? '' : text);
  s = s.replace(/(Шлюз|шлюз)\s+Hermes(\s+\(pid\s+\d+\))?/g, (m, w) => (w === 'Шлюз' ? 'Движок команды' : 'движок команды'));
  s = s.replace(/(Движок|движок)\s+канбана/g, '$1 команды');
  s = s.replace(/(Доск[а-я]+|доск[а-я]+)\s+канбана/g, '$1 задач');
  s = s.replace(/(Ошибка|ошибка)\s+канбана/g, '$1 движка');
  s = s.replace(/([Дд])испетчер(ом|а|у|е)?/g, (m, first, end) => {
    const word = ENGINE_FORMS[end || ''];
    return first === 'Д' ? word[0].toUpperCase() + word.slice(1) : word;
  });
  // «Воркер упал 2 раз(а)…» → «Исполнитель не справился 2 раза…»: поломка
  // называется действием, а не техническим состоянием процесса.
  s = s.replace(/(\S+)\s+упал(а|о|и)?\s+(\d+)\s+раз(?:\(а\)|а)?(\s+подряд)?/gi,
    (m, who, end, count) => `${who} ${failVerb(end)} ${countNoun(Number(count), ['раз', 'раза', 'раз'])}${m.includes('подряд') ? ' подряд' : ''}`);
  s = s.replace(/(\S+)\s+упал(а|о|и)?\s+дважды/gi,
    (m, who, end) => `${who} ${failVerb(end)} дважды`);
  s = s.replace(/([Вв])оркер(ом|а|у|е)?/g, (m, first, end) => {
    const word = EXECUTOR_FORMS[end || ''];
    return first === 'В' ? word[0].toUpperCase() + word.slice(1) : word;
  });
  s = s.replace(/(\S+)\s+упал(а|о|и)?(?=[\s.,;:!?)]|$)/gi, (m, who, end) => `${who} ${failVerb(end)}`);
  return s;
}

/**
 * Коды ошибок API → человеческие фразы. Код в интерфейсе не показываем:
 * он остаётся в консоли и в подсказке для разработчика.
 */
const ERROR_RU = {
  not_found: 'Запрос не найден — возможно, его убрали в архив.',
  bad_request: 'Запрос составлен неверно — обновите страницу и попробуйте снова.',
  conflict: 'Данные уже изменились — обновите страницу и повторите.',
  engine_error: 'Движок команды не смог выполнить действие. Попробуйте ещё раз.',
  engine_down: 'Движок команды остановлен — действие не выполнено.',
  timeout: 'Приложение не ответило вовремя. Попробуйте ещё раз.',
  network: 'Нет связи с приложением.',
  forbidden: 'Это действие здесь недоступно.',
  unknown: 'Не удалось выполнить действие. Попробуйте ещё раз.',
};

/**
 * Текст ошибки для показа: словарь по коду, иначе сообщение сервера.
 * Код 'network' оставляем — по нему errorBanner добавляет подсказку, как
 * запустить приложение; остальные коды наружу не отдаём.
 * @param {{code?: string, message?: string, status?: number}|*} err
 * @returns {{message: string, code: string}}
 */
export function errorText(err) {
  const e = err && typeof err === 'object' ? err : {};
  const code = String(e.code || '');
  const fallback = plainTerms(String(e.message || '')).trim();
  if (code === 'network') return { message: fallback || ERROR_RU.network, code: 'network' };
  if (ERROR_RU[code]) return { message: ERROR_RU[code], code: '' };
  if (/^http_5/.test(code)) return { message: ERROR_RU.engine_error, code: '' };
  if (/^http_4/.test(code)) return { message: ERROR_RU.bad_request, code: '' };
  if (!code && !fallback) return { message: ERROR_RU.unknown, code: '' };
  return { message: fallback || ERROR_RU.unknown, code: '' };
}
