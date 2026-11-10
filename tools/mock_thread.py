# -*- coding: utf-8 -*-
"""Диалог по запросу в мок-сервере: лента thread, сообщение руководителю, имитация ответа.

Контракт ThreadItem (docs/WAVE8-RULES.md, блок A / Д1):
    {id, at(unix), author_kind: "mikhail"|"agent"|"system", author, author_label,
     text, task_id|None, kind: "message"|"comment"|"event"}

Поведение мока:
* `POST /api/requests/{rid}/messages {text, attach?, urgent?}` — сообщение сразу
  появляется в ленте (автор mikhail) и комментарием `[Михаил · уточнение]` на корне;
* через 3–6 с (в тестах — быстрее, делится на phase_scale) «приходит» ответ
  руководителя комнаты («Принял: … Передал: …») и системное событие;
* для `done`-запроса ответ руководителя запускает существующую имитацию
  «На доработку» — версия N+1 (mock_write._stored_revise / jump в live);
* пока ответ не «пришёл», новое сообщение дописывается к той же передаче
  (`merged: true`) — как пакетирование карточки «Уточнение» на боевом сервере.

Файлы: только tools/mock_thread.py, на диск ничего не пишется.
"""
from __future__ import annotations

import copy
import random
import re
import time

from mock_core import ApiError, now

MAX_TEXT = 1000000            # docs/API.md §6.3: общий лимит текста
MAX_ATTACH = 10
DELAY_MIN, DELAY_MAX = 3.0, 6.0      # секунды «раздумья» руководителя, как в ТЗ
MARK = "[Михаил · уточнение]"
MARK_URGENT = "[Михаил · ПЕРЕСМОТРЕТЬ ПЛАН]"

# Имена из демо-данных (Vault/журнал Лето) — чтобы лента читалась как живая переписка.
NAMES = {"biz-director": "Глеб", "biz-lawyer": "Антон", "biz-ops": "Сергей", "biz-strategy": "Илья",
         "biz-marketing": "Марина", "biz-cfo": "Вера", "biz-critic": "Ольга", "biz-runner": "Егор",
         "lawyer": "Юрист", "coder": "Программист", "surveyor": "Исследователь"}
# роли по-русски — как в server/thread.py ROLE_RU (ключи профилей не меняем)
ROLE_RU = {"orchestrator": "руководитель комнаты", "critic": "критик", "worker": "исполнитель",
           "observer": "наблюдатель"}
TOPICS = {"biz-ops": "упаковка и поставщики", "biz-marketing": "каналы и продвижение",
          "biz-lawyer": "юридический блок", "biz-cfo": "экономика и расчёты",
          "biz-strategy": "план и этапы", "biz-runner": "сбор фактов", "biz-critic": "проверка свода"}
# Кому передать уточнение: ключевые слова → профиль и тема (по компетенции, Д1).
ROUTING = (
    (("упаков", "поставщ", "moq", "короб", "тираж", "доставк", "склад", "слой", "образец", "котиров",
      "печать", "пе "), "biz-ops", "упаковка и поставщики"),
    (("канал", "продаж", "едок", "развоз", "telegram", "реклам", "контент", "d19"),
     "biz-marketing", "каналы и продвижение"),
    (("роспатент", "ип", "оквэд", "бренд", "юр", "сэс", "лиценз", "налог", "договор", "разрешение"),
     "biz-lawyer", "юридический блок"),
    (("марж", "себестоим", "цифр", "смет", "эконом", "цен", "r$"), "biz-cfo", "экономика и расчёты"),
    (("план", "этап", "стратег", "дорог", "roadmap", "срок"), "biz-strategy", "план и этапы"),
)
_CLEAN = re.compile(r"[#*`>]+")

# Порядок в одну и ту же секунду: сообщение Михаила → ответ → системное событие.
KIND_RANK = {"message": 0, "comment": 1, "event": 2}


_PREFIX = re.compile(r"^(?:уточнение|дополнение|ещ[её]|и)\s*[:\-—]\s*", re.IGNORECASE)


def _short(text: str, limit: int = 90) -> str:
    """Короткая суть сообщения для ответа руководителя (без «Уточнение:» и кавычек)."""
    line = _CLEAN.sub("", str(text or "")).strip()
    line = _PREFIX.sub("", line).strip()
    line = re.split(r"(?<=[.!?])\s", line)[0].strip(" .!?;:")
    if len(line) > limit:
        line = line[:limit].rsplit(" ", 1)[0].rstrip(" ,;:") + "…"
    return line or "уточнение"


def author_label(profile: str, role: str | None = None) -> str:
    """`biz-director` → «Глеб · руководитель» (Д1.6: по-русски, не техническое имя)."""
    profile = str(profile or "system")
    if profile in ("system", "team"):
        return "Комната команды"
    name = NAMES.get(profile) or profile
    role_ru = ROLE_RU.get(role or "") or ""
    return "%s · %s" % (name, role_ru) if role_ru else name


def lead_profile(room: dict, detail: dict) -> str:
    """Руководитель комнаты: `room.orchestrator`, иначе исполнитель корня."""
    lead = (room or {}).get("orchestrator")
    if lead:
        return str(lead)
    root = next((n for n in (detail.get("tree") or []) if n.get("role") == "root"), None)
    if root and root.get("assignee"):
        return str(root["assignee"])
    return "biz-director"


def _roster(room: dict) -> list:
    out = []
    for profile in [room.get("orchestrator")] + [m.get("profile") for m in room.get("members") or []]:
        if profile and profile not in out:
            out.append(profile)
    return out


def route_for(text: str, room: dict, detail: dict, lead: str) -> tuple:
    """Кому по компетенции передать уточнение → (профиль, тема)."""
    roster = [p for p in _roster(room) if p != lead]
    low = str(text or "").casefold()
    for words, profile, topic in ROUTING:
        if profile in roster and any(word in low for word in words):
            return profile, topic
    if roster:
        profile = roster[0]
        return profile, TOPICS.get(profile) or "текущие шаги"
    return lead, "текущие шаги"


def reply_text(text: str, room: dict, detail: dict, *, status: str, urgent: bool = False) -> str:
    """Правдоподобный ответ руководителя: «Принял: … Передал: …»."""
    lead = lead_profile(room, detail)
    profile, topic = route_for(text, room, detail, lead)
    name = NAMES.get(profile) or profile
    prefix = "Пересматриваю оставшиеся шаги, начатую работу не останавливаю. " if urgent else ""
    if status == "done":
        return ("%sПринял: собираю новую версию ответа с учётом уточнения. Передал: %s — %s; "
                "версию {version} пришлю, как только он вернётся с ответом." % (prefix, name, topic))
    if status == "failed":
        return ("%sПринял: повторяем работу с уточнением. Передал: %s — %s; о результате напишу здесь."
                % (prefix, name, topic))
    return ("%sПринял: %s. Передал: %s — %s; остальных не трогаю, работа идёт дальше."
            % (prefix, _short(text), name, topic))


def event_text(status: str) -> str:
    if status == "done":
        return "Версия {version} готова: учтено уточнение Михаила"
    if status == "failed":
        return "Запрос повторён с уточнением Михаила"
    return "Уточнение Михаила принято: руководитель комнаты передал его исполнителю"


def _attach_allowed(path: str, extra_roots=None) -> bool:
    """Путь вложения из разрешённых мест (API §2/§6.3); диск не читаем, только префиксы."""
    from mock_core import FILE_ROOTS, inside
    import os as _os
    try:
        target = _os.path.realpath(str(path))
    except (OSError, ValueError):
        return False
    roots = [root for root in list(FILE_ROOTS) + list(extra_roots or []) if root]
    return any(inside(root, target) for root in roots)


class MockThread:
    """Лента диалога и сообщения руководителю (состояние в памяти мока)."""

    # --- состояние --------------------------------------------------------
    def thread_init(self) -> None:
        self.thread_store = {}      # rid → [ThreadItem] уже доставленные
        self.thread_pending = {}    # rid → [{"due": monotonic, ...}] ждут доставки
        self.thread_seq = 0

    def _delay(self) -> float:
        """3–6 с на демо; в тестах умножается на phase_scale (0.03 → ~0,1 с)."""
        return random.uniform(DELAY_MIN, DELAY_MAX) * max(0.001, float(self.phase_scale))

    # --- лента ------------------------------------------------------------
    def _base_thread(self, rid: str, detail: dict) -> list:
        """Заводская лента: фикстура (если есть) или дерево/хронология живого запроса."""
        stored = self.requests.get(rid)
        if isinstance(stored, dict) and isinstance(stored.get("thread"), list):
            return copy.deepcopy(stored["thread"])
        items = [{"id": "%s:c1" % rid, "at": int(detail.get("created_at") or now()),
                  "author_kind": "mikhail", "author": "mikhail", "author_label": "Михаил",
                  "text": str(detail.get("text") or ""), "task_id": None, "kind": "message"}]
        for node in detail.get("tree") or []:
            for comment in node.get("comments") or []:
                text = str(comment.get("text") or "").strip()
                if not text or text.startswith("[Михаил"):
                    continue
                items.append({"id": "%s:c%s" % (rid, comment.get("id") or len(items)),
                              "at": int(comment.get("at") or detail.get("created_at") or now()),
                              "author_kind": "agent", "author": str(comment.get("author") or ""),
                              "author_label": author_label(comment.get("author")),
                              "text": text, "task_id": node.get("id"), "kind": "comment"})
        for event in detail.get("timeline") or []:
            if event.get("kind") in ("commented", "promoted"):
                continue
            items.append({"id": "%s:e%d" % (rid, len(items)),
                          "at": int(event.get("at") or detail.get("created_at") or now()),
                          "author_kind": "system", "author": "system",
                          "author_label": "Комната команды", "text": str(event.get("text") or ""),
                          "task_id": event.get("task_id"), "kind": "event"})
        seen, out = set(), []
        for item in items:
            key = (item["at"], item["text"])
            if key in seen:
                continue
            seen.add(key)
            out.append(item)
        return out

    def thread_for(self, rid: str, detail: dict | None = None) -> list:
        """Полная лента запроса по времени: заводская + присланные + доставленные."""
        detail = detail if isinstance(detail, dict) else self.detail(rid)
        self._thread_tick(rid)
        items = self._base_thread(rid, detail) + copy.deepcopy(self.thread_store.get(rid) or [])
        items.sort(key=lambda m: (int(m.get("at") or 0), KIND_RANK.get(m.get("kind"), 3),
                                   str(m.get("id") or "")))
        return items

    # --- доставка отложенного ответа --------------------------------------
    def _thread_tick(self, rid: str) -> None:
        pending = self.thread_pending.get(rid) or []
        if not pending:
            return
        mono = time.monotonic()
        due = [p for p in pending if p["due"] <= mono]
        if not due:
            return
        self.thread_pending[rid] = [p for p in pending if p["due"] > mono]
        for pack in sorted(due, key=lambda p: p["due"]):
            version = None
            if pack.get("action") == "revise":
                version = self._apply_revision(rid, pack.get("note") or "")
            elif pack.get("action") == "finish":
                version = self._finish_revision(rid)
            stamp = now()
            for raw in pack["items"]:
                item = copy.deepcopy(raw)
                item["text"] = item["text"].replace("{version}", str(version or 1))
                item["at"] = stamp
                self.thread_store.setdefault(rid, []).append(item)

    def _finish_revision(self, rid: str) -> int:
        """Имитация «доработка закончена»: запрос снова готов, версия N+1 на месте."""
        detail = self.requests.get(rid)
        if not isinstance(detail, dict):
            return int((self.live_detail(rid).get("synthesis") or {}).get("version") or 1)
        version = int((detail.get("synthesis") or {}).get("version") or 1)
        detail.update({"status": "done", "status_reason": None,
                       "finished_at": int(detail.get("finished_at") or now())})
        return version

    def _apply_revision(self, rid: str, text: str) -> int:
        """Существующая имитация «На доработку»: версия N+1. Возвращает номер версии."""
        if rid in self.live:
            req = self.live[rid]
            req.setdefault("revision_notes", []).append(text)
            req["extra_versions"] = int(req.get("extra_versions") or 0) + 1
            self.jump(rid, "synthesizing")
            return 1 + int(req["extra_versions"])
        detail = self.requests.get(rid)
        if isinstance(detail, dict):
            info = self._stored_revise(detail, rid, text)
            return int(info.get("version") or 1)
        return 1

    # --- запись -----------------------------------------------------------
    def post_message(self, rid: str, body: dict) -> dict:
        """`POST /api/requests/{rid}/messages` → {message, thread, routed_to, merged}."""
        detail = self.detail(rid)
        text = body.get("text")
        if text is None:
            raise ApiError(400, "bad_request", "Нужно поле «text» — напишите сообщение", {"field": "text"})
        if not isinstance(text, str):
            raise ApiError(400, "bad_request", "Поле «text» должно быть строкой",
                           {"field": "text", "got": type(text).__name__})
        text = text.strip()
        if not text:
            raise ApiError(400, "bad_request",
                           "Пустое сообщение: напишите, что уточнить или изменить", {"field": "text"})
        if "\x00" in text:
            raise ApiError(400, "bad_request", "В сообщении недопустимый символ", {"field": "text"})
        if len(text) > MAX_TEXT:
            raise ApiError(400, "bad_request", "Сообщение длиннее %d символов" % MAX_TEXT,
                           {"field": "text", "length": len(text), "max": MAX_TEXT})
        attach = body.get("attach")
        if attach is None:
            attach = []
        if not isinstance(attach, list) or any(not isinstance(a, str) for a in attach):
            raise ApiError(400, "bad_request", "Поле «attach» должно быть списком путей",
                           {"field": "attach"})
        if len(attach) > MAX_ATTACH:
            raise ApiError(400, "bad_request", "К одному сообщению не больше %d файлов" % MAX_ATTACH,
                           {"field": "attach", "length": len(attach)})
        for path in attach:
            if not _attach_allowed(path, getattr(self, "extra_roots", None)):
                raise ApiError(403, "forbidden", "Файл вне разрешённых мест — приложите из Vault, "
                                                "папки проекта или рабочей папки",
                               {"field": "attach", "path": path})
        urgent = body.get("urgent")
        if urgent is not None and not isinstance(urgent, bool):
            raise ApiError(400, "bad_request", "Поле «urgent» должно быть true или false",
                           {"field": "urgent", "got": type(urgent).__name__})

        room = self.room(detail["room"])
        lead = lead_profile(room, detail)
        status = str(detail.get("status") or "working")
        merged = bool(self.thread_pending.get(rid))
        self.thread_seq += 1
        message = {"id": "c%d" % self.thread_seq, "at": now(), "author_kind": "mikhail",
                   "author": "mikhail", "author_label": "Михаил", "text": text,
                   "task_id": None, "kind": "message"}
        if attach:
            message["attach"] = list(attach)
        self.thread_store.setdefault(rid, []).append(copy.deepcopy(message))
        self._comment_root(rid, detail, text, urgent=bool(urgent))

        reply = reply_text(text, room, detail, status=status, urgent=bool(urgent))
        added = self.note_message(rid, detail, text)
        if added is not None:
            reply = reply.rstrip(" ") + " Добавил в карту направление «%s» — запущу по вашему «Запустить»." \
                % added["title"]
        event = event_text(status)
        if added is not None:
            event = "Руководитель комнаты добавил направление «%s» в карту проекта" % added["title"]
        role = "orchestrator" if lead in (room.get("orchestrator"), "") else \
            next((m.get("role") for m in room.get("members") or [] if m.get("profile") == lead), "worker")
        self.thread_seq += 1
        items = [
            {"id": "c%d" % self.thread_seq, "author_kind": "agent", "author": lead,
             "author_label": author_label(lead, role), "text": reply, "task_id": rid, "kind": "comment"},
            {"id": "e%d" % self.thread_seq, "author_kind": "system", "author": "system",
             "author_label": "Комната команды", "text": event, "task_id": rid, "kind": "event"},
        ]
        delay = self._delay()
        pack = {"due": time.monotonic() + delay, "items": items}
        packs = [pack]
        if status == "done":
            pack["action"] = "revise"
            pack["note"] = text
            packs.append({"due": time.monotonic() + delay + max(0.5, 6.0 * float(self.phase_scale)),
                          "action": "finish",
                          "items": [{"id": "v%d" % self.thread_seq, "author_kind": "system",
                                     "author": "system", "author_label": "Комната команды",
                                     "text": "Версия {version} отправлена Михаилу: уточнение учтено",
                                     "task_id": rid, "kind": "event"}]})
        # пакетирование: пока ответ не пришёл — одно уточнение, ответ обновляется
        self.thread_pending[rid] = packs
        if status == "failed":
            self._retry_failed(rid, detail)
        current = int((detail.get("synthesis") or {}).get("version") or 0)
        revision = ({"version": current + 1, "previous_version": current, "revision_task_id": None,
                     "note": text} if status == "done" else None)
        return {"message": message, "thread": self.thread_for(rid), "routed_to": lead,
                "merged": merged, "task_id": None, "status": status, "revision": revision,
                "retry": status == "failed", "room": room["id"]}

    def _comment_root(self, rid: str, detail: dict, text: str, *, urgent: bool) -> None:
        """Комментарий на корневую карточку — сразу виден в диалоге и на доске."""
        comment = None
        if rid in self.requests:
            tree = self.requests[rid].get("tree") or []
            if tree:
                self.comment_seq = int(getattr(self, "comment_seq", 0)) + 1
                comment = {"id": self.comment_seq, "author": "mikhail",
                           "text": "%s %s" % (MARK_URGENT if urgent else MARK, text), "at": now()}
                tree[0].setdefault("comments", []).append(comment)
        elif rid in self.live:
            self.comment_seq = int(getattr(self, "comment_seq", 0)) + 1
            comment = {"id": self.comment_seq, "author": "mikhail",
                       "text": "%s %s" % (MARK_URGENT if urgent else MARK, text), "at": now()}
            self.live[rid].setdefault("comments", []).append(comment)
        if comment is not None:
            self.log.append({"at": comment["at"], "action": "message", "task_id": rid, "text": text[:120]})

    def _retry_failed(self, rid: str, detail: dict) -> None:
        """`failed` → «Повторить с уточнением»: упавшие карточки снова готовы."""
        failed = [n for n in detail.get("tree") or [] if n.get("status") == "blocked" and n.get("error")]
        if rid in self.live:
            for node in failed:
                self.live[rid].setdefault("node_patch", {})[node["id"]] = {"status": "ready", "error": None}
            self.live[rid]["force_status"] = "working"
            self.jump(rid, "working")
        elif rid in self.requests:
            for node in self.requests[rid].get("tree") or []:
                if node.get("status") == "blocked" and node.get("error"):
                    node["status"], node["error"] = "ready", None
            self.requests[rid]["status"] = "working"
            self.requests[rid]["status_reason"] = None
