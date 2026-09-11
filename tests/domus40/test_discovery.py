"""Synthetic discovery regressions using the pinned HA config-entry manager."""

from __future__ import annotations

import unittest
from types import MappingProxyType
from unittest.mock import AsyncMock, patch

from homeassistant.config_entries import (
    SOURCE_IGNORE,
    SOURCE_SSDP,
    SOURCE_USER,
    ConfigEntries,
    ConfigEntry,
    ConfigEntryState,
)
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import AbortFlow
from homeassistant.helpers.service_info.ssdp import SsdpServiceInfo

from custom_components.domus40.config_flow import Domus40ConfigFlow
from custom_components.domus40.const import CONF_BASE_URL, CONF_SERVER_UDN, DOMAIN


def _discovery(udn="uuid:fixture-new", host="192.0.2.10"):
    return SsdpServiceInfo(
        ssdp_usn=f"{udn}::upnp:rootdevice",
        ssdp_st="upnp:rootdevice",
        ssdp_udn=udn,
        ssdp_location=f"http://{host}:57086/desc",
        upnp={
            "UDN": udn,
            "manufacturer": "EFAPEL",
            "deviceType": "urn:schemas-upnp-org:device:HomeServer-fixture:1",
            "friendlyName": "Fixture Home Server",
        },
    )


class DiscoveryTests(unittest.IsolatedAsyncioTestCase):
    """Preserve entity identity while deduplicating changing advertisements."""

    async def asyncSetUp(self):
        self.hass = HomeAssistant("/tmp/domus40-discovery-fixture")
        self.hass.config_entries = ConfigEntries(self.hass, {})
        self.entry = ConfigEntry(
            version=1,
            minor_version=1,
            domain=DOMAIN,
            title="Fixture Home Server",
            source=SOURCE_USER,
            unique_id="uuid:fixture-old",
            data={
                CONF_BASE_URL: "http://192.0.2.10",
                CONF_SERVER_UDN: "uuid:fixture-old",
                "username": "fixture-user",
                "password": "fixture-password",
            },
            options={"enable_metering": False},
            discovery_keys=MappingProxyType({}),
            subentries_data=[],
            state=ConfigEntryState.LOADED,
        )
        # Register only the synthetic entry; platform setup is unrelated here.
        self.hass.config_entries._entries[self.entry.entry_id] = self.entry
        self.flow = Domus40ConfigFlow()
        self.flow.hass = self.hass
        self.flow.handler = DOMAIN
        self.flow.flow_id = "fixture-flow"
        self.flow.context = {"source": SOURCE_SSDP}
        self.save = patch.object(self.hass.config_entries, "_async_schedule_save")
        self.reload = patch.object(self.hass.config_entries, "async_schedule_reload")
        self.save.start()
        self.schedule_reload = self.reload.start()
        self.addCleanup(self.save.stop)
        self.addCleanup(self.reload.stop)

    async def _result(self, coroutine):
        try:
            return await coroutine
        except AbortFlow as error:
            return {"type": "abort", "reason": error.reason}

    async def test_restarted_hub_preserves_entry_and_remembers_new_udn(self):
        before_id = self.entry.entry_id
        result = await self._result(self.flow.async_step_ssdp(_discovery()))
        self.assertEqual(result["type"], "abort")
        self.assertEqual(result["reason"], "already_configured")
        self.assertEqual(self.entry.entry_id, before_id)
        self.assertEqual(self.entry.unique_id, "uuid:fixture-old")
        self.assertEqual(self.entry.data[CONF_SERVER_UDN], "uuid:fixture-new")
        self.assertEqual(self.entry.data["password"], "fixture-password")
        self.assertEqual(self.entry.options, {"enable_metering": False})
        self.schedule_reload.assert_not_called()

    async def test_remembered_udn_follows_changed_address(self):
        self.hass.config_entries.async_update_entry(
            self.entry,
            data={**self.entry.data, CONF_SERVER_UDN: "uuid:fixture-new"},
        )
        result = await self._result(
            self.flow.async_step_ssdp(_discovery(host="192.0.2.20"))
        )
        self.assertEqual(result["reason"], "already_configured")
        self.assertEqual(self.entry.unique_id, "uuid:fixture-old")
        self.assertEqual(self.entry.data[CONF_BASE_URL], "http://192.0.2.20")
        self.schedule_reload.assert_called_once_with(self.entry.entry_id)

    async def test_original_udn_still_follows_changed_address(self):
        result = await self._result(
            self.flow.async_step_ssdp(
                _discovery(udn="uuid:fixture-old", host="192.0.2.20")
            )
        )
        self.assertEqual(result["reason"], "already_configured")
        self.assertEqual(self.entry.data[CONF_BASE_URL], "http://192.0.2.20")
        self.schedule_reload.assert_called_once_with(self.entry.entry_id)

    async def test_manual_entry_is_recognized_without_changing_entity_identity(self):
        identity = "manual:http://192.0.2.10"
        self.hass.config_entries.async_update_entry(
            self.entry,
            unique_id=identity,
            data={**self.entry.data, CONF_SERVER_UDN: identity},
        )
        result = await self._result(self.flow.async_step_ssdp(_discovery()))
        self.assertEqual(result["reason"], "already_configured")
        self.assertEqual(self.entry.unique_id, identity)
        self.assertEqual(self.entry.data[CONF_SERVER_UDN], "uuid:fixture-new")
        self.schedule_reload.assert_not_called()

    async def test_manual_duplicate_is_rejected_before_authentication(self):
        self.flow.context = {"source": SOURCE_USER}
        with patch(
            "custom_components.domus40.config_flow._validate", new_callable=AsyncMock
        ) as authenticate:
            result = await self._result(
                self.flow.async_step_manual(
                    {"host": "192.0.2.10:80", "username": "unused", "password": "unused"}
                )
            )
        self.assertEqual(result["reason"], "already_configured")
        authenticate.assert_not_awaited()

    async def test_selected_duplicate_is_rejected_before_authentication(self):
        with patch(
            "custom_components.domus40.config_flow._validate", new_callable=AsyncMock
        ) as authenticate:
            result = await self._result(
                self.flow._async_finish(
                    _discovery(), {"username": "unused", "password": "unused"}, "user"
                )
            )
        self.assertEqual(result["reason"], "already_configured")
        authenticate.assert_not_awaited()

    async def test_matching_name_at_other_address_is_a_new_hub(self):
        result = await self.flow.async_step_ssdp(_discovery(host="192.0.2.20"))
        self.assertEqual(result["type"], "form")
        self.assertEqual(result["step_id"], "confirm")
        self.assertEqual(self.entry.data[CONF_SERVER_UDN], "uuid:fixture-old")

    async def test_discovery_picker_excludes_configured_hubs(self):
        self.flow.context = {"source": SOURCE_USER}
        with patch(
            "custom_components.domus40.config_flow.ssdp.async_get_discovery_info_by_st",
            return_value=[_discovery(), _discovery("uuid:fixture-other", "192.0.2.20")],
        ):
            await self.flow._async_discover()
        self.assertEqual(set(self.flow._discoveries), {"uuid:fixture-other"})

    async def test_ignored_discovery_is_not_modified(self):
        ignored = ConfigEntry(
            version=1, minor_version=1, domain=DOMAIN, title="Ignored fixture",
            source=SOURCE_IGNORE, unique_id="uuid:fixture-new", data={}, options={},
            discovery_keys=MappingProxyType({}), subentries_data=[],
        )
        self.hass.config_entries._entries[ignored.entry_id] = ignored
        result = await self._result(self.flow.async_step_ssdp(_discovery()))
        self.assertEqual(result["reason"], "already_configured")
        self.assertEqual(ignored.data, {})
        self.assertEqual(self.entry.data[CONF_SERVER_UDN], "uuid:fixture-old")
