"""Map the Domus40 location hierarchy into Home Assistant registries."""

from __future__ import annotations

from dataclasses import dataclass

from homeassistant.core import HomeAssistant
from homeassistant.helpers import area_registry as ar
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import floor_registry as fr

from .const import DOMAIN
from .models import Domus40State


@dataclass(frozen=True, slots=True)
class LocationSyncStats:
    """Aggregate hierarchy changes safe to include in logs."""

    created_floors: int = 0
    created_areas: int = 0
    assigned_area_floors: int = 0


@dataclass(frozen=True, slots=True)
class DeviceRegistrySyncStats:
    """Aggregate device registry changes safe to include in logs."""

    assigned_areas: int = 0
    updated_links: int = 0


def ensure_location_hierarchy(
    hass: HomeAssistant, state: Domus40State
) -> tuple[dict[str, str], LocationSyncStats]:
    """Ensure every named Domus40 area/division has an HA floor/area."""
    floor_registry = fr.async_get(hass)
    area_registry = ar.async_get(hass)
    division_area_ids: dict[str, str] = {}
    created_floors = 0
    created_areas = 0
    assigned_area_floors = 0

    for location in state.locations.values():
        floor_id: str | None = None
        if location.floor_name is not None:
            floor = floor_registry.async_get_floor_by_name(location.floor_name)
            if floor is None:
                floor = floor_registry.async_create(location.floor_name)
                created_floors += 1
            floor_id = floor.floor_id

        area = area_registry.async_get_area_by_name(location.ha_area_name)
        if area is None:
            area = area_registry.async_create(
                location.ha_area_name, floor_id=floor_id
            )
            created_areas += 1
        elif floor_id is not None and area.floor_id is None:
            area = area_registry.async_update(area.id, floor_id=floor_id)
            assigned_area_floors += 1
        # A non-empty, different floor is a Home Assistant user choice. Keep it.
        division_area_ids[location.division_id] = area.id

    return division_area_ids, LocationSyncStats(
        created_floors=created_floors,
        created_areas=created_areas,
        assigned_area_floors=assigned_area_floors,
    )


def sync_device_registry(
    hass: HomeAssistant,
    *,
    config_entry_id: str,
    server_id: str,
    state: Domus40State,
    division_area_ids: dict[str, str],
) -> DeviceRegistrySyncStats:
    """Reconcile sibling links and conservative Home Assistant area placement."""
    area_registry = ar.async_get(hass)
    device_registry = dr.async_get(hass)
    registry_devices = {
        device.device_id: registry_device
        for device in state.devices.values()
        if (
            registry_device := device_registry.async_get_device_by_identifier(
                (DOMAIN, f"{server_id}-{device.device_id}"), config_entry_id
            )
        )
        is not None
    }
    assigned_areas = 0
    updated_links = 0
    for device in state.devices.values():
        if (registry_device := registry_devices.get(device.device_id)) is None:
            continue
        updates: dict[str, str | None] = {}

        primary = state.primary_device(device.device_id)
        desired_via_device_id: str | None = None
        if primary.device_id != device.device_id and primary.supports_entity:
            if (primary_entry := registry_devices.get(primary.device_id)) is None:
                # Entity setup should have registered the primary first. If it
                # did not, preserve the current relationship for this refresh.
                desired_via_device_id = getattr(
                    registry_device, "via_device_id", None
                )
            else:
                desired_via_device_id = primary_entry.id
        if (
            getattr(registry_device, "via_device_id", None)
            != desired_via_device_id
        ):
            updates["via_device_id"] = desired_via_device_id
            updated_links += 1

        if device.division_id is not None and (
            target_area_id := division_area_ids.get(device.division_id)
        ) is not None and registry_device.area_id != target_area_id:
            default_area_ids: set[str | None] = {None}
            if device.division_name is not None and (
                old_area := area_registry.async_get_area_by_name(device.division_name)
            ) is not None:
                default_area_ids.add(old_area.id)
            if registry_device.area_id in default_area_ids:
                updates["area_id"] = target_area_id
                assigned_areas += 1

        if updates:
            device_registry.async_update_device(registry_device.id, **updates)

    return DeviceRegistrySyncStats(
        assigned_areas=assigned_areas,
        updated_links=updated_links,
    )
