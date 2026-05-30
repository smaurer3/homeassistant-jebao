"""Switch platform for Jebao MD-4.4 dosing pump.

Exposes:
  - master switch (powers all channels)
  - one switch per channel (channe1..channe4) — toggles the channel ON/OFF
  - one timer-enable switch per channel (Timer1ON..Timer4ON) — whether the
    pump's stored schedule runs for that channel

The MDP-20000 wavemaker doesn't use this platform; it stays on ``fan``.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import (
    DOMAIN,
    MD44_CHANNEL_COUNT,
    MODEL_MD44,
    OPT_CAL_FACTOR_10X,
)
from .coordinator import JebaoDataUpdateCoordinator
from .entity import JebaoEntity
from .md44 import MD44Device, MD44Error

_LOGGER = logging.getLogger(__name__)

# Pump -> MQTT -> cloud cache propagation usually takes 2-5 seconds. We
# want to confirm the write without spamming the cloud, so we hold the
# optimistic pin for a generous initial wait, then refresh and only do a
# couple of retries before giving up. With the older "poll every 2 s for
# 20 s" loop, multiple switches toggled in quick succession produced ~1
# refresh per second between them.
VERIFY_RETRY_DELAYS = (5.0, 4.0, 4.0)  # total worst case ~13 s + cloud latency


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Jebao switches from config entry (MD-4.4 only)."""
    data = hass.data[DOMAIN][entry.entry_id]
    if data["model"] != MODEL_MD44:
        return

    device: MD44Device = data["device"]
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

    entities: list[SwitchEntity] = [
        MD44MasterSwitch(coordinator, device_id, model, host, device, mac_address, firmware_version),
        MD44CalFactorSwitch(coordinator, device_id, model, host, entry, mac_address, firmware_version),
    ]
    for idx in range(MD44_CHANNEL_COUNT):
        entities.append(
            MD44ChannelSwitch(coordinator, device_id, model, host, device, idx, mac_address, firmware_version)
        )
        entities.append(
            MD44TimerEnableSwitch(coordinator, device_id, model, host, device, idx, mac_address, firmware_version)
        )
    async_add_entities(entities)


class _MD44SwitchBase(JebaoEntity, SwitchEntity):
    """Shared init for MD-4.4 switches.

    Each subclass keeps an ``_optimistic`` override so we can show the
    just-written state immediately and not flap back to the cloud's stale
    cached value before the pump has reported in.
    """

    _attr_assumed_state = False

    def __init__(
        self,
        coordinator: JebaoDataUpdateCoordinator,
        device_id: str,
        model: str,
        host: str,
        device: MD44Device,
        mac_address: str | None = None,
        firmware_version: str | None = None,
    ) -> None:
        super().__init__(coordinator, device_id, model, host, mac_address, firmware_version)
        self._device = device
        self._optimistic: bool | None = None
        self._verify_task: asyncio.Task | None = None

    def _set_optimistic(self, value: bool) -> None:
        """Pin the displayed state to ``value`` until the cloud catches up."""
        self._optimistic = value
        self.async_write_ha_state()

    def _clear_optimistic(self) -> None:
        self._optimistic = None
        self.async_write_ha_state()

    def _coordinator_value(self) -> bool:
        """Subclasses override to return the boolean from the coordinator."""
        raise NotImplementedError

    @property
    def is_on(self) -> bool:
        if self._optimistic is not None:
            return self._optimistic
        return self._coordinator_value()

    async def _do_write(self, target: bool, write_coro) -> None:
        """Run the cloud write and hold the optimistic state until the
        cloud's cached value catches up.

        If a write is in flight for this entity (verify task hasn't
        finished) it's cancelled so we don't pile multiple verify loops
        on top of each other and storm the cloud with refreshes.
        """
        # Cancel any verify still running from a previous toggle of the
        # same switch — otherwise back-to-back clicks stack verify loops.
        if self._verify_task and not self._verify_task.done():
            self._verify_task.cancel()
            self._verify_task = None

        self._set_optimistic(target)
        try:
            await write_coro
        except MD44Error as err:
            self._clear_optimistic()
            raise err

        async def _verify() -> None:
            try:
                for delay in VERIFY_RETRY_DELAYS:
                    await asyncio.sleep(delay)
                    try:
                        await self.coordinator.async_request_refresh()
                    except Exception:  # pylint: disable=broad-except
                        # One failed refresh shouldn't abandon the pin.
                        continue
                    if self._coordinator_value() == target:
                        return
                _LOGGER.debug(
                    "Verify gave up after %.0fs; cloud still doesn't reflect "
                    "the write. Releasing optimistic pin.",
                    sum(VERIFY_RETRY_DELAYS),
                )
            except asyncio.CancelledError:
                # A newer write took over — let it own the pin.
                return
            finally:
                self._clear_optimistic()

        self._verify_task = self.hass.async_create_task(_verify())


class MD44MasterSwitch(_MD44SwitchBase):
    """The pump's master power switch."""

    _attr_translation_key = "master"
    _attr_icon = "mdi:power"

    def __init__(self, coordinator, device_id, model, host, device, mac_address=None, firmware_version=None) -> None:
        super().__init__(coordinator, device_id, model, host, device, mac_address, firmware_version)
        self._attr_unique_id = f"{device_id}_master"
        self._attr_name = "Master"

    def _coordinator_value(self) -> bool:
        state = self.coordinator.data.get("state")
        return bool(state and state.master_on)

    async def async_turn_on(self, **kwargs: Any) -> None:
        try:
            await self._do_write(True, self._device.set_master(True))
        except MD44Error as err:
            _LOGGER.error("Failed to turn on master: %s", err)

    async def async_turn_off(self, **kwargs: Any) -> None:
        try:
            await self._do_write(False, self._device.set_master(False))
        except MD44Error as err:
            _LOGGER.error("Failed to turn off master: %s", err)


class MD44ChannelSwitch(_MD44SwitchBase):
    """A single dosing-channel ON/OFF switch (channels 1..4)."""

    _attr_icon = "mdi:water-pump"

    def __init__(self, coordinator, device_id, model, host, device, idx, mac_address=None, firmware_version=None) -> None:
        super().__init__(coordinator, device_id, model, host, device, mac_address, firmware_version)
        self._idx = idx
        ch_number = idx + 1
        self._attr_unique_id = f"{device_id}_channel_{ch_number}"
        self._attr_name = f"Channel {ch_number}"
        self._attr_translation_key = f"channel_{ch_number}"

    def _coordinator_value(self) -> bool:
        state = self.coordinator.data.get("state")
        if not state:
            return False
        return state.channels[self._idx]

    async def async_turn_on(self, **kwargs: Any) -> None:
        try:
            await self._do_write(True, self._device.set_channel(self._idx, True))
        except MD44Error as err:
            _LOGGER.error("Failed to turn on channel %d: %s", self._idx + 1, err)

    async def async_turn_off(self, **kwargs: Any) -> None:
        try:
            await self._do_write(False, self._device.set_channel(self._idx, False))
        except MD44Error as err:
            _LOGGER.error("Failed to turn off channel %d: %s", self._idx + 1, err)


class MD44TimerEnableSwitch(_MD44SwitchBase):
    """Timer-enable switch for one channel.

    When on, the pump runs the channel's stored CH*SWTime schedule
    automatically. When off, only manual ``set_channel`` calls dispense.
    """

    _attr_icon = "mdi:timer-cog"

    def __init__(self, coordinator, device_id, model, host, device, idx, mac_address=None, firmware_version=None) -> None:
        super().__init__(coordinator, device_id, model, host, device, mac_address, firmware_version)
        self._idx = idx
        ch_number = idx + 1
        self._attr_unique_id = f"{device_id}_timer_{ch_number}"
        self._attr_name = f"Timer {ch_number}"
        self._attr_translation_key = f"timer_{ch_number}"

    def _coordinator_value(self) -> bool:
        state = self.coordinator.data.get("state")
        if not state:
            return False
        return state.timers_enabled[self._idx]

    async def async_turn_on(self, **kwargs: Any) -> None:
        try:
            await self._do_write(True, self._device.set_timer_enabled(self._idx, True))
        except MD44Error as err:
            _LOGGER.error("Failed to enable timer %d: %s", self._idx + 1, err)

    async def async_turn_off(self, **kwargs: Any) -> None:
        try:
            await self._do_write(False, self._device.set_timer_enabled(self._idx, False))
        except MD44Error as err:
            _LOGGER.error("Failed to disable timer %d: %s", self._idx + 1, err)


class MD44CalFactorSwitch(JebaoEntity, SwitchEntity):
    """Toggles the 10x calibration-factor mode for this pump.

    Stored in the config entry's options (so it persists across HA
    restarts) rather than as a pump-side attribute — the pump has no
    concept of this multiplier, it's purely an HA-side display/scaling
    trick documented in ``const.OPT_CAL_FACTOR_10X``.

    Flipping this switch doesn't touch the pump at all. Schedule and dose
    calculator entities read the toggle on each update and adjust their
    visible numbers accordingly.
    """

    _attr_translation_key = "cal_factor_10x"
    _attr_icon = "mdi:numeric-10-box-multiple-outline"
    _attr_entity_category = "config"

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
        self._attr_unique_id = f"{device_id}_cal_factor_10x"
        self._attr_name = "10x dose precision"

    @property
    def is_on(self) -> bool:
        return bool(self._entry.options.get(OPT_CAL_FACTOR_10X, False))

    async def _set(self, value: bool) -> None:
        new_options = dict(self._entry.options)
        new_options[OPT_CAL_FACTOR_10X] = value
        self.hass.config_entries.async_update_entry(self._entry, options=new_options)
        self.async_write_ha_state()
        # Other entities re-read on the next coordinator tick. Nudge it so
        # the dose calculator + schedule text update immediately.
        await self.coordinator.async_request_refresh()

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self._set(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self._set(False)
