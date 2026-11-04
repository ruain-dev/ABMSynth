"""Framework-neutral localization service for ABMSynth.

The UI, audio engine and validation layer share stable message IDs in JSON
catalogs. This keeps translated copy outside code without another runtime
package.
"""

from __future__ import annotations

import json
import locale as system_locale
import os
import re
from functools import lru_cache
from pathlib import Path
from typing import Any
from product import ENV_LOCALE, PRODUCT_NAME


LOCALE_DIRECTORY = Path(__file__).with_name("locales")
DEFAULT_LOCALE = "ko-KR"
_PLACEHOLDER = re.compile(r"(?<!\{)\{([A-Za-z_][A-Za-z0-9_]*)[^{}]*\}(?!\})")


def normalize_locale(value: str | None) -> str:
    """Normalize common Windows/POSIX locale spellings to a BCP-47 tag."""
    if not value:
        return DEFAULT_LOCALE
    value = value.split(".", 1)[0].replace("_", "-").strip()
    if not value:
        return DEFAULT_LOCALE
    parts = value.split("-")
    if len(parts) == 1:
        return parts[0].lower()
    return "-".join((parts[0].lower(), *(part.upper() if len(part) == 2 else part for part in parts[1:])))


def preferred_locale() -> str:
    """Read explicit deployment preference before falling back to OS locale."""
    explicit = os.environ.get(ENV_LOCALE)
    if explicit:
        return normalize_locale(explicit)
    return normalize_locale(system_locale.getlocale()[0])


def _flatten_messages(node: object, prefix: str = "") -> dict[str, str]:
    """Flatten the human-editable message tree into stable dotted lookup IDs."""
    if isinstance(node, str):
        if not prefix:
            raise ValueError(f"Invalid {PRODUCT_NAME} locale catalog: message has no ID")
        return {prefix: node}
    if not isinstance(node, dict):
        raise ValueError(f"Invalid {PRODUCT_NAME} locale catalog: message tree contains a non-string leaf")

    messages: dict[str, str] = {}
    for key, value in node.items():
        if not isinstance(key, str) or not key:
            raise ValueError(f"Invalid {PRODUCT_NAME} locale catalog: message tree has an invalid key")
        child_prefix = f"{prefix}.{key}" if prefix else key
        for message_id, text in _flatten_messages(value, child_prefix).items():
            if message_id in messages:
                raise ValueError(f"Invalid {PRODUCT_NAME} locale catalog: duplicate ID {message_id!r}")
            messages[message_id] = text
    return messages


def _unique_message_object(pairs):
    """Reject overwritten JSON keys before flattening can lose their identity."""
    result={}
    for key,value in pairs:
        if key in result:raise ValueError(f'Invalid {PRODUCT_NAME} locale catalog: duplicate key {key!r}')
        result[key]=value
    return result


@lru_cache(maxsize=None)
def load_catalog(locale_name: str) -> dict[str, str]:
    path = LOCALE_DIRECTORY / f"{normalize_locale(locale_name)}.json"
    if not path.is_file():
        return {}
    payload = json.loads(path.read_text(encoding="utf-8"),object_pairs_hook=_unique_message_object)
    if not isinstance(payload, dict) or not isinstance(payload.get("meta"), dict):
        raise ValueError(f"Invalid {PRODUCT_NAME} locale catalog: {path}")
    if "strings" in payload:
        raise ValueError(f"Invalid {PRODUCT_NAME} locale catalog: legacy 'strings' container is not allowed")
    return _flatten_messages({key: value for key, value in payload.items() if key != "meta"})


_active_locale = preferred_locale()


def set_locale(locale_name: str | None) -> str:
    """Select a locale for future string lookups and return its normalized tag."""
    global _active_locale
    _active_locale = normalize_locale(locale_name)
    return _active_locale


def active_locale() -> str:
    return _active_locale


def available_locales() -> tuple[str, ...]:
    return tuple(sorted(path.stem for path in LOCALE_DIRECTORY.glob("*.json") if path.stem != "template"))


def catalog_value(key: str, locale_name: str | None = None) -> str:
    """Resolve an ID through exact -> language -> Korean fallback catalogs."""
    requested = normalize_locale(locale_name or _active_locale)
    candidates = (requested, requested.split("-", 1)[0], DEFAULT_LOCALE)
    for candidate in dict.fromkeys(candidates):
        value = load_catalog(candidate).get(key)
        if value is not None:
            return value
    raise KeyError(f"Missing {PRODUCT_NAME} localization key: {key}")


def tr(key: str, /, **values: Any) -> str:
    """Translate a stable ID and format named, translator-visible values."""
    template = catalog_value(key)
    if not values:
        return template
    try:
        return template.format(**values)
    except (KeyError, ValueError) as exc:
        raise ValueError(f"Invalid values for localization key {key!r}: {exc}") from exc


def placeholders(text: str) -> frozenset[str]:
    return frozenset(_PLACEHOLDER.findall(text))

