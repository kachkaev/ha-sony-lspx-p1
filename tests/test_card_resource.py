"""How dashboards get the card: a Lovelace resource in storage mode, else an extra module."""

from __future__ import annotations

from collections.abc import Generator
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from homeassistant.components.lovelace.const import LOVELACE_DATA
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import CONF_ADDRESS
from homeassistant.core import HomeAssistant
from homeassistant.loader import async_get_integration
from homeassistant.setup import async_setup_component
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.sony_lspx_p1.card import (
    CACHE_FOREVER,
    CACHE_NEVER,
    CARD_PATH,
    CARD_URL,
    DATA_CARD,
    STORAGE_KEY,
    async_remove_card,
    async_setup_card,
    card_token,
    is_card_url,
)
from custom_components.sony_lspx_p1.const import DOMAIN

RESOURCES_KEY = "lovelace_resources"


async def current_url(hass: HomeAssistant) -> str:
    version = str((await async_get_integration(hass, DOMAIN)).version)
    return f"{CARD_URL}?v={card_token(version, CARD_PATH.read_bytes())}"


def stored(key: str, data: dict[str, Any]) -> dict[str, Any]:
    return {"version": 1, "minor_version": 1, "key": key, "data": data}


def stored_resources(hass_storage: dict[str, Any], *items: dict[str, str]) -> None:
    hass_storage[RESOURCES_KEY] = stored(RESOURCES_KEY, {"items": list(items)})


def remember_own(hass_storage: dict[str, Any], resource_id: str) -> None:
    hass_storage[STORAGE_KEY] = stored(STORAGE_KEY, {"resource_id": resource_id})


def resources(hass: HomeAssistant) -> list[dict[str, Any]]:
    return list(hass.data[LOVELACE_DATA].resources.async_items())


@pytest.fixture
def extra_js() -> Generator[tuple[MagicMock, MagicMock]]:
    with (
        patch("homeassistant.components.frontend.add_extra_js_url") as add,
        patch("homeassistant.components.frontend.remove_extra_js_url") as remove,
    ):
        yield add, remove


@pytest.fixture
async def web(hass: HomeAssistant) -> None:
    """A web interface: the real http component, and the frontend's name (the frontend needs its package)."""
    assert await async_setup_component(hass, "http", {})
    hass.config.components.add("frontend")


async def setup_lovelace(hass: HomeAssistant, config: dict[str, Any] | None = None) -> None:
    assert await async_setup_component(hass, "lovelace", {"lovelace": config or {}})


@pytest.fixture
async def storage_mode(hass: HomeAssistant, web: None) -> None:
    await setup_lovelace(hass)


def test_card_url_matching() -> None:
    assert is_card_url(CARD_URL)
    assert is_card_url(f"{CARD_URL}?v=1")
    assert is_card_url(f"http://homeassistant.local:8123{CARD_URL}")
    assert not is_card_url("/local/sony-lspx-p1-card.js")
    assert not is_card_url(None)


def test_token_changes_with_version_and_content() -> None:
    assert card_token("1.0.0", b"a") == card_token("1.0.0", b"a")
    assert card_token("1.0.0", b"a") != card_token("1.0.1", b"a")
    assert card_token("1.0.0", b"a") != card_token("1.0.0", b"b")
    assert card_token("1.0.0", b"a").startswith("1.0.0-")


async def test_storage_mode_creates_one_resource(
    hass: HomeAssistant, storage_mode: None, extra_js: tuple[MagicMock, MagicMock], hass_storage: dict[str, Any]
) -> None:
    stored_resources(hass_storage, {"id": "other", "type": "module", "url": "/local/other-card.js"})
    await async_setup_card(hass)
    url = await current_url(hass)
    ours = [item for item in resources(hass) if item["url"] == url]
    assert len(ours) == 1
    assert ours[0]["type"] == "module"
    assert hass_storage[STORAGE_KEY]["data"] == {"resource_id": ours[0]["id"]}
    # Someone else's resource is kept, and the card is not loaded a second time as an extra module.
    assert {"id": "other", "type": "module", "url": "/local/other-card.js"} in resources(hass)
    extra_js[0].assert_not_called()


async def test_repeated_setup_keeps_one_resource(
    hass: HomeAssistant, storage_mode: None, extra_js: tuple[MagicMock, MagicMock]
) -> None:
    for _ in range(3):
        await async_setup_card(hass)
    assert [item["url"] for item in resources(hass)] == [await current_url(hass)]


async def test_update_on_version_change(
    hass: HomeAssistant, web: None, extra_js: tuple[MagicMock, MagicMock], hass_storage: dict[str, Any]
) -> None:
    stored_resources(hass_storage, {"id": "ours", "type": "module", "url": f"{CARD_URL}?v=1.0.0-0123abcd"})
    remember_own(hass_storage, "ours")
    await setup_lovelace(hass)
    await async_setup_card(hass)
    assert resources(hass) == [{"id": "ours", "type": "module", "url": await current_url(hass)}]
    assert hass_storage[STORAGE_KEY]["data"] == {"resource_id": "ours"}


async def test_own_resource_deleted_by_hand_comes_back(
    hass: HomeAssistant, web: None, extra_js: tuple[MagicMock, MagicMock], hass_storage: dict[str, Any]
) -> None:
    stored_resources(hass_storage)
    remember_own(hass_storage, "gone")
    await setup_lovelace(hass)
    await async_setup_card(hass)
    (item,) = resources(hass)
    assert item["url"] == await current_url(hass)
    assert hass_storage[STORAGE_KEY]["data"] == {"resource_id": item["id"]}


async def test_users_own_resource_is_left_alone(
    hass: HomeAssistant, web: None, extra_js: tuple[MagicMock, MagicMock], hass_storage: dict[str, Any]
) -> None:
    theirs = {"id": "theirs", "type": "module", "url": CARD_URL}
    stored_resources(hass_storage, theirs)
    await setup_lovelace(hass)
    await async_setup_card(hass)
    assert resources(hass) == [theirs]
    assert STORAGE_KEY not in hass_storage
    extra_js[0].assert_not_called()


async def test_users_own_resource_replaces_ours(
    hass: HomeAssistant, web: None, extra_js: tuple[MagicMock, MagicMock], hass_storage: dict[str, Any]
) -> None:
    """A resource the user added later wins: the integration removes its own, so the card loads once."""
    theirs = {"id": "theirs", "type": "js", "url": f"http://ha.local:8123{CARD_URL}"}
    stored_resources(hass_storage, {"id": "ours", "type": "module", "url": f"{CARD_URL}?v=1.0.0-0123abcd"}, theirs)
    remember_own(hass_storage, "ours")
    await setup_lovelace(hass)
    await async_setup_card(hass)
    assert resources(hass) == [theirs]
    assert STORAGE_KEY not in hass_storage
    extra_js[0].assert_not_called()


async def test_yaml_mode_falls_back_to_an_extra_module(
    hass: HomeAssistant, web: None, extra_js: tuple[MagicMock, MagicMock], hass_storage: dict[str, Any]
) -> None:
    await setup_lovelace(hass, {"mode": "yaml", "resources": [{"url": "/local/other-card.js", "type": "module"}]})
    await async_setup_card(hass)
    await async_setup_card(hass)
    extra_js[0].assert_called_once_with(hass, await current_url(hass))
    assert STORAGE_KEY not in hass_storage


async def test_yaml_resource_for_the_card_is_enough(
    hass: HomeAssistant, web: None, extra_js: tuple[MagicMock, MagicMock]
) -> None:
    await setup_lovelace(hass, {"mode": "yaml", "resources": [{"url": CARD_URL, "type": "module"}]})
    await async_setup_card(hass)
    extra_js[0].assert_not_called()


async def test_without_lovelace_falls_back_to_an_extra_module(
    hass: HomeAssistant, web: None, extra_js: tuple[MagicMock, MagicMock]
) -> None:
    await async_setup_card(hass)
    extra_js[0].assert_called_once_with(hass, await current_url(hass))


async def test_without_a_web_interface_does_nothing(
    hass: HomeAssistant, extra_js: tuple[MagicMock, MagicMock], hass_storage: dict[str, Any]
) -> None:
    await async_setup_card(hass)
    assert DATA_CARD not in hass.data
    extra_js[0].assert_not_called()
    assert STORAGE_KEY not in hass_storage


async def test_failing_resource_falls_back_and_never_raises(
    hass: HomeAssistant, storage_mode: None, extra_js: tuple[MagicMock, MagicMock], caplog: pytest.LogCaptureFixture
) -> None:
    collection = hass.data[LOVELACE_DATA].resources
    with patch.object(collection, "async_create_item", side_effect=RuntimeError("boom")):
        await async_setup_card(hass)
    assert "Could not add the dashboard card to the dashboard resources" in caplog.text
    extra_js[0].assert_called_once_with(hass, await current_url(hass))
    # Once the collection works again, a later setup adds the resource; the extra module stays for this run.
    await async_setup_card(hass)
    assert [item["url"] for item in resources(hass)] == [await current_url(hass)]
    extra_js[0].assert_called_once()


async def test_failing_to_serve_never_raises(
    hass: HomeAssistant, storage_mode: None, extra_js: tuple[MagicMock, MagicMock], caplog: pytest.LogCaptureFixture
) -> None:
    with patch.object(hass.http, "register_view", side_effect=RuntimeError("boom")):
        await async_setup_card(hass)
    assert "Could not serve the dashboard card" in caplog.text
    assert resources(hass) == []


async def test_served_with_cache_headers_for_the_current_url_only(
    hass: HomeAssistant, storage_mode: None, extra_js: tuple[MagicMock, MagicMock], hass_client_no_auth
) -> None:
    await async_setup_card(hass)
    client = await hass_client_no_auth()
    response = await client.get(await current_url(hass))
    assert response.status == 200
    assert response.headers["Cache-Control"] == CACHE_FOREVER
    assert "javascript" in response.headers["Content-Type"]
    assert await response.text() == CARD_PATH.read_text()
    assert (await client.head(await current_url(hass))).status == 200
    # An old token, or a resource without one, must not keep a stale card in the browser.
    for url in (CARD_URL, f"{CARD_URL}?v=1.0.0-0123abcd"):
        response = await client.get(url)
        assert response.status == 200
        assert response.headers["Cache-Control"] == CACHE_NEVER


async def test_remove_deletes_only_its_own_resource(
    hass: HomeAssistant, web: None, extra_js: tuple[MagicMock, MagicMock], hass_storage: dict[str, Any]
) -> None:
    other = {"id": "other", "type": "module", "url": "/local/other-card.js"}
    stored_resources(hass_storage, other)
    await setup_lovelace(hass)
    await async_setup_card(hass)
    assert len(resources(hass)) == 2
    await async_remove_card(hass)
    assert resources(hass) == [other]
    assert STORAGE_KEY not in hass_storage
    extra_js[1].assert_not_called()


async def test_remove_keeps_a_resource_repointed_by_the_user(
    hass: HomeAssistant, web: None, extra_js: tuple[MagicMock, MagicMock], hass_storage: dict[str, Any]
) -> None:
    repointed = {"id": "ours", "type": "module", "url": "/local/my-fork-of-the-card.js"}
    stored_resources(hass_storage, repointed)
    remember_own(hass_storage, "ours")
    await setup_lovelace(hass)
    await async_remove_card(hass)
    assert resources(hass) == [repointed]
    assert STORAGE_KEY not in hass_storage


async def test_remove_after_a_restart_without_setup(hass: HomeAssistant, hass_storage: dict[str, Any]) -> None:
    """The entry can be removed while the integration is not set up (disabled, or failed): the resource still goes."""
    stored_resources(hass_storage, {"id": "ours", "type": "module", "url": f"{CARD_URL}?v=1.0.0-0123abcd"})
    remember_own(hass_storage, "ours")
    await setup_lovelace(hass)
    await async_remove_card(hass)
    assert resources(hass) == []
    assert STORAGE_KEY not in hass_storage


async def test_remove_drops_the_extra_module(
    hass: HomeAssistant, web: None, extra_js: tuple[MagicMock, MagicMock]
) -> None:
    await async_setup_card(hass)
    await async_remove_card(hass)
    extra_js[1].assert_called_once_with(hass, await current_url(hass))
    # Adding a projector again in the same run loads the card again.
    await async_setup_card(hass)
    assert extra_js[0].call_count == 2


async def test_remove_never_raises(
    hass: HomeAssistant, storage_mode: None, extra_js: tuple[MagicMock, MagicMock], caplog: pytest.LogCaptureFixture
) -> None:
    await async_setup_card(hass)
    collection = hass.data[LOVELACE_DATA].resources
    with patch.object(collection, "async_delete_item", side_effect=RuntimeError("boom")):
        await async_remove_card(hass)
    assert "Could not remove the dashboard card's resource" in caplog.text


@pytest.mark.usefixtures("mock_bluetooth", "mock_ble_lookup", "fake_connection")
async def test_entries_add_reload_and_remove(
    hass: HomeAssistant, extra_js: tuple[MagicMock, MagicMock], config_entry: MockConfigEntry
) -> None:
    """Through the config entries: Lovelace set up after the integration, reloads, and removing the last entry."""
    second = MockConfigEntry(domain=DOMAIN, unique_id="11:22:33:44:55:66", data={CONF_ADDRESS: "11:22:33:44:55:66"})
    config_entry.add_to_hass(hass)  # no web interface yet
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()
    assert DATA_CARD not in hass.data

    assert await async_setup_component(hass, "http", {})
    hass.config.components.add("frontend")
    await setup_lovelace(hass)
    assert await hass.config_entries.async_reload(config_entry.entry_id)
    second.add_to_hass(hass)
    assert await hass.config_entries.async_setup(second.entry_id)
    assert await hass.config_entries.async_reload(config_entry.entry_id)
    await hass.async_block_till_done()
    assert config_entry.state is ConfigEntryState.LOADED
    url = await current_url(hass)
    assert [item["url"] for item in resources(hass)] == [url]

    # Another projector is still there: the card stays.
    await hass.config_entries.async_remove(second.entry_id)
    assert [item["url"] for item in resources(hass)] == [url]
    await hass.config_entries.async_remove(config_entry.entry_id)
    await hass.async_block_till_done()
    assert resources(hass) == []
    extra_js[0].assert_not_called()


@pytest.mark.usefixtures("mock_bluetooth", "mock_ble_lookup", "fake_connection")
async def test_card_failure_does_not_fail_setup(
    hass: HomeAssistant, storage_mode: None, extra_js: tuple[MagicMock, MagicMock], config_entry: MockConfigEntry
) -> None:
    config_entry.add_to_hass(hass)
    with patch("custom_components.sony_lspx_p1.card._async_sync_resource", side_effect=RuntimeError("boom")):
        assert await hass.config_entries.async_setup(config_entry.entry_id)
        await hass.async_block_till_done()
    assert config_entry.state is ConfigEntryState.LOADED
    extra_js[0].assert_called_once()
