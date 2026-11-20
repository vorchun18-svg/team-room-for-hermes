# API «Комнаты команды» — контракт HTTP+JSON

Версия: 2.1 (28.10.2026; волна 5 — «done + needs_you», единый путь STATE.md, стабильный ETag; список правок — §10). Источник истины по сущностям — `docs/ARCHITECTURE.md` §3–5, 7–8 и §12 (решения после волны 1).
Этот файл — формальный контракт: по нему параллельно пишутся бэкенд (`server/`) и фронтенд (`web/`).
Эталонные ответы — `fixtures/*.json`; живой мок — `python tools/mock_server.py` (порт 8778).

## Что изменилось в v2 (для фронтенда)

Режим `swarm` снят: запрос всегда `auto`, кнопки «Рой» в интерфейсе нет.
Поле `critic` теперь **объект везде**: в `RequestSummary` — короткий `CriticRef` (`{enabled, state, verdict, round}`), в `RequestDetail` — полный `CriticBlock` (`+ max_rounds, issues, task_id`). Булева значения `critic` в API больше нет.

Что нужно отрисовать нового:

1. **Чип критика на запросе**: `critic.state` → `off` (нет), `waiting` (ещё не начинал), `reviewing` («критик проверяет»), `approved` (принял), `revised` (отправил на доработку), `failed`, а в режиме «до полного одобрения» ещё `paused` (пауза из-за лимита провайдера), `stalled` (остановлено страховкой «нет прогресса»), `capped` (достигнут потолок раундов). Рядом — `раунд N` (в ленте — `critic.round`, на странице — `critic.round` и `critic.mode`/`critic.round_cap`) и ход цикла `critic.progress`.
2. **Блок «Проверка критика»** на странице запроса: вердикт (`approve`/`revise`), список замечаний `critic.issues[]` — три колонки «где / в чём проблема / что исправить», кнопка «Запустить критика» (действие `critique`).
3. **Версии ответа**: `synthesis.version` + переключатель версий по `synthesis.history[]` (kind `initial|revision`, автор, время, вердикт, заметка). Показывать последнюю; старые — по клику. Над ответом строка «Версия 2 · доработано после замечаний критика».
4. **Кнопка «На доработку»** на весь ответ → `POST actions {action:"revise", text}`; в дереве — роль `revision` (карточка доработки) и её статус.
5. **Карточки вне состава комнаты**: `tree[].outside_room=true` — пометить предупреждающим чипом «вне комнаты» + кнопка «Переназначить» (`reassign`). Текст предупреждения — в `warnings[]` с кодом `outside_room`.
6. **Статусы `reviewing` / `synthesizing`** теперь означают именно критика и доработку: «Критик проверяет свод» / «Директор дорабатывает свод». При этом `synthesis.state="ready"` и версия 1 уже доступны — ответ можно показывать, но с пометкой «проверяется».
7. **«Разложить сейчас»** — явная кнопка на `queued`-запросе: `POST actions {action:"decompose"}`. Никакого автоматического swarm/декомпоза приложение не запускает.
8. **«Привязать карточку»** — список кандидатов `GET /api/rooms/{id}/candidates` (2 демо-карточки в `fixtures/candidates_cafe.json`), привязка — `POST /api/adopt {room, task_id}`.
9. **Комната**: новые поля `max_rounds` (1–2) и `seeded`; в настройках комнаты — селектор «раундов критика».
10. **Вторая комната в фикстурах**: `fn` («Рабочее пространство») с `team_fn.json`, `requests_fn.json` и запросом `request_outside_room.json` (ребёнок вне состава + предупреждение).

Ломающие изменения: `mode:"swarm"` → `400 bad_request` «не поддерживается»; поле `critic` стало объектом и в сводке, и в детали (`CriticRef` / `CriticBlock`); `synthesis.state:"missing"` → `"none"`.


## 0. Общие соглашения

| Правило | Значение |
|---|---|
| База | `http://127.0.0.1:8777` (мок — `:8778`). Только loopback, без TLS. |
| Формат | Все ответы — JSON UTF-8 (`application/json; charset=utf-8`), без BOM, без обёртки `data`. |
| Методы | Только `GET` и `POST`/`PATCH`/`DELETE` там, где указано. `OPTIONS` не поддерживается. |
| Время | Целые epoch-секунды (`int`), не строки. Зона в данных не хранится: сервер считает время в локальном поясе ОС, её имя — `health.app.tz` (`UTC` — только если сама ОС в UTC). |
| Id карточек | `t_` + 8 hex (`t_a1b2c3d4`). Id комнаты — `[a-z0-9][a-z0-9-]{1,31}`. |
| Id запроса | `rid` = id корневой карточки (`t_a1b2c3d4`). |
| Пустые значения | `null` для «нет данных», `[]` для пустого списка, `0` для счётчика. Поля не опускаются. |
| Пагинация | `limit` (1..100, по умолчанию 20), `cursor` — непрозрачная строка. Ответ: `{items, next_cursor, has_more}`. |
| Сортировка | Списки запросов/журнала — по убыванию времени (`created_at`, потом `id`). |
| ETag | `GET /api/requests/{rid}`, `GET /api/rooms/{id}`, `GET /api/reports/{room}/{key}` отдают `ETag: W/"<16 hex>"`. При совпадении `If-None-Match` → `304` без тела (заголовок `ETag` присутствует, `Content-Type` — нет). ETag — хэш тела ответа, поэтому в тела с ETag **нельзя** класть поля, зависящие от часов (`generated_at`, «идёт N с», `time.time()`): без изменения данных два подряд `GET` обязаны дать один и тот же ETag (проверяется тестами `test_etag_does_not_depend_on_clock`, `test_real_etags_ignore_clock` со сдвигом часов). Живые времена (`generated_at`, `updated_at` «сейчас») отдают только ответы без ETag. |
| Кэш | API: `Cache-Control: no-store`; детали с ETag: `Cache-Control: no-cache`. |
| Заголовки безопасности | `X-Content-Type-Options: nosniff`, `Referrer-Policy: no-referrer`, `X-Frame-Options: SAMEORIGIN`. |

`If-None-Match` понимает: точное значение, список через запятую, `*`. Слабый/сильный префикс `W/` игнорируется при сравнении.
Тело запроса — только JSON (`Content-Type: application/json`), максимум **8 МиБ (8 388 608 байт)**; иначе `413` → `bad_request` (`detail.length`). Лимит задан в `server/http_util.MAX_BODY` и отдаётся в `GET /api/config → limits.body_bytes_max` (исключение — `POST /api/stt/transcribe`: там тело — сырые байты аудио, 64 МиБ, §6.9).
Пределы текста (проверяются до записи, `400 bad_request`, `detail.field`): `text` запроса — **1..1 000 000** символов (`app_common.MAX_TEXT`), комментарий/заметка/ответ/доработка — **1..200 000** (`app_common.MAX_NOTE`); оба значения отдаются в `GET /api/config → limits.request_text_max|note_text_max`. Пустая строка и строка из пробелов — `400`.

## 1. Ошибки

Любой не-2xx ответ имеет ровно такую форму:

```json
{"error": {"code": "not_found", "message": "Запрос не найден", "detail": {"rid": "t_deadbeef"}}}
```

| `code` | HTTP | Когда |
|---|---|---|
| `bad_request` | 400 (также 405, 413, 415) | неверный JSON/поля/параметры, неизвестное действие, недопустимый статус, неподдерживаемый режим (`swarm`) |
| `forbidden` | 403 | чужой `Host`/`Origin`, путь вне белого списка, файл из чёрного списка |
| `not_found` | 404 | нет комнаты, запроса, карточки, отчёта, файла, элемента бэклога |
| `conflict` | 409 | состояние не позволяет действие: уже в архиве, вопрос уже отвечен, участник уже в комнате, синтез ещё не готов |
| `engine_error` | 502 (500 для внутренних сбоев) | ошибка канбана/CLI/БД, таймаут движка, повреждённая схема |

`message` — по-русски, пригодно для показа пользователю. `detail` — объект (не `null`), содержит машинные подробности: `{"field": "text"}`, `{"rid": "..."}`, `{"stderr": "..."}`.
Тело ошибки никогда не содержит HTML и неэкранированный пользовательский текст.

## 2. Безопасность транспорта

* `Host` обязателен и должен быть одним из: `127.0.0.1:<port>`, `localhost:<port>`, `[::1]:<port>`. Иначе `403 forbidden` (`detail.reason = "bad_host"`).
* Для `POST/PATCH/DELETE` проверяется `Origin`: если заголовок есть, он должен быть `http://127.0.0.1:<port>` или `http://localhost:<port>`; иначе `403` (`detail.reason = "bad_origin"`). Отсутствующий `Origin` (curl, тесты) допускается.
* `GET /api/file` — только из белых корней (`data/`, `C:\TeamRoom\Vault`, `C:\TeamRoom\Team`, `C:\TeamRoom\Agents`, `%LOCALAPPDATA%\hermes\kanban\boards\*\workspaces`) после `realpath`; чёрный список: `.env`, `auth.json`, `*token*`, `*secret*`, `*.session`, `google_*`. Нарушение → `403 forbidden`. С `?room=<id>` к корням добавляются папка проекта этой комнаты (после проверки, что она не системная и не внутри Hermes) и её зеркало в Vault — для файлов карточки (WAVE19, B-22).
* HTML-отчёты и пользовательский HTML отдаются только в `<iframe sandbox="allow-scripts">`; сервер добавляет к `text/html` заголовок `Content-Security-Policy: default-src 'none'; img-src 'self' data:; style-src 'unsafe-inline'; script-src 'unsafe-inline'`.
* `POST /api/open` вызывает `explorer /select,<path>` только для путей из белого списка.

## 3. Схемы данных

Все схемы — «плоские» объекты; незнакомые клиенту поля нужно игнорировать (контракт расширяемый).

### 3.1 `Health` — `GET /api/health`

```json
{
  "ok": true,
  "app": {"name": "team-room", "version": "1.0.0", "port": 8777, "started_at": 1793171000,
          "uptime_s": 900, "sandbox": false, "tz": "Europe/Moscow"},
  "engine": {"ok": true, "warning": null, "board": "team",
             "db": "C:/Users/User/AppData/Local/hermes/kanban/boards/team/kanban.db",
             "schema_ok": true, "schema_version": 3,
             "tasks": {"total": 61, "done": 54, "blocked": 2, "active": 0},
             "cli": {"ok": true, "path": "C:/Users/User/AppData/Local/hermes/bin/hermes"}},
  "dispatcher": {"ok": true, "state": "running", "pid": 9608, "warning": null, "updated_at": 1793171850},
  "data": {"dir": "C:/TeamRoom/Team/team-room/data", "writable": true, "rooms": 2, "requests": 7},
  "vault": {"dir": "C:/TeamRoom/Vault", "ok": true},
  "watcher": {"running": true, "last_run_at": 1793171890, "last_error": null},
  "warnings": []
}
```

`ok=false`, если недоступна БД канбана или `schema_ok=false`; HTTP всё равно `200`, UI показывает диагностику.
`dispatcher.state`: `running | stopped | stale | unknown`. `stale` — pid из `gateway_state.json` не существует.
`warnings[]` — `{code, message, detail}`; коды: `schema_drift`, `dispatcher_down`, `cli_missing`, `vault_missing`, `watcher_error`.

**Единая форма `engine`.** Наружу всегда уходит **плоский** блок `engine` (как выше): `{ok, warning, board, db, schema_ok, schema_version, tasks{total,done,blocked,active}, cli{ok,path}}`. Диагностика добавляет необязательные `schema_missing: string[]` (`table:tasks`, `column:tasks.tenant`) и `gateway: {running, pid, state, age_s}`. Вложенный вариант `{ok, engine:{…}}` — внутренний формат `engine_read.health`; `app.py` его нормализует, в API он не выходит. `schema_ok=true` → `schema_version` — целое (сейчас `3`: движок не публикует номер, это версия схемы, под которую написан `engine_read`; `PRAGMA user_version` у доски `0`). `null` — только если схему не удалось распознать (`schema_ok=false`).

### 3.2 `Room`, `RoomSummary`

`Room` (полная):

| Поле | Тип | Описание |
|---|---|---|
| `id` | string | `cafe`, `fn` |
| `title` | string | «Лето (demo)» |
| `icon` | string | имя codicon (`cake`, `law`) |
| `board` | string | слаг доски канбана (`team`) |
| `tenant` | string | `room:<id>` — метка всех карточек комнаты |
| `orchestrator` | string | профиль-директор (`team-director`) |
| `critic` | string \| null | профиль-критик (`team-critic`) |
| `members` | `Member[]` | участники комнаты, **кроме директора**; критик входит сюда же с `role:"critic"` (дублирует `room.critic`) |
| `context_files` | string[] | абсолютные пути, читаются агентом первыми |
| `project_dir` | string | папка проекта комнаты: абсолютный путь Windows или `""` (§6.2.1) |
| `mode_default` | `auto` | всегда `auto`: `swarm` не поддерживается (ARCH §12.2) |
| `critic_pass` | bool | «с критиком» по умолчанию |
| `max_rounds` | int | 1..2, сколько раундов доработки допускает критик (по умолчанию 1); в режиме `until_approved` не используется |
| `critic_mode` | `rounds` \| `until_approved` | режим проверки критиком (волна 13, §9): `rounds` — не больше `max_rounds` раундов, `until_approved` — до полного одобрения. Нет поля — `rounds` |
| `critic_round_cap` | int | 2..30, мягкий потолок раундов для `until_approved` (по умолчанию 12): по достижении цикл останавливается с отчётом |
| `launch_mode` | `plan_first` \| `auto` | «Проектный режим» **этой** комнаты: `plan_first` — направления ждут кнопки «Запустить» (по умолчанию), `auto` — стартуют сразу после карты (A1) |
| `max_parallel_streams` | int | 1..4, сколько направлений идёт одновременно; остальные ждут очереди (по умолчанию 2, A2) |
| `project_mode` | bool | «Проектный режим» включён: главный запрос → карта **частей работы**. У новых комнат `true`, у записей без поля — `false` |
| `phases` | `{id, title}[]` \| `null` | Этапы проекта **этой** комнаты (W17-R §1, B-8): необязательный список, порядок = порядок показа; `id` — стабильный слаг, по нему связываются статусы. `[]` — этапов нет (полосы этапов и чипа «этап» не будет); `null` — поля нет в записи, этапы подтягиваются миграцией чтением из `data/state/<room>/roadmap.json` (только если там есть реальные статусы/заметки); данные при этом не переписываются |
| `agent_notes` | `{profile: {text, updated_at, entries[], review}}` | особенности участникам **этой** комнаты (§6.2.2): записи `{id, text, source, origin, created_at, used_count, pinned}`, `text` — склейка; файлы профиля не меняются; нет поля — `{}` |
| `agent_models` | `{profile: {provider, model, reasoning, updated_at}}` | модель/провайдер/мышление участника **только в этой комнате** (§6.11); нет записи — настройки агента |
| `seeded` | bool | комната создана сидом при первом запуске (а не пользователем) |
| `created_at` | int | epoch |
| `archived` | bool | в архиве |

`Member`: `{profile, role: "worker"|"critic"|"observer", note, pinned: bool, added_at: int}`. `pinned` — «всегда в составе» (меняется `PATCH …/members/{profile}`); у руководителя и критика закрепления нет — они в комнате по настройкам, в интерфейсе это текст «обязательная роль».
`role` — роль в комнате. Директор (`orchestrator`) в `members` не входит; критик входит с `role:"critic"` и дублирует `room.critic` (это позволяет держать в комнате второго критика с той же ролью). `POST /api/rooms/{id}/members` не принимает директора и текущего `room.critic` — они меняются через `PATCH`.

`RoomSummary` = `{id, title, icon, archived, orchestrator, critic, members_count, project_dir, mode_default, critic_pass, max_rounds, critic_mode, critic_round_cap, launch_mode, max_parallel_streams, project_mode, phases, counters, phase, updated_at}`:

* `counters` = `{working, needs_you, done_today}` — живые счётчики запросов комнаты.
  `working` — статусы `working|reviewing|synthesizing`; `needs_you` — запросы со статусом `needs_you` **и** запросы со статусом `done`, у которых `needs_you:true` (завершены, но Михаилу остался вопрос; см. §4); `done_today` — `done` c `finished_at` внутри текущих суток по поясу сервера (запрос `done + needs_you` попадает в оба счётчика — это сознательно: он и завершён сегодня, и ждёт ответа).
* `phases` = `{id, title}[]` — этапы этой комнаты (та же миграция чтением, что в `Room`); `[]` — этапов нет.
* `phase` = `{id, title, status}` — текущий этап (та же логика, что `roadmap.current`); `null`, если этапов у комнаты нет.
* `RoomDetail` = `Room` + `counters`, `phase`, `dispatcher` (`{ok, state, warning}`).

### 3.3 `RequestSummary`

| Поле | Тип | Описание |
|---|---|---|
| `id` | string | `t_a1b2c3d4` (корневая карточка) |
| `room` | string | id комнаты (для привязанной через `adopt` карточки — из реестра, даже если `tenant` у неё `NULL`) |
| `title` | string | полный заголовок запроса (первая строка диктовки, как раньше) |
| `title_short` | string | **короткое название** (W17-B, ≤ 60 знаков): `title_source="user"` (переименование) → `metadata.title` руководителя (`leader`) → название малой модели Hermes (`llm`) → мгновенное `derived` (первая осмысленная строка без вводных «Так,…», по границе слова, с многоточием). Полный текст — в `title`, исходная диктовка — в `text` (§3.4). Везде, где показывается название, интерфейс берёт `title_short`, полный — в подсказку |
| `title_source` | enum | `derived` \| `llm` \| `leader` \| `user` — откуда взято название (B9 усилено, §4 W17-B). Приоритет: `derived` < `llm` < `leader` < `user` |
| `title_locked` | bool | `true` — название задал пользователь (`PATCH /api/requests/{id} {title}`); автоматика и модель его не перезаписывают. `PATCH` с пустой строкой снимает блокировку |
| `status` | enum | см. §4 |
| `status_reason` | string \| null | короткое объяснение («ждёт ответа Михаила по Q2») |
| `mode` | `auto` | как разбирали — всегда `auto` |
| `critic` | `CriticRef` | короткий блок критика: `{enabled, state, verdict, round}` (см. ниже); в `RequestDetail` одноимённое поле расширено до `CriticBlock` (§3.4) |
| `created_at` | int | создание |
| `started_at` | int \| null | взятие корня в работу; если корень ещё не взят (движок часто держит его в `todo`, пока работают дети) — минимальный `started_at` детей; `null`, пока никто не начинал |
| `finished_at` | int \| null | завершение всего цикла (корень + критик + доработка); `null`, пока идёт критик/доработка |
| `duration_s` | int \| null | `finished_at - started_at`; `null`, пока запрос не завершён (живое время UI считает сам) |
| `progress` | object | `{done, total, percent}` по карточкам-детям |
| `assignee` | string \| null | исполнитель корня; если корень не назначен — директор комнаты (запрос ведёт он) |
| `needs_you` | bool | **флаг «ждёт тебя»**, независимый от `status`: `true`, если `status == "needs_you"`, **или** есть неотвеченный вопрос `for="mikhail"` в последней версии ответа, **или** есть blocked-карточка с `needs_human=true`. У `done`-запроса с открытым вопросом `status` остаётся `done`, а `needs_you:true` (см. §4) |
| `preview` | string | первые 200 символов текста запроса, без markdown |
| `archived` | bool | корень в архиве |

`progress.total` — число потомков корня (все карточки дерева, кроме самого корня); `done` — из них в статусе `done|archived`;
`percent` = `100`, если `status=done`; `0`, если `total=0`; иначе `floor(100*done/total)`.

`CriticRef` (только сводка — для чипов в лентах и счётчиках):

| Поле | Тип | Описание |
|---|---|---|
| `enabled` | bool | у запроса включён критик (реестр `requests.json → critic`) |
| `state` | `off \| waiting \| reviewing \| approved \| revised \| failed` | то же значение, что в `CriticBlock.state` |
| `verdict` | `null \| "approve" \| "revise"` | последний вердикт |
| `round` | int | текущий/последний раунд (0 — критик ещё не начинал) |

`CriticRef` — ровно эти четыре ключа; `max_rounds`, `issues`, `task_id` есть только в `RequestDetail.critic` (`CriticBlock`, §3.4). Фронтенд не должен читать `critic.issues` из ленты.

### 3.4 `RequestDetail`

`RequestDetail` = `RequestSummary` + поля ниже.

| Поле | Тип | Описание |
|---|---|---|
| `text` | string | исходный текст запроса (дословно) |
| `dispatcher` | object | `{ok: bool, warning: string\|null}` на момент ответа |
| `synthesis` | `Synthesis` | сводный ответ: последняя версия + история |
| `critic` | `CriticBlock` | пост-синтез проверка (ARCH §12.4–12.5) |
| `next_steps` | `NextStep[]` | 3–7 шагов (+ связь с вопросом и метки ответа, W14-D §4) |
| `open_questions` | `OpenQuestion[]` | вопросы Михаилу/профилю (+ статус ответа и реакция руководителя, W14-D §1–§3) |
| `answers` | `{items, summary, pending_label, waiting_label, conflict_label, cancelled_label}` | сводка ответов на вопросы (W14-D): «вопрос — ваш ответ», статусы, конфликты шагов |
| `decisions` | `Decision[]` | решения (действия «Принять/Отклонить/Обсудить», статус и цепочка ответа — W19-D2 §1–§2) |
| `stage` | `Stage` | этап проекта |
| `future_questions` | `string[]` | вопросы «на будущее» |
| `artifacts` | `Artifact[]` | файлы работы |
| `report` | `ReportRef` | интерактивный отчёт |
| `tree` | `TaskNode[]` | дерево карточек, корень первым |
| `timeline` | `TimelineEvent[]` | хронология запроса (по возрастанию `at`). **Без `heartbeat`** по умолчанию (W17-B): 117 из 214 событий боевого запроса были «признаком жизни». `?include=heartbeat` (через запятую) возвращает их |
| `heartbeats` | `{count: int, last_at: int\|null}` | сводка скрытых heartbeat: сколько их и когда был последний (W17-B). Поле есть всегда, даже когда `timeline` без них |
| `thread` | `ThreadItem[]` | диалог по запросу (§6.3, «Диалог по запросу»; необязательное поле: без него клиент работает по `timeline`) |
| `warnings` | `Warning[]` | мягкие проблемы чтения/нормализации |
| `brief` | bool | запрос — главный (бриф проекта): руководитель выдал карту направлений (§6.10). Необязательное, по умолчанию `false` |
| `plan_link` | `{stream_id, title, status}` \| null | запрос — под-запрос направления карты: какое направление его породило (§6.10). `null` у обычных запросов |
| `streams_summary` | `{total, running, done, status}` \| null | сводка карты для страницы брифа: сколько направлений, сколько идёт/готово, статус карты. `null` у под-запросов |

`RequestDetail` = `RequestSummary` + поля выше. Единственное исключение: одноимённое поле `critic` расширено — в сводке `CriticRef` (4 ключа), в детали `CriticBlock` (7 ключей, включает `CriticRef`). Оба — объекты; `bool` не принимается нигде.

**Поля запроса детали (W17-B).** `GET /api/requests/{rid}` принимает два параметра:

| Параметр | Значения | Смысл |
|---|---|---|
| `include` | `heartbeat` (через запятую) | вернуть скрытые по умолчанию события `heartbeat` в `timeline` |
| `fields` | имена блоков через запятую | вернуть только перечисленные блоки сверх ядра `RequestSummary`. Имена: `text`, `body_full`, `dispatcher`, `synthesis`, `steps` (`next_steps`), `questions` (`open_questions`), `answers`, `decisions`, `stage`, `future_questions`, `artifacts`, `report`, `tree`, `timeline`, `thread`, `warnings`, `critic`, `heartbeats`. Неизвестное имя → `400 bad_request` (`detail.allowed`). Пример: `?fields=questions,answers,steps,timeline,thread` — страница без `tree` и `synthesis` |

Дублирующее поле `questions` (полная копия `open_questions`, 14,9 КБ боевого ответа) **убрано** из детали (W17-B): источник один — `open_questions`. Прежний ключ отдаётся только при явном `?fields=questions` для старых клиентов.

`ETag` считается по уже урезанному ответу: без `heartbeat` он не «дрожит» от признаков жизни — `304` работает, пока не изменились настоящие данные.

`Synthesis`:

| Поле | Тип | Описание |
|---|---|---|
| `state` | `ready \| pending \| none` | `pending` — ещё пишется; `none` — завершён, но текста нет (прежнее имя `missing` принимается на чтение) |
| `version` | int | номер последней **завершённой** версии; `0`, пока ответа нет |
| `text_md` | string | markdown последней версии (`""` при `pending`/`none`) |
| `author` | string \| null | профиль автора последней версии |
| `at` | int \| null | время последней версии |
| `source_task` | string \| null | карточка-источник последней версии (корень или доработка) |
| `legacy` | bool | ответ получен из старого формата metadata |
| `history` | `SynthesisVersion[]` | все версии по возрастанию номера; последняя = `version` |
| `live` | `SynthesisLive` | **W20-SVOD** (§1.1, B-36): живой свод — `state`, `behind`, `text` |

`SynthesisVersion` = `{version: int, kind: "initial"|"revision"|"leader", task_id: string|null, source_task?: string, author: string|null, at: int|null, verdict: null|"approve"|"revise", note: string, reason?: string, answers_count?: int, changes?: string[], auto?: true}`.
`kind` — `initial` (первый свод корня) или `revision` (доработка после критика или замечания Михаила). `verdict` — вердикт, из-за которого появилась следующая версия (`revise`), либо `approve`, если версия принята критиком; иначе `null`. `note` — короткая русская заметка («Учтены три замечания критика»).

**W17-S §3 (1.0.3):** у версии есть причина и список изменений —
`reason` = `initial | note | answers | revision` (`note` — «по вашему замечанию», `answers` — «после ваших ответов и уточнений»), `note` для `reason="note"` содержит текст замечания Михаила (из реестра карточки доработки, до 400 знаков), `answers_count` — сколько ответов/уточнений было в дайджесте этой доработки, `changes` — «что изменилось» из `metadata.changes` руководителя (или из реестра, если metadata пустая; до 10 пунктов, каждый ≤ 300 знаков). Интерфейс подписывает версию причиной и показывает `changes` списком; запасной путь без `changes` — разница по заголовкам разделов, которую считает клиент (`web/js/views/request-answer.js`).

`synthesis.at` может быть позже `finished_at` только у старых записей; в v2 `finished_at` — конец всего цикла, включая доработку.

`CriticBlock`:

| Поле | Тип | Описание |
|---|---|---|
| `enabled` | bool | был ли критик у запроса (`false` — проверки нет) |
| `round` | int | текущий/последний раунд (0 — критик ещё не начинал) |
| `max_rounds` | int | лимит раундов из комнаты (1..2) |
| `state` | `off \| waiting \| reviewing \| approved \| revised \| failed \| paused \| stalled \| capped` | `off` — критик выключен (или вердикт закрытой карточки не распознан); `waiting` — критик включён, но проверки ещё не было (карточки критика нет или она не начиналась); `reviewing` — проверяет; `approved` — принял; `revised` — отправил на доработку; `failed` — карточка критика упала (на `status` запроса не влияет, см. §4); `paused` — пауза из-за лимита провайдера (волна 13, §9); `stalled` — цикл остановлен страховкой «нет прогресса»; `capped` — достигнут мягкий потолок раундов |
| `verdict` | `null \| "approve" \| "revise"` | последний вердикт |
| `issues` | `{where, problem, fix}[]` | замечания критика; пусто при `approve` |
| `task_id` | string \| null | карточка-критик (`t_…`), `null` пока её нет |
| `mode` | `rounds \| until_approved` | режим проверки: реестр запроса → комната (волна 13, §9) |
| `round_cap` | int | мягкий потолок раундов для `until_approved` (2..30) |
| `progress` | `{round, rounds, issues_before, closed, open, closed_items[], open_items[], verdict, stop}` | ход цикла: «раунд N, замечаний было K, закрыто M, осталось R»; `closed_items` — ключи закрытых замечаний (`где · проблема`, нижний регистр) |
| `paused` | `{kind, round, until, attempt, reason}` \| null | активная пауза при лимите провайдера; истёкшая пауза уже `null` |
| `stopped` | `{kind: "no_progress"\|"cap", round, message, needs_user, at, issues?, cap?}` \| null | остановка цикла страховкой; `needs_user: true` — ответ выдан как есть и ждёт решения Михаила |
| `rounds_history` | `{round, verdict, issues[], at}[]` | история раундов из реестра запроса (`critic_rounds`), по возрастанию номера |

`issues[]` — русский текст: `where` («Раздел «Что сделать», п. 3»), `problem` (что не так), `fix` (что сделать). UI рисует их таблицей или списком; больше 10 не приходит.

`NextStep`: `{id, text, owner: string|null, due: "YYYY-MM-DD"|null, state, from_question, question_link, question, answer, state_label, cancelled_by_answer, cancelled_reason, conflicts_with_answer, can_remove}`; `state` — `open|asked|done|dismissed` (то же, что статус элемента бэклога) или `cancelled` (снят ответом Михаила).
`id` стабилен в пределах запроса: `ns_<hash8>` от текста шага (порядок из metadata).
Связка с вопросом (W14-D §4): `from_question` — id вопроса из metadata (`"from_question": "q_…"`), `question_link` = `explicit|text|null`, `question = {id, text}|null`, `answer` — текст ответа на этот вопрос.
Метки ответа: `cancelled_by_answer: true` + `state_label="снят по вашему ответу"` (ответ «нет/не нужно» снял шаг); `conflicts_with_answer: true` + `state_label="противоречит вашему ответу"` (шаг появился ПОСЛЕ ответа-отказа) — у такого шага `can_remove=true` (кнопка «Убрать» → `step_drop`); `state="dismissed"`, `state_label="убран"` — Михаил убрал шаг сам.

**Действия пользователя по шагу (W14-H §12).** Шаг — план руководителя: статус ведут руководитель и система, у пользователя галочек нет. Но он может влиять на любой шаг — действия уходят руководителю **как ответ на этот шаг** (тот же механизм, что у вопросов):
`user_action = {kind: agree|comment|move|drop, at, text, due, owner, direction, label}|null`, `user_action_label` («согласовано вами», «прокомментировано вами», «перемещено вами», «убрано вами»), `reaction = {text, at, status: received|applied|no_change|moved|removed, label, cards}|null` (из ветки `[Руководитель · реакция на шаг ns_x] …`), `reaction_label` — видимая подпись («принято», «учтено: работа пересмотрена», «срок изменён», «порядок изменён», «шаг убран»), `reaction_pending=true` + `reaction_label="руководитель ещё не ответил"` (реакции ещё нет), `reaction_waited_s`, `reaction_overdue`.
`history[] = {at, kind, label, text, due, owner, direction}` — история шага (действия пользователя и реакция руководителя); `can_agree`/`can_drop` — доступны ли действия; `due_changed_by_user`/`owner_changed_by_user` — срок/исполнитель изменены пользователем («Переместить»); `order` — позиция после перемещения (`steps_order` в реестре).

`OpenQuestion`: `{id, text, for: string, answered: bool, answer: string|null, answered_at: int|null, answer_state, answer_label, needs_explanation, explanation_requested_at, explanation_text, reaction, leader_pending, can_remind, reaction_waited_s, reaction_overdue, reminded_at, answer_changes_done, aliases: string[]}`.
`for` — `"mikhail"` или профиль. `id` — `q_<hash8>` от текста вопроса.
`aliases` — id-близнецы того же вопроса (W17-B §7, B7-2): руководитель при повторной выдаче плана создаёт
тот же вопрос с новым id (боевые `q_22b45cc3`/`q_18b309bf`, `q_d47fe5e7`/`q_d5b4abf0`,
`q_789a8dce`/`q_2a6cce56`). Вопросы сравниваются по нормализованному тексту (регистр, пробелы, знаки,
префиксы «Не к спеху:», скобочные уточнения). В `open_questions` такой вопрос один, остальные id — в
`aliases`; ответ, данный на любой из id, закрывает все. `answered`/`answer_state` считаются по реестру
ответов — он единственный источник истины статуса.
`answer_state` — цепочка статуса ответа (W19-D2 §2, B-21): `open` (нет ответа) | `waiting_explanation` (встречный вопрос: **вопрос не закрыт**) | `sent` — «ответ отправлен» | `received` — «руководитель прочитал» | `applied` — «учтено: изменено N карточек» | `no_change` — «учтено: менять ничего не нужно». `answer_label` — то же по-русски (`state_label` подставляет число карточек: «учтено: изменено 2 карточки»); `answer_hint` — подсказка «что значит» для этого состояния; `answer_chain_hint` — расшифровка всей цепочки.
Исход реакции задаёт **руководитель** в метаданных своей карточки: `metadata.reaction = {question_id|decision_id, outcome: "read"|"applied"|"no_change", cards?: ["t_…"], changes?: int, text?}` (первая дорога чтения, `answers.reactions_from_tree`). Комментарий реакции без исхода (`[Руководитель · реакция на ответ q_x] …` или с меткой `outcome=…`) — запасная дорога для старых записей: тогда `reaction.guessed=true`.
`reaction = {text, at, status, label, cards: t_…, changes, outcome, guessed, source: "metadata"|"thread", author, author_label, implicit, task_id}|null`.
`leader_pending=true` + `can_remind=true` → «руководитель ещё не ответил» и кнопка «Напомнить руководителю» (`answer_remind`); `reaction_waited_s` — сколько секунд ждём, `reaction_overdue=true` — реакции нет дольше «разумного времени» (180 с). `answer_changes_done=true` — ответ изменил уже сделанное (в реакции есть карточки).

`Decision` (W19-D2 §1, B-20): `{id, text, why: string|null, status: "proposed"|"accepted"|"rejected"|"discuss", at: int|null, source_request: string, open: bool, status_label, status_hint, status_source: "user"|"leader", answered: bool, answer_action: "accept"|"reject"|"discuss"|null, answer_action_label, answer_text: string|null, answered_at: int|null, answer_state, answer_label: string|null, answer_hint, reaction, leader_pending: bool, can_remind: bool, reaction_waited_s, reaction_overdue: bool, actions: [{action, label, hint, current}]}`. `id` — `d_<hash8>` от текста.
`open=true` — решение ждёт слова Михаила (`proposed` — ещё не отвечено, `discuss` — он попросил обсудить, слово всё ещё за ним). «Принять»/«Отклонить» ставят `status=accepted|rejected`, `open=false`, и решение **сразу уходит** из «Нужен ваш ответ» и из счётчиков (деталь запроса, вкладка «От вас нужно», боковое меню) — по данным реестра ответов, без перезаписи файлов.
`status_source` — кто закрыл решение: `user` (Михаил кнопкой) или `leader` (руководитель отметил в metadata); у второго `status_label` — «принято/отклонено руководителем».
`answer_state`/`answer_label`/`answer_hint`/`reaction` — та же цепочка, что у вопросов (см. `OpenQuestion`): «ответ отправлен → руководитель прочитал → учтено (изменено N карточек / менять ничего не нужно)». Запись ответа лежит в реестре запроса: `answers[d_x] = {kind: "decision", decision_id, decision_text, action, outcome, text, user_text, at, answered, status: "sent"}`.
`decision_actions` (на уровне детали) — `{labels, hints, status_labels, chain_hint}`: подписи действий и расшифровка цепочки одним источником, чтобы интерфейс не придумывал свои слова.

`Stage`: `{current: string|null, phases: StagePhase[]}`; `StagePhase` = `{id, title, status: "done"|"active"|"next"|"blocked", note: string}`.
**Этапы принадлежат комнате** (W17-R §1, B-8): список, порядок и заголовки — из `Room.phases`; списка этапов в коде нет. У комнаты без этапов `stage` — `{current: null, phases: []}` (полоса этапов и чип «этап» не показываются).
Статус и `note` берутся из metadata ответа: этап комнаты сопоставляется с этапом из ответа по `id`, а если не совпал — по названию (старые ответы называли этапы словами). Этапы, которых комната не объявила, в ответ не попадают.
`current` = `current` ответа, если он есть среди этапов комнаты; иначе первая фаза со статусом `active|blocked`, иначе последняя `done`, иначе первая `next`, иначе `null`.

`Artifact`: `{path, title, kind, exists: bool, size: int|null, state: "ok"|"missing"|"outside"|"denied", outside_project: bool, reason: string|null, mtime: int|null, group: "final"|"material"|"service", folder: string, newer_than_svod: bool}`.
`kind`: `md|json|html|csv|image|pdf|docx|xlsx|pptx|other` (по расширению; неизвестное → `other`). `size` — байты, `null` если файла нет. `mtime` — время изменения файла (секунды), `null` если файла нет.
`group`/`folder`/`newer_than_svod` и элемент-папка `{kind:"folder", title, count, items}` — W20-A1 (§1.4, B-43), см. раздел «W20-ARTIFACTS».

**Где ищется файл (WAVE19, B-22).** Для файлов запроса корни — общие (`data/`, Vault, `Team`,
`Agents`, `workspaces`, `TEAMROOM_EXTRA_ROOTS`) **плюс папка проекта ЭТОЙ комнаты** (`Room.project_dir`,
только после проверки `project_dir.check_dir`: системные каталоги, каталог Hermes, каталог программы и
`data/` не допускаются) **и зеркальная папка комнаты в Vault** (`<vault_dir>/<room.id>`).
Раньше папка проекта не входила в белый список, и файлы внутри неё показывались как «файл не найден»,
хотя лежали на диске.

`state`:
* `ok` — файл есть и лежит в разрешённом месте;
* `missing` — файла нет по этому пути: «Файл удалён или перемещён» (не «недоступен»);
* `outside` — файл есть, но сохранён **вне** папки проекта и вне зеркала комнаты: `exists=true`,
  `outside_project=true`, в `warnings` — `artifact_outside` («Файл сохранён вне папки проекта: …»),
  в журнал комнаты добавляется одна запись на файл (`action: "artifact_outside"`). Файл не блокируется
  и не переносится — он открывается, но с предупреждением;
* `denied` — файл есть, но открыть нельзя (имя или тип в чёрном списке): `exists=false`, `reason`,
  предупреждение `artifact_denied`.
`reason` — готовая фраза для интерфейса (`null` при `state="ok"`).

`ReportRef`: `{available: bool, kind: "spec"|"html"|null, path: string|null, title: string|null}`.
`path` — абсолютный путь к `report.json`/`report.html`; `available=false` при `kind=null,path=null,title=null`.

`TaskNode`:

| Поле | Тип | Описание |
|---|---|---|
| `id` | string | `t_…` |
| `parent_ids` | string[] | родители (для корня — `[]`) |
| `title` | string | заголовок карточки |
| `assignee` | string \| null | профиль |
| `role` | `worker\|critic\|synthesizer\|root\|revision` | роль в дереве |
| `outside_room` | bool | `assignee` не входит в состав комнаты — карточка «вне комнаты» (есть предупреждение `outside_room`) |
| `status` | enum | `triage\|todo\|ready\|running\|review\|blocked\|scheduled\|done\|archived` |
| `started_at` | int \| null | |
| `finished_at` | int \| null | |
| `summary_md` | string | последний summary или `""` |
| `metadata` | object | сырые metadata последнего run'а (`{}` если нет) |
| `comments` | `{author, text, at}[]` | по возрастанию времени |
| `runs` | `Run[]` | попытки выполнения (сырые записи движка) |
| `attempts` | `Attempt[]` | попытки карточки «по-человечески»: итог, причина, модель, провайдер, число вызовов (W28-C2, см. ниже) |
| `needs_human` | bool | карточка ждёт человека |
| `error` | `{code, message}\|null` | ошибка **по итогу карточки** (W28-C2): `null`, если карточка завершилась успешно, даже когда её первые попытки были убиты контролем моделей |

`Run` = `{id: int, profile, status, outcome, started_at, ended_at, summary?, metadata?, error: string|null}`;
`summary` и `metadata` попытки **опускаются**, если дословно повторяют `summary_md`/`metadata` самой
карточки (W17-B §5: в дереве боевого запроса это было 30 КБ копий). Фронтенд берёт свод карточки из
узла, из попытки — статус, профиль, время и ошибку.
`status`: `running|done|failed|crashed`; `outcome`: `completed|failed|crashed|timed_out|gave_up|reclaimed|null`.

**`attempts[]` — попытки карточки (W28-C2, «ошибки, которых нет»).** Одна запись на попытку:

`Attempt` = `{n: int, outcome: "completed"|"reclaimed"|"crashed"|"failed"|"timed_out"|"gave_up"|…,
reason: string, model: string, provider: string, calls: int|null, started_at: int|null,
ended_at: int|null, duration_s: int|null, guard: bool, ok: bool, final: bool}`.

* `n` — номер попытки с 1 (в интерфейсе «Попытка 1»);
* `reason` — причина по-русски, когда она известна без толкования техники: убитая контролем
  моделей попытка — «остановлена контролем моделей: была не та модель», ручной `reclaimed` —
  «остановлена и возвращена в очередь»; у сбоя — исходный текст ошибки движка (интерфейс
  переводит его сам, `errorReason`);
* `model`, `provider` — ФАКТ этой попытки из сессии её воркера (`sessions.model` /
  `sessions.billing_provider`), а не из карточки: у убитой первой попытки в карточке уже стоит
  исправленный `model_override`, и подпись из карточки врала бы. `provider` пустая строка, если
  в сессии он не записан (выдумывать провайдера нельзя — «ollama-cloud · claude-sonnet-5-5»);
* `calls` — `sessions.api_call_count` этой попытки (`0` — попытку убили до первого запроса,
  `null` — сессии нет и число неизвестно);
* `guard` — попытку остановил контроль моделей (`reclaimed` + причина «контроль моделей…»);
* `ok` — попытка завершилась успешно; `final` — последняя попытка (её итог = итог карточки).

`error` карточки ставится ТОЛЬКО по итогу: `crashed/failed/timed_out/gave_up` последней
решающей попытки, если после неё не было `completed`. Убитая контролем моделей попытка
(`reclaimed`) ошибкой не считается, в счётчик «ошибок» не входит и предупреждения `card_failed`
не создаёт. Счётчик «ошибок» в интерфейсе считается по `error` карточек, а не по числу
неудачных попыток.

`TimelineEvent` = `{at, kind, task_id, actor, text}`; `kind`: `created|decomposed|promoted|claimed|started|commented|completed|blocked|unblocked|failed|archived|heartbeat`.
`actor` — профиль, `"mikhail"` или `"system"`; `text` — короткая русская строка.
`heartbeat` («Признак жизни») — технический шум: по умолчанию не отдаётся, его счётчик и время — в `RequestDetail.heartbeats` (W17-B).

`Warning` = `{code, message, detail?}`; коды: `legacy_metadata`, `missing_artifact`, `no_synthesis`, `report_invalid`, `schema_drift`, `partial_ingest`, `dispatcher_down`, `card_failed`, `outside_room` (ребёнок назначен профилю вне состава комнаты), `critic_failed` (карточка критика упала), `critic_rounds_exhausted` (лимит раундов исчерпан, ответ отдаётся как есть), `artifact_outside` (файл сохранён вне папки проекта и вне зеркала комнаты — WAVE19 B-27), `artifact_denied` (файл есть, но его имя или тип открывать нельзя — WAVE19 B-22).

### 3.5 `TeamMember`, `Roadmap`, `BacklogItem`, `ReportIndexItem`, `JournalEvent`, `SearchHit`, `Profile`

`TeamMember` = `{profile, title, role, note, pinned, state, current, last, counts, name, job, label, label_source, custom_label, description, label_pending}`:
* `role`: `orchestrator | worker | critic | observer`.
* `name`, `job`, `label` — подпись участника (WAVE13 §11): имя и должность из описания профиля Hermes. `label` = готовая строка «Анна — финансы и экономика (CFO)»; `name`/`job` пустые, если описания нет.
* `label_source`: `member` (подпись задана в комнате) | `describe` (`hermes profile describe`) | `profile` (описание из `profile.yaml`) | `role` (описания нет — интерфейс показывает роль).
* `custom_label`: ручная подпись участника (поле `label` в составе комнаты, а у руководителя и критика — `Room.role_labels[profile]`); `""` — не задана (WAVE17 B-17).
* `description`: описание профиля для короткой строки под заголовком карточки.
* `label_pending`: `true` — ответ `hermes profile describe` ещё не пришёл (подпись уточнится сама, ответ не ждёт CLI).
* `state`: `idle | working | waiting`; `waiting` — есть blocked-карточка или вопрос к этому профилю.
* `current`: `{task_id, title, status, started_at, request_id}` \| `null` — что делает сейчас: активная карточка профиля (`ready|running|review`) либо, если активной нет, blocked-карточка, на которой он стоит (тогда `status:"blocked"`, `state:"waiting"`). `request_id` — id **запроса** (корня дерева), к которому относится карточка (`null`, если по `task_links` корень найти не удалось): по нему интерфейс ведёт на страницу запроса, а не карточки (W23-ST).
* `last`: `{task_id, title, status, finished_at, summary}` \| `null` — последняя завершённая работа.
* `counts`: `{done, blocked, active}` по карточкам комнаты.

`Roadmap` = `{room, updated_at, current: StagePhase|null, phases: StagePhase[], decisions: Decision[]}`.
`phases` — этапы **этой комнаты** в её порядке (`Room.phases`; миграция чтением, если поля в записи нет), с `note` (русский текст), `updated_at` (int|null) и `source_request`. У комнаты без этапов — `[]`, `current: null`.
`Decision` здесь тот же, что в `RequestDetail`, плюс `owner` (кто решает) и `updated_at`.

`BacklogItem`:

| Поле | Тип |
|---|---|
| `id` | string, `"<root_id>:ns_1"` — уникален в комнате |
| `kind` | `next_step\|future_question\|open_question\|decision` |
| `text` | string |
| `owner` | string \| null (профиль или `mikhail`) |
| `due` | `YYYY-MM-DD` \| null |
| `status` | `open\|asked\|done\|dismissed` |
| `source_request` | string (`t_…`) |
| `source_title` | string |
| `asked_request` | string \| null — запрос, созданный кнопкой «Задать» |
| `created_at`, `updated_at` | int |

`ReportIndexItem` = `{key, title, subtitle, kind: "spec"|"html", path, request_id, blocks: int, created_at, updated_at, exists: bool}`.
`key` — слаг в URL (`unit-economics`).

`JournalEvent` = `{id, at, kind, room, request_id: string|null, task_id: string|null, actor, text, detail: object}`.
`kind`: `request_created|request_status|request_done|step_added|step_done|question_open|question_answered|decision_proposed|decision_accepted|report_added|artifact_added|member_added|member_removed|room_created|room_archived|ingest|error`.

`SearchHit` = `{kind: "request"|"journal"|"decision"|"backlog"|"report", id, room, title, snippet, score: number, at: int|null, route: string}`.
`route` — готовый hash-маршрут для перехода (`#/room/cafe/request/t_a1b2c3d4`).

`Profile` = `{profile, title, description, tags: string[], kind: "director"|"worker"|"critic"|"utility", available: bool, name, job, label, label_source}`.
`name`/`job`/`label` — подпись профиля (WAVE13 §11), разобрано из `hermes profile describe` (кэш) или из `description`; `label_source` тот же набор, что у `TeamMember` (`member` у профиля не бывает).

## 4. Вывод статуса запроса

Статус не хранится, а вычисляется из дерева карточек и состояния критика. Проверки идут сверху вниз, первое совпадение выигрывает (порядок — как в `server/engine_read._status`):

| # | Статус | Условие |
|---|---|---|
| 1 | `reviewing` | карточка `role=critic` в статусе `ready\|running\|review` |
| 2 | `synthesizing` | карточка `role=revision` в статусе `ready\|running\|review`, **или** `critic.state="revised"` и карточки доработки ещё нет |
| 3 | `done` | `root.status == "done"` (активных карточек критика/доработки нет — иначе сработали п.1–2) |
| 4 | `needs_you` | (корень не завершён) есть дочерняя карточка `blocked` с `needs_human=true` (`block_kind` содержит `human` **или** все её родители закрыты (или родителей нет) — так движок блокирует «нужен человек»), **или** есть `open_questions[]` с `for="mikhail"` и `answered=false` |
| 5 | `failed` | `root.last_failure_error != null` или `root.consecutive_failures >= max_retries` (по умолчанию 2) при `root.status != done`; **или** все листья в `failed/crashed` и нет активных карточек |
| 6 | `reviewing` | `root.status == "review"` (ответ ждёт проверки) |
| 7 | `synthesizing` | `root.status == "running"` при всех детях `done\|archived` (директор сводит ответ сам) |
| 8 | `working` | любой ребёнок в `ready\|running\|review`, либо `root.status == "running"` с незавершёнными детьми |
| 9 | `queued` | иначе (`triage/todo/ready` без детей и без активной работы) |

**Решение «`done` + `needs_you`».** Статус отвечает на вопрос «на каком этапе запрос», а `needs_you` (флаг в `RequestSummary` и `RequestDetail`, §3.3) — «ждёт ли он тебя». Они независимы:

* `status` **не перебивается** вопросом: завершённый запрос (`root.status == "done"`) с открытым вопросом Михаилу остаётся `status:"done"`, у него `needs_you:true`. То же для `reviewing` / `synthesizing` / `working`: вопрос или blocked-карточка статус не меняют, флаг поднимается.
* `needs_you` = `status == "needs_you"` **или** неотвеченный вопрос `for="mikhail"` в последней версии ответа **или** blocked-карточка с `needs_human=true`. Вопрос, адресованный профилю (`for="team-director"`), флаг не поднимает.
* Статус `needs_you` бывает только у **незавершённого** корня (п.4 стоит ниже `done`). После `answer` (+ `unblock` блокирующих детей) флаг и статус пересчитываются; ответ на вопрос завершённого запроса снимает флаг, статус остаётся `done`.
* `counters.needs_you` в `RoomSummary` (§3.2) считает запросы со статусом `needs_you` **и** `done` с `needs_you:true`; такой запрос входит и в `done_today`. `reviewing|synthesizing|working` с флагом идут в `counters.working`, а не в `needs_you`.
* UI показывает значок «ждёт тебя» по **флагу** (`needs_you === true`), а не по `status === "needs_you"`; фильтр ленты «ждут ответа» — по тому же флагу.

`failed` стоит ниже `needs_you`: заблокированная карточка не сломалась, а ждёт ответа. Падение карточки критика (`critic.state="failed"`) статус запроса **не меняет**: ответ корня уже готов (`done`), состояние видно только в `critic` (код предупреждения `critic_failed` зарезервирован; текущий бэкенд его не выдаёт — как и `critic_rounds_exhausted`).
`reviewing`/`synthesizing` по критику/доработке стоят **выше** `done`: корень уже завершён, но идёт пост-синтез проверка или доработка, поэтому ответ ещё «не финальный». `finished_at` в это время `null`.
`status_reason` заполняется для `needs_you` («ждёт ответа по Q2»), `failed` («воркер упал 2 раза»), `reviewing` («Критик проверяет ответ») и `synthesizing` («Идёт доработка ответа по замечаниям критика»); иначе `null`. Точные русские формулировки — на усмотрение бэкенда, UI показывает их как есть.

## 5. Legacy-нормализация (`metadata` старого формата)

Старые карточки (реальный `t_605613e0`) кладут в metadata строки и словарь этапов. Нормализация выполняется при чтении; исходные значения не перезаписываются.

**next_steps.** `["строка", …]` → `[{id: "ns_1", text: "строка", owner: null, due: null, state: "open"}]`.
Строки нумеруются по порядку; `owner`/`due` не выдумываются. Объекты с `text` принимаются как есть, отсутствующие `id/state` дополняются. В `warnings[]` добавляется `legacy_metadata`.

**open_questions.** Строки → `{id: "q_1", text, for: "mikhail", answered: false, answer: null, answered_at: null}`.
Если объектов нет, но в тексте ответа есть «От Михаила сейчас: …», вопросы НЕ парсятся из прозы — только metadata.

### 5.1 `stage`: словарь «область → статус» и коллизия ключей

**stage.** Словарь «область → статус» превращается в этапы ровно по его ключам (W17-R §1, B-8: канонического списка этапов больше нет):

1. `id` этапа — стабильный слаг из ключа (`legal → legal`, `«Согласование» → «согласование»`), `title` — сам ключ как написан. Порядок — порядок ключей в JSON. Алиасов и подмены ключей нет: `ops` остаётся отдельным этапом, а не «переименовывается» в `production`.
   Какие из этих этапов видит пользователь, решает `Room.phases` (связка по `id`, затем по названию).
2. Статус по тексту значения, проверки по порядку (первое совпадение):
   * `blocked` — «ждёт решения», «ждёт действия», «blocked», «заблокирован»;
   * `next` — «не начат», «не начато», «не запущен», «not started»;
   * `active` — «в работе», «идёт», «начат», «in progress», «active»;
   * `done` — «готов», «завершен», «закрыт», «утвержд», «done»;
   * иначе `next` + warning `legacy_metadata`.
   Порядок именно такой: «в работе, но ждёт решения» — это `blocked`.
3. Фазы, которых нет в словаре, получают `next` и `note: ""`; `phases[].note` = исходная строка.
4. `current` — по правилу §3.4.

Пример реального `t_605613e0` (сокращённо):

```json
{"next_steps": ["Ш1 Михаил (1–2 мин, 0 ₽): снять блокер канала TG/WA…"],
 "stage": {"idea": "готов: VISION/брифы утверждены…", "legal": "в работе → ждёт действия Михаила…",
           "brand": "в работе → ждёт решения…", "packaging": "в работе: исследование готово…",
           "production": "не начат: кухня есть…", "sales": "не начат: 0 продаж…"},
 "open_questions": ["Q1 Канал для D19: V1 / V2 / V3…"]}
```

→ `next_steps=[{id:"ns_1", text:"Ш1 Михаил …", owner:null, due:null, state:"open"}]`,
`stage.phases = [done, blocked, blocked, active, next, next]`, `stage.current = "legal"`,
`open_questions=[{id:"q_1", text:"Q1 Канал…", for:"mikhail", answered:false}]`, `synthesis.legacy = true`.

**synthesis.** Текст берётся из последнего завершённого run'а корня (`summary`), иначе из run'а ребёнка-синтезатора
(заголовок содержит «свест», «сводн» или «synth»). `tasks.result` пустой — не используется.
Если текста нет, но корень `done` → `state: "none"`, `warnings += no_synthesis`.

**v2-поля у legacy-запросов.** Старые карточки не знают версий и критика, поэтому сервер дополняет ответ при чтении:
`synthesis.version = 1`, `synthesis.history = [{version:1, kind:"initial", task_id:<источник>, author, at, verdict:null, note:""}]`;
`critic = {enabled:false, round:0, max_rounds:<из комнаты>, state:"off", verdict:null, issues:[], task_id:null}` — «критик приложением не вёлся» (в ленте тот же запрос отдаёт `CriticRef {enabled:false, state:"off", verdict:null, round:0}`).
`tree[].outside_room = false` (состав комнаты на момент старого запроса неизвестен). Сами фикстуры/карточки не перезаписываются.

## 6. Эндпоинты

Соглашение: `{id}` — id комнаты, `{rid}` — id корневой карточки, `{item_id}` — id элемента бэклога (URL-encoded, содержит `:`).
Тело ответа — объект, описанный в разделе; полные примеры — в `fixtures/`.

### 6.1 Служебные

#### `GET /api/health`
Живость приложения, движка, диспетчера, watcher'а. Параметров нет.
`200` → `Health` (§3.1). Ошибок нет (деградация выражается `ok=false` и `warnings[]`).
```json
{"ok": true, "app": {"name": "team-room", "version": "1.0.0", "port": 8777, "uptime_s": 900},
 "engine": {"ok": true, "schema_ok": true, "board": "team"},
 "dispatcher": {"ok": true, "state": "running", "pid": 9608}, "warnings": []}
```
→ `fixtures/health.json`

#### `GET /api/config`
Настройки для экрана «Настройки»: пути, лимиты, белые корни, флаги. Параметров нет.
`200` → `{app: {name, version, port, started_at, sandbox, tz}, paths: {data, vault, project_dir, reports}, allowed_roots: string[], limits: {request_text_max, note_text_max, body_bytes_max, attach_max, attach_bytes_max, page_size_max, journal_page_max}, features: {reports: bool, ics: bool, export_html: bool, adopt: bool, stt: bool}}`.
`500 engine_error` — не читается конфиг. → `fixtures/config.json`

#### `GET /api/profiles`
Ростер профилей Hermes для «добавить участника» и «переназначить». Параметров нет.
`200` → `{items: Profile[], count: int, dispatcher: {ok, warning}}`.
Каждый профиль читается из `%LOCALAPPDATA%\hermes\profiles\<name>`; отсутствующий на диске → `available:false`, но остаётся в списке.
Подпись (`name`, `job`, `label` — WAVE13 §11) берётся из `hermes profile describe` через кэш `server/profile_info.py`: ответ CLI не ждёт ни один запрос, пока его нет — разбор `description` из `profile.yaml`.
`502 engine_error` — каталог профилей недоступен. → `fixtures/profiles.json`

#### `POST /api/dispatcher/kick`
Один проход диспетчера (`hermes kanban dispatch`) — кнопка «Проверить доску сейчас»: запустить
готовые задачи, не дожидаясь минутного таймера (W13-F).

Тело: `{}` — проход; `{max: 1..5}` — предел карточек за проход; `{confirm: true}` — разрешить
чужие (вне `room:*`) карточки; `{preview: true}` — **только состояние доски**, ничего не
запускается и CLI не вызывается (работает и в песочнице; нужно, чтобы решить, осмысленна ли кнопка).

`200` → `{ok: true, spawned: int, reclaimed: int, promoted: int, planned: PlannedCard[], max: int,
report: KickReport, dispatcher: {ok, state, warning}, at: int}`.
Для `preview` — `{ok: true, preview: true, report: KickReport, at: int}`.

```json
{"ok": true, "spawned": 1, "reclaimed": 0, "promoted": 1, "max": 3,
 "planned": [{"id": "t_e9c6c2b2", "assignee": "lawyer", "tenant": "room:fn", "foreign": false}],
 "report": {"ok": true, "kind": "started", "preview": false, "spawned": 1, "reclaimed": 0,
            "promoted": 1, "max": 3,
            "started": [{"id": "t_e9c6c2b2", "assignee": "lawyer", "title": "Юр-проверка ИП и ОКВЭД"}],
            "counts": {"ready": 0, "stuck": 0, "running": 3, "review": 0, "input": 1, "blocked": 0,
                       "deps": 2, "triage": 0, "scheduled": 0, "queued": 0, "done": 12, "total": 18},
            "cards": [{"id": "t_776c0be5", "title": "Пробный прогон 30 моти", "assignee": "team-ops",
                       "status": "blocked", "reason_code": "input", "reason": "ждёт вашего ответа",
                       "tenant": "room:cafe", "since": 1793195160}],
            "useful": false, "error": null, "at": 1793912795}}
```

`KickReport` (W13-F, `server/kick_report.py`) — итог простыми словами:
* `kind`: `started` | `reclaimed` | `promoted` | `nothing` | `preview` — что сделал проход;
* `started[]` — кого запустили (`id`, `assignee`, `title`);
* `counts` — карточки комнат (`tenant LIKE 'room:%'`, без архива) по причинам:
  `ready` (готова к запуску), `stuck` (running без heartbeat > 15 мин), `running`, `review`,
  `input` (blocked, `block_kind` содержит `input`/`human` — ждёт ответа человека), `blocked`
  (ждёт решения), `deps` (todo — ждёт других задач), `triage`, `scheduled`, `queued`, `done`, `total`;
* `cards[]` — до 60 карточек (`id`, `title`, `assignee`, `status`, `reason_code`, `reason`, `tenant`,
  `since`), сначала те, что проход может сдвинуть;
* `useful` — есть ли смысл нажимать кнопку (`ready + stuck > 0`);
* `error` — почему доска не прочиталась (тогда `ok: false`, остальное пустое).

Ошибка итога проход не ломает: `report` может быть `{}`.
`429 rate_limited` — проход уже был меньше 30 с назад (`detail.retry_after`, `detail.cooldown_s`).
`409 conflict` — в списке есть чужие карточки без `confirm` (`detail.reason="foreign_cards"`) или
проход в песочнице (`detail.reason="sandbox_no_dispatch"`).
`502 engine_error` — CLI вернул ошибку (`detail.stderr`).

#### `GET /api/dispatcher/preview`
Состояние доски **только чтением** (W17-B, вместо `POST /api/dispatcher/kick {preview:true}`): тот же
`KickReport`, что у `preview`-прохода, но `GET` ничего не пишет и не считает лимит частоты — его можно
звать на каждой загрузке экрана, и он честно работает в песочнице.

`200` → `{ok: true, preview: true, report: KickReport, at: int}`. Тело `report` — как у `kick` (§6.1).
Ошибка чтения доски не роняет ответ: `report: {}`, `ok: true` (кнопку просто не показываем).
`POST /api/dispatcher/kick {preview:true}` оставлен для старых клиентов и отвечает так же.

### 6.2 Комнаты

#### `GET /api/rooms`
Список комнат с живыми счётчиками. Query: `include_archived` (`1|0`, по умолчанию `0`).
`200` → `{rooms: RoomSummary[], engine: {ok, board, warning}, updated_at: int}`.
`502 engine_error` — БД недоступна (комнаты читаются из `data/rooms.json`, счётчики — из канбана; при сбое счётчики нулевые и `engine.ok=false`, ответ всё равно `200`).
```json
{"rooms": [{"id": "cafe", "title": "Лето (demo)", "counters": {"working": 2, "needs_you": 1, "done_today": 2},
            "phase": {"id": "legal", "title": "Легализация", "status": "active"}}], "updated_at": 1793171890}
```
→ `fixtures/rooms.json`

#### `GET /api/rooms/summary`
**Один запрос на всё боковое меню** (W17-B): счётчики и последние запросы ВСЕХ комнат. Раньше меню
каждые ~9 с отправляло 11 запросов по всем комнатам, включая неактивные.

Query: `requests` (0..10, по умолчанию `3`) — сколько последних запросов на комнату; `include_archived`
(`1|0`, по умолчанию `0`); `room` (необязательно, id комнаты — тогда в ответе только она).

`200` →
```json
{"ok": true, "at": 1793912795, "count": 2,
 "engine": {"ok": true, "board": "team", "warning": null},
 "rooms": [{"id": "fn", "title": "Гранит", "icon": "folder", "archived": false,
            "orchestrator": "team-director", "critic": "team-critic", "members_count": 4,
            "mode_default": "auto", "critic_pass": true, "max_rounds": 2,
            "critic_mode": "rounds", "critic_round_cap": 12,
            "launch_mode": "plan_first", "max_parallel_streams": 2, "project_mode": false,
            "counters": {"working": 2, "needs_you": 1, "done_today": 3},
            "phase": {"id": "legal", "title": "Легализация", "status": "active"},
            "requests_count": 7, "last_at": 1793912000,
            "last": [{"id": "t_1b0aa253", "room": "fn", "title": "Так, начнём работу…",
                      "title_short": "Начнём работу в этой комнате", "status": "done",
                      "needs_you": true, "created_at": 1793906100, "finished_at": 1793911000,
                      "preview": "Так, начнём работу…"}]}]}
```

* Поля комнаты — те же, что в `RoomSummary` (§3.2), плюс `requests_count` и `last_at` (последняя запись реестра).
* `last[]` — до `requests` последних запросов комнаты **из реестра**, без полной сборки детали:
  `id`, `room`, `title`, `title_short`, `status` (из доски, одним запросом на доску), `needs_you` из кэша
  наблюдателя, `created_at`, `finished_at`, `preview`. Запросов с неизвестным статусом (карточки нет на доске)
  в списке нет.
* Ответ кэшируется на 2–3 с: меню можно опрашивать часто и всем экранам сразу.
* `200` даже при недоступном канбане: счётчики из кэша наблюдателя, `last[].status = null`, `engine.ok=false`.

#### `POST /api/rooms`
Создать комнату (и доску, если её нет). Тело:

| Поле | Тип | Обяз. | По умолчанию |
|---|---|---|---|
| `id` | string | нет | слаг из `title` |
| `title` | string | да | — |
| `icon` | string | нет | `folder` |
| `board` | string | нет | `team` |
| `orchestrator` | string | да | — |
| `critic` | string \| null | нет | `null` |
| `members` | `{profile, role?, note?, pinned?, label?}[]` | нет | `[]` |
| `context_files` | string[] | нет | `[]` |
| `project_dir` | string | нет | `""` |
| `mode_default` | `auto` | нет | `auto` (только) |
| `critic_pass` | bool | нет | `false` |
| `max_rounds` | int | нет | `1` (допустимо 1..2) |
| `launch_mode` | `plan_first` \| `auto` | нет | `auto` — **новая** комната запускает части сразу, без подтверждения (решение Михаила, W17-R §3). Записи без поля читаются как `plan_first`: настройки существующих комнат не меняются |
| `max_parallel_streams` | int 1..4 | нет | `2` |
| `project_mode` | bool | нет | `true` (новая комната сразу в проектном режиме) |
| `phases` | `{id?, title}[]` | нет | `[]` — этапов нет, шаблонов по умолчанию не бывает; `id` выводится из заголовка, лимиты 24 этапа / 80 знаков |
| `agent_notes` | `{profile: {text?, updated_at?, entries?[]}}` | нет | `{}` — особенности участникам этой комнаты (§6.2.2), записи или старый текст |

`201` → `RoomDetail` (§3.2), заголовок `Location: /api/rooms/{id}`.
`400 bad_request` — нет `title`/`orchestrator`, плохой `id`, неизвестный профиль (`detail.available`), `mode_default="swarm"` («не поддерживается»), `max_rounds` вне 1..2, негодный `project_dir` (§6.2.1), `launch_mode` не из `plan_first|auto` («Режим запуска: «сначала план» или «сразу без подтверждения»»), `max_parallel_streams` не целое 1..4 («Одновременно частей работы: от 1 до 4»), `project_mode` не bool («ожидается true или false»), `phases` не список/этап без заголовка/повтор `id`/больше 24 этапов/заголовок длиннее 80 знаков.
Старые записи `data/rooms.json` без этих полей читаются как `plan_first` / `2` / `false`; файл при чтении не переписывается (обратная совместимость).
`409 conflict` — комната с таким `id` уже есть (`detail.room`). `502 engine_error` — не удалось создать доску.

#### 6.2.1 Папка проекта комнаты (`project_dir`)
Необязательное поле: абсолютный путь Windows, куда команда складывает ВСЕ итоговые файлы по комнате (отчёты, таблицы, документы). `""` — папка не задана (прежнее поведение — Vault/`Материалы`).

* При сохранении комнаты (`POST`/`PATCH`) папка создаётся (`mkdir -p`) — это единственная новая запись на диске, только под `project_dir`.
* Внутри: `<project_dir>/<YYYY-MM-DD> <название запроса>/` на каждый запрос и `<project_dir>/README.md` с автоматическим «Содержанием» между маркерами `<!-- teamroom:begin index -->…<!-- teamroom:end -->`; ручной текст вне маркеров сохраняется.
* Запрещены: корень диска, `C:\Windows`, `C:\Program Files`, `C:\Program Files (x86)`, `C:\ProgramData`, весь `%LOCALAPPDATA%\hermes`, папка данных приложения и каталоги программы (`server/`, `web/`), UNC `\\server\share`, длинные `\\?\`-пути, `..`, зарезервированные имена (`con`, `prn`, `aux`, `nul`, `com1..9`, `lpt1..9`) и символы `< > : " | ? *`.
* Внутри Vault разрешено, но с предупреждением (`warnings[]`): для результатов лучше отдельная папка.
* `400 bad_request` — путь не подходит; сообщение по-русски, например «Папка внутри служебных каталогов Hermes — выберите другую».

#### 6.2.2 Особые указания участникам комнаты (`agent_notes`)
Добавка к роли участника, действующая ТОЛЬКО в этой комнате (правила, навыки, на что смотреть особенно). Файлы профиля (`SOUL.md`, `config.yaml`, skills) не меняются никогда — текст живёт в реестре комнат.

Поле комнаты: `agent_notes: {"<profile>": {"text": string ≤ 8 000, "updated_at": int}}`; нет поля — указаний нет. Отдаётся в `GET /api/rooms/{id}` и в `GET /api/rooms?include_archived=1`. Профиль обязан быть в составе комнаты (`orchestrator`, `critic` или `members`).

##### `PUT /api/rooms/{id}/agents/{profile}/notes`
Тело: `{text: string}`. `text` ≤ 8 000 знаков; пустая строка — очистка указаний участника.

`200` →
```json
{"ok": true, "profile": "team-lawyer", "text": "Всегда проверяй по закону X", "cleared": false,
 "agent_notes": {"team-lawyer": {"text": "Всегда проверяй по закону X", "updated_at": 1793100000}},
 "warnings": []}
```
`400 bad_request` — профиль не в составе («Указания можно задать только участнику комнаты: «coder» в составе нет»), текст длиннее 8 000 («Указания: 8001 знаков, а можно не больше 8000»), текст не строка, NUL. `404 not_found` — нет комнаты. Похожее на секрет (`sk-…`, `password=`, `token:`, `bearer …`) **не блокирует** запись, но приходит в `warnings[]` в замаскированном виде.

Также принимается:
* `POST /api/rooms` — `agent_notes` вместе с составом (профили сверяются с новым составом);
* `PATCH /api/rooms/{id}` — те же правила, профили сверяются с текущим составом.

##### Записи (WAVE14 §13): структура, журнал, откат

С версии 1.0.2 указания у участника — **список записей**, а не один текст:

```
agent_notes: {
  "<profile>": {
    text: string,                       // склейка записей (совместимость)
    updated_at: int,
    entries: [{id, text, source, origin, created_at, used_count, pinned}],
    review: {count_at_review: int, reviewed_at: int, due: bool}
  }
}
```

* `source` — `user` (Михаил), `leader` (руководитель комнаты), `auto` (переоценка/система);
* `origin` — источник записи: задача, комментарий Михаила, ручная правка;
* `used_count` — сколько раз запись пригодилась (комментарий в карточке, явная отметка);
* `pinned` — ручная запись Михаила; руководитель её **не удаляет** (только он сам).

Старый формат `{профиль: {text}}` читается и принимается по-прежнему: он превращается
в одну закреплённую запись. `PUT …/notes` (полный текст) теперь заменяет список одной
закреплённой записью; в существующей комнате интерфейс правит записи по одной.

Ограничения роста: `limits = {max_entries: 24, max_text: 8000, review_every: 5,
stale_days: 30, entry_hint: 1000}` (отдаются в ответах `GET …/notes`). Каждое
`review_every` записей и при превышении лимита запускается переоценка: дубли сливаются,
устаревшее и ни разу не использованное (`used_count = 0`, старше `stale_days`) убирается,
закреплённое не трогается.

Журнал комнаты: `agent_notes_log` (поле комнаты, отдаётся в `GET /api/rooms/{id}`),
новые события сверху, ≤ 200. Событие: `{id, at, action, profile, entry_id, text, why,
origin, actor, source, note, rolled_back, before[], after[]}`, `action` —
`add | update | remove | merge | review | replace | pinned | unpin | rollback` +
`artifact_outside` (B-27: файл вне папки проекта), `leader_review` и `leader_reminder`
(B-35, ниже).
Каждое событие можно откатить: возвращаются записи из `before`, закреплённые записи,
добавленные позже, сохраняются.

##### Отметка «оценил работу» и одно напоминание (WAVE19 B-35)

Обязанность руководителя после каждого завершённого запроса: оценить работу участников,
решить, нужна ли запись в «Особенности», и оставить отметку в `metadata` своей карточки
(`leader_review`). Текст обязанности приложение пишет само — блок «Правила комнаты»
в контракте карточки (вместе с правилом мест сохранения файлов, B-27/B-34).

Приложение (watcher, раз в тик) делает две вещи:

1. **Отметка → журнал.** Нашёл `metadata.leader_review` в дереве запроса (корень или
   карточка доработки; блок читается и из `metadata.extra`, куда попадают незнакомые ключи
   после нормализации) — пишет событие журнала комнаты:

```
{action: "leader_review", source: "leader", profile: "<руководитель>",
 at, text: "оценил работу участников; добавил 2 записи (team-lawyer, default)",
 why: "добавил 2 записи", note: "<summary>",
 key: "leader_review:<hash12>", root_id, outcome: "no_change"|"note_added",
 outcome_ru, profiles: [профиль], added: [{profile, text}], count: N}
```

   `action_ru` в `journal()` — «оценка работы руководителем». На запрос — одна отметка:
   повторный проход возвращает то же событие (`key` = комната + корень).

2. **Отметки нет → ОДНО напоминание.** Не раньше `DUTY_GRACE_S` (180 с) после завершения
   приложение пишет комментарий на корневую карточку от профиля руководителя:
   `[Комната команды · проверка работы] …` (что сделать и как оформить `leader_review`).
   Это обычный комментарий — карточек «Уточнение» не создаётся, модели не запускаются,
   вопросов Михаилу не задаётся. Ретро-рассылки нет: запросы, завершённые до запуска приложения
   (и раньше чем за `DUTY_START_LOOKBACK_S` = 900 с до него), не трогаются — обязанность касается
   новых завершений; если время завершения неизвестно, напоминания тоже нет. Повторно
   напоминание не ставится: событие
   `leader_reminder` (`key`, `root_id`, `version`, `source: "auto"`) и метка в тексте
   комментария проверяются до записи. Появившаяся позже отметка снимает обязанность и
   второго напоминания не будет.

Служебные события (`leader_review`, `leader_reminder`, `artifact_outside`) записи участника не
меняли, поэтому `POST …/notes/journal/{e}/rollback` для них отвечает `409 conflict`
(`detail.action`) — иначе «откат» вернул бы пустой список и снёс записи. В интерфейсе кнопка
«Откатить» у таких событий не показывается.

`GET /api/rooms/{id}/notes` → `{ok, room, count, limits, participants: [{profile, role,
entries, text, updated_at, review}]}`.

`GET /api/rooms/{id}/agents/{profile}/notes` → записи участника + `limits` + `journal`
(последние 20 событий). `404` — участника нет в комнате.

`POST /api/rooms/{id}/agents/{profile}/notes/entries` — тело `{text, source?, origin?, why?,
actor?, pinned?}`, `201` → `{ok, profile, entries, text, entry, duplicate, review}`.
Точный дубль не создаётся: `duplicate: true`, возвращается существующая запись. `400` —
пустой текст / не участник / превышен общий лимит знаков; `409` — комната в архиве.

`PATCH /api/rooms/{id}/agents/{profile}/notes/entries/{entry}` — `{text, why?}`, `200`.
`404` — записи нет.

`DELETE /api/rooms/{id}/agents/{profile}/notes/entries/{entry}` — `{why?, force?}` или
`?force=1` в строке запроса (некоторые клиенты не передают тело у DELETE).
Закреплённую запись без `force: true` удалить нельзя: `409 conflict`
(`detail.reason = "pinned"`). Интерфейс Михаила передаёт `force: true`.

`POST /api/rooms/{id}/agents/{profile}/notes/review` — `{apply?: bool, why?, origin?}`.
`apply: false` — предпросмотр (`merges[]`, `drops[]`, `before/after`, `chars_before/after`,
`applied: false`), ничего не меняется. `apply: true` (по умолчанию) — применить; ответ
дополнительно содержит `applied`, `entries`, `journal`.

`POST /api/rooms/{id}/agents/{profile}/notes/used` — `{entry_ids?: [id]}`; без списка
считаются все записи участника. `200` → `{ok, profile, touched}`.

`POST /api/rooms/{id}/agents/{profile}/notes/entries/{entry}/pin` — `{pinned: bool}`
(WAVE17 P6, окно-«блокнот»): закрепить или снять закрепление записи. `200` →
`{ok, profile, entries, text, entry, limits}`; в журнал комнаты пишется событие
`pinned` / `unpin` (его можно откатить). `400` — нет поля `pinned`; `404` — записи нет
или участник не в комнате; `409` — комната в архиве.

`GET /api/rooms/{id}/notes/journal?limit=50` → `{ok, room, count, events}` — новые сверху,
текст замаскирован (`[скрыто]`), добавлены `action_ru` и `source_ru`.

`POST /api/rooms/{id}/notes/journal/{event}/rollback` — `{why?, actor?}`, `200` →
`{ok, profile, entries, text, rolled_back, restored, journal}`. `404` — события нет;
`409` — уже откачено / участника больше нет в комнате.

Как указания доходят до агента (два канала, оба обязательны):
1. **Тело карточки** — раздел «Особые указания участникам этой комнаты» в блоке «КОНТЕКСТ КОМНАТЫ» (`context_pack.build_context_block`): `- <роль по-русски> (<профиль>): <текст>` + правило «Руководитель при раздаче подзадач обязан переписать указание участника в тело его карточки…». Лимит раздела — 3 000 знаков: длинный текст обрезается «…» с пометкой, полный лежит в Obsidian (`Rooms/<комната>/Указания участникам.md`).
2. **Автокомментарий** — watcher для карточек дерева запроса комнаты, чей исполнитель имеет указания, пишет одну строку `[Указание комнаты · <hash8>] <текст>` (через CLI `comment`, автор — сам исполнитель). Идемпотентно по `hash8`: повторно не пишется; при смене текста — новый комментарий с пометкой `(обновлено)`.

Приоритет в тексте: профиль → указания комнаты → текст запроса; в запросе Михаил может переопределить указание.

Зеркало в Obsidian (`vault_mirror.sync_room`): раздел «Особые указания» в `CONTEXT.md` и файл `Указания участникам.md` (служебный блок между маркерами, ручной текст вне маркеров сохраняется).

#### 6.2.3 Подпись участника в комнате (`label`)
Как участник называется в этой комнате: «Анна — финансы и экономика (CFO)» (WAVE13 §11, пункт 11).
Подпись по умолчанию собирается из описания профиля Hermes (`hermes profile describe`), а поле
`label` в составе комнаты перекрывает её. Хранится только в реестре комнат (`data/rooms.json`),
файлы профиля (`profile.yaml`, `SOUL.md`, `config.yaml`, skills) не меняются никогда.

Состав комнаты (`Room.members[]`): `{profile, role, note, pinned, added_at, label}`; `label: ""` —
подпись из описания профиля.

**Одна операция для всех (WAVE17 B-17).** Руководитель комнаты и критик не лежат в `members`
(они — настройки комнаты), поэтому их подпись хранится в `Room.role_labels: {profile: label}`
(профиль Hermes тоже не меняется). Тот же `PUT` пишет её туда, пустая строка — сбрасывает.
Приоритет источников подписи: `label` участника / `role_labels` комнаты (пользователь) →
`hermes profile describe` → описание из `profile.yaml` → роль в комнате (`label_source=role`).
`GET /api/rooms/{id}` и блок команды (`GET /api/rooms/{id}/team`) отдают её одинаково — полем
`label`, поэтому все экраны (команда, шапка, дерево работ, вопросы, шаги, боковое меню) берут
подпись из одного места.

##### `PUT /api/rooms/{id}/agents/{profile}/label`
Тело: `{label: string}` ≤ 120 знаков (пробелы схлопываются); пустая строка — вернуться к описанию профиля.
Доступно **всем** участникам: исполнителю, руководителю комнаты, критику.

`200` →
```json
{"ok": true, "profile": "default", "label": "Михаил — руководитель комнаты",
 "role_labels": {"default": "Михаил — руководитель комнаты"},
 "members": [{"profile": "team-cfo", "role": "worker", "note": "", "pinned": false,
              "added_at": 1793060000, "label": "Ира — бухгалтер проекта"}]}
```
`role_labels` есть всегда (у исполнителя — без изменений). `400 bad_request` — `label` не строка, NUL в подписи.
`404 not_found` — нет комнаты или профиля ни в составе, ни в ролях комнаты. `502 engine_error` — реестр не записался.

#### `POST /api/rooms/check-dir`
Проверка папки проекта до сохранения (GET-подобная, **ничего не создаёт**: права смотрятся у ближайшего существующего родителя).
Тело: `{path: string}`. `200` →
```json
{"normalized": "C:\\TeamRoom\\Projects\\Лето", "exists": false, "writable": true,
 "will_create": true, "warnings": [], "allowed": true, "reason": null}
```
`allowed:false` — путь не подходит, `reason` — короткое объяснение по-русски (то же, что вернёт `400` при сохранении). `400` — `path` не строка/длиннее 400 знаков/NUL.

#### `GET /api/rooms/{id}`
`200` → `RoomDetail` (+ `ETag`). `404 not_found` — нет комнаты. `502 engine_error` — счётчики не посчитались (вернётся `RoomDetail` с нулями и `dispatcher.warning`).
→ `fixtures/room_cafe.json`

#### `PATCH /api/rooms/{id}`
Частичное обновление. Разрешены поля: `title, icon, orchestrator, critic, context_files, project_dir, mode_default, critic_pass, max_rounds, critic_mode, critic_round_cap, launch_mode, max_parallel_streams, project_mode, phases, agent_notes`.
Настройки проектного режима меняются только здесь — общих режимов «для всех комнат» нет (A1); `id`, `board`, `tenant` неизменяемы.
Пустой `launch_mode` (`""`) = рекомендуемый `plan_first`. PATCH не трогает `project_dir` и состав, если они не переданы.

Окно настроек комнаты отправляет одним PATCH `project_mode`, `launch_mode`, `max_parallel_streams` и
`phases` вместе с остальными полями — поэтому «Сохранить» в разделе «Проект» сохраняет и режим, и
этапы, а карта проекта обновляется тем же ответом (`project_mode` читается обратно из `GET /api/rooms/{id}`).
`200` → `RoomDetail`. `400 bad_request` — неизменяемое поле (`id`, `board`, `tenant`) или неизвестное (`detail.fields`), `mode_default="swarm"`, `max_rounds` вне 1..2, `critic_mode` не из `rounds|until_approved`, `critic_round_cap` вне 2..30. `404 not_found`. `409 conflict` — `orchestrator`/`critic` не из ростера. Отклонённая правка ничего не меняет (проверка до записи).

#### `POST /api/rooms/{id}/archive`
Архивирует комнату (карточки не трогает, комната скрывается из списка).
Тело: `{}`. `200` → `{ok: true, changed: bool, room: RoomSummary}`. Повторный вызов — `changed:false`, не ошибка.
`404 not_found`.

#### `POST /api/rooms/{id}/members`
Добавить участника. Тело: `{profile, role?: "worker"|"critic"|"observer", note?: string, pinned?: bool}`.
`201` → `{member: Member, members: Member[]}`. `400 bad_request` — профиля нет в ростере (`detail.available`). `404 not_found` — нет комнаты. `409 conflict` — уже участник (`detail.member`).

#### `DELETE /api/rooms/{id}/members/{profile}`
`200` → `{ok: true, members: Member[]}`. `404 not_found` — нет комнаты/участника.
`409 conflict` — профиль является `orchestrator` или `critic` комнаты (`detail.reason="room_lead"`): сначала смените директора через `PATCH`.

#### `PATCH /api/rooms/{id}/members/{profile}`
Закрепление участника в составе (волна 19, B-28): `{pinned: bool}`. Флаг `pinned` — «всегда в составе»:
участник остаётся в комнате, даже если задачи давно не приходят. Меняется только реестр комнаты
(`data/rooms.json`); профиль Hermes (`profile.yaml`, `SOUL.md`, `config.yaml`) не трогается.
В интерфейсе это пункт меню «⋯» карточки участника («Закрепить в составе / Открепить») с пояснением;
на лице карточки значка больше нет. Руководитель комнаты и критик закреплены всегда — на карточке
у них одинаковая подпись «обязательная роль», а не отдельный значок.
`200` → `{ok: true, profile, pinned, members: Member[]}`. `400 bad_request` — нет поля `pinned` или оно не bool.
`404 not_found` — нет комнаты/участника. `409 conflict` — `orchestrator` или `critic` комнаты
(`detail.reason="mandatory_role"`).

### 6.3 Запросы

#### `POST /api/rooms/{id}/requests`
Создать запрос: текст Михаила уходит в корневую карточку, директор разбирает его сам.
Тело:

| Поле | Тип | Обяз. | Правило |
|---|---|---|---|
| `text` | string | да | 1..20000 символов, дословно, без правок |
| `title` | string | нет | до 120; иначе первая непустая строка текста, очищенная от markdown, ≤80 символов |
| `mode` | `auto` | нет | единственный режим; `"swarm"` → `400 bad_request` «Режим swarm не поддерживается» (`detail.allowed=["auto"]`) |
| `critic` | bool | нет | по умолчанию `room.critic_pass`; `true` включает пост-синтез проверку критика |
| `critic_mode` | `rounds` \| `until_approved` | нет | режим проверки **этого** запроса (волна 13, §9): по умолчанию `room.critic_mode`. `until_approved` без критика → `400` (`detail.field="critic_mode"`) |
| `max_rounds` | int | нет | 1..5, число раундов для `critic_mode="rounds"`; по умолчанию `room.max_rounds` |
| `attach` | string[] | нет | ≤10 путей из белых корней; копируются в `data/uploads/<slug>/` |
| `client_id` | string | нет | uuid с фронта (1..64 символа: латиница, цифры, `. _ : -`); ключ идемпотентности |
| `brief` | bool | нет | «Это бриф проекта» (§6.10): тело становится лёгким — руководитель не исследует, а предлагает карту направлений. Строка `"true"` → `400` (`detail.field="brief"`) |

`201` → `{request: RequestSummary, root_id: string, dispatcher: {ok, warning}, warning?: {code, message, detail}, duplicate: false}`, `Location: /api/requests/{rid}`.
`400 bad_request` — пустой/длинный текст, `mode` не `auto` (`detail.field="mode"`, `detail.allowed=["auto"]`), `critic` не bool (`detail.field="critic"`; строка `"false"` — тоже ошибка, не `false`), больше 10 вложений, `client_id` не по формату. `403 forbidden` — вложение вне белых корней или из чёрного списка (`detail.path`).
`404 not_found` — нет комнаты. `409 conflict` — комната в архиве. `503 engine_busy` — БД канбана заблокирована (`detail`), карточка не создана; `502 engine_error` — CLI не создал карточку (`detail.stderr`); карточка-сирота не остаётся.
`warning` заполняется, когда карточка создана, но декомпозиция не запустилась (диспетчер выключен): `{code:"dispatcher_down", message:"Запрос создан, но диспетчер не запущен — работа не начнётся сама."}`.

**Защита от двойной отправки.** Повторный `POST` в течение **60 секунд** с тем же `client_id` (если он передан) либо с тем же хэшем `(комната, text, attach)` **не создаёт вторую карточку**: сервер возвращает `200` (не `201`) с тем же телом, но `duplicate: true` и тем же `root_id`. Так двойной клик/повтор после таймаута не запускает вторую декомпозицию LLM. Первый ответ всегда `duplicate: false`.
Если карточка создана, а сводку прочитать не удалось (БД занята), ответ всё равно `201`: `request.degraded=true`, `request.warning={code:"summary_unavailable", …}`, `warning` в корне ответа с тем же кодом — карточка уже есть, повторять запрос не нужно.
Создание запроса **никогда** не запускает `decompose`/`swarm`/`specify` само: только `triage` + `tenant` + `idempotency-key`, дальше работает `auto_decompose` движка (ARCH §12.2).

#### `GET /api/rooms/{id}/candidates`
«Кандидаты на привязку»: карточки доски с событием `decomposed`, которых нет в реестре запросов комнаты (ARCH §12.1). Параметров нет.
`200` → `{room: string, generated_at: int, items: Candidate[], count: int}` — `count` всегда равен `len(items)`, порядок — новые сверху (`created_at`). `generated_at` — время сборки списка (диагностика; клиент может игнорировать). Бэкенд и мок отдают одинаковую форму.

`Candidate` = `{task_id, id, title, status, assignee: string|null, created_at, tenant: string|null, children: int, decomposed_at: int|null, preview: string}`:
* `task_id` и `id` — одно и то же (реальный `engine_read.list_candidates` отдаёт оба ключа); `preview` (первые ~200 символов текста, без markdown) и `decomposed_at` (время события `decomposed`) отдаёт и бэкенд, и мок. Старый клиент мог считать их необязательными — для совместимости читайте с запасным значением (`""` / `null`).
* `children` — сколько детей у карточки; `tenant` — текущая метка (`null`, если карточка без комнаты; чужая метка → `adopt` вернёт `409`).
`404 not_found` — нет комнаты. `502 engine_error` — БД недоступна.
→ `fixtures/candidates_cafe.json` (2 карточки; у второй `tenant="room:fn"` — пример конфликта при привязке в `cafe`).

#### `GET /api/rooms/{id}/requests`
Лента запросов комнаты. Query: `limit` (1..100, 20), `cursor`, `status` (через запятую), `q` (подстрока в заголовке/тексте).
`200` → `{items: RequestSummary[], next_cursor: string|null, has_more: bool}`.
`400 bad_request` — неизвестный `status` (`detail.allowed`), `limit` вне диапазона. `404 not_found` — нет комнаты.
→ `fixtures/requests_cafe.json`

#### `GET /api/requests/{rid}`
Полная карточка запроса. Заголовки: `ETag`; при `If-None-Match` → `304`.
`200` → `RequestDetail` (§3.4). `404 not_found` — карточки нет или она не корень комнаты (`detail.reason="not_root"`).
`502 engine_error` — БД/CLI не прочитались. Битые metadata не роняют ответ: поля нормализуются, в `warnings[]` — `legacy_metadata`.
→ `fixtures/request_done.json`, `request_critic_approved.json`, `request_done_legacy.json`, `request_working.json`, `request_reviewing.json`, `request_needs_you.json`, `request_outside_room.json`, `request_failed.json`, `request_queued.json`

#### `PATCH /api/requests/{rid}`
Переименование названия запроса (W17-B §4, B9 усилено). Тело: `{title}` — строка до 120 знаков; `""` снимает блокировку и возвращает автоматическое название.
`200` → `{ok: true, action: "rename", request: RequestSummary, title_short, title_source, title_locked}`.
После непустого `title` в реестре `title_source="user"`, `title_locked=true`; это название не перезаписывается ни моделью, ни руководителем, ни пересчётом по ходу беседы. Полный текст запроса (`title`, `text`) не меняется.
`400 bad_request` — нет поля `title` / не строка / длиннее 120 / NUL-байт. `404 not_found` — запрос не корень комнаты.

#### `POST /api/requests/{rid}/actions`
Одно действие над запросом. Тело: `{action, ...поля}`.

| `action` | Поля | Что делает | Ответ (кроме `ok:true, action, request`) | Ошибки |
|---|---|---|---|---|
| `dispatch` | `task_id?` | проход диспетчера, чтобы карточку взяли (кнопка «Проверить доску сейчас») | `{spawned, promoted, reclaimed, planned, max, report: KickReport, dispatcher}` — формат `report` см. §6.1 | `502` |
| `comment` | `text` (1..8000), `author?`=`mikhail` | комментарий к корню | `{comment: {id: int, author, text, at}}` (`id` — номер строки `task_comments`; растёт; мок отдаёт тот же формат) | `400`, `404` |
| `answer` | `question_id`, `text` (1..8000), `need_explanation?` (bool) | **Ответ на конкретный вопрос** (W14-D §1–§4): комментарий с меткой `[Михаил · ответ на вопрос q_x «текст вопроса»]` + `unblock` блокирующих детей; в ветке появляется запись `kind="answer"` со ссылкой на вопрос; руководителю создаётся (или дописывается) карточка «Уточнение» с контрактом реакции. Запись в реестре: `answers[q_id] = {text, at, answered, status, question_text, steps_at_answer, unblocked, cancelled_steps?}`. Статус `sent`, пока руководитель не отреагировал. При ответе-отказе («нет/не нужно») связанные шаги снимаются («снят по вашему ответу»). `need_explanation:true` (кнопка «Мне нужно пояснение») → **вопрос остаётся открытым** (`answered:false`, `answer_state:"waiting_explanation"`); текст-просьба без кнопки («поясни…») считается ОБЫЧНЫМ ОТВЕТОМ (W17-B §2, аудит B5) | `{question: OpenQuestion, answer: {question_id, text, at, state, label, needs_explanation}, steps: {cancelled: string[], cancelled_label}, unblocked: string[], note?: {task_id, merged, routed_to}}` | `400` (`need_explanation` не bool), `404` (`detail.question_id`), `409` уже отвечен (кроме встречного вопроса — на него можно ответить снова) |
| `answer_remind` | `question_id` | **«Напомнить руководителю»** (W14-D §2): короткий сигнал в ветку `[Михаил · напоминание] …` на корень и в открытую карточку «Уточнение»; отметка `reminded_at`/`reminders` в реестре. Реакция ищется ТОЙ ЖЕ функцией, что в окне вопроса (B-12): только от руководителя комнаты и только ПОСЛЕ ответа | `{question_id, reminded_at, reminders, text, routed_to, reaction: null, question}` | `404` ответа на вопрос нет. `409` руководитель уже отреагировал — `detail: {reason: "already_answered", question_id, answer: {question_id, text, at, state, label}, reaction: {text, at, status, label, cards, implicit, author}, reaction_label, question}`: ответ и найденная реакция приходят ВМЕСТЕ с ошибкой, чтобы окно показало их, а не пустое «уже ответил» |
| `answers_resend` | — | **«Отправить руководителю накопленные ответы»** (W17-B, только по клику): одним сообщением в ветку `[Михаил · накопленные ответы] …` и одной карточкой «Уточнение» руководителю — все ответы без реакции, по порядку вопросов, со ссылками `q_…` на каждый вопрос и требованием отреагировать на каждый. Автоматически (по таймеру/при открытии) не отправляется никогда: это запускает настоящего агента. Отметка `resent_at`/`resend_count` в реестре ответов | `{sent: int, question_ids: string[], task_id, at, note?: {task_id, merged, routed_to}}` | `409` нечего отправлять (`detail.reason=\"nothing_to_resend\"`, `detail.pending=0`) |
| `decision` | `decision_id` (`d_…` или `<rid>:d_…`), `decision` (`accept`\|`reject`\|`discuss`), `text?` (1..12000; для `discuss` обязателен) | **«Принять» / «Отклонить» / «Обсудить» решение** (W19-D2 §1, B-20): в ветке — метка `[Михаил · решение d_x «текст решения»] Принять: …`, запись в реестре `answers[d_x]` (`kind="decision"`), руководителю — карточка «Уточнение» с контрактом реакции (`metadata.reaction.decision_id/outcome`). Принятое/отклонённое сразу уходит из «Нужен ваш ответ» и из счётчиков; `discuss` оставляет решение за Михаилом (`open=true`). | `{decision_id, decision: Decision, message, action_label, answer_label, pending_label, chain_hint, note?: {task_id, merged, routed_to}}` | `400` (`decision` вне `accept/reject/discuss`, пустой текст у `discuss`, длинный текст), `404` решения нет (`detail.decision_id`) |
| `step_drop` | `step_id` (`ns_…` или `<rid>:ns_…`) | **«Не согласен — убрать»** (W14-D §4, W14-H §12): шаг помечается убранным (`steps_dropped` + `step_actions` в реестре), снимается в бэклоге комнаты (`dismissed`), руководителю уходит `[Михаил · убрать шаг ns_x «…»]` | `{step_id, step: NextStep, dropped: string[], message, reaction_label: "шаг убран"}` | `400` кривой id, `404` шага нет |
| `step_agree` | `step_id` | **«Согласен»** (W14-H §12): действие пользователя по шагу — ответ на ЭТОТ шаг. Метка в ветке `[Михаил · согласен с шагом ns_x «…»]`, запись `step_actions[ns_x] = {kind:"agree", at}`; статус шага не меняется (его ведёт руководитель), `reaction_label` = «руководитель ещё не ответил» до реакции | `{step_id, step: NextStep, message, reaction_label, pending_label, log}` | `400` кривой id, `404` шага нет |
| `step_comment` | `step_id`, `text` (1..8000) | **«Прокомментировать»**: комментарий к шагу как ответ на него — `[Михаил · комментарий к шагу ns_x «…»] текст`; текст виден в `step.user_action.text` и в истории | `{step_id, step, message, reaction_label, pending_label, log}` | `400` нет текста/кривой id, `404` шага нет |
| `step_move` | `step_id`, `due?` (`YYYY-MM-DD`\|null), `owner?` (профиль), `direction?` (`up`\|`down`) | **«Переместить»**: срок, исполнитель или порядок. Срок пишется в реестр (`step_actions`) и в бэклог комнаты; порядок — `steps_order`; метка `[Михаил · переместить шаг ns_x «…»: срок …, исполнитель …, выше/ниже]`. Хотя бы одно из полей обязательно | `{step_id, step, message, reaction_label, pending_label, log}` | `400` нет изменений / кривой `due`/`owner`/`direction`, `404` шага нет |
| `unblock` | `task_id?`, `text?` | снять блокировку (одну или все) | `{unblocked: string[]}` | `409` нечего разблокировать |
| `decompose` | — | **«Разложить сейчас»**: явный LLM-вызов декомпозиции (таймаут 180 с). Разрешён только по клику пользователя и только для `queued` | `{decomposed: true, child_ids: string[], dispatcher}` | `409` уже разложен (`detail.reason="already_decomposed"`, `detail.status`) |
| `revise` | `text` (1..8000) | «Доработать свод» от Михаила (**единственный** путь доработки, W17-S §1): создаёт карточку-доработку (`role=revision`, parent — критик или корень), ответ уходит в `synthesizing`; готовая доработка = новая версия | `{version: int, previous_version: int, revision_task_id: string, note: string, digest: {version, count, note}}` | `400` пустой текст, `409` ответа ещё нет (`detail.reason="synthesis_pending"`) |
| `critique` | — | запустить критика вручную (повторная проверка текущей версии); `round += 1` | `{round: int, critic_task_id: string, state: "reviewing"}` | `409` у комнаты нет критика (`detail.reason="no_critic"`), `409` ответа ещё нет |
| `reassign` | `task_id`, `profile` | сменить исполнителя карточки; снимает `outside_room`, если профиль из состава комнаты | `{task: TaskNode}` | `400` не участник комнаты (`detail.available`), `404` нет карточки, `409` карточка done/archived |
| `rework` | `task_id?`, `text` (1..8000) | «на доработку» конкретной карточки (по умолчанию — критик/корень) с указанием | `{task: TaskNode}` | `400`, `404`, `409` карточка archived |
| `retry` | `task_id?`, `idempotency_key?` | перезапустить упавшие карточки. **Ключ идемпотентности (W19-T, B-25):** `idempotency_key` — случайный ключ одного нажатия (`^[A-Za-z0-9._:\-]{6,120}$`, клиент строит `retry-<task>-<время36>-<случайное>`). Повторная доставка того же ключа в пределах 120 с не запускает действие второй раз: сервер возвращает сохранённый ответ с `duplicate: true`. Другой ключ (новое нажатие) — новое действие. Без ключа работает как раньше (обратная совместимость) | `{retried: string[], duplicate?: true}` | `400` кривой `idempotency_key` (`detail.field="idempotency_key"`), `409` нет упавших |
| `archive` | — | архивировать корень и детей | `{archived: string[], changed: bool}` | `409` уже в архиве (`changed:false` не ошибка — `200`) |
| `clone` | `title?` | новый запрос с тем же текстом и флагом критика | `{root_id, warning?}` + `201 Created` | `502` |

`400 bad_request` — неизвестный `action` (`detail.allowed`) или нет обязательного поля. `404 not_found` — нет `rid`. `409 conflict` — состояние не позволяет действие (`detail.reason`).
`request` в ответе — свежий `RequestSummary` с новым `critic`-блоком; UI обновляет ленту и страницу без перезагрузки.
`clone` возвращает `201`; остальные — `200`.

Порядок пост-синтез цикла (ARCH §12.4): корень `done` → (если `critic=true` и `round < max_rounds`) карточка-критик → вердикт `revise` → карточка-доработка → новая версия → снова критик, пока не `approve` или не исчерпаны раунды. Приложение само `decompose/swarm/specify` не запускает никогда; `decompose` — только этим действием по клику.

#### Диалог по запросу

Михаил уточняет задачу, пока команда работает, или задаёт вопрос после ответа. Сообщение
уходит **руководителю комнаты** (`room.orchestrator`, иначе исполнитель корня) — он решает,
кого это касается, и раздаёт работу участникам (docs/WAVE8-RULES.md, Д1). Рассылки по всем
дочерним карточкам нет.

#### `ThreadItem` — элемент ленты

| Поле | Тип | Описание |
|---|---|---|
| `id` | string | стабильный в пределах ответа: `c12` (комментарий), `e31` (событие), `v2` (версия), `s7` (итог карточки «Уточнение») |
| `at` | int | время, unix-секунды (локальную зону рисует клиент) |
| `author_kind` | `mikhail \| agent \| system` | кто написал |
| `author` | string | `mikhail`, профиль (`team-director`) или `system` — ключ, не подпись |
| `author_label` | string | подпись по-русски: `Михаил`, `Глеб · руководитель комнаты`, `Антон · исполнитель`, `Диалог запроса` |
| `text` | string | текст комментария/события (события — короткая русская фраза) |
| `task_id` | string \| null | карточка, к которой относится запись |
| `kind` | `message \| comment \| event \| answer` | `message` — написал Михаил, `comment` — команда, `event` — системная строка (тонкая серая), `answer` — **ответ на конкретный вопрос** (W14-D §1) |
| `answer_kind` | `answer \| explanation_request` | только при `kind="answer"`: ответ или просьба пояснить (встречный вопрос) |
| `question_id` / `question_text` | string \| null | только при `kind="answer"`: на КАКОЙ вопрос отвечено |
| `text_mark` | string | только при `kind="answer"`: полный текст с меткой (`[Михаил · ответ на вопрос q_x «…»] …`); `text` — сам ответ |

Лента = все комментарии канбана по дереву запроса (корень + дочерние ± карточки
«Уточнение» этого запроса) **по возрастанию `at`** + события: «Руководитель разложил
запрос…», «Уточнение передано руководителю комнаты.», «Версия N готова.», «Карточка ждёт
решения…», «Шаг готов…». Итог карточки «Уточнение от Михаила» (`task_runs.summary`)
показывается как сообщение команды с ролью и аватаром руководителя. Лимит — 500 записей.
Сбой чтения не роняет страницу: `thread: []`.

#### `POST /api/requests/{rid}/messages`

Тело: `{text, attach?: string[], urgent?: bool}`.

| Поле | Тип | Обяз. | Правило |
|---|---|---|---|
| `text` | string | да | 1 … 1 000 000 знаков (`MAX_TEXT`), без NUL; дословно |
| `attach` | string[] | нет | ≤10 путей из разрешённых мест; копии не создаются — пути перечисляются в сообщении и в карточке; чужой путь → `403` |
| `urgent` | bool | нет | «Срочно: пересмотреть план целиком» — меняет **только формулировку** инструкции руководителю (метка сообщения та же); по умолчанию `false` |

`201` → `{message: ThreadItem, thread: ThreadItem[], routed_to: string, merged: bool}` +
диагностика: `task_id` (карточка «Уточнение»), `status` (статус запроса), `revision`
(готовый запрос), `retry` (упавший). `Location: /api/requests/{rid}`.

Что происходит (Д1):

1. комментарий `[Михаил · уточнение] <текст>` **на корневую карточку** (автор `mikhail`) — сразу видно в ленте;
2. карточка **«Уточнение от Михаила»**: `assignee = руководитель комнаты`, `tenant = room:<id>`,
   **не** дочерняя корня (`--parent` не ставится — иначе запуск ждал бы конца корня).
   В теле: дословный текст, id корня и инструкция руководителю (а–е): перечитать корень и
   карточки (`kanban_show`), решить, кого это касается, передать каждому затронутому
   комментарием `[Руководитель → <роль>] …` на его карточку (`kanban_comment` работает
   по чужим карточкам), при необходимости создать карточку (`kanban_create` + `kanban_link`),
   остальных не трогать, на корне написать «Принял; передал: …», в своде — блок
   «Учтено из уточнений». Переназначения среди инструментов агента нет — в инструкции его нет;
3. **пакетирование:** если предыдущая карточка «Уточнение» этого запроса ещё не взята в
   работу (`triage/todo/ready/running`) — новый текст дописывается к ней комментарием,
   новой карточки нет, `merged: true`;
4. запрос `done` → дополнительно существующая «На доработку» (`revise`, версия N+1) с этим
   текстом (`revision` в ответе); запрос упал → повтор (`retry`: `unblock`/`promote`) с тем же
   сообщением (`retry` в ответе). Ничего не убивается и не останавливается;
5. после записи вызывается зеркало Obsidian (`vault_mirror.sync_request`), если модуль готов.

`400 bad_request` — пустой/длинный текст, NUL, `attach` не список, `urgent` не bool.
`403 forbidden` — путь вложения вне разрешённых. `404 not_found` — нет запроса
(`detail.reason="not_root"`) или комнаты. `409 conflict` — комната в архиве, у комнаты нет
руководителя и у корня нет исполнителя. `502 engine_error` — CLI не создал карточку.
Тексты ошибок — по-русски. Мок: принимает сообщение, добавляет правдоподобный ответ
руководителя через 3–6 с.

### 6.4 Команда, карта, бэклог

#### `GET /api/rooms/{id}/team`
Кто в комнате и что делает сейчас. Параметров нет.
`200` → `{room, generated_at, dispatcher: {ok, state, pid, warning}, members: TeamMember[]}`. Порядок `members`: директор комнаты первым, затем остальные в порядке состава (критик входит с `role:"critic"`).
У каждого участника есть подпись (WAVE13 §11): `name`, `job`, `label` («Анна — финансы и экономика (CFO)»), `label_source`, `custom_label`, `description`, `label_pending` — см. §3.5.
У каждого участника также `model_settings` (что выбрано, §6.11) и необязательное `model_sent` —
«что реально ушло» у его последнего запуска в комнате (`{}`, если запусков не было): поля
`provider, model, reasoning, label, text, confirmed, expected, mismatch, note, cost_usd` (§6.11).
**W19 B-29:** `mismatch` — только если карточка ушла не на выбранную модель ПОСЛЕ последней смены модели участника. Если смена позже последнего запуска или в очереди уже есть карточка с верной моделью — `mismatch:false`, `pending:true` и `pending_text:"Новая модель применится со следующей задачи"`; рядом `changed_at` (когда меняли модель, из `agent_models[…].updated_at`), `run_at` (начало последнего запуска) и необязательное `queue: {id, status, created_at}` — та самая карточка в очереди с верной моделью. Интерфейс показывает `pending` спокойной строкой, без значка тревоги.
`state=working` — есть активный run профиля; `waiting` — blocked-карточка или вопрос к нему. `404 not_found`.
→ `fixtures/team_cafe.json`

#### `GET /api/rooms/{id}/roadmap`
Этапы проекта и решения. `200` → `Roadmap` (§3.5). `404 not_found`. Если у комнаты нет своих этапов — `phases: []`, `current: null` (раньше здесь были шесть чужих этапов из константы), `decisions` — как есть.
→ `fixtures/roadmap_cafe.json`

#### `GET /api/rooms/{id}/backlog`
«От вас нужно»: шаги, будущие вопросы, вопросы, решения. Query: `status` (через запятую), `kind` (через запятую), `limit` (по умолчанию 200).
`200` → `{room, updated_at, counts: {open, asked, done, dismissed, total, hidden_service}, items: BacklogItem[], sections, hidden_service?}`.
`400 bad_request` — неизвестный `status`/`kind`. `404 not_found`.
→ `fixtures/backlog_cafe.json`

**Статус вопроса — из реестра ответов (W17-B §7, аудит B7).** Раньше бэклог хранил свой `status`
и после ответа Михаила вопрос продолжал висеть в «ждут вашего решения» (боевой `q_e7b16109`:
`answered=true` в детали, `status=open` в «От вас нужно»). Теперь при каждом чтении:

* вопрос с ответом → `status: "done"`, `state_label: "отвечен"`, `answer_state`, `answer`, `answered_at`;
* просьба пояснить (`need_explanation`) → `status: "open"`, `state_label: "ждёт пояснения"`;
* иначе `status: "open"`, `state_label: "ждёт вашего ответа"`;
* близнецы (см. `OpenQuestion.aliases`) схлопываются в один элемент — `aliases` у элемента, счётчики
  и `sections` считаются по уникальным;
* служебные вопросы (число раундов критика) не показываются: `counts.hidden_service` — сколько скрыто,
  `hidden_service: [{id, text}]` — какие (для диагностики).

`counts` и `sections` пересчитываются на чтении, поэтому вкладка «От вас нужно», бейдж и страница запроса
показывают одно и то же сразу после ответа — без ожидания следующего прохода плана. Данные на диске
не перезаписываются (миграция чтением).

#### `POST /api/rooms/{id}/backlog/{item_id}`
Отметить шаг/вопрос. Тело: `{status: "open"|"asked"|"done"|"dismissed", due?: "YYYY-MM-DD"|null}`.
`200` → `{item: BacklogItem, counts: {...}}`. `400 bad_request` — плохой статус/дата. `404 not_found` — нет комнаты/элемента.
`409 conflict` — элемент уже `asked` и у него есть активный запрос (сначала закройте запрос).

#### `POST /api/rooms/{id}/backlog/{item_id}/ask`
Кнопка «Задать»: создаёт запрос из текста элемента и помечает его `asked`.
Тело: `{}` или `{text?}` (переопределить текст запроса). `201` → `{request: RequestSummary, item: BacklogItem, root_id: string, request_id: string}`.
`request_id` и `root_id` — одно и то же (id новой корневой карточки); `request_id` оставлен как каноническое имя поля в журнале/реестре, `root_id` — для совместимости с формой `POST /api/rooms/{id}/requests`.
`400 bad_request` — пустой текст. `404 not_found`. `409 conflict` — уже задан (`detail.asked_request`). `502 engine_error` — карточка не создалась (статус элемента не меняется).

### 6.5 Отчёты, журнал, поиск

#### `GET /api/rooms/{id}/reports`
Галерея отчётов комнаты. `200` → `{room, items: ReportIndexItem[]}`. `404 not_found`.
Битый `report.json` попадает в индекс с `exists:true`; предупреждение о валидации — в `GET /api/reports/{room}/{key}` (`502`) и в журнале.
→ `fixtures/reports_cafe.json`

#### `GET /api/reports/{room}/{key}`
Нормализованный отчёт. `200`:
* spec: `ReportSpec` (`{version, title, subtitle?, summary?, blocks[]}`) — ровно то, что принимает `reportkit/validate.py`;
* html: `{kind: "html", url: "/api/file?path=…", title}` — рендерится в sandbox-iframe.
Метаданные отчёта (комната, ключ, путь, запрос) — в `GET /api/rooms/{id}/reports`, в теле отчёта их нет.
Заголовок `ETag`; `If-None-Match` → `304`. `404 not_found` — нет отчёта/комнаты. `502 engine_error` — spec не прошёл `reportkit/validate.py` (`detail.errors[]`).
→ `fixtures/report_unit_economics.json`

#### `GET /api/rooms/{id}/journal`
Хронология комнаты. Query: `limit` (1..200, 50), `q` (подстрока), `cursor` (назад по времени).
`200` → `{room, items: JournalEvent[], next_cursor, has_more}`. `400 bad_request`. `404 not_found`.
→ `fixtures/journal_cafe.json`

#### `GET /api/search`
Глобальный поиск (Ctrl+K). Query: `q` (2..200 символов, обязательно), `room` (опционально), `limit` (1..50, 20).
`200` → `{q, room: string|null, total, items: SearchHit[]}`; `total` — всего совпадений **до** применения `limit`, `items` — страница (≤ `limit`). Если модуль поиска не умеет считать общее число, сервер отдаёт `total = len(items)` — фронтенд не должен показывать «найдено N», если `total <= len(items)`; надёжный признак «есть ещё» — `total > len(items)`.
`400 bad_request` — короткий/пустой `q`. `404 not_found` — указана несуществующая комната.
→ `fixtures/search.json`

### 6.6 Файлы, календарь, экспорт

#### `GET /api/logs?name=<файл>&tail=<N>`
Хвост журнала сервера из `data/logs/` — **единственный** способ прочитать журнал по HTTP (`/api/file` файлы `*.log` не отдаёт: `403`, `detail.reason="blacklisted"`).
Query: `name` — имя файла (`engine.log`, `app.log`, `app.log.1`; по умолчанию `engine.log`), только `[A-Za-z0-9._-]`, без `..`; `tail` — последние N строк (1..1000, по умолчанию 200).
`200` → `text/plain; charset=utf-8`, `Cache-Control: no-store`. Значения секретов маскируются: `password=`, `passwd=`, `pwd=`, `token=`, `secret=`, `api_key=`, `api-key=`, `access_key=`, `Authorization: Bearer …` → `***` (`server/http_util.mask_secrets`).
`400 bad_request` — плохое `name`/`tail`. `403 forbidden` — путь вне `data/logs`. `404 not_found` — журнала нет.

#### `GET /api/file?path=<абсолютный путь>[&room=<id комнаты>]`
Отдача файла из белого списка. `200` — байты; `Content-Type` по расширению, `Content-Length`, `Content-Disposition: inline` (для `docx/xlsx/pptx` — `attachment`).
`400 bad_request` — нет `path`. `403 forbidden` — вне корней или чёрный список (`detail.reason = "outside_roots"|"blacklisted"`). `404 not_found` — файла нет (с `room` текст «Файл удалён или перемещён»).
Range-запросы не поддерживаются (всегда `200`, не `206`). SVG отдаётся `image/svg+xml` с `Content-Security-Policy: sandbox`. Для `docx/xlsx/pptx` — `Content-Disposition: attachment; filename="<имя>"`, для остальных — `inline`.
Белый список корней: `data/`, `C:\TeamRoom\Vault`, `C:\TeamRoom\Team`, `C:\TeamRoom\Agents`, `%LOCALAPPDATA%\hermes\kanban\boards\*\workspaces`; дополнительно проверяются `realpath`, расширение (`.md .txt .json .csv .html .png .jpg .jpeg .gif .webp .svg .pdf .docx .xlsx .pptx .ics .log`) и чёрный список имён в последних 4 сегментах пути. Исходники `.py`, `.js`, `.css` через `/api/file` не отдаются.

**`room` (WAVE19, B-22).** С параметром `room` белый список расширяется папкой проекта этой комнаты
(после проверки `project_dir.check_dir`) и её зеркалом в Vault — так открываются файлы карточки.
Файл, который лежит **вне** этих мест, но прошёл отдельную проверку (настоящий файл, разрешённое
расширение, не запрещённое имя ни в одном сегменте, не системный каталог, не каталог Hermes,
не каталог программы и не `data/`), тоже отдаётся — интерфейс показывает к нему предупреждение
`artifact_outside` (B-27). Без `room` — ровно прежнее поведение.

#### `POST /api/files/launch` (W26-D3)
«Открыть» и «Показать в папке» для файла карточки. Тело: `{path, room?, mode?}`,
`mode` — `"open"` (по умолчанию) или `"reveal"`.
`200` → `{ok: true, path, mode, opened: true, revealed: <mode=="reveal">}`.

* `open` — файл открывает **программа по умолчанию** в сеансе человека (`proc.open_path`,
  на Windows `os.startfile` = ShellExecute, то же, что двойной клик в проводнике). Так
  открываются `.docx/.xlsx/.pdf/.md` — браузер тут не участвует: внутри окна Hermes Desktop
  `window.open` молча блокируется, и прежняя кнопка «Открыть» не делала ничего.
* `reveal` — проводник с выделением файла: `explorer /select,"<путь>"` (кавычки вокруг **пути**,
  а не вокруг всего аргумента; прежняя форма со списком аргументов ломалась на любом пути
  с пробелом и открывала «Документы»). Если запустить не удалось — открывается папка файла.

Проверки те же, что у `/api/file?room=…`: белые корни комнаты (папка проекта + зеркало),
внешние файлы B-27, чёрный список имён, запрет исполняемых типов и журналов.
`400` — нет `path`, `mode` не из списка, файл не открылся; `403` — файл есть, но открыть
нельзя (`detail.reason="blacklisted"`); `404` — комнаты или файла нет («Файл удалён или перемещён»).
Без `room` проверяется общий белый список (как у `/api/file`).

#### `POST /api/artifacts/open`
Прежний адрес «Показать в папке» (WAVE19, B-22), оставлен для совместимости: то же
действие, что `/api/files/launch {mode:"reveal"}`. Тело: `{path, room}`.
`200` → `{ok: true, path, revealed: true}`. Проверки те же, что у `/api/file?room=…`:
`400` — нет `path`/`room` или проводник не запустился; `403` — файл есть, но открыть нельзя
(`detail.reason="blacklisted"`); `404` — комнаты нет или файла нет по этому пути
(«Файл удалён или перемещён»). Журналы (`*.log`, `data/logs`) по-прежнему не открываются.

#### `POST /api/open`
«Открыть в проводнике». Тело: `{path, kind?, mkdir?}`.

* `kind: "dir"` — путь считается **папкой** (папка проекта комнаты): сервер сам выводит
  окно проводника вперёд (`EnumWindows` → `SetForegroundWindow`), даже если запущен
  скрыто. `200` → `{ok: true, path, revealed: true, kind: "dir", foreground: bool, reason}`
  (`reason` — подсказка, если окно не удалось поднять: «найдите его на панели задач»).
  `mkdir: true` — сначала создать папку (`mkdir -p` под разрешённым путём) и открыть.
  Если папки нет: `200` → `{ok: false, revealed: false, kind: "dir", path, reason: "Папки нет на диске — её можно создать", can_create: true}`.
  Запрещённый путь (системные каталоги, `%LOCALAPPDATA%\hermes`, `data/`) → `400 bad_request`
  с русской причиной, проводник не запускается.
* Без `kind` (старое поведение) — `explorer /select,<path>` для файла из белого списка:
  `200` → `{ok: true, path, revealed: bool}`. `400 bad_request` — нет `path`. `403 forbidden`. `404 not_found`.

Путь никогда не уходит в оболочку (`shell`): запуск — `os.startfile` / `explorer.exe <путь>`
списком аргументов. `TEAMROOM_NO_SHELL=1` выключает запуск (тесты, headless-прогоны).

#### `GET /api/ics?room=<id>&items=<id,id>&all=1`
Экспорт в календарь. Нужен `room` и либо `items` (id элементов бэклога), либо `all=1` (все `open` элементы с `due`).
`200` → `text/calendar; charset=utf-8`, `Content-Disposition: attachment; filename="<room>.ics"`.
События all-day: `DTSTART;VALUE=DATE:YYYYMMDD`. `UID: <room>-<item_id>@team-room.local`. `SUMMARY` — текст шага (обрезка 200), `DESCRIPTION` — владелец и источник.
`400 bad_request` — нет `room`, нет `items` и нет `all`. `404 not_found` — комната не найдена или **ни один** id из `items` не найден (`detail.missing[]` — какие именно); элементы без `due` в календарь не попадают.

#### `GET /api/export/request/{rid}.md` и `.html`
Экспорт запроса: ответ, шаги, вопросы, решения, файлы, дерево.
`.md` → `text/markdown; charset=utf-8`; `.html` → `text/html; charset=utf-8`, один автономный файл (рендерер встроен, без сети, отчёт — статикой).
`Content-Disposition: attachment; filename="<slug>.md|html"`. `404 not_found` — нет запроса. `409 conflict` — `synthesis.state != "ready"` (`detail.reason="synthesis_pending"`). `502 engine_error` — экспорт HTML упал (`detail.stderr`).

### 6.7 Обслуживание комнаты

#### `POST /api/rooms/{id}/consolidate`
Создаёт запрос директору «обнови STATE.md по комнате»: собрать накопленные решения/шаги/фазы в актуальный STATE.md и зеркало Vault.
Путь файла — единый для сервера и агентов: `<vault_dir>/<room.id>/STATE.md` (по умолчанию `C:\TeamRoom\Vault\Projects\Rooms\<id комнаты>\STATE.md`, рядом `Журнал.md`); его пишет ingest и его же получает агент в блоке КОМНАТА-КОНТРАКТ (`server/config.py: state_md_path`). Каталог назван по `room.id`, а не по названию: переименование комнаты файл не перемещает.
Тело: `{note?: string}` (до 2000 символов). `201` → `{request: RequestSummary, root_id: string, warning?}`.
`404 not_found`. `409 conflict` — такой запрос уже активен (`detail.existing_root_id`) или создавался менее 10 минут назад. `502 engine_error`.

#### `POST /api/adopt`
Привязать существующую корневую карточку к комнате, не создавая новую. Работает и для карточек из `GET /api/rooms/{id}/candidates`.
Тело: `{room, task_id}`. `200` → `{ok: true, request: RequestSummary, adopted: bool, already: bool, warning?: {code, message}}`.
`adopted` — карточка привязана (первый вызов `true`), `already` — она уже была в реестре комнаты (повторный вызов идемпотентен: `adopted:true, already:true`, второй записи нет). `warning` — если карточка создана вне приложения и metadata пустые.
Привязка = **запись в реестр** `data/state/<room>/requests.json` с `adopted: true` (и `retargeted: true`): карточка появляется в ленте комнаты и читается как обычный запрос. `tenant` карточки **не меняется** — движок правится только через `assign`, а метка `room:<id>` остаётся как была (в т.ч. `null` у карточек, созданных вне комнат). Отдельной функции `engine_write.adopt` нет.
`400 bad_request` — у карточки есть родители (`detail.reason="not_root"`). `404 not_found` — нет комнаты/карточки. `409 conflict` — карточка уже в другой комнате (`detail.tenant`). `501 engine_error` — модуль не умеет привязку (не должно случаться в v2; `detail.module="engine_write"`).

### 6.8 Сводная таблица

| Метод | Путь | Ответ | Fixture |
|---|---|---|---|
| GET | `/api/health` | Health | `health.json` |
| GET | `/api/config` | Config | `config.json` |
| GET | `/api/profiles` | `{items, count, dispatcher}` | `profiles.json` |
| POST | `/api/dispatcher/kick` | `{ok, spawned, …}` | — |
| GET | `/api/dispatcher/preview` | `{ok, preview, report, at}` | — |
| GET | `/api/rooms` | `{rooms, engine, updated_at}` | `rooms.json` |
| GET | `/api/rooms/summary` | `{ok, at, count, engine, rooms[{…, last[]}]}` | `rooms_summary.json` |
| POST | `/api/rooms` | RoomDetail (201) | — |
| GET | `/api/rooms/{id}` | RoomDetail | `room_cafe.json`, `room_fn.json` |
| PATCH | `/api/rooms/{id}` | RoomDetail | — |
| POST | `/api/rooms/{id}/archive` | `{ok, changed, room}` | — |
| POST | `/api/rooms/{id}/members` | `{member, members}` (201) | — |
| DELETE | `/api/rooms/{id}/members/{profile}` | `{ok, members}` | — |
| PUT | `/api/rooms/{id}/agents/{profile}/label` | `{ok, profile, label, members}` | — |
| POST | `/api/rooms/{id}/requests` | `{request, root_id, dispatcher}` (201) | — |
| GET | `/api/rooms/{id}/requests` | `{items, next_cursor, has_more}` | `requests_cafe.json`, `requests_fn.json` |
| GET | `/api/rooms/{id}/candidates` | `{room, count, items}` | `candidates_cafe.json` |
| GET | `/api/requests/{rid}` | RequestDetail | `request_*.json` |
| POST | `/api/requests/{rid}/actions` | `{ok, action, request, …}` | — |
| GET | `/api/rooms/{id}/team` | `{members, dispatcher}` | `team_cafe.json`, `team_fn.json` |
| GET | `/api/rooms/{id}/roadmap` | Roadmap | `roadmap_cafe.json` |
| GET | `/api/rooms/{id}/backlog` | `{items, counts}` | `backlog_cafe.json` |
| POST | `/api/rooms/{id}/backlog/{item_id}` | `{item, counts}` | — |
| POST | `/api/rooms/{id}/backlog/{item_id}/ask` | `{request, item, request_id, root_id}` (201) | — |
| GET | `/api/rooms/{id}/reports` | `{items}` | `reports_cafe.json` |
| GET | `/api/reports/{room}/{key}` | ReportSpec \| `{kind:"html",url}` | `report_unit_economics.json` |
| GET | `/api/rooms/{id}/journal` | `{items, next_cursor}` | `journal_cafe.json` |
| GET | `/api/search` | `{q, total, items}` | `search.json` |
| GET | `/api/file?path=` | байты | — |
| GET | `/api/logs?name=&tail=` | `text/plain` (секреты маскируются) | — |
| POST | `/api/open` | `{ok, revealed}` | — |
| POST | `/api/files/launch` | `{ok, path, mode, opened}` — «Открыть» (`mode:"open"`) / «Показать в папке» (`"reveal"`), `{path, room, mode?}` | — |
| POST | `/api/artifacts/open` | `{ok, path, revealed}` — прежний адрес «Показать в папке» (`{path, room}`) | — |
| GET | `/api/ics` | `text/calendar` | — |
| GET | `/api/export/request/{rid}.md\|.html` | текст/HTML | — |
| POST | `/api/rooms/{id}/consolidate` | `{request, root_id}` (201) | — |
| POST | `/api/adopt` | `{ok, request}` | — |
| GET | `/api/stt/status` | `{available, loaded, loading, model, device, reason, max_recording_s, part_seconds}` | — |
| POST | `/api/stt/transcribe` | `{text, raw, duration_s, elapsed_s, language, model, device, corrections}` | — |
| GET | `/api/stt/dictionary` | `{terms, replacements, updated_at}` | — |
| PUT | `/api/stt/dictionary` | `{terms, replacements, updated_at}` | — |
| POST | `/api/stt/learn` | `{suggestions, saved}` | — |
| PUT/POST | `/api/stt/recordings/{rid}/parts/{n}` | `{rid, part, bytes, seconds_est, parts_total}` | — |
| GET | `/api/stt/recordings/{rid}` | `{rid, parts, parts_total, bytes, seconds_est}` | — |
| GET | `/api/stt/recordings/{rid}/parts/{n}` | аудио части (Raw) | — |
| GET | `/api/stt/recordings` | `{items, limit}` | — |
| DELETE | `/api/stt/recordings/{rid}` | `{rid, removed}` | — |
| GET | `/api/rooms/{id}/plan` | `{plan: Plan\|null, summary}` | `plan_cafe.json`, `plan_fn.json` |
| POST | `/api/rooms/{id}/plan/approve` | `{plan, started, queued, warnings}` | — |
| PATCH | `/api/rooms/{id}/plan/streams/{sid}` | `{plan, warnings}` | — |
| POST | `/api/rooms/{id}/plan/streams` | `{plan, stream, warnings}` (201) | — |
| DELETE | `/api/rooms/{id}/plan/streams/{sid}` | `{plan, comment, warnings}` | — |
| POST | `/api/rooms/{id}/plan/rebuild` | `{root_id, title, slug, request}` (201) | — |

### 6.9 Диктовка (STT)

Локальное распознавание речи (faster-whisper) для поля ввода. Аудио не покидает компьютер: сервер пишет временный файл в `data/tmp/`, отдаёт его постоянному рабочему процессу `server/stt_worker.py` (отдельный интерпретатор с faster-whisper) и удаляет в `finally`. В журнал попадают только длины и тайминги — тексты диктовки не логируются.

Модель читается из `%LOCALAPPDATA%\hermes\config.yaml` (только чтение): `stt.local.model` (по умолчанию `large-v3-turbo`), `stt.local.device` (`cuda`), `stt.local.compute_type` (`float16`), `stt.local.initial_prompt`, `stt.local.vad`, `stt.language` (`ru`). Модель грузится при первом запросе (в это время `status.loading=true`), выгружается после 10 минут простоя — процесс при этом выходит и поднимается снова при следующей диктовке. Если CUDA не поднялась (нет `cublas64_12.dll`/`cudnn64_9.dll`), воркер один раз откатывается на `cpu/int8`, а причина уходит в `status.reason`. Таймаут распознавания — `max(60 с, 3 × длительность аудио)`; холодный старт получает дополнительный запас на загрузку модели.

Все маршруты — под теми же проверками `Host`/`Origin`, что и остальные (§2); `ETag` не отдаётся.

#### `GET /api/stt/status`
`200` →

```json
{"available": true, "loaded": false, "loading": false,
 "model": "large-v3-turbo", "device": "cuda", "reason": null,
 "max_recording_s": 600, "max_recording_source": "hermes",
 "max_recording_setting": "voice.max_recording_seconds", "max_recording_hermes_s": 600,
 "max_recording_min_s": 600, "part_seconds": 120}
```

**Лимит записи — из настроек Hermes** (WAVE13 §2): `voice.max_recording_seconds` из
`config.yaml` (при отсутствии — `stt.max_recording_seconds`), только чтение.
`max_recording_s` — применённый лимит: не меньше 10 минут (`max_recording_min_s`),
даже если в Hermes стоит 120 с; `max_recording_source` = `hermes` или `fallback`
(600 с); `max_recording_hermes_s` — что было в конфиге. Своих настроек диктовка не
заводит. `part_seconds` — длина одной части длинной записи (2 минуты по умолчанию;
на стендах задаётся переменной `TEAMROOM_STT_PART_SECONDS`, 10…600).

`available` — найден интерпретатор с faster-whisper (сначала текущий, затем `%LOCALAPPDATA%\hermes\installs\*\environments\*\venv\Scripts\python.exe`); `loaded` — модель в памяти; `loading` — идёт первая загрузка; `device` — фактическое устройство (`cuda` или `cpu` после отката); `reason` — русское объяснение, если что-то не так (`null` в норме). Модель этот запрос не запускает.

#### `POST /api/stt/transcribe?language=ru`
Тело — **байты аудио**, не JSON: `Content-Type: audio/webm | audio/ogg | audio/wav | audio/mp4` (`audio/x-wav`, `audio/wave`, `audio/mpeg` тоже принимаются). Лимит тела — **64 МиБ только у этого маршрута**; остальные маршруты принимают JSON до 8 МиБ. `language` необязателен (по умолчанию `stt.language`).

`200` →

```json
{"text": "Привет, это проверка распознавания речи.",
 "raw": "Привет, это проверка распознавания речи.",
 "duration_s": 7.3, "elapsed_s": 0.31, "language": "ru",
 "model": "large-v3-turbo", "device": "cuda",
 "corrections": []}
```

`text` — после замен словаря, `raw` — как распознано; `corrections` — какие замены сработали (`{from, to}`). **Тишина или отсутствие речи — успех с `text: ""`**, а не ошибка.

| Код | HTTP | Когда |
|---|---|---|
| `audio_empty` | 400 | тело пустое или в файле нет декодируемого аудио (мусор вместо записи) — `detail.reason` |
| `audio_too_big` | 413 | тело больше 64 МиБ (`detail.length`, `detail.limit`) |
| `bad_request` | 415 | `Content-Type` не аудио (`detail.allowed`) |
| `stt_unavailable` | 503 | не найден Python с faster-whisper, воркер не запустился |
| `stt_failed` | 500 | воркер упал или не уложился во время (`detail.reason`, `detail.stderr` — хвост) |

Ошибки — обычный конверт §1, сообщения по-русски («Распознавание речи не запущено: …», «Аудио больше 64 МиБ — сократите запись»).

#### Части длинной записи — `/api/stt/recordings/*` (WAVE13 §2)

Длинная запись (до лимита, минимум 10 минут) режется в интерфейсе на части по
`part_seconds`. Каждая часть — **самостоятельный файл**: интерфейс сразу кладёт её
на диск сервера (`data/stt-recordings/<rid>/part-0001.webm`, атомарно) и в
IndexedDB браузера, и только потом отправляет на распознавание. Поэтому обрыв
связи, закрытие окна или перезагрузка страницы не уносят наговоренное: части
остаются на диске, текст можно получить кнопкой «Распознать ещё раз».

`rid` — номер записи от браузера: `[a-z0-9][a-z0-9_-]{5,40}` (формат `r_<время><хвост>`);
некорректный — `400 bad_request`. Части нумеруются с 1, до 64 штук, до 64 МиБ
каждая, до 256 МиБ на запись (иначе `413 audio_too_big`). Хранение — не больше
30 записей и не дольше 72 часов, уборка ленивая (при обращении, не чаще раза в
минуту).

| Метод и путь | Что делает | Ответ |
|---|---|---|
| `PUT`/`POST /api/stt/recordings/<rid>/parts/<n>` | сохранить часть (тело — байты аудио) | `200 {rid, part, bytes, seconds_est, parts_total}` |
| `GET /api/stt/recordings/<rid>` | что уже сохранено | `200 {rid, created_at, mime, parts: [{n, bytes, seconds_est}], parts_total, bytes, seconds_est}` |
| `GET /api/stt/recordings/<rid>/parts/<n>` | аудио части (для повтора) | `200`, `Content-Type` части |
| `GET /api/stt/recordings?limit=20` | последние записи (восстановление) | `200 {items: [<как выше>], limit}` |
| `DELETE /api/stt/recordings/<rid>` | убрать запись (после вставки или отмены) | `200 {rid, removed}` |

Лимит тела у части — те же 64 МиБ (`body_policy` в `server/routes_stt.py`).
`seconds_est` — оценка длительности по размеру (opus ≈ 3 КБ/с), точная
длительность приходит из распознавания (`duration_s`) и уходит в прогресс
«Распознано 3 из 7 минут».

#### `GET /api/stt/dictionary` · `PUT /api/stt/dictionary`
Словарь диктовки — `data/dictation/dictionary.json`. Запись атомарная (временный файл рядом + `os.replace` с повторами при `PermissionError`), поэтому параллельное чтение никогда не видит половину файла.

```json
{"terms": ["Гранит", "TaskFlow"],
 "replacements": [{"from": "тескфло", "to": "TaskFlow", "count": 3, "added_at": 1793171000}],
 "updated_at": 1793171200}
```

`terms` дописываются к `initial_prompt` (итог усекается до ~800 символов, новые термины в приоритете). `replacements` применяются к тексту после распознавания: без учёта регистра, по границам слов, длинные фразы первыми, регистр первой буквы сохраняется. `count` — сколько раз правило добавляли (дубли схлопываются с суммированием счётчика).

`PUT` принимает `{terms?, replacements?}`: не переданное поле остаётся как было (это не «стереть», а частичное обновление). Валидация: термин — строка до 60 символов, до 300 терминов; замена — `from` ≠ `to` (без учёта регистра), до 200 записей, до 200 символов в поле. Нарушение → `400 bad_request` с `detail.field` (`terms` / `replacements`). Дубли схлопываются, `updated_at` проставляет сервер.

#### `POST /api/stt/learn`
`{raw, final, apply?}` — сравнить продиктованное (`raw`) и отправленное (`final`) пословно и предложить замены. Пунктуация и регистр не в счёт; вставки и удаления целых фраз игнорируются; предлагаются только замены, где с обеих сторон 1–3 слова.

`200` → `{"suggestions": [{"from": "тескфло", "to": "TaskFlow"}], "saved": []}`.
`apply: true` сразу сохраняет предложения в `replacements` и пополняет `terms` правильным вариантом (`saved` — что сохранено). `400 bad_request` — нет строк `raw`/`final`, текст длиннее 20000 символов, `apply` не bool (`detail.field`).


### 6.10 Проектный режим — карта проекта

Михаил пишет **один** главный запрос (бриф). Руководитель комнаты сам предлагает **карту проекта**: направления (workstreams) с исполнителями, зависимостями и сроками. После «Запустить» каждое направление становится под-запросом комнаты; Михаил не дробит запрос руками.

Настройки — у **конкретной** комнаты (общих «планов для всех комнат» нет): `launch_mode` (`plan_first` — план, потом «Запустить»; `auto` — сразу), `max_parallel_streams` (1..4, по умолчанию 2), `project_mode` (у новых комнат включён, у старых выключен до включения). Карта хранится в `data/state/<room>/plan.json`, запись атомарная, чтение ничего не переписывает.

```
Plan   = {version:int, brief_root_id:string|null, updated_at:int,
          status:"draft|approved|running|done", streams:[Stream], log:[LogItem]}
Stream = {id:"s1", title, goal, owner, members:[профиль], depends_on:[stream_id],
          due:"YYYY-MM-DD"|null, status:"proposed|queued|running|waiting_quota|review|done|dropped",
          request_id:string|null, reason, created_by:"manager|mikhail"}
LogItem= {at:int, who:"manager|mikhail|system", kind:"created|changed|added|launched|dropped|quota",
          text, stream_id:string|null}
```

Проверка блока `plan` от руководителя: ≤ 8 направлений, `owner`/`members` только из состава комнаты, `id` уникальны (приложение присваивает `s1…` само), `depends_on` без циклов и ссылок на несуществующие, заголовок 3–120 знаков, `due` — настоящая дата. Поля вне схемы отбрасываются. Ошибки — не исключение, а список русских `warnings`; непригодное направление просто не попадает в карту.

Жизненный цикл: `draft` (руководитель предложил) → `approve` → `approved` → `running` → `done`. Запуск идёт **только** по «Запустить»; очередь двигает `tick` (вызывается наблюдателем раз на комнату за тик, а также сразу после `approve`): направление стартует, когда все его `depends_on` в `done` и число `running` меньше `max_parallel_streams`. Под-запрос создаётся движком записи с контекст-блоком комнаты; `request_id` сохраняется в направлении, повторный запуск того же `stream.id` дублей не создаёт (ключ идемпотентности `plan:<room>:<stream_id>`).

Сторож лимита: если в дереве карточки направления есть исход с причиной `429`/`usage limit`, направление получает `waiting_quota` («ждёт лимит»), в журнал пишется запись `quota`, очередь новых не запускает. Как только лимит снят, ближайший `tick` возвращает направление в работу — молчаливых повторов нет.

Правки от руководителя (ответ на «Уточнение от Михаила», блок `plan_patch`): `change` полей `title/goal/due/members/status` применяется сразу; `add` нового направления — только как `proposed` (нужно «Запустить»); перезапуск сделанного (`status: running|queued` у `done/dropped`) — тоже только через «Запустить». Каждое изменение пишется в журнал плана с причиной.

#### `GET /api/rooms/{id}/plan`
`200` → `{plan: Plan|null, summary: {total, running, done, status}|null}`. Карты ещё нет — `plan: null` (это не ошибка: пустое состояние «Карты пока нет…»). `404 not_found` — нет комнаты. `409 conflict` — комната в архиве. `503 engine_error` — файл карты занят другим процессом (данные не подменяются).

#### `POST /api/rooms/{id}/plan/approve`
`{streams: ["s1","s2"] | "all"}` (по умолчанию `all`).
`200` → `{plan, started: [id…], queued: [id…]}` — `started` — что уже пошло в работу (учитывая зависимости и предел параллельности), `queued` — что встало в очередь.
`400 bad_request` — `streams` не список строк и не `"all"`. `404 not_found` — нет комнаты / карты нет / нет таких направлений (`detail.unknown`). `409 conflict` — комната в архиве или проектный режим выключен. Повторный вызов идемпотентен: `started` пустой, дублей нет.

#### `PATCH /api/rooms/{id}/plan/streams/{sid}`
Ручная правка Михаила: любые из `title, goal, due, members, depends_on, status` (+ `reason` — попадает в журнал).
`200` → `{plan, warnings}`. `400 bad_request` — пустое тело или не тот тип (`members` — список, `status` — строка). `404 not_found` — нет карты/направления. `409 conflict` — проектный режим выключен. Плохая дата и неизвестный статус не применяются, а объясняются в `warnings`.

#### `POST /api/rooms/{id}/plan/streams`
Добавить направление вручную (`created_by: "mikhail"`, статус `proposed` — запускается только «Запустить»).
`201` → `{plan, stream, warnings}`. `400 bad_request` — нет названия / короче 3 знаков / чужой исполнитель. `409 conflict` — в карте уже 8 направлений. `404 not_found` — карты ещё нет.

#### `DELETE /api/rooms/{id}/plan/streams/{sid}`
«Отложить»: `status: "dropped"` (физического удаления нет), `{reason?}` в теле.
`200` → `{plan, comment, warnings}`; `comment` — текст комментария, добавленного в карточку под-запроса, если направление уже запущено (`null`, если карточки нет). Зависимые направления при этом ждут — вернуть в работу можно `PATCH`-ем `depends_on` или статуса.

#### `POST /api/rooms/{id}/plan/rebuild`
«Собрать карту из запросов комнаты» (A7): создаёт запрос-бриф, в тело которого подставлены существующие запросы комнаты (название, итог, статус). Ответ руководителя приходит обычным блоком `plan`.
`201` → `{root_id, title, slug, request, ok: true, dispatcher: {ok, state, warning}}`. `409 conflict` — проектный режим выключен или не удалось создать карточку. Дальше — `GET /api/rooms/{id}/plan`.

#### Связь с запросом
В `RequestDetail` (§3.4): `brief: true` у главного запроса; `plan_link: {stream_id, title, status}` у под-запроса направления; `streams_summary: {total, running, done, status}` у брифа (сколько направлений, сколько идёт, сколько готово). Все три поля необязательные: у обычного запроса это `false`/`null`/`null`.

#### `POST /api/rooms/{id}/requests` с `brief: true`
Тело запроса становится лёгким брифом: «Не исследуй глубоко. Выдай: (1) как ты понял задачу, (2) вопросы к Михаилу, (3) блок `plan` — направления (3–8), (4) блок `stage`». Ответ руководителя разбирается тем же приложением: пригодные направления становятся черновиком карты (`draft`, `proposed`), запуск — только после «Запустить».


### 6.11 Модели участников — провайдер, модель, мышление

У каждого участника комнаты (руководитель, критик, исполнители) можно задать **свою** модель,
провайдера и уровень мышления — замену, которая действует **только в этой комнате** и только
для её карточек. По умолчанию берутся настройки самого агента (read-only из его конфига),
файлы профилей (`config.yaml`, `SOUL.md`, `skills`) не меняются никогда.

Хранение — поле комнаты `agent_models` в реестре комнат (`data/rooms.json`):

```
agent_models = {"<профиль>": {provider:string, model:string, reasoning:string,
                              updated_at:int}}
```

Нет записи для профиля = «как у агента». `provider` и `model` ходят парой (провайдер без
модели не применяется, и наоборот — половина пары отбрасывается при чтении, чтобы провайдер
комнаты никогда не «подхватывал» модель агента), `reasoning` — независимо: `none` (мышление
выключено) или один из `minimal, low, medium, high, xhigh, max, ultra`.

Идентификаторы моделей и провайдеров — **как в каталоге Hermes, посимвольно**: сервер не
угадывает регистр, префикс и подстроку (`deepseek-v4-pro:0813` ≠ `deepseek-v4-pro`,
`anthropic/claude-sonnet-5.5` ≠ `claude-sonnet-5-5`, `Model-A` ≠ `model-a`). Иначе «Применить»
молча ставил бы соседнюю модель. Провайдер-слаг регистронезависим, но если в каталоге есть два
провайдера, отличающихся только регистром, выбор такого слага — `400` (`detail.candidates`).

#### `GET /api/models`
Каталог выбора — **тот же, что в Hermes Desktop** («Поиск моделей»): только провайдеры с
рабочим ключом (`configured: true`). Читается из Hermes без сетевых запросов к провайдерам
(дисковый кэш/встроенные списки), кэшируется на 60 с.

```
{providers: [{id, label, configured: bool,
              models: [{id, label}]}],
 efforts:   [{id, label}],        // none, minimal, low, medium, high, xhigh, max, ultra
 source:    "hermes:inventory" | "unavailable",
 cached:    bool, updated_at: int, warnings: [string],
 agent_defaults: {<профиль>: {provider, model, reasoning,
                              source: "profile" | "global" | "none", note: string}}}
```

`agent_defaults` (W20-MODELS §1.5) — что стоит **в настройках самого агента** и откуда взято:
`profile` — свой `config.yaml` профиля; `global` — общая настройка Hermes (профиль модель не
задаёт), `note` объясняет это словами; `none` — модели нет нигде. В карту попадают все профили
Hermes на диске, а с `?room=<id>` ещё и участники этой комнаты (даже если файла профиля нет —
тогда `none`). Значения никогда не «похожие»: ровно то, что записано в конфиге.

`?refresh=1` — перечитать каталог, не дожидаясь 60 с. Пустой `providers` + `source:
"unavailable"` = Hermes недоступен: интерфейс предлагает «своя модель» (ручной ввод), ошибки
нет (`200`).

#### `GET /api/rooms/{id}` — что видит интерфейс
* `agent_models` — поле комнаты как есть (`{}`, если замен нет).
* `agent_models_meta` — то же для **всех** участников, включая руководителя и критика (их может
  не быть в `members[]`): `{profile: {profile, role, model_settings, agent_default}}` — по нему
  вкладка «Команда» рисует строку модели у каждого.
* у каждого `members[]` (и у руководителя/критика в `members[]`, если они там есть):
  * `model_settings = {provider, model, reasoning, source: "room"|"agent", updated_at:int}` —
    что реально применится к следующей карточке участника; `source:"agent"` — замена не задана;
    `provider`/`model` всегда одной парой из одного источника;
  * `agent_default = {provider, model, reasoning}` — настройки самого агента (для подписи
    «по умолчанию (из настроек агента): …»): `model.provider`, `model.default` и **только**
    `agent.reasoning_effort` из его `config.yaml`. `delegation.reasoning_effort` — уровень
    делегированных детей, за дефолт агента он не выдаётся. Пустая строка = «как в Hermes по
    умолчанию» (профиль без `model.default` тоже даёт пустую модель, а не чужую).
  * `agent_default_info = {provider, model, reasoning, source, note, label}` — то же плюс
    источник (W20-MODELS §1.5): `profile` (свой конфиг), `global` (общая настройка Hermes —
    профиль модель не задаёт) или `none`; `note` — готовая пометка по-русски. Форма
    `agent_default` не менялась: старые клиенты читают её как раньше.
  * в `model_settings` добавлены `default_source` и `default_note` — тот же источник
    «по умолчанию» рядом с выбором комнаты, чтобы интерфейс не собирал это из двух мест.

`POST /api/rooms` и `PATCH /api/rooms/{id}` принимают `agent_models` (та же форма, что в поле
комнаты; профили — только из состава комнаты, иначе `400`). Интерфейс правит модели **в карточке
участника** на вкладке «Команда» (`PUT …/agents/{profile}/model`); окно настроек комнаты модели не
меняет и при создании отправляет `agent_models: {}` — пока модель не выбрана в карточке, действует
настройка профиля. Ошибки проверки — русский текст,
`400`, тело `{error:{code:"bad_request",message,detail}}` как обычно.

#### `PUT /api/rooms/{id}/agents/{profile}/model`
```
{provider?: string, model?: string|null, reasoning?: string|null}
```
Пустой объект, `null` в теле или `model: null` без `provider` — «вернуть по умолчанию»: запись
участника стирается, действуют настройки агента.
`200` →
```
{ok: true, profile,
 member: Member,                       // с model_settings и agent_default
 agent_models: {профиль: {...}},
 applied: {cards_updated: int,         // сколько карточек дерева закрепили на новой модели
           pending_running: int,       // сколько карточек идёт на старой — нужна кнопка «Перезапустить»
           cards_failed: [{id, reason}],
           settings: {provider, model, reasoning, source, updated_at},  // эхо: что применится
           saved:    {provider, model, reasoning, updated_at} | {},       // что лежит в реестре
           cleared:  bool},
 warnings: [string]}
```
`applied.settings` — эхо сохранённого выбора (`source:"room"` — замена комнаты,
`source:"agent"` после «вернуть по умолчанию»), `applied.saved` — ровно запись реестра
(`{}` после сброса). Фронтенд сверяет по ним выбор с ответом и **не** должен подставлять
«ближайшую» модель: значения совпадают с отправленными посимвольно.

Применение: у комнаты обходятся **все её карточки по `tenant`** (`room:<id>`), а не только деревья
запросов из реестра: руководитель создаёт карточки сам (`hermes kanban create`, `created_by=default`,
часто без связи с родителем), и такие карточки в деревья не попадают — именно из-за этого работа
уходила на модель профиля (W13-A). Карточки **этого** профиля в статусах
`triage/todo/ready/blocked/queued` закрепляются на новой модели сразу (идемпотентно, событие
`model_override_set` в карточке); проход повторяется сторожем каждый тик (5 с), чтобы перехватить
карточку до допуска запуска. `running` и `done` не меняются: для идущих в ответе
`pending_running > 0` и работает `POST …/restart`. Изменение пишется в журнал комнаты
(`kind: "model_changed"`, «Модель участника «…» изменена: провайдер · модель · мышление»).

**Идущая карточка сменой модели не прерывается (W19-T, B-26).** В момент сохранения выбора
приложение запоминает идущие карточки этого участника (`id` + время старта попытки) в
`data/state/<комната>/model_pending.json`; сторож (`verify_started`, `POST …/models/check`)
видит такую карточку, понимает, что она стартовала ДО смены, и не снимает её с работы — в ответе
проверки она попадает в `pending: [{id, assignee, expected, actual, run_id, since}]`
с формулировкой «новая модель применится со следующей задачи». Отметка снимается сама, когда
у карточки появляется новая попытка (другое время старта): тогда расхождение снова считается
настоящим и перехватывается как раньше. Явный `POST …/restart` отметку снимает сразу — это
осознанное «перезапустить на новой модели, текущая работа будет прервана».

`400 bad_request` — профиль не в составе комнаты; задана модель без провайдера; провайдер не
настроен в Hermes («Провайдер «…» не настроен — в Hermes нет ключа»); модели нет в каталоге
провайдера (если каталог провайдера непуст — иначе разрешена «своя модель»; в `detail` —
`suggestions` (id, отличающиеся только регистром) и `other_providers` (у кого эта модель есть),
но применяются они только после явного выбора); уровень мышления вне списка. `404 not_found` —
нет комнаты. `503 engine_error` — реестр занят.

#### `POST /api/rooms/{id}/agents/{profile}/restart`
«Перезапустить на новой модели» — у **running**-карточек этого профиля в комнате (по `tenant`)
сначала закрепляется выбор комнаты (`model_override`/`provider_override`/`reasoning_effort`;
движок разрешает менять переопределение у идущей карточки — оно применится к следующему
запуску), затем они снимаются с работы (`reassign --reclaim`). Если у профиля **нет** замены
комнаты (`source:"agent"`), перезапускать нечего: ответ `{restarted: 0}` и предупреждение
«В комнате модель этого участника не менялась» — идущую карточку не снимают. Дальше карточки
подхватывает диспетчер.
По умолчанию не вызывается — это явное действие Михаила.
`200` → `{ok: true, restarted: int, cards: [id], warnings: [string]}`; `restarted: 0` — идущих
карточек нет (не ошибка).

#### `GET /api/rooms/{id}/models/sent` — «что реально ушло» (W13-A)
```
{room, generated_at,
 members: [{profile, role,
            model_settings: {provider, model, reasoning, source, updated_at},
            model_sent: {…}|{}}],   // факт последнего запуска участника в комнате
 cards:   [{id, assignee, status, model_label, model_sent}]}  // только подтверждённые/расходящиеся
```
`model_sent` — та же форма, что у узла дерева (§6.11 «RequestDetail»): `provider/model/reasoning`,
готовая подпись `text`, `confirmed`, `expected`, `pinned`, `mismatch`, `note`, `cost_usd` (`number|null` — стоимость запуска; `null`, если подтверждённой сессии с ценой нет). Факт берётся из
сессии воркера профиля (`state.db`, `mode=ro`); нет подтверждения — `{}` у участника и запись
карточки в `model_sent` с `confirmed:false`. У участника (вкладка «Команда») дополнительно
`pending`, `pending_text`, `changed_at`, `run_at`, `queue` — расхождение только после смены модели, см. §6.4.

#### `POST /api/rooms/{id}/models/check` — сверить и вернуть «не ту» карточку
```
{fix?: bool}          // по умолчанию true — останавливать и возвращать
`200` → {ok, room, fix, checked, confirmed, mismatched: [{id, assignee, expected, actual, run_id}],
       pending: [{id, assignee, expected, actual, run_id, since}],
       fixed: [id], failed: [{id, reason}], unknown}
```
`pending` — карточки, которые стартовали ДО смены модели: их работа не прерывается, новая модель
применится со следующей задачи (W19-T, B-26); в `mismatched` они не попадают и `reassign --reclaim`
для них не выполняется.
Та же проверка, что сторож делает каждый тик: у идущих карточек (`running/review`) модель
реального запуска сравнивается с выбором комнаты. Расхождение пишется в журнал комнаты
(`kind: "model_changed"`, «Карточка … ушла на …, а в комнате выбрано …»); при `fix:true` карточка
закрепляется на выборе комнаты и снимается с работы штатным `reassign --reclaim`
(`kind: "model_restarted"`, «остановлена и возвращена на выбор комнаты»), диспетчер запускает её
заново. Защита от петли: не больше 2 перехватов на карточку (`data/state/<комната>/model_fixups.json`),
дальше только предупреждение в журнале. Карточки, которым меньше 10 с от старта, не снимаются
(воркер ещё поднимается), но расхождение фиксируется. Участникам без замены комнаты проверка
ничего не делает.

#### `RequestDetail` (страница запроса)
У узлов `tree[]` необязательное `model_label: "Anthropic · claude-sonnet-5-5 · высокий"` —
«провайдер · модель · мышление» **по факту карточки** (`null`, если у карточки нет переопределения).
Показывается мелко на странице запроса; в тексты для Михаила технические id не попадают.

У узла есть и необязательное `model_sent` — **что реально ушло** (W13-A):

```
model_sent = {
  provider, model, reasoning,          // фактически применённые значения
  label:    "ollama-cloud · glm-5.3-flash",         // как в model_label (id из карточки/каталога)
  text:     "ушло на: ollama-cloud · glm-5.3-flash",// готовая подпись для интерфейса
  confirmed: bool,      // true — модель подтверждена из запуска (сессия воркера), false — из карточки
  run_id:   int|null, at: int|null,
  cost_usd: number|null,                            // стоимость запуска (оценка сессии)
  expected: {provider, model, reasoning},           // выбор комнаты (или настройка агента)
  pinned:   {provider, model, reasoning},           // что записано в карточке
  mismatch: bool,                                   // расходится с выбором комнаты
  note:     "расходится с выбором комнаты" | "",
} | null
```

Источник факта — сессия воркера: у прогона карточки (`task_runs.metadata.worker_session_id`;
для старых записей — событие `owner_admitted`) из `state.db` профиля читается
`sessions.model` и `sessions.billing_provider` (`sqlite mode=ro`). Движок выбирает модель в
момент допуска запуска (`gateway/session_kanban.py`), поэтому это единственное надёжное место,
где видно фактическую модель; лог воркера — свободный текст, `task_runs.metadata` хранит только
id сессии. Если подтверждения нет, `model_sent` показывает запись карточки с `confirmed:false`
(«запуск ещё не подтверждён»), а не выдуманную модель.

## W20-WORKLOG — «Ход работы» (v1.1.0, WAVE20 §1.6)

Источник: `docs/WAVE18-RULES.md` пакет W. Поток событий «что сейчас делают агенты»:
рассуждения, вызовы инструментов, результаты, сообщения, события карточек и промежуточный
итог сторожа («дайджест»). Всё читается **только на чтение** (`sqlite mode=ro`, WAL) из
`state.db` профилей и `kanban.db` доски; приложение ничего не пишет в чужие БД, моделей не
запускает (0 токенов). Режим `off` полностью отключает чтение.

### W20.1 Настройки комнаты

`PATCH /api/rooms/{id}` и `Room` (`§3.2`) получают два поля:

| поле | значения | по умолчанию | смысл |
|---|---|---|---|
| `worklog_mode` | `full` \| `brief` \| `off` | `full` | «Подробно (с рассуждениями)» / «Кратко (только действия)» / «Выключено» |
| `digest_every_min` | `0` \| `1` \| `2` \| `5` | `2` | промежуточный итог сторожа раз в N минут; `0` — выключено |

Неизвестное значение → `400 bad_request` с `detail.allowed`. У старых комнат полей в
`rooms.json` нет — читаются как `full` и `2` (миграция чтением, файл не переписывается).
Строка режима для интерфейса приходит в `Room.worklog` (`{mode, mode_label, digest_every_min,
digest_label, on}`), чтобы клиент не собирал подписи сам.

### W20.2 `GET /api/requests/{id}/worklog`

| параметр | значения | по умолчанию |
|---|---|---|
| `since` | курсор из предыдущего ответа (непрозрачная строка) | пусто — последние `limit` записей |
| `before` | курсор: «показать раньше» — записи строго старше него | пусто |
| `level` | `thoughts` \| `steps` | из настройки комнаты (`full`→`thoughts`, `brief`→`steps`) |
| `limit` | 1…200 | `50` |

```
200 → {
  request: "t_1a2b3c4d",
  room: "cafe",
  mode: "full"|"brief"|"off",          // действующая настройка комнаты
  level: "thoughts"|"steps",           // фактический уровень (brief всегда steps)
  items: [Item],                        // по возрастанию ts
  cursor: "w1.…",                       // курсор ПОСЛЕ последней записи ответа
  before: "w1.…"|"",                    // курсор «показать раньше» (пусто — история кончилась)
  more: bool,                           // есть ещё более новые/старые записи за limit
  live: [Live],                         // кто работает прямо сейчас
  digest: Digest|null,                  // последний промежуточный итог (если был)
  stuck: [Stuck],                       // карточки, похожие на зависшие
  generated_at: int
}
```

`Item`:

```
{id: "m_12f3…",            // устойчивый ключ (для data-key фронтенда)
 ts: 1762749600,            // секунды, UTC
 card_id: "t_1a2b3c4d"|null,
 profile: "lawyer",
 label: "Юрист · Анна",      // подпись участника из 1.0.3 (пусто — профиль)
 role: "orchestrator"|"critic"|"worker"|"observer"|"",
 kind: "thought"|"action"|"result"|"message"|"status"|"handoff"|"digest",
 text: "думает: проверяет, все ли пункты договора учтены…",
 tool: "read_file"|"",
 ok: bool|null}             // у result: false — инструмент вернул ошибку
```

Смысл `kind`: `thought` — рассуждение (только `level=thoughts`); `action` — вызов
инструмента; `result` — короткий итог вызова; `message` — реплика агента в сеансе;
`status` — событие карточки («принял карточку», «готово»); `handoff` — передача работы
(«Руководитель → Юрист: …»); `digest` — промежуточный итог сторожа.

`Live`:

```
{profile, label, card_id, state: "working"|"waiting"|"idle"|"stuck",
 since: int|null,            // когда началось текущее действие
 last_action: "ищет в договорах",
 tools: int, messages: int,  // счётчики за сеанс
 stuck: bool, stuck_text: ""}
```

`Digest`:

```
{id, ts, text, minutes, workers: [{profile, label, last_action, waiting: bool}],
 done: int, total: int}
```

`Stuck`: `{card_id, profile, label, minutes, text}` — `text` = «возможно завис: нет
активности 18 мин. Проверьте доску».

Поведение:

* `off` — `{mode:"off", items: [], live: [], digest: null, cursor: ""}` и **никакого**
  чтения БД (проверяется тестом: файл `state.db` с битым содержимым не открывается);
* `brief` — те же действия/статусы/передачи/дайджест, без `thought` и без `message`
  (тексты реплик скрываются: «действует»); `level=thoughts` при `brief` не действует —
  ответ честно возвращает `level:"steps"`;
* курсор: `items` после `since` не повторяются и не теряются (сравнение по
  `(ts, session, row_id × номер события)`, не по времени — записи в одну секунду и несколько
  событий одной строки не склеиваются); `more:true` — есть ещё; `before` отдаёт предыдущую
  страницу, не сдвигая курсор «что нового»;
* **страницы «раньше» (W21-L1).** Первый ответ (без `since`) — последние `limit` записей: лента
  открывается на свежем. `before` в ответе — курсор следующей страницы «раньше» (ключ самой старой
  отданной записи + отметки строк по сеансам); пусто — история кончилась. Каждая следующая страница
  строго старше предыдущей, повторов и пропусков нет. Сервер читает строки сеанса НАЗАД от отметки,
  поэтому история доступна и у сеанса длиннее окна `limit`;
* **мусор в `before` (W21-REV2).** Непустая строка, которая не является нашим курсором (обрезанный
  адрес, `before=abc`, строка длиннее 4096 знаков), означает «страницы нет»: ответ — как без
  параметра (свежие записи, `more:true`, новый `before`). Раньше такой запрос молча возвращал
  ПУСТУЮ страницу с `more:false`, и «Показать раньше» переставало работать до перезагрузки;
* нет `state.db`/`kanban.db`/сеанса — `{items: [], live: [], warnings: ["…"]}` и `200`,
  никогда `500`; отсутствие сессии не ошибка (карточка ещё не стартовала);
* объём: не больше `limit` записей, но не более 200; одно рассуждение — до 300 знаков
  (дальше «…ещё»), результат инструмента — до 200 знаков, без дампов и base64;
* маскирование: единая `agent_notes.mask_secrets()` на КАЖДЫЙ `text` (ключи `sk-…`,
  `ghp_…`, `password=…`, `Bearer …`) + фильтр base64/длинных hex; пути укорачиваются
  (`…\Отчёты\свод.md`);
* записи `compacted`/`observed` не показываются;
* ответ отдаётся с `Cache-Control: no-store`; опрос — 3 с во время работы, 30 с после
  (решает фронтенд пакета FB).

### W20.3 Сторож-дайджест (без модели)

Пока запрос в работе (`queued/working/reviewing/synthesizing`), сторож (`server/watcher.py`)
раз в `digest_every_min` минут кладёт в `data/state/<комната>/worklog_digest.json` запись
`kind:"digest"` на запрос (по `root_id`): «Прошло 6 мин. Работают: Юрист — читал 3 файла,
сейчас ищет в договорах; Сметчик — ждёт вашего ответа. Готово 2 из 5. Ничего нового с 17:40.»
Формируется из событий и счётчиков (вызовы инструментов, последнее действие, статусы
карточек), не чаще раза в `digest_every_min` минут, не дублируется, после завершения
запроса прекращается и в карточки агентам не уходит. Дайджест приходит в `items` (`kind`
`digest`) и в поле `digest`.

Частоту задаёт **только** `digest_every_min` (0 — выключить совсем): настройка
`worklog_mode=off` гасит экран и чтение через API, но промежуточный итог в ленте остаётся —
так и просили: «сторож работает и при выключенных рассуждениях».

**Обнаружение зависания:** карточка в `running`/`review` без активности (heartbeat,
сообщения сеанса) дольше `stuck_after_min` (по умолчанию 15 мин) попадает в `stuck[]` и в
`live[]` с `state:"stuck"`. Это только пометка, приложение ничего не останавливает.

### W20.4 Ошибки

`404 not_found` — запроса нет в реестре комнаты. `400 bad_request` — `limit` вне 1…200,
`level` не из списка. `503 unavailable` — реестр комнат занят. Отсутствие БД **не** ошибка.

## W20-MODELS — модель участника по умолчанию из настроек агента (v1.1.0, WAVE20 §1.5)

Раньше «по умолчанию» означало «пусто, разберись сам»: интерфейс показывал «как у агента», а в
реестре комнат оставались записи, которых никто не выбирал. Теперь правило одно и то же у сервера,
интерфейса и скрипта сброса.

### W20.1 Что считается «по умолчанию»
* `profiles/<профиль>/config.yaml` → `model.provider`, `model.default` и `agent.reasoning_effort`.
  Для профиля `default` — общий `config.yaml` в корне Hermes.
* Если в профиле **нет пары** провайдер+модель — берётся **общая** настройка Hermes (корневой
  `config.yaml`) **целой парой**, с пометкой «из общих настроек Hermes». Смешивать провайдера
  одного источника с моделью другого нельзя: так на карточку попадала чужая модель.
* Если модели нет нигде — пустые значения, `source: "none"`. Приложение ничего не придумывает и
  не подставляет «похожую» модель.
* `delegation.reasoning_effort` «по умолчанию» не считается: это уровень делегированных детей.
* Файлы профилей (`config.yaml`, `SOUL.md`, `skills`) только читаются — ни один путь
  приложения не пишет в них.

`GET /api/models` → `agent_defaults[профиль] = {provider, model, reasoning,
source: "profile"|"global"|"none", note, label}`. `?room=<id>` добавляет участников комнаты,
даже если файла профиля нет (`source: "none"`). В карточке комнаты то же значение — полем
`agent_default_info` (`agent_default` остаётся прежней формой из трёх полей), а в
`model_settings` — `default_source`/`default_note`.

### W20.2 Переопределение комнаты — только явный выбор
* `rooms.agent_models` создаётся **только** кнопкой выбора модели у участника
  (`PUT /api/rooms/{id}/agents/{profile}/model`) или явным `agent_models` в `POST`/`PATCH`
  комнаты.
* `POST /api/rooms` без `agent_models` → у комнаты нет ни одной записи. Если клиент пришлёт
  «эхо» текущих настроек агента (ровно те же провайдер, модель и мышление), запись **не
  сохраняется**: это не выбор пользователя, и она перекрыла бы будущую смену конфига профиля.
* «Вернуть по умолчанию» в интерфейсе отправляет пустой объект — запись удаляется целиком
  (`409`, если комната в архиве).

### W20.3 Тексты интерфейса
* «по умолчанию (из настроек агента): провайдер · модель · мышление» — под каждым участником;
  при `source: "global"` добавляется «— из общих настроек Hermes».
* «изменено для этой комнаты» — метка у участника, у которого запись комнаты есть.
* «Вернуть по умолчанию» удаляет запись; окно настроек комнаты модели не меняет и при
  создании отправляет `agent_models: {}`. Выбор пользователя показывается ровно как выбран —
  без «ближайших» моделей и без предупреждений о цене.

### W20.4 Скрипт сброса `tools/reset_room_models.py`
Для данных, где переопределения остались с прошлых версий:

```
python tools/reset_room_models.py --room <id>            # только показывает
python tools/reset_room_models.py --room <id> --apply    # копия данных + удаление
```

По умолчанию ничего не меняет: печатает по каждому участнику «было … → по умолчанию (из
настроек агента): …». С `--apply` делает копию `data/rooms.json` (`rooms.json.bak-<дата-время>`)
и убирает `agent_models` **только этой** комнаты; другие комнаты и файлы профилей не трогаются.
`--data <папка>` — другой каталог данных, `--json` — машинный вывод. Коды возврата: `0` — готово
(в том числе «менять нечего»), `2` — комната/файл не найдены или файл не читается.



## W20-COMPOSER — диалог по запросу и панель диктовки (v1.1.0, WAVE20 §1.3)

Три дефекта третьего круга (B-39, B-40, B-42) в одном месте: отправка сообщения и
кнопки записи. Контракт интерфейса — один: блок диалога монтируется на странице
ОДИН раз и дальше обновляется данными, а не пересобирается.

### W20.1 `POST /api/requests/{id}/messages` — поля и идемпотентность

Тело: `{text, attach?: [пути], urgent?: bool, client_id?: str}`.

* `client_id` — ключ одного нажатия (6…120 знаков, без NUL). Клиент присылает его на
  каждую отправку (`msg-<время>-<случайное>`); при ошибке связи повтор идёт с ТЕМ ЖЕ
  ключом.
* Повтор в окне **120 с** не создаёт второе сообщение. Дублем считается: тот же
  `client_id` **или** тот же текст от того же автора с теми же вложениями (пробелы по
  краям и сдвоенные пробелы не различаются). В ответ — тело ПЕРВОГО ответа и
  `duplicate: true`.
* Ответы: `201` — сообщение создано (`duplicate: false`), `200` — повтор
  (`duplicate: true`, `message.id` прежний, карточка и лента не меняются), `400` —
  пустой текст, `client_id` не строка/пустой/длиннее 120 знаков, `attach` не список,
  `urgent` не bool; `403` — вложение вне разрешённых путей; `409` — комната в архиве;
  `502` — сообщение записано, но ответ пуст.
* Поля ответа: `message` (`ThreadItem`), `thread[]`, `routed_to`, `merged`,
  `duplicate`, `client_id` (эхо ключа, если был), при наличии — `task_id`, `status`,
  `revision`, `retry`. Заголовок `Location: /api/requests/{id}`.

### W20.2 «Накопленные ответы» — `POST …/actions {action:"answers_resend"}`

Второе нажатие в окне 120 с вторую карточку не создаёт: ответ `200` с прежним телом и
`duplicate: true` (`sent`, `question_ids`, `task_id`, `at`, `request`). Ключ — состав
отправляемого (вопросы и тексты ответов), поэтому новый ответ снова отправляется;
`409 nothing_to_resend` — если отправлять нечего.

### W20.3 `mountThread(container, ctx, {roomId, detail, rid, onSent})`

Возвращает `{element, update(detail), focus(), destroy()}`.

* Страница запроса монтирует блок ОДИН раз за визит и дальше зовёт `update(detail)`:
  `update` пересобирает ленту (по подписи: id, время, длина текста) и, если сменился
  статус, переключатели под полем. Поле ввода, кнопка «Отправить» и фокус остаются
  ТЕМИ ЖЕ узлами — их не заменяют.
* Место блока на странице помечено `data-keep-tree`, поле — `data-keep="thread-draft"`:
  точечная правка страницы (`live-patch.js`) такое поддерево не заменяет и внутрь не
  заходит. Это и есть исправление B-40: живая кнопка и живое поле всегда из одной
  сборки.
* Обработчик отправки берёт текст из ВИДИМОГО поля блока (а не из замыкания монтажа) —
  защита на случай, если блок всё-таки окажется не в странице.
* «Отправить» неактивна, пока поле пустое (пробелы не считаются текстом), и во время
  отправки: подпись меняется на «Отправляется…», `aria-busy="true"`. Второй клик во
  время отправки ничего не делает.
* Ответ с `duplicate: true` показывает сообщение из ответа и подпись «Это сообщение уже
  отправлено — второго не будет»; новых строк в ленте не появляется.

### W20.4 Панель диктовки — `createPanel(...).update(state)`

Кнопки («Стоп», «Отмена», «Распознать ещё раз», «Закрыть») создаются ОДИН раз на смену
набора, а не на каждое обновление: набор задаётся фазой записи и доступностью повтора
(`canRetry`, `retryLabel`). Таймер записи (250 мс) меняет только текст: время, подсказку,
прогресс распознавания. Поэтому клик, у которого нажатие и отпускание попали в разные
обновления, доходит до обработчика (B-42).

### W20.5 Проверки в репозитории

```
python tools/probe_composer_rerender.py --port 8857   # 0 потерь отправок (Chromium + мок)
python tools/probe_click_loss.py --port 8858          # 0 потерянных кликов (Chromium + мок)
node --test tests/composer-send-once.test.mjs         # N отправок с перерисовками
node --test tests/mic-panel-actions.test.mjs          # кнопки панели не пересоздаются
python -m unittest tests.test_composer_idempotency    # 120 с, duplicate, ответы
```

Оба браузерных скрипта возвращают `0` только при нулевых потерях; `probe_click_loss.py`
дополнительно проверяет чувствительность метода — на старой схеме (кнопка
пересоздаётся каждые 250 мс) клики обязаны теряться.



## 7. Мок-сервер и фикстуры

```
python tools/mock_server.py                 # http://127.0.0.1:8778, статика web/ + fixtures/
python tools/mock_server.py --port 8799     # другой порт
python tests/test_mock_server.py            # unittest, сам поднимает свободный порт
```

* `GET` отдаёт фикстуры по таблице путей; `POST` меняет состояние в памяти (создание запроса, действия, бэклог, привязка кандидатов).
* Сверка фикстур со схемами §3–5: `python tools/check_fixtures.py` (обязательные поля, типы, enum, `^t_[0-9a-f]{8}$`, формула `progress.percent`, `duration_s`, целостность `tree`, кросс-ссылки лент и деталей). Exit 0 — всё сходится; exit 1 — печатает `файл: поле`.
* Код разложен на модули: `tools/mock_server.py` (HTTP, CLI), `mock_app.py` (состояние и сборка), `mock_read.py` (GET), `mock_write.py` (POST/PATCH/DELETE), `mock_versions.py` (расписание фаз и форма v2), `mock_core.py` (константы и помощники).
* Мок повторяет контракт и по «мелочам»: тело >2 MiB → `413`, неверный метод на известном пути → `405` + `Allow`, `limit` у журнала вне 1..200 → `400`, валидация типов тел → `400` с `detail.field`, `/api/file` по тем же 5 корням + расширениям + чёрному списку имён, SVG с `Content-Security-Policy: sandbox`, office — `attachment`. Дополнительно мок закрывает собственные исходники (`server/`, `tools/`, `tests/`, `reportkit/`, `fixtures/`) — это отличие мока, а не контракта: `.py/.js/.css` всё равно отсекаются списком расширений, а фикстуры в реальном сервере лежат внутри `C:\TeamRoom\Team`.
* Новый запрос с критиком: `queued → working (4 c) → reviewing (12 c, критик) → synthesizing (20 c, доработка) → done (35 c)` — две версии ответа. Без критика: `queued → working (4 c) → synthesizing (22 c) → done (30 c)` — одна версия. `GET /api/requests/{rid}` собирает ответ из фикстур с растущим `progress`.
* Действия меняют состояние живого запроса: `decompose` → `working`, `critique` → `reviewing` (раунд +1), `revise` → `synthesizing` (новая версия). У статичных фикстур те же действия правят ответ в памяти, но дальнейшей эволюции нет.
* `GET /api/rooms/{id}/candidates` отдаёт `fixtures/candidates_cafe.json` (2 карточки); `POST /api/adopt` привязывает кандидата и убирает его из списка (вторая карточка — с чужой меткой `room:fn`, `adopt` в `cafe` вернёт `409`).
* Комнат две: `cafe` (команда исполнителей, критик `team-critic`) и `fn` (`default`, lawyer/coder/surveyor, критика нет) — `/api/rooms/fn/...` работает полностью.
* Сервер перезапускается без потери файлов — состояние только в памяти.
* Проверки `Host`/`Origin` — как в §2 (те же коды и статусы), чтобы фронтенд сразу видел реальные 403.
* Фикстуры — эталон форм; менять их можно только вместе с этим документом.
* Флаг `needs_you` мок считает по тому же правилу, что бэкенд (`mock_core.compute_needs_you`): статус `needs_you` **или** неотвеченный вопрос Михаилу **или** blocked-карточка с `needs_human`. Поэтому `request_done.json` и `request_done_legacy.json` (статус `done`, открытые вопросы Михаилу) несут `needs_you:true`, а `counters.needs_you` комнаты `cafe` в фикстурах равен 3 (запрос `needs_you` + два `done` с вопросами). `tools/check_fixtures.py` проверяет оба инварианта. `answer` в моке пересчитывает флаг: пока остались открытые вопросы Михаилу, он держится.
* `POST …/actions comment` в моке отдаёт `{comment: {id, author, text, at}}` (`id` — растущий int), как бэкенд.
* Мок отдаёт и «лишние» ключи (`backlog.sections`, `progress.running`, `team[].load`, `current.since`, `last.at`, `warnings[].detail`, `roadmap.phases[].source_at`): контракт расширяемый, клиент читает только поля, описанные в §3.

## 8. Экраны → эндпоинты

| Экран | Запросы |
|---|---|
| Левая колонка | `GET /api/rooms`, `GET /api/health` |
| Шапка комнаты | `GET /api/rooms/{id}`, `GET /api/rooms/{id}/team` |
| Запрос (лента + поле) | `GET/POST /api/rooms/{id}/requests`, `GET /api/requests/{rid}` |
| Страница запроса | `GET /api/requests/{rid}`, `POST …/actions` (`decompose`, `revise`, `critique`, `reassign`), `GET /api/reports/{room}/{key}` |
| Кандидаты на привязку | `GET /api/rooms/{id}/candidates`, `POST /api/adopt` |
| Команда | `GET /api/rooms/{id}/team`, `GET /api/profiles`, `POST/DELETE …/members` |
| Карта проекта | `GET /api/rooms/{id}/roadmap` |
| Дальше | `GET /api/rooms/{id}/backlog`, `POST …/backlog/{item_id}`, `…/ask`, `GET /api/ics` |
| Отчёты | `GET /api/rooms/{id}/reports`, `GET /api/reports/{room}/{key}` |
| Журнал | `GET /api/rooms/{id}/journal` |
| Палитра / поиск | `GET /api/search`, `POST /api/rooms/{id}/requests` |
| Настройки | `GET /api/config`, `GET /api/health`, `PATCH /api/rooms/{id}` |

## W20-ARTIFACTS — блок «Файлы» запроса (v1.1.0, WAVE20 §1.4; W26-D3 — переименован из «Артефактов»)

Источник: B-43. Раньше блок собирался только из `metadata.artifacts` карточек: в настоящем
запросе это 4 файла из 15, порядок — по порядку карточек, без даты и без «свежее свода».

### W20.1 Что попадает в `artifacts[]`

| источник | как ищется |
|---|---|
| метаданные карточек | `metadata.artifacts` карточек дерева — как раньше |
| папка материалов запроса | папка проекта комнаты и её зеркало: подпапки, в названии которых есть id запроса, плюс папка, где уже лежат файлы из метаданных карточек |
| содержимое папки материалов | файлы разрешённых типов на глубине ≤ 2 (`ARTIFACT_DEPTH`), включая подпапки |

Ограничения: только внутри `config.room_roots` (общие корни + папка проекта комнаты + зеркало),
не больше 200 элементов (`ARTIFACT_LIMIT`); запрещённые имена и типы отбрасываются; заметки
запросов в зеркале (`Запросы/`) материалами не считаются; файлы с чужим id запроса в имени тоже.

### W20.2 Поля и порядок

* `mtime` — время изменения файла (секунды), `null` если файла нет; `size` — байты.
* `group`: `final` — итоговые (в имени `итог|письмо|кратко|final|summary|letter`), `material` —
  рабочие файлы, `service` — служебные `_*.json`, `_*.html`, `_*.log`. Явная `group` из
  метаданных карточки важнее имени файла.
* `folder` — относительная подпапка внутри папки материалов (`""` — файл лежит прямо в ней).
* `newer_than_svod` — файл изменён позже последней версии свода (`synthesis.at`).
* Порядок: `final` сначала, затем остальные по `mtime` от новых к старым (внутри группы — так же).

### W20.3 Подпапки

Все файлы одной подпапки возвращаются одним элементом:

```
{kind: "folder", title: "_ред2-копии", folder: "_ред2-копии", count: 2,
 path: "<абсолютный путь подпапки>", mtime: int, group: "final"|"material"|"service",
 newer_than_svod: bool, exists: true, size: null, state: "ok", items: [Artifact, …]}
```

Папка стоит там, где стоял её самый свежий файл; `group` папки — группа этого файла.
Интерфейс рисует её свёрнутой группой (`<details>` со счётчиком), раскрытие запоминается
на время сеанса.

### W20.4 Ссылки внутри свода

Markdown-ссылки на локальные файлы в тексте свода (`[письмо](C:/…/Письмо.docx)`) открываются
в контексте комнаты, как кнопки блока файлов: «Просмотр» — `GET /api/file?path=…&room=…`,
«Открыть»/«Показать в папке» — `POST /api/files/launch {path, room, mode}` (W26-D3;
прежний адрес — `/api/artifacts/open`). Раньше клиент открывал
`/api/file?path=…` без `room`, и сервер отвечал `403` на файлы папки проекта комнаты.

## W20-PHASES — этапы проекта предлагает руководитель (v1.1.0, WAVE20 §1.7, F-2)

Раздел описывает серверную часть пункта 20 (этапы проекта предлагаются автоматически).
Интерфейс плашки и редактора — пакет FC (`docs/API.md` здесь только про данные).

### W20.1 Поля комнаты

| Поле | Тип | Что это |
|---|---|---|
| `phases` | `[{id, title}]` | этапы комнаты — как раньше (W17-R §1). Правит **только человек** в настройках комнаты: добавить, переименовать, переставить, удалить. `PATCH /api/rooms/{id} {phases}` |
| `phases_proposal` | `{at, by, state, items, signature, source, decided_at?, accepted_ids?}` \| `null` | предложение этапов от руководителя (см. W20.2). **Не** редактируется через `PATCH` комнаты: его меняют маршруты `phases/*` и приём `metadata` |
| `phases_proposal_text` | `str` | короткая подпись состояния для сводки: «Руководитель предлагает 4 этапа: Идея, Запуск, Проверка, Продажи», «Этапы приняты (4 этапа)», «Предложение этапов отклонено (3 этапа)». Пустая строка — предложения нет |

`state` — `proposed` (ждёт решения), `accepted` (принято), `rejected` (отклонено).
`items` — `[{id, title, description}]` в том порядке, в каком предложил руководитель
(`id` выводится из названия, если руководитель его не прислал; `description` — одна строка).
`signature` — отпечаток списка (id + название): по нему приложение понимает, что пришло
то же самое предложение. `source` — `root_id` карточки, из которой пришло предложение,
`by` — профиль, кто предложил. `decided_at` и `accepted_ids` появляются после решения.

Где видно:

* карточка комнаты (`GET /api/rooms/{id}`) и `GET /api/rooms/summary` — оба поля;
* карта проекта (`GET /api/rooms/{id}/roadmap`) — `phases_proposal` и `proposal_text`;
  `phases[]` берутся из самой комнаты, поэтому принятые этапы видны **сразу** после решения;
* «От вас нужно» (`GET /api/rooms/{id}/backlog`) — `proposal_item` (та же строка, что в `items[0]`,
  пока предложение ждёт решения) с подсказкой «что будет, если принять»; после решения —
  `null`, строки в `items` нет.

### W20.2 Как приходит предложение (`metadata.phases_proposal`)

Руководитель пишет в metadata **любой** своей карточки:

```json
"phases_proposal": [
  {"title": "Идея и вывеска", "description": "что проверяем и каким способом"},
  {"title": "Проверка спроса", "description": "мини-опрос у стойки"}
]
```

Приложение (`ingest.apply_phases_from_detail`) кладёт это в комнату с `state="proposed"`
и **не трогает** `phases`:

1. предложение совпало с текущими этапами комнаты → ничего не сохраняется (`same_as_phases`):
   показывать нечего, этапы уже такие;
2. то же предложение уже лежит (`signature` совпал) → ничего не меняется: повторное чтение
   карточки **не сбрасывает** решение человека, в том числе «отклонено»;
3. иначе — новое предложение с `state="proposed"`, `at` (время карточки), `by`, `source`;
   в журнал комнаты пишется событие `phases_proposed`.

Приём устойчив к мусору: пустые заголовки, дубли и лишние пункты отбрасываются с
предупреждениями в `warnings` результата ingest, а не ошибкой приёма ответа. Больше 24
этапов не принимается (как и в `PATCH phases`).

### W20.3 `POST /api/rooms/{id}/phases/proposal`

Тело: `{action: "accept" | "accept_selected" | "reject", ids?: ["<id этапа>", …]}`.

| action | Что делает |
|---|---|
| `accept` | дописывает **все** этапы предложения в конец `phases` |
| `accept_selected` | дописывает только перечисленные `ids` (обязательны; в порядке предложения) |
| `reject` | `phases` не меняются, предложение получает `state="rejected"` |

Правила, которые нельзя нарушать:

* существующие этапы человека не переименовываются и не переставляются: новый этап ставится
  **в конец**; совпавший по `id` **или по названию** (без учёта регистра, ё/е, знаков
  препинания и продолжения названия целым словом — «Проверка спроса» ↔ «Проверка спроса, цен
  и меню») пропускается, название человека остаётся;
* оба поля (`phases` и `phases_proposal`) меняются одним движением под замком реестра
  (`rooms.set_phases_state`), поэтому параллельная правка этапов не теряется;
* если человек сам собрал в редакторе ровно предложенные этапы, предложение автоматически
  становится `accepted` (`auto=true` в записи): плашка не висит зря.

Ответ `200`:

```json
{"ok": true, "action": "accept", "changed": true, "state": "accepted",
 "phases": [{"id": "ideya", "title": "Идея и вывеска"}],
 "proposal": {"at": 1770000000, "by": "team-director", "state": "accepted", "items": […],
              "decided_at": 1770000500, "accepted_ids": ["ideya"], "signature": "…", "source": "t_…"},
 "added": ["ideya"], "skipped": [], "text": "Этапы приняты (1 этап)",
 "room": {…карточка комнаты…}}
```

Ошибки:

| Код | `detail.reason` | Когда |
|---|---|---|
| `400` | `field: "action"` | действия нет в списке |
| `400` | `field: "ids"` | `accept_selected` без `ids`, `ids` не список строк или в предложении нет таких этапов (`detail.unknown`) |
| `409` | `no_proposal` | предложения нет вовсе |
| `409` | `already_decided` | предложение уже принято/отклонено, а просят противоположное (повтор **того же** действия — `200` с `changed:false`) |
| `409` | `archived` | комната в архиве |

### W20.4 `POST /api/rooms/{id}/phases/propose`

Тело: `{note?: "<что учесть>"}` (≤ 2000 знаков). Ставит руководителю карточку
«Какие этапы у проекта»: в ней просят только список этапов в формате `metadata.phases_proposal`
и прямо запрещают начинать работу по проекту. В контекст карточки попадают уже заданные этапы
комнаты и правило «эти этапы человек правил — не переписывай».

Ответ `201`: `{ok, action:"phases_propose", root_id, title, request, warning}`.
`409` с `reason:"proposal_pending"` — пока предыдущее предложение ждёт решения (и `400` на
пояснение не строкой или длиннее 2000 знаков).

### W20.5 Журнал и наблюдатель

* `phases_proposed` — «Руководитель предложил этапы проекта: …» (при приёме metadata);
* `phases_decided` — «Этапы проекта приняты: …» / «… приняты выбранные: …» / «Предложение
  этапов отклонено: …» (при решении человека).
* Наблюдатель ничего не запускает: карточку «предложи этапы» создаёт только кнопка, а модели
  стартует обычный диспетчер, как у любого запроса.

### W20.6 Интерфейс (пакет FC)

Серверных изменений нет: интерфейс пользуется теми же маршрутами.

| Действие в интерфейсе | Запрос |
|---|---|
| «Принять все» / «Принять выбранные» / «Отклонить» на плашке карты и в строке «От вас нужно» | `POST /api/rooms/{id}/phases/proposal {action, ids?}` |
| «Предложить этапы» (карта, пустое состояние) | `POST /api/rooms/{id}/phases/propose {note?}`; `409 proposal_pending` — подсказка «сначала примите или отклоните», второй карточки нет |
| «Сохранить этапы» в окне редактора и «Сохранить» в настройках комнаты (вкладка «Проект») | `PATCH /api/rooms/{id} {phases}` — этапы остаются свойством комнаты |
| Открыть предложение и список с описаниями | `GET /api/rooms/{id}/roadmap` (`phases`, `phases_proposal`, `proposal_text`) |

Редактор (`web/js/views/phases-editor.js`) — один на приложение: окно «Этапы проекта» с карты,
встроенный блок на вкладке «Проект» настроек комнаты и строка предложения в «От вас нужно» берут из
него и плашку, и список, и решения. «Изменить» на плашке открывает тот же редактор с блоком
предложенных этапов («Добавить выбранные / Добавить все», дубли по названию пропускаются).
Если человек собрал в редакторе ровно предложенные этапы, сервер сам закрывает предложение
(`reconcile`) — плашка не висит зря. Демо-мок ведёт себя так же: `tools/mock_write.py`
принимает `phases` в `PATCH` и пересчитывает предложение, а карта в `tools/mock_read.py`
берёт этапы комнаты (правки, порядок и удаления видны сразу).

## W20-DECQ — решения и вопросы: снятие, «устарело», вопросы из сообщений (v1.1.0, WAVE20 §1.2, B-37/B-38/B-41)

Пакет D1. Описывает:
статусы решения `dismissed`/`superseded`, действия `decision dismiss|restore` и `question_dismiss`,
регистрацию вопросов руководителя из текста комментариев, метку `asks` у сообщения в ленте,
состояние ответа `replied_text` и счётчики «Нужен ваш ответ» с новыми статусами.

### W20-DECQ.1 Статусы решения

| Статус | Подпись (`status_label`) | `open` | `needs_answer` | Откуда |
|---|---|---|---|---|
| `proposed` | `предложено` | `true` | `true` | `metadata.decisions[].status` |
| `accepted` | `принято вами` / `принято руководителем` | `false` | `false` | действие `accept` |
| `rejected` | `отклонено вами` / `отклонено руководителем` | `false` | `false` | действие `reject` |
| `discuss` | `обсуждаем с руководителем` | `true` | `true` | действие `discuss` |
| `dismissed` | `снято — не актуально` | `false` | `false` | действие `dismiss` (B-37) |
| `superseded` | `устарело` | `false` | `false` | `metadata.decisions_retracted` или отсутствие в новой версии свода (B-37) |

Элемент `decisions[]` в `GET /api/requests/{id}` дополнительно несёт:

* `status_muted: bool` — показывать серым («снято»/«устарело»);
* `previous_status` — прежний статус, если он сменился этим действием;
* `can_dismiss`, `can_restore` — какие кнопки показывать;
* `dismissed_at` — когда сняли;
* `superseded`, `superseded_by` (версия свода), `superseded_at`, `superseded_why`
  (`retracted` — руководитель отозвал, `absent` — решения нет в новой версии свода);
* `needs_answer: bool` — ждёт ли решение слова владельца (единственный признак для «Нужен ваш ответ» и счётчиков);
* `muted_actions: [{action, label, hint, available}]` — «Снять — неактуально» / «Вернуть».
  Три привычных действия остаются в `actions[]` без изменений (обратная совместимость);
* `carried: true`, `from_version: N` (W21-S5) — решение пришло из прежней версии: действующая
  карточка руководителя его не повторяла, но список без `svod.decisions_full` — дополнение,
  поэтому решение продолжает действовать.

`status_source`: `user` — действие владельца, `leader` — руководитель закрыл/отозвал решение сам.

Счётчики: `answers`/`decisions` в детали и бэклоге используют `needs_answer`; снятые и устаревшие
решения не попадают в «Нужен ваш ответ» и в `counts.open`, но остаются в списке «Решения»
(`decision_summary`: `{total, open, closed, dismissed, superseded, muted}`).

### W20-DECQ.2 Действие `POST /api/requests/{id}/actions {action:"decision", decision:"dismiss"|"restore", decision_id, text?}`

* `dismiss` — «Снять — неактуально»: `status=dismissed`, уходит из «Нужен ваш ответ», в списке
  «Решения» показывается серым с возвратом. `restore` — «Вернуть»: снова `proposed` (или `discuss`,
  если так и было). `accept`/`reject`/`discuss` работают как раньше.
* Ответ `200`: `{ok, action:"decision", decision_id, decision, message, action_label, status,
  status_label, status_hint, open, muted, decisions_summary, request}`.
* `400` — неизвестное `decision` (в `allowed` перечислены все пять), пустой/NUL текст, текст > 2000 знаков;
  `404` — решения с таким `decision_id` нет в запросе.
* Побочные эффекты: запись в реестре ответов комнаты (`answers[d_id]`, `kind="decision"`,
  `action`, `history:[{action, at, text}]`) и строка в ветке
  `[Михаил · решение d_x «текст»] Снять — неактуально: …` / `… Вернуть`. **Карточка руководителю
  не создаётся**: уборка списка не должна будить агента. Решает ПОСЛЕДНЕЕ действие пользователя
  (`history`), поэтому «снял → принял» = `accepted`.

### W20-DECQ.3 Автоматическое «устарело» (`superseded`)

Источник — история версий свода, собираемая при чтении (`answers.decision_versions`,
`answers.superseded_decisions`). Версия карточки берётся из `metadata.svod.version`; если её нет —
порядковый номер карточки с решениями по времени завершения.

* `metadata.decisions_retracted:["d_…"]` в ЛЮБОЙ карточке руководителя → решение устарело с этой версии
  (`why:"retracted"`);
* решение было в версии N и отсутствует в версии M > N, у которой есть непустой `metadata.decisions`
  **и заявлен полный список** (`metadata.svod.decisions_full: true`, см. «W21-SVOD.9») → `why:"absent"`,
  `superseded_by:M`. Без флага список решений карточки — дополнение: прежние решения не гаснут;
* карточка с одним отзывом и без `decisions` НЕ объявляет устаревшими все прежние решения;
* решение, снова попавшее в более позднюю полную версию, перестаёт быть «исчезнувшим» (последнее слово
  руководителя сильнее прежней отметки); явный отзыв (`retracted`) так не снимается;
* устаревшее решение, которого уже нет в действующем списке, добавляется в `decisions[]` отдельным
  элементом со статусом `superseded` (чтобы было видно, что именно устарело).

Данные карточек и реестра не переписываются: всё считается наложением при чтении.

### W20-DECQ.4 Вопросы руководителя из комментариев (B-38)

`GET /api/requests/{id}` → `open_questions[]` пополняется вопросами из ленты запроса:

| Поле | Значение |
|---|---|
| `id` | `cq_<8hex>` — стабильный id (sha1 от текста, id комментария и номера в сообщении; между чтениями не меняется) |
| `source` | `comment` — строка `[Руководитель · вопрос] текст`; `candidate` — пункт списка под заголовком; `card` — из `metadata.open_questions` |
| `comment_id` | id элемента ленты (`c<номер>`), в котором вопрос задан |
| `at` | время сообщения |
| `kind` | `question`; у строк `[Руководитель · решение]` — `decision` (id `cd_<8hex>`) |
| `dismissed` | `true`, если владелец нажал «Это не вопрос» |
| `needs_answer` | `true`, пока вопрос ждёт ответа (снятые и `replied_text` — `false`) |

Метки: `[Руководитель · вопрос]`, `[Руководитель · вопрос к Михаилу]`, `[Руководитель · вопросы]`
(та же семья — `[Руководитель · решение]`/`решения`/`предложение`). Метка читается только в начале
сообщения.

**Кто имеет право «задать вопрос» (W21-L1, уточнение авторства).**

* **кандидаты** (пункты под «Что решить…») — ТОЛЬКО из сообщений руководителя комнаты
  (`room.orchestrator`). Реплика исполнителя или критика со списком «что решить» вопросов не даёт:
  это чужой список задач, адресованный не владельцу;
* **комната без руководителя** (допустимое состояние) — свободный текст вопросов не порождает
  вовсе; свод при этом не обновляется сам (`synthesis.live.reason = "no_leader"`);
* **явная метка** `[Руководитель · вопрос] …` — от ЛЮБОГО автора-агента, в том числе в комнате без
  руководителя: её ставят осознанно, двусмысленности нет;
* `[Руководитель · решение] …` — по-прежнему только от руководителя комнаты (в комнате без
  руководителя — от любого агента).

Кандидаты (`candidate`): нумерованные (`1)`, `1.`) и маркированные (`-`, `–`, `•`, `*`) пункты,
идущие непосредственно под заголовком, который начинается с «Что решить», «Что нужно решить»,
«Нужен ваш ответ», «Вопросы к вам/тебе», «Вопросы». Пустая строка после пунктов, новая проза или
следующий заголовок список заканчивают; пункты длиннее 400 знаков отбрасываются; максимум 20 пунктов
на сообщение. Дубли по нормализованному тексту схлопываются.

У элемента ленты `thread[]` от руководителя с зарегистрированными вопросами появляется
`asks:[id]` — по нему интерфейс рисует «ждёт ваших ответов: N → справа». Сообщения без вопросов
поля не получают; снятые вопросы из `asks` исключаются.

На вопрос из комментария можно ответить обычным действием `{action:"answer", question_id:"cq_…"}`
и напомнить о реакции (`answer_remind`): поиск вопроса идёт сначала по карточкам, затем по ленте.
Ответ на снятый вопрос — `409` (`detail.question_id`).

### W20-DECQ.5 Действие `POST /api/requests/{id}/actions {action:"question_dismiss", question_id}`

«Это не вопрос» — для вопроса из карточки и для кандидата/метки из комментария.

* Ответ `200`: `{ok, action:"question_dismiss", question_id, question, answer_state:"dismissed",
  answer_label:"это не вопрос", dismissed:true, needs_answer:false, request}`;
* `400` — некорректный id; `404` — вопроса нет ни в карточках, ни в ленте;
* запись в реестре ответов: `answers[cq_…] = {kind:"question", dismissed:true, status:"dismissed",
  text:"", at, question_text, source}`; данные запроса не меняются, отметка ставится при чтении;
* следствие: вопрос уходит из «Нужен ваш ответ», из счётчиков и из `asks`, пункт бэклога получает
  `status="dismissed"` и `answer_state="dismissed"`.

### W20-DECQ.6 `answer_state = replied_text` (B-41)

Когда ответ владельца записан, а руководитель ответил **в ленте**, но не по формату (нет метки
`[Руководитель · реакция на ответ …]` и нет `outcome`), вопрос получает:

* `answer_state: "replied_text"`, `answer_label: "руководитель ответил текстом — посмотрите в ленте"`,
  `answer_hint`: пояснение, что ответ есть в диалоге;
* `answered_text_only: true`, `answered: true`, `leader_pending: false`, `needs_answer: false`;
* `reaction: {status:"replied_text", text, at, implicit:true, unparsed:true}` — текст, который написал
  руководитель.

Признаки «ответа по делу» (все — только от руководителя комнаты и только после ответа):
упоминание id вопроса; начало сообщения со слова реакции («Принято», «Учтено», «Согласен», «Понял»,
«Работа пересмотрена», «Ничего менять» и т. п.); перекрытие текста вопроса двумя значимыми основами
слов. Служебные метки (`[Михаил · …]`, `[Руководитель · …]`, `[Руководитель → …]`) реакцией не считаются.
Если исход распознан (`metadata.reaction.outcome`, метка `outcome=…` или `[Руководитель · реакция на
ответ q_x]`) — статус обычный: `received|applied|no_change`, и `replied_text` не появляется.

### W20-DECQ.7 Обратная совместимость

* старые записи реестра без `history`/`dismissed` читаются как раньше (`apply_decision`,
  `state_of`: пустая запись → `open`, `sent` → `sent`);
* `actions[]` решения по-прежнему содержит ровно `accept|reject|discuss` — новые кнопки в
  `muted_actions` и флагах `can_dismiss`/`can_restore`;
* вопросы из карточек получают `source:"card"`; отсутствие `source` трактуется как `card`;
* `replied_text` появляется только там, где реакция была бы `sent`, — вопросы без ответа владельца
  не меняются.

## W20-SVOD — «живой свод»: версия растёт сама (v1.1.0, WAVE20 §1.1, B-36)

Раньше новая версия свода рождалась только от карточки, которую ставило приложение по кнопке
«Доработать с учётом ответов». Работа, сделанная руководителем по сообщению в диалоге, в свод не
попадала: версия оставалась прежней, и интерфейс молчал. Теперь свод обновляется **сам**: событие →
«тишина» → карточка обновления → версия N+1.

### W20-SVOD.1 Поля комнаты

| Поле | Тип | По умолчанию | Допустимо |
|---|---|---|---|
| `auto_svod` | bool | `true` | — |
| `auto_svod_quiet_s` | int | `180` | 30…3600 |
| `auto_svod_min_gap_s` | int | `600` | 60…86400 |

Принимаются в `PATCH /api/rooms/{id}` (неизвестное значение → `400 bad_request` с `field` и
границами `min`/`max`), возвращаются в карточке комнаты. Там же готовый блок `svod` для интерфейса:

```json
{"auto_svod": true, "auto_svod_quiet_s": 180, "auto_svod_min_gap_s": 600,
 "auto_svod_label": "Включено: свод обновляется сам",
 "quiet_label": "не раньше чем через 3 мин тишины", "gap_label": "не чаще раза в 10 мин"}
```

У комнат, созданных раньше, полей нет — читаются как «включено, 3 минуты, 10 минут», то есть
живой свод работает и у старых комнат.

### W20-SVOD.2 `synthesis.live` в `GET /api/requests/{id}`

```json
{"state": "pending", "since_version": 4, "behind": {"answers": 2, "notes": 1, "decisions": 0, "cards": 0},
 "last_event_at": 1793164900, "next_update_at": 1793165080, "auto": true,
 "text": "Отстаёт: 2 ответа и 1 сообщение не учтены — обновится в 14:05"}
```

| `state` | Когда | `text` |
|---|---|---|
| `fresh` | после последней версии значимых событий нет | «Актуально на 13:48 · версия 4 · учтено: 9 ответов, 3 сообщения, 2 решения» |
| `pending` | события есть, ждём «тишину» и/или потолок частоты | «Отстаёт: 2 ответа и 1 сообщение не учтены — обновится в 14:05» |
| `updating` | карточка обновления уже в работе | «Обновляется… версия 5» |
| `stale` | события есть, автообновление выключено или невозможно | «Отстаёт: 2 ответа не учтены — автообновление свода выключено, обновите вручную» |
| `off` | у запроса ещё нет свода | «Свод ещё не выпущен — появится после первого ответа команды.» |

С версии 1.2.0 добавлено состояние `waiting_cards` (свод ждёт завершения работы команды) —
см. «W21-SVOD.2».

`behind` — сколько **не учтено** показанной версией: `answers` (ответы на вопросы), `notes`
(сообщения владельца в диалоге), `decisions` (решения), `cards` (завершённые карточки работы).

**Что считается событием:** ответ на вопрос, сообщение владельца в диалоге, решение и завершённая
карточка работы (в том числе карточка руководителя, но не его итоговая версия свода).
**Что НЕ считается** (защита от зацикливания): сама карточка обновления свода, карточка критика,
ручная доработка, карточка «Уточнение от Михаила» (её содержимое уже учтено как сообщение) и
корневая карточка-источник версии. Поэтому новая версия не порождает ещё одну.

`last_event_at` — время последнего события (`null`, если событий нет). `next_update_at` — когда
свод обновится: `max(last_event_at + quiet_s, version_at + min_gap_s)`; `null` в состояниях
`fresh`, `updating`, `off` и `stale`, а также если расчётное время уже позади (B-49: прошедшее время
не обещаем — в `text` тогда «обновится в ближайшие минуты», см. «W21-SVOD.7»).

### W20-SVOD.3 `delivery` у сообщений в `thread[]`

Каждый элемент ленты `thread[]` с `kind:"message"` (автор — человек) получает:

```json
{"state": "applied", "version": 4, "cards": ["t_c0ffee01"], "reason": "",
 "text": "Учтено в версии 4"}
```

| `state` | Значение | `text` |
|---|---|---|
| `waiting` | ждёт обновления свода | «Ждёт обновления свода — в 14:05» |
| `working` | обновление уже идёт | «В работе: 1 карточка» (карточки — в `cards`) |
| `applied` | учтено версией | «Учтено в версии 4» |
| `applied_nochange` | учтено, но менять в своде было нечего | «Учтено, менять нечего» |
| `declined` | не будет учтено | «Не учтено — автообновление свода выключено, нажмите «Обновить сейчас»» |

`reason` — `auto_off`, `no_leader`, `no_svod` (у `declined`/`waiting`); `version` — номер версии,
которая учла сообщение, `cards` — карточки этой версии (или идущего обновления).
Комментарии руководителя и служебные события `delivery` не получают.

### W20-SVOD.4 `POST /api/requests/{id}/actions {action:"svod_refresh"}`

Кнопка «Обновить сейчас»: версия свода по запросу пользователя, без ожидания «тишины».
`note` (необязательно, ≤ 200 000 знаков) уходит в карточку как «замечание Михаила».

* `200` → `{ok:true, action:"svod_refresh", card_id, behind:{…}}`;
* `409` → `{code:"conflict", message:"Обновление свода уже идёт — дождитесь новой версии.",
  detail.reason:"updating"}` — второй раз не запускаем;
* `409`, если свода ещё нет (`detail.reason:"no_svod"`) или в комнате не выбран руководитель
  (`no_leader`).

Карточка создаётся тем же путём, что автоматическая и что кнопка «Доработать с учётом ответов»:
`engine_write.create_child_card` + `contract.build_revision_body` + тот же дайджест «что появилось
после версии». Приложение модели не запускает — карточку берёт обычный движок.

### W20-SVOD.5 Автоматическая карточка обновления

* Ставится сторожем (`server/watcher.py`, шаг после «оценки работы»), не чаще
  `auto_svod_min_gap_s` и не раньше «тишины» `auto_svod_quiet_s`.
* **«Льготный старт»:** события раньше момента включения автообновления событий свода не
  образуют. Момент хранится в `<каталог состояния>/svod_started.json` (`started_at`; файла нет —
  пишется текущее время при первом запуске версии). Поэтому после переноса в боевое дерево
  старые ответы и сообщения **не** поднимают доработки и не показываются как «Отстаёт»;
  перезапуск сервера порог не сдвигает. Кнопка «Обновить сейчас» видит все события.
* **Лимит частоты:** не больше `MAX_CARDS_PER_HOUR = 6` автоматических карточек в час на
  комнату (в том же файле, `launches`); седьмая не ставится, сторож возвращает
  `{action:"wait", reason:"hour_limit"}`. Ручной `svod_refresh` лимит не соблюдает — это действие
  пользователя.
* Заголовок — «Обновление свода (авто)», исполнитель — руководитель комнаты, родитель — карточка
  последней версии (или корень).
* Заметка — «Автообновление: учтены события после версии — 2 ответа и 1 сообщение. Что учесть:
  ответ на вопрос; сообщение.»
* Идемпотентность: в реестре запроса хранится отпечаток набора событий (`svod_event_key`) и время
  запуска (`svod_launched_at`). Тот же набор событий второй карточки не создаёт; в ключ
  идемпотентности карточки входит тот же отпечаток (`svod-<rid>-<ключ>`).
* В `history[]` такая версия помечена `auto: true` (её поставило приложение), в `reason` —
  `auto`.

### W20-SVOD.6 Версия от руководителя: `metadata.svod`

Руководитель публикует свою версию итоговой карточкой:

```json
{"svod": {"note": "учли замечание про сроки поставки", "changes": ["сроки поставки", "состав меню"]}}
```

Приложение принимает такую карточку как следующую версию свода: `history[].kind = "leader"`,
`source_task` — id этой карточки, `note` — из `note` (принимается и старое имя `version_note`),
`changes` — из `changes`. Текст версии — свод этой карточки (`summary`).

### W20-SVOD.7 Контракт руководителя

В `КОМНАТА-КОНТРАКТ` (и в `docs/AGENT-GUIDE.md`) добавлено правило: свод обновляет **приложение**;
руководитель работает через итоговую карточку и `metadata.svod`; советовать пользователю
«переоткрыть», «пере-завершить» или «закрыть-открыть» корень **запрещено** — такого способа нет.
Формулировки проверяются тестом `tests/test_svod_live.py` (`ContractTest`).

### W20-SVOD.8 Совместимость

* `synthesis.live` и `delivery` — **добавленные** поля; старые клиенты их просто не читают.
* `history[].kind` получает новое значение `leader`; `SynthesisVersion.auto` появляется только у
  автоматических версий.
* У комнат без полей `auto_svod*` действуют значения по умолчанию — данные не переписываются.
* Демо-сервер считает то же самое на лету (`tools/mock_versions.py`, `apply_svod_live`), включая
  ответ на кнопку «Обновить сейчас».

## W21-SVOD — живой свод: ответы вперёд, ожидание команды, «остывание» (v1.2.0, WAVE21 §2 S2, B-45/B-46/B-47)

Три жалобы 11.11 и остатки ревью волны 1. Контракт §1.1 волны 20 не отменяется — меняются правила
«когда пора», добавляется одно состояние и два `reason`.

### W21-SVOD.1 Ответ на вопрос — приоритетное событие (B-45)

Ответ на вопрос руководителя ждёт **60 секунд** «тишины» вместо `auto_svod_quiet_s`, и **потолок
частоты** (`auto_svod_min_gap_s`) такую доработку не задерживает: человек уже ответил, руководителю
нужно учесть ответ, а не выдерживать паузу. Лимит `MAX_CARDS_PER_HOUR = 6` карточек в час остаётся
для всех.

* «Приоритет» — только когда **последнее** событие набора — ответ (`kind:"answer"`). Если после
  ответа пришло сообщение, действуют обычные правила: последнее слово снова за сообщением.
* Если комната настроена на «тишину» короче минуты (`auto_svod_quiet_s < 60`), берётся настройка
  комнаты — ответ не может ждать дольше неё.
* `next_update_at` считается так: `last_event_at + quiet`, где `quiet = min(auto_svod_quiet_s, 60)`
  для ответа и `auto_svod_quiet_s` для остальных; затем для остальных событий —
  `max(…, version_at + auto_svod_min_gap_s)`, для ответа — без этого ограничения.
* Проверки: `tests/test_svod_live.py` (`AnswersPriorityTest`, `AnswersPriorityLimitTest`).

### W21-SVOD.2 `waiting_cards` — «обновится после завершения работы команды» (B-46)

Карточка обновления **не ставится**, пока на запросе есть карточки команды в статусах
`triage|todo|ready|running|review|scheduled` (или `blocked`, если это ожидание зависимости). Их ведут
руководитель и исполнители: собранный сейчас свод вышел бы устаревшим в ту же минуту.

```json
{"state": "waiting_cards", "since_version": 5, "behind": {"answers": 0, "notes": 1, "decisions": 0, "cards": 0},
 "last_event_at": 1793164900, "next_update_at": null, "auto": true,
 "text": "Обновится после завершения работы команды"}
```

* `next_update_at` — `null`: точное время неизвестно, оно зависит от команды (кнопка «Обновить
  сейчас» работает: это действие человека).
* **Не держат** свод: сами карточки обновления (иначе свод ждал бы сам себя), карточки-сообщения
  «Уточнение от Михаила» (их содержимое уже в диалоге) и карточки, которые ждут человека
  (`blocked` с вопросом, `needs_human:true`) — иначе ожидание было бы вечным, пока Михаил не
  ответит.
* В `plan()` это `{action:"wait", reason:"waiting_cards"}`; в `delivery` сообщений — «Ждёт
  завершения работы команды» (`reason:"waiting_cards"`).
* Полный список состояний `synthesis.live` теперь: `fresh`, `pending`, `waiting_cards`, `updating`,
  `stale`, `off`.
* Проверки: `WaitingCardsTest` (цепочка из трёх карточек, «ждёт человека», карточка-сообщение).

**Потолок ожидания (W21-REV2).** Карточка команды, которая держит свод дольше **двух часов**
(`BUSY_MAX_S = 7200`), свод больше не держит: в дереве нет сердцебиений, и брошенная карточка
«в работе» иначе останавливала бы обновление навсегда. Время считается по `started_at` карточки и
времени её запусков (`runs[]`); если времени нет вовсе, карточка считается свежей (доказать
«зависла» нельзя) — и ждём, как раньше. При срабатывании потолка:

* `live.state` уже `pending`, `text` = «Отстаёт: … — работа команды идёт дольше двух часов,
  обновлю в 14:20» (причина названа, иначе это выглядело бы обычной «тишиной»);
* в заметке карточки обновления — «Собрано, не дожидаясь карточек (работа команды идёт дольше двух
  часов): «…»»;
* `plan()` отдаёт `stalled:[id…]` рядом с `action:"create"`.
* Проверки: `WaitingCardsCeilingTest`.

### W21-SVOD.3 Снятие и возврат решения — не событие свода (B-47)

Строки ветки `[Михаил · решение d_… «…»] Снять — неактуально` и `… Вернуть` **событием не
считаются**: они меняют только список решений (сам статус виден при чтении), а автообновление из-за
уборки списка будило руководителя (в боевых данных — карточка `t_eb947e05`).

* В ленте у такой строки `svod_event:false` и `decision_action:"dismiss"|"restore"`.
* `delivery` у неё — `{state:"applied_nochange", reason:"decision_status", text:"Учтено, менять
  нечего"}`.
* Ответы на решения (`Принять`, `Отклонить`, `Обсудить`) — по-прежнему сообщения владельца и событие
  свода.
* Проверки: `DismissNotEventTest` (снятие, возврат, «Принять» всё ещё считается).

### W21-SVOD.4 «Остывание» после неудачной карточки обновления

Если карточка обновления упала, ушла в таймаут или `blocked`, сторож больше не пробует на каждом
тике: ожидание растёт **10 → 20 → 40 минут** (`600 · 2^(n−1)`), но не больше **двух часов**.

* В `live` это `state:"pending"`, `text:"Обновление не удалось — повтор в 14:40, кнопка «Обновить
  сейчас» работает"`, `next_update_at` — момент повтора.
* В `plan()` — `{action:"wait", reason:"cooldown"}`.
* Упавшая карточка больше не выглядит как «Обновляется…» (раньше `updating` висело вечно, потому что
  статус `failed` не закрытый).
* Повтор после «остывания» разрешён даже на том же наборе событий (`svod_event_key` тот же) — иначе
  одна неудача блокировала бы свод навсегда.
* После `MAX_AUTO_RETRIES = 5` попыток подряд автоматика останавливается: `state:"stale"`,
  `text:"… — обновление не удалось несколько раз, нажмите «Обновить сейчас»"`, `reason:"retry_limit"`.
  Кнопка «Обновить сейчас» работает в любой момент, в том числе во время «остывания».
* Проверки: `CooldownTest`.

### W21-SVOD.5 Если отметку о старте сохранить нельзя — не молчим

«Льготный старт» (`<каталог состояния>/svod_started.json`) — единственное место, где помнится момент
включения автообновления. Если записать его нельзя (нет каталога, нет прав, файл нечитаем), раньше
это происходило молча и автообновление фактически не работало.

* `svod_live.state_problem(cfg)` → `""` / `"no_state_dir"` / `"unreadable"`.
* Сторож пишет об этом в журнал **не чаще раза в час на комнату** (`log_problem`, шаг 7 `tick_once`).
* `live` в этом случае `state:"stale"`, `text:"Отстаёт: … — автообновление свода не может сохранить
  отметку (…), нажмите «Обновить сейчас»"`.
* Проверки: `StateProblemTest`.

### W21-SVOD.6 Демо-мок

`tools/mock_versions.py` считает то же самое на лету (`waiting_cards`, минутная «тишина» после
ответа, `delivery` «Ждёт завершения работы команды»). «Льготный старт» в демо включается явно:

```
python tools/mock_server.py --demo-grace        # или TEAMROOM_DEMO_GRACE=1
```

Тогда всё, что было в данных на момент запуска демо, получает подпись «Не учтено — написано до
включения автообновления; «Обновить сейчас» учтёт вручную» (`delivery.reason = "grace_start"`).

### W21-SVOD.7 Версия свода по завершению карточки приложения (B-49)

**Симптом на боевом (12.11).** Три карточки «Обновление свода (авто)» подряд завершились
`outcome=completed`, но в `task_runs.metadata` не было ключа `svod` (руководитель-модель забыл поле).
Приложение такие карточки версией не признавало: `behind` не обнулялся, `next_update_at` оставался в
прошлом (страница обещала «обновится в 11:47» через 18 часов), а ключ идемпотентности на том же
наборе событий блокировал новую карточку. Три запуска агента ушли впустую.

**Правило.** Карточка обновления, которую поставило ПРИЛОЖЕНИЕ, — новая версия свода **и без
`metadata.svod`**:

* опознание: запись реестра (`svod`/`auto`/ключ идемпотентности `svod-<rid>-…`) **или** заголовок
  «Обновление свода» / «Обновление свода (авто)» (так ставит и сторож, и кнопки «Обновить сейчас» /
  «Доработать с учётом ответов»); `metadata.svod` НЕ требуется;
* условие: карточка завершилась успешно (`status` закрыт, последний запуск `outcome=completed`) и в
  ней есть текст свода; **упавшая, отменённая или незавершённая карточка версией не становится** —
  её разбирает «остывание» (`W21-SVOD.4`) как раньше;
* `synthesis.history[]` получает `{kind:"revision", reason:"auto"|"note", task_id, at, auto:true,
  source:"app", posted_at}`: `at` — время завершения карточки, `changes` — из `metadata.changes`
  (список строк), `note` — заметка карточки (в реестре), `posted_at` — когда карточка ушла;
* текст свода берётся по прежним правилам — последний принятый текст из ответа карточки
  (`synthesis.text_md`, `synthesis.source_task` — эта карточка).

**Какие события считаются учтёнными.** Граница — `posted_at` (когда карточка ушла руководителю), а не
время её завершения: события с `at <= posted_at` в карточку вошли и учтены, а написанные **пока
карточка работала** — остаются в `behind` и попадут в следующую версию. Нет `posted_at` (старые
записи, первая версия) — граница прежняя, время выпуска (`contract.accounted_at`).

**Прошлое время не обещаем.** Если расчётное время (`last_event_at + quiet`, `version_at + min_gap`)
уже позади, `live.next_update_at` не отдаётся (`null`), а `live.text` говорит «обновится в ближайшие
минуты» (у ожидания карточек — «обновлю в ближайшие минуты»). У `delivery` сообщений в этом случае
«Ждёт обновления свода — в ближайшие минуты».

**Защита от цикла.** Если на тот же набор событий (`event_key`) уже выпущена успешная
автоматическая карточка, а `behind` всё ещё не ноль (версия не зарегистрировалась — например,
руководитель вернул пустой ответ), автоматика останавливается: набор и руководитель те же, вторая
такая же карточка тоже не даст версии. Жёсткий потолок — `MAX_AUTO_TRIES_PER_KEY = 2` карточки на
набор (третьей не будет ни при каких условиях):

```json
{"state": "stale", "since_version": 5, "behind": {"answers": 0, "notes": 1, "decisions": 0, "cards": 0},
 "auto": true, "next_update_at": null,
 "text": "Обновление выполнено, но что-то не учтено — нажмите «Обновить сейчас»"}
```

`plan()` → `{action:"none", reason:"loop_guard"}` (`LOOP_GUARD_AFTER = 1`). Неудачные карточки
счётчик не расходуют (это «остывание» W21-SVOD.4), ручные — тем более; кнопка «Обновить сейчас»
работает всегда, в том числе в этом состоянии.

**Контракт руководителя усилен** (`contract.svod_rule`, `docs/AGENT-GUIDE.md`): «**после доработки
свода обязательно заполни `metadata.svod`**» — но приложение от этого поля не зависит.

* Проверки: `tests/test_svod_live.py` (`AppCardVersionTest`, `AppCardLiveTest`, `LoopGuardTest`).

### W21-SVOD.8 Карточка обновления крепится к КОРНЮ, дерево читается до 12 уровней (B-49b)

**Симптом на боевом (12.11).** Каждая новая карточка «Обновление свода (авто)» привязывалась
`task_links` ребёнком ПРЕДЫДУЩЕЙ карточки обновления (родителем был `synthesis.source_task`), поэтому
цепочка «корень → … → обновление → обновление → …» росла в глубину. Чтение дерева обходило его вширь
только до `MAX_DEPTH = 6` уровней, и карточки глубже в дерево запроса не попадали: три завершённые
карточки обновления и четвёртая (в очереди) не были видны → версии свода не регистрировались (даже
после `W21-SVOD.7`), события оставались «неучтёнными», а предохранители, которые опираются на
видимые карточки (лимит `MAX_CARDS_PER_HOUR`, защита от цикла `W21-SVOD.7`), не срабатывали.

**Правило 1 — новая карточка ставится ребёнком корня запроса** (`svod_live.create_update_card`):

* и автообновление сторожа, и ручные «Обновить сейчас» / «Доработать с учётом ответов» пишут
  `parent_id = <root_id>` — и только его; `synthesis.source_task` для связи больше не используется;
* порядок «одна карточка за другой» держит не связь, а `open_update_cards` (пока идёт предыдущая,
  новая не ставится), идемпотентность — прежний ключ `svod-<rid>-<отпечаток>` (ручные —
  `svod-<rid>-m<версия>-<отпечаток>`), формат ключа не изменился;
* глубина новых цепочек не растёт: все карточки обновления — первого уровня.

**Правило 2 — старые цепочки читаются без правки данных.** `engine_tree._collect` после обхода
дерева делает дополнительный шаг `_collect_svod`:

* один `SELECT ... WHERE idempotency_key LIKE 'svod-<rid>-%' ESCAPE '\' ... LIMIT <остаток
  MAX_NODES>` — карточки обновления этого запроса попадают в дерево **независимо от глубины**;
* `depth`/`parents` — по фактическим связям доски (если родитель известен), иначе карточка считается
  ребёнком корня (`depth = 1`); при цепочке `depth` растёт по связям, но не выше `MAX_DEPTH`;
* шаблон экранируется (`engine_db.like_prefix`): `_` в id запроса — не «любой символ» в `LIKE`,
  поэтому карточки чужих запросов с похожим id (`t_1000aaaa` / `tX1000aaaa`) не подтягиваются;
  тем же экранированием пользуется поиск карточек «Уточнение от Михаила» (`thread._note_cards`,
  ключ `tr-note-<rid>-…`);
* предел схемы без `tasks.idempotency_key` — шаг молча пропускается (чтение работает как раньше).

**Правило 3 — `MAX_DEPTH = 12`, `MAX_NODES = 400`** (`engine_db`). Замер на песочной доске
(`tests/test_engine_read.py`, `TreeDepthTest`): 69 карточек в дереве (20 линейной цепочкой + 8 карточек
обновления + 60 под корнем) — медиана чтения детали 25,8 мс против 25,4 мс на прежнем коде (15
прогонов; разница — в пределах шума, лишний `SELECT` по ключу бесплатен на таком размере), прежний
сторож `test_21` (8 карточек, медиана < 50 мс) остался зелёным. Предел остался документированным: карточка КОМАНДЫ глубже 12
уровней в дерево не попадает (для линейной цепочки из 20 карточек видны 12 уровней), а карточки
обновления видны всегда — их подтягивает отдельный шаг.

Что меняется для клиента: ничего (`tree[]`, `parent_ids`, `depth`-порядок — те же поля). Версии
свода в `synthesis.history[]` теперь приходят целиком даже у старых запросов с длинной цепочкой;
`synthesis.version` может вырасти (например, 2 → 9), потому что ранее невидимые карточки обновления
наконец учтены.

* Проверки: `tests/test_engine_read.py` (`TreeDepthTest` — 8 тестов: цепочка из 8 карточек обновления
  даёт все версии, новая схема держит дерево плоским, хвост 20-карточной цепочки виден, чужой запрос
  не цепляется, `open_update_cards`/`compute`/`plan`/`update_tries` видят самую глубокую карточку,
  время чтения), `tests/test_svod_live.py` (`UpdateCardParentTest`, `UpdateCardsSeenTest`).

### W21-SVOD.9 `metadata.svod.decisions_full` и результат карточки обновления — не событие (B-50)

Два дефекта в логике решений, найденные на боевых данных после переноса 1.2.0 (когда карточки
обновления стали видны в дереве — `W21-SVOD.8`).

**1. «Решения нет в новой версии» только по полному списку.** Прежнее правило («есть непустой
`metadata.decisions` → пропущенное устарело») ложно срабатывало: карточка обновления с одним
элементом в `metadata.decisions` (руководитель перечисляет только открытое/новое) объявляла
устаревшими ВСЕ прежние решения — на боевом посерели два правильно принятых решения.

* `absent` (`superseded_why:"absent"`) ставится ТОЛЬКО если карточка заявила полный список:
  `metadata.svod.decisions_full: true` (`true`/`1`/`"true"`/`"да"`);
* без флага карточка со списком решений их **добавляет/обновляет** и ничего не гасит; решения
  прежних версий, которые она не повторила, продолжают действовать и приходят в `decisions[]`
  элементами с `carried: true` и `from_version: N` (статус — тот, что был);
* явный отзыв `metadata.decisions_retracted` работает как раньше (`why:"retracted"`);
* решение, снова попавшее в более позднюю полную версию, перестаёт быть «исчезнувшим»; отзыв так
  не снимается;
* последнее действие ЧЕЛОВЕКА сильнее статуса в карточке руководителя: если владелец снял решение
  (`dismissed`), повторное перечисление его руководителем (в том числе со `status:"accepted"`) его
  не воскрешает — `status_source:"user"`, `status:"dismissed"`.

**2. Результат карточки обновления не запускает следующую карточку.** Решения карточки обновления
приложения получают время версии (`at`) и на следующем тике выглядели новым событием: приложение
ставило карточку обновления по собственному результату, та снова перечисляла решение — бесконечный
цикл с запуском модели раз в ~30 минут.

* правило «собственные карточки автоматики событием не считаются» распространено на их РЕШЕНИЯ:
  `svod_live.events()` не считает решения, чьё `at` совпадает с временем версии, выпущенной
  карточкой обновления приложения (`synthesis.history[].auto`/`source:"app"`;
  `svod_live.app_versions`, `own_decision_times`);
* завершённые карточки обновления не считаются и событием-карточкой (`_cards_after` отсекает их по
  заголовку «Обновление свода…» — даже если запись реестра потерялась, а роль в дереве не `revision`);
* ответы и сообщения Михаила отсекаются как раньше (по своим `at`, `_no_event_marks`); вопросы и шаги
  событиями свода не являются вовсе;
* следствие: после завершения карточки обновления `live.state = "fresh"`, `behind` = 0, `plan` →
  `none/fresh` — сколько тиков подряд ни пройдёт;
* решения, появившиеся ПОСЛЕ версии (например, от рабочей карточки), событием остаются.

* Проверки: `tests/test_dec_q.py` (`DecisionsFullTests`, `PureFunctionTests.test_absent_needs_the_full_flag`,
  `test_decision_returning_to_the_swod_is_live_again`), `tests/test_svod_live.py` (`OwnCardResultTest`).

## W21-ANSWERS — «Пояснить» без текста и ответы без кнопки досылки (v1.2.0, WAVE21 §2 Q1, B-44/B-45)

Раздел описывает клиент и серверные поля, которых касается пакет Q1: кнопка «Пояснить…» у вопроса
теперь работает без текста, а кнопка «Отправить руководителю накопленные ответы» больше не
показывается, пока свод обновляется сам.

### W21-ANSWERS.1 `POST /api/requests/{id}/actions {action:"answer"}` — текст необязателен для пояснения

`need_explanation: true` (bool, по умолчанию `false`) — это просьба ПОЯСНИТЬ вопрос, а не ответ.
Для неё `text` может отсутствовать или быть пустым.

| Тело | Ответ | Что происходит |
|---|---|---|
| `{action:"answer", question_id, need_explanation:true}` | `200` | В ветку уходит `[Михаил · просит пояснить по вопросу q_x «…»] Поясни вопрос подробнее: зачем он, какие варианты ответа и их последствия — простыми словами`; реестр ответов получает `status:"waiting_explanation"`, `answered:false` (**вопрос остаётся открытым**); руководителю создаётся (или дописывается) карточка «Уточнение» — та же, что у пояснения с текстом |
| `{action:"answer", question_id, need_explanation:true, text:"уточни вот это…"}` | `200` | То же, но в ветке и в карточке — слова человека («поясни вот это: …») |
| `{action:"answer", question_id}` (или `text:""`) без `need_explanation` | `400` | `{"error":{"message":"Нужен текст","detail":{"field":"text"}}}` — «Отправить» с пустым полем по-прежнему ничего не отправляет |
| тот же запрос повторно в окне 120 с | `200`, `duplicate:true` | Вторая просьба НЕ создаётся: ключ идемпотентности — запрос + вопрос + текст просьбы (другой текст = новая просьба) |

Стандартная формулировка — одна на сервер, ветку и демо-мок: `answers.DEFAULT_EXPLAIN_TEXT`.
Ответ на вопрос (`need_explanation` не задан) остаётся как раньше: текст обязателен, вопрос
закрывается (`status:"sent"`, `answered:true`).

**Проверка типов (W21-REV2).** `need_explanation` принимается только как булево (`"true"`, `1`,
`"да"` → `400` с `detail.field="need_explanation"`), а `text`, если передан, — только строкой:
`text:123` или `text:[…]` дают `400` с `detail.field="text"`, а не молчаливую подмену
стандартной просьбой. Ключ идемпотентности содержит id вопроса, поэтому просьбы по разным
вопросам одного запроса не смешиваются (две просьбы — две карточки). Состояние
`waiting_explanation` видно и в `open_questions[]` (`needs_explanation:true`, `answered:false`,
`needs_answer:true`), и в счётчиках `answers.summary.waiting_explanation`.

### W21-ANSWERS.2 `answers.summary` — спокойные подписи вместо «без реакции»

`RequestDetail.answers.summary` (без изменений в структуре; `can_resend` оставлен для
совместимости и старых клиентов):

```json
{"answers": 2, "states": {"applied": 1, "sent": 1}, "pending": 1, "waiting_explanation": 0,
 "can_resend": true, "resend_pending": 1,
 "resend_label": "Отправить руководителю накопленные ответы",
 "resend_hint": "Ответы переданы руководителю — он учтёт их в ближайшем обновлении свода",
 "resend_hint_soon": "Ответы переданы руководителю — он учтёт их в ближайшее время"}
```

* `resend_hint` / `resend_hint_soon` — спокойная строка для показа вместо счётчика «N ответа без
  реакции руководителя». Слова «без реакции» из интерфейса убраны; в данных состояния `sent` /
  `received` остаются как были.
* `waiting_explanation` в `resend_pending` не входит: просьба о пояснении — не накопленный ответ,
  досылать нечего.

### W21-ANSWERS.3 Где показывать кнопку — решает клиент по `synthesis.live.auto`

`RequestDetail.synthesis.live` (см. W20-S1 §1.1):

* `live.auto == true` (автообновление свода включено) → кнопки «Отправить руководителю
  накопленные ответы» НЕТ. Вместо неё и вместо счётчика «без реакции» — строка
  `«Ответы переданы руководителю — он учтёт их в ближайшем обновлении свода (около 14:20)»`;
  время берётся из `live.next_update_at` (локальное `ЧЧ:ММ`), без него — «…в ближайшее время».
* `live.auto == false` → кнопка `answers_resend` остаётся, подпись под ней — «N ответ(ов)
  ждёт отправки» (без слов «без реакции»). Это единственный случай, когда ответы правда ждут
  нажатия.
* поля `live` нет вовсе (старый сервер) → ведём себя как при `auto == false`: кнопка есть.

### W21-ANSWERS.4 Демо-мок

`tools/mock_write.py` повторяет сервер: `need_explanation:true` без `text` → стандартная просьба,
`waiting_explanation`, запись в ленте, защита от повтора в 120 с (`duplicate:true`); «Отправить»
с пустым полем → `400`. `tools/mock_read.py` отдаёт `synthesis.live` у готового свода, причём
`auto` берётся из настроек КОМНАТЫ (`auto_svod`): выключите автообновление в «Настройках» демо —
кнопка досылки появится.

## 9. Открытые вопросы контракта

1. `stage` из нескольких запросов: побеждает фаза из последнего завершённого запроса комнаты; ручные правки фаз в API не предусмотрены (только через новый запрос).
2. `done_today` считается по поясу сервера; если Михаил путешествует — счётчик может «скакнуть» на границе суток.
3. `critic` — объект в обеих формах: `RequestSummary.critic` = `CriticRef` (`{enabled, state, verdict, round}`), `RequestDetail.critic` = `CriticBlock` (`+ max_rounds, issues, task_id`). Так реализовано в `server/engine_read.py`; фронтенд читает `critic.issues` только на странице запроса.
4. `finished_at` — конец всего цикла, включая пост-синтез проверку. У legacy-карточек он равен завершению корня, и `synthesis.at` может быть больше `finished_at`; UI не должен на этом падать.
5. `max_rounds` больше 2 не поддерживается **в режиме `rounds`**: движок не перепаивает граф, а каждый раунд — новая карточка. При исчерпании лимита ответ отдаётся как есть + `warnings[].code=critic_rounds_exhausted`. Для «до полного одобрения» (`critic_mode="until_approved"`) тот же механизм работает без лимита `max_rounds`: ограничитель — `critic_round_cap` (2..30, по умолчанию 12); по достижении цикл останавливается (`critic.state="capped"`, `critic.stopped`), ответ отдаётся с отчётом об остановке. При повторе одних и тех же замечаний 3 раунда подряд — `critic.state="stalled"` и вопрос Михаилу; при лимите провайдера — `critic.state="paused"` и автоматическое продолжение (`critic.paused.until`).
6. `GET /api/file` не умеет Range — большие PDF отдаются целиком.
7. `POST /api/open` на не-Windows хосте возвращает `400` (`detail.reason="unsupported"`), в моке — `{revealed:false}` без запуска процесса.

## 10. Журнал расхождений W4-CONFORMANCE и как они закрыты (v2.1, волна 5)

Источник: `docs/status/conformance.md` (§3 «Решения спорных семантик», §7 «Осталось другим»). Прогон `python tools/check_conformance.py` + `python tools/check_fixtures.py` — 0 нарушений.

| # | Расхождение (запись W4) | Решение | Где описано |
|---|---|---|---|
| 1 | `done` + открытый вопрос Михаилу: §4 ставил `needs_you` выше `done`, §3.3 определял `needs_you = status=="needs_you"`; код и фикстуры (`request_done_legacy.json`, реальный `t_605613e0`) держат `done` | **Принято решение кода:** `status` остаётся `done`, `needs_you:true` в `RequestSummary` и `RequestDetail`; `counters.needs_you` считает такие запросы, `done_today` — тоже. Фикстуры приведены (`needs_you:true` у `request_done*.json`, ленты и счётчик `cafe`=3), `check_fixtures.py` проверяет инвариант | §3.2 `counters`, §3.3 `needs_you`, §4 |
| 2 | `/api/profiles` отдавал ростер Hermes (`name/model/provider`), а не `Profile` | Контракт `Profile` (`profile/title/description/tags/kind/available`) сохранён, бэкенд приведён | §3.5, §6.1 |
| 3 | `health.engine.schema_version` — `null` в контракте, int в фикстуре | int (сейчас `3`) при `schema_ok=true`; `null` — только при нераспознанной схеме | §3.1 |
| 4 | `synthesis.state:"missing"`, `history[].note: null` | `none` и всегда строка (`""`, «Учтены замечания критика», «Принято критиком») | §3.4 |
| 5 | `critic.state` у включённого критика без карточки — `off` | `waiting` (проверки ещё не было); `off` — критик выключен | §3.4 `CriticBlock` |
| 6 | `assignee`/`started_at` невзятого корня — `null`; `room` у adopted-карточки — из `tenant` (`NULL`) | директор комнаты / минимальный `started_at` детей / `room` из реестра | §3.3 |
| 7 | `answer` нельзя было сохранить (metadata run'а движок не переписывает) | ответ лежит в реестре комнаты (`answers`), чтение накладывает его на `open_questions`; повтор → `409`, неизвестный `question_id` → `404` | §6.3 `answer` |
| 8 | `comment`: бэкенд отдаёт `id`, мок — без | мок отдаёт `id:int` (растёт), тест `test_comment_has_int_id` | §6.3 `comment`, §7 |
| 9 | `candidates`: нет `generated_at`, `preview`, `decomposed_at` | бэкенд отдаёт все три, как мок и фикстура | §6.3 `candidates` |
| 10 | `team`: нет `current`, оркестратор не первым | `current` = активная или blocked-карточка; директор первым | §3.5, §6.4 |
| 11 | Мягкие сравнения: `metadata` карточки, `JournalEvent.detail`, `blocks` отчёта — только как объект; живые `null` в nullable-полях и пустые списки — допустимы; лишние ключи (`progress.running`, `team[].load`, `backlog.sections`, …) — допустимы | не расхождения: контракт расширяемый, клиент игнорирует незнакомые поля | §3 (преамбула), §7 |
| 12 | Посев `tools/seed_content.py` беднее фикстур (у `working` нет run'ов, у `needs_you` нет metadata) | **Не закрыто:** файл вне зоны этого контракта; вызывает «пустые списки» в предупреждениях `check_conformance.py`, форму ответов не нарушает | — |

Найдено при актуализации (не из записи W4): статус `failed` по `critic.state="failed"` и предупреждения `critic_failed` / `critic_rounds_exhausted` в прежней редакции §4 бэкендом не реализованы (падение критика статус запроса не меняет) — §4 приведён к фактическому порядку проверок, коды предупреждений остаются зарезервированными. Путь `STATE.md` единый для контракта агентов и записи зеркала: `<vault_dir>/<room.id>/STATE.md` (§6.7).

## W20-I1 — сборка, проверки и автозапуск (v1.1.0, WAVE20, п.22–24)

### POST /api/diagnostics/autostart — «Включить автозапуск» из приложения

Пункт «Автозапуск» в `GET /api/diagnostics` при выключенном автозапуске получает действие

```json
{"fix": {"kind": "action", "label": "Включить автозапуск", "value": "autostart.enable",
         "confirm": "Сервер будет подниматься сам при входе в систему (…). Окно не появится — сервер слушает только этот компьютер."}}
```

`kind: "action"` — новое действие пункта проверки: интерфейс обязан спросить подтверждение
(показывает `confirm`) и только после согласия отправить запрос. Другие пункты по-прежнему
используют `command` (скопировать команду), `doc` (открыть документацию), `route` (перейти) и
`link` (открыть ссылку).

```http
POST /api/diagnostics/autostart
Content-Type: application/json

{"action": "enable"}
```

| Код | Когда | Тело |
|---|---|---|
| 200 | действие выполнено (даже если автозапуск уже был включён) | `{"ok": true, "message": "Автозапуск включён: …", "how": "…", "os": "windows\|macos\|linux", "written": ["~/…"], "notes": []}` |
| 400 | неизвестное действие или включать нечего | `{"error": {"code": "bad_request", "message": "В установке нет файла bin/team-room-server.cmd — автозапуску нечего запускать. Поставьте программу установщиком (install.ps1 / install.sh)."}}` |
| 500 | система отказала | `{"error": {"code": "internal", "message": "Не получилось включить автозапуск: …"}}` |

Правила ответа:

* `ok: false` и человеческий `message` — если ни один способ не сработал; «тихого успеха» не
  бывает, приложение говорит, что именно отказало;
* `written` — только `~`-пути (полные пути пользователя наружу не отдаются);
* `how` — способ, который реально применён: `задача планировщика TeamRoomServer`,
  `ярлык в «Автозагрузке»`, `LaunchAgent com.teamroom.server`,
  `служба systemd --user teamroom`, `строка в ~/.profile`;
* повторный вызов не дублирует автозапуск (маркер блока в профиле, `schtasks /F`, `launchctl
  unload` перед `load`).

Реализация — `server/autostart.py` (одна точка правды для всех систем); интерфейс —
`web/js/views/diagnostics.js` (`fix.kind === "action"`). Установщики (`install.ps1`,
`install.sh`) ставят ровно тот же автозапуск, поэтому кнопка и установщик не расходятся.

### CI и проверки на чистом раннере

`tools/ci_hermes_shim.py` (+ `ci_hermes_shim.cmd` / `ci_hermes_shim.sh`) — замена CLI `hermes`
для проверок: `kanban boards create|list`, `create`, `complete`, `comment`, `show`, `list`,
`unblock`, `assign`, `reassign`, `promote`, `reopen-review`, `archive`. Не поддерживает
диспетчер, декомпозицию и запуск агентов — на такие команды отвечает кодом 2 с объяснением.
Признак подмены для тестов: `TEAMROOM_CI_HERMES_SHIM=1` и строка `--version`
(`team-room-ci-hermes-shim …`), см. `tests/ci_marks.py` (`cli_is_shim`,
`has_real_hermes_cli`, `WHY_CI_SHIM`).

## W20-U4 — уведомление о реакции руководителя и композер (v1.1.0, WAVE20 §4, P9/P10)

Пакет U4. Описывает:

* поля сводки комнат для бейджа «есть новые реакции» (`last_reaction_at`,
  `reactions_unseen`, `reactions`);
* строку «Ответ учтён: …» у вопроса — из уже существующего `reaction` вопроса/шага/решения;
* локальную отметку «прочитано» (браузер, не сервер);
* композер нового запроса: один селектор проверки критиком при прежних полях API.

### W20-U4.1 Сводка комнат: реакции руководителя

`GET /api/rooms/summary` — по каждой комнате добавлены поля (старые не менялись):

| Поле | Тип | Значение |
|---|---|---|
| `last_reaction_at` | `int\|null` | время последней реакции руководителя в комнате (секунды эпохи); `null` — реакций нет |
| `reactions_unseen` | `int` | сколько реакций найдено (точное «непрочитанное» считает браузер) |
| `reactions_total` | `int` | то же число явным именем (для читаемости и тестов) |
| `reactions` | `{<id запроса>: {count, last_at}}` | реакции по запросам — для отметок в «Недавнем» |

Что считается реакцией: комментарий руководителя комнаты (`room.orchestrator`) с меткой
`[Руководитель · реакция на ответ|шаг|решение …]` **или** `metadata.reaction` в его карточке
(первая дорога чтения — `answers.reactions_from_tree`). Считается по корню запроса: реакция
на дочернюю карточку (шаг) поднимается к своему запросу.

Считаются только запросы, по которым в реестре комнаты есть `answers` или `step_actions` —
без ответа человека реагировать не на что, такие запросы не читаются вовсе. Ограничения:
до 40 свежих запросов комнаты, глубина дерева ≤ 2, до 20 элементов в `items` модуля
`server/reaction_notice.py` (в API они не отдаются). Чтение — `sqlite mode=ro`, два запроса на
комнату (`task_links`, `task_comments` + `tasks.metadata LIKE '%reaction%'`), без сборки деталей —
сводку меню опрашивают часто. Нет доски/таблиц/руководителя — пустые значения, ответ 200:
бейдж не важнее страницы.

### W20-U4.2 «Прочитано» — локально

Браузер хранит `localStorage['teamroom.reactions.<комната>'] = {at, req:{<id>: <время>}}`:

* `req[<id>]` — реакция в этом запросе показана на экране (ставится при отрисовке строки
  «Ответ учтён» в правой колонке запроса);
* `at` — «прочитал всё по комнате до этого времени» (резерв для «прочитать всё»).

«Непрочитанное» = `last_at` реакции больше `max(at, req[id])`. Сервер об отметках не знает и в
доску не пишет; очистка — удалением ключа в браузере или `resetReactionState()` в консоли.

### W20-U4.3 Строка «Ответ учтён: …»

Собирается на клиенте из полей `reaction` (`status`, `label`, `at`) — `views/reaction-notice.js`,
функция `noticeLine`:

| `reaction.status` | Строка |
|---|---|
| `applied` / `no_change` | `Ответ учтён: работа пересмотрена · 13:48`, `Ответ учтён: менять ничего не нужно · 13:52` |
| `received` | `Руководитель прочитал ответ · 13:40` |
| нет исхода, только текст | `Ответ учтён: <текст реакции> · 13:48` |

Повтор «учтено» в подписи снимается (`учтено: работа пересмотрена` → `работа пересмотрена`).
Для шага и решения — «Шаг учтён: …», «Решение учтено: …». Новых полей API не требуется.

### W20-U4.4 Композер: `POST /api/rooms/{id}/requests`

Один селектор проверки критиком, значения уходят в прежние поля:

| Выбор в интерфейсе | Тело запроса |
|---|---|
| «Без критика» | `{critic: false}` |
| «1 раунд — быстро» | `{critic: true, critic_mode: "rounds", max_rounds: 1}` |
| «2 раунда» | `{critic: true, critic_mode: "rounds", max_rounds: 2}` |
| «До полного одобрения» | `{critic: true, critic_mode: "until_approved"}` |

Значение по умолчанию — настройки комнаты (`critic_pass`, `critic_mode`, `max_rounds`): критик не
настроен или проверка в комнате выключена → «Без критика». Черновик запоминает выбор ключом
`criticChoice`; старый булев `critic` в черновике читается как «раунды комнаты / без критика».
Кнопка «Отправить» неактивна на пустом поле (пробелы не считаются) и объясняет причину в `title`;
подсказки аватаров команды — из `participantLabel` («имя — роль»), а не из профиля.

## W20-U3 — горячие клавиши, закрепление, экспорт и режим вида (v1.1.0, WAVE20 §4, P13/P14)

Пакет U3. Три вещи, у которых есть серверная часть, и две — только в браузере:

| Что | Где живёт | Требует сервера |
|---|---|---|
| Закрепление запроса | поле `pinned` реестра комнаты + `PATCH /api/requests/{id}` | да |
| Экспорт свода «В Obsidian» / «HTML» / «Печать» | `POST /api/requests/{id}/export` | да (печать — нет) |
| Горячие клавиши (`J`/`K`, `Ctrl+Enter`, `?`, `V`) | `web/js/hotkeys.js` | нет |
| Режим «простой / подробный» | `web/js/view-mode.js` + `web/css/view-mode.css` | нет |

### W20-U3.1 Закрепление запроса: `PATCH /api/requests/{id} {pinned}`

```
PATCH /api/requests/t_a1b2c3d4
{"pinned": true}          → 200 {"ok": true, "action": "pin", "pinned": true, "pinned_at": 1761700000, "request": RequestSummary}
{"pinned": false}         → 200 {"ok": true, "action": "unpin", "pinned": false, "pinned_at": 0, "request": …}
{"pinned": "true"}        → 200 (строки "1"/"true"/"yes"/"on" понимаются как true)
{"pinned": {"x": 1}}      → 400 {"field": "pinned"}
{}                        → 400 (нужно `title` или `pinned`)
```

Меняется **только реестр комнаты** (`data/state/<комната>/requests.json`): поля `pinned`
(`bool`, по умолчанию `false`) и `pinned_at` (`int`, время закрепления; `0` — не закреплён).
Текст, название, карточка на доске и ответ руководителя не трогаются.

Тот же маршрут по-прежнему переименовывает: `{title: "…"}` → `action: "rename"`
(пустая строка снимает блокировку). Если в теле есть оба поля, сначала ставится закрепление,
потом название; ответ один.

**Где видно закрепление** (все три представления отдают одинаковые поля):

| Ответ | Поле |
|---|---|
| `GET /api/requests/{id}` | `pinned: bool`, `pinned_at: int\|null` (входят в ядро детали, не срезаются `?fields=`) |
| `GET /api/rooms/{id}/requests` | `pinned`, `pinned_at` в каждом элементе; **закреплённые идут первыми**, дальше по `created_at` |
| `GET /api/rooms/summary` → `rooms[].last[]` | те же поля; закреплённые комнаты-запросы выше свежих |

Клиент дополнительно сортирует «Недавнее» по `pinned` (сводка отдаёт до 10 строк на комнату,
а «Недавнее» смешивает комнаты) и показывает у закреплённого значок скрепки.

### W20-U3.2 Экспорт свода: `POST /api/requests/{id}/export {format}`

Один маршрут на три кнопки блока «Свод команды». Форматы: `obsidian`, `html`, `print`.

**`{"format": "obsidian"}`** — файл в зеркальную папку комнаты в хранилище заметок
(то же место, куда приложение ведёт `Запросы/<YYYY-MM-DD> <название> (<id>).md`):

```json
{"ok": true, "format": "obsidian", "path": "…\\Запросы\\2026-11-11 Мука для моти (t_a1b2c3d4).md",
 "folder": "…\\Запросы", "name": "2026-11-11 Мука для моти (t_a1b2c3d4).md",
 "changed": true, "room": "cafe", "request": "t_a1b2c3d4"}
```

* повторный экспорт **обновляет ту же заметку** (`changed: false`, если текст не изменился),
  а дописанное человеком вне служебных маркеров `<!-- teamroom:begin NAME -->` сохраняется;
* в демо (`TEAMROOM_MOCK=1`, `cfg.mock`) и без хранилища — `200 {"ok": false, "skipped": "mock"|"no_vault", "reason": "…по-русски…", "path": ""}`
  (интерфейс покажет причину, а не выдуманный путь);
* нет готового ответа — `409 reason: "synthesis_pending"`.

**`{"format": "html"}`** — автономный файл (`Content-Type: text/html`, `Content-Disposition: attachment`):
тот же рендер, что `GET /api/export/request/{id}.html` (свод, шаги, вопросы, решения, дерево,
встроенный отчёт; ни одной внешней ссылки). Клиент скачивает его blob-ом, не уходя со страницы.

**`{"format": "print"}`** — `200 {"ok": true, "format": "print", "hint": "print_css"}`:
сервер только подтверждает формат, печать собирает браузер по стилю
`web/css/request-export.css` (`@media print`: печатается свод, скрыты правая колонка,
лента, «Ход работы», меню, кнопки и служебные подсказки; фон белый даже в тёмной теме).

Неизвестный/пустой формат — `400` с `allowed: ["obsidian", "html", "print"]`.

### W20-U3.3 Горячие клавиши

`web/js/hotkeys.js`, установка один раз в `app.js` (`installHotkeys`), обработчик `keydown`
на `document`:

| Клавиши | Действие |
|---|---|
| `J` / `K` | следующий / предыдущий вопрос в колонке «Нужен ваш ответ»: прокрутка к центру и фокус в поле ответа; по кругу |
| `Ctrl+Enter` (`Cmd+Enter`) | отправить из поля. Запасной путь: `form.requestSubmit()` или блок с `data-enter-submit="<селектор кнопки>"` |
| `?` (Shift+7) | справка по клавишам |
| `V` | простой / подробный вид |

Правила, чтобы не мешать вводу:

* одиночные клавиши (`J`, `K`, `V`, `?`) **молчат**, если фокус в `input`/`textarea`/`select`
  или в `contenteditable`; в поле `?` — это обычный знак вопроса;
* `Ctrl+Enter` не отправляет дважды: если поле уже обработало клавишу (`event.defaultPrevented`),
  общий обработчик ничего не делает (`submitFromField` возвращает `false`);
* русская раскладка: `J`/`K` работают и как `о`/`л` (те же физические клавиши).

Разметка, на которую опираются клавиши (её ставят `request-aside.js` и другие экраны):
`[data-hotkey-list="questions"]` → `[data-hotkey-item]` → `[data-hotkey-input]`. Новому экрану
достаточно поставить эти атрибуты — `J`/`K` заработают без правок `hotkeys.js`.

### W20-U3.4 Режим «простой / подробный» (P14)

Только браузер: `localStorage['teamroom.viewMode'] = "simple" | "full"`, по умолчанию **`full`**
(вид не меняется у того, кто ничего не нажимал). Применяется атрибутом
`<html data-view-mode="simple">` (+ класс `view-simple`), поэтому переключение мгновенное и не
требует перезагрузки данных.

Что скрывает простой вид (`web/css/view-mode.css`): модель и провайдер участника
(`.agent-model__line`), служебные пометки профиля в шапке запроса (`.req__meta-item[title^="Профиль:"]`),
«подробности для разработчика» (`.req-dev`), внутренние номера и служебные строки —
всё, что помечено `data-tech` или классом `tech-only`.

Переключатель — в подвале бокового меню рядом с темой (`#viewmode-toggle`, класс
`viewmode-toggle`, подпись «просто»/«подробно», `aria-pressed`), он же клавишей `V`.
Смена режима рассылает событие `teamroom:viewmode` — подписи всех переключателей обновляются сами.

## W20-U1 — словарь интерфейса и шапка (v1.1.0, WAVE20 §4, P7/P8)

Пакет U1. **Контракт HTTP+JSON не менялся**: ни одного нового маршрута, ни одного нового поля,
ни одной новой записи в данных. Меняются подписи и раскладка, и вот что об этом нужно знать
тем, кто читает наши файлы и ссылки снаружи.

### W20-U1.1 Один словарь: `web/js/text-ru.js`

| Экспорт | Что это |
|---|---|
| `TERMS` | слова одного смысла: `svod` (свод), `round` (раунд), `critic` (критик), `version` (версия), `phase` (этап). У каждого — `name` (в строке), `title` (заголовок), `hint` (объяснение для «?»), `example` |
| `term(key)` / `termHint(key)` / `termTitle(key)` | безопасный доступ: неизвестный ключ даёт пустые строки, интерфейс не падает |
| `TAB_TITLES` | названия вкладок комнаты — единственный источник: `router.js` строит `TABS` из него, поэтому шапка, крошки (`tabTitle`) и палитра (`palette-model`) не расходятся |
| `tabTitleOf(id)` | название вкладки по id ('' для неизвестной) |
| `SCOPE_LABELS` | `{whole: 'Вся комната', request: 'Этот запрос'}` — слова охвата для шапки, бейджа крошек и чипа фильтра |
| `roundsLabel(n)` / `versionsLabel(n)` | «1 раунд», «2 раунда», «5 раундов» — «раунд» одним словом везде |

Правило: подпись берётся из словаря, а не пишется строкой на месте. Иначе одно и то же снова
начнёт называться по-разному («ответ», «свод команды», «прогон»).

### W20-U1.2 Переименование вкладки: «Дальше» → «От вас нужно»

* Идентификатор остался **`backlog`**: `#/room/<id>/backlog`, `?request=<id>`,
  подпись вкладки в `TABS`, `FILTER_TABS` и маршруты не изменились. Все закладки, ссылки
  из уведомлений и записи в документации продолжают работать.
* Изменились **подписи**: вкладка, крошка (`Комната «…» › От вас нужно`), палитра команд
  («Перейти: От вас нужно»), счётчик (`aria-label`: «От вас нужно, 9 дел…»), чип фильтра
  («Этот запрос ✕»), ссылка в правой колонке запроса.
* **Заголовок раздела в файлах** (то, что читают люди и внешние инструменты):
  `## Дальше` → `## От вас нужно` в `server/export.py` (Markdown-экспорт запроса) и в
  `server/vault_mirror.py` (заметка Obsidian), а также в моке `tools/mock_core.py`.
  Если у вас есть обработчик, который искал строку `## Дальше`, поменяйте его на новую —
  старые заметки в зеркале остаются как есть и не переписываются задним числом.

### W20-U1.3 Подсказка «?»: `web/js/term-help.js`

```js
import { termHelp, termHelpLabel, openTermHelp } from '../term-help.js';
termHelp('svod');          // <button class="term-help" data-term="svod">?</button>
```

* `termHelp(key, {className})` — маленькая кнопка «?»: `title` = объяснение (наведение),
  `aria-label` = «Что такое свод: …» (скринридер), клик открывает окно с объяснением и примером.
* `openTermHelp(key)` — само окно (на общем каркасе `ui.buildDialog`); `null`, если объяснять
  нечего или нет DOM.
* Текст окна — из `TERMS`, то есть правится в одном месте. Где стоит: заголовок блока «Свод»,
  переключатель версий, чип «критик: раунд N» в шапке запроса, строка раундов на вкладке
  «Команда», чип «Этап: …» в шапке.

### W20-U1.4 Шапка: `web/js/topbar-scope.js` (+ правки `app.js`)

| Экспорт | Что делает |
|---|---|
| `phaseLabel(phase)` | `'Этап: Легализация'`; пустая строка, если этапов у комнаты нет |
| `scopeModel({roomId, tab, name, requestId})` | правило охвата: страница запроса → активен «Этот запрос», `whole.href` = `#/room/<id>/composer`; вкладка с `?request=` → активен «Этот запрос»; вкладка без фильтра → активна «Вся комната», второй сегмент не предлагается; вкладки без охвата (`team`, `roadmap`, `composer`) → `null` |
| `scopeSwitcher(model)` | разметка `.topbar__scope.scope-switch` с двумя сегментами, `aria-current="page"` у активного |
| `toolsGroup({navigate, openPalette, onSettings})` | одна группа `.topbar__group--tools`: поиск (`data-tool="search"`), палитра (`palette`, подсказка «Палитра команд · Ctrl K»), настройки приложения (`settings`, `#/settings`) |
| `tabName(id)` | то же, что `tabTitleOf` — для тестов и внешних вызовов |

Из шапки убраны аватары команды и бейдж «вся команда» (дублировали вкладку «Команда»), а также
отдельная кнопка поиска с надписью «Ctrl K» (это сочетание открывает палитру). Правая часть
теперь две группы: комната (папка проекта, настройки комнаты) и инструменты, между ними — черта.

### W20-U1.5 Проверки

* `node --test tests/vocab.test.mjs tests/topbar-scope.test.mjs` — словарь, переименование,
  подсказки, правила охвата, состав группы значков (тесты красные на коде до U1).
* `tools/probe_u1_header.py` (Chromium, локальный мок) — 15 проверок живьём: подписи шапки,
  клик по «?» открывает и закрывает окно, вкладка и крошка называются одинаково, палитра знает
  новое имя.
* `tools/ui_audit.py --widths 1440,1100,900,640 --themes light,dark` — 0 проблем на всех
  комнатах мока (200 экранов), вёрстка шапки на длинных названиях.

## W20-U2 — «Настройки» по группам и карта проекта (v1.1.0, WAVE20 §4, P11/P12)

Пакет интерфейсный: **новых маршрутов и полей API нет**. Экран настроек использует
существующие `GET /api/health` и `GET /api/diagnostics` без изменений ответов; карта
проекта — существующий `GET /api/rooms/{id}/roadmap` и `GET /api/rooms/{id}/backlog`.
Контракт руководителя (`server/contract.py`, `docs/AGENT-GUIDE.md`) не менялся.

### W20-U2.1 Настройки: `web/js/views/settings-groups.js` (чистые правила)

| Экспорт | Что делает |
|---|---|
| `SETTINGS_GROUPS` | четыре группы экрана: `status`, `paths`, `diagnostics`, `prefs`; `settingsGroup(id)` — одна по id |
| `shortenPathLeft(path, maxChars=46)` | сокращение пути **слева**: остаётся хвост (последние папки и файл), начало заменяется «…»; полный путь вьюха кладёт в `title`/`aria-label` |
| `baseName(path)` | последний сегмент пути (для подписей «Содержимое») |
| `CHECK_GROUPS`, `checkGroupId(id)` | темы «Проверки системы»: `program`, `hermes`, `data`, `comfort`; неизвестный id → `other` (не пропадает) |
| `groupChecks(checks)` | `[{id,title,hint,items,worst,counts}]`: внутри группы «сначала проблемы», группы по худшему статусу (группа с `fail` — первой), пустые не возвращаются |
| `checksSummary(checks)`, `worstStatus(items)`, `topProblems(checks, limit=3)` | счётчики, худший статус, до `limit` проблем (`fail` → `warn`) |
| `CHECK_STATUS` | словарь статуса проверки: `label`, `icon`, `chip`, `tone` (его реэкспортирует `diagnostics.js` как `STATUS_LABELS`) |
| `overallStatus(health)` | один сводный статус: `{tone:'ok'|'warn'|'danger'|'muted', icon, title, text, issues[]}` |
| `PREF_LABELS`, `pendingChanges(saved, draft)`, `saveSummary(changes)` | что не сохранено и подпись строки состояния для единственного сохраняющего блока |

Правила `overallStatus`: `engine.schema_ok === false` или `engine.ok === false` → `danger`
«Движок не отвечает»; `dispatcher.state !== 'running'` → `danger` «Движок команды не
запущен»; остановленная фоновая проверка, `data.writable === false` и `warnings[]` →
`warn` «Работает, но есть замечания»; `app.sandbox` → `muted` «Песочница»; иначе `ok`
«Всё работает».

### W20-U2.2 Настройки: экран `web/js/views/settings.js`

Якоря сохранены: `#set-health` (группа «Статус»), `#set-look` (внешний вид и
уведомления), `#set-newroom`; добавлены `#set-paths`, `#set-diagnostics`, у каждой
группы есть `data-group`. Свёрнутый блок техники — `<details class="set-more"
data-role="tech">`. Сохраняющий блок — `[data-action="settings-save"]` и
`[data-action="settings-revert"]`, состояние черновика — `[data-dirty]` у `.set-save`.
Тема в «Настройках» применяется как предпросмотр (`applyTheme(pref, {persist:false})`):
`localStorage` и store темы пишутся только по «Сохранить настройки».

### W20-U2.3 Карта проекта: `web/js/views/roadmap-next.js` (чистые правила)

| Экспорт | Что делает |
|---|---|
| `legendItems()` | подпись статусов этапа (`done/active/blocked/next`) — значок, цвет, слово |
| `nextPhase(roadmap)` | следующий этап: `current` (если не `done`) → `active` → `blocked` → `next`; `null`, когда все готовы |
| `nextSteps(backlog, limit=4)` | открытые шаги (`kind:next_step`, `status:open|asked`) по сроку, без срока — в конце |
| `nextBlock(roadmap, backlog, {limit})` | `{phase, steps, allDone, hasPhases, empty, line, note, detail}` — всё для одного блока «Что дальше» |
| `emptyPlan({modeOn, proposalPending})` | текст и действия пустой карты: `{title, text, primary, secondary, note}`; `secondary = null`, пока предложение этапов ждёт решения (иначе 409) |
| `stepsCountLabel(n)` | «1 шаг», «3 шага», «11 шагов», «шагов нет» |

Карта рендерит **один** заголовок «Что дальше» (в `renderNext()`), этап — кнопка с
`data-phase` и `aria-expanded`; редактор этапов (`phases-editor.js`, W20-FC) по клику на
этап не открывается.

### W20-U2.4 Проверки

* `node --test tests/settings-groups.test.mjs tests/roadmap-next.test.mjs` — правила
  группировки, сокращения пути, сводного статуса, несохранённых изменений, выбора
  следующего этапа, шагов и пустой карты; плюс структурные проверки вьюх (тесты красные
  на коде до U2).
* `node --test tests/diagnostics.test.mjs` — группировка не сломала порядок «падает —
  первым» и отчёт для поддержки.
* `python tools/ui_audit.py --base http://127.0.0.1:8894/ --rooms map,empty
  --widths 1440,1100,900,640 --themes light,dark` → 112 экранов, 0 проблем (комната
  `map` — 12 этапов с названиями 60–80 знаков и путь 120 знаков из песочницы).

## W22-APPROVALS — «Разрешения агентам» (v1.2.0, WAVE22 §2)

Зачем: карточки комнаты — «конечные» запуски (`-q`), в них Hermes блокирует «опасные»
команды сразу, окна разрешения нет (`approvals.single_query_mode: deny`). Участник
упирается в стену и либо бросает работу, либо ищет обход. Здесь у пользователя
появляется то же, что в обычном чате Hermes: **разрешить один раз / разрешать всегда /
запретить**, — но кнопкой в приложении.

Что важно знать сразу:

* приложение находит запросы разрешения **само, чтением** (`state.db` профилей —
  `mode=ro`): блокировки в сессиях карточек комнаты видны без участия агентов;
* выдаётся разрешение **только** штатным механизмом Hermes — списком `command_allowlist`
  в `config.yaml` профиля (подпроцесс `server/approval_grant.py`);
* «один раз» = запись того же списка, которая снимается, когда карточка завершилась или
  через 30 минут (реестр комнаты `data/state/<room>/approvals.json`);
* интерпретатора Hermes нет (чистая машина/CI) → `422`, ничего не пишем.

### W22.1 `GET /api/requests/{id}` → `approvals[]`

```json
"approvals": [
  {"id": "ap_3f7c1a90", "task_id": "t_5b2c1e04", "profile": "ivan-petrov",
   "member_title": "Исполнитель", "kind": "terminal",
   "command": "python - <<'EOF'\nprint(1)\nEOF",
   "description": "запуск скрипта через heredoc", "pattern_key": "script execution via heredoc",
   "floor": false, "grantable": true, "state": "pending", "at": 1763381000,
   "decided_at": null, "count": 2, "card_state": "running", "note": ""}
],
"approvals_pending": 1
```

| Поле | Что это |
|---|---|
| `id` | `ap_<8hex>`; один и тот же (карточка, `pattern_key`, хэш команды) = один запрос. У `execute_code` команда в ключ не входит (код каждый раз другой) — один запрос на карточку |
| `kind` | `terminal` или `execute_code` |
| `command` | команда целиком (≤ 4000 знаков, секреты замаскированы). Для `execute_code` — первые строки кода |
| `description` | по-русски, человеческим языком: «запуск скрипта через heredoc», «выполнение кода python -c», «execute_code — произвольный код» |
| `pattern_key` | ключ записи в `command_allowlist` (что именно разрешается); `null`, если Hermes ключ не отдал |
| `floor` | `true` — жёсткий запрет политики Hermes (например удаление корня): **разрешить нельзя ничем** |
| `grantable` | `false` — разрешить нельзя штатно (`execute_code`: Hermes не сверяет `command_allowlist` в режиме без человека). Действие вернёт `422` |
| `state` | `pending` · `granted_once` · `granted_always` · `denied` · `expired` |
| `count` | сколько раз участник натыкался на эту же блокировку (у строки-группы — сумма по всем её командам) |
| `card_state` | статус карточки в доске (`running`, `done`, `failed`, …) |
| `note` | короткая подпись для интерфейса («разрешение на время этой работы», «ждёт вашего решения») |
| `group` | **необязательное.** `gr_<8hex>` — строка-группа: одинаковые по причине блокировки запросы (W22.4) |
| `approval_ids` | **необязательное.** Все `ap_…` этой группы, свежие первыми; решение по строке закрывает их все |
| `commands[]` | **необязательное.** `{id, command, at, count}` — все различные команды группы, свежие первыми (в `command` — первая из них) |

Счётчики: `approvals_pending` — сколько запросов ждут решения. `needs_you` у запроса
истинно, пока есть `pending` у незавершённых карточек **или** у завершённых, если агент
прямо написал о блокировке в итоге. В `GET /api/rooms` / `GET /api/rooms/summary`
`counters.needs_you` учитывает такие запросы наравне с вопросами и решениями.

Откуда берутся запросы: сообщения `role='tool'` в сессиях карточек комнаты, где
`status: "blocked"` (или текст `BLOCKED:`) и упомянут режим без человека (`-q`,
«single-query»). Связь «карточка ↔ сессия» — как у «Хода работы» (метаданные прогона,
событие `owner_admitted`, первый запрос пользователя с id карточки). Команда — из
предыдущего сообщения ассистента (`tool_calls`). Блокировки жёсткой политики
(`BLOCKED (hardline)`) тоже показываются — с `floor: true`.

### W22.4 Схлопывание одинаковых по причине блокировок (W22-ST)

Разрешение «на вид» (`command_allowlist`) выдаётся на **класс** команд: один `pattern_key`
закрывает все команды этого правила. Поэтому десять разных `python -c …` одного
участника в одной карточке — это один вопрос, а не десять (у `team-local` на живой доске их
было 20 — список превращался в стену).

Сервер отдаёт их **одной строкой** (`collapse`), если совпали карточка, участник и
`pattern_key`, а состояние — `pending`:

```json
{"id": "ap_ab12cd34", "group": "gr_7e51f0aa",
 "approval_ids": ["ap_ab12cd34", "ap_cd34ef56"],
 "command": "python -c \"print(3)\"",
 "commands": [{"id": "ap_ab12cd34", "command": "python -c \"print(3)\"", "at": 1763381200, "count": 1},
              {"id": "ap_cd34ef56", "command": "python -c \"print(2)\"", "at": 1763381000, "count": 2}],
 "count": 3, "state": "pending", "pattern_key": "script execution via -e/-c flag", "...": "…"}
```

Правила:

* **`id` строки** — самый свежий запрос группы; `approval_id` в действии — он же
  (`approval_ids[0]`). Поле `group` в теле действия **необязательное** (`{"action":"approval",
  "approval_id", "decision", "group"?}`): сервер и без него решает всю группу;
* **`command`** — последняя команда (её и видно в строке), остальные — в `commands[]`
  (интерфейс показывает их раскрываемым списком: человек видит ВСЁ, что разрешает);
* **`count`** — сумма блокировок по всем командам группы; «×N» в интерфейсе;
* **решение атомарно**: `once`/`always`/`deny` записывается всем запросам группы сразу
  (`applied: [id…]` в ответе, в реестре — запись на каждый `ap_…`). Если хоть один запрос
  группы уже решён или исчез — `409`, и **ничего** не пишется: половина разрешённой группы
  хуже, чем честный отказ;
* не сливаются: решённые запросы (у каждого своя история), записи без `pattern_key`
  (`floor`: разрешить нельзя, пусть видно каждую команду), разные участники и разные карточки;
* `approvals_pending` и счётчики «ждёт вашего решения» считают **строки** (одно решение —
  один пункт), а не команды.

### W22.2 `POST /api/requests/{id}/actions {action:"approval"}`

```json
{"action": "approval", "approval_id": "ap_3f7c1a90", "decision": "once", "note": "нужно для отчёта"}
```

| `decision` | Что делает сервер |
|---|---|
| `once` | добавляет `pattern_key` в `command_allowlist` профиля, ставит в реестр комнаты запись «до завершения карточки / 30 мин», пишет в карточку `[Михаил · разрешено один раз: <описание>] повтори команду`, перезапускает остановленную карточку |
| `always` | то же, но запись остаётся навсегда; в ответе `grant.scope = "always"` |
| `deny` | состояние `denied`, в карточку `[Михаил · запрещено: <описание>] найди другой способ`, перезапуска нет |

Ответ `200`:

```json
{"ok": true, "approval": {"id": "ap_3f7c1a90", "state": "granted_once", "decided_at": 1763381120, "...": "…"},
 "grant": {"profile": "ivan-petrov", "pattern_key": "script execution via heredoc",
           "description": "запуск скрипта через heredoc", "scope": "once", "expires_at": 1763382920},
 "restarted": true, "message": "Разрешено один раз. Команда будет выполняться до конца этой работы."}
```

`applied` — только при решении строки-группы (W22.4): все `ap_…`, которым записано это
решение. `grant` у `deny` отсутствует. `restarted` — `true`, только если карточка была
остановлена и её удалось вернуть в очередь штатным действием движка (`retry`). Для
завершённой карточки (`done`/`failed`/`archived`) перезапуска нет: разрешение всё равно
запишется и подействует, а `message` попросит повторить работу кнопкой «Доработать».

Ошибки: `404` — нет такого запроса разрешения; `409` — уже решено (двойное нажатие
безопасно); `422` — `floor: true` (жёсткий запрет), `grantable: false` или нет
интерпретатора Hermes (текст по-русски, ничего не записано); `400` — плохое тело.

### W22.3 `GET /api/approvals/grants?room=<id>`

```json
{"items": [{"profile": "ivan-petrov", "member_title": "Исполнитель",
            "pattern_key": "script execution via heredoc",
            "description": "запуск скрипта через heredoc",
            "scope": "always", "granted_at": 1763381120, "expires_at": null,
            "source": "room"}], "room": "r_1", "at": 1763381200}
```

Читает `command_allowlist` профилей участников комнаты (`config.yaml`, `yaml.safe_load`,
только чтение). `source: "room"` — запись выдана из приложения (есть в реестре комнаты),
`"manual"` — стоит в конфиге сама по себе (правил человек или другая сессия Hermes).
`room` без параметра — все комнаты. Ошибка чтения одного профиля не ломает список:
в `warnings[]` будет строка.

### W22.4 `POST /api/approvals/revoke {profile, pattern_key, room?}`

Убирает запись из `command_allowlist` профиля штатным путём Hermes (`load_config` →
изменить список → `save_config`; другие ключи конфига не меняются) и снимает её из
реестра комнаты. Ответ `200 {ok, revoked: {...}, items: [...], message}`; `404` — такой
записи в конфиге нет; `422` — нет интерпретатора Hermes; `400` — нет профиля/ключа.
В журнал комнаты (`journal.jsonl`) пишется строка «разрешено/отозвано, кем, что, когда».

### W22.5 Что НЕ делает сервер

* не правит `config.yaml` руками (никакого `sed` и YAML-текста): только
  `tools.approval.save_permanent_allowlist` / `hermes_cli.config.save_config`;
* не обходит жёсткие запреты Hermes (`approval_floors`) — такие блокировки показываются
  как «разрешить нельзя»;
* не выдаёт разрешения сам: только по действию пользователя;
* в песочнице (`TEAMROOM_HERMES_HOME`) пишет **не** в реальные профили, а в поддельный
  домашний каталог `<data>/hermes-home` — демо-сервер на живых конфигах не тренируется.

### W22.6 Проверки

* `python -m unittest tests.test_approvals -v` — поиск блокировок в поддельном
  `state.db`, идемпотентность (`count`), «разово/всегда/запретить», запись и отзыв в
  поддельном `HERMES_HOME`, жёсткий запрет (`floor`), `execute_code` (`grantable:false`),
  снятие «разового» разрешения по завершении карточки и по сроку, сохранность остальных
  ключей конфига, отсутствие интерпретатора → отказ без записи.
* Реальная сверка с Hermes (только на поддельном профиле): после «разрешать всегда»
  `tools.approval.check_all_command_guards(<команда>, "local")` в режиме
  `single_query_mode: deny` возвращает `approved: true`, после отзыва — снова `false`.
  Тест ищет интерпретатор Hermes через `%LOCALAPPDATA%`, а без него честно пропускается.

## W26-FOUND — чипы-ссылки и блок «Найдено» (WAVE26 §2 D4)

Одно правило оформления объекта-ссылки во всём интерфейсе (свод, чат, «Ход работы»,
файлы, справки, экспорт): локальный файл — имя файла, которое открывает файл, плюс
«Показать в папке» и «Копировать путь»; адрес — название, которое открывает сайт, плюс
«Копировать ссылку». Реализация — `web/js/found.js` (разметка и действия),
`web/js/svod-format.js` (пути в тексте), `web/js/md.js` + `md-inline.js` (рендер),
`web/css/found.css` (вид в обеих темах).

### W26-FOUND.1 `GET /api/files/info?path=<путь>` — есть ли файл

Нужен чипам: прежде чем рисовать «Открыть», приложение проверяет существование и
показывает серую «не найден» вместо ложной кнопки.

```json
{"ok": true, "exists": true, "path": "C:/Работа/Планы/Годовой план.md",
 "name": "Годовой план.md", "size": 18432, "ext": ".md"}
```

* `exists: false` — путь внутри разрешённых каталогов, но файла нет (чип становится
  серым «не найден», действия убираются);
* `exists: null` — путь **вне** разрешённых каталогов: существование не подтверждаем и
  не отрицаем, чип остаётся обычным (`reason: "outside_roots"`);
* `400` — нет `path`. Содержимое файла маршрут не отдаёт — только имя, размер и тип.
* Журналы (`data/logs/*.log`) — `403`, как и у `/api/file` (в них бывают секреты).

Действия чипа: «Открыть» и «Показать в папке» — `POST /api/files/launch {path, room?, mode}`
(W26-D3, `mode: "open"|"reveal"`); «Копировать путь/ссылку» — в браузере
(`navigator.clipboard` с запасным способом). Если маршрута `launch` в сборке нет —
«Показать в папке» уходит в прежний `POST /api/open`, «Открыть» — в `/api/file`
(кнопка не остаётся мёртвой).

### W26-FOUND.2 Машинный блок «Найдено» в своде

Поисковая задача обязана содержать раздел `## Найдено` с блоком ```` ```найдено ````:
YAML-список (или JSON-массив) записей `{название, тип, где, описание, отношение, комментарий}`.

| Поле | Значения | Обязательное |
|---|---|---|
| `название` | строка | да |
| `тип` | `файл` \| `сайт` \| `письмо` \| `запись` | нет (угадывается по `где`) |
| `где` | путь или адрес объекта | да |
| `описание` | 1–2 строки, что это и что отвечает | нет |
| `отношение` | `искомое` \| `упоминает` \| `смежное` \| `не то` | нет (`смежное`) |
| `комментарий` | где/в каком смысле упомянуто | нет |

Вместо массива допустима обёртка `{искали, где искали, найдено: [...]}` — она даёт
«что и где искали» для честной плашки. Ключи понимаются и по-английски
(`title, type, where, description, relation, comment`).

Рендер (`renderFoundBlock`): заголовок «Найдено» со счётчиком, группы «Нашли то, что
искали (N)», «Нашли упоминания (N)», «Нашли смежное (N)», «Похожее, но не то (N)»;
пустая группа не рисуется. Если записей с `отношение: искомое` нет — сверху плашка
«Искомого нет; нашли смежное». Действия по типу: файл — «Открыть · Показать в папке ·
Копировать путь»; сайт — «Перейти на сайт · Копировать ссылку»; письмо — «Открыть письмо»;
запись — «Открыть в CRM». Тот же блок питает панель «Файлы», реестр источников и экспорт.

Запасной разбор: если раздела `## Найдено` нет в машинном виде, но он написан обычным
текстом, приложение вытаскивает из него пути и адреса и рисует те же карточки с меткой
«разобрано из текста» — найденное не теряется.

В экспорте (`server/export.py`) блок превращается в тот же список с группами и метками
(HTML-экспорт рисует карточки тем же `md.js` + `found.css`). Правило для агентов —
`contract.summary_format_rule()` и `docs/AGENT-GUIDE.md` §«Найдено».

## W26-DEADENDS — «Без тупиков»: недоговорные блокировки, автозакрытие, действия (v1.3.0, WAVE26 §2 D1)

Правило Михаила (`Requirements-Mikhail.md`): всё, что показано как «ждёт вашего решения»,
обязано иметь действие, которое реально что-то меняет. На скриншоте 8777 было иначе:
`team-local хочет выполнить команду · execute_code · блокировалось 2 раза · ждёт вашего
решения` — и **ни одной кнопки**, причём карточка уже `done`. Разбор — Vault
`Prep-2026-11-18-dead-ends-rail-duties.md` §1 (дефекты T1–T3).

### W26.1 Три вида блокировки (`mode`) и состояния (`state`)

| `mode` | Когда | `state` по умолчанию | Кнопки (`actions[]`) |
|---|---|---|---|
| `grant` | разрешить можно штатно (`command_allowlist`) | `pending` | `once` · `always` · `deny` |
| `workaround` | нельзя штатно (`execute_code`: Hermes не сверяет список разрешений в `-q`) | `workaround` | `hint_agent` · `trust_profile` (со сроком) · `dismiss` |
| `floor` | жёсткий запрет политики Hermes (`BLOCKED (hardline)`) | `info` | `hint_agent` · `dismiss` («Понятно, скрыть») |

**`pending` — только `grant`.** У недоговорной блокировки и жёсткого запрета состояния
«ждёт вашего решения» больше нет: это статус/информация, а не вопрос к человеку.

Состояния: `pending` · `granted_once` · `granted_always` · `denied` · `expired` ·
`workaround` · `hinted` · `info` · `closed` · `dismissed` · `trusted`.

| Поле строки `approvals[]` | Что это |
|---|---|
| `mode` | `grant` · `workaround` · `floor` |
| `mode_label` | по-русски: «можно разрешить» / «нельзя разрешить штатно — есть другой способ» / «жёсткий запрет Hermes — только информация» |
| `actions[]` | `{id, label, kind}` — кнопки строки; у `trust_profile` есть `ttl_required: true` и `ttl_options[]` (`30` / `card` / `forever`). **Пустой список = пункт не «ждёт вас»** (история) |
| `actionable` | `true` — у строки есть чем ответить |
| `closed_reason` | `card_finished` — карточка завершилась, строка закрыта автоматически (T2) |

Счётчики: `approvals_pending` и `needs_you` считают **только** строки `state="pending"`
с непустым `actions[]`. Новое поле детали: `approvals_actionable` — сколько строк вообще
можно чем-то закрыть (для сворачиваемого блока «нельзя разрешить штатно» в интерфейсе).

### W26.2 Автозакрытие (T2)

Строка в состоянии «ещё не решено» (`pending`/`workaround`/`info`) у карточки в
`done`/`archived`/`failed`/`cancelled` отдаётся как `state: "closed"`,
`closed_reason: "card_finished"`, `actions: []`, подпись «работа завершена — закрыто
автоматически; если блокировка ещё нужна, повторите работу кнопкой «Доработать»».

Это чтение (`collect`), а не запись: состояние пересчитывается на каждом запросе,
поэтому «зависших» `pending` у завершённых карточек не остаётся даже в старых реестрах.
Решение человека (`denied`/`granted_*`/`dismissed`/`hinted`) автозакрытие не перетирает.

### W26.3 `POST /api/requests/{id}/actions {action:"approval"}` — новые `decision`

```json
{"action": "approval", "approval_id": "ap_3f7c1a90", "decision": "hint_agent"}
{"action": "approval", "approval_id": "ap_3f7c1a90", "decision": "trust_profile", "ttl": "card"}
{"action": "approval", "approval_id": "ap_3f7c1a90", "decision": "trust_profile", "ttl_min": 30}
{"action": "approval", "approval_id": "ap_3f7c1a90", "decision": "dismiss", "note": "уже не нужно"}
```

| `decision` | Что делает сервер | Ответ |
|---|---|---|
| `hint_agent` | кладёт в ветку карточки штатным действием движка (`comment`, от лица Михаила) текст «execute_code недоступен в фоновой работе: сделай через файл и терминал — запиши скрипт файлом (`write_file`) и запусти `python файл.py`…». Разрешений не выдаёт, в профиль не пишет | `approval.state = "hinted"`; без канала записи → `422 no_channel` |
| `trust_profile` | пишет `approvals.single_query_mode: approve` **только** в профиль участника (`hermes_cli.config.load_config` → `save_config`), **только** по этому действию и **только** с названным сроком; запись реестра комнаты позволяет отозвать | `approval.state = "trusted"`, `trust: {profile, scope, ttl_min, expires_at, previous, label}` |
| `dismiss` | закрывает пункт как неактуальное (работает и для `pending`: «отклонить без запрета») | `approval.state = "dismissed"` |

Срок обязателен: `ttl: "card"` (до конца карточки — снимается сторожем, когда карточка
завершилась), `"forever"` или `ttl_min` (1…1440). Без срока → `400 ttl_required`,
в профиль ничего не пишется.

Почему `trust_profile` отдельным действием: `single_query_mode: approve` снимает защиту
со **всех** «опасных» команд профиля в конечных запусках (жёсткие запреты Hermes —
`approval_floors` — остаются). Поэтому: ясное предупреждение в интерфейсе, отдельный
срок, отзыв и запись только в присланный профиль (в песочнице — поддельный
`<data>/hermes-home`).

Ошибки: `404` — нет такого пункта; `409 already_decided` — уже решён/закрыт;
`422 floor` — доверие не снимает жёсткий запрет; `422 grantable` — эта блокировка
разрешается обычным способом, доверие не нужно; `422 no_hermes` — нет интерпретатора
Hermes; `400` — плохое тело/срок.

Регресс: `decision: "once"|"always"` на недоговорной блокировке по-прежнему `422
not_grantable` — но в `detail.actions` теперь приходят `["hint_agent", "trust_profile",
"dismiss"]`, а текст перечисляет кнопки (раньше человек видел только «нельзя»).

### W26.4 Доверие профилю: `GET /api/approvals/trusts`, `POST /api/approvals/untrust`

```json
GET /api/approvals/trusts?room=cafe
{"items": [{"profile": "team-ops", "member_title": "Операции", "scope": "minutes",
            "ttl_min": 30, "expires_at": 1763382920, "granted_at": 1763381120,
            "card_title": "Смета упаковки", "label": "30 мин.", "room": "cafe"}],
 "room": "cafe", "at": 1763381200}

POST /api/approvals/untrust {"profile": "team-ops", "room": "cafe"}
{"ok": true, "revoked": {"profile": "team-ops", "previous": "deny"},
 "message": "Доверие отозвано: профиль «team-ops» снова спрашивает разрешения.",
 "items": []}
```

Отзыв возвращает прежнее `single_query_mode` (или убирает ключ, если его не было).
Сторож (`approvals.sweep`) снимает доверие сам: по сроку (`ttl_min`) и по завершении
карточки (`ttl: "card"`). В ответе `sweep` появились `trusts_revoked`/`closed`.

### W26.5 `POST /api/approvals/tidy {room, request_id, delay?}` — способ A без человека

Подсказка участнику (та же `hint_agent`) для всех недоговорных блокировок запроса:
`{"ok": true, "hinted": 1, "ids": ["ap_…"], "failed": 0, "message": "Подсказано участникам: 1."}`.
`delay` — сколько секунд блокировка должна провисеть (интерфейс зовёт с `120`; в
предложении было «сама через 2 минуты»). Разрешений не выдаёт, в профили не пишет.
Хук в сторож (`watcher.py`) не ставился: файл принадлежит другой волне — вызов
оставлен маршрутом (и `approvals.auto_hint` для кода).

### W26.6 `tools/check_dead_ends.py` — сторожевой обход

```bash
python tools/check_dead_ends.py --base http://127.0.0.1:8851          # живой сервер
python tools/check_dead_ends.py --base http://127.0.0.1:8851 --json dead-ends.json --quiet
python tools/check_dead_ends.py --items approvals.json                # офлайн-массив
python tools/check_dead_ends.py --selftest                            # проверка проверки
```

Обходит все комнаты → запросы с `needs_you` → детали, и проверяет:

1. у каждой строки `pending`/`workaround`/`info` есть непустой `actions[]`;
2. нет `pending` у карточки в `done`/`archived`/`failed`/`cancelled`;
3. жёсткий запрет и недоговорная блокировка не показаны как «ждёт вашего решения»;
4. надпись «ждёт вашего решения» не стоит у строки без кнопок;
5. `approvals_pending` не больше числа строк с действием (иначе счётчик считает
   недоговорное); «нужен ваш ответ» не висит над запросом, где решать нечего;
6. пункты `questions`/`decisions`/`next_steps` с явным пустым `actions[]` — тупик.

Код возврата: `0` — тупиков нет, `1` — есть, `2` — сервер/файл не прочитан.
Публично: `check_approval`, `check_items`, `check_detail`, `walk`, `selftest`, `main`.

### W26.7 Что НЕ делает D1 (границы волны)

* не отправляет письма и ничего не пишет во внешние системы (это гейт человека);
* не трогает набор инструментов профиля (способ C из разбора — «убрать `execute_code`
  у рабочих профилей» — решение Михаила, отдельная волна);
* не ставит хук автоподсказки в `watcher.py` (файл другой волны): маршрут `tidy` +
  `auto_hint` готовы, включение — одна строка у владельца файла;
* интерфейс (три вида строк, сворачивание колонки, единый стор) — пакет D2.

### W26.8 Проверки

* `python -m unittest tests.test_dead_ends -v` — 29 тестов: `execute_code`/`floor` не
  `pending` и не в счётчиках; у каждой строки есть действие; автозакрытие
  `done/archived/failed/cancelled` (и сохранение решения человека); `hint_agent` пишет
  комментарий с точной формулировкой и честно отказывает без канала;
  `trust_profile` без срока → `400`, пишет только в профиль участника, отзывается,
  истекает по сроку и по концу карточки; `dismiss` идемпотентен (`409` на повтор);
  `check_dead_ends.selftest` и ловля старого тупика; счётчик «Сегодня».
* `python -m unittest tests.test_approvals -v` — прежние 36 тестов приведены к новому
  правилу (у завершённой карточки блокировка закрыта, а не «ждёт»).

## W23-MODEL-GUARD — «Контроль моделей» (v1.2.0, WAVE23 §2)

Жалоба 17.11: исполнитель `team-local` (локальная Qwen, бесплатно) выполнил карточку на
`anthropic/claude-sonnet-5-5` — $2,45 за один запуск. Причина: приложение клеило **модель
руководителя** на корневую карточку в момент создания (исполнитель ещё неизвестен,
`assignee=null`, статус `triage`), а когда Hermes назначал исполнителя, переопределение
оставалось. Сверки «модель карточки ↔ ожидаемая модель исполнителя» не было нигде.

Правило волны: **запрос уходит только на ту модель, которая ожидается**, и если он
собирается уйти не туда — приложение исправляет это **до** запуска, а запущенное на
чужой модели **останавливает и перезапускает** на верной. Предупреждений «проверьте»
по умолчанию больше нет.

Модуль: `server/model_guard.py`. Настройка комнаты: `model_guard` = `enforce` (по
умолчанию) | `warn`. Состояние: `data/state/<room>/model_guard.json`, журнал комнаты —
`journal.jsonl`.

### W23.1 Единый источник ожидаемой модели

```python
expected_model(cfg, room, profile, card_id=None) -> {
    "provider": str, "model": str, "reasoning": str,
    "source": "user" | "room" | "profile" | "none",
    "base_url": str,            # только если задан в конфиге (локальный провайдер)
    "label": str,               # «провайдер · модель · мышление» для текстов
}
```

Порядок (первый непустой побеждает — как в §2.1 WAVE23):

1. **`source: "user"`** — модель, которую человек выбрал **для этой карточки** действием
   `card_model` (W23.8). Выбор пользователя сильнее настроек комнаты: сторож его не
   «исправляет».
2. **`source: "room"`** — `room.agent_models[profile]` (переопределение комнаты), если
   пользователь сам его задал.
3. **`source: "profile"`** — пара `provider`+`model` из `config.yaml` профиля Hermes
   (`default` → общий `config.yaml`), правило W20 §1.5 (профиль → общие настройки).
4. **`source: "none"`** — модель определить нельзя. Сторож **ничего не останавливает и не
   правит**, только пишет предупреждение `unknown`: из-за собственной слепоты работу не
   убиваем.

`override_for_card(room, profile)` — что писать в карточку при создании:
`model_override`/`provider_override` ставятся **только** при `source:"room"`; иначе пусто
(`NULL`) — Hermes берёт модель профиля, и это и есть «модель в настройках агента».

**Сравнение моделей** (`same_model(expected, actual)`) — с нормализацией, но без угадывания:

* регистр и лишние пробелы не важны (`Qwen36` = `qwen36`);
* `provider/model` в поле модели равно паре `provider` + `model`;
* известные алиасы (`ALIASES` в модуле) и суффикс `:latest` — то же самое;
* **похожесть — не равенство**: `qwen36` ≠ `qwen36v`, `deepseek-v4-pro` ≠
  `deepseek-v4-pro:0813`, `glm-5.3` ≠ `glm-5.3-flash` (те же грабли, из-за которых работа
  уходила «на почти ту» модель, W10-FIX);
* провайдер сравнивается, если он известен с обеих сторон (`billing_provider` в сессии
  бывает пустым — тогда это не расхождение); `anthropic` ≠ `llamacpp` при одинаковом
  имени модели — расхождение (та же модель через платного провайдера = те же деньги).

### W23.2 Создание карточки (единственный путь)

Любое создание карточки приложением идёт через `engine_write._model_args` →
`model_guard.override_for_card`:

| что создаётся | `assignee` | переопределение модели |
|---|---|---|
| корневая карточка запроса (`--triage`) | неизвестен | **не ставится никогда** |
| карточка-ребёнок (критик, доработка, подзадача) | известен | только выбор комнаты (`source:"room"`) |
| направление карты проекта (под-запрос) | неизвестен до назначения | **не ставится** |

Модель профиля в карточку не переносится: она и так действует (движок читает
`config.yaml` профиля), а «пин» навсегда перекрыл бы будущую смену настроек агента.
Уровень мышления (`reasoning_effort`) по-прежнему ставит приложение через мост — только
когда он выбран в комнате.

### W23.3 Быстрый сторож (`server/watcher.py`, свой цикл ≤ 3 с)

Отдельный поток наблюдателя (не общий тик `TICK_ACTIVE`): раз в `GUARD_TICK = 3` с по
каждой неархивной комнате, только карточки `tenant LIKE 'room:%'`. Чужие доски и карточки
других tenant не трогаются **никогда**.

| состояние карточки | что делает сторож |
|---|---|
| `triage, todo, ready, blocked, queued, scheduled` (не запущена) | сверяет `model_override/provider_override` с `expected_model`. Расхождение → **немедленно** правит штатной функцией движка (мост `tools/kb_bridge.py`, та же, что закрепляет модель участника), событие `fixed` |
| `running` и есть `owner_admitted` с `session_id` | читает `sessions.model` + `billing_provider` (`state.db` профиля, `mode=ro`). Не та модель → **немедленно** останавливает (`reclaim`), ставит верное переопределение, возвращает в очередь (`ready`), событие `stopped` со стоимостью на момент остановки |
| модель определить нельзя (`source:"none"`) | ничего не останавливает, событие `unknown` (один раз на карточку в сутки) |
| `running` без `session_id` (воркер ещё поднимается) | ждёт: `unknown`, ничего не делает |
| `review, done, archived` | не трогает |

Ограничения (защита от петли и от убийства нормальной работы):

* не более **2** автоперезапусков на карточку (`GUARD_MAX_RESTARTS`). Исчерпала —
  карточка остаётся остановленной (`blocked`) с пометкой «модель не совпадает, нужно
  решение» и сообщением в ветку запроса; дальше её ведёт человек;
* карточку моложе `GUARD_MIN_AGE = 15` с не трогаем: воркер мог ещё не записать сессию;
* карточка, остановленная **человеком** (`card_stop`, W23.8), сторожем не поднимается;
* `mode = "warn"` (настройка комнаты) — сторож только пишет события `warned`, ничего не
  останавливает и не правит;
* ошибка моста/БД не валит цикл: она уходит в `data/logs/app.log` и в
  `/api/health.guard.last_error`.

Состояние (`data/state/<room>/model_guard.json`):

```json
{"updated_at": 1763381200,
 "restarts": {"t_375d5a5f": {"count": 1, "at": 1763380000, "model": "claude-sonnet-5-5"}},
 "user_model": {"t_375d5a5f": {"provider": "llamacpp", "model": "qwen36v", "at": 1763381000}},
 "stopped": {"t_375d5a5f": {"by": "user", "at": 1763381200, "reason": "остановлено пользователем"}},
 "unknown": {"t_11111111": 1763381200},
 "events": [{"at": 1763380000, "kind": "stopped", "card_id": "t_375d5a5f",
             "root_id": "t_aaaaaaaa", "profile": "team-local",
             "expected": {"provider": "llamacpp", "model": "qwen36v"},
             "actual": {"provider": "anthropic", "model": "claude-sonnet-5-5"},
             "cost_usd": 2.45,
             "text": "Запуск шёл на claude-sonnet-5-5 вместо локальной qwen36v — остановлено и перезапущено"}]}
```

`events` — кольцо последних 200 событий (журнал комнаты остаётся полным).

### W23.4 `GET /api/rooms/{id}/model-check` — «куда собирается уйти запрос»

Проверка по кнопке и для диагностики: ожидаемая модель участника, переопределение карточки
и факт последнего запуска из `sessions`. `guard.open` — сколько карточек комнаты прямо
сейчас расходятся (это же число рисуется рядом с кнопкой «Проверить сейчас»).

```json
{"room": "fn", "at": 1763381200,
 "members": [
   {"profile": "team-local", "title": "Исполнитель на локальной модели",
    "expected": {"provider": "llamacpp", "model": "qwen36v", "reasoning": "",
                 "source": "profile", "base_url": "http://127.0.0.1:8090/v1"},
    "card_model": {"provider": "anthropic", "model": "claude-sonnet-5-5"},
    "last_run": {"provider": "anthropic", "model": "claude-sonnet-5-5",
                 "at": 1763378000, "cost_usd": 2.45, "card_id": "t_375d5a5f"},
    "ok": false,
    "problem": "team-local: ожидалась qwen36v (llamacpp), запущено claude-sonnet-5-5 (anthropic)"},
   {"profile": "anna", "title": "Анна — руководитель",
    "expected": {"provider": "anthropic", "model": "claude-sonnet-5-5", "reasoning": "high",
                 "source": "room"},
    "card_model": null, "last_run": null, "ok": true, "problem": null}],
 "guard": {"mode": "enforce", "open": 1, "fixed_24h": 3, "stopped_24h": 1,
           "saved_usd_est": 2.45,
           "text": "Строгий контроль: 3 исправления и 1 остановка за сутки, сэкономлено ≈ $2,45"},
 "warnings": []}
```

* `members[]` — по каждому участнику комнаты (руководитель, критик, исполнители);
  `title` — «Имя — роль» по подписям комнаты (как в «Команде»);
* `expected.source` — как в W23.1 (`user|room|profile|none`);
* `card_model` — что записано в карточках участника, `null` — переопределения нет
  (карточка пойдёт на модель профиля);
* `last_run` — факт последнего запуска участника из `sessions` (`mode=ro`), `null` — не
  запускался или `state.db` недоступен; `cost_usd` — `estimated_cost_usd` сессии;
* `ok=false` + `problem` — по-русски: «ожидалась X, запущено Y» (без id и служебных слов);
  `ok=false` бывает и тогда, когда в карточке записана не та модель (карточка уйдёт не туда);
* `ok=null` — модель участника определить нельзя (в профиле и в комнате не задана):
  контроль не вмешивается, `problem` объясняет почему — интерфейс рисует нейтральный статус;
* `guard.saved_usd_est` — сумма `estimated_cost_usd` остановленных запусков за сутки
  (оценка «сколько не сгорело»), округление до цента;
* `warnings[]` — что не удалось прочитать (нет `state.db`, нет конфига профиля).

`404` — нет комнаты. `200` и в `warn`-режиме: `guard.mode` показывает режим.

### W23.5 `model_guard[]` в деталях запроса

`GET /api/requests/{id}` получает поле `model_guard` (массив, новые первыми, ≤ 50):

```json
"model_guard": [
  {"at": 1763380000, "kind": "stopped", "card_id": "t_375d5a5f", "profile": "team-local",
   "expected": {"provider": "llamacpp", "model": "qwen36v"},
   "actual": {"provider": "anthropic", "model": "claude-sonnet-5-5"},
   "cost_usd": 2.45,
   "text": "Запуск шёл на claude-sonnet-5-5 вместо локальной qwen36v — остановлено и перезапущено на qwen36v"},
  {"at": 1763379000, "kind": "fixed", "card_id": "t_375d5a5f", "profile": "team-local",
   "expected": {"provider": "llamacpp", "model": "qwen36v"},
   "actual": {"provider": "anthropic", "model": "claude-sonnet-5-5"}, "cost_usd": null,
   "text": "Модель карточки исправлена до запуска: claude-sonnet-5-5 → модель профиля (qwen36v)"}]
```

Виды (`kind`): `fixed` (исправлено до запуска), `stopped` (остановлено и перезапущено),
`unknown` (модель определить нельзя — предупреждение), `warned` (расхождение найдено, но
комната в режиме «только предупреждать»), `stopped_by_user` / `resumed` / `model_changed`
(действия человека, W23.8). **`text` — готовый русский текст**: интерфейс показывает его
как есть и не собирает фразы сам. Неизвестный вид клиент рисует нейтрально (по `text`).

Поле есть всегда (пустой массив, если вмешательств не было). Дополнительно доступно имя
`?fields=model_guard` — только этот блок сверх ядра.

### W23.6 Пункт диагностики «Контроль моделей»

`GET /api/diagnostics[?room=]` получает пункт:

```json
{"id": "model_guard", "title": "Контроль моделей",
 "status": "ok|warn|fail|off",
 "value": "Строгий контроль: 3 исправления и 1 остановка за сутки",
 "hint": "Запрос уходит только на модель из настроек агента — расхождения исправляются до запуска",
 "fix": null,
 "details": ["team-local: ожидалась qwen36v (llamacpp), запущено claude-sonnet-5-5"]}
```

`status`: `ok` — расхождений нет; `warn` — есть карточки с расхождением или режим `warn`;
`off` — проверять нечего (нет доски/комнат); `fail` — сторож падал (последняя ошибка в
`details`). Пункт есть и без `?room=` (сводка по всем комнатам).

### W23.7 Настройка комнаты `model_guard`

`PATCH /api/rooms/{id} {"model_guard": "enforce"|"warn"}`. В карточке комнаты:

```json
"model_guard": "enforce",
"model_guard_label": "Строго: неверную модель исправляю сам",
"model_guard_block": {"mode": "enforce", "on": true,
                      "label": "Строго: неверную модель исправляю сам",
                      "hint": "Расхождение исправляется до запуска; запущенное на чужой модели останавливается и перезапускается",
                      "modes": [{"id": "enforce", "label": "Строго: неверную модель исправляю сам"},
                                {"id": "warn", "label": "Только предупреждать — решаю я"}]}
```

Старые комнаты без поля — `enforce` (требование пользователя 17.11: жёсткий контроль по
умолчанию). Другие значения → `400 bad_request` (`detail.field = "model_guard"`).

### W23.8.1 Метка `stopped` в узле дерева (W23-ST)

Узел `tree[]` (`RequestDetail`, §6.11) получает необязательное поле `stopped`, если карточку
остановил **человек** и она ещё не продолжена:

```json
"stopped": {"by": "user", "at": 1793169060, "model": "qwen36v", "provider": "llamacpp",
            "cost_usd": 0.37}
```

* `by` — `"user"` (остановку сторожа в узел не кладём: это не решение человека, о ней
  рассказывает лента `model_guard[]`);
* `at` — когда остановили (unix-время, `0` — неизвестно);
* `model`/`provider` — модель, на которой шла работа на момент остановки (`""`, если прогона
  не было: карточка стояла в очереди);
* `cost_usd` — стоимость этого прогона на момент остановки (`number|null`, `null` — считать
  нечем: нет подтверждённой сессии с ценой).

Источник — реестр сторожа `data/state/<room>/model_guard.json: stopped[<task_id>]`; поле
снимается, когда карточку продолжают (`card_resume`). Зачем: `status: "blocked"` бывает и у
карточки, ждущей зависимостей, поэтому интерфейс по одной метке отличает «остановлено вами»
(строка «Остановлено вами в 10:41 · модель: qwen36v» + кнопка «Продолжить») от обычной
блокировки. У старых записей реестра (без модели и стоимости) `model` — `""`, `cost_usd` — `null`:
подпись будет короче, но остановка всё равно видна.

### W23.8 Управление карточкой: остановить / продолжить / сменить модель

Те же три действия, что кнопки остановки в самом Hermes. Работают **только** для карточек
комнат (`tenant = room:<эта комната>`) и **только в статусах** `running`, `ready`,
`blocked` — то есть пока работа идёт или вот-вот пойдёт.

```
POST /api/requests/{id}/actions
  {"action": "card_stop",   "task_id": "t_375d5a5f", "note": "сначала уточню задачу"}
  {"action": "card_resume", "task_id": "t_375d5a5f", "note": "продолжай"}
  {"action": "card_model",  "task_id": "t_375d5a5f", "model": "qwen36v", "provider": "llamacpp"}
  {"action": "card_model",  "task_id": "t_375d5a5f", "model": "none"}
```

Ответ `200`:

```json
{"ok": true, "action": "card_model", "task_id": "t_375d5a5f", "status": "ready",
 "model": {"provider": "llamacpp", "model": "qwen36v", "source": "user"},
 "message": "Модель карточки: qwen36v (llamacpp). Работа перезапущена на ней.",
 "detail": {"stopped": true, "resumed": true, "queue": "ready"}}
```

**`card_stop`** — остановить работу, **не теряя прогресс**: рабочая папка, коммиты и
комментарии остаются, воркер завершается. Двумя вызовами CLI останавливать нельзя (проверено
по `hermes_cli/kanban_db.py`):

1. `reclaim_task` — **единственный** глагол, который завершает процесс воркера
   (`_terminate_reclaimed_worker`), но карточку он возвращает в очередь (`ready`/`todo`), и
   диспетчер подхватывает её на следующем тике (30 с), а если слот освободился — сразу;
2. `block_task` — удерживает карточку (`blocked`, `claim_lock`/`worker_pid` в NULL), но
   процесс воркера **не убивает**.

Поэтому остановка — **одна операция в одном процессе интерпретатора Hermes**
(`server/kanban_atomic.py`, W23-AT1): `reclaim_task` и сразу за ним `block_task` — одно
соединение, без промежуточного состояния `ready`: зазор между шагами — микросекунды (замер на
поддельной доске: 13–19 мкс против 1,4–2,0 с у второго вызова CLI), а `block` держит блокировку
записи всё время своей работы, так что захватить карточку в этом окне практически нельзя. Затем результат проверяется по доске (статус `blocked` и «pid
воркера больше не жив» — `_worker_alive` с отпечатком запуска) и при неудаче действие
повторяется (до 3 раз). Если интерпретатора Hermes нет (чистая машина), идёт прежний путь
«CLI + проверка + повтор»: медленнее, результат тот же; в ответе это видно по
`detail.path` (`"atomic"` — один процесс, `"cli"` — последовательность команд).

Тип блокировки — `needs_input`; при повторных остановках он **чередуется с «без типа»**, чтобы
счётчик петель Hermes (`BLOCK_RECURRENCE_LIMIT = 2`) не принял решение человека за цикл
воркера и не увёл карточку в `triage`: «остановил → продолжил → остановил» остаётся
остановкой, а не исчезновением карточки из очереди.

3. метка `stopped[task_id] = {by: "user", at, reason, model, provider, cost_usd}` — сторож
   моделей такую карточку не поднимает (проверено двумя тиками подряд), а сообщение
   пользователя по запросу её возобновляет (ниже). В метку попадает то, что было в работе на
   момент остановки (модель и стоимость прогона), — снимается ДО `reclaim`, пока открытый
   прогон ещё есть.

Если удержать карточку не удалось, `block` не сработал или воркер не завершился (например,
claim принадлежит другому хосту — чужой pid Hermes не сигналит), ответ честный: `200`,
`detail.paused: false` (или `detail.worker_alive: true`) и текст «работа снята, карточка снова
в очереди» / «процесс воркера ещё жив — нажмите ещё раз». Молчаливого «остановлено» над живым
воркером больше нет.

**`card_resume`** — вернуть в очередь: `unblock <id> --reason "продолжаем"`, для `todo`
(`unblock` вернул в ожидание родителей или карточка и так в `todo`) — `promote <id>`.
Обе штатные функции вызываются подряд в одном процессе (`server/kanban_atomic.py`), поэтому
между снятием паузы и постановкой в очередь нет окна, в котором диспетчер видит карточку
`todo` без причины.
Воркер стартует заново с контекстом карточки: тело, история комментариев (включая
последний комментарий пользователя) и рабочая папка на месте. Метка `stopped` снимается,
в журнал — «карточка продолжена». `409`, если карточка уже `running`.

**`card_model`** — сменить модель карточки или вернуть её к модели профиля:

* `model: "none"` (`"null"`, `"-"`, `""`) → `set-model <id> none` — переопределение
  снимается, воркер пойдёт на модели профиля (ровно то, что пользователь сделал руками
  17.11 для `t_375d5a5f`: `set-model … none` + `reclaim`);
* `model: "<id>"` (+ `provider`) → проверка по каталогу Hermes (`agent_models.validate`:
  неизвестная модель → `422` с понятным текстом), затем `set-model <id> <model> --provider P`;
* идущую карточку смена не ждёт: `set-model` применяется со следующего запуска, поэтому
  сразу `reclaim` — и она продолжается уже на новой модели («Сменить и продолжить» одним
  нажатием). `blocked` — `unblock` + `promote`. Обе штатные функции идут **в одном процессе**
  (`server/kanban_atomic.py`, W23-AT1): диспетчер не успевает подхватить карточку между
  записью модели и снятием работы, то есть на старой модели она уже не запустится;
* выбор человека запоминается (`user_model[task_id]`) и **сильнее** настроек комнаты:
  сторож его не «исправляет». Снятие (`none`) возвращает обычное правило (комната → профиль).

Ошибки (все тексты по-русски):

| код | когда | текст |
|---|---|---|
| `404` | карточки нет в этом запросе / чужой tenant | «Карточка не из этого запроса» |
| `409` | статус `done`, `archived` | «Карточка уже завершена» |
| `409` | статус `triage`, `todo` | «Карточка ещё не запускалась — модель применится при запуске» |
| `409` | статус `review` | «Карточка ждёт проверки — останавливать нечего» |
| `409` | `card_resume` на `running` | «Карточка уже работает» |
| `409` | `card_stop`, воркер уже снят | «Работа уже остановлена» |
| `422` | `card_model`: модель не из каталога / провайдер не настроен | «Такой модели нет у провайдера «X» — выберите из списка» |
| `400` | нет `task_id`, мусор в `model` | «Нужен task_id» |

Журнал комнаты (`journal.jsonl`): «карточка t_… остановлена пользователем (team-local)»,
«карточка t_… продолжена», «модель карточки t_…: claude-sonnet-5-5 → qwen36v» (в записи
`actor: "mikhail"` — видно, что действовал человек). Те же события — в `model_guard[]`
запроса (`stopped_by_user`, `resumed`, `model_changed`).

**Возобновление сообщением.** Если у запроса есть карточка, остановленная пользователем
(`stopped[task_id].by == "user"`), то любое новое сообщение пользователя по запросу
(`POST /api/requests/{id}/messages`) возобновляет её: текст уже лежит в ветке (карточка его
увидит), затем вызывается `card_resume` для всех таких карточек. В ответе появляется
`resumed: ["t_…"]` (дополнительное поле, старые клиенты его игнорируют). Так «продолжай» в
чат работает так же, как кнопка.

### W23.9 Что НЕ делает сервер

* не пишет в `kanban.db` напрямую: только CLI `hermes kanban`, штатный мост
  (`set_model_override` / `set_reasoning_effort`) и `server/kanban_atomic.py` — отдельный
  процесс интерпретатора Hermes, который **вызывает функции самого `hermes_cli.kanban_db`**
  (`reclaim_task`, `block_task`, `unblock_task`, `promote_task`, `set_model_override`),
  а не свой SQL. Нужен он ровно затем, чтобы остановка была одной операцией (W23-AT1):
  своего SQL по карточкам сервер не пишет нигде;
* не правит `config.yaml`, `SOUL.md` и `skills` профилей — только читает (`mode=ro`);
* не запускает модели сам: карточки ставит в очередь, работу берёт обычный движок;
* не трогает карточки чужих tenant, чужих досок и архивных комнат;
* не останавливает работу, если ожидаемую модель определить нельзя (лучше пропустить, чем
  убить нормальный запуск);
* не «лечит» выбор пользователя: `card_model` и переопределение комнаты для сторожа —
  ожидаемая модель, а не ошибка.

### W23.10 Проверки

* `python -m unittest tests.test_model_guard -v` — корневая карточка без переопределения
  при неизвестном исполнителе; сторож приводит override к ожидаемому ДО старта;
  запущенная на чужой модели останавливается, перезапускается и пишет стоимость; не
  больше 2 перезапусков; профиль без модели — не убиваем; чужой tenant не трогаем;
  нормализация имён; режим `warn` ничего не останавливает; десять тиков — одно
  исправление; регрессия на всех путях создания карточек; `card_stop` / `card_resume` /
  `card_model` на поддельной доске; возобновление сообщением.
* `python -m unittest tests.test_kanban_atomic -v` (W23-AT1) — атомарная остановка на
  поддельной доске с собственным интерпретатором Hermes и **настоящим процессом-воркером**:
  воркер убит и карточка удержана одной операцией (проверено по доске и по ОС); карточка в
  очереди удерживается без убийства; три «остановить/продолжить» подряд не уводят её в
  `triage`; чужой claim честно отвечает «воркер жив»; `resume` / `сменить модель и продолжить` /
  перезапуск сторожа — одним процессом; запасной путь CLI (последовательность, проверка,
  повтор) и два тика сторожа после остановки (карточку не поднимает, движок не зовёт). Без
  интерпретатора Hermes эти проверки честно пропускаются (`skip`), а не «проходят».
* `GET /api/rooms/{id}/model-check`, `model_guard[]` в деталях, пункт диагностики и
  настройка комнаты — те же тесты через `App.dispatch`.
* Живая проверка (только чтение реальных данных, после переноса на 8777): модуль
  показывает для `t_375d5a5f` проблему «team-local: ожидалась qwen36v (llamacpp), запущено
  claude-sonnet-5-5 (anthropic)».

## W24-DUTIES — «Дежурства» и «Здоровье интеграций» (v1.3.0-dev, WAVE24 §3.2–3.3)

Дежурство — задание Hermes по расписанию. Приложение показывает их **только на чтение**
(`cron/jobs.json`, `cron/executions.db` через `sqlite mode=ro`, `cron/output/<job_id>/*.md`)
и не запускает, не правит расписание и не пишет в Hermes ничего. «Запустить сейчас» и
правка расписания — не в этой волне.

Модули: `server/duties.py` (чтение, расписание по-русски, маски), `server/health.py`
(регистрируемые проверки), `server/routes_duties.py` (маршруты). Поле комнаты `duties`
(список id заданий) хранится в `data/rooms.json` — правится маршрутом ниже, Hermes не
меняется.

### W24.1 `GET /api/duties?room=<id|all>`

`room` по умолчанию — `all`. Неизвестная комната → `404 not_found`. Ответ (все массивы
упорядочены, пустые — `[]`, не `null`):

```
{ "generated_at": int,
  "items": [ { "job_id": str, "name": str, "enabled": bool,
               "schedule": str,                 // schedule_display из jobs.json («0 8 * * *», «every 30m»)
               "schedule_human": str,           // «каждый день в 08:00», «каждые 30 минут», «по будням в 07:30»
               "bound_rooms": [str],            // комнаты, привязавшие дежурство (rooms.json, duties[])
               "last_run": { "status": "completed"|"failed"|"running"|"missed"|"unknown",
                             "started_at": int|null, "finished_at": int|null,
                             "scheduled_for": int|null, "late_s": int|null,
                             "delivered": bool|null, "error": str|null } | null,
               "next_run_at": int|null,
               "missed_recent": int,            // пропусков за 7 суток
               "recent": [ {"status": str, "started_at": int|null, "scheduled_for": int|null} ],  // до 5
               "excerpt": str,                  // ≤ 600 знаков из «## Response» последнего вывода
               "problem": null | {"kind": "failed"|"stale"|"never_ran"|"disabled_but_due",
                                  "text": str} } ] }
```

Правила расчёта:

* **`last_run`** — последняя запись ведомости (`executions`, окно 40 запусков **на каждое**
  задание, а не общий хвост: у частого задания тысячи записей). Ведомости нет — берём
  `last_run_at`/`last_status` из `jobs.json` (`delivered` тогда `null`).
* **`missed_recent`** — записи `status='missed'` за 7 суток (по `claimed_at`, иначе по
  `scheduled_instant`).
* **`excerpt`** — из блока «## Response» самого свежего файла вывода. Текст задания,
  памятки и «## Prompt» в итог не попадают. Секреты маскируются (`server/worklog.py`
  маски + `token=`-пары), домашний каталог заменяется на `~`, длина ≤ 600 знаков.
  У «тихих» запусков (скрипт без вывода, `no_change`) блока нет — `excerpt` пустой.
* **`problem`** (первое подходящее): `failed` — последний запуск с ошибкой; `disabled_but_due`
  — задание выключено; `never_ran` — ни одного запуска; `stale` — `next_run_at` прошёл
  больше 15 минут назад, либо последний запуск старше двух шагов расписания (шаг из cron:
  сутки / неделя / месяц, из интервала — его минуты).
* Порядок `items`: сначала с замечанием, затем по имени.

### W24.2 `PUT /api/rooms/{id}/duties`

```
{ "job_ids": ["<id задания>", …] }        // [] — отвязать всё
→ 200 { "ok": true, "room": str, "duties": [str], "generated_at": int, "items": [Item] }
```

* Проверка: список строк, формат `[A-Za-z0-9_.-]{1,64}`, без повторов, не больше 40;
  неизвестный id (нет в `jobs.json`) → `422 unknown_job` с `detail.unknown`;
  тело без `job_ids` → `400` с `detail.missing`; неизвестная комната → `404`.
* Меняется только запись комнаты (`duties`). Файлы Hermes не трогаются — это проверено
  тестом (снимок mtime до и после) и живой проверкой.

### W24.3 `GET /api/health/integrations`

```
{ "generated_at": int, "overall": "ok"|"warn"|"fail",
  "checks": [ {"id": str, "title": str, "status": "ok"|"warn"|"fail"|"unknown",
               "detail": str, "since": int|null, "hint": str|null} ] }
```

`overall`: `fail`, если есть хоть один `fail`; иначе `warn`, если есть `warn`; `unknown`
сам по себе оценку не портит (неизвестное ≠ плохое). Проверки идут в порядке регистрации
(`server/health.py`, декоратор `@register`) — новая проверка сразу попадает в ответ:

| id | что смотрит | статусы |
|---|---|---|
| `gateway` | `gateway_state.json`: состояние, свежесть записи (> 300 с → warn) | ok / warn / fail / unknown |
| `dispatcher` | `engine_health.dispatcher_status` (та же диагностика, что в «Проверке системы») | ok / warn / fail |
| `peer_link` | `bot_peers` из `config.yaml`: TCP-подключение к localhost-портам (0,8 с). Адресов нет → `unknown` | ok / warn / fail / unknown |
| `calendar_token` | `google_token.json` (наличие, `expiry`) **и** `invalid_grant`/`REFRESH_FAILED` в блоках «## Response» дежурств за 3 суток | ok / warn / fail / unknown |
| `duties` | задания с `problem` (см. W24.1) | ok / warn / unknown |
| `backups` | самая свежая папка в `TEAMROOM_BACKUPS` или `C:/TeamRoom/Backups`; > 14 суток → warn | ok / warn / unknown |
| `disk` | свободное место там, где данные: < 10 % → warn, < 3 % → fail | ok / warn / fail / unknown |
| `stt` | `diagnostics._stt_status`: режим диктовки | ok / warn |
| `model_guard` | `model_guard.diag_item` по комнатам | ok / warn / unknown |

Внешняя сеть не используется: только файлы Hermes (на чтение) и localhost-порты из
настроек. Исключение внутри проверки не роняет отчёт — проверка становится `unknown` с
текстом причины. Ответы `GET`-маршрутов отдаются с `Cache-Control: no-store`.

### W24.4 Проверки

* `python -m unittest tests.test_duties -v` (31) — расписание по-русски (cron, `*/30`,
  `every 30m`, `in 2h`, дни недели, месяцы), `last_run`/`missed`, `failed`/`stale`/
  `never_ran`/`disabled_but_due`, маскирование и длина `excerpt`, фильтр по комнате,
  привязка (дубли, мусор, неизвестный id → 422, предел 40, отвязка), «Hermes не изменён»
  (снимок mtime), битые `jobs.json` и отсутствие каталога.
* `python -m unittest tests.test_health -v` (34) — форма ответа, порядок и расширяемость
  реестра, падение проверки → `unknown`, приоритет `overall`, шлюз (ok/warn/fail/нет файла),
  раздача заданий, сосед (нет настроек/отвечает/молчит), календарь (истёк, `invalid_grant`
  в выводах, упоминание вне окна, нет файла, здоровый), дежурства, копии, диск, диктовка,
  контроль моделей, «в Hermes не пишем» (снимок файлов).

## W24-TODAY — экран «Сегодня» (v1.3.0-dev, WAVE24 §3.1, пакет N2)

Два маршрута отвечают на вопрос «что ждёт человека сейчас и что изменилось с прошлого
раза». Правила собраны в `server/today.py` (чистые функции), HTTP-слой — в
`server/routes_today.py`. Новых хранилищ нет: единственный файл — отметка захода
`data/state/today.json`.

### W24-TODAY.1 `GET /api/today`

```
{ "generated_at": int, "last_visit_at": int|null,
  "counters": {"needs_you": int, "overdue": int, "due_soon": int, "running": int, "changed": int},
  "needs_you": [ {"room", "room_title", "request_id", "request_title",
                  "kind": "question"|"decision"|"approval"|"step"|"draft"|"failure"|"stalled",
                  "text", "since", "urgent", "recommended"} ],
  "overdue":  [ {"room", "room_title", "item_id", "text", "owner", "due", "days_overdue", "request_id"} ],
  "due_soon": [ { …то же…, "days_left": int } ],
  "running":  [ {"room", "room_title", "request_id", "request_title", "members_working", "since"} ],
  "changed":  { "finished": [ {"room", "request_id", "request_title", "at"} ],
                "new_requests": int, "new_answers": int,
                "failures": [ {"room", "request_id", "request_title", "at", "reason"} ] },
  "rooms":    [ {"id", "title", "needs_you", "overdue", "running"} ] }
```

Время — пояс сервера (unix). Пустые разделы — пустые массивы, не `null`. Массив `rooms`
включает все неархивные комнаты, даже пустые (нули) и недоступные (нули: экран «Сегодня»
не должен падать из-за одной комнаты).

**Откуда берётся каждый раздел** (только существующее чтение):

| раздел | источник |
|---|---|
| `needs_you` (вопросы, решения, шаги) | пункты «От вас нужно» (`ingest.backlog_view`, с наложением ответов `answers`), владелец = человек (`mikhail`), статус `open`/`asked` |
| `needs_you` (предложение этапов) | тот же бэклог, вид `phases_proposal` → `kind: "decision"`; `request_id` — пустая строка (пункт не привязан к запросу) |
| `needs_you` (разрешения) | `approvals.collect` — только у корней со статусом `needs_you`, не больше 8 на комнату |
| `needs_you` (черновик карты) | `plan.load`: `status="draft"` и есть потоки → «ждёт утверждения» |
| `needs_you` (сбой) | корень со статусом `failed`; `text` — причина из карточки, секреты замаскированы (`worklog.mask`) |
| `needs_you` (карточка не отвечает — **W25-M2**) | `watchdog.stalled(cfg, room)`: только состояние сторожа (`data/state/<room>/watchdog.json`), доска не читается; `text` — «Карточка <кто> не отвечает N мин» (после остановки — «…остановлена, нужно внимание»); `urgent` — при N ≥ 20 мин (`today.URGENT_STALL_MIN`) |
| `overdue` / `due_soon` | открытые пункты бэклога с `due` (`YYYY-MM-DD`); «просрочено» = `due` раньше сегодняшнего дня, «ближайшие» = сегодня и ≤ 3 суток |
| `running` | `engine_read.today_states`: корень `working`/`reviewing`/`synthesizing` или живые карточки (`running`/`review`) |
| `changed` | реестр запросов (`created_at`, `answer_at`) и состояние корней (`done`/`failed`) относительно `last_visit_at` |
| `rooms` | `rooms.list_rooms` (неархивные) |

**Правила, которых нет в §3.1, но нужны интерфейсу**

* `urgent` — сбой и запрос разрешения срочны всегда, остальное — если ждёт больше суток.
* `recommended` — короткая подсказка следующего шага («Ответить на вопрос», «Разрешить или
  запретить», «Посмотреть, на чём остановилось»…), может быть `null`.
* `counters.changed` = `len(finished) + new_requests + new_answers + len(failures)` — значка
  столько, сколько строк в сводке «что изменилось».
* «Изменилось» при `last_visit_at = null` считается за последние 24 часа.
* `running.members_working` — сколько разных исполнителей занято (не меньше 1 у идущего
  запроса, даже если карточки ещё не взяты).
* Порядок: `needs_you` — срочные сверху, затем кто ждёт дольше; `overdue` — больше
  просрочка выше; `due_soon` — ближайший срок выше; `running` — кто начал раньше;
  `finished`/`failures` — свежие сверху.
* Кэш ответа — 10 секунд (опроса раз в 20 секунд достаточно). `POST /api/today/seen`
  сбрасывает кэш.
* Ошибка одного источника (комната, бэклог, доска) не роняет ответ: раздел этой комнаты
  пустой. Ошибки чтения доски попадают в `data/logs/app.log`.

### W24-TODAY.2 `POST /api/today/seen`

```
→ 200 {"ok": true, "last_visit_at": int, "previous": int|null, "changed": bool}
```

* Отмечает заход: пишет `data/state/today.json` (`{"last_visit_at", "updated_at"}`, атомарно,
  под замком файла). Это значение отдаёт `GET /api/today` как «предыдущий заход».
* Идемпотентно: если отметка моложе 60 секунд, файл не переписывается (`changed: false`),
  ответ — прежнее значение.
* Тело не читается. Запись не удалась (нет прав/диск) → `503 state_unavailable` с русским
  текстом; экран продолжает работать, «изменилось» считается за сутки.

### W24-TODAY.3 Проверки

* `python -m unittest tests.test_today -v` (35) — форма ответа и порядок, все виды
  `needs_you`, просрочка и ближайшие сроки (границы 0 и 3 суток), `last_visit_at` и окно
  24 часа, пустые и битые комнаты, маска секретов в причине сбоя, обрезка «сути» до 200
  знаков, отметка захода (идемпотентность, 60 секунд, битый файл), живой маршрут в
  песочнице (кэш, `POST /api/today/seen`), замер 2 комнаты × 50 запросов, сверка ключей с
  фикстурой интерфейса `fixtures/today.json`.
* `python tools/probe_today_be.py --port 8855` — живая проверка в Chromium на своём
  мок-сервере: ключи из браузера, замеры `fetch`, скриншоты в `docs/shots/w24-today-be/`.
* Замер 18.11: 2 комнаты × 50 запросов — медиана сборки **6 мс**, худший **7 мс** (без кэша),
  ответ маршрута из браузера — **0,8–1,5 мс** при кэше. Требование §3.1 — ≤ 200 мс.

## W26-DUTIES — действия с дежурствами и недельная сетка (v1.4.0-dev, WAVE26 §2, пакет D8)

Развитие §W24-DUTIES. Чтение осталось прежним (`GET /api/duties`), добавлены: недельная
сетка в том же ответе и маршрут действий. Единственная запись в Hermes — штатный CLI
`hermes cron run|pause|resume <id>`, и только по явному подтверждению человека
(`confirm: true`). Правка расписания — **предложение**, Hermes не меняется.

### W26.1 `GET /api/duties?room=<id|all>` — новый ключ `week`

```
{ "generated_at": int, "items": [Item…],           // как в §W24.1, без изменений
  "week": { "start": "2026-11-16", "end": "2026-11-22",
            "runs": int, "failed": int, "missed": int,
            "days": [ { "date": "2026-11-16", "weekday": 0, "label": "пн", "full": "понедельник",
                        "day": 16, "month": 11, "today": bool, "future": bool,
                        "runs": int, "failed": int, "missed": int,
                        "jobs": [{"job_id": str, "name": str, "runs": int,
                                  "state": "ok"|"failed"}],      // до 3, чаще — сверху
                        "more": int,                             // «+N ещё»
                        "planned": int } ] } }                    // сколько по расписанию
```

Правила: `weekday` 0 — понедельник; неделя начинается с понедельника по местному времени;
`runs` — **фактические** запуски из `executions` (окно читается прямо в SQL, а не хвостом
в 40 записей на задание); `planned` — ожидаемое число по расписанию (интервал —
`86400/минуты`, cron с днями недели — только в свои дни, выключенные — 0). В будущих днях
`runs = 0`, а `planned > 0`: интерфейс пишет «план: N», а не «0 запусков». Если посчитать
не удалось, `week.error` — строка по-русски.

### W26.2 `POST /api/duties/{id}/actions`

```
{ "action": "run"|"pause"|"resume"|"bind"|"unbind"|"schedule",
  "confirm": true,                    // обязательно, иначе 409 confirm_required
  "room": "<id комнаты>",             // обязателен для bind/unbind
  "schedule": "every 2h" }            // обязателен для schedule (≤ 80 знаков)
→ 200 { "ok": true, "action": str, "label": str, "job_id": str, "name": str,
        "applied": bool, "dry": bool, "command": str|null, "text": str,
        "bound_rooms": [str], "proposal": {...}|null, "job": Item|null, "generated_at": int }
```

* `run|pause|resume` — ровно `hermes cron <action> <id>` (argv, без shell, `HERMES_HOME`
  из конфигурации, таймаут 25 с). Ненулевой код → `502 cli_failed` с
  `detail.command|code|err|out`: «не получилось» не выдаётся за успех.
* `bind|unbind` — меняется только реестр комнат (`data/rooms.json`, поле `duties`),
  задание в Hermes остаётся; в ответе новый список `duties`.
* `schedule` — сохраняется **предложение** в `data/state/duties/proposals.json`
  (`state: "proposed"`), `applied: false`, текст говорит «расписание в Hermes не изменено».
* Пробный режим `TEAMROOM_DUTIES_DRY=1` (или `cfg.duties_dry_run`): CLI не вызывается,
  ответ `dry: true`, в `command` — что было бы выполнено.
* Подставной CLI для тестов и песочницы: `duties.set_cli_runner(fn)` / `TEAMROOM_HERMES_BIN`.
* В песочнице (`TEAMROOM_HERMES_HOME`) дежурства читаются из песочного дома, а не из
  реального: действие из песочницы не может попасть в живые задания.

Коды ошибок: `400 bad_request|schedule_required|room_required`, `409 confirm_required`,
`422 unknown_job`, `502 cli_failed`. Тесты: `tests/test_duties_actions.py` (17) и
`tests/duties-view.test.mjs` (14).

## W25-REGISTRY — реестр решений комнаты: append-only и хеш-цепочка (v1.3.0-dev, WAVE25 §M1)

Источник правды по решениям комнаты — `data/state/<room>/memory/registry.jsonl`
(одна строка = одно событие, ничего не перезаписывается). `decisions.json` остаётся,
но становится **проекцией** реестра (`"projection_of": "registry.jsonl"`): статус в нём
теперь совпадает с паспортом комнаты. Причина: раньше решение, снятое в запросе
(`dismissed`) или отменённое новой версией свода (`superseded`), оставалось в
`decisions.json` как `accepted` и попадало в паспорт — исполнитель работал по
отменённому решению (R4 §3 п.1).

### W25-REGISTRY.1 Формат строки реестра

```
{ "id": "<root_id>:d_<id>", "seq": int, "at": int, "text": str, "why": str|null,
  "status": "proposed"|"accepted"|"rejected"|"dismissed"|"superseded",
  "prev_status": str|null, "by": "app"|"user"|"migration", "source_request": str|null,
  "due": "YYYY-MM-DD"|null, "supersedes": int|null, "prev_hash": str, "hash": str }
```

* `seq` — номер строки журнала (1, 2, …); он же адрес версии;
* `supersedes` — `seq` прежней строки того же решения при смене статуса;
* `prev_hash` — хеш предыдущей строки, `hash` — sha256 канонического JSON строки
  вместе с `prev_hash`. Подмена любой строки ломает цепочку (проверка — `verify`);
* `by` — кто записал: приложение (`app`), действие владельца (`user`), миграция (`migration`).
  Автоматический слой не понижает `accepted` до `proposed` и не «возвращает» снятое:
  это делает только явное действие владельца (`by="user"`).

Запись — **единственная функция** `server.decision_registry.append`: межпроцессная
блокировка файла (`msvcrt`/`fcntl`), `O_APPEND`, `fsync`, перечитывание хвоста под замком.
Отдельного «POST в реестр» нет: статусы меняются штатными действиями решения
(`POST /api/requests/{id}/actions`, `decision: accept|reject|dismiss|restore`) и приёмом
ответа (`ingest`), которые пишут строку сами.

### W25-REGISTRY.2 `GET /api/rooms/{id}/registry`

Только чтение. Параметры: `history=1` — все строки журнала (иначе свёртка по `id`),
`status=accepted,dismissed` — фильтр по статусам, `limit` — 1…500 (по умолчанию 200).

```
{ "room": "fn", "generated_at": int,
  "registry": {"path": "memory/registry.jsonl", "rows": int, "decisions": int, "append_only": true},
  "chain": {"ok": bool, "rows": int, "broken_at": int|null, "reason": str},
  "counts": {"proposed": int, "accepted": int, "rejected": int, "dismissed": int, "superseded": int},
  "pinned": {"ids": [str], "count": int, "limit": 30, "over_limit": bool},
  "items": [ {"id", "at", "text", "why", "status", "prev_status", "by",
              "source_request", "due", "seq", "pinned"} ],
  "total": int, "limit": int, "history": bool,
  "warnings": [ {"code": "registry_chain_broken"|"pin_limit", "message", "detail"} ] }
```

* `chain.ok=false` — цепочка повреждена; `broken_at` — номер строки, где разрыв.
  API не «чинит» файл: это сигнал человеку и сторожу (`tools/check_room_memory.py`).
* `pinned.limit` — потолок закрепления (**30** записей на комнату). При достижении
  новые закрепления не добавляются, в ответе `over_limit: true` и предупреждение
  `pin_limit` — то же попадает в «Здоровье».
* `404 not_found` — нет комнаты; `400 bad_request` — неизвестный статус в фильтре.
* Пустая комната: `items: []`, `counts` нули, `chain.ok: true` — это норма, не ошибка.

### W25-REGISTRY.3 Файлы памяти комнаты

| Файл | Что | Кто пишет |
|---|---|---|
| `memory/registry.jsonl` | журнал решений (источник правды) | `decision_registry.append` |
| `memory/registry.lock` | замок записи | `_FileLock` |
| `memory/pinned.json` | закреплённые `id` (не сжимаются), потолок 30 | действия закрепления |
| `memory/snapshot.json` | снимок множества `id` (инвариант «только растёт») | приём ответа |
| `decisions.json` | проекция реестра (совместимость) | `project_decisions` |
| `Rooms/<id>/Решения/реестр.md` | читаемый вид реестра в Vault | `write_mirror` |

Миграция: `python tools/migrate_decisions.py --room fn` (или `--all`, `--dry-run`,
`--check`). Кладёт резервную копию `decisions.json.bak-<unixtime>` и идемпотентна:
повторный запуск не добавляет строк.

### W25-REGISTRY.4 Проверка памяти комнаты

`python tools/check_room_memory.py --room fn [--json] [--only 1,2]` — 7 инвариантов
R4 §4.5 (CLI и функция `check(cfg, room_id)` для экрана «Здоровье»). Код возврата 1 —
нарушен блокирующий инвариант (цепочка реестра, append-only, паспорт ↔ реестр, обрезка
ответа), 0 — только предупреждения/пропуски. Инварианты 5–6 (бюджеты тела карточки и
дублирование устава) сейчас дают `warn`: их лечит пакет M2, а не реестр. Инвариант 7
(recall@5 золотого набора) — честный `skip`: набора вопросов нет.

## W25-CARD-BUDGETS — бюджеты тела карточки и сторож зависаний (v1.3.0-dev, WAVE25 §M2)

### W25-CARD-BUDGETS.1 Бюджеты (знаки, `server/card_budget.py`)

| Тип карточки | `kind` | Потолок | Что в теле |
|---|---|---|---|
| Руководитель (корневой запрос, бриф) | `leader` | **6 000** | текст Михаила + шапка + устав комнаты |
| Исполнитель (направление) | `worker` | **2 500** | задача + выжимка дела + 2 решения |
| Критик | `critic` | **8 000** | запрос + выжимка свода + критерии |
| Обновление свода (доработка) | `svod` | **8 000** | дайджест ≤ 4 000 + выжимка прошлой версии ≤ 3 000 + ссылка на карточку версии + ядро контракта |
| Приёмщик, служебные | `service` | **2 000** | письмо + 5 строк инструкции |

Правила: текст Михаила **не режется** — при длинном тексте ужимается текст приложения
(сначала дополнительные правила, затем необязательные поля `metadata`); выкинутое
перечисляется строкой со ссылкой на `docs/AGENT-GUIDE.md`, не молча. Прошлый свод в
доработку больше не вставляется целиком: выжимка + `Прошлая версия целиком — карточка t_…`.
Полный устав комнаты живёт в корневой карточке; доработка получает ядро (8–10 строк).
Замер: `tools/check_card_budgets.py [--fixtures] [--board <kanban.db>]` (только чтение).

### W25-CARD-BUDGETS.2 Сторож зависаний (`server/watchdog.py`)

Настройка комнаты (необязательное поле `card_watchdog` в `rooms.json`):

```
"card_watchdog": {"warn_min": 10, "stop_min": 20, "hard_stop": true}
```

* `warn_min` (2–120, по умолчанию 10) — через сколько минут без событий и вывода
  инструментов предупредить: строка в «Ход работы» и вид дела `stalled` в «Сегодня»;
* `stop_min` (больше `warn_min`, по умолчанию 20) — через сколько остановить карточку
  **штатным механизмом W23** (`kanban_atomic.stop`: воркер убит, карточка в `blocked`)
  и пометить «Нужно внимание» (варианты: «Продолжить», «Сменить модель и продолжить»);
* `hard_stop: false` — только предупреждать, не останавливать.

Карточка с **живым выводом инструментов** зависшей не считается: таймер идёт от
последнего сообщения сессии (`worklog.activity`), а незавершённый вызов инструмента у
живого воркера снимает подозрение. Один активный проход на карточку — замок
`data/state/<room>/watchdog/locks/<card>.json` с TTL 300 с. Состояние:
`data/state/<room>/watchdog.json` (`cards`, `warns`, `stops`). Проход — свой поток
наблюдателя (`watcher.watchdog_tick_once`, интервал 30 с), статус — `status()["watchdog"]`.


## W26-INBOX — «Входящие комнаты» и полоса «Надиктовать» (v1.4.0-dev, WAVE26 §2, пакет D9)

Быстрая запись мысли в комнате и её разбор. Хранилище —
`data/state/<room>/inbox.json` (через `statefiles`, атомарная запись). Запись
создаётся полосой «Надиктовать» внизу экранов комнаты (клавиша `N`, микрофон —
существующая диктовка `/api/stt/*`; ничего в диктовке не менялось).

### W26-INBOX.1 Маршруты

| Метод | Путь | Что делает |
|---|---|---|
| `GET` | `/api/rooms/{id}/inbox?status=active\|new\|processing\|processed\|deleted\|all` | список записей (по умолчанию `active` = новые + в разборе) |
| `POST` | `/api/rooms/{id}/inbox` `{text, source:"dictation\|manual"}` | записать мысль; `201` и свежие счётчики |
| `POST` | `/api/rooms/{id}/inbox/{item}/actions` `{action, …}` | разбор записи |

Ответ списка:

```
{ room, generated_at, filter, counts:{new,processing,processed,deleted,total,active,proposals},
  items:[InboxItem…], notes:[…], queue:[Card…], updated_at, warning }
```

`warning` непустой, если файл входящих был повреждён: копия сохранена рядом
(`inbox.json.corrupt-<unixtime>`), список начат заново — молчаливой подмены нет.

### W26-INBOX.2 Запись `InboxItem`

```
{ id:"in_xxxxxxxx", room, text, source:"dictation|manual", source_title:"диктовка|вручную",
  created_at:"2026-11-18T14:03:11+03:00", created_hhmm:"14:03",
  status:"new|processing|processed|deleted", kind:"delo|step|question|duty|note"|null,
  proposal:{id,at,kind,kind_title,title,body,state:"waiting|accepted|rejected",by,actions:[…]}|null,
  resolution:{action,kind,kind_title,title,at}|null,
  note, actions:[{id,label}…] }
```

`actions` — единственный источник кнопок интерфейса. Он **никогда не пуст**:
у `new`/`processing` — `propose` + ручной разбор (`to_delo|to_step|to_question|
to_duty|to_note`) + `delete`; у записи с ожидающим предложением — `accept|edit|
reject|delete`; у `deleted` — `restore`; у `processed` — `restore|delete`.
Правило «без тупиков» проверяется тестом `tests/test_inbox.py`.

### W26-INBOX.3 Действия и предложение

| `action` | Что происходит |
|---|---|
| `propose` | разбор руководителем: вид угадывается по словам, собирается шаблон-предложение `state:"waiting"`, запись → `processing`, в `queue` кладётся карточка «разобрать входящее» |
| `to_delo` / `to_step` / `to_question` / `to_duty` / `to_note` | тот же шаблон, но вид выбирает человек |
| `edit` `{text, title?}` | правка текста предложения (`by:"ваша правка"`) |
| `accept` | предложение принято: `status:"processed"`, заготовка уходит в `delos_drafts` / `steps_drafts` / `questions_drafts` / `duties_drafts` / `notes`, карточка очереди закрывается |
| `reject` `{text?}` | предложение отклонено, запись возвращается в `new` (разбор можно повторить), причина — в `note` |
| `delete` / `restore` | мягкое удаление и возврат |

Предложение — **шаблон этой волны**: разбор делает руководитель-модель, а
интерфейс показывает «Предлагаю оформить так: … [Принять] [Изменить] [Отклонить]».
Карточка руководителю кладётся в очередь `queue` (ветка «руководитель») внутри
`inbox.json`, в песочнице: агентов не запускаем, наружу ничего не отправляем.

Заготовки разбора остаются в самом `inbox.json`: дела (D5), шаги, вопросы и
дежурства (D8) в этой волне — отдельные пакеты со своими хранилищами, поэтому
здесь лежит текст заготовки (`state:"draft"`), а не выдуманная запись в чужом
файле. `notes` — «справки» комнаты (видны в ответе списка).

### W26-INBOX.4 Ошибки

`400` — пустой/слишком длинный текст (лимит 4000 знаков), неизвестный источник,
неизвестный `action`/`status`; `404` — комната или запись не найдены (в том числе
неверный вид `id`); `409` — принять/изменить нечего (предложения нет или оно уже
решено); `503 state_unreadable` — файл входящих занят, повторите.

## W26-DELO — «Дело»: серверная часть (v1.4.0-dev, WAVE26 §1–2, пакет D5)

Долгоживущий объект комнаты. Запрос — одно событие внутри дела; дело живёт неделями:
этапы, срок, «ждём: кого/чего», части результата и файлы, исходящее с состояниями,
«Ход дела». Форма — контракт `docs/WAVE26-RULES.md` §1, общий с интерфейсом (D6).
Кода: `server/delo.py` (правила + хранение), `server/routes_delo.py` (HTTP),
`tests/test_delo.py`, `tools/probe_delo.py`, демо — `tools/mock_delos.py`.

### W26.1 Хранение

`data/state/<room>/delos.json` — один файл на комнату, запись атомарная
(`statefiles.write_json_atomic`) под замком файла:

```
{version:1, updated_at:<unix>, items:[Delo…]}
```

Правила чтения файла — как у остальных файлов состояния: нет файла → пусто;
битый JSON → копия `delos.json.corrupt-<время>` рядом и пустой список (тревога уходит
в `/api/health`); файл занят другим процессом → `503 state_unavailable`
(значениями по умолчанию НЕ подменяем). Порядок списка — свежие сверху (`updated_at`).
Потолок — 500 дел на комнату (`409`).

### W26.2 Форма `Delo`

```
{ id:"dl_xxxxxxxx", room, title, status:"open|waiting|paused|done|cancelled",
  due:"2026-11-21"|null, owner:"manager", members:["profile"…], updated_at:<unix>,
  request_id:"t_…"|null,
  stages:[{id:"st_1", title, state:"done|now|todo"}],
  waiting_on:[{who:"you|<текст>", what, since:<unix>}],
  needs_you:[{id:"ny_…", kind:"approve|question|permission", text, recommended,
              since:<unix>, options?:[{id,label}], actions:["approve","reject"…],
              source:{kind, request_id, question_id|decision_id|step_id|approval_id}}],
  result:{parts:[{id:"pt_1", title, state:"agreed|discussing|todo"}],
          files:[{path, title, role:"final|source|draft", note}]},
  outbox:[{id:"ob_1", kind:"email|file|message", to, subject, body,
           attachments:[path], state:"draft|approved|sent|failed|cancelled",
           sent_at?, error?}],
  found:[…формат «Найдено» D4, как есть…],
  timeline:[{at:<unix>, who:"manager|you|system", text, kind}],
  duties:[{id, title, schedule_human, state}] }
```

* `status` на чтении **вычисляется**: `paused|done|cancelled` — явные (их ставят
  действиями), иначе `waiting` при непустом `waiting_on`, иначе `open`.
* `needs_you` **не хранится**, а собирается при каждом чтении из существующих
  модулей (§W26.3) — поэтому ответ на вопрос сразу убирает пункт из дела.
  `actions` — имена действий этого же маршрута; `recommended` — короткая подсказка
  (её пишет источник, иначе вид пункта). `source` — служебная ссылка на пункт
  источника (интерфейсу не нужна, нужна действию).
* `stages` по умолчанию: Сбор → Черновик → Согласование → Исполнение → Готово;
  «сейчас» в цепочке ровно один (если дело не завершено).
* `timeline` — «Ход дела», до 300 последних строк (старое обрезается).
* В коде и данных нет терминов конкретного бизнеса: только дела, этапы, ожидания.

### W26.3 Откуда «ждёт вашего решения»

Дело не заводит своих правил, а спрашивает:

| источник | что берём | как |
|---|---|---|
| бэклог комнаты (`App._backlog` → `ingest.backlog_view`) | `open_question` (владелец `mikhail`), `decision`, `next_step` со статусом `open|asked` и `source_request` = запрос дела | `kind`: `question` / `approve` / `approve` |
| разрешения (`approvals.collect` + `pending`) | только у корня со статусом `needs_you` и только `grantable:true` (недоговорные не ждут решения — правило D1) | `kind:"permission"` |
| связь с запросом (`registry.list_requests`) | заголовок дела, если он не задан | `request_id` |

Порядок пунктов: разрешения → решения/шаги → вопросы; внутри вида — кто ждёт дольше.
У каждого пункта ≥1 действие (правило «без тупиков»). Дело без `request_id`
(создано вручную) пунктов не имеет — пустой список честнее выдуманного.

### W26.4 Маршруты

| метод и путь | что |
|---|---|
| `GET /api/rooms/{id}/delos` | `{items:[Delo…], generated_at}` — дела комнаты |
| `POST /api/rooms/{id}/delos` | открыть дело → `201 Delo` |
| `GET /api/delos?room=<id|all>` | дела всех комнат (бейдж в меню) |
| `GET /api/delos/{id}` | одно дело |
| `POST /api/delos/{id}/actions` | действие → `{ok:true, action, delo, delegated?}` |

Тело создания: `{request_id?, title?, due?, owner?, members?, stages?, waiting_on?,
result?{parts,files}, outbox?, found?, duties?, status?}`. `title` можно не передавать
при `request_id` — возьмётся из запроса. Одно дело на запрос: повтор → `409` с id
существующего дела (иначе на один запрос появятся клоны).

### W26.5 Действия `POST /api/delos/{id}/actions`

| `action` | поля | что происходит |
|---|---|---|
| `approve` | `item_id` (или `need_id`), `text?` | пункт вида `approve|permission` утверждён — уходит в механизм запроса |
| `reject` | `item_id` (или `need_id`), `text?` | отклонён — туда же |
| `answer` | `item_id` (или `need_id`), `text` или `option_id` | ответ на вопрос — в механизм ответов; выбранный вариант (`option_id`) уходит подписью варианта |
| `set_stage` | `stage_id`, `state?`, `title?` | «сейчас» закрывает предыдущие; «сделано» делает следующий текущим; `title` переименовывает; **без `state`** этап просто становится текущим |
| `add_part` | `title`, `state?` | часть результата |
| `set_part` | `part_id`, `state`, `title?` | «согласовано / обсуждаем / не начато» |
| `approve_outbox` | `outbox_id` | `draft|failed` → `approved` (ваше «да») |
| `edit_outbox` | `outbox_id`, `to?|subject?|body?|attachments?` | правка; утверждённое возвращается в `draft` (текст изменился — «да» заново) |
| `cancel_outbox` | `outbox_id` | `cancelled`; отправленное не отменяется (`409`) |
| `mark_sent` | `outbox_id`, `error?` | `approved` → `sent` + `sent_at`; с `error` → `failed` + текст ошибки |
| `pause` / `resume` | — | пауза / продолжение (повтор → `409`) |
| `done` | — | дело закрыто (повтор → `409`) |
| `add_waiting` | `who`, `what` | «Ждём» + лента |
| `clear_waiting` | `all:true` или `who`/`what`/`index` | снять ожидание (нечего снимать → `404`) |

Действия по пункту (`approve|reject|answer`) **не переписывают** правила запроса:
маршрут вызывает штатный обработчик (`App.h_request_actions`) с телом
`{action:"answer"|"decision"|"step_agree"|"step_drop"|"approval", …}`. Если запрос
отказал — дело **не меняется** (сначала внешнее действие, потом одна атомарная
запись файла). Без связи с запросом — `409 channel_unavailable`.

**Наружу ничего не отправляется.** `mark_sent` — смена состояния и время; письма,
файлы и сообщения реально не уходят (гейт «необратимо/видно третьему лицу» остаётся
за человеком). `sent_at` — момент отметки, а не подтверждение доставки.

### W26.6 Ошибки

`400` — нет названия/поля, неизвестное действие (в `detail.allowed` — список),
мусор вместо строки, срок не `YYYY-MM-DD`, некорректный id; `404` — комната, дело,
этап, часть или исходящее не найдены; `409` — дубль дела по запросу, повтор
паузы/закрытия, отправка без утверждения, правка отправленного, пункт уже не ждёт
решения; `503 state_unavailable` — файл дел занят.

### W26.7 Проверки

* `python -m unittest tests.test_delo` — 85 тестов: этапы (переходы, «ровно один
  сейчас», переименование, «сделать текущим» без `state`), пункты «ждёт вас» (виды,
  действия, порядок, опции, битый источник, `need_id` и `option_id` интерфейса),
  создание (из запроса, вручную, дубль, срок, лимит, 8 одновременных созданий),
  действия (все 15, коды, лента, «нечего править»), исходящее (draft→approved→sent,
  ошибка канала, правка после «да», отмена), делегирование (текст ответа, отказ
  запроса не пишет дело), форма по контракту и сверка с `fixtures/delo.json`.
* `python tools/probe_delo.py --port 8855` — живой сервер в песочнице: 24 проверки
  по HTTP, замеры списка (p50 ≈ 3 мс, p95 ≈ 18 мс на 2 делах), отчёт
  `docs/status/delo-be.json`.
* `python tools/check_conformance.py` — нарушений 0.

## W27-AUTONOMY — уровни автономии комнаты (v1.4.0-dev, WAVE27 §2, пакет E5)

Три уровня поверх **штатных** настроек Hermes: `careful` («Осторожный»), `working`
(«Рабочий», по умолчанию), `free` («Свободный»). Комната хранит только выбор
(`rooms.json` → `autonomy: {level, never, updated_at}`); сами настройки живут в
профилях Hermes (`config.yaml`): `command_allowlist`, `approvals.deny`,
`approvals.single_query_mode`. Запись — только в профили участников ЭТОЙ комнаты,
только по `confirm: true`, всегда с копией старого конфига.

### W27.1 Чтение: `GET /api/rooms/{id}/autonomy`

```json
{
  "room": "r_leto", "level": "working", "level_label": "Рабочий",
  "never": ["*rm -rf*", "…"], "updated_at": 1790000000,
  "applied_level": "working", "applied_at": 1790000000,
  "applies_to": ["anna-lead", "ivan-petrov"],
  "profiles": [
    {"profile": "ivan-petrov", "label": "Исполнитель по заявкам", "exists": true,
     "allowlist": ["script execution via heredoc"],
     "deny": [], "single_query_mode": "deny",
     "auto_allowed": ["script execution via heredoc"]}
  ],
  "defaults": {"never": ["*rm -rf*", "…"],
               "working_rules": ["script execution via -e/-c flag",
                                 "script execution via heredoc"]},
  "never_labels": {"*rm -rf*": "рекурсивное удаление (rm -rf)"},
  "levels": [{"level": "careful", "label": "Осторожный", "hint": "…"}],
  "preview": {
    "careful": {"level": "careful", "label": "Осторожный", "unchanged": false,
                "changes": ["…по-русски, что изменится…"],
                "profiles": [{"profile": "…", "add": [], "remove": [], "deny": 0,
                              "single_query_mode": null, "missing": false}]},
    "working": {"…": "…"}, "free": {"…": "…"}
  },
  "at": 1790000000
}
```

* `level` — выбранный уровень; у комнаты без записи — `working` (умолчание), при этом
  **профили не меняются**: применяет только `POST`.
* `applied_level` — что реально применялось последним (`data/state/autonomy-applied.json`);
  пусто — «ещё не применяли».
* `profiles[].single_query_mode` — как записано в профиле (пусто → показываем `deny`);
  `exists: false` — профиля нет, писать некуда.
* `preview.<уровень>.changes` — готовые русские строки для экрана «что изменится»
  (сервер считает их по текущим конфигам, ничего не записывая). `profiles[]` — то же
  машинно: `add`/`remove` (ключи списка разрешений), `deny` (сколько масок станет),
  `single_query_mode` (`null` — не меняется).

### W27.2 Запись: `POST /api/rooms/{id}/autonomy`

```json
{"level": "working", "never": ["*rm -rf*", "*git push*"], "confirm": true}
```

Без `confirm: true` — **`409 confirm_required`** с полем `detail.preview` (все три
уровня) и `detail.confirm: true`: интерфейс показывает «что изменится» и только потом
повторяет запрос.

```json
{"ok": true, "room": "r_leto", "level": "working", "level_label": "Рабочий",
 "never": ["…"], "at": 1790000000,
 "applied": [{"profile": "ivan-petrov", "ok": true,
              "changes": ["«ivan-petrov»: в список разрешений добавится «script execution via heredoc»"],
              "backup": "backups/autonomy/r_leto-ivan-petrov-20261118-231500.yaml",
              "single_query_mode": "deny", "allowlist_size": 2, "deny_size": 0}],
 "warnings": [], "message": "Уровень «Рабочий»: изменено профилей — 2."}
```

Что делает каждый уровень:

| Уровень | `command_allowlist` | `approvals.single_query_mode` | `approvals.deny` |
|---|---|---|---|
| `careful` | снимаются записи Team Room (учёт в `data/state/autonomy-applied.json`) | `deny` | записи Team Room снимаются, свои остаются |
| `working` | `+= ["script execution via -e/-c flag", "script execution via heredoc"]` | `deny` | без изменений |
| `free` | те же два класса | `approve` | `never` + свои записи (не затираются) |

Правила записи (жёсткие):

* пишем **только** в `HERMES_HOME` профилей комнаты; в песочнице — в поддельный дом
  (`<data>/hermes-home`), реальные конфиги не трогаются;
* перед правкой — копия `config.yaml` в `data/backups/autonomy/<комната>-<профиль>-<время>.yaml`
  (путь возвращается в `applied[].backup`);
* правку делает сам Hermes (`hermes_cli.config.load_config → save_config`) отдельным
  процессом (`server/approval_grant.py::op_apply`) — посторонние ключи конфига целы;
* после записи сервер **перечитывает** конфиг: расхождения попадают в `warnings`
  (`applied[].ok: false`), а не выдаются за успех;
* hardline-пол Hermes (удаление корня, формат диска, fork-бомба, shutdown) не
  снимается ни одним уровнем — он в коде Hermes, а не в конфиге;
* ошибка записи по одному профилю не отменяет остальные (`applied[].ok: false` +
  `warnings`); уровень комнаты записывается последним, чтобы настройка не врала;
* отзыв = смена уровня (`careful` возвращает «как было» по учёту Team Room).

`400` — нет/неизвестный `level` (`detail.allowed`), `never` не список, маска с
переводом строки; `404` — комната не найдена; `409 confirm_required` — нет
подтверждения, `409 no_profiles` — в комнате нет участников; `503 autonomy_unavailable` —
конфиги профилей недоступны.

### W27.3 Журнал: `GET /api/rooms/{id}/autonomy/journal?limit=60`

«Что агенты сделали сами по уровню»: строка = профиль, класс, время, карточка.
**Счётчика «нужно от вас» здесь нет** — журнал не пополняет очередь решений.

```json
{"room": "r_leto", "level": "working", "count": 2, "total": 2,
 "items": [{"at": 1790000100, "kind": "decision", "level": "working",
            "profile": "ivan-petrov", "class": "запуск скрипта через heredoc",
            "card": "Собрать отчёт по заявкам", "severity": "routine",
            "text": "уровень пропустил бы сам: запуск скрипта через heredoc — «once»"}],
 "note": "Что агенты сделали сами по уровню. Это не «нужно от вас»: счётчик здесь не растёт.",
 "at": 1790000200}
```

`kind`: `level` (смена уровня), `decision` (решение по блокировке), `auto`.
`severity`: `critical` (разрушительные классы и «наружу» — показываются отдельно,
заметно, с предпросмотром «что именно») | `routine`. То же поле появилось у каждой
блокировки в деталях запроса (`approvals[].severity`) — состояние и действия оно не
меняет.

### W27.4 Подсказки: `GET /api/rooms/{id}/autonomy/suggest?days=90&limit=10`

Обёртка `hermes approvals suggest --json` (только чтение; в тестах и в песочнице —
подставной CLI). Разрушительные классы Hermes не предлагает никогда — это его правило.

```json
{"room": "r_leto", "profile": "anna-lead", "available": true, "count": 1,
 "items": [{"pattern": "git *", "kind": "glob", "count": 4, "classes": ["git"],
            "example": "git status", "already": false, "severity": "routine",
            "text": "«git *» — одобряли 4 раз(а)"}],
 "message": "Нашлось предложений: 1", "days": 90, "at": 1790000300}
```

`available: false` — CLI не найден/не ответил: `items: []` и русское `message`
(это не ошибка страницы).

Поле `source` — откуда взялись подсказки: `hermes` (настоящий CLI) или `fake`
(подставной ответ песочницы/тестов, см. ниже).

**Подставные подсказки без запуска Hermes.** Переменная `TEAMROOM_SUGGEST_FAKE`:

* `TEAMROOM_SUGGEST_FAKE=1` — встроенный пример (`server/autonomy.py::FAKE_SUGGEST`),
  три предложения; настоящий `hermes` не вызывается вовсе;
* `TEAMROOM_SUGGEST_FAKE=<путь к .json>` — свой ответ в форме
  `{"proposals": [{"pattern", "kind", "count", "classes", "example"}]}`.

Переменная нужна песочнице и тестам: раньше маршрут поднимал настоящий CLI и на живой
машине падал по таймауту 25 с. В боевом режиме (переменная не задана) поведение прежнее.

### W27.5 Правило контракта

`contract.approval_rule(room)` учитывает уровень комнаты: на «Рабочем» добавлена
строка «запуск твоих скриптов (`python -c`, `python файл.py`, heredoc) и работа в
папке комнаты разрешены — спрашивать их не нужно; удаление, реестр, `sudo`, отправка
наружу остаются вопросом к человеку»; на «Свободном» — «запрещённое нельзя ни обойти,
ни согласовать: задай вопрос в `open_questions`» (в этом режиме спросить некого).

### W27.6 Проверки

* `python -m unittest tests.test_autonomy` — 47 тестов: форма записи, умолчания
  «только с моего да», вид блокировки (`critical`/`routine`), чтение профилей,
  предпросмотр трёх уровней, 409 без подтверждения, запись `working`/`free`/`careful`
  (включая «чужие записи целы» и «наши сняты»), идемпотентность, пропуск отсутствующего
  профиля, честный отказ при ошибке записи, журнал, подсказки и проба через подставной
  CLI, протокол `approval_grant.op_apply` (подставной `hermes_cli.config`), правило
  контракта, вид в `approvals`, чтение старой комнаты без записи.
* `python tools/probe_autonomy.py` — песочница с поддельным `HERMES_HOME` и подставным
  CLI: 9 команд × 3 уровня; на «Рабочем» рутина (`python -c`, heredoc, `python файл.py`,
  `git status`) проходит, критичное (`rm -rf`, `taskkill /F`, `sudo`, `curl … | sh`,
  реестр) блокируется. Код возврата 0 — расхождений нет.

## W27-DUTIES — «Дежурства комнаты» целиком (пакет W27-E7, §3 правил волны)

Основа — штатный Hermes: расписание и запуск живут в `cron/jobs.json`, приложение
**не хранит второй список расписаний**. Правила реакции, корзина и история — в
`data/state/<room>/duties.json`; предложения — в `data/state/<room>/duty-proposals.json`.

Все записи в Hermes идут ровно одной командой (`shell=False`, `HERMES_HOME` из
конфигурации, таймаут 25 с):

| действие приложения | команда |
|---|---|
| создание, возврат из корзины | `hermes cron create <schedule> [<prompt>] --name … --deliver room:<id> …` |
| правка расписания/задания | `hermes cron edit <job_id> --schedule/--prompt/--name/--workdir …` |
| пауза, возобновление, запуск | `hermes cron pause|resume|run <job_id>` |
| удаление | `hermes cron remove <job_id>` |

В тестах и песочнице CLI подставной: `duties.set_cli_runner(fn)`, `TEAMROOM_HERMES_BIN`,
`TEAMROOM_DUTIES_DRY=1` (команда печатается, но не выполняется).

### W27-DUTIES.1 `GET /api/duty-templates`

Семь универсальных шаблонов (без терминов бизнеса): почта/ящик, папка, утренний обзор,
страница/цена, сроки, еженедельный отчёт, выполнение шагов.

```json
{"generated_at": 1763500000,
 "kinds": [{"id": "model|script|monitor", "title": "…"}],
 "on_change": [{"id": "log|inbox|delo|step|question", "title": "…"}],
 "levels": [{"id": "self|propose|ask", "title": "…"}],
 "notify": [{"id": "today|room|windows", "title": "…"}],
 "templates": [{"id": "mail", "title": "Проверять почту/ящик на новое", "why": "…",
                "hint": "…", "kind_title": "…", "on_change_title": "…", "level_title": "…",
                "schedule_human": "каждые 30 минут",
                "wizard": {"kind": "monitor", "schedule": "*/30 * * * *", "prompt": "…",
                           "script": "", "monitor_script": "room-mail.sh", "monitor_url": "",
                           "changed_rule": "…", "on_change": "inbox", "level": "propose",
                           "notify": ["today"], "catch_up": null, "continuity": true}}]}
```

### W27-DUTIES.2 `POST /api/rooms/{room}/duties`

Поля мастера: `title`, `kind` (`model|script|monitor`), `schedule` (cron `0 9 * * 1-5`,
`30m`, `every 2h`, `every day at 9am`), `prompt`, `script` (имя файла из каталога
скриптов Hermes — без путей), `monitor_script`, `monitor_url`, `workdir` (полный путь),
`skills`, `model`, `provider`, `catch_up`, `continuity`, `paused`, `changed_rule`,
`on_change`, `level`, `notify`, `source` (`manual|manager|dictation|delo`), `template`.

* `preview: true` → **200** `{command, argv, duty, rule, changes[], warnings[], next}` —
  точная команда и что изменится; ничего не создаётся и CLI не зовётся.
* `preview: false` без `confirm: true` → **409 `confirm_required`**
  (`detail.command` — та же команда).
* `preview: false, confirm: true` → **201** `{applied, job_id, command, duties[], rule,
  changes[], warnings[], job}`; задание привязано к комнате (`rooms.json → duties`),
  правило сохранено в `duties.json → rules[<job_id>]`.
* Ошибки: `400 bad_request` (пустое название, неразборчивое расписание, скрипт с путём,
  нет задания для модели, относительный `workdir`), `422 path_denied`, `502 cli_failed`
  (ненулевой код Hermes — неудача не выдаётся за успех).

Задание крона автоматически дополняется контрактом исполнителя — блоком `результат`
(`contract.duty_result_block()`), иначе приложение не поймёт, что нашло дежурство.

### W27-DUTIES.3 `PATCH /api/duties/{job_id}`

`schedule`, `prompt`, `title`, `workdir`, `monitor_script`, `monitor_url` — через
`hermes cron edit` (нужен `confirm: true`, иначе **409**); `changed_rule`, `on_change`,
`level`, `notify`, `source`, `template` — только правило (внутреннее состояние,
применяется сразу); `pause: true|false` — `cron pause|resume`; `run_now: true` —
`cron run`. Ответ: `{ok, applied, job_id, command, argv, changes[], rule?, job?, text}`.
`preview: true` — тот же ответ без применения.

### W27-DUTIES.4 Корзина, история, пробный прогон

* `DELETE /api/duties/{job_id}?confirm=true` → `cron remove`, задание снимается с
  комнаты, запись с правилом уезжает в `trash[]` на 30 дней (`delete_after`).
  Без `confirm` → **409** с `detail.note` «можно вернуть». Ответ: `{applied, command,
  trash: {job_id, delete_after_text, days}}`.
* `POST /api/duties/{job_id}/restore {confirm:true}` → `cron create` из корзины,
  новое `job_id`, привязка и правило восстанавливаются (**201**). Без `confirm` — 409,
  запись при этом остаётся в корзине. Просроченное (30 дней) — **410 `trash_expired`**.
* `GET /api/duties/{job_id}/history` → `{job_id, room, rule, items: [{run_id, job_id, at,
  changed, done, found, proposals[], links[], created[], on_change, level, silent}]}`
  (новые сверху, до 40).
* `POST /api/duties/{job_id}/dry_run` → `{ran:false, side_effects:"none", checks:{kind,
  prompt, script, monitor_script, monitor_url, workdir}, last_result, last_excerpt,
  command}`: ничего не запускает и не пишет, показывает, что проверяется и что нашлось
  в прошлый раз.
* `GET /api/duties?room=<id>` дополнен ключами `rules` (карта `job_id → правило`),
  `trash[]` и `rule` у каждого пункта.
* `POST /api/rooms/{room}/duties/process` — тот же проход, что делает тик сторожа
  (`{processed, cards, self, silent, logs, skipped, limit}`); нужен кнопке «Обновить».

### W27-DUTIES.5 Результат запуска → «Входящие» / Дело (§3)

Машинный блок в конце ответа агента (```результат … ```): `изменилось: да|нет`,
`сделано`, `обнаружено`, `предложения: [{что, почему, действие}]`, `ссылки`.

`duty_rules.process_runs(room)` (тик сторожа, `watcher._duties_tick`):

* лимит **≤ 5 обработок за тик**, идемпотентность по `run_id` (`<job_id>:<файл вывода>`);
* `изменилось: нет` (или блока нет) → одна тихая строка в `history[]`, без «ждёт вас»;
* `да` + `on_change`:
  * `log` — только история;
  * `inbox` / `question` — карточка во «Входящих» (`source:"duty"`, `duty:{job_id,
    run_id, done, found, proposals, links, level, on_change, notify}`) с текстом
    «Что выполнено / Что обнаружено / Предлагаю» и кнопками `accept | edit | reject |
    make_rule` (+ `delete`);
  * `delo` / `step` — при `level:"propose"|"ask"` та же карточка; при `level:"self"`
    сразу создаётся Дело (обратимая запись внутри комнаты), карточки нет;
* **наружу ничего не уходит**: ни письма, ни платежа, ни задачи в чужой системе —
  только предложение человеку.

Решение по карточке — `POST /api/rooms/{room}/duty-cards/{item_id} {action}`:
`accept` (создаёт Дело, `resolution.delo_id`; повтор → **409 `already_decided`**),
`edit` (текст «что сделать иначе»), `reject` («Не надо»), `make_rule` (правило
→ `level:"self"`; правила нет → **409 `no_rule`**), `delete`. Неизвестная карточка —
**404 `not_found`**.

### W27-DUTIES.6 Предложения руководителя и диктовка

* `GET /api/rooms/{room}/duty-proposals?state=proposed|all` → `{week_used, week_limit:2,
  declined[], items:[{id, title, why, when, what, state, source, wizard, actions:
  [create, edit, dismiss, never]}]}`. Перед чтением принимаются заготовки из «Входящих»
  (`inbox.json → duties_drafts`, D9 `to_duty`) — один раз на заготовку.
* `POST /api/duty-proposals/{id} {action, room}`: `create` (черновик мастера в `draft` +
  `next` — крон создаёт только `POST …/duties` с `confirm`), `edit`, `dismiss`,
  `never` («не предлагать такое» — название запоминается, повторные предложения
  пропускаются с `skipped:true, reason:"declined"`). Больше двух предложений
  руководителя за 7 дней → **409 `too_many_proposals`**.
* Предложение от руководителя в контракте — необязательный блок
  `contract.duty_proposal_rule()`; из Дела — `duty_rules.from_delo(cfg, room, delo_id)`
  (кнопка «Сделать регулярным»).

### W27.7 Проверки

* `python -m unittest tests.test_duties_full` — 42 теста: шаблоны, предпросмотр,
  обязательное «да», точный argv `create|edit|remove|pause|resume`, привязка и правило,
  корзина 30 дней и возврат, пробный прогон без CLI, разбор блока `результат`
  (русские и английские ключи, `да/нет/непонятно`), тихая строка и карточка с четырьмя
  кнопками, Дело по `level:"self"`, идемпотентность по `run_id`, лимит 5 за тик,
  лимит предложений 2 в неделю, «не предлагать такое», диктовка → черновик.
* `python tools/probe_duties_full.py --port 8857` — песочница + подставной CLI:
  30 проверок по HTTP, ни одного настоящего крона.

## W27-STALE — единые состояния пунктов, «Не актуально»/«Вернуть», счётчик (v1.4.0-dev, WAVE27 §1, пакет E1)

Основание — дефект 1 из `Vault/Projects/Developer/Team-Room/Defects-2026-11-18-evening-stale-needs-you.md`:
сервер отдавал `approvals[].state = "closed"`, интерфейс знал только `pending` — двадцать
закрытых пунктов рисовались как «ждёт вашего решения», счётчик не уменьшался, нажатие
давало `409`. Теперь состояния пунктов живут в **одном** модуле `server/stale.py`, и сервер
сам считает, что и сколько ждёт человека.

### W27.1 Состояния (единственный словарь)

| Пункт | Поле | Значения |
|---|---|---|
| вопрос | `open_questions[].state` | `open · answered · dismissed` |
| решение | `decisions[].state` | `open (proposed) · answered (обсуждаем) · accepted · rejected · dismissed · done` |
| шаг | `next_steps[].state` | `open · done · dismissed · cancelled` (`cancelled` = «снят по вашему ответу», в счётчик не входит) |
| разрешение | `approvals[].state` | `pending · granted_once · granted_always · denied · expired · closed · dismissed` |

Ждёт человека **только** `open` (у решения — `proposed`, у разрешения — `pending`).
У каждого пункта появились поля:

* `needs_you: bool` — правда ли он ждёт вас (учитывает наличие действия);
* `closed: bool` — пункт закрыт (не «ждёт»);
* `closed_reason` ∈ `card_finished · request_done · dismissed · superseded` +
  `closed_reason_label` по-русски («работа завершена», «запрос завершён», …);
* `actions: [{id:"dismiss", label:"Не актуально"}]` у ожидающего и
  `[{id:"restore", label:"Вернуть"}]` у снятого.

`status`/`answer_state` у пунктов не изменились — новые поля добавлены рядом.

### W27.2 Счётчик запроса

`GET /api/requests/{id}` → `needs_you_count`:

```json
"needs_you_count": {"questions": 1, "decisions": 1, "approvals": 0, "steps": 1, "total": 3}
```

Считает только пункты с действием; `questions` и `open_questions` — один источник (без
двойного счёта). Интерфейс показывает это число и **не пересчитывает** список.
Рядом: `stale: {request_finished, auto_close, open:{questions,decisions,approvals,steps,total},
auto_closed, reasons, labels}` — `open` для кнопки «Закрыть все неактуальные (N)».

`approvals_pending` и `approvals_actionable` остаются как были (W26-D1).

### W27.3 «Не актуально» и «Вернуть» (одно действие на все виды)

```
POST /api/requests/{rid}/actions
{ "action": "dismiss" | "restore",
  "target": {"kind": "question|decision|approval|step", "id": "q_…|d_…|ap_…|ns_…|t_…:ns_…"},
  "reason": "необязательно, ≤ 1000 знаков" }
```

* вопрос — отметка в реестре ответов комнаты (`dismissed` / снова `open`), в ветке метка
  `[Михаил · закрыто как неактуальное: вопрос q_… «…»]`;
* решение — тот же механизм, что «Снять — неактуально» / «Вернуть» (W20-D1);
* разрешение — реестр разрешений (`approvals.act_block("dismiss")` / `approvals.restore`);
* шаг — `steps_dropped` + статус пункта бэклога (`dismissed` / `open`).

Ответ `200` — обновлённый пункт (`state`, `needs_you`) и `message`. Повторное нажатие —
`409 already_decided` (`{"error":{"code":"already_decided","detail":{"state":…}}}`):
для интерфейса это **не ошибка** — тихая перерисовка и один тост на причину.
Пункт, решённый человеком (ответ, принятое решение, выполненный шаг), снять нельзя — `409`.

### W27.4 Массовое «Закрыть все неактуальные»

```
POST /api/rooms/{room}/dismiss_stale  {"scope": "finished_requests"}
→ { "ok": true, "scope": "finished_requests", "room": "…",
    "closed": {"questions": 2, "decisions": 1, "approvals": 0, "steps": 3},
    "already_closed": {…}, "total": 6, "requests": [{"request": "t_…", "closed": {…}}],
    "errors": [], "message": "Закрыто как неактуальное: 6." }
```

Закрывает пункты запросов со статусом `done`/`archived`. Решённое человеком не трогает.
Повторный вызов безопасен: уже закрытое попадает в `already_closed`, а не в ошибки.
`scope` кроме `finished_requests` — `400`.

### W27.5 Автозакрытие

* **разрешения** у завершённой карточки закрываются сами — как раньше (`closed_reason:
  card_finished`, W26-D1);
* **все виды пунктов** у `archived`-запроса закрываются на чтении (`closed_reason:
  request_done`, `auto_closed: true`) — в счётчик и в «Сегодня» они не входят;
* у `done`-запроса автозакрытия **нет**: «руководитель завершил карточку, чтобы спросить» —
  рабочее состояние «ждёт вашего слова», а не мусор. Такие пункты закрывает человек
  («Не актуально» / массовая кнопка).

### W27.6 «Сегодня»

`GET /api/today` не показывает пункты `archived`-запросов (автозакрытие §W27.5). Ключи
ответа не менялись — контракт W24-TODAY в силе.

### W27.7 Сторож

`python tools/check_dead_ends.py --base http://127.0.0.1:8851` теперь проверяет и это:

* состояние пункта вне словаря §W27.1 — проблема (`kind: "state"`);
* `needs_you_count` не совпадает с числом пунктов с действием — проблема (`kind: "counter"`);
* `open` + `needs_you`, а действий нет — проблема (`kind: "dead_end"`);
* `--selftest` проверяет сами проверки.

### W27.8 Проверки

* `python -m unittest tests.test_stale` — 24 теста: словарь состояний, счётчик,
  автозакрытие (`archived` закрывает, `done` — нет), снять/вернуть вопрос, решение, шаг,
  `409 already_decided`, `400/404` на плохую цель, массовое закрытие и его повтор,
  рабочая комната не трогается, сторож понимает счётчик и состояния.
* `python tools/check_dead_ends.py --selftest`.

## W28-A — вложения человека: окно выбора файлов (v1.5.0, W28-A, дефект 4)

Причина: вложение проверялось тем же белым списком, что и ПОКАЗ файлов в браузере
(`config.path_allowed`). Файл из «Загрузок» давал `403 Вложение вне разрешённых путей`,
`.doc`/`.zip`/`.dwg` не прикреплялись вовсе, а чтобы приложить файл, человек копировал
путь в поле. Теперь вложение — действие человека: проверяем только «файл существует,
это файл, читается».

### W28-A.1 Правила приёма вложения (`server/attachments.py`)

* Корни и расширения **не ограничены**: «Загрузки», рабочая папка проекта, сетевой диск,
  `C:/Проекты/…` — принимаются.
* Отказ только по делу, и всегда с действием:
  `400 empty_path` — «Путь вложения пуст — выберите файл кнопкой «Прикрепить файлы»…»;
  `400 not_found` — «Недоступно: файла нет по пути «…». Проверьте путь или выберите файл заново.»;
  `400 not_file` — «…это папка, а не файл. Выберите файл.»;
  `400 busy` — ««имя» занят другой программой — закройте файл и повторите.»;
  `400 unreadable`/`bad_path` — «не читается (…), скопируйте путь заново».
* Секреты (`.env`, `.env.*`, `*.key`, `*.pem`, `*.pfx`, `id_rsa`, `google_token.json`,
  `credentials.json`, `auth.json`, имена с `secret|token|password|credential`) —
  **не запрет, а вопрос**: `409 needs_confirm` + текст «похож на секрет… нажмите
  «Всё равно прикрепить»». Повтор запроса с `confirm_secret: true` файл принимает.
* Предел — **30** вложений (`app_common.MAX_ATTACH`, было 10). Повторы путей
  отбрасываются, порядок сохраняется.
* Белый список `config.path_allowed` остался там, где он и нужен, — в показе файлов
  (`GET /api/file`, `/api/files/launch`).

### W28-A.2 `POST /api/files/pick` — окно выбора файлов Windows

Тело: `{title?}`. Сервер открывает `System.Windows.Forms.OpenFileDialog` (`Multiselect`,
фильтр «Все файлы»), запуск — через `server/proc.py` без мигающей консоли
(`CREATE_NO_WINDOW`) с невидимым окном-владельцем `TopMost` (иначе диалог уходит за окно
браузера). Ответ: `{ok, paths: [...], cancelled: bool, count, total, dropped, max}`.

`200` — пути (браузер полный путь из `<input type=file>` не отдаёт, поэтому диалог
серверный). `cancelled: true` — человек закрыл окно (не ошибка). `dropped > 0` — выбрано
больше предела, лишние не взяты (интерфейс говорит об этом).
`501 pick_unavailable` — не-Windows или нет PowerShell: «Вставьте путь в поле «Путь к
файлу» — можно несколько, по одному на строку». `504 pick_timeout` — окно не дождалось
ответа (по умолчанию 300 с, `TEAMROOM_PICK_TIMEOUT`). `500 pick_failed` — диалог упал
(`detail.stderr`).
Песочница/демо/тесты подменяют диалог списком: `TEAMROOM_PICK_FAKE` — пути через перевод
строки или JSON-массив; пустое значение = «окно закрыли».

### W28-A.3 `POST /api/files/attach-check` — проверка до отправки

Тело: `{attach: [...], confirm_secret?: bool}` (принимается и `paths`).
Ответ: `{ok, items: [{path, ok, code, message, name, size, ext, secret}],
needs_confirm: [...], accepted: [...], max}`. Ошибкой считается только кривое тело
(`400 attach`/`400` на 31 путь): непригодный файл — это `ok: false` у своего элемента,
чтобы причина стояла рядом с файлом, а не одной общей ошибкой.

### W28-A.4 `POST /api/files/attach-open` — «Открыть» / «Открыть в папке» у чипа

Тело: `{path, mode?: "open"|"reveal"}`. Проверка — та же, что у вложения (файл есть, это
файл, читается), **без** белого списка папок: файл выбрал человек. `200` →
`{ok, path, mode, opened, revealed, note?}`. Исполняемые типы (`.exe`, `.bat`, `.lnk`,
`.ps1`, …) не запускаются: `mode` становится `reveal`, в `note` — «Программу из вложения
не запускаю — показал файл в папке». `400` — файла нет (`code` из W28-A.1); `500
launch_failed` — нет программы по умолчанию.

### W28-A.5 Тело запроса и сообщения

`POST /api/rooms/{id}/requests` и `POST /api/requests/{id}/messages` принимают
`attach[]` по правилам W28-A.1 (белого списка папок больше нет) и
`confirm_secret?: bool` — «Всё равно прикрепить». `server/thread.py::_attach_paths`
использует ту же проверку, поэтому уточнение по запросу принимает файл из «Загрузок».

### W28-A.6 Интерфейс (`web/js/views/composer.js`, `web/css/composer-attach.css`)

Скрепка подписана «Прикрепить файлы» и открывает окно Windows. Чип вложения: имя, размер,
«Открыть», «Открыть в папке», «Убрать». Секрет до подтверждения — красноватый чип с
причиной и кнопкой «Всё равно прикрепить»; отправка такого запроса не уходит молча
(подсказка + файл на виду). Поле пути осталось запасным путём (несколько путей — по
одному на строку), оно раскрывается само, если окно недоступно.

## W28-B — действия разрешений своими именами (v1.4.0, WAVE28 §B, дефект 6)

19.11.2026: кнопки карточки «нельзя штатно» отвечали `400 «Неизвестное действие»`, хотя
серверный обработчик был написан и протестирован. Причина — на стыке: интерфейс слал
`{"action":"hint_agent"|"trust_profile", "approval_id":…}`, а `ACTION_ALLOWED` этих имён
не знал. Исправлено, контракт ОДИН:

```json
{"action": "hint_agent",    "approval_id": "ap_3f7c1a90", "note": "необязательно"}
{"action": "trust_profile", "approval_id": "ap_3f7c1a90", "ttl": "card" | "min30" | "forever", "ttl_min": 30}
```

| Что | Значение |
|---|---|
| `ACTION_ALLOWED` | + `hint_agent`, `trust_profile` (обработчик — `routes_write.h_request_actions` → `routes_approvals._approval_action` → `approvals.decide` → `approvals.act_block`) |
| `ACTION_REQUIRED` | `hint_agent`/`trust_profile` → `("approval_id",)`; нет его → `400` с `missing: ["approval_id"]` |
| `ttl` | `card` · `min<NN>` (то, что шлёт карточка доверия: `min30`) · `forever` · число минут · `ttl_min` (1…1440) |
| без срока | `422 ttl_required` (было `400`: это невыполнимое действие, а не кривое тело) |
| `floor` | `422 floor` («доверие не снимает жёсткий запрет Hermes») |
| `grantable` | `422 grantable` («разрешается обычным способом — доверие не нужно») |
| повтор | `409 already_decided` — тихая перерисовка, не 500 |
| неизвестное имя | `400 bad_request`, в `error.detail.allowed` — полный список допустимых имён |

Те же имена по-прежнему принимаются и полем `decision` при `action:"approval"` (W26.3) —
второго контракта нет.

**Состояния честные.** `approvals[].state` отдаёт `workaround` / `info` / `hinted` /
`trusted` как есть (`server/stale.py`: `APPROVAL_EXTRA_STATES`), а не подменяет их словом
`pending`: раньше подмена делала недоговорную блокировку «вопросом к вам» и завышала
`needs_you_count`. В счётчик входит только `pending` с непустым `actions[]`.

**Интерфейс (дефект 6, вторая половина).** Экран знает эти состояния (`web/js/views/
approvals.js`: `APPROVAL_STATES`, `ACTIONABLE_STATES`), поэтому у карточки «нельзя штатно»
есть кнопки «Подсказать участнику…» / «Доверять…» / «Не актуально». Ошибка действия
остаётся в карточке **постоянной строкой** с причиной и кнопкой «Повторить» (тост исчезает,
причина — нет); повтор отправляет то же действие заново и не глотается.

**Проверки.** `tests/test_contract_actions.py`: (1) python читает `web/js/**` и сверяет
КАЖДОЕ имя действия, которое уходит на `/api/requests/{id}/actions`, с `ACTION_ALLOWED` /
`ACTION_REQUIRED` (неизвестное имя — провал; таблица исключений сама проверяется на
гниение); (2) живой HTTP на порту 8853 в песочнице: `hint_agent` → `hinted`,
`trust_profile` с `ttl` → `trusted` и запись только в поддельный профиль, без срока →
`422 ttl_required`, `floor` → `422 floor`, повтор → `409`. `tools/check_dead_ends.py
--press`: нажимает кнопку каждого ждущего пункта и требует смены состояния.

---

## W28-E — расход токенов запроса

### `GET /api/requests/{id}/usage`

Сколько стоил запрос: вызовы модели, токены, стоимость и строка для интерфейса.
Только чтение: `state.db` профилей Hermes открывается с `mode=ro`, доска — тоже.

**Источники.** Карточки запроса — реестр (`extra_cards`) и дерево (`detail.tree`);
сессии карточек — `task_events` доски (`kind = "owner_admitted"`: в `payload` лежат
`session_id` и путь к `state.db` профиля); токены и стоимость — `sessions` этого
`state.db` (`input_tokens`, `cache_read_tokens`, `cache_write_tokens`, `output_tokens`,
`estimated_cost_usd`).

**Ответ 200**

| Поле | Что значит |
|---|---|
| `rid` | корневая карточка запроса |
| `cards` | сколько карточек запроса нашли (корень + дерево + реестр) |
| `calls` | вызовов модели всего (`sessions.api_call_count`) |
| `tokens` | `{input, cache_read, cache_write, output, reasoning}` |
| `cost_usd` | сумма `estimated_cost_usd` (оценка, как в «Сессиях») |
| `split` | `{base, body, tools}` — база первого вызова / всё остальное / схемы инструментов |
| `models` | `[{model, calls, cost_usd, sessions}]` |
| `mismatched` | `{attempts, cards}` — «не та модель»: попытки `reclaimed` и смена модели |
| `line` | готовая строка: «Расход: N выз. / M тыс. токенов (база / тело / инструменты)» |
| `source` | `state.db` · `fixtures` (песочница/демо) · `none` (сессий ещё нет) |
| `warnings` | почему цифры такие (например «данные фикстуры») |

**Ошибки.** `404 not_root` — не корневая карточка запроса; `503 usage_unavailable` —
не удалось прочитать расход (повторяемо, без 500). Пустых нулей не бывает: нет данных —
`source: "none"` и строка «Расход: нет данных…».

**Интерфейс.** `web/js/views/usage-line.js`: строка под заголовком правой колонки
запроса + расшифровка (стоимость, модели, «попыток не на той модели: N»). Ошибка сети —
строка с причиной и кнопкой «Обновить».

**Замер тела карточки.** `python tools/measure_context.py` — состав первого запроса по
блокам (знаки и ≈токены) и база по `state.db`; `--json` сохраняет замер.

## W28-B — свод: ширина колонки и «Коротко» из metadata (v1.5.0, WAVE28b, дефекты 8 и 9)

Две правки интерфейса свода; новых маршрутов нет, формат ответа расширен **необязательным**
полем (старые клиенты его не читают).

### 1. `synthesis.metadata.svod.short` — готовая резолюция

```json
"synthesis": {
  "state": "ready", "version": 3, "text_md": "…",
  "metadata": {"svod": {"short": "По вашим ответам закрыты три условия… Остался один шаг: согласовать спецификацию."}}
}
```

| Поле | Тип | Что это |
|---|---|---|
| `metadata.svod.short` | string ≤ 700 | 2–4 законченных предложения: ответ на вопрос, главный вывод, что делать. Пишет модель (`metadata.svod.short` карточки), читает блок «Коротко» |
| `metadata.svod.headline` | string ≤ 700 | старое имя того же поля (совместимость), используется, если `short` нет |

* Сервер отдаёт **только** `short`/`headline` (`server/contract.py: svod_public_metadata`);
  заметка версии, `changes` и флаги остаются в дереве и в карточку ответа не копируются.
* Клиент (`web/js/svod-format.js: svodHeadline`) берёт `short`, если его нет — собирает
  «Коротко» из первого абзаца **целыми предложениями** до 700 знаков. Обрыв «…» запрещён:
  если резолюция длиннее 400 знаков, видна первая часть, остальное — «Читать далее».
* Контракт в карточке модели: `contract.summary_format_rule()` требует `metadata.short`
  (предел `contract.SVOD_SHORT_MAX = 700`).

### 2. Ширина свода

`web/css/svod.css`: у контейнера нет `max-width` (было `70ch` — узкая полоска на 1366+ px),
меру строки держит текст: `--svod-measure: min(120ch, 100%)` (абзацы, пункты списков,
заголовки). Таблицы, блоки кода, «Найдено» и плашки идут во всю ширину колонки; вторая
рамка `.req-answer__body { max-width: 720px }` снята (`view-request.css`).
Замер: `tools/probe_w25_modes.py` (строка свода 45–120 знаков), проверки —
`tests/svod-format.test.mjs`, `tests/w25-polish.test.mjs`.

