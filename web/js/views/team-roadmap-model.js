/**
 * team-roadmap-model.js — чистая логика вкладок «Команда» и «Карта проекта».
 *
 * Никакого DOM и никаких импортов: всё проверяется node --test (tests/team-roadmap.test.mjs).
 * Здесь живут только преобразования данных API → то, что рисуют views/team.js и views/roadmap.js.
 */

/* ── Словари ──────────────────────────────────────────────────────────────── */

export const ROLE_LABELS = {
  orchestrator: 'руководитель',
  worker: 'исполнитель',
  critic: 'критик',
  observer: 'наблюдатель',
};

/** Роль комнаты → русская подпись. */
export function roleLabel(role) {
  return ROLE_LABELS[role] || String(role || '');
}

export const MEMBER_STATES = {
  idle: { label: 'свободен', kind: 'idle' },
  working: { label: 'работает', kind: 'working' },
  waiting: { label: 'ждёт человека', kind: 'waiting' },
};

/** Живой статус участника → {state,label,kind}; pulse — только у работы. */
export function memberState(state) {
  const key = MEMBER_STATES[state] ? state : 'idle';
  const meta = MEMBER_STATES[key];
  return { state: key, label: meta.label, kind: meta.kind, pulse: key === 'working' };
}

export const STAGE_STATES = {
  // W17-R §4: у этапа — понятный статус и значок, а не шесть «дальше» подряд.
  done: { label: 'готово', tone: 'ok', icon: 'check' },
  active: { label: 'в работе', tone: 'info', icon: 'play' },
  next: { label: 'не начат', tone: 'muted', icon: 'circle-outline' },
  blocked: { label: 'ждёт решения', tone: 'warn', icon: 'warning' },
};

/** Статус этапа → {status,label,tone,icon}; неизвестное значение считается «не начат». */
export function stageMeta(status) {
  const key = STAGE_STATES[status] ? status : 'next';
  const meta = STAGE_STATES[key];
  return { status: key, label: meta.label, tone: meta.tone, icon: meta.icon || 'circle-outline' };
}

/** Русское склонение: plural(2, ['этап','этапа','этапов']) → 'этапа'. */
export function plural(n, forms) {
  const num = Math.abs(Number(n) || 0);
  const a = forms && forms[0] ? forms[0] : '';
  const b = forms && forms[1] ? forms[1] : a;
  const c = forms && forms[2] ? forms[2] : b;
  const tail100 = num % 100;
  const tail10 = num % 10;
  if (tail100 >= 11 && tail100 <= 14) return c;
  if (tail10 === 1) return a;
  if (tail10 >= 2 && tail10 <= 4) return b;
  return c;
}

/* ── Время ───────────────────────────────────────────────────────────────── */

const MIN = 60 * 1000;
const HOUR = 60 * MIN;
const DAY = 24 * HOUR;

function toMs(value) {
  const n = Number(value);
  if (!Number.isFinite(n) || n <= 0) return null;
  return n > 1e11 ? n : n * 1000; // секунды API и миллисекунды JS
}

function pad2(n) {
  return String(n).padStart(2, '0');
}

function startOfDay(ms) {
  const d = new Date(ms);
  d.setHours(0, 0, 0, 0);
  return d.getTime();
}

/** «14:20» по локальному поясу. */
export function clockLabel(ts) {
  const ms = toMs(ts);
  if (ms === null) return '';
  const d = new Date(ms);
  return pad2(d.getHours()) + ':' + pad2(d.getMinutes());
}

/**
 * Сколько уже идёт работа: «уже 4 мин», «уже 1 ч 10 мин», «уже 2 дн».
 * null/будущее → пустая строка (вьюха ничего не рисует).
 */
export function sinceLabel(startedAt, now = Date.now()) {
  const ms = toMs(startedAt);
  if (ms === null) return '';
  const base = toMs(now) === null ? Date.now() : toMs(now);
  const diff = Math.max(0, base - ms);
  if (diff < MIN) return 'только что';
  if (diff < HOUR) return 'уже ' + Math.floor(diff / MIN) + ' мин';
  if (diff < DAY) {
    const h = Math.floor(diff / HOUR);
    const m = Math.floor((diff % HOUR) / MIN);
    return m ? 'уже ' + h + ' ч ' + pad2(m) + ' мин' : 'уже ' + h + ' ч';
  }
  const d = Math.floor(diff / DAY);
  return 'уже ' + d + ' ' + plural(d, ['день', 'дня', 'дней']);
}

/** «сегодня, 14:20» / «вчера, 14:20» / «27 окт, 14:20» / '' для пустого. */
export function lastWorkLabel(finishedAt, now = Date.now()) {
  const ms = toMs(finishedAt);
  if (ms === null) return '';
  const base = toMs(now) === null ? Date.now() : toMs(now);
  const today = startOfDay(base);
  const day = startOfDay(ms);
  const time = clockLabel(ms);
  if (day === today) return 'сегодня, ' + time;
  if (day === today - DAY) return 'вчера, ' + time;
  const d = new Date(ms);
  const months = ['янв', 'фев', 'мар', 'апр', 'мая', 'июн', 'июл', 'авг', 'сен', 'окт', 'ноя', 'дек'];
  const sameYear = d.getFullYear() === new Date(base).getFullYear();
  const date = d.getDate() + ' ' + months[d.getMonth()] + (sameYear ? '' : ' ' + d.getFullYear());
  return date + ', ' + time;
}

/** «обновлено 28 окт, 09:31»; пусто, если время неизвестно. */
export function updatedLabel(ts, now = Date.now()) {
  const label = lastWorkLabel(ts, now);
  return label ? 'обновлено ' + label : '';
}

/* ── Состав команды ─────────────────────────────────────────────────────── */

/** Роль, которую можно менять в интерфейсе (руководителя комнаты и критика — нельзя). */
export function isRoleEditable(role) {
  return role !== 'orchestrator' && role !== 'critic';
}

function profileById(profiles) {
  const map = new Map();
  const list = Array.isArray(profiles) ? profiles : profiles && Array.isArray(profiles.items) ? profiles.items : [];
  for (const p of list) {
    if (p && p.profile) map.set(p.profile, p);
  }
  return map;
}

function asList(value) {
  return Array.isArray(value) ? value.filter((x) => x && typeof x === 'object') : [];
}

/**
 * Разделить ответ /team на «в составе» и «вне состава».
 * Принимает {members, outside|outside_members}, а также флаг outside_room/in_room у участника.
 * @returns {{inside: object[], outside: object[]}}
 */
export function splitTeam(team) {
  const data = team && typeof team === 'object' ? team : {};
  const members = asList(data.members);
  const topOutside = asList(data.outside).concat(asList(data.outside_members));
  const inside = [];
  const seen = new Set();
  for (const member of members) {
    if (!member || !member.profile || seen.has(member.profile)) continue;
    if (member.outside_room === true || member.in_room === false) continue;
    seen.add(member.profile);
    inside.push(member);
  }
  const outside = [];
  const flagged = members.filter((m) => m && m.profile && (m.outside_room === true || m.in_room === false));
  for (const item of topOutside.concat(flagged)) {
    if (!item || !item.profile || seen.has(item.profile)) continue;
    if (outside.some((o) => o.profile === item.profile)) continue;
    outside.push(item);
  }
  return { inside, outside };
}

/**
 * Роль по настройкам комнаты (Д1): руководитель — тот, кого комната записала в
 * `orchestrator`; критик — в `critic`. Даже если движок отдал участнику роль
 * «worker», подпись в интерфейсе должна быть «руководитель комнаты».
 * @param {object[]} members
 * @param {object|null} room
 * @returns {object[]}
 */
export function withRoomRoles(members, room) {
  const orchestrator = String((room && room.orchestrator) || '');
  const critic = String((room && room.critic) || '');
  return (Array.isArray(members) ? members : []).map((m) => {
    if (!m || typeof m !== 'object') return m;
    const profile = String(m.profile || m.name || '');
    if (orchestrator && profile === orchestrator) return Object.assign({}, m, { role: 'orchestrator' });
    if (critic && profile === critic) return Object.assign({}, m, { role: 'critic' });
    return m;
  });
}

const ROLE_RANK = { orchestrator: 0, worker: 1, observer: 2, critic: 3 };

/** Руководитель комнаты → исполнители → критик; внутри группы порядок API сохраняется. */
export function sortMembers(members) {
  const list = Array.isArray(members) ? members.slice() : [];
  return list
    .map((m, i) => ({ m, i }))
    .sort((a, b) => {
      const ra = ROLE_RANK[a.m && a.m.role] ?? 9;
      const rb = ROLE_RANK[b.m && b.m.role] ?? 9;
      if (ra !== rb) return ra - rb;
      return a.i - b.i;
    })
    .map((x) => x.m);
}

/**
 * Карточка участника: соединяем TeamMember с описанием профиля из /api/profiles.
 * @param {object} member  TeamMember
 * @param {object|object[]|null} profiles  ответ /api/profiles или его items
 * @param {number} [now]
 */
export function memberModel(member, profiles, now = Date.now()) {
  const m = member && typeof member === 'object' ? member : {};
  const map = profiles instanceof Map ? profiles : profileById(profiles);
  const profile = map.get(m.profile) || null;
  const role = m.role || (profile && profile.kind === 'critic' ? 'critic' : 'worker');
  const state = memberState(m.state);
  const current = m.current && typeof m.current === 'object'
    ? {
      taskId: m.current.task_id || null,
      title: m.current.title || '',
      status: m.current.status || '',
      startedAt: m.current.started_at || null,
      since: sinceLabel(m.current.started_at, now),
    }
    : null;
  const last = m.last && typeof m.last === 'object'
    ? {
      taskId: m.last.task_id || null,
      title: m.last.title || '',
      status: m.last.status || '',
      finishedAt: m.last.finished_at || null,
      at: lastWorkLabel(m.last.finished_at, now),
      summary: m.last.summary || '',
    }
    : null;
  const counts = m.counts && typeof m.counts === 'object' ? m.counts : {};
  return {
    profile: m.profile,
    name: (profile && profile.title) || m.title || m.profile || '',
    role,
    roleLabel: roleLabel(role),
    roleEditable: isRoleEditable(role),
    removable: isRoleEditable(role),
    isDirector: role === 'orchestrator',
    isCritic: role === 'critic',
    pinned: m.pinned === true,
    title: m.title || (profile && profile.title) || '',
    description: String(m.description || (profile && profile.description) || ''),
    // WAVE13 §11: имя и должность участника (сервер разбирает описание профиля).
    label: String(m.label || (profile && profile.label) || ''),
    labelName: String(m.name || (profile && profile.name) || ''),
    labelJob: String(m.job || (profile && profile.job) || ''),
    labelSource: String(m.label_source || (profile && profile.label_source) || ''),
    labelPending: m.label_pending === true || ((profile && profile.label_pending) === true),
    customLabel: String(m.custom_label || ''),
    model: m.model || (profile && profile.model) || '',
    tags: Array.isArray(profile && profile.tags) ? profile.tags : [],
    state: state.state,
    stateLabel: state.label,
    stateKind: state.kind,
    pulse: state.pulse,
    note: m.note || '',
    counts: {
      done: Number(counts.done) || 0,
      blocked: Number(counts.blocked) || 0,
      active: Number(counts.active) || 0,
    },
    current,
    last,
  };
}

/** Счётчики состава для подзаголовка. */
export function countTeam(models) {
  const list = Array.isArray(models) ? models : [];
  const out = { total: list.length, working: 0, waiting: 0, idle: 0 };
  for (const m of list) {
    if (m.stateKind === 'working') out.working += 1;
    else if (m.stateKind === 'waiting') out.waiting += 1;
    else out.idle += 1;
  }
  return out;
}

/**
 * Профили, которых ещё нет в комнате (для диалога «Добавить агента»).
 * @param {object|object[]} profiles  ответ /api/profiles
 * @param {object} team  ответ /team (или null)
 * @param {object} [room]  комната (orchestrator/critic/members)
 */
export function availableProfiles(profiles, team, room) {
  const all = Array.isArray(profiles) ? profiles : (profiles && Array.isArray(profiles.items) ? profiles.items : []);
  const taken = new Set();
  const { inside, outside } = splitTeam(team);
  for (const m of inside.concat(outside)) if (m && m.profile) taken.add(m.profile);
  if (room && typeof room === 'object') {
    if (room.orchestrator) taken.add(room.orchestrator);
    if (room.critic) taken.add(room.critic);
    for (const m of asList(room.members)) if (m.profile) taken.add(m.profile);
  }
  return all.filter((p) => p && p.profile && !taken.has(p.profile));
}

/* ── Карта проекта ──────────────────────────────────────────────────────── */

function phasesOf(roadmap) {
  return Array.isArray(roadmap && roadmap.phases) ? roadmap.phases.filter((p) => p && p.id) : [];
}

/**
 * Текущая фаза: roadmap.current → первый active → первый blocked → первый next → последняя done.
 * @returns {object|null}
 */
export function pickCurrent(roadmap) {
  const phases = phasesOf(roadmap);
  if (!phases.length) return null;
  const declared = roadmap && roadmap.current;
  if (declared && typeof declared === 'object' && declared.id) {
    const found = phases.find((p) => p.id === declared.id);
    if (found) return found;
  }
  if (typeof declared === 'string') {
    const found = phases.find((p) => p.id === declared);
    if (found) return found;
  }
  const byStatus = ['active', 'blocked', 'next'];
  for (const status of byStatus) {
    const hit = phases.find((p) => p.status === status);
    if (hit) return hit;
  }
  const done = phases.filter((p) => p.status === 'done');
  return done.length ? done[done.length - 1] : phases[0];
}

/** «готово 1 из 6 этапов» + доля для полосы прогресса. */
export function progressInfo(phases) {
  const list = Array.isArray(phases) ? phases.filter((p) => p && p.id) : [];
  const total = list.length;
  const done = list.filter((p) => p.status === 'done').length;
  const percent = total ? Math.round((done / total) * 100) : 0;
  const label = total
    ? 'готово ' + done + ' из ' + total + ' ' + plural(total, ['этапа', 'этапов', 'этапов'])
    : 'этапов пока нет';
  return { done, total, percent, label };
}

/**
 * Всё, что нужно блоку «Где мы сейчас».
 * @returns {{current: object|null, index: number, progress: object, blocked: object[], upcoming: object|null, nextAfter: object|null}}
 */
export function phaseSummary(roadmap) {
  const phases = phasesOf(roadmap);
  const current = pickCurrent(roadmap);
  const index = current ? phases.findIndex((p) => p.id === current.id) : -1;
  const blocked = phases.filter((p) => p.status === 'blocked');
  const upcoming = phases.find((p) => p.status === 'next') || null;
  const nextAfter = index >= 0 ? phases.slice(index + 1).find((p) => p.status !== 'done') || null : null;
  return {
    current,
    index,
    progress: progressInfo(phases),
    blocked,
    upcoming,
    nextAfter,
  };
}

/** Источник этапа: ссылка на запрос, если API её отдал. */
export function phaseSource(phase) {
  if (!phase || typeof phase !== 'object') return null;
  const rid = phase.source_request || phase.request_id || phase.root_id || null;
  const title = phase.source_title || '';
  return rid ? { requestId: String(rid), title: title || String(rid) } : null;
}

/** Решения, связанные с этапом: по phase_id или по тому же источнику-запросу. */
export function relatedDecisions(roadmap, phaseId) {
  const decisions = Array.isArray(roadmap && roadmap.decisions) ? roadmap.decisions.filter((d) => d && d.text) : [];
  const phase = phasesOf(roadmap).find((p) => p.id === phaseId);
  if (!phase) return [];
  const source = phaseSource(phase);
  return decisions.filter((d) => {
    if (d.phase === phaseId || d.stage === phaseId) return true;
    if (source && d.source_request && d.source_request === source.requestId) return true;
    return false;
  });
}

/** Шаги/вопросы из запроса-источника этапа (для панели деталей). */
export function relatedSteps(items, requestId) {
  const list = Array.isArray(items) ? items.filter((i) => i && i.text) : [];
  if (!requestId) return [];
  return list.filter((i) => {
    if (i.source_request && i.source_request === requestId) return true;
    return false;
  });
}

const DECISION_TITLES = {
  accepted: 'Принято',
  proposed: 'Предложено',
};

/**
 * Сгруппировать решения по статусу с учётом фильтра и поиска.
 * @param {object[]} decisions
 * @param {{status?: 'all'|'accepted'|'proposed', q?: string}} [filter]
 * @returns {{groups: {key:string,title:string,items:object[]}[], counts: object, shown: number}}
 */
export function decisionGroups(decisions, filter = {}) {
  const list = Array.isArray(decisions) ? decisions.filter((d) => d && d.text) : [];
  const status = filter.status || 'all';
  const q = String(filter.q || '').trim().toLocaleLowerCase('ru');
  const counts = { total: list.length, accepted: 0, proposed: 0, other: 0 };
  for (const d of list) {
    if (d.status === 'accepted') counts.accepted += 1;
    else if (d.status === 'proposed') counts.proposed += 1;
    else counts.other += 1;
  }
  const match = (d) => {
    if (status !== 'all' && d.status !== status) return false;
    if (!q) return true;
    const hay = [d.text, d.why, d.owner, d.source_title].filter(Boolean).join(' ').toLocaleLowerCase('ru');
    return hay.includes(q);
  };
  const groups = [];
  for (const key of ['accepted', 'proposed']) {
    const items = list.filter((d) => d.status === key && match(d));
    if (items.length) groups.push({ key, title: DECISION_TITLES[key], items });
  }
  const shown = groups.reduce((acc, g) => acc + g.items.length, 0);
  return { groups, counts, shown };
}

/** Путь зеркала STATE.md: roadmap.mirror_path → room.mirror_path → context_files. */
export function mirrorPathOf(roadmap, room) {
  const candidates = [
    roadmap && roadmap.mirror_path,
    roadmap && roadmap.state_path,
    room && room.mirror_path,
  ];
  for (const c of candidates) {
    if (typeof c === 'string' && c.trim()) return c.trim();
  }
  const files = room && Array.isArray(room.context_files) ? room.context_files : [];
  const hit = files.find((f) => typeof f === 'string' && /state\.md$/i.test(f.trim()));
  return hit ? hit.trim() : null;
}

/** «Свести в систему» доступно, только если в комнате есть хоть что-то осмысленное. */
export function canConsolidate(roadmap) {
  const phases = phasesOf(roadmap);
  const decisions = Array.isArray(roadmap && roadmap.decisions) ? roadmap.decisions : [];
  if (decisions.length) return true;
  return phases.some((p) => p.status === 'done' || p.status === 'active' || p.status === 'blocked');
}

/** Пустая карта: данных ещё нет (все этапы next, решений нет). */
export function isEmptyRoadmap(roadmap) {
  const phases = phasesOf(roadmap);
  if (!phases.length) return true;
  const decisions = Array.isArray(roadmap && roadmap.decisions) ? roadmap.decisions : [];
  if (decisions.length) return false;
  return phases.every((p) => (p.status || 'next') === 'next' && !p.note);
}

export default {
  ROLE_LABELS,
  MEMBER_STATES,
  STAGE_STATES,
  roleLabel,
  withRoomRoles,
  memberState,
  stageMeta,
  plural,
  clockLabel,
  sinceLabel,
  lastWorkLabel,
  updatedLabel,
  isRoleEditable,
  splitTeam,
  sortMembers,
  memberModel,
  countTeam,
  availableProfiles,
  pickCurrent,
  progressInfo,
  phaseSummary,
  phaseSource,
  relatedDecisions,
  relatedSteps,
  decisionGroups,
  mirrorPathOf,
  canConsolidate,
  isEmptyRoadmap,
};
