"""Coordinator contracts for MQTT state and bounded REST reconciliation."""

from __future__ import annotations

import asyncio
import json
import unittest
from collections import Counter
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from custom_components.domus40.coordinator import (
    REST_REFRESH_MIN_INTERVAL,
    WRITE_REFRESH_DELAYS,
    Domus40Coordinator,
    _PendingLevel,
)
from custom_components.domus40.models import Domus40State, MqttInfo

FIXTURES = Path(__file__).parent / "fixtures"
COMPONENT = Path(__file__).resolve().parents[2] / "custom_components" / "domus40"
EVENTS_SCHEMA = (COMPONENT / "events.proto").read_text()
CONSTANTS_SCHEMA = (COMPONENT / "constants.proto").read_text()
STATE_TOPIC = "fixture/events/device/101/state/changed"
STATE_PAYLOAD = b"\x0a\x00\x10\x65\xc8\x0c\x14"


def _fixture_state() -> Domus40State:
    fixture = json.loads((FIXTURES / "inventory.json").read_text())
    return Domus40State.from_api(
        fixture["devices"], fixture["divisions"], fixture["areas"]
    )


def _push_coordinator(state: Domus40State) -> Domus40Coordinator:
    coordinator = object.__new__(Domus40Coordinator)
    coordinator.client = SimpleNamespace(
        mqtt_info=MqttInfo("fixture-user", "fixture-password", "fixture/")
    )
    coordinator._entry = SimpleNamespace(options={})
    coordinator.data = state
    coordinator.schema_compatible = True
    coordinator.metering_schema_compatible = True
    coordinator._pending_levels = {}
    coordinator._recent_push_levels = {}
    coordinator._button_listeners = set()
    coordinator.push_messages_received = 0
    coordinator.push_messages_decoded = 0
    coordinator.push_decode_failures = 0
    coordinator.push_state_decode_failures = 0
    coordinator.push_metering_decode_failures = 0
    coordinator.push_schema_mismatch_messages = 0
    coordinator.push_unhandled_state_messages = 0
    coordinator.push_state_updates = 0
    coordinator.push_button_events = 0
    coordinator.push_metering_messages = 0
    coordinator.push_power_updates = 0
    coordinator.power_readings = {}
    coordinator.unknown_topic_shapes = Counter()
    coordinator.unknown_wire_signatures = Counter()
    coordinator.unknown_observations_dropped = 0
    coordinator._schedule_fallback_refresh = Mock()
    coordinator._schedule_write_refresh = Mock()

    def set_updated_data(updated: Domus40State) -> None:
        coordinator.data = updated

    coordinator.async_set_updated_data = set_updated_data
    return coordinator


class PushStateTests(unittest.IsolatedAsyncioTestCase):
    """Use valid push state directly and reserve REST for safe fallback."""

    async def asyncSetUp(self) -> None:
        self.coordinator = _push_coordinator(_fixture_state())

    async def test_decoded_state_updates_memory_without_rest_refresh(self) -> None:
        await self.coordinator._async_handle_push(STATE_TOPIC, STATE_PAYLOAD)

        self.assertEqual(self.coordinator.data.devices["101"].level, 20)
        self.assertTrue(self.coordinator.data.devices["101"].is_on)
        self.assertEqual(self.coordinator.push_state_updates, 1)
        self.assertEqual(self.coordinator.push_messages_decoded, 1)
        self.assertIn("101", self.coordinator._recent_push_levels)
        self.coordinator._schedule_fallback_refresh.assert_not_called()
        self.coordinator._schedule_write_refresh.assert_not_called()

    async def test_matching_push_confirms_optimistic_command_without_rest(self) -> None:
        self.coordinator._pending_levels["101"] = _PendingLevel(20, 999999)

        await self.coordinator._async_handle_push(STATE_TOPIC, STATE_PAYLOAD)

        self.assertNotIn("101", self.coordinator._pending_levels)
        self.assertEqual(self.coordinator.data.devices["101"].level, 20)
        self.coordinator._schedule_fallback_refresh.assert_not_called()
        self.coordinator._schedule_write_refresh.assert_not_called()

    async def test_conflicting_push_keeps_optimistic_command_until_rest(self) -> None:
        self.coordinator._pending_levels["101"] = _PendingLevel(42, 999999)

        await self.coordinator._async_handle_push(STATE_TOPIC, STATE_PAYLOAD)

        self.assertEqual(self.coordinator.data.devices["101"].level, 73)
        self.assertEqual(self.coordinator._pending_levels["101"].level, 42)
        self.coordinator._schedule_write_refresh.assert_called_once_with()
        self.coordinator._schedule_fallback_refresh.assert_not_called()

    async def test_button_push_does_not_request_rest_refresh(self) -> None:
        listener = Mock()
        self.coordinator._button_listeners.add(listener)

        await self.coordinator._async_handle_push(
            "fixture/events/device/105/state/changed",
            b"\x0a\x00\x10\x69\xa0\x06\x09\xa8\x06\x00",
        )

        listener.assert_called_once()
        self.assertEqual(self.coordinator.push_button_events, 1)
        self.coordinator._schedule_fallback_refresh.assert_not_called()
        self.coordinator._schedule_write_refresh.assert_not_called()

    async def test_decode_failure_warns_with_redacted_shape_and_signature(self) -> None:
        with self.assertLogs(
            "custom_components.domus40.coordinator", level="WARNING"
        ) as captured:
            await self.coordinator._async_handle_push(STATE_TOPIC, b"\x08\x00")

        message = "\n".join(captured.output)
        self.assertIn("topic shape=events/device/{value}/state/changed", message)
        self.assertIn("wire signature=1:0", message)
        self.assertNotIn("101", message)
        self.assertNotIn(repr(b"\x08\x00"), message)
        self.assertEqual(self.coordinator.push_state_decode_failures, 1)
        self.coordinator._schedule_fallback_refresh.assert_called_once_with()

    async def test_unhandled_state_uses_rest_fallback(self) -> None:
        await self.coordinator._async_handle_push(STATE_TOPIC, b"\x0a\x00\x10\x65")

        self.assertEqual(self.coordinator.push_unhandled_state_messages, 1)
        self.coordinator._schedule_fallback_refresh.assert_called_once_with()

    async def test_incompatible_schema_uses_rest_fallback(self) -> None:
        self.coordinator.schema_compatible = False

        await self.coordinator._async_handle_push(STATE_TOPIC, STATE_PAYLOAD)

        self.assertEqual(self.coordinator.push_schema_mismatch_messages, 1)
        self.assertEqual(self.coordinator.push_messages_decoded, 0)
        self.coordinator._schedule_fallback_refresh.assert_called_once_with()


class RefreshTests(unittest.IsolatedAsyncioTestCase):
    """Pin the global REST limiter and command backoff."""

    async def test_every_full_rest_refresh_obeys_five_second_cap(self) -> None:
        coordinator = object.__new__(Domus40Coordinator)
        coordinator._rest_refresh_lock = asyncio.Lock()
        coordinator._last_rest_refresh_at = asyncio.get_running_loop().time() - 1.0
        coordinator.rest_refresh_attempts = 0
        coordinator._pending_levels = {}
        coordinator._recent_push_levels = {}

        async def get_state() -> Domus40State:
            return _fixture_state()

        coordinator.client = SimpleNamespace(async_get_state=get_state)
        delays: list[float] = []

        async def record_sleep(delay: float) -> None:
            delays.append(delay)

        with patch("custom_components.domus40.coordinator.asyncio.sleep", record_sleep):
            state = await coordinator._async_update_data()

        self.assertEqual(state.devices["101"].level, 73)
        self.assertEqual(coordinator.rest_refresh_attempts, 1)
        self.assertEqual(len(delays), 1)
        self.assertAlmostEqual(delays[0], REST_REFRESH_MIN_INTERVAL - 1.0, delta=0.1)

    async def test_command_refreshes_use_bounded_backoff(self) -> None:
        coordinator = object.__new__(Domus40Coordinator)
        coordinator._pending_levels = {"101": _PendingLevel(20, 999999)}
        coordinator._write_refresh_task = asyncio.current_task()
        coordinator._shutting_down = False
        coordinator.write_refresh_requests = 0
        refreshes = 0
        delays: list[float] = []

        async def request_refresh() -> None:
            nonlocal refreshes
            refreshes += 1
            if refreshes == 2:
                coordinator._pending_levels.clear()

        async def record_sleep(delay: float) -> None:
            delays.append(delay)

        coordinator._async_request_coordinated_refresh = request_refresh
        with patch("custom_components.domus40.coordinator.asyncio.sleep", record_sleep):
            await coordinator._async_write_refresh_loop()

        self.assertEqual(refreshes, 2)
        self.assertEqual(coordinator.write_refresh_requests, 2)
        self.assertEqual(delays, list(WRITE_REFRESH_DELAYS[:2]))
        self.assertIsNone(coordinator._write_refresh_task)


class SchemaWarningTests(unittest.IsolatedAsyncioTestCase):
    """Surface a private-schema change without retaining its contents."""

    async def test_schema_mismatch_logs_only_fingerprints(self) -> None:
        events_schema = EVENTS_SCHEMA.replace("energyLevel = 201", "energyLevel = 8")
        constants_schema = CONSTANTS_SCHEMA

        class Client:
            mqtt_info = None

            async def async_get_events_schema(self) -> str:
                return events_schema

            async def async_get_proto_schema(self, _name: str) -> str:
                return constants_schema

        coordinator = object.__new__(Domus40Coordinator)
        coordinator.client = Client()

        with self.assertLogs(
            "custom_components.domus40.coordinator", level="WARNING"
        ) as captured:
            await coordinator.async_start_push()

        message = "\n".join(captured.output)
        self.assertIn("schema differs", message)
        self.assertIn("events=", message)
        self.assertIn("constants=", message)
        self.assertNotIn("energyLevel", message)
        self.assertFalse(coordinator.schema_compatible)


if __name__ == "__main__":
    unittest.main()
