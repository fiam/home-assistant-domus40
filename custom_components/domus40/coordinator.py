"""State coordinator for the EFAPEL Domus40 integration."""

from __future__ import annotations

import asyncio
import logging
from collections import Counter
from collections.abc import Callable, Iterable
from dataclasses import dataclass, replace

import aiohttp
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import Domus40AuthError, Domus40Client, Domus40Error
from .const import (
    BUTTON_ENDPOINTS,
    BUTTON_STATES,
    DEFAULT_POLL_INTERVAL,
    DOMAIN,
    IR_BUTTON_ENDPOINTS,
    METERING_SAMPLE_SECONDS,
    MQTT_METERING_TOPIC,
    MQTT_MONITOR_TOPIC,
    MQTT_STATE_TOPIC,
    WALL_BUTTON_ENDPOINTS,
    identify_duration_seconds,
    monitor_unknown_messages,
)
from .models import Domus40ButtonBinding, Domus40Device, Domus40State
from .mqtt import (
    Domus40MqttClient,
    Domus40MqttError,
    topic_matches_subscription,
)
from .proto import (
    INSTANT_EVENT_FIELD_NUMBERS,
    STATE_EVENT_FIELD_NUMBERS,
    DeviceInstantReadingEvent,
    DeviceStateEvent,
    ProtobufDecodeError,
    constants_schema_is_compatible,
    decode_device_instant_reading_event,
    decode_device_state_event,
    instant_schema_is_compatible,
    schema_field_map,
    schema_fingerprint,
    schema_is_compatible,
    schema_message_field_map,
    wire_field_signature,
)

_LOGGER = logging.getLogger(__name__)

WRITE_CONFIRM_TIMEOUT = 15.0
WRITE_REFRESH_DELAYS = (0.5, 1.5, 3.0, 5.0)
FALLBACK_REFRESH_DELAY = 0.5
REST_REFRESH_MIN_INTERVAL = 5.0
DECODE_WARNING_INTERVAL = 100
BUTTON_MAPPING_CONCURRENCY = 2
REPORTING_CONCURRENCY = 2
REPORTING_BATCH_SIZE = 8
MAX_UNKNOWN_SIGNATURES = 64

_SAFE_TOPIC_SEGMENTS = frozenset(
    {
        "changed",
        "cloud",
        "device",
        "events",
        "eula",
        "instant",
        "notification",
        "pen",
        "progress",
        "reading",
        "reset",
        "state",
        "system",
    }
)


@dataclass(frozen=True, slots=True)
class _PendingLevel:
    """An optimistic output level awaiting confirmation from REST."""

    level: int
    expires_at: float


def _reconcile_pending_levels(
    state: Domus40State,
    pending: dict[str, _PendingLevel],
    now: float,
) -> Domus40State:
    """Mask lagging REST values until a write is confirmed or times out."""
    devices: dict[str, Domus40Device] | None = None
    for device_id, write in tuple(pending.items()):
        device = state.devices.get(device_id)
        if device is None:
            pending.pop(device_id, None)
            continue

        confirmed = (
            device.level == write.level
            if device.supports_level
            else device.is_on == (write.level > 0)
        )
        if confirmed or now >= write.expires_at:
            pending.pop(device_id, None)
            continue

        if devices is None:
            devices = dict(state.devices)
        devices[device_id] = device.with_level(write.level)

    return state if devices is None else replace(state, devices=devices)


def _mapped_emitter_ids(
    bindings: Iterable[Domus40ButtonBinding], target_ids: set[str]
) -> set[str]:
    """Reverse the hub's button mappings for one or more receiver rows."""
    return {
        binding.device_id
        for binding in bindings
        if any(action.target_device_id in target_ids for action in binding.actions)
    }


def _reporting_batches(
    devices: Iterable[Domus40Device], batch_size: int = REPORTING_BATCH_SIZE
) -> tuple[tuple[str, ...], ...]:
    """Build bounded batches with at most one row per physical module."""
    remaining = list(devices)
    batches: list[tuple[str, ...]] = []
    while remaining:
        batch: list[str] = []
        deferred: list[Domus40Device] = []
        hardware_addresses: set[str] = set()
        for device in remaining:
            hardware_key = device.hardware_address or f"logical:{device.device_id}"
            if len(batch) < batch_size and hardware_key not in hardware_addresses:
                batch.append(device.device_id)
                hardware_addresses.add(hardware_key)
            else:
                deferred.append(device)
        batches.append(tuple(batch))
        remaining = deferred
    return tuple(batches)


def _topic_shape(topic: str, prefix: str) -> str:
    """Remove the private prefix and redact every unrecognised topic value."""
    relative = topic.removeprefix(prefix) if prefix else topic
    return "/".join(
        segment if segment in _SAFE_TOPIC_SEGMENTS else "{value}"
        for segment in relative.split("/")
    )


def _unexpected_wire_signature(
    payload: bytes, known_fields: frozenset[int]
) -> str | None:
    """Describe unknown top-level protobuf fields without retaining values."""
    signature = wire_field_signature(payload)
    unknown = sorted({item for item in signature if item[0] not in known_fields})
    if not unknown:
        return None
    return ",".join(f"{field}:{wire_type}" for field, wire_type in unknown)


class Domus40Coordinator(DataUpdateCoordinator[Domus40State]):
    """Combine authoritative polling with decoded MQTT push events."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        client: Domus40Client,
        session: aiohttp.ClientSession,
    ) -> None:
        """Initialize the coordinator."""
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=DOMAIN,
            update_interval=DEFAULT_POLL_INTERVAL,
            always_update=False,
        )
        self.client = client
        self._entry = entry
        self._session = session
        self._push_task: asyncio.Task[None] | None = None
        self._reporting_task: asyncio.Task[None] | None = None
        self._write_refresh_task: asyncio.Task[None] | None = None
        self._fallback_refresh_task: asyncio.Task[None] | None = None
        self._binding_task: asyncio.Task[None] | None = None
        self._ir_binding_tasks: dict[str, asyncio.Task[None]] = {}
        self._pending_levels: dict[str, _PendingLevel] = {}
        self._recent_push_levels: dict[str, _PendingLevel] = {}
        self._fallback_refresh_requested = False
        self._last_fallback_refresh_at: float | None = None
        self._last_rest_refresh_at: float | None = None
        self._rest_refresh_lock = asyncio.Lock()
        self._shutting_down = False
        self.schema_compatible = False
        self.metering_schema_compatible = False
        self.schema_fingerprint: str | None = None
        self.constants_schema_fingerprint: str | None = None
        self.schema_fields: dict[str, dict[str, str | int]] = {}
        self.metering_schema_fields: dict[str, dict[str, str | int]] = {}
        self.power_readings: dict[str, DeviceInstantReadingEvent] = {}
        self.reporting_active_ids: set[str] = set()
        self.reporting_activation_failures = 0
        self.button_bindings: dict[tuple[str, str], Domus40ButtonBinding] = {}
        self.button_mapping_failures = 0
        self.wall_mappings_loaded = False
        self._button_listeners: set[Callable[[DeviceStateEvent], None]] = set()
        self.push_messages_received = 0
        self.push_messages_decoded = 0
        self.push_decode_failures = 0
        self.push_state_decode_failures = 0
        self.push_metering_decode_failures = 0
        self.push_schema_mismatch_messages = 0
        self.push_unhandled_state_messages = 0
        self.push_state_updates = 0
        self.push_button_events = 0
        self.push_metering_messages = 0
        self.push_power_updates = 0
        self.unknown_topic_shapes: Counter[str] = Counter()
        self.unknown_wire_signatures: Counter[str] = Counter()
        self.unknown_observations_dropped = 0
        self.rest_refresh_attempts = 0
        self.write_refresh_requests = 0
        self.fallback_refresh_requests = 0

    @property
    def pending_write_count(self) -> int:
        """Return the number of outputs awaiting authoritative confirmation."""
        return len(self._pending_levels)

    @property
    def recent_push_guard_count(self) -> int:
        """Return the number of push states protected from stale REST readback."""
        return len(self._recent_push_levels)

    @property
    def rest_refresh_min_interval(self) -> float:
        """Return the global minimum interval between full REST refreshes."""
        return REST_REFRESH_MIN_INTERVAL

    @property
    def unknown_monitoring_enabled(self) -> bool:
        """Return whether broad, redacted MQTT observation is enabled."""
        return monitor_unknown_messages(self._entry.options)

    async def _async_update_data(self) -> Domus40State:
        async with self._rest_refresh_lock:
            loop = asyncio.get_running_loop()
            if self._last_rest_refresh_at is not None:
                delay = max(
                    0.0,
                    self._last_rest_refresh_at
                    + REST_REFRESH_MIN_INTERVAL
                    - loop.time(),
                )
                if delay:
                    await asyncio.sleep(delay)
            self._last_rest_refresh_at = loop.time()
            self.rest_refresh_attempts += 1
            try:
                state = await self.client.async_get_state()
            except Domus40AuthError as err:
                raise ConfigEntryAuthFailed from err
            except Domus40Error as err:
                raise UpdateFailed(
                    "Unable to update from the Domus40 Home Server"
                ) from err
            state = _reconcile_pending_levels(state, self._pending_levels, loop.time())
            return _reconcile_pending_levels(
                state, self._recent_push_levels, loop.time()
            )

    async def async_set_device_level(self, device_id: str, level: int) -> None:
        """Write a level without exposing the Home Server's stale readback."""
        bounded = max(0, min(100, level))
        await self.client.async_set_level(device_id, bounded)
        self._pending_levels[device_id] = _PendingLevel(
            bounded,
            asyncio.get_running_loop().time() + WRITE_CONFIRM_TIMEOUT,
        )
        self._recent_push_levels.pop(device_id, None)
        if self.data is not None:
            self.async_set_updated_data(self.data.with_device_level(device_id, bounded))
        self._schedule_write_refresh()

    async def async_identify_device(self, device_id: str) -> None:
        """Ask the Home Server to blink exactly one logical device ID."""
        await self.client.async_identify_device(
            device_id,
            identify_duration_seconds(self._entry.options),
        )

    async def async_identify_associated_emitters(self, receiver_id: str) -> None:
        """Blink every emitter whose current mapping targets this receiver ID."""
        task = self._binding_task
        if task is None:
            self.async_start_button_binding_load()
            task = self._binding_task
        if task is not None and not task.done():
            await asyncio.shield(task)

        state = self.data
        if state is None or receiver_id not in state.devices:
            return
        emitter_ids = _mapped_emitter_ids(self.button_bindings.values(), {receiver_id})
        await asyncio.gather(
            *(
                self.async_identify_device(emitter_id)
                for emitter_id in sorted(emitter_ids)
                if emitter_id in state.devices
                and state.devices[emitter_id].supports_button_events
            )
        )

    async def async_start_push(self) -> None:
        """Validate the private schema and start the MQTT listener."""
        events_schema, constants_schema = await asyncio.gather(
            self.client.async_get_events_schema(),
            self.client.async_get_proto_schema("constants.proto"),
        )
        if events_schema:
            self.schema_fingerprint = schema_fingerprint(events_schema)
            self.schema_fields = schema_field_map(events_schema)
            self.metering_schema_fields = schema_message_field_map(
                events_schema, "DeviceInstantReadingEvent"
            )
        if constants_schema:
            self.constants_schema_fingerprint = schema_fingerprint(constants_schema)
        self.schema_compatible = bool(
            events_schema
            and constants_schema
            and schema_is_compatible(events_schema)
            and constants_schema_is_compatible(constants_schema)
        )
        self.metering_schema_compatible = bool(
            events_schema and instant_schema_is_compatible(events_schema)
        )
        if not self.schema_compatible:
            _LOGGER.warning(
                "Domus40 state-event protobuf schema differs from the baked "
                "revision (events=%s, constants=%s); state pushes will use "
                "rate-limited REST refreshes only",
                self.schema_fingerprint or "unavailable",
                self.constants_schema_fingerprint or "unavailable",
            )
        if events_schema and not self.metering_schema_compatible:
            _LOGGER.warning(
                "Domus40 metering-event protobuf schema differs from the baked "
                "revision; instantaneous power sensors are disabled"
            )

        if self.client.mqtt_info is not None:
            self._push_task = asyncio.create_task(
                self._async_push_loop(), name=f"{DOMAIN}-mqtt"
            )

    def async_start_reporting(self) -> None:
        """Start metering after the one-time emitter mapping load completes."""
        state = self.data
        if (
            self._reporting_task is not None
            or not self.metering_schema_compatible
            or state is None
            or not state.metering_devices
        ):
            return
        self._reporting_task = asyncio.create_task(
            self._async_reporting_after_bindings(), name=f"{DOMAIN}-reporting"
        )

    async def _async_reporting_after_bindings(self) -> None:
        """Avoid competing with the initial scenario-discovery workload."""
        if self._binding_task is not None:
            try:
                await self._binding_task
            except asyncio.CancelledError:
                raise
            except Exception:
                _LOGGER.exception(
                    "Unexpected Domus40 button-mapping failure; starting metering"
                )
        if not self._shutting_down:
            await self._async_reporting_loop()

    def initialize_button_bindings(self) -> None:
        """Create immediate placeholders for every physical wall key."""
        state = self.data
        if state is None:
            return
        pairs = [
            (device.device_id, endpoint)
            for device in state.devices.values()
            if device.supports_wall_button_events
            for endpoint in WALL_BUTTON_ENDPOINTS
        ]
        self.button_bindings = {pair: Domus40ButtonBinding(*pair) for pair in pairs}

    def async_start_button_binding_load(self) -> None:
        """Discover wall-key assignments without delaying integration setup."""
        if self._binding_task and not self._binding_task.done():
            return
        self._binding_task = asyncio.create_task(
            self.async_load_button_bindings(), name=f"{DOMAIN}-button-bindings"
        )

    async def async_load_button_bindings(self) -> None:
        """Discover the Home Server scenarios assigned to all wall emitters."""
        state = self.data
        if state is None:
            return
        devices = [
            device
            for device in state.devices.values()
            if device.supports_wall_button_events
        ]
        if not self.button_bindings:
            self.initialize_button_bindings()
        pairs = list(self.button_bindings)
        if not pairs:
            self.wall_mappings_loaded = True
            return

        semaphore = asyncio.Semaphore(BUTTON_MAPPING_CONCURRENCY)

        async def load_device(
            device: Domus40Device,
        ) -> dict[str, Domus40ButtonBinding]:
            async with semaphore:
                return await self.client.async_get_button_bindings(
                    device.device_id, tuple(WALL_BUTTON_ENDPOINTS), state
                )

        results = await asyncio.gather(
            *(load_device(device) for device in devices),
            return_exceptions=True,
        )
        failures = 0
        for device, result in zip(devices, results, strict=True):
            if isinstance(result, asyncio.CancelledError):
                raise result
            if isinstance(result, Exception):
                failures += len(WALL_BUTTON_ENDPOINTS)
                continue
            self.button_bindings.update(
                ((device.device_id, endpoint), binding)
                for endpoint, binding in result.items()
            )
        self.button_mapping_failures = failures
        self.wall_mappings_loaded = True
        self.async_update_listeners()
        if failures:
            _LOGGER.warning(
                "Could not read %s of %s Domus40 button assignments",
                failures,
                len(pairs),
            )

    def async_start_ir_binding_load(self, device_id: str) -> None:
        """Load one enabled IR receiver's assignments in the background."""
        task = self._ir_binding_tasks.get(device_id)
        if task and not task.done():
            return
        self._ir_binding_tasks[device_id] = asyncio.create_task(
            self._async_load_ir_bindings(device_id),
            name=f"{DOMAIN}-ir-bindings-{device_id}",
        )

    async def _async_load_ir_bindings(self, device_id: str) -> None:
        state = self.data
        if state is None:
            return
        device = state.devices.get(device_id)
        if device is None or not device.supports_ir_button_events:
            return
        endpoints = tuple(IR_BUTTON_ENDPOINTS)
        self.button_bindings.update(
            {
                (device_id, endpoint): Domus40ButtonBinding(device_id, endpoint)
                for endpoint in endpoints
            }
        )
        try:
            bindings = await self.client.async_get_button_bindings(
                device_id, endpoints, state
            )
        except asyncio.CancelledError:
            raise
        except Domus40Error as err:
            _LOGGER.warning(
                "Could not read Domus40 IR assignments for one emitter: %s",
                type(err).__name__,
            )
            return
        self.button_bindings.update(
            ((device_id, endpoint), binding) for endpoint, binding in bindings.items()
        )
        self.async_update_listeners()

    @callback
    def async_add_button_listener(
        self, listener: Callable[[DeviceStateEvent], None]
    ) -> Callable[[], None]:
        """Subscribe an entity to decoded physical button events."""
        self._button_listeners.add(listener)

        @callback
        def remove_listener() -> None:
            self._button_listeners.discard(listener)

        return remove_listener

    async def async_options_updated(self) -> None:
        """Reconnect MQTT so an observer-option change takes effect in place."""
        if self._shutting_down or self.client.mqtt_info is None:
            return
        if self._push_task is not None and not self._push_task.done():
            self._push_task.cancel()
            await asyncio.gather(self._push_task, return_exceptions=True)
        self._push_task = asyncio.create_task(
            self._async_push_loop(), name=f"{DOMAIN}-mqtt"
        )

    def _mqtt_topics(self) -> tuple[str, ...]:
        """Return either the known filters or one opt-in observation filter."""
        mqtt_info = self.client.mqtt_info
        if mqtt_info is None:
            return ()
        if self.unknown_monitoring_enabled:
            return (f"{mqtt_info.prefix}{MQTT_MONITOR_TOPIC}",)
        return (
            f"{mqtt_info.prefix}{MQTT_STATE_TOPIC}",
            f"{mqtt_info.prefix}{MQTT_METERING_TOPIC}",
        )

    async def _async_reporting_loop(self) -> None:
        """Sample metered devices in bounded batches without overloading the hub."""
        state = self.data
        if state is None:
            return
        batches = _reporting_batches(state.metering_devices)
        semaphore = asyncio.Semaphore(REPORTING_CONCURRENCY)
        mqtt_info = self.client.mqtt_info
        prefix = mqtt_info.prefix if mqtt_info is not None else ""
        reporting_filter = f"{prefix}{MQTT_METERING_TOPIC}"

        async def activate(device_id: str) -> tuple[str, bool]:
            async with semaphore:
                try:
                    topic = await self.client.async_activate_reporting(device_id)
                except asyncio.CancelledError:
                    raise
                except Domus40Error:
                    return device_id, False
                if not topic_matches_subscription(reporting_filter, topic):
                    try:
                        await self.client.async_deactivate_reporting(device_id)
                    except Domus40Error:
                        pass
                    return device_id, False
                return device_id, True

        async def deactivate(device_id: str) -> None:
            async with semaphore:
                await self._async_deactivate_reporting(device_id)

        batch_index = 0
        while True:
            batch = batches[batch_index]
            batch_index = (batch_index + 1) % len(batches)
            results = await asyncio.gather(*(activate(item) for item in batch))
            active = {device_id for device_id, succeeded in results if succeeded}
            self.reporting_activation_failures += len(batch) - len(active)
            if active != self.reporting_active_ids:
                self.reporting_active_ids = active
                self.async_update_listeners()
            await asyncio.sleep(METERING_SAMPLE_SECONDS)
            await asyncio.gather(*(deactivate(device_id) for device_id in active))
            if self.reporting_active_ids:
                self.reporting_active_ids.clear()
                self.async_update_listeners()

    async def _async_deactivate_reporting(self, device_id: str) -> None:
        """Best-effort release of one instantaneous reporting lease."""
        try:
            await self.client.async_deactivate_reporting(device_id)
        except Domus40Error:
            pass

    def _record_unknown_message(self, topic: str, payload: bytes) -> None:
        """Record only a redacted topic shape and field/wire signature."""
        mqtt_info = self.client.mqtt_info
        prefix = mqtt_info.prefix if mqtt_info is not None else ""
        shape = _topic_shape(topic, prefix)
        if (
            shape not in self.unknown_topic_shapes
            and len(self.unknown_topic_shapes) >= MAX_UNKNOWN_SIGNATURES
        ):
            self.unknown_observations_dropped += 1
            return
        self.unknown_topic_shapes[shape] += 1
        try:
            wire = wire_field_signature(payload)
        except ProtobufDecodeError:
            signature = "not-protobuf"
        else:
            fields = sorted(set(wire))[:16]
            signature = ",".join(f"{field}:{wire_type}" for field, wire_type in fields)
            if len(set(wire)) > len(fields):
                signature = f"{signature},..."
            if not signature:
                signature = "empty"
        key = f"{shape}|{signature}"
        if (
            key not in self.unknown_wire_signatures
            and len(self.unknown_wire_signatures) >= MAX_UNKNOWN_SIGNATURES
        ):
            self.unknown_observations_dropped += 1
            return
        first_observation = self.unknown_wire_signatures[key] == 0
        self.unknown_wire_signatures[key] += 1
        if first_observation:
            _LOGGER.info(
                "Observed a new redacted Domus40 MQTT shape: %s (%s)",
                shape,
                signature,
            )

    def _record_unexpected_fields(
        self, message_name: str, payload: bytes, known_fields: frozenset[int]
    ) -> None:
        """Count additive fields on known messages when monitoring is enabled."""
        if not self.unknown_monitoring_enabled:
            return
        try:
            signature = _unexpected_wire_signature(payload, known_fields)
        except ProtobufDecodeError:
            return
        if signature is None:
            return
        key = f"{message_name}|{signature}"
        if (
            key not in self.unknown_wire_signatures
            and len(self.unknown_wire_signatures) >= MAX_UNKNOWN_SIGNATURES
        ):
            self.unknown_observations_dropped += 1
            return
        first_observation = self.unknown_wire_signatures[key] == 0
        self.unknown_wire_signatures[key] += 1
        if first_observation:
            _LOGGER.info(
                "Observed new fields on a Domus40 %s message (%s)",
                message_name,
                signature,
            )

    async def _async_push_loop(self) -> None:
        mqtt_info = self.client.mqtt_info
        if mqtt_info is None:
            return
        backoff = 1
        while True:
            try:
                mqtt = Domus40MqttClient(
                    self.client.base_url,
                    mqtt_info,
                    self._session,
                    self._async_handle_push,
                    self._mqtt_topics(),
                )
                await mqtt.async_listen()
                backoff = 1
            except asyncio.CancelledError:
                raise
            except (Domus40MqttError, aiohttp.ClientError, TimeoutError) as err:
                _LOGGER.debug(
                    "Domus40 MQTT listener reconnecting after %s",
                    type(err).__name__,
                )
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 30)

    async def _async_handle_push(self, topic: str, payload: bytes) -> None:
        self.push_messages_received += 1
        mqtt_info = self.client.mqtt_info
        prefix = mqtt_info.prefix if mqtt_info is not None else ""
        metering_filter = f"{prefix}{MQTT_METERING_TOPIC}"
        state_filter = f"{prefix}{MQTT_STATE_TOPIC}"

        if topic_matches_subscription(metering_filter, topic):
            self.push_metering_messages += 1
            self._record_unexpected_fields(
                "instant-reading", payload, INSTANT_EVENT_FIELD_NUMBERS
            )
            if not self.metering_schema_compatible or self.data is None:
                return
            try:
                reading = decode_device_instant_reading_event(payload)
            except ProtobufDecodeError:
                self._record_decode_failure("metering", topic, payload)
            else:
                self.push_messages_decoded += 1
                device = self.data.devices.get(reading.device_id)
                if (
                    device is not None
                    and device.supports_metering
                    and reading.power_w is not None
                ):
                    self.power_readings[reading.device_id] = reading
                    self.push_power_updates += 1
                    self.async_update_listeners()
            return

        if not topic_matches_subscription(state_filter, topic):
            if self.unknown_monitoring_enabled:
                self._record_unknown_message(topic, payload)
            return

        self._record_unexpected_fields("state", payload, STATE_EVENT_FIELD_NUMBERS)
        if not self.schema_compatible or self.data is None:
            self.push_schema_mismatch_messages += 1
            self._schedule_fallback_refresh()
            return
        try:
            event = decode_device_state_event(payload)
        except ProtobufDecodeError:
            self._record_decode_failure("state", topic, payload)
            self._schedule_fallback_refresh()
            return

        self.push_messages_decoded += 1
        handled = False
        if (
            event.endpoint in BUTTON_ENDPOINTS.values()
            and event.button_state in BUTTON_STATES
        ):
            handled = True
            self.push_button_events += 1
            for listener in tuple(self._button_listeners):
                listener(event)

        if event.energy_level is not None and event.device_id in self.data.devices:
            handled = True
            pending = self._pending_levels.get(event.device_id)
            if pending is None or pending.level == event.energy_level:
                self._pending_levels.pop(event.device_id, None)
                self._recent_push_levels[event.device_id] = _PendingLevel(
                    event.energy_level,
                    asyncio.get_running_loop().time() + WRITE_CONFIRM_TIMEOUT,
                )
                self.async_set_updated_data(
                    self.data.with_device_level(event.device_id, event.energy_level)
                )
                self.push_state_updates += 1
            else:
                self._schedule_write_refresh()

        if not handled:
            self.push_unhandled_state_messages += 1
            self._schedule_fallback_refresh()

    def _record_decode_failure(
        self, message_type: str, topic: str, payload: bytes
    ) -> None:
        """Count and safely surface protobuf failures without retaining payloads."""
        self.push_decode_failures += 1
        if message_type == "state":
            self.push_state_decode_failures += 1
            failures = self.push_state_decode_failures
        else:
            self.push_metering_decode_failures += 1
            failures = self.push_metering_decode_failures
        mqtt_info = self.client.mqtt_info
        prefix = mqtt_info.prefix if mqtt_info is not None else ""
        topic_shape = _topic_shape(topic, prefix)
        try:
            fields = sorted(set(wire_field_signature(payload)))[:16]
        except ProtobufDecodeError:
            wire_signature = "invalid"
        else:
            wire_signature = ",".join(
                f"{field}:{wire_type}" for field, wire_type in fields
            )
            if not wire_signature:
                wire_signature = "empty"
        if failures == 1 or failures % DECODE_WARNING_INTERVAL == 0:
            _LOGGER.warning(
                "Could not decode a Domus40 %s protobuf message (%s failures); "
                "topic shape=%s, wire signature=%s; no values or device "
                "information were retained",
                message_type,
                failures,
                topic_shape,
                wire_signature,
            )
        else:
            _LOGGER.debug(
                "Ignored an invalid Domus40 %s event; topic shape=%s, wire "
                "signature=%s",
                message_type,
                topic_shape,
                wire_signature,
            )

    async def _async_request_coordinated_refresh(self) -> None:
        """Request a REST refresh through the coordinator's global rate limit."""
        await self.async_request_refresh()

    def _schedule_write_refresh(self) -> None:
        """Reconcile optimistic commands with bounded-backoff REST reads."""
        if self._write_refresh_task and not self._write_refresh_task.done():
            return
        self._write_refresh_task = asyncio.create_task(
            self._async_write_refresh_loop(), name=f"{DOMAIN}-write-refresh"
        )

    async def _async_write_refresh_loop(self) -> None:
        delay_index = 0
        try:
            while self._pending_levels:
                delay = WRITE_REFRESH_DELAYS[
                    min(delay_index, len(WRITE_REFRESH_DELAYS) - 1)
                ]
                await asyncio.sleep(delay)
                if not self._pending_levels:
                    return
                self.write_refresh_requests += 1
                await self._async_request_coordinated_refresh()
                delay_index += 1
        finally:
            self._write_refresh_task = None
            if not self._shutting_down and self._pending_levels:
                self._schedule_write_refresh()

    def _schedule_fallback_refresh(self) -> None:
        """Coalesce undecodable state pushes into rate-limited REST reads."""
        self._fallback_refresh_requested = True
        if self._fallback_refresh_task and not self._fallback_refresh_task.done():
            return
        self._fallback_refresh_task = asyncio.create_task(
            self._async_fallback_refresh_loop(), name=f"{DOMAIN}-fallback-refresh"
        )

    async def _async_fallback_refresh_loop(self) -> None:
        loop = asyncio.get_running_loop()
        try:
            while self._fallback_refresh_requested:
                if self._last_fallback_refresh_at is None:
                    delay = FALLBACK_REFRESH_DELAY
                else:
                    delay = max(
                        0.0,
                        self._last_fallback_refresh_at
                        + REST_REFRESH_MIN_INTERVAL
                        - loop.time(),
                    )
                await asyncio.sleep(delay)
                self._fallback_refresh_requested = False
                self.fallback_refresh_requests += 1
                await self._async_request_coordinated_refresh()
                self._last_fallback_refresh_at = loop.time()
        finally:
            self._fallback_refresh_task = None
            if not self._shutting_down and self._fallback_refresh_requested:
                self._schedule_fallback_refresh()

    async def async_shutdown(self) -> None:
        """Stop background work and revoke the Home Server session."""
        self._shutting_down = True
        tasks = [
            task
            for task in (
                self._push_task,
                self._reporting_task,
                self._write_refresh_task,
                self._fallback_refresh_task,
                self._binding_task,
            )
            if task is not None
        ] + list(self._ir_binding_tasks.values())
        for task in tasks:
            if task is not None:
                task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        semaphore = asyncio.Semaphore(REPORTING_CONCURRENCY)

        async def deactivate(device_id: str) -> None:
            async with semaphore:
                await self._async_deactivate_reporting(device_id)

        await asyncio.gather(
            *(deactivate(device_id) for device_id in self.reporting_active_ids)
        )
        self.reporting_active_ids.clear()
        await self.client.async_logout()
