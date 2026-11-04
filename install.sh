#!/usr/bin/env bash
# -*- coding: utf-8 -*-
#
# Командные комнаты — установка для macOS и Linux.
#
#   curl -fsSL https://raw.githubusercontent.com/vorchun18-svg/team-room-for-hermes/main/install.sh | sh
#   sh install.sh --dry-run
#   sh install.sh --install-deps
#   sh install.sh --uninstall [--remove-data]
#
# Ничего, кроме curl, tar (или unzip) и python3, не нужно. Права администратора
# не требуются: всё ставится в домашний каталог пользователя.
#
# Повторный запуск — это обновление: программа заменяется, папка data
# с комнатами, журналом и настройками остаётся на месте.

set -u

REPO="vorchun18-svg/team-room-for-hermes"
PLUGIN_ID="team-room"
SERVICE_NAME="teamroom"
LAUNCHD_LABEL="com.teamroom.server"
PROFILE_BEGIN="# >>> team-room >>>"
PROFILE_END="# <<< team-room <<<"
MIN_PY_CODE=311

# ── флаги ───────────────────────────────────────────────────────────────────
VERSION=""
SOURCE_DIR=""
HOME_DIR=""
PORT=""
WAIT_SECONDS=30
DRY_RUN=0
UNINSTALL=0
REMOVE_DATA=0
NO_AUTOSTART=0
NO_BROWSER=0
INSTALL_DEPS=0
ASSUME_YES=0
PORT_SET=0

usage() {
    cat <<'TXT'
  Командные комнаты — установка для macOS и Linux

  Ключи:
    --version ТЕГ      версия на GitHub (например v1.0.0 или main);
                       по умолчанию — последний выпуск, иначе ветка main
    --home ПАПКА       куда ставить программу
                       (по умолчанию ${XDG_DATA_HOME:-$HOME/.local/share}/TeamRoom)
    --port ЧИСЛО       порт локального сервера (по умолчанию 8777)
    --source-dir ПАПКА уже распакованный проект вместо скачивания (без интернета)
    --dry-run          только проверить компьютер и показать отчёт
    --install-deps     поставить недостающее, спрашивая согласие на каждый пункт
    --yes              не спрашивать подтверждений (только вместе с --install-deps)
    --no-autostart     не включать автозапуск (и убрать прежний)
    --no-browser       не открывать страницу в браузере
    --uninstall        удалить программу, плагин и автозапуск (данные остаются)
    --remove-data      вместе с --uninstall удалить и папку data
    --wait СЕКУНДЫ     сколько ждать ответа сервера (по умолчанию 30)
    -h, --help         эта подсказка

  Примеры:
    sh install.sh --dry-run
    sh install.sh --home "$HOME/Applications/TeamRoom" --port 8788
    sh install.sh --uninstall --remove-data
TXT
}

need_value() {  # --ключ значение | --ключ=значение
    case "$1" in
        *=*) printf '%s' "${1#*=}" ;;
        *) shift; printf '%s' "${1:-}" ;;
    esac
}

while [ $# -gt 0 ]; do
    arg="$1"
    case "$arg" in
        --version)        VERSION="$(need_value "$@")"; shift ;;
        --version=*)      VERSION="${arg#*=}" ;;
        --home)           HOME_DIR="$(need_value "$@")"; shift ;;
        --home=*)         HOME_DIR="${arg#*=}" ;;
        --port)           PORT="$(need_value "$@")"; PORT_SET=1; shift ;;
        --port=*)         PORT="${arg#*=}"; PORT_SET=1 ;;
        --source-dir)     SOURCE_DIR="$(need_value "$@")"; shift ;;
        --source-dir=*)   SOURCE_DIR="${arg#*=}" ;;
        --wait)           WAIT_SECONDS="$(need_value "$@")"; shift ;;
        --wait=*)         WAIT_SECONDS="${arg#*=}" ;;
        --install-deps)   INSTALL_DEPS=1 ;;
        --yes|-y)         ASSUME_YES=1 ;;
        --dry-run)        DRY_RUN=1 ;;
        --uninstall)      UNINSTALL=1 ;;
        --remove-data)    REMOVE_DATA=1 ;;
        --no-autostart)   NO_AUTOSTART=1 ;;
        --no-browser)     NO_BROWSER=1 ;;
        -h|--help)        usage; exit 0 ;;
        *)
            printf 'Неизвестный ключ: %s\n\n' "$arg" >&2
            usage >&2
            exit 2
            ;;
    esac
    shift
done

# ── вывод ───────────────────────────────────────────────────────────────────
if [ -t 1 ]; then
    C_STEP="$(printf '\033[36m')"; C_OK="$(printf '\033[32m')"; C_WARN="$(printf '\033[33m')"
    C_BAD="$(printf '\033[31m')"; C_NOTE="$(printf '\033[90m')"; C_OFF="$(printf '\033[0m')"
else
    C_STEP=""; C_OK=""; C_WARN=""; C_BAD=""; C_NOTE=""; C_OFF=""
fi
step() { printf '\n  %s%s%s\n' "$C_STEP" "$1" "$C_OFF"; }
ok()   { printf '  %s[ок]%s %s\n' "$C_OK" "$C_OFF" "$1"; }
warn() { printf '  %s[!]%s %s\n' "$C_WARN" "$C_OFF" "$1"; }
bad()  { printf '  %s[х]%s %s\n' "$C_BAD" "$C_OFF" "$1"; }
note() { printf '      %s%s%s\n' "$C_NOTE" "$1" "$C_OFF"; }

have() { command -v "$1" >/dev/null 2>&1; }

#: Есть ли перенаправление /dev/tcp (bash). В dash (`sh install.sh`) его нет.
HAVE_DEV_TCP=0
if (exec 3<>/dev/tcp/127.0.0.1/9) 2>/dev/null; then HAVE_DEV_TCP=1; exec 3>&- 2>/dev/null; fi

# ── пути ────────────────────────────────────────────────────────────────────
# Нативные программы (python) не понимают MSYS-пути вида /c/<пользователь>/...:
# конвертируем в C:/... там, где это уместно, иначе оставляем как есть.
native_path() {
    value="$1"
    [ -n "$value" ] || return 0
    case "$value" in
        [A-Za-z]:[\\/]*) printf '%s' "$value" | tr '\\' '/'; return 0 ;;
    esac
    case "$value" in
        /*) ;;
        *) value="$(pwd)/$value" ;;
    esac
    # Ищем ближайшего существующего предка: он и даст нативный путь.
    probe="$value"
    rest=""
    while [ ! -d "$probe" ] && [ "$probe" != "/" ]; do
        rest="/$(basename "$probe")$rest"
        probe="$(dirname "$probe")"
    done
    base="$(cd "$probe" 2>/dev/null && { pwd -W 2>/dev/null || cygpath -m "$probe" 2>/dev/null || pwd; })"
    [ -n "$base" ] || base="$probe"
    printf '%s%s' "${base%/}" "$rest"
}

OS_RAW="$(uname -s 2>/dev/null || echo unknown)"
case "$OS_RAW" in
    Darwin)                      OS="macos" ;;
    Linux)                       OS="linux" ;;
    MINGW*|MSYS*|CYGWIN*)        OS="windows-bash" ;;
    *)                           OS="unknown" ;;
esac

USER_HOME="${HOME:-}"
if [ -z "$USER_HOME" ]; then
    bad "Не вижу домашний каталог (\$HOME пуст). Запустите установку обычным пользователем."
    exit 2
fi

# Программа: --home, иначе TEAMROOM_HOME, иначе стандартный каталог данных.
if [ -z "$HOME_DIR" ]; then HOME_DIR="${TEAMROOM_HOME:-}"; fi
if [ -z "$HOME_DIR" ]; then
    HOME_DIR="${XDG_DATA_HOME:-$USER_HOME/.local/share}/TeamRoom"
fi
HOME_DIR="$(native_path "$HOME_DIR")"
DATA_DIR="${TEAMROOM_DATA:-$HOME_DIR/data}"
DATA_DIR="$(native_path "$DATA_DIR")"

# Hermes Desktop: HERMES_HOME, иначе обычные места.
if [ -z "${HERMES_HOME:-}" ]; then
    if [ "$OS" = "macos" ] && [ -d "$USER_HOME/Library/Application Support/hermes" ]; then
        HERMES_HOME="$USER_HOME/Library/Application Support/hermes"
    elif [ -d "$USER_HOME/.hermes" ]; then
        HERMES_HOME="$USER_HOME/.hermes"
    elif [ "$OS" = "macos" ]; then
        HERMES_HOME="$USER_HOME/Library/Application Support/hermes"
    else
        HERMES_HOME="$USER_HOME/.hermes"
    fi
fi
HERMES_HOME="$(native_path "$HERMES_HOME")"

MANIFEST="$HOME_DIR/install.json"
if [ "$PORT_SET" = "0" ] && [ -f "$MANIFEST" ]; then
    saved="$(sed -n 's/.*"port"[[:space:]]*:[[:space:]]*\([0-9][0-9]*\).*/\1/p' "$MANIFEST" | head -n 1)"
    if [ -n "$saved" ]; then PORT="$saved"; fi
fi
if [ -z "$PORT" ]; then PORT="${TEAMROOM_PORT:-8777}"; fi
case "$PORT" in
    ''|*[!0-9]*)
        bad "Такой порт не подойдёт: $PORT. Возьмите 1024–65535, обычно 8777."
        exit 2
        ;;
esac
if [ "$PORT" -lt 1024 ] || [ "$PORT" -gt 65535 ]; then
    bad "Такой порт не подойдёт: $PORT. Возьмите 1024–65535, обычно 8777."
    exit 2
fi

URL="http://127.0.0.1:$PORT/"

# ── python ──────────────────────────────────────────────────────────────────
py_code() {  # путь к python → 311/312/... либо пусто
    [ -n "$1" ] || return 0
    "$1" -c 'import sys;print(sys.version_info[0]*100+sys.version_info[1])' 2>/dev/null | tr -dc '0-9'
}

find_python() {
    candidates=""
    if [ -n "${TEAMROOM_PYTHON:-}" ]; then candidates="$TEAMROOM_PYTHON"; fi
    if [ -f "$DATA_DIR/run/python.txt" ]; then
        candidates="$candidates $(tr -d '\r\n' < "$DATA_DIR/run/python.txt")"
    fi
    candidates="$candidates python3 python3.13 python3.12 python3.11 python"
    for exe in $candidates; do
        path="$(command -v "$exe" 2>/dev/null || true)"
        [ -n "$path" ] || continue
        code="$(py_code "$path")"
        [ -n "$code" ] || continue
        if [ "$code" -ge "$MIN_PY_CODE" ] 2>/dev/null; then
            printf '%s\n%s\n%s\n' "$path" "${code%??}.${code#?}" "$code"
            return 0
        fi
    done
    return 1
}

PY=""
PY_VERSION=""
PY_CODE=""
found="$(find_python || true)"
if [ -n "$found" ]; then
    PY="$(printf '%s\n' "$found" | sed -n '1p')"
    PY_VERSION="$(printf '%s\n' "$found" | sed -n '2p')"
    PY_CODE="$(printf '%s\n' "$found" | sed -n '3p')"
fi
PY="$(native_path "$PY")"

# ── проверки окружения ──────────────────────────────────────────────────────
free_kb() {
    dir="$1"
    probe="$dir"
    while [ ! -d "$probe" ] && [ "$probe" != "/" ] && [ -n "$probe" ]; do probe="$(dirname "$probe")"; done
    df -Pk "$probe" 2>/dev/null | awk 'NR==2 {print $4}' | tr -dc '0-9'
}

format_size() {  # КБ → «1,2 ГБ» / «540 МБ»
    kb="$1"
    case "$kb" in ''|*[!0-9]*) printf 'неизвестно'; return 0 ;; esac
    if [ "$kb" -ge 1048576 ]; then
        printf '%s ГБ' "$(awk -v k="$kb" 'BEGIN {printf "%.1f", k/1048576}' | tr '.' ',')"
    else
        printf '%s МБ' "$((kb / 1024))"
    fi
}

port_open() {  # 0 — кто-то слушает порт
    port="$1"
    if [ "${HAVE_DEV_TCP:-0}" = "1" ]; then
        if (exec 3<>"/dev/tcp/127.0.0.1/$port") 2>/dev/null; then
            exec 3>&- 2>/dev/null
            return 0
        fi
        return 1
    fi
    # `sh` без /dev/tcp (dash): проверяем тем же python, что и всё остальное.
    if [ -n "$PY" ]; then
        "$PY" - "$port" >/dev/null 2>&1 <<'PYEOF'
import socket, sys
sock = socket.socket()
sock.settimeout(0.4)
sys.exit(0 if sock.connect_ex(("127.0.0.1", int(sys.argv[1]))) == 0 else 1)
PYEOF
        return $?
    fi
    return 1
}

http_health() {
    if have curl; then
        curl -fsS -m 2 --noproxy '*' "http://127.0.0.1:$1/api/health" >/dev/null 2>&1 && return 0
        return 1
    fi
    if [ -n "$PY" ]; then
        "$PY" - "$1" >/dev/null 2>&1 <<'PYEOF'
import sys, urllib.request
opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
try:
    with opener.open("http://127.0.0.1:%s/api/health" % sys.argv[1], timeout=2) as r:
        sys.exit(0 if r.getcode() == 200 else 1)
except Exception:
    sys.exit(1)
PYEOF
    fi
    return 1
}

internet_ok() {
    if have curl; then
        curl -fsS -m 4 -o /dev/null https://github.com >/dev/null 2>&1 && return 0
        return 1
    fi
    return 1
}

whisper_version() {
    [ -n "$PY" ] || return 0
    "$PY" -c 'import faster_whisper;print(getattr(faster_whisper,"__version__","?"))' 2>/dev/null | head -n 1
}

# ── таблица ──────────────────────────────────────────────────────────────────
ROWS=""          # name|required|ok|state|note|kind
add_row() { ROWS="$ROWS$1|$2|$3|$4|$5|$6
"; }

build_report() {
    ROWS=""
    case "$OS" in
        macos) os_name="macOS ($(sw_vers -productVersion 2>/dev/null || echo версия неизвестна)), $(uname -m)" ;;
        linux) os_name="Linux ($(uname -r 2>/dev/null || echo ядро неизвестно)), $(uname -m)" ;;
        windows-bash) os_name="Windows, терминал bash (проверочный режим)" ;;
        *) os_name="$OS_RAW" ;;
    esac
    if [ "$OS" = "macos" ] || [ "$OS" = "linux" ]; then
        add_row "macOS или Linux" yes yes "есть ($os_name)" "обязательно" os
    else
        add_row "macOS или Linux" no yes "есть ($os_name)" "на Windows ставьте install.ps1" os
    fi

    if [ -n "$PY" ]; then
        add_row "Python 3.11+" yes yes "есть ($PY_VERSION)" "обязательно" python
    else
        add_row "Python 3.11+" yes no "нет" "обязательно: без него сервер не запустится" python
    fi

    free="$(free_kb "$DATA_DIR")"
    if [ -z "$free" ]; then
        add_row "Место на диске" yes yes "неизвестно" "нужно около 300 МБ" disk
    elif [ "$free" -ge 307200 ]; then
        add_row "Место на диске" yes yes "$(format_size "$free") свободно" "нужно около 300 МБ" disk
    else
        add_row "Место на диске" yes no "$(format_size "$free") свободно" "нужно около 300 МБ" disk
    fi

    if http_health "$PORT"; then
        add_row "Порт $PORT" yes yes "порт отвечает" "если это не ваш сервер — возьмите другой --port" port
    elif port_open "$PORT"; then
        add_row "Порт $PORT" yes no "занят другой программой" "обязательно: возьмите другой --port" port
    else
        add_row "Порт $PORT" yes yes "свободен" "обязательно" port
    fi

    if [ -d "$HERMES_HOME" ]; then
        add_row "Hermes Desktop" no yes "есть" "без него: только браузер, без строки в меню" hermes
    else
        add_row "Hermes Desktop" no no "нет" "без него: только браузер, без строки в меню" hermes
    fi

    if have hermes; then
        add_row "hermes CLI" no yes "есть" "без него: не пишет в канбан, комнаты работают" cli
    else
        add_row "hermes CLI" no no "нет" "без него: не пишет в канбан, комнаты работают" cli
    fi

    whisper="$(whisper_version)"
    if [ -n "$whisper" ]; then
        add_row "faster-whisper" no yes "есть ($whisper)" "без него: диктовка недоступна" whisper
    else
        add_row "faster-whisper" no no "нет" "без него: диктовка недоступна" whisper
    fi

    if have ffmpeg; then
        add_row "ffmpeg" no yes "есть" "необязательно: диктовка и так работает" ffmpeg
    else
        add_row "ffmpeg" no no "нет" "необязательно: часть форматов не конвертируется" ffmpeg
    fi

    if [ -n "$SOURCE_DIR" ]; then
        add_row "Интернет (github.com)" no yes "не нужен" "ставлю из папки" net
    elif internet_ok; then
        add_row "Интернет (github.com)" yes yes "есть" "обязательно для скачивания" net
    else
        add_row "Интернет (github.com)" yes no "нет" "обязательно для скачивания" net
    fi
}

pad_right() {  # текст, ширина в СИМВОЛАХ (кириллица — не байты)
    text="$1"; width="$2"
    len="$(printf '%s' "$text" | LC_ALL=C tr -d '\200-\277' | wc -c | tr -d ' ')"
    if [ "${len:-0}" -ge "$width" ]; then printf '%s' "$text"; return 0; fi
    printf '%s%*s' "$text" "$((width - len))" ''
}

show_table() {
    printf '\n  %s %s %s\n' "$(pad_right "Что" 24)" "$(pad_right "Состояние" 28)" "Что это значит"
    printf '  %s\n' "--------------------------------------------------------------------------"
    printf '%s' "$ROWS" | while IFS='|' read -r name required is_ok state text kind; do
        [ -n "$name" ] || continue
        color="$C_NOTE"
        if [ "$is_ok" = "yes" ]; then color="$C_OK"
        elif [ "$required" = "yes" ]; then color="$C_BAD"
        else color="$C_WARN"; fi
        printf '  %s%s %s %s%s\n' "$color" "$(pad_right "$name" 24)" "$(pad_right "$state" 28)" "$text" "$C_OFF"
    done
}

show_missing() {
    missing="$(printf '%s' "$ROWS" | awk -F'|' '$1 != "" && $3 != "yes"')"
    if [ -z "$missing" ]; then
        printf '\n'
        ok "Всё на месте — можно ставить."
        return 0
    fi
    printf '\n  Чего не хватает:\n'
    printf '%s\n' "$missing" | while IFS='|' read -r name required is_ok state text kind; do
        case "$kind" in
            python)
                printf '   - Python 3.11+ — без него сервер не запустится вообще.\n'
                case "$OS" in
                    macos) note "brew install python@3.12" ;;
                    *)     note "sudo apt install python3   (или: sudo dnf install python3)" ;;
                esac
                ;;
            port)
                printf '   - %s %s — сервер не сможет занять место.\n' "$name" "$state"
                note "Запустите установку с другим портом: --port 8788"
                ;;
            disk)
                printf '   - Мало места на диске — распаковка может не закончиться.\n'
                ;;
            net)
                printf '   - Нет связи с github.com — скачать программу не получится.\n'
                note "Проверьте интернет или поставьте из папки: --source-dir /путь/к/team-room"
                ;;
            hermes)
                printf '   - Hermes Desktop не найден — страница откроется только в браузере.\n'
                note "Плагин встанет сам, когда Hermes появится: повторите установку."
                ;;
            cli)
                printf '   - hermes CLI не найден — записи в канбан не будет, остальное работает.\n'
                ;;
            whisper)
                printf '   - faster-whisper нет — кнопка микрофона скажет «распознавание не установлено».\n'
                note "${PY:-python3} -m pip install --user faster-whisper"
                ;;
            ffmpeg)
                printf '   - ffmpeg нет — часть аудиоформатов не конвертируется.\n'
                case "$OS" in
                    macos) note "brew install ffmpeg" ;;
                    *)     note "sudo apt install ffmpeg   (или: sudo dnf install ffmpeg)" ;;
                esac
                ;;
        esac
    done
}

ask_yes() {  # вопрос → 0 = да
    prompt="$1"
    if [ "$ASSUME_YES" = "1" ]; then return 0; fi
    if [ -r /dev/tty ] && [ -t 1 ]; then
        printf '  %s [Y/n] ' "$prompt" >&2
        answer=""
        read -r answer < /dev/tty || answer="n"
    else
        note "Спросить некого (ввод не с терминала) — пропускаю. Запустите с --install-deps в терминале."
        return 1
    fi
    case "$answer" in
        ''|y|Y|yes|YES|д|да|Да|ДА) return 0 ;;
        *) return 1 ;;
    esac
}

install_deps() {
    log_file="$DATA_DIR/logs/install-deps.log"
    mkdir -p "$DATA_DIR/logs" 2>/dev/null
    missing="$(printf '%s' "$ROWS" | awk -F'|' '$1 != "" && $3 != "yes"')"
    if [ -z "$missing" ]; then ok "Ставить нечего — всё уже есть"; return 0; fi
    printf '%s\n' "$missing" | while IFS='|' read -r name required is_ok state text kind; do
        case "$kind" in
            python)
                note "Python не ставлю сам: это системный пакет."
                case "$OS" in
                    macos) note "brew install python@3.12  (около 30 МБ)" ;;
                    *)     note "sudo apt install python3  (около 30 МБ)" ;;
                esac
                ;;
            whisper)
                if ask_yes "Поставить faster-whisper (диктовка)? ~400 МБ, плюс модель 1–3 ГБ при первом включении"; then
                    step "Ставлю faster-whisper (диктовка)"
                    "$PY" -m pip install --user faster-whisper 2>&1 | tail -n 5
                    printf 'pip faster-whisper: exit %s\n' "$?" >> "$log_file"
                    ok "готово (подробности: $log_file)"
                fi
                ;;
            ffmpeg)
                if ! have brew && [ "$OS" = "macos" ]; then
                    note "ffmpeg: brew не найден — поставьте вручную: brew install ffmpeg"
                    continue
                fi
                if ask_yes "Поставить ffmpeg? ~150 МБ"; then
                    step "Ставлю ffmpeg"
                    case "$OS" in
                        macos) brew install ffmpeg 2>&1 | tail -n 5
                               printf 'brew ffmpeg: exit %s\n' "$?" >> "$log_file" ;;
                        *)     note "ffmpeg ставится системным пакетом: sudo apt install ffmpeg"
                               printf 'ffmpeg: подсказка выдана\n' >> "$log_file" ;;
                    esac
                fi
                ;;
            *)
                note "Не ставлю автоматически: $name — поставьте вручную."
                ;;
        esac
    done
}

# ── остановка сервера ───────────────────────────────────────────────────────
proc_cmdline() {
    pid="$1"
    if [ -r "/proc/$pid/cmdline" ]; then
        tr '\0' ' ' < "/proc/$pid/cmdline" 2>/dev/null
    else
        ps -p "$pid" -o command= 2>/dev/null || ps -p "$pid" 2>/dev/null | tail -n +2
    fi
}

is_our_process() {  # pid
    pid="$1"
    case "$pid" in ''|*[!0-9]*) return 1 ;; esac
    line="$(proc_cmdline "$pid")"
    case "$line" in
        *app.py*) ;;
        *) return 1 ;;
    esac
    case "$line" in *python*) ;; *) return 1 ;; esac
    case "$line" in
        *"$HOME_DIR"*|*"$DATA_DIR"*) return 0 ;;
    esac
    return 1
}

stop_server() {
    killed=0
    kept=0
    for file in "$DATA_DIR"/run/server-*.pid; do
        [ -f "$file" ] || continue
        pid="$(tr -dc '0-9' < "$file" 2>/dev/null)"
        if is_our_process "$pid"; then
            kill "$pid" 2>/dev/null && killed=$((killed + 1))
            n=0
            while kill -0 "$pid" 2>/dev/null && [ "$n" -lt 20 ]; do sleep 0.2; n=$((n + 1)); done
            kill -9 "$pid" 2>/dev/null || true
            # Процесс жив — pid-файл оставляем: удалять его нельзя, иначе
            # следующая установка сочтёт сервер остановленным и не запишет pid.
            if kill -0 "$pid" 2>/dev/null; then kept=$((kept + 1)); continue; fi
        fi
        rm -f "$file" 2>/dev/null
    done
    # Запасной путь: сервер, запущенный вручную, без pid-файла.
    # `ps -ef` есть и на macOS/Linux, и в git-bash (там pid тот же, что у kill).
    self="$$"
    for pid in $(ps -ef 2>/dev/null | awk 'NR > 1 { print $2 }'); do
        [ "$pid" = "$self" ] && continue
        if is_our_process "$pid"; then
            kill "$pid" 2>/dev/null && killed=$((killed + 1))
        fi
    done
    if [ "$kept" -gt 0 ]; then
        printf '  [!] не удалось остановить %s процесс(ов) — pid-файл сохранён\n' "$kept" >&2
    fi
    printf '%s\n' "$killed"
}

# ── автозапуск ──────────────────────────────────────────────────────────────
plist_path() { printf '%s/Library/LaunchAgents/%s.plist' "$USER_HOME" "$LAUNCHD_LABEL"; }
unit_path()  { printf '%s/.config/systemd/user/%s.service' "$USER_HOME" "$SERVICE_NAME"; }
profile_path() {
    for name in .profile .bash_profile; do
        if [ -f "$USER_HOME/$name" ]; then printf '%s/%s' "$USER_HOME" "$name"; return 0; fi
    done
    printf '%s/.profile' "$USER_HOME"
}

remove_autostart() {
    removed=""
    plist="$(plist_path)"
    if [ -f "$plist" ]; then
        if [ "$OS" = "macos" ] && have launchctl; then
            launchctl unload "$plist" >/dev/null 2>&1 || true
        fi
        rm -f "$plist" && removed="$removed${removed:+, }автозапуск launchd"
    fi
    unit="$(unit_path)"
    if [ -f "$unit" ]; then
        if have systemctl; then
            systemctl --user disable --now "$SERVICE_NAME.service" >/dev/null 2>&1 || true
            systemctl --user daemon-reload >/dev/null 2>&1 || true
        fi
        rm -f "$unit" && removed="$removed${removed:+, }автозапуск systemd"
    fi
    profile="$(profile_path)"
    if [ -f "$profile" ] && grep -qF "$PROFILE_BEGIN" "$profile" 2>/dev/null; then
        tmp="$profile.team-room.$$"
        awk -v b="$PROFILE_BEGIN" -v e="$PROFILE_END" '
            $0 == b {skip = 1; next}
            $0 == e {skip = 0; next}
            !skip {print}
        ' "$profile" > "$tmp" 2>/dev/null && mv "$tmp" "$profile"
        rm -f "$tmp" 2>/dev/null
        removed="$removed${removed:+, }строка автозапуска в $(basename "$profile")"
    fi
    printf '%s' "$removed"
}

set_autostart() {
    launcher="$HOME_DIR/bin/team-room-server.sh"
    if [ ! -f "$launcher" ]; then
        warn "нет файла bin/team-room-server.sh — автозапуск пропущен"
        return 1
    fi
    chmod +x "$launcher" "$HOME_DIR/bin/"*.sh 2>/dev/null || true
    mkdir -p "$DATA_DIR/logs" 2>/dev/null

    if [ "$OS" = "macos" ]; then
        plist="$(plist_path)"
        mkdir -p "$(dirname "$plist")"
        cat > "$plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LAUNCHD_LABEL</string>
  <key>ProgramArguments</key>
  <array>
    <string>$launcher</string>
    <string>--home</string><string>$HOME_DIR</string>
    <string>--quiet</string>
  </array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><false/>
  <key>WorkingDirectory</key><string>$HOME_DIR</string>
  <key>StandardOutPath</key><string>$DATA_DIR/logs/launchd.out.log</string>
  <key>StandardErrorPath</key><string>$DATA_DIR/logs/launchd.err.log</string>
</dict>
</plist>
PLIST
        if have launchctl; then
            launchctl unload "$plist" >/dev/null 2>&1 || true
            if launchctl load "$plist" >/dev/null 2>&1; then
                ok "автозапуск: launchd ($LAUNCHD_LABEL)"
                return 0
            fi
            warn "launchd не принял файл — оставил его, сервер поднимется при следующем входе"
            return 0
        fi
        ok "автозапуск: файл launchd готов ($plist)"
        return 0
    fi

    if [ "$OS" = "linux" ] && have systemctl; then
        unit="$(unit_path)"
        mkdir -p "$(dirname "$unit")"
        cat > "$unit" <<UNIT
[Unit]
Description=Командные комнаты — локальный сервер
After=network.target

[Service]
Type=forking
ExecStart="$launcher" --home "$HOME_DIR" --quiet
PIDFile=$DATA_DIR/run/server-$PORT.pid
TimeoutStartSec=$((WAIT_SECONDS + 15))
Restart=no

[Install]
WantedBy=default.target
UNIT
        systemctl --user daemon-reload >/dev/null 2>&1 || true
        if systemctl --user enable --now "$SERVICE_NAME.service" >/dev/null 2>&1; then
            ok "автозапуск: systemd --user ($SERVICE_NAME.service)"
            note "Чтобы сервер жил и без входа в систему: sudo loginctl enable-linger $USER (не обязательно)"
            return 0
        fi
        warn "systemd не принял службу — остался запасной способ"
        rm -f "$unit" 2>/dev/null
    fi

    # Запасной способ: строка в ~/.profile (работает в любом терминале и в сессии).
    profile="$(profile_path)"
    touch "$profile" 2>/dev/null || { warn "не смог записать $profile"; return 1; }
    if ! grep -qF "$PROFILE_BEGIN" "$profile" 2>/dev/null; then
        {
            printf '\n%s\n' "$PROFILE_BEGIN"
            printf '# Автозапуск «Командных комнат» (локальный сервер на 127.0.0.1)\n'
            printf '[ -x "%s" ] && "%s" --quiet >/dev/null 2>&1\n' "$launcher" "$launcher"
            printf '%s\n' "$PROFILE_END"
        } >> "$profile"
    fi
    ok "автозапуск: строка в $(basename "$profile") (запасной способ)"
    return 0
}

# ── плагин ──────────────────────────────────────────────────────────────────
install_plugin() {
    script="$HOME_DIR/tools/install_desktop_plugin.py"
    if [ ! -f "$script" ]; then
        warn "в поставке нет tools/install_desktop_plugin.py"
        return 1
    fi
    if [ ! -d "$HERMES_HOME" ]; then
        warn "папка Hermes не найдена: $HERMES_HOME"
        return 1
    fi
    if "$PY" "$script" --root "$HERMES_HOME" --port "$PORT" >/dev/null 2>&1; then
        ok "плагин на месте: $HERMES_HOME/desktop-plugins/$PLUGIN_ID/plugin.js"
        return 0
    fi
    warn "плагин не поставлен (подробности: $PY \"$script\" --root \"$HERMES_HOME\" --port $PORT)"
    return 1
}

remove_plugin() {
    if [ -n "$PY" ] && [ -f "$HOME_DIR/tools/install_desktop_plugin.py" ]; then
        "$PY" "$HOME_DIR/tools/install_desktop_plugin.py" --root "$HERMES_HOME" --uninstall >/dev/null 2>&1 || true
    fi
    rm -rf "$HERMES_HOME/desktop-plugins/$PLUGIN_ID" 2>/dev/null || true
    [ ! -d "$HERMES_HOME/desktop-plugins/$PLUGIN_ID" ]
}

# ── файлы программы ────────────────────────────────────────────────────────
install_files() {  # источник
    src="$1"
    if [ ! -f "$src/server/app.py" ]; then
        bad "в папке нет server/app.py: $src"
        return 1
    fi
    mkdir -p "$HOME_DIR" || return 1
    if [ "$(cd "$src" 2>/dev/null && pwd)" = "$(cd "$HOME_DIR" 2>/dev/null && pwd)" ]; then
        # Ставим из самой установленной папки: копировать нечего, только права.
        chmod +x "$HOME_DIR/bin/"*.sh 2>/dev/null || true
        mkdir -p "$DATA_DIR" || return 1
        return 0
    fi
    for name in server web tools desktop-plugin reportkit bin docs tests fixtures; do
        [ -d "$src/$name" ] || continue
        rm -rf "$HOME_DIR/$name" 2>/dev/null
        cp -R "$src/$name" "$HOME_DIR/$name" 2>/dev/null || {
            bad "не удалось скопировать папку $name — она занята другой программой?"
            return 1
        }
    done
    for name in README.md LICENSE install.ps1 install.sh VERSION \
                start-team-room.cmd stop-team-room.cmd start-demo-room.cmd; do
        [ -f "$src/$name" ] && cp -f "$src/$name" "$HOME_DIR/$name" 2>/dev/null
    done
    find "$HOME_DIR" -type d -name __pycache__ -prune -exec rm -rf {} + 2>/dev/null || true
    chmod +x "$HOME_DIR/bin/"*.sh 2>/dev/null || true
    mkdir -p "$DATA_DIR" || return 1
    [ -f "$HOME_DIR/server/app.py" ]
}

# ── скачивание ─────────────────────────────────────────────────────────────
extract_archive() {  # файл, куда
    file="$1"; dest="$2"
    case "$file" in
        *.zip)
            if have unzip; then unzip -q -o "$file" -d "$dest"
            else "$PY" -m zipfile -e "$file" "$dest"; fi
            ;;
        *) tar -xzf "$file" -C "$dest" 2>/dev/null || "$PY" -c 'import sys,tarfile;tarfile.open(sys.argv[1]).extractall(sys.argv[2])' "$file" "$dest" ;;
    esac
}

fetch_archive() {  # рабочая папка → печатает «путь|метка|адрес»
    work="$1"
    base="https://github.com/$REPO"
    urls=""
    if [ -n "$VERSION" ] && [ "$VERSION" != "main" ] && [ "$VERSION" != "master" ]; then
        urls="$base/archive/refs/tags/$VERSION.tar.gz $base/archive/refs/tags/$VERSION.zip"
    else
        urls="$base/releases/latest/download/team-room.tar.gz $base/releases/latest/download/team-room.zip $base/archive/refs/heads/main.tar.gz $base/archive/refs/heads/main.zip"
    fi
    for url in $urls; do
        ext="${url##*.}"
        file="$work/team-room.$ext"
        note "$url"
        if ! curl -fsSL --retry 1 -m 120 "$url" -o "$file" 2>/dev/null; then continue; fi
        head -c 200 "$file" | grep -qi '<!DOCTYPE\|<html' && { rm -f "$file"; continue; }
        rm -rf "$work/unpacked"; mkdir -p "$work/unpacked"
        if ! extract_archive "$file" "$work/unpacked"; then rm -f "$file"; continue; fi
        found="$(find "$work/unpacked" -maxdepth 4 -type f -path '*/server/app.py' 2>/dev/null | head -n 1)"
        if [ -z "$found" ]; then rm -f "$file"; continue; fi
        root="$(dirname "$(dirname "$found")")"
        label="$VERSION"
        if [ -z "$label" ]; then
            case "$url" in
                */releases/latest/*) label="latest" ;;
                */heads/main.*)      label="main" ;;
                *refs/tags/*)        label="$(printf '%s' "$url" | sed -n 's#.*refs/tags/\([^/]*\)/.*#\1#p')" ;;
                *)                   label="main" ;;
            esac
        fi
        printf '%s|%s|%s\n' "$root" "$label" "$url"
        return 0
    done
    return 1
}

health_wait() {  # порт, секунды
    port="$1"; seconds="$2"; deadline=$(( $(date +%s) + seconds ))
    while [ "$(date +%s)" -lt "$deadline" ]; do
        http_health "$port" && return 0
        sleep 0.5
    done
    return 1
}

open_browser() {
    [ "$NO_BROWSER" = "1" ] && return 0
    [ "${TEAMROOM_NO_SHELL:-0}" = "1" ] && return 0
    if [ "$OS" = "macos" ] && have open; then open "$URL" >/dev/null 2>&1 &
    elif [ "$OS" = "linux" ] && have xdg-open; then xdg-open "$URL" >/dev/null 2>&1 &
    elif [ "$OS" = "windows-bash" ] && have cmd; then cmd //c start "" "$URL" >/dev/null 2>&1 &
    elif [ -n "$PY" ]; then "$PY" -m webbrowser "$URL" >/dev/null 2>&1 &
    fi
    return 0
}

# ─────────────────────────────────────────────────────────────────────────────
#  Начало
# ─────────────────────────────────────────────────────────────────────────────
printf '\n  Командные комнаты — установка\n'
printf '  %s\n' "----------------------------------------------"

note "программа: $HOME_DIR"
note "данные:    $DATA_DIR"
note "hermes:    $HERMES_HOME"

# Зеркало комнат по умолчанию пишется рядом с данными: на чужой машине
# приложение не должно создавать папки в чужих местах.
if [ -z "${TEAMROOM_VAULT_DIR:-}" ]; then
    TEAMROOM_VAULT_DIR="$DATA_DIR/vault"
    note "зеркало:  $TEAMROOM_VAULT_DIR"
fi
export TEAMROOM_VAULT_DIR

if [ "$UNINSTALL" = "0" ]; then
    step "Проверяю окружение"
    build_report
    show_table
    show_missing

    if [ "$INSTALL_DEPS" = "1" ]; then
        step "Ставлю недостающее"
        install_deps
        found="$(find_python || true)"
        if [ -n "$found" ]; then
            PY="$(native_path "$(printf '%s\n' "$found" | sed -n '1p')")"
            PY_VERSION="$(printf '%s\n' "$found" | sed -n '2p')"
        fi
        build_report
    fi
fi

if [ "$DRY_RUN" = "1" ]; then
    printf '\n  Это только проверка: ничего не скачано и не изменено.\n'
    note "Уберите ключ --dry-run, и установка пойдёт по-настоящему."
    if printf '%s' "$ROWS" | awk -F'|' '$1 != "" && $3 != "yes" && $2 == "yes"' | grep -q .; then
        note "Обязательное пока не на месте — см. список «Чего не хватает» выше."
    fi
    exit 0
fi

if [ -z "$PY" ]; then
    if [ "$UNINSTALL" = "1" ]; then
        warn "Python не найден — удалю файлы и плагин без остановки сервера."
    else
        bad "Без Python 3.11 или новее дальше нельзя: на нём работает сервер."
        printf '\n  Поставьте Python и запустите установку снова:\n'
        case "$OS" in
            macos) printf '      %sbrew install python@3.12%s\n' "$C_WARN" "$C_OFF" ;;
            *)     printf '      %ssudo apt install python3%s   (или: sudo dnf install python3)\n' "$C_WARN" "$C_OFF" ;;
        esac
        printf '\n'
        note "Или запустите с ключом --install-deps — я подскажу точную команду."
        exit 2
    fi
fi
[ -n "$PY" ] && note "python:    $PY ($PY_VERSION)"

# ── удаление ────────────────────────────────────────────────────────────────
if [ "$UNINSTALL" = "1" ]; then
    step "Останавливаю сервер"
    killed="$(stop_server)"
    if [ "${killed:-0}" -gt 0 ] 2>/dev/null; then ok "остановлено процессов: $killed"; else ok "сервер не запущен"; fi

    step "Убираю автозапуск"
    removed="$(remove_autostart)"
    if [ -n "$removed" ]; then ok "$removed"; else ok "автозапуска не было"; fi

    step "Убираю плагин из Hermes Desktop"
    if remove_plugin; then
        ok "плагина больше нет: $HERMES_HOME/desktop-plugins/$PLUGIN_ID"
    else
        warn "папка плагина осталась — закройте Hermes Desktop и повторите"
    fi

    step "Убираю файлы программы"
    if [ -d "$HOME_DIR" ]; then
        for item in "$HOME_DIR"/* "$HOME_DIR"/.[!.]*; do
            [ -e "$item" ] || continue
            base="$(basename "$item")"
            if [ "$base" = "data" ] && [ "$REMOVE_DATA" = "0" ]; then continue; fi
            rm -rf "$item" 2>/dev/null || true
        done
        rmdir "$HOME_DIR" 2>/dev/null || true
    fi
    ok "программа удалена"

    printf '\n'
    if [ "$REMOVE_DATA" = "1" ]; then
        printf '  Данные тоже удалены.\n'
    else
        printf '  Данные сохранены: %s\n' "$DATA_DIR"
        note "Комнаты, журнал и настройки остались. Удалить их совсем: --uninstall --remove-data"
    fi
    printf '\n  Готово. Строка «Командные комнаты» исчезнет из Hermes после перезагрузки плагинов\n'
    printf '  (Ctrl+K → Reload desktop plugins).\n\n'
    exit 0
fi

# ── 1. исходники ─────────────────────────────────────────────────────────────
work="${TMPDIR:-/tmp}/team-room-install-$$"
mkdir -p "$work" 2>/dev/null || work="$DATA_DIR/tmp-install"
mkdir -p "$work" 2>/dev/null

label="local"
source_path=""
if [ -n "$SOURCE_DIR" ]; then
    step "Беру программу из папки"
    source_path="$(native_path "$SOURCE_DIR")"
    if [ ! -f "$source_path/server/app.py" ]; then
        bad "В папке нет server/app.py: $source_path"
        exit 2
    fi
    [ -f "$source_path/docs/RELEASE-TAG.txt" ] && \
        label="$(tr -d '\r\n' < "$source_path/docs/RELEASE-TAG.txt")"
else
    step "Скачиваю программу с GitHub"
    if ! have curl; then
        bad "Для скачивания нужен curl — его нет в системе."
        note "Поставьте curl или используйте --source-dir с уже распакованной папкой."
        exit 3
    fi
    fetched="$(fetch_archive "$work")" || fetched=""
    if [ -z "$fetched" ]; then
        bad "Не получилось скачать программу с GitHub."
        note "Проверьте интернет и запустите установку снова."
        note "Или поставьте из папки: --source-dir /путь/к/team-room"
        exit 3
    fi
    source_path="$(printf '%s' "$fetched" | cut -d'|' -f1)"
    label="$(printf '%s' "$fetched" | cut -d'|' -f2)"
    ok "получено: $(printf '%s' "$fetched" | cut -d'|' -f3) (версия $label)"
fi

# ── 2. останавливаю прежний сервер ──────────────────────────────────────────
step "Останавливаю прежний сервер, если он работал"
killed="$(stop_server)"
if [ "${killed:-0}" -gt 0 ] 2>/dev/null; then ok "остановлено процессов: $killed"; else ok "работающего сервера нет"; fi

# ── 3. файлы ────────────────────────────────────────────────────────────────
step "Ставлю файлы программы"
if ! install_files "$source_path"; then
    bad "Установка не удалась."
    note "Если сервер ещё работает — закройте его и запустите установку снова."
    exit 4
fi
ok "файлы на месте: $HOME_DIR"

# ── 4. плагин ───────────────────────────────────────────────────────────────
step "Ставлю плагин в Hermes Desktop"
plugin_ok=0
install_plugin && plugin_ok=1
if [ "$plugin_ok" = "0" ]; then
    note "Приложение всё равно откроется в браузере; плагин добавим, когда Hermes появится."
fi

# ── 5. автозапуск ───────────────────────────────────────────────────────────
# Лаунчеры bin/*.sh берут порт, python и каталог зеркала отсюда — автозапуск
# работает ровно так же, как установка, без повторного разбора окружения.
mkdir -p "$DATA_DIR/run" "$DATA_DIR/logs" 2>/dev/null
printf '%s\n' "$PORT" > "$DATA_DIR/run/port.txt"
printf '%s\n' "$PY" > "$DATA_DIR/run/python.txt"
printf '%s\n' "$TEAMROOM_VAULT_DIR" > "$DATA_DIR/run/vault.txt"

autostart_how="выключен"
if [ "$NO_AUTOSTART" = "1" ]; then
    step "Автозапуск не включаю (ключ --no-autostart)"
    removed="$(remove_autostart)"
    if [ -n "$removed" ]; then ok "прежний автозапуск убран: $removed"; else ok "прежнего автозапуска нет"; fi
else
    step "Включаю автозапуск"
    case "$OS" in
        macos) note "launchd: ~/Library/LaunchAgents/$LAUNCHD_LABEL.plist" ;;
        linux) note "systemd --user: ~/.config/systemd/user/$SERVICE_NAME.service" ;;
        *)     note "на этой системе автозапуск не настраивается" ;;
    esac
    if set_autostart; then
        case "$OS" in
            macos) autostart_how="launchd" ;;
            linux) autostart_how="systemd --user" ;;
            *)     autostart_how="строка в профиле" ;;
        esac
    fi
fi

# ── 6. запуск ───────────────────────────────────────────────────────────────
step "Запускаю сервер"
mkdir -p "$DATA_DIR/run" "$DATA_DIR/logs" 2>/dev/null
printf '%s\n' "$PORT" > "$DATA_DIR/run/port.txt"
printf '%s\n' "$PY" > "$DATA_DIR/run/python.txt"
printf '%s\n' "$TEAMROOM_VAULT_DIR" > "$DATA_DIR/run/vault.txt"

launcher="$HOME_DIR/bin/team-room-server.sh"
pid_file="$DATA_DIR/run/server-$PORT.pid"
already=0
if http_health "$PORT"; then
    already=1
fi
# Лаунчер зовём всегда: он либо поднимет сервер, либо найдёт уже работающий
# и запишет его pid (без pid-файла остановка и обновление не работают).
launcher_rc=0
if [ -x "$launcher" ]; then
    "$launcher" --home "$HOME_DIR" --port "$PORT" --wait "$WAIT_SECONDS" --quiet || launcher_rc=$?
fi

if [ "$launcher_rc" = "3" ]; then
    warn "порт $PORT занят другой программой — наш сервер не запущен"
    note "Освободите порт или поставьте с другим: install.sh --port 8788"
fi

if health_wait "$PORT" "$WAIT_SECONDS"; then
    alive=1
    own_server=1
    if [ -f "$pid_file" ]; then
        if [ "$already" = "1" ] && [ "$launcher_rc" = "0" ]; then
            ok "сервер уже работал — второй не запускал"
        else
            ok "сервер отвечает: $URL"
        fi
    else
        # Сервер отвечает, но pid-файла нет: это не наш процесс (чужой сервер на
        # том же порту) либо лаунчер не смог его найти. Обещать «работает» нельзя.
        own_server=0
        warn "на порту $PORT отвечает не наш сервер — pid-файла нет"
        note "Такой сервер не остановить и не обновить отсюда. Возьмите другой порт: --port 8788"
    fi
else
    warn "сервер не ответил за $WAIT_SECONDS секунд"
    note "Логи: $DATA_DIR/logs"
    alive=0
    own_server=0
fi

# ── 7. запись о версии ──────────────────────────────────────────────────────
{
    printf '{\n'
    printf '  "version": "%s",\n' "$label"
    printf '  "port": %s,\n' "$PORT"
    printf '  "home": "%s",\n' "$HOME_DIR"
    printf '  "data": "%s",\n' "$DATA_DIR"
    printf '  "python": "%s",\n' "$PY"
    printf '  "installed_at": "%s",\n' "$(date '+%Y-%m-%d %H:%M:%S')"
    printf '  "autostart": "%s"\n' "$autostart_how"
    printf '}\n'
} > "$MANIFEST" 2>/dev/null || note "не смог записать $MANIFEST"

# ── 8. страница ────────────────────────────────────────────────────────────
open_browser

# ── итог ────────────────────────────────────────────────────────────────────
printf '\n  %s\n' "----------------------------------------------"
if [ "$alive" = "1" ] && [ "$own_server" = "1" ]; then
    printf '  %sГотово. Командные комнаты: %s%s\n' "$C_OK" "$URL" "$C_OFF"
elif [ "$alive" = "1" ]; then
    printf '  %sФайлы установлены, но на порту %s отвечает чужой сервер.%s\n' "$C_WARN" "$PORT" "$C_OFF"
    printf '  Запустить свой: %s --port 8788\n' "$launcher"
else
    printf '  %sФайлы установлены, но сервер пока не отвечает.%s\n' "$C_WARN" "$C_OFF"
    printf '  Запустить вручную: %s\n' "$launcher"
fi
printf '\n  Как открыть в Hermes Desktop:\n'
printf '      %sлевое меню → «Командные комнаты»   (или Ctrl+K → «командные»)%s\n' "$C_NOTE" "$C_OFF"
if [ "$plugin_ok" = "0" ]; then
    printf '      %sплагина пока нет — откройте адрес выше в браузере%s\n' "$C_NOTE" "$C_OFF"
fi
printf '\n'
printf '  Программа:  %s\n' "$HOME_DIR"
printf '  Данные:     %s\n' "$DATA_DIR"
printf '  Автозапуск: %s\n' "$autostart_how"
printf '\n  Обновить — запустите эту же команду снова: данные сохранятся.\n'
printf '  Удалить:    install.sh --uninstall   (данные останутся)\n\n'

rm -rf "$work" 2>/dev/null || true

[ "$alive" = "1" ] && [ "$own_server" = "1" ] && exit 0
exit 1
