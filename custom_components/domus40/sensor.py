"""Power sensors for EFAPEL Domus40 metering-capable actuators."""

from __future__ import annotations

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.const import UnitOfPower
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import Domus40ConfigEntry
from .entity import Domus40Entity
from .models import Domus40Device


async def async_setup_entry(
    hass: HomeAssistant,
    entry: Domus40ConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up instantaneous power sensors for all metered actuator rows."""
    devices = entry.runtime_data.coordinator.data.metering_devices
    async_add_entities(Domus40PowerSensor(entry, device) for device in devices)


class Domus40PowerSensor(Domus40Entity, SensorEntity):
    """Instantaneous consumed power reported by one Domus40 actuator."""

    _attr_device_class = SensorDeviceClass.POWER
    _attr_native_unit_of_measurement = UnitOfPower.WATT
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_suggested_display_precision = 1
    _attr_has_entity_name = True

    def __init__(self, entry: Domus40ConfigEntry, device: Domus40Device) -> None:
        """Initialize a power sensor from the inventory row."""
        super().__init__(entry, device)
        self._attr_unique_id = f"{self._attr_unique_id}-power"
        self._attr_name = "Power"

    @property
    def native_value(self) -> float | None:
        """Return the latest milliwatt reading converted to watts."""
        reading = self.coordinator.power_readings.get(self._device_id)
        return reading.power_w if reading is not None else None

    @property
    def available(self) -> bool:
        """Return whether the device and verified metering schema are available."""
        return super().available and self.coordinator.metering_schema_compatible
