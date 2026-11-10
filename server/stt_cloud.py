"""Распознавание речи «как настроено у пользователя»: локально, через его провайдера или никак.

Hermes умеет три разных пути, и приложение не должно предполагать свой:

1. **local** — установлен ``faster_whisper`` → работает наш постоянный процесс
   ``server/stt_worker.py`` (как раньше);
2. **provider** — локального движка нет, но в Hermes выбран облачный STT
   (``stt.provider: openai|groq|mistral|xai|elevenlabs|deepinfra``) и есть ключ →
   отправляем аудио в его HTTP API тем же провайдером/моделью/ключом. Своих ключей
   не заводим, ничего не доустанавливаем;
3. **none** — ничего не настроено: ``/api/stt/status`` отдаёт ``mode="none"`` и
   понятный текст, диктовка просто недоступна (остальное приложение работает).

Секреты: ключ читается только чтобы подписать запрос. Он никогда не попадает ни в
ответы API, ни в журнал — наружу уходят лишь имя провайдера, модель и причина отказа.

Публично: ``resolve(cfg)``, ``describe(cfg)``, ``transcribe(cfg, audio, suffix, ...)``,
``PROVIDERS``, ``CloudError``.
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
import uuid
from pathlib import Path

#: Облачные STT Hermes: как называется, чем подписывается, куда и с каким полем шлём файл.
PROVIDERS = {
    "openai": {"env": "OPENAI_API_KEY", "model": "whisper-1", "path": "/audio/transcriptions",
               "base": "https://api.openai.com/v1", "auth": "bearer", "model_field": "model"},
    "groq": {"env": "GROQ_API_KEY", "model": "whisper-large-v3-turbo", "path": "/audio/transcriptions",
             "base": "https://api.groq.com/openai/v1", "auth": "bearer", "model_field": "model",
             "base_env": "GROQ_BASE_URL"},
    "deepinfra": {"env": "DEEPINFRA_API_KEY", "model": "openai/whisper-large-v3-turbo",
                  "path": "/audio/transcriptions", "base": "https://api.deepinfra.com/v1/openai",
                  "auth": "bearer", "model_field": "model"},
    "mistral": {"env": "MISTRAL_API_KEY", "model": "voxtral-mini-latest", "path": "/audio/transcriptions",
                "base": "https://api.mistral.ai/v1", "auth": "bearer", "model_field": "model"},
    "xai": {"env": "XAI_API_KEY", "model": "grok-voice-transcribe-2.0", "path": "/stt",
            "base": "https://api.x.ai/v1", "auth": "bearer", "model_field": "model",
            "base_env": "XAI_STT_BASE_URL"},
    "elevenlabs": {"env": "ELEVENLABS_API_KEY", "model": "scribe_v2", "path": "/speech-to-text",
                   "base": "https://api.elevenlabs.io/v1", "auth": "xi", "model_field": "model_id",
                   "base_env": "ELEVENLABS_STT_BASE_URL"},
}

#: Сообщения для человека — короткие и без техножаргона.
NO_PROVIDER_TEXT = ("Распознавание речи не настроено: включите диктовку в Hermes "
                    "(Настройки → Голос → Распознавание) или установите faster-whisper. "
                    "Без диктовки всё остальное работает.")
NO_KEY_TEXT = ("В Hermes выбран способ распознавания «%s», но ключ не найден — "
               "проверьте ключ в Hermes или включите локальную диктовку.")
HINT_LOCAL = "pip install faster-whisper"

MAX_SECONDS = 120.0


class CloudError(RuntimeError):
    """Ошибка облачного распознавания: код для API и русский текст."""

    def __init__(self, code: str, message_ru: str, detail: dict | None = None):
        super().__init__(message_ru)
        self.code = code
        self.message_ru = message_ru
        self.detail = dict(detail or {})


# ---------------------------------------------------------------- конфиг Hermes
def _scalar(raw: str) -> str:
    text = raw.strip()
    if not text:
        return ""
    if text[0] in "\"'" and text[-1:] == text[0] and len(text) > 1:
        return text[1:-1]
    return text.split(" #", 1)[0].strip()


def parse_yaml_subset(text: str) -> dict:
    """Минимальный YAML: вложенные словари по отступам, скаляры, комментарии.

    Нужен только чтобы прочитать ``stt.provider``/``stt.<p>.*`` и
    ``providers.<p>.api_key`` без PyYAML (сервер — stdlib).
    """
    root: dict = {}
    stack: list[tuple[int, dict]] = [(-1, root)]
    for line in (text or "").splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        indent = len(line) - len(line.lstrip(" "))
        body = line.strip()
        if body.startswith("- "):
            continue
        if ":" not in body:
            continue
        key, _, value = body.partition(":")
        key = _scalar(key)
        while stack and indent <= stack[-1][0]:
            stack.pop()
        parent = stack[-1][1] if stack else root
        value = _scalar(value)
        if value:
            parent[key] = value
            continue
        child: dict = {}
        parent[key] = child
        stack.append((indent, child))
    return root


def config_paths(cfg=None) -> list:
    """Файлы настроек Hermes: активный профиль, затем корень (только чтение)."""
    env_path = os.environ.get("TEAMROOM_STT_CONFIG")
    if env_path:
        return [Path(env_path)]
    home = getattr(cfg, "hermes_home", None) if cfg is not None else None
    if not home:
        home = (os.environ.get("TEAMROOM_HERMES_HOME") or os.environ.get("HERMES_HOME"))
    if not home:
        local = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        home = str(Path(local) / "hermes")
    home_path = Path(home)
    out: list = []
    profile = (os.environ.get("HERMES_PROFILE") or os.environ.get("TEAMROOM_STT_PROFILE")
               or os.environ.get("HERMES_PROFILE_NAME") or "").strip()
    if profile:
        out.append(home_path / "profiles" / profile / "config.yaml")
    out.append(home_path / "config.yaml")
    return out


def _read_dotenv(home: Path) -> dict:
    """Пары из ``.env`` Hermes: только имена и значения, ничего не пишем."""
    out: dict = {}
    try:
        text = (home / ".env").read_text(encoding="utf-8-sig", errors="replace")
    except OSError:
        return out
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        out[key.strip()] = value.strip().strip("\"'")
    return out


def load_settings(cfg=None) -> dict:
    """Свести настройки Hermes в один словарь: провайдер, модели, базы, ключи.

    Читаем корень, затем активный профиль — профиль перекрывает корень.
    """
    merged: dict = {}
    paths = config_paths(cfg)
    for path in reversed(paths):
        try:
            text = Path(path).read_text(encoding="utf-8-sig", errors="replace")
        except OSError:
            continue
        parsed = parse_yaml_subset(text)
        for key, value in parsed.items():
            if isinstance(value, dict):
                slot = merged.setdefault(key, {})
                if isinstance(slot, dict):
                    slot.update(value)
            else:
                merged.setdefault(key, value)
        for key, value in parsed.items():
            if isinstance(value, dict) and isinstance(merged.get(key), dict):
                merged[key].update(value)
    home = Path(paths[-1]).parent
    dotenv = _read_dotenv(home)
    stt = merged.get("stt") if isinstance(merged.get("stt"), dict) else {}
    providers = merged.get("providers") if isinstance(merged.get("providers"), dict) else {}
    return {"raw": merged, "stt": stt, "providers": providers, "env": dotenv}


def _key_for(settings: dict, name: str) -> str:
    """Ключ провайдера: переменная окружения → providers.<p>.api_key → stt.<p>.api_key."""
    spec = PROVIDERS[name]
    value = (os.environ.get(spec["env"]) or settings["env"].get(spec["env"]) or "").strip()
    if value:
        return value
    for section, holder in (("providers", settings["providers"]), ("stt", settings["stt"])):
        block = holder.get(name) if isinstance(holder, dict) else None
        if isinstance(block, dict):
            value = str(block.get("api_key") or "").strip()
            if value:
                return value
    return ""


def _base_for(settings: dict, name: str) -> str:
    spec = PROVIDERS[name]
    block = settings["stt"].get(name) if isinstance(settings["stt"], dict) else None
    if isinstance(block, dict) and block.get("base_url"):
        return str(block["base_url"]).strip().rstrip("/")
    env_name = spec.get("base_env") or ("STT_%s_BASE_URL" % name.upper())
    value = (os.environ.get(env_name) or settings["env"].get(env_name) or "").strip()
    return (value or spec["base"]).rstrip("/")


def _model_for(settings: dict, name: str) -> str:
    block = settings["stt"].get(name) if isinstance(settings["stt"], dict) else None
    field = PROVIDERS[name]["model_field"]
    if isinstance(block, dict):
        for key in (field, "model", "model_id"):
            value = str(block.get(key) or "").strip()
            if value:
                return value
    env_name = "STT_%s_MODEL" % name.upper()
    return (os.environ.get(env_name) or settings["env"].get(env_name)
            or PROVIDERS[name]["model"]).strip()


def selected_provider(settings: dict) -> str:
    """``stt.provider`` из Hermes (в нижнем регистре, без пробелов)."""
    return str(settings["stt"].get("provider") or "").strip().lower()


def language_for(settings: dict) -> str:
    stt = settings["stt"]
    return str(stt.get("language") or "").strip()


def resolve(cfg=None) -> dict:
    """Что доступно: {mode, source, provider, model, base_url, key, reason}.

    ``mode``: ``provider`` (облако Hermes), иначе ``none`` — локальный движок
    проверяет `server/stt.py` (он знает про faster-whisper).
    """
    settings = load_settings(cfg)
    name = selected_provider(settings)
    if not name:
        return {"mode": "none", "source": "none", "provider": "", "model": "",
                "base_url": "", "key": "", "reason": NO_PROVIDER_TEXT}
    if name in ("local", "local_command"):
        return {"mode": "local_configured", "source": "local", "provider": name, "model": "",
                "base_url": "", "key": "", "reason": ""}
    if name not in PROVIDERS:
        return {"mode": "none", "source": "provider", "provider": name, "model": "",
                "base_url": "", "key": "",
                "reason": "Способ распознавания «%s» в Hermes не поддерживается этим приложением. "
                          "%s" % (name, NO_PROVIDER_TEXT)}
    key = _key_for(settings, name)
    model = _model_for(settings, name)
    base = _base_for(settings, name)
    if not key:
        return {"mode": "none", "source": "provider", "provider": name, "model": model,
                "base_url": base, "key": "", "reason": NO_KEY_TEXT % name}
    return {"mode": "provider", "source": "provider", "provider": name, "model": model,
            "base_url": base, "key": key, "reason": ""}


# ---------------------------------------------------------------- HTTP
def _multipart(fields: dict, filename: str, content: bytes, field_name: str = "file"):
    """Собрать multipart/form-data тело (stdlib, без requests)."""
    boundary = "----teamroom" + uuid.uuid4().hex
    chunks: list[bytes] = []
    for key, value in fields.items():
        if value in (None, ""):
            continue
        chunks.append(("--%s\r\nContent-Disposition: form-data; name=\"%s\"\r\n\r\n%s\r\n"
                       % (boundary, key, value)).encode("utf-8"))
    chunks.append(("--%s\r\nContent-Disposition: form-data; name=\"%s\"; filename=\"%s\"\r\n"
                   "Content-Type: application/octet-stream\r\n\r\n"
                   % (boundary, field_name, filename)).encode("utf-8"))
    chunks.append(content)
    chunks.append(("\r\n--%s--\r\n" % boundary).encode("utf-8"))
    return b"".join(chunks), "multipart/form-data; boundary=%s" % boundary


def _headers(name: str, key: str) -> dict:
    spec = PROVIDERS[name]
    if spec["auth"] == "xi":
        return {"xi-api-key": key, "Accept": "application/json"}
    return {"Authorization": "Bearer %s" % key, "Accept": "application/json"}


def _extract_text(payload: bytes) -> str:
    """Текст из ответа провайдера (у всех перечисленных — поле ``text``)."""
    try:
        body = json.loads(payload.decode("utf-8", "replace"))
    except ValueError:
        return ""
    if isinstance(body, dict):
        text = body.get("text")
        if isinstance(text, str):
            return text.strip()
        for holder in ("transcription", "data"):
            inner = body.get(holder)
            if isinstance(inner, dict) and isinstance(inner.get("text"), str):
                return inner["text"].strip()
    return ""


def transcribe(cfg, audio: bytes, suffix: str = ".webm", *, language: str | None = None,
               timeout: float = MAX_SECONDS) -> dict:
    """Распознать через провайдера Hermes. Возвращает {text, provider, model, elapsed_s}."""
    info = resolve(cfg)
    if info["mode"] != "provider":
        raise CloudError("stt_unavailable", info["reason"] or NO_PROVIDER_TEXT,
                         {"provider": info.get("provider") or ""})
    name = info["provider"]
    spec = PROVIDERS[name]
    fields = {spec["model_field"]: info["model"]}
    lang = (language or language_for(load_settings(cfg)) or "").strip()
    if lang:
        fields["language" if name != "elevenlabs" else "language_code"] = lang
    if name == "elevenlabs":
        fields.setdefault("tag_audio_events", "false")
    body, content_type = _multipart(fields, "audio%s" % (suffix or ".webm"), audio)
    headers = _headers(name, info["key"])
    headers["Content-Type"] = content_type
    request = urllib.request.Request(info["base_url"] + spec["path"], data=body,
                                     headers=headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read()
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = exc.read().decode("utf-8", "replace")[:300]
        except OSError:
            detail = ""
        raise CloudError("stt_failed", "Сервис распознавания ответил ошибкой (%d). "
                                       "Проверьте ключ и выбранную модель в Hermes."
                         % exc.code, {"provider": name, "status": exc.code,
                                      "body": detail}) from exc
    except (OSError, ValueError) as exc:
        raise CloudError("stt_failed", "Нет связи с сервисом распознавания — проверьте интернет.",
                         {"provider": name, "error": str(exc)[:200]}) from exc
    text = _extract_text(raw)
    if not text and raw.strip():
        raise CloudError("stt_failed", "Сервис распознавания вернул пустой ответ.",
                         {"provider": name})
    return {"text": text, "provider": name, "model": info["model"]}
