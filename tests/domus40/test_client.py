"""End-to-end HTTP contract test against the sanitized emulator."""

from __future__ import annotations

import asyncio
import sys
import unittest
from pathlib import Path

import aiohttp
from aiohttp import web

from custom_components.domus40.api import Domus40Client
from custom_components.domus40.coordinator import _mapped_emitter_ids
from custom_components.domus40.mqtt import Domus40MqttClient
from custom_components.domus40.proto import (
    constants_schema_is_compatible,
    decode_device_instant_reading_event,
    decode_device_state_event,
    instant_schema_is_compatible,
    schema_is_compatible,
)

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from domus40_emulator.server import (
    EMULATOR_KEY,
    PASSWORD,
    USERNAME,
    create_app,
)


class ClientContractTests(unittest.IsolatedAsyncioTestCase):
    """Exercise authentication, inventory, schema, and writes over HTTP."""

    async def asyncSetUp(self) -> None:
        self.app = create_app()
        self.runner = web.AppRunner(self.app)
        await self.runner.setup()
        self.site = web.TCPSite(self.runner, "127.0.0.1", 0)
        await self.site.start()
        server = self.site._server
        assert server is not None
        port = server.sockets[0].getsockname()[1]
        self.session = aiohttp.ClientSession(cookie_jar=aiohttp.CookieJar(unsafe=True))
        self.client = Domus40Client(
            f"http://127.0.0.1:{port}", USERNAME, PASSWORD, self.session
        )

    async def asyncTearDown(self) -> None:
        await self.client.async_logout()
        await self.session.close()
        await self.runner.cleanup()

    async def test_full_http_contract(self) -> None:
        await self.client.async_authenticate()
        self.assertIsNotNone(self.client.mqtt_info)

        state = await self.client.async_get_state()
        self.assertEqual(len(state.devices), 5)
        self.assertEqual(state.devices["101"].level, 73)
        self.assertEqual(state.devices["101"].floor_name, "Fixture floor")

        schema = await self.client.async_get_events_schema()
        self.assertIsNotNone(schema)
        assert schema is not None
        self.assertTrue(schema_is_compatible(schema))
        self.assertTrue(instant_schema_is_compatible(schema))
        constants_schema = await self.client.async_get_proto_schema("constants.proto")
        self.assertIsNotNone(constants_schema)
        assert constants_schema is not None
        self.assertTrue(constants_schema_is_compatible(constants_schema))

        binding = await self.client.async_get_button_binding("105", "TeclaA", state)
        self.assertEqual(binding.description, "Fixture dimmer: set to 42%")
        self.assertEqual(binding.actions[0].action, "SetLevel")
        self.assertEqual(binding.actions[0].target_device_id, "101")
        bindings = await self.client.async_get_button_bindings(
            "104", ("TeclaA", "TeclaB", "TeclaC", "TeclaD"), state
        )
        self.assertEqual(bindings["TeclaA"].description, "Fixture dimmer: set to 100%")
        self.assertEqual(bindings["TeclaB"].description, "Unassigned")
        self.assertEqual(bindings["TeclaC"].description, "Fixture dimmer: set to 0%")
        self.assertEqual(
            _mapped_emitter_ids(
                [binding, *bindings.values()],
                {"101"},
            ),
            {"104", "105"},
        )

        await self.client.async_set_level("101", 27)
        state = await self.client.async_get_state()
        self.assertEqual(state.devices["101"].level, 27)

        await self.client.async_identify_device("104")
        self.assertEqual(self.app[EMULATOR_KEY].last_identification, (104, 30))

        topic = await self.client.async_activate_reporting("101")
        self.assertEqual(topic, "fixture/events/device/101/reading/instant")
        self.assertIn(101, self.app[EMULATOR_KEY].reporting_ids)
        await self.client.async_deactivate_reporting("101")
        self.assertNotIn(101, self.app[EMULATOR_KEY].reporting_ids)


class MqttContractTests(unittest.IsolatedAsyncioTestCase):
    """Exercise a push event through a real local WebSocket."""

    async def test_state_event(self) -> None:
        app = create_app()
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 9998)
        await site.start()
        session = aiohttp.ClientSession(cookie_jar=aiohttp.CookieJar(unsafe=True))
        client = Domus40Client("http://127.0.0.1:9998", USERNAME, PASSWORD, session)
        listener: asyncio.Task[None] | None = None
        try:
            await client.async_authenticate()
            assert client.mqtt_info is not None
            received: asyncio.Future[bytes] = asyncio.get_running_loop().create_future()

            def on_message(_topic: str, payload: bytes) -> None:
                if not received.done():
                    received.set_result(payload)

            mqtt = Domus40MqttClient(
                client.base_url, client.mqtt_info, session, on_message
            )
            listener = asyncio.create_task(mqtt.async_listen())
            await asyncio.sleep(0.05)
            await client.async_set_level("101", 61)
            event = decode_device_state_event(await asyncio.wait_for(received, 1))
            self.assertEqual(event.device_id, "101")
            self.assertEqual(event.energy_level, 61)
        finally:
            if listener is not None:
                listener.cancel()
                await asyncio.gather(listener, return_exceptions=True)
            await client.async_logout()
            await session.close()
            await runner.cleanup()

    async def test_instant_power_event(self) -> None:
        app = create_app()
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 9998)
        await site.start()
        session = aiohttp.ClientSession(cookie_jar=aiohttp.CookieJar(unsafe=True))
        client = Domus40Client("http://127.0.0.1:9998", USERNAME, PASSWORD, session)
        listener: asyncio.Task[None] | None = None
        try:
            await client.async_authenticate()
            assert client.mqtt_info is not None
            topic = await client.async_activate_reporting("101")
            received: asyncio.Future[bytes] = asyncio.get_running_loop().create_future()

            def on_message(_topic: str, payload: bytes) -> None:
                if not received.done():
                    received.set_result(payload)

            mqtt = Domus40MqttClient(
                client.base_url,
                client.mqtt_info,
                session,
                on_message,
                (topic,),
            )
            listener = asyncio.create_task(mqtt.async_listen())
            await asyncio.sleep(0.05)
            await app[EMULATOR_KEY].publish_power(101, 12345)
            event = decode_device_instant_reading_event(
                await asyncio.wait_for(received, 1)
            )
            self.assertEqual(event.device_id, "101")
            self.assertEqual(event.power_w, 12.345)
            await client.async_deactivate_reporting("101")
        finally:
            if listener is not None:
                listener.cancel()
                await asyncio.gather(listener, return_exceptions=True)
            await client.async_logout()
            await session.close()
            await runner.cleanup()

    async def test_button_event(self) -> None:
        app = create_app()
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 9998)
        await site.start()
        session = aiohttp.ClientSession(cookie_jar=aiohttp.CookieJar(unsafe=True))
        client = Domus40Client("http://127.0.0.1:9998", USERNAME, PASSWORD, session)
        listener: asyncio.Task[None] | None = None
        try:
            await client.async_authenticate()
            assert client.mqtt_info is not None
            received: asyncio.Future[bytes] = asyncio.get_running_loop().create_future()

            def on_message(_topic: str, payload: bytes) -> None:
                if not received.done():
                    received.set_result(payload)

            mqtt = Domus40MqttClient(
                client.base_url, client.mqtt_info, session, on_message
            )
            listener = asyncio.create_task(mqtt.async_listen())
            await asyncio.sleep(0.05)
            await app[EMULATOR_KEY].publish_button(105, 9, 0)
            event = decode_device_state_event(await asyncio.wait_for(received, 1))
            self.assertEqual(event.device_id, "105")
            self.assertEqual(event.endpoint, 9)
            self.assertEqual(event.button_state, 0)
            self.assertIsNone(event.energy_level)
        finally:
            if listener is not None:
                listener.cancel()
                await asyncio.gather(listener, return_exceptions=True)
            await client.async_logout()
            await session.close()
            await runner.cleanup()

    async def test_ir_button_event(self) -> None:
        app = create_app()
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 9998)
        await site.start()
        session = aiohttp.ClientSession(cookie_jar=aiohttp.CookieJar(unsafe=True))
        client = Domus40Client("http://127.0.0.1:9998", USERNAME, PASSWORD, session)
        listener: asyncio.Task[None] | None = None
        try:
            await client.async_authenticate()
            assert client.mqtt_info is not None
            received: asyncio.Future[bytes] = asyncio.get_running_loop().create_future()

            def on_message(_topic: str, payload: bytes) -> None:
                if not received.done():
                    received.set_result(payload)

            mqtt = Domus40MqttClient(
                client.base_url, client.mqtt_info, session, on_message
            )
            listener = asyncio.create_task(mqtt.async_listen())
            await asyncio.sleep(0.05)
            await app[EMULATOR_KEY].publish_button(104, 21, 0)
            event = decode_device_state_event(await asyncio.wait_for(received, 1))
            self.assertEqual(event.device_id, "104")
            self.assertEqual(event.endpoint, 21)
            self.assertEqual(event.button_state, 0)
        finally:
            if listener is not None:
                listener.cancel()
                await asyncio.gather(listener, return_exceptions=True)
            await client.async_logout()
            await session.close()
            await runner.cleanup()


if __name__ == "__main__":
    unittest.main()
