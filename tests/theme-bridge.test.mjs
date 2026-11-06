/**
 * theme-bridge.test.mjs — мост токенов Hermes: цвета, контраст, отказы.
 *
 * Доказываем три вещи, без которых мост опасен:
 *   1) цвета разбираются строго: всё, что не цвет (выражения, url, мусор), — в отказ;
 *   2) пара цветов хозяина с контрастом < 4.5:1 НЕ применяется — остаётся наша;
 *   3) тема и радиус применяются к нашей странице, схема (светлая/тёмная) —
 *      следует за хозяином.
 *
 *   node --test tests/theme-bridge.test.mjs
 */

import test from 'node:test';
import assert from 'node:assert/strict';
import {
  parseColor,
  contrast,
  composite,
  luminance,
  buildPalette,
  applyMessage,
  connectTheme,
  MIN_CONTRAST,
  SURFACE_VARS,
  INK_VARS,
  STROKE_VARS,
  ALLOWED_VARS
} from '../web/js/theme-bridge.js';

/** Минимальный документ: корень со style.setProperty, тело, probe-элемент. */
function fakeDoc() {
  const props = {};
  const root = {
    dataset: {},
    style: {
      setProperty(name, value) { props[name] = value; },
      getPropertyValue(name) { return props[name] || ''; }
    },
    removeAttribute() {}
  };
  const empty = { style: {}, setAttribute() {}, appendChild() {} };

  return {
    props,
    root,
    doc: {
      documentElement: root,
      body: { appendChild() {} },
      createElement: () => empty,
      // Хозяина в тесте нет: своих значений «из CSS» не читаем — только присланные.
      defaultView: { getComputedStyle: () => ({ color: '', backgroundColor: '' }) }
    }
  };
}

/* ─────────────────────────────────────────────── разбор цвета */

test('parseColor: hex, rgb(), rgba(), color(srgb …)', () => {
  assert.deepEqual(parseColor('#17171a'), { r: 23, g: 23, b: 26, a: 1 });
  assert.deepEqual(parseColor('#fff'), { r: 255, g: 255, b: 255, a: 1 });
  assert.deepEqual(parseColor('#0053fd80'), { r: 0, g: 83, b: 253, a: 128 / 255 });
  assert.deepEqual(parseColor('rgb(23, 23, 26)'), { r: 23, g: 23, b: 26, a: 1 });
  assert.deepEqual(parseColor('rgb(23 23 26 / 50%)'), { r: 23, g: 23, b: 26, a: 0.5 });
  assert.deepEqual(parseColor('rgba(0, 83, 253, 0.25)'), { r: 0, g: 83, b: 253, a: 0.25 });

  const srgb = parseColor('color(srgb 0.09 0.09 0.1 / 0.94)');
  assert.equal(srgb.a, 0.94);
  assert.ok(Math.abs(srgb.r - 23) <= 1, 'канал color(srgb) приводится к 0…255');
});

test('parseColor: не-цвета отклоняются (никаких выражений и url)', () => {
  for (const junk of [
    '',
    '   ',
    'expression(alert(1))',
    'url(javascript:alert(1))',
    'rgb(1,2)',
    'rgb(a,b,c)',
    'red',
    '#12345',
    '#gggggg',
    'var(--ui-accent)',
    'а'.repeat(200)
  ]) {
    assert.equal(parseColor(junk), null, 'должно быть отклонено: ' + JSON.stringify(junk));
  }
});

/* ─────────────────────────────────────────────── контраст */

test('contrast: крайние случаи и порог 4.5:1', () => {
  const white = parseColor('#ffffff');
  const black = parseColor('#000000');

  assert.equal(Math.round(contrast(black, white)), 21);
  assert.equal(Math.round(contrast(white, white)), 1);
  assert.equal(Math.round(luminance(white) * 100) / 100, 1);

  // #777777 на белом — 4.48:1, чуть ниже AA: мост обязан отклонить.
  const gray = parseColor('#777777');
  assert.ok(contrast(gray, white) < MIN_CONTRAST);
  assert.ok(contrast(gray, white) > 4.4);

  // Акцент Hermes на белом — с запасом.
  assert.ok(contrast(parseColor('#0053fd'), white) > 4.5);
  // Полупрозрачный текст кладём на подложку, а не считаем «как есть».
  const faded = composite(parseColor('#00000080'), white);
  assert.ok(faded.r > 100 && faded.r < 160);
});

/* ─────────────────────────────────────────────── сборка палитры */

const DARK_HOST = {
  type: 'team-room:theme',
  version: 1,
  scheme: 'dark',
  radius: 10,
  tokens: {
    '--ui-bg-chrome': 'rgb(23, 23, 26)',
    '--ui-bg-sidebar': 'rgb(10, 10, 11)',
    '--ui-bg-editor': 'rgb(29, 29, 33)',
    '--ui-bg-elevated': 'rgb(35, 35, 39)',
    '--ui-accent': '#6e9bff',
    '--ui-base': '#e7e7ea',
    '--ui-text-primary': 'rgba(231, 231, 234, 0.94)',
    '--ui-text-secondary': 'rgba(231, 231, 234, 0.74)',
    '--ui-text-tertiary': 'rgba(231, 231, 234, 0.62)',
    '--ui-stroke-secondary': 'rgba(110, 155, 255, 0.16)'
  }
};

test('buildPalette применяет цвета хозяина и схему', () => {
  const palette = buildPalette(DARK_HOST);

  assert.equal(palette.scheme, 'dark');
  assert.deepEqual(palette.rejected, []);
  assert.equal(palette.vars['--ui-bg-chrome'], 'rgb(23, 23, 26)');
  assert.equal(palette.vars['--ui-text-primary'], 'rgba(231, 231, 234, 0.94)');
  assert.equal(palette.vars['--ui-accent'], '#6e9bff');
  // Акцент работает и как ссылка: он же становится нашим --theme-primary.
  assert.equal(palette.vars['--theme-primary'], '#6e9bff');
  assert.equal(palette.radius, 10);
  assert.equal(palette.vars['--radius-lg'], '10px');
  assert.equal(palette.vars['--radius-md'], '8px');
});

test('buildPalette отклоняет нечитаемые пары и сохраняет наши значения', () => {
  const payload = {
    type: 'team-room:theme',
    scheme: 'dark',
    tokens: {
      '--ui-bg-chrome': '#17171a',
      '--ui-bg-editor': '#1d1d21',
      // серый текст на почти чёрном — 2.1:1
      '--ui-text-tertiary': '#3a3a3d',
      '--ui-text-primary': '#f0f0f2'
    }
  };

  const palette = buildPalette(payload);
  const names = palette.rejected.map((r) => r.name);

  assert.ok(names.includes('--ui-text-tertiary'), 'нечитаемый текст должен быть отклонён');
  assert.ok(!('--ui-text-tertiary' in palette.vars), 'отклонённое значение не применяется');
  assert.match(palette.rejected.find((r) => r.name === '--ui-text-tertiary').reason, /контраст/);
  // Читаемый текст рядом остаётся — отказ точечный, а не «всё или ничего».
  assert.equal(palette.vars['--ui-text-primary'], '#f0f0f2');
});

test('buildPalette: мусор, прозрачный фон и чужая схема — в отказ', () => {
  const palette = buildPalette({
    type: 'team-room:theme',
    scheme: 'синяя',
    radius: 999,
    tokens: {
      '--ui-bg-chrome': 'red; background: url(http://evil)',
      '--ui-bg-sidebar': 'rgba(255, 255, 255, 0.4)',
      '--ui-accent': 'expression(alert(1))'
    }
  });

  const names = palette.rejected.map((r) => r.name);
  assert.equal(palette.scheme, null);
  assert.equal(palette.radius, undefined);
  assert.ok(names.includes('scheme'));
  assert.ok(names.includes('--ui-bg-chrome'));
  assert.ok(names.includes('--ui-bg-sidebar'));
  assert.ok(names.includes('--ui-accent'));
  assert.deepEqual(Object.keys(palette.vars), []);
});

test('buildPalette не падает на пустом и неверном сообщении', () => {
  for (const payload of [null, undefined, 42, 'тема', [], {}]) {
    const palette = buildPalette(payload);
    assert.deepEqual(palette.vars, {});
    assert.ok(palette.rejected.length >= 1);
  }
});

test('белый список переменных не расширяется случайно', () => {
  assert.deepEqual(ALLOWED_VARS.slice(0, 4), SURFACE_VARS);
  assert.ok(INK_VARS.every((name) => name.startsWith('--ui-text-')));
  assert.ok(STROKE_VARS.every((name) => name.startsWith('--ui-stroke-')));
  // Ничего исполняемого: только переменные --ui-*.
  assert.ok(ALLOWED_VARS.every((name) => /^--ui-[a-z-]+$/.test(name)));
});

/* ─────────────────────────────────────────────── применение */

test('applyMessage пишет переменные и схему в документ', () => {
  const { doc, root, props } = fakeDoc();
  const result = applyMessage(DARK_HOST, doc);

  assert.equal(result.applied, true);
  assert.equal(result.scheme, 'dark');
  assert.equal(root.dataset.theme, 'dark');
  assert.equal(root.dataset.themeSource, 'host');
  assert.equal(props['--ui-bg-chrome'], 'rgb(23, 23, 26)');
  assert.equal(props['--radius-lg'], '10px');
});

test('applyMessage без применения не трогает тему страницы', () => {
  const { doc, root, props } = fakeDoc();
  const result = applyMessage({ type: 'team-room:theme', scheme: 'синяя', tokens: { '--ui-accent': 'javascript:1' } }, doc);

  assert.equal(result.applied, false);
  assert.deepEqual(root.dataset, {});
  assert.deepEqual(props, {});
});

test('connectTheme: подписка на сообщение и безопасный запрос при старте', () => {
  const sent = [];
  const listeners = [];
  const win = {
    document: fakeDoc().doc,
    parent: { postMessage: (data, origin) => sent.push({ data, origin }) },
    addEventListener: (type, fn) => listeners.push({ type, fn }),
    removeEventListener: (type, fn) => {
      const i = listeners.findIndex((l) => l.type === type && l.fn === fn);
      if (i >= 0) listeners.splice(i, 1);
    },
    dispatchEvent: () => true,
    CustomEvent: class { constructor(type, init) { this.type = type; this.detail = init && init.detail; } }
  };

  const bridge = connectTheme({ win });
  assert.ok(bridge, 'мост должен подняться');

  // При старте — один короткий запрос темы, без данных.
  assert.equal(sent.length, 1);
  assert.deepEqual(sent[0].data, { type: 'team-room:theme:request', version: 1 });
  assert.equal(sent[0].origin, '*');

  const onMessage = listeners.find((l) => l.type === 'message').fn;
  onMessage({ data: DARK_HOST });
  assert.equal(bridge.state.host, true);
  assert.equal(bridge.state.last.scheme, 'dark');
  assert.equal(win.document.documentElement.dataset.theme, 'dark');

  // Чужой тип сообщения игнорируется.
  const before = win.document.documentElement.dataset.theme;
  onMessage({ data: { type: 'что-то-другое', scheme: 'light' } });
  assert.equal(win.document.documentElement.dataset.theme, before);

  bridge.disconnect();
});
