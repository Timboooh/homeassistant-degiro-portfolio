"""Button to re-read the portfolio from Degiro on demand."""

from __future__ import annotations

from homeassistant.components.button import ButtonEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import DegiroConfigEntry
from .const import DOMAIN
from .coordinator import DegiroPortfolioCoordinator


async def async_setup_entry(
    hass: HomeAssistant,
    entry: DegiroConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the refresh button."""
    async_add_entities([DegiroRefreshButton(entry.runtime_data)])


class DegiroRefreshButton(ButtonEntity):
    """Press after buying or selling to pick up the new holdings right away."""

    _attr_has_entity_name = True
    _attr_name = "Refresh from Degiro"
    _attr_icon = "mdi:refresh"

    def __init__(self, coordinator: DegiroPortfolioCoordinator) -> None:
        self._coordinator = coordinator
        entry_id = coordinator.config_entry.entry_id
        self._attr_unique_id = f"{entry_id}_refresh"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry_id)},
            name="Degiro portfolio",
            manufacturer="Degiro",
            entry_type=DeviceEntryType.SERVICE,
        )

    async def async_press(self) -> None:
        await self._coordinator.async_refresh_holdings()
