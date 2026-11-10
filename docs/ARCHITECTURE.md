# Комната команды (Team Room) — архитектура

Автор: Василий (оркестратор). Дата: 28.10.2026. Это главный документ: все исполнители читают его первым.
Корень проекта: `C:\TeamRoom\Team\team-room\`. Заказчик — Михаил (юрист-предприниматель, диктует голосом, требователен к проверенным фактам).

## 0. Что строим и зачем

Локальное веб-приложение «Комната команды»: одна точка, где Михаил пишет ОДИН запрос (исполнителя не выбирает) → директор комнаты разбирает его по команде реальных агентов (профили Hermes со своими SOUL/скиллами/памятью) → критик проверяет → директор сводит ОДИН ответ → Михаил видит: ответ, «Дальше» (следующие шаги), вопросы к нему, решения, этап проекта, интерактивный отчёт (таблицы, графики, связи, картинки). Ответы не лежат разрозненно: каждый завершённый запрос ложится «кирпичиком» в **здание проекта** (дорожная карта этапов, решения, бэклог будущих вопросов, журнал).

Из слов Михаила (обязательно учесть):
- Комната на проект: «Лето» (директор biz-director, команда исполнителей) и «Рабочее пространство» (оркестратор — профиль `default`/Василий, исполнители lawyer, coder, surveyor). В комнату можно в любой момент добавить агента (например, coder — на постоянку).
- Запрос → директор сам раздаёт → команда отчитывается → критик → один сводный ответ. Всё в одном месте, ничего не надо искать.
- Предложения следующих шагов, решения, календарь. Перечень будущих вопросов выделен отдельно от текущей беседы.
- Видно, на каком этапе проекта мы сейчас и куда движемся.
- Только локально. Без Telegram и любых мессенджеров/облаков. Вид — минималистичный, как основное оформление Hermes Desktop.
- Ответы агентов должны уметь превращаться в наглядные интерактивные страницы (таблицы, связи, схемы, изображения), тоже локально.

## 1. Главные решения (не пересматривать без причины)

1. **Движок — существующий канбан Hermes.** Оркестрацию, очередь, диспетчер, БД НЕ пишем. Декомпозиция `hermes kanban decompose` (triage → дети, корень просыпается последним и пишет синтез), swarm (`hermes kanban swarm`) — как есть.
2. **Приложение — автономный локальный веб-сервер** (`python server/app.py`, 127.0.0.1:8777), открывается в браузере/окне рядом с Hermes. Позже его можно встроить в Desktop одной страницей-плагином (iframe) — поэтому всё — чистые HTTP+JSON, без привязки к Electron.
3. **Только stdlib Python 3.14 (`python` в PATH) + ванильный JS (ES modules), без сборки, без CDN, без pip/npm-установок.** Никакой внешней сети во время работы.
4. **Чтение канбана — напрямую из SQLite в режиме read-only** (`file:...?mode=ro`, `uri=True`); **запись — только через CLI `hermes kanban ...`** (публичный интерфейс; приложение НИКОГДА не пишет в kanban.db напрямую). Схема проверяется на старте (`/api/health.engine.schema_ok`), при дрейфе показываем диагностику, а не падаем.
5. **Свои данные — отдельно**: `data/` (JSON/JSONL, атомарная запись tmp+rename) и зеркало для людей в Vault (`C:\TeamRoom\Vault\Projects\Rooms\<комната>\STATE.md` и `Журнал.md`, по-русски, генерируется кодом, не LLM).
6. **Контракт ответа** (раздел 5) — единый, толерантный к старому формату (`next_steps` строками, `stage` словарём «область → статус»): реальный запрос `t_605613e0` уже в таком формате и должен отображаться.
7. **Безопасность**: bind только 127.0.0.1; проверка заголовка Host (127.0.0.1:порт/localhost:порт) и Origin на POST; весь пользовательский/агентский текст экранируется (никакого innerHTML с сырым вводом); HTML-отчёты агентов — только в `<iframe sandbox="allow-scripts">` с CSP `default-src 'none'; img-src 'self' data:; style-src 'unsafe-inline'; script-src 'unsafe-inline'`; файлы отдаются только из белого списка корней (раздел 7) с realpath-проверкой и чёрным списком (`.env`, `auth.json`, `*token*`, `*secret*`, `*.session`, `google_*`).
8. **Тесты и разработка НИКОГДА не трогают реальный канбан на запись.** Песочница: переменные окружения `TEAMROOM_HERMES_HOME`, `TEAMROOM_DATA`, `TEAMROOM_VAULT_DIR` (в тестах — временные каталоги). Реальные данные — только чтение (`mode=ro`). Запрещено запускать `hermes kanban decompose`/воркеров и создавать карточки с исполнителями в реальной доске (это тратит токены и будит реальных агентов).

## 2. Раскладка файлов

```
team-room/
  start-team-room.cmd            # запуск сервера + открыть браузер
  docs/                          # ARCHITECTURE.md API.md ENGINE-FACTS.md DESIGN-NOTES.md PROGRESS.md
    status/*.md                  # отчёты исполнителей (что сделано / проблемы / что нужно от других)
    critic/*.md                  # отчёты критиков волн (wave1-contract.md, wave1-front.md)
    shots/*.png                  # скриншоты для приёмки
  server/
    app.py                       # HTTP-сервер (ThreadingHTTPServer), роутинг, статика, ошибки, фоновый watcher
    config.py                    # пути/окружение/песочница, белые корни, расширения, DENY-имена
    http_util.py                 # json-ответы, ETag, проверка Host/Origin, чтение тела (MAX_BODY)
    engine_read.py               # чтение канбана (sqlite ro): RequestDetail/Summary, team_state, health, candidates
    engine_write.py              # запись через CLI: create_request, act(...), list_profiles, ensure_board
    critic_loop.py               # пост-синтез цикл критика: карточки critic/revision, вердикты, версии ответа
    contract.py                  # тело запроса/критика/доработки для карточек (текстовые контракты)
    normalize.py                 # ЕДИНАЯ нормализация metadata → канон API §3.4/§5 (legacy + v2)
    statefiles.py                # атомарная запись data/state/<room>/*.json (tmp+rename), блокировки
    registry.py                  # реестр запросов комнаты: adopted, extra_cards, ingested, retargeted
    rooms.py                     # реестр комнат, участники, контекст (data/rooms.json)
    ingest.py                    # metadata → журнал/бэклог/дорожная карта/решения; зеркало в Vault
    watcher.py                   # фоновый цикл: retarget корня на room.orchestrator, ingest, отчёты
    files.py                     # белый список, отдача файлов, «открыть в проводнике»
    reports.py                   # поиск/валидация отчётов комнаты (spec JSON и HTML)
    ics.py                       # .ics экспорт
    search.py                    # поиск по запросам/синтезам/решениям (в памяти)
    export.py                    # экспорт запроса в .md/.html (автономный файл)
  web/
    index.html                   # + dev-страницы _kit.html, _charts-demo.html, _report-demo.html
    css/{tokens,base,components,report,report-charts,view-request,view-ui}.css
    js/{app,router,api,store,util,md,md-inline,icons,ui}.js
    js/views/{request,request-parts-model,backlog-model,palette-model}.js
    js/report/{index,blocks,table,table-model,charts,charts-draw,charts-math,graph,graph-layout,md-node}.js
    fonts/ (codicon.ttf локально)
  reportkit/                     # SPEC.md validate.py export_html.py _schema.py _blocks.py _html_assets.py examples/*.report.json (+examples/assets/)
  fixtures/*.json                # 26 образцов ответов API (эталон форм; мок и тесты читают их же)
  tools/
    mock_server.py               # HTTP+CLI мока (:8778)
    mock_core.py                # константы, ApiError/Raw, маршруты и 405, критические хелперы
    mock_app.py                 # состояние в памяти, сборка ответов, живой запрос
    mock_read.py                # GET-обработчики, /api/file (белый список)
    mock_write.py               # POST/PATCH/DELETE, валидация тел действий
    mock_versions.py            # расписание фаз и форма v2 (normalize_v2)
    probe_sandbox.py            # песочница канбана + доказательство невмешательства (sha256/WAL/счётчики)
    check_fixtures.py           # сверка fixtures/*.json со схемами API.md (обязательные поля, типы, связи)
    seed_sandbox.py             # песочная БД с деревьями запросов для тестов/скриншотов
    seed_content.py             # контент сида (тексты запросов, ответы, отчёты)
    shot.py                     # скриншоты через DevTools (127.0.0.1, без сети)
  tests/                        # test_*.py (unittest), *.test.mjs (node --test), fixtures/, helpers.py
  data/                         # рантайм (rooms.json, state/<room>/..., reports/<room>/<slug>/..., uploads/, logs/)
```
README.md и USER-GUIDE.md ещё не написаны — это единственные пункты раскладки из ARCH §2, которых нет на диске.

## 3. Сущности

- **Room**: `{id, title, icon, board, tenant, orchestrator, critic|null, members:[{profile, role, note, pinned}], context_files:[путь], project_dir, mode_default:"auto"|"swarm", critic_pass:bool, created_at, archived}`. `tenant = "room:<id>"` ставится на все карточки комнаты. Seed: `cafe` (board `team`, orchestrator `biz-director`, critic `biz-critic`, members biz-lawyer/biz-ops/biz-strategy/biz-marketing/biz-cfo/biz-runner/biz-critic) и `fn` (board `team`, orchestrator `default`, members lawyer, coder, surveyor).
- **Request** = корневая карточка канбана + запись в реестре `data/state/<room>/requests.json` (`root_id`, `slug`, `created_at`, `text`, `ingested_runs`). Слаг (`r-20261028-0931-ab12`) задаётся ДО создания карточки — под него готовится папка отчётов `data/reports/<room>/<slug>/`.
- **Статус запроса** (выводится): `queued` (triage/todo без детей) → `working` (есть ready/running дети) → `reviewing` (работает карточка-критик/review) → `synthesizing` (работает корень/синтезатор) → `done`; боковые: `needs_you` (есть blocked-карточка или открытый вопрос к Михаилу), `failed` (исчерпаны попытки/crashed без дальнейшего прогресса).
- **Ответ** (synthesis): итог корня — последний завершённый run корня (`summary` + `metadata`); если нет — summary карточки-синтезатора (ребёнок исполнителя-оркестратора с заголовком «свести/сводн/synth»); если нет — `state:"pending"`. `tasks.result` пустой — не использовать.
- **Здание проекта** (на комнату, строится ingest-ом из metadata завершённых запросов): `roadmap` (фазы со статусами), `decisions`, `backlog` (next_steps + future_questions + open_questions, у каждого статус open/asked/done/dismissed), `journal` (JSONL событий), `reports` (индекс).

## 4. Python-интерфейсы модулей (чтобы исполнители работали параллельно)

Все функции получают `cfg` (объект из `config.load()`), возвращают JSON-совместимые dict/list; формы — в `docs/API.md`.

```python
# config.py
load() -> Cfg   # Cfg.hermes_home, Cfg.kanban_db(board), Cfg.data_dir, Cfg.vault_dir, Cfg.port, Cfg.allowed_roots, Cfg.hermes_cli (путь к hermes), Cfg.sandbox:bool
# engine_read.py
health(cfg) -> dict
list_roots(cfg, board, tenant=None, limit=50) -> list[dict]          # корневые карточки (без родителей-детей-логики: корни = карточки, у которых есть родители-дети по схеме декомпозиции ИЛИ созданные приложением)
request_detail(cfg, board, root_id, room=None) -> dict               # RequestDetail
request_summary(cfg, board, root_id) -> dict                         # RequestSummary (лёгкий)
team_state(cfg, board, profiles) -> list[dict]
needs_human(cfg, board, root_ids) -> list[dict]
dispatcher_status(cfg) -> dict                                       # gateway_state.json + pid-проверка
# engine_write.py
list_profiles(cfg) -> list[dict]                                     # ростер профилей с описаниями
create_request(cfg, room, slug, title, body, mode, critic) -> dict   # {root_id, warning, child_ids?}
act(cfg, board, root_id, action, **kw) -> dict                       # dispatch|comment|answer|unblock|reassign|rework|retry|archive|clone
ensure_board(cfg, slug) -> bool
# rooms.py
list_rooms(cfg) / get_room(cfg,id) / create_room(cfg,data) / update_room(cfg,id,patch) / add_member / remove_member / archive_room / seed_defaults(cfg)
# ingest.py
ingest_request(cfg, room, detail) -> dict   # идемпотентно по (root_id, run_id); возвращает {changed, added:{...}}
load_state(cfg, room_id) -> {"roadmap","decisions","backlog","journal"}
set_backlog_status(cfg, room_id, item_id, status, due=None)
write_vault_mirror(cfg, room_id)            # STATE.md + Журнал.md, русский, детерминированно
# reports.py / files.py / ics.py / search.py
find_report(cfg, room_id, slug_or_root) -> dict|None ; load_report(cfg, path) -> dict (валидированный, нормализованный)
resolve_file(cfg, path) -> Path|None (белый список) ; reveal(cfg, path)
make_ics(items:list[dict], room_title:str) -> str
build_index(cfg, room_id) / query(cfg, q, room=None) -> list[dict]
```

Фоновый **watcher** в `app.py` (поток, период 5 с, 15 с если ничего не идёт): по каждой неархивной комнате берёт последние запросы, для завершённых вызывает `ingest_request` + `write_vault_mirror`, поддерживает кэш сводок для `/api/rooms`. Ошибки не роняют поток — логируются в `data/logs/app.log` и показываются в `/api/health`.

## 5. Контракт ответа корневой карточки (кладётся агентам в тело запроса)

Тело карточки, создаваемой приложением, = текст Михаила (дословно, без правок) + огороженный блок:

```
<<<КОМНАТА-КОНТРАКТ  (относится ТОЛЬКО к финальному ответу корневой карточки; в подзадачи не копировать)
Комната: <название>. Контекст проекта (читать первым): <STATE.md>, <CONTEXT.md/файлы комнаты>.
Корневая карточка завершается kanban_complete(summary=<готовый ответ Михаилу в Markdown, по-русски, ОДИН связный текст, а не пересказ отчётов подряд>, metadata={...}).
metadata (все поля необязательны, но заполняй, что знаешь):
  next_steps:   [{"text","owner"(профиль),"due"(YYYY-MM-DD)}]   # 3–7 конкретных шагов
  open_questions:[{"text","for":"mikhail|<профиль>"}]               # что нужно решить Михаилу
  decisions:    [{"text","why","status":"proposed|accepted"}]
  stage:        {"current":"<id фазы>","phases":[{"id","title","status":"done|active|next|blocked","note"}]}
  future_questions:["…"]                                            # вопросы на будущее, отдельно от текущего ответа
  artifacts:    [{"path","title","kind"}]                           # файлы, созданные в ходе работы
  report:       "<путь к report.json или report.html>"              # интерактивный отчёт, формат — <reportkit/SPEC.md>
Если нужны таблицы/графики/связи/схемы/картинки — сделай интерактивный отчёт: запиши JSON по <reportkit/SPEC.md> в <data/reports/<комната>/<slug>/report.json>.
КОМНАТА-КОНТРАКТ>>>
```
Старый формат (`next_steps: ["строка"]`, `stage: {"legal":"…"}`) принимается: строка → `{text}`; `stage`-словарь «область→статус» → фазы со статусом по ключевым словам (готов/в работе/ждёт/не начат).

## 6. Карта экранов (минимализм Hermes: светлая/тёмная тема, тонкие границы, мало цвета, один акцент)

Хэш-маршрутизация: `#/room/<id>/<tab>[/<id>]`.
- **Левая колонка**: список комнат (иконка, название, индикаторы: идёт N работ / ждёт тебя M), «+ Комната», внизу — здоровье (диспетчер, БД), настройки, тема.
- **Шапка комнаты**: название, чип текущей фазы проекта, аватары команды, переключатель режима (Авто / Рой), индикатор диспетчера.
- **Вкладки**: 
  1. **Запрос** — крупное поле запроса (большая textarea под диктовку, Ctrl+Enter — отправить, прикрепить файлы, режим, «с критиком»), под ним лента запросов комнаты (статус-чипы, живой прогресс).
  2. **Команда** — состав, роль, кто сейчас что делает (живой статус), последняя работа, добавить/убрать участника (из ростера профилей с описаниями).
  3. **Карта проекта** — полоса этапов (фазы и статусы), решения (принято/предложено), «где мы сейчас».
  4. **Дальше** — «Ждёт тебя» (blocked + вопросы + предложенные решения, ответ прямо в строке), следующие шаги (чекбокс, срок, «в календарь .ics»), будущие вопросы («Задать» → создаёт запрос одним кликом).
  5. **Отчёты** — галерея интерактивных отчётов комнаты.
  6. **Журнал** — хронология событий комнаты, поиск.
- **Страница запроса** (`#/room/<id>/request/<root>`): сверху **Ответ** (Markdown), рядом/под ним «Дальше», «Вопросы к тебе» (с полем ответа), «Решения»; **Отчёт** (встроенный интерактивный); **Дерево работ** (карточки-дети: исполнитель, статус, время, развернуть summary/комментарии, переназначить, «на доработку» с текстом); **Артефакты** (открыть/копировать путь/в проводнике); **Хронология**; действия: клонировать, перезапустить, подтолкнуть диспетчер, экспорт .md/.html.
- **Палитра Ctrl+K**: перейти в комнату/вкладку/запрос, новый запрос, поиск. Глобальный поиск `#/search?q=`.
- Тосты и заголовок вкладки «(2) Комната команды» при готовом ответе/вопросе к Михаилу.

### Предугаданные вопросы Михаила → что отвечает приложение
| Вопрос | Где ответ |
|---|---|
| Где ответ на мой запрос? | страница запроса, блок «Ответ» вверху; в ленте — чип «готово» |
| Что дальше делать? | вкладка «Дальше» + блок на странице запроса |
| Что от меня ждут? | «Ждёт тебя» (сверху у комнаты — бейдж), ответ прямо в строке → комментарий + unblock |
| На каком мы этапе? | «Карта проекта», чип фазы в шапке |
| Кто что делает сейчас? / почему тишина? | «Команда» (живой статус), баннер «диспетчер не запущен» с кнопкой «толкнуть» |
| Что сказал критик? Можно переделать? | в дереве роль «критик» с вердиктом; кнопка «на доработку» на любой карточке |
| Хочу переназначить / перезапустить / повторить | действия в дереве и на запросе |
| Что было раньше по теме? | поиск (Ctrl+K), журнал, решения |
| Хочу в календарь | .ics для шага/решения/всех шагов; локальная повестка |
| Хочу послать кому-то результат | экспорт запроса в .md и самодостаточный .html (reportkit/export_html.py) |
| Сколько это шло / сколько задач / кто опоздал? | длительности по карточкам, прогресс N/M |
| Хочу добавить агента / завести комнату | «Команда → Добавить», «+ Комната» (ростер профилей с описаниями) |
| Это точно локально и безопасно? | README + бейдж «127.0.0.1», CSP, нет внешних запросов (проверяется тестом) |
| Как запустить? Само запустится? | `start-team-room.cmd`; автозапуск — только по просьбе (не ставим сами) |

## 7. Файлы и белый список
Разрешённые корни: `data/`, Vault (`C:\TeamRoom\Vault`), `C:\TeamRoom\Team`, `C:\TeamRoom\Agents`, `%LOCALAPPDATA%\hermes\kanban\boards\*\workspaces`. Всё остальное → 403. Допустимые типы: md, txt, json, csv, html, png, jpg, jpeg, gif, webp, svg(только как img), pdf, docx/xlsx/pptx (только скачивание). «Открыть в проводнике»: `explorer /select,<path>` только для файлов из белого списка.

## 8. Отчёты (reportkit) — черновик спецификации
Файл `report.json`: `{"version":1,"title","subtitle","blocks":[...]}`. Типы блоков (поля — в `reportkit/SPEC.md`):
`heading`, `text`(md), `callout`(tone info|success|warn|danger), `stats`(KPI), `table`(колонки типизированы: text/number/money/percent/date/status/link; сортировка, фильтр, итоги), `chart`(bar/hbar/line/area/pie/donut/stacked/scatter — чистый SVG), `graph`(узлы/связи; layered|force|radial; пан/зум/подсветка соседей), `timeline`, `phases`(полоса этапов), `compare`(варианты: плюсы/минусы/оценка), `matrix`(тепловая/SWOT), `checklist`, `kanban`(мини-доска), `gallery`(картинки по путям из белого списка), `code`, `tabs`, `accordion`, `columns`, `links`, `sources`(источники/ссылки на файлы vault). Лимиты: ≤200 блоков, ≤5000 строк таблицы. Дополнительно допускается свободный `report.html` (в sandbox-iframe). Экспорт: `reportkit/export_html.py report.json out.html` → один автономный файл (рендерер встроен).

## 9. Дизайн
Источник стилей — `%LOCALAPPDATA%\hermes\hermes-agent\apps\desktop\DESIGN.md`, `src/styles.css`, `src/themes/`. Берём ТЕ ЖЕ токены (CSS-переменные `--background --foreground --muted-foreground --accent --border --card --ring`, радиусы, шрифты, плотность, иконки codicons — шрифт копируется локально из `node_modules/@vscode/codicons`). Минимализм: никакой цветной «мишуры», статусы — маленькие чипы, акцент один, тени почти нет, много воздуха, клавиатурная навигация, адаптивность от 900 px. Язык интерфейса — русский. Тёмная/светлая по системе + ручной переключатель.

## 10. Правила для исполнителей (субагентов)
1. Читай ЭТОТ файл и `docs/API.md` (если есть), затем свою задачу. Владеешь ТОЛЬКО перечисленными в задаче файлами; чужие не правь — если нужна правка, опиши её в handoff-заметке.
2. Реальные данные канбана — только чтение. Тесты — только в песочнице (`TEAMROOM_*`). Не ставь пакеты, не трогай чужие профили/крон/настройки Hermes, не запускай воркеров/декомпозицию, не открывай браузер пользователя.
3. Python — только stdlib, `python` из PATH. JS — только ES modules, без зависимостей. Node для тестов: `%LOCALAPPDATA%\hermes\tools\node-26.12.1-win32-x64\node.exe --test`. Скриншоты: headless Chromium `%LOCALAPPDATA%\hermes\tools\chromium-1208\chrome-win64\chrome.exe --headless=new --screenshot=...` (см. `tools/shot.py`, если уже есть).
4. Весь видимый пользователю текст — по-русски; код/идентификаторы — по-английски; комментарии кратко.
5. Каждый файл ≤ ~600 строк; публичные функции — с коротким docstring/JSDoc. Не хардкодь абсолютные пути — только через `config`.
6. Завершая работу: запусти свои тесты и покажи реальный вывод; запиши `docs/status/<имя-задачи>.md`: что сделано, что проверено (команда → результат), что НЕ сделано, известные проблемы, что нужно от других. Не заявляй «работает», если не запускал.
7. Если упёрся в блокер — опиши его честно в handoff и сделай максимум вокруг него; не выдумывай результаты.

## 11. Приёмка (критик проверяет по этому списку)
A. Запуск: `python server/app.py` поднимается, `/api/health` отдаёт ok, страница открывается, консоль без ошибок.
B. Запрос из UI создаёт карточку (в песочнице), статус и дерево обновляются без перезагрузки.
C. Реальный завершённый запрос `t_605613e0` (доска `team`) отображается: ответ, шаги, этапы (legacy-формат), дерево.
D. «Дальше»/вопросы/решения/этапы попадают в здание проекта и в Vault-зеркало; повторный ingest ничего не дублирует.
E. Отчёты: все типы блоков рендерятся, экспорт в автономный HTML работает без сети.
F. Безопасность: Host/Origin, белый список файлов, XSS-тест (`<script>`/`onerror` в тексте агента не исполняется), нет внешних запросов.
G. Нет записи в реальную kanban.db тестами (проверка размера/mtime/счётчика строк до/после).
H. Дизайн: светлая и тёмная темы, нет горизонтального скролла от 900 px, контраст читаем, русские тексты без обрезки.

## 12. Решения архитектора после волны 1 (ПЕРЕКРЫВАЮТ предыдущие разделы и API.md, если есть расхождение)

Основание: `docs/ENGINE-FACTS.md` (проверено recon-ом).

1. **Корни запросов** определяются НЕ топологией (она не отличает корень от обычной карточки), а: реестр `data/state/<room>/requests.json` ∪ `tenant = room:<id>` ∪ ручной `adopt`. «Кандидаты на привязку» = карточки с событием `decomposed` в доске, которых нет в реестре (`GET /api/rooms/{id}/candidates`). Дерево строится в обе стороны: `task_links.child_id=root` (так делает decompose) ∪ `parent_id=root` (ручные ссылки).
2. **Режим только `auto`**: `triage` + `tenant room:<id>` + `created-by` + `idempotency-key=<slug>` + `--body-file`; дальше `auto_decompose` движка. `mode:"swarm"` → ошибка `bad_request` «не поддерживается»; в UI режима нет. Кнопка «Разложить сейчас» = явное действие `decompose` (LLM-вызов aux-моделью, таймаут 180 с) — разрешено только по клику пользователя. Приложение само `decompose/swarm/specify` НИКОГДА не запускает.
3. **Директор комнаты ≠ глобальный orchestrator_profile.** После события `decomposed` на корне комнаты watcher делает `assign <root> <room.orchestrator>`, если у корня другой assignee и корень не `running` (один раз, `retargeted=true` в реестре). В тело запроса вшивается строка «Команда комнаты (назначай подзадачи только этим профилям): …». Дети с assignee вне состава → `tree[].outside_room=true` и предупреждение в `warnings`; действие `reassign`.
4. **Критик — после синтеза (post-synthesis loop), без перепайки графа движка.** Когда корень `done`, у запроса `critic=true`, раунд < `max_rounds` (по умолчанию 1, максимум 2): приложение создаёт карточку-критика (`create --parent <root> --assignee <room.critic> --tenant room:<id>`), тело = исходный запрос + ответ корня + контракт вердикта: `kanban_complete(summary, metadata={"verdict":"approve|revise","issues":[{"where","problem","fix"}]})`. Вердикт `revise` → приложение создаёт карточку-доработку (`--parent <critic> --assignee <room.orchestrator>`), тело = запрос + предыдущий ответ + замечания + тот же контракт ответа. Готовая доработка = новая **версия ответа**. Карточки критика/доработки регистрируются в `requests.json → extra_cards:[{id,kind:"critic|revision",round,at}]` (ids известны приложению, дети движка не трогаются). Действие `revise {text}` (кнопка «на доработку» от Михаила) создаёт такую же карточку-доработку с его замечанием; `critique` запускает критика вручную.
5. **Версии ответа**: `RequestDetail.synthesis = {state:"pending|ready|none", version:int, text_md, author, at, source_task, legacy:bool, history:[{version, kind:"initial|revision", task_id, author, at, verdict:null|"approve"|"revise", note}]}`; показывается последняя завершённая версия. Новое поле `RequestDetail.critic = {enabled, round, max_rounds, state:"off|waiting|reviewing|approved|revised|failed", verdict, issues:[{where,problem,fix}], task_id}`. Роли дерева: `worker|critic|synthesizer|root|revision`; у элемента дерева добавлено `outside_room:bool`. Статус запроса: пока идёт критик/доработка → `reviewing`/`synthesizing`.
6. **Запись только через CLI**, чтение только sqlite `mode=ro` (подтверждено: CLI 1.5 с против 3.5 мс). В песочнице допускается прямая запись sqlite ТОЛЬКО в БД внутри `TEAMROOM_HERMES_HOME` (assert, что путь не под реальным `%LOCALAPPDATA%\hermes`) — для посева статусов `running/review`, которые CLI не ставит.
7. **Песочница** = `TEAMROOM_HERMES_HOME` (становится `kanban_home`; реальный `hermes_home` остаётся только для чтения профилей/gateway_state/конфига), `TEAMROOM_DATA`, `TEAMROOM_VAULT_DIR`. `server/config.py` уже написан архитектором — использовать, не переписывать.
8. **Каноническая нормализация metadata** — единственная реализация в `server/normalize.py` (`normalize_metadata(meta, summary_md=None) -> dict` канонической формы из API.md; понимает legacy-формат и фолбэк из заголовков markdown «Следующие шаги/Дальше»). И `engine_read`, и `ingest` обязаны импортировать её, а не дублировать.
9. Ответ API при отсутствии движка/БД — не 500, а `engine_error` с русским сообщением и диагностикой; `/api/health` всегда отвечает.
10. Реестр запросов (`data/state/<room>/requests.json`): `{"requests":[{"root_id","slug","title","text","created_at","critic":bool,"mode":"auto","adopted":bool,"extra_cards":[],"ingested":{"<run_id>":true},"retargeted":bool}]}`; владелец модуля — `registry.py` (пишет задача rooms/ingest).

