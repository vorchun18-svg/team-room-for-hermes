#!/usr/bin/env bash
# -*- coding: utf-8 -*-
#
# Командные комнаты — остановка локального сервера (macOS/Linux).
#
#   bin/team-room-stop.sh
#   bin/team-room-stop.sh --home /путь/к/TeamRoom
#
# Берёт pid из data/run/server-*.pid и проверяет, что это действительно наш
# сервер (в командной строке есть app.py и папка приложения). Чужой процесс
# на том же порту не трогается.

set -u

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
APP="$(cd "$SCRIPT_DIR/.." && pwd)"
QUIET=0

while [ $# -gt 0 ]; do
    case "$1" in
        --home)   APP="$2"; shift ;;
        --home=*) APP="${1#*=}" ;;
        --quiet|-q) QUIET=1 ;;
        *) printf 'Неизвестный ключ: %s\n' "$1" >&2; exit 2 ;;
    esac
    shift
done

case "$APP" in
    [A-Za-z]:[\\/]*) APP="$(printf '%s' "$APP" | tr '\\' '/')" ;;
    *)
        native="$(cd "$APP" 2>/dev/null && { pwd -W 2>/dev/null || cygpath -m "$APP" 2>/dev/null || pwd; })"
        [ -n "$native" ] && APP="$native"
        ;;
esac

DATA="$APP/data"
RUN="$DATA/run"
say() { [ "$QUIET" = "1" ] || printf '%s\n' "$1"; }

read_file() { [ -f "$1" ] && tr -d '\r\n' < "$1" 2>/dev/null || true; }
PORT="$(read_file "$RUN/port.txt")"
case "$PORT" in ''|*[!0-9]*) PORT="8777" ;; esac

proc_cmdline() {
    if [ -r "/proc/$1/cmdline" ]; then
        tr '\0' ' ' < "/proc/$1/cmdline" 2>/dev/null
    else
        ps -p "$1" -o command= 2>/dev/null || ps -p "$1" 2>/dev/null | tail -n +2
    fi
}

is_ours() {
    pid="$1"
    case "$pid" in ''|*[!0-9]*) return 1 ;; esac
    line="$(proc_cmdline "$pid")"
    case "$line" in *app.py*) ;; *) return 1 ;; esac
    case "$line" in *python*) ;; *) return 1 ;; esac
    case "$line" in
        *"$APP"*|*"$DATA"*) return 0 ;;
    esac
    return 1
}

killed=0
kept=""
for file in "$RUN"/server-*.pid; do
    [ -f "$file" ] || continue
    pid="$(tr -dc '0-9' < "$file" 2>/dev/null)"
    if is_ours "$pid"; then
        if kill "$pid" 2>/dev/null; then
            n=0
            while kill -0 "$pid" 2>/dev/null && [ "$n" -lt 25 ]; do sleep 0.2; n=$((n + 1)); done
            kill -9 "$pid" 2>/dev/null || true
            killed=$((killed + 1))
        fi
        # Процесс жив — файл не трогаем: иначе следующая остановка не найдёт,
        # кого убивать, а установка решит, что сервер уже не работает.
        if kill -0 "$pid" 2>/dev/null; then
            kept="$file (pid $pid)"
            continue
        fi
    fi
    rm -f "$file" 2>/dev/null
done

# Сервер, запущенный вручную, pid-файла не оставляет: ищем только свои процессы.
# `ps -ef` есть и на macOS/Linux, и в git-bash (там pid тот же, что понимает kill).
self="$$"
for pid in $(ps -ef 2>/dev/null | awk 'NR > 1 { print $2 }'); do
    [ "$pid" = "$self" ] && continue
    if is_ours "$pid"; then
        kill "$pid" 2>/dev/null && killed=$((killed + 1))
    fi
done

if [ -n "$kept" ]; then
    printf 'Не удалось остановить: %s\n' "$kept" >&2
    exit 1
fi

if [ "$killed" -gt 0 ]; then
    say "Остановлено процессов: $killed"
else
    say "Сервер не запущен (порт $PORT, папка $APP)"
fi
exit 0
