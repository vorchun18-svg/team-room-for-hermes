# Дизайн-система «Комнаты команды» — заметки

Дата: 28.10.2026. Отвечает за: `web/css/{tokens,base,components}.css`, `web/index.html`,
`web/js/{app,router,api,store,icons}.js`, `web/fonts/codicon.ttf`, `web/_kit.html`,
`tools/shot.py`. Демо всех компонентов — `web/_kit.html` (открывается напрямую из файла).

## 1. Откуда взяты значения

Источник — Hermes Desktop: `apps/desktop/DESIGN.md` (принципы), `src/styles.css`
(пайплайн токенов, радиусы, шрифты, тени, z-слои), `src/themes/` (палитры).
Скопирован не «внешний вид», а **сам способ считать цвет**: в Hermes почти нет
литералов — всё выводится `color-mix()`-ом из трёх «семян» фона, одного акцента
и нейтралей. Поэтому смена темы — это ~20 переменных, остальное пересчитывается.

Иконки — `@vscode/codicons` (шрифт `codicon.ttf`, лицензии рядом:
`web/fonts/codicon-LICENSE*.txt`). В CSS включён только нужный набор глифов
(~124 имени), а не все 649 — иначе файл распухает.

## 2. Три слоя токенов (`tokens.css`)

1. **Сырьё** — `--theme-*`: `--theme-foreground`, `--theme-primary` (#0053fd),
   `--theme-background-seed`, `--theme-sidebar-seed`, `--theme-card-seed`,
   `--theme-elevated-seed`, `--theme-neutral-*` (#f3f3f3 / #0d0d0e…) и «ручки» смешивания
   `--theme-mix-chrome: 92%` (dark: 74%), `--theme-fill-*-accent-mix` и т.д.
2. **Производные** — `--ui-*`: лестницы поверхностей (`--ui-bg-chrome|sidebar|editor|elevated`),
   текста (`--ui-text-primary…quaternary` = base 94/74/54/36 % через прозрачность),
   штрихов (`--ui-stroke-primary…quaternary`), заливок контролов
   (`--ui-row-hover-background`, `--ui-control-hover-background`).
3. **Имена для компонентов** — привычный набор: `--background --foreground --muted
   --muted-foreground --card --card-foreground --popover --border --input --ring
   --accent --accent-foreground --primary --primary-foreground --destructive --link`.

Компоненты используют **только слой 3** (и статусы). Если нужен новый оттенок —
заведите токен в `tokens.css`, а не hex в компоненте.

### Ключевые значения

| Токен | Светлая | Тёмная |
|---|---|---|
| `--background` | `#f3f7ff`→`#f6f7f8` (seed #f8faff 92 % + #f3f3f3) | `#141415` (seed #17171a 74 % + #0d0d0e) |
| `--sidebar` | `#f3f7ff` | `#0a0a0b` |
| `--card` | `#fcfdfe` | `#191a1c` |
| `--popover` | `#ffffff` | `#1c1c1f` |
| `--foreground` | `#17171a` @94 % | `#e7e7ea` @94 % |
| `--primary` | `#0053fd` | `#0053fd` (ссылки — `--link` `#7ba4ff`) |
| `--destructive` | `#cf2d56` | `#e75e78` |
| `--border` | акцент 16 % + base 7 % | акцент 16 % + base 7 % (base светлый) |
| статусы | ok `#1f8a65`, warn `#c08532`, danger `#cf2d56`, info `#0053fd` | ok `#55a583`, warn `#d8a95a`, danger `#e75e78`, info `#6e9bff` (через `--chart-*`/`--ui-*`) |
| графики | `--chart-1…8` | те же роли, светлее и различимее |

Тёмная тема включается `data-theme="dark"` на `<html>`; `data-theme="auto"` —
через `@media (prefers-color-scheme: dark)` (дублируется только блок сырья, ~20 строк —
производные пересчитываются сами). `?theme=dark` в URL перекрывает сохранённое
(нужно для скриншотов), выбор хранится в `localStorage['teamroom.theme']`.

### Шрифты

- `--font-sans`: `'Segoe UI', -apple-system, …, system-ui`. Hermes использует
  `'Segoe WPC'` (поставляется с Electron) — в обычном браузере его нет, поэтому
  первый в стеке Segoe UI: на Windows это тот же рисунок.
- `--font-mono`: `'Cascadia Mono', 'Cascadia Code', 'JetBrains Mono', …, Consolas`.
  JetBrains Mono в Hermes лежит в `apps/desktop/src/fonts/*.woff2`; в проект **не
  копировал** (лишние бинарники, лицензия OFL требует отдельного файла) — Cascadia
  есть на Windows 11 и метрически близка.
- `codicon` — локальный `@font-face` в `components.css`.

### Радиусы, плотность, тени

Hermes: `--radius-scalar: 0.2` → 2–3 px (почти прямые углы). У нас чуть мягче:
`--radius-xs 2 · sm 4 · md 5 · lg 7 · xl 10 · 2xl 14 · full 999`.
Высота контролов: `--control-sm 26 · md 30 · lg 34`. База текста — **13 px**
(`--text-base`), мелкий 12/11/10, заголовки 16/20/26. Тени — только
`--shadow-sm` (карточки не теним, у них hairline) и `--shadow-md` (меню) /
`--shadow-nous` (диалог, тост).

## 3. Словарь классов (`components.css`)

| Класс | Что это | Модификаторы |
|---|---|---|
| `.app` | сетка `248px + 1fr`, высота `100dvh` | `.nav-open` (мобильный сайдбар) |
| `.app__main` | колонка контента | — |
| `.sidebar` | левая колонка, `--sidebar`, hairline справа | `.sidebar__label`, `.sidebar__foot`, `.sidebar__scrim` |
| `.brand` | марка + название + «локально» | `.brand__mark/__text/__name/__sub` |
| `.nav-item` | строка комнаты/навигации (30 px) | `.nav-item--active`, `.nav-item__text`, `.nav-item__badges` |
| `.nav-badge` | счётчик в строке | `--you` (ждёт), `--live` (+ точка), `.nav-badge__dot` |
| `.health` | блок здоровья в подвале | `.health__row--ok/warn/bad`, `.health__origin` («127.0.0.1 · локально», путь данных в `title`) |
| `.topbar` | шапка: строка + вкладки + баннеры | `.topbar__row/__title/__name/__sub/__actions/__team/__banners` |
| `.tabs` / `.tab` | вкладки комнаты, подчёркивание акцента | `.tab--active`, `.tab__count` |
| `.btn` | единственная кнопка | `--primary --secondary --ghost --danger --link`, `--sm --lg --icon --block --pill` |
| `.input .textarea .select` | поля | `.textarea--lg`, `.textarea--composer`, `.select` со своей стрелкой |
| `.field` | подпись+поле+подсказка | `.field__label/__hint/__error` |
| `.checkbox` | чекбокс с подписью | — |
| `.search-field` | поле с лупой | — |
| `.card` | панель с hairline | `--flush`, `--quiet`, `.card__head/__title/__sub/__actions/__body/__foot` |
| `.chip` | статус/метка 20 px | `--ok --warn --danger --info --muted --outline --accent`, `.chip__dot(--pulse)` |
| `.avatar` | инициалы, цвет от `--h` (хеш имени) | `--sm --lg --stacked` |
| `.kbd` | клавиша | — |
| `.toasts` / `.toast` | правый нижний стек | `.toast--ok/warn/danger/info`, `.is-leaving` |
| `.banner` | полоса-предупреждение | `--warn --error --info --ok` |
| `.empty` | пустое состояние | `.empty__title/__text/__actions` |
| `.skeleton` | заглушка загрузки | `--title --line --short` |
| `.progress` | полоса 4 px | `--ok --warn --danger --indeterminate`, `.progress__bar` |
| `.table-wrap` / `.table` | плотная таблица, липкая шапка | `--dense`, `--zebra`, `.num` |
| `.popover` / `.menu` | выпадашка | `.menu__item(--danger) __hint __sep __label` |
| `.dialog-backdrop` / `.dialog` | модалка (`shadow-nous`) | `--wide`, `__head/__title/__body/__foot` |
| `.divider` | hairline | `--v` |
| `.stack` / `.row` | раскладка | `--tight --loose --top --between --wrap`; `.grow .spacer` |
| `.markdown` | вывод `js/md.js` (h1–h4, списки, таблицы, цитаты, код) | — |
| `.codicon` | иконка-глиф; размер через `font-size` | `.codicon-modifier-spin` |
| `.sr-only`, `.hidden`, `.skip-link` | доступность/служебное | — |

Утилиты текста: `.muted .small .mono .truncate .nowrap .tnum`.

## 4. Контракт вьюх

```js
// web/js/views/<name>.js
export async function mount(el, ctx) {
  // собрать DOM в el; вернуть { unmount() } — вызывается перед следующей вьюхой
  return { unmount() {} };
}
```

`ctx = { route, params, api, store, navigate(hash), toast(msg, kind), room, refresh() }`

- `route` — `{name, roomId, tab, id, query, hash, view}`;
- `params` — то же + query-параметры (`q` и т.п.);
- `api` — `web/js/api.js` (`api.get(path,{etag,signal})`, `post`, `patch`, `del`,
  `text`, `fileUrl(path)`, `poll(fn,ms,{pauseHidden,immediate})`, `ApiError`);
- `store` — `{rooms, currentRoom, toasts, theme, health}` (`.get(key?) .set(patch) .subscribe(fn)`);
- `room` — нормализованная комната из `/api/rooms` (или `null`).
- `refresh()` — перемонтировать текущую вьюху (после POST).

Маршруты: `#/` (обзор), `#/room/<id>/<tab>`, `#/room/<id>/request/<root>`,
`#/search?q=`, `#/settings`. Вкладки: `composer team roadmap backlog reports journal`.
`router.js` сам поднимает шапку/вкладки и рисует заглушку «Раздел в разработке»,
если файла вьюхи нет — вьюхи можно писать по одной.

## 5. Правила

1. **Ничего не красим вручную.** Только `var(--token)`. Нужен новый оттенок — новый токен.
2. **Одна кнопка, одно поле, один чип.** Варианты — классом-модификатором, не инлайн-стилем.
3. **Плоскость важнее коробок.** Разделяем воздухом и одним hairline (`--border`),
   не вкладываем карточку в карточку с рамкой.
4. **Цвет — только смысл.** Акцент один (`--primary`), статусы — маленькие чипы
   (`--ok/--warn/--danger/--info`), никаких цветных заливок на пол-экрана.
5. **Плотность:** строки списков 30 px, кнопки 26/30/34, таблица 6/7 px по вертикали.
6. **Клавиатура и фокус:** у всего интерактивного есть `:focus-visible`-кольцо
   (`--focus-ring`); модалки закрываются `Esc`; `Ctrl+K` — палитра.
7. **Экранирование:** только `h()`/`textContent`. `h()` вообще не принимает `innerHTML`/
   `outerHTML` (бросает ошибку) — единственный разрешённый путь HTML в проекте —
   `web/js/report/md-node.js` (`mdNode()`), и только для результата `renderMarkdown()`,
   который экранирует весь текст агента. CSP в `index.html`: `default-src 'self'`, без inline-скриптов.
8. **Адаптив от 900 px:** сайдбар становится выдвижным (`max-width: 899px`), горизонтального
   скролла нет. Строка сетки `.app` — `minmax(0, 1fr)`: иначе длинная вьюха растягивает ряд
   и низ страницы обрезается.
9. **Контраст:** текст ≥ 4.5:1, крупный текст/иконки/графика ≥ 3:1 (WCAG AA) в обеих темах.
   `--ui-text-tertiary` 62 % (было 54 %), `--ui-yellow` #996419, `--ui-green` #1b7f5e,
   в тёмной теме заданы `--ui-blue: #6e9bff` и `--ui-yellow: #c08532`. Проверять замером,
   а не на глаз.

## 6. Как добавить вьюху

1. Создать `web/js/views/<name>.js` с `export async function mount(el, ctx)`.
2. Взять данные через `ctx.api`, положить в `ctx.store` при необходимости.
3. Собрать DOM через `h()` из `web/js/util.js` (`h(tag, attrs, ...children)`:
   `class`, `dataset`, `style`-объект, `onclick`, `aria-*`; дети — строки/узлы/массивы).
4. Показывать состояния: `.skeleton` (загрузка), `.empty` (пусто), `.banner--error` (ошибка).
5. Проверить в двух темах: `python tools/shot.py "http://127.0.0.1:8777/?theme=dark#/room/cafe/<tab>" out.png --dark`.
6. Ничего не менять в `app.js` — вкладка уже есть в `TABS` (для `request` маршрут `#/room/<id>/request/<root>`).

## 7. Скриншоты

```
python tools/shot.py web/_kit.html docs/shots/kit-light.png --light --full
python tools/shot.py web/_kit.html docs/shots/kit-dark.png  --dark  --full
python tools/shot.py "http://127.0.0.1:8777/#/room/cafe/composer" docs/shots/shell-light.png --light
```

`shot.py` запускает `chromium-1208` headless со своим `--user-data-dir` и снимает
кадр по CDP. **Почему не `--screenshot`:** в этой сборке Chrome 145 флаг зависает
на старте (страница не догружается, файл не пишется — проверено на data:, file: и
http). CDP-путь даёт тот же результат и позволяет ждать `document.fonts.ready`.
`--dark` добавляет `?theme=dark` **до** `#`, чтобы не портить хеш-маршрут.

## 8. Известные ограничения

- **Отсутствующая вьюха:** `router.js` не делает HEAD, а пробует `import('./views/<name>.js')`
  и ловит отказ: файла нет → карточка «Раздел — в разработке»; в консоли остаётся
  сетевая запись 404 (ожидаемо, исчезнет, когда все вьюхи появятся). Результат кэшируется
  на сессию.
- **Палитра `Ctrl+K`** открывается только если есть `web/js/views/palette.js`
  (`export function open(ctx)` или `mount(el, ctx)`); иначе молчит.
- **`+ Комната`** вызывает `import('./views/room-dialog.js')` → `openRoomDialog(ctx)`;
  если файла нет — тост «Раздел в разработке».
- **Горячие клавиши:** `Ctrl+K` — палитра; `g`, затем `r/t/m/d/o/j` — вкладки комнаты
  (Запрос/Команда/Карта/Дальше/Отчёты/Журнал); `n` — фокус в поле запроса. Пока фокус
  в поле ввода, клавиши молчат. Окно после `g` — 1.4 с.
- **Уведомления:** флаг `localStorage['teamroom:notify'] === 'true'` + разрешение браузера.
  Включаются кнопкой «Уведомления» в подвале сайдбара (разрешение спрашивается по клику).
  При росте `counters.needs_you` или `counters.done_today` по данным опроса `/api/rooms`
  показывается системное уведомление и тост; первый опрос только запоминается.
- **Контракт API, на который рассчитывает каркас:**
  `GET /api/rooms → {rooms:[{id,title,icon,members[],mode_default,phase,counters:{working,needs_you,done_today}}]}`;
  дополнительно принимаются `counts.{working,needs_you,done}`, `summary.*` и массив в корне —
  `counters` читается первым (F-3).
  `GET /api/health → {ok, dispatcher:{running}|{status}, engine:{schema_ok}, paths?:{data,vault}}`
  (`paths` — необязательно, только для подсказки «где данные»).
- **Сигнатуры утилит (частые ошибки):**
  `fmtNumber(n, {decimals})`, `fmtPercent(x, {ratio, decimals})`, `fmtMoney(n, {currency, decimals})`,
  `plural(n, [one, few, many])`. Позиционные формы (`fmtPercent(0.42, 1, true)`,
  `plural(5, 'день', 'дня', 'дней')`) поддержаны для совместимости, но в dev-режиме
  (localhost) печатают предупреждение; неверный тип опций — `TypeError`.
- **Тема и `localStorage`:** выбор хранится в браузере ключом `teamroom.theme`
  (перебивается `?theme=`); ключ последней комнаты — `teamroom.lastRoom`.
- **CSS отчёта:** `index.html` их не подключает — `web/js/report/index.js` сам добавляет
  `<link>` на `report.css`, `report-charts.css`, `report-graph.css` (относительно
  `import.meta.url`, без дублей; в автономном HTML reportkit стили уже встроены).
- **Страничные заглушки токенов** (демо, отдельные страницы) обязаны быть в `:where(...)`:
  иначе `html[data-theme="dark"]` (0,1,1) перебивает `:root` (0,1,0) и страница живёт
  на своей палитре, а не на дизайн-системе.
- `web/js/views/*` — не мои файлы; каркас их только монтирует.

## 9. Единые компоненты

### Микрофон диктовки — один компонент на всё приложение (с 29.10.2026)

**Жёсткое правило:** новое поле или кнопка диктовки делается **ТОЛЬКО** через этот
компонент — `web/js/dictation-auto.js` (`attachMic` / `enhanceAll`) +
`web/js/dictation.js` (`createMic`) + `web/css/dictation.css`. Своих кнопок
микрофона, своих стилей `.mic-*` и своих `position: absolute` под микрофон
в отдельных файлах (вьюхах, диалогах, лентах) **не бывает**. Нужно новое место —
просто сделайте обычное поле: вьюха о диктовке ничего не пишет.

Эталон вида — кнопка в композере (`#cmp-input`, кнопка «Надиктовать» рядом с
«Отправить»). Все остальные места выглядят так же; отличается только подпись.

| Класс | Где | Как выглядит |
|---|---|---|
| `.mic-btn` (+`btn btn--secondary`) | везде | 32×32 минимум, иконка `codicon-mic` цветом `--primary`, радиус как у `.btn` |
| `.mic-btn--action` | рядом с кнопкой-действием | отдельная кнопка **перед** «Ответить»/«Отправить»/«Добавить»; в широкой строке подпись «Надиктовать» (`--wide`), в узкой — только иконка (title + aria-label) |
| `.mic-btn--compact` | внутри `.mic-field` | та же кнопка без подписи, прижата к правому краю поля |
| `.mic-field` | обёртка поля | `position: relative` у **обёртки самого поля**, `flex: 1 1 auto`, `min-width: 0`, `padding-right: 44px` у поля, `min-height: 34px` |

Правило размещения (`placement()` в `dictation-auto.js`) — одно на всё приложение:

1. **Рядом с полем в той же строке есть кнопка-действие** («Ответить», «Добавить»,
   «Отправить», «Сохранить», «Искать») → микрофон — собственная отдельная кнопка
   **перед ней**, обычным элементом строки. Никакого `absolute`: налезть на соседа
   физически нечем. (Инцидент 29.10: в «Дальше» микрофон лежал поверх букв «ть»
   в «Ответить», потому что `absolute; right: 2px` ставился относительно всей строки.)
2. **Кнопки рядом нет** (диалог комнаты, палитра Ctrl+K, настройки, указания
   участникам, поиск ленты) → поле оборачивается в `.mic-field`, кнопка встаёт
   в правом краю поля. `relative` — у обёртки, внутри которой только поле и кнопка.

Состояния одинаковы во всех местах (`data-state` на кнопке):

| `data-state` | Значок | Что видно |
|---|---|---|
| `idle` | `codicon-mic` | «Надиктовать» (в узком месте — только иконка) |
| `arming` | крутилка | «Разрешите…», кнопка неактивна |
| `recording` | красная точка | «Стоп» + таймер `0:07`, заливка `--danger-soft` |
| `transcribing` | крутилка | «Расшифровываю…» |
| `error` | `codicon-warning` | «Повторить», красная рамка |

Пометки на полях: `data-mic="manual"` — место занято явной кнопкой (композер),
`data-mic="auto"` — поставили мы, `data-mic="off"` — не ставить. Служебным полям
(путь к файлу, дата, время, поиск в шапке) `data-mic="off"` ставит сам
`enhanceAll` по правилу `noMicPlace()` — в разметке вьюх для этого ничего писать
не нужно.

Проверка компонента (обязательна после правок в диктовке):
`node --test tests/mic-layout.test.mjs` и `python tools/mic_layout.py` —
обходит все маршруты на 1280/900/420 в двух темах, меряет
`getBoundingClientRect` каждой кнопки (пересечения с кнопками и полями,
видимость целиком, цель клика ≥ 32×32, контраст подписи ≥ 4.5:1, иконки ≥ 3:1)
и пишет таблицу `docs/status/unify-mic-table.md`.

## 10. Адаптив: точки перелома и z-index

Один договор на весь каркас (с 29.10.2026). Правила живут в `tokens.css`
(токены) и `components.css` (единственный блок «АДАПТИВ» в конце файла);
вьюхи добавляют только своё, своих брейкпоинтов для каркаса не заводят.

### Точки перелома

| Ширина | Сайдбар | Шапка | Что ещё |
|---|---|---|---|
| ≥ 1100 | постоянный, 248 px (`--sidebar-width`) | 3 зоны в одну строку | всё видно: крошки, этап, «вся комната», команда, папка проекта, поиск с подписью |
| 900–1099 | постоянный, 208 px (`--sidebar-width-narrow`), без подписи «только на этом компьютере» | в одну строку | вторичное сжимается, крошки режутся `…` |
| 700–899 | выдвижной поверх (288 px), затемнение | гамбургер + крошки, мета **второй строкой** | кнопка «Закрыть» в марке, цели клика ≥ 32 px |
| 640–699 | выдвижной | поиск без «Ctrl K», этап скрыт | — |
| 560–639 | выдвижной | поиск — иконка 32×32, команда (аватары) скрыта | крошки без «уровня выше» |
| ≤ 559 | выдвижной | одна строка | диалог и палитра — на весь экран, кнопки действий внизу |

Горизонтального скролла страницы нет от 360 px: строка сетки `.app` —
`minmax(0, 1fr)`, у каждой зоны шапки `min-width: 0`, крошки — `text-overflow:
ellipsis` с полным текстом в `title`, длинные слова переносятся
(`overflow-wrap: break-word`).

### Шапка — три зоны

```
.topbar__row
├─ .nav-toggle        гамбургер (только < 900)
├─ .topbar__lead      крошки «Комната «…» › Запрос «…»» — flex: 1 1 240px, min-width: 0
├─ .topbar__meta      этап проекта, «вся комната», аватары — flex: 0 1 auto
└─ .topbar__actions   папка проекта + поиск — flex: none (не сжимаются)
```

Правая группа не сжимается никогда; на < 900 `.topbar__meta` получает
`order: 9; flex: 1 1 100%` и уезжает во вторую строку — поэтому бейдж «вся
комната» и аватары не отнимают место у названия страницы. Строка шапки —
`min-height`, не `height`: иначе вторая строка обрезается.

**Подсказка «Задайте папку проекта…» в шапке не живёт.** Она рисуется
`projectDirButton()` и попадает в `#page-hint` — полосу под вкладками
(`index.html`). В шапке это длинная фраза, которая на 560–900 px вылезала за
край и схлопывала поиск (дефект с реального экрана ~780 px).

### Сайдбар и меню

* `< 900` панель `position: fixed` + `transform: translateX(-100%)`; открывается
  классом `.app.nav-open`.
* Закрывается: гамбургером, кнопкой «Закрыть» в марке, `Esc`, кликом по
  затемнению, выбором пункта (любая ссылка `a[href^="#"]`), открытием любого
  диалога/палитры (`closeNav()` в `shell-features.js`) и расширением окна
  ≥ 900 px (`resize`).
* Класс `nav-open` меняет **только** `setNavOpen()` в `app.js` — он же держит
  `aria-expanded`/`aria-label` гамбургера.

### Шкала слоёв (`tokens.css`)

| Токен | Значение | Кто |
|---|---|---|
| `--z-sticky` | 10 | липкая шапка таблицы |
| `--z-dropdown` | 40 | меню, поповер внутри страницы |
| `--z-nav-scrim` | 90 | затемнение под выдвижным сайдбаром |
| `--z-sidebar` | 100 | сам выдвижной сайдбар |
| `--z-backdrop` | 120 | подложка диалога/палитры |
| `--z-modal` | 130 | окно диалога |
| `--z-popover` | 140 | выпадашки поверх диалога |
| `--z-toast` | 200 | тосты |
| `--z-palette` | 210 | палитра `Ctrl+K` (со своей подложкой) |

Порядок обязателен: **сайдбар ниже подложки диалога** (иначе диалог создания
комнаты оказывается «под меню» — реальный дефект), но **выше своего
затемнения**. Новый слой — новый токен, не литерал.

### Диалоги на узком экране

`≤ 559 px`: `body > .dialog-backdrop` без отступов, окно `100dvh × 100%`,
шапка и подвал не сжимаются, `.dialog__body` прокручивается внутри,
`.dialog__foot` — `position: sticky; bottom: 0` (кнопки всегда видны),
кнопки растягиваются на ширину и не ниже 36 px. Селектор с `body >` и
`!important` на размерах нужны из-за `view-*.css`, которые подключаются
динамически позже.

### Как проверить

```
node --test tests/responsive.test.mjs        # договор по файлам и поведению
python tools/mock_server.py --port 8898      # мок на своём порту
python tools/e2e_ui.py                       # 10 сценариев в Chrome
```
Таблица «маршрут × ширина × тема» — `docs/status/responsive.md`
(12 маршрутов × 9 ширин 1440…360 × 2 темы, автотест переполнения и пересечений).
