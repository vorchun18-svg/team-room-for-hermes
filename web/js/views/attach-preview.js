/**
 * attach-preview.js — предпросмотр вложения в панели приложения (W29B-Q3).
 *
 * Зачем: вложение было только путём — чтобы понять, что принесли, человек
 * уходил в другую программу. Здесь файл читается на месте: картинка, PDF
 * (страницы браузерным просмотром), текст и markdown, CSV-таблица, текст из
 * docx/xlsx/pptx, а голосовое — расшифровкой штатным STT приложения.
 * OCR показывается только если сервер нашёл tesseract.
 *
 * Сервер: `GET /api/attach/preview`, `GET /api/attach/file`,
 * `POST /api/attach/transcribe` + `GET …?job=`, `POST /api/attach/ocr`.
 * Никаких `window.open`: окно рисуем сами и держим постоянный размер
 * (правило волны 29 — диалог не прыгает).
 */

import { h } from '../util.js';
import { icon } from '../icons.js';
import { buildDialog, ensureCss } from '../ui.js';

ensureCss('css/attach-preview.css');

const KIND_RU = {
  image: 'Изображение', pdf: 'PDF', text: 'Текст', table: 'Таблица',
  docx: 'Документ Word', xlsx: 'Таблица Excel', pptx: 'Презентация',
  audio: 'Голосовое', other: 'Файл',
};

const KIND_ICON = {
  image: 'eye', pdf: 'file-text', text: 'file-text', table: 'table',
  docx: 'file-text', xlsx: 'table', pptx: 'layout', audio: 'play-circle', other: 'file',
};

const IMAGE_EXT = /\.(?:png|jpe?g|gif|webp|bmp|tiff?)$/i;
const AUDIO_EXT = /\.(?:ogg|mp3|wav|m4a|opus|webm|flac)$/i;
const TABLE_EXT = /\.(?:csv|tsv)$/i;
const OFFICE = { docx: true, xlsx: true, pptx: true };
const TEXT_EXT = /\.(?:md|markdown|txt|log|json|jsonl|ya?ml|xml|ics|html?|css|js|mjs|py|sh|ini|conf|cfg)$/i;

/** Вид файла по расширению — те же группы, что на сервере (`attach_preview.kind_of`). */
export function kindOf(path) {
  const p = String(path == null ? '' : path).trim();
  if (IMAGE_EXT.test(p)) return 'image';
  if (/\.pdf$/i.test(p)) return 'pdf';
  if (/\.docx$/i.test(p)) return 'docx';
  if (/\.xlsx$/i.test(p)) return 'xlsx';
  if (/\.pptx$/i.test(p)) return 'pptx';
  if (TABLE_EXT.test(p)) return 'table';
  if (AUDIO_EXT.test(p)) return 'audio';
  if (TEXT_EXT.test(p)) return 'text';
  return 'other';
}

/** Есть ли смысл в кнопке «Просмотр»: всё, кроме неизвестных типов. */
export function canPreview(path) {
  return kindOf(path) !== 'other';
}

function sizeText(bytes) {
  const n = Number(bytes);
  if (!Number.isFinite(n) || n < 0) return '';
  if (n < 1024) return n + ' Б';
  if (n < 1024 * 1024) return (n / 1024).toFixed(1).replace('.', ',') + ' КБ';
  return (n / (1024 * 1024)).toFixed(1).replace('.', ',') + ' МБ';
}

function btn(label, opts = {}) {
  const o = opts || {};
  const node = h('button', {
    class: ['btn', o.variant ? 'btn--' + o.variant : 'btn--secondary', 'btn--sm', o.className || ''],
    type: 'button',
    title: o.title || null,
    disabled: o.disabled ? 'disabled' : null,
  }, o.icon ? icon(o.icon, { size: 13 }) : null, h('span', null, label));
  return node;
}

async function copyText(text) {
  const value = String(text || '');
  try {
    if (navigator.clipboard && navigator.clipboard.writeText) {
      await navigator.clipboard.writeText(value);
      return true;
    }
  } catch { /* ниже — запасной путь */ }
  try {
    const area = h('textarea', { class: 'ap-clip', readonly: 'readonly' });
    area.value = value;
    document.body.appendChild(area);
    area.select();
    const ok = document.execCommand && document.execCommand('copy');
    document.body.removeChild(area);
    return Boolean(ok);
  } catch {
    return false;
  }
}

/**
 * Открыть панель предпросмотра вложения.
 *
 * @param {{path: string, name?: string, room?: string, requestId?: string,
 *          api?: object, onInsert?: (text: string) => void,
 *          onChanged?: Function}} opts
 *   `api` — клиент приложения (ctx.api); `onInsert` — «Добавить расшифровку в
 *   запрос» (у страницы запроса — сообщение, у поля запроса — текст в поле);
 *   `requestId` — куда отправить расшифровку, если `onInsert` не передан.
 * @returns {Promise<{close: Function}>} панель
 */
export async function openAttachPreview(opts = {}) {
  const o = opts || {};
  const path = String(o.path == null ? '' : o.path).trim();
  const room = String(o.room || '').trim();
  const kind = kindOf(path);
  const name = String(o.name || path.split(/[\\/]/).pop() || 'Файл');
  let api = o.api;
  if (!api) {
    const mod = await import('../api.js');
    api = mod.api;
  }

  const sizeEl = h('span', { class: 'ap-meta__size tnum' }, '');
  const headMeta = h('div', { class: 'ap-meta' },
    h('span', { class: 'chip chip--muted' }, KIND_RU[kind] || 'Файл'),
    sizeEl);
  const status = h('div', { class: 'ap-status small', role: 'status' }, '');

  const body = h('div', { class: 'ap-body' }, h('div', { class: 'ap-loading' },
    icon('loading', { size: 14 }), h('span', null, 'Читаю файл…')));

  const content = h('div', { class: 'ap-content' }, headMeta, body, status);

  const openBtn = btn('Открыть', {
    icon: 'open-preview', variant: 'secondary', title: 'Открыть файл программой по умолчанию',
  });
  const revealBtn = btn('Открыть в папке', {
    icon: 'folder-opened', variant: 'ghost', title: 'Показать файл в проводнике',
  });
  const closeBtn = btn('Закрыть', { variant: 'primary' });

  let dlg = null;
  const close = () => { if (dlg) dlg.close(); };
  closeBtn.addEventListener('click', close);

  const runFileAction = async (mode) => {
    try {
      await api.post('/api/files/launch', { path, room, mode });
    } catch (err) {
      status.textContent = (err && err.message)
        || (mode === 'reveal' ? 'Не удалось открыть папку' : 'Не удалось открыть файл');
      status.classList.add('ap-status--bad');
    }
  };
  openBtn.addEventListener('click', () => { runFileAction('open'); });
  revealBtn.addEventListener('click', () => { runFileAction('reveal'); });

  dlg = buildDialog({
    title: name,
    wide: true,
    onCancel: close,
    body: content,
    buttons: [openBtn, revealBtn, closeBtn],
  });
  dlg.dialog.classList.add('ap-dialog');

  const say = (text, bad) => {
    status.textContent = String(text || '');
    status.classList.toggle('ap-status--bad', Boolean(bad));
  };

  /** «Добавить в запрос»: у страницы запроса — сообщением, иначе через onInsert. */
  const insertText = async (text) => {
    const value = String(text || '').trim();
    if (!value) return;
    if (typeof o.onInsert === 'function') {
      o.onInsert(value);
      say('Расшифровка добавлена в задачу');
      return;
    }
    if (o.requestId) {
      try {
        await api.post('/api/requests/' + encodeURIComponent(o.requestId) + '/messages',
          { text: 'Расшифровка голосового «' + name + '»:\n' + value });
        say('Расшифровка отправлена в задачу');
        if (typeof o.onChanged === 'function') o.onChanged();
        return;
      } catch (err) {
        say((err && err.message) || 'Не удалось отправить расшифровку', true);
        return;
      }
    }
    const ok = await copyText(value);
    say(ok ? 'Текст скопирован — вставьте его в задачу' : 'Не удалось скопировать текст', !ok);
  };

  const transcriptBox = (text, extra) => {
    const pre = h('pre', { class: 'ap-transcript', tabindex: '0' }, text || 'Тишина: в записи нет речи.');
    const acts = h('div', { class: 'ap-actions' },
      btn('Добавить расшифровку в задачу', {
        icon: 'comment-draft', variant: 'primary',
        onclick: () => { insertText(text); },
      }),
      btn('Скопировать', {
        icon: 'copy', variant: 'ghost',
        onclick: async () => { say(await copyText(text) ? 'Текст скопирован' : 'Не удалось скопировать', false); },
      }));
    return h('div', { class: 'ap-block' },
      h('h3', { class: 'ap-block__title' },
        'Расшифровка',
        extra ? h('span', { class: 'chip chip--muted' }, extra) : null),
      pre, acts);
  };

  const render = (data) => {
    body.replaceChildren();
    if (data && data.size != null) sizeEl.textContent = sizeText(data.size);
    if (!data || data.ok === false) {
      body.appendChild(h('div', { class: 'ap-note ap-note--bad' },
        (data && data.message) || 'Не удалось прочитать файл — откройте его программой.'));
      return;
    }
    if (data.unavailable) {
      body.appendChild(h('div', { class: 'ap-note' }, data.note || 'Предпросмотра для этого файла нет.'));
      return;
    }
    if (data.kind === 'image') {
      body.appendChild(h('div', { class: 'ap-image' }, h('img', { src: data.url, alt: name, loading: 'lazy' })));
      const ocrReady = Boolean(data.ocr && data.ocr.available);
      const block = h('div', { class: 'ap-block' },
        h('h3', { class: 'ap-block__title' }, 'Текст на картинке'),
        h('div', { class: 'small muted', 'data-ap': 'ocr-hint' },
          ocrReady ? 'Можно распознать текст: сканы, чеки, скриншоты.'
            : 'Распознавание сканов недоступно: в системе не найден tesseract.'));
      if (ocrReady) {
        const out = h('div', { class: 'ap-ocr' });
        const run = btn('Распознать текст', { icon: 'eye', variant: 'secondary' });
        run.addEventListener('click', async () => {
          run.disabled = true;
          say('Распознаю текст…');
          try {
            const res = await api.post('/api/attach/ocr', { path, room });
            out.replaceChildren(h('pre', { class: 'ap-transcript', tabindex: '0' },
              (res && res.text) || 'Текст не найден.'));
            if (res && res.text) {
              out.appendChild(h('div', { class: 'ap-actions' },
                btn('Добавить текст в задачу', { icon: 'comment-draft', variant: 'primary', onclick: () => insertText(res.text) })));
            }
            say('Готово за ' + (res && res.seconds != null ? res.seconds + ' с' : 'несколько секунд'));
          } catch (err) {
            say((err && err.message) || 'Не удалось распознать текст', true);
          } finally {
            run.disabled = false;
          }
        });
        block.appendChild(h('div', { class: 'ap-actions' }, run));
        block.appendChild(out);
      }
      body.appendChild(block);
      return;
    }
    if (data.kind === 'pdf') {
      body.appendChild(h('iframe', {
        class: 'ap-pdf', src: data.url, title: 'PDF: ' + name,
      }));
      body.appendChild(h('div', { class: 'small muted ap-hint' },
        'Страницы листаются в окне просмотра. Если пусто — нажмите «Открыть».'));
      return;
    }
    if (data.kind === 'audio') {
      const player = h('audio', { class: 'ap-audio', controls: 'controls', preload: 'metadata', src: data.url });
      const info = h('div', { class: 'small muted' },
        data.duration_s ? 'Примерно ' + Math.round(data.duration_s) + ' с' : '');
      const host = h('div', { class: 'ap-block' }, h('h3', { class: 'ap-block__title' }, 'Прослушать'),
        player, info);
      if (data.stt && data.stt.fake) {
        host.appendChild(h('div', { class: 'small muted' }, 'Песочница: расшифровка подставная.'));
      }
      const out = h('div', { class: 'ap-stt' });
      const run = btn('Расшифровать', { icon: 'play-circle', variant: 'secondary' });
      run.addEventListener('click', async () => {
        run.disabled = true;
        out.replaceChildren();
        const bar = h('div', { class: 'ap-progress' }, h('span', { class: 'ap-progress__fill' }));
        out.appendChild(h('div', { class: 'ap-stt__run' },
          h('span', { class: 'small' }, 'Расшифровываю…'), bar));
        try {
          let job = await api.post('/api/attach/transcribe', { path, room });
          const tick = async () => {
            const fill = bar.querySelector('.ap-progress__fill');
            if (fill) fill.style.width = Math.round((job.progress || 0) * 100) + '%';
            if (job.state === 'running') {
              await new Promise((resolve) => { setTimeout(resolve, 700); });
              job = await api.get('/api/attach/transcribe?job=' + encodeURIComponent(job.job_id));
              return tick();
            }
            if (job.state === 'error') {
              out.replaceChildren(h('div', { class: 'ap-note ap-note--bad' },
                job.message || 'Расшифровка не удалась'));
              return;
            }
            out.replaceChildren(transcriptBox(job.text || '',
              job.model ? String(job.model) : ''));
            return null;
          };
          await tick();
        } catch (err) {
          out.replaceChildren(h('div', { class: 'ap-note ap-note--bad' },
            (err && err.message) || 'Не удалось запустить расшифровку'));
        } finally {
          run.disabled = false;
        }
      });
      host.appendChild(h('div', { class: 'ap-actions' }, run));
      host.appendChild(out);
      body.appendChild(host);
      return;
    }
    if (data.kind === 'table') {
      const table = data.table || {};
      const cols = Array.isArray(table.columns) ? table.columns : [];
      const rows = Array.isArray(table.rows) ? table.rows : [];
      const node = h('table', { class: 'ap-table' });
      if (cols.length) {
        const tr = h('tr');
        for (const col of cols) tr.appendChild(h('th', { scope: 'col' }, String(col == null ? '' : col)));
        node.appendChild(h('thead', null, tr));
      }
      const tbody = h('tbody');
      for (const row of rows) {
        const tr = h('tr');
        for (const cell of row) tr.appendChild(h('td', null, String(cell == null ? '' : cell)));
        tbody.appendChild(tr);
      }
      node.appendChild(tbody);
      body.appendChild(h('div', { class: 'ap-table-wrap' }, node));
      if (table.truncated) {
        body.appendChild(h('div', { class: 'small muted ap-hint' },
          'Показаны первые ' + rows.length + ' строк из ' + (table.total_rows || rows.length) + '.'));
      }
      return;
    }
    if (data.kind === 'pptx' && Array.isArray(data.slides)) {
      const list = h('ol', { class: 'ap-slides' });
      for (const slide of data.slides) {
        list.appendChild(h('li', { class: 'ap-slide' },
          h('div', { class: 'ap-slide__num' }, 'Слайд ' + slide.number),
          h('pre', { class: 'ap-slide__text', tabindex: '0' }, String(slide.text || ''))));
      }
      body.appendChild(list);
      if (data.truncated) body.appendChild(h('div', { class: 'small muted ap-hint' }, 'Показаны не все слайды.'));
      return;
    }
    if (data.kind === 'docx' || data.kind === 'xlsx') {
      if (data.table) {
        const wrap = h('div', { class: 'ap-table-wrap' });
        const table = h('table', { class: 'ap-table' });
        const cols = Array.isArray(data.table.columns) ? data.table.columns : [];
        if (cols.length) {
          const tr = h('tr');
          for (const col of cols) tr.appendChild(h('th', { scope: 'col' }, String(col == null ? '' : col)));
          table.appendChild(h('thead', null, tr));
        }
        const tbody = h('tbody');
        for (const row of data.table.rows || []) {
          const tr = h('tr');
          for (const cell of row) tr.appendChild(h('td', null, String(cell == null ? '' : cell)));
          tbody.appendChild(tr);
        }
        table.appendChild(tbody);
        wrap.appendChild(table);
        body.appendChild(wrap);
        return;
      }
    }
    // Текст, Word и всё, что сервер отдал текстом.
    const text = String(data.text || '');
    if (!text) {
      body.appendChild(h('div', { class: 'ap-note' }, 'В файле нет текста — откройте его программой.'));
      return;
    }
    body.appendChild(h('pre', { class: 'ap-text', tabindex: '0' }, text));
    const hints = [];
    if (data.encoding) hints.push('кодировка ' + data.encoding);
    if (data.lines) hints.push(data.lines + ' стр.');
    if (data.paragraphs) hints.push(data.paragraphs + ' абз.');
    if (data.truncated) hints.push('показано не всё');
    if (hints.length) body.appendChild(h('div', { class: 'small muted ap-hint' }, hints.join(' · ')));
  };

  try {
    const query = '/api/attach/preview?path=' + encodeURIComponent(path)
      + (room ? '&room=' + encodeURIComponent(room) : '');
    const data = await api.get(query);
    render(data);
  } catch (err) {
    body.replaceChildren(h('div', { class: 'ap-note ap-note--bad' },
      (err && err.message) || 'Не удалось построить предпросмотр'));
    const retry = btn('Повторить', { icon: 'refresh', variant: 'secondary' });
    retry.addEventListener('click', async () => {
      body.replaceChildren(h('div', { class: 'ap-loading' }, icon('loading', { size: 14 }),
        h('span', null, 'Читаю файл…')));
      try {
        render(await api.get('/api/attach/preview?path=' + encodeURIComponent(path)
          + (room ? '&room=' + encodeURIComponent(room) : '')));
      } catch (again) {
        body.replaceChildren(h('div', { class: 'ap-note ap-note--bad' },
          (again && again.message) || 'Файл не читается'));
      }
    });
    const holder = body.firstChild;
    if (holder) body.appendChild(h('div', { class: 'ap-actions' }, retry));
  }
  return { close, dialog: dlg.dialog, body };
}

export default { openAttachPreview, canPreview, kindOf };
