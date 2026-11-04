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
import { firstLine, criticOf, synthesisOf } from './request-util.js';

export function createHeader(R) {
  const { rid, runAction, cloneRequest, decompose } = R;

  async function archive() {
    const ok = await confirmDialog({
      title: 'Архивировать запрос?',
      text: 'Запрос и все его задачи уйдут в архив. Ответ останется в экспорте и журнале комнаты.',
      confirmLabel: 'Архивировать',
      cancelLabel: 'Отмена',
      danger: true,
    });
    if (!ok) return;
    await runAction('archive', {}, { toast: 'Запрос в архиве' });
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

  function buildMenu() {
    const s = synthesisOf(R.detail);
    const c = criticOf(R.detail);
    const ready = s.state === 'ready' && Boolean(s.text_md || s.version);
    const canCritique = c.enabled && ready;
    const items = [
      // Главный пункт меню: написать руководителю комнаты (фокус в поле диалога).
      menuItem({
        iconName: 'comment-draft', label: 'Добавить сообщение руководителю',
        onclick: () => { closeMenu(); R.focusThread?.(); },
      }),
      menuItem({ iconName: 'copy', label: 'Клонировать', onclick: () => { closeMenu(); cloneRequest(); } }),
      menuItem({ iconName: 'zap', label: 'Подтолкнуть движок', onclick: () => { closeMenu(); runAction('dispatch', {}, { toast: 'Движок подтолкнут' }); } }),
    ];
    if (R.detail.status === 'queued') {
      items.push(menuItem({
        iconName: 'split-horizontal', label: 'Разложить сейчас', hint: 'потратит токены',
        onclick: () => { closeMenu(); decompose(); },
      }));
    }
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
    items.push(menuItem({ iconName: 'output', label: 'Печать', onclick: () => { closeMenu(); window.print(); } }));
    items.push(menuItem({
      iconName: 'archive', label: 'Архивировать', danger: true,
      disabled: Boolean(R.detail.archived),
      hint: R.detail.archived ? 'Уже в архиве' : null,
      onclick: () => { closeMenu(); archive(); },
    }));
    return h('div', { class: 'popover req-menu__pop', role: 'menu', 'aria-label': 'Действия с запросом' },
      h('div', { class: 'menu' }, items));
  }

  function actionsMenu() {
    const wrap = h('div', { class: 'req-menu' });
    const btn = h('button', {
      type: 'button', class: 'btn btn--secondary', 'aria-haspopup': 'menu', 'aria-expanded': 'false',
      title: 'Действия с запросом',
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

  function buildHeader() {
    const d = R.detail;
    const meta = h('div', { class: 'req__meta' });
    // Статус в шапке — ровно один раз. Архив — тихая пометка, не второй чип.
    const items = [statusChip(d.status)];
    if (d.archived) items.push(metaItem('archive', 'в архиве'));
    items.push(metaItem('clock', 'Создан ' + fmtDateTime(d.created_at)));

    const p = d.progress || {};
    if (d.duration_s != null) items.push(metaItem('history', 'шёл ' + fmtDuration(d.duration_s)));
    else if (d.started_at && !['failed', 'done', 'archived'].includes(d.status)) {
      items.push(metaItem('history', 'идёт ' + fmtDuration(Math.max(0, Date.now() / 1000 - d.started_at))));
    }
    if (p.total) items.push(metaItem('checklist', fromCount(p.done, p.total, ['работа', 'работы', 'работ'])));
    if (d.assignee) {
      items.push(h('span', { class: 'req__meta-item', title: 'Профиль: ' + d.assignee },
        icon('organization', { size: 11 }), 'руководитель комнаты'));
    }

    items.filter(Boolean).forEach((node, i) => {
      if (i) meta.append(h('span', { class: 'req__meta-sep', 'aria-hidden': 'true' }));
      meta.append(node);
    });

    return h('header', { class: 'req__head' },
      h('div', { class: 'req__crumb' },
        h('a', { href: `#/room/${encodeURIComponent(R.roomId)}/composer` },
          icon('arrow-left', { size: 12 }), 'К запросам')),
      h('div', { class: 'req__headrow' },
        h('h1', { class: 'req__title' }, d.title || firstLine(d.text) || 'Запрос'),
        h('div', { class: 'req__headactions' }, actionsMenu())),
      meta);
  }

  return { buildHeader, closeMenu, isMenuOpen: () => Boolean(openMenu) };
}
