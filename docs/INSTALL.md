# Установка «Командных комнат»

Коротко: одна строка в PowerShell — и приложение на месте, плагин в Hermes Desktop
зарегистрирован, сервер уже работает. Всё остальное ниже нужно только если что-то
пошло не так или хочется поставить руками.

Приложение работает целиком на вашем компьютере: `127.0.0.1`, без облаков,
без регистрации и без сбора данных.

---

## 1. Одной командой

Windows 10/11, обычный PowerShell (права администратора **не** нужны):

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -Command "$s = irm https://raw.githubusercontent.com/vorchun18-svg/team-room-for-hermes/main/install.ps1; iex $s.TrimStart([char]0xFEFF)"
```

Что произойдёт:

1. Установщик **сначала проверит компьютер** и покажет таблицу: есть ли Python 3.11+,
   Hermes Desktop, свободен ли порт, сколько места на диске, есть ли `faster-whisper`,
   `ffmpeg`, `hermes` CLI. Под таблицей — список того, чего не хватает, и что именно
   это ломает.
2. Скачает готовый архив с GitHub (git не нужен) и распакует его.
3. Остановит прежний сервер, если он работал, и разложит файлы в
   `%LOCALAPPDATA%\TeamRoom`. Папка `data` с комнатами не трогается.
4. Зарегистрирует плагин в Hermes Desktop:
   `%LOCALAPPDATA%\hermes\desktop-plugins\team-room\plugin.js`
   (или в `%HERMES_HOME%\desktop-plugins\team-room\`, если переменная задана).
5. Включит автозапуск при входе в Windows и сразу поднимет сервер — без консольного окна.
6. Откроет `http://127.0.0.1:8777/` в браузере и напишет итог.

Дальше в Hermes Desktop: **левое меню → «Командные комнаты»** (или `Ctrl+K` → «командные»).

Только проверить компьютер, ничего не скачивая и не меняя, удобнее файлом: скачайте
`install.ps1` в любую папку и запустите с `-DryRun`. Тогда доступны и остальные
параметры (раздел 3):

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\install.ps1 -DryRun
```

---

## 2. Что нужно и что необязательно

Установщик проверяет это сам (шаг «Проверяю окружение»). «Обязательно» — без этого
дальше нельзя; «необязательно» — приложение работает, но в ограниченном режиме,
и оно честно об этом говорит.

| Что | Нужно? | Если нет |
|---|---|---|
| Windows 10/11 | обязательно | установка не пойдёт |
| **Python 3.11+** | **обязательно** | сервер не запустится вообще: установщик остановится и покажет команду установки |
| 300 МБ на диске | обязательно | скачивание и распаковка могут не закончиться |
| Свободный порт 8777 | обязательно | сервер не сможет занять место: возьмите другой порт `-Port 8788` |
| Интернет (github.com) | обязательно при установке из сети | поставьте из папки: `-SourceDir "D:\team-room"` |
| Hermes Desktop | необязательно | страница откроется только в браузере, строки в меню не будет |
| `hermes` CLI | необязательно | не пишет карточки в канбан: комнаты, журнал, отчёты и файлы работают, запись объясняется плашкой |
| `faster-whisper` | необязательно | диктовка недоступна: микрофон сообщает «распознавание не установлено» |
| `ffmpeg` | необязательно | часть аудиоформатов не конвертируется |
| Хранилище Obsidian | необязательно | работаем без зеркала; путь задаётся `TEAMROOM_VAULT_DIR` |

Поставить недостающее можно двумя способами.

**Самому, вручную** (эти же команды установщик показывает в отчёте):

```powershell
winget install --id Python.Python.3.12 -e          # Python 3.11 или новее
python -m pip install --user faster-whisper        # диктовка, ~400 МБ (+ модель 1–3 ГБ)
winget install --id Gyan.FFmpeg -e                 # ffmpeg, ~150 МБ
```

**Через установщик, с согласием на каждый пункт:**

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\install.ps1 -InstallDeps
```

Он спросит по каждому пункту (`Y/n`), напишет, что и сколько места займёт, и покажет
вывод установки. Без согласия ничего не ставится: без интерактивной консоли нужен
явный ключ `-InstallDeps -Yes`. Всё, что поставили, дописывается в
`%TEMP%\team-room-install-deps.log`.

---

## 3. Параметры установщика

| Параметр | По умолчанию | Что делает |
|---|---|---|
| `-Version <тег>` | последний выпуск, иначе `main` | какую версию скачать (`v1.0.0`, `main`) |
| `-Home <папка>` | `%LOCALAPPDATA%\TeamRoom` | куда ставить программу; данные — в `<Home>\data` |
| `-Port <номер>` | `8777` | порт сервера; запоминается для автозапуска |
| `-SourceDir <папка>` | — | поставить из уже распакованной папки, без интернета |
| `-NoAutostart` | автозапуск включён | не включать автозапуск (и убрать прежний) |
| `-NoBrowser` | страница открывается | не открывать браузер |
| `-DryRun` | — | только проверка окружения, ничего не менять |
| `-InstallDeps` | — | поставить недостающее (Python, `faster-whisper`, `ffmpeg`) с согласия |
| `-Uninstall` | — | удалить программу, плагин и автозапуск |
| `-RemoveData` | данные сохраняются | вместе с `-Uninstall` удалить и `data` |
| `-WaitSeconds` | `30` | сколько ждать ответа сервера после запуска |

Переменные окружения: `HERMES_HOME` (папка данных Hermes), `TEAMROOM_HOME`
(куда ставить), `TEAMROOM_DATA` (папка данных приложения), `TEAMROOM_PORT`,
`TEAMROOM_VAULT_DIR` (куда писать зеркало комнат).

---

## 4. Установка руками, без скрипта

1. Скачайте архив нужного выпуска с GitHub и распакуйте, например в
   `%LOCALAPPDATA%\TeamRoom`.
2. Проверьте Python: `py -3 -V` (или `python -V`) — нужна версия 3.11 или новее.
3. Поставьте плагин: `python tools\install_desktop_plugin.py` (при нестандартном порте —
   `--port 8788`).
4. Запустите сервер: `bin\team-room-server.cmd` (скрыто, с логом) или
   `python server\app.py` в консоли.
5. Откройте `http://127.0.0.1:8777/`. В Hermes Desktop появится «Командные комнаты»
   через несколько секунд; если нет — `Ctrl+K` → «Reload desktop plugins».

Автозапуск вручную: `schtasks /Create /городе ONLOGON /TN TeamRoomServer /TR "\"%LOCALAPPDATA%\TeamRoom\bin\team-room-server.cmd\"" /F`.
Если планировщик не даёт создать задачу — положите ярлык на `bin\team-room-server.cmd`
в папку «Автозагрузка» (`Win+R` → `shell:startup`).

Остановить сервер: `bin\team-room-stop.cmd`. Открыть страницу, подняв сервер, если нужно:
`bin\team-room.cmd`.

---

## 5. Обновление

Запустите ту же команду ещё раз. Установщик остановит работающий сервер, заменит файлы
программы и плагин, снова поднимет сервер и оставит всё, что накопилось:

* `data\rooms.json`, `data\state\`, `data\reports\`, `data\tmp\` — комнаты, запросы, отчёты;
* ваши настройки в `install.json` (порт, автозапуск) — перечитываются;
* папки проектов вне приложения и зеркало в Vault — не трогаются вообще.

Задача автозапуска и ярлык не дублируются: установщик перезаписывает их по имени.

---

## 6. Удаление

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\install.ps1 -Uninstall
```

Убирает: сервер (только свои процессы), задачу `TeamRoomServer` или ярлык из
«Автозагрузки», папку плагина в Hermes Desktop, файлы программы.

**Данные остаются** — `data` со всеми комнатами. Удалить и их:

```powershell
... -File .\install.ps1 -Uninstall -RemoveData
```

Строка «Командные комнаты» исчезнет из меню Hermes после перезагрузки плагинов:
`Ctrl+K` → «Reload desktop plugins».

---

## 7. Только плагин, без сервера

Иногда нужен лишь модуль в Hermes Desktop (сервер уже работает отдельно или стоит
на другой машине в локальной сети — хотя по умолчанию приложение слушает только
`127.0.0.1`):

```powershell
python tools\install_desktop_plugin.py                 # в %LOCALAPPDATA%\hermes
python tools\install_desktop_plugin.py --port 8788     # другой порт
python tools\install_desktop_plugin.py --root "D:\TeamRoom"  # нестандартная папка Hermes
python tools\install_desktop_plugin.py --uninstall     # убрать
```

Плагин — один файл, копируется только при изменении и никогда не перезаписывает
профили Hermes.

---

## 8. Типичные проблемы

**«Выполнение сценариев отключено в этой системе».** Политика PowerShell. Запускайте
команду как в разделе 1 (там уже есть `-ExecutionPolicy Bypass` для одного запуска) или
разово разрешите для текущего пользователя:

```powershell
Set-ExecutionPolicy -Scope CurrentUser -ExecutionPolicy RemoteSigned
```

**Нет Python.** Установщик скажет об этом и покажет `winget install --id Python.Python.3.12 -e`.
После установки запустите его снова. Не хотите `winget` — скачайте с python.org и
отметьте «Add python.exe to PATH».

**Порт 8777 занят.** В отчёте будет видно, кто именно держит порт. Поставьте на другой:
`-Port 8788` (тогда плагин тоже получит 8788 — установщик подставит порт в копию плагина).

**Антивирус ругается на `.ps1` или `.cmd`.** Файлы не подписаны. Добавьте папку
`%LOCALAPPDATA%\TeamRoom` в исключения или запустите установку из папки (`-SourceDir`),
предварительно посмотрев содержимое `install.ps1` — он короткий и без обфускации.

**Строки «Командные комнаты» нет в меню.** `Ctrl+K` → «Reload desktop plugins».
Если не помогло — проверьте, что файл на месте:
`%LOCALAPPDATA%\hermes\desktop-plugins\team-room\plugin.js`, и повторите установку.

**Страница не открывается.** Посмотрите логи: `%LOCALAPPDATA%\TeamRoom\data\logs\`
(`app.log`, `launcher.log`). Чаще всего сервер просто ещё поднимается — плагин сам
повторяет попытку каждые 5 секунд.

**Диктовка молчит.** Нет `faster-whisper` или провайдера распознавания в Hermes.
`python -m pip install --user faster-whisper` или установка с `-InstallDeps`.

**Планировщик не дал создать задачу.** Установщик сам положит ярлык в «Автозагрузку» —
работает так же. Причина видна в отчёте установки.

---

## 9. Куда что ставится

| Что | Путь |
|---|---|
| Программа (код, `web/`, `server/`, `tools/`, `bin/`, `desktop-plugin/`) | `%LOCALAPPDATA%\TeamRoom` |
| Данные: комнаты, отчёты, логи, состояние | `%LOCALAPPDATA%\TeamRoom\data` |
| Запись об установке (версия, порт) | `%LOCALAPPDATA%\TeamRoom\install.json` |
| Плагин для Hermes Desktop | `%LOCALAPPDATA%\hermes\desktop-plugins\team-room\plugin.js` |
| Автозапуск | задача `TeamRoomServer` или ярлык в «Автозагрузке» |
| Логи | `%LOCALAPPDATA%\TeamRoom\data\logs\` |

Всё это переживает обновления Hermes: Hermes подменяет свою папку программы, а не данные.

---

## 10. macOS и Linux

Всё то же самое, что в разделах 1–9, но установщик — `install.sh` (bash). Права
администратора не нужны, ставится в домашний каталог пользователя. Нужны только
`curl`, `tar` (или `unzip`) и Python 3.11+ — ничего доустанавливать не требуется.

### 10.1 Одной командой

```bash
curl -fsSL https://raw.githubusercontent.com/vorchun18-svg/team-room-for-hermes/main/install.sh | sh
```

Установщик:

1. **Проверяет компьютер** и показывает таблицу: Python 3.11+, Hermes Desktop,
   свободен ли порт, место на диске, `faster-whisper`, `ffmpeg`, `hermes` CLI —
   и что именно ломает каждый недостающий пункт.
2. Скачивает готовый релиз с GitHub (если релиза ещё нет — ветку `main`).
3. Останавливает прежний сервер и раскладывает файлы в
   `${XDG_DATA_HOME:-$HOME/.local/share}/TeamRoom`. Папка `data` не трогается.
4. Ставит плагин в `${HERMES_HOME:-$HOME/.hermes}/desktop-plugins/team-room/plugin.js`
   (на macOS, если `~/.hermes` нет, — `~/Library/Application Support/hermes`).
5. Включает автозапуск: `launchd` на macOS, `systemd --user` на Linux, а если
   служба недоступна — строку в `~/.profile`.
6. Поднимает сервер через `nohup` (без окна, лог в `data/logs/`) и ждёт ответа
   до 30 секунд.
7. Открывает `http://127.0.0.1:8777/` и пишет итог.

Дальше в Hermes Desktop: **левое меню → «Командные комнаты»** (или `Cmd+K` → «командные»).

Если терминал закрывается вместе с `curl` (редко, при обрыве связи), скачайте файл и
запустите его отдельно:

```bash
curl -fsSL -o install.sh https://raw.githubusercontent.com/vorchun18-svg/team-room-for-hermes/main/install.sh
sh install.sh --dry-run
```

### 10.2 Что нужно и что необязательно

| Что | Нужно? | Если нет |
|---|---|---|
| macOS 12+ или Linux с `bash` | обязательно | установка не пойдёт |
| **Python 3.11+** | **обязательно** | сервер не запустится: установщик остановится и покажет команду (`brew install python@3.12` / `sudo apt install python3`) |
| 300 МБ на диске | обязательно | распаковка может не закончиться |
| Свободный порт 8777 | обязательно | возьмите другой: `--port 8788` |
| Интернет (github.com) | обязательно при установке из сети | поставьте из папки: `--source-dir ~/team-room` |
| Hermes Desktop | необязательно | страница откроется в браузере, строки в меню не будет |
| `hermes` CLI | необязательно | не пишет карточки в канбан, остальное работает |
| `faster-whisper` | необязательно | микрофон скажет «распознавание не установлено» |
| `ffmpeg` | необязательно | часть аудиоформатов не конвертируется |

Поставить недостающее с согласием на каждый пункт:

```bash
sh install.sh --install-deps      # спрашивает «Поставить …? [Y/n]»
```

`--install-deps` сам ставит только `faster-whisper` (через `pip --user`) и `ffmpeg`
(через `brew` на macOS); Python установщик не трогает — это системный пакет, он
показывает точную команду. Без терминала (ввод перенаправлен) вопросы не задаются:
добавьте `--yes`, если согласны заранее.

### 10.3 Ключи

| Ключ | Что делает |
|---|---|
| `--version ТЕГ` | версия на GitHub (`v1.0.0`, `main`); по умолчанию последний выпуск, иначе `main` |
| `--home ПАПКА` | куда ставить программу |
| `--port ЧИСЛО` | порт сервера (по умолчанию 8777) |
| `--source-dir ПАПКА` | поставить из уже распакованной папки, без сети |
| `--dry-run` | только отчёт, ничего не меняет |
| `--install-deps` | доустановить недостающее, спрашивая согласие |
| `--yes` | не спрашивать (вместе с `--install-deps`) |
| `--no-autostart` | не включать автозапуск (и убрать прежний) |
| `--no-browser` | не открывать страницу |
| `--uninstall` | удалить программу, плагин и автозапуск (данные остаются) |
| `--remove-data` | вместе с `--uninstall` удалить и `data` |
| `--wait СЕКУНДЫ` | сколько ждать ответа сервера (по умолчанию 30) |

Ключи можно писать и через `=`: `--port=8788`, `--home=/opt/TeamRoom`.

### 10.4 Куда что ставится

| Что | Путь |
|---|---|
| Программа | `${XDG_DATA_HOME:-$HOME/.local/share}/TeamRoom` (или `$TEAMROOM_HOME`, `--home`) |
| Данные: комнаты, отчёты, логи, состояние | `<программа>/data` |
| Запись об установке (версия, порт) | `<программа>/install.json` |
| Плагин для Hermes Desktop | `${HERMES_HOME:-$HOME/.hermes}/desktop-plugins/team-room/plugin.js` |
| Порт и python для лаунчеров | `<данные>/run/port.txt`, `run/python.txt` |
| Автозапуск (macOS) | `~/Library/LaunchAgents/com.teamroom.server.plist` |
| Автозапуск (Linux) | `~/.config/systemd/user/teamroom.service` |
| Автозапуск (запасной) | блок `# >>> team-room >>>` в `~/.profile` |
| Логи | `<данные>/logs/` (`server.log`, `launcher.log`) |

На macOS Hermes Desktop может держать данные в `~/Library/Application Support/hermes` —
установщик берёт эту папку, если `~/.hermes` нет. Переопределить можно переменной
`HERMES_HOME` или ключом `--root` у `tools/install_desktop_plugin.py`.

### 10.5 Автозапуск

* **macOS:** `launchd`-файл `~/Library/LaunchAgents/com.teamroom.server.plist`
  (`RunAtLoad`, `KeepAlive` выключен — сервер не перезапускается после падения,
  чтобы не копить процессы). Загружается сразу: `launchctl load`.
* **Linux:** служба `~/.config/systemd/user/teamroom.service`
  (`Type=forking`, `PIDFile` = `data/run/server-<порт>.pid`), включается
  `systemctl --user enable --now teamroom.service`. Чтобы сервер поднимался без
  входа в систему, один раз выполните `sudo loginctl enable-linger $USER` —
  необязательно, установщик только подсказывает.
* **Запасной способ:** если `systemctl --user` недоступен (контейнер, WSL без
  systemd), в `~/.profile` дописывается блок, который запускает
  `bin/team-room-server.sh` при входе в терминал.

Убрать автозапуск: `sh install.sh --no-autostart` или `--uninstall`.

### 10.6 Запуск, остановка, обновление

```bash
~/.local/share/TeamRoom/bin/team-room.sh          # открыть страницу (сервер поднимется сам)
~/.local/share/TeamRoom/bin/team-room-server.sh   # только сервер, в фоне
~/.local/share/TeamRoom/bin/team-room-stop.sh     # остановить
```

Сервер живёт без окна: `nohup`, лог — `data/logs/server.log`, pid — `data/run/server-<порт>.pid`.
Второй экземпляр не запускается: лаунчер сначала спрашивает `/api/health`.
Чужой процесс на том же порту не убивается — остановка проверяет, что в командной
строке есть `app.py` и папка этого приложения.

Обновление — та же команда, что и установка: программа заменяется, `data` остаётся.
Удаление:

```bash
sh install.sh --uninstall                # данные останутся
sh install.sh --uninstall --remove-data  # удалить и комнаты
```

### 10.7 Если что-то не так

**`command not found: python3`.** Поставьте Python 3.11+: `brew install python@3.12`
(macOS) или `sudo apt install python3` (Debian/Ubuntu). Затем запустите установку снова.

**Порт занят.** В отчёте видно, свободен ли порт. Возьмите другой: `--port 8788` —
установщик подставит его и в плагин.

**Нет `curl`.** `xcode-select --install` на macOS; `sudo apt install curl` на Linux.
Либо распакуйте архив руками и запустите `sh install.sh --source-dir <папка>`.

**Нет строки «Командные комнаты» в меню.** `Cmd+K` (Linux — `Ctrl+K`) → «Reload desktop
plugins». Проверьте файл `${HERMES_HOME:-$HOME/.hermes}/desktop-plugins/team-room/plugin.js`
и повторите установку.

**Сервер не отвечает.** Смотрите `<данные>/logs/server.log` и `launcher.log`. Проверить
вручную: `curl http://127.0.0.1:8777/api/health`.

**Диктовка молчит.** Нет `faster-whisper` и нет провайдера распознавания в Hermes:
`python3 -m pip install --user faster-whisper` или `sh install.sh --install-deps`.

**Автозапуск не сработал.** На Linux проверьте `systemctl --user status teamroom.service`;
если система без systemd — установщик уже дописал строку в `~/.profile`.

