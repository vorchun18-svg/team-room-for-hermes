/**
 * tests/no-request-word.test.mjs — тест-страж W30-R1: слово «запрос» ушло из интерфейса.
 *
 * Михаил сказал прямо: «Запрос» и «Задача» — одно и то же, путаница мешает; на экране
 * везде «задача». Тест смотрит на ВИДИМЫЕ строки (строковые литералы и тексты HTML),
 * а не на код: идентификаторы `request`, `/api/requests`, ключи JSON, имена файлов и
 * CSS-классы остаются как были — их человек не видит.
 *
 * Исключения — только там, где «запрос» значит НЕ задачу. Каждое исключение живёт
 * здесь с обоснованием: список короткий и проверяется глазами при ревью.
 */

import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync, readdirSync, statSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

const here = dirname(fileURLToPath(import.meta.url));
const root = join(here, '..');

/**
 * Исключения: «запрос», который не является задачей.
 *
 *  • «запрос разрешения», «запросы разрешений» — просьба к Hermes разрешить команду
 *    (approvals): это не задача комнаты, а пункт согласования;
 *  • «поисковый запрос», «Измените запрос» — строка поиска, а не работа команды;
 *  • «Запрос доступа к микрофону» — разрешение браузера/ОС;
 *  • «Запрос уходит на модель» — обращение к модели, не задача;
 *  • «Запросить список моделей» — глагол, не существительное;
 *  • «внешних запросов нет» — сетевые обращения наружу (безопасность);
 *  • «папка «Запросы»» — имя папки на диске (путь, менять нельзя);
 *  • «запросы разрешений агентам» — то же согласование, что выше.
 */
const ALLOWED = [
  { re: /запрос[а-яё]*\s+разрешени/i, why: 'согласование команды (approvals), не задача комнаты' },
  { re: /разрешений по этой карточке/i, why: 'согласование команды (approvals), не задача комнаты' },
  { re: /разрешений агентам/i, why: 'согласование команды (approvals), не задача комнаты' },
  { re: /поисков[а-яё]*\s+запрос/i, why: 'строка поиска, не работа команды' },
  { re: /измените (фильтр или )?запрос/i, why: 'строка поиска, не работа команды' },
  { re: /что искать: запрос/i, why: 'строка поиска, не работа команды' },
  { re: /запросы от двух символов/i, why: 'длина поискового запроса' },
  { re: /запрос доступа/i, why: 'разрешение микрофона (ОС/браузер)' },
  { re: /запрос уходит/i, why: 'обращение к модели, не задача' },
  { re: /уйдёт запрос/i, why: 'обращение к модели, не задача' },
  { re: /запросить список моделей/i, why: 'глагол «запросить», не существительное' },
  { re: /внешних запросов/i, why: 'сетевые обращения наружу (безопасность)' },
  { re: /папке «Запросы»/i, why: 'имя папки на диске — путь, менять нельзя' },
];

/** Все .js интерфейса (web/js рекурсивно) + web/index.html. */
function uiFiles(dir = join(root, 'web', 'js'), out = []) {
  for (const name of readdirSync(dir)) {
    const p = join(dir, name);
    if (statSync(p).isDirectory()) uiFiles(p, out);
    else if (name.endsWith('.js')) out.push(p);
  }
  return out;
}

const FILES = [...uiFiles(), join(root, 'web', 'index.html')];

/** Строковые литералы строки кода (одинарные, двойные, шаблонные). */
function literals(line) {
  const out = [];
  const re = /'(?:[^'\\]|\\.)*'|"(?:[^"\\]|\\.)*"|`(?:[^`\\]|\\.)*`/g;
  let m;
  while ((m = re.exec(line)) !== null) out.push(m[0]);
  return out;
}

function isComment(line) {
  const s = line.trim();
  return s.startsWith('//') || s.startsWith('*') || s.startsWith('/*') || s.startsWith('<!--');
}

/** Видимые тексты файла: литералы в коде и текст HTML (без тегов и атрибутов-ключей). */
function visibleChunks(file, src) {
  const chunks = [];
  if (file.endsWith('.html')) {
    const noComments = src.replace(/<!--[\s\S]*?-->/g, ' ');
    for (const m of noComments.matchAll(/>([^<>]+)</g)) chunks.push(m[1]);
    for (const m of noComments.matchAll(/(?:content|title|placeholder|aria-label)="([^"]*)"/g)) chunks.push(m[1]);
    return chunks;
  }
  for (const line of src.split('\n')) {
    if (isComment(line)) continue;
    const code = line.replace(/\/\/.*$/, '');
    for (const lit of literals(code)) chunks.push(lit);
  }
  return chunks;
}

test('в видимых строках интерфейса нет слова «запрос» (кроме списка исключений)', () => {
  const offenders = [];
  for (const file of FILES) {
    const src = readFileSync(file, 'utf8');
    if (!/запрос/i.test(src)) continue;
    for (const chunk of visibleChunks(file, src)) {
      if (!/запрос/i.test(chunk)) continue;
      if (ALLOWED.some((a) => a.re.test(chunk))) continue;
      offenders.push(`${file.slice(root.length + 1)}: ${chunk.trim().slice(0, 160)}`);
    }
  }
  assert.deepEqual(offenders, [], 'слово «запрос» осталось в видимых строках:\n' + offenders.join('\n'));
});

test('вкладка композера называется «Новая задача», охват — «Эта задача»', async () => {
  const { TAB_TITLES, SCOPE_LABELS } = await import('../web/js/text-ru.js');
  assert.equal(TAB_TITLES.composer, 'Новая задача');
  assert.equal(SCOPE_LABELS.request, 'Эта задача');
});

test('«Задачи без дела» — так называется блок запросов без дела', () => {
  const src = readFileSync(join(root, 'web', 'js', 'views', 'delo.js'), 'utf8');
  assert.ok(src.includes("'Задачи без дела'"), 'заголовок блока переименован');
  assert.ok(!src.includes("'Запросы без дела'"), 'старого заголовка нет');
});
