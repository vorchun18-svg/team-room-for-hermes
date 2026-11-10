"""POST/PATCH/DELETE-хендлеры «Комнаты команды» — миксин для ``App`` (server/app.py).

Здесь всё, что меняет домен: комнаты, участники, создание запросов, действия над
ними, бэклог, обслуживание и adopt. Каждая запись идёт через движок канбана.
"""
from __future__ import annotations

import re
import time

try:  # пакетный и «плоский» импорт
    from . import config
    from . import answers as answers_mod
    from . import idempotency as idem_mod
    from .app_common import (ACTION_ALLOWED, ACTION_REQUIRED, ACTION_TEXT, AUTHOR_MAXIM, BACKLOG_STATUSES,
                             MAX_ATTACH, MAX_NOTE, MAX_TEXT, PATCH_FIELDS, REPORT_KEY_RE, TASK_ID_RE,
                             UNASSIGN_VALUES, derive_title, err, has_nul, make_slug, profile_ok,
                             task_id_ok, text_too_long, try_call)
    from .http_util import ApiError
    # Режим критика (W13-E §9): поля комнаты и значения — из одного источника.
    from .rooms import (CRITIC_MODES, MAX_CRITIC_ROUND_CAP, MIN_CRITIC_ROUND_CAP,
                        DEFAULT_CRITIC_ROUND_CAP)
except ImportError:  # pragma: no cover
    import config  # type: ignore
    import answers as answers_mod  # type: ignore
    import idempotency as idem_mod  # type: ignore
    from app_common import (ACTION_ALLOWED, ACTION_REQUIRED, ACTION_TEXT, AUTHOR_MAXIM,  # type: ignore
                            BACKLOG_STATUSES, MAX_ATTACH, MAX_NOTE, MAX_TEXT, PATCH_FIELDS,
                            REPORT_KEY_RE, TASK_ID_RE, UNASSIGN_VALUES, derive_title, err, has_nul,
                            make_slug, profile_ok, task_id_ok, text_too_long, try_call)
    from http_util import ApiError  # type: ignore
    from rooms import (CRITIC_MODES, MAX_CRITIC_ROUND_CAP, MIN_CRITIC_ROUND_CAP,  # type: ignore
                       DEFAULT_CRITIC_ROUND_CAP)

#: Поля комнаты сверх общего списка — только критик (W13-E §9).
CRITIC_PATCH_FIELDS = frozenset({"critic_mode", "critic_round_cap"})


class WriteRoutes:
    """Обработчики записи (``self`` — экземпляр ``server.app.App``)."""

    def h_kick(self, query, body):
        body = dict(body or {})
        # W13-F (§8): {"preview": true} — только посмотреть доску, ничего не запускать.
        # Нужно интерфейсу, чтобы понять, осмысленна ли кнопка «Проверить доску сейчас»;
        # работает и в песочнице: чтение доски безопасно, CLI не зовётся.
        if body.get("preview") is True:
            return 200, {"ok": True, "preview": True, "report": self._kick_report({"preview": True}),
                         "at": int(time.time())}, {}
        res = self._dispatch(body)
        payload = {"ok": True, "spawned": int(res.get("spawned") or 0),
                   "reclaimed": int(res.get("reclaimed") or 0),
                   "promoted": int(res.get("promoted") or 0),
                   "planned": res.get("planned") or [], "max": res.get("max"),
                   "report": res.get("report") or self._kick_report(res),
                   "dispatcher": self._dispatcher_short(), "at": int(time.time())}
        return 200, payload, {}

    # --- итог прохода (W13-F, §8): «запущено N, остальные ждут …» ---
    def _kick_report(self, res: dict) -> dict:
        """Понятный итог прохода по состоянию доски (``server/kick_report.py``).

        Только чтение; любая ошибка — пустой итог, проход из-за него не падает.
        """
        try:
            report = self.mod("kick_report").report(self.cfg, self.board, dict(res or {}))
        except Exception as exc:  # noqa: BLE001 - итог не должен ломать сам проход
            self.log.warning("kick_report: %s" % exc)
            return {}
        return report if isinstance(report, dict) else {}

    def _with_report(self, res) -> dict:
        """Добавить к результату прохода поле ``report`` (если его ещё нет)."""
        res = res if isinstance(res, dict) else {}
        if not isinstance(res.get("report"), dict) or not res.get("report"):
            res["report"] = self._kick_report(res)
        return res

    # --- диспетчер (D2) ---
    def _dispatch(self, body: dict) -> dict:
        """Проход диспетчера: песочница → 409, частота → 429, dry-run + `--max` + confirm.

        Порог карточек и список «кого запустим» считает ``engine_write``: там же
        dry-run перед реальным вызовом и отказ, если в списке есть чужая работа.
        """
        body = body if isinstance(body, dict) else {}
        if getattr(self.cfg, "sandbox", False):
            # D2: в песочнице агенты работают на реальных профилях/ключах — отказ
            raise ApiError(409, "В песочнице диспетчер не запускается: агенты работают на "
                                "реальных профилях и ключах. Запуск — только на боевом Hermes.",
                           code="sandbox_no_dispatch",
                           detail={"reason": "sandbox_no_dispatch"})
        mod = self.mod("engine_write")
        board = self.board
        guard = getattr(mod, "dispatch_guard", None)
        if callable(guard):
            reason = guard(self.cfg, board)
            if reason:
                raise ApiError(int(reason.get("status") or 409),
                               str(reason.get("message") or "Диспетчер недоступен"),
                               code=reason.get("code") or None,
                               detail=reason.get("detail") or {})
        max_cards = body.get("max")
        confirm = body.get("confirm") is True
        runner = getattr(mod, "dispatch_board", None)
        if not callable(runner):
            with self.engine():
                res = self.call("engine_write", "act",
                                ((self.cfg, board, None, "dispatch"),
                                 {"max": max_cards, "confirm": confirm}))
            return self._with_report(res)
        try:
            res = runner(self.cfg, board, max_cards=max_cards, confirm=confirm)
        except ApiError:
            raise
        except Exception as exc:  # noqa: BLE001 - доменная ошибка движка
            raise self._engine_failure(exc) from exc
        return self._with_report(res)

    def _engine_failure(self, exc: Exception) -> ApiError:
        """EngineWriteError → ApiError с верным статусом (409/429/400/404/502)."""
        status = getattr(exc, "http_status", None)
        code = getattr(exc, "code", None)
        detail = getattr(exc, "detail", None)
        message = (getattr(exc, "message_ru", None) or getattr(exc, "message", None)
                   or str(exc) or "Ошибка движка канбана")
        if not status:
            status = {"bad_request": 400, "forbidden": 403, "not_found": 404,
                      "conflict": 409}.get(code, 500)
            code = code if status != 500 else "engine_error"
        if status >= 500:
            self.log.error("движок: %s" % message, exc)
        return ApiError(int(status), str(message), code=code,
                        detail=detail if isinstance(detail, dict) else {})

    # --- комнаты ---
    def h_room_check_dir(self, query, body):
        """POST /api/rooms/check-dir {path} — «можно ли положить сюда папку проекта».

        Ничего не создаёт (GET-подобная проверка): права смотрим у ближайшего
        существующего родителя. Ответ — `check_dir` из `server/project_dir.py`.
        """
        path = body.get("path") if isinstance(body, dict) else None
        if path is not None and not isinstance(path, str):
            raise err(400, "path: нужен путь строкой", field="path")
        text = (path or "").strip()
        if len(text) > 400:
            raise err(400, "Путь слишком длинный — до 400 знаков", field="path")
        if has_nul(text):
            raise err(400, "В пути есть непечатаемый символ", field="path")
        with self.engine():
            result = self.call("project_dir", "check_dir", ((self.cfg, text), {}),
                               ((self.cfg,), {"path": text}))
        if not isinstance(result, dict):
            raise ApiError(502, "Не удалось проверить папку", detail={"path": text})
        return 200, result, {}

    def h_room_create(self, query, body):
        with self.engine():
            room = self.call("rooms", "create_room", ((self.cfg, dict(body)), {}))
        if not isinstance(room, dict) or not room.get("id"):
            raise ApiError(502, "Комната создана, но ответ пуст", detail={})
        return 201, self._room_detail(room), {"Location": "/api/rooms/" + str(room["id"])}

    def h_room_patch(self, query, body, room_id):
        self._room(room_id)
        bad = sorted(set(body) - PATCH_FIELDS - CRITIC_PATCH_FIELDS)
        if bad:
            raise err(400, "Поля нельзя менять", fields=bad,
                      allowed=sorted(PATCH_FIELDS | CRITIC_PATCH_FIELDS))
        # Режим критика (W13-E §9): три варианта в интерфейсе, два значения в API.
        if "critic_mode" in body and body.get("critic_mode") not in CRITIC_MODES:
            raise err(400, "Режим критика: «rounds» или «until_approved»",
                      field="critic_mode", value=body.get("critic_mode"),
                      allowed=list(CRITIC_MODES))
        if "critic_round_cap" in body:
            cap = body.get("critic_round_cap")
            if isinstance(cap, bool) or not isinstance(cap, int) \
                    or not MIN_CRITIC_ROUND_CAP <= cap <= MAX_CRITIC_ROUND_CAP:
                raise err(400, "Потолок раундов: целое от %d до %d"
                          % (MIN_CRITIC_ROUND_CAP, MAX_CRITIC_ROUND_CAP),
                          field="critic_round_cap", value=cap)
        if body.get("mode_default") not in (None, "auto"):
            raise err(400, "Режим swarm не поддерживается — только auto", field="mode_default")
        with self.engine():
            updated = self.call("rooms", "update_room", ((self.cfg, room_id, dict(body)), {}))
        if not isinstance(updated, dict):
            updated = self._room(room_id)
        return 200, self._room_detail(updated), {}

    def h_room_archive(self, query, body, room_id):
        room = self._room(room_id)
        before = bool(room.get("archived"))
        with self.engine():
            res = self.call("rooms", "archive_room", ((self.cfg, room_id), {}))
        changed = res.get("changed") if isinstance(res, dict) else None
        if changed is None:
            changed = not before
        fresh = res.get("room") if isinstance(res, dict) and isinstance(res.get("room"), dict) else None
        if fresh is None:
            fresh = self._room(room_id)
        return 200, {"ok": True, "changed": bool(changed), "room": self._room_summary(fresh)}, {}

    def h_member_add(self, query, body, room_id):
        room = self._room(room_id)
        profile = str(body.get("profile") or "").strip()
        if not profile:
            raise err(400, "Нужен profile", field="profile")
        if body.get("role") not in (None, "worker", "critic", "observer"):
            raise err(400, "role: worker|critic|observer", field="role")
        roster = self._roster_names()
        if roster and profile not in roster:
            raise err(400, "Профиля нет в ростере", profile=profile, available=sorted(roster))
        if any((m or {}).get("profile") == profile for m in room.get("members") or []) \
                or profile in (room.get("orchestrator"), room.get("critic")):
            raise err(409, "Профиль уже в комнате", profile=profile)
        with self.engine():
            res = self.call("rooms", "add_member", ((self.cfg, room_id, dict(body)), {}))
        if isinstance(res, dict) and "members" in res:
            payload = {"member": res.get("member"), "members": res.get("members") or []}
        else:
            fresh = self._room(room_id)
            payload = {"member": res if isinstance(res, dict) else {"profile": profile},
                       "members": fresh.get("members") or []}
        return 201, payload, {}

    def h_member_remove(self, query, body, room_id, profile):
        room = self._room(room_id)
        if profile in (room.get("orchestrator"), room.get("critic")):
            raise err(409, "Это директор или критик комнаты — сначала смените его",
                      reason="room_lead", profile=profile)
        with self.engine():
            res = self.call("rooms", "remove_member", ((self.cfg, room_id, profile), {}))
        members = res.get("members") if isinstance(res, dict) else None
        if members is None:
            members = self._room(room_id).get("members") or []
        return 200, {"ok": True, "members": members}, {}

    def h_agent_label_put(self, query, body, room_id, profile):
        """PUT /api/rooms/{id}/agents/{profile}/label — подпись участника (WAVE13 §11).

        Тело: `{label}` — «Анна — финансы и экономика (CFO)» или пустая строка
        (вернуться к описанию профиля Hermes). Меняется только реестр комнаты:
        `profile.yaml`, `SOUL.md` и прочие файлы профиля не трогаются.
        """
        room = self._room(room_id)
        body = body if isinstance(body, dict) else {}
        label = body.get("label")
        if label is not None and not isinstance(label, str):
            raise err(400, "Подпись: нужен текст строкой", field="label")
        if has_nul(label or ""):
            raise err(400, "В подписи есть непечатаемый символ", field="label")
        profile = str(profile or "").strip()
        in_room = profile in [str((m or {}).get("profile") or "") for m in room.get("members") or []]
        if not in_room and profile not in (room.get("orchestrator"), room.get("critic")):
            raise err(404, "Участника нет в комнате", profile=profile, room=room_id)
        with self.engine():
            res = self.call("rooms", "set_member_label", ((self.cfg, room_id, profile, label or ""), {}))
        if not isinstance(res, dict):
            raise ApiError(502, "Подпись не сохранена — приложение не ответило",
                           detail={"room": room_id, "profile": profile})
        members = res.get("members")
        if members is None:
            members = self._room(room_id).get("members") or []
        # B-17: подписи руководителя и критика — отдельная карта реестра комнаты.
        role_labels = res.get("role_labels")
        if role_labels is None:
            role_labels = self._room(room_id).get("role_labels") or {}
        return 200, {"ok": True, "profile": res.get("profile") or profile,
                     "label": res.get("label") or "", "members": members,
                     "role_labels": role_labels}, {}

    def h_agent_notes_put(self, query, body, room_id, profile):
        """PUT /api/rooms/{id}/agents/{profile}/notes — особые указания участнику (WAVE9 §9).

        Тело: `{text}`. Профиль обязан быть в составе комнаты (иначе 400 по-русски),
        текст ≤ 8 000 знаков, пустой текст — очистка. Файлы профиля (SOUL.md,
        config.yaml, skills) НЕ меняются: указания живут только в реестре комнаты.
        Секреты в тексте — предупреждение (маска), не блокировка.
        """
        self._room(room_id)
        body = body if isinstance(body, dict) else {}
        text = body.get("text")
        if text is not None and not isinstance(text, str):
            raise err(400, "Указания: нужен текст строкой", field="text")
        if has_nul(text or ""):
            raise err(400, "В указаниях есть непечатаемый символ", field="text")
        if len(text or "") > 8000:
            raise err(400, "Указания: %d знаков, а можно не больше 8000. Сократите текст."
                      % len(text or ""), field="text", length=len(text or ""), limit=8000)
        with self.engine():
            res = self.call("agent_notes", "set",
                            ((self.cfg, room_id, profile, text or ""), {}))
        if not isinstance(res, dict):
            raise ApiError(502, "Указания не сохранены — приложение не ответило",
                           detail={"room": room_id, "profile": profile})
        payload = {"ok": True, "profile": res.get("profile") or profile,
                   "text": res.get("text") or "", "cleared": bool(res.get("cleared")),
                   "agent_notes": res.get("agent_notes") or {},
                   "warnings": res.get("warnings") or []}
        return 200, payload, {}

    # --- запросы ---
    def h_request_create(self, query, body, room_id):
        room = self._room(room_id)
        if room.get("archived"):
            raise err(409, "Комната в архиве — новые запросы недоступны", room=room_id)
        text = body.get("text")
        if not isinstance(text, str) or not text.strip():
            raise err(400, "Текст запроса пуст", field="text")
        if has_nul(text):
            raise err(400, "В тексте есть NUL-байт", field="text")
        if len(text) > MAX_TEXT:
            raise err(400, text_too_long("Текст запроса", len(text), MAX_TEXT),
                      field="text", length=len(text), limit=MAX_TEXT)
        mode = body.get("mode") or room.get("mode_default") or "auto"
        if mode != "auto":
            raise err(400, "Режим swarm не поддерживается — только auto", field="mode", value=mode)
        critic = body.get("critic")
        critic = bool(room.get("critic_pass")) if critic is None else bool(critic)
        attach = body.get("attach") or []
        if not isinstance(attach, list):
            raise err(400, "attach: список путей", field="attach")
        if len(attach) > MAX_ATTACH:
            raise err(400, "attach: не больше %d путей" % MAX_ATTACH, field="attach",
                      count=len(attach))
        for path in attach:
            if not isinstance(path, str) or not config.path_allowed(self.cfg, path):
                raise err(403, "Вложение вне разрешённых путей", path=str(path))
        title = body.get("title")
        user_title = False
        if title is not None:
            if not isinstance(title, str) or not title.strip():
                raise err(400, "title пуст", field="title")
            title = title.strip()
            if has_nul(title):
                raise err(400, "В заголовке есть NUL-байт", field="title")
            if len(title) > 120:
                raise err(400, "title длиннее 120 символов", field="title", length=len(title))
            user_title = True          # W17-B §4: имя задал человек — не перезаписываем
        else:
            title = derive_title(text)
        brief = body.get("brief", False)
        if not isinstance(brief, bool):
            raise err(400, "brief: ожидается да/нет", field="brief", value=brief)
        # W13-E §9: третий вариант раундов выбирается и при постановке запроса.
        critic_mode = body.get("critic_mode")
        if critic_mode is not None:
            if not isinstance(critic_mode, str) or critic_mode not in CRITIC_MODES:
                raise err(400, "Режим критика: «rounds» или «until_approved»",
                          field="critic_mode", value=critic_mode, allowed=list(CRITIC_MODES))
            if not critic:
                raise err(400, "Режим критика без критика: сначала включите проверку",
                          field="critic_mode", value=critic_mode)
        req_rounds = body.get("max_rounds")
        if req_rounds is not None:
            if isinstance(req_rounds, bool) or not isinstance(req_rounds, int) \
                    or not 1 <= req_rounds <= 5:
                raise err(400, "max_rounds: целое от 1 до 5", field="max_rounds",
                          value=req_rounds)
        return self._create(room, text, title, critic, attach, mode="auto", brief=brief,
                            critic_mode=critic_mode, max_rounds=req_rounds,
                            user_title=user_title)

    def h_request_actions(self, query, body, rid):
        action = body.get("action")
        if action not in ACTION_ALLOWED:
            raise err(400, "Неизвестное действие", action=action, allowed=list(ACTION_ALLOWED))
        room, rec = self._find_record(rid)
        kw: dict = {}
        for field in ("task_id", "question_id", "profile", "step_id", "decision_id"):
            if field in body and body.get(field) is not None:
                value = body.get(field)
                if not isinstance(value, str) or not value.strip():
                    raise err(400, "Поле %s должно быть непустой строкой" % field, field=field)
        for field in ("task_id", "question_id", "profile", "step_id", "decision_id", "title", "author"):
            value = body.get(field)
            if isinstance(value, str) and value.strip():
                kw[field] = value.strip()
        missing = [field for field in ACTION_REQUIRED.get(action, ()) if field not in kw]
        if missing:
            raise err(400, "Нужны поля: " + ", ".join(missing), missing=missing)
        if action in ACTION_TEXT:
            text = body.get("text")
            if not isinstance(text, str) or not text.strip():
                raise err(400, "Нужен текст", field="text")
            if len(text) > MAX_NOTE:
                raise err(400, text_too_long("Текст", len(text), MAX_NOTE),
                          field="text", length=len(text), limit=MAX_NOTE)
            if has_nul(text):
                raise err(400, "В тексте есть NUL-байт", field="text")
            kw["text"] = text
        if action == "revise" and "task_id" not in kw:
            kw.pop("task_id", None)
        self._check_action_values(room, rec, rid, action, kw)
        # B-25: ключ идемпотентности — повторная доставка того же нажатия «Повторить»
        # не запускает действие второй раз, а получает сохранённый ответ (`duplicate: true`).
        retry_slot = ""
        if action == "retry" and body.get("idempotency_key") is not None:
            try:
                idem_key = idem_mod.ensure_key(body.get("idempotency_key"))
            except idem_mod.IdempotencyError as exc:
                raise err(400, exc.message, field="idempotency_key",
                          value=str(body.get("idempotency_key"))[:120]) from exc
            retry_slot = "%s:%s:%s" % (rid, kw.get("task_id") or "", idem_key)
            replayed = idem_mod.GUARD.replay(retry_slot, scope="retry")
            if replayed is not None:
                replayed["duplicate"] = True
                return 200, replayed, {}
        if action == "clone":
            return self._clone(room, rec, kw.get("title"))
        if action in ("critique", "revise"):
            return self._critic_action(room, rec, rid, action, kw)
        if action == "dispatch":
            res = self._dispatch(kw)
            payload = {"ok": True, "action": "dispatch",
                       "spawned": int(res.get("spawned") or 0),
                       "promoted": int(res.get("promoted") or 0),
                       "reclaimed": int(res.get("reclaimed") or 0),
                       "planned": res.get("planned") or [], "max": res.get("max"),
                       # W13-F (§8): итог прохода простыми словами (панель в интерфейсе)
                       "report": res.get("report") or self._kick_report(res),
                       "dispatcher": self._dispatcher_short()}
            payload["request"] = self._summary_or_local(
                room, rid, rec.get("text") or "", rec.get("title") or "",
                bool(rec.get("critic")), rec.get("mode") or "auto", rec.get("answers"))
            return 200, payload, {}
        if action == "decompose":
            status = self._request_status(room, rid, rec)
            if status not in ("queued", None):
                raise ApiError(409, "Запрос уже разложен или не ждёт декомпозиции", code="conflict",
                               detail={"reason": "already_decomposed", "status": status,
                                       "rid": str(rid)})
        if action == "answer_remind":          # §2: «Напомнить руководителю»
            return self._remind_lead(room, rec, rid, kw)
        if action == "answers_resend":         # W17-B §3: накопленные ответы — только по клику
            return self._answers_resend(room, rec, rid)
        if action in ("step_agree", "step_comment", "step_move", "step_drop"):
            # §12: действия пользователя по шагу — «Согласен», «Прокомментировать»,
            # «Переместить», «Не согласен — убрать» (общий механизм ответа, §5/§6).
            return self._step_action(room, rec, rid, kw, action[len("step_"):], body)
        if action == "decision":
            # W19-D2 §1 (B-20): действие над решением «предложено».
            return self._decision_action(room, rec, rid, kw, body)
        question = None
        need_explanation = body.get("need_explanation")
        if need_explanation is not None and not isinstance(need_explanation, bool):
            raise err(400, "need_explanation: true или false", field="need_explanation")
        before: dict = {}
        steps_before: list = []
        user_text = kw.get("text") or ""
        if action == "answer":
            before = self._detail(room, rid, rec.get("extra_cards"), rec.get("critic"),
                                  rec.get("answers"))
            question = next((q for q in before.get("open_questions") or []
                             if str(q.get("id")) == str(kw.get("question_id"))), None)
            if question is None:
                raise err(404, "Вопрос не найден", question_id=kw.get("question_id"))
            if question.get("answered"):
                raise err(409, "Вопрос уже отвечен", question_id=kw.get("question_id"))
            # §3 / W17-B §2: встречный вопрос — ТОЛЬКО явная кнопка. Эвристика по
            # тексту больше не решает: она ошибалась на старых записях (B5). Текст,
            # похожий на просьбу пояснить, лишь подсказка для журнала.
            need_explanation = bool(need_explanation)
            if not need_explanation and answers_mod.explanation_hint({"text": user_text}):
                self._answer_hint_journal(room.get("id"), kw.get("question_id"))
            # §1: ответ уходит в ветку с меткой, по которой видно, НА КАКОЙ вопрос отвечено
            kw["text"] = (answers_mod.explanation_comment(kw.get("question_id"), question.get("text"), user_text)
                          if need_explanation else
                          answers_mod.answer_comment(kw.get("question_id"), question.get("text"), user_text))
            steps_before = [s for s in (before.get("next_steps") or [])
                            if isinstance(s, dict) and s.get("id")]
        with self.engine():
            res = self.call("engine_write", "act",
                            ((self.cfg, room.get("board") or self.board, rid, action), dict(kw)))
        res = res if isinstance(res, dict) else {}
        payload = {"ok": True, "action": action}
        payload.update(res)
        answers = rec.get("answers") if isinstance(rec.get("answers"), dict) else None
        if action == "answer":
            # ответ храним в реестре: metadata карточки движок переписывать не даёт
            at = int(time.time())
            qid = str(kw.get("question_id"))
            record = {"text": user_text, "at": at,
                      "answered": not need_explanation,
                      "status": "waiting_explanation" if need_explanation else "sent",
                      "question_text": str(question.get("text") or ""),
                      "steps_at_answer": [str(s.get("id")) for s in steps_before],
                      "unblocked": list(res.get("unblocked") or [])}
            updated = dict(answers or {})
            updated[qid] = record
            answers = updated
            self._save_answers(room, rid, answers)
            # §2: руководитель получает ответ рабочей карточкой с контрактом реакции
            note = {}
            try:
                note = self.call("thread", "post_answer_note",
                                 ((self.cfg, room, rid, dict(question), user_text),
                                  {"need_explanation": bool(need_explanation)}),
                                 ((self.cfg, room, rid, dict(question), user_text), {})) or {}
            except ApiError as exc:
                self.log.warning("карточка-ответ %s: %s" % (rid, exc.message))
            # §4: ответ «нет/не нужно» снимает связанные шаги («снят по вашему ответу»)
            cancelled = [] if need_explanation else self._cancel_steps(room, rid, before,
                                                                       question, user_text)
            if cancelled:
                record["cancelled_steps"] = cancelled
                updated[qid] = record
                self._save_answers(room, rid, updated)
            payload["question"] = answers_mod.apply_question(dict(question or {}), record)
            payload["answer"] = {"question_id": qid, "text": user_text, "at": at,
                                 "state": record["status"],
                                 "label": answers_mod.STATE_RU[record["status"]],
                                 "needs_explanation": bool(need_explanation)}
            payload["steps"] = {"cancelled": cancelled,
                                "cancelled_label": answers_mod.CANCELLED_RU}
            for key in ("task_id", "merged", "routed_to"):
                if note.get(key) is not None:
                    payload.setdefault("note", {})[key] = note[key]
            payload["unblocked"] = list(res.get("unblocked") or [])
            # W17-B §4: существенное уточнение — пересчитать название (в фоне, с паузой).
            try:
                self.mod("titles").maybe_refresh(self.cfg, room, dict(rec, answers=answers),
                                                 before, answers)
            except Exception as exc:  # noqa: BLE001 - название вторично
                self.log.warning("название после ответа %s: %s" % (rid, exc))
        payload["request"] = self._summary_or_local(
            room, rid, rec.get("text") or "", rec.get("title") or "",
            bool(rec.get("critic")), rec.get("mode") or "auto", answers)
        if action == "comment":
            task_id = res.get("task_id") or rid
            payload["comment"] = self._last_comment(room, task_id) or {
                "id": 0, "author": kw.get("author") or "mikhail", "text": kw.get("text") or "",
                "at": int(time.time())}
        elif action == "reassign":
            payload["task"] = self._task_node(room, rid, rec, res.get("task_id") or kw.get("task_id"))
        if retry_slot:
            # Запомнили ответ: повторное нажатие получит его же и ничего не запустит.
            idem_mod.GUARD.remember(retry_slot, payload, scope="retry")
        return 200, payload, {}

    # --- диалог по запросу (Блок A + Д1) ---
    def h_request_messages(self, query, body, rid):
        """Сообщение Михаила → руководителю комнаты (комментарий + карточка «Уточнение»).

        Рассылки на дочерние карточки нет: руководитель сам решает, кому передать
        (Д1). Ответ 201: ``{message, thread, routed_to, merged}``; поле ``thread`` —
        та же лента, что в ``RequestDetail``.
        """
        room, rec = self._find_record(rid)
        if room.get("archived"):
            raise err(409, "Комната в архиве — сообщения по запросу недоступны", room=room.get("id"))
        text = body.get("text")
        if not isinstance(text, str) or not text.strip():
            raise err(400, "Пустое сообщение — напишите текст", field="text")
        if has_nul(text):
            raise err(400, "В тексте есть NUL-байт", field="text")
        if len(text) > MAX_TEXT:
            raise err(400, text_too_long("Сообщение", len(text), MAX_TEXT),
                      field="text", length=len(text), limit=MAX_TEXT)
        attach = body.get("attach") or []
        if not isinstance(attach, list):
            raise err(400, "attach: список путей к файлам", field="attach")
        if len(attach) > MAX_ATTACH:
            raise err(400, "attach: не больше %d путей" % MAX_ATTACH, field="attach",
                      count=len(attach))
        for path in attach:
            if not isinstance(path, str) or not path.strip():
                raise err(400, "attach: путь должен быть непустой строкой", field="attach")
            if has_nul(path):
                raise err(400, "В пути вложения есть NUL-байт", field="attach")
            if not config.path_allowed(self.cfg, path):
                raise err(403, "Вложение вне разрешённых путей", path=str(path))
        urgent = body.get("urgent")
        if urgent is not None and not isinstance(urgent, bool):
            raise err(400, "urgent: true или false", field="urgent")
        with self.engine():
            res = self.call("thread", "post_message",
                            ((self.cfg, room, rid, text), {"attach": attach, "urgent": bool(urgent)}),
                            ((self.cfg, room.get("id"), rid, text), {"attach": attach}))
        if not isinstance(res, dict) or not res.get("message"):
            raise ApiError(502, "Сообщение записано, но ответ пуст — обновите страницу",
                           detail={"rid": rid})
        payload = {"message": res["message"], "thread": res.get("thread") or [],
                   "routed_to": res.get("routed_to"), "merged": bool(res.get("merged"))}
        for key in ("task_id", "status", "revision", "retry"):
            if res.get(key) is not None:
                payload[key] = res[key]
        return 201, payload, {"Location": "/api/requests/" + str(rid),
                              "Cache-Control": "no-cache"}

    # --- название запроса: переименование и приоритет источников (W17-B §4) ---

    def _title_view(self, summary, rec):
        """Наложить название из реестра на сводку (``derived`` < ``llm`` < ``leader`` < ``user``)."""
        try:
            return self.mod("titles").overlay(summary, rec)
        except Exception as exc:  # noqa: BLE001 - название вторично
            self.log.warning("название сводки: %s" % exc)
            return summary

    def _title_fields(self, text: str, title: str, user: bool = False) -> dict:
        """Поля названия для новой записи реестра (``derived`` сразу, без моста)."""
        try:
            return self.mod("titles").seed(text, title, user=bool(user))
        except Exception as exc:  # noqa: BLE001 - запись обязана создаться
            self.log.warning("название нового запроса: %s" % exc)
            return {"title_short": (title or text or "")[:60], "title_source": "derived",
                    "title_locked": False}

    def h_request_patch(self, query, body, rid):
        """`PATCH /api/requests/{id}` — переименование названия пользователем.

        Тело: ``{title}``. Непустое значение → ``title_source="user"``,
        ``title_locked=true`` (автоматика и модель его больше не трогают).
        Пустое значение снимает блокировку и возвращает автоматическое название.
        Полный текст запроса не меняется — переименовывается только название.
        """
        room, rec = self._find_record(rid)
        if "title" not in body:
            raise err(400, "Нужно поле title (пустая строка снимает блокировку)", field="title")
        title = body.get("title")
        if title is None:
            title = ""
        if not isinstance(title, str):
            raise err(400, "title: ожидается строка", field="title")
        if has_nul(title):
            raise err(400, "В названии есть NUL-байт", field="title")
        flat = " ".join(title.split())
        if len(flat) > 120:
            raise err(400, "title длиннее 120 символов", field="title", length=len(flat))
        try:
            updated = self.mod("titles").rename(self.cfg, room.get("id"), rid, flat)
        except ApiError:
            raise
        except Exception as exc:  # noqa: BLE001 - реестр недоступен
            raise ApiError(503, "Не удалось сохранить название — попробуйте ещё раз",
                           detail={"rid": rid, "error": str(exc)[:200]}) from exc
        if not isinstance(updated, dict):
            raise err(404, "Запрос не найден", rid=rid)
        summary = self._summary_or_local(
            room, rid, rec.get("text") or "", rec.get("title") or "",
            bool(rec.get("critic")), rec.get("mode") or "auto", updated.get("answers"))
        self._title_view(summary, updated)
        payload = {"ok": True, "action": "rename", "request": summary,
                   "title_short": summary.get("title_short"),
                   "title_source": summary.get("title_source"),
                   "title_locked": summary.get("title_locked")}
        return 200, payload, {"Cache-Control": "no-store"}

    # --- ответы на вопросы: реестр, напоминание, шаги (W14-D §2, §4) ----------

    def _save_answers(self, room: dict, rid: str, answers: dict) -> None:
        """Записать реестр ответов (ошибка реестра не отменяет уже записанный ответ)."""
        try:
            self.call("registry", "update", ((self.cfg, room.get("id"), rid,
                                              {"answers": answers}), {}))
        except Exception as exc:  # noqa: BLE001 - комментарий уже записан
            self.log.error("реестр: ответ на вопрос %s не сохранён" % rid, exc)

    def _backlog_id(self, rid: str, step_id: str) -> str:
        """`ns_1` → `<rid>:ns_1` (id шага в бэклоге комнаты)."""
        text = str(step_id or "")
        return text if ":" in text else "%s:%s" % (rid, text)

    # --- накопленные ответы (W17-B §3, аудит B6) ------------------------------

    def _answers_resend(self, room: dict, rec: dict, rid: str):
        """«Отправить руководителю накопленные ответы» — одно действие по клику.

        Собирает ответы реестра, у которых ЕСТЬ текст, нет просьбы пояснить и нет
        реакции руководителя (`sent`), в порядке вопросов ответа, и передаёт их
        одной карточкой (``thread.post_answers_digest``). Автоматически такие
        ответы не отправляются никогда: это запускает настоящего агента.
        """
        stored = rec.get("answers") if isinstance(rec.get("answers"), dict) else {}
        if not stored:
            raise ApiError(409, "Нечего отправлять: ответов на вопросы нет.",
                           code="conflict", detail={"reason": "nothing_to_resend", "pending": 0})
        detail = self._detail(room, rid, rec.get("extra_cards"), rec.get("critic"), stored)
        questions = [q for q in (detail.get("open_questions") or []) if isinstance(q, dict)]
        try:
            thread_items = self.call("thread", "get_thread",
                                     ((self.cfg, rid), {"room": room})) or []
        except Exception:  # noqa: BLE001 - без ленты реакций просто нет
            thread_items = []
        reactions = answers_mod.reactions_from_thread(thread_items, questions, stored,
                                                      lead=room.get("orchestrator"))
        order = {str(q.get("id")): index for index, q in enumerate(questions)}
        pending: list = []
        for qid, record in stored.items():
            if not isinstance(record, dict):
                continue
            text = answers_mod.norm(record.get("text"))
            if not text:
                continue
            state = answers_mod.state_of(record, reactions.get(str(qid)))
            if state != "sent":
                continue        # уже отреагировали / просьба пояснить / пустой ответ
            question_text = str(record.get("question_text") or "")
            if not question_text:
                found = next((q for q in questions if str(q.get("id")) == str(qid)), None)
                question_text = str((found or {}).get("text") or "")
            pending.append({"question_id": str(qid), "question_text": question_text,
                            "answer": text, "at": int(record.get("at") or 0),
                            "order": order.get(str(qid), len(order) + len(pending))})
        pending.sort(key=lambda item: (item["order"], item["at"]))
        if not pending:
            raise ApiError(409, "Нечего отправлять: на все ответы руководитель уже отреагировал.",
                           code="conflict",
                           detail={"reason": "nothing_to_resend", "pending": 0,
                                   "answers": len(stored)})
        with self.engine():
            res = self.call("thread", "post_answers_digest",
                            ((self.cfg, room, rid, [dict(item) for item in pending]), {}))
        res = res if isinstance(res, dict) else {}
        now = int(time.time())
        updated = {key: dict(value) if isinstance(value, dict) else value
                   for key, value in stored.items()}
        for item in pending:
            record = updated.get(item["question_id"]) or {}
            record["resent_at"] = now
            record["resend_count"] = int(record.get("resend_count") or 0) + 1
            updated[item["question_id"]] = record
        self._save_answers(room, rid, updated)
        payload = {"ok": True, "action": "answers_resend", "sent": len(pending),
                   "question_ids": [item["question_id"] for item in pending],
                   "task_id": res.get("task_id"), "at": res.get("at") or now,
                   "note": {"task_id": res.get("task_id"), "merged": res.get("merged"),
                            "routed_to": res.get("routed_to")}}
        payload["request"] = self._summary_or_local(
            room, rid, rec.get("text") or "", rec.get("title") or "",
            bool(rec.get("critic")), rec.get("mode") or "auto", updated)
        return 200, payload, {}

    def _answer_hint_journal(self, room_id, question_id) -> None:
        """Запись в журнал: текст похож на просьбу пояснить, но это ответ (B5).

        Только подсказка — статус ответа не меняется. Журнал вторичен: ошибка
        записи не мешает ответу.
        """
        if not room_id:
            return
        try:
            try:
                from .routes_read import _journal_once
            except ImportError:  # pragma: no cover
                from routes_read import _journal_once  # type: ignore
            _journal_once(self.cfg, room_id, "answer_hint",
                          "Ответ на вопрос %s похож на просьбу пояснить, но записан как ОТВЕТ "
                          "(решает только кнопка «Мне нужно пояснение»)." % question_id)
        except Exception as exc:  # noqa: BLE001 - журнал вторичен
            self.log.warning("журнал подсказки ответа: %s" % exc)

    def _cancel_steps(self, room: dict, rid: str, detail: dict, question: dict, text: str) -> list:
        """§4: ответ «нет/не нужно» снимает связанные шаги («снят по вашему ответу»)."""
        try:
            ids = answers_mod.steps_to_cancel((detail or {}).get("next_steps") or [], question, text)
        except Exception as exc:  # noqa: BLE001 - связка не должна ломать ответ
            self.log.warning("связка шагов %s: %s" % (rid, exc))
            return []
        out: list = []
        for step_id in ids:
            item_id = self._backlog_id(rid, step_id)
            try:
                if not self._has_mod("ingest"):
                    break
                ingest = self.mod("ingest")
                item = ingest.ensure_backlog_item(self.cfg, room.get("id"), item_id, detail)
                if item is None:
                    continue
                ingest.set_backlog_status(self.cfg, room.get("id"), item_id, "dismissed")
                out.append(step_id)
            except Exception as exc:  # noqa: BLE001 - шаг вторичен, ответ важнее
                self.log.warning("шаг %s не снят: %s" % (item_id, exc))
        return out

    def _remind_lead(self, room: dict, rec: dict, rid: str, kw: dict):
        """«Напомнить руководителю»: сигнал в ветке + отметка в реестре.

        W17-B §8 (аудит B-12): реакция ищется ТОЙ ЖЕ функцией и по ТОМУ ЖЕ списку
        вопросов, что окно вопроса (`answers.reaction_lookup`). Если реакция есть —
        409 всегда вместе с ответом и найденной реакцией: пользователь обязан
        увидеть, что именно руководитель ответил, а не сухое «уже ответил».
        """
        qid = str(kw.get("question_id") or "")
        answers = rec.get("answers") if isinstance(rec.get("answers"), dict) else {}
        record = answers.get(qid)
        if not isinstance(record, dict) or not str(record.get("text") or "").strip():
            raise err(404, "Ответ на этот вопрос не найден", question_id=qid)
        detail = self._detail(room, rid, rec.get("extra_cards"), rec.get("critic"), answers)
        questions = [q for q in (detail.get("open_questions") or []) if isinstance(q, dict)]
        question = next((q for q in questions if str(q.get("id")) == qid), None)
        if question is None:
            raise err(404, "Вопрос не найден", question_id=qid)
        # Один поиск реакции: та же функция, тот же набор вопросов, что в окне.
        try:
            thread_items = self.call("thread", "get_thread", ((self.cfg, rid), {"room": room})) or []
        except Exception:  # noqa: BLE001 - лента не прочиталась, реакции просто нет
            thread_items = detail.get("thread") or []
        record, reaction = answers_mod.reaction_lookup(question, questions, answers,
                                                       thread_items, lead=room.get("orchestrator"))
        if not isinstance(record, dict):
            record = answers.get(qid) if isinstance(answers.get(qid), dict) else {}
        applied = answers_mod.apply_question(dict(question), record, reaction)
        if isinstance(reaction, dict) and reaction.get("status") in ("received", "applied", "no_change"):
            # Ответ показываем ВМЕСТЕ с «уже ответил» — иначе окно пустое (B-12).
            raise ApiError(409, "Руководитель уже ответил на этот вопрос", code="conflict",
                           detail={"reason": "already_answered", "question_id": qid,
                                   "answer": {"question_id": qid,
                                              "text": record.get("text") or applied.get("answer"),
                                              "at": record.get("at") or applied.get("answered_at"),
                                              "state": applied.get("answer_state"),
                                              "label": applied.get("answer_label")},
                                   "reaction": dict(reaction),
                                   "reaction_label": reaction.get("label") or applied.get("answer_label"),
                                   "question": applied})
        ago = 0
        if record.get("at"):
            ago = max(0, int((int(time.time()) - int(record["at"])) // 60))
        with self.engine():
            res = self.call("thread", "remind_lead",
                            ((self.cfg, room, rid, dict(question)), {"ago_min": ago}),
                            ((self.cfg, room, rid, dict(question)), {}))
        res = res if isinstance(res, dict) else {}
        record = dict(record, reminded_at=int(time.time()),
                      reminders=int(record.get("reminders") or 0) + 1)
        answers[qid] = record
        self._save_answers(room, rid, answers)
        payload = {"ok": True, "action": "answer_remind", "question_id": qid,
                   "reminded_at": record["reminded_at"], "reminders": record["reminders"],
                   "text": res.get("text"), "routed_to": res.get("routed_to"),
                   "reaction": None,
                   "question": answers_mod.apply_question(dict(question), record, None)}
        payload["request"] = self._summary_or_local(
            room, rid, rec.get("text") or "", rec.get("title") or "",
            bool(rec.get("critic")), rec.get("mode") or "auto", answers)
        return 200, payload, {}

    def _step_drop(self, room: dict, rec: dict, rid: str, kw: dict):
        """Обратная совместимость: «Убрать» шаг — это действие `step_drop`."""
        return self._step_action(room, rec, rid, kw, "drop", {})

    def _step_action(self, room: dict, rec: dict, rid: str, kw: dict, kind: str, body: dict):
        """Действие пользователя по шагу: согласен / прокомментировать / переместить / убрать.

        Пункт 12: статус шага ведёт руководитель, а пользователь может ВЛИЯТЬ на
        план — каждое действие уходит руководителю КАК ОТВЕТ НА ЭТОТ ШАГ (тот же
        механизм, что у вопросов, §5/§6): метка ``[Михаил · … шаг ns_x «…»]`` в
        ветке, запись в реестре, видимая реакция «принято / учтено / срок изменён /
        шаг убран». «Убрать» дополнительно снимает шаг в бэклоге комнаты.
        """
        body = body if isinstance(body, dict) else {}
        step_id = str(kw.get("step_id") or "").strip()
        if not re.fullmatch(r"[A-Za-z0-9_:\-]{1,64}", step_id):
            raise err(400, "Некорректный id шага", field="step_id", value=step_id[:80])
        # `<rid>:ns_1` → `ns_1` (id шага в бэклоге комнаты)
        if ":" in step_id:
            step_id = step_id.split(":", 1)[1]
        detail = self._detail(room, rid, rec.get("extra_cards"), rec.get("critic"),
                              rec.get("answers"))
        step = next((s for s in detail.get("next_steps") or []
                     if str(s.get("id")) == step_id), None)
        if step is None:
            raise err(404, "Шаг не найден", step_id=step_id)
        action: dict = {"kind": kind, "at": int(time.time())}
        if kind == "comment":
            text = str(kw.get("text") or "").strip()
            if not text:
                raise err(400, "Нужен текст комментария", field="text")
            action["text"] = text
        if kind == "move":
            changed = False
            if "due" in body:
                due = body.get("due")
                if due is not None and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(due)):
                    raise err(400, "due: YYYY-MM-DD", field="due", value=str(due)[:40])
                action["due"] = str(due) if due else None
                changed = True
            if "owner" in body:
                owner = body.get("owner")
                if owner is not None:
                    owner = str(owner).strip()
                    if owner and owner != "mikhail" and not profile_ok(owner):
                        raise err(400, "owner: имя профиля", field="owner", value=owner[:80])
                action["owner"] = owner or None
                changed = True
            if "direction" in body:
                direction = str(body.get("direction") or "").strip()
                if direction not in ("up", "down"):
                    raise err(400, "direction: «up» или «down»", field="direction", value=direction)
                action["direction"] = direction
                changed = True
            if not changed:
                raise err(400, "Перемещение без изменений: срок, исполнитель или порядок",
                          field="due|owner|direction")
        updates: dict = {}
        actions = dict(rec.get("step_actions") or {})
        actions[step_id] = action
        updates["step_actions"] = actions
        log = list(rec.get("steps_log") or [])
        log.append(dict(action, step_id=step_id))
        updates["steps_log"] = log[-80:]
        if kind == "move" and action.get("direction"):
            order = [str(item) for item in (rec.get("steps_order") or []) if str(item) != step_id]
            ids = [str(s.get("id")) for s in detail.get("next_steps") or [] if s.get("id")]
            order = order + [item for item in ids if item not in order]
            index = order.index(step_id) if step_id in order else -1
            if action["direction"] == "up" and index > 0:
                order[index - 1], order[index] = order[index], order[index - 1]
            elif action["direction"] == "down" and 0 <= index < len(order) - 1:
                order[index + 1], order[index] = order[index], order[index + 1]
            updates["steps_order"] = order
        elif kind == "move":
            order = [str(item) for item in (rec.get("steps_order") or [])]
            updates["steps_order"] = order
        dropped = [str(item) for item in (rec.get("steps_dropped") or [])]
        if kind == "drop":
            if step_id not in dropped:
                dropped.append(step_id)
            updates["steps_dropped"] = dropped
        try:
            self.call("registry", "update", ((self.cfg, room.get("id"), rid, updates), {}))
        except Exception as exc:  # noqa: BLE001 - метка в ветке важнее реестра
            self.log.error("реестр: действие по шагу %s не сохранено" % rid, exc)
        item_id = self._backlog_id(rid, step_id)
        if self._has_mod("ingest") and (kind == "drop" or "due" in action):
            try:
                ingest = self.mod("ingest")
                ingest.ensure_backlog_item(self.cfg, room.get("id"), item_id, detail)
                status = "dismissed" if kind == "drop" else (
                    str(step.get("state") or "open")
                    if str(step.get("state")) in ("open", "asked", "done") else "open")
                extra = {"due": action.get("due")} if "due" in action else {}
                ingest.set_backlog_status(self.cfg, room.get("id"), item_id, status, **extra)
            except Exception as exc:  # noqa: BLE001 - шаг вторичен, действие важнее
                self.log.warning("шаг %s не обновлён в бэклоге: %s" % (item_id, exc))
        # руководитель должен узнать о действии (ответ на шаг — механизм §5/§6/§12)
        mark = answers_mod.step_comment(kind, step_id, step.get("text"), action.get("text") or "",
                                        action.get("due"), action.get("owner"),
                                        action.get("direction"))
        try:
            with self.engine():
                self.call("engine_write", "act",
                          ((self.cfg, room.get("board") or self.board, rid, "comment"),
                           {"text": mark, "author": AUTHOR_MAXIM}))
        except Exception as exc:  # noqa: BLE001 - метка в реестре уже есть
            self.log.warning("сообщение о шаге %s не записано: %s" % (step_id, exc))
        view = answers_mod.apply_step(dict(step), action, None)
        if kind == "drop":
            view.update({"state": "dismissed", "state_label": "убран", "can_remove": False,
                         "can_agree": False, "can_drop": False})
        payload = {"ok": True, "action": "step_" + kind, "step_id": step_id,
                   "step": view, "message": mark, "log": log[-5:],
                   "reaction_label": view.get("reaction_label"),
                   "pending_label": answers_mod.PENDING_RU}
        if kind == "drop":
            payload["dropped"] = dropped
        payload["request"] = self._summary_or_local(
            room, rid, rec.get("text") or "", rec.get("title") or "",
            bool(rec.get("critic")), rec.get("mode") or "auto", rec.get("answers"))
        return 200, payload, {}

    # --- решения «предложено»: принять / отклонить / обсудить (W19-D2, B-20) ---

    def _decision_action(self, room: dict, rec: dict, rid: str, kw: dict, body: dict):
        """Действие Михаила над решением руководителя (B-20).

        Механизм тот же, что у ответов на вопросы и действий по шагам (W14-D §1–§4):

        * в ветке — метка ``[Михаил · решение d_x «текст решения»] Принять: …``;
        * в реестре — запись ``answers[d_x]`` с ``kind="decision"``; её читают деталь
          запроса, вкладка «Дальше» и счётчики, поэтому принятое/отклонённое сразу
          уходит из «Нужен ваш ответ» (данные API, без перезагрузки);
        * руководителю — карточка «Уточнение» с контрактом реакции
          (``metadata.reaction.outcome``), чтобы исход был явным, а не по словам.
        """
        body = body if isinstance(body, dict) else {}
        decision_id = str(kw.get("decision_id") or "").strip()
        if ":" in decision_id:                      # `<rid>:d_1` → `d_1`
            decision_id = decision_id.split(":", 1)[1]
        if not re.fullmatch(r"[A-Za-z0-9_\-]{1,64}", decision_id):
            raise err(400, "Некорректный id решения", field="decision_id", value=decision_id[:80])
        outcome = str(body.get("decision") or body.get("outcome") or "").strip().lower()
        if outcome not in answers_mod.DECISION_ACTIONS:
            raise err(400, "decision: принять/отклонить/обсудить",
                      field="decision", allowed=list(answers_mod.DECISION_ACTIONS), value=outcome[:40])
        text = body.get("text")
        text = text.strip() if isinstance(text, str) else ""
        if len(text) > MAX_NOTE:
            raise err(400, text_too_long("Текст", len(text), MAX_NOTE), field="text",
                      length=len(text), limit=MAX_NOTE)
        if has_nul(text):
            raise err(400, "В тексте есть NUL-байт", field="text")
        if outcome == "discuss" and not text:
            raise err(400, "Напишите или надиктуйте, что обсудить", field="text")
        detail = self._detail(room, rid, rec.get("extra_cards"), rec.get("critic"),
                              rec.get("answers"))
        decision = next((d for d in detail.get("decisions") or []
                         if isinstance(d, dict) and str(d.get("id")) == decision_id), None)
        if decision is None:
            raise err(404, "Решение не найдено", decision_id=decision_id)
        at = int(time.time())
        mark = answers_mod.decision_comment(outcome, decision_id, decision.get("text") or "", text)
        record = {"kind": "decision", "decision_id": decision_id,
                  "decision_text": str(decision.get("text") or ""),
                  "action": outcome,
                  "outcome": {"accept": "accepted", "reject": "rejected",
                              "discuss": "discuss"}[outcome],
                  "text": text or answers_mod.DECISION_ACTION_RU[outcome],
                  "user_text": text or None,
                  "at": at, "answered": True, "status": "sent",
                  "decision_status_at_answer": str(decision.get("status") or "proposed")}
        answers = dict(rec.get("answers") or {})
        answers[decision_id] = record
        self._save_answers(room, rid, answers)
        # Ветка: решение уходит руководителю тем же путём, что и ответ на вопрос.
        try:
            with self.engine():
                self.call("engine_write", "act",
                          ((self.cfg, room.get("board") or self.board, rid, "comment"),
                           {"text": mark, "author": AUTHOR_MAXIM}))
        except Exception as exc:  # noqa: BLE001 - запись в реестре уже есть
            self.log.warning("сообщение о решении %s не записано: %s" % (decision_id, exc))
        note: dict = {}
        try:
            note = self.call("thread", "post_decision_note",
                             ((self.cfg, room, rid, dict(decision), outcome, text), {}),
                             ((self.cfg, room, rid, dict(decision), outcome, text), {})) or {}
        except Exception as exc:  # noqa: BLE001 - карточка не важнее ответа
            self.log.warning("карточка-решение %s: %s" % (rid, exc))
        view = answers_mod.apply_decision(dict(decision), record, None)
        payload = {"ok": True, "action": "decision", "decision_id": decision_id,
                   "decision": view, "message": mark,
                   "action_label": answers_mod.DECISION_ACTION_RU[outcome],
                   "answer_label": view.get("answer_label"),
                   "pending_label": answers_mod.PENDING_RU,
                   "chain_hint": answers_mod.CHAIN_HINT}
        for key in ("task_id", "merged", "routed_to"):
            if note.get(key) is not None:
                payload.setdefault("note", {})[key] = note[key]
        payload["request"] = self._summary_or_local(
            room, rid, rec.get("text") or "", rec.get("title") or "",
            bool(rec.get("critic")), rec.get("mode") or "auto", answers)
        return 200, payload, {}

    # --- проверки значений действия (D1) ---

    def _check_action_values(self, room: dict, rec: dict, rid: str, action: str, kw: dict) -> None:
        """task_id — из дерева запроса; profile — из комнаты; author — белый список.

        Всё проверяется ДО вызова CLI, поэтому инъекция опций (`--rm=…`, `--help`)
        и чужая карточка не доходят до `hermes kanban`.
        """
        if action == "archive" and kw.get("task_id"):
            raise err(400, "Действие archive архивирует корень и детей — task_id не принимается",
                      field="task_id", allowed=[])
        for field in ("task_id", "question_id", "profile", "author", "title"):
            value = kw.get(field)
            if isinstance(value, str) and has_nul(value):
                raise err(400, "В поле %s есть NUL-байт" % field, field=field)
        if "task_id" in kw:
            self._check_task_owned(room, rec, rid, kw["task_id"])
        if task_id_ok(str(kw.get("question_id") or "")):
            self._check_task_owned(room, rec, rid, kw["question_id"], field="question_id")
        if action in ("reassign", "rework") and "profile" in kw:
            self._check_room_profile(room, kw["profile"])
        if action in ("comment", "answer") and "author" in kw:
            self._check_author(room, kw["author"])

    def _check_task_owned(self, room: dict, rec: dict, rid: str, value, field: str = "task_id") -> str:
        """`^t_[0-9a-f]{8}$` + принадлежность дереву запроса (400/404)."""
        text = str(value or "").strip()
        if not task_id_ok(text):
            raise err(400, "Некорректный id карточки: ожидается t_ и 8 hex-символов",
                      field=field, value=text[:80])
        allowed = self._tree_ids(room, rec, rid)
        if allowed is not None and text not in allowed:
            raise err(404, "Карточка не принадлежит этому запросу",
                      reason="foreign_task", task_id=text, rid=str(rid))
        return text

    def _tree_ids(self, room: dict, rec: dict, rid: str):
        """id дерева запроса по чтению (extra_cards + tree); None — движок не прочитался."""
        allowed = {str(rid)}
        extra = rec.get("extra_cards") or []
        for item in extra:
            if isinstance(item, dict):
                for key in ("id", "task_id"):
                    if item.get(key):
                        allowed.add(str(item[key]))
            elif item:
                allowed.add(str(item))
        try:
            detail = self._detail(room, rid, extra, rec.get("critic"))
        except ApiError as exc:
            self.log.warning("дерево %s не прочиталось: %s" % (rid, exc.message))
            return None          # пусть решает engine_write (он читает дерево по SQL)
        except Exception as exc:  # noqa: BLE001
            self.log.warning("дерево %s не прочиталось: %s" % (rid, exc))
            return None
        for node in detail.get("tree") or []:
            if isinstance(node, dict) and node.get("id"):
                allowed.add(str(node["id"]))
        return allowed

    def _room_profiles(self, room: dict) -> set:
        """Состав комнаты: директор, критик и участники."""
        names = {room.get("orchestrator"), room.get("critic")}
        for member in room.get("members") or []:
            if isinstance(member, dict):
                names.add(member.get("profile"))
        return {str(name) for name in names if name}

    def _check_room_profile(self, room: dict, profile) -> str:
        text = str(profile or "").strip()
        if text.lower() in UNASSIGN_VALUES:
            return "none"
        names = self._room_profiles(room)
        if not profile_ok(text):
            raise err(400, "Некорректное имя профиля", field="profile", profile=text[:80],
                      available=sorted(names))
        if names and text not in names:
            raise err(400, "Профиль не из состава комнаты", field="profile",
                      profile=text, available=sorted(names))
        return text

    def _check_author(self, room: dict, author) -> str:
        text = str(author or "").strip()
        allowed = {AUTHOR_MAXIM} | self._room_profiles(room) | self._roster_profiles()
        if text not in allowed:
            raise err(400, "Автор не из комнаты", field="author", author=text[:80],
                      available=sorted(allowed))
        return text

    def _roster_profiles(self) -> set:
        """Имена профилей Hermes (engine_write.list_profiles отдаёт ключ ``name``)."""
        try:
            items = self.call("engine_write", "list_profiles", ((self.cfg,), {}))
        except ApiError:
            return set()
        except Exception:  # noqa: BLE001
            return set()
        if isinstance(items, dict):
            items = items.get("items") or []
        names = set()
        for item in items or []:
            if isinstance(item, dict):
                name = item.get("name") or item.get("profile")
                if name:
                    names.add(str(name))
        return names

    def _request_status(self, room: dict, rid: str, rec: dict):
        """Свежий статус запроса (queued/working/…) или None, если не прочитался."""
        try:
            summary = self._summary(room, rid, rec.get("extra_cards"), rec.get("critic"))
        except ApiError as exc:
            self.log.warning("статус %s не прочитался: %s" % (rid, exc.message))
            return None
        return (summary or {}).get("status")

    def _last_comment(self, room: dict, task_id: str):
        """Последний комментарий карточки в форме API §6.3 (id/author/text/at)."""
        try:
            return self.call("engine_write", "last_comment",
                             ((self.cfg, room.get("board") or self.board, task_id), {}))
        except Exception as exc:  # noqa: BLE001 - ответ без id комментария не критичен
            self.log.warning("last_comment %s: %s" % (task_id, exc))
            return None

    def _task_node(self, room: dict, rid: str, rec: dict, task_id):
        """Свежий TaskNode карточки после действия (форма API §3.4)."""
        if not task_id:
            return None
        detail = self._detail(room, rid, rec.get("extra_cards"), rec.get("critic"),
                              rec.get("answers"))
        return next((n for n in detail.get("tree") or [] if n.get("id") == task_id), None)

    def _backlog_item_from_request(self, room: dict, room_id: str, item_id: str):
        """Пункт `<root_id>:<local>` из метаданных запроса → бэклог (шаг ещё не принят).

        Шаги на странице запроса живут в `RequestDetail.next_steps`, а в бэклог
        комнаты попадают только после ingest. Без этого отметка падала 404
        «Элемент бэклога не найден» у всех непринятых запросов.
        """
        item_id = str(item_id or "")
        if ":" not in item_id:
            return None
        rid = item_id.split(":", 1)[0]
        rec = next((r for r in self._records(room_id, quiet=True)
                    if str(r.get("root_id") or "") == rid), None)
        if rec is None:
            return None
        try:
            detail = self._detail(room, rid, rec.get("extra_cards"), rec.get("critic"),
                                  rec.get("answers"))
        except ApiError as exc:
            self.log.warning("бэклог: detail %s не прочитался: %s" % (rid, exc.message))
            return None
        ensure = getattr(self.mod("ingest"), "ensure_backlog_item", None)
        if not callable(ensure):
            return None
        try:
            return ensure(self.cfg, room_id, item_id, detail)
        except Exception as exc:  # noqa: BLE001 - без upsert остаётся прежний 404
            self.log.warning("бэклог: upsert %s не удался: %s" % (item_id, exc))
            return None

    def h_backlog_set(self, query, body, room_id, item_id):
        room = self._room(room_id)
        status = body.get("status")
        if status not in BACKLOG_STATUSES:
            raise err(400, "status: open|asked|done|dismissed", field="status", value=status)
        extra: dict = {}
        if "due" in body:
            due = body.get("due")
            if due is not None and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(due)):
                raise err(400, "due: YYYY-MM-DD", field="due")
            extra["due"] = due
        try:
            with self.engine():
                self.call("ingest", "set_backlog_status", ((self.cfg, room_id, item_id, status), extra))
        except ApiError as exc:
            if exc.status != 404 or self._backlog_item_from_request(room, room_id, item_id) is None:
                raise
            # пункт перенесён из метаданных запроса — повторяем действие
            with self.engine():
                self.call("ingest", "set_backlog_status",
                          ((self.cfg, room_id, item_id, status), dict(extra)))
        view = self._backlog(room_id)
        item = next((i for i in view["items"] if (i or {}).get("id") == item_id), None)
        if item is None:
            raise err(404, "Элемент бэклога не найден", room=room_id, item_id=item_id)
        return 200, {"item": item, "counts": view["counts"]}, {}

    def h_backlog_ask(self, query, body, room_id, item_id):
        room = self._room(room_id)
        text = body.get("text")
        if text is not None and (not isinstance(text, str) or not text.strip()):
            raise err(400, "Пустой текст запроса", field="text")
        extra = {"text": text} if isinstance(text, str) and text.strip() else {}
        try:
            with self.engine():
                res = self.call("ingest", "ask_item", ((self.cfg, room_id, item_id), extra))
        except ApiError as exc:
            if exc.status != 404 or self._backlog_item_from_request(room, room_id, item_id) is None:
                raise
            with self.engine():
                res = self.call("ingest", "ask_item", ((self.cfg, room_id, item_id), dict(extra)))
        if isinstance(res, dict):
            ask_text, item = (res.get("text") or ""), res.get("item")
        else:
            ask_text, item = (res or ""), None
        ask_text = str(ask_text or "").strip()
        if not ask_text:
            raise err(404, "Элемент бэклога не найден", room=room_id, item_id=item_id)
        if item is None:
            try:
                view = self._backlog(room_id)
                item = next((i for i in view["items"] if (i or {}).get("id") == item_id), None)
            except ApiError:
                item = None
        critic = bool(room.get("critic_pass"))
        title = ((item or {}).get("text") or derive_title(ask_text))[:120]
        made = self._new_request(room, ask_text, title, critic, [])
        root_id = made["root_id"]
        with self.engine():
            item = self.call("ingest", "mark_asked", ((self.cfg, room_id, item_id, root_id), {}),
                             ((self.cfg, room_id, item_id), {"root_id": root_id}))
        payload = {"request_id": root_id, "root_id": root_id, "item": item,
                   "request": self._summary_or_local(room, root_id, ask_text, title, critic, "auto"),
                   "dispatcher": self._dispatcher_short(), "warning": made["warning"]}
        return 201, payload, {"Location": "/api/requests/" + str(root_id)}

    # --- отчёты, журнал, поиск ---
    def h_open(self, query, body):
        path = body.get("path")
        if not isinstance(path, str) or not path.strip():
            raise err(400, "Нужен path", field="path")
        path = path.strip()
        if has_nul(path):
            raise err(400, "В пути есть NUL-байт", field="path")
        # Папка проекта комнаты — не файл: открываем её целиком в проводнике.
        # W10: `kind: "dir"` — это осознанный запрос папки, `mkdir: true` — «Создать
        # папку и открыть». Разрешены только пути, которые пропускает
        # project_dir.check_dir (системные каталоги, Hermes и data/ отклоняются там же).
        want_dir = str(body.get("kind") or "").strip().lower() == "dir"
        make_dir = bool(body.get("mkdir"))
        try:
            report = self.call("project_dir", "open_dir_report",
                               ((self.cfg, path), {"create": make_dir}))
        except ApiError:
            raise
        except Exception as exc:  # noqa: BLE001 - модуль может быть не готов
            report = None
            self.log.warning("open_dir %s: %s" % (path, exc))
        if isinstance(report, dict) and (want_dir or report.get("ok")):
            if report.get("ok"):
                return 200, {"ok": True, "path": report.get("path") or path,
                             "revealed": True, "kind": "dir",
                             "foreground": bool(report.get("foreground")),
                             "reason": report.get("reason") or None}, {}
            reason = str(report.get("reason") or "Не удалось открыть папку")
            payload = {"ok": False, "revealed": False, "kind": "dir",
                       "path": report.get("path") or path, "reason": reason,
                       "can_create": bool(report.get("can_create"))}
            if report.get("can_create"):
                # папки нет на диске — интерфейс предложит «Создать папку и открыть»
                return 200, payload, {}
            raise ApiError(400, reason, detail=payload)
        self._file_guard(path)
        try:
            res = self.call("files", "reveal", ((self.cfg, path), {}))
        except ApiError:
            raise
        except Exception as exc:  # noqa: BLE001
            self.log.error("reveal %s" % path, exc)
            raise ApiError(500, "Не удалось открыть проводник", detail={"path": path}) from exc
        revealed = bool(res.get("revealed")) if isinstance(res, dict) else bool(res)
        return 200, {"ok": True, "path": path, "revealed": revealed}, {}

    def h_consolidate(self, query, body, room_id):
        room = self._room(room_id)
        note = body.get("note")
        if note is not None and (not isinstance(note, str) or len(note) > 2000):
            raise err(400, "note до 2000 символов", field="note")
        now = int(time.time())
        for rec in self._records(room_id, quiet=True):
            if "STATE.md" not in (rec.get("title") or ""):
                continue
            status = None
            try:
                status = (self._summary(room, rec.get("root_id"), rec.get("extra_cards"),
                                         rec.get("critic")) or {}).get("status")
            except ApiError:
                status = None
            if status not in ("done", "archived"):
                raise err(409, "Свод STATE.md уже идёт", existing_root_id=rec.get("root_id"))
            if now - int(rec.get("created_at") or 0) < 600:
                raise err(409, "Свод STATE.md создавался менее 10 минут назад",
                          existing_root_id=rec.get("root_id"))
        text = ("Собери актуальный STATE.md комнаты «%s»: решения, шаги, фазы, открытые вопросы. "
                "Запиши в Vault и обнови зеркало." % (room.get("title") or room_id))
        if note and note.strip():
            text += " " + note.strip()
        title = "Обнови STATE.md: %s" % (room.get("title") or room_id)
        return self._create(room, text, title, bool(room.get("critic_pass")), [], kind="consolidate")

    def h_adopt(self, query, body):
        """Привязать существующую карточку доски к комнате (без записи в доску).

        Проверка существования — через ``engine_read`` (карточка должна читаться на
        доске комнаты и не принадлежать другой комнате). Tenant не меняем, критик
        не включаем: adopted-запрос попадает в реестр с ``critic=false``.
        """
        room_id = str(body.get("room") or "").strip()
        task_id = str(body.get("task_id") or "").strip()
        if not room_id:
            raise err(400, "Нужен room", field="room")
        room = self._room(room_id)
        if not re.fullmatch(r"t_[A-Za-z0-9_]+", task_id):
            raise err(400, "Некорректный task_id", field="task_id")
        existing = None
        try:
            existing = self.call("registry", "get", ((self.cfg, room_id, task_id), {}),
                                 ((self.cfg, task_id), {}))
        except ApiError:
            existing = None
        if existing and isinstance(existing, dict):
            summary = self._summary_or_local(room, task_id, existing.get("text") or "",
                                             existing.get("title") or "", False, "auto")
            return 200, {"ok": True, "request": summary, "adopted": bool(existing.get("adopted")),
                         "already": True, "warning": None}, {}
        detail = None
        with self.engine():
            detail = self._detail(room, task_id, [], False)
        owner_room = (detail or {}).get("room")
        if owner_room and str(owner_room) != room_id:
            raise err(409, "Карточка принадлежит другой комнате", room=room_id,
                      owner_room=owner_room, task_id=task_id)
        title = str((detail or {}).get("title") or "")
        text = str((detail or {}).get("text") or "")
        record = {"root_id": task_id, "slug": make_slug(), "title": title, "text": text,
                  "created_at": int(time.time()), "critic": False, "mode": "auto",
                  "adopted": True, "extra_cards": [], "ingested": {}, "retargeted": True}
        self._register(room, record)
        summary = self._summary_or_local(room, task_id, text, title, False, "auto")
        warning = None
        if not (summary.get("progress") or {}).get("total"):
            warning = {"code": "legacy_metadata",
                       "message": "Карточка создана вне приложения — metadata могут быть пустыми."}
        return 200, {"ok": True, "request": summary, "adopted": True, "already": False,
                     "warning": warning}, {}


    def _new_request(self, room: dict, text: str, title: str, critic: bool, attach=None,
                     brief: bool = False, critic_mode: str | None = None,
                     max_rounds: int | None = None, user_title: bool = False) -> dict:
        """Создать корневую карточку через тот же путь, что POST /requests.

        contract.build_request_body → engine_write.create_request → registry.add_request.
        Возвращает ``{root_id, slug, title, text, critic, warning}`` или бросает ApiError.
        ``brief`` (WAVE 9, A5) — тело в режиме брифа проекта.
        ``critic_mode`` (W13-E §9) — режим проверки: «rounds» или «until_approved»;
        по умолчанию берётся из комнаты, чтобы запрос жил по её настройке.
        """
        slug = make_slug()
        attach = list(attach or [])
        body_text = self.call("contract", "build_request_body",
                              ((room, text, slug, self.cfg, attach), {}),
                              ((room, text, slug, self.cfg), {"attach": attach}))
        with self.engine():
            res = self.call("engine_write", "create_request",
                            ((self.cfg, room, slug, title, body_text), {"brief": bool(brief)}),
                            ((self.cfg, room, slug, title, body_text), {}))
        res = res if isinstance(res, dict) else {}
        root_id = res.get("root_id") or res.get("id")
        if not root_id:
            raise ApiError(502, "Движок не вернул id созданной карточки",
                           detail={"module": "engine_write"})
        mode = str(critic_mode or room.get("critic_mode") or "rounds")
        if mode not in CRITIC_MODES:
            mode = "rounds"
        try:
            cap = int(room.get("critic_round_cap") or DEFAULT_CRITIC_ROUND_CAP)
        except (TypeError, ValueError):
            cap = DEFAULT_CRITIC_ROUND_CAP
        record = {"root_id": root_id, "slug": slug, "title": title, "text": text,
                  "created_at": int(time.time()), "critic": bool(critic), "mode": "auto",
                  "critic_mode": mode, "critic_round_cap": cap,
                  "adopted": False, "extra_cards": [], "ingested": {}, "retargeted": False}
        # W17-B §4 (B9 усилено): название сразу — мгновенное `derived` (у пользователя,
        # задавшего имя при создании, — `user` + блокировка от перезаписи).
        record.update(self._title_fields(text, title, user=bool(user_title)))
        if isinstance(max_rounds, int) and not isinstance(max_rounds, bool) and 1 <= max_rounds <= 5:
            record["max_rounds"] = max_rounds
        if brief:
            record["brief"] = True
        self._register(room, record)
        warning = res.get("warning")
        dispatcher = self._dispatcher()
        if not dispatcher.get("ok") and not warning:
            warning = {"code": "dispatcher_down",
                       "message": "Запрос создан, но диспетчер не запущен — работа не начнётся сама."}
        return {"root_id": root_id, "slug": slug, "title": title, "text": text,
                "critic": bool(critic), "warning": warning, "record": record}

    def _create(self, room: dict, text: str, title: str, critic: bool, attach, mode: str = "auto",
                kind: str | None = None, brief: bool = False, critic_mode: str | None = None,
                max_rounds: int | None = None, user_title: bool = False):
        made = self._new_request(room, text, title, critic, attach, brief=brief,
                                 critic_mode=critic_mode, max_rounds=max_rounds,
                                 user_title=user_title)
        if kind:
            self._register(room, dict(made["record"], kind=kind))  # пометка вида запроса
        root_id = made["root_id"]
        # W17-B §4: улучшение названия малой моделью Hermes — В ФОНЕ, создание не ждёт.
        try:
            self.mod("titles").start(self.cfg, room, root_id, text, user=bool(user_title))
        except Exception as exc:  # noqa: BLE001 - название вторично
            self.log.warning("название %s: %s" % (root_id, exc))
        summary = self._summary_or_local(room, root_id, text, title, critic, mode)
        self._title_view(summary, made["record"])
        payload = {"request": summary,
                   "root_id": root_id, "dispatcher": self._dispatcher_short(),
                   "warning": made["warning"]}
        return 201, payload, {"Location": "/api/requests/" + str(root_id)}

    def _clone(self, room: dict, rec: dict, title=None):
        """Клонировать запрос: тот же текст и режим, новая карточка."""
        text = rec.get("text") or ""
        if not text:
            raise err(409, "В реестре нет текста запроса — клонировать нечего", rid=rec.get("root_id"))
        new_title = (title or ("%s (копия)" % (rec.get("title") or derive_title(text))))[:120]
        status, payload, headers = self._create(room, text, new_title, bool(rec.get("critic")),
                                                [], mode=rec.get("mode") or "auto",
                                                critic_mode=rec.get("critic_mode"))
        payload["ok"] = True
        payload["action"] = "clone"
        return status, payload, headers

    def _critic_action(self, room: dict, rec: dict, rid: str, action: str, kw: dict):
        """critique/revise: логика целиком в critic_loop, app только вызывает."""
        if not self._has_mod("critic_loop"):
            raise ApiError(501, "Модуль critic_loop не готов — ручной критик недоступен",
                           detail={"module": "critic_loop"})
        func_name = {"critique": "manual_critique", "revise": "manual_revision"}.get(action, "")
        func = getattr(self.mod("critic_loop"), func_name, None) if func_name else None
        if not callable(func):
            raise ApiError(501, "critic_loop не умеет %s" % action,
                           detail={"module": "critic_loop", "function": func_name or action})
        detail = self._detail(room, rid, rec.get("extra_cards"), rec.get("critic"))
        with self.engine():
            res = try_call(func, ((self.cfg, room, rec, detail), {"text": kw.get("text")}),
                           ((self.cfg, room, rec, detail), {}))
        payload = {"ok": True, "action": action}
        if isinstance(res, dict):
            payload.update(res)
        if action == "revise":
            fresh = self._detail(room, rid, rec.get("extra_cards"), rec.get("critic"),
                                 rec.get("answers"))
            previous = int((fresh.get("synthesis") or {}).get("version") or 0)
            payload["previous_version"] = previous
            payload["version"] = previous + 1
            payload["revision_task_id"] = payload.get("task_id")
            payload["note"] = str(kw.get("text") or "")
        elif action == "critique":
            payload["critic_task_id"] = payload.get("task_id")
            payload["state"] = "reviewing"
            payload["round"] = int(payload.get("round") or 0)
        payload["request"] = self._summary_or_local(room, rid, rec.get("text") or "",
                                                    rec.get("title") or "",
                                                    bool(rec.get("critic")),
                                                    rec.get("mode") or "auto",
                                                    rec.get("answers"))
        return 200, payload, {}

    # --- команда, карта, бэклог ---
