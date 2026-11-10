# Правила безопасности: реальный Hermes и Vault

Для всех исполнителей, агентов и инструментов проекта «Комната команды».
Дата: 28.10.2026. Обязательно к прочтению перед любым запуском тестов, скриптов
и «песочниц». Техническая опора — `server/safety.py`, `tests/helpers.py`,
`tests/test_safety.py`; журнал аудита — `docs/status/guardrails.md`.

## 1. Термины

| Что | Значение |
|---|---|
| **`cfg.hermes_home`** | **РЕАЛЬНЫЙ** корень Hermes: `%HERMES_HOME%`, иначе `%LOCALAPPDATA%\hermes` (`C:\Users\User\AppData\Local\hermes`). Здесь живут `config.yaml`, `profiles\<имя>\{config,profile}.yaml`, `auth.json`, `.env`, `skills/`, `memories/`, `kanban/boards/team/kanban.db`. **Только чтение.** |
| `cfg.kanban_home` | Куда смотрит движок. В песочнице — `<sandbox>/kanban_home`, в бою — реальный `hermes_home`. |
| **Vault** | `C:\TeamRoom\Vault` (зеркало комнат — `Projects\Rooms`). Пишут только боевой сервер и агенты. В Vault **нет git** — перезапись необратима. |
| **Песочница** | `tests.helpers.Sandbox`: временный каталог вне реального Hermes; внутри свои `kanban_home`, `data`, `vault`. |
| `cache/scratch` | `%LOCALAPPDATA%\hermes\cache\scratch` — единственный подкаталог реального Hermes, куда писать можно (рабочие файлы агентов). |

## 2. Запрещено (без исключений)

1. **Писать, удалять, переименовывать, копировать** в `cfg.hermes_home`:
   `config.yaml`, `profiles\*\profile.yaml`, `profiles\*\config.yaml`, `auth.json`,
   `.env`, `skills/`, `memories/`, `kanban/boards/team/*` — включая «безобидные
   заглушки для теста». Нельзя также `mkdir`, `unlink`, `os.replace`,
   `shutil.rmtree/move/copy*` по этим путям.
2. **Писать в `C:\TeamRoom\Vault`** из тестов и инструментов.
3. **Запускать агентский CLI**: `hermes kanban dispatch|decompose|swarm`,
   `hermes profile|config|auth|update|setup`, `hermes gateway …` — и вообще
   `hermes` с этими глаголами в любом месте argv.
4. **Дёргать HTTP без подмены CLI**: `POST /api/dispatcher/kick`,
   `POST /api/requests/*/actions` с `dispatch`/`decompose`/`swarm`.
   «Песочница» перенаправляет только доску — профили, ключи LLM и Vault остаются
   реальными, поэтому такой запрос поднимает настоящих агентов.
5. Считать `cfg.hermes_home` «песочницей» только потому, что `cfg.sandbox=True`.
   Песочница — это `TEAMROOM_HERMES_HOME`/`TEAMROOM_DATA`/`TEAMROOM_VAULT_DIR`,
   а `hermes_home` в `Cfg` всегда реальный.

## 3. Как делать правильно

### Тесты

```python
from tests.helpers import Sandbox

with Sandbox() as sb:              # сам включает оба запрета
    ...
```

`Sandbox.__enter__` вызывает `server.safety.forbid_real_writes()` и
`forbid_real_subprocess()`; `__exit__` их снимает. Пока контекст активен, любая
попытка записи в реальный Hermes/Vault (включая `open(...,'w')`,
`Path.write_text/write_bytes/mkdir/unlink/rename`, `os.replace/remove/rename/rmdir`,
`shutil.rmtree/move/copy*`, `os.open` с флагами записи) даёт `SafetyError`.
Временный каталог и `cache/scratch` разрешены.

Если нужен «фальшивый Hermes» (например, ростер профилей для conformance):

```python
from server import safety

fake_home = Path(sb.root) / "fake_hermes_home"      # ВНУТРИ песочницы
safety.assert_not_real(fake_home, "ростер профилей")
safety.guarded_mkdir(fake_home, parents=True, exist_ok=True)
safety.guarded_write_text(fake_home / "config.yaml", "...")
safety.guarded_copy2(real_gateway_state, fake_home / "gateway_state.json")  # читать реальное можно
```

Точечная проверка произвольного пути: `safety.assert_not_real(path, "что делаем")`
(бросает `SafetyError` по-русски), `safety.is_under_real(path)` — булев аналог.

### Инструменты `tools/*.py`

`main()` оборачивается:

```python
from server import safety

def main(argv=None):
    with safety.forbid_real_writes(), safety.forbid_real_subprocess():
        return _main(argv)
```

Так сделано в `e2e_ui.py`, `check_conformance.py`, `seed_sandbox.py`,
`probe_sandbox.py`. Новый инструмент без этого — нарушение.

### CLI в тестах — только shim

Подмена ставится **выше** `server.safety`: в `server.engine_cli`/`server.engine_write`
подменяется `subprocess` (или функция запуска) на объект, который записывает argv
и ничего не выполняет. Файл с таким тестом помечается комментарием `# SHIM-OK`
(см. `tests/test_write_security.py`, `tests/test_safety.py`). Статический тест
`tests/test_safety.py::StaticSubprocessScanTest` не пропустит `dispatch|decompose|
swarm|kick` в `subprocess` без этого маркера.

### Исключения из статической проверки

Если запись по пути, где в выражении встречается `hermes_home`, действительно
безопасна (путь заведомо внутри песочницы), ставится комментарий на строке или
строкой выше:

```python
cfg.hermes_home.mkdir(parents=True, exist_ok=True)  # SAFETY-OK: fake_hermes внутри sb.root
```

Без комментария `tests/test_safety.py::StaticWriteScanTest` упадёт.

## 4. Проверка «до/после» — обязательна

1. **Конфиги (22 файла):**
   `python C:/Users/User/AppData/Local/hermes/cache/scratch/hashcfg.py`
   до и после работы; вывод — в тот же `cache/scratch`, не в `/tmp`.
   Хэши `config.yaml` и `profiles\*\profile.yaml` должны совпасть.
2. **Реальная доска:** `snapshot_real_db()` до, `assert_real_untouched(before)`
   после (открывается только `mode=ro`).
3. **Vault:** список файлов с `ctime/mtime` до и после, если тест хоть косвенно
   касается ingest/агентов. Эталон — `docs/critic/wave4-backend.md` §0.
4. Любое расхождение — стоп, разбор, запись в `docs/status/guardrails.md`.

## 5. Если нарушение уже произошло

1. **Остановиться.** Не удалять и не «дочищать» следы: в Vault нет git, удаление
   уничтожит единственную копию.
2. Зафиксировать: путь, время (локальное и UTC), что именно изменено
   (`sha256`, размер, `mtime`), чем запущено.
3. **Восстановить конфиги из бэкапов Hermes:**
   - `%LOCALAPPDATA%\hermes\backups\config\config.yaml.good.<YYYYMMDD-HHMMSS>`
   - `%LOCALAPPDATA%\hermes\profiles\<имя>\backups\config\config.yaml.good.<…>`
   - `%LOCALAPPDATA%\hermes\profiles\<имя>\backups\config\profile.yaml.good.<…>`
   Берётся последний `good.*` **до** времени инцидента; после восстановления
   повторить `hashcfg.py` и сравнить с эталоном.
4. Vault: сверить файлы по `ctime/mtime` с временем запуска; решение
   «оставить/удалить/восстановить» принимает владелец (Михаил), не исполнитель.
5. Записать разбор в `docs/status/guardrails.md` и уведомить оркестратора.
6. Если сработал `SafetyError` — это не сбой, а защита: не обходить, а переписать
   тест/инструмент на песочницу или shim.

## 6. Инцидент 28.10.2026 — в трёх строках

1. `tools/check_conformance.py` записал тестовые заглушки в `cfg.hermes_home`
   (реальный `%LOCALAPPDATA%\hermes`), затёр `config.yaml` и профили
   `biz-director`/`biz-critic`/`coder`; восстановлено из `backups\config`.
2. Тест послал `POST /api/dispatcher/kick` в «песочницу» без подмены CLI —
   `hermes kanban dispatch` запустил реальных агентов: 65 вызовов LLM,
   5,71 млн входных токенов, 5 файлов записано в реальный `C:\TeamRoom\Vault`.
3. Реальная доска не пострадала, но песочница оказалась изоляцией только доски;
   поэтому запрет записи и запрет агентского CLI теперь включены системно
   (`server/safety.py` + `Sandbox` + `main()` инструментов).
