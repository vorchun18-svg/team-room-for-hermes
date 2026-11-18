#!/usr/bin/env python
"""Мост к штатному генератору названий Hermes: ``agent.title_generator``.

Зачем мост: название запроса должно формироваться ТЕМ ЖЕ механизмом, что название
сеанса в Hermes Agent (`hermes-agent/agent/title_generator.py`), а не своей моделью
и не своими правилами — иначе у другого пользователя будет работать его настройка,
а у нас наша. Скрипт запускается интерпретатором Hermes (в нём есть пакет ``agent``
и его зависимости) через ``server/proc.py`` — без окна и в фоне.

Режимы (текст — из stdin, чтобы не упираться в длину командной строки):

    python tools/title_bridge.py --derive      < текст   # мгновенное название
    python tools/title_bridge.py --generate    < текст   # + улучшение малой моделью
    python tools/title_bridge.py --check                 # есть ли модуль/модель

Ответ — один JSON в stdout. Коды возврата: 0 — всё сделано (в т.ч. запасной
вариант), 2 — ошибка аргументов, 1 — непредвиденная ошибка. Мост НИКОГДА не
печатает трассировку: вызывающая сторона получает JSON с ``ok: false``.

Что важно знать про модуль Hermes (проверено чтением исходника):

* ``derive_title(user_message, title_preview=None) -> str | None`` — первая
  осмысленная строка, ≤ 48 знаков по границе слова, без модели, не падает;
* ``generate_title(user_message, timeout=None, failure_callback=None,
  main_runtime=None, runtime_validator=None, title_preview=None) -> str | None`` —
  один вызов ``agent.auxiliary_client.call_llm(task="title_generation")`; модель и
  язык берутся из ``auxiliary.title_generation`` конфига Hermes
  (``hermes_cli.config.load_config_readonly``), ``enabled=false`` выключает;
* приоритет источников (derived < llm < user) обеспечивает ХРАНИЛИЩЕ сессий Hermes;
  у нас его роль играет реестр запросов (``server/titles.py``).

Секреты не читаются и не печатаются: из конфига берутся только имя провайдера,
модели и язык названия.
"""
from __future__ import annotations

import json
import os
import re
import sys

#: переменные агентской сессии: доска/задача родителя не должны влиять на вызов
CLEAN_ENV = ("HERMES_DELEGATED_CHILD_CONTEXT", "HERMES_KANBAN_DB", "HERMES_KANBAN_TASK",
             "HERMES_KANBAN_BOARD", "HERMES_KANBAN_HOME", "HERMES_KANBAN_WORKSPACES_ROOT",
             "HERMES_KANBAN_ATTACHMENTS_ROOT")
#: сколько знаков в запасном названии (столько же у нашего интерфейса)
FALLBACK_LIMIT = 60
_FILLERS = ("так", "итак", "в общем", "в целом", "ну", "значит", "смотри", "смотрите",
            "вот", "ладно", "хорошо", "окей", "ок", "короче", "давай", "давайте")
_FILLER_RE = re.compile(r"^(?:%s)\s*[,.:;!—–-]\s*" % "|".join(_FILLERS), re.IGNORECASE)


def _clean_env() -> None:
    for key in CLEAN_ENV:
        os.environ.pop(key, None)


def _agent_paths() -> list:
    """Где искать пакет ``agent`` (по порядку)."""
    import pathlib
    roots = []
    for value in (os.environ.get("HERMES_AGENT_HOME"), os.environ.get("HERMES_AGENT_PATH"),
                  os.environ.get("HERMES_HOME")):
        if value:
            roots.append(pathlib.Path(value))
            roots.append(pathlib.Path(value) / "hermes-agent")
    local = os.environ.get("LOCALAPPDATA")
    if local:
        roots.append(pathlib.Path(local) / "hermes" / "hermes-agent")
    return [str(root) for root in roots if (root / "agent").is_dir()]


def load_module():
    """Импорт ``agent.title_generator``; ``None`` — Hermes недоступен (не падаем)."""
    try:
        import agent.title_generator as title_generator   # noqa: F401
        return title_generator
    except Exception:  # noqa: BLE001 - нет пакета/битая установка
        pass
    for path in _agent_paths():
        if path not in sys.path:
            sys.path.insert(0, path)
        try:
            import agent.title_generator as title_generator   # noqa: F401
            return title_generator
        except Exception:  # noqa: BLE001
            continue
    return None


def fallback_derive(text: str, limit: int = FALLBACK_LIMIT) -> str:
    """Запасное название без Hermes: первая осмысленная строка по границе слова."""
    value = " ".join(str(text or "").replace("\u00a0", " ").split())
    for _ in range(4):
        match = _FILLER_RE.match(value)
        if not match:
            break
        value = value[match.end():].strip()
    line = ""
    for candidate in value.splitlines():
        candidate = candidate.strip()
        if candidate:
            line = candidate
            break
    if not line:
        return ""
    if len(line) <= limit:
        return line
    head = line[: max(1, limit - 1)]
    space = head.rfind(" ")
    if space > limit // 2:
        head = head[:space]
    return head.rstrip(" ,.;:—-") + "…"


def _model_info(title_generator) -> dict:
    """Провайдер/модель/язык названия из конфига Hermes (read-only, без секретов)."""
    try:
        cfg = title_generator._title_config()          # noqa: SLF001 - штатное чтение
    except Exception:  # noqa: BLE001
        return {}
    if not isinstance(cfg, dict):
        return {}
    return {"provider": str(cfg.get("provider") or ""), "model": str(cfg.get("model") or ""),
            "language": str(cfg.get("language") or ""),
            "enabled": cfg.get("enabled", True)}


def derive_payload(text: str, title_generator=None) -> dict:
    """``{ok, derived, source}``: название от Hermes или запасное (никогда не пусто)."""
    local = fallback_derive(text)
    if title_generator is not None:
        try:
            value = title_generator.derive_title(text)
        except Exception:  # noqa: BLE001 - запасной вариант всегда есть
            value = None
        value = " ".join(str(value or "").split())
        if value:
            return {"ok": True, "derived": value[:FALLBACK_LIMIT], "source": "hermes"}
    return {"ok": True, "derived": local, "source": "fallback" if local else "none"}


def generate_payload(text: str, timeout=None) -> dict:
    """``{ok, derived, title, source}`` — две ступени Hermes, как в сессии."""
    title_generator = load_module()
    if title_generator is None:
        payload = derive_payload(text, None)
        payload.update({"title": "", "source": payload.get("source") or "fallback",
                        "error": "agent.title_generator не найден — Hermes недоступен"})
        return payload
    derived = derive_payload(text, title_generator)
    out = {"ok": True, "derived": derived.get("derived") or "",
           "derived_source": derived.get("source") or "", "title": "", "source": "derived",
           "model": _model_info(title_generator)}
    try:
        title = title_generator.generate_title(text, timeout=timeout)
    except Exception as exc:  # noqa: BLE001 - модель не ответила: остаётся derived
        out["ok"] = False
        out["error"] = "%s: %s" % (exc.__class__.__name__, str(exc)[:200])
        return out
    title = " ".join(str(title or "").split())
    if title:
        out["title"] = title[:FALLBACK_LIMIT]
        out["source"] = "llm"
    return out


def check_payload() -> dict:
    """Есть ли модуль названий и какая модель настроена (модель НЕ вызывается)."""
    title_generator = load_module()
    if title_generator is None:
        return {"ok": False, "code": "unavailable",
                "error": "agent.title_generator не найден",
                "paths": _agent_paths()}
    out = {"ok": True, "module": getattr(title_generator, "__file__", ""),
           "model": _model_info(title_generator)}
    try:
        out["enabled"] = bool(title_generator._auto_title_enabled())          # noqa: SLF001
        out["model_upgrade"] = bool(title_generator._model_title_upgrade_enabled())  # noqa: SLF001
    except Exception:  # noqa: BLE001
        out["enabled"] = True
    try:
        out["max_input"] = int(getattr(title_generator, "MAX_TITLE_INPUT_CHARS", 1000))
        out["max_derived"] = int(getattr(title_generator, "MAX_DERIVED_TITLE_CHARS", 48))
    except Exception:  # noqa: BLE001
        pass
    return out


def _parse(argv: list) -> dict:
    opts = {"mode": "", "timeout": None}
    for token in argv:
        if token in ("--derive", "--generate", "--check"):
            opts["mode"] = token[2:]
        elif token.startswith("--timeout="):
            try:
                opts["timeout"] = float(token.split("=", 1)[1])
            except ValueError:
                raise SystemExit(json.dumps({"ok": False, "code": "bad_request",
                                             "error": "кривой --timeout"}, ensure_ascii=False))
        else:
            raise SystemExit(json.dumps({"ok": False, "code": "bad_request",
                                         "error": "Неизвестный аргумент: %s" % token},
                                        ensure_ascii=False))
    return opts


def main(argv: list, stdin=None) -> int:
    opts = _parse(argv)
    _clean_env()
    os.environ.setdefault("PYTHONIOENCODING", "utf-8")
    if opts["mode"] == "check":
        payload = check_payload()
    elif opts["mode"] in ("derive", "generate"):
        text = (stdin if stdin is not None else sys.stdin).read()
        if not text.strip():
            print(json.dumps({"ok": False, "code": "bad_request", "error": "Пустой текст"},
                             ensure_ascii=False))
            return 2
        payload = (derive_payload(text, load_module()) if opts["mode"] == "derive"
                   else generate_payload(text, opts["timeout"]))
    else:
        print(json.dumps({"ok": False, "code": "bad_request",
                          "error": "Нужен режим --derive, --generate или --check"},
                         ensure_ascii=False))
        return 2
    print(json.dumps(payload, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv[1:]))
    except SystemExit:
        raise
    except Exception as exc:  # noqa: BLE001 - мост обязан вернуть JSON, а не трассировку
        print(json.dumps({"ok": False, "code": "engine_error",
                          "error": "%s: %s" % (exc.__class__.__name__, str(exc)[:300])},
                         ensure_ascii=False))
        sys.exit(1)
