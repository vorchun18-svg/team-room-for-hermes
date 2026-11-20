"""Вложения человека: «файл существует, это файл, читается» (W28-A, дефект 4).

Зачем отдельный модуль
----------------------
До W28 вложение проверялось тем же ``config.path_allowed``, что и ПОКАЗ файлов в
браузере: белый список корней (папка данных, Vault, папки проекта) плюс белый
список расширений. Для показа это защита, а для вложения — тупик: человек сам
выбрал файл в «Загрузках», а комната отвечала «вложение вне разрешённых путей»,
и ``.doc``/``.zip`` прикрепить было нельзя вовсе. При этом агент всё равно читает
файл по полному пути своими инструментами — ограничения Team Room его не
защищают, а человека только раздражают.

Правило W28: **если человек приложил файл — доступ одобрен им**. Проверяем
только то, что действительно мешает работе:

* путь не пустой и без NUL;
* файл существует (иначе — «файла нет по пути …, выберите заново»);
* это файл, а не папка;
* файл читается (занят другой программой / нет прав — «закройте и повторите»).

Единственное исключение — **секреты**: ``.env``, ключи, токены,
``google_token.json``, ``credentials.json``. Их не запрещаем навсегда (человек
решает сам), а требуем явного подтверждения: первый ответ — код
``needs_confirm`` и кнопка «Всё равно прикрепить»; повтор с
``confirm_secret=true`` принимает файл.

Белый список остаётся там, где он и нужен, — в показе файлов (``/api/file``,
``config.path_allowed``). Здесь его нет и не должно быть.

Публично: ``check()``, ``check_all()``, ``is_secret()``, ``AttachmentError``,
``MAX_ATTACH``.
"""
from __future__ import annotations

import os
from pathlib import Path

try:  # пакетный и «плоский» импорт (python server/app.py)
    from .app_common import MAX_ATTACH
except ImportError:  # pragma: no cover
    from app_common import MAX_ATTACH  # type: ignore

#: Файлы, которые почти всегда секреты: имя целиком (регистр не важен).
SECRET_NAMES = frozenset({
    ".env", ".netrc", ".npmrc", ".pgpass", ".htpasswd", ".git-credentials",
    "auth.json", "credentials.json", "google_token.json", "token.json",
    "tokens.json", "secrets.json", "secret.json", "id_rsa", "id_dsa",
    "id_ecdsa", "id_ed25519", "keystore", "wallet.dat",
})
#: Расширения ключей и хранилищ паролей.
SECRET_EXTS = frozenset({".key", ".pem", ".pfx", ".p12", ".keystore", ".jks", ".ppk", ".kdbx"})
#: Части имени, по которым файл «похож на секрет» (нужно подтверждение, не запрет).
SECRET_PARTS = ("secret", "token", "password", "passwd", "credential", "apikey", "api_key")


def is_secret(name) -> bool:
    """Похоже ли имя файла на секрет (``.env``, ``*.key``, ``google_token.json``).

    Это не запрет, а повод спросить: «Всё равно прикрепить». Проверяем ТОЛЬКО имя
    файла: папки вроде ``C:/Проекты/secret/`` — обычные рабочие каталоги.
    """
    low = str(name or "").strip().lower()
    if not low:
        return False
    if low == ".env" or low.startswith(".env."):
        return True
    if low in SECRET_NAMES:
        return True
    if Path(low).suffix in SECRET_EXTS:
        return True
    return any(part in low for part in SECRET_PARTS)


class AttachmentError(Exception):
    """Вложение не принято: машинный код + русский текст с действием.

    ``status`` — HTTP-подсказка (409 у ``needs_confirm``, 400 у остальных),
    ``detail`` — хвост для ``error.detail`` (путь, имя, причина).
    """

    def __init__(self, code: str, message: str, *, path: str = "", status: int = 400,
                 detail: dict | None = None):
        super().__init__(message)
        self.code = str(code or "attach_error")
        self.message = str(message)
        self.path = str(path or "")
        self.status = int(status)
        self.detail = dict(detail or {})
        if self.path:
            self.detail.setdefault("path", self.path)
        self.detail.setdefault("reason", self.code)

    def to_dict(self) -> dict:
        return {"code": self.code, "message": self.message, "path": self.path}


def _fail(code, message, raw="", *, status=400, **detail) -> None:
    raise AttachmentError(code, message, path=raw, status=status, detail=detail)


def check(path, *, confirm_secret: bool = False) -> dict:
    """Проверить одно вложение. Возвращает сведения о файле или бросает AttachmentError.

    Ничего не копирует и не меняет на диске: только ``stat`` и чтение одного байта
    (проверка «читается», а не «есть права на запись»).

    @returns ``{path, name, size, ext, secret}`` — ``path`` уже приведён к
    абсолютному resolved-виду, его и надо отправлять в запросе.
    """
    raw = "" if path is None else str(path).strip()
    if not raw:
        _fail("empty_path", "Путь вложения пуст — выберите файл кнопкой «Прикрепить файлы» "
                            "или вставьте путь в поле.", raw)
    if "\x00" in raw:
        _fail("bad_path", "В пути вложения непечатаемый символ — скопируйте путь заново.", raw)
    try:
        resolved = Path(raw).expanduser().resolve(strict=True)
    except FileNotFoundError:
        _fail("not_found", "Недоступно: файла нет по пути «%s». Проверьте путь или выберите "
                           "файл заново." % raw, raw)
    except PermissionError:
        _fail("denied", "Недоступно: нет прав на «%s». Выберите файл из своей папки или "
                        "закройте программу, которая держит папку." % raw, raw)
    except (OSError, RuntimeError, ValueError) as exc:
        _fail("bad_path", "Недоступно: путь «%s» не читается (%s). Скопируйте путь заново."
              % (raw, str(exc)[:120] or "некорректный путь"), raw)
    try:
        is_file = resolved.is_file()
    except OSError as exc:  # pragma: no cover - редкий сбой файловой системы
        _fail("unreadable", "Недоступно: «%s» не проверяется (%s). Повторите позже."
              % (raw, str(exc)[:120]), raw)
    if not is_file:
        _fail("not_file", "Недоступно: «%s» — это папка, а не файл. Выберите файл." % raw, raw)
    try:
        size = int(resolved.stat().st_size)
    except PermissionError:
        _fail("busy", "Недоступно: «%s» занят другой программой — закройте её и повторите."
              % resolved.name, raw)
    except OSError as exc:
        _fail("unreadable", "Недоступно: размер «%s» не читается (%s). Повторите позже."
              % (resolved.name, str(exc)[:120]), raw)
    try:
        with open(resolved, "rb") as handle:
            handle.read(1)
    except PermissionError:
        _fail("busy", "Недоступно: «%s» занят другой программой или закрыт для чтения — "
                      "закройте файл и повторите." % resolved.name, raw)
    except OSError as exc:
        _fail("unreadable", "Недоступно: «%s» не читается (%s). Закройте программу, которая "
                            "держит файл, и повторите." % (resolved.name, str(exc)[:120]), raw)
    secret = is_secret(resolved.name)
    info = {"path": str(resolved), "name": resolved.name, "size": size,
            "ext": resolved.suffix.lower(), "secret": secret}
    if secret and not confirm_secret:
        raise AttachmentError(
            "needs_confirm",
            "«%s» похож на секрет. Если он правда нужен команде — нажмите «Всё равно прикрепить»."
            % resolved.name, path=raw, status=409,
            detail={"path": raw, "name": resolved.name, "size": size})
    return info


def check_all(attach, confirm_secret: bool = False, *, limit: int | None = None) -> list:
    """Проверить список вложений: вернуть resolved-пути строками (для API и канбана).

    Повторы отбрасываются (Windows сравнивает без учёта регистра), порядок
    сохраняется. Бросает ``AttachmentError`` на первом же непригодном файле —
    причина всегда с действием.
    """
    if attach in (None, ""):
        return []
    if isinstance(attach, (str, bytes)) or not isinstance(attach, (list, tuple)):
        raise AttachmentError("bad_attach", "attach: список путей к файлам.",
                             detail={"field": "attach"})
    cap = MAX_ATTACH if limit is None else int(limit)
    if len(attach) > cap:
        raise AttachmentError("too_many", "attach: не больше %d путей — уберите лишние."
                             % cap, detail={"count": len(attach), "limit": cap})
    out, seen = [], set()
    for item in attach:
        info = check(item, confirm_secret=confirm_secret)
        key = os.path.normcase(info["path"])
        if key in seen:
            continue
        seen.add(key)
        out.append(info["path"])
    return out


__all__ = ["AttachmentError", "MAX_ATTACH", "SECRET_EXTS", "SECRET_NAMES", "SECRET_PARTS",
           "check", "check_all", "is_secret"]
