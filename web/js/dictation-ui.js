/**
 * dictation-ui.js — самозапуск микрофона во всех полях ввода.
 *
 * Подключается из index.html как <script type="module">; модуль ничего не
 * рисует сам, только просит dictation.js следить за DOM: новые диалоги,
 * палитра Ctrl+K, лента, вкладка «Дальше» получают кнопку автоматически.
 * Поля с data-mic="manual" (композер) и "off" не трогаются.
 */

import { startAutoEnhance, enhanceAll } from './dictation.js';

function boot() {
  try {
    startAutoEnhance();
    enhanceAll(globalThis.document);
    if (globalThis.document && globalThis.document.documentElement) {
      globalThis.document.documentElement.setAttribute('data-dictation', 'on');
    }
  } catch (err) {
    if (typeof console !== 'undefined' && console.warn) console.warn('[диктовка] не запустилась', err);
  }
}

if (typeof document !== 'undefined') {
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', boot, { once: true });
  else boot();
  globalThis.teamroomDictation = { enhanceAll, startAutoEnhance };
}
