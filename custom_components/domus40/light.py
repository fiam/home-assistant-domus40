"""Light entities for EFAPEL Domus40."""

from __future__ import annotations

from typing import Any

from homeassistant.components.light import ATTR_BRIGHTNESS, ColorMode, LightEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import Domus40ConfigEntry
from .const import TYPE_DIMMER, TYPE_LIGHTS
from .entity import Domus40Entity


async def async_setup_entry(
    hass: HomeAssistant,
    entry: Domus40ConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up all discovered lighting actuators."""
    devices = entry.runtime_data.coordinator.data.devices.values()
    async_add_entities(
        Domus40Light(entry, device)
        for device in devices
        if device.device_type in TYPE_LIGHTS and device.supports_entity
    )


class Domus40Light(Domus40Entity, LightEntity):
    """A switched or dimmable Domus40 light."""

    def __init__(self, entry: Domus40ConfigEntry, device: Any) -> None:
        """Initialize the light."""
        super().__init__(entry, device)
        self._dimmable = device.device_type == TYPE_DIMMER or device.supports_level
        self._attr_color_mode = (
            ColorMode.BRIGHTNESS if self._dimmable else ColorMode.ONOFF
        )
        self._attr_supported_color_modes = {self._attr_color_mode}

    @property
    def is_on(self) -> bool:
        """Return whether the output is on."""
        return self.device.is_on

    @property
    def brightness(self) -> int | None:
        """Return brightness on Home Assistant's 0..255 scale."""
        if not self._dimmable:
            return None
        return round(self.device.level * 255 / 100)

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Turn on or set the light level."""
        brightness = kwargs.get(ATTR_BRIGHTNESS)
        level = round(brightness * 100 / 255) if brightness is not None else 100
        await self.async_set_domus_level(level)

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Turn the light off."""
        await self.async_set_domus_level(0)
