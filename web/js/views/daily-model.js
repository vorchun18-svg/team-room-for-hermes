/**
 * daily-model.js — чистые решения «Итога дня» (W29B-Q8).
 *
 * Ни DOM, ни сети: считаем подписи, порядок и то, что человек увидит. Вид
 * (`daily.js`) только раскладывает готовые строки. Так экран не «думает» дважды,
 * и всё, что можно проверить словами, проверяется в `tests/daily-model.test.mjs`.
 *
 * Данные — `GET /api/daily` (§ см. `docs/API.md`, «W29B-Q8»). Модель терпима к
 * пропускам: сервер может не отдать раздел — экран покажет пустое состояние.
 */

import { countNoun } from '../text-ru.js';

/** Порядок и названия разделов итога — один источник для вида и тестов. */
export const DAILY_SECTIONS = [
  { key: 'done', title: 'Сделано', empty: 'За сутки ничего не завершено' },
  { key: 'decisions', title: 'Решено', empty: 'Решений за сутки не было' },
  { key: 'waiting', title: 'Ждёт вас', empty: 'Ждать от вас нечего' },
  { key: 'files', title: 'Файлы', empty: 'Новых файлов нет' },
  { key: 'stopped', title: 'Остановлено', empty: 'Ничего не остановилось' },
];

/** Подписи видов ожидания — как на остальных экранах. */
export const KIND_LABELS = {
  question: 'вопрос',
  decision: 'решение',
  approval: 'согласование',
  blocked: 'заблокированная задача',
  draft: 'черновик',
  failure: 'сбой',
  stalled: 'карточка не отвечает',
};

/** Формы для чисел итога. */
export const FORMS = {
  request: ['запрос', 'запроса', 'запросов'],
  decision: ['решение', 'решения', 'решений'],
  file: ['файл', 'файла', 'файлов'],
  case: ['дело', 'дела', 'дел'],
  run: ['запуск', 'запуска', 'запусков'],
  token: ['токен', 'токена', 'токенов'],
};

function list(value) {
  return Array.isArray(value) ? value.filter((item) => item && typeof item === 'object') : [];
}

function num(value) {
  const n = Number(value);
  return Number.isFinite(n) ? n : 0;
}

/** «1 234 567» → «1,2 млн» / «12 тыс.» — коротко и без ложной точности. */
export function shortNumber(value) {
  const n = Math.abs(num(value));
  if (n >= 1_000_000_000) return (num(value) / 1_000_000_000).toFixed(1).replace('.', ',') + ' млрд';
  if (n >= 1_000_000) return (num(value) / 1_000_000).toFixed(1).replace('.', ',') + ' млн';
  if (n >= 10_000) return Math.round(num(value) / 1000) + ' тыс.';
  return String(Math.round(num(value)));
}

/** Деньги: «$0,31». Без валюты, если ноль и вызовов не было. */
export function money(value, currency = '$') {
  const n = num(value);
  if (!n) return '';
  if (n < 0.01) return currency + n.toFixed(4).replace('.', ',');
  return currency + n.toFixed(2).replace('.', ',');
}

/** Расход за сутки одной строкой; пусто — данных нет. */
export function spentText(spent) {
  const s = spent || {};
  const calls = num(s.calls);
  const tokens = num(s.tokens);
  const cost = num(s.cost_usd);
  if (!calls && !tokens && !cost) return '';
  const parts = [];
  if (calls) parts.push(countNoun(calls, FORMS.run));
  if (tokens) parts.push(shortNumber(tokens) + ' токенов');
  const cash = money(cost);
  if (cash) parts.push(cash);
  return parts.join(' · ');
}

/** Строка периода: «Вечер · 21 ноября». */
export function periodTitle(payload, months = ['января', 'февраля', 'марта', 'апреля', 'мая',
  'июня', 'июля', 'августа', 'сентября', 'октября', 'ноября', 'декабря']) {
  const p = payload || {};
  const title = String(p.period_title || 'День');
  const m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(String(p.day || ''));
  if (!m) return title;
  const month = months[Number(m[2]) - 1] || '';
  return title + ' · ' + Number(m[3]) + ' ' + month;
}

/** Заголовок секции с числом: «Сделано · 3». */
export function sectionTitle(key, count) {
  const section = DAILY_SECTIONS.find((s) => s.key === key);
  const base = section ? section.title : '';
  const n = num(count);
  return n ? base + ' · ' + n : base;
}

/** Строки раздела «сделано»: название запроса, комната, ссылка. */
export function doneRows(payload) {
  return list((payload || {}).done).map((row) => ({
    room: String(row.room || ''),
    roomTitle: String(row.room_title || row.room || ''),
    requestId: String(row.request_id || ''),
    title: String(row.request_title || 'Запрос'),
    at: row.at || null,
    href: row.room && row.request_id
      ? '#/room/' + encodeURIComponent(row.room) + '/request/' + encodeURIComponent(row.request_id)
      : '',
  }));
}

/** Строки «решено»: текст решения, статус по-русски, ссылка на запрос. */
export const STATUS_LABELS = {
  accepted: 'принято',
  rejected: 'отклонено',
  dismissed: 'снято',
  proposed: 'предложено',
  superseded: 'заменено',
};

export function decisionRows(payload) {
  return list((payload || {}).decisions).map((row) => ({
    room: String(row.room || ''),
    roomTitle: String(row.room_title || row.room || ''),
    requestId: String(row.request_id || ''),
    title: String(row.title || ''),
    status: String(row.status || ''),
    statusLabel: STATUS_LABELS[String(row.status || '')] || String(row.status || ''),
    at: row.at || null,
    href: row.room && row.request_id
      ? '#/room/' + encodeURIComponent(row.room) + '/request/' + encodeURIComponent(row.request_id)
      : '',
  }));
}

/**
 * Строки «ждёт вас»: у каждой есть действие (правило «без тупиков»).
 * Строка без ссылки и без действия на экран не попадает — её нечем закрыть.
 */
export function waitingRows(payload) {
  return list((payload || {}).waiting).map((row) => {
    const room = String(row.room || '');
    const requestId = String(row.request_id || '');
    return {
      room,
      roomTitle: String(row.room_title || row.room || ''),
      requestId,
      kind: String(row.kind || ''),
      kindLabel: KIND_LABELS[String(row.kind || '')] || '',
      text: String(row.text || ''),
      recommended: String(row.recommended || ''),
      since: row.since || null,
      href: room && requestId
        ? '#/room/' + encodeURIComponent(room) + '/request/' + encodeURIComponent(requestId)
        : (room ? '#/room/' + encodeURIComponent(room) + '/backlog' : ''),
      action: 'open',
    };
  });
}

/** Строки «остановлено»: причина и ссылка; архив — отдельным видом. */
export function stoppedRows(payload) {
  return list((payload || {}).stopped).map((row) => ({
    room: String(row.room || ''),
    roomTitle: String(row.room_title || row.room || ''),
    requestId: String(row.request_id || ''),
    title: String(row.request_title || 'Запрос'),
    reason: String(row.reason || ''),
    kind: String(row.kind || 'failed'),
    at: row.at || null,
    href: row.room && row.request_id
      ? '#/room/' + encodeURIComponent(row.room) + '/request/' + encodeURIComponent(row.request_id)
      : '',
  }));
}

/** Строки «файлы»: имя и комната (путей с диска на экране нет). */
export function fileRows(payload) {
  return list((payload || {}).files).map((row) => ({
    room: String(row.room || ''),
    roomTitle: String(row.room_title || row.room || ''),
    name: String(row.name || ''),
    at: row.at || null,
  }));
}

/** Счётчики шапки: только непустые, чтобы не было «0 · 0 · 0». */
export function counterChips(payload) {
  const c = (payload || {}).counters || {};
  const chips = [];
  const done = num(c.done);
  const decisions = num(c.decisions);
  const waiting = num(c.waiting);
  const stopped = num(c.stopped);
  if (done) chips.push({ key: 'done', text: 'сделано: ' + done, tone: 'ok' });
  if (decisions) chips.push({ key: 'decisions', text: 'решено: ' + decisions, tone: 'info' });
  if (waiting) chips.push({ key: 'waiting', text: 'ждёт вас: ' + waiting, tone: 'warn' });
  if (stopped) chips.push({ key: 'stopped', text: 'остановлено: ' + stopped, tone: 'danger' });
  return chips;
}

/** Надпись на кнопке «Короче»: что будет стоить нажатие. */
export function shortButtonText(estimate) {
  const est = estimate || {};
  const tokens = num(est.input_tokens) + num(est.output_tokens);
  const cash = money(est.cost_usd);
  if (cash) return 'Короче · ≈ ' + shortNumber(tokens) + ' токенов, ' + cash;
  if (tokens) return 'Короче · ≈ ' + shortNumber(tokens) + ' токенов';
  return 'Короче';
}

/** Что сказать после сжатия: есть текст — показываем; ошибка — причина и что делать. */
export function shortState(result) {
  const r = result || {};
  if (!r.ok) {
    return {
      ok: false,
      text: '',
      message: String(r.error || '«Короче» не получился'),
      hint: 'Итог дня выше собран по данным и без модели — он остаётся.',
    };
  }
  return {
    ok: true,
    text: String(r.text || ''),
    message: r.fake ? 'Сжатие песочницы: настоящая модель не вызывалась.' : '',
    hint: '',
  };
}

/** Есть ли что показывать (иначе — пустое состояние с объяснением). */
export function isEmptyDaily(payload) {
  const p = payload || {};
  if (!p.day) return true;
  const c = p.counters || {};
  return !num(c.all);
}

/** Подпись пустого состояния: зачем экран и что сделать сейчас. */
export function emptyText(payload) {
  const p = payload || {};
  if (isEmptyDaily(p)) {
    return {
      title: 'За сутки в комнатах ничего не произошло',
      text: 'Итог собирается сам: сюда попадут завершённые запросы, решения, файлы, '
        + 'то, что ждёт вас, и остановленное. Как только что-то случится — увидите здесь.',
    };
  }
  return { title: '', text: '' };
}

export default {
  DAILY_SECTIONS, KIND_LABELS, FORMS, STATUS_LABELS,
  shortNumber, money, spentText, periodTitle, sectionTitle, doneRows, decisionRows,
  waitingRows, stoppedRows, fileRows, counterChips, shortButtonText, shortState,
  isEmptyDaily, emptyText,
};
