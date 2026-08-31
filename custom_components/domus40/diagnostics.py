"""Privacy-preserving diagnostics for EFAPEL Domus40."""

from __future__ import annotations

from collections import Counter
from typing import Any

from homeassistant.core import HomeAssistant

from . import Domus40ConfigEntry


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: Domus40ConfigEntry
) -> dict[str, Any]:
    """Return no hostnames, device identifiers, names, or credentials."""
    coordinator = entry.runtime_data.coordinator
    devices = coordinator.data.devices.values() if coordinator.data else []
    type_counts = Counter(device.device_type for device in devices)
    return {
        "entry": {
            "configured": True,
            "coordinator_update_success": coordinator.last_update_success,
            "username_present": bool(entry.data.get("username")),
            "password_present": bool(entry.data.get("password")),
        },
        "inventory": {
            "device_count": sum(type_counts.values()),
            "physical_device_count": (
                coordinator.data.physical_device_count if coordinator.data else 0
            ),
            "device_types": dict(sorted(type_counts.items())),
            "actionable_count": sum(device.supports_entity for device in devices),
            "emitter_count": sum(device.supports_button_events for device in devices),
            "level_capable_count": sum(device.supports_level for device in devices),
            "on_off_capable_count": sum(device.supports_on_off for device in devices),
            "metering_capable_row_count": sum(
                device.supports_metering for device in devices
            ),
            "metering_target_count": len(coordinator.data.metering_devices),
            "quad_button_count": sum(
                device.device_type == "QuadPressureButton" for device in devices
            ),
            "wall_key_entity_count": sum(
                4 for device in devices if device.supports_wall_button_events
            ),
            "ir_receiver_count": sum(
                device.supports_ir_button_events for device in devices
            ),
            "button_binding_count": len(coordinator.button_bindings),
            "button_mapping_failures": coordinator.button_mapping_failures,
            "wall_mappings_loaded": coordinator.wall_mappings_loaded,
        },
        "locations": {
            "division_count": len(coordinator.data.locations),
            "floor_count": len(
                {
                    location.floor_name
                    for location in coordinator.data.locations.values()
                    if location.floor_name is not None
                }
            ),
            "disambiguated_division_count": sum(
                location.ha_area_name != location.division_name
                for location in coordinator.data.locations.values()
            ),
        },
        "push": {
            "mqtt_available": coordinator.client.mqtt_info is not None,
            "schema_compatible": coordinator.schema_compatible,
            "schema_fingerprint": coordinator.schema_fingerprint,
            "constants_schema_fingerprint": (coordinator.constants_schema_fingerprint),
            "device_state_fields": coordinator.schema_fields,
            "messages_received": coordinator.push_messages_received,
            "messages_decoded": coordinator.push_messages_decoded,
            "decode_failures": coordinator.push_decode_failures,
            "state_updates": coordinator.push_state_updates,
            "button_events": coordinator.push_button_events,
            "pending_write_count": coordinator.pending_write_count,
        },
        "metering": {
            "schema_compatible": coordinator.metering_schema_compatible,
            "instant_reading_fields": coordinator.metering_schema_fields,
            "reporting_active_count": len(coordinator.reporting_active_ids),
            "reporting_target_count": len(coordinator.data.metering_devices),
            "reporting_activation_failures": (
                coordinator.reporting_activation_failures
            ),
            "messages_received": coordinator.push_metering_messages,
            "power_updates": coordinator.push_power_updates,
            "devices_with_readings": len(coordinator.power_readings),
        },
        "unknown_message_monitor": {
            "enabled": coordinator.unknown_monitoring_enabled,
            "topic_shapes": dict(sorted(coordinator.unknown_topic_shapes.items())),
            "wire_signatures": dict(
                sorted(coordinator.unknown_wire_signatures.items())
            ),
            "observations_dropped": coordinator.unknown_observations_dropped,
        },
    }
