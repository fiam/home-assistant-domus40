"""Contract tests for the observed Domus40 Home Server LAN protocol."""

from __future__ import annotations

import base64
import json
import unittest
from dataclasses import replace
from pathlib import Path

from homeassistant.helpers.service_info.ssdp import SsdpServiceInfo

from custom_components.domus40 import _device_entity_unique_ids
from custom_components.domus40.api import (
    Domus40ConnectionError,
    base_url_from_host,
    base_url_from_location,
    challenge_response,
)
from custom_components.domus40.config_flow import _is_domus40
from custom_components.domus40.const import (
    identify_duration_seconds,
    monitor_unknown_messages,
)
from custom_components.domus40.coordinator import (
    _PendingLevel,
    _reconcile_pending_levels,
    _reporting_batches,
    _topic_shape,
)
from custom_components.domus40.entity import _parent_device
from custom_components.domus40.models import Domus40State, MqttInfo
from custom_components.domus40.mqtt import (
    _connect_packet,
    _pop_packet,
    _subscribe_packet,
    topic_matches_subscription,
)
from custom_components.domus40.proto import (
    constants_schema_is_compatible,
    decode_device_instant_reading_event,
    decode_device_state_event,
    instant_schema_is_compatible,
    schema_enum_map,
    schema_field_map,
    schema_is_compatible,
    schema_message_field_map,
    wire_field_signature,
)

REPOSITORY = Path(__file__).resolve().parents[2]
FIXTURES = Path(__file__).parent / "fixtures"
COMPONENT = REPOSITORY / "custom_components/domus40"


class AuthenticationTests(unittest.TestCase):
    """Pin the official client's authentication transformation."""

    def test_challenge_response(self) -> None:
        self.assertEqual(
            challenge_response("fixture-password", "fixture-challenge"),
            "234a8118fe24e015df021bae3546bac962761cf0978484cab8f90346495ce33f",
        )

    def test_ssdp_location_uses_home_server_http_port(self) -> None:
        self.assertEqual(
            base_url_from_location("http://192.0.2.10:57086/desc"),
            "http://192.0.2.10",
        )

    def test_manual_address(self) -> None:
        self.assertEqual(base_url_from_host("192.0.2.10"), "http://192.0.2.10")
        self.assertEqual(
            base_url_from_host("fixture.invalid:8080"),
            "http://fixture.invalid:8080",
        )
        self.assertEqual(base_url_from_host("[2001:db8::10]"), "http://[2001:db8::10]")

    def test_manual_address_rejects_non_origins(self) -> None:
        for address in ("", "https://192.0.2.10", "192.0.2.10/path", "user@host"):
            with (
                self.subTest(address=address),
                self.assertRaises(Domus40ConnectionError),
            ):
                base_url_from_host(address)

    def test_ssdp_match(self) -> None:
        info = SsdpServiceInfo(
            ssdp_usn="uuid:fixture::upnp:rootdevice",
            ssdp_st="upnp:rootdevice",
            ssdp_udn="uuid:fixture",
            ssdp_location="http://192.0.2.10:57086/desc",
            upnp={
                "manufacturer": "EFAPEL",
                "deviceType": "urn:schemas-upnp-org:device:HomeServer-fixture:1",
                "friendlyName": "Fixture Home Server",
            },
        )
        self.assertTrue(_is_domus40(info))


class InventoryTests(unittest.TestCase):
    """Pin tolerant conversion of sanitized inventory."""

    def test_inventory(self) -> None:
        fixture = json.loads((FIXTURES / "inventory.json").read_text())
        state = Domus40State.from_api(
            fixture["devices"], fixture["divisions"], fixture["areas"]
        )
        self.assertEqual(len(state.devices), 5)
        self.assertEqual(state.devices["101"].level, 73)
        self.assertTrue(state.devices["101"].is_on)
        self.assertEqual(state.devices["102"].division_name, "Fixture room")
        self.assertEqual(state.devices["102"].floor_name, "Fixture floor")
        self.assertEqual(state.devices["102"].ha_area_name, "Fixture room")
        self.assertFalse(state.devices["103"].is_on)
        self.assertTrue(state.devices["101"].supports_entity)
        self.assertTrue(state.devices["101"].supports_metering)
        self.assertFalse(state.devices["104"].supports_entity)
        self.assertFalse(state.devices["104"].supports_metering)
        self.assertTrue(state.devices["105"].supports_button_events)
        self.assertTrue(state.devices["104"].supports_button_events)
        self.assertTrue(state.devices["104"].supports_wall_button_events)
        self.assertTrue(state.devices["104"].supports_ir_button_events)
        self.assertTrue(state.devices["101"].supports_receiver_identify)
        self.assertTrue(state.devices["102"].supports_receiver_identify)
        self.assertFalse(state.devices["103"].supports_receiver_identify)
        self.assertFalse(state.devices["104"].supports_receiver_identify)
        self.assertEqual(state.primary_device("104").device_id, "101")
        self.assertEqual(state.physical_device_count, 4)
        first_channel = replace(
            state.devices["101"],
            device_type="OnOffCommuter2",
            endpoint="AtuadorOnOff1",
        )
        second_channel = replace(
            first_channel,
            device_id="106",
            division_id="11",
            division_name="Fixture second room",
            floor_name="Fixture second floor",
            ha_area_name="Fixture second room",
            endpoint="AtuadorOnOff2",
            name="Fixture second channel",
        )
        independent_channel = replace(
            first_channel,
            device_id="107",
            device_type="LightingRegulator",
            endpoint="ReguladorDeLuz",
            name="Fixture independent channel",
        )
        dual_channel_state = Domus40State(
            devices={
                **state.devices,
                first_channel.device_id: first_channel,
                second_channel.device_id: second_channel,
                independent_channel.device_id: independent_channel,
            }
        )
        self.assertIsNone(_parent_device(dual_channel_state, first_channel))
        self.assertEqual(
            _parent_device(dual_channel_state, second_channel), first_channel
        )
        self.assertEqual(
            _parent_device(dual_channel_state, state.devices["104"]), first_channel
        )
        self.assertEqual(second_channel.division_name, "Fixture second room")
        self.assertEqual(
            _device_entity_unique_ids("fixture-server", second_channel),
            {
                "fixture-server-106",
                "fixture-server-106-identify",
                "fixture-server-106-identify-associated",
                "fixture-server-106-power",
            },
        )
        self.assertEqual(len(dual_channel_state.metering_devices), 5)
        batches = _reporting_batches(dual_channel_state.metering_devices, batch_size=2)
        self.assertEqual(
            {device_id for batch in batches for device_id in batch},
            {"101", "102", "103", "106", "107"},
        )
        for batch in batches:
            addresses = {
                dual_channel_state.devices[device_id].hardware_address
                for device_id in batch
            }
            self.assertEqual(len(addresses), len(batch))
        self.assertEqual(
            sum(device.supports_entity for device in state.devices.values()), 3
        )

    def test_only_duplicate_division_names_are_disambiguated(self) -> None:
        fixture = json.loads((FIXTURES / "inventory.json").read_text())
        devices = [
            {**fixture["devices"][0], "id": 201, "division": 10},
            {**fixture["devices"][1], "id": 202, "division": 11},
            {**fixture["devices"][2], "id": 203, "division": 12},
        ]
        state = Domus40State.from_api(
            devices,
            [
                {"id": 10, "name": "Fixture hall", "area": 1},
                {"id": 11, "name": "Fixture hall", "area": 2},
                {"id": 12, "name": "Fixture studio", "area": 1},
            ],
            [
                {"id": 1, "name": "Fixture lower floor"},
                {"id": 2, "name": "Fixture upper floor"},
            ],
        )

        self.assertEqual(
            state.devices["201"].ha_area_name,
            "Fixture lower floor · Fixture hall",
        )
        self.assertEqual(
            state.devices["202"].ha_area_name,
            "Fixture upper floor · Fixture hall",
        )
        self.assertEqual(state.devices["203"].ha_area_name, "Fixture studio")

    def test_identification_duration(self) -> None:
        self.assertEqual(identify_duration_seconds({}), 30)
        self.assertEqual(
            identify_duration_seconds({"identify_duration_seconds": "7"}), 7
        )
        self.assertEqual(identify_duration_seconds({"identify_duration_seconds": 0}), 1)
        self.assertEqual(
            identify_duration_seconds({"identify_duration_seconds": 99}), 30
        )
        self.assertEqual(
            identify_duration_seconds({"identify_duration_seconds": "invalid"}),
            30,
        )
        self.assertFalse(monitor_unknown_messages({}))
        self.assertTrue(monitor_unknown_messages({"monitor_unknown_messages": True}))
        self.assertTrue(monitor_unknown_messages({"monitor_unknown_messages": "yes"}))


class ProtobufTests(unittest.TestCase):
    """Pin the baked private state and instantaneous-reading mappings."""

    # info={}, deviceId=101, endpoint=1, buttonState=0,
    # energyChangeState=2, energyLevel=73, timeActive=5
    EVENT = (
        b"\x0a\x00\x10\x65\xa0\x06\x01\xa8\x06\x00\xc0\x0c\x02\xc8\x0c\x49\xe8\x12\x05"
    )
    # info={}, deviceId=101, current=215mA, factor=98, consumed=12345mW,
    # voltage=230V, temperature=21.5, externalTemperature=19.25, luminance=640.
    INSTANT_EVENT = (
        b"\x0a\x00\x10\x65\x50\xd7\x01\x58\x62\x60\xb9\x60\x68\xe6\x01"
        b"\xa1\x01\x00\x00\x00\x00\x00\x80\x35\x40"
        b"\xa9\x01\x00\x00\x00\x00\x00\x40\x33\x40\xb0\x01\x80\x05"
    )

    def test_binary_event(self) -> None:
        event = decode_device_state_event(self.EVENT)
        self.assertEqual(event.device_id, "101")
        self.assertEqual(event.endpoint, 1)
        self.assertEqual(event.energy_level, 73)
        self.assertEqual(event.time_active, 5)

    def test_base64_event(self) -> None:
        event = decode_device_state_event(base64.b64encode(self.EVENT))
        self.assertEqual(event.device_id, "101")
        self.assertEqual(event.energy_change_state, 2)

    def test_button_event(self) -> None:
        # info={}, deviceId=105, endpoint=TeclaA(9), buttonState=Pressed(0)
        event = decode_device_state_event(b"\x0a\x00\x10\x69\xa0\x06\x09\xa8\x06\x00")
        self.assertEqual(event.device_id, "105")
        self.assertEqual(event.endpoint, 9)
        self.assertEqual(event.button_state, 0)
        self.assertIsNone(event.energy_level)

    def test_instant_power_event(self) -> None:
        event = decode_device_instant_reading_event(self.INSTANT_EVENT)
        self.assertEqual(event.device_id, "101")
        self.assertEqual(event.power_measured_ma, 215)
        self.assertEqual(event.power_factor, 98)
        self.assertEqual(event.consumed_mw, 12345)
        self.assertEqual(event.power_w, 12.345)
        self.assertEqual(event.voltage_v, 230)
        self.assertEqual(event.temperature, 21.5)
        self.assertEqual(event.external_temperature, 19.25)
        self.assertEqual(event.luminance, 640)
        self.assertEqual(
            decode_device_instant_reading_event(
                base64.b64encode(self.INSTANT_EVENT)
            ).power_w,
            12.345,
        )

    def test_wire_signature_contains_no_values(self) -> None:
        self.assertEqual(
            wire_field_signature(self.INSTANT_EVENT),
            (
                (1, 2),
                (2, 0),
                (10, 0),
                (11, 0),
                (12, 0),
                (13, 0),
                (20, 1),
                (21, 1),
                (22, 0),
            ),
        )

    def test_baked_schema(self) -> None:
        schema = (COMPONENT / "events.proto").read_text()
        self.assertTrue(schema_is_compatible(schema))
        self.assertTrue(instant_schema_is_compatible(schema))
        self.assertEqual(schema_field_map(schema)["endpoint"]["number"], 100)
        self.assertEqual(schema_field_map(schema)["energyLevel"]["number"], 201)
        self.assertEqual(
            schema_message_field_map(schema, "DeviceInstantReadingEvent")[
                "consumed_mW"
            ]["number"],
            12,
        )
        self.assertFalse(
            schema_is_compatible(schema.replace("energyLevel = 201", "energyLevel = 8"))
        )
        self.assertFalse(
            instant_schema_is_compatible(
                schema.replace("consumed_mW = 12", "consumed_mW = 120")
            )
        )

    def test_baked_button_enums(self) -> None:
        schema = (COMPONENT / "constants.proto").read_text()
        self.assertTrue(constants_schema_is_compatible(schema))
        self.assertEqual(schema_enum_map(schema, "DeviceEndpoint")["TeclaA"], 9)
        self.assertEqual(schema_enum_map(schema, "DeviceEndpoint")["TeclaD"], 12)
        self.assertEqual(schema_enum_map(schema, "DeviceEndpoint")["TeclaIR1Up"], 21)
        self.assertEqual(schema_enum_map(schema, "DeviceEndpoint")["TeclaIR4Down"], 28)
        self.assertEqual(schema_enum_map(schema, "ButtonState")["Pressed"], 0)
        self.assertFalse(
            constants_schema_is_compatible(schema.replace("TeclaA = 9", "TeclaA = 90"))
        )


class PendingWriteTests(unittest.TestCase):
    """Keep optimistic state stable while the Home Server REST view lags."""

    def setUp(self) -> None:
        fixture = json.loads((FIXTURES / "inventory.json").read_text())
        self.state = Domus40State.from_api(
            fixture["devices"], fixture["divisions"], fixture["areas"]
        )

    def test_stale_level_is_masked_before_deadline(self) -> None:
        pending = {"101": _PendingLevel(level=20, expires_at=20)}
        reconciled = _reconcile_pending_levels(self.state, pending, now=10)
        self.assertEqual(reconciled.devices["101"].level, 20)
        self.assertIs(reconciled.locations, self.state.locations)
        self.assertIn("101", pending)

    def test_matching_level_clears_pending_write(self) -> None:
        pending = {"101": _PendingLevel(level=73, expires_at=20)}
        reconciled = _reconcile_pending_levels(self.state, pending, now=10)
        self.assertIs(reconciled, self.state)
        self.assertNotIn("101", pending)

    def test_expired_write_accepts_authoritative_state(self) -> None:
        pending = {"101": _PendingLevel(level=20, expires_at=10)}
        reconciled = _reconcile_pending_levels(self.state, pending, now=10)
        self.assertIs(reconciled, self.state)
        self.assertNotIn("101", pending)

    def test_on_off_confirmation_uses_switch_state(self) -> None:
        devices = dict(self.state.devices)
        devices["103"] = replace(devices["103"], level=37, is_on=False)
        state = Domus40State(devices)
        pending = {"103": _PendingLevel(level=0, expires_at=20)}
        reconciled = _reconcile_pending_levels(state, pending, now=10)
        self.assertIs(reconciled, state)
        self.assertNotIn("103", pending)


class MqttTests(unittest.TestCase):
    """Pin the dependency-free MQTT packet framing."""

    def test_connect_and_subscribe_packets(self) -> None:
        connect = _connect_packet(MqttInfo("fixture-user", "fixture-password"))
        parsed = _pop_packet(connect)
        self.assertIsNotNone(parsed)
        assert parsed is not None
        self.assertEqual(parsed[0], 1)
        self.assertIn(b"MQTT", parsed[2])
        # MQTT 3.1.1 credentials belong in the CONNECT payload; production
        # diagnostics and logs never include this packet.
        self.assertIn(b"fixture-user", parsed[2])
        self.assertIn(b"fixture-password", parsed[2])

        subscribe = _subscribe_packet("events/device/+/state/changed")
        parsed = _pop_packet(subscribe)
        self.assertIsNotNone(parsed)
        assert parsed is not None
        self.assertEqual(parsed[0], 8)

    def test_topic_matching_and_redaction(self) -> None:
        self.assertTrue(
            topic_matches_subscription(
                "fixture/events/device/+/reading/instant",
                "fixture/events/device/101/reading/instant",
            )
        )
        self.assertTrue(
            topic_matches_subscription("fixture/#", "fixture/events/new/value")
        )
        self.assertFalse(
            topic_matches_subscription(
                "fixture/events/device/+/state/changed",
                "fixture/events/device/101/reading/instant",
            )
        )
        self.assertEqual(
            _topic_shape("fixture/events/device/101/new-kind", "fixture/"),
            "events/device/{value}/{value}",
        )


if __name__ == "__main__":
    unittest.main()
