"""Every translation has exactly the keys and placeholders of the English source."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

TRANSLATIONS = Path(__file__).resolve().parent.parent / "custom_components" / "sony_lspx_p1" / "translations"
# Keep in sync with .claude/skills/translations/SKILL.md
LANGUAGES = {
    "en", "de", "fr", "es", "it", "nl", "pl", "pt-BR", "sv", "da", "nb", "cs", "uk", "ru", "zh-Hans", "ja",
}  # fmt: skip


def flatten(obj: dict, prefix: str = "") -> dict[str, str]:
    out: dict[str, str] = {}
    for key, value in obj.items():
        path = f"{prefix}.{key}" if prefix else key
        if isinstance(value, dict):
            out.update(flatten(value, path))
        else:
            out[path] = value
    return out


def placeholders(text: str) -> set[str]:
    return set(re.findall(r"\{(\w+)\}", text))


ENGLISH = flatten(json.loads((TRANSLATIONS / "en.json").read_text(encoding="utf-8")))


def test_every_supported_language_has_a_file() -> None:
    assert {p.stem for p in TRANSLATIONS.glob("*.json")} == LANGUAGES


@pytest.mark.parametrize("lang", sorted(LANGUAGES - {"en"}))
def test_translation_matches_english(lang: str) -> None:
    other = flatten(json.loads((TRANSLATIONS / f"{lang}.json").read_text(encoding="utf-8")))
    assert set(other) == set(ENGLISH), f"{lang}: keys differ from en.json"
    for key, text in ENGLISH.items():
        assert placeholders(other[key]) == placeholders(text), f"{lang}: placeholders differ in {key}"
        assert other[key].strip(), f"{lang}: empty string for {key}"
