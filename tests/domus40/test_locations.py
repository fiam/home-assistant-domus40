"""Home Assistant floor and area registry contracts."""

from __future__ import annotations

import json
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

from custom_components.domus40.locations import (
    ensure_location_hierarchy,
    sync_device_registry,
)
from custom_components.domus40.models import Domus40State

FIXTURES = Path(__file__).parent / "fixtures"


class _FakeFloorRegistry:
    def __init__(self) -> None:
        self.floors: dict[str, Any] = {}

    def async_get_floor_by_name(self, name: str) -> Any:
        return self.floors.get(name.casefold().replace(" ", ""))

    def async_create(self, name: str) -> Any:
        floor = SimpleNamespace(floor_id=f"floor-{len(self.floors) + 1}", name=name)
        self.floors[name.casefold().replace(" ", "")] = floor
        return floor


class _FakeAreaRegistry:
    def __init__(self) -> None:
        self.areas: dict[str, Any] = {}

    def add(self, area_id: str, name: str, floor_id: str | None = None) -> Any:
        area = SimpleNamespace(id=area_id, name=name, floor_id=floor_id)
        self.areas[name.casefold().replace(" ", "")] = area
        return area

    def async_get_area_by_name(self, name: str) -> Any:
        return self.areas.get(name.casefold().replace(" ", ""))

    def async_create(self, name: str, *, floor_id: str | None = None) -> Any:
        return self.add(f"area-{len(self.areas) + 1}", name, floor_id)

    def async_update(self, area_id: str, *, floor_id: str) -> Any:
        area = next(item for item in self.areas.values() if item.id == area_id)
        area.floor_id = floor_id
        return area


class _FakeDeviceRegistry:
    def __init__(self, devices: dict[tuple[str, str], Any]) -> None:
        self.devices = devices
        self.updates: list[tuple[str, dict[str, str | None]]] = []

    def async_get_device_by_identifier(
        self, identifier: tuple[str, str], config_entry_id: str
    ) -> Any:
        if config_entry_id != "fixture-entry":
            raise AssertionError(config_entry_id)
        return self.devices.get(identifier)

    def async_update_device(
        self, device_id: str, **changes: str | None
    ) -> None:
        device = next(item for item in self.devices.values() if item.id == device_id)
        for key, value in changes.items():
            setattr(device, key, value)
        self.updates.append((device_id, changes))


class LocationRegistryTests(unittest.TestCase):
    """Pin hierarchy creation, migration, and user-area preservation."""

    def test_hierarchy_and_conservative_device_assignment(self) -> None:
        fixture = json.loads((FIXTURES / "inventory.json").read_text())
        state = Domus40State.from_api(
            [
                {**fixture["devices"][0], "id": 201, "division": 10},
                {**fixture["devices"][1], "id": 202, "division": 11},
                {**fixture["devices"][2], "id": 203, "division": 12},
            ],
            [
                {"id": 10, "name": "Fixture hall", "area": 1},
                {"id": 11, "name": "Fixture hall", "area": 2},
                {"id": 12, "name": "Fixture studio", "area": 1},
            ],
            [
                {"id": 1, "name": "Fixture lower floor"},
                {"id": 2, "name": "Fixture upper floor"},
            ],
        )
        floors = _FakeFloorRegistry()
        areas = _FakeAreaRegistry()
        old_hall = areas.add("old-hall", "Fixture hall")
        studio = areas.add("studio", "Fixture studio")

        with (
            patch(
                "custom_components.domus40.locations.fr.async_get",
                return_value=floors,
            ),
            patch(
                "custom_components.domus40.locations.ar.async_get",
                return_value=areas,
            ),
        ):
            division_area_ids, stats = ensure_location_hierarchy(
                SimpleNamespace(), state
            )

        self.assertEqual(stats.created_floors, 2)
        self.assertEqual(stats.created_areas, 2)
        self.assertEqual(stats.assigned_area_floors, 1)
        self.assertEqual(
            studio.floor_id,
            floors.async_get_floor_by_name("Fixture lower floor").floor_id,
        )

        custom_area = areas.add("custom", "Fixture custom area")
        registry = _FakeDeviceRegistry(
            {
                ("domus40", "fixture-server-201"): SimpleNamespace(
                    id="device-201", area_id=old_hall.id, via_device_id=None
                ),
                ("domus40", "fixture-server-202"): SimpleNamespace(
                    id="device-202", area_id=custom_area.id, via_device_id=None
                ),
                ("domus40", "fixture-server-203"): SimpleNamespace(
                    id="device-203", area_id=None, via_device_id=None
                ),
            }
        )
        with (
            patch(
                "custom_components.domus40.locations.ar.async_get",
                return_value=areas,
            ),
            patch(
                "custom_components.domus40.locations.dr.async_get",
                return_value=registry,
            ),
        ):
            stats = sync_device_registry(
                SimpleNamespace(),
                config_entry_id="fixture-entry",
                server_id="fixture-server",
                state=state,
                division_area_ids=division_area_ids,
            )

        self.assertEqual(stats.assigned_areas, 2)
        self.assertEqual(stats.updated_links, 0)
        self.assertEqual(
            registry.devices[("domus40", "fixture-server-201")].area_id,
            division_area_ids["10"],
        )
        self.assertEqual(
            registry.devices[("domus40", "fixture-server-202")].area_id,
            custom_area.id,
        )
        self.assertEqual(
            registry.devices[("domus40", "fixture-server-203")].area_id,
            division_area_ids["12"],
        )

    def test_sibling_links_use_registry_ids_and_clear_stale_links(self) -> None:
        fixture = json.loads((FIXTURES / "inventory.json").read_text())
        state = Domus40State.from_api(
            fixture["devices"], fixture["divisions"], fixture["areas"]
        )
        secondary = replace(
            state.devices["104"],
            division_id="11",
            division_name="Fixture secondary room",
        )
        state = Domus40State(
            devices={**state.devices, secondary.device_id: secondary},
            locations=state.locations,
        )
        registry = _FakeDeviceRegistry(
            {
                ("domus40", "fixture-server-101"): SimpleNamespace(
                    id="device-101", area_id=None, via_device_id=None
                ),
                ("domus40", "fixture-server-104"): SimpleNamespace(
                    id="device-104", area_id=None, via_device_id=None
                ),
                ("domus40", "fixture-server-105"): SimpleNamespace(
                    id="device-105", area_id=None, via_device_id="stale-parent"
                ),
            }
        )
        with (
            patch(
                "custom_components.domus40.locations.ar.async_get",
                return_value=_FakeAreaRegistry(),
            ),
            patch(
                "custom_components.domus40.locations.dr.async_get",
                return_value=registry,
            ),
        ):
            stats = sync_device_registry(
                SimpleNamespace(),
                config_entry_id="fixture-entry",
                server_id="fixture-server",
                state=state,
                division_area_ids={"10": "area-primary", "11": "area-secondary"},
            )

        self.assertEqual(stats.assigned_areas, 3)
        self.assertEqual(stats.updated_links, 2)
        self.assertEqual(
            registry.devices[("domus40", "fixture-server-104")].via_device_id,
            "device-101",
        )
        self.assertEqual(
            registry.devices[("domus40", "fixture-server-101")].area_id,
            "area-primary",
        )
        self.assertEqual(
            registry.devices[("domus40", "fixture-server-104")].area_id,
            "area-secondary",
        )
        self.assertIsNone(
            registry.devices[("domus40", "fixture-server-105")].via_device_id
        )
