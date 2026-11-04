#!/usr/bin/env bash
# -*- coding: utf-8 -*-
#
# Командные комнаты — запуск сервера в фоне (macOS/Linux).
#
#   bin/team-room-server.sh                 # порт из data/run/port.txt, иначе 8777
#   bin/team-room-server.sh --port 8788
#   bin/team-room-server.sh --open-browser
#
# Сервер стартует через nohup: окно не открывается, терминал можно закрывать.
# Если сервер уже отвечает на /api/health, второй экземпляр не запускается.
# Пишет data/run/server-<порт>.pid и строки в data/logs/launcher.log.

set -u

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
APP="$(cd "$SCRIPT_DIR/.." && pwd)"
PORT=""
WAIT=20
QUIET=0
OPEN=0

while [ $# -gt 0 ]; do
    case "$1" in
        --home)         APP="$2"; shift ;;
        --home=*)       APP="${1#*=}" ;;
        --port)         PORT="$2"; shift ;;
        --port=*)       PORT="${1#*=}" ;;
        --wait)         WAIT="$2"; shift ;;
        --wait=*)       WAIT="${1#*=}" ;;
        --quiet|-q)     QUIET=1 ;;
        --open-browser) OPEN=1 ;;
        *) printf 'Неизвестный ключ: %s\n' "$1" >&2; exit 2 ;;
    esac
    shift
done

case "$APP" in
    [A-Za-z]:[\\/]*) APP="$(printf '%s' "$APP" | tr '\\' '/')" ;;
    *)
        # На Windows (git-bash) python — нативная программа: нужен путь C:/...
        native="$(cd "$APP" 2>/dev/null && { pwd -W 2>/dev/null || cygpath -m "$APP" 2>/dev/null || pwd; })"
        [ -n "$native" ] && APP="$native"
        ;;
esac

DATA="$APP/data"
RUN="$DATA/run"
LOGS="$DATA/logs"
mkdir -p "$RUN" "$LOGS" 2>/dev/null

say()  { [ "$QUIET" = "1" ] || printf '%s\n' "$1"; }
err()  { printf '%s\n' "$1" >&2; }

log() {
    printf '[%s] %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$1" >> "$LOGS/launcher.log" 2>/dev/null
}

read_file() {
    [ -f "$1" ] || return 0
    tr -d '\r\n' < "$1" 2>/dev/null
}

if [ -z "$PORT" ]; then
    PORT="$(read_file "$RUN/port.txt")"
    case "$PORT" in ''|*[!0-9]*) PORT="8777" ;; esac
fi
case "$PORT" in ''|*[!0-9]*) PORT="8777" ;; esac

VAULT="$(read_file "$RUN/vault.txt")"
if [ -n "$VAULT" ] && [ -z "${TEAMROOM_VAULT_DIR:-}" ]; then
    TEAMROOM_VAULT_DIR="$VAULT"
    export TEAMROOM_VAULT_DIR
fi

health() {
    if command -v curl >/dev/null 2>&1; then
        curl -fsS -m 2 --noproxy '*' "http://127.0.0.1:$PORT/api/health" >/dev/null 2>&1
        return $?
    fi
    return 1
}

# ── pid-файл ────────────────────────────────────────────────────────────────
# pid-файл обязан указывать на ЖИВОЙ процесс сервера: иначе stop не сможет его
# остановить, а установка решит, что сервер уже поднят, и не запишет pid.
PID_FILE="$RUN/server-$PORT.pid"

write_pid() {  # pid → 0, если файл записан и совпадает
    [ -n "$1" ] || return 1
    printf '%s\n' "$1" > "$PID_FILE" 2>/dev/null || return 1
    [ "$(read_file "$PID_FILE")" = "$1" ] || { rm -f "$PID_FILE" 2>/dev/null; return 1; }
    return 0
}

remove_pid() { rm -f "$PID_FILE" 2>/dev/null || true; }

pid_alive() {
    case "$1" in ''|*[!0-9]*) return 1 ;; esac
    kill -0 "$1" 2>/dev/null
}

# Процесс нашего сервера на этом порту — по командной строке.
# В git-bash `ps` печатает те же pid, что понимает `kill`, поэтому найденный
# pid годится и для pid-файла, и для остановки (nohup остаётся родителем python).
find_server_pid() {
    [ -n "$PORT" ] || return 0
    ps -ef 2>/dev/null | awk 'NR > 1 { print $2 "\t" $0 }' | while IFS="$(printf '\t')" read -r pid line; do
        case "$line" in *app.py*) ;; *) continue ;; esac
        case "$line" in *"--port $PORT"*) ;; *) continue ;; esac
        case "$line" in *"$APP"*) ;; *) continue ;; esac
        case "$line" in *python*) ;; *) continue ;; esac
        printf '%s\n' "$pid"
        break
    done
}

open_page() {
    url="http://127.0.0.1:$PORT/"
    case "$(uname -s 2>/dev/null)" in
        Darwin) command -v open >/dev/null 2>&1 && open "$url" >/dev/null 2>&1 ;;
        MINGW*|MSYS*|CYGWIN*) command -v cmd >/dev/null 2>&1 && cmd //c start "" "$url" >/dev/null 2>&1 ;;
        *) command -v xdg-open >/dev/null 2>&1 && xdg-open "$url" >/dev/null 2>&1 ;;
    esac
    return 0
}

# Python: сохранённый в run/python.txt, переменная TEAMROOM_PYTHON, затем PATH.
PY="$(read_file "$RUN/python.txt")"
if [ -z "$PY" ] || [ ! -x "$PY" ]; then
    PY="${TEAMROOM_PYTHON:-}"
fi
if [ -z "$PY" ]; then
    for candidate in python3 python3.13 python3.12 python3.11 python; do
        found="$(command -v "$candidate" 2>/dev/null || true)"
        [ -n "$found" ] || continue
        code="$("$found" -c 'import sys;print(sys.version_info[0]*100+sys.version_info[1])' 2>/dev/null | tr -dc '0-9')"
        [ -n "$code" ] && [ "$code" -ge 311 ] && { PY="$found"; break; }
    done
fi
if [ -z "$PY" ]; then
    err "Team Room: Python 3.11+ не найден. Поставьте Python и запустите install.sh снова."
    log "python not found"
    exit 2
fi

if health; then
    # Сервер уже отвечает. Это может быть и наш прежний экземпляр без pid-файла
    # (файл потеряли при обновлении или остановке) — тогда находим его процесс
    # и записываем pid, иначе stop не сможет его остановить.
    pid="$(find_server_pid || true)"
    if [ -n "$pid" ] && write_pid "$pid"; then
        printf '%s\n' "$PORT" > "$RUN/port.txt" 2>/dev/null
        log "already running pid=$pid port=$PORT (pid file refreshed)"
        say "Командные комнаты уже работают: http://127.0.0.1:$PORT/"
        [ "$OPEN" = "1" ] && open_page
        exit 0
    fi
    log "port $PORT answers, but no $APP/server/app.py process found"
    err "Порт $PORT занят другой программой: /api/health отвечает, но процесса нашего сервера на нём нет."
    err "Остановите эту программу или запустите с другим портом: --port 8788"
    exit 3
fi

URL="http://127.0.0.1:$PORT/"
LOG="$LOGS/server.log"
printf '\n===== %s запуск на порту %s =====\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$PORT" >> "$LOG" 2>/dev/null

nohup "$PY" "$APP/server/app.py" --root "$APP" --home "$DATA" --port "$PORT" \
    >> "$LOG" 2>&1 < /dev/null &
pid=$!
if write_pid "$pid"; then
    log "started pid=$pid port=$PORT"
else
    err "Team Room: не смог записать $PID_FILE"
    log "cannot write pid file $PID_FILE"
fi
printf '%s\n' "$PORT" > "$RUN/port.txt" 2>/dev/null

deadline=$(( $(date +%s) + WAIT ))
while [ "$(date +%s)" -lt "$deadline" ]; do
    if health; then
        # $! может оказаться pid обёртки (nohup), а сервер — её дочерний python.
        # Тогда pid-файл должен указывать на настоящий процесс сервера.
        real="$(find_server_pid || true)"
        if [ -n "$real" ]; then
            if [ "$real" != "$pid" ]; then
                pid="$real"
                write_pid "$pid" || err "Team Room: не смог обновить $PID_FILE"
                log "pid file points to $pid"
            fi
        elif ! pid_alive "$pid"; then
            remove_pid
            log "server answers but its process is unknown; pid file removed"
            err "Сервер отвечает, но его процесс не найден — остановить по pid не получится."
            exit 1
        fi
        log "server is up on $URL (pid=$pid)"
        say "Сервер запущен: $URL"
        say "Лог: $LOG"
        [ "$OPEN" = "1" ] && open_page
        exit 0
    fi
    if ! pid_alive "$pid"; then
        # Обёртка nohup могла выйти, оставив живой python: находим настоящий pid.
        real="$(find_server_pid || true)"
        if [ -n "$real" ]; then
            pid="$real"
            write_pid "$pid" || true
            sleep 0.4
            continue
        fi
        remove_pid
        log "server process exited early; pid file removed"
        err "Сервер завершился сразу после запуска. Последние строки лога:"
        tail -n 10 "$LOG" >&2 2>/dev/null
        exit 1
    fi
    sleep 0.4
done

remove_pid
log "server did not answer in ${WAIT}s; pid file removed"
err "Сервер не ответил за $WAIT секунд. Логи: $LOGS"
exit 1
