"""Jebao MD-4.4 dosing pump — cloud-based device class.

We tried the LAN protocol on TCP/12416 first. Reads work (the pump returns
a P0 buffer in response to 0x90), but the firmware on this device class
silently drops 0x93 control commands — the ACK comes back fine, but the
state never changes. tancou's mask+state format was verified byte-for-byte
against his reference frames and against MQTT captures from older
firmware, so the format itself is correct; the firmware just doesn't honour
it locally. The official Jebao Aqua app on Android works because it
routes control through the Gizwits cloud (``usapi.gizwits.com``), and the
cloud forwards changes to the pump over MQTT/m2m.

So this class talks to the cloud REST API. ``connect()`` logs in with the
user's Gizwits credentials and ``update()`` pulls the latest datapoint
values. Each ``set_*`` method POSTs to ``/app/control/<did>`` with the
attribute name(s) the cloud expects.

The firmware exposes 8 channels even on the physical 4-head pump body;
we surface all 8 so users running a different doser variant don't see
missing entities.
"""
from __future__ import annotations

import binascii
import datetime as dt
import logging
from dataclasses import dataclass, field
from typing import Any, List, Optional

import aiohttp

from .const import MD44_CHANNEL_COUNT
from .gizwits_cloud import GizwitsAuthError, GizwitsCloudClient, GizwitsCloudError

_LOGGER = logging.getLogger(__name__)


SCHEDULES_PER_CHANNEL = 24
SCHEDULE_ENTRY_LEN = 4
SCHEDULE_BLOB_LEN = 96  # bytes


class MD44Error(Exception):
    """Top-level error for callers."""


class MD44ConnectionError(MD44Error):
    """Couldn't reach the cloud / authentication failed."""


@dataclass
class ScheduleEntry:
    hour: int
    minute: int
    quantity: int  # raw 16-bit value the firmware stores

    @property
    def quantity_ml(self) -> float:
        # The firmware stores dosing volume in 0.1 mL increments.
        return self.quantity / 10.0


@dataclass
class MD44State:
    master_on: bool = False
    channels: List[bool] = field(default_factory=lambda: [False] * MD44_CHANNEL_COUNT)
    timers_enabled: List[bool] = field(default_factory=lambda: [False] * MD44_CHANNEL_COUNT)
    intervals_days: List[int] = field(default_factory=lambda: [0] * MD44_CHANNEL_COUNT)
    schedules: List[List[ScheduleEntry]] = field(
        default_factory=lambda: [[] for _ in range(MD44_CHANNEL_COUNT)]
    )
    cal_switch: bool = False
    cal_set: str = ""        # cloud returns localized label like "校准1"
    calib1: int = 0
    ymd: str = "00000000"
    hms: str = "00000000"
    channel_ttl: int = 0
    time1: int = 0
    open_circuit: bool = False
    fault_uart: bool = False

    def as_dict(self) -> dict:
        return {
            "master_on": self.master_on,
            "channels": list(self.channels),
            "timers_enabled": list(self.timers_enabled),
            "intervals_days": list(self.intervals_days),
            "cal_switch": self.cal_switch,
            "cal_set": self.cal_set,
            "calib1": self.calib1,
            "ymd": self.ymd,
            "hms": self.hms,
            "channel_ttl": self.channel_ttl,
            "open_circuit": self.open_circuit,
            "fault_uart": self.fault_uart,
            "schedules": [
                [(s.hour, s.minute, s.quantity) for s in chan]
                for chan in self.schedules
            ],
        }


def _parse_schedule_blob(hex_blob: str) -> List[ScheduleEntry]:
    """Parse a CH*SWTime hex string into structured entries.

    Layout: 96 bytes (192 hex chars). Byte 0 is reserved; bytes 1..96 hold
    up to 24 entries of 4 bytes each: hour, minute, quantity_hi, quantity_lo.
    All-zero entries are skipped.
    """
    if not hex_blob:
        return []
    try:
        blob = binascii.unhexlify(hex_blob)
    except (binascii.Error, ValueError):
        return []
    out: List[ScheduleEntry] = []
    for i in range(SCHEDULES_PER_CHANNEL):
        base = 1 + i * SCHEDULE_ENTRY_LEN
        if base + SCHEDULE_ENTRY_LEN > len(blob):
            break
        hour, minute, qhi, qlo = blob[base : base + SCHEDULE_ENTRY_LEN]
        quantity = (qhi << 8) | qlo
        if hour == 0 and minute == 0 and quantity == 0:
            continue
        out.append(ScheduleEntry(hour=hour, minute=minute, quantity=quantity))
    return out


def _attrs_to_state(attr: dict[str, Any]) -> MD44State:
    """Build an ``MD44State`` from the cloud's ``/app/devdata/.../latest`` payload.

    Missing keys default to False/0 — devices in different firmware variants
    may expose only a subset of channels.
    """
    s = MD44State()
    s.master_on = bool(attr.get("switch", 0))
    s.channels = [bool(attr.get(f"channe{i + 1}", 0)) for i in range(MD44_CHANNEL_COUNT)]
    s.timers_enabled = [bool(attr.get(f"Timer{i + 1}ON", 0)) for i in range(MD44_CHANNEL_COUNT)]
    s.intervals_days = [int(attr.get(f"IntervalT{i + 1}", 0)) for i in range(MD44_CHANNEL_COUNT)]
    s.schedules = [_parse_schedule_blob(attr.get(f"CH{i + 1}SWTime", "")) for i in range(MD44_CHANNEL_COUNT)]
    s.cal_switch = bool(attr.get("CALSW", 0))
    s.cal_set = str(attr.get("CALSet", ""))
    s.calib1 = int(attr.get("Calib1", 0))
    s.ymd = str(attr.get("YMDData", "00000000"))
    s.hms = str(attr.get("HMSData", "00000000"))
    s.channel_ttl = int(attr.get("channelTTL", 0))
    s.time1 = int(attr.get("time1", 0))
    s.open_circuit = bool(attr.get("OpenCircuit", 0))
    s.fault_uart = bool(attr.get("Fault_UART", 0))
    return s


class MD44Device:
    """Cloud-backed client for the MD-4.4 doser."""

    model = "MD-4.4"

    def __init__(
        self,
        session: aiohttp.ClientSession,
        username: str,
        password: str,
        region: str,
        did: str,
        device_id: Optional[str] = None,
    ) -> None:
        self._cloud = GizwitsCloudClient(session, region=region)
        self._username = username
        self._password = password
        self.did = did
        # Keep ``device_id`` for compatibility with the existing entity layer
        # that uses it as part of unique_id. The cloud uses ``did``.
        self.device_id = device_id or did
        self.state = MD44State()
        self._connected = False

    @property
    def host(self) -> str:
        # Kept for the existing device_info link; the cloud doesn't have a
        # host so we display the region.
        return f"gizwits/{self._cloud.region}"

    @host.setter
    def host(self, value: str) -> None:
        # Coordinator may set this when recovering an IP change for the
        # MDP-20000; the cloud client doesn't use it.
        pass

    @property
    def is_connected(self) -> bool:
        return self._connected and not self._cloud.needs_relogin()

    async def connect(self, timeout: float = 10.0) -> None:  # noqa: D401
        """Log in to the cloud and prove we can reach the device."""
        try:
            await self._cloud.login(self._username, self._password)
            await self.update()
            self._connected = True
        except GizwitsAuthError as err:
            raise MD44ConnectionError(f"Auth failed: {err}") from err
        except GizwitsCloudError as err:
            raise MD44ConnectionError(f"Cloud unreachable: {err}") from err

    async def disconnect(self) -> None:
        # Nothing to close — the client just uses the shared aiohttp session.
        self._connected = False

    async def update(self) -> MD44State:
        try:
            attr = await self._cloud.get_device_data(self.did)
        except GizwitsCloudError as err:
            raise MD44ConnectionError(str(err)) from err
        self.state = _attrs_to_state(attr)
        return self.state

    # ------------------------------------------------------------------ writes

    async def _write(self, attrs: dict[str, Any]) -> None:
        try:
            await self._cloud.control(self.did, attrs)
        except GizwitsCloudError as err:
            raise MD44Error(str(err)) from err

    async def set_master(self, on: bool) -> None:
        _LOGGER.info("MD-4.4: master switch -> %s", on)
        await self._write({"switch": 1 if on else 0})

    async def set_channel(self, idx: int, on: bool) -> None:
        if not 0 <= idx < MD44_CHANNEL_COUNT:
            raise ValueError(f"channel idx out of range: {idx}")
        _LOGGER.info("MD-4.4: channel %d -> %s", idx + 1, on)
        await self._write({f"channe{idx + 1}": 1 if on else 0})

    async def set_timer_enabled(self, idx: int, on: bool) -> None:
        if not 0 <= idx < MD44_CHANNEL_COUNT:
            raise ValueError(f"timer idx out of range: {idx}")
        _LOGGER.info("MD-4.4: timer %d enabled -> %s", idx + 1, on)
        await self._write({f"Timer{idx + 1}ON": 1 if on else 0})

    async def set_interval_days(self, idx: int, days: int) -> None:
        if not 0 <= idx < MD44_CHANNEL_COUNT:
            raise ValueError(f"interval idx out of range: {idx}")
        if not 0 <= days <= 30:
            raise ValueError(f"interval days out of range: {days}")
        _LOGGER.info("MD-4.4: IntervalT%d -> %d days", idx + 1, days)
        await self._write({f"IntervalT{idx + 1}": int(days)})

    async def sync_time(self, now: Optional[dt.datetime] = None) -> None:
        """Write the pump's clock to the local wall-clock time."""
        if now is None:
            now = dt.datetime.now()
        # YMDData: yy mm dd dow (dow: Sun=0, Mon=1, ... per the firmware)
        ymd = bytes(
            [
                (now.year - 2000) & 0xFF,
                now.month,
                now.day,
                now.isoweekday() % 7,
            ]
        ).hex()
        hms = bytes([now.hour, now.minute, now.second, 0]).hex()
        await self._write({"YMDData": ymd, "HMSData": hms})
