"""Sensor platform for Jebao."""
from __future__ import annotations

import datetime as dt
import logging

from homeassistant.components.sensor import SensorEntity, SensorStateClass
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import PERCENTAGE, UnitOfTime
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN, MD44_CHANNEL_COUNT, MODEL_MD44
from .coordinator import JebaoDataUpdateCoordinator
from .entity import JebaoEntity

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Jebao sensors from config entry."""
    data = hass.data[DOMAIN][entry.entry_id]
    device = data["device"]
    device_id = data["device_id"]
    model = data["model"]
    host = data["host"]
    mac_address = data.get("mac_address")
    firmware_version = data.get("firmware_version")

    if "coordinator" not in data:
        scan_interval = entry.options.get("scan_interval")
        if scan_interval:
            coordinator = JebaoDataUpdateCoordinator(hass, device, entry, device_id, scan_interval)
        else:
            coordinator = JebaoDataUpdateCoordinator(hass, device, entry, device_id)
        await coordinator.async_config_entry_first_refresh()
        data["coordinator"] = coordinator
    else:
        coordinator = data["coordinator"]

    if model == MODEL_MD44:
        entities: list[SensorEntity] = [
            MD44CalibrationChannelSensor(coordinator, device_id, model, host, mac_address, firmware_version),
            MD44Calib1Sensor(coordinator, device_id, model, host, mac_address, firmware_version),
            MD44ClockSensor(coordinator, device_id, model, host, mac_address, firmware_version),
        ]
        for idx in range(MD44_CHANNEL_COUNT):
            entities.append(
                MD44ScheduleCountSensor(coordinator, device_id, model, host, idx, mac_address, firmware_version)
            )
            entities.append(
                MD44NextScheduleSensor(coordinator, device_id, model, host, idx, mac_address, firmware_version)
            )
        async_add_entities(entities)
    else:
        async_add_entities(
            [
                JebaoSpeedSensor(coordinator, device_id, model, host, mac_address, firmware_version),
                JebaoStateSensor(coordinator, device_id, model, host, mac_address, firmware_version),
            ]
        )


class JebaoSpeedSensor(JebaoEntity, SensorEntity):
    """Sensor for current pump speed (MDP-20000)."""

    _attr_translation_key = "speed"
    _attr_native_unit_of_measurement = PERCENTAGE
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(
        self,
        coordinator: JebaoDataUpdateCoordinator,
        device_id: str,
        model: str,
        host: str,
        mac_address: str | None = None,
        firmware_version: str | None = None,
    ) -> None:
        """Initialize sensor."""
        super().__init__(coordinator, device_id, model, host, mac_address, firmware_version)
        self._attr_unique_id = f"{device_id}_speed"
        self._attr_name = "Speed"
        self._attr_icon = "mdi:speedometer"

    @property
    def native_value(self) -> int | None:
        return self.coordinator.data.get("speed")


class JebaoStateSensor(JebaoEntity, SensorEntity):
    """Sensor for device state (MDP-20000)."""

    _attr_translation_key = "state"

    def __init__(
        self,
        coordinator: JebaoDataUpdateCoordinator,
        device_id: str,
        model: str,
        host: str,
        mac_address: str | None = None,
        firmware_version: str | None = None,
    ) -> None:
        """Initialize sensor."""
        super().__init__(coordinator, device_id, model, host, mac_address, firmware_version)
        self._attr_unique_id = f"{device_id}_state"
        self._attr_name = "State"
        self._attr_icon = "mdi:information"

    @property
    def native_value(self) -> str | None:
        from jebao import DeviceState  # noqa: F401  (kept for typing parity)

        state = self.coordinator.data.get("state")
        if state is None:
            return None
        if hasattr(state, "name"):
            return state.name
        return None


# ---------- MD-4.4 sensors ----------


class MD44CalibrationChannelSensor(JebaoEntity, SensorEntity):
    """Which channel is currently armed for calibration (1..4).

    The cloud returns the value as a localized string like ``"校准1"``
    (Chinese for "Calibration 1") or sometimes the English equivalent
    depending on account locale. We extract the trailing digit so the
    sensor surfaces a clean integer 1..4.
    """

    _attr_translation_key = "calibration_channel"
    _attr_icon = "mdi:tune-variant"

    def __init__(self, coordinator, device_id, model, host, mac_address=None, firmware_version=None) -> None:
        super().__init__(coordinator, device_id, model, host, mac_address, firmware_version)
        self._attr_unique_id = f"{device_id}_cal_channel"
        self._attr_name = "Calibration channel"

    @property
    def native_value(self) -> int | None:
        state = self.coordinator.data.get("state")
        if state is None or not state.cal_set:
            return None
        import re
        match = re.search(r"(\d+)", state.cal_set)
        return int(match.group(1)) if match else None


class MD44Calib1Sensor(JebaoEntity, SensorEntity):
    """The currently-stored calibration value (10..100)."""

    _attr_translation_key = "calib1"
    _attr_icon = "mdi:cup-water"
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, coordinator, device_id, model, host, mac_address=None, firmware_version=None) -> None:
        super().__init__(coordinator, device_id, model, host, mac_address, firmware_version)
        self._attr_unique_id = f"{device_id}_calib1"
        self._attr_name = "Calibration value"

    @property
    def native_value(self) -> int | None:
        state = self.coordinator.data.get("state")
        if state is None:
            return None
        return state.calib1


class MD44ClockSensor(JebaoEntity, SensorEntity):
    """The clock the pump's MCU currently believes is "now".

    Both ``YMDData`` and ``HMSData`` come from the cloud as 8-char hex
    strings (e.g. ``"19050a00"`` = year 25, month 5, day 10, dow 0).
    The pump returns all zeros until ``Sync clock`` has been pressed,
    so an unset clock just shows "unset" instead of "2000-00-00".
    """

    _attr_translation_key = "clock"
    _attr_icon = "mdi:clock-outline"

    def __init__(self, coordinator, device_id, model, host, mac_address=None, firmware_version=None) -> None:
        super().__init__(coordinator, device_id, model, host, mac_address, firmware_version)
        self._attr_unique_id = f"{device_id}_clock"
        self._attr_name = "Device clock"

    @property
    def native_value(self) -> str | None:
        state = self.coordinator.data.get("state")
        if state is None:
            return None
        ymd_hex = state.ymd or ""
        hms_hex = state.hms or ""
        if len(ymd_hex) < 8 or len(hms_hex) < 8:
            return None
        if ymd_hex == "00000000" and hms_hex == "00000000":
            return "unset"
        try:
            ymd = bytes.fromhex(ymd_hex)
            hms = bytes.fromhex(hms_hex)
        except ValueError:
            return None
        if len(ymd) < 3 or len(hms) < 3:
            return None
        year = 2000 + ymd[0]
        return (
            f"{year:04d}-{ymd[1]:02d}-{ymd[2]:02d} "
            f"{hms[0]:02d}:{hms[1]:02d}:{hms[2]:02d}"
        )


class MD44ScheduleCountSensor(JebaoEntity, SensorEntity):
    """Number of programmed schedule entries on one channel."""

    _attr_icon = "mdi:calendar-clock"
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, coordinator, device_id, model, host, idx, mac_address=None, firmware_version=None) -> None:
        super().__init__(coordinator, device_id, model, host, mac_address, firmware_version)
        self._idx = idx
        ch = idx + 1
        self._attr_unique_id = f"{device_id}_sched_count_{ch}"
        self._attr_name = f"Channel {ch} schedules"
        self._attr_translation_key = f"sched_count_{ch}"

    @property
    def native_value(self) -> int | None:
        state = self.coordinator.data.get("state")
        if state is None:
            return None
        return len(state.schedules[self._idx])


class MD44NextScheduleSensor(JebaoEntity, SensorEntity):
    """The next-up schedule entry for one channel, as HH:MM (Qmg)."""

    _attr_icon = "mdi:clock-time-four-outline"

    def __init__(self, coordinator, device_id, model, host, idx, mac_address=None, firmware_version=None) -> None:
        super().__init__(coordinator, device_id, model, host, mac_address, firmware_version)
        self._idx = idx
        ch = idx + 1
        self._attr_unique_id = f"{device_id}_next_sched_{ch}"
        self._attr_name = f"Channel {ch} next dose"
        self._attr_translation_key = f"next_sched_{ch}"

    @property
    def native_value(self) -> str | None:
        state = self.coordinator.data.get("state")
        if state is None:
            return None
        entries = state.schedules[self._idx]
        if not entries:
            return None
        # Sort by time-of-day so we always pick the chronologically next one,
        # not the next one in the order the user added them in the app.
        sorted_entries = sorted(entries, key=lambda e: (e.hour, e.minute))
        now = dt.datetime.now().time()
        upcoming = [e for e in sorted_entries if (e.hour, e.minute) >= (now.hour, now.minute)]
        nxt = upcoming[0] if upcoming else sorted_entries[0]
        suffix = "" if upcoming else " (tomorrow)"
        return f"{nxt.hour:02d}:{nxt.minute:02d} ({nxt.quantity_ml:g} mL){suffix}"
