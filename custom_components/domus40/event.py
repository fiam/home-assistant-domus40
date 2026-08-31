"""Event entities for EFAPEL Domus40 wall and IR emitters."""

from __future__ import annotations

from typing import Any, ClassVar

from homeassistant.components.event import EventDeviceClass, EventEntity
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import Domus40ConfigEntry
from .const import (
    BUTTON_STATES,
    IR_BUTTON_ENDPOINTS,
    IR_ENDPOINT_LABELS,
    WALL_BUTTON_ENDPOINTS,
)
from .entity import Domus40Entity
from .models import Domus40ButtonBinding, Domus40Device
from .proto import DeviceStateEvent


async def async_setup_entry(
    hass: HomeAssistant,
    entry: Domus40ConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up physical wall keys and one optional IR receiver per emitter."""
    devices = entry.runtime_data.coordinator.data.devices.values()
    entities: list[EventEntity] = [
        Domus40ButtonEvent(entry, device, endpoint, endpoint_value)
        for device in devices
        if device.supports_wall_button_events
        for endpoint, endpoint_value in WALL_BUTTON_ENDPOINTS.items()
    ]
    entities.extend(
        Domus40IrEvent(entry, device)
        for device in devices
        if device.supports_ir_button_events
    )
    async_add_entities(entities)


class Domus40ButtonEvent(Domus40Entity, EventEntity):
    """A physical key whose existing Home Server scenario remains authoritative."""

    _attr_device_class = EventDeviceClass.BUTTON
    _attr_event_types: ClassVar[list[str]] = list(BUTTON_STATES.values())
    _attr_has_entity_name = True

    def __init__(
        self,
        entry: Domus40ConfigEntry,
        device: Domus40Device,
        endpoint: str,
        endpoint_value: int,
    ) -> None:
        """Initialize one key of a four-key controller."""
        super().__init__(entry, device)
        self._endpoint = endpoint
        self._endpoint_value = endpoint_value
        self._attr_unique_id = f"{self._attr_unique_id}-{endpoint}"

    @property
    def name(self) -> str:
        """Include the live hub assignment in the entity's display name."""
        return f"Key {self._endpoint[-1]} — {self.binding.description}"

    @property
    def binding(self) -> Domus40ButtonBinding:
        """Return the assignment discovered during entry setup."""
        return self.coordinator.button_bindings.get(
            (self._device_id, self._endpoint),
            Domus40ButtonBinding(self._device_id, self._endpoint),
        )

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Expose what the Home Server already executes for this key."""
        binding = self.binding
        return {
            "hub_endpoint": binding.endpoint,
            "hub_mapping": binding.description,
            "hub_actions": [action.as_entity_attribute() for action in binding.actions],
            "hub_mapping_status": (
                "loaded" if self.coordinator.wall_mappings_loaded else "loading"
            ),
        }

    async def async_added_to_hass(self) -> None:
        """Subscribe to decoded physical button messages."""
        await super().async_added_to_hass()
        self.async_on_remove(
            self.coordinator.async_add_button_listener(self._handle_button_event)
        )

    @callback
    def _handle_button_event(self, event: DeviceStateEvent) -> None:
        """Publish a matching key press or long-press release."""
        if event.device_id != self._device_id or event.endpoint != self._endpoint_value:
            return
        event_type = BUTTON_STATES.get(event.button_state)
        if event_type is None:
            return
        self._trigger_event(event_type)
        self.async_write_ha_state()


def _ir_event_type(endpoint: str, state: str) -> str:
    """Return a stable HA event type for one IR key transition."""
    label = IR_ENDPOINT_LABELS[endpoint].lower().replace(" ", "_")
    return f"key_{label}_{state}"


class Domus40IrEvent(Domus40Entity, EventEntity):
    """The multifunction IR receiver associated with one wall emitter."""

    _attr_device_class = EventDeviceClass.BUTTON
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_entity_registry_enabled_default = False
    _attr_event_types: ClassVar[list[str]] = [
        _ir_event_type(endpoint, state)
        for endpoint in IR_BUTTON_ENDPOINTS
        for state in BUTTON_STATES.values()
    ]
    _attr_has_entity_name = True
    _attr_name = "IR receiver"

    def __init__(self, entry: Domus40ConfigEntry, device: Domus40Device) -> None:
        """Initialize the receiver without eagerly loading sixteen mappings."""
        super().__init__(entry, device)
        self._attr_unique_id = f"{self._attr_unique_id}-ir"
        self._attr_name = "IR receiver"
        self._endpoint_names = {
            value: name for name, value in IR_BUTTON_ENDPOINTS.items()
        }

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Expose assignments after this disabled-by-default entity is enabled."""
        mappings = {}
        for endpoint in IR_BUTTON_ENDPOINTS:
            binding = self.coordinator.button_bindings.get((self._device_id, endpoint))
            if binding is not None:
                mappings[IR_ENDPOINT_LABELS[endpoint]] = binding.description
        return {
            "hub_endpoints": list(IR_ENDPOINT_LABELS.values()),
            "hub_mappings": mappings,
        }

    async def async_added_to_hass(self) -> None:
        """Subscribe to MQTT and lazily discover this receiver's mappings."""
        await super().async_added_to_hass()
        self.async_on_remove(
            self.coordinator.async_add_button_listener(self._handle_button_event)
        )
        self.coordinator.async_start_ir_binding_load(self._device_id)

    @callback
    def _handle_button_event(self, event: DeviceStateEvent) -> None:
        """Publish a matching IR key transition with its hub assignment."""
        if (
            event.device_id != self._device_id
            or event.endpoint not in self._endpoint_names
        ):
            return
        state = BUTTON_STATES.get(event.button_state)
        if state is None:
            return
        endpoint = self._endpoint_names[event.endpoint]
        binding = self.coordinator.button_bindings.get(
            (self._device_id, endpoint),
            Domus40ButtonBinding(self._device_id, endpoint),
        )
        self._trigger_event(
            _ir_event_type(endpoint, state),
            {
                "hub_endpoint": endpoint,
                "hub_mapping": binding.description,
            },
        )
        self.async_write_ha_state()
