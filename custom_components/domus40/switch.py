"""Switch entities for EFAPEL Domus40 plugs."""

from __future__ import annotations

from typing import Any

from homeassistant.components.switch import SwitchDeviceClass, SwitchEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import Domus40ConfigEntry
from .const import TYPE_PLUGS
from .entity import Domus40Entity


async def async_setup_entry(
    hass: HomeAssistant,
    entry: Domus40ConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up all discovered switched plugs."""
    devices = entry.runtime_data.coordinator.data.devices.values()
    async_add_entities(
        Domus40Switch(entry, device)
        for device in devices
        if device.device_type in TYPE_PLUGS and device.supports_entity
    )


class Domus40Switch(Domus40Entity, SwitchEntity):
    """A Domus40 switched outlet."""

    _attr_device_class = SwitchDeviceClass.OUTLET

    @property
    def is_on(self) -> bool:
        """Return whether the plug is on."""
        return self.device.is_on

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Turn the plug on."""
        await self.async_set_domus_level(100)

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Turn the plug off."""
        await self.async_set_domus_level(0)
