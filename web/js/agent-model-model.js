/**
 * agent-model-model.js — «Модель, провайдер и мышление»: чистая логика без DOM.
 *
 * Только данные и подписи — модуль целиком покрыт `node --test`
 * (tests/agent-model.test.mjs) и не знает про document:
 *  • EFFORT_RU / effortOptions — уровни мышления по-русски (из каталога, запасной список);
 *  • providersOf — только настроенные провайдеры (остальные не предлагаем);
 *  • filterModels — поиск по вводу, группы по провайдерам;
 *  • modelSettingsOf / agentDefaultOf — что действует в комнате и что у самого агента;
 *  • defaultsText / settingsLabel — «по умолчанию: …» и «провайдер · модель · мышление».
 *
 * Интерфейс — в agent-model-ui.js: он импортирует всё отсюда и реэкспортирует наружу.
 */

/** Уровни мышления по-русски — как лестница в самом Hermes. */
export const EFFORT_RU = {
  minimal: 'Минимальное',
  low: 'Низкое',
  medium: 'Среднее',
  high: 'Высокое',
  xhigh: 'Очень высокое',
  max: 'Максимум',
  ultra: 'Ультра',
};

/** Порядок, если каталог не пришёл: от «Минимальное» к «Максимум». */
export const EFFORT_ORDER = ['minimal', 'low', 'medium', 'high', 'xhigh', 'max', 'ultra'];

/** «Размышление выключено» — то же значение, что ждёт движок. */
export const THINKING_OFF = 'none';

/** Значение усилий в один вид: 'none' | 'minimal'… | '' (не задано). */
export function normalizeReasoning(value) {
  const key = String(value === null || value === undefined ? '' : value).trim().toLowerCase();
  if (!key) return '';
  if (key === 'off' || key === 'false' || key === 'no' || key === 'выкл') return THINKING_OFF;
  if (key === 'auto' || key === 'default' || key === 'inherit') return '';
  return key;
}

/** Подпись уровня: «Высокое», «без размышления», «как у агента». */
export function reasoningLabel(value) {
  const key = normalizeReasoning(value);
  if (key === THINKING_OFF) return 'без размышления';
  if (!key) return 'как у агента';
  return EFFORT_RU[key] || key;
}

export function lowerFirst(text) {
  const s = String(text || '');
  return s ? s.charAt(0).toLowerCase() + s.slice(1) : s;
}

/** Короткая подпись модели: «Дилижанс миди · среднее» / «как у агента». */
export function shortModelText(catalog, settings) {
  const s = settings || {};
  const parts = [s.model ? modelLabelOf(catalog, s.provider, s.model) : 'как у агента'];
  const effort = lowerFirst(reasoningLabel(s.reasoning));
  if (s.model || normalizeReasoning(s.reasoning)) parts.push(effort);
  return parts.join(' · ');
}

/** Сегменты «Усилия» из каталога (или из общего списка Hermes). */
export function effortOptions(catalog) {
  const raw = Array.isArray(catalog && catalog.efforts) ? catalog.efforts : [];
  const out = [];
  const seen = new Set();
  for (const item of raw) {
    const id = String(typeof item === 'string' ? item : (item && (item.id || item.value)) || '').trim().toLowerCase();
    if (!id || seen.has(id) || id === THINKING_OFF) continue;
    seen.add(id);
    const serverLabel = item && typeof item === 'object' ? String(item.label || '') : '';
    out.push({ value: id, label: EFFORT_RU[id] || serverLabel || id });
  }
  if (out.length) return out;
  return EFFORT_ORDER.map((id) => ({ value: id, label: EFFORT_RU[id] }));
}

/** Провайдеры, с которыми можно работать: только настроенные (есть ключ). */
export function providersOf(catalog) {
  const list = Array.isArray(catalog && catalog.providers) ? catalog.providers : [];
  return list
    .filter((p) => p && p.id && p.configured !== false)
    .map((p) => ({
      id: String(p.id),
      label: String(p.label || p.id),
      models: (Array.isArray(p.models) ? p.models : [])
        .filter((m) => m && m.id)
        .map((m) => ({ id: String(m.id), label: String(m.label || m.id) })),
    }));
}

export function allProviders(catalog) {
  const list = Array.isArray(catalog && catalog.providers) ? catalog.providers : [];
  return list.filter((p) => p && p.id).map((p) => ({
    id: String(p.id),
    label: String(p.label || p.id),
    configured: p.configured !== false,
  }));
}

export function providerLabelOf(catalog, providerId) {
  const id = String(providerId || '');
  const hit = allProviders(catalog).find((p) => p.id === id);
  return hit ? hit.label : id;
}

export function modelLabelOf(catalog, providerId, modelId) {
  const id = String(modelId || '');
  if (!id) return 'не выбрана';
  // Ищем и у ненастроенных провайдеров: уже выбранную модель всё равно надо назвать.
  const list = Array.isArray(catalog && catalog.providers) ? catalog.providers : [];
  const p = list.find((x) => x && String(x.id) === String(providerId || ''));
  const hit = p && Array.isArray(p.models) ? p.models.find((m) => m && String(m.id) === id) : null;
  return hit ? String(hit.label || hit.id) : id;
}

/** Подпись «по умолчанию: провайдер · модель · мышление». */
export function defaultsText(catalog, defaults) {
  const d = defaults || {};
  if (!d.provider && !d.model) return 'по умолчанию: как у агента';
  const parts = [];
  if (d.provider) parts.push(providerLabelOf(catalog, d.provider));
  if (d.model) parts.push(modelLabelOf(catalog, d.provider, d.model));
  parts.push(lowerFirst(reasoningLabel(d.reasoning)));
  return 'по умолчанию: ' + parts.join(' · ');
}

/** Полная подпись «провайдер · модель · мышление» (для title и дерева работ). */
export function settingsLabel(catalog, settings) {
  const s = settings || {};
  const parts = [];
  if (s.provider) parts.push(providerLabelOf(catalog, s.provider));
  parts.push(s.model ? modelLabelOf(catalog, s.provider, s.model) : 'модель не выбрана');
  parts.push(lowerFirst(reasoningLabel(s.reasoning)));
  return parts.join(' · ');
}

/** Участник комнаты по профилю (из members[]). */
export function memberOf(room, profile) {
  const list = Array.isArray(room && room.members) ? room.members : [];
  const name = String(profile || '');
  return list.find((m) => m && String(m.profile || '') === name) || null;
}

function pickSettings(raw) {
  const o = raw && typeof raw === 'object' ? raw : {};
  return {
    provider: String(o.provider || ''),
    model: String(o.model || ''),
    reasoning: normalizeReasoning(o.reasoning !== undefined ? o.reasoning : o.effort),
  };
}

/** Что сейчас действует: настройка комнаты, иначе настройка агента. */
export function modelSettingsOf(room, profile, member) {
  // Переданный «лёгкий» участник может быть без настроек — тогда берём состав комнаты.
  const given = member && (member.model_settings || member.agent_default) ? member : null;
  const m = given || memberOf(room, profile);
  const entry = room && room.agent_models && typeof room.agent_models === 'object'
    ? room.agent_models[String(profile || '')] : null;
  const fromMember = m && m.model_settings && typeof m.model_settings === 'object' ? m.model_settings : null;
  if (fromMember) {
    return Object.assign(pickSettings(fromMember), {
      source: String(fromMember.source || (entry ? 'room' : 'agent')),
    });
  }
  if (entry) return Object.assign(pickSettings(entry), { source: 'room' });
  return Object.assign(agentDefaultOf(room, profile, m), { source: 'agent' });
}

/** Настройки самого агента (read-only): к ним же возвращает «Вернуть по умолчанию». */
export function agentDefaultOf(room, profile, member) {
  const given = member && (member.agent_default || member.model_settings) ? member : null;
  const m = given || memberOf(room, profile);
  const own = m && m.agent_default && typeof m.agent_default === 'object' ? m.agent_default : null;
  return pickSettings(own);
}

/**
 * Модели по провайдерам с учётом ввода.
 * Ввод есть — ищем по ВСЕМ настроенным провайдерам и показываем группами;
 * ввода нет — только выбранный провайдер.
 * @param {object} catalog
 * @param {{provider?: string, query?: string}} [opts]
 * @returns {Array<{id: string, label: string, models: Array<{id: string, label: string}>}>}
 */
export function filterModels(catalog, opts = {}) {
  const o = opts || {};
  const query = String(o.query || '').trim().toLowerCase();
  const provider = String(o.provider || '');
  const out = [];
  for (const p of providersOf(catalog)) {
    if (!query && provider && p.id !== provider) continue;
    const models = (p.models || []).filter((m) => !query
      || m.id.toLowerCase().includes(query)
      || m.label.toLowerCase().includes(query));
    if (!models.length && !(query && p.label.toLowerCase().includes(query))) continue;
    out.push({ id: p.id, label: p.label, models });
  }
  return out;
}

export function flatModels(groups) {
  const out = [];
  for (const g of groups || []) {
    for (const m of g.models || []) out.push({ provider: g.id, providerLabel: g.label, id: m.id, label: m.label });
  }
  return out;
}
