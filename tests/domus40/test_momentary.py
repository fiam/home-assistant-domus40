"""Explicit pulse-output classification and native registry migrations."""

from __future__ import annotations

import json
import unittest
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from types import MappingProxyType, SimpleNamespace
from unittest.mock import AsyncMock, patch

from homeassistant.config_entries import SOURCE_USER, ConfigEntries, ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er

from custom_components.domus40 import _async_update_listener, _migrate_output_domains
from custom_components.domus40.button import (
    Domus40MomentaryButton,
)
from custom_components.domus40.button import (
    async_setup_entry as setup_buttons,
)
from custom_components.domus40.config_flow import Domus40OptionsFlow
from custom_components.domus40.const import CONF_MOMENTARY_OUTPUTS, DOMAIN
from custom_components.domus40.light import async_setup_entry as setup_lights
from custom_components.domus40.models import Domus40State


class MomentaryTests(unittest.IsolatedAsyncioTestCase):
    """Exercise only synthetic outputs; never issue a live door command."""

    async def asyncSetUp(self):
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.hass = HomeAssistant(self.directory.name)
        self.hass.config_entries = ConfigEntries(self.hass, {})
        fixture = json.loads(
            (Path(__file__).parent / "fixtures/inventory.json").read_text()
        )
        self.state = Domus40State.from_api(
            fixture["devices"], fixture["divisions"], fixture["areas"]
        )
        relay = replace(
            self.state.devices["101"], device_id="201", name="Fixture gate",
            device_type="OnOffCommuter2", supports_level=False,
            endpoint="AtuadorOnOff1",
        )
        sibling = replace(
            relay, device_id="202", name="Fixture porch light",
            endpoint="AtuadorOnOff2",
        )
        self.state = replace(
            self.state, devices={**self.state.devices, "201": relay, "202": sibling}
        )
        self.coordinator = SimpleNamespace(
            data=self.state,
            async_set_device_level=AsyncMock(),
            async_options_updated=AsyncMock(),
        )
        self.entry = ConfigEntry(
            version=1, minor_version=1, domain=DOMAIN, title="Fixture Home Server",
            source=SOURCE_USER, unique_id="fixture-server", data={},
            options={CONF_MOMENTARY_OUTPUTS: ["201"]},
            discovery_keys=MappingProxyType({}), subentries_data=[],
        )
        self.hass.config_entries._entries[self.entry.entry_id] = self.entry
        self.entry.runtime_data = SimpleNamespace(
            coordinator=self.coordinator, momentary_output_ids=frozenset({"201"})
        )
        dr.async_setup(self.hass)
        self.devices = dr.async_get(self.hass)
        await self.devices.async_load()
        self.registry = er.async_get(self.hass)
        await self.registry.async_load()
        self.save = patch.object(self.registry, "async_schedule_save")
        self.save.start()
        self.addCleanup(self.save.stop)

    async def asyncTearDown(self):
        await self.hass.async_stop()

    async def test_only_selected_relay_becomes_a_button(self):
        buttons, lights = [], []
        await setup_buttons(self.hass, self.entry, buttons.extend)
        await setup_lights(self.hass, self.entry, lights.extend)
        pulse = [entity for entity in buttons if isinstance(entity, Domus40MomentaryButton)]
        self.assertEqual([entity._device_id for entity in pulse], ["201"])
        self.assertEqual({entity._device_id for entity in lights}, {"101", "202"})
        self.coordinator.async_set_device_level.assert_not_awaited()
        await pulse[0].async_press()
        await pulse[0].async_press()
        self.assertEqual(
            [call.args for call in self.coordinator.async_set_device_level.await_args_list],
            [("201", 100), ("201", 100)],
        )

    async def test_invalid_saved_types_cannot_convert_dimmers_or_blinds(self):
        self.hass.config_entries.async_update_entry(
            self.entry, options={CONF_MOMENTARY_OUTPUTS: ["101", "102", "103", "104"]}
        )
        buttons, lights = [], []
        await setup_buttons(self.hass, self.entry, buttons.extend)
        await setup_lights(self.hass, self.entry, lights.extend)
        self.assertFalse(any(isinstance(e, Domus40MomentaryButton) for e in buttons))
        self.assertEqual({e._device_id for e in lights}, {"101", "201", "202"})

    async def test_native_registry_migrates_both_directions_and_preserves_overrides(self):
        device = self.devices.async_get_or_create(
            config_entry_id=self.entry.entry_id,
            identifiers={(DOMAIN, "fixture-server-201")},
        )
        light = self.registry.async_get_or_create(
            "light", DOMAIN, "fixture-server-201", config_entry=self.entry,
            suggested_object_id="custom_gate", device_id=device.id,
            disabled_by=er.RegistryEntryDisabler.USER,
            hidden_by=er.RegistryEntryHider.USER,
        )
        self.registry.async_update_entity(
            light.entity_id, name="Fixture custom name", icon="mdi:gate", labels={"fixture"}
        )
        _migrate_output_domains(self.hass, self.entry)
        _migrate_output_domains(self.hass, self.entry)
        button_id = self.registry.async_get_entity_id("button", DOMAIN, light.unique_id)
        self.assertEqual(button_id, "button.custom_gate")
        self.assertIsNone(self.registry.async_get(light.entity_id))
        button = self.registry.async_get(button_id)
        self.assertEqual(button.name, "Fixture custom name")
        self.assertEqual(button.icon, "mdi:gate")
        self.assertEqual(button.labels, {"fixture"})
        self.assertEqual(button.device_id, device.id)
        self.assertEqual(button.disabled_by, er.RegistryEntryDisabler.USER)
        self.assertEqual(button.hidden_by, er.RegistryEntryHider.USER)

        self.hass.config_entries.async_update_entry(self.entry, options={})
        _migrate_output_domains(self.hass, self.entry)
        self.assertIsNone(self.registry.async_get(button_id))
        restored = self.registry.async_get(light.entity_id)
        self.assertEqual(restored.name, button.name)
        self.assertEqual(restored.device_id, device.id)
        self.coordinator.async_set_device_level.assert_not_awaited()

    async def test_options_list_only_relays_and_preserve_saved_missing_output(self):
        self.hass.config_entries.async_update_entry(
            self.entry, options={CONF_MOMENTARY_OUTPUTS: ["201", "999"], "future_option": True}
        )
        flow = Domus40OptionsFlow()
        flow.hass = self.hass
        flow.handler = self.entry.entry_id
        form = await flow.async_step_init()
        selector = next(
            value for key, value in form["data_schema"].schema.items()
            if key.schema == CONF_MOMENTARY_OUTPUTS
        )
        self.assertEqual(
            {choice["value"] for choice in selector.config["options"]},
            {"201", "202", "999"},
        )
        saved = await flow.async_step_init(
            form["data_schema"]({CONF_MOMENTARY_OUTPUTS: []})
        )
        self.assertEqual(saved["data"][CONF_MOMENTARY_OUTPUTS], [])
        self.assertTrue(saved["data"]["future_option"])

    async def test_only_classification_change_reloads_platforms(self):
        with patch.object(self.hass.config_entries, "async_reload", new_callable=AsyncMock) as reload:
            await _async_update_listener(self.hass, self.entry)
            reload.assert_not_awaited()
            self.coordinator.async_options_updated.assert_awaited_once()
            self.hass.config_entries.async_update_entry(self.entry, options={})
            await _async_update_listener(self.hass, self.entry)
            reload.assert_awaited_once_with(self.entry.entry_id)
