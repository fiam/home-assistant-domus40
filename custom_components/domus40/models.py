"""Data models for the EFAPEL Domus40 integration."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field, replace
from typing import Any

from .const import (
    HEADER_SWITCHES,
    TYPE_BLINDS,
    TYPE_DIMMER,
    TYPE_LIGHTS,
    TYPE_PLUGS,
)


def _optional_bool(value: Any) -> bool | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"true", "on", "1"}:
            return True
        if normalized in {"false", "off", "0"}:
            return False
    return None


def _level(value: Any, *, is_on: bool | None) -> int:
    try:
        result = round(float(value))
    except TypeError, ValueError:
        return 100 if is_on else 0
    return max(0, min(100, result))


def _display_name(value: Any) -> str | None:
    """Return a non-empty Home Server display name."""
    if not isinstance(value, str) or not (name := value.strip()):
        return None
    return name


def _normalized_location_name(name: str) -> str:
    """Match Home Assistant's floor and area name normalization."""
    return name.casefold().replace(" ", "")


def _identifier_sort_key(identifier: str) -> tuple[int, int | str]:
    """Sort numeric private IDs deterministically without exposing them."""
    try:
        return (0, int(identifier))
    except ValueError:
        return (1, identifier)


@dataclass(frozen=True, slots=True)
class Domus40Location:
    """One Domus40 division and its optional parent area."""

    division_id: str
    division_name: str
    floor_name: str | None
    ha_area_name: str


def _locations_from_api(
    raw_areas: list[dict[str, Any]], raw_divisions: list[dict[str, Any]]
) -> dict[str, Domus40Location]:
    """Build collision-safe HA area names from the two-level inventory."""
    area_names = {
        str(item["id"]): name
        for item in raw_areas
        if item.get("id") is not None
        and (name := _display_name(item.get("name"))) is not None
    }
    divisions: dict[str, tuple[str, str | None]] = {}
    for item in raw_divisions:
        if item.get("id") is None or (
            name := _display_name(item.get("name"))
        ) is None:
            continue
        raw_area = item.get("area")
        floor_name = (
            area_names.get(str(raw_area)) if raw_area is not None else None
        )
        divisions[str(item["id"])] = (name, floor_name)

    name_counts = Counter(
        _normalized_location_name(name) for name, _floor in divisions.values()
    )
    locations: dict[str, Domus40Location] = {}
    used_names = {
        normalized
        for normalized, count in name_counts.items()
        if count == 1
    }
    for division_id in sorted(divisions, key=_identifier_sort_key):
        division_name, floor_name = divisions[division_id]
        normalized = _normalized_location_name(division_name)
        if name_counts[normalized] == 1:
            ha_area_name = division_name
        else:
            base_name = (
                f"{floor_name} · {division_name}"
                if floor_name is not None
                else division_name
            )
            ha_area_name = base_name
            suffix = 2
            while _normalized_location_name(ha_area_name) in used_names:
                ha_area_name = f"{base_name} · {suffix}"
                suffix += 1
            used_names.add(_normalized_location_name(ha_area_name))
        locations[division_id] = Domus40Location(
            division_id=division_id,
            division_name=division_name,
            floor_name=floor_name,
            ha_area_name=ha_area_name,
        )
    return locations


@dataclass(frozen=True, slots=True)
class Domus40Device:
    """An actionable device returned by the Home Server."""

    device_id: str
    name: str
    device_type: str
    division_id: str | None
    division_name: str | None
    is_on: bool
    level: int
    supports_level: bool
    supports_on_off: bool
    supports_metering: bool
    supports_abcd_buttons: bool
    supports_ir_buttons: bool
    header_filter: str | None
    endpoint: str | None
    hardware_address: str | None
    firmware_version: str | None
    floor_name: str | None = None
    ha_area_name: str | None = None

    @property
    def supports_entity(self) -> bool:
        """Return whether this endpoint row represents an actionable output."""
        if self.device_type == TYPE_BLINDS:
            return self.supports_level
        if self.device_type in TYPE_LIGHTS:
            return self.supports_level or self.supports_on_off
        if self.device_type in TYPE_PLUGS:
            return self.supports_on_off
        return False

    @property
    def supports_button_events(self) -> bool:
        """Return whether this logical row is an emitter/input device."""
        return self.header_filter == HEADER_SWITCHES and (
            self.supports_abcd_buttons or self.supports_ir_buttons
        )

    @property
    def supports_receiver_identify(self) -> bool:
        """Return whether this row is a light or blind receiver."""
        return self.supports_entity and (
            self.device_type == TYPE_BLINDS or self.device_type in TYPE_LIGHTS
        )

    @property
    def supports_wall_button_events(self) -> bool:
        """Return whether this emitter exposes its four physical wall keys."""
        return self.supports_button_events and self.supports_abcd_buttons

    @property
    def supports_ir_button_events(self) -> bool:
        """Return whether this emitter exposes the multifunction IR keys."""
        return self.supports_button_events and self.supports_ir_buttons

    @classmethod
    def from_api(
        cls, data: dict[str, Any], locations: dict[str, Domus40Location]
    ) -> Domus40Device | None:
        """Build a device from the private REST representation."""
        raw_id = data.get("id")
        device_type = data.get("type")
        if raw_id is None or not isinstance(device_type, str):
            return None

        device_id = str(raw_id)
        hardware_address = data.get("hardwareAddress")
        if not isinstance(hardware_address, str) or not hardware_address:
            hardware_address = None

        display_name = data.get("displayName")
        name = (
            display_name
            if isinstance(display_name, str) and display_name.strip()
            else hardware_address or f"Domus40 device {device_id}"
        )

        raw_division = data.get("division")
        division_id = str(raw_division) if raw_division is not None else None
        location = locations.get(division_id) if division_id is not None else None
        switched_on = _optional_bool(data.get("switchedOn"))
        level = _level(data.get("levelPercentage"), is_on=switched_on)
        supports_level = bool(_optional_bool(data.get("capLevel")))
        supports_on_off = bool(_optional_bool(data.get("capOnOff")))
        supports_metering = bool(_optional_bool(data.get("capMetering")))
        supports_abcd_buttons = bool(_optional_bool(data.get("capABCDButtons")))
        supports_ir_buttons = bool(_optional_bool(data.get("capIRButtons")))
        header_filter = data.get("headerFilter")
        if not isinstance(header_filter, str) or not header_filter:
            header_filter = None
        endpoint = data.get("endpoint")
        if not isinstance(endpoint, str) or not endpoint:
            endpoint = None
        # The live Home Server reports switchedOn=false for every regulator,
        # including lit outputs. Its regulator level is the authoritative
        # on/off state as well as the brightness value.
        is_on = (
            level > 0
            if device_type == TYPE_DIMMER and supports_level
            else switched_on
            if switched_on is not None
            else level > 0
        )

        firmware_version = data.get("firmwareVersion")
        if not isinstance(firmware_version, str) or not firmware_version:
            firmware_version = None

        return cls(
            device_id=device_id,
            name=name,
            device_type=device_type,
            division_id=division_id,
            division_name=location.division_name if location is not None else None,
            is_on=is_on,
            level=level,
            supports_level=supports_level,
            supports_on_off=supports_on_off,
            supports_metering=supports_metering,
            supports_abcd_buttons=supports_abcd_buttons,
            supports_ir_buttons=supports_ir_buttons,
            header_filter=header_filter,
            endpoint=endpoint,
            hardware_address=hardware_address,
            firmware_version=firmware_version,
            floor_name=location.floor_name if location is not None else None,
            ha_area_name=location.ha_area_name if location is not None else None,
        )

    def with_level(self, level: int) -> Domus40Device:
        """Return an optimistic copy with a new output level."""
        bounded = max(0, min(100, level))
        return replace(self, level=bounded, is_on=bounded > 0)


@dataclass(frozen=True, slots=True)
class Domus40State:
    """Authoritative state snapshot from the Home Server."""

    devices: dict[str, Domus40Device]
    locations: dict[str, Domus40Location] = field(default_factory=dict)

    def physical_siblings(self, device_id: str) -> tuple[Domus40Device, ...]:
        """Return all logical rows belonging to the same physical module."""
        device = self.devices[device_id]
        if device.hardware_address is None:
            return (device,)
        return tuple(
            candidate
            for candidate in self.devices.values()
            if candidate.hardware_address == device.hardware_address
        )

    def primary_device(self, device_id: str) -> Domus40Device:
        """Choose the stable actuator-first HA device for a physical module."""
        endpoint_priority = {
            "AtuadorOnOff1": 0,
            "AtuadorOnOff2": 1,
            "AtuadorOnOffPlug": 2,
            "ControladorDePersiana": 3,
            "ReguladorDeLuz": 4,
        }

        def sort_key(device: Domus40Device) -> tuple[bool, int, tuple[int, str]]:
            try:
                id_key = (0, f"{int(device.device_id):020d}")
            except ValueError:
                id_key = (1, device.device_id)
            return (
                not device.supports_entity,
                endpoint_priority.get(device.endpoint or "", 99),
                id_key,
            )

        return min(self.physical_siblings(device_id), key=sort_key)

    @property
    def metering_devices(self) -> tuple[Domus40Device, ...]:
        """Return every metering-capable logical row in deterministic order."""
        return tuple(
            sorted(
                (
                    device
                    for device in self.devices.values()
                    if device.supports_metering
                ),
                key=lambda device: (len(device.device_id), device.device_id),
            )
        )

    @property
    def physical_device_count(self) -> int:
        """Return the number of physical modules without exposing addresses."""
        groups = {
            device.hardware_address or f"logical:{device.device_id}"
            for device in self.devices.values()
        }
        return len(groups)

    @classmethod
    def from_api(
        cls,
        raw_devices: list[dict[str, Any]],
        raw_divisions: list[dict[str, Any]],
        raw_areas: list[dict[str, Any]],
    ) -> Domus40State:
        """Build a state snapshot from API responses."""
        locations = _locations_from_api(raw_areas, raw_divisions)
        devices: dict[str, Domus40Device] = {}
        for item in raw_devices:
            device = Domus40Device.from_api(item, locations)
            if device is not None:
                devices[device.device_id] = device
        return cls(devices=devices, locations=locations)

    def with_device_level(self, device_id: str, level: int) -> Domus40State:
        """Return a snapshot updated from a validated push event."""
        device = self.devices.get(device_id)
        if device is None:
            return self
        devices = dict(self.devices)
        devices[device_id] = device.with_level(level)
        return replace(self, devices=devices)


@dataclass(frozen=True, slots=True)
class Domus40ScenarioAction:
    """One action the Home Server executes for a button endpoint."""

    target_device_id: str
    target_name: str
    action: str
    level: int | None = None

    @classmethod
    def from_api(
        cls, data: dict[str, Any], devices: dict[str, Domus40Device]
    ) -> Domus40ScenarioAction | None:
        """Build an action while resolving its target to a user-facing name."""
        raw_target = data.get("targetDeviceId")
        action = data.get("action")
        if raw_target is None or not isinstance(action, str) or not action:
            return None

        target = devices.get(str(raw_target))
        target_name = target.name if target is not None else "Unknown Domus40 device"
        raw_level = data.get("level")
        level: int | None = None
        if isinstance(raw_level, (int, float)):
            level = max(0, min(100, round(raw_level)))
        return cls(
            target_device_id=str(raw_target),
            target_name=target_name,
            action=action,
            level=level,
        )

    @property
    def description(self) -> str:
        """Return a compact user-facing description of this action."""
        if self.action == "SetOn":
            operation = "turn on"
        elif self.action == "SetOff":
            operation = "turn off"
        elif self.action == "Toggle":
            operation = "toggle"
        elif self.action == "SetLevel" and self.level is not None:
            operation = f"set to {self.level}%"
        else:
            operation = self.action
        return f"{self.target_name}: {operation}"

    def as_entity_attribute(self) -> dict[str, str | int]:
        """Return the non-sensitive mapping shown by Home Assistant."""
        result: dict[str, str | int] = {
            "target": self.target_name,
            "action": self.action,
        }
        if self.level is not None:
            result["level"] = self.level
        return result


@dataclass(frozen=True, slots=True)
class Domus40ButtonBinding:
    """The scenario currently assigned to one wall or IR endpoint."""

    device_id: str
    endpoint: str
    actions: tuple[Domus40ScenarioAction, ...] = ()

    @property
    def description(self) -> str:
        """Return a readable summary without exposing internal identifiers."""
        if not self.actions:
            return "Unassigned"
        return ", ".join(action.description for action in self.actions)


@dataclass(frozen=True, slots=True)
class MqttInfo:
    """Ephemeral MQTT credentials returned by authentication."""

    username: str
    password: str
    prefix: str = ""

    @classmethod
    def from_api(cls, data: Any) -> MqttInfo | None:
        """Parse MQTT connection information without retaining the raw object."""
        if not isinstance(data, dict):
            return None
        username = data.get("username")
        password = data.get("password")
        prefix = data.get("prefix", "")
        if not isinstance(username, str) or not isinstance(password, str):
            return None
        return cls(
            username=username,
            password=password,
            prefix=prefix if isinstance(prefix, str) else "",
        )
