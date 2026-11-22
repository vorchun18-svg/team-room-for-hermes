"""Вложения с пониманием: предпросмотр, расшифровка голосовых, OCR (W29B-Q3).

Зачем
-----
Вложение в запрос раньше было только путём: человек видел имя файла и кнопку
«Открыть» — то есть уходил в другую программу, чтобы понять, что он вообще
принёс. Для голосового сообщения (ogg/mp3/wav/m4a/opus) не было ничего: чтобы
узнать, что там наговорено, нужно было слушать. Здесь — чтение вложения прямо
в панели приложения:

* изображения, PDF — браузерным просмотром (`/api/attach/file` отдаёт байты);
* текст / markdown / csv / tsv — текстом и таблицей;
* docx / xlsx / pptx — извлечённый ТЕКСТ локально (`zipfile` + `xml.etree`,
  ничего не устанавливаем и никуда не отправляем);
* голосовые — расшифровка штатным STT приложения (`server/stt.py`,
  faster-whisper в отдельном процессе); в песочнице и тестах —
  `TEAMROOM_STT_FAKE` (путь к файлу с текстом или «1» — фиксированный текст);
* OCR сканов и картинок — ТОЛЬКО если в системе найден `tesseract`; иначе
  кнопки нет и ничего не устанавливается (`TEAMROOM_OCR_FAKE` — песочница).

Безопасность
------------
* только чтение; ничего не пишем рядом с файлом;
* путь разрешается `Path.resolve(strict=True)`, проверяются NUL, ADS-потоки,
  секретные имена (`.env`, `*.pem`, `*token*`), каталоги Hermes/программы/системы;
* символическая ссылка, ведущая из разрешённого корня наружу, не разворачивается:
  исходный путь и цель обязаны лежать в одном разрешённом корне;
* архивы (docx/xlsx/pptx) читаются В ПАМЯТИ — ничего не распаковываем на диск,
  записи с `..` и абсолютными именами пропускаются (zip-slip);
* пределы: размер файла, объём текста, число строк таблицы, число страниц,
  таймауты OCR и STT.

Публично: ``kind_of``, ``resolve_file``, ``preview``, ``file_bytes``,
``start_transcribe``, ``transcribe_job``, ``run_ocr``, ``capabilities``,
``AttachError``, ``KINDS``.
"""
from __future__ import annotations

import csv
import io
import os
import re
import shutil
import subprocess
import threading
import time
import uuid
import zipfile
import xml.etree.ElementTree as ET
from pathlib import Path


def _proc_run(argv, **kw):
    """Запуск процесса только через server/proc.py (без мигающего окна)."""
    try:
        from . import proc as proc_mod
    except ImportError:             # запуск как скрипт
        import proc as proc_mod     # type: ignore
    return proc_mod.run(argv, **kw)

try:  # пакетный и «плоский» импорт
    from . import config as config_mod
    from . import stt as stt_mod
except ImportError:  # pragma: no cover
    import config as config_mod           # type: ignore
    import stt as stt_mod                 # type: ignore

#: Пределы: файл целиком не читаем «в текст», только окно.
MAX_PREVIEW_BYTES = 32 * 1024 * 1024     # больше — «Открыть», без разбора
MAX_TEXT_BYTES = 4 * 1024 * 1024         # сколько байт читаем из текстового файла
MAX_TEXT_CHARS = 300_000                 # сколько знаков отдаём интерфейсу
MAX_TABLE_ROWS = 500                     # строк таблицы в ответе
MAX_TABLE_COLS = 64                      # колонок
MAX_ARCHIVE_ENTRY = 24 * 1024 * 1024     # запись внутри docx/xlsx/pptx
MAX_SLIDES = 120
MAX_PDF_BYTES = 50 * 1024 * 1024
MAX_AUDIO_BYTES = getattr(stt_mod, "MAX_AUDIO", 64 * 1024 * 1024)
MAX_OCR_BYTES = 16 * 1024 * 1024
OCR_TIMEOUT = 90.0
#: Сколько живёт завершённая задача расшифровки в памяти и сколько задач держим.
JOB_TTL = 1800.0
JOB_KEEP = 20
#: Переменные песочницы: настоящие STT/OCR в тестах не запускаются.
STT_FAKE_ENV = "TEAMROOM_STT_FAKE"
OCR_FAKE_ENV = "TEAMROOM_OCR_FAKE"
OCR_LANG_RE = re.compile(r"^[a-zA-Z+_]{2,40}$")
FAKE_TRANSCRIPT = "Это расшифровка голосового сообщения (песочница)."

KINDS = ("image", "pdf", "text", "table", "docx", "xlsx", "pptx", "audio", "other")

TEXT_EXT = frozenset({".md", ".markdown", ".txt", ".log", ".json", ".jsonl", ".yaml", ".yml",
                      ".xml", ".ics", ".html", ".htm", ".css", ".js", ".mjs", ".py", ".sh",
                      ".ini", ".conf", ".cfg", ".csv", ".tsv"})
TABLE_EXT = frozenset({".csv", ".tsv"})
IMAGE_EXT = frozenset({".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".tif", ".tiff"})
AUDIO_EXT = frozenset({".ogg", ".mp3", ".wav", ".m4a", ".opus", ".webm", ".flac"})
OFFICE_KIND = {".docx": "docx", ".xlsx": "xlsx", ".pptx": "pptx"}

#: Content-Type для отдачи байтов (у `files.MIME` нет голосовых).
MIME = {
    ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".gif": "image/gif",
    ".webp": "image/webp", ".bmp": "image/bmp", ".tif": "image/tiff", ".tiff": "image/tiff",
    ".pdf": "application/pdf",
    ".ogg": "audio/ogg", ".opus": "audio/ogg", ".mp3": "audio/mpeg", ".wav": "audio/wav",
    ".m4a": "audio/mp4", ".flac": "audio/flac", ".webm": "audio/webm",
}
#: MIME для STT: `stt.transcribe` принимает только эти типы.
STT_MIME = {".ogg": "audio/ogg", ".opus": "audio/ogg", ".mp3": "audio/mpeg",
            ".wav": "audio/wav", ".m4a": "audio/mp4", ".webm": "audio/webm",
            ".flac": "audio/wav"}
PLAIN_EXT = frozenset({".html", ".htm", ".svg"})


class AttachError(Exception):
    """Предпросмотр невозможен: машинный код, HTTP-подсказка и русский текст с действием."""

    def __init__(self, code: str, message: str, status: int = 400, **detail):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status
        self.detail = detail

    def to_dict(self) -> dict:
        return {"code": self.code, "message": self.message, "detail": dict(self.detail)}


# ─────────────────────────────────────────────────────────────── вид и путь

def kind_of(path) -> str:
    """Вид вложения по расширению: image / pdf / text / table / docx / xlsx / pptx / audio / other."""
    ext = Path(str(path or "")).suffix.lower()
    if ext in IMAGE_EXT:
        return "image"
    if ext == ".pdf":
        return "pdf"
    if ext in OFFICE_KIND:
        return OFFICE_KIND[ext]
    if ext in TABLE_EXT:
        return "table"
    if ext in AUDIO_EXT:
        return "audio"
    if ext in TEXT_EXT:
        return "text"
    return "other"


def _suspicious(raw: str) -> bool:
    """Обход путей: NUL, ADS-потоки (`file.txt:stream`), запрещённые Windows-символы."""
    if not raw or "\x00" in raw or "::" in raw:
        return True
    rest = raw[2:] if len(raw) > 1 and raw[1] == ":" else raw
    if ":" in rest:
        return True
    return any(ch in raw for ch in '<>"|?*')


def _under(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except (ValueError, OSError):
        return False


def _forbidden_dirs(cfg) -> list:
    """Каталоги, из которых предпросмотр не показываем никогда.

    Системные каталоги Windows, конфиги Hermes и профили, каталог программы,
    журналы и состояние приложения: там секреты из argv/stderr и служебные файлы.
    Отдельно добавляем то, что уже запрещено показу файлов (`project_dir`
    `_forbidden_roots`). Сам `data_dir` не запрещён — в нём лежат отчёты и
    материалы, которые показывать как раз нужно.
    """
    out = []
    candidates: list = []
    try:
        candidates += [str(root) for root in config_mod._forbidden_dirs(cfg)]   # noqa: SLF001
    except Exception:                                                          # noqa: BLE001
        candidates += [r"C:\Windows", r"C:\Program Files", r"C:\Program Files (x86)",
                       r"C:\ProgramData"]
    data_dir = getattr(cfg, "data_dir", None)
    try:
        data_path = Path(data_dir).resolve() if data_dir else None
    except (OSError, ValueError):
        data_path = None
    for value in (getattr(cfg, "hermes_home", None), getattr(cfg, "app_dir", None),
                  getattr(cfg, "log_dir", None), getattr(cfg, "state_dir", None)):
        if value:
            candidates.append(str(value))
    for raw in candidates:
        try:
            path = Path(raw).resolve()
        except (OSError, ValueError, RuntimeError):
            continue
        # `data_dir` держит отчёты и материалы комнаты — их показывать нужно.
        if data_path is not None and path == data_path:
            continue
        if path not in out:
            out.append(path)
    return out


def resolve_file(cfg, path, roots=None) -> Path:
    """Разрешённый путь вложения или ``AttachError`` с причиной и действием.

    ``roots`` — корни комнаты (папка проекта, зеркало); без них — общие корни
    приложения. Файл ВНЕ корней (человек прикрепил его из «Загрузок») показываем
    так же, как «Открыть»: он его сам выбрал; запрещены только секреты,
    служебные каталоги и подмена через символическую ссылку.
    """
    raw = "" if path is None else str(path).strip()
    if not raw:
        raise AttachError("path_required", "Не указан путь к файлу.", 400)
    if _suspicious(raw):
        raise AttachError("bad_path", "Путь содержит недопустимые символы.", 403, path=raw)
    try:
        target = Path(raw).resolve(strict=True)
    except (OSError, RuntimeError, ValueError):
        raise AttachError("not_found", "Файл не найден — возможно, его удалили или переместили.",
                          404, path=raw) from None
    try:
        if not target.is_file():
            raise AttachError("not_file", "Это папка, а не файл — выберите файл.", 400, path=raw)
    except OSError:
        raise AttachError("not_found", "Файл не найден — возможно, его удалили или переместили.",
                          404, path=raw) from None
    if target.suffix.lower() == ".pem" or config_mod.is_denied_name(target.name):
        raise AttachError("secret", "Файл похож на секрет (ключ или пароль) — предпросмотр не показываем.",
                          403, path=raw)
    for forbidden in _forbidden_dirs(cfg):
        if _under(target, forbidden):
            raise AttachError("forbidden_dir",
                              "Файл лежит в служебном каталоге приложения или Hermes — "
                              "предпросмотр не показываем.", 403, path=raw)
    # Символическая ссылка не должна уводить за пределы выбранного файла:
    # если исходный путь был внутри разрешённого корня, цель обязана быть там же.
    allowed = [Path(r).resolve() for r in (roots if roots is not None else cfg.allowed_roots)
               if r]
    try:
        literal = Path(os.path.abspath(raw))
    except (OSError, ValueError):
        literal = None
    for root in allowed:
        if literal is not None and _under(literal, root) and not _under(target, root):
            raise AttachError("symlink_escape",
                              "Ссылка ведёт за пределы файла, который вы выбрали, — "
                              "предпросмотр не показываем.", 403, path=raw)
    return target


def file_bytes(cfg, path, roots=None) -> tuple:
    """Байты файла и Content-Type для браузерного просмотра (img/pdf/audio)."""
    target = resolve_file(cfg, path, roots=roots)
    try:
        size = target.stat().st_size
    except OSError:
        raise AttachError("not_found", "Файл не найден", 404, path=str(target)) from None
    limit = MAX_PDF_BYTES if target.suffix.lower() == ".pdf" else MAX_PREVIEW_BYTES
    if size > limit:
        raise AttachError("too_big",
                          "Файл больше %d МБ — предпросмотр не строим, откройте его программой."
                          % (limit // (1024 * 1024)), 413,
                          path=str(target), size=size, limit=limit)
    try:
        data = target.read_bytes()
    except OSError as exc:
        raise AttachError("read_failed",
                          "Файл занят другой программой или нет прав на чтение — "
                          "закройте его и повторите.", 409, path=str(target),
                          reason=str(exc)[:120]) from None
    ext = target.suffix.lower()
    return data, MIME.get(ext, "application/octet-stream")


# ──────────────────────────────────────────────────────────── чтение текста

def _decode(data: bytes) -> tuple:
    """``(text, encoding)``: utf-8 с BOM, затем cp1251, затем latin-1 — без падений."""
    for enc in ("utf-8-sig", "utf-8", "cp1251", "latin-1"):
        try:
            return data.decode(enc), enc
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", "replace"), "utf-8/replace"


def _read_window(path: Path, limit: int) -> bytes:
    try:
        with path.open("rb") as fh:
            return fh.read(limit)
    except OSError as exc:
        raise AttachError("read_failed",
                          "Файл занят другой программой или нет прав на чтение — "
                          "закройте его и повторите.", 409,
                          path=str(path), reason=str(exc)[:120]) from None


def _text_part(path: Path) -> dict:
    data = _read_window(path, MAX_TEXT_BYTES)
    text, enc = _decode(data)
    total_lines = text.count("\n") + (1 if text and not text.endswith("\n") else 0)
    truncated = len(text) > MAX_TEXT_CHARS
    return {"text": text[:MAX_TEXT_CHARS], "encoding": enc,
            "truncated": truncated, "chars": len(text), "lines": int(total_lines)}


def _sniff_delimiter(text: str) -> str:
    first = text.splitlines()[0] if text else ""
    best, best_count = ",", 0
    for cand in (",", ";", "\t", "|"):
        count = first.count(cand)
        if count > best_count:
            best, best_count = cand, count
    return best


def _table_part(path: Path) -> dict:
    data = _read_window(path, MAX_TEXT_BYTES)
    text, enc = _decode(data)
    delimiter = _sniff_delimiter(text)
    rows: list = []
    total = 0
    try:
        reader = csv.reader(io.StringIO(text), delimiter=delimiter)
        for row in reader:
            total += 1
            if len(rows) <= MAX_TABLE_ROWS:
                rows.append([str(cell)[:500] for cell in row[:MAX_TABLE_COLS]])
    except csv.Error as exc:
        raise AttachError("bad_table", "Не удалось прочитать таблицу: %s" % str(exc)[:120],
                          422, path=str(path)) from None
    header = rows[0] if rows else []
    body = rows[1:] if rows else []
    return {"table": {"columns": header, "rows": body, "delimiter": delimiter,
                      "total_rows": max(0, total - 1),
                      "truncated": total - 1 > len(body)},
            "encoding": enc}


def _zip_read(path: Path, name: str) -> bytes | None:
    """Прочитать запись архива в память; опасные имена и гигантские записи — пропустить."""
    try:
        with zipfile.ZipFile(path) as zf:
            for info in zf.infolist():
                if info.filename != name:
                    continue
                if info.file_size > MAX_ARCHIVE_ENTRY:
                    return None
                normal = info.filename.replace("\\", "/")
                if normal.startswith("/") or ".." in normal.split("/"):
                    return None
                with zf.open(info) as fh:
                    return fh.read(MAX_ARCHIVE_ENTRY)
    except (zipfile.BadZipFile, OSError, KeyError):
        raise AttachError("bad_office", "Файл повреждён или это не документ Office — "
                                        "откройте его программой.", 422, path=str(path)) from None
    return None


def _docx_part(path: Path) -> dict:
    xml = _zip_read(path, "word/document.xml")
    if not xml:
        raise AttachError("bad_office", "В документе нет текста — откройте его программой.",
                          422, path=str(path))
    ns = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
    try:
        root = ET.fromstring(xml)
    except ET.ParseError:
        raise AttachError("bad_office", "Документ не читается — откройте его программой.",
                          422, path=str(path)) from None
    paragraphs = []
    for para in root.iter(ns + "p"):
        line = "".join((t.text or "") for t in para.iter(ns + "t")).strip()
        if line:
            paragraphs.append(line)
    text = "\n".join(paragraphs)
    return {"text": text[:MAX_TEXT_CHARS], "paragraphs": len(paragraphs),
            "truncated": len(text) > MAX_TEXT_CHARS, "chars": len(text)}


def _xlsx_part(path: Path) -> dict:
    ns = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
    shared_xml = _zip_read(path, "xl/sharedStrings.xml")
    shared: list = []
    if shared_xml:
        try:
            sroot = ET.fromstring(shared_xml)
            for item in sroot.iter(ns + "si"):
                shared.append("".join(t.text or "" for t in item.iter(ns + "t")))
        except ET.ParseError:
            shared = []
    try:
        with zipfile.ZipFile(path) as zf:
            sheets = sorted(name for name in zf.namelist()
                            if re.fullmatch(r"xl/worksheets/sheet\d+\.xml", name))
    except (zipfile.BadZipFile, OSError):
        raise AttachError("bad_office", "Книга не читается — откройте её программой.",
                          422, path=str(path)) from None
    if not sheets:
        raise AttachError("bad_office", "В книге нет листов — откройте её программой.",
                          422, path=str(path))
    sheet_xml = _zip_read(path, sheets[0])
    try:
        root = ET.fromstring(sheet_xml or b"<x/>")
    except ET.ParseError:
        raise AttachError("bad_office", "Книга не читается — откройте её программой.",
                          422, path=str(path)) from None

    def col_index(ref: str) -> int:
        letters = re.sub(r"[^A-Z]", "", (ref or "").upper()) or "A"
        value = 0
        for ch in letters:
            value = value * 26 + (ord(ch) - 64)
        return max(0, value - 1)

    grid: list = []
    for row in root.iter(ns + "row"):
        cells: dict = {}
        for cell in row.iter(ns + "c"):
            idx = col_index(cell.get("r") or "")
            if idx >= MAX_TABLE_COLS:
                continue
            kind = cell.get("t")
            if kind == "inlineStr":
                value = "".join(t.text or "" for t in cell.iter(ns + "t"))
            else:
                node = cell.find(ns + "v")
                value = node.text if node is not None else ""
                if kind == "s" and value:
                    try:
                        value = shared[int(value)]
                    except (ValueError, IndexError):
                        pass
            cells[idx] = str(value or "")[:500]
        if cells:
            width = max(cells) + 1
            grid.append([cells.get(i, "") for i in range(width)])
        if len(grid) > MAX_TABLE_ROWS + 1:
            break
    header = grid[0] if grid else []
    body = grid[1:] if grid else []
    return {"table": {"columns": header, "rows": body, "sheet": 1,
                      "total_rows": max(0, len(grid) - 1),
                      "truncated": len(grid) > MAX_TABLE_ROWS + 1}}


def _pptx_part(path: Path) -> dict:
    ns = "{http://schemas.openxmlformats.org/drawingml/2006/main}"
    try:
        with zipfile.ZipFile(path) as zf:
            names = [name for name in zf.namelist()
                     if re.fullmatch(r"ppt/slides/slide\d+\.xml", name)]
    except (zipfile.BadZipFile, OSError):
        raise AttachError("bad_office", "Презентация не читается — откройте её программой.",
                          422, path=str(path)) from None
    names.sort(key=lambda name: int(re.search(r"(\d+)\.xml$", name).group(1)))
    slides = []
    for index, name in enumerate(names[:MAX_SLIDES], start=1):
        xml = _zip_read(path, name) or b"<x/>"
        try:
            root = ET.fromstring(xml)
        except ET.ParseError:
            continue
        lines = ["".join(t.text or "" for t in para.iter(ns + "t")).strip()
                 for para in root.iter(ns + "p")]
        lines = [line for line in lines if line]
        if lines:
            slides.append({"number": index, "text": "\n".join(lines)})
    text = "\n\n".join("Слайд %d\n%s" % (s["number"], s["text"]) for s in slides)
    return {"slides": slides, "text": text[:MAX_TEXT_CHARS],
            "truncated": len(text) > MAX_TEXT_CHARS, "count": len(names)}


# ─────────────────────────────────────────────────────────────── OCR и STT

_OCR_CACHE: dict = {"at": 0.0, "path": None}


def ocr_path() -> str | None:
    """Путь к `tesseract` или None (кэш 60 с). В песочнице — `TEAMROOM_OCR_FAKE`."""
    fake = os.environ.get(OCR_FAKE_ENV)
    if fake and fake.strip() not in ("", "0", "off"):
        return "fake"
    now = time.monotonic()
    if now - float(_OCR_CACHE.get("at") or 0) < 60.0:
        return _OCR_CACHE.get("path")
    found = shutil.which("tesseract")
    _OCR_CACHE.update({"at": now, "path": found})
    return found


def capabilities() -> dict:
    """Что умеет эта машина: OCR (tesseract), STT (штатный модуль приложения)."""
    ocr = ocr_path()
    stt_state = {}
    try:
        stt_state = {"available": True, "mode": "fake" if _stt_fake() else "app"}
    except Exception:                                    # noqa: BLE001
        stt_state = {"available": False, "reason": "stt недоступен"}
    return {"ocr": {"available": bool(ocr), "engine": "tesseract" if ocr else "",
                    "reason": "" if ocr else "tesseract не найден — распознавание сканов недоступно"},
            "stt": stt_state}


def _stt_fake() -> str:
    return str(os.environ.get(STT_FAKE_ENV) or "").strip()


def _fake_text(value: str) -> str:
    """Текст из `TEAMROOM_STT_FAKE`: файл с текстом либо фиксированная строка."""
    if value and value not in ("1", "on", "yes", "true"):
        try:
            candidate = Path(value)
            if candidate.is_file():
                return candidate.read_text(encoding="utf-8", errors="replace")
        except (OSError, ValueError):
            pass
    return FAKE_TRANSCRIPT


def run_ocr(path, lang: str = "rus+eng") -> dict:
    """OCR картинки через tesseract (если найден). Только чтение, без установки."""
    engine = ocr_path()
    if not engine:
        raise AttachError("ocr_unavailable",
                          "Распознавание сканов недоступно: в системе нет tesseract.",
                          503, reason="tesseract_not_found")
    clean_lang = str(lang or "rus+eng").strip()
    if not OCR_LANG_RE.fullmatch(clean_lang):
        raise AttachError("bad_lang", "Некорректный язык распознавания.", 400, lang=clean_lang)
    raw = "" if path is None else str(path).strip()
    if _suspicious(raw):
        raise AttachError("bad_path", "Путь содержит недопустимые символы.", 403, path=raw)
    try:
        target = Path(raw).resolve(strict=True)
    except (OSError, RuntimeError, ValueError):
        raise AttachError("not_found", "Файл не найден", 404, path=raw) from None
    try:
        size = target.stat().st_size
    except OSError:
        raise AttachError("not_found", "Файл не найден", 404, path=raw) from None
    if size > MAX_OCR_BYTES:
        raise AttachError("too_big", "Картинка больше %d МБ — распознавание не запускаем."
                          % (MAX_OCR_BYTES // (1024 * 1024)), 413, path=str(target))
    if engine == "fake":
        return {"text": _fake_text(str(os.environ.get(OCR_FAKE_ENV) or "")),
                "engine": "fake", "chars": 0, "seconds": 0.0}
    started = time.monotonic()
    try:
        proc = _proc_run([engine, str(target), "stdout", "-l", clean_lang],
                         capture_output=True, timeout=OCR_TIMEOUT, check=False)
    except subprocess.TimeoutExpired:
        raise AttachError("ocr_timeout",
                          "Распознавание не уложилось в %d с — попробуйте файл поменьше."
                          % int(OCR_TIMEOUT), 504, path=str(target)) from None
    except OSError as exc:
        raise AttachError("ocr_failed", "Не удалось запустить распознавание: %s" % str(exc)[:120],
                          500, path=str(target)) from None
    if proc.returncode != 0:
        tail = (proc.stderr or b"").decode("utf-8", "replace").strip().splitlines()[-1:]
        raise AttachError("ocr_failed",
                          "Распознавание не удалось: %s" % (tail[0][:160] if tail else "tesseract вернул ошибку"),
                          500, path=str(target))
    text, _enc = _decode(proc.stdout or b"")
    text = text.strip()
    return {"text": text[:MAX_TEXT_CHARS], "engine": "tesseract", "lang": clean_lang,
            "chars": len(text), "truncated": len(text) > MAX_TEXT_CHARS,
            "seconds": round(time.monotonic() - started, 2)}


#: Задачи расшифровки: длинное голосовое распознаётся в фоне, панель показывает прогресс.
_JOBS: dict = {}
_JOBS_LOCK = threading.Lock()


def _prune_jobs() -> None:
    now = time.time()
    with _JOBS_LOCK:
        dead = [key for key, job in _JOBS.items()
                if job.get("state") != "running" and now - float(job.get("updated") or 0) > JOB_TTL]
        for key in dead:
            _JOBS.pop(key, None)
        if len(_JOBS) > JOB_KEEP:
            for key in sorted(_JOBS, key=lambda k: float(_JOBS[k].get("updated") or 0))[:len(_JOBS) - JOB_KEEP]:
                _JOBS.pop(key, None)


def _duration_estimate(size: int, ext: str) -> float:
    try:
        return float(stt_mod._estimate_duration(size, ext))     # noqa: SLF001 — общая оценка
    except Exception:                                            # noqa: BLE001
        return round(size / 16000.0, 1)                          # ≈128 кбит/с


def _transcribe_worker(cfg, job_id: str, target: Path, size: int) -> None:
    """Фоновое распознавание: обновляет прогресс задачи, ничего не пишет на диск."""
    ext = target.suffix.lower()
    fake = _stt_fake()
    try:
        if fake:
            text = _fake_text(fake)
            for progress in (0.25, 0.6):
                time.sleep(0.05)
                _job_update(job_id, progress=progress)
            _job_done(job_id, text, model="песочница", duration_s=0.0)
            return
        data = target.read_bytes()
        expected = max(3.0, _duration_estimate(size, ext) / 3.0)
        started = time.monotonic()
        stop = threading.Event()

        def tick() -> None:
            while not stop.wait(0.5):
                _job_update(job_id, progress=min(0.9, (time.monotonic() - started) / expected))

        ticker = threading.Thread(target=tick, name="attach-stt-tick", daemon=True)
        ticker.start()
        try:
            result = stt_mod.transcribe(cfg, data, STT_MIME.get(ext, "audio/ogg"))
        finally:
            stop.set()
        _job_done(job_id, str(result.get("text") or ""),
                  model=str(result.get("model") or ""),
                  duration_s=float(result.get("duration_s") or 0.0))
    except AttachError as exc:
        _job_fail(job_id, exc.message, exc.code)
    except Exception as exc:                                     # noqa: BLE001
        _job_fail(job_id, "Не удалось расшифровать голосовое: %s" % str(exc)[:200], "stt_failed")


def _job_update(job_id: str, **fields) -> None:
    with _JOBS_LOCK:
        job = _JOBS.get(job_id)
        if job is None:
            return
        job.update(fields)
        job["updated"] = time.time()


def _job_done(job_id: str, text: str, **extra) -> None:
    _job_update(job_id, state="done", progress=1.0, text=text, chars=len(text), **extra)


def _job_fail(job_id: str, message: str, code: str) -> None:
    _job_update(job_id, state="error", progress=1.0, message=message, code=code)


def start_transcribe(cfg, path, roots=None) -> dict:
    """Запустить расшифровку голосового в фоне. Возвращает ``{job_id, state, ...}``."""
    _prune_jobs()
    target = resolve_file(cfg, path, roots=roots)
    ext = target.suffix.lower()
    if ext not in AUDIO_EXT:
        raise AttachError("not_audio", "Это не голосовое сообщение — расшифровка не нужна.", 400,
                          path=str(target), ext=ext)
    try:
        size = target.stat().st_size
    except OSError:
        raise AttachError("not_found", "Файл не найден", 404, path=str(target)) from None
    if size <= 0:
        raise AttachError("audio_empty", "Пустое аудио: в файле нет данных.", 400, path=str(target))
    if size > MAX_AUDIO_BYTES:
        raise AttachError("audio_too_big",
                          "Аудио больше %d МБ — расшифровку не запускаем."
                          % (MAX_AUDIO_BYTES // (1024 * 1024)), 413, path=str(target))
    job_id = uuid.uuid4().hex[:12]
    now = time.time()
    with _JOBS_LOCK:
        _JOBS[job_id] = {"job_id": job_id, "state": "running", "progress": 0.0,
                         "path": str(target), "name": target.name, "size": size,
                         "duration_s": _duration_estimate(size, ext),
                         "created": now, "updated": now, "text": ""}
    threading.Thread(target=_transcribe_worker, args=(cfg, job_id, target, size),
                     name="attach-stt", daemon=True).start()
    return transcribe_job(job_id)


def transcribe_job(job_id) -> dict:
    """Состояние задачи расшифровки: ``running`` / ``done`` / ``error`` + текст."""
    with _JOBS_LOCK:
        job = _JOBS.get(str(job_id or ""))
        if job is None:
            raise AttachError("job_not_found", "Расшифровка не найдена — запустите её заново.",
                              404, job=str(job_id or ""))
        return dict(job)


# ────────────────────────────────────────────────────────────────── превью

def preview(cfg, path, roots=None, room: str = "") -> dict:
    """Всё, что нужно панели предпросмотра: вид, имя, размер и содержимое.

    Для картинок/PDF/аудио содержимое — ссылка ``/api/attach/file``; текст,
    таблицы и Office — извлечённые данные; голосовые — признак доступности STT.
    """
    target = resolve_file(cfg, path, roots=roots)
    ext = target.suffix.lower()
    kind = kind_of(target)
    try:
        stat = target.stat()
    except OSError:
        raise AttachError("not_found", "Файл не найден", 404, path=str(target)) from None
    out = {"ok": True, "path": str(target), "name": target.name, "ext": ext, "kind": kind,
           "size": int(stat.st_size), "mtime": int(stat.st_mtime),
           "mime": MIME.get(ext, "application/octet-stream"),
           "room": str(room or ""), "truncated": False,
           "caps": capabilities()}
    if stat.st_size > MAX_PREVIEW_BYTES and kind != "pdf":
        out.update({"unavailable": "too_big", "note":
                    "Файл больше %d МБ — предпросмотр не строим, откройте его программой."
                    % (MAX_PREVIEW_BYTES // (1024 * 1024))})
        return out
    if kind in ("image", "pdf", "audio"):
        out["url"] = "/api/attach/file?path=%s%s" % (
            _q(str(target)), ("&room=" + _q(room)) if room else "")
        if kind == "audio":
            out["duration_s"] = _duration_estimate(int(stat.st_size), ext)
            out["stt"] = {"fake": bool(_stt_fake())}
        if kind == "image":
            out["ocr"] = {"available": bool(ocr_path())}
        return out
    if kind == "text":
        out.update(_text_part(target))
        return out
    if kind == "table":
        out.update(_table_part(target))
        out["truncated"] = bool(out.get("table", {}).get("truncated"))
        return out
    if kind == "docx":
        part = _docx_part(target)
    elif kind == "xlsx":
        part = _xlsx_part(target)
    elif kind == "pptx":
        part = _pptx_part(target)
    else:
        out.update({"unavailable": "unsupported",
                    "note": "Для этого типа предпросмотра нет — откройте файл программой."})
        return out
    out.update(part)
    out["truncated"] = bool(part.get("truncated"))
    return out


def _q(value: str) -> str:
    from urllib.parse import quote
    return quote(str(value), safe="")


__all__ = ["KINDS", "MIME", "AttachError", "kind_of", "resolve_file", "file_bytes", "preview",
           "start_transcribe", "transcribe_job", "run_ocr", "capabilities", "ocr_path",
           "MAX_TEXT_CHARS", "MAX_TABLE_ROWS", "MAX_PREVIEW_BYTES",
           "STT_FAKE_ENV", "OCR_FAKE_ENV"]
