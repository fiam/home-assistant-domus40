"""Synthetic contracts for aggregate HTTP and background metering load."""

from __future__ import annotations

import asyncio
import json
import unittest
from collections import Counter
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import aiohttp
from aiohttp import web
from homeassistant.core import HomeAssistant

from custom_components.domus40.api import Domus40Client, Domus40ConnectionError
from custom_components.domus40.coordinator import Domus40Coordinator
from custom_components.domus40.models import Domus40State, MqttInfo
from domus40_emulator.server import PASSWORD, USERNAME, create_app


def _fixture_state() -> Domus40State:
    fixture = json.loads(
        (Path(__file__).parent / "fixtures/inventory.json").read_text()
    )
    return Domus40State.from_api(
        fixture["devices"], fixture["divisions"], fixture["areas"]
    )


class HttpLoadTests(unittest.IsolatedAsyncioTestCase):
    """Measure actual HTTP concurrency and session recovery in the emulator."""

    async def asyncSetUp(self) -> None:
        self.in_flight = 0
        self.peak = 0
        self.calls = Counter()
        self.reject_session = False

        @web.middleware
        async def observe(request, handler):
            self.in_flight += 1
            self.peak = max(self.peak, self.in_flight)
            self.calls[request.path] += 1
            try:
                await asyncio.sleep(0.02)
                if self.reject_session and request.path.endswith(
                    ("getDevices", "getDivisions", "getAreas")
                ):
                    return web.Response(status=403)
                response = await handler(request)
                if request.path.endswith("/authentication") and response.status == 200:
                    self.reject_session = False
                return response
            finally:
                self.in_flight -= 1

        app = create_app()
        app.middlewares.append(observe)
        self.runner = web.AppRunner(app, access_log=None)
        await self.runner.setup()
        site = web.TCPSite(self.runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        self.session = aiohttp.ClientSession(cookie_jar=aiohttp.CookieJar(unsafe=True))
        self.client = Domus40Client(
            f"http://127.0.0.1:{port}", USERNAME, PASSWORD, self.session
        )
        await self.client.async_authenticate()
        self.calls.clear()
        self.peak = 0

    async def asyncTearDown(self) -> None:
        await self.client.async_logout()
        await self.session.close()
        await self.runner.cleanup()

    async def test_inventory_metering_schema_and_commands_share_two_slots(self) -> None:
        state, _, schema, _ = await asyncio.gather(
            self.client.async_get_state(),
            self.client.async_activate_reporting("101"),
            self.client.async_get_events_schema(),
            self.client.async_set_level("101", 42),
        )
        self.assertTrue(state.devices)
        self.assertIsNotNone(schema)
        self.assertEqual(self.peak, 2)

    async def test_concurrent_session_rejections_share_one_authentication(self) -> None:
        self.reject_session = True
        state = await self.client.async_get_state()
        self.assertTrue(state.devices)
        self.assertEqual(self.calls["/HsAPI/users/valid"], 1)
        self.assertEqual(self.calls["/HsAPI/authentication"], 2)


class MeteringLoadTests(unittest.IsolatedAsyncioTestCase):
    """Keep reporting work bounded even when the hub or MQTT is unavailable."""

    async def asyncSetUp(self) -> None:
        async def activate(device_id):
            return f"fixture/events/device/{device_id}/reading/instant"

        self.client = SimpleNamespace(
            mqtt_info=MqttInfo("fixture-user", "fixture-password", "fixture/"),
            async_activate_reporting=AsyncMock(side_effect=activate),
            async_deactivate_reporting=AsyncMock(),
            async_set_level=AsyncMock(),
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

    async def asyncTearDown(self) -> None:
        await self.coordinator.async_shutdown()

    async def test_disconnected_mqtt_does_not_acquire_reporting_leases(self) -> None:
        self.coordinator.async_start_reporting()
        await asyncio.sleep(0.05)
        self.client.async_activate_reporting.assert_not_awaited()

    async def test_expired_command_stops_fast_retries_during_http_failure(self) -> None:
        self.client.async_get_state.side_effect = Domus40ConnectionError(
            "Synthetic unavailable server"
        )
        await self._assert_expired_command_stops_fast_retries()

    async def test_unrelated_push_does_not_extend_failed_command_retries(self) -> None:
        async def fail_with_push():
            # A push immediately after each failed poll marks the HA coordinator
            # successful again, but the REST reconciliation is still failing.
            asyncio.get_running_loop().call_soon(
                self.coordinator.async_set_updated_data,
                self.coordinator.data.with_device_level("102", 20),
            )
            raise Domus40ConnectionError("Synthetic unavailable server")

        self.client.async_get_state.side_effect = fail_with_push
        await self._assert_expired_command_stops_fast_retries()

    async def _assert_expired_command_stops_fast_retries(self) -> None:
        self.coordinator._debounced_refresh.cooldown = 0.02
        with (
            patch("custom_components.domus40.coordinator.WRITE_CONFIRM_TIMEOUT", 0.05),
            patch("custom_components.domus40.coordinator.WRITE_REFRESH_DELAYS", (0.01,)),
            patch("custom_components.domus40.coordinator.REST_REFRESH_MIN_INTERVAL", 0.01),
            self.assertLogs("custom_components.domus40.coordinator", level="ERROR"),
        ):
            await self.coordinator.async_set_device_level("101", 42)
            self.assertEqual(self.coordinator.data.devices["101"].level, 42)
            await asyncio.sleep(0.15)
            self.assertEqual(self.coordinator.pending_write_count, 0)
            self.assertIsNone(self.coordinator._write_refresh_task)
            calls = self.client.async_get_state.await_count
            self.assertGreater(calls, 0)
            await asyncio.sleep(0.05)
            self.assertEqual(self.client.async_get_state.await_count, calls)

    async def test_full_batches_pause_and_visit_every_logical_device(self) -> None:
        template = self.coordinator.data.devices["101"]
        self.coordinator.data = replace(
            self.coordinator.data,
            devices={
                str(index): replace(
                    template, device_id=str(index), hardware_address=f"fixture-{index}"
                )
                for index in range(1000, 1008)
            },
        )
        self.coordinator._set_push_connected(True)
        samples = []
        pauses = []

        async def sample():
            samples.append(set(self.coordinator.reporting_active_ids))

        async def pause(delay):
            pauses.append(delay)
            self.assertFalse(self.coordinator.reporting_active_ids)
            if len(pauses) == 2:
                raise asyncio.CancelledError

        with (
            patch.object(self.coordinator, "_async_wait_reporting_sample", sample),
            patch("custom_components.domus40.coordinator.asyncio.sleep", pause),
            self.assertRaises(asyncio.CancelledError),
        ):
            await self.coordinator._async_reporting_loop()

        self.assertEqual([len(batch) for batch in samples], [4, 4])
        self.assertEqual(set.union(*samples), set(self.coordinator.data.devices))
        self.assertEqual(pauses, [30, 30])
        self.assertEqual(self.client.async_activate_reporting.await_count, 8)
        self.assertEqual(self.client.async_deactivate_reporting.await_count, 8)
        # Two batches now take at least 80 seconds. A reading from three minutes
        # ago can still be within the allowed three-cycle freshness window.
        self.coordinator.power_reading_recorded_at["1000"] = (
            asyncio.get_running_loop().time() - 180
        )
        self.assertTrue(self.coordinator.power_reading_is_fresh("1000"))

    async def test_failed_batches_back_off_and_recovery_resets_the_pause(self) -> None:
        self.coordinator._set_push_connected(True)
        pauses = []

        async def activate(device_id):
            if len(pauses) < 5:
                raise Domus40ConnectionError("Synthetic rejected lease")
            return f"fixture/events/device/{device_id}/reading/instant"

        async def pause(delay):
            pauses.append(delay)
            if len(pauses) == 6:
                raise asyncio.CancelledError

        self.client.async_activate_reporting.side_effect = activate
        with (
            patch.object(self.coordinator, "_async_wait_reporting_sample", AsyncMock()),
            patch("custom_components.domus40.coordinator.asyncio.sleep", pause),
            self.assertRaises(asyncio.CancelledError),
        ):
            await self.coordinator._async_reporting_loop()

        self.assertEqual(pauses, [60, 120, 240, 300, 300, 30])
        self.assertEqual(self.coordinator.reporting_activation_failures, 15)

    async def test_failed_cleanup_blocks_new_leases_until_released(self) -> None:
        self.coordinator._set_push_connected(True)
        pauses = []

        async def deactivate(_device_id):
            if not pauses:
                raise Domus40ConnectionError("Synthetic rejected cleanup")

        async def pause(delay):
            pauses.append(delay)
            if len(pauses) == 2:
                raise asyncio.CancelledError

        self.client.async_deactivate_reporting.side_effect = deactivate
        with (
            patch.object(self.coordinator, "_async_wait_reporting_sample", AsyncMock()),
            patch("custom_components.domus40.coordinator.asyncio.sleep", pause),
            self.assertRaises(asyncio.CancelledError),
        ):
            await self.coordinator._async_reporting_loop()

        self.assertEqual(pauses, [60, 30])
        self.assertEqual(self.client.async_activate_reporting.await_count, 3)
        self.assertEqual(self.client.async_deactivate_reporting.await_count, 6)
        self.assertEqual(self.coordinator.reporting_release_failures, 3)
        self.assertFalse(self.coordinator.reporting_active_ids)

    async def test_disconnect_releases_batch_and_reconnect_resumes_reporting(
        self,
    ) -> None:
        self.coordinator._set_push_connected(True)
        sampling = asyncio.Event()
        wait_sample = self.coordinator._async_wait_reporting_sample

        async def sample():
            sampling.set()
            await wait_sample()

        with (
            patch.object(self.coordinator, "_async_wait_reporting_sample", sample),
            patch("custom_components.domus40.coordinator.REPORTING_COOLDOWN_SECONDS", 0.05),
        ):
            self.coordinator.async_start_reporting()
            await asyncio.wait_for(sampling.wait(), 1)
            self.coordinator._set_push_connected(False)
            await asyncio.sleep(0.1)
            self.assertFalse(self.coordinator.reporting_active_ids)
            self.assertEqual(self.client.async_deactivate_reporting.await_count, 3)
            self.assertEqual(self.client.async_activate_reporting.await_count, 3)
            sampling.clear()
            self.coordinator._set_push_connected(True)
            await asyncio.wait_for(sampling.wait(), 1)
            self.assertEqual(self.client.async_activate_reporting.await_count, 6)

    async def test_unload_releases_completed_part_of_an_activation_batch(self) -> None:
        started = asyncio.Event()

        async def activate(device_id):
            if device_id != "101":
                started.set()
                await asyncio.Event().wait()
            return f"fixture/events/device/{device_id}/reading/instant"

        self.client.async_activate_reporting.side_effect = activate
        self.coordinator._set_push_connected(True)
        self.coordinator.async_start_reporting()
        await asyncio.wait_for(started.wait(), 1)
        await self.coordinator.async_shutdown()
        self.client.async_deactivate_reporting.assert_awaited_once_with("101")


if __name__ == "__main__":
    unittest.main()
