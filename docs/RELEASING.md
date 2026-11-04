# Как выпустить версию

Короткая версия: **обновили `VERSION` и `CHANGELOG.md` → поставили тег → запушили тег → CI сам
собрал `team-room-<version>.zip`, `.tar.gz` и загрузил их в GitHub Release**. Установщики
качают архив из последнего релиза, поэтому пользователям ничего делать не нужно, кроме
повторного запуска установки.

Всё локально и без секретов: релизный workflow не требует ни токенов, ни ключей — только
встроенный `GITHUB_TOKEN`.

## Одна цифра правды — `VERSION`

`VERSION` в корне репозитория содержит единственную версию продукта, например `1.0.0`.
Её читают:

| Кто | Как |
|---|---|
| сервер | `server/config.py` читает `VERSION` при старте → `/api/health` отдаёт `app.version` (`1.0.0`) |
| плагин Hermes Desktop | `PLUGIN_VERSION` в `desktop-plugin/plugin.js` — сборка релиза сверяет его с `VERSION` и падает при расхождении |
| сборка | `tools/build_release.py` берёт версию из `VERSION`: имя папки, имя архива, тег `v<version>`, раздел в `CHANGELOG.md`, `package.json` |
| установщик | печатает версию скачанного архива (тег, с которого он собран) |

Нигде больше числа версии нет — специально, чтобы архив, сервер и плагин не разъезжались.

## Выпуск: по шагам

1. **Проверить, что тесты зелёные локально** (те же команды, что в CI):

   ```bash
   python -m unittest discover -s tests -p "test_*.py"
   node --test tests/
   python tools/check_conformance.py
   python tools/check_fixtures.py
   ```

2. **Обновить `VERSION`** — одна строка, без `v`:

   ```
   1.0.1
   ```

3. **Обновить `CHANGELOG.md`** — новый раздел сверху, ровно `## <version> — ГГГГ-ММ-ДД`.
   Без раздела текущей версии сборка релиза завершится ошибкой.

4. **Проверить сборку целиком** (ничего не публикует, пишет в `dist/`):

   ```bash
   python tools/build_release.py --no-git --run-tests --out-dir dist
   ls -l dist
   ```

   В конце сборщик сам проверяет: `VERSION` внутри копии, `PLUGIN_VERSION`, раздел
   CHANGELOG, `release_scan` (0 находок), наличие `install.ps1`, `server/app.py` в архиве.

5. **Коммит и тег** (тег обязан совпадать с `VERSION`, иначе релизный workflow остановится):

   ```bash
   git add -A
   git commit -m "Team Room 1.0.1"
   git tag -a v1.0.1 -m "Team Room 1.0.1"
   git push origin main --tags
   ```

6. **Дождаться workflow `Release`** (`.github/workflows/release.yml`). Он на теге `v*`:

   * сверяет тег с `VERSION`;
   * гоняет тесты, собирает обезличенную копию с `--run-tests`;
   * собирает `team-room-<version>.zip` и `team-room-<version>.tar.gz` (внутри одна папка
     `team-room-<version>/` с файлом `docs/RELEASE-TAG.txt`), кладёт рядом копии без версии
     — `team-room.zip` / `team-room.tar.gz` для ссылки «последний выпуск», считает
     `SHA256SUMS.txt`;
   * публикует их в GitHub Release через `softprops/action-gh-release`.

7. **Проверить результат**: `https://github.com/vorchun18-svg/team-room-for-hermes/releases/latest`
   и прямые ссылки

   ```
   https://github.com/vorchun18-svg/team-room-for-hermes/releases/latest/download/team-room-1.0.1.zip
   https://github.com/vorchun18-svg/team-room-for-hermes/releases/latest/download/team-room.zip
   ```

   Имя без версии — то, что качают установщики: оно всегда указывает на последний выпуск.
   Имя с версией — для точной ссылки на конкретный релиз и для установки старой версии.

   Скачать zip, распаковать, убедиться, что внутри есть `VERSION` с новой версией и
   `install.ps1` / `install.sh`.

Тег `v*` не запускает обычный CI (там `tags-ignore: ['v*']`) — на теге работает только
релизный workflow, чтобы не собирать одно и то же дважды.

## Откат

**Тег не переписываем.** Перезапись тега ломает уже скачанные архивы и кеш GitHub; вместо
этого выпускаем следующую версию.

| Что случилось | Что делать |
|---|---|
| В релизе ошибка, пользователи ещё не обновились | удалить Release (`gh release delete v1.0.1 --yes --cleanup-tag`) — тогда `releases/latest` снова указывает на предыдущий выпуск, установщик скачает его |
| Релиз уже скачали | выпустить `1.0.2` с исправлением (пункты 2–6): установщик всегда берёт последний релиз, поэтому все получат исправление при повторной установке |
| Плохой коммит в `main` | `git revert <sha>`, обычный PR, затем патч-версия |
| Нужна конкретная старая версия | `install.ps1 -Version v1.0.0` / `install.sh --version v1.0.0` — установщики умеют ставить тег напрямую (архив тега), если релиза с таким тегом нет |
| Надо просто откатить установку у себя | `install.ps1 -Uninstall` — папка `data/` с комнатами остаётся; `-RemoveData` удаляет и её |

Данные пользователя (`data/` рядом с программой) откат не затрагивает: установщик заменяет
программу и не трогает комнаты, журнал и настройки.

## Как устроены установщики

* **Windows** — `install.ps1` (одна команда PowerShell из README). Качает архив последнего
  релиза (`releases/latest/download/team-room.zip`), при отсутствии релиза берёт
  `archive/refs/tags/<tag>.zip`, и только потом `main`; распаковывает в
  `%LOCALAPPDATA%\TeamRoom` (`-Home` / `TEAMROOM_HOME`), ставит плагин в Hermes Desktop,
  включает автозапуск, поднимает сервер и открывает страницу. `-Version v1.0.0` — поставить
  конкретный тег, `-DryRun` — только проверить компьютер, `-Uninstall` — удалить.
* **macOS / Linux** — `install.sh`: те же шаги, установка в
  `${XDG_DATA_HOME:-$HOME/.local/share}/TeamRoom`, плагин в `~/.hermes/desktop-plugins/team-room`,
  автозапуск через launchd / systemd --user.
* Оба установщика печатают версию скачанного архива и путь, куда его положили: если версия
  в отчёте не та, что ждали, — значит скачался не тот тег.

Архив релиза содержит: `server/`, `web/`, `desktop-plugin/`, `reportkit/`, `tests/`,
`fixtures/`, `tools/`, публичные `docs/`, скриншоты, `README.md`, `LICENSE`, `CHANGELOG.md`,
`VERSION`, `install.ps1`, `install.sh`, `bin/`, `.gitignore`, `package.json`.

## Что проверяет CI

`.github/workflows/ci.yml` — на push в `main` и на pull request, матрица
**Windows + Ubuntu × Python 3.11 / 3.13**, Node 22:

1. `python -m unittest discover -s tests -p "test_*.py"`;
2. `node --test tests/`;
3. `python tools/check_conformance.py` (живой бэкенд против `docs/API.md`);
4. `python tools/check_fixtures.py`;
5. `python tools/build_release.py --no-git` и `python tools/release_scan.py <папка релиза>`.

Секреты workflow не нужны: `permissions: contents: read` в CI и `contents: write` только в
релизном (для загрузки файлов). Проверка YAML локально:

```bash
python -c "import yaml,glob; [yaml.safe_load(open(f, encoding='utf-8')) for f in glob.glob('.github/workflows/*.yml')]; print('ok')"
```

Тесты, которым нужен живой Hermes Desktop, GPU, сеть или Windows-плагин, сами себя
пропускают при `TEAMROOM_CI=1` (см. `tests/ci_marks.py`) — на чистом раннере падений нет.
