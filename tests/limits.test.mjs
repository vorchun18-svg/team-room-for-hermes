/**
 * limits.test.mjs — W29-P9: панель «Расход и лимиты» и чип в шапке.
 *
 * Что проверяем:
 *  · цифры подписываются по-русски: «645 тыс.», «1,2 млн», «$1,28», «84 %»;
 *  · пороги предупреждений — 70 % и 90 %, перерасход — отдельный тон;
 *  · предупреждение называет провайдера и процент (не «ошибка» без причины);
 *  · черновик лимитов ↔ тело запроса: пустое поле = «лимита нет»;
 *  · чип шапки показывает серверную строку и ведёт в раздел лимитов;
 *  · панель и чип подключены к экрану настроек и шапке (одна точка встраивания).
 */

import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

import {
  tokensText, moneyText, pctText, progressTone, limitText, limitWarning,
  windowSummary, wastedText, draftFromLimits, limitsPayload, providerList,
} from '../web/js/views/limits.js';
import { chipText, chipTone, chipTitle, chipSignature } from '../web/js/limits-chip.js';

const HERE = dirname(fileURLToPath(import.meta.url));
const read = (rel) => readFileSync(join(HERE, '..', rel), 'utf8');

const PAYLOAD = {
  source: 'state.db',
  line: 'Расход за 5 ч: 645 тыс. токенов · $1.28',
  chip: '5 ч: 645 тыс. · 84 %',
  windows: {
    '5h': {
      calls: 46,
      tokens: { total: 645000 },
      cost_usd: 1.284,
      summary: { calls: 46, tokens: 645000, cost_usd: 1.284 },
      providers: [
        {
          provider: 'example-ai', title: 'Example AI', calls: 32, cost_usd: 1.284,
          tokens: { input: 12840, cache_read: 402000, cache_write: 74000, output: 12400, total: 501240 },
          models: [{ model: 'model-a', calls: 26 }],
          limit: { kind: 'tokens', value: 600000, used: 501240, pct: 83.5, status: 'warn' },
        },
      ],
    },
    week: {
      calls: 903,
      tokens: { total: 16874000 },
      cost_usd: 21.37,
      summary: { calls: 903, tokens: 16874000, cost_usd: 21.37 },
      providers: [
        {
          provider: 'example-ai', title: 'Example AI', calls: 612, cost_usd: 21.37,
          tokens: { total: 14694000 },
          models: [],
          limit: { kind: 'tokens', value: 20000000, used: 14694000, pct: 73.5, status: 'warn' },
        },
      ],
    },
  },
  limits: {
    providers: {
      'example-ai': { '5h': { kind: 'tokens', value: 600000 }, week: { kind: 'tokens', value: 20000000 } },
    },
  },
};

test('подписи чисел и денег', () => {
  assert.equal(tokensText(645000), '645 тыс.');
  assert.equal(tokensText(1200000), '1,2 млн');
  assert.equal(tokensText(640), '640');
  assert.equal(moneyText(1.284), '$1,28');
  assert.equal(moneyText(0.0031), '$0,003');
  assert.equal(pctText(83.5), '84 %');
  assert.equal(pctText(9.4), '9,4 %');
});

test('пороги лимита: 70 % — жёлтый, 90 % — красный, 100 % — перерасход', () => {
  assert.equal(progressTone({ pct: 12 }), 'ok');
  assert.equal(progressTone({ pct: 70 }), 'warn');
  assert.equal(progressTone({ pct: 89.9 }), 'warn');
  assert.equal(progressTone({ pct: 90 }), 'alert');
  assert.equal(progressTone({ pct: 120 }), 'over');
  assert.equal(progressTone({ status: 'over', pct: 100 }), 'over');
});

test('подпись и предупреждение называют провайдера, единицу и период', () => {
  const limit = PAYLOAD.windows['5h'].providers[0].limit;
  const text = limitText(limit, '5h');
  assert.match(text, /600 тыс\. токенов/);
  assert.match(text, /84 %/);
  const warn = limitWarning(limit, 'Example AI');
  assert.match(warn, /Example AI/);
  assert.match(warn, /84 %/);
  assert.match(limitWarning({ ...limit, pct: 95, status: 'alert' }, 'Example AI'), /Почти весь лимит/);
  assert.match(limitWarning({ ...limit, pct: 110, status: 'over' }, 'Example AI'), /исчерпан/);
  assert.equal(limitWarning(null, 'Example AI'), '');
  assert.match(limitText(null, 'week'), /не задан/);
});

test('итог окна — вызовы, токены и стоимость', () => {
  const text = windowSummary(PAYLOAD.windows['5h'].summary);
  assert.match(text, /^46 вызовов/);
  assert.match(text, /645 тыс\. токенов/);
  assert.match(text, /\$1,28/);
});

test('строка «потрачено зря» — с сервера, иначе честная', () => {
  assert.match(wastedText({ text: 'Потрачено зря: 1 запусков на 190 тыс. токенов' }), /190 тыс/);
  assert.match(wastedText({}), /нет/);
  assert.match(wastedText({ attempts: 2, tokens: 3000, cost_usd: 0.4 }), /2 запуска/);
});

test('черновик лимитов ↔ тело запроса: пустое поле — «лимита нет»', () => {
  const draft = draftFromLimits(PAYLOAD.limits);
  assert.equal(draft['example-ai']['5h'].value, '600000');
  assert.equal(draft['example-ai'].week.kind, 'tokens');

  const body = limitsPayload(draft);
  assert.equal(body.providers['example-ai']['5h'].value, 600000);
  assert.equal(body.providers['example-ai']['5h'].kind, 'tokens');

  draft['example-ai']['5h'] = { kind: 'calls', value: ' 1 500 ' };
  const body2 = limitsPayload(draft);
  assert.deepEqual(body2.providers['example-ai']['5h'], { kind: 'calls', value: 1500 });

  draft['example-ai']['5h'].value = '';
  const body3 = limitsPayload(draft);
  assert.equal(body3.providers['example-ai']['5h'], undefined);
  assert.equal(limitsPayload({}).providers && Object.keys(limitsPayload({}).providers).length, 0);
});

test('список провайдеров — из расхода и из уже заданных лимитов', () => {
  const list = providerList(PAYLOAD);
  assert.deepEqual(list.map((p) => p.key), ['example-ai']);
  const extended = providerList({ windows: {}, limits: { providers: { 'demo-cloud': {} } } });
  assert.ok(extended.some((p) => p.key === 'demo-cloud'), 'заданный лимит виден даже без запусков');
});

test('чип шапки: серверная строка, тон по худшему лимиту, ведёт в лимиты', () => {
  assert.equal(chipText(PAYLOAD), '5 ч: 645 тыс. · 84 %');
  assert.equal(chipTone(PAYLOAD), 'warn');
  assert.equal(chipTone({ windows: { week: { summary: { worst_limit: { pct: 95, status: 'alert' } } } } }), 'alert');
  assert.equal(chipTone({ windows: {} }), 'ok');
  assert.match(chipTitle(PAYLOAD), /Открыть «Расход и лимиты»/);
  assert.match(chipTitle(null, { message: 'нет связи' }), /недоступен/);
  assert.equal(chipSignature(), '');
  assert.match(chipText({ windows: { '5h': { tokens: { total: 1200000 } } } }), /1,2 млн/);
});

test('панель встроена в настройки, чип — в шапку', () => {
  const settings = read('web/js/views/settings.js');
  assert.match(settings, /id: 'set-limits'/);
  assert.match(settings, /mountLimits\(limitsHost/);
  const app = read('web/js/app.js');
  assert.match(app, /limitsChip\(\{ navigate \}\)/);
  assert.match(app, /refreshLimits\(api/);
  const html = read('web/index.html');
  assert.match(html, /css\/limits-chip\.css/);
});
