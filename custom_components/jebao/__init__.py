"""The Jebao integration."""
from __future__ import annotations

from datetime import timedelta
import logging
from typing import TYPE_CHECKING, Any

import voluptuous as vol
from jebao import JebaoError, MDP20000Device, discover_devices

from homeassistant.config_entries import SOURCE_INTEGRATION_DISCOVERY, ConfigEntry
from homeassistant.const import CONF_HOST, Platform
from homeassistant.core import HomeAssistant, ServiceCall, SupportsResponse
from homeassistant.exceptions import ConfigEntryNotReady, HomeAssistantError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.helpers.typing import ConfigType

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

# Periodic background discovery interval (UDP broadcast scan)
DISCOVERY_INTERVAL = timedelta(minutes=5)


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Set up periodic background discovery for Jebao pumps."""

    async def _periodic_discovery(_now=None) -> None:
        try:
            devices = await discover_devices(timeout=5.0)
        except Exception as err:  # pylint: disable=broad-except
            _LOGGER.debug("Periodic discovery failed: %s", err)
            return

        entries = hass.config_entries.async_entries(DOMAIN)
        entries_by_id = {entry.unique_id: entry for entry in entries}

        for device in devices:
            if not device.is_mdp20000:
                continue

            existing = entries_by_id.get(device.device_id)
            if existing is not None:
                # Backfill missing MAC / update IP if it drifted (DHCP discovery is the
                # preferred IP-recovery path, but periodic scan is a belt-and-suspenders).
                updates: dict[str, Any] = {}
                if not existing.data.get("mac_address") and device.mac_address:
                    updates["mac_address"] = device.mac_address
                if existing.data.get(CONF_HOST) != device.ip_address:
                    updates[CONF_HOST] = device.ip_address
                if updates:
                    _LOGGER.info(
                        "Updating Jebao entry %s from periodic discovery: %s",
                        existing.title,
                        updates,
                    )
                    hass.config_entries.async_update_entry(
                        existing, data={**existing.data, **updates}
                    )
                continue

            # New, unconfigured pump - kick off a discovery flow so it appears
            # in Settings -> Devices & Services as "Discovered".
            hass.async_create_task(
                hass.config_entries.flow.async_init(
                    DOMAIN,
                    context={"source": SOURCE_INTEGRATION_DISCOVERY},
                    data={
                        "device_id": device.device_id,
                        "ip": device.ip_address,
                        "model": device.model,
                        "mac_address": device.mac_address,
                        "firmware_version": device.firmware_version,
                    },
                )
            )

    # Fire once shortly after startup, then on the regular interval.
    hass.async_create_background_task(
        _periodic_discovery(), "jebao_initial_discovery"
    )
    async_track_time_interval(hass, _periodic_discovery, DISCOVERY_INTERVAL)
    return True


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
        # Default the Calibration-amount slot before platforms load so the
        # paired "Value to enter in app" sensor never reads an empty bucket
        # during startup. The number entity overwrites this from its
        # restored state in async_added_to_hass.
        "dose_input": 1.0,
    }

    # Create the coordinator here, before forwarding to platforms, so all
    # platforms share one. The old "platform creates it if missing" pattern
    # was racy: ``async_forward_entry_setups`` runs platforms concurrently,
    # they all see "no coordinator yet", and each spins up its own — six
    # coordinators all polling on their own schedule. With one coordinator
    # built up front, every platform just reads it out of hass.data.
    from .coordinator import JebaoDataUpdateCoordinator

    scan_interval = entry.options.get("scan_interval")
    if scan_interval:
        coordinator = JebaoDataUpdateCoordinator(
            hass, device, entry, device_id, scan_interval
        )
    else:
        coordinator = JebaoDataUpdateCoordinator(hass, device, entry, device_id)
    await coordinator.async_config_entry_first_refresh()
    hass.data[DOMAIN][entry.entry_id]["coordinator"] = coordinator

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

_GET_SCHEDULE_SCHEMA = vol.Schema(
    {
        vol.Required("device_id"): cv.string,
        vol.Required("channel"): _CHANNEL,
    }
)

_GET_SLOT_SCHEMA = vol.Schema(
    {
        vol.Required("device_id"): cv.string,
        vol.Required("channel"): _CHANNEL,
        vol.Required("slot"): _SLOT,
    }
)


def _find_doser(hass: HomeAssistant, target: str):
    """Look up the MD44Device + config entry for a service call's device target.

    The device picker in services.yaml passes an HA device-registry ID
    (a long opaque string). We resolve that to the Gizwits ``did`` by
    pulling our domain's identifier out of the matched device's
    ``identifiers`` set, then locate the stored MD44Device.

    Returns ``(device, entry)`` or ``(None, None)`` if nothing matches.
    """
    registry = dr.async_get(hass)
    ha_device = registry.async_get(target)
    if ha_device is None:
        return None, None
    gizwits_did: str | None = None
    for domain, identifier in ha_device.identifiers:
        if domain == DOMAIN:
            gizwits_did = identifier
            break
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


def _raw_to_ml(raw: int, entry) -> float:
    """Inverse of ``_ml_to_raw``: scale the firmware's byte back to real
    mL using the 10x precision toggle. Returned as a float so the value
    keeps its decimal when the toggle is on."""
    factor = cal_factor(entry.options) if entry is not None else 1
    return raw / factor if factor != 1 else float(raw)


def _entry_to_dict(slot: int, entry_obj, config_entry) -> dict:
    """Render a ScheduleEntry as a dict suitable for both service-response
    payloads and entity attributes."""
    return {
        "slot": slot,
        "time": f"{entry_obj.hour:02d}:{entry_obj.minute:02d}",
        "hour": entry_obj.hour,
        "minute": entry_obj.minute,
        "quantity_ml": _raw_to_ml(entry_obj.quantity, config_entry),
    }


def _push_schedule_update(
    hass: HomeAssistant,
    device: MD44Device,
    channel_idx: int,
    entries: list,
) -> None:
    """Mirror the just-written schedule into the coordinator's data so the
    Channel N schedule text entity (and the next-dose / count sensors)
    re-render immediately rather than waiting for the next push.

    The WS subscription may replace ``device.state`` wholesale on each
    push, while ``coordinator.data["state"]`` keeps pointing at whatever
    object was current when ``async_set_updated_data`` last fired. Calling
    ``async_set_updated_data`` here rebinds the coordinator to the now-
    mutated state and notifies all listeners. The next push from the pump
    overwrites this once the cloud catches up.
    """
    if device.state is not None:
        device.state.schedules[channel_idx] = list(entries)
    for stored in hass.data.get(DOMAIN, {}).values():
        if stored.get("device") is device:
            coord = stored.get("coordinator")
            if coord is not None and device.state is not None:
                coord.async_set_updated_data({"state": device.state})
            return


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
        _push_schedule_update(hass, device, channel - 1, entries)

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
        # device.state is kept current by the WS push subscription, so we
        # don't need a pre-write REST poll here. Just bail if push hasn't
        # populated state yet (only possible in the brief window before the
        # first push lands after a reconnect).
        if device.state is None:
            _LOGGER.error("set_schedule_slot: device state not ready yet")
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
        _push_schedule_update(hass, device, channel - 1, entries)

    async def _handle_delete_slot(call: ServiceCall) -> None:
        target_id = call.data["device_id"]
        channel = int(call.data["channel"])
        slot_idx = int(call.data["slot"]) - 1
        device, _entry = _find_doser(hass, target_id)
        if device is None:
            _LOGGER.error("delete_schedule_slot: no MD-4.4 found with device_id %s", target_id)
            return
        if device.state is None:
            _LOGGER.error("delete_schedule_slot: device state not ready yet")
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
        _push_schedule_update(hass, device, channel - 1, entries)

    async def _handle_get_schedule(call: ServiceCall) -> dict:
        target_id = call.data["device_id"]
        channel = int(call.data["channel"])
        device, entry = _find_doser(hass, target_id)
        if device is None:
            raise HomeAssistantError(
                f"get_schedule: no MD-4.4 found for device {target_id}"
            )
        if device.state is None:
            # Stale cache — force a read so we don't return a placeholder.
            try:
                await device.update()
            except MD44Error as err:
                raise HomeAssistantError(f"get_schedule: cloud read failed: {err}")
        entries = device.state.schedules[channel - 1]
        return {
            "channel": channel,
            "factor": cal_factor(entry.options) if entry is not None else 1,
            "entry_count": len(entries),
            "entries": [
                _entry_to_dict(i, e, entry) for i, e in enumerate(entries, start=1)
            ],
        }

    async def _handle_get_slot(call: ServiceCall) -> dict:
        target_id = call.data["device_id"]
        channel = int(call.data["channel"])
        slot_1based = int(call.data["slot"])
        device, entry = _find_doser(hass, target_id)
        if device is None:
            raise HomeAssistantError(
                f"get_schedule_slot: no MD-4.4 found for device {target_id}"
            )
        if device.state is None:
            try:
                await device.update()
            except MD44Error as err:
                raise HomeAssistantError(
                    f"get_schedule_slot: cloud read failed: {err}"
                )
        entries = device.state.schedules[channel - 1]
        if slot_1based - 1 >= len(entries):
            # Return a present-but-empty result so automations can branch
            # cleanly on ``exists: false`` rather than having to catch an
            # error.
            return {
                "channel": channel,
                "slot": slot_1based,
                "exists": False,
            }
        entry_obj = entries[slot_1based - 1]
        result = _entry_to_dict(slot_1based, entry_obj, entry)
        result["channel"] = channel
        result["exists"] = True
        return result

    hass.services.async_register(
        DOMAIN, "set_schedule", _handle_set_schedule, schema=_SET_SCHEDULE_SCHEMA,
    )
    hass.services.async_register(
        DOMAIN, "set_schedule_slot", _handle_set_slot, schema=_SET_SLOT_SCHEMA,
    )
    hass.services.async_register(
        DOMAIN, "delete_schedule_slot", _handle_delete_slot, schema=_DELETE_SLOT_SCHEMA,
    )
    hass.services.async_register(
        DOMAIN, "get_schedule", _handle_get_schedule,
        schema=_GET_SCHEDULE_SCHEMA,
        supports_response=SupportsResponse.ONLY,
    )
    hass.services.async_register(
        DOMAIN, "get_schedule_slot", _handle_get_slot,
        schema=_GET_SLOT_SCHEMA,
        supports_response=SupportsResponse.ONLY,
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
