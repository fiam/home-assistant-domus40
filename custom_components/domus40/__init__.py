"""EFAPEL Domus40 integration."""

from __future__ import annotations

import logging
from dataclasses import dataclass

import aiohttp
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.aiohttp_client import async_create_clientsession

from .api import Domus40Client
from .const import (
    CONF_BASE_URL,
    DOMAIN,
    MANUFACTURER,
    PLATFORMS,
    REFRESH_INVENTORY_UNIQUE_ID_SUFFIX,
    WALL_BUTTON_ENDPOINTS,
)
from .coordinator import Domus40Coordinator
from .locations import ensure_location_hierarchy, sync_device_registry
from .models import Domus40Device

_LOGGER = logging.getLogger(__name__)


@dataclass(slots=True)
class Domus40RuntimeData:
    """Objects owned by a loaded config entry."""

    coordinator: Domus40Coordinator
    session: aiohttp.ClientSession


type Domus40ConfigEntry = ConfigEntry[Domus40RuntimeData]


def _device_entity_unique_ids(
    server_id: str, device: Domus40Device
) -> set[str]:
    """Return every registry identity owned by one logical device row."""
    base = f"{server_id}-{device.device_id}"
    expected: set[str] = set()
    if device.supports_entity:
        expected.add(base)
    if device.supports_wall_button_events:
        expected.update(f"{base}-{endpoint}" for endpoint in WALL_BUTTON_ENDPOINTS)
    if device.supports_ir_button_events:
        expected.add(f"{base}-ir")
    if device.supports_button_events or device.supports_receiver_identify:
        expected.add(f"{base}-identify")
    if device.supports_receiver_identify:
        expected.add(f"{base}-identify-associated")
    if device.supports_metering:
        expected.add(f"{base}-power")
    return expected


async def _async_update_listener(
    hass: HomeAssistant, entry: Domus40ConfigEntry
) -> None:
    """Apply options without rebuilding inventory and emitter mappings."""
    await entry.runtime_data.coordinator.async_options_updated()


def _expected_registry_unique_ids(
    entry: Domus40ConfigEntry, coordinator: Domus40Coordinator
) -> set[str]:
    """Return every entity identity produced by the current device model."""
    if coordinator.data is None:
        return set()
    server_id = entry.unique_id or entry.entry_id
    expected = {f"{server_id}-{REFRESH_INVENTORY_UNIQUE_ID_SUFFIX}"}
    for device in coordinator.data.devices.values():
        expected.update(_device_entity_unique_ids(server_id, device))
    return expected


def _split_grouped_logical_devices(
    hass: HomeAssistant,
    entry: Domus40ConfigEntry,
    coordinator: Domus40Coordinator,
) -> int:
    """Move existing secondary-channel entities out of a grouped HA device."""
    state = coordinator.data
    if state is None:
        return 0
    server_id = entry.unique_id or entry.entry_id
    entity_registry = er.async_get(hass)
    device_registry = dr.async_get(hass)
    entries_by_unique_id = {
        item.unique_id: item
        for item in er.async_entries_for_config_entry(entity_registry, entry.entry_id)
        if item.platform == DOMAIN
    }
    moved = 0
    for device in state.devices.values():
        primary = state.primary_device(device.device_id)
        if primary.device_id == device.device_id or not primary.supports_entity:
            continue
        row_entries = [
            entries_by_unique_id[unique_id]
            for unique_id in _device_entity_unique_ids(server_id, device)
            if unique_id in entries_by_unique_id
        ]
        current_device_ids = {item.device_id for item in row_entries}
        if len(current_device_ids) != 1 or None in current_device_ids:
            continue
        primary_identifier = (DOMAIN, f"{server_id}-{primary.device_id}")
        primary_entry = device_registry.async_get_device_by_identifier(
            primary_identifier, entry.entry_id
        )
        current_device_id = next(iter(current_device_ids))
        if primary_entry is None or current_device_id != primary_entry.id:
            continue

        logical_identifier = (DOMAIN, f"{server_id}-{device.device_id}")
        if logical_identifier in primary_entry.identifiers:
            device_registry.async_update_device(
                primary_entry.id,
                new_identifiers=set(primary_entry.identifiers) - {logical_identifier},
            )
        logical_entry = device_registry.async_get_or_create(
            config_entry_id=entry.entry_id,
            identifiers={logical_identifier},
            manufacturer=MANUFACTURER,
            model=(
                f"{device.device_type} emitter"
                if device.supports_button_events
                else device.device_type
            ),
            name=device.name,
            sw_version=device.firmware_version,
            via_device_id=primary_entry.id,
        )
        for row_entry in row_entries:
            entity_registry.async_update_entity(
                row_entry.entity_id, device_id=logical_entry.id
            )
            moved += 1
    return moved


def _remove_stale_registry_entries(
    hass: HomeAssistant,
    entry: Domus40ConfigEntry,
    coordinator: Domus40Coordinator,
) -> tuple[int, int]:
    """Remove entities and devices orphaned by integration model migrations."""
    expected_unique_ids = _expected_registry_unique_ids(entry, coordinator)
    entity_registry = er.async_get(hass)
    removed_entities = 0
    for entity_entry in er.async_entries_for_config_entry(
        entity_registry, entry.entry_id
    ):
        if (
            entity_entry.platform == DOMAIN
            and entity_entry.unique_id not in expected_unique_ids
        ):
            entity_registry.async_remove(entity_entry.entity_id)
            hass.states.async_remove(entity_entry.entity_id)
            removed_entities += 1

    used_device_ids = {
        entity_entry.device_id
        for entity_entry in er.async_entries_for_config_entry(
            entity_registry, entry.entry_id
        )
        if entity_entry.platform == DOMAIN and entity_entry.device_id is not None
    }
    device_registry = dr.async_get(hass)
    removed_devices = 0
    for device_entry in dr.async_entries_for_config_entry(
        device_registry, entry.entry_id
    ):
        if device_entry.id not in used_device_ids:
            device_registry.async_remove_device(device_entry.id)
            removed_devices += 1

    return removed_entities, removed_devices


async def async_setup_entry(hass: HomeAssistant, entry: Domus40ConfigEntry) -> bool:
    """Set up a Domus40 Home Server from a config entry."""
    session = async_create_clientsession(
        hass, cookie_jar=aiohttp.CookieJar(unsafe=True)
    )
    client = Domus40Client(
        entry.data[CONF_BASE_URL],
        entry.data[CONF_USERNAME],
        entry.data[CONF_PASSWORD],
        session,
    )
    coordinator = Domus40Coordinator(hass, entry, client, session)
    await coordinator.async_config_entry_first_refresh()
    coordinator.initialize_button_bindings()
    entry.runtime_data = Domus40RuntimeData(coordinator, session)
    entry.async_on_unload(entry.add_update_listener(_async_update_listener))

    assert coordinator.data is not None
    division_area_ids, location_stats = ensure_location_hierarchy(
        hass, coordinator.data
    )
    moved_entities = _split_grouped_logical_devices(hass, entry, coordinator)
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    device_registry_stats = sync_device_registry(
        hass,
        config_entry_id=entry.entry_id,
        server_id=entry.unique_id or entry.entry_id,
        state=coordinator.data,
        division_area_ids=division_area_ids,
    )
    removed_entities, removed_devices = _remove_stale_registry_entries(
        hass, entry, coordinator
    )
    if (
        moved_entities
        or device_registry_stats.assigned_areas
        or device_registry_stats.updated_links
        or removed_entities
        or removed_devices
        or location_stats.created_floors
        or location_stats.created_areas
        or location_stats.assigned_area_floors
    ):
        _LOGGER.info(
            "Created %s floors and %s areas, assigned %s areas to floors and "
            "%s devices to areas, updated %s physical sibling links, moved %s "
            "Domus40 entities, removed %s stale entities and %s orphaned devices",
            location_stats.created_floors,
            location_stats.created_areas,
            location_stats.assigned_area_floors,
            device_registry_stats.assigned_areas,
            device_registry_stats.updated_links,
            moved_entities,
            removed_entities,
            removed_devices,
        )
    await coordinator.async_start_push()
    coordinator.async_start_button_binding_load()
    coordinator.async_start_reporting()
    return True


async def async_unload_entry(hass: HomeAssistant, entry: Domus40ConfigEntry) -> bool:
    """Unload a Domus40 config entry."""
    if not await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        return False
    await entry.runtime_data.coordinator.async_shutdown()
    await entry.runtime_data.session.close()
    return True
