# -*- coding: utf-8 -*-
"""«Модель, провайдер и уровень мышления» участника в мок-сервере (WAVE10).

Каталог демонстрационный и вымышленный (`fixtures/models.json`): провайдеры
«Облако-А», «Локальный сервер», «Облако-Б» и ненастроенный «Закрытый контур».
Реальных провайдеров в фикстурах нет — демо и публичный релиз должны быть чистыми.

Правила те же, что у `server/agent_models.py`:
* провайдер — только настроенный (`configured: true`), модель — только из его каталога;
* усилия (`reasoning`) — из списка `efforts`;
* по умолчанию берутся настройки самого агента (`agent_default`), замена живёт
  только в комнате (`room["agent_models"]`), нет записи — «как у агента»;
* пустой объект в `PUT …/model` — «вернуть как у агента»;
* карточки «в работе» не перезапускаются: для них отдельная кнопка `restart`,
  поэтому в ответе есть `applied.pending_running`.

Состояние — в памяти мока; файлы профилей Hermes не читаются и не пишутся.
"""
from __future__ import annotations

import copy

from mock_core import ApiError, now
from mock_notes import roster, role_of

#: короткие подписи мышления для `model_label` — как `EFFORT_SHORT` в `server/agent_models.py`
EFFORT_LABEL = {"none": "без размышления", "minimal": "минимальные", "low": "низкие",
                "medium": "средние", "high": "высокие", "xhigh": "очень высокие",
                "max": "максимальные", "ultra": "предельные"}

#: статусы карточек, которым сторож комнаты закрепляет модель (WAVE10 §14)
PINNABLE = ("triage", "todo", "ready", "blocked", "scheduled")
#: статусы «карточка в работе» — их новая модель касается только после «Перезапустить»
RUNNING = ("running", "review")


def reason_label(value, labels: dict | None = None) -> str:
    labels = labels if labels is not None else EFFORT_LABEL
    return labels.get(str(value or ""), str(value or ""))


def public_map(stored) -> dict:
    """`agent_models` комнаты: только полные записи `{provider, model, reasoning, updated_at}`."""
    out = {}
    for profile, item in (stored or {}).items():
        if not isinstance(item, dict):
            continue
        provider = str(item.get("provider") or "").strip()
        model = str(item.get("model") or "").strip()
        if not provider or not model:
            continue
        out[str(profile)] = {"provider": provider, "model": model,
                             "reasoning": str(item.get("reasoning") or "medium"),
                             "updated_at": int(item.get("updated_at") or 0)}
    return out


class MockModels:
    """Каталог моделей и настройки участников: состояние в памяти мока."""

    # --- инициализация ----------------------------------------------------

    def models_init(self) -> None:
        data = copy.deepcopy(self.fx.get("models.json") or {})
        self.catalog_providers = list(data.get("providers") or [])
        self.catalog_efforts = [e for e in (data.get("efforts") or []) if isinstance(e, dict)]
        self.effort_ids = [str(e.get("id")) for e in self.catalog_efforts]
        self.effort_labels = {str(e.get("id")): str(e.get("label") or e.get("id")) for e in self.catalog_efforts}
        self.catalog_source = str(data.get("source") or "hermes:inventory")
        self.catalog_cached = bool(data.get("cached"))
        self.catalog_updated_at = int(data.get("updated_at") or 0)
        self.catalog_warnings = [str(w) for w in data.get("warnings") or []]
        self.agent_defaults = {str(p): {k: str(v.get(k) or "") for k in ("provider", "model", "reasoning")}
                               for p, v in (data.get("agent_defaults") or {}).items() if isinstance(v, dict)}
        self.models_journal = {}       # room_id -> [события «модель изменена»], новые первыми
        self.models_seq = 0
        for room in self.room_details.values():
            room["agent_models"] = public_map(room.get("agent_models"))

    # --- каталог ----------------------------------------------------------

    def catalog(self) -> dict:
        """`GET /api/models` — форма как в docs/API.md §6.11 (`source`, `cached`, `warnings`)."""
        return {"providers": copy.deepcopy(self.catalog_providers),
                "efforts": copy.deepcopy(self.catalog_efforts),
                "source": self.catalog_source,
                "cached": self.catalog_cached,
                "updated_at": self.catalog_updated_at,
                "warnings": list(self.catalog_warnings)}

    def provider(self, provider_id) -> dict | None:
        return next((p for p in self.catalog_providers if p.get("id") == provider_id), None)

    def provider_model(self, provider_id, model_id) -> dict | None:
        provider = self.provider(provider_id) or {}
        return next((m for m in provider.get("models") or [] if m.get("id") == model_id), None)

    def configured_providers(self) -> list:
        return [p for p in self.catalog_providers if p.get("configured")]

    def agent_default(self, profile: str) -> dict:
        """Настройки самого агента (в демо — из `models.json`, у незнакомого профиля — первые из каталога)."""
        item = self.agent_defaults.get(str(profile))
        if item is None:
            provider = (self.configured_providers() or [{}])[0]
            model = (provider.get("models") or [{}])[0]
            item = {"provider": provider.get("id") or "", "model": model.get("id") or "", "reasoning": "medium"}
        return {"provider": item.get("provider"), "model": item.get("model"),
                "reasoning": item.get("reasoning") or "medium"}

    def effective(self, room: dict, profile: str) -> dict:
        """Что действует сейчас: замена комнаты или настройка агента (`source`)."""
        stored = (room.get("agent_models") or {}).get(str(profile))
        if stored:
            return {"provider": stored["provider"], "model": stored["model"],
                    "reasoning": stored["reasoning"], "source": "room"}
        return {**self.agent_default(profile), "source": "agent"}

    def settings_label(self, settings: dict) -> str:
        """«провайдер · модель · мышление» — как `model_label` у карточки."""
        provider = self.provider(settings.get("provider"))
        model = self.provider_model(settings.get("provider"), settings.get("model"))
        return "%s · %s · %s" % (provider["label"] if provider else settings.get("provider") or "—",
                                 model["label"] if model else settings.get("model") or "—",
                                 reason_label(settings.get("reasoning")))

    def model_label_for(self, room: dict, profile: str) -> str:
        return self.settings_label(self.effective(room, profile))

    # --- members в ответе комнаты -----------------------------------------

    def member_view(self, room: dict, member: dict) -> dict:
        """Строка участника: прежние поля + `model_settings` и `agent_default`."""
        out = copy.deepcopy(member)
        profile = str(member.get("profile") or "")
        effective = self.effective(room, profile)
        updated = int(((room.get("agent_models") or {}).get(profile) or {}).get("updated_at") or 0)
        out["model_settings"] = {k: effective[k] for k in ("provider", "model", "reasoning", "source")}
        out["model_settings"]["updated_at"] = updated
        out["agent_default"] = self.agent_default(profile)
        return out

    def with_models(self, room: dict) -> dict:
        """Копия комнаты для ответа: у участников — `model_settings`/`agent_default`."""
        out = copy.deepcopy(room)
        out["agent_models"] = public_map(room.get("agent_models"))
        out["members"] = [self.member_view(room, m) for m in room.get("members") or []]
        return out

    def member_row(self, room: dict, profile: str) -> dict:
        """Участник по профилю; руководитель и критик — синтетическая строка."""
        member = next((m for m in room.get("members") or [] if m.get("profile") == profile), None)
        if member is None:
            member = {"profile": profile, "role": role_of(room, profile), "note": "", "pinned": False}
        return self.member_view(room, member)

    # --- проверка и сохранение --------------------------------------------

    def _available(self, providers=None) -> str:
        return ", ".join("«%s»" % p.get("label") for p in (providers or self.configured_providers()))

    def validate_change(self, room: dict, profile: str, body) -> dict | None:
        """Проверка `{provider, model, reasoning}` → чистая настройка или `None` (сброс).

        Ошибки — `400` по-русски: профиль вне состава, ненастроенный провайдер,
        модель не из каталога, недопустимые усилия.
        """
        people = roster(room)
        if profile not in people:
            raise ApiError(400, "bad_request",
                           "Профиль «%s» не участвует в комнате «%s» — модель задают только её участникам"
                           % (profile, room.get("title") or room.get("id")),
                           {"field": "profile", "profile": profile, "available": people})
        if body is None:
            body = {}
        if not isinstance(body, dict):
            raise ApiError(400, "bad_request", "Настройки модели — объект {provider, model, reasoning}",
                           {"field": "body", "got": type(body).__name__})
        clean = {}
        for key in ("provider", "model", "reasoning"):
            value = body.get(key)
            if value is None:
                clean[key] = ""
                continue
            if not isinstance(value, str):
                raise ApiError(400, "bad_request",
                               "Поле «%s» должно быть строкой" % key,
                               {"field": key, "got": type(value).__name__})
            clean[key] = value.strip()
        if not clean["provider"] and not clean["model"]:
            return None                                  # пустой объект — вернуть как у агента
        if not clean["provider"]:
            raise ApiError(400, "bad_request",
                           "Модель указана без провайдера — выберите, где она работает. Доступно: %s"
                           % self._available(), {"field": "provider"})
        if not clean["model"]:
            provider = self.provider(clean["provider"]) or {"label": clean["provider"]}
            raise ApiError(400, "bad_request",
                           "У провайдера «%s» нужно выбрать модель" % provider["label"],
                           {"field": "model", "provider": clean["provider"]})
        provider = self.provider(clean["provider"])
        if provider is None:
            raise ApiError(400, "bad_request",
                           "Провайдера «%s» нет в каталоге. Доступно: %s"
                           % (clean["provider"], self._available(self.catalog_providers)),
                           {"field": "provider", "provider": clean["provider"]})
        if not provider.get("configured"):
            raise ApiError(400, "bad_request",
                           "Провайдер «%s» не настроен: нет рабочего ключа — выберите другой. Доступно: %s"
                           % (provider["label"], self._available()),
                           {"field": "provider", "provider": provider["id"], "reason": "provider_not_configured"})
        model = self.provider_model(provider["id"], clean["model"])
        if model is None:
            raise ApiError(400, "bad_request",
                           "У провайдера «%s» нет модели «%s». Доступны: %s"
                           % (provider["label"], clean["model"],
                              ", ".join("«%s»" % m["label"] for m in provider.get("models") or [])),
                           {"field": "model", "provider": provider["id"], "model": clean["model"],
                            "available": [m["id"] for m in provider.get("models") or []]})
        reasoning = clean["reasoning"] or self.effective(room, profile)["reasoning"]
        if reasoning not in self.effort_ids:
            raise ApiError(400, "bad_request",
                           "Усилия «%s» недопустимы. Допустимо: %s"
                           % (reason_label(reasoning, self.effort_labels),
                              ", ".join(e["label"] for e in self.catalog_efforts)),
                           {"field": "reasoning", "value": reasoning, "allowed": list(self.effort_ids)})
        return {"provider": provider["id"], "model": model["id"], "reasoning": reasoning}

    def apply_agent_models(self, room: dict, value) -> list:
        """`agent_models` в `POST/PATCH /api/rooms` — та же проверка, что у `PUT …/model`."""
        if value is None:
            return []
        if not isinstance(value, dict):
            raise ApiError(400, "bad_request", "agent_models — объект «профиль: {provider, model, reasoning}»",
                           {"field": "agent_models", "got": type(value).__name__})
        stored, warnings = {}, []
        for profile, item in value.items():
            clean = self.validate_change(room, str(profile), item)
            if clean is not None:
                stored[str(profile)] = {**clean, "updated_at": now()}
        room["agent_models"] = stored
        return warnings

    # --- карточки ---------------------------------------------------------

    def profile_cards(self, room_id: str, profile: str) -> list:
        """Карточки комнаты, закреплённые за профилем (фикстуры + живые запросы)."""
        out = []
        for _rid, detail in self._room_details(room_id):
            for node in detail.get("tree") or []:
                if node.get("assignee") == profile:
                    out.append(node)
        return out

    def applied_counts(self, room_id: str, profile: str) -> dict:
        cards = self.profile_cards(room_id, profile)
        return {"cards_updated": sum(1 for n in cards if n.get("status") in PINNABLE),
                "pending_running": sum(1 for n in cards if n.get("status") in RUNNING)}

    def pin_labels(self, room_id: str, profile: str, label: str) -> int:
        """Имитация сторожа комнаты: подпись модели у карточек профиля, которые ещё не в работе.

        Так демо ведёт себя как боевой сервер: поменяли модель — у карточек `triage/todo/
        ready/blocked` сразу новая подпись, у `running/done` остаётся прежняя.
        """
        changed = 0
        for detail in list(self.requests.values()):
            if detail.get("room") != room_id:
                continue
            for node in detail.get("tree") or []:
                if node.get("assignee") == profile and node.get("status") in PINNABLE:
                    node["model_label"] = label
                    changed += 1
        return changed

    # --- журнал -----------------------------------------------------------

    def journal_add(self, room_id: str, text: str, detail: dict, *, kind: str = "model_changed",
                    actor: str = "mikhail") -> dict:
        self.models_seq += 1
        item = {"id": "j_m%03d" % self.models_seq, "at": now(), "kind": kind, "room": room_id,
                "request_id": None, "task_id": None, "actor": actor, "text": text, "detail": detail}
        self.models_journal.setdefault(room_id, []).insert(0, item)
        return item

    # --- действия ---------------------------------------------------------

    def put_model(self, room_id: str, profile: str, body) -> dict:
        """`PUT /api/rooms/{id}/agents/{profile}/model` (WAVE10 §19)."""
        room = self.room(room_id)
        clean = self.validate_change(room, profile, body)
        counts = self.applied_counts(room_id, profile)
        warnings = []
        if clean is None:
            room.setdefault("agent_models", {}).pop(profile, None)
            label = self.settings_label(self.agent_default(profile))
            self.journal_add(room_id, "Модель участника «%s» возвращена к настройкам агента: %s"
                             % (profile, label), {"profile": profile, "cleared": True,
                                                  "label": label, "source": "agent"})
            self.pin_labels(room_id, profile, label)
        else:
            room.setdefault("agent_models", {})[profile] = {**clean, "updated_at": now()}
            label = self.settings_label(clean)
            self.journal_add(room_id, "Модель участника «%s» изменена: %s" % (profile, label),
                             {"profile": profile, "cleared": False, "label": label, "source": "room",
                              **clean})
            self.pin_labels(room_id, profile, label)
        if counts["pending_running"]:
            warnings.append("У участника есть карточка в работе — новая модель применится после "
                            "«Перезапустить на новой модели»")
        return {"ok": True, "room": room_id, "profile": profile,
                "member": self.member_row(room, profile),
                "agent_models": public_map(room.get("agent_models")),
                "applied": {**counts, "cards_failed": []}, "warnings": warnings}

    def restart_agent(self, room_id: str, profile: str) -> dict:
        """`POST /api/rooms/{id}/agents/{profile}/restart` — имитация перезапуска карточек."""
        room = self.room(room_id)
        if profile not in roster(room):
            raise ApiError(400, "bad_request",
                           "Профиль «%s» не участвует в комнате «%s» — перезапускать нечего"
                           % (profile, room.get("title") or room.get("id")),
                           {"field": "profile", "profile": profile, "available": roster(room)})
        settings = self.effective(room, profile)
        running = [n for n in self.profile_cards(room_id, profile) if n.get("status") in RUNNING]
        label = self.settings_label(settings)
        cards = [str(n.get("id")) for n in running]
        if running:
            self.journal_add(room_id, "Перезапуск на новой модели: участник «%s», карточек %d — %s"
                             % (profile, len(running), label),
                             {"profile": profile, "restarted": len(running), "label": label},
                             kind="model_restarted")
        return {"ok": True, "room": room_id, "profile": profile, "restarted": len(running),
                "cards": cards, "model_label": label, "source": settings["source"], "warnings": []}
