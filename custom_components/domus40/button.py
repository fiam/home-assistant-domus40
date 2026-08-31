"""Identification buttons for EFAPEL Domus40 devices."""

from __future__ import annotations

from homeassistant.components.button import ButtonDeviceClass, ButtonEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import Domus40ConfigEntry
from .const import REFRESH_INVENTORY_UNIQUE_ID_SUFFIX
from .entity import Domus40Entity
from .models import Domus40Device


async def async_setup_entry(
    hass: HomeAssistant,
    entry: Domus40ConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up inventory refresh and identification actions."""
    state = entry.runtime_data.coordinator.data
    entities: list[ButtonEntity] = [Domus40RefreshInventoryButton(hass, entry)]
    for device in state.devices.values():
        if device.supports_button_events or device.supports_receiver_identify:
            entities.append(Domus40DirectIdentifyButton(entry, device))
        if device.supports_receiver_identify:
            entities.append(Domus40AssociatedIdentifyButton(entry, device))
    async_add_entities(entities)


class Domus40RefreshInventoryButton(ButtonEntity):
    """Reload the integration to rebuild inventory and emitter mappings."""

    _attr_has_entity_name = True
    _attr_icon = "mdi:refresh"
    _attr_translation_key = "refresh_inventory"

    def __init__(self, hass: HomeAssistant, entry: Domus40ConfigEntry) -> None:
        """Initialize the integration-level refresh button."""
        self._hass = hass
        self._entry_id = entry.entry_id
        server_id = entry.unique_id or entry.entry_id
        self._attr_unique_id = (
            f"{server_id}-{REFRESH_INVENTORY_UNIQUE_ID_SUFFIX}"
        )

    async def async_press(self) -> None:
        """Schedule a full config-entry reload and return immediately."""
        self._hass.config_entries.async_schedule_reload(self._entry_id)


class Domus40DirectIdentifyButton(Domus40Entity, ButtonEntity):
    """Activate exactly one logical device ID's identification LED."""

    _attr_device_class = ButtonDeviceClass.IDENTIFY
    _attr_has_entity_name = True

    def __init__(self, entry: Domus40ConfigEntry, device: Domus40Device) -> None:
        """Initialize a direct identification button."""
        super().__init__(entry, device)
        self._attr_unique_id = f"{self._attr_unique_id}-identify"
        self._attr_name = (
            "Identify switch"
            if device.supports_button_events
            else f"Identify actuator — {device.name}"
        )

    async def async_press(self) -> None:
        """Activate the vendor identification LED for this exact ID."""
        await self.coordinator.async_identify_device(self._device_id)


class Domus40AssociatedIdentifyButton(Domus40Entity, ButtonEntity):
    """Identify every switch currently associated with a receiver."""

    _attr_device_class = ButtonDeviceClass.IDENTIFY
    _attr_has_entity_name = True

    def __init__(self, entry: Domus40ConfigEntry, device: Domus40Device) -> None:
        """Initialize a mapping-aware identification button."""
        super().__init__(entry, device)
        self._attr_unique_id = f"{self._attr_unique_id}-identify-associated"
        self._attr_name = f"Identify associated switches — {device.name}"

    async def async_press(self) -> None:
        """Identify every switch mapped to this exact receiver ID."""
        await self.coordinator.async_identify_associated_emitters(self._device_id)
