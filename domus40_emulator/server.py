"""Sanitized local emulator for the private EFAPEL Domus40 bridge protocol."""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import secrets
from pathlib import Path
from typing import Any

from aiohttp import web

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests/domus40/fixtures/inventory.json"
SCHEMA = ROOT / "custom_components/domus40/events.proto"
CONSTANTS_SCHEMA = SCHEMA.with_name("constants.proto")
USERNAME = "fixture-admin"
PASSWORD = "fixture-password"
MQTT_USERNAME = "fixture-mqtt"
MQTT_PASSWORD = "fixture-mqtt-password"
CSRF = "fixture-csrf-token"
CHALLENGE = "fixture-challenge"
SESSION = "fixture-session"


def _challenge_response(password: str, challenge: str) -> str:
    first = hashlib.sha256(password.encode()).hexdigest()
    second = hashlib.sha256(first.encode()).hexdigest()
    return hashlib.sha256(f"{challenge}{second}".encode()).hexdigest()


def _varint(value: int) -> bytes:
    result = bytearray()
    while True:
        byte = value & 0x7F
        value >>= 7
        result.append(byte | (0x80 if value else 0))
        if not value:
            return bytes(result)


def _device_event(device_id: int, level: int) -> bytes:
    return (
        b"\x0a\x00"
        + _varint(2 << 3)
        + _varint(device_id)
        + _varint(100 << 3)
        + _varint(1)
        + _varint(201 << 3)
        + _varint(level)
    )


def _button_event(device_id: int, endpoint: int, button_state: int) -> bytes:
    return (
        b"\x0a\x00"
        + _varint(2 << 3)
        + _varint(device_id)
        + _varint(100 << 3)
        + _varint(endpoint)
        + _varint(101 << 3)
        + _varint(button_state)
    )


def _instant_reading_event(device_id: int, consumed_mw: int) -> bytes:
    return (
        b"\x0a\x00"
        + _varint(2 << 3)
        + _varint(device_id)
        + _varint(12 << 3)
        + _varint(consumed_mw)
    )


def _mqtt_packet(header: int, body: bytes = b"") -> bytes:
    return bytes([header]) + _varint(len(body)) + body


def _pop_mqtt(buffer: bytes) -> tuple[int, int, bytes, bytes] | None:
    if len(buffer) < 2:
        return None
    remaining = 0
    multiplier = 1
    offset = 1
    while True:
        if offset >= len(buffer):
            return None
        byte = buffer[offset]
        offset += 1
        remaining += (byte & 0x7F) * multiplier
        if not byte & 0x80:
            break
        multiplier *= 128
    end = offset + remaining
    if end > len(buffer):
        return None
    return buffer[0] >> 4, buffer[0] & 0x0F, buffer[offset:end], buffer[end:]


class Emulator:
    """Mutable fixture state and connected MQTT clients."""

    def __init__(self, *, base64_events: bool = False) -> None:
        data = json.loads(FIXTURE.read_text())
        self.devices: list[dict[str, Any]] = data["devices"]
        self.divisions: list[dict[str, Any]] = data["divisions"]
        self.areas: list[dict[str, Any]] = data["areas"]
        self.base64_events = base64_events
        self.websockets: set[web.WebSocketResponse] = set()
        self.reporting_ids: set[int] = set()
        self.last_identification: tuple[int, int] | None = None
        self.button_scenarios = {
            ("104", "TeclaA"): (
                10409,
                [{"targetDeviceId": 101, "action": "SetLevel", "level": 100}],
            ),
            ("104", "TeclaC"): (
                10411,
                [{"targetDeviceId": 101, "action": "SetLevel", "level": 0}],
            ),
            ("105", "TeclaA"): (
                10509,
                [{"targetDeviceId": 101, "action": "SetLevel", "level": 42}],
            ),
            ("105", "TeclaB"): (
                10510,
                [{"targetDeviceId": 103, "action": "SetOn", "level": 100}],
            ),
            ("105", "TeclaC"): (
                10511,
                [{"targetDeviceId": 102, "action": "SetLevel", "level": 0}],
            ),
            ("105", "TeclaD"): (
                10512,
                [{"targetDeviceId": 103, "action": "Toggle", "level": 69}],
            ),
        }

    @staticmethod
    def _authorized(request: web.Request) -> bool:
        return request.cookies.get("JSESSIONID") == SESSION

    @staticmethod
    def _csrf_headers() -> dict[str, str]:
        return {"X-AntiCSRF": CSRF}

    async def users_valid(self, request: web.Request) -> web.Response:
        return web.Response(status=403, headers=self._csrf_headers())

    async def authenticate(self, request: web.Request) -> web.Response:
        data = await request.json()
        if data.get("username") != USERNAME or data.get("csrftoken") != CSRF:
            return web.Response(status=403, headers=self._csrf_headers())
        response = data.get("challengeResponse")
        if response is None:
            return web.json_response(
                {"challenge": CHALLENGE}, status=401, headers=self._csrf_headers()
            )
        if not secrets.compare_digest(
            str(response), _challenge_response(PASSWORD, CHALLENGE)
        ):
            return web.Response(status=403, headers=self._csrf_headers())
        result = web.json_response(
            {
                "mqttInfo": {
                    "username": MQTT_USERNAME,
                    "password": MQTT_PASSWORD,
                    "prefix": "fixture/",
                }
            },
            headers=self._csrf_headers(),
        )
        result.set_cookie("JSESSIONID", SESSION, httponly=True)
        return result

    async def logout(self, request: web.Request) -> web.Response:
        response = web.json_response({})
        response.del_cookie("JSESSIONID")
        return response

    async def devices_list(self, request: web.Request) -> web.Response:
        if not self._authorized(request):
            return web.Response(status=403, headers=self._csrf_headers())
        return web.json_response(self.devices, headers=self._csrf_headers())

    async def divisions_list(self, request: web.Request) -> web.Response:
        if not self._authorized(request):
            return web.Response(status=403, headers=self._csrf_headers())
        return web.json_response(self.divisions, headers=self._csrf_headers())

    async def areas_list(self, request: web.Request) -> web.Response:
        if not self._authorized(request):
            return web.Response(status=403, headers=self._csrf_headers())
        return web.json_response(self.areas, headers=self._csrf_headers())

    async def events_schema(self, request: web.Request) -> web.Response:
        if not self._authorized(request):
            return web.Response(status=403, headers=self._csrf_headers())
        return web.Response(text=SCHEMA.read_text(), content_type="text/plain")

    async def constants_schema(self, request: web.Request) -> web.Response:
        if not self._authorized(request):
            return web.Response(status=403, headers=self._csrf_headers())
        return web.Response(
            text=CONSTANTS_SCHEMA.read_text(), content_type="text/plain"
        )

    async def device_scenarios(self, request: web.Request) -> web.Response:
        if not self._authorized(request):
            return web.Response(status=403, headers=self._csrf_headers())
        device_id = request.query.get("deviceId", "")
        endpoint = request.query.get("deviceEndpoint")
        if endpoint is not None:
            binding = self.button_scenarios.get((device_id, endpoint))
            device_scenarios = (
                [{"scenario": {"id": binding[0]}}] if binding is not None else []
            )
        else:
            device_scenarios = [
                {
                    "device": {"id": int(candidate_device)},
                    "endpoint": candidate_endpoint,
                    "scenario": {"id": binding[0]},
                }
                for (candidate_device, candidate_endpoint), binding in (
                    self.button_scenarios.items()
                )
                if candidate_device == device_id
            ]
        return web.json_response(
            {"deviceScenarios": device_scenarios}, headers=self._csrf_headers()
        )

    async def full_scenario(self, request: web.Request) -> web.Response:
        if not self._authorized(request):
            return web.Response(status=403, headers=self._csrf_headers())
        scenario_id = int(request.match_info["scenario_id"])
        actions = next(
            (
                item[1]
                for item in self.button_scenarios.values()
                if item[0] == scenario_id
            ),
            None,
        )
        if actions is None:
            raise web.HTTPNotFound()
        return web.json_response(
            {
                "scenario": {"id": scenario_id},
                "actions": {"scenarioId": scenario_id, "actions": actions},
            },
            headers=self._csrf_headers(),
        )

    async def set_state(self, request: web.Request) -> web.Response:
        if not self._authorized(request):
            return web.Response(status=403, headers=self._csrf_headers())
        device_id = int(request.match_info["device_id"])
        level = max(0, min(100, round(float((await request.json())["level"]))))
        for device in self.devices:
            if int(device["id"]) == device_id:
                device["levelPercentage"] = level
                device["switchedOn"] = level > 0
                await self.publish(device_id, level)
                return web.json_response(device, headers=self._csrf_headers())
        raise web.HTTPNotFound()

    async def identify_device(self, request: web.Request) -> web.Response:
        if not self._authorized(request):
            return web.Response(status=403, headers=self._csrf_headers())
        device_id = int(request.match_info["device_id"])
        duration = int((await request.json())["duration"])
        if not any(int(device["id"]) == device_id for device in self.devices):
            raise web.HTTPNotFound()
        self.last_identification = (device_id, duration)
        return web.json_response({}, headers=self._csrf_headers())

    async def activate_reporting(self, request: web.Request) -> web.Response:
        if not self._authorized(request):
            return web.Response(status=403, headers=self._csrf_headers())
        await request.json()
        device_id = int(request.match_info["device_id"])
        if not any(
            int(device["id"]) == device_id and device.get("capMetering") is True
            for device in self.devices
        ):
            raise web.HTTPNotFound()
        self.reporting_ids.add(device_id)
        return web.json_response(
            {"topic": f"fixture/events/device/{device_id}/reading/instant"},
            headers=self._csrf_headers(),
        )

    async def deactivate_reporting(self, request: web.Request) -> web.Response:
        if not self._authorized(request):
            return web.Response(status=403, headers=self._csrf_headers())
        device_id = int(request.match_info["device_id"])
        self.reporting_ids.discard(device_id)
        return web.json_response({}, headers=self._csrf_headers())

    async def publish(self, device_id: int, level: int) -> None:
        topic = f"fixture/events/device/{device_id}/state/changed".encode()
        payload = _device_event(device_id, level)
        if self.base64_events:
            payload = base64.b64encode(payload)
        body = len(topic).to_bytes(2, "big") + topic + payload
        packet = _mqtt_packet(0x30, body)
        for websocket in tuple(self.websockets):
            if websocket.closed:
                self.websockets.discard(websocket)
            else:
                await websocket.send_bytes(packet)

    async def publish_power(self, device_id: int, consumed_mw: int) -> None:
        """Publish one synthetic instantaneous power reading."""
        if device_id not in self.reporting_ids:
            return
        topic = f"fixture/events/device/{device_id}/reading/instant".encode()
        payload = _instant_reading_event(device_id, consumed_mw)
        if self.base64_events:
            payload = base64.b64encode(payload)
        body = len(topic).to_bytes(2, "big") + topic + payload
        packet = _mqtt_packet(0x30, body)
        for websocket in tuple(self.websockets):
            if websocket.closed:
                self.websockets.discard(websocket)
            else:
                await websocket.send_bytes(packet)

    async def publish_button(
        self, device_id: int, endpoint: int, button_state: int
    ) -> None:
        """Publish one synthetic physical key event."""
        topic = f"fixture/events/device/{device_id}/state/changed".encode()
        payload = _button_event(device_id, endpoint, button_state)
        if self.base64_events:
            payload = base64.b64encode(payload)
        body = len(topic).to_bytes(2, "big") + topic + payload
        packet = _mqtt_packet(0x30, body)
        for websocket in tuple(self.websockets):
            if websocket.closed:
                self.websockets.discard(websocket)
            else:
                await websocket.send_bytes(packet)

    async def mqtt(self, request: web.Request) -> web.WebSocketResponse:
        websocket = web.WebSocketResponse(protocols=("mqttv3.1",))
        await websocket.prepare(request)
        self.websockets.add(websocket)
        buffer = b""
        try:
            async for message in websocket:
                if message.type != web.WSMsgType.BINARY:
                    continue
                buffer += bytes(message.data)
                while (packet := _pop_mqtt(buffer)) is not None:
                    packet_type, _, body, buffer = packet
                    if packet_type == 1:
                        await websocket.send_bytes(_mqtt_packet(0x20, b"\x00\x00"))
                    elif packet_type == 8:
                        packet_id = body[:2]
                        await websocket.send_bytes(
                            _mqtt_packet(0x90, packet_id + b"\x00")
                        )
                    elif packet_type == 12:
                        await websocket.send_bytes(_mqtt_packet(0xD0))
        finally:
            self.websockets.discard(websocket)
        return websocket


EMULATOR_KEY = web.AppKey("emulator", Emulator)


def create_app(*, base64_events: bool = False) -> web.Application:
    """Create the emulator application."""
    emulator = Emulator(base64_events=base64_events)
    app = web.Application()
    app[EMULATOR_KEY] = emulator
    app.add_routes(
        [
            web.get("/HsAPI/users/valid", emulator.users_valid),
            web.post("/HsAPI/authentication", emulator.authenticate),
            web.delete("/HsAPI/authentication", emulator.logout),
            web.get("/HsAPI/devices/getDevices", emulator.devices_list),
            web.get("/HsAPI/divisions/getDivisions", emulator.divisions_list),
            web.get("/HsAPI/areas/getAreas", emulator.areas_list),
            web.get("/HsAPI/static/protos/events.proto", emulator.events_schema),
            web.get("/HsAPI/static/protos/constants.proto", emulator.constants_schema),
            web.get("/HsAPI/devicescenarios/", emulator.device_scenarios),
            web.get("/HsAPI/scenarios/full/{scenario_id}", emulator.full_scenario),
            web.put("/HsAPI/devices/{device_id}/state", emulator.set_state),
            web.put("/HsAPI/devices/{device_id}/led", emulator.identify_device),
            web.put(
                "/HsAPI/devices/{device_id}/reporting", emulator.activate_reporting
            ),
            web.delete(
                "/HsAPI/devices/{device_id}/reporting", emulator.deactivate_reporting
            ),
            web.get("/", emulator.mqtt),
        ]
    )
    return app


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", default=8080, type=int)
    parser.add_argument("--base64-events", action="store_true")
    args = parser.parse_args()
    web.run_app(
        create_app(base64_events=args.base64_events),
        host=args.host,
        port=args.port,
    )


if __name__ == "__main__":
    main()
