"""The bundled dashboard card."""

from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component

from custom_components.sony_lspx_p1 import CARD_PATH, CARD_URL
from custom_components.sony_lspx_p1.const import DOMAIN


def test_card_file_is_bundled() -> None:
    assert CARD_PATH.is_file()
    assert 'const CARD_TYPE = "sony-lspx-p1-card"' in CARD_PATH.read_text()
    assert CARD_URL.endswith(CARD_PATH.name)


async def test_setup_without_frontend(hass: HomeAssistant) -> None:
    """Headless installs (no frontend/http) still set up the integration."""
    assert await async_setup_component(hass, DOMAIN, {})
