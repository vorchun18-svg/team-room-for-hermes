/**
 * duty-model.js — модель «дежурств комнаты» для W27-E8 (без DOM).
 *
 * Всё, что можно проверить тестом, живёт здесь: четыре шага мастера, правила
 * «что считать изменением», действия «что делать», уровни, шаблоны, русское
 * расписание, предпросмотр точной команды, корзина, история, предложения
 * руководителя и «Сделать регулярным» из Дела/запроса.
 *
 * Контракт сервера — `docs/WAVE27-RULES.md` §3. Ничего не выдумываем:
 *  • расписание — Hermes (`hermes cron create|edit|…`), у нас только черновик;
 *  • создание — ТОЛЬКО `preview:false, confirm:true`;
 *  • `GET /api/duty-templates`, `POST /api/rooms/{id}/duties`,
 *    `PATCH /api/duties/{id}`, `DELETE`, `/restore`, `/history`, `/dry_run`.
 */

import { plural } from '../util.js';

/** Четыре шага мастера: что → когда → что считать изменением → что делать. */
export const WIZARD_STEPS = [
  { id: 'what', n: 1, title: 'Что проверять', short: 'Что',
    hint: 'Название и задание: что именно смотреть и где.' },
  { id: 'when', n: 2, title: 'Когда', short: 'Когда',
    hint: 'Расписание. Его исполняет Hermes — мы только передаём.' },
  { id: 'change', n: 3, title: 'Что считать изменением', short: 'Изменение',
    hint: 'Иначе дежурство будет писать «всё как было» каждый раз.' },
  { id: 'do', n: 4, title: 'Что делать', short: 'Что делать',
    hint: 'Куда сообщить и делает ли оно это само.' },
];

/** Тип дежурства: чем именно проверяем. */
export const KINDS = [
  { id: 'model', title: 'Модель по заданию',
    hint: 'Агент читает и сравнивает сам. Гибко, но тратит модель — по умолчанию дешёвую.' },
  { id: 'script', title: 'Скрипт без модели',
    hint: 'Дешёвая проверка кодом: файлы, папки, строки. Модель не нужна.' },
  { id: 'watch', title: 'Слежение за страницей, папкой или ящиком',
    hint: 'Hermes сам помнит прошлое состояние и говорит, что изменилось.' },
];

/** Готовые расписания: человек выбирает словами, точное значение — рядом. */
export const SCHEDULE_PRESETS = [
  { id: 'every-30m', value: '30m', label: 'каждые 30 минут' },
  { id: 'hourly', value: '0 * * * *', label: 'каждый час' },
  { id: 'every-2h', value: 'every 2h', label: 'каждые 2 часа' },
  { id: 'weekdays-9', value: '0 9 * * 1-5', label: 'по будням в 09:00' },
  { id: 'daily-9', value: '0 9 * * *', label: 'каждый день в 09:00' },
  { id: 'mon-10', value: '0 10 * * 1', label: 'по понедельникам в 10:00' },
  { id: 'custom', value: '', label: 'своё расписание' },
];

/** Правила «что считать изменением». */
export const CHANGE_RULES = [
  { id: 'new_items', label: 'Появилось новое',
    hint: 'Новые файлы, письма, строки — то, чего не было в прошлый раз.' },
  { id: 'count_over', label: 'Стало больше порога',
    hint: 'Например: просроченных пунктов больше 3. Нужен порог.' },
  { id: 'text_differs', label: 'Текст отличается',
    hint: 'Страница или файл изменились по содержанию, а не по размеру.' },
  { id: 'model_compares', label: 'Модель сравнивает с прошлым разом',
    hint: 'Модель смотрит итог прошлого запуска и говорит, что поменялось.' },
];

/** Действие при изменении (по контракту §3 `on_change`). */
export const ON_CHANGE = [
  { id: 'log', label: 'Записать в историю', hint: 'Тихая строка в журнале дежурства. Ничего не ждёт вас.' },
  { id: 'inbox', label: 'Во «Входящие» комнаты', hint: 'Карточка «что выполнено / что обнаружено / предлагаю».' },
  { id: 'delo', label: 'Предложить Дело', hint: 'Создаётся по вашему «да», с пояснением и ссылкой на запуск.' },
  { id: 'step', label: 'Предложить шаг в Деле', hint: 'Шаг появится только после вашего согласия.' },
  { id: 'question', label: 'Спросить меня', hint: 'Вопрос в «От вас нужно» — с вариантами ответа.' },
];

/** Уровень: делает ли дежурство что-то само. */
export const LEVELS = [
  { id: 'self', label: 'Сделать сам',
    hint: 'Только обратимые записи внутри комнаты. Наружу — никогда без человека.' },
  { id: 'propose', label: 'Предложить',
    hint: 'Покажет карточку, действие — по вашему «да». Рекомендуем по умолчанию.' },
  { id: 'ask', label: 'Спросить',
    hint: 'Ничего не делает, пока вы не ответите на вопрос.' },
];

/** Куда сообщить (`notify`). */
export const NOTIFY = [
  { id: 'today', label: 'На «Сегодня»' },
  { id: 'badge', label: 'Значок комнаты' },
  { id: 'windows', label: 'Уведомление Windows' },
  { id: 'messenger', label: 'Внешний мессенджер' },
];

/** Откуда дежурство взялось (`source`). */
export const SOURCE_LABELS = {
  manual: 'создано вручную',
  manager: 'предложил руководитель',
  dictation: 'из диктовки',
  delo: 'из дела',
  template: 'по шаблону',
};

const MONTHS = ['января', 'февраля', 'марта', 'апреля', 'мая', 'июня', 'июля',
  'августа', 'сентября', 'октября', 'ноября', 'декабря'];
const DOW_SHORT = ['вс', 'пн', 'вт', 'ср', 'чт', 'пт', 'сб'];
const DOW_GEN = ['воскресеньям', 'понедельникам', 'вторникам', 'средам', 'четвергам',
  'пятницам', 'субботам'];

function num(n) {
  const value = Number(n);
  return Number.isFinite(value) ? value : 0;
}

function pad2(value) {
  return String(num(value)).padStart(2, '0');
}

/** «2 минуты», «5 минут» — для подписей. */
export function countText(count, forms) {
  const value = num(count);
  return value + ' ' + plural(value, forms);
}

/**
 * Русское расписание по точному значению Hermes.
 * Понимает интервалы (`30m`, `every 2h`) и типовые cron-выражения; незнакомое
 * возвращает как есть — лучше показать точное значение, чем соврать.
 */
export function scheduleHuman(spec) {
  const raw = String(spec == null ? '' : spec).trim();
  if (!raw) return '';
  const preset = SCHEDULE_PRESETS.find((one) => one.value && one.value === raw);
  if (preset) return preset.label;

  const interval = /^(?:every\s+)?(\d+)\s*(m|min|minutes?|h|hours?|d|days?)$/i.exec(raw);
  if (interval) {
    const value = Number(interval[1]);
    const unit = interval[2].toLowerCase();
    if (unit[0] === 'm') return value === 1 ? 'каждую минуту' : 'каждые ' + countText(value, ['минуту', 'минуты', 'минут']);
    if (unit[0] === 'h') return value === 1 ? 'каждый час' : 'каждые ' + countText(value, ['час', 'часа', 'часов']);
    return value === 1 ? 'каждый день' : 'каждые ' + countText(value, ['день', 'дня', 'дней']);
  }

  const parts = raw.split(/\s+/);
  if (parts.length !== 5) return raw;
  const [minute, hour, dom, month, dow] = parts;
  let time = '';
  if (/^\d{1,2}$/.test(hour) && /^\d{1,2}$/.test(minute)) {
    time = 'в ' + pad2(hour) + ':' + pad2(minute);
  } else if (hour === '*' && /^\d{1,2}$/.test(minute)) {
    return minute === '0' ? 'каждый час' : 'каждый час в :' + pad2(minute);
  } else if (/^\*\/(\d+)$/.test(minute) && hour === '*') {
    return 'каждые ' + countText(Number(minute.slice(2)), ['минуту', 'минуты', 'минут']);
  } else if (/^\d{1,2}$/.test(minute) && /^\*\/(\d+)$/.test(hour)) {
    const step = Number(hour.slice(2));
    return 'каждые ' + countText(step, ['час', 'часа', 'часов'])
      + (minute === '0' ? '' : ' в :' + pad2(minute));
  }

  // Дни недели: 1-5, 1,3,5, отдельные числа.
  if (dow !== '*' && dow !== '?') {
    if (dow === '1-5') return 'по будням' + (time ? ' ' + time : '');
    if (dow === '0,6' || dow === '6,0') return 'по выходным' + (time ? ' ' + time : '');
    const days = dow.split(',').map((part) => {
      const range = /^(\d)-(\d)$/.exec(part);
      if (range) {
        const out = [];
        for (let i = Number(range[1]); i <= Number(range[2]); i += 1) out.push(i);
        return out;
      }
      return [Number(part)];
    }).flat().filter((n) => Number.isFinite(n) && n >= 0 && n <= 6);
    if (days.length === 1) return 'по ' + DOW_GEN[days[0]] + (time ? ' ' + time : '');
    if (days.length) {
      return 'по ' + days.map((n) => DOW_SHORT[n]).join(', ') + (time ? ' ' + time : '');
    }
  }

  // День месяца.
  if (dom !== '*') {
    const day = /^\d{1,2}$/.test(dom) ? dom : '';
    if (day) return day + '-го числа каждого месяца' + (time ? ' ' + time : '');
  }
  if (month !== '*') {
    const m = /^\d{1,2}$/.test(month) ? MONTHS[Math.min(11, Math.max(0, Number(month) - 1))] : '';
    if (m) return 'в ' + m + (dom !== '*' ? ' ' + dom + '-го' : '') + (time ? ' ' + time : '');
  }
  if (time) return 'каждый день ' + time;
  return raw;
}

/** Подпись правила изменения. */
export function changeRuleLabel(ruleId) {
  const found = CHANGE_RULES.find((one) => one.id === ruleId);
  return found ? found.label : String(ruleId || '');
}

/** Человеческая строка «что считать изменением» с порогом. */
export function changeText(change) {
  const doc = change && typeof change === 'object' ? change : {};
  const rule = String(doc.rule || '');
  if (!rule) return '';
  if (rule === 'count_over') {
    const n = num(doc.threshold);
    return n > 0 ? 'стало больше ' + n : 'стало больше порога';
  }
  if (rule === 'new_items') return 'появилось новое';
  if (rule === 'text_differs') return 'текст отличается';
  if (rule === 'model_compares') return 'модель сравнивает с прошлым разом';
  return changeRuleLabel(rule);
}

/** Подпись действия при изменении. */
export function onChangeLabel(actionId) {
  const found = ON_CHANGE.find((one) => one.id === actionId);
  return found ? found.label : String(actionId || 'ничего');
}

/** Подпись уровня автономии. */
export function levelLabel(levelId) {
  const found = LEVELS.find((one) => one.id === levelId);
  return found ? found.label : 'Предложить';
}

/** Пустой черновик мастера. */
export function emptyDraft(opts = {}) {
  const room = String((opts && opts.room) || '');
  return {
    room,
    mode: 'create',
    template: '',
    title: '',
    kind: 'model',
    task: '',
    workdir: room ? 'C:/Work/Rooms/' + room : '',
    skills: [],
    schedule: '0 9 * * 1-5',
    catch_up: true,
    started_paused: false,
    change: { rule: 'new_items', threshold: 0 },
    onChange: 'inbox',
    level: 'propose',
    notify: ['today'],
    source: 'manual',
    jobId: '',
    from: null,
  };
}

/** Привести черновик к полной форме (сервер мог прислать не всё). */
export function normalizeDraft(input) {
  const doc = input && typeof input === 'object' ? input : {};
  const base = emptyDraft({ room: doc.room });
  const change = doc.change && typeof doc.change === 'object' ? doc.change : {};
  return {
    ...base,
    ...doc,
    room: String(doc.room || base.room || ''),
    mode: doc.jobId ? 'edit' : (doc.mode || 'create'),
    title: String(doc.title || ''),
    kind: KINDS.some((one) => one.id === doc.kind) ? doc.kind : base.kind,
    task: String(doc.task || ''),
    workdir: String(doc.workdir || ''),
    skills: Array.isArray(doc.skills) ? doc.skills.map(String) : [],
    schedule: String(doc.schedule || base.schedule),
    catch_up: doc.catch_up === undefined ? base.catch_up : !!doc.catch_up,
    started_paused: !!doc.started_paused,
    change: {
      rule: CHANGE_RULES.some((one) => one.id === change.rule) ? change.rule : base.change.rule,
      threshold: num(change.threshold) || 0,
    },
    onChange: ON_CHANGE.some((one) => one.id === doc.onChange || one.id === doc.on_change)
      ? (doc.onChange || doc.on_change) : base.onChange,
    level: LEVELS.some((one) => one.id === doc.level) ? doc.level : base.level,
    notify: Array.isArray(doc.notify) ? doc.notify.map(String) : base.notify.slice(),
    source: String(doc.source || base.source),
    jobId: String(doc.jobId || doc.job_id || ''),
    template: String(doc.template || ''),
    from: doc.from && typeof doc.from === 'object' ? doc.from : null,
  };
}

/** Чего не хватает на шаге: пустой список — шаг готов. */
export function stepProblems(draft, stepId) {
  const doc = normalizeDraft(draft);
  const out = [];
  if (stepId === 'what') {
    if (!doc.title.trim()) out.push('название — одной строкой, чтобы узнавать дежурство в списке');
    if (doc.title.trim().length > 90) out.push('название короче 90 знаков');
    if (!doc.task.trim()) {
      out.push(doc.kind === 'script' ? 'что запускать: путь к скрипту или команду'
        : doc.kind === 'watch' ? 'за чем следить: адрес страницы, папка или ящик'
          : 'задание: что именно проверить и по каким признакам');
    }
  }
  if (stepId === 'when') {
    if (!doc.schedule.trim()) out.push('расписание: выберите готовое или впишите своё');
    else if (!scheduleHuman(doc.schedule)) out.push('расписание не распознано');
  }
  if (stepId === 'change') {
    if (!doc.change.rule) out.push('правило: что считать изменением');
    if (doc.change.rule === 'count_over' && num(doc.change.threshold) <= 0) {
      out.push('порог: число, после которого это уже изменение');
    }
  }
  if (stepId === 'do') {
    if (!doc.onChange) out.push('что делать при изменении');
    if (!doc.level) out.push('уровень: делает само, предлагает или спрашивает');
  }
  return out;
}

/** Все замечания по черновику: [{step, text}] — по шагам мастера. */
export function draftProblems(draft) {
  const out = [];
  for (const step of WIZARD_STEPS) {
    for (const text of stepProblems(draft, step.id)) out.push({ step: step.id, stepTitle: step.title, text });
  }
  return out;
}

/** Готов ли черновик к предпросмотру. */
export function draftReady(draft) {
  const problems = draftProblems(draft);
  return { ok: problems.length === 0, problems };
}

/** Первый шаг с замечанием (куда вернуть человека). */
export function firstBadStep(draft) {
  for (const step of WIZARD_STEPS) {
    if (stepProblems(draft, step.id).length) return step.id;
  }
  return '';
}

/** Итоговая строка черновика: «по будням в 09:00 · появилось новое → во «Входящие», предложить». */
export function draftSummary(draft) {
  const doc = normalizeDraft(draft);
  const bits = [];
  const when = scheduleHuman(doc.schedule);
  if (when) bits.push(when);
  const change = changeText(doc.change);
  if (change) bits.push(change);
  if (doc.onChange) bits.push(onChangeLabel(doc.onChange).toLowerCase());
  bits.push(levelLabel(doc.level).toLowerCase());
  return bits.join(' · ');
}

/** Точная команда, которую выполнит сервер (локальная копия предпросмотра). */
export function localCommand(draft) {
  const doc = normalizeDraft(draft);
  const quote = (value) => '"' + String(value).replace(/"/g, '\\"') + '"';
  // Правка — это `hermes cron edit <id>`: команда та же, что выполнит сервер.
  const argv = doc.jobId
    ? ['hermes', 'cron', 'edit', String(doc.jobId)]
    : ['hermes', 'cron', 'create', quote(doc.title || 'дежурство')];
  if (doc.schedule) argv.push('--schedule', quote(doc.schedule));
  if (doc.kind === 'script') argv.push('--script', quote(doc.task));
  else argv.push('--prompt', quote(doc.task));
  if (doc.workdir) argv.push('--workdir', quote(doc.workdir));
  if (doc.kind === 'watch') argv.push('--monitor-url', quote(doc.task));
  if (doc.catch_up) argv.push('--catch-up');
  if (doc.started_paused) argv.push('--paused');
  if (doc.room) argv.push('--deliver', quote('room:' + doc.room));
  return argv.join(' ');
}

/**
 * Предпросмотр «что изменится» перед созданием/правкой.
 * @param {object} local  {command, changes, note} из ответа сервера (может быть пусто)
 * @param {object} draft  черновик мастера
 */
export function previewModel(local, draft) {
  const doc = normalizeDraft(draft);
  const src = local && typeof local === 'object' ? local : {};
  const changes = Array.isArray(src.changes) ? src.changes.map(String) : [];
  const command = String(src.command || '').trim() || String(src.text || '').trim() || localCommand(doc);
  const isEdit = doc.mode === 'edit';
  return {
    command,
    changes: changes.length ? changes : localChanges(doc),
    note: String(src.note || '').trim() || (isEdit
      ? 'Это предпросмотр. В Hermes ничего не изменится, пока вы не нажмёте «Сохранить».'
      : 'Это предпросмотр. Задание в Hermes не создано — оно появится только после «Создать».'),
    ok: src.ok !== false,
    error: String(src.error || ''),
  };
}

/** Что изменится — если сервер не прислал список, собираем по черновику. */
export function localChanges(draft) {
  const doc = normalizeDraft(draft);
  const out = [];
  const when = scheduleHuman(doc.schedule) || doc.schedule;
  out.push((doc.mode === 'edit' ? 'Расписание станет: ' : 'Расписание: ') + when);
  if (doc.mode === 'edit') {
    out.push('Задание обновится: ' + (doc.task ? doc.task.slice(0, 60) : 'без изменений'));
  } else {
    out.push('Задание выполнит ' + (doc.kind === 'script' ? 'скрипт (без модели)'
      : doc.kind === 'watch' ? 'слежение Hermes (без модели)' : 'модель комнаты'));
  }
  out.push('Изменением считается: ' + (changeText(doc.change) || 'не задано'));
  out.push('При изменении: ' + onChangeLabel(doc.onChange).toLowerCase() + ', ' + levelLabel(doc.level).toLowerCase());
  if (doc.notify.length) out.push('Сообщить: ' + doc.notify.map(notifyLabel).join(', '));
  return out;
}

/** Подпись канала уведомления. */
export function notifyLabel(id) {
  const found = NOTIFY.find((one) => one.id === id);
  return found ? found.label.toLowerCase() : String(id || '');
}

/** Тело запроса на создание (POST /api/rooms/{id}/duties). */
export function createBody(draft, opts = {}) {
  const doc = normalizeDraft(draft);
  const preview = !!(opts && opts.preview);
  const body = {
    title: doc.title.trim(),
    kind: doc.kind,
    task: doc.task.trim(),
    workdir: doc.workdir.trim(),
    skills: doc.skills,
    schedule: doc.schedule.trim(),
    catch_up: doc.catch_up,
    started_paused: doc.started_paused,
    change: { rule: doc.change.rule, threshold: num(doc.change.threshold) },
    on_change: doc.onChange,
    level: doc.level,
    notify: doc.notify,
    source: doc.source,
    template: doc.template || undefined,
    preview,
  };
  if (!preview) body.confirm = true;
  return body;
}

/** Тело запроса на правку (PATCH /api/duties/{id}). */
export function patchBody(draft) {
  const doc = normalizeDraft(draft);
  return {
    title: doc.title.trim(),
    task: doc.task.trim(),
    schedule: doc.schedule.trim(),
    catch_up: doc.catch_up,
    paused: doc.started_paused,
    change: { rule: doc.change.rule, threshold: num(doc.change.threshold) },
    on_change: doc.onChange,
    level: doc.level,
    notify: doc.notify,
  };
}

/* ───────────────────────────────────────────────────────────── список */

/** Итог последнего запуска одной фразой: что человек увидит в карточке. */
export function outcomeLine(item) {
  const doc = item && typeof item === 'object' ? item : {};
  const outcome = doc.outcome && typeof doc.outcome === 'object' ? doc.outcome : null;
  if (outcome) {
    const changed = String(outcome.changed || '');
    if (changed === 'нет' || changed === 'no') return 'изменений нет';
    if (changed === 'да' || changed === 'yes') {
      const found = String(outcome.found || '').trim();
      const action = String(outcome.action || '').trim();
      let text = found ? 'изменение: ' + found : 'изменение найдено';
      if (action) text += ' → ' + action;
      return text;
    }
    const text = String(outcome.text || '').trim();
    if (text) return text;
  }
  const last = doc.last_run && typeof doc.last_run === 'object' ? doc.last_run : null;
  if (!last) return 'ещё не запускалось';
  const status = String(last.status || '');
  if (status === 'failed') return 'последний запуск завершился ошибкой';
  if (status === 'running') return 'идёт сейчас';
  if (status === 'missed') return 'последний запуск пропущен';
  const excerpt = String(doc.excerpt || '').trim();
  if (excerpt) return excerpt;
  return status === 'completed' ? 'изменений нет' : 'итог неизвестен';
}

/** Когда был последний запуск: «2 часа назад» (готовый текст сервера тоже подходит). */
export function lastRunAgo(item, relFn) {
  const doc = item && typeof item === 'object' ? item : {};
  if (doc.last_run_text) return String(doc.last_run_text);
  const last = doc.last_run && typeof doc.last_run === 'object' ? doc.last_run : null;
  if (!last) return '';
  const stamp = num(last.finished_at || last.started_at || last.scheduled_for);
  if (!stamp) return '';
  return typeof relFn === 'function' ? relFn(stamp) : '';
}

/** Расписание дежурства: готовый текст сервера или наша расшифровка. */
export function dutyWhen(item) {
  const doc = item && typeof item === 'object' ? item : {};
  return String(doc.schedule_human || '').trim() || scheduleHuman(doc.schedule) || 'расписание не указано';
}

/** Строка истории запусков: что нашла, что сделала, что предложила. */
export function historyRow(entry, timeFn) {
  const doc = entry && typeof entry === 'object' ? entry : {};
  const at = num(doc.at || doc.scheduled_for || doc.started_at);
  const changed = String(doc.changed || (doc.outcome && doc.outcome.changed) || '').toLowerCase();
  const isChanged = changed === 'да' || changed === 'yes' || changed === 'true';
  const proposals = Array.isArray(doc.proposals) ? doc.proposals : [];
  const found = String(doc.found || (doc.outcome && doc.outcome.found) || '').trim();
  const done = String(doc.done || (doc.outcome && doc.outcome.done) || '').trim();
  return {
    jobId: String(doc.job_id || ''),
    at,
    time: at && typeof timeFn === 'function' ? timeFn(at) : '',
    changed: isChanged,
    changedLabel: isChanged ? 'изменение' : 'изменений нет',
    done,
    found: found || (isChanged ? 'что именно — в отчёте запуска' : ''),
    proposals: proposals.map((p) => {
      const one = p && typeof p === 'object' ? p : { what: String(p || '') };
      return { what: String(one.what || one.text || ''), why: String(one.why || ''), action: String(one.action || '') };
    }),
    created: Array.isArray(doc.created) ? doc.created.map(String) : [],
    runId: String(doc.run_id || ''),
  };
}

/** Одна строка истории — «пн 16, 09:00 · изменение · предложено Дело». */
export function historyLine(row) {
  const doc = row && typeof row === 'object' ? row : {};
  const bits = [];
  if (doc.time) bits.push(doc.time);
  bits.push(doc.changed ? 'изменение' : 'изменений нет');
  if (doc.found) bits.push(doc.found);
  if (doc.created.length) bits.push('создано: ' + doc.created.join(', '));
  if (doc.proposals.length) bits.push('предложений: ' + doc.proposals.length);
  return bits.join(' · ');
}

/** Корзина: сколько дней осталось до окончательного удаления. */
export function trashModel(items, nowSec) {
  const list = Array.isArray(items) ? items.filter((one) => one && typeof one === 'object') : [];
  const now = num(nowSec) || Math.floor(Date.now() / 1000);
  return list.map((item) => {
    const at = num(item.trashed_at || item.at);
    const expires = num(item.expires_at) || (at ? at + 30 * 86400 : 0);
    const days = expires ? Math.max(0, Math.ceil((expires - now) / 86400)) : 30;
    return {
      jobId: String(item.job_id || item.id || ''),
      title: String(item.title || item.name || 'дежурство'),
      at,
      expires,
      daysLeft: days,
      daysText: days > 0
        ? 'вернуть можно ещё ' + countText(days, ['день', 'дня', 'дней'])
        : 'срок возврата истёк',
    };
  });
}

/** Карточка-предложение «Предлагаю дежурство». */
export function proposalModel(proposal) {
  const doc = proposal && typeof proposal === 'object' ? proposal : {};
  return {
    id: String(doc.id || doc.proposal_id || ''),
    title: String(doc.title || 'дежурство'),
    why: String(doc.why || doc.zachem || ''),
    when: String(doc.when_human || scheduleHuman(doc.when || doc.schedule) || doc.when || ''),
    check: String(doc.check || doc.task || ''),
    onChange: String(doc.on_change_label || onChangeLabel(doc.on_change || doc.onChange || 'inbox')),
    source: String(doc.source || 'manager'),
    sourceLabel: SOURCE_LABELS[String(doc.source || 'manager')] || 'предложил руководитель',
    schedule: String(doc.when || doc.schedule || ''),
    kind: String(doc.kind || 'model'),
    limitText: String(doc.limit_text || ''),
  };
}

/** Черновик мастера из предложения руководителя. */
export function draftFromProposal(proposal, room) {
  const doc = proposalModel(proposal);
  const draft = emptyDraft({ room: room || (proposal && proposal.room) || '' });
  return normalizeDraft({
    ...draft,
    title: doc.title,
    kind: doc.kind,
    task: doc.check || doc.why,
    schedule: doc.schedule || draft.schedule,
    onChange: String((proposal && (proposal.on_change || proposal.onChange)) || 'inbox'),
    source: 'manager',
    template: 'proposal',
    from: { kind: 'proposal', id: doc.id, title: doc.title },
  });
}

/** Черновик мастера из шаблона (`GET /api/duty-templates`). */
export function draftFromTemplate(template, room) {
  const doc = template && typeof template === 'object' ? template : {};
  const draft = emptyDraft({ room });
  return normalizeDraft({
    ...draft,
    title: String(doc.title || ''),
    kind: String(doc.kind || 'model'),
    task: String(doc.task || doc.check || ''),
    schedule: String(doc.schedule || draft.schedule),
    change: doc.change && typeof doc.change === 'object'
      ? doc.change : { rule: String(doc.change || 'new_items'), threshold: 0 },
    onChange: String(doc.on_change || doc.onChange || 'inbox'),
    level: String(doc.level || 'propose'),
    notify: Array.isArray(doc.notify) ? doc.notify : ['today'],
    source: 'manual',
    template: String(doc.id || ''),
  });
}

/** Черновик «Сделать регулярным» из Дела. */
export function draftFromDelo(delo, room) {
  const doc = delo && typeof delo === 'object' ? delo : {};
  const title = String(doc.title || doc.name || 'дело');
  const draft = emptyDraft({ room: room || doc.room || '' });
  return normalizeDraft({
    ...draft,
    title: 'Проверять: ' + title,
    kind: 'model',
    task: 'Проверить, что по делу «' + title + '» ничего не изменилось и сроки не подошли. '
      + 'Если изменилось — коротко сказать что.',
    schedule: '0 9 * * 1-5',
    change: { rule: 'model_compares', threshold: 0 },
    onChange: 'inbox',
    level: 'propose',
    source: 'delo',
    from: { kind: 'delo', id: String(doc.id || ''), title },
  });
}

/** Черновик «Сделать регулярным» из запроса. */
export function draftFromRequest(request, room) {
  const doc = request && typeof request === 'object' ? request : {};
  const title = String(doc.title || doc.title_short || 'запрос');
  const draft = emptyDraft({ room: room || doc.room || '' });
  return normalizeDraft({
    ...draft,
    title: 'Проверять: ' + title,
    kind: 'model',
    task: 'Повторить проверку из запроса «' + title + '»: что изменилось с прошлого раза. '
      + 'Если изменилось — сказать что и предложить следующий шаг.',
    schedule: '0 9 * * 1-5',
    change: { rule: 'model_compares', threshold: 0 },
    onChange: 'inbox',
    level: 'propose',
    source: 'delo',
    from: { kind: 'request', id: String(doc.id || ''), title },
  });
}

/** Черновик мастера из существующего дежурства (правка и «Дублировать»). */
export function draftFromDuty(item, room) {
  const doc = item && typeof item === 'object' ? item : {};
  const draft = emptyDraft({ room: room || doc.room || '' });
  return normalizeDraft({
    ...draft,
    mode: 'edit',
    jobId: String(doc.job_id || doc.id || ''),
    title: String(doc.name || doc.title || 'дежурство'),
    kind: KINDS.some((one) => one.id === doc.kind) ? doc.kind : 'model',
    task: String(doc.task || doc.prompt || doc.excerpt || ''),
    workdir: String(doc.workdir || draft.workdir),
    schedule: String(doc.schedule || draft.schedule),
    catch_up: doc.catch_up === undefined ? true : !!doc.catch_up,
    started_paused: doc.enabled === false,
    change: doc.change && typeof doc.change === 'object'
      ? doc.change : { rule: 'new_items', threshold: 0 },
    onChange: String(doc.on_change || doc.onChange || 'inbox'),
    level: String(doc.level || 'propose'),
    notify: Array.isArray(doc.notify) ? doc.notify : ['today'],
    source: String(doc.source || 'manual'),
  });
}

/** Черновик-копия для «Дублировать»: новое название, без привязки к заданию. */
export function duplicateDraft(draft, room) {
  const doc = normalizeDraft(draft);
  return normalizeDraft({
    ...doc,
    mode: 'create',
    jobId: '',
    title: doc.title ? doc.title + ' (копия)' : 'дежурство (копия)',
    started_paused: true,
    room: room || doc.room,
  });
}

/** Шаблоны из ответа `GET /api/duty-templates`. */
export function templatesFrom(payload) {
  const list = Array.isArray(payload) ? payload
    : (payload && Array.isArray(payload.items) ? payload.items : []);
  return list.filter((one) => one && typeof one === 'object').map((one) => ({
    id: String(one.id || one.template || ''),
    title: String(one.title || one.name || 'шаблон'),
    for: String(one.for || one.for_what || ''),
    when: String(one.when_human || scheduleHuman(one.schedule) || ''),
    check: String(one.check || one.task || ''),
    change: String(one.change_human || changeText(one.change) || ''),
    action: String(one.action_human || onChangeLabel(one.on_change || one.onChange || 'inbox')),
    kind: String(one.kind || 'model'),
    schedule: String(one.schedule || ''),
    level: String(one.level || 'propose'),
    raw: one,
  }));
}

/** Заголовок корзины: «Корзина (2)». */
export function trashTitle(items) {
  const list = Array.isArray(items) ? items : [];
  return 'Корзина' + (list.length ? ' (' + list.length + ')' : '');
}

/** Итог сухого прогона одной строкой. */
export function dryRunText(result) {
  const doc = result && typeof result === 'object' ? result : {};
  const changed = String(doc.changed || '').toLowerCase();
  const found = Array.isArray(doc.found) ? doc.found.map(String).filter(Boolean) : [];
  if (doc.text) return String(doc.text);
  if (changed === 'да' || changed === 'yes') {
    return found.length
      ? 'Нашло бы ' + countText(found.length, ['изменение', 'изменения', 'изменений']) + ': ' + found.join('; ')
      : 'Нашло бы изменение (подробности — в отчёте запуска)';
  }
  if (changed === 'нет' || changed === 'no') return 'Изменений бы не нашло';
  return 'Проверка выполнена';
}

export default {
  WIZARD_STEPS, KINDS, SCHEDULE_PRESETS, CHANGE_RULES, ON_CHANGE, LEVELS, NOTIFY,
  scheduleHuman, emptyDraft, normalizeDraft, stepProblems, draftProblems, draftReady,
  draftSummary, localCommand, previewModel, createBody, patchBody, outcomeLine,
  historyRow, historyLine, trashModel, proposalModel, draftFromProposal, draftFromTemplate,
  draftFromDelo, draftFromRequest, templatesFrom, dryRunText, changeText, onChangeLabel,
  levelLabel,
};
