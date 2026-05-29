"""The Jebao integration."""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from jebao import JebaoError, MDP20000Device

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_HOST, Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers.device_registry import DeviceInfo

from .const import DOMAIN, MODEL_MD44, MODEL_MDP20000
from .md44 import MD44Device, MD44Error

if TYPE_CHECKING:
    from homeassistant.helpers.entity import Entity

_LOGGER = logging.getLogger(__name__)

# Platforms used by either pump model. Each platform decides per-entry whether
# it has anything to add based on the configured model.
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
    host = entry.data[CONF_HOST]
    device_id = entry.data.get("device_id")
    model = entry.data.get("model", MODEL_MDP20000)
    mac_address = entry.data.get("mac_address")
    firmware_version = entry.data.get("firmware_version")

    _LOGGER.info("Setting up Jebao %s at %s", model, host)

    if _is_md44(model):
        device = MD44Device(host=host, device_id=device_id)
        try:
            await device.connect()
        except MD44Error as err:
            _LOGGER.error("Failed to connect to MD-4.4 at %s: %s", host, err)
            await device.disconnect()
            raise ConfigEntryNotReady(f"Failed to connect: {err}") from err
    else:
        device = MDP20000Device(host=host, device_id=device_id)
        try:
            await device.connect()
            # Ensure manual mode (exit Program mode if active)
            await device.ensure_manual_mode()
        except JebaoError as err:
            _LOGGER.error("Failed to connect to Jebao device at %s: %s", host, err)
            await device.disconnect()
            raise ConfigEntryNotReady(f"Failed to connect: {err}") from err

    _LOGGER.info("Successfully connected to Jebao %s at %s", model, host)

    # Store device instance
    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN][entry.entry_id] = {
        "device": device,
        "host": host,
        "device_id": device_id,
        "model": model,
        "mac_address": mac_address,
        "firmware_version": firmware_version,
    }

    # Forward setup to platforms
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    # Unload platforms
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)

    if unload_ok:
        data = hass.data[DOMAIN].pop(entry.entry_id)
        device = data["device"]
        await device.disconnect()
        _LOGGER.info("Disconnected from Jebao device at %s", data["host"])

    return unload_ok


def get_device_info(entry: ConfigEntry) -> DeviceInfo:
    """Get device info for device registry."""
    device_id = entry.data.get("device_id", "unknown")
    model = entry.data.get("model", MODEL_MDP20000)
    host = entry.data[CONF_HOST]

    return DeviceInfo(
        identifiers={(DOMAIN, device_id)},
        name=entry.title,
        manufacturer="Jebao",
        model=model,
        configuration_url=f"http://{host}:12416",
    )
