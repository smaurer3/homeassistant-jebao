"""Config flow for Jebao integration."""
from __future__ import annotations

import logging
from typing import Any

import netifaces
import voluptuous as vol
from jebao import JebaoError, MDP20000Device, discover_devices

from homeassistant import config_entries
from homeassistant.const import CONF_HOST
from homeassistant.core import HomeAssistant, callback
from homeassistant.data_entry_flow import FlowResult
from homeassistant.helpers import selector
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.service_info.dhcp import DhcpServiceInfo

from .const import (
    CONF_DEVICE_ID,
    CONF_DID,
    CONF_INTERFACES,
    CONF_MODEL,
    CONF_PASSWORD,
    CONF_REGION,
    CONF_USERNAME,
    DEFAULT_NAME,
    DOMAIN,
    GIZWITS_REGIONS,
    MODEL_MD44,
    MODEL_MDP20000,
)
from .gizwits_cloud import GizwitsAuthError, GizwitsCloudClient, GizwitsCloudError

_LOGGER = logging.getLogger(__name__)


async def validate_wavemaker_connection(host: str) -> dict[str, Any]:
    """Probe an IP for an MDP-20000 wavemaker."""
    device = MDP20000Device(host=host)
    try:
        await device.connect(timeout=10.0)
        await device.update()
        return {
            "device_id": device.device_id or "unknown",
            "model": device.model or MODEL_MDP20000,
            "state": device.state.name if device.state else "unknown",
            "mac_address": None,
            "firmware_version": None,
        }
    finally:
        await device.disconnect()


class JebaoConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Top-level Jebao config flow.

    Branches: choose the wavemaker (LAN discovery/manual IP) path or the
    doser (Gizwits cloud login) path. The doser firmware doesn't accept
    LAN writes, so doser users have to authenticate against the cloud.
    """

    VERSION = 2

    def __init__(self) -> None:
        self._discovered_devices: dict[str, Any] = {}
        self._selected_interfaces: list[str] | None = None
        self._discovery_attempted: bool = False
        self._no_devices_reason: str | None = None
        # Cloud-flow scratch state
        self._cloud_username: str | None = None
        self._cloud_password: str | None = None
        self._cloud_region: str = "us"
        self._cloud_devices: list[dict[str, Any]] = []

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Top-level menu: pick which pump family."""
        return self.async_show_menu(
            step_id="user",
            menu_options=["wavemaker", "doser"],
        )

    # ------------------------------------------------------------------ MDP-20000

    async def async_step_wavemaker(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        return self.async_show_menu(
            step_id="wavemaker",
            menu_options=["discover", "manual"],
        )

    async def async_step_discover(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        errors = {}

        if user_input is not None:
            selected = user_input.get(CONF_INTERFACES, [])
            self._selected_interfaces = [iface.split(" (")[0] for iface in selected]
            return await self.async_step_select_device()

        interfaces = self._get_available_interfaces()
        if not interfaces:
            return await self.async_step_manual()

        return self.async_show_form(
            step_id="discover",
            data_schema=vol.Schema(
                {
                    vol.Optional(
                        CONF_INTERFACES,
                        default=interfaces,
                    ): selector.SelectSelector(
                        selector.SelectSelectorConfig(
                            options=interfaces,
                            multiple=True,
                            mode=selector.SelectSelectorMode.DROPDOWN,
                        )
                    ),
                }
            ),
            description_placeholders={
                "interface_count": str(len(interfaces)),
                "interfaces": ", ".join(interfaces),
            },
            errors=errors,
        )

    async def async_step_select_device(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        errors = {}

        if user_input is not None:
            selected_id = user_input["device"]
            device_info = self._discovered_devices[selected_id]

            await self.async_set_unique_id(device_info["device_id"])
            self._abort_if_unique_id_configured()

            return self.async_create_entry(
                title=f"{device_info['model']} ({device_info['ip']})",
                data={
                    CONF_HOST: device_info["ip"],
                    CONF_DEVICE_ID: device_info["device_id"],
                    CONF_MODEL: device_info["model"],
                    "mac_address": device_info.get("mac"),
                    "firmware_version": device_info.get("firmware_version"),
                },
            )

        try:
            devices = await discover_devices(
                timeout=10.0, interfaces=self._selected_interfaces
            )
        except Exception as err:
            _LOGGER.error("Discovery failed: %s", err, exc_info=True)
            errors["base"] = "discovery_failed"
            return self.async_show_form(
                step_id="select_device",
                data_schema=vol.Schema({}),
                errors=errors,
            )

        # The doser flow uses cloud login, so we only surface wavemakers here.
        wavemakers = [d for d in devices if d.is_mdp20000]

        if not wavemakers:
            self._discovery_attempted = True
            self._no_devices_reason = "no_supported"
            return await self.async_step_manual()

        configured_ids = {entry.unique_id for entry in self._async_current_entries()}
        new_devices = [d for d in wavemakers if d.device_id not in configured_ids]

        if not new_devices:
            return self.async_abort(reason="already_configured")

        self._discovered_devices = {
            f"{d.device_id}_{d.ip_address}": {
                "device_id": d.device_id,
                "ip": d.ip_address,
                "model": d.model,
                "mac": d.mac_address,
                "firmware_version": d.firmware_version,
            }
            for d in new_devices
        }

        device_options = [
            selector.SelectOptionDict(
                value=key,
                label=f"{info['model']} ({info['device_id']}) at {info['ip']}",
            )
            for key, info in self._discovered_devices.items()
        ]

        return self.async_show_form(
            step_id="select_device",
            data_schema=vol.Schema(
                {
                    vol.Required("device"): selector.SelectSelector(
                        selector.SelectSelectorConfig(
                            options=device_options,
                            mode=selector.SelectSelectorMode.DROPDOWN,
                        )
                    ),
                }
            ),
            description_placeholders={"device_count": str(len(new_devices))},
            errors=errors,
        )

    async def async_step_dhcp(
        self, discovery_info: DhcpServiceInfo
    ) -> FlowResult:
        """Handle DHCP discovery - used for IP recovery on registered devices."""
        mac_normalized = discovery_info.macaddress.lower().replace(":", "")
        ip = discovery_info.ip

        for entry in self._async_current_entries():
            stored_mac = (entry.data.get("mac_address") or "").lower().replace(":", "")
            if stored_mac and stored_mac == mac_normalized:
                if entry.data.get(CONF_HOST) != ip:
                    _LOGGER.info(
                        "DHCP recovery: updating %s IP from %s to %s",
                        entry.title,
                        entry.data.get(CONF_HOST),
                        ip,
                    )
                    self.hass.config_entries.async_update_entry(
                        entry, data={**entry.data, CONF_HOST: ip}
                    )
                    self.hass.async_create_task(
                        self.hass.config_entries.async_reload(entry.entry_id)
                    )
                return self.async_abort(reason="already_configured")

        # Unknown MAC - we don't try to auto-create entries from DHCP alone
        # (need pump-side validation; UDP discovery covers new pumps).
        return self.async_abort(reason="not_jebao_device")

    async def async_step_integration_discovery(
        self, discovery_info: dict[str, Any]
    ) -> FlowResult:
        """Handle integration discovery (periodic UDP scan finding new pumps)."""
        device_id = discovery_info["device_id"]
        ip = discovery_info["ip"]

        await self.async_set_unique_id(device_id)
        self._abort_if_unique_id_configured(updates={CONF_HOST: ip})

        self._discovered_devices = {device_id: discovery_info}
        self.context["title_placeholders"] = {
            "name": f"{discovery_info['model']} ({ip})"
        }
        return await self.async_step_confirm_discovery()

    async def async_step_confirm_discovery(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Ask the user to confirm adding a discovered pump."""
        device_id = self.unique_id
        info = self._discovered_devices[device_id]

        if user_input is not None:
            return self.async_create_entry(
                title=f"{info['model']} ({info['ip']})",
                data={
                    CONF_HOST: info["ip"],
                    CONF_DEVICE_ID: device_id,
                    CONF_MODEL: info["model"],
                    "mac_address": info.get("mac_address"),
                    "firmware_version": info.get("firmware_version"),
                },
            )

        return self.async_show_form(
            step_id="confirm_discovery",
            description_placeholders={
                "model": info["model"],
                "device_id": device_id,
                "ip": info["ip"],
            },
        )

    async def async_step_manual(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        errors = {}
        if user_input is not None:
            host = user_input[CONF_HOST]
            try:
                info = await validate_wavemaker_connection(host)
                await self.async_set_unique_id(info["device_id"])
                self._abort_if_unique_id_configured()
                return self.async_create_entry(
                    title=f"{info['model']} ({host})",
                    data={
                        CONF_HOST: host,
                        CONF_DEVICE_ID: info["device_id"],
                        CONF_MODEL: info["model"],
                        "mac_address": info.get("mac_address"),
                        "firmware_version": info.get("firmware_version"),
                    },
                )
            except JebaoError as err:
                _LOGGER.error("Failed to connect to %s: %s", host, err)
                errors["base"] = "cannot_connect"
            except Exception as err:  # pylint: disable=broad-except
                _LOGGER.exception("Unexpected error: %s", err)
                errors["base"] = "unknown"

        description_placeholders = {}
        if self._discovery_attempted:
            description_placeholders["discovery_result"] = (
                "⚠️ No MDP-20000 wavemakers were found during automatic discovery."
            )
        else:
            description_placeholders["discovery_result"] = ""

        return self.async_show_form(
            step_id="manual",
            data_schema=vol.Schema({vol.Required(CONF_HOST): str}),
            description_placeholders=description_placeholders,
            errors=errors,
        )

    # ------------------------------------------------------------------ MD-4.4

    async def async_step_doser(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Step 1 of the doser flow: collect Gizwits credentials."""
        errors: dict[str, str] = {}

        if user_input is not None:
            self._cloud_username = user_input[CONF_USERNAME]
            self._cloud_password = user_input[CONF_PASSWORD]
            self._cloud_region = user_input.get(CONF_REGION, "us")

            session = async_get_clientsession(self.hass)
            client = GizwitsCloudClient(session, region=self._cloud_region)
            try:
                await client.login(self._cloud_username, self._cloud_password)
                self._cloud_devices = await client.get_bindings()
            except GizwitsAuthError:
                errors["base"] = "invalid_auth"
            except GizwitsCloudError as err:
                _LOGGER.error("Cloud error: %s", err)
                errors["base"] = "cannot_connect"

            if not errors:
                return await self.async_step_doser_pick()

        return self.async_show_form(
            step_id="doser",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_USERNAME): str,
                    vol.Required(CONF_PASSWORD): str,
                    vol.Required(CONF_REGION, default="us"): vol.In(
                        list(GIZWITS_REGIONS.keys())
                    ),
                }
            ),
            errors=errors,
        )

    async def async_step_doser_pick(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Step 2: pick which device on the account to control."""
        errors: dict[str, str] = {}

        if user_input is not None:
            did = user_input[CONF_DID]
            device = next((d for d in self._cloud_devices if d["did"] == did), None)
            if not device:
                errors["base"] = "device_not_found"
            else:
                await self.async_set_unique_id(did)
                self._abort_if_unique_id_configured()

                title = device.get("dev_alias") or device.get("product_name") or did
                return self.async_create_entry(
                    title=f"MD-4.4 ({title})",
                    data={
                        CONF_USERNAME: self._cloud_username,
                        CONF_PASSWORD: self._cloud_password,
                        CONF_REGION: self._cloud_region,
                        CONF_DID: did,
                        CONF_DEVICE_ID: did,
                        CONF_MODEL: MODEL_MD44,
                        "mac_address": device.get("mac"),
                        "firmware_version": device.get("wifi_soft_version"),
                    },
                )

        if not self._cloud_devices:
            return self.async_abort(reason="no_devices_on_account")

        options = [
            selector.SelectOptionDict(
                value=d["did"],
                label=(
                    f"{d.get('dev_alias') or d.get('product_name') or 'Unknown'} "
                    f"({d['did']})"
                ),
            )
            for d in self._cloud_devices
        ]

        return self.async_show_form(
            step_id="doser_pick",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_DID): selector.SelectSelector(
                        selector.SelectSelectorConfig(
                            options=options,
                            mode=selector.SelectSelectorMode.DROPDOWN,
                        )
                    ),
                }
            ),
            errors=errors,
        )

    # ------------------------------------------------------------------ helpers

    @staticmethod
    def _get_available_interfaces() -> list[str]:
        interfaces = []
        try:
            for iface in netifaces.interfaces():
                if iface.startswith("lo"):
                    continue
                addrs = netifaces.ifaddresses(iface)
                if netifaces.AF_INET in addrs:
                    ip = addrs[netifaces.AF_INET][0]["addr"]
                    interfaces.append(f"{iface} ({ip})")
        except Exception as err:
            _LOGGER.error("Error enumerating interfaces: %s", err)
        return interfaces

    @staticmethod
    @callback
    def async_get_options_flow(
        config_entry: config_entries.ConfigEntry,
    ) -> JebaoOptionsFlow:
        return JebaoOptionsFlow()


class JebaoOptionsFlow(config_entries.OptionsFlow):
    """Options flow for Jebao.

    HA 2024.11+ exposes ``config_entry`` as a read-only property, so we
    don't store it ourselves — the flow manager handles that for us.
    """

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)

        from .const import DEFAULT_SCAN_INTERVAL

        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Optional(
                        "scan_interval",
                        default=self.config_entry.options.get(
                            "scan_interval", DEFAULT_SCAN_INTERVAL
                        ),
                    ): vol.All(vol.Coerce(int), vol.Range(min=10, max=300)),
                }
            ),
        )
