"""Pinned Domus40 protobuf wire decoder.

The Home Server serves this schema only after authentication. It is private and
version-coupled. Keep the field numbers in sync with ``events.proto`` and the
compatibility record; unknown fields are intentionally skipped for forwards
compatibility.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import re
import struct
from dataclasses import dataclass

from .const import BUTTON_ENDPOINTS, BUTTON_STATES, PROTO_SCHEMA_REVISION


class ProtobufDecodeError(ValueError):
    """Raised when a payload is not a valid supported protobuf message."""


@dataclass(frozen=True, slots=True)
class DeviceStateEvent:
    """The state-bearing fields of the private DeviceStateEvent message."""

    device_id: str
    endpoint: int | None = None
    energy_level: int | None = None
    energy_change_state: int | None = None
    button_state: int | None = None
    time_active: int | None = None
    schema_revision: str = PROTO_SCHEMA_REVISION


@dataclass(frozen=True, slots=True)
class DeviceInstantReadingEvent:
    """The instantaneous readings of one metering-capable device."""

    device_id: str
    power_measured_ma: int | None = None
    power_factor: int | None = None
    consumed_mw: int | None = None
    voltage_v: int | None = None
    temperature: float | None = None
    external_temperature: float | None = None
    luminance: int | None = None
    schema_revision: str = PROTO_SCHEMA_REVISION

    @property
    def power_w(self) -> float | None:
        """Return the official client's milliwatt field in watts."""
        if self.consumed_mw is None:
            return None
        return self.consumed_mw / 1000


_EXPECTED_STATE_FIELDS = {
    "info": ("required", "MessageInfo", 1),
    "deviceId": ("required", "int64", 2),
    "endpoint": ("optional", "DeviceEndpoint", 100),
    "buttonState": ("optional", "ButtonState", 101),
    "energyChangeState": ("optional", "RegulatorState", 200),
    "energyLevel": ("optional", "uint32", 201),
    "timeActive": ("optional", "uint32", 301),
}

_EXPECTED_INSTANT_FIELDS = {
    "info": ("required", "MessageInfo", 1),
    "deviceId": ("required", "int64", 2),
    "powerMeasured_mA": ("optional", "int64", 10),
    "powerFactor": ("optional", "int32", 11),
    "consumed_mW": ("optional", "int64", 12),
    "voltage_V": ("optional", "int32", 13),
    "temperature": ("optional", "double", 20),
    "externalTemperature": ("optional", "double", 21),
    "luminance": ("optional", "int32", 22),
}

STATE_EVENT_FIELD_NUMBERS = frozenset(
    value[2] for value in _EXPECTED_STATE_FIELDS.values()
)
INSTANT_EVENT_FIELD_NUMBERS = frozenset(
    value[2] for value in _EXPECTED_INSTANT_FIELDS.values()
)

_EXPECTED_DEVICE_ENDPOINTS = {name: value for name, value in BUTTON_ENDPOINTS.items()}
_EXPECTED_BUTTON_STATES = {
    "Pressed": next(
        value for value, name in BUTTON_STATES.items() if name == "pressed"
    ),
    "Released": next(
        value for value, name in BUTTON_STATES.items() if name == "released"
    ),
}


def schema_message_field_map(
    schema: str, message_name: str
) -> dict[str, dict[str, str | int]]:
    """Return the non-sensitive field contract for one protobuf message."""
    match = re.search(
        rf"message\s+{re.escape(message_name)}\s*\{{(?P<body>.*?)\}}",
        schema,
        re.DOTALL,
    )
    if match is None:
        return {}
    fields: dict[str, dict[str, str | int]] = {}
    for field in re.finditer(
        r"\b(optional|required|repeated)\s+([.A-Za-z0-9_]+)\s+"
        r"([A-Za-z0-9_]+)\s*=\s*(\d+)(?:\s*\[[^\]]*\])?\s*;",
        match.group("body"),
    ):
        label, field_type, name, number = field.groups()
        fields[name] = {
            "label": label,
            "type": field_type,
            "number": int(number),
        }
    return fields


def schema_field_map(schema: str) -> dict[str, dict[str, str | int]]:
    """Return the non-sensitive DeviceStateEvent field contract."""
    return schema_message_field_map(schema, "DeviceStateEvent")


def schema_fingerprint(schema: str) -> str:
    """Return a non-secret short fingerprint for diagnostics."""
    return hashlib.sha256(schema.encode()).hexdigest()[:12]


def schema_enum_map(schema: str, enum_name: str) -> dict[str, int]:
    """Return the numeric values of one enum from a private schema."""
    match = re.search(
        rf"\benum\s+{re.escape(enum_name)}\s*\{{(?P<body>.*?)\}}", schema, re.DOTALL
    )
    if match is None:
        return {}
    return {
        item.group(1): int(item.group(2))
        for item in re.finditer(
            r"\b([A-Za-z0-9_]+)\s*=\s*(-?\d+)\s*;", match.group("body")
        )
    }


def constants_schema_is_compatible(schema: str) -> bool:
    """Check the button enum values used for event routing."""
    endpoints = schema_enum_map(schema, "DeviceEndpoint")
    states = schema_enum_map(schema, "ButtonState")
    return all(
        endpoints.get(name) == value
        for name, value in _EXPECTED_DEVICE_ENDPOINTS.items()
    ) and all(
        states.get(name) == value for name, value in _EXPECTED_BUTTON_STATES.items()
    )


def schema_is_compatible(schema: str) -> bool:
    """Check the authenticated state-event schema against the baked field map."""
    fields = schema_message_field_map(schema, "DeviceStateEvent")
    for name, (label, field_type, field_number) in _EXPECTED_STATE_FIELDS.items():
        field = fields.get(name)
        if field is None or field != {
            "label": label,
            "type": field_type,
            "number": field_number,
        }:
            return False
    return True


def instant_schema_is_compatible(schema: str) -> bool:
    """Check the authenticated instantaneous-reading schema."""
    fields = schema_message_field_map(schema, "DeviceInstantReadingEvent")
    for name, (label, field_type, field_number) in _EXPECTED_INSTANT_FIELDS.items():
        field = fields.get(name)
        if field is None or field != {
            "label": label,
            "type": field_type,
            "number": field_number,
        }:
            return False
    return True


def _read_varint(payload: bytes, offset: int) -> tuple[int, int]:
    result = 0
    shift = 0
    while offset < len(payload) and shift < 70:
        value = payload[offset]
        offset += 1
        result |= (value & 0x7F) << shift
        if not value & 0x80:
            return result, offset
        shift += 7
    raise ProtobufDecodeError("truncated or oversized varint")


def _skip_field(payload: bytes, offset: int, wire_type: int) -> int:
    if wire_type == 0:
        _, offset = _read_varint(payload, offset)
        return offset
    if wire_type == 1:
        offset += 8
    elif wire_type == 2:
        size, offset = _read_varint(payload, offset)
        offset += size
    elif wire_type == 5:
        offset += 4
    else:
        raise ProtobufDecodeError(f"unsupported wire type {wire_type}")
    if offset > len(payload):
        raise ProtobufDecodeError("truncated field")
    return offset


def _binary_payload(payload: bytes) -> bytes:
    """Accept binary MQTT frames and the base64 form consumed by the web UI."""
    stripped = payload.strip()
    if not stripped:
        raise ProtobufDecodeError("empty payload")
    if (
        all(
            byte in b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/="
            for byte in stripped
        )
        and len(stripped) % 4 == 0
    ):
        try:
            decoded = base64.b64decode(stripped, validate=True)
        except binascii.Error:
            pass
        else:
            if decoded:
                return decoded
    return payload


def _signed_varint(value: int, bits: int) -> int:
    """Interpret an int32/int64 protobuf varint as two's complement."""
    mask = (1 << bits) - 1
    value &= mask
    sign_bit = 1 << (bits - 1)
    return value - (1 << bits) if value & sign_bit else value


def wire_field_signature(payload: bytes) -> tuple[tuple[int, int], ...]:
    """Return protobuf field numbers and wire types without retaining values."""
    wire = _binary_payload(payload)
    signature: list[tuple[int, int]] = []
    offset = 0
    while offset < len(wire):
        tag, offset = _read_varint(wire, offset)
        field_number = tag >> 3
        wire_type = tag & 0x07
        if field_number == 0:
            raise ProtobufDecodeError("protobuf field number is zero")
        signature.append((field_number, wire_type))
        offset = _skip_field(wire, offset, wire_type)
    return tuple(signature)


def decode_device_state_event(payload: bytes) -> DeviceStateEvent:
    """Decode the pinned private DeviceStateEvent schema.

    Schema v2 reserves field 1 for ``MessageInfo``. Only the scalar fields used
    by Home Assistant are decoded; unknown fields remain forward-compatible.
    """
    wire = _binary_payload(payload)
    values: dict[int, int] = {}
    offset = 0
    while offset < len(wire):
        tag, offset = _read_varint(wire, offset)
        field_number = tag >> 3
        wire_type = tag & 0x07
        if field_number in {2, 100, 101, 200, 201, 301} and wire_type == 0:
            values[field_number], offset = _read_varint(wire, offset)
        else:
            offset = _skip_field(wire, offset, wire_type)

    device_id = values.get(2)
    if device_id is None:
        raise ProtobufDecodeError("DeviceStateEvent has no device_id")
    level = values.get(201)
    if level is not None and not 0 <= level <= 100:
        raise ProtobufDecodeError("DeviceStateEvent energy_level is out of range")
    return DeviceStateEvent(
        device_id=str(device_id),
        endpoint=values.get(100),
        energy_level=level,
        energy_change_state=values.get(200),
        button_state=values.get(101),
        time_active=values.get(301),
    )


def decode_device_instant_reading_event(
    payload: bytes,
) -> DeviceInstantReadingEvent:
    """Decode the pinned private DeviceInstantReadingEvent schema."""
    wire = _binary_payload(payload)
    varints: dict[int, int] = {}
    doubles: dict[int, float] = {}
    offset = 0
    while offset < len(wire):
        tag, offset = _read_varint(wire, offset)
        field_number = tag >> 3
        wire_type = tag & 0x07
        if field_number in {2, 10, 11, 12, 13, 22} and wire_type == 0:
            varints[field_number], offset = _read_varint(wire, offset)
        elif field_number in {20, 21} and wire_type == 1:
            end = offset + 8
            if end > len(wire):
                raise ProtobufDecodeError("truncated double field")
            doubles[field_number] = struct.unpack("<d", wire[offset:end])[0]
            offset = end
        else:
            offset = _skip_field(wire, offset, wire_type)

    device_id = varints.get(2)
    if device_id is None:
        raise ProtobufDecodeError("DeviceInstantReadingEvent has no device_id")
    return DeviceInstantReadingEvent(
        device_id=str(_signed_varint(device_id, 64)),
        power_measured_ma=(_signed_varint(varints[10], 64) if 10 in varints else None),
        power_factor=(_signed_varint(varints[11], 32) if 11 in varints else None),
        consumed_mw=(_signed_varint(varints[12], 64) if 12 in varints else None),
        voltage_v=_signed_varint(varints[13], 32) if 13 in varints else None,
        temperature=doubles.get(20),
        external_temperature=doubles.get(21),
        luminance=_signed_varint(varints[22], 32) if 22 in varints else None,
    )
