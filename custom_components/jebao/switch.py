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

from .const import DOMAIN, MD44_CHANNEL_COUNT, MODEL_MD44
from .coordinator import JebaoDataUpdateCoordinator
from .entity import JebaoEntity
from .md44 import MD44Device, MD44Error

_LOGGER = logging.getLogger(__name__)

# Seconds to wait after a write before polling the cloud again. The pump
# acknowledges the change locally within a few ms, but the cloud's
# ``/app/devdata/.../latest`` cache only refreshes after the device sends an
# MQTT state push — typically ~2 seconds. Refreshing sooner reads the stale
# value and makes the switch flap back to the old state.
WRITE_VERIFY_DELAY = 3.0


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
        """Run the cloud write, pin the optimistic state, and schedule a
        delayed refresh once the cloud has had time to learn the new value."""
        self._set_optimistic(target)
        try:
            await write_coro
        except MD44Error as err:
            # Roll back to whatever the coordinator believes.
            self._clear_optimistic()
            raise err

        async def _verify() -> None:
            await asyncio.sleep(WRITE_VERIFY_DELAY)
            try:
                await self.coordinator.async_request_refresh()
            finally:
                # Once the coordinator has a fresh value, drop the pin so
                # the cloud's authoritative answer wins.
                self._clear_optimistic()

        self.hass.async_create_task(_verify())


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
