"""Entity helpers for EFAPEL Domus40."""

from __future__ import annotations

from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import Domus40ConfigEntry
from .const import DOMAIN, MANUFACTURER
from .coordinator import Domus40Coordinator
from .models import Domus40Device


class Domus40Entity(CoordinatorEntity[Domus40Coordinator]):
    """Base class for a Domus40 device entity."""

    _attr_has_entity_name = False

    def __init__(self, entry: Domus40ConfigEntry, device: Domus40Device) -> None:
        """Initialize an entity from the first inventory snapshot."""
        super().__init__(entry.runtime_data.coordinator)
        self._entry = entry
        self._device_id = device.device_id
        server_id = entry.unique_id or entry.entry_id
        self._attr_unique_id = f"{server_id}-{device.device_id}"
        self._attr_name = device.name
        # Domus40 assigns names and divisions to logical rows, including
        # separate output channels on one physical module. Give every row its
        # own HA device so a sibling cannot inherit the primary row's area or
        # produce a misleading combined name. Physical relationships are
        # reconciled by registry id after every entity platform is set up.
        registry_device = device
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, f"{server_id}-{registry_device.device_id}")},
            manufacturer=MANUFACTURER,
            model=(
                f"{registry_device.device_type} emitter"
                if registry_device.supports_button_events
                else registry_device.device_type
            ),
            name=registry_device.name,
            sw_version=registry_device.firmware_version,
        )

    @property
    def device(self) -> Domus40Device:
        """Return this device from the latest snapshot."""
        return self.coordinator.data.devices[self._device_id]

    @property
    def available(self) -> bool:
        """Return whether the device remains in a successful snapshot."""
        return (
            super().available
            and self.coordinator.data is not None
            and self._device_id in self.coordinator.data.devices
        )

    async def async_set_domus_level(self, level: int) -> None:
        """Write a level and optimistically update the coordinator."""
        await self.coordinator.async_set_device_level(self._device_id, level)
