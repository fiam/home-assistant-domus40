"""Identification entity and targeting contracts."""

from __future__ import annotations

import asyncio
import json
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import voluptuous as vol

from custom_components.domus40 import (
    _expected_registry_unique_ids,
    _split_grouped_logical_devices,
)
from custom_components.domus40.button import (
    Domus40AssociatedIdentifyButton,
    Domus40DirectIdentifyButton,
    Domus40RefreshInventoryButton,
    async_setup_entry,
)
from custom_components.domus40.config_flow import Domus40OptionsFlow
from custom_components.domus40.const import (
    CONF_IDENTIFY_DURATION_SECONDS,
    CONF_MONITOR_UNKNOWN_MESSAGES,
)
from custom_components.domus40.coordinator import Domus40Coordinator
from custom_components.domus40.light import Domus40Light
from custom_components.domus40.models import (
    Domus40ButtonBinding,
    Domus40ScenarioAction,
    Domus40State,
)

FIXTURES = Path(__file__).parent / "fixtures"


class _FakeCoordinator:
    """Minimal coordinator used by entity construction and button presses."""

    def __init__(self, state: Domus40State) -> None:
        self.data = state
        self.direct_ids: list[str] = []
        self.associated_ids: list[str] = []

    async def async_identify_device(self, device_id: str) -> None:
        self.direct_ids.append(device_id)

    async def async_identify_associated_emitters(self, device_id: str) -> None:
        self.associated_ids.append(device_id)


class IdentificationEntityTests(unittest.IsolatedAsyncioTestCase):
    """Pin the three user-visible identification actions."""

    async def asyncSetUp(self) -> None:
        fixture = json.loads((FIXTURES / "inventory.json").read_text())
        self.state = Domus40State.from_api(
            fixture["devices"], fixture["divisions"], fixture["areas"]
        )
        self.coordinator = _FakeCoordinator(self.state)
        self.entry = SimpleNamespace(
            unique_id="fixture-server",
            entry_id="fixture-entry",
            runtime_data=SimpleNamespace(coordinator=self.coordinator),
        )
        self.entities: list[Any] = []

        def add_entities(entities: list[Any]) -> None:
            self.entities.extend(entities)

        await async_setup_entry(None, self.entry, add_entities)

    async def test_entities_and_exact_direct_targets(self) -> None:
        direct = [
            entity
            for entity in self.entities
            if isinstance(entity, Domus40DirectIdentifyButton)
        ]
        associated = [
            entity
            for entity in self.entities
            if isinstance(entity, Domus40AssociatedIdentifyButton)
        ]

        self.assertEqual(
            {entity._device_id for entity in direct}, {"101", "102", "104", "105"}
        )
        self.assertEqual({entity._device_id for entity in associated}, {"101", "102"})
        self.assertNotIn(
            "fixture-server-103-identify",
            {entity.unique_id for entity in self.entities},
        )

        emitter = next(entity for entity in direct if entity._device_id == "104")
        actuator = next(entity for entity in direct if entity._device_id == "101")
        await emitter.async_press()
        await actuator.async_press()
        self.assertEqual(self.coordinator.direct_ids, ["104", "101"])

        mapped = next(entity for entity in associated if entity._device_id == "101")
        await mapped.async_press()
        self.assertEqual(self.coordinator.associated_ids, ["101"])

    async def test_refresh_inventory_schedules_one_full_entry_reload(self) -> None:
        reloads: list[str] = []
        hass = SimpleNamespace(
            config_entries=SimpleNamespace(
                async_schedule_reload=lambda entry_id: reloads.append(entry_id)
            )
        )
        button = Domus40RefreshInventoryButton(hass, self.entry)

        self.assertEqual(button.unique_id, "fixture-server-refresh-inventory")
        self.assertEqual(button.translation_key, "refresh_inventory")
        self.assertIsNone(button.device_info)

        await button.async_press()

        self.assertEqual(reloads, ["fixture-entry"])

    async def test_setup_adds_one_integration_level_refresh_action(self) -> None:
        refresh = [
            entity
            for entity in self.entities
            if isinstance(entity, Domus40RefreshInventoryButton)
        ]

        self.assertEqual(len(refresh), 1)
        self.assertIsNone(refresh[0].device_info)

    async def test_stale_cleanup_preserves_integration_refresh_action(self) -> None:
        expected = _expected_registry_unique_ids(self.entry, self.coordinator)

        self.assertIn("fixture-server-refresh-inventory", expected)

    async def test_secondary_actuator_keeps_its_own_device_and_area(self) -> None:
        first_channel = replace(
            self.state.devices["101"],
            device_type="OnOffCommuter2",
            endpoint="AtuadorOnOff1",
        )
        second_channel = replace(
            first_channel,
            device_id="106",
            division_id="11",
            division_name="Fixture second room",
            floor_name="Fixture second floor",
            ha_area_name="Fixture second room",
            endpoint="AtuadorOnOff2",
            name="Fixture second channel",
        )
        state = Domus40State(
            devices={
                **self.state.devices,
                first_channel.device_id: first_channel,
                second_channel.device_id: second_channel,
            }
        )
        entry = SimpleNamespace(
            unique_id="fixture-server",
            entry_id="fixture-entry",
            runtime_data=SimpleNamespace(coordinator=_FakeCoordinator(state)),
        )

        entity = Domus40Light(entry, second_channel)

        self.assertEqual(
            entity.device_info["identifiers"],
            {("domus40", "fixture-server-106")},
        )
        self.assertNotIn("via_device", entity.device_info)
        self.assertNotIn("via_device_id", entity.device_info)
        self.assertEqual(entity.device_info["name"], "Fixture second channel")
        self.assertNotIn("suggested_area", entity.device_info)

    async def test_existing_secondary_entities_are_moved_before_setup(self) -> None:
        first_channel = replace(
            self.state.devices["101"],
            device_type="OnOffCommuter2",
            endpoint="AtuadorOnOff1",
        )
        second_channel = replace(
            first_channel,
            device_id="106",
            division_id="11",
            division_name="Fixture second room",
            floor_name="Fixture second floor",
            ha_area_name="Fixture second room",
            endpoint="AtuadorOnOff2",
            name="Fixture second channel",
        )
        state = Domus40State(
            devices={
                **self.state.devices,
                first_channel.device_id: first_channel,
                second_channel.device_id: second_channel,
            }
        )
        primary_identifier = ("domus40", "fixture-server-101")
        secondary_identifier = ("domus40", "fixture-server-106")
        primary_device = SimpleNamespace(
            id="ha-primary",
            identifiers={primary_identifier, secondary_identifier},
        )
        child_device = SimpleNamespace(id="ha-secondary")
        registry_entries = [
            SimpleNamespace(
                unique_id="fixture-server-106",
                platform="domus40",
                device_id="ha-primary",
                entity_id="light.fixture_second_channel",
            ),
            SimpleNamespace(
                unique_id="fixture-server-106-power",
                platform="domus40",
                device_id="ha-primary",
                entity_id="sensor.fixture_second_channel_power",
            ),
        ]
        entity_updates: list[tuple[str, str]] = []
        device_updates: list[set[tuple[str, str]]] = []
        created: list[dict[str, Any]] = []

        class FakeEntityRegistry:
            def async_update_entity(self, entity_id: str, *, device_id: str) -> None:
                entity_updates.append((entity_id, device_id))

        class FakeDeviceRegistry:
            def async_get_device_by_identifier(
                self, identifier: tuple[str, str], config_entry_id: str
            ) -> Any:
                self.assert_config_entry(config_entry_id)
                return primary_device if identifier == primary_identifier else None

            @staticmethod
            def assert_config_entry(config_entry_id: str) -> None:
                if config_entry_id != "fixture-entry":
                    raise AssertionError(config_entry_id)

            def async_update_device(
                self, device_id: str, *, new_identifiers: set[tuple[str, str]]
            ) -> None:
                if device_id != primary_device.id:
                    raise AssertionError(device_id)
                device_updates.append(new_identifiers)
                primary_device.identifiers = new_identifiers

            def async_get_or_create(self, **kwargs: Any) -> Any:
                created.append(kwargs)
                return child_device

        entry = SimpleNamespace(
            unique_id="fixture-server",
            entry_id="fixture-entry",
        )
        with (
            patch(
                "custom_components.domus40.er.async_get",
                return_value=FakeEntityRegistry(),
            ),
            patch(
                "custom_components.domus40.dr.async_get",
                return_value=FakeDeviceRegistry(),
            ),
            patch(
                "custom_components.domus40.er.async_entries_for_config_entry",
                return_value=registry_entries,
            ),
        ):
            moved = _split_grouped_logical_devices(
                SimpleNamespace(), entry, SimpleNamespace(data=state)
            )

        self.assertEqual(moved, 2)
        self.assertEqual(device_updates, [{primary_identifier}])
        self.assertEqual(created[0]["identifiers"], {secondary_identifier})
        self.assertNotIn("suggested_area", created[0])
        self.assertEqual(created[0]["via_device_id"], "ha-primary")
        self.assertCountEqual(
            entity_updates,
            [
                ("light.fixture_second_channel", "ha-secondary"),
                ("sensor.fixture_second_channel_power", "ha-secondary"),
            ],
        )


class IdentificationCoordinatorTests(unittest.IsolatedAsyncioTestCase):
    """Pin exact IDs and configured duration at the coordinator boundary."""

    async def asyncSetUp(self) -> None:
        fixture = json.loads((FIXTURES / "inventory.json").read_text())
        self.state = Domus40State.from_api(
            fixture["devices"], fixture["divisions"], fixture["areas"]
        )

    async def test_direct_identification_uses_option(self) -> None:
        requests: list[tuple[str, int]] = []

        async def identify(device_id: str, duration: int) -> None:
            requests.append((device_id, duration))

        coordinator = SimpleNamespace(
            client=SimpleNamespace(async_identify_device=identify),
            _entry=SimpleNamespace(options={"identify_duration_seconds": 7}),
        )
        await Domus40Coordinator.async_identify_device(coordinator, "104")
        self.assertEqual(requests, [("104", 7)])

    async def test_associated_identification_uses_only_emitter_ids(self) -> None:
        calls: list[str] = []

        async def identify(device_id: str) -> None:
            calls.append(device_id)

        completed = asyncio.get_running_loop().create_future()
        completed.set_result(None)
        bindings = {
            ("104", "TeclaA"): Domus40ButtonBinding(
                "104",
                "TeclaA",
                (Domus40ScenarioAction("101", "Fixture dimmer", "SetOn"),),
            ),
            ("105", "TeclaA"): Domus40ButtonBinding(
                "105",
                "TeclaA",
                (Domus40ScenarioAction("101", "Fixture dimmer", "SetLevel", 42),),
            ),
            ("105", "TeclaC"): Domus40ButtonBinding(
                "105",
                "TeclaC",
                (Domus40ScenarioAction("102", "Fixture blind", "SetLevel", 0),),
            ),
        }
        coordinator = SimpleNamespace(
            _binding_task=completed,
            data=self.state,
            button_bindings=bindings,
            async_identify_device=identify,
        )

        await Domus40Coordinator.async_identify_associated_emitters(coordinator, "101")
        self.assertEqual(calls, ["104", "105"])


class IdentificationOptionsTests(unittest.IsolatedAsyncioTestCase):
    """Pin the native Home Assistant Configure form and its bounds."""

    async def test_default_and_saved_duration(self) -> None:
        entry = SimpleNamespace(options={})
        flow = Domus40OptionsFlow()
        flow.handler = "fixture-entry"
        flow.hass = SimpleNamespace(
            config_entries=SimpleNamespace(async_get_known_entry=lambda entry_id: entry)
        )

        form = await flow.async_step_init()
        schema = form["data_schema"]
        self.assertEqual(schema({})[CONF_IDENTIFY_DURATION_SECONDS], 30)
        self.assertFalse(schema({})[CONF_MONITOR_UNKNOWN_MESSAGES])
        self.assertEqual(
            schema({CONF_IDENTIFY_DURATION_SECONDS: "7"}),
            {
                CONF_IDENTIFY_DURATION_SECONDS: 7,
                CONF_MONITOR_UNKNOWN_MESSAGES: False,
            },
        )
        with self.assertRaises(vol.Invalid):
            schema({CONF_IDENTIFY_DURATION_SECONDS: 31})

        saved = await flow.async_step_init({CONF_IDENTIFY_DURATION_SECONDS: 7})
        self.assertEqual(saved["data"], {CONF_IDENTIFY_DURATION_SECONDS: 7})


if __name__ == "__main__":
    unittest.main()
