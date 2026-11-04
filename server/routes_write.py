"""POST/PATCH/DELETE-хендлеры «Комнаты команды» — миксин для ``App`` (server/app.py).

Здесь всё, что меняет домен: комнаты, участники, создание запросов, действия над
ними, бэклог, обслуживание и adopt. Каждая запись идёт через движок канбана.
"""
from __future__ import annotations

import re
import time

try:  # пакетный и «плоский» импорт
    from . import config
    from .app_common import (ACTION_ALLOWED, ACTION_REQUIRED, ACTION_TEXT, AUTHOR_MAXIM, BACKLOG_STATUSES,
                             MAX_ATTACH, MAX_NOTE, MAX_TEXT, PATCH_FIELDS, REPORT_KEY_RE, TASK_ID_RE,
                             UNASSIGN_VALUES, derive_title, err, has_nul, make_slug, profile_ok,
                             task_id_ok, text_too_long, try_call)
    from .http_util import ApiError
except ImportError:  # pragma: no cover
    import config  # type: ignore
    from app_common import (ACTION_ALLOWED, ACTION_REQUIRED, ACTION_TEXT, AUTHOR_MAXIM,  # type: ignore
                            BACKLOG_STATUSES, MAX_ATTACH, MAX_NOTE, MAX_TEXT, PATCH_FIELDS,
                            REPORT_KEY_RE, TASK_ID_RE, UNASSIGN_VALUES, derive_title, err, has_nul,
                            make_slug, profile_ok, task_id_ok, text_too_long, try_call)
    from http_util import ApiError  # type: ignore


class WriteRoutes:
    """Обработчики записи (``self`` — экземпляр ``server.app.App``)."""

    def h_kick(self, query, body):
        res = self._dispatch(dict(body or {}))
        payload = {"ok": True, "spawned": int(res.get("spawned") or 0),
                   "reclaimed": int(res.get("reclaimed") or 0),
                   "promoted": int(res.get("promoted") or 0),
                   "planned": res.get("planned") or [], "max": res.get("max"),
                   "dispatcher": self._dispatcher_short(), "at": int(time.time())}
        return 200, payload, {}

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
                return self.call("engine_write", "act",
                                 ((self.cfg, board, None, "dispatch"),
                                  {"max": max_cards, "confirm": confirm}))
        try:
            return runner(self.cfg, board, max_cards=max_cards, confirm=confirm)
        except ApiError:
            raise
        except Exception as exc:  # noqa: BLE001 - доменная ошибка движка
            raise self._engine_failure(exc) from exc

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
        bad = sorted(set(body) - PATCH_FIELDS)
        if bad:
            raise err(400, "Поля нельзя менять", fields=bad, allowed=sorted(PATCH_FIELDS))
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
        if title is not None:
            if not isinstance(title, str) or not title.strip():
                raise err(400, "title пуст", field="title")
            title = title.strip()
            if has_nul(title):
                raise err(400, "В заголовке есть NUL-байт", field="title")
            if len(title) > 120:
                raise err(400, "title длиннее 120 символов", field="title", length=len(title))
        else:
            title = derive_title(text)
        brief = body.get("brief", False)
        if not isinstance(brief, bool):
            raise err(400, "brief: ожидается да/нет", field="brief", value=brief)
        return self._create(room, text, title, critic, attach, mode="auto", brief=brief)

    def h_request_actions(self, query, body, rid):
        action = body.get("action")
        if action not in ACTION_ALLOWED:
            raise err(400, "Неизвестное действие", action=action, allowed=list(ACTION_ALLOWED))
        room, rec = self._find_record(rid)
        kw: dict = {}
        for field in ("task_id", "question_id", "profile"):
            if field in body and body.get(field) is not None:
                value = body.get(field)
                if not isinstance(value, str) or not value.strip():
                    raise err(400, "Поле %s должно быть непустой строкой" % field, field=field)
        for field in ("task_id", "question_id", "profile", "title", "author"):
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
        question = None
        if action == "answer":
            before = self._detail(room, rid, rec.get("extra_cards"), rec.get("critic"),
                                  rec.get("answers"))
            question = next((q for q in before.get("open_questions") or []
                             if str(q.get("id")) == str(kw.get("question_id"))), None)
            if question is None:
                raise err(404, "Вопрос не найден", question_id=kw.get("question_id"))
            if question.get("answered"):
                raise err(409, "Вопрос уже отвечен", question_id=kw.get("question_id"))
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
            updated = dict(answers or {})
            updated[str(kw.get("question_id"))] = {"text": kw.get("text") or "", "at": at}
            try:
                self.call("registry", "update", ((self.cfg, room.get("id"), rid,
                                                  {"answers": updated}), {}))
                answers = updated
            except Exception as exc:  # noqa: BLE001 - комментарий уже записан
                self.log.error("реестр: ответ на вопрос %s не сохранён" % rid, exc)
            payload["question"] = dict(question or {}, answered=True,
                                       answer=kw.get("text") or "", answered_at=at)
            payload["unblocked"] = list(res.get("unblocked") or [])
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
                     brief: bool = False) -> dict:
        """Создать корневую карточку через тот же путь, что POST /requests.

        contract.build_request_body → engine_write.create_request → registry.add_request.
        Возвращает ``{root_id, slug, title, text, critic, warning}`` или бросает ApiError.
        ``brief`` (WAVE 9, A5) — тело в режиме брифа проекта.
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
        record = {"root_id": root_id, "slug": slug, "title": title, "text": text,
                  "created_at": int(time.time()), "critic": bool(critic), "mode": "auto",
                  "adopted": False, "extra_cards": [], "ingested": {}, "retargeted": False}
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
                kind: str | None = None, brief: bool = False):
        made = self._new_request(room, text, title, critic, attach, brief=brief)
        if kind:
            self._register(room, dict(made["record"], kind=kind))  # пометка вида запроса
        root_id = made["root_id"]
        payload = {"request": self._summary_or_local(room, root_id, text, title, critic, mode),
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
                                                [], mode=rec.get("mode") or "auto")
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
