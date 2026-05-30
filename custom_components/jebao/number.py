"""Number platform for Jebao."""
from __future__ import annotations

import logging

from jebao import JebaoError

from homeassistant.components.number import NumberEntity, NumberMode
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfTime
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN, MD44_CHANNEL_COUNT, MODEL_MD44, cal_factor
from .coordinator import JebaoDataUpdateCoordinator
from .entity import JebaoEntity
from .md44 import MD44Device, MD44Error
from homeassistant.helpers.restore_state import RestoreEntity

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
        entities: list[NumberEntity] = []
        for idx in range(MD44_CHANNEL_COUNT):
            entities.append(
                MD44IntervalDaysSensor(coordinator, device_id, model, host, device, idx, mac_address, firmware_version)
            )
        # Dose calculator pair — the user types the actual mL they want into
        # ``MD44DoseInputNumber`` and reads the matching app-side value off
        # ``MD44DoseAppValueSensor`` (defined in sensor.py).
        entities.append(
            MD44DoseInputNumber(coordinator, device_id, model, host, entry, mac_address, firmware_version)
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
    """Per-channel "days to skip" between scheduled doses.

    0 = dose every day (no skipping), 1 = every second day, 2 = every third
    day, and so on. The official app accepts values up to at least 184,
    so we deliberately don't tighten the range below the firmware's uint8
    limit. Stored as ``IntervalT1``..``IntervalT4`` in the cloud.
    """

    _attr_native_unit_of_measurement = UnitOfTime.DAYS
    _attr_mode = NumberMode.BOX
    _attr_native_min_value = 0
    _attr_native_max_value = 255
    # Integer step so HA renders the value without a ".0" suffix — the pump
    # only stores whole-day intervals.
    _attr_native_step = 1
    _attr_suggested_display_precision = 0
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
        import asyncio as _asyncio
        self._device = device
        self._idx = idx
        self._optimistic: float | None = None
        self._verify_task: _asyncio.Task | None = None
        ch = idx + 1
        self._attr_unique_id = f"{device_id}_interval_{ch}"
        self._attr_name = f"Channel {ch} interval"
        self._attr_translation_key = f"interval_{ch}"

    @property
    def native_value(self) -> float | None:
        if self._optimistic is not None:
            return self._optimistic
        state = self.coordinator.data.get("state")
        if state is None:
            return None
        return float(state.intervals_days[self._idx])

    async def async_set_native_value(self, value: float) -> None:
        import asyncio
        if self._verify_task and not self._verify_task.done():
            self._verify_task.cancel()
            self._verify_task = None
        target = int(value)
        self._optimistic = float(target)
        self.async_write_ha_state()
        try:
            await self._device.set_interval_days(self._idx, target)
        except MD44Error as err:
            _LOGGER.error(
                "Failed to set channel %d interval: %s", self._idx + 1, err
            )
            self._optimistic = None
            self.async_write_ha_state()
            return

        async def _verify() -> None:
            try:
                for delay in (5.0, 4.0, 4.0):
                    await asyncio.sleep(delay)
                    try:
                        await self.coordinator.async_request_refresh()
                    except Exception:  # pylint: disable=broad-except
                        continue
                    state = self.coordinator.data.get("state")
                    if state is not None and state.intervals_days[self._idx] == target:
                        return
            except asyncio.CancelledError:
                return
            finally:
                self._optimistic = None
                self.async_write_ha_state()

        self._verify_task = self.hass.async_create_task(_verify())


class MD44DoseInputNumber(JebaoEntity, NumberEntity, RestoreEntity):
    """Calculator-helper input: type the actual mL you want to dose.

    Paired with ``MD44DoseAppValueSensor`` in sensor.py — when the 10x
    calibration mode is on, the sensor shows ``this value * 10`` so you
    know what raw number to type into the Jebao app's UI (which only
    accepts whole-mL values). When the calibration toggle is off the
    sensor mirrors this value verbatim.

    The number persists across HA restarts via ``RestoreEntity`` so you
    don't have to re-set it every time you reboot.
    """

    _attr_translation_key = "dose_input"
    _attr_icon = "mdi:beaker-question-outline"
    _attr_native_min_value = 0.0
    _attr_native_max_value = 25.5
    _attr_native_step = 0.1
    _attr_native_unit_of_measurement = "mL"
    _attr_mode = NumberMode.BOX
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(
        self,
        coordinator: JebaoDataUpdateCoordinator,
        device_id: str,
        model: str,
        host: str,
        entry,
        mac_address: str | None = None,
        firmware_version: str | None = None,
    ) -> None:
        super().__init__(coordinator, device_id, model, host, mac_address, firmware_version)
        self._entry = entry
        self._value: float = 1.0
        self._attr_unique_id = f"{device_id}_dose_input"
        self._attr_name = "Desired dose"

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        last_state = await self.async_get_last_state()
        if last_state is not None and last_state.state not in (None, "", "unknown", "unavailable"):
            try:
                self._value = float(last_state.state)
            except ValueError:
                pass
        # Publish the current value so the paired Required-app-value sensor
        # can read it without trying to guess our entity_id (which HA derives
        # from the display name, not the unique_id we set).
        self._publish_to_shared_state()

    def _publish_to_shared_state(self) -> None:
        bucket = self.hass.data.setdefault(DOMAIN, {}).setdefault(
            self._entry.entry_id, {}
        )
        bucket["dose_input"] = self._value

    @property
    def native_value(self) -> float:
        return self._value

    async def async_set_native_value(self, value: float) -> None:
        self._value = round(float(value), 1)
        self._publish_to_shared_state()
        self.async_write_ha_state()
        # Force the paired sensor's listener to recompute now.
        await self.coordinator.async_request_refresh()
