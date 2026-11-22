/**
 * recipes.js — «Рецепты» комнаты (W29B-Q1).
 *
 * Рецепт — повторяющийся процесс одной кнопкой: имя, описание, поля ввода
 * (текст, файл, срок) и заранее заданный граф шагов «роль → шаг → критик».
 * «Запустить» создаёт запрос с готовым планом и карточки шагов сразу с моделью
 * комнаты — руководитель ничего не разбирает заново (меньше токенов и разброса).
 *
 * Правила волны: у каждого пункта есть действие, пустой экран объясняет, зачем
 * он и что сделать сейчас. Всё, что выглядит ссылкой, нажимается.
 */

import { h, esc, fmtDateTime, plural } from '../util.js';
import { icon } from '../icons.js';
import { ensureCss, sectionHeader, skeleton, errorBanner, emptyState, buildDialog } from '../ui.js';

const VIEW_CSS = 'css/view-recipes.css';

/* ── чистые помощники (node --test) ───────────────────────────────────────── */

/** Подписи вида поля. */
export const FIELD_KIND_RU = { text: 'текст', file: 'файл или папка', due: 'срок' };

/** Состояние запуска по-русски. */
export function runStateRu(state) {
  return ({
    launching: 'запускается', running: 'идёт', done: 'готово',
    failed: 'ошибка', archived: 'в архиве',
  })[String(state || '')] || 'идёт';
}

/** Состояние шага по-русски. */
export function stepStatusRu(status) {
  return ({
    pending: 'ждёт предыдущий шаг', created: 'создан', running: 'в работе',
    done: 'готово', blocked: 'на паузе', failed: 'не создался',
  })[String(status || '')] || 'ждёт';
}

/**
 * Слои графа шагов: [[шаг без зависимостей], [шаги после них], …].
 * Пустой граф → []. Цикл/битая ссылка не вешают интерфейс: такие шаги
 * попадают в последний слой.
 */
export function stepLayers(steps) {
  const list = Array.isArray(steps) ? steps.filter((s) => s && s.id) : [];
  const ids = new Set(list.map((s) => String(s.id)));
  const done = new Set();
  const layers = [];
  let guard = 0;
  while (done.size < list.length && guard <= list.length + 1) {
    guard += 1;
    const layer = list.filter((s) => !done.has(String(s.id))
      && (s.depends_on || []).every((d) => !ids.has(String(d)) || done.has(String(d))));
    if (!layer.length) {
      const rest = list.filter((s) => !done.has(String(s.id)));
      layers.push(rest);
      break;
    }
    layers.push(layer);
    for (const s of layer) done.add(String(s.id));
  }
  return layers;
}

/** Роли в шаге для подписи: «юрист · team-lawyer». */
export function stepRole(step) {
  const role = String((step && step.role) || '').trim();
  const assignee = String((step && step.assignee) || '').trim();
  if (role && assignee && role !== assignee) return role + ' · ' + assignee;
  return assignee || role || 'участник';
}

/** Короткая строка «что за рецепт»: поля и шаги. */
export function recipeLine(item) {
  const fields = (item && item.fields) || [];
  const steps = (item && item.steps) || [];
  return fields.length + ' ' + plural(fields.length, ['поле', 'поля', 'полей']) + ' · '
    + steps.length + ' ' + plural(steps.length, ['шаг', 'шага', 'шагов']);
}

/* ── экран ───────────────────────────────────────────────────────────────── */

export async function mount(el, ctx = {}) {
  await ensureCss(VIEW_CSS);
  const api = ctx.api;
  const toast = typeof ctx.toast === 'function' ? ctx.toast : () => {};
  const room = ctx.room || {};
  const roomId = String((ctx.route && ctx.route.roomId) || room.id || '');
  let stopped = false;
  let data = null;
  let error = null;
  let showTrash = false;

  const root = h('div', { class: 'rcp' });
  const head = h('div', { class: 'rcp__head' });
  const body = h('div', { class: 'rcp__body' });
  const history = h('div', { class: 'rcp__history' });
  el.appendChild(root);
  root.appendChild(head);
  root.appendChild(body);
  root.appendChild(history);
  body.appendChild(skeleton(3));

  const apiGet = (path) => (api && typeof api.get === 'function' ? api.get(path) : Promise.reject(new Error('нет связи с приложением')));
  const apiCall = (method, path, payload) => {
    const fn = api && typeof api[method] === 'function' ? api[method] : null;
    if (!fn) return Promise.reject(new Error('нет связи с приложением'));
    return fn.call(api, path, payload);
  };
  const roomPath = '/api/rooms/' + encodeURIComponent(roomId) + '/recipes';

  function load() {
    body.replaceChildren(skeleton(3));
    return apiGet(roomPath)
      .then((payload) => {
        if (stopped) return;
        data = payload || { items: [], trash: [], runs: [], roles: [] };
        error = null;
        render();
      })
      .catch((err) => {
        if (stopped) return;
        error = err || { message: 'не удалось прочитать рецепты' };
        render();
      });
  }

  function render() {
    head.replaceChildren();
    body.replaceChildren();
    history.replaceChildren();
    if (error) {
      head.appendChild(sectionHeader('Рецепты'));
      body.appendChild(errorBanner(error, load));
      return;
    }
    const items = (data && data.items) || [];
    const trash = (data && data.trash) || [];
    const runs = (data && data.runs) || [];
    head.appendChild(sectionHeader('Рецепты', {
      count: items.length,
      actions: [
        h('button', { class: 'btn btn--secondary', type: 'button', onclick: () => fromRequestDialog() },
          icon('copy', { size: 13 }), 'Сохранить запрос как рецепт'),
        h('button', { class: 'btn btn--primary', type: 'button', onclick: () => editorDialog(null) },
          icon('plus', { size: 13 }), 'Создать рецепт'),
      ],
    }));
    head.appendChild(h('p', { class: 'rcp__hint small muted' },
      'Повторяющийся процесс одной кнопкой: заполнили поля — приложение создаёт запрос '
      + 'с готовым планом и карточки шагов с моделями этой комнаты. Разбирать заново ничего не нужно.'));

    if (!items.length) {
      body.appendChild(emptyState({
        icon: 'book',
        title: 'Рецептов пока нет',
        text: 'Рецепт — это порядок шагов, который вы делаете регулярно: «ответ на претензию», '
          + '«сверка накладных», «меню на неделю». Заведите его один раз — дальше запуск одной кнопкой.',
        action: [
          { label: 'Создать из запроса', onclick: () => fromRequestDialog(), variant: 'primary' },
          { label: 'Создать с нуля', onclick: () => editorDialog(null), variant: 'secondary' },
        ],
      }));
    } else {
      const list = h('div', { class: 'rcp__list' });
      for (const item of items) list.appendChild(recipeCard(item));
      body.appendChild(list);
    }

    if (trash.length) {
      const toggle = h('button', {
        class: 'btn btn--ghost btn--sm', type: 'button',
        onclick: () => { showTrash = !showTrash; render(); },
      }, (showTrash ? '▾ ' : '▸ ') + 'Корзина (' + trash.length + ')');
      body.appendChild(h('div', { class: 'rcp__trash' }, toggle));
      if (showTrash) {
        const box = h('div', { class: 'rcp__list rcp__list--trash' });
        for (const item of trash) {
          box.appendChild(h('div', { class: 'rcp-card rcp-card--trash' },
            h('div', { class: 'rcp-card__main' },
              h('div', { class: 'rcp-card__title' }, item.name),
              h('div', { class: 'rcp-card__meta small muted' }, recipeLine(item))),
            h('button', {
              class: 'btn btn--secondary btn--sm', type: 'button',
              onclick: () => restoreRecipe(item),
            }, icon('history', { size: 12 }), 'Вернуть')));
        }
        body.appendChild(box);
      }
    }

    if (runs.length) {
      history.appendChild(sectionHeader('История запусков', { count: runs.length }));
      const list = h('div', { class: 'rcp-runs' });
      for (const run of runs) list.appendChild(runRow(run));
      history.appendChild(list);
    }
  }

  function recipeCard(item) {
    const layers = stepLayers(item.steps);
    const actions = h('div', { class: 'rcp-card__actions' },
      h('button', {
        class: 'btn btn--primary btn--sm', type: 'button',
        onclick: () => launchDialog(item),
      }, icon('play', { size: 12 }), 'Запустить'),
      h('button', {
        class: 'btn btn--secondary btn--sm', type: 'button',
        onclick: () => editorDialog(item),
      }, icon('edit', { size: 12 }), 'Изменить'),
      h('button', {
        class: 'btn btn--ghost btn--sm', type: 'button', title: 'В корзину',
        onclick: () => removeRecipe(item),
      }, icon('trash', { size: 12 }), 'В корзину'));
    const chain = h('div', { class: 'rcp-card__steps' });
    layers.forEach((layer, index) => {
      const row = h('div', { class: 'rcp-card__layer' },
        h('span', { class: 'rcp-card__layer-n tnum' }, String(index + 1)),
        h('span', { class: 'rcp-card__step-text' }, layer.map((s) => s.title || stepRole(s)).join(' · ')));
      chain.appendChild(row);
    });
    return h('div', { class: 'rcp-card' },
      h('div', { class: 'rcp-card__main' },
        h('div', { class: 'rcp-card__title-row' },
          h('div', { class: 'rcp-card__title' }, item.name),
          item.builtin ? h('span', { class: 'chip chip--muted' }, 'пример') : null,
          item.source === 'request' ? h('span', { class: 'chip chip--muted' }, 'из запроса') : null),
        item.description ? h('div', { class: 'rcp-card__desc' }, item.description) : null,
        h('div', { class: 'rcp-card__meta small muted' }, recipeLine(item)),
        chain),
      actions);
  }

  function runRow(run) {
    const steps = run.steps || [];
    const failed = steps.filter((s) => s.status === 'failed');
    const row = h('div', { class: 'rcp-run' },
      h('div', { class: 'rcp-run__main' },
        h('div', { class: 'rcp-run__title' },
          h('a', {
            class: 'rcp-run__link', href: '#/room/' + encodeURIComponent(roomId) + '/request/' + encodeURIComponent(run.request_id),
            onclick: (ev) => { if (typeof ctx.navigate === 'function') { ev.preventDefault(); ctx.navigate('#/room/' + encodeURIComponent(roomId) + '/request/' + encodeURIComponent(run.request_id)); } },
          }, run.recipe_name || 'Запуск'),
          h('span', { class: 'chip chip--muted' }, runStateRu(run.state))),
        h('div', { class: 'rcp-run__meta small muted' },
          fmtDateTime(run.at) + ' · ' + steps.length + ' '
          + plural(steps.length, ['шаг', 'шага', 'шагов'])
          + (failed.length ? ' · не создались: ' + failed.length : '')),
        h('div', { class: 'rcp-run__steps small' },
          steps.map((s) => (s.title || 'шаг') + ' — ' + stepStatusRu(s.status)).join('; ')),
        failed.length ? h('div', { class: 'rcp-run__error small' },
          'Причина: ' + (failed[0].error || 'не удалось создать карточку') +
          ' Действие: проверьте доску и профили участников, затем запустите ещё раз.') : null));
    return row;
  }

  /* ── запуск ─────────────────────────────────────────────────────────── */

  function launchDialog(item) {
    const inputs = {};
    const form = h('div', { class: 'rcp-form' });
    form.appendChild(h('p', { class: 'small muted' },
      'Заполните поля — дальше приложение само создаст запрос и карточки шагов.'));
    for (const fieldItem of item.fields || []) {
      const id = 'rcp-' + String(fieldItem.id || '');
      const control = fieldItem.kind === 'due'
        ? h('input', { class: 'input', id, type: 'date' })
        : h('input', {
          class: 'input', id, type: 'text',
          placeholder: fieldItem.kind === 'file' ? 'полный путь к файлу или папке' : (fieldItem.hint || ''),
        });
      inputs[fieldItem.id] = control;
      form.appendChild(h('label', { class: 'rcp-field', for: id },
        h('span', { class: 'rcp-field__label' },
          String(fieldItem.label || fieldItem.id),
          fieldItem.required ? h('span', { class: 'rcp-field__req' }, ' *') : null),
        control,
        fieldItem.hint && fieldItem.kind !== 'file'
          ? h('span', { class: 'rcp-field__hint small muted' }, fieldItem.hint) : null));
    }
    if (!(item.fields || []).length) {
      form.appendChild(h('p', { class: 'small muted' }, 'У этого рецепта нет полей — можно запускать сразу.'));
    }
    const errorLine = h('div', { class: 'rcp-form__error small', role: 'alert' });
    form.appendChild(errorLine);

    const submit = h('button', { class: 'btn btn--primary', type: 'button' }, icon('play', { size: 13 }), 'Запустить');
    const shell = buildDialog({
      title: 'Запустить: ' + item.name,
      body: form,
      buttons: [h('button', {
        class: 'btn btn--secondary', type: 'button',
        onclick: () => shell.close(),
      }, 'Отмена'), submit],
      onCancel: () => shell.close(),
    });
    submit.addEventListener('click', () => {
      const values = {};
      for (const [id, control] of Object.entries(inputs)) values[id] = String(control.value || '').trim();
      submit.disabled = true;
      apiCall('post', roomPath + '/' + encodeURIComponent(item.id) + '/launch', { values })
        .then((res) => {
          shell.close();
          toast((res && res.message) || 'Рецепт запущен', 'ok');
          load();
        })
        .catch((err) => {
          submit.disabled = false;
          errorLine.textContent = String((err && err.message) || 'не удалось запустить');
        });
    });
  }

  /* ── редактор ───────────────────────────────────────────────────────── */

  function editorDialog(item) {
    const isNew = !item;
    const draft = {
      name: (item && item.name) || '',
      description: (item && item.description) || '',
      fields: ((item && item.fields) || []).map((f) => ({ ...f })),
      steps: ((item && item.steps) || []).map((s) => ({ ...s, depends_on: [...(s.depends_on || [])] })),
    };
    if (!draft.fields.length) draft.fields.push({ id: 'f1', label: 'Что нужно', kind: 'text', required: true, hint: '' });
    if (!draft.steps.length) draft.steps.push({ id: 's1', role: 'руководитель', title: '', instruction: '', depends_on: [] });

    const form = h('div', { class: 'rcp-form' });
    const nameInput = h('input', { class: 'input', type: 'text', value: draft.name, placeholder: 'например: ответ клиенту на претензию' });
    const descInput = h('textarea', { class: 'input', rows: '2', placeholder: 'что делает рецепт и когда им пользоваться' }, draft.description);
    form.appendChild(h('label', { class: 'rcp-field' }, h('span', { class: 'rcp-field__label' }, 'Название *'), nameInput));
    form.appendChild(h('label', { class: 'rcp-field' }, h('span', { class: 'rcp-field__label' }, 'Описание'), descInput));

    const fieldsBox = h('div', { class: 'rcp-edit-fields' });
    const stepsBox = h('div', { class: 'rcp-edit-steps' });
    const errorLine = h('div', { class: 'rcp-form__error small', role: 'alert' });

    function drawFields() {
      fieldsBox.replaceChildren(h('div', { class: 'rcp-form__subtitle' }, 'Поля ввода'));
      draft.fields.forEach((fieldItem, index) => {
        const label = h('input', { class: 'input input--sm', type: 'text', value: fieldItem.label || '', placeholder: 'подпись поля' });
        const kind = h('select', { class: 'input input--sm' },
          Object.entries(FIELD_KIND_RU).map(([value, text]) => h('option', { value, selected: (fieldItem.kind || 'text') === value ? 'selected' : undefined }, text)));
        const required = h('input', { type: 'checkbox', checked: fieldItem.required ? 'checked' : undefined });
        label.addEventListener('input', () => { fieldItem.label = label.value; });
        kind.addEventListener('change', () => { fieldItem.kind = kind.value; });
        required.addEventListener('change', () => { fieldItem.required = Boolean(required.checked); });
        fieldsBox.appendChild(h('div', { class: 'rcp-edit-row' },
          label, kind,
          h('label', { class: 'rcp-edit-check small' }, required, 'обязательное'),
          h('button', {
            class: 'btn btn--ghost btn--sm', type: 'button', title: 'Убрать поле',
            onclick: () => { draft.fields.splice(index, 1); drawFields(); },
          }, icon('close', { size: 12 }))));
      });
      fieldsBox.appendChild(h('button', {
        class: 'btn btn--secondary btn--sm', type: 'button',
        onclick: () => {
          if (draft.fields.length >= 8) return;
          draft.fields.push({ id: 'f' + (draft.fields.length + 1), label: '', kind: 'text', required: false, hint: '' });
          drawFields();
        },
      }, icon('plus', { size: 12 }), 'Поле'));
    }

    function drawSteps() {
      stepsBox.replaceChildren(h('div', { class: 'rcp-form__subtitle' }, 'Шаги: кто, что и после кого'));
      draft.steps.forEach((step, index) => {
        const role = h('input', {
          class: 'input input--sm', type: 'text', value: step.role || step.assignee || '',
          placeholder: 'роль: юрист, оператор, руководитель',
        });
        const title = h('input', { class: 'input input--sm', type: 'text', value: step.title || '', placeholder: 'что сделать в этом шаге' });
        const instruction = h('textarea', { class: 'input input--sm', rows: '2', placeholder: 'подробнее для исполнителя (не обязательно)' }, step.instruction || '');
        const depends = h('select', {
          class: 'input input--sm', multiple: 'multiple', size: '3', title: 'после каких шагов',
        }, draft.steps.filter((s) => s !== step).map((s, i) => h('option', {
          value: s.id,
          selected: (step.depends_on || []).includes(s.id) ? 'selected' : undefined,
        }, String(indexOfStep(s) + 1) + '. ' + (s.title || 'шаг'))));
        role.addEventListener('input', () => { step.role = role.value; });
        title.addEventListener('input', () => { step.title = title.value; });
        instruction.addEventListener('input', () => { step.instruction = instruction.value; });
        depends.addEventListener('change', () => {
          step.depends_on = Array.from(depends.selectedOptions || []).map((o) => o.value);
        });
        stepsBox.appendChild(h('div', { class: 'rcp-edit-step' },
          h('div', { class: 'rcp-edit-step__head' },
            h('span', { class: 'rcp-edit-step__n' }, String(index + 1)),
            role,
            h('button', {
              class: 'btn btn--ghost btn--sm', type: 'button', title: 'Убрать шаг',
              onclick: () => { draft.steps.splice(index, 1); drawSteps(); },
            }, icon('close', { size: 12 }))),
          title, instruction,
          h('label', { class: 'rcp-field rcp-field--tight' },
            h('span', { class: 'rcp-field__label small muted' }, 'Зависит от шагов (не обязательно)'),
            depends)));
      });
      stepsBox.appendChild(h('button', {
        class: 'btn btn--secondary btn--sm', type: 'button',
        onclick: () => {
          if (draft.steps.length >= 10) return;
          draft.steps.push({ id: 's' + (draft.steps.length + 1), role: '', title: '', instruction: '', depends_on: [] });
          drawSteps();
        },
      }, icon('plus', { size: 12 }), 'Шаг'));
    }

    function indexOfStep(step) { return draft.steps.indexOf(step); }
    drawFields();
    drawSteps();
    form.appendChild(fieldsBox);
    form.appendChild(stepsBox);
    form.appendChild(errorLine);

    const save = h('button', { class: 'btn btn--primary', type: 'button' }, 'Сохранить');
    const shell = buildDialog({
      title: isNew ? 'Новый рецепт' : 'Рецепт: ' + item.name,
      body: form, wide: true,
      buttons: [h('button', { class: 'btn btn--secondary', type: 'button', onclick: () => shell.close() }, 'Отмена'), save],
      onCancel: () => shell.close(),
    });
    save.addEventListener('click', () => {
      const payload = {
        name: String(nameInput.value || '').trim(),
        description: String(descInput.value || '').trim(),
        fields: draft.fields,
        steps: draft.steps.map((s) => ({
          id: s.id, role: s.role, assignee: '',
          title: s.title, instruction: s.instruction, depends_on: s.depends_on || [],
        })),
      };
      save.disabled = true;
      const call = isNew
        ? apiCall('post', roomPath, payload)
        : apiCall('patch', roomPath + '/' + encodeURIComponent(item.id), payload);
      call.then(() => { shell.close(); toast('Рецепт сохранён', 'ok'); load(); })
        .catch((err) => { save.disabled = false; errorLine.textContent = String((err && err.message) || 'не удалось сохранить'); });
    });
  }

  /* ── из запроса ─────────────────────────────────────────────────────── */

  function fromRequestDialog() {
    const list = h('div', { class: 'rcp-form' });
    const pick = h('div', { class: 'rcp-requests' });
    const errorLine = h('div', { class: 'rcp-form__error small', role: 'alert' });
    let chosen = '';
    list.appendChild(h('p', { class: 'small muted' },
      'Выберите запрос с готовыми шагами — приложение возьмёт его порядок в шаги рецепта.'));
    list.appendChild(pick);
    list.appendChild(errorLine);
    pick.appendChild(skeleton(2));
    apiGet('/api/rooms/' + encodeURIComponent(roomId) + '/requests?limit=20')
      .then((payload) => {
        const items = (payload && payload.items) || [];
        pick.replaceChildren();
        if (!items.length) {
          pick.appendChild(emptyState({
            icon: 'inbox', title: 'Запросов пока нет',
            text: 'Рецепт можно собрать и с нуля: «Создать рецепт».',
          }));
          return;
        }
        for (const item of items) {
          const btn = h('button', {
            class: 'rcp-request', type: 'button', 'data-id': item.id,
            onclick: () => {
              chosen = item.id;
              for (const other of pick.querySelectorAll('.rcp-request')) other.classList.remove('is-active');
              btn.classList.add('is-active');
            },
          }, h('span', { class: 'rcp-request__title' }, item.title || item.id),
            h('span', { class: 'rcp-request__meta small muted' }, item.status || ''));
          pick.appendChild(btn);
        }
      })
      .catch(() => {
        pick.replaceChildren(h('p', { class: 'small muted' }, 'Список запросов не загрузился — можно создать рецепт с нуля.'));
      });

    const go = h('button', { class: 'btn btn--primary', type: 'button' }, 'Сохранить как рецепт');
    const shell = buildDialog({
      title: 'Сохранить запрос как рецепт', body: list,
      buttons: [h('button', { class: 'btn btn--secondary', type: 'button', onclick: () => shell.close() }, 'Отмена'), go],
      onCancel: () => shell.close(),
    });
    go.addEventListener('click', () => {
      if (!chosen) { errorLine.textContent = 'Выберите запрос из списка.'; return; }
      go.disabled = true;
      apiCall('post', roomPath + '/from-request', { request_id: chosen })
        .then(() => { shell.close(); toast('Рецепт сохранён из запроса', 'ok'); load(); })
        .catch((err) => { go.disabled = false; errorLine.textContent = String((err && err.message) || 'не удалось сохранить'); });
    });
  }

  function removeRecipe(item) {
    apiCall('del', roomPath + '/' + encodeURIComponent(item.id))
      .then((res) => { toast((res && res.message) || 'Рецепт в корзине', 'ok'); load(); })
      .catch((err) => toast(String((err && err.message) || 'не удалось убрать рецепт'), 'error'));
  }

  function restoreRecipe(item) {
    apiCall('post', roomPath + '/' + encodeURIComponent(item.id) + '/restore', {})
      .then(() => { toast('Рецепт вернулся', 'ok'); load(); })
      .catch((err) => toast(String((err && err.message) || 'не удалось вернуть рецепт'), 'error'));
  }

  load();
  return { unmount() { stopped = true; } };
}

export default { mount };
