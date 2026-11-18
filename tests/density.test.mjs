/**
 * tests/density.test.mjs — плотность интерфейса (W25-L2, R5 §7.9).
 * Проверяем чистые функции и контроллер: без браузера, с подставным хранилищем.
 */
import test from 'node:test';
import assert from 'node:assert/strict';

import {
  DENSITIES, DENSITY_KEY, DENSITY_LABEL, normalizeDensity, nextDensity, densityLabel,
  writeDensity, applyDensity, installDensity,
} from '../web/js/density.js';

function memStorage(init = {}) {
  const map = new Map(Object.entries(init));
  return {
    getItem: (k) => (map.has(k) ? map.get(k) : null),
    setItem: (k, v) => { map.set(k, String(v)); },
    removeItem: (k) => { map.delete(k); },
    dump: () => Object.fromEntries(map),
  };
}

function fakeApp() {
  return { dataset: {} };
}

test('плотности — ровно три и с известными именами', () => {
  assert.deepEqual(DENSITIES, ['compact', 'normal', 'roomy']);
});

test('мусор и пустое значение приводятся к normal', () => {
  assert.equal(normalizeDensity(''), 'normal');
  assert.equal(normalizeDensity(null), 'normal');
  assert.equal(normalizeDensity(undefined), 'normal');
  assert.equal(normalizeDensity('huge'), 'normal');
  assert.equal(normalizeDensity('compact'), 'compact');
  assert.equal(normalizeDensity(' roomy '), 'roomy');
});

test('перебор по кругу: compact → normal → roomy → compact', () => {
  assert.equal(nextDensity('compact'), 'normal');
  assert.equal(nextDensity('normal'), 'roomy');
  assert.equal(nextDensity('roomy'), 'compact');
  assert.equal(nextDensity('roomy', -1), 'normal');
  assert.equal(nextDensity('compact', -1), 'roomy');
  // Неизвестное значение не ломает круг: считаем от normal.
  assert.equal(nextDensity('???'), 'roomy');
});

test('подписи по-русски — по одному слову на значение', () => {
  assert.equal(densityLabel('compact'), DENSITY_LABEL.compact);
  assert.equal(densityLabel('normal'), 'Обычно');
  assert.equal(densityLabel('roomy'), 'Просторно');
  assert.equal(densityLabel('nonsense'), 'Обычно');
});

test('writeDensity пишет ключ teamroom.density и отдаёт записанное', () => {
  const store = memStorage();
  assert.equal(writeDensity(store, 'roomy'), 'roomy');
  assert.equal(store.dump()[DENSITY_KEY], 'roomy');
  // Мусор пишется как normal, а не как мусор.
  assert.equal(writeDensity(store, 'mega'), 'normal');
  assert.equal(store.dump()[DENSITY_KEY], 'normal');
});

test('writeDensity без хранилища не падает', () => {
  assert.equal(writeDensity(null, 'compact'), 'compact');
  assert.equal(writeDensity(undefined, 'x'), 'normal');
});

test('applyDensity ставит data-density на #app и помнит выбор', () => {
  const store = memStorage();
  const app = fakeApp();
  assert.equal(applyDensity(app, 'compact', store), 'compact');
  assert.equal(app.dataset.density, 'compact');
  assert.equal(store.dump()[DENSITY_KEY], 'compact');
});

test('applyDensity без элемента всё равно запоминает', () => {
  const store = memStorage();
  assert.equal(applyDensity(null, 'roomy', store), 'roomy');
  assert.equal(store.dump()[DENSITY_KEY], 'roomy');
});

test('installDensity читает сохранённое значение и ставит атрибут', () => {
  const store = memStorage({ [DENSITY_KEY]: 'roomy' });
  const app = fakeApp();
  const ctl = installDensity({ app, storage: store });
  assert.equal(ctl.value, 'roomy');
  assert.equal(app.dataset.density, 'roomy');
  assert.equal(ctl.label(), 'Просторно');
});

test('installDensity: set и next меняют и атрибут, и хранилище', () => {
  const store = memStorage();
  const app = fakeApp();
  const ctl = installDensity({ app, storage: store });
  assert.equal(ctl.value, 'normal');
  assert.equal(ctl.next(), 'roomy');
  assert.equal(app.dataset.density, 'roomy');
  assert.equal(store.dump()[DENSITY_KEY], 'roomy');
  assert.equal(ctl.set('compact'), 'compact');
  assert.equal(app.dataset.density, 'compact');
  assert.equal(ctl.next(-1), 'roomy');
});

test('installDensity: битое хранилище — работаем на normal, без исключений', () => {
  const broken = { getItem() { throw new Error('denied'); }, setItem() { throw new Error('denied'); } };
  const app = fakeApp();
  const ctl = installDensity({ app, storage: broken });
  assert.equal(ctl.value, 'normal');
  assert.equal(app.dataset.density, 'normal');
  assert.equal(ctl.next(), 'roomy'); // в памяти контроллера значение всё равно меняется
});
