/**
 * found.test.mjs — W26-D4: кликабельные чипы-ссылки и блок «Найдено».
 *
 * Проверяем:
 *  * разбор машинного блока ```найдено (YAML-список, JSON-массив, обёртка с
 *    «искали/где искали»), русские и английские ключи, метки отношения;
 *  * рендер: группы «Нашли то, что искали / Нашли упоминания / Нашли смежное /
 *    Похожее, но не то», счётчики, честная плашка «Искомого нет; нашли смежное»;
 *  * действия по типу: файл — Открыть / Показать в папке / Копировать путь;
 *    сайт — Перейти на сайт / Копировать ссылку; письмо — Открыть письмо;
 *  * запасной разбор обычного текста: пути и адреса из раздела «Найдено» → карточки;
 *  * чипы в своде: путь → чип, адрес → чип, «не найден» не рисует ложных кнопок;
 *  * свойства: детерминизм, идемпотентность, экранирование (нет сырого HTML);
 *  * общий вид: CSS использует токены темы (обе темы), узкий экран, печать;
 *  * фикстуры found_*.md|json разбираются.
 */

import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

import {
  parseFound, renderFoundBlock, renderFoundFromText, foundSection,
  fileChipHtml, urlChipHtml, relationKind, typeKind, isAbsolutePath, FOUND_GROUPS,
} from '../web/js/found.js';
import { formatSvod, svodPlainText } from '../web/js/svod-format.js';
import { renderMarkdown, mdInline } from '../web/js/md.js';

const HERE = dirname(fileURLToPath(import.meta.url));
const FIX = join(HERE, '..', 'fixtures');
const fixture = (name) => readFileSync(join(FIX, name), 'utf8');
const CSS = readFileSync(join(HERE, '..', 'web', 'css', 'found.css'), 'utf8');

/* ── разбор ─────────────────────────────────────────────────────────────── */

test('YAML-список: ключи, отношение, тип', () => {
  const data = parseFound(fixture('found_files.md'));
  assert.equal(data.items.length, 3);
  assert.equal(data.items[0].title, 'Годовой план работ');
  assert.equal(data.items[0].type, 'file');
  assert.equal(data.items[0].relation, 'exact');
  assert.match(data.items[0].where, /Годовой план\.md$/);
  assert.deepEqual(data.items.map((x) => x.relation), ['exact', 'adjacent', 'wrong']);
});

test('JSON-массив и английские ключи', () => {
  const data = parseFound(fixture('found_records.json'));
  assert.equal(data.items.length, 2);
  assert.equal(data.items[0].relation, 'exact');
  assert.equal(data.items[1].type, 'record');
});

test('обёртка «искали / где искали» и пустой список', () => {
  const data = parseFound(fixture('found_empty.md'));
  assert.equal(data.items.length, 0);
  assert.match(data.query, /спецификация партий/);
  assert.equal(data.places.length, 3);
  const html = renderFoundBlock(fixture('found_empty.md'));
  assert.match(html, /Искомого нет/);
  assert.match(html, /искали в/);
  assert.doesNotMatch(html, /found-block__group--exact/, 'пустая группа не рисуется');
});

test('метки отношения и типы понимают синонимы и неизвестное', () => {
  assert.equal(relationKind('Искомое'), 'exact');
  assert.equal(relationKind('упоминает искомое'), 'mentions');
  assert.equal(relationKind('похожее, но не то'), 'wrong');
  assert.equal(relationKind('непонятно что'), 'adjacent');
  assert.equal(typeKind('', 'https://example.org/x'), 'site');
  assert.equal(typeKind('', 'mailto:a@b.c'), 'email');
  assert.equal(typeKind('', 'C:/a/b.md'), 'file');
  assert.equal(typeKind('письмо', ''), 'email');
});

/* ── рендер ─────────────────────────────────────────────────────────────── */

test('группы, счётчики и честная плашка', () => {
  const html = renderFoundBlock(fixture('found_files.md'));
  assert.match(html, /found-block__group--exact/);
  assert.match(html, /Нашли то, что искали <span class="found-block__group-count">\(1\)/);
  assert.match(html, /Нашли смежное <span class="found-block__group-count">\(1\)/);
  assert.match(html, /Похожее, но не то <span class="found-block__group-count">\(1\)/);
  assert.doesNotMatch(html, /Искомого нет/, 'искомое есть — плашки нет');

  const soft = renderFoundBlock(fixture('found_nothing.md'));
  assert.match(soft, /Искомого нет; нашли смежное/);
  assert.doesNotMatch(soft, /group--exact/);
});

test('действия по типу объекта', () => {
  const fileCard = renderFoundBlock(fixture('found_files.md'));
  assert.match(fileCard, /md-file-chip__act--open/);
  assert.match(fileCard, /md-file-chip__act--reveal/);
  assert.match(fileCard, /md-file-chip__act--copy/);
  assert.match(fileCard, /data-path="C:\/Работа\/Планы\/Годовой план\.md"/);

  const sites = renderFoundBlock(fixture('found_sites.md'));
  assert.match(sites, /Перейти на сайт/);
  assert.match(sites, /Копировать ссылку/);
  assert.match(sites, /Открыть письмо/);
});

test('запасной разбор обычного текста', () => {
  const text = 'Нашёл `C:/Работа/Планы/Годовой план.md` и страницу https://example.org/a.';
  const html = renderFoundFromText(text);
  assert.match(html, /found-card/);
  assert.match(html, /data-path="C:\/Работа\/Планы\/Годовой план\.md"/);
  assert.match(html, /https:\/\/example\.org\/a/);
  assert.equal(renderFoundFromText('ничего не нашлось'), '');
});

test('md.js рисует машинный блок карточками, а не кодом', () => {
  const html = renderMarkdown(fixture('found_files.md'));
  assert.match(html, /<div class="found-block"/);
  assert.doesNotMatch(html, /<pre class="md-code">/);
});

test('запасной разбор включается только когда машинного блока нет', () => {
  const md = '## Найдено\n\nИскал в папке проекта, нашёл `C:/Работа/Планы/Годовой план.md`.\n';
  const html = renderMarkdown(md);
  assert.match(html, /class="found-block"/);
  assert.match(html, /разобрано из текста/);
  const once = renderMarkdown(fixture('found_files.md'));
  assert.equal((once.match(/class="found-block"/g) || []).length, 1, 'ровно один блок');
});

test('foundSection находит раздел и не съедает следующий', () => {
  const md = '# Свод\n\n## Найдено\n\nA\n\n### Что дальше\n\nB\n';
  assert.equal(foundSection(md), 'A');
  assert.equal(foundSection('# Свод\n\nнет раздела'), '');
});

/* ── чипы в своде ──────────────────────────────────────────────────────── */

test('путь в своде → чип, адрес → ссылка-чип, текст не теряется', () => {
  const src = 'Макет: `C:/Работа/Проект Витрина/Макет витрины — финал.md`. Источник: https://example.org/x.';
  const out = formatSvod(src);
  assert.match(out, /\[C:\/Работа\/Проект Витрина\/Макет витрины — финал\.md\]\(<file:/);
  assert.match(out, /\[https:\/\/example\.org\/x\]\(https:\/\/example\.org\/x "tr-chip"\)/);
  assert.equal(svodPlainText(out), svodPlainText(src), 'ничего не пропало');
  const html = renderMarkdown(out);
  assert.match(html, /md-file-chip/);
  assert.match(html, /md-url-chip/);
  assert.match(html, /Копировать путь/);
  assert.match(html, /Копировать/);
});

test('чип файла: имя, путь и действия; «не найден» — без кнопок', () => {
  const chip = fileChipHtml({ path: 'C:/Папка/Отчёт за год.docx' });
  assert.match(chip, /md-file-chip__name[^>]*>Отчёт за год\.docx</, 'имя файла, а не весь путь');
  assert.match(chip, /data-path="C:\/Папка\/Отчёт за год\.docx"/);
  assert.equal((chip.match(/__act--/g) || []).length, 3);

  const missing = fileChipHtml({ path: 'C:/Нет/файла.md', missing: true });
  assert.match(missing, /md-file-chip--missing/);
  assert.match(missing, /не найден/);
  assert.doesNotMatch(missing, /__act--/, 'ложных кнопок нет');
});

test('чип адреса: переход и копирование', () => {
  const chip = urlChipHtml({ url: 'https://example.org/a', label: 'example.org' });
  assert.match(chip, /href="https:\/\/example\.org\/a"/);
  assert.match(chip, /target="_blank"/);
  assert.match(chip, /Копировать/);
});

test('абсолютный путь определяем строго', () => {
  assert.equal(isAbsolutePath('C:\\a\\b.md'), true);
  assert.equal(isAbsolutePath('C:/a/b.md'), true);
  assert.equal(isAbsolutePath('/var/log/a.log'), true);
  assert.equal(isAbsolutePath('\\\\server\\share\\a.md'), true);
  assert.equal(isAbsolutePath('./docs/a.md'), false);
  assert.equal(isAbsolutePath('#якорь'), false);
});

/* ── безопасность и свойства ───────────────────────────────────────────── */

test('данные экранируются: HTML из блока не исполняется', () => {
  const evil = '- название: <img src=x onerror=alert(1)>\n  где: C:/a/b.md\n  описание: <script>alert(1)</script>\n';
  const html = renderFoundBlock(evil);
  assert.doesNotMatch(html, /<img/);
  assert.doesNotMatch(html, /<script/);
  assert.match(html, /&lt;script&gt;/);
});

test('рендер детерминирован и не зависит от порядка вызовов', () => {
  const src = fixture('found_files.md');
  assert.equal(renderFoundBlock(src), renderFoundBlock(src));
  assert.equal(renderFoundBlock(fixture('found_sites.md')), renderFoundBlock(fixture('found_sites.md')));
});

test('FOUND_GROUPS: четыре группы в человеческом порядке', () => {
  assert.deepEqual(FOUND_GROUPS.map((g) => g[0]), ['exact', 'mentions', 'adjacent', 'wrong']);
});

/* ── общий вид ─────────────────────────────────────────────────────────── */

test('CSS: токены темы, узкий экран, печать, без фиксированных цветов фона', () => {
  assert.match(CSS, /md-file-chip/);
  assert.match(CSS, /found-card/);
  assert.match(CSS, /--muted-foreground/, 'цвета из токенов — обе темы');
  assert.match(CSS, /@media \(max-width: 720px\)/);
  assert.match(CSS, /@media print/);
  assert.doesNotMatch(CSS, /background:\s*#fff;?\s*\n[^}]*\.found-card--exact/);
});
