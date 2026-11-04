/**
 * request-answer.js — блок «Свод команды»: текст ответа, версии, ожидание,
 * ошибка, пустое состояние; действия «На доработку» и «Копировать».
 *
 * Фабрика createAnswer(R) получает общий контекст страницы из request.js
 * и возвращает { buildAnswer }. Поведение прежнее: вынесено из request.js.
 */

import { h, fmtDateTime, timeAgo } from '../util.js';
import { icon } from '../icons.js';
import { fileUrl } from '../api.js';
import { emptyState, skeleton, mdBlock, copyText, promptDialog, segmented } from '../ui.js';
import {
  personName, profileName, criticOf, synthesisOf, versionsOf, latestVersion, wrapTables, devDetails,
} from './request-util.js';
import { failureInfo } from './request-parts-model.js';
import { countNoun } from '../text-ru.js';

export function createAnswer(R) {
  const { ctx, rid, root, state, render, runAction, cloneRequest, decompose } = R;

  async function revise() {
    const text = await promptDialog({
      title: 'Отправить ответ на доработку',
      label: 'Что исправить',
      placeholder: 'Например: добавь срок подачи в Роспатент и уточни MOQ у трёх поставщиков',
      multiline: true,
      submitLabel: 'На доработку',
    });
    if (text == null || !String(text).trim()) return;
    const res = await runAction('revise', { text: String(text).trim() }, { toast: 'Руководитель комнаты дорабатывает ответ' });
    if (res) state.version = null;
  }

  async function copyAnswer() {
    const s = synthesisOf(R.detail);
    const text = answerText(s, currentVersion(s));
    if (!text) return;
    try {
      await copyText(text);
      ctx.toast('Ответ скопирован', 'ok');
    } catch {
      ctx.toast('Не удалось скопировать — выделите текст вручную', 'danger');
    }
  }

  /* ── ответ ─────────────────────────────────────────────────── */

  function currentVersion(s) {
    const latest = latestVersion(s);
    return state.version && state.version < latest ? state.version : latest;
  }

  function answerText(s, version) {
    const list = versionsOf(s);
    const latest = latestVersion(s);
    if (!version || version >= latest) return String(s.text_md || '');
    const entry = list.find((v) => v.version === version);
    if (!entry) return '';
    if (entry.text_md) return String(entry.text_md);
    const node = (R.detail.tree || []).find((t) => t && t.id === entry.task_id);
    return node && node.summary_md ? String(node.summary_md) : '';
  }

  function versionSwitch(s, cur) {
    const list = versionsOf(s);
    if (list.length < 2) return null;
    try {
      return segmented({
        options: list.map((v) => ({ value: String(v.version), label: 'v' + v.version })),
        value: String(cur),
        onchange: (val) => {
          const num = Number(val);
          const latest = latestVersion(s);
          state.version = num >= latest ? null : num;
          render();
        },
      });
    } catch (err) {
      console.warn('[request] segmented:', err);
      return null;
    }
  }

  function buildAnswer() {
    const d = R.detail;
    const s = synthesisOf(d);
    const list = versionsOf(s);
    const latest = latestVersion(s);
    const cur = currentVersion(s);
    const isOld = list.length > 1 && cur > 0 && cur < latest;
    const ready = s.state === 'ready' && Boolean(s.text_md || isOld);
    const c = criticOf(d);

    const card = h('section', { class: 'card req-answer', id: 'answer', 'aria-labelledby': 'answer-title' });
    card.append(h('div', { class: 'req-answer__head' },
      h('h2', { class: 'req-answer__title', id: 'answer-title' }, 'Свод команды'),
      s.legacy ? h('span', { class: 'chip chip--muted', title: 'Ответ получен из старого формата metadata' }, 'старый формат') : null,
      h('div', { class: 'spacer' }),
      versionSwitch(s, cur)));

    if (d.status === 'failed') {
      const f = failureInfo(d);
      card.append(h('div', { class: 'req-empty' }, emptyState({
        icon: 'error',
        title: 'Ответ не получился',
        text: f.text,
        action: { label: 'Повторить', variant: 'primary', onclick: () => runAction('retry', {}, { toast: 'Перезапуск отправлен' }) },
      })));
      // Сырой лог — только по клику, для разработчика (D7).
      const dev = devDetails(f.details);
      if (dev) card.appendChild(dev);
      return card;
    }

    if (ready) {
      const entry = list.find((v) => v.version === cur) || null;
      const metaBits = [`Версия ${cur}`];
      if (entry && entry.kind === 'revision') metaBits.push('доработка');
      if (entry && entry.author) metaBits.push(personName(entry.author));
      if (entry && entry.at) metaBits.push(fmtDateTime(entry.at));

      const verRow = h('div', { class: 'req-answer__ver' },
        h('span', { class: 'req-answer__ver-text', title: (entry && entry.note) || null }, metaBits.join(' · ')),
        entry && entry.verdict === 'approve' ? h('span', { class: 'chip chip--ok' }, 'принято критиком') : null,
        entry && entry.verdict === 'revise' ? h('span', { class: 'chip chip--warn' }, 'было на доработке') : null);
      card.append(verRow);

      if (isOld) {
        card.append(h('div', { class: 'req-answer__old' },
          h('span', { class: 'grow' }, `Показана версия ${cur} из ${latest}: это не последний ответ.`),
          h('button', {
            type: 'button', class: 'btn btn--link btn--sm',
            onclick: () => { state.version = null; render(); },
          }, 'К актуальной')));
      }

      const text = answerText(s, cur);
      if (text) {
        const body = h('div', { class: 'req-answer__body' }, mdBlock(text, { fileUrl }));
        wrapTables(body);
        card.append(body);
      } else {
        card.append(h('div', { class: 'req-answer__body' },
          h('div', { class: 'banner banner--warn' },
            icon('warning', { size: 14 }),
            h('span', { class: 'banner__text' }, 'Текст этой версии не найден в задачах запроса — возможно, задача удалена.'))));
      }

      if (!isOld && (c.state === 'reviewing' || d.status === 'reviewing')) {
        card.insertBefore(
          h('div', { class: 'req-answer__notice' },
            icon('sync', { size: 12, className: 'codicon-modifier-spin' }),
            `Критик проверяет этот ответ — раунд ${c.round || 1} из ${c.max_rounds || 1}.`),
          card.children[1] || null,
        );
      } else if (!isOld && d.status === 'synthesizing') {
        card.insertBefore(
          h('div', { class: 'req-answer__notice' },
            icon('sync', { size: 12, className: 'codicon-modifier-spin' }),
            'Руководитель комнаты дорабатывает ответ — скоро появится новая версия.'),
          card.children[1] || null,
        );
      }

      card.append(h('div', { class: 'req-answer__foot' },
        h('button', {
          type: 'button', class: 'btn btn--secondary', disabled: Boolean(d.archived),
          onclick: revise,
        }, icon('comment-draft', { size: 13 }), 'На доработку…'),
        h('button', { type: 'button', class: 'btn btn--ghost', onclick: copyAnswer },
          icon('copy', { size: 13 }), 'Копировать'),
        h('a', {
          class: 'btn btn--ghost', href: `/api/export/request/${encodeURIComponent(rid)}.md`,
          download: '', title: 'Скачать ответ с шагами и деревом работ',
        }, icon('markdown', { size: 13 }), 'Скачать .md'),
        h('span', { class: 'req-answer__hint' }, entry && entry.at ? 'обновлено ' + timeAgo(entry.at) : '')));
      return card;
    }

    if (s.state === 'pending') {
      const active = (d.tree || [])
        .filter((t) => t && (t.status === 'running' || t.status === 'review') && t.assignee)
        .map((t) => profileName(t.assignee, ctx.room));
      const who = [...new Set(active)].filter(Boolean);
      const queued = d.status === 'queued';
      const waiting = d.status === 'needs_you';
      const openCount = (d.open_questions || [])
        .filter((q) => q && !q.answered && (q.for || 'mikhail') === 'mikhail').length;

      const body = h('div', { class: 'req-pending' },
        h('div', { class: 'req-pending__title' },
          h('span', { class: 'req-pending__dot', 'aria-hidden': 'true' }),
          queued ? 'Запрос ждёт разбора' : waiting ? 'Ждёт твоего ответа' : 'Команда работает над ответом'));
      // Сам вопрос живёт в «Вопросах к тебе» — здесь только якорь к нему (D5).
      if (waiting && openCount) {
        body.append(h('a', {
          class: 'req-pending__jump',
          href: '#',
          'data-jump': 'questions',
          title: 'Перейти к вопросам и ответить',
          onclick: (e) => {
            if (e && typeof e.preventDefault === 'function') e.preventDefault();
            const target = root.querySelector('[data-questions]');
            if (!target) return;
            if (typeof target.scrollIntoView === 'function') target.scrollIntoView({ block: 'start', behavior: 'smooth' });
            const field = target.querySelector('textarea');
            if (field && typeof field.focus === 'function') field.focus({ preventScroll: true });
          },
        }, `${countNoun(openCount, ['вопрос ждёт', 'вопроса ждут', 'вопросов ждут'])} вашего ответа ↓`));
      } else if (waiting) {
        body.append(h('div', { class: 'req-pending__line' }, 'Команда ждёт вашего ответа.'));
      } else {
        // Прогресс уже показан в «Ходе работы» — здесь только то, чего там нет.
        body.append(h('div', { class: 'req-pending__line' }, queued
          ? 'Руководитель комнаты ещё не разложил запрос по команде. Обычно это происходит само, когда движок запущен.'
          : (who.length ? 'Сейчас: ' + who.join(', ') : 'Движок раздаёт задачи команде.')));
        body.append(h('div', { class: 'req-pending__skel' }, skeleton(4)));
        body.append(h('div', { class: 'row', style: { marginTop: '4px' } },
          queued
            ? h('button', { type: 'button', class: 'btn btn--secondary btn--sm', onclick: (e) => decompose(e.currentTarget) },
              icon('split-horizontal', { size: 12 }), 'Разложить сейчас')
            : h('button', { type: 'button', class: 'btn btn--ghost btn--sm', onclick: () => runAction('dispatch', {}, { toast: 'Движок подтолкнут' }) },
              icon('zap', { size: 12 }), 'Подтолкнуть движок')));
      }

      card.append(body);
      return card;
    }

    // state none / missing: завершено без текста
    card.append(h('div', { class: 'req-empty' }, emptyState({
      icon: 'comment-discussion',
      title: 'Ответа нет',
      text: 'Запрос завершён, но текст ответа не сохранился. Можно повторить запрос тем же текстом или посмотреть задачи в дереве работ.',
      action: { label: 'Повторить', onclick: cloneRequest },
    })));
    return card;
  }

  return { buildAnswer };
}
