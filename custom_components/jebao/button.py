"""Button platform for Jebao."""
from __future__ import annotations

import logging

from jebao import JebaoError

from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN, MODEL_MD44
from .coordinator import JebaoDataUpdateCoordinator
from .entity import JebaoEntity
from .md44 import MD44Error

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Jebao buttons from config entry."""
    data = hass.data[DOMAIN][entry.entry_id]

    if data["model"] == MODEL_MD44:
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

        async_add_entities(
            [
                MD44SyncTimeButton(coordinator, device_id, model, host, device, mac_address, firmware_version),
            ]
        )
        return

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

    async_add_entities(
        [
            JebaoStartFeedButton(coordinator, device_id, model, host, device, mac_address, firmware_version),
            JebaoCancelFeedButton(coordinator, device_id, model, host, device, mac_address, firmware_version),
        ]
    )


class JebaoStartFeedButton(JebaoEntity, ButtonEntity):
    """Button to start feed mode."""

    _attr_translation_key = "start_feed"

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
        """Initialize button."""
        super().__init__(coordinator, device_id, model, host, mac_address, firmware_version)
        self._device = device
        self._attr_unique_id = f"{device_id}_start_feed"
        self._attr_name = "Start feed"
        self._attr_icon = "mdi:fishbowl"

    async def async_press(self) -> None:
        """Handle button press."""
        try:
            await self._device.start_feed()
            await self.coordinator.async_request_refresh()
            _LOGGER.info("Feed mode started")

        except JebaoError as err:
            _LOGGER.error("Failed to start feed mode: %s", err)


class MD44SyncTimeButton(JebaoEntity, ButtonEntity):
    """Push the local wall clock to the MD-4.4. The pump's schedules fire
    against its own clock, so without a sync it can drift far from real time."""

    _attr_translation_key = "sync_time"
    _attr_icon = "mdi:clock-edit-outline"

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
        self._attr_unique_id = f"{device_id}_sync_time"
        self._attr_name = "Sync clock"

    async def async_press(self) -> None:
        try:
            await self._device.sync_time()
            await self.coordinator.async_request_refresh()
            _LOGGER.info("MD-4.4 clock synced")
        except MD44Error as err:
            _LOGGER.error("Failed to sync clock: %s", err)


class JebaoCancelFeedButton(JebaoEntity, ButtonEntity):
    """Button to cancel feed mode."""

    _attr_translation_key = "cancel_feed"

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
        """Initialize button."""
        super().__init__(coordinator, device_id, model, host, mac_address, firmware_version)
        self._device = device
        self._attr_unique_id = f"{device_id}_cancel_feed"
        self._attr_name = "Cancel feed"
        self._attr_icon = "mdi:cancel"

    async def async_press(self) -> None:
        """Handle button press."""
        try:
            await self._device.cancel_feed()
            await self.coordinator.async_request_refresh()
            _LOGGER.info("Feed mode canceled")

        except JebaoError as err:
            _LOGGER.error("Failed to cancel feed mode: %s", err)
