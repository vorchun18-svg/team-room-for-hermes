# API «Комнаты команды» — контракт HTTP+JSON

Версия: 2.1 (28.10.2026; волна 5 — «done + needs_you», единый путь STATE.md, стабильный ETag; список правок — §10). Источник истины по сущностям — `docs/ARCHITECTURE.md` §3–5, 7–8 и §12 (решения после волны 1).
Этот файл — формальный контракт: по нему параллельно пишутся бэкенд (`server/`) и фронтенд (`web/`).
Эталонные ответы — `fixtures/*.json`; живой мок — `python tools/mock_server.py` (порт 8778).

## Что изменилось в v2 (для фронтенда)

Режим `swarm` снят: запрос всегда `auto`, кнопки «Рой» в интерфейсе нет.
Поле `critic` теперь **объект везде**: в `RequestSummary` — короткий `CriticRef` (`{enabled, state, verdict, round}`), в `RequestDetail` — полный `CriticBlock` (`+ max_rounds, issues, task_id`). Булева значения `critic` в API больше нет.

Что нужно отрисовать нового:

1. **Чип критика на запросе**: `critic.state` → `off` (нет), `waiting` (ещё не начинал), `reviewing` («критик проверяет»), `approved` (принял), `revised` (отправил на доработку), `failed`. Рядом — `раунд N` (в ленте — `critic.round`, на странице — `critic.round` из `max_rounds`).
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
* `GET /api/file` — только из белых корней (`data/`, `C:\TeamRoom\Vault`, `C:\TeamRoom\Team`, `C:\TeamRoom\Agents`, `%LOCALAPPDATA%\hermes\kanban\boards\*\workspaces`) после `realpath`; чёрный список: `.env`, `auth.json`, `*token*`, `*secret*`, `*.session`, `google_*`. Нарушение → `403 forbidden`.
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
| `orchestrator` | string | профиль-директор (`biz-director`) |
| `critic` | string \| null | профиль-критик (`biz-critic`) |
| `members` | `Member[]` | участники комнаты, **кроме директора**; критик входит сюда же с `role:"critic"` (дублирует `room.critic`) |
| `context_files` | string[] | абсолютные пути, читаются агентом первыми |
| `project_dir` | string | папка проекта комнаты: абсолютный путь Windows или `""` (§6.2.1) |
| `mode_default` | `auto` | всегда `auto`: `swarm` не поддерживается (ARCH §12.2) |
| `critic_pass` | bool | «с критиком» по умолчанию |
| `max_rounds` | int | 1..2, сколько раундов доработки допускает критик (по умолчанию 1) |
| `launch_mode` | `plan_first` \| `auto` | «Проектный режим» **этой** комнаты: `plan_first` — направления ждут кнопки «Запустить» (по умолчанию), `auto` — стартуют сразу после карты (A1) |
| `max_parallel_streams` | int | 1..4, сколько направлений идёт одновременно; остальные ждут очереди (по умолчанию 2, A2) |
| `project_mode` | bool | «Проектный режим» включён: главный запрос → карта направлений. У новых комнат `true`, у записей без поля — `false` |
| `agent_notes` | `{profile: {text, updated_at}}` | особые указания участникам **этой** комнаты (§6.2.2): добавка к роли, файлы профиля не меняются; нет поля — `{}` |
| `agent_models` | `{profile: {provider, model, reasoning, updated_at}}` | модель/провайдер/мышление участника **только в этой комнате** (§6.11); нет записи — настройки агента |
| `seeded` | bool | комната создана сидом при первом запуске (а не пользователем) |
| `created_at` | int | epoch |
| `archived` | bool | в архиве |

`Member`: `{profile, role: "worker"|"critic"|"observer", note, pinned: bool, added_at: int}`.
`role` — роль в комнате. Директор (`orchestrator`) в `members` не входит; критик входит с `role:"critic"` и дублирует `room.critic` (это позволяет держать в комнате второго критика с той же ролью). `POST /api/rooms/{id}/members` не принимает директора и текущего `room.critic` — они меняются через `PATCH`.

`RoomSummary` = `{id, title, icon, archived, orchestrator, critic, members_count, project_dir, mode_default, critic_pass, max_rounds, launch_mode, max_parallel_streams, project_mode, counters, phase, updated_at}`:

* `counters` = `{working, needs_you, done_today}` — живые счётчики запросов комнаты.
  `working` — статусы `working|reviewing|synthesizing`; `needs_you` — запросы со статусом `needs_you` **и** запросы со статусом `done`, у которых `needs_you:true` (завершены, но Михаилу остался вопрос; см. §4); `done_today` — `done` c `finished_at` внутри текущих суток по поясу сервера (запрос `done + needs_you` попадает в оба счётчика — это сознательно: он и завершён сегодня, и ждёт ответа).
* `phase` = `{id, title, status}` — текущая фаза проекта (та же логика, что `roadmap.current`); `null`, если фаз нет.
* `RoomDetail` = `Room` + `counters`, `phase`, `dispatcher` (`{ok, state, warning}`).

### 3.3 `RequestSummary`

| Поле | Тип | Описание |
|---|---|---|
| `id` | string | `t_a1b2c3d4` (корневая карточка) |
| `room` | string | id комнаты (для привязанной через `adopt` карточки — из реестра, даже если `tenant` у неё `NULL`) |
| `title` | string | заголовок запроса |
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
| `next_steps` | `NextStep[]` | 3–7 шагов |
| `open_questions` | `OpenQuestion[]` | вопросы Михаилу/профилю |
| `decisions` | `Decision[]` | решения |
| `stage` | `Stage` | этап проекта |
| `future_questions` | `string[]` | вопросы «на будущее» |
| `artifacts` | `Artifact[]` | файлы работы |
| `report` | `ReportRef` | интерактивный отчёт |
| `tree` | `TaskNode[]` | дерево карточек, корень первым |
| `timeline` | `TimelineEvent[]` | хронология запроса (по возрастанию `at`) |
| `thread` | `ThreadItem[]` | диалог по запросу (§6.3, «Диалог по запросу»; необязательное поле: без него клиент работает по `timeline`) |
| `warnings` | `Warning[]` | мягкие проблемы чтения/нормализации |
| `brief` | bool | запрос — главный (бриф проекта): руководитель выдал карту направлений (§6.10). Необязательное, по умолчанию `false` |
| `plan_link` | `{stream_id, title, status}` \| null | запрос — под-запрос направления карты: какое направление его породило (§6.10). `null` у обычных запросов |
| `streams_summary` | `{total, running, done, status}` \| null | сводка карты для страницы брифа: сколько направлений, сколько идёт/готово, статус карты. `null` у под-запросов |

`RequestDetail` = `RequestSummary` + поля выше. Единственное исключение: одноимённое поле `critic` расширено — в сводке `CriticRef` (4 ключа), в детали `CriticBlock` (7 ключей, включает `CriticRef`). Оба — объекты; `bool` не принимается нигде.

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

`SynthesisVersion` = `{version: int, kind: "initial"\|"revision", task_id: string\|null, author: string\|null, at: int\|null, verdict: null\|"approve"\|"revise", note: string}`.
`kind` — `initial` (первый свод корня) или `revision` (доработка после критика или замечания Михаила). `verdict` — вердикт, из-за которого появилась следующая версия (`revise`), либо `approve`, если версия принята критиком; иначе `null`. `note` — короткая русская заметка («Учтены три замечания критика»).

`synthesis.at` может быть позже `finished_at` только у старых записей; в v2 `finished_at` — конец всего цикла, включая доработку.

`CriticBlock`:

| Поле | Тип | Описание |
|---|---|---|
| `enabled` | bool | был ли критик у запроса (`false` — проверки нет) |
| `round` | int | текущий/последний раунд (0 — критик ещё не начинал) |
| `max_rounds` | int | лимит раундов из комнаты (1..2) |
| `state` | `off \| waiting \| reviewing \| approved \| revised \| failed` | `off` — критик выключен (или вердикт закрытой карточки не распознан); `waiting` — критик включён, но проверки ещё не было (карточки критика нет или она не начиналась); `reviewing` — проверяет; `approved` — принял; `revised` — отправил на доработку; `failed` — карточка критика упала (на `status` запроса не влияет, см. §4) |
| `verdict` | `null \| "approve" \| "revise"` | последний вердикт |
| `issues` | `{where, problem, fix}[]` | замечания критика; пусто при `approve` |
| `task_id` | string \| null | карточка-критик (`t_…`), `null` пока её нет |

`issues[]` — русский текст: `where` («Раздел «Что сделать», п. 3»), `problem` (что не так), `fix` (что сделать). UI рисует их таблицей или списком; больше 10 не приходит.

`NextStep`: `{id, text, owner: string|null, due: "YYYY-MM-DD"|null, state}`; `state` — `open|asked|done|dismissed` (то же, что статус элемента бэклога).
`id` стабилен в пределах запроса: `ns_1…`, `ns_2…` (порядок из metadata).

`OpenQuestion`: `{id, text, for: string, answered: bool, answer: string|null, answered_at: int|null}`.
`for` — `"mikhail"` или профиль. `id` — `q_1…`.

`Decision`: `{id, text, why: string|null, status: "proposed"|"accepted"|"rejected", at: int|null, source_request: string}`. `id` — `d_1…`.

`Stage`: `{current: string|null, phases: StagePhase[]}`; `StagePhase` = `{id, title, status: "done"|"active"|"next"|"blocked", note: string}`.
Порядок фаз фиксирован: `idea → legal → brand → packaging → production → sales`.
`current` = первая фаза со статусом `active|blocked`, иначе последняя `done`, иначе первая `next`, иначе `null`.

`Artifact`: `{path, title, kind, exists: bool, size: int|null}`.
`kind`: `md|json|html|csv|image|pdf|docx|xlsx|pptx|other` (по расширению; неизвестное → `other`). `size` — байты, `null` если файла нет.

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
| `runs` | `Run[]` | попытки выполнения |
| `needs_human` | bool | карточка ждёт человека |
| `error` | `{code, message}\|null` | последняя ошибка |

`Run` = `{id: int, profile, status, outcome, started_at, ended_at, summary, error: string|null}`;
`status`: `running|done|failed|crashed`; `outcome`: `completed|failed|crashed|reclaimed|null`.

`TimelineEvent` = `{at, kind, task_id, actor, text}`; `kind`: `created|decomposed|promoted|claimed|started|commented|completed|blocked|unblocked|failed|archived`.
`actor` — профиль, `"mikhail"` или `"system"`; `text` — короткая русская строка.

`Warning` = `{code, message, detail?}`; коды: `legacy_metadata`, `missing_artifact`, `no_synthesis`, `report_invalid`, `schema_drift`, `partial_ingest`, `dispatcher_down`, `card_failed`, `outside_room` (ребёнок назначен профилю вне состава комнаты), `critic_failed` (карточка критика упала), `critic_rounds_exhausted` (лимит раундов исчерпан, ответ отдаётся как есть).

### 3.5 `TeamMember`, `Roadmap`, `BacklogItem`, `ReportIndexItem`, `JournalEvent`, `SearchHit`, `Profile`

`TeamMember` = `{profile, title, role, note, pinned, state, current, last, counts}`:
* `role`: `orchestrator | worker | critic | observer`.
* `state`: `idle | working | waiting`; `waiting` — есть blocked-карточка или вопрос к этому профилю.
* `current`: `{task_id, title, status, started_at}` \| `null` — что делает сейчас: активная карточка профиля (`ready|running|review`) либо, если активной нет, blocked-карточка, на которой он стоит (тогда `status:"blocked"`, `state:"waiting"`).
* `last`: `{task_id, title, status, finished_at, summary}` \| `null` — последняя завершённая работа.
* `counts`: `{done, blocked, active}` по карточкам комнаты.

`Roadmap` = `{room, updated_at, current: StagePhase|null, phases: StagePhase[], decisions: Decision[]}`.
`phases` — всегда 6 фаз в фиксированном порядке, с `note` (русский текст) и `updated_at` (int|null).
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

`Profile` = `{profile, title, description, tags: string[], kind: "director"|"worker"|"critic"|"utility", available: bool}`.

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
* `needs_you` = `status == "needs_you"` **или** неотвеченный вопрос `for="mikhail"` в последней версии ответа **или** blocked-карточка с `needs_human=true`. Вопрос, адресованный профилю (`for="biz-director"`), флаг не поднимает.
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

**stage.** Словарь «область → статус» превращается в 6 фаз:

1. Ключи приводятся к канону: `idea|vision|идея → idea`; `legal|law|юр* → legal`; `brand|роспатент|trademark|бренд → brand`; `packaging|pack|упаковка → packaging`; `production|ops|производство|операции → production`; `sales|vendas|продажи → sales`.
   **Коллизия ключей.** Сначала раскладываются ключи, совпадающие с id фазы (канонические: `idea, legal, brand, packaging, production, sales`), затем — алиасы. Алиас применяется **только если канонического ключа для этой фазы нет**: в реальном `t_605613e0` есть и `production`, и `ops` — оба значат фазу `production`, побеждает `production`, а `ops` не перезаписывает её и не сдвигает порядок обхода. Так результат не зависит от порядка ключей в JSON. Если канонического ключа нет, побеждает первый по порядку алиас (порядок алиасов перечислен выше).
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
`502 engine_error` — каталог профилей недоступен. → `fixtures/profiles.json`

#### `POST /api/dispatcher/kick`
Один проход диспетчера (`hermes kanban dispatch`): разбудить доску, если агенты «молчат».
Тело: `{}` (поля игнорируются). `200` → `{ok: true, spawned: int, reclaimed: int, promoted: int, dispatcher: {ok, state, warning}, at: int}`.
`502 engine_error` — CLI вернул ошибку (`detail.stderr`). `409 conflict` — диспетчер уже выполняет проход (`detail.reason="busy"`).

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
| `members` | `{profile, role?, note?, pinned?}[]` | нет | `[]` |
| `context_files` | string[] | нет | `[]` |
| `project_dir` | string | нет | `""` |
| `mode_default` | `auto` | нет | `auto` (только) |
| `critic_pass` | bool | нет | `false` |
| `max_rounds` | int | нет | `1` (допустимо 1..2) |
| `launch_mode` | `plan_first` \| `auto` | нет | `plan_first` |
| `max_parallel_streams` | int 1..4 | нет | `2` |
| `project_mode` | bool | нет | `true` (новая комната сразу в проектном режиме) |
| `agent_notes` | `{profile: {text, updated_at?}}` | нет | `{}` — особые указания участникам этой комнаты (§6.2.2) |

`201` → `RoomDetail` (§3.2), заголовок `Location: /api/rooms/{id}`.
`400 bad_request` — нет `title`/`orchestrator`, плохой `id`, неизвестный профиль (`detail.available`), `mode_default="swarm"` («не поддерживается»), `max_rounds` вне 1..2, негодный `project_dir` (§6.2.1), `launch_mode` не из `plan_first|auto` («Режим запуска: «сначала план» или «сразу без подтверждения»»), `max_parallel_streams` не целое 1..4 («Одновременно направлений: от 1 до 4»), `project_mode` не bool («ожидается true или false»).
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
{"ok": true, "profile": "biz-lawyer", "text": "Всегда проверяй по закону X", "cleared": false,
 "agent_notes": {"biz-lawyer": {"text": "Всегда проверяй по закону X", "updated_at": 1793100000}},
 "warnings": []}
```
`400 bad_request` — профиль не в составе («Указания можно задать только участнику комнаты: «coder» в составе нет»), текст длиннее 8 000 («Указания: 8001 знаков, а можно не больше 8000»), текст не строка, NUL. `404 not_found` — нет комнаты. Похожее на секрет (`sk-…`, `password=`, `token:`, `bearer …`) **не блокирует** запись, но приходит в `warnings[]` в замаскированном виде.

Также принимается:
* `POST /api/rooms` — `agent_notes` вместе с составом (профили сверяются с новым составом);
* `PATCH /api/rooms/{id}` — те же правила, профили сверяются с текущим составом.

Как указания доходят до агента (два канала, оба обязательны):
1. **Тело карточки** — раздел «Особые указания участникам этой комнаты» в блоке «КОНТЕКСТ КОМНАТЫ» (`context_pack.build_context_block`): `- <роль по-русски> (<профиль>): <текст>` + правило «Руководитель при раздаче подзадач обязан переписать указание участника в тело его карточки…». Лимит раздела — 3 000 знаков: длинный текст обрезается «…» с пометкой, полный лежит в Obsidian (`Rooms/<комната>/Указания участникам.md`).
2. **Автокомментарий** — watcher для карточек дерева запроса комнаты, чей исполнитель имеет указания, пишет одну строку `[Указание комнаты · <hash8>] <текст>` (через CLI `comment`, автор — сам исполнитель). Идемпотентно по `hash8`: повторно не пишется; при смене текста — новый комментарий с пометкой `(обновлено)`.

Приоритет в тексте: профиль → указания комнаты → текст запроса; в запросе Михаил может переопределить указание.

Зеркало в Obsidian (`vault_mirror.sync_room`): раздел «Особые указания» в `CONTEXT.md` и файл `Указания участникам.md` (служебный блок между маркерами, ручной текст вне маркеров сохраняется).

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
Частичное обновление. Разрешены поля: `title, icon, orchestrator, critic, context_files, project_dir, mode_default, critic_pass, max_rounds, launch_mode, max_parallel_streams, project_mode, agent_notes`.
Настройки проектного режима меняются только здесь — общих режимов «для всех комнат» нет (A1); `id`, `board`, `tenant` неизменяемы.
Пустой `launch_mode` (`""`) = рекомендуемый `plan_first`. PATCH не трогает `project_dir` и состав, если они не переданы.
`200` → `RoomDetail`. `400 bad_request` — неизменяемое поле (`id`, `board`, `tenant`) или неизвестное (`detail.fields`), `mode_default="swarm"`, `max_rounds` вне 1..2. `404 not_found`. `409 conflict` — `orchestrator`/`critic` не из ростера. Отклонённая правка ничего не меняет (проверка до записи).

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

#### `POST /api/requests/{rid}/actions`
Одно действие над запросом. Тело: `{action, ...поля}`.

| `action` | Поля | Что делает | Ответ (кроме `ok:true, action, request`) | Ошибки |
|---|---|---|---|---|
| `dispatch` | `task_id?` | проход диспетчера, чтобы карточку взяли | `{spawned, promoted, dispatcher}` | `502` |
| `comment` | `text` (1..8000), `author?`=`mikhail` | комментарий к корню | `{comment: {id: int, author, text, at}}` (`id` — номер строки `task_comments`; растёт; мок отдаёт тот же формат) | `400`, `404` |
| `answer` | `question_id`, `text` (1..8000) | ответ на вопрос: комментарий + `unblock` блокирующих детей, вопрос → `answered`. Ответ **сохраняется** в реестре комнаты (`requests.json → answers: {q_id: {text, at}}`): чтение накладывает его на `open_questions` (metadata run'а движок переписывать не даёт). Если после ответа открытых вопросов Михаилу не осталось, `needs_you` сбрасывается; `status` завершённого запроса остаётся `done` | `{question: OpenQuestion, unblocked: string[]}` | `400`, `404` (`detail.question_id`), `409` уже отвечен |
| `unblock` | `task_id?`, `text?` | снять блокировку (одну или все) | `{unblocked: string[]}` | `409` нечего разблокировать |
| `decompose` | — | **«Разложить сейчас»**: явный LLM-вызов декомпозиции (таймаут 180 с). Разрешён только по клику пользователя и только для `queued` | `{decomposed: true, child_ids: string[], dispatcher}` | `409` уже разложен (`detail.reason="already_decomposed"`, `detail.status`) |
| `revise` | `text` (1..8000) | «На доработку» от Михаила: создаёт карточку-доработку (`role=revision`, parent — критик или корень), ответ уходит в `synthesizing`; готовая доработка = новая версия | `{version: int, previous_version: int, revision_task_id: string, note: string}` | `400` пустой текст, `409` ответа ещё нет (`detail.reason="synthesis_pending"`) |
| `critique` | — | запустить критика вручную (повторная проверка текущей версии); `round += 1` | `{round: int, critic_task_id: string, state: "reviewing"}` | `409` у комнаты нет критика (`detail.reason="no_critic"`), `409` ответа ещё нет |
| `reassign` | `task_id`, `profile` | сменить исполнителя карточки; снимает `outside_room`, если профиль из состава комнаты | `{task: TaskNode}` | `400` не участник комнаты (`detail.available`), `404` нет карточки, `409` карточка done/archived |
| `rework` | `task_id?`, `text` (1..8000) | «на доработку» конкретной карточки (по умолчанию — критик/корень) с указанием | `{task: TaskNode}` | `400`, `404`, `409` карточка archived |
| `retry` | `task_id?` | перезапустить упавшие карточки | `{retried: string[]}` | `409` нет упавших |
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
| `author` | string | `mikhail`, профиль (`biz-director`) или `system` — ключ, не подпись |
| `author_label` | string | подпись по-русски: `Михаил`, `Глеб · руководитель комнаты`, `Антон · исполнитель`, `Диалог запроса` |
| `text` | string | текст комментария/события (события — короткая русская фраза) |
| `task_id` | string \| null | карточка, к которой относится запись |
| `kind` | `message \| comment \| event` | `message` — написал Михаил, `comment` — команда, `event` — системная строка (тонкая серая) |

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
`state=working` — есть активный run профиля; `waiting` — blocked-карточка или вопрос к нему. `404 not_found`.
→ `fixtures/team_cafe.json`

#### `GET /api/rooms/{id}/roadmap`
Этапы проекта и решения. `200` → `Roadmap` (§3.5). `404 not_found`. Если данных ещё нет — 6 фаз со статусом `next`, `current: "idea"`, `decisions: []`.
→ `fixtures/roadmap_cafe.json`

#### `GET /api/rooms/{id}/backlog`
«Дальше»: шаги, будущие вопросы, вопросы, решения. Query: `status` (через запятую), `kind` (через запятую), `limit` (по умолчанию 200).
`200` → `{room, updated_at, counts: {open, asked, done, dismissed, total}, items: BacklogItem[]}`.
`400 bad_request` — неизвестный `status`/`kind`. `404 not_found`.
→ `fixtures/backlog_cafe.json`

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

#### `GET /api/file?path=<абсолютный путь>`
Отдача файла из белого списка. `200` — байты; `Content-Type` по расширению, `Content-Length`, `Content-Disposition: inline` (для `docx/xlsx/pptx` — `attachment`).
`400 bad_request` — нет `path`. `403 forbidden` — вне корней или чёрный список (`detail.reason = "outside_roots"|"blacklisted"`). `404 not_found` — файла нет.
Range-запросы не поддерживаются (всегда `200`, не `206`). SVG отдаётся `image/svg+xml` с `Content-Security-Policy: sandbox`. Для `docx/xlsx/pptx` — `Content-Disposition: attachment; filename="<имя>"`, для остальных — `inline`.
Белый список корней: `data/`, `C:\TeamRoom\Vault`, `C:\TeamRoom\Team`, `C:\TeamRoom\Agents`, `%LOCALAPPDATA%\hermes\kanban\boards\*\workspaces`; дополнительно проверяются `realpath`, расширение (`.md .txt .json .csv .html .png .jpg .jpeg .gif .webp .svg .pdf .docx .xlsx .pptx .ics .log`) и чёрный список имён в последних 4 сегментах пути. Исходники `.py`, `.js`, `.css` через `/api/file` не отдаются.

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
Экспорт запроса: ответ, шаги, вопросы, решения, артефакты, дерево.
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
| GET | `/api/rooms` | `{rooms, engine, updated_at}` | `rooms.json` |
| POST | `/api/rooms` | RoomDetail (201) | — |
| GET | `/api/rooms/{id}` | RoomDetail | `room_cafe.json`, `room_fn.json` |
| PATCH | `/api/rooms/{id}` | RoomDetail | — |
| POST | `/api/rooms/{id}/archive` | `{ok, changed, room}` | — |
| POST | `/api/rooms/{id}/members` | `{member, members}` (201) | — |
| DELETE | `/api/rooms/{id}/members/{profile}` | `{ok, members}` | — |
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
| GET | `/api/ics` | `text/calendar` | — |
| GET | `/api/export/request/{rid}.md\|.html` | текст/HTML | — |
| POST | `/api/rooms/{id}/consolidate` | `{request, root_id}` (201) | — |
| POST | `/api/adopt` | `{ok, request}` | — |
| GET | `/api/stt/status` | `{available, loaded, loading, model, device, reason}` | — |
| POST | `/api/stt/transcribe` | `{text, raw, duration_s, elapsed_s, language, model, device, corrections}` | — |
| GET | `/api/stt/dictionary` | `{terms, replacements, updated_at}` | — |
| PUT | `/api/stt/dictionary` | `{terms, replacements, updated_at}` | — |
| POST | `/api/stt/learn` | `{suggestions, saved}` | — |
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
 "model": "large-v3-turbo", "device": "cuda", "reason": null}
```

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
 cached:    bool, updated_at: int, warnings: [string]}
```

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
    «по умолчанию: …»): `model.provider`, `model.default` и **только** `agent.reasoning_effort`
    из его `config.yaml`. `delegation.reasoning_effort` — уровень делегированных детей, за
    дефолт агента он не выдаётся. Пустая строка = «как в Hermes по умолчанию» (профиль без
    `model.default` тоже даёт пустую модель, а не чужую).

`POST /api/rooms` и `PATCH /api/rooms/{id}` принимают `agent_models` (та же форма, что в поле
комнаты; профили — только из состава комнаты, иначе `400`). Ошибки проверки — русский текст,
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

Применение: у комнаты обходятся деревья её запросов; карточки **этого** профиля в статусах
`triage/todo/ready/blocked` закрепляются на новой модели сразу (идемпотентно, событие
`model_override_set` в карточке). `running` и `done` не меняются: для идущих в ответе
`pending_running > 0` и работает `POST …/restart`. Изменение пишется в журнал комнаты
(`kind: "model_changed"`, «Модель участника «…» изменена: провайдер · модель · мышление»).

`400 bad_request` — профиль не в составе комнаты; задана модель без провайдера; провайдер не
настроен в Hermes («Провайдер «…» не настроен — в Hermes нет ключа»); модели нет в каталоге
провайдера (если каталог провайдера непуст — иначе разрешена «своя модель»; в `detail` —
`suggestions` (id, отличающиеся только регистром) и `other_providers` (у кого эта модель есть),
но применяются они только после явного выбора); уровень мышления вне списка. `404 not_found` —
нет комнаты. `503 engine_error` — реестр занят.

#### `POST /api/rooms/{id}/agents/{profile}/restart`
«Перезапустить на новой модели» — у **running**-карточек этого профиля в деревьях запросов
комнаты сначала закрепляется выбор комнаты (`model_override`/`provider_override`/
`reasoning_effort`; движок разрешает менять переопределение у идущей карточки — оно применится
к следующему запуску), затем они снимаются с работы (`reassign --reclaim`). Без замены комнаты
пин снимается, и карточка идёт на настройках профиля. Дальше карточки подхватывает диспетчер.
По умолчанию не вызывается — это явное действие Михаила.
`200` → `{ok: true, restarted: int, cards: [id], warnings: [string]}`; `restarted: 0` — идущих
карточек нет (не ошибка).

#### `RequestDetail` (страница запроса)
У узлов `tree[]` необязательное `model_label: "Anthropic · claude-sonnet-5-5 · высокий"` —
«провайдер · модель · мышление» **по факту карточки** (`null`, если у карточки нет переопределения).
Показывается мелко на странице запроса; в тексты для Михаила технические id не попадают.

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
* Комнат две: `cafe` (команда исполнителей, критик `biz-critic`) и `fn` (`default`, lawyer/coder/surveyor, критика нет) — `/api/rooms/fn/...` работает полностью.
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

## 9. Открытые вопросы контракта

1. `stage` из нескольких запросов: побеждает фаза из последнего завершённого запроса комнаты; ручные правки фаз в API не предусмотрены (только через новый запрос).
2. `done_today` считается по поясу сервера; если Михаил путешествует — счётчик может «скакнуть» на границе суток.
3. `critic` — объект в обеих формах: `RequestSummary.critic` = `CriticRef` (`{enabled, state, verdict, round}`), `RequestDetail.critic` = `CriticBlock` (`+ max_rounds, issues, task_id`). Так реализовано в `server/engine_read.py`; фронтенд читает `critic.issues` только на странице запроса.
4. `finished_at` — конец всего цикла, включая пост-синтез проверку. У legacy-карточек он равен завершению корня, и `synthesis.at` может быть больше `finished_at`; UI не должен на этом падать.
5. `max_rounds` больше 2 не поддерживается: движок не перепаивает граф, а каждый раунд — новая карточка. При исчерпании лимита ответ отдаётся как есть + `warnings[].code=critic_rounds_exhausted`.
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
