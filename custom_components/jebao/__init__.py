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
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.device_registry import DeviceInfo

from .const import (
    CONF_DID,
    CONF_PASSWORD,
    CONF_REGION,
    CONF_USERNAME,
    DOMAIN,
    MD44_CHANNEL_COUNT,
    MODEL_MD44,
    MODEL_MDP20000,
    cal_factor,
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
    Platform.TEXT,
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
        # The schedule-slot services need access to entry.options to read
        # the 10x precision toggle when converting real mL to raw bytes.
        "entry": entry,
    }

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    # Register the doser-specific schedule service. It's a no-op for the
    # wavemaker but harmless to register once globally.
    if _is_md44(model):
        await _async_register_md44_services(hass)

    return True


_QTY_ML = vol.All(
    vol.Any(int, float, vol.Coerce(float)),
    vol.Range(min=0.0, max=25.5),
)
_HOUR = vol.All(int, vol.Range(min=0, max=23))
_MINUTE = vol.All(int, vol.Range(min=0, max=59))
_CHANNEL = vol.All(int, vol.Range(min=1, max=MD44_CHANNEL_COUNT))
_SLOT = vol.All(int, vol.Range(min=1, max=24))


_SET_SCHEDULE_SCHEMA = vol.Schema(
    {
        vol.Required("device_id"): cv.string,
        vol.Required("channel"): _CHANNEL,
        vol.Required("entries"): vol.All(
            cv.ensure_list,
            [
                vol.Schema(
                    {
                        vol.Required("hour"): _HOUR,
                        vol.Required("minute"): _MINUTE,
                        vol.Required("quantity_ml"): _QTY_ML,
                    }
                )
            ],
        ),
    }
)

_SET_SLOT_SCHEMA = vol.Schema(
    {
        vol.Required("device_id"): cv.string,
        vol.Required("channel"): _CHANNEL,
        # 1-based across the visible (non-empty) entries of the schedule.
        # Slot 1 = the entry shown first in the text entity. Slots beyond
        # the current count append as a new entry (provided qty > 0).
        vol.Required("slot"): _SLOT,
        vol.Required("hour"): _HOUR,
        vol.Required("minute"): _MINUTE,
        vol.Required("quantity_ml"): _QTY_ML,
    }
)

_DELETE_SLOT_SCHEMA = vol.Schema(
    {
        vol.Required("device_id"): cv.string,
        vol.Required("channel"): _CHANNEL,
        vol.Required("slot"): _SLOT,
    }
)


def _find_doser(hass: HomeAssistant, target: str):
    """Look up the MD44Device + config entry for a service call's device target.

    Accepts either:
      * an HA device-registry ID (the long opaque string the device selector
        returns), or
      * the Gizwits ``did`` directly (kept as a fallback so manually-written
        automations from earlier versions don't break).

    Returns ``(device, entry)`` or ``(None, None)`` if nothing matches.
    """
    # First try as an HA device-registry ID: look it up and pull our domain's
    # identifier out of its identifiers set.
    gizwits_did: str | None = None
    registry = dr.async_get(hass)
    ha_device = registry.async_get(target)
    if ha_device is not None:
        for domain, identifier in ha_device.identifiers:
            if domain == DOMAIN:
                gizwits_did = identifier
                break
    else:
        # Caller passed the Gizwits did itself.
        gizwits_did = target

    if gizwits_did is None:
        return None, None

    for stored in hass.data.get(DOMAIN, {}).values():
        if stored.get("device_id") == gizwits_did and isinstance(
            stored.get("device"), MD44Device
        ):
            return stored["device"], stored.get("entry")
    return None, None


def _ml_to_raw(quantity_ml: float, entry) -> int:
    """Convert the real-mL value the user passed into the raw byte the
    firmware stores. Applies the 10x precision toggle if it's on for
    this entry."""
    factor = cal_factor(entry.options) if entry is not None else 1
    raw = int(round(float(quantity_ml) * factor))
    return max(0, min(255, raw))


async def _async_register_md44_services(hass: HomeAssistant) -> None:
    if hass.services.has_service(DOMAIN, "set_schedule"):
        return

    async def _handle_set_schedule(call: ServiceCall) -> None:
        target_id = call.data["device_id"]
        channel = int(call.data["channel"])
        device, entry = _find_doser(hass, target_id)
        if device is None:
            _LOGGER.error("set_schedule: no MD-4.4 found with device_id %s", target_id)
            return
        entries = [
            ScheduleEntry(
                hour=e["hour"],
                minute=e["minute"],
                quantity=_ml_to_raw(e["quantity_ml"], entry),
            )
            for e in call.data["entries"]
        ]
        await device.set_schedule(channel - 1, entries)

    async def _handle_set_slot(call: ServiceCall) -> None:
        target_id = call.data["device_id"]
        channel = int(call.data["channel"])
        slot_idx = int(call.data["slot"]) - 1
        hour = int(call.data["hour"])
        minute = int(call.data["minute"])
        device, entry = _find_doser(hass, target_id)
        if device is None:
            _LOGGER.error("set_schedule_slot: no MD-4.4 found with device_id %s", target_id)
            return
        raw_qty = _ml_to_raw(call.data["quantity_ml"], entry)
        # Read latest so we don't overwrite a parallel change.
        try:
            await device.update()
        except MD44Error as err:
            _LOGGER.error("set_schedule_slot: failed to read current state: %s", err)
            return
        entries = list(device.state.schedules[channel - 1])
        new_entry = ScheduleEntry(hour=hour, minute=minute, quantity=raw_qty)
        if slot_idx < len(entries):
            if raw_qty == 0:
                # Treat qty=0 as "delete this slot" so the user has one
                # service to remember.
                entries.pop(slot_idx)
            else:
                entries[slot_idx] = new_entry
        elif raw_qty > 0:
            # Past the end → append. Slot index beyond count is fine; we
            # just add to the next available position.
            if len(entries) >= 24:
                _LOGGER.error(
                    "set_schedule_slot: channel %d already has the max 24 entries",
                    channel,
                )
                return
            entries.append(new_entry)
        else:
            # Slot past the end AND qty=0 → nothing to do.
            return
        await device.set_schedule(channel - 1, entries)

    async def _handle_delete_slot(call: ServiceCall) -> None:
        target_id = call.data["device_id"]
        channel = int(call.data["channel"])
        slot_idx = int(call.data["slot"]) - 1
        device, _entry = _find_doser(hass, target_id)
        if device is None:
            _LOGGER.error("delete_schedule_slot: no MD-4.4 found with device_id %s", target_id)
            return
        try:
            await device.update()
        except MD44Error as err:
            _LOGGER.error("delete_schedule_slot: failed to read current state: %s", err)
            return
        entries = list(device.state.schedules[channel - 1])
        if slot_idx >= len(entries):
            _LOGGER.warning(
                "delete_schedule_slot: channel %d has only %d entries; nothing to delete at slot %d",
                channel, len(entries), slot_idx + 1,
            )
            return
        entries.pop(slot_idx)
        await device.set_schedule(channel - 1, entries)

    hass.services.async_register(
        DOMAIN, "set_schedule", _handle_set_schedule, schema=_SET_SCHEDULE_SCHEMA,
    )
    hass.services.async_register(
        DOMAIN, "set_schedule_slot", _handle_set_slot, schema=_SET_SLOT_SCHEMA,
    )
    hass.services.async_register(
        DOMAIN, "delete_schedule_slot", _handle_delete_slot, schema=_DELETE_SLOT_SCHEMA,
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
