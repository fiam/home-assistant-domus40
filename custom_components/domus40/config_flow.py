"""Config flow for EFAPEL Domus40."""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any, override

import aiohttp
import voluptuous as vol
from homeassistant.components import ssdp
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.const import CONF_HOST, CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import callback
from homeassistant.helpers.aiohttp_client import async_create_clientsession
from homeassistant.helpers.service_info.ssdp import SsdpServiceInfo

from .api import (
    Domus40AuthError,
    Domus40Client,
    Domus40ConnectionError,
    base_url_from_host,
    base_url_from_location,
)
from .const import (
    CONF_BASE_URL,
    CONF_IDENTIFY_DURATION_SECONDS,
    CONF_MONITOR_UNKNOWN_MESSAGES,
    CONF_SERVER_UDN,
    DISCOVERY_ST,
    DOMAIN,
    MANUFACTURER,
    MAX_IDENTIFY_DURATION_SECONDS,
    MIN_IDENTIFY_DURATION_SECONDS,
    identify_duration_seconds,
    monitor_unknown_messages,
)

_LOGGER = logging.getLogger(__name__)

CREDENTIAL_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_USERNAME): str,
        vol.Required(CONF_PASSWORD): str,
    }
)

MANUAL_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_HOST): str,
        vol.Required(CONF_USERNAME): str,
        vol.Required(CONF_PASSWORD): str,
    }
)


def _is_domus40(info: SsdpServiceInfo) -> bool:
    manufacturer = info.upnp.get("manufacturer")
    device_type = info.upnp.get("deviceType")
    return (
        isinstance(manufacturer, str)
        and manufacturer.casefold() == MANUFACTURER.casefold()
        and isinstance(device_type, str)
        and device_type.startswith("urn:schemas-upnp-org:device:HomeServer-")
    )


def _unique_id(info: SsdpServiceInfo) -> str:
    candidate = info.ssdp_udn or info.upnp.get("UDN") or info.ssdp_usn.split("::", 1)[0]
    return str(candidate).strip().lower()


def _title(info: SsdpServiceInfo) -> str:
    name = info.upnp.get("friendlyName")
    if isinstance(name, str) and name.strip():
        return name.strip()
    return "EFAPEL Domus40"


async def _validate(
    flow: ConfigFlow, base_url: str, user_input: dict[str, Any]
) -> None:
    session = async_create_clientsession(
        flow.hass,
        auto_cleanup=False,
        cookie_jar=aiohttp.CookieJar(unsafe=True),
    )
    client = Domus40Client(
        base_url,
        user_input[CONF_USERNAME],
        user_input[CONF_PASSWORD],
        session,
    )
    try:
        await client.async_authenticate()
        await client.async_get_state()
        await client.async_logout()
    finally:
        await session.close()


class Domus40ConfigFlow(ConfigFlow, domain=DOMAIN):
    """Discover and authenticate a Domus40 Home Server."""

    VERSION = 1

    def __init__(self) -> None:
        """Initialize flow state."""
        self._discoveries: dict[str, SsdpServiceInfo] = {}
        self._selected: SsdpServiceInfo | None = None

    @staticmethod
    @callback
    @override
    def async_get_options_flow(config_entry: ConfigEntry) -> Domus40OptionsFlow:
        """Return the integration options flow."""
        return Domus40OptionsFlow()

    async def _async_discover(self) -> None:
        discoveries = await ssdp.async_get_discovery_info_by_st(self.hass, DISCOVERY_ST)
        self._discoveries = {
            _unique_id(info): info for info in discoveries if _is_domus40(info)
        }

    async def _async_finish(
        self, info: SsdpServiceInfo, user_input: dict[str, Any], step_id: str
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        try:
            if not info.ssdp_location:
                raise Domus40ConnectionError("Discovery has no description location")
            base_url = base_url_from_location(info.ssdp_location)
            await _validate(self, base_url, user_input)
        except Domus40AuthError:
            errors["base"] = "invalid_auth"
        except Domus40ConnectionError:
            errors["base"] = "cannot_connect"
        except Exception:
            _LOGGER.exception("Unexpected Domus40 setup error")
            errors["base"] = "unknown"

        if errors:
            return self.async_show_form(
                step_id=step_id,
                data_schema=self.add_suggested_values_to_schema(
                    CREDENTIAL_SCHEMA,
                    {CONF_USERNAME: user_input.get(CONF_USERNAME, "")},
                ),
                errors=errors,
            )

        unique_id = _unique_id(info)
        await self.async_set_unique_id(unique_id)
        self._abort_if_unique_id_configured()
        if not info.ssdp_location:
            return self.async_abort(reason="cannot_connect")
        return self.async_create_entry(
            title=_title(info),
            data={
                CONF_BASE_URL: base_url,
                CONF_SERVER_UDN: unique_id,
                CONF_USERNAME: user_input[CONF_USERNAME],
                CONF_PASSWORD: user_input[CONF_PASSWORD],
            },
        )

    @override
    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Use discovery when available and otherwise offer manual setup."""
        if not self._discoveries:
            await self._async_discover()
        if not self._discoveries:
            return await self.async_step_manual(user_input)

        if len(self._discoveries) == 1:
            self._selected = next(iter(self._discoveries.values()))
        if user_input is not None:
            selected = self._selected
            if selected is None:
                selected = self._discoveries[user_input["server"]]
                self._selected = selected
                user_input = dict(user_input)
                user_input.pop("server")
            return await self._async_finish(selected, user_input, "user")

        schema = CREDENTIAL_SCHEMA
        if self._selected is None:
            options = {
                unique_id: _title(info) for unique_id, info in self._discoveries.items()
            }
            schema = vol.Schema(
                {
                    vol.Required("server"): vol.In(options),
                    vol.Required(CONF_USERNAME): str,
                    vol.Required(CONF_PASSWORD): str,
                }
            )
        return self.async_show_form(step_id="user", data_schema=schema)

    async def async_step_manual(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Connect by address when multicast discovery is unavailable."""
        errors: dict[str, str] = {}
        if user_input is not None:
            try:
                base_url = base_url_from_host(user_input[CONF_HOST])
            except Domus40ConnectionError:
                errors[CONF_HOST] = "invalid_host"
            else:
                try:
                    await _validate(self, base_url, user_input)
                except Domus40AuthError:
                    errors["base"] = "invalid_auth"
                except Domus40ConnectionError:
                    errors["base"] = "cannot_connect"
                except Exception:
                    _LOGGER.exception("Unexpected Domus40 manual setup error")
                    errors["base"] = "unknown"
                if not errors:
                    await self.async_set_unique_id(f"manual:{base_url.casefold()}")
                    self._abort_if_unique_id_configured()
                    for entry in self._async_current_entries():
                        if entry.data.get(CONF_BASE_URL) == base_url:
                            return self.async_abort(reason="already_configured")
                    return self.async_create_entry(
                        title="EFAPEL Domus40",
                        data={
                            CONF_BASE_URL: base_url,
                            CONF_SERVER_UDN: f"manual:{base_url.casefold()}",
                            CONF_USERNAME: user_input[CONF_USERNAME],
                            CONF_PASSWORD: user_input[CONF_PASSWORD],
                        },
                    )
        return self.async_show_form(
            step_id="manual",
            data_schema=self.add_suggested_values_to_schema(
                MANUAL_SCHEMA,
                {
                    CONF_HOST: (user_input or {}).get(CONF_HOST, ""),
                    CONF_USERNAME: (user_input or {}).get(CONF_USERNAME, ""),
                },
            ),
            errors=errors,
        )

    @override
    async def async_step_ssdp(
        self, discovery_info: SsdpServiceInfo
    ) -> ConfigFlowResult:
        """Handle a Home Assistant SSDP discovery."""
        if not _is_domus40(discovery_info) or not discovery_info.ssdp_location:
            return self.async_abort(reason="not_domus40")
        unique_id = _unique_id(discovery_info)
        await self.async_set_unique_id(unique_id)
        self._abort_if_unique_id_configured(
            updates={
                CONF_BASE_URL: base_url_from_location(discovery_info.ssdp_location)
            }
        )
        self._selected = discovery_info
        self.context["title_placeholders"] = {"name": _title(discovery_info)}
        return await self.async_step_confirm()

    async def async_step_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Ask for existing Home Server administrator credentials."""
        if self._selected is None:
            return self.async_abort(reason="cannot_connect")
        if user_input is not None:
            return await self._async_finish(self._selected, user_input, "confirm")
        return self.async_show_form(
            step_id="confirm",
            data_schema=CREDENTIAL_SCHEMA,
            description_placeholders={"name": _title(self._selected)},
        )

    @override
    async def async_step_reauth(
        self, entry_data: Mapping[str, Any]
    ) -> ConfigFlowResult:
        """Start reauthentication."""
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Update invalid Home Server credentials."""
        entry = self._get_reauth_entry()
        errors: dict[str, str] = {}
        if user_input is not None:
            session = async_create_clientsession(
                self.hass,
                auto_cleanup=False,
                cookie_jar=aiohttp.CookieJar(unsafe=True),
            )
            client = Domus40Client(
                entry.data[CONF_BASE_URL],
                user_input[CONF_USERNAME],
                user_input[CONF_PASSWORD],
                session,
            )
            try:
                await client.async_authenticate()
            except Domus40AuthError:
                errors["base"] = "invalid_auth"
            except Domus40ConnectionError:
                errors["base"] = "cannot_connect"
            finally:
                await session.close()
            if not errors:
                return self.async_update_reload_and_abort(
                    entry,
                    data_updates={
                        CONF_USERNAME: user_input[CONF_USERNAME],
                        CONF_PASSWORD: user_input[CONF_PASSWORD],
                    },
                )
        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=CREDENTIAL_SCHEMA,
            errors=errors,
        )


class Domus40OptionsFlow(OptionsFlow):
    """Configure non-credential Domus40 behavior."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Configure the duration of identification LED requests."""
        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)

        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_IDENTIFY_DURATION_SECONDS,
                        default=identify_duration_seconds(self.config_entry.options),
                    ): vol.All(
                        vol.Coerce(int),
                        vol.Range(
                            min=MIN_IDENTIFY_DURATION_SECONDS,
                            max=MAX_IDENTIFY_DURATION_SECONDS,
                        ),
                    ),
                    vol.Required(
                        CONF_MONITOR_UNKNOWN_MESSAGES,
                        default=monitor_unknown_messages(self.config_entry.options),
                    ): bool,
                }
            ),
        )
