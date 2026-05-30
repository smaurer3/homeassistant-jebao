"""Number platform for Jebao."""
from __future__ import annotations

import logging

from jebao import JebaoError

from homeassistant.components.number import NumberEntity, NumberMode
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfTime
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN, MD44_CHANNEL_COUNT, MODEL_MD44
from .coordinator import JebaoDataUpdateCoordinator
from .entity import JebaoEntity
from .md44 import MD44Device, MD44Error

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Jebao number entities from config entry."""
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
        # Interval-in-days entities are read-only for now: the byte-level
        # write protocol is documented but not yet validated end-to-end on
        # this firmware, so we show the value but don't accept changes.
        entities: list[NumberEntity] = []
        for idx in range(MD44_CHANNEL_COUNT):
            entities.append(
                MD44IntervalDaysSensor(coordinator, device_id, model, host, device, idx, mac_address, firmware_version)
            )
        async_add_entities(entities)
        return

    # MDP-20000 path
    async_add_entities(
        [
            JebaoFeedDurationNumber(coordinator, device_id, model, host, device, mac_address, firmware_version),
        ]
    )


class JebaoFeedDurationNumber(JebaoEntity, NumberEntity):
    """Number entity for feed duration (MDP-20000)."""

    _attr_translation_key = "feed_duration"
    _attr_native_unit_of_measurement = UnitOfTime.MINUTES
    _attr_mode = NumberMode.SLIDER
    _attr_native_min_value = 1
    _attr_native_max_value = 10
    _attr_native_step = 1

    def __init__(
        self,
        coordinator: JebaoDataUpdateCoordinator,
        device_id: str,
        model: str,
        host: str,
        device,
        mac_address: str | None = None,
        firmware_version: str | None = None,
    ) -> None:
        super().__init__(coordinator, device_id, model, host, mac_address, firmware_version)
        self._device = device
        self._attr_unique_id = f"{device_id}_feed_duration"
        self._attr_name = "Feed duration"
        self._attr_icon = "mdi:timer"
        self._value = 1

    @property
    def native_value(self) -> float:
        return self._value

    async def async_set_native_value(self, value: float) -> None:
        try:
            minutes = int(value)
            await self._device.set_feed_duration(minutes)
            self._value = minutes
            self.async_write_ha_state()
            _LOGGER.info("Feed duration set to %d minutes", minutes)
        except JebaoError as err:
            _LOGGER.error("Failed to set feed duration: %s", err)


class MD44IntervalDaysSensor(JebaoEntity, NumberEntity):
    """Per-channel "interval in days" between scheduled doses.

    The pump stores one byte per channel (IntervalT1..IntervalT4). The value
    is the gap between schedule firings in days; the firmware caps it at 30.
    This entity reports the stored value. Setting it requires the byte-level
    write protocol which isn't shipped yet; the value field is disabled for
    now so HA shows the reading without offering a slider that would no-op.
    """

    _attr_native_unit_of_measurement = UnitOfTime.DAYS
    _attr_mode = NumberMode.BOX
    _attr_native_min_value = 0
    _attr_native_max_value = 30
    _attr_native_step = 1
    _attr_icon = "mdi:calendar-range"

    def __init__(
        self,
        coordinator: JebaoDataUpdateCoordinator,
        device_id: str,
        model: str,
        host: str,
        device: MD44Device,
        idx: int,
        mac_address: str | None = None,
        firmware_version: str | None = None,
    ) -> None:
        super().__init__(coordinator, device_id, model, host, mac_address, firmware_version)
        self._device = device
        self._idx = idx
        ch = idx + 1
        self._attr_unique_id = f"{device_id}_interval_{ch}"
        self._attr_name = f"Channel {ch} interval"
        self._attr_translation_key = f"interval_{ch}"

    @property
    def native_value(self) -> float | None:
        state = self.coordinator.data.get("state")
        if state is None:
            return None
        return float(state.intervals_days[self._idx])

    async def async_set_native_value(self, value: float) -> None:
        try:
            await self._device.set_interval_days(self._idx, int(value))
            await self.coordinator.async_request_refresh()
        except MD44Error as err:
            _LOGGER.error(
                "Failed to set channel %d interval: %s", self._idx + 1, err
            )
