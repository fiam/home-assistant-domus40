"""Cover entities for EFAPEL Domus40."""

from __future__ import annotations

from typing import Any

from homeassistant.components.cover import (
    ATTR_POSITION,
    CoverDeviceClass,
    CoverEntity,
    CoverEntityFeature,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import Domus40ConfigEntry
from .const import TYPE_BLINDS
from .entity import Domus40Entity


async def async_setup_entry(
    hass: HomeAssistant,
    entry: Domus40ConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up all discovered blind controllers."""
    devices = entry.runtime_data.coordinator.data.devices.values()
    async_add_entities(
        Domus40Cover(entry, device)
        for device in devices
        if device.device_type == TYPE_BLINDS and device.supports_entity
    )


class Domus40Cover(Domus40Entity, CoverEntity):
    """A Domus40 blind using its verified 0..100 level command."""

    _attr_device_class = CoverDeviceClass.BLIND
    _attr_supported_features = (
        CoverEntityFeature.OPEN
        | CoverEntityFeature.CLOSE
        | CoverEntityFeature.SET_POSITION
    )

    @property
    def current_cover_position(self) -> int:
        """Return the reported blind position."""
        return self.device.level

    @property
    def is_closed(self) -> bool:
        """Return whether the blind is fully closed."""
        return self.device.level == 0

    async def async_open_cover(self, **kwargs: Any) -> None:
        """Open the blind."""
        await self.async_set_domus_level(100)

    async def async_close_cover(self, **kwargs: Any) -> None:
        """Close the blind."""
        await self.async_set_domus_level(0)

    async def async_set_cover_position(self, **kwargs: Any) -> None:
        """Set the blind position."""
        await self.async_set_domus_level(kwargs[ATTR_POSITION])
