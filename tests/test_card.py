"""The bundled dashboard card."""

import json
from unittest.mock import AsyncMock, MagicMock, patch

from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component

from custom_components.sony_lspx_p1 import CARD_PATH, CARD_URL, async_setup
from custom_components.sony_lspx_p1.const import DOMAIN


def test_card_file_is_bundled() -> None:
    assert CARD_PATH.is_file()
    assert 'const CARD_TYPE = "sony-lspx-p1-card"' in CARD_PATH.read_text()
    assert CARD_URL.endswith(CARD_PATH.name)


async def test_setup_without_frontend(hass: HomeAssistant) -> None:
    """Headless installs (no frontend/http) still set up the integration."""
    assert await async_setup_component(hass, DOMAIN, {})


async def test_card_is_served_and_loaded_on_every_dashboard(hass: HomeAssistant) -> None:
    version = json.loads((CARD_PATH.parent.parent / "manifest.json").read_text())["version"]
    hass.config.components.update({"frontend", "http"})
    hass.http = MagicMock(async_register_static_paths=AsyncMock())
    with patch("homeassistant.components.frontend.add_extra_js_url") as add_extra_js_url:
        assert await async_setup(hass, {})
    (paths,) = hass.http.async_register_static_paths.call_args.args
    assert [(p.url_path, p.path, p.cache_headers) for p in paths] == [(CARD_URL, str(CARD_PATH), False)]
    # The version in the URL makes browsers fetch the card again after an update.
    add_extra_js_url.assert_called_once_with(hass, f"{CARD_URL}?v={version}")
