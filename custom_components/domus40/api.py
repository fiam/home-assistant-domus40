"""Async client for the private EFAPEL Domus40 Home Server API."""

from __future__ import annotations

import asyncio
import hashlib
from typing import Any
from urllib.parse import urljoin, urlsplit

import aiohttp

from .const import API_PREFIX, DEFAULT_REQUEST_TIMEOUT
from .models import (
    Domus40ButtonBinding,
    Domus40ScenarioAction,
    Domus40State,
    MqttInfo,
)


class Domus40Error(Exception):
    """Base exception for Domus40 communication failures."""


class Domus40ConnectionError(Domus40Error):
    """The Home Server could not be reached or returned invalid data."""


class Domus40AuthError(Domus40Error):
    """The Home Server rejected the credentials or session."""


def base_url_from_location(location: str) -> str:
    """Return the HTTP origin from an SSDP description location."""
    parsed = urlsplit(location)
    if parsed.scheme != "http" or not parsed.hostname:
        raise Domus40ConnectionError("SSDP location is not a local HTTP URL")
    # SSDP advertises a dedicated UPnP description port. The shipped client
    # intentionally keeps only the discovered address and uses HTTP port 80.
    return f"http://{parsed.hostname}"


def base_url_from_host(host: str) -> str:
    """Return a validated HTTP origin from a manually entered LAN address."""
    candidate = host.strip()
    if not candidate:
        raise Domus40ConnectionError("Home Server address is empty")
    if "://" not in candidate:
        candidate = f"http://{candidate}"
    parsed = urlsplit(candidate)
    try:
        port = parsed.port
    except ValueError as err:
        raise Domus40ConnectionError("Home Server address has an invalid port") from err
    if (
        parsed.scheme != "http"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        raise Domus40ConnectionError("Home Server address is not a valid HTTP origin")
    hostname = parsed.hostname
    if ":" in hostname:
        hostname = f"[{hostname}]"
    port_suffix = f":{port}" if port not in {None, 80} else ""
    return f"http://{hostname}{port_suffix}"


def challenge_response(password: str, challenge: str) -> str:
    """Reproduce the two-stage SHA-256 challenge used by the official client."""
    first = hashlib.sha256(password.encode()).hexdigest()
    second = hashlib.sha256(first.encode()).hexdigest()
    return hashlib.sha256(f"{challenge}{second}".encode()).hexdigest()


class Domus40Client:
    """Client for one Home Server and one isolated cookie jar."""

    def __init__(
        self,
        base_url: str,
        username: str,
        password: str,
        session: aiohttp.ClientSession,
    ) -> None:
        """Initialize the API client."""
        self.base_url = base_url.rstrip("/")
        self._username = username
        self._password = password
        self._session = session
        self._csrf_token: str | None = None
        self._authenticated = False
        self._auth_lock = asyncio.Lock()
        self.mqtt_info: MqttInfo | None = None

    def _url(self, endpoint: str) -> str:
        return urljoin(f"{self.base_url}{API_PREFIX}/", endpoint.lstrip("/"))

    async def _raw_json(
        self,
        method: str,
        endpoint: str,
        *,
        json: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
    ) -> tuple[int, Any]:
        try:
            async with asyncio.timeout(DEFAULT_REQUEST_TIMEOUT):
                async with self._session.request(
                    method,
                    self._url(endpoint),
                    json=json,
                    params=params,
                ) as response:
                    if token := response.headers.get("X-AntiCSRF"):
                        self._csrf_token = token
                    try:
                        payload = await response.json(content_type=None)
                    except aiohttp.ContentTypeError, ValueError:
                        payload = None
                    return response.status, payload
        except (TimeoutError, aiohttp.ClientError) as err:
            raise Domus40ConnectionError("Home Server request failed") from err

    async def _prime_csrf(self) -> None:
        status, _ = await self._raw_json(
            "GET", "/users/valid", params={"_": "home-assistant"}
        )
        if status not in {200, 401, 403}:
            raise Domus40ConnectionError(
                f"Home Server CSRF request returned status {status}"
            )
        if not self._csrf_token:
            raise Domus40ConnectionError("Home Server did not issue a CSRF token")

    async def async_authenticate(self, *, force: bool = False) -> None:
        """Authenticate and retain only the returned session and MQTT fields."""
        async with self._auth_lock:
            if self._authenticated and not force:
                return
            await self._prime_csrf()
            status, payload = await self._raw_json(
                "POST",
                "/authentication",
                json={"username": self._username, "csrftoken": self._csrf_token},
            )

            if status == 200:
                self._finish_authentication(payload)
                return
            if status != 401 or not isinstance(payload, dict):
                if status == 403:
                    raise Domus40AuthError("Home Server rejected the credentials")
                raise Domus40ConnectionError(
                    f"Home Server challenge request returned status {status}"
                )

            challenge = payload.get("challenge")
            if not isinstance(challenge, str) or not challenge:
                raise Domus40ConnectionError(
                    "Home Server returned an invalid challenge"
                )

            status, payload = await self._raw_json(
                "POST",
                "/authentication",
                json={
                    "username": self._username,
                    "challengeResponse": challenge_response(self._password, challenge),
                    "csrftoken": self._csrf_token,
                },
            )
            if status == 403:
                raise Domus40AuthError("Home Server rejected the credentials")
            if status != 200:
                raise Domus40ConnectionError(
                    f"Home Server authentication returned status {status}"
                )
            self._finish_authentication(payload)

    def _finish_authentication(self, payload: Any) -> None:
        self._authenticated = True
        if isinstance(payload, dict):
            self.mqtt_info = MqttInfo.from_api(payload.get("mqttInfo"))

    async def _authorized_json(
        self,
        method: str,
        endpoint: str,
        *,
        json: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
        retry_auth: bool = True,
    ) -> Any:
        await self.async_authenticate()
        status, payload = await self._raw_json(
            method, endpoint, json=json, params=params
        )
        if status == 403 and retry_auth:
            self._authenticated = False
            await self.async_authenticate(force=True)
            return await self._authorized_json(
                method,
                endpoint,
                json=json,
                params=params,
                retry_auth=False,
            )
        if status == 403:
            raise Domus40AuthError("Home Server session was rejected")
        if status < 200 or status >= 300:
            raise Domus40ConnectionError(
                f"Home Server API request returned status {status}"
            )
        return payload

    async def async_get_state(self) -> Domus40State:
        """Fetch all device and room state."""
        raw_devices, raw_divisions, raw_areas = await asyncio.gather(
            self._authorized_json(
                "GET",
                "/devices/getDevices",
                params={"orderBy": "DEV_SET_DISPLAY_NAME", "order": "AscNoCase"},
            ),
            self._authorized_json("GET", "/divisions/getDivisions"),
            self._authorized_json("GET", "/areas/getAreas"),
        )
        if (
            not isinstance(raw_devices, list)
            or not isinstance(raw_divisions, list)
            or not isinstance(raw_areas, list)
        ):
            raise Domus40ConnectionError("Home Server returned an invalid inventory")
        return Domus40State.from_api(raw_devices, raw_divisions, raw_areas)

    async def async_set_level(self, device_id: str, level: int) -> None:
        """Set an actuator level from zero through one hundred."""
        await self._authorized_json(
            "PUT",
            f"/devices/{device_id}/state",
            json={"level": max(0, min(100, level))},
        )

    async def async_identify_device(self, device_id: str, duration: int = 30) -> None:
        """Activate the device's identification LED for a bounded duration."""
        await self._authorized_json(
            "PUT",
            f"/devices/{device_id}/led",
            json={"duration": max(1, min(30, duration))},
        )

    async def async_activate_reporting(self, device_id: str) -> str:
        """Enable one device's short-lived instantaneous reporting stream."""
        payload = await self._authorized_json(
            "PUT", f"/devices/{device_id}/reporting", json={}
        )
        topic = payload.get("topic") if isinstance(payload, dict) else None
        if not isinstance(topic, str) or not topic:
            raise Domus40ConnectionError(
                "Home Server returned an invalid reporting topic"
            )
        return topic

    async def async_deactivate_reporting(self, device_id: str) -> None:
        """Disable one device's instantaneous reporting stream."""
        await self._authorized_json("DELETE", f"/devices/{device_id}/reporting")

    async def async_get_proto_schema(self, filename: str) -> str | None:
        """Read one authenticated private protobuf schema."""
        if filename not in {"constants.proto", "common_header.proto", "events.proto"}:
            raise ValueError("Unsupported Domus40 protobuf schema")
        await self.async_authenticate()
        try:
            async with asyncio.timeout(DEFAULT_REQUEST_TIMEOUT):
                async with self._session.get(
                    self._url(f"/static/protos/{filename}")
                ) as response:
                    if token := response.headers.get("X-AntiCSRF"):
                        self._csrf_token = token
                    if response.status == 403:
                        self._authenticated = False
                        return None
                    if response.status != 200:
                        return None
                    return await response.text()
        except TimeoutError, aiohttp.ClientError:
            return None

    async def async_get_events_schema(self) -> str | None:
        """Read the authenticated private event schema."""
        return await self.async_get_proto_schema("events.proto")

    async def async_get_button_binding(
        self,
        device_id: str,
        endpoint: str,
        state: Domus40State,
    ) -> Domus40ButtonBinding:
        """Read the scenario assigned to one button endpoint."""
        payload = await self._authorized_json(
            "GET",
            "/devicescenarios/",
            params={"deviceId": device_id, "deviceEndpoint": endpoint},
        )
        if not isinstance(payload, dict):
            raise Domus40ConnectionError(
                "Home Server returned an invalid button assignment"
            )
        device_scenarios = payload.get("deviceScenarios")
        if not isinstance(device_scenarios, list):
            raise Domus40ConnectionError(
                "Home Server returned an invalid button assignment"
            )
        if not device_scenarios:
            return Domus40ButtonBinding(device_id, endpoint)

        first = device_scenarios[0]
        scenario = first.get("scenario") if isinstance(first, dict) else None
        scenario_id = scenario.get("id") if isinstance(scenario, dict) else None
        if scenario_id is None:
            raise Domus40ConnectionError(
                "Home Server button assignment has no scenario"
            )

        full = await self._authorized_json("GET", f"/scenarios/full/{scenario_id}")
        if not isinstance(full, dict):
            raise Domus40ConnectionError(
                "Home Server returned an invalid button scenario"
            )
        actions_container = full.get("actions")
        raw_actions = (
            actions_container.get("actions")
            if isinstance(actions_container, dict)
            else None
        )
        if not isinstance(raw_actions, list):
            raise Domus40ConnectionError(
                "Home Server returned an invalid button scenario"
            )
        actions = tuple(
            action
            for item in raw_actions
            if isinstance(item, dict)
            and (action := Domus40ScenarioAction.from_api(item, state.devices))
            is not None
        )
        return Domus40ButtonBinding(device_id, endpoint, actions)

    async def async_get_button_bindings(
        self,
        device_id: str,
        endpoints: tuple[str, ...],
        state: Domus40State,
    ) -> dict[str, Domus40ButtonBinding]:
        """Read several endpoint assignments with one device index request."""
        payload = await self._authorized_json(
            "GET", "/devicescenarios/", params={"deviceId": device_id}
        )
        if not isinstance(payload, dict):
            raise Domus40ConnectionError(
                "Home Server returned an invalid button assignment index"
            )
        device_scenarios = payload.get("deviceScenarios")
        if not isinstance(device_scenarios, list):
            raise Domus40ConnectionError(
                "Home Server returned an invalid button assignment index"
            )

        scenario_ids: dict[str, Any] = {}
        for item in device_scenarios:
            if not isinstance(item, dict):
                continue
            endpoint = item.get("endpoint")
            scenario = item.get("scenario")
            scenario_id = scenario.get("id") if isinstance(scenario, dict) else None
            if isinstance(endpoint, str) and scenario_id is not None:
                scenario_ids[endpoint] = scenario_id

        bindings: dict[str, Domus40ButtonBinding] = {}
        for endpoint in endpoints:
            scenario_id = scenario_ids.get(endpoint)
            if scenario_id is None:
                bindings[endpoint] = Domus40ButtonBinding(device_id, endpoint)
                continue
            full = await self._authorized_json("GET", f"/scenarios/full/{scenario_id}")
            if not isinstance(full, dict):
                raise Domus40ConnectionError(
                    "Home Server returned an invalid button scenario"
                )
            actions_container = full.get("actions")
            raw_actions = (
                actions_container.get("actions")
                if isinstance(actions_container, dict)
                else None
            )
            if not isinstance(raw_actions, list):
                raise Domus40ConnectionError(
                    "Home Server returned an invalid button scenario"
                )
            actions = tuple(
                action
                for item in raw_actions
                if isinstance(item, dict)
                and (action := Domus40ScenarioAction.from_api(item, state.devices))
                is not None
            )
            bindings[endpoint] = Domus40ButtonBinding(device_id, endpoint, actions)
        return bindings

    async def async_logout(self) -> None:
        """Best-effort logout; the Home Server also expires the session cookie."""
        if not self._authenticated:
            return
        try:
            await self._raw_json("DELETE", "/authentication")
        except Domus40Error:
            pass
        self._authenticated = False
