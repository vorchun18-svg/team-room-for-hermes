/**
 * theme-bridge.js — мост токенов оформления Hermes → «Комната команды».
 *
 * Зачем: окно комнаты живёт внутри Hermes Desktop, а значит должно выглядеть
 * его продолжением, а не «старым сайтом в рамке». Плагин (desktop-plugin/plugin.js)
 * читает у хозяина вычисленные значения токенов (--ui-bg-*, --ui-text-*, акцент,
 * схема, радиус) и присылает их сюда одним сообщением:
 *
 *     { type: 'team-room:theme', version: 1,
 *       scheme: 'dark' | 'light',
 *       tokens: { '--ui-bg-chrome': 'rgb(23, 23, 26)', '--ui-accent': '#0053fd', … },
 *       radius: 8 }
 *
 * Правила (почему именно так):
 *  1. В браузере, в демо и в тестах хозяина нет — тогда мост молчит, и всё
 *     работает на наших собственных токенах (tokens.css) как раньше.
 *  2. Контраст важнее единообразия: если пара цветов хозяина даёт < 4.5:1
 *     (пользовательская тема Hermes может быть любой), значение НЕ применяется,
 *     остаётся наше. Отклонённые пары собираются в state.rejected — их видно
 *     в консоли и в отчёте.
 *  3. Ничего не «принимаем на веру»: тип сообщения, схема и значения проверяются;
 *     каждый цвет обязан разбираться как цвет (никаких выражений и CSS-инъекций),
 *     имена переменных — только из белого списка ниже.
 *  4. Схема (светлая/тёмная) следует за хозяином: приходит в том же сообщении.
 *
 * Модуль намеренно не зависит от остального приложения: его можно проверить
 * в node (чистые функции) и подключить первым в index.html.
 */

/** Тип сообщения «тема хозяина» и запрос темы при старте. */
export const THEME_MESSAGE = 'team-room:theme';
export const THEME_REQUEST = 'team-room:theme:request';

/** Порог WCAG AA для обычного текста. */
export const MIN_CONTRAST = 4.5;

/** Что принимаем у хозяина: поверхности. */
export const SURFACE_VARS = [
  '--ui-bg-chrome',
  '--ui-bg-sidebar',
  '--ui-bg-editor',
  '--ui-bg-elevated'
];

/** Что принимаем у хозяина: цвет текста (альфа-лестница). */
export const INK_VARS = [
  '--ui-text-primary',
  '--ui-text-secondary',
  '--ui-text-tertiary',
  '--ui-text-quaternary'
];

/** Что принимаем у хозяина: штрихи (границы). */
export const STROKE_VARS = [
  '--ui-stroke-primary',
  '--ui-stroke-secondary',
  '--ui-stroke-tertiary',
  '--ui-stroke-quaternary'
];

/** Акцент и «основной цвет чернил» (из него считается лестница прозрачности). */
export const ACCENT_VARS = ['--ui-accent', '--ui-base'];

export const ALLOWED_VARS = SURFACE_VARS.concat(INK_VARS, STROKE_VARS, ACCENT_VARS);

/** Как наши токены называются у хозяина (совпадают почти один в один). */
const FALLBACK_ALIAS = {
  '--ui-base': ['--ui-base', '--ui-text-primary']
};

/* ─────────────────────────────────────────────────────────── цвета ─────── */

/**
 * Разобрать цвет в {r,g,b,a}. Понимает #rgb, #rrggbb, #rrggbbaa,
 * rgb()/rgba() (запятые и пробелы, проценты), color(srgb …).
 * Возвращает null, если это не цвет — такие значения не применяем.
 */
export function parseColor(value) {
  if (typeof value !== 'string') return null;
  const text = value.trim().toLowerCase();
  if (!text || text.length > 120) return null;

  const hex = /^#([0-9a-f]{3,8})$/.exec(text);
  if (hex) {
    const h = hex[1];
    const to = (s) => parseInt(s, 16);
    if (h.length === 3 || h.length === 4) {
      return {
        r: to(h[0] + h[0]),
        g: to(h[1] + h[1]),
        b: to(h[2] + h[2]),
        a: h.length === 4 ? to(h[3] + h[3]) / 255 : 1
      };
    }
    if (h.length === 6 || h.length === 8) {
      return {
        r: to(h.slice(0, 2)),
        g: to(h.slice(2, 4)),
        b: to(h.slice(4, 6)),
        a: h.length === 8 ? to(h.slice(6, 8)) / 255 : 1
      };
    }
    return null;
  }

  const rgb = /^rgba?\(([^)]*)\)$/.exec(text);
  if (rgb) {
    const parts = rgb[1].split(/[,/\s]+/).filter(Boolean);
    if (parts.length < 3) return null;
    const channel = (part) => {
      if (part.endsWith('%')) return Math.round((parseFloat(part) / 100) * 255);
      return parseFloat(part);
    };
    const nums = parts.slice(0, 3).map(channel);
    if (nums.some((n) => !Number.isFinite(n))) return null;
    let alpha = 1;
    if (parts.length >= 4) {
      alpha = parts[3].endsWith('%') ? parseFloat(parts[3]) / 100 : parseFloat(parts[3]);
    }
    if (!Number.isFinite(alpha)) return null;
    const clamp = (n) => Math.min(255, Math.max(0, n));
    return { r: clamp(nums[0]), g: clamp(nums[1]), b: clamp(nums[2]), a: Math.min(1, Math.max(0, alpha)) };
  }

  // Chromium иногда отдаёт color-mix-результат так: color(srgb 0.09 0.09 0.1 / 0.94)
  const srgb = /^color\(srgb\s+([^)]*)\)$/.exec(text);
  if (srgb) {
    const parts = srgb[1].split(/[,/\s]+/).filter(Boolean);
    if (parts.length < 3) return null;
    const nums = parts.slice(0, 3).map((p) => parseFloat(p));
    if (nums.some((n) => !Number.isFinite(n))) return null;
    const alpha = parts.length >= 4 ? parseFloat(parts[3]) : 1;
    if (!Number.isFinite(alpha)) return null;
    const to255 = (n) => Math.min(255, Math.max(0, Math.round(n * 255)));
    return { r: to255(nums[0]), g: to255(nums[1]), b: to255(nums[2]), a: Math.min(1, Math.max(0, alpha)) };
  }

  if (text === 'transparent') return { r: 0, g: 0, b: 0, a: 0 };
  return null;
}

/** Наложить полупрозрачный цвет на подложку (иначе контраст не посчитать). */
export function composite(fg, bg) {
  const a = Math.min(1, Math.max(0, fg.a));
  return {
    r: fg.r * a + bg.r * (1 - a),
    g: fg.g * a + bg.g * (1 - a),
    b: fg.b * a + bg.b * (1 - a),
    a: 1
  };
}

/** Относительная яркость по WCAG 2.1. */
export function luminance(color) {
  const channel = (c) => {
    const v = c / 255;
    return v <= 0.03928 ? v / 12.92 : Math.pow((v + 0.055) / 1.055, 2.4);
  };
  return 0.2126 * channel(color.r) + 0.7152 * channel(color.g) + 0.0722 * channel(color.b);
}

/** Контраст двух цветов (1…21). Полупрозрачный передний план кладём на задний. */
export function contrast(fg, bg) {
  if (!fg || !bg) return 1;
  const front = fg.a >= 1 ? fg : composite(fg, bg);
  const back = bg.a >= 1 ? bg : composite(bg, { r: 255, g: 255, b: 255, a: 1 });
  const l1 = luminance(front);
  const l2 = luminance(back);
  const hi = Math.max(l1, l2);
  const lo = Math.min(l1, l2);
  return (hi + 0.05) / (lo + 0.05);
}

/** Округлить контраст до «3.2:1» — для журнала и тестов. */
export function ratioLabel(ratio) {
  return (Math.round(ratio * 10) / 10) + ':1';
}

/* ──────────────────────────────────────────────── сборка палитры ──────── */

function resolveColor(resolve, name) {
  if (typeof resolve !== 'function') return null;
  const aliases = FALLBACK_ALIAS[name] || [name];
  for (const alias of aliases) {
    let raw = null;
    try {
      raw = resolve(alias);
    } catch {
      raw = null;
    }
    const color = parseColor(raw);
    if (color) return color;
  }
  return null;
}

/**
 * Собрать набор переменных из сообщения хозяина.
 *
 * @param {object} payload  сообщение моста
 * @param {(name: string) => string} resolve  наше текущее значение токена в виде цвета
 * @returns {{scheme: string|null, vars: object, rejected: Array, radius?: number}}
 */
export function buildPalette(payload, resolve) {
  const result = { scheme: null, vars: {}, rejected: [] };

  if (!payload || typeof payload !== 'object') {
    result.rejected.push({ name: '*', value: '', reason: 'сообщение не объект' });
    return result;
  }

  const scheme = String(payload.scheme || payload.colorScheme || '').toLowerCase();
  if (scheme === 'light' || scheme === 'dark') result.scheme = scheme;
  else result.rejected.push({ name: 'scheme', value: scheme, reason: 'ожидается light или dark' });

  const tokens = payload.tokens && typeof payload.tokens === 'object'
    ? payload.tokens
    : payload.vars && typeof payload.vars === 'object'
      ? payload.vars
      : null;

  if (!tokens) {
    result.rejected.push({ name: 'tokens', value: '', reason: 'нет набора токенов' });
    return result;
  }

  const reject = (name, value, reason) => {
    result.rejected.push({ name, value: typeof value === 'string' ? value.slice(0, 60) : '', reason });
  };

  // 1. Поверхности. Фон обязан быть плотным: полупрозрачный фон ломает контраст.
  const surfaces = {};
  for (const name of SURFACE_VARS) {
    const raw = tokens[name];
    if (raw === undefined || raw === null || raw === '') continue;
    const color = parseColor(raw);
    if (!color) { reject(name, raw, 'не цвет'); continue; }
    if (color.a < 0.99) { reject(name, raw, 'полупрозрачный фон'); continue; }
    surfaces[name] = color;
    result.vars[name] = String(raw).trim();
  }

  const chrome = surfaces['--ui-bg-chrome'] || resolveColor(resolve, '--ui-bg-chrome');
  const editor = surfaces['--ui-bg-editor'] || resolveColor(resolve, '--ui-bg-editor');
  const backdrops = [chrome, editor].filter(Boolean);
  if (!backdrops.length) {
    reject('--ui-bg-chrome', '', 'нет ни фона хозяина, ни своего');
  }

  // 2. Текст: принимаем только читаемое (≥ 4.5:1 на обеих рабочих поверхностях).
  for (const name of INK_VARS) {
    const raw = tokens[name];
    if (raw === undefined || raw === null || raw === '') continue;
    const color = parseColor(raw);
    if (!color) { reject(name, raw, 'не цвет'); continue; }
    const worst = backdrops.reduce(
      (acc, bg) => (acc === null ? contrast(color, bg) : Math.min(acc, contrast(color, bg))),
      null
    );
    if (worst !== null && worst < MIN_CONTRAST) {
      reject(name, raw, 'контраст ' + ratioLabel(worst) + ' < 4.5:1');
      continue;
    }
    result.vars[name] = String(raw).trim();
  }

  // 3. Акцент и «чернила»: тот же порог — акцент у нас работает и как текст ссылки.
  for (const name of ACCENT_VARS) {
    const raw = tokens[name];
    if (raw === undefined || raw === null || raw === '') continue;
    const color = parseColor(raw);
    if (!color) { reject(name, raw, 'не цвет'); continue; }
    const worst = backdrops.reduce(
      (acc, bg) => (acc === null ? contrast(color, bg) : Math.min(acc, contrast(color, bg))),
      null
    );
    if (worst !== null && worst < MIN_CONTRAST) {
      reject(name, raw, 'контраст ' + ratioLabel(worst) + ' < 4.5:1');
      continue;
    }
    result.vars[name] = String(raw).trim();
    if (name === '--ui-accent') result.vars['--theme-primary'] = String(raw).trim();
  }

  // 4. Штрихи: декоративные, порог не нужен, но цвет обязателен.
  for (const name of STROKE_VARS) {
    const raw = tokens[name];
    if (raw === undefined || raw === null || raw === '') continue;
    if (!parseColor(raw)) { reject(name, raw, 'не цвет'); continue; }
    result.vars[name] = String(raw).trim();
  }

  // 5. Радиус: одно число, разумные пределы. Производные считаем от него.
  const radius = Number(payload.radius);
  if (Number.isFinite(radius) && radius >= 0 && radius <= 32) {
    const base = Math.round(radius);
    result.radius = base;
    result.vars['--radius-sm'] = Math.max(4, base - 4) + 'px';
    result.vars['--radius-md'] = Math.max(6, base - 2) + 'px';
    result.vars['--radius-lg'] = Math.max(6, base) + 'px';
    result.vars['--radius-xl'] = Math.max(8, base + 2) + 'px';
  }

  return result;
}

/* ──────────────────────────────────────────────── применение ──────────── */

/** Куда писать переменные и как читать наши запасные значения. */
function createContext(doc) {
  const root = doc && doc.documentElement;
  let probe = null;
  const holder = doc && (doc.body || doc.documentElement);

  const resolver = (name) => {
    if (!doc || !root) return '';
    try {
      if (!probe) {
        probe = doc.createElement('span');
        probe.setAttribute('aria-hidden', 'true');
        probe.style.cssText = 'position:absolute;left:-9999px;top:0;width:0;height:0;pointer-events:none';
        holder && holder.appendChild(probe);
      }
      const value = 'var(' + name + ')';
      // Разные свойства читают разные ветви var(): фон и чернила берём по очереди.
      probe.style.color = value;
      const asInk = doc.defaultView.getComputedStyle(probe).color;
      if (parseColor(asInk)) return asInk;
      probe.style.backgroundColor = value;
      const asBg = doc.defaultView.getComputedStyle(probe).backgroundColor;
      if (parseColor(asBg)) return asBg;
      return '';
    } catch {
      return '';
    }
  };

  const set = (vars) => {
    if (!root) return;
    for (const [name, value] of Object.entries(vars)) {
      try {
        root.style.setProperty(name, value);
      } catch {
        // одна неудачная переменная не должна рушить остальные
      }
    }
  };

  return { root, resolver, set };
}

/**
 * Применить сообщение хозяина к странице.
 * @returns {{applied: boolean, scheme: string|null, vars: object, rejected: Array}}
 */
export function applyMessage(payload, doc) {
  const target = doc || (typeof document !== 'undefined' ? document : null);
  if (!target) return { applied: false, scheme: null, vars: {}, rejected: [] };

  const ctx = createContext(target);
  const palette = buildPalette(payload, ctx.resolver);

  if (palette.scheme) {
    ctx.root.dataset.theme = palette.scheme;
    ctx.root.dataset.themeSource = 'host';
  }
  if (Object.keys(palette.vars).length) ctx.set(palette.vars);

  return {
    applied: Object.keys(palette.vars).length > 0 || Boolean(palette.scheme),
    scheme: palette.scheme,
    vars: palette.vars,
    rejected: palette.rejected
  };
}

/* ──────────────────────────────────────────────── связь с хозяином ────── */

let connection = null;

/**
 * Слушать сообщения хозяина и попросить тему при старте.
 *
 * Безопасность: слушаем только сообщения с точным типом и проверяем КАЖДОЕ
 * значение (см. buildPalette). Источник не фильтруем по origin — страница
 * комнаты живёт на 127.0.0.1 и в webview хозяина, а чужое окно всё равно
 * не сможет подсунуть ничего, кроме цвета.
 */
export function connectTheme(options = {}) {
  const win = options.win || (typeof window !== 'undefined' ? window : null);
  if (!win || typeof win.addEventListener !== 'function') return null;
  if (connection) return connection;

  const doc = options.doc || win.document;
  const state = { last: null, rejected: [], host: false };

  const onMessage = (event) => {
    const data = event && event.data;
    if (!data || typeof data !== 'object' || data.type !== THEME_MESSAGE) return;
    const result = applyMessage(data, doc);
    state.last = result;
    state.host = true;
    state.rejected = result.rejected;
    if (result.rejected.length && typeof console !== 'undefined' && console.info) {
      console.info('[team-room] тема хозяина: отклонено', result.rejected);
    }
    try {
      win.dispatchEvent(new win.CustomEvent('team-room:theme-applied', { detail: result }));
    } catch {
      // CustomEvent нет (старый движок) — не беда
    }
  };

  const request = () => {
    try {
      if (win.parent && win.parent !== win) {
        win.parent.postMessage({ type: THEME_REQUEST, version: 1 }, '*');
      }
    } catch {
      // нет доступа к родителю — значит и хозяина нет
    }
  };

  win.addEventListener('message', onMessage);
  request();

  connection = {
    state,
    request,
    apply: (payload) => {
      const result = applyMessage(payload, doc);
      state.last = result;
      state.host = true;
      state.rejected = result.rejected;
      return result;
    },
    disconnect() {
      win.removeEventListener('message', onMessage);
      connection = null;
      try {
        doc.documentElement.removeAttribute('data-theme-source');
      } catch {
        // ничего
      }
    }
  };

  // Для живой проверки в браузере (CDP): window.__teamRoomTheme.state
  try {
    win.__teamRoomTheme = connection;
  } catch {
    // окно могло быть заморожено
  }

  return connection;
}

/* Побочный эффект при подключении модуля в браузере: в node (тесты) — молчит. */
if (typeof window !== 'undefined' && typeof window.document !== 'undefined') {
  try {
    connectTheme();
  } catch {
    // страница живёт и без моста
  }
}
