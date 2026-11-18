/**
 * delo-model.js — чистые функции экрана «Дела» (W26-D6).
 *
 * Здесь нет DOM: всё, что можно проверить без браузера — подписи, тон, порядок,
 * группировка «Найдено», кнопки действий. Вьюха (`delo.js`) только собирает узлы.
 * Форма дела — контракт `docs/WAVE26-RULES.md` §1; сервер (D5) отдаёт те же поля,
 * фикстура `fixtures/delo.json` — эталон формы.
 */

/** Подписи статусов дела (не капсом: это состояние, а не команда). */
export const STATUS_LABEL = {
  open: 'открыто',
  waiting: 'ждёт ответа',
  paused: 'на паузе',
  done: 'закрыто',
  cancelled: 'отменено',
};

/** Тон статуса для чипа. */
export const STATUS_TONE = {
  open: 'info',
  waiting: 'warn',
  paused: 'muted',
  done: 'ok',
  cancelled: 'muted',
};

/** Подписи состояний частей результата. */
export const PART_LABEL = { agreed: 'согласовано', discussing: 'обсуждаем', todo: 'не начато' };
export const PART_MARK = { agreed: '✓', discussing: '●', todo: '○' };
export const PART_TONE = { agreed: 'ok', discussing: 'warn', todo: 'muted' };

/** Роль файла результата. */
export const FILE_ROLE_LABEL = { final: 'итоговый', source: 'исходный', draft: 'черновик' };

/** Вид пункта «Ждёт вашего решения». */
export const NEED_KIND_LABEL = { approve: 'Утвердить', question: 'Вопрос', permission: 'Разрешение' };

/** Подписи исходящих. */
export const OUTBOX_STATE_LABEL = {
  draft: 'черновик · не отправлено',
  approved: 'одобрено · ждёт отправки',
  sent: 'отправлено',
  failed: 'не ушло',
  cancelled: 'отменено',
};

export const OUTBOX_STATE_TONE = {
  draft: 'muted',
  approved: 'info',
  sent: 'ok',
  failed: 'danger',
  cancelled: 'muted',
};

export const OUTBOX_KIND_LABEL = { email: 'Письмо', file: 'Файл', message: 'Сообщение' };

/** Состояния этапа → знак и тон. */
export const STAGE_MARK = { done: '✓', now: '●', todo: '○' };

/** Сколько дел ждёт решения человека (для бейджа меню и вкладки). */
export function needsTotal(items) {
  const list = Array.isArray(items) ? items : [];
  return list.reduce((sum, delo) => sum + (Array.isArray(delo && delo.needs_you) ? delo.needs_you.length : 0), 0);
}

/** Дела, у которых есть «ждёт вашего решения». */
export function waitingDelos(items) {
  return (Array.isArray(items) ? items : []).filter((d) => Array.isArray(d && d.needs_you) && d.needs_you.length);
}

/**
 * Порядок списка: сначала ждущие решения, затем по сроку (ближние выше, без
 * срока — в конце), затем по времени обновления. Решение — за человеком,
 * поэтому «ждёт вас» никогда не тонет в списке.
 */
export function sortDelos(items) {
  const list = (Array.isArray(items) ? items : []).slice();
  const dueKey = (d) => {
    const raw = String((d && d.due) || '');
    const m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(raw);
    return m ? Number(m[1] + m[2] + m[3]) : Number.MAX_SAFE_INTEGER;
  };
  return list.sort((a, b) => {
    const wa = Array.isArray(a && a.needs_you) && a.needs_you.length ? 0 : 1;
    const wb = Array.isArray(b && b.needs_you) && b.needs_you.length ? 0 : 1;
    if (wa !== wb) return wa - wb;
    const da = dueKey(a);
    const db = dueKey(b);
    if (da !== db) return da - db;
    return (Number(b && b.updated_at) || 0) - (Number(a && a.updated_at) || 0);
  });
}

/** Счётчики шапки списка. */
export function listCounters(items) {
  const list = Array.isArray(items) ? items : [];
  return {
    total: list.length,
    waiting: waitingDelos(list).length,
    needs: needsTotal(list),
    overdue: list.filter((d) => dueInfo(d && d.due, Date.now()).overdue).length,
  };
}

const WEEKDAYS = ['вс', 'пн', 'вт', 'ср', 'чт', 'пт', 'сб'];

/** Разобрать ISO-дату срока в локальную полночь (без часовых сюрпризов). */
export function parseDue(due) {
  const m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(String(due || ''));
  if (!m) return null;
  const y = Number(m[1]);
  const mo = Number(m[2]);
  const d = Number(m[3]);
  if (!y || !mo || !d) return null;
  const date = new Date(y, mo - 1, d);
  if (Number.isNaN(date.getTime())) return null;
  return date;
}

/**
 * Срок словами: «срок: пт 21.11», «просрочено на 2 дня», «без срока».
 * @param {string|null} due ISO-дата
 * @param {number} [nowMs]
 * @returns {{text:string, tone:string, overdue:boolean, soon:boolean, days:number|null}}
 */
export function dueInfo(due, nowMs) {
  const date = parseDue(due);
  if (!date) return { text: 'без срока', tone: 'muted', overdue: false, soon: false, days: null };
  const now = new Date(Number.isFinite(Number(nowMs)) ? Number(nowMs) : Date.now());
  const today = new Date(now.getFullYear(), now.getMonth(), now.getDate());
  const days = Math.round((date.getTime() - today.getTime()) / 86400000);
  const dd = String(date.getDate()).padStart(2, '0');
  const mm = String(date.getMonth() + 1).padStart(2, '0');
  const stamp = WEEKDAYS[date.getDay()] + ' ' + dd + '.' + mm;
  if (days < 0) {
    return { text: 'просрочено на ' + dayNoun(-days), tone: 'danger', overdue: true, soon: false, days };
  }
  if (days === 0) return { text: 'срок: сегодня', tone: 'danger', overdue: false, soon: true, days };
  if (days === 1) return { text: 'срок: завтра', tone: 'warn', overdue: false, soon: true, days };
  return { text: 'срок: ' + stamp, tone: days <= 3 ? 'warn' : 'muted', overdue: false, soon: days <= 3, days };
}

/** «1 день», «2 дня», «5 дней». */
export function dayNoun(n) {
  const v = Math.abs(Number(n) || 0);
  const mod10 = v % 10;
  const mod100 = v % 100;
  if (mod10 === 1 && mod100 !== 11) return v + ' день';
  if (mod10 >= 2 && mod10 <= 4 && (mod100 < 12 || mod100 > 14)) return v + ' дня';
  return v + ' дней';
}

/** Русские формы: pluralRu(2, ['решение','решения','решений']) → 'решения'. */
export function pluralRu(n, forms) {
  const v = Math.abs(Number(n) || 0);
  const list = Array.isArray(forms) ? forms : ['', '', ''];
  const mod10 = v % 10;
  const mod100 = v % 100;
  if (mod10 === 1 && mod100 !== 11) return list[0];
  if (mod10 >= 2 && mod10 <= 4 && (mod100 < 12 || mod100 > 14)) return list[1];
  return list[2];
}

/** «12 минут назад», «вчера», «3 дня назад» — для меты и хода дела. */
export function agoText(atSec, nowSec) {
  const at = Number(atSec);
  if (!Number.isFinite(at) || at <= 0) return '';
  const now = Number.isFinite(Number(nowSec)) ? Number(nowSec) : Math.floor(Date.now() / 1000);
  const diff = now - at;
  if (diff < 0) return 'только что';
  if (diff < 60) return 'только что';
  if (diff < 3600) return Math.floor(diff / 60) + ' мин назад';
  if (diff < 86400) {
    const h = Math.floor(diff / 3600);
    return h + ' ' + (h === 1 ? 'час' : h < 5 ? 'часа' : 'часов') + ' назад';
  }
  const d = Math.floor(diff / 86400);
  if (d === 1) return 'вчера';
  return d + ' ' + dayNoun(d) + ' назад';
}

/** Время в «ЧЧ:ММ» по локальным часам. */
export function clockText(atSec) {
  const at = Number(atSec);
  if (!Number.isFinite(at) || at <= 0) return '';
  const d = new Date(at * 1000);
  return String(d.getHours()).padStart(2, '0') + ':' + String(d.getMinutes()).padStart(2, '0');
}

/** Дата разговора для «Хода дела»: «сегодня 10:42», «вчера 18:10», «16.11 09:00». */
export function stampText(atSec, nowSec) {
  const at = Number(atSec);
  if (!Number.isFinite(at) || at <= 0) return '';
  const now = Number.isFinite(Number(nowSec)) ? Number(nowSec) : Math.floor(Date.now() / 1000);
  const day = 86400;
  const startOfToday = Math.floor(now / day) * day;
  const clock = clockText(at);
  if (at >= startOfToday) return 'сегодня ' + clock;
  if (at >= startOfToday - day) return 'вчера ' + clock;
  const d = new Date(at * 1000);
  return String(d.getDate()).padStart(2, '0') + '.' + String(d.getMonth() + 1).padStart(2, '0') + ' ' + clock;
}

/**
 * Цепочка этапов для чипов.
 * @returns {{id:string,title:string,state:'done'|'now'|'todo',mark:string}[]}
 */
export function stageChips(stages) {
  return (Array.isArray(stages) ? stages : [])
    .filter((s) => s && s.title)
    .map((s, i) => {
      const state = ['done', 'now', 'todo'].includes(String(s.state)) ? String(s.state) : 'todo';
      return { id: String(s.id || 'sg_' + i), title: String(s.title), state, mark: STAGE_MARK[state] };
    });
}

/** «2 из 5» — короткая подпись прогресса. */
export function stageProgress(stages) {
  const list = stageChips(stages);
  const done = list.filter((s) => s.state === 'done').length;
  return done + ' из ' + list.length;
}

/** Подпись «Ждём»: «you» — это вы, остальное — как записано. */
export function waitingWho(who) {
  const raw = String(who || '').trim();
  if (!raw) return 'кто-то';
  return raw === 'you' ? 'вас' : raw;
}

/** Строки «Ждём» с давностью. */
export function waitingRows(waitingOn, nowSec) {
  return (Array.isArray(waitingOn) ? waitingOn : []).map((w, i) => ({
    id: 'wait_' + i,
    who: waitingWho(w && w.who),
    what: String((w && w.what) || '').trim(),
    since: agoText(w && w.since, nowSec),
    isYou: String((w && w.who) || '') === 'you',
  }));
}

/** Кнопки пункта «ждёт вашего решения» — у каждого есть хотя бы одно действие. */
export function needButtons(need) {
  const actions = Array.isArray(need && need.actions) ? need.actions.map(String) : [];
  const out = [];
  const push = (b) => { if (b && !out.some((x) => x.action === b.action && x.label === b.label)) out.push(b); };
  for (const action of actions) {
    if (action === 'approve' || action === 'permission') {
      push({ action: 'approve', label: need && need.kind === 'permission' ? 'Разрешить' : 'Утвердить', kind: 'primary' });
    } else if (action === 'reject') {
      push({ action: 'reject', label: need && need.kind === 'question' ? 'Не так — уточню' : 'Вернуть на доработку…', kind: 'secondary' });
    } else if (action === 'answer') {
      const options = Array.isArray(need && need.options) ? need.options : [];
      for (const opt of options) {
        push({ action: 'answer', label: String((opt && opt.label) || ''), kind: 'secondary', optionId: String((opt && opt.id) || '') });
      }
      push({ action: 'answer', label: options.length ? 'Свой вариант…' : 'Ответить', kind: options.length ? 'ghost' : 'secondary', own: true });
    } else if (action === 'done') {
      push({ action: 'done', label: 'Закрыть дело', kind: 'secondary' });
    } else {
      push({ action, label: actionLabel(action), kind: 'secondary' });
    }
  }
  // «Без тупиков»: пункт без действий не показываем пустым — даём осмысленный выход.
  if (!out.length) push({ action: 'approve', label: 'Утвердить', kind: 'primary' });
  return out;
}

/** Подпись действия по id (для кнопок исходящих и меню дела). */
export function actionLabel(action) {
  const map = {
    approve: 'Утвердить',
    reject: 'Отклонить',
    answer: 'Ответить',
    set_stage: 'Сменить этап',
    add_part: 'Добавить часть',
    set_part: 'Отметить часть',
    approve_outbox: 'Отправить после утверждения',
    edit_outbox: 'Править текст',
    cancel_outbox: 'Отложить',
    mark_sent: 'Отметить отправленным',
    pause: 'Пауза',
    resume: 'Возобновить',
    done: 'Закрыть дело',
    add_waiting: 'Добавить ожидание',
    clear_waiting: 'Снять ожидание',
  };
  return map[String(action)] || String(action || 'Действие');
}

/** Кнопки исходящего по его состоянию. Наружу — только по «да» человека. */
export function outboxButtons(outbox) {
  const state = String((outbox && outbox.state) || 'draft');
  const id = String((outbox && outbox.id) || '');
  if (state === 'draft') {
    return [
      { action: 'approve_outbox', label: 'Отправить после утверждения', kind: 'primary', outboxId: id },
      { action: 'edit_outbox', label: 'Править текст', kind: 'secondary', outboxId: id },
      { action: 'cancel_outbox', label: 'Отложить', kind: 'ghost', outboxId: id },
    ];
  }
  if (state === 'approved') {
    return [
      { action: 'mark_sent', label: 'Отметить отправленным', kind: 'primary', outboxId: id },
      { action: 'edit_outbox', label: 'Править текст', kind: 'secondary', outboxId: id },
      { action: 'cancel_outbox', label: 'Отменить отправку', kind: 'ghost', outboxId: id },
    ];
  }
  if (state === 'failed') {
    return [
      { action: 'approve_outbox', label: 'Повторить отправку', kind: 'primary', outboxId: id },
      { action: 'cancel_outbox', label: 'Отменить', kind: 'ghost', outboxId: id },
    ];
  }
  return [];
}

/** Короткая строка исходящего: «Кому: … · Вложение: …». */
export function outboxMeta(outbox) {
  const o = outbox || {};
  const parts = [];
  if (o.to) parts.push('Кому: ' + String(o.to));
  const att = Array.isArray(o.attachments) ? o.attachments : [];
  if (att.length) parts.push('Вложение: ' + att.map((p) => String(p).split('/').pop()).join(', '));
  if (o.state === 'sent' && o.sent_at) parts.push('отправлено ' + stampText(o.sent_at));
  if (o.state === 'failed' && o.error) parts.push('причина: ' + String(o.error));
  return parts.join(' · ');
}

/** Ключ группы «Найдено» по отношению. */
export const FOUND_GROUPS = [
  { key: 'sought', title: 'Нашли то, что искали', tone: 'ok' },
  { key: 'mentions', title: 'Нашли упоминания', tone: 'info' },
  { key: 'related', title: 'Смежное — может пригодиться', tone: 'muted' },
  { key: 'notit', title: 'Не то', tone: 'muted' },
];

const RELATION_TO_GROUP = {
  'искомое': 'sought',
  'упоминает': 'mentions',
  'смежное': 'related',
  'не то': 'notit',
};

/** Отношение → ключ группы (терпимо к регистру и пустому значению). */
export function relationKey(item) {
  const raw = String((item && (item['отношение'] || item.relation)) || '').trim().toLowerCase();
  return RELATION_TO_GROUP[raw] || 'related';
}

/** Название пункта «Найдено» (русские ключи D4 или поле title). */
export function foundTitle(item) {
  return String((item && (item['название'] || item.title || item['где'] || item.where)) || 'Без названия');
}

/** Тип пункта «Найдено»: файл | сайт | письмо | запись. */
export function foundType(item) {
  const raw = String((item && (item['тип'] || item.type)) || 'файл').trim().toLowerCase();
  return ['файл', 'сайт', 'письмо', 'запись'].includes(raw) ? raw : 'файл';
}

/**
 * Группы «Найдено». Пустые группы не отдаём: «не нашли» — это отдельная честная
 * плашка, а не пустой заголовок.
 * @returns {{key:string,title:string,tone:string,items:object[]}[]}
 */
export function foundGroups(found) {
  const list = Array.isArray(found) ? found.filter(Boolean) : [];
  const buckets = { sought: [], mentions: [], related: [], notit: [] };
  for (const item of list) buckets[relationKey(item)].push(item);
  return FOUND_GROUPS
    .map((g) => ({ key: g.key, title: g.title, tone: g.tone, items: buckets[g.key] }))
    .filter((g) => g.items.length);
}

/** Честная плашка «искомого нет, нашли смежное». */
export function foundVerdict(found) {
  const list = Array.isArray(found) ? found.filter(Boolean) : [];
  if (!list.length) return '';
  const sought = list.filter((i) => relationKey(i) === 'sought').length;
  if (sought) return '';
  const other = list.length;
  return 'Искомого нет; нашли смежное: ' + other + ' ' + (other === 1 ? 'запись' : other < 5 ? 'записи' : 'записей') + '.';
}

/** Части результата с подписями. */
export function partRows(parts) {
  return (Array.isArray(parts) ? parts : []).filter((p) => p && p.title).map((p, i) => {
    const state = ['agreed', 'discussing', 'todo'].includes(String(p.state)) ? String(p.state) : 'todo';
    return {
      id: String(p.id || 'pt_' + i),
      title: String(p.title),
      state,
      label: PART_LABEL[state],
      mark: PART_MARK[state],
      tone: PART_TONE[state],
    };
  });
}

/** Файлы результата с подписью роли. */
export function fileRows(files) {
  return (Array.isArray(files) ? files : []).filter((f) => f && (f.path || f.title)).map((f, i) => {
    const role = ['final', 'source', 'draft'].includes(String(f.role)) ? String(f.role) : 'source';
    return {
      id: 'file_' + i,
      path: String(f.path || ''),
      title: String(f.title || String(f.path || '').split('/').pop()),
      role,
      roleLabel: FILE_ROLE_LABEL[role],
      note: String(f.note || ''),
    };
  });
}

/** Дежурства дела: подпись состояния. */
export function dutyState(duty) {
  const state = String((duty && duty.state) || 'active');
  if (state === 'paused' || state === 'off') return { label: 'на паузе', tone: 'muted' };
  if (state === 'failed' || state === 'error') return { label: 'сбой', tone: 'danger' };
  return { label: 'работает', tone: 'ok' };
}

/**
 * Одна строка «что дальше» для списка и шапки карточки. Без тупиков: если
 * решение не нужно, честно говорим, чего ждём.
 * @returns {{text:string, tone:string}}
 */
export function nextStepText(delo) {
  const d = delo || {};
  const needs = Array.isArray(d.needs_you) ? d.needs_you : [];
  if (needs.length) {
    const first = needs[0];
    const kind = NEED_KIND_LABEL[String(first.kind)] || 'Решение';
    return { text: kind + ': ' + String(first.text || '').trim(), tone: 'warn' };
  }
  const stages = stageChips(d.stages);
  const now = stages.find((s) => s.state === 'now');
  if (String(d.status) === 'paused') return { text: 'Дело на паузе — можно возобновить', tone: 'muted' };
  if (now) return { text: 'Идёт этап «' + now.title + '» — от вас ничего не нужно', tone: 'info' };
  const wait = Array.isArray(d.waiting_on) ? d.waiting_on[0] : null;
  if (wait) return { text: 'Ждём ' + waitingWho(wait.who) + ': ' + String(wait.what || ''), tone: 'muted' };
  return { text: 'Всё сделано', tone: 'ok' };
}

/** Мета строки списка: «комната · ведёт: … · обновлено …». */
export function deloMeta(delo, nowSec) {
  const d = delo || {};
  const parts = [];
  if (d.owner) parts.push('ведёт: ' + String(d.owner));
  if (Array.isArray(d.members) && d.members.length) parts.push('исполнители: ' + d.members.join(', '));
  const ago = agoText(d.updated_at, nowSec);
  if (ago) parts.push('обновлено ' + ago);
  return parts.join(' · ');
}

/**
 * Нормализация дела из ответа сервера: недостающие блоки — пустыми массивами,
 * чтобы вьюха не падала на старом сервере (D5 может отдать не всё).
 */
export function normalizeDelo(raw) {
  const d = (raw && typeof raw === 'object') ? raw : {};
  const arr = (v) => (Array.isArray(v) ? v : []);
  return {
    id: String(d.id || ''),
    room: String(d.room || ''),
    title: String(d.title || 'Без названия'),
    status: ['open', 'waiting', 'paused', 'done', 'cancelled'].includes(String(d.status)) ? String(d.status) : 'open',
    due: d.due ? String(d.due) : null,
    owner: String(d.owner || ''),
    members: arr(d.members).map(String),
    updated_at: Number(d.updated_at) || 0,
    request_id: d.request_id ? String(d.request_id) : null,
    stages: arr(d.stages),
    waiting_on: arr(d.waiting_on),
    needs_you: arr(d.needs_you),
    result: {
      parts: arr(d.result && d.result.parts),
      files: arr(d.result && d.result.files),
    },
    outbox: arr(d.outbox),
    found: arr(d.found),
    timeline: arr(d.timeline),
    duties: arr(d.duties),
  };
}

/**
 * Значок «Дела» в меню: сколько дел ждёт решения. Ошибку молчим — значок
 * украшение, экран всё равно скажет правду.
 * @param {{get:(url:string)=>Promise<any>}} apiRef
 * @param {string[]} roomIds
 * @returns {Promise<number|null>}
 */
export async function fetchNeedsBadge(apiRef, roomIds) {
  const ids = (Array.isArray(roomIds) ? roomIds : []).filter(Boolean);
  if (!apiRef || !ids.length) return null;
  // Сервер D5 умеет один общий список (GET /api/delos?room=all) — им и пользуемся,
  // чтобы значок не стоил N запросов. Нет такого маршрута (старый сервер) — идём по комнатам.
  try {
    const all = await apiRef.get('/api/delos?room=all');
    const list = Array.isArray(all) ? all : (all && Array.isArray(all.items) ? all.items : null);
    if (list) return needsTotal(list);
  } catch { /* маршрута нет — ниже запасной путь */ }
  try {
    const payloads = await Promise.all(ids.map((id) =>
      apiRef.get('/api/rooms/' + encodeURIComponent(id) + '/delos').catch(() => null)));
    const items = [];
    for (const payload of payloads) {
      const list = Array.isArray(payload) ? payload : (payload && Array.isArray(payload.items) ? payload.items : []);
      items.push(...list);
    }
    return needsTotal(items);
  } catch {
    return null;
  }
}

export default {
  STATUS_LABEL, STATUS_TONE, PART_LABEL, FILE_ROLE_LABEL, NEED_KIND_LABEL, OUTBOX_STATE_LABEL,
  needsTotal, sortDelos, listCounters, dueInfo, stageChips, stageProgress, waitingRows, needButtons,
  outboxButtons, foundGroups, foundVerdict, nextStepText, normalizeDelo, fetchNeedsBadge,
};
