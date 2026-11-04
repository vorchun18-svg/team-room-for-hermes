/**
 * api.js — единственная точка общения с локальным сервером.
 * Формат ошибок сервера: {error:{code,message}} → ApiError(code, message, status).
 */

import { plainTerms } from './util.js';

export class ApiError extends Error {
  /**
   * @param {string} code  машинный код ('network'|'not_found'|'bad_request'|…)
   * @param {string} message  текст по-русски для показа
   * @param {number} status  HTTP-статус (0 — сеть)
   * @param {*} [details]
   */
  constructor(code, message, status = 0, details = null) {
    super(message || 'Неизвестная ошибка');
    this.name = 'ApiError';
    this.code = code || 'unknown';
    this.status = status || 0;
    this.details = details;
  }
}

/** Последние ETag по путям (для условных GET). */
const etags = new Map();

/** Достать {code,message} из тела ошибки в любом из ожидаемых видов. */
function readError(data, status) {
  const e = data && typeof data === 'object' ? (data.error ?? data) : null;
  if (e && typeof e === 'object') {
    return {
      code: String(e.code || (status === 404 ? 'not_found' : 'http_' + status)),
      message: plainTerms(e.message || e.detail || e.error || ('Ошибка ' + status)),
    };
  }
  if (typeof e === 'string' && e.trim()) return { code: 'http_' + status, message: e.trim() };
  return {
    code: status === 404 ? 'not_found' : 'http_' + status,
    message: status === 404 ? 'Не найдено' : 'Сервер ответил ' + status,
  };
}

async function request(method, path, opts = {}) {
  const { body, etag, signal, headers: extra } = opts;
  const headers = Object.assign({ Accept: 'application/json' }, extra || {});
  let sentEtag = null;
  if (etag === true) sentEtag = etags.get(path) || null;
  else if (typeof etag === 'string' && etag) sentEtag = etag;
  if (sentEtag) headers['If-None-Match'] = sentEtag;
  if (body !== undefined && !(body instanceof FormData)) headers['Content-Type'] = 'application/json';

  let res;
  try {
    res = await fetch(path, {
      method,
      headers,
      signal,
      credentials: 'same-origin',
      cache: 'no-store',
      body: body === undefined ? undefined : body instanceof FormData ? body : JSON.stringify(body),
    });
  } catch (err) {
    if (err && err.name === 'AbortError') throw err;
    throw new ApiError('network', 'Нет связи с приложением', 0, err);
  }

  if (res.status === 304) return { notModified: true, etag: sentEtag };

  const tag = res.headers.get('ETag');
  if (tag) etags.set(path, tag);

  const type = res.headers.get('Content-Type') || '';
  let data = null;
  if (res.status !== 204) {
    if (type.includes('json')) {
      data = await res.json().catch(() => null);
    } else {
      const text = await res.text();
      data = text || null;
      if (!res.ok) data = { error: { code: 'http_' + res.status, message: text.slice(0, 400) } };
    }
  }

  if (!res.ok) {
    const { code, message } = readError(data, res.status);
    throw new ApiError(code, message, res.status, data);
  }
  return data;
}

export const api = {
  /** GET → JSON. `etag:true` — условный запрос по сохранённому ETag. */
  get(path, opts) {
    return request('GET', path, opts);
  },
  post(path, body, opts) {
    return request('POST', path, Object.assign({ body: body === undefined ? {} : body }, opts));
  },
  patch(path, body, opts) {
    return request('PATCH', path, Object.assign({ body: body === undefined ? {} : body }, opts));
  },
  /** PUT — полная замена части ресурса (напр. особые указания участника комнаты). */
  put(path, body, opts) {
    return request('PUT', path, Object.assign({ body: body === undefined ? {} : body }, opts));
  },
  /** DELETE; `body` не обязателен. */
  del(path, body, opts) {
    return request('DELETE', path, Object.assign({ body }, opts));
  },
  /** Текстовый GET (markdown, ics) — без разбора JSON. */
  text(path, opts) {
    return request('GET', path, Object.assign({ headers: { Accept: 'text/plain' } }, opts));
  },
  /** Последний ETag для пути (для ручных условных запросов). */
  etag(path) {
    return etags.get(path) || null;
  },
  /** Сбросить кэш ETag (после массового обновления). */
  resetEtags() {
    etags.clear();
  },
};

/** Ссылка на файл из белого списка сервера. */
export function fileUrl(path) {
  return '/api/file?path=' + encodeURIComponent(String(path == null ? '' : path));
}

/**
 * Опрос с паузой в скрытой вкладке.
 * @param {() => any} fn  что вызывать (может быть async)
 * @param {number} [ms=5000]
 * @param {{pauseHidden?:boolean, immediate?:boolean, onError?:(e:Error)=>void}} [opts]
 * @returns {{stop:()=>void, tick:()=>Promise<void>, isStopped:()=>boolean}}
 */
export function poll(fn, ms = 5000, opts = {}) {
  const { pauseHidden = true, immediate = true, onError } = opts;
  let stopped = false;
  let timer = null;
  let running = false;

  const schedule = (delay = ms) => {
    if (stopped) return;
    clearTimeout(timer);
    timer = setTimeout(tick, delay);
  };

  async function tick() {
    if (stopped) return;
    if (pauseHidden && typeof document !== 'undefined' && document.hidden) return schedule();
    if (running) return schedule();
    running = true;
    try {
      await fn();
    } catch (err) {
      if (onError) onError(err);
    } finally {
      running = false;
      schedule();
    }
  }

  const onVisible = () => {
    if (typeof document !== 'undefined' && !document.hidden) {
      clearTimeout(timer);
      tick();
    }
  };
  if (pauseHidden && typeof document !== 'undefined') {
    document.addEventListener('visibilitychange', onVisible);
  }

  if (immediate) tick();
  else schedule();

  return {
    stop() {
      stopped = true;
      clearTimeout(timer);
      if (pauseHidden && typeof document !== 'undefined') {
        document.removeEventListener('visibilitychange', onVisible);
      }
    },
    tick,
    isStopped: () => stopped,
  };
}

export default api;
