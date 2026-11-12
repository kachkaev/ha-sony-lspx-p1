"""Every translation has exactly the keys and placeholders of the English source."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from tools import translations as generator

TRANSLATIONS = Path(__file__).resolve().parent.parent / "custom_components" / "sony_lspx_p1" / "translations"
# Keep in sync with .agents/skills/translations/SKILL.md
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


def test_every_other_language_is_generated() -> None:
    assert set(generator.LANGUAGES) == LANGUAGES - {"en"}
    for lang, table in generator.LANGUAGES.items():
        on_disk = json.loads((TRANSLATIONS / f"{lang}.json").read_text(encoding="utf-8"))
        assert on_disk == generator.build(table), f"{lang}.json was edited by hand or not regenerated"


@pytest.mark.parametrize("lang", sorted(LANGUAGES - {"en"}))
def test_translation_matches_english(lang: str) -> None:
    other = flatten(json.loads((TRANSLATIONS / f"{lang}.json").read_text(encoding="utf-8")))
    assert set(other) == set(ENGLISH), f"{lang}: keys differ from en.json"
    for key, text in ENGLISH.items():
        assert placeholders(other[key]) == placeholders(text), f"{lang}: placeholders differ in {key}"
        assert other[key].strip(), f"{lang}: empty string for {key}"
