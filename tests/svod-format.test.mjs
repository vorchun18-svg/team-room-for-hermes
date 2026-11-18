/**
 * svod-format.test.mjs — W24-N1: «Свод» читается человеком.
 *
 * Что проверяем:
 *  * свойства нормализатора на 10 вымышленных фикстурах: детерминированность,
 *    идемпотентность, отсутствие потери текста (сравнение «голого» текста);
 *  * инлайн-перечни «1) … 2) … 3) …» и «а) б) в)» становятся списками;
 *  * длинные абзацы режутся по границам предложений и не режутся внутри
 *    сокращений («т.е.», «г.»), инициалов, десятичных дробей, путей и кода;
 *  * метки ИТОГ / ВЫВОД / НЕ НАЙДЕНО / НЕ ПРОВЕРЕНО / ВАЖНО → плашки, и рендерер
 *    `md.js` рисует их как `blockquote.md-callout`;
 *  * пути Windows/Unix и ссылки → `code`; уже код и блоки кода не трогаются;
 *  * готовый хороший свод не меняется (идемпотентность «на глаз»);
 *  * «Коротко»: metadata.svod.headline, первый абзац ≤ 240, первое предложение;
 *  * три свойства на случайных вставках (детерминизм/идемпотентность/текст);
 *  * CSS свода: колонка чтения, кегль, интервал, плашки, печать, тёмная тема.
 */

import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

import {
  formatSvod, svodPlainText, svodHeadline, svodParts, wrapPaths,
  splitSentences, inlineEnumeration, formatParagraph, matchCalloutLabel,
  SVOD_HEADLINE_MAX, SVOD_MAX_PARAGRAPH,
} from '../web/js/svod-format.js';
import { renderMarkdown, calloutKind } from '../web/js/md.js';

const HERE = dirname(fileURLToPath(import.meta.url));
const FIX = join(HERE, '..', 'fixtures');
const fixture = (name) => readFileSync(join(FIX, name), 'utf8');
const svodFiles = [
  'svod_wall.md', 'svod_inline.md', 'svod_paths.md', 'svod_labels.md', 'svod_good.md',
  'svod_table.md', 'svod_code.md', 'svod_mixed.md', 'svod_abbr.md', 'svod_empty.md',
];

/* ── свойства на всех фикстурах ────────────────────────────────────────── */

test('фикстуры: идемпотентность и отсутствие потери текста', () => {
  for (const name of svodFiles) {
    const src = fixture(name);
    const once = formatSvod(src);
    assert.equal(formatSvod(once), once, `${name}: второй прогон изменил текст`);
    assert.equal(formatSvod(src), once, `${name}: результат не детерминирован`);
    assert.equal(svodPlainText(once), svodPlainText(src), `${name}: потерян текст`);
  }
});

test('фикстуры: ни один абзац не длиннее 450 знаков (кроме одиночного предложения)', () => {
  for (const name of svodFiles) {
    const out = formatSvod(fixture(name));
    for (const block of out.split('\n\n')) {
      // W26-D4: мерим «видимую» длину — адрес ссылки (`[путь](<file:путь>)`) не
      // считается: путь стоит в метке, а дублирующий адрес — разметка, не текст.
      const one = block.replace(/\n/g, ' ').replace(/!?\[([^\]\n]*)\]\([^)\n]*\)/g, '$1').trim();
      if (!one || one.startsWith('#') || one.startsWith('> [!') || /^[-\d]/.test(one) || one.startsWith('|') || one.startsWith('```')) continue;
      assert.ok(one.length <= SVOD_MAX_PARAGRAPH, `${name}: абзац ${one.length} знаков — ${one.slice(0, 60)}…`);
    }
  }
});

test('хороший свод не меняется ни на знак', () => {
  const src = fixture('svod_good.md');
  assert.equal(formatSvod(src), src);
});

test('пустой свод остаётся пустым', () => {
  assert.equal(formatSvod(''), '');
  assert.equal(formatSvod(null), '');
  assert.equal(formatSvod('   \n\n '), '   \n\n ');
});

/* ── (а) инлайн-перечни ────────────────────────────────────────────────── */

test('перечень «1) 2) 3)» внутри строки становится нумерованным списком', () => {
  const out = formatSvod(fixture('svod_inline.md'));
  assert.match(out, /1\. в разделе о расходах не сходятся итоги на 12 тыс\. рублей;/);
  assert.match(out, /2\. подпись ответственного стоит на копии, а не на оригинале;/);
  assert.match(out, /3\. приложение с расшифровкой не приложено к электронной версии\./);
  // вступление осталось абзацем перед списком
  assert.match(out, /три замечания:\n\n1\. /);
});

test('перечень «а) б) в)» становится маркированным списком', () => {
  const out = formatSvod(fixture('svod_inline.md'));
  assert.match(out, /- ежемесячная отчётность вместо квартальной;/);
  assert.match(out, /- оплата по факту вместо аванса;/);
  assert.match(out, /- хранение документов на нашей стороне\./);
});

test('менее трёх пунктов — перечень не трогаем', () => {
  const text = 'Нужно две вещи: 1) подписать акт; 2) приложить счёт.';
  assert.equal(inlineEnumeration(text), null);
  assert.equal(formatSvod(text), text);
});

test('перечень продолжается после списка и не съедает следующую прозу', () => {
  const [enumeration] = [inlineEnumeration('Итоги: 1) да; 2) нет; 3) не знаю. Дальше обычный текст.')];
  assert.equal(enumeration.items.length, 3);
  const out = formatSvod('Итоги: 1) да; 2) нет; 3) не знаю. Дальше обычный текст.');
  assert.match(out, /3\. не знаю\./);
  assert.match(out, /Дальше обычный текст\./);
});

/* ── (б) длинные абзацы ────────────────────────────────────────────────── */

test('длинный абзац режется по предложениям на 2–4 предложения', () => {
  const out = formatSvod(fixture('svod_wall.md'));
  const paras = out.split('\n\n').map((p) => p.trim()).filter(Boolean);
  assert.ok(paras.length >= 2, 'абзац не разрезан');
  for (const p of paras) {
    const sentences = splitSentences(p);
    assert.ok(sentences.length <= 4, `в абзаце ${sentences.length} предложений`);
    assert.ok(p.length <= SVOD_MAX_PARAGRAPH, `абзац ${p.length} знаков`);
  }
});

test('не режем внутри сокращений, инициалов, дробей, путей и кода', () => {
  const text = 'Средний чек вырос на 4,5 тыс. рублей, т.е. на 3 %. Ответственный — А. С. Это видно в таблице 3, стр. 14.';
  const sentences = splitSentences(text);
  // «т.е.» и «А. С.» предложение не кончают: две границы, а не четыре
  assert.equal(sentences.length, 2);
  assert.match(sentences[0], /т\.е\. на 3 %\.$/);
  assert.match(sentences[1], /А\. С\. Это видно в таблице 3, стр\. 14\.$/);

  const withPath = 'Смета лежит в C:/Работа/Проекты/Годовой отчёт/смета.xlsx. Её нужно приложить к письму.';
  assert.equal(splitSentences(withPath).length, 2);

  const withCode = 'Команда `npm run check --all` печатает итог. Это занимает минуту.';
  assert.equal(splitSentences(withCode).length, 2);
});

test('фикстура с сокращениями сохраняет три предложения в абзаце', () => {
  const out = formatSvod(fixture('svod_abbr.md'));
  assert.equal(out.trim(), fixture('svod_abbr.md').trim());
});

/* ── (в) метки-плашки ──────────────────────────────────────────────────── */

test('метки в начале абзаца становятся плашками', () => {
  const out = formatSvod(fixture('svod_labels.md'));
  assert.match(out, /^> \[!НЕ НАЙДЕНО\]\n> подтверждение оплаты/m);
  assert.match(out, /^> \[!ИТОГ\]\n> отчёт можно сдавать/m);
  assert.match(out, /^> \[!ВАЖНО\]\n> срок сдачи/m);
  assert.match(out, /^> \[!НЕ ПРОВЕРЕНО\]\n> суммы в приложении 2/m);
});

test('слово-метка без двоеточия — обычный текст', () => {
  assert.equal(matchCalloutLabel('Важно понимать, что сроки сдвинутся.'), null);
  const text = 'Важно понимать, что сроки сдвинутся. Это стоит проверить.';
  assert.equal(formatSvod(text), text);
});

test('метка посреди абзаца отделяется в свою плашку', () => {
  const out = formatSvod('Работа почти закончена. НЕ ПРОВЕРЕНО: суммы сверены по первой странице.');
  assert.match(out, /^Работа почти закончена\.$/m);
  assert.match(out, /^> \[!НЕ ПРОВЕРЕНО\]\n> суммы сверены/m);
});

test('рендерер рисует плашку, а не обычную цитату', () => {
  const html = renderMarkdown('> [!ИТОГ]\n> всё сходится');
  assert.match(html, /<blockquote class="md-callout md-callout--ok">/);
  assert.match(html, /<div class="md-callout__title">ИТОГ<\/div>/);
  assert.match(html, /<p>всё сходится<\/p>/);
  assert.equal(calloutKind('НЕ НАЙДЕНО'), 'muted');
  assert.equal(calloutKind('ВНИМАНИЕ'), 'warn');
  assert.equal(calloutKind('ВЫВОД'), 'ok');
  assert.equal(calloutKind('что-то ещё'), 'note');
  // обычная цитата остаётся цитатой
  assert.equal(renderMarkdown('> просто цитата'), '<blockquote><p>просто цитата</p></blockquote>');
});

test('плашка с несколькими абзацами рендерится без потери текста', () => {
  const html = renderMarkdown('> [!ВЫВОД]\n> первый\n>\n> второй');
  const paras = html.match(/<p>/g) || [];
  assert.equal(paras.length, 2);
  assert.match(html, /md-callout--ok/);
});

/* ── (г) пути и ссылки ─────────────────────────────────────────────────── */

test('пути Windows/Unix становятся чипами-ссылками, точки в конце — текст', () => {
  const out = formatSvod(fixture('svod_paths.md'));
  // W26-D4: путь в `<file:…>` — md.js рисует из него чип «Открыть · Показать в папке · Копировать»
  assert.match(out, /\[C:\\Работа\\Проекты\\Годовой отчёт\\смета_итог_v3\.xlsx\]\(<file:C:\\Работа[^>]*>\)/);
  assert.match(out, /\[C:\/Работа\/Проекты\/Годовой отчёт\/расшифровка\/расходы_по_статьям_январь_февраль\.csv\]\(<file:/);
  assert.match(out, /\[\/home\/studio\/проекты\/отчёт\/раздел_2_черновик\.md\]\(<file:/);
  // точка в конце предложения осталась текстом, а не частью пути
  assert.match(out, /\]\(<file:[^>]*\.\w+>\)\./);
  // ссылка остаётся кликабельной и помечена как чип (метка в title)
  const urls = out.match(/\[[^\]]*\]\(https:\/\/example\.org\/docs\/svod-format "tr-chip"\)/);
  assert.ok(urls, 'адрес должен стать ссылкой-чипом');
});

test('путь в обратных кавычках становится ссылкой один раз, блок кода не трогается', () => {
  const once = wrapPaths('Смета: `C:/Проекты/смета.xlsx` тут');
  assert.match(once, /^Смета: \[C:\/Проекты\/смета\.xlsx\]\(<file:C:\/Проекты\/смета\.xlsx>\) тут$/);
  assert.equal(wrapPaths(once), once, 'второй раз не оборачивается');
  const code = fixture('svod_code.md');
  const out = formatSvod(code);
  assert.match(out, /1\) проверить итоги/);
  assert.match(out, /путь: C:\\Работа\\Проекты\\Годовой отчёт\\смета\.xlsx/);
  assert.doesNotMatch(out, /`C:\\Работа/);
});

test('обычный слэш в словах не превращается в код', () => {
  assert.equal(wrapPaths('формат ввода/вывода и ещё и/или'), 'формат ввода/вывода и ещё и/или');
});

/* ── (д) готовые блоки не трогаем ──────────────────────────────────────── */

test('таблицы, списки и заголовки остаются как были', () => {
  const table = fixture('svod_table.md');
  assert.equal(formatSvod(table), table);
  const listy = '## Разделы\n\n- один\n- два\n- три\n';
  assert.equal(formatSvod(listy), listy);
});

/* ── «Коротко» ─────────────────────────────────────────────────────────── */

test('«Коротко» берёт metadata.svod.headline', () => {
  const head = svodHeadline(fixture('svod_wall.md'), { headline: 'Ремонт укладывается в 3,4 млн' });
  assert.deepEqual(head, { text: 'Ремонт укладывается в 3,4 млн', source: 'metadata' });
});

test('«Коротко» — первый абзац ≤ 240 знаков', () => {
  const head = svodHeadline('Короткий вывод по отчёту.\n\nДальше подробности.');
  assert.equal(head.text, 'Короткий вывод по отчёту.');
  assert.equal(head.source, 'paragraph');
});

test('«Коротко» не длиннее 240 знаков и не повторяется в тексте', () => {
  const long = 'А'.repeat(900) + ' конец.';
  const head = svodHeadline(long);
  assert.ok(head.text.length <= SVOD_HEADLINE_MAX);
  const parts = svodParts(fixture('svod_good.md'));
  assert.match(parts.headline, /Отчёт можно сдавать/);
  assert.doesNotMatch(parts.body, /Отчёт можно сдавать/);
  assert.doesNotMatch(parts.body, /## Коротко/);
  assert.match(parts.body, /## Что сделано/);
});

test('«Коротко» не появляется у пустого свода', () => {
  assert.equal(svodHeadline(''), null);
  assert.deepEqual(svodParts(''), { headline: '', body: '' });
});

/* ── свойства на случайных вставках ────────────────────────────────────── */

function rng(seed) {
  let s = seed >>> 0;
  return () => {
    s = (s * 1664525 + 1013904223) >>> 0;
    return s / 4294967296;
  };
}

const CHUNKS = [
  'Смета уточнена по последней версии договора.',
  'Нужно сверить итоги в разделе о расходах.',
  'Ответственный подтвердил срок до двадцатого марта.',
  'Файл лежит в C:\\Работа\\Проекты\\Годовой отчёт\\смета.xlsx.',
  'Ссылка на описание: https://example.org/docs/svod-format.',
  'НЕ ПРОВЕРЕНО: суммы сверены по первой странице.',
  'ИТОГ: отчёт можно сдавать после трёх правок.',
  'Приложение с расшифровкой приложим отдельно.',
  'Таблица по строкам сходится, расхождений нет.',
  'Это займёт примерно неделю работы.',
];

test('три свойства на случайных вставках (детерминизм, идемпотентность, текст)', () => {
  const rand = rng(20261118);
  for (let round = 0; round < 40; round += 1) {
    const n = 2 + Math.floor(rand() * 10);
    const parts = [];
    for (let k = 0; k < n; k += 1) parts.push(CHUNKS[Math.floor(rand() * CHUNKS.length)]);
    const src = parts.join(rand() > 0.5 ? ' ' : '\n\n');
    const once = formatSvod(src);
    assert.equal(formatSvod(src), once, `круг ${round}: не детерминировано`);
    assert.equal(formatSvod(once), once, `круг ${round}: не идемпотентно`);
    assert.equal(svodPlainText(once), svodPlainText(src), `круг ${round}: потерян текст`);
  }
});

test('formatParagraph — пустой абзац ничего не добавляет', () => {
  assert.deepEqual(formatParagraph(''), []);
  assert.deepEqual(formatParagraph('   '), []);
});
