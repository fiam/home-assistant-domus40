"""Power sensor contracts for reading freshness."""

from __future__ import annotations

import asyncio
import json
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from homeassistant.core import HomeAssistant

from custom_components.domus40.coordinator import Domus40Coordinator
from custom_components.domus40.models import Domus40State, MqttInfo
from custom_components.domus40.proto import DeviceInstantReadingEvent
from custom_components.domus40.sensor import Domus40PowerSensor

FIXTURES = Path(__file__).parent / "fixtures"


def _fixture_state() -> Domus40State:
    fixture = json.loads((FIXTURES / "inventory.json").read_text())
    return Domus40State.from_api(
        fixture["devices"], fixture["divisions"], fixture["areas"]
    )


class PowerSensorFreshnessTests(unittest.IsolatedAsyncioTestCase):
    """A stopped push pipeline must not read as a live power value."""

    async def test_stale_reading_becomes_unknown(self) -> None:
        coordinator = object.__new__(Domus40Coordinator)
        coordinator.data = _fixture_state()
        coordinator.power_readings = {
            "101": DeviceInstantReadingEvent("101", consumed_mw=12345)
        }
        now = asyncio.get_running_loop().time()
        coordinator.power_reading_recorded_at = {"101": now}
        sensor = object.__new__(Domus40PowerSensor)
        sensor.coordinator = coordinator
        sensor._device_id = "101"

        self.assertEqual(sensor.native_value, 12.345)

        coordinator.power_reading_recorded_at["101"] = (
            now - coordinator.power_reading_max_age_seconds() - 1
        )

        self.assertIsNone(sensor.native_value)

    async def test_missing_reading_is_unknown(self) -> None:
        coordinator = object.__new__(Domus40Coordinator)
        coordinator.data = _fixture_state()
        coordinator.power_readings = {}
        coordinator.power_reading_recorded_at = {}
        sensor = object.__new__(Domus40PowerSensor)
        sensor.coordinator = coordinator
        sensor._device_id = "101"

        self.assertIsNone(sensor.native_value)


class PowerSensorNotificationTests(unittest.IsolatedAsyncioTestCase):
    """Expire the displayed state even when polling and push stay silent."""

    async def asyncSetUp(self) -> None:
        self.client = SimpleNamespace(
            mqtt_info=MqttInfo("fixture-user", "fixture-password", "fixture/"),
            async_get_state=AsyncMock(return_value=_fixture_state()),
            async_logout=AsyncMock(),
        )
        self.coordinator = Domus40Coordinator(
            HomeAssistant("/tmp/domus40-fixture"), None, self.client, None
        )
        self.coordinator._entry = SimpleNamespace(options={})
        self.coordinator.update_interval = None
        self.coordinator.data = _fixture_state()
        self.coordinator.metering_schema_compatible = True
        self.coordinator.power_reading_max_age_seconds = Mock(return_value=0.05)
        entry = SimpleNamespace(
            unique_id="fixture-server",
            entry_id="fixture-entry",
            runtime_data=SimpleNamespace(coordinator=self.coordinator),
        )
        self.sensor = Domus40PowerSensor(entry, self.coordinator.data.devices["101"])
        self.observations: list[float | None] = []
        self.expired = asyncio.Event()

        def updated() -> None:
            value = self.sensor.native_value
            self.observations.append(value)
            if value is None:
                self.expired.set()

        self.remove_listener = self.coordinator.async_add_listener(updated)

    async def asyncTearDown(self) -> None:
        self.remove_listener()
        await self.coordinator.async_shutdown()

    async def publish_reading(self) -> None:
        await self.coordinator._async_handle_push(
            "fixture/events/device/101/reading/instant",
            b"\x0a\x00\x10\x65\x60\xb9\x60",
        )

    async def test_silent_pipeline_notifies_unknown_without_rest(self) -> None:
        await self.publish_reading()
        self.assertEqual(self.observations, [12.345])

        await asyncio.wait_for(self.expired.wait(), 1)

        self.assertEqual(self.observations, [12.345, None])
        self.client.async_get_state.assert_not_awaited()
        await self.publish_reading()
        self.assertEqual(self.observations[-1], 12.345)

    async def test_shutdown_cancels_expiry_notifications(self) -> None:
        await self.publish_reading()
        await self.coordinator.async_shutdown()
        await asyncio.sleep(0.1)

        self.assertEqual(self.observations, [12.345])

    async def test_fresh_reading_postpones_unknown_notification(self) -> None:
        self.coordinator.power_reading_max_age_seconds.return_value = 0.1
        await self.publish_reading()
        await asyncio.sleep(0.06)
        await self.publish_reading()
        await asyncio.sleep(0.05)

        self.assertEqual(self.observations, [12.345, 12.345])
        await asyncio.wait_for(self.expired.wait(), 1)
        self.assertEqual(self.observations, [12.345, 12.345, None])


if __name__ == "__main__":
    unittest.main()
