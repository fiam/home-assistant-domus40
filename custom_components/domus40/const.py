"""Constants for the EFAPEL Domus40 integration."""

from collections.abc import Mapping
from datetime import timedelta
from typing import Any

DOMAIN = "domus40"

CONF_BASE_URL = "base_url"
CONF_IDENTIFY_DURATION_SECONDS = "identify_duration_seconds"
CONF_MONITOR_UNKNOWN_MESSAGES = "monitor_unknown_messages"
CONF_SERVER_UDN = "server_udn"

API_PREFIX = "/HsAPI"
DEFAULT_POLL_INTERVAL = timedelta(seconds=30)
DEFAULT_REQUEST_TIMEOUT = 15
DISCOVERY_ST = "upnp:rootdevice"
MANUFACTURER = "EFAPEL"
MODEL = "Domus40 Home Server"

PLATFORMS = ["button", "cover", "event", "light", "sensor", "switch"]

IDENTIFY_DURATION_SECONDS = 30
MIN_IDENTIFY_DURATION_SECONDS = 1
MAX_IDENTIFY_DURATION_SECONDS = 30
DEFAULT_MONITOR_UNKNOWN_MESSAGES = False
REFRESH_INVENTORY_UNIQUE_ID_SUFFIX = "refresh-inventory"


def identify_duration_seconds(options: Mapping[str, Any]) -> int:
    """Return the validated identification duration from config-entry options."""
    try:
        duration = int(
            options.get(CONF_IDENTIFY_DURATION_SECONDS, IDENTIFY_DURATION_SECONDS)
        )
    except TypeError, ValueError:
        duration = IDENTIFY_DURATION_SECONDS
    return max(
        MIN_IDENTIFY_DURATION_SECONDS,
        min(MAX_IDENTIFY_DURATION_SECONDS, duration),
    )


def monitor_unknown_messages(options: Mapping[str, Any]) -> bool:
    """Return whether redacted unknown MQTT diagnostics are enabled."""
    value = options.get(CONF_MONITOR_UNKNOWN_MESSAGES, DEFAULT_MONITOR_UNKNOWN_MESSAGES)
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


TYPE_BLINDS = "BlindsControl"
TYPE_DIMMER = "LightingRegulator"
TYPE_LIGHTS = frozenset({TYPE_DIMMER, "OnOffCommuter", "OnOffCommuter2"})
TYPE_PLUGS = frozenset({"Plug", "PlugSocket", "PlugWall"})
TYPE_QUAD_BUTTON = "QuadPressureButton"
HEADER_SWITCHES = "Switches"

WALL_BUTTON_ENDPOINTS = {
    "TeclaA": 9,
    "TeclaB": 10,
    "TeclaC": 11,
    "TeclaD": 12,
}
IR_BUTTON_ENDPOINTS = {
    "TeclaIRA": 13,
    "TeclaIRB": 14,
    "TeclaIRC": 15,
    "TeclaIRD": 16,
    "TeclaIRE": 17,
    "TeclaIRF": 18,
    "TeclaIRG": 19,
    "TeclaIRH": 20,
    "TeclaIR1Up": 21,
    "TeclaIR1Down": 22,
    "TeclaIR2Up": 23,
    "TeclaIR2Down": 24,
    "TeclaIR3Up": 25,
    "TeclaIR3Down": 26,
    "TeclaIR4Up": 27,
    "TeclaIR4Down": 28,
}
BUTTON_ENDPOINTS = WALL_BUTTON_ENDPOINTS | IR_BUTTON_ENDPOINTS
IR_ENDPOINT_LABELS = {
    "TeclaIRA": "A",
    "TeclaIRB": "B",
    "TeclaIRC": "C",
    "TeclaIRD": "D",
    "TeclaIRE": "E",
    "TeclaIRF": "F",
    "TeclaIRG": "G",
    "TeclaIRH": "H",
    "TeclaIR1Up": "1 up",
    "TeclaIR1Down": "1 down",
    "TeclaIR2Up": "2 up",
    "TeclaIR2Down": "2 down",
    "TeclaIR3Up": "3 up",
    "TeclaIR3Down": "3 down",
    "TeclaIR4Up": "4 up",
    "TeclaIR4Down": "4 down",
}
BUTTON_STATES = {
    0: "pressed",
    1: "released",
}

MQTT_PORT = 9998
MQTT_PATH = "/"
MQTT_STATE_TOPIC = "events/device/+/state/changed"
MQTT_METERING_TOPIC = "events/device/+/reading/instant"
MQTT_MONITOR_TOPIC = "#"
METERING_SAMPLE_SECONDS = 10

PROTO_SCHEMA_REVISION = "domus40-events-v4"
