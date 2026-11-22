"""Serve the dashboard card and make every dashboard load it.

Home Assistant serves pages as soon as its frontend is set up, before custom integrations are. A page loaded in that
window (the companion apps reload the moment they reconnect after a restart) imports the extra modules it was served
with once, at load, so a module added later never reaches it, and requests for the card's file get a 404 until this
integration serves it. The dashboard then shows "Configuration error" until the page is refreshed.

So the card goes in as a Lovelace resource in storage mode. Resources are stored, so a page loaded during start-up
lists the card too; and the resource's URL carries a token that changes whenever the file does, so browsers keep the
file in their cache for good and load it from there while Home Assistant is still starting. Only a browser that never
loaded this version of the card still misses it in that window.

There is at most one resource for the card. The integration creates it, keeps its URL current and deletes it when the
last projector is removed; it remembers which resource is its own in `.storage/sony_lspx_p1.card_resource`. A resource
the user added for the same file is left alone, and then the integration deletes its own, so the card loads once.
Where resources are kept in YAML (or the resource collection is not available), the card is loaded as an extra module
instead, as before, unless a YAML resource already loads it.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

from aiohttp import hdrs, web
from homeassistant.components.http import HomeAssistantView
from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store
from homeassistant.loader import async_get_integration
from homeassistant.util.hass_dict import HassKey

from .const import DOMAIN

if TYPE_CHECKING:
    from homeassistant.components.lovelace.resources import ResourceStorageCollection, ResourceYAMLCollection

_LOGGER = logging.getLogger(__name__)

CARD_URL = f"/{DOMAIN}/sony-lspx-p1-card.js"
CARD_PATH = Path(__file__).parent / "frontend" / "sony-lspx-p1-card.js"
# The URL with the current token (see card_token) never serves anything else, so browsers may keep it; any other URL
# (an older token, a resource the user added without one) is checked with the server on every load.
CACHE_FOREVER = "public, max-age=31536000, immutable"
CACHE_NEVER = "no-cache"
STORAGE_KEY = f"{DOMAIN}.card_resource"
STORAGE_VERSION = 1


@dataclass
class CardData:
    """What this run of Home Assistant knows about the card."""

    url: str  # the card's URL with the current token
    store: Store[dict[str, Any]]
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    extra_module: bool = False  # loaded as an extra module (the fallback)


DATA_CARD: HassKey[CardData] = HassKey(f"{DOMAIN}_card")


def card_token(version: str, content: bytes) -> str:
    """The integration's version and a hash of the file: it changes on every update and every edit."""
    return f"{version}-{hashlib.sha256(content).hexdigest()[:8]}"


def is_card_url(url: object) -> bool:
    """Whether a resource URL loads this integration's card, whatever its query string or host."""
    return isinstance(url, str) and urlsplit(url).path == CARD_URL


class CardView(HomeAssistantView):
    """The card's file, cached for good under the current token's URL only."""

    url = CARD_URL
    name = f"{DOMAIN}:card"
    requires_auth = False  # loaded by a <script> tag, like any static file

    def __init__(self, token: str) -> None:
        self._token = token

    async def get(self, request: web.Request) -> web.FileResponse:
        cache = CACHE_FOREVER if request.query.get("v") == self._token else CACHE_NEVER
        return web.FileResponse(CARD_PATH, headers={hdrs.CACHE_CONTROL: cache})

    head = get  # as static files answer HEAD too


def _resource_collection(hass: HomeAssistant) -> ResourceStorageCollection | ResourceYAMLCollection | None:
    from homeassistant.components.lovelace.const import LOVELACE_DATA  # noqa: PLC0415

    lovelace = hass.data.get(LOVELACE_DATA)
    return lovelace.resources if lovelace is not None else None


async def _async_storage_items(resources: ResourceStorageCollection) -> list[dict[str, Any]]:
    # Home Assistant reads the stored resources only when they are first asked for. `loaded` and `async_load` are
    # what its own websocket command uses (checked on 2025.4 and 2026.10); if they change, setup falls back.
    if not resources.loaded:
        await resources.async_load()
        resources.loaded = True
    return list(resources.async_items())


async def async_setup_card(hass: HomeAssistant) -> None:
    """Serve the card and make dashboards load it. Never raises: the card must not keep the projector from setting up.

    Called on every entry setup (also reloads): it does nothing that is already done.
    """
    if "http" not in hass.config.components or "frontend" not in hass.config.components:
        return  # no web interface (tests, minimal setups); a later entry setup serves the card if one appears
    try:
        if (data := hass.data.get(DATA_CARD)) is None:
            version = str((await async_get_integration(hass, DOMAIN)).version)
            token = card_token(version, await hass.async_add_executor_job(CARD_PATH.read_bytes))
            hass.http.register_view(CardView(token))
            data = hass.data[DATA_CARD] = CardData(f"{CARD_URL}?v={token}", Store(hass, STORAGE_VERSION, STORAGE_KEY))
    except Exception:
        _LOGGER.exception("Could not serve the dashboard card")
        return
    async with data.lock:
        try:
            loaded = await _async_sync_resource(hass, data)
        except Exception:
            _LOGGER.exception("Could not add the dashboard card to the dashboard resources; loading it as before")
            loaded = False
        if loaded == data.extra_module:  # a resource now loads the card after a failure, or the other way round
            from homeassistant.components.frontend import add_extra_js_url, remove_extra_js_url  # noqa: PLC0415

            (remove_extra_js_url if loaded else add_extra_js_url)(hass, data.url)
            data.extra_module = not loaded


async def _async_sync_resource(hass: HomeAssistant, data: CardData) -> bool:
    """Keep exactly one dashboard resource loading the card. False when resources are not kept in storage."""
    from homeassistant.components.lovelace.resources import ResourceStorageCollection  # noqa: PLC0415

    resources = _resource_collection(hass)
    if resources is None:
        return False
    if not isinstance(resources, ResourceStorageCollection):
        # Resources in YAML: the user's own resource may load the card already; otherwise fall back.
        return any(is_card_url(item.get("url")) for item in resources.async_items())
    items = await _async_storage_items(resources)
    own_id = ((await data.store.async_load()) or {}).get("resource_id")
    own = next((item for item in items if item["id"] == own_id and is_card_url(item.get("url"))), None)
    if others := [item for item in items if item is not own and is_card_url(item.get("url"))]:
        # The user loads the card with a resource of their own: leave theirs alone and stop loading it twice.
        if own is not None:
            await resources.async_delete_item(own["id"])
            _LOGGER.info("Removed this integration's dashboard resource: %s loads the card", others[0]["url"])
        if own_id is not None:
            await data.store.async_remove()
        return True
    if own is None:
        own = await resources.async_create_item({"res_type": "module", "url": data.url})
        await data.store.async_save({"resource_id": own["id"]})
        _LOGGER.info("Added the dashboard card to the dashboard resources: %s", data.url)
    elif own["url"] != data.url or own.get("type") != "module":
        await resources.async_update_item(own["id"], {"res_type": "module", "url": data.url})
        _LOGGER.info("Updated the dashboard card's resource: %s", data.url)
    return True


async def async_remove_card(hass: HomeAssistant) -> None:
    """Undo async_setup_card when the last projector is removed: delete the resource it added, and only that one."""
    data = hass.data.get(DATA_CARD)
    store = data.store if data is not None else Store[dict[str, Any]](hass, STORAGE_VERSION, STORAGE_KEY)
    async with data.lock if data is not None else asyncio.Lock():
        try:
            from homeassistant.components.lovelace.resources import ResourceStorageCollection  # noqa: PLC0415

            if data is not None and data.extra_module:
                from homeassistant.components.frontend import remove_extra_js_url  # noqa: PLC0415

                remove_extra_js_url(hass, data.url)
                data.extra_module = False
            own_id = ((await store.async_load()) or {}).get("resource_id")
            resources = _resource_collection(hass)
            if own_id is not None and not isinstance(resources, ResourceStorageCollection):
                # Resources are in YAML now (or not loaded), so the stored ones cannot be changed. Keep the record:
                # a later setup in storage mode still knows the resource as its own and updates or removes it.
                return
            if own_id is not None and isinstance(resources, ResourceStorageCollection):
                items = await _async_storage_items(resources)
                if any(item["id"] == own_id and is_card_url(item.get("url")) for item in items):
                    await resources.async_delete_item(own_id)
                    _LOGGER.info("Removed the dashboard card from the dashboard resources")
            await store.async_remove()
        except Exception:
            _LOGGER.exception("Could not remove the dashboard card's resource")
