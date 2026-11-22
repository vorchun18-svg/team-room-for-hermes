/**
 * shell-svod-bar.js — липкая панель свода (волна 25, пакет L1, R5 §7.6;
 * переделка W25.5: одно место управления вместо двух).
 *
 * Одна строка 40 px вверху области прокрутки, и в ней ВСЁ управление сводом:
 *   слева  «Свод» + переключатель версий v1|v2|v3 + тихая пометка «актуально»;
 *   справа «Доработать · Копировать · Ещё ▾».
 * Внутри карточки свода переключателя версий и строки «Актуально на …» больше
 * нет — иначе человек видел два места с одними и теми же данными.
 *
 * При виде < 900 (режимы m/s/xs) переключатель версий и «Обновить свод сейчас»
 * уходят в меню «Ещё ▾»: три чипа версий рядом с названием в 800 px спорят за
 * место. Скрытие — чистым CSS (`@container view`), поэтому разметка одна, а
 * показ зависит от ширины вида, а не от JS.
 *
 * Дублирования логики нет: кнопки панели нажимают кнопки карточки свода
 * (`[data-action=revise|copy]`), обновление — та же функция `refreshSvod()`
 * из request-answer.js, меню экспорта — тот же `exportGroup()`.
 * Если свода ещё нет — панели нет: пустая строка над содержимым не нужна.
 */

import { h } from './util.js';
import { icon } from './icons.js';
import { segmented } from './ui.js';
import { exportGroup } from './views/request-export.js';
import { synthesisOf, latestVersion, versionsOf } from './views/request-util.js';
import { SVOD_RU } from './text-ru.js';

/** Высота панели, px: то же число в `--req-bar-h` (tokens.css) и в CSS. */
export const BAR_H = 40;

/**
 * Подпись состояния свода для панели: «Свод · v3».
 * @param {object} detail карточка запроса
 * @returns {string} '' — свода нет
 */
export function barLabel(detail) {
  const s = synthesisOf(detail);
  const v = latestVersion(s);
  if (!v) return '';
  return 'Свод · v' + v;
}

/** Свод актуален или отстаёт — для тихой пометки в панели. */
export function barNote(detail) {
  const s = synthesisOf(detail);
  const live = s && s.live && typeof s.live === 'object' ? String(s.live.state || '') : '';
  if (live === 'stale' || live === 'pending' || live === 'waiting_cards') return 'отстаёт';
  return '';
}

/** Номера версий свода по возрастанию: `[1, 2, 3]`. */
export function barVersions(detail) {
  const list = versionsOf(synthesisOf(detail));
  return list.map((v) => Number(v && v.version)).filter((n) => n > 0);
}

/**
 * Короткое слово о состоянии свода (одно, без повторов): «актуально» /
 * «отстаёт» / «обновляется…» / «ждёт команду».
 * @param {object} detail
 * @returns {string} '' — живого свода нет (старый сервер)
 */
export function barLiveWord(detail) {
  const s = synthesisOf(detail);
  const live = s && s.live && typeof s.live === 'object' ? s.live : null;
  if (!live) return '';
  const state = String(live.state || '') || 'fresh';
  if (state === 'fresh') return 'актуально';
  if (state === 'updating') return 'обновляется…';
  if (state === 'waiting_cards') return 'ждёт команду';
  if (state === 'pending' || state === 'stale') return 'отстаёт';
  return '';
}

/** Полная подпись состояния (как её пишет сервер) — для подсказки. */
export function barLiveTitle(detail) {
  const s = synthesisOf(detail);
  const live = s && s.live && typeof s.live === 'object' ? s.live : null;
  if (!live) return '';
  return String(live.text || '').trim() || barLiveWord(detail);
}

/**
 * Текущая версия: выбранная человеком, иначе последняя.
 * @param {object} detail
 * @param {number} picked `state.version` страницы (0 — не выбирали)
 * @returns {number}
 */
export function barCurrent(detail, picked) {
  const list = barVersions(detail);
  const latest = list.length ? list[list.length - 1] : 0;
  const n = Number(picked) || 0;
  return n > 0 && n < latest ? n : latest;
}

/**
 * Панель действий. Возвращает `null`, когда показывать нечего (свода нет).
 * @param {{ctx: object, rid: string, getDetail: () => object, root: HTMLElement,
 *          getVersion?: () => number, onVersion?: (n: number) => void,
 *          onRefresh?: () => void}} opts
 * @returns {HTMLElement|null}
 */
export function buildSvodBar(opts = {}) {
  const detail = opts.getDetail ? opts.getDetail() : null;
  const label = barLabel(detail);
  if (!label) return null;
  const note = barNote(detail);
  const root = opts.root || null;
  const versions = barVersions(detail);
  const latest = versions.length ? versions[versions.length - 1] : 0;
  const current = barCurrent(detail, opts.getVersion ? opts.getVersion() : 0);
  const liveWord = barLiveWord(detail);
  const liveTitle = barLiveTitle(detail);
  // «отстаёт» пишем один раз: либо словом живого свода, либо (старый сервер,
  // без `synthesis.live`) прежней пометкой — дубль в одной строке не нужен.
  const noteText = liveWord ? '' : note;
  const liveState = String((synthesisOf(detail).live || {}).state || 'fresh');

  /** Нажать кнопку карточки свода: одна реализация действия, два места нажатия. */
  const press = (action) => () => {
    const btn = root && root.querySelector ? root.querySelector(`.req-answer__foot [data-action="${action}"]`) : null;
    if (btn && !btn.disabled) btn.click();
  };

  /** Выбор версии делает страница (то же состояние, что у карточки). */
  const pickVersion = (val) => {
    const n = Number(val) || 0;
    if (!n || n === current || typeof opts.onVersion !== 'function') return;
    opts.onVersion(n);
  };

  /* Переключатель версий в панели — только когда версий больше одной. */
  const versSwitch = versions.length < 2 ? null : (() => {
    try {
      return h('span', { class: 'req-bar__vers', 'data-bar-versions': '' },
        segmented({
          options: versions.map((v) => ({ value: String(v), label: 'v' + v })),
          value: String(current),
          onchange: pickVersion,
        }));
    } catch (err) {
      console.warn('[request] версии в панели:', err);
      return null;
    }
  })();

  /* Пункты меню «Ещё ▾»: те же версии и обновление свода — для узкого вида. */
  const extra = [];
  // W29B-Q4: «Второе мнение» — та же кнопка, что в блоке под сводом (одна
  // реализация на два места нажатия). Пока блока нет (страница ещё строится) —
  // пункт просто ничего не делает, второй копии окна не заводим.
  extra.push(h('button', {
    type: 'button', class: 'btn btn--ghost btn--sm req-export__btn req-bar__so-item',
    'data-bar': 'second-opinion',
    title: 'Попросить другую модель проверить свод: только расхождения, свод не меняется',
    onclick: () => {
      const btn = root && root.querySelector ? root.querySelector('[data-action="second-opinion"]') : null;
      if (btn && !btn.disabled) btn.click();
    },
  }, icon('shield', { size: 13 }), h('span', null, 'Второе мнение…')));
  if (versions.length > 1 && typeof opts.onVersion === 'function') {
    for (const v of versions) {
      extra.push(h('button', {
        type: 'button',
        class: 'btn btn--ghost btn--sm req-export__btn req-bar__vers-item',
        'data-bar-vers': String(v),
        'aria-current': v === current ? 'true' : null,
        title: v === latest ? 'Последняя версия свода' : 'Показать версию ' + v + ' из ' + latest,
        onclick: () => pickVersion(v),
      }, icon(v === current ? 'check' : 'history', { size: 13 }),
      h('span', null, v === latest ? 'Версия ' + v + ' (последняя)' : 'Версия ' + v)));
    }
  }
  if (typeof opts.onRefresh === 'function') {
    extra.push(h('button', {
      type: 'button',
      class: 'btn btn--ghost btn--sm req-export__btn req-bar__refresh-item',
      'data-bar': 'refresh', title: SVOD_RU.refreshHint,
      onclick: () => opts.onRefresh(),
    }, icon('refresh', { size: 13 }), h('span', null, 'Обновить свод сейчас')));
  }

  const bar = h('div', { class: 'req-bar', 'data-svod-bar': '', role: 'toolbar', 'aria-label': 'Действия со сводом' },
    h('span', { class: 'req-bar__label' },
      h('span', { class: 'req-bar__dot', 'aria-hidden': 'true' }),
      versions.length > 1 ? 'Свод' : label,
      noteText ? h('span', { class: 'req-bar__note' }, noteText) : null),
    versSwitch,
    liveWord ? h('span', {
      class: 'req-bar__live', 'data-bar-live': liveState, title: liveTitle,
      role: 'status', 'aria-live': 'polite',
    }, liveWord) : null,
    h('span', { class: 'req-bar__spacer' }),
    h('button', {
      type: 'button', class: 'btn btn--secondary btn--sm req-bar__revise', 'data-bar': 'revise',
      title: 'Руководитель получит запрос, этот свод, ваше замечание и ваши ответы после этой версии',
      onclick: press('revise'),
    }, icon('comment-draft', { size: 13 }), h('span', { class: 'req-bar__btn-text' }, 'Доработать')),
    h('button', {
      type: 'button', class: 'btn btn--secondary btn--sm req-bar__copy', 'data-bar': 'copy',
      title: 'Скопировать текст свода', onclick: press('copy'),
    }, icon('copy', { size: 13 }), h('span', { class: 'req-bar__btn-text' }, 'Копировать')),
    h('div', { class: 'req-bar__more' },
      exportGroup(opts.ctx || {}, opts.rid || '', { label: 'Ещё', extra })));

  return bar;
}

export default {
  buildSvodBar, barLabel, barNote, barVersions, barLiveWord, barLiveTitle, barCurrent, BAR_H,
};
