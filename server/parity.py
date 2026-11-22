# -*- coding: utf-8 -*-
"""«Совместимость с Hermes» (W29B-Q2): участник комнаты против обычного запуска.

Зачем
-----
Комната — продолжение Hermes, а не надстройка: у каждого участника есть профиль
Hermes со своими навыками, MCP-серверами, разрешениями и моделью. Комната может
переопределять часть этого (модель участника, уровень автономии), и тогда
запуск карточки перестаёт быть «обычным запуском агента» — но человек об этом
нигде не узнавал. Экран показывает по каждому участнику: что взято из профиля,
что из общих настроек Hermes, что задала комната, и где это расходится.

Правила
--------
* Только чтение: конфиги профилей и ``profile.yaml`` открываются на чтение,
  реальный Hermes CLI не запускается, ничего не пишется (ни в профили, ни в
  реестр комнат).
* Наружу не уходят ни полные пути, ни ключи: каталог профиля — ``profiles/<имя>``
  или «корень Hermes», значения — только имена моделей/серверов/навыков.
* Источник значения честный: ``profile`` (свой ``config.yaml`` профиля),
  ``global`` (общие настройки Hermes) или ``none`` (не задано нигде). Модель
  считается ОДНОЙ парой провайдер+модель — как в ``agent_models``.
* Ошибка чтения одного профиля не роняет экран: участник получает статус ``off``
  с причиной.

Публично: ``report(cfg, room)``, ``reset_model(cfg, room, profile)``,
``STATUSES``, ``SEVERITY``.
"""
from __future__ import annotations

import time
from pathlib import Path

try:                                        # пакетный и «плоский» импорт
    from . import agent_models as models
    from . import autonomy as autonomy_mod
except ImportError:                         # pragma: no cover
    import agent_models as models           # type: ignore
    import autonomy as autonomy_mod         # type: ignore

STATUSES = ("ok", "warn", "fail", "off")
#: Тяжесть расхождения: красное — только то, что меняет поведение запуска.
SEVERITY = {"fail": "fail", "warn": "warn"}

#: Ключи, которые комната умеет переопределять (остальное — только профиль).
ROOM_KEYS = ("model", "autonomy")

#: Подписи уровня автономии (словарь комнаты, но здесь — для строки экрана).
LEVEL_RU = {"careful": "Осторожный", "working": "Рабочий", "free": "Свободный"}
#: Как называть источник значения.
SOURCE_RU = {"profile": "из профиля", "global": "из общих настроек Hermes",
             "none": "не задано", "room": "задано комнатой"}


class ParityError(Exception):
    """Отказ действия экрана (``bad_request``/``not_found``) для HTTP-слоя."""

    def __init__(self, message: str, code: str = "bad_request"):
        super().__init__(message)
        self.message = message
        self.code = code


# ---------------------------------------------------------------- чтение файлов

def _now() -> int:
    return int(time.time())


def _clean(value, limit: int = 120) -> str:
    """Однострочное значение без переводов строк (конфиг мог быть испорчен)."""
    text = " ".join(str(value if value is not None else "").split())
    return text[:limit]


def _load_yaml(path: Path) -> tuple[dict, str]:
    """Прочитать YAML только на чтение: ``(данные, чем читали)``.

    Нет файла — ``({}, "none")``; нет PyYAML — ``({}, "no_yaml")``: экран честно
    скажет «не прочитано», а не покажет пустоту за «всё в порядке».
    """
    if not path.is_file():
        return {}, "none"
    try:
        text = path.read_text("utf-8-sig", errors="replace")
    except OSError:
        return {}, "none"
    try:
        import yaml                                     # noqa: PLC0415 - есть не всегда
    except Exception:                                   # noqa: BLE001
        return {}, "no_yaml"
    try:
        data = yaml.safe_load(text) or {}
    except Exception:                                   # noqa: BLE001
        return {}, "bad_yaml"
    return (data if isinstance(data, dict) else {}), "yaml"


def _profile_rel(cfg, profile: str) -> str:
    """Каталог профиля ДЛЯ ПОКАЗА: без имени пользователя и буквы диска."""
    name = str(profile or "default")
    return "корень Hermes" if name in ("", "default") else "profiles/" + name


def _merged(cfg, profile: str) -> dict:
    """Настройки профиля поверх общих настроек Hermes + ИСТОЧНИК каждого ключа.

    ``values`` — только разделы, которые нужны экрану; ``sources[key]`` —
    ``profile`` (ключ есть в своём конфиге), ``global`` (взят из корневого) или
    ``none``. Так же считает модель ``agent_models.default_info`` — расхождений
    между экраном и карточкой быть не должно.
    """
    name = str(profile or "default")
    own, own_by = _load_yaml(models.config_path(cfg, name))
    shared, shared_by = _load_yaml(Path(getattr(cfg, "hermes_home", "") or "") / "config.yaml")
    out = {"values": {}, "sources": {}, "read_by": own_by or shared_by, "profile": name}
    for key in ("platform_toolsets", "known_builtin_toolsets", "tools", "mcp_servers",
                "approvals", "command_allowlist", "agent", "delegation"):
        if key in own:
            out["values"][key] = own.get(key)
            out["sources"][key] = "profile"
        elif key in shared:
            out["values"][key] = shared.get(key)
            out["sources"][key] = "global"
        else:
            out["values"][key] = None
            out["sources"][key] = "none"
    return out


def _skills(cfg, profile: str) -> dict:
    """Сколько навыков у профиля и сколько общих (считаем ``SKILL.md``, чтение)."""
    root = Path(getattr(cfg, "hermes_home", "") or "")
    name = str(profile or "default")
    own_dir = (root / "skills") if name in ("", "default") else (root / "profiles" / name / "skills")
    shared_dir = root / "skills"
    own = _count_skills(own_dir)
    shared = _count_skills(shared_dir)
    return {"own": own, "shared": shared, "total": max(own, shared),
            "source": "profile" if own else ("global" if shared else "none")}


def _count_skills(path: Path) -> int:
    try:
        if not path.is_dir():
            return 0
        return sum(1 for _ in path.glob("*/**/SKILL.md"))
    except OSError:
        return 0


def _description(cfg, profile: str) -> str:
    """Описание профиля из ``profile.yaml`` (только чтение, без CLI)."""
    root = Path(getattr(cfg, "hermes_home", "") or "")
    name = str(profile or "default")
    folder = root if name in ("", "default") else root / "profiles" / name
    data, _by = _load_yaml(folder / "profile.yaml")
    return _clean(data.get("description"), 120)


def _mcp(values: dict, source: str) -> dict:
    raw = values.get("mcp_servers")
    names = sorted(_clean(k, 40) for k in raw) if isinstance(raw, dict) else []
    return {"names": names, "count": len(names), "source": source}


def _tools(values: dict, source: str) -> dict:
    """Наборы инструментов: только имена и число — без внутренних путей."""
    raw = values.get("platform_toolsets")
    names: list = []
    if isinstance(raw, dict):
        for key, item in raw.items():
            if item in (None, True, "all", "auto"):
                names.append(_clean(key, 40))
            elif isinstance(item, list):
                names.append(_clean(key, 40))
    elif isinstance(raw, list):
        names = [_clean(item, 40) for item in raw]
    custom = values.get("tools")
    disabled = []
    if isinstance(custom, dict) and isinstance(custom.get("disabled"), list):
        disabled = [_clean(item, 40) for item in custom["disabled"]][:20]
    return {"names": sorted(set(names))[:20], "count": len(set(names)),
            "disabled": disabled, "source": source if (names or disabled) else "none"}


def _permissions(cfg, room: dict, profile: str, values: dict, sources: dict) -> dict:
    """Политика разрешений: что записано в профиле и что задаёт комната."""
    approvals = values.get("approvals") if isinstance(values.get("approvals"), dict) else {}
    allowlist = values.get("command_allowlist")
    allow = [str(item) for item in allowlist] if isinstance(allowlist, list) else []
    deny = approvals.get("deny")
    deny_list = [str(item) for item in deny] if isinstance(deny, list) else []
    level = autonomy_mod.room_settings(room or {}).get("level") or "working"
    try:
        state = autonomy_mod.profile_state(cfg, profile)
        allow = state.get("allowlist") or allow
        deny_list = state.get("deny") or deny_list
        single = state.get("single_query_mode") or ""
    except Exception:                                    # noqa: BLE001
        single = str(approvals.get("single_query_mode") or "").strip().lower()
    return {"mode": _clean(approvals.get("mode"), 40),
            "single_query_mode": single or "deny",
            "allowlist": allow, "allowlist_count": len(allow),
            "deny_count": len(deny_list),
            "level": level, "level_label": LEVEL_RU.get(level, level),
            "source": sources.get("approvals", "none")}


# ---------------------------------------------------------------- расхождения

def _diff(code: str, severity: str, text: str, fix: dict | None = None) -> dict:
    return {"code": code, "severity": severity, "text": text, "fix": fix or {}}


def _model_diffs(member: dict, room_pair: tuple, profile_pair: tuple) -> list:
    """Модель: комната против профиля. Красное — только смена модели запуска."""
    out = []
    room_p, room_m = room_pair
    prof_p, prof_m = profile_pair
    if member.get("model", {}).get("source") != "room":
        return out
    if prof_p and prof_m and (room_p, room_m) != (prof_p, prof_m):
        out.append(_diff(
            "model_mismatch", "fail",
            "Комната запускает «%s» (%s), а профиль Hermes — «%s» (%s). "
            "Карточки этой комнаты пойдут не на модель профиля."
            % (room_m, room_p, prof_m, prof_p),
            {"kind": "reset_room_model", "profile": member["profile"],
             "label": "Вернуть модель профиля"}))
    elif not (prof_p and prof_m):
        out.append(_diff(
            "model_unknown", "warn",
            "В профиле модель не задана, а комната задаёт «%s» (%s): в обычном "
            "запуске агент пошёл бы на общую настройку Hermes." % (room_m, room_p),
            {"kind": "open_models", "profile": member["profile"],
             "label": "Открыть модель участника"}))
    return out


def _autonomy_diffs(member: dict) -> list:
    """Разрешения: комната обещает больше, чем профиль разрешает без человека."""
    out = []
    perm = member.get("permissions") or {}
    level = perm.get("level")
    if level == "careful":
        return out
    auto = perm.get("allowlist") or []
    if perm.get("single_query_mode") == "deny" and not auto:
        out.append(_diff(
            "autonomy_gap", "warn",
            "В комнате уровень «%s», а профиль по-прежнему спрашивает человека на "
            "каждый запуск скрипта: карточка встанет на паузу вместо работы."
            % (perm.get("level_label") or level),
            {"kind": "open_autonomy", "profile": member["profile"],
             "label": "Открыть «Автономию»"}))
    return out


def _member_status(diffs: list, read_by: str) -> str:
    if read_by in ("no_yaml", "bad_yaml"):
        return "off"
    if any(item["severity"] == "fail" for item in diffs):
        return "fail"
    if diffs:
        return "warn"
    return "ok"


def _member(cfg, room: dict, profile: str, role: str) -> dict:
    """Один участник: профиль, модель, инструменты, навыки, MCP, разрешения."""
    name = str(profile or "")
    merged = _merged(cfg, name)
    values, sources = merged["values"], merged["sources"]
    effective = models.effective(cfg, room, name)
    default = models.default_info(cfg, name)
    skills = _skills(cfg, name)
    member = {
        "profile": name,
        "role": role,
        "label": models.role_ru(room, name) or role,
        "description": _description(cfg, name),
        "profile_dir": _profile_rel(cfg, name),
        "read_by": merged["read_by"],
        "model": {"provider": effective.get("provider") or "",
                  "model": effective.get("model") or "",
                  "reasoning": effective.get("reasoning") or "",
                  "source": effective.get("source") or "agent"},
        "profile_model": {"provider": default.get("provider") or "",
                          "model": default.get("model") or "",
                          "reasoning": default.get("reasoning") or "",
                          "source": default.get("source") or "none"},
        "tools": _tools(values, sources.get("platform_toolsets") or "none"),
        "skills": skills,
        "mcp": _mcp(values, sources.get("mcp_servers") or "none"),
        "permissions": _permissions(cfg, room, name, values, sources),
    }
    diffs = _model_diffs(member, (member["model"]["provider"], member["model"]["model"]),
                         (member["profile_model"]["provider"], member["profile_model"]["model"]))
    diffs += _autonomy_diffs(member)
    if not profile_exists(cfg, name):
        diffs.insert(0, _diff("no_profile", "warn",
                              "Профиль «%s» не найден: карточка создастся, но запуск "
                              "агента в Hermes не состоится." % name))
    if merged["read_by"] in ("no_yaml", "bad_yaml"):
        diffs.insert(0, _diff("config_unreadable", "warn",
                              "Настройки профиля не читаются (%s): совместимость "
                              "не проверена." % ("нет библиотеки YAML"
                                                 if merged["read_by"] == "no_yaml"
                                                 else "файл испорчен")))
    member["status"] = _member_status(diffs, merged["read_by"])
    member["diffs"] = diffs
    member["differences"] = _differences(member, values, sources)
    return member


def profile_exists(cfg, profile: str) -> bool:
    """Есть ли у профиля свой конфиг или каталог (у ``default`` — корень Hermes)."""
    name = str(profile or "default")
    root = Path(getattr(cfg, "hermes_home", "") or "")
    if name in ("", "default"):
        return (root / "config.yaml").is_file()
    return (root / "profiles" / name).is_dir()


def _differences(member: dict, values: dict, sources: dict) -> list:
    """«Чем запуск в комнате отличается от обычного запуска агента в Hermes»."""
    out = []
    model = member.get("model") or {}
    prof = member.get("profile_model") or {}
    if model.get("source") == "room":
        out.append("модель задана комнатой: «%s» (%s)" % (model.get("model") or "—",
                                                         model.get("provider") or "—"))
    elif prof.get("source") == "global":
        out.append("модель взята из общих настроек Hermes (в профиле не задана)")
    if model.get("reasoning") and model.get("reasoning") != prof.get("reasoning"):
        out.append("уровень мышления: %s (в профиле %s)"
                   % (model.get("reasoning") or "—", prof.get("reasoning") or "не задан"))
    perm = member.get("permissions") or {}
    if perm.get("level") != "careful":
        out.append("уровень автономии комнаты: «%s» (%s разрешений записано)"
                   % (perm.get("level_label") or "—", perm.get("allowlist_count") or 0))
    if sources.get("mcp_servers") == "global":
        out.append("MCP-серверы — из общих настроек Hermes, у профиля своих нет")
    if not out:
        out.append("совпадает с обычным запуском агента в Hermes")
    return out


# ---------------------------------------------------------------- отчёт

def report(cfg, room) -> dict:
    """``GET /api/rooms/{id}/parity`` — совместимость участников с обычным Hermes."""
    room = room if isinstance(room, dict) else {}
    room_id = str(room.get("id") or "")
    if not room_id:
        raise ParityError("Нужна комната: совместимость считается по её участникам", "bad_request")
    members = []
    for profile, role in models.participants(room):
        try:
            members.append(_member(cfg, room, profile, role))
        except Exception as exc:                         # noqa: BLE001 — экран не падает
            members.append({"profile": str(profile), "role": str(role),
                            "label": str(profile), "description": "", "profile_dir": "",
                            "read_by": "none", "status": "off",
                            "model": {}, "profile_model": {}, "skills": {},
                            "tools": {}, "mcp": {}, "permissions": {},
                            "differences": [], "diffs": [_diff(
                                "read_failed", "warn",
                                "Не удалось прочитать настройки профиля «%s»: %s"
                                % (profile, _clean(exc, 160)))]})
    summary = {key: sum(1 for item in members if item["status"] == key) for key in STATUSES}
    summary["total"] = len(members)
    level = autonomy_mod.room_settings(room).get("level") or "working"
    return {
        "room": room_id,
        "room_title": _clean(room.get("title"), 120),
        "level": level, "level_label": LEVEL_RU.get(level, level),
        "members": members,
        "summary": summary,
        "fixable": [item["fix"] for member in members for item in member.get("diffs") or []
                    if isinstance(item.get("fix"), dict) and item["fix"].get("kind") == "reset_room_model"],
        "notes": [
            "Экран только читает: профили Hermes, общие настройки и реестр комнаты не меняются.",
            "Модель — одна пара «провайдер + модель»; источник «из общих настроек» значит, "
            "что в профиле модели нет.",
            "Правки профилей Hermes здесь недоступны намеренно: безопасно только вернуть "
            "модель комнаты к модели профиля.",
        ],
        "at": _now(),
    }


def reset_model(cfg, room, profile: str) -> dict:
    """Убрать замену модели участника в реестре комнаты (профиль не трогаем).

    Это единственное «исправление» экрана: штатный путь ``agent_models.clear``
    (тот же, что кнопка «как в профиле» в настройках модели), ничего не пишет в
    профили Hermes и не запускает агентов.
    """
    room = room if isinstance(room, dict) else {}
    name = str(profile or "").strip()
    if not name:
        raise ParityError("Не указан участник", "bad_request")
    if name not in [p for p, _role in models.participants(room)]:
        raise ParityError("«%s» не в составе комнаты" % name, "bad_request")
    models.clear(cfg, str(room.get("id") or ""), name)
    return report(cfg, room)


__all__ = ["report", "reset_model", "profile_exists", "STATUSES", "SEVERITY",
           "ParityError", "LEVEL_RU", "SOURCE_RU"]
