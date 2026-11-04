#!/usr/bin/env bash
# -*- coding: utf-8 -*-
#
# Командные комнаты — открыть страницу в браузере (сервер поднимется сам).
#
#   bin/team-room.sh

set -u

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
APP="$(cd "$SCRIPT_DIR/.." && pwd)"

PORT=""
if [ -f "$APP/data/run/port.txt" ]; then
    PORT="$(tr -d '\r\n' < "$APP/data/run/port.txt")"
fi
case "$PORT" in ''|*[!0-9]*) PORT="${TEAMROOM_PORT:-8777}" ;; esac

if ! "$SCRIPT_DIR/team-room-server.sh" --home "$APP" --port "$PORT" --open-browser; then
    printf 'Team Room: сервер не запустился. Логи: %s\n' "$APP/data/logs"
    printf 'Запустите bin/team-room-server.sh в терминале, чтобы увидеть ошибку.\n'
    exit 1
fi
exit 0
