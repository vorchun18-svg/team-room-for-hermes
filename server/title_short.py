"""Короткое название запроса (W17-B §4, аудит B9): «≤ 60 знаков по-человечески».

Заголовок карточки в канбане — первая строка диктовки («Так, начнём работу в этой
комнате. Работа будет построена следующим образом. Для…»). Показывать это в меню,
крошках и списках нельзя: пользователь не узнаёт запрос.

Правило источников:

1. ``metadata.title`` руководителя (≤ 60 знаков) — он разбирает запрос и лучше всех
   знает короткое имя;
2. первое предложение текста без вводных «Так,», «Итак,», «В общем,» и markdown;
3. фрагмент до 60 знаков по границе слова с многоточием.

Модуль чистый: ни файлов, ни сети. Используется чтением (``engine_read``) и
локальной сводкой (``app._local_summary``), поэтому одинаков в любом пути.
"""
from __future__ import annotations

import re

#: сколько знаков в коротком названии (контракт W17-B, docs/API.md §3.3)
TITLE_LIMIT = 60

#: вводные слова диктовки, которые в названии только мешают
_FILLERS = ("так", "итак", "в общем", "в целом", "ну", "значит", "смотри", "смотрите",
            "вот", "ладно", "хорошо", "окей", "ок", "короче", "давай", "давайте")

_FILLER_RE = re.compile(r"^(?:%s)\s*[,.:;!—–-]\s*" % "|".join(_FILLERS), re.IGNORECASE)
_MD_RE = re.compile(r"[#*_`>\[\]]+")
_EDGE = " .,:;—–-…"
#: конец предложения: точка/!/?/; (не внутри сокращений вида «т.д.» — грубо, но достаточно)
_SENTENCE_RE = re.compile(r"[.!?;](?=\s|$)")
#: «предложение» короче этого — вводная-пустышка («Теперь.», «Работа.»), её пропускаем
_MIN_SENTENCE = 12


def clean(text) -> str:
    """Схлопнуть пробелы, убрать markdown-мусор (``—`` и кавычки остаются)."""
    value = _MD_RE.sub(" ", str(text or ""))
    return " ".join(value.replace("\u00a0", " ").split()).strip()


def strip_fillers(text) -> str:
    """Убрать ведущие вводные «Так,», «Ну,», «В общем,» (сколько бы их ни было)."""
    value = clean(text)
    for _ in range(4):
        match = _FILLER_RE.match(value)
        if not match:
            break
        value = value[match.end():].strip()
    return value


def cut(text, limit: int = TITLE_LIMIT) -> str:
    """Обрезать по границе слова; многоточие входит в предел."""
    value = clean(text).strip(_EDGE)
    if len(value) <= limit:
        return value
    head = value[: max(1, limit - 1)]
    if " " in head:
        head = head[: head.rindex(" ")]
    head = head.strip(_EDGE)
    return (head or value[: max(1, limit - 1)].rstrip()) + "…"


def first_sentence(text) -> str:
    """Первое СОДЕРЖАТЕЛЬНОЕ предложение ('' если его нет).

    Вводные-пустышки («Так, теперь.») пропускаются: название из одного «теперь»
    бесполезно. Если содержательных предложений нет — возвращается весь текст.
    """
    value = strip_fillers(text)
    if not value:
        return ""
    rest, last = value, ""
    while rest:
        match = _SENTENCE_RE.search(rest)
        if match:
            last = rest[: match.end()].strip(_EDGE)
            rest = strip_fillers(rest[match.end():])
        else:
            last, rest = rest.strip(_EDGE), ""
        if len(last) >= _MIN_SENTENCE:
            return last
    return last or value.strip(_EDGE)


def capitalize(text: str) -> str:
    """Первая буква — заглавная (название в меню/крошках читается как заголовок)."""
    for index, char in enumerate(text or ""):
        if char.isalpha():
            return text[:index] + char.upper() + text[index + 1:]
        if not char.isspace():
            return text            # начинается с цифры/кавычки — не трогаем
    return text


def short_title(text, limit: int = TITLE_LIMIT, fallback: str = "Запрос") -> str:
    """Короткое название ≤ ``limit`` знаков: первое предложение или фрагмент.

    Пустой/мусорный текст → ``fallback``. Никогда не бросает и не возвращает
    строку длиннее ``limit`` (многоточие входит в предел).
    """
    limit = max(8, int(limit or TITLE_LIMIT))
    sentence = first_sentence(text)
    if sentence:
        candidate = sentence if len(sentence) <= limit else cut(sentence, limit)
        if candidate:
            return capitalize(candidate)
    value = cut(text, limit)
    return capitalize(value) or fallback


def title_from_metadata(meta, limit: int = TITLE_LIMIT) -> str:
    """Название, которое задал руководитель: ``metadata.title`` (или ``extra.title``).

    Принимает и «сырую» metadata, и каноническую форму ``normalize`` (там незнакомые
    ключи лежат в ``extra``). Длинное название обрезается, а не отбрасывается: текст
    руководителя полезнее автоматического.
    """
    if not isinstance(meta, dict):
        return ""
    raw = meta.get("title")
    if not isinstance(raw, str) or not raw.strip():
        extra = meta.get("extra")
        raw = extra.get("title") if isinstance(extra, dict) else None
    if not isinstance(raw, str) or not raw.strip():
        return ""
    value = " ".join(raw.split())
    if len(value) > limit:
        return cut(value, limit)
    return value


__all__ = ["TITLE_LIMIT", "clean", "strip_fillers", "cut", "first_sentence", "short_title",
           "title_from_metadata"]
