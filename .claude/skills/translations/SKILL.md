---
name: translations
description: Keep the integration's user-facing strings translated into every supported language. Use whenever adding, removing or changing any string in custom_components/sony_lspx_p1/translations/en.json (entity names and states, config flow steps, errors, exceptions), or when adding an entity with a translation_key.
---

# Translations

The integration ships translations for these 16 languages (Home Assistant language codes):

`en` (source of truth), `de`, `fr`, `es`, `it`, `nl`, `pl`, `pt-BR`, `sv`, `da`, `nb`, `cs`, `uk`, `ru`, `zh-Hans`, `ja`.

They were chosen as the languages with the largest Home Assistant communities. Always keep all of them in sync; never ship a string in English only.

## How to change strings

1. Edit `custom_components/sony_lspx_p1/translations/en.json`.
2. Edit `translations/ru.json` by hand in the same way (it is maintained directly).
3. Add or change the matching entries in every language table in `tools/translations.py` (`LANGUAGES`), and extend `build()` if you added a key. Then regenerate:

   ```bash
   .venv/bin/python tools/translations.py
   ```

4. Run `.venv/bin/pytest tests/test_translations.py`. It fails if any language misses a key, has an extra key, drops a placeholder such as `{name}` or `{error}`, or if a language file exists that is not in the list above.

## Conventions

- Keep placeholders exactly as in English (`{name}`, `{error}`).
- Menu names from Sony's smartphone app stay in English in every language (`**Settings → Network → Restrict adding smartphones**`, `**Smartphones added**`), because the app shows them in English.
- `xLOUD`, `Home Assistant`, `ESPHome`, `RESET` and `Sony LSPX-P1` are not translated.
- Prefer the terms Home Assistant itself uses in that language (for example "Konfiguration", "Gerät"); keep entity names short.

## Adding a language

Add the code to the list above, to `LANGUAGES` in `tests/test_translations.py`, and a table to `tools/translations.py`; regenerate and run the tests.
