"""Power sensor contracts for reading freshness."""

from __future__ import annotations

import asyncio
import json
import unittest
from pathlib import Path

from custom_components.domus40.coordinator import Domus40Coordinator
from custom_components.domus40.models import Domus40State
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


if __name__ == "__main__":
    unittest.main()
