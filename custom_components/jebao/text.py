"""Text platform for Jebao MD-4.4 — schedule editor.

One ``text`` entity per channel exposes that channel's full schedule as
a single editable string. Users can change the schedule from the regular
HA UI (Developer Tools → Entity → set value, or click the entity in the
device card) without having to call a service.

Format: ``HH:MM=mL, HH:MM=mL, ...`` (max 24 entries). Whitespace is
flexible. Empty string clears the channel. Examples::

    12:00=3                     -> one daily 3 mL dose at noon
    09:00=2, 21:00=2            -> two doses (morning + evening)
                                -> (empty)  clears all 24 slots
"""
from __future__ import annotations

import asyncio
import logging
import re
from typing import Optional

from homeassistant.components.text import TextEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import (
    DOMAIN,
    MD44_CHANNEL_COUNT,
    MODEL_MD44,
    cal_factor,
    signal_cal_factor_changed,
)
from .coordinator import JebaoDataUpdateCoordinator
from .entity import JebaoEntity
from .md44 import MD44Device, MD44Error, ScheduleEntry
from homeassistant.helpers.dispatcher import async_dispatcher_connect

_LOGGER = logging.getLogger(__name__)

# Match HH:MM=mL where mL can be either an integer ("3") or a single-decimal
# float ("1.4") — the latter is the user-visible representation when the 10x
# calibration mode is on.
_ENTRY_RE = re.compile(
    r"\s*(\d{1,2})\s*:\s*(\d{1,2})\s*=\s*(\d{1,3}(?:\.\d)?)\s*"
)


def parse_schedule_text(text: str, factor: int = 1) -> list[ScheduleEntry]:
    """Parse ``HH:MM=mL[, HH:MM=mL ...]`` into ScheduleEntry list.

    ``factor`` scales the user-visible mL up to the integer the firmware
    actually stores (1 for normal mode, 10 for the 10x precision mode).
    Raises ``ValueError`` on bad format / out-of-range values.
    """
    text = (text or "").strip()
    if not text:
        return []
    entries: list[ScheduleEntry] = []
    for piece in text.split(","):
        if not piece.strip():
            continue
        m = _ENTRY_RE.fullmatch(piece)
        if not m:
            raise ValueError(
                f"Bad entry {piece!r}; expected HH:MM=mL (e.g. 12:00=3)"
            )
        hour = int(m.group(1))
        minute = int(m.group(2))
        ml_real = float(m.group(3))
        if not 0 <= hour <= 23:
            raise ValueError(f"hour out of range in {piece!r}: {hour}")
        if not 0 <= minute <= 59:
            raise ValueError(f"minute out of range in {piece!r}: {minute}")
        raw = int(round(ml_real * factor))
        if not 0 <= raw <= 255:
            raise ValueError(
                f"quantity out of range in {piece!r}: {ml_real} mL × {factor}x = {raw}"
            )
        entries.append(ScheduleEntry(hour=hour, minute=minute, quantity=raw))
    return entries


def format_schedule(entries, factor: int = 1) -> str:
    if not entries:
        return ""
    if factor == 1:
        return ", ".join(
            f"{e.hour:02d}:{e.minute:02d}={int(e.quantity)}" for e in entries
        )
    return ", ".join(
        f"{e.hour:02d}:{e.minute:02d}={e.quantity / factor:.1f}" for e in entries
    )


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    data = hass.data[DOMAIN][entry.entry_id]
    if data["model"] != MODEL_MD44:
        return

    device: MD44Device = data["device"]
    device_id = data["device_id"]
    model = data["model"]
    host = data["host"]
    mac_address = data.get("mac_address")
    firmware_version = data.get("firmware_version")

    # Coordinator is built up front in __init__.py so all platforms
    # share one. Just grab it.
    coordinator = data["coordinator"]

    entities: list[TextEntity] = []
    for idx in range(MD44_CHANNEL_COUNT):
        entities.append(
            MD44ScheduleText(
                coordinator, device_id, model, host, device, idx, entry,
                mac_address, firmware_version,
            )
        )
    async_add_entities(entities)


class MD44ScheduleText(JebaoEntity, TextEntity):
    """One channel's complete dosing schedule as a single editable string."""

    _attr_icon = "mdi:calendar-edit"
    _attr_mode = "text"
    # Max length: 24 entries × "HH:MM=NNN, " = 24 * 12 = 288 chars, give some slack
    _attr_native_max = 400

    def __init__(
        self,
        coordinator: JebaoDataUpdateCoordinator,
        device_id: str,
        model: str,
        host: str,
        device: MD44Device,
        idx: int,
        entry,
        mac_address: str | None = None,
        firmware_version: str | None = None,
    ) -> None:
        super().__init__(coordinator, device_id, model, host, mac_address, firmware_version)
        self._device = device
        self._idx = idx
        self._entry = entry
        self._optimistic: Optional[str] = None
        self._verify_task: Optional[asyncio.Task] = None
        ch = idx + 1
        self._attr_unique_id = f"{device_id}_schedule_text_{ch}"
        self._attr_name = f"Channel {ch} schedule"
        self._attr_translation_key = f"schedule_{ch}"

    def _factor(self) -> int:
        return cal_factor(self._entry.options)

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        # When the user flips the 10x precision toggle, re-render so the
        # schedule string switches between integer and one-decimal-mL
        # display right away rather than waiting for the next poll.
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass,
                signal_cal_factor_changed(self._entry.entry_id),
                self.async_write_ha_state,
            )
        )

    def _coordinator_value(self) -> str:
        state = self.coordinator.data.get("state")
        if state is None:
            return ""
        return format_schedule(state.schedules[self._idx], self._factor())

    @property
    def native_value(self) -> str | None:
        if self._optimistic is not None:
            return self._optimistic
        return self._coordinator_value()

    @property
    def extra_state_attributes(self) -> dict:
        """Expose the schedule as structured data so users can pull it into
        dashboards without having to parse the text value.

        ``entries`` is a list of ``{slot, time, hour, minute, quantity_ml}``
        in storage order — slot 1 is the first entry in the text value, etc.
        Convenience top-level fields (``slot_1_time``, ``slot_1_ml``, ...)
        cover the common case of pinning just the first one or two slots to
        a Lovelace card without templating.
        """
        state = self.coordinator.data.get("state")
        factor = self._factor()
        if state is None:
            return {"entry_count": 0, "entries": [], "factor": factor}
        entries = state.schedules[self._idx]
        structured = []
        for i, e in enumerate(entries, start=1):
            qty = e.quantity / factor if factor != 1 else e.quantity
            structured.append({
                "slot": i,
                "time": f"{e.hour:02d}:{e.minute:02d}",
                "hour": e.hour,
                "minute": e.minute,
                "quantity_ml": qty,
            })
        attrs: dict = {
            "entry_count": len(structured),
            "entries": structured,
            "factor": factor,
        }
        # Flatten the first three entries into top-level attributes so users
        # can drop them into an Entity card with attribute: slot_1_time
        # rather than having to template into a list.
        for i, entry in enumerate(structured[:3], start=1):
            attrs[f"slot_{i}_time"] = entry["time"]
            attrs[f"slot_{i}_ml"] = entry["quantity_ml"]
        return attrs

    async def async_set_value(self, value: str) -> None:
        """Replace the channel's schedule. Empty string clears it."""
        factor = self._factor()
        try:
            entries = parse_schedule_text(value, factor=factor)
        except ValueError as err:
            _LOGGER.error("Channel %d: bad schedule string: %s", self._idx + 1, err)
            return

        # Cancel any verify in flight from a previous edit.
        if self._verify_task and not self._verify_task.done():
            self._verify_task.cancel()
            self._verify_task = None

        # Reformat so the optimistic view matches what we wrote.
        target = format_schedule(entries, factor)
        self._optimistic = target
        self.async_write_ha_state()

        try:
            await self._device.set_schedule(self._idx, entries)
        except MD44Error as err:
            _LOGGER.error("Channel %d: schedule write failed: %s", self._idx + 1, err)
            self._optimistic = None
            self.async_write_ha_state()
            return

        ws_on = getattr(self._device, "ws_connected", False)
        # WS connected: cloud pushes the new schedule back within ~1 s, so
        # we just wait and check — no REST polls. WS down: poll as before.
        delays = (2.0, 3.0, 5.0) if ws_on else (5.0, 4.0, 4.0)

        async def _verify() -> None:
            try:
                for delay in delays:
                    await asyncio.sleep(delay)
                    if not ws_on:
                        try:
                            await self.coordinator.async_request_refresh()
                        except Exception:  # pylint: disable=broad-except
                            continue
                    if self._coordinator_value() == target:
                        return
            except asyncio.CancelledError:
                return
            finally:
                self._optimistic = None
                self.async_write_ha_state()

        self._verify_task = self.hass.async_create_task(_verify())
