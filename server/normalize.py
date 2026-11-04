"""Каноническая нормализация `metadata` ответа корневой карточки.

Единственная реализация (ARCHITECTURE §12.8): и `engine_read`, и `ingest`
импортируют `normalize_metadata` отсюда, а не дублируют логику.

Вход — то, что агент положил в `kanban_complete(metadata=...)`. Реальный формат
бывает трёх видов и всё это должно переживаться без исключений:

* новый контракт (ARCHITECTURE §5): объекты `{text, owner, due}`, `stage` с
  `{current, phases[]}`, `artifacts` объектами;
* legacy (реальный `t_605613e0`): `next_steps` — список строк, `open_questions`
  — строки, `stage` — словарь «область → статус-строка», `artifacts` — пути;
* мусор: `None`, строка вместо объекта, JSON-строка вместо dict, числа,
  вложенные списки, ключ с ведущим пробелом (`' contradictions_flagged'`).

Возвращается всегда один и тот же набор ключей:

    {
      "next_steps":       [{"id","text","owner","due","state"}],
      "open_questions":   [{"id","text","for","answered"}],
      "decisions":        [{"id","text","why","status"}],
      "stage":            {"current": str|None,
                           "phases": [{"id","title","status","note"}]},
      "future_questions": [str],
      "artifacts":        [{"path","title","kind"}],
      "report":           str|None,
      "verdict":          "approve"|"revise"|None,
      "issues":           [{"where","problem","fix"}],
      "extra":            {неизвестные ключи как есть},
    }

`id` стабильны между вызовами и версиями ответа: `<префикс>_<sha1(text)[:8]>`
(`ns_`, `q_`, `d_`). Совпадающие тексты получают детерминированный суффикс
`_2`, `_3`, … — чтобы id оставались уникальными в пределах одного ответа.

`stage.phases` всегда содержит 6 канонических фаз в фиксированном порядке
(API §3.4); недостающие заполняются `next`, незнакомые фазы агента
дописываются в конец, а незнакомые области legacy-словаря складываются в
`extra["stage_unmapped"]`.

Фолбэк из `summary_md` работает ТОЛЬКО когда соответствующего ключа нет в
metadata: раздел с заголовком «след…»/«дальше»/«next step» → `next_steps`,
«вопрос…» → `open_questions`, «решени…» → `decisions`.

Волна 9 добавляет ДВА необязательных блока — `plan` и `plan_patch` (карта
проекта руководителя). Канонические ключи выше не меняются: карта разбирается
отдельной функцией `normalize_plan_blocks(meta, room)` (словарь поверх
нормализованной формы), а сырые значения остаются в `extra`.
"""
from __future__ import annotations

from typing import Any

try:  # пакетный и «плоский» импорт (python server/app.py)
    from .normalize_base import (PHASE_IDS, PHASE_TITLES, PHASES, STATES, _as_dict, _clean_keys,
                                 artifact_kind, parse_due, stable_id)
    from .normalize_meta import (_artifacts, _decisions, _future_questions, _issues, _next_steps,
                                 _open_questions, _parse_markdown, _report, _verdict)
    from .normalize_stage import canonical_phase_id, canonical_stage
except ImportError:  # pragma: no cover
    from normalize_base import (PHASE_IDS, PHASE_TITLES, PHASES, STATES, _as_dict, _clean_keys,  # type: ignore
                                artifact_kind, parse_due, stable_id)
    from normalize_meta import (_artifacts, _decisions, _future_questions, _issues, _next_steps,  # type: ignore
                                _open_questions, _parse_markdown, _report, _verdict)
    from normalize_stage import canonical_phase_id, canonical_stage  # type: ignore

__all__ = [
    "PHASES",
    "PHASE_IDS",
    "PHASE_TITLES",
    "STATES",
    "normalize_metadata",
    "normalize_plan_blocks",
    "empty_metadata",
    "looks_legacy",
    "stable_id",
    "parse_due",
    "artifact_kind",
    "canonical_phase_id",
    "canonical_stage",
]

# Раскладка: normalize_base (примитивы, фазы) ← normalize_stage (этапы) / normalize_meta (поля ответа,
# markdown-фолбэк) ← normalize (этот фасад: публичное API). Импортируйте отсюда.

# ---------------------------------------------------------------- публичное API


def empty_metadata() -> dict:
    """Пустая каноническая форма — те же ключи, что и у `normalize_metadata`."""
    return {
        "next_steps": [],
        "open_questions": [],
        "decisions": [],
        "stage": {"current": None, "phases": [
            {"id": pid, "title": title, "status": "next", "note": ""} for pid, title in PHASES
        ]},
        "future_questions": [],
        "artifacts": [],
        "report": None,
        "verdict": None,
        "issues": [],
        "extra": {},
    }


def looks_legacy(meta: Any) -> bool:
    """True, если metadata в старом формате (для `synthesis.legacy` и warnings)."""
    data = _clean_keys(_as_dict(meta))
    if not data:
        return False
    for key in ("next_steps", "open_questions"):
        value = data.get(key)
        if isinstance(value, list) and any(isinstance(v, str) for v in value):
            return True
    stage = data.get("stage")
    if isinstance(stage, dict) and "phases" not in stage:
        if any(isinstance(v, str) for k, v in stage.items() if k not in ("current", "phases")):
            return True
    return False


_KNOWN = {"next_steps", "open_questions", "decisions", "stage", "future_questions",
          "artifacts", "report", "verdict", "issues"}


def normalize_plan_blocks(meta, room=None) -> dict:
    """Необязательные блоки карты проекта: {plan, plan_patch, warnings} (WAVE9 §2).

    Канонические ключи ответа (`normalize_metadata`) не меняются: карта живёт
    отдельным словарём ПОВЕРХ нормализованной формы, а сырые значения по-прежнему
    остаются в `extra`. Разбор — `contract.parse_plan_metadata` (толерантный:
    нет блока — не ошибка; битый — предупреждение, ничего не применяется).
    """
    try:
        from .contract import parse_plan_metadata
    except ImportError:  # pragma: no cover
        from contract import parse_plan_metadata  # type: ignore
    try:
        return parse_plan_metadata(meta, room)
    except Exception:  # noqa: BLE001 - карта не должна ронять нормализацию ответа
        return {"plan": None, "plan_patch": None,
                "warnings": ["Блок plan не разобран — карта не создана."]}


def normalize_metadata(meta: dict | None, summary_md: str | None = None) -> dict:
    """Привести metadata ответа к канонической форме. Никогда не бросает.

    `summary_md` используется только как источник, если соответствующего ключа
    в metadata нет вовсе (не пустого списка — именно нет).
    """
    try:
        data = _clean_keys(_as_dict(meta))
    except Exception:
        data = {}
    try:
        fallback = _parse_markdown(summary_md)
    except Exception:
        fallback = {}

    try:
        stage = canonical_stage(data["stage"]) if "stage" in data else canonical_stage(None)
        unmapped = stage.pop("unmapped", None)

        out = {
            "next_steps": _next_steps(data["next_steps"] if "next_steps" in data
                                     else fallback.get("next_steps")),
            "open_questions": _open_questions(data["open_questions"] if "open_questions" in data
                                              else fallback.get("open_questions")),
            "decisions": _decisions(data["decisions"] if "decisions" in data
                                    else fallback.get("decisions")),
            "stage": stage,
            "future_questions": _future_questions(data.get("future_questions")),
            "artifacts": _artifacts(data.get("artifacts")),
            "report": _report(data.get("report")),
            "verdict": _verdict(data.get("verdict")),
            "issues": _issues(data.get("issues")),
            "extra": {},
        }
        for key, value in data.items():
            if key in _KNOWN:
                continue
            if key == "extra" and isinstance(value, dict):
                # повторная нормализация уже канонической формы не должна давать extra.extra
                for k2, v2 in value.items():
                    if isinstance(k2, str) and k2.strip() and k2.strip() not in out["extra"]:
                        out["extra"][k2.strip()] = v2
                continue
            out["extra"][key] = value
        if unmapped:
            out["extra"]["stage_unmapped"] = unmapped
        return out
    except Exception:  # pragma: no cover - защита от совсем неожиданного входа
        return empty_metadata()
