/**
 * theme.test.mjs — договор о типографике и оформлении «как Hermes».
 *
 * Проверяем то, что видно пользователю и что легко сломать снова:
 *   1) базовый кегль 14 px (text-sm у Hermes) и шкала 12/14/16/18/20/24;
 *   2) межстрочный 1.5, гарнитура Segoe WPC → Segoe UI → system-ui;
 *   3) моно — JetBrains Mono: локальные файлы, OFL-1.1, local() раньше url();
 *   4) скругления 8–12 px, управления под 14 px;
 *   5) токены есть в светлой и тёмной теме (одно сырьё — два набора значений);
 *   6) ни один ЧИТАЕМЫЙ текст не мельче 11 px: аватары, счётные бейджи и
 *      подписи графиков — единственные исключения;
 *   7) счётные бейджи — 11 px, подписи — 12 px, основной текст — 14 px.
 *
 *   node --test tests/theme.test.mjs
 */

import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync, statSync, readdirSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import path from 'node:path';

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const read = (rel) => readFileSync(path.join(ROOT, rel), 'utf8');

const TOKENS = read('web/css/tokens.css');
const BASE = read('web/css/base.css');
const COMPONENTS = read('web/css/components.css');

/** Значение токена в указанном блоке (по умолчанию :root). */
function token(css, name) {
  const m = new RegExp('--' + name + '\\s*:\\s*([^;]+);').exec(css);
  return m ? m[1].trim() : null;
}

/** Число пикселей из значения токена. */
const px = (value) => {
  const m = /^([\d.]+)px$/.exec(String(value || '').trim());
  return m ? Number(m[1]) : null;
};

/* ─────────────────────────────────────────────── 1. Шкала и базовый кегль */

test('кегль: база 14 px и шкала 11/12/14/16/18/20/24', () => {
  const scale = {
    'text-2xs': 11,
    'text-xs': 12,
    'text-sm': 14,
    'text-base': 14,
    'text-md': 16,
    'text-lg': 18,
    'text-xl': 20,
    'text-2xl': 24
  };

  for (const [name, expected] of Object.entries(scale)) {
    const value = token(TOKENS, name);
    assert.equal(px(value), expected, `--${name} должно быть ${expected}px, а не ${value}`);
  }

  // Заголовок страницы 22–24, заголовок раздела 18–20 — проверяем по базе.
  assert.match(BASE, /h1 \{ font-size: var\(--text-2xl\); \}/);
  assert.match(BASE, /h2 \{ font-size: var\(--text-xl\); \}/);
  assert.match(BASE, /h3 \{ font-size: var\(--text-lg\); \}/);
  assert.ok(px(token(TOKENS, 'text-2xl')) >= 22 && px(token(TOKENS, 'text-2xl')) <= 24);
  assert.ok(px(token(TOKENS, 'text-lg')) >= 18 && px(token(TOKENS, 'text-lg')) <= 20);

  // Тело страницы живёт на --text-base (14), а не на своём числе.
  assert.match(BASE, /font-size: var\(--text-base\);/);
  assert.doesNotMatch(BASE, /body \{[^}]*font-size: 1[0-3]px/);
});

test('межстрочный 1.5 и плотность заголовков', () => {
  assert.equal(token(TOKENS, 'leading-normal'), '1.5');
  assert.match(BASE, /line-height: var\(--leading-normal\)/);
});

/* ─────────────────────────────────────────────── 2. Гарнитуры */

test('sans: Segoe WPC → Segoe UI → system-ui (как в Hermes)', () => {
  const sans = token(TOKENS, 'font-sans');
  assert.ok(sans, 'нет --font-sans');
  const order = ['Segoe WPC', 'Segoe UI', 'system-ui'].map((name) => sans.indexOf(name));
  assert.ok(order[0] >= 0, 'первой должна идти Segoe WPC');
  assert.ok(order[0] < order[1] && order[1] < order[2], 'порядок гарнитур нарушен: ' + sans);
  assert.ok(sans.includes('sans-serif'), 'нужен общий запасной sans-serif');
});

test('mono: JetBrains Mono первым, запасные Cascadia Mono и Consolas', () => {
  const mono = token(TOKENS, 'font-mono');
  assert.ok(mono, 'нет --font-mono');
  assert.ok(mono.trim().startsWith("'JetBrains Mono'"), 'JetBrains Mono должен стоять первым: ' + mono);
  assert.ok(mono.includes('Cascadia Mono'));
  assert.ok(mono.includes('Consolas'));
});

test('JetBrains Mono: локальные файлы, local() раньше url(), лицензия OFL-1.1', () => {
  const faces = BASE.match(/@font-face\s*\{[^}]*\}/g) || [];
  const jb = faces.filter((block) => block.includes('JetBrains Mono'));
  assert.equal(jb.length, 2, 'нужны два @font-face: Regular и Bold');

  const weights = jb.map((block) => (/font-weight:\s*(\d+)/.exec(block) || [])[1]).sort();
  assert.deepEqual(weights, ['400', '700']);

  for (const block of jb) {
    const local = block.indexOf('local(');
    const url = block.indexOf('url(');
    assert.ok(local >= 0, 'нужен local(), чтобы не грузить файл зря');
    assert.ok(url > local, 'local() обязан идти раньше url()');
    assert.match(block, /format\('woff2'\)/);
    assert.match(block, /font-display:\s*swap/);
  }

  for (const file of ['web/fonts/JetBrainsMono-Regular.woff2', 'web/fonts/JetBrainsMono-Bold.woff2']) {
    const size = statSync(path.join(ROOT, file)).size;
    assert.ok(size > 20000, `${file} подозрительно мал: ${size} байт`);
  }

  const ofl = read('web/fonts/OFL.txt');
  assert.match(ofl, /SIL Open Font License/i);
  assert.match(ofl, /Version 1\.1/);
  assert.match(ofl, /JetBrains Mono/i);
});

/* ─────────────────────────────────────────────── 3. Геометрия */

test('скругления 8–12 px и управления под кегль 14', () => {
  const radii = {
    'radius-sm': [6, 8],
    'radius-md': [8, 10],
    'radius-lg': [8, 12],
    'radius-xl': [10, 12]
  };

  for (const [name, [min, max]] of Object.entries(radii)) {
    const value = px(token(TOKENS, name));
    assert.ok(value !== null, `нет --${name}`);
    assert.ok(value >= min && value <= max, `--${name} = ${value}px, ожидалось ${min}–${max}`);
  }

  const md = px(token(TOKENS, 'control-md'));
  assert.ok(md >= 30 && md <= 34, `кнопка/поле под 14 px: ожидалось 30–34, дано ${md}`);
});

/* ─────────────────────────────────────────────── 4. Обе темы */

test('токены есть в светлой и тёмной теме', () => {
  const dark = /:root\[data-theme='dark'\]\s*\{([\s\S]*?)\n\}/.exec(TOKENS);
  assert.ok(dark, 'нет блока :root[data-theme="dark"]');
  const auto = /@media \(prefers-color-scheme: dark\)\s*\{\s*:root\[data-theme='auto'\]\s*\{([\s\S]*?)\n  \}/.exec(TOKENS);
  assert.ok(auto, 'нет тёмных значений для :root[data-theme="auto"]');

  for (const name of ['theme-foreground', 'theme-background-seed', 'theme-card-seed', 'theme-neutral-chrome']) {
    assert.ok(token(TOKENS, name), `светлая тема: нет --${name}`);
    assert.ok(new RegExp('--' + name + '\\s*:').test(dark[1]), `тёмная тема: нет --${name}`);
  }

  // Обе темы дают одни и те же производные имена, на них живут компоненты.
  for (const name of ['ui-bg-chrome', 'ui-bg-sidebar', 'ui-bg-editor', 'ui-bg-elevated', 'ui-text-primary', 'ui-stroke-secondary']) {
    assert.ok(new RegExp('--' + name + '\\s*:').test(TOKENS), `нет производного --${name}`);
  }

  // Тёмная тема обязана переопределять статусы: светлые значения на тёмном фоне не читаются.
  assert.match(dark[1], /--ui-green:/);
  assert.match(dark[1], /--ui-blue:/);
});

/* ─────────────────────────────────────────────── 5. Мелкий текст */

const ALLOWED_TINY = /avatar|count-pill|tab__count|badge|codicon|kbd|dot/;

test('нет читаемого текста мельче 11 px (кроме аватаров, бейджей и подписей графиков)', () => {
  const dir = path.join(ROOT, 'web/css');
  const violations = [];

  for (const file of readdirSync(dir).filter((f) => f.endsWith('.css'))) {
    const css = readFileSync(path.join(dir, file), 'utf8');
    const rule = /([^{}]+)\{([^{}]*)\}/g;
    let m;

    while ((m = rule.exec(css)) !== null) {
      const selector = m[1].replace(/\/\*[\s\S]*?\*\//g, '').trim();
      const body = m[2];
      if (!selector || selector.startsWith('@')) continue;

      const sizes = body.match(/font-size:\s*([\d.]+)px/g) || [];
      for (const hit of sizes) {
        const value = Number(/([\d.]+)px/.exec(hit)[1]);
        if (!Number.isFinite(value) || value >= 11) continue;
        if (ALLOWED_TINY.test(selector)) continue;
        violations.push(`${file}: ${selector} → ${value}px`);
      }
    }
  }

  assert.deepEqual(violations, [], 'текст мельче 11 px:\n' + violations.join('\n'));
});

test('бейджи-счётчики 11 px, подписи 12 px, основной текст 14 px', () => {
  // Один класс на все счётчики: сайдбар, вкладки, версия ответа.
  assert.match(COMPONENTS, /\.count-pill,[^{]*\{[^}]*font-size: var\(--text-2xs\)/);
  assert.match(COMPONENTS, /\.count-pill,\s*\n?\.tab__count, \.kbd \{ font-size: var\(--text-2xs\); \}/);

  // Подписи полей, подсказки, крошки и мелкие кнопки — 12 px, не 14.
  assert.match(COMPONENTS, /\.btn--sm, \.table th \{ font-size: var\(--text-xs\); \}/);
  assert.match(COMPONENTS, /\.topbar__sub, \.card__sub, \.field__hint, \.field__error, \.page-hint,/);

  // Аватар — одна буква, не текст: 10 px допустимо.
  assert.match(COMPONENTS, /\.avatar--sm \{ font-size: 10px; \}/);
});

