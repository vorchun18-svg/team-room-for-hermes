import test from 'node:test';
import assert from 'node:assert/strict';

import { renderMarkdown, mdInline, mdToText } from '../web/js/md.js';

/* ------------------------------------------------------------------ headings */

test('headings # through ####', () => {
  assert.equal(renderMarkdown('# Заголовок'), '<h1>Заголовок</h1>');
  assert.equal(renderMarkdown('## Второй'), '<h2>Второй</h2>');
  assert.equal(renderMarkdown('### Третий'), '<h3>Третий</h3>');
  assert.equal(renderMarkdown('#### Четвёртый'), '<h4>Четвёртый</h4>');
  assert.equal(renderMarkdown('##### Пятый'), '<h4>Пятый</h4>');
  assert.equal(renderMarkdown('### Закрытый ###'), '<h3>Закрытый</h3>');
  // без пробела — не заголовок
  assert.equal(renderMarkdown('#хештег'), '<p>#хештег</p>');
});

test('paragraphs join lines and honour two-space breaks', () => {
  assert.equal(renderMarkdown('первая\nвторая'), '<p>первая вторая</p>');
  assert.equal(renderMarkdown('первая  \nвторая'), '<p>первая<br>вторая</p>');
  assert.equal(renderMarkdown('a\n\nb'), '<p>a</p>\n<p>b</p>');
  assert.equal(renderMarkdown(''), '');
  assert.equal(renderMarkdown(null), '');
  assert.equal(renderMarkdown(undefined), '');
});

/* -------------------------------------------------------------------- inline */

test('bold, italic, bold-italic, strikethrough', () => {
  assert.equal(mdInline('**жирный**'), '<strong>жирный</strong>');
  assert.equal(mdInline('*курсив*'), '<em>курсив</em>');
  assert.equal(mdInline('~~зачёркнутый~~'), '<del>зачёркнутый</del>');
  assert.equal(mdInline('***оба***'), '<strong><em>оба</em></strong>');
  assert.equal(mdInline('**жирный и *курсив***'), '<strong>жирный и <em>курсив</em></strong>');
  assert.equal(mdInline('*курсив **жирный** курсив*'), '<em>курсив <strong>жирный</strong> курсив</em>');
  assert.equal(mdInline('**a** и **b**'), '<strong>a</strong> и <strong>b</strong>');
  // непарные маркеры остаются текстом
  assert.equal(mdInline('2 * 3 = 6'), '2 * 3 = 6');
  assert.equal(mdInline('**незакрытый'), '**незакрытый');
  // snake_case не превращается в курсив
  assert.equal(mdInline('snake_case_name'), 'snake_case_name');
  assert.equal(mdInline('_курсив_'), '<em>курсив</em>');
});

test('underscores inside words never become emphasis (window.__xss, snake_case)', () => {
  // регрессия из e2e S9: «window.__xss» отрисовывался как «window.xss»
  assert.equal(mdInline('window.__xss'), 'window.__xss');
  assert.equal(mdInline('window.__xss=2 и window.__xss=3'), 'window.__xss=2 и window.__xss=3');
  assert.equal(mdInline('self.__dict__ и os.path.__file__'), 'self.__dict__ и os.path.__file__');
  assert.equal(mdInline('snake_case_name'), 'snake_case_name');
  assert.equal(mdInline('a_b_c'), 'a_b_c');
  assert.equal(mdInline('a__b__c'), 'a__b__c');
  assert.equal(mdInline('my_var и other_var'), 'my_var и other_var');
  assert.equal(mdInline('__init__.py'), '__init__.py', 'имя файла с расширением — одно слово');
  assert.equal(mdInline('открой __init__.py и __main__.py'), 'открой __init__.py и __main__.py');
  assert.equal(mdInline('/api/__health__/x'), '/api/__health__/x');
});

test('underscores: опенер после пробела/скобки работает, как раньше', () => {
  assert.equal(mdInline('_курсив_'), '<em>курсив</em>');
  assert.equal(mdInline('__жирный__'), '<strong>жирный</strong>');
  assert.equal(mdInline('слово __жирный__ слово'), 'слово <strong>жирный</strong> слово');
  assert.equal(mdInline('слово _курсив_ слово'), 'слово <em>курсив</em> слово');
  assert.equal(mdInline('(_курсив_)'), '(<em>курсив</em>)');
  assert.equal(mdInline('«__жирный__»'), '«<strong>жирный</strong>»');
  assert.equal(mdInline('_курсив_, потом __жирный__.'), '<em>курсив</em>, потом <strong>жирный</strong>.');
  assert.equal(mdInline('___оба___'), '<strong><em>оба</em></strong>');
  assert.equal(mdInline('__жирный _курсив___'), '<strong>жирный <em>курсив</em></strong>');
});

test('underscores: внутрисловное подчёркивание внутри настоящего выделения', () => {
  assert.equal(mdInline('_snake_case_ вот'), '<em>snake_case</em> вот');
  assert.equal(mdInline('__window.__xss__'), '<strong>window.__xss</strong>');
  assert.equal(mdInline('**window.__xss**'), '<strong>window.__xss</strong>');
  assert.equal(mdInline('*a_b_c*'), '<em>a_b_c</em>');
});

test('underscores: опенер с пробелом внутри или одиночный не выделяет', () => {
  assert.equal(mdInline('_ не курсив _'), '_ не курсив _');
  assert.equal(mdInline('__ не жирный __'), '__ не жирный __');
  assert.equal(mdInline('одиночный _ знак'), 'одиночный _ знак');
  assert.equal(mdInline('хвост _'), 'хвост _');
  assert.equal(mdInline('__'), '__');
  assert.equal(mdInline('___'), '___');
});

test('underscores: звёздочки ведут себя как раньше', () => {
  assert.equal(mdInline('**жирный**'), '<strong>жирный</strong>');
  assert.equal(mdInline('*курсив*'), '<em>курсив</em>');
  assert.equal(mdInline('сло**во**'), 'сло<strong>во</strong>');
  assert.equal(mdInline('a*b*c'), 'a<em>b</em>c');
  assert.equal(mdInline('2 * 3 = 6'), '2 * 3 = 6');
});

test('underscores: код и экранирование не затрагиваются', () => {
  assert.equal(mdInline('`window.__xss`'), '<code class="md-code-inline">window.__xss</code>');
  assert.equal(mdInline('`__init__` и `_x_`'), '<code class="md-code-inline">__init__</code> и <code class="md-code-inline">_x_</code>');
  assert.equal(mdInline('\\_не курсив\\_'), '_не курсив_');
  assert.equal(mdInline('`a` window.__xss `b`'), '<code class="md-code-inline">a</code> window.__xss <code class="md-code-inline">b</code>');
});

test('underscores: блоки — список, цитата, заголовок, таблица', () => {
  assert.equal(renderMarkdown('- window.__xss и __жирный__'), '<ul>\n<li>window.__xss и <strong>жирный</strong></li>\n</ul>');
  assert.equal(renderMarkdown('# window.__xss'), '<h1>window.__xss</h1>');
  assert.equal(renderMarkdown('> a_b_c'), '<blockquote><p>a_b_c</p></blockquote>');
  assert.match(renderMarkdown('| a | b |\n|---|---|\n| window.__xss | __x__ |'), /<td>window\.__xss<\/td>\s*<td><strong>x<\/strong><\/td>/);
  assert.equal(renderMarkdown('Ответ с window.__xss=5 и снова window.__xss=6.'), '<p>Ответ с window.__xss=5 и снова window.__xss=6.</p>');
});

test('underscores: текст агента с тегами (S9) сохраняет __ и экранирует теги', () => {
  const html = renderMarkdown('Ответ с <script>window.__xss=5</script> и <img src=x onerror=window.__xss=6>.');
  assert.ok(html.includes('window.__xss=5'), html);
  assert.ok(html.includes('window.__xss=6'), html);
  assert.ok(!/<script|<img/i.test(html), html);
});

test('mdToText сохраняет подчёркивания внутри слов', () => {
  assert.equal(mdToText('window.__xss=1 и snake_case'), 'window.__xss=1 и snake_case');
  assert.equal(mdToText('_курсив_ и __жирный__'), 'курсив и жирный');
});

test('inline code is escaped and never parsed', () => {
  assert.equal(mdInline('`<b>x</b>`'), '<code class="md-code-inline">&lt;b&gt;x&lt;/b&gt;</code>');
  assert.equal(mdInline('`**не жирный**`'), '<code class="md-code-inline">**не жирный**</code>');
  assert.equal(mdInline('``a ` b``'), '<code class="md-code-inline">a ` b</code>');
  assert.equal(mdInline('`&amp;`'), '<code class="md-code-inline">&amp;amp;</code>');
  assert.equal(mdInline('`незакрытый'), '`незакрытый');
});

test('escapes protect plain text', () => {
  assert.equal(mdInline('1 < 2 & 3 > 2'), '1 &lt; 2 &amp; 3 &gt; 2');
  assert.equal(mdInline('a "b" c \'d\''), 'a &quot;b&quot; c &#39;d&#39;');
  assert.equal(mdInline('\\*не курсив\\*'), '*не курсив*');
});

/* ----------------------------------------------------------------- code/quote */

test('fenced code blocks keep content verbatim and are escaped', () => {
  const md = '```js\nconst a = 1 < 2;\n```';
  assert.equal(renderMarkdown(md),
    '<pre class="md-code"><code class="language-js">const a = 1 &lt; 2;</code></pre>');
  assert.equal(renderMarkdown('```\nplain\n**not bold**\n```'),
    '<pre class="md-code"><code>plain\n**not bold**</code></pre>');
  // без языка
  assert.equal(renderMarkdown('```txt\nстрока\n```'),
    '<pre class="md-code"><code class="language-txt">строка</code></pre>');
  // экранирование внутри кода
  assert.equal(renderMarkdown('```\n<script>alert(1)</script>\n```'),
    '<pre class="md-code"><code>&lt;script&gt;alert(1)&lt;/script&gt;</code></pre>');
  // незакрытый fence не теряет содержимое
  assert.equal(renderMarkdown('```\nabc'), '<pre class="md-code"><code>abc</code></pre>');
  // язык санитизируется (info-строка режется до первого токена)
  assert.equal(renderMarkdown('```js" onload="x\ncode\n```'),
    '<pre class="md-code"><code class="language-js">code</code></pre>');
  assert.equal(renderMarkdown('```js title="a"\ncode\n```'),
    '<pre class="md-code"><code class="language-js">code</code></pre>');
});

test('blockquotes nest', () => {
  assert.equal(renderMarkdown('> цитата'), '<blockquote><p>цитата</p></blockquote>');
  assert.equal(renderMarkdown('> **важно**'), '<blockquote><p><strong>важно</strong></p></blockquote>');
  assert.equal(renderMarkdown('> внешняя\n> > вложенная'),
    '<blockquote><p>внешняя</p>\n<blockquote><p>вложенная</p></blockquote></blockquote>');
});

/* --------------------------------------------------------------------- lists */

test('unordered lists and nesting to 3 levels', () => {
  const md = '- один\n- два\n  - два.один\n    - два.один.один\n- три';
  const flat = renderMarkdown(md).replace(/\n/g, '');
  assert.equal(flat,
    '<ul><li>один</li><li>два<ul><li>два.один<ul><li>два.один.один</li></ul></li></ul></li><li>три</li></ul>');
});

test('ordered lists respect start numbers', () => {
  assert.equal(renderMarkdown('1. раз\n2. два'), '<ol>\n<li>раз</li>\n<li>два</li>\n</ol>');
  assert.equal(renderMarkdown('3. три\n4. четыре'), '<ol start="3">\n<li>три</li>\n<li>четыре</li>\n</ol>');
  // смена типа на том же уровне — новый список
  assert.equal(renderMarkdown('- a\n1. b'), '<ul>\n<li>a</li>\n</ul>\n<ol>\n<li>b</li>\n</ol>');
});

test('task list checkboxes are disabled inputs', () => {
  const html = renderMarkdown('- [ ] ждёт\n- [x] готово');
  assert.match(html, /<input type="checkbox" class="md-check" disabled> ждёт/);
  assert.match(html, /<input type="checkbox" class="md-check" disabled checked> готово/);
  assert.match(html, /class="md-task"/);
});

test('list items may contain paragraphs and code', () => {
  const html = renderMarkdown('- пункт с `кодом`\n- пункт с **жирным**');
  assert.equal(html, '<ul>\n<li>пункт с <code class="md-code-inline">кодом</code></li>\n<li>пункт с <strong>жирным</strong></li>\n</ul>');
});

/* -------------------------------------------------------------------- tables */

test('GFM tables with alignment', () => {
  const md = '| Имя | Роль | Сумма |\n|:---|:---:|---:|\n| Михаил | **заказчик** | 1 200 |\n| Аня | критик | 300 |';
  const html = renderMarkdown(md);
  assert.match(html, /^<table class="md-table"><thead><tr>/);
  assert.match(html, /<th style="text-align:left">Имя<\/th>/);
  assert.match(html, /<th style="text-align:center">Роль<\/th>/);
  assert.match(html, /<th style="text-align:right">Сумма<\/th>/);
  assert.match(html, /<td style="text-align:center"><strong>заказчик<\/strong><\/td>/);
  assert.match(html, /<tbody>/);
  assert.equal((html.match(/<tr>/g) || []).length, 3);
});

test('table cells escape pipes in code and pad missing cells', () => {
  const html = renderMarkdown('| a | b |\n|---|---|\n| `x\\|y` | 1 |\n| только одна |');
  assert.match(html, /<code class="md-code-inline">x\|y<\/code>/);
  assert.match(html, /<td>только одна<\/td><td><\/td>/);
});

test('a line of dashes alone is an hr, not a table', () => {
  assert.equal(renderMarkdown('текст\n\n---\n\nещё'), '<p>текст</p>\n<hr>\n<p>ещё</p>');
  assert.equal(renderMarkdown('***'), '<hr>');
  assert.equal(renderMarkdown('___'), '<hr>');
});

/* --------------------------------------------------------------------- links */

test('external links open in a new tab safely', () => {
  assert.equal(mdInline('[сайт](https://example.com)'),
    '<a href="https://example.com" target="_blank" rel="noopener noreferrer">сайт</a>');
  assert.equal(mdInline('[почта](mailto:a@b.c)'),
    '<a href="mailto:a@b.c" target="_blank" rel="noopener noreferrer">почта</a>');
  assert.equal(mdInline('[заголовок](https://e.com "Подсказка")'),
    '<a href="https://e.com" title="Подсказка" target="_blank" rel="noopener noreferrer">заголовок</a>');
});

test('local paths become data-path links', () => {
  assert.equal(mdInline('[отчёт](C:\\TeamRoom\\Vault\\a.md)'),
    '<a href="#" class="md-file" data-path="C:\\TeamRoom\\Vault\\a.md">отчёт</a>');
  assert.equal(mdInline('[лог](/var/log/app.log)'),
    '<a href="#" class="md-file" data-path="/var/log/app.log">лог</a>');
  assert.equal(mdInline('[файл](file:///C:/tmp/a.txt)'),
    '<a href="#" class="md-file" data-path="C:/tmp/a.txt">файл</a>');
  assert.equal(mdInline('[якорь](#section)'), '<a href="#section">якорь</a>');
});

test('autolinks bare http(s) urls', () => {
  assert.equal(mdInline('см. https://example.com/a?b=1 конец'),
    'см. <a href="https://example.com/a?b=1" target="_blank" rel="noopener noreferrer">https://example.com/a?b=1</a> конец');
  assert.equal(mdInline('(https://e.com).'),
    '(<a href="https://e.com" target="_blank" rel="noopener noreferrer">https://e.com</a>).');
});

/* -------------------------------------------------------------------- images */

test('local images go through fileUrl', () => {
  const html = mdInline('![схема](C:\\pics\\a.png)');
  assert.equal(html, '<img class="md-img" src="/api/file?path=C%3A%5Cpics%5Ca.png" alt="схема" loading="lazy">');

  const custom = mdInline('![x](/tmp/a.png)', { fileUrl: (p) => '/f?p=' + encodeURIComponent(p) });
  assert.equal(custom, '<img class="md-img" src="/f?p=%2Ftmp%2Fa.png" alt="x" loading="lazy">');
});

test('remote images render as links (no network access)', () => {
  assert.equal(mdInline('![лого](https://e.com/i.png)'),
    '<a class="md-img-link" href="https://e.com/i.png" target="_blank" rel="noopener noreferrer">лого</a>');
  assert.equal(mdInline('![](https://e.com/i.png)'),
    '<a class="md-img-link" href="https://e.com/i.png" target="_blank" rel="noopener noreferrer">https://e.com/i.png</a>');
});

test('data:image base64 renders, svg data uri refused', () => {
  const png = 'data:image/png;base64,iVBORw0KGgo=';
  assert.equal(mdInline('![p](' + png + ')'),
    '<img class="md-img" src="' + png + '" alt="p">');
  assert.equal(mdInline('![p](data:image/svg+xml;base64,PHN2Zz4=)'), 'p');
  assert.equal(mdInline('![p](data:text/html;base64,PHNjcmlwdD4=)'), 'p');
});

/* ----------------------------------------------------------------------- XSS */

const XSS_VECTORS = [
  '<script>alert(1)</script>',
  '<img src=x onerror=alert(1)>',
  '"><svg onload=alert(1)>',
  '<iframe src="javascript:alert(1)"></iframe>',
  '<style>body{display:none}</style>',
  '<!-- комментарий -->',
  '<a href="javascript:alert(1)">x</a>',
  '<div onclick=alert(1)>',
  '<scr<script>ipt>alert(1)</script>',
  '<img/src=`x`onerror=alert(1)>',
  '<svg><script>alert(1)</script></svg>',
  '</textarea><script>alert(1)</script>',
  '<math><mtext><script>alert(1)</script></mtext></math>',
  '<noscript><p title="</noscript><img src=x onerror=alert(1)>">',
];

/** Tags the renderer is allowed to emit; anything else in the output is a bug. */
const ALLOWED_TAGS = /<\/?(?:p|br|strong|em|del|code|pre|ul|ol|li|blockquote|h[1-4]|hr|table|thead|tbody|tr|th|td|a|img|input)\b[^>]*>/g;

for (const vector of XSS_VECTORS) {
  test('XSS: raw html is escaped — ' + vector.slice(0, 32), () => {
    const html = renderMarkdown(vector);
    const stripped = html.replace(ALLOWED_TAGS, '');
    assert.equal(/[<>]/.test(stripped), false,
      `в выводе остался неэкранированный HTML: ${html}`);
    assert.match(html, /&lt;|&gt;/);
  });
}

test('XSS: javascript: links are dropped, text kept', () => {
  for (const md of [
    '[x](javascript:alert(1))',
    '[x](JaVaScRiPt:alert(1))',
    '[x](JAVASCRIPT:alert(1))',
    '[x]( javascript:alert(1))',
    '[x](java\nscript:alert(1))',
    '[x](vbscript:msgbox(1))',
    '[x](data:text/html;base64,PHNjcmlwdD5hbGVydCgxKTwvc2NyaXB0Pg==)',
    '[x](data:text/javascript,alert(1))',
  ]) {
    const html = mdInline(md);
    assert.equal(/href/i.test(html), false, 'ссылка не должна появиться: ' + md + ' → ' + html);
    assert.equal(/<a\b/.test(html), false, 'тег <a> не должен появиться: ' + md);
    assert.equal(html.includes('javascript:') || html.includes('vbscript:'), false);
    assert.equal(html.includes('x'), true);
  }
});

test('XSS: javascript: images are dropped, alt kept', () => {
  for (const md of ['![x](javascript:alert(1))', '![x](JaVaScRiPt:alert(1))', '![x](vbscript:x)']) {
    const html = mdInline(md);
    assert.equal(/<img|<a\b|href=/i.test(html), false, html);
    assert.equal(html, 'x');
  }
});

test('XSS: attempts inside tables, lists, quotes and headings', () => {
  const md = [
    '# <script>alert(1)</script>',
    '',
    '| <img src=x onerror=alert(1)> | b |',
    '|---|---|',
    '| <script>alert(1)</script> | <b>x</b> |',
    '',
    '- <svg onload=alert(1)>',
    '- [<script>alert(1)</script>](javascript:alert(1))',
    '',
    '> <iframe src=javascript:alert(1)>',
    '',
    '```',
    '<script>alert(1)</script>',
    '```',
  ].join('\n');
  const html = renderMarkdown(md);
  assert.equal(/<script|<svg|<iframe|<img(?![^>]*class="md-img")|<b>/i.test(html), false);
  assert.match(html, /&lt;script&gt;/);
  assert.match(html, /&lt;img src=x onerror=alert\(1\)&gt;/);
});

test('XSS: entity-encoded schemes stay literal', () => {
  const html = mdInline('[x](java&#115;cript:alert(1))');
  assert.equal(html.includes('href="javascript'), false);
  assert.match(html, /&amp;#115;/);
});

test('XSS: attribute injection through link titles', () => {
  const html = mdInline('[x](https://e.com "a\\" onmouseover=\\"alert(1)")');
  // кавычка внутри title обязана быть экранирована, иначе атрибут «вылезет»
  const m = /<a href="https:\/\/e\.com" title="([^"]*)" target="_blank"/.exec(html);
  assert.ok(m, 'title не должен разрывать атрибут: ' + html);
  assert.match(m[1], /&quot;/);
});

/* ------------------------------------------------------- encodings & unicode */

test('CRLF input renders the same as LF', () => {
  const md = '# Заголовок\r\n\r\n- пункт\r\n- ещё\r\n\r\n**жирный** и `код`\r\n';
  const crlf = renderMarkdown(md);
  const lf = renderMarkdown(md.replace(/\r\n/g, '\n'));
  assert.equal(crlf, lf);
  assert.match(crlf, /<h1>Заголовок<\/h1>/);
  assert.match(crlf, /<li>пункт<\/li>/);
});

test('cyrillic and emoji survive', () => {
  assert.equal(mdInline('Привет, Михаил! 👋'), 'Привет, Михаил! 👋');
  assert.equal(renderMarkdown('## Лето — этап 2'),
    '<h2>Лето — этап 2</h2>');
  assert.equal(mdInline('**Ёжик**'), '<strong>Ёжик</strong>');
});

test('html entities inside code stay doubly escaped', () => {
  assert.equal(mdInline('`&lt;script&gt;`'), '<code class="md-code-inline">&amp;lt;script&amp;gt;</code>');
  assert.equal(renderMarkdown('```\n&amp;\n```'), '<pre class="md-code"><code>&amp;amp;</code></pre>');
});

test('unterminated inline constructs do not swallow text', () => {
  assert.equal(mdInline('**a'), '**a');
  assert.equal(mdInline('[a](b'), '[a](b');
  assert.equal(mdInline('`a'), '`a');
  assert.equal(mdInline('~~a'), '~~a');
  assert.equal(mdInline('![a](b'), '![a](b');
  // блок после списка не съедается списком
  const html = renderMarkdown('- a\n**жирный** текст');
  assert.match(html, /<li>a<\/li>/);
  assert.match(html, /<p><strong>жирный<\/strong> текст<\/p>/);
});

/* --------------------------------------------------------------- performance */

test('200KB document renders in well under 200 ms', () => {
  const block = [
    '## Раздел',
    '',
    'Абзац с **жирным**, *курсивом*, `кодом` и ссылкой [сайт](https://example.com).',
    '',
    '| Колонка | Значение |',
    '|---|---:|',
    '| Итого | 1 234,50 |',
    '',
    '- пункт один',
    '- пункт два',
    '  - вложенный',
    '',
    '> цитата с **разметкой**',
    '',
    '```js',
    'const x = 1 < 2;',
    '```',
    '',
  ].join('\n');
  let doc = '';
  while (doc.length < 200 * 1024) doc += block;
  assert.ok(doc.length >= 200 * 1024);

  const started = process.hrtime.bigint();
  const html = renderMarkdown(doc);
  const ms = Number(process.hrtime.bigint() - started) / 1e6;
  assert.ok(html.length > 1000);
  assert.ok(ms < 200, `рендер занял ${ms.toFixed(1)} мс (лимит 200)`);
});

test('pathological nesting does not hang', () => {
  const started = process.hrtime.bigint();
  renderMarkdown('- ' + '  '.repeat(500) + 'x');
  renderMarkdown('>'.repeat(200) + ' quote');
  renderMarkdown('*'.repeat(300));
  renderMarkdown('['.repeat(200) + 'x' + ']'.repeat(200));
  renderMarkdown('`'.repeat(99) + '\n' + 'a'.repeat(1000));
  const ms = Number(process.hrtime.bigint() - started) / 1e6;
  assert.ok(ms < 500, `патологический ввод занял ${ms.toFixed(1)} мс`);
});

/* ---------------------------------------------------------------- mdToText */

test('mdToText strips markup for snippets', () => {
  assert.equal(mdToText('**жирный** и *курсив*'), 'жирный и курсив');
  assert.equal(mdToText('# Заголовок\n\nабзац'), 'Заголовок абзац');
  assert.equal(mdToText('- пункт\n- ещё'), 'пункт ещё');
  assert.equal(mdToText('[сайт](https://e.com)'), 'сайт');
  assert.equal(mdToText('![картинка](a.png)'), 'картинка');
  assert.equal(mdToText('> цитата'), 'цитата');
  assert.equal(mdToText('`код`'), 'код');
  assert.equal(mdToText('| a | b |\n|---|---|\n| 1 | 2 |'), 'a b 1 2');
  assert.equal(mdToText('<b>не тег</b>'), 'не тег');
  assert.equal(mdToText('- [x] готово'), 'готово');
  assert.equal(mdToText('  много   пробелов \n\n тут  '), 'много пробелов тут');
});

test('mdToText truncates with ellipsis', () => {
  const long = 'а'.repeat(100);
  const out = mdToText(long, 20);
  assert.equal(out.length, 20);
  assert.ok(out.endsWith('…'));
  assert.equal(mdToText('коротко', 50), 'коротко');
  assert.equal(mdToText(''), '');
});

test('mdToText output is plain text, never executable markup', () => {
  assert.equal(mdToText('<script>alert(1)</script>'), 'alert(1)');
  assert.equal(mdToText('<img src=x onerror=alert(1)>'), '');
});

/* --------------------------------------------------------------------- fuzz */

/** Deterministic PRNG so a failure is reproducible. */
function mulberry32(seed) {
  let a = seed >>> 0;
  return () => {
    a = (a + 0x6d2b79f5) >>> 0;
    let t = Math.imul(a ^ (a >>> 15), 1 | a);
    t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

test('fuzz: random input never throws and never emits raw html', () => {
  const alphabet = '<>&"\'`*_~[]()!#-|\: \n\tabcАБ1./';
  const rnd = mulberry32(0xC0FFEE);
  const allowed = /<\/?(?:p|br|strong|em|del|code|pre|ul|ol|li|blockquote|h[1-4]|hr|table|thead|tbody|tr|th|td|a|img|input)\b[^>]*>/g;
  for (let iter = 0; iter < 600; iter++) {
    const len = 1 + Math.floor(rnd() * 120);
    let s = '';
    for (let k = 0; k < len; k++) s += alphabet[Math.floor(rnd() * alphabet.length)];
    let html;
    assert.doesNotThrow(() => { html = renderMarkdown(s); }, 'упало на: ' + JSON.stringify(s));
    const stripped = html.replace(allowed, '');
    assert.equal(/[<>]/.test(stripped), false,
      'неэкранированный HTML на входе ' + JSON.stringify(s) + ' → ' + html);
    assert.equal(/href="(?:javascript|data(?!:image)|vbscript)/i.test(html), false,
      'опасная схема в href на входе ' + JSON.stringify(s));
  }
});

test('fuzz: inline-only input has balanced structure and no crash', () => {
  const alphabet = '*_~`[]()\! <>"\'абв';
  const rnd = mulberry32(42);
  for (let iter = 0; iter < 400; iter++) {
    let s = '';
    const len = 1 + Math.floor(rnd() * 60);
    for (let k = 0; k < len; k++) s += alphabet[Math.floor(rnd() * alphabet.length)];
    assert.doesNotThrow(() => mdInline(s), 'упало на: ' + JSON.stringify(s));
  }
});
