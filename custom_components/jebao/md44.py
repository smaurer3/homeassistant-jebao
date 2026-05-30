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

import asyncio
import binascii
import datetime as dt
import logging
from dataclasses import dataclass, field
from typing import Any, Callable, List, Optional

import aiohttp

from .const import GIZWITS_APP_ID, MD44_CHANNEL_COUNT
from .gizwits_cloud import GizwitsAuthError, GizwitsCloudClient, GizwitsCloudError
from .gizwits_ws import GizwitsWebSocketClient, GizwitsWebSocketError

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
    quantity: int  # mL — whole-mL units, max ~255 per dose

    @property
    def quantity_ml(self) -> float:
        return float(self.quantity)


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


def _parse_schedule_blob(raw) -> List[ScheduleEntry]:
    """Parse a CH*SWTime blob into structured entries.

    Accepts both formats the Gizwits cloud uses:

      * Hex string (from ``GET /app/devdata/.../latest``) — 192 chars,
        96 bytes total.
      * List of ints (from ``s2c_noti`` push messages over WebSocket) —
        the same 96 bytes already decoded.

    Layout in either case: 24 entries of 4 bytes each, ``[hour, minute,
    reserved, quantity_mL]``. Verified by setting a known schedule via
    the app (CH1 = 3 mL @ 12:00) and reading it back as ``0c 00 00 03``
    at bytes 0..3. All-zero entries are skipped.
    """
    if not raw:
        return []
    if isinstance(raw, (bytes, bytearray)):
        blob = bytes(raw)
    elif isinstance(raw, str):
        try:
            blob = binascii.unhexlify(raw)
        except (binascii.Error, ValueError):
            return []
    elif isinstance(raw, list):
        try:
            blob = bytes(int(x) & 0xFF for x in raw)
        except (TypeError, ValueError):
            return []
    else:
        return []
    out: List[ScheduleEntry] = []
    for i in range(SCHEDULES_PER_CHANNEL):
        base = i * SCHEDULE_ENTRY_LEN
        if base + SCHEDULE_ENTRY_LEN > len(blob):
            break
        hour, minute, _reserved, quantity = blob[base : base + SCHEDULE_ENTRY_LEN]
        if hour == 0 and minute == 0 and quantity == 0:
            continue
        out.append(ScheduleEntry(hour=hour, minute=minute, quantity=quantity))
    return out


def _ymd_to_hex(raw) -> str:
    """``YMDData`` / ``HMSData`` come as 8-char hex strings from REST but
    as 4-element int lists from WS pushes. Normalise to the hex string
    the rest of the integration expects."""
    if isinstance(raw, str):
        return raw
    if isinstance(raw, list):
        try:
            return bytes(int(x) & 0xFF for x in raw).hex()
        except (TypeError, ValueError):
            return "00000000"
    if isinstance(raw, (bytes, bytearray)):
        return bytes(raw).hex()
    return "00000000"


def _schedule_to_hex(entries: list) -> str:
    """Pack a ScheduleEntry list back into the 96-byte hex blob the cloud
    uses. Inverse of ``_parse_schedule_blob``."""
    blob = bytearray(SCHEDULE_BLOB_LEN)
    for i, entry in enumerate(entries[:SCHEDULES_PER_CHANNEL]):
        base = i * SCHEDULE_ENTRY_LEN
        blob[base] = int(entry.hour) & 0xFF
        blob[base + 1] = int(entry.minute) & 0xFF
        blob[base + 2] = 0
        blob[base + 3] = max(0, min(255, int(entry.quantity)))
    return bytes(blob).hex()


def _state_to_attrs(state: MD44State) -> dict[str, Any]:
    """Render an MD44State back to the same key/value shape the cloud's
    ``/app/devdata/.../latest`` returns. Used so partial push updates
    (which only include changed keys) can be merged onto a known-good
    baseline without losing the rest of the state."""
    out: dict[str, Any] = {
        "switch": 1 if state.master_on else 0,
        "CALSW": 1 if state.cal_switch else 0,
        "CALSet": state.cal_set,
        "Calib1": state.calib1,
        "YMDData": state.ymd,
        "HMSData": state.hms,
        "channelTTL": state.channel_ttl,
        "time1": state.time1,
        "OpenCircuit": 1 if state.open_circuit else 0,
        "Fault_UART": 1 if state.fault_uart else 0,
    }
    for i in range(MD44_CHANNEL_COUNT):
        out[f"channe{i + 1}"] = 1 if state.channels[i] else 0
        out[f"Timer{i + 1}ON"] = 1 if state.timers_enabled[i] else 0
        out[f"IntervalT{i + 1}"] = state.intervals_days[i]
        out[f"CH{i + 1}SWTime"] = _schedule_to_hex(state.schedules[i])
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
    s.ymd = _ymd_to_hex(attr.get("YMDData", "00000000"))
    s.hms = _ymd_to_hex(attr.get("HMSData", "00000000"))
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
        self._session = session
        self._region = region
        self._username = username
        self._password = password
        self.did = did
        # Keep ``device_id`` for compatibility with the existing entity layer
        # that uses it as part of unique_id. The cloud uses ``did``.
        self.device_id = device_id or did
        self.state = MD44State()
        self._connected = False
        # WebSocket push subscription — created in ``connect`` after we
        # have a fresh REST token. ``on_push_state`` is called from
        # ``__init__`` registration; the integration sets this after
        # the coordinator exists so the push handler can forward to it.
        self._ws: Optional[GizwitsWebSocketClient] = None
        # Callback fired on every WS push after state is merged. The
        # integration's coordinator wires this up so HA entities see the
        # new state immediately. May return None (sync) or a coroutine
        # (async); ``_handle_push`` awaits the latter.
        self.on_push_state: Optional[Callable[[MD44State], Any]] = None

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

    @property
    def ws_connected(self) -> bool:
        """Whether the WebSocket push subscription is currently live.
        Coordinator uses this to skip routine polling once push is
        flowing."""
        return self._ws is not None and self._ws.connected

    async def connect(self, timeout: float = 10.0) -> None:  # noqa: D401
        """Log in to the cloud, fetch initial state, and start the
        WebSocket push subscription."""
        try:
            await self._cloud.login(self._username, self._password)
            await self.update()
            self._connected = True
        except GizwitsAuthError as err:
            raise MD44ConnectionError(f"Auth failed: {err}") from err
        except GizwitsCloudError as err:
            raise MD44ConnectionError(f"Cloud unreachable: {err}") from err

        # Spin up the WS subscription. We never raise if WS fails — the
        # REST fallback in the coordinator keeps the integration working.
        self._ws = GizwitsWebSocketClient(
            session=self._session,
            app_id=GIZWITS_APP_ID,
            region=self._region,
            uid=self._cloud.uid or "",
            token=self._cloud.token or "",
            token_refresh=self._refresh_token,
        )
        self._ws.register_device(self.did, self._handle_push)
        await self._ws.start()
        # Wait briefly for the handshake to complete so the coordinator's
        # first refresh already sees ws_connected=True and picks the
        # slower fallback interval. If the handshake takes longer than
        # this the WS keeps trying in the background and the coordinator
        # will swap interval on the next poll.
        await self._ws.wait_until_connected(timeout=5.0)

    async def disconnect(self) -> None:
        if self._ws is not None:
            await self._ws.stop()
            self._ws = None
        self._connected = False

    async def update(self) -> MD44State:
        try:
            attr = await self._cloud.get_device_data(self.did)
        except GizwitsCloudError as err:
            raise MD44ConnectionError(str(err)) from err
        self.state = _attrs_to_state(attr)
        return self.state

    async def _refresh_token(self) -> None:
        """Called from the WS client when the cloud rejected its token
        mid-flight. Re-login via REST then hand the fresh credentials
        back to the WS client for the next reconnect."""
        await self._cloud.login(self._username, self._password)
        if self._ws is not None:
            self._ws.update_credentials(self._cloud.uid or "", self._cloud.token or "")

    async def _handle_push(self, did: str, attrs: dict[str, Any]) -> None:
        """``s2c_noti`` callback. Merges the incoming partial ``attrs``
        dict into the cached state (the cloud only sends changed keys)
        and forwards the new state to whatever the coordinator
        registered with ``on_push_state``."""
        if did != self.did:
            return
        merged = _state_to_attrs(self.state)
        merged.update(attrs)
        self.state = _attrs_to_state(merged)
        callback = self.on_push_state
        if callback is None:
            return
        result = callback(self.state)
        if asyncio.iscoroutine(result):
            await result

    # ------------------------------------------------------------------ writes

    async def _write(self, attrs: dict[str, Any]) -> None:
        """Send a control command via REST.

        I tested c2s_write on the WebSocket channel — the cloud ACKs it
        but the change never actually reaches the device. REST control
        works reliably and the cloud pushes the new state back over the
        WebSocket within ~1 second, so the UI updates near-realtime
        anyway. The handful of REST POSTs we still make for writes is
        dwarfed by the polling we no longer do.
        """
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

    async def set_schedule(
        self, channel_idx: int, entries: List[ScheduleEntry]
    ) -> None:
        """Overwrite one channel's CH*SWTime blob with the given entries.

        Entries are written in order starting at byte 0; remaining slots up
        to 24 are zeroed. Quantity is clamped to 0..255 mL.
        """
        if not 0 <= channel_idx < MD44_CHANNEL_COUNT:
            raise ValueError(f"channel idx out of range: {channel_idx}")
        if len(entries) > SCHEDULES_PER_CHANNEL:
            raise ValueError(
                f"max {SCHEDULES_PER_CHANNEL} schedule entries per channel"
            )
        blob = bytearray(SCHEDULE_BLOB_LEN)
        for i, e in enumerate(entries):
            base = i * SCHEDULE_ENTRY_LEN
            blob[base] = e.hour & 0xFF
            blob[base + 1] = e.minute & 0xFF
            blob[base + 2] = 0
            blob[base + 3] = max(0, min(255, int(e.quantity)))
        attr = f"CH{channel_idx + 1}SWTime"
        _LOGGER.info("MD-4.4: writing %d schedule entries to %s", len(entries), attr)
        await self._write({attr: bytes(blob).hex()})

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
