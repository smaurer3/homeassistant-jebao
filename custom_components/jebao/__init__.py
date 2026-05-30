"""The Jebao integration."""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

import voluptuous as vol
from jebao import JebaoError, MDP20000Device

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_HOST, Platform
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.device_registry import DeviceInfo

from .const import (
    CONF_DID,
    CONF_PASSWORD,
    CONF_REGION,
    CONF_USERNAME,
    DOMAIN,
    MODEL_MD44,
    MODEL_MDP20000,
)
from .md44 import MD44Device, MD44Error, ScheduleEntry

if TYPE_CHECKING:
    from homeassistant.helpers.entity import Entity

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [
    Platform.FAN,
    Platform.SWITCH,
    Platform.BINARY_SENSOR,
    Platform.BUTTON,
    Platform.NUMBER,
    Platform.SENSOR,
]


def _is_md44(model: str | None) -> bool:
    return model == MODEL_MD44


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Jebao from a config entry."""
    model = entry.data.get("model", MODEL_MDP20000)
    device_id = entry.data.get("device_id")
    mac_address = entry.data.get("mac_address")
    firmware_version = entry.data.get("firmware_version")

    _LOGGER.info("Setting up Jebao %s (entry %s)", model, entry.title)

    if _is_md44(model):
        # Cloud-backed setup. Host is just a display string for the
        # device_info card.
        session = async_get_clientsession(hass)
        device = MD44Device(
            session=session,
            username=entry.data[CONF_USERNAME],
            password=entry.data[CONF_PASSWORD],
            region=entry.data.get(CONF_REGION, "us"),
            did=entry.data[CONF_DID],
            device_id=device_id,
        )
        try:
            await device.connect()
        except MD44Error as err:
            _LOGGER.error("Failed to connect to MD-4.4 %s: %s", device_id, err)
            await device.disconnect()
            raise ConfigEntryNotReady(f"Failed to connect: {err}") from err
        host = device.host
    else:
        host = entry.data[CONF_HOST]
        device = MDP20000Device(host=host, device_id=device_id)
        try:
            await device.connect()
            await device.ensure_manual_mode()
        except JebaoError as err:
            _LOGGER.error("Failed to connect to Jebao device at %s: %s", host, err)
            await device.disconnect()
            raise ConfigEntryNotReady(f"Failed to connect: {err}") from err

    _LOGGER.info("Successfully connected to Jebao %s", model)

    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN][entry.entry_id] = {
        "device": device,
        "host": host,
        "device_id": device_id,
        "model": model,
        "mac_address": mac_address,
        "firmware_version": firmware_version,
    }

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    # Register the doser-specific schedule service. It's a no-op for the
    # wavemaker but harmless to register once globally.
    if _is_md44(model):
        await _async_register_md44_services(hass)

    return True


_SET_SCHEDULE_SCHEMA = vol.Schema(
    {
        vol.Required("device_id"): cv.string,
        vol.Required("channel"): vol.All(int, vol.Range(min=1, max=4)),
        vol.Required("entries"): vol.All(
            cv.ensure_list,
            [
                vol.Schema(
                    {
                        vol.Required("hour"): vol.All(int, vol.Range(min=0, max=23)),
                        vol.Required("minute"): vol.All(int, vol.Range(min=0, max=59)),
                        vol.Required("quantity_ml"): vol.All(int, vol.Range(min=0, max=255)),
                    }
                )
            ],
        ),
    }
)


async def _async_register_md44_services(hass: HomeAssistant) -> None:
    if hass.services.has_service(DOMAIN, "set_schedule"):
        return

    async def _handle_set_schedule(call: ServiceCall) -> None:
        target_id = call.data["device_id"]
        channel = int(call.data["channel"])
        entries = [
            ScheduleEntry(hour=e["hour"], minute=e["minute"], quantity=e["quantity_ml"])
            for e in call.data["entries"]
        ]
        # Find the matching MD44Device by device_id (matches the unique_id we
        # set during config flow).
        for stored in hass.data.get(DOMAIN, {}).values():
            if stored.get("device_id") == target_id and isinstance(
                stored.get("device"), MD44Device
            ):
                device: MD44Device = stored["device"]
                await device.set_schedule(channel - 1, entries)
                return
        _LOGGER.error("set_schedule: no MD-4.4 found with device_id %s", target_id)

    hass.services.async_register(
        DOMAIN,
        "set_schedule",
        _handle_set_schedule,
        schema=_SET_SCHEDULE_SCHEMA,
    )


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        data = hass.data[DOMAIN].pop(entry.entry_id)
        device = data["device"]
        await device.disconnect()
        _LOGGER.info("Disconnected Jebao device at %s", data["host"])
    return unload_ok


def get_device_info(entry: ConfigEntry) -> DeviceInfo:
    device_id = entry.data.get("device_id", "unknown")
    model = entry.data.get("model", MODEL_MDP20000)
    host = entry.data.get(CONF_HOST, "gizwits-cloud")

    return DeviceInfo(
        identifiers={(DOMAIN, device_id)},
        name=entry.title,
        manufacturer="Jebao",
        model=model,
        configuration_url=f"http://{host}:12416",
    )
