"""The Degiro Portfolio integration."""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store

from .const import DOMAIN, STORE_VERSION
from .coordinator import DegiroPortfolioCoordinator

PLATFORMS = [Platform.SENSOR, Platform.BUTTON]

type DegiroConfigEntry = ConfigEntry[DegiroPortfolioCoordinator]


async def async_setup_entry(hass: HomeAssistant, entry: DegiroConfigEntry) -> bool:
    """Set up Degiro Portfolio from a config entry."""
    coordinator = DegiroPortfolioCoordinator(hass, entry)
    await coordinator.async_config_entry_first_refresh()
    entry.runtime_data = coordinator

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_async_options_updated))
    return True


async def async_unload_entry(hass: HomeAssistant, entry: DegiroConfigEntry) -> bool:
    """Unload a config entry."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def async_remove_entry(hass: HomeAssistant, entry: DegiroConfigEntry) -> None:
    """Delete the cached holdings when the entry is removed."""
    await Store(hass, STORE_VERSION, f"{DOMAIN}.{entry.entry_id}").async_remove()


async def _async_options_updated(hass: HomeAssistant, entry: DegiroConfigEntry) -> None:
    """Reload so a changed interval or ticker override takes effect."""
    await hass.config_entries.async_reload(entry.entry_id)
