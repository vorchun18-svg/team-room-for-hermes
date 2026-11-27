/**
 * request-header.js — шапка страницы запроса и меню «Действия».
 *
 * Фабрика createHeader(R) получает общий контекст страницы из request.js
 * (R.detail / R.roomId — актуальные значения через геттеры) и возвращает
 * { buildHeader, closeMenu, isMenuOpen }. Поведение прежнее: вынесено из request.js.
 */

import { h, fmtDateTime, fmtDuration, fromCount } from '../util.js';
import { icon } from '../icons.js';
import { statusChip, confirmDialog } from '../ui.js';
import { criticOf, synthesisOf } from './request-util.js';
import { KICK_LABEL, kickStateFromTree, kickUsefulness, kickNow } from './kick-panel.js';
import { buildTitleBlock } from '../title-edit.js';
import { resetRequestTitles } from '../scope.js';
import { termHelp } from '../term-help.js';
// W25-L2 (R5 §7.4, §7.7, §7.9): «Фокус» и плотность — в меню «Действия», чтобы
// в шапке осталось 3 видимых органа (R5 §9 п. 7). Состояние живёт в localStorage
// (`teamroom.focus`, `teamroom.density`) и в атрибутах `#app` — оболочку L1 не трогаем.
import { applyDensity, densityLabel, nextDensity, readDensity } from '../density.js';
import { routeKeyOf, readFocusMap, writeFocus } from '../focus-mode.js';
// W29-Q7: «в архив» выполняется через общую отмену (10 с), а ★ — та же закладка,
// что видна в меню приложения.
import { undo } from '../undo.js';
import { setBookmark } from '../recents.js';
// W31-B2 (дефект 30): блок статуса задачи в шапке — тот же перенос, что у доски.
import { buildStatusBlock } from './request-status.js';

export function createHeader(R) {
  const { rid, ctx, state, render, runAction, cloneRequest, decompose } = R;

  async function archive() {
    const ok = await confirmDialog({
      title: 'Убрать задачу в архив?',
      text: 'Работа по задаче остановится: идущие задачи встанут, новые вопросы и обновления ответа не появятся. ' +
        'Ответ, решения и файлы сохранятся — 10 секунд можно передумать, потом задача вернётся кнопкой «Вернуть из архива».',
      confirmLabel: 'В архив',
      cancelLabel: 'Отмена',
      danger: true,
    });
    if (!ok) return;
    // W29-Q7: необратимое действие отложено на 10 секунд. Пока идёт отсчёт,
    // запрос жив как был — «Вернуть» ничего не откатывает, потому что нечего.
    undo.offer({
      key: 'archive:' + rid,
      text: 'Задача в архиве',
      action: async () => {
        await runAction('archive', {}, { toast: 'Задача в архиве — вернуть можно в «Архиве»' });
      },
      onUndo: () => { if (typeof ctx.toast === 'function') ctx.toast('Задача осталась как была — ничего не изменилось', 'ok'); },
    });
  }

  /** W29-P4: вернуть запрос из архива — данные целы, модели не запускаются. */
  async function restore() {
    await runAction('unarchive', {}, { toast: 'Задача возвращена — она снова в списке задач' });
  }

  /* ── меню действий ─────────────────────────────────────────── */

  let openMenu = null;

  function closeMenu() {
    if (!openMenu) return;
    const { wrap, pop, btn, onDoc, onKey } = openMenu;
    document.removeEventListener('mousedown', onDoc);
    document.removeEventListener('keydown', onKey);
    pop.remove();
    btn.setAttribute('aria-expanded', 'false');
    if (wrap && wrap.isConnected) wrap.classList.remove('is-open');
    openMenu = null;
  }

  function menuItem({ iconName, label, onclick, href, download, danger, disabled, hint }) {
    const cls = ['menu__item', danger ? 'menu__item--danger' : ''];
    const kids = [
      iconName ? icon(iconName, { size: 13 }) : null,
      h('span', { class: 'grow' }, label),
      hint ? h('span', { class: 'menu__hint' }, hint) : null,
    ];
    if (href && !disabled) {
      return h('a', { class: cls, href, download: download || null, role: 'menuitem', title: hint || null }, kids);
    }
    return h('button', {
      type: 'button', class: cls, role: 'menuitem', disabled: Boolean(disabled),
      title: disabled && hint ? hint : null,
      onclick: disabled ? null : onclick,
    }, kids);
  }

  // Недоступный пункт всегда объясняет, КОГДА он станет доступен (Д3).
  const LATER = 'Станет доступно, когда ';

  /** «Проверить доску сейчас» из меню: итог покажет «Свод» панелью. */
  async function kickFromMenu() {
    state.kick = await kickNow(ctx.api, rid);
    await R.refresh({ force: true });
    render();
  }

  /**
   * Закрепить / открепить запрос (W20-U3, P13): `PATCH /api/requests/{id} {pinned}`.
   * Меняется только реестр комнаты — текст, ответ и карточка не трогаются.
   */
  async function togglePin() {
    const want = !(R.detail && R.detail.pinned);
    try {
      const res = await ctx.api.patch(`/api/requests/${encodeURIComponent(rid)}`, { pinned: want });
      // W29-Q7: ★ в меню («Закладки») — тот же флаг `pinned`, что и на сервере:
      // одна звезда, одно место хранения, никаких двух списков.
      const saved = res && res.pinned !== undefined ? res.pinned === true : want;
      setBookmark({
        id: rid,
        roomId: R.roomId || '',
        title: (R.detail && (R.detail.title_short || R.detail.title)) || '',
      }, saved);
      window.dispatchEvent(new CustomEvent('teamroom:bookmarks', { detail: { id: rid, on: saved } }));
      if (typeof ctx.toast === 'function') {
        ctx.toast(saved
          ? 'Задача в закладках — она сверху в меню и в «Недавнем»'
          : 'Закладка снята', 'ok');
      }
      await R.refresh({ force: true });
      render();
    } catch (err) {
      if (typeof ctx.toast === 'function') {
        ctx.toast((err && err.message) || 'Не удалось изменить закрепление', 'danger');
      }
    }
  }

  /**
   * W30 §2 / QA2: «Разбить на задачи» — вход в предложение руководителя для
   * старой большой задачи. Сама кнопка ничего не создаёт: она ставит карточку
   * руководителю «подготовь план веток». Подзадачи появятся ТОЛЬКО после
   * согласия человека, модели при нажатии не запускаются.
   * Сервер идемпотентен: повтор возвращает ту же карточку (`duplicate`).
   */
  async function splitToTasks() {
    const ok = await confirmDialog({
      title: 'Разбить задачу на подзадачи?',
      text: 'Руководитель подготовит план: ветки-подзадачи, исполнители и вопросы к каждой. '
        + 'Ничего не создастся без вас — предложение появится на странице задачи, и вы решите '
        + 'по каждой ветке («Да» / «Нет» / «Свой вариант»). Модели сейчас не запускаются.',
      confirmLabel: 'Разбить на задачи',
      cancelLabel: 'Отмена',
    });
    if (!ok) return;
    const res = await runAction('split_to_tasks', {}, {});
    if (!res) return;
    if (typeof ctx.toast === 'function') {
      ctx.toast(res.duplicate
        ? 'План уже готовится — руководитель вернётся с предложением задач'
        : 'Руководитель готовит предложение задач: подзадачи появятся после вашего согласия', 'ok');
    }
  }

  function buildMenu() {
    const s = synthesisOf(R.detail);
    const c = criticOf(R.detail);
    const ready = s.state === 'ready' && Boolean(s.text_md || s.version);
    const canCritique = c.enabled && ready;
    // W29-P4: у архивного запроса действия, которые будят агентов, недоступны.
    // В меню остаётся только «Вернуть из архива» и чтение (экспорт, печать).
    if (R.detail.archived) {
      const only = [
        menuItem({ iconName: 'debug-restart', label: 'Вернуть из архива',
          onclick: () => { closeMenu(); restore(); } }),
        h('div', { class: 'menu__sep' }),
      ];
      const md = `/api/export/request/${encodeURIComponent(rid)}.md`;
      const html = `/api/export/request/${encodeURIComponent(rid)}.html`;
      only.push(menuItem({ iconName: 'markdown', label: 'Экспорт .md', href: md, download: '' }));
      only.push(menuItem({ iconName: 'globe', label: 'Экспорт .html', href: html, download: '' }));
      only.push(menuItem({ iconName: 'output', label: 'Печать', onclick: () => { closeMenu(); window.print(); } }));
      return h('div', { class: 'popover req-menu__pop', role: 'menu', 'aria-label': 'Действия с задачей' },
        h('div', { class: 'menu' }, only));
    }
    const kick = kickUsefulness(kickStateFromTree(R.detail.tree));
    // W25-L2: «Фокус» и плотность. Одним нажатием каждое (R5 §9 пп. 15, 18):
    // фокус переключается, плотность идёт по кругу компактно → обычно → просторно.
    const app = (typeof document !== 'undefined' && document.getElementById) ? document.getElementById('app') : null;
    let store = null;
    try { store = (typeof localStorage !== 'undefined' && localStorage) ? localStorage : null; } catch { store = null; }
    const focusOn = (() => {
      const map = readFocusMap(store);
      return map[routeKeyOf({ name: 'request', roomId: R.roomId, id: rid })] === true;
    })();
    const dens = readDensity(store);

    const items = [
      menuItem({
        iconName: 'target',
        label: focusOn ? 'Выйти из фокуса' : 'Фокус: только свод',
        hint: 'Ctrl + Shift + F',
        onclick: () => {
          closeMenu();
          const next = !focusOn;
          writeFocus(store, routeKeyOf({ name: 'request', roomId: R.roomId, id: rid }), next);
          if (app && app.dataset) app.dataset.focus = next ? 'on' : 'off';
        },
      }),
      menuItem({
        iconName: 'settings-gear',
        label: 'Плотность: ' + densityLabel(dens).toLowerCase(),
        hint: 'дальше — ' + densityLabel(nextDensity(dens)).toLowerCase(),
        onclick: () => {
          closeMenu();
          applyDensity(app, nextDensity(dens), store);
          // Кегль свода не меняется (R5 §7.9): CSS держит `--svod-fs` отдельно.
          render();
        },
      }),
      h('div', { class: 'menu__sep' }),
      // Главный пункт меню: написать руководителю комнаты (фокус в поле диалога).
      menuItem({
        iconName: 'comment-draft', label: 'Добавить сообщение руководителю',
        onclick: () => { closeMenu(); R.focusThread?.(); },
      }),
      menuItem({ iconName: 'copy', label: 'Клонировать', onclick: () => { closeMenu(); cloneRequest(); } }),
    ];
    // Пункт «Проверить доску сейчас» — только когда есть готовые к запуску или
    // зависшие карточки: иначе он декоративный (W13-F §8, п. 3).
    if (kick.useful) {
      items.push(menuItem({
        iconName: 'zap', label: KICK_LABEL, hint: 'не ждать минуту',
        onclick: () => { closeMenu(); kickFromMenu(); },
      }));
    }
    if (R.detail.status === 'queued') {
      items.push(menuItem({
        iconName: 'split-horizontal', label: 'Разложить сейчас', hint: 'потратит токены',
        onclick: () => { closeMenu(); decompose(); },
      }));
    }
    // W30-QA2 §3: «Разбить на задачи» — для старых больших задач с множеством
    // вопросов. Видна всегда (кроме архива): у задачи уже может быть предложение,
    // тогда сервер ответит `duplicate` и человек увидит карточку предложения.
    items.push(menuItem({
      iconName: 'split-horizontal', label: 'Разбить на задачи',
      hint: 'руководитель подготовит план; без вас ничего не создастся',
      onclick: () => { closeMenu(); splitToTasks(); },
    }));
    items.push(menuItem({
      iconName: 'verified', label: 'Запустить критика',
      disabled: !canCritique,
      hint: !canCritique
        ? LATER + (c.enabled ? 'появится ответ' : 'критика включат в настройках комнаты')
        : null,
      onclick: () => { closeMenu(); runAction('critique', {}, { toast: 'Критик проверяет ответ' }); },
    }));
    items.push(h('div', { class: 'menu__sep' }));
    const mdHref = `/api/export/request/${encodeURIComponent(rid)}.md`;
    const htmlHref = `/api/export/request/${encodeURIComponent(rid)}.html`;
    const exportHint = ready ? null : LATER + 'появится ответ';
    items.push(menuItem({ iconName: 'markdown', label: 'Экспорт .md', href: mdHref, download: '', disabled: !ready, hint: exportHint }));
    items.push(menuItem({ iconName: 'globe', label: 'Экспорт .html', href: htmlHref, download: '', disabled: !ready, hint: exportHint }));
    items.push(h('div', { class: 'menu__sep' }));
    // W20-U3 (P13): закрепление запроса — держится сверху в «Недавнем» и списках.
    items.push(menuItem({
      iconName: 'pin',
      label: R.detail.pinned ? 'Открепить задачу' : 'Закрепить задачу',
      hint: R.detail.pinned ? 'вернуть в общий порядок' : 'сверху в «Недавнем»',
      onclick: () => { closeMenu(); togglePin(); },
    }));
    items.push(menuItem({ iconName: 'output', label: 'Печать', onclick: () => { closeMenu(); window.print(); } }));
    items.push(menuItem({
      iconName: 'archive', label: 'Архивировать', danger: true,
      disabled: Boolean(R.detail.archived),
      hint: R.detail.archived ? 'Уже в архиве' : null,
      onclick: () => { closeMenu(); archive(); },
    }));
    return h('div', { class: 'popover req-menu__pop', role: 'menu', 'aria-label': 'Действия с задачей' },
      h('div', { class: 'menu' }, items));
  }

  function actionsMenu() {
    const wrap = h('div', { class: 'req-menu' });
    const btn = h('button', {
      type: 'button', class: 'btn btn--secondary', 'aria-haspopup': 'menu', 'aria-expanded': 'false',
      title: 'Действия с задачей',
    }, icon('ellipsis', { size: 14 }), 'Действия');
    wrap.append(btn);
    btn.addEventListener('click', () => {
      if (openMenu && openMenu.wrap === wrap) { closeMenu(); return; }
      closeMenu();
      const pop = buildMenu();
      wrap.append(pop);
      wrap.classList.add('is-open');
      btn.setAttribute('aria-expanded', 'true');
      const onDoc = (e) => { if (!wrap.contains(e.target)) closeMenu(); };
      const onKey = (e) => { if (e.key === 'Escape') { closeMenu(); btn.focus(); } };
      document.addEventListener('mousedown', onDoc);
      document.addEventListener('keydown', onKey);
      openMenu = { wrap, pop, btn, onDoc, onKey };
      const first = pop.querySelector('.menu__item:not([disabled])');
      if (first) first.focus();
    });
    return wrap;
  }

  /* ── шапка ─────────────────────────────────────────────────── */

  function metaItem(iconName, text) {
    return h('span', { class: 'req__meta-item' }, iconName ? icon(iconName, { size: 11 }) : null, text);
  }

  /** W29-P4: заметная кнопка архива/возврата в шапке (не только в меню «…»). */
  function archiveButton() {
    if (R.detail && R.detail.archived) {
      return h('button', {
        type: 'button', class: 'btn btn--primary btn--sm req__archive-btn',
        title: 'Вернуть задачу в обычный список — без запуска моделей',
        onclick: () => restore(),
      }, icon('debug-restart', { size: 13 }), 'Вернуть из архива');
    }
    return h('button', {
      type: 'button', class: 'btn btn--secondary btn--sm req__archive-btn',
      title: 'Не актуально → в архив: работа остановится, задачу можно вернуть',
      onclick: () => archive(),
    }, icon('archive', { size: 13 }), 'Не актуально');
  }

  /**
   * W31-B2 (дефект 27): «Разбить на задачи» — ЗАМЕТНОЙ кнопкой в шапке задачи,
   * а не пунктом меню «Действия». Пункт в меню остаётся (привычка), кнопка
   * делает то же самое: ставит карточку руководителю «подготовь план веток».
   */
  function splitButton() {
    if (R.detail && R.detail.archived) return null;
    return h('button', {
      type: 'button', class: 'btn btn--secondary btn--sm req__split-btn',
      title: 'Разбить задачу на подзадачи: руководитель подготовит план, вы решите по каждой ветке',
      onclick: () => splitToTasks(),
    }, icon('split-horizontal', { size: 13 }), h('span', { class: 'req__split-text' }, 'Разбить на задачи'));
  }

  /**
   * W31-B2 (дефект 30): строка статуса с кнопками переноса — под названием.
   * Кнопки строятся по `allowed_moves` сервера, поэтому «Принять» появляется
   * только в «На проверке», а «Вернуть в работу» — только в «Готово».
   */
  function statusRow() {
    if (!R.detail || R.detail.archived) return null;
    return buildStatusBlock({
      detail: R.detail,
      requestId: rid,
      onMove: typeof R.moveTask === 'function' ? R.moveTask : undefined,
      toast: typeof ctx.toast === 'function' ? ctx.toast : null,
    });
  }

  function buildHeader() {
    const d = R.detail;
    const meta = h('div', { class: 'req__meta' });
    // W31-B2 (дефект 30): статус задачи теперь несёт блок статуса под названием
    // (чип «В работе» / «На проверке» + кнопки переноса). Вторая подпись в мете
    // не нужна — оставляем её только там, где она добавляет смысл: архив,
    // ошибка, очередь (у них блок статуса молчит или говорит о колонке доски).
    const items = [];
    if (d.archived || d.status === 'failed' || d.status === 'queued') {
      items.push(statusChip(d.archived ? 'archived' : d.status));
    }
    if (d.archived) items.push(metaItem('archive', 'в архиве'));
    items.push(metaItem('clock', 'Создан ' + fmtDateTime(d.created_at)));

    const p = d.progress || {};
    if (d.duration_s != null) items.push(metaItem('history', 'шёл ' + fmtDuration(d.duration_s)));
    else if (d.started_at && !['failed', 'done', 'archived'].includes(d.status)) {
      items.push(metaItem('history', 'идёт ' + fmtDuration(Math.max(0, Date.now() / 1000 - d.started_at))));
    }
    if (p.total) items.push(metaItem('checklist', fromCount(p.done, p.total, ['работа', 'работы', 'работ'])));
    // W13-E §7/§9: раунд критика виден сразу в шапке (единое слово «раунд»).
    const cr = criticOf(d);
    if (cr.enabled && String(cr.state || '') !== 'off' && Number(cr.round) > 0) {
      const mode = cr.mode === 'until_approved' ? 'до одобрения' : `из ${Number(cr.max_rounds) || 1}`;
      // W20-U1 (P7): «?» у слова «раунд» — объяснение из общего словаря.
      items.push(h('span', {
        class: 'req__meta-item',
        title: 'Критик проверяет сводный ответ. Раунд — один цикл «проверил → доработали → проверили снова».',
      }, icon('shield', { size: 11 }), `критик: раунд ${Number(cr.round)} ${mode}`, termHelp('round')));
    }
    if (d.assignee) {
      items.push(h('span', { class: 'req__meta-item', title: 'Профиль: ' + d.assignee },
        icon('organization', { size: 11 }), 'руководитель комнаты'));
    }

    items.filter(Boolean).forEach((node, i) => {
      if (i) meta.append(h('span', { class: 'req__meta-sep', 'aria-hidden': 'true' }));
      meta.append(node);
    });

    return h('header', { class: 'req__head' },
      h('div', { class: 'req__headrow' },
        // W25-L1 (R5 §7.1): крошка «← К запросам» стоит В ОДНОЙ строке с названием —
        // раньше она занимала отдельную строку и съедала 24 px до содержания.
        h('div', { class: 'req__crumb' },
          h('a', { href: `#/room/${encodeURIComponent(R.roomId)}/composer` },
            icon('arrow-left', { size: 12 }), h('span', { class: 'req__crumb-text' }, 'К задачам'))),
        // W17-G §1 (B8/P5): короткое название + карандаш и замок. Полный текст
        // диктовки — в подсказке и в блоке «Запрос» ниже, не в заголовке.
        buildTitleBlock({
          getDetail: () => R.detail,
          api: ctx.api,
          toast: typeof ctx.toast === 'function' ? ctx.toast : R.toast,
          roomTitle: (R.room && R.room.title) || '',
          onSaved: () => {
            // Крошка и заголовок окна берут название из кеша — сбрасываем его.
            resetRequestTitles();
            if (typeof window !== 'undefined' && typeof window.dispatchEvent === 'function') {
              try {
                window.dispatchEvent(new CustomEvent('teamroom:request-title', {
                  detail: { id: String(R.detail.id || rid), title_short: String(R.detail.title_short || '') },
                }));
              } catch { /* старый браузер без CustomEvent */ }
            }
            render();
          },
        }),
        h('div', { class: 'req__headactions' }, splitButton(), archiveButton(), actionsMenu())),
      statusRow(),
      meta);
  }

  return { buildHeader, closeMenu, isMenuOpen: () => Boolean(openMenu) };
}
