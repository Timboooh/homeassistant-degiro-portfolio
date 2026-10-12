"""Sensors for the Degiro Portfolio integration."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import dt as dt_util

from . import DegiroConfigEntry
from .const import BASE_CURRENCY, DOMAIN, STALE_AFTER
from .coordinator import DegiroPortfolioCoordinator, PortfolioData


@dataclass(frozen=True, kw_only=True)
class DegiroSensorDescription(SensorEntityDescription):
    """Describes a portfolio sensor."""

    value_fn: Callable[[PortfolioData], float]


# state_class "total" makes Home Assistant keep a running sum in its long-term
# statistics, which the dashboard's "change over day/week/month/year" cards use.
SENSORS: tuple[DegiroSensorDescription, ...] = (
    DegiroSensorDescription(
        key="value",
        name="Value",
        icon="mdi:chart-line",
        device_class=SensorDeviceClass.MONETARY,
        state_class=SensorStateClass.TOTAL,
        native_unit_of_measurement=BASE_CURRENCY,
        suggested_display_precision=2,
        value_fn=lambda d: d.total,
    ),
    DegiroSensorDescription(
        key="investments",
        name="Investments",
        icon="mdi:chart-line",
        device_class=SensorDeviceClass.MONETARY,
        state_class=SensorStateClass.TOTAL,
        native_unit_of_measurement=BASE_CURRENCY,
        suggested_display_precision=2,
        value_fn=lambda d: d.investments,
    ),
    DegiroSensorDescription(
        key="cash",
        name="Cash",
        icon="mdi:cash",
        device_class=SensorDeviceClass.MONETARY,
        state_class=SensorStateClass.TOTAL,
        native_unit_of_measurement=BASE_CURRENCY,
        suggested_display_precision=2,
        value_fn=lambda d: d.cash,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: DegiroConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the sensors."""
    coordinator = entry.runtime_data
    async_add_entities(DegiroSensor(coordinator, desc) for desc in SENSORS)


class DegiroSensor(CoordinatorEntity[DegiroPortfolioCoordinator], SensorEntity):
    """A portfolio sensor."""

    _attr_has_entity_name = True
    # The per-position list can be large; keep it out of the database.
    _unrecorded_attributes = frozenset({"positions", "fallback_positions"})
    entity_description: DegiroSensorDescription

    def __init__(
        self,
        coordinator: DegiroPortfolioCoordinator,
        description: DegiroSensorDescription,
    ) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        entry_id = coordinator.config_entry.entry_id
        self._attr_unique_id = f"{entry_id}_{description.key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry_id)},
            name="Degiro portfolio",
            manufacturer="Degiro",
            entry_type=DeviceEntryType.SERVICE,
        )

    @property
    def available(self) -> bool:
        """Stay available on a failed refresh; `stale` tells the story."""
        return self.coordinator.data is not None

    @property
    def native_value(self) -> float | None:
        data = self.coordinator.data
        return None if data is None else self.entity_description.value_fn(data)

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        data = self.coordinator.data
        if data is None:
            return None
        now = dt_util.utcnow()
        stale = now - data.updated_at > STALE_AFTER or (
            data.holdings_updated_at is not None
            and now - data.holdings_updated_at > STALE_AFTER
        )
        attrs: dict[str, Any] = {"stale": stale}
        if self.entity_description.key == "value":
            attrs.update(
                investments=data.investments,
                cash=data.cash,
                last_updated=data.updated_at.isoformat(timespec="seconds"),
                holdings_updated_at=(
                    data.holdings_updated_at.isoformat(timespec="seconds")
                    if data.holdings_updated_at
                    else None
                ),
                fallback_positions=data.fallback_positions,
                positions=[
                    {
                        "name": p["name"],
                        "qty": p["qty"],
                        "value_eur": p["value_eur"],
                        "ticker": p["ticker"],
                    }
                    for p in data.positions
                ],
            )
        return attrs
