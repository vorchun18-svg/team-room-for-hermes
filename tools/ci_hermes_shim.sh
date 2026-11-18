#!/bin/sh
# Тонкая замена CLI hermes для проверок CI (macOS/Linux): см. ci_hermes_shim.py.
# CI кладёт этот файл в PATH под именем hermes.
here=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
shim=${TEAMROOM_CI_SHIM:-$here/ci_hermes_shim.py}
if [ ! -f "$shim" ]; then
  echo "kanban: shim не найден: $shim" >&2
  exit 2
fi
if command -v python3 >/dev/null 2>&1; then
  exec python3 "$shim" "$@"
fi
exec python "$shim" "$@"
