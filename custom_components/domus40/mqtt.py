"""Minimal MQTT 3.1.1 over WebSocket client for Domus40 push events."""

from __future__ import annotations

import asyncio
import inspect
import secrets
from collections.abc import Awaitable, Callable
from urllib.parse import urlsplit

import aiohttp
from aiohttp import WSMsgType

from .const import MQTT_PATH, MQTT_PORT, MQTT_STATE_TOPIC
from .models import MqttInfo

MessageCallback = Callable[[str, bytes], Awaitable[None] | None]


class Domus40MqttError(Exception):
    """The private MQTT endpoint rejected or closed the connection."""


def _mqtt_string(value: str | bytes) -> bytes:
    encoded = value.encode() if isinstance(value, str) else value
    if len(encoded) > 65535:
        raise Domus40MqttError("MQTT string is too long")
    return len(encoded).to_bytes(2, "big") + encoded


def _remaining_length(length: int) -> bytes:
    encoded = bytearray()
    while True:
        digit = length % 128
        length //= 128
        if length:
            digit |= 0x80
        encoded.append(digit)
        if not length:
            return bytes(encoded)


def _packet(packet_type_and_flags: int, body: bytes = b"") -> bytes:
    return bytes([packet_type_and_flags]) + _remaining_length(len(body)) + body


def _connect_packet(info: MqttInfo) -> bytes:
    variable_header = _mqtt_string("MQTT") + bytes([4, 0xC2]) + (45).to_bytes(2, "big")
    client_id = f"home-assistant-domus40-{secrets.token_hex(6)}"
    payload = (
        _mqtt_string(client_id)
        + _mqtt_string(info.username)
        + _mqtt_string(info.password)
    )
    return _packet(0x10, variable_header + payload)


def _subscribe_packet(topic: str, packet_id: int = 1) -> bytes:
    body = packet_id.to_bytes(2, "big") + _mqtt_string(topic) + b"\x00"
    return _packet(0x82, body)


def _pop_packet(buffer: bytes) -> tuple[int, int, bytes, bytes] | None:
    """Pop one MQTT packet from a WebSocket byte stream."""
    if len(buffer) < 2:
        return None
    multiplier = 1
    remaining = 0
    offset = 1
    for _ in range(4):
        if offset >= len(buffer):
            return None
        digit = buffer[offset]
        offset += 1
        remaining += (digit & 0x7F) * multiplier
        if not digit & 0x80:
            break
        multiplier *= 128
    else:
        raise Domus40MqttError("invalid MQTT remaining length")
    end = offset + remaining
    if end > len(buffer):
        return None
    header = buffer[0]
    return header >> 4, header & 0x0F, buffer[offset:end], buffer[end:]


def _publish_message(flags: int, body: bytes) -> tuple[str, bytes, int | None]:
    if len(body) < 2:
        raise Domus40MqttError("truncated MQTT PUBLISH")
    topic_length = int.from_bytes(body[:2], "big")
    offset = 2 + topic_length
    if offset > len(body):
        raise Domus40MqttError("truncated MQTT topic")
    try:
        topic = body[2:offset].decode()
    except UnicodeDecodeError as err:
        raise Domus40MqttError("invalid MQTT topic") from err
    qos = (flags >> 1) & 0x03
    packet_id = None
    if qos:
        if offset + 2 > len(body):
            raise Domus40MqttError("truncated MQTT packet identifier")
        packet_id = int.from_bytes(body[offset : offset + 2], "big")
        offset += 2
    return topic, body[offset:], packet_id


def topic_matches_subscription(subscription: str, topic: str) -> bool:
    """Match one MQTT topic against a ``+``/``#`` subscription filter."""
    filter_levels = subscription.split("/")
    topic_levels = topic.split("/")
    for index, filter_level in enumerate(filter_levels):
        if filter_level == "#":
            return index == len(filter_levels) - 1
        if index >= len(topic_levels):
            return False
        if filter_level != "+" and filter_level != topic_levels[index]:
            return False
    return len(filter_levels) == len(topic_levels)


class Domus40MqttClient:
    """Receive private Domus40 state events without an MQTT dependency."""

    def __init__(
        self,
        base_url: str,
        info: MqttInfo,
        session: aiohttp.ClientSession,
        callback: MessageCallback,
        topics: tuple[str, ...] | None = None,
    ) -> None:
        """Initialize a push client."""
        host = urlsplit(base_url).hostname
        if not host:
            raise Domus40MqttError("invalid Home Server address")
        self._url = f"ws://{host}:{MQTT_PORT}{MQTT_PATH}"
        self._info = info
        self._session = session
        self._callback = callback
        self._topics = topics or (f"{info.prefix}{MQTT_STATE_TOPIC}",)
        if not self._topics:
            raise Domus40MqttError("at least one MQTT topic is required")

    async def _receive(
        self, websocket: aiohttp.ClientWebSocketResponse, buffer: bytes
    ) -> tuple[int, int, bytes, bytes]:
        while (packet := _pop_packet(buffer)) is None:
            message = await websocket.receive()
            if message.type == WSMsgType.BINARY:
                buffer += bytes(message.data)
            elif message.type == WSMsgType.TEXT:
                buffer += message.data.encode()
            elif message.type in {WSMsgType.CLOSE, WSMsgType.CLOSED, WSMsgType.ERROR}:
                raise Domus40MqttError("MQTT WebSocket closed")
        return packet

    async def async_listen(self) -> None:
        """Connect, subscribe, and run until cancelled or disconnected."""
        async with self._session.ws_connect(
            self._url,
            protocols=("mqttv3.1",),
            timeout=aiohttp.ClientWSTimeout(ws_close=10),
            autoclose=True,
        ) as websocket:
            await websocket.send_bytes(_connect_packet(self._info))
            packet_type, _, body, buffer = await self._receive(websocket, b"")
            if packet_type != 2 or len(body) < 2 or body[1] != 0:
                raise Domus40MqttError("MQTT broker rejected the connection")

            for packet_id, topic in enumerate(self._topics, start=1):
                await websocket.send_bytes(_subscribe_packet(topic, packet_id))
            pending_subacks = set(range(1, len(self._topics) + 1))
            while pending_subacks:
                packet_type, flags, body, buffer = await self._receive(
                    websocket, buffer
                )
                if packet_type == 9 and len(body) >= 3:
                    packet_id = int.from_bytes(body[:2], "big")
                    if body[2] == 0x80:
                        raise Domus40MqttError("MQTT broker rejected a subscription")
                    pending_subacks.discard(packet_id)
                elif packet_type == 3:
                    await self._async_handle_publish(websocket, flags, body)
                elif packet_type == 12:
                    await websocket.send_bytes(_packet(0xD0))
                elif packet_type == 13:
                    continue

            while True:
                try:
                    async with asyncio.timeout(35):
                        packet_type, flags, body, buffer = await self._receive(
                            websocket, buffer
                        )
                except TimeoutError:
                    await websocket.send_bytes(_packet(0xC0))
                    continue
                if packet_type == 3:
                    await self._async_handle_publish(websocket, flags, body)
                elif packet_type == 12:
                    await websocket.send_bytes(_packet(0xD0))

    async def _async_handle_publish(
        self,
        websocket: aiohttp.ClientWebSocketResponse,
        flags: int,
        body: bytes,
    ) -> None:
        """Dispatch one PUBLISH and acknowledge QoS 1 messages."""
        topic, payload, packet_id = _publish_message(flags, body)
        result = self._callback(topic, payload)
        if inspect.isawaitable(result):
            await result
        if packet_id is not None:
            await websocket.send_bytes(_packet(0x40, packet_id.to_bytes(2, "big")))
