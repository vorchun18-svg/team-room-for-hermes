/**
 * dictation-learn.test.mjs — тесты «Запомнить исправление?».
 *
 * Первая половина — чистая функция suggestFromEdit() на живых примерах
 * диктовки Михаила. Вторая — плашка offerLearning() на фейковом DOM.
 *
 *   node --test tests/
 */

import test from 'node:test';
import assert from 'node:assert/strict';

import { installFakeDom, uninstallFakeDom, FakeElement } from './fake-dom.mjs';
import { suggestFromEdit, offerLearning } from '../web/js/dictation-learn.js';
import {
  applyReplacements, describeStatus, deviceRu, normalizeDictionary, validateDictionaryFile,
} from '../web/js/dictation-dictionary.js';

/* ------------------------------------------------------------------ helpers */

if (typeof FakeElement.prototype.removeChild !== 'function') {
  FakeElement.prototype.removeChild = function removeChild(node) {
    const idx = this.childNodes.indexOf(node);
    if (idx !== -1) { this.childNodes.splice(idx, 1); node.parentNode = null; }
    return node;
  };
}

function walk(el, out = []) {
  for (const child of el.childNodes || []) {
    if (child.nodeType === 1) { out.push(child); walk(child, out); }
  }
  return out;
}

function byText(root, text) {
  return walk(root).find((e) => e.textContent.includes(text)) || null;
}

function buttonByText(root, text) {
  return walk(root).find((e) => String(e.tagName).toLowerCase() === 'button'
    && e.textContent.includes(text)) || null;
}

const flush = () => new Promise((resolve) => setTimeout(resolve, 0));

const pairs = (raw, final) => suggestFromEdit(raw, final).map((s) => [s.from, s.to]);

/* --------------------------------------------------- suggestFromEdit: имена */

test('тескфло → TaskFlow', () => {
  assert.deepEqual(pairs('отправить тескфло в работу', 'отправить TaskFlow в работу'),
    [['тескфло', 'TaskFlow']]);
});

test('диаген → Атлас', () => {
  assert.deepEqual(pairs('позвонить диагену завтра', 'позвонить Диогену завтра'),
    [['диагену', 'Диогену']]);
});

test('недре → Гранит', () => {
  assert.deepEqual(pairs('запрос недре по участку', 'запрос Гранит по участку'),
    [['недре', 'Гранит']]);
});

test('мотти → моти', () => {
  assert.deepEqual(pairs('встреча с мотти', 'встреча с моти'), [['мотти', 'моти']]);
});

test('несколько имён в одной фразе — по порядку', () => {
  assert.deepEqual(
    pairs('тескфло и диаген и мотти', 'TaskFlow и Атлас и моти'),
    [['тескфло', 'TaskFlow'], ['диаген', 'Атлас'], ['мотти', 'моти']],
  );
});

/* ------------------------------------------- suggestFromEdit: что не ошибка */

test('только регистр — не ошибка распознавания', () => {
  assert.deepEqual(pairs('егрюл и сбер', 'ЕГРЮЛ и Сбер'), []);
});

test('регистр целой фразы — не ошибка', () => {
  assert.deepEqual(pairs('Отправь запрос в гранит', 'отправь запрос в гранит'), []);
});

test('пунктуация не считается', () => {
  assert.deepEqual(pairs('моти, ты где?', 'моти ты где'), []);
});

test('ё и е не считаются разными', () => {
  assert.deepEqual(pairs('ёлка и елка', 'елка и ёлка'), []);
});

test('падежная форма — предложение, если правили именно слово', () => {
  assert.deepEqual(pairs('отправил запросу в недры', 'отправил запросом в недры'),
    [['запросу', 'запросом']]);
});

test('падежную форму не предлагаем, если слово не трогали', () => {
  // «запросу» осталось как было — правка была в другом месте.
  assert.deepEqual(pairs('отправил запросу в недры', 'отправил запросу в Гранит'),
    [['недры', 'Гранит']]);
});

test('идентичный текст — пусто', () => {
  assert.deepEqual(pairs('отправить тескфло', 'отправить тескфло'), []);
});

test('одно слово без изменений — пусто', () => {
  assert.deepEqual(pairs('Гранит', 'Гранит'), []);
});

/* ----------------------------------------------- suggestFromEdit: границы */

test('вставка слова не предлагается', () => {
  assert.deepEqual(pairs('отправь запрос недре', 'отправь запрос недре срочно'), []);
});

test('удаление слова не предлагается', () => {
  assert.deepEqual(pairs('отправь запрос недре срочно', 'отправь запрос недре'), []);
});

test('вставка рядом с настоящей правкой — только правка', () => {
  assert.deepEqual(pairs('нужно отправить тескфло', 'срочно нужно отправить TaskFlow'),
    [['тескфло', 'TaskFlow']]);
});

test('одиночный предлог короче трёх букв не учим', () => {
  assert.deepEqual(pairs('и я пошёл в дом', 'и я пошёл во дом'), []);
});

test('два коротких служебных слова не учим', () => {
  assert.deepEqual(pairs('сказал и ушёл', 'сказал а ушёл'), []);
});

test('замена двух слов одним', () => {
  assert.deepEqual(pairs('это теск флоу для тебя', 'это TaskFlow для тебя'),
    [['теск флоу', 'TaskFlow']]);
});

test('замена одного слова двумя', () => {
  assert.deepEqual(pairs('позови диагена', 'позови Дио гена'),
    [['диагена', 'Дио гена']]);
});

test('кусок длиннее трёх слов не предлагаем', () => {
  assert.deepEqual(
    pairs('надо было сразу отправить запрос в недры',
      'надо было быстро передать все бумаги и папки'),
    [],
  );
});

test('почти полностью переписанный текст — пусто', () => {
  assert.deepEqual(
    pairs('сегодня надо позвонить в недры и уточнить статус по егрюл',
      'завтра нужно написать письмо диогену про отчёт за квартал'),
    [],
  );
});

test('переписанный наполовину текст ещё даёт правку', () => {
  const got = pairs('надо позвонить в недры и уточнить статус',
    'надо позвонить в Гранит и уточнить статус');
  assert.deepEqual(got, [['недры', 'Гранит']]);
});

/* --------------------------------------------------- suggestFromEdit: ещё */

test('пустой продиктованный текст — пусто', () => {
  assert.deepEqual(pairs('', 'привет'), []);
});

test('пустой отправленный текст — пусто', () => {
  assert.deepEqual(pairs('привет', ''), []);
});

test('мусор вместо текста — пусто', () => {
  assert.deepEqual(pairs(null, undefined), []);
  assert.deepEqual(pairs(42, 'сорок два'), []);
});

test('числа сравниваются как слова', () => {
  assert.deepEqual(pairs('договор от 2026 года', 'договор от 2027 года'),
    [['2026', '2027']]);
});

test('дефис внутри слова — часть слова', () => {
  assert.deepEqual(pairs('это теск-флоу', 'это TaskFlow'), [['теск-флоу', 'TaskFlow']]);
});

test('английские слова тоже ловятся', () => {
  assert.deepEqual(pairs('open the тэскфло file', 'open the TaskFlow file'),
    [['тэскфло', 'TaskFlow']]);
});

test('точки и тире вокруг слов не мешают', () => {
  assert.deepEqual(pairs('— тескфло, — сказал он.', '— TaskFlow, — сказал он.'),
    [['тескфло', 'TaskFlow']]);
});

test('три правки в короткой фразе — все три', () => {
  assert.deepEqual(
    pairs('один двп три четыри пять шезть',
      'один два три четыре пять шесть'),
    [['двп', 'два'], ['четыри', 'четыре'], ['шезть', 'шесть']],
  );
});

test('не больше пяти предложений за раз', () => {
  const raw = 'альфа двп бета четыри гамма шезть дельта восем эпсилон десить дзета дви';
  const final = 'альфа два бета четыре гамма шесть дельта восемь эпсилон десять дзета два';
  const got = suggestFromEdit(raw, final);
  assert.equal(got.length, 5);
  assert.deepEqual(got.map((s) => s.from), ['двп', 'четыри', 'шезть', 'восем', 'десить']);
});

test('повторы одной пары не дублируются', () => {
  const got = pairs('тескфло и тескфло', 'TaskFlow и TaskFlow');
  assert.deepEqual(got, [['тескфло', 'TaskFlow']]);
});

test('в from/to попадают слова как их написали', () => {
  const got = suggestFromEdit('Запрос НЕДРЕ готов', 'Запрос Гранит готов');
  assert.deepEqual(got, [{ from: 'НЕДРЕ', to: 'Гранит' }]);
});

test('результат — всегда массив объектов from/to', () => {
  const got = suggestFromEdit('тескфло', 'TaskFlow');
  assert.equal(Array.isArray(got), true);
  assert.deepEqual(Object.keys(got[0]), ['from', 'to']);
  assert.equal(typeof got[0].from, 'string');
  assert.equal(typeof got[0].to, 'string');
});

/* ------------------------------------------------------------ offerLearning */

function withDom(fn) {
  return async () => {
    fakeDoc = installFakeDom();
    fakeDoc.body = fakeDoc.createElement('div');
    try { await fn(); } finally { uninstallFakeDom(); }
  };
}

let fakeDoc = null;
test.beforeEach(() => { fakeDoc = installFakeDom(); });

test('плашка не появляется, если править нечего', withDom(async () => {
  const host = fakeDoc.createElement('div');
  const res = offerLearning({ toast: () => {} }, { raw: 'гранит', final: 'Гранит', anchor: host });
  assert.equal(res, null);
  assert.equal(host.childNodes.length, 0);
}));

test('одна правка: текст, кнопки и подпись', withDom(async () => {
  const host = fakeDoc.createElement('div');
  const res = offerLearning({ toast: () => {} },
    { raw: 'встреча с мотти', final: 'встреча с моти', anchor: host, ttlMs: 0 });
  assert.ok(res && res.el);
  assert.equal(host.childNodes.length, 1);
  const text = res.el.textContent;
  assert.match(text, /Запомнить исправление\?/);
  assert.match(text, /«мотти»/);
  assert.match(text, /«моти»/);
  assert.ok(buttonByText(res.el, 'Запомнить'));
  assert.ok(buttonByText(res.el, 'Не надо'));
  // «Запомнить все» остаётся в наборе даже для одной пары — обещано в макете.
  assert.ok(buttonByText(res.el, 'Запомнить все'));
  assert.match(text, /Whisper не обучается/);
  // не диалог: никакой подложки и перехвата
  assert.equal(fakeDoc.querySelector ? fakeDoc.querySelector('.dialog-backdrop') : null, null);
  res.close();
}));

test('несколько правок: список с галочками', withDom(async () => {
  const host = fakeDoc.createElement('div');
  const res = offerLearning({ toast: () => {} },
    { raw: 'недры и теск флоу', final: 'Гранит и TaskFlow', anchor: host, ttlMs: 0 });
  assert.ok(res);
  const boxes = walk(res.el).filter((e) => String(e.tagName).toLowerCase() === 'input');
  assert.equal(boxes.length, 2);
  assert.ok(boxes.every((b) => b.checked === true));
  assert.match(res.el.textContent, /Запомнить исправления\?/);
  assert.ok(buttonByText(res.el, 'Запомнить выбранные'));
  assert.ok(buttonByText(res.el, 'Запомнить все'));
  res.close();
}));

test('закрытие убирает плашку из разметки', withDom(async () => {
  const host = fakeDoc.createElement('div');
  const res = offerLearning({ toast: () => {} },
    { raw: 'отправить тескфло', final: 'отправить TaskFlow', anchor: host, ttlMs: 0 });
  assert.ok(res);
  assert.equal(host.childNodes.length, 1);
  res.close();
  assert.equal(host.childNodes.length, 0);
}));

test('ту же пару за сессию второй раз не предлагаем', withDom(async () => {
  const host = fakeDoc.createElement('div');
  const first = offerLearning({ toast: () => {} },
    { raw: 'снова про телеграм', final: 'снова про Telegram', anchor: host, ttlMs: 0 });
  assert.ok(first);
  first.close();
  const second = offerLearning({ toast: () => {} },
    { raw: 'снова про телеграм', final: 'снова про Telegram', anchor: host, ttlMs: 0 });
  assert.equal(second, null);
}));

test('«Не надо» — отказ до конца сессии', withDom(async () => {
  const host = fakeDoc.createElement('div');
  const res = offerLearning({ toast: () => {} },
    { raw: 'позвонить диагену', final: 'позвонить Диогену', anchor: host, ttlMs: 0 });
  assert.ok(res);
  buttonByText(res.el, 'Не надо').dispatch('click');
  assert.equal(host.childNodes.length, 0);
  const again = offerLearning({ toast: () => {} },
    { raw: 'позвонить диагену', final: 'позвонить Диогену', anchor: host, ttlMs: 0 });
  assert.equal(again, null);
}));

test('«Запомнить» отправляет raw/final с apply:true и говорит тост', withDom(async () => {
  const calls = [];
  const toasts = [];
  const host = fakeDoc.createElement('div');
  const res = offerLearning({
    api: { post: (path, body) => { calls.push([path, body]); return Promise.resolve({ saved: [] }); } },
    toast: (msg, kind) => toasts.push([msg, kind]),
  }, { raw: 'запрос недре по участку', final: 'запрос Гранит по участку', anchor: host, ttlMs: 0 });
  assert.ok(res);
  buttonByText(res.el, 'Запомнить').dispatch('click');
  await flush();
  assert.equal(calls.length, 1);
  assert.equal(calls[0][0], '/api/stt/learn');
  assert.equal(calls[0][1].apply, true);
  assert.equal(calls[0][1].raw, 'запрос недре по участку');
  assert.equal(calls[0][1].final, 'запрос Гранит по участку');
  assert.deepEqual(toasts, [['Запомнил. В следующий раз исправлю сам.', 'ok']]);
  assert.equal(host.childNodes.length, 0);
}));

test('ошибка сети — человеческий текст и «Повторить»', withDom(async () => {
  const host = fakeDoc.createElement('div');
  const fail = { code: 'network', message: 'Нет связи с приложением' };
  const res = offerLearning({
    api: { post: () => Promise.reject(fail) },
    toast: () => {},
  }, { raw: 'отчёт для диагена', final: 'отчёт для Диогена', anchor: host, ttlMs: 0 });
  assert.ok(res);
  buttonByText(res.el, 'Запомнить').dispatch('click');
  await flush();
  const err = byText(res.el, 'Нет связи');
  assert.ok(err, 'текст ошибки появился');
  assert.match(err.textContent, /Текст остался как есть/);
  assert.ok(buttonByText(res.el, 'Повторить'));
  // плашка на месте — правка не потеряна
  assert.equal(host.childNodes.length, 1);
}));

test('плашка сама прячется по таймеру', withDom(async () => {
  const host = fakeDoc.createElement('div');
  const res = offerLearning({ toast: () => {} },
    { raw: 'созвон по битрикс', final: 'созвон по Битрикс24', anchor: host, ttlMs: 20 });
  assert.ok(res);
  assert.equal(host.childNodes.length, 1);
  await new Promise((r) => setTimeout(r, 60));
  assert.equal(host.childNodes.length, 0);
}));

test('якорь-поле: плашка встаёт рядом, а не внутрь input', withDom(async () => {
  const wrap = fakeDoc.createElement('div');
  const input = fakeDoc.createElement('input');
  wrap.appendChild(input);
  const res = offerLearning({ toast: () => {} },
    { raw: 'поле статус кворумма', final: 'поле статус кворума', anchor: input, ttlMs: 0 });
  assert.ok(res);
  assert.equal(wrap.childNodes.length, 2);
  assert.equal(input.childNodes.length, 0);
  res.close();
}));

test('без DOM плашка не строится и не падает', () => {
  uninstallFakeDom();
  const res = offerLearning({ toast: () => {} }, { raw: 'тескфло', final: 'TaskFlow' });
  assert.equal(res, null);
  fakeDoc = installFakeDom();
});


/* --------------------------------------------- словарь: чистые помощники */

test('автозамена не зависит от регистра', () => {
  const res = applyReplacements('Тескфло и тескфло', [{ from: 'тескфло', to: 'TaskFlow' }]);
  assert.equal(res.text, 'TaskFlow и TaskFlow');
  assert.equal(res.count, 2);
});

test('автозамена работает по границам слов', () => {
  const res = applyReplacements('поднедре и недре', [{ from: 'недре', to: 'Гранит' }]);
  assert.equal(res.text, 'поднедре и Гранит');
  assert.equal(res.count, 1);
});

test('регистр первой буквы сохраняется', () => {
  const res = applyReplacements('недре прислал отчёт', [{ from: 'недре', to: 'гранит' }]);
  assert.equal(res.text, 'гранит прислал отчёт');
  const up = applyReplacements('Недре прислал отчёт', [{ from: 'недре', to: 'гранит' }]);
  assert.equal(up.text, 'Гранит прислал отчёт');
});

test('своё написание замены не ломается', () => {
  const res = applyReplacements('отправил в тескфло', [{ from: 'тескфло', to: 'TaskFlow' }]);
  assert.equal(res.text, 'отправил в TaskFlow');
});

test('автозамена из двух слов', () => {
  const res = applyReplacements('это теск  флоу для тебя', [{ from: 'теск флоу', to: 'TaskFlow' }]);
  assert.equal(res.text, 'это TaskFlow для тебя');
});

test('счётчик и список сработавших пар', () => {
  const res = applyReplacements('недре и тескфло', [
    { from: 'недре', to: 'Гранит' },
    { from: 'тескфло', to: 'TaskFlow' },
    { from: 'диаген', to: 'Атлас' },
  ]);
  assert.equal(res.count, 2);
  assert.deepEqual(res.hits.map((h) => h.from), ['недре', 'тескфло']);
});

test('пустой список замен ничего не портит', () => {
  assert.equal(applyReplacements('привет', []).text, 'привет');
  assert.equal(applyReplacements('привет', null).count, 0);
});

test('словарь с сервера чистится и не дублируется', () => {
  const dict = normalizeDictionary({
    terms: ['Гранит', ' гранит ', '', 'Атлас', 'x'.repeat(80)],
    replacements: [
      { from: 'тескфло', to: 'TaskFlow', count: 3 },
      { from: 'Тескфло', to: 'другое' },
      { from: 'пусто', to: '' },
      { from: 'егрюл', to: 'ЕГРЮЛ' },
    ],
    updated_at: 1761600000,
  });
  assert.deepEqual(dict.terms, ['Гранит', 'Атлас']);
  assert.deepEqual(dict.replacements.map((r) => r.from), ['тескфло', 'егрюл']);
  assert.equal(dict.replacements[0].count, 3);
  assert.equal(dict.updated_at, 1761600000);
});

test('файл не словарь — понятная причина', () => {
  assert.equal(validateDictionaryFile(null).ok, false);
  assert.equal(validateDictionaryFile([1, 2]).ok, false);
  assert.match(validateDictionaryFile({}).error, /нет ни слов/);
  assert.match(validateDictionaryFile({ terms: [42] }).error, /не текст/);
  assert.match(validateDictionaryFile({ replacements: [{ from: 'а' }] }).error, /без «как распознал»/);
});

test('правильный файл проходит проверку', () => {
  const res = validateDictionaryFile({
    terms: ['Гранит'],
    replacements: [{ from: 'недре', to: 'Гранит' }],
  });
  assert.equal(res.ok, true);
  assert.deepEqual(res.value.terms, ['Гранит']);
});

test('состояние распознавания — человеческими словами', () => {
  assert.equal(describeStatus({ available: false }).label, 'не запущено');
  assert.equal(describeStatus({ loading: true }).label, 'загружается');
  assert.equal(describeStatus({ loaded: true }).label, 'готово');
  assert.equal(describeStatus({}).label, 'не загружено');
  assert.equal(describeStatus(null).tone, 'chip--muted');
});

test('устройство называется по-русски', () => {
  assert.equal(deviceRu('cuda'), 'на видеокарте');
  assert.equal(deviceRu('CPU'), 'на процессоре');
  assert.equal(deviceRu(''), '—');
});


test('якорь-диапазон вставки: плашка идёт к активному полю', withDom(async () => {
  const wrap = fakeDoc.createElement('div');
  const area = fakeDoc.createElement('textarea');
  wrap.appendChild(area);
  fakeDoc.body.appendChild(wrap);
  fakeDoc.activeElement = area;
  const res = offerLearning({ toast: () => {} },
    { raw: 'правка про статут', final: 'правка про статус', anchor: { start: 3, end: 9, text: 'статут' }, ttlMs: 0 });
  assert.ok(res);
  assert.equal(wrap.childNodes.length, 2, 'плашка встала рядом с полем');
  assert.equal(area.childNodes.length, 0);
  res.close();
}));

test('якорь-объект с полем: берём его узел', withDom(async () => {
  const wrap = fakeDoc.createElement('div');
  const res = offerLearning({ toast: () => {} },
    { raw: 'ещё про тескфлоу', final: 'ещё про TaskFlow', anchor: { el: wrap }, ttlMs: 0 });
  assert.ok(res);
  assert.equal(wrap.childNodes.length, 1);
  res.close();
}));

test('без якоря и без фокуса плашка встаёт вниз экрана', withDom(async () => {
  const res = offerLearning({ toast: () => {} },
    { raw: 'низ про ноушен', final: 'низ про Notion', ttlMs: 0 });
  assert.ok(res);
  assert.match(String(res.el.className), /dl-plate--fixed/);
  assert.equal(fakeDoc.body.childNodes.length, 1);
  res.close();
}));
