"""
app/channels/telegram/i18n.py

Internationalization helper for Telegram messages (English & Amharic).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

LOCALES_DIR = Path(__file__).parent / "locales"

_STRINGS: dict[str, dict[str, str]] = {}


def _load_strings() -> None:
    for lang in ("en", "am"):
        path = LOCALES_DIR / f"{lang}.json"
        if path.exists():
            with open(path, "r", encoding="utf-8") as f:
                _STRINGS[lang] = json.load(f)
        else:
            _STRINGS[lang] = {}


_load_strings()


def t(key: str, lang: str = "en", **kwargs: Any) -> str:
    """
    Get localized string by key and language code ('en' or 'am').
    Interpolates kwargs if provided.
    Falls back to English if key missing in target language.
    """
    lang = lang.lower()
    if lang not in _STRINGS:
        lang = "en"

    template = _STRINGS.get(lang, {}).get(key)
    if template is None:
        template = _STRINGS.get("en", {}).get(key, key)

    if kwargs:
        try:
            return template.format(**kwargs)
        except Exception:
            return template

    return template
