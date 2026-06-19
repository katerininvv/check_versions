"""Перевод текста на русский. Несколько провайдеров на выбор, по умолчанию —
бесплатный Google (без ключа). Модуль подключаемый: провайдер задаётся в настройках.

Провайдеры (settings: translator_provider):
  - google        : бесплатно, без ключа, работает сразу (рекомендуется по умолчанию)
  - libretranslate: self-hosted/публичный сервер (бесплатно, приватно); нужен URL
  - mymemory      : бесплатно, без ключа, есть суточный лимит
  - deepl         : платно/лимит; нужен ключ DeepL API
Все провайдеры реализованы через библиотеку deep-translator.
"""
from . import database

# Лимит длины одного запроса (Google ~5000). Режем с запасом и переводим частями.
CHUNK = 4500


def _chunks(text: str, size: int = CHUNK):
    """Бьёт текст на части по границам строк, не длиннее size символов."""
    parts, cur = [], ""
    for line in text.split("\n"):
        if len(cur) + len(line) + 1 > size:
            if cur:
                parts.append(cur)
            while len(line) > size:
                parts.append(line[:size])
                line = line[size:]
            cur = line
        else:
            cur = f"{cur}\n{line}" if cur else line
    if cur:
        parts.append(cur)
    return parts or [text]


def _make_translator(provider: str):
    """Создаёт объект-переводчик deep-translator для выбранного провайдера."""
    if provider == "google":
        from deep_translator import GoogleTranslator
        return GoogleTranslator(source="auto", target="ru")
    if provider == "libretranslate":
        from deep_translator import LibreTranslator
        url = database.get_setting("libretranslate_url", "").strip()
        if not url:
            return None
        key = database.get_setting("libretranslate_api_key", "").strip() or None
        return LibreTranslator(source="auto", target="ru", base_url=url, api_key=key)
    if provider == "mymemory":
        from deep_translator import MyMemoryTranslator
        return MyMemoryTranslator(source="auto", target="ru-RU")
    if provider == "deepl":
        from deep_translator import DeeplTranslator
        key = database.get_setting("deepl_api_key", "").strip()
        if not key:
            return None
        return DeeplTranslator(api_key=key, source="en", target="ru",
                               use_free_api=key.endswith(":fx"))
    return None


def translate_to_russian(text: str | None) -> tuple[str | None, bool]:
    """Переводит текст на русский.

    Возвращает (текст, translated_flag). При отсутствии провайдера/ошибке —
    исходный текст и False (graceful degradation: дайджест уйдёт без перевода).
    """
    if not text or not text.strip():
        return text, False

    provider = database.get_setting("translator_provider", "google") or "google"
    try:
        translator = _make_translator(provider)
        if translator is None:
            return text, False
        out = []
        for chunk in _chunks(text):
            if chunk.strip():
                out.append(translator.translate(chunk))
            else:
                out.append(chunk)
        return "\n".join(out), True
    except Exception:
        return text, False
